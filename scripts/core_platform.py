"""Configure persistent Core state using authenticated image defaults as data."""
import hashlib
import json
from pathlib import PurePosixPath
import re
import shlex
import uuid

STATE = '/data/adb/hyperos-avd-core'
SERIAL = re.compile(r'(?:[0-9]{5}/[A-HJ-NP-Z][0-9][NPQRSTUVWXYZ][1-9ABCDEFHJKMNPQRSTUVWXYZ][0-9]{5}|[0-9A-F]{16})')
IDENTITY_PATHS = {'/system/build.prop', '/product/etc/build.prop'}


def digest(data):
    return hashlib.sha256(data.encode() if isinstance(data, str) else data).hexdigest()


def context_files(context):
    """Validate paths and immutable pins before reading or changing a guest."""
    if (not isinstance(context, dict) or context.get('schema') != 1
            or not {'schema', 'identity_sources'} <= context.keys()
            or set(context) - {'schema', 'identity_sources', 'rear', 'boot_services'}):
        raise RuntimeError('Unknown Core platform context schema.')
    rows, seen = [], set()
    identity = context['identity_sources']
    if not isinstance(identity, list) or not identity:
        raise RuntimeError('Core context needs verified current-image identity sources.')

    def append(kind, item):
        if not isinstance(item, dict) or set(item) != {'path', 'sha256'}:
            raise RuntimeError('Invalid Core image source record.')
        path, checksum = item['path'], item['sha256']
        if (not isinstance(path, str) or not re.fullmatch(r'/[A-Za-z0-9_./-]+', path)
                or '..' in PurePosixPath(path).parts or '//' in path
                or not isinstance(checksum, str) or not re.fullmatch('[0-9a-f]{64}', checksum)
                or path in seen):
            raise RuntimeError('Invalid or duplicate Core image pin.')
        if kind == 'identity' and path not in IDENTITY_PATHS:
            raise RuntimeError('Unreviewed Core identity source path.')
        if kind == 'rear' and not (path in {'/system_ext/framework/rear-display-wake.jar',
                '/system_ext/bin/rear-display-wake', '/product/etc/hyperos-avd-rear-display.json'}
                or path == '/product/overlay/HyperOSAVDRearDisplay/RearDisplay.apk'
                or re.fullmatch(r'/product/etc/displayconfig/display_id_[0-9]+\.xml', path)):
            raise RuntimeError('Unreviewed Core rear prerequisite path.')
        seen.add(path); rows.append((kind, path, checksum))

    for item in identity:
        append('identity', item)
    files = {}
    rear = context.get('rear')
    if rear is not None:
        if (not isinstance(rear, dict) or set(rear) != {'files', 'display_id', 'group_id', 'unique_id', 'launcher_path'}
                or rear['display_id'] != 1 or rear['group_id'] != 1
                or rear['unique_id'] != 'local:4619827551948147201'
                or rear['launcher_path'] != '/system_ext/bin/rear-display-wake'
                or not isinstance(rear['files'], list)):
            raise RuntimeError('Unknown Core rear topology or helper contract.')
        for item in rear['files']:
            append('rear', item)
        from apply_rear_display_fix import EXPECTED_WAKE_MANIFEST
        pins = {item['path']: item['sha256'] for item in rear['files']}
        for path, checksum in ((EXPECTED_WAKE_MANIFEST['jar_path'], EXPECTED_WAKE_MANIFEST['jar_sha256']),
                               (EXPECTED_WAKE_MANIFEST['script_path'], EXPECTED_WAKE_MANIFEST['script_sha256'])):
            if pins.get(path) != checksum:
                raise RuntimeError('Unknown Core rear wake native helper.')
        if ('/product/etc/hyperos-avd-rear-display.json' not in pins
                or not any(path.startswith('/product/overlay/') for path in pins)
                or not any(path.startswith('/product/etc/displayconfig/') for path in pins)):
            raise RuntimeError('Incomplete Core physical rear display prerequisites.')
        files['rear.record'] = '1|1|1|local:4619827551948147201\n'
    boot = context.get('boot_services')
    if boot is not None:
        from patch_boot_services import TARGETS, AFTER, PROBE_SHA256
        expected = {item[0]: AFTER[name] for name, item in TARGETS.items()}
        if (not isinstance(boot, dict) or set(boot) != {'files', 'probe'}
                or not isinstance(boot['files'], list)
                or boot['probe'] != {'path': '/system/bin/hyperos_kernel_probe', 'sha256': PROBE_SHA256}
                or len(boot['files']) != len(expected)
                or any(not isinstance(item, dict) or set(item) != {'path', 'sha256'} for item in boot['files'])
                or {item['path']: item['sha256'] for item in boot['files']} != expected):
            raise RuntimeError('Unreviewed Core boot-service image prerequisites.')
        for item in [*boot['files'], boot['probe']]:
            rows.append(('boot', item['path'], item['sha256']))
    files['image.tsv'] = ''.join('|'.join(row) + '\n' for row in sorted(rows))
    files['image.json'] = json.dumps(context, sort_keys=True, indent=2) + '\n'
    return files


