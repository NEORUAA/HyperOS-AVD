"""Verify the pinned ELF transformation and execute its IRQ instructions."""
from collections import deque
import hashlib
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'scripts'))
import patch_goldfish_sync as patcher

SOURCE = (REPO / 'work/os4-r3-build/work/vendor-alpha-tree/'
          'extract-system-71c777c3f8ac437b/lib/modules/goldfish_sync.ko')
COMPILERS = sorted((Path.home() / 'Library/Android/sdk/ndk').glob(
    '*/toolchains/llvm/prebuilt/darwin-*/bin/aarch64-linux-android36-clang'))


def sections(data):
    """Independent ELF section reader, also used for assembler output."""
    offset = struct.unpack_from('<Q', data, 40)[0]
    size, count, names_index = struct.unpack_from('<HHH', data, 58)
    headers = [struct.unpack_from('<IIQQQQIIQQ', data, offset + i * size)
               for i in range(count)]
    names = headers[names_index]
    strings = data[names[4]:names[4] + names[5]]
    return {strings[h[0]:strings.index(b'\0', h[0])].decode(): h
            for h in headers}


def relocations(data):
    section = sections(data)['.rela.text']
    return [struct.unpack_from('<QQq', data, p)
            for p in range(section[4], section[4] + section[5], section[9])]


def signed(value, bits):
    return value - (1 << bits) if value & (1 << (bits - 1)) else value


