#!/usr/bin/env python3
"""Install version-guarded KernelSU overlays on the official OS4 AVD only."""
import argparse
import json
from pathlib import Path
import shlex
import subprocess
import zipfile
import hashlib

from module_lifecycle import (preserved as lifecycle_preserved, mutation_guard,
                              enable_owned, guarded_hook, verify_hooks, refresh_hooks,
                              refresh_receipt, UnreviewedHook)
from common import ROOT, adb, runtime, sha256
from patch_flutter import digest, patch, profile

MODULE_ID = 'hyperos_avd_flutter_render'
MODULE = '/data/adb/modules/' + MODULE_ID
SYSTEM_LIB = '/system_ext/lib64/libhyper_os_flutter.so'
ENGINE_ENTRY = 'lib/arm64-v8a/libhyper_os_flutter.so'
PACKAGES = ('com.miui.home', 'com.miui.weather2')
REVISION = 8
EMPTY = hashlib.sha256(b'').hexdigest()
PAD_SOURCE = 'official-yingtian-ota'
PAD_WEATHER_APK = '/product/data-app/MIUIWeather/MIUIWeather.apk'
PAD_WEATHER_APK_SHA256 = '50f5dd5a06818f9613bf92ae2861beb58426895e6ae86364ed472e4c0aec5652'
PAD_WEATHER_ENGINE = '/data/app-lib/MIUIWeather/arm64/libhyper_os_flutter.so'
PAD_WEATHER_BEFORE = 'd67e5c634800a97cd853fd425ded4752c43c2c879fe7269c8b9ffab6c5104836'
PAD_WEATHER_AFTER = '109fcc92d722321481e62d0bc5488b09d2e5ecbe3241b555d86fe681ddd84083'

# Use KernelSU BusyBox: toybox can resolve a file bind back to its source.
BOOT_SCRIPT = r'''#!/system/bin/sh
MODDIR=${0%/*}
. "$MODDIR/targets.conf"
BB=/data/adb/ksu/bin/busybox
LOG="$MODDIR/render-fix.log"
log() { echo "$(date +%s) $*" >> "$LOG"; }
bind_target() {
    local pid=$1 source=$2 target=$3 before=$4 after=$5 actual mounted source_hash
    actual=$(nsenter -t "$pid" -m -- "$BB" sha256sum "$target" 2>/dev/null | "$BB" cut -d ' ' -f 1)
    [ "$actual" = "$after" ] && return 0
    source_hash=$(nsenter -t "$pid" -m -- "$BB" sha256sum "$source" 2>/dev/null | "$BB" cut -d ' ' -f 1)
    [ "$source_hash" = "$after" ] || { log "Invalid overlay: $source ($source_hash) in $pid"; return 1; }
    if [ -z "$actual" ] && [ -f "$source.original" ]; then
        case "$target" in /data/app/*/lib/arm64/libhyper_os_flutter.so) ;; *) return 1 ;; esac
        [ "$(sha256sum "$source.original" | cut -d ' ' -f 1)" = "$before" ] || return 1
        nsenter -t "$pid" -m -- sh -c 'mkdir -p "${1%/*}"; cp "$2" "$1"; chmod 644 "$1"; chcon u:object_r:apk_data_file:s0 "$1"' sh "$target" "$source.original" || return 1
        actual=$before
    fi
    [ "$actual" = "$before" ] || { log "Refused target: $target ($actual)"; return 1; }
    nsenter -t "$pid" -m -- "$BB" mount -o bind "$source" "$target" || return 1
    mounted=$(nsenter -t "$pid" -m -- "$BB" sha256sum "$target" | "$BB" cut -d ' ' -f 1)
    [ "$mounted" = "$after" ] || return 1
    changed=1
    log "Applied $target in $pid"
}
[ -f "$MODDIR/disable" ] && exit 0
[ -x "$BB" ] || exit 1
changed=0
bind_target 1 "$MODDIR/flutter.so" /system_ext/lib64/libhyper_os_flutter.so "$SYSTEM_BEFORE" "$SYSTEM_AFTER" || exit 1
if [ "${0##*/}" = post-fs-data.sh ]; then
    # PackageManager is not available yet. Verify the saved signed APK itself
    # before mounting native libraries needed by the first launcher process.
    while IFS='|' read -r package payload apk_target target before after apk_sha; do
        [ -n "$apk_sha" ] && [ -f "$apk_target" ] || continue
        [ "$(sha256sum "$apk_target" | cut -d ' ' -f 1)" = "$apk_sha" ] || continue
        bind_target 1 "$MODDIR/$payload" "$target" "$before" "$after" || log "Private engine skipped: $package; original code preserved."
    done < "$MODDIR/apks.conf"
    exit 0
fi
count=0
while [ "$(getprop sys.boot_completed)" != 1 ]; do
    count=$((count + 1)); [ "$count" -lt 300 ] || exit 1
    sleep 1
done
for pid in 1 $(getprop init.svc_debug_pid.hyos_spawner) $(pidof zygote64); do
    [ -d "/proc/$pid" ] || continue
    bind_target "$pid" "$MODDIR/flutter.so" /system_ext/lib64/libhyper_os_flutter.so "$SYSTEM_BEFORE" "$SYSTEM_AFTER" || exit 1
    while IFS='|' read -r package payload apk_target target before after apk_sha; do
        [ -n "$package" ] || continue
        current=$(pm path "$package" </dev/null | "$BB" sed -n 's/^package://p' | "$BB" head -n 1)
        current_sha=$(sha256sum "$apk_target" 2>/dev/null | "$BB" cut -d ' ' -f 1)
        if [ "$current" = "$apk_target" ] && { [ "$current_sha" = "$apk_sha" ] ||
                { [ "$target" = "$apk_target" ] && [ "$current_sha" = "$after" ]; }; }; then
            bind_target "$pid" "$MODDIR/$payload" "$target" "$before" "$after" || log "Private engine skipped: $package; original code preserved."
        else
            log "APK changed: $package; original code preserved. Native compatibility catalog reconciles supported updates."
        fi
    done < "$MODDIR/apks.conf"
done
if [ "$changed" = 1 ]; then
    am force-stop com.miui.home
    am force-stop com.miui.weather2
fi
exit 0
'''


