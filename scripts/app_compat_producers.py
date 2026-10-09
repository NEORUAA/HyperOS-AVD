"""Build the complete OS4 app recipe union from audited local producer assets.

This is a release/build API. The guest and consumer installer only receive the
resulting universal archive; they never discover donor workspaces or compile.
"""
import hashlib
import io
from pathlib import Path
import subprocess
import uuid
import zipfile

import apply_camera_fix as parrot
import apply_pad_camera_native_fix as planes_b
import apply_xiaomi_camera_fix as planes_a
from common import REPO_ROOT, sha256, tool
import patch_weather as weather


CAMERA_PACKAGE = 'com.android.camera'
WEATHER_PACKAGE = 'com.miui.weather2'
CAMERA_APK = planes_a.APK
CAMERA_NATIVE = planes_a.APP + '/lib/arm64'
CAMERA_ENTRY = 'lib/arm64-v8a/libcamera_yuv_jni.so'
# Audited original factory preopt files from source-verified packed images.
# APK content selects these immutable anchors, never OTA/device/AVD names.
CAMERA_FACTORY_PASSTHROUGH = {
    'd378b495e71f6fa416adf3513e6d56acee4c8309371ac837f0bc28c9a0f7fca7': {
        'oat/arm64/MiuiCamera.odex': 'e58d61fbaee2bfb1001d5aed8c4d00dbd885857dd742d70de8892dd0b1142aee',
        'oat/arm64/MiuiCamera.vdex': 'dffd9d16ca7de14fa608517ad94d1c1008fa590da78e9d3dceceb76d40133029'},
    '729b0b193d1684e7c14ed0ca56ba15247d58016c70164329de233b49727a2c48': {
        'oat/arm64/MiuiCamera.odex': 'aecbdeb931136a74f82623f5c2925dc9be9fd59258b124c2dba5a77edb919328',
        'oat/arm64/MiuiCamera.vdex': '4e26b05784c3f1bc5bf1b69959e41a561694ef39126da2fe006302365606b2eb'},
    '70e32c772c406d2a0ccd21af23d001d26acbef4b46d07cee6b4b55cf1070e8c0': {
        'oat/arm64/MiuiCamera.odex': '2d5c0e7d30ca890258e2fb80a02ee2ae01c20424b198199519788581a3c9056c',
        'oat/arm64/MiuiCamera.vdex': '6950cefb42c23713726d12a0f68c710fb3d89f3b536b3250fe2a9fed6557bd5c'},
}


def _camera_recipe(identifier, apk, runtime, jni, yuv, enabled):
    passthrough = CAMERA_FACTORY_PASSTHROUGH.get(apk)
    if passthrough is None:
        raise RuntimeError('No audited OEM factory directory assets for APK: ' + apk)
    return {'id': identifier, 'feature': 'oem-camera', 'package': CAMERA_PACKAGE,
            'default_enabled': enabled, 'apk_sha256': apk,
            'factory_apk': CAMERA_APK, 'native': CAMERA_NATIVE,
            'factory_overlay': True, 'apk_libraries': {CAMERA_ENTRY: jni},
            'factory_passthrough': dict(passthrough),
            'system_libraries': {'/vendor/lib64/libc++.so': planes_a.CPP_HASH,
                                 '/system/lib64/libandroid_runtime.so': runtime},
            'native_libraries': {},
            'libraries': [{'name': 'libcamera_yuv_jni.so', 'before': jni,
                           'after': yuv, 'placeholder': True}],
            'system_targets': [
                {'target': planes_a.PROVIDER, 'before': planes_a.PROVIDER_HASH,
                 'after': planes_a.PROVIDER_AFTER, 'mount_flags': 'ro,suid,exec'},
                {'target': planes_a.HWL, 'before': planes_a.HWL_HASH,
                 'after': planes_a.PAYLOADS['hwl.so'], 'mount_flags': 'ro,nosuid,nodev'}],
            'policy': {'angle': {'driver': 'angle', 'features': ['exposeES32ForTesting']}}}


