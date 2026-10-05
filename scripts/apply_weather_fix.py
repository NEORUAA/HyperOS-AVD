#!/usr/bin/env python3
"""Install the private Weather ANGLE bridge on the official OS4 AVD."""
import argparse
import json
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import zipfile

from common import ROOT, adb, runtime, sha256
from apply_flutter_fix import official, root
from patch_weather import (ANGLE, APK_SHA256, EMPTY, LIBRARIES, PAD_APK,
                           PAD_NATIVE, build_bridge, patch, profile)

MODULE = '/data/adb/modules/hyperos_avd_weather_gl'
PACKAGE = 'com.miui.weather2'
PAD_VERSION = 'OS4.0.15.0.XBMCNXM'
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


def pad_native(apk):
    """Resolve only the factory or normal signed Pad Weather installation."""
    if apk == PAD_APK:
        return PAD_NATIVE
    if not isinstance(apk, str) or not re.fullmatch(
            r'/data/app/(?:~~[A-Za-z0-9_=-]+/)?com\.miui\.weather2-[A-Za-z0-9_=-]+/base\.apk', apk):
        raise RuntimeError('Unsupported tablet Weather package layout.')
    return str(Path(apk).parent / 'lib/arm64')


def pad_directories(apk):
    native = pad_native(apk)
    return sorted({str(path) for base in (Path(native), Path(apk).parent)
                   for path in (base, *base.parents) if str(path) != '/'})


def pad_native_hash(config, target):
    """Reject aliases before reading a private native target's checksum."""
    quoted = shlex.quote(target)
    return root(config, f'if [ -e {quoted} ] || [ -L {quoted} ]; then\n'
                f'[ -f {quoted} ] && [ ! -L {quoted} ] || exit 1\n'
                f'sha256sum {quoted}\nfi')


def validate_pad_manifest(manifest, selected):
    """Keep the exact signed package overlay inside its own native directory."""
    native = pad_native(manifest.get('apk'))
    if (selected.get('id') != 'tablet-yingtian'
            or manifest.get('profile') != selected['id']
            or manifest.get('apk_sha256') != selected['apk_sha256']):
        raise RuntimeError('Unsupported saved tablet Weather bridge.')
    expected = {**selected['libraries'],
                **{name: (EMPTY, checksum) for name, checksum in ANGLE.items()}}
    targets = manifest.get('targets', [])
    if len(targets) != len(expected) + 1 or len({item.get('payload') for item in targets}) != len(targets):
        raise RuntimeError('Incomplete saved tablet Weather bridge.')
    for item in targets:
        name = item.get('payload')
        if item.get('target') != native + '/' + str(name):
            raise RuntimeError('Invalid tablet Weather native target.')
        pair = (item.get('before'), item.get('after'))
        if name == 'libhgl.so':
            if pair[0] != EMPTY or not re.fullmatch('[0-9a-f]{64}', str(pair[1])):
                raise RuntimeError('Invalid tablet Weather bridge checksum.')
        elif name not in expected or pair != expected[name]:
            raise RuntimeError('Unsupported saved tablet Weather library: ' + str(name))


def boot_script(selected, manifest):
    if selected['id'] == 'phone-hongkong':
        return BOOT_SCRIPT
    validate_pad_manifest(manifest, selected)
    native = pad_native(manifest['apk'])
    script = BOOT_SCRIPT.replace(APK_SHA256, selected['apk_sha256'])
    guard = '[ "$apk" = ' + shlex.quote(manifest['apk']) + ' ] || exit 1\n'
    guard += '        case "$payload|$target|$before|$after" in\n'
    for item in manifest['targets']:
        value = '|'.join(item[key] for key in ('payload', 'target', 'before', 'after'))
        guard += '            ' + shlex.quote(value) + ') ;;\n'
    guard += '            *) exit 1 ;;\n        esac\n        '
    script = script.replace('current=$(pm path', guard + 'current=$(pm path', 1)
    pattern = '|'.join(native + '/' + name for name in ('libhgl.so', *ANGLE))
    script = script.replace('/data/app/*/lib/arm64/libhgl.so', pattern)
    # Publish the placeholder with an atomic hard link; never truncate a file.
    atomic = ('set -e; [ ! -e "$1" ] && [ ! -L "$1" ]; '
              'temporary="$1.hyperos-avd-placeholder.$$"; '
              '(set -C; : > "$temporary"); trap \'rm -f "$temporary"\' EXIT; '
              'chmod 644 "$temporary"; '
              'chcon u:object_r:apk_data_file:s0 "$temporary"; '
              'ln "$temporary" "$1"')
    script = script.replace("'touch \"$1\"; chmod 644 \"$1\"; chcon u:object_r:apk_data_file:s0 \"$1\"'",
                            shlex.quote(atomic))
    script = script.replace('changed=0\n',
                            '[ "$(getprop ro.boot.hardware)" = ranchu ] || exit 1\n'
                            '[ "$(getprop ro.product.device)" = yingtian ] || exit 1\n'
                            '[ "$(getprop ro.mi.os.version.incremental)" = ' + PAD_VERSION + ' ] || exit 1\n'
                            'changed=0\n', 1)
    # Verify the signed APK and private payload in every inherited namespace,
    # including when a propagated bind already has the expected output hash.
    script = script.replace('$(sha256sum "$apk" | cut -d \' \' -f 1)',
                            '$(nsenter -t "$pid" -m -- "$BB" sha256sum "$apk" | "$BB" cut -d \' \' -f 1)')
    directory_guard = ('for directory in ' + ' '.join(shlex.quote(path) for path in pad_directories(manifest['apk']))
                       + '; do [ -d "$directory" ] && [ ! -L "$directory" ] || exit 1; done; '
                         '[ ! -L "$1" ] && [ ! -L "$2" ]')
    namespace_guard = ('        nsenter -t "$pid" -m -- sh -c ' + shlex.quote(directory_guard)
                       + ' sh "$target" "$apk" || exit 1\n'
                         '        source_hash=$(nsenter -t "$pid" -m -- "$BB" sha256sum "$MODDIR/$payload" | "$BB" cut -d \' \' -f 1)\n'
                         '        [ "$source_hash" = "$after" ] || exit 1\n')
    script = script.replace('        actual=$(nsenter', namespace_guard + '        actual=$(nsenter', 1)
    return script


