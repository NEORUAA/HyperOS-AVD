"""Resolve source-pinned hongkong firmware profiles without changing HAL identity."""
import hashlib
import json
from pathlib import Path

CONFIG = Path(__file__).resolve().parent.parent / 'config'
ARCHIVES = {
    '4.0.17.0.XFRCNXM': 'c45f3fcaccb74d0d8212f68a721814abf229a537f8a8ac6d227be20b348752f2',
    '4.0.18.0.XFRCNXM': 'b022c3fcfeb52ac014e971ea48dd07f8b35a0fb3fbdec74798da22375f7933e4',
}
PROP_PATHS = {
    'system': '/system/build.prop', 'system_ext': '/etc/build.prop',
    'product': '/etc/build.prop', 'mi_ext': '/etc/build.prop',
    'vendor': '/build.prop', 'odm': '/etc/build.prop',
    'system_dlkm': '/etc/build.prop', 'vendor_dlkm': '/etc/build.prop',
}
PIN_PATHS = {
    'services': ('system', '/system/framework/services.jar'),
    'hwui': ('system', '/system/lib64/libhwui.so'),
    'android_runtime': ('system', '/system/lib64/libandroid_runtime.so'),
    'flutter': ('system_ext', '/lib64/libhyper_os_flutter.so'),
    'finddevice_apk': ('product', '/priv-app/MIUIFindDeviceCN/MIUIFindDeviceCN.apk'),
    'assistant_apk': ('product', '/priv-app/VoiceAssistAndroidT/VoiceAssistAndroidT.apk'),
    'camera_apk': ('product', '/priv-app/MiuiCamera/MiuiCamera.apk'),
    'home_apk': ('product', '/priv-app/MiuiHome/MiuiHome.apk'),
    'aod_apk': ('product', '/priv-app/MIUIAod/MIUIAod.apk'),
}
LEGACY_PINS = {
    'services': 'e60bfa83c50060666c9c157c558f4a88449b3697c45a2895a1069b9d59e32df0',
    'hwui': '6dfbac6a533ce08b6ace8e7cd7df3c4067350a1f1410729f8c0acd4bc8a78bd4',
    'android_runtime': '22b624d7bfa130cb66ad841b0af152d975c5329449dd804e077d106c57a89c3a',
    'flutter': '71caea24a7fec06ae7c1b7cdb93c99f45288154a9ca21bb634d8181a97dcef62',
    'finddevice_apk': 'e57888e1721680fece2961ec0cf23c646693be2e58d0ae6b772c285ce317cfd1',
    'assistant_apk': '70b833c96947b17fc58a3cb7d84c06577b97b58798ce3a7d8f9dd585db5d061d',
    'camera_apk': 'd378b495e71f6fa416adf3513e6d56acee4c8309371ac837f0bc28c9a0f7fca7',
}


def profile(version='4.0.17.0.XFRCNXM'):
    if version not in ARCHIVES:
        raise RuntimeError('Unsupported official hongkong firmware: ' + str(version))
    name = 'hongkong-identity.json' if version == '4.0.17.0.XFRCNXM' else 'hongkong-4.0.18-identity.json'
    value = json.loads((CONFIG / name).read_text())
    if value.get('device') != 'hongkong' or value.get('schema') != 1:
        raise RuntimeError('Invalid hongkong source profile.')
    value.update(hyperos=version, incremental='OS' + version,
                 archive_sha256=ARCHIVES[version])
    if version == '4.0.17.0.XFRCNXM':
        value.setdefault('pins', dict(LEGACY_PINS))
    required_pins = set(LEGACY_PINS) if version == '4.0.17.0.XFRCNXM' else set(PIN_PATHS)
    if set(value.get('pins', {})) != required_pins:
        raise RuntimeError('Incomplete native/application pins in hongkong profile.')
    value.setdefault('display', {'width': 1120, 'height': 2436, 'density': 480})
    value.setdefault('model_xml', 'hongkong.xml')
    value['model_xml'] = str(CONFIG / value['model_xml'])
    value.setdefault('model_xml_sha256', '807318324b95a9b92e6f95c0c5cb809a41408b009df27acce437a1b063a74db4')
    return value


def profile_for_archive(digest):
    for version, expected in ARCHIVES.items():
        if digest == expected:
            return profile(version)
    raise RuntimeError('Use a pinned original official hongkong OTA ZIP.')


