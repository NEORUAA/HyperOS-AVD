"""Verify rear wake installation and startup without operating an emulator."""
from contextlib import ExitStack
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

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'scripts'))
import apply_rear_display_fix as fix
import rear_display_config as rear
from patch_goldfish_sync import MANIFEST as SYNC
from patch_rear_display import MANIFEST as COMPOSER

DISPLAY = ('DisplayInfo{"Rear", displayId 1, displayGroupId 1, '
           'uniqueId "local:4619827551948147201", state OFF}')
JAR = b'reviewed DEX fixture for installation guards'


def build_fixture(wake):
    displays = []
    for original, physical, geometry in zip(rear.SOURCE_IDS, rear.PHYSICAL_IDS,
                                            ((1120, 2436, 480), (912, 596, 450))):
        source = f'product/etc/displayconfig/display_id_{original}.xml'
        displays.append({'source_physical_id': original, 'physical_id': physical,
                         'unique_id': 'local:' + str(physical),
                         'config': f'/product/etc/displayconfig/display_id_{physical}.xml',
                         'config_sha256': rear.SOURCE_SHA256[source],
                         'width': geometry[0], 'height': geometry[1], 'density': geometry[2]})
    marker = {'schema': 1, 'hyperos': rear.VERSION, 'archive_sha256': rear.ARCHIVE_SHA256,
              'source_sha256': dict(rear.SOURCE_SHA256), 'mapping_sha256': rear.MAPPING_SHA256,
              'overlay': rear.OVERLAY, 'overlay_path': '/' + rear.OVERLAY_PATH,
              'overlay_sha256': hashlib.sha256(b'ID-only RRO fixture').hexdigest(),
              'overlay_priority': 1001, 'source_overlay_priority': 1000,
              'requires_runtime_resource_verification': True,
              'overridden_resources': ['config_displayUniqueIdArray'], 'displays': displays,
              'rear_safe_inset_left': 296, 'rear_corner_radius': 106}
    return {'source': 'official-hongkong-ota', 'hyperos': rear.VERSION,
            'archive_sha256': rear.ARCHIVE_SHA256, 'rear_display': marker,
            'rear_display_composer_fix': dict(COMPOSER), 'goldfish_sync_fix': dict(SYNC),
            'rear_display_wake_fix': copy.deepcopy(wake)}


def module_manifest(build):
    return {'schema': 1, 'revision': 2, 'module_id': fix.MODULE_ID,
            'firmware': {'source': build['source'], 'hyperos': rear.VERSION,
                         'incremental': 'OS' + rear.VERSION, 'archive_sha256': rear.ARCHIVE_SHA256,
                         'hardware': 'ranchu', 'device': 'hongkong'},
            'marker': {'path': '/' + rear.MARKER_PATH,
                       'sha256': fix.digest(fix.encode(build['rear_display']))},
            'wake': copy.deepcopy(build['rear_display_wake_fix'])}


