"""Guard the experimental provider bridge against unsupported firmware."""
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import apply_xiaomi_camera_fix as camera


class XiaomiCameraTests(unittest.TestCase):
    def test_unrecognized_provider_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'Unsupported'):
            camera.patch_provider(b'unsupported provider')

    def test_guarded_patch_changes_only_the_reserved_code_slot(self):
        source = b'X' * camera.OFFSET + camera.BEFORE + b'unrelated instructions'
        before, after = Mock(), Mock()
        before.hexdigest.return_value = camera.PROVIDER_HASH
        after.hexdigest.return_value = camera.PROVIDER_AFTER
        with patch.object(camera.hashlib, 'sha256', side_effect=[before, after]):
            result = camera.patch_provider(source)
        self.assertEqual(len(source), len(result))
        self.assertEqual(source[:camera.OFFSET], result[:camera.OFFSET])
        size = len(camera.BEFORE)
        self.assertEqual(source[camera.OFFSET+size:], result[camera.OFFSET+size:])
        self.assertEqual(camera.AFTER, result[camera.OFFSET:camera.OFFSET+size])

    def test_correct_hash_does_not_bypass_the_instruction_guard(self):
        digest = Mock()
        digest.hexdigest.return_value = camera.PROVIDER_HASH
        with patch.object(camera.hashlib, 'sha256', return_value=digest):
            with self.assertRaisesRegex(RuntimeError, 'Unsupported'):
                camera.patch_provider(b'X' * (camera.OFFSET + len(camera.BEFORE)))

    def test_exact_vendor_mode_and_branch_destinations(self):
        words = [int.from_bytes(camera.AFTER[i:i+4], 'little') for i in range(0, len(camera.AFTER), 4)]
        self.assertEqual(words[0], 0x529200a8)  # mov w8, #0x9005
        self.assertEqual(words[1], 0x6b08009f)  # cmp w4, w8
        self.assertEqual(words[2], 0x54000080)  # b.eq: photo to NORMAL
        self.assertEqual(words[3], 0x52900088)  # mov w8, #0x8004
        self.assertEqual(words[4], 0x6b08009f)  # cmp w4, w8
        self.assertEqual(words[5], 0x54000061)  # b.ne: preserve other invalid modes
        self.assertEqual(words[6], 0x2a1f03e4)  # mov w4, wzr: NORMAL
        for index, destination in ((7, 0x29a98),):
            displacement = (words[index] & 0x3ffffff)
            if displacement & 0x2000000:
                displacement -= 0x4000000
            self.assertEqual(camera.OFFSET + index * 4 + displacement * 4, destination)
        for index, destination in ((2, 0x29b34), (5, 0x29b3c)):
            displacement = (words[index] >> 5) & 0x7ffff
            if displacement & 0x40000:
                displacement -= 0x80000
            self.assertEqual(camera.OFFSET + index * 4 + displacement * 4, destination)
        self.assertEqual(words[8], 0x10ef2213)  # original error tag address

    def test_provider_upgrade_refuses_other_hal_changes(self):
        hwl = {'payload': 'hwl.so', 'target': camera.HWL, 'before': camera.HWL_HASH, 'after': 'a'*64}
        old = {'payload': 'provider', 'target': camera.PROVIDER, 'before': camera.PROVIDER_HASH,
               'after': camera.PROVIDER_REV1}
        new = {**old, 'after': camera.PROVIDER_AFTER}
        saved, newer = {'targets': [old, hwl]}, {'targets': [new, hwl]}
        self.assertTrue(camera.provider_upgrade(saved, newer))
        self.assertFalse(camera.provider_upgrade(newer, newer))
        with self.assertRaisesRegex(RuntimeError, 'Unsupported HAL'):
            camera.provider_upgrade(saved, {'targets': [new, {**hwl, 'after': 'b'*64}]})
        with self.assertRaisesRegex(RuntimeError, 'Unsupported HAL'):
            camera.provider_upgrade({'targets': [{**old, 'after': 'c'*64}, hwl]}, newer)


if __name__ == '__main__':
    unittest.main()
