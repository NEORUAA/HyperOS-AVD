#!/usr/bin/env python3
"""Use packed RGBA for the pinned MIUIAod native video window producer."""
import argparse
import hashlib
import io
from pathlib import Path
import struct
import zipfile

PACKAGE = 'com.miui.aod'
APK = '/product/priv-app/MIUIAod/MIUIAod.apk'
APK_SHA256 = 'eae2b82953e6413c33315a0445577f0b2700204e7cf6d1dce8733e0866403674'
ENTRY = 'lib/arm64-v8a/libfastplayer.so'
NATIVE = '/product/priv-app/MIUIAod/lib/arm64/libfastplayer.so'
BEFORE = '207a906f28319233173ca4deae264c8a7d6d0f927778bb18ae044e6013e5c814'
AFTER = '128ab8cf7bce3224819803e1a94969f1609e58be1157ed2f6f741e4091c62d44'
SIZE = 658544
MANIFEST = {'revision': 1, 'package': PACKAGE, 'apk': APK, 'apk_sha256': APK_SHA256,
            'native': NATIVE, 'native_before': BEFORE, 'native_sha256': AFTER,
            'video_pixel_format': 'rgba', 'native_window_format': 'RGBA_8888'}
# Symbol, address, size, full original function hash, full patched function hash.
FUNCTIONS = (
    ('_ZN10fastplayer15ResourceControl14workThreadFuncEv', 0x376b0, 0x8f0,
     '9b6b5e650fe1fb798b7828a46f65e3c9837863b9cffed2342263cf156923f109',
     '52e4467703c30261ad2ec3ad3784c7f47311022c7ceaec93064fa5b805ba1ddc'),
    ('_ZN10fastplayer15ResourceControl15videoThreadFuncEv', 0x383d0, 0xaf0,
     '02158991480c08658260a7831987ed532c8370c454d980a76ad76406302351a5',
     '02158991480c08658260a7831987ed532c8370c454d980a76ad76406302351a5'),
    ('_ZN10fastplayer10ANWDisplay4InitEii', 0x49014, 0x94,
     '4bdc498b4bbbd048908fbfa7a2f17a4312400dce9b4c48cbbe60741d1b789eb5',
     '052b481d3bd95186696be1496a6104c34a0a1f7d091bc537bfdc2e914cf13605'),
    ('_ZN10fastplayer10ANWDisplay13renderPictureEP7AVFrame', 0x490a8, 0x1ac,
     'a3d96ee5ca50252f8133f531d553777ea4d584b6c2972d3108fea10b03571138',
     '37cceee098d2b34c8e48e5d54bebe5d39bd05d9c6d4b3df402090c03f5335f40'),
)
# Existing virtual addresses equal file offsets in this verified executable LOAD.
SITES = (
    # Store AV_PIX_FMT_RGBA=26; the packed x9 store preserves width and height.
    (0x37b4c, bytes.fromhex('1f7500b9'), bytes.fromhex('4a038052')),
    (0x37b50, bytes.fromhex('2afd60d3'), bytes.fromhex('0a7500b9')),
    (0x37b54, bytes.fromhex('09290d29'), bytes.fromhex('093500f9')),
    # mov w3,#1 replaces the final YV12 immediate with WINDOW_FORMAT_RGBA_8888.
    (0x49084, bytes.fromhex('23cb8a72'), bytes.fromhex('23008052')),
    # Reload actual pixel stride, then sign-extend and multiply it by four.
    (0x49124, bytes.fromhex('176d1c12'), bytes.fromhex('770a40b9')),
    (0x49130, bytes.fromhex('fa7e4093'), bytes.fromhex('fa7e7e93')),
    # memcpy length is width*4; source row offset uses preserved row counter x25.
    (0x49144, bytes.fromhex('2a7f4093'), bytes.fromhex('42f47ed3')),
    (0x4914c, bytes.fromhex('21210a9b'), bytes.fromhex('2121199b')),
    # Branch to unlockAndPost, bypassing both planar chroma-copy loops.
    (0x49168, bytes.fromhex('1f090071'), bytes.fromhex('32000014')),
)


def _checksum(data):
    return hashlib.sha256(data).hexdigest()


