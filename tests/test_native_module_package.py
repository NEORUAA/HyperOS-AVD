"""Exercise standard KernelSU delivery without a guest or proprietary payload."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / 'scripts'))
import package_native_module as package


class NativeModulePackageTests(unittest.TestCase):
    def test_deterministic_zip_is_self_contained_and_checked(self):
        with tempfile.TemporaryDirectory() as temporary:
            first, second = Path(temporary) / 'a.zip', Path(temporary) / 'b.zip'
            one, two = package.package(first), package.package(second)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertEqual(one['sha256'], two['sha256'])
            with zipfile.ZipFile(first) as archive:
                names = archive.namelist()
                self.assertEqual(len(names), len(set(names)))
                self.assertIn('customize.sh', names)
                self.assertIn('post-fs-data.sh', names)
                self.assertIn('service.sh', names)
                self.assertIn('skip_mount', names)
                self.assertFalse(any(name.startswith('system/') for name in names))
                self.assertFalse(any(name.endswith(('.so', '.apk', '.jar')) for name in names))
                for row in archive.read('SHA256SUMS').decode().splitlines():
                    expected, name = row.split('  ', 1)
                    self.assertEqual(hashlib.sha256(archive.read(name)).hexdigest(), expected)
                    self.assertNotEqual(name, 'customize.sh')
                manifest = json.loads(archive.read('manifest.json'))
                self.assertEqual(manifest['id'], package.MODULE_ID)
                self.assertFalse(manifest['automatic_reboot'])
                self.assertEqual(manifest['catalog_sha256'], hashlib.sha256(archive.read('catalog.json')).hexdigest())
                for name in names:
                    self.assertFalse(Path(name).is_absolute())
                    self.assertNotIn('..', Path(name).parts)
                    if name.endswith('.sh'):
                        self.assertEqual((archive.getinfo(name).external_attr >> 16) & 0o777, 0o755)

    def test_every_packaged_shell_script_parses(self):
        with tempfile.TemporaryDirectory() as temporary:
            for name, body in package.module_files().items():
                if name.endswith('.sh'):
                    target = Path(temporary) / Path(name).name
                    target.write_bytes(body)
                    result = subprocess.run(['sh', '-n', str(target)], capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)

    def test_package_does_not_follow_an_output_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            original = Path(temporary) / 'original'
            original.write_text('preserved')
            link = Path(temporary) / 'module.zip'
            link.symlink_to(original)
            with self.assertRaisesRegex(RuntimeError, 'non-regular'):
                package.package(link)
            self.assertEqual(original.read_text(), 'preserved')

    def test_release_source_list_carries_module_templates(self):
        import package_release
        paths = package_release.runtime_files()
        for name in ('runtime.sh', 'module.prop', 'customize.sh', 'post-fs-data.sh', 'service.sh'):
            self.assertEqual(paths['modules/native-compat/' + name], REPO / 'modules/native-compat' / name)

    def test_lost_staging_race_preserves_the_other_packager_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'module.zip'
            staging = output.with_name(output.name + '.next')
            original_open = Path.open

            def racing_open(path, mode='r', *args, **kwargs):
                if path == staging and mode == 'xb':
                    with original_open(path, 'wb') as other:
                        other.write(b'other operation')
                    raise FileExistsError(str(path))
                return original_open(path, mode, *args, **kwargs)

            with patch.object(Path, 'open', racing_open):
                with self.assertRaises(FileExistsError):
                    package.package(output)
            self.assertEqual(staging.read_bytes(), b'other operation')
            self.assertFalse(output.exists())

    def test_installed_checksums_survive_kernel_su_customizer_removal(self):
        import shutil
        checker = shutil.which('sha256sum') or shutil.which('gsha256sum')
        if checker is None:
            self.skipTest('A SHA256SUMS-compatible host verifier is unavailable.')
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            for name, data in package.module_files().items():
                path = directory / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            (directory / 'customize.sh').unlink()
            result = subprocess.run([checker, '-c', 'SHA256SUMS'], cwd=directory,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
