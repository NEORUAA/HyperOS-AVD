"""Import audited legacy app choices and archive owners after normal activation.

Historical controls are frozen receipts, not generated from today's runtime.
Archiving renames whole directories so loaded libraries retain their inodes.
No legacy hook, uninstall command, lazy unmount or provider restart is executed.
"""
import hashlib
import json
from pathlib import Path
import re
import shlex
import subprocess

from common import ROOT, REPO_ROOT, sha256
from apply_flutter_fix import root
from app_bridge_module import _inspect, _path, _word

RECORDS = REPO_ROOT / 'scripts/app_compat_legacy_records.json'
RECORDS_SHA256 = '506ef49ea224e4c64d260779e332416382d7ede47af556d1edc1ac5bb40c4d0a'
PREFERENCES = '/data/adb/hyperos_app_compat/features'
ARCHIVE = '/data/adb/hyperos_app_compat/legacy'
MODULES = {'hyperos_avd_weather_gl': 'weather',
           'hyperos_avd_parrot_camera': 'parrot-camera',
           'hyperos_avd_xiaomi_camera': 'oem-camera',
           'hyperos_avd_pad_camera': 'oem-camera'}


def records():
    if (not RECORDS.is_file() or RECORDS.is_symlink() or RECORDS.stat().st_nlink != 1
            or sha256(RECORDS) != RECORDS_SHA256):
        raise RuntimeError('Historical application migration receipt changed.')
    return json.loads(RECORDS.read_text())['records']


def _pending(config, identifier):
    path = '/data/adb/modules_update/' + identifier
    return root(config, f'[ ! -L /data ] && [ ! -L /data/adb ] && [ ! -L /data/adb/modules_update ] || exit 1\n'
                f'if [ -e {path} ] || [ -L {path} ]; then printf yes; fi') == 'yes'


def _preference(config, feature):
    path = PREFERENCES + '/' + feature
    value = root(config, f'''for directory in /data /data/adb /data/adb/hyperos_app_compat {PREFERENCES}; do
    [ ! -L "$directory" ] || exit 1
done
if [ -e {path} ] || [ -L {path} ]; then
    [ -f {path} ] && [ ! -L {path} ] && [ "$(stat -c %h:%u {path})" = 1:0 ] || exit 1
    cat {path}
else printf missing; fi''')
    if value not in ('missing', 'on', 'off', 'auto'):
        raise RuntimeError('Unknown application feature preference is preserved: ' + feature)
    return None if value == 'missing' else value


def _asset_path(directory, name, checksums, *, root_owned=True):
    if (not isinstance(name, str) or (any(not _word(part) for part in name.split('/'))
                                    if root_owned else not _path('/' + name))
            or not isinstance(checksums, (list, tuple)) or not checksums
            or any(not isinstance(checksum, str) or not re.fullmatch('[0-9a-f]{64}', checksum)
                   for checksum in checksums)):
        raise RuntimeError('Invalid historical application asset receipt.')
    return directory + '/' + name


def _parent_checks(paths, checked=None):
    """Check each mandatory ancestor once, including combined owner guards."""
    checked = set() if checked is None else checked
    parents = {str(parent) for path in paths for parent in Path(path).parents if str(parent) != '/'}
    commands = []
    for parent in sorted(parents, key=lambda item: (len(Path(item).parts), item)):
        if parent not in checked:
            quoted = shlex.quote(parent)
            commands.append(f'[ -d {quoted} ] && [ ! -L {quoted} ] || exit 1')
            checked.add(parent)
    return commands


