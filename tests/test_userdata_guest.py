"""Exercise guest-only ext4 capacity guards without connecting to any AVD."""
import hashlib
import json
from pathlib import Path
import re
import shlex
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import userdata_guest as guest

GIB = 1024**3
TARGET = 32 * GIB
BOOT = '01234567-89ab-cdef-0123-456789abcdef'


def superblock(size, identity=bytes(range(16)), *, incompatible=0x84):
    data = bytearray(4096)
    blocks = size // 4096
    struct.pack_into('<I', data, 1028, blocks & 0xffffffff)
    struct.pack_into('<I', data, 1048, 2)
    struct.pack_into('<H', data, 1080, 0xef53)
    struct.pack_into('<I', data, 1120, incompatible)
    struct.pack_into('<I', data, 1360, blocks >> 32)
    data[1128:1144] = identity
    return bytes(data)


class FakeGuest:
    """Model binary reads, mounted identity and the externally observable writes."""
    def __init__(self, config, *, filesystem_bytes=6 * GIB):
        self.config = config
        self.identity = ['0', 'Enforcing', config['name'], 'ranchu', 'hongkong',
                         'OS4.0.18.0.XFRCNXM', '1', BOOT]
        self.device, self.capacity = '/dev/block/dm-56', TARGET
        self.filesystem_bytes, self.uuid = filesystem_bytes, bytes(range(16))
        self.mounts = None
        self.header_override = None
        self.serial = 'emulator-' + str(config['port'])
        self.tool_available = True
        self.calls, self.events, self.probes = [], [], {}
        self.before = None
        self.after_resize = None
        self.resize_failure = None
        self.corrupt_probe = False

    def mount_lines(self):
        return self.mounts if self.mounts is not None else [f'{self.device} /data ext4 rw,seclabel 0 0']

    def invoke(self, config, *arguments, **kwargs):
        self.assert_config(config, kwargs)
        self.calls.append((arguments, kwargs))
        if arguments == ('get-serialno',):
            return subprocess.CompletedProcess(arguments, 0, self.serial + '\n', '')
        if arguments[0] not in ('shell', 'exec-out') or len(arguments) != 2:
            raise AssertionError('Unscoped or unexpected ADB command: ' + repr(arguments))
        wrapper = shlex.split(arguments[1])
        if wrapper[:3] != ['su', '-W', '-c'] or len(wrapper) != 4:
            raise AssertionError('Sensitive command did not use the expected root wrapper.')
        script = wrapper[3]
        if not script.startswith('set -eu\n'):
            raise AssertionError('Root shell must stop after a failed guard.')
        if script == 'set -eu\n' + guest.IDENTITY_QUERY:
            phase = 'identity'
        elif re.search(r'^/system/bin/resize2fs /dev/block/dm-[0-9]+$', script, re.M):
            phase = 'resize'
        elif 'printf ' in script:
            phase = 'probe-create'
        elif re.search(r'^rm /data/local/tmp/', script, re.M):
            phase = 'probe-remove'
        elif re.search(r'^sha256sum /data/local/tmp/', script, re.M):
            phase = 'probe-check'
        elif 'test -x /system/bin/resize2fs' in script:
            phase = 'tool'
        elif re.search(r'^dd if=/dev/block/dm-', script, re.M):
            phase = 'header'
        elif re.search(r'^/system/bin/blockdev --getsize64 ', script, re.M):
            phase = 'capacity'
        elif script.endswith('\nsync'):
            phase = 'sync'
        else:
            raise AssertionError('Unexpected root command: ' + script)
        self.events.append(phase)
        if self.before:
            self.before(phase, self)
        output, status = '', 0
        if phase == 'identity':
            output = '\n'.join(self.identity + self.mount_lines()) + '\n'
        elif not self.guard_matches(script):
            status = 1
        elif phase == 'capacity':
            output = str(self.capacity) + '\n'
        elif phase == 'header':
            self.assert_binary(arguments, kwargs)
            output = self.header_override if self.header_override is not None else superblock(self.filesystem_bytes, self.uuid)
        elif phase == 'tool':
            status = 0 if self.tool_available else 1
        elif phase == 'probe-create':
            match = re.search(r"printf '%s\\n' ([a-f0-9]{64}) > "
                              r"(/data/local/tmp/hyperos-avd-userdata-probe-[a-f0-9]{32})", script)
            if not match:
                raise AssertionError('Probe contents/path were not bounded random values.')
            nonce, path = match.groups()
            self.probes[path] = hashlib.sha256((nonce + '\n').encode()).hexdigest()
        elif phase == 'probe-check':
            path = script.splitlines()[-1].split()[-1]
            checksum = '0' * 64 if self.corrupt_probe else self.probes[path]
            output = checksum + '  ' + path + '\n'
        elif phase == 'probe-remove':
            path = script.splitlines()[-1].split()[-1]
            del self.probes[path]
        elif phase == 'resize':
            if kwargs['timeout'] != guest.GROW_TIMEOUT:
                raise AssertionError('Online resize must have a bounded growth timeout.')
            if f'--getsize64 {self.device})" = {self.capacity}' not in script:
                status = 1
            elif isinstance(self.resize_failure, Exception):
                raise self.resize_failure
            elif self.resize_failure:
                status = 1
            else:
                self.filesystem_bytes = self.capacity
                if self.after_resize:
                    self.after_resize(self)
        return subprocess.CompletedProcess(arguments, status, output, 'private error detail must not leak')

    def guard_matches(self, script):
        expected = [('id -u', self.identity[0]), ('getenforce', self.identity[1]),
                    ('getprop ro.boot.qemu.avd_name', self.identity[2]),
                    ('getprop ro.boot.hardware', self.identity[3]),
                    ('getprop ro.product.device', self.identity[4]),
                    ('getprop ro.mi.os.version.incremental', self.identity[5]),
                    ('getprop sys.boot_completed', self.identity[6]),
                    ('cat /proc/sys/kernel/random/boot_id', self.identity[7])]
        for command, value in expected:
            quoted = shlex.quote(value)
            if f'test "$({command})" = {quoted}' not in script:
                return False
        return (f'test "$source" = {self.device}' in script
                and self.mount_lines() == [f'{self.device} /data ext4 rw,seclabel 0 0'])

    def assert_config(self, config, kwargs):
        for key in ('sdk', 'name', 'port'):
            if config[key] != self.config[key]:
                raise AssertionError('ADB was redirected to a different guest.')
        if not kwargs.get('capture_output') or kwargs.get('timeout') not in (guest.QUERY_TIMEOUT, guest.GROW_TIMEOUT):
            raise AssertionError('All guest commands must be captured and bounded.')

    def assert_binary(self, arguments, kwargs):
        if arguments[0] != 'exec-out' or kwargs.get('text') is not False:
            raise AssertionError('Superblock reads must preserve binary bytes.')


class GuestCapacityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name) / 'offline backup'
        self.config = {'sdk': '/unused/SDK', 'name': 'Renamed_Private_AVD', 'port': 5588,
                       'product_device': 'hongkong', 'incremental': 'OS4.0.18.0.XFRCNXM',
                       'userdata_capacity_token': {'nonce': 'opaque immutable host binding'}}
        self.fake = FakeGuest(self.config)
        self.mock_adb = patch.object(guest, 'adb', side_effect=self.fake.invoke).start()
        self.addCleanup(patch.stopall)

    def backup(self):
        (self.folder / 'avd').mkdir(parents=True)
        files = {}
        for name in guest.LAYER_NAMES:
            path = self.folder / 'avd' / name
            path.write_bytes(('offline only ' + name).encode())
            files['avd/' + name] = hashlib.sha256(path.read_bytes()).hexdigest()
        (self.folder / 'backup.json').write_text(json.dumps({'name': self.config['name'], 'files': files}))
        return self.folder

    def grow(self, **kwargs):
        return guest.verify_and_grow(self.config, TARGET, **kwargs)

    def assert_no_writes(self):
        self.assertFalse(set(self.fake.events) & {'probe-create', 'probe-remove', 'resize', 'sync'})

    def test_already_sized_guest_is_read_only_and_echoes_a_detached_token(self):
        self.fake.filesystem_bytes = TARGET
        record = self.grow(allow_grow=False)
        self.assertTrue(record['verified'])
        self.assertFalse(record['changed'])
        self.assertEqual(record['capacity_proof'], 'verified-in-guest')
        self.assertEqual(record['filesystem']['bytes'], TARGET)
        self.assertEqual(record['filesystem']['uuid'], bytes(range(16)).hex())
        self.assertEqual(record['filesystem']['incompatible'], 0x80)
        self.assertEqual(record['mapped_device_bytes'], TARGET)
        self.assertEqual(record['host_token'], self.config['userdata_capacity_token'])
        self.assertIsNot(record['host_token'], self.config['userdata_capacity_token'])
        self.assertIsNone(record['probe_verified'])
        self.assertIsNone(record['backup'])
        self.assert_no_writes()

    def test_64_bit_superblock_count_is_read_as_binary(self):
        size = ((1 << 32) + 7) * 4096
        self.fake.capacity = self.fake.filesystem_bytes = size
        record = guest.verify_and_grow(self.config, size)
        self.assertEqual(record['filesystem']['bytes'], size)
        self.assert_no_writes()

    def test_invalid_explicit_config_is_refused_before_adb(self):
        for field, value in (('port', 5589), ('port', True), ('port', 1), ('name', 'bad;name'),
                             ('name', ''), ('sdk', ''), ('serial', 'emulator-5590')):
            with self.subTest(field=field, value=value):
                config = dict(self.config, **{field: value})
                with self.assertRaises(guest.GuestCapacityError):
                    guest.verify_and_grow(config, TARGET)
        self.mock_adb.assert_not_called()

    def test_wrong_serial_is_refused(self):
        self.fake.serial = 'emulator-5590'
        with self.assertRaisesRegex(guest.GuestCapacityError, 'serial'):
            self.grow()
        self.assert_no_writes()

    def test_guest_identity_and_root_guards_refuse_before_mutation(self):
        for index, value in ((0, '2000'), (1, 'Permissive'), (2, 'Another_AVD'), (3, 'other'),
                             (4, 'yingtian'), (5, 'foreign incremental'), (6, '0'), (7, 'malformed')):
            with self.subTest(index=index):
                original = self.fake.identity[index]
                self.fake.identity[index] = value
                try:
                    with self.assertRaises(guest.GuestCapacityError):
                        self.grow()
                finally:
                    self.fake.identity[index] = original
        self.assert_no_writes()

    def test_unsafe_mounts_are_refused(self):
        for mounts in (['/dev/block/vda /data ext4 rw 0 0'], ['/dev/block/dm-56 /data f2fs rw 0 0'],
                       ['/dev/block/dm-56 /data ext4 ro 0 0'], ['/dev/block/dm-56 /data ext4 rw,ro 0 0'],
                       ['/dev/block/dm-56;bad /data ext4 rw 0 0'], [],
                       ['/dev/block/dm-56 /data ext4 rw 0 0'] * 2):
            with self.subTest(mounts=mounts):
                self.fake.mounts = mounts
                with self.assertRaises(guest.GuestCapacityError):
                    self.grow()
        self.assert_no_writes()

    def test_short_or_opaque_superblock_is_refused(self):
        for header in (b'\0' * 1024, b'\0' * 4096):
            with self.subTest(length=len(header)):
                self.fake.header_override = header
                with self.assertRaises(guest.GuestCapacityError):
                    self.grow()
        self.assert_no_writes()

    def test_wrong_mapping_capacity_and_shrink_are_refused(self):
        self.fake.capacity = 64 * GIB
        with self.assertRaisesRegex(guest.GuestCapacityError, 'mapped device'):
            self.grow()
        self.fake.capacity, self.fake.filesystem_bytes = TARGET, 64 * GIB
        with self.assertRaisesRegex(guest.GuestCapacityError, 'shrink'):
            self.grow()
        self.assert_no_writes()

    def test_growth_policy_and_absent_backup_refuse_without_probe(self):
        with self.assertRaisesRegex(guest.GuestCapacityError, 'disabled'):
            self.grow(allow_grow=False)
        with self.assertRaisesRegex(guest.GuestCapacityError, 'offline'):
            self.grow()
        self.assert_no_writes()

    def test_backup_name_missing_layers_and_hashes_are_verified_before_probe(self):
        folder = self.backup()
        receipt = folder / 'backup.json'
        original = json.loads(receipt.read_text())
        bad_name = dict(original, name='Another_AVD')
        missing = dict(original, files={key: value for key, value in original['files'].items()
                                       if not key.endswith('encryptionkey.img.qcow2')})
        changed_hash = dict(original, files=dict(original['files'], **{'avd/userdata-qemu.img': '0' * 64}))
        for malformed in (bad_name, missing, changed_hash):
            with self.subTest(receipt=malformed):
                receipt.write_text(json.dumps(malformed))
                with self.assertRaisesRegex(guest.GuestCapacityError, 'backup'):
                    self.grow(backup=folder)
        self.assert_no_writes()

    def test_symlinked_backup_layer_is_refused(self):
        folder = self.backup()
        path = folder / 'avd/encryptionkey.img'
        saved = folder / 'original-key'
        path.rename(saved)
        path.symlink_to(saved)
        with self.assertRaisesRegex(guest.GuestCapacityError, 'backup'):
            self.grow(backup=folder)
        self.assert_no_writes()

    def test_growth_preserves_uuid_probe_and_offline_backup_bytes(self):
        folder = self.backup()
        hashes_before = {path.name: path.read_bytes() for path in (folder / 'avd').iterdir()}
        record = self.grow(backup=folder)
        self.assertTrue(record['changed'])
        self.assertTrue(record['probe_verified'])
        self.assertEqual(record['filesystem']['bytes'], TARGET)
        self.assertEqual(record['filesystem']['uuid'], bytes(range(16)).hex())
        self.assertEqual(record['backup'], str(folder.resolve()))
        self.assertEqual(record['schema'], guest.SCHEMA)
        self.assertEqual(record['host_token'], self.config['userdata_capacity_token'])
        self.assertEqual(self.fake.events.count('resize'), 1)
        self.assertLess(self.fake.events.index('probe-check'), self.fake.events.index('resize'))
        self.assertGreater(self.fake.events.index('probe-remove'), self.fake.events.index('resize'))
        self.assertEqual(self.fake.events[-1], 'sync')
        self.assertFalse(self.fake.probes)
        self.assertEqual(hashes_before, {path.name: path.read_bytes() for path in (folder / 'avd').iterdir()})

    def test_missing_resize_binary_is_refused_before_probe(self):
        self.fake.tool_available = False
        with self.assertRaises(guest.GuestCapacityError) as error:
            self.grow(backup=self.backup())
        self.assertFalse(error.exception.resize_attempted)
        self.assert_no_writes()

    def test_pre_resize_superblock_change_is_refused_and_keeps_probe(self):
        def change(phase, model):
            if phase == 'header' and model.probes:
                model.uuid = bytes(reversed(range(16)))
        self.fake.before = change
        with self.assertRaisesRegex(guest.GuestCapacityError, 'before resize') as error:
            self.grow(backup=self.backup())
        self.assertFalse(error.exception.resize_attempted)
        self.assertNotIn('resize', self.fake.events)
        self.assertTrue(self.fake.probes)

    def test_final_root_guard_refuses_capacity_change_before_resize(self):
        self.fake.before = lambda phase, model: setattr(model, 'capacity', 64 * GIB) if phase == 'resize' else None
        with self.assertRaises(guest.GuestCapacityError) as error:
            self.grow(backup=self.backup())
        self.assertTrue(error.exception.resize_attempted)
        self.assertEqual(self.fake.filesystem_bytes, 6 * GIB)
        self.assertTrue(self.fake.probes)

    def test_post_resize_uuid_reboot_or_mapping_changes_never_claim_success(self):
        for index, change in enumerate((lambda model: setattr(model, 'uuid', bytes(reversed(range(16)))),
                                      lambda model: model.identity.__setitem__(7, '11111111-2222-3333-4444-555555555555'),
                                      lambda model: setattr(model, 'device', '/dev/block/dm-57'))):
            with self.subTest(change=change):
                self.fake = FakeGuest(self.config)
                self.mock_adb.side_effect = self.fake.invoke
                self.fake.after_resize = change
                folder = self.folder / str(index)
                saved_folder = self.folder
                self.folder = folder
                try:
                    with self.assertRaises(guest.GuestCapacityError) as error:
                        self.grow(backup=self.backup())
                finally:
                    self.folder = saved_folder
                self.assertTrue(error.exception.resize_attempted)
                self.assertIn('cannot be rolled back', str(error.exception))
                self.assertTrue(self.fake.probes)
                self.assertNotIn('probe-remove', self.fake.events)

    def test_resize_failure_or_timeout_keeps_backup_and_never_retries(self):
        folder = self.backup()
        for failure in (True, subprocess.TimeoutExpired('resize', guest.GROW_TIMEOUT)):
            with self.subTest(failure=type(failure).__name__):
                self.fake = FakeGuest(self.config)
                self.mock_adb.side_effect = self.fake.invoke
                self.fake.resize_failure = failure
                with self.assertRaises(guest.GuestCapacityError) as error:
                    self.grow(backup=folder)
                self.assertTrue(error.exception.resize_attempted)
                self.assertEqual(self.fake.events.count('resize'), 1)
                self.assertNotIn('probe-remove', self.fake.events)
                self.assertTrue((folder / 'backup.json').is_file())
                self.assertNotIn('private error detail', str(error.exception))

    def test_changed_probe_after_resize_is_retained_and_refuses_attestation(self):
        self.fake.after_resize = lambda model: setattr(model, 'corrupt_probe', True)
        with self.assertRaisesRegex(guest.GuestCapacityError, 'probe changed') as error:
            self.grow(backup=self.backup())
        self.assertTrue(error.exception.resize_attempted)
        self.assertTrue(self.fake.probes)
        self.assertNotIn('probe-remove', self.fake.events)


if __name__ == '__main__':
    unittest.main()