def valid_target(path):
    private = {PAD_NATIVE + '/' + name for name in (*LIBRARIES, 'libhgl.so', *ANGLE)}
    return (path in private or (path.startswith('/data/app/')
            and '..' not in Path(path).parts
            and not any(c in path for c in '\n\r|\"\'`$\\')))


def detach(config, manifest):
    commands = []
    for item in manifest['targets']:
        path = item['target']
        if not valid_target(path):
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


def install(config, enable=False, sources=('official-hongkong-ota',), angle_folder=None):
    official(config, sources=sources)
    selected = profile(json.loads((ROOT / 'local/build.json').read_text())['source'])
    paths = root(config, 'pm path ' + PACKAGE).splitlines()
    if len(paths) != 1 or not paths[0].startswith('package:'):
        raise RuntimeError('Expected exactly one signed Weather base APK.')
    apk = paths[0].removeprefix('package:')
    native = str(Path(apk).parent / 'lib/arm64')
    tablet = selected['id'] == 'tablet-yingtian'
    libraries = selected['libraries']
    if tablet:
        native = pad_native(apk)
        if root(config, 'id -u') != '0' or root(config, 'getprop sys.boot_completed') != '1':
            raise RuntimeError('Wait for boot completion and KernelSU root first.')
        for prop, expected in (('ro.boot.hardware', 'ranchu'), ('ro.product.device', 'yingtian'),
                               ('ro.mi.os.version.incremental', PAD_VERSION)):
            if root(config, 'getprop ' + prop) != expected:
                raise RuntimeError('Unsupported running tablet Weather firmware profile.')
        directories = ' '.join(shlex.quote(path) for path in pad_directories(apk))
        root(config, 'for directory in ' + directories + '; do\n'
             '[ -d "$directory" ] && [ ! -L "$directory" ] || exit 1\ndone\n'
             '[ -f ' + shlex.quote(apk) + ' ] && [ ! -L ' + shlex.quote(apk) + ' ]')
        if root(config, 'sha256sum ' + shlex.quote(apk)).split()[0] != selected['apk_sha256']:
            raise RuntimeError('Unsupported Weather APK shader workload; refusing the ANGLE feature override.')
    if not tablet and apk == '/product/app/MIUIWeather/MIUIWeather.apk':
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
    if (not tablet and not apk.startswith('/data/app/')) or root(config, 'if [ -f ' + shlex.quote(native + '/libmglnative.so') + ' ]; then echo yes; fi') != 'yes':
        print('Weather MGL bridge: this package layout is not supported; keeping its original driver.', flush=True)
        return None
    if not tablet and root(config, 'sha256sum ' + shlex.quote(apk)).split()[0] != selected['apk_sha256']:
        raise RuntimeError('Unsupported Weather APK shader workload; refusing the ANGLE feature override.')
    folder = ROOT / 'work/weather-angle-fix'
    folder.mkdir(parents=True, exist_ok=True)
    old_text = root(config, f'if [ -f {MODULE}/manifest.json ]; then cat {MODULE}/manifest.json; fi')
    old = json.loads(old_text) if old_text else None
    if old and old.get('revision') != 1:
        raise RuntimeError('Unknown existing Weather bridge revision.')
    if old and tablet:
        validate_pad_manifest(old, selected)
    if old and root(config, f'if [ -f {MODULE}/disable ]; then echo yes; fi') == 'yes':
        if not enable:
            print('Weather bridge is disabled; keeping this choice.', flush=True)
            return old
        if not tablet:
            root(config, f'rm {MODULE}/disable')
    if old and old['apk'] == apk:
        for item in old['targets']:
            actual = (pad_native_hash(config, item['target']) if tablet else
                      root(config, 'if [ -f ' + shlex.quote(item['target']) + ' ]; then sha256sum ' + shlex.quote(item['target']) + '; fi'))
            actual = actual.split()[0] if actual else ''
            if not actual and item['before'] == EMPTY:
                continue
            if actual not in (item['before'], item['after']):
                raise RuntimeError('Weather overlay target changed: ' + item['target'])
        script = folder / 'service.sh'
        script.write_text(boot_script(selected, old))
        adb(config, 'push', str(script), '/data/local/tmp/hyperos-weather-service.sh', check=True, capture_output=True, timeout=30)
        if tablet:
            root(config, f'cp /data/local/tmp/hyperos-weather-service.sh {MODULE}/service.sh.next\n'
                 f'chmod 755 {MODULE}/service.sh.next\nsh -n {MODULE}/service.sh.next\n'
                 f'mv {MODULE}/service.sh.next {MODULE}/service.sh\n'
                 f'rm /data/local/tmp/hyperos-weather-service.sh\n'
                 + (f'rm -f {MODULE}/disable\n' if enable else '') + f'sh {MODULE}/service.sh')
        else:
            root(config, f'cp /data/local/tmp/hyperos-weather-service.sh {MODULE}/service.sh\nchmod 755 {MODULE}/service.sh\nrm /data/local/tmp/hyperos-weather-service.sh\nsh {MODULE}/service.sh')
        return old
    original = folder / 'weather-original.apk'
    adb(config, 'pull', apk, str(original), check=True, capture_output=True, timeout=60)
    if sha256(original) != selected['apk_sha256']:
        raise RuntimeError('Exported Weather APK checksum mismatch.')
    targets = []
    with zipfile.ZipFile(original) as archive:
        for name, (before, after) in libraries.items():
            fixed = patch(name, archive.read('lib/arm64-v8a/' + name), libraries=libraries)
            (folder / name).write_bytes(fixed)
            target = native + '/' + name
            actual = (pad_native_hash(config, target) if tablet else root(config, 'sha256sum ' + shlex.quote(target)))
            actual = actual.split()[0] if actual else ''
            if actual not in (before, after):
                raise RuntimeError('Extracted Weather MGL library differs from its APK: ' + name)
            targets.append({'payload': name, 'target': target, 'before': before, 'after': after})
    for name in ANGLE:
        if angle_folder is None:
            adb(config, 'pull', '/system/lib64/' + name, str(folder / name), check=True, capture_output=True, timeout=60)
        else:
            if not tablet or sha256(Path(angle_folder) / name) != ANGLE[name]:
                raise RuntimeError('Unsupported private Weather ANGLE dependency: ' + name)
            if (Path(angle_folder) / name).resolve() != (folder / name).resolve():
                shutil.copyfile(Path(angle_folder) / name, folder / name)
        if tablet:
            target = native + '/' + name
            existing = pad_native_hash(config, target)
            if existing and existing.split()[0] not in (EMPTY, ANGLE[name]):
                raise RuntimeError('An unrelated private ANGLE library exists in the Weather directory.')
            targets.append({'payload': name, 'target': target, 'before': EMPTY, 'after': ANGLE[name]})
    bridge = build_bridge(config, folder)
    target = native + '/libhgl.so'
    existing = (pad_native_hash(config, target) if tablet else
                root(config, 'if [ -e ' + shlex.quote(target) + ' ]; then sha256sum ' + shlex.quote(target) + '; fi'))
    if existing and existing.split()[0] != EMPTY:
        raise RuntimeError('An unrelated libhgl.so exists in the Weather directory.')
    targets.append({'payload': 'libhgl.so', 'target': target, 'before': EMPTY, 'after': sha256(bridge)})
    manifest = {'revision': 1, 'apk': apk, 'targets': targets}
    if tablet:
        manifest.update({'profile': selected['id'], 'apk_sha256': selected['apk_sha256']})
    (folder / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    (folder / 'service.sh').write_text(boot_script(selected, manifest))
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
    root(config, f'mkdir -p /data/adb/modules\ntest ! -e {MODULE}\nmv {stage} {MODULE}\nsh {MODULE}/service.sh')
    (ROOT / 'local').mkdir(parents=True, exist_ok=True)
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