def _file_checks(directory, name, checksums, *, optional=False, root_owned=True, checked_parents=None):
    path = _asset_path(directory, name, checksums, root_owned=root_owned)
    # Optional ancestors must remain within this conditional: their absence is
    # allowed. Do not let a skipped optional check authenticate another asset.
    commands = _parent_checks([path], set(checked_parents or ()))
    quoted = shlex.quote(path)
    ownership = f'[ "$(stat -c %h:%u {quoted})" = 1:0 ] || exit 1' if root_owned else \
                f'[ "$(stat -c %h {quoted})" = 1 ] || exit 1'
    commands += [f'[ -f {quoted} ] && [ ! -L {quoted} ] || exit 1', ownership,
                 f'case "$(sha256sum {quoted} | cut -d \' \' -f 1)" in\n'
                 + '|'.join(checksums) + ') ;;\n*) exit 1 ;;\nesac']
    body = '\n'.join(commands)
    return f'if [ -e {quoted} ] || [ -L {quoted} ]; then\n{body}\nfi' if optional else body


def _checks(module, record, *, checked_parents=None):
    directory, identifier = module['directory'], record['id']
    if directory != '/data/adb/modules/' + identifier or module['properties'].strip() != record['properties'].strip():
        raise RuntimeError('Historical application ownership identity changed.')
    commands = [f'[ -d {directory} ] && [ ! -L {directory} ] && '
                f'[ "$(stat -c %u {directory})" = 0 ] || exit 1',
                f'[ ! -e /data/adb/modules_update/{identifier} ] && '
                f'[ ! -L /data/adb/modules_update/{identifier} ] || exit 1']
    for flag in ('disable', 'remove'):
        path = directory + '/' + flag
        commands.append(f'[ -e {path} ] || [ -L {path} ] || exit 1' if flag in module['flags'] else
                        f'[ ! -e {path} ] && [ ! -L {path} ] || exit 1')
    required = {name: [checksum] for name, checksum in record['hashes'].items()}
    required.update(record.get('allowed_hashes', {}))
    paths = [_asset_path(directory, name, checksums) for name, checksums in required.items()]
    checked_parents = set() if checked_parents is None else checked_parents
    commands += _parent_checks(paths, checked_parents)
    # Keep the complete receipt in a compact table. Per-file expanded paths and
    # ancestor guards can otherwise exceed the ADB shell command limit.
    table = '\n'.join(name + ' ' + '|'.join(checksums) for name, checksums in sorted(required.items()))
    commands.append(f'''directory={shlex.quote(directory)}
while IFS=' ' read -r name checksums; do
    path="$directory/$name"
    [ -f "$path" ] && [ ! -L "$path" ] || exit 1
    [ "$(stat -c %h:%u "$path")" = 1:0 ] || exit 1
    actual=$(sha256sum "$path" | cut -d ' ' -f 1)
    case "|$checksums|" in *"|$actual|"*) ;; *) exit 1 ;; esac
done <<'HYPEROS_LEGACY_REQUIRED'
{table}
HYPEROS_LEGACY_REQUIRED''')
    for name, checksum in sorted(record.get('optional_hashes', {}).items()):
        commands.append(_file_checks(directory, name, [checksum], optional=True,
                                     checked_parents=checked_parents))
    allowed = {name.split('/')[0] for name in [*required, *record.get('optional_hashes', {})]}
    allowed |= {'disable', 'remove', 'cache', 'state', 'runtime.log', 'runtime.log.next'}
    # Added KernelSU entry points or local controls make this a custom owner.
    flag_links = '|'.join(shlex.quote(directory + '/' + flag) for flag in module['flags']) or 'never-an-absolute-path'
    commands.append(f'''for entry in {directory}/* {directory}/.[!.]* {directory}/..?*; do
    [ -e "$entry" ] || [ -L "$entry" ] || continue
    case "${{entry##*/}}" in {'|'.join(sorted(allowed))}) ;; *) exit 1 ;; esac
done
find {directory} -type l -print | while IFS= read -r alias; do
    case "$alias" in {flag_links}) ;; *) exit 1 ;; esac
done''')
    return '\n'.join(commands)


