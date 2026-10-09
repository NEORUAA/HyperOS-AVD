"""Check native module lifecycle and preservation without touching a real AVD."""
import hashlib
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / 'scripts'))
import apply_native_compat as install
import package_native_module as package

PROPERTY = f'id={install.MODULE_ID}\nauthor=HyperOS-AVD\nversionCode=1'


class NativeModuleInstallTests(unittest.TestCase):
    def run_install(self, active=None, pending=None, firmware='OS4.99.999.0.TEST', verify=None):
        commands = []
        modules = {install.MODULE: active, install.PENDING: pending}

        def guest(config, command):
            commands.append(command)
            if command.startswith('getprop'):
                return 'ranchu\n' + firmware
            directory = install.PENDING if install.PENDING in command else install.MODULE
            state = modules[directory]
            if command.startswith('if ') and "printf 'present" in command:
                return 'absent' if state is None else 'present\n' + state.get('ownership', PROPERTY)
            if command.startswith('for flag in disable remove;'):
                return '\n'.join(state.get('flags', ()))
            if command.startswith('if ') and '/manifest.json' in command:
                return state.get('manifest', '')
            if 'sha256sum -c SHA256SUMS' in command and verify:
                return verify(command)
            return ''

        with patch.object(install, 'root', side_effect=guest), patch.object(install, 'adb') as adb:
            result = install.install({'name': 'Renamed_By_User', 'port': 5590})
        return result, commands, adb

    def current_manifest(self):
        return package.module_files()['manifest.json'].decode()

    def test_standard_install_has_no_implicit_restart_or_legacy_changes(self):
        result, commands, adb = self.run_install()
        self.assertTrue(result['reboot_required'])
        self.assertEqual(adb.call_count, 1)
        staged = next(command for command in commands if '/data/adb/ksud module install ' in command)
        self.assertIn(f'[ ! -e {install.PENDING} ] && [ ! -L {install.PENDING} ]', staged)
        self.assertTrue(commands[-1].startswith('rm -f /data/local/tmp/hyperos-native-compat-'))
        self.assertFalse(any('reboot' in command or 'ctl.restart' in command or 'force-stop' in command
                             or 'hyperos_avd_flutter_render' in command for command in commands))

    def test_active_or_pending_lifecycle_choice_is_preserved_before_packaging(self):
        for directory in ('active', 'pending'):
            for flag in ('disable', 'remove'):
                with self.subTest(directory=directory, flag=flag), patch.object(install, 'package') as pack:
                    result, _, adb = self.run_install(**{directory: {'flags': [flag]}})
                    self.assertTrue(result['disabled'])
                    self.assertEqual(result['flags'], [flag])
                    pack.assert_not_called()
                    adb.assert_not_called()

    def test_existing_identical_manifest_authenticates_expected_checksum_list(self):
        result, commands, adb = self.run_install(active={'manifest': self.current_manifest()})
        self.assertTrue(result['reused'])
        verification = next(command for command in commands if 'sha256sum -c SHA256SUMS' in command)
        checksum = hashlib.sha256(package.module_files()['SHA256SUMS']).hexdigest()
        self.assertIn(f'sha256sum {install.MODULE}/SHA256SUMS', verification)
        self.assertIn(' = ' + checksum, verification)
        self.assertLess(verification.index(' = ' + checksum), verification.index('sha256sum -c SHA256SUMS'))
        adb.assert_not_called()

    def test_identical_pending_stage_is_reused_until_normal_reboot(self):
        # A staged update takes precedence over the still-active module.
        result, commands, adb = self.run_install(active={'manifest': '{}'},
                                                pending={'manifest': self.current_manifest()})
        self.assertTrue(result['reused'])
        self.assertTrue(result['pending'])
        self.assertTrue(result['reboot_required'])
        self.assertFalse(result['deferred'])
        self.assertTrue(any(f'cd {install.PENDING}' in command for command in commands))
        self.assertFalse(any(f'cat {install.MODULE}/manifest.json' in command for command in commands))
        adb.assert_not_called()

    def test_different_pending_stage_and_its_choices_are_never_replaced(self):
        value = json.loads(self.current_manifest())
        value['catalog_sha256'] = '0' * 64
        result, commands, adb = self.run_install(active={'manifest': self.current_manifest()},
                                                pending={'manifest': json.dumps(value)})
        self.assertTrue(result['deferred'])
        self.assertTrue(result['pending'])
        self.assertTrue(result['reboot_required'])
        self.assertFalse(result['reused'])
        self.assertFalse(any('features.disabled' in command or 'module install' in command
                             or command.startswith('rm ') for command in commands))
        adb.assert_not_called()

    def test_incomplete_pending_stage_is_deferred_without_repairing_it(self):
        result, _, adb = self.run_install(pending={})
        self.assertTrue(result['deferred'])
        self.assertTrue(result['reboot_required'])
        adb.assert_not_called()

    def test_unknown_owner_or_newer_revision_in_either_location_is_not_overwritten(self):
        for directory in ('active', 'pending'):
            for prop in ('id=another-module\nauthor=HyperOS-AVD\nversionCode=1',
                         f'id={install.MODULE_ID}\nauthor=Another\nversionCode=1',
                         f'id={install.MODULE_ID}\nauthor=HyperOS-AVD\nversionCode=999',
                         PROPERTY + '\nversionCode=1'):
                with self.subTest(directory=directory, prop=prop), \
                        patch.object(install, 'package') as pack, \
                        self.assertRaisesRegex(RuntimeError, 'Refused'):
                    self.run_install(**{directory: {'ownership': prop}})
                pack.assert_not_called()

    def test_unknown_manifest_is_preserved_in_either_location(self):
        for directory in ('active', 'pending'):
            for value in ('invalid-json', '{}', '[]', json.dumps({'schema': 99, 'id': install.MODULE_ID,
                                                                'revision': 1})):
                with self.subTest(directory=directory, value=value), \
                        self.assertRaisesRegex(RuntimeError, 'Refused'):
                    self.run_install(**{directory: {'manifest': value}})

    def test_other_os_family_is_not_modified(self):
        with self.assertRaisesRegex(RuntimeError, 'ranchu OS4'):
            self.run_install(firmware='OS3.0.2.0.WMCCNXM')

    def test_script_updates_change_the_manifest_identity(self):
        original = json.loads(self.current_manifest())
        self.assertIn('runtime.sh', original['files_sha256'])
        self.assertIn('customize.sh', original['files_sha256'])
        self.assertEqual(original['revision'], install.REVISION)


