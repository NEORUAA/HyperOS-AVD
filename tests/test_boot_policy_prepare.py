"""Verify data-neutral packed boot-policy staging without proprietary images."""
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import os4_boot_policy as boot
import os4_pad
import patch_boot_services as services
import patch_init_capabilities as gates
import phone_profile
import prepare_release_image as prepare


def digest(data):
    return hashlib.sha256(data).hexdigest()


class SharedBootEditsTests(unittest.TestCase):
    def test_final_graph_uses_existing_firmware_edits_and_preserves_prior_repairs(self):
        raw, vendor, work = Path('/accepted/system.img'), Path('/accepted/vendor.img'), Path('/policy')
        init, policy = b'previous firmware init edits', b'previous firmware policy edits'
        edits = {boot.INIT_PATH: (init, 0o644, 'preserved-label'),
                 boot.POLICY_PATH: (policy, 0o644, 'preserved-label'),
                 'system/framework/accepted.jar': (b'existing DEX repair', 0o644, 'preserved-label')}
        original = edits.copy()
        graph = {boot.INIT_PATH: init, gates.QTI_SCRIPT: b'final QTI script'}
        extra = {'system/etc/hyperos-init-capabilities.sh': (b'boot helper', 0o755, 'system-file-label')}
        receipt = {'schema': 1, 'targets': {'fixture': {'after': 'verified'}}}

        def init_files(partitions, replacements):
            self.assertEqual(partitions, [('', raw), ('vendor', vendor)])
            self.assertEqual(replacements, original)
            return graph

        with patch.object(boot, 'init_files', side_effect=init_files) as reader, \
                patch.object(boot, 'image_replacements', return_value=(extra, receipt)) as bundle, \
                patch.object(prepare, 'erofs', return_value=b'original raw bytes'):
            result = prepare.shared_boot_edits(raw, vendor, edits, work)
        reader.assert_called_once()
        bundle.assert_called_once_with(graph, b'final QTI script', init, policy, work)
        self.assertEqual(result, receipt)
        self.assertEqual(edits, dict(original, **extra))

    def test_init_graph_includes_every_accepted_logical_partition(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            for name in ('system', 'vendor', 'odm', 'vendor_dlkm'):
                (folder / (name + '.img')).write_bytes(('original ' + name).encode())
            edits = {}
            receipt = {'schema': 1, 'targets': {'fixture': {}}}
            with patch.object(boot, 'init_files', return_value={}) as reader, \
                    patch.object(boot, 'image_replacements', return_value=({}, receipt)), \
                    patch.object(prepare, 'erofs', return_value=b'original bytes'):
                self.assertEqual(prepare.shared_boot_edits(folder / 'system.img', folder / 'vendor.img',
                                                          edits, folder / 'policy'), receipt)
            reader.assert_called_once_with([('', folder / 'system.img'), ('vendor', folder / 'vendor.img'),
                                            ('odm', folder / 'odm.img'),
                                            ('vendor_dlkm', folder / 'vendor_dlkm.img')], {})

    def test_retained_kernel_helper_is_migrated_only_with_known_input_and_source_hashes(self):
        import apply_boot_service_fix as legacy
        old, current = b'known old helper', b'known current helper'
        init = services.BOOT_INIT
        helper = 'system/etc/hyperos-kernel-services.sh'
        with tempfile.TemporaryDirectory() as temporary:
            script = Path(temporary) / 'current.sh'
            script.write_bytes(current)
            for old_input, source_pin, error in ((old, digest(current), None),
                                                (b'unknown old helper', digest(current), 'existing image kernel helper'),
                                                (old, digest(b'unknown source'), 'image kernel helper source')):
                with self.subTest(error=error):
                    edits = {boot.INIT_PATH: (init, 0o644, 'old-label'),
                             helper: (old_input, 0o755, 'old-label')}
                    with patch.object(boot, 'init_files', return_value={boot.INIT_PATH: init}), \
                            patch.object(boot, 'image_replacements', return_value=({}, {'schema': 1})) as bundle, \
                            patch.object(prepare, 'erofs', return_value=b'raw bytes'), \
                            patch.object(legacy, 'KERNEL_SCRIPT_HASHES', (digest(old), digest(current))), \
                            patch.object(legacy, 'KERNEL_SCRIPT_SHA256', source_pin), \
                            patch.object(legacy, 'PERF_SCRIPT', script):
                        if error is None:
                            prepare.shared_boot_edits(Path('/raw/system.img'), Path('/raw/vendor.img'),
                                                      edits, Path('/work'))
                            self.assertEqual(edits[helper], (current, 0o755, 'u:object_r:system_file:s0'))
                            bundle.assert_called_once()
                        else:
                            with self.assertRaisesRegex(RuntimeError, error):
                                prepare.shared_boot_edits(Path('/raw/system.img'), Path('/raw/vendor.img'),
                                                          edits, Path('/work'))
                            self.assertEqual(edits[helper][0], old_input)
                            bundle.assert_not_called()

    def test_kernel_helper_tab_duplicate_or_foreign_owner_refuses_before_bundle(self):
        canonical = services.BOOT_INIT
        tabbed = canonical.replace(b'service hyperos-kernel-services ', b'service\thyperos-kernel-services\t')
        folded = canonical.replace(b'service hyperos-kernel-services ', b'service \\\n    hyperos-kernel-services ')
        quoted = canonical.replace(b'service hyperos-kernel-services ', b'"service" "hyperos-kernel-services" ')
        cases = [(tabbed, {boot.INIT_PATH: tabbed}),
                 (canonical + canonical, {boot.INIT_PATH: canonical + canonical}),
                 (canonical, {boot.INIT_PATH: canonical, 'vendor/etc/init/foreign.rc': tabbed}),
                 (canonical, {boot.INIT_PATH: canonical, 'product/routes/hidden.conf': tabbed}),
                 (canonical, {boot.INIT_PATH: canonical, 'product/routes/hidden.conf': folded}),
                 (canonical, {boot.INIT_PATH: canonical, 'product/routes/hidden.conf': quoted})]
        for init, graph in cases:
            with self.subTest(graph=graph):
                edits = {boot.INIT_PATH: (init, 0o644, 'preserved-label')}
                with patch.object(boot, 'init_files', return_value=graph), \
                        patch.object(boot, 'image_replacements') as bundle, \
                        patch.object(prepare, 'erofs', return_value=b'raw bytes'):
                    with self.assertRaisesRegex(RuntimeError, 'Unknown existing image kernel helper service'):
                        prepare.shared_boot_edits(Path('/raw/system.img'), Path('/raw/vendor.img'),
                                                  edits, Path('/work'))
                    self.assertEqual(edits[boot.INIT_PATH][0], init)
                    bundle.assert_not_called()


class PackedBootPolicyPreparationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.source, self.output = self.root / 'source', self.root / 'output'
        for directory in ('images', 'local', 'avd', 'config', 'tools'):
            (self.source / directory).mkdir(parents=True)
        self.packed = self.source / 'images/system.img'
        self.packed.write_bytes(b'original packed firmware')
        for path, data in {'avd/userdata-qemu.img.qcow2': b'encrypted private user data',
                           'avd/encryptionkey.img': b'private encryption key',
                           'config/avd.ini': b'custom device settings',
                           'tools/owned-helper': b'original executable'}.items():
            (self.source / path).write_bytes(data)
        value = os4_pad.PROFILE
        self.info = {'source': os4_pad.SOURCE, 'device': value['device'], 'hyperos': value['hyperos'],
                     'archive_sha256': value['source_archive_sha256'],
                     'display': value['display'],
                     'model_xml_sha256': value['model_xml_sha256'],
                     'identity_source_sha256': value['source_sha256'],
                     'system_sha256': digest(self.packed.read_bytes()), 'retained': {'fixture': True}}
        self.partitions = {'system': b'original raw system', 'vendor': b'original vendor',
                           'system_dlkm': b'original modules', 'vendor_dlkm': b'additional logical partition',
                           'odm': b'additional OEM logical partition'}
        self.receipt = {'schema': 1, 'targets': {'fixture': {'after': 'known'}}}
        self.edits = {'system/etc/hyperos-init-capabilities.sh':
                      (b'audited capability helper', 0o755, 'u:object_r:system_file:s0')}
        self.builds, self.packs, self.boot_inputs = [], [], []
        self.write_info()

    def write_info(self):
        (self.source / 'local/build.json').write_text(json.dumps(self.info))

    def source_files(self):
        return {str(path.relative_to(self.source)): path.read_bytes()
                for path in self.source.rglob('*') if path.is_file()}

    def pipeline(self, *, corrupt_read=False, mutate_source=False):
        stack = ExitStack()
        self.addCleanup(stack.close)

        def unpack(source, destination):
            self.assertEqual(source, self.packed)
            self.assertEqual(destination.parent.parent, self.output)
            destination.mkdir(parents=True)
            for name, data in self.partitions.items():
                (destination / (name + '.img')).write_bytes(data)

        def read(image, path):
            if image.name == 'system-fixed.img':
                return b'incorrect candidate data' if corrupt_read else self.builds[-1]['edits'][path.lstrip('/')][0]
            self.assertEqual(image.name, 'system.img')
            if path == '/' + boot.INIT_PATH:
                return b'original init\n'
            if path == '/' + boot.POLICY_PATH:
                return b'original policy\n'
            self.assertIn(path, {entry[0] for entry in services.TARGETS.values()})
            return ('original DEX target ' + path).encode()

        def shared(raw, vendor, edits, work):
            self.assertEqual(raw.parent.name, 'accepted')
            self.assertEqual(vendor, raw.with_name('vendor.img'))
            self.assertEqual(work, raw.parent.parent / 'policy')
            self.boot_inputs.append(edits.copy())
            edits.update(self.edits)
            return self.receipt

        def build(destination, partitions, tree, edits):
            self.assertEqual(partitions, [('', destination.parent / 'accepted/system.img')])
            self.builds.append({'edits': edits.copy(), 'partitions': partitions})
            destination.write_bytes(b'updated raw firmware')

        def pack(source, destination, partitions):
            self.assertEqual(source, self.packed)
            self.packs.append([(name, path.read_bytes()) for name, path in partitions])
            destination.write_bytes(b'updated packed firmware')
            if mutate_source:
                self.packed.write_bytes(b'concurrent firmware replacement')

        self.unpack_mock = stack.enter_context(patch.object(prepare, 'unpack', side_effect=unpack))
        stack.enter_context(patch.object(prepare, 'erofs', side_effect=read))
        self.boot_mock = stack.enter_context(patch.object(prepare, 'shared_boot_edits', side_effect=shared))
        self.build_mock = stack.enter_context(patch.object(prepare, 'build', side_effect=build))
        self.pack_mock = stack.enter_context(patch.object(prepare, 'pack', side_effect=pack))
        return stack

    def test_stage_pins_original_firmware_retains_all_partitions_and_never_copies_userdata(self):
        original = self.source_files()
        self.pipeline()
        prepare.prepare_boot_policy(self.source, self.output)
        self.assertEqual(self.source_files(), original)
        self.assertEqual({str(path.relative_to(self.output)) for path in self.output.rglob('*') if path.is_file()},
                         {'images/system.img', 'local/build.json', 'local/boot-policy-source.json'})
        self.assertFalse(list(self.output.glob('boot-policy-*')))
        packed = dict(self.packs[0])
        self.assertEqual(packed['system'], b'updated raw firmware')
        self.assertEqual({name: packed[name] for name in self.partitions if name != 'system'},
                         {name: data for name, data in self.partitions.items() if name != 'system'})
        self.assertEqual(self.builds[0]['edits'], self.edits)
        value = json.loads((self.output / 'local/build.json').read_text())
        self.assertEqual(value['retained'], self.info['retained'])
        self.assertEqual(value['boot_policy'], self.receipt)
        self.assertEqual(value['system_sha256'], digest(b'updated packed firmware'))
        self.assertEqual(value['raw_sha256'], digest(b'updated raw firmware'))
        proof = json.loads((self.output / 'local/boot-policy-source.json').read_text())
        self.assertEqual(proof, {'system_sha256': digest(b'original packed firmware'),
                                 'boot_policy': self.receipt})

    def test_owned_original_pad_without_packed_receipt_still_records_snapshot_pin(self):
        del self.info['system_sha256']
        self.write_info()
        original = self.source_files()
        self.pipeline()
        prepare.prepare_boot_policy(self.source, self.output)
        self.assertEqual(self.source_files(), original)
        proof = json.loads((self.output / 'local/boot-policy-source.json').read_text())
        self.assertEqual(proof['system_sha256'], digest(b'original packed firmware'))

    def test_present_packed_receipt_mismatch_refuses_before_output_or_unpack(self):
        self.info['system_sha256'] = digest(b'foreign image')
        self.write_info()
        self.pipeline()
        with self.assertRaisesRegex(RuntimeError, 'differs from its build receipt'):
            prepare.prepare_boot_policy(self.source, self.output)
        self.assertFalse(self.output.exists())
        self.unpack_mock.assert_not_called()

    def test_foreign_source_refuses_before_output_or_unpack(self):
        self.info['source'] = 'foreign-workspace'
        self.write_info()
        self.pipeline()
        with self.assertRaises(RuntimeError):
            prepare.prepare_boot_policy(self.source, self.output)
        self.assertFalse(self.output.exists())
        self.unpack_mock.assert_not_called()

    def test_unknown_pad_version_or_identity_refuses_before_output_or_unpack(self):
        self.pipeline()
        original = self.info.copy()
        for key, value in (('hyperos', 'OS4.999.0.0.UNKNOWN'), ('archive_sha256', 'unknown'),
                           ('model_xml_sha256', 'unknown'), ('identity_source_sha256', {}),
                           ('display', {'width': 100, 'height': 100, 'density': 100})):
            with self.subTest(key=key):
                self.info = dict(original, **{key: value})
                self.write_info()
                with self.assertRaisesRegex(RuntimeError, 'source-pinned official yingtian'):
                    prepare.prepare_boot_policy(self.source, self.output)
                self.assertFalse(self.output.exists())
        self.unpack_mock.assert_not_called()

    def test_phone_profile_is_always_validated_even_when_legacy_fixes_already_exist(self):
        self.info.update(source='official-hongkong-ota', hyperos='4.999.0.0.UNKNOWN',
                         boot_service_fix={'accepted': True})
        self.write_info()
        self.pipeline()
        with self.assertRaisesRegex(RuntimeError, 'Unsupported official hongkong firmware'):
            prepare.prepare_boot_policy(self.source, self.output)
        self.assertFalse(self.output.exists())
        self.unpack_mock.assert_not_called()

    def test_same_nested_and_existing_workspaces_are_rejected_before_mutation(self):
        original = self.source_files()
        self.pipeline()
        for output in (self.source, self.source / 'nested', self.root, self.source / 'images'):
            with self.subTest(output=output), self.assertRaisesRegex(RuntimeError, 'new separate sibling'):
                prepare.prepare_boot_policy(self.source, output)
        self.assertEqual(self.source_files(), original)
        self.unpack_mock.assert_not_called()

    def test_candidate_verification_failure_prevents_pack_or_acceptance_receipts(self):
        original = self.source_files()
        self.pipeline(corrupt_read=True)
        with self.assertRaisesRegex(RuntimeError, 'Boot policy image verification failed'):
            prepare.prepare_boot_policy(self.source, self.output)
        self.assertEqual(self.source_files(), original)
        self.pack_mock.assert_not_called()
        self.assertFalse((self.output / 'local/build.json').exists())
        self.assertFalse((self.output / 'local/boot-policy-source.json').exists())
        self.assertFalse(list(self.output.glob('boot-policy-*')))

    def test_source_hash_is_rechecked_after_packing_before_acceptance_receipts(self):
        private = {path: self.source_files()[path] for path in ('avd/userdata-qemu.img.qcow2', 'avd/encryptionkey.img')}
        self.pipeline(mutate_source=True)
        with self.assertRaisesRegex(RuntimeError, 'Source firmware changed'):
            prepare.prepare_boot_policy(self.source, self.output)
        self.assertFalse((self.output / 'local/build.json').exists())
        self.assertFalse((self.output / 'local/boot-policy-source.json').exists())
        self.assertFalse(list(self.output.glob('boot-policy-*')))
        for path, data in private.items():
            self.assertEqual((self.source / path).read_bytes(), data)

    def test_original_phone_stage_preserves_all_four_pinned_dex_repairs_before_shared_policy(self):
        selected = phone_profile.profile('4.0.18.0.XFRCNXM')
        self.info.update(source='official-hongkong-ota', device='hongkong', hyperos=selected['hyperos'],
                         archive_sha256=selected['archive_sha256'])
        self.write_info()
        extra = {path.lstrip('/'): (('patched ' + name).encode(), 0o644, 'u:object_r:system_file:s0')
                 for name, (path, _, _) in services.TARGETS.items()}
        legacy = {'targets': {name: {'after': 'known'} for name in services.TARGETS}}
        original = self.source_files()
        self.pipeline()
        with patch.object(services, 'image_replacements', return_value=(extra, legacy)) as legacy_patch:
            prepare.prepare_boot_policy(self.source, self.output)
        self.assertEqual(self.source_files(), original)
        self.assertEqual(set(legacy_patch.call_args.args[0]), set(services.TARGETS))
        shared_input = self.boot_inputs[0]
        for path, edit in extra.items():
            self.assertEqual(shared_input[path], edit)
            self.assertEqual(self.builds[0]['edits'][path], edit)
        self.assertEqual(shared_input[boot.INIT_PATH][0], b'original init\n' + services.BOOT_INIT)
        self.assertEqual(shared_input[boot.POLICY_PATH][0], b'original policy\n' + services.BOOT_SEPOLICY)
        value = json.loads((self.output / 'local/build.json').read_text())
        self.assertEqual(value['boot_service_fix'], legacy)
        self.assertEqual(value['boot_policy'], self.receipt)


if __name__ == '__main__':
    unittest.main()
