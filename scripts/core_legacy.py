"""Retire only fully audited legacy owners; never invoke uninstall hooks."""
import json
import re
import shlex

from core_platform import STATE, digest, parent_guard, read_record

ROLES = {
    'hyperos_avd_flutter_render': ['flutter'],
    'hyperos_avd_navigation': ['navigation', 'identity', 'serial', 'thermal'],
    'hyperos_avd_assistant_mgl': ['assistant'],
    'hyperos_avd_boot_services': ['boot-services'],
    'hyperos_avd_rear_display': ['rear-wake'],
}
LOGS = {'flutter-fix.log', 'render-fix.log', 'navigation-fix.log', 'navigation.log', 'assistant-fix.log', 'startup.log',
        'wake.log', 'daemon.pid', 'startup.guard', 'startup.lock/pid', 'startup.lock/boot'}


# Frozen public r4 producer: apply_flutter_fix.py SHA-256
# c4df0c95d91a46aa0be7be9593ca7af4fe84c71eef56d9e154da05dde9c1235c.
# These old hooks are authenticated for retirement only, never executed.
FLUTTER_V7_RECORDS = {
    ('official-hongkong-ota', 'OS4.0.18.0.XFRCNXM',
     '7f88d7f4d9a464fdd56fb255642c9d300f28f50272ccf5fd31f082c1a52e17f4'):
        {'hook_sha256': '416cb8720a6b7b9e075d0fa607cf7bf42f5c2113bbb31aeb0ce61ca86f878cc0',
         'system_after_sha256': '439bb47881f64431ba43dc4de5788e7f0a3a0c4e78102b47cc8f0daa6229b91b'},
}


def _body(values, name, content):
    values[name] = {digest(content)}


def _hooks(values, scripts):
    for name, (reviewed, current) in scripts.items():
        reviewed = (reviewed,) if isinstance(reviewed, str) else reviewed
        values[name] = {digest(item) for item in (*reviewed, current)}


def _phone(manifest):
    from phone_profile import profile
    version = manifest.get('hyperos') or manifest.get('firmware', {}).get('incremental', '').removeprefix('OS')
    selected = profile(version)
    return selected


