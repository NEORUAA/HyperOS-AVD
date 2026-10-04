"""Bounded EIO recovery for the pinned ranchu playback PCM library."""
import hashlib
import struct

NATIVE = '/vendor/lib64/libtinyalsav2.so'
BEFORE = '2e63aa3839319ebbe21362db89f63b6d2b7f9486b6d79345eaeb80c3a8adb0b7'
AFTER = 'dcf17a02aaebeda94d171e1ca53158f45f30de80fb9270c202db2be639425788'
# Assembled from native/audio_pcm_recovery.S at 0x18000. All calls stay in
# this ELF; no added dependencies, GOT edits, runtime hooks or ABI changes.
CODE = bytes.fromhex(
    'fd7bbca9fd030091f35301a9f55b02a9f30300aaf40301aaf503022a28d2ff97'
    'f603002a0003f836df060031a1020054ccd9ff97080040b91f15007121020054'
    '680640b9e801f037e00313aaffcdff9720010035e00313aa52cfff97c0000035'
    'e00313aae10314aae203152a14d2ff9705000014bbd9ff97a8008052080000b9'
    'e003162af55b42a9f35341a9fd7bc4a8c0035fd6')
MANIFEST = {'revision': 1, 'native': NATIVE, 'native_sha256': AFTER,
            'max_eio_retries': 1, 'capture_unchanged': True}


def patch(data):
    """Redirect only pcm_writei after its original capture-direction check.

    Tinyalsa recovers EPIPE/ESTRPIPE but returns -1 for EIO; the ranchu
    consumer then drops samples indefinitely. On EIO, stop and prepare
    that same PCM once, and retry the actual write once. Argument errors,
    PCM_NORESTART, other errors and recording keep their original behavior.
    Reuse the NOTE header for a 16 KiB-aligned RX segment. Original notes
    remain in the first LOAD, and all addresses, relocations and RELRO stay
    unchanged. The added segment is never writable.
    """
    checksum = hashlib.sha256(data).hexdigest()
    if checksum == AFTER:
        return data
    if checksum != BEFORE:
        raise RuntimeError('Unsupported playback tinyalsa SHA-256: ' + checksum)
    if (len(data) != 87720 or data[:5] != b'\x7fELF\x02'
            or struct.unpack_from('<Q', data, 32)[0] != 64
            or struct.unpack_from('<HH', data, 54) != (56, 10)
            or data[0xc62c:0xc640] != bytes.fromhex(
                '081c403948002037a2000014a0028012c0035fd6')):
        raise RuntimeError('Unexpected playback tinyalsa ELF layout or entry.')
    note = 64 + 9 * 56
    if struct.unpack_from('<IIQQQQQQ', data, note) != (
            4, 4, 0x270, 0x270, 0x270, 0x50, 0x50, 4):
        raise RuntimeError('Unexpected playback tinyalsa NOTE header.')
    result = bytearray(data)
    struct.pack_into('<I', result, 0xc634, 0x14000000 | ((0x18000 - 0xc634) // 4))
    struct.pack_into('<IIQQQQQQ', result, note, 1, 5,
                     0x18000, 0x18000, 0x18000, len(CODE), len(CODE), 16384)
    result.extend(bytes(0x18000 - len(result)))
    result.extend(CODE)
    if hashlib.sha256(result).hexdigest() != AFTER:
        raise RuntimeError('Playback tinyalsa patch checksum mismatch.')
    return bytes(result)


def build_vendor(source, destination, work):
    """Retain all vendor inode metadata and bake only the playback fix."""
    from build_image import erofs
    from erofs_image import build
    path = NATIVE.removeprefix('/vendor/')
    build(destination, [('', source)], work,
          {path: (patch(erofs(source, '/' + path)),
                  0o644, 'u:object_r:vendor_file:s0')},
          preserve_replacement_metadata=True)
    if hashlib.sha256(erofs(destination, '/' + path)).hexdigest() != AFTER:
        raise RuntimeError('Baked playback tinyalsa verification failed.')
    return dict(MANIFEST)
