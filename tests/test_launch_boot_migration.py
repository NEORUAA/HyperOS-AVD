"""Keep optional legacy module migration ordered and local during initialization."""
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'scripts'))
import launch
import apply_boot_service_fix as boot
import apply_native_compat as native
import os4_defaults


class LaunchBootMigrationTests(unittest.TestCase):
    def initialize(self, supported=True, failure=None):
        order = []
        config = {'name': 'Any_Renamed_AVD', 'port': 5590}
        def adb(_config, *arguments, **_kwargs):
            command = arguments[-1]
            if command == 'id':
                output = 'uid=2000(shell)'
            elif command == "su -W -c 'id'":
                output = 'uid=0(root)'
            elif 'feature set selinux_hide' in command:
                output = 'uid=0(root)\nEnforcing\n'
            elif command == 'getprop sys.boot_completed':
                output = '1\n'
            elif command == 'pm path me.weishu.kernelsu':
                output = 'package:/system/app/KernelSU.apk\n'
            else:
                output = ''
            return Mock(returncode=0, stdout=output, stderr='')
        def kernel(_config):
            order.append('kernel')
            if failure:
                raise RuntimeError(failure)
            return {'migrated': True, 'reboot_required': True}
        def uninstall(_config):
            order.append('uninstall')
            return {'migrated': False, 'unsupported': True, 'reason': 'Unknown hook is preserved.'}
        def standard(_config):
            order.append('native')
        def lifecycle(_config):
            order.append('lifecycle')
            return {'migrated': False}
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(launch, 'ROOT', Path(temporary)), \
                patch.object(launch, 'is_os4', return_value=supported), \
                patch.object(launch, 'adb', side_effect=adb) as transport, \
                patch.object(os4_defaults, 'apply_sensor_defaults'), \
                patch.object(boot, 'migrate_kernel_helper', side_effect=kernel), \
                patch.object(boot, 'migrate_uninstall_hook', side_effect=uninstall), \
                patch.object(boot, 'migrate_lifecycle_hooks', side_effect=lifecycle), \
                patch.object(native, 'install', side_effect=standard), \
                patch('sys.stdout', new_callable=io.StringIO) as output:
            launch.initialize(config)
            record = json.loads((Path(temporary) / 'local/last-boot.json').read_text())
        return order, output.getvalue(), transport.call_args_list, record

    def test_os4_migrates_known_hooks_before_native_module_installation(self):
        order, output, calls, record = self.initialize()
        self.assertEqual(order, ['kernel', 'uninstall', 'lifecycle', 'native'])
        self.assertIn('current user settings are preserved', output)
        self.assertIn('Unknown hook is preserved', output)
        self.assertFalse(any('reboot' in str(call) or 'ctl.restart' in str(call) for call in calls))
        self.assertEqual(record['name'], 'Any_Renamed_AVD')

    def test_unknown_legacy_source_does_not_abort_native_initialization(self):
        order, output, _, _ = self.initialize(failure='Unknown owned helper checksum')
        self.assertEqual(order, ['kernel', 'uninstall', 'lifecycle', 'native'])
        self.assertIn('kernel helper is preserved: Unknown owned helper checksum', output)
        self.assertIn('HyperOS is ready', output)

    def test_other_os_does_not_invoke_os4_migrations(self):
        order, _, _, _ = self.initialize(supported=False)
        self.assertEqual(order, [])


if __name__ == '__main__':
    unittest.main()