def parent_guard(path=STATE):
    parents = []
    while path != '/':
        parents.append(path); path = path.rsplit('/', 1)[0] or '/'
    guards = []
    for parent in reversed(parents):
        # Android's userdata root is system-owned. KernelSU /data/adb and
        # every managed child must remain root-owned; this exception is exact.
        owner = f'"$(/data/adb/ksu/bin/busybox stat -c %u {shlex.quote(parent)})"'
        ownership = f'{{ [ {owner} = 0 ] || [ {owner} = 1000 ]; }}' if parent == '/data' else f'[ {owner} = 0 ]'
        guards.append(f'[ ! -L {shlex.quote(parent)} ] && {{ [ ! -e {shlex.quote(parent)} ] || '
                      f'{{ [ -d {shlex.quote(parent)} ] && {ownership}; }}; }} || exit 1')
    return '\n'.join(guards) + '\n'


def module_guard(directory, checksum):
    """A staged Core is allowed only after its exact package is authenticated."""
    from apply_native_compat import MODULE, PENDING
    if directory not in (MODULE, PENDING) or not re.fullmatch('[0-9a-f]{64}', checksum):
        raise RuntimeError('Invalid Core migration owner.')
    guard = f'''BB=/data/adb/ksu/bin/busybox
[ -x "$BB" ] || exit 1
[ -d {directory} ] && [ ! -L {directory} ] || exit 1
[ -f {directory}/SHA256SUMS ] && [ ! -L {directory}/SHA256SUMS ] || exit 1
[ -z "$("$BB" find {directory} -type l -print)" ] || exit 1
[ "$("$BB" sha256sum {directory}/SHA256SUMS | "$BB" cut -d ' ' -f 1)" = {checksum} ] || exit 1
(cd {directory} && "$BB" sha256sum -c SHA256SUMS >/dev/null) || exit 1
'''
    for location in (MODULE, PENDING):
        for flag in ('disable', 'remove'):
            guard += f'[ ! -e {location}/{flag} ] && [ ! -L {location}/{flag} ] || exit 1\n'
    if directory == MODULE:
        guard += f'[ ! -e {PENDING} ] && [ ! -L {PENDING} ] || exit 1\n'
    return guard


