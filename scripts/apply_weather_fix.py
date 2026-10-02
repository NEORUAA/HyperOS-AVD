#!/usr/bin/env python3
"""Install the private Weather ANGLE bridge on the official OS4 AVD."""
import argparse
import json
from pathlib import Path
import shlex
import subprocess
import zipfile

from common import ROOT, adb, runtime, sha256
from apply_flutter_fix import official, root
from patch_weather import ANGLE, APK_SHA256, EMPTY, LIBRARIES, build_bridge, patch

MODULE = '/data/adb/modules/hyperos_avd_weather_gl'
PACKAGE = 'com.miui.weather2'
BOOT_SCRIPT = r'''#!/system/bin/sh
MODDIR=${0%/*}
BB=/data/adb/ksu/bin/busybox
[ -f "$MODDIR/disable" ] && exit 0
count=0
while [ "$(getprop sys.boot_completed)" != 1 ]; do
    count=$((count+1)); [ "$count" -lt 300 ] || exit 1; sleep 1
done
changed=0
for pid in 1 $(getprop init.svc_debug_pid.hyos_spawner) $(pidof zygote64); do
    [ -d "/proc/$pid" ] || continue
    while IFS='|' read -r apk payload target before after; do
        [ -n "$apk" ] || continue
        current=$(pm path com.miui.weather2 </dev/null | "$BB" sed -n 's/^package://p' | "$BB" head -n 1)
        [ "$current" = "$apk" ] || { echo "APK changed. Rerun apply_weather_fix.py." >> "$MODDIR/bridge.log"; exit 0; }
        [ "$(sha256sum "$apk" | cut -d ' ' -f 1)" = 017d613137c0512a937dc6b8565da856099e988feab47b9eabc5c3d0a13f06e6 ] || { echo "Unsupported Weather shader workload" >> "$MODDIR/bridge.log"; exit 1; }
        actual=$(nsenter -t "$pid" -m -- "$BB" sha256sum "$target" 2>/dev/null | "$BB" cut -d ' ' -f 1)
        [ "$actual" = "$after" ] && continue
        source_hash=$(nsenter -t "$pid" -m -- "$BB" sha256sum "$MODDIR/$payload" | "$BB" cut -d ' ' -f 1)
        [ "$source_hash" = "$after" ] || exit 1
        if [ -z "$actual" ] && [ "$before" = e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855 ]; then
            case "$target" in /data/app/*/lib/arm64/libhgl.so) ;; *) exit 1 ;; esac
            nsenter -t "$pid" -m -- sh -c 'touch "$1"; chmod 644 "$1"; chcon u:object_r:apk_data_file:s0 "$1"' sh "$target" || exit 1
            actual=$before
        fi
        [ "$actual" = "$before" ] || { echo "Refused $target ($actual)" >> "$MODDIR/bridge.log"; exit 1; }
        nsenter -t "$pid" -m -- "$BB" mount -o bind "$MODDIR/$payload" "$target" || exit 1
        mounted=$(nsenter -t "$pid" -m -- "$BB" sha256sum "$target" | "$BB" cut -d ' ' -f 1)
        [ "$mounted" = "$after" ] || exit 1
        changed=1
        echo "Applied $target in $pid" >> "$MODDIR/bridge.log"
    done < "$MODDIR/targets.conf"
done
[ "$changed" = 0 ] || am force-stop com.miui.weather2
exit 0
'''


