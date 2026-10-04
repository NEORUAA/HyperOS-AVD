#!/usr/bin/env python3
"""Bilingual release manager with isolated instances and recoverable upgrades."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

import common
import setup
from common import REPO_ROOT, avd_home, host_check, port_free, sdk_path, sha256

VERSION = '1.0.0'
REPOSITORY = 'NEORUAA/HyperOS-AVD'
HOME = Path(os.environ.get('HYPEROS_AVD_HOME', Path.home() / 'HyperOS-AVD')).expanduser().resolve()
LANG = 'zh'


def tr(zh, en):
    return zh if LANG == 'zh' else en


def say(zh, en):
    print(tr(zh, en), flush=True)


def ask(zh, en, default=''):
    value = input(tr(zh, en) + (f' [{default}]' if default != '' else '') + ': ').strip()
    return value or str(default)


class Back(Exception):
    """Return to the dashboard before any mutating operation."""


def display_width(value):
    return sum(0 if unicodedata.combining(c) else
               2 if unicodedata.east_asian_width(c) in ('W', 'F') else 1 for c in value)


def wrap_line(value, width):
    line = ''
    for char in str(value):
        if char == '\n' or display_width(line + char) > width:
            yield line
            line = ''
        if char != '\n':
            line += char
    yield line


def panel(title, rows, width=None):
    """Draw ASCII borders with correct Chinese terminal-cell alignment."""
    width = width or max(36, min(88, shutil.get_terminal_size((88, 24)).columns - 2))
    inner = width - 4
    border = '+' + '-' * (width - 2) + '+'
    print(border)
    for section in ([title], rows):
        for row in section:
            for line in wrap_line(row, inner):
                print('| ' + line + ' ' * (inner - display_width(line)) + ' |')
        print(border)


def choose(title, rows, default=1):
    panel(title, [f'[{i}] {row}' for i, row in enumerate(rows, 1)] +
          [tr('[0] 返回主菜单', '[0] Back to dashboard')])
    while True:
        value = ask('选择', 'Select', default)
        if value == '0':
            raise Back()
        if value.isdigit() and 1 <= int(value) <= len(rows):
            return int(value) - 1
        say('编号超出范围，请重新选择。', 'Out of range; choose a listed number.')


def progress(name, received, total):
    fraction = received / max(total, 1)
    filled = min(24, int(fraction * 24))
    bar = '[' + '#' * filled + '-' * (24 - filled) + ']'
    print(f'\r{bar} {fraction:6.1%}  {received / 1024**2:.1f}/{total / 1024**2:.1f} MiB',
          end='', flush=True)


def release_variant(release):
    tag = release.get('tag_name', '').lower()
    if tag in ('v0.1.0', 'v0.1.0-a16-hyperos3-fuxi-r1') or re.fullmatch(r'v\d+\.\d+\.\d+-a\d+-hyperos3-.+', tag):
        return 'os3'
    if re.fullmatch(r'v\d+\.\d+\.\d+-a\d+-hyperos4-.+', tag):
        return 'os4-official'
    return None


def version_key(tag):
    match = re.match(r'v?(\d+)\.(\d+)\.(\d+)', tag)
    return tuple(map(int, match.groups())) if match else (0, 0, 0)


def request(url):
    return urllib.request.Request(url, headers={'User-Agent': 'HyperOS-AVD/' + VERSION,
        'Accept': 'application/vnd.github+json'})


def remote_json(url):
    with urllib.request.urlopen(request(url), timeout=30) as response:
        data = response.read(2 * 1024**2 + 1)
    if len(data) > 2 * 1024**2:
        raise RuntimeError('Oversized remote metadata.')
    return json.loads(data)


def release_rows():
    for page in range(1, 11):
        rows = remote_json(f'https://api.github.com/repos/{REPOSITORY}/releases?per_page=100&page={page}')
        yield from rows
        if len(rows) < 100:
            break


def catalog(variant='all', stable_only=False):
    """List prereleases too; GitHub's latest endpoint omits this project's builds."""
    found = []
    for release in release_rows():
        kind = release_variant(release)
        if (release.get('draft') or stable_only and release.get('prerelease')
                or not kind or variant != 'all' and variant != kind):
            continue
        assets = {item['name']: item for item in release.get('assets', [])}
        if 'manifest.json' in assets:
            found.append({**release, 'asset_map': assets})
    return sorted(found, key=lambda item: (version_key(item['tag_name']), item.get('published_at', '')), reverse=True)


def installer_catalog():
    rows = release_rows()
    return sorted([r for r in rows if not r.get('draft') and not r.get('prerelease')
                   and re.fullmatch(r'installer-v\d+\.\d+\.\d+', r.get('tag_name', ''))],
                  key=lambda r: version_key(r['tag_name'].removeprefix('installer-')), reverse=True)


def download(url, target, size, checksum):
    """Resume a partial transfer; validate before promoting it to the cache."""
    if not re.fullmatch(r'[0-9a-f]{64}', checksum) or size < 0:
        raise RuntimeError('Invalid download checksum or size.')
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file() and target.stat().st_size == size and sha256(target) == checksum:
        return target
    partial = target.with_name(target.name + '.partial')
    if partial.exists() and partial.stat().st_size > size:
        partial.unlink()
    for attempt in range(3):
        offset = partial.stat().st_size if partial.exists() else 0
        if offset == size:
            if sha256(partial) != checksum:
                partial.unlink()
                if attempt == 2:
                    raise RuntimeError('Download checksum mismatch: ' + target.name)
                continue
            partial.replace(target)
            return target
        req = request(url)
        if offset:
            req.add_header('Range', f'bytes={offset}-')
        try:
            print(tr('下载：', 'Downloading: ') + target.name, flush=True)
            with urllib.request.urlopen(req, timeout=60) as response:
                status = response.getcode()
                if offset and status != 206:
                    offset = 0  # Some release CDNs ignore Range; restart safely.
                if status == 206 and not response.headers.get('Content-Range', '').startswith(f'bytes {offset}-'):
                    raise RuntimeError('Invalid partial download response.')
                last = time.monotonic()
                with partial.open('ab' if offset else 'wb') as output:
                    while True:
                        block = response.read(1024**2)
                        if not block:
                            break
                        offset += len(block)
                        if offset > size:
                            raise RuntimeError('Download exceeds manifest size.')
                        output.write(block)
                        if time.monotonic() - last > 2:
                            progress(target.name, offset, size)
                            last = time.monotonic()
            progress(target.name, offset, size)
            print(flush=True)
        except (OSError, urllib.error.URLError):
            if attempt == 2:
                raise
        # Verification occurs on the next iteration, or here after the final attempt.
    if partial.stat().st_size != size or sha256(partial) != checksum:
        raise RuntimeError('Incomplete or corrupt download: ' + target.name)
    partial.replace(target)
    return target


def fetch_release(release, cache):
    assets = release['asset_map']
    cache = Path(cache) / release['tag_name']
    cache.mkdir(parents=True, exist_ok=True)
    entry = assets['manifest.json']
    checksum = entry.get('digest') or ''
    if checksum.startswith('sha256:'):
        checksum = checksum[7:]
    else:
        sums = assets.get('SHA256SUMS')
        if not sums:
            raise RuntimeError('Release is missing manifest integrity metadata.')
        with urllib.request.urlopen(request(sums['browser_download_url']), timeout=30) as response:
            lines = response.read(65536).decode().splitlines()
        matches = [line.split()[0] for line in lines if line.split()[-1:] == ['manifest.json']]
        if len(matches) != 1:
            raise RuntimeError('Ambiguous manifest checksum.')
        checksum = matches[0]
    manifest_path = download(entry['browser_download_url'], cache / 'manifest.json', entry['size'], checksum)
    manifest, _ = setup.read_manifest(str(manifest_path))
    if manifest['version'] != release['tag_name']:
        # OS3 r1 used v0.1.0 as its bundle version; only that known legacy mapping is accepted.
        if (manifest['version'], release['tag_name']) != ('v0.1.0', 'v0.1.0-a16-hyperos3-fuxi-r1'):
            raise RuntimeError('Manifest does not match the selected release.')
    for part in manifest['parts']:
        asset = assets.get(part['name'])
        if not asset or asset['size'] != part['size']:
            raise RuntimeError('Release is missing a matching archive part.')
        download(asset['browser_download_url'], cache / part['name'], part['size'], part['sha256'])
    return manifest_path


def properties(path):
    return dict(line.split('=', 1) for line in path.read_text().splitlines() if '=' in line)


def instances():
    """Discover project instances by registry ownership; never contact a guest."""
    results = []
    for registry in sorted(avd_home().glob('*.ini')):
        try:
            avd = Path(properties(registry)['path']).expanduser().resolve()
            root = avd.parent.parent
            runtime = json.loads((root / 'local/runtime.json').read_text())
            if avd != root / 'avd' / (runtime['name'] + '.avd') or registry.stem != runtime['name']:
                continue
            installed = root / 'local/installed-release.json'
            metadata = json.loads(installed.read_text()) if installed.is_file() else {}
            build = root / 'local/build.json'
            official = build.is_file() and json.loads(build.read_text()).get('source') == common.OS4_SOURCE
            template = root / 'config/avd.ini'
            legacy_os3 = (runtime['name'] == common.DEFAULT_NAME
                          and (root / 'scripts/build_image.py').is_file() and template.is_file()
                          and properties(template).get('target') == 'android-36'
                          and properties(template).get('hw.cpu.arch') == 'arm64')
            if not official and metadata.get('project') != 'HyperOS-AVD' and not legacy_os3:
                continue
            results.append({'root': root, 'runtime': runtime, 'version': metadata.get('version', 'legacy'),
                            'variant': 'os4-official' if official else 'os3'})
        except (OSError, ValueError, KeyError):
            continue
    return results


def validate_name(name):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', name):
        raise RuntimeError('AVD names support 1-64 ASCII letters, digits, dots, underscores and hyphens.')


def choose_port():
    reserved = {entry['runtime'].get('port') for entry in instances()}
    for port in range(5580, 5683, 2):
        if port in reserved:
            continue
        try:
            port_free(port)
            return port
        except RuntimeError:
            continue
    raise RuntimeError('No free emulator port.')


def idle(root, name, port):
    """Protect the instance even if Studio chose a different console port."""
    port_free(port)
    if (root / 'local/instances.json').is_file():
        for saved in json.loads((root / 'local/instances.json').read_text()).values():
            port_free(saved)
    rows = subprocess.check_output(['ps', '-ax', '-o', 'command='], text=True)
    import shlex
    for row in rows.splitlines():
        if 'qemu-system-' not in row and '/emulator/emulator ' not in row:
            continue
        try:
            args = shlex.split(row)
        except ValueError:
            continue
        if ('-avd' in args and args[args.index('-avd') + 1] == name
                or '-sysdir' in args and Path(args[args.index('-sysdir') + 1]).resolve() == (root / 'images').resolve()):
            raise RuntimeError('Close this AVD before changing its files. No emulator was stopped.')


def owner(root, name):
    registry = avd_home() / (name + '.ini')
    if registry.exists() and Path(properties(registry).get('path', '/nonexistent')).expanduser().resolve() != root / 'avd' / (name + '.avd'):
        raise RuntimeError('AVD name belongs to another workspace.')


@contextmanager
def lock(root):
    folder = root / 'local'
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / '.manager.lock').open('a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Another manager is working on this instance.') from None
        yield


def clone(source, target):
    """Use APFS clones where possible; copy across volumes as a fallback."""
    source, target = Path(source), Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(['cp', '-c', '-p', str(source), str(target)], capture_output=True)
    if result.returncode:
        shutil.copy2(source, target)
    return target


def backup(root, name):
    folder = root / 'backups' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    folder.mkdir(parents=True)
    data = root / 'avd' / (name + '.avd')
    if data.is_dir():
        shutil.copytree(data, folder / 'avd', copy_function=clone, symlinks=True)
    hashes = {}
    for path in (folder / 'avd').rglob('*') if (folder / 'avd').exists() else ():
        if path.is_file() and not path.is_symlink() and ('userdata' in path.name or 'encryption' in path.name):
            hashes[path.relative_to(folder).as_posix()] = sha256(path)
    for source in (root / 'local', root / 'Start.command'):
        if source.is_dir():
            shutil.copytree(source, folder / 'local', ignore=shutil.ignore_patterns('.manager.lock'), copy_function=clone)
        elif source.is_file():
            clone(source, folder / source.name)
    registry = avd_home() / (name + '.ini')
    if registry.exists():
        clone(registry, folder / 'registry.ini')
    (folder / 'backup.json').write_text(json.dumps({'name': name, 'files': hashes}, indent=2) + '\n')
    print(tr('数据备份：', 'Data backup: ') + str(folder), flush=True)
    return folder


def data_size(sdk, path):
    return json.loads(subprocess.check_output([str(sdk / 'emulator/qemu-img'), 'info', '--output=json', str(path)], text=True))


def validate_userdata(sdk, avd):
    """Refuse overlays the Emulator would discard for an unexpected backing name."""
    for stem in ('userdata-qemu.img', 'encryptionkey.img'):
        overlay = avd / (stem + '.qcow2')
        if not overlay.is_file():
            continue
        info = data_size(sdk, overlay)
        if info.get('backing-filename') != stem or not (avd / stem).is_file():
            raise RuntimeError('Unexpected userdata/key backing chain; preserved without booting: ' + str(overlay))


def resize(sdk, avd, gib):
    """Grow both layers; never shrink or replace encrypted userdata."""
    wanted = gib * 1024**3
    paths = [avd / name for name in ('userdata-qemu.img', 'userdata-qemu.img.qcow2') if (avd / name).is_file()]
    info = [(path, data_size(sdk, path)) for path in paths]
    if any(item['virtual-size'] > wanted for _, item in info):
        raise RuntimeError('Storage cannot be reduced without erasing data; choose a larger size.')
    for path, item in info:
        if item['virtual-size'] < wanted:
            subprocess.run([str(sdk / 'emulator/qemu-img'), 'resize', '-f', item['format'], str(path), str(wanted)], check=True)
        if item['format'] == 'qcow2':
            subprocess.run([str(sdk / 'emulator/qemu-img'), 'check', str(path)], check=True, capture_output=True)
        if data_size(sdk, path)['virtual-size'] != wanted:
            raise RuntimeError('Userdata resize failed.')


def hardware(ram, storage, cores):
    if not 2 <= ram <= 64 or not 6 <= storage <= 1024 or not 1 <= cores <= (os.cpu_count() or 1):
        raise RuntimeError('Use RAM 2-64 GiB, storage 6-1024 GiB, and available host CPU cores.')
    return {'hw.ramSize': str(round(ram * 1024)), 'hw.cpu.ncore': str(cores), 'disk.dataPartition.size': f'{storage}G'}


def family(manifest):
    if manifest.get('format') == 3:
        return manifest.get('compatibility', {}).get('userdata_family')
    if manifest.get('format') == 2 and manifest.get('variant') == 'os4-official' and manifest.get('android_api') == 37:
        return 'os4-hongkong-api37-ranchu-4k'
    if manifest.get('format') == 1 and manifest.get('android_api', 36) == 36:
        return 'os3-fuxi-api36-ranchu'
    return None


def compatible(old, new):
    if not family(old) or family(old) != family(new):
        raise RuntimeError('Incompatible userdata family. Install a separate AVD for this image.')
    for field in ('images/encryptionkey.img',):
        before = old.get('files', {}).get(field, {}).get('sha256')
        after = new.get('files', {}).get(field, {}).get('sha256')
        if not before or before != after:
            raise RuntimeError('Encryption template changed; automatic data migration is unsafe.')
    if version_key(new['version']) < version_key(old['version']):
        raise RuntimeError('Data-preserving downgrades are not supported. Use a separate AVD.')


def json_write(path, data):
    temporary = path.with_name(path.name + '.next')
    temporary.write_text(json.dumps(data, indent=2) + '\n')
    temporary.replace(path)


@contextmanager
def scope(root):
    previous = setup.ROOT, common.ROOT
    setup.ROOT = common.ROOT = root
    try:
        yield
    finally:
        setup.ROOT, common.ROOT = previous


def point(root, target):
    for name in ('images', 'tools', 'config'):
        path = root / name
        if path.exists() and not path.is_symlink():
            legacy = root / 'versions' / ('legacy-' + str(time.time_ns()))
            legacy.mkdir(parents=True)
            path.rename(legacy / name)
        temporary = path.with_name(name + '.next')
        temporary.unlink(missing_ok=True)
        temporary.symlink_to(os.path.relpath(target / name, root), target_is_directory=True)
        temporary.replace(path)


def wrapper(root):
    file = root / 'Start.command'
    file.write_text('''#!/bin/zsh
cd "${0:A:h}" || exit 1
exec python3 - "$PWD" <<'PYTHON'
import json, os, pathlib, subprocess, sys
root = pathlib.Path(sys.argv[1])
state = json.loads((root / 'local/manager.json').read_text())
os.environ['HYPEROS_AVD_WORKSPACE'] = str(root)
raise SystemExit(subprocess.call([sys.executable, str(root / state['runtime'] / 'scripts/launch.py')], env=os.environ))
PYTHON
''')
    file.chmod(0o755)


def restore(root, folder):
    """Recover a failed switch or roll back an upgrade including encrypted data."""
    root, folder = root.resolve(), folder.resolve()
    if root / 'backups' not in folder.parents:
        raise RuntimeError('Backup must belong to this instance.')
    saved = json.loads((folder / 'transaction.json').read_text())
    name = saved['name']
    current = root / 'local/runtime.json'
    if current.exists() and json.loads(current.read_text()).get('name') != name:
        raise RuntimeError('Backup belongs to another AVD.')
    metadata = json.loads((folder / 'backup.json').read_text())
    for relative, checksum in metadata['files'].items():
        if Path(relative).is_absolute() or '..' in Path(relative).parts or sha256(folder / relative) != checksum:
            raise RuntimeError('Backup integrity check failed.')
    for key, value in saved['paths'].items():
        path = root / key
        if path.is_symlink():
            path.unlink()
        elif path.exists():
            # A directory was not moved yet when the failure occurred.
            if value == str(path) or saved['real'].get(key) and not Path(value).exists():
                continue
            raise RuntimeError('Rollback encountered an unexpected real directory.')
        if value:
            origin = Path(value)
            if saved['real'].get(key):
                if origin.exists():
                    origin.rename(path)
            else:
                path.symlink_to(os.path.relpath(origin, root), target_is_directory=True)
    data = root / 'avd' / (name + '.avd')
    if (folder / 'avd').exists():
        if data.exists():
            data.rename(root / 'backups' / ('failed-data-' + str(time.time_ns())))
        shutil.copytree(folder / 'avd', data, copy_function=clone, symlinks=True)
    elif data.exists() and not any(data.glob('*userdata*')) and not any(data.glob('*encryptionkey*')):
        # A failed fresh configuration created only generated files, not user data.
        shutil.rmtree(data)
    if (folder / 'local').is_dir():
        for path in (root / 'local').iterdir():
            if path.name != '.manager.lock':
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
        shutil.copytree(folder / 'local', root / 'local', dirs_exist_ok=True, copy_function=clone)
    registry = avd_home() / (name + '.ini')
    if (folder / 'registry.ini').exists():
        clone(folder / 'registry.ini', registry)
    elif registry.exists():
        registry.unlink()
    if (folder / 'Start.command').exists():
        clone(folder / 'Start.command', root / 'Start.command')
    else:
        (root / 'Start.command').unlink(missing_ok=True)
    (root / 'local/upgrade-pending.json').unlink(missing_ok=True)
    say('已恢复升级前的镜像、配置和用户数据。', 'Previous firmware, configuration and userdata restored.')


def switch(root, target, manifest, name, port, sdk, options, folder, camera=False):
    """Persist recovery instructions before the first active-path change."""
    paths, real = {}, {}
    for key in ('images', 'tools', 'config'):
        path = root / key
        real[key] = path.exists() and not path.is_symlink()
        if real[key]:
            paths[key] = str(root / 'versions' / ('legacy-' + folder.name) / key)
        else:
            paths[key] = str(path.resolve()) if path.exists() else None
    transaction = {'name': name, 'port': port, 'paths': paths, 'real': real, 'backup': str(folder)}
    json_write(folder / 'transaction.json', transaction)
    json_write(root / 'local/upgrade-pending.json', transaction)
    try:
        for key in paths:
            path = root / key
            if real[key]:
                destination = Path(paths[key])
                destination.parent.mkdir(parents=True, exist_ok=True)
                path.rename(destination)
        point(root, target)
        json_write(root / 'local/installed-release.json', manifest)
        if 'build' in manifest:
            json_write(root / 'local/build.json', manifest['build'])
        json_write(root / 'local/runtime.json', {'sdk': str(sdk), 'name': name, 'port': port, 'hardware': options, 'camera_bridge': camera})
        with scope(root):
            setup.configure(sdk, name, port)
        resize(sdk, root / 'avd' / (name + '.avd'), int(options['disk.dataPartition.size'][:-1]))
        runtime = target / 'runtime' if (target / 'runtime/scripts/launch.py').is_file() else REPO_ROOT
        json_write(root / 'local/manager.json', {'schema': 1, 'version': manifest['version'],
            'runtime': os.path.relpath(runtime, root), 'backup': str(folder)})
        wrapper(root)
        (root / 'local/upgrade-pending.json').unlink()
    except BaseException:
        restore(root, folder)
        raise


def install(root, manifest_path, name, port, sdk, options, camera=False):
    root = Path(root).expanduser().resolve()
    validate_name(name)
    manifest, _ = setup.read_manifest(str(manifest_path))
    minimum = manifest.get('compatibility', {}).get('minimum_installer', '0.2.1')
    if version_key(minimum) > version_key(VERSION):
        raise RuntimeError('This release needs a newer installer; download the latest stable Installer Release.')
    owner(root, name)
    if (root / 'local/runtime.json').exists():
        saved_runtime = json.loads((root / 'local/runtime.json').read_text())
        if saved_runtime.get('name') != name or saved_runtime.get('port') != port:
            raise RuntimeError('Upgrade must retain the instance name and console port.')
    idle(root, name, port)
    with lock(root):
        if (root / 'local/upgrade-pending.json').exists():
            raise RuntimeError('An interrupted upgrade needs recovery first: use the Recover menu.')
        data = root / 'avd' / (name + '.avd')
        old_path = root / 'local/installed-release.json'
        if data.exists():
            if not old_path.exists():
                raise RuntimeError('Existing userdata has no release manifest; migration refused.')
            validate_userdata(sdk, data)
            old = json.loads(old_path.read_text())
            compatible(old, manifest)
            if (root / 'local/runtime.json').exists() and json.loads((root / 'local/runtime.json').read_text())['name'] != name:
                raise RuntimeError('This workspace belongs to a different instance.')
            wanted = int(options['disk.dataPartition.size'][:-1]) * 1024**3
            if any(data_size(sdk, p)['virtual-size'] > wanted for p in data.glob('userdata-qemu.img*') if p.name in ('userdata-qemu.img', 'userdata-qemu.img.qcow2')):
                raise RuntimeError('Storage cannot shrink during an upgrade.')
        root.mkdir(parents=True, exist_ok=True)
        total = sum(item['size'] for item in manifest['files'].values())
        if shutil.disk_usage(root).free < total + sum(item['size'] for item in manifest['parts']) + 2 * 1024**3:
            raise RuntimeError('Insufficient free storage for staging and archive validation.')
        target = root / 'versions' / manifest['version']
        if not (target / 'local/installed-release.json').is_file():
            if target.exists():
                raise RuntimeError('An incomplete version directory exists; remove it after inspection: ' + str(target))
            target.mkdir(parents=True)
            with scope(target):
                setup.install_bundle(str(manifest_path))
        else:
            for path, info in manifest['files'].items():
                if sha256(target / path) != info['sha256']:
                    raise RuntimeError('Cached firmware differs from the release manifest.')
        idle(root, name, port)
        folder = backup(root, name)
        switch(root, target, manifest, name, port, sdk, options, folder, camera)
    print(tr('安装完成：', 'Installed: ') + str(root / 'Start.command'), flush=True)
    return folder


def start(instance, headless=False, skip_oobe=False):
    root = instance['root']
    state = root / 'local/manager.json'
    runtime = root / json.loads(state.read_text())['runtime'] if state.is_file() else REPO_ROOT
    env = dict(os.environ, HYPEROS_AVD_WORKSPACE=str(root))
    command = [sys.executable, str(runtime / 'scripts/launch.py')]
    if headless:
        command.append('--headless')
    if skip_oobe:
        command.append('--skip-oobe')
    subprocess.run(command, env=env, check=True)


def pick_instance():
    entries = instances()
    if not entries:
        raise RuntimeError(tr('未找到本项目 AVD，请先安装。', 'No project AVD found. Install one first.'))
    labels = [f"[{'OS4' if item['variant'] == 'os4-official' else 'OS3'}] "
              f"{item['runtime']['name']} | {item['version']}\n    {item['root']}" for item in entries]
    return entries[choose(tr('选择已安装实例', 'Select an installed instance'), labels)]


def pick_release(variant='all'):
    say('正在查询 OS3 / OS4 镜像发布...', 'Checking OS3 / OS4 image releases...')
    releases = catalog(variant)
    if not releases:
        raise RuntimeError('No published compatible release found.')
    labels = [f"[{'OS4' if release_variant(r) == 'os4-official' else 'OS3'}] {r['tag_name']}"
              + (' | Pre-release' if r.get('prerelease') else ' | Stable') for r in releases]
    return releases[choose(tr('镜像仓库 / 新版本优先', 'Image library / newest first'), labels)]


def options_for(instance=None, variant='os4-official'):
    values = {}
    if instance:
        values = properties(instance['root'] / 'avd' / (instance['runtime']['name'] + '.avd') / 'config.ini')
    default_ram = int(values.get('hw.ramSize', 6144 if variant == 'os4-official' else 2560)) / 1024
    disk = values.get('disk.dataPartition.size', '32G')
    match = re.fullmatch(r'(\d+)(G|GB)', disk)
    storage = int(match[1]) if match else 32
    panel(tr('资源配置', 'Hardware settings'), [
        tr('OS4 建议 6 GiB / 32 GiB / 4 核；OS3 默认 2.5 GiB / 2 核。',
           'OS4: 6 GiB / 32 GiB / 4 cores. OS3: 2.5 GiB / 2 cores.'),
        tr('OS4 完整负一屏模糊建议 8 GiB；已有存储只支持扩容。',
           'OS4 full App Vault blur: 8 GiB. Existing storage only grows.')])
    return hardware(float(ask('RAM (GiB)', 'RAM (GiB)', default_ram)),
        int(ask('存储 (GiB)', 'Storage (GiB)', storage)),
        int(ask('CPU 核心', 'CPU cores', values.get('hw.cpu.ncore', 4 if variant == 'os4-official' else 2))))


def dashboard(entries):
    if sys.stdout.isatty():
        print('\033[2J\033[H', end='')
    panel('H Y P E R O S - A V D   /   INSTALLER ' + VERSION, [
        'Apple Silicon / ARM64 / Android Studio',
        tr('镜像：OS4 官方 OTA  |  OS3 GSI', 'Images: OS4 official OTA  |  OS3 GSI'),
        tr('安装器：正式 Release  |  镜像：包含 Pre-release',
           'Installer: stable releases  |  Images: prereleases included')])
    panel(tr('主菜单', 'Dashboard'), [
        tr('[1] 安装新 AVD             [2] 保数据升级', '[1] Install a new AVD      [2] Upgrade / keep data'),
        tr('[3] 启动已有实例           [4] RAM / 存储 / CPU', '[3] Start an instance      [4] RAM / storage / CPU'),
        tr('[5] 镜像浏览 / 检查更新    [6] 恢复 / 回滚', '[5] Browse images / updates [6] Recover / rollback'),
        tr('[7] 安装器检查更新         [0] 退出', '[7] Installer updates      [0] Exit')])
    rows = [f"[{'OS4' if e['variant'] == 'os4-official' else 'OS3'}] {e['runtime']['name']} | "
            + (tr('源码旧实例', 'Source workspace') if e['version'] == 'legacy' else e['version'])
            for e in entries]
    panel(tr(f'已安装实例 ({len(entries)})', f'Installed instances ({len(entries)})'),
          rows or [tr('尚未安装，选择 [1] 浏览 OS3 / OS4。', 'No instances yet. Choose [1] for OS3 / OS4.')])


def image_updates(entries):
    rows = catalog('all')
    details = []
    for kind, label in (('os4-official', 'OS4'), ('os3', 'OS3')):
        matches = [r for r in rows if release_variant(r) == kind]
        installed = [e['runtime']['name'] + ': ' + e['version'] for e in entries if e['variant'] == kind]
        details.append(label + ' | ' + tr('最新：', 'Latest: ') +
                       (matches[0]['tag_name'] if matches else tr('未发布', 'Unavailable')))
        details.extend('  ' + v for v in installed)
        if matches:
            details.append('  ' + matches[0].get('html_url', ''))
    panel(tr('OS3 / OS4 镜像更新', 'OS3 / OS4 image updates'), details)


def installer_updates():
    rows = installer_catalog()
    details = [tr('当前：', 'Current: ') + 'installer-v' + VERSION]
    if rows:
        latest = rows[0]
        newer = version_key(latest['tag_name'].removeprefix('installer-')) > version_key(VERSION)
        details += [tr('最新：', 'Latest: ') + latest['tag_name'],
                    tr('有新安装器可下载。', 'A newer installer is available.') if newer else
                    tr('当前安装器已是最新。', 'The installer is up to date.'), latest.get('html_url', '')]
    else:
        details.append(tr('尚无已发布的正式安装器 Release。', 'No stable installer release is published yet.'))
    panel(tr('安装器更新 / 独立正式 Release', 'Installer updates / separate stable release'), details)


def tui(language=None):
    global LANG
    if language is None:
        panel('HyperOS-AVD / Installer ' + VERSION, ['[1] 中文', '[2] English'])
        LANG = 'en' if ask('语言 / Language: 1 中文, 2 English', 'Language', 1) == '2' else 'zh'
    else:
        LANG = language
    while True:
        entries = instances()
        dashboard(entries)
        try:
            action = ask('选择操作', 'Choose action', 0)
            if action == '0':
                return
            if action in ('1', '2'):
                item = pick_instance() if action == '2' else None
                if item and not (item['root'] / 'local/installed-release.json').is_file():
                    raise RuntimeError(tr('源码旧实例没有发布清单；可启动或调整硬件，不能自动迁移固件。',
                        'This source workspace has no release manifest; start/hardware work, automatic firmware migration is refused.'))
                release = pick_release(item['variant'] if item else 'all')
                variant = release_variant(release)
                name = item['runtime']['name'] if item else ask('AVD 名称', 'AVD name', 'HyperOS_4' if variant == 'os4-official' else 'HyperOS_3')
                root = item['root'] if item else Path(ask('安装目录', 'Installation folder', str(HOME / 'instances' / name))).expanduser().resolve()
                port = item['runtime']['port'] if item else choose_port()
                settings = options_for(item, variant)
                validate_name(name)
                panel(tr('安装计划 / 下载后备份并切换', 'Install plan / verify, back up, then switch'), [
                    'AVD: ' + name, 'Image: ' + release['tag_name'],
                    f"RAM: {int(settings['hw.ramSize']) / 1024:g} GiB | Storage: {settings['disk.dataPartition.size']} | CPU: {settings['hw.cpu.ncore']}",
                    'Port: ' + str(port), str(root)])
                idle(root, name, port)
                path = fetch_release(release, HOME / 'downloads')
                install(root, path, name, port, sdk_path(), settings, camera=
                    variant == 'os4-official' and ask('启用实验性小米相机？1 是，0 否（后摄录像仍掉帧）',
                    'Enable experimental Xiaomi camera? 1 yes, 0 no (rear video drops frames)', 0) == '1')
                if ask('现在启动？1 是，0 否', 'Start now? 1 yes, 0 no', 1) == '1':
                    start({'root': root})
            elif action == '3':
                start(pick_instance())
            elif action == '4':
                item = pick_instance()
                root, run = item['root'], item['runtime']
                settings = options_for(item, item['variant'])
                idle(root, run['name'], run['port'])
                with lock(root):
                    folder = backup(root, run['name'])
                    paths = {key: str((root / key).resolve()) for key in ('images', 'tools', 'config')}
                    json_write(folder / 'transaction.json', {'name': run['name'], 'port': run['port'], 'paths': paths, 'real': dict.fromkeys(paths, False)})
                    try:
                        resize(Path(run['sdk']), root / 'avd' / (run['name'] + '.avd'), int(settings['disk.dataPartition.size'][:-1]))
                        json_write(root / 'local/runtime.json', {**run, 'hardware': settings})
                        with scope(root):
                            setup.configure(Path(run['sdk']), run['name'], run['port'])
                    except BaseException:
                        restore(root, folder)
                        raise
                say('硬件配置已保存，下次冷启动生效。', 'Hardware saved; changes apply on the next cold boot.')
            elif action == '5':
                image_updates(entries)
            elif action == '6':
                item = pick_instance()
                root, run = item['root'], item['runtime']
                pending = root / 'local/upgrade-pending.json'
                folder = Path(json.loads(pending.read_text())['backup']) if pending.exists() else Path(ask('备份目录', 'Backup directory', json.loads((root / 'local/manager.json').read_text()).get('backup', '')))
                idle(root, run['name'], run['port'])
                with lock(root):
                    restore(root, folder)
            elif action == '7':
                installer_updates()
            else:
                say('请输入菜单编号。', 'Enter a menu number.')
        except Back:
            continue
        except (RuntimeError, OSError, ValueError, KeyError, IndexError, subprocess.SubprocessError) as error:
            panel(tr('操作失败 / 原数据保留', 'Operation failed / userdata retained'), [str(error)])
        if sys.stdin.isatty():
            input(tr('按 Enter 返回主菜单...', 'Press Enter to return to the dashboard...'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--language', choices=('zh', 'en'))
    commands = parser.add_subparsers(dest='command')
    listing = commands.add_parser('releases')
    listing.add_argument('--variant', choices=('all', 'os3', 'os4-official'), default='all')
    listing.add_argument('--stable-only', action='store_true')
    commands.add_parser('list')
    commands.add_parser('installer-updates')
    installing = commands.add_parser('install')
    installing.add_argument('--bundle', type=Path)
    installing.add_argument('--release', default='latest')
    installing.add_argument('--variant', choices=('os3', 'os4-official'), default='os4-official')
    installing.add_argument('--root', type=Path, required=True)
    installing.add_argument('--name', required=True)
    installing.add_argument('--port', type=int)
    installing.add_argument('--sdk', type=Path)
    installing.add_argument('--ram', type=float, default=6)
    installing.add_argument('--storage', type=int, default=32)
    installing.add_argument('--cores', type=int, default=4)
    installing.add_argument('--start', action='store_true')
    installing.add_argument('--camera', action='store_true', help='Enable experimental Xiaomi camera bridge on boot')
    starting = commands.add_parser('start')
    starting.add_argument('--root', type=Path, required=True)
    starting.add_argument('--headless', action='store_true')
    starting.add_argument('--skip-oobe', action='store_true')
    args = parser.parse_args()
    global LANG
    LANG = args.language or 'zh'
    host_check()
    if args.command == 'releases':
        print(json.dumps([{key: item[key] for key in ('tag_name', 'html_url', 'prerelease')}
                          for item in catalog(args.variant, args.stable_only)], indent=2))
    elif args.command == 'list':
        print(json.dumps(instances(), indent=2, default=str))
    elif args.command == 'installer-updates':
        installer_updates()
    elif args.command == 'start':
        start({'root': args.root.expanduser().resolve()}, args.headless, args.skip_oobe)
    elif args.command == 'install':
        root = args.root.expanduser().resolve()
        previous = root / 'local/runtime.json'
        port = args.port or (json.loads(previous.read_text())['port'] if previous.exists() else choose_port())
        settings = hardware(args.ram, args.storage, args.cores)
        if port % 2 or not 5556 <= port <= 5682:
            parser.error('Use an even console port between 5556 and 5682.')
        if args.bundle:
            path = args.bundle.expanduser().resolve()
        else:
            idle(root, args.name, port)
            rows = catalog(args.variant)
            choices = rows if args.release == 'latest' else [r for r in rows if r['tag_name'] == args.release or version_key(r['tag_name']) == version_key(args.release)]
            if not choices:
                raise RuntimeError('Requested release is not published.')
            path = fetch_release(choices[0], HOME / 'downloads')
        install(root, path, args.name, port, sdk_path(args.sdk), settings, args.camera)
        if args.start:
            start({'root': root})
    else:
        tui(args.language)


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        raise SystemExit(tr('操作失败：', 'Operation failed: ') + str(error))
    except (KeyboardInterrupt, EOFError):
        print('\n' + tr('已退出；下载缓存和备份保留。', 'Exited; download cache and backups retained.'))
