#!/usr/bin/env python3
"""Package verified firmware into GitHub-sized parts, excluding all personal data."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile

import common
from common import REPO_ROOT, ROOT, OS4_SOURCE, fetch_ksu, sha256, tool
from init_userdata import create as create_userdata


class PartsWriter:
    def __init__(self, directory, stem, limit):
        self.directory, self.stem, self.limit = directory, stem, limit
        self.parts, self.stream, self.used = [], None, 0
        self.digest = None

    def write(self, data):
        length = len(data)
        while data:
            if self.stream is None:
                name = f'{self.stem}.tar.gz.part{len(self.parts) + 1:03d}'
                self.stream = (self.directory / name).open('xb')
                self.digest = hashlib.sha256()
                self.parts.append({'name': name})
                self.used = 0
            chunk, data = data[:self.limit - self.used], data[self.limit - self.used:]
            self.stream.write(chunk)
            self.digest.update(chunk)
            self.used += len(chunk)
            if self.used == self.limit:
                self.close_part()
        return length

    def close_part(self):
        if self.stream:
            self.stream.close()
            self.parts[-1].update(size=self.used, sha256=self.digest.hexdigest())
            self.stream = None

    def flush(self):
        if self.stream:
            self.stream.flush()


IMAGE_FILES = ('NOTICE.txt', 'VerifiedBootParams.textproto', 'advancedFeatures.ini',
               'build.prop', 'encryptionkey.img', 'kernel-ranchu', 'kernel_cmdline.txt',
               'ramdisk.img', 'source.properties', 'system.img', 'vendor.img')
KSU_FILES = ('ksud-aarch64-linux-android', 'KernelSU_v3.3.0_32601-release.apk')
BUILD_KEYS = ('hyperos', 'source', 'android_api', 'archive_sha256', 'kernel_page_size',
              'hardware_base_api', 'kernel_kmi', 'surfaceflinger_backend', 'hwui_renderer', 'gnss_patch',
              'mi_ext_overlays', 'mi_ext_software_identity', 'flutter_render_fix',
              'preinstalled_apps', 'native_quickstep_identity', 'factory_launcher_deadlines', 'avd_defaults',
              'assistant_render_fix', 'composer_alpha_fix', 'audio_pcm_fix', 'camera_scene_fix',
              'adb_authentication', 'experimental', 'device', 'display', 'model_xml_sha256',
              'identity_source_sha256', 'vendor_fixes', 'hwui',
              'flutter_engine', 'finddevice_provider_disabled')
RUNTIME_PAYLOADS = {
    'os4-official': {'xiaomi-camera': ('provider', 'hwl.so', 'yuv.so', 'manifest.json', 'receipt.json')},
    'os4-pad': {'pad-camera-native': ('provider', 'hwl.so', 'yuv.so', 'manifest.json', 'receipt.json'),
                'weather-angle': ('libEGL_angle.so', 'libGLESv2_angle.so', 'libhgl.so', 'receipt.json')},
}


def verify_lockscreen_video(profile, build, read):
    """Require the exact r3 receipt, signed APK and patched native producer."""
    if profile['hyperos'] != '4.0.18.0.XFRCNXM':
        return 0
    from patch_lockscreen_video import MANIFEST
    if (build.get('lockscreen_video_fix') != MANIFEST
            or profile.get('pins', {}).get('aod_apk') != MANIFEST['apk_sha256']):
        raise RuntimeError('Missing verified phone lockscreen video metadata or source pin.')
    for path, checksum in ((MANIFEST['apk'], MANIFEST['apk_sha256']),
                           (MANIFEST['native'], MANIFEST['native_sha256'])):
        if hashlib.sha256(read(path)).hexdigest() != checksum:
            raise RuntimeError('Baked lockscreen video checksum mismatch: ' + path)
    return 2


def verify_rear_display(profile, build, read, read_vendor, template):
    """Verify the r3 physical rear panel from the packed image's actual bytes."""
    from rear_display_config import VERSION
    if profile.get('hyperos') != VERSION:
        return 0
    import xml.etree.ElementTree as ET
    import rear_display_config as rear
    from patch_composer import MANIFEST as ALPHA_FIX
    from patch_rear_display import MANIFEST as REAR_FIX
    from patch_goldfish_sync import MANIFEST as SYNC_FIX
    from apply_rear_display_fix import EXPECTED_WAKE_MANIFEST as WAKE_FIX
    import rear_display_wake as wake
    rear.validate_profile(profile)
    alpha = {**ALPHA_FIX, 'native_sha256': REAR_FIX['native_sha256']}
    if (build.get('rear_display_composer_fix') != REAR_FIX
            or build.get('composer_alpha_fix') != alpha
            or build.get('goldfish_sync_fix') != SYNC_FIX):
        raise RuntimeError('Missing verified rear-display composer or goldfish sync metadata.')
    marker = build.get('rear_display')
    if (not isinstance(marker, dict) or not isinstance(marker.get('overlay_sha256'), str)
            or not re.fullmatch('[0-9a-f]{64}', marker['overlay_sha256'])):
        raise RuntimeError('Missing verified rear-display resource metadata.')
    displays = []
    for original, physical, geometry in zip(rear.SOURCE_IDS, rear.PHYSICAL_IDS,
                                            ((1120, 2436, 480), (912, 596, 450))):
        source = f'product/etc/displayconfig/display_id_{original}.xml'
        displays.append({'source_physical_id': original, 'physical_id': physical,
                         'unique_id': 'local:' + str(physical),
                         'config': f'/product/etc/displayconfig/display_id_{physical}.xml',
                         'config_sha256': rear.SOURCE_SHA256[source],
                         'width': geometry[0], 'height': geometry[1], 'density': geometry[2]})
    expected = {'schema': 1, 'hyperos': rear.VERSION, 'archive_sha256': rear.ARCHIVE_SHA256,
                'source_sha256': dict(rear.SOURCE_SHA256), 'mapping_sha256': rear.MAPPING_SHA256,
                'overlay': rear.OVERLAY, 'overlay_path': '/' + rear.OVERLAY_PATH,
                'overlay_sha256': marker['overlay_sha256'], 'overlay_priority': rear.PRIORITY,
                'source_overlay_priority': 1000, 'requires_runtime_resource_verification': True,
                'overridden_resources': ['config_displayUniqueIdArray'], 'displays': displays,
                'rear_safe_inset_left': 296, 'rear_corner_radius': 106}
    if marker != expected or json.dumps(marker, sort_keys=True) != json.dumps(expected, sort_keys=True):
        raise RuntimeError('Unexpected rear-display resource metadata.')
    wake_marker = build.get('rear_display_wake_fix')
    if (wake_marker != WAKE_FIX
            or json.dumps(wake_marker, sort_keys=True) != json.dumps(WAKE_FIX, sort_keys=True)):
        raise RuntimeError('Missing or unexpected verified rear-display wake metadata.')
    if (WAKE_FIX['source_sha256'] != hashlib.sha256(wake.JAVA_SOURCE.encode()).hexdigest()
            or WAKE_FIX['script_sha256'] != hashlib.sha256(wake.LAUNCHER).hexdigest()):
        raise RuntimeError('Rear-display wake source or launcher differs from the reviewed metadata.')
    if (rear.runtime_options(build) != ['-append-userspace-opt', rear.BOOT_PROPERTY]
            or template.get('hw.multi_display_window') != 'yes'):
        raise RuntimeError('Missing verified physical rear-display boot/window configuration.')
    for receipt in (REAR_FIX, SYNC_FIX):
        if hashlib.sha256(read_vendor(receipt['native'])).hexdigest() != receipt['native_sha256']:
            raise RuntimeError('Baked rear-display vendor checksum mismatch: ' + receipt['native'])
    for path, checksum in ((WAKE_FIX['jar_path'], WAKE_FIX['jar_sha256']),
                           (WAKE_FIX['script_path'], WAKE_FIX['script_sha256'])):
        data = read(path)
        if hashlib.sha256(data).hexdigest() != checksum:
            raise RuntimeError('Baked rear-display wake checksum mismatch: ' + path)
        if path == WAKE_FIX['script_path'] and data != wake.LAUNCHER:
            raise RuntimeError('Baked rear-display wake launcher differs from the reviewed source.')
    try:
        baked = json.loads(read('/' + rear.MARKER_PATH))
    except (ValueError, UnicodeError) as error:
        raise RuntimeError('Invalid baked rear-display resource metadata.') from error
    if baked != marker or json.dumps(baked, sort_keys=True) != json.dumps(marker, sort_keys=True):
        raise RuntimeError('Baked rear-display resource metadata differs from the release.')
    if hashlib.sha256(read('/' + rear.OVERLAY_PATH)).hexdigest() != marker['overlay_sha256']:
        raise RuntimeError('Baked rear-display overlay checksum mismatch.')
    sources = {path: read('/' + path) for path in rear.SOURCE_SHA256}
    rear.validate_sources(sources)
    for display in displays:
        data = read(display['config'])
        if hashlib.sha256(data).hexdigest() != display['config_sha256']:
            raise RuntimeError('Baked rear-display XML alias checksum mismatch: ' + display['config'])
        try:
            xml = ET.fromstring(data)
            density = xml.findall('densityMapping/density')
            geometry = (tuple(int(density[0].findtext(key, '0')) for key in
                              ('width', 'height', 'density')) if len(density) == 1 else None)
        except (ET.ParseError, ValueError) as error:
            raise RuntimeError('Invalid baked rear-display XML geometry: ' + display['config']) from error
        if (xml.tag != 'displayConfiguration' or
                geometry != tuple(display[key] for key in ('width', 'height', 'density'))):
            raise RuntimeError('Unexpected baked rear-display XML geometry: ' + display['config'])
    properties = read('/system/build.prop').splitlines()
    for key, value in rear.PROPERTIES.items():
        prefix = key.encode() + b'='
        if [line for line in properties if line.startswith(prefix)] != [prefix + value.encode()]:
            raise RuntimeError('Missing or ambiguous rear-display hardware property: ' + key)
    init = read('/system_ext/etc/init/init.hyperos_avd.rc')
    if init.count(rear.boot_init(profile)) != 1:
        raise RuntimeError('Missing or ambiguous rear-display persistent hardware initialization.')
    for key, value in rear.PROPERTIES.items():
        commands = [words for line in init.decode().splitlines()
                    if (words := line.strip().split())[:2] == ['setprop', key]]
        if commands != [['setprop', key, value], ['setprop', key, value]]:
            raise RuntimeError('Unexpected rear-display init property override: ' + key)
    return len(sources) + len(displays) + 5


