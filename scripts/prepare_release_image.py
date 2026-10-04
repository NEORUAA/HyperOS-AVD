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
from lp_image import pack
from os4_defaults import image_replacements, production_properties, boot_defaults
from patch_assistant import APK, image_replacements as assistant_replacements


def clone(source, target):
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(['cp', '-c', str(source), str(target)], check=True)


def prepare(source, output):
    source, output = source.resolve(), output.resolve()
    if source == output or source in output.parents or output in source.parents or output.exists():
        raise RuntimeError('Use a new separate sibling workspace for release preparation.')
    info = json.loads((source / 'local/build.json').read_text())
    if info.get('source') != 'official-hongkong-ota':
        raise RuntimeError('Expected the official OS4 build.')
    raw = source / 'work/hyperos-system.img'
    output.mkdir(parents=True)
    for directory in ('images', 'tools', 'config'):
        shutil.copytree(source / directory, output / directory, copy_function=clone)
    (output / 'local').mkdir()
    work = output / 'work'
    work.mkdir()
    edits, defaults = image_replacements(sdk_path(), work / 'defaults-overlay')
    # The resource defaults are unchanged; preserve the already accepted RRO signature.
    overlay = 'product/overlay/HyperOSAVDSettingsDefaults/SettingsDefaults.apk'
    original = erofs(raw, '/' + overlay)
    edits[overlay] = (original, 0o644, 'u:object_r:system_file:s0')
    import hashlib
    defaults['settings_overlay_sha256'] = hashlib.sha256(original).hexdigest()
    edits['product/etc/hyperos-avd-defaults.json'] = (
        (json.dumps(defaults, indent=2) + '\n').encode(), 0o644, 'u:object_r:system_file:s0')
    edits['system/build.prop'] = (production_properties(erofs(raw, '/system/build.prop')),
                                0o600, 'u:object_r:system_file:s0')
    edits['system_ext/etc/init/init.hyperos_avd.rc'] = (
        boot_defaults(erofs(raw, '/system_ext/etc/init/init.hyperos_avd.rc')),
        0o644, 'u:object_r:system_file:s0')
    extra, assistant = assistant_replacements(erofs(raw, APK))
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
    info.update(avd_defaults=defaults, assistant_render_fix=assistant,
                system_sha256=sha256(output / 'images/system.img'), raw_sha256=sha256(candidate))
    info.pop('preinstalled_backup', None)
    (output / 'local/build.json').write_text(json.dumps(info, indent=2) + '\n')
    print('Prepared isolated release image: ' + str(output), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    prepare(args.source, args.output)
