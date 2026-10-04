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
              'hardware_base_api', 'kernel_kmi', 'surfaceflinger_backend', 'gnss_patch',
              'mi_ext_overlays', 'mi_ext_software_identity', 'flutter_render_fix',
              'preinstalled_apps', 'native_quickstep_identity', 'avd_defaults',
              'assistant_render_fix', 'composer_alpha_fix', 'audio_pcm_fix', 'camera_scene_fix',
              'adb_authentication', 'experimental')


def verify_os4_image(root, metadata):
    """Verify the release's actual packed image and baked compatibility files."""
    from lp_image import read_lp
    from os4_defaults import COMPONENT_XML, PROVIDER, MODEL_XML, DISPLAY, AOD_SCRIPT, AOD_INIT
    from patch_flutter import PROFILES
    from patch_weather import ANGLE
    from patch_assistant import MANIFEST as ASSISTANT_FIX
    from patch_composer import MANIFEST as COMPOSER_FIX
    from patch_audio import MANIFEST as AUDIO_FIX
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
    if metadata['build'].get('composer_alpha_fix') != COMPOSER_FIX:
        raise RuntimeError('Missing verified ranchu composer alpha fix metadata.')
    composer = subprocess.check_output([dump, '--cat', '--path=' +
        COMPOSER_FIX['native'].removeprefix('/vendor'), str(vendor)])
    if hashlib.sha256(composer).hexdigest() != COMPOSER_FIX['native_sha256']:
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
    def read(path):
        return subprocess.check_output([dump, '--cat', '--path=' + path, str(raw)])
    prop = read('/system/build.prop')
    for line in (b'ro.build.version.sdk=37', b'ro.adb.secure=1', b'ro.debuggable=0',
                 b'ro.miui.product.home=com.miui.home'):
        if prop.splitlines().count(line) != 1:
            raise RuntimeError('Missing or ambiguous release property: ' + line.decode())
    if read('/system/etc/sysconfig/hyperos-avd-components.xml') != COMPONENT_XML:
        raise RuntimeError('OOBE component override differs from the verified default.')
    defaults = metadata['build']['avd_defaults']
    if defaults.get('finddevice_disabled_component') != PROVIDER:
        raise RuntimeError('Unexpected OOBE component metadata.')
    if json.loads(read('/product/etc/hyperos-avd-defaults.json')) != defaults:
        raise RuntimeError('Baked awake defaults do not match release metadata.')
    if read('/product/etc/device_features/emu64a.xml') != MODEL_XML:
        raise RuntimeError('Missing verified emulator AOD features.')
    if read('/product/etc/device_features/hongkong.xml') != MODEL_XML:
        raise RuntimeError('Emulator and original phone model configurations must match.')
    if b'start console' in read('/system_ext/etc/init/init.hyperos_avd.rc'):
        raise RuntimeError('Debug console must be disabled in the OS4 release.')
    if (AOD_INIT not in read('/system_ext/etc/init/init.hyperos_avd.rc')
            or read('/system_ext/bin/hyperos-avd-aod-defaults.sh') != AOD_SCRIPT):
        raise RuntimeError('Missing first-boot AOD service.')
    if defaults.get('display') != DISPLAY or not defaults.get('aod', {}).get('support_aod_fullscreen'):
        raise RuntimeError('Missing official display / AOD defaults.')
    template = dict(line.split('=', 1) for line in (root / 'config/avd.ini').read_text().splitlines() if '=' in line)
    if template.get('hw.audioOutput') != 'yes':
        raise RuntimeError('Release AVD audio output must be enabled.')
    for key, value in DISPLAY.items():
        if template.get('hw.lcd.' + key) != str(value):
            raise RuntimeError('Release AVD differs from the official primary display: ' + key)
    expected = {
        ASSISTANT_FIX['apk']: ASSISTANT_FIX['apk_sha256'],
        ASSISTANT_FIX['native']: ASSISTANT_FIX['native_sha256'],
        '/product/overlay/HyperOSAVDSettingsDefaults/SettingsDefaults.apk': defaults['settings_overlay_sha256'],
        '/product/priv-app/MIUIFindDeviceCN/MIUIFindDeviceCN.apk': defaults['finddevice_apk_sha256'],
        '/system_ext/lib64/libhyper_os_flutter.so': PROFILES['71caea24a7fec06ae7c1b7cdb93c99f45288154a9ca21bb634d8181a97dcef62']['output'],
        **{'/system/lib64/' + name: checksum for name, checksum in ANGLE.items()},
    }
    apps = metadata['build']['preinstalled_apps']
    if metadata['build'].get('assistant_render_fix') != ASSISTANT_FIX:
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
    print(f'OS4 preflight passed: packed system/vendor, properties, defaults and {len(expected)} signed/native files.', flush=True)


