#!/usr/bin/env python3
"""Package and stage exact-workload private libraries through KernelSU."""
import hashlib
import json
from pathlib import Path
import re
import shlex
import stat
import tempfile
import zipfile

from common import REPO_ROOT, adb, sha256
from apply_flutter_fix import root

REVISION = 2
TEMPLATE = REPO_ROOT / 'modules/app-bridge'
EMPTY = hashlib.sha256(b'').hexdigest()


def _word(value):
    return isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]*', value)


def _hex(value):
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value)


def _path(value):
    return (isinstance(value, str)
            and re.fullmatch(r'/(?:product|system|system_ext|data/app|data/app-lib)/[A-Za-z0-9_./=+~-]+', value)
            and not any(part in ('', '.', '..') for part in value.split('/')[1:]))


def validate_profiles(profiles, payloads):
    if not isinstance(profiles, list) or not profiles or payloads is not None and not isinstance(payloads, dict):
        raise RuntimeError('Invalid private bridge profile catalog.')
    seen, identities, expected = set(), set(), {}
    for index, profile in enumerate(profiles):
        if not isinstance(profile, dict) or not _hex(profile.get('apk_sha256')):
            raise RuntimeError('Invalid private bridge signed workload.')
        if profile['apk_sha256'] in seen:
            raise RuntimeError('Ambiguous private bridge signed workload.')
        seen.add(profile['apk_sha256'])
        identity = profile.get('id', 'workload-' + str(index))
        if not _word(identity) or identity in identities:
            raise RuntimeError('Invalid private bridge profile ID.')
        identities.add(identity)
        factory, native = profile.get('factory_apk', '-'), profile.get('native', '-')
        if (factory == '-') != (native == '-') or factory != '-' and not (_path(factory) and _path(native)):
            raise RuntimeError('Invalid private bridge factory layout.')
        libraries = profile.get('libraries')
        if not isinstance(libraries, list) or not libraries:
            raise RuntimeError('Empty private bridge native recipe.')
        names = set()
        for library in libraries:
            if not isinstance(library, dict):
                raise RuntimeError('Invalid private bridge native recipe.')
            name, before, after = (library.get(key) for key in ('name', 'before', 'after'))
            if not _word(name) or not name.endswith('.so') or name in names or not _hex(before) or not _hex(after):
                raise RuntimeError('Invalid or duplicate private bridge library.')
            names.add(name)
            if type(library.get('placeholder', False)) is not bool:
                raise RuntimeError('Invalid private bridge placeholder policy.')
            if library.get('placeholder', False) and before != EMPTY:
                raise RuntimeError('Private bridge placeholders require an absent original library.')
            legacy = library.get('legacy_direct', [])
            if not isinstance(legacy, list) or any(not _hex(value) for value in legacy):
                raise RuntimeError('Invalid private bridge legacy helper checksum.')
            if legacy and not library.get('placeholder'):
                raise RuntimeError('Legacy direct migration is restricted to private helper placeholders.')
            if name in expected and expected[name] != after:
                raise RuntimeError('A private bridge payload has conflicting profiles.')
            expected[name] = after
        systems = profile.get('system_libraries', {})
        if not isinstance(systems, dict) or any(not _path(path) or not _hex(checksum)
                                                for path, checksum in systems.items()):
            raise RuntimeError('Invalid private bridge runtime dependency pin.')
        callers = profile.get('native_libraries', {})
        if (not isinstance(callers, dict) or any(not _word(name) or not name.endswith('.so')
                                                or not _hex(checksum) for name, checksum in callers.items())):
            raise RuntimeError('Invalid private bridge caller ABI pin.')
    if payloads is None:
        return expected
    if set(payloads) != set(expected):
        raise RuntimeError('Private bridge payload set differs from its recipes.')
    for name, checksum in expected.items():
        path = Path(payloads[name])
        if not path.is_file() or path.is_symlink() or path.stat().st_nlink != 1 or sha256(path) != checksum:
            raise RuntimeError('Private bridge payload checksum or ownership changed: ' + name)
    return expected