def _authenticate(config, module, index, registry):
    record = registry[index]
    result = root(config, _checks(module, record) + '\nstat -c %d:%i ' + module['directory'])
    if not re.fullmatch(r'[0-9]+:[0-9]+', result):
        raise RuntimeError('Invalid historical application inode identity.')
    return result


def _marker(config, workspace, catalog):
    """A host opt-in marker imports policy only after exact live caller checks."""
    import apply_camera_fix as parrot
    workspace = Path(workspace).resolve()
    if (workspace / 'local').is_symlink():
        return None
    path = workspace / 'local/camera-fix.json'
    if not path.exists() and not path.is_symlink():
        return None
    expected = {'revision': 3, 'module': parrot.MODULE_ID, 'package': parrot.PACKAGE,
                'apk_sha256': parrot.APK_SHA256, 'runtime_sha256': parrot.RUNTIME_SHA256,
                'source_sha256': parrot.BRIDGE_SOURCE_SHA256, 'sha256': parrot.BRIDGE_SHA256,
                'experimental': True}
    try:
        if (not path.is_file() or path.is_symlink() or path.stat().st_nlink != 1
                or json.loads(path.read_text()) != expected):
            return None
        recipe = next(item for item in catalog if item['feature'] == 'parrot-camera'
                      and item['apk_sha256'] == parrot.APK_SHA256)
        paths = root(config, 'pm path ' + parrot.PACKAGE).splitlines()
        if len(paths) != 1 or not paths[0].startswith('package:/data/app/'):
            return None
        apk = paths[0][len('package:'):]
        info = root(config, 'dumpsys package ' + parrot.PACKAGE)
        directories = re.findall(r'^\s*nativeLibraryDir=(\S+)\s*$', info, re.M)
        if len(set(directories)) != 1:
            return None
        native = directories[0]
        if not _path(apk) or not _path(native) or native != str(Path(apk).parent / 'lib/arm64'):
            return None
        checks = [_file_checks('', apk.lstrip('/'), [recipe['apk_sha256']], root_owned=False)]
        checks += [_file_checks('', name.lstrip('/'), [checksum], root_owned=False)
                   for name, checksum in recipe.get('system_libraries', {}).items()]
        checks += [_file_checks('', (native + '/' + name).lstrip('/'), [checksum], root_owned=False)
                   for name, checksum in recipe.get('native_libraries', {}).items()]
        root(config, '\n'.join(checks))
        return {'path': str(path), 'sha256': sha256(path)}
    except (RuntimeError, OSError, subprocess.SubprocessError, ValueError, KeyError, StopIteration):
        return None