def recipes():
    """Return every audited workload, independent of donor/device/OTA names."""
    values = []
    for identifier, apk, natives, factory, native, private_angle in (
            ('weather-mgl-d35b0a', weather.APK_SHA256, weather.LIBRARIES,
             '/product/app/MIUIWeather/MIUIWeather.apk', '/product/app/MIUIWeather/lib/arm64', False),
            ('weather-mgl-ab3942', weather.PAD_APK_SHA256, weather.PAD_LIBRARIES,
             weather.PAD_APK, weather.PAD_NATIVE, True)):
        libraries = [{'name': name, 'before': before, 'after': after, 'placeholder': False}
                     for name, (before, after) in natives.items()]
        libraries.append({'name': 'libhgl.so', 'before': weather.EMPTY,
                          'after': weather.BRIDGE_SHA256, 'placeholder': True,
                          'legacy_direct': [weather.BRIDGE_SHA256]})
        if private_angle:
            libraries += [{'name': name, 'before': weather.EMPTY, 'after': checksum,
                           'placeholder': True, 'legacy_direct': [checksum]}
                          for name, checksum in weather.ANGLE.items()]
        values.append({'id': identifier, 'feature': 'weather', 'package': WEATHER_PACKAGE,
                       'default_enabled': True, 'apk_sha256': apk,
                       'factory_apk': factory, 'native': native,
                       'system_libraries': {} if private_angle else {
                           '/system/lib64/' + name: checksum for name, checksum in weather.ANGLE.items()},
                       'native_libraries': {}, 'libraries': libraries, 'system_targets': []})
    for apk in sorted(planes_a.CAMERA_VERSIONS):
        values.append(_camera_recipe('camera-imagereader-22b624-' + apk[:12], apk,
                                     planes_a.RUNTIME_HASH, planes_a.YUV_HASH,
                                     planes_a.PAYLOADS['yuv.so'], False))
    values.append(_camera_recipe('camera-imagereader-5aad1f', planes_b.APK_HASH,
                                 planes_b.RUNTIME_HASH, planes_b.YUV_BEFORE,
                                 planes_b.YUV_AFTER, True))
    value = parrot.bridge_profiles()[0]
    values.append({'id': 'parrot-aion-ddf45e', 'feature': 'parrot-camera',
                   'package': parrot.PACKAGE, 'default_enabled': False,
                   'apk_sha256': value['apk_sha256'],
                   'system_libraries': dict(value['system_libraries']),
                   'native_libraries': dict(value['native_libraries']),
                   'libraries': [dict(item) for item in value['libraries']], 'system_targets': []})
    return sorted(values, key=lambda row: row['id'])


def _regular(path):
    path = Path(path)
    if not path.is_file() or path.is_symlink() or path.stat().st_nlink != 1:
        raise RuntimeError('Invalid app compatibility producer asset: ' + str(path))
    return path


def _cache_directory(root, name):
    root = Path(root).resolve()
    tools = root / 'tools'
    # Installed workspaces expose the current version through an owned alias.
    # Resolve only that root-level alias; caches and files still cannot alias.
    if tools.is_symlink():
        target = tools.resolve()
        if not target.is_dir() or root not in target.parents:
            raise RuntimeError('Invalid app compatibility producer directory: ' + str(tools))
        tools = target
    folder = tools / name
    for path in (tools, folder):
        if path.is_symlink() or path.exists() and not path.is_dir():
            raise RuntimeError('Invalid app compatibility producer directory: ' + str(path))
    return folder


def _source_pins():
    pins = {**planes_a.SOURCES, **planes_b.SOURCES,
            'weather_angle.c': weather.BRIDGE_SOURCE_SHA256,
            'camera_aion_cpu.c': parrot.BRIDGE_SOURCE_SHA256}
    for name, checksum in pins.items():
        path = _regular(REPO_ROOT / 'native' / name)
        if sha256(path) != checksum:
            raise RuntimeError('App compatibility source changed: ' + name)