def firmware_context(build):
    """Record the source engine and OTA identity independently of AVD names."""
    source = build['source']
    if source == 'official-hongkong-ota':
        from phone_profile import profile_from_build
        selected = profile_from_build(build)
        return {'source': source, 'incremental': selected['incremental'],
                'shared_input_sha256': selected['pins']['flutter']}
    if source == PAD_SOURCE:
        from os4_pad import PROFILE
        from patch_flutter import PROFILES
        matches = [checksum for checksum, entry in PROFILES.items()
                   if entry['name'] == 'tablet-yingtian']
        if len(matches) != 1:
            raise RuntimeError('Ambiguous tablet Flutter engine profile.')
        return {'source': source, 'incremental': PROFILE['hyperos'],
                'shared_input_sha256': matches[0]}
    raise RuntimeError('Unsupported Flutter firmware profile.')


def guarded_boot_script(script, source, firmware):
    """Prevent retained overlays from masking a newer firmware before PM scans."""
    if firmware is None:
        return script
    if firmware['source'] != source:
        raise RuntimeError('Flutter firmware guard source mismatch.')
    anchor = '. "$MODDIR/targets.conf"\n'
    if script.count(anchor) != 1:
        raise RuntimeError('Unexpected Flutter targets configuration guard.')
    guard = ('[ "$(getprop ro.boot.hardware)" = ranchu ] || exit 1\n'
             '[ "$(getprop ro.mi.os.version.incremental)" = ' +
             shlex.quote(firmware['incremental']) + ' ] || exit 0\n')
    script = script.replace(anchor, guard + anchor)
    if source == 'official-hongkong-ota':
        # A firmware fingerprint rescan may atomically extract factory Weather
        # libraries; mounting them before PackageManager prevents that rescan.
        early = '        [ -n "$apk_sha" ] && [ -f "$apk_target" ] || continue\n'
        if script.count(early) != 1:
            raise RuntimeError('Unexpected phone early APK loop.')
        script = script.replace(early, '        [ "$package" = com.miui.weather2 ] && continue\n' + early)
    # A baked previous patch is a verified upgrade input, not an unknown ELF.
    # Bind guards remain literal OTA/profile hashes rather than configurable
    # wildcards; data-app engines retain their existing exact-input checks.
    from patch_flutter import PROFILES
    original = firmware['shared_input_sha256']
    engine = PROFILES.get(original, {})
    if engine.get('glyph_raster_site') and engine.get('legacy'):
        check = '    [ "$actual" = "$before" ] || { log "Refused target: $target ($actual)"; return 1; }\n'
        if script.count(check) != 1:
            raise RuntimeError('Unexpected native input guard.')
        cases = '|'.join(shlex.quote(SYSTEM_LIB + '|' + original + '|' + engine['output'] + '|' + old)
                         for old in engine['legacy'])
        script = script.replace(check, '    if [ "$actual" != "$before" ]; then\n'
                                '        case "$target|$before|$after|$actual" in\n'
                                '            ' + cases + ') ;;\n'
                                '            *) log "Refused target: $target ($actual)"; return 1 ;;\n'
                                '        esac\n    fi\n')
    return script


