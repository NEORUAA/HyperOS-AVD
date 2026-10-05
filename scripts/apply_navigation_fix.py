#!/usr/bin/env python3
"""Enable the original Xiaomi recents RRO before Android caches resources."""
import argparse
from datetime import date
import json
from pathlib import Path
import re
import secrets
import shlex
import subprocess
import zipfile

from common import ROOT, adb, runtime
from apply_flutter_fix import official, root
from patch_flutter import digest
from os4_defaults import PHONE_IDENTITY, THERMAL_LABEL_SCRIPT

MODULE = '/data/adb/modules/hyperos_avd_navigation'
PROPERTY = 'ro.miui.product.home'
COMPONENT = 'com.miui.home/com.miui.home.recents.RecentsActivity'
APK_SHA256 = '50a86b5976d645f9254ce84719d07de05f462452a14b05c60b28dc61afa4d464'
AOT_BEFORE = 'fb8b1f91eba2d9c43875f49bbded70cca462be341fc379b9864e7c6136d6e463'
AOT_AFTER = '44ab76174a5085b9cc45d44420b82e53c46423e2f58d32e0eee86264dc683dd5'
AOT_OFFSET = 0xe68c7c
WATCHDOG_BEFORE = '4dc9fa82371dda8a0a8b9eff48301469d2f84752b52b71635b84e5fc0c47fa6e'
WATCHDOG_AFTER = 'adf0bd94b88463669bebecb61ce301b614a974e28367a281ea1f81504062d3ed'
WATCHDOG_OFFSET = 0x61f878


def simulated_serial(previous):
    """Match the supplied phone's SN style, not an official factory identity."""
    value = previous.get('serial_number') if previous else None
    pattern = r'[0-9]{5}/[A-HJ-NP-Z][0-9][NPQRSTUVWXYZ][1-9ABCDEFHJKMNPQRSTUVWXYZ][0-9]{5}'
    if isinstance(value, str) and re.fullmatch(pattern, value):
        return value
    # Migrate only this project's earlier hex identifiers, once. Do not
    # silently replace an invalid or unrelated saved identifier.
    legacy = (isinstance(value, str) and re.fullmatch(r'[0-9A-F]{16}', value)
              and previous.get('revision') == 8)
    if value is not None and not legacy:
        raise RuntimeError('Invalid saved simulated serial number.')
    today = date.today()
    month = 'NPQRSTUVWXYZ'[today.month - 1]
    day = '123456789ABCDEFHJKMNPQRSTUVWXYZ'[today.day - 1]
    prefix = secrets.randbelow(90000) + 10000
    factory = secrets.choice('ABCDEFGHJKLMNPQRSTUVWXYZ')
    sequence = secrets.randbelow(99999) + 1
    return f'{prefix}/{factory}{today.year % 10}{month}{day}{sequence:05d}'


def patch_aot(data):
    """Allow the verified launcher to wait for slow AVD window targets.

    RecentsStateManager._startTimeouts cancels gestures after 800 ms, before
    this AVD delivers onAnimationStart. Load its existing five-second Duration
    instead. Both deadlines remain finite; animation and input handling remain
    intact. Change only this call site's pool load, not the shared Duration.
    """
    checksum = digest(data)
    if checksum == AOT_AFTER:
        return data
    if checksum != AOT_BEFORE:
        raise RuntimeError('Unsupported launcher AOT SHA-256: ' + checksum)
    if data[AOT_OFFSET:AOT_OFFSET + 8] != bytes.fromhex('625b4091420441f9'):
        raise RuntimeError('Unexpected recents deadline instruction.')
    result = bytearray(data)
    result[AOT_OFFSET:AOT_OFFSET + 8] = bytes.fromhex('62bf409142d043f9')
    if digest(result) != AOT_AFTER:
        raise RuntimeError('Launcher AOT output checksum mismatch.')
    return bytes(result)


def patch_watchdog(data):
    """Keep the app route until this AVD delivers its closing window target.

    AppPopWatchDog otherwise removes the route after 500 ms, before the
    original remote transition can match its app-window hero to the icon.
    Give that cleanup watchdog five seconds; keep its original handler.
    """
    checksum = digest(data)
    if checksum == WATCHDOG_AFTER:
        return data
    if checksum != WATCHDOG_BEFORE:
        raise RuntimeError('Unsupported launcher watchdog SHA-256: ' + checksum)
    if data[WATCHDOG_OFFSET:WATCHDOG_OFFSET + 4] != bytes.fromhex('883e8052'):
        raise RuntimeError('Unexpected launcher watchdog instruction.')
    result = bytearray(data)
    result[WATCHDOG_OFFSET:WATCHDOG_OFFSET + 4] = bytes.fromhex('08718252')
    if digest(result) != WATCHDOG_AFTER:
        raise RuntimeError('Launcher watchdog output checksum mismatch.')
    return bytes(result)


