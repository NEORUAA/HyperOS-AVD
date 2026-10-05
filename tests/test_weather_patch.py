"""Guard the tablet Weather workload and its private driver boundary."""
import copy
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch as mock_patch
import zipfile

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'scripts'))
from patch_weather import (ANGLE, APK_SHA256, EMPTY, LIBRARIES, PAD_APK,
                           PAD_APK_SHA256, PAD_LIBRARIES, PAD_NATIVE,
                           build_bridge, patch, profile)
import patch_weather
from apply_weather_fix import BOOT_SCRIPT, boot_script, valid_target, validate_pad_manifest
import apply_weather_fix

SOURCE = (REPO / 'work/os4-pad/work/official-tree/extract-product-c2fede3d9b16b1b5'
          '/data-app/MIUIWeather/MIUIWeather.apk')
INSTALLED_APK = ('/data/app/~~WQcq3eZxMKOjKsLtmhNePA=='
                 '/com.miui.weather2-wUtOk0ciwBrI8za2GkoANQ==/base.apk')


class WeatherPrebuiltTests(unittest.TestCase):
    def fixture(self, temporary, *, receipt=True, bridge=True):
        repository = Path(temporary) / 'runtime renamed'
        source = repository / 'native/weather_angle.c'
        source.parent.mkdir(parents=True)
        source.write_bytes(b'verified bridge source fixture')
        workspace = Path(temporary) / 'custom tablet instance'
        prebuilt = workspace / 'tools/weather-angle'
        destination = workspace / 'work/custom weather target'
        prebuilt.mkdir(parents=True)
        destination.mkdir(parents=True)
        angle_data = {name: ('verified ' + name).encode() for name in ANGLE}
        hashes = {name: hashlib.sha256(data).hexdigest() for name, data in angle_data.items()}
        bridge_data = b'verified bridge binary fixture'
        with ExitStack() as stack:
            stack.enter_context(mock_patch.object(patch_weather, 'REPO_ROOT', repository))
            stack.enter_context(mock_patch.object(patch_weather, 'ROOT', workspace))
            stack.enter_context(mock_patch.object(patch_weather, 'ANGLE', hashes))
            stack.enter_context(mock_patch.object(patch_weather, 'BRIDGE_SOURCE_SHA256',
                                                  hashlib.sha256(source.read_bytes()).hexdigest()))
            stack.enter_context(mock_patch.object(patch_weather, 'BRIDGE_SHA256',
                                                  hashlib.sha256(bridge_data).hexdigest()))
            for name, data in angle_data.items():
                (prebuilt / name).write_bytes(data)
                (destination / name).write_bytes(data)
            if bridge:
                (prebuilt / 'libhgl.so').write_bytes(bridge_data)
            metadata = patch_weather.bridge_prebuilt_receipt()
            if receipt:
                (prebuilt / 'receipt.json').write_text(json.dumps(metadata, indent=2) + '\n')
        return repository, workspace, prebuilt, destination, source, hashes, bridge_data, metadata

    def environment(self, repository, workspace, hashes, source, bridge_data):
        stack = ExitStack()
        stack.enter_context(mock_patch.object(patch_weather, 'REPO_ROOT', repository))
        stack.enter_context(mock_patch.object(patch_weather, 'ROOT', workspace))
        stack.enter_context(mock_patch.object(patch_weather, 'ANGLE', hashes))
        stack.enter_context(mock_patch.object(patch_weather, 'BRIDGE_SOURCE_SHA256',
                                              hashlib.sha256(b'verified bridge source fixture').hexdigest()))
        stack.enter_context(mock_patch.object(patch_weather, 'BRIDGE_SHA256',
                                              hashlib.sha256(bridge_data).hexdigest()))
        return stack

    def test_verified_release_has_no_sdk_or_ndk_dependency_and_copies_atomically(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, workspace, prebuilt, destination, source, hashes, data, metadata = self.fixture(temporary)
            old = destination / 'libhgl.so'
            old.write_bytes(b'previous binary')
            with self.environment(repository, workspace, hashes, source, data), \
                    mock_patch.object(patch_weather.subprocess, 'run') as compile_command, \
                    mock_patch.object(Path, 'glob', side_effect=AssertionError('NDK lookup is forbidden')):
                self.assertEqual(patch_weather.verify_bridge_prebuilt(prebuilt), metadata)
                with old.open('rb') as prior_inode:
                    result = build_bridge({}, destination)
                    self.assertEqual(prior_inode.read(), b'previous binary')
                self.assertEqual(result, old)
                self.assertEqual(result.read_bytes(), data)
                self.assertEqual((prebuilt / 'libhgl.so').read_bytes(), data)
                self.assertFalse(any('.stage-' in path.name for path in destination.iterdir()))
                compile_command.assert_not_called()

    def test_same_directory_reuses_verified_library_without_self_copy(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, workspace, prebuilt, destination, source, hashes, data, metadata = self.fixture(temporary)
            with self.environment(repository, workspace, hashes, source, data), \
                    mock_patch.object(patch_weather.subprocess, 'run') as compile_command, \
                    mock_patch.object(patch_weather.shutil, 'copyfileobj') as copy_command:
                self.assertEqual(build_bridge({}, prebuilt), prebuilt / 'libhgl.so')
                copy_command.assert_not_called()
                compile_command.assert_not_called()

    def test_invalid_source_payload_and_receipt_fail_before_copy_or_ndk_fallback(self):
        mutations = ('source', *ANGLE, 'libhgl.so', 'receipt', 'receipt-missing',
                     'payload-missing', 'receipt-malformed', 'receipt-extra', 'revision-bool')
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                repository, workspace, prebuilt, destination, source, hashes, data, metadata = self.fixture(temporary)
                (destination / 'libhgl.so').write_bytes(b'existing destination')
                if mutation == 'source':
                    source.write_bytes(b'changed source')
                elif mutation in (*ANGLE, 'libhgl.so'):
                    (prebuilt / mutation).write_bytes(b'changed payload')
                elif mutation == 'receipt-missing':
                    (prebuilt / 'receipt.json').unlink()
                elif mutation == 'payload-missing':
                    (prebuilt / 'libhgl.so').unlink()
                elif mutation == 'receipt-malformed':
                    (prebuilt / 'receipt.json').write_text('{invalid')
                else:
                    changed = copy.deepcopy(metadata)
                    if mutation == 'receipt-extra':
                        changed['unverified'] = 'extra field'
                    elif mutation == 'revision-bool':
                        changed['revision'] = True
                    else:
                        changed['files']['libhgl.so'] = '0' * 64
                    (prebuilt / 'receipt.json').write_text(json.dumps(changed))
                with self.environment(repository, workspace, hashes, source, data), \
                        mock_patch.object(patch_weather.subprocess, 'run') as compile_command, \
                        mock_patch.object(patch_weather.shutil, 'copyfileobj') as copy_command:
                    with self.assertRaises(RuntimeError):
                        build_bridge({}, destination)
                    compile_command.assert_not_called()
                    copy_command.assert_not_called()
                self.assertEqual((destination / 'libhgl.so').read_bytes(), b'existing destination')

    def test_unverified_guest_angle_still_rejects_an_otherwise_valid_prebuilt(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, workspace, prebuilt, destination, source, hashes, data, metadata = self.fixture(temporary)
            (destination / next(iter(ANGLE))).write_bytes(b'unverified guest ANGLE')
            with self.environment(repository, workspace, hashes, source, data), \
                    mock_patch.object(patch_weather.subprocess, 'run') as compile_command:
                with self.assertRaisesRegex(RuntimeError, 'Unsupported system ANGLE revision'):
                    build_bridge({}, destination)
                compile_command.assert_not_called()

    def test_failed_copy_preserves_previous_destination_and_removes_staging_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, workspace, prebuilt, destination, source, hashes, data, metadata = self.fixture(temporary)
            (destination / 'libhgl.so').write_bytes(b'existing destination')
            def corrupt_copy(original, output):
                output.write(b'changed during copy')
            with self.environment(repository, workspace, hashes, source, data), \
                    mock_patch.object(patch_weather.shutil, 'copyfileobj', side_effect=corrupt_copy):
                with self.assertRaisesRegex(RuntimeError, 'copy verification failed'):
                    build_bridge({}, destination)
            self.assertEqual((destination / 'libhgl.so').read_bytes(), b'existing destination')
            self.assertFalse(any('.stage-' in path.name for path in destination.iterdir()))

    def test_prebuilt_aliases_are_rejected(self):
        for relative in ('receipt.json', 'libhgl.so', *ANGLE):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as temporary:
                repository, workspace, prebuilt, destination, source, hashes, data, metadata = self.fixture(temporary)
                original = prebuilt / relative
                moved = prebuilt / (relative + '.original')
                original.replace(moved)
                original.symlink_to(moved)
                with self.environment(repository, workspace, hashes, source, data):
                    with self.assertRaises(RuntimeError):
                        build_bridge({}, destination)

    def test_valid_angle_only_cache_retains_source_build_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, workspace, prebuilt, destination, source, hashes, data, metadata = self.fixture(
                temporary, receipt=False, bridge=False)
            compiler = workspace / 'sdk/ndk/30.0.16138531/toolchains/llvm/prebuilt/darwin-x86_64/bin/aarch64-linux-android36-clang'
            compiler.parent.mkdir(parents=True)
            compiler.write_text('fixture compiler')
            def compile_fixture(command, **kwargs):
                self.assertEqual(command[0], str(compiler))
                self.assertEqual(command[1], str(source))
                Path(command[-1]).write_bytes(b'source compiled bridge')
            with self.environment(repository, workspace, hashes, source, data), \
                    mock_patch.object(patch_weather.subprocess, 'run', side_effect=compile_fixture) as compile_command:
                result = build_bridge({'sdk': str(workspace / 'sdk')}, destination)
                self.assertEqual(result.read_bytes(), b'source compiled bridge')
                compile_command.assert_called_once()


class WeatherGuardTests(unittest.TestCase):
    def manifest(self, apk=PAD_APK):
        native = apply_weather_fix.pad_native(apk)
        pairs = {**PAD_LIBRARIES, **{name: (EMPTY, checksum) for name, checksum in ANGLE.items()},
                 'libhgl.so': (EMPTY, '1' * 64)}
        return {'revision': 1, 'apk': apk, 'profile': 'tablet-yingtian',
                'apk_sha256': PAD_APK_SHA256,
                'targets': [{'payload': name, 'target': native + '/' + name,
                             'before': pair[0], 'after': pair[1]} for name, pair in pairs.items()]}

    def install_fixture(self, folder, *, apk=INSTALLED_APK, old=False, disabled=False,
                        wrong_apk=False, wrong_native=False, wrong_property=False, enable=False):
        workspace = Path(folder)
        (workspace / 'local').mkdir()
        (workspace / 'local/build.json').write_text(json.dumps({'source': 'official-yingtian-ota'}))
        original = workspace / 'weather.apk'
        raw = {name: ('original ' + name).encode() for name in PAD_LIBRARIES}
        fixed = {name: ('fixed ' + name).encode() for name in PAD_LIBRARIES}
        pairs = {name: tuple(hashlib.sha256(data).hexdigest() for data in (raw[name], fixed[name]))
                 for name in raw}
        angle_data = {name: ('private ' + name).encode() for name in ANGLE}
        angles = {name: hashlib.sha256(data).hexdigest() for name, data in angle_data.items()}
        with zipfile.ZipFile(original, 'w') as archive:
            for name, data in raw.items():
                archive.writestr('lib/arm64-v8a/' + name, data)
            archive.writestr('assets/original', b'untouched APK bytes')
        original_bytes = original.read_bytes()
        apk_hash = hashlib.sha256(original_bytes).hexdigest()
        selected = {'id': 'tablet-yingtian', 'apk_sha256': apk_hash, 'libraries': pairs}
        previous = {'revision': 1, 'apk': PAD_APK, 'profile': selected['id'], 'apk_sha256': apk_hash,
                    'targets': [{'payload': name, 'target': PAD_NATIVE + '/' + name,
                                 'before': pair[0], 'after': pair[1]} for name, pair in
                                {**pairs, **{name: (EMPTY, checksum) for name, checksum in angles.items()},
                                 'libhgl.so': (EMPTY, '1' * 64)}.items()]}
        commands, transfers = [], []
        native = str(Path(apk).parent / 'lib/arm64') if apk != PAD_APK else PAD_NATIVE

        def guest(config, command, **kwargs):
            commands.append(command)
            if command == 'pm path com.miui.weather2':
                return 'package:' + apk
            properties = {'id -u': '0', 'getprop sys.boot_completed': '1',
                          'getprop ro.boot.hardware': 'ranchu', 'getprop ro.product.device': 'yingtian',
                          'getprop ro.mi.os.version.incremental': apply_weather_fix.PAD_VERSION}
            if command in properties:
                return 'unknown' if wrong_property and command == 'getprop ro.product.device' else properties[command]
            if command == 'sha256sum ' + shlex.quote(apk):
                return ('0' * 64 if wrong_apk else apk_hash) + '  ' + apk
            if command.startswith('if [ -f ' + apply_weather_fix.MODULE + '/manifest.json'):
                return json.dumps(previous) if old else ''
            if command.startswith('if [ -f ' + apply_weather_fix.MODULE + '/disable'):
                return 'yes' if disabled else ''
            if command.startswith('if [ -f ' + shlex.quote(native + '/libmglnative.so')):
                return 'yes'
            if command.startswith('if [ -e '):
                path = next(shlex.split(line)[1] for line in command.splitlines()
                            if line.startswith('sha256sum '))
                name = Path(path).name
                if name in pairs:
                    return ('0' * 64 if wrong_native else pairs[name][0]) + '  ' + path
                return ''
            if command.startswith('sha256sum /data/adb/hyperos-weather-stage-'):
                return apply_weather_fix.sha256(workspace / 'work/weather-angle-fix' / Path(command).name) + '  staged'
            return ''

        def transfer(config, action, *arguments, **kwargs):
            transfers.append((action, arguments))
            if action == 'pull':
                Path(arguments[1]).write_bytes(original_bytes if arguments[0] == apk else angle_data[Path(arguments[0]).name])

        def make_bridge(config, folder):
            path = folder / 'libhgl.so'
            path.write_bytes(b'fixture bridge')
            return path

        with mock_patch.object(apply_weather_fix, 'ROOT', workspace), \
                mock_patch.object(apply_weather_fix, 'official') as ownership, \
                mock_patch.object(apply_weather_fix, 'profile', return_value=selected), \
                mock_patch.object(apply_weather_fix, 'ANGLE', angles), \
                mock_patch.object(apply_weather_fix, 'root', side_effect=guest), \
                mock_patch.object(apply_weather_fix, 'adb', side_effect=transfer), \
                mock_patch.object(apply_weather_fix, 'patch', side_effect=lambda name, data, **kwargs: fixed[name]), \
                mock_patch.object(apply_weather_fix, 'build_bridge', side_effect=make_bridge) as build, \
                mock_patch.object(apply_weather_fix, 'detach') as detach:
            if wrong_apk or wrong_native or wrong_property or apk.endswith('/other.apk'):
                with self.assertRaises(RuntimeError):
                    apply_weather_fix.install({'sdk': '/unused'}, enable=enable,
                                              sources=('official-yingtian-ota',))
                self.assertFalse(any(action == 'push' for action, _ in transfers))
                self.assertFalse(any(command.startswith(('mkdir ', 'rm ', 'cp ', 'mv ')) for command in commands))
                result = None
            else:
                result = apply_weather_fix.install({'sdk': '/unused'}, enable=enable,
                                                  sources=('official-yingtian-ota',))
                self.assertEqual(detach.call_count, int(old and not (disabled and not enable)))
            ownership.assert_called_once_with({'sdk': '/unused'}, sources=('official-yingtian-ota',))
            if disabled and not enable:
                build.assert_not_called()
                self.assertFalse(any(action == 'push' for action, _ in transfers))
        self.assertEqual(original.read_bytes(), original_bytes)
        self.assertFalse(any(action == 'push' and str(arguments[0]).endswith('.apk')
                             for action, arguments in transfers))
        return result, commands

    def test_pad_signed_reinstall_uses_only_its_extracted_native_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            manifest, commands = self.install_fixture(temporary)
            self.assertEqual(manifest['apk'], INSTALLED_APK)
            native = apply_weather_fix.pad_native(INSTALLED_APK)
            self.assertTrue(all(item['target'] == native + '/' + item['payload'] for item in manifest['targets']))
            self.assertTrue(any('getprop sys.boot_completed' == command for command in commands))
            self.assertTrue(all(item['target'].endswith('.so') for item in manifest['targets']))

    def test_factory_manifest_migrates_and_disabled_choice_is_preserved(self):
        for disabled, enable in ((False, False), (True, False), (True, True)):
            with self.subTest(disabled=disabled, enable=enable), tempfile.TemporaryDirectory() as temporary:
                manifest, _ = self.install_fixture(temporary, old=True, disabled=disabled, enable=enable)
                self.assertEqual(manifest['apk'], PAD_APK if disabled and not enable else INSTALLED_APK)

    def test_installed_apk_native_and_properties_are_guarded_before_guest_writes(self):
        for arguments in ({'wrong_apk': True}, {'wrong_native': True}, {'wrong_property': True},
                          {'apk': INSTALLED_APK.removesuffix('base.apk') + 'other.apk'}):
            with self.subTest(arguments=arguments), tempfile.TemporaryDirectory() as temporary:
                self.install_fixture(temporary, **arguments)

    def test_pad_layout_resolver_and_manifest_reject_cross_package_targets(self):
        legacy = '/data/app/com.miui.weather2-Ab_01==/base.apk'
        for apk in (PAD_APK, INSTALLED_APK, legacy):
            manifest = self.manifest(apk)
            validate_pad_manifest(manifest, profile('official-yingtian-ota'))
            self.assertEqual(apply_weather_fix.pad_native(apk), PAD_NATIVE if apk == PAD_APK else str(Path(apk).parent / 'lib/arm64'))
        for apk in (None, '/data/app/../com.miui.weather2-a/base.apk',
                    '/data/app/~~a/com.other-a/base.apk', '/data/app/~~a/com.miui.weather2-a/split.apk',
                    '/data/app/~~a/extra/com.miui.weather2-a/base.apk',
                    '/data/app/~~a/com.miui.weather2-a/base.apk\n',
                    '/data/app/~~a/com.miui.weather2-$(id)/base.apk'):
            with self.subTest(apk=apk), self.assertRaises(RuntimeError):
                apply_weather_fix.pad_native(apk)
        changed = self.manifest(INSTALLED_APK)
        changed['targets'][0]['target'] = PAD_NATIVE + '/' + changed['targets'][0]['payload']
        with self.assertRaises(RuntimeError):
            validate_pad_manifest(changed, profile('official-yingtian-ota'))

    def test_unknown_profile_and_workload_are_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'Unsupported Weather firmware'):
            profile('unknown-ota')
        for libraries in (LIBRARIES, PAD_LIBRARIES):
            with self.assertRaisesRegex(RuntimeError, 'Unsupported Weather MGL'):
                patch('libhyper_opengl.so', b'unknown update', libraries=libraries)
        checksum = hashlib.sha256(b'unknown update').hexdigest()
        with self.assertRaisesRegex(RuntimeError, 'Unsupported Weather MGL library profile'):
            patch('libhyper_opengl.so', b'unknown update', libraries={'libhyper_opengl.so': (checksum, checksum)})
        self.assertEqual(profile('official-hongkong-ota')['apk_sha256'], APK_SHA256)
        self.assertIs(profile('official-hongkong-ota')['libraries'], LIBRARIES)
        self.assertEqual(boot_script(profile('official-hongkong-ota'), {}), BOOT_SCRIPT)

    def test_private_manifest_rejects_paths_hashes_and_duplicates(self):
        selected = profile('official-yingtian-ota')
        manifest = self.manifest()
        validate_pad_manifest(manifest, selected)
        for key, value in [('target', '/system/lib64/libEGL_angle.so'),
                           ('after', '0' * 64), ('before', '0' * 64)]:
            changed = copy.deepcopy(manifest)
            changed['targets'][0][key] = value
            with self.assertRaises(RuntimeError):
                validate_pad_manifest(changed, selected)
        changed = copy.deepcopy(manifest)
        changed['targets'][-1] = changed['targets'][0]
        with self.assertRaises(RuntimeError):
            validate_pad_manifest(changed, selected)
        for value in ('0' * 64, APK_SHA256):
            with self.assertRaises(RuntimeError):
                validate_pad_manifest({**manifest, 'apk_sha256': value}, selected)

    def test_generated_shell_pins_only_private_targets(self):
        subprocess.run(['sh', '-n'], input=BOOT_SCRIPT, text=True, check=True, capture_output=True)
        for apk in (PAD_APK, INSTALLED_APK):
            with self.subTest(apk=apk):
                manifest = self.manifest(apk)
                script = boot_script(profile('official-yingtian-ota'), manifest)
                subprocess.run(['sh', '-n'], input=script, text=True, check=True, capture_output=True)
                self.assertIn(PAD_APK_SHA256, script)
                self.assertNotIn(APK_SHA256, script)
                self.assertNotIn('/data/app/*/lib/arm64/libhgl.so', script)
                self.assertIn('ln "$temporary" "$1"', script)
                self.assertNotIn('touch "$1"', script)
                self.assertIn('ro.mi.os.version.incremental', script)
                self.assertIn(apply_weather_fix.PAD_VERSION, script)
                self.assertLess(script.index('while [ "$(getprop sys.boot_completed)"'), script.index('mount -o bind'))
                self.assertIn('nsenter -t "$pid" -m -- "$BB" sha256sum "$apk"', script)
                self.assertLess(script.index('source_hash=$(nsenter'), script.index('[ "$actual" = "$after" ] && continue'))
                self.assertLess(script.index('(set -C; : > "$temporary")'), script.index('trap '))
                self.assertIn('[ ! -L "$directory" ]', script)
                for target in manifest['targets']:
                    self.assertIn('|'.join(target[key] for key in ('payload', 'target', 'before', 'after')), script)
                    self.assertTrue(valid_target(target['target']))
        for target in ('/data/app-lib/Other/arm64/libhgl.so', PAD_NATIVE + '/unknown.so',
                       '/system/lib64/libEGL_angle.so', '/data/app/../escape', '/data/app/a\'b'):
            self.assertFalse(valid_target(target))

    def test_other_angle_revisions_are_rejected_before_compilation(self):
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            for library in ANGLE:
                (folder / library).write_bytes(b'different ANGLE revision')
            with self.assertRaisesRegex(RuntimeError, 'Unsupported system ANGLE revision'):
                build_bridge({'sdk': '/unused'}, folder)

    @unittest.skipUnless(SOURCE.is_file(), 'Verified private tablet firmware is not bundled')
    def test_factory_native_patch_is_equal_size_and_dependency_only(self):
        apk = SOURCE.read_bytes()
        self.assertEqual(hashlib.sha256(apk).hexdigest(), PAD_APK_SHA256)
        with zipfile.ZipFile(SOURCE) as archive:
            for name, (before, after) in PAD_LIBRARIES.items():
                original = archive.read('lib/arm64-v8a/' + name)
                self.assertEqual(hashlib.sha256(original).hexdigest(), before)
                fixed = patch(name, original, libraries=PAD_LIBRARIES)
                self.assertEqual(hashlib.sha256(fixed).hexdigest(), after)
                self.assertEqual(len(fixed), len(original))
                allowed = set()
                for dependency in (b'libEGL.so\0', b'libGLESv3.so\0'):
                    self.assertEqual(original.count(dependency), 1)
                    offset = original.index(dependency)
                    allowed.update(range(offset, offset + len(dependency)))
                changed = {i for i, (a, b) in enumerate(zip(original, fixed)) if a != b}
                self.assertTrue(changed)
                self.assertLessEqual(changed, allowed)
                self.assertEqual(patch(name, fixed, libraries=PAD_LIBRARIES), fixed)
                corrupted = bytearray(original)
                corrupted[-1] ^= 1
                with self.assertRaisesRegex(RuntimeError, 'Unsupported Weather MGL'):
                    patch(name, corrupted, libraries=PAD_LIBRARIES)
                if name == 'libhyper_opengl.so':
                    with self.assertRaisesRegex(RuntimeError, 'Unsupported Weather MGL'):
                        patch(name, original)


if __name__ == '__main__':
    unittest.main()
