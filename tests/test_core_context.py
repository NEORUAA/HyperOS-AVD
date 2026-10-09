"""Verify image provenance and path admission before guest migration."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import core_context
import package_release


class CoreContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.assets = self.root / 'tools/os4-core'
        self.assets.mkdir(parents=True)
        (self.root / 'local').mkdir()
        self.image = 'a' * 64
        (self.root / 'local/build.json').write_text(json.dumps({'system_sha256': self.image}))
        self.context = {'schema': 1, 'identity_sources': [
            {'path': '/system/build.prop', 'sha256': 'b' * 64},
            {'path': '/product/etc/build.prop', 'sha256': 'c' * 64}],
            'rear': None, 'boot_services': None}
        self.write()

    def write(self):
        data = core_context.canonical(self.context)
        (self.assets / 'context.json').write_bytes(data)
        receipt = {'schema': 1, 'source_packed_sha256': self.image,
                   'context_sha256': hashlib.sha256(data).hexdigest()}
        (self.assets / 'receipt.json').write_bytes(core_context.canonical(receipt))

    def test_receipts_must_agree_with_installed_image(self):
        self.assertEqual(core_context.load(self.root), self.context)
        (self.root / 'local/installed-release.json').write_text(json.dumps({
            'files': {'images/system.img': {'sha256': 'd' * 64}}}))
        with self.assertRaisesRegex(RuntimeError, 'disagree'):
            core_context.load(self.root)

    def test_mutated_context_is_rejected_before_use(self):
        (self.assets / 'context.json').write_text('{}')
        with self.assertRaisesRegex(RuntimeError, 'content changed'):
            core_context.load(self.root)

    def test_valid_receipt_cannot_admit_unreviewed_paths(self):
        self.context['identity_sources'][0]['path'] = '/data/private/build.prop'
        self.write()
        with self.assertRaisesRegex(RuntimeError, 'identity source path'):
            core_context.load(self.root)

    def test_aliased_assets_are_not_read(self):
        source = self.assets / 'context.json'
        source.rename(self.assets / 'foreign.json')
        source.symlink_to('foreign.json')
        with self.assertRaisesRegex(RuntimeError, 'aliased'):
            core_context.load(self.root)

    def test_both_variants_ship_same_complete_module_runtime(self):
        self.assertEqual(package_release.RUNTIME_PAYLOADS['os4-official'],
                         package_release.RUNTIME_PAYLOADS['os4-pad'])
        files = package_release.runtime_files()
        for name in ('scripts/core_context.py', 'scripts/core_platform.py',
                     'scripts/apply_app_compat.py', 'scripts/app_compat_legacy_records.json',
                     'scripts/app_compat_history.json',
                     'modules/compat-webui/app.js', 'modules/compat-webui/status.sh',
                     'modules/native-compat/platform.sh', 'modules/app-bridge/catalog.sh'):
            self.assertIn(name, files)
        self.assertFalse(any('/state/' in name or 'runtime.log' in name for name in files))


if __name__ == '__main__':
    unittest.main()
