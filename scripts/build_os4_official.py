#!/usr/bin/env python3
"""Build an isolated AVD GSI candidate from the official hongkong Android 17 OTA."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import zipfile

SOURCE_PARTITIONS = ('system', 'system_ext', 'product', 'mi_ext', 'mi_product',
                     'vendor', 'odm', 'system_dlkm', 'vendor_dlkm')


def archive_digest(path):
    """Resolve the source before importing workspace-scoped build modules."""
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(8 * 1024**2), b''):
            digest.update(block)
    return digest.hexdigest()


def candidate_defaults(profile, repo):
    """Keep a new OTA build away from the existing 4.0.17 workspace."""
    if profile['hyperos'] == '4.0.17.0.XFRCNXM':
        return repo / 'work/os4-official', 'HyperOS_4_Official_API_37', 5574
    if profile['hyperos'] == '4.0.18.0.XFRCNXM':
        return repo / 'work/os4-r3-build', 'HyperOS_4_R3_API_37', 5584
    raise RuntimeError('No isolated build defaults exist for this phone OTA.')


def validate_workspace_firmware(profile, workspace):
    """Keep retained userdata on its verified firmware before any build writes."""
    workspace = Path(workspace)
    if (profile['hyperos'] != '4.0.18.0.XFRCNXM'
            or not any(path.is_file() for path in (workspace / 'avd').glob('*.avd/userdata-qemu.img*'))):
        return
    from phone_profile import profile_from_build
    try:
        saved = json.loads((workspace / 'local/build.json').read_text())
        selected = profile_from_build(saved) if isinstance(saved, dict) else {}
        verified = (isinstance(saved, dict)
                    and saved.get('source') == 'official-hongkong-ota'
                    and saved.get('hyperos') == profile['hyperos']
                    and saved.get('archive_sha256') == selected.get('archive_sha256')
                    and saved.get('archive_sha256') == profile['archive_sha256'])
    except (OSError, ValueError, KeyError, RuntimeError):
        verified = False
    if not verified:
        raise RuntimeError('Use a separate candidate workspace or Installer 1.2.0 Upgrade or Recover '
                           'for retained OS4 userdata; direct builds require verified matching '
                           'firmware and OTA identity, including with --no-configure.')


def phone_renderer(profile):
    """Keep r2 reproducible while selecting the verified r3 Vulkan path."""
    if profile['hyperos'] == '4.0.17.0.XFRCNXM':
        return 'skiagl'
    if profile['hyperos'] == '4.0.18.0.XFRCNXM':
        return 'skiavk'
    raise RuntimeError('No verified phone renderer exists for this OTA.')


def renderer_properties(profile):
    return ('ro.mediaserver.64b.enable=true\n'
            'debug.hwui.renderer=' + phone_renderer(profile) + '\n'
            'debug.renderengine.backend=skiavkthreaded\n').encode()


def renderer_init(profile):
    # Keep the r2 init byte-for-byte unchanged. r3 must also override persisted
    # runtime renderer settings before apps fork from the zygote.
    hwui = (b'    setprop debug.hwui.renderer skiavk\n'
            if phone_renderer(profile) == 'skiavk' else b'')
    return hwui + b'    setprop debug.renderengine.backend skiavkthreaded\n'


def native_hwui(profile, data):
    """Bind a source-pinned preload fix to the r3 Vulkan renderer only."""
    from patch_pad_hwui import PHONE_BEFORE, PHONE_PROFILE, patch
    if hashlib.sha256(data).hexdigest() != profile['pins']['hwui']:
        raise RuntimeError('Unexpected phone HWUI source SHA-256.')
    if phone_renderer(profile) == 'skiagl':
        return data, None
    if profile['pins']['hwui'] != PHONE_BEFORE:
        raise RuntimeError('No verified phone Vulkan HWUI preload profile.')
    fixed = patch(data)
    return fixed, {'before': PHONE_BEFORE, 'after': hashlib.sha256(fixed).hexdigest(),
                   'profile': PHONE_PROFILE}


def lockscreen_video_replacements(profile, apk):
    """Add the verified phone r3 native producer beside the signed AOD APK."""
    if profile['hyperos'] == '4.0.17.0.XFRCNXM':
        return {}, None
    if profile['hyperos'] != '4.0.18.0.XFRCNXM':
        raise RuntimeError('No verified phone lockscreen video profile for this OTA.')
    from patch_lockscreen_video import APK_SHA256, MANIFEST, NATIVE, native_from_apk
    if profile.get('pins', {}).get('aod_apk') != APK_SHA256:
        raise RuntimeError('No verified phone lockscreen video APK source pin.')
    return {NATIVE.lstrip('/'): (native_from_apk(apk), 0o644,
                                'u:object_r:system_lib_file:s0')}, dict(MANIFEST)


def validate_candidate(name, port, workspace, registry_home=None):
    """Refuse invalid or foreign registrations before touching firmware."""
    if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', name):
        raise RuntimeError('Use an ASCII AVD name up to 64 characters.')
    if isinstance(port, bool) or not isinstance(port, int) or port % 2 or not 5556 <= port <= 5682:
        raise RuntimeError('Use a free, even emulator console port between 5556 and 5682.')
    if registry_home is not None:
        registry = Path(registry_home) / (name + '.ini')
        if registry.exists():
            values = dict(line.split('=', 1) for line in registry.read_text().splitlines() if '=' in line)
            registered = values.get('path')
            expected = Path(workspace).resolve() / 'avd' / (name + '.avd')
            if not registered or Path(registered).expanduser().resolve() != expected:
                raise RuntimeError(f'AVD {name} belongs to another workspace. Use --name with a new name.')


def avd_template(original, profile):
    """Apply the published phone hardware defaults to a portable template."""
    from os4_defaults import display_template
    values = dict(line.split('=', 1) for line in original.splitlines() if '=' in line)
    values.update({'avd.ini.displayname': 'HyperOS 4 Official - Android 17',
                   'target': 'android-37.0', 'hw.ramSize': '6144', 'hw.cpu.ncore': '4',
                   'disk.dataPartition.size': '32G', 'hw.audioOutput': 'yes',
                   'hw.camera.back': 'virtualscene', 'hw.camera.front': 'emulated'})
    if profile['hyperos'] == '4.0.18.0.XFRCNXM':
        values.update({'hw.hotplug_multi_display': 'no', 'hw.multi_display_window': 'yes'})
    template = ''.join(key + '=' + value + '\n' for key, value in values.items())
    return display_template(template, profile=profile)


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
    parser.add_argument('--partitions', type=Path,
                        help='Extracted source partitions; defaults to the selected candidate workspace')
    parser.add_argument('--workspace', type=Path,
                        help='Separate build workspace; each supported OTA has an isolated default')
    parser.add_argument('--name', help='AVD name to register, preserving all other registrations')
    parser.add_argument('--port', type=int, help='Free even emulator console port')
    parser.add_argument('--no-configure', action='store_true',
                        help='Build firmware and its template without registering or reconfiguring an AVD')
    parser.add_argument('--preinstalled-apps', type=Path,
                        help='Directory containing the two verified original signed store APKs')
    parser.add_argument('--diagnostic-adb', action='store_true',
                        help='Temporarily disable ADB authentication on this isolated candidate')
    args = parser.parse_args()
    from phone_profile import profile_for_archive, verify_source
    archive_sha256 = archive_digest(args.zip)
    profile = profile_for_archive(archive_sha256)
    default_workspace, default_name, default_port = candidate_defaults(profile, repo)
    workspace = (args.workspace or default_workspace).resolve()
    if workspace in (repo, repo / 'work') or workspace in repo.parents:
        raise RuntimeError('Use a separate official candidate workspace.')
    validate_workspace_firmware(profile, workspace)
    os.environ['HYPEROS_AVD_WORKSPACE'] = str(workspace)
    from common import (ROOT, OS4_SOURCE, avd_home, fetch_ksu,
                        firmware_idle, host_check, port_free, sdk_path)
    from build_image import erofs
    from erofs_image import build
    from init_userdata import create
    from lp_image import pack, unpack
    from patch_gnss import patch
    from patch_flutter import patch as patch_flutter
    from patch_assistant import APK as ASSISTANT_APK, image_replacements as assistant_replacements
    from patch_composer import build_vendor
    from patch_camera_scene import build_vendor as build_scene_vendor
    from patch_audio import build_vendor as build_audio_vendor
    from preinstall_os4_apps import REMOVALS, replacements as app_replacements, validated_apks
    from os4_defaults import image_replacements as default_replacements, production_properties
    from setup import configure, select_build_instance
    host_check()
    instance_name, instance_port = select_build_instance(OS4_SOURCE, default_name, default_port)
    instance_name = args.name if args.name is not None else instance_name
    instance_port = args.port if args.port is not None else instance_port
    validate_candidate(instance_name, instance_port, ROOT,
                       None if args.no_configure else avd_home())
    firmware_idle(instance_port)
    # A requested new port may differ from the saved instance's port checked by
    # firmware_idle; reserve neither endpoint unless both are unoccupied.
    port_free(instance_port)
    with zipfile.ZipFile(args.zip) as archive:
        metadata_bytes = archive.read('META-INF/com/android/metadata')
        metadata = dict(line.split('=', 1) for line in metadata_bytes.decode().splitlines() if '=' in line)
    base = sdk_path() / 'system-images/android-36/google_apis_playstore/arm64-v8a'
    if 'Pkg.Revision=7' not in (base / 'source.properties').read_text().splitlines():
        raise RuntimeError('Use the verified API 36 ARM64 revision 7 hardware base.')
    partitions = (args.partitions or ROOT / 'input' /
                  ('hongkong-' + profile['hyperos'].split('.XFRCNXM')[0].rsplit('.', 1)[0])).resolve()
    if any(not (partitions / (name + '.img')).exists() for name in SOURCE_PARTITIONS):
        dumper = repo / 'tools/payload-dumper-go'
        if not dumper.is_file():
            raise RuntimeError('Extract the official OTA partitions with payload-dumper-go first.')
        subprocess.run([str(dumper), '-c', '2', '-p', ','.join(SOURCE_PARTITIONS), '-o', str(partitions),
                        str(args.zip.resolve())], check=True)
    # Validate the untouched OTA sources, including hardware identity sources;
    # only the five software partitions below are merged into the AVD image.
    verify_source(profile, partitions, metadata_bytes)
    app_bundle = args.preinstalled_apps.resolve() if args.preinstalled_apps else ROOT / 'input/preinstalled-apps'
    validated_apks(app_bundle)
    for directory in ('work', 'images', 'logs', 'config', 'local', 'tools'):
        (ROOT / directory).mkdir(parents=True, exist_ok=True)
    system = partitions / 'system.img'
    prop = erofs(system, '/system/build.prop')
    product = erofs(partitions / 'product.img', '/etc/build.prop')
    if b'ro.build.version.sdk=37\n' not in prop or profile['incremental'].encode() not in product:
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
    if ('ro.mi.os.version.incremental=' + profile['incremental']).encode() not in identity:
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
    composer_manifest = build_vendor(vendor, ROOT / 'work/vendor-alpha-fixed.img',
                                    ROOT / 'work/vendor-alpha-tree')
    (ROOT / 'work/vendor-alpha-fixed.img').replace(vendor)
    rear_composer_manifest = sync_manifest = None
    if profile['hyperos'] == '4.0.18.0.XFRCNXM':
        from patch_rear_display import build_vendor as build_rear_vendor
        from patch_goldfish_sync import build_vendor as build_sync_vendor
        rear_composer_manifest = build_rear_vendor(vendor, ROOT / 'work/vendor-rear-fixed.img',
                                                  ROOT / 'work/vendor-rear-tree')
        (ROOT / 'work/vendor-rear-fixed.img').replace(vendor)
        composer_manifest['native_sha256'] = rear_composer_manifest['native_sha256']
        sync_manifest = build_sync_vendor(vendor, ROOT / 'work/vendor-sync-fixed.img',
                                          ROOT / 'work/vendor-sync-tree')
        (ROOT / 'work/vendor-sync-fixed.img').replace(vendor)
    scene_manifest = build_scene_vendor(vendor, ROOT / 'work/vendor-scene-fixed.img',
                                       ROOT / 'work/vendor-scene', sdk_path())
    (ROOT / 'work/vendor-scene-fixed.img').replace(vendor)
    audio_manifest = build_audio_vendor(vendor, ROOT / 'work/vendor-audio-fixed.img',
                                       ROOT / 'work/vendor-audio')
    (ROOT / 'work/vendor-audio-fixed.img').replace(vendor)
    replacements = {}
    def put(path, data, mode=0o644, label='u:object_r:system_file:s0'):
        replacements[path] = (data, mode, label)
    prop = prop.replace(
        b'media.settings.xml=/vendor/etc/media_profiles_vendor.xml',
        b'media.settings.xml=/vendor/etc/media_profiles_V1_0.xml')
    if args.diagnostic_adb:
        prop = prop.replace(b'ro.adb.secure=1', b'ro.adb.secure=0')
    prop += b'\n# HyperOS-AVD virtual hardware and native effects.\n'
    prop += renderer_properties(profile)
    prop = production_properties(prop, profile=profile)
    if profile['hyperos'] == '4.0.18.0.XFRCNXM':
        from rear_display_config import properties as rear_properties
        prop = rear_properties(prop, profile)
    put('system/build.prop', prop, 0o600)
    fixed_hwui, hwui_manifest = native_hwui(profile, erofs(system, '/system/lib64/libhwui.so'))
    if hwui_manifest is not None:
        put('system/lib64/libhwui.so', fixed_hwui, label='u:object_r:system_lib_file:s0')
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
    init += renderer_init(profile)
    init += b'    setprop persist.sys.background_blur_supported true\n'
    init += b'\non property:ro.persistent_properties.ready=true\n    setprop persist.sys.background_blur_supported true\n'
    if profile['hyperos'] == '4.0.18.0.XFRCNXM':
        from rear_display_config import boot_init as rear_init
        init += rear_init(profile)
    from os4_defaults import boot_defaults
    put('system_ext/etc/init/init.hyperos_avd.rc', boot_defaults(init, profile=profile))
    cached = ('ksud-aarch64-apple-darwin', 'lkm-aarch64-android15-6.6_kernelsu.ko',
              'ksud-aarch64-linux-android', 'KernelSU_v3.3.0_32601-release.apk')
    for name in cached:
        shutil.copy2(repo / 'tools' / name, ROOT / 'tools' / name)
    fetch_ksu(cached)
    shutil.copytree(repo / 'tools/smali', ROOT / 'tools/smali', dirs_exist_ok=True)
    services = ROOT / 'work/services-original.jar'
    services.write_bytes(erofs(system, '/system/framework/services.jar'))
    fixed_services = patch(services, ROOT / 'work/services-gps-fixed.jar')
    put('system/framework/services.jar', fixed_services)
    boot_service_manifest = None
    if profile['hyperos'] == '4.0.18.0.XFRCNXM':
        from patch_boot_services import image_replacements as service_replacements
        service_edits, boot_service_manifest = service_replacements({
            'services': fixed_services,
            'miui-services': erofs(partitions / 'system_ext.img', '/framework/miui-services.jar'),
            'qualcomm': erofs(partitions / 'system_ext.img', '/priv-app/com.qualcomm.location/com.qualcomm.location.apk'),
            'registration': erofs(partitions / 'product.img', '/priv-app/AutoRegistration/AutoRegistration.apk'),
        }, ROOT / 'work/boot-service-fixes')
        replacements.update(service_edits)
    app_edits, app_manifest = app_replacements(app_bundle, system, ROOT / 'work/preinstalled-native', sdk_path())
    replacements.update(app_edits)
    default_edits, default_manifest = default_replacements(sdk_path(), ROOT / 'work/defaults-overlay',
                                                         profile=profile)
    replacements.update(default_edits)
    assistant_edits, assistant_manifest = assistant_replacements(erofs(
        partitions / 'product.img', ASSISTANT_APK.removeprefix('/product')), firmware=profile)
    replacements.update(assistant_edits)
    launcher_manifest = None
    lockscreen_manifest = None
    rear_manifest = None
    rear_wake_manifest = None
    if profile['hyperos'] == '4.0.18.0.XFRCNXM':
        from apply_navigation_fix import image_replacements as launcher_replacements
        from patch_lockscreen_video import APK as AOD_APK
        launcher_edits, launcher_manifest = launcher_replacements(erofs(
            partitions / 'product.img', '/priv-app/MiuiHome/MiuiHome.apk'), profile)
        replacements.update(launcher_edits)
        lockscreen_edits, lockscreen_manifest = lockscreen_video_replacements(profile, erofs(
            partitions / 'product.img', AOD_APK.removeprefix('/product')))
        replacements.update(lockscreen_edits)
        from rear_display_config import image_replacements as rear_replacements
        rear_edits, rear_manifest = rear_replacements(profile, partitions, sdk_path(),
                                                     ROOT / 'work/rear-display-resources')
        replacements.update(rear_edits)
        from rear_display_wake import image_replacements as rear_wake_replacements
        rear_wake_edits, rear_wake_manifest = rear_wake_replacements(
            sdk_path(), ROOT / 'work/rear-display-wake')
        replacements.update(rear_wake_edits)
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
    template = avd_template((repo / 'config/avd.ini').read_text(), profile)
    (ROOT / 'config/avd.ini').write_text(template)
    build_metadata = {
        'hyperos': profile['hyperos'], 'source': OS4_SOURCE,
        'android_api': 37, 'archive_sha256': archive_sha256, 'kernel_page_size': 4096,
        'hardware_base_api': 36, 'kernel_kmi': 'android15-6.6',
        'surfaceflinger_backend': 'skiavkthreaded', 'gnss_patch': True,
        'hwui_renderer': phone_renderer(profile), 'hwui': hwui_manifest,
        'mi_ext_overlays': True, 'mi_ext_software_identity': True, 'flutter_render_fix': 6,
        'preinstalled_apps': app_manifest,
        'native_quickstep_identity': True,
        'factory_launcher_deadlines': launcher_manifest,
        'assistant_render_fix': assistant_manifest,
        'composer_alpha_fix': composer_manifest,
        'camera_scene_fix': scene_manifest,
        'audio_pcm_fix': audio_manifest,
        'avd_defaults': default_manifest,
        'adb_authentication': not args.diagnostic_adb,
        'experimental': True, 'ota_metadata': metadata}
    if lockscreen_manifest is not None:
        build_metadata['lockscreen_video_fix'] = lockscreen_manifest
    if boot_service_manifest is not None:
        build_metadata['boot_service_fix'] = boot_service_manifest
    if rear_manifest is not None:
        build_metadata['rear_display'] = rear_manifest
        build_metadata['rear_display_composer_fix'] = rear_composer_manifest
        build_metadata['goldfish_sync_fix'] = sync_manifest
        build_metadata['rear_display_wake_fix'] = rear_wake_manifest
    (ROOT / 'local/build.json').write_text(json.dumps(build_metadata, indent=2) + '\n')
    if not args.no_configure:
        configure(sdk_path(), instance_name, instance_port)
    print('Official GSI candidate ready.' + (' No AVD registration was changed.' if args.no_configure
                                           else ' Use Start-HyperOS4-Official.command.'), flush=True)


if __name__ == '__main__':
    main()
