#!/usr/bin/env python3
"""Install a verified release and register an isolated Android Studio AVD."""
import argparse
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import tarfile
import tempfile
import urllib.parse
import urllib.request
from https_transport import secure_urlopen, secure_urlretrieve
import common

from common import (ROOT, REPO_ROOT, DEFAULT_NAME, DEFAULT_PORT, OS4_NAME, OS4_PORT, OS4_SOURCE, avd_home, fetch_ksu, firmware_idle,
                    host_check, port_free, sdk_path, sha256)

PAD_SOURCE = 'official-yingtian-ota'
PAD_VARIANT = 'os4-pad'
PAD_HYPEROS = 'OS4.0.15.0.XBMCNXM'
PAD_FAMILY = 'os4-yingtian-api37-ranchu-4k'


def release_source(manifest):
    """Map the declared firmware family without adopting another profile."""
    variant = manifest.get('variant', 'os3')
    if variant not in ('os3', 'os4-official', PAD_VARIANT):
        raise RuntimeError('Unsupported release firmware variant.')
    return {'os4-official': OS4_SOURCE, PAD_VARIANT: PAD_SOURCE}.get(variant, 'os3')


def validate_memory(properties=None):
    """Apply the same sane RAM range to every firmware profile."""
    if properties is None:
        template = ROOT / 'config/avd.ini'
        properties = dict(line.split('=', 1) for line in template.read_text().splitlines()
                          if '=' in line) if template.is_file() else {}
    saved = ROOT / 'local/runtime.json'
    hardware = json.loads(saved.read_text()).get('hardware', {}) if saved.is_file() else {}
    for values in (properties, hardware):
        if 'hw.ramSize' in values and not 1024 <= int(values['hw.ramSize']) <= 65536:
            raise RuntimeError('Use RAM 1024-65536 MiB.')


def read_manifest(value):
    if value.startswith('https://'):
        with secure_urlopen(value, timeout=30) as response:
            data = response.read(2 * 1024**2 + 1)
        if len(data) > 2 * 1024**2:
            raise RuntimeError('Oversized remote release manifest.')
        manifest = json.loads(data)
        base = value.rsplit('/', 1)[0] + '/'
    else:
        path = Path(value).expanduser().resolve()
        manifest = json.loads(path.read_text())
        base = path.parent
    if manifest.get('project') != 'HyperOS-AVD' or manifest.get('format') not in (1, 2, 3):
        raise RuntimeError('Unsupported HyperOS-AVD release manifest.')
    if not re.fullmatch(r'(?:pad-)?v[0-9A-Za-z_.-]+', manifest.get('version', '')):
        raise RuntimeError('Invalid release version.')
    if manifest['version'].startswith('pad-') and manifest.get('variant') != PAD_VARIANT:
        raise RuntimeError('Invalid official OS4 Pad profile: version namespace requires the Pad firmware variant.')
    if manifest.get('platform') != 'macos-arm64':
        raise RuntimeError('This installer supports macos-arm64 releases only.')
    if manifest.get('format') in (2, 3):
        build = manifest.get('build', {})
        source = release_source(manifest)
        if (source not in (OS4_SOURCE, PAD_SOURCE)
                or build.get('source') != source
                or build.get('android_api') != 37
                or manifest.get('android_api') != 37
                or build.get('hyperos') != manifest.get('hyperos')
                or build.get('adb_authentication') is not True
                or 'config/avd.ini' not in manifest.get('files', {})):
            raise RuntimeError('Invalid official OS4 release profile.')
        if source == PAD_SOURCE:
            compatibility = manifest.get('compatibility', {})
            minimum = compatibility.get('minimum_installer', '')
            if (not re.fullmatch(r'pad-v\d+\.\d+\.\d+-a17-hyperos4-yingtian-r\d+', manifest['version'])
                    or type(manifest['format']) is not int or manifest['format'] != 3
                    or manifest.get('source') != PAD_SOURCE
                    or manifest.get('source_device') != 'yingtian'
                    or build.get('device') != 'yingtian'
                    or manifest.get('hyperos') != PAD_HYPEROS
                    or compatibility.get('userdata_family') != PAD_FAMILY
                    or compatibility.get('runtime_in_bundle') is not True
                    or not isinstance(minimum, str)
                    or not re.fullmatch(r'\d+\.\d+\.\d+', minimum)
                    or tuple(map(int, minimum.split('.'))) < (1, 1, 0)):
                raise RuntimeError('Invalid official OS4 Pad release profile.')
        elif manifest.get('hyperos') == '4.0.18.0.XFRCNXM':
            from phone_profile import profile_from_build
            from manage import MODULE_UPGRADE_PREFLIGHT, FORWARD_UPGRADE_POLICY
            selected = profile_from_build(build)
            compatibility = manifest.get('compatibility', {})
            minimum = compatibility.get('minimum_installer', '')
            r4 = build.get('boot_service_fix') is not None
            policy = compatibility.get('upgrade_policy')
            forward = policy == FORWARD_UPGRADE_POLICY
            if policy is not None and not forward:
                raise RuntimeError('Unknown official OS4 forward upgrade policy.')
            if r4:
                from apply_boot_service_fix import receipt
                if build['boot_service_fix'] != receipt() or not forward:
                    raise RuntimeError('Invalid official OS4 r4 boot service profile.')
            elif forward:
                raise RuntimeError('Missing official OS4 r4 boot service profile.')
            if (type(manifest['format']) is not int or manifest['format'] != 3
                    or manifest.get('source') != OS4_SOURCE
                    or manifest.get('source_device') != 'hongkong'
                    or build.get('archive_sha256') != selected['archive_sha256']
                    or compatibility.get('userdata_family') != 'os4-hongkong-api37-ranchu-4k'
                    or compatibility.get('runtime_in_bundle') is not True
                    or compatibility.get('module_upgrade_preflight') != MODULE_UPGRADE_PREFLIGHT
                    or (not forward and compatibility.get('upgrade_from') != ['v0.2.1-a17-hyperos4-hongkong-r2'])
                    or not isinstance(minimum, str) or not re.fullmatch(r'\d+\.\d+\.\d+', minimum)
                    or tuple(map(int, minimum.split('.'))) < ((1, 2, 1) if r4 else (1, 2, 0))):
                raise RuntimeError('Invalid official OS4 ' + ('r4' if r4 else 'r3') + ' upgrade profile.')
    elif (manifest.get('android_api', 36) != 36 or manifest.get('variant', 'os3') != 'os3'
          or manifest.get('source') in (OS4_SOURCE, PAD_SOURCE)
          or manifest.get('build', {}).get('source') in (OS4_SOURCE, PAD_SOURCE)):
        raise RuntimeError('OS4 requires a format 2 release; refusing an OS3/OS4 mix.')
    return manifest, base


