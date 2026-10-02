#!/usr/bin/env python3
"""Install version-guarded KernelSU overlays on the official OS4 AVD only."""
import argparse
import json
from pathlib import Path
import shlex
import subprocess
import zipfile
import hashlib

from common import ROOT, adb, runtime, sha256
from patch_flutter import digest, patch, profile

MODULE_ID = 'hyperos_avd_flutter_render'
MODULE = '/data/adb/modules/' + MODULE_ID
SYSTEM_LIB = '/system_ext/lib64/libhyper_os_flutter.so'
ENGINE_ENTRY = 'lib/arm64-v8a/libhyper_os_flutter.so'
PACKAGES = ('com.miui.home', 'com.miui.weather2')
REVISION = 6
EMPTY = hashlib.sha256(b'').hexdigest()

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
        bind_target 1 "$MODDIR/$payload" "$target" "$before" "$after" || exit 1
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
        if [ "$current" = "$apk_target" ]; then
            bind_target "$pid" "$MODDIR/$payload" "$target" "$before" "$after" || exit 1
        else
            log "APK changed: $package. Rerun apply_flutter_fix.py."
        fi
    done < "$MODDIR/apks.conf"
done
if [ "$changed" = 1 ]; then
    am force-stop com.miui.home
    am force-stop com.miui.weather2
