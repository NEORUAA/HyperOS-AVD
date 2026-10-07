#!/usr/bin/env python3
"""Set only the pinned ranchu secondary display configuration to 60 Hz."""
import argparse
import hashlib
import lzma
from pathlib import Path
import struct

NATIVE = '/vendor/bin/hw/android.hardware.graphics.composer3-service.ranchu'
# Apply patch_composer.patch first so the existing plane-alpha fix is retained.
ORIGINAL = 'e5c002c43532b16250908eb125b1a43f40aade6e783800f82744f6528b47c8ea'
BEFORE = 'b7fd0aa525ebcbf223a400452c0721e75d02a43a298094c4acc47d3053b26ad5'
AFTER = '2aafbaa3fd7f919214be29c8a0e38f8f7e1555ccec61cfd0f7b4fb1113e0f74d'
SIZE = 306640
SYMBOL = (b'_ZN4aidl7android8hardware8graphics9composer34impl12findDisplays'
          b'EPKNS4_9DrmClientEPNSt3__16vectorINS4_19DisplayMultiConfigs'
          b'ENS8_9allocatorISA_EEEE')
FUNCTION_ADDRESS = 0x2b8a0
FUNCTION_SIZE = 0xf24
FUNCTION_BEFORE = '4b81d34018d2273a862f8dd77e7068ea39791175f7c6b2fd702b985d3e241113'
FUNCTION_AFTER = '856b0af7d7ab6cdcf723789880481e455d34a7915b6840595b570f0046fd0c6e'
SITES = ((0x2c648, bytes.fromhex('1ac28b52'), bytes.fromhex('5a058a52')),
         (0x2c654, bytes.fromhex('fa0ba072'), bytes.fromhex('da1fa072')))
STORE_ADDRESS = 0x2c6a4
ORIGINAL_PERIOD_NS = 6250000
PERIOD_NS = 16666666
# The pinned source expresses the independent secondary period as
# HertzToPeriodNanos(160), while primary timing reads ro.boot.qemu.vsync:
# https://android.googlesource.com/device/generic/goldfish-opengl/+/40ce58937bc44cd5ce6959afe7266189bbdd4eca/system/hwc3/DisplayFinder.cpp
MANIFEST = {'revision': 1, 'native': NATIVE, 'native_sha256': AFTER,
            'input_native_sha256': BEFORE, 'secondary_refresh_hz': 60,
            'secondary_vsync_period_ns': PERIOD_NS,
            'primary_vsync_unchanged': True, 'plane_alpha_fix_retained': True}


def _sections(data):
    """Read ELF64 sections without requiring an external analysis package."""
    if (len(data) < 64 or data[:7] != b'\x7fELF\x02\x01\x01'
            or struct.unpack_from('<HH', data, 16) != (3, 183)):
        raise RuntimeError('Unexpected ranchu composer ELF header.')
    offset = struct.unpack_from('<Q', data, 40)[0]
    entry_size, count, names_index = struct.unpack_from('<HHH', data, 58)
    if (entry_size != 64 or not count or names_index >= count
            or offset > len(data) or count * entry_size > len(data) - offset):
        raise RuntimeError('Unexpected ranchu composer ELF section table.')
    sections = [struct.unpack_from('<IIQQQQIIQQ', data, offset + i * entry_size)
                for i in range(count)]
    for section in sections:
        if section[1] == 8:  # SHT_NOBITS has no bytes in the file.
            continue
        if section[4] > len(data) or section[5] > len(data) - section[4]:
            raise RuntimeError('Unexpected ranchu composer ELF section extent.')
    names = sections[names_index]
    if names[1] != 3:  # SHT_STRTAB
        raise RuntimeError('Unexpected ranchu composer ELF section names.')
    strings = bytes(data[names[4]:names[4] + names[5]])
    by_name = {}
    for section in sections:
        end = strings.find(b'\0', section[0])
        if section[0] >= len(strings) or end < 0:
            raise RuntimeError('Unexpected ranchu composer ELF section name.')
        name = strings[section[0]:end]
        if name in by_name:
            raise RuntimeError('Duplicate ranchu composer ELF section name.')
        by_name[name] = section
    return sections, by_name


