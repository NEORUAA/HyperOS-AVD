"""Bound decrypted capacity repair to one verified, backed-up AVD process."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import common
import manage
import userdata_resize

GIB = 1024**3
BOOT_FILES = ('images/system.img', 'images/vendor.img', 'images/kernel-ranchu',
              'images/ramdisk.img', 'tools/ksud-aarch64-linux-android')


class ManageGuestStorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = (Path(self.temporary.name) / 'Renamed instance').resolve()
        self.name, self.port = 'Renamed_AVD', 5598
        self.sdk = Path(self.temporary.name) / 'SDK'
        self.folder = self.root / 'backups' / 'before-storage-repair'
        self.folder.mkdir(parents=True)
        self.avd = self.root / 'avd' / (self.name + '.avd')
        self.avd.mkdir(parents=True)
        (self.avd / 'config.ini').write_text('hw.ramSize=6144\nhw.cpu.ncore=4\ndisk.dataPartition.size=32G\n')
        self.saved = {'name': self.name, 'port': self.port, 'sdk': '/old/sdk',
                      'hardware': {'disk.dataPartition.size': '32G'}, 'camera_bridge': False}
        (self.root / 'local').mkdir()
        self.write_runtime()
        self.manifest = {'variant': 'os4-official', 'version': 'v0.2.1-a17-hyperos4-hongkong-r2',
                         'build': {'source': 'official-hongkong-ota', 'hyperos': '4.0.17.0.XFRCNXM'}, 'files': {}}
        for name in BOOT_FILES:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            data = ('verified ' + name).encode()
            path.write_bytes(data)
            self.manifest['files'][name] = {'sha256': hashlib.sha256(data).hexdigest()}
        self.write_manifest()
        self.protected = {'encryptionkey.img': b'original key', 'encryptionkey.img.qcow2': b'original key overlay',
                          'qemu-version.txt': b'2'}
        for name, data in self.protected.items():
            (self.avd / name).write_bytes(data)
        self.events, self.adb_calls = [], []
        self.token = {'schema': 1, 'data': 'verified private capacity token'}
        self.record = {'schema': 1, 'filesystem_bytes': 32 * GIB, 'proof': 'owned decrypted guest'}
        self.guest = types.ModuleType('userdata_guest')
        self.guest.verify_and_grow = Mock(side_effect=self.verify)
        self.process = Mock()
        self.process.pid = 43210
        self.process.poll.return_value = None
        self.process.wait.side_effect = self.wait
        self.process.terminate.side_effect = lambda: self.events.append('terminate-own-pid')
        self.mocks = {}
        self.registry = Path(self.temporary.name) / 'isolated-registry'
        self.registry.mkdir()
        self.install_mock('avd_home', patch.object(manage, 'avd_home', return_value=self.registry))
        self.install_mock('idle', patch.object(manage, 'idle', side_effect=lambda *_: self.events.append('idle')))
        self.install_mock('resize', patch.object(manage, 'resize', side_effect=self.resize))
        self.install_mock('popen', patch.object(manage.subprocess, 'Popen', side_effect=self.spawn))
        self.install_mock('adb', patch.object(common, 'adb', side_effect=self.adb))
        self.install_mock('sleep', patch.object(manage.time, 'sleep'))
        self.install_mock('publish', patch.object(userdata_resize, 'publish_guest_capacity',
                                                  side_effect=self.publish, create=True))
        self.install_mock('features', patch('launch.vulkan_features', return_value=['-feature', 'VerifiedVulkan']))
        self.install_mock('rear', patch('rear_display_config.runtime_options', return_value=['-verified-rear-panel']))
        self.install_mock('guest_module', patch.dict(sys.modules, {'userdata_guest': self.guest}))

    def install_mock(self, name, context):
        self.mocks[name] = context.start()
        self.addCleanup(context.stop)

    def write_manifest(self):
        (self.root / 'local/installed-release.json').write_text(json.dumps(self.manifest))

    def write_runtime(self):
        (self.root / 'local/runtime.json').write_text(json.dumps(self.saved))

    def resize(self, *_args, **_kwargs):
        self.events.append('offline-proof')
        return {'guest_required': True, 'userdata_capacity_token': self.token}

    def spawn(self, *_args, **_kwargs):
        self.events.append('spawn')
        return self.process

    def verify(self, *_args, **_kwargs):
        self.events.append('verify-and-grow')
        return self.record

    def wait(self, **_kwargs):
        self.events.append('wait-owned-exit')
        self.process.poll.return_value = 0
        return 0

    def publish(self, *_args, **_kwargs):
        self.events.append('publish')
        return {'changed': True, 'filesystem_bytes': 32 * GIB}

    def adb(self, config, *arguments, **_kwargs):
        self.assertEqual(config['name'], self.name)
        self.assertEqual(config['port'], self.port)
        self.assertEqual(config['sdk'], str(self.sdk))
        self.assertEqual(config['userdata_capacity_token'], self.token)
        self.adb_calls.append(arguments)
        outputs = {('get-state',): 'device', ('emu', 'avd', 'name'): self.name + '\nOK',
                   ('shell', 'getprop sys.boot_completed'): '1', ('shell', 'su -W -c sync'): '',
                   ('emu', 'kill'): ''}
        self.assertIn(arguments, outputs, 'Storage orchestration exceeded its ADB scope.')
        self.events.append('adb:' + ' '.join(arguments))
        return subprocess.CompletedProcess(arguments, 0, outputs[arguments], '')

    def prepare(self):
        return manage.prepare_storage(self.root, self.name, self.port, self.sdk, 32, self.folder)

    def assert_no_publish(self):
        self.mocks['publish'].assert_not_called()
        self.assertFalse((self.folder / 'storage-repair.json').exists())

    def test_verified_offline_noop_does_not_read_manifest_or_boot_a_guest(self):
        proof = {'changed': False, 'filesystem_bytes': 32 * GIB}
        self.mocks['resize'].side_effect = None
        self.mocks['resize'].return_value = proof
        (self.root / 'local/installed-release.json').unlink()
        self.assertEqual(self.prepare(), proof)
        self.mocks['resize'].assert_called_once_with(self.sdk, self.avd, 32, allow_guest=True)
        self.mocks['popen'].assert_not_called()
        self.mocks['adb'].assert_not_called()
        self.guest.verify_and_grow.assert_not_called()
        self.assert_no_publish()

    def test_owned_guest_proof_is_published_only_after_sync_exit_and_idle(self):
        result = self.prepare()
        self.assertEqual(result, {'changed': True, 'filesystem_bytes': 32 * GIB})
        config = {**self.saved, 'sdk': str(self.sdk), 'userdata_capacity_token': self.token}
        self.guest.verify_and_grow.assert_called_once_with(config, 32 * GIB, backup=self.folder)
        self.mocks['publish'].assert_called_once_with(self.sdk, self.avd, 32 * GIB, self.record)
        self.assertEqual(json.loads((self.folder / 'storage-repair.json').read_text()), self.record)
        tail = self.events[self.events.index('verify-and-grow'):]
        self.assertEqual(tail, ['verify-and-grow', 'adb:shell su -W -c sync', 'adb:emu avd name',
                                'adb:emu kill', 'wait-owned-exit', 'idle', 'publish'])
        self.process.terminate.assert_not_called()
        self.process.wait.assert_called_once_with(timeout=60)
        command = self.mocks['popen'].call_args.args[0]
        for option, expected in (('-avd', self.name), ('-sysdir', str(self.root / 'images')),
                                 ('-port', str(self.port)), ('-memory', '6144'), ('-cores', '4')):
            self.assertEqual(command[command.index(option) + 1], expected)
        for option in ('-no-window', '-no-snapshot-load', '-no-snapshot-save', 'VerifiedVulkan', '-verified-rear-panel'):
            self.assertIn(option, command)
        self.assertTrue(self.mocks['popen'].call_args.kwargs['start_new_session'])
        for name, data in self.protected.items():
            self.assertEqual((self.avd / name).read_bytes(), data)

    def test_unknown_firmware_variant_refuses_before_spawn(self):
        self.manifest['variant'] = 'foreign-firmware'
        self.write_manifest()
        with self.assertRaisesRegex(RuntimeError, 'known installed release'):
            self.prepare()
        self.mocks['popen'].assert_not_called()
        self.assert_no_publish()

    def test_foreign_avd_registry_refuses_before_idle_or_capacity_probe(self):
        foreign = self.root.parent / 'another-workspace' / 'Foreign.avd'
        (self.registry / (self.name + '.ini')).write_text('path=' + str(foreign) + '\n')
        with self.assertRaisesRegex(RuntimeError, 'another workspace'):
            self.prepare()
        self.mocks['idle'].assert_not_called()
        self.mocks['resize'].assert_not_called()
        self.mocks['popen'].assert_not_called()
        self.mocks['adb'].assert_not_called()
        self.assert_no_publish()

    def test_matching_registry_allows_the_owned_capacity_probe(self):
        (self.registry / (self.name + '.ini')).write_text('path=' + str(self.avd) + '\n')
        self.mocks['resize'].side_effect = None
        self.mocks['resize'].return_value = {'changed': False, 'filesystem_bytes': 32 * GIB}
        self.prepare()
        self.mocks['resize'].assert_called_once_with(self.sdk, self.avd, 32, allow_guest=True)
        self.mocks['popen'].assert_not_called()

    def test_invalid_avd_name_refuses_before_scope_or_capacity_probe(self):
        with self.assertRaisesRegex(RuntimeError, 'AVD names'):
            manage.prepare_storage(self.root, '../Other_AVD', self.port, self.sdk, 32, self.folder)
        self.mocks['idle'].assert_not_called()
        self.mocks['resize'].assert_not_called()
        self.mocks['popen'].assert_not_called()
        self.assert_no_publish()

    def test_mismatched_registered_name_or_port_refuses_before_spawn(self):
        for field, value in (('name', 'Another_AVD'), ('port', 5574)):
            with self.subTest(field=field):
                original = self.saved[field]
                self.saved[field] = value
                self.write_runtime()
                with self.assertRaisesRegex(RuntimeError, 'different instance'):
                    self.prepare()
                self.saved[field] = original
                self.write_runtime()
        self.mocks['popen'].assert_not_called()
        self.assert_no_publish()

    def test_each_mutated_boot_input_refuses_before_spawn(self):
        for relative in BOOT_FILES:
            with self.subTest(relative=relative):
                path = self.root / relative
                original = path.read_bytes()
                path.write_bytes(original + b'mutated')
                try:
                    with self.assertRaisesRegex(RuntimeError, 'firmware differs'):
                        self.prepare()
                finally:
                    path.write_bytes(original)
        self.mocks['popen'].assert_not_called()
        self.assert_no_publish()

    def test_missing_boot_input_hash_refuses_before_spawn(self):
        self.manifest['files']['images/vendor.img'] = {}
        self.write_manifest()
        with self.assertRaisesRegex(RuntimeError, 'firmware differs'):
            self.prepare()
        self.mocks['popen'].assert_not_called()
        self.assert_no_publish()

    def test_wrong_adb_guest_terminates_only_owned_child_without_console_kill(self):
        normal = self.mocks['adb'].side_effect
        def wrong_identity(config, *arguments, **kwargs):
            if arguments == ('emu', 'avd', 'name'):
                self.adb_calls.append(arguments)
                return subprocess.CompletedProcess(arguments, 0, 'Other_AVD\nOK', '')
            return normal(config, *arguments, **kwargs)
        self.mocks['adb'].side_effect = wrong_identity
        with self.assertRaisesRegex(RuntimeError, 'different AVD'):
            self.prepare()
        self.process.terminate.assert_called_once_with()
        self.process.wait.assert_called_once_with(timeout=60)
        self.assertNotIn(('emu', 'kill'), self.adb_calls)
        self.guest.verify_and_grow.assert_not_called()
        self.assert_no_publish()

    def test_unverified_adb_timeout_terminates_only_owned_child(self):
        self.mocks['adb'].side_effect = subprocess.TimeoutExpired('adb get-state', 10)
        with patch.object(manage.time, 'monotonic', side_effect=[0, 1, 301]):
            with self.assertRaisesRegex(RuntimeError, 'did not finish booting'):
                self.prepare()
        self.process.terminate.assert_called_once_with()
        self.process.wait.assert_called_once_with(timeout=60)
        self.assertNotIn(('emu', 'kill'), self.adb_calls)
        self.guest.verify_and_grow.assert_not_called()
        self.assert_no_publish()

    def test_exited_child_never_calls_or_kills_another_guest(self):
        self.process.poll.return_value = 1
        with self.assertRaisesRegex(RuntimeError, 'emulator exited'):
            self.prepare()
        self.mocks['adb'].assert_not_called()
        self.process.terminate.assert_not_called()
        self.guest.verify_and_grow.assert_not_called()
        self.assert_no_publish()

    def test_guest_proof_failure_stops_owned_child_without_publication(self):
        self.guest.verify_and_grow.side_effect = RuntimeError('Unsafe decrypted capacity proof')
        with self.assertRaisesRegex(RuntimeError, 'Unsafe decrypted'):
            self.prepare()
        self.assertIn(('emu', 'kill'), self.adb_calls)
        self.process.wait.assert_called_once_with(timeout=60)
        self.assert_no_publish()

    def test_cleanup_identity_timeout_terminates_owned_child_before_publication(self):
        normal = self.mocks['adb'].side_effect
        identities = 0
        def timeout_on_cleanup(config, *arguments, **kwargs):
            nonlocal identities
            if arguments == ('emu', 'avd', 'name'):
                identities += 1
                if identities == 2:
                    raise subprocess.TimeoutExpired('adb emu avd name', 15)
            return normal(config, *arguments, **kwargs)
        self.mocks['adb'].side_effect = timeout_on_cleanup
        self.assertEqual(self.prepare(), {'changed': True, 'filesystem_bytes': 32 * GIB})
        self.process.terminate.assert_called_once_with()
        self.process.wait.assert_called_once_with(timeout=60)
        self.assertNotIn(('emu', 'kill'), self.adb_calls)
        self.assertEqual(self.events[-4:], ['terminate-own-pid', 'wait-owned-exit', 'idle', 'publish'])

    def test_failed_console_stop_falls_back_to_owned_pid_before_publication(self):
        normal = self.mocks['adb'].side_effect
        def refused_stop(config, *arguments, **kwargs):
            if arguments == ('emu', 'kill'):
                self.adb_calls.append(arguments)
                return subprocess.CompletedProcess(arguments, 1, '', 'console unavailable')
            return normal(config, *arguments, **kwargs)
        self.mocks['adb'].side_effect = refused_stop
        self.prepare()
        self.process.terminate.assert_called_once_with()
        self.assertEqual(self.events[-4:], ['terminate-own-pid', 'wait-owned-exit', 'idle', 'publish'])

    def test_post_exit_port_collision_prevents_proof_publication(self):
        self.mocks['idle'].side_effect = [None, RuntimeError('Port now belongs to another AVD')]
        with self.assertRaisesRegex(RuntimeError, 'another AVD'):
            self.prepare()
        self.process.wait.assert_called_once_with(timeout=60)
        self.assert_no_publish()

    def test_guest_sync_failure_stops_owned_child_without_publication(self):
        normal = self.mocks['adb'].side_effect
        def failed_sync(config, *arguments, **kwargs):
            if arguments == ('shell', 'su -W -c sync'):
                raise subprocess.CalledProcessError(1, 'owned guest sync')
            return normal(config, *arguments, **kwargs)
        self.mocks['adb'].side_effect = failed_sync
        with self.assertRaises(subprocess.CalledProcessError):
            self.prepare()
        self.assertIn(('emu', 'kill'), self.adb_calls)
        self.process.wait.assert_called_once_with(timeout=60)
        self.assert_no_publish()

    def test_offline_proof_failure_leaves_recovery_to_the_caller(self):
        self.mocks['resize'].side_effect = RuntimeError('Unverified disk ownership')
        with patch.object(manage, 'restore') as restore:
            with self.assertRaisesRegex(RuntimeError, 'disk ownership'):
                self.prepare()
        restore.assert_not_called()
        self.mocks['popen'].assert_not_called()
        self.mocks['adb'].assert_not_called()
        self.assert_no_publish()

    def test_child_wait_timeout_blocks_capacity_publication(self):
        self.process.wait.side_effect = subprocess.TimeoutExpired('owned emulator', 60)
        with self.assertRaisesRegex(RuntimeError, 'still running'):
            self.prepare()
        self.assertIn(('emu', 'kill'), self.adb_calls)
        self.assert_no_publish()


if __name__ == '__main__':
    unittest.main()
