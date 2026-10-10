"""Verify actual offline ext4 growth and recoverable raw/QCOW2 activation."""
import hashlib
import copy
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
        self.avd = (Path(self.temporary.name) / 'avd/Offline.avd').resolve()
        self.avd.mkdir(parents=True)
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

    def opaque_overlay(self, name=storage.DATA):
        """Model unreadable mapped-device metadata; do not claim crypto validation."""
        effective = self.avd / ('opaque-' + name)
        shutil.copyfile(self.avd / name, effective)
        with effective.open('r+b') as stream:
            stream.write(b'\xa5' * 4096)
        self.run_tool([self.qemu, 'convert', '-O', 'qcow2', '-B', name,
                       effective, self.avd / (name + '.qcow2')])
        effective.unlink()

    def install_key_template(self):
        workspace = self.avd.parent.parent
        template = workspace / 'images/encryptionkey.img'
        template.parent.mkdir(parents=True)
        shutil.copyfile(self.avd / 'encryptionkey.img', template)
        manifest = workspace / 'local/installed-release.json'
        manifest.parent.mkdir()
        manifest.write_text(json.dumps({'files': {'images/encryptionkey.img': {
            'sha256': storage.digest(template), 'size': template.stat().st_size}}}))
        return template

    def boot_key_layers(self, template):
        shutil.copyfile(template, self.avd / 'encryptionkey.img')
        self.run_tool([self.qemu, 'create', '-q', '-f', 'qcow2', '-F', 'raw', '-b',
                       'encryptionkey.img', self.avd / 'encryptionkey.img.qcow2'])

    def legacy_opaque(self):
        self.run_tool([self.qemu, 'resize', self.base, 256 * MIB])
        self.opaque_overlay()

    def offline_backup(self):
        folder = self.avd.parent / 'complete-offline-backup'
        data = folder / 'avd'
        data.mkdir(parents=True)
        for name in (*storage.LAYER_NAMES, 'qemu-version.txt'):
            shutil.copy2(self.avd / name, data / name)
        (folder / 'backup.json').write_text(json.dumps({
            'name': self.avd.name.removesuffix('.avd'),
            'files': {'avd/' + name: storage.digest(data / name) for name in storage.LAYER_NAMES}}))
        return folder

    def grow_opaque(self, folder):
        return storage.resize_userdata(SDK, self.avd, 512 * MIB,
                                       allow_guest=True, backup=folder)

    def test_backed_up_opaque_growth_preserves_every_old_sector_and_keys(self):
        self.legacy_opaque()
        before = self.hashes()
        folder = self.offline_backup()
        with self.assertRaises(storage.OfflineBackupRequiredError):
            self.grow_opaque(None)
        with patch.object(storage, 'check_dependencies', side_effect=AssertionError('No host fs tools')):
            result = self.grow_opaque(folder)
        self.assertTrue(result['changed'])
        self.assertTrue(result['guest_required'])
        self.assertIsNone(result['filesystem_bytes'])
        self.assertNotIn('prepared_filesystem_bytes', result)
        self.assertEqual(result['disk_capacity_bytes'], 512 * MIB)
        self.assertEqual(result['previous_disk_capacity_bytes'], 256 * MIB)
        self.assertEqual(result['backup'], str(folder))
        self.assertEqual(self.geometry()['bytes'], 64 * MIB)
        self.assertEqual(storage._prefix_digest(self.base, 256 * MIB), before[storage.DATA])
        self.probe_is_retained()
        layers = storage._layers(self.qemu, self.avd)
        self.assertEqual([layers[name]['virtual-size'] for name in storage.LAYER_NAMES[:2]], [512 * MIB] * 2)
        for name in ('encryptionkey.img', 'encryptionkey.img.qcow2', 'qemu-version.txt'):
            self.assertEqual(storage.digest(self.avd / name), before[name])
        self.run_tool([self.qemu, 'compare', folder / 'avd' / (storage.DATA + '.qcow2'),
                       self.avd / (storage.DATA + '.qcow2')])
        retained = Path(result['transaction_backup'])
        for name in storage.LAYER_NAMES:
            self.assertEqual(storage.digest(retained / 'original' / name), before[name])
        receipt = json.loads((retained / 'transaction.json').read_text())
        self.assertEqual(receipt['mode'], 'opaque-disk-capacity')
        self.assertNotIn('after', receipt)
        for name in (storage.PENDING, storage.VERIFIED, storage.GUEST_VERIFIED):
            self.assertFalse((self.avd / name).exists())
        commands = (retained / 'commands.log').read_text()
        self.assertNotIn('e2fsck', commands)
        self.assertNotIn('resize2fs', commands)
        self.assertNotIn('"convert", "-O", "raw", "' + str(self.avd / (storage.DATA + '.qcow2')), commands)
        self.assertEqual(result['userdata_capacity_token']['requested_bytes'], 512 * MIB)
        self.assertEqual(result['userdata_capacity_token']['base_filesystem']['bytes'], 64 * MIB)

    def test_opaque_growth_needs_explicit_opt_in_and_matching_complete_backup(self):
        self.legacy_opaque()
        before = self.hashes()
        folder = self.offline_backup()
        with self.assertRaises(storage.OpaqueFilesystemError):
            storage.resize_userdata(SDK, self.avd, 512 * MIB, backup=folder)
        with self.assertRaisesRegex(RuntimeError, 'reduced'):
            storage.resize_userdata(SDK, self.avd, 128 * MIB, allow_guest=True, backup=folder)
        receipt = folder / 'backup.json'
        saved = receipt.read_bytes()
        for kind in ('absent', 'wrong-name', 'missing-key', 'wrong-hash', 'changed-file', 'aliased-file', 'marker'):
            with self.subTest(kind=kind):
                if kind == 'absent':
                    candidate = None
                else:
                    candidate = folder
                    value = json.loads(saved)
                    if kind == 'wrong-name':
                        value['name'] = 'Different'
                    elif kind == 'missing-key':
                        value['files'].pop('avd/encryptionkey.img.qcow2')
                    elif kind == 'wrong-hash':
                        value['files']['avd/' + storage.DATA] = '0' * 64
                    elif kind == 'changed-file':
                        with (folder / 'avd/encryptionkey.img').open('r+b') as stream:
                            stream.write(b'changed')
                    elif kind == 'aliased-file':
                        os.link(folder / 'avd/encryptionkey.img', folder / 'alias')
                    elif kind == 'marker':
                        (folder / 'avd/qemu-version.txt').write_text('1')
                    receipt.write_text(json.dumps(value))
                with self.assertRaisesRegex(storage.OpaqueFilesystemError, 'matching complete offline'):
                    self.grow_opaque(candidate)
                self.assertEqual(self.hashes(), before)
                self.assertFalse((self.avd / storage.PENDING).exists())
                receipt.write_bytes(saved)
                (folder / 'alias').unlink(missing_ok=True)
                shutil.copy2(self.avd / 'encryptionkey.img', folder / 'avd/encryptionkey.img')
                shutil.copy2(self.avd / 'qemu-version.txt', folder / 'avd/qemu-version.txt')

    def test_opaque_growth_accepts_identical_overlay_with_sparsified_backing_allocation(self):
        self.legacy_opaque()
        # Force a physically allocated zero range in the raw backing.
        with self.base.open('r+b') as stream:
            stream.seek(100 * MIB)
            stream.write(b'\0' * MIB)
        top = self.avd / (storage.DATA + '.qcow2')
        # The converter emits explicit zero clusters. Replace only offsetless
        # zero entries with unallocated entries so those sectors inherit raw.
        # qemu-img check below verifies this genuine QCOW2 regression fixture.
        with top.open('r+b') as stream:
            header = stream.read(72)
            self.assertEqual(struct.unpack_from('>I', header, 4)[0], 3)
            cluster_size = 1 << struct.unpack_from('>I', header, 20)[0]
            l1_size = struct.unpack_from('>I', header, 36)[0]
            stream.seek(struct.unpack_from('>Q', header, 40)[0])
            l1 = stream.read(l1_size * 8)
            cleared = 0
            for index in range(l1_size):
                l2_offset = struct.unpack_from('>Q', l1, index * 8)[0] & 0x00fffffffffffe00
                if not l2_offset:
                    continue
                stream.seek(l2_offset)
                entries = bytearray(stream.read(cluster_size))
                for offset in range(0, len(entries), 8):
                    entry = struct.unpack_from('>Q', entries, offset)[0]
                    if entry & 1 and not entry & 0x00fffffffffffe00:
                        struct.pack_into('>Q', entries, offset, 0)
                        cleared += 1
                stream.seek(l2_offset)
                stream.write(entries)
        self.assertGreater(cleared, 0)
        self.run_tool([self.qemu, 'check', top])
        before = self.hashes()
        with tempfile.TemporaryDirectory(dir=self.avd) as temporary:
            sparse = Path(temporary)
            self.run_tool([self.qemu, 'convert', '-O', 'raw', self.base, sparse / storage.DATA])
            shutil.copy2(top, sparse / top.name)
            self.assertEqual(storage.digest(sparse / top.name), before[top.name])
            self.assertEqual(storage.digest(sparse / storage.DATA), before[storage.DATA])
            strict = subprocess.run([str(self.qemu), 'compare', '-s', str(top), str(sparse / top.name)],
                                    capture_output=True, text=True)
            self.assertEqual(strict.returncode, 1)
            self.assertIn('block status mismatch', strict.stdout + strict.stderr)
            self.run_tool([self.qemu, 'compare', top, sparse / top.name])
        folder = self.offline_backup()
        with patch.object(storage, 'check_dependencies', side_effect=AssertionError('No host fs tools')):
            result = self.grow_opaque(folder)
        self.assertTrue(result['guest_required'])
        self.assertNotIn('prepared_filesystem_bytes', result)
        self.assertEqual(result['disk_capacity_bytes'], 512 * MIB)
        self.assertEqual(storage._prefix_digest(self.base, 256 * MIB), before[storage.DATA])
        self.run_tool([self.qemu, 'compare', folder / 'avd' / top.name, top])
        for name in ('encryptionkey.img', 'encryptionkey.img.qcow2', 'qemu-version.txt'):
            self.assertEqual(storage.digest(self.avd / name), before[name])
        self.assertEqual(self.geometry()['bytes'], 64 * MIB)
        self.probe_is_retained()

    def test_opaque_growth_refuses_nonmatching_layer_capacity_or_alias_before_staging(self):
        self.legacy_opaque()
        folder = self.offline_backup()
        self.run_tool([self.qemu, 'resize', self.avd / (storage.DATA + '.qcow2'), 512 * MIB])
        before = self.hashes()
        with self.assertRaisesRegex(storage.OpaqueFilesystemError, 'guest-scoped'):
            storage.resize_userdata(SDK, self.avd, 1024 * MIB, allow_guest=True, backup=folder)
        with self.assertRaises(storage.OpaqueFilesystemError) as refused:
            storage.resize_userdata(SDK, self.avd, 1024 * MIB, allow_guest=True)
        self.assertNotIsInstance(refused.exception, storage.OfflineBackupRequiredError)
        self.assertEqual(self.hashes(), before)
        self.assertFalse((self.avd / storage.PENDING).exists())
        shutil.copy2(folder / 'avd' / (storage.DATA + '.qcow2'), self.avd / (storage.DATA + '.qcow2'))
        os.link(self.avd / 'encryptionkey.img', self.avd / 'key-alias')
        before = self.hashes()
        with self.assertRaisesRegex(storage.OpaqueFilesystemError, 'aliased'):
            self.grow_opaque(folder)
        self.assertEqual(self.hashes(), before)
        self.assertFalse((self.avd / storage.PENDING).exists())

    def test_opaque_growth_refuses_insufficient_storage_before_mutating_layers(self):
        self.legacy_opaque()
        folder, before = self.offline_backup(), self.hashes()
        with patch.object(storage.shutil, 'disk_usage', return_value=shutil._ntuple_diskusage(1, 1, 0)), \
                self.assertRaisesRegex(RuntimeError, 'Insufficient free host storage'):
            self.grow_opaque(folder)
        self.assertEqual(self.hashes(), before)
        self.assertFalse((self.avd / storage.PENDING).exists())

    def test_opaque_growth_rejects_nonzero_new_sectors_without_activation(self):
        self.legacy_opaque()
        folder, before = self.offline_backup(), self.hashes()
        run = storage._run
        def corrupt_tail(arguments, **kwargs):
            value = run(arguments, **kwargs)
            if arguments[1] == 'resize' and Path(arguments[2]).name == storage.DATA + '.qcow2':
                with (self.avd / storage.PENDING / 'staged' / storage.DATA).open('r+b') as stream:
                    stream.seek(300 * MIB)
                    stream.write(b'unexpected new sectors')
            return value
        with patch.object(storage, '_run', side_effect=corrupt_tail), \
                self.assertRaisesRegex(RuntimeError, 'qemu-img, exit 1'):
            self.grow_opaque(folder)
        self.assertEqual(self.hashes(), before)
        self.assertFalse((self.avd / storage.PENDING).exists())

    def test_opaque_growth_partial_and_completed_activation_failures_restore_exact_chain(self):
        self.legacy_opaque()
        folder, before = self.offline_backup(), self.hashes()
        activate = storage._activate
        for completed in (False, True):
            def interrupt(avd, pending, names):
                if completed:
                    activate(avd, pending, names)
                else:
                    for name in names:
                        (avd / name).replace(pending / 'original' / name)
                    (pending / 'staged' / storage.DATA).replace(avd / storage.DATA)
                raise RuntimeError('injected interrupted encrypted activation')
            with self.subTest(completed=completed), patch.object(storage, '_activate', side_effect=interrupt), \
                    self.assertRaisesRegex(RuntimeError, 'interrupted encrypted'):
                self.grow_opaque(folder)
            self.assertEqual(self.hashes(), before)
            self.assertFalse((self.avd / storage.PENDING).exists())

    def test_opaque_growth_durable_pending_recovers_on_next_attempt(self):
        self.legacy_opaque()
        folder, before = self.offline_backup(), self.hashes()
        activate, recover = storage._activate, storage._recover
        def interrupt(avd, pending, names):
            activate(avd, pending, names)
            raise RuntimeError('injected completed interrupted activation')
        def crash(avd):
            if (avd / storage.PENDING).exists():
                raise RuntimeError('simulated abrupt process exit')
            return recover(avd)
        with patch.object(storage, '_activate', side_effect=interrupt), \
                patch.object(storage, '_recover', side_effect=crash), \
                self.assertRaisesRegex(RuntimeError, 'abrupt process exit'):
            self.grow_opaque(folder)
        self.assertTrue((self.avd / storage.PENDING).is_dir())
        self.assertEqual(storage._info(self.qemu, self.base)['virtual-size'], 512 * MIB)
        # The public path recovers before rechecking requested size.
        result = storage.resize_userdata(SDK, self.avd, 256 * MIB, allow_guest=True)
        self.assertTrue(result['guest_required'])
        self.assertEqual(self.hashes(), before)
        self.assertFalse((self.avd / storage.PENDING).exists())

    def test_opaque_growth_recovery_preserves_later_overlay_writes(self):
        self.legacy_opaque()
        folder, before = self.offline_backup(), self.hashes()
        activate = storage._activate
        def later_write(avd, pending, names):
            activate(avd, pending, names)
            self.run_tool([self.qemu, 'amend', '-f', 'qcow2', '-o', 'lazy_refcounts=on',
                           avd / (storage.DATA + '.qcow2')])
            raise RuntimeError('injected later guest write')
        with patch.object(storage, '_activate', side_effect=later_write), \
                self.assertRaisesRegex(RuntimeError, 'needs inspection'):
            self.grow_opaque(folder)
        pending = self.avd / storage.PENDING
        current = self.hashes()
        self.assertTrue(pending.is_dir())
        self.assertNotEqual(current[storage.DATA + '.qcow2'], before[storage.DATA + '.qcow2'])
        with self.assertRaisesRegex(RuntimeError, 'needs inspection'):
            storage.resize_userdata(SDK, self.avd, 512 * MIB, allow_guest=True, backup=folder)
        self.assertEqual(self.hashes(), current)
        for name in storage.LAYER_NAMES:
            self.assertEqual(storage.digest(pending / 'original' / name), before[name])

    def test_opaque_disk_growth_capacity_remains_unproven_until_verified_guest_record(self):
        self.legacy_opaque()
        result = self.grow_opaque(self.offline_backup())
        before = self.hashes()
        with patch.object(storage, 'check_dependencies', side_effect=AssertionError('No host fs tools')):
            with self.assertRaisesRegex(storage.OpaqueFilesystemError, 'guest-scoped'):
                storage.resize_userdata(SDK, self.avd, 512 * MIB)
            deferred = storage.resize_userdata(SDK, self.avd, 512 * MIB, allow_guest=True)
            self.assertTrue(deferred['guest_required'])
            proof = storage.publish_guest_capacity(SDK, self.avd, 512 * MIB,
                                                   self.guest_record(result, changed=True))
            historical = storage.resize_userdata(SDK, self.avd, 512 * MIB)
        self.assertEqual(proof['capacity_proof'], 'verified-in-guest')
        self.assertEqual(historical['prepared_filesystem_bytes'], 512 * MIB)
        self.assertEqual(self.hashes(), before)
        self.assertEqual(self.geometry()['bytes'], 64 * MIB)

    def guest_record(self, result, *, changed=False):
        """Model the trusted guest helper's return, not a measured live guest."""
        wanted = result['userdata_capacity_token']['requested_bytes']
        return {'schema': storage.GUEST_RECORD_SCHEMA, 'requested_bytes': wanted,
                'name': self.avd.name.removesuffix('.avd'), 'serial': 'emulator-5584',
                'boot_id': '01234567-89ab-cdef-0123-456789abcdef', 'hardware': 'ranchu',
                'product_device': 'test_device', 'incremental': 'test_build', 'root_uid': 0,
                'selinux': 'Enforcing', 'mounted_type': 'ext4', 'mapped_device': '/dev/block/dm-0',
                'mapped_device_bytes': wanted, 'filesystem': {**self.geometry(), 'bytes': wanted},
                'verified': True, 'changed': changed, 'capacity_proof': 'verified-in-guest',
                'host_token': result['userdata_capacity_token'], 'probe_verified': True if changed else None,
                'backup': str(self.avd.parent / 'offline-backup') if changed else None}

    def test_explicit_guest_deferral_has_no_capacity_claim_or_data_mutation(self):
        self.legacy_opaque()
        before = self.hashes()
        with patch.object(storage, 'check_dependencies', side_effect=AssertionError('No host fs tools')):
            with self.assertRaisesRegex(storage.OpaqueFilesystemError, 'guest-scoped'):
                storage.resize_userdata(SDK, self.avd, 256 * MIB)
            result = storage.resize_userdata(SDK, self.avd, 256 * MIB, allow_guest=True)
        self.assertTrue(result['guest_required'])
        self.assertFalse(result['changed'])
        self.assertIsNone(result['filesystem_bytes'])
        self.assertNotIn('prepared_filesystem_bytes', result)
        token = result['userdata_capacity_token']
        self.assertEqual(token['base_filesystem']['bytes'], 64 * MIB)
        self.assertEqual(token['data_base_sha256'], before[storage.DATA])
        self.assertEqual(set(token['layer_identity']), set(storage.LAYER_NAMES))
        self.assertEqual(self.hashes(), before)
        for name in (storage.PENDING, storage.VERIFIED, storage.GUEST_VERIFIED):
            self.assertFalse((self.avd / name).exists())

    def test_guest_deferral_requires_same_capacity_complete_key_chain_and_version(self):
        self.legacy_opaque()
        before = self.hashes()
        for size in (128 * MIB, 512 * MIB):
            with self.subTest(size=size), self.assertRaises(RuntimeError):
                storage.resize_userdata(SDK, self.avd, size, allow_guest=True)
            self.assertEqual(self.hashes(), before)
        key = self.avd / 'encryptionkey.img.qcow2'
        saved = key.read_bytes()
        key.unlink()
        with self.assertRaises(storage.OpaqueFilesystemError):
            storage.resize_userdata(SDK, self.avd, 256 * MIB, allow_guest=True)
        key.write_bytes(saved)
        self.run_tool([self.qemu, 'resize', key, 8 * MIB])
        with self.assertRaises(storage.OpaqueFilesystemError):
            storage.resize_userdata(SDK, self.avd, 256 * MIB, allow_guest=True)
        key.write_bytes(saved)
        (self.avd / 'qemu-version.txt').write_text('1')
        with self.assertRaises(storage.OpaqueFilesystemError):
            storage.resize_userdata(SDK, self.avd, 256 * MIB, allow_guest=True)

    def test_guest_proof_publishes_then_accepts_historical_capacity_with_small_raw_filesystem(self):
        self.legacy_opaque()
        prepared = storage.resize_userdata(SDK, self.avd, 256 * MIB, allow_guest=True)
        # The guest may write both overlays while their raw bases remain fixed.
        original = self.hashes()
        self.run_tool([self.qemu, 'amend', '-f', 'qcow2', '-o', 'lazy_refcounts=on',
                       self.avd / (storage.DATA + '.qcow2')])
        self.assertNotEqual(self.hashes()[storage.DATA + '.qcow2'], original[storage.DATA + '.qcow2'])
        before = self.hashes()
        with patch.object(storage, 'check_dependencies', side_effect=AssertionError('No host fs tools')):
            result = storage.publish_guest_capacity(SDK, self.avd, 256 * MIB,
                                                   self.guest_record(prepared, changed=True))
            self.run_tool([self.qemu, 'amend', '-f', 'qcow2', '-o', 'lazy_refcounts=on',
                           self.avd / 'encryptionkey.img.qcow2'])
            after_guest_write = self.hashes()
            historical = storage.resize_userdata(SDK, self.avd, 256 * MIB)
        self.assertEqual(result['capacity_proof'], 'verified-in-guest')
        self.assertIsNone(result['filesystem_bytes'])
        self.assertEqual(historical['prepared_filesystem_bytes'], 256 * MIB)
        self.assertEqual(historical['capacity_proof'], 'verified-in-guest')
        self.assertEqual(self.geometry()['bytes'], 64 * MIB)
        self.assertEqual(self.hashes(), after_guest_write)
        self.assertEqual(self.hashes()[storage.DATA], before[storage.DATA])
        self.assertEqual(self.hashes()['encryptionkey.img'], before['encryptionkey.img'])
        receipt = json.loads((self.avd / storage.GUEST_VERIFIED).read_text())
        self.assertEqual(receipt['base_filesystem']['bytes'], 64 * MIB)
        self.assertEqual(receipt['filesystem']['bytes'], 256 * MIB)
        self.assertNotIn('host_token', receipt)

    def test_guest_publisher_rejects_unverified_wrong_scope_capacity_and_typed_records(self):
        self.legacy_opaque()
        prepared = storage.resize_userdata(SDK, self.avd, 256 * MIB, allow_guest=True)
        before = self.hashes()
        record = self.guest_record(prepared)
        cases = [('verified', False), ('verified', 1), ('root_uid', False), ('name', 'Different'),
                 ('serial', 'emulator-5585'), ('boot_id', 'wrong'), ('hardware', 'other'),
                 ('selinux', 'Permissive'), ('mounted_type', 'f2fs'), ('mapped_device', '/dev/block/vda'),
                 ('mapped_device_bytes', 64 * MIB), ('requested_bytes', 64 * MIB), ('changed', 0),
                 ('host_token', None), ('probe_verified', True), ('backup', '/unexpected')]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                invalid = {**record, field: value}
                with self.assertRaisesRegex(RuntimeError, 'Guest capacity proof did not match'):
                    storage.publish_guest_capacity(SDK, self.avd, 256 * MIB, invalid)
                self.assertEqual(self.hashes(), before)
                self.assertFalse((self.avd / storage.GUEST_VERIFIED).exists())
        for field, value in (('bytes', 64 * MIB), ('block_size', True), ('uuid', 'z' * 32)):
            invalid = copy.deepcopy(record)
            invalid['filesystem'][field] = value
            with self.subTest(geometry=field), self.assertRaises(RuntimeError):
                storage.publish_guest_capacity(SDK, self.avd, 256 * MIB, invalid)
        for field, value in (('avd', str(self.avd.parent)), ('nonce', 'wrong'),
                             ('data_base_sha256', '0' * 64), ('layer_identity', {})):
            invalid = copy.deepcopy(record)
            invalid['host_token'][field] = value
            with self.subTest(token=field), self.assertRaises(RuntimeError):
                storage.publish_guest_capacity(SDK, self.avd, 256 * MIB, invalid)
        self.assertEqual(self.hashes(), before)

    def test_guest_publisher_rechecks_bases_layer_identity_and_offline_state(self):
        self.legacy_opaque()
        prepared = storage.resize_userdata(SDK, self.avd, 256 * MIB, allow_guest=True)
        record = self.guest_record(prepared)
        for name in (storage.DATA, 'encryptionkey.img'):
            path = self.avd / name
            with path.open('r+b') as stream:
                stream.seek(8192)
                old = stream.read(1)
                stream.seek(8192)
                stream.write(bytes([old[0] ^ 0xff]))
            before = self.hashes()
            with self.subTest(base=name), self.assertRaises(RuntimeError):
                storage.publish_guest_capacity(SDK, self.avd, 256 * MIB, record)
            self.assertEqual(self.hashes(), before)
            with path.open('r+b') as stream:
                stream.seek(8192)
                stream.write(old)
        with patch.object(storage, 'assert_offline', side_effect=RuntimeError('Close this AVD')), \
                self.assertRaisesRegex(RuntimeError, 'Close this AVD'):
            storage.publish_guest_capacity(SDK, self.avd, 256 * MIB, record)
        path = self.avd / (storage.DATA + '.qcow2')
        copy_path = path.with_suffix('.replacement')
        shutil.copyfile(path, copy_path)
        copy_path.replace(path)
        before = self.hashes()
        with self.assertRaises(RuntimeError):
            storage.publish_guest_capacity(SDK, self.avd, 256 * MIB, record)
        self.assertEqual(self.hashes(), before)
        self.assertFalse((self.avd / storage.GUEST_VERIFIED).exists())

    def test_guest_continuity_accepts_rename_but_refuses_replaced_overlay_and_larger_target(self):
        self.legacy_opaque()
        prepared = storage.resize_userdata(SDK, self.avd, 256 * MIB, allow_guest=True)
        storage.publish_guest_capacity(SDK, self.avd, 256 * MIB, self.guest_record(prepared))
        renamed = self.avd.with_name('Renamed.avd')
        self.avd.rename(renamed)
        self.avd, self.base = renamed, renamed / storage.DATA
        self.assertEqual(storage.resize_userdata(SDK, self.avd, 256 * MIB)['capacity_proof'], 'verified-in-guest')
        before = self.hashes()
        with self.assertRaises(storage.OpaqueFilesystemError):
            storage.resize_userdata(SDK, self.avd, 512 * MIB, allow_guest=True)
        self.assertEqual(self.hashes(), before)
        path = self.avd / (storage.DATA + '.qcow2')
        copy_path = path.with_suffix('.replacement')
        shutil.copyfile(path, copy_path)
        copy_path.replace(path)
        with self.assertRaises(storage.OpaqueFilesystemError):
            storage.resize_userdata(SDK, self.avd, 256 * MIB)
        self.assertEqual(self.hashes(), before)
        fresh = storage.resize_userdata(SDK, self.avd, 256 * MIB, allow_guest=True)
        self.assertTrue(fresh['guest_required'])

    def test_guest_marker_foreign_ownership_and_publication_failure_preserve_images(self):
        self.legacy_opaque()
        prepared = storage.resize_userdata(SDK, self.avd, 256 * MIB, allow_guest=True)
        record = self.guest_record(prepared)
        before = self.hashes()
        marker = self.avd / storage.GUEST_VERIFIED
        marker.write_text(json.dumps({'schema': 'foreign'}))
        with self.assertRaisesRegex(RuntimeError, 'foreign guest userdata capacity receipt'):
            storage.publish_guest_capacity(SDK, self.avd, 256 * MIB, record)
        self.assertEqual(self.hashes(), before)
        marker.unlink()
        with patch.object(storage, '_json', side_effect=OSError('proof write failed')), \
                self.assertRaisesRegex(OSError, 'proof write failed'):
            storage.publish_guest_capacity(SDK, self.avd, 256 * MIB, record)
        self.assertEqual(self.hashes(), before)
        self.assertFalse(marker.exists())
        storage.publish_guest_capacity(SDK, self.avd, 256 * MIB, record)
        original = marker.read_text()
        for field, value in (('requested_bytes', True), ('layer_identity', {}), ('data_base_sha256', '0' * 64)):
            invalid = json.loads(original)
            invalid[field] = value
            marker.write_text(json.dumps(invalid))
            with self.subTest(marker=field), self.assertRaises(storage.OpaqueFilesystemError):
                storage.resize_userdata(SDK, self.avd, 256 * MIB)
            self.assertEqual(self.hashes(), before)

    def test_opaque_guest_writes_keep_verified_capacity_without_host_resize(self):
        storage.resize_userdata(SDK, self.avd, 256 * MIB)
        self.opaque_overlay()
        self.opaque_overlay('encryptionkey.img')
        before = self.hashes()
        with patch.object(storage, 'check_dependencies', side_effect=RuntimeError('Missing resize2fs')) as tools:
            result = storage.resize_userdata(SDK, self.avd, 256 * MIB)
        tools.assert_not_called()
        self.assertFalse(result['changed'])
        self.assertIsNone(result['filesystem_bytes'])
        self.assertEqual(result['prepared_filesystem_bytes'], 256 * MIB)
        self.assertEqual(result['capacity_proof'], 'verified-before-encryption')
        self.assertEqual(self.hashes(), before)

    def test_completed_old_receipt_can_follow_an_opaque_chain_after_avd_rename(self):
        storage.resize_userdata(SDK, self.avd, 256 * MIB)
        (self.avd / storage.VERIFIED).unlink()
        renamed = self.avd.with_name('Renamed.avd')
        self.avd.rename(renamed)
        self.avd, self.base = renamed, renamed / storage.DATA
        self.opaque_overlay()
        before = self.hashes()
        result = storage.resize_userdata(SDK, self.avd, 256 * MIB)
        self.assertEqual(result['prepared_filesystem_bytes'], 256 * MIB)
        self.assertEqual(self.hashes(), before)

    def test_new_preboot_proof_pins_later_created_stock_key_layers(self):
        template = self.install_key_template()
        for name in ('encryptionkey.img', 'encryptionkey.img.qcow2'):
            (self.avd / name).unlink()
        storage.resize_userdata(SDK, self.avd, 256 * MIB)
        self.boot_key_layers(template)
        self.opaque_overlay()
        before = self.hashes()
        result = storage.resize_userdata(SDK, self.avd, 256 * MIB)
        self.assertEqual(result['prepared_filesystem_bytes'], 256 * MIB)
        self.assertEqual(self.hashes(), before)

    def test_old_preboot_receipt_requires_manifest_pinned_stock_key_template(self):
        template = self.install_key_template()
        for name in ('encryptionkey.img', 'encryptionkey.img.qcow2'):
            (self.avd / name).unlink()
        storage.resize_userdata(SDK, self.avd, 256 * MIB)
        (self.avd / storage.VERIFIED).unlink()
        # Model a receipt written before explicit preboot key pins existed.
        for folder in self.avd.glob('.userdata-resize-backup-*'):
            path = folder / 'transaction.json'
            receipt = json.loads(path.read_text())
            receipt.pop('key_base', None)
            path.write_text(json.dumps(receipt))
        self.boot_key_layers(template)
        self.opaque_overlay()
        result = storage.resize_userdata(SDK, self.avd, 256 * MIB)
        self.assertEqual(result['prepared_filesystem_bytes'], 256 * MIB)
        (self.avd.parent.parent / 'local/installed-release.json').unlink()
        before = self.hashes()
        with self.assertRaisesRegex(storage.OpaqueFilesystemError, 'guest-scoped'):
            storage.resize_userdata(SDK, self.avd, 256 * MIB)
        self.assertEqual(self.hashes(), before)

    def test_plaintext_noop_records_proof_before_first_encrypted_boot(self):
        result = storage.resize_userdata(SDK, self.avd, 64 * MIB)
        self.assertFalse(result['changed'])
        self.assertTrue((self.avd / storage.VERIFIED).is_file())
        self.opaque_overlay()
        result = storage.resize_userdata(SDK, self.avd, 64 * MIB)
        self.assertEqual(result['prepared_filesystem_bytes'], 64 * MIB)

    def test_opaque_legacy_oversize_disk_is_not_claimed_as_filesystem_growth(self):
        self.run_tool([self.qemu, 'resize', self.base, 256 * MIB])
        self.opaque_overlay()
        before = self.hashes()
        with self.assertRaisesRegex(storage.OpaqueFilesystemError, 'guest-scoped'):
            storage.resize_userdata(SDK, self.avd, 256 * MIB)
        self.assertEqual(self.hashes(), before)
        self.assertEqual(self.geometry()['bytes'], 64 * MIB)

    def test_opaque_growth_and_shrink_refuse_without_writing_data_or_keys(self):
        storage.resize_userdata(SDK, self.avd, 256 * MIB)
        self.opaque_overlay()
        before = self.hashes()
        for size, message in ((512 * MIB, 'guest-scoped'), (128 * MIB, 'reduced')):
            with self.subTest(size=size), self.assertRaisesRegex(RuntimeError, message):
                storage.resize_userdata(SDK, self.avd, size)
            self.assertEqual(self.hashes(), before)

    def test_opaque_continuity_rejects_changed_raw_userdata_and_key_bases(self):
        storage.resize_userdata(SDK, self.avd, 256 * MIB)
        self.opaque_overlay()
        for name in ('encryptionkey.img', storage.DATA):
            path = self.avd / name
            with path.open('r+b') as stream:
                stream.seek(8192)
                saved = stream.read(1)
                stream.seek(8192)
                stream.write(bytes([saved[0] ^ 0xff]))
            before = self.hashes()
            with self.subTest(name=name), self.assertRaisesRegex(storage.OpaqueFilesystemError, 'guest-scoped'):
                storage.resize_userdata(SDK, self.avd, 256 * MIB)
            self.assertEqual(self.hashes(), before)
            with path.open('r+b') as stream:
                stream.seek(8192)
                stream.write(saved)

    def test_opaque_continuity_rejects_forged_missing_and_foreign_proofs(self):
        storage.resize_userdata(SDK, self.avd, 256 * MIB)
        self.opaque_overlay()
        marker = self.avd / storage.VERIFIED
        original = marker.read_text()
        before = self.hashes()
        for field, value in (('schema', 'foreign'), ('filesystem', {'bytes': 256 * MIB}),
                             ('data_base_sha256', '0' * 64), ('key_base', None)):
            with self.subTest(field=field):
                saved = json.loads(original)
                saved[field] = value
                marker.write_text(json.dumps(saved))
                with self.assertRaisesRegex(storage.OpaqueFilesystemError, 'guest-scoped'):
                    storage.resize_userdata(SDK, self.avd, 256 * MIB)
                self.assertEqual(self.hashes(), before)
        marker.unlink()
        foreign = self.avd / 'foreign-capacity.json'
        foreign.write_text(original)
        marker.symlink_to(foreign.name)
        with self.assertRaisesRegex(storage.OpaqueFilesystemError, 'guest-scoped'):
            storage.resize_userdata(SDK, self.avd, 256 * MIB)
        self.assertEqual(foreign.read_text(), original)
        marker.unlink()
        for folder in self.avd.glob('.userdata-resize-backup-*'):
            shutil.rmtree(folder)
        with self.assertRaisesRegex(storage.OpaqueFilesystemError, 'guest-scoped'):
            storage.resize_userdata(SDK, self.avd, 256 * MIB)
        self.assertEqual(self.hashes(), before)

    def test_opaque_continuity_requires_metadata_overlay_and_qemu_version_two(self):
        storage.resize_userdata(SDK, self.avd, 256 * MIB)
        self.opaque_overlay()
        (self.avd / 'qemu-version.txt').write_text('1')
        before = self.hashes()
        with self.assertRaisesRegex(storage.OpaqueFilesystemError, 'guest-scoped'):
            storage.resize_userdata(SDK, self.avd, 256 * MIB)
        self.assertEqual(self.hashes(), before)
        (self.avd / 'qemu-version.txt').write_text('2')
        (self.avd / 'encryptionkey.img.qcow2').unlink()
        before = self.hashes()
        with self.assertRaisesRegex(storage.OpaqueFilesystemError, 'guest-scoped'):
            storage.resize_userdata(SDK, self.avd, 256 * MIB)
        self.assertEqual(self.hashes(), before)

    def test_plaintext_growth_refuses_foreign_marker_before_any_disk_changes(self):
        before = self.hashes()
        marker = self.avd / storage.VERIFIED
        foreign = self.avd / 'foreign-marker'
        foreign.write_bytes(b'foreign marker must stay intact')
        for kind in ('foreign-schema', 'malformed-json', 'marker-symlink', 'marker-directory', 'temporary-symlink'):
            with self.subTest(kind=kind):
                if kind == 'foreign-schema':
                    marker.write_text(json.dumps({'schema': 'foreign'}))
                elif kind == 'malformed-json':
                    marker.write_text('{')
                elif kind == 'marker-symlink':
                    marker.symlink_to(foreign.name)
                elif kind == 'marker-directory':
                    marker.mkdir()
                else:
                    marker.with_suffix('.next').symlink_to(foreign.name)
                with self.assertRaisesRegex(RuntimeError, 'foreign userdata capacity receipt'):
                    storage.resize_userdata(SDK, self.avd, 256 * MIB)
                self.assertEqual(self.hashes(), before)
                self.assertFalse((self.avd / storage.PENDING).exists())
                self.assertEqual(foreign.read_bytes(), b'foreign marker must stay intact')
                if marker.is_dir() and not marker.is_symlink():
                    marker.rmdir()
                else:
                    marker.unlink(missing_ok=True)
                marker.with_suffix('.next').unlink(missing_ok=True)

    def test_capacity_proof_preparation_failure_rolls_back_data_and_keeps_previous_marker(self):
        storage.resize_userdata(SDK, self.avd, 64 * MIB)
        before = self.hashes()
        marker = (self.avd / storage.VERIFIED).read_bytes()
        with patch.object(storage, '_remember_filesystem', side_effect=RuntimeError('injected proof preparation failure')), \
                self.assertRaisesRegex(RuntimeError, 'proof preparation'):
            storage.resize_userdata(SDK, self.avd, 256 * MIB)
        self.assertEqual(self.hashes(), before)
        self.assertEqual((self.avd / storage.VERIFIED).read_bytes(), marker)
        self.assertFalse((self.avd / storage.PENDING).exists())

    def test_postcommit_marker_publication_failure_keeps_durable_capacity_proof(self):
        storage.resize_userdata(SDK, self.avd, 64 * MIB)
        before = self.hashes()
        marker = self.avd / storage.VERIFIED
        previous = marker.read_bytes()
        write_json = storage._json
        def fail_marker(path, value):
            if path == marker:
                raise OSError('injected marker publication failure')
            return write_json(path, value)
        with patch.object(storage, '_json', side_effect=fail_marker):
            result = storage.resize_userdata(SDK, self.avd, 256 * MIB)
        self.assertTrue(result['changed'])
        self.assertEqual(self.geometry()['bytes'], 256 * MIB)
        self.probe_is_retained()
        self.assertEqual(marker.read_bytes(), previous)
        backup = Path(result['backup'])
        self.assertEqual(storage.digest(backup / 'original' / storage.DATA), before[storage.DATA])
        self.assertIn('key_base', json.loads((backup / 'transaction.json').read_text()))
        self.opaque_overlay()
        opaque = storage.resize_userdata(SDK, self.avd, 256 * MIB)
        self.assertEqual(opaque['prepared_filesystem_bytes'], 256 * MIB)

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