def reviewed_assets(module_id, manifest, text):
    """Return exact approved content hashes, never hashes authorized by a receipt alone."""
    values = {'manifest.json': {digest(text)}}
    if module_id == 'hyperos_avd_navigation':
        import apply_navigation_fix as nav
        from core_platform import serial_record
        if (manifest.get('revision') != nav.REVISION or set(manifest) -
                {'revision', 'hyperos', 'incremental', 'property', 'value', 'component', 'animation_backend',
                 'sf_animation', 'phone_identity', 'serial_number', 'aot', 'watchdog', 'deadline_status'}):
            raise ValueError('Unreviewed navigation receipt schema.')
        selected = _phone(manifest)
        serial_record(manifest['serial_number'])
        identity = dict(selected['properties'], **{key: manifest['serial_number']
                        for key in ('ro.serialno', 'ro.boot.serialno', 'ro.ril.oem.psno')})
        expected = {'incremental': selected['incremental'], 'property': nav.PROPERTY,
                    'value': 'com.miui.home', 'component': nav.COMPONENT, 'animation_backend': 'stock-sf',
                    'sf_animation': True, 'phone_identity': identity}
        if any(manifest.get(key) != value for key, value in expected.items()):
            raise ValueError('Unreviewed navigation identity.')
        _hooks(values, nav.hook_versions(selected))
        _body(values, 'system.prop', 'ro.miui.product.home=com.miui.home\npersist.miui.home_sf_anim=true\n')
        _body(values, 'identity.prop', ''.join(key + '=' + value + '\n' for key, value in identity.items()))
        _body(values, 'module.prop', f'id={module_id}\nname=HyperOS AVD native Quickstep\nversion={nav.REVISION}\nversionCode={nav.REVISION}\nauthor=HyperOS-AVD\ndescription=Original phone and launcher identity, persistent Xiaomi-style simulated serial and stock SF transitions\n')
        for kind, entry, stem, before, after in (
                ('aot', 'libapp.so', 'launcher-aot', nav.AOT_BEFORE, nav.AOT_AFTER),
                ('watchdog', 'libapp_launcher.so', 'launcher-watchdog', nav.WATCHDOG_BEFORE, nav.WATCHDOG_AFTER)):
            item = manifest.get(kind)
            if item is None:
                continue
            if (set(item) != {'apk', 'target', 'before', 'after'} or item['before'] != before or item['after'] != after
                    or not re.fullmatch(r'/data/app/[A-Za-z0-9_=+./~-]+/base\.apk', item['apk'])
                    or item['target'] != item['apk'].rsplit('/', 1)[0] + '/lib/arm64/' + entry):
                raise ValueError('Unreviewed navigation native payload.')
            patched, original = stem + '.so', stem + '.original.so'
            values[patched], values[original] = {after}, {before}
            config = {'HOME_APK': item['apk'], 'HOME_NATIVE': item['target'], 'APK_SHA256': nav.APK_SHA256,
                      'AOT_BEFORE': before, 'AOT_AFTER': after, 'PATCHED_NAME': patched, 'ORIGINAL_NAME': original}
            _body(values, kind + '.conf', ''.join(key + '=' + shlex.quote(value) + '\n' for key, value in config.items()))
    elif module_id == 'hyperos_avd_flutter_render':
        import apply_flutter_fix as flutter
        from native_patch_catalog import catalog
        revision = manifest.get('revision')
        keys = {'revision', 'firmware', 'system', 'apks', 'packages', 'apk_hashes', 'startup_script_sha256'}
        if type(revision) is not int:
            raise ValueError('Unreviewed Flutter receipt schema.')
        if revision == flutter.REVISION:
            keys.add('skipped_apks')
        elif revision != 7:
            raise ValueError('Unreviewed Flutter receipt schema.')
        if set(manifest) != keys:
            raise ValueError('Unreviewed Flutter receipt schema.')
        firmware = manifest['firmware']
        if not isinstance(firmware, dict):
            raise ValueError('Unreviewed Flutter firmware provenance.')
        source = firmware.get('source')
        selected = _phone(manifest) if source == 'official-hongkong-ota' else None
        if source not in ('official-hongkong-ota', 'official-yingtian-ota'):
            raise ValueError('Unreviewed Flutter source.')
        if source == 'official-yingtian-ota' and firmware.get('incremental') != 'OS4.0.15.0.XBMCNXM':
            raise ValueError('Unreviewed Flutter tablet profile.')
        historical_hook = None
        if revision == 7:
            historical = FLUTTER_V7_RECORDS.get((source, firmware.get('incremental'),
                                                 firmware.get('shared_input_sha256')))
            if historical is None or set(firmware) != {'source', 'incremental', 'shared_input_sha256'}:
                raise ValueError('Unreviewed historical Flutter firmware provenance.')
            historical_hook = historical['hook_sha256']
            if (not isinstance(manifest['packages'], dict) or not isinstance(manifest['apk_hashes'], dict)
                    or set(manifest['packages']) != set(flutter.PACKAGES)
                    or set(manifest['apk_hashes']) != set(flutter.PACKAGES)
                    or not isinstance(manifest['apks'], list)):
                raise ValueError('Unreviewed historical Flutter package inventory.')
        edges = {(item['before'], output) for item in catalog()['profiles'] if item['feature'] == 'flutter'
                 for output in (item['after'], *item['legacy'])}
        system = manifest['system']
        if (not isinstance(system, dict) or set(system) != {'target', 'before', 'after'}
                or system['target'] != flutter.SYSTEM_LIB or (system['before'], system['after']) not in edges):
            raise ValueError('Unreviewed Flutter system payload.')
        if historical_hook is not None and (system['before'] != firmware['shared_input_sha256']
                                           or system['after'] != historical['system_after_sha256']):
            raise ValueError('Historical Flutter system payload differs from its firmware provenance.')
        values['flutter.so'] = {system['after']}
        _body(values, 'targets.conf', ''.join(key + '=' + shlex.quote(value) + '\n' for key, value in
                                             {'SYSTEM_BEFORE': system['before'], 'SYSTEM_AFTER': system['after']}.items()))
        seen = set()
        for item in manifest['apks']:
            if historical_hook is not None:
                required = {'package', 'payload', 'apk_target', 'target', 'before', 'after', 'apk_sha256'}
                if (not isinstance(item, dict) or set(item) not in (required, required | {'external'})
                        or 'external' in item and item['external'] is not True
                        or item.get('package') in seen
                        or item.get('apk_target') != manifest['packages'].get(item.get('package'))
                        or item.get('apk_sha256') != manifest['apk_hashes'].get(item.get('package'))
                        or not isinstance(item.get('apk_sha256'), str)
                        or not re.fullmatch('[0-9a-f]{64}', item['apk_sha256'])):
                    raise ValueError('Unreviewed historical Flutter APK receipt.')
                seen.add(item['package'])
            if (not isinstance(item, dict) or item.get('package') not in flutter.PACKAGES
                    or item.get('payload') != item['package'] + '.so'
                    or (item.get('before'), item.get('after')) not in edges
                    or item.get('target') != item['apk_target'].rsplit('/', 1)[0] + '/lib/arm64/libhyper_os_flutter.so'
                    and not (source == flutter.PAD_SOURCE and item.get('target') == flutter.PAD_WEATHER_ENGINE)):
                raise ValueError('An APK-overlay or unreviewed Flutter payload is preserved.')
            values[item['payload']] = {item['after']}
            if item.get('external'):
                values[item['payload'] + '.original'] = {item['before']}
        _body(values, 'apks.conf', ''.join('|'.join(item[key] for key in
              ('package', 'payload', 'apk_target', 'target', 'before', 'after', 'apk_sha256')) + '\n' for item in manifest['apks']))
        if historical_hook is None:
            _hooks(values, flutter.hook_versions(source, firmware))
        else:
            values['post-fs-data.sh'] = values['service.sh'] = {historical_hook}
        if manifest['startup_script_sha256'] not in values['service.sh']:
            raise ValueError('Unreviewed Flutter startup receipt.')
        _body(values, 'module.prop', f'id={module_id}\nname=HyperOS AVD Flutter render fix\nversion={revision}\nversionCode={revision}\nauthor=HyperOS-AVD\ndescription=Native depth, Float16, storage alignment and dispersion shadow fix for the official ARM64 AVD\n')
    elif module_id == 'hyperos_avd_assistant_mgl':
        import apply_assistant_fix as assistant
        import patch_assistant as patch
        from phone_profile import ARCHIVES, profile
        profiles = [(patch.PHONE_SOURCE, None), (patch.PAD_SOURCE, None),
                    *((patch.PHONE_SOURCE, profile(version)) for version in ARCHIVES)]
        matching = [(source, firmware) for source, firmware in profiles if patch.profile(source, firmware) == manifest]
        if not matching:
            raise ValueError('Unreviewed XiaoAI payload receipt.')
        # The reviewed Phone17 module can retain its original payload/receipt
        # after the Phone18 image bakes the library and refreshes only hooks.
        hook_profiles = profiles if manifest['apk_sha256'] == patch.APK_SHA256 else matching
        for name in ('post-fs-data.sh', 'service.sh'):
            values[name] = {digest(hook(source, firmware)) for source, firmware in hook_profiles
                            for hook in (assistant.legacy_boot_script, assistant.boot_script)}
        _body(values, 'module.prop', 'id=hyperos_avd_assistant_mgl\nname=HyperOS AVD XiaoAI MGL fix\nversion=1\nversionCode=1\nauthor=HyperOS-AVD\ndescription=Original wakeup light effect with GLSL 300 and EGL alpha compatibility\n')
        values['payload/VoiceAssistAndroidT.apk'] = {manifest['apk_sha256']}
        values['payload/lib/arm64/libmglnative2.so'] = {patch.AFTER}
    elif module_id == 'hyperos_avd_boot_services':
        import apply_boot_service_fix as boot
        if manifest != boot.receipt():
            raise ValueError('Unreviewed boot-service framework payloads.')
        values = {name: {checksum} for name, checksum in boot._module_asset_hashes().items()}
        for name in ('post-fs-data.sh', 'service.sh'):
            values[name].add(digest(boot.boot_script()[0]))
        values['check-kernel-services.sh'] = set(boot.KERNEL_SCRIPT_HASHES)
        values['uninstall.sh'] = {digest(boot.UNINSTALL_SCRIPT)}
        values['uninstall.sh'].update(boot.LEGACY_UNINSTALL_SHA256S)
    elif module_id == 'hyperos_avd_rear_display':
        import apply_rear_display_fix as rear
        if (set(manifest) != {'schema', 'revision', 'module_id', 'firmware', 'marker', 'wake'}
                or manifest.get('schema') != 1 or manifest.get('revision') != rear.REVISION
                or manifest.get('module_id') != module_id or manifest.get('wake') != rear.EXPECTED_WAKE_MANIFEST
                or manifest['marker'].get('path') != '/product/etc/hyperos-avd-rear-display.json'
                or not re.fullmatch('[0-9a-f]{64}', manifest['marker'].get('sha256', ''))):
            raise ValueError('Unreviewed rear wake contract.')
        _body(values, 'module.prop', rear.MODULE_PROP)
        values['service.sh'] = {digest(rear.legacy_boot_script(manifest)), digest(rear.boot_script(manifest))}
        _body(values, 'skip_mount', b'')
    else:
        raise ValueError('Unknown legacy Core owner.')
    return values