def legacy_boot_script(source='official-hongkong-ota', firmware=None):
    """Reviewed pre-lifecycle hooks, used only to authenticate migrations."""
    if source == 'official-hongkong-ota':
        return guarded_boot_script(BOOT_SCRIPT, source, firmware)
    if source != PAD_SOURCE:
        raise RuntimeError('Unsupported Flutter firmware profile.')
    anchor = '    if [ -z "$actual" ] && [ -f "$source.original" ]; then\n'
    if BOOT_SCRIPT.count(anchor) != 1:
        raise RuntimeError('Unexpected Flutter original-library restoration block.')
    guard = f'''    if [ "$target" = {PAD_WEATHER_ENGINE} ]; then
        [ "$(getprop ro.boot.hardware)" = ranchu ] || return 1
        [ "$(getprop ro.product.device)" = yingtian ] || return 1
        [ "$(getprop ro.mi.os.version.incremental)" = OS4.0.15.0.XBMCNXM ] || return 1
        [ "$before" = {PAD_WEATHER_BEFORE} ] && [ "$after" = {PAD_WEATHER_AFTER} ] || return 1
        [ "$(nsenter -t "$pid" -m -- "$BB" sha256sum {PAD_WEATHER_APK} | "$BB" cut -d ' ' -f 1)" = {PAD_WEATHER_APK_SHA256} ] || return 1
        [ "$(nsenter -t "$pid" -m -- "$BB" sha256sum "$source" | "$BB" cut -d ' ' -f 1)" = {PAD_WEATHER_AFTER} ] || return 1
    fi
'''
    restoration = f'''    if [ "$target" = {PAD_WEATHER_ENGINE} ]; then
        if [ -z "$actual" ]; then
            nsenter -t "$pid" -m -- sh -c '
                set -e
                target=$1; original=$2; before=$3
                [ "$(sha256sum "$original" | cut -d " " -f 1)" = "$before" ] || exit 1
                [ ! -e "$target" ] && [ ! -L "$target" ] || exit 1
                for directory in /data/app-lib /data/app-lib/MIUIWeather /data/app-lib/MIUIWeather/arm64; do
                    [ ! -L "$directory" ] || exit 1
                    if [ -e "$directory" ]; then
                        [ -d "$directory" ] || exit 1
                    else
                        mkdir "$directory"
                        chmod 755 "$directory"
                        chcon u:object_r:apk_data_file:s0 "$directory"
                    fi
                done
                temporary="$target.hyperos-avd-original.$$"
                (set -C; cat "$original" > "$temporary") || exit 1
                trap \'rm -f "$temporary"\' EXIT
                chmod 644 "$temporary"
                chcon u:object_r:apk_data_file:s0 "$temporary"
                [ "$(sha256sum "$temporary" | cut -d " " -f 1)" = "$before" ] || exit 1
                ln "$temporary" "$target"
            ' sh "$target" "$source.original" "$before" || return 1
            actual=$before
        fi
    fi
'''
    # The inner shell uses single quotes, so quote its cleanup without closing it.
    restoration = restoration.replace('trap \'rm -f "$temporary"\' EXIT',
                                      'trap "rm -f \\"$temporary\\"" EXIT')
    early = '        [ -n "$apk_sha" ] && [ -f "$apk_target" ] || continue\n'
    if BOOT_SCRIPT.count(early) != 1:
        raise RuntimeError('Unexpected Flutter early APK loop.')
    # PackageManager may replace extracted factory libraries on a fingerprint
    # rescan. A file bind here prevents its atomic extraction from completing.
    # PackageInstaller can also replace a restored or updated Weather app's
    # extracted library directory. Defer both factory and /data/app layouts.
    deferred = '        [ "$package" = com.miui.weather2 ] && continue\n'
    already = '    [ "$actual" = "$after" ] && return 0\n'
    if BOOT_SCRIPT.count(already) != 1:
        raise RuntimeError('Unexpected Flutter existing-library guard.')
    script = BOOT_SCRIPT.replace(already, guard + already).replace(
        anchor, restoration + anchor).replace(early, deferred + early)
    return guarded_boot_script(script, source, firmware)


