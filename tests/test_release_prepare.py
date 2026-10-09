"""Prepare phone releases from accepted packed images without source caches."""
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'scripts'))
import patch_flutter
import phone_profile
import prepare_release_image as prepare


def digest(data):
    return hashlib.sha256(data).hexdigest()


class ReleasePreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source, self.output = self.root / 'source', self.root / 'output'
        self.selected = phone_profile.profile('4.0.18.0.XFRCNXM')
        for directory in ('images', 'tools', 'config', 'local', 'avd'):
            (self.source / directory).mkdir(parents=True)
        (self.source / 'images/system.img').write_bytes(b'accepted packed image')
        (self.source / 'images/vendor-qemu.img').write_bytes(b'unrelated hardware image')
        (self.source / 'tools/verified-helper').write_bytes(b'accepted helper')
        (self.source / 'config/avd.ini').write_text('hw.ramSize=6144\n')
        (self.source / 'avd/userdata-qemu.img.qcow2').write_bytes(b'private userdata')
        self.info = {
            'source': 'official-hongkong-ota', 'hyperos': self.selected['hyperos'],
            'archive_sha256': self.selected['archive_sha256'],
            'system_sha256': digest(b'accepted packed image'),
            'flutter_render_fix': 6, 'rear_display': {'accepted': True},
            'boot_service_fix': {'accepted': 'r4'},
            'identity_source_sha256': {'system.build.prop': 'accepted source pin'},
        }
        self.write_info()
        self.engine, self.latest = b'previous verified engine', b'latest verified engine'
        self.input_engine_pin = self.selected['pins']['flutter']
        self.entries = {
            '/' + prepare.SHARED_FLUTTER: self.engine,
            '/product/overlay/HyperOSAVDSettingsDefaults/SettingsDefaults.apk': b'signed accepted RRO',
            '/system/build.prop': b'accepted OTA properties\n',
            '/system_ext/etc/init/init.hyperos_avd.rc': b'accepted OTA boot services\n',
            prepare.APK: b'accepted signed assistant APK',
        }
        self.partitions = {
            'system': b'accepted raw system', 'vendor': b'accepted vendor',
            'system_dlkm': b'accepted kernel modules', 'vendor_dlkm': b'unrelated logical partition',
        }
        self.cloned, self.builds, self.packs = [], [], []

    def write_info(self):
        (self.source / 'local/build.json').write_text(json.dumps(self.info))

    def source_files(self):
        return {str(path.relative_to(self.source)): path.read_bytes()
                for path in self.source.rglob('*') if path.is_file()}

    def mocked_pipeline(self):
        stack = ExitStack()
        self.addCleanup(stack.close)

        def clone(source, target):
            source, target = Path(source), Path(target)
            self.assertFalse(self.source / 'work' in source.parents)
            self.cloned.append((source, target))
            target.parent.mkdir(parents=True, exist_ok=True)
            return shutil.copy2(source, target)

        def unpack(source, target):
            self.assertEqual(source, self.source / 'images/system.img')
            self.assertEqual(target, self.output / 'work/accepted')
            target.mkdir(parents=True)
            for name, content in self.partitions.items():
                (target / (name + '.img')).write_bytes(content)

        def read(image, path):
            if image == self.output / 'work/accepted/system.img':
                return self.entries[path]
            self.assertEqual(image, self.output / 'work/hyperos-system.img')
            return self.builds[-1]['edits'][path.lstrip('/')][0]

        def build(target, sources, tree, edits):
            self.assertEqual(sources, [('', self.output / 'work/accepted/system.img')])
            self.builds.append({'edits': edits.copy(), 'sources': sources})
            target.write_bytes(b'updated accepted raw system')

        def pack(source, target, partitions):
            self.assertEqual(source, self.source / 'images/system.img')
            self.packs.append([(name, path.read_bytes()) for name, path in partitions])
            target.write_bytes(b'updated accepted packed image')

        stack.enter_context(patch.object(prepare, 'clone', side_effect=clone))
        stack.enter_context(patch.object(prepare, 'unpack', side_effect=unpack))
        stack.enter_context(patch.object(prepare, 'erofs', side_effect=read))
        stack.enter_context(patch.object(prepare, 'build', side_effect=build))
        stack.enter_context(patch.object(prepare, 'pack', side_effect=pack))
        stack.enter_context(patch.object(prepare, 'sdk_path', return_value=self.root / 'sdk'))
        self.defaults_mock = stack.enter_context(patch.object(prepare, 'image_replacements', return_value=(
            {'product/etc/accepted-new-default': (b'new default', 0o644, 'u:object_r:system_file:s0')},
            {'accepted_setting': True})))
        stack.enter_context(patch.object(prepare, 'production_properties',
                                         side_effect=lambda data, **_: data + b'secure ADB\n'))
        stack.enter_context(patch.object(prepare, 'boot_defaults',
                                         side_effect=lambda data, **_: data + b'latest defaults\n'))
        stack.enter_context(patch.object(prepare, 'assistant_replacements', return_value=(
            {'product/lib64/accepted-assistant.so': (b'fixed assistant', 0o644,
                                                     'u:object_r:system_lib_file:s0')},
            {'accepted': True})))
        self.profile_mock = stack.enter_context(patch.object(patch_flutter, 'profile',
            return_value=(self.input_engine_pin, {'output': digest(self.latest)})))
        self.patch_mock = stack.enter_context(patch.object(patch_flutter, 'patch', return_value=self.latest))
        return stack

    def test_canonical_input_without_work_cache_preserves_source_ota_and_partitions(self):
        original = self.source_files()
        self.assertFalse((self.source / 'work').exists())
        self.mocked_pipeline()
        prepare.prepare(self.source, self.output)
        self.assertEqual(self.source_files(), original)
        self.assertFalse((self.source / 'work').exists())
        self.assertEqual((self.output / 'images/vendor-qemu.img').read_bytes(), b'unrelated hardware image')
        self.assertFalse((self.output / 'avd').exists())
        inputs = dict(self.packs[0])
        self.assertEqual(inputs['system'], b'updated accepted raw system')
        self.assertEqual({name: inputs[name] for name in self.partitions if name != 'system'},
                         {name: content for name, content in self.partitions.items() if name != 'system'})
        edits = self.builds[0]['edits']
        self.assertEqual(edits[prepare.SHARED_FLUTTER],
                         (self.latest, 0o644, 'u:object_r:system_lib_file:s0'))
        self.assertEqual(edits['product/overlay/HyperOSAVDSettingsDefaults/SettingsDefaults.apk'][0],
                         b'signed accepted RRO')
        self.assertTrue(edits['system/build.prop'][0].startswith(b'accepted OTA properties\n'))
        self.assertTrue(edits['system_ext/etc/init/init.hyperos_avd.rc'][0].startswith(
            b'accepted OTA boot services\n'))
        self.profile_mock.assert_called_once_with(self.engine)
        self.patch_mock.assert_called_once_with(self.engine)
        info = json.loads((self.output / 'local/build.json').read_text())
        for key in ('hyperos', 'archive_sha256', 'source', 'identity_source_sha256',
                    'rear_display', 'boot_service_fix', 'flutter_render_fix'):
            self.assertEqual(info[key], self.info[key])
        self.assertEqual(info['flutter_engine'],
                         {'before': self.selected['pins']['flutter'], 'after': digest(self.latest)})
        self.assertEqual(info['system_sha256'], digest(b'updated accepted packed image'))
        self.assertEqual(info['raw_sha256'], digest(b'updated accepted raw system'))

    def test_stale_cache_cannot_replace_the_accepted_system_or_vendor(self):
        (self.source / 'work/base').mkdir(parents=True)
        for path in ('hyperos-system.img', 'vendor.img', 'base/system_dlkm.img'):
            (self.source / 'work' / path).write_bytes(b'stale rejected cache')
        original = self.source_files()
        self.mocked_pipeline()
        prepare.prepare(self.source, self.output)
        self.assertEqual(self.source_files(), original)
        self.assertNotIn(b'stale rejected cache', dict(self.packs[0]).values())

    def test_packed_receipt_mismatch_rejects_before_output_or_build(self):
        self.info['system_sha256'] = digest(b'another accepted image')
        self.write_info()
        self.mocked_pipeline()
        with self.assertRaisesRegex(RuntimeError, 'accepted release receipt'):
            prepare.prepare(self.source, self.output)
        self.assertFalse(self.output.exists())
        self.assertEqual(self.builds, [])
        self.assertEqual(self.packs, [])

    def test_missing_packed_receipt_rejects_before_output_or_build(self):
        del self.info['system_sha256']
        self.write_info()
        self.mocked_pipeline()
        with self.assertRaisesRegex(RuntimeError, 'accepted release receipt'):
            prepare.prepare(self.source, self.output)
        self.assertFalse(self.output.exists())
        self.assertEqual(self.builds, [])

    def test_engine_from_another_known_ota_rejects_before_patching_or_build(self):
        self.input_engine_pin = phone_profile.profile()['pins']['flutter']
        original = self.source_files()
        self.mocked_pipeline()
        with self.assertRaisesRegex(RuntimeError, 'original OTA profile'):
            prepare.prepare(self.source, self.output)
        self.assertEqual(self.source_files(), original)
        self.patch_mock.assert_not_called()
        self.defaults_mock.assert_not_called()
        self.assertEqual(self.builds, [])
        self.assertEqual(self.packs, [])

    def test_already_current_engine_is_idempotent(self):
        self.latest = self.engine
        self.mocked_pipeline()
        prepare.prepare(self.source, self.output)
        self.assertEqual(self.builds[0]['edits'][prepare.SHARED_FLUTTER][0], self.engine)
        info = json.loads((self.output / 'local/build.json').read_text())
        self.assertEqual(info['flutter_engine']['after'], digest(self.engine))


