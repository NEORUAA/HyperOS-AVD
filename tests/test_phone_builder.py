"""Verify phone OTA build isolation and the published hardware contract."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'scripts'))
import build_os4_official as builder
from build_os4_official import (archive_digest, avd_template, candidate_defaults,
                               native_hwui, phone_renderer, renderer_init,
                               renderer_properties, validate_candidate,
                               validate_workspace_firmware)
from phone_profile import ARCHIVES, profile


class PhoneBuilderIsolationTests(unittest.TestCase):
    def test_new_source_defaults_do_not_reuse_the_existing_phone_workspace(self):
        old = candidate_defaults({'hyperos': '4.0.17.0.XFRCNXM'}, REPO)
        new = candidate_defaults({'hyperos': '4.0.18.0.XFRCNXM'}, REPO)
        self.assertEqual(old, (REPO / 'work/os4-official', 'HyperOS_4_Official_API_37', 5574))
        self.assertEqual(new, (REPO / 'work/os4-r3-build', 'HyperOS_4_R3_API_37', 5584))
        self.assertTrue(all(previous != candidate for previous, candidate in zip(old, new)))
        with self.assertRaisesRegex(RuntimeError, 'No isolated build defaults'):
            candidate_defaults({'hyperos': 'unverified'}, REPO)

    def test_foreign_registration_is_refused_and_its_bytes_are_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            home, workspace = folder / 'registry', folder / 'new-workspace'
            home.mkdir()
            registry = home / 'Existing_Pixel.ini'
            original = ('avd.ini.encoding=UTF-8\npath=' + str(folder / 'another-workspace/Pixel.avd')
                        + '\ntarget=android-36\n').encode()
            registry.write_bytes(original)
            with self.assertRaisesRegex(RuntimeError, 'belongs to another workspace'):
                validate_candidate('Existing_Pixel', 5584, workspace, home)
            self.assertEqual(registry.read_bytes(), original)
            self.assertFalse(workspace.exists())

    def test_owned_or_new_registration_is_only_inspected(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            home, workspace = folder / 'registry', folder / 'new-workspace'
            home.mkdir()
            registry = home / 'My_Renamed_AVD.ini'
            original = ('path=' + str(workspace / 'avd/My_Renamed_AVD.avd') + '\n').encode()
            registry.write_bytes(original)
            validate_candidate('My_Renamed_AVD', 5584, workspace, home)
            validate_candidate('Another_Renamed_AVD', 5586, workspace, home)
            self.assertEqual(registry.read_bytes(), original)
            self.assertFalse((home / 'Another_Renamed_AVD.ini').exists())
            self.assertFalse(workspace.exists())

    def test_invalid_names_and_ports_cannot_escape_the_candidate(self):
        for name in ('../Pixel', 'AVD/Pixel', '', '-AVD', 'two words', 'a' * 65, None):
            with self.subTest(name=name), self.assertRaisesRegex(RuntimeError, 'ASCII AVD name'):
                validate_candidate(name, 5584, REPO / 'work/test')
        for port in (5554, 5557, 5684, True, '5584', None):
            with self.subTest(port=port), self.assertRaisesRegex(RuntimeError, 'even emulator console port'):
                validate_candidate('Renamed_AVD', port, REPO / 'work/test')

    def test_archive_digest_uses_the_exact_original_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'source.zip'
            data = bytes(range(256)) * 40000
            path.write_bytes(data)
            self.assertEqual(archive_digest(path), hashlib.sha256(data).hexdigest())
            path.write_bytes(data + b'changed')
            self.assertNotEqual(archive_digest(path), hashlib.sha256(data).hexdigest())

    def test_help_exposes_safe_controls_without_importing_a_source_profile(self):
        result = subprocess.run([sys.executable, str(REPO / 'scripts/build_os4_official.py'), '--help'],
                                check=True, capture_output=True, text=True, timeout=10)
        for option in ('--name', '--port', '--no-configure', '--workspace', '--partitions'):
            self.assertIn(option, result.stdout)

    def test_r3_build_refuses_retained_userdata_without_verified_matching_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / 'existing'
            avd = workspace / 'avd/User_Renamed.avd'
            avd.mkdir(parents=True)
            (avd / 'userdata-qemu.img.qcow2').write_bytes(b'retained encrypted userdata overlay')
            (workspace / 'local').mkdir()
            saved_path = workspace / 'local/build.json'
            current = {'source': 'official-hongkong-ota', 'hyperos': '4.0.18.0.XFRCNXM',
                       'archive_sha256': ARCHIVES['4.0.18.0.XFRCNXM']}
            invalid = (None, '{invalid', [], {},
                       {**current, 'hyperos': '4.0.17.0.XFRCNXM',
                        'archive_sha256': ARCHIVES['4.0.17.0.XFRCNXM']},
                       {**current, 'source': 'official-yingtian-ota'},
                       {**current, 'archive_sha256': 'f' * 64},
                       {key: value for key, value in current.items() if key != 'source'})
            for saved in invalid:
                with self.subTest(saved=saved):
                    if saved is None:
                        saved_path.unlink(missing_ok=True)
                    else:
                        saved_path.write_text(saved if isinstance(saved, str) else json.dumps(saved))
                    before = {path.relative_to(workspace): path.read_bytes()
                              for path in workspace.rglob('*') if path.is_file()}
                    with self.assertRaisesRegex(RuntimeError, 'Installer 1.2.0 Upgrade or Recover'):
                        validate_workspace_firmware(profile('4.0.18.0.XFRCNXM'), workspace)
                    self.assertEqual({path.relative_to(workspace): path.read_bytes()
                                      for path in workspace.rglob('*') if path.is_file()}, before)

    def test_same_r3_rebuild_and_fresh_workspace_remain_supported_without_writes(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / 'existing'
            validate_workspace_firmware(profile('4.0.18.0.XFRCNXM'), workspace)
            self.assertFalse(workspace.exists())
            avd = workspace / 'avd/User_Renamed.avd'
            avd.mkdir(parents=True)
            data = avd / 'userdata-qemu.img'
            data.write_bytes(b'retained encrypted userdata')
            (workspace / 'local').mkdir()
            saved_path = workspace / 'local/build.json'
            saved_path.write_text(json.dumps({'source': 'official-hongkong-ota',
                'hyperos': '4.0.18.0.XFRCNXM', 'archive_sha256': ARCHIVES['4.0.18.0.XFRCNXM']}))
            before = {path.relative_to(workspace): path.read_bytes()
                      for path in workspace.rglob('*') if path.is_file()}
            validate_workspace_firmware(profile('4.0.18.0.XFRCNXM'), workspace)
            self.assertEqual({path.relative_to(workspace): path.read_bytes()
                              for path in workspace.rglob('*') if path.is_file()}, before)

    def test_r2_rebuild_requires_matching_identity_and_refuses_downgrade(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / 'renamed-phone'
            avd = workspace / 'avd/Renamed.avd'
            avd.mkdir(parents=True)
            (avd / 'userdata-qemu.img').write_bytes(b'user data')
            (workspace / 'local').mkdir()
            receipt = workspace / 'local/build.json'
            for version in ('4.0.18.0.XFRCNXM', '4.0.17.0.XFRCNXM'):
                receipt.write_text(json.dumps({'source': 'official-hongkong-ota', 'hyperos': version,
                                               'archive_sha256': ARCHIVES[version]}))
                before = {path.relative_to(workspace): path.read_bytes()
                          for path in workspace.rglob('*') if path.is_file()}
                if version == '4.0.18.0.XFRCNXM':
                    with self.assertRaisesRegex(RuntimeError, 'verified matching'):
                        validate_workspace_firmware(profile('4.0.17.0.XFRCNXM'), workspace)
                else:
                    validate_workspace_firmware(profile(version), workspace)
                self.assertEqual({path.relative_to(workspace): path.read_bytes()
                                  for path in workspace.rglob('*') if path.is_file()}, before)

    def test_no_configure_cannot_bypass_guard_before_archive_extract_or_environment_change(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / 'existing-r2'
            avd = workspace / 'avd/User_Renamed.avd'
            avd.mkdir(parents=True)
            data = avd / 'userdata-qemu.img'
            data.write_bytes(b'retained encrypted userdata')
            (workspace / 'local').mkdir()
            saved_path = workspace / 'local/build.json'
            saved_path.write_text(json.dumps({'source': 'official-hongkong-ota',
                'hyperos': '4.0.17.0.XFRCNXM', 'archive_sha256': ARCHIVES['4.0.17.0.XFRCNXM']}))
            before = {path.relative_to(workspace): path.read_bytes()
                      for path in workspace.rglob('*') if path.is_file()}
            previous = os.environ.get('HYPEROS_AVD_WORKSPACE')
            with patch.object(sys, 'argv', ['build_os4_official.py', '--zip', str(Path(temporary) / 'r3.zip'),
                                          '--workspace', str(workspace), '--no-configure']), \
                    patch.object(builder, 'archive_digest', return_value=ARCHIVES['4.0.18.0.XFRCNXM']), \
                    patch.object(builder.zipfile, 'ZipFile') as archive, \
                    patch.object(builder.subprocess, 'run') as extract:
                with self.assertRaisesRegex(RuntimeError, '--no-configure'):
                    builder.main()
            archive.assert_not_called()
            extract.assert_not_called()
            self.assertEqual(os.environ.get('HYPEROS_AVD_WORKSPACE'), previous)
            self.assertEqual({path.relative_to(workspace): path.read_bytes()
                              for path in workspace.rglob('*') if path.is_file()}, before)


class PhoneBuilderHardwareTests(unittest.TestCase):
    def test_vulkan_is_selected_only_for_r3_and_legacy_r2_stays_reproducible(self):
        previous = {'hyperos': '4.0.17.0.XFRCNXM'}
        candidate = {'hyperos': '4.0.18.0.XFRCNXM'}
        self.assertEqual(phone_renderer(previous), 'skiagl')
        self.assertEqual(renderer_properties(previous),
                         b'ro.mediaserver.64b.enable=true\ndebug.hwui.renderer=skiagl\n'
                         b'debug.renderengine.backend=skiavkthreaded\n')
        self.assertEqual(renderer_init(previous),
                         b'    setprop debug.renderengine.backend skiavkthreaded\n')
        self.assertEqual(phone_renderer(candidate), 'skiavk')
        self.assertIn(b'debug.hwui.renderer=skiavk\n', renderer_properties(candidate))
        self.assertEqual(renderer_init(candidate),
                         b'    setprop debug.hwui.renderer skiavk\n'
                         b'    setprop debug.renderengine.backend skiavkthreaded\n')
        with self.assertRaisesRegex(RuntimeError, 'No verified phone renderer'):
            phone_renderer({'hyperos': 'unknown'})

    def test_native_replacement_refuses_an_unpinned_source_before_patching(self):
        from phone_profile import profile
        for version in ('4.0.17.0.XFRCNXM', '4.0.18.0.XFRCNXM'):
            with self.subTest(version=version), self.assertRaisesRegex(RuntimeError, 'HWUI source'):
                native_hwui(profile(version), b'unknown firmware native library')

    def test_both_firmware_versions_use_the_same_r2_hardware(self):
        original = (REPO / 'config/avd.ini').read_text()
        expected = {'target': 'android-37.0', 'hw.cpu.arch': 'arm64', 'hw.cpu.ncore': '4',
                    'hw.ramSize': '6144', 'disk.dataPartition.size': '32G',
                    'hw.lcd.width': '1120', 'hw.lcd.height': '2436', 'hw.lcd.density': '480',
                    'hw.camera.back': 'virtualscene', 'hw.camera.front': 'emulated',
                    'hw.audioOutput': 'yes', 'kernel.parameters': 'androidboot.selinux=enforcing'}
        for version in ('4.0.17.0.XFRCNXM', '4.0.18.0.XFRCNXM'):
            with self.subTest(version=version):
                profile = {'hyperos': version, 'display': {'width': 1120, 'height': 2436, 'density': 480}}
                template = avd_template(original, profile)
                values = dict(line.split('=', 1) for line in template.splitlines() if '=' in line)
                self.assertEqual({key: values[key] for key in expected}, expected)
                self.assertTrue(all(key not in values for key in ('path', 'path.rel', 'image.sysdir.1')))
        self.assertEqual((REPO / 'config/avd.ini').read_text(), original)


if __name__ == '__main__':
    unittest.main()
