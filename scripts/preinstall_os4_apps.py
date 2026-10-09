#!/usr/bin/env python3
"""Bake the verified signed Weather/Gallery APKs and native fixes into OS4."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import zipfile

APPS = (
    {'package': 'com.miui.weather2', 'directory': 'MIUIWeather',
     'version_name': '18.0.0.27-R', 'version_code': 180000270,
     'sha256': '017d613137c0512a937dc6b8565da856099e988feab47b9eabc5c3d0a13f06e6'},
    {'package': 'com.miui.gallery', 'directory': 'MIUIGallery',
     'version_name': '5.4.2.10-0907-cn', 'version_code': 5040210,
     'sha256': '6a9c9677a6a6ef398c3d60dad58c71942adab010b8e4e7727b2c028063c3fd9d'},
)
REMOVALS = tuple('product/data-app/' + app['directory'] for app in APPS)


def apk_path(bundle, app):
    return Path(bundle) / (app['directory'] + '.apk')


def validated_apks(bundle):
    from common import sha256
    result = []
    for app in APPS:
        path = apk_path(bundle, app)
        if not path.is_file() or sha256(path) != app['sha256']:
            raise RuntimeError('Missing or unsupported signed preload: ' + str(path)
                               + '. Export the verified store APKs with preinstall_os4_apps.py --export.')
        result.append((app, path))
    return result


def export(config, bundle):
    from apply_flutter_fix import official, root
    from common import adb, sha256
    official(config)
    bundle.mkdir(parents=True, exist_ok=True)
    for app in APPS:
        paths = root(config, 'pm path ' + app['package']).splitlines()
        if len(paths) != 1 or not paths[0].startswith('package:'):
            raise RuntimeError('Expected a single signed base APK for ' + app['package'])
        source = paths[0].removeprefix('package:')
        if root(config, 'sha256sum ' + shlex.quote(source)).split()[0] != app['sha256']:
            raise RuntimeError('Installed APK differs from the verified store version: ' + app['package'])
        destination = apk_path(bundle, app)
        if destination.exists() and sha256(destination) != app['sha256']:
            raise RuntimeError('Refusing to overwrite a different APK: ' + str(destination))
        if not destination.exists():
            temporary = destination.with_suffix('.next.apk')
            adb(config, 'pull', source, str(temporary), check=True, capture_output=True, timeout=60)
            if sha256(temporary) != app['sha256']:
                raise RuntimeError('Exported APK checksum mismatch.')
            temporary.replace(destination)
        print('Exported original signed APK: ' + app['package'], flush=True)
    validated_apks(bundle)
    (bundle / 'manifest.json').write_text(json.dumps({'schema': 1, 'apps': APPS}, indent=2) + '\n')


def replacements(bundle, system_image, folder, sdk):
    """Keep signed APK bytes intact; patched native libraries are sidecars."""
    from build_image import erofs
    from common import sha256
    from patch_flutter import patch as patch_flutter
    from patch_weather import ANGLE, LIBRARIES, build_bridge, patch as patch_weather
    apks = validated_apks(bundle)
    folder.mkdir(parents=True, exist_ok=True)
    for name in ANGLE:
        (folder / name).write_bytes(erofs(system_image, '/system/lib64/' + name))
    bridge = build_bridge({'sdk': str(sdk)}, folder)
    edits, manifest = {}, {'schema': 1, 'apps': []}
    label = 'u:object_r:system_file:s0'
    for app, path in apks:
        directory = 'product/app/' + app['directory']
        edits[directory + '/' + path.name] = (path.read_bytes(), 0o644, label)
        libraries = {}
        with zipfile.ZipFile(path) as archive:
            for entry in archive.infolist():
                if not entry.filename.startswith('lib/arm64-v8a/') or entry.is_dir():
                    continue
                name = entry.filename.removeprefix('lib/arm64-v8a/')
                if '/' in name or not name.endswith('.so') or name in libraries:
                    raise RuntimeError('Unexpected APK native entry: ' + entry.filename)
                data = archive.read(entry)
                if (data[:6] != b'\x7fELF\x02\x01' or data[18:20] != b'\xb7\0'):
                    raise RuntimeError('Expected an ARM64 ELF library: ' + name)
                if app['package'] == 'com.miui.weather2':
                    if name == 'libhyper_os_flutter.so':
                        data = patch_flutter(data)
                    elif name in LIBRARIES:
                        data = patch_weather(name, data)
                libraries[name] = hashlib.sha256(data).hexdigest()
                edits[directory + '/lib/arm64/' + name] = (data, 0o644, label)
        if not libraries:
            raise RuntimeError('Missing ARM64 native libraries in ' + str(path))
        if app['package'] == 'com.miui.weather2':
            if not {*LIBRARIES, 'libhyper_os_flutter.so'} <= libraries.keys():
                raise RuntimeError('Weather native workload is incomplete.')
            edits[directory + '/lib/arm64/libhgl.so'] = (bridge.read_bytes(), 0o644, label)
            libraries['libhgl.so'] = sha256(bridge)
        manifest['apps'].append({**app, 'apk': '/' + directory + '/' + path.name,
                                 'native_libraries': libraries})
    edits['product/etc/hyperos-avd-preinstalled-apps.json'] = (
        (json.dumps(manifest, indent=2) + '\n').encode(), 0o644, label)
    return edits, manifest


def prepare_image(bundle, sdk):
    from common import ROOT
    from build_image import erofs
    from erofs_image import build
    from packed_source import packed_candidate
    from patch_flutter import patch
    folder = ROOT / 'work/preinstalled-candidate'
    with packed_candidate(ROOT, folder, 'official-hongkong-ota') as candidate:
        source = candidate.raw
        edits, manifest = replacements(bundle, source, candidate.work / 'native', sdk)
        edits['system_ext/lib64/libhyper_os_flutter.so'] = (
            patch(erofs(source, '/system_ext/lib64/libhyper_os_flutter.so')),
            0o644, 'u:object_r:system_lib_file:s0')
        raw = candidate.work / 'hyperos-system.img'
        build(raw, [('', source)], candidate.work / 'tree', edits, removals=REMOVALS)
        # Verify content as well as the builder's full inode metadata comparison.
        for path, (data, _, _) in edits.items():
            if erofs(raw, '/' + path) != data:
                raise RuntimeError('Preinstalled image content mismatch: ' + path)
        packed = candidate.finish(raw, {'preinstalled_apps': manifest})
    print('Verified candidate ready: ' + str(packed), flush=True)
    print('Stop only the official OS4 AVD before activating it. Userdata must be retained.', flush=True)


def main():
    repo = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', type=Path, default=repo / 'work/os4-official')
    parser.add_argument('--bundle', type=Path)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--export', action='store_true', help='Export the two verified installed signed APKs')
    action.add_argument('--prepare-image', action='store_true', help='Build a separate candidate; never stop an AVD')
    args = parser.parse_args()
    os.environ['HYPEROS_AVD_WORKSPACE'] = str(args.workspace.resolve())
    from common import ROOT, host_check, runtime, sdk_path
    host_check()
    bundle = args.bundle.resolve() if args.bundle else ROOT / 'input/preinstalled-apps'
    if args.export:
        export(runtime(), bundle)
    else:
        build = ROOT / 'local/build.json'
        if not build.is_file() or json.loads(build.read_text()).get('source') != 'official-hongkong-ota':
            raise RuntimeError('This operation requires an existing official OS4 workspace.')
        prepare_image(bundle, sdk_path())


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        raise SystemExit(str(error))
