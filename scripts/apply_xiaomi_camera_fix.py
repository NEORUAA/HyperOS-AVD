#!/usr/bin/env python3
"""Build and install experimental bridges for the pinned official Xiaomi camera."""
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
from module_lifecycle import (preserved as lifecycle_preserved, mutation_guard,
                              guarded_hook, refresh_hooks)
from common import ROOT, REPO_ROOT, adb, runtime, sha256
from apply_flutter_fix import official, root

MODULE = '/data/adb/modules/hyperos_avd_xiaomi_camera'
APP = '/product/priv-app/MiuiCamera'
APK = APP + '/MiuiCamera.apk'
APK_HASH = 'd378b495e71f6fa416adf3513e6d56acee4c8309371ac837f0bc28c9a0f7fca7'
PROVIDER = '/vendor/bin/hw/android.hardware.camera.provider@2.7-service-google'
PROVIDER_HASH = '1424c5afc7f60824a640896d42af131d06ac0fc5eaa2e4985f804f43451c1068'
PROVIDER_AFTER = 'b8b09bf20e4cb786e31837e2e6ed597e43614b71042f28511a68f6d6867f7889'
PROVIDER_REV1 = '39563bee97520e732154027ca84caa05db1758b26c535e5b274c0537d0610a0c'
HWL = '/vendor/lib64/libgooglecamerahwl_impl.so'
HWL_HASH = 'e9566298975e3346c5c5a797f0a96bf48f5901b2391fae4b56b58e9befb8cb83'
CPP_HASH = '2267f93b8b3c9d1967f1833d5f71c7312213c43bb291250cf772800763037fb9'
RUNTIME_HASH = '22b624d7bfa130cb66ad841b0af152d975c5329449dd804e077d106c57a89c3a'
YUV_HASH = '51f3c6ec355423d9013dfdbac42d001a7c146bf30c84d650d962edbbc04450b8'
APK18_HASH = '729b0b193d1684e7c14ed0ca56ba15247d58016c70164329de233b49727a2c48'
# All referenced Java members (including access flags) are identical in these
# two pinned APKs; notifyCaptureFailed's complete smali body is also identical.
CAPTURE_ABI = 'edff86b65363d64ff6004d477a52ed32b2fa34db00a64d7c7adaf172a82de0a8'
CAMERA_VERSIONS = {APK_HASH: '4.0.17.0.XFRCNXM', APK18_HASH: '4.0.18.0.XFRCNXM'}
SOURCES = {
    'camera_hwl_roles.cpp': 'ef601e79de5cc2c786b8a3004b70b9cff77e560a72ba476b91a1f30fc2fc4ca1',
    'camera_yuv_planes.c': '12db8e944726863f2daec400f2e725bfbc1cdd18f3b9ad7bbd14da69a7823074',
}
PAYLOADS = {
    'provider': PROVIDER_AFTER,
    'hwl.so': '812cc14c4cfb23d6401277dd90c795729654e9b3c2e818165748785172035e84',
    'yuv.so': '286b2c5abc6eee0243035b862b4ff26e131a013ccca9d0fa43f69828fc8d50c8',
}
OFFSET = 0x29b1c
BEFORE = bytes.fromhex('1f2003d5f322ef10e2fefff042040991e3fefff063740891c0008052e10313aa85030094')
AFTER = bytes.fromhex('a80092529f00086b80000054880090529f00086b61000054e4031f2ad8ffff171322ef10')


def patch_provider(data):
    """Normalize only Xiaomi photo 0x9005/video 0x8004; preserve other paths."""
    if hashlib.sha256(data).hexdigest() != PROVIDER_HASH or data[OFFSET:OFFSET+len(BEFORE)] != BEFORE:
        raise RuntimeError('Unsupported Google camera provider; refusing binary offsets.')
    result = data[:OFFSET] + AFTER + data[OFFSET+len(BEFORE):]
    if hashlib.sha256(result).hexdigest() != PROVIDER_AFTER:
        raise RuntimeError('Camera provider patch verification failed.')
    return result


def camera_inputs(firmware=None):
    from phone_profile import profile_from_build
    firmware = profile_from_build() if firmware is None else firmware
    apk = firmware['pins']['camera_apk']
    if (CAMERA_VERSIONS.get(apk) != firmware['hyperos']
            or firmware['incremental'] != 'OS' + firmware['hyperos']
            or firmware['pins']['android_runtime'] != RUNTIME_HASH):
        raise RuntimeError('Unsupported Xiaomi camera/framework ABI profile.')
    return {'apk_sha256': apk, 'runtime_sha256': RUNTIME_HASH,
            'firmware': {'source': 'official-hongkong-ota', 'incremental': firmware['incremental']},
            'native_abi': {'jni_sha256': YUV_HASH, 'capture_contract_sha256': CAPTURE_ABI}}