def _publish(folder, checksum, data):
    if hashlib.sha256(data).hexdigest() != checksum:
        raise RuntimeError('App compatibility producer output differs from its audited recipe.')
    path = folder / (checksum + '.bin')
    if path.exists() or path.is_symlink():
        if sha256(_regular(path)) != checksum:
            raise RuntimeError('App compatibility output cache changed: ' + str(path))
        return path
    stage = path.with_name(path.name + '.stage-' + uuid.uuid4().hex)
    try:
        with stage.open('xb') as output:
            output.write(data)
        stage.chmod(0o644)
        if path.exists() or path.is_symlink():
            raise RuntimeError('App compatibility output cache changed during publication.')
        stage.rename(path)
    finally:
        stage.unlink(missing_ok=True)
    return path


class PackedFiles:
    """Read tiny audited files directly from a verified linear LP partition."""
    def __init__(self):
        self.sources = {}

    def read(self, root, partition, path):
        from packed_source import source_info
        from lp_image import read_lp, SECTOR
        if root not in self.sources:
            _, image, _, _ = source_info(root)
            base, partitions = read_lp(image)
            self.sources[root] = (image, base, {row['name']: row for row in partitions})
        image, base, partitions = self.sources[root]
        part = partitions.get(partition)
        # The emulator's SAR image can fold Android's product/system_ext
        # directories into the system LP entry instead of shipping extra LPs.
        if part is None and partition in ('product', 'system_ext'):
            part = partitions.get('system')
            path = '/' + partition + path
        if not part or len(part['extents']) != 1:
            raise RuntimeError('Producer requires one verified linear LP extent: ' + partition)
        length, kind, start, device = part['extents'][0]
        if kind != 0 or device != 0 or length * SECTOR != part['size']:
            raise RuntimeError('Unsupported app producer LP extent.')
        result = subprocess.run([tool('dump.erofs', 'erofs-utils'), '--cat',
                                 '--offset=' + str(base + start * SECTOR), '--path=' + path,
                                 str(image)], check=False, capture_output=True)
        return result.stdout if result.returncode == 0 else None


