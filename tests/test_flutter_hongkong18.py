"""Verify the independently audited OS4.0.18 native renderer profile."""
import hashlib
import lzma
import os
from pathlib import Path
import struct
import sys
import unittest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'scripts'))
import patch_flutter

INPUT = '7f88d7f4d9a464fdd56fb255642c9d300f28f50272ccf5fd31f082c1a52e17f4'
OUTPUT = '3ce87f3200841ae53cd869c9f939906d2f904f6af6601c1543365052b577e275'
PREVIOUS = '439bb47881f64431ba43dc4de5788e7f0a3a0c4e78102b47cc8f0daa6229b91b'
TRAMPOLINE = 0x68a968


def sections(data):
    header = struct.unpack_from('<16sHHIQQQIHHHHHH', data)
    rows = [struct.unpack_from('<IIQQQQIIQQ', data, header[6] + index * header[11])
            for index in range(header[12])]
    table = rows[header[13]]
    strings = data[table[4]:table[4] + table[5]]
    return {strings[row[0]:].split(b'\0', 1)[0].decode(): row for row in rows}


def debug_symbols(data):
    section = sections(data)['.gnu_debugdata']
    debug = lzma.decompress(data[section[4]:section[4] + section[5]])
    tables = sections(debug)
    symbols, strings = tables['.symtab'], tables['.strtab']
    names = debug[strings[4]:strings[4] + strings[5]]
    result = {}
    for offset in range(symbols[4], symbols[4] + symbols[5], symbols[9]):
        name, info, _, _, address, size = struct.unpack_from('<IBBHQQ', debug, offset)
        if info & 0xf == 2:
            result[names[name:].split(b'\0', 1)[0].decode()] = (address, size)
    return result


def branch_target(offset, instruction):
    value = struct.unpack('<I', bytes.fromhex(instruction))[0]
    if value & 0xfc000000 != 0x94000000:
        raise AssertionError('Expected an ARM64 BL instruction')
    immediate = value & 0x3ffffff
    if immediate & 0x2000000:
        immediate -= 0x4000000
    return offset + immediate * 4


