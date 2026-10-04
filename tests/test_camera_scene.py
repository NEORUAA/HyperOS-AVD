"""Reject unsupported virtual camera binaries and verify the pinned ELF edit."""
from pathlib import Path
import struct
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import patch_camera_scene as scene


class CameraSceneTests(unittest.TestCase):
    def test_unknown_provider_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'Unsupported'):
            scene.patch(b'unknown provider')

    def test_already_patched_provider_is_unchanged(self):
        digest = Mock()
        digest.hexdigest.return_value = scene.AFTER
        with patch.object(scene.hashlib, 'sha256', return_value=digest):
            self.assertEqual(scene.patch(b'already verified'), b'already verified')

    def test_hash_cannot_bypass_the_instruction_guard(self):
        digest = Mock()
        digest.hexdigest.return_value = scene.BEFORE
        with patch.object(scene.hashlib, 'sha256', return_value=digest):
            with self.assertRaisesRegex(RuntimeError, 'instruction'):
                scene.patch(bytes(0x53190))

    def test_dependency_preserves_addresses_and_relro(self):
        source = bytearray(0x53190)
        source[0x34cd4:0x34cd8] = bytes.fromhex('40018052')
        struct.pack_into('<Q', source, 32, 64)
        struct.pack_into('<HH', source, 54, 56, 12)
        struct.pack_into('<IIQQQQQQ', source, 64 + 11 * 56, 4, 4, 0x2f8, 0x2f8,
                         0x2f8, 56, 56, 4)
        # A RELRO header and an existing relocation table must survive verbatim.
        struct.pack_into('<IIQQQQQQ', source, 64 + 8 * 56, 0x6474e552, 4,
                         0x4c000, 0x4c000, 0x4c000, 0x4000, 0x4000, 1)
        source[0x3e18:0x3ec0] = b'R' * (0x3ec0 - 0x3e18)
        for index, entry in enumerate(((5, 0x209c), (10, 7542), (21, 0))):
            struct.pack_into('<QQ', source, 0x4ca98 + index * 16, *entry)
        before, after = Mock(), Mock()
        before.hexdigest.return_value = scene.BEFORE
        after.hexdigest.return_value = scene.AFTER
        with patch.object(scene.hashlib, 'sha256', side_effect=[before, after]):
            result = scene.patch(source)
        self.assertEqual(result[0x34cd4:0x34cd8], bytes.fromhex('00008052'))
        self.assertEqual(result[64 + 8 * 56:64 + 9 * 56], source[64 + 8 * 56:64 + 9 * 56])
        self.assertEqual(result[0x3e18:0x3ec0], source[0x3e18:0x3ec0])
        self.assertEqual(struct.unpack_from('<QQ', result, 0x4ca98), (5, 0x54000))
        self.assertEqual(struct.unpack_from('<QQ', result, 0x4cab8), (1, 7542))
        self.assertEqual(result[0x54000:0x54000 + 7542], source[0x209c:0x209c + 7542])
        self.assertEqual(result[0x54000 + 7542:], scene.LIBRARY.encode() + b'\0')


if __name__ == '__main__':
    unittest.main()