def profile_from_build(build=None):
    if build is None:
        from common import ROOT
        path = ROOT / 'local/build.json'
        build = json.loads(path.read_text()) if path.is_file() else {}
    if build.get('source') not in (None, 'official-hongkong-ota'):
        raise RuntimeError('Phone profile refused a different firmware source.')
    value = profile(build.get('hyperos', '4.0.17.0.XFRCNXM'))
    digest = build.get('archive_sha256')
    if (value['hyperos'] == '4.0.18.0.XFRCNXM' or digest is not None) and digest != value['archive_sha256']:
        raise RuntimeError('Firmware version and OTA archive identity disagree.')
    return value


def parse_properties(data):
    result = {}
    for line in data.decode().splitlines():
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        # Official build.prop files may repeat defaults; Android uses the last.
        result[key] = value
    return result


def identity_values(sources, metadata, keys):
    props = {part: parse_properties(data) for part, data in sources.items()}
    meta = parse_properties(metadata)
    allowed = json.loads((CONFIG / 'hongkong-identity.json').read_text())['properties']
    if set(keys) != set(allowed):
        raise RuntimeError('Unexpected public hongkong identity property set.')
    values = {}
    for key in keys:
        owner = None
        for part in ('system', 'system_ext', 'product', 'vendor', 'odm',
                     'system_dlkm', 'vendor_dlkm'):
            if key.startswith('ro.product.' + part + '.') or key == 'ro.' + part + '.build.fingerprint':
                owner = part
                break
        if owner is None:
            owner = 'vendor'
        if key in props[owner]:
            values[key] = props[owner][key]
    for field in ('brand', 'device', 'manufacturer', 'marketname', 'model', 'name'):
        key = 'ro.product.' + field
        original = 'ro.product.odm.' + field
        if key in keys and original in props['odm']:
            values[key] = props['odm'][original]
    if 'ro.build.fingerprint' in keys:
        values['ro.build.fingerprint'] = meta['post-build']
    missing = set(keys) - values.keys()
    if missing:
        raise RuntimeError('Missing public source identity: ' + ', '.join(sorted(missing)))
    return dict(sorted(values.items()))


def verify_source(value, partitions, metadata):
    """Verify source bytes and identity before any firmware edits or registration."""
    from build_image import erofs
    partitions = Path(partitions)
    expected_parts = set(PROP_PATHS) | {'mi_product'} if value['hyperos'] == '4.0.18.0.XFRCNXM' else {'mi_product'}
    if set(value.get('source_partition_sha256', {})) != expected_parts:
        raise RuntimeError('Incomplete original OTA partition pins.')
    for part, expected in value['source_partition_sha256'].items():
        digest = hashlib.sha256()
        with (partitions / (part + '.img')).open('rb') as stream:
            for block in iter(lambda: stream.read(4 * 1024**2), b''):
                digest.update(block)
        if digest.hexdigest() != expected:
            raise RuntimeError('Unexpected original OTA partition: ' + part)
    required_pins = set(LEGACY_PINS) if value['hyperos'] == '4.0.17.0.XFRCNXM' else set(PIN_PATHS)
    if set(value.get('pins', {})) != required_pins:
        raise RuntimeError('Incomplete native/application source pins.')
    sources = {part: erofs(partitions / (part + '.img'), path)
               for part, path in PROP_PATHS.items()}
    hashes = {part + '.build.prop': hashlib.sha256(data).hexdigest()
              for part, data in sources.items()}
    hashes['ota.metadata'] = hashlib.sha256(metadata).hexdigest()
    required = set(hashes)
    if set(value['source_sha256']) != required:
        raise RuntimeError('Incomplete source hash map for the official hongkong OTA.')
    for key, expected in value['source_sha256'].items():
        if hashes.get(key) != expected:
            raise RuntimeError('Unexpected hongkong source file: ' + key)
    if identity_values(sources, metadata, value['properties']) != value['properties']:
        raise RuntimeError('Public phone identity differs from the pinned original OTA.')
    model = erofs(partitions / 'product.img', '/etc/device_features/hongkong.xml')
    if hashlib.sha256(model).hexdigest() != value['model_xml_sha256']:
        raise RuntimeError('Unexpected official hongkong model configuration.')
    if model != Path(value['model_xml']).read_bytes():
        raise RuntimeError('Model configuration differs from the original OTA.')
    import xml.etree.ElementTree as ET
    display = ET.fromstring(erofs(partitions / 'product.img',
        '/etc/displayconfig/display_id_4630947121878579347.xml')).find('densityMapping/density')
    if display is None or {key: int(display.findtext(key)) for key in ('width', 'height', 'density')} != value['display']:
        raise RuntimeError('Display defaults differ from the original OTA configuration.')
    for key, expected in value['pins'].items():
        part, path = PIN_PATHS[key]
        if hashlib.sha256(erofs(partitions / (part + '.img'), path)).hexdigest() != expected:
            raise RuntimeError('Unexpected native or application source: ' + key)
    return hashes
