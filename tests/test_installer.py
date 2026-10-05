"""Check image import integrity and isolation with small synthetic release files."""
import hashlib
import copy
import io
import json
from pathlib import Path
import socket
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import common
import setup
import package_release


class InstallerTests(unittest.TestCase):
    def instance_profile(self, root, source, name='Custom_instance', port=5580):
        (root / 'local').mkdir(parents=True)
        (root / 'local/build.json').write_text(json.dumps({
            'source': source, 'android_api': 36 if source == 'os3' else 37}))
        (root / 'local/runtime.json').write_text(json.dumps({'name': name, 'port': port}))

    def fixture(self, root, member='images/system.img'):
        payload = b'test firmware'
        asset = root / 'test.part001'
        with tarfile.open(asset, 'w:gz') as archive:
            info = tarfile.TarInfo(member)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
        manifest = {'project': 'HyperOS-AVD', 'format': 1, 'version': 'vtest',
                    'platform': 'macos-arm64', 'parts': [{'name': asset.name,
                    'size': asset.stat().st_size, 'sha256': common.sha256(asset)}],
                    'files': {member: {'size': len(payload), 'sha256': hashlib.sha256(payload).hexdigest()}}}
        path = root / 'manifest.json'
        path.write_text(json.dumps(manifest))
        return path, asset

    def os4_fixture(self, root, template=None):
        entries = {'images/system.img': b'official test firmware',
                   'config/avd.ini': template or b'target=android-37.0\nhw.cpu.arch=arm64\n'}
        asset = root / 'os4.part001'
        with tarfile.open(asset, 'w:gz') as archive:
            for name, payload in entries.items():
                info = tarfile.TarInfo(name)
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))
        build = {'source': common.OS4_SOURCE, 'hyperos': '4.0.17.0.XFRCNXM',
                 'android_api': 37, 'adb_authentication': True}
        manifest = {'project': 'HyperOS-AVD', 'format': 2, 'version': 'vtest-os4',
                    'platform': 'macos-arm64', 'variant': 'os4-official',
                    'hyperos': build['hyperos'], 'android_api': 37, 'build': build,
                    'parts': [{'name': asset.name, 'size': asset.stat().st_size,
                               'sha256': common.sha256(asset)}],
                    'files': {name: {'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
                              for name, data in entries.items()}}
        path = root / 'manifest.json'
        path.write_text(json.dumps(manifest))
        return path, manifest

    def pad_fixture(self, root, template=None):
        path, manifest = self.os4_fixture(root, template or
            b'target=android-37.0\nhw.cpu.arch=arm64\nhw.ramSize=4096\nhw.cpu.ncore=4\ndisk.dataPartition.size=32G\n')
        manifest.update(format=3, variant='os4-pad', version='v0.3.0-a17-hyperos4-yingtian-r1',
                        source=setup.PAD_SOURCE, source_device='yingtian', hyperos=setup.PAD_HYPEROS)
        manifest['build'].update(source=setup.PAD_SOURCE, device='yingtian',
                                 hyperos=setup.PAD_HYPEROS, memory_limit_mib=4096)
        manifest['compatibility'] = {'userdata_family': setup.PAD_FAMILY,
                                     'minimum_installer': '1.1.0', 'runtime_in_bundle': True}
        path.write_text(json.dumps(manifest))
        return path, manifest

    def test_pad_manifest_requires_exact_source_schema_device_and_userdata_family(self):
        with tempfile.TemporaryDirectory() as d:
            path, manifest = self.pad_fixture(Path(d))
            self.assertEqual(setup.read_manifest(str(path))[0], manifest)
            mutations = [(('format',), 2), (('variant',), 'os4-official'),
                         (('source',), common.OS4_SOURCE), (('source_device',), 'hongkong'),
                         (('hyperos',), 'OS4.0.16.0.XBMCNXM'), (('android_api',), 36),
                         (('build', 'source'), common.OS4_SOURCE), (('build', 'device'), 'hongkong'),
                         (('build', 'android_api'), 36), (('build', 'adb_authentication'), False),
                         (('build', 'memory_limit_mib'), 6144),
                         (('compatibility', 'userdata_family'), 'os4-hongkong-api37-ranchu-4k'),
                         (('compatibility', 'minimum_installer'), '1.0.0'),
                         (('compatibility', 'runtime_in_bundle'), False)]
            for keys, value in mutations:
                invalid = copy.deepcopy(manifest)
                field = invalid
                for key in keys[:-1]:
                    field = field[key]
                field[keys[-1]] = value
                path.write_text(json.dumps(invalid))
                with self.subTest(keys=keys), self.assertRaises(RuntimeError):
                    setup.read_manifest(str(path))

    def test_pad_import_preserves_custom_userdata_and_writes_verified_build(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            path, manifest = self.pad_fixture(root)
            data = root / 'avd/My_Pad.avd/userdata-qemu.img'
            data.parent.mkdir(parents=True)
            data.write_bytes(b'private tablet data')
            with patch.object(setup, 'ROOT', root):
                setup.install_bundle(str(path))
            self.assertEqual(data.read_bytes(), b'private tablet data')
            self.assertEqual(json.loads((root / 'local/build.json').read_text()), manifest['build'])
            self.assertEqual((root / 'images/system.img').read_bytes(), b'official test firmware')

    def test_pad_six_gib_saved_choice_rejected_before_download_or_directory_write(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            path, _ = self.pad_fixture(folder)
            root = folder / 'installed'
            (root / 'local').mkdir(parents=True)
            (root / 'local/runtime.json').write_text(json.dumps({'hardware': {'hw.ramSize': '6144'}}))
            with patch.object(setup, 'ROOT', root), patch.object(setup.urllib.request, 'urlretrieve') as download:
                with self.assertRaisesRegex(RuntimeError, '4096'):
                    setup.install_bundle(str(path))
            download.assert_not_called()
            self.assertFalse((root / 'downloads').exists())
            self.assertFalse((root / 'images').exists())

    def test_pad_invalid_archive_ram_rejected_before_firmware_promotion(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            path, _ = self.pad_fixture(root,
                b'target=android-37.0\nhw.cpu.arch=arm64\nhw.ramSize=6144\n')
            image = root / 'images/system.img'
            image.parent.mkdir()
            image.write_bytes(b'original tablet image')
            with patch.object(setup, 'ROOT', root), self.assertRaisesRegex(RuntimeError, '4096'):
                setup.install_bundle(str(path))
            self.assertEqual(image.read_bytes(), b'original tablet image')
            self.assertFalse((root / 'local/build.json').exists())

    def test_pad_selects_isolated_root_and_keeps_custom_name_and_port(self):
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d).resolve()
            self.instance_profile(repo, 'os3', name='Kept_OS3', port=5586)
            original = (repo / 'local/runtime.json').read_bytes()
            with patch.object(setup, 'ROOT', repo), patch.object(setup, 'REPO_ROOT', repo), \
                    patch.object(common, 'ROOT', repo), patch.dict('os.environ', {}, clear=True):
                self.assertEqual(setup.select_release({'variant': 'os4-pad'}),
                                 ('HyperOS_4_Pad9ProMax_API_37', 5582))
                root = repo / 'work/os4-pad'
                self.assertEqual(setup.ROOT, root)
                self.assertFalse(root.exists())
                self.instance_profile(root, setup.PAD_SOURCE, name='My_Tablet', port=5584)
                self.assertEqual(setup.select_release({'variant': 'os4-pad'}), ('My_Tablet', 5584))
                self.assertEqual(setup.select_release({'variant': 'os4-pad'}, name='New_Label'),
                                 ('New_Label', 5584))
                with patch.dict('os.environ', {'HYPEROS_AVD_WORKSPACE': str(root)}):
                    with self.assertRaisesRegex(RuntimeError, 'does not match'):
                        setup.select_release({'variant': 'os4-official'})
            self.assertEqual((repo / 'local/runtime.json').read_bytes(), original)

    def test_pad_installed_manifest_identifies_profile_without_local_build(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / 'local').mkdir()
            (root / 'local/installed-release.json').write_text(json.dumps({
                'project': 'HyperOS-AVD', 'variant': 'os4-pad'}))
            self.assertEqual(setup.workspace_profile(root), setup.PAD_SOURCE)

    def test_os4_import_installs_boot_profile_and_preserves_userdata(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path, manifest = self.os4_fixture(root)
            data = root / 'avd/HyperOS_4_Official_API_37.avd/userdata-qemu.img'
            data.parent.mkdir(parents=True)
            data.write_bytes(b'personal OS4 data')
            with patch.object(setup, 'ROOT', root):
                setup.install_bundle(str(path))
            self.assertEqual(json.loads((root / 'local/build.json').read_text()), manifest['build'])
            self.assertEqual((root / 'config/avd.ini').read_bytes(), b'target=android-37.0\nhw.cpu.arch=arm64\n')
            self.assertEqual(data.read_bytes(), b'personal OS4 data')

    def test_nonportable_os4_profile_never_replaces_firmware(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path, _ = self.os4_fixture(root, b'target=android-37.0\nhw.cpu.arch=arm64\nimage.sysdir.1=/private/host/path\n')
            image = root / 'images/system.img'
            image.parent.mkdir()
            image.write_bytes(b'original')
            with patch.object(setup, 'ROOT', root), self.assertRaisesRegex(RuntimeError, 'nonportable'):
                setup.install_bundle(str(path))
            self.assertEqual(image.read_bytes(), b'original')

    def test_os4_selects_separate_root_and_supports_owned_custom_instances(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary).resolve()
            with patch.object(setup, 'ROOT', repo), patch.object(setup, 'REPO_ROOT', repo), \
                    patch.object(common, 'ROOT', repo), patch.dict('os.environ', {}, clear=True):
                self.assertEqual(setup.select_release({'variant': 'os4-official'}), (common.OS4_NAME, 5574))
                self.assertEqual(setup.ROOT, repo / 'work/os4-official')
                self.assertEqual(setup.select_release({'variant': 'os4-official'}, name='Custom_temp', port=5580), ('Custom_temp', 5580))
                setup.ROOT = repo / 'work/os4-official'
                (setup.ROOT / 'local').mkdir(parents=True)
                (setup.ROOT / 'local/build.json').write_text(json.dumps({'source': common.OS4_SOURCE}))
                with self.assertRaisesRegex(RuntimeError, 'does not match'):
                    setup.select_release({'format': 1})

    def test_same_profile_reuses_custom_instance_and_explicit_overrides(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary).resolve()
            for index, source in enumerate(('os3', common.OS4_SOURCE, 'official-yingtian-ota')):
                with self.subTest(source=source):
                    root = repo / str(index)
                    self.instance_profile(root, source)
                    before = (root / 'local/runtime.json').read_bytes()
                    with patch.object(setup, 'ROOT', root), patch.object(setup, 'REPO_ROOT', repo), \
                            patch.object(common, 'ROOT', root):
                        self.assertEqual(setup.select_release(None), ('Custom_instance', 5580))
                        self.assertEqual(setup.select_release(None, name='Explicit'), ('Explicit', 5580))
                        self.assertEqual(setup.select_release(None, port=5584), ('Custom_instance', 5584))
                        self.assertEqual(setup.select_release(None, name='Explicit', port=5584), ('Explicit', 5584))
                    self.assertEqual((root / 'local/runtime.json').read_bytes(), before)

    def test_legacy_os3_release_metadata_can_identify_custom_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / 'local').mkdir()
            (root / 'local/installed-release.json').write_text(json.dumps({
                'project': 'HyperOS-AVD', 'format': 1}))
            (root / 'local/runtime.json').write_text(json.dumps({'name': 'My_OS3', 'port': 5586}))
            with patch.object(setup, 'ROOT', root), patch.object(common, 'ROOT', root):
                self.assertEqual(setup.select_release(None), ('My_OS3', 5586))

    def test_fresh_os4_root_never_adopts_original_os3_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary).resolve()
            self.instance_profile(repo, 'os3', name='My_OS3', port=5586)
            before = (repo / 'local/runtime.json').read_bytes()
            with patch.object(setup, 'ROOT', repo), patch.object(setup, 'REPO_ROOT', repo), \
                    patch.object(common, 'ROOT', repo), patch.dict('os.environ', {}, clear=True):
                self.assertEqual(setup.select_release({'variant': 'os4-official'}),
                                 (common.OS4_NAME, common.OS4_PORT))
                self.assertEqual(setup.ROOT, repo / 'work/os4-official')
            self.assertEqual((repo / 'local/runtime.json').read_bytes(), before)
            self.assertFalse((repo / 'work/os4-official').exists())

    def test_profile_mismatch_is_rejected_before_runtime_adoption(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary).resolve()
            root = repo / 'old-os3'
            self.instance_profile(root, 'os3', name='My_OS3', port=5586)
            before = (root / 'local/runtime.json').read_bytes()
            with patch.object(setup, 'ROOT', repo), patch.object(setup, 'REPO_ROOT', repo), \
                    patch.object(common, 'ROOT', repo), \
                    patch.dict('os.environ', {'HYPEROS_AVD_WORKSPACE': str(root)}, clear=True):
                for overrides in ({}, {'name': 'Explicit', 'port': 5584}):
                    with self.assertRaisesRegex(RuntimeError, 'does not match'):
                        setup.select_release({'variant': 'os4-official'}, **overrides)
                self.assertEqual(setup.ROOT, repo)
                self.assertEqual(common.ROOT, repo)
            self.assertEqual((root / 'local/runtime.json').read_bytes(), before)

    def test_unidentified_runtime_is_not_silently_reused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / 'local').mkdir()
            (root / 'local/runtime.json').write_text(json.dumps({'name': 'Unknown', 'port': 5586}))
            with patch.object(setup, 'ROOT', root), patch.object(common, 'ROOT', root):
                with self.assertRaisesRegex(RuntimeError, 'no matching firmware profile'):
                    setup.select_release(None)

    def test_launcher_instruction_follows_source_and_workspace_not_name(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary).resolve()
            cases = ((repo, 'os3', common.OS4_NAME, 'Start-HyperOS.command'),
                     (repo / 'work/os4-official', common.OS4_SOURCE, 'My_phone',
                      'Start-HyperOS4-Official.command'),
                     (repo / 'work/os4-pad', 'official-yingtian-ota', 'My_pad',
                      'Start-HyperOS4-Pad.command'))
            with patch.object(setup, 'REPO_ROOT', repo):
                for root, source, name, launcher in cases:
                    self.instance_profile(root, source, name)
                    self.assertIn(str(repo / launcher), setup.start_instruction(root))
                custom = repo / 'custom workspace'
                self.instance_profile(custom, common.OS4_SOURCE, 'My_other_phone')
                instruction = setup.start_instruction(custom)
                self.assertIn("HYPEROS_AVD_WORKSPACE='" + str(custom) + "'", instruction)
                self.assertIn(str(repo / 'scripts/launch.py'), instruction)
                self.assertNotIn('Start-HyperOS.command', instruction)
                (custom / 'Start.command').write_text('# managed wrapper')
                self.assertIn(str(custom / 'Start.command'), setup.start_instruction(custom))

    def test_source_build_instance_reuses_only_matching_profile(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary).resolve()
            for index, source in enumerate((common.OS4_SOURCE, 'official-yingtian-ota')):
                with self.subTest(source=source):
                    root = repo / str(index)
                    with patch.object(setup, 'ROOT', root):
                        self.assertEqual(setup.select_build_instance(source, 'Fresh', 5582), ('Fresh', 5582))
                    self.assertFalse(root.exists())
                    self.instance_profile(root, source, 'Kept_name', 5584)
                    before = (root / 'local/runtime.json').read_bytes()
                    with patch.object(setup, 'ROOT', root):
                        self.assertEqual(setup.select_build_instance(source, 'Fresh', 5582), ('Kept_name', 5584))
                        other = 'official-yingtian-ota' if source == common.OS4_SOURCE else common.OS4_SOURCE
                        with self.assertRaisesRegex(RuntimeError, 'does not match'):
                            setup.select_build_instance(other, 'Fresh', 5582)
                    self.assertEqual((root / 'local/runtime.json').read_bytes(), before)

    def test_source_build_instance_refuses_bad_saved_id_and_port(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            self.instance_profile(root, common.OS4_SOURCE)
            with patch.object(setup, 'ROOT', root):
                for saved, message in (({'name': '../escape', 'port': 5580}, 'ASCII'),
                                       ({'name': 'Custom', 'port': 5581}, 'even emulator'),
                                       ({'name': 'Custom', 'port': True}, 'even emulator')):
                    (root / 'local/runtime.json').write_text(json.dumps(saved))
                    with self.assertRaisesRegex(RuntimeError, message):
                        setup.select_build_instance(common.OS4_SOURCE, 'Fresh', 5582)

    def test_os4_metadata_cannot_masquerade_as_legacy_os3(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path, _ = self.fixture(root)
            manifest = json.loads(path.read_text())
            manifest['android_api'] = 37
            path.write_text(json.dumps(manifest))
            with self.assertRaisesRegex(RuntimeError, 'OS3/OS4 mix'):
                setup.read_manifest(str(path))

    def test_parts_have_exact_boundaries_and_checksums(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            writer = package_release.PartsWriter(root, 'fixture', 3)
            writer.write(b'0123456')
            writer.close_part()
            self.assertEqual([item['size'] for item in writer.parts], [3, 3, 1])
            self.assertEqual(b''.join((root / item['name']).read_bytes() for item in writer.parts), b'0123456')
            for item in writer.parts:
                self.assertEqual(common.sha256(root / item['name']), item['sha256'])

    def test_import_preserves_userdata(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest, _ = self.fixture(root)
            data = root / 'avd/HyperOS_Test.avd/userdata-qemu.img'
            data.parent.mkdir(parents=True)
            data.write_bytes(b'personal data')
            with patch.object(setup, 'ROOT', root):
                setup.install_bundle(str(manifest))
            self.assertEqual((root / 'images/system.img').read_bytes(), b'test firmware')
            self.assertEqual(data.read_bytes(), b'personal data')

    def test_corrupt_part_never_replaces_image(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest, asset = self.fixture(root)
            image = root / 'images/system.img'
            image.parent.mkdir()
            image.write_bytes(b'original')
            asset.write_bytes(b'corrupt')
            with patch.object(setup, 'ROOT', root), self.assertRaises(RuntimeError):
                setup.install_bundle(str(manifest))
            self.assertEqual(image.read_bytes(), b'original')

    def test_archive_cannot_escape_installation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest, _ = self.fixture(root, '../outside')
            with patch.object(setup, 'ROOT', root), self.assertRaises(RuntimeError):
                setup.install_bundle(str(manifest))

    def test_busy_port_is_refused_without_guest_action(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen()
            with self.assertRaises(RuntimeError):
                common.port_free(listener.getsockname()[1])

    def test_foreign_avd_registry_is_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry = root / 'registry'
            registry.mkdir()
            ini = registry / 'HyperOS_Test.ini'
            ini.write_text('path=/some/other/workspace/HyperOS_Test.avd\n')
            with patch.object(setup, 'ROOT', root), patch.object(setup, 'avd_home', return_value=registry):
                with self.assertRaises(RuntimeError):
                    setup.configure(root / 'sdk', 'HyperOS_Test', 5570)
            self.assertEqual(ini.read_text(), 'path=/some/other/workspace/HyperOS_Test.avd\n')

    def test_profile_target_is_registered_without_replacing_userdata(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'config').mkdir()
            (root / 'config/avd.ini').write_text('target=android-37.0\nhw.lcd.width=1080\n')
            data = root / 'avd/HyperOS_Test.avd/userdata-qemu.img'
            data.parent.mkdir(parents=True)
            data.write_bytes(b'personal profile data')
            registry = root / 'registry'
            with patch.object(setup, 'ROOT', root), patch.object(setup, 'avd_home', return_value=registry):
                setup.configure(root / 'sdk', 'HyperOS_Test', 5572)
            self.assertIn('target=android-37.0\n', (registry / 'HyperOS_Test.ini').read_text())
            self.assertEqual(data.read_bytes(), b'personal profile data')

    def test_pad_memory_limit_is_checked_before_any_configuration_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.instance_profile(root, 'official-yingtian-ota', name='My_pad', port=5582)
            (root / 'config').mkdir()
            template = root / 'config/avd.ini'
            template.write_text('target=android-37.0\nhw.cpu.arch=arm64\nhw.ramSize=4096\n')
            runtime = root / 'local/runtime.json'
            saved = json.loads(runtime.read_text())
            runtime.write_text(json.dumps({**saved, 'hardware': {'hw.ramSize': '6144'}}))
            data = root / 'avd/My_pad.avd/userdata-qemu.img'
            data.parent.mkdir(parents=True)
            data.write_bytes(b'personal tablet data')
            config = data.parent / 'config.ini'
            config.write_text('original configuration\n')
            registry = root / 'registry';registry.mkdir()
            registration = registry / 'My_pad.ini'
            registration.write_text('path=' + str(data.parent) + '\ntarget=android-37.0\n')
            before = {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()}
            with patch.object(setup, 'ROOT', root), patch.object(setup, 'avd_home', return_value=registry):
                with self.assertRaisesRegex(RuntimeError, '4096'):
                    setup.configure(root / 'sdk', 'My_pad', 5582)
            after = {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()}
            self.assertEqual(after, before)

    def test_pad_oversized_template_is_rejected_before_creating_avd(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.instance_profile(root, 'official-yingtian-ota', name='My_pad', port=5582)
            (root / 'config').mkdir()
            (root / 'config/avd.ini').write_text(
                'target=android-37.0\nhw.cpu.arch=arm64\nhw.ramSize=6144\n')
            before = (root / 'local/runtime.json').read_bytes()
            with patch.object(setup, 'ROOT', root), \
                    patch.object(setup, 'avd_home', return_value=root / 'registry'):
                with self.assertRaisesRegex(RuntimeError, '4096'):
                    setup.configure(root / 'sdk', 'My_pad', 5582)
            self.assertFalse((root / 'avd').exists())
            self.assertFalse((root / 'registry').exists())
            self.assertFalse((root / 'local/instances.json').exists())
            self.assertEqual((root / 'local/runtime.json').read_bytes(), before)

    def test_memory_limit_is_pad_specific_and_preserves_userdata(self):
        with tempfile.TemporaryDirectory() as temporary:
            for index, (source, ram) in enumerate((('official-yingtian-ota', '4096'),
                                                  (common.OS4_SOURCE, '8192'), ('os3', '8192'))):
                with self.subTest(source=source):
                    root = Path(temporary) / str(index)
                    self.instance_profile(root, source, name='Custom', port=5582)
                    (root / 'config').mkdir()
                    (root / 'config/avd.ini').write_text(
                        'target=android-37.0\nhw.cpu.arch=arm64\nhw.ramSize=' + ram + '\n')
                    data = root / 'avd/Custom.avd/userdata-qemu.img'
                    data.parent.mkdir(parents=True)
                    data.write_bytes(b'preserved userdata')
                    with patch.object(setup, 'ROOT', root), \
                            patch.object(setup, 'avd_home', return_value=root / 'registry'):
                        setup.configure(root / 'sdk', 'Custom', 5582)
                    self.assertIn('hw.ramSize=' + ram + '\n', (data.parent / 'config.ini').read_text())
                    self.assertEqual(data.read_bytes(), b'preserved userdata')


if __name__ == '__main__':
    unittest.main()
