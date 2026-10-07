#!/usr/bin/env python3
"""Refresh baked OS4 defaults in a separate release workspace, without AVD writes."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess

from common import sdk_path, sha256
from build_image import erofs
from erofs_image import build
from lp_image import pack, unpack
from os4_defaults import image_replacements, production_properties, boot_defaults
from patch_assistant import APK, image_replacements as assistant_replacements


def clone(source, target):
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(['cp', '-c', str(source), str(target)], check=True)


def authenticated_properties(data):
    """Restore ADB authorization without changing the accepted device defaults."""
    lines = data.splitlines()
    for key, allowed, value in ((b'ro.adb.secure', (b'0', b'1'), b'1'),
                                (b'ro.debuggable', (b'0',), b'0')):
        matches = [line for line in lines if line.startswith(key + b'=')]
        if len(matches) != 1 or matches[0].partition(b'=')[2] not in allowed:
            raise RuntimeError('Missing or ambiguous production property: ' + key.decode())
        lines[lines.index(matches[0])] = key + b'=' + value
    return b'\n'.join(lines) + b'\n'


def prepare_pad(source, output, info):
    from os4_pad import PROFILE, SOURCE
    from patch_weather import ANGLE, bridge_prebuilt_receipt, verify_bridge_prebuilt
    from apply_pad_camera_native_fix import build as camera_build
    if (info.get('source') != SOURCE or info.get('device') != PROFILE['device']
            or info.get('hyperos') != PROFILE['hyperos']
            or info.get('archive_sha256') != PROFILE['source_archive_sha256']
            or info.get('model_xml_sha256') != PROFILE['model_xml_sha256']
            or info.get('identity_source_sha256') != PROFILE['source_sha256']):
        raise RuntimeError('Expected the source-pinned official yingtian image.')
    # The running candidate may have been replaced after the builder's raw image.
    # Derive every partition from the actual packed image, never its stale cache.
    work = output / 'work'
    unpack(source / 'images/system.img', work / 'accepted')
    raw = work / 'accepted/system.img'
    prop = authenticated_properties(erofs(raw, '/system/build.prop'))
    candidate = work / 'hyperos-system.img'
    build(candidate, [('', raw)], work / 'tree', {
        'system/build.prop': (prop, 0o600, 'u:object_r:system_file:s0')})
    if erofs(candidate, '/system/build.prop') != prop:
        raise RuntimeError('Production ADB property was not baked correctly.')
    clone(work / 'accepted/vendor.img', work / 'vendor.img')
    clone(work / 'accepted/system_dlkm.img', work / 'base/system_dlkm.img')
    pack(source / 'images/system.img', output / 'images/system.img', [
        ('system', candidate), ('vendor', work / 'vendor.img'),
        ('system_dlkm', work / 'base/system_dlkm.img')])
    cache = output / 'tools/weather-angle'
    for name, checksum in ANGLE.items():
        if sha256(cache / name) != checksum:
            raise RuntimeError('Unverified Weather ANGLE release input: ' + name)
    clone(source / 'work/weather-angle-fix/libhgl.so', cache / 'libhgl.so')
    (cache / 'receipt.json').write_text(json.dumps(bridge_prebuilt_receipt(), indent=2) + '\n')
    verify_bridge_prebuilt(cache)
    # Its own source/hash receipt checks that end users will not need an NDK.
    import apply_pad_camera_native_fix
    previous = apply_pad_camera_native_fix.ROOT
    try:
        apply_pad_camera_native_fix.ROOT = output
        camera_build(None, work / 'verified-camera')
    finally:
        apply_pad_camera_native_fix.ROOT = previous
    template = output / 'config/avd.ini'
    text = template.read_text().replace('disk.dataPartition.size=6G', 'disk.dataPartition.size=32G')
    template.write_text(text)
    info.update(adb_authentication=True, system_sha256=sha256(output / 'images/system.img'),
                raw_sha256=sha256(candidate))
    info.pop('memory_limit_mib', None)
    return info


def prepare(source, output, variant='os4-official'):
    source, output = source.resolve(), output.resolve()
    if source == output or source in output.parents or output in source.parents or output.exists():
        raise RuntimeError('Use a new separate sibling workspace for release preparation.')
    info = json.loads((source / 'local/build.json').read_text())
    expected = {'os4-official': 'official-hongkong-ota', 'os4-pad': 'official-yingtian-ota'}
    if variant not in expected or info.get('source') != expected[variant]:
        raise RuntimeError('Expected the official OS4 build.')
    firmware = None
    if variant == 'os4-official':
        from phone_profile import profile_from_build
        firmware = profile_from_build(info)
    raw = source / 'work/hyperos-system.img'
    output.mkdir(parents=True)
    for directory in ('images', 'tools', 'config'):
        shutil.copytree(source / directory, output / directory, copy_function=clone)
    (output / 'local').mkdir()
    work = output / 'work'
    work.mkdir()
    if variant == 'os4-pad':
        info = prepare_pad(source, output, info)
        (output / 'local/build.json').write_text(json.dumps(info, indent=2) + '\n')
        print('Prepared isolated Pad release image: ' + str(output), flush=True)
        return
    edits, defaults = image_replacements(sdk_path(), work / 'defaults-overlay', profile=firmware)
    # The resource defaults are unchanged; preserve the already accepted RRO signature.
    overlay = 'product/overlay/HyperOSAVDSettingsDefaults/SettingsDefaults.apk'
    original = erofs(raw, '/' + overlay)
    edits[overlay] = (original, 0o644, 'u:object_r:system_file:s0')
    import hashlib
    defaults['settings_overlay_sha256'] = hashlib.sha256(original).hexdigest()
    edits['product/etc/hyperos-avd-defaults.json'] = (
        (json.dumps(defaults, indent=2) + '\n').encode(), 0o644, 'u:object_r:system_file:s0')
    edits['system/build.prop'] = (production_properties(erofs(raw, '/system/build.prop'), profile=firmware),
                                0o600, 'u:object_r:system_file:s0')
    edits['system_ext/etc/init/init.hyperos_avd.rc'] = (
        boot_defaults(erofs(raw, '/system_ext/etc/init/init.hyperos_avd.rc'), profile=firmware),
        0o644, 'u:object_r:system_file:s0')
    extra, assistant = assistant_replacements(erofs(raw, APK), firmware=firmware)
    edits.update(extra)
    candidate = work / 'hyperos-system.img'
    build(candidate, [('', raw)], work / 'tree', edits)
    for path, (content, _, _) in edits.items():
        if erofs(candidate, '/' + path) != content:
            raise RuntimeError('Release preparation mismatch: ' + path)
    clone(source / 'work/vendor.img', work / 'vendor.img')
    clone(source / 'work/base/system_dlkm.img', work / 'base/system_dlkm.img')
    pack(source / 'images/system.img', output / 'images/system.img', [
        ('system', candidate), ('vendor', work / 'vendor.img'),
        ('system_dlkm', work / 'base/system_dlkm.img')])
    info.update(avd_defaults=defaults, assistant_render_fix=assistant, adb_authentication=True,
                system_sha256=sha256(output / 'images/system.img'), raw_sha256=sha256(candidate))
    info.pop('preinstalled_backup', None)
    (output / 'local/build.json').write_text(json.dumps(info, indent=2) + '\n')
    print('Prepared isolated release image: ' + str(output), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--variant', choices=('os4-official', 'os4-pad'), default='os4-official')
    args = parser.parse_args()
    prepare(args.source, args.output, args.variant)
