"""Verify candidate isolation and complete LP retention without large images."""
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import build_image
import common
import erofs_image
import os4_defaults
import packed_source
import patch_flutter
import phone_profile
import preinstall_os4_apps


def digest(data):
    return hashlib.sha256(data).hexdigest()


class PackedCandidateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve() / 'renamed-workspace'
        (self.root / 'images').mkdir(parents=True)
        (self.root / 'local').mkdir()
        (self.root / 'avd/Custom.avd').mkdir(parents=True)
        (self.root / 'avd/Custom.avd/userdata-qemu.img').write_bytes(b'private userdata')
        self.packed = self.root / 'images/system.img'
        self.packed.write_bytes(b'current accepted packed image')
        self.profile = phone_profile.profile('4.0.18.0.XFRCNXM')
        self.info = {'source': 'official-hongkong-ota', 'hyperos': self.profile['hyperos'],
                     'archive_sha256': self.profile['archive_sha256'],
                     'system_sha256': digest(self.packed.read_bytes())}
        self.receipt = self.root / 'local/build.json'
        self.write_info()
        # Metadata order deliberately differs from both alphabetical and legacy order.
        self.partitions = {'vendor_dlkm': b'accepted vendor modules', 'system': b'accepted merged system',
                           'odm': b'accepted OEM extra partition', 'vendor': b'accepted vendor',
                           'system_dlkm': b'accepted modules'}
        self.packs, self.builds = [], []

    def write_info(self):
        self.receipt.write_text(json.dumps(self.info))

    def source_files(self):
        return {str(path.relative_to(self.root)): path.read_bytes()
                for path in self.root.rglob('*') if path.is_file() and 'work' not in path.relative_to(self.root).parts}

    def pipeline(self, *, mutate_source=False, corrupt_candidate=False):
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.read_lp = stack.enter_context(patch.object(packed_source, 'read_lp', return_value=(
            0, [{'name': name, 'attributes': 1} for name in self.partitions])))

        def unpack(source, folder):
            self.assertEqual(source, self.packed)
            folder.mkdir()
            for name, data in self.partitions.items():
                (folder / (name + '.img')).write_bytes(data)

        def pack(source, target, partitions):
            self.assertEqual(source, self.packed)
            self.packs.append([(name, path.read_bytes()) for name, path in partitions])
            target.write_bytes(b'new packed candidate')
            if mutate_source:
                self.packed.write_bytes(b'concurrent replacement')

        def build(target, sources, tree, edits, **kwargs):
            self.assertEqual(sources[0][1].read_bytes(), self.partitions['system'])
            self.assertEqual(sources[0][1].parent.name, 'accepted')
            self.builds.append(edits.copy())
            tree.mkdir()
            (tree / 'temporary-cache').write_bytes(b'clean me')
            target.write_bytes(b'new raw candidate')

        def erofs(image, path):
            if image.name == 'hyperos-system.img':
                return b'corrupted' if corrupt_candidate else self.builds[-1][path.lstrip('/')][0]
            self.assertEqual(image.parent.name, 'accepted')
            if path == '/product/etc/device_features/hongkong.xml':
                return Path(self.profile['model_xml']).read_bytes()
            if path == '/product/priv-app/MIUIFindDeviceCN/MIUIFindDeviceCN.apk':
                return b'factory FindDevice'
            return b'current source file bytes'

        stack.enter_context(patch.object(packed_source, 'unpack', side_effect=unpack))
        stack.enter_context(patch.object(packed_source, 'pack', side_effect=pack))
        stack.enter_context(patch.object(common, 'ROOT', self.root))
        stack.enter_context(patch.object(common, 'sdk_path', return_value=self.root / 'sdk'))
        stack.enter_context(patch.object(erofs_image, 'build', side_effect=build))
        stack.enter_context(patch.object(build_image, 'erofs', side_effect=erofs))
        stack.enter_context(patch.object(os4_defaults, 'image_replacements', return_value=(
            {'product/etc/test-marker': (b'new marker', 0o644, 'u:object_r:system_file:s0')}, {'fixture': True})))
        selected = {**self.profile, 'pins': {**self.profile['pins'], 'finddevice_apk': digest(b'factory FindDevice')}}
        stack.enter_context(patch.object(phone_profile, 'profile_from_build', return_value=selected))
        stack.enter_context(patch.object(os4_defaults, 'production_properties', side_effect=lambda data, *_: data + b'props'))
        stack.enter_context(patch.object(os4_defaults, 'boot_defaults', side_effect=lambda data, *_: data + b'init'))
        stack.enter_context(patch.object(preinstall_os4_apps, 'replacements', return_value=(
            {'product/app/Test/Test.apk': (b'signed APK', 0o644, 'u:object_r:system_file:s0')}, {'apps': ['fixture']})))
        stack.enter_context(patch.object(patch_flutter, 'patch', return_value=b'patched shared engine'))

    def verify_candidate(self, kind):
        folder = self.root / 'work' / (kind + '-candidate')
        self.assertEqual({path.name for path in folder.iterdir()}, {'system.img', 'hyperos-system.img', 'manifest.json'})
        self.assertFalse(list((self.root / 'work').glob('.*-candidate-*')))
        manifest = json.loads((folder / 'manifest.json').read_text())
        self.assertEqual(manifest['source_packed_sha256'], self.info['system_sha256'])
        self.assertEqual(manifest['logical_partitions'], list(self.partitions))
        self.assertEqual([name for name, _ in self.packs[0]], list(self.partitions))
        packed = dict(self.packs[0])
        self.assertEqual(packed['system'], b'new raw candidate')
        self.assertEqual({name: value for name, value in packed.items() if name != 'system'},
                         {name: value for name, value in self.partitions.items() if name != 'system'})

    def test_defaults_uses_current_packed_source_without_work_cache(self):
        before = self.source_files()
        self.pipeline()
        os4_defaults.prepare_image()
        self.verify_candidate('defaults')
        self.assertEqual(self.source_files(), before)

    def test_defaults_keeps_accepted_overlay_signature_and_checks_its_receipt(self):
        self.info['avd_defaults'] = {'settings_overlay_sha256': digest(b'current source file bytes')}
        self.write_info()
        self.pipeline()
        os4_defaults.prepare_image()
        path = 'product/overlay/HyperOSAVDSettingsDefaults/SettingsDefaults.apk'
        self.assertEqual(self.builds[0][path][0], b'current source file bytes')
        receipt = json.loads((self.root / 'work/defaults-candidate/manifest.json').read_text())
        self.assertEqual(receipt['defaults']['settings_overlay_sha256'], self.info['avd_defaults']['settings_overlay_sha256'])

    def test_unknown_lp_attributes_are_rejected_without_unpacking(self):
        self.pipeline()
        for value in (None, 0, 2, 3, True):
            with self.subTest(attributes=value):
                self.read_lp.return_value = (0, [{'name': 'system', 'attributes': value}])
                with self.assertRaisesRegex(RuntimeError, 'partition attributes'):
                    os4_defaults.prepare_image()
        self.assertEqual(self.builds, [])
        self.assertFalse((self.root / 'work').exists())

    def test_dangling_candidate_alias_is_preserved(self):
        self.pipeline()
        folder = self.root / 'work/defaults-candidate'
        folder.parent.mkdir()
        missing = self.root / 'outside-candidate'
        folder.symlink_to(missing)
        with self.assertRaisesRegex(RuntimeError, 'already exists'):
            os4_defaults.prepare_image()
        self.assertTrue(folder.is_symlink())
        self.assertFalse(missing.exists())
        self.read_lp.assert_not_called()

    def test_preinstall_ignores_stale_raw_and_vendor_caches(self):
        work = self.root / 'work'
        work.mkdir()
        (work / 'hyperos-system.img').write_bytes(b'stale system')
        (work / 'vendor.img').write_bytes(b'stale vendor')
        before = self.source_files()
        self.pipeline()
        preinstall_os4_apps.prepare_image(self.root / 'apps', self.root / 'sdk')
        self.verify_candidate('preinstalled')
        self.assertEqual((work / 'hyperos-system.img').read_bytes(), b'stale system')
        self.assertEqual((work / 'vendor.img').read_bytes(), b'stale vendor')
        self.assertEqual(self.source_files(), before)

    def test_stale_build_or_installed_receipt_rejects_before_unpack(self):
        self.pipeline()
        self.info['system_sha256'] = digest(b'stale packed image')
        self.write_info()
        with self.assertRaisesRegex(RuntimeError, 'build receipt'):
            os4_defaults.prepare_image()
        self.read_lp.assert_not_called()
        self.info['system_sha256'] = digest(self.packed.read_bytes())
        self.write_info()
        (self.root / 'local/installed-release.json').write_text(json.dumps({
            'project': 'HyperOS-AVD', 'files': {'images/system.img': {'sha256': digest(b'foreign')}}}))
        with self.assertRaisesRegex(RuntimeError, 'installed receipt'):
            os4_defaults.prepare_image()
        self.read_lp.assert_not_called()
        self.assertFalse((self.root / 'work').exists())

    def test_source_change_or_failed_verification_never_publishes_candidate(self):
        for mutation in ('source', 'candidate'):
            with self.subTest(mutation=mutation):
                self.packed.write_bytes(b'current accepted packed image')
                self.pipeline(mutate_source=mutation == 'source', corrupt_candidate=mutation == 'candidate')
                with self.assertRaises(RuntimeError):
                    preinstall_os4_apps.prepare_image(self.root / 'apps', self.root / 'sdk')
                self.assertFalse((self.root / 'work/preinstalled-candidate').exists())
                self.assertFalse(list((self.root / 'work').glob('.*-candidate-*')))
                self.assertEqual((self.root / 'avd/Custom.avd/userdata-qemu.img').read_bytes(), b'private userdata')

    def test_preexisting_candidate_is_preserved(self):
        self.pipeline()
        destination = self.root / 'work/defaults-candidate'
        destination.mkdir(parents=True)
        (destination / 'manifest.json').write_bytes(b'previous candidate')
        with self.assertRaisesRegex(RuntimeError, 'already exists'):
            os4_defaults.prepare_image()
        self.assertEqual((destination / 'manifest.json').read_bytes(), b'previous candidate')
        self.read_lp.assert_not_called()

    def test_pinned_local_builder_without_image_digest_records_active_snapshot(self):
        del self.info['system_sha256']
        self.write_info()
        info, source, source_hash, _ = packed_source.source_info(self.root, 'official-hongkong-ota')
        self.assertEqual(info, self.info)
        self.assertEqual(source, self.packed)
        self.assertEqual(source_hash, digest(self.packed.read_bytes()))

    def test_unknown_source_version_archive_and_malformed_installed_receipt_are_refused(self):
        original = self.info.copy()
        for key, value in (('source', 'foreign'), ('hyperos', '4.999.0.0.UNKNOWN'),
                           ('archive_sha256', 'unknown'), ('archive_sha256', None)):
            with self.subTest(key=key, value=value):
                self.info = {**original, key: value}
                self.write_info()
                with self.assertRaises(RuntimeError):
                    packed_source.source_info(self.root)
        self.info = original
        self.write_info()
        installed = self.root / 'local/installed-release.json'
        for data in ('{broken', '[]', '{}'):
            with self.subTest(data=data):
                installed.write_text(data)
                with self.assertRaisesRegex(RuntimeError, 'Invalid installed'):
                    packed_source.source_info(self.root)

    def test_invalid_logical_partition_names_are_refused_before_unpack_or_output(self):
        for names in (['system', 'system'], ['vendor'], ['system', '../extra'], ['system', []]):
            with self.subTest(names=names), \
                    patch.object(packed_source, 'read_lp', return_value=(0, [{'name': name} for name in names])), \
                    patch.object(packed_source, 'unpack') as unpack:
                with self.assertRaisesRegex(RuntimeError, 'logical partition names'):
                    with packed_source.packed_candidate(self.root, self.root / 'work/candidate'):
                        self.fail('Invalid LP metadata must not reach a builder.')
                unpack.assert_not_called()
                self.assertFalse((self.root / 'work').exists())

    def test_changed_or_new_receipt_after_unpack_never_publishes_candidate(self):
        self.pipeline()
        destination = self.root / 'work/candidate'
        with self.assertRaisesRegex(RuntimeError, 'receipt changed'):
            with packed_source.packed_candidate(self.root, destination) as candidate:
                raw = candidate.work / 'patched-system.img'
                raw.write_bytes(b'candidate')
                (self.root / 'local/installed-release.json').write_text('{}')
                candidate.finish(raw, {})
        self.assertFalse(destination.exists())
        self.assertFalse(list((self.root / 'work').glob('.candidate-*')))
        self.assertEqual(self.packs, [])


if __name__ == '__main__':
    unittest.main()
