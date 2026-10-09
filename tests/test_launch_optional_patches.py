"""Keep unknown optional workloads local without masking integrity failures."""
from contextlib import ExitStack
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import launch
from patch_outcome import UnsupportedPatch


class OptionalPatchTests(unittest.TestCase):
    def test_only_explicit_untouched_outcome_is_skipped(self):
        outcomes = {}
        with patch('sys.stdout', new_callable=io.StringIO):
            launch.optional_patch(outcomes, 'weather', Mock(side_effect=UnsupportedPatch('Unknown APK')))
        self.assertEqual(outcomes, {'weather': {'state': 'unsupported', 'reason': 'Unknown APK'}})
        for failure in (RuntimeError('Unknown owned payload'), OSError('read failure'),
                        subprocess.CalledProcessError(1, 'adb')):
            with self.subTest(error=failure), self.assertRaises(type(failure)):
                launch.optional_patch({}, 'weather', Mock(side_effect=failure))

    def _initialize(self, source, failure=None, mandatory_failure=None):
        order = []
        def transport(_config, *args, **_kwargs):
            command = args[-1]
            output = ('uid=2000(shell)' if command == 'id' else
                      'uid=0(root)' if command == "su -W -c 'id'" else
                      'uid=0(root)\nEnforcing' if 'feature set selinux_hide' in command else
                      '1' if command == 'getprop sys.boot_completed' else
                      'package:/system/app/KernelSU.apk' if command == 'pm path me.weishu.kernelsu' else '')
            return Mock(returncode=0, stdout=output, stderr='')
        def record(name, error=None):
            def call(*_args, **_kwargs):
                order.append(name)
                if error:
                    raise error
                return {'installed': True, 'features': ['weather', 'oem-camera', 'parrot-camera']}
            return call
        imports = {
            'os4_defaults.apply_sensor_defaults': Mock(),
            'os4_defaults.apply_runtime': record('defaults'),
            'os4_pad.apply_runtime': record('defaults'),
            'os4_pad.align_window': record('window'),
            'apply_native_compat.install': record('native', mandatory_failure),
            'core_context.load': Mock(return_value={'schema': 1, 'identity_sources': []}),
            'apply_app_compat.install_prebuilt': record('apps', failure),
            'apply_flutter_fix.install': Mock(side_effect=AssertionError('Legacy Flutter installer invoked')),
            'apply_navigation_fix.install': Mock(side_effect=AssertionError('Legacy navigation installer invoked')),
            'apply_weather_fix.install': Mock(side_effect=AssertionError('Legacy Weather installer invoked')),
            'apply_assistant_fix.install': Mock(side_effect=AssertionError('Legacy XiaoAI installer invoked')),
            'apply_xiaomi_camera_fix.install': Mock(side_effect=AssertionError('Legacy Phone camera invoked')),
            'apply_pad_camera_fix.install': Mock(side_effect=AssertionError('Legacy Pad ANGLE invoked')),
            'apply_pad_camera_native_fix.install': Mock(side_effect=AssertionError('Legacy Pad camera invoked')),
        }
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
            root = Path(temporary)
            (root / 'local').mkdir()
            (root / 'local/build.json').write_text(json.dumps({'source': source}))
            stack.enter_context(patch.object(launch, 'ROOT', root))
            stack.enter_context(patch.object(launch, 'adb', side_effect=transport))
            stack.enter_context(patch.object(launch, 'migrate_boot_service_helpers', side_effect=record('migration')))
            stack.enter_context(patch('sys.stdout', new_callable=io.StringIO))
            for target, effect in imports.items():
                stack.enter_context(patch(target, side_effect=effect))
            launch.initialize({'name': 'Renamed_Device', 'port': 5586, 'camera_bridge': True})
            result = json.loads((root / 'local/last-boot.json').read_text())
        return order, result

    def test_phone_uses_two_common_owners_without_per_app_installers(self):
        order, result = self._initialize('official-hongkong-ota')
        self.assertEqual(order, ['defaults', 'migration', 'native', 'apps'])
        self.assertEqual(set(result['patch_outcomes']), {'core', 'apps'})

    def test_pad_uses_the_same_common_owners_as_phone(self):
        order, result = self._initialize('official-yingtian-ota')
        self.assertEqual(order, ['defaults', 'window', 'migration', 'native', 'apps'])
        self.assertEqual(set(result['patch_outcomes']), {'core', 'apps'})

    def test_mandatory_shared_engine_failure_stays_fatal(self):
        with self.assertRaisesRegex(RuntimeError, 'Unknown shared engine'):
            self._initialize('official-hongkong-ota', mandatory_failure=RuntimeError('Unknown shared engine'))

    def test_optional_integrity_failure_stays_fatal(self):
        with self.assertRaisesRegex(RuntimeError, 'Unrelated module'):
            self._initialize('official-yingtian-ota', failure=RuntimeError('Unrelated module'))


if __name__ == '__main__':
    unittest.main()
