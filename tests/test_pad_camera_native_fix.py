"""Guard and script checks for the isolated Pad camera native installer."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import apply_pad_camera_native_fix as camera


class PadCameraNativeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='pad-camera-install-test-')
        self.folder = Path(self.temporary.name)
        self.calls = []
        self.properties = dict(camera.PROPERTIES)
        self.hashes = {camera.APK: camera.APK_HASH, camera.CPP: camera.CPP_HASH,
                       camera.RUNTIME: camera.RUNTIME_HASH, camera.PROVIDER: camera.PROVIDER_BEFORE,
                       camera.HWL: camera.HWL_BEFORE}
        self.package = 'package:' + camera.APK
        self.native = ''
        self.existing = False
        self.disabled = False
        self.saved = camera.manifest()
        self.config = {'name': 'Custom_Pad', 'port': 5582, 'sdk': '/unused-sdk'}
        self.root_patch = patch.object(camera, 'ROOT', self.folder)
        self.root_patch.start()

    def tearDown(self):
        self.root_patch.stop()
        self.temporary.cleanup()

    def fake_root(self, config, command):
        self.assertEqual(config, self.config)
        self.calls.append(command)
        if command.startswith('getprop '):
            return self.properties[command.split()[1]]
        if command == 'pm path com.android.camera':
            return self.package
        if command.startswith('if [ -e ' + camera.MODULE + ' ]'):
            return 'yes' if self.existing else ''
        if command == 'cat ' + camera.MODULE + '/manifest.json':
            return json.dumps(self.saved)
        if command.startswith('if [ -e ' + camera.MODULE + '/disable'):
            return 'yes' if self.disabled else ''
        if command.startswith('sha256sum ' + camera.MODULE + '/'):
            name = command.rsplit('/', 1)[1]
            p = self.folder / 'work/pad-camera-native/script-check' / name
            return hashlib.sha256(p.read_bytes()).hexdigest() + '  ' + name
        if command.startswith('sh ' + camera.MODULE + '/post-fs-data.sh'):
            return ''
        if 'sha256sum' in command:
            if command.endswith(' sh ' + camera.YUV):
                return self.native
            path = command.rsplit(' ', 1)[1]
            return self.hashes[path] + '  ' + path
        raise AssertionError('Unexpected root command: ' + command)

    def install(self):
        with patch.object(camera, 'official') as ownership, patch.object(camera, 'root', self.fake_root):
            result = camera.install(self.config)
            ownership.assert_called_once_with(self.config, sources=(camera.SOURCE,))
            return result

    def assert_no_guest_write(self):
        self.assertFalse(any(command.startswith(('mkdir', 'cp ', 'mv ', 'sh ', 'am ', 'settings '))
                             for command in self.calls))

    def test_unknown_framework_refused_before_mutation(self):
        self.hashes[camera.RUNTIME] = '0' * 64
        with self.assertRaisesRegex(RuntimeError, 'native input'):
            self.install()
        self.assert_no_guest_write()
        self.assertFalse((self.folder / 'work').exists())

    def test_unknown_vendor_inputs_are_refused_before_mutation(self):
        for target in (camera.CPP, camera.PROVIDER, camera.HWL, camera.APK):
            with self.subTest(target=target):
                previous = self.hashes[target]
                self.hashes[target] = '0' * 64
                with self.assertRaisesRegex(RuntimeError, 'native input'):
                    self.install()
                self.assert_no_guest_write()
                self.hashes[target] = previous

    def test_wrong_source_stops_before_any_guest_command(self):
        with patch.object(camera, 'official', side_effect=RuntimeError('wrong source')), \
                patch.object(camera, 'root', self.fake_root):
            with self.assertRaisesRegex(RuntimeError, 'wrong source'):
                camera.install(self.config)
        self.assertEqual(self.calls, [])

    def test_unknown_update_and_private_native_are_refused(self):
        self.package += '.updated'
        with self.assertRaisesRegex(RuntimeError, 'camera update'):
            self.install()
        self.assert_no_guest_write()
        self.package = 'package:' + camera.APK
        self.native = 'f' * 64 + '  ' + camera.YUV
        with self.assertRaisesRegex(RuntimeError, 'native overlay'):
            self.install()
        self.assert_no_guest_write()

    def test_disabled_owned_module_remains_disabled(self):
        self.existing = self.disabled = True
        self.assertEqual(self.install(), camera.manifest())
        self.assert_no_guest_write()
        self.assertFalse((self.folder / 'work').exists())

    def test_unknown_existing_manifest_is_never_adopted(self):
        self.existing = True
        self.saved['targets'][0]['after'] = '0' * 64
        with self.assertRaisesRegex(RuntimeError, 'Unknown Pad camera manifest'):
            self.install()
        self.assert_no_guest_write()

    def test_verified_prototype_hashes_are_accepted_for_existing_module(self):
        self.existing = True
        self.hashes[camera.HWL] = camera.HWL_AFTER
        self.hashes[camera.PROVIDER] = camera.PROVIDER_AFTER
        self.native = camera.YUV_AFTER + '  ' + camera.YUV
        self.assertEqual(self.install(), camera.manifest())
        self.assertEqual(self.calls[-1], 'sh ' + camera.MODULE + '/post-fs-data.sh')

    def test_unknown_runtime_never_uses_private_offsets(self):
        with self.assertRaisesRegex(RuntimeError, 'Unsupported Pad ImageReader'):
            camera.validate_runtime(b'not an ELF')

    def test_manifest_rejects_phone_and_changes(self):
        for field in ('source', 'apk_sha256', 'runtime_sha256', 'embedded_yuv_sha256',
                      'yuv_sha256', 'plane_offset', 'recovery'):
            changed = copy.deepcopy(camera.manifest())
            changed[field] = 'unknown'
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                camera.validate_manifest(changed)

    def test_generated_shell_scripts_have_no_placeholders_and_parse(self):
        camera.write_scripts(self.folder)
        for name in ('post-fs-data.sh', 'service.sh'):
            subprocess.run(['sh', '-n', str(self.folder / name)], check=True)
        source = (self.folder / 'post-fs-data.sh').read_text()
        self.assertNotRegex(source, r'@[A-Z_]+@')
        self.assertLess(source.index('# Preflight every'), source.index('mount -o bind'))
        self.assertIn(camera.RUNTIME_HASH, source)
        self.assertIn(camera.PROVIDER_BEFORE + '|' + camera.PROVIDER_AFTER, source)
        self.assertIn(hashlib.sha256(camera.targets_text().encode()).hexdigest(), source)
        preflight = source.split('changed=0', 1)[0]
        for name in ('provider', 'hwl.so', 'app/MiuiCamera.apk',
                     'app/lib/arm64/libcamera_yuv_jni.so'):
            self.assertIn('nsenter -t "$pid" -m -- "$BB" sha256sum "$MODDIR/' + name + '"',
                          preflight)
        self.assertIn('flags=ro,suid,exec', source)
        self.assertNotIn('setenforce', source)
        self.assertNotIn('pm clear', source)

    def test_early_boot_without_zygote_still_selects_init(self):
        prefix = camera.namespace_preflight().split('# Preflight every', 1)[0]
        script = 'set -e\ngetprop() { return 0; }\npidof() { return 1; }\n' + prefix
        script += '\nprintf "%s" "$pids"\n'
        result = subprocess.run(['sh', '-c', script], text=True, check=True, capture_output=True)
        self.assertEqual(result.stdout.strip(), '1')

    def test_unknown_prebuilt_receipt_never_copies_payloads(self):
        cache = self.folder / 'tools/pad-camera-native'
        cache.mkdir(parents=True)
        (cache / 'receipt.json').write_text(json.dumps({'sources': {}, 'files': {}}))
        destination = self.folder / 'output'
        with self.assertRaisesRegex(RuntimeError, 'Unknown precompiled'):
            camera.build('/unused-sdk', destination)
        self.assertFalse(destination.exists())

    def publication_inputs(self):
        for name in ('provider', 'hwl.so', 'yuv.so', 'manifest.json', 'post-fs-data.sh',
                     'service.sh', 'targets.conf', 'module.prop'):
            (self.folder / name).write_text('temporary test payload\n')

    def test_publication_uses_unbound_unique_uploads_and_valid_shell(self):
        self.publication_inputs()
        commands = []
        def capture(config, command):
            commands.append(command)
            subprocess.run(['sh', '-n'], input=command, text=True, check=True)
            return ''
        with patch.object(camera, 'root', capture), patch.object(camera, 'adb') as transport:
            camera.publish(self.config, self.folder)
        uploads = [call.args[-1] for call in transport.call_args_list]
        self.assertEqual(len(uploads), 8)
        self.assertTrue(all('/hyperos-pad-camera-install-' in path for path in uploads))
        self.assertNotIn('/data/local/tmp/hyperos-pad-camera-provider', uploads)
        self.assertIn('mv /data/adb/hyperos-pad-camera-stage-', commands[-1])
        self.assertNotIn('rm -rf ' + camera.MODULE, '\n'.join(commands))

    def test_failed_upload_cleanup_never_removes_published_module(self):
        self.publication_inputs()
        commands = []
        with patch.object(camera, 'root', side_effect=lambda config, cmd: commands.append(cmd) or ''), \
                patch.object(camera, 'adb', side_effect=subprocess.CalledProcessError(1, ['adb'])):
            with self.assertRaises(subprocess.CalledProcessError):
                camera.publish(self.config, self.folder)
        removals = [cmd for cmd in commands if 'rm ' in cmd]
        self.assertEqual(len(removals), 2)
        self.assertIn('rm -rf /data/adb/hyperos-pad-camera-stage-', removals[0])
        self.assertIn('rm -f /data/local/tmp/hyperos-pad-camera-install-', removals[1])
        self.assertNotIn(camera.MODULE, '\n'.join(removals))

    def test_prebuilt_bytes_must_match_all_pinned_checksums(self):
        cache = self.folder / 'tools/pad-camera-native'
        cache.mkdir(parents=True)
        expected = {**camera.PAYLOADS, 'manifest.json': hashlib.sha256(
            (json.dumps(camera.manifest(), indent=2) + '\n').encode()).hexdigest()}
        (cache / 'receipt.json').write_text(json.dumps({'sources': camera.SOURCES, 'files': expected}))
        for name in expected:
            (cache / name).write_bytes(b'corrupt payload')
        destination = self.folder / 'output'
        with self.assertRaisesRegex(RuntimeError, 'payload changed'):
            camera.build('/unused-sdk', destination)
        self.assertFalse(destination.exists())


if __name__ == '__main__':
    unittest.main()
