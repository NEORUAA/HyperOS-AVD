"""Exercise authenticated migration against a local shell guest filesystem."""
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import app_compat_migration as migration
import app_bridge_module
import apply_app_compat
import apply_camera_fix as parrot

REAL_MARKER = migration._marker


def checksum(body):
    return hashlib.sha256(body).hexdigest()


class HistoricalReceiptTests(unittest.TestCase):
    def test_frozen_catalog_is_independent_of_current_templates_and_complete(self):
        values = migration.records()
        self.assertEqual({record['id'] for record in values}, set(migration.MODULES))
        bridges = [record for record in values if record['feature'] != 'oem-camera']
        self.assertEqual(len(bridges), 3)
        for record in bridges:
            self.assertEqual(record['manifest']['revision'], 2)
            self.assertEqual(record['hashes']['runtime.sh'],
                             '84b37d80cbbe7633e4b5debabf694a3252e7e1b8ac1b85f7f979e6b5c71dd59a')
            self.assertEqual(record['manifest']['files_sha256']['runtime.sh'], record['hashes']['runtime.sh'])
            self.assertIn('SHA256SUMS', record['hashes'])
            self.assertTrue(any(name.startswith('payloads/') for name in record['hashes']))
        cameras = [record for record in values if record['feature'] == 'oem-camera']
        self.assertEqual(len({record['hashes']['app/MiuiCamera.apk'] for record in cameras}), 3)
        self.assertEqual(len({record['hashes']['app/lib/arm64/libcamera_yuv_jni.so'] for record in cameras}), 2)
        for record in cameras:
            self.assertIn('provider', record['hashes'])
            self.assertIn('hwl.so', record['hashes'])
            self.assertIn('post-fs-data.sh', record['hashes'])
            self.assertIn('service.sh', record['hashes'])

    def test_real_full_union_and_each_legacy_guard_fit_one_adb_command(self):
        from app_compat_catalog import MODULE_ID, catalog_files, canonical
        from app_compat_producers import recipes
        files = catalog_files(recipes())
        manifest = json.loads(files['manifest.json'])
        hashes = {name: value for name, value in manifest['files_sha256'].items() if name != 'customize.sh'}
        hashes['manifest.json'] = checksum(canonical(manifest))
        hashes['SHA256SUMS'] = checksum(files['SHA256SUMS'])
        self.assertGreater(len(hashes), 70)
        self.assertEqual(len(manifest['recipes']), 6)
        self.assertEqual(sum(name.startswith('objects/') for name in hashes), 11)
        active = {'directory': '/data/adb/modules/' + MODULE_ID,
                  'properties': files['module.prop'].decode(), 'flags': []}
        record = {'id': MODULE_ID, 'properties': active['properties'], 'hashes': hashes,
                  'optional_hashes': {'customize.sh': manifest['files_sha256']['customize.sh']}}
        for legacy in migration.records():
            with self.subTest(legacy=legacy['id'], assets=len(legacy['hashes'])):
                checked = set()
                old = {'directory': '/data/adb/modules/' + legacy['id'],
                       'properties': legacy['properties'], 'flags': []}
                guards = (migration._checks(old, legacy, checked_parents=checked) + '\n' +
                          migration._checks(active, record, checked_parents=checked))
                transport = 'su -W -c ' + shlex.quote('set -e\n' + guards)
                # Leave room for archive, lifecycle and inode checks in retire.
                self.assertLess(len(transport.encode()), 24 * 1024)
                self.assertEqual(guards.count('[ -d /data ] && [ ! -L /data ] || exit 1'), 1)
                self.assertEqual(guards.count('[ -d /data/adb ] && [ ! -L /data/adb ] || exit 1'), 1)
                for name, value in hashes.items():
                    self.assertIn(name + ' ' + value + '\n', guards)
                self.assertIn('stat -c %h:%u "$path"', guards)

    def test_receipt_table_rejects_empty_or_injected_hashes_and_paths(self):
        for name, hashes in (('payloads/a.so', []), ('payloads/a.so', ['x']),
                             ('payloads/a.so', [None]), ('payloads/../a.so', ['0' * 64]),
                             ('payloads/a\n.so', ['0' * 64])):
            with self.subTest(name=name, hashes=hashes), self.assertRaises(RuntimeError):
                migration._asset_path('/data/adb/modules/audited', name, hashes)


class AppCompatMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.data = self.base / 'data'
        (self.data / 'adb/modules').mkdir(parents=True)
        (self.data / 'adb/modules_update').mkdir()
        self.bin = self.base / 'bin'
        self.bin.mkdir()
        (self.bin / 'stat').write_text(f'''#!{sys.executable}
import os,sys
s=os.stat(sys.argv[-1]); value=sys.argv[sys.argv.index('-c')+1]
print(value.replace('%h',str(s.st_nlink)).replace('%u','0').replace('%d',str(s.st_dev)).replace('%i',str(s.st_ino)))
''')
        (self.bin / 'sha256sum').write_text(f'''#!{sys.executable}
import hashlib,sys
for name in sys.argv[1:]:
 print(hashlib.sha256(open(name,'rb').read()).hexdigest()+'  '+name)
''')
        for path in self.bin.iterdir():
            path.chmod(0o755)
        self.calls = []
        self.registry = []
        self.catalog = [{'feature': name} for name in ('weather', 'oem-camera', 'parrot-camera')]
        self.stack = ExitStack()
        self.stack.enter_context(patch.object(migration, 'records', lambda: self.registry))
        for module in (migration, app_bridge_module, apply_app_compat):
            self.stack.enter_context(patch.object(module, 'root', self.root))
        self.stack.enter_context(patch.object(migration, '_marker', return_value=None))
        self.owner_manifest = None

    def tearDown(self):
        self.stack.close()
        self.temporary.cleanup()

    def root(self, config, command):
        self.calls.append(command)
        translated = command.replace('/data', str(self.data))
        env = {**os.environ, 'PATH': str(self.bin) + ':' + os.environ['PATH']}
        result = subprocess.run(['sh', '-e', '-c', translated], env=env, capture_output=True, text=True)
        if result.returncode:
            raise subprocess.CalledProcessError(result.returncode, command, result.stdout, result.stderr)
        return result.stdout.strip()

    def create_old(self, identifier, flags=()):
        feature = migration.MODULES[identifier]
        folder = self.data / 'adb/modules' / identifier
        folder.mkdir()
        props = f'id={identifier}\nname=Audited fixture\nversion=2\nversionCode=2\nauthor=HyperOS-AVD\ndescription=Fixture\n'
        manifest = {'id': identifier, 'revision': 2, 'fixture': 'canonical'}
        assets = {'module.prop': props.encode(), 'manifest.json': (json.dumps(manifest) + '\n').encode(),
                  'runtime.sh': b'#!/system/bin/sh\nexit 0\n',
                  'service.sh': b'#!/system/bin/sh\nexit 0\n', 'payloads/helper.so': b'original audited binary'}
        for name, body in assets.items():
            path = folder / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
        for flag in flags:
            (folder / flag).touch()
        self.registry.append({'id': identifier, 'feature': feature, 'manifest': manifest,
                              'properties': props, 'hashes': {name: checksum(body) for name, body in assets.items()}})
        return folder

    def create_active(self, full_catalog=False):
        identifier = 'hyperos_avd_app_compat'
        folder = self.data / 'adb/modules' / identifier
        folder.mkdir()
        props = f'id={identifier}\nname=Universal\nversion=1\nversionCode=1\nauthor=HyperOS-AVD\ndescription=Fixture\n'
        assets = {'module.prop': props.encode(), 'runtime.sh': b'new runtime', 'objects/helper.bin': b'new binary',
                  'customize.sh': b'install control'}
        manifest = {'revision': 1, 'files_sha256': {name: checksum(body) for name, body in assets.items()}}
        if full_catalog:
            from app_compat_catalog import catalog_files
            from app_compat_producers import recipes
            assets = catalog_files(recipes())
            manifest = json.loads(assets.pop('manifest.json'))
            assets.pop('SHA256SUMS')
            # Exercise the real catalog's complete file layout with hermetic
            # binary fixtures; the separate bound test retains production pins.
            for name in manifest['files_sha256']:
                if name not in assets:
                    assets[name] = ('binary fixture ' + name).encode()
            manifest['files_sha256'] = {name: checksum(body) for name, body in assets.items()}
            props = assets['module.prop'].decode()
        body = apply_app_compat.canonical(manifest)
        hashes = {name: value for name, value in manifest['files_sha256'].items() if name != 'customize.sh'}
        hashes['manifest.json'] = checksum(body)
        assets['manifest.json'] = body
        assets['SHA256SUMS'] = ''.join(value + '  ' + name + '\n' for name, value in sorted(hashes.items())).encode()
        for name, contents in assets.items():
            if name == 'customize.sh':
                continue
            path = folder / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(contents)
        self.stack.enter_context(patch('app_compat_catalog.catalog_files', return_value={'module.prop': props.encode(), 'manifest.json': body}))
        self.owner_manifest = manifest
        return folder

    def plan(self):
        return migration.plan({}, self.catalog, workspace=self.base)

    def snapshot(self):
        return {str(path.relative_to(self.data)): path.read_bytes() for path in self.data.rglob('*') if path.is_file()}

    def test_plan_is_read_only_and_imports_only_authenticated_optins(self):
        self.create_old('hyperos_avd_xiaomi_camera')
        self.create_old('hyperos_avd_parrot_camera')
        before = self.snapshot()
        planned = self.plan()
        self.assertEqual(planned['choices'], {'oem-camera': True, 'parrot-camera': True})
        self.assertEqual(self.snapshot(), before)
        migration.import_choices({}, planned)
        self.assertEqual((self.data / 'adb/hyperos_app_compat/features/oem-camera').read_text(), 'on\n')
        self.assertEqual((self.data / 'adb/hyperos_app_compat/features/parrot-camera').read_text(), 'on\n')
        self.assertFalse(any('mv /data/adb/modules/' in command for command in self.calls))

    def test_disabled_owner_is_preserved_and_imports_off(self):
        folder = self.create_old('hyperos_avd_weather_gl', ('disable',))
        planned = self.plan()
        self.assertEqual(planned['choices'], {'weather': False})
        self.assertEqual(planned['modules'], [])
        migration.import_choices({}, planned)
        self.assertTrue((folder / 'disable').exists())
        self.assertEqual((self.data / 'adb/hyperos_app_compat/features/weather').read_text(), 'off\n')

    def test_broken_lifecycle_alias_is_preserved_and_imports_off(self):
        folder = self.create_old('hyperos_avd_weather_gl')
        (folder / 'disable').symlink_to('/missing-user-choice')
        planned = self.plan()
        self.assertEqual(planned['choices'], {'weather': False})
        self.assertEqual(planned['modules'], [])
        migration.import_choices({}, planned)
        self.assertTrue((folder / 'disable').is_symlink())
        self.assertEqual((self.data / 'adb/hyperos_app_compat/features/weather').read_text(), 'off\n')

    def test_pending_payload_changes_and_extra_entrypoints_are_preserved(self):
        for kind in ('pending', 'payload', 'control', 'custom'):
            with self.subTest(kind=kind):
                folder = self.create_old('hyperos_avd_weather_gl')
                if kind == 'pending':
                    (self.data / 'adb/modules_update/hyperos_avd_weather_gl').mkdir()
                elif kind == 'payload':
                    (folder / 'payloads/helper.so').write_bytes(b'foreign binary')
                elif kind == 'control':
                    (folder / 'runtime.sh').write_bytes(b'foreign control')
                else:
                    (folder / 'action.sh').write_bytes(b'custom action')
                before = self.snapshot()
                planned = self.plan()
                self.assertEqual(planned['choices'], {})
                self.assertEqual(planned['modules'], [])
                self.assertEqual(len(planned['preserved']), 1)
                self.assertEqual(self.snapshot(), before)
                shutil.rmtree(folder)
                pending = self.data / 'adb/modules_update/hyperos_avd_weather_gl'
                if pending.exists():
                    pending.rmdir()
                self.registry.clear()

    def test_existing_and_concurrent_new_feature_choices_win(self):
        self.create_old('hyperos_avd_parrot_camera')
        planned = self.plan()
        path = self.data / 'adb/hyperos_app_compat/features/parrot-camera'
        path.parent.mkdir(parents=True)
        path.write_text('auto\n')
        migration.import_choices({}, planned)
        self.assertEqual(path.read_text(), 'auto\n')
        self.assertEqual(self.plan()['choices'], {})

    def test_changed_old_payload_after_plan_never_imports_a_choice(self):
        folder = self.create_old('hyperos_avd_xiaomi_camera')
        planned = self.plan()
        (folder / 'payloads/helper.so').write_bytes(b'changed after plan')
        migration.import_choices({}, planned)
        self.assertFalse((self.data / 'adb/hyperos_app_compat/features').exists())

    def test_compact_table_preserves_exact_alternatives_and_single_link_guards(self):
        old = self.create_old('hyperos_avd_weather_gl')
        payload = old / 'payloads/helper.so'
        original = checksum(payload.read_bytes())
        alternative = b'second audited representation'
        self.registry[0]['allowed_hashes'] = {'payloads/helper.so': [original, checksum(alternative)]}
        payload.write_bytes(alternative)
        self.assertEqual(self.plan()['choices'], {'weather': True})
        payload.write_bytes(alternative + b'foreign suffix')
        self.assertEqual(self.plan()['choices'], {})
        payload.write_bytes(alternative)
        alias = self.base / 'linked-original'
        os.link(payload, alias)
        self.assertEqual(self.plan()['choices'], {})
        self.assertEqual(payload.stat().st_nlink, 2)

    def test_host_marker_requires_exact_dynamic_caller_and_never_authorizes_retirement(self):
        apk = '/data/app/~~ABC/com.google.android.GoogleCamera.parrot-XYZ/base.apk'
        native = str(Path(apk).parent / 'lib/arm64')
        folder = self.data / Path(apk).relative_to('/data')
        folder.parent.mkdir(parents=True)
        folder.write_bytes(b'caller APK')
        library = self.data / Path(native + '/libgcastartup.so').relative_to('/data')
        library.parent.mkdir(parents=True)
        library.write_bytes(b'native caller')
        metadata = {'revision': 3, 'module': parrot.MODULE_ID, 'package': parrot.PACKAGE,
                    'apk_sha256': checksum(b'caller APK'), 'runtime_sha256': parrot.RUNTIME_SHA256,
                    'source_sha256': parrot.BRIDGE_SOURCE_SHA256, 'sha256': parrot.BRIDGE_SHA256,
                    'experimental': True}
        path = self.base / 'local/camera-fix.json'
        path.parent.mkdir()
        path.write_text(json.dumps(metadata))
        catalog = [{'feature': 'parrot-camera', 'apk_sha256': metadata['apk_sha256'],
                    'native_libraries': {'libgcastartup.so': checksum(b'native caller')}}]
        def caller_root(config, command):
            if command == 'pm path ' + parrot.PACKAGE:
                return 'package:' + apk
            if command == 'dumpsys package ' + parrot.PACKAGE:
                return '  nativeLibraryDir=' + native
            return self.root(config, command)
        with patch.object(parrot, 'APK_SHA256', metadata['apk_sha256']), \
                patch.object(migration, 'root', caller_root):
            marker = REAL_MARKER({}, self.base, catalog)
            self.assertEqual(marker['sha256'], checksum(path.read_bytes()))
            library.write_bytes(b'changed caller')
            self.assertIsNone(REAL_MARKER({}, self.base, catalog))
        self.assertFalse((self.data / 'adb/hyperos_app_compat').exists())
        self.assertEqual(list((self.data / 'adb/modules').iterdir()), [])

    def test_retirement_requires_active_owner_and_preserves_all_loaded_inodes(self):
        old = self.create_old('hyperos_avd_parrot_camera')
        planned = self.plan()
        active = self.create_active()
        pending = self.data / 'adb/modules_update/hyperos_avd_app_compat'
        pending.mkdir()
        result = migration.retire({}, planned, self.owner_manifest)
        self.assertTrue(result['deferred'])
        self.assertTrue(old.exists())
        pending.rmdir()
        inode = old.stat().st_ino
        payload_inode = (old / 'payloads/helper.so').stat().st_ino
        result = migration.retire({}, planned, self.owner_manifest)
        self.assertTrue(result['reboot_required'])
        self.assertEqual(result['retired'], ['hyperos_avd_parrot_camera'])
        self.assertFalse(old.exists())
        archived = next((self.data / 'adb/hyperos_app_compat/legacy').iterdir())
        self.assertEqual(archived.stat().st_ino, inode)
        self.assertEqual((archived / 'payloads/helper.so').stat().st_ino, payload_inode)
        self.assertTrue(active.exists())
        for command in self.calls:
            self.assertNotIn('umount', command)
            self.assertNotIn('module uninstall', command)
            self.assertFalse(re.search(r'(^|\n)(stop |start |am force-stop)', command))

    def test_current_universal_revision_reuses_through_public_path_with_empty_migration(self):
        from app_compat_catalog import REVISION
        self.assertGreaterEqual(REVISION,3)
        self.create_active(full_catalog=True)
        before=self.snapshot()
        def public_root(config,command):
            if command.startswith('getprop'):
                return 'ranchu\nOS4.999.0.TEST'
            return self.root(config,command)
        with patch.object(apply_app_compat,'root',side_effect=public_root), \
                patch.object(apply_app_compat,'verify_prebuilt',return_value=(self.base/'verified.zip',self.owner_manifest)), \
                patch.object(apply_app_compat,'adb') as adb:
            result=apply_app_compat.install_prebuilt({},workspace=self.base,catalog=self.catalog,migration=migration)
        self.assertTrue(result['reused'])
        self.assertFalse(result['pending'])
        self.assertFalse(result['reboot_required'])
        self.assertEqual(result['migration'],{'retired':[],'reboot_required':False})
        self.assertEqual(self.snapshot(),before)
        adb.assert_not_called()

    def test_current_owner_exact_revision_and_full_assets_remain_required(self):
        old=self.create_old('hyperos_avd_parrot_camera')
        planned=self.plan()
        active=self.create_active(full_catalog=True)
        props=active/'module.prop'
        original=props.read_text()
        props.write_text(re.sub(r'versionCode=\d+','versionCode=999',original))
        before=self.snapshot()
        with self.assertRaisesRegex(RuntimeError,'Unknown private bridge module revision'):
            migration.retire({},planned,self.owner_manifest)
        self.assertEqual(self.snapshot(),before)
        self.assertTrue(old.exists())
        props.write_text(original)
        asset=active/'webroot/app.js'
        asset.write_bytes(b'unknown local bytes in the exact current revision')
        before=self.snapshot()
        with self.assertRaises(subprocess.CalledProcessError):
            migration.retire({},planned,self.owner_manifest)
        self.assertEqual(self.snapshot(),before)
        self.assertTrue(old.exists())
        self.assertFalse((self.data/'adb/hyperos_app_compat/legacy').exists())

    def test_new_lifecycle_flag_and_old_inode_replacement_prevent_retirement(self):
        old = self.create_old('hyperos_avd_xiaomi_camera')
        planned = self.plan()
        active = self.create_active()
        (active / 'disable').touch()
        self.assertTrue(migration.retire({}, planned, self.owner_manifest)['deferred'])
        (active / 'disable').unlink()
        other = old.with_name('replacement')
        shutil.copytree(old, other)
        shutil.rmtree(old)
        other.rename(old)
        self.assertEqual(migration.retire({}, planned, self.owner_manifest)['retired'], [])
        self.assertTrue(old.exists())

    def test_archive_alias_refuses_rename_and_preserves_old_contents(self):
        old = self.create_old('hyperos_avd_weather_gl')
        planned = self.plan()
        self.create_active()
        archive = self.data / 'adb/hyperos_app_compat/legacy'
        archive.parent.mkdir()
        foreign = self.base / 'foreign'
        foreign.mkdir()
        archive.symlink_to(foreign)
        with self.assertRaises(subprocess.CalledProcessError):
            migration.retire({}, planned, self.owner_manifest)
        self.assertTrue(old.exists())
        self.assertEqual(list(foreign.iterdir()), [])

    def test_full_union_retirement_bound_rechecks_late_asset_before_rename(self):
        old = self.create_old('hyperos_avd_parrot_camera')
        planned = self.plan()
        active = self.create_active(full_catalog=True)
        asset = active / 'webroot/app.js'
        original = asset.read_bytes()
        real_active_owner = migration._active_owner
        calls = 0

        def mutate_after_authentication(config, expected, catalog):
            nonlocal calls
            result = real_active_owner(config, expected, catalog)
            calls += 1
            if calls == 2:
                asset.write_bytes(b'changed after active authentication')
            return result

        with patch.object(migration, '_active_owner', mutate_after_authentication), \
                self.assertRaises(subprocess.CalledProcessError):
            migration.retire({}, planned, self.owner_manifest)
        self.assertTrue(old.exists())
        archive_command = self.calls[-1]
        self.assertIn('mv /data/adb/modules/hyperos_avd_parrot_camera', archive_command)
        self.assertLess(len(('su -W -c ' + shlex.quote(archive_command)).encode()), 24 * 1024)
        asset.write_bytes(original)
        self.assertEqual(migration.retire({}, planned, self.owner_manifest)['retired'],
                         ['hyperos_avd_parrot_camera'])

    def test_optional_nested_assets_preserve_missing_directories_and_reject_aliases(self):
        old = self.create_old('hyperos_avd_weather_gl')
        optional = b'optional audited contents'
        self.registry[0]['optional_hashes'] = {'optional/helper.so': checksum(optional)}
        self.assertEqual(self.plan()['choices'], {'weather': True})
        foreign = self.base / 'optional-foreign'
        foreign.mkdir()
        (foreign / 'helper.so').write_bytes(optional)
        (old / 'optional').symlink_to(foreign)
        self.assertEqual(self.plan()['choices'], {})
        self.assertTrue((old / 'optional').is_symlink())


if __name__ == '__main__':
    unittest.main()
