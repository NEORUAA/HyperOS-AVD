"""Check image import integrity and isolation with small synthetic release files."""
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
import setup
import package_release


class InstallerTests(unittest.TestCase):
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


if __name__ == '__main__':
    unittest.main()