def _layout(data):
    """Resolve native functions and preserve ELF headers, tables and ABI."""
    if (len(data) != SIZE or data[:7] != b'\x7fELF\x02\x01\x01'
            or struct.unpack_from('<H', data, 18)[0] != 183):
        raise RuntimeError('Unexpected MIUIAod fastplayer ELF layout.')
    header = struct.unpack_from('<16sHHIQQQIHHHHHH', data)
    if header[9] != 56 or header[11] != 64:
        raise RuntimeError('Unexpected MIUIAod fastplayer table layout.')
    segments = [struct.unpack_from('<IIQQQQQQ', data, header[5] + i * header[9])
                for i in range(header[10])]
    sections = [struct.unpack_from('<IIQQQQIIQQ', data, header[6] + i * header[11])
                for i in range(header[12])]
    symbols = {}
    for section in sections:
        if section[1] != 11:  # SHT_DYNSYM
            continue
        if section[9] != 24:
            raise RuntimeError('Unexpected MIUIAod fastplayer symbol layout.')
        strings = sections[section[6]]
        names = data[strings[4]:strings[4] + strings[5]]
        for offset in range(section[4], section[4] + section[5], 24):
            name, info, _, index, address, size = struct.unpack_from('<IBBHQQ', data, offset)
            text = names[name:names.index(b'\0', name)].decode()
            if text not in {value[0] for value in FUNCTIONS}:
                continue
            code = sections[index]
            if info != 0x12 or not code[2] & 4 or text in symbols:
                raise RuntimeError('Unexpected MIUIAod fastplayer function type.')
            symbols[text] = (address, size, code[4] + address - code[3])
    return header, segments, sections, symbols


def _validate_functions(data, patched):
    layout = _layout(data)
    symbols = layout[3]
    for name, address, size, before_hash, after_hash in FUNCTIONS:
        if symbols.get(name) != (address, size, address):
            raise RuntimeError('Unexpected MIUIAod fastplayer function or offset: ' + name)
        expected = after_hash if patched else before_hash
        if _checksum(data[address:address + size]) != expected:
            raise RuntimeError('MIUIAod fastplayer function byte guard failed: ' + name)
    # The second ANW caller already requests RGBA and must stay unchanged.
    if data[0x38a8c:0x38a94] != bytes.fromhex('49038052097500b9'):
        raise RuntimeError('Unexpected MIUIAod videoThread RGBA instructions.')
    return layout


def patch(data):
    """Return the verified RGBA native library without changing its ABI.

    WorkThread converts Main10 video to RGBA rather than planar YUV. Shared
    ANW rendering copies width*4 bytes per row into the actual stride*4 buffer
    and posts it directly, avoiding the host YV12 import path. VideoThread
    already requests RGBA and remains byte-for-byte unchanged.
    """
    checksum = _checksum(data)
    if checksum not in (BEFORE, AFTER):
        raise RuntimeError('Unsupported MIUIAod fastplayer SHA-256: ' + checksum)
    patched = checksum == AFTER
    layout = _validate_functions(data, patched)
    for address, before, after in SITES:
        if data[address:address + 4] != (after if patched else before):
            raise RuntimeError(f'Unexpected MIUIAod fastplayer instruction at {address:#x}')
    if patched:
        return data
    result = bytearray(data)
    for address, before, after in SITES:
        result[address:address + 4] = after
    result = bytes(result)
    if len(result) != len(data) or _checksum(result) != AFTER:
        raise RuntimeError('MIUIAod fastplayer patch output checksum mismatch.')
    if _validate_functions(result, True) != layout:
        raise RuntimeError('MIUIAod fastplayer patch changed ELF layout or ABI.')
    instruction = struct.unpack_from('<I', result, 0x49168)[0]
    if instruction != 0x14000032 or 0x49168 + (instruction & 0x3ffffff) * 4 != 0x49230:
        raise RuntimeError('Unexpected MIUIAod fastplayer post branch target.')
    return result


def native_from_apk(data):
    """Read the pinned signed APK and return only its patched native library."""
    if _checksum(data) != APK_SHA256:
        raise RuntimeError('Unsupported MIUIAod APK; original signature is preserved.')
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        if archive.namelist().count(ENTRY) != 1:
            raise RuntimeError('Expected exactly one MIUIAod fastplayer library.')
        return patch(archive.read(ENTRY))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--apk', action='store_true', help='Extract from the verified signed APK')
    args = parser.parse_args()
    if args.apk and (args.input.resolve() == args.output.resolve()
                     or (args.output.exists() and args.input.samefile(args.output))):
        raise RuntimeError('Signed MIUIAod APK input must not be overwritten.')
    data = args.input.read_bytes()
    result = native_from_apk(data) if args.apk else patch(data)
    args.output.write_bytes(result)
    print('MIUIAod RGBA video native library: ' + _checksum(result))


if __name__ == '__main__':
    main()