class Hongkong18FlutterTests(unittest.TestCase):
    def binary(self):
        path = Path(os.environ.get('HYPEROS_AVD_FLUTTER18_ARTIFACT', str(
            REPO / 'work/os4-r3-build/input/hongkong-4.0.18/flutter.bin')))
        if not path.is_file():
            self.skipTest('Original proprietary OTA engine is not distributed in Git.')
        data = path.read_bytes()
        self.assertEqual(hashlib.sha256(data).hexdigest(), INPUT)
        return data

    def test_profile_has_independent_fingerprint_and_both_alignment_call_sites(self):
        profile = patch_flutter.PROFILES[INPUT]
        self.assertEqual(profile['name'], 'system-hongkong-4.0.18')
        self.assertEqual(profile['output'], OUTPUT)
        self.assertEqual(len(profile['sites']), 18)
        sites = {offset: (before, after) for offset, before, after in profile['sites']}
        for offset in (0xb436d0, 0xb43c64):
            self.assertEqual(sites[offset][0], 'f30300aa')
            self.assertEqual(branch_target(offset, sites[offset][1]), TRAMPOLINE)
        self.assertEqual(profile['shadow_sites'], (0xd3d9c0, 0xd3e61c, 0xd3e630))
        with self.assertRaisesRegex(RuntimeError, 'Unsupported Flutter engine'):
            patch_flutter.patch(b'unknown updated engine')

    def test_real_engine_edits_only_audited_sites_and_rejects_other_bytes(self):
        data = self.binary()
        fixed = patch_flutter.patch(data)
        self.assertEqual(hashlib.sha256(fixed).hexdigest(), OUTPUT)
        self.assertEqual(len(data), len(fixed))
        self.assertEqual(patch_flutter.patch(fixed), fixed)
        expected = bytearray(data)
        for offset, before, after in patch_flutter.PROFILES[INPUT]['sites']:
            original, replacement = bytes.fromhex(before), bytes.fromhex(after)
            self.assertEqual(data[offset:offset + len(original)], original)
            expected[offset:offset + len(original)] = replacement
        self.assertEqual(fixed, expected)
        changed = bytearray(data)
        changed[0xde01a8 + 4] ^= 1
        with self.assertRaisesRegex(RuntimeError, 'Unsupported Flutter engine'):
            patch_flutter.patch(changed)

    def test_large_glyph_threshold_is_bounded_and_old_patch_migrates(self):
        data = self.binary()
        fixed = patch_flutter.patch(data)
        offset = patch_flutter.PROFILES[INPUT]['glyph_raster_site']
        self.assertEqual(offset, 0xb2d4a8)
        symbols = debug_symbols(data)
        collect = [value for name, value in symbols.items()
                   if 'TypographerContextSkia16CollectNewGlyphs' in name]
        self.assertEqual(len(collect), 1)
        self.assertLessEqual(collect[0][0], offset + 0x10000)
        self.assertLess(offset + 0x10000, collect[0][0] + collect[0][1])
        for payload, threshold in ((data, 150.0), (fixed, 512.0)):
            instruction = struct.unpack_from('<I', payload, offset)[0]
            self.assertEqual(instruction & 0xffe0001f, 0x52a00008)
            bits = ((instruction >> 5) & 0xffff) << 16
            self.assertEqual(struct.unpack('<f', struct.pack('<I', bits))[0], threshold)
        previous = bytearray(fixed)
        previous[offset:offset + 4] = data[offset:offset + 4]
        self.assertEqual(hashlib.sha256(previous).hexdigest(), PREVIOUS)
        self.assertEqual(patch_flutter.patch(previous), fixed)
        # Only this instruction differs from the already released renderer.
        self.assertEqual(previous[:offset], fixed[:offset])
        self.assertEqual(previous[offset + 4:], fixed[offset + 4:])

    def test_trampoline_is_executable_padding_outside_neighbor_functions(self):
        data = self.binary()
        header = struct.unpack_from('<16sHHIQQQIHHHHHH', data)
        loads = [struct.unpack_from('<IIQQQQQQ', data, header[5] + index * header[9])
                 for index in range(header[10])]
        self.assertTrue(any(kind == 1 and flags & 1 and offset <= TRAMPOLINE
                            and TRAMPOLINE + 24 <= offset + filesz
                            for kind, flags, offset, _, _, filesz, _, _ in loads))
        symbols = debug_symbols(data)
        aes, aes_size = symbols['aes_gcm_dec_kernel']
        following, _ = symbols['bn_mul_mont_words']
        self.assertEqual(aes + aes_size - 0x10000, TRAMPOLINE)
        self.assertEqual(following - 0x10000, TRAMPOLINE + 24)
        self.assertEqual(data[TRAMPOLINE - 4:TRAMPOLINE], bytes.fromhex('c0035fd6'))
        self.assertEqual(data[TRAMPOLINE:TRAMPOLINE + 24], bytes(24))

    def test_changed_constructor_sites_and_viewport_constants_use_new_layout(self):
        data = self.binary()
        symbols = debug_symbols(data)
        constructor = symbols['_ZN8impeller12RenderPassVKC2ERKNSt3_fl10shared_ptrIKNS_7ContextEEERKNS_12RenderTargetENS2_INS_15CommandBufferVKEEE']
        self.assertEqual(constructor, (0xded8ac, 14404))
        for offset in (0xde01a8, 0xde0630):
            self.assertLessEqual(constructor[0], offset + 0x10000)
            self.assertLess(offset + 0x10000, constructor[0] + constructor[1])
        self.assertEqual(symbols['_ZN8impeller12RenderPassVK11SetViewportENS_8ViewportE'],
                         (0xdf2584, 112))
        self.assertEqual(struct.unpack_from('<2f', data, 0x125100), (0, 1))
        self.assertEqual(struct.unpack_from('<2f', data, 0x125110), (1, 0))
        fixed = patch_flutter.patch(data)
        for offset in (0xde0630, 0xde25d0):
            before, after = (struct.unpack_from('<I', payload, offset)[0]
                             for payload in (data, fixed))
            self.assertEqual(((before >> 10) & 0xfff) * 8, 0x100)
            self.assertEqual(((after >> 10) & 0xfff) * 8, 0x110)
        self.assertEqual(fixed[0xde01a8:0xde01ac], bytes.fromhex('00102e1e'))


if __name__ == '__main__':
    unittest.main()