def workspace_profile(root):
    """Read firmware identity before reusing an existing instance's settings."""
    root = Path(root)
    build = root / 'local/build.json'
    if build.is_file():
        metadata = json.loads(build.read_text())
        source = metadata.get('source')
        if source in (OS4_SOURCE, PAD_SOURCE):
            return source
        if metadata.get('android_api') == 36:
            return 'os3'
        if source or metadata.get('android_api'):
            return 'unsupported'
    installed = root / 'local/installed-release.json'
    if installed.is_file():
        metadata = json.loads(installed.read_text())
        if metadata.get('project') == 'HyperOS-AVD':
            if metadata.get('variant') == 'os4-official':
                return OS4_SOURCE
            if metadata.get('variant') == PAD_VARIANT:
                return PAD_SOURCE
            if metadata.get('variant', 'os3') == 'os3' and metadata.get('android_api', 36) == 36:
                return 'os3'
            return 'unsupported'
    template = root / 'config/avd.ini'
    if template.is_file():
        values = dict(line.split('=', 1) for line in template.read_text().splitlines() if '=' in line)
        if values.get('target') in ('android-36', 'android-36.0') and values.get('hw.cpu.arch') == 'arm64':
            return 'os3'
    return None


def select_build_instance(source, default_name, default_port):
    """Read the existing same-profile instance before a source rebuild mutates it."""
    if source not in ('os3', OS4_SOURCE, 'official-yingtian-ota'):
        raise RuntimeError('Unsupported firmware build profile.')
    current = workspace_profile(ROOT)
    if current is not None and current != source:
        raise RuntimeError('Build does not match this workspace; firmware was preserved.')
    saved = ROOT / 'local/runtime.json'
    previous = {}
    if saved.is_file():
        if current != source:
            raise RuntimeError('Existing runtime has no matching firmware profile; instance was preserved.')
        previous = json.loads(saved.read_text())
    name, port = previous.get('name', default_name), previous.get('port', default_port)
    if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', name):
        raise RuntimeError('Use an ASCII AVD name up to 64 characters.')
    if isinstance(port, bool) or not isinstance(port, int) or port % 2 or not 5556 <= port <= 5682:
        raise RuntimeError('Use a free, even emulator console port between 5556 and 5682.')
    return name, port


