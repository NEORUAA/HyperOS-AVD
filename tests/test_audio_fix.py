"""Guard the bounded PCM recovery patch and retained vendor ELF metadata."""
import hashlib
from pathlib import Path
import struct
import sys
import unittest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'scripts'))
from patch_audio import AFTER, CODE, patch

SOURCE = REPO / 'work/os4-official/work/audio-output/source/libtinyalsav2.so'


class PlaybackGuardTest(unittest.TestCase):
    def test_unknown_vendor_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'Unsupported playback tinyalsa'):
            patch(b'unknown vendor update')

    @unittest.skipUnless(SOURCE.is_file(), 'Verified vendor ELF is not bundled')
    def test_elf_retains_abi_and_relro(self):
        original = SOURCE.read_bytes()
        result = patch(original)
        self.assertEqual(hashlib.sha256(result).hexdigest(), AFTER)
        self.assertEqual(patch(result), result)
        # Existing code/data changes only at the branch and the reused NOTE.
        changed = {i for i, (a, b) in enumerate(zip(original, result)) if a != b}
        allowed = set(range(0xc634, 0xc638)) | set(range(64+9*56, 64+10*56))
        self.assertTrue(changed <= allowed)
        self.assertEqual(result[0xc62c:0xc634], original[0xc62c:0xc634])
        self.assertEqual(result[64+6*56:64+7*56], original[64+6*56:64+7*56])
        self.assertEqual(result[0x10248:0x105f0], original[0x10248:0x105f0])
        header = struct.unpack_from('<IIQQQQQQ', result, 64+9*56)
        self.assertEqual(header, (1, 5, 0x18000, 0x18000, 0x18000,
                                  len(CODE), len(CODE), 16384))
        self.assertEqual(result[0x18000:], CODE)

    @unittest.skipUnless(SOURCE.is_file(), 'Verified vendor ELF is not bundled')
    def test_tampered_vendor_is_rejected(self):
        data = bytearray(SOURCE.read_bytes())
        data[-1] ^= 1
        with self.assertRaisesRegex(RuntimeError, 'Unsupported playback tinyalsa'):
            patch(data)


if __name__ == '__main__':
    unittest.main()