def normalized_manifest(manifest, firmware=None):
    """Adopt a verified equivalent APK without changing native payload hashes."""
    selected = camera_inputs(firmware)
    if (manifest.get('revision') != 2 or manifest.get('experimental') is not True
            or manifest.get('apk_sha256') not in CAMERA_VERSIONS
            or manifest.get('runtime_sha256') != RUNTIME_HASH
            or len(manifest.get('targets', [])) != 2
            or {item.get('target'): item.get('before') for item in manifest['targets']} != {
                PROVIDER: PROVIDER_HASH, HWL: HWL_HASH}
            or {item.get('target'): item.get('payload') for item in manifest['targets']} != {
                PROVIDER: 'provider', HWL: 'hwl.so'}
            or any(not isinstance(item.get('after'), str)
                   or not re.fullmatch(r'[0-9a-f]{64}', item['after'])
                   for item in manifest['targets'])
            or not isinstance(manifest.get('yuv_sha256'), str)
            or not re.fullmatch(r'[0-9a-f]{64}', manifest['yuv_sha256'])
            or 'native_abi' in manifest and manifest['native_abi'] != selected['native_abi']
            or 'firmware' in manifest and manifest['firmware'] != {
                'source': 'official-hongkong-ota',
                'incremental': 'OS' + CAMERA_VERSIONS.get(manifest.get('apk_sha256'), '')}):
        raise RuntimeError('Unsupported precompiled camera manifest.')
    return {**manifest, **selected}


def verify_universal_prebuilt(folder):
    """Authenticate an existing producer bundle for the common content catalog."""
    folder = Path(folder)
    if not folder.is_dir() or folder.is_symlink():
        raise RuntimeError('Invalid precompiled Xiaomi camera directory.')
    for name, checksum in SOURCES.items():
        source = REPO_ROOT / 'native' / name
        if (not source.is_file() or source.is_symlink() or source.stat().st_nlink != 1
                or sha256(source) != checksum):
            raise RuntimeError('Xiaomi camera source differs from its audited producer.')
    names = {*PAYLOADS, 'manifest.json', 'receipt.json'}
    for name in names:
        path = folder / name
        if not path.is_file() or path.is_symlink() or path.stat().st_nlink != 1:
            raise RuntimeError('Invalid precompiled Xiaomi camera asset: ' + name)
    try:
        receipt = json.loads((folder / 'receipt.json').read_text())
        saved = json.loads((folder / 'manifest.json').read_text())
        from phone_profile import profile
        selected = profile(CAMERA_VERSIONS[saved['apk_sha256']])
        normalized = normalized_manifest(saved, selected)
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise RuntimeError('Invalid precompiled Xiaomi camera recipe.') from error
    expected = {**PAYLOADS, 'manifest.json': sha256(folder / 'manifest.json')}
    if (receipt != {'sources': SOURCES, 'files': expected}
            or normalized['yuv_sha256'] != PAYLOADS['yuv.so']
            or {item['payload']: item['after'] for item in normalized['targets']} != {
                name: PAYLOADS[name] for name in ('provider', 'hwl.so')}):
        raise RuntimeError('Unknown precompiled Xiaomi camera recipe or receipt.')
    for name, checksum in PAYLOADS.items():
        if sha256(folder / name) != checksum:
            raise RuntimeError('Precompiled Xiaomi camera payload changed: ' + name)
    return normalized


