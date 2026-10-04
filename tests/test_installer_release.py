"""Check the standalone installer archive and firmware-free metadata."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import package_installer


class InstallerReleaseTests(unittest.TestCase):
    def test_standalone_archive_contains_verified_sources_and_executable_entry(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / 'installer'
            metadata = package_installer.package(out)
            self.assertEqual(metadata['tag'], 'installer-v1.0.0')
            self.assertFalse(metadata['prerelease'])
            self.assertEqual(metadata['type'], 'installer')
            with zipfile.ZipFile(out / metadata['archive']['name']) as archive:
                for name, item in metadata['files'].items():
                    data = archive.read('HyperOS-AVD/' + name)
                    self.assertEqual(hashlib.sha256(data).hexdigest(), item['sha256'])
                    self.assertFalse(any(part in name.split('/') for part in ('images', 'avd', 'backups', 'local')))
                self.assertTrue((archive.getinfo('HyperOS-AVD/Install.command').external_attr >> 16) & 0o111)
                self.assertEqual(json.loads(archive.read('HyperOS-AVD/installer.json'))['type'], 'installer')
            self.assertTrue((out / 'SHA256SUMS').is_file())
            self.assertTrue((out / 'install.sh').stat().st_mode & 0o111)
            self.assertEqual(hashlib.sha256((out / 'install.sh').read_bytes()).hexdigest(),
                             metadata['bootstrap']['sha256'])
            self.assertFalse((out / 'manifest.json').exists())


if __name__ == '__main__':
    unittest.main()
