"""Verify pre-boot module gates and recoverable phone firmware upgrades."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import common
import manage
import setup
import package_release
from phone_profile import ARCHIVES, LEGACY_PINS
from patch_flutter import PROFILES
import test_manager

R2 = 'v0.2.1-a17-hyperos4-hongkong-r2'
R3 = 'v0.2.2-a17-hyperos4-hongkong-r3'
R4 = 'v0.2.3-a17-hyperos4-hongkong-r4'
OLD = 'OS4.0.17.0.XFRCNXM'
NEW = '4.0.18.0.XFRCNXM'


def r3_manifest(value):
    result = copy.deepcopy(value)
    result.update(version=R3, hyperos=NEW, source=common.OS4_SOURCE, source_device='hongkong')
    result['build'].update(hyperos=NEW, archive_sha256=ARCHIVES[NEW])
    result['compatibility'].update(minimum_installer='1.2.0', runtime_in_bundle=True,
        upgrade_from=[R2], module_upgrade_preflight=manage.MODULE_UPGRADE_PREFLIGHT)
    return result


class PhoneUpgradeTests(unittest.TestCase):
    def test_forward_policy_accepts_supported_old_and_future_revisions(self):
        from apply_boot_service_fix import receipt
        helper = test_manager.ManagerTests()
        with tempfile.TemporaryDirectory() as directory:
            path, original = helper.bundle(Path(directory) / 'bundle')
            old = r3_manifest(original)
            new = copy.deepcopy(old)
            new['version'] = R4
            new['build']['boot_service_fix'] = receipt()
            new['compatibility'].pop('upgrade_from')
            new['compatibility'].update(minimum_installer='1.2.1', upgrade_policy=manage.FORWARD_UPGRADE_POLICY)
            path.write_text(json.dumps(new))
            parsed, _ = setup.read_manifest(str(path))
            manage.compatible(old, parsed)
            self.assertFalse(manage.firmware_change(old, parsed))
            for mutate in ('old-installer', 'unknown-policy', 'bad-receipt', 'missing-receipt'):
                bad = copy.deepcopy(new)
                if mutate == 'old-installer':
                    bad['compatibility']['minimum_installer'] = '1.2.0'
                elif mutate == 'unknown-policy':
                    bad['compatibility']['upgrade_policy'] = 'unsafe-skip-checks'
                elif mutate == 'bad-receipt':
                    bad['build']['boot_service_fix']['probe_sha256'] = 'f' * 64
                else:
                    del bad['build']['boot_service_fix']
                path.write_text(json.dumps(bad))
                with self.subTest(mutate=mutate), self.assertRaisesRegex(RuntimeError, 'r4|upgrade policy'):
                    setup.read_manifest(str(path))
            for version in ('v0.2.0-a17-hyperos4-hongkong-r1', R2, R3):
                source = copy.deepcopy(original if version != R3 else old)
                source['version'] = version
                with self.subTest(source=version):
                    manage.compatible(source, new)
                    self.assertEqual(manage.firmware_change(source, new), version != R3)
            future = copy.deepcopy(new)
            future['version'] = 'v0.9.9-a17-hyperos4-hongkong-r999'
            path.write_text(json.dumps(future))
            setup.read_manifest(str(path))
            manage.compatible(new, future)
            future['version'] = 'v0.2.3-a17-hyperos4-hongkong-r999'
            path.write_text(json.dumps(future))
            setup.read_manifest(str(path))
            manage.compatible(new, future)
            with self.assertRaisesRegex(RuntimeError, 'downgrades'):
                manage.compatible(future, new)
            wrong = copy.deepcopy(old)
            wrong['build']['archive_sha256'] = 'f' * 64
            with self.assertRaisesRegex(RuntimeError, 'archive'):
                manage.compatible(wrong, new)
            with self.assertRaisesRegex(RuntimeError, 'downgrades'):
                manage.compatible(new, old)

    def test_r4_metadata_requires_new_installer_and_retains_pinned_receipt(self):
        from apply_boot_service_fix import receipt
        build = {'source': common.OS4_SOURCE, 'hyperos': NEW, 'android_api': 37,
                 'archive_sha256': ARCHIVES[NEW], 'adb_authentication': True,
                 'flutter_render_fix': 6, 'native_quickstep_identity': True,
                 'preinstalled_apps': {'apps': ['test']}, 'avd_defaults': {'test': True},
                 'boot_service_fix': receipt()}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'local').mkdir()
            (root / 'local/build.json').write_text(json.dumps(build))
            metadata = package_release.release_metadata(root, 'os4-official')
            self.assertEqual(metadata['build']['boot_service_fix'], receipt())
            self.assertEqual(metadata['compatibility']['minimum_installer'], '1.2.1')
            self.assertNotIn('upgrade_from', metadata['compatibility'])
            self.assertEqual(metadata['compatibility']['upgrade_policy'], manage.FORWARD_UPGRADE_POLICY)

    def staging_fixture(self, folder):
        root = folder / 'instance'
        avd = root / 'avd/Test_temp.avd'
        avd.mkdir(parents=True)
        (avd / 'config.ini').write_text('hw.ramSize=6144\nhw.cpu.ncore=4\n')
        old = {'version': R2, 'variant': 'os4-official', 'build': {}, 'files': {}}
        for name in ('images/system.img', 'images/kernel-ranchu', 'images/ramdisk.img',
                     'tools/ksud-aarch64-linux-android'):
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(name.encode())
            old['files'][name] = {'sha256': manage.sha256(path)}
        backup = folder / 'backup'
        backup.mkdir()
        return root, old, backup

    def test_staging_marks_awake_defaults_only_after_all_module_validation(self):
        from os4_defaults import AWAKE_STAMP
        for reject, touched in ((False, True), (True, False)):
            with self.subTest(reject=reject), tempfile.TemporaryDirectory() as directory:
                folder = Path(directory)
                root, old, backup = self.staging_fixture(folder)
                calls = []
                def adb(config, *args, **kwargs):
                    self.assertEqual(config['name'], 'Test_temp')
                    self.assertEqual(config['port'], 5584)
                    calls.append(args)
                    output = ''
                    if args[0] == 'get-state':
                        output = 'device'
                    elif args[:3] == ('emu', 'avd', 'name'):
                        output = 'Test_temp\nOK'
                    elif 'getenforce' in args[-1]:
                        output = 'uid=0(root) gid=0(root)\nEnforcing\n' + OLD
                    return subprocess.CompletedProcess(args, 0, output, '')
                def guards(*args):
                    self.assertEqual(args[-1], NEW)
                    calls.append(('module-validation',))
                    if reject:
                        raise RuntimeError('unsupported module')
                    return []
                process = unittest.mock.Mock()
                process.poll.return_value = None
                with patch('phone_profile.profile_from_build', return_value={
                            'hyperos': '4.0.17.0.XFRCNXM', 'incremental': OLD}), \
                        patch.object(manage, 'idle'), patch.object(manage.subprocess, 'Popen', return_value=process), \
                        patch.object(common, 'adb', side_effect=adb), \
                        patch.object(manage, 'guarded_modules', side_effect=guards):
                    if reject:
                        with self.assertRaisesRegex(RuntimeError, 'unsupported module'):
                            manage.prepare_module_upgrade(root, old, {'version': R3, 'hyperos': NEW},
                                                          'Test_temp', 5584, folder / 'sdk', backup)
                    else:
                        manage.prepare_module_upgrade(root, old, {'version': R3, 'hyperos': NEW},
                                                      'Test_temp', 5584, folder / 'sdk', backup)
                stamps = [index for index, args in enumerate(calls) if AWAKE_STAMP in args[-1]]
                self.assertEqual(bool(stamps), touched)
                if touched:
                    self.assertGreater(stamps[0], calls.index(('module-validation',)))
                    self.assertNotIn('settings put', calls[stamps[0]][-1])
                    receipt = json.loads((backup / 'module-upgrade.json').read_text())
                    self.assertEqual(receipt['preserved_defaults'], {'awake_stamp': AWAKE_STAMP})

    def test_staging_guest_refuses_safe_mode_before_module_inventory(self):
        for persistent, boot, accepted in (('', '', True), ('0', '0', True),
                                           ('1', '0', False), ('0', '1', False)):
            with self.subTest(persistent=persistent, boot=boot):
                def adb(config, *args, **kwargs):
                    self.assertEqual(config['port'], 5584)
                    value = {'persist.sys.safemode': persistent, 'ro.sys.safemode': boot}.get(
                        args[-1], 'uid=0(root) gid=0(root)\nEnforcing\n' + OLD)
                    return subprocess.CompletedProcess(args, 0, value + '\n', '')
                with patch.object(common, 'adb', side_effect=adb):
                    if accepted:
                        manage.validate_upgrade_guest({'port': 5584}, OLD)
                    else:
                        with self.assertRaisesRegex(RuntimeError, 'safe mode'):
                            manage.validate_upgrade_guest({'port': 5584}, OLD)

    def test_candidate_metadata_keeps_phone_family_and_requires_guarded_upgrade(self):
        build = {'source': common.OS4_SOURCE, 'hyperos': NEW, 'android_api': 37,
                 'archive_sha256': ARCHIVES[NEW], 'adb_authentication': True,
                 'flutter_render_fix': 6, 'native_quickstep_identity': True,
                 'preinstalled_apps': {'apps': ['test']}, 'avd_defaults': {'test': True}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'local').mkdir()
            source = root / 'local/build.json'
            source.write_text(json.dumps(build))
            metadata = package_release.release_metadata(root, 'os4-official')
            self.assertEqual(metadata['hyperos'], NEW)
            self.assertEqual(metadata['compatibility'], {
                'minimum_installer': '1.2.0', 'userdata_family': 'os4-hongkong-api37-ranchu-4k',
                'upgrade_from': [R2], 'runtime_in_bundle': True,
                'module_upgrade_preflight': manage.MODULE_UPGRADE_PREFLIGHT})
            source.write_text(json.dumps({**build, 'archive_sha256': 'f' * 64}))
            with self.assertRaisesRegex(RuntimeError, 'archive'):
                package_release.release_metadata(root, 'os4-official')

    def test_module_staging_preserves_serial_manifest_and_disabled_choice(self):
        navigation = {'revision': 10, 'property': 'ro.miui.product.home', 'value': 'com.miui.home',
                      'component': 'com.miui.home/com.miui.home.recents.RecentsActivity',
                      'serial_number': '69704/F5XA01467'}
        source = '#!/system/bin/sh\n# Retain exact original whitespace.\nprintf patched\n\n'
        calls = []
        def adb(config, *args, **kwargs):
            self.assertEqual(config['port'], 5584)
            calls.append(args)
            command = args[-1]
            output = ''
            if args[0] == 'push':
                self.assertIn('hyperos_avd_navigation', args[1])
            elif 'if [ -d /data/adb/modules/hyperos_avd_navigation' in command:
                output = 'yes\n'
            elif 'cat /data/adb/modules/hyperos_avd_navigation/module.prop' in command:
                output = 'id=hyperos_avd_navigation\nauthor=HyperOS-AVD\n'
            elif 'cat /data/adb/modules/hyperos_avd_navigation/manifest.json' in command:
                output = json.dumps(navigation) + '\n'
            elif 'for flag in disable remove' in command:
                output = 'disable\n'
            elif args[0] == 'exec-out':
                output = source
            return subprocess.CompletedProcess(args, 0, output, '')
        with tempfile.TemporaryDirectory() as directory, patch.object(common, 'adb', side_effect=adb):
            receipt = manage.guarded_modules({'port': 5584}, Path(directory), OLD)
            local = Path(directory) / 'hyperos_avd_navigation-post-fs-data.sh'
            self.assertTrue(local.read_text().endswith(source[len('#!/system/bin/sh\n'):]))
        self.assertEqual(receipt[0]['flags'], ['disable'])
        self.assertEqual(receipt[0]['manifest_sha256'],
                         hashlib.sha256(json.dumps(navigation, sort_keys=True).encode()).hexdigest())
        write_commands = [args[-1] for args in calls if args[0] == 'shell' and 'cp /data/local/tmp/' in args[-1]]
        self.assertEqual(len(write_commands), 2)
        self.assertTrue(any(args[0] == 'push' for args in calls))
        self.assertTrue(all('/manifest.json.next' not in command and '/disable.next' not in command
                            and '/identity.prop.next' not in command for command in write_commands))

    def test_unknown_later_module_is_rejected_before_any_guest_file_push(self):
        navigation = {'revision': 10, 'property': 'ro.miui.product.home', 'value': 'com.miui.home',
                      'component': 'com.miui.home/com.miui.home.recents.RecentsActivity'}
        calls = []
        def adb(config, *args, **kwargs):
            calls.append(args)
            command = args[-1]
            output = ''
            if 'if [ -d ' in command:
                output = 'yes\n'
            elif '/module.prop' in command:
                module = ('hyperos_avd_flutter_render' if 'flutter_render' in command
                          else 'hyperos_avd_navigation')
                output = 'id=' + module + '\nauthor=HyperOS-AVD\n'
            elif '/manifest.json' in command:
                output = json.dumps({'revision': 88} if 'flutter_render' in command else navigation)
            elif args[0] == 'exec-out':
                output = '#!/system/bin/sh\nexit 0\n'
            return subprocess.CompletedProcess(args, 0, output, '')
        with tempfile.TemporaryDirectory() as directory, patch.object(common, 'adb', side_effect=adb):
            with self.assertRaisesRegex(RuntimeError, 'schema'):
                manage.guarded_modules({'port': 5584}, Path(directory), OLD)
        self.assertFalse(any(args[0] == 'push' for args in calls))

    def test_camera_revision_one_is_rejected_before_r3_guest_writes_and_revision_two_keeps_flags(self):
        module_id = 'hyperos_avd_xiaomi_camera'
        module = '/data/adb/modules/' + module_id
        saved = {'revision': 1, 'experimental': True, 'apk_sha256': LEGACY_PINS['camera_apk'],
                 'runtime_sha256': LEGACY_PINS['android_runtime'], 'targets': [
                     {'target': '/vendor/bin/hw/android.hardware.camera.provider@2.7-service-google'},
                     {'target': '/vendor/lib64/libgooglecamerahwl_impl.so'}]}
        for revision in (1, 2):
            with self.subTest(revision=revision), tempfile.TemporaryDirectory() as directory:
                calls = []
                manifest = {**saved, 'revision': revision}
                def adb(config, *args, **kwargs):
                    calls.append(args)
                    command = args[-1]
                    output = ''
                    if 'if [ -d ' + module in command:
                        output = 'yes\n'
                    elif 'cat ' + module + '/module.prop' in command:
                        output = 'id=' + module_id + '\nauthor=HyperOS-AVD\n'
                    elif 'cat ' + module + '/manifest.json' in command:
                        output = json.dumps(manifest) + '\n'
                    elif 'for flag in disable remove' in command:
                        output = 'disable\nremove\n'
                    elif args[0] == 'exec-out':
                        output = '#!/system/bin/sh\nexit 0\n'
                    return subprocess.CompletedProcess(args, 0, output, '')
                with patch.object(common, 'adb', side_effect=adb):
                    if revision == 1:
                        with self.assertRaisesRegex(RuntimeError, 'existing r2 firmware.*revision 2'):
                            manage.guarded_modules({'port': 5584}, Path(directory), OLD, NEW)
                        self.assertFalse(any(args[0] in ('push', 'exec-out') for args in calls))
                        self.assertFalse(list(Path(directory).iterdir()))
                    else:
                        receipt = manage.guarded_modules({'port': 5584}, Path(directory), OLD, NEW)
                        self.assertEqual(receipt[0]['revision'], 2)
                        self.assertEqual(receipt[0]['flags'], ['disable', 'remove'])
                        self.assertEqual(receipt[0]['manifest_sha256'],
                            hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest())
                        self.assertEqual(sum(args[0] == 'push' for args in calls), 2)
                guest_writes = [args[-1] for args in calls if args[0] == 'shell' and '.next' in args[-1]]
                self.assertTrue(all('/manifest.json.next' not in command and '/disable.next' not in command
                                    and '/remove.next' not in command for command in guest_writes))

    def test_camera_revision_one_remains_supported_outside_r3_preflight(self):
        module_id = 'hyperos_avd_xiaomi_camera'
        manage.validate_owned_module(module_id, 'id=' + module_id + '\nauthor=HyperOS-AVD\n',
            {'revision': 1, 'experimental': True, 'apk_sha256': LEGACY_PINS['camera_apk'],
             'runtime_sha256': LEGACY_PINS['android_runtime'], 'targets': [
                 {'target': '/vendor/bin/hw/android.hardware.camera.provider@2.7-service-google'},
                 {'target': '/vendor/lib64/libgooglecamerahwl_impl.so'}]})

    def test_firmware_gate_executes_only_on_original_firmware_and_is_idempotent(self):
        guarded = manage.migration_guard('#!/system/bin/sh\nprintf migrated\n', OLD)
        self.assertEqual(manage.migration_guard(guarded, OLD), guarded)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            getprop = root / 'getprop'
            getprop.write_text('#!/bin/sh\nprintf "%s" "$TEST_FIRMWARE"\n')
            getprop.chmod(0o755)
            script = root / 'module.sh'
            script.write_text(guarded)
            for firmware, expected in ((OLD, 'migrated'), ('OS' + NEW, '')):
                output = subprocess.check_output(['sh', str(script)], text=True,
                    env={**os.environ, 'PATH': str(root) + os.pathsep + os.environ['PATH'],
                         'TEST_FIRMWARE': firmware})
                self.assertEqual(output, expected)
        for invalid in ('#!/bin/sh\nexit 0\n', '#!/system/bin/sh\n\x00'):
            with self.assertRaisesRegex(RuntimeError, 'script'):
                manage.migration_guard(invalid, OLD)

    def test_owned_module_validation_rejects_unrelated_code_and_automatic_mounts(self):
        module = 'hyperos_avd_navigation'
        prop = 'id=' + module + '\nauthor=HyperOS-AVD\n'
        manifest = {'revision': 10, 'property': 'ro.miui.product.home', 'value': 'com.miui.home',
                    'component': 'com.miui.home/com.miui.home.recents.RecentsActivity',
                    'serial_number': '69704/F5XA01467'}
        manage.validate_owned_module(module, prop, manifest, 'persist.miui.home_sf_anim=true\n')
        for kwargs in ({'has_system': True}, {'system_prop': 'ro.build.fingerprint=old\n'}):
            with self.assertRaises(RuntimeError):
                manage.validate_owned_module(module, prop, manifest, **kwargs)
        for wrong in ({**manifest, 'revision': 50}, {**manifest, 'component': 'com.android.launcher3/.Home'}):
            with self.assertRaisesRegex(RuntimeError, 'schema'):
                manage.validate_owned_module(module, prop, wrong)
        with self.assertRaisesRegex(RuntimeError, 'ownership'):
            manage.validate_owned_module(module, prop.replace('HyperOS-AVD', 'other'), manifest)
        flutter = 'hyperos_avd_flutter_render'
        manage.validate_owned_module(flutter, 'id=' + flutter + '\nauthor=HyperOS-AVD\n',
            {'revision': 6, 'packages': {'com.miui.home': '/product/Home.apk'},
             'system': {'target': '/system_ext/lib64/libhyper_os_flutter.so',
                        'before': LEGACY_PINS['flutter'], 'after': PROFILES[LEGACY_PINS['flutter']]['output']}})
        camera = 'hyperos_avd_xiaomi_camera'
        manage.validate_owned_module(camera, 'id=' + camera + '\nauthor=HyperOS-AVD\n',
            {'revision': 2, 'experimental': True, 'apk_sha256': LEGACY_PINS['camera_apk'],
             'runtime_sha256': LEGACY_PINS['android_runtime'], 'targets': [
                {'target': '/vendor/bin/hw/android.hardware.camera.provider@2.7-service-google'},
                {'target': '/vendor/lib64/libgooglecamerahwl_impl.so'}]})

    def test_r3_requires_a_known_upgrade_source_and_same_family_key(self):
        old = {'version': R2, 'variant': 'os4-official', 'format': 3, 'hyperos': '4.0.17.0.XFRCNXM',
               'compatibility': {'userdata_family': 'os4-hongkong-api37-ranchu-4k'},
               'files': {'images/encryptionkey.img': {'sha256': 'same'}}}
        new = r3_manifest({**old, 'build': {}})
        manage.compatible(old, new)
        self.assertTrue(manage.firmware_change(old, new))
        with self.assertRaisesRegex(RuntimeError, 'validated'):
            manage.compatible({**old, 'version': 'v0.2.0-a17-hyperos4-hongkong-r1'}, new)
        with self.assertRaisesRegex(RuntimeError, 'migration'):
            manage.compatible(old, {**new, 'compatibility': {
                **new['compatibility'], 'module_upgrade_preflight': 'unknown'}})

    def test_failed_staging_restores_original_userdata_before_switch(self):
        helper = test_manager.ManagerTests()
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            path, original = helper.bundle(folder / 'bundle')
            new = r3_manifest(original)
            path.write_text(json.dumps(new))
            root = folder / 'instance'
            before = helper.legacy(root, original)
            old_path = root / 'local/installed-release.json'
            old = json.loads(old_path.read_text())
            old.update(format=3, version=R2, compatibility=original['compatibility'])
            old_path.write_text(json.dumps(old))
            def fail_stage(*args):
                self.assertTrue((root / 'local/upgrade-pending.json').is_file())
                (root / 'avd/Test_temp.avd/userdata-qemu.img').write_bytes(b'staging changed data')
                raise RuntimeError('injected staging failure')
            with patch('phone_profile.profile_from_build', return_value={'archive_sha256': ARCHIVES[NEW]}), \
                    patch('userdata_resize.check_dependencies', return_value={}), \
                    patch.object(manage, 'prepare_module_upgrade', side_effect=fail_stage) as stage:
                with self.assertRaisesRegex(RuntimeError, 'injected staging'):
                    helper.invoke(root, path, folder / 'registry')
            stage.assert_called_once()
            self.assertEqual((root / 'images/system.img').read_bytes(), b'old firmware')
            for name, value in before.items():
                self.assertEqual((root / 'avd/Test_temp.avd' / name).read_bytes(), value)
            self.assertFalse((root / 'local/upgrade-pending.json').exists())
            self.assertEqual(json.loads(old_path.read_text())['version'], R2)

    def test_direct_setup_refuses_retained_data_before_extracting_new_image(self):
        helper = test_manager.ManagerTests()
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            path, original = helper.bundle(folder / 'bundle')
            path.write_text(json.dumps(r3_manifest(original)))
            root = folder / 'instance'
            before = helper.legacy(root, original)
            with patch.object(setup, 'ROOT', root), \
                    patch('phone_profile.profile_from_build', return_value={'archive_sha256': ARCHIVES[NEW]}):
                with self.assertRaisesRegex(RuntimeError, 'Installer 1.2.0 Upgrade'):
                    setup.install_bundle(str(path))
            self.assertEqual((root / 'images/system.img').read_bytes(), b'old firmware')
            self.assertEqual((root / 'avd/Test_temp.avd/userdata-qemu.img').read_bytes(), before['userdata-qemu.img'])
            self.assertFalse((root / 'downloads').exists())

    def test_direct_setup_refuses_userdata_without_verified_current_build(self):
        helper = test_manager.ManagerTests()
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            path, original = helper.bundle(folder / 'bundle')
            path.write_text(json.dumps(r3_manifest(original)))
            root = folder / 'instance'
            before = helper.legacy(root, original)
            build_path = root / 'local/build.json'
            for saved in (None, {}, {'source': common.OS4_SOURCE, 'hyperos': NEW},
                          {'source': common.OS4_SOURCE, 'hyperos': NEW, 'archive_sha256': 'f' * 64},
                          {'source': 'official-yingtian-ota', 'hyperos': NEW,
                           'archive_sha256': ARCHIVES[NEW]}):
                with self.subTest(saved=saved):
                    if saved is None:
                        build_path.unlink(missing_ok=True)
                    else:
                        build_path.write_text(json.dumps(saved))
                    with patch.object(setup, 'ROOT', root):
                        with self.assertRaisesRegex(RuntimeError, 'Installer 1.2.0 Upgrade or Recover'):
                            setup.install_bundle(str(path))
                    self.assertEqual((root / 'images/system.img').read_bytes(), b'old firmware')
                    self.assertEqual((root / 'avd/Test_temp.avd/userdata-qemu.img').read_bytes(),
                                     before['userdata-qemu.img'])
                    self.assertFalse((root / 'downloads').exists())

    def test_direct_setup_accepts_verified_same_firmware_userdata(self):
        helper = test_manager.ManagerTests()
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            path, original = helper.bundle(folder / 'bundle')
            path.write_text(json.dumps(r3_manifest(original)))
            root = folder / 'instance'
            helper.legacy(root, original)
            (root / 'local/build.json').write_text(json.dumps({
                'source': common.OS4_SOURCE, 'hyperos': NEW, 'archive_sha256': ARCHIVES[NEW]}))
            with patch.object(setup, 'ROOT', root), \
                    patch.object(setup, 'validate_memory', side_effect=RuntimeError('passed userdata guard')):
                with self.assertRaisesRegex(RuntimeError, 'passed userdata guard'):
                    setup.install_bundle(str(path))

    def test_r3_manifest_rejects_missing_preflight_or_old_installer_requirement(self):
        helper = test_manager.ManagerTests()
        with tempfile.TemporaryDirectory() as directory:
            path, original = helper.bundle(Path(directory) / 'bundle')
            for field, invalid in (('minimum_installer', '1.1.0'),
                                   ('module_upgrade_preflight', None), ('upgrade_from', [])):
                value = r3_manifest(original)
                value['compatibility'][field] = invalid
                path.write_text(json.dumps(value))
                with patch('phone_profile.profile_from_build', return_value={'archive_sha256': ARCHIVES[NEW]}):
                    with self.assertRaisesRegex(RuntimeError, 'r3 upgrade'):
                        setup.read_manifest(str(path))


if __name__ == '__main__':
    unittest.main()