class IRQMachine:
    """Small ARM64 executor for the actual replacement instruction stream.

    Kernel helpers are modeled at the ELF relocation/local-call targets.
    Worker semantics follow the unchanged module: copy at most 32 entries,
    reset the count under its spinlock, then execute each copied command.
    This is machine-code/queue proof, not guest or host transport acceptance.
    """
    STATE = 0x100000
    STACK = 0x200000
    FOPS = 0x400230
    MMIO = 0x500050

    def __init__(self, module, commands=(), prefilled=(), ready=True,
                 during_worker=None, after_empty=None):
        self.code = module[0x1000:0x2230]
        self.relocs = {r[0]: r[1:] for r in relocations(module)}
        self.heap = bytearray(0x408)
        self.stack = {}
        self.pending = deque(commands)
        self.consumed = []
        self.processed = []
        self.batch_sizes = []
        self.max_count = 0
        self.reads = 0
        self.held = False
        self.during_worker = during_worker
        self.after_empty = after_empty
        self.store(self.STATE + 0x10, self.FOPS if ready else 0, 8)
        self.store(self.STATE + 0x50, self.MMIO, 8)
        for i, cmd in enumerate(prefilled):
            self.heap[0x98 + i * 24:0x98 + (i + 1) * 24] = struct.pack('<QQII', *cmd)
        self.store(self.STATE + 0x398, len(prefilled), 4)

    def load(self, address, width):
        if self.STATE <= address < self.STATE + len(self.heap):
            offset = address - self.STATE
            assert offset + width <= len(self.heap), 'Heap read exceeds state'
            return int.from_bytes(self.heap[offset:offset + width], 'little')
        return int.from_bytes(bytes(self.stack.get(address + i, 0)
                                    for i in range(width)), 'little')

    def store(self, address, value, width):
        data = (value & ((1 << (width * 8)) - 1)).to_bytes(width, 'little')
        if self.STATE <= address < self.STATE + len(self.heap):
            offset = address - self.STATE
            assert offset + width <= len(self.heap), 'Heap write exceeds state'
            self.heap[offset:offset + width] = data
            if offset == 0x398:
                assert value <= 32, 'Queue count exceeds its 32-entry capacity'
                self.max_count = max(self.max_count, value)
        else:
            for i, byte in enumerate(data):
                self.stack[address + i] = byte

    def helper(self, pc, target):
        info, _ = self.relocs.get(pc, (0, 0))
        symbol = info >> 32
        if symbol == 201:  # _raw_spin_lock, original ELF symbol index
            assert self.reg[0] == self.STATE + 0x39c
            assert not self.held
            self.held = True
        elif symbol == 202:  # _raw_spin_unlock
            assert self.reg[0] == self.STATE + 0x39c
            assert self.held
            self.held = False
        elif target == 0x119c:  # Original local readl helper
            assert self.held and self.reg[0] == self.MMIO
            assert self.load(self.STATE + 0x398, 4) < 32, 'Consumed MMIO with a full queue'
            self.reads += 1
            if self.pending:
                cmd = self.pending.popleft()
                self.consumed.append(cmd)
                self.heap[0x3a0:0x3b8] = struct.pack('<QQII', *cmd)
            else:
                self.store(self.STATE + 0x3b0, 0, 4)
                if self.after_empty:
                    callback, self.after_empty = self.after_empty, None
                    callback(self)
        elif target == 0xb2c:  # Original local worker, left byte-identical
            assert not self.held, 'Worker called while holding the producer spinlock'
            assert self.reg[0] == self.STATE + 0x3d8
            count = self.load(self.STATE + 0x398, 4)
            assert count <= 32
            batch = [struct.unpack_from('<QQII', self.heap, 0x98 + i * 24)
                     for i in range(count)]
            self.store(self.STATE + 0x398, 0, 4)
            self.batch_sizes.append(count)
            if self.during_worker:
                callback, self.during_worker = self.during_worker, None
                callback(self)
            self.processed.extend(batch)
        else:
            raise AssertionError(f'Unexpected helper at {pc:x}, target {target:x}, symbol {symbol}')
        # AAPCS64 helpers may destroy caller-saved registers. The loop must
        # preserve its state, lock address and continuation flag independently.
        for i in range(19):
            self.reg[i] = 0xbad00000 + i

    def run(self):
        self.reg = [0xabc00000 + i for i in range(31)]
        self.reg[0], self.reg[1] = 21, self.STATE
        saved = self.reg[:]
        self.sp = self.STACK
        self.pc = 0xc74
        self.eq = self.hs = False

        def get(index, stack=False):
            return self.sp if index == 31 and stack else (0 if index == 31 else self.reg[index])

        def put(index, value, width=64, stack=False):
            value &= (1 << width) - 1
            if index == 31 and stack:
                self.sp = value
            elif index != 31:
                self.reg[index] = value

        for _ in range(2_000_000):
            pc = self.pc
            word = struct.unpack_from('<I', self.code, pc)[0]
            self.pc += 4
            rd, rn, rm = word & 31, (word >> 5) & 31, (word >> 16) & 31
            width = 64 if word >> 31 else 32
            if word in (0xd503233f, 0xd50323bf, 0xd503201f):
                continue  # PAC instructions remain at their original positions.
            if word == 0xd65f03c0:
                assert not self.held and self.sp == self.STACK
                for index in (19, 20, 21, 29, 30):
                    assert self.reg[index] == saved[index], f'Callee-saved x{index} changed'
                return self.reg[0]
            if word & 0x3a000000 == 0x28000000:  # Load/store register pair
                unit = 8 if word >> 31 else 4
                delta = signed((word >> 15) & 127, 7) * unit
                mode = (word >> 23) & 3
                base = get(rn, stack=True)
                address = base + delta if mode in (2, 3) else base
                if mode == 3:
                    put(rn, address, stack=True)
                rt2 = (word >> 10) & 31
                if word & (1 << 22):
                    put(rd, self.load(address, unit), unit * 8)
                    put(rt2, self.load(address + unit, unit), unit * 8)
                else:
                    if rn == 12:
                        assert self.STATE + 0x98 <= address
                        assert address + 2 * unit <= self.STATE + 0x398, 'Queue write exceeds entry 31'
                    self.store(address, get(rd), unit)
                    self.store(address + unit, get(rt2), unit)
                if mode == 1:
                    put(rn, base + delta, stack=True)
            elif word & 0x3b000000 == 0x39000000:  # Unsigned immediate LDR/STR
                unit = 1 << (word >> 30)
                address = get(rn, stack=True) + ((word >> 10) & 4095) * unit
                if word & (1 << 22):
                    put(rd, self.load(address, unit), unit * 8)
                else:
                    self.store(address, get(rd), unit)
            elif word & 0x9f000000 == 0x90000000:  # ADRP, relocated fops page
                assert self.relocs[pc] == (0x7c00000113, 0x230)
                put(rd, self.FOPS & ~4095)
            elif word & 0x7f000000 == 0x11000000:  # ADD immediate, including MOV SP
                value = get(rn, stack=True) + (((word >> 10) & 4095) << (12 if word & (1 << 22) else 0))
                if pc in self.relocs:
                    assert self.relocs[pc] == (0x7c00000115, 0x230)
                    value += self.FOPS & 4095
                put(rd, value, width, stack=True)
            elif word & 0x7f000000 == 0x71000000:  # CMP unsigned immediate
                value = get(rn) & ((1 << width) - 1)
                immediate = (word >> 10) & 4095
                self.eq, self.hs = value == immediate, value >= immediate
            elif word & 0xff20001f == 0xeb00001f:  # CMP X register
                self.eq, self.hs = get(rn) == get(rm), get(rn) >= get(rm)
            elif word & 0xffe0ffe0 in (0xaa0003e0, 0x2a0003e0):  # MOV register
                put(rd, get(rm), width)
            elif word & 0x7f800000 == 0x52800000:  # MOVZ
                put(rd, ((word >> 5) & 65535) << (((word >> 21) & 3) * 16), width)
            elif word & 0xffe08000 == 0x9ba00000:  # UMADDL
                put(rd, (get(rn) & 0xffffffff) * (get(rm) & 0xffffffff)
                    + get((word >> 10) & 31))
            elif word & 0xfc000000 == 0x94000000:  # BL
                self.helper(pc, pc + signed(word & 0x3ffffff, 26) * 4)
            elif word & 0xfc000000 == 0x14000000:  # B
                self.pc = pc + signed(word & 0x3ffffff, 26) * 4
            elif word & 0xff000010 == 0x54000000:  # B.cond
                condition = {0: self.eq, 1: not self.eq, 2: self.hs, 3: not self.hs}[word & 15]
                if condition:
                    self.pc = pc + signed((word >> 5) & 0x7ffff, 19) * 4
            elif word & 0x7e000000 == 0x34000000:  # CBZ/CBNZ
                nonzero = bool(get(rd) & ((1 << width) - 1))
                if nonzero == bool(word & (1 << 24)):
                    self.pc = pc + signed((word >> 5) & 0x7ffff, 19) * 4
            else:
                raise AssertionError(f'Unmodeled instruction {word:08x} at {pc:x}')
        raise AssertionError('IRQ handler failed to reach a return')