def boot_script(source='official-hongkong-ota', firmware=None):
    """Retain verified engine guards and restore only the audited factory Pad engine."""
    return guarded_hook(legacy_boot_script(source, firmware))


def reviewed_boot_scripts(source, firmware):
    """Enumerate exact generated hooks from the source-pinned OTA catalog."""
    contexts = [None, firmware]
    if source == 'official-hongkong-ota':
        from phone_profile import ARCHIVES
        contexts.extend(firmware_context({'source': source, 'hyperos': version,
                                          'archive_sha256': checksum})
                        for version, checksum in ARCHIVES.items())
    elif source == PAD_SOURCE:
        contexts.append(firmware_context({'source': source}))
    else:
        raise RuntimeError('Unsupported Flutter firmware profile.')
    return tuple(dict.fromkeys(script for context in contexts for script in
                               (legacy_boot_script(source, context), boot_script(source, context))))


def hook_versions(source, firmware):
    current = boot_script(source, firmware)
    reviewed = reviewed_boot_scripts(source, firmware)
    return {name: (reviewed, current) for name in ('post-fs-data.sh', 'service.sh')}


def root(config, command, **kwargs):
    result = adb(config, 'shell', 'su -W -c ' + shlex.quote('set -e\n' + command),
                 capture_output=True, text=True, timeout=60, **kwargs)
    if result.returncode:
        raise RuntimeError('Flutter overlay command failed: ' + result.stdout + result.stderr)
    return result.stdout.strip()


def official(config, sources=('official-hongkong-ota',)):
    """Require the registered owned instance and the verified OS4 profile."""
    from common import avd_home
    build = ROOT / 'local/build.json'
    saved = ROOT / 'local/runtime.json'
    registry = avd_home() / (config['name'] + '.ini')
    if not build.is_file() or json.loads(build.read_text()).get('source') not in sources:
        raise RuntimeError('This patch requires the official OS4 firmware profile.')
    expected = json.loads(saved.read_text()) if saved.is_file() else {}
    values = dict(line.split('=', 1) for line in registry.read_text().splitlines() if '=' in line) if registry.is_file() else {}
    if (expected.get('name') != config['name'] or expected.get('port') != config['port']
            or Path(values.get('path', '/nonexistent')).expanduser().resolve()
               != ROOT / 'avd' / (config['name'] + '.avd')):
        raise RuntimeError('This patch is restricted to registered owned OS4 instances.')
    actual = adb(config, 'shell', 'getprop ro.boot.qemu.avd_name',
                 capture_output=True, text=True, check=True, timeout=10).stdout.strip()
    if actual != config['name']:
        raise RuntimeError('Refused a different running AVD.')


def rewrite_apk(source, destination, engine):
    """Produce a runtime overlay; never install or resign the user's APK."""
    with zipfile.ZipFile(source) as archive, zipfile.ZipFile(destination, 'w') as output:
        entries = archive.infolist()
        if sum(entry.filename == ENGINE_ENTRY for entry in entries) != 1:
            raise RuntimeError('Expected exactly one embedded Flutter engine.')
        for entry in entries:
            output.writestr(entry, engine if entry.filename == ENGINE_ENTRY else archive.read(entry))


def refresh_scripts(config, source='official-hongkong-ota', firmware=None):
    refresh_hooks(root, config, MODULE, hook_versions(source, firmware))


def old_targets(manifest):
    if manifest.get('revision') == 1:
        return [{'target': SYSTEM_LIB, 'before': manifest['system_before'], 'after': manifest['system_after']},
                {'target': manifest['home_target'], 'before': manifest['home_before'], 'after': manifest['home_after']}]
    if manifest.get('revision') in (2, 3, 4, 5, 6, 7, REVISION):
        return [manifest['system'], *manifest['apks']]
    raise RuntimeError('Unknown existing Flutter overlay revision.')