def detach(config, manifest):
    commands = []
    for item in manifest['targets']:
        path = item['target']
        if not path.startswith('/data/app/') or any(c in path for c in '\n\r|"'):
            raise RuntimeError('Invalid saved Weather overlay target.')
        loop = ("for attempt in 1 2 3 4; do "
                f"awk '$4 ~ /^\\/adb\\/modules\\/hyperos_avd_weather_gl\\// && $5 == \"{path}\" {{found=1}} END {{exit !found}}' /proc/self/mountinfo || break; "
                f'/data/adb/ksu/bin/busybox umount -l {shlex.quote(path)} || exit 1; done')
        commands.append('nsenter -t "$pid" -m -- sh -c ' + shlex.quote(loop))
    root(config, 'for pid in 1 $(getprop init.svc_debug_pid.hyos_spawner) $(pidof zygote64); do\n'
         '[ -d "/proc/$pid" ] || continue\n' + '\n'.join(commands) + '\ndone')
    for item in manifest['targets']:
        if item['before'] == EMPTY:
            path = shlex.quote(item['target'])
            root(config, f'if [ -f {path} ] && [ "$(sha256sum {path} | cut -d " " -f 1)" = {EMPTY} ]; then rm {path}; fi')


def install(config, enable=False):
    official(config)
    apk = root(config, 'pm path ' + PACKAGE).splitlines()[0].removeprefix('package:')
    native = str(Path(apk).parent / 'lib/arm64')
    if apk == '/product/app/MIUIWeather/MIUIWeather.apk':
        marker = json.loads(root(config, 'cat /product/etc/hyperos-avd-preinstalled-apps.json'))
        app = next(item for item in marker['apps'] if item['package'] == PACKAGE)
        if app['apk'] != apk or app['sha256'] != APK_SHA256:
            raise RuntimeError('Unsupported baked Weather APK.')
        expected = {name: pair[1] for name, pair in LIBRARIES.items()}
        expected['libhgl.so'] = app['native_libraries']['libhgl.so']
        if root(config, 'sha256sum ' + shlex.quote(apk)).split()[0] != APK_SHA256:
            raise RuntimeError('Baked Weather APK checksum mismatch.')
        for name, checksum in expected.items():
            if root(config, 'sha256sum ' + shlex.quote(native + '/' + name)).split()[0] != checksum:
                raise RuntimeError('Baked Weather bridge checksum mismatch: ' + name)
        print('Weather ANGLE bridge is already baked into the system image.', flush=True)
        return app
    # Factory packages may use a different MGL ABI. Never guess its offsets.
    if not apk.startswith('/data/app/') or root(config, 'if [ -f ' + shlex.quote(native + '/libmglnative.so') + ' ]; then echo yes; fi') != 'yes':
        print('Weather MGL bridge: this package layout is not supported; keeping its original driver.', flush=True)
        return None
    if root(config, 'sha256sum ' + shlex.quote(apk)).split()[0] != APK_SHA256:
        raise RuntimeError('Unsupported Weather APK shader workload; refusing the ANGLE feature override.')
    folder = ROOT / 'work/weather-angle-fix'
    folder.mkdir(parents=True, exist_ok=True)
    old_text = root(config, f'if [ -f {MODULE}/manifest.json ]; then cat {MODULE}/manifest.json; fi')
    old = json.loads(old_text) if old_text else None
    if old and old.get('revision') != 1:
        raise RuntimeError('Unknown existing Weather bridge revision.')
    if old and root(config, f'if [ -f {MODULE}/disable ]; then echo yes; fi') == 'yes':
        if not enable:
            print('Weather bridge is disabled; keeping this choice.', flush=True)
            return old
        root(config, f'rm {MODULE}/disable')
    if old and old['apk'] == apk:
        for item in old['targets']:
            actual = root(config, 'if [ -f ' + shlex.quote(item['target']) + ' ]; then sha256sum ' + shlex.quote(item['target']) + '; fi')
            actual = actual.split()[0] if actual else ''
            if not actual and item['before'] == EMPTY:
                continue
            if actual not in (item['before'], item['after']):
                raise RuntimeError('Weather overlay target changed: ' + item['target'])
        script = folder / 'service.sh'
        script.write_text(BOOT_SCRIPT)
        adb(config, 'push', str(script), '/data/local/tmp/hyperos-weather-service.sh', check=True, capture_output=True, timeout=30)
        root(config, f'cp /data/local/tmp/hyperos-weather-service.sh {MODULE}/service.sh\nchmod 755 {MODULE}/service.sh\nrm /data/local/tmp/hyperos-weather-service.sh\nsh {MODULE}/service.sh')
        return old
    original = folder / 'weather-original.apk'
    adb(config, 'pull', apk, str(original), check=True, capture_output=True, timeout=60)
    targets = []
    with zipfile.ZipFile(original) as archive:
        for name, (before, after) in LIBRARIES.items():
            fixed = patch(name, archive.read('lib/arm64-v8a/' + name))
            (folder / name).write_bytes(fixed)
            target = native + '/' + name
            actual = root(config, 'sha256sum ' + shlex.quote(target)).split()[0]
            if actual not in (before, after):
                raise RuntimeError('Extracted Weather MGL library differs from its APK: ' + name)
            targets.append({'payload': name, 'target': target, 'before': before, 'after': after})
    for name in ANGLE:
        adb(config, 'pull', '/system/lib64/' + name, str(folder / name), check=True, capture_output=True, timeout=60)
    bridge = build_bridge(config, folder)
    target = native + '/libhgl.so'
    existing = root(config, 'if [ -e ' + shlex.quote(target) + ' ]; then sha256sum ' + shlex.quote(target) + '; fi')
    if existing and existing.split()[0] != EMPTY:
        raise RuntimeError('An unrelated libhgl.so exists in the Weather directory.')
    targets.append({'payload': 'libhgl.so', 'target': target, 'before': EMPTY, 'after': sha256(bridge)})
    manifest = {'revision': 1, 'apk': apk, 'targets': targets}
    (folder / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    (folder / 'service.sh').write_text(BOOT_SCRIPT)
    (folder / 'targets.conf').write_text(''.join('|'.join([apk, *[i[k] for k in ('payload', 'target', 'before', 'after')]]) + '\n' for i in targets))
    (folder / 'module.prop').write_text('id=hyperos_avd_weather_gl\nname=HyperOS AVD Weather ANGLE bridge\nversion=1\nversionCode=1\nauthor=HyperOS-AVD\ndescription=Private Vulkan EGL display for verified Xiaomi Weather MGL libraries\n')
    stage = '/data/adb/hyperos-weather-stage-' + manifest['targets'][-1]['after'][:12]
    root(config, f'test ! -e {stage}\nmkdir -p {stage}')
    for name in ('manifest.json', 'service.sh', 'targets.conf', 'module.prop', *[i['payload'] for i in targets]):
        remote = '/data/local/tmp/hyperos-weather-' + name
        adb(config, 'push', str(folder / name), remote, check=True, capture_output=True, timeout=60)
        root(config, f'cp {shlex.quote(remote)} {stage}/{name}\nchmod {"755" if name.endswith(".sh") else "644"} {stage}/{name}\nrm {shlex.quote(remote)}')
    for item in targets:
        path = stage + '/' + item['payload']
        root(config, 'chcon u:object_r:apk_data_file:s0 ' + path)
        if root(config, 'sha256sum ' + path).split()[0] != item['after']:
            raise RuntimeError('Staged Weather overlay checksum mismatch.')
    if old:
        detach(config, old)
        root(config, f'mv {MODULE} /data/adb/hyperos-weather-backup-$(date +%s)')
    root(config, f'test ! -e {MODULE}\nmv {stage} {MODULE}\nsh {MODULE}/service.sh')
    (ROOT / 'local/weather-angle-fix.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print('Weather MGL now uses a private ANGLE Vulkan display. Other apps retain their drivers.', flush=True)
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
        manifest = json.loads(root(config, f'cat {MODULE}/manifest.json'))
        root(config, f'touch {MODULE}/disable\nam force-stop {PACKAGE}')
        detach(config, manifest)
        print('Weather bridge disabled. Original package libraries restored.', flush=True)
    else:
        install(config, enable=args.enable)


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        raise SystemExit(str(error))