def verify_os4_image(root, metadata):
    """Verify the release's actual packed image and baked compatibility files."""
    from lp_image import read_lp
    from os4_defaults import COMPONENT_XML, PROVIDER, AOD_INIT, LOG_SCRIPT, LOG_TAGS, phone_scripts
    from phone_profile import profile_from_build
    from patch_flutter import PROFILES
    from patch_weather import ANGLE
    from patch_assistant import profile as assistant_profile
    from patch_composer import MANIFEST as COMPOSER_FIX
    from patch_audio import MANIFEST as AUDIO_FIX
    selected = profile_from_build(metadata['build'])
    assistant_fix = assistant_profile(OS4_SOURCE, selected)
    model_xml = Path(selected['model_xml']).read_bytes()
    display = selected['display']
    aod_script, refresh_script = phone_scripts(selected)
    raw, packed = root / 'work/hyperos-system.img', root / 'images/system.img'
    base, partitions = read_lp(packed)
    system = next(item for item in partitions if item['name'] == 'system')
    digest = hashlib.sha256()
    with packed.open('rb') as source:
        remaining = raw.stat().st_size
        for length, kind, start, device in system['extents']:
            if kind != 0 or device != 0:
                raise RuntimeError('Unexpected official system extent.')
            source.seek(base + start * 512)
            count = min(remaining, length * 512)
            while count:
                block = source.read(min(count, 8 * 1024**2))
                if not block:
                    raise RuntimeError('Truncated packed system image.')
                digest.update(block)
                count -= len(block)
                remaining -= len(block)
        if remaining or digest.hexdigest() != sha256(raw):
            raise RuntimeError('Packed system differs from the verified raw image.')
    vendor = root / 'work/vendor.img'
    partition = next(item for item in partitions if item['name'] == 'vendor')
    digest = hashlib.sha256()
    with packed.open('rb') as source:
        remaining = vendor.stat().st_size
        for length, kind, start, device in partition['extents']:
            if kind != 0 or device != 0:
                raise RuntimeError('Unexpected official vendor extent.')
            source.seek(base + start * 512)
            count = min(remaining, length * 512)
            while count:
                block = source.read(min(count, 8 * 1024**2))
                if not block:
                    raise RuntimeError('Truncated packed vendor image.')
                digest.update(block)
                count -= len(block)
                remaining -= len(block)
        if remaining or digest.hexdigest() != sha256(vendor):
            raise RuntimeError('Packed vendor differs from the verified raw image.')
    dump = tool('dump.erofs', 'erofs-utils')
    def read(path):
        return subprocess.check_output([dump, '--cat', '--path=' + path, str(raw)])
    def read_vendor(path):
        return subprocess.check_output([dump, '--cat', '--path=' + path.removeprefix('/vendor'), str(vendor)])
    template = dict(line.split('=', 1) for line in (root / 'config/avd.ini').read_text().splitlines() if '=' in line)
    rear_files = verify_rear_display(selected, metadata['build'], read, read_vendor, template)
    if selected['hyperos'] != '4.0.18.0.XFRCNXM':
        if metadata['build'].get('composer_alpha_fix') != COMPOSER_FIX:
            raise RuntimeError('Missing verified ranchu composer alpha fix metadata.')
        if hashlib.sha256(read_vendor(COMPOSER_FIX['native'])).hexdigest() != COMPOSER_FIX['native_sha256']:
            raise RuntimeError('Baked ranchu composer checksum mismatch.')
    if metadata['build'].get('audio_pcm_fix') != AUDIO_FIX:
        raise RuntimeError('Missing verified playback PCM fix metadata.')
    audio = subprocess.check_output([dump, '--cat', '--path=' +
        AUDIO_FIX['native'].removeprefix('/vendor'), str(vendor)])
    if hashlib.sha256(audio).hexdigest() != AUDIO_FIX['native_sha256']:
        raise RuntimeError('Baked playback PCM checksum mismatch.')
    scene = metadata['build'].get('camera_scene_fix', {})
    from patch_camera_scene import PROVIDER as CAMERA_PROVIDER, LIBRARY as CAMERA_LIBRARY
    for path, key in ((CAMERA_PROVIDER, 'provider_sha256'), (CAMERA_LIBRARY, 'library_sha256')):
        content = subprocess.check_output([dump, '--cat', '--path=' + path.removeprefix('/vendor'), str(vendor)])
        if hashlib.sha256(content).hexdigest() != scene.get(key):
            raise RuntimeError('Baked virtual camera checksum mismatch.')
    lockscreen_files = verify_lockscreen_video(selected, metadata['build'], read)
    prop = read('/system/build.prop')
    for line in (b'ro.build.version.sdk=37', b'ro.adb.secure=1', b'ro.debuggable=0',
                 b'ro.miui.product.home=com.miui.home'):
        if prop.splitlines().count(line) != 1:
            raise RuntimeError('Missing or ambiguous release property: ' + line.decode())
    for key, value in selected['properties'].items():
        if prop.splitlines().count((key + '=' + value).encode()) != 1:
            raise RuntimeError('Missing source-pinned phone identity: ' + key)
    product_props = read('/product/etc/build.prop')
    if product_props.splitlines().count(('ro.mi.os.version.incremental=' + selected['incremental']).encode()) != 1:
        raise RuntimeError('Baked phone HyperOS version differs from its source profile.')
    if read('/system/etc/sysconfig/hyperos-avd-components.xml') != COMPONENT_XML:
        raise RuntimeError('OOBE component override differs from the verified default.')
    defaults = metadata['build']['avd_defaults']
    if defaults.get('finddevice_disabled_component') != PROVIDER:
        raise RuntimeError('Unexpected OOBE component metadata.')
    if json.loads(read('/product/etc/hyperos-avd-defaults.json')) != defaults:
        raise RuntimeError('Baked awake defaults do not match release metadata.')
    if (read('/product/etc/init/lock_fps.sh') != refresh_script
            or defaults.get('refresh', {}).get('script_sha256') != hashlib.sha256(refresh_script).hexdigest()):
        raise RuntimeError('Baked refresh service differs from its firmware profile.')
    logging = defaults.get('log_filter', {})
    if (read('/system_ext/bin/kill_HyperOS_Log.sh') != LOG_SCRIPT
            or logging.get('script_sha256') != hashlib.sha256(LOG_SCRIPT).hexdigest()
            or logging.get('tags') != list(LOG_TAGS) or logging.get('level') != 'S'):
        raise RuntimeError('Missing verified HyperOS log filter.')
    for tag in LOG_TAGS:
        if prop.splitlines().count(('log.tag.' + tag + '=S').encode()) != 1:
            raise RuntimeError('Missing or duplicate log filter property: ' + tag)
    if read('/product/etc/device_features/emu64a.xml') != model_xml:
        raise RuntimeError('Missing verified emulator AOD features.')
    if read('/product/etc/device_features/hongkong.xml') != model_xml:
        raise RuntimeError('Emulator and original phone model configurations must match.')
    if b'start console' in read('/system_ext/etc/init/init.hyperos_avd.rc'):
        raise RuntimeError('Debug console must be disabled in the OS4 release.')
    if (AOD_INIT not in read('/system_ext/etc/init/init.hyperos_avd.rc')
            or read('/system_ext/bin/hyperos-avd-aod-defaults.sh') != aod_script):
        raise RuntimeError('Missing first-boot AOD service.')
    if defaults.get('display') != display or not defaults.get('aod', {}).get('support_aod_fullscreen'):
        raise RuntimeError('Missing official display / AOD defaults.')
    if template.get('hw.audioOutput') != 'yes':
        raise RuntimeError('Release AVD audio output must be enabled.')
    for key, value in display.items():
        if template.get('hw.lcd.' + key) != str(value):
            raise RuntimeError('Release AVD differs from the official primary display: ' + key)
    expected = {
        assistant_fix['apk']: assistant_fix['apk_sha256'],
        assistant_fix['native']: assistant_fix['native_sha256'],
        '/product/overlay/HyperOSAVDSettingsDefaults/SettingsDefaults.apk': defaults['settings_overlay_sha256'],
        '/product/priv-app/MIUIFindDeviceCN/MIUIFindDeviceCN.apk': defaults['finddevice_apk_sha256'],
        '/system_ext/lib64/libhyper_os_flutter.so': PROFILES[selected['pins']['flutter']]['output'],
        **{'/system/lib64/' + name: checksum for name, checksum in ANGLE.items()},
    }
    if selected['hyperos'] == '4.0.18.0.XFRCNXM':
        from apply_navigation_fix import AOT_BEFORE, AOT_AFTER, WATCHDOG_BEFORE, WATCHDOG_AFTER
        from patch_pad_hwui import NATIVE, PHONE_BEFORE, PHONE_AFTER, PHONE_PROFILE
        hwui = {'before': PHONE_BEFORE, 'after': PHONE_AFTER, 'profile': PHONE_PROFILE}
        if (metadata['build'].get('hwui') != hwui
                or metadata['build'].get('hwui_renderer') != 'skiavk'
                or prop.splitlines().count(b'debug.hwui.renderer=skiavk') != 1
                or b'    setprop debug.hwui.renderer skiavk\n' not in
                    read('/system_ext/etc/init/init.hyperos_avd.rc')):
            raise RuntimeError('Missing verified phone HWUI Vulkan preload configuration.')
        vendor_props = subprocess.check_output([dump, '--cat', '--path=/build.prop', str(vendor)])
        if vendor_props.splitlines().count(b'ro.zygote.disable_gl_preload=0') != 1:
            raise RuntimeError('Phone Vulkan requires the OEM CPU shader preload gate.')
        expected[NATIVE] = PHONE_AFTER
        launcher = {'revision': 1, 'apk_sha256': selected['pins']['home_apk'], 'libraries': {
            'libapp.so': {'before': AOT_BEFORE, 'after': AOT_AFTER},
            'libapp_launcher.so': {'before': WATCHDOG_BEFORE, 'after': WATCHDOG_AFTER}}}
        if metadata['build'].get('factory_launcher_deadlines') != launcher:
            raise RuntimeError('Missing verified factory launcher deadline metadata.')
        expected['/product/priv-app/MiuiHome/MiuiHome.apk'] = launcher['apk_sha256']
        expected.update({'/product/priv-app/MiuiHome/lib/arm64/' + name: item['after']
                         for name, item in launcher['libraries'].items()})
    apps = metadata['build']['preinstalled_apps']
    if metadata['build'].get('assistant_render_fix') != assistant_fix:
        raise RuntimeError('Missing verified XiaoAI MGL fix metadata.')
    if json.loads(read('/product/etc/hyperos-avd-preinstalled-apps.json')) != apps:
        raise RuntimeError('Baked app metadata does not match the release.')
    for app in apps['apps']:
        expected[app['apk']] = app['sha256']
        expected.update({str(Path(app['apk']).parent / 'lib/arm64' / name): checksum
                         for name, checksum in app['native_libraries'].items()})
    for path, checksum in expected.items():
        if hashlib.sha256(read(path)).hexdigest() != checksum:
            raise RuntimeError('Baked release checksum mismatch: ' + path)
    print(f'OS4 preflight passed: packed system/vendor, properties, defaults and {len(expected) + lockscreen_files + rear_files} signed/native/resource files.', flush=True)