def detach(config, manifest, preserve_paths=()):
    """Detach only binds whose source belongs to this module."""
    paths = [item['target'] for item in old_targets(manifest)]
    commands = []
    for path in paths:
        if any(c in path for c in '\n\r|"') or not (path.startswith(('/system_ext/', '/data/app/', '/product/'))
                or path == '/data/app-lib/MIUIWeather/arm64/libhyper_os_flutter.so'):
            raise RuntimeError('Invalid saved overlay target.')
        loop = (f'for attempt in 1 2 3 4; do '
                f"awk '$4 ~ /^\\/adb\\/modules\\/{MODULE_ID}\\// && $5 == \"{path}\" {{ found=1 }} END {{exit !found}}' /proc/self/mountinfo || break; "
                f'/data/adb/ksu/bin/busybox umount -l {shlex.quote(path)} || exit 1; done')
        commands.append('nsenter -t "$pid" -m -- sh -c ' + shlex.quote(loop))
    root(config, 'set -e\n' + mutation_guard(MODULE) + 'for pid in 1 $(getprop init.svc_debug_pid.hyos_spawner) $(pidof zygote64); do\n'
         '[ -d "/proc/$pid" ] || continue\n' + '\n'.join(commands) + '\ndone')
    for item in old_targets(manifest):
        if ((item.get('placeholder') or item.get('external')) and not item.get('preserve_original')
                and item['target'] not in preserve_paths):
            path = shlex.quote(item['target'])
            root(config, f'if [ -f {path} ] && [ "$(sha256sum {path} | cut -d " " -f 1)" = {item["before"]} ]; then rm {path}; fi')


def reusable_manifest(saved, targets, firmware, apk_hashes, system_hash, script_hash,
                      *, reviewed_script_hashes=()):
    """Paths alone do not prove that an OTA or app update kept its native code."""
    if (saved.get('revision') != REVISION or saved.get('firmware') != firmware
            or saved.get('packages') != targets
            or saved.get('startup_script_sha256') not in {script_hash, *reviewed_script_hashes}
            or set(saved.get('apk_hashes', {})) != set(targets)):
        return False
    system = saved.get('system', {})
    if (system.get('before') != firmware['shared_input_sha256']
            or system_hash not in (system.get('before'), system.get('after'))):
        return False
    for package, checksum in apk_hashes.items():
        acceptable = {saved['apk_hashes'][package]}
        acceptable.update(item['after'] for item in saved.get('apks', ())
                          if item.get('package') == package and item.get('target') == targets[package])
        if checksum not in acceptable:
            return False
    return True