def build(sdk, folder, firmware=None):
    """Embed originals without modifying or re-signing the Xiaomi APK."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    selected = camera_inputs(firmware)
    prebuilt = ROOT / 'tools/xiaomi-camera'
    if (prebuilt / 'receipt.json').is_file():
        receipt = json.loads((prebuilt / 'receipt.json').read_text())
        sources = {name: sha256(REPO_ROOT / 'native' / name) for name in
                   ('camera_hwl_roles.cpp', 'camera_yuv_planes.c')}
        if receipt.get('sources') != sources:
            raise RuntimeError('Camera bridge source differs from the release binary.')
        if set(receipt.get('files', {})) != {'provider', 'hwl.so', 'yuv.so', 'manifest.json'}:
            raise RuntimeError('Incomplete precompiled camera bridge receipt.')
        for name, checksum in receipt['files'].items():
            if name not in ('provider', 'hwl.so', 'yuv.so', 'manifest.json') or sha256(prebuilt / name) != checksum:
                raise RuntimeError('Invalid precompiled camera bridge.')
            shutil.copy2(prebuilt / name, folder / name)
        manifest = normalized_manifest(json.loads((folder / 'manifest.json').read_text()), firmware)
        if ({item['payload']: item['after'] for item in manifest['targets']} != {
                'provider': receipt['files']['provider'], 'hwl.so': receipt['files']['hwl.so']}
                or manifest['yuv_sha256'] != receipt['files']['yuv.so']):
            raise RuntimeError('Precompiled camera manifest differs from its verified payloads.')
        (folder / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
        return manifest
    originals = folder / 'original'
    originals.mkdir(parents=True, exist_ok=True)
    expected = {'provider': PROVIDER_HASH, 'hwl.so': HWL_HASH, 'libc++.so': CPP_HASH,
                'camera.apk': selected['apk_sha256']}
    for name, checksum in expected.items():
        if not (originals / name).is_file() or sha256(originals / name) != checksum:
            raise RuntimeError('Missing verified camera build input: ' + name)
    with zipfile.ZipFile(originals / 'camera.apk') as archive:
        yuv = archive.read('lib/arm64-v8a/libcamera_yuv_jni.so')
    if hashlib.sha256(yuv).hexdigest() != YUV_HASH:
        raise RuntimeError('Unsupported Xiaomi camera JNI ABI.')
    (originals / 'yuv.so').write_bytes(yuv)
    compilers = list((Path(sdk) / 'ndk').glob(
        '*/toolchains/llvm/prebuilt/darwin-*/bin/aarch64-linux-android36-clang'))
    if not compilers:
        raise RuntimeError('Install Android NDK with API 36 to build the camera bridges.')
    compiler = max(compilers, key=lambda p: tuple(int(v) for v in p.parents[5].name.split('.')))
    for output, source, embedded, cpp, alignment in (
            ('hwl.so', 'camera_hwl_roles.cpp', 'hwl.so', True, 4096),
            ('yuv.so', 'camera_yuv_planes.c', 'yuv.so', False, 16384)):
        assembly = folder / (output + '.S')
        # Pass relative incbin paths through an explicit working directory.
        assembly.write_text('.section .rodata.avd_original,"a",@progbits\n'
                            f'.balign {alignment}\n.global avd_original_start\n.hidden avd_original_start\n'
                            f'avd_original_start:\n.incbin "original/{embedded}"\n'
                            '.global avd_original_end\n.hidden avd_original_end\navd_original_end:\n')
        command = [str(compiler) + ('++' if cpp else ''),
                   str(REPO_ROOT / 'native' / source), assembly.name,
                   '-shared', '-fPIC', '-O2', '-Wall', '-Wextra', '-Werror',
                   '-llog', '-ldl', f'-Wl,-z,max-page-size={alignment}',
                   '-Wl,-soname,' + ('libgooglecamerahwl_impl.so' if cpp else 'libcamera_yuv_jni.so'),
                   '-o', output]
        if cpp:
            command += ['-D_LIBCPP_ABI_NAMESPACE=__1', '-nostdlib++', 'original/libc++.so']
        subprocess.run(command, cwd=folder, check=True)
    (folder / 'provider').write_bytes(patch_provider((originals / 'provider').read_bytes()))
    manifest = {'revision': 2, 'experimental': True, **selected,
                'targets': [{'payload': payload, 'target': target, 'before': before,
                             'after': sha256(folder / payload)}
                            for payload, target, before in (
                                ('provider', PROVIDER, PROVIDER_HASH), ('hwl.so', HWL, HWL_HASH))],
                'yuv_sha256': sha256(folder / 'yuv.so')}
    (folder / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    return manifest


MOUNT_SCRIPT = r'''#!/system/bin/sh
set -eu
MODDIR=${0%/*}
BB=/data/adb/ksu/bin/busybox
[ ! -f "$MODDIR/disable" ] || exit 0
[ ! -f "$MODDIR/remove" ] || exit 0
[ "$(getprop ro.mi.os.version.incremental)" = @INCREMENTAL@ ] || exit 1
[ "$(getprop ro.miui.product.home)" = com.miui.home ] || exit 1
[ "$(getprop ro.boot.hardware)" = ranchu ] || exit 1
[ "$(sha256sum /vendor/lib64/libc++.so | cut -d ' ' -f 1)" = @CPP@ ] || exit 1
[ "$(sha256sum /system/lib64/libandroid_runtime.so | cut -d ' ' -f 1)" = @RUNTIME@ ] || exit 1
changed=0
# Validate every namespace before the first HAL or directory mount.
for pid in 1 $(getprop init.svc_debug_pid.hyos_spawner) $(pidof zygote64); do
    [ -d "/proc/$pid" ] || continue
    [ "$(nsenter -t "$pid" -m -- "$BB" sha256sum @APK@ | "$BB" cut -d ' ' -f 1)" = @APK_HASH@ ] || exit 1
done
[ "$(sha256sum "$MODDIR/app/MiuiCamera.apk" | cut -d ' ' -f 1)" = @APK_HASH@ ] || exit 1
[ "$(sha256sum "$MODDIR/app/lib/arm64/libcamera_yuv_jni.so" | cut -d ' ' -f 1)" = @YUV@ ] || exit 1
for pid in 1 $(getprop init.svc_debug_pid.hyos_spawner) $(pidof zygote64); do
    [ -d "/proc/$pid" ] || continue
    while IFS='|' read -r payload target before after; do
        actual=$(nsenter -t "$pid" -m -- "$BB" sha256sum "$target" | "$BB" cut -d ' ' -f 1)
        [ "$actual" != "$after" ] || continue
        [ "$actual" = "$before" ] || exit 1
        [ "$(sha256sum "$MODDIR/$payload" | cut -d ' ' -f 1)" = "$after" ] || exit 1
        nsenter -t "$pid" -m -- "$BB" mount -o bind "$MODDIR/$payload" "$target"
        flags=ro
        [ "$payload" != provider ] || flags=ro,suid,exec
        nsenter -t "$pid" -m -- "$BB" mount -o remount,bind,$flags "$target"
        changed=1
    done < "$MODDIR/targets.conf"
    [ "$(nsenter -t "$pid" -m -- "$BB" sha256sum @APK@ | "$BB" cut -d ' ' -f 1)" = @APK_HASH@ ] || exit 1
    target=@APP@/lib/arm64/libcamera_yuv_jni.so
    actual=$(nsenter -t "$pid" -m -- sh -c 'if [ -f "$1" ]; then sha256sum "$1"; fi' sh "$target" | "$BB" cut -d ' ' -f 1)
    if [ "$actual" != @YUV@ ]; then
        [ -z "$actual" ] || exit 1
        [ "$(sha256sum "$MODDIR/app/MiuiCamera.apk" | cut -d ' ' -f 1)" = @APK_HASH@ ] || exit 1
        [ "$(sha256sum "$MODDIR/app/lib/arm64/libcamera_yuv_jni.so" | cut -d ' ' -f 1)" = @YUV@ ] || exit 1
        nsenter -t "$pid" -m -- "$BB" mount -o bind "$MODDIR/app" @APP@
        nsenter -t "$pid" -m -- "$BB" mount -o remount,bind,ro @APP@
        changed=1
    fi
done
if [ "$changed" = 1 ]; then
    stop vendor.camera-provider-2-7-google
    start vendor.camera-provider-2-7-google
fi
log -t HyperOSAVDCamera 'Verified camera overlays installed; original signed APK preserved.'
'''

ANGLE_SCRIPT = r'''#!/system/bin/sh
set -eu
MODDIR=${0%/*}
[ ! -f "$MODDIR/disable" ] || exit 0
[ ! -f "$MODDIR/remove" ] || exit 0
count=0
while [ "$(getprop sys.boot_completed)" != 1 ]; do
    count=$((count+1)); [ "$count" -lt 300 ] || exit 1; sleep 1
done
sh "$MODDIR/post-fs-data.sh"
pkgs=$(settings get global angle_gl_driver_selection_pkgs)
vals=$(settings get global angle_gl_driver_selection_values)
[ "$pkgs" != null ] || pkgs=
[ "$vals" != null ] || vals=
bb=/data/adb/ksu/bin/busybox
count=$(echo "$pkgs" | "$bb" awk -F, '{print NF}')
vcount=$(echo "$vals" | "$bb" awk -F, '{print NF}')
[ "$count" = "$vcount" ] || exit 1
index=$(echo "$pkgs" | "$bb" awk -F, '{for(i=1;i<=NF;i++) if($i=="com.android.camera") print i}')
if [ -z "$index" ]; then
    pkgs=${pkgs:+$pkgs,}com.android.camera
    vals=${vals:+$vals,}angle
else
    vals=$(echo "$vals" | "$bb" awk -v idx="$index" 'BEGIN{FS=OFS=","} {$idx="angle"; print}')
fi
settings put global angle_gl_driver_selection_pkgs "$pkgs"
settings put global angle_gl_driver_selection_values "$vals"
features=$(settings get global angle_egl_features)
[ "$features" != null ] || features=
case ",$features," in *,exposeES32ForTesting,*) ;; *)
    settings put global angle_egl_features "${features:+$features,}exposeES32ForTesting" ;;
esac
'''


def write_module_scripts(folder, manifest, firmware=None, *, lifecycle=True):
    selected = camera_inputs(firmware)
    if any(manifest.get(key) != selected[key] for key in ('apk_sha256', 'runtime_sha256')):
        raise RuntimeError('Camera startup profile differs from its payload manifest.')
    script = MOUNT_SCRIPT
    for key, value in {'CPP': CPP_HASH, 'RUNTIME': selected['runtime_sha256'], 'APK': APK,
                       'APP': APP, 'APK_HASH': selected['apk_sha256'], 'YUV': manifest['yuv_sha256'],
                       'INCREMENTAL': selected['firmware']['incremental']}.items():
        script = script.replace('@' + key + '@', value)
    (folder / 'post-fs-data.sh').write_text(guarded_hook(script) if lifecycle else script)
    (folder / 'service.sh').write_text(guarded_hook(ANGLE_SCRIPT) if lifecycle else ANGLE_SCRIPT)
    (folder / 'targets.conf').write_text(''.join('|'.join(item[k] for k in
        ('payload', 'target', 'before', 'after')) + '\n' for item in manifest['targets']))
    (folder / 'module.prop').write_text('id=hyperos_avd_xiaomi_camera\nname=HyperOS AVD Xiaomi camera bridge\n'
        'version=2\nversionCode=2\nauthor=HyperOS-AVD\n'
        'description=Experimental ranchu photo/video stream, role and private YUV compatibility\n')


def provider_upgrade(saved, newer):
    """Allow only the verified revision 1 to 2 provider update in place."""
    if saved['targets'] == newer['targets']:
        return False
    previous = {item['target']: item for item in saved['targets']}
    current = {item['target']: item for item in newer['targets']}
    if (previous.get(HWL) != current.get(HWL)
            or previous.get(PROVIDER) != {'payload': 'provider', 'target': PROVIDER,
                                          'before': PROVIDER_HASH, 'after': PROVIDER_REV1}
            or current.get(PROVIDER) != {'payload': 'provider', 'target': PROVIDER,
                                         'before': PROVIDER_HASH, 'after': PROVIDER_AFTER}):
        raise RuntimeError('Unsupported HAL payload change; refusing an in-place update.')
    return True


def upgrade_provider_script():
    """Detach only our pinned provider mounts before replacing their inode."""
    return f'''bb=/data/adb/ksu/bin/busybox
pids="1 $(getprop init.svc_debug_pid.hyos_spawner) $(pidof zygote64)"
for pid in $pids; do
    [ -d /proc/$pid ] || continue
    actual=$(nsenter -t "$pid" -m -- "$bb" sha256sum {PROVIDER} | "$bb" cut -d ' ' -f 1)
    case "$actual" in
        {PROVIDER_HASH}) ;;
        {PROVIDER_REV1})
            "$bb" awk '$4 == "/adb/modules/hyperos_avd_xiaomi_camera/provider" && $5 == "{PROVIDER}" {{found=1}} END {{exit !found}}' /proc/$pid/mountinfo ;;
        *) exit 1 ;;
    esac
done
test "$(sha256sum {MODULE}/provider | cut -d ' ' -f 1)" = {PROVIDER_REV1}
test "$(sha256sum {MODULE}/provider.next | cut -d ' ' -f 1)" = {PROVIDER_AFTER}
stop vendor.camera-provider-2-7-google
trap 'start vendor.camera-provider-2-7-google' EXIT
for pid in $pids; do
    [ -d /proc/$pid ] || continue
    actual=$(nsenter -t "$pid" -m -- "$bb" sha256sum {PROVIDER} | "$bb" cut -d ' ' -f 1)
    if [ "$actual" = {PROVIDER_REV1} ]; then
        nsenter -t "$pid" -m -- "$bb" umount {PROVIDER}
    fi
    test "$(nsenter -t "$pid" -m -- "$bb" sha256sum {PROVIDER} | "$bb" cut -d ' ' -f 1)" = {PROVIDER_HASH}
done
chmod 755 {MODULE}/provider.next
chcon u:object_r:hal_camera_default_exec:s0 {MODULE}/provider.next
mv {MODULE}/provider.next {MODULE}/provider
'''


def migrate_camera_app(config, saved, newer, folder, firmware):
    """Replace only our old factory-app copy after native/Java ABI verification."""
    normalized_manifest(saved, firmware)
    if (saved.get('revision') != 2 or saved.get('apk_sha256') != APK_HASH
            or newer.get('apk_sha256') != APK18_HASH
            or saved.get('runtime_sha256') != RUNTIME_HASH
            or saved.get('targets') != newer.get('targets')
            or saved.get('yuv_sha256') != newer.get('yuv_sha256')):
        raise RuntimeError('Unsupported owned camera firmware migration.')
    prop = root(config, f'cat {MODULE}/module.prop')
    if 'id=hyperos_avd_xiaomi_camera\n' not in prop or 'author=HyperOS-AVD' not in prop.splitlines():
        raise RuntimeError('Refused unrelated camera module ownership.')
    allowed = {'provider', 'hwl.so', 'app', 'manifest.json', 'module.prop', 'targets.conf',
               'post-fs-data.sh', 'service.sh', 'skip_mount', 'disable', 'remove'}
    layout = set(root(config, f'ls -A {MODULE}').splitlines())
    if layout - allowed or not {'provider', 'hwl.so', 'app', 'manifest.json', 'module.prop'} <= layout:
        raise RuntimeError('Unknown owned camera module layout.')
    root(config, f'test -z "$(find {MODULE} -maxdepth 1 -type l -print)"')
    for path, checksum in ((MODULE + '/app/MiuiCamera.apk', saved['apk_sha256']),
                           (MODULE + '/app/lib/arm64/libcamera_yuv_jni.so', saved['yuv_sha256']),
                           *[(MODULE + '/' + item['payload'], item['after']) for item in saved['targets']]):
        if root(config, 'sha256sum ' + path).split()[0] != checksum:
            raise RuntimeError('Owned camera migration input changed: ' + path)
    # The old startup guards must have kept this new signed factory app visible.
    root(config, f'test ! -e {APP}/lib/arm64/libcamera_yuv_jni.so')
    write_module_scripts(folder, newer, firmware)
    stage = '/data/adb/hyperos-xiaomi-camera-upgrade-stage'
    root(config, 'set -e\n' + mutation_guard(MODULE) + f'test ! -e {stage}\nmkdir -p {stage}')
    for name in ('provider', 'hwl.so', 'yuv.so', 'manifest.json', 'module.prop',
                 'targets.conf', 'post-fs-data.sh', 'service.sh'):
        temporary = '/data/local/tmp/hyperos-camera-upgrade-' + name
        adb(config, 'push', str(folder / name), temporary, check=True, capture_output=True, timeout=60)
        root(config, 'set -e\n' + mutation_guard(MODULE) + f'cp {shlex.quote(temporary)} {stage}/{name}\nrm {shlex.quote(temporary)}')
    checks = '\n'.join('test "$(sha256sum ' + stage + '/' + item['payload']
                       + ' | cut -d " " -f 1)" = ' + item['after'] for item in newer['targets'])
    root(config, 'set -eu\n' + mutation_guard(MODULE) + f'''am force-stop com.android.camera
mkdir {stage}/app
cp -a {APP}/. {stage}/app/
mkdir -p {stage}/app/lib/arm64
mv {stage}/yuv.so {stage}/app/lib/arm64/libcamera_yuv_jni.so
test "$(sha256sum {stage}/app/MiuiCamera.apk | cut -d ' ' -f 1)" = {newer['apk_sha256']}
test "$(sha256sum {stage}/app/lib/arm64/libcamera_yuv_jni.so | cut -d ' ' -f 1)" = {newer['yuv_sha256']}
{checks}
chcon -R u:object_r:system_file:s0 {stage}/app
chmod 644 {stage}/app/lib/arm64/libcamera_yuv_jni.so
chmod 755 {stage}/provider {stage}/*.sh
chcon u:object_r:hal_camera_default_exec:s0 {stage}/provider
chcon u:object_r:vendor_file:s0 {stage}/hwl.so
touch {stage}/skip_mount
for flag in disable remove; do
    [ ! -e {MODULE}/$flag ] || cp -p {MODULE}/$flag {stage}/$flag
done
backup=/data/adb/hyperos-xiaomi-camera-backup-$(date +%s)
test ! -e "$backup"
mv {MODULE} "$backup"
trap '[ -e {MODULE} ] || mv "$backup" {MODULE}' EXIT
mv {stage} {MODULE}
trap - EXIT
sh {MODULE}/post-fs-data.sh
sh {MODULE}/service.sh''')
    (ROOT / 'local/xiaomi-camera-fix.json').write_text(json.dumps(newer, indent=2) + '\n')
    print('Owned Xiaomi camera copy upgraded; native payloads, module choice and app userdata preserved.', flush=True)
    return newer


def install(config, rebuild=False):
    official(config)
    lifecycle = lifecycle_preserved(root, config, MODULE)
    if lifecycle:
        return lifecycle
    from phone_profile import profile_from_build
    firmware = profile_from_build()
    selected = camera_inputs(firmware)
    for key, value in {'ro.boot.qemu.avd_name': config['name'], 'ro.boot.hardware': 'ranchu',
                       'ro.mi.os.version.incremental': firmware['incremental']}.items():
        if root(config, 'getprop ' + key) != value:
            raise RuntimeError('Xiaomi camera bridge refused this device: ' + key)
    if root(config, 'pm path com.android.camera') != 'package:' + APK:
        raise UnsupportedPatch('Xiaomi camera update is unsupported; keep the original system version.')
    for path, checksum in ((APK, selected['apk_sha256']), ('/system/lib64/libandroid_runtime.so', selected['runtime_sha256']),
                           ('/vendor/lib64/libc++.so', CPP_HASH)):
        if root(config, 'sha256sum ' + path).split()[0] != checksum:
            raise UnsupportedPatch('Unsupported camera input: ' + path)
    existing = root(config, f'if [ -d {MODULE} ]; then echo yes; fi')
    if existing:
        saved = json.loads(root(config, f'cat {MODULE}/manifest.json'))
        if (saved.get('revision') not in (1, 2) or saved.get('apk_sha256') not in CAMERA_VERSIONS
                or saved.get('runtime_sha256') != RUNTIME_HASH):
            raise RuntimeError('Unknown existing camera module.')
        expected = {PROVIDER: PROVIDER_HASH, HWL: HWL_HASH}
        if (len(saved.get('targets', [])) != 2
                or {item.get('target'): item.get('before') for item in saved['targets']} != expected
                or any(len(item.get('after', '')) != 64 or
                       any(c not in '0123456789abcdef' for c in item['after'])
                       for item in saved['targets'])):
            raise RuntimeError('Invalid saved camera overlay targets.')
        if saved['apk_sha256'] != selected['apk_sha256']:
            folder = ROOT / 'work/xiaomi-camera-fix'
            newer = build(config['sdk'], folder, firmware)
            return migrate_camera_app(config, saved, newer, folder, firmware)
        for item in saved['targets']:
            if root(config, 'sha256sum ' + item['target']).split()[0] not in (item['before'], item['after']):
                raise RuntimeError('Camera overlay target changed: ' + item['target'])
        if rebuild:
            folder = ROOT / 'work/xiaomi-camera-fix'
            newer = build(config['sdk'], folder, firmware)
            update_provider = provider_upgrade(saved, newer)
            target = MODULE + '/app/lib/arm64/libcamera_yuv_jni.so'
            if root(config, 'sha256sum ' + target).split()[0] != saved['yuv_sha256']:
                raise RuntimeError('An unrelated camera JNI overlay exists.')
            write_module_scripts(folder, newer, firmware)
            names = ['yuv.so', 'post-fs-data.sh', 'service.sh', 'manifest.json', 'targets.conf', 'module.prop']
            if update_provider:
                names.append('provider')
            for name in names:
                remote = '/data/local/tmp/hyperos-camera-update-' + name
                adb(config, 'push', str(folder / name), remote, check=True, capture_output=True, timeout=60)
                root(config, 'set -e\n' + mutation_guard(MODULE) + f'cp {shlex.quote(remote)} {MODULE}/{name}.next\nrm {shlex.quote(remote)}')
            root(config, 'set -e\n' + mutation_guard(MODULE) + f'''am force-stop com.android.camera
{upgrade_provider_script() if update_provider else ''}
test "$(sha256sum {MODULE}/yuv.so.next | cut -d ' ' -f 1)" = {newer['yuv_sha256']}
chmod 644 {MODULE}/yuv.so.next
chcon u:object_r:system_file:s0 {MODULE}/yuv.so.next
mv {MODULE}/yuv.so.next {target}
chmod 755 {MODULE}/*.sh.next
mv {MODULE}/post-fs-data.sh.next {MODULE}/post-fs-data.sh
mv {MODULE}/service.sh.next {MODULE}/service.sh
mv {MODULE}/targets.conf.next {MODULE}/targets.conf
mv {MODULE}/module.prop.next {MODULE}/module.prop
mv {MODULE}/manifest.json.next {MODULE}/manifest.json
sh {MODULE}/post-fs-data.sh''')
            saved = newer
            (ROOT / 'local/xiaomi-camera-fix.json').write_text(json.dumps(saved, indent=2) + '\n')
        scripts = ROOT / 'work/xiaomi-camera-fix/script-check'
        legacy = ROOT / 'work/xiaomi-camera-fix/legacy-script-check'
        scripts.mkdir(parents=True, exist_ok=True)
        legacy.mkdir(parents=True, exist_ok=True)
        write_module_scripts(scripts, saved, firmware)
        write_module_scripts(legacy, saved, firmware, lifecycle=False)
        refresh_hooks(root, config, MODULE, {name: ((legacy / name).read_text(),
                                                   (scripts / name).read_text())
                                           for name in ('post-fs-data.sh', 'service.sh')})
        root(config, 'set -e\n' + mutation_guard(MODULE) + f'sh {MODULE}/post-fs-data.sh\nsh {MODULE}/service.sh')
        print('Existing Xiaomi camera bridges verified.', flush=True)
        return saved
    folder = ROOT / 'work/xiaomi-camera-fix'
    originals = folder / 'original'
    originals.mkdir(parents=True, exist_ok=True)
    for name, path, checksum in (('provider', PROVIDER, PROVIDER_HASH), ('hwl.so', HWL, HWL_HASH),
                                 ('libc++.so', '/vendor/lib64/libc++.so', CPP_HASH), ('camera.apk', APK, selected['apk_sha256'])):
        if root(config, 'sha256sum ' + path).split()[0] != checksum:
            raise RuntimeError('An unrelated camera overlay exists: ' + path)
        local = originals / name
        if not local.is_file() or sha256(local) != checksum:
            with local.open('wb') as output:
                adb(config, 'exec-out', 'su -W -c ' + shlex.quote('cat ' + shlex.quote(path)),
                    stdout=output, stderr=subprocess.PIPE, check=True, timeout=60)
    manifest = build(config['sdk'], folder, firmware)
    write_module_scripts(folder, manifest, firmware)
    stage = '/data/adb/hyperos-xiaomi-camera-stage'
    root(config, 'set -e\n' + mutation_guard(MODULE) + f'test ! -e {stage}\nmkdir -p {stage}')
    for name in ('provider', 'hwl.so', 'yuv.so', 'manifest.json', 'module.prop',
                 'targets.conf', 'post-fs-data.sh', 'service.sh'):
        temporary = '/data/local/tmp/hyperos-xiaomi-' + name
        adb(config, 'push', str(folder / name), temporary, check=True, capture_output=True, timeout=60)
        root(config, 'set -e\n' + mutation_guard(MODULE) + f'cp {shlex.quote(temporary)} {stage}/{name}\nrm {shlex.quote(temporary)}')
    # Directory bind adds the private JNI wrapper while retaining original APK/AOT bytes.
    root(config, 'set -e\n' + mutation_guard(MODULE) + f'''am force-stop com.android.camera
test ! -e {APP}/lib/arm64/libcamera_yuv_jni.so
mkdir -p {stage}/app
cp -a {APP}/. {stage}/app/
mkdir -p {stage}/app/lib/arm64
mv {stage}/yuv.so {stage}/app/lib/arm64/libcamera_yuv_jni.so
chcon -R u:object_r:system_file:s0 {stage}/app
chmod 644 {stage}/app/lib/arm64/libcamera_yuv_jni.so
chmod 755 {stage}/provider {stage}/*.sh
chcon u:object_r:hal_camera_default_exec:s0 {stage}/provider
chcon u:object_r:vendor_file:s0 {stage}/hwl.so
touch {stage}/skip_mount
mv {stage} {MODULE}
sh {MODULE}/post-fs-data.sh
sh {MODULE}/service.sh''')
    (ROOT / 'local/xiaomi-camera-fix.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print('Experimental Xiaomi camera bridges installed; APK and user data preserved.', flush=True)
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rebuild', action='store_true', help='Rebuild the owned camera bridges from source')
    args = parser.parse_args()
    try:
        install(runtime(), rebuild=args.rebuild)
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        raise SystemExit(str(error))