def snapshot_script(directory):
    """Inspect complete regular files and metadata without following any link."""
    return parent_guard(directory.rsplit('/', 1)[0]) + f'''BB=/data/adb/ksu/bin/busybox
[ -d {directory} ] && [ ! -L {directory} ] || exit 1
[ "$("$BB" stat -c %u {directory})" = 0 ] || exit 1
[ -z "$("$BB" find {directory} -type l -print)" ] || exit 1
"$BB" find {directory} -mindepth 1 -type d -print | while IFS= read -r child; do
    printf 'directory|%s\\n' "${{child#{directory}/}}"
done
"$BB" find {directory} -type f -print | while IFS= read -r file; do
    if [ "$("$BB" stat -c %u "$file")" != 0 ] || [ "$("$BB" stat -c %h "$file")" != 1 ]; then
        printf 'invalid-metadata\\n'; exit 1
    fi
    printf '%s|' "$("$BB" sha256sum "$file" | "$BB" cut -d ' ' -f 1)"
    printf '%s\\n' "${{file#{directory}/}}"
done | "$BB" sort'''


def inspect_legacy(root, config, context=None):
    result = []
    for module_id, features in ROLES.items():
        directory = '/data/adb/modules/' + module_id
        pending = '/data/adb/modules_update/' + module_id
        lifecycle = root(config, f'''if [ -e {pending} ] || [ -L {pending} ]; then echo pending
elif [ -e {directory} ] || [ -L {directory} ]; then
    for flag in disable remove; do
        if [ -e {directory}/"$flag" ] || [ -L {directory}/"$flag" ]; then echo "$flag"; fi
    done
    echo present
else echo absent; fi''').splitlines()
        if lifecycle == ['absent']:
            continue
        item = {'id': module_id, 'directory': directory, 'features': features,
                'status': 'preserved', 'reason': 'legacy-lifecycle-choice'}
        result.append(item)
        if lifecycle != ['present']:
            continue
        try:
            if module_id == 'hyperos_avd_boot_services' and not (context and context.get('boot_services')):
                raise ValueError('Framework/APK image prerequisites are not proven; legacy boot owner retained.')
            if module_id == 'hyperos_avd_rear_display' and not (context and context.get('rear')):
                raise ValueError('Physical rear image prerequisites are not proven; legacy wake owner retained.')
            text = read_record(root, config, directory + '/manifest.json')
            if not text:
                raise ValueError('Missing legacy receipt.')
            manifest = json.loads(text)
            expected = reviewed_assets(module_id, manifest, text)
            snapshot = root(config, snapshot_script(directory))
            actual, directories = {}, set()
            for row in snapshot.splitlines():
                checksum, name = row.split('|')
                if checksum == 'directory':
                    directories.add(name); continue
                if (not re.fullmatch('[0-9a-f]{64}', checksum) or not re.fullmatch('[A-Za-z0-9_./-]+', name)
                        or '..' in name.split('/') or name in actual):
                    raise ValueError('Invalid legacy inventory.')
                actual[name] = checksum
            if not set(expected) <= actual.keys() or set(actual) - set(expected) - LOGS:
                raise ValueError('Unknown legacy file inventory.')
            allowed_directories = {'startup.lock'}
            for name in expected:
                while '/' in name:
                    name = name.rsplit('/', 1)[0]; allowed_directories.add(name)
            if not directories <= allowed_directories:
                raise ValueError('Unknown legacy directory inventory.')
            if any(actual[name] not in hashes for name, hashes in expected.items()):
                raise ValueError('Unreviewed legacy file contents.')
            item.update(status='audited', reason='exact-reviewed-owner', snapshot=snapshot,
                        serial_number=manifest.get('serial_number'))
        except (RuntimeError, ValueError, KeyError, TypeError, AttributeError) as error:
            item['reason'] = str(error)
    return result