def install(config, enable=False, sources=('official-hongkong-ota',)):
    official(config, sources=sources)
    lifecycle = lifecycle_preserved(root, config, MODULE, enable=enable)
    if lifecycle:
        return lifecycle
    build = json.loads((ROOT / 'local/build.json').read_text())
    source = build['source']
    firmware = firmware_context(build)
    startup_script = boot_script(source, firmware)
    script_hash = digest(startup_script.encode())
    if root(config, 'id -u') != '0' or root(config, 'getprop sys.boot_completed') != '1':
        raise RuntimeError('Wait for boot completion and KernelSU root first.')
    if root(config, 'getprop ro.mi.os.version.incremental') != firmware['incremental']:
        raise RuntimeError('Flutter overlay refused a different OTA version.')
    targets, skipped = {}, {}
    for pkg in PACKAGES:
        paths = root(config, 'pm path ' + pkg).splitlines()
        if len(paths) != 1 or not paths[0].startswith('package:'):
            skipped[pkg] = 'active base APK unavailable or split; shared engine remains active'
        else:
            targets[pkg] = paths[0].removeprefix('package:')
    old_text = root(config, f'if [ -f {MODULE}/manifest.json ]; then cat {MODULE}/manifest.json; fi')
    old = json.loads(old_text) if old_text else None
    if old:
        old_targets(old)  # Validate revision before changing anything.
        try:
            verify_hooks(root, config, MODULE, hook_versions(source, firmware), allow_disabled=enable)
        except UnreviewedHook as error:
            print(str(error) + '; existing Flutter module retained.', flush=True)
            return {'preserved': True, 'reason': 'unreviewed-startup-hook', 'hook': error.name}
        if enable:
            enable_owned(root, config, MODULE)
        pad_legacy = source == PAD_SOURCE and any(
            i.get('package') == 'com.miui.weather2' and i.get('apk_target') == PAD_WEATHER_APK
            and not (i.get('external') and i.get('preserve_original'))
            for i in old.get('apks', ()))
        reuse = False
        if not pad_legacy and old.get('revision') == REVISION and old.get('firmware') == firmware:
            current_apks = {package: root(config, 'sha256sum ' + shlex.quote(target)).split()[0]
                            for package, target in targets.items()}
            current_system = root(config, 'sha256sum ' + SYSTEM_LIB).split()[0]
            reviewed_hashes = {digest(legacy_boot_script(source, firmware).encode()),
                               digest(legacy_boot_script(source).encode()), digest(boot_script(source).encode())}
            reuse = reusable_manifest(old, targets, firmware, current_apks, current_system, script_hash,
                                      reviewed_script_hashes=reviewed_hashes)
        if reuse:
            for item in old_targets(old):
                current = root(config, 'if [ -f ' + shlex.quote(item['target']) + ' ]; then sha256sum ' + shlex.quote(item['target']) + '; fi')
                current = current.split()[0] if current else ''
                if not current and item.get('external'):
                    continue
                if current not in (item['before'], item['after']):
                    if item['target'] == SYSTEM_LIB:
                        raise RuntimeError('Overlay target changed unexpectedly: ' + item['target'])
                    reuse = False
                    break
        if reuse:
            if not all('apk_target' in i for i in old['apks']):
                raise RuntimeError('Overlay schema requires rebuilding this module.')
            refresh_scripts(config, source, firmware)
            if old.get('startup_script_sha256') != script_hash:
                old['startup_script_sha256'] = script_hash
                receipt = json.dumps(old, indent=2) + '\n'
                refresh_receipt(root, config, MODULE, old_text, receipt)
                (ROOT / 'local/flutter-render-fix.json').write_text(receipt)
            root(config, 'set -e\n' + mutation_guard(MODULE) + f'sh {MODULE}/service.sh')
            for pkg, reason in old.get('skipped_apks', {}).items():
                print('Private Flutter engine ' + pkg + ': ' + reason + '; original code retained.', flush=True)
            print('Native Flutter overlays are ready.', flush=True)
            return old
    folder = ROOT / 'work/flutter-render-fix'
    folder.mkdir(parents=True, exist_ok=True)
    original_lib = folder / 'system-current.so'
    adb(config, 'pull', SYSTEM_LIB, str(original_lib), check=True, capture_output=True, timeout=60)
    lib = original_lib.read_bytes()
    before, system_profile = profile(lib)
    if source == PAD_SOURCE:
        expected_engine = 'tablet-yingtian'
    else:
        from patch_flutter import PROFILES
        expected_engine = PROFILES.get(firmware['shared_input_sha256'], {}).get('name')
    if system_profile['name'] != expected_engine or before != firmware['shared_input_sha256']:
        raise RuntimeError('Unexpected system Flutter engine.')
    (folder / 'flutter.so').write_bytes(patch(lib))
    system = {'target': SYSTEM_LIB, 'before': before, 'after': system_profile['output']}
    apks = []
    apk_hashes = {}
    for pkg, target in targets.items():
        try:
            if any(c in target for c in '\n\r|') or not target.startswith(('/data/app/', '/system_ext/', '/product/')):
                raise RuntimeError('Unexpected APK location: ' + target)
            original = folder / (pkg + '-current.apk')
            adb(config, 'pull', target, str(original), check=True, capture_output=True, timeout=60)
            actual = sha256(original)
            apk_hashes[pkg] = actual
            factory_pad_weather = source == PAD_SOURCE and pkg == 'com.miui.weather2' and target == PAD_WEATHER_APK
            if factory_pad_weather and actual != PAD_WEATHER_APK_SHA256:
                raise RuntimeError('Unsupported factory tablet Weather APK; factory extraction was preserved')
            payload = pkg + '.apk'
            fixed = folder / payload
            with zipfile.ZipFile(original) as archive:
                if ENGINE_ENTRY not in archive.namelist():
                    skipped[pkg] = 'no private engine; patched shared system engine applies'
                    continue
                if archive.namelist().count(ENGINE_ENTRY) != 1:
                    raise RuntimeError('Ambiguous private Flutter engine entry')
                raw = archive.read(ENGINE_ENTRY)
                engine = patch(raw)
            native_target = str(Path(target).parent / 'lib/arm64/libhyper_os_flutter.so')
            if factory_pad_weather:
                # Factory Weather must wait until PM finishes its extraction.
                if digest(raw) != PAD_WEATHER_BEFORE or digest(engine) != PAD_WEATHER_AFTER:
                    raise RuntimeError('Unsupported factory tablet Weather engine')
                native_target = PAD_WEATHER_ENGINE
                current = root(config, 'if [ -e ' + shlex.quote(native_target) + ' ] || [ -L ' + shlex.quote(native_target) + ' ]; then\n'
                               '[ -f ' + shlex.quote(native_target) + ' ] && [ ! -L ' + shlex.quote(native_target) + ' ] || exit 1\n'
                               'sha256sum ' + shlex.quote(native_target) + '\nfi')
                if current and current.split()[0] not in (PAD_WEATHER_BEFORE, PAD_WEATHER_AFTER):
                    raise RuntimeError('Unrelated tablet Weather engine already exists')
                payload = pkg + '.so'
                (folder / payload).write_bytes(engine)
                (folder / (payload + '.original')).write_bytes(raw)
                apks.append({'package': pkg, 'apk_target': target, 'payload': payload,
                             'target': native_target, 'before': PAD_WEATHER_BEFORE,
                             'after': PAD_WEATHER_AFTER, 'external': True, 'preserve_original': True})
                continue
            if source == PAD_SOURCE and target.startswith('/product/data-app/'):
                raise RuntimeError('Unsupported tablet factory native layout; signed APK was preserved')
            extracted = root(config, 'if [ -f ' + shlex.quote(native_target) + ' ]; then echo yes; fi') == 'yes'
            previous_placeholder = any(i['target'] == native_target and (i.get('placeholder') or i.get('external')) for i in old_targets(old)) if old else False
            if target.startswith('/data/app/') and (not extracted or previous_placeholder):
                # Supply the known original engine; leave the signed APK intact.
                native_before, native_profile = profile(raw)
                payload = pkg + '.so'
                (folder / payload).write_bytes(engine)
                (folder / (payload + '.original')).write_bytes(raw)
                apks.append({'package': pkg, 'apk_target': target, 'payload': payload,
                             'target': native_target, 'before': native_before,
                             'after': native_profile['output'], 'external': True})
                continue
            if extracted:
                native = folder / (pkg + '-native-current.so')
                adb(config, 'pull', native_target, str(native), check=True, capture_output=True, timeout=60)
                native_raw = native.read_bytes()
                native_before, native_profile = profile(native_raw)
                if profile(raw)[0] != native_before:
                    raise RuntimeError('Extracted engine differs from its APK: ' + pkg)
                payload = pkg + '.so'
                fixed = folder / payload
                fixed.write_bytes(patch(native_raw))
                apks.append({'package': pkg, 'apk_target': target, 'payload': payload, 'target': native_target,
                             'before': native_before, 'after': native_profile['output']})
                continue
            if engine == raw:
                fixed.write_bytes(original.read_bytes())
            else:
                candidates = sorted((Path(config['sdk']) / 'build-tools').glob('*/zipalign'), reverse=True)
                if not candidates:
                    raise RuntimeError('Install Android SDK Build-Tools with zipalign for an APK overlay')
                unaligned = folder / (pkg + '-unaligned.apk')
                rewrite_apk(original, unaligned, engine)
                subprocess.run([str(candidates[0]), '-f', '-P', '16', '4', str(unaligned), str(fixed)], check=True)
                subprocess.run([str(candidates[0]), '-c', '-P', '16', '4', str(fixed)], check=True, capture_output=True)
            with zipfile.ZipFile(fixed) as archive:
                if digest(archive.read(ENGINE_ENTRY)) != digest(engine):
                    raise RuntimeError('Aligned APK engine checksum mismatch')
            before = actual
            for previous in old_targets(old) if old else []:
                if previous['target'] == target and previous['after'] == actual:
                    before = previous['before']
            apks.append({'package': pkg, 'apk_target': target, 'payload': payload, 'target': target, 'before': before, 'after': sha256(fixed)})
        except (RuntimeError, OSError, subprocess.SubprocessError, zipfile.BadZipFile, KeyError) as error:
            skipped[pkg] = str(error)
    for pkg, reason in skipped.items():
        print('Private Flutter engine ' + pkg + ': ' + reason +
              '; original code retained, native compatibility catalog safely handles supported profiles.', flush=True)
    for item in apks:
        item['apk_sha256'] = apk_hashes[item['package']]
    manifest = {'revision': REVISION, 'firmware': firmware, 'system': system,
                'apks': apks, 'packages': targets, 'apk_hashes': apk_hashes,
                'startup_script_sha256': script_hash, 'skipped_apks': skipped}
    values = {'SYSTEM_BEFORE': system['before'], 'SYSTEM_AFTER': system['after']}
    (folder / 'targets.conf').write_text(''.join(k + '=' + shlex.quote(v) + '\n' for k, v in values.items()))
    (folder / 'apks.conf').write_text(''.join('|'.join(i[k] for k in ('package', 'payload', 'apk_target', 'target', 'before', 'after', 'apk_sha256')) + '\n' for i in apks))
    (folder / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    (folder / 'module.prop').write_text(f'id={MODULE_ID}\nname=HyperOS AVD Flutter render fix\nversion={REVISION}\nversionCode={REVISION}\nauthor=HyperOS-AVD\ndescription=Native depth, Float16, storage alignment and dispersion shadow fix for the official ARM64 AVD\n')
    for name in ('post-fs-data.sh', 'service.sh'):
        (folder / name).write_text(startup_script)
    # Validate every input before replacing the previous module. Keep its backup.
    stage = '/data/adb/hyperos-render-stage-v' + str(REVISION) + '-' + system['after'][:12]
    pending = root(config, f'if [ -d {stage} ]; then cat {stage}/manifest.json; fi')
    if pending and json.loads(pending) != manifest:
        raise RuntimeError('An unrelated Flutter staging directory exists.')
    root(config, 'set -e\n' + mutation_guard(MODULE) + f'mkdir -p /data/adb/modules {stage}')
    originals = [(i['payload'] + '.original', i['before']) for i in apks if i.get('external')]
    files = ['flutter.so', 'targets.conf', 'apks.conf', 'manifest.json', 'module.prop', 'post-fs-data.sh', 'service.sh', *[i['payload'] for i in apks], *[name for name, _ in originals]]
    for name in files:
        remote = '/data/local/tmp/hyperos-render-' + name
        adb(config, 'push', str(folder / name), remote, check=True, capture_output=True, timeout=60)
        mode = '755' if name.endswith('.sh') else '644'
        root(config, 'set -e\n' + mutation_guard(MODULE) + f'cp {shlex.quote(remote)} {stage}/{name}\nchmod {mode} {stage}/{name}\nrm {shlex.quote(remote)}')
    root(config, 'set -e\n' + mutation_guard(MODULE) + f'chcon u:object_r:system_lib_file:s0 {stage}/flutter.so\n' + '\n'.join(f'chcon u:object_r:apk_data_file:s0 {stage}/{i["payload"]}' for i in apks))
    for name, _ in originals:
        root(config, 'set -e\n' + mutation_guard(MODULE) + f'chcon u:object_r:apk_data_file:s0 {stage}/{name}')
    for name, checksum in [('flutter.so', system['after']), *[(i['payload'], i['after']) for i in apks], *originals]:
        if root(config, f'sha256sum {stage}/{name}').split()[0] != checksum:
            raise RuntimeError('Staged overlay checksum mismatch: ' + name)
    if old:
        verify_hooks(root, config, MODULE, hook_versions(source, firmware))
        preserved = {item['target'] for item in old.get('apks', ()) if item.get('package') in skipped}
        detach(config, old, preserve_paths=preserved)
        root(config, 'set -e\n' + mutation_guard(MODULE) + f'mv {MODULE} /data/adb/hyperos-render-backup-$(date +%s)')
    root(config, 'set -e\n' + mutation_guard(MODULE) + f'test ! -e {MODULE}\nmv {stage} {MODULE}\nsh {MODULE}/service.sh')
    (ROOT / 'local/flutter-render-fix.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print('Native Flutter shared engine and ' + str(len(apks)) +
          ' verified private overlays installed. Original APKs and userdata were preserved.', flush=True)
    return manifest


def disable(config):
    official(config)
    manifest = json.loads(root(config, f'cat {MODULE}/manifest.json'))
    root(config, f'touch {MODULE}/disable\nam force-stop com.miui.home\nam force-stop com.miui.weather2')
    detach(config, manifest)
    print('Overlay disabled. Reboot to restore already-running Flutter processes.', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    flags = parser.add_mutually_exclusive_group()
    flags.add_argument('--disable', action='store_true')
    flags.add_argument('--enable', action='store_true')
    args = parser.parse_args()
    if args.disable:
        disable(runtime())
    else:
        install(runtime(), enable=args.enable)


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        raise SystemExit(str(error))
