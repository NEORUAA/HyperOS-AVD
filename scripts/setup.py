#!/usr/bin/env python3
"""Install a verified release and register an isolated Android Studio AVD."""
import argparse
import json
from pathlib import Path
import re
import shutil
import tarfile
import tempfile
import urllib.parse
import urllib.request

from common import (ROOT, DEFAULT_NAME, DEFAULT_PORT, avd_home, fetch_ksu, firmware_idle,
                    host_check, port_free, sdk_path, sha256)


def read_manifest(value):
    if value.startswith('https://'):
        with urllib.request.urlopen(value, timeout=30) as response:
            manifest = json.load(response)
        base = value.rsplit('/', 1)[0] + '/'
    else:
        path = Path(value).expanduser().resolve()
        manifest = json.loads(path.read_text())
        base = path.parent
    if manifest.get('project') != 'HyperOS-AVD' or manifest.get('format') != 1:
        raise RuntimeError('Unsupported HyperOS-AVD release manifest.')
    if not re.fullmatch(r'v[0-9A-Za-z_.-]+', manifest.get('version', '')):
        raise RuntimeError('Invalid release version.')
    if manifest.get('platform') != 'macos-arm64':
        raise RuntimeError('This installer supports macos-arm64 releases only.')
    return manifest, base


def install_bundle(value):
    manifest, base = read_manifest(value)
    cache = ROOT / 'downloads' / manifest['version']
    cache.mkdir(parents=True, exist_ok=True)
    parts = []
    for entry in manifest['parts']:
        name = entry['name']
        if not re.fullmatch(r'[A-Za-z0-9_.-]+', name):
            raise RuntimeError('Invalid release asset name.')
        if isinstance(base, Path):
            path = base / name
        else:
            path = cache / name
            if not path.exists():
                print('Downloading:', name, flush=True)
                partial = path.with_suffix(path.suffix + '.part')
                urllib.request.urlretrieve(urllib.parse.urljoin(base, name), partial)
                partial.replace(path)
        if path.stat().st_size != entry['size'] or sha256(path) != entry['sha256']:
            raise RuntimeError('Release checksum mismatch: ' + str(path))
        parts.append(path)
    # Concatenate compressed chunks once; tar extraction never follows links.
    with tempfile.TemporaryDirectory(prefix='hyperos-install-', dir=cache) as temporary:
        staging = Path(temporary)
        archive = staging / 'bundle.tar.gz'
        with archive.open('wb') as output:
            for path in parts:
                with path.open('rb') as source:
                    shutil.copyfileobj(source, output, 8 * 1024**2)
        expected = manifest['files']
        seen = set()
        with tarfile.open(archive, 'r:gz') as tar:
            for member in tar:
                relative = Path(member.name)
                if (not member.isfile() or relative.is_absolute() or '..' in relative.parts
                        or member.name not in expected
                        or relative.parts[0] not in ('images', 'tools')):
                    raise RuntimeError('Unexpected release archive entry: ' + member.name)
                if member.name in seen:
                    raise RuntimeError('Duplicate release archive entry: ' + member.name)
                target = staging / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile(member) as source, target.open('wb') as output:
                    shutil.copyfileobj(source, output, 8 * 1024**2)
                info = expected[member.name]
                if target.stat().st_size != info['size'] or sha256(target) != info['sha256']:
                    raise RuntimeError('Extracted file checksum mismatch: ' + member.name)
                seen.add(member.name)
        if seen != set(expected):
            raise RuntimeError('Release archive is incomplete.')
        for name in sorted(seen):
            target = ROOT / name
            target.parent.mkdir(parents=True, exist_ok=True)
            (staging / name).replace(target)
    (ROOT / 'local').mkdir(exist_ok=True)
    (ROOT / 'local/installed-release.json').write_text(json.dumps(manifest, indent=2) + '\n')


def configure(sdk, name, port):
    path = ROOT / 'avd' / (name + '.avd')
    registry = avd_home() / (name + '.ini')
    instances_path = ROOT / 'local/instances.json'
    instances = json.loads(instances_path.read_text()) if instances_path.exists() else {}
    if name in instances and instances[name] != port:
        port_free(instances[name])
    if registry.exists():
        current = dict(line.split('=', 1) for line in registry.read_text().splitlines() if '=' in line)
        registered = current.get('path')
        if not registered or Path(registered).expanduser().resolve() != path:
            raise RuntimeError(f'AVD {name} belongs to another workspace. Use --name with a new name.')
    path.mkdir(parents=True, exist_ok=True)
    template = (ROOT / 'config/avd.ini').read_text()
    template += f'image.sysdir.1={ROOT / "images"}/\n'
    (path / 'config.ini').write_text(template)
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(f'avd.ini.encoding=UTF-8\npath={path}\ntarget=android-36\n')
    (ROOT / 'local').mkdir(exist_ok=True)
    (ROOT / 'local/runtime.json').write_text(json.dumps(
        {'sdk': str(sdk), 'name': name, 'port': port}, indent=2) + '\n')
    instances[name] = port
    instances_path.write_text(json.dumps(instances, indent=2) + '\n')
    print(f'Registered {name} at emulator-{port}. Existing userdata was preserved.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', help='Local release manifest.json, next to all archive parts')
    parser.add_argument('--manifest', help='HTTPS URL of a published release manifest.json')
    parser.add_argument('--sdk', type=Path)
    parser.add_argument('--name', default=DEFAULT_NAME)
    parser.add_argument('--port', type=int, default=DEFAULT_PORT)
    args = parser.parse_args()
    host_check()
    if not re.fullmatch(r'HyperOS_[A-Za-z0-9_]+', args.name):
        raise RuntimeError('Use an isolated AVD name beginning with HyperOS_.')
    if args.port % 2 or not 5556 <= args.port <= 5682:
        raise RuntimeError('Use a free, even emulator console port between 5556 and 5682.')
    port_free(args.port)
    # Check registry ownership before downloading or replacing any firmware.
    registry = avd_home() / (args.name + '.ini')
    if registry.exists():
        current = dict(line.split('=', 1) for line in registry.read_text().splitlines() if '=' in line)
        if Path(current.get('path', '/nonexistent')).expanduser().resolve() != ROOT / 'avd' / (args.name + '.avd'):
            raise RuntimeError(f'AVD {args.name} belongs to another workspace. Use --name with a new name.')
    # Protect every AVD in this checkout before replacing shared firmware files.
    if args.bundle or args.manifest:
        firmware_idle(args.port)
        install_bundle(args.bundle or args.manifest)
    sdk = sdk_path(args.sdk)
    required = ('system.img', 'ramdisk.img', 'kernel-ranchu', 'source.properties', 'userdata.img')
    missing = [name for name in required if not (ROOT / 'images' / name).is_file()]
    if missing:
        raise RuntimeError('Install the release assets first: ./Setup.command --bundle /path/to/manifest.json. '
                           'Missing: ' + ', '.join(missing))
    fetch_ksu(['ksud-aarch64-linux-android', 'KernelSU_v3.3.0_32601-release.apk'])
    configure(sdk, args.name, args.port)
    print('Ready. Double-click Start-HyperOS.command to boot and complete first-start setup.')


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, OSError, ValueError, KeyError) as error:
        raise SystemExit(str(error))
