"""Keep the optional allocator bridge portable, guarded and module-owned."""
from contextlib import ExitStack
import hashlib
import json
import shlex
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import apply_camera_fix as camera
from patch_outcome import UnsupportedPatch


class ParrotModuleTests(unittest.TestCase):
    def fixture(self, temporary):
        base = Path(temporary)
        source = base / 'native/camera_aion_cpu.c'
        source.parent.mkdir()
        source.write_bytes(b'audited source fixture')
        folder = base / 'tools/parrot-camera'
        folder.mkdir(parents=True)
        payload = folder / 'lib_aion_buffer.so'
        payload.write_bytes(b'audited allocator fixture')
        source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        payload_hash = hashlib.sha256(payload.read_bytes()).hexdigest()
        receipt = {'revision': 1, 'source_sha256': source_hash,
                   'files': {'lib_aion_buffer.so': payload_hash}}
        (folder / 'receipt.json').write_text(json.dumps(receipt))
        stack = ExitStack()
        stack.enter_context(patch.object(camera, 'ROOT', base))
        stack.enter_context(patch.object(camera, 'REPO_ROOT', base))
        stack.enter_context(patch.object(camera, 'BRIDGE_SOURCE_SHA256', source_hash))
        stack.enter_context(patch.object(camera, 'BRIDGE_SHA256', payload_hash))
        return base, folder, payload, stack

    def test_release_prebuilt_works_without_ndk_or_guest_writes(self):
        with tempfile.TemporaryDirectory() as temporary:
            base, folder, payload, stack = self.fixture(temporary)
            destination = base / 'candidate.so'
            with stack, patch.object(camera.subprocess, 'run') as compile_call:
                camera.build_payload({}, destination)
                self.assertEqual(destination.read_bytes(), payload.read_bytes())
                compile_call.assert_not_called()
                self.assertEqual(camera.prepare_prebuilt(base, 'missing SDK'), folder)
                compile_call.assert_not_called()
                self.assertFalse(list(folder.parent.glob('.parrot-prebuilt-*')))

    def test_corrupt_prebuilt_or_source_never_falls_back_to_compiler(self):
        for change in ('source', 'payload', 'receipt', 'missing', 'alias'):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temporary:
                base, folder, payload, stack = self.fixture(temporary)
                if change == 'source':
                    (base / 'native/camera_aion_cpu.c').write_bytes(b'future source')
                elif change == 'payload':
                    payload.write_bytes(b'unknown allocator')
                elif change == 'receipt':
                    (folder / 'receipt.json').write_text('{')
                elif change == 'missing':
                    payload.unlink()
                elif change == 'alias':
                    actual = base / 'actual.so'
                    payload.rename(actual)
                    payload.symlink_to(actual)
                with stack, patch.object(camera.subprocess, 'run') as compile_call, \
                        self.assertRaises(RuntimeError):
                    camera.build_payload({}, base / 'candidate.so')
                compile_call.assert_not_called()
                self.assertFalse((base / 'candidate.so').exists())

    def test_disabled_or_pending_choice_precedes_apk_lookup(self):
        for flag in ('disable', 'remove', 'pending'):
            transport = Mock(return_value=flag)
            fake = SimpleNamespace(install=Mock(), remove=Mock())
            with patch.object(camera, 'official'), patch.object(camera, 'root', transport), \
                    patch.dict(sys.modules, {'app_bridge_module': fake}):
                result = camera.install({'name': 'Renamed_AVD'})
            self.assertEqual(result['lifecycle'], [flag])
            self.assertEqual(transport.call_count, 1)
            fake.install.assert_not_called()

    def test_same_apk_reinstallation_does_not_pin_module_to_old_path(self):
        for suffix in ('first', 'reinstalled'):
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                apk = '/data/app/~~a/com.google.android.GoogleCamera.parrot-' + suffix + '/base.apk'
                def transport(_config, command):
                    if 'for flag in disable remove' in command:
                        return ''
                    if command == 'pm path ' + camera.PACKAGE:
                        return 'package:' + apk
                    if command.startswith('sha256sum /system/'):
                        return camera.RUNTIME_SHA256 + ' runtime'
                    if command.startswith('sha256sum ') and shlex.split(command)[1].startswith('/data/'):
                        return camera.APK_SHA256 + ' apk'
                    raise AssertionError('Unexpected guest mutation: ' + command)
                payload = base / 'bridge.so'
                payload.write_bytes(b'fixture')
                fake = SimpleNamespace(install=Mock(return_value={'pending': True}), remove=Mock())
                with patch.object(camera, 'ROOT', base), patch.object(camera, 'official'), \
                        patch.object(camera, 'root', side_effect=transport), \
                        patch.object(camera, 'build_payload', return_value=payload), \
                        patch.dict(sys.modules, {'app_bridge_module': fake}):
                    self.assertEqual(camera.install({}), {'pending': True})
                options = fake.install.call_args.kwargs
                self.assertNotIn(apk, json.dumps(options['profiles']))
                self.assertEqual(options['profiles'], camera.bridge_profiles())
                self.assertEqual(json.loads((base / 'local/camera-fix.json').read_text())['revision'], 3)

    def test_unknown_apk_is_explicit_untouched_outcome(self):
        transport = Mock(side_effect=['', 'package:/data/app/com.google.android.GoogleCamera.parrot-a/base.apk',
                                      '0' * 64 + ' apk'])
        fake = SimpleNamespace(install=Mock(), remove=Mock())
        with patch.object(camera, 'official'), patch.object(camera, 'root', transport), \
                patch.dict(sys.modules, {'app_bridge_module': fake}), self.assertRaises(UnsupportedPatch):
            camera.install({})
        fake.install.assert_not_called()

    def test_remove_after_uninstall_marks_only_module(self):
        fake = SimpleNamespace(install=Mock(), remove=Mock(return_value={'removed': True}))
        with patch.object(camera, 'official'), patch.object(camera, 'root', return_value='') as transport, \
                patch.dict(sys.modules, {'app_bridge_module': fake}):
            self.assertEqual(camera.install({}, remove=True), {'removed': True})
        self.assertEqual(transport.call_args.args[-1], 'pm path ' + camera.PACKAGE)
        fake.remove.assert_called_once_with({}, camera.MODULE_ID,
            profiles=camera.bridge_profiles(), name=camera.MODULE_NAME,
            description=camera.MODULE_DESCRIPTION, package=camera.PACKAGE)


if __name__ == '__main__':
    unittest.main()
