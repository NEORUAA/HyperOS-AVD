#!/usr/bin/env python3
"""Keep pinned Xiaomi CPU shader preloads without pre-fork GPU drivers."""
import argparse
import hashlib
from pathlib import Path
import struct

NATIVE = '/system/lib64/libhwui.so'
BEFORE = 'da7749e855c370ffce8207427875aad949d8fc35622117a27c8b7fcbd50b70ae'
AFTER = 'fcb22136e65580b52712e74fba987f90c17d20e9f0efba73d2eb910090a4708d'
SIZE = 17432848
SYMBOL = b'zygote_preload_graphics'
SYMBOL_ADDRESS = 0x94add0
SYMBOL_SIZE = 0x70
SITE = 0x94adec
TARGET = 0x94ae10
ORIGINAL = bytes.fromhex('d4a6ff97')
REPLACEMENT = bytes.fromhex('09000014')
PHONE_PROFILE = 'hongkong-4.0.18'
PHONE_BEFORE = '6dfbac6a533ce08b6ace8e7cd7df3c4067350a1f1410729f8c0acd4bc8a78bd4'
PHONE_AFTER = '63e0c54ded534019fdda4a4e31c1755cd88086f2838ff73b76607b0cfd638829'
PROFILES = {
    BEFORE: {'name': 'yingtian', 'before': BEFORE, 'after': AFTER, 'size': SIZE,
             'symbol_address': SYMBOL_ADDRESS, 'symbol_size': SYMBOL_SIZE,
             'site': SITE, 'target': TARGET, 'original': ORIGINAL},
    PHONE_BEFORE: {'name': PHONE_PROFILE, 'before': PHONE_BEFORE, 'after': PHONE_AFTER,
                   'size': 17458360, 'symbol_address': 0x9ecc68, 'symbol_size': 0x50,
                   'site': 0x9ecc78, 'target': 0x9ecc9c,
                   'original': bytes.fromhex('49a6ff97')},
}


def source_profile(data):
    checksum = hashlib.sha256(data).hexdigest()
    for value in PROFILES.values():
        if checksum in (value['before'], value['after']):
            return value, checksum
    raise RuntimeError('Unsupported Xiaomi HWUI SHA-256: ' + checksum)


def phone_hashes(profile):
    """Accept the preload fix only for the independently verified r3 source."""
    original = profile['pins']['hwui']
    if profile['hyperos'] == '4.0.18.0.XFRCNXM' and original == PHONE_BEFORE:
        return (original, PHONE_AFTER)
    return (original,)


def preload_symbol(data):
    """Resolve the exported function and its file offset from this ELF."""
    value, _ = source_profile(data)
    if (len(data) != value['size'] or data[:6] != b'\x7fELF\x02\x01'
            or struct.unpack_from('<H', data, 18)[0] != 183):
        raise RuntimeError('Unexpected Xiaomi HWUI ELF layout.')
    offset = struct.unpack_from('<Q', data, 40)[0]
    entry_size, count = struct.unpack_from('<HH', data, 58)
    if entry_size != 64:
        raise RuntimeError('Unexpected yingtian HWUI section layout.')
    sections = [struct.unpack_from('<IIQQQQIIQQ', data, offset + i * entry_size)
                for i in range(count)]
    matches = []
    for section in sections:
        if section[1] != 11:  # SHT_DYNSYM
            continue
        if section[9] != 24:
            raise RuntimeError('Unexpected yingtian HWUI symbol layout.')
        strings = sections[section[6]]
        names = data[strings[4]:strings[4] + strings[5]]
        for position in range(section[4], section[4] + section[5], 24):
            name, info, _, index, address, size = struct.unpack_from('<IBBHQQ', data, position)
            if names[name:names.find(b'\0', name)] != SYMBOL:
                continue
            code = sections[index]
            if info != 0x12 or not code[2] & 4:  # Global function in SHF_EXECINSTR
                raise RuntimeError('Unexpected yingtian HWUI preload symbol type.')
            matches.append((address, size, code[4] + address - code[3]))
    expected = (value['symbol_address'], value['symbol_size'], value['symbol_address'])
    if matches != [expected]:
        raise RuntimeError('Unexpected Xiaomi HWUI preload symbol or offset.')
    return matches[0]


def patch(data):
    """Skip only driver preload; retain Xiaomi force-dark and SkSL effects.

    The Java zygote preload gate must remain enabled (disable_gl_preload=0).
    Disabling that gate also skips MiBlurBlendUtils::initShaders, leaving its
    overlay effect null. Branch past peekRenderPipelineType, Vulkan instance
    enumeration and eglGetDisplay, while retaining all three OEM calls at the
    pinned target. Renderers initialize their drivers after the fork.
    """
    value, checksum = source_profile(data)
    preload_symbol(data)
    expected = REPLACEMENT if checksum == value['after'] else value['original']
    site = value['site']
    if data[site:site + 4] != expected:
        raise RuntimeError('Unexpected Xiaomi HWUI GPU preload instruction.')
    instruction = struct.unpack('<I', REPLACEMENT)[0]
    if instruction != 0x14000000 | ((value['target'] - site) // 4):
        raise RuntimeError('Unexpected Xiaomi HWUI preload branch target.')
    if checksum == value['after']:
        return data
    result = bytearray(data)
    result[site:site + 4] = REPLACEMENT
    if len(result) != len(data) or hashlib.sha256(result).hexdigest() != value['after']:
        raise RuntimeError('Xiaomi HWUI patch output checksum mismatch.')
    return bytes(result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    data = patch(args.input.read_bytes())
    args.output.write_bytes(data)
    value, checksum = source_profile(data)
    print(value['name'] + ' HWUI CPU shader preload: ' + checksum)


if __name__ == '__main__':
    main()
