#!/usr/bin/env python3
"""Install optional, hash-guarded Parrot 8.2 allocator and GL query bridges."""
import argparse
import json
from pathlib import Path
import shlex
import shutil
import tempfile
import subprocess

from common import ROOT, REPO_ROOT, runtime, sha256
from apply_flutter_fix import official, root
from patch_outcome import UnsupportedPatch
from module_lifecycle import preserved

PACKAGE = 'com.google.android.GoogleCamera.parrot'
APK_SHA256 = '2d5db1f5185ae09c9f0dc1e13fd3bc0a6babdbf6587487361258d7d89be07376'
NATIVE_SHA256 = 'ddf45ea66324f1e653095358213d104d23431d2ca0f433f1df70f8bb6bb25aaf'
RUNTIME_SHA256 = '22b624d7bfa130cb66ad841b0af152d975c5329449dd804e077d106c57a89c3a'
PROTOTYPE_SHA256 = 'fbe115ff5d3e87beadae31c3bd9da344fdc8dd28cd44e5b02f9e42be83d1cd65'


MODULE_ID = 'hyperos_avd_parrot_camera'
MODULE = '/data/adb/modules/' + MODULE_ID
MODULE_NAME = 'HyperOS AVD Parrot camera bridge'
MODULE_DESCRIPTION = 'Optional verified CPU allocator and private GL query bridge'
BRIDGE_SOURCE_SHA256 = 'bd00736dc208d654085ab0636fddad3b315591b32052f89db6e6a06b8a43619c'
BRIDGE_SHA256 = '37f845d5f45a207da895345f57ee13e8f252b30a132977b04d1b7543b881664d'
EMPTY = 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'


def prebuilt_receipt():
    return {'revision': 1, 'source_sha256': BRIDGE_SOURCE_SHA256,
            'files': {'lib_aion_buffer.so': BRIDGE_SHA256}}


def build_payload(config, destination, workspace=None):
    """Use a pinned release payload; only source builds require an NDK."""
    source = REPO_ROOT / 'native/camera_aion_cpu.c'
    if not source.is_file() or source.is_symlink() or sha256(source) != BRIDGE_SOURCE_SHA256:
        raise RuntimeError('Parrot camera source differs from the verified bridge.')
    prebuilt = Path(workspace or ROOT) / 'tools/parrot-camera'
    destination = Path(destination)
    if prebuilt.exists() or prebuilt.is_symlink():
        receipt = prebuilt / 'receipt.json'
        payload = prebuilt / 'lib_aion_buffer.so'
        try:
            metadata = json.loads(receipt.read_text())
        except (OSError, ValueError) as error:
            raise RuntimeError('Invalid precompiled Parrot camera receipt.') from error
        if (not prebuilt.is_dir() or prebuilt.is_symlink() or not receipt.is_file()
                or receipt.is_symlink() or metadata != prebuilt_receipt()
                or not payload.is_file() or payload.is_symlink() or sha256(payload) != BRIDGE_SHA256):
            raise RuntimeError('Unknown precompiled Parrot camera receipt or payload.')
        shutil.copyfile(payload, destination)
        return destination
    compilers = list((Path(config['sdk']) / 'ndk').glob(
        '*/toolchains/llvm/prebuilt/darwin-*/bin/aarch64-linux-android36-clang'))
    if not compilers:
        raise UnsupportedPatch('No verified Parrot prebuilt is available; source builds require NDK API 36+.')
    compiler = max(compilers, key=lambda p: tuple(int(v) for v in p.parents[5].name.split('.')))
    subprocess.run([str(compiler), str(source), '-shared', '-fPIC', '-O2', '-Wall',
                    '-Wextra', '-Werror', '-landroid', '-llog', '-lGLESv2', '-ldl',
                    '-Wl,-soname,lib_aion_buffer.so', '-Wl,-z,max-page-size=16384',
                    '-o', str(destination)], check=True)
    if sha256(destination) != BRIDGE_SHA256:
        raise RuntimeError('Parrot camera compiler output differs from the verified payload.')
    return destination


def prepare_prebuilt(workspace, sdk):
    """Package producers publish the audited helper, without enabling the app."""
    workspace = Path(workspace)
    folder = workspace / 'tools/parrot-camera'
    folder.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.parrot-prebuilt-', dir=folder.parent) as temporary:
        stage = Path(temporary)
        build_payload({'sdk': str(sdk)}, stage / 'lib_aion_buffer.so', workspace)
        (stage / 'receipt.json').write_text(json.dumps(prebuilt_receipt(), indent=2) + '\n')
        if folder.exists() or folder.is_symlink():
            # build_payload already authenticated every existing asset.
            return folder
        stage.rename(folder)
    return folder