def module_files(*, module_id, name, description, package, profiles, payloads):
    for value in (module_id, package):
        if not _word(value):
            raise RuntimeError('Invalid private bridge module identity.')
    for value in (name, description):
        if not isinstance(value, str) or any(character in value for character in '\r\n'):
            raise RuntimeError('Invalid private bridge module text.')
    payload_hashes = validate_profiles(profiles, payloads)
    files = {}
    for filename in ('customize.sh', 'runtime.sh', 'service.sh', 'uninstall.sh'):
        source = TEMPLATE / filename
        if not source.is_file() or source.is_symlink():
            raise RuntimeError('Missing portable private bridge source: ' + filename)
        files[filename] = source.read_bytes()
    files['module.prop'] = (f'id={module_id}\nname={name}\nversion={REVISION}\nversionCode={REVISION}\n'
                            f'author=HyperOS-AVD\ndescription={description}\n').encode()
    files['skip_mount'] = b''
    files['package.txt'] = (package + '\n').encode()
    rows, libraries, systems, callers = [], [], [], []
    for index, profile in enumerate(profiles):
        identity = profile.get('id', 'workload-' + str(index))
        rows.append('|'.join((identity, profile['apk_sha256'], profile.get('factory_apk', '-'),
                              profile.get('native', '-'))))
        for library in profile['libraries']:
            libraries.append('|'.join((identity, library['name'], library['before'], library['after'],
                                       '1' if library.get('placeholder') else '0',
                                       ','.join(library.get('legacy_direct', [])))))
        for target, checksum in sorted(profile.get('system_libraries', {}).items()):
            systems.append('|'.join((identity, target, checksum)))
        for name, checksum in sorted(profile.get('native_libraries', {}).items()):
            callers.append('|'.join((identity, name, checksum)))
    for filename, records in (('profiles.tsv', rows), ('libraries.tsv', libraries), ('systems.tsv', systems),
                              ('callers.tsv', callers)):
        files[filename] = ('\n'.join(records) + ('\n' if records else '')).encode()
    for filename, path in sorted((payloads or {}).items()):
        files['payloads/' + filename] = Path(path).read_bytes()
    asset_hashes = {filename: hashlib.sha256(data).hexdigest() for filename, data in sorted(files.items())}
    asset_hashes.update({'payloads/' + filename: checksum for filename, checksum in payload_hashes.items()})
    manifest = {'schema': 1, 'id': module_id, 'revision': REVISION, 'package': package,
                'delivery': 'guest-private-exact-workload', 'profiles': profiles,
                'files_sha256': dict(sorted(asset_hashes.items())),
                'install_only_files': ['customize.sh'], 'automatic_reboot': False}
    files['manifest.json'] = (json.dumps(manifest, sort_keys=True, indent=2) + '\n').encode()
    asset_hashes['manifest.json'] = hashlib.sha256(files['manifest.json']).hexdigest()
    files['SHA256SUMS'] = ''.join(checksum + '  ' + filename + '\n'
                                 for filename, checksum in sorted(asset_hashes.items())
                                 if filename != 'customize.sh').encode()
    return files


def package_module(output, **kwargs):
    if kwargs.get('payloads') is None:
        raise RuntimeError('Private bridge packaging requires verified payload files.')
    files = module_files(**kwargs)
    with zipfile.ZipFile(output, 'x', zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for filename, body in sorted(files.items()):
            info = zipfile.ZipInfo(filename, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | (0o755 if filename.endswith('.sh') else 0o644)) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, body)
    return files


def _inspect(config, directory, module_id):
    value = root(config, f'''for parent in /data /data/adb /data/adb/modules /data/adb/modules_update; do
    [ ! -L "$parent" ] || exit 1
done
if [ -e {directory} ] || [ -L {directory} ]; then
    [ -d {directory} ] && [ ! -L {directory} ] || exit 1
    [ -f {directory}/module.prop ] && [ ! -L {directory}/module.prop ] || exit 1
    [ "$(stat -c %h {directory}/module.prop)" = 1 ] || exit 1
    printf 'present\\n'; cat {directory}/module.prop
else printf 'absent\\n'; fi''')
    if value == 'absent':
        return None
    if not value.startswith('present\n'):
        raise RuntimeError('Invalid private bridge ownership record.')
    properties = value.split('\n', 1)[1]
    values = {}
    for line in properties.splitlines():
        if '=' in line:
            key, item = line.split('=', 1)
            if key in values:
                raise RuntimeError('Duplicate private bridge module property.')
            values[key] = item
    if values.get('id') != module_id or values.get('author') != 'HyperOS-AVD':
        raise RuntimeError('Refused unrelated private bridge module ownership.')
    revision = values.get('versionCode')
    if revision not in ('1', str(REVISION)):
        raise RuntimeError('Unknown private bridge module revision.')
    flags = root(config, f'''for flag in disable remove; do
    if [ -e {directory}/"$flag" ] || [ -L {directory}/"$flag" ]; then printf '%s\\n' "$flag"; fi
done''').splitlines()
    if any(flag not in ('disable', 'remove') for flag in flags):
        raise RuntimeError('Invalid private bridge lifecycle flags.')
    return {'directory': directory, 'properties': properties, 'revision': int(revision), 'flags': flags}