def commands(count, start=0):
    # Exercise every original host command kind with distinct 64-bit arguments.
    return [(0x100000000 + i, 0x200000000 + i, i % 4 + 1, i * 17)
            for i in range(start, start + count)]


class GoldfishSyncGuardTest(unittest.TestCase):
    def test_unknown_module_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'Unsupported goldfish sync SHA-256'):
            patcher.patch(b'unknown kernel module')

    @unittest.skipUnless(SOURCE.is_file(), 'Pinned vendor module is not bundled')
    def test_pinned_hash_idempotence_and_only_allowed_changes(self):
        original = SOURCE.read_bytes()
        result = patcher.patch(original)
        self.assertEqual(hashlib.sha256(original).hexdigest(), patcher.BEFORE)
        self.assertEqual(hashlib.sha256(result).hexdigest(), patcher.AFTER)
        self.assertEqual(patcher.patch(result), result)
        self.assertEqual(len(result), len(original))
        allowed = set(range(0x1c74, 0x1d5c)) | set(range(0x194c, 0x1960))
        for index in range(107, 112):
            allowed.update(range(0x3c08 + index * 24, 0x3c08 + (index + 1) * 24))
        changed = {i for i, (a, b) in enumerate(zip(original, result)) if a != b}
        self.assertTrue(changed <= allowed)
        # Symbols/versions, unwind/PAC metadata, data/rodata and the original
        # worker must not change. Also retain both adjacent KCFI type words.
        for name, section in sections(original).items():
            if name not in ('.text', '.rela.text') and section[1] != 8:
                start, size = section[4:6]
                self.assertEqual(result[start:start + size], original[start:start + size], name)
        self.assertEqual(result[0x1b2c:0x1c74], original[0x1b2c:0x1c74])
        self.assertEqual(result[0x1d48:0x1d60], original[0x1d48:0x1d60])
        self.assertEqual(result[0x1c74:0x1c88], original[0x1c74:0x1c88])

    @unittest.skipUnless(SOURCE.is_file(), 'Pinned vendor module is not bundled')
    def test_tampered_and_signed_modules_are_rejected(self):
        data = bytearray(SOURCE.read_bytes())
        data[0x1c74] ^= 1
        with self.assertRaisesRegex(RuntimeError, 'Unsupported goldfish sync SHA-256'):
            patcher.patch(data)
        with self.assertRaisesRegex(RuntimeError, 'Signed goldfish sync modules'):
            patcher.patch(SOURCE.read_bytes() + patcher.SIGNATURE_MAGIC)

    @unittest.skipUnless(SOURCE.is_file(), 'Pinned vendor module is not bundled')
    def test_instructions_and_relocations_match_threaded_irq_contract(self):
        original = SOURCE.read_bytes()
        result = patcher.patch(original)
        old, new = relocations(original), relocations(result)
        self.assertEqual(new[:107], old[:107])
        self.assertEqual(new[112:], old[112:])
        self.assertEqual(new[107:112], [
            (0xca8, 0xc90000011b, 0), (0xd04, 0xca0000011b, 0),
            (0xd30, 256, 0), (0xd3c, 256, 0), (0xd40, 256, 0)])
        # Decode registration argument registers, retaining the two ADRP/ADD
        # relocations to the existing handler and the imported registration.
        probe = struct.unpack_from('<IIIII', result, 0x194c)
        self.assertEqual(probe[0] & 31, 3)
        self.assertEqual((probe[1] & 31, (probe[1] >> 5) & 31), (3, 3))
        self.assertEqual(probe[3], 0xaa1f03e2)  # x2 = NULL primary handler
        self.assertEqual((probe[4] & 31, (probe[4] >> 5) & 65535), (4, 0x2080))
        for pc, target in ((0xcc0, 0x119c), (0xd0c, 0xb2c)):
            word = struct.unpack_from('<I', result, 0x1000 + pc)[0]
            self.assertEqual(word & 0xfc000000, 0x94000000)
            self.assertEqual(pc + signed(word & 0x3ffffff, 26) * 4, target)
        words = struct.unpack_from('<58I', result, 0x1c74)
        self.assertFalse(any(w & 0xffe0001f == 0xd4200000 for w in words))
        self.assertEqual(result[0x1d1c:0x1d44], struct.pack('<I', 0xd503201f) * 10)

    @unittest.skipUnless(COMPILERS, 'Android AArch64 assembler is not installed')
    def test_embedded_assembly_reproduces_handler_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            source, output = Path(temp) / 'handler.S', Path(temp) / 'handler.o'
            source.write_text(patcher.ASSEMBLY)
            subprocess.run([str(COMPILERS[-1]), '-c', str(source), '-o', str(output)],
                           check=True, capture_output=True)
            obj = output.read_bytes()
            section = sections(obj)['.text']
            self.assertEqual(obj[section[4] + 0xc74:section[4] + 0xd5c], patcher.CODE)

    @unittest.skipUnless(SOURCE.is_file(), 'Pinned vendor module is not bundled')
    def test_actual_handler_instructions_preserve_every_burst_command(self):
        result = patcher.patch(SOURCE.read_bytes())
        for count in (0, 1, 31, 32, 33, 63, 64, 65, 4097):
            with self.subTest(count=count):
                batch = commands(count)
                machine = IRQMachine(result, batch)
                self.assertEqual(machine.run(), 1)
                self.assertEqual(machine.consumed, batch)
                self.assertEqual(machine.processed, batch)
                self.assertEqual(machine.reads, count + 1)
                self.assertEqual(machine.max_count, min(count, 32))
                self.assertTrue(all(size <= 32 for size in machine.batch_sizes))
                self.assertEqual(machine.load(machine.STATE + 0x398, 4), 0)

    @unittest.skipUnless(SOURCE.is_file(), 'Pinned vendor module is not bundled')
    def test_executor_detects_a_removed_capacity_check(self):
        result = bytearray(patcher.patch(SOURCE.read_bytes()))
        struct.pack_into('<I', result, 0x1cb8, 0xd503201f)  # Remove B.HS process.
        with self.assertRaisesRegex(AssertionError, 'Consumed MMIO with a full queue'):
            IRQMachine(result, commands(33)).run()

    @unittest.skipUnless(SOURCE.is_file(), 'Pinned vendor module is not bundled')
    def test_full_queue_is_processed_before_consuming_more_mmio(self):
        result = patcher.patch(SOURCE.read_bytes())
        initial, incoming = commands(32), commands(67, 32)
        machine = IRQMachine(result, incoming, prefilled=initial)
        self.assertEqual(machine.run(), 1)
        self.assertEqual(machine.processed, initial + incoming)
        self.assertEqual(machine.batch_sizes[0], 32)

    @unittest.skipUnless(SOURCE.is_file(), 'Pinned vendor module is not bundled')
    def test_commands_arriving_during_worker_and_after_empty_are_retained(self):
        result = patcher.patch(SOURCE.read_bytes())
        first, during, later = commands(33), commands(61, 33), commands(35, 94)
        machine = IRQMachine(result, first,
                             during_worker=lambda m: m.pending.extend(during),
                             after_empty=lambda m: m.pending.extend(later))
        self.assertEqual(machine.run(), 1)
        self.assertEqual(machine.processed, first + during)
        # A new level interrupt remains pending after the empty MMIO read.
        # Returning/unmasking permits the next IRQ thread to consume it.
        self.assertEqual(list(machine.pending), later)
        self.assertEqual(machine.run(), 1)
        self.assertEqual(machine.processed, first + during + later)

    @unittest.skipUnless(SOURCE.is_file(), 'Pinned vendor module is not bundled')
    def test_not_ready_guard_returns_none_without_consuming_commands(self):
        machine = IRQMachine(patcher.patch(SOURCE.read_bytes()), commands(33), ready=False)
        self.assertEqual(machine.run(), 0)
        self.assertEqual(machine.reads, 0)
        self.assertEqual(machine.processed, [])


if __name__ == '__main__':
    unittest.main()
