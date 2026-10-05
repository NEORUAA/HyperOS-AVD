#!/usr/bin/env python3
"""Install pinned ranchu stream/role and private yingtian Camera YUV bridges."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shlex
import shutil
import struct
import subprocess
import uuid
import zipfile

from common import ROOT, REPO_ROOT, adb, runtime, sha256
from apply_flutter_fix import official, root
from apply_xiaomi_camera_fix import patch_provider

SOURCE = 'official-yingtian-ota'
MODULE = '/data/adb/modules/hyperos_avd_pad_camera'
APP = '/product/priv-app/MiuiCamera'
APK = APP + '/MiuiCamera.apk'
YUV = APP + '/lib/arm64/libcamera_yuv_jni.so'
PROVIDER = '/vendor/bin/hw/android.hardware.camera.provider@2.7-service-google'
HWL = '/vendor/lib64/libgooglecamerahwl_impl.so'
CPP = '/vendor/lib64/libc++.so'
RUNTIME = '/system/lib64/libandroid_runtime.so'
APK_HASH = '70e32c772c406d2a0ccd21af23d001d26acbef4b46d07cee6b4b55cf1070e8c0'
YUV_BEFORE = '4d7e594c0550fb40ae96c094dcb09fc3b5d057e5bb912033c962c0d98b165c2a'
YUV_AFTER = '575008309cb9c588b7b871c680cd50a26dcf364db03708d6ac52fe5cc9ef550a'
PROVIDER_BEFORE = '1424c5afc7f60824a640896d42af131d06ac0fc5eaa2e4985f804f43451c1068'
PROVIDER_AFTER = 'b8b09bf20e4cb786e31837e2e6ed597e43614b71042f28511a68f6d6867f7889'
HWL_BEFORE = 'e9566298975e3346c5c5a797f0a96bf48f5901b2391fae4b56b58e9befb8cb83'
HWL_AFTER = '812cc14c4cfb23d6401277dd90c795729654e9b3c2e818165748785172035e84'
CPP_HASH = '2267f93b8b3c9d1967f1833d5f71c7312213c43bb291250cf772800763037fb9'
RUNTIME_HASH = '5aad1f635b6e8ca93a5bf190f07188d5b04e8c38d39d6a772a6ccdb0f02a13e3'
SOURCES = {
    'camera_hwl_roles.cpp': 'ef601e79de5cc2c786b8a3004b70b9cff77e560a72ba476b91a1f30fc2fc4ca1',
    'camera_pad_yuv_planes.c': 'f95d2cbf94fed90a37731c9efa5fa10d9cec292b504e857e1d74adaa83a41f1f',
}
PROPERTIES = {'ro.boot.hardware': 'ranchu', 'ro.product.device': 'yingtian',
              'ro.mi.os.version.incremental': 'OS4.0.15.0.XBMCNXM'}
ORIGINALS = {'provider': (PROVIDER, PROVIDER_BEFORE), 'hwl.so': (HWL, HWL_BEFORE),
             'libc++.so': (CPP, CPP_HASH), 'runtime.so': (RUNTIME, RUNTIME_HASH),
             'camera.apk': (APK, APK_HASH)}
PAYLOADS = {'provider': PROVIDER_AFTER, 'hwl.so': HWL_AFTER, 'yuv.so': YUV_AFTER}


def manifest():
    return {'revision': 1, 'source': SOURCE, 'experimental': True,
            'apk_sha256': APK_HASH, 'runtime_sha256': RUNTIME_HASH,
            'cpp_sha256': CPP_HASH, 'embedded_yuv_sha256': YUV_BEFORE,
            'yuv_sha256': YUV_AFTER, 'plane_offset': '0x1a310c', 'recovery': False,
            'sources': dict(SOURCES),
            'targets': [{'payload': name, 'target': target, 'before': before, 'after': after}
                        for name, target, before, after in (
                            ('provider', PROVIDER, PROVIDER_BEFORE, PROVIDER_AFTER),
                            ('hwl.so', HWL, HWL_BEFORE, HWL_AFTER))]}


def validate_manifest(value):
    if value != manifest():
        raise RuntimeError('Unknown Pad camera manifest; existing files were preserved.')
    return value


def validate_runtime(data):
    if hashlib.sha256(data).hexdigest() != RUNTIME_HASH:
        raise RuntimeError('Unsupported Pad ImageReader runtime; refusing private offsets.')
    if (data[0x1a310c:0x1a312c].hex() !=
            'ff0304d1fd7b0aa9fb5b00f9fa670ca9f85f0da9f6570ea9f44f0fa9fd830291'
            or struct.unpack_from('<QQQ', data, 0x2cd0a8) != (0x80531, 0x932c5, 0x1a310c)
            or data[0x80531:0x80531+18] != b'nativeCreatePlanes'
            or data[0x932c5:].split(b'\0', 1)[0] !=
            b'(IIJ)[Landroid/media/ImageReader$SurfaceImage$SurfacePlane;'):
        raise RuntimeError('Pad nativeCreatePlanes registration verification failed.')


def build(sdk, folder):
    """Compile pinned originals or use an exact source/payload prebuilt receipt."""
    folder = Path(folder)
    for name, checksum in SOURCES.items():
        if sha256(REPO_ROOT / 'native' / name) != checksum:
            raise RuntimeError('Pad camera bridge source changed: ' + name)
    prebuilt = ROOT / 'tools/pad-camera-native'
    if (prebuilt / 'receipt.json').is_file():
        receipt = json.loads((prebuilt / 'receipt.json').read_text())
        expected = {**PAYLOADS, 'manifest.json': hashlib.sha256(
            (json.dumps(manifest(), indent=2) + '\n').encode()).hexdigest()}
        if receipt != {'sources': SOURCES, 'files': expected}:
            raise RuntimeError('Unknown precompiled Pad camera receipt.')
        for name, checksum in expected.items():
            if sha256(prebuilt / name) != checksum:
                raise RuntimeError('Precompiled Pad camera payload changed: ' + name)
        folder.mkdir(parents=True, exist_ok=True)
        for name in expected:
            if (prebuilt / name).resolve() != (folder / name).resolve():
                shutil.copy2(prebuilt / name, folder / name)
        return manifest()
    originals = folder / 'original'
    for name, (_, checksum) in ORIGINALS.items():
        if not (originals / name).is_file() or sha256(originals / name) != checksum:
            raise RuntimeError('Missing verified Pad camera build input: ' + name)
    validate_runtime((originals / 'runtime.so').read_bytes())
    with zipfile.ZipFile(originals / 'camera.apk') as archive:
        yuv = archive.read('lib/arm64-v8a/libcamera_yuv_jni.so')
    if len(yuv) != 529744 or hashlib.sha256(yuv).hexdigest() != YUV_BEFORE:
        raise RuntimeError('Unsupported Pad camera embedded JNI.')
    (originals / 'yuv.so').write_bytes(yuv)
    compiler = Path(sdk) / 'ndk/30.0.16138531/toolchains/llvm/prebuilt/darwin-x86_64/bin/aarch64-linux-android36-clang'
    if not compiler.is_file():
        raise RuntimeError('Install the pinned Android NDK 30.0.16138531 for Pad camera bridges.')
    for output, source, cpp, alignment in (
            ('hwl.so', 'camera_hwl_roles.cpp', True, 4096),
            ('yuv.so', 'camera_pad_yuv_planes.c', False, 16384)):
        assembly = 'hwl.S' if cpp else 'yuv.so.S'
        (folder / assembly).write_text('.section .rodata.avd_original,"a",@progbits\n'
            f'.balign {alignment}\n.global avd_original_start\n.hidden avd_original_start\n'
            f'avd_original_start:\n.incbin "original/{output}"\n'
            '.global avd_original_end\n.hidden avd_original_end\navd_original_end:\n')
        command = [str(compiler) + ('++' if cpp else ''), str(REPO_ROOT / 'native' / source),
                   assembly, '-shared', '-fPIC', '-O2', '-Wall', '-Wextra', '-Werror', '-llog', '-ldl',
                   f'-Wl,-z,max-page-size={alignment}', '-Wl,-soname,' +
                   ('libgooglecamerahwl_impl.so' if cpp else 'libcamera_yuv_jni.so'), '-o', output]
        if cpp:
            command += ['-D_LIBCPP_ABI_NAMESPACE=__1', '-nostdlib++', 'original/libc++.so']
        subprocess.run(command, cwd=folder, check=True)
    (folder / 'provider').write_bytes(patch_provider((originals / 'provider').read_bytes()))
    for name, checksum in PAYLOADS.items():
        if sha256(folder / name) != checksum:
            raise RuntimeError('Pad camera build output verification failed: ' + name)
    (folder / 'manifest.json').write_text(json.dumps(manifest(), indent=2) + '\n')
    return manifest()


MOUNT_SCRIPT = r'''#!/system/bin/sh
set -eu
MODDIR=${0%/*}
BB=/data/adb/ksu/bin/busybox
if [ -e "$MODDIR/disable" ] || [ -L "$MODDIR/disable" ]; then exit 0; fi
@PROPS@
for name in provider hwl.so; do
    case "$name" in provider) expected=@PROVIDER_AFTER@ ;; *) expected=@HWL_AFTER@ ;; esac
    [ "$(sha256sum "$MODDIR/$name" | cut -d ' ' -f 1)" = "$expected" ] || exit 1
done
[ "$(sha256sum "$MODDIR/app/MiuiCamera.apk" | cut -d ' ' -f 1)" = @APK_HASH@ ] || exit 1
[ "$(sha256sum "$MODDIR/app/lib/arm64/libcamera_yuv_jni.so" | cut -d ' ' -f 1)" = @YUV_AFTER@ ] || exit 1
[ "$(sha256sum "$MODDIR/targets.conf" | cut -d ' ' -f 1)" = @TARGETS_HASH@ ] || exit 1
@NAMESPACE_PREFLIGHT@
changed=0
hal_changed=0
for pid in $pids; do
    [ -d "/proc/$pid" ] || continue
    while IFS='|' read -r payload target before after; do
        actual=$(nsenter -t "$pid" -m -- "$BB" sha256sum "$target" | "$BB" cut -d ' ' -f 1)
        [ "$actual" != "$after" ] || continue
        [ "$actual" = "$before" ] || exit 1
        nsenter -t "$pid" -m -- "$BB" mount -o bind "$MODDIR/$payload" "$target"
        flags=ro
        [ "$payload" != provider ] || flags=ro,suid,exec
        nsenter -t "$pid" -m -- "$BB" mount -o remount,bind,$flags "$target"
        [ "$(nsenter -t "$pid" -m -- "$BB" sha256sum "$target" | "$BB" cut -d ' ' -f 1)" = "$after" ] || exit 1
        changed=1; hal_changed=1
    done < "$MODDIR/targets.conf"
    actual=$(nsenter -t "$pid" -m -- "$BB" sh -c 'if [ -e "$1" ]; then sha256sum "$1"; fi' sh @YUV@ | "$BB" cut -d ' ' -f 1)
    if [ "$actual" != @YUV_AFTER@ ]; then
        [ -z "$actual" ] || exit 1
        nsenter -t "$pid" -m -- "$BB" mount -o bind "$MODDIR/app" @APP@
        nsenter -t "$pid" -m -- "$BB" mount -o remount,bind,ro @APP@
        changed=1
    fi
    [ "$(nsenter -t "$pid" -m -- "$BB" sha256sum @APK@ | "$BB" cut -d ' ' -f 1)" = @APK_HASH@ ] || exit 1
    [ "$(nsenter -t "$pid" -m -- "$BB" sha256sum @YUV@ | "$BB" cut -d ' ' -f 1)" = @YUV_AFTER@ ] || exit 1
done
if [ "$hal_changed" = 1 ]; then
    stop vendor.camera-provider-2-7-google
    start vendor.camera-provider-2-7-google
fi
if [ "$changed" = 1 ] && [ "$(getprop sys.boot_completed)" = 1 ]; then
    am force-stop com.android.camera
fi
log -t HyperOSAVDPadCamera 'Verified Pad camera overlays installed; original signed APK preserved.'
'''


def targets_text():
    return ''.join('|'.join(item[k] for k in ('payload', 'target', 'before', 'after')) + '\n'
                   for item in manifest()['targets'])


def namespace_preflight(sources=False):
    """Share the same exact namespace guards with installation and cold boot."""
    checks = '\n    '.join(
        '[ "$(nsenter -t "$pid" -m -- "$BB" sha256sum ' + path +
        ' | "$BB" cut -d \' \' -f 1)" = ' + checksum + ' ] || exit 1'
        for path, checksum in ((APK, APK_HASH), (RUNTIME, RUNTIME_HASH), (CPP, CPP_HASH)))
    if sources:
        checks += '\n    ' + '\n    '.join(
            '[ "$(nsenter -t "$pid" -m -- "$BB" sha256sum "$MODDIR/' + path +
            '" | "$BB" cut -d \' \' -f 1)" = ' + checksum + ' ] || exit 1'
            for path, checksum in (('provider', PROVIDER_AFTER), ('hwl.so', HWL_AFTER),
                                    ('app/MiuiCamera.apk', APK_HASH),
                                    ('app/lib/arm64/libcamera_yuv_jni.so', YUV_AFTER)))
    return f'''BB=/data/adb/ksu/bin/busybox
pids="1 $(getprop init.svc_debug_pid.hyos_spawner) $(pidof zygote64 || true)"
# Preflight every active owned namespace before creating any new bind mount.
for pid in $pids; do
    [ -d "/proc/$pid" ] || continue
    {checks}
    actual=$(nsenter -t "$pid" -m -- "$BB" sha256sum {PROVIDER} | "$BB" cut -d ' ' -f 1)
    case "$actual" in {PROVIDER_BEFORE}|{PROVIDER_AFTER}) ;; *) exit 1 ;; esac
    actual=$(nsenter -t "$pid" -m -- "$BB" sha256sum {HWL} | "$BB" cut -d ' ' -f 1)
    case "$actual" in {HWL_BEFORE}|{HWL_AFTER}) ;; *) exit 1 ;; esac
    actual=$(nsenter -t "$pid" -m -- "$BB" sh -c 'if [ -e "$1" ] || [ -L "$1" ]; then sha256sum "$1"; fi' sh {YUV} | "$BB" cut -d ' ' -f 1)
    case "$actual" in ''|{YUV_AFTER}) ;; *) exit 1 ;; esac
done'''


def write_scripts(folder):
    values = {key: value for key, value in globals().items()
              if isinstance(value, str) and key.isupper()}
    values['PROPS'] = '\n'.join('[ "$(getprop ' + key + ')" = ' + value + ' ] || exit 1'
                              for key, value in PROPERTIES.items())
    values['NAMESPACE_PREFLIGHT'] = namespace_preflight(sources=True)
    values['TARGETS_HASH'] = hashlib.sha256(targets_text().encode()).hexdigest()
    script = MOUNT_SCRIPT
    for key, value in values.items():
        script = script.replace('@' + key + '@', value)
    if re.search(r'@[A-Z_]+@', script):
        raise RuntimeError('Unexpanded Pad camera mount script.')
    (folder / 'post-fs-data.sh').write_text(script)
    (folder / 'service.sh').write_text('''#!/system/bin/sh
set -eu
MODDIR=${0%/*}
if [ -e "$MODDIR/disable" ] || [ -L "$MODDIR/disable" ]; then exit 0; fi
count=0
while [ "$(getprop sys.boot_completed)" != 1 ]; do
    count=$((count+1)); [ "$count" -lt 300 ] || exit 1; sleep 1
done
sh "$MODDIR/post-fs-data.sh"
''')
    (folder / 'targets.conf').write_text(targets_text())
    (folder / 'module.prop').write_text('id=hyperos_avd_pad_camera\nname=HyperOS AVD Pad camera bridge\n'
        'version=1\nversionCode=1\nauthor=HyperOS-AVD\n'
        'description=Pinned yingtian stream, role and private I420 to NV21 compatibility\n')


def _hash(config, path):
    command = '/data/adb/ksu/bin/busybox nsenter -t 1 -m -- /data/adb/ksu/bin/busybox '
    value = root(config, command + 'sha256sum ' + shlex.quote(path)).split()
    return value[0] if value else ''


def _preflight(config):
    official(config, sources=(SOURCE,))
    for key, value in PROPERTIES.items():
        if root(config, 'getprop ' + key) != value:
            raise RuntimeError('Pad camera native fix refused this device: ' + key)
    if root(config, 'pm path com.android.camera') != 'package:' + APK:
        raise RuntimeError('Unsupported Pad camera update; existing files were preserved.')
    for path, allowed in ((APK, (APK_HASH,)), (CPP, (CPP_HASH,)), (RUNTIME, (RUNTIME_HASH,)),
                          (PROVIDER, (PROVIDER_BEFORE, PROVIDER_AFTER)), (HWL, (HWL_BEFORE, HWL_AFTER))):
        if _hash(config, path) not in allowed:
            raise RuntimeError('Unsupported Pad camera native input: ' + path)
    native = root(config, '/data/adb/ksu/bin/busybox nsenter -t 1 -m -- '
                  '/data/adb/ksu/bin/busybox sh -c ' + shlex.quote(
                      'if [ -e "$1" ] || [ -L "$1" ]; then sha256sum "$1"; fi') +
                  ' sh ' + shlex.quote(YUV)).split()
    if native and native[0] != YUV_AFTER:
        raise RuntimeError('Unrelated Pad camera native overlay; no files changed.')


def publish(config, folder):
    token = uuid.uuid4().hex
    stage = '/data/adb/hyperos-pad-camera-stage-' + token
    root(config, f'test ! -e {stage}\ntest ! -L {stage}\ntest ! -e {MODULE}\ntest ! -L {MODULE}\nmkdir {stage}')
    uploads = []
    try:
        for name in ('provider', 'hwl.so', 'yuv.so', 'manifest.json', 'post-fs-data.sh',
                     'service.sh', 'targets.conf', 'module.prop'):
            temporary = '/data/local/tmp/hyperos-pad-camera-install-' + token + '-' + name
            root(config, f'test ! -e {temporary}\ntest ! -L {temporary}')
            uploads.append(temporary)
            adb(config, 'push', str(folder / name), temporary, check=True, capture_output=True, timeout=60)
            root(config, f'cp {temporary} {stage}/{name}\nrm {temporary}')
        checks = '\n'.join(f'test "$(sha256sum {stage}/{name} | cut -d \' \' -f 1)" = {sha256(folder / name)}'
                           for name in ('provider', 'hwl.so', 'yuv.so', 'manifest.json', 'post-fs-data.sh',
                                        'service.sh', 'targets.conf', 'module.prop'))
        root(config, f'''{checks}
    sh -n {stage}/post-fs-data.sh
    sh -n {stage}/service.sh
    BB=/data/adb/ksu/bin/busybox
    mkdir {stage}/app
    "$BB" nsenter -t 1 -m -- "$BB" cp -a {APP}/. {stage}/app/
    mkdir -p {stage}/app/lib/arm64
    mv {stage}/yuv.so {stage}/app/lib/arm64/libcamera_yuv_jni.so
    test "$(sha256sum {stage}/app/MiuiCamera.apk | cut -d ' ' -f 1)" = {APK_HASH}
    test "$(sha256sum {stage}/app/lib/arm64/libcamera_yuv_jni.so | cut -d ' ' -f 1)" = {YUV_AFTER}
    chcon -R u:object_r:system_file:s0 {stage}/app
    chmod 644 {stage}/app/lib/arm64/libcamera_yuv_jni.so {stage}/hwl.so
    chcon u:object_r:vendor_file:s0 {stage}/hwl.so
    chmod 755 {stage}/provider
    chcon u:object_r:hal_camera_default_exec:s0 {stage}/provider
    chmod 700 {stage}/*.sh
    touch {stage}/skip_mount
    test ! -e {MODULE}
    test ! -L {MODULE}
    mv {stage} {MODULE}
    sh {MODULE}/post-fs-data.sh''')
    except Exception as error:
        # This random directory was created by this invocation and is never a
        # bind source until it is atomically renamed to MODULE. Leave MODULE
        # intact even if a post-publication mount/restart command failed.
        try:
            root(config, f'if [ -d {stage} ] && [ ! -L {stage} ]; then rm -rf {stage}; fi')
            for temporary in uploads:
                root(config, f'rm -f {temporary}')
        except Exception as cleanup_error:
            raise RuntimeError('Pad camera staging failed; owned staging cleanup also failed: '
                               + str(cleanup_error)) from error
        raise


def install(config):
    """Publish a new module atomically; never overwrite a mounted payload inode."""
    _preflight(config)
    existing = root(config, f'if [ -e {MODULE} ] || [ -L {MODULE} ]; then echo yes; fi')
    folder = ROOT / 'work/pad-camera-native'
    if existing:
        saved = validate_manifest(json.loads(root(config, f'cat {MODULE}/manifest.json')))
        if root(config, f'if [ -e {MODULE}/disable ] || [ -L {MODULE}/disable ]; then echo yes; fi'):
            print('Pad camera native bridge remains disabled.', flush=True)
            return saved
        write_scripts_local = folder / 'script-check'
        write_scripts_local.mkdir(parents=True, exist_ok=True)
        write_scripts(write_scripts_local)
        for name in ('post-fs-data.sh', 'service.sh', 'targets.conf'):
            expected = sha256(write_scripts_local / name)
            if root(config, f'sha256sum {MODULE}/{name}').split()[0] != expected:
                raise RuntimeError('Saved Pad camera script changed: ' + name)
        root(config, f'sh {MODULE}/post-fs-data.sh')
        print('Existing Pad camera native bridges verified.', flush=True)
        return saved
    originals = folder / 'original'
    originals.mkdir(parents=True, exist_ok=True)
    inputs = {} if (ROOT / 'tools/pad-camera-native/receipt.json').is_file() else ORIGINALS
    for name, (path, checksum) in inputs.items():
        local = originals / name
        if local.is_file() and sha256(local) == checksum:
            continue
        if _hash(config, path) != checksum:
            raise RuntimeError('Original Pad camera build input unavailable: ' + path)
        temporary = local.with_suffix(local.suffix + '.next')
        command = '/data/adb/ksu/bin/busybox nsenter -t 1 -m -- /data/adb/ksu/bin/busybox cat ' + shlex.quote(path)
        with temporary.open('wb') as output:
            adb(config, 'exec-out', 'su -W -c ' + shlex.quote(command),
                stdout=output, stderr=subprocess.PIPE, check=True, timeout=60)
        if sha256(temporary) != checksum:
            temporary.unlink()
            raise RuntimeError('Rooted Pad camera export checksum mismatch: ' + name)
        temporary.replace(local)
    result = build(config['sdk'], folder)
    write_scripts(folder)
    # Check every namespace before the first guest filesystem write.
    root(config, namespace_preflight())
    publish(config, folder)
    (ROOT / 'local').mkdir(exist_ok=True)
    (ROOT / 'local/pad-camera-native-fix.json').write_text(json.dumps(result, indent=2) + '\n')
    print('Pad camera native bridges installed; signed APK and user data preserved.', flush=True)
    return result


if __name__ == '__main__':
    argparse.ArgumentParser(description=__doc__).parse_args()
    try:
        install(runtime())
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as error:
        raise SystemExit(str(error))