AOT_SCRIPT = r'''
BB=/data/adb/ksu/bin/busybox
changed=0
for patch_conf in aot.conf watchdog.conf; do
    [ -f "$MODDIR/$patch_conf" ] || continue
    . "$MODDIR/$patch_conf"
    [ "$(sha256sum "$HOME_APK" 2>/dev/null | cut -d ' ' -f 1)" = "$APK_SHA256" ] || continue
for pid in 1 $(getprop init.svc_debug_pid.hyos_spawner) $(pidof zygote64); do
    [ -d "/proc/$pid" ] || continue
    actual=$(nsenter -t "$pid" -m -- "$BB" sha256sum "$HOME_NATIVE" 2>/dev/null | cut -d ' ' -f 1)
    [ "$actual" = "$AOT_AFTER" ] && continue
    [ "$(sha256sum "$MODDIR/$PATCHED_NAME" | cut -d ' ' -f 1)" = "$AOT_AFTER" ] || exit 1
    if [ -z "$actual" ]; then
        [ "$(sha256sum "$MODDIR/$ORIGINAL_NAME" | cut -d ' ' -f 1)" = "$AOT_BEFORE" ] || exit 1
        nsenter -t "$pid" -m -- sh -c 'cp "$2" "$1"; chmod 644 "$1"; chcon u:object_r:apk_data_file:s0 "$1"' sh "$HOME_NATIVE" "$MODDIR/$ORIGINAL_NAME" || exit 1
        actual=$AOT_BEFORE
    fi
    [ "$actual" = "$AOT_BEFORE" ] || exit 1
    nsenter -t "$pid" -m -- "$BB" mount -o bind "$MODDIR/$PATCHED_NAME" "$HOME_NATIVE" || exit 1
    [ "$(nsenter -t "$pid" -m -- "$BB" sha256sum "$HOME_NATIVE" | cut -d ' ' -f 1)" = "$AOT_AFTER" ] || exit 1
    changed=1
done
done
if [ "${0##*/}" = service.sh ] && [ "$changed" = 1 ]; then
    am force-stop com.miui.home
fi
'''
EARLY_SCRIPT = r'''#!/system/bin/sh
MODDIR=${0%/*}
[ -f "$MODDIR/disable" ] && exit 0
exec >> "$MODDIR/navigation.log" 2>&1
echo "$(date +%s) Applying early launcher identity"
old=$(getprop ro.miui.product.home)
case "$old" in ''|com.miui.home) ;; *) echo "Refused home identity: $old" >> "$MODDIR/navigation.log"; exit 1 ;; esac
# This property activates the phone's original static product overlay. It must
# be present before PackageManager scans overlays and system_server caches UID.
/data/adb/ksud resetprop -n ro.miui.product.home com.miui.home || exit 1
# mi_ext build.prop is loaded by the phone vendor init, absent on ranchu.
/data/adb/ksud resetprop -n persist.sys.pre_startup true || exit 1
# Restore stock SF transitions after fixing the compositor's 20 Hz render rate.
# This also migrates images carrying the earlier local-animation workaround.
/data/adb/ksud resetprop -n persist.miui.home_sf_anim true || exit 1
# Set public phone identity before zygote caches android.os.Build fields.
# Emulator HAL selectors and boot hardware remain ranchu.
while IFS='=' read -r key value; do
    [ -n "$key" ] || continue
    current=$(getprop "$key")
    [ "$current" = "$value" ] && continue
    # A short property slot cannot grow into Android's long read-only storage.
    # Recreate long OTA fingerprints before zygote starts caching properties.
    if [ "${#value}" -ge 91 ] && [ -n "$current" ]; then
        /data/adb/ksud resetprop -d "$key" || exit 1
    fi
    /data/adb/ksud resetprop -n "$key" "$value" || exit 1
done < "$MODDIR/identity.prop"
echo "$(date +%s) Enabled original Xiaomi launcher resource overlay" >> "$MODDIR/navigation.log"
''' + THERMAL_LABEL_SCRIPT + AOT_SCRIPT
BOOT_SCRIPT = r'''#!/system/bin/sh
MODDIR=${0%/*}
[ -f "$MODDIR/disable" ] && exit 0
count=0
while [ "$(getprop sys.boot_completed)" != 1 ]; do
    count=$((count+1)); [ "$count" -lt 300 ] || exit 1; sleep 1
done
component=$(cmd overlay lookup android android:string/config_recentsComponentName)
echo "$(date +%s) Recents component: $component" >> "$MODDIR/navigation.log"
[ "$component" = com.miui.home/com.miui.home.recents.RecentsActivity ] || exit 1
''' + AOT_SCRIPT


