"""Guard the secondary timing patch and its chain after the alpha fix."""
import hashlib
from pathlib import Path
import struct
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch as mock_patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'scripts'))
import patch_composer
import patch_rear_display as rear

SOURCE = REPO / 'work/os4-r3-build/work/rear-display-audit/composer3-ranchu.bin'
REAL_SHA256 = hashlib.sha256


def pretend_pinned_digest(data):
    """Bypass only the whole-file hash to exercise secondary guard layers."""
    if len(data) == rear.SIZE:
        return SimpleNamespace(hexdigest=lambda: rear.BEFORE)
    return REAL_SHA256(data)


class RearDisplayGuardTest(unittest.TestCase):
    def test_unknown_composer_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'Unsupported rear-display ranchu composer'):
            rear.patch(b'unknown vendor update')

    def test_patch_chain_and_manifest_are_pinned(self):
        self.assertEqual(rear.ORIGINAL, patch_composer.BEFORE)
        self.assertEqual(rear.BEFORE, patch_composer.AFTER)
        self.assertEqual((rear.MANIFEST['native'], rear.MANIFEST['native_sha256']),
                         (patch_composer.NATIVE, rear.AFTER))
        self.assertEqual(rear.MANIFEST['input_native_sha256'], patch_composer.AFTER)
        self.assertEqual(rear.MANIFEST['secondary_refresh_hz'], 60)
        self.assertEqual(rear.MANIFEST['secondary_vsync_period_ns'], 1000000000 // 60)
        self.assertTrue(rear.MANIFEST['primary_vsync_unchanged'])
        self.assertTrue(rear.MANIFEST['plane_alpha_fix_retained'])

    def test_instruction_encodings_keep_the_destination_register(self):
        # Decode the AArch64 move-wide fields independently of patch().
        expected = ((0x2c648, 0x52800000, 0, 0x5e10, 0x502a),
                    (0x2c654, 0x72800000, 1, 0x005f, 0x00fe))
        self.assertEqual(len(rear.SITES), 2)
        periods = [0, 0]
        for (address, before, after), (site, opcode, shift, old, new) in zip(rear.SITES, expected):
            self.assertEqual(address, site)
            for index, (encoded, immediate) in enumerate(((before, old), (after, new))):
                instruction, = struct.unpack('<I', encoded)
                self.assertEqual(instruction & 0xff800000, opcode)
                self.assertEqual((instruction >> 21) & 3, shift)
                self.assertEqual(instruction & 31, 26)
                self.assertEqual((instruction >> 5) & 0xffff, immediate)
                periods[index] |= immediate << (16 * shift)
        self.assertEqual(periods, [6250000, 1000000000 // 60])


@unittest.skipUnless(SOURCE.is_file(), 'Verified vendor ELF is not bundled')
class PinnedRearDisplayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original = SOURCE.read_bytes()
        cls.alpha = patch_composer.patch(cls.original)

    def test_original_elf_requires_the_existing_alpha_fix(self):
        self.assertEqual(REAL_SHA256(self.original).hexdigest(), rear.ORIGINAL)
        with self.assertRaisesRegex(RuntimeError, 'Unsupported rear-display ranchu composer'):
            rear.patch(self.original)

    def test_embedded_symbol_resolves_the_function_file_extent(self):
        self.assertEqual(rear.find_displays_symbol(self.alpha), (0x2b8a0, 0xf24, 0x2b8a0))
        for address in (0x2c648, 0x2c654, 0x2c6a4):
            self.assertGreaterEqual(address, 0x2b8a0)
            self.assertLess(address + 4, 0x2c7c4)

    def test_only_secondary_constant_changes_and_patch_is_idempotent(self):
        result = rear.patch(self.alpha)
        self.assertEqual(REAL_SHA256(result).hexdigest(),
                         '2aafbaa3fd7f919214be29c8a0e38f8f7e1555ccec61cfd0f7b4fb1113e0f74d')
        self.assertEqual(len(result), len(self.alpha))
        self.assertEqual([i for i, (a, b) in enumerate(zip(self.alpha, result)) if a != b],
                         [0x2c648, 0x2c649, 0x2c64a, 0x2c654, 0x2c655])
        self.assertEqual(result[:0x2c648], self.alpha[:0x2c648])
        self.assertEqual(result[0x2c64c:0x2c654], self.alpha[0x2c64c:0x2c654])
        self.assertEqual(result[0x2c658:], self.alpha[0x2c658:])
        self.assertEqual(result[0x2c6a4:0x2c6a8], bytes.fromhex('1a1400b9'))
        self.assertEqual(result[0x3a178:0x3a17c], bytes.fromhex('0028201e'))
        self.assertEqual(rear.patch(result), result)
        self.assertEqual(rear.find_displays_symbol(result), (0x2b8a0, 0xf24, 0x2b8a0))

    def test_unrelated_update_is_rejected_without_mutating_input(self):
        tampered = bytearray(self.alpha)
        tampered[-1] ^= 1
        unchanged = bytes(tampered)
        with self.assertRaisesRegex(RuntimeError, 'Unsupported rear-display ranchu composer'):
            rear.patch(tampered)
        self.assertEqual(bytes(tampered), unchanged)

    def test_instruction_guard_rejects_a_different_register(self):
        tampered = bytearray(self.alpha)
        tampered[0x2c648] ^= 1  # Change w26 to w27 without changing its immediate.
        with mock_patch.object(rear.hashlib, 'sha256', side_effect=pretend_pinned_digest):
            with self.assertRaisesRegex(RuntimeError, 'secondary vsync instruction'):
                rear.patch(tampered)

    def test_field_store_guard_rejects_a_different_field(self):
        tampered = bytearray(self.alpha)
        tampered[0x2c6a5] ^= 4
        with mock_patch.object(rear.hashlib, 'sha256', side_effect=pretend_pinned_digest):
            with self.assertRaisesRegex(RuntimeError, 'secondary vsync field store'):
                rear.patch(tampered)

    def test_function_guard_covers_unmodified_primary_code(self):
        tampered = bytearray(self.alpha)
        tampered[0x2bfc4] ^= 1  # Primary DisplayConfig field store remains guarded.
        with mock_patch.object(rear.hashlib, 'sha256', side_effect=pretend_pinned_digest):
            with self.assertRaisesRegex(RuntimeError, 'findDisplays function body'):
                rear.patch(tampered)

    def test_elf_mapping_guard_rejects_an_offset_change(self):
        tampered = bytearray(self.alpha)
        # The RX PT_LOAD must map the symbol VA to its verified file offset.
        struct.pack_into('<Q', tampered, 64 + 3 * 56 + 8, 0x14004)
        with mock_patch.object(rear.hashlib, 'sha256', side_effect=pretend_pinned_digest):
            with self.assertRaisesRegex(RuntimeError, 'findDisplays executable mapping'):
                rear.patch(tampered)


if __name__ == '__main__':
    unittest.main()
