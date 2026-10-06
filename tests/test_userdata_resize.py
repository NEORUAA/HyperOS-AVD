"""Verify actual offline ext4 growth and recoverable raw/QCOW2 activation."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import common
import userdata_resize as storage

MIB = 1024**2
SDK = Path(os.environ.get('ANDROID_SDK_ROOT', Path.home() / 'Library/Android/sdk'))


class GeometryTests(unittest.TestCase):
    def test_64_bit_block_count_and_filesystem_identity(self):
        data = bytearray(4096)
        for offset, value in ((0x04, 7), (0x18, 2), (0x60, 0x84), (0x150, 1)):
            struct.pack_into('<I', data, 1024 + offset, value)
        struct.pack_into('<H', data, 1024 + 0x38, 0xEF53)
        data[1024 + 0x68:1024 + 0x78] = bytes(range(16))
        geometry = storage.ext4_geometry(data)
        self.assertEqual(geometry['bytes'], ((1 << 32) + 7) * 4096)
        self.assertEqual(geometry['incompatible'], 0x80)
        self.assertEqual(geometry['uuid'], bytes(range(16)).hex())

    def test_unknown_encrypted_partition_layout_is_refused(self):
        for header in (b'', b'\0' * 4096):
            with self.subTest(length=len(header)), self.assertRaisesRegex(RuntimeError, 'directly readable ext4'):
                storage.ext4_geometry(header)

    def test_open_userdata_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            avd = Path(temporary)
            (avd / storage.DATA).touch()
            with patch.object(storage.shutil, 'which', return_value='/usr/sbin/lsof'), \
                    patch.object(storage.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '123\n', '')):
                with self.assertRaisesRegex(RuntimeError, 'Close this AVD'):
                    storage.assert_offline(avd)


class OfflineFilesystemTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qemu = SDK / 'emulator/qemu-img'
        if not cls.qemu.is_file():
            raise unittest.SkipTest('An Android SDK qemu-img is required for the genuine offline test.')
        try:
            cls.mkfs = common.tool('mke2fs', 'e2fsprogs')
            cls.debugfs = common.tool('debugfs', 'e2fsprogs')
            common.tool('e2fsck', 'e2fsprogs')
            common.tool('resize2fs', 'e2fsprogs')
        except RuntimeError as error:
            raise unittest.SkipTest(str(error))

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.avd = (Path(self.temporary.name) / 'Offline.avd').resolve()
        self.avd.mkdir()
        self.base = self.avd / storage.DATA
        with self.base.open('wb') as stream:
            stream.truncate(64 * MIB)
        self.run_tool([self.mkfs, '-q', '-F', '-t', 'ext4', '-b', '4096',
                       '-O', 'encrypt,quota,project,casefold', '-E', 'encoding=utf8', self.base])
        self.probe = self.avd / 'probe.bin'
        self.probe.write_bytes(bytes(range(256)) * 400)
        self.probe_hash = storage.digest(self.probe)
        self.run_tool([self.debugfs, '-w', '-R', f'write {self.probe} /preserved.bin', self.base])
        (self.avd / 'qemu-version.txt').write_text('2')
        with (self.avd / 'encryptionkey.img').open('wb') as stream:
            stream.write(b'original FBE metadata/key image')
            stream.truncate(4 * MIB)
        self.run_tool([self.qemu, 'create', '-q', '-f', 'qcow2', '-F', 'raw', '-b',
                       'encryptionkey.img', self.avd / 'encryptionkey.img.qcow2'])

    def run_tool(self, arguments):
        return subprocess.run(list(map(str, arguments)), check=True, capture_output=True, cwd=self.avd)

    def geometry(self, path=None):
        with (path or self.base).open('rb') as stream:
            return storage.ext4_geometry(stream.read(4096))

    def hashes(self):
        return {name: storage.digest(self.avd / name) for name in (*storage.LAYER_NAMES, 'qemu-version.txt')
                if (self.avd / name).exists()}

    def probe_is_retained(self):
        output = self.avd / 'extracted.bin'
        self.run_tool([self.debugfs, '-R', f'dump /preserved.bin {output}', self.base])
        self.assertEqual(storage.digest(output), self.probe_hash)

    def overlay(self):
        effective = self.avd / 'effective.img'
        shutil.copy2(self.base, effective)
        self.probe.write_bytes(b'overlay-only contents' * 9000)
        self.probe_hash = storage.digest(self.probe)
        self.run_tool([self.debugfs, '-w', '-R', 'rm /preserved.bin', effective])
        self.run_tool([self.debugfs, '-w', '-R', f'write {self.probe} /preserved.bin', effective])
        self.run_tool([self.qemu, 'resize', effective, 256 * MIB])
        self.run_tool([self.qemu, 'convert', '-O', 'qcow2', '-B', self.base.name, effective,
                       self.avd / (storage.DATA + '.qcow2')])
        # Reproduce the old bug: both virtual layers are already at the target,
        # while the effective superblock still describes a 64 MiB filesystem.
        self.run_tool([self.qemu, 'resize', self.base, 256 * MIB])
        effective.unlink()

    def test_raw_filesystem_growth_preserves_probe_uuid_and_key_chain(self):
        before = self.geometry()
        keys = self.hashes()
        result = storage.resize_userdata(SDK, self.avd, 256 * MIB)
        self.assertTrue(result['changed'])
        self.assertEqual(self.geometry()['bytes'], 256 * MIB)
        self.assertEqual(self.geometry()['uuid'], before['uuid'])
        self.probe_is_retained()
        for name in ('encryptionkey.img', 'encryptionkey.img.qcow2', 'qemu-version.txt'):
            self.assertEqual(storage.digest(self.avd / name), keys[name])
        with patch.object(storage, 'check_dependencies', side_effect=RuntimeError('Missing resize2fs')) as dependencies:
            self.assertFalse(storage.resize_userdata(SDK, self.avd, 256 * MIB)['changed'])
        dependencies.assert_not_called()

    def test_existing_large_qcow2_disk_gets_a_real_filesystem_repair(self):
        self.overlay()
        before = self.hashes()
        result = storage.resize_userdata(SDK, self.avd, 256 * MIB)
        self.assertEqual(self.geometry()['bytes'], 256 * MIB)
        self.probe_is_retained()
        info = storage._info(self.qemu, self.avd / (storage.DATA + '.qcow2'))
        self.assertEqual(info['backing-filename'], storage.DATA)
        self.assertEqual(info['virtual-size'], 256 * MIB)
        backup = Path(result['backup']) / 'original'
        for name, checksum in before.items():
            if name != 'qemu-version.txt':
                self.assertEqual(storage.digest(backup / name), checksum)

    def test_shrink_and_foreign_backing_are_refused_without_data_writes(self):
        self.overlay()
        before = self.hashes()
        with self.assertRaisesRegex(RuntimeError, 'reduced'):
            storage.resize_userdata(SDK, self.avd, 128 * MIB)
        self.assertEqual(self.hashes(), before)
        self.run_tool([self.qemu, 'rebase', '-u', '-b', str(self.base), self.avd / (storage.DATA + '.qcow2')])
        before = self.hashes()
        with self.assertRaisesRegex(RuntimeError, 'backing chain'):
            storage.resize_userdata(SDK, self.avd, 256 * MIB)
        self.assertEqual(self.hashes(), before)

    def test_failed_resize_does_not_activate_or_modify_the_original_chain(self):
        before = self.hashes()
        run = storage._run
        def fail(arguments, **kwargs):
            if Path(arguments[0]).name == 'resize2fs':
                raise RuntimeError('injected filesystem failure')
            return run(arguments, **kwargs)
        with patch.object(storage, '_run', side_effect=fail), self.assertRaisesRegex(RuntimeError, 'injected'):
            storage.resize_userdata(SDK, self.avd, 256 * MIB)
        self.assertEqual(self.hashes(), before)
        self.assertFalse((self.avd / storage.PENDING).exists())

    def test_partial_two_file_activation_rolls_back_the_exact_original_chain(self):
        self.overlay()
        before = self.hashes()
        def interrupt(avd, pending, names):
            for name in names:
                (avd / name).replace(pending / 'original' / name)
            (pending / 'staged' / storage.DATA).replace(avd / storage.DATA)
            raise RuntimeError('injected interrupted activation')
        with patch.object(storage, '_activate', side_effect=interrupt), self.assertRaisesRegex(RuntimeError, 'interrupted'):
            storage.resize_userdata(SDK, self.avd, 256 * MIB)
        self.assertEqual(self.hashes(), before)
        self.assertFalse((self.avd / storage.PENDING).exists())

    def test_pending_activation_is_recovered_before_a_future_resize(self):
        self.overlay()
        before = self.hashes()
        pending = self.avd / storage.PENDING
        (pending / 'original').mkdir(parents=True)
        (pending / 'staged').mkdir()
        storage._json(pending / 'transaction.json', {
            'schema': storage.SCHEMA, 'avd': str(self.avd),
            'original_sha256': {k:v for k,v in before.items() if k in storage.LAYER_NAMES},
            'staged_sha256': {storage.DATA: hashlib.sha256(b'incomplete new activation').hexdigest()}})
        self.base.replace(pending / 'original' / self.base.name)
        self.base.write_bytes(b'incomplete new activation')
        with self.assertRaisesRegex(RuntimeError, 'reduced'):
            storage.resize_userdata(SDK, self.avd, 128 * MIB)
        self.assertEqual(self.hashes(), before)
        self.assertFalse(pending.exists())

    def test_completed_activation_failure_restores_the_original_chain(self):
        self.overlay()
        before = self.hashes()
        activate = storage._activate
        def interrupt(avd, pending, names):
            activate(avd, pending, names)
            raise RuntimeError('injected post-activation failure')
        with patch.object(storage, '_activate', side_effect=interrupt), self.assertRaisesRegex(RuntimeError, 'post-activation'):
            storage.resize_userdata(SDK, self.avd, 256 * MIB)
        self.assertEqual(self.hashes(), before)
        self.assertFalse((self.avd / storage.PENDING).exists())

    def test_activated_pending_recovery_never_erases_later_userdata_writes(self):
        self.overlay()
        before = self.hashes()
        activate = storage._activate
        def later_write(avd, pending, names):
            activate(avd, pending, names)
            self.run_tool([self.debugfs, '-w', '-R', f'write {self.probe} /later.bin', self.base])
            raise RuntimeError('injected later guest write')
        with patch.object(storage, '_activate', side_effect=later_write), self.assertRaisesRegex(RuntimeError, 'needs inspection'):
            storage.resize_userdata(SDK, self.avd, 256 * MIB)
        pending = self.avd / storage.PENDING
        self.assertTrue(pending.exists())
        self.assertEqual(self.geometry()['bytes'], 256 * MIB)
        output = self.avd / 'later-extracted.bin'
        self.run_tool([self.debugfs, '-R', f'dump /later.bin {output}', self.base])
        self.assertEqual(storage.digest(output), self.probe_hash)
        current = self.hashes()
        with self.assertRaisesRegex(RuntimeError, 'needs inspection'):
            storage.resize_userdata(SDK, self.avd, 256 * MIB)
        self.assertEqual(self.hashes(), current)
        for name in storage.LAYER_NAMES:
            self.assertEqual(storage.digest(pending / 'original' / name), before[name])

    def test_recovery_sync_failure_retains_receipt_until_all_originals_are_durable(self):
        self.overlay()
        before = self.hashes()
        pending = self.avd / storage.PENDING
        def interrupt(avd, pending, names):
            for name in names:
                (avd / name).replace(pending / 'original' / name)
            (pending / 'staged' / storage.DATA).replace(avd / storage.DATA)
            raise RuntimeError('injected activation failure')
        sync = storage._sync_directory
        def fail_restored_entry(path):
            if path == self.avd and self.base.exists() and storage.digest(self.base) == before[storage.DATA] and pending.exists():
                raise OSError('injected recovery sync failure')
            return sync(path)
        with patch.object(storage, '_activate', side_effect=interrupt), \
                patch.object(storage, '_sync_directory', side_effect=fail_restored_entry), \
                self.assertRaisesRegex(RuntimeError, 'needs inspection'):
            storage.resize_userdata(SDK, self.avd, 256 * MIB)
        self.assertTrue((pending / 'transaction.json').exists())
        self.assertTrue((pending / 'original' / (storage.DATA + '.qcow2')).exists())
        storage._recover(self.avd)
        self.assertEqual(self.hashes(), before)
        self.assertFalse(pending.exists())

    def test_unknown_or_foreign_pending_children_refuse_without_data_changes(self):
        before = self.hashes()
        foreign = self.avd / 'foreign'
        foreign.mkdir()
        sentinel = foreign / 'sentinel'
        sentinel.write_bytes(b'foreign files must stay intact')
        receipt = {'schema': storage.SCHEMA, 'avd': str(self.avd), 'original_sha256': {
            name: checksum for name, checksum in before.items() if name in storage.LAYER_NAMES}}
        foreign_receipt = foreign / 'receipt.json'
        foreign_receipt.write_text(json.dumps(receipt))
        pending = self.avd / storage.PENDING
        for kind in ('missing-receipt', 'symlink-receipt', 'symlink-original', 'symlink-staged'):
            with self.subTest(kind=kind):
                (pending / 'original').mkdir(parents=True)
                (pending / 'staged').mkdir()
                if kind == 'symlink-receipt':
                    (pending / 'transaction.json').symlink_to(foreign_receipt)
                elif kind != 'missing-receipt':
                    storage._json(pending / 'transaction.json', receipt)
                if kind in ('symlink-original', 'symlink-staged'):
                    child = pending / kind.removeprefix('symlink-')
                    child.rmdir()
                    child.symlink_to(foreign, target_is_directory=True)
                with self.assertRaisesRegex(RuntimeError, 'needs inspection'):
                    storage._recover(self.avd)
                self.assertEqual(self.hashes(), before)
                self.assertEqual(sentinel.read_bytes(), b'foreign files must stay intact')
                self.assertTrue(pending.exists())
                shutil.rmtree(pending)

    def test_raw_recovery_retry_retains_receipt_until_restored_directory_is_synced(self):
        before = self.hashes()
        pending = self.avd / storage.PENDING
        def interrupt(avd, pending, names):
            (avd / storage.DATA).replace(pending / 'original' / storage.DATA)
            (pending / 'staged' / storage.DATA).replace(avd / storage.DATA)
            raise RuntimeError('injected raw activation failure')
        sync = storage._sync_directory
        def fail_restored_entry(path):
            if path == self.avd and self.base.exists() and storage.digest(self.base) == before[storage.DATA] and pending.exists():
                raise OSError('injected raw recovery sync failure')
            return sync(path)
        with patch.object(storage, '_activate', side_effect=interrupt), \
                patch.object(storage, '_sync_directory', side_effect=fail_restored_entry), \
                self.assertRaisesRegex(RuntimeError, 'needs inspection'):
            storage.resize_userdata(SDK, self.avd, 256 * MIB)
        self.assertFalse((pending / 'original' / storage.DATA).exists())
        with patch.object(storage, '_sync_directory', side_effect=fail_restored_entry), \
                self.assertRaisesRegex(RuntimeError, 'needs inspection'):
            storage._recover(self.avd)
        self.assertTrue((pending / 'transaction.json').exists())
        storage._recover(self.avd)
        self.assertEqual(self.hashes(), before)
        self.assertFalse(pending.exists())

    def test_unrecorded_saved_userdata_layer_is_never_restored(self):
        before = self.hashes()
        pending = self.avd / storage.PENDING
        (pending / 'original').mkdir(parents=True)
        (pending / 'staged').mkdir()
        storage._json(pending / 'transaction.json', {
            'schema': storage.SCHEMA, 'avd': str(self.avd), 'original_sha256': {
                name: checksum for name, checksum in before.items() if name in storage.LAYER_NAMES}})
        foreign = pending / 'original' / (storage.DATA + '.qcow2')
        foreign.write_bytes(b'unverified saved overlay')
        with self.assertRaisesRegex(RuntimeError, 'needs inspection'):
            storage._recover(self.avd)
        self.assertEqual(self.hashes(), before)
        self.assertEqual(foreign.read_bytes(), b'unverified saved overlay')
        self.assertFalse((self.avd / foreign.name).exists())

    def test_real_open_image_is_refused_before_conversion(self):
        before = self.hashes()
        with self.base.open('rb'), self.assertRaisesRegex(RuntimeError, 'Close this AVD'):
            storage.resize_userdata(SDK, self.avd, 256 * MIB)
        self.assertEqual(self.hashes(), before)
        self.assertFalse((self.avd / storage.PENDING).exists())

    def test_missing_tools_and_space_are_refused_before_original_changes(self):
        before = self.hashes()
        with patch.object(storage, 'check_dependencies', side_effect=RuntimeError('Missing resize2fs')), \
                self.assertRaisesRegex(RuntimeError, 'Missing resize2fs'):
            storage.resize_userdata(SDK, self.avd, 256 * MIB)
        self.assertEqual(self.hashes(), before)
        self.assertFalse((self.avd / storage.PENDING).exists())
        with patch.object(storage.shutil, 'disk_usage', return_value=shutil._ntuple_diskusage(1, 1, 0)), \
                self.assertRaisesRegex(RuntimeError, 'Insufficient free'):
            storage.resize_userdata(SDK, self.avd, 256 * MIB)
        self.assertEqual(self.hashes(), before)
        self.assertFalse((self.avd / storage.PENDING).exists())

    def test_unknown_filesystem_and_symlink_layers_are_never_reformatted(self):
        self.base.write_bytes(b'opaque whole-disk encrypted userdata')
        before = self.hashes()
        with self.assertRaisesRegex(RuntimeError, 'directly readable ext4'):
            storage.resize_userdata(SDK, self.avd, 256 * MIB)
        self.assertEqual(self.hashes(), before)
        foreign = self.avd / 'foreign-key.img'
        (self.avd / 'encryptionkey.img').replace(foreign)
        (self.avd / 'encryptionkey.img').symlink_to(foreign.name)
        with self.assertRaisesRegex(RuntimeError, 'nonregular userdata/key'):
            storage.resize_userdata(SDK, self.avd, 256 * MIB)
        self.assertEqual(storage.digest(foreign), before['encryptionkey.img'])

    def test_unactivated_staging_never_rolls_back_later_user_writes(self):
        pending = self.avd / storage.PENDING
        (pending / 'original').mkdir(parents=True)
        (pending / 'staged').mkdir()
        storage._json(pending / 'transaction.json', {
            'schema': storage.SCHEMA, 'avd': str(self.avd), 'original_sha256': {
                name:storage.digest(self.avd / name) for name in storage.LAYER_NAMES
                if (self.avd / name).exists()}})
        (pending / 'staged' / storage.DATA).write_bytes(b'failed scratch conversion')
        self.probe.write_bytes(b'later preserved user write')
        self.run_tool([self.debugfs, '-w', '-R', f'write {self.probe} /later.bin', self.base])
        later = self.hashes()
        storage._recover(self.avd)
        self.assertEqual(self.hashes(), later)
        self.assertFalse(pending.exists())


if __name__ == '__main__':
    unittest.main()
