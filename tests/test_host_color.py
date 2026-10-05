"""Exercise owned-launch authorization and the compiled native AVD guard."""
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'scripts'))
import host_color


class HostColorEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / 'local').mkdir()
        self.library = self.root / 'tools/emulator_srgb.dylib'
        inherited = {'DYLD_INSERT_LIBRARIES': '/existing/plugin.dylib',
                     'HYPEROS_AVD_SRGB': '1', 'HYPEROS_AVD_SRGB_AVD': 'Old_AVD',
                     'UNRELATED_OPTION': 'kept'}
        environment = patch.dict(os.environ, inherited, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def manifest(self, source):
        (self.root / 'local/build.json').write_text(json.dumps({'source': source}))

    def test_owned_phone_and_pad_use_current_name_without_global_changes(self):
        for source, name in (('official-hongkong-ota', 'Renamed_Phone_API_99'),
                             ('official-yingtian-ota', 'Renamed_Pad_API_99')):
            with self.subTest(source=source):
                self.manifest(source)
                before = dict(os.environ)
                with patch.object(host_color, 'build', return_value=(self.library, None)) as build:
                    result = host_color.environment(self.root, name)
                build.assert_called_once_with(self.root)
                self.assertEqual(result['HYPEROS_AVD_SRGB'], '1')
                self.assertEqual(result['HYPEROS_AVD_SRGB_AVD'], name)
                self.assertEqual(result['DYLD_INSERT_LIBRARIES'],
                                 str(self.library.resolve()) + ':/existing/plugin.dylib')
                self.assertEqual(result['UNRELATED_OPTION'], 'kept')
                self.assertEqual(dict(os.environ), before)

    def test_default_scope_requires_an_owned_source_manifest(self):
        for source in (None, 'google-apis', 'official-unverified-ota'):
            with self.subTest(source=source):
                if source is None:
                    (self.root / 'local/build.json').unlink(missing_ok=True)
                else:
                    self.manifest(source)
                with patch.object(host_color, 'build') as build:
                    result = host_color.environment(self.root, 'Pixel_9')
                build.assert_not_called()
                self.assertNotIn('HYPEROS_AVD_SRGB', result)
                self.assertNotIn('HYPEROS_AVD_SRGB_AVD', result)
                self.assertEqual(result['DYLD_INSERT_LIBRARIES'], '/existing/plugin.dylib')

    def test_opt_out_clears_inherited_authorization_without_building(self):
        self.manifest('official-yingtian-ota')
        with patch.object(host_color, 'build') as build:
            result = host_color.environment(self.root, 'Owned_Pad', enabled=False)
        build.assert_not_called()
        self.assertNotIn('HYPEROS_AVD_SRGB', result)
        self.assertNotIn('HYPEROS_AVD_SRGB_AVD', result)
        self.assertEqual(result['DYLD_INSERT_LIBRARIES'], '/existing/plugin.dylib')
        self.assertEqual(os.environ['HYPEROS_AVD_SRGB_AVD'], 'Old_AVD')

    def test_owned_launch_requires_a_nonempty_name(self):
        self.manifest('official-yingtian-ota')
        with patch.object(host_color, 'build') as build:
            for name in ('', None):
                with self.subTest(name=name), self.assertRaisesRegex(RuntimeError, 'AVD name'):
                    host_color.environment(self.root, name)
        build.assert_not_called()


@unittest.skipUnless(sys.platform == 'darwin' and platform.machine() == 'arm64'
                     and shutil.which('xcrun'), 'Native host guard needs macOS ARM64 CLT')
class NativeHostColorGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(temporary.cleanup)
        cls.root = Path(temporary.name)
        # Build the actual dylib with the same strict flags as a managed launch.
        cls.library, _ = host_color.build(cls.root)
        main = cls.root / 'probe.c'
        main.write_text('int main(void) { return 0; }\n')
        cls.probe = cls.root / 'probe'
        subprocess.run(['xcrun', 'clang', '-Wall', '-Wextra', '-Werror', '-arch', 'arm64',
                        '-mmacosx-version-min=12.0', str(main), '-o', str(cls.probe)],
                       check=True, capture_output=True)

    def run_probe(self, arguments, enabled='1', expected='Renamed_Pad_API_99'):
        environment = os.environ.copy()
        environment.pop('HYPEROS_AVD_SRGB', None)
        environment.pop('HYPEROS_AVD_SRGB_AVD', None)
        environment['DYLD_INSERT_LIBRARIES'] = str(self.library)
        if enabled is not None:
            environment['HYPEROS_AVD_SRGB'] = enabled
        if expected is not None:
            environment['HYPEROS_AVD_SRGB_AVD'] = expected
        # This program only exercises the constructor; it creates no AVD/window.
        result = subprocess.run([str(self.probe), *arguments], env=environment,
                                text=True, capture_output=True, check=True)
        return result.stderr

    def test_compiled_hook_accepts_the_matching_renamed_instance(self):
        output = self.run_probe(['-avd', 'Renamed_Pad_API_99'])
        self.assertIn('sRGB tagging enabled for the owned OS4 AVD Renamed_Pad_API_99', output)

    def test_compiled_hook_rejects_optout_missing_name_and_other_instances(self):
        cases = ((['-avd', 'Pixel_9'], '1', 'Renamed_Pad_API_99'),
                 (['-avd', 'Renamed_Pad_API_99'], '0', 'Renamed_Pad_API_99'),
                 (['-avd', 'Renamed_Pad_API_99'], None, 'Renamed_Pad_API_99'),
                 (['-avd', 'HyperOS_4_Official_API_37'], '1', None),
                 (['-avd', 'HyperOS_4_Pad9ProMax_API_37'], '1', ''),
                 ([], '1', 'Renamed_Pad_API_99'),
                 (['-avd'], '1', 'Renamed_Pad_API_99'))
        for arguments, enabled, expected in cases:
            with self.subTest(arguments=arguments, enabled=enabled, expected=expected):
                self.assertNotIn('sRGB tagging enabled', self.run_probe(arguments, enabled, expected))


if __name__ == '__main__':
    unittest.main()
