"""Fail-closed verification of the public yingtian release and pinned payloads."""
import hashlib
import io
import json
from pathlib import Path
import zipfile

from common import sha256
from os4_pad import PROFILE, SOURCE, MODEL_XML, FINDDEVICE_SHA256


def validate_build(info):
    from patch_pad_hwui import BEFORE, AFTER
    from patch_flutter import PROFILES
    from patch_assistant import profile as manifest
    from os4_defaults import LOG_SCRIPT, OVERLAY, SCREEN_TIMEOUT
    engine = '9caf8bd3413b3093ae0855f86369f31ddf45b46c7ddbaa4c2f656e4f67bb5009'
    expected = {'source': SOURCE, 'device': 'yingtian', 'android_api': 37,
        'hyperos': PROFILE['hyperos'], 'archive_sha256': PROFILE['source_archive_sha256'],
        'display': PROFILE['display'], 'model_xml_sha256': PROFILE['model_xml_sha256'],
        'identity_source_sha256': PROFILE['source_sha256'],
        'hardware_base_api': 36, 'kernel_page_size': 4096, 'gnss_patch': True,
        'experimental': True, 'finddevice_provider_disabled': True,
        'adb_authentication': True, 'hwui': {'before': BEFORE, 'after': AFTER},
        'flutter_engine': {'before': engine, 'after': PROFILES[engine]['output']},
        'assistant_render_fix': manifest(SOURCE)}
    for key, value in expected.items():
        if info.get(key) != value:
            raise RuntimeError('Unverified Pad release metadata: ' + key)
    if not info.get('avd_defaults') or not info.get('vendor_fixes'):
        raise RuntimeError('Missing Pad defaults or hardware fixes.')
    defaults = {'settings_overlay': OVERLAY, 'screen_off_timeout': SCREEN_TIMEOUT,
        'sleep_timeout': -1, 'first_boot_stay_on_while_plugged_in': 1,
        'stay_on_while_plugged_in': 7,
        'log_script_sha256': hashlib.sha256(LOG_SCRIPT).hexdigest()}
    if any(info['avd_defaults'].get(key) != value for key, value in defaults.items()):
        raise RuntimeError('Unverified Pad awake/log defaults.')
    if info.get('boot_policy') is not None:
        from package_release import validate_boot_policy_receipt
        validate_boot_policy_receipt(info['boot_policy'])


def verify_partition(packed, raw, name):
    from lp_image import read_lp
    base, partitions = read_lp(packed)
    matches = [item for item in partitions if item['name'] == name]
    if len(matches) != 1:
        raise RuntimeError('Missing or duplicate packed partition: ' + name)
    digest, remaining = hashlib.sha256(), raw.stat().st_size
    if matches[0]['size'] != remaining:
        raise RuntimeError('Packed partition size differs: ' + name)
    with packed.open('rb') as stream:
        for length, kind, start, device in matches[0]['extents']:
            if kind != 0 or device != 0:
                raise RuntimeError('Unexpected packed extent: ' + name)
            stream.seek(base + start * 512)
            count = length * 512
            while count:
                block = stream.read(min(count, 8 * 1024**2))
                if not block:
                    raise RuntimeError('Truncated packed partition: ' + name)
                digest.update(block)
                count -= len(block)
                remaining -= len(block)
    if remaining or digest.hexdigest() != sha256(raw):
        raise RuntimeError('Packed partition differs from verified raw image: ' + name)


def verify_apk_libraries(data, libraries):
    """The signed factory APK retains original natives; overlays supply patches."""
    with zipfile.ZipFile(io.BytesIO(data)) as apk:
        for name, (before, _) in libraries.items():
            entry = 'lib/arm64-v8a/' + name
            if apk.namelist().count(entry) != 1 or hashlib.sha256(apk.read(entry)).hexdigest() != before:
                raise RuntimeError('Factory Weather native input differs: ' + name)


def verify_properties(lines, expected):
    for key, value in expected.items():
        matches = [line for line in lines if line.startswith((key + '=').encode())]
        if matches != [(key + '=' + value).encode()]:
            raise RuntimeError('Missing or ambiguous Pad release property: ' + key)


