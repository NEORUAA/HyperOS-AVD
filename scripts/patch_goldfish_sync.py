"""Backport bounded threaded IRQ draining to the pinned goldfish sync module.

The original ISR reads the entire host command list into a 32-entry array
before scheduling its worker. WARN_ON at entry 32 does not prevent the write.
This backport keeps the array, state layout, command worker and fence behavior
intact. A masked IRQ thread processes each full batch before reading more.

Source: kernel/common-modules/virtual-device f7f0c0e682b4e5c9e1feb5d5a056c32feaac65d8,
goldfish_drivers/goldfish_sync.c. The matching kernel b66429556fb8 supports
NULL primary handlers with IRQF_ONESHOT and R_AARCH64_NONE relocations.
"""
import hashlib
import struct
import zlib

NATIVE = '/vendor/lib/modules/goldfish_sync.ko'
BEFORE = 'cef4fec0e8e48a9856a7496d97f4402518f29305ac905604942bd3ac83f9500d'
AFTER = 'cfb6bb17a8f126b53dd12ae70fc57e9139769f1ac13a10ecea0b530cae4b2c4a'
VERMAGIC = ('6.6.66-android15-8-gb66429556fb8-ab13070261-4k SMP preempt '
            'mod_unload modversions aarch64')
SOURCE_REVISION = 'f7f0c0e682b4e5c9e1feb5d5a056c32feaac65d8'
TEXT_OFFSET = 0x1000
IRQ_START = 0xc74
IRQ_END = 0xd5c
RELA_OFFSET = 0x3c08
SIGNATURE_MAGIC = b'~Module signature appended~\n'

# Build this source with aarch64-linux-android36-clang -c, then take .text
# [IRQ_START:IRQ_END]. The local labels establish direct BL displacements;
# the two external lock calls and fops address retain module relocations.
# Padding retains the original PAC prologue/epilogue offsets for unwind data.
ASSEMBLY = r""".text
.org 0xb2c
goldfish_sync_work_item_fn:
.org 0xc74
paciasp
stp x29,x30,[sp,#-0x30]!
str x21,[sp,#0x10]
stp x20,x19,[sp,#0x20]
mov x29,sp
ldr x8,[x1,#0x10]
adrp x9,goldfish_sync_fops
add x9,x9,:lo12:goldfish_sync_fops
cmp x8,x9
b.ne not_ready
add x20,x1,#0x39c
mov x19,x1
batch: mov x0,x20
bl _raw_spin_lock
mov w21,#1
read: ldr w8,[x19,#0x398]
cmp w8,#32
b.hs process
ldr x0,[x19,#0x50]
bl readl
ldr w8,[x19,#0x3b0]
cbz w8,empty
ldr w12,[x19,#0x398]
ldr x11,[x19,#0x3a0]
ldr x10,[x19,#0x3a8]
ldr w9,[x19,#0x3b4]
mov w13,#24
umaddl x12,w12,w13,x19
stp x11,x10,[x12,#0x98]
stp w8,w9,[x12,#0xa8]
ldr w8,[x19,#0x398]
add w8,w8,#1
str w8,[x19,#0x398]
b read
empty: mov w21,#0
process: mov x0,x20
bl _raw_spin_unlock
add x0,x19,#0x3d8
bl goldfish_sync_work_item_fn
cbnz w21,batch
mov w0,#1
b return
.rept 10
nop
.endr
not_ready: mov w0,#0
return: ldp x20,x19,[sp,#0x20]
ldr x21,[sp,#0x10]
ldp x29,x30,[sp],#0x30
autiasp
ret
.org 0x119c
readl:
"""
CODE = bytes.fromhex(
    '3f2303d5fd7bbda9f50b00f9f44f02a9fd030091280840f90900009029010091'
    '1f0109eb6105005434700e91f30301aae00314aa0000009435008052689a43b9'
    '1f81007142020054602a40f93701009468b243b9a80100346c9a43b96bd241f9'
    '6ad641f969b643b90d0380528c4dad9b8ba909a988251529689a43b908050011'
    '689a03b9eeffff1715008052e00314aa0000009460620f9188ffff97b5fcff35'
    '200080520c0000141f2003d51f2003d51f2003d51f2003d51f2003d51f2003d5'
    '1f2003d51f2003d51f2003d51f2003d500008052f44f42a9f50b40f9fd7bc3a8'
    'bf2303d5c0035fd6')

