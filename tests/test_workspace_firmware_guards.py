"""Keep retained userdata and registrations safe across renamed OS4 workspaces."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import build_os4_pad as pad_builder
import common
import os4_pad
import phone_profile
import setup


class WorkspaceFirmwareGuardTests(unittest.TestCase):
    def test_direct_setup_rejects_forward_and_backward_phone_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'local').mkdir()
            (root / 'avd/Renamed.avd').mkdir(parents=True)
            userdata = root / 'avd/Renamed.avd/userdata-qemu.img'
            userdata.write_bytes(b'private encrypted data')
            receipt = root / 'local/build.json'
            versions = tuple(phone_profile.ARCHIVES)
            for wanted in versions:
                manifest = {'hyperos': wanted, 'build': {'source': common.OS4_SOURCE,
                    'archive_sha256': phone_profile.ARCHIVES[wanted]}}
                for saved_version in versions:
                    receipt.write_text(json.dumps({'source': common.OS4_SOURCE,
                        'hyperos': saved_version, 'archive_sha256': phone_profile.ARCHIVES[saved_version]}))
                    before = receipt.read_bytes()
                    with self.subTest(wanted=wanted, saved=saved_version), \
                            patch.object(setup, 'ROOT', root), \
                            patch.object(setup, 'read_manifest', return_value=(manifest, root)), \
                            patch.object(setup, 'validate_memory', side_effect=RuntimeError('guard passed')) as memory:
                        message = 'guard passed' if wanted == saved_version else 'verified matching firmware'
                        with self.assertRaisesRegex(RuntimeError, message):
                            setup.install_bundle('fixture')
                        self.assertEqual(memory.call_count, int(wanted == saved_version))
                    self.assertEqual(receipt.read_bytes(), before)
                    self.assertEqual(userdata.read_bytes(), b'private encrypted data')
                    self.assertFalse((root / 'downloads').exists())

    def test_pad_accepts_fresh_or_matching_arbitrary_workspace_without_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'Custom-Pad'
            repo = Path(temporary) / 'repository'
            pad_builder.validate_workspace_firmware(os4_pad.PROFILE, root, repo)
            self.assertFalse(root.exists())
            (root / 'local').mkdir(parents=True)
            receipt = root / 'local/build.json'
            receipt.write_text(json.dumps({'source': os4_pad.SOURCE, 'device': os4_pad.PROFILE['device'],
                'hyperos': os4_pad.PROFILE['hyperos'], 'archive_sha256': os4_pad.PROFILE['source_archive_sha256']}))
            (root / 'avd/Renamed.avd').mkdir(parents=True)
            userdata = root / 'avd/Renamed.avd/userdata-qemu.img'
            userdata.write_bytes(b'private Pad data')
            before = receipt.read_bytes()
            pad_builder.validate_workspace_firmware(os4_pad.PROFILE, root, repo)
            self.assertEqual(receipt.read_bytes(), before)
            self.assertEqual(userdata.read_bytes(), b'private Pad data')

    def test_pad_refuses_foreign_version_and_missing_receipt_with_retained_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'Custom-Pad'
            repo = Path(temporary) / 'repository'
            (root / 'local').mkdir(parents=True)
            (root / 'avd/Renamed.avd').mkdir(parents=True)
            userdata = root / 'avd/Renamed.avd/userdata-qemu.img'
            userdata.write_bytes(b'private Pad data')
            receipt = root / 'local/build.json'
            valid = {'source': os4_pad.SOURCE, 'device': os4_pad.PROFILE['device'],
                     'hyperos': os4_pad.PROFILE['hyperos'], 'archive_sha256': os4_pad.PROFILE['source_archive_sha256']}
            invalid = [None, [], {}, '{broken', {**valid, 'source': common.OS4_SOURCE},
                       {**valid, 'hyperos': 'OS4.999.0.0.XBMCNXM'}, {**valid, 'archive_sha256': 'unknown'}]
            for value in invalid:
                with self.subTest(value=value):
                    if value is None:
                        receipt.unlink(missing_ok=True)
                    else:
                        receipt.write_text(value if isinstance(value, str) else json.dumps(value))
                    with self.assertRaisesRegex(RuntimeError, 'existing firmware and userdata'):
                        pad_builder.validate_workspace_firmware(os4_pad.PROFILE, root, repo)
                    self.assertEqual(userdata.read_bytes(), b'private Pad data')

    def test_pad_refuses_shared_roots_and_foreign_registration_before_firmware_work(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary).resolve()
            repo, root = folder / 'repository', folder / 'Custom-Pad'
            for unsafe in (repo, repo / 'work', folder):
                with self.subTest(unsafe=unsafe), self.assertRaisesRegex(RuntimeError, 'separate official tablet'):
                    pad_builder.validate_workspace_firmware(os4_pad.PROFILE, unsafe, repo)
            registry = folder / 'registry'
            registry.mkdir()
            entry = registry / 'Existing.ini'
            data = ('path=' + str(folder / 'foreign/Existing.avd') + '\n').encode()
            entry.write_bytes(data)
            with patch.dict(os.environ), \
                    patch.object(sys, 'argv', ['build_os4_pad.py', '--workspace', str(root),
                                              '--zip', str(folder / 'unused.zip')]), \
                    patch.object(common, 'ROOT', root), patch.object(common, 'host_check'), \
                    patch.object(common, 'avd_home', return_value=registry), \
                    patch.object(setup, 'select_build_instance', return_value=('Existing', 5582)), \
                    patch.object(common, 'firmware_idle') as idle, \
                    patch.object(pad_builder.zipfile, 'ZipFile') as archive:
                with self.assertRaisesRegex(RuntimeError, 'belongs to another workspace'):
                    pad_builder.main()
            idle.assert_not_called()
            archive.assert_not_called()
            self.assertEqual(entry.read_bytes(), data)
            self.assertFalse(root.exists())


if __name__ == '__main__':
    unittest.main()