class Guest:
    """Small read/write command mock; unsupported commands fail the test."""
    def __init__(self, build, name):
        wake = build['rear_display_wake_fix']
        self.files = {'/' + rear.MARKER_PATH: fix.encode(build['rear_display']),
                      wake['jar_path']: JAR, wake['script_path']: fix.LAUNCHER}
        self.properties = ['ranchu', 'hongkong', 'OS' + rear.VERSION, name]
        self.display = DISPLAY
        self.module = ''
        self.flags = []
        self.occupied = False
        self.commands = []
        self.mutations = []
        self.pushes = []
        self.starts = 0
        self.ready = True
        self.confirmations = 0

    def existing(self, manifest):
        self.module = 'directory'
        self.files.update({fix.MODULE + '/manifest.json': fix.encode(manifest),
                           fix.MODULE + '/module.prop': fix.MODULE_PROP.encode(),
                           fix.MODULE + '/service.sh': fix.boot_script(manifest).encode(),
                           fix.MODULE + '/skip_mount': b''})

    def root(self, config, command):
        self.commands.append(command)
        code = command.removeprefix('set -e\n')
        lines = code.splitlines()
        if code.startswith('getprop '):
            return '\n'.join(self.properties)
        if code.startswith('sha256sum '):
            return '\n'.join(f'{fix.digest(self.files[path])}  {path}'
                             for path in shlex.split(code)[1:])
        if code == 'dumpsys display':
            return self.display
        if code.startswith('if [ -L ' + fix.MODULE + ' ];'):
            return self.module
        if code.startswith('cat '):
            return self.files[shlex.split(code)[1]].decode().strip()
        if code.startswith('for flag in disable remove;'):
            return '\n'.join(self.flags)
        if code.startswith('if [ -e ' + fix.MODULE + '/disable ]'):
            if not self.flags:
                self.starts += 1
            return ''
        if code.startswith('MODDIR='):
            self.confirmations += 1
            if not self.ready:
                raise RuntimeError('mock readiness failure')
            return 'ready'
        if code.startswith('if [ -e /data/adb/hyperos-rear-display-stage-'):
            return 'occupied' if self.occupied else ''
        if code.startswith('mkdir -p /data/adb/modules\nmkdir '):
            self.mutations.append(command)
            return ''
        if code.startswith('test "$(sha256sum '):
            self.mutations.append(command)
            source = lines[0].split('sha256sum ', 1)[1].split(' |', 1)[0]
            expected = lines[0].rsplit(' = ', 1)[1]
            if fix.digest(self.files[source]) != expected:
                raise RuntimeError('mock push checksum failure')
            _, source, target = shlex.split(lines[1])
            self.files[target] = self.files[source]
            del self.files[shlex.split(lines[3])[1]]
            return ''
        if code.startswith('if [ -e ' + fix.MODULE + ' ]'):
            self.mutations.append(command)
            if self.module:
                raise RuntimeError('mock activation race')
            _, stage, target = shlex.split(lines[1])
            self.files.update({target + path[len(stage):]: data
                               for path, data in list(self.files.items())
                               if path.startswith(stage + '/')})
            self.module = 'directory'
            return ''
        raise AssertionError('Unexpected root command: ' + command)

    def adb(self, config, operation, local, remote, **kwargs):
        assert operation == 'push'
        self.files[remote] = Path(local).read_bytes()
        self.pushes.append((local, remote))
        return subprocess.CompletedProcess([], 0)