def select_release(manifest, name=None, port=None):
    """Select the firmware root before any port, registry or download action."""
    global ROOT
    root = ROOT
    requested = release_source(manifest) \
        if manifest is not None else workspace_profile(root) or 'os3'
    if requested in (OS4_SOURCE, PAD_SOURCE):
        if manifest is not None:
            folder = 'work/os4-pad' if requested == PAD_SOURCE else 'work/os4-official'
            root = Path(os.environ.get('HYPEROS_AVD_WORKSPACE', REPO_ROOT / folder)).expanduser().resolve()
        if root == REPO_ROOT:
            raise RuntimeError('OS4 must use a separate workspace; OS3 firmware was preserved.')
    current = workspace_profile(root)
    if (current is not None and current != requested) or requested == 'unsupported':
        raise RuntimeError('Release does not match this workspace; firmware was preserved.')
    saved = root / 'local/runtime.json'
    previous = {}
    if saved.is_file():
        if current != requested:
            raise RuntimeError('Existing runtime has no matching firmware profile; instance was preserved.')
        previous = json.loads(saved.read_text())
    if requested == OS4_SOURCE:
        defaults = OS4_NAME, OS4_PORT
    elif requested == 'official-yingtian-ota':
        from os4_pad import NAME, PORT
        defaults = NAME, PORT
    else:
        defaults = DEFAULT_NAME, DEFAULT_PORT
    name = previous.get('name', defaults[0]) if name is None else name
    port = previous.get('port', defaults[1]) if port is None else port
    ROOT = root
    common.ROOT = root
    return name, port


def start_instruction(root):
    """Select a launcher by its firmware workspace, independent of AVD names."""
    root = Path(root).resolve()
    profile = workspace_profile(root)
    if (root / 'Start.command').is_file():
        start = root / 'Start.command'
    elif profile == OS4_SOURCE and root == REPO_ROOT / 'work/os4-official':
        start = REPO_ROOT / 'Start-HyperOS4-Official.command'
    elif profile == 'official-yingtian-ota' and root == REPO_ROOT / 'work/os4-pad':
        start = REPO_ROOT / 'Start-HyperOS4-Pad.command'
    elif root == REPO_ROOT and profile in (None, 'os3'):
        start = REPO_ROOT / 'Start-HyperOS.command'
    else:
        command = ('HYPEROS_AVD_WORKSPACE=' + shlex.quote(str(root)) +
                   ' python3 ' + shlex.quote(str(REPO_ROOT / 'scripts/launch.py')))
        return 'Run ' + command + ' to boot and complete first-start setup.'
    return 'Double-click ' + str(start) + ' to boot and complete first-start setup.'


def install_bundle(value):
    manifest, base = read_manifest(value)
    previous = ROOT / 'local/build.json'
    if (manifest.get('build', {}).get('source') == OS4_SOURCE
            and any(path.is_file() for path in (ROOT / 'avd').glob('*.avd/userdata-qemu.img*'))):
        from phone_profile import profile_from_build
        try:
            saved = json.loads(previous.read_text())
            selected = profile_from_build(saved) if isinstance(saved, dict) else {}
            verified = (isinstance(saved, dict) and saved.get('source') == OS4_SOURCE
                        and saved.get('hyperos') == manifest['hyperos']
                        and saved.get('archive_sha256') == selected['archive_sha256']
                        and saved.get('archive_sha256') == manifest['build']['archive_sha256'])
        except (OSError, ValueError, KeyError, RuntimeError):
            verified = False
        if not verified:
            raise RuntimeError('Use Installer 1.2.0 Upgrade or Recover for retained OS4 userdata; '
                               'direct Setup requires verified matching firmware and OTA identity.')
    validate_memory()
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
                secure_urlretrieve(urllib.parse.urljoin(base, name), partial)
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
                if member.name in expected and member.size != expected[member.name]['size']:
                    raise RuntimeError('Archive member size differs from manifest.')
                if (not member.isfile() or relative.is_absolute() or '..' in relative.parts
                        or member.name not in expected
                        or not (relative.parts[0] in ('images', 'tools')
                                or manifest['format'] in (2, 3) and member.name == 'config/avd.ini'
                                or manifest['format'] == 3 and relative.parts[0] == 'runtime')):
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
        if manifest['format'] in (2, 3):
            template = (staging / 'config/avd.ini').read_text()
            properties = dict(line.split('=', 1) for line in template.splitlines() if '=' in line)
            if (properties.get('target') != 'android-37.0'
                    or properties.get('hw.cpu.arch') != 'arm64'
                    or any(key in properties for key in ('image.sysdir.1', 'path', 'path.rel'))):
                raise RuntimeError('Invalid or nonportable OS4 AVD template.')
            validate_memory(properties)
        for name in sorted(seen):
            target = ROOT / name
            target.parent.mkdir(parents=True, exist_ok=True)
            (staging / name).replace(target)
    (ROOT / 'local').mkdir(exist_ok=True)
    if manifest['format'] in (2, 3):
        (ROOT / 'local/build.json').write_text(json.dumps(manifest['build'], indent=2) + '\n')
    (ROOT / 'local/installed-release.json').write_text(json.dumps(manifest, indent=2) + '\n')


