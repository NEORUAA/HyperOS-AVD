"""Verify the exact signed-source RGBA video patch and preserved native layout."""
import hashlib
from pathlib import Path
import struct
import sys
import unittest
from unittest.mock import patch
import zipfile

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'scripts'))
import patch_lockscreen_video as video

FIXTURES = REPO / 'work/os4-r3-build/work/fastplayer-analysis'


def checksum(data):
    return hashlib.sha256(data).hexdigest()


class LockscreenVideoPatchTests(unittest.TestCase):
    def native(self):
        path = FIXTURES / 'libfastplayer.so'
        if not path.is_file():
            self.skipTest('Original proprietary MIUIAod library is not distributed in Git.')
        data = path.read_bytes()
        self.assertEqual(checksum(data), video.BEFORE)
        return data

    def apk(self):
        path = FIXTURES / 'MIUIAod.apk'
        if not path.is_file():
            self.skipTest('Original proprietary signed MIUIAod APK is not distributed in Git.')
        data = path.read_bytes()
        self.assertEqual(checksum(data), video.APK_SHA256)
        return data

    def test_public_identity_selects_original_signed_apk_and_external_native_path(self):
        self.assertEqual(video.PACKAGE, 'com.miui.aod')
        self.assertEqual(video.APK, '/product/priv-app/MIUIAod/MIUIAod.apk')
        self.assertEqual(video.ENTRY, 'lib/arm64-v8a/libfastplayer.so')
        self.assertEqual(video.NATIVE, '/product/priv-app/MIUIAod/lib/arm64/libfastplayer.so')
        self.assertEqual(video.MANIFEST['apk_sha256'], video.APK_SHA256)
        self.assertEqual(video.MANIFEST['native_sha256'], video.AFTER)

    def test_exact_verified_output_is_idempotent(self):
        source = self.native()
        result = video.patch(source)
        self.assertEqual(len(result), 658544)
        self.assertEqual(checksum(result), video.AFTER)
        self.assertEqual(video.patch(result), result)

    def test_only_nine_source_instructions_change(self):
        source = self.native()
        expected = bytearray(source)
        # Independent receipt from the visually verified offline candidate.
        substitutions = (
            (0x37b4c, '1f7500b9', '4a038052'),
            (0x37b50, '2afd60d3', '0a7500b9'),
            (0x37b54, '09290d29', '093500f9'),
            (0x49084, '23cb8a72', '23008052'),
            (0x49124, '176d1c12', '770a40b9'),
            (0x49130, 'fa7e4093', 'fa7e7e93'),
            (0x49144, '2a7f4093', '42f47ed3'),
            (0x4914c, '21210a9b', '2121199b'),
            (0x49168, '1f090071', '32000014'),
        )
        for address, before, after in substitutions:
            self.assertEqual(source[address:address + 4], bytes.fromhex(before))
            expected[address:address + 4] = bytes.fromhex(after)
        result = video.patch(source)
        self.assertEqual(result, bytes(expected))
        self.assertEqual(sum(a != b for a, b in zip(source, result)), 27)
        self.assertEqual(result[0x383d0:0x38ec0], source[0x383d0:0x38ec0])

    def test_elf_tables_and_native_function_layout_are_preserved(self):
        source = self.native()
        result = video.patch(source)
        header = struct.unpack_from('<16sHHIQQQIHHHHHH', source)
        self.assertEqual(result[:64], source[:64])
        for offset, size, count in ((header[5], header[9], header[10]),
                                    (header[6], header[11], header[12])):
            self.assertEqual(result[offset:offset + size * count],
                             source[offset:offset + size * count])
        sections = [struct.unpack_from('<IIQQQQIIQQ', source, header[6] + i * header[11])
                    for i in range(header[12])]
        for section in sections:
            if section[1] in (3, 11):  # String tables and exported dynamic symbols.
                offset, size = section[4:6]
                self.assertEqual(result[offset:offset + size], source[offset:offset + size])
        self.assertEqual(video._layout(source), video._layout(result))

    def test_packed_row_arithmetic_and_post_branch(self):
        result = video.patch(self.native())
        instruction = lambda address: struct.unpack_from('<I', result, address)[0]
        # sbfiz x26,x23,#2,#32: sign-extend the 32-bit native stride, then multiply by four.
        stride = instruction(0x49130)
        self.assertEqual((stride & 31, (stride >> 5) & 31), (26, 23))
        self.assertEqual(((stride >> 16) & 63, (stride >> 10) & 63), (62, 31))
        # lsl x2,x2,#2 scales memcpy width, and madd uses the preserved x25 row index.
        self.assertEqual(instruction(0x49144), 0xd37ef442)
        self.assertEqual((instruction(0x4914c) >> 16) & 31, 25)
        branch = instruction(0x49168)
        self.assertEqual(branch & 0xfc000000, 0x14000000)
        self.assertEqual(0x49168 + (branch & 0x3ffffff) * 4, 0x49230)
        self.assertEqual(result[0x49230:0x49254], self.native()[0x49230:0x49254])

    def test_native_extraction_leaves_signed_apk_unchanged(self):
        data = self.apk()
        snapshot = bytes(data)
        with zipfile.ZipFile(FIXTURES / 'MIUIAod.apk') as archive:
            original_native = archive.read(video.ENTRY)
        result = video.native_from_apk(data)
        self.assertEqual(checksum(original_native), video.BEFORE)
        self.assertEqual(result, video.patch(original_native))
        self.assertEqual(data, snapshot)
        self.assertEqual(checksum((FIXTURES / 'MIUIAod.apk').read_bytes()), video.APK_SHA256)

    def test_unknown_native_and_apk_are_rejected(self):
        for data in (b'', b'not an ELF', b'\x7fELF' + bytes(658540)):
            with self.subTest(kind='native', size=len(data)), \
                    self.assertRaisesRegex(RuntimeError, 'Unsupported MIUIAod fastplayer SHA-256'):
                video.patch(data)
        for data in (b'', b'not an APK', b'PK\x03\x04' + bytes(32)):
            with self.subTest(kind='apk', size=len(data)), \
                    self.assertRaisesRegex(RuntimeError, 'Unsupported MIUIAod APK'):
                video.native_from_apk(data)

    def test_partial_patch_and_extra_native_edits_are_rejected(self):
        source = self.native()
        partial = bytearray(source)
        partial[0x37b4c:0x37b50] = bytes.fromhex('4a038052')
        extra = bytearray(video.patch(source))
        extra[0x49234] ^= 1
        for data in (bytes(partial), bytes(extra)):
            with self.subTest(checksum=checksum(data)), \
                    self.assertRaisesRegex(RuntimeError, 'Unsupported MIUIAod fastplayer SHA-256'):
                video.patch(data)

    def test_function_guards_reject_changed_code_even_if_source_pin_is_replaced(self):
        changed = bytearray(self.native())
        changed[0x37b10] ^= 1
        data = bytes(changed)
        with patch.object(video, 'BEFORE', checksum(data)), \
                self.assertRaisesRegex(RuntimeError, 'function byte guard failed'):
            video.patch(data)

    def test_function_guards_also_validate_idempotent_output(self):
        changed = bytearray(video.patch(self.native()))
        changed[0x49150] ^= 1
        data = bytes(changed)
        with patch.object(video, 'AFTER', checksum(data)), \
                self.assertRaisesRegex(RuntimeError, 'function byte guard failed'):
            video.patch(data)

    def test_changed_replacement_is_rejected_by_verified_output_pin(self):
        sites = list(video.SITES)
        address, before, after = sites[-1]
        sites[-1] = (address, before, bytes.fromhex('33000014'))
        with patch.object(video, 'SITES', tuple(sites)), \
                self.assertRaisesRegex(RuntimeError, 'patch output checksum mismatch'):
            video.patch(self.native())

    def test_modified_signed_apk_is_rejected(self):
        data = bytearray(self.apk())
        data[-1] ^= 1
        with self.assertRaisesRegex(RuntimeError, 'Unsupported MIUIAod APK'):
            video.native_from_apk(bytes(data))

    def test_apk_cli_rejects_same_path_and_hardlink_before_any_write(self):
        source = REPO / 'MIUIAod.apk'
        for output in (source, REPO / 'MIUIAod-native.so'):
            with self.subTest(output=output), \
                    patch.object(sys, 'argv', ['patch_lockscreen_video.py', '--apk',
                                               str(source), str(output)]), \
                    patch.object(Path, 'exists', return_value=True), \
                    patch.object(Path, 'samefile', return_value=True), \
                    patch.object(Path, 'read_bytes') as read, \
                    patch.object(Path, 'write_bytes') as write, \
                    self.assertRaisesRegex(RuntimeError, 'APK input must not be overwritten'):
                video.main()
            read.assert_not_called()
            write.assert_not_called()


if __name__ == '__main__':
    unittest.main()
