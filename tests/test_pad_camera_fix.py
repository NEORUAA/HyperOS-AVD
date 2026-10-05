"""Exercise guarded, reversible package-scoped ANGLE settings without a guest."""
import json
from pathlib import Path
import shlex
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import apply_flutter_fix
import apply_pad_camera_fix as camera


class FakeRoot:
    """Reject commands outside the installer's read guards and settings scope."""

    def __init__(self, values, fail_write=None, fail_restore=False,
                 corrupt_verification=False):
        self.state = dict(zip(camera.KEYS, values))
        self.properties = dict(camera.PROPERTIES)
        self.hashes = dict(camera.HASHES)
        self.apk = 'package:' + camera.APK
        self.commands = []
        self.mutations = []
        self.fail_write = fail_write
        self.fail_restore = fail_restore
        self.corrupt_verification = corrupt_verification

    def __call__(self, config, command, **kwargs):
        self.commands.append((dict(config), command))
        words = shlex.split(command)
        if len(words) == 2 and words[0] == 'getprop':
            return self.properties[words[1]]
        if words == ['pm', 'path', camera.PACKAGE]:
            return self.apk
        if len(words) == 2 and words[0] == 'sha256sum':
            return self.hashes[words[1]] + '  ' + words[1]
        if len(words) < 4 or words[0] != 'settings' or words[2] != 'global':
            raise AssertionError('Unexpected root command: ' + command)
        operation, key = words[1], words[3]
        if key not in camera.KEYS:
            raise AssertionError('Unexpected settings key: ' + key)
        if operation == 'get' and len(words) == 4:
            if self.corrupt_verification and self.mutations and key == camera.KEYS[2]:
                self.corrupt_verification = False
                return 'unexpectedFeature'
            return self.state[key]
        if operation == 'put' and len(words) == 5:
            value = words[4]
        elif operation == 'delete' and len(words) == 4:
            value = 'null'
        else:
            raise AssertionError('Unexpected settings command: ' + command)
        self.mutations.append((operation, key, value))
        count = len(self.mutations)
        if self.fail_restore and count == 3:
            raise RuntimeError('Injected restore failure before its side effect.')
        self.state[key] = value
        if count == self.fail_write:
            raise RuntimeError('Injected settings failure after its side effect.')
        return ''

    def setting_reads(self):
        return [shlex.split(command)[3] for _, command in self.commands
                if shlex.split(command)[:3] == ['settings', 'get', 'global']]


class AngleSettingsTests(unittest.TestCase):
    def test_empty_and_absent_selections_append_only_camera(self):
        expected = (camera.PACKAGE, 'angle', 'exposeES32ForTesting')
        for packages in ('null', ''):
            for values in ('null', ''):
                for features in ('null', ''):
                    with self.subTest(packages=packages, values=values, features=features):
                        self.assertEqual(camera.angle_settings(packages, values, features), expected)

    def test_unrelated_selection_and_feature_order_survive(self):
        self.assertEqual(camera.angle_settings(
            'com.example.video,com.example.reader', 'native,default',
            'forceVulkanUseDefaultUniformBlock,preferSubmitAtEndOfPass'),
            ('com.example.video,com.example.reader,' + camera.PACKAGE,
             'native,default,angle',
             'forceVulkanUseDefaultUniformBlock,preferSubmitAtEndOfPass,exposeES32ForTesting'))

    def test_existing_camera_and_repeated_es32_feature_are_idempotent(self):
        expected = ('com.example.video,' + camera.PACKAGE + ',com.example.reader',
                    'native,angle,default', 'first,exposeES32ForTesting,last')
        actual = camera.angle_settings(
            expected[0], 'native,native,default',
            'first,exposeES32ForTesting,last,exposeES32ForTesting')
        self.assertEqual(actual, expected)
        self.assertEqual(camera.angle_settings(*actual), expected)

    def test_malformed_lists_cannot_be_silently_repaired(self):
        cases = (
            ('com.one,com.two', 'angle', ''),
            ('com.one', 'angle,native', ''),
            ('com.one', '', ''),
            ('null', 'angle', ''),
            ('com.one,,com.two', 'angle,native,default', ''),
            ('com.one,', 'angle,native', ''),
            (',com.one', 'native,angle', ''),
            ('com.one,com.two', ',angle', ''),
            ('com.one,com.two', 'angle,', ''),
            ('com.one,com.one', 'native,angle', ''),
            ('', '', 'first,,last'),
        )
        for values in cases:
            with self.subTest(values=values), self.assertRaises(RuntimeError):
                camera.angle_settings(*values)


