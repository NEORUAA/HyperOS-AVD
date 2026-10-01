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


if __name__ == '__main__':
    unittest.main()
