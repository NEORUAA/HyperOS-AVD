"""Exercise actual lifecycle shell guards and installer preservation boundaries."""
from contextlib import ExitStack
import hashlib
import importlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import module_lifecycle as lifecycle


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.module = self.folder / 'modules/test_module'
        self.pending = self.folder / 'modules_update/test_module'
        self.module.mkdir(parents=True)
        self.module_id = '/data/adb/modules/test_module'

    def translate(self, command):
        return command.replace('/data/adb/modules_update/test_module', str(self.pending)).replace(
            self.module_id, str(self.module))

    def root(self, config, command):
        result = subprocess.run(['sh', '-c', self.translate(command)], capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError('guest shell refused mutation: ' + str(result.returncode))
        return result.stdout.strip()

    def test_all_lifecycle_markers_including_dangling_aliases_win(self):
        for flag in ('disable', 'remove', 'pending'):
            marker = self.pending if flag == 'pending' else self.module / flag
            marker.parent.mkdir(parents=True, exist_ok=True)
            for alias in (False, True):
                if alias:
                    marker.symlink_to(self.folder / 'missing')
                else:
                    marker.touch()
                self.assertEqual(lifecycle.preserved(self.root, {}, self.module_id),
                                 {'preserved': True, 'lifecycle': [flag]})
                if flag != 'disable':
                    self.assertIsNotNone(lifecycle.preserved(self.root, {}, self.module_id, enable=True))
                marker.unlink()

    def test_enable_requires_owned_metadata_and_never_clears_removal(self):
        flag = self.module / 'disable'
        flag.symlink_to(self.folder / 'missing')
        prop = self.module / 'module.prop'
        prop.write_text('id=foreign_module\nauthor=SomeoneElse\n')
        with self.assertRaises(RuntimeError):
            lifecycle.enable_owned(self.root, {}, self.module_id)
        self.assertTrue(flag.is_symlink())
        prop.write_text('id=test_module\nauthor=HyperOS-AVD\n')
        (self.module / 'remove').symlink_to(self.folder / 'missing')
        with self.assertRaises(RuntimeError):
            lifecycle.enable_owned(self.root, {}, self.module_id)
        self.assertTrue(flag.is_symlink())
        (self.module / 'remove').unlink()
        lifecycle.enable_owned(self.root, {}, self.module_id)
        self.assertFalse(flag.is_symlink())

    def test_pending_or_new_flags_block_mutation_and_do_not_touch_payload(self):
        target = self.folder / 'must-not-write'
        for flag in ('disable', 'remove', 'pending'):
            marker = self.pending if flag == 'pending' else self.module / flag
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.symlink_to(self.folder / 'missing')
            with self.assertRaises(RuntimeError):
                self.root({}, lifecycle.mutation_guard(self.module_id) + 'touch ' + str(target))
            self.assertFalse(target.exists())
            self.assertTrue(marker.is_symlink())
            marker.unlink()

    def test_only_exact_legacy_hooks_are_migrated_atomically(self):
        (self.module / 'module.prop').write_text('id=test_module\nauthor=HyperOS-AVD\n')
        hook = self.module / 'service.sh'
        old, new = '#!/bin/sh\nexit 0\n', '#!/bin/sh\n# checked lifecycle\nexit 0\n'
        hook.write_text(old)
        lifecycle.refresh_hooks(self.root, {}, self.module_id, {'service.sh': (old, new)})
        self.assertEqual(hook.read_text(), new)
        lifecycle.refresh_hooks(self.root, {}, self.module_id, {'service.sh': (old, new)})
        hook.write_text(old + '# local hook\n')
        with self.assertRaisesRegex(RuntimeError, 'Unreviewed'):
            lifecycle.refresh_hooks(self.root, {}, self.module_id, {'service.sh': (old, new)})
        self.assertEqual(hook.read_text(), old + '# local hook\n')
        self.assertFalse(any('.lifecycle.' in p.name for p in self.module.iterdir()))

    def test_unknown_second_hook_preserves_first_legacy_hook(self):
        (self.module / 'module.prop').write_text('id=test_module\nauthor=HyperOS-AVD\n')
        old, new = '#!/bin/sh\nexit 0\n', '#!/bin/sh\n# checked lifecycle\nexit 0\n'
        first = self.module / 'post-fs-data.sh'
        second = self.module / 'service.sh'
        first.write_text(old)
        second.write_text(old + '# custom service\n')
        with self.assertRaisesRegex(RuntimeError, 'Unreviewed'):
            lifecycle.refresh_hooks(self.root, {}, self.module_id,
                                    {'post-fs-data.sh': (old, new), 'service.sh': (old, new)})
        self.assertEqual(first.read_text(), old)
        self.assertEqual(second.read_text(), old + '# custom service\n')

    def test_reviewed_multiple_versions_can_be_verified_without_enabling(self):
        (self.module / 'module.prop').write_text('id=test_module\nauthor=HyperOS-AVD\n')
        marker = self.module / 'disable'
        marker.touch()
        old, prior, current = '#!/bin/sh\nexit 0\n', '#!/bin/sh\nexit 1\n', '#!/bin/sh\nexit 2\n'
        service = self.module / 'service.sh'
        service.write_text(prior)
        inode = service.stat().st_ino
        observed = lifecycle.verify_hooks(self.root, {}, self.module_id,
                                          {'service.sh': ((old, prior), current)}, allow_disabled=True)
        self.assertEqual(observed['service.sh'], hashlib.sha256(prior.encode()).hexdigest())
        self.assertEqual(service.stat().st_ino, inode)
        self.assertTrue(marker.exists())
        marker.unlink()
        lifecycle.refresh_hooks(self.root, {}, self.module_id, {'service.sh': ((old, prior), current)})
        self.assertEqual(service.read_text(), current)

    def test_receipt_refresh_checks_inspected_content_and_preserves_payload(self):
        (self.module / 'module.prop').write_text('id=test_module\nauthor=HyperOS-AVD\n')
        receipt = self.module / 'manifest.json'
        payload = self.module / 'payload.so'
        old, current = '{"hook": "old"}\n', '{"hook": "current"}\n'
        receipt.write_text(old)
        payload.write_bytes(b'unchanged payload')
        inode = payload.stat().st_ino
        with self.assertRaises(RuntimeError):
            lifecycle.refresh_receipt(self.root, {}, self.module_id, '{"hook": "unrelated"}', current)
        self.assertEqual(receipt.read_text(), old)
        lifecycle.refresh_receipt(self.root, {}, self.module_id, old.strip(), current)
        self.assertEqual(receipt.read_text(), current)
        self.assertEqual(payload.stat().st_ino, inode)
        self.assertEqual(payload.read_bytes(), b'unchanged payload')
        self.assertFalse(any('.lifecycle.' in path.name for path in self.module.iterdir()))

    def test_removed_during_boot_wait_exits_before_effects(self):
        tools = self.folder / 'tools'
        tools.mkdir()
        flag = self.module / 'remove'
        (tools / 'getprop').write_text('#!/bin/sh\ntouch ' + str(flag) + '\necho 0\n')
        (tools / 'getprop').chmod(0o700)
        target = self.folder / 'must-not-write'
        original = ('#!/bin/sh\nMODDIR=${0%/*}\ncount=0\n'
                    'while [ "$(getprop sys.boot_completed)" != 1 ]; do\n'
                    '    count=$((count+1)); [ "$count" -lt 2 ] || exit 1; sleep 1\n'
                    'done\nsettings put global test 1\ntouch ' + str(target) + '\n')
        (tools / 'settings').write_text('#!/bin/sh\ntouch ' + str(target) + '\n')
        (tools / 'settings').chmod(0o700)
        service = self.module / 'service.sh'
        service.write_text(lifecycle.guarded_hook(original))
        result = subprocess.run(['sh', str(service)], capture_output=True, text=True,
                                env=dict(os.environ, PATH=str(tools) + ':' + os.environ['PATH']))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(flag.exists())
        self.assertFalse(target.exists())

    def test_generated_hooks_stop_on_broken_lifecycle_markers(self):
        import apply_assistant_fix as assistant
        import apply_flutter_fix as flutter
        import apply_navigation_fix as navigation
        import apply_boot_service_fix as boot
        import apply_pad_camera_native_fix as pad_camera
        import apply_xiaomi_camera_fix as phone_camera
        from phone_profile import profile
        firmware = profile('4.0.18.0.XFRCNXM')
        pad_folder = self.folder / 'pad-hooks'
        phone_folder = self.folder / 'phone-hooks'
        pad_folder.mkdir()
        phone_folder.mkdir()
        pad_camera.write_scripts(pad_folder)
        phone_camera.write_module_scripts(phone_folder, {
            'apk_sha256': phone_camera.APK18_HASH, 'runtime_sha256': phone_camera.RUNTIME_HASH,
            'yuv_sha256': 'a' * 64, 'targets': []}, firmware)
        hooks = [assistant.boot_script(), assistant.boot_script(assistant.PAD_SOURCE),
                 flutter.boot_script(), flutter.boot_script(flutter.PAD_SOURCE), boot.boot_script()[0],
                 navigation.startup_script(navigation.EARLY_SCRIPT, firmware),
                 navigation.startup_script(navigation.BOOT_SCRIPT, firmware)]
        hooks += [(folder / name).read_text() for folder in (pad_folder, phone_folder)
                  for name in ('post-fs-data.sh', 'service.sh')]
        service = self.module / 'service.sh'
        for index, script in enumerate(hooks):
            subprocess.run(['sh', '-n'], input=script, text=True, check=True, capture_output=True)
            service.write_text(script)
            for name in ('disable', 'remove'):
                marker = self.module / name
                marker.symlink_to(self.folder / 'missing')
                baseline = {p.name for p in self.module.iterdir()}
                with self.subTest(hook=index, marker=name):
                    result = subprocess.run(['sh', str(service)], capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual({p.name for p in self.module.iterdir()}, baseline)
                    self.assertTrue(marker.is_symlink())
                marker.unlink()

    def test_provider_restoration_runs_when_disabled_during_owned_restart(self):
        tools = self.folder / 'tools'
        tools.mkdir()
        marker = self.module / 'disable'
        events = self.folder / 'events'
        (tools / 'stop').write_text('#!/bin/sh\necho stop >> ' + str(events) + '\ntouch ' + str(marker) + '\n')
        (tools / 'start').write_text('#!/bin/sh\necho start >> ' + str(events) + '\n')
        for path in tools.iterdir():
            path.chmod(0o700)
        service = self.module / 'service.sh'
        service.write_text(lifecycle.guarded_hook('#!/bin/sh\nMODDIR=${0%/*}\n'
                           'stop owned-provider\nstart owned-provider\n'))
        result = subprocess.run(['sh', str(service)], capture_output=True, text=True,
                                env=dict(os.environ, PATH=str(tools) + ':' + os.environ['PATH']))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(events.read_text().splitlines(), ['stop', 'start'])
        self.assertTrue(marker.exists())

    def test_assigned_installers_preserve_choices_before_optional_apk_checks(self):
        for module_name in ('apply_flutter_fix', 'apply_navigation_fix', 'apply_assistant_fix',
                            'apply_pad_camera_native_fix', 'apply_xiaomi_camera_fix',
                            'apply_boot_service_fix'):
            module = importlib.import_module(module_name)
            for flag in ('disable', 'remove', 'pending'):
                with self.subTest(module=module_name, flag=flag), ExitStack() as stack:
                    stack.enter_context(patch.object(module, 'official'))
                    root = stack.enter_context(patch.object(module, 'root', return_value=flag))
                    adb = stack.enter_context(patch.object(module, 'adb'))
                    stack.enter_context(patch.object(module, 'ROOT', self.folder / 'unused'))
                    args = ({}, self.folder / 'unsupported-payload') if module_name == 'apply_boot_service_fix' else ({},)
                    self.assertEqual(module.install(*args), {'preserved': True, 'lifecycle': [flag]})
                    self.assertEqual(root.call_count, 1)
                    adb.assert_not_called()
                    self.assertFalse((self.folder / 'unused').exists())


if __name__ == '__main__':
    unittest.main()