def _saved(config, module):
    directory = module['directory']
    data = root(config, f'''[ -f {directory}/manifest.json ] && [ ! -L {directory}/manifest.json ] || exit 1
cat {directory}/manifest.json''')
    try:
        saved = json.loads(data)
    except (ValueError, TypeError) as error:
        raise RuntimeError('Invalid private bridge saved manifest.') from error
    if (not isinstance(saved, dict) or saved.get('schema') != 1
            or saved.get('id') != directory.rsplit('/', 1)[1] or saved.get('revision') != REVISION
            or saved.get('delivery') != 'guest-private-exact-workload'):
        raise RuntimeError('Refused unknown private bridge manifest ownership.')
    return saved


def _verify_assets(config, module, saved):
    directory = module['directory']
    files = saved.get('files_sha256')
    if not isinstance(files, dict) or not files:
        raise RuntimeError('Missing private bridge asset receipts.')
    commands = []
    for filename, checksum in files.items():
        if filename == 'customize.sh':
            continue
        if not re.fullmatch(r'(?:payloads/)?[A-Za-z0-9_.-]+', filename) or not _hex(checksum):
            raise RuntimeError('Invalid private bridge asset receipt.')
        path = directory + '/' + filename
        commands.append(f'''[ -f {path} ] && [ ! -L {path} ] || exit 1
[ "$(stat -c %h {path})" = 1 ] || exit 1
[ "$(sha256sum {path} | cut -d ' ' -f 1)" = {checksum} ] || exit 1''')
    manifest_checksum = hashlib.sha256((json.dumps(saved, sort_keys=True, indent=2) + '\n').encode()).hexdigest()
    hashes = {**files, 'manifest.json': manifest_checksum}
    checksum_list = ''.join(checksum + '  ' + filename + '\n'
                            for filename, checksum in sorted(hashes.items()) if filename != 'customize.sh')
    commands += [f'''[ -f {directory}/manifest.json ] && [ ! -L {directory}/manifest.json ] || exit 1
[ "$(stat -c %h {directory}/manifest.json)" = 1 ] || exit 1
[ "$(sha256sum {directory}/manifest.json | cut -d ' ' -f 1)" = {manifest_checksum} ] || exit 1
[ -f {directory}/SHA256SUMS ] && [ ! -L {directory}/SHA256SUMS ] || exit 1
[ "$(stat -c %h {directory}/SHA256SUMS)" = 1 ] || exit 1
[ "$(sha256sum {directory}/SHA256SUMS | cut -d ' ' -f 1)" = {hashlib.sha256(checksum_list.encode()).hexdigest()} ] || exit 1''']
    root(config, '\n'.join(commands))


def install(config, *, module_id, name, description, package, profiles, payloads, legacy=None):
    """Stage immutable payloads; KernelSU activates them on the next normal boot."""
    if not _word(module_id):
        raise RuntimeError('Invalid private bridge module identity.')
    values = root(config, 'getprop ro.boot.hardware; getprop ro.mi.os.version.incremental').splitlines()
    if len(values) != 2 or values[0] != 'ranchu' or not values[1].startswith(('OS4.', '4.')):
        raise RuntimeError('Private bridge requires a ranchu OS4 guest.')
    active_path, pending_path = '/data/adb/modules/' + module_id, '/data/adb/modules_update/' + module_id
    active, pending = (_inspect(config, directory, module_id) for directory in (active_path, pending_path))
    for module in (active, pending):
        if module and module['flags']:
            print('Private bridge lifecycle choice is preserved: ' + ', '.join(module['flags']) + '.', flush=True)
            return {'installed': False, 'disabled': True, 'flags': module['flags']}
    with tempfile.TemporaryDirectory(prefix='hyperos-app-bridge-') as temporary:
        archive = Path(temporary) / 'bridge.zip'
        files = package_module(archive, module_id=module_id, name=name, description=description,
                               package=package, profiles=profiles, payloads=payloads)
        current = json.loads(files['manifest.json'])
        for module in (pending, active):
            if not module:
                continue
            if module['revision'] == 1:
                if not legacy or not legacy(config, module):
                    raise RuntimeError('Refused unknown legacy private bridge module.')
                if module is pending:
                    return {'installed': False, 'pending': True, 'deferred': True, 'reboot_required': True}
            else:
                saved = _saved(config, module)
                if saved == current:
                    _verify_assets(config, module, current)
                    return {'installed': True, 'reused': True, 'pending': module is pending,
                            'reboot_required': module is pending, 'revision': REVISION}
                if module is pending:
                    print('A different private bridge update is staged; preserving it until normal boot.', flush=True)
                    return {'installed': False, 'pending': True, 'deferred': True, 'reboot_required': True}
                raise RuntimeError('Unknown or changed private bridge manifest; local module is preserved.')
        if pending:
            return {'installed': False, 'pending': True, 'deferred': True, 'reboot_required': True}
        guards = [f'[ ! -e {pending_path} ] && [ ! -L {pending_path} ] || exit 1']
        if active:
            guards += [f'[ -d {active_path} ] && [ ! -L {active_path} ] || exit 1',
                       f'test "$(cat {active_path}/module.prop)" = {shlex.quote(active["properties"])} || exit 1']
            guards += [f'[ ! -e {active_path}/{flag} ] && [ ! -L {active_path}/{flag} ] || exit 1'
                       for flag in ('disable', 'remove')]
        else:
            guards += [f'[ ! -e {active_path} ] && [ ! -L {active_path} ] || exit 1']
        checksum = sha256(archive)
        remote = '/data/local/tmp/hyperos-app-bridge-' + checksum[:16] + '.zip'
        adb(config, 'push', str(archive), remote, check=True, capture_output=True, timeout=60)
        try:
            root(config, '\n'.join(guards) +
                 f'\ntest "$(sha256sum {remote} | cut -d \' \' -f 1)" = {checksum} || exit 1\n'
                 f'/data/adb/ksud module install {shlex.quote(remote)}')
        finally:
            root(config, 'rm -f ' + shlex.quote(remote))
    return {'installed': True, 'reused': False, 'reboot_required': True, 'revision': REVISION}