def release_metadata(root, variant):
    metadata = {'project': 'HyperOS-AVD', 'format': 1, 'platform': 'macos-arm64',
                'hyperos': '3.0.2.0.WMCCNXM', 'android_api': 36,
                'kernelsu': '3.3.0 (32601)', 'gnss_patch': 'status-satellite-first-fix-async'}
    if variant == 'os4-official':
        build = json.loads((root / 'local/build.json').read_text())
        from phone_profile import profile_from_build
        selected = profile_from_build(build)
        if (build.get('source') != OS4_SOURCE or build.get('android_api') != 37
                or build.get('hyperos') != selected['hyperos']
                or build.get('adb_authentication') is not True
                or build.get('flutter_render_fix') != 6
                or not build.get('native_quickstep_identity')
                or not build.get('preinstalled_apps') or not build.get('avd_defaults')):
            raise RuntimeError('OS4 release requires the verified official image, native fixes, defaults and secure ADB.')
        metadata.update(format=3, variant=variant, hyperos=build['hyperos'], android_api=37,
                        source=OS4_SOURCE, source_device='hongkong', hardware_base_api=36,
                        build={key: build[key] for key in BUILD_KEYS if key in build})
    elif variant == 'os4-pad':
        from release_pad import validate_build
        build = json.loads((root / 'local/build.json').read_text())
        validate_build(build)
        metadata.update(format=3, variant=variant, hyperos=build['hyperos'], android_api=37,
                        source=build['source'], source_device='yingtian', hardware_base_api=36,
                        build={key: build[key] for key in BUILD_KEYS if key in build},
                        compatibility={'minimum_installer': '1.1.0',
                            'userdata_family': 'os4-yingtian-api37-ranchu-4k',
                            'upgrade_from': [], 'runtime_in_bundle': True})
    elif variant != 'os3':
        raise RuntimeError('Unknown release variant.')
    elif (root / 'local/build.json').exists() and json.loads((root / 'local/build.json').read_text()).get('source') in (OS4_SOURCE, 'official-yingtian-ota'):
        raise RuntimeError('Refusing to label official OS4 firmware as OS3.')
    if variant == 'os4-official':
        metadata['compatibility'] = {'minimum_installer': '1.0.0',
            'userdata_family': 'os4-hongkong-api37-ranchu-4k',
            'upgrade_from': ['v0.2.0-a17-hyperos4-hongkong-r1'],
            'runtime_in_bundle': True}
        if selected['hyperos'] == '4.0.18.0.XFRCNXM':
            from manage import MODULE_UPGRADE_PREFLIGHT
            if 'lockscreen_video_fix' in build:
                metadata['build']['lockscreen_video_fix'] = build['lockscreen_video_fix']
            for key in ('rear_display', 'rear_display_composer_fix', 'goldfish_sync_fix',
                        'rear_display_wake_fix'):
                if key in build:
                    metadata['build'][key] = build[key]
            metadata['compatibility'].update(minimum_installer='1.2.0',
                upgrade_from=['v0.2.1-a17-hyperos4-hongkong-r2'],
                module_upgrade_preflight=MODULE_UPGRADE_PREFLIGHT)
    return metadata