fi
exit 0
'''


def root(config, command, **kwargs):
    result = adb(config, 'shell', 'su -W -c ' + shlex.quote('set -e\n' + command),
                 capture_output=True, text=True, timeout=60, **kwargs)
    if result.returncode:
        raise RuntimeError('Flutter overlay command failed: ' + result.stdout + result.stderr)
    return result.stdout.strip()


def official(config):
    build = ROOT / 'local/build.json'
    if (config['name'] != 'HyperOS_4_Official_API_37' or config['port'] != 5574
            or not build.is_file() or json.loads(build.read_text()).get('source') != 'official-hongkong-ota'):
        raise RuntimeError('This patch is restricted to the official OS4 AVD (emulator-5574).')


def rewrite_apk(source, destination, engine):
    """Produce a runtime overlay; never install or resign the user's APK."""
    with zipfile.ZipFile(source) as archive, zipfile.ZipFile(destination, 'w') as output:
        entries = archive.infolist()
        if sum(entry.filename == ENGINE_ENTRY for entry in entries) != 1:
            raise RuntimeError('Expected exactly one embedded Flutter engine.')
        for entry in entries:
            output.writestr(entry, engine if entry.filename == ENGINE_ENTRY else archive.read(entry))


def refresh_scripts(config):
    folder = ROOT / 'work/flutter-render-fix'
    folder.mkdir(parents=True, exist_ok=True)
    for name in ('post-fs-data.sh', 'service.sh'):
        local = folder / name
        local.write_text(BOOT_SCRIPT)
        remote = '/data/local/tmp/hyperos-render-' + name
        adb(config, 'push', str(local), remote, check=True, capture_output=True, timeout=30)
        root(config, f'cp {remote} {MODULE}/{name}.next\nchmod 755 {MODULE}/{name}.next\nmv {MODULE}/{name}.next {MODULE}/{name}\nrm {remote}')


def old_targets(manifest):
    if manifest.get('revision') == 1:
        return [{'target': SYSTEM_LIB, 'before': manifest['system_before'], 'after': manifest['system_after']},
                {'target': manifest['home_target'], 'before': manifest['home_before'], 'after': manifest['home_after']}]
    if manifest.get('revision') in (2, 3, 4, 5, REVISION):
        return [manifest['system'], *manifest['apks']]
    raise RuntimeError('Unknown existing Flutter overlay revision.')


def detach(config, manifest):
    """Detach only binds whose source belongs to this module."""
    paths = [item['target'] for item in old_targets(manifest)]
    commands = []
    for path in paths:
        if any(c in path for c in '\n\r|"') or not path.startswith(('/system_ext/', '/data/app/', '/product/')):
            raise RuntimeError('Invalid saved overlay target.')
        loop = (f'for attempt in 1 2 3 4; do '
                f"awk '$4 ~ /^\\/adb\\/modules\\/{MODULE_ID}\\// && $5 == \"{path}\" {{ found=1 }} END {{exit !found}}' /proc/self/mountinfo || break; "
                f'/data/adb/ksu/bin/busybox umount -l {shlex.quote(path)} || exit 1; done')
        commands.append('nsenter -t "$pid" -m -- sh -c ' + shlex.quote(loop))
    root(config, 'for pid in 1 $(getprop init.svc_debug_pid.hyos_spawner) $(pidof zygote64); do\n'
         '[ -d "/proc/$pid" ] || continue\n' + '\n'.join(commands) + '\ndone')
    for item in old_targets(manifest):
        if item.get('placeholder') or item.get('external'):
            path = shlex.quote(item['target'])
            root(config, f'if [ -f {path} ] && [ "$(sha256sum {path} | cut -d " " -f 1)" = {item["before"]} ]; then rm {path}; fi')


def install(config, enable=False):
    official(config)
    if root(config, 'id -u') != '0' or root(config, 'getprop sys.boot_completed') != '1':
        raise RuntimeError('Wait for boot completion and KernelSU root first.')
    targets = {pkg: root(config, 'pm path ' + pkg).splitlines()[0].removeprefix('package:') for pkg in PACKAGES}
    old_text = root(config, f'if [ -f {MODULE}/manifest.json ]; then cat {MODULE}/manifest.json; fi')
    old = json.loads(old_text) if old_text else None
    if old:
        old_targets(old)  # Validate revision before changing anything.
        disabled = root(config, f'if [ -f {MODULE}/disable ]; then echo disabled; fi')
        if disabled and not enable:
            print('Native Flutter overlay is disabled; keeping this choice.', flush=True)
            return old
        if old.get('revision') == REVISION and all('apk_target' in i for i in old['apks']) and old.get('packages', {i['package']: i.get('apk_target', i['target']) for i in old['apks']}) == targets:
            for item in old_targets(old):
                current = root(config, 'if [ -f ' + shlex.quote(item['target']) + ' ]; then sha256sum ' + shlex.quote(item['target']) + '; fi')
                current = current.split()[0] if current else ''
                if not current and item.get('external'):
                    continue
                if current not in (item['before'], item['after']):
                    raise RuntimeError('Overlay target changed unexpectedly: ' + item['target'])
            if not all('apk_target' in i for i in old['apks']):
                raise RuntimeError('Overlay schema requires rebuilding this module.')
            refresh_scripts(config)
            root(config, f'rm -f {MODULE}/disable\nsh {MODULE}/service.sh')
            print('Native Flutter overlays are ready.', flush=True)
            return old
    folder = ROOT / 'work/flutter-render-fix'
    folder.mkdir(parents=True, exist_ok=True)
    original_lib = folder / 'system-current.so'
    adb(config, 'pull', SYSTEM_LIB, str(original_lib), check=True, capture_output=True, timeout=60)
    lib = original_lib.read_bytes()
    before, system_profile = profile(lib)
    if system_profile['name'] != 'system-v3':
        raise RuntimeError('Unexpected system Flutter engine.')
    (folder / 'flutter.so').write_bytes(patch(lib))
    system = {'target': SYSTEM_LIB, 'before': before, 'after': system_profile['output']}
    apks = []
    apk_hashes = {}
    for pkg, target in targets.items():
        if any(c in target for c in '\n\r|') or not target.startswith(('/data/app/', '/system_ext/', '/product/')):
            raise RuntimeError('Unexpected APK location: ' + target)
        original = folder / (pkg + '-current.apk')
        adb(config, 'pull', target, str(original), check=True, capture_output=True, timeout=60)
        actual = sha256(original)
        apk_hashes[pkg] = actual
        saved = folder / (pkg + '-' + actual + '.apk')
        if not saved.exists():
            saved.write_bytes(original.read_bytes())
        payload = pkg + '.apk'
        fixed = folder / payload
        with zipfile.ZipFile(original) as archive:
            if ENGINE_ENTRY not in archive.namelist():
                continue  # Factory Rust apps use the patched shared system engine.
            raw = archive.read(ENGINE_ENTRY)
            engine = patch(raw)
        native_target = str(Path(target).parent / 'lib/arm64/libhyper_os_flutter.so')
        extracted = root(config, 'if [ -f ' + shlex.quote(native_target) + ' ]; then echo yes; fi') == 'yes'
        previous_placeholder = any(i['target'] == native_target and (i.get('placeholder') or i.get('external')) for i in old_targets(old)) if old else False
        if target.startswith('/data/app/') and (not extracted or previous_placeholder):
            # Rust's linker namespace searches nativeLibraryDir before the APK.
            # Supply only the engine there; keep the signed APK byte-identical.
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
                raise RuntimeError('Install Android SDK Build-Tools with zipalign for an APK overlay.')
            unaligned = folder / (pkg + '-unaligned.apk')
            rewrite_apk(original, unaligned, engine)
            subprocess.run([str(candidates[0]), '-f', '-P', '16', '4', str(unaligned), str(fixed)], check=True)
            subprocess.run([str(candidates[0]), '-c', '-P', '16', '4', str(fixed)], check=True, capture_output=True)
        with zipfile.ZipFile(fixed) as archive:
            if digest(archive.read(ENGINE_ENTRY)) != digest(engine):
                raise RuntimeError('Aligned APK engine checksum mismatch.')
        before = actual
        for previous in old_targets(old) if old else []:
            if previous['target'] == target and previous['after'] == actual:
                before = previous['before']
        apks.append({'package': pkg, 'apk_target': target, 'payload': payload, 'target': target, 'before': before, 'after': sha256(fixed)})
    for item in apks:
        item['apk_sha256'] = apk_hashes[item['package']]
    manifest = {'revision': REVISION, 'system': system, 'apks': apks, 'packages': targets}
    values = {'SYSTEM_BEFORE': system['before'], 'SYSTEM_AFTER': system['after']}
    (folder / 'targets.conf').write_text(''.join(k + '=' + shlex.quote(v) + '\n' for k, v in values.items()))
    (folder / 'apks.conf').write_text(''.join('|'.join(i[k] for k in ('package', 'payload', 'apk_target', 'target', 'before', 'after', 'apk_sha256')) + '\n' for i in apks))
    (folder / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    (folder / 'module.prop').write_text(f'id={MODULE_ID}\nname=HyperOS AVD Flutter render fix\nversion={REVISION}\nversionCode={REVISION}\nauthor=HyperOS-AVD\ndescription=Native depth, Float16, storage alignment and dispersion shadow fix for the official ARM64 AVD\n')
    for name in ('post-fs-data.sh', 'service.sh'):
        (folder / name).write_text(BOOT_SCRIPT)
    # Validate every input before replacing the previous module. Keep its backup.
    stage = '/data/adb/hyperos-render-stage-v' + str(REVISION) + '-' + system['after'][:12]
    pending = root(config, f'if [ -d {stage} ]; then cat {stage}/manifest.json; fi')
    if pending and json.loads(pending) != manifest:
        raise RuntimeError('An unrelated Flutter staging directory exists.')
    root(config, f'mkdir -p /data/adb/modules {stage}')
    originals = [(i['payload'] + '.original', i['before']) for i in apks if i.get('external')]
    files = ['flutter.so', 'targets.conf', 'apks.conf', 'manifest.json', 'module.prop', 'post-fs-data.sh', 'service.sh', *[i['payload'] for i in apks], *[name for name, _ in originals]]
    for name in files:
        remote = '/data/local/tmp/hyperos-render-' + name
        adb(config, 'push', str(folder / name), remote, check=True, capture_output=True, timeout=60)
        mode = '755' if name.endswith('.sh') else '644'
        root(config, f'cp {shlex.quote(remote)} {stage}/{name}\nchmod {mode} {stage}/{name}\nrm {shlex.quote(remote)}')
    root(config, f'chcon u:object_r:system_lib_file:s0 {stage}/flutter.so\n' + '\n'.join(f'chcon u:object_r:apk_data_file:s0 {stage}/{i["payload"]}' for i in apks))
    for name, _ in originals:
        root(config, f'chcon u:object_r:apk_data_file:s0 {stage}/{name}')
    for name, checksum in [('flutter.so', system['after']), *[(i['payload'], i['after']) for i in apks], *originals]:
        if root(config, f'sha256sum {stage}/{name}').split()[0] != checksum:
            raise RuntimeError('Staged overlay checksum mismatch: ' + name)
    if old:
        detach(config, old)
        root(config, f'mv {MODULE} /data/adb/hyperos-render-backup-$(date +%s)')
    root(config, f'test ! -e {MODULE}\nmv {stage} {MODULE}\nsh {MODULE}/service.sh')
    (ROOT / 'local/flutter-render-fix.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print('Native Flutter fixes installed for system, launcher and weather. Original APKs and userdata were preserved.', flush=True)
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