class SharedFlutterPreparationTests(unittest.TestCase):
    def test_real_previous_engine_migrates_to_the_current_verified_output(self):
        original_pin = phone_profile.profile('4.0.18.0.XFRCNXM')['pins']['flutter']
        profile = patch_flutter.PROFILES[original_pin]
        path = Path(os.environ.get('HYPEROS_AVD_FLUTTER18_ARTIFACT', str(
            REPO / 'work/os4-r3-build/input/hongkong-4.0.18/flutter.bin')))
        if not path.is_file():
            self.skipTest('Original proprietary OTA engine is not distributed in Git.')
        original = path.read_bytes()
        self.assertEqual(digest(original), original_pin)
        current = patch_flutter.patch(original)
        previous = bytearray(current)
        offset = profile['glyph_raster_site']
        previous[offset:offset + 4] = original[offset:offset + 4]
        self.assertIn(digest(previous), profile['legacy'])
        fixed, receipt = prepare.refresh_shared_flutter(bytes(previous),
            phone_profile.profile('4.0.18.0.XFRCNXM'))
        self.assertEqual(fixed, current)
        self.assertEqual(receipt, {'before': original_pin, 'after': profile['output']})
        self.assertEqual(prepare.refresh_shared_flutter(fixed,
            phone_profile.profile('4.0.18.0.XFRCNXM')), (fixed, receipt))

    def test_unknown_shared_engine_retains_the_existing_patcher_guard(self):
        with self.assertRaisesRegex(RuntimeError, 'Unsupported Flutter engine'):
            prepare.refresh_shared_flutter(b'unverified engine', phone_profile.profile())


if __name__ == '__main__':
    unittest.main()
