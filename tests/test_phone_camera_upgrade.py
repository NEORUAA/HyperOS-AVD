"""Verify source-pinned camera ABI reuse and owned factory-app migration."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import apply_xiaomi_camera_fix as camera
from phone_profile import profile


def manifest(apk=camera.APK_HASH):
    return {'revision': 2, 'experimental': True, 'apk_sha256': apk,
            'runtime_sha256': camera.RUNTIME_HASH,
            'targets': [{'payload': payload, 'target': target, 'before': before,
                         'after': hashlib.sha256(payload.encode()).hexdigest()}
                        for payload, target, before in (
                            ('provider', camera.PROVIDER, camera.PROVIDER_HASH),
                            ('hwl.so', camera.HWL, camera.HWL_HASH))],
            'yuv_sha256': hashlib.sha256(b'yuv.so').hexdigest()}


class PhoneCameraUpgradeTests(unittest.TestCase):
    def test_only_audited_apk_and_framework_pairs_are_accepted(self):
        for version, digest in camera.CAMERA_VERSIONS.items():
            selected = camera.camera_inputs(profile(digest))
            self.assertEqual(selected['apk_sha256'], version)
            self.assertEqual(selected['native_abi']['jni_sha256'], camera.YUV_HASH)
            self.assertEqual(selected['native_abi']['capture_contract_sha256'], camera.CAPTURE_ABI)
        selected = profile('4.0.18.0.XFRCNXM')
        invalid = [copy.deepcopy(selected) for _ in range(3)]
        invalid[0]['pins']['camera_apk'] = 'f' * 64
        invalid[1]['pins']['android_runtime'] = 'e' * 64
        invalid[2]['incremental'] = 'OS4.0.17.0.XFRCNXM'
        for value in invalid:
            with self.assertRaisesRegex(RuntimeError, 'ABI profile'):
                camera.camera_inputs(value)

    def test_legacy_receipt_adopts_only_metadata_after_payload_and_source_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            prebuilt = workspace / 'tools/xiaomi-camera'
            prebuilt.mkdir(parents=True)
            saved = manifest()
            (prebuilt / 'manifest.json').write_text(json.dumps(saved))
            for name in ('provider', 'hwl.so', 'yuv.so'):
                (prebuilt / name).write_bytes(name.encode())
            receipt = {'sources': {name: camera.sha256(camera.REPO_ROOT / 'native' / name)
                                  for name in ('camera_hwl_roles.cpp', 'camera_yuv_planes.c')},
                       'files': {name: camera.sha256(prebuilt / name)
                                 for name in ('provider', 'hwl.so', 'yuv.so', 'manifest.json')}}
            (prebuilt / 'receipt.json').write_text(json.dumps(receipt))
            selected = profile('4.0.18.0.XFRCNXM')
            with patch.object(camera, 'ROOT', workspace), patch.object(camera.subprocess, 'run') as compile:
                newer = camera.build('/no-ndk', workspace / 'normalized', selected)
            compile.assert_not_called()
            self.assertEqual(newer['apk_sha256'], camera.APK18_HASH)
            self.assertEqual(newer['runtime_sha256'], camera.RUNTIME_HASH)
            self.assertEqual(newer['targets'], saved['targets'])
            self.assertEqual(newer['yuv_sha256'], saved['yuv_sha256'])
            for name in ('provider', 'hwl.so', 'yuv.so'):
                self.assertEqual((workspace / 'normalized' / name).read_bytes(), (prebuilt / name).read_bytes())
            self.assertEqual(json.loads((prebuilt / 'manifest.json').read_text()), saved)
            (prebuilt / 'hwl.so').write_bytes(b'changed payload')
            with patch.object(camera, 'ROOT', workspace):
                with self.assertRaisesRegex(RuntimeError, 'Invalid precompiled'):
                    camera.build('/no-ndk', workspace / 'invalid', selected)
            (prebuilt / 'hwl.so').write_bytes(b'hwl.so')
            receipt['sources']['camera_yuv_planes.c'] = '0' * 64
            (prebuilt / 'receipt.json').write_text(json.dumps(receipt))
            with patch.object(camera, 'ROOT', workspace):
                with self.assertRaisesRegex(RuntimeError, 'source differs'):
                    camera.build('/no-ndk', workspace / 'invalid-source', selected)

    def test_unknown_metadata_and_native_contract_are_rejected(self):
        for field, wrong in (('revision', 99), ('experimental', False), ('apk_sha256', 'f' * 64),
                             ('runtime_sha256', 'e' * 64), ('yuv_sha256', 'z' * 64),
                             ('native_abi', {'jni_sha256': 'e' * 64}),
                             ('firmware', {'source': 'official-yingtian-ota'})):
            with self.subTest(field=field):
                value = {**manifest(), field: wrong}
                with self.assertRaisesRegex(RuntimeError, 'precompiled camera'):
                    camera.normalized_manifest(value, profile('4.0.18.0.XFRCNXM'))

    def test_boot_script_checks_firmware_and_every_apk_before_hal_mount(self):
        selected = profile('4.0.18.0.XFRCNXM')
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            value = camera.normalized_manifest(manifest(), selected)
            camera.write_module_scripts(folder, value, selected)
            source = (folder / 'post-fs-data.sh').read_text()
            first_mount = source.index('mount -o bind')
            for guard in ('/disable', '/remove', selected['incremental'], camera.APK18_HASH,
                          '/system/lib64/libandroid_runtime.so', '# Validate every namespace'):
                self.assertLess(source.index(guard), first_mount)
            self.assertNotIn(camera.APK_HASH, source)
            self.assertNotIn('@INCREMENTAL@', source)
            for name in ('post-fs-data.sh', 'service.sh'):
                subprocess.run(['sh', '-n', str(folder / name)], check=True)

    def test_owned_camera_migration_preserves_flags_and_copies_new_signed_factory_app(self):
        calls, pushes = [], []
        saved = manifest()
        selected = profile('4.0.18.0.XFRCNXM')
        newer = camera.normalized_manifest(saved, selected)
        expected = {camera.MODULE + '/app/MiuiCamera.apk': saved['apk_sha256'],
                    camera.MODULE + '/app/lib/arm64/libcamera_yuv_jni.so': saved['yuv_sha256'],
                    **{camera.MODULE + '/' + item['payload']: item['after'] for item in saved['targets']}}
        def guest(config, command):
            self.assertEqual(config['port'], 5584)
            calls.append(command)
            if command == 'cat ' + camera.MODULE + '/module.prop':
                return 'id=hyperos_avd_xiaomi_camera\nauthor=HyperOS-AVD\n'
            if command == 'ls -A ' + camera.MODULE:
                return '\n'.join(['provider', 'hwl.so', 'app', 'manifest.json', 'module.prop',
                                  'post-fs-data.sh', 'service.sh', 'skip_mount', 'disable', 'remove'])
            if command.startswith('sha256sum '):
                return expected[command[len('sha256sum '):]] + '  payload'
            return ''
        def push(config, *args, **kwargs):
            self.assertEqual(config['port'], 5584)
            pushes.append(args)
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / 'local').mkdir()
            with patch.object(camera, 'ROOT', workspace), patch.object(camera, 'root', side_effect=guest), \
                    patch.object(camera, 'adb', side_effect=push):
                self.assertEqual(camera.migrate_camera_app({'port': 5584}, saved, newer, workspace, selected), newer)
            self.assertEqual(len(pushes), 8)
            transaction = next(command for command in calls if 'backup=/data/adb/' in command)
            self.assertIn('cp -a ' + camera.APP + '/.', transaction)
            self.assertIn(camera.APK18_HASH, transaction)
            self.assertIn('for flag in disable remove', transaction)
            self.assertIn('cp -p ' + camera.MODULE + '/$flag', transaction)
            self.assertIn("trap '[ -e " + camera.MODULE, transaction)
            self.assertNotIn('pm clear', transaction)
            self.assertNotIn('/data/user', transaction)
            subprocess.run(['sh', '-n'], input=transaction, text=True, check=True)

    def test_camera_migration_refuses_unknown_owned_layout_before_push(self):
        saved = manifest()
        selected = profile('4.0.18.0.XFRCNXM')
        def guest(config, command):
            if '/module.prop' in command:
                return 'id=hyperos_avd_xiaomi_camera\nauthor=HyperOS-AVD\n'
            if command.startswith('ls -A'):
                return 'provider\nhwl.so\napp\nmanifest.json\nmodule.prop\nsystem\n'
            return ''
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(camera, 'root', side_effect=guest), patch.object(camera, 'adb') as push:
            with self.assertRaisesRegex(RuntimeError, 'Unknown owned camera module layout'):
                camera.migrate_camera_app({'port': 5584}, saved,
                    camera.normalized_manifest(saved, selected), Path(directory), selected)
            push.assert_not_called()

    def test_retained_disabled_old_copy_migrates_before_disabled_fastpath(self):
        selected = profile('4.0.18.0.XFRCNXM')
        saved = manifest()
        newer = camera.normalized_manifest(saved, selected)
        calls = []
        config = {'name': 'User-renamed-phone', 'port': 5584, 'sdk': '/unused-sdk'}
        values = {'ro.boot.qemu.avd_name': config['name'], 'ro.boot.hardware': 'ranchu',
                  'ro.mi.os.version.incremental': selected['incremental']}
        hashes = {camera.APK: camera.APK18_HASH, '/system/lib64/libandroid_runtime.so': camera.RUNTIME_HASH,
                  '/vendor/lib64/libc++.so': camera.CPP_HASH}
        def guest(device, command):
            self.assertEqual(device, config)
            calls.append(command)
            if command.startswith('getprop '):
                return values[command[len('getprop '):]]
            if command.startswith('sha256sum '):
                return hashes[command[len('sha256sum '):]] + '  factory'
            if command == 'pm path com.android.camera':
                return 'package:' + camera.APK
            if command == 'cat ' + camera.MODULE + '/manifest.json':
                return json.dumps(saved)
            if command.startswith('if [ -d ') or '/disable' in command:
                return 'yes'
            return ''
        with patch.object(camera, 'official'), patch('phone_profile.profile_from_build', return_value=selected), \
                patch.object(camera, 'root', side_effect=guest), patch.object(camera, 'build', return_value=newer), \
                patch.object(camera, 'migrate_camera_app', return_value=newer) as migrate:
            self.assertEqual(camera.install(config), newer)
        migrate.assert_called_once()
        self.assertEqual(migrate.call_args.args[:3], (config, saved, newer))
        self.assertFalse(any('/disable' in command for command in calls))

    def test_unverified_app_store_camera_update_is_rejected_before_module_access(self):
        selected = profile('4.0.18.0.XFRCNXM')
        config = {'name': 'User-renamed-phone', 'port': 5584, 'sdk': '/unused-sdk'}
        values = {'ro.boot.qemu.avd_name': config['name'], 'ro.boot.hardware': 'ranchu',
                  'ro.mi.os.version.incremental': selected['incremental']}
        def guest(device, command):
            if command.startswith('getprop '):
                return values[command[len('getprop '):]]
            self.assertEqual(command, 'pm path com.android.camera')
            return 'package:/data/app/unsupported/base.apk'
        with patch.object(camera, 'official'), patch('phone_profile.profile_from_build', return_value=selected), \
                patch.object(camera, 'root', side_effect=guest), patch.object(camera, 'build') as build:
            with self.assertRaisesRegex(RuntimeError, 'update is unsupported'):
                camera.install(config)
        build.assert_not_called()


if __name__ == '__main__':
    unittest.main()
