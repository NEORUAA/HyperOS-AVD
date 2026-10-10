"""Guard launch-time userdata creation and offline filesystem preparation."""
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import launch
from userdata_resize import OfflineBackupRequiredError, OpaqueFilesystemError


class LaunchUserdataTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.name = 'Renamed_User_AVD'
        self.avd = self.root / 'avd' / (self.name + '.avd')
        self.avd.mkdir(parents=True)
        self.template = self.root / 'images/userdata.img'
        self.template.parent.mkdir()
        self.template.write_bytes(b'clean userdata template')
        self.config = {'name': self.name, 'sdk': '/unused/sdk', 'hardware': {}}
        self.sdk = Path(self.config['sdk'])
        self.patch_root = patch.object(launch, 'ROOT', self.root)
        self.patch_root.start()
        self.addCleanup(self.patch_root.stop)
        self.write_capacity('32G')

    def write_capacity(self, value):
        lines = ['hw.ramSize=6144', 'hw.cpu.ncore=4']
        if value is not None:
            lines.append('disk.dataPartition.size=' + value)
        (self.avd / 'config.ini').write_text('\n'.join(lines) + '\n')

    def existing(self):
        userdata = self.avd / 'userdata-qemu.img'
        userdata.write_bytes(b'retained userdata')
        return userdata

    def test_legacy_empty_hardware_uses_configured_capacity(self):
        userdata = self.existing()
        with patch('manage.validate_userdata') as validate, patch('manage.resize', return_value='repaired') as resize, \
                patch('userdata_resize.check_dependencies') as dependencies, patch.object(launch.shutil, 'copyfile') as copy:
            result = launch.prepare_userdata(self.config)
        self.assertEqual(result, 'repaired')
        validate.assert_called_once_with(self.sdk, self.avd)
        resize.assert_called_once_with(self.sdk, self.avd, 32, allow_guest=True)
        dependencies.assert_not_called()
        copy.assert_not_called()
        self.assertEqual(userdata.read_bytes(), b'retained userdata')

    def test_missing_hardware_and_partial_hardware_use_configured_capacity(self):
        self.existing()
        for hardware in (None, {'hw.ramSize': '8192'}):
            with self.subTest(hardware=hardware):
                config = dict(self.config)
                if hardware is None:
                    config.pop('hardware')
                else:
                    config['hardware'] = hardware
                with patch('manage.validate_userdata'), patch('manage.resize') as resize:
                    launch.prepare_userdata(config)
                resize.assert_called_once_with(self.sdk, self.avd, 32, allow_guest=True)

    def test_runtime_capacity_overrides_configured_capacity(self):
        self.existing()
        self.config['hardware']['disk.dataPartition.size'] = '48G'
        with patch('manage.validate_userdata'), patch('manage.resize') as resize:
            launch.prepare_userdata(self.config)
        resize.assert_called_once_with(self.sdk, self.avd, 48, allow_guest=True)

    def test_valid_configured_sizes_reach_resize_as_gib(self):
        self.existing()
        for gib in (6, 32, 64, 1024):
            with self.subTest(gib=gib):
                self.write_capacity(f'{gib}G')
                with patch('manage.validate_userdata'), patch('manage.resize') as resize:
                    launch.prepare_userdata(self.config)
                resize.assert_called_once_with(self.sdk, self.avd, gib, allow_guest=True)

    def test_missing_or_invalid_capacity_refuses_before_mutation(self):
        for capacity in (None, '', '32', '32M', '-1G', '1.5G', 'G', '0G', '5G', '1025G'):
            with self.subTest(capacity=capacity):
                self.write_capacity(capacity)
                with patch('manage.validate_userdata') as validate, patch('manage.resize') as resize, \
                        patch('userdata_resize.check_dependencies') as dependencies, patch.object(launch.shutil, 'copyfile') as copy:
                    with self.assertRaisesRegex(RuntimeError, 'capacity'):
                        launch.prepare_userdata(self.config)
                dependencies.assert_not_called()
                copy.assert_not_called()
                validate.assert_not_called()
                resize.assert_not_called()

    def test_invalid_runtime_capacity_is_not_replaced_by_config_fallback(self):
        for capacity in (None, 32, False, '32M'):
            with self.subTest(capacity=capacity):
                self.config['hardware']['disk.dataPartition.size'] = capacity
                with patch('manage.validate_userdata') as validate, patch('manage.resize') as resize, \
                        patch.object(launch.shutil, 'copyfile') as copy:
                    with self.assertRaisesRegex(RuntimeError, 'capacity'):
                        launch.prepare_userdata(self.config)
                copy.assert_not_called()
                validate.assert_not_called()
                resize.assert_not_called()

    def test_missing_fresh_dependencies_refuse_before_template_copy(self):
        with patch('userdata_resize.check_dependencies', side_effect=RuntimeError('Missing e2fsprogs')) as dependencies, \
                patch.object(launch.shutil, 'copyfile') as copy, patch('manage.validate_userdata') as validate, \
                patch('manage.resize') as resize:
            with self.assertRaisesRegex(RuntimeError, 'e2fsprogs'):
                launch.prepare_userdata(self.config)
        dependencies.assert_called_once_with(self.sdk)
        copy.assert_not_called()
        validate.assert_not_called()
        resize.assert_not_called()
        self.assertFalse((self.avd / 'userdata-qemu.img').exists())

    def test_fresh_creation_checks_dependencies_then_validates_before_resize(self):
        order = Mock()
        with patch('userdata_resize.check_dependencies') as dependencies, \
                patch.object(launch.shutil, 'copyfile', wraps=launch.shutil.copyfile) as copy, \
                patch('manage.validate_userdata') as validate, patch('manage.resize', return_value='expanded') as resize:
            for name, function in (('dependencies', dependencies), ('copy', copy), ('validate', validate), ('resize', resize)):
                order.attach_mock(function, name)
            result = launch.prepare_userdata(self.config)
        self.assertEqual(result, 'expanded')
        self.assertEqual([item[0] for item in order.mock_calls], ['dependencies', 'copy', 'validate', 'resize'])
        dependencies.assert_called_once_with(self.sdk)
        copy.assert_called_once_with(self.template, self.avd / 'userdata-qemu.img')
        validate.assert_called_once_with(self.sdk, self.avd)
        resize.assert_called_once_with(self.sdk, self.avd, 32, allow_guest=True)
        self.assertEqual((self.avd / 'userdata-qemu.img').read_bytes(), self.template.read_bytes())

    def test_existing_validation_failure_prevents_resize_and_copy(self):
        userdata = self.existing()
        with patch('manage.validate_userdata', side_effect=RuntimeError('Invalid QCOW2 chain')) as validate, \
                patch('manage.resize') as resize, patch.object(launch.shutil, 'copyfile') as copy:
            with self.assertRaisesRegex(RuntimeError, 'QCOW2'):
                launch.prepare_userdata(self.config)
        validate.assert_called_once_with(self.sdk, self.avd)
        resize.assert_not_called()
        copy.assert_not_called()
        self.assertEqual(userdata.read_bytes(), b'retained userdata')

    def test_opaque_existing_data_is_backed_up_before_owned_guest_repair(self):
        userdata = self.existing()
        self.config['port'] = 5588
        folder = self.root / 'backups' / 'saved'
        deferred = {'guest_required': True, 'userdata_capacity_token': {'nonce': 'private'}}
        order = Mock()
        with patch('manage.validate_userdata'), patch('manage.resize', return_value=deferred), \
                patch('manage.backup', return_value=folder) as backup, \
                patch('manage.prepare_storage', return_value={'capacity_proof': 'verified-in-guest'}) as repair:
            order.attach_mock(backup, 'backup')
            order.attach_mock(repair, 'repair')
            result = launch.prepare_userdata(self.config)
        self.assertEqual([call[0] for call in order.mock_calls], ['backup', 'repair'])
        backup.assert_called_once_with(self.root, self.name)
        repair.assert_called_once_with(self.root, self.name, 5588, self.sdk, 32, folder)
        self.assertEqual(result, {'capacity_proof': 'verified-in-guest'})
        self.assertEqual(userdata.read_bytes(), b'retained userdata')

    def test_guest_repair_backup_failure_stops_before_starting_guest(self):
        userdata = self.existing()
        self.config['port'] = 5588
        with patch('manage.validate_userdata'), patch('manage.resize', return_value={'guest_required': True}), \
                patch('manage.backup', side_effect=OSError('backup failed')), patch('manage.prepare_storage') as repair:
            with self.assertRaisesRegex(OSError, 'backup failed'):
                launch.prepare_userdata(self.config)
        repair.assert_not_called()
        self.assertEqual(userdata.read_bytes(), b'retained userdata')

    def test_encrypted_disk_growth_creates_backup_before_managed_resize_and_guest(self):
        userdata = self.existing()
        self.config['port'] = 5596
        folder = self.root / 'backups' / 'before-encrypted-growth'
        order = Mock()
        with patch('manage.validate_userdata') as validate, \
                patch('manage.resize', side_effect=OfflineBackupRequiredError('matching complete offline backup')) as resize, \
                patch('manage.backup', return_value=folder) as backup, \
                patch('manage.prepare_storage', return_value={'prepared_filesystem_bytes': 32 * 1024**3}) as repair:
            for name, function in (('validate', validate), ('resize', resize), ('backup', backup), ('repair', repair)):
                order.attach_mock(function, name)
            result = launch.prepare_userdata(self.config)
        self.assertEqual([call[0] for call in order.mock_calls], ['validate', 'resize', 'backup', 'repair'])
        backup.assert_called_once_with(self.root, self.name)
        repair.assert_called_once_with(self.root, self.name, 5596, self.sdk, 32, folder)
        self.assertEqual(result['prepared_filesystem_bytes'], 32 * 1024**3)
        self.assertEqual(userdata.read_bytes(), b'retained userdata')

    def test_encrypted_disk_growth_backup_failure_preserves_data_without_boot(self):
        userdata = self.existing()
        with patch('manage.validate_userdata'), \
                patch('manage.resize', side_effect=OfflineBackupRequiredError('offline backup')), \
                patch('manage.backup', side_effect=OSError('encrypted backup failed')), \
                patch('manage.prepare_storage') as repair, patch.object(launch.shutil, 'copyfile') as copy:
            with self.assertRaisesRegex(OSError, 'encrypted backup failed'):
                launch.prepare_userdata(self.config)
        repair.assert_not_called()
        copy.assert_not_called()
        self.assertEqual(userdata.read_bytes(), b'retained userdata')

    def test_other_opaque_refusals_cannot_create_backup_or_boot(self):
        userdata = self.existing()
        with patch('manage.validate_userdata'), \
                patch('manage.resize', side_effect=OpaqueFilesystemError('mismatched or unknown chain')), \
                patch('manage.backup') as backup, patch('manage.prepare_storage') as repair:
            with self.assertRaisesRegex(OpaqueFilesystemError, 'mismatched'):
                launch.prepare_userdata(self.config)
        backup.assert_not_called()
        repair.assert_not_called()
        self.assertEqual(userdata.read_bytes(), b'retained userdata')

    def test_interrupted_activation_recovers_original_before_fresh_copy(self):
        from userdata_resize import PENDING
        original = self.avd / PENDING / 'original'
        original.mkdir(parents=True)
        retained = original / 'userdata-qemu.img'
        retained.write_bytes(b'original interrupted userdata')
        overlay = original / 'userdata-qemu.img.qcow2'
        overlay.write_bytes(b'original interrupted overlay')
        order = Mock()

        def recover(*_, **kwargs):
            retained.replace(self.avd / retained.name)
            overlay.replace(self.avd / overlay.name)
            return 'recovered and expanded'

        with patch('manage.resize', side_effect=recover) as resize, patch('manage.validate_userdata') as validate, \
                patch('userdata_resize.check_dependencies') as dependencies, patch.object(launch.shutil, 'copyfile') as copy:
            order.attach_mock(resize, 'resize')
            order.attach_mock(validate, 'validate')
            result = launch.prepare_userdata(self.config)
        self.assertEqual(result, 'recovered and expanded')
        resize.assert_called_once_with(self.sdk, self.avd, 32, allow_guest=True)
        validate.assert_called_once_with(self.sdk, self.avd)
        self.assertEqual([item[0] for item in order.mock_calls], ['resize', 'validate'])
        copy.assert_not_called()
        dependencies.assert_not_called()
        self.assertEqual((self.avd / retained.name).read_bytes(), b'original interrupted userdata')
        self.assertEqual((self.avd / overlay.name).read_bytes(), b'original interrupted overlay')

    def test_bad_pending_transaction_refuses_before_fresh_copy(self):
        from userdata_resize import PENDING
        for kind in ('directory', 'broken_symlink'):
            with self.subTest(kind=kind):
                pending = self.avd / PENDING
                if kind == 'directory':
                    pending.mkdir()
                else:
                    pending.symlink_to(self.avd / 'missing-transaction')
                try:
                    with patch('manage.resize', side_effect=RuntimeError('Unknown pending transaction')) as resize, \
                            patch('manage.validate_userdata') as validate, patch('userdata_resize.check_dependencies') as dependencies, \
                            patch.object(launch.shutil, 'copyfile') as copy:
                        with self.assertRaisesRegex(RuntimeError, 'pending transaction'):
                            launch.prepare_userdata(self.config)
                    resize.assert_called_once_with(self.sdk, self.avd, 32, allow_guest=True)
                    copy.assert_not_called()
                    dependencies.assert_not_called()
                    validate.assert_not_called()
                    self.assertFalse((self.avd / 'userdata-qemu.img').exists())
                finally:
                    pending.rmdir() if kind == 'directory' else pending.unlink()


if __name__ == '__main__':
    unittest.main()
