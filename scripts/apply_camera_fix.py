#!/usr/bin/env python3
"""Install optional, hash-guarded Parrot 8.2 allocator and GL query bridges."""
import argparse
import json
from pathlib import Path
import shlex
import subprocess
import zipfile

from common import ROOT, REPO_ROOT, adb, runtime, sha256
from apply_flutter_fix import official, root
from patch_flutter import digest

PACKAGE = 'com.google.android.GoogleCamera.parrot'
APK_SHA256 = '2d5db1f5185ae09c9f0dc1e13fd3bc0a6babdbf6587487361258d7d89be07376'
NATIVE_SHA256 = 'ddf45ea66324f1e653095358213d104d23431d2ca0f433f1df70f8bb6bb25aaf'
RUNTIME_SHA256 = '22b624d7bfa130cb66ad841b0af152d975c5329449dd804e077d106c57a89c3a'
PROTOTYPE_SHA256 = 'fbe115ff5d3e87beadae31c3bd9da344fdc8dd28cd44e5b02f9e42be83d1cd65'


def install(config, remove=False):
    official(config)
    if root(config, 'getprop ro.boot.qemu.avd_name') != config['name']:
        raise RuntimeError('Camera bridge is restricted to this official OS4 AVD.')
    paths = root(config, 'pm path ' + PACKAGE).splitlines()
    if len(paths) != 1 or not paths[0].startswith('package:/data/app/'):
        raise RuntimeError('Install the supported Parrot 8.2 APK first.')
    apk = paths[0].removeprefix('package:')
    if root(config, 'sha256sum ' + shlex.quote(apk)).split()[0] != APK_SHA256:
        raise RuntimeError('Unsupported camera APK; no native files were changed.')
    if not remove and root(config, 'sha256sum /system/lib64/libandroid_runtime.so').split()[0] != RUNTIME_SHA256:
        raise RuntimeError('Unsupported Android GL runtime; no native files were changed.')
    target = str(Path(apk).parent / 'lib/arm64/lib_aion_buffer.so')
    quoted = shlex.quote(target)
    existing = root(config, f'if [ -e {quoted} ]; then sha256sum {quoted}; fi')
    existing = existing.split()[0] if existing else ''
    marker = ROOT / 'local/camera-fix.json'
    saved = json.loads(marker.read_text()) if marker.is_file() else {}
    owned = {PROTOTYPE_SHA256}
    if saved.get('apk') == apk and saved.get('target') == target:
        owned.add(saved['sha256'])
    if remove:
        if existing and existing not in owned:
            raise RuntimeError('Refused to remove an unrelated camera library.')
        if existing:
            root(config, f'am force-stop {PACKAGE}\nrm {quoted}')
        marker.unlink(missing_ok=True)
        print('This project camera bridge was removed; APK and app data were preserved.')
        return

    folder = ROOT / 'work/camera-fix'
    folder.mkdir(parents=True, exist_ok=True)
    original = folder / 'camera.apk'
    if not original.is_file() or sha256(original) != APK_SHA256:
        adb(config, 'pull', apk, str(original), check=True, capture_output=True, timeout=60)
    if sha256(original) != APK_SHA256:
        raise RuntimeError('Camera APK changed while reading it.')
    with zipfile.ZipFile(original) as archive:
        if digest(archive.read('lib/arm64-v8a/libgcastartup.so')) != NATIVE_SHA256:
            raise RuntimeError('Unsupported camera allocator ABI.')
    compilers = list((Path(config['sdk']) / 'ndk').glob(
        '*/toolchains/llvm/prebuilt/darwin-*/bin/aarch64-linux-android36-clang'))
    if not compilers:
        raise RuntimeError('Install Android NDK (API 36+) to build this optional camera bridge.')
    compiler = max(compilers, key=lambda p: tuple(int(v) for v in p.parents[5].name.split('.')))
    payload = folder / 'lib_aion_buffer.so'
    subprocess.run([str(compiler), str(REPO_ROOT / 'native/camera_aion_cpu.c'),
                    '-shared', '-fPIC', '-O2', '-Wall', '-Wextra', '-Werror',
                    '-landroid', '-llog', '-lGLESv2', '-ldl', '-Wl,-soname,lib_aion_buffer.so',
                    '-Wl,-z,max-page-size=16384', '-o', str(payload)], check=True)
    checksum = sha256(payload)
    if existing and existing not in owned | {checksum}:
        raise RuntimeError('Refused to replace an unrelated camera library.')
    if existing != checksum:
        remote = '/data/local/tmp/hyperos-camera-aion.so'
        adb(config, 'push', str(payload), remote, check=True, capture_output=True, timeout=30)
        root(config, f'''test "$(sha256sum {shlex.quote(apk)} | cut -d ' ' -f 1)" = {APK_SHA256}
test "$(sha256sum {remote} | cut -d ' ' -f 1)" = {checksum}
test -d {shlex.quote(str(Path(target).parent))}
am force-stop {PACKAGE}
cp {remote} {quoted}.next
chmod 644 {quoted}.next
chcon u:object_r:apk_data_file:s0 {quoted}.next
mv {quoted}.next {quoted}
rm {remote}''')
    if root(config, 'sha256sum ' + quoted).split()[0] != checksum:
        raise RuntimeError('Camera bridge checksum mismatch.')
    manifest = {'revision': 2, 'package': PACKAGE, 'apk': apk, 'apk_sha256': APK_SHA256,
                'target': target, 'sha256': checksum, 'allocator': 'cpu-shared-memory',
                'runtime_sha256': RUNTIME_SHA256, 'external_texture_unit_query': True,
                'gpu_import': False, 'experimental': True}
    marker.parent.mkdir(exist_ok=True)
    marker.write_text(json.dumps(manifest, indent=2) + '\n')
    print('Parrot 8.2 allocator and GL query bridges installed. HDR capture compatibility remains experimental.')
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--remove', action='store_true')
    args = parser.parse_args()
    install(runtime(), remove=args.remove)


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        raise SystemExit(str(error))