def atomic_write(root, config, path, data, guard, expected=None):
    """Replace only the exact inspected record; never follow an alias."""
    stage = path + '.next-' + uuid.uuid4().hex
    if expected is None:
        previous = f'[ ! -e {path} ] && [ ! -L {path} ] || exit 1\n'
    else:
        previous = f'''[ -f {path} ] && [ ! -L {path} ] || exit 1
[ "$("$BB" stat -c %u {path})" = 0 ] && [ "$("$BB" stat -c %h {path})" = 1 ] || exit 1
[ "$("$BB" sha256sum {path} | "$BB" cut -d ' ' -f 1)" = {expected} ] || exit 1
'''
    root(config, 'set -e\n' + guard + parent_guard(path.rsplit('/', 1)[0]) + previous +
         f'''umask 077
mkdir -p {path.rsplit('/', 1)[0]}
[ ! -e {stage} ] && [ ! -L {stage} ] || exit 1
(set -C; : > {stage}) || exit 1
trap {shlex.quote('rm -f ' + stage)} EXIT
printf %s {shlex.quote(data)} > {stage}
chmod 600 {stage}
[ "$("$BB" sha256sum {stage} | "$BB" cut -d ' ' -f 1)" = {digest(data)} ] || exit 1
''' + guard + previous + f'mv -f {stage} {path}\n')


def read_record(root, config, path):
    """Return bytes and a digest only for an unaliased, root-owned record."""
    value = root(config, parent_guard(path.rsplit('/', 1)[0]) + f'''BB=/data/adb/ksu/bin/busybox
if [ -e {path} ] || [ -L {path} ]; then
    [ -f {path} ] && [ ! -L {path} ] || exit 1
    [ "$("$BB" stat -c %u {path})" = 0 ] && [ "$("$BB" stat -c %h {path})" = 1 ] || exit 1
    printf 'present\\n'
    cat {path}
    printf '\\n__HYPEROS_RECORD_END__'
else printf 'absent\\n'; fi''')
    if value == 'absent':
        return None
    if not value.startswith('present\n') or not value.endswith('\n__HYPEROS_RECORD_END__'):
        raise RuntimeError('Invalid Core record inspection response.')
    return value[len('present\n'):-len('\n__HYPEROS_RECORD_END__')]


def serial_record(value):
    if not isinstance(value, str) or not SERIAL.fullmatch(value):
        raise RuntimeError('Invalid saved simulated serial; identifier preserved.')
    return '1|hyperos_avd_native_compat|' + value + '\n'


def migrate_serial(root, config, guard, legacy):
    """Adopt authenticated Phone/Pad userdata identity without changing it."""
    file = STATE + '/serial.record'
    saved = read_record(root, config, file)
    if saved is not None:
        if not saved.endswith('\n') or saved.count('\n') != 1:
            raise RuntimeError('Unknown persistent Core serial record; preserved.')
        fields = saved.rstrip('\n').split('|')
        if len(fields) != 3 or fields[:2] != ['1', 'hyperos_avd_native_compat']:
            raise RuntimeError('Unknown persistent Core serial record; preserved.')
        serial_record(fields[2])
        return {'status': 'ready', 'serial_number': fields[2], 'reused': True}
    values = [item['serial_number'] for item in legacy if item.get('serial_number') is not None]
    pad_text = read_record(root, config, '/data/adb/hyperos-avd-pad/serial.json')
    if pad_text is not None:
        import os4_pad
        pad = json.loads(pad_text)
        if (not isinstance(pad, dict) or set(pad) != {'revision', 'source', 'hyperos', 'serial_number'}
                or pad['revision'] != 1 or pad['source'] != os4_pad.SOURCE
                or not re.fullmatch(r'OS4\.[0-9]+\.[0-9]+\.[0-9]+\.XBMCNXM', pad['hyperos'])):
            raise RuntimeError('Unknown Pad serial provenance; preserved.')
        serial_record(pad['serial_number'])
        # Exact original generator, including its original firmware guard.
        hook = read_record(root, config, os4_pad.SERIAL_EARLY)
        expected = os4_pad._serial_script(pad, pad['hyperos'])
        if hook != expected:
            raise RuntimeError('Unreviewed Pad serial hook; preserved.')
        values.append(pad['serial_number'])
    if len(set(values)) > 1:
        raise RuntimeError('Conflicting saved simulated serials; all identifiers preserved.')
    if values:
        value = values[0]
        atomic_write(root, config, file, serial_record(value), guard)
        return {'status': 'ready', 'serial_number': value, 'migrated': True}
    # The early guest runtime mints one identifier under a kernel file lease.
    return {'status': 'pending', 'reason': 'first-boot-identity'}