def plan(config, catalog=None, workspace=None):
    """Read-only authentication and preference import plan; no guest writes."""
    if catalog is None:
        from app_compat_producers import recipes
        catalog = recipes()
    registry = records()
    workspace = Path(workspace or ROOT)
    features = {item['feature'] for item in catalog}
    if any(not _word(feature) for feature in features):
        raise RuntimeError('Invalid application feature identity in migration catalog.')
    preferences = {feature: _preference(config, feature) for feature in features}
    result = {'choices': {}, 'origins': {}, 'modules': [], 'preserved': [],
              'catalog': catalog, 'workspace': str(workspace)}
    observed = {}
    for identifier, feature in MODULES.items():
        if feature not in features:
            continue
        try:
            if _pending(config, identifier):
                result['preserved'].append({'module': identifier, 'reason': 'pending'})
                continue
            module = _inspect(config, '/data/adb/modules/' + identifier, identifier)
            if module is None:
                continue
            saved = json.loads(root(config, f'cat {module["directory"]}/manifest.json'))
            authenticated = None
            for index, record in enumerate(registry):
                if record['id'] != identifier or record['manifest'] != saved:
                    continue
                try:
                    inode = _authenticate(config, module, index, registry)
                except (RuntimeError, OSError, subprocess.SubprocessError):
                    continue
                authenticated = {**module, 'id': identifier, 'feature': feature,
                                 'record': index, 'inode': inode}
                break
            if authenticated is None:
                raise RuntimeError('Unknown historical application controls or payloads.')
            enabled = not authenticated['flags']
            observed.setdefault(feature, []).append((enabled, authenticated))
            if enabled:
                result['modules'].append(authenticated)
            else:
                result['preserved'].append({'module': identifier, 'reason': authenticated['flags']})
        except (RuntimeError, OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
            result['preserved'].append({'module': identifier, 'reason': 'unknown-or-custom'})
    for feature, owners in observed.items():
        if preferences[feature] is not None:
            continue
        # Conflicting historical owners preserve the explicit off choice.
        selected = next((owner for enabled, owner in owners if not enabled), owners[0][1])
        result['choices'][feature] = all(enabled for enabled, _ in owners)
        result['origins'][feature] = {'module': selected}
    if 'parrot-camera' in features and preferences['parrot-camera'] is None and 'parrot-camera' not in observed:
        marker = _marker(config, workspace, catalog)
        if marker and not any(item['module'] == 'hyperos_avd_parrot_camera' for item in result['preserved']):
            result['choices']['parrot-camera'] = True
            result['origins']['parrot-camera'] = {'marker': marker}
    return result


def import_choices(config, planned):
    """Import absent choices with compare-and-publish; concurrent choices win."""
    registry, imported = records(), []
    for feature, enabled in sorted(planned['choices'].items()):
        if not _word(feature) or type(enabled) is not bool:
            raise RuntimeError('Invalid historical application feature import.')
        origin = planned['origins'][feature]
        if 'module' in origin:
            module = origin['module']
            if (module['feature'] != feature or registry[module['record']]['feature'] != feature
                    or enabled != (not module['flags'])):
                raise RuntimeError('Historical application choice differs from its authenticated lifecycle.')
            try:
                inode = _authenticate(config, module, module['record'], registry)
            except (RuntimeError, OSError, subprocess.SubprocessError):
                continue
            if inode != module['inode']:
                continue
            guard = _checks(module, registry[module['record']])
            guard += f'\n[ "$(stat -c %d:%i {module["directory"]})" = {inode} ] || exit 1'
        else:
            if feature != 'parrot-camera' or enabled is not True:
                raise RuntimeError('Invalid historical caller opt-in import.')
            current = _marker(config, Path(planned['workspace']), planned['catalog'])
            if current != origin.get('marker'):
                continue
            guard = ''
        value = 'on' if enabled else 'off'
        path = PREFERENCES + '/' + feature
        command = f'''set -e
{guard}
for directory in /data /data/adb /data/adb/hyperos_app_compat {PREFERENCES}; do
    [ ! -L "$directory" ] || exit 1
done
mkdir -p {PREFERENCES}
chmod 700 /data/adb/hyperos_app_compat {PREFERENCES}
if [ -e {path} ] || [ -L {path} ]; then
    [ -f {path} ] && [ ! -L {path} ] && [ "$(stat -c %h:%u {path})" = 1:0 ] || exit 1
    case "$(cat {path})" in on|off|auto) ;; *) exit 1 ;; esac
else
    temporary=$(mktemp {PREFERENCES}/.import.XXXXXX)
    trap 'rm -f "$temporary"' EXIT
    printf '%s\\n' {value} > "$temporary"
    chmod 600 "$temporary"
    if ! ln "$temporary" {path}; then
        [ -f {path} ] && [ ! -L {path} ] && [ "$(stat -c %h:%u {path})" = 1:0 ] || exit 1
        case "$(cat {path})" in on|off|auto) ;; *) exit 1 ;; esac
    fi
    rm "$temporary"
    trap - EXIT
fi'''
        root(config, command)
        imported.append(feature)
    return {'imported': imported}


def _active_owner(config, expected, catalog):
    from app_compat_catalog import MODULE_ID, catalog_files
    from apply_app_compat import _saved
    canonical = json.loads(catalog_files(catalog)['manifest.json'])
    if expected != canonical or _pending(config, MODULE_ID):
        return None
    # This is the authenticated current universal owner, not an old private
    # bridge. Admit only its source-canonical revision before full asset checks.
    active = _inspect(config, '/data/adb/modules/' + MODULE_ID, MODULE_ID,
                      allowed_revisions=(str(expected['revision']),))
    if not active or active['flags']:
        return None
    _saved(config, active, expected)
    return active


def retire(config, planned, expected_active_manifest):
    """Archive only exact enabled old owners after the common owner activates."""
    active = _active_owner(config, expected_active_manifest, planned['catalog'])
    if active is None:
        return {'retired': [], 'reboot_required': False, 'deferred': True}
    registry, retired = records(), []
    for module in planned['modules']:
        if module['flags']:
            continue
        try:
            current = _inspect(config, module['directory'], module['id'])
            if current is None or current['flags'] or _pending(config, module['id']):
                continue
            inode = _authenticate(config, current, module['record'], registry)
            if inode != module['inode']:
                continue
        except (RuntimeError, OSError, subprocess.SubprocessError):
            continue
        destination = ARCHIVE + '/' + module['id'] + '-' + inode.replace(':', '-')
        checked_parents = set()
        guard = _checks(current, registry[module['record']], checked_parents=checked_parents)
        guard += f'\n[ "$(stat -c %d:%i {module["directory"]})" = {inode} ] || exit 1'
        # Recheck the new owner immediately before the inode-preserving rename.
        current_owner = _active_owner(config, expected_active_manifest, planned['catalog'])
        if current_owner is None:
            continue
        from app_compat_catalog import canonical, catalog_files
        controls = catalog_files(planned['catalog'])
        hashes = {name: checksum for name, checksum in expected_active_manifest['files_sha256'].items()
                  if name != 'customize.sh'}
        hashes['manifest.json'] = hashlib.sha256(canonical(expected_active_manifest)).hexdigest()
        checksum_list = ''.join(checksum + '  ' + name + '\n' for name, checksum in sorted(hashes.items()))
        hashes['SHA256SUMS'] = hashlib.sha256(checksum_list.encode()).hexdigest()
        active_checks = _checks(current_owner, {'id': current_owner['directory'].rsplit('/', 1)[1],
                               'properties': controls['module.prop'].decode(), 'hashes': hashes,
                               'optional_hashes': {'customize.sh': expected_active_manifest['files_sha256']['customize.sh']}},
                                checked_parents=checked_parents)
        root(config, f'''set -e
{guard}
for directory in /data /data/adb /data/adb/hyperos_app_compat {ARCHIVE}; do
    [ ! -L "$directory" ] || exit 1
done
mkdir -p {ARCHIVE}
chmod 700 {ARCHIVE}
[ "$(stat -c %u {ARCHIVE})" = 0 ] || exit 1
[ ! -e {destination} ] && [ ! -L {destination} ] || exit 1
[ ! -e /data/adb/modules_update/{active['directory'].rsplit('/', 1)[1]} ] && \
    [ ! -L /data/adb/modules_update/{active['directory'].rsplit('/', 1)[1]} ] || exit 1
[ ! -e {active['directory']}/disable ] && [ ! -L {active['directory']}/disable ] || exit 1
[ ! -e {active['directory']}/remove ] && [ ! -L {active['directory']}/remove ] || exit 1
{active_checks}
[ ! -e {module['directory']}/disable ] && [ ! -L {module['directory']}/disable ] || exit 1
[ ! -e {module['directory']}/remove ] && [ ! -L {module['directory']}/remove ] || exit 1
[ "$(stat -c %d:%i {module['directory']})" = {inode} ] || exit 1
mv {module['directory']} {destination}''')
        retired.append(module['id'])
    return {'retired': retired, 'reboot_required': bool(retired)}