def verify_image(root, metadata):
    from build_image import erofs
    from os4_defaults import COMPONENT_XML, LOG_SCRIPT, LOG_TAGS, GRADIENT_BLUR_INIT, REFRESH_INIT, REFRESH_SCRIPT
    from os4_pad import refresh_script
    from patch_weather import PAD_APK, PAD_APK_SHA256, PAD_LIBRARIES
    from patch_assistant import profile as manifest
    from patch_composer import MANIFEST as COMPOSER
    from patch_audio import MANIFEST as AUDIO
    from patch_camera_scene import PROVIDER, LIBRARY
    import apply_pad_camera_native_fix as camera
    info = metadata['build']
    validate_build(info)
    raw, packed, vendor = root / 'work/hyperos-system.img', root / 'images/system.img', root / 'work/vendor.img'
    for name, path in (('system', raw), ('vendor', vendor), ('system_dlkm', root / 'work/base/system_dlkm.img')):
        verify_partition(packed, path, name)
    read = lambda path: erofs(raw, path)
    boot_policy_files = 0
    if info.get('boot_policy') is not None:
        from package_release import verify_boot_policy
        from os4_boot_policy import init_files
        boot_policy_files = verify_boot_policy(info, read, init_files([('', raw), ('vendor', vendor)]))
    prop = read('/system/build.prop').splitlines()
    verify_properties(prop, {**PROFILE['properties'], 'ro.build.version.sdk': '37',
                       'ro.adb.secure': '1', 'ro.debuggable': '0',
                       'ro.miui.product.home': 'com.miui.home',
                       'debug.hwui.renderer': 'skiavk',
                       **{'log.tag.' + tag: 'S' for tag in LOG_TAGS}})
    for path in ('/product/etc/device_features/emu64a.xml', '/product/etc/device_features/yingtian.xml'):
        if read(path) != MODEL_XML:
            raise RuntimeError('Original Pad model configuration differs: ' + path)
    if read('/product/etc/permissions/hyperos_avd_components.xml') != COMPONENT_XML:
        raise RuntimeError('OOBE component override differs.')
    if read('/system_ext/bin/kill_HyperOS_Log.sh') != LOG_SCRIPT:
        raise RuntimeError('Pad log script differs.')
    init = read('/system_ext/etc/init/init.hyperos_avd.rc')
    if b'start console' in init or GRADIENT_BLUR_INIT not in init or REFRESH_INIT not in init:
        raise RuntimeError('Pad boot services differ.')
    if read('/product/etc/init/lock_fps.sh') != refresh_script(REFRESH_SCRIPT):
        raise RuntimeError('Pad refresh-rate script differs.')
    defaults, assistant = info['avd_defaults'], manifest(SOURCE)
    expected = {
        '/product/overlay/HyperOSAVDSettingsDefaults/SettingsDefaults.apk': defaults['settings_overlay_sha256'],
        '/product/priv-app/MIUIFindDeviceCN/MIUIFindDeviceCN.apk': FINDDEVICE_SHA256,
        '/system/lib64/libhwui.so': info['hwui']['after'],
        '/system_ext/lib64/libhyper_os_flutter.so': info['flutter_engine']['after'],
        assistant['apk']: assistant['apk_sha256'], assistant['native']: assistant['native_sha256'],
        PAD_APK: PAD_APK_SHA256, camera.APK: camera.APK_HASH,
        camera.RUNTIME: camera.RUNTIME_HASH,
    }
    for path, checksum in expected.items():
        if hashlib.sha256(read(path)).hexdigest() != checksum:
            raise RuntimeError('Baked Pad file differs: ' + path)
    verify_apk_libraries(read(PAD_APK), PAD_LIBRARIES)
    if info['vendor_fixes']['composer'] != COMPOSER or info['vendor_fixes']['audio'] != AUDIO:
        raise RuntimeError('Pad hardware-fix metadata differs.')
    natives = {COMPOSER['native']: COMPOSER['native_sha256'], AUDIO['native']: AUDIO['native_sha256'],
               PROVIDER: info['vendor_fixes']['scene']['provider_sha256'],
               LIBRARY: info['vendor_fixes']['scene']['library_sha256'], camera.CPP: camera.CPP_HASH}
    for path, checksum in natives.items():
        if hashlib.sha256(erofs(vendor, path.removeprefix('/vendor'))).hexdigest() != checksum:
            raise RuntimeError('Baked Pad vendor file differs: ' + path)
    template = dict(line.split('=', 1) for line in (root / 'config/avd.ini').read_text().splitlines() if '=' in line)
    for key, value in {'hw.ramSize': '4096', 'hw.lcd.width': '2272', 'hw.lcd.height': '3408',
                       'hw.lcd.density': '400', 'hw.audioOutput': 'yes',
                       'disk.dataPartition.size': '32G'}.items():
        if template.get(key) != value:
            raise RuntimeError('Pad release hardware profile differs: ' + key)
    from app_compat_catalog import verify_prebuilt
    from app_compat_producers import recipes
    verify_prebuilt(root / 'tools/os4-app-compat', recipes())
    print(f'Pad preflight passed: three packed partitions, official identity/XML, defaults, {len(expected) + len(natives) + boot_policy_files} signed/native files and prebuilt bridges.', flush=True)