def install(config, enable=False):
    official(config)
    if root(config, f'if [ -f {MODULE}/disable ]; then echo yes; fi') == 'yes':
        if not enable:
            print('Navigation module is disabled; keeping this choice.', flush=True)
            return
        root(config, f'rm {MODULE}/disable')
    identity = root(config, 'getprop ' + PROPERTY)
    if identity not in ('', 'com.miui.home'):
        raise RuntimeError('Unexpected launcher identity: ' + identity)
    root(config, 'test -f /product/overlay/MiuiHomeLauncherResOverlay.apk')
    current = root(config, 'cmd overlay lookup android android:string/config_recentsComponentName')
    previous = root(config, f'if [ -d {MODULE} ]; then cat {MODULE}/module.prop; fi')
    if previous and 'id=hyperos_avd_navigation\n' not in previous:
        raise RuntimeError('Refused to replace an unrelated navigation module.')
    old_text = root(config, f'if [ -f {MODULE}/manifest.json ]; then cat {MODULE}/manifest.json; fi')
    old = json.loads(old_text) if old_text else None
    if old and old.get('revision') not in (2, 3, 4, 5, 6, 7, 8, 9, 10):
        raise RuntimeError('Unknown navigation module revision.')
    folder = ROOT / 'work/navigation-fix'
    folder.mkdir(parents=True, exist_ok=True)
    serial = simulated_serial(old)
    # Xiaomi's DeviceIdentifiersPolicyService reads psno specifically for
    # Settings and Contacts; other callers use the standard serial property.
    phone_identity = dict(PHONE_IDENTITY, **{'ro.serialno': serial,
                                           'ro.boot.serialno': serial,
                                           'ro.ril.oem.psno': serial})
    manifest = {'revision': 10, 'property': PROPERTY, 'value': 'com.miui.home', 'component': COMPONENT,
                'animation_backend': 'stock-sf', 'sf_animation': True,
                'phone_identity': phone_identity, 'serial_number': serial}
    apk = root(config, 'pm path com.miui.home').splitlines()[0].removeprefix('package:')
    aot_supported = apk.startswith('/data/app/') and root(config, 'sha256sum ' + shlex.quote(apk)).split()[0] == APK_SHA256
    payloads = []
    checksums = []
    if aot_supported:
        original = folder / 'home-original.apk'
        if not original.is_file() or digest(original.read_bytes()) != APK_SHA256:
            adb(config, 'pull', apk, str(original), check=True, capture_output=True, timeout=60)
        if digest(original.read_bytes()) != APK_SHA256:
            raise RuntimeError('Launcher APK changed while reading it.')
        with zipfile.ZipFile(original) as archive:
            for kind, entry, stem, before, after, patcher in (
                    ('aot', 'libapp.so', 'launcher-aot', AOT_BEFORE, AOT_AFTER, patch_aot),
                    ('watchdog', 'libapp_launcher.so', 'launcher-watchdog', WATCHDOG_BEFORE, WATCHDOG_AFTER, patch_watchdog)):
                native = str(Path(apk).parent / 'lib/arm64' / entry)
                actual = root(config, 'if [ -f ' + shlex.quote(native) + ' ]; then sha256sum ' + shlex.quote(native) + '; fi')
                if actual and actual.split()[0] not in (before, after):
                    raise RuntimeError('An unrelated launcher library already exists: ' + entry)
                data = archive.read('lib/arm64-v8a/' + entry)
                patched_name, original_name = stem + '.so', stem + '.original.so'
                (folder / patched_name).write_bytes(patcher(data))
                (folder / original_name).write_bytes(data)
                manifest[kind] = {'apk': apk, 'target': native, 'before': before, 'after': after}
                (folder / (kind + '.conf')).write_text(''.join(k + '=' + shlex.quote(v) + '\n' for k, v in {
                    'HOME_APK': apk, 'HOME_NATIVE': native, 'APK_SHA256': APK_SHA256,
                    'AOT_BEFORE': before, 'AOT_AFTER': after,
                    'PATCHED_NAME': patched_name, 'ORIGINAL_NAME': original_name}.items()))
                payloads += [patched_name, original_name, kind + '.conf']
                checksums += [(patched_name, after), (original_name, before)]
    (folder / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    (folder / 'post-fs-data.sh').write_text(EARLY_SCRIPT)
    (folder / 'service.sh').write_text(BOOT_SCRIPT)
    (folder / 'system.prop').write_text('ro.miui.product.home=com.miui.home\npersist.miui.home_sf_anim=true\n')
    (folder / 'identity.prop').write_text(''.join(key + '=' + value + '\n'
                                             for key, value in phone_identity.items()))
    (folder / 'module.prop').write_text('id=hyperos_avd_navigation\nname=HyperOS AVD native Quickstep\nversion=10\nversionCode=10\nauthor=HyperOS-AVD\ndescription=Original phone and launcher identity, persistent Xiaomi-style simulated serial and stock SF transitions\n')
    if old and old['revision'] == 2:
        # Remove only this project's experimental directory bind. Original
        # overlay files remain untouched on the read-only system partition.
        check = 'awk \'$4 == "/adb/modules/hyperos_avd_navigation/overlay" && $5 == "/product/overlay" {found=1} END {exit !found}\' /proc/self/mountinfo'
        root(config, 'for pid in 1 $(getprop init.svc_debug_pid.hyos_spawner) $(pidof zygote64); do\n'
             '[ -d "/proc/$pid" ] || continue\n'
             'if nsenter -t "$pid" -m -- sh -c ' + shlex.quote(check) + '; then\n'
             'nsenter -t "$pid" -m -- /data/adb/ksu/bin/busybox umount -l /product/overlay || exit 1\nfi\ndone')
        root(config, f'mv {MODULE} /data/adb/hyperos-navigation-backup-$(date +%s)')
    root(config, f'mkdir -p {MODULE}')
    names = ['manifest.json', 'module.prop', 'system.prop', 'identity.prop', 'post-fs-data.sh', 'service.sh']
    names += payloads
    for name in names:
        remote = '/data/local/tmp/hyperos-nav-' + name
        adb(config, 'push', str(folder / name), remote, check=True, capture_output=True, timeout=30)
        root(config, f'cp {remote} {MODULE}/{name}.next\nchmod {"755" if name.endswith(".sh") else "644"} {MODULE}/{name}.next\nmv {MODULE}/{name}.next {MODULE}/{name}\nrm {remote}')
    if aot_supported:
        for filename, expected in checksums:
            if root(config, f'sha256sum {MODULE}/{filename}').split()[0] != expected:
                raise RuntimeError('Staged launcher AOT checksum mismatch.')
        root(config, f'chcon u:object_r:apk_data_file:s0 {MODULE}/launcher-aot.so {MODULE}/launcher-watchdog.so')
        if identity == 'com.miui.home' and current == COMPONENT:
            root(config, f'sh {MODULE}/service.sh')
    else:
        root(config, f'rm -f {MODULE}/aot.conf {MODULE}/watchdog.conf')
        print('Launcher deadline patch: this APK is not supported; keeping its original code.', flush=True)
    # A fresh userdata install happens after post-fs-data has already passed.
    # Apply thermal labels now; the baked identity handles the first RRO scan.
    root(config, f'sh {MODULE}/post-fs-data.sh')
    # A previous diagnostic mutable overlay must not mask the original RRO.
    root(config, 'cmd overlay disable --user 0 com.android.shell:hyperos_avd_recents 2>/dev/null || true')
    (ROOT / 'local/navigation-fix.json').write_text(json.dumps(manifest, indent=2) + '\n')
    if identity == 'com.miui.home' and current == COMPONENT:
        print('Original Xiaomi Quickstep identity is active.', flush=True)
    else:
        print('Original Xiaomi Quickstep identity staged. Cold-boot this OS4 AVD to activate it.', flush=True)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--disable', action='store_true')
    group.add_argument('--enable', action='store_true')
    args = parser.parse_args()
    config = runtime()
    if args.disable:
        official(config)
        root(config, f'touch {MODULE}/disable')
        print('Navigation module disabled. Cold-boot this OS4 AVD to restore its image properties.', flush=True)
    else:
        install(config, enable=args.enable)


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        raise SystemExit(str(error))