def _authenticate(config, module, *, profiles, name, description, package):
    if any(value is None for value in (profiles, name, description, package)):
        raise RuntimeError('Private bridge lifecycle requires its canonical project recipe.')
    expected = json.loads(module_files(module_id=module['directory'].rsplit('/', 1)[1],
                                       name=name, description=description, package=package,
                                       profiles=profiles, payloads=None)['manifest.json'])
    if _saved(config, module) != expected:
        raise RuntimeError('Unknown or modified private bridge manifest; local module is preserved.')
    _verify_assets(config, module, expected)


def remove(config, module_id, *, profiles=None, name=None, description=None, package=None):
    """Use KernelSU's uninstall lifecycle after authenticating project assets."""
    if not _word(module_id):
        raise RuntimeError('Invalid private bridge module identity.')
    active = _inspect(config, '/data/adb/modules/' + module_id, module_id)
    pending = _inspect(config, '/data/adb/modules_update/' + module_id, module_id)
    if pending:
        raise RuntimeError('A private bridge update is staged; boot normally before removal.')
    if not active:
        return {'removed': False, 'absent': True}
    if active['revision'] != REVISION:
        raise RuntimeError('Legacy private bridge removal requires its authenticated migration.')
    _authenticate(config, active, profiles=profiles, name=name, description=description, package=package)
    root(config, '/data/adb/ksud module uninstall ' + shlex.quote(module_id))
    return {'removed': True, 'reboot_required': True}


def set_enabled(config, module_id, enabled, legacy=None, *, profiles=None, name=None,
                description=None, package=None):
    """Change only an authenticated active module's explicit disable flag."""
    if not _word(module_id) or type(enabled) is not bool:
        raise RuntimeError('Invalid private bridge lifecycle request.')
    active = _inspect(config, '/data/adb/modules/' + module_id, module_id)
    pending = _inspect(config, '/data/adb/modules_update/' + module_id, module_id)
    if pending or not active or 'remove' in active['flags']:
        raise RuntimeError('Private bridge lifecycle is absent, staged, or pending removal.')
    if active['revision'] == 1:
        if not legacy or not legacy(config, active):
            raise RuntimeError('Unknown legacy private bridge lifecycle.')
    else:
        _authenticate(config, active, profiles=profiles, name=name, description=description, package=package)
    directory = active['directory']
    guard = (f'[ -d {directory} ] && [ ! -L {directory} ] || exit 1\n'
             f'test "$(cat {directory}/module.prop)" = {shlex.quote(active["properties"])} || exit 1\n'
             f'[ ! -e {directory}/remove ] && [ ! -L {directory}/remove ] || exit 1\n'
             f'[ ! -e /data/adb/modules_update/{module_id} ] && '
             f'[ ! -L /data/adb/modules_update/{module_id} ] || exit 1\n')
    command = 'rm -f ' + directory + '/disable' if enabled else 'touch ' + directory + '/disable'
    root(config, guard + command)
    return {'enabled': enabled, 'reboot_required': True}
