#!/usr/bin/env python3
"""Build an isolated AVD GSI candidate from the official hongkong Android 17 OTA."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import zipfile

SOURCE_SHA256 = 'c45f3fcaccb74d0d8212f68a721814abf229a537f8a8ac6d227be20b348752f2'


def merged_fstab(data):
    lines = data.splitlines(keepends=True)
    if sum(line.startswith((b'product ', b'system_ext ')) for line in lines) != 4:
        raise RuntimeError('Unexpected first-stage partition mount table.')
    return b''.join(b'#' + line[1:] if line.startswith((b'product ', b'system_ext ')) else line
                    for line in lines)


def patch_ramdisk(path):
    from common import tool
    lz4 = tool('lz4', 'lz4')
    raw = bytearray(subprocess.check_output([lz4, '-dc', str(path)]))
    cursor, matches = 0, 0
    while raw[cursor:cursor + 6] == b'070701':
        header = raw[cursor:cursor + 110]
        size, name_size = int(header[54:62], 16), int(header[94:102], 16)
        name = raw[cursor + 110:cursor + 110 + name_size].rstrip(b'\0')
        start = (cursor + 110 + name_size + 3) // 4 * 4
        if name == b'first_stage_ramdisk/fstab.ranchu':
            raw[start:start + size] = merged_fstab(raw[start:start + size])
            matches += 1
        if name == b'TRAILER!!!':
            break
        cursor = (start + size + 3) // 4 * 4
    if matches != 1:
        raise RuntimeError('Expected exactly one ranchu fstab in the newc ramdisk.')
    temporary = Path(path).with_suffix('.next.img')
    subprocess.run([lz4, '-l', '-f', '-', str(temporary)], input=raw, check=True, capture_output=True)
    if subprocess.check_output([lz4, '-dc', str(temporary)]) != raw:
        raise RuntimeError('Ramdisk verification failed.')
    temporary.replace(path)


def main():
    repo = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--zip', type=Path, default=repo / 'hongkong-ota_full-OS4.0.17.0.XFRCNXM-user-17.0-9cf2afb0fc.zip')
    parser.add_argument('--partitions', type=Path, default=repo / 'work/os4-official/input/hongkong-4.0.17')
    parser.add_argument('--workspace', type=Path, default=repo / 'work/os4-official')
    parser.add_argument('--preinstalled-apps', type=Path,
                        help='Directory containing the two verified original signed store APKs')
    parser.add_argument('--diagnostic-adb', action='store_true',
                        help='Temporarily disable ADB authentication on this isolated candidate')
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    if workspace in (repo, repo / 'work') or workspace in repo.parents:
        raise RuntimeError('Use a separate official candidate workspace.')
    os.environ['HYPEROS_AVD_WORKSPACE'] = str(workspace)
    from common import ROOT, fetch_ksu, firmware_idle, host_check, sdk_path, sha256
    from build_image import erofs
    from erofs_image import build
    from init_userdata import create
    from lp_image import pack, unpack
    from patch_gnss import patch
    from patch_flutter import patch as patch_flutter
    from preinstall_os4_apps import REMOVALS, replacements as app_replacements, validated_apks
    from os4_defaults import image_replacements as default_replacements
    from setup import configure
    host_check()
    firmware_idle(5574)
    app_bundle = args.preinstalled_apps.resolve() if args.preinstalled_apps else ROOT / 'input/preinstalled-apps'
    validated_apks(app_bundle)
    archive_sha256 = sha256(args.zip)
    if archive_sha256 != SOURCE_SHA256:
        raise RuntimeError('Use the original supplied official hongkong OS4.0.17.0 ZIP.')
    with zipfile.ZipFile(args.zip) as archive:
        metadata = dict(line.split('=', 1) for line in archive.read('META-INF/com/android/metadata').decode().splitlines() if '=' in line)
    if metadata.get('pre-device') != 'hongkong' or '/17OS4.0.260925.' not in metadata.get('post-build', ''):
        raise RuntimeError('This experiment is pinned to the supplied official hongkong OS4 OTA.')
    base = sdk_path() / 'system-images/android-36/google_apis_playstore/arm64-v8a'
    if 'Pkg.Revision=7' not in (base / 'source.properties').read_text().splitlines():
        raise RuntimeError('Use the verified API 36 ARM64 revision 7 hardware base.')
    for directory in ('work', 'images', 'logs', 'config', 'local', 'tools'):
        (ROOT / directory).mkdir(parents=True, exist_ok=True)
    partitions = args.partitions.resolve()
    required = ('system', 'system_ext', 'product', 'mi_ext', 'mi_product')
    if any(not (partitions / (name + '.img')).exists() for name in required):
        dumper = repo / 'tools/payload-dumper-go'
        if not dumper.is_file():
            raise RuntimeError('Extract the official OTA partitions with payload-dumper-go first.')
        subprocess.run([str(dumper), '-c', '2', '-p', ','.join(required), '-o', str(partitions),
                        str(args.zip.resolve())], check=True)
    system = partitions / 'system.img'
    prop = erofs(system, '/system/build.prop')
    product = erofs(partitions / 'product.img', '/etc/build.prop')
    if b'ro.build.version.sdk=37\n' not in prop or b'OS4.0.17.0.XFRCNXM' not in product:
        raise RuntimeError('Unexpected source partition version.')
    # Xiaomi loads mi_ext properties through its phone vendor init. Keep the
    # software identity here without importing Qualcomm hardware properties.
    mi_ext_prop = erofs(partitions / 'mi_ext.img', '/etc/build.prop')
    software_prefixes = (b'ro.mi.', b'ro.miui.', b'ro.ai.', b'ro.com.google.')
    software_keys = (b'ro.product.mod_device', b'ro.build.version.incremental',
                     b'ro.product.build.version.incremental', b'ro.build.version.smr_baseversion',
                     b'persist.sys.pre_startup')
    identity = [line for line in mi_ext_prop.splitlines()
                if line.startswith(software_prefixes) or line.partition(b'=')[0] in software_keys]
    if b'ro.mi.os.version.incremental=OS4.0.17.0.XFRCNXM' not in identity:
        raise RuntimeError('Missing official HyperOS identity properties.')
    product += b'\n# Official mi_ext software identity for the ranchu vendor.\n'
    product += b'\n'.join(identity) + b'\n'
    print('Preparing the official OS4 candidate on a separate AVD.', flush=True)
    unpack(base / 'system.img', ROOT / 'work/base')
    vendor = ROOT / 'work/vendor.img'
    shutil.copyfile(ROOT / 'work/base/vendor.img', vendor)
    fstab = erofs(vendor, '/etc/fstab.ranchu')
    patched_fstab = merged_fstab(fstab)
    content = vendor.read_bytes()
    if content.count(fstab) != 1 or content.count(b'ro.zygote.disable_gl_preload=1') != 1:
        raise RuntimeError('Unexpected ranchu vendor layout.')
    vendor.write_bytes(content.replace(fstab, patched_fstab).replace(
        b'ro.zygote.disable_gl_preload=1', b'ro.zygote.disable_gl_preload=0'))
    replacements = {}
    def put(path, data, mode=0o644, label='u:object_r:system_file:s0'):
        replacements[path] = (data, mode, label)
    prop = prop.replace(
        b'media.settings.xml=/vendor/etc/media_profiles_vendor.xml',
        b'media.settings.xml=/vendor/etc/media_profiles_V1_0.xml')
    if args.diagnostic_adb:
        prop = prop.replace(b'ro.adb.secure=1', b'ro.adb.secure=0')
    prop += b'\n# HyperOS-AVD virtual hardware and native effects.\n'
    prop += b'ro.mediaserver.64b.enable=true\ndebug.hwui.renderer=skiagl\n'
    prop += b'debug.renderengine.backend=skiavkthreaded\n'
    # Activate the original Xiaomi product RRO, including RecentsActivity.
    prop += b'ro.miui.product.home=com.miui.home\n'
    put('system/build.prop', prop, 0o600)
    put('system_ext/lib64/libhyper_os_flutter.so',
        patch_flutter(erofs(partitions / 'system_ext.img', '/lib64/libhyper_os_flutter.so')),
        label='u:object_r:system_lib_file:s0')
    put('product/etc/build.prop', product)
    name = 'init.ranchu.adb.setup.sh'
    domain = 'goldfish_system_setup'
    put('system_ext/bin/' + name, erofs(ROOT / 'work/base/system_ext.img', '/bin/' + name),
        0o755, 'u:object_r:' + domain + '_exec:s0')
    contexts = erofs(partitions / 'system_ext.img', '/etc/selinux/system_ext_file_contexts')
    contexts += ('\n/system_ext/bin/' + re.escape(name) + ' u:object_r:' + domain + '_exec:s0\n').encode()
    put('system_ext/etc/selinux/system_ext_file_contexts', contexts)
    prop_contexts = erofs(partitions / 'system_ext.img', '/etc/selinux/system_ext_property_contexts')
    prop_contexts += b'\n# Emulator boot transport and camera scene properties.\n'
    prop_contexts += b'ro.boot.qemu. u:object_r:bootloader_prop:s0\n'
    prop_contexts += b'ro.boot.hardware.gltransport u:object_r:bootloader_prop:s0 exact string\n'
    for scene in ('3rdhighResolutionBlob', '3rdlive', '3rdvideocall'):
        key = ('persist.vendor.camera.' + scene + '.scenes').encode()
        if key not in prop_contexts:
            prop_contexts += key + b' u:object_r:exported_system_prop:s0 exact bool\n'
    put('system_ext/etc/selinux/system_ext_property_contexts', prop_contexts)
    init = erofs(ROOT / 'work/base/system_ext.img', '/etc/init/init.system_ext.rc')
    init += b'\n' + erofs(ROOT / 'work/base/system_ext.img', '/etc/init/init.system_ext.radio.rc')
    init += b'\non post-fs-data\n    setprop persist.sys.usb.config adb\n    setprop sys.usb.config adb\n'
    init += b'    setprop debug.renderengine.backend skiavkthreaded\n'
    init += b'    setprop persist.sys.background_blur_supported true\n'
    init += b'\non property:ro.persistent_properties.ready=true\n    setprop persist.sys.background_blur_supported true\n'
    from os4_defaults import boot_defaults
    put('system_ext/etc/init/init.hyperos_avd.rc', boot_defaults(init))
    cached = ('ksud-aarch64-apple-darwin', 'lkm-aarch64-android15-6.6_kernelsu.ko',
              'ksud-aarch64-linux-android', 'KernelSU_v3.3.0_32601-release.apk')
    for name in cached:
        shutil.copy2(repo / 'tools' / name, ROOT / 'tools' / name)
    fetch_ksu(cached)
    shutil.copytree(repo / 'tools/smali', ROOT / 'tools/smali', dirs_exist_ok=True)
    services = ROOT / 'work/services-original.jar'
    services.write_bytes(erofs(system, '/system/framework/services.jar'))
    put('system/framework/services.jar', patch(services, ROOT / 'work/services-gps-fixed.jar'))
    app_edits, app_manifest = app_replacements(app_bundle, system, ROOT / 'work/preinstalled-native', sdk_path())
    replacements.update(app_edits)
    default_edits, default_manifest = default_replacements(sdk_path(), ROOT / 'work/defaults-overlay')
    replacements.update(default_edits)
    image = ROOT / 'work/hyperos-system.img'
    # Flatten the phone overlay mounts. Merely retaining /mi_ext does not make
    # its permissions, runtime declarations or product resources visible.
    build(image, [('', system), *[(name, partitions / (name + '.img'))
                                for name in ('system_ext', 'product', 'mi_ext')],
                  ('product:product', partitions / 'mi_product.img'),
                  *[(name + ':' + name, partitions / 'mi_ext.img')
                    for name in ('system', 'system_ext', 'product')]],
          ROOT / 'work/official-tree', replacements, removals=REMOVALS)
    for path in base.iterdir():
        if path.is_file() and path.name not in ('system.img', 'package.xml'):
            shutil.copy2(path, ROOT / 'images' / path.name)
    for relative in ('data/misc/modem_simulator', 'data/misc/emulator'):
        shutil.copytree(base / relative, ROOT / 'images' / relative, dirs_exist_ok=True)
    with (ROOT / 'logs/kernelsu-patch.log').open('wb') as log:
        subprocess.run([str(ROOT / 'tools/ksud-aarch64-apple-darwin'), 'boot-patch',
                        '-b', str(ROOT / 'images/ramdisk.img'), '--ramdisk', '--kmi', 'android15-6.6',
                        '--allow-shell', '-m', str(ROOT / 'tools/lkm-aarch64-android15-6.6_kernelsu.ko'),
                        '-o', str(ROOT / 'work'), '--out-name', 'ramdisk-kernelsu.img'],
                       check=True, stdout=log, stderr=subprocess.STDOUT)
    shutil.copy2(ROOT / 'work/ramdisk-kernelsu.img', ROOT / 'images/ramdisk.img')
    # EROFS first-stage mounts retain the ramdisk fstab across switch_root.
    # Disable mounts already embedded in the standalone system image there too.
    patch_ramdisk(ROOT / 'images/ramdisk.img')
    create()
    pack(base / 'system.img', ROOT / 'images/system.img', [
        ('system', image), ('vendor', vendor), ('system_dlkm', ROOT / 'work/base/system_dlkm.img')])
    template = (repo / 'config/avd.ini').read_text().replace('HyperOS 3 - Android 16',
        'HyperOS 4 Official - Android 17').replace('target=android-36', 'target=android-37.0')
    template = template.replace('hw.ramSize=2560', 'hw.ramSize=4096')
    template = template.replace('hw.cpu.ncore=2', 'hw.cpu.ncore=4')
    from os4_defaults import display_template
    template = display_template(template)
    (ROOT / 'config/avd.ini').write_text(template)
    (ROOT / 'local/build.json').write_text(json.dumps({
        'hyperos': '4.0.17.0.XFRCNXM', 'source': 'official-hongkong-ota',
        'android_api': 37, 'archive_sha256': archive_sha256, 'kernel_page_size': 4096,
        'hardware_base_api': 36, 'kernel_kmi': 'android15-6.6',
        'surfaceflinger_backend': 'skiavkthreaded', 'gnss_patch': True,
        'mi_ext_overlays': True, 'mi_ext_software_identity': True, 'flutter_render_fix': 6,
        'preinstalled_apps': app_manifest,
        'native_quickstep_identity': True,
        'avd_defaults': default_manifest,
        'adb_authentication': not args.diagnostic_adb,
        'experimental': True, 'ota_metadata': metadata}, indent=2) + '\n')
    configure(sdk_path(), 'HyperOS_4_Official_API_37', 5574)
    print('Official GSI candidate ready. Use Start-HyperOS4-Official.command.', flush=True)


if __name__ == '__main__':
    main()
