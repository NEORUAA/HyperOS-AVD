#!/usr/bin/env python3
"""Refresh baked OS4 defaults in a separate release workspace, without AVD writes."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from common import sdk_path, sha256
from build_image import erofs
from erofs_image import build
from lp_image import pack, unpack
from os4_defaults import image_replacements, production_properties, boot_defaults
from patch_assistant import APK, image_replacements as assistant_replacements

SHARED_FLUTTER = 'system_ext/lib64/libhyper_os_flutter.so'


def validate_pad_source(info):
    from os4_pad import PROFILE, SOURCE
    expected = {'source': SOURCE, 'device': PROFILE['device'],
                'hyperos': PROFILE['hyperos'], 'archive_sha256': PROFILE['source_archive_sha256'],
                'model_xml_sha256': PROFILE['model_xml_sha256'],
                'identity_source_sha256': PROFILE['source_sha256'], 'display': PROFILE['display']}
    if any(info.get(key) != value for key, value in expected.items()):
        raise RuntimeError('Expected the source-pinned official yingtian image.')


def shared_boot_edits(raw, vendor, edits, work):
    """Audit the effective packed init graph, retaining prior firmware repairs."""
    from os4_boot_policy import INIT_PATH, POLICY_PATH, init_files, image_replacements, service_definitions
    partitions = [('', raw), ('vendor', vendor)]
    partitions.extend((path.stem, path) for path in sorted(Path(raw).parent.glob('*.img'))
                      if path.stem not in ('system', 'vendor'))
    graph = init_files(partitions, edits)
    init = edits.get(INIT_PATH, (erofs(raw, '/' + INIT_PATH),))[0]
    policy = edits.get(POLICY_PATH, (erofs(raw, '/' + POLICY_PATH),))[0]
    helpers = [path for path, data in graph.items() if path != 'product/bin/init.qti.display.sh'
               for _ in service_definitions(data, b'hyperos-kernel-services')]
    if helpers:
        from patch_boot_services import BOOT_INIT
        if helpers != [INIT_PATH] or init.count(BOOT_INIT) != 1:
            raise RuntimeError('Unknown existing image kernel helper service.')
        from apply_boot_service_fix import KERNEL_SCRIPT_HASHES, KERNEL_SCRIPT_SHA256, PERF_SCRIPT
        path = 'system/etc/hyperos-kernel-services.sh'
        previous = edits[path][0] if path in edits else erofs(raw, '/' + path)
        if hashlib.sha256(previous).hexdigest() not in KERNEL_SCRIPT_HASHES:
            raise RuntimeError('Unknown existing image kernel helper.')
        current = PERF_SCRIPT.read_bytes()
        if hashlib.sha256(current).hexdigest() != KERNEL_SCRIPT_SHA256:
            raise RuntimeError('Unexpected image kernel helper source.')
        edits[path] = (current, 0o755, 'u:object_r:system_file:s0')
    result, receipt = image_replacements(
        graph, graph.get('product/bin/init.qti.display.sh'), init, policy, work)
    edits.update(result)
    return receipt


def prepare_boot_policy(source, output):
    """Stage only firmware and its receipt; never modify an AVD or userdata."""
    source, output = Path(source).resolve(), Path(output).resolve()
    if source == output or source in output.parents or output in source.parents or output.exists():
        raise RuntimeError('Use a new separate sibling workspace for boot policy preparation.')
    info = json.loads((source / 'local/build.json').read_text())
    if info.get('source') not in ('official-hongkong-ota', 'official-yingtian-ota'):
        raise RuntimeError('Expected an owned official OS4 build.')
    if info['source'] == 'official-hongkong-ota':
        from phone_profile import profile_from_build
        profile_from_build(info)
    else:
        validate_pad_source(info)
    packed = source / 'images/system.img'
    before = sha256(packed)
    if info.get('system_sha256') and info['system_sha256'] != before:
        raise RuntimeError('Source packed image differs from its build receipt.')
    output.mkdir(parents=True)
    (output / 'images').mkdir()
    (output / 'local').mkdir()
    with tempfile.TemporaryDirectory(prefix='boot-policy-', dir=output) as temporary:
        work = Path(temporary)
        unpack(packed, work / 'accepted')
        raw, vendor = work / 'accepted/system.img', work / 'accepted/vendor.img'
        edits = {}
        if info['source'] == 'official-hongkong-ota' and not info.get('boot_service_fix'):
            from phone_profile import profile_from_build
            if profile_from_build(info)['hyperos'] == '4.0.18.0.XFRCNXM':
                from patch_boot_services import TARGETS, BOOT_INIT, BOOT_SEPOLICY
                from patch_boot_services import image_replacements as service_replacements
                extra, receipt = service_replacements({
                    key: erofs(raw, path) for key, (path, _, _) in TARGETS.items()}, work / 'services')
                edits.update(extra)
                init_path = 'system_ext/etc/init/init.hyperos_avd.rc'
                edits[init_path] = (erofs(raw, '/' + init_path) + BOOT_INIT,
                                   0o644, 'u:object_r:system_file:s0')
                policy_path = 'system_ext/etc/selinux/system_ext_sepolicy.cil'
                edits[policy_path] = (erofs(raw, '/' + policy_path) + BOOT_SEPOLICY,
                                     0o644, 'u:object_r:system_file:s0')
                info['boot_service_fix'] = receipt
        info['boot_policy'] = shared_boot_edits(raw, vendor, edits, work / 'policy')
        candidate = work / 'system-fixed.img'
        build(candidate, [('', raw)], work / 'tree', edits)
        for path, (content, _, _) in edits.items():
            if erofs(candidate, '/' + path) != content:
                raise RuntimeError('Boot policy image verification failed: ' + path)
        partitions = [('system', candidate)]
        partitions.extend((path.stem, path) for path in sorted((work / 'accepted').glob('*.img'))
                          if path.stem != 'system')
        pack(packed, output / 'images/system.img', partitions)
        if sha256(packed) != before:
            raise RuntimeError('Source firmware changed during boot policy preparation.')
        info.update(system_sha256=sha256(output / 'images/system.img'), raw_sha256=sha256(candidate))
    (output / 'local/build.json').write_text(json.dumps(info, indent=2) + '\n')
    (output / 'local/boot-policy-source.json').write_text(
        json.dumps({'system_sha256': before, 'boot_policy': info['boot_policy']}, indent=2) + '\n')
    print('Prepared data-neutral boot policy image: ' + str(output), flush=True)


def clone(source, target):
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(['cp', '-c', str(source), str(target)], check=True)


def copy_release_inputs(source, output):
    """Carry portable inputs, deriving new platform provenance after repacking."""
    for directory in ('images', 'tools', 'config'):
        # Core context is bound to the old packed image, unlike universal Apps.
        ignore = shutil.ignore_patterns('os4-core') if directory == 'tools' else None
        shutil.copytree(source / directory, output / directory,
                        copy_function=clone, ignore=ignore)


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


def refresh_shared_flutter(data, firmware):
    """Refresh only a known engine from the selected original OTA profile."""
    from patch_flutter import patch, profile
    before, _ = profile(data)
    if before != firmware['pins']['flutter']:
        raise RuntimeError('Shared Flutter engine differs from its original OTA profile.')
    fixed = patch(data)
    return fixed, {'before': before, 'after': hashlib.sha256(fixed).hexdigest()}


def prepare_boot_services(source, output):
    """Bake r4 repairs from the verified packed r3 image, never guest userdata."""
    from phone_profile import profile_from_build
    from package_release import release_metadata, verify_os4_image
    from patch_boot_services import TARGETS, BOOT_INIT, BOOT_SEPOLICY, image_replacements as service_replacements
    source, output = Path(source).resolve(), Path(output).resolve()
    if source == output or source in output.parents or output in source.parents or output.exists():
        raise RuntimeError('Use a new separate sibling workspace for release preparation.')
    info = json.loads((source / 'local/build.json').read_text())
    if profile_from_build(info)['hyperos'] != '4.0.18.0.XFRCNXM' or info.get('boot_service_fix'):
        raise RuntimeError('Expected the unmodified source-pinned r3 phone release.')
    if info.get('system_sha256') != sha256(source / 'images/system.img'):
        raise RuntimeError('Source packed image differs from its accepted release receipt.')
    output.mkdir(parents=True)
    copy_release_inputs(source, output)
    work = output / 'work'
    unpack(source / 'images/system.img', work / 'accepted')
    raw = work / 'accepted/system.img'
    clone(raw, work / 'hyperos-system.img')
    clone(work / 'accepted/vendor.img', work / 'vendor.img')
    (output / 'local').mkdir()
    (output / 'local/build.json').write_text(json.dumps(info, indent=2) + '\n')
    verify_os4_image(output, release_metadata(output, 'os4-official'))
    edits, receipt = service_replacements(
        {name: erofs(raw, path) for name, (path, _, _) in TARGETS.items()}, work / 'boot-services')
    init_path = 'system_ext/etc/init/init.hyperos_avd.rc'
    init = erofs(raw, '/' + init_path)
    from os4_boot_policy import service_definitions
    if service_definitions(init, b'hyperos-kernel-services'):
        raise RuntimeError('Source image already contains a kernel capability policy.')
    edits[init_path] = (init + BOOT_INIT, 0o644, 'u:object_r:system_file:s0')
    policy_path = 'system_ext/etc/selinux/system_ext_sepolicy.cil'
    edits[policy_path] = (erofs(raw, '/' + policy_path) + BOOT_SEPOLICY,
                          0o644, 'u:object_r:system_file:s0')
    info['boot_policy'] = shared_boot_edits(raw, work / 'accepted/vendor.img', edits,
                                           work / 'boot-policy')
    candidate = work / 'hyperos-system.img'
    build(candidate, [('', raw)], work / 'tree', edits)
    for path, (content, _, _) in edits.items():
        if erofs(candidate, '/' + path) != content:
            raise RuntimeError('Boot service preparation mismatch: ' + path)
    partitions = [('system', candidate)]
    partitions.extend((path.stem, path) for path in sorted((work / 'accepted').glob('*.img'))
                      if path.stem != 'system')
    pack(source / 'images/system.img', output / 'images/system.img', partitions)
    info.update(boot_service_fix=receipt, system_sha256=sha256(output / 'images/system.img'),
                raw_sha256=sha256(candidate))
    (output / 'local/build.json').write_text(json.dumps(info, indent=2) + '\n')
    verify_os4_image(output, release_metadata(output, 'os4-official'))
    print('Prepared isolated r4 release image: ' + str(output), flush=True)


def prepare_pad(source, output, info):
    from apply_app_compat import prepare_prebuilt
    validate_pad_source(info)
    # The running candidate may have been replaced after the builder's raw image.
    # Derive every partition from the actual packed image, never its stale cache.
    work = output / 'work'
    unpack(source / 'images/system.img', work / 'accepted')
    raw = work / 'accepted/system.img'
    prop = authenticated_properties(erofs(raw, '/system/build.prop'))
    candidate = work / 'hyperos-system.img'
    edits = {'system/build.prop': (prop, 0o600, 'u:object_r:system_file:s0')}
    info['boot_policy'] = shared_boot_edits(raw, work / 'accepted/vendor.img', edits,
                                           work / 'boot-policy')
    build(candidate, [('', raw)], work / 'tree', edits)
    for path, (content, _, _) in edits.items():
        if erofs(candidate, '/' + path) != content:
            raise RuntimeError('Pad preparation mismatch: ' + path)
    clone(work / 'accepted/vendor.img', work / 'vendor.img')
    clone(work / 'accepted/system_dlkm.img', work / 'base/system_dlkm.img')
    partitions = [('system', candidate)]
    partitions.extend((path.stem, path) for path in sorted((work / 'accepted').glob('*.img'))
                      if path.stem != 'system')
    pack(source / 'images/system.img', output / 'images/system.img', partitions)
    # The authenticated universal archive owns Weather and Camera payloads.
    # A release must remain buildable after disposable producer caches vanish.
    prepare_prebuilt(output, cache_roots=[output, source])
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
        if info.get('system_sha256') != sha256(source / 'images/system.img'):
            raise RuntimeError('Source packed image differs from its accepted release receipt.')
    else:
        validate_pad_source(info)
        if info.get('system_sha256') and info['system_sha256'] != sha256(source / 'images/system.img'):
            raise RuntimeError('Source packed image differs from its build receipt.')
    output.mkdir(parents=True)
    copy_release_inputs(source, output)
    (output / 'local').mkdir()
    work = output / 'work'
    work.mkdir()
    if variant == 'os4-pad':
        info = prepare_pad(source, output, info)
        (output / 'local/build.json').write_text(json.dumps(info, indent=2) + '\n')
        print('Prepared isolated Pad release image: ' + str(output), flush=True)
        return
    # Cleanup can remove all source work caches, and accepted repairs may have
    # replaced the packed image since those caches were produced.
    accepted = work / 'accepted'
    unpack(source / 'images/system.img', accepted)
    raw = accepted / 'system.img'
    flutter, flutter_receipt = refresh_shared_flutter(erofs(raw, '/' + SHARED_FLUTTER), firmware)
    edits, defaults = image_replacements(sdk_path(), work / 'defaults-overlay', profile=firmware)
    # The resource defaults are unchanged; preserve the already accepted RRO signature.
    overlay = 'product/overlay/HyperOSAVDSettingsDefaults/SettingsDefaults.apk'
    original = erofs(raw, '/' + overlay)
    edits[overlay] = (original, 0o644, 'u:object_r:system_file:s0')
    defaults['settings_overlay_sha256'] = hashlib.sha256(original).hexdigest()
    edits[SHARED_FLUTTER] = (flutter, 0o644, 'u:object_r:system_lib_file:s0')
    edits['product/etc/hyperos-avd-defaults.json'] = (
        (json.dumps(defaults, indent=2) + '\n').encode(), 0o644, 'u:object_r:system_file:s0')
    edits['system/build.prop'] = (production_properties(erofs(raw, '/system/build.prop'), profile=firmware),
                                0o600, 'u:object_r:system_file:s0')
    edits['system_ext/etc/init/init.hyperos_avd.rc'] = (
        boot_defaults(erofs(raw, '/system_ext/etc/init/init.hyperos_avd.rc'), profile=firmware),
        0o644, 'u:object_r:system_file:s0')
    extra, assistant = assistant_replacements(erofs(raw, APK), firmware=firmware)
    edits.update(extra)
    info['boot_policy'] = shared_boot_edits(raw, accepted / 'vendor.img', edits,
                                           work / 'boot-policy')
    candidate = work / 'hyperos-system.img'
    build(candidate, [('', raw)], work / 'tree', edits)
    for path, (content, _, _) in edits.items():
        if erofs(candidate, '/' + path) != content:
            raise RuntimeError('Release preparation mismatch: ' + path)
    clone(accepted / 'vendor.img', work / 'vendor.img')
    clone(accepted / 'system_dlkm.img', work / 'base/system_dlkm.img')
    partitions = [
        ('system', candidate), ('vendor', work / 'vendor.img'),
        ('system_dlkm', work / 'base/system_dlkm.img')]
    # pack() rebuilds liblp from the explicit inputs; retain any additional
    # accepted partition rather than silently dropping it during preparation.
    partitions.extend((path.stem, path) for path in sorted(accepted.glob('*.img'))
                      if path.stem not in {'system', 'vendor', 'system_dlkm'})
    pack(source / 'images/system.img', output / 'images/system.img', partitions)
    info.update(avd_defaults=defaults, assistant_render_fix=assistant, adb_authentication=True,
                flutter_engine=flutter_receipt,
                system_sha256=sha256(output / 'images/system.img'), raw_sha256=sha256(candidate))
    info.pop('preinstalled_backup', None)
    (output / 'local/build.json').write_text(json.dumps(info, indent=2) + '\n')
    print('Prepared isolated release image: ' + str(output), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--variant', choices=('os4-official', 'os4-pad'), default='os4-official')
    parser.add_argument('--boot-service-fixes', action='store_true',
                        help='Bake pinned r4 repairs into the accepted r3 phone image')
    parser.add_argument('--boot-policy', action='store_true',
                        help='Stage shared boot/ART repairs without copying or editing userdata')
    args = parser.parse_args()
    if args.boot_policy:
        if args.boot_service_fixes:
            parser.error('Select one firmware preparation mode.')
        prepare_boot_policy(args.source, args.output)
    elif args.boot_service_fixes:
        if args.variant != 'os4-official':
            parser.error('Boot service fixes only support the phone release.')
        prepare_boot_services(args.source, args.output)
    else:
        prepare(args.source, args.output, args.variant)
