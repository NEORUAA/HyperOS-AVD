#!/usr/bin/env python3
"""Repack the local HyperOS GSI for the installed API 36 ARM64 AVD base."""
import argparse
from pathlib import Path
import hashlib
import shutil
import subprocess
import zipfile

from lp_image import pack, read_lp, unpack
from common import ROOT, fetch_ksu, firmware_idle, host_check, sdk_path, tool
from init_userdata import create as create_userdata
from patch_gnss import patch as patch_gnss

BASE = sdk_path() / 'system-images/android-36/google_apis_playstore/arm64-v8a'
DEBUGFS = tool('debugfs', 'e2fsprogs')
DUMP_EROFS = tool('dump.erofs', 'erofs-utils')
KSU_TOOL = ROOT / 'tools/ksud-aarch64-apple-darwin'
KSU_MODULE = ROOT / 'tools/lkm-aarch64-android15-6.6_kernelsu.ko'
KSU_HASHES = {
    KSU_TOOL: '40ca97a2fb61284129909abac5325dcae790736d9b88901f4f31cc7ec6d9a705',
    KSU_MODULE: 'c31d994aaf285e7bf4cf1ec38c2bbf2d7f303d1a4a7d616405bcd9f850d684e5',
}


def erofs(image, path):
    result = subprocess.run([DUMP_EROFS, '--cat', '--path=' + path, str(image)],
                            check=True, capture_output=True)
    if not result.stdout:
        raise RuntimeError('Empty file: ' + path)
    return result.stdout


def debugfs(image, command, write=False):
    result = subprocess.run([DEBUGFS, *(['-w'] if write else []), '-R', command, str(image)],
                            check=True, capture_output=True)
    with (ROOT / 'logs/image-edits.log').open('ab') as log:
        log.write(command.encode() + b'\n' + result.stdout + result.stderr)
    if any(s in result.stderr for s in [b'File not found', b'File already exists',
                                        b'Filesystem not open', b'Could not', b'Usage:']):
        raise RuntimeError(result.stderr.decode())
    return result.stdout


