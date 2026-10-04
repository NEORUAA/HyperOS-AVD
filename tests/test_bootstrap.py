"""Exercise the curl bootstrap's release selection, extraction and cache guards."""
import hashlib
import json
from pathlib import Path
import stat
import tempfile
import types
import unittest
from unittest.mock import patch
import zipfile

SOURCE = Path(__file__).resolve().parents[1] / 'install.sh'
bootstrap = types.ModuleType('bootstrap_test')
code = SOURCE.read_text().split("<<'PY'\n", 1)[1].rsplit('\nPY\n', 1)[0]
exec(compile(code, str(SOURCE), 'exec'), bootstrap.__dict__)


class BootstrapTests(unittest.TestCase):
    def fixture(self, root, extra=None):
        files = {'Install.command': b'#!/bin/zsh\n', 'scripts/manage.py': b'# fixture\n'}
        info = {'project': 'HyperOS-AVD', 'type': 'installer', 'schema': 1,
                'platform': 'macos-arm64', 'tag': 'installer-v1.0.0', 'version': '1.0.0',
                'prerelease': False, 'files': {n: {'size': len(d),
                'sha256': hashlib.sha256(d).hexdigest()} for n, d in files.items()}}
        archive = root / 'HyperOS-AVD-Installer-v1.0.0-macos-arm64.zip'
        with zipfile.ZipFile(archive, 'w') as z:
            for name, data in files.items():
                z.writestr('HyperOS-AVD/' + name, data)
            z.writestr('HyperOS-AVD/installer.json', json.dumps(info))
            if extra is not None:
                z.writestr(extra, b'invalid')
        info['archive'] = {'name': archive.name, 'size': archive.stat().st_size,
                           'sha256': bootstrap.digest(archive)}
        release = {'tag_name': info['tag'], 'prerelease': False, 'assets': [
            {'name': 'installer.json', 'browser_download_url': 'https://example.test/installer.json'},
            {'name': archive.name, 'size': archive.stat().st_size,
             'browser_download_url': archive.as_uri()}]}
        return archive, info, release

    def test_latest_ignores_images_and_preview_installers_and_reads_pages(self):
        first = [{'tag_name': 'v0.2.1-a17-hyperos4-hongkong-r2'}] * 100
        second = [{'tag_name': 'installer-v1.0.0'}, {'tag_name': 'installer-v1.2.0'},
                  {'tag_name': 'installer-v2.0.0', 'prerelease': True}]
        with patch.object(bootstrap, 'remote_json', side_effect=[first, second]):
            self.assertEqual(bootstrap.latest()['tag_name'], 'installer-v1.2.0')

    def test_missing_stable_release_has_explicit_error(self):
        with patch.object(bootstrap, 'remote_json', return_value=[]):
            with self.assertRaisesRegex(RuntimeError, 'No stable'):
                bootstrap.latest()

    def test_metadata_binds_archive_and_source_paths_to_the_selected_release(self):
        with tempfile.TemporaryDirectory() as d:
            _, info, release = self.fixture(Path(d))
            with patch.object(bootstrap, 'remote_json', return_value=info):
                self.assertEqual(bootstrap.metadata(release)[0], info)
                for name in ('../escape', '/absolute', 'scripts//duplicate.py', 'avd/userdata.img'):
                    invalid = {**info, 'files': {**info['files'], name: {'size': 0, 'sha256': '0'*64}}}
                    with patch.object(bootstrap, 'remote_json', return_value=invalid):
                        with self.assertRaisesRegex(RuntimeError, 'source'):
                            bootstrap.metadata(release)
                with patch.object(bootstrap, 'remote_json', return_value={**info, 'tag': 'installer-v9.0.0'}):
                    with self.assertRaisesRegex(RuntimeError, 'match'):
                        bootstrap.metadata(release)

    def test_download_and_extract_under_custom_directory_with_spaces(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            archive, info, _ = self.fixture(root)
            base = root / 'My custom folder'
            cached = base / 'downloads/installer' / archive.name
            bootstrap.fetch(archive.as_uri(), cached, info['archive'])
            destination = base / 'installer' / info['tag']
            folder = bootstrap.extract(cached, destination, info)
            self.assertTrue((folder / 'Install.command').stat().st_mode & 0o111)
            self.assertEqual(bootstrap.extract(cached, destination, info), folder)
            with patch.object(bootstrap.urllib.request, 'urlopen', side_effect=AssertionError('cache was ignored')):
                bootstrap.fetch(archive.as_uri(), cached, info['archive'])
            (folder / 'scripts/manage.py').write_text('# user changed this')
            with self.assertRaisesRegex(RuntimeError, 'Existing installer differs'):
                bootstrap.extract(cached, destination, info)
            self.assertEqual((folder / 'scripts/manage.py').read_text(), '# user changed this')

    def test_corrupt_download_preserves_existing_file_and_removes_partial(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            archive, info, _ = self.fixture(root)
            cached = root / 'cached.zip'
            cached.write_bytes(b'old cache')
            with self.assertRaisesRegex(RuntimeError, 'checksum'):
                bootstrap.fetch(archive.as_uri(), cached, {**info['archive'], 'sha256': '0'*64})
            self.assertEqual(cached.read_bytes(), b'old cache')
            self.assertEqual(sorted(p.name for p in root.iterdir()), sorted([archive.name, 'cached.zip']))

    def test_extra_or_symlink_archive_entry_is_rejected_without_promotion(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            archive, info, _ = self.fixture(root, 'HyperOS-AVD/../../escape')
            destination = root / 'installer'
            with self.assertRaisesRegex(RuntimeError, 'Unexpected'):
                bootstrap.extract(archive, destination, info)
            self.assertFalse(destination.exists())
            archive.unlink()
            archive, info, _ = self.fixture(root)
            with zipfile.ZipFile(archive) as z:
                contents = [(p, z.read(p)) for p in z.infolist()]
            with zipfile.ZipFile(archive, 'w') as z:
                for entry, data in contents:
                    if entry.filename.endswith('Install.command'):
                        entry.external_attr = (stat.S_IFLNK | 0o777) << 16
                    z.writestr(entry, data)
            with self.assertRaisesRegex(RuntimeError, 'symlink'):
                bootstrap.extract(archive, destination, info)
            self.assertFalse(destination.exists())


if __name__ == '__main__':
    unittest.main()
