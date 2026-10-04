"""Guard the physical display alpha fix against unrelated vendor updates."""
import hashlib
from pathlib import Path
import sys
import unittest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'scripts'))
from patch_composer import AFTER, MANIFEST, NATIVE, patch

SOURCE = REPO / 'work/os4-official/work/activity-dim/android.hardware.graphics.composer3-service.ranchu'


class ComposerGuardTest(unittest.TestCase):
    def test_unknown_vendor_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'Unsupported ranchu composer'):
            patch(b'unknown vendor update')

    @unittest.skipUnless(SOURCE.is_file(), 'Verified vendor ELF is not bundled')
    def test_pinned_elf_has_only_one_byte_changed(self):
        original = SOURCE.read_bytes()
        result = patch(original)
        self.assertEqual(hashlib.sha256(result).hexdigest(), AFTER)
        self.assertEqual(len(result), len(original))
        self.assertEqual([i for i, (a, b) in enumerate(zip(original, result)) if a != b], [0x3a17a])
        self.assertEqual(patch(result), result)
        tampered = bytearray(original)
        tampered[-1] ^= 1
        with self.assertRaisesRegex(RuntimeError, 'Unsupported ranchu composer'):
            patch(tampered)
        self.assertEqual((MANIFEST['native'], MANIFEST['native_sha256']), (NATIVE, AFTER))


if __name__ == '__main__':
    unittest.main()