class RearInstallTests(unittest.TestCase):
    def setUp(self):
        context = ExitStack()
        self.addCleanup(context.close)
        self.folder = Path(context.enter_context(tempfile.TemporaryDirectory()))
        self.wake = {**fix.EXPECTED_WAKE_MANIFEST, 'jar_sha256': fix.digest(JAR)}
        context.enter_context(patch.object(fix, 'EXPECTED_WAKE_MANIFEST', self.wake))
        context.enter_context(patch.object(fix, 'ROOT', self.folder))
        self.config = {'name': 'A_Renamed_Owned_AVD', 'port': 5584}
        self.build = build_fixture(self.wake)
        self.manifest = module_manifest(self.build)
        self.guest = Guest(self.build, self.config['name'])
        self.official = context.enter_context(patch.object(fix, 'official'))
        self.root = context.enter_context(patch.object(fix, 'root', side_effect=self.guest.root))
        self.adb = context.enter_context(patch.object(fix, 'adb', side_effect=self.guest.adb))

    def unchanged(self):
        self.assertEqual(self.guest.mutations, [])
        self.assertEqual(self.guest.pushes, [])
        self.assertEqual(self.guest.starts, 0)
        self.assertFalse((self.folder / 'work').exists())

    def test_bad_source_profile_receipts_and_types_fail_before_guest_reads(self):
        variants = [{**self.build, key: value} for key, value in (
            ('source', 'official-yingtian-ota'), ('hyperos', '4.0.17.0.XFRCNXM'),
            ('archive_sha256', '0' * 64), ('rear_display', None),
            ('rear_display_composer_fix', {}), ('goldfish_sync_fix', {}),
            ('rear_display_wake_fix', {}))]
        for key, value in (('revision', 2.0), ('schema', True),
                           ('require_down_after_observed_sleep', 1),
                           ('source_sha256', '0' * 64), ('jar_sha256', '0' * 64),
                           ('script_sha256', '0' * 64), ('display_unique_id', 'wrong')):
            variants.append({**self.build, 'rear_display_wake_fix': {**self.wake, key: value}})
        altered = copy.deepcopy(self.build)
        altered['rear_display']['displays'][1]['unique_id'] = 'local:another'
        variants.append(altered)
        for build in variants:
            with self.subTest(build=build), self.assertRaises(RuntimeError):
                fix.install(self.config, build)
        self.official.assert_not_called()
        self.root.assert_not_called()
        self.unchanged()

    def test_changed_host_source_or_launcher_fails_before_guest_reads(self):
        for key, value in (('JAVA_SOURCE', fix.JAVA_SOURCE + '\n// changed\n'),
                           ('LAUNCHER', fix.LAUNCHER + b'# changed\n')):
            with self.subTest(key=key), patch.object(fix, key, value), self.assertRaises(RuntimeError):
                fix.install(self.config, self.build)
        self.root.assert_not_called()
        self.unchanged()

    def test_invalid_name_or_unowned_registered_instance_is_refused(self):
        for name in ('', None, 'name\nother', 'name\0other'):
            with self.subTest(name=name), self.assertRaises(RuntimeError):
                fix.install({**self.config, 'name': name}, self.build)
        self.official.assert_not_called()
        self.official.side_effect = RuntimeError('not the registered owned instance')
        with self.assertRaisesRegex(RuntimeError, 'registered owned'):
            fix.install(self.config, self.build)
        self.root.assert_not_called()
        self.unchanged()

    def test_wrong_live_hardware_product_incremental_or_name_never_writes(self):
        original = list(self.guest.properties)
        for index, value in enumerate(('physical', 'yingtian', 'OS4.0.17.0.XFRCNXM', 'Other_AVD')):
            self.guest.properties = original.copy()
            self.guest.properties[index] = value
            with self.subTest(index=index), self.assertRaisesRegex(RuntimeError, 'guest identity'):
                fix.install(self.config, self.build)
        self.unchanged()

    def test_wrong_baked_marker_jar_or_launcher_hash_never_writes(self):
        original = dict(self.guest.files)
        for path in original:
            self.guest.files = {**original, path: original[path] + b'changed'}
            with self.subTest(path=path), self.assertRaisesRegex(RuntimeError, 'checksum mismatch'):
                fix.install(self.config, self.build)
        self.unchanged()

    def test_display_identifiers_must_match_together_before_writes(self):
        for display in (DISPLAY.replace('displayId 1', 'displayId 10'),
                        DISPLAY.replace('displayGroupId 1', 'displayGroupId 0'),
                        DISPLAY.replace('4619827551948147201', '4619827259835644672'),
                        'DisplayInfo{displayId 1}\nDisplayInfo{displayGroupId 1, '
                        'uniqueId "local:4619827551948147201"}'):
            self.guest.display = display
            with self.subTest(display=display), self.assertRaisesRegex(RuntimeError, 'not ready'):
                fix.install(self.config, self.build)
        self.unchanged()

    def test_fresh_module_has_only_service_assets_no_host_compilation(self):
        self.assertEqual(fix.install(self.config, self.build), self.manifest)
        self.official.assert_called_once_with(self.config)
        self.assertEqual(len(self.guest.pushes), 4)
        self.assertEqual(self.guest.starts, 1)
        self.assertEqual(self.guest.confirmations, 1)
        self.assertEqual(set((self.folder / 'work/rear-display-fix').iterdir()),
                         {self.folder / 'work/rear-display-fix' / name for name in
                          ('manifest.json', 'module.prop', 'service.sh', 'skip_mount')})
        for command in self.guest.mutations:
            self.assertTrue(command.startswith('set -e\n'), command)
        self.assertIn('then exit 1; fi\nmv ', self.guest.mutations[-1])
        self.assertTrue(self.guest.mutations[-1].endswith('\nsync'))
        self.assertEqual(self.guest.files[fix.MODULE + '/skip_mount'], b'')
        self.assertEqual(self.guest.files[self.wake['jar_path']], JAR)

    def test_actual_shell_stops_bad_push_and_refuses_activation_race(self):
        fix.install(self.config, self.build)
        copy_script = next(code for code in self.guest.mutations
                           if code.startswith('set -e\ntest "$(sha256sum '))
        source = copy_script.split('sha256sum ', 1)[1].split(' |', 1)[0]
        target = shlex.split(copy_script.splitlines()[2])[2]
        temporary_source = self.folder / 'bad-upload'
        temporary_target = self.folder / 'must-not-copy'
        temporary_source.write_bytes(b'corrupt upload')
        hash_program = self.folder / 'sha256sum'
        hash_program.write_text('#!' + sys.executable + '\n'
            'import hashlib, pathlib, sys\n'
            'print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())\n')
        hash_program.chmod(0o700)
        code = copy_script.replace(source, str(temporary_source)).replace(target, str(temporary_target))
        result = subprocess.run(['sh', '-c', code], capture_output=True,
            env=dict(os.environ, PATH=str(self.folder) + ':' + os.environ['PATH']))
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(temporary_source.exists())
        self.assertFalse(temporary_target.exists())

        activation = self.guest.mutations[-1]
        stage = shlex.split(activation.splitlines()[2])[1]
        staged = self.folder / 'stage'
        foreign = self.folder / 'foreign-module'
        staged.mkdir()
        foreign.mkdir()
        (staged / 'service.sh').write_text('owned staged file')
        (foreign / 'keep').write_text('foreign module must remain')
        code = activation.replace(stage, str(staged)).replace(fix.MODULE, str(foreign))
        result = subprocess.run(['sh', '-c', code], capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((foreign / 'keep').read_text(), 'foreign module must remain')
        self.assertEqual(set(foreign.iterdir()), {foreign / 'keep'})
        self.assertTrue((staged / 'service.sh').exists())

    def test_exact_module_reuse_survives_avd_rename_without_rewriting(self):
        self.guest.existing(self.manifest)
        original = dict(self.guest.files)
        for name in (self.config['name'], 'Another_Name_After_Install'):
            config = {**self.config, 'name': name}
            self.guest.properties[-1] = name
            self.assertEqual(fix.install(config, self.build), self.manifest)
        self.assertNotIn('avd_name', self.manifest)
        script = self.guest.files[fix.MODULE + '/service.sh'].decode()
        self.assertNotIn('ro.boot.qemu.avd_name', script)
        self.assertNotIn(self.config['name'], script)
        self.assertEqual(self.guest.files, original)
        self.assertEqual(self.guest.starts, 2)
        self.assertEqual(self.guest.confirmations, 2)
        self.assertEqual(self.guest.pushes, [])
        self.assertFalse((self.folder / 'work').exists())

    def test_disabled_and_removed_existing_modules_are_preserved(self):
        self.guest.existing(self.manifest)
        original = dict(self.guest.files)
        for flags in (['disable'], ['remove'], ['disable', 'remove']):
            self.guest.flags = flags
            self.assertEqual(fix.install(self.config, self.build), self.manifest)
        self.assertEqual(self.guest.files, original)
        self.assertEqual(self.guest.confirmations, 0)
        self.unchanged()

    def test_install_never_reports_success_when_started_daemon_is_unready(self):
        self.guest.existing(self.manifest)
        self.guest.ready = False
        with self.assertRaisesRegex(RuntimeError, '10-second readiness check'):
            fix.install(self.config, self.build)
        self.assertEqual(self.guest.starts, 1)
        self.assertEqual(self.guest.confirmations, 1)
        self.assertEqual(self.guest.pushes, [])

    def test_foreign_module_manifest_property_service_and_stage_are_refused(self):
        for kind in ('foreign', 'manifest', 'property', 'service', 'skip_mount'):
            self.guest.existing(self.manifest)
            if kind == 'foreign':
                self.guest.module = 'foreign'
            else:
                key = {'manifest': 'manifest.json', 'property': 'module.prop',
                       'service': 'service.sh', 'skip_mount': 'skip_mount'}[kind]
                self.guest.files[fix.MODULE + '/' + key] += b'foreign'
            with self.subTest(kind=kind), self.assertRaises(RuntimeError):
                fix.install(self.config, self.build)
        self.guest.module = ''
        self.guest.occupied = True
        with self.assertRaisesRegex(RuntimeError, 'staging path'):
            fix.install(self.config, self.build)
        self.unchanged()

    def test_default_build_path_and_explicit_build_hook(self):
        folder = self.folder / 'local'
        folder.mkdir()
        (folder / 'build.json').write_text(json.dumps(self.build))
        self.guest.existing(self.manifest)
        self.assertEqual(fix.install(self.config), self.manifest)
        (folder / 'build.json').unlink()
        self.assertEqual(fix.install(self.config, self.build), self.manifest)


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.module = self.folder / 'module'
        self.module.mkdir()
        self.proc = self.folder / 'proc'
        (self.proc / 'sys/kernel/random').mkdir(parents=True)
        (self.proc / 'sys/kernel/random/boot_id').write_text('fixture-boot\n')
        self.jar = self.folder / 'bridge.jar'
        self.jar.write_bytes(JAR)
        self.launcher = self.folder / 'bridge'
        self.launcher.write_bytes(fix.LAUNCHER)
        self.marker = self.folder / 'marker.json'
        self.marker.write_bytes(b'reviewed marker bytes')
        self.manifest = {'wake': {**fix.EXPECTED_WAKE_MANIFEST,
                                 'jar_path': str(self.jar), 'jar_sha256': fix.digest(JAR),
                                 'script_path': str(self.launcher)},
                         'marker': {'path': str(self.marker),
                                    'sha256': fix.digest(self.marker.read_bytes())}}
        (self.module / 'manifest.json').write_bytes(fix.encode(self.manifest))
        (self.module / 'module.prop').write_text(fix.MODULE_PROP)
        (self.module / 'skip_mount').touch()
        self.tools = self.folder / 'tools'
        self.tools.mkdir()
        self.busybox = self.tools / 'busybox'
        self.busybox.write_text('#!' + sys.executable + '\n'
            'import hashlib, os, pathlib, sys\n'
            'if sys.argv[1] == "sha256sum":\n'
            '    p=sys.argv[2]; print(hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest()+"  "+p)\n'
            'elif sys.argv[1] == "nohup":\n'
            '    raise SystemExit("unexpected daemon launch")\n'
            'else:\n'
            '    os.execvp(sys.argv[1],sys.argv[1:])\n')
        self.busybox.chmod(0o700)
        self.properties = {'sys.boot_completed': '1', 'ro.boot.hardware': 'ranchu',
                           'ro.product.device': 'hongkong',
                           'ro.mi.os.version.incremental': 'OS' + rear.VERSION}
        self.getprop = self.tools / 'getprop'
        self.write_properties()
        (self.tools / 'id').write_text('#!/bin/sh\necho 0\n')
        (self.tools / 'id').chmod(0o700)
        self.environment = dict(os.environ, PATH=str(self.tools) + ':' + os.environ['PATH'])
        self.script = fix.boot_script(self.manifest).replace(
            'BB=/data/adb/ksu/bin/busybox', 'BB=' + shlex.quote(str(self.busybox))).replace(
            'PROC=/proc', 'PROC=' + shlex.quote(str(self.proc)))
        self.service = self.module / 'service.sh'
        self.service.write_text(self.script)

    def write_properties(self):
        self.getprop.write_text('#!/bin/sh\ncase "$1" in\n' +
            ''.join(key + ') echo ' + shlex.quote(value) + ';;\n'
                    for key, value in self.properties.items()) + 'esac\n')
        self.getprop.chmod(0o700)

    def process(self, pid=123, class_name=fix.CLASS, classpath=None, uid=0):
        directory = self.proc / str(pid)
        directory.mkdir(exist_ok=True)
        (directory / 'cmdline').write_bytes(b'app_process\0/system/bin\0' + class_name.encode() + b'\0')
        (directory / 'environ').write_bytes(('CLASSPATH=' + str(classpath or self.jar)).encode() + b'\0')
        (directory / 'status').write_text(f'Name:\tapp_process\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n')

    def run_service(self):
        return subprocess.run(['sh', str(self.service)], env=self.environment,
                              capture_output=True, text=True, timeout=15)

    def test_shell_syntax_and_repeated_owned_startup_never_launch_again(self):
        result = subprocess.run(['sh', '-n', str(self.service)], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.process()
        for _ in range(2):
            result = self.run_service()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((self.module / 'daemon.pid').read_text(), '123\n')
            self.assertFalse((self.module / 'startup.lock').exists())
        self.assertFalse((self.module / 'wake.log').exists())

    def test_shell_hash_alias_cannot_intercept_guard_checks(self):
        # Insert the alias after the function declaration: POSIX shells differ
        # in whether defining a function clears an existing alias. Android
        # mksh's preset hash alias was observed intercepting the old calls.
        collision = self.script.replace('guard() {', 'alias hash=false\nguard() {')
        self.process()
        self.service.write_text(collision)
        result = self.run_service()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.module / 'daemon.pid').read_text(), '123\n')
        # Negative control proves this shell actually expands the alias and
        # would fail the former production helper's checksum guards.
        self.service.write_text(collision.replace('hyperos_file_sha', 'hash'))
        result = self.run_service()
        self.assertNotEqual(result.returncode, 0)

    def test_readiness_receipt_requires_live_owned_process_and_is_bounded(self):
        probe = fix.readiness_script().replace(fix.MODULE, str(self.module)).replace(
            fix.EXPECTED_WAKE_MANIFEST['jar_path'], str(self.jar)).replace(
            'BB=/data/adb/ksu/bin/busybox', 'BB=' + shlex.quote(str(self.busybox))).replace(
            'PROC=/proc', 'PROC=' + shlex.quote(str(self.proc)))
        path = self.folder / 'readiness.sh'
        path.write_text(probe)
        # Remove real delays while executing the production bounded loop.
        sleeps = self.folder / 'sleeps'
        (self.tools / 'sleep').write_text('#!/bin/sh\necho wait >> ' + shlex.quote(str(sleeps)) + '\n')
        (self.tools / 'sleep').chmod(0o700)
        self.process()
        (self.module / 'daemon.pid').write_text('123\n')
        result = subprocess.run(['sh', str(path)], env=self.environment,
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'ready')
        self.assertFalse(sleeps.exists())
        for pid, uid in (('999', 0), ('123', 2000)):
            (self.module / 'daemon.pid').write_text(pid + '\n')
            self.process(uid=uid)
            result = subprocess.run(['sh', str(path)], env=self.environment,
                                    capture_output=True, text=True, timeout=5)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('not ready after 10 seconds', result.stderr)
            self.assertEqual(len(sleeps.read_text().splitlines()), 10)
            sleeps.unlink()
        flag = self.module / 'disable'
        flag.symlink_to(self.folder / 'missing')
        result = subprocess.run(['sh', str(path)], env=self.environment,
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, '')
        self.assertFalse(sleeps.exists())

    def test_ownership_requires_exact_class_classpath_and_root_uid(self):
        definitions = self.script.split('owned() {', 1)[1].split('LOCK=', 1)[0]
        probe = ('PROC=' + shlex.quote(str(self.proc)) + '\nBB=' + shlex.quote(str(self.busybox)) +
                 '\nCLASS=' + shlex.quote(fix.CLASS) + '\nJAR=' + shlex.quote(str(self.jar)) +
                 '\nowned() {' + definitions + '\nowned "$1"\n')
        path = self.folder / 'owned.sh'
        path.write_text(probe)
        for class_name, classpath, uid, accepted in (
                (fix.CLASS, self.jar, 0, True), (fix.CLASS + 'Foreign', self.jar, 0, False),
                (fix.CLASS, self.folder / 'foreign.jar', 0, False),
                (fix.CLASS, self.jar, 2000, False)):
            self.process(class_name=class_name, classpath=classpath, uid=uid)
            result = subprocess.run(['sh', str(path), '123'], env=self.environment,
                                    capture_output=True, timeout=5)
            self.assertEqual(result.returncode == 0, accepted)

    def test_boot_guard_rejects_hashes_and_foreign_firmware_before_writes(self):
        baseline = set(self.module.iterdir())
        original = dict(self.properties)
        for key, value in (('ro.boot.hardware', 'physical'), ('ro.product.device', 'yingtian'),
                           ('ro.mi.os.version.incremental', 'OS4.0.17.0.XFRCNXM')):
            self.properties = {**original, key: value}
            self.write_properties()
            result = self.run_service()
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(set(self.module.iterdir()), baseline)
        self.properties = original
        self.write_properties()
        for path in (self.jar, self.launcher, self.marker, self.module / 'manifest.json',
                     self.module / 'module.prop'):
            data = path.read_bytes()
            path.write_bytes(data + b'changed')
            result = self.run_service()
            path.write_bytes(data)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(set(self.module.iterdir()), baseline)

    def test_disabled_or_removed_service_exits_without_writes(self):
        for name in ('disable', 'remove'):
            flag = self.module / name
            flag.symlink_to(self.folder / 'nonexistent')
            baseline = set(self.module.iterdir())
            self.jar.write_bytes(b'foreign')
            result = self.run_service()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(set(self.module.iterdir()), baseline)
            self.assertTrue(flag.is_symlink())
            flag.unlink()


if __name__ == '__main__':
    unittest.main()
