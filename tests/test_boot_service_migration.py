"""Exercise legacy helper authentication and migration on disposable files."""
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'scripts'))
import apply_boot_service_fix as install

LEGACY_IORAP_BLOCK = b'''if [ ! -e /dev/iorap_dev ]; then
    # Use the service's own documented opt-out; retain data and other preloaders.
    setprop persist.sys.stability.PrereadEnable false
    setprop ctl.stop iorapd
fi
'''


class BootServiceMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='hyperos-boot-migration-')
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.module, self.pending = self.folder / 'module', self.folder / 'pending'
        self.tools = self.folder / 'tools'
        self.tools.mkdir()
        if shutil.which('sha256sum'):
            checksum = shlex.quote(shutil.which('sha256sum'))
        elif shutil.which('shasum'):
            checksum = shlex.quote(shutil.which('shasum')) + ' -a 256'
        else:
            self.skipTest('No local SHA-256 utility is available.')
        self.source = install.PERF_SCRIPT.read_bytes()
        self.legacy = self.source.replace(b'PROBE=${1:-', LEGACY_IORAP_BLOCK + b'PROBE=${1:-', 1)
        self.assertEqual(hashlib.sha256(self.legacy).hexdigest(), install.LEGACY_KERNEL_SCRIPT_SHA256)
        self.assertEqual(hashlib.sha256(self.source).hexdigest(), install.KERNEL_SCRIPT_SHA256)
        self.environment = dict(os.environ, PATH=str(self.tools) + ':' + os.environ['PATH'],
                                HYPEROS_TEST_MODULE=str(self.module), HYPEROS_TEST_OWNER='0',
                                HYPEROS_TEST_RACE='', HYPEROS_TEST_CHCON_FAIL='0')
        self.executable('getprop', '''case "$1" in
ro.boot.hardware) echo ranchu;;
ro.product.device) echo hongkong;;
ro.mi.os.version.incremental) echo OS4.0.18.0.XFRCNXM;;
*) exit 1;;
esac''')
        self.executable('busybox', '''command=$1
shift
case "$command" in
stat) printf '%s\\n' "$HYPEROS_TEST_OWNER";;
sha256sum) exec ''' + checksum + ''' "$@";;
cp)
    case "$HYPEROS_TEST_RACE" in
    disable|remove) touch "$HYPEROS_TEST_MODULE/$HYPEROS_TEST_RACE";;
    helper) printf 'changed concurrently' > "$HYPEROS_TEST_MODULE/check-kernel-services.sh";;
    esac
    exec cp "$@";;
*) exec "$command" "$@";;
esac''')
        self.executable('chcon', 'exit "$HYPEROS_TEST_CHCON_FAIL"')
        self.executable('ls', 'printf "u:object_r:system_file:s0 %s\\n" "$2"')
        self.commands = []
        self.expected = install._module_asset_hashes()
        self.fixture()

    def executable(self, name, body):
        path = self.tools / name
        path.write_text('#!/bin/sh\n' + body + '\n')
        path.chmod(0o755)

    def fixture(self):
        self.module.mkdir()
        (self.module / 'payload').mkdir()
        hook, targets = install.boot_script()
        files = {'module.prop': install.MODULE_PROPERTIES.encode(),
                 'manifest.json': (json.dumps(install.receipt(), indent=2) + '\n').encode(),
                 'post-fs-data.sh': hook.encode(), 'service.sh': hook.encode(),
                 'targets.conf': targets.encode(), 'skip_mount': b'',
                 'kernel-probe': b'private probe fixture',
                 'check-kernel-services.sh': self.legacy,
                 'uninstall.sh': b'#!/system/bin/sh\n# Preserve an existing user hook.\n',
                 'user-choice': b'keep my choice\n'}
        for name in install.AFTER:
            files['payload/' + name] = ('private fixture ' + name).encode()
        for name, content in files.items():
            path = self.module / name
            path.write_bytes(content)
            path.chmod(0o755 if name.endswith('.sh') or name == 'kernel-probe' else 0o644)
        # Small stand-ins avoid requiring the proprietary APK/JAR/probe artifacts.
        # All metadata, hooks, helper bytes and expected-owner checks remain real.
        for name in ('kernel-probe', *('payload/' + name for name in install.AFTER)):
            self.expected[name] = hashlib.sha256(files[name]).hexdigest()

    def guest(self, config, command):
        self.assertEqual(config, {'name': 'Renamed_Without_Changing_Patches', 'port': 5580})
        self.commands.append(command)
        translated = command.replace('/data/adb/ksu/bin/busybox', str(self.tools / 'busybox'))
        translated = translated.replace('/system/bin/chcon', str(self.tools / 'chcon'))
        translated = translated.replace('/system/bin/ls', str(self.tools / 'ls'))
        result = subprocess.run(['sh', '-c', 'set -e\n' + translated], text=True,
                                capture_output=True, env=self.environment)
        if result.returncode:
            raise RuntimeError('Disposable guest guard rejected: ' + result.stderr)
        return result.stdout.strip()

    def migrate(self, operation='migrate_kernel_helper'):
        with patch.object(install, 'MODULE', str(self.module)), \
                patch.object(install, 'PENDING', str(self.pending)), \
                patch.object(install, '_module_asset_hashes', return_value=self.expected), \
                patch.object(install, 'root', side_effect=self.guest), \
                patch.object(install, 'adb') as adb:
            result = getattr(install, operation)({'name': 'Renamed_Without_Changing_Patches', 'port': 5580})
            adb.assert_not_called()
            return result

    def snapshot(self):
        return {str(path.relative_to(self.module)): (path.read_bytes(), path.stat().st_mode & 0o777)
                for path in self.module.rglob('*') if path.is_file() and not path.is_symlink()}

    def test_only_known_helper_changes_atomically_without_executing_hooks(self):
        helper = self.module / 'check-kernel-services.sh'
        helper.chmod(0o750)
        before = self.snapshot()
        result = self.migrate()
        self.assertTrue(result['migrated'])
        self.assertTrue(result['reboot_required'])
        before['check-kernel-services.sh'] = (self.source, 0o750)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(list(self.module.glob('.kernel-helper-*')), [])
        # Hooks would attempt namespace mounts; the emitted migration never runs them.
        self.assertFalse(any('nsenter' in command or 'ctl.restart' in command or 'reboot' in command
                             for command in self.commands))

    def test_current_helper_is_authenticated_and_reused_without_writing(self):
        (self.module / 'check-kernel-services.sh').write_bytes(self.source)
        before = self.snapshot()
        result = self.migrate()
        self.assertTrue(result['reused'])
        self.assertFalse(result['migrated'])
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(any('mkdir -m 700' in command for command in self.commands))

    def test_absent_module_is_not_created(self):
        shutil.rmtree(self.module)
        self.assertEqual(self.migrate(), {'migrated': False, 'present': False})
        self.assertFalse(self.module.exists())

    def test_disabled_removed_or_pending_module_choices_are_untouched(self):
        for flag in ('disable', 'remove'):
            with self.subTest(flag=flag):
                path = self.module / flag
                # Broken links also represent a user's lifecycle choice.
                path.symlink_to(self.module / 'no-such-file')
                result = self.migrate()
                self.assertEqual(result['preserved'], [flag])
                self.assertTrue(path.is_symlink())
                self.assertEqual((self.module / 'check-kernel-services.sh').read_bytes(), self.legacy)
                path.unlink()
        self.pending.mkdir()
        (self.pending / 'features.disabled').write_text('keep pending settings\n')
        self.assertTrue(self.migrate()['deferred'])
        self.assertEqual((self.pending / 'features.disabled').read_text(), 'keep pending settings\n')

    def test_unknown_owner_receipt_hooks_payload_probe_or_helper_are_rejected(self):
        for name in ('module.prop', 'manifest.json', 'targets.conf', 'post-fs-data.sh',
                     'service.sh', 'kernel-probe', 'payload/services', 'check-kernel-services.sh'):
            with self.subTest(name=name):
                path = self.module / name
                original = path.read_bytes()
                path.write_bytes(original + b'changed')
                before = self.snapshot()
                with self.assertRaises(RuntimeError):
                    self.migrate()
                self.assertEqual(self.snapshot(), before)
                path.write_bytes(original)
        self.environment['HYPEROS_TEST_OWNER'] = '1000'
        with self.assertRaises(RuntimeError):
            self.migrate()
        self.assertEqual((self.module / 'check-kernel-services.sh').read_bytes(), self.legacy)

    def test_newer_module_revision_and_symlinked_assets_are_preserved(self):
        prop = self.module / 'module.prop'
        original = prop.read_bytes()
        prop.write_bytes(original.replace(b'versionCode=2', b'versionCode=999'))
        with self.assertRaises(RuntimeError):
            self.migrate()
        self.assertIn(b'versionCode=999', prop.read_bytes())
        prop.write_bytes(original)
        target = self.module / 'service.sh'
        content = target.read_bytes()
        replacement = self.folder / 'elsewhere.sh'
        replacement.write_bytes(content)
        target.unlink()
        target.symlink_to(replacement)
        with self.assertRaises(RuntimeError):
            self.migrate()
        self.assertTrue(target.is_symlink())
        self.assertEqual(replacement.read_bytes(), content)

    def test_other_device_or_firmware_does_not_receive_phone_policy(self):
        for key, value in (('ro.product.device', 'yingtian'),
                           ('ro.boot.hardware', 'qcom'),
                           ('ro.mi.os.version.incremental', 'OS4.0.19.0.XFRCNXM')):
            with self.subTest(key=key):
                self.executable('getprop', f'''case "$1" in
{key}) echo {value};;
ro.boot.hardware) echo ranchu;;
ro.product.device) echo hongkong;;
ro.mi.os.version.incremental) echo OS4.0.18.0.XFRCNXM;;
*) exit 1;;
esac''')
                before = self.snapshot()
                with self.assertRaises(RuntimeError):
                    self.migrate()
                self.assertEqual(self.snapshot(), before)

    def test_probe_must_remain_executable(self):
        (self.module / 'kernel-probe').chmod(0o644)
        before = self.snapshot()
        with self.assertRaises(RuntimeError):
            self.migrate()
        self.assertEqual(self.snapshot(), before)

    def test_late_lifecycle_or_content_change_aborts_and_cleans_its_stage(self):
        for race in ('disable', 'remove', 'helper'):
            with self.subTest(race=race):
                (self.module / 'check-kernel-services.sh').write_bytes(self.legacy)
                self.environment['HYPEROS_TEST_RACE'] = race
                with self.assertRaises(RuntimeError):
                    self.migrate()
                self.assertEqual(list(self.module.glob('.kernel-helper-*')), [])
                if race == 'helper':
                    self.assertEqual((self.module / 'check-kernel-services.sh').read_bytes(), b'changed concurrently')
                else:
                    self.assertTrue((self.module / race).exists())
                    self.assertEqual((self.module / 'check-kernel-services.sh').read_bytes(), self.legacy)
                    (self.module / race).unlink()

    def test_failed_context_preservation_does_not_replace_helper(self):
        self.environment['HYPEROS_TEST_CHCON_FAIL'] = '1'
        before = self.snapshot()
        with self.assertRaises(RuntimeError):
            self.migrate()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(list(self.module.glob('.kernel-helper-*')), [])

    def test_helper_hash_exports_are_exact_and_current_source_stays_pinned(self):
        self.assertEqual(install.KERNEL_SCRIPT_HASHES,
                         (install.LEGACY_KERNEL_SCRIPT_SHA256, install.KERNEL_SCRIPT_SHA256))
        self.assertEqual(hashlib.sha256(self.source).hexdigest(), install.KERNEL_SCRIPT_SHA256)
        self.assertNotIn(b'persist.sys.stability.PrereadEnable', self.source)
        self.assertNotIn(b'ctl.stop iorapd', self.source)
        self.assertIn(b'sys.hyperos_avd.millet_supported 0', self.source)
        self.assertIn(b'gnss-extension', self.source)
        with tempfile.NamedTemporaryFile() as unrelated, patch.object(install, 'PERF_SCRIPT', Path(unrelated.name)):
            unrelated.write(b'changed repository helper')
            unrelated.flush()
            with self.assertRaisesRegex(RuntimeError, 'source differs'):
                install._kernel_migration_script()

    def test_known_uninstall_hooks_migrate_without_touching_current_preferences(self):
        target = self.module / 'uninstall.sh'
        template = ('#!/system/bin/sh\n[ "$(getprop persist.sys.stability.PrereadEnable)" = false ] && '
                    'setprop persist.sys.stability.PrereadEnable ')
        before = self.snapshot()
        digests = []
        for prior in ('', 'false', 'true', '0', '1'):
            with self.subTest(prior=prior):
                old = (template + shlex.quote(prior) + '\n').encode()
                digests.append(hashlib.sha256(old).hexdigest())
                target.write_bytes(old)
                self.assertTrue(self.migrate('migrate_uninstall_hook')['migrated'])
                self.assertEqual(target.read_bytes(), install.UNINSTALL_SCRIPT.encode())
                self.assertEqual((self.module / 'check-kernel-services.sh').read_bytes(), self.legacy)
                self.assertEqual(list(self.module.glob('.kernel-helper-*')), [])
        self.assertEqual(tuple(digests), install.LEGACY_UNINSTALL_SHA256S)
        before['uninstall.sh'] = (install.UNINSTALL_SCRIPT.encode(), 0o755)
        self.assertEqual(self.snapshot(), before)
        self.assertNotIn(b'persist.sys.stability.PrereadEnable', target.read_bytes())
        self.assertTrue(self.migrate('migrate_uninstall_hook')['reused'])

    def test_unknown_absent_or_disabled_uninstall_hook_is_preserved(self):
        target = self.module / 'uninstall.sh'
        original = target.read_bytes()
        result = self.migrate('migrate_uninstall_hook')
        self.assertTrue(result['unsupported'])
        self.assertEqual(target.read_bytes(), original)
        (self.module / 'disable').touch()
        self.assertEqual(self.migrate('migrate_uninstall_hook')['preserved'], ['disable'])
        self.assertEqual(target.read_bytes(), original)
        (self.module / 'disable').unlink()
        target.unlink()
        self.assertFalse(self.migrate('migrate_uninstall_hook')['hook_present'])
        self.assertFalse(target.exists())


if __name__ == '__main__':
    unittest.main()
