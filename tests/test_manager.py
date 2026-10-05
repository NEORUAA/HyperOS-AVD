"""Exercise recoverable upgrades, isolation, discovery and release downloads."""
import hashlib
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
import manage
import setup


class ManagerTests(unittest.TestCase):
    def bundle(self, directory, version='v0.2.1-test', bad=False, variant='os4-official'):
        if variant == 'os4-pad' and version == 'v0.2.1-test':
            version = 'pad-v0.1.0-a17-hyperos4-yingtian-r1'
        payloads = {'images/system.img': b'new firmware', 'images/encryptionkey.img': b'key',
                    'images/userdata.img': b'blank', 'tools/test': b'tool',
                    'config/avd.ini': b'target=android-37.0\nhw.cpu.arch=arm64\nhw.ramSize=6144\nhw.cpu.ncore=4\ndisk.dataPartition.size=32G\n',
                    'runtime/scripts/launch.py': b'# launch'}
        directory.mkdir()
        archive = directory / 'test.part001'
        with tarfile.open(archive, 'w:gz') as tar:
            for name, data in payloads.items():
                entry = tarfile.TarInfo(name)
                entry.size = len(data)
                tar.addfile(entry, io.BytesIO(data))
        manifest = {'project': 'HyperOS-AVD', 'version': version, 'format': 3,
                    'platform': 'macos-arm64', 'variant': 'os4-official', 'android_api': 37,
                    'hyperos': '4.0.17.0.XFRCNXM',
                    'build': {'source': common.OS4_SOURCE, 'android_api': 37,
                              'hyperos': '4.0.17.0.XFRCNXM', 'adb_authentication': True},
                    'compatibility': {'userdata_family': 'os4-hongkong-api37-ranchu-4k'},
                    'parts': [{'name': archive.name, 'size': archive.stat().st_size, 'sha256': common.sha256(archive)}],
                    'files': {name: {'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
                              for name, data in payloads.items()}}
        if variant == 'os4-pad':
            # Keep the archive and its metadata consistent with the Pad profile.
            payloads['config/avd.ini'] = payloads['config/avd.ini'].replace(b'6144', b'4096')
            with tarfile.open(archive, 'w:gz') as tar:
                for name, data in payloads.items():
                    entry = tarfile.TarInfo(name)
                    entry.size = len(data)
                    tar.addfile(entry, io.BytesIO(data))
            manifest.update(variant=variant, source=setup.PAD_SOURCE,
                            source_device='yingtian', hyperos=setup.PAD_HYPEROS)
            manifest['build'].update(source=setup.PAD_SOURCE, device='yingtian',
                                     hyperos=setup.PAD_HYPEROS, memory_limit_mib=4096)
            manifest['compatibility'] = {'userdata_family': setup.PAD_FAMILY,
                                         'minimum_installer': '1.1.0', 'runtime_in_bundle': True}
            manifest['parts'] = [{'name': archive.name, 'size': archive.stat().st_size,
                                   'sha256': common.sha256(archive)}]
            manifest['files'] = {name: {'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
                                 for name, data in payloads.items()}
        path = directory / 'manifest.json'
        path.write_text(json.dumps(manifest))
        if bad:
            archive.write_bytes(b'broken')
        return path, manifest

    def test_pad_catalog_does_not_mix_phone_releases(self):
        tags = ('pad-v0.1.0-a17-hyperos4-yingtian-r1', 'v0.2.1-a17-hyperos4-hongkong-r2',
                'v0.1.0-a16-hyperos3-fuxi-r1')
        rows = [{'tag_name': tag, 'prerelease': True, 'assets': [{'name': 'manifest.json'}]} for tag in tags]
        with patch.object(manage, 'remote_json', return_value=rows):
            self.assertEqual([r['tag_name'] for r in manage.catalog('os4-pad')], [tags[0]])
            self.assertEqual([r['tag_name'] for r in manage.catalog('os4-official')], [tags[1]])
            self.assertEqual(len(manage.catalog()), 3)
        with patch.object(manage, 'catalog', return_value=rows), \
                patch.object(manage, 'choose', return_value=0) as choose, \
                patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(manage.pick_release(), rows[0])
            self.assertIn('[OS4 Pad]', choose.call_args.args[1][0])
        self.assertIsNone(manage.release_variant({'tag_name': 'v0.3.0-a17-hyperos4-yingtian-r1'}))
        self.assertEqual(manage.version_key(tags[0]), (0, 1, 0))

    def test_pad_and_phone_userdata_are_incompatible_even_with_same_encryption_key(self):
        key = {'images/encryptionkey.img': {'sha256': 'same'}}
        phone = {'variant': 'os4-official', 'format': 3, 'version': 'v0.2.1', 'files': key,
                 'compatibility': {'userdata_family': 'os4-hongkong-api37-ranchu-4k'}}
        pad = {**phone, 'variant': 'os4-pad', 'version': 'pad-v0.1.0',
               'compatibility': {'userdata_family': setup.PAD_FAMILY}}
        for old, new in ((phone, pad), (pad, phone)):
            with self.subTest(old=old['variant']), self.assertRaisesRegex(RuntimeError, 'family'):
                manage.compatible(old, new)
        spoofed = {**phone, 'compatibility': pad['compatibility']}
        self.assertIsNone(manage.family(spoofed))
        with self.assertRaisesRegex(RuntimeError, 'family'):
            manage.compatible(pad, spoofed)

    def test_pad_versions_use_an_independent_semver_sequence_for_downgrade_checks(self):
        old = {'variant': 'os4-pad', 'format': 3, 'version': 'pad-v0.1.0-a17-hyperos4-yingtian-r1',
               'files': {'images/encryptionkey.img': {'sha256': 'same'}},
               'compatibility': {'userdata_family': setup.PAD_FAMILY}}
        new = {**old, 'version': 'pad-v0.1.1-a17-hyperos4-yingtian-r2'}
        manage.compatible(old, new)
        with self.assertRaisesRegex(RuntimeError, 'downgrade'):
            manage.compatible(new, old)

    def test_pad_install_rejects_six_gib_before_owner_download_or_writes(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            path, _ = self.bundle(folder / 'bundle', variant='os4-pad')
            root = folder / 'instance'
            with patch.object(manage, 'owner') as owner, patch.object(manage, 'idle') as idle, \
                    patch.object(setup, 'install_bundle') as extract:
                with self.assertRaisesRegex(RuntimeError, '4096'):
                    manage.install(root, path, 'My_Tablet', 5584, Path('/sdk'), manage.hardware(6, 32, 4))
            owner.assert_not_called()
            idle.assert_not_called()
            extract.assert_not_called()
            self.assertFalse(root.exists())

    def test_phone_to_pad_install_refuses_before_backup_or_firmware_switch(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            _, phone = self.bundle(folder / 'phone')
            pad, _ = self.bundle(folder / 'pad', variant='os4-pad')
            root = folder / 'instance'
            before = self.legacy(root, phone)
            with patch.object(manage, 'idle'), patch.object(manage, 'validate_userdata'), \
                    patch.object(manage, 'avd_home', return_value=folder / 'registry'):
                with self.assertRaisesRegex(RuntimeError, 'family'):
                    manage.install(root, pad, 'Test_temp', 5580, Path('/sdk'),
                                   manage.hardware(4, 32, 4, variant='os4-pad'))
            self.assertEqual((root / 'images/system.img').read_bytes(), b'old firmware')
            for name, data in before.items():
                self.assertEqual((root / 'avd/Test_temp.avd' / name).read_bytes(), data)
            self.assertFalse((root / 'backups').exists())

    def test_pad_install_and_upgrade_keep_custom_name_port_and_data(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            path, manifest = self.bundle(folder / 'bundle',
                version='pad-v0.1.0-a17-hyperos4-yingtian-r1', variant='os4-pad')
            root, registry = folder / 'My tablet', folder / 'registry'
            settings = manage.hardware(4, 32, 4, variant='os4-pad')
            with patch.object(manage, 'idle'), patch.object(manage, 'resize'), \
                    patch.object(manage, 'validate_userdata'), \
                    patch.object(manage, 'data_size', return_value={'virtual-size': 32 * 1024**3}), \
                    patch.object(manage, 'avd_home', return_value=registry), \
                    patch.object(setup, 'avd_home', return_value=registry):
                manage.install(root, path, 'My_Tablet', 5584, Path('/sdk'), settings)
                data = root / 'avd/My_Tablet.avd/userdata-qemu.img'
                data.write_bytes(b'personal tablet data')
                second, _ = self.bundle(folder / 'next',
                    version='pad-v0.1.1-a17-hyperos4-yingtian-r2', variant='os4-pad')
                manage.install(root, second, 'My_Tablet', 5584, Path('/sdk'), settings)
                self.assertEqual(data.read_bytes(), b'personal tablet data')
                runtime = json.loads((root / 'local/runtime.json').read_text())
                self.assertEqual((runtime['name'], runtime['port']), ('My_Tablet', 5584))
                self.assertEqual(runtime['hardware'], settings)
                self.assertEqual(manage.instances()[0]['variant'], 'os4-pad')
                config = manage.properties(root / 'avd/My_Tablet.avd/config.ini')
                self.assertEqual(config['AvdId'], 'My_Tablet')
                self.assertEqual(config['hw.ramSize'], '4096')
                self.assertEqual(config['disk.dataPartition.size'], '32G')
                self.assertEqual(json.loads((root / 'local/installed-release.json').read_text())
                                 ['compatibility']['userdata_family'], setup.PAD_FAMILY)

    def test_pad_cli_defaults_and_over_memory_refusal_before_download(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / 'instance'
            arguments = ['manage.py', 'install', '--variant', 'os4-pad', '--root', str(root),
                         '--name', 'My_Tablet', '--port', '5584']
            with patch('sys.argv', arguments), patch.object(manage, 'host_check'), \
                    patch.object(manage, 'sdk_path', return_value=Path('/sdk')), \
                    patch.object(manage, 'idle'), patch.object(manage, 'catalog', return_value=[{'tag_name': 'vpad'}]), \
                    patch.object(manage, 'fetch_release', return_value=Path('/bundle/manifest.json')), \
                    patch.object(manage, 'install') as install:
                manage.main()
                self.assertEqual(install.call_args.args[5], manage.hardware(4, 32, 4, variant='os4-pad'))
            with patch('sys.argv', arguments + ['--ram', '6']), patch.object(manage, 'host_check'), \
                    patch.object(manage, 'fetch_release') as download:
                with self.assertRaisesRegex(RuntimeError, '4096'):
                    manage.main()
            download.assert_not_called()
            self.assertFalse(root.exists())

    def test_local_pad_cli_selects_four_gib_without_explicit_variant(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            path, _ = self.bundle(folder / 'bundle', variant='os4-pad')
            args = ['manage.py', 'install', '--bundle', str(path), '--root', str(folder / 'instance'),
                    '--name', 'Renamed_Pad', '--port', '5584']
            with patch('sys.argv', args), patch.object(manage, 'host_check'), \
                    patch.object(manage, 'sdk_path', return_value=Path('/sdk')), \
                    patch.object(manage, 'install') as install:
                manage.main()
            self.assertEqual(install.call_args.args[2:4], ('Renamed_Pad', 5584))
            self.assertEqual(install.call_args.args[5]['hw.ramSize'], '4096')

    def test_pad_tui_defaults_name_and_resources_before_download(self):
        row = {'tag_name': 'pad-v0.1.0-a17-hyperos4-yingtian-r1', 'prerelease': True}
        inputs = ['1', '1', '1', '', '', '', '', '', '0']
        with patch.object(manage, 'instances', return_value=[]), \
                patch.object(manage, 'catalog', return_value=[row]), \
                patch.object(manage, 'choose_port', return_value=5584), patch.object(manage, 'idle'), \
                patch.object(manage, 'fetch_release', side_effect=manage.Back) as fetch, \
                patch('builtins.input', side_effect=inputs), patch('sys.stdout', new_callable=io.StringIO) as output:
            manage.tui()
        self.assertEqual(fetch.call_args.args[0], row)
        self.assertIn('AVD: HyperOS_4_Pad', output.getvalue())
        self.assertIn('RAM: 4 GiB | Storage: 32G | CPU: 4', output.getvalue())

    def test_pad_cli_catalog_selection_is_supported(self):
        with patch('sys.argv', ['manage.py', 'releases', '--variant', 'os4-pad']), \
                patch.object(manage, 'host_check'), patch.object(manage, 'catalog', return_value=[]) as catalog, \
                patch('sys.stdout', new_callable=io.StringIO):
            manage.main()
        catalog.assert_called_once_with('os4-pad', False)

    def legacy(self, root, manifest):
        root.mkdir()
        for key in ('images', 'tools', 'config', 'local'):
            (root / key).mkdir()
        (root / 'images/system.img').write_bytes(b'old firmware')
        (root / 'tools/test').write_bytes(b'old tool')
        (root / 'config/avd.ini').write_text('target=android-37.0\n')
        data = root / 'avd/Test_temp.avd'
        data.mkdir(parents=True)
        for name in ('userdata-qemu.img', 'userdata-qemu.img.qcow2', 'encryptionkey.img', 'encryptionkey.img.qcow2'):
            (data / name).write_bytes(('personal ' + name).encode())
        old = {**manifest, 'version': 'v0.2.0-a17-hyperos4-hongkong-r1', 'format': 2}
        old.pop('compatibility')
        (root / 'local/installed-release.json').write_text(json.dumps(old))
        (root / 'local/runtime.json').write_text(json.dumps({'name': 'Test_temp', 'port': 5580, 'sdk': '/sdk'}))
        (root / 'local/build.json').write_text(json.dumps(old['build']))
        return {p.name: p.read_bytes() for p in data.iterdir()}

    def invoke(self, root, path, registry, **kwargs):
        with patch.object(manage, 'idle'), patch.object(manage, 'resize'), patch.object(manage, 'validate_userdata'), \
                patch.object(manage, 'data_size', return_value={'virtual-size': 32 * 1024**3}), \
                patch.object(manage, 'avd_home', return_value=registry), \
                patch.object(setup, 'avd_home', return_value=registry):
            return manage.install(root, path, 'Test_temp', 5580, Path('/sdk'), manage.hardware(6, 32, 2), **kwargs)

    def test_v020_upgrade_preserves_all_userdata_and_keys(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            path, manifest = self.bundle(folder / 'bundle')
            root = folder / 'instance'
            before = self.legacy(root, manifest)
            backup = self.invoke(root, path, folder / 'registry')
            self.assertEqual((root / 'images/system.img').read_bytes(), b'new firmware')
            for name, value in before.items():
                self.assertEqual((root / 'avd/Test_temp.avd' / name).read_bytes(), value)
                self.assertEqual((backup / 'avd' / name).read_bytes(), value)
            config = setup.json.loads((root / 'local/runtime.json').read_text())
            self.assertEqual(config['hardware']['hw.ramSize'], '6144')
            self.assertEqual(config['port'], 5580)
            self.assertFalse((root / 'local/upgrade-pending.json').exists())

    def test_failed_switch_rolls_back_firmware_data_and_registry(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            path, manifest = self.bundle(folder / 'bundle')
            root = folder / 'instance'
            before = self.legacy(root, manifest)
            registry = folder / 'registry'
            registry.mkdir()
            ini = registry / 'Test_temp.ini'
            text = f'path={root}/avd/Test_temp.avd\n'
            ini.write_text(text)
            with patch.object(setup, 'configure', side_effect=RuntimeError('injected configuration fault')):
                with self.assertRaisesRegex(RuntimeError, 'injected'):
                    self.invoke(root, path, registry)
            self.assertEqual((root / 'images/system.img').read_bytes(), b'old firmware')
            self.assertFalse((root / 'images').is_symlink())
            self.assertEqual(ini.read_text(), text)
            for name, value in before.items():
                self.assertEqual((root / 'avd/Test_temp.avd' / name).read_bytes(), value)

    def test_explicit_rollback_restores_old_version_and_app_data(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            path, manifest = self.bundle(folder / 'bundle')
            root = folder / 'instance'
            before = self.legacy(root, manifest)
            registry = folder / 'registry'
            backup = self.invoke(root, path, registry)
            (root / 'avd/Test_temp.avd/userdata-qemu.img').write_bytes(b'mutated by new OS')
            with patch.object(manage, 'avd_home', return_value=registry):
                manage.restore(root, backup)
            self.assertEqual((root / 'images/system.img').read_bytes(), b'old firmware')
            self.assertEqual((root / 'avd/Test_temp.avd/userdata-qemu.img').read_bytes(), before['userdata-qemu.img'])
            self.assertEqual(json.loads((root / 'local/installed-release.json').read_text())['version'], 'v0.2.0-a17-hyperos4-hongkong-r1')

    def test_failed_fresh_configuration_can_be_retried(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            path, _ = self.bundle(folder / 'bundle')
            root = folder / 'instance'
            registry = folder / 'registry'
            configure = setup.configure
            def fail_after_configuration(*args):
                configure(*args)
                raise RuntimeError('injected after fresh configuration')
            with patch.object(setup, 'configure', side_effect=fail_after_configuration):
                with self.assertRaisesRegex(RuntimeError, 'injected'):
                    self.invoke(root, path, registry)
            self.assertFalse((root / 'avd/Test_temp.avd').exists())
            self.invoke(root, path, registry)
            self.assertEqual((root / 'images/system.img').read_bytes(), b'new firmware')

    def test_corrupt_download_does_not_change_existing_data(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            path, manifest = self.bundle(folder / 'bundle', bad=True)
            root = folder / 'instance'
            before = self.legacy(root, manifest)
            with self.assertRaisesRegex(RuntimeError, 'checksum'):
                self.invoke(root, path, folder / 'registry')
            self.assertEqual((root / 'images/system.img').read_bytes(), b'old firmware')
            self.assertEqual((root / 'avd/Test_temp.avd/userdata-qemu.img').read_bytes(), before['userdata-qemu.img'])
            self.assertFalse((root / 'backups').exists())

    def test_v020_family_maps_to_new_manifest(self):
        old = {'format': 2, 'variant': 'os4-official', 'android_api': 37}
        self.assertEqual(manage.family(old), 'os4-hongkong-api37-ranchu-4k')

    def test_incompatible_images_or_keys_refused(self):
        old = {'format': 2, 'variant': 'os4-official', 'android_api': 37, 'version': 'v0.2.0',
               'files': {'images/encryptionkey.img': {'sha256': 'same'}}}
        new = {**old, 'format': 3, 'version': 'v0.2.1', 'compatibility': {'userdata_family': manage.family(old)}}
        manage.compatible(old, new)
        with self.assertRaisesRegex(RuntimeError, 'family'):
            manage.compatible(old, {**new, 'compatibility': {'userdata_family': 'api38'}})
        with self.assertRaisesRegex(RuntimeError, 'Encryption'):
            manage.compatible(old, {**new, 'files': {'images/encryptionkey.img': {'sha256': 'new'}}})
        with self.assertRaisesRegex(RuntimeError, 'downgrade'):
            manage.compatible(new, old)

    def test_newer_installer_requirement_checked_before_mutation(self):
        with tempfile.TemporaryDirectory() as d:
            directory = Path(d)
            path, manifest = self.bundle(directory / 'bundle')
            manifest['compatibility']['minimum_installer'] = '2.0.0'
            path.write_text(json.dumps(manifest))
            root = directory / 'instance'
            with self.assertRaisesRegex(RuntimeError, 'newer installer'):
                self.invoke(root, path, directory / 'registry')
            self.assertFalse(root.exists())

    def test_foreign_name_and_changed_port_refused(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            path, manifest = self.bundle(folder / 'bundle')
            root = folder / 'instance'
            self.legacy(root, manifest)
            registry = folder / 'registry';registry.mkdir()
            (registry / 'Test_temp.ini').write_text('path=/foreign/Test_temp.avd\n')
            with self.assertRaisesRegex(RuntimeError, 'another workspace'):
                self.invoke(root, path, registry)
            (registry / 'Test_temp.ini').unlink()
            saved = root / 'local/runtime.json'
            saved.write_text(json.dumps({'name': 'Test_temp', 'port': 5582}))
            with self.assertRaisesRegex(RuntimeError, 'retain'):
                self.invoke(root, path, registry)

    def test_broken_backup_refuses_restore_before_changes(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            path, manifest = self.bundle(folder / 'bundle')
            root = folder / 'instance'
            self.legacy(root, manifest)
            registry = folder / 'registry'
            backup = self.invoke(root, path, registry)
            (backup / 'avd/userdata-qemu.img').write_bytes(b'corrupt')
            with self.assertRaisesRegex(RuntimeError, 'integrity'):
                manage.restore(root, backup)
            self.assertEqual((root / 'images/system.img').read_bytes(), b'new firmware')

    def test_runtime_reconfiguration_preserves_hardware(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / 'local').mkdir();(root / 'config').mkdir()
            (root / 'config/avd.ini').write_text('target=android-37.0\nhw.cpu.arch=arm64\nhw.ramSize=4096\nhw.cpu.ncore=4\ndisk.dataPartition.size=6G\n')
            desired = manage.hardware(8, 64, 2)
            (root / 'local/runtime.json').write_text(json.dumps({'hardware': desired, 'camera_bridge': True}))
            with patch.object(setup, 'ROOT', root), patch.object(setup, 'avd_home', return_value=root / 'registry'):
                setup.configure(Path('/sdk'), 'Custom_temp', 5580)
                setup.configure(Path('/sdk'), 'Custom_temp', 5580)
            actual = manage.properties(root / 'avd/Custom_temp.avd/config.ini')
            for key, value in desired.items():
                self.assertEqual(actual[key], value)
            self.assertTrue(json.loads((root / 'local/runtime.json').read_text())['camera_bridge'])

    def test_catalog_includes_prereleases_and_filters_os3(self):
        rows = [{'tag_name': tag, 'prerelease': True, 'published_at': '2026-10-04', 'assets': [{'name': 'manifest.json'}]}
                for tag in ('v0.2.0-a17-hyperos4-hongkong-r1', 'v0.2.1-a17-hyperos4-hongkong-r2', 'v0.1.0-a16-hyperos3-fuxi-r1')]
        with patch.object(manage, 'remote_json', return_value=rows):
            self.assertEqual(manage.catalog()[0]['tag_name'], 'v0.2.1-a17-hyperos4-hongkong-r2')
            self.assertEqual(len(manage.catalog()), 3)
            self.assertEqual(len(manage.catalog('os4-official')), 2)
            self.assertEqual(len(manage.catalog('os3')), 1)
            self.assertEqual(manage.catalog(stable_only=True), [])

    def test_stable_installer_is_separate_from_image_catalog(self):
        rows = [{'tag_name': tag, 'prerelease': preview, 'assets': [{'name': 'manifest.json'}]}
                for tag, preview in [('installer-v1.0.0', False), ('installer-v2.0.0', True),
                                     ('v0.1.0-a16-hyperos3-fuxi-r1', True)]]
        with patch.object(manage, 'remote_json', return_value=rows):
            self.assertEqual([r['tag_name'] for r in manage.catalog()], ['v0.1.0-a16-hyperos3-fuxi-r1'])
            self.assertEqual([r['tag_name'] for r in manage.installer_catalog()], ['installer-v1.0.0'])

    def test_installer_discovery_reads_later_pages(self):
        first = [{'tag_name': 'unrelated'}] * 100
        second = [{'tag_name': 'installer-v1.0.0', 'prerelease': False}]
        with patch.object(manage, 'remote_json', side_effect=[first, second]):
            self.assertEqual(manage.installer_catalog()[0]['tag_name'], 'installer-v1.0.0')

    def test_source_os3_instance_is_visible_without_release_manifest(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / 'source'
            data = root / 'avd' / (common.DEFAULT_NAME + '.avd')
            data.mkdir(parents=True)
            (root / 'local').mkdir();(root / 'config').mkdir();(root / 'scripts').mkdir()
            (root / 'local/runtime.json').write_text(json.dumps({'name': common.DEFAULT_NAME, 'port': 5566}))
            (root / 'config/avd.ini').write_text('target=android-36\nhw.cpu.arch=arm64\n')
            (root / 'scripts/build_image.py').write_text('# source workspace')
            registry = Path(d) / 'registry';registry.mkdir()
            (registry / (common.DEFAULT_NAME + '.ini')).write_text('path=' + str(data) + '\n')
            with patch.object(manage, 'avd_home', return_value=registry):
                entries = manage.instances()
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]['variant'], 'os3')
            self.assertEqual(entries[0]['version'], 'legacy')

    def test_custom_named_source_os3_keeps_discovery_ownership_gates(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / 'source'
            name = 'My_OS3'
            data = root / 'avd' / (name + '.avd')
            data.mkdir(parents=True)
            (root / 'local').mkdir();(root / 'config').mkdir();(root / 'scripts').mkdir()
            runtime = root / 'local/runtime.json'
            runtime.write_text(json.dumps({'name': name, 'port': 5586}))
            template = root / 'config/avd.ini'
            template.write_text('target=android-36\nhw.cpu.arch=arm64\n')
            builder = root / 'scripts/build_image.py'
            builder.write_text('# source workspace')
            registry = Path(d) / 'registry';registry.mkdir()
            ini = registry / (name + '.ini')
            ini.write_text('path=' + str(data) + '\n')
            with patch.object(manage, 'avd_home', return_value=registry):
                entries = manage.instances()
                self.assertEqual(len(entries), 1)
                self.assertEqual(entries[0]['runtime']['name'], name)
                self.assertEqual(entries[0]['variant'], 'os3')
                for invalid in ('target=android-37.0\nhw.cpu.arch=arm64\n',
                                'target=android-36\nhw.cpu.arch=x86_64\n'):
                    template.write_text(invalid)
                    self.assertEqual(manage.instances(), [])
                template.write_text('target=android-36\nhw.cpu.arch=arm64\n')
                builder.unlink()
                self.assertEqual(manage.instances(), [])
                builder.write_text('# source workspace')
                ini.rename(registry / 'Other.ini')
                self.assertEqual(manage.instances(), [])
                (registry / 'Other.ini').rename(ini)
                runtime.write_text(json.dumps({'name': 'Other', 'port': 5586}))
                self.assertEqual(manage.instances(), [])

    def test_custom_named_source_pad_is_classified_with_ownership_gates(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / 'source'
            name = 'My_tablet'
            data = root / 'avd' / (name + '.avd')
            data.mkdir(parents=True)
            (root / 'local').mkdir();(root / 'config').mkdir()
            runtime = root / 'local/runtime.json'
            runtime.write_text(json.dumps({'name': name, 'port': 5582}))
            build = root / 'local/build.json'
            build.write_text(json.dumps({'source': 'official-yingtian-ota', 'android_api': 37}))
            (root / 'config/avd.ini').write_text('target=android-37.0\nhw.cpu.arch=arm64\n')
            registry = Path(d) / 'registry';registry.mkdir()
            ini = registry / (name + '.ini')
            ini.write_text('path=' + str(data) + '\n')
            with patch.object(manage, 'avd_home', return_value=registry):
                entries = manage.instances()
                self.assertEqual(len(entries), 1)
                self.assertEqual(entries[0]['variant'], 'os4-pad')
                self.assertEqual(entries[0]['runtime']['name'], name)
                self.assertEqual(entries[0]['version'], 'legacy')
                ini.write_text('path=' + str(root / 'avd/Other.avd') + '\n')
                self.assertEqual(manage.instances(), [])
                ini.write_text('path=' + str(data) + '\n')
                ini.rename(registry / 'Other.ini')
                self.assertEqual(manage.instances(), [])
                (registry / 'Other.ini').rename(ini)
                runtime.write_text(json.dumps({'name': 'Other', 'port': 5582}))
                self.assertEqual(manage.instances(), [])
                runtime.write_text(json.dumps({'name': name, 'port': 5582}))
                build.write_text(json.dumps({'source': 'unverified-tablet', 'android_api': 37}))
                self.assertEqual(manage.instances(), [])
                build.write_text(json.dumps({'source': common.OS4_SOURCE, 'android_api': 37}))
                self.assertEqual(manage.instances()[0]['variant'], 'os4-official')

    def test_pad_hardware_options_use_four_gib_limit_and_label(self):
        self.assertEqual(manage.hardware(4, 32, 2, variant='os4-pad')['hw.ramSize'], '4096')
        with self.assertRaisesRegex(RuntimeError, '4096'):
            manage.hardware(6, 32, 2, variant='os4-pad')
        self.assertEqual(manage.hardware(8, 32, 2, variant='os4-official')['hw.ramSize'], '8192')
        with patch.object(manage, 'ask', side_effect=lambda zh, en, default: default), \
                patch('sys.stdout', new_callable=io.StringIO) as output:
            settings = manage.options_for(variant='os4-pad')
        self.assertEqual(settings['hw.ramSize'], '4096')
        self.assertEqual(settings['hw.cpu.ncore'], '4')
        self.assertIn('OS4 Pad', output.getvalue())
        self.assertNotIn('8 GiB', output.getvalue())
        with patch.object(manage, 'ask', side_effect=['6', '32', '2']), \
                patch('sys.stdout', new_callable=io.StringIO):
            with self.assertRaisesRegex(RuntimeError, '4096'):
                manage.options_for(variant='os4-pad')
        entry = {'root': Path('/owned/tablet'), 'runtime': {'name': 'My_tablet'},
                 'variant': 'os4-pad', 'version': 'legacy'}
        with patch.object(manage, 'instances', return_value=[entry]), \
                patch.object(manage, 'choose', return_value=0) as choose:
            self.assertEqual(manage.pick_instance(), entry)
            self.assertIn('[OS4 Pad]', choose.call_args.args[1][0])
        with patch('sys.stdout', new_callable=io.StringIO) as output:
            manage.dashboard([entry])
        self.assertIn('[OS4 Pad] My_tablet', output.getvalue())
        with patch.object(manage, 'catalog', return_value=[]), \
                patch('sys.stdout', new_callable=io.StringIO) as output:
            manage.image_updates([entry])
        self.assertIn('OS4 Pad |', output.getvalue())
        self.assertIn('My_tablet: legacy', output.getvalue())

    def test_ascii_panel_aligns_chinese_and_wrapped_paths(self):
        with patch('sys.stdout', new_callable=io.StringIO) as output:
            manage.panel('中文 / English', ['机型 OS3', '/long/path/' * 12], width=60)
        self.assertTrue(all(manage.display_width(line) == 60 for line in output.getvalue().splitlines()))

    def test_zero_returns_without_selecting_the_last_instance(self):
        with patch('builtins.input', return_value='0'), patch('sys.stdout', new_callable=io.StringIO):
            with self.assertRaises(manage.Back):
                manage.choose('Instances', ['first', 'last'])
        with patch('builtins.input', side_effect=['-1', '99', '1']), patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(manage.choose('Instances', ['first', 'last']), 0)

    def test_tui_can_select_os3_from_combined_image_library(self):
        rows = [{'tag_name': tag, 'prerelease': True} for tag in
                ('v0.2.0-a17-hyperos4-hongkong-r1', 'v0.1.0-a16-hyperos3-fuxi-r1')]
        inputs = ['1', '1', '2', '', '', '', '', '', '0']
        with patch.object(manage, 'instances', return_value=[]), patch.object(manage, 'catalog', return_value=rows), \
                patch.object(manage, 'choose_port', return_value=5580), patch.object(manage, 'idle'), \
                patch.object(manage, 'fetch_release', side_effect=manage.Back) as fetch, \
                patch('builtins.input', side_effect=inputs), patch('sys.stdin.isatty', return_value=False), \
                patch('sys.stdout', new_callable=io.StringIO) as output:
            manage.tui()
        self.assertEqual(fetch.call_args.args[0]['tag_name'], rows[1]['tag_name'])
        self.assertIn('RAM: 2.5 GiB', output.getvalue())
        self.assertIn('CPU: 2', output.getvalue())

    def test_explicit_language_skips_duplicate_language_prompt(self):
        with patch.object(manage, 'instances', return_value=[]), \
                patch('builtins.input', return_value='0') as prompt, \
                patch('sys.stdout', new_callable=io.StringIO):
            manage.tui('en')
        self.assertEqual(prompt.call_count, 1)
        self.assertIn('Choose action', prompt.call_args.args[0])

    def test_browse_shows_both_families_without_installed_instances(self):
        rows = [{'tag_name': tag} for tag in
                ('v0.2.0-a17-hyperos4-hongkong-r1', 'v0.1.0-a16-hyperos3-fuxi-r1')]
        with patch.object(manage, 'catalog', return_value=rows), patch('sys.stdout', new_callable=io.StringIO) as output:
            manage.image_updates([])
        self.assertIn('hyperos3-fuxi-r1', output.getvalue())
        self.assertIn('hyperos4-hongkong-r1', output.getvalue())

    def test_partial_download_restart_and_checksum(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source = root / 'source';source.write_bytes(b'payload')
            target = root / 'download'
            target.with_name('download.partial').write_bytes(b'pa')
            manage.download(source.as_uri(), target, 7, hashlib.sha256(b'payload').hexdigest())
            self.assertEqual(target.read_bytes(), b'payload')
            manage.download(source.as_uri(), target, 7, hashlib.sha256(b'payload').hexdigest())

    def test_automatic_port_preserves_stopped_instances(self):
        entries = [{'runtime': {'port': 5580}}, {'runtime': {'port': 5582}}]
        with patch.object(manage, 'instances', return_value=entries), patch.object(manage, 'port_free') as available:
            self.assertEqual(manage.choose_port(), 5584)
            available.assert_called_once_with(5584)

    def test_absolute_or_foreign_backing_path_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / 'userdata-qemu.img').touch()
            (root / 'userdata-qemu.img.qcow2').touch()
            with patch.object(manage, 'data_size', return_value={'backing-filename': '/foreign/userdata-qemu.img'}):
                with self.assertRaisesRegex(RuntimeError, 'backing chain'):
                    manage.validate_userdata(Path('/sdk'), root)
            with patch.object(manage, 'data_size', return_value={'backing-filename': 'userdata-qemu.img'}):
                manage.validate_userdata(Path('/sdk'), root)

    def test_resize_both_images_and_refuse_shrink_before_writes(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / 'userdata-qemu.img').touch();(root / 'userdata-qemu.img.qcow2').touch()
            with patch.object(manage, 'data_size', return_value={'virtual-size': 64 * 1024**3}), patch.object(manage.subprocess, 'run') as run:
                with self.assertRaisesRegex(RuntimeError, 'reduced'):
                    manage.resize(Path('/sdk'), root, 32)
                run.assert_not_called()
            infos = iter([{'virtual-size': 6 * 1024**3, 'format': 'raw'},
                          {'virtual-size': 6 * 1024**3, 'format': 'qcow2'},
                          {'virtual-size': 32 * 1024**3}, {'virtual-size': 32 * 1024**3}])
            with patch.object(manage, 'data_size', side_effect=lambda *a: next(infos)), patch.object(manage.subprocess, 'run') as run:
                manage.resize(Path('/sdk'), root, 32)
                self.assertEqual(sum('resize' in call.args[0] for call in run.call_args_list), 2)

    def test_new_instance_does_not_touch_other_workspace(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            path, _ = self.bundle(folder / 'bundle')
            other = folder / 'other';other.mkdir();(other / 'userdata.img').write_bytes(b'untouched')
            self.invoke(folder / 'instance', path, folder / 'registry')
            self.assertEqual((other / 'userdata.img').read_bytes(), b'untouched')


if __name__ == '__main__':
    unittest.main()