def bridge_profiles():
    """Pin the complete APK and private runtime ABI, independent of AVD names."""
    return [{'apk_sha256': APK_SHA256,
             'system_libraries': {'/system/lib64/libandroid_runtime.so': RUNTIME_SHA256},
             'native_libraries': {'libgcastartup.so': NATIVE_SHA256},
             'libraries': [{'name': 'lib_aion_buffer.so', 'before': EMPTY,
                            'after': BRIDGE_SHA256, 'placeholder': True,
                            'legacy_direct': [BRIDGE_SHA256, PROTOTYPE_SHA256]}]}]


def install(config, remove=False):
    official(config, sources=('official-hongkong-ota', 'official-yingtian-ota'))
    from app_bridge_module import install as install_module, remove as remove_module
    if remove:
        outcome = remove_module(config, MODULE_ID, profiles=bridge_profiles(),
                                name=MODULE_NAME, description=MODULE_DESCRIPTION,
                                package=PACKAGE)
    else:
        choice = preserved(root, config, MODULE)
        if choice:
            return choice
    paths = root(config, 'pm path ' + PACKAGE).splitlines()
    if len(paths) != 1 or not paths[0].startswith('package:/data/app/'):
        if remove:
            return outcome
        raise UnsupportedPatch('Install the supported Parrot 8.2 APK first.')
    apk = paths[0].removeprefix('package:')
    import re
    if not re.fullmatch(r'/data/app/(?:~~[A-Za-z0-9_=-]+/)?com\.google\.android\.GoogleCamera\.parrot-[A-Za-z0-9_=-]+/base\.apk', apk):
        raise UnsupportedPatch('Unsupported Parrot package layout; original files retained.')
    if root(config, 'sha256sum ' + shlex.quote(apk)).split()[0] != APK_SHA256:
        if remove:
            return outcome
        raise UnsupportedPatch('Unsupported camera APK; no native files were changed.')
    target = str(Path(apk).parent / 'lib/arm64/lib_aion_buffer.so')
    quoted = shlex.quote(target)
    if remove:
        # Only the two historically audited direct files are project-owned.
        # Host markers alone never authorize removal of arbitrary app code.
        hashes = '|'.join((BRIDGE_SHA256, PROTOTYPE_SHA256))
        root(config, 'set -e\n' +
             # Mounted targets are released only by the owning module hook.
             'if awk -v target=' + quoted +
             ' \'$5 == target {found=1} END {exit !found}\' /proc/self/mountinfo; then exit 0; fi\n' +
             f'if [ -f {quoted} ] && [ ! -L {quoted} ]; then\n'
             f'  case "$(sha256sum {quoted} | cut -d " " -f 1)" in\n'
             f'    {hashes}) am force-stop {PACKAGE}; rm {quoted} ;;\n'
             '    *) : ;;\nesac\nfi')
        (ROOT / 'local/camera-fix.json').unlink(missing_ok=True)
        return outcome
    if root(config, 'sha256sum /system/lib64/libandroid_runtime.so').split()[0] != RUNTIME_SHA256:
        raise UnsupportedPatch('Unsupported Android GL runtime; no native files were changed.')
    with tempfile.TemporaryDirectory(prefix='parrot-bridge-') as temporary:
        payload = build_payload(config, Path(temporary) / 'lib_aion_buffer.so')
        result = install_module(config, module_id=MODULE_ID,
            name=MODULE_NAME, description=MODULE_DESCRIPTION,
            package=PACKAGE, profiles=bridge_profiles(), payloads={'lib_aion_buffer.so': payload})
    marker = ROOT / 'local/camera-fix.json'
    marker.parent.mkdir(exist_ok=True)
    marker.write_text(json.dumps({'revision': 3, 'module': MODULE_ID, 'package': PACKAGE,
                                'apk_sha256': APK_SHA256, 'runtime_sha256': RUNTIME_SHA256,
                                'source_sha256': BRIDGE_SOURCE_SHA256, 'sha256': BRIDGE_SHA256,
                                'experimental': True}, indent=2) + '\n')
    print('Parrot bridge staged through KernelSU; normal boot activates its app-path reconciliation.', flush=True)
    return result


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
