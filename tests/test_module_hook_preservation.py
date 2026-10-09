"""Verify real hook files through host installer reuse and enable paths."""
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import apply_flutter_fix as flutter
import apply_navigation_fix as navigation
from patch_flutter import PROFILES
from phone_profile import profile


class HookPreservationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.workspace = self.folder / 'workspace'
        (self.workspace / 'local').mkdir(parents=True)
        self.firmware = profile('4.0.18.0.XFRCNXM')
        (self.workspace / 'local/build.json').write_text(json.dumps({
            'source': 'official-hongkong-ota', 'hyperos': self.firmware['hyperos'],
            'archive_sha256': self.firmware['archive_sha256']}))
        self.context = flutter.firmware_context(json.loads(
            (self.workspace / 'local/build.json').read_text()))
        self.commands = []
        self.module = self.folder / 'guest/module'
        self.module.mkdir(parents=True)
        self.pending = self.folder / 'guest/modules_update'
        self.remote = self.folder / 'guest/tmp'
        self.remote.mkdir()
        self.packages = {'com.miui.home': '/product/priv-app/MiuiHome/MiuiHome.apk',
                         'com.miui.weather2': '/data/app/com.miui.weather2/base.apk'}
        self.apk_hashes = {name: hashlib.sha256(name.encode()).hexdigest() for name in self.packages}
        self.module_id = None

    def root(self, config, command, **kwargs):
        self.commands.append(command)
        replies = {'id -u': '0', 'getprop sys.boot_completed': '1',
                   'getprop ro.mi.os.version.incremental': self.firmware['incremental'],
                   'getprop ' + navigation.PROPERTY: 'com.miui.home',
                   'cmd overlay lookup android android:string/config_recentsComponentName': navigation.COMPONENT,
                   'test -f /product/overlay/MiuiHomeLauncherResOverlay.apk': ''}
        if command in replies:
            return replies[command]
        if command.startswith('pm path '):
            return 'package:' + self.packages[command.removeprefix('pm path ')]
        for package, path in self.packages.items():
            if command == 'sha256sum ' + path:
                return self.apk_hashes[package] + '  ' + path
        if command == 'sha256sum ' + flutter.SYSTEM_LIB or command.startswith('if [ -f ' + flutter.SYSTEM_LIB):
            return PROFILES[self.context['shared_input_sha256']]['output'] + '  ' + flutter.SYSTEM_LIB
        # Hooks and Android overlay operations are never executed on the host.
        if re.search(r'\nsh ' + re.escape(self.module_id) + r'/(service|post-fs-data)\.sh$', command):
            return ''
        if '\ncmd overlay disable ' in command:
            return ''
        translated = command.replace('/data/adb/modules_update/' + self.module_id.rsplit('/', 1)[1],
                                     str(self.pending))
        translated = translated.replace(self.module_id, str(self.module)).replace('/data/local/tmp', str(self.remote))
        result = subprocess.run(['sh', '-c', translated], capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError('Fixture guest command failed: ' + command + '\n' + result.stderr)
        return result.stdout.strip()

    def push(self, config, action, *args, **kwargs):
        if action != 'push':
            raise AssertionError('Native payloads must not be pulled during hook migration.')
        destination = Path(args[1].replace('/data/local/tmp', str(self.remote)))
        shutil.copyfile(args[0], destination)

    def seed_flutter(self, *, receipt='legacy', hooks='legacy', revision=None):
        self.module_id = flutter.MODULE
        legacy = flutter.legacy_boot_script(firmware=self.context)
        current = flutter.boot_script(firmware=self.context)
        receipt_script = legacy if receipt == 'legacy' else current
        script = legacy if hooks == 'legacy' else current
        (self.module / 'module.prop').write_text('id=' + flutter.MODULE_ID + '\nauthor=HyperOS-AVD\n')
        self.saved = {'revision': flutter.REVISION if revision is None else revision,
                      'firmware': self.context, 'packages': self.packages, 'apk_hashes': self.apk_hashes,
                      'startup_script_sha256': hashlib.sha256(receipt_script.encode()).hexdigest(),
                      'system': {'target': flutter.SYSTEM_LIB, 'before': self.context['shared_input_sha256'],
                                 'after': PROFILES[self.context['shared_input_sha256']]['output']},
                      'apks': [], 'skipped_apks': {}}
        (self.module / 'manifest.json').write_text(json.dumps(self.saved, indent=2) + '\n')
        for name in ('post-fs-data.sh', 'service.sh'):
            (self.module / name).write_text(script)
            (self.module / name).chmod(0o755)
        for name in ('flutter.so', 'com.miui.home.apk', 'targets.conf', 'apks.conf'):
            (self.module / name).write_bytes(b'owned native payload: ' + name.encode())
        return current

    def seed_navigation(self):
        self.module_id = navigation.MODULE
        (self.module / 'module.prop').write_text('id=hyperos_avd_navigation\nauthor=HyperOS-AVD\n')
        self.saved = {'revision': navigation.REVISION, 'hyperos': '4.0.17.0.XFRCNXM',
                      'incremental': 'OS4.0.17.0.XFRCNXM', 'serial_number': '69704/F5XA01467'}
        (self.module / 'manifest.json').write_text(json.dumps(self.saved, indent=2) + '\n')
        previous = profile('4.0.17.0.XFRCNXM')
        for name, template in (('post-fs-data.sh', navigation.EARLY_SCRIPT), ('service.sh', navigation.BOOT_SCRIPT)):
            (self.module / name).write_text(navigation.legacy_startup_script(template, previous))
            (self.module / name).chmod(0o755)
        (self.module / 'owned-native.so').write_bytes(b'unchanged native payload')

    def snapshot(self):
        return {path.name: (path.lstat().st_ino, path.read_bytes() if not path.is_symlink() else path.readlink())
                for path in self.module.iterdir()}

    def run_flutter(self, *, enable=False):
        with patch.object(flutter, 'ROOT', self.workspace), patch.object(flutter, 'official'), \
                patch.object(flutter, 'root', side_effect=self.root), \
                patch.object(flutter, 'adb') as adb, patch.object(flutter, 'detach') as detach:
            result = flutter.install({}, enable=enable)
            adb.assert_not_called()
            detach.assert_not_called()
        return result

    def run_navigation(self, *, enable=False):
        with ExitStack() as stack:
            stack.enter_context(patch.object(navigation, 'ROOT', self.workspace))
            stack.enter_context(patch.object(navigation, 'official'))
            stack.enter_context(patch.object(navigation, 'root', side_effect=self.root))
            adb = stack.enter_context(patch.object(navigation, 'adb', side_effect=self.push))
            stack.enter_context(patch('phone_profile.profile_from_build', return_value=self.firmware))
            stack.enter_context(patch.object(navigation, 'inspect_deadlines', return_value=('catalog', 'audited native code')))
            result = navigation.install({}, enable=enable)
        return result, adb

    def test_flutter_saved_current_sha_never_authorizes_local_hook_overwrite(self):
        for revision in (6, flutter.REVISION):
            with self.subTest(revision=revision):
                self.seed_flutter(receipt='current', hooks='current', revision=revision)
                service = self.module / 'service.sh'
                service.write_text(service.read_text() + '# local startup edit\n')
                baseline = self.snapshot()
                result = self.run_flutter()
                self.assertEqual(result, {'preserved': True, 'reason': 'unreviewed-startup-hook', 'hook': 'service.sh'})
                self.assertEqual(self.snapshot(), baseline)
                self.assertFalse((self.workspace / 'work').exists())
                self.assertFalse((self.workspace / 'local/flutter-render-fix.json').exists())

    def test_flutter_enable_keeps_disable_when_hook_is_local_or_missing(self):
        for missing in (False, True):
            with self.subTest(missing=missing):
                self.seed_flutter(receipt='current')
                service = self.module / 'service.sh'
                if missing:
                    service.unlink()
                    service.symlink_to(self.folder / 'missing-hook')
                else:
                    service.write_text(service.read_text() + '# local startup edit\n')
                (self.module / 'disable').touch()
                baseline = self.snapshot()
                result = self.run_flutter(enable=True)
                self.assertTrue(result['preserved'])
                self.assertEqual(self.snapshot(), baseline)
                self.assertTrue((self.module / 'disable').exists())
                self.assertFalse((self.workspace / 'work').exists())

    def test_flutter_prior_same_revision_migrates_only_hooks_and_receipt(self):
        current = self.seed_flutter()
        baseline = self.snapshot()
        result = self.run_flutter()
        for name in ('flutter.so', 'com.miui.home.apk', 'targets.conf', 'apks.conf', 'module.prop'):
            self.assertEqual(self.snapshot()[name], baseline[name])
        for name in ('post-fs-data.sh', 'service.sh'):
            self.assertEqual((self.module / name).read_text(), current)
            self.assertEqual((self.module / name).stat().st_mode & 0o777, 0o755)
        receipt = json.loads((self.module / 'manifest.json').read_text())
        self.assertEqual(receipt['startup_script_sha256'], hashlib.sha256(current.encode()).hexdigest())
        self.assertEqual(result, receipt)
        self.assertEqual(json.loads((self.workspace / 'local/flutter-render-fix.json').read_text()), receipt)
        self.assertFalse((self.workspace / 'work').exists())
        self.assertFalse(any('.lifecycle.' in path.name for path in self.module.iterdir()))

    def test_flutter_current_hooks_are_not_replaced_on_reuse(self):
        self.seed_flutter(receipt='current', hooks='current')
        baseline = self.snapshot()
        self.run_flutter()
        self.assertEqual(self.snapshot(), baseline)
        self.assertFalse((self.workspace / 'work').exists())

    def test_navigation_unknown_hook_preserves_disabled_owned_module(self):
        self.seed_navigation()
        (self.module / 'service.sh').write_text('#!/system/bin/sh\n# locally maintained hook\n')
        (self.module / 'disable').touch()
        baseline = self.snapshot()
        result, adb = self.run_navigation(enable=True)
        self.assertEqual(result, {'preserved': True, 'reason': 'unreviewed-startup-hook', 'hook': 'service.sh'})
        self.assertEqual(self.snapshot(), baseline)
        adb.assert_not_called()
        self.assertFalse((self.workspace / 'work').exists())
        self.assertFalse((self.workspace / 'local/navigation-fix.json').exists())

    def test_navigation_known_previous_ota_hooks_use_atomic_refresh(self):
        self.seed_navigation()
        baseline = self.snapshot()
        result, adb = self.run_navigation()
        self.assertEqual(self.snapshot()['owned-native.so'], baseline['owned-native.so'])
        self.assertEqual(result['serial_number'], self.saved['serial_number'])
        for name, template in (('post-fs-data.sh', navigation.EARLY_SCRIPT), ('service.sh', navigation.BOOT_SCRIPT)):
            self.assertEqual((self.module / name).read_text(), navigation.startup_script(template, self.firmware))
            self.assertEqual((self.module / name).stat().st_mode & 0o777, 0o755)
            self.assertFalse(any(name + '.next' in command for command in self.commands))
            self.assertFalse(any(call.args[2].endswith('/' + name) for call in adb.call_args_list))
        self.assertFalse(any('.lifecycle.' in path.name for path in self.module.iterdir()))


if __name__ == '__main__':
    unittest.main()