def release_metadata(root, variant):
    metadata = {'project': 'HyperOS-AVD', 'format': 1, 'platform': 'macos-arm64',
                'hyperos': '3.0.2.0.WMCCNXM', 'android_api': 36,
                'kernelsu': '3.3.0 (32601)', 'gnss_patch': 'status-satellite-first-fix-async'}
    if variant == 'os4-official':
        build = json.loads((root / 'local/build.json').read_text())
        if (build.get('source') != OS4_SOURCE or build.get('android_api') != 37
                or build.get('hyperos') != '4.0.17.0.XFRCNXM'
                or build.get('adb_authentication') is not True
                or build.get('flutter_render_fix') != 6
                or not build.get('native_quickstep_identity')
                or not build.get('preinstalled_apps') or not build.get('avd_defaults')):
            raise RuntimeError('OS4 release requires the verified official image, native fixes, defaults and secure ADB.')
        metadata.update(format=3, variant=variant, hyperos=build['hyperos'], android_api=37,
                        source=OS4_SOURCE, source_device='hongkong', hardware_base_api=36,
                        build={key: build[key] for key in BUILD_KEYS if key in build})
    elif (root / 'local/build.json').exists() and json.loads((root / 'local/build.json').read_text()).get('source') == OS4_SOURCE:
        raise RuntimeError('Refusing to label official OS4 firmware as OS3.')
    if variant == 'os4-official':
        metadata['compatibility'] = {'minimum_installer': '1.0.0',
            'userdata_family': 'os4-hongkong-api37-ranchu-4k',
            'upgrade_from': ['v0.2.0-a17-hyperos4-hongkong-r1'],
            'runtime_in_bundle': True}
    return metadata


def runtime_files():
    """Explicit portable source set; exclude logs, secrets, user state and binaries."""
    paths = {'Install.command': REPO_ROOT / 'Install.command'}
    for directory, patterns in {'scripts': ('*.py',), 'config': ('*.ini', '*.xml', '*.json', '*.sh', '*.rc'),
                                'native': ('*.S', '*.c', '*.cpp', '*.m')}.items():
        for pattern in patterns:
            for path in sorted((REPO_ROOT / directory).glob(pattern)):
                paths[path.relative_to(REPO_ROOT).as_posix()] = path
    return paths


def write_bundle(directory, version, metadata, paths, part_mib=1536):
    """Archive only explicit portable files; publish the manifest last."""
    if not re.fullmatch(r'v[0-9A-Za-z_.-]+', version):
        raise RuntimeError('Invalid release version.')
    if not 1 <= part_mib < 2048:
        raise RuntimeError('Every GitHub release asset must be smaller than 2 GiB.')
    files = {}
    for relative, path in paths.items():
        if path.is_symlink() or not path.is_file():
            raise RuntimeError('Release files must not be symlinks.')
        allowed = (relative.startswith('images/') or relative in ('tools/' + name for name in KSU_FILES)
                   or metadata['format'] in (2, 3) and relative == 'config/avd.ini'
                   or metadata['format'] == 3 and relative.startswith('runtime/')
                   or relative in ('tools/emulator_srgb.dylib', 'tools/emulator_srgb.build.json')
                   or metadata['format'] == 3 and relative in ('tools/xiaomi-camera/' + name for name in
                      ('provider', 'hwl.so', 'yuv.so', 'manifest.json', 'receipt.json')))
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
    parser.add_argument('--variant', choices=('os3', 'os4-official'), default='os3')
    parser.add_argument('--part-mib', type=int, default=1536)
    args = parser.parse_args()
    root = ROOT
    if args.variant == 'os4-official' and 'HYPEROS_AVD_WORKSPACE' not in os.environ:
        root = REPO_ROOT / 'work/os4-official'
    common.ROOT = root
    metadata = release_metadata(root, args.variant)
    if args.variant == 'os4-official':
        verify_os4_image(root, metadata)
    version = args.version or ('v0.2.1-a17-hyperos4-hongkong-r2' if args.variant == 'os4-official' else 'v0.1.0')
    fetch_ksu(KSU_FILES)
    paths = {'images/' + name: root / 'images' / name for name in IMAGE_FILES}
    for relative in ('data/misc/modem_simulator', 'data/misc/emulator'):
        paths.update({path.relative_to(root).as_posix(): path for path in (root / 'images' / relative).rglob('*')
                      if path.is_file() and path.name != '.DS_Store' and not path.name.startswith('._')})
    paths.update({'tools/' + name: root / 'tools' / name for name in KSU_FILES})
    if args.variant == 'os4-official':
        paths['config/avd.ini'] = root / 'config/avd.ini'
        from host_color import build as build_host_color
        for path in build_host_color(root):
            paths['tools/' + path.name] = path
    if metadata['format'] == 3:
        paths.update({'tools/xiaomi-camera/' + path.name: path for path in (root / 'tools/xiaomi-camera').iterdir() if path.is_file()})
        paths.update({'runtime/' + relative: path for relative, path in runtime_files().items()})
    # Always create a new blank template; never trust or read personal avd/ data.
    temporary = root / 'work/release-templates' / version / 'userdata.img'
    if temporary.exists():
        raise RuntimeError('A release template already exists; choose a new revision.')
    paths['images/userdata.img'] = create_userdata(temporary)
    directory = REPO_ROOT / 'releases' / version
    write_bundle(directory, version, metadata, paths, args.part_mib)


if __name__ == '__main__':
    main()