def install(image, target, data, mode, label, replace=False):
    staging = ROOT / 'work/install-file'
    staging.write_bytes(data)
    if replace:
        debugfs(image, 'rm ' + target, True)
    staging_arg = '"' + str(staging).replace('\\', '\\\\').replace('"', '\\"') + '"'
    debugfs(image, f'write {staging_arg} {target}', True)
    debugfs(image, f'set_inode_field {target} mode {mode}', True)
    context = ROOT / 'work/install-context'
    context.write_bytes(label.encode() + b'\0')
    context_arg = '"' + str(context).replace('\\', '\\\\').replace('"', '\\"') + '"'
    debugfs(image, f'ea_set -f {context_arg} {target} security.selinux', True)
    assert debugfs(image, 'cat ' + target) == data


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--base', type=Path, default=BASE)
    parser.add_argument('--zip', type=Path, default=ROOT / 'Hyperos-fuxi-16-OS3.0.2.0.WMCCNXM-AB-20251210-MysticGSI.zip')
    args = parser.parse_args()
    host_check()
    firmware_idle()
    if 'Pkg.Revision=7' not in (args.base / 'source.properties').read_text().splitlines():
        raise SystemExit('This build is pinned to API 36 Google Play ARM64 system image revision 7.')
    fetch_ksu(list(p.name for p in KSU_HASHES))
    for path, expected in KSU_HASHES.items():
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise SystemExit(f'Missing or invalid official KernelSU v3.3.0 asset: {path}')
    for directory in ['input', 'images', 'work/base', 'logs']:
        (ROOT / directory).mkdir(parents=True, exist_ok=True)
    original = ROOT / 'input/system.img'
    with zipfile.ZipFile(args.zip) as archive:
        expected_size = archive.getinfo('system.img').file_size
        if not original.exists() or original.stat().st_size != expected_size:
            archive.extract('system.img', ROOT / 'input')
    print('Extracting AVD hardware partitions.', flush=True)
    unpack(args.base / 'system.img', ROOT / 'work/base')
    image = ROOT / 'work/hyperos-system.img'
    shutil.copyfile(original, image)
    vendor = ROOT / 'work/vendor.img'
    shutil.copyfile(ROOT / 'work/base/vendor.img', vendor)
    old = erofs(vendor, '/etc/fstab.ranchu')
    new = b''.join((b'#' + line[1:]) if line.startswith((b'product ', b'system_ext ')) else line
                   for line in old.splitlines(keepends=True))
    assert len(old) == len(new)
    content = vendor.read_bytes()
    assert content.count(old) == 1, 'fstab must be stored uncompressed exactly once'
    vendor.write_bytes(content.replace(old, new))
    (ROOT / 'work/patched-fstab.ranchu').write_bytes(new)
    print('Adding AVD initialization and selecting 64-bit media services.', flush=True)
    install(image, '/system_ext/bin/init.ranchu.adb.setup.sh',
            erofs(ROOT / 'work/base/system_ext.img', '/bin/init.ranchu.adb.setup.sh'),
            '0100755', 'u:object_r:goldfish_system_setup_exec:s0')
    init = erofs(ROOT / 'work/base/system_ext.img', '/etc/init/init.system_ext.rc')
    init += b'\n' + erofs(ROOT / 'work/base/system_ext.img', '/etc/init/init.system_ext.radio.rc')
    install(image, '/system_ext/etc/init/init.hyperos_avd.rc', init, '0100644',
            'u:object_r:system_file:s0')
    prop = debugfs(image, 'cat /system/build.prop')
    assert b'ro.mediaserver.64b.enable=' not in prop
    prop = prop.replace(b'ro.secure=0', b'ro.secure=1')
    prop += b'\n# Use the native ARM64 service on Apple Silicon AVD.\nro.mediaserver.64b.enable=true\n'
    install(image, '/system/build.prop', prop, '0100600', 'u:object_r:system_file:s0', True)
    # Keep the GSI from switching global SELinux to permissive during early-init.
    install(image, '/system/etc/init/permissiver.rc',
            b'# SELinux remains enforcing in this AVD.\n',
            '0100644', 'u:object_r:system_file:s0', True)
    # CameraActivitySceneMode in miui-services.jar writes these three booleans
    # at boot. The ranchu vendor lacks Xiaomi's property context entries.
    target = '/system_ext/etc/selinux/system_ext_property_contexts'
    contexts = debugfs(image, 'cat ' + target)
    contexts += b'\n# Xiaomi CameraServiceProxy scene properties on the AVD.\n'
    for scene in ('3rdhighResolutionBlob', '3rdlive', '3rdvideocall'):
        name = f'persist.vendor.camera.{scene}.scenes'.encode()
        assert name not in contexts
        contexts += name + b' u:object_r:exported_system_prop:s0 exact bool\n'
    install(image, target, contexts, '0100644', 'u:object_r:system_file:s0', True)
    # The GSI labels logcat as logd_exec, preventing ordinary ADB shell execution.
    install(image, '/system/bin/logcat', debugfs(image, 'cat /system/bin/logcat'),
            '0100755', 'u:object_r:logcat_exec:s0', True)
    # Match AOSP's userdebug permission for Settings' log persistence controls.
    # The GSI is debuggable but its user-build policy omits this narrow allow.
    policy_target = '/system_ext/etc/selinux/system_ext_sepolicy.cil'
    policy = debugfs(image, 'cat ' + policy_target)
    policy += b'\n; AOSP userdebug Settings log persistence controls.\n'
    policy += b'(allow system_app logpersistd_logging_prop (property_service (set)))\n'
    install(image, policy_target, policy, '0100644', 'u:object_r:system_file:s0', True)
    # A synchronous SESSION_END can enter the framework while GnssHal.stop()
    # still owns its monitor. Dispatch status callbacks after that call returns.
    services = ROOT / 'work/services-original.jar'
    services.write_bytes(debugfs(image, 'cat /system/framework/services.jar'))
    fixed = patch_gnss(services, ROOT / 'work/services-gps-fixed.jar')
    install(image, '/system/framework/services.jar', fixed,
            '0100644', 'u:object_r:system_file:s0', True)
    # The GSI ships only a 32-bit drmserver, which cannot execute under ARM64 HVF.
    # The API 36 AOSP executable uses the existing GSI framework libraries.
    install(image, '/system/bin/drmserver', erofs(ROOT / 'work/base/system.img', '/system/bin/drmserver'),
            '0100755', 'u:object_r:drmserver_exec:s0', True)
    for p in args.base.iterdir():
        if p.is_file() and p.name not in ('system.img', 'package.xml'):
            shutil.copy2(p, ROOT / 'images' / p.name)
    # Host modem setup reads these profiles from the image directory.
    for relative in ['data/misc/modem_simulator', 'data/misc/emulator']:
        shutil.copytree(args.base / relative, ROOT / 'images' / relative, dirs_exist_ok=True)
    # Official ksud supports AVD ramdisks directly. Keep the ranchu GKI kernel
    # and virtual device drivers; load KernelSU as an early-boot module.
    KSU_TOOL.chmod(0o755)
    with (ROOT / 'logs/kernelsu-patch.log').open('wb') as log:
        subprocess.run([str(KSU_TOOL), 'boot-patch', '-b', str(ROOT / 'images/ramdisk.img'),
                        '--ramdisk', '--kmi', 'android15-6.6', '--allow-shell',
                        '-m', str(KSU_MODULE), '-o', str(ROOT / 'work'),
                        '--out-name', 'ramdisk-kernelsu.img'],
                       check=True, stdout=log, stderr=subprocess.STDOUT)
    shutil.copy2(ROOT / 'work/ramdisk-kernelsu.img', ROOT / 'images/ramdisk.img')
    create_userdata()
    print('Writing GPT + vbmeta + super image.', flush=True)
    pack(args.base / 'system.img', ROOT / 'images/system.img', [
        ('system', image), ('vendor', vendor), ('system_dlkm', ROOT / 'work/base/system_dlkm.img')])
    assert {p['name'] for p in read_lp(ROOT / 'images/system.img')[1]} == {'system', 'vendor', 'system_dlkm'}
    print('Image ready. Start-HyperOS.command launches the configured project AVD.', flush=True)


if __name__ == '__main__':
    main()