class NativeModuleShellGuardTests(unittest.TestCase):
    """Execute the emitted read/verification guards on disposable local files."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='hyperos-native-guards-')
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.active = self.folder / 'active'
        self.pending = self.folder / 'pending'
        if shutil.which('sha256sum'):
            self.hash_command = shlex.quote(shutil.which('sha256sum'))
        elif shutil.which('shasum'):
            self.hash_command = shlex.quote(shutil.which('shasum')) + ' -a 256'
        else:
            self.skipTest('No local SHA-256 utility is available.')

    def shell(self, command):
        translated = command.replace(install.PENDING, str(self.pending)).replace(install.MODULE, str(self.active))
        translated = translated.replace('/data/adb/ksu/bin/busybox sha256sum', self.hash_command)
        result = subprocess.run(['sh', '-c', 'set -e\n' + translated], capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError('Local module guard rejected unsafe or mismatched assets.')
        return result.stdout.strip()

    def test_exact_assets_pass_but_rewritten_checksums_cannot_authenticate_tampered_script(self):
        files = package.module_files()
        self.active.mkdir()
        for name, body in files.items():
            target = self.active / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
        expected = hashlib.sha256(files['SHA256SUMS']).hexdigest()
        with patch.object(install, 'root', side_effect=lambda config, command: self.shell(command)):
            install._verify_assets({}, install.MODULE, expected)
            # Rewriting both the script and the guest's checksum list used to
            # pass a self-check while retaining the original manifest identity.
            files['runtime.sh'] += b'\n# Changed outside the known package.\n'
            (self.active / 'runtime.sh').write_bytes(files['runtime.sh'])
            checksum = ''.join(hashlib.sha256(body).hexdigest() + '  ' + name + '\n'
                               for name, body in sorted(files.items()) if name != 'SHA256SUMS')
            (self.active / 'SHA256SUMS').write_text(checksum)
            with self.assertRaisesRegex(RuntimeError, 'rejected'):
                install._verify_assets({}, install.MODULE, expected)

    def test_dangling_remove_link_is_preserved_as_a_lifecycle_flag(self):
        self.active.mkdir()
        (self.active / 'module.prop').write_text(PROPERTY + '\n')
        (self.active / 'remove').symlink_to(self.folder / 'missing')
        with patch.object(install, 'root', side_effect=lambda config, command: self.shell(command)):
            self.assertEqual(install._module({}, install.MODULE)['flags'], ['remove'])

    def test_module_directory_link_is_not_followed(self):
        self.pending.mkdir()
        (self.pending / 'module.prop').write_text(PROPERTY + '\n')
        self.active.symlink_to(self.pending, target_is_directory=True)
        with patch.object(install, 'root', side_effect=lambda config, command: self.shell(command)), \
                self.assertRaisesRegex(RuntimeError, 'rejected'):
            install._module({}, install.MODULE)

    def test_preinstall_guard_rejects_a_new_pending_stage(self):
        self.pending.mkdir()
        marker = self.folder / 'should-not-install'
        with self.assertRaisesRegex(RuntimeError, 'rejected'):
            self.shell(install._stage_guard(None) + 'touch ' + shlex.quote(str(marker)))
        self.assertFalse(marker.exists())

    def test_preinstall_guard_rejects_a_late_user_disable_choice(self):
        self.active.mkdir()
        (self.active / 'module.prop').write_text(PROPERTY + '\n')
        (self.active / 'disable').touch()
        marker = self.folder / 'should-not-install'
        with self.assertRaisesRegex(RuntimeError, 'rejected'):
            self.shell(install._stage_guard({'properties': PROPERTY}) + 'touch ' + shlex.quote(str(marker)))
        self.assertFalse(marker.exists())

    def test_preinstall_guard_accepts_unchanged_owned_active_module(self):
        self.active.mkdir()
        (self.active / 'module.prop').write_text(PROPERTY + '\n')
        self.shell(install._stage_guard({'properties': PROPERTY}))


if __name__ == '__main__':
    unittest.main()