def configure(root, config, context, directory, checksum):
    """Publish state only after Core assets and image files are authenticated."""
    files = context_files(context)
    guard = module_guard(directory, checksum)
    # Source pin verification precedes any persistent state or legacy mutation.
    commands = [guard]
    for row in files['image.tsv'].splitlines():
        _, path, expected = row.split('|')
        commands.append(f'''[ -f {path} ] && [ ! -L {path} ] || exit 1
[ "$("$BB" sha256sum {path} | "$BB" cut -d ' ' -f 1)" = {expected} ] || exit 1''')
    root(config, '\n'.join(commands))
    # An OTA may replace valid prior image defaults. Unknown local state is
    # not a migration receipt and must never be overwritten opportunistically.
    previous_json = read_record(root, config, STATE + '/image.json')
    previous_tsv = read_record(root, config, STATE + '/image.tsv')
    if (previous_json is None) != (previous_tsv is None):
        # Recover a crash between the two publications only when the surviving
        # half exactly matches this newly authenticated image context.
        if (previous_json is not None and previous_json != files['image.json']
                or previous_tsv is not None and previous_tsv != files['image.tsv']):
            raise RuntimeError('Incomplete prior Core image context; local state preserved.')
    if previous_json is not None:
        try:
            prior_files = context_files(json.loads(previous_json))
        except (ValueError, TypeError, RuntimeError) as error:
            raise RuntimeError('Unknown prior Core image context; local state preserved.') from error
        if previous_tsv is not None and prior_files['image.tsv'] != previous_tsv and previous_tsv != files['image.tsv']:
            raise RuntimeError('Changed prior Core image pins; local state preserved.')
    prior_rear = read_record(root, config, STATE + '/rear.record')
    if prior_rear is not None and prior_rear != '1|1|1|local:4619827551948147201\n':
        raise RuntimeError('Unknown prior Core rear topology; local state preserved.')
    from core_legacy import inspect_legacy, retire_legacy, migrate_global_hooks
    legacy = inspect_legacy(root, config, context)
    blocked = sorted({feature for item in legacy if item['status'] == 'preserved' for feature in item['features']})
    for name, data in files.items():
        previous = read_record(root, config, STATE + '/' + name)
        if previous == data:
            continue
        atomic_write(root, config, STATE + '/' + name, data, guard,
                     digest(previous) if previous is not None else None)
    try:
        serial = migrate_serial(root, config, guard, legacy)
    except (RuntimeError, ValueError, TypeError) as error:
        serial = {'status': 'preserved', 'reason': str(error)}
        blocked.append('serial')
    # Block competing roles before moving hooks; a failure remains fail-closed.
    collision = sorted(set(blocked) | {feature for item in legacy if item['status'] == 'audited'
                                      for feature in item['features']})
    path = STATE + '/legacy-blocked.features'
    previous = read_record(root, config, path)
    atomic_write(root, config, path, ''.join(item + '\n' for item in collision), guard,
                 digest(previous) if previous is not None else None)
    for item in legacy:
        if item['status'] == 'audited':
            try:
                retire_legacy(root, config, item, guard)
                item['status'] = 'retired'
            except RuntimeError:
                item['status'] = 'preserved'; item['reason'] = 'changed-during-migration'
                blocked.extend(item['features'])
    globals_result = migrate_global_hooks(root, config, guard, serial)
    blocked.extend(feature for item in globals_result if item['status'] == 'preserved' for feature in item['features'])
    current = read_record(root, config, path)
    atomic_write(root, config, path, ''.join(item + '\n' for item in sorted(set(blocked))), guard,
                 digest(current) if current is not None else None)
    return {'identity': 'verified-current-image', 'serial': serial, 'legacy': legacy,
            'global_hooks': globals_result, 'blocked_features': sorted(set(blocked)),
            'activation': 'normal-boot', 'status_path': directory + '/state/status.tsv'}