def configure(sdk, name, port):
    path = ROOT.resolve() / 'avd' / (name + '.avd')
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
    config_file = ROOT / 'config/avd.ini'
    template = (config_file if config_file.exists() else REPO_ROOT / 'config/avd.ini').read_text()
    properties = dict(line.split('=', 1) for line in template.splitlines() if '=' in line)
    target = properties['target']
    # Retain per-instance hardware choices across launcher reconfiguration.
    saved = ROOT / 'local/runtime.json'
    previous = json.loads(saved.read_text()) if saved.exists() else {}
    hardware = previous.get('hardware', {})
    for key, value in hardware.items():
        if key not in ('hw.ramSize', 'hw.cpu.ncore', 'disk.dataPartition.size'):
            raise RuntimeError('Unexpected per-instance hardware key: ' + key)
        properties[key] = str(value)
    validate_memory(properties)
    template = ''.join(key + '=' + value + '\n' for key, value in properties.items())
    template += f'image.sysdir.1={ROOT / "images"}/\n'
    template += 'AvdId=' + name + '\navd.ini.displayname=' + name + '\n'

    path.mkdir(parents=True, exist_ok=True)
    (path / 'config.ini').write_text(template)
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(f'avd.ini.encoding=UTF-8\npath={path}\ntarget={target}\n')
    (ROOT / 'local').mkdir(exist_ok=True)
    (ROOT / 'local/runtime.json').write_text(json.dumps(
        {**previous, 'sdk': str(sdk), 'name': name, 'port': port, 'hardware': hardware}, indent=2) + '\n')
    instances[name] = port
    instances_path.write_text(json.dumps(instances, indent=2) + '\n')
    print(f'Registered {name} at emulator-{port}. Existing userdata was preserved.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', help='Local release manifest.json, next to all archive parts')
    parser.add_argument('--manifest', help='HTTPS URL of a published release manifest.json')
    parser.add_argument('--sdk', type=Path)
    parser.add_argument('--name')
    parser.add_argument('--port', type=int)
    args = parser.parse_args()
    host_check()
    if args.bundle and args.manifest:
        raise RuntimeError('Choose either --bundle or --manifest.')
    release = read_manifest(args.bundle or args.manifest)[0] if args.bundle or args.manifest else None
    args.name, args.port = select_release(release, args.name, args.port)
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', args.name):
        raise RuntimeError('Use an ASCII AVD name up to 64 characters.')
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
    sdk = sdk_path(args.sdk)
    from userdata_resize import check_dependencies
    check_dependencies(sdk)
    if args.bundle or args.manifest:
        firmware_idle(args.port)
        install_bundle(args.bundle or args.manifest)
    required = ('system.img', 'ramdisk.img', 'kernel-ranchu', 'source.properties', 'userdata.img')
    missing = [name for name in required if not (ROOT / 'images' / name).is_file()]
    if missing:
        raise RuntimeError('Install the release assets first: ./Setup.command --bundle /path/to/manifest.json. '
                           'Missing: ' + ', '.join(missing))
    fetch_ksu(['ksud-aarch64-linux-android', 'KernelSU_v3.3.0_32601-release.apk'])
    configure(sdk, args.name, args.port)
    print('Ready. ' + start_instruction(ROOT))


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, OSError, ValueError, KeyError) as error:
        raise SystemExit(str(error))