# Entry index -> original (offset, symbol/type, addend). These are all ISR
# relocations. Removing async scheduling must also disable its old relocations
# so the loader cannot rewrite the replacement NOPs into old instructions.
ORIGINAL_RELOCATIONS = {
    105: (0xc8c, 0x7c00000113, 0x230),
    106: (0xc90, 0x7c00000115, 0x230),
    107: (0xcb0, 0xc90000011b, 0),
    108: (0xd2c, 0xca0000011b, 0),
    109: (0xd30, 0xcb00000113, 0),
    110: (0xd3c, 0xcb0000011e, 0),
    111: (0xd40, 0xcc0000011b, 0),
}
REPLACEMENT_RELOCATIONS = {
    **ORIGINAL_RELOCATIONS,
    107: (0xca8, 0xc90000011b, 0),
    108: (0xd04, 0xca0000011b, 0),
    109: (0xd30, 0x100, 0),
    110: (0xd3c, 0x100, 0),
    111: (0xd40, 0x100, 0),
}
PROBE_BEFORE = bytes.fromhex('0200009042000091e00315aae3031faa04108052')
PROBE_AFTER = bytes.fromhex('0300009063000091e00315aae2031faa04108452')


def _sections(data):
    """Read this fixed ELF64 section table without a runtime dependency."""
    headers = [struct.unpack_from('<IIQQQQIIQQ', data, 0x7468 + i * 64)
               for i in range(45)]
    strings = data[headers[43][4]:headers[43][4] + headers[43][5]]
    return {strings[h[0]:strings.index(b'\0', h[0])].decode(): h
            for h in headers}


