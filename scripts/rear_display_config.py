"""Map the pinned hongkong rear-panel resources onto ranchu physical displays."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import xml.etree.ElementTree as ET

VERSION = '4.0.18.0.XFRCNXM'
ARCHIVE_SHA256 = 'b022c3fcfeb52ac014e971ea48dd07f8b35a0fb3fbdec74798da22375f7933e4'
PRODUCT_SHA256 = 'd6881694209ec53f9bf728ff27468c3c26627126abac1cea16a9b47c91ca08c7'
SOURCE_IDS = (4630947121878579347, 4630947121878579348)
# SurfaceFlinger IDs for the emulator's fixed EMU_display_0/1 EDIDs.
PHYSICAL_IDS = (4619827259835644672, 4619827551948147201)
OVERLAY = 'org.hyperos.avd.rear.display'
OVERLAY_PATH = 'product/overlay/HyperOSAVDRearDisplay/RearDisplay.apk'
MARKER_PATH = 'product/etc/hyperos-avd-rear-display.json'
# The original Xiaomi static RRO declares priority 1000. Keep its signed APK
# untouched and place the ID-only mapping later in the same partition.
PRIORITY = 1001
LABEL = 'u:object_r:system_file:s0'
PROPERTIES = {'persist.sys.multi_display_type': '6',
              'persist.sys.secondary_builtin_display_id': '1',
              'persist.sys.dual_screen_cover_mode_enable': 'true'}
# Static physical discovery runs before the companion app or shell is ready.
BOOT_PROPERTY = 'androidboot.qemu.external.displays=1,912,596,450,0'
SOURCE_SHA256 = {
    'product/etc/displayconfig/display_id_4630947121878579347.xml':
        'f4b1824a2b3902e60967fe6232afac237d06bc72722ea6ce2ce262b9cd0082b6',
    'product/etc/displayconfig/display_id_4630947121878579348.xml':
        'b110508baf6f457443b88d1a6eabf7c552202f11778480bad8a503986bed5671',
    'product/overlay/AospFrameworkResOverlay.apk':
        '28c71ab62155581296bbc7de383c67def701aa4f18697278b671e75a8fb0a1a5',
    'product/overlay/DevicesAndroidOverlay.apk':
        '763a4c4531799cd6f8ea8832b3b259584767596100bbaf50ce8b8e71e60c176c',
    'system/framework/framework-res.apk':
        '1a7eccd729b69ea85189dfffd6469d635173a01d11b6144ad51b74adcb4093b7',
}
RESOURCE_XML = '''<resources>
    <string-array name="config_displayUniqueIdArray" translatable="false">
        <item>local:4619827259835644672</item>
        <item>local:4619827551948147201</item>
    </string-array>
</resources>
'''.encode()
MAPPING_SHA256 = hashlib.sha256(RESOURCE_XML).hexdigest()
ARRAY_REFERENCES = {
    'config_displayCutoutPathArray':
        ('@string/config_mainBuiltInDisplayCutout', '@string/config_secondaryBuiltInDisplayCutout'),
    'config_displayCutoutApproximationRectArray':
        ('@string/config_mainBuiltInDisplayCutoutRectApproximation',
         '@string/config_secondaryBuiltInDisplayCutoutRectApproximation'),
    'config_roundedCornerRadiusArray':
        ('@dimen/rounded_corner_radius', '@dimen/secondary_rounded_corner_radius'),
    'config_roundedCornerTopRadiusArray':
        ('@dimen/rounded_corner_radius_top', '@dimen/secondary_rounded_corner_radius_top'),
    'config_roundedCornerBottomRadiusArray':
        ('@dimen/rounded_corner_radius_bottom', '@dimen/secondary_rounded_corner_radius_bottom'),
}


def validate_profile(profile):
    """Refuse every source other than the verified official hongkong 4.0.18."""
    if (not isinstance(profile, dict) or profile.get('device') != 'hongkong' or
            profile.get('hyperos') != VERSION or
            profile.get('incremental') != 'OS' + VERSION or
            profile.get('archive_sha256') != ARCHIVE_SHA256 or
            profile.get('source_partition_sha256', {}).get('product') != PRODUCT_SHA256 or
            profile.get('display') != {'width': 1120, 'height': 2436, 'density': 480}):
        raise RuntimeError('Unsupported official hongkong rear-display profile.')


def properties(data, profile):
    """Declare verified hardware capabilities without resetting user options."""
    validate_profile(profile)
    lines = data.splitlines()
    for key, value in PROPERTIES.items():
        prefix = key.encode() + b'='
        matches = [index for index, line in enumerate(lines) if line.startswith(prefix)]
        if len(matches) > 1:
            raise RuntimeError('Duplicate rear-display hardware property: ' + key)
        replacement = prefix + value.encode()
        if matches:
            lines[matches[0]] = replacement
        else:
            lines.append(replacement)
    return b'\n'.join(lines) + b'\n'


def boot_init(profile):
    """Restore hardware declarations after persistent properties are loaded."""
    validate_profile(profile)
    settings = ''.join(f'    setprop {key} {value}\n' for key, value in PROPERTIES.items())
    return ('\non post-fs-data\n' + settings +
            '\non property:ro.persistent_properties.ready=true\n' + settings).encode()


def runtime_options(build):
    """Enable the original physical rear panel only for a verified baked build."""
    marker = build.get('rear_display')
    if marker is None:
        return []
    from phone_profile import profile_from_build
    from patch_rear_display import MANIFEST
    from patch_goldfish_sync import MANIFEST as SYNC_MANIFEST
    validate_profile(profile_from_build(build))
    expected = {'schema': 1, 'hyperos': VERSION, 'archive_sha256': ARCHIVE_SHA256,
                'source_sha256': SOURCE_SHA256, 'mapping_sha256': MAPPING_SHA256,
                'overlay': OVERLAY, 'overlay_priority': PRIORITY}
    if (not isinstance(marker, dict) or any(marker.get(key) != value
                                           for key, value in expected.items()) or
            build.get('rear_display_composer_fix') != MANIFEST or
            build.get('goldfish_sync_fix') != SYNC_MANIFEST):
        raise RuntimeError('Unverified physical rear-display build metadata.')
    return ['-append-userspace-opt', BOOT_PROPERTY]


def validate_sources(sources):
    """Check all original bytes before creating an overlay or any image edit."""
    if set(sources) != set(SOURCE_SHA256):
        raise RuntimeError('Incomplete rear-display source files.')
    for path, expected in SOURCE_SHA256.items():
        if hashlib.sha256(sources[path]).hexdigest() != expected:
            raise RuntimeError('Unexpected original rear-display source: ' + path)
    for identifier, geometry in zip(SOURCE_IDS, ((1120, 2436, 480), (912, 596, 450))):
        path = f'product/etc/displayconfig/display_id_{identifier}.xml'
        root = ET.fromstring(sources[path])
        entries = root.findall('densityMapping/density')
        if (root.tag != 'displayConfiguration' or len(entries) != 1 or
                tuple(int(entries[0].findtext(key, '0')) for key in
                      ('width', 'height', 'density')) != geometry):
            raise RuntimeError('Unexpected original display density mapping: ' + path)


def _resource_block(dump, kind, name):
    pattern = (r'^\s*resource 0x[0-9a-f]+ ' + re.escape(kind + '/' + name) +
               r'\n(.*?)(?=^\s*resource |^\s*type |\Z)')
    blocks = re.findall(pattern, dump, re.MULTILINE | re.DOTALL)
    if len(blocks) != 1:
        raise RuntimeError('Missing or ambiguous source resource: ' + name)
    return blocks[0]


def _array(dump, name):
    block = _resource_block(dump, 'array', name)
    match = re.fullmatch(r'\s*\(\) \(array\) size=2\s*\[([^]]*)\]\s*', block)
    if match is None:
        raise RuntimeError('Unexpected source display array: ' + name)
    return tuple(item.strip().strip('"') for item in match[1].split(','))


def validate_resource_tables(framework, aosp, devices):
    """Retain the stock two-panel cutout and corner references and values."""
    if _array(aosp, 'config_displayUniqueIdArray') != tuple('local:' + str(i) for i in SOURCE_IDS):
        raise RuntimeError('Unexpected original display unique ID mapping.')
    for name, expected in ARRAY_REFERENCES.items():
        if _array(framework, name) != expected:
            raise RuntimeError('Unexpected original display array references: ' + name)
    for name, expected in {
            'config_mainBuiltInDisplayCutout': 'M 0,0 H -34 V 140 H 34 V 0 H 0 Z',
            'config_secondaryBuiltInDisplayCutout':
                'M -456,0 L -456,596 L -160,596 L -160,0 Z @bind_left_cutout'}.items():
        if _resource_block(devices, 'string', name).strip() != '() "' + expected + '"':
            raise RuntimeError('Unexpected original display cutout: ' + name)
    for name, value in (('rounded_corner_radius', 180), ('rounded_corner_radius_top', 180),
                        ('rounded_corner_radius_bottom', 180),
                        ('secondary_rounded_corner_radius_top', 106),
                        ('secondary_rounded_corner_radius_bottom', 106)):
        block = _resource_block(devices, 'dimen', name)
        if not re.fullmatch(r'\s*\(\) ' + str(value) + r'\.000000px\s*'
                           r'\(anydpi\) ' + str(value) + r'\.000000px\s*', block):
            raise RuntimeError('Unexpected original display corner radius: ' + name)


def _dump(aapt2, path):
    return subprocess.run([str(aapt2), 'dump', 'resources', str(path)], check=True,
                          capture_output=True, text=True).stdout


def build_overlay(sdk, folder, sources):
    """Compile one ID-array resource; sign only the new local development RRO."""
    validate_sources(sources)
    from patch_gnss import java
    sdk, folder = Path(sdk), Path(folder)
    tools = sdk / 'build-tools/37.0.0'
    aapt2 = tools / 'aapt2'
    folder.mkdir(parents=True, exist_ok=True)
    staged = {}
    for path in SOURCE_SHA256:
        if path.endswith('.apk'):
            target = folder / ('original-' + Path(path).name)
            target.write_bytes(sources[path])
            staged[Path(path).name] = target
    validate_resource_tables(_dump(aapt2, staged['framework-res.apk']),
                             _dump(aapt2, staged['AospFrameworkResOverlay.apk']),
                             _dump(aapt2, staged['DevicesAndroidOverlay.apk']))
    resources = folder / 'res/values/display_ids.xml'
    resources.parent.mkdir(parents=True, exist_ok=True)
    resources.write_bytes(RESOURCE_XML)
    manifest = folder / 'AndroidManifest.xml'
    manifest.write_text(f'''<manifest xmlns:android="http://schemas.android.com/apk/res/android"
    package="{OVERLAY}" android:versionCode="1" android:versionName="1">
    <uses-sdk android:minSdkVersion="21" android:targetSdkVersion="28" />
    <overlay android:targetPackage="android" android:isStatic="true" android:priority="{PRIORITY}" />
    <application android:hasCode="false" />
</manifest>
''')
    compiled, unsigned, signed = (folder / name for name in
                                 ('resources.zip', 'unsigned.apk', 'RearDisplay.apk'))
    subprocess.run([str(aapt2), 'compile', str(resources), '-o', str(compiled)],
                   check=True, capture_output=True)
    subprocess.run([str(aapt2), 'link', '-o', str(unsigned),
                    '-I', str(sdk / 'platforms/android-36/android.jar'),
                    '--manifest', str(manifest), str(compiled)], check=True, capture_output=True)
    dump = _dump(aapt2, unsigned)
    if (re.findall(r'^\s*resource 0x[0-9a-f]+ (\S+)', dump, re.MULTILINE) !=
            ['array/config_displayUniqueIdArray'] or
            _array(dump, 'config_displayUniqueIdArray') != tuple('local:' + str(i) for i in PHYSICAL_IDS)):
        raise RuntimeError('Compiled rear-display RRO changes unexpected resources.')
    keystore = folder / 'overlay.jks'
    java_path = Path(java())
    if not keystore.exists():
        subprocess.run([str(java_path.parent / 'keytool'), '-genkeypair', '-keystore', str(keystore),
                        '-storepass', 'android', '-keypass', 'android', '-alias', 'overlay',
                        '-dname', 'CN=HyperOS AVD Rear Display', '-keyalg', 'RSA', '-keysize', '2048',
                        '-validity', '10000', '-noprompt'], check=True, capture_output=True)
    environment = dict(os.environ, JAVA_HOME=str(java_path.parent.parent))
    subprocess.run([str(tools / 'apksigner'), 'sign', '--ks', str(keystore),
                    '--ks-key-alias', 'overlay', '--ks-pass', 'pass:android',
                    '--key-pass', 'pass:android', '--out', str(signed), str(unsigned)],
                   check=True, capture_output=True, env=environment)
    subprocess.run([str(tools / 'apksigner'), 'verify', str(signed)],
                   check=True, capture_output=True, env=environment)
    return signed.read_bytes()


def image_replacements(profile, partitions, sdk, folder):
    """Add display-ID aliases and one RRO without editing the signed sources."""
    validate_profile(profile)
    from build_image import erofs
    partitions = Path(partitions)
    sources = {}
    for path in SOURCE_SHA256:
        part, relative = path.split('/', 1)
        source_path = '/system/' + relative if part == 'system' else '/' + relative
        sources[path] = erofs(partitions / (part + '.img'), source_path)
    validate_sources(sources)
    overlay = build_overlay(sdk, folder, sources)
    displays = []
    edits = {OVERLAY_PATH: (overlay, 0o644, LABEL)}
    for original, physical, geometry in zip(SOURCE_IDS, PHYSICAL_IDS,
                                            ((1120, 2436, 480), (912, 596, 450))):
        source = f'product/etc/displayconfig/display_id_{original}.xml'
        destination = f'product/etc/displayconfig/display_id_{physical}.xml'
        edits[destination] = (sources[source], 0o644, LABEL)
        displays.append({'source_physical_id': original, 'physical_id': physical,
                         'unique_id': 'local:' + str(physical),
                         'config': '/' + destination, 'config_sha256': SOURCE_SHA256[source],
                         'width': geometry[0], 'height': geometry[1], 'density': geometry[2]})
    marker = {'schema': 1, 'hyperos': VERSION, 'archive_sha256': ARCHIVE_SHA256,
              'source_sha256': dict(SOURCE_SHA256), 'mapping_sha256': MAPPING_SHA256,
              'overlay': OVERLAY, 'overlay_path': '/' + OVERLAY_PATH,
              'overlay_sha256': hashlib.sha256(overlay).hexdigest(),
              'overlay_priority': PRIORITY, 'source_overlay_priority': 1000,
              'requires_runtime_resource_verification': True,
              'overridden_resources': ['config_displayUniqueIdArray'],
              'displays': displays, 'rear_safe_inset_left': 296, 'rear_corner_radius': 106}
    edits[MARKER_PATH] = ((json.dumps(marker, indent=2) + '\n').encode(), 0o644, LABEL)
    return edits, marker
