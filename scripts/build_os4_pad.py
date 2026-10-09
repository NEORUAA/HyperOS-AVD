#!/usr/bin/env python3
"""Build the supplied official yingtian OTA as an isolated tablet AVD."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import zipfile


def identity_values(build_props, metadata_bytes):
    """Select official public identity without importing hardware selectors."""
    values = {part: dict(line.split('=', 1) for line in data.decode().splitlines()
                         if '=' in line and not line.startswith('#'))
              for part, data in build_props.items()}
    metadata = dict(line.split('=', 1) for line in metadata_bytes.decode().splitlines()
                    if '=' in line)
    result = {}
    fields = ('brand', 'device', 'manufacturer', 'model', 'name', 'marketname', 'cert')
    for part in ('system', 'system_ext', 'product', 'vendor', 'odm'):
        for field in fields:
            key = f'ro.product.{part}.{field}'
            if key in values[part]:
                result[key] = values[part][key]
        fingerprint = f'ro.{part}.build.fingerprint'
        if fingerprint in values[part]:
            result[fingerprint] = values[part][fingerprint]
    # Global Build identity follows the official ODM model, while each partition
    # keeps its own original fingerprint. Build.BOARD is a public identifier.
    for field in fields[:-1]:
        key = 'ro.product.odm.' + field
        if key in values['odm']:
            result['ro.product.' + field] = values['odm'][key]
    for field in ('brand', 'device', 'manufacturer', 'model', 'name'):
        key = 'ro.product.' + field + '_for_attestation'
        if key in values['odm']:
            result[key] = values['odm'][key]
    for key in ('ro.product.first_api_level', 'ro.product.board',
                'ro.soc.model', 'ro.soc.manufacturer'):
        if key in values['vendor']:
            result[key] = values['vendor'][key]
    result['ro.build.characteristics'] = values['product']['ro.build.characteristics']
    result['ro.build.fingerprint'] = metadata['post-build']
    return result


def verify_identity_sources(profile, build_props, metadata_bytes):
    """Reject stale extracted partitions or non-source identity before image edits."""
    sources = {part + '.build.prop': data for part, data in build_props.items()}
    sources['ota.metadata'] = metadata_bytes
    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in sources.items()}
    if hashes != profile.get('source_sha256'):
        raise RuntimeError('The yingtian identity sources differ from the verified OTA.')
    if identity_values(build_props, metadata_bytes) != profile.get('properties'):
        raise RuntimeError('The yingtian public identity differs from its verified sources.')


def settings_replacements(sdk, folder):
    """Build only the shared awake defaults RRO and log filter for this image."""
    from common import sha256
    from os4_defaults import DEFAULTS_XML, LOG_SCRIPT, OVERLAY, SCREEN_TIMEOUT
    from patch_gnss import java
    sdk, folder = Path(sdk), Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    tools = sdk / 'build-tools/37.0.0'
    framework = sdk / 'platforms/android-36/android.jar'
    resources = folder / 'res/values'
    if any(path.is_file() and path != resources / 'defaults.xml'
           for path in (folder / 'res').rglob('*')):
        raise RuntimeError('Unexpected cached resources in the tablet defaults overlay.')
    resources.mkdir(parents=True, exist_ok=True)
    (resources / 'defaults.xml').write_text(DEFAULTS_XML)
    manifest = folder / 'AndroidManifest.xml'
    # The OEM SettingsProvider overlay uses priority 999. A tie sorts by APK
    # path and lets its 120000 ms timeout override this image's awake default.
    manifest.write_text(f'''<manifest xmlns:android="http://schemas.android.com/apk/res/android"
    package="{OVERLAY}" android:versionCode="1" android:versionName="1">
    <uses-sdk android:minSdkVersion="21" android:targetSdkVersion="28" />
    <overlay android:targetPackage="com.android.providers.settings"
        android:isStatic="true" android:priority="1000" />
    <application android:hasCode="false" />
</manifest>
''')
    compiled, unsigned, signed = (folder / name for name in
                                  ('resources.zip', 'unsigned.apk', 'SettingsDefaults.apk'))
    subprocess.run([str(tools / 'aapt2'), 'compile', '--dir', str(folder / 'res'),
                    '-o', str(compiled)], check=True, capture_output=True)
    subprocess.run([str(tools / 'aapt2'), 'link', '-o', str(unsigned), '-I', str(framework),
                    '--manifest', str(manifest), str(compiled)], check=True, capture_output=True)
    keystore = folder / 'overlay.jks'
    if not keystore.exists():
        subprocess.run([str(Path(java()).parent / 'keytool'), '-genkeypair', '-keystore', str(keystore),
                        '-storepass', 'android', '-keypass', 'android', '-alias', 'overlay',
                        '-dname', 'CN=HyperOS AVD Defaults', '-keyalg', 'RSA', '-keysize', '2048',
                        '-validity', '10000', '-noprompt'], check=True, capture_output=True)
    environment = dict(os.environ, JAVA_HOME=str(Path(java()).parent.parent))
    subprocess.run([str(tools / 'apksigner'), 'sign', '--ks', str(keystore), '--ks-key-alias', 'overlay',
                    '--ks-pass', 'pass:android', '--key-pass', 'pass:android',
                    '--out', str(signed), str(unsigned)], check=True, capture_output=True, env=environment)
    subprocess.run([str(tools / 'apksigner'), 'verify', str(signed)],
                   check=True, capture_output=True, env=environment)
    label = 'u:object_r:system_file:s0'
    edits = {'product/overlay/HyperOSAVDSettingsDefaults/SettingsDefaults.apk':
             (signed.read_bytes(), 0o644, label),
             'system_ext/bin/kill_HyperOS_Log.sh': (LOG_SCRIPT, 0o755, label)}
    marker = {'settings_overlay': OVERLAY, 'settings_overlay_sha256': sha256(signed),
              'screen_off_timeout': SCREEN_TIMEOUT, 'sleep_timeout': -1,
              # SettingsProvider converts the RRO boolean to 1 (AC only).
              'first_boot_stay_on_while_plugged_in': 1,
              # The runtime initializer enables AC, USB and wireless power.
              'stay_on_while_plugged_in': 7,
              'log_script_sha256': hashlib.sha256(LOG_SCRIPT).hexdigest()}
    return edits, marker


def main():
    repo = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--zip', type=Path, default=repo / 'yingtian-ota_full-OS4.0.15.0.XBMCNXM-user-17.0-748c6d2437.zip')
    parser.add_argument('--workspace', type=Path, default=repo / 'work/os4-pad')
    parser.add_argument('--diagnostic-adb', action='store_true')
    parser.add_argument('--weather-angle-libs', type=Path,
                        help='Directory containing the two verified private Weather ANGLE libraries')
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    if workspace != repo / 'work/os4-pad':
        raise RuntimeError('The tablet experiment must use work/os4-pad; other firmware is preserved.')
    os.environ['HYPEROS_AVD_WORKSPACE'] = str(workspace)
    from common import ROOT, fetch_ksu, firmware_idle, host_check, sdk_path, sha256
    from build_image import erofs
    from build_os4_official import merged_fstab, patch_ramdisk
    from erofs_image import build
    from init_userdata import create
    from lp_image import pack, unpack
    from os4_pad import (FINDDEVICE_SHA256, NAME, PORT, PROFILE, SOURCE,
                         model_config, properties, refresh_script, template)
    from patch_gnss import patch as patch_gnss
    from patch_composer import build_vendor as composer_vendor
    from patch_camera_scene import build_vendor as scene_vendor
    from patch_audio import build_vendor as audio_vendor
    from patch_flutter import patch as flutter_patch
    from patch_pad_hwui import patch as hwui_patch
    from patch_assistant import APK as ASSISTANT_APK, image_replacements as assistant_replacements
    from setup import configure, select_build_instance
    host_check()
    instance_name, instance_port = select_build_instance(SOURCE, NAME, PORT)
    firmware_idle(instance_port)
    for name in ('work', 'images', 'logs', 'config', 'local', 'tools'):
        (ROOT / name).mkdir(parents=True, exist_ok=True)
    if args.weather_angle_libs:
        from patch_weather import ANGLE
        for name, checksum in ANGLE.items():
            if sha256(args.weather_angle_libs / name) != checksum:
                raise RuntimeError('Unsupported private Weather ANGLE library: ' + name)
        destination = ROOT / 'tools/weather-angle'
        destination.mkdir(exist_ok=True)
        for name in ANGLE:
            shutil.copy2(args.weather_angle_libs / name, destination / name)
    if sha256(args.zip) != PROFILE['source_archive_sha256']:
        raise RuntimeError('Use the original supplied yingtian OS4.0.15.0 full OTA.')
    with zipfile.ZipFile(args.zip) as archive:
        metadata_bytes = archive.read('META-INF/com/android/metadata')
        metadata = dict(line.split('=', 1) for line in metadata_bytes.decode().splitlines()
                        if '=' in line)
    if metadata.get('pre-device') != 'yingtian' or metadata.get('post-sdk-level') != '37':
        raise RuntimeError('Unexpected official tablet OTA metadata.')
    base = sdk_path() / 'system-images/android-36/google_apis_playstore/arm64-v8a'
    if 'Pkg.Revision=7' not in (base / 'source.properties').read_text().splitlines():
        raise RuntimeError('Use the verified API 36 ARM64 revision 7 hardware base.')
    partitions = ROOT / 'input/yingtian-4.0.15'
    required = ('system', 'system_ext', 'product', 'mi_ext', 'mi_product', 'vendor', 'odm')
    if any(not (partitions / (name + '.img')).is_file() for name in required):
        subprocess.run([str(repo / 'tools/payload-dumper-go'), '-c', '2', '-p',
                        ','.join(required), '-o', str(partitions), str(args.zip)], check=True)
    system = partitions / 'system.img'
    prop = erofs(system, '/system/build.prop')
    product = erofs(partitions / 'product.img', '/etc/build.prop')
    if b'ro.build.version.sdk=37\n' not in prop or PROFILE['hyperos'].encode() not in product:
        raise RuntimeError('Unexpected yingtian partition version.')
    identity_sources = {'system': prop, 'product': product,
                        'system_ext': erofs(partitions / 'system_ext.img', '/etc/build.prop'),
                        'vendor': erofs(partitions / 'vendor.img', '/build.prop'),
                        'odm': erofs(partitions / 'odm.img', '/etc/build.prop'),
                        'mi_ext': erofs(partitions / 'mi_ext.img', '/etc/build.prop')}
    verify_identity_sources(PROFILE, identity_sources, metadata_bytes)
    # Vulkan defaults require the pinned preload branch before any image edits.
    hwui = erofs(system, '/system/lib64/libhwui.so')
    patched_hwui = hwui_patch(hwui)
    original_xml = erofs(partitions / 'product.img', '/etc/device_features/yingtian.xml')
    if original_xml != model_config():
        raise RuntimeError('The tablet model XML must match the official OTA exactly.')
    mi_ext = identity_sources['mi_ext']
    keys = (b'ro.product.mod_device', b'ro.build.version.incremental',
            b'ro.build.version.smr_baseversion', b'persist.sys.pre_startup')
    identity = [line for line in mi_ext.splitlines() if line.startswith(
        (b'ro.mi.', b'ro.miui.', b'ro.ai.', b'ro.com.google.')) or line.partition(b'=')[0] in keys]
    product += b'\n# Original mi_ext software identity for the ranchu vendor.\n'
    product += b'\n'.join(identity) + b'\n'
    print('Building independent yingtian tablet candidate.', flush=True)
    unpack(base / 'system.img', ROOT / 'work/base')
    vendor = ROOT / 'work/vendor.img'
    shutil.copyfile(ROOT / 'work/base/vendor.img', vendor)
    fstab = erofs(vendor, '/etc/fstab.ranchu')
    data = vendor.read_bytes()
    if data.count(fstab) != 1 or data.count(b'ro.zygote.disable_gl_preload=1') != 1:
        raise RuntimeError('Unexpected ranchu vendor layout.')
    vendor.write_bytes(data.replace(fstab, merged_fstab(fstab)).replace(
        b'ro.zygote.disable_gl_preload=1', b'ro.zygote.disable_gl_preload=0'))
    vendor_fixes = {}
    for name, builder in (('composer', composer_vendor), ('scene', scene_vendor), ('audio', audio_vendor)):
        destination = ROOT / 'work' / ('vendor-' + name + '.img')
        arguments = [vendor, destination, ROOT / 'work' / ('vendor-' + name)]
        if name == 'scene':
            arguments.append(sdk_path())
        vendor_fixes[name] = builder(*arguments)
        destination.replace(vendor)
    edits = {}
    def put(path, data, mode=0o644, label='u:object_r:system_file:s0'):
        edits[path] = (data, mode, label)
    prop = prop.replace(b'media.settings.xml=/vendor/etc/media_profiles_vendor.xml',
                        b'media.settings.xml=/vendor/etc/media_profiles_V1_0.xml')
    if args.diagnostic_adb:
        prop = prop.replace(b'ro.adb.secure=1', b'ro.adb.secure=0')
    put('system/build.prop', properties(prop), 0o600)
    put('product/etc/build.prop', product)
    put('product/etc/device_features/emu64a.xml', original_xml)
    put('system/lib64/libhwui.so', patched_hwui,
        label='u:object_r:system_lib_file:s0')
    engine = erofs(partitions / 'system_ext.img', '/lib64/libhyper_os_flutter.so')
    engine_hash = hashlib.sha256(engine).hexdigest()
    patched = flutter_patch(engine)
    put('system_ext/lib64/libhyper_os_flutter.so', patched,
        label='u:object_r:system_lib_file:s0')
    assistant_edits, assistant_manifest = assistant_replacements(erofs(
        partitions / 'product.img', ASSISTANT_APK.removeprefix('/product')),
        source=SOURCE)
    edits.update(assistant_edits)
    setup_name = 'init.ranchu.adb.setup.sh'
    put('system_ext/bin/' + setup_name, erofs(ROOT / 'work/base/system_ext.img', '/bin/' + setup_name),
        0o755, 'u:object_r:goldfish_system_setup_exec:s0')
    contexts = erofs(partitions / 'system_ext.img', '/etc/selinux/system_ext_file_contexts')
    contexts += ('\n/system_ext/bin/' + re.escape(setup_name) +
                 ' u:object_r:goldfish_system_setup_exec:s0\n').encode()
    put('system_ext/etc/selinux/system_ext_file_contexts', contexts)
    contexts = erofs(partitions / 'system_ext.img', '/etc/selinux/system_ext_property_contexts')
    contexts += b'\nro.boot.qemu. u:object_r:bootloader_prop:s0\n'
    contexts += b'ro.boot.hardware.gltransport u:object_r:bootloader_prop:s0 exact string\n'
    put('system_ext/etc/selinux/system_ext_property_contexts', contexts)
    init = erofs(ROOT / 'work/base/system_ext.img', '/etc/init/init.system_ext.rc')
    init += b'\n' + erofs(ROOT / 'work/base/system_ext.img', '/etc/init/init.system_ext.radio.rc')
    from os4_defaults import (COMPONENT_XML, GRADIENT_BLUR_INIT, REFRESH_INIT,
                              REFRESH_SCRIPT, disable_debug_console)
    finddevice = erofs(partitions / 'product.img', '/priv-app/MIUIFindDeviceCN/MIUIFindDeviceCN.apk')
    if hashlib.sha256(finddevice).hexdigest() != FINDDEVICE_SHA256:
        raise RuntimeError('Unexpected tablet FindDevice APK.')
    put('product/etc/permissions/hyperos_avd_components.xml', COMPONENT_XML)
    init = disable_debug_console(init)
    init += b'\non post-fs-data\n    setprop persist.sys.usb.config adb\n    setprop sys.usb.config adb\n'
    init += b'    setprop debug.hwui.renderer skiavk\n'
    init += b'    setprop debug.renderengine.backend skiavkthreaded\n'
    init += b'    setprop persist.sys.background_blur_supported true\n'
    init += b'\non property:ro.persistent_properties.ready=true\n    setprop persist.sys.background_blur_supported true\n'
    put('system_ext/etc/init/init.hyperos_avd.rc', init + GRADIENT_BLUR_INIT + REFRESH_INIT)
    put('product/etc/init/lock_fps.sh', refresh_script(REFRESH_SCRIPT), 0o755)
    default_edits, default_manifest = settings_replacements(sdk_path(), ROOT / 'work/defaults-overlay')
    edits.update(default_edits)
    cached = ('ksud-aarch64-apple-darwin', 'lkm-aarch64-android15-6.6_kernelsu.ko',
              'ksud-aarch64-linux-android', 'KernelSU_v3.3.0_32601-release.apk')
    for name in cached:
        shutil.copy2(repo / 'tools' / name, ROOT / 'tools' / name)
    fetch_ksu(cached)
    shutil.copytree(repo / 'tools/smali', ROOT / 'tools/smali', dirs_exist_ok=True)
    services = ROOT / 'work/services-original.jar'
    services.write_bytes(erofs(system, '/system/framework/services.jar'))
    put('system/framework/services.jar', patch_gnss(services, ROOT / 'work/services-gps-fixed.jar'))
    from os4_boot_policy import (INIT_PATH, init_files,
                                image_replacements as boot_policy_replacements)
    merged_partitions = [('', system), *[(name, partitions / (name + '.img'))
                           for name in ('system_ext', 'product', 'mi_ext')],
                         ('product:product', partitions / 'mi_product.img'),
                         *[(name + ':' + name, partitions / 'mi_ext.img')
                           for name in ('system', 'system_ext', 'product')]]
    graph = init_files([*merged_partitions, ('vendor', vendor)], edits)
    qti_script = graph.get('product/bin/init.qti.display.sh')
    boot_edits, boot_policy_manifest = boot_policy_replacements(
        graph, qti_script, edits[INIT_PATH][0], erofs(
            partitions / 'system_ext.img', '/etc/selinux/system_ext_sepolicy.cil'),
        ROOT / 'work/boot-policy')
    edits.update(boot_edits)
    image = ROOT / 'work/hyperos-system.img'
    build(image, merged_partitions, ROOT / 'work/official-tree', edits)
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
    patch_ramdisk(ROOT / 'images/ramdisk.img')
    create()
    pack(base / 'system.img', ROOT / 'images/system.img', [
         ('system', image), ('vendor', vendor), ('system_dlkm', ROOT / 'work/base/system_dlkm.img')])
    (ROOT / 'config/avd.ini').write_text(template((repo / 'config/avd.ini').read_text()))
    manifest = {'source': SOURCE, 'device': 'yingtian', 'android_api': 37,
        'hyperos': PROFILE['hyperos'], 'archive_sha256': PROFILE['source_archive_sha256'],
        'display': PROFILE['display'], 'model_xml_sha256': PROFILE['model_xml_sha256'],
        'identity_source_sha256': PROFILE['source_sha256'],
        'hardware_base_api': 36, 'kernel_page_size': 4096,
        'vendor_fixes': vendor_fixes, 'gnss_patch': True, 'experimental': True,
        'finddevice_provider_disabled': True,
        'hwui': {'before': hashlib.sha256(hwui).hexdigest(),
                 'after': hashlib.sha256(patched_hwui).hexdigest()},
        'flutter_engine': {'before': engine_hash, 'after': hashlib.sha256(patched).hexdigest()},
        'assistant_render_fix': assistant_manifest,
        'avd_defaults': default_manifest,
        'adb_authentication': not args.diagnostic_adb, 'ota_metadata': metadata,
        'boot_policy': boot_policy_manifest}
    (ROOT / 'local/build.json').write_text(json.dumps(manifest, indent=2) + '\n')
    configure(sdk_path(), instance_name, instance_port)
    print('Tablet candidate ready. Start-HyperOS4-Pad.command boots only this test AVD.', flush=True)


if __name__ == '__main__':
    main()