def _validate_original(data):
    if (len(data) != 32680 or data[:16] != bytes.fromhex(
            '7f454c46020101000000000000000000')
            or struct.unpack_from('<HHI', data, 16) != (1, 183, 1)
            or struct.unpack_from('<Q', data, 40)[0] != 0x7468
            or struct.unpack_from('<HHH', data, 58) != (64, 45, 43)):
        raise RuntimeError('Unexpected goldfish sync ELF64/AArch64 layout.')
    if SIGNATURE_MAGIC in data:
        raise RuntimeError('Signed goldfish sync modules require a fresh build/signature.')
    sections = _sections(data)
    expected = {
        '.text': (1, 0x1000, 0x1230, 0),
        '.rela.text': (4, 0x3c08, 0xe10, 24),
        '.modinfo': (1, 0x2608, 0x17a, 0),
        '.symtab': (2, 0x53c0, 0x1440, 24),
        '__versions': (1, 0x2f00, 0xcc0, 0),
    }
    for name, layout in expected.items():
        section = sections.get(name)
        if section is None or (section[1], section[4], section[5], section[9]) != layout:
            raise RuntimeError('Unexpected goldfish sync section: ' + name)
    if (b'vermagic=' + VERMAGIC.encode() + b'\0' not in data[0x2608:0x2782]
            or b'scmversion=g' + SOURCE_REVISION[:12].encode() + b'\0'
            not in data[0x2608:0x2782]):
        raise RuntimeError('Unexpected goldfish sync kernel/source version.')
    if (zlib.crc32(data[0x1c74:0x1d5c]) != 0x1d0505df
            or zlib.crc32(data[0x1b2c:0x1c70]) != 0x4fdf45ba
            or data[0x1c70:0x1c74] != bytes.fromhex('f6050ff9')
            or data[0x1d5c:0x1d60] != bytes.fromhex('cd0814e0')
            or data[0x194c:0x1960] != PROBE_BEFORE):
        raise RuntimeError('Unexpected goldfish sync ISR/KCFI/probe instructions.')
    # The worker, including its mutex, 32-entry stack copy and fence execution,
    # is retained byte for byte. Verify the three local call targets as symbols.
    symtab, strtab = sections['.symtab'], sections['.strtab']
    strings = data[strtab[4]:strtab[4] + strtab[5]]
    symbols = {}
    for offset in range(symtab[4], symtab[4] + symtab[5], 24):
        name, _, _, index, value, size = struct.unpack_from('<IBBHQQ', data, offset)
        symbols[strings[name:strings.index(b'\0', name)].decode()] = (index, value, size)
    for name, expected_symbol in {
            'goldfish_sync_interrupt': (12, 0xc74, 0xe8),
            'goldfish_sync_work_item_fn': (12, 0xb2c, 0x144),
            'readl': (12, 0x119c, 0x80)}.items():
        if symbols.get(name) != expected_symbol:
            raise RuntimeError('Unexpected goldfish sync local symbol: ' + name)
    for index, entry in ORIGINAL_RELOCATIONS.items():
        if struct.unpack_from('<QQq', data, RELA_OFFSET + index * 24) != entry:
            raise RuntimeError('Unexpected goldfish sync ISR relocation.')
    for index, entry in {
            79: (0x94c, 0x7b00000113, 0xc74),
            80: (0x950, 0x7b00000115, 0xc74),
            81: (0x964, 0xc10000011b, 0)}.items():
        if struct.unpack_from('<QQq', data, RELA_OFFSET + index * 24) != entry:
            raise RuntimeError('Unexpected goldfish sync probe relocation.')


def patch(data):
    """Accept only the unsigned pinned module and its exact patched output."""
    if SIGNATURE_MAGIC in data:
        raise RuntimeError('Signed goldfish sync modules require a fresh build/signature.')
    checksum = hashlib.sha256(data).hexdigest()
    if checksum == AFTER:
        return data
    if checksum != BEFORE:
        raise RuntimeError('Unsupported goldfish sync SHA-256: ' + checksum)
    _validate_original(data)
    result = bytearray(data)
    result[TEXT_OFFSET + IRQ_START:TEXT_OFFSET + IRQ_END] = CODE
    # Existing ADRP/ADD relocations now address x3 (thread_fn), with a NULL x2
    # primary and IRQF_SHARED | IRQF_ONESHOT. Other arguments are unchanged.
    result[0x194c:0x1960] = PROBE_AFTER
    for index, entry in REPLACEMENT_RELOCATIONS.items():
        struct.pack_into('<QQq', result, RELA_OFFSET + index * 24, *entry)
    if hashlib.sha256(result).hexdigest() != AFTER:
        raise RuntimeError('Goldfish sync patch checksum mismatch.')
    return bytes(result)


MANIFEST = {'revision': 1, 'native': NATIVE, 'native_sha256': AFTER,
            'source_revision': SOURCE_REVISION, 'kernel_vermagic': VERMAGIC,
            'max_queued_commands': 32, 'irq_threaded': True,
            'irq_oneshot': True, 'worker_unchanged': True}


def build_vendor(source, destination, work):
    """Retain vendor inode metadata and replace only the pinned sync module."""
    from build_image import erofs
    from erofs_image import build
    path = NATIVE.removeprefix('/vendor/')
    build(destination, [('', source)], work,
          {path: (patch(erofs(source, '/' + path)),
                  0o644, 'u:object_r:vendor_file:s0')},
          preserve_replacement_metadata=True)
    if hashlib.sha256(erofs(destination, '/' + path)).hexdigest() != AFTER:
        raise RuntimeError('Baked goldfish sync verification failed.')
    return dict(MANIFEST)
