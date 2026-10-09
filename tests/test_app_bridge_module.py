"""Exercise exact-workload packages and autonomous lifecycle in a fake guest."""
import copy
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'scripts'))
import app_bridge_module as bridge


def digest(data):
    return hashlib.sha256(data).hexdigest()


BUSYBOX = r'''#!/usr/bin/env python3
import fcntl, hashlib, json, os, pathlib, stat, subprocess, sys
root = pathlib.Path(os.environ['BRIDGE_TEST_ROOT'])
args = sys.argv[1:]; command, args = args[0], args[1:]
metadata_path = root / 'metadata.json'
metadata = json.loads(metadata_path.read_text())
mount_path = root / 'mounts.json'
mounts = json.loads(mount_path.read_text())
pid = os.environ.get('BRIDGE_TEST_PID', '1')
def translate(value):
    if value in mounts.get(pid, {}): return mounts[pid][value]
    if value == '/data' or value.startswith(('/data/', '/product/', '/system/')):
        return str(root / value.lstrip('/'))
    return value
if command == 'nsenter':
    pid = args[args.index('-t')+1]; os.environ['BRIDGE_TEST_PID'] = pid
    args = args[args.index('--')+1:]
    if len(args) > 1 and args[0].endswith('/busybox') and args[1] in ('mount','umount'):
        os.environ['BRIDGE_TEST_RAW_TARGET'] = args[-1]
    if args[0] == '/system/bin/ls':
        value = pathlib.Path(translate(args[-1])).stat()
        attrs = metadata.get(f'{value.st_dev}:{value.st_ino}', {})
        print(attrs.get('context','u:object_r:apk_data_file:s0') + ' ' + args[-1]); sys.exit(0)
    args = [translate(value) for value in args]
    os.execvp(args[0], args)
if command == 'sha256sum':
    if args[0] == '-c':
        for line in pathlib.Path(args[1]).read_text().splitlines():
            expected, name = line.split(None, 1)
            if digest := hashlib.sha256(pathlib.Path(name.strip()).read_bytes()).hexdigest():
                if digest != expected: sys.exit(1)
        sys.exit(0)
    with (root / 'hash-trace').open('a') as output: output.write(args[0] + '\n')
    try: print(hashlib.sha256(pathlib.Path(args[0]).read_bytes()).hexdigest() + '  ' + args[0])
    except OSError: sys.exit(1)
    sys.exit(0)
if command == 'stat' and args[0] == '-c':
    try:
        value = pathlib.Path(args[2]).stat()
        attrs = metadata.get(f'{value.st_dev}:{value.st_ino}', {})
        form = args[1]
        owner = attrs.get('owner', '0:0')
        print(form.replace('%u:%g', owner).replace('%u', owner.split(':')[0]).replace('%g',owner.split(':')[1])
              .replace('%a',format(stat.S_IMODE(value.st_mode),'o')).replace('%h',str(value.st_nlink))
              .replace('%s',str(value.st_size)).replace('%i',str(value.st_ino))
              .replace('%Y',str(int(value.st_mtime))).replace('%d',str(value.st_dev)))
    except OSError: sys.exit(1)
    sys.exit(0)
if command == 'flock':
    try: fcntl.flock(int(args[-1]), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError: sys.exit(1)
    sys.exit(0)
if command in ('mount','umount'):
    target = os.environ['BRIDGE_TEST_RAW_TARGET']
    failure = root / ('fail-unmount' if command == 'umount' else 'fail-remount')
    if (command == 'umount' or args[1] != 'bind') and failure.exists(): sys.exit(1)
    if command == 'mount' and args[1] == 'bind':
        if (root / 'fail-mount').exists() and target.endswith((root / 'fail-mount').read_text()):
            sys.exit(1)
        mounts.setdefault(pid,{})[target] = args[-2]
        if (root / 'load-after-bind').exists():
            (root / 'proc' / pid / 'maps').write_text(f'0-1 r--p 0 1:2 12 {target}\n')
    elif command == 'umount':
        mounts.setdefault(pid,{}).pop(target,None)
    mount_path.write_text(json.dumps(mounts))
    lines = [f'10 1 0:1 {source} {target} ro,nosuid,nodev - ext4 data rw\n'
             for target, source in mounts.get(pid,{}).items()]
    (root / 'proc' / pid / 'mountinfo').write_text(''.join(lines))
    with (root / 'mount-trace').open('a') as output: output.write(command + ' ' + pid + ' ' + target + '\n')
    sys.exit(0)
os.execvp(command,[command]+args)
'''


class AppBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.module = self.root / 'module'
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.bb = self.bin / 'busybox'
        self.bb.write_text(BUSYBOX)
        self.bb.chmod(0o755)
        for name, body in {
            'id': 'echo 0',
            'getprop': 'case "$1" in ro.boot.hardware) echo ranchu;; '
                       'ro.mi.os.version.incremental) echo OS4.999.0.TEST;; sys.boot_completed) echo 1;; esac',
            'sleep': 'exit 0',
            'pm': 'cat "$BRIDGE_TEST_ROOT/pm-path"',
            'pidof': 'case "$1" in zygote64) cat "$BRIDGE_TEST_ROOT/pids";; '
                     '*) cat "$BRIDGE_TEST_ROOT/app-pids";; esac',
            'am': 'echo "$*" >> "$BRIDGE_TEST_ROOT/app-stop-trace"',
        }.items():
            path = self.bin / name
            path.write_text('#!/bin/sh\n' + body + '\n')
            path.chmod(0o755)
        for name, attribute in (('chcon', 'context'), ('chown', 'owner')):
            path = self.bin / name
            path.write_text('#!' + sys.executable + '\n' + '''
import json, os, pathlib, sys
path = pathlib.Path(os.environ['BRIDGE_TEST_ROOT']) / 'metadata.json'
values = json.loads(path.read_text()); target = pathlib.Path(sys.argv[2]).stat()
key = f'{target.st_dev}:{target.st_ino}'
values.setdefault(key,{})[ATTRIBUTE] = sys.argv[1]
path.write_text(json.dumps(values))
'''.replace('ATTRIBUTE', repr(attribute)))
            path.chmod(0o755)
        for filename, body in (('metadata.json', '{}'), ('mounts.json', '{}'), ('pids', ''), ('app-pids', '')):
            (self.root / filename).write_text(body)
        self.proc(1)
        self.apk_data = b'known signed APK fixture'
        self.before = b'original native fixture'
        self.after = b'patched native fixture'
        self.apk = '/data/app/~~fixture/com.fixture.app-A/base.apk'
        self.native = self.apk.rsplit('/', 1)[0] + '/lib/arm64'
        self.install_app(self.apk)
        (self.root / 'pm-path').write_text('package:' + self.apk + '\n')
        self.payload = self.root / 'libtest.so'
        self.payload.write_bytes(self.after)
        self.options = {'module_id': 'hyperos_avd_fixture_bridge', 'name': 'Verified fixture bridge',
                        'description': 'Exact fixture workload', 'package': 'com.fixture.app',
                        'profiles': [{'id': 'fixture', 'apk_sha256': digest(self.apk_data),
                                      'libraries': [{'name': 'libtest.so', 'before': digest(self.before),
                                                     'after': digest(self.after), 'placeholder': False}]}],
                        'payloads': {'libtest.so': self.payload}}
        self.publish()
        self.env = {**os.environ, 'PATH': str(self.bin) + os.pathsep + os.environ['PATH'],
                    'BRIDGE_TEST_ROOT': str(self.root), 'HYPEROS_BRIDGE_BB': str(self.bb),
                    'HYPEROS_BRIDGE_PROC': str(self.root / 'proc')}

    def proc(self, pid):
        directory = self.root / 'proc' / str(pid)
        (directory / 'ns').mkdir(parents=True)
        (directory / 'ns/mnt').touch()
        (directory / 'stat').write_text(' '.join(['0'] * 21 + [str(pid * 7)]))
        (directory / 'mountinfo').write_text('')
        (directory / 'maps').write_text('')

    def install_app(self, apk):
        path = self.root / apk.lstrip('/')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.apk_data)
        native = path.parent / 'lib/arm64'
        native.mkdir(parents=True, exist_ok=True)
        library = native / 'libtest.so'
        library.write_bytes(self.before)
        library.chmod(0o640)
        self.metadata(library, owner='1000:1000', context='u:object_r:apk_data_file:s0')

    def metadata(self, path, **attributes):
        file = self.root / 'metadata.json'
        values = json.loads(file.read_text())
        state = path.stat()
        values.setdefault(f'{state.st_dev}:{state.st_ino}', {}).update(attributes)
        file.write_text(json.dumps(values))

    def publish(self):
        self.files = bridge.module_files(**self.options)
        self.module.mkdir(exist_ok=True)
        for filename, body in self.files.items():
            path = self.module / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)

    def shell(self, body, *, check=True):
        source = ('MODDIR=' + shlex.quote(str(self.module)) + '\n'
                  '. "$MODDIR/runtime.sh"\n' + body)
        return subprocess.run(['sh', '-c', source], env=self.env, text=True,
                              capture_output=True, check=check, timeout=30)

    def mounts(self):
        return json.loads((self.root / 'mounts.json').read_text())

    def test_package_is_deterministic_and_current_recipe_authenticates_without_payload_files(self):
        first, second = self.root / 'first.zip', self.root / 'second.zip'
        bridge.package_module(first, **self.options)
        bridge.package_module(second, **self.options)
        self.assertEqual(first.read_bytes(), second.read_bytes())
        expected = bridge.module_files(**{**self.options, 'payloads': None})
        self.assertEqual(expected['manifest.json'], self.files['manifest.json'])
        self.assertEqual(expected['SHA256SUMS'], self.files['SHA256SUMS'])
        with zipfile.ZipFile(first) as archive:
            self.assertIn('uninstall.sh', archive.namelist())
            self.assertNotIn('base.apk', archive.namelist())
        self.shell('bridge_assets')

    def test_same_signed_workload_reinstall_is_recovered_without_host_rerun(self):
        second = '/data/app/~~other/com.fixture.app-B/base.apk'
        self.install_app(second)
        command = ('bridge_assets && bridge_once || exit 1\n'
                   'printf %s ' + shlex.quote('package:' + second + '\n') +
                   ' > "$BRIDGE_TEST_ROOT/pm-path"\nbridge_once')
        self.shell(command)
        self.assertEqual(set(self.mounts()['1']), {second.rsplit('/', 1)[0] + '/lib/arm64/libtest.so'})
        self.assertEqual((self.root / self.apk.lstrip('/')).read_bytes(), self.apk_data)
        self.assertEqual((self.root / second.lstrip('/')).read_bytes(), self.apk_data)

    def test_unchanged_token_avoids_native_and_apk_hashes_but_new_namespace_reconciles(self):
        self.proc(66)
        self.shell('bridge_assets && bridge_once && : > "$BRIDGE_TEST_ROOT/hash-trace"\n'
                   'bridge_once && [ ! -s "$BRIDGE_TEST_ROOT/hash-trace" ]\n'
                   'echo 66 > "$BRIDGE_TEST_ROOT/pids"\nbridge_once')
        target = self.native + '/libtest.so'
        self.assertIn(target, self.mounts()['66'])
        self.assertIn(target, self.mounts()['1'])

    def test_uninstall_and_unknown_workload_cleanup_only_owned_mounts(self):
        self.shell('bridge_assets && bridge_once\n: > "$BRIDGE_TEST_ROOT/pm-path"\nbridge_once')
        self.assertEqual(self.mounts()['1'], {})
        self.shell('bridge_assets && bridge_once')
        (self.root / 'pm-path').write_text('package:' + self.apk + '\n')
        (self.root / self.apk.lstrip('/')).write_bytes(b'unknown APK update')
        self.shell('bridge_assets && bridge_once')
        self.assertEqual(self.mounts()['1'], {})
        self.assertIn('Unsupported', (self.module / 'runtime.log').read_text())

    def test_unknown_native_library_fails_before_any_bind_or_payload_change(self):
        target = self.root / (self.native + '/libtest.so').lstrip('/')
        target.write_bytes(b'foreign native library')
        identity = (target.stat().st_ino, target.read_bytes())
        result = self.shell('bridge_assets && bridge_once', check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.mounts(), {})
        self.assertEqual((target.stat().st_ino, target.read_bytes()), identity)

    def test_disable_remove_and_single_worker_lease_are_respected(self):
        for flag in ('disable', 'remove'):
            (self.module / flag).touch()
            self.assertNotEqual(self.shell('bridge_assets && bridge_once', check=False).returncode, 0)
            (self.module / flag).unlink()
        self.shell('bridge_assets && bridge_lock\n'
                   '( bridge_lock ) && exit 1\nexit 0')
        # The kernel releases the file lease after normal exit or a crash.
        self.shell('bridge_assets && bridge_lock')
        self.assertEqual(self.mounts(), {})

    def test_metadata_is_preserved_in_immutable_cache_and_foreign_bind_is_not_peeled(self):
        self.shell('bridge_assets && bridge_once')
        target = self.native + '/libtest.so'
        cached = Path(self.mounts()['1'][target])
        self.assertEqual(cached.stat().st_mode & 0o777, 0o640)
        value = cached.stat()
        metadata = json.loads((self.root / 'metadata.json').read_text())[f'{value.st_dev}:{value.st_ino}']
        self.assertEqual(metadata['owner'], '1000:1000')
        self.assertEqual(metadata['context'], 'u:object_r:apk_data_file:s0')
        original = self.root / target.lstrip('/')
        self.assertEqual(original.read_bytes(), self.before)
        mountinfo = self.root / 'proc/1/mountinfo'
        mountinfo.write_text(f'10 1 0:1 /foreign/module/cache/x.so {target} ro - ext4 data rw\n')
        self.shell('bridge_assets && bridge_cleanup')
        self.assertIn(target, self.mounts()['1'])

    def test_all_namespaces_and_targets_preflight_before_transaction_and_rollback(self):
        helper = self.root / 'libhelper.so'
        helper.write_bytes(b'known helper')
        self.options['payloads']['libhelper.so'] = helper
        self.options['profiles'][0]['libraries'].append({'name': 'libhelper.so', 'before': bridge.EMPTY,
                                                        'after': digest(helper.read_bytes()), 'placeholder': True})
        self.publish()
        (self.root / 'fail-mount').write_text('libhelper.so')
        self.shell('bridge_assets && bridge_once', check=False)
        self.assertEqual(self.mounts()['1'], {})
        self.assertEqual((self.root / (self.native + '/libtest.so').lstrip('/')).read_bytes(), self.before)
        self.assertFalse((self.root / (self.native + '/libhelper.so').lstrip('/')).exists())
        (self.root / 'fail-mount').unlink()
        self.shell('bridge_assets && bridge_once && bridge_cleanup')
        self.assertFalse((self.root / (self.native + '/libhelper.so').lstrip('/')).exists())
        self.assertEqual(self.mounts()['1'], {})

    def test_foreign_cover_preserves_owned_placeholder_receipt_until_it_is_released(self):
        helper = self.root / 'libhelper.so'
        helper.write_bytes(b'known helper')
        self.options['payloads']['libhelper.so'] = helper
        self.options['profiles'][0]['libraries'].append(
            {'name': 'libhelper.so', 'before': bridge.EMPTY,
             'after': digest(helper.read_bytes()), 'placeholder': True})
        self.publish()
        self.shell('bridge_assets && bridge_once')
        target = self.native + '/libhelper.so'
        installed = self.root / target.lstrip('/')
        identity = installed.stat().st_ino
        foreign = self.root / 'foreign-cover.so'
        foreign.write_bytes(b'foreign library cover')
        mounts = self.mounts()
        mounts['1'][target] = str(foreign)
        (self.root / 'mounts.json').write_text(json.dumps(mounts))
        (self.root / 'proc/1/mountinfo').write_text(
            ''.join(f'10 1 0:1 {source} {name} ro - ext4 data rw\n'
                    for name, source in mounts['1'].items()))
        self.shell('bridge_assets && bridge_cleanup')
        self.assertEqual(self.mounts()['1'][target], str(foreign))
        self.assertIn(str(identity), (self.module / 'state/owned.tsv').read_text())
        self.assertEqual(installed.read_bytes(), b'')
        self.assertEqual(foreign.read_bytes(), b'foreign library cover')
        (self.root / 'mounts.json').write_text(json.dumps({'1': {}}))
        (self.root / 'proc/1/mountinfo').write_text('')
        self.shell('bridge_assets && bridge_cleanup')
        self.assertFalse(installed.exists())
        self.assertNotIn(target, (self.module / 'state/owned.tsv').read_text())
        self.assertEqual(foreign.read_bytes(), b'foreign library cover')

    def test_failed_verification_keeps_busy_or_loaded_bind_owned_until_safe_recovery(self):
        for blocker in ('fail-unmount', 'load-after-bind'):
            with self.subTest(blocker=blocker):
                (self.root / 'fail-remount').touch()
                (self.root / blocker).touch()
                result = self.shell('bridge_assets && bridge_once', check=False)
                self.assertNotEqual(result.returncode, 0)
                target = self.native + '/libtest.so'
                self.assertIn(target, self.mounts()['1'])
                self.assertIn(target, (self.module / 'state/owned.tsv').read_text())
                self.assertTrue(list((self.module / 'state').glob('transaction.*')))
                self.assertNotEqual(self.shell('bridge_assets && bridge_once', check=False).returncode, 0)
                # A crashed older worker's incomplete stage must not prevent
                # a fresh exclusive stage from completing automatic recovery.
                stale = next((self.module / 'state').glob('transaction.*'))
                old_stage = Path(str(stale) + '.next')
                old_stage.write_bytes(b'incomplete private rollback stage')
                (self.root / blocker).unlink()
                (self.root / 'fail-remount').unlink()
                (self.root / 'proc/1/maps').write_text('')
                self.shell('bridge_assets && bridge_once && bridge_cleanup')
                self.assertEqual(self.mounts()['1'], {})
                self.assertEqual(old_stage.read_bytes(), b'incomplete private rollback stage')
                old_stage.unlink()
                self.assertFalse(list((self.module / 'state').glob('transaction.*')))

    def test_legacy_direct_helper_is_restored_atomically_when_later_mount_fails(self):
        old = b'known obsolete helper'
        for name, content, legacy in [('libhelper.so', b'known modern helper', [digest(old)]),
                                       ('liblast.so', b'last helper', [])]:
            payload = self.root / name
            payload.write_bytes(content)
            self.options['payloads'][name] = payload
            self.options['profiles'][0]['libraries'].append(
                {'name': name, 'before': bridge.EMPTY, 'after': digest(content),
                 'placeholder': True, 'legacy_direct': legacy})
        self.publish()
        installed = self.root / (self.native + '/libhelper.so').lstrip('/')
        installed.write_bytes(old)
        inode = installed.stat().st_ino
        (self.root / 'fail-mount').write_text('liblast.so')
        with installed.open('rb') as original_mapping:
            self.assertNotEqual(self.shell('bridge_assets && bridge_once', check=False).returncode, 0)
            self.assertEqual(original_mapping.read(), old)
        self.assertEqual(installed.stat().st_ino, inode)
        self.assertEqual(installed.read_bytes(), old)
        self.assertEqual(self.mounts()['1'], {})
        self.assertFalse((self.root / (self.native + '/liblast.so').lstrip('/')).exists())
        self.assertFalse(list((self.module / 'state').glob('transaction.*')))
        (self.root / 'fail-mount').unlink()
        self.shell('bridge_assets && bridge_once && bridge_cleanup')
        self.assertFalse(installed.exists())
        self.assertFalse((self.root / (self.native + '/liblast.so').lstrip('/')).exists())
        self.assertEqual(self.mounts()['1'], {})

    def test_extracted_caller_dependency_is_pinned_before_any_binding(self):
        self.options['profiles'][0]['native_libraries'] = {'libcaller.so': digest(b'known ABI caller')}
        self.publish()
        (self.root / (self.native + '/libcaller.so').lstrip('/')).write_bytes(b'unknown ABI caller')
        self.assertNotEqual(self.shell('bridge_assets && bridge_once', check=False).returncode, 0)
        self.assertEqual(self.mounts(), {})

    def test_worker_lease_refuses_hardlinks_and_nonempty_foreign_state(self):
        self.shell('bridge_assets')
        lock = self.module / 'state/worker.lock'
        lock.write_bytes(b'foreign state')
        self.assertNotEqual(self.shell('bridge_lock', check=False).returncode, 0)
        self.assertEqual(lock.read_bytes(), b'foreign state')
        lock.write_bytes(b'')
        os.link(lock, self.root / 'foreign-lock')
        self.assertNotEqual(self.shell('bridge_lock', check=False).returncode, 0)

    def test_aliased_and_hardlinked_module_assets_are_rejected(self):
        for filename in ('runtime.sh', 'manifest.json', 'package.txt', 'libraries.tsv', 'payloads/libtest.so'):
            with self.subTest(filename=filename):
                path = self.module / filename
                saved = path.read_bytes()
                alias = self.root / 'asset-alias'
                os.link(path, alias)
                self.assertNotEqual(self.shell('bridge_assets', check=False).returncode, 0)
                alias.unlink()
                path.unlink()
                alias.write_bytes(saved)
                path.symlink_to(alias)
                self.assertNotEqual(self.shell('bridge_assets', check=False).returncode, 0)
                path.unlink(); alias.unlink(); path.write_bytes(saved)

    def test_foreign_second_namespace_is_rejected_before_first_namespace_bind(self):
        self.proc(66)
        (self.root / 'pids').write_text('66')
        target = self.native + '/libtest.so'
        foreign = self.root / 'foreign-native.so'
        foreign.write_bytes(b'foreign namespace library')
        (self.root / 'mounts.json').write_text(json.dumps({'66': {target: str(foreign)}}))
        result = self.shell('bridge_assets && bridge_once', check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('1', self.mounts())
        self.assertEqual(self.mounts()['66'][target], str(foreign))
        self.assertFalse((self.root / 'mount-trace').exists())

    def test_loaded_old_namespace_is_kept_until_it_exits(self):
        self.shell('bridge_assets && bridge_once')
        target = self.native + '/libtest.so'
        (self.root / 'proc/1/maps').write_text(f'0-1 r--p 0 1:2 12 {target}\n')
        self.shell('bridge_assets && bridge_cleanup')
        self.assertIn(target, self.mounts()['1'])
        self.assertIn(target, (self.module / 'state/owned.tsv').read_text())
        (self.root / 'proc/1/maps').write_text('')
        self.shell('bridge_assets && bridge_cleanup')
        self.assertEqual(self.mounts()['1'], {})

    def test_legacy_direct_helper_uses_new_placeholder_inode_without_truncating_old(self):
        old, modern = b'obsolete project helper', b'current project helper'
        helper = self.root / 'libhelper.so'
        helper.write_bytes(modern)
        self.options['payloads']['libhelper.so'] = helper
        self.options['profiles'][0]['libraries'].append({'name': 'libhelper.so', 'before': bridge.EMPTY,
                                                        'after': digest(modern), 'placeholder': True,
                                                        'legacy_direct': [digest(old), digest(modern)]})
        self.publish()
        installed = self.root / (self.native + '/libhelper.so').lstrip('/')
        installed.write_bytes(old)
        original_inode = installed.stat().st_ino
        with installed.open('rb') as mapping:
            self.shell('bridge_assets && bridge_once')
            self.assertEqual(mapping.read(), old)
        self.assertNotEqual(installed.stat().st_ino, original_inode)
        self.assertEqual(installed.read_bytes(), b'')
        self.assertIn(self.native + '/libhelper.so', self.mounts()['1'])
        self.shell('bridge_assets && bridge_cleanup')
        self.assertFalse(installed.exists())

    def test_profile_and_payload_guards_reject_aliases_and_unknown_libraries(self):
        for field, value in (('apk_sha256', 'unknown'), ('factory_apk', '/data/app/../escape'),
                             ('id', 'unsafe|id')):
            changed = copy.deepcopy(self.options)
            changed['profiles'][0][field] = value
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                bridge.module_files(**changed)
        foreign = self.root / 'foreign-payload'
        os.link(self.payload, foreign)
        with self.assertRaises(RuntimeError):
            bridge.module_files(**self.options)
        foreign.unlink()
        changed = copy.deepcopy(self.options)
        changed['profiles'][0]['libraries'][0]['legacy_direct'] = ['0' * 64]
        with self.assertRaises(RuntimeError):
            bridge.module_files(**changed)

    def test_host_install_uses_ksud_staging_without_live_mount_or_activation(self):
        calls = []
        def root(config, command):
            calls.append(command)
            return 'ranchu\nOS4.999.0.TEST' if command.startswith('getprop') else ''
        with patch.object(bridge, 'root', side_effect=root), \
                patch.object(bridge, '_inspect', return_value=None), patch.object(bridge, 'adb') as adb:
            result = bridge.install({}, **self.options)
        self.assertTrue(result['reboot_required'])
        self.assertEqual(adb.call_count, 1)
        self.assertTrue(any('/data/adb/ksud module install ' in command for command in calls))
        for command in calls:
            for forbidden in ('mount -o', 'service.sh', 'am force-stop', 'reboot', 'mv /data/adb/modules'):
                self.assertNotIn(forbidden, command)

    def test_host_flags_and_unknown_local_manifest_are_preserved_before_staging(self):
        active = {'directory': '/data/adb/modules/' + self.options['module_id'],
                  'revision': 2, 'flags': ['disable'], 'properties': 'owned fixture'}
        with patch.object(bridge, 'root', return_value='ranchu\nOS4.999.0.TEST'), \
                patch.object(bridge, '_inspect', side_effect=[active, None]), \
                patch.object(bridge, 'adb') as adb:
            self.assertTrue(bridge.install({}, **self.options)['disabled'])
            adb.assert_not_called()
        active['flags'] = []
        saved = json.loads(self.files['manifest.json'])
        saved['files_sha256']['runtime.sh'] = '0' * 64
        with patch.object(bridge, 'root', return_value='ranchu\nOS4.999.0.TEST'), \
                patch.object(bridge, '_inspect', side_effect=[active, None]), \
                patch.object(bridge, '_saved', return_value=saved), \
                patch.object(bridge, '_verify_assets') as verify, patch.object(bridge, 'adb') as adb:
            with self.assertRaisesRegex(RuntimeError, 'Unknown or changed'):
                bridge.install({}, **self.options)
            verify.assert_not_called()
            adb.assert_not_called()

    def test_host_remove_authenticates_canonical_recipe_before_kernel_su_uninstall(self):
        active = {'directory': '/data/adb/modules/' + self.options['module_id'],
                  'revision': 2, 'flags': [], 'properties': 'owned fixture'}
        options = {key: value for key, value in self.options.items() if key not in ('module_id', 'payloads')}
        with patch.object(bridge, '_inspect', side_effect=[active, None]), \
                patch.object(bridge, '_saved', return_value=json.loads(self.files['manifest.json'])), \
                patch.object(bridge, '_verify_assets') as verify, patch.object(bridge, 'root') as root:
            self.assertTrue(bridge.remove({}, self.options['module_id'], **options)['removed'])
            verify.assert_called_once()
            root.assert_called_once_with({}, '/data/adb/ksud module uninstall ' + self.options['module_id'])
        changed = json.loads(self.files['manifest.json'])
        changed['profiles'][0]['apk_sha256'] = '0' * 64
        with patch.object(bridge, '_inspect', side_effect=[active, None]), \
                patch.object(bridge, '_saved', return_value=changed), patch.object(bridge, 'root') as root:
            with self.assertRaisesRegex(RuntimeError, 'Unknown or modified'):
                bridge.remove({}, self.options['module_id'], **options)
            root.assert_not_called()


if __name__ == '__main__':
    unittest.main()