def retire_legacy(root, config, item, guard):
    """Move a verified old owner intact; no uninstall, bind, delete, or rewrite."""
    directory = item['directory']
    archive = STATE + '/retired/' + item['id'] + '-' + digest(item['snapshot'])[:16]
    pending = '/data/adb/modules_update/' + item['id']
    commands = guard + parent_guard(archive.rsplit('/', 1)[0])
    commands += f'[ ! -e {pending} ] && [ ! -L {pending} ] || exit 1\n'
    for flag in ('disable', 'remove'):
        commands += f'[ ! -e {directory}/{flag} ] && [ ! -L {directory}/{flag} ] || exit 1\n'
    commands += '[ "$(' + snapshot_script(directory) + ')" = ' + shlex.quote(item['snapshot']) + ' ] || exit 1\n'
    commands += f'''[ ! -e {archive} ] && [ ! -L {archive} ] || exit 1
mkdir -p {STATE}/retired
chmod 700 {STATE}/retired
mv {directory} {archive}
'''
    root(config, 'set -e\n' + commands)
    item['archive'] = archive
    item.pop('snapshot', None)


def migrate_global_hooks(root, config, guard, serial):
    """Archive exact old global hooks only when Core can take their roles."""
    import os4_pad
    import os4_defaults
    from phone_profile import ARCHIVES, profile
    refreshed = {digest(os4_defaults.REFRESH_WRAPPER)}
    refreshed.update(digest(os4_defaults.refresh_wrapper(profile(version))) for version in ARCHIVES)
    refreshed.add(digest(os4_pad.refresh_script(os4_defaults.REFRESH_WRAPPER)))
    candidates = [(os4_defaults.REFRESH_SERVICE, ['refresh'], refreshed),
                  (os4_pad.THERMAL_EARLY, ['thermal', 'identity', 'hwui'],
                   {digest(os4_pad.thermal_profile_script()), os4_pad.LEGACY_THERMAL_HWUI_SHA256})]
    pad = read_record(root, config, os4_pad.SERIAL_FILE)
    allowed = set()
    if pad is not None:
        try:
            state = json.loads(pad)
            expected = digest(os4_pad._serial_script(state, state['hyperos']))
            allowed = {expected} if serial.get('status') == 'ready' and serial.get('serial_number') == state['serial_number'] else set()
        except (ValueError, RuntimeError, KeyError, TypeError):
            allowed = set()
    candidates.append((os4_pad.SERIAL_EARLY, ['serial'], allowed))
    result = []
    for path, features, hashes in candidates:
        item = {'path': path, 'features': features, 'status': 'preserved'}
        try:
            data = read_record(root, config, path)
            if data is None:
                continue
            result.append(item)
            checksum = digest(data)
            if checksum not in hashes:
                item['reason'] = 'unreviewed-global-hook'; continue
            archive = STATE + '/retired/' + path.rsplit('/', 1)[1] + '-' + checksum[:16]
            root(config, 'set -e\n' + guard + parent_guard(archive.rsplit('/', 1)[0]) + f'''
[ -f {path} ] && [ ! -L {path} ] || exit 1
[ "$("$BB" stat -c %u {path})" = 0 ] && [ "$("$BB" stat -c %h {path})" = 1 ] || exit 1
[ "$("$BB" sha256sum {path} | "$BB" cut -d ' ' -f 1)" = {checksum} ] || exit 1
[ ! -e {archive} ] && [ ! -L {archive} ] || exit 1
mkdir -p {STATE}/retired
chmod 700 {STATE}/retired
mv {path} {archive}
''')
            item.update(status='retired', archive=archive)
        except RuntimeError:
            if item not in result:
                result.append(item)
            item['reason'] = 'aliased-or-changed-global-hook'
    return result