class PadCameraInstallTests(unittest.TestCase):
    def setUp(self):
        self.config = {'sdk': '/unused-sdk', 'name': 'Renamed_Pad_Camera', 'port': 5596}

    def install(self, fake):
        with patch.object(camera, 'official') as official, \
                patch.object(camera, 'root', side_effect=fake):
            result = camera.install(self.config)
        official.assert_called_once_with(self.config, sources=(camera.SOURCE,))
        return result

    def test_renamed_instance_preserves_other_packages_and_writes_only_changes(self):
        before = ('com.example.video,' + camera.PACKAGE + ',com.example.reader',
                  'native,native,default', 'otherFeature,exposeES32ForTesting')
        fake = FakeRoot(before)
        self.install(fake)
        expected = (before[0], 'native,angle,default', before[2])
        self.assertEqual(tuple(fake.state[key] for key in camera.KEYS), expected)
        self.assertEqual(fake.mutations, [('put', camera.KEYS[1], expected[1])])
        self.assertTrue(all(config == self.config for config, _ in fake.commands))
        first_setting = next(index for index, (_, command) in enumerate(fake.commands)
                             if shlex.split(command)[0] == 'settings')
        guards = [shlex.split(command) for _, command in fake.commands[:first_setting]]
        self.assertEqual(guards, [
            *(['getprop', key] for key in camera.PROPERTIES),
            ['pm', 'path', camera.PACKAGE],
            *(['sha256sum', path] for path in camera.HASHES),
        ])
        fake.commands.clear()
        fake.mutations.clear()
        self.install(fake)
        self.assertEqual(fake.mutations, [])
        self.assertEqual(fake.setting_reads(), list(camera.KEYS))

    def test_actual_unknown_source_is_refused_before_any_adb(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'local').mkdir()
            (root / 'local/build.json').write_text(json.dumps({'source': 'official-unverified-ota'}))
            with patch.object(apply_flutter_fix, 'ROOT', root), \
                    patch.object(apply_flutter_fix, 'adb') as adb, \
                    patch.object(camera, 'root') as command:
                with self.assertRaisesRegex(RuntimeError, 'firmware profile'):
                    camera.install(self.config)
            adb.assert_not_called()
            command.assert_not_called()

    def test_each_device_guard_refuses_before_reading_or_writing_settings(self):
        for key in camera.PROPERTIES:
            with self.subTest(key=key):
                fake = FakeRoot(('null', 'null', 'null'))
                fake.properties[key] = 'unsupported'
                with self.assertRaises(RuntimeError):
                    self.install(fake)
                self.assertEqual(fake.mutations, [])
                self.assertEqual(fake.setting_reads(), [])

    def test_updated_apk_path_and_each_unknown_hash_refuse_before_settings(self):
        for target in ('apk-path', *camera.HASHES):
            with self.subTest(target=target):
                fake = FakeRoot(('null', 'null', 'null'))
                if target == 'apk-path':
                    fake.apk = 'package:/data/app/updated-camera/base.apk'
                else:
                    fake.hashes[target] = '0' * 64
                with self.assertRaises(RuntimeError):
                    self.install(fake)
                self.assertEqual(fake.mutations, [])
                self.assertEqual(fake.setting_reads(), [])

    def test_malformed_paired_settings_produce_no_writes(self):
        for before in (('com.one,com.two', 'angle', 'otherFeature'),
                       ('com.one,com.one', 'native,angle', ''),
                       ('com.one,', 'native,angle', '')):
            with self.subTest(before=before):
                fake = FakeRoot(before)
                with self.assertRaises(RuntimeError):
                    self.install(fake)
                self.assertEqual(fake.mutations, [])
                self.assertEqual(tuple(fake.state[key] for key in camera.KEYS), before)

    def test_second_write_failure_restores_attempted_writes_in_reverse(self):
        before = ('null', '', 'null')
        fake = FakeRoot(before, fail_write=2)
        with self.assertRaisesRegex(RuntimeError, 'original settings restored'):
            self.install(fake)
        self.assertEqual(tuple(fake.state[key] for key in camera.KEYS), before)
        self.assertEqual(fake.mutations, [
            ('put', camera.KEYS[0], camera.PACKAGE),
            ('put', camera.KEYS[1], 'angle'),
            ('put', camera.KEYS[1], ''),
            ('delete', camera.KEYS[0], 'null'),
        ])
        self.assertEqual(fake.setting_reads()[-3:], list(camera.KEYS))

    def test_failed_restore_continues_other_restores_and_reports_incomplete_rollback(self):
        fake = FakeRoot(('null', '', 'null'), fail_write=2, fail_restore=True)
        with self.assertRaises(RuntimeError) as error:
            self.install(fake)
        self.assertIn('rollback', str(error.exception).lower())
        self.assertRegex(str(error.exception).lower(), 'failed|incomplete')
        self.assertEqual([key for _, key, _ in fake.mutations],
                         [camera.KEYS[0], camera.KEYS[1], camera.KEYS[1], camera.KEYS[0]])
        self.assertEqual(fake.state[camera.KEYS[0]], 'null')
        self.assertEqual(fake.state[camera.KEYS[1]], 'angle')
        self.assertEqual(fake.setting_reads()[-3:], list(camera.KEYS))

    def test_postwrite_verification_mismatch_restores_all_three_settings(self):
        before = ('null', '', 'otherFeature')
        fake = FakeRoot(before, corrupt_verification=True)
        with self.assertRaisesRegex(RuntimeError, 'original settings restored'):
            self.install(fake)
        self.assertEqual(tuple(fake.state[key] for key in camera.KEYS), before)
        self.assertEqual([key for _, key, _ in fake.mutations],
                         [*camera.KEYS, *reversed(camera.KEYS)])
        self.assertEqual(fake.setting_reads(), [*camera.KEYS, *camera.KEYS, *camera.KEYS])


if __name__ == '__main__':
    unittest.main()
