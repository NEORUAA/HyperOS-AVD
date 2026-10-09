#!/usr/bin/env python3
"""Install the private Weather ANGLE bridge on the official OS4 AVD."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import zipfile

from patch_outcome import UnsupportedPatch
from common import ROOT, REPO_ROOT, adb, runtime, sha256
from apply_flutter_fix import official, root
from patch_weather import (ANGLE, APK_SHA256, EMPTY, LIBRARIES, PAD_APK,
                           PAD_NATIVE, BRIDGE_SHA256, BRIDGE_SOURCE_SHA256, build_bridge, patch, profile)
from patch_weather import verify_bridge_prebuilt

MODULE = '/data/adb/modules/hyperos_avd_weather_gl'
PACKAGE = 'com.miui.weather2'
NAME = 'HyperOS AVD Weather ANGLE bridge'
DESCRIPTION = 'Private Vulkan EGL for verified signed Xiaomi Weather workloads'
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
    directories = [str(path) for path in Path(target).parents if str(path) != '/']
    return root(config, f'if [ -e {quoted} ] || [ -L {quoted} ]; then\n'
                + ''.join(f'[ -d {shlex.quote(path)} ] && [ ! -L {shlex.quote(path)} ] || exit 1\n'
                          for path in directories)
                + f'[ -f {quoted} ] && [ ! -L {quoted} ] || exit 1\n'
                f'[ "$(stat -c %h {quoted})" = 1 ] || exit 1\n'
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


def _legacy_module(config, module):
    """Authenticate the old generated overlay before KernelSU stages an update."""
    directory = module['directory']
    text = root(config, f'[ -f {directory}/manifest.json ] && [ ! -L {directory}/manifest.json ] || exit 1\n'
                f'cat {directory}/manifest.json')
    try:
        saved = json.loads(text)
    except (ValueError, TypeError) as error:
        raise RuntimeError('Invalid legacy Weather manifest.') from error
    if not isinstance(saved, dict) or saved.get('revision') != 1:
        raise RuntimeError('Unknown existing Weather bridge revision.')
    selected = profile('official-yingtian-ota' if saved.get('profile') == 'tablet-yingtian'
                       else 'official-hongkong-ota')
    if selected['id'] == 'tablet-yingtian':
        if set(saved) != {'revision', 'apk', 'profile', 'apk_sha256', 'targets'}:
            raise RuntimeError('Unknown legacy Weather manifest fields.')
        validate_pad_manifest(saved, selected)
    else:
        native = str(Path(saved.get('apk', '')).parent / 'lib/arm64')
        expected = {**LIBRARIES, 'libhgl.so': (EMPTY, BRIDGE_SHA256)}
        targets = saved.get('targets', [])
        if (set(saved) != {'revision', 'apk', 'targets'}
                or not re.fullmatch(r'/data/app/(?:~~[A-Za-z0-9_=-]+/)?com\.miui\.weather2-[A-Za-z0-9_=-]+/base\.apk',
                                    saved.get('apk', '')) or len(targets) != len(expected)
                or {item.get('payload') for item in targets} != set(expected)):
            raise RuntimeError('Unknown legacy Weather package layout.')
        for item in targets:
            name = item.get('payload')
            if (name not in expected or item.get('target') != native + '/' + name
                    or (item.get('before'), item.get('after')) != expected[name]):
                raise RuntimeError('Unknown legacy Weather native recipe.')
    if any(item.get('payload') == 'libhgl.so' and item.get('after') != BRIDGE_SHA256
           for item in saved['targets']):
        raise RuntimeError('Unknown legacy Weather helper ABI.')
    generated = {'service.sh': boot_script(selected, saved),
                 'targets.conf': ''.join('|'.join([saved['apk'], *[item[key] for key in
                                            ('payload', 'target', 'before', 'after')]]) + '\n'
                                         for item in saved['targets'])}
    checks = []
    for filename, body in generated.items():
        checksum = hashlib.sha256(body.encode()).hexdigest()
        checks.append(f'[ -f {directory}/{filename} ] && [ ! -L {directory}/{filename} ] || exit 1\n'
                      f'[ "$(sha256sum {directory}/{filename} | cut -d \' \' -f 1)" = {checksum} ] || exit 1')
    for item in saved['targets']:
        checks.append(f'[ -f {directory}/{item["payload"]} ] && [ ! -L {directory}/{item["payload"]} ] || exit 1\n'
                      f'[ "$(sha256sum {directory}/{item["payload"]} | cut -d \' \' -f 1)" = {item["after"]} ] || exit 1')
    root(config, '\n'.join(checks))
    return True


def bridge_options(selected):
    """Canonical data recipe for install and authenticated lifecycle changes."""
    tablet = selected['id'] == 'tablet-yingtian'
    libraries = [{'name': name, 'before': pair[0], 'after': pair[1], 'placeholder': False}
                 for name, pair in selected['libraries'].items()]
    libraries += [{'name': 'libhgl.so', 'before': EMPTY, 'after': BRIDGE_SHA256, 'placeholder': True}]
    if tablet:
        libraries += [{'name': name, 'before': EMPTY, 'after': checksum, 'placeholder': True}
                      for name, checksum in ANGLE.items()]
    recipe = {'id': selected['id'], 'apk_sha256': selected['apk_sha256'],
              'factory_apk': PAD_APK if tablet else '/product/app/MIUIWeather/MIUIWeather.apk',
              'native': PAD_NATIVE if tablet else '/product/app/MIUIWeather/lib/arm64',
              'libraries': libraries,
              'system_libraries': {} if tablet else {'/system/lib64/' + name: checksum
                                                    for name, checksum in ANGLE.items()}}
    return {'name': NAME, 'description': DESCRIPTION, 'package': PACKAGE, 'profiles': [recipe]}


def install(config, enable=False, sources=('official-hongkong-ota',), angle_folder=None):
    """Provision a portable bridge; APK reinstalls are reconciled inside the guest."""
    import tempfile
    import app_bridge_module
    official(config, sources=sources)
    module_id = MODULE.rsplit('/', 1)[1]
    selected = profile(json.loads((ROOT / 'local/build.json').read_text())['source'])
    # Check lifecycle before the current APK: an update must never override a
    # disabled or removed project module merely because an app was reinstalled.
    for directory in (MODULE, '/data/adb/modules_update/' + module_id):
        saved = app_bridge_module._inspect(config, directory, module_id)
        if saved and saved['flags']:
            if enable and saved['flags'] == ['disable'] and directory == MODULE:
                app_bridge_module.set_enabled(config, module_id, True, legacy=_legacy_module,
                                              **bridge_options(selected))
            else:
                print('Weather bridge lifecycle choice is preserved.', flush=True)
                return {'installed': False, 'disabled': True, 'flags': saved['flags']}
    source = REPO_ROOT / 'native/weather_angle.c'
    if not source.is_file() or source.is_symlink() or sha256(source) != BRIDGE_SOURCE_SHA256:
        raise RuntimeError('Weather bridge source differs from its verified build receipt.')
    paths = root(config, 'pm path ' + PACKAGE).splitlines()
    if len(paths) != 1 or not paths[0].startswith('package:'):
        raise UnsupportedPatch('Expected exactly one signed Weather base APK.')
    apk = paths[0].removeprefix('package:')
    tablet = selected['id'] == 'tablet-yingtian'
    if tablet and (root(config, 'id -u') != '0' or root(config, 'getprop sys.boot_completed') != '1'
                   or root(config, 'getprop ro.boot.hardware') != 'ranchu'
                   or root(config, 'getprop ro.product.device') != 'yingtian'):
        raise RuntimeError('Unsupported running tablet Weather firmware profile.')
    factory = PAD_APK if tablet else '/product/app/MIUIWeather/MIUIWeather.apk'
    native = PAD_NATIVE if tablet and apk == PAD_APK else str(Path(apk).parent / 'lib/arm64')
    if apk != factory:
        if not re.fullmatch(r'/data/app/(?:~~[A-Za-z0-9_=-]+/)?com\.miui\.weather2-[A-Za-z0-9_=-]+/base\.apk', apk):
            raise UnsupportedPatch('Unsupported Weather package layout; keeping its original driver.')
    if root(config, 'sha256sum ' + shlex.quote(apk)).split()[0] != selected['apk_sha256']:
        raise UnsupportedPatch('Unsupported Weather APK shader workload; refusing the ANGLE feature override.')
    # Read-only native preflight: extracted APK libraries or the exact baked
    # bridge are accepted, but unrelated files are never replaced or unmounted.
    for name, (before, after) in selected['libraries'].items():
        current = pad_native_hash(config, native + '/' + name)
        if not current or current.split()[0] not in (before, after):
            raise RuntimeError('Weather MGL native library differs from its signed workload: ' + name)
    existing = pad_native_hash(config, native + '/libhgl.so')
    if existing and existing.split()[0] not in (EMPTY, BRIDGE_SHA256):
        raise RuntimeError('Unrelated private Weather helper library is preserved.')
    with tempfile.TemporaryDirectory(prefix='hyperos-weather-bridge-') as temporary:
        folder = Path(temporary)
        original = folder / 'weather.apk'
        adb(config, 'pull', apk, str(original), check=True, capture_output=True, timeout=60)
        if sha256(original) != selected['apk_sha256']:
            raise RuntimeError('Exported Weather APK checksum mismatch.')
        payloads = {}
        with zipfile.ZipFile(original) as archive:
            for name, pair in selected['libraries'].items():
                destination = folder / name
                destination.write_bytes(patch(name, archive.read('lib/arm64-v8a/' + name),
                                              libraries=selected['libraries']))
                if sha256(destination) != pair[1]:
                    raise RuntimeError('Weather native output checksum mismatch.')
                payloads[name] = destination
        for name, checksum in ANGLE.items():
            destination = folder / name
            if angle_folder is not None:
                source = Path(angle_folder) / name
                if not source.is_file() or source.is_symlink() or sha256(source) != checksum:
                    raise RuntimeError('Unsupported private Weather ANGLE dependency: ' + name)
                shutil.copyfile(source, destination)
            else:
                adb(config, 'pull', '/system/lib64/' + name, str(destination),
                    check=True, capture_output=True, timeout=60)
            if sha256(destination) != checksum:
                raise RuntimeError('Unsupported private Weather ANGLE dependency: ' + name)
            if tablet:
                payloads[name] = destination
        # A present release bundle must authenticate even when a baked helper
        # could otherwise hide a damaged receipt or payload.
        prebuilt = ROOT / 'tools/weather-angle'
        if any((prebuilt / item).exists() or (prebuilt / item).is_symlink()
               for item in ('libhgl.so', 'receipt.json')):
            verify_bridge_prebuilt(prebuilt)
        helper_source = native + '/libhgl.so' if existing and existing.split()[0] == BRIDGE_SHA256 else None
        if helper_source is None and not tablet:
            factory_helper = '/product/app/MIUIWeather/lib/arm64/libhgl.so'
            factory_hash = pad_native_hash(config, factory)
            baked_hash = pad_native_hash(config, factory_helper)
            if (factory_hash and factory_hash.split()[0] == selected['apk_sha256']
                    and baked_hash and baked_hash.split()[0] == BRIDGE_SHA256):
                helper_source = factory_helper
        # Factory Phone images carry this pinned helper even after the signed
        # Weather APK is reinstalled at a new PackageManager path. No NDK is
        # needed to provision that portable workload's recovery module.
        if helper_source is not None:
            bridge = folder / 'libhgl.so'
            adb(config, 'pull', helper_source, str(bridge),
                check=True, capture_output=True, timeout=60)
        else:
            bridge = build_bridge(config, folder)
        if sha256(bridge) != BRIDGE_SHA256:
            raise RuntimeError('Weather bridge differs from its verified source/build receipt.')
        payloads['libhgl.so'] = bridge
        result = app_bridge_module.install(config, module_id=module_id,
                                           **bridge_options(selected), payloads=payloads,
                                           legacy=_legacy_module)
    (ROOT / 'local').mkdir(parents=True, exist_ok=True)
    (ROOT / 'local/weather-angle-fix.json').write_text(json.dumps(result, indent=2) + '\n')
    print('Weather bridge provisioned through KernelSU; normal boot activates automatic reinstall recovery.', flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--disable', action='store_true')
    group.add_argument('--enable', action='store_true')
    args = parser.parse_args()
    config = runtime()
    if args.disable:
        official(config, sources=('official-hongkong-ota', 'official-yingtian-ota'))
        from app_bridge_module import set_enabled
        selected = profile(json.loads((ROOT / 'local/build.json').read_text())['source'])
        set_enabled(config, MODULE.rsplit('/', 1)[1], False, legacy=_legacy_module,
                    **bridge_options(selected))
        print('Weather bridge disabled. Normal boot restores the image/package baseline.', flush=True)
    else:
        install(config, enable=args.enable, sources=('official-hongkong-ota', 'official-yingtian-ota'))


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        raise SystemExit(str(error))