def collect_payloads(cache_roots, sdk=None, output=None):
    """Collect the universal union; consumer startup never invokes this API.

    Explicit donor roots can have any name. Existing producer caches are reused;
    missing Weather inputs are read from verified packed images without copying
    multi-GiB partitions. No NDK, ADB, guest mutation or download is involved.
    """
    del sdk  # Kept for build orchestration compatibility; prebuilt inputs are mandatory.
    if isinstance(cache_roots, (str, Path)):
        cache_roots = [cache_roots]
    roots = sorted({Path(root).resolve() for root in cache_roots}, key=str)
    if not roots:
        raise RuntimeError('No local audited app compatibility producer roots were supplied.')
    _source_pins()
    folder = Path(output) if output is not None else roots[0] / 'work/app-compat-inputs'
    if folder.is_symlink() or folder.exists() and not folder.is_dir():
        raise RuntimeError('Invalid app compatibility output directory.')
    folder.mkdir(parents=True, exist_ok=True)
    payloads = {}

    def accept(checksum, data):
        payloads[checksum] = _publish(folder, checksum, data)

    camera_inputs = ((planes_a, 'xiaomi-camera'), (planes_b, 'pad-camera-native'))
    for producer, name in camera_inputs:
        found = False
        for root in roots:
            cache = _cache_directory(root, name)
            if not cache.exists():
                continue
            producer.verify_universal_prebuilt(cache)
            for filename, checksum in producer.PAYLOADS.items():
                accept(checksum, (cache / filename).read_bytes())
            found = True
        if not found:
            raise RuntimeError('Missing audited producer bundle: ' + name)
    found = False
    for root in roots:
        cache = _cache_directory(root, 'parrot-camera')
        if not cache.exists():
            continue
        receipt = _regular(cache / 'receipt.json')
        import json
        try:
            saved = json.loads(receipt.read_text())
        except (OSError, ValueError) as error:
            raise RuntimeError('Invalid audited allocator receipt.') from error
        if saved != parrot.prebuilt_receipt():
            raise RuntimeError('Unknown audited allocator receipt.')
        accept(parrot.BRIDGE_SHA256, _regular(cache / 'lib_aion_buffer.so').read_bytes())
        found = True
    if not found:
        raise RuntimeError('Missing audited allocator producer bundle.')

    packed = PackedFiles()
    for root in roots:
        cache = _cache_directory(root, 'weather-angle')
        # Old producer folders can contain only the two copied dependencies.
        # A present complete-helper receipt must validate in full.
        if any((cache / name).exists() or (cache / name).is_symlink()
               for name in ('libhgl.so', 'receipt.json')):
            weather.verify_bridge_prebuilt(cache)
            accept(weather.BRIDGE_SHA256, (cache / 'libhgl.so').read_bytes())
        for name, checksum in weather.ANGLE.items():
            candidate = cache / name
            if candidate.exists() or candidate.is_symlink():
                accept(checksum, _regular(candidate).read_bytes())
        candidate = root / 'work/weather-angle-fix/libhgl.so'
        if candidate.exists() or candidate.is_symlink():
            accept(weather.BRIDGE_SHA256, _regular(candidate).read_bytes())
    # Only a missing pinned helper/dependency invokes the verified packed reader.
    packed_targets = [('product', '/app/MIUIWeather/lib/arm64/libhgl.so', weather.BRIDGE_SHA256),
                      *[('system', '/system/lib64/' + name, checksum)
                        for name, checksum in weather.ANGLE.items()]]
    for partition, path, checksum in packed_targets:
        if checksum in payloads:
            continue
        for root in roots:
            if not (root / 'images/system.img').is_file():
                continue
            data = packed.read(root, partition, path)
            if data and hashlib.sha256(data).hexdigest() == checksum:
                accept(checksum, data)
                break
        if checksum not in payloads:
            raise RuntimeError('Missing pinned Weather producer dependency: ' + path)

    workloads = [(weather.APK_SHA256, weather.LIBRARIES,
                  '/app/MIUIWeather/MIUIWeather.apk'),
                 (weather.PAD_APK_SHA256, weather.PAD_LIBRARIES,
                  '/data-app/MIUIWeather/MIUIWeather.apk')]
    for checksum, libraries, factory_path in workloads:
        data = None
        for root in roots:
            candidates = (root / 'work/flutter-render-fix/com.miui.weather2-current.apk',
                          root / ('work/flutter-render-fix/com.miui.weather2-' + checksum + '.apk'))
            for candidate in candidates:
                if candidate.exists() or candidate.is_symlink():
                    raw = _regular(candidate).read_bytes()
                    if hashlib.sha256(raw).hexdigest() == checksum:
                        data = raw
                        break
            if data is not None:
                break
        if data is None:
            for root in roots:
                if not (root / 'images/system.img').is_file():
                    continue
                raw = packed.read(root, 'product', factory_path)
                if raw and hashlib.sha256(raw).hexdigest() == checksum:
                    data = raw
                    break
        if data is None:
            raise RuntimeError('Missing unchanged signed Weather producer APK: ' + checksum)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for name, (_, after) in libraries.items():
                entry = 'lib/arm64-v8a/' + name
                if archive.namelist().count(entry) != 1:
                    raise RuntimeError('Ambiguous signed Weather native entry: ' + entry)
                accept(after, weather.patch(name, archive.read(entry), libraries=libraries))
    expected = {target['after'] for recipe in recipes()
                for target in [*recipe['libraries'], *recipe['system_targets']]}
    if set(payloads) != expected:
        raise RuntimeError('App compatibility payload union differs from its complete catalog.')
    return dict(sorted(payloads.items()))