def find_displays_symbol(data):
    """Resolve the embedded debug symbol and its executable file extent."""
    if len(data) != SIZE or struct.unpack_from('<Q', data, 32)[0] != 64:
        raise RuntimeError('Unexpected ranchu composer ELF layout.')
    _, by_name = _sections(data)
    text = by_name.get(b'.text')
    debug = by_name.get(b'.gnu_debugdata')
    if (text is None or text[1:6] != (1, 6, 0x14000, 0x14000, 0x2b46c)
            or debug is None or debug[1:6] != (1, 0, 0, 0x4813a, 0x2554)):
        raise RuntimeError('Unexpected ranchu composer executable or debug section.')
    try:
        symbols = lzma.decompress(data[debug[4]:debug[4] + debug[5]])
    except lzma.LZMAError as error:
        raise RuntimeError('Invalid ranchu composer embedded debug data.') from error
    sections, _ = _sections(symbols)
    matches = []
    for section in sections:
        if section[1] != 2:  # SHT_SYMTAB
            continue
        if section[9] != 24 or section[5] % 24 or section[6] >= len(sections):
            raise RuntimeError('Unexpected ranchu composer debug symbol table.')
        names = sections[section[6]]
        if names[1] != 3:
            raise RuntimeError('Unexpected ranchu composer debug symbol names.')
        strings = symbols[names[4]:names[4] + names[5]]
        for position in range(section[4], section[4] + section[5], 24):
            name, info, _, index, address, size = struct.unpack_from('<IBBHQQ', symbols, position)
            end = strings.find(b'\0', name)
            if name >= len(strings) or end < 0:
                raise RuntimeError('Unexpected ranchu composer debug symbol name.')
            if strings[name:end] != SYMBOL:
                continue
            if (info != 0x12 or index >= len(sections)
                    or sections[index][1:4] != (8, 6, text[3])):
                raise RuntimeError('Unexpected ranchu composer findDisplays symbol type.')
            matches.append((address, size, text[4] + address - text[3]))
    expected = (FUNCTION_ADDRESS, FUNCTION_SIZE, FUNCTION_ADDRESS)
    if matches != [expected]:
        raise RuntimeError('Unexpected ranchu composer findDisplays symbol or offset.')
    address, size, offset = matches[0]
    if address < text[3] or address + size > text[3] + text[5]:
        raise RuntimeError('Ranchu composer findDisplays exceeds its code section.')
    phoff = struct.unpack_from('<Q', data, 32)[0]
    entry_size, count = struct.unpack_from('<HH', data, 54)
    if (entry_size, count) != (56, 12):
        raise RuntimeError('Unexpected ranchu composer program header layout.')
    executable = []
    for position in range(phoff, phoff + count * entry_size, entry_size):
        kind, flags, file_start, virtual_start, _, file_size, _, _ = struct.unpack_from(
            '<IIQQQQQQ', data, position)
        if (kind == 1 and flags == 5 and virtual_start <= address
                and address + size <= virtual_start + file_size):
            executable.append(file_start + address - virtual_start)
    if executable != [offset]:
        raise RuntimeError('Unexpected ranchu composer findDisplays executable mapping.')
    return matches[0]


def patch(data):
    """Replace secondary timing only after the pinned plane-alpha patch.

    findDisplays builds the primary config from the host and qemu vsync
    property, then separately builds secondary configs with 6250000 ns
    (160 Hz). Its two AArch64 MOVZ/MOVK instructions initialize w26, which
    is written to secondary DisplayConfig::vsyncPeriod at offset 0x14.
    Use 1000000000 / 60 (integer nanoseconds) in that same register. All
    primary timing, display geometry, connection type, groups, discovery,
    composition, ELF metadata and the plane-alpha fix remain unchanged.
    """
    checksum = hashlib.sha256(data).hexdigest()
    if checksum not in (BEFORE, AFTER):
        raise RuntimeError('Unsupported rear-display ranchu composer SHA-256: ' + checksum)
    _, size, offset = find_displays_symbol(data)
    for address, original, replacement in SITES:
        if (address % 4 or address < offset or address + 4 > offset + size
                or data[address:address + 4] != (replacement if checksum == AFTER else original)):
            raise RuntimeError('Unexpected ranchu composer secondary vsync instruction.')
    if data[STORE_ADDRESS:STORE_ADDRESS + 4] != bytes.fromhex('1a1400b9'):
        raise RuntimeError('Unexpected ranchu composer secondary vsync field store.')
    function_checksum = hashlib.sha256(data[offset:offset + size]).hexdigest()
    if function_checksum != (FUNCTION_AFTER if checksum == AFTER else FUNCTION_BEFORE):
        raise RuntimeError('Unexpected ranchu composer findDisplays function body.')
    if checksum == AFTER:
        return data
    result = bytearray(data)
    for address, _, replacement in SITES:
        result[address:address + 4] = replacement
    if len(result) != len(data) or hashlib.sha256(result).hexdigest() != AFTER:
        raise RuntimeError('Ranchu composer secondary vsync patch checksum mismatch.')
    return bytes(result)


def build_vendor(source, destination, work):
    """Retain vendor metadata while chaining rear timing after plane alpha."""
    from build_image import erofs
    from erofs_image import build
    path = NATIVE.removeprefix('/vendor/')
    data = patch(erofs(source, '/' + path))
    build(destination, [('', source)], work,
          {path: (data, 0o755, 'u:object_r:hal_graphics_composer_default_exec:s0')},
          preserve_replacement_metadata=True)
    if hashlib.sha256(erofs(destination, '/' + path)).hexdigest() != AFTER:
        raise RuntimeError('Baked rear-display composer checksum mismatch.')
    return dict(MANIFEST)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    args.output.write_bytes(patch(args.input.read_bytes()))
    print('Ranchu secondary display 60 Hz: ' + AFTER)


if __name__ == '__main__':
    main()
