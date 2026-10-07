"""Check scoped boot-service guards and supplied firmware artifacts."""
import io
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'scripts'))
import patch_boot_services as fixes
import apply_boot_service_fix as installer
from package_release import verify_boot_services
from phone_profile import profile
from os4_defaults import boot_defaults


def archive():
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as zipped:
        zipped.writestr('classes.dex', b'original')
    return output.getvalue()


def signed_fixture(data):
    end, directory = fixes.zip_directory(data)
    payload = b'original signer metadata'
    length = len(payload) + 24
    block = struct.pack('<Q', length) + payload + struct.pack('<Q', length) + b'APK Sig Block 42'
    result = bytearray(data[:directory] + block + data[directory:])
    struct.pack_into('<I', result, end + len(block) + 16, directory + len(block))
    return bytes(result)


class BootServiceTests(unittest.TestCase):
    def test_boot_service_requires_a_narrow_init_transition(self):
        self.assertIn(b'seclabel u:r:su:s0', fixes.BOOT_INIT)
        self.assertIn(b'(allow init su (process (transition)))', fixes.BOOT_SEPOLICY)
        self.assertNotIn(b'permissive', fixes.BOOT_SEPOLICY)

    def test_unknown_input_rejected_before_tools_and_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / 'source.apk', Path(directory) / 'output.apk'
            source.write_bytes(b'unknown')
            with patch.object(fixes.subprocess, 'run') as command:
                with self.assertRaisesRegex(RuntimeError, 'Unsupported boot service input'):
                    fixes.patch('registration', source, output)
                command.assert_not_called()
            self.assertFalse(output.exists())

    def test_modified_zip_retains_exact_original_signer_block(self):
        source = signed_fixture(archive())
        modified = io.BytesIO()
        with zipfile.ZipFile(modified, 'w') as zipped:
            zipped.writestr('classes.dex', b'new bytecode with different length')
        result = fixes.retain_system_signer(source, modified.getvalue())
        self.assertEqual(fixes.signing_block(source), fixes.signing_block(result))
        with zipfile.ZipFile(io.BytesIO(result)) as zipped:
            self.assertEqual(zipped.read('classes.dex'), b'new bytecode with different length')
            self.assertIsNone(zipped.testzip())
        with self.assertRaisesRegex(RuntimeError, 'unexpectedly contains'):
            fixes.retain_system_signer(source, result)

    def test_signer_and_zip_layout_fail_closed(self):
        for data in (b'not an APK', archive(), archive()[:-1]):
            with self.assertRaises(RuntimeError):
                fixes.signing_block(data)
        bad = bytearray(signed_fixture(archive()))
        _, directory = fixes.zip_directory(bad)
        struct.pack_into('<Q', bad, directory - 24, 2**63)
        with self.assertRaises(RuntimeError):
            fixes.signing_block(bad)

    def test_round_trip_compares_targets_and_instructions(self):
        old = '.method private run()V\n.registers 2\nif-eqz v0, :alias\n:alias\n:exit\nreturn-void\n.end method'
        same = old.replace(':alias', ':cond_4').replace(':exit\n', '')
        self.assertEqual(fixes.method_code(old, 'run()V'), fixes.method_code(same, 'run()V'))
        self.assertNotEqual(fixes.method_code(old, 'run()V'),
                            fixes.method_code(same.replace('v0', 'v1'), 'run()V'))

    def test_only_hongkong18_gets_kernel_boot_policy(self):
        empty = b'on boot\n    setprop example 1\n'
        self.assertNotIn(fixes.BOOT_INIT, boot_defaults(empty, profile('4.0.17.0.XFRCNXM')))
        current = boot_defaults(empty, profile('4.0.18.0.XFRCNXM'))
        self.assertEqual(current.count(fixes.BOOT_INIT), 1)
        self.assertEqual(boot_defaults(current, profile('4.0.18.0.XFRCNXM')), current)

    def test_capability_policy_distinguishes_supported_missing_and_denied(self):
        script = (REPO / 'config/check_kernel_services.sh').read_text()
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            events, node = folder / 'events', folder / 'iorap_dev'
            for name, body in {
                'getprop': 'case "$1" in ro.boot.hardware) echo "$HARDWARE";; ro.product.device) echo "$DEVICE";; ro.mi.os.version.incremental) echo "$FIRMWARE";; ro.millet.netlink) echo 31;; esac',
                'setprop': 'echo "$*" >> "$EVENTS"',
                'probe': 'if [ "$1" = gnss-extension ]; then exit "$GNSS_RESULT"; fi\nexit "$PROBE_RESULT"',
            }.items():
                path = folder / name
                path.write_text('#!/bin/sh\n' + body + '\n')
                path.chmod(0o755)
            env = dict(os.environ, PATH=str(folder) + ':' + os.environ['PATH'], EVENTS=str(events),
                       HARDWARE='ranchu', DEVICE='hongkong', FIRMWARE='OS4.0.18.0.XFRCNXM', GNSS_RESULT='0')
            command = script.replace('/dev/iorap_dev', str(node)).replace(
                'PROBE=${1:-/system/bin/hyperos_kernel_probe}', 'PROBE=' + str(folder / 'probe'))
            for result, present in ((0, True), (2, False), (3, True)):
                with self.subTest(result=result):
                    events.unlink(missing_ok=True)
                    if present:
                        node.touch()
                    else:
                        node.unlink(missing_ok=True)
                    run = subprocess.run(['sh'], input=command, text=True,
                                         env={**env, 'PROBE_RESULT': str(result)})
                    changes = events.read_text() if events.exists() else ''
                    self.assertEqual(run.returncode, 1 if result == 3 else 0)
                    self.assertEqual('ctl.stop millet_monitor' in changes, result == 2)
                    self.assertEqual('ctl.stop iorapd' in changes, not present)
                    if result == 0:
                        self.assertIn('sys.hyperos_avd.millet_supported 1', changes)
                    self.assertNotIn('ctl.stop loc_sys_service', changes)
            for result in (2, 3):
                events.unlink(missing_ok=True)
                node.touch()
                run = subprocess.run(['sh'], input=command, text=True,
                    env={**env, 'PROBE_RESULT': '0', 'GNSS_RESULT': str(result)})
                changes = events.read_text()
                self.assertEqual(run.returncode, 1 if result == 3 else 0)
                self.assertEqual('ctl.stop loc_sys_service' in changes, result == 2)
                self.assertNotIn('ctl.stop vendor.gnss.ranchu', changes)
            for field, value in (('DEVICE', 'yingtian'), ('HARDWARE', 'qcom'),
                                 ('FIRMWARE', 'OS4.0.17.0.XFRCNXM')):
                events.unlink(missing_ok=True)
                subprocess.run(['sh'], input=command, text=True,
                               env={**env, field: value, 'PROBE_RESULT': '2'}, check=True)
                self.assertFalse(events.exists())

    def test_existing_module_payload_is_verified_before_reuse(self):
        device = {'name': 'Renamed-by-user', 'port': 5584}
        import json
        calls = []
        def root(config, command):
            self.assertEqual(config, device)
            calls.append(command)
            if command.startswith('if [ -d '):
                return json.dumps(installer.receipt())
            for name, (path, _, _) in fixes.TARGETS.items():
                if command == 'pm path ' + installer.PACKAGES.get(name, '-'):
                    return 'package:' + path
                if command == 'sha256sum ' + path:
                    return fixes.AFTER[name] + '  ' + path
                if command == f'sha256sum {installer.MODULE}/payload/{name}':
                    return 'f' * 64 + '  modified-payload'
            raise AssertionError('Unexpected mutation: ' + command)
        def digest(path):
            return fixes.AFTER[Path(path).stem]
        with patch.object(installer, 'official'), \
                patch.object(installer, 'profile_from_build', return_value=profile('4.0.18.0.XFRCNXM')), \
                patch.object(installer, 'sha256', side_effect=digest), \
                patch.object(installer, 'root', side_effect=root), patch.object(installer, 'adb') as adb:
            with self.assertRaisesRegex(RuntimeError, 'Existing boot service payload differs'):
                installer.install(device, Path('unused-payload'))
            adb.assert_not_called()
        self.assertFalse(any('mkdir' in call or 'mount ' in call for call in calls))

    def test_release_rejects_wrong_version_and_claimed_but_missing_repair(self):
        reader = unittest.mock.Mock(return_value=b'wrong artifact')
        self.assertEqual(verify_boot_services({}, reader), 0)
        reader.assert_not_called()
        with self.assertRaisesRegex(RuntimeError, 'Unexpected boot service fix'):
            verify_boot_services({'hyperos': '4.0.17.0.XFRCNXM',
                                  'boot_service_fix': installer.receipt()}, reader)
        with self.assertRaisesRegex(RuntimeError, 'Baked boot service checksum'):
            verify_boot_services({'hyperos': '4.0.18.0.XFRCNXM',
                                  'boot_service_fix': installer.receipt()}, reader)

    def test_real_artifacts_preserve_all_unrelated_entries_and_signers(self):
        location = os.environ.get('HYPEROS_AVD_BOOT_FIX_ARTIFACTS')
        if not location:
            self.skipTest('Pinned firmware artifacts not supplied.')
        folder = Path(location)
        for name, (_, before, dex) in fixes.TARGETS.items():
            suffix = '.apk' if name in ('qualcomm', 'registration') else '.jar'
            source, output = folder / 'input' / (name + suffix), folder / 'output' / (name + suffix)
            self.assertEqual(fixes.sha256(source), before)
            self.assertEqual(fixes.sha256(output), fixes.AFTER[name])
            with zipfile.ZipFile(source) as old, zipfile.ZipFile(output) as new:
                self.assertEqual(old.namelist(), new.namelist())
                self.assertNotEqual(old.read(dex), new.read(dex))
                for entry in old.namelist():
                    if entry != dex:
                        self.assertEqual(old.read(entry), new.read(entry), entry)
            if suffix == '.apk':
                self.assertEqual(fixes.signing_block(source.read_bytes()), fixes.signing_block(output.read_bytes()))


if __name__ == '__main__':
    unittest.main()
