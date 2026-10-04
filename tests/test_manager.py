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
    def bundle(self, directory, version='v0.2.1-test', bad=False):
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
        path = directory / 'manifest.json'
        path.write_text(json.dumps(manifest))
        if bad:
            archive.write_bytes(b'broken')
        return path, manifest

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