def runtime_files():
    """Explicit portable source set; exclude logs, secrets, user state and binaries."""
    paths = {'Install.command': REPO_ROOT / 'Install.command', 'install.sh': REPO_ROOT / 'install.sh'}
    for directory, patterns in {'scripts': ('*.py',), 'config': ('*.ini', '*.xml', '*.json', '*.sh', '*.rc'),
                                'native': ('*.S', '*.c', '*.cpp', '*.m')}.items():
        for pattern in patterns:
            for path in sorted((REPO_ROOT / directory).glob(pattern)):
                paths[path.relative_to(REPO_ROOT).as_posix()] = path
    return paths


def write_bundle(directory, version, metadata, paths, part_mib=1536):
    """Archive only explicit portable files; publish the manifest last."""
    if not re.fullmatch(r'(?:pad-)?v[0-9A-Za-z_.-]+', version):
        raise RuntimeError('Invalid release version.')
    if not 1 <= part_mib < 2048:
        raise RuntimeError('Every GitHub release asset must be smaller than 2 GiB.')
    files = {}
    for relative, path in paths.items():
        if path.is_symlink() or not path.is_file():
            raise RuntimeError('Release files must not be symlinks.')
        allowed = (relative in ('images/' + name for name in (*IMAGE_FILES, 'userdata.img'))
                   or relative.startswith(('images/data/misc/modem_simulator/', 'images/data/misc/emulator/'))
                   or relative in ('tools/' + name for name in KSU_FILES)
                   or metadata['format'] in (2, 3) and relative == 'config/avd.ini'
                   or metadata['format'] == 3 and relative.startswith('runtime/')
                   or relative in ('tools/emulator_srgb.dylib', 'tools/emulator_srgb.build.json')
                   or metadata['format'] == 3 and relative in (
                       'tools/' + folder + '/' + name
                       for folder, names in RUNTIME_PAYLOADS.get(metadata.get('variant'), {}).items()
                       for name in names))
        if not allowed or '..' in Path(relative).parts or Path(relative).is_absolute():
            raise RuntimeError('Unexpected release path: ' + relative)
        files[relative] = {'size': path.stat().st_size, 'sha256': sha256(path)}
    directory.mkdir(parents=True, exist_ok=False)
    writer = PartsWriter(directory, 'HyperOS-AVD-' + version + '-macos-arm64', part_mib * 1024**2)
    try:
        with tarfile.open(fileobj=writer, mode='w|gz', compresslevel=6) as archive:
            for relative, path in sorted(paths.items()):
                print('Packaging: ' + relative, flush=True)
                entry = archive.gettarinfo(str(path), arcname=relative)
                entry.uid = entry.gid = 0
                entry.uname = entry.gname = ''
                entry.mtime = 0
                entry.mode = 0o644
                with path.open('rb') as source:
                    archive.addfile(entry, source)
                # Do not publish an image that changed during packaging.
                if sha256(path) != files[relative]['sha256']:
                    raise RuntimeError('Firmware changed while packaging: ' + relative)
    finally:
        writer.close_part()
    manifest = {**metadata, 'version': version, 'parts': writer.parts, 'files': files}
    if manifest['format'] in (2, 3):
        manifest['build']['system_sha256'] = files['images/system.img']['sha256']
    (directory / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    (directory / 'SHA256SUMS').write_text(''.join(
        f'{entry["sha256"]}  {entry["name"]}\n' for entry in writer.parts)
        + sha256(directory / 'manifest.json') + '  manifest.json\n')
    print(json.dumps({'directory': str(directory), 'parts': writer.parts}, indent=2))
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version')
    parser.add_argument('--output', type=Path, help='Stage a new bundle before replacing an unpublished release')
    parser.add_argument('--variant', choices=('os3', 'os4-official', 'os4-pad'), default='os3')
    parser.add_argument('--part-mib', type=int, default=1536)
    args = parser.parse_args()
    root = ROOT
    if args.variant != 'os3' and 'HYPEROS_AVD_WORKSPACE' not in os.environ:
        root = REPO_ROOT / 'work' / args.variant
    common.ROOT = root
    metadata = release_metadata(root, args.variant)
    if args.variant == 'os4-official':
        verify_os4_image(root, metadata)
    elif args.variant == 'os4-pad':
        from release_pad import verify_image
        verify_image(root, metadata)
    phone_version = ('v0.2.2-a17-hyperos4-hongkong-r3'
                     if metadata['hyperos'] == '4.0.18.0.XFRCNXM'
                     else 'v0.2.1-a17-hyperos4-hongkong-r2')
    version = args.version or {'os3': 'v0.1.0',
        'os4-official': phone_version,
        'os4-pad': 'pad-v0.1.0-a17-hyperos4-yingtian-r1'}[args.variant]
    fetch_ksu(KSU_FILES)
    paths = {'images/' + name: root / 'images' / name for name in IMAGE_FILES}
    for relative in ('data/misc/modem_simulator', 'data/misc/emulator'):
        paths.update({path.relative_to(root).as_posix(): path for path in (root / 'images' / relative).rglob('*')
                      if path.is_file() and path.name != '.DS_Store' and not path.name.startswith('._')})
    paths.update({'tools/' + name: root / 'tools' / name for name in KSU_FILES})
    if args.variant != 'os3':
        paths['config/avd.ini'] = root / 'config/avd.ini'
        from host_color import build as build_host_color
        for path in build_host_color(root):
            paths['tools/' + path.name] = path
    if metadata['format'] == 3:
        for folder, names in RUNTIME_PAYLOADS[args.variant].items():
            paths.update({'tools/' + folder + '/' + name: root / 'tools' / folder / name for name in names})
        paths.update({'runtime/' + relative: path for relative, path in runtime_files().items()})
    # Always create a new blank template; never trust or read personal avd/ data.
    temporary = root / 'work/release-templates' / version / 'userdata.img'
    if temporary.exists():
        raise RuntimeError('A release template already exists; choose a new revision.')
    paths['images/userdata.img'] = create_userdata(temporary)
    directory = args.output or REPO_ROOT / 'releases' / version
    write_bundle(directory, version, metadata, paths, args.part_mib)


if __name__ == '__main__':
    main()
