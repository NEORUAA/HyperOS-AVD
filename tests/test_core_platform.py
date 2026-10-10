"""Exercise shared platform capabilities and identity persistence in real shells."""
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import copy

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'scripts'))
import core_platform as platform
import core_legacy as legacy
import package_native_module
import test_native_module_runtime as fixture


class LegacyFlutterReceiptTests(unittest.TestCase):
    """Authenticate the frozen public r4 contract without proprietary binaries."""
    def setUp(self):
        import apply_flutter_fix as flutter
        from phone_profile import profile
        from patch_flutter import PROFILES
        from patch_weather import APK_SHA256
        selected = profile('4.0.18.0.XFRCNXM')
        self.before = selected['pins']['flutter']
        self.after = PROFILES[self.before]['legacy'][0]
        self.hook = '416cb8720a6b7b9e075d0fa607cf7bf42f5c2113bbb31aeb0ce61ca86f878cc0'
        weather = 'com.miui.weather2'
        paths = {'com.miui.home': '/product/priv-app/MiuiHome/MiuiHome.apk',
                 weather: '/product/app/MIUIWeather/MIUIWeather.apk'}
        hashes = {'com.miui.home': selected['pins']['home_apk'], weather: APK_SHA256}
        engine = 'd67e5c634800a97cd853fd425ded4752c43c2c879fe7269c8b9ffab6c5104836'
        self.manifest = {'revision': 7,
            'firmware': {'source': 'official-hongkong-ota', 'incremental': selected['incremental'],
                         'shared_input_sha256': self.before},
            'system': {'target': flutter.SYSTEM_LIB, 'before': self.before, 'after': self.after},
            'apks': [{'package': weather, 'apk_target': paths[weather], 'payload': weather + '.so',
                      'target': '/product/app/MIUIWeather/lib/arm64/libhyper_os_flutter.so',
                      'before': engine, 'after': PROFILES[engine]['output'], 'apk_sha256': APK_SHA256}],
            'packages': paths, 'apk_hashes': hashes, 'startup_script_sha256': self.hook}

    def assets(self, manifest=None):
        value = self.manifest if manifest is None else manifest
        return legacy.reviewed_assets('hyperos_avd_flutter_render', value, json.dumps(value))

    def inventory(self):
        # The frozen producer writes these canonical files. Hash pins for the
        # proprietary ELF and old hooks are checked without executing either.
        item = self.manifest['apks'][0]
        return {'manifest.json': platform.digest(json.dumps(self.manifest)),
                'flutter.so': self.after, item['payload']: item['after'],
                'post-fs-data.sh': self.hook, 'service.sh': self.hook,
                'module.prop': 'd738bf4d476f7fd6deaf77a286848b86fcff7c77e977472e7cca69acded3ee98',
                'targets.conf': platform.digest(f'SYSTEM_BEFORE={self.before}\nSYSTEM_AFTER={self.after}\n'),
                'apks.conf': platform.digest('|'.join(item[key] for key in
                    ('package', 'payload', 'apk_target', 'target', 'before', 'after', 'apk_sha256')) + '\n')}

    def inspect(self, inventory=None, lifecycle='present'):
        supplied = self.inventory() if inventory is None else inventory
        commands = []
        directory = '/data/adb/modules/hyperos_avd_flutter_render'
        def guest(config, command):
            commands.append(command)
            if 'elif [ -e ' in command:
                return lifecycle if directory + ' ]' in command else 'absent'
            if '__HYPEROS_RECORD_END__' in command:
                return 'present\n' + json.dumps(self.manifest) + '\n__HYPEROS_RECORD_END__'
            if '-mindepth 1 -type d' in command:
                return '\n'.join(checksum + '|' + name for name, checksum in sorted(supplied.items()))
            raise AssertionError('Unexpected command: ' + command)
        return legacy.inspect_legacy(guest, {}), commands

    def test_public_r4_revision_seven_authenticates_old_output_and_frozen_hooks(self):
        values = self.assets()
        self.assertEqual(values['flutter.so'], {self.after})
        self.assertEqual(values['post-fs-data.sh'], {self.hook})
        self.assertEqual(values['service.sh'], {self.hook})
        # Bind the property file to the reviewed historical revision, not Core's current producer.
        self.assertEqual(values['module.prop'],
                         {'d738bf4d476f7fd6deaf77a286848b86fcff7c77e977472e7cca69acded3ee98'})

    def test_complete_public_r4_inventory_is_eligible_for_retirement(self):
        items, commands = self.inspect()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['status'], 'audited')
        self.assertEqual(items[0]['reason'], 'exact-reviewed-owner')
        self.assertIn('snapshot', items[0])
        self.assertTrue(any('-mindepth 1 -type d' in command for command in commands))
        self.assertFalse(any('uninstall.sh' in command or 'sh /data/adb/modules/' in command for command in commands))

    def test_tampered_or_extra_payload_keeps_legacy_owner_and_conflicting_role(self):
        for changed in ('service.sh', 'flutter.so', 'module.prop', 'unreviewed.so'):
            inventory = self.inventory(); inventory[changed] = '0' * 64
            with self.subTest(changed=changed):
                items, commands = self.inspect(inventory)
                self.assertEqual(items[0]['status'], 'preserved')
                self.assertEqual(items[0]['features'], ['flutter'])
                self.assertNotIn('snapshot', items[0])
                self.assertFalse(any('\nmv ' in command for command in commands))

    def test_legacy_lifecycle_choices_are_not_bypassed_by_a_known_revision(self):
        for lifecycle in ('pending', 'disable\npresent', 'remove\npresent'):
            with self.subTest(lifecycle=lifecycle):
                items, commands = self.inspect(lifecycle=lifecycle)
                self.assertEqual(items[0]['status'], 'preserved')
                self.assertEqual(items[0]['reason'], 'legacy-lifecycle-choice')
                self.assertFalse(any('-mindepth 1 -type d' in command for command in commands))

    def test_current_receipt_remains_separate_from_the_frozen_contract(self):
        import apply_flutter_fix as flutter
        from patch_flutter import PROFILES
        value = copy.deepcopy(self.manifest)
        value.update(revision=flutter.REVISION, skipped_apks={})
        value['system']['after'] = PROFILES[self.before]['output']
        value['startup_script_sha256'] = platform.digest(flutter.boot_script(
            value['firmware']['source'], value['firmware']))
        self.assertIn(value['startup_script_sha256'], self.assets(value)['service.sh'])
        self.assertNotIn(self.hook, self.assets(value)['service.sh'])

    def test_unknown_revision_and_changed_historical_schema_are_rejected(self):
        for change in ({'revision': 6}, {'revision': 9}, {'revision': '7'}, {'revision': 7.0},
                       {'skipped_apks': {}}, {'unreviewed': True}):
            value = {**self.manifest, **change}
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'schema'):
                self.assets(value)
        value = copy.deepcopy(self.manifest); del value['apk_hashes']
        with self.assertRaisesRegex(ValueError, 'schema'):
            self.assets(value)

    def test_firmware_pin_and_hook_tampering_cannot_authorize_retirement(self):
        for field, changed in (('shared_input_sha256', '0' * 64),
                               ('incremental', 'OS4.0.17.0.XFRCNXM'), ('unreviewed', True)):
            value = copy.deepcopy(self.manifest); value['firmware'][field] = changed
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'provenance'):
                self.assets(value)
        value = copy.deepcopy(self.manifest); value['startup_script_sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'startup receipt'):
            self.assets(value)

    def test_native_hashes_must_belong_to_the_same_audited_edge(self):
        from patch_flutter import PROFILES
        for field, changed in (('before', '0' * 64), ('after', '0' * 64),
                               ('after', PROFILES['d67e5c634800a97cd853fd425ded4752c43c2c879fe7269c8b9ffab6c5104836']['output']),
                               ('after', PROFILES[self.before]['output'])):
            value = copy.deepcopy(self.manifest); value['system'][field] = changed
            with self.subTest(field=field, changed=changed), self.assertRaisesRegex(ValueError, 'system payload'):
                self.assets(value)

    def test_changed_package_inventory_or_payload_contract_is_rejected(self):
        for field, changed in (('payload', 'foreign.so'), ('before', '0' * 64),
                               ('after', '0' * 64), ('apk_sha256', '0' * 64),
                               ('unreviewed', True), ('external', False)):
            value = copy.deepcopy(self.manifest); value['apks'][0][field] = changed
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.assets(value)
        for field in ('packages', 'apk_hashes'):
            value = copy.deepcopy(self.manifest); value[field]['foreign'] = 'unknown'
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'package inventory'):
                self.assets(value)
        value = copy.deepcopy(self.manifest); value['apks'] *= 2
        with self.assertRaisesRegex(ValueError, 'APK receipt'):
            self.assets(value)


class CorePlatformShellTests(unittest.TestCase):
    setUp = fixture.NativeModuleRuntimeTests.setUp
    profile = fixture.NativeModuleRuntimeTests.profile
    file_metadata = fixture.NativeModuleRuntimeTests.file_metadata

    def run_platform(self, body, overrides='', check=True):
        state = self.root / 'persistent'
        state.mkdir(exist_ok=True)
        self.environment.update(HYPEROS_CORE_STATE=str(state), HYPEROS_CORE_PENDING=str(self.root / 'pending'),
                                HYPEROS_CORE_RESETPROP=str(self.bin / 'resetprop'))
        # Android BusyBox metadata uses the guest's root owner, not the host UID.
        busybox = fixture.BUSYBOX.replace(".replace('%u:%g', attrs.get('owner', os.environ.get('HYPEROS_NATIVE_TEST_OWNER', f'{value.st_uid}:{value.st_gid}'))))",
            ".replace('%u:%g', attrs.get('owner', '0:0')).replace('%u', attrs.get('owner', '0:0').split(':')[0]).replace('%h', str(value.st_nlink)))")
        self.bb.write_text(busybox)
        trace = self.root / 'effects'
        (self.bin / 'resetprop').write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> ' + shlex.quote(str(trace)) + '\n')
        (self.bin / 'resetprop').chmod(0o755)
        script = 'MODDIR=' + shlex.quote(str(self.module)) + '\n. "$MODDIR/runtime.sh"\n. "$MODDIR/platform.sh"\n'
        script += overrides + '\n' + body
        result = subprocess.run(['sh', '-c', script], env=self.environment, text=True,
                                capture_output=True, check=check)
        return result, trace.read_text() if trace.exists() else ''

    def test_serial_persists_across_ota_and_never_replays_a_device_table(self):
        state = self.root / 'persistent'; state.mkdir()
        record = platform.serial_record('69704/F5XA01467')
        (state / 'serial.record').write_text(record)
        inode = (state / 'serial.record').stat().st_ino
        _, effects = self.run_platform('core_serial; core_serial')
        self.assertEqual((state / 'serial.record').read_text(), record)
        self.assertEqual((state / 'serial.record').stat().st_ino, inode)
        self.assertEqual(effects.count('-n ro.ril.oem.psno 69704/F5XA01467'), 2)
        self.assertNotIn('hongkong', effects); self.assertNotIn('yingtian', effects)

    def test_invalid_serial_and_alias_are_preserved_without_property_changes(self):
        state = self.root / 'persistent'; state.mkdir()
        target = state / 'serial.record'; target.write_text('1|hyperos_avd_native_compat|unknown\n')
        result, effects = self.run_platform('core_serial', check=False)
        self.assertNotEqual(result.returncode, 0); self.assertEqual(effects, '')
        target.unlink(); target.symlink_to(self.root / 'foreign')
        result, effects = self.run_platform('core_serial', check=False)
        self.assertNotEqual(result.returncode, 0); self.assertEqual(effects, '')
        self.assertTrue(target.is_symlink())

    def test_disable_remove_and_pending_recheck_before_effects(self):
        state = self.root / 'persistent'; state.mkdir()
        (state / 'serial.record').write_text(platform.serial_record('69704/F5XA01467'))
        for flag in ('disable', 'remove'):
            path = self.module / flag; path.symlink_to(self.root / 'missing')
            _, effects = self.run_platform('core_serial; core_refresh; core_rear')
            self.assertEqual(effects, ''); path.unlink()
        (self.root / 'pending').mkdir()
        _, effects = self.run_platform('core_serial; core_refresh; core_rear')
        self.assertEqual(effects, '')

    def test_serial_new_uses_xiaomi_style_once_per_userdata(self):
        _, effects = self.run_platform('core_serial; core_serial')
        record = (self.root / 'persistent/serial.record').read_text().rstrip('\n')
        serial = record.split('|')[-1]
        self.assertRegex(serial, platform.SERIAL)
        self.assertEqual(effects.count('-n ro.serialno ' + serial), 2)

    def test_identity_is_data_and_whitelisted_public_keys_only(self):
        state = self.root / 'persistent'; state.mkdir()
        (state / 'image.tsv').write_text('identity|/system/build.prop|' + 'a' * 64 + '\n')
        source = self.root / 'build.prop'
        source.write_text('ro.product.model=Future Xiaomi\nro.build.fingerprint=Xiaomi/future:user/release-keys\n'
                          'ro.board.platform=vendor-hal\nro.hardware.camera=vendor-hal\nro.boot.serialno=foreign\n'
                          'ro.product.cpu.abilist=x86\nro.vendor.api_level=99\nmalicious=$(touch /tmp/never)\n')
        overrides = 'core_context_ready() { return 0; }\n'
        overrides += 'core_identity_rows() { "$BB" awk -F "=" \'$1=="ro.product.model" || $1=="ro.build.fingerprint" {print $1 "|" $2}\' ' + shlex.quote(str(source)) + '; }'
        _, effects = self.run_platform('core_identity', overrides)
        self.assertIn('-n ro.product.model Future Xiaomi', effects)
        self.assertNotIn('vendor-hal', effects); self.assertNotIn('foreign', effects); self.assertNotIn('x86', effects)
        # Also exercise the actual parser without property mutations.
        parser = (REPO / 'modules/native-compat/platform.sh').read_text().split('core_identity_rows() {', 1)[1].split('\ncore_identity() {', 1)[0]
        parser = 'core_identity_rows() {' + parser.replace('"$path"', shlex.quote(str(source)))
        result, _ = self.run_platform('core_identity_rows', parser)
        self.assertEqual(result.stdout.splitlines(), ['ro.product.model|Future Xiaomi', 'ro.build.fingerprint|Xiaomi/future:user/release-keys'])

    def test_unverified_identity_source_has_no_resetprop_side_effects(self):
        state = self.root / 'persistent'; state.mkdir()
        (state / 'image.tsv').write_text('identity|/system/build.prop|' + '0' * 64 + '\n')
        _, effects = self.run_platform('core_identity')
        self.assertEqual(effects, '')
        self.assertIn('unverified-image-defaults', (self.module / 'state/status.tsv').read_text())

    def test_refresh_requires_actual_primary_mode_and_keeps_settings_choices(self):
        overrides = '''getprop() { case "$1" in ro.boot.qemu.vsync) echo 60;; *) echo 1;; esac; }
dumpsys() { printf 'activeMode={id=0, hwcId=0, resolution=2272x3408, vsyncRate=60.00 Hz} '; if [ -f "$CORE_STATE/refreshed" ]; then echo 'renderRate=60.00 Hz'; else echo 'renderRate=20.00 Hz'; fi; }
service() { echo "$*" >> "$CORE_STATE/refresh-calls"; touch "$CORE_STATE/refreshed"; echo 'Result: Parcel(NULL)'; }
settings() { echo unexpected-settings-write >&2; return 1; }
'''
        self.run_platform('core_refresh; core_refresh', overrides)
        self.assertIn('compositor-60hz', (self.module / 'state/status.tsv').read_text())
        self.assertEqual((self.root / 'persistent/refresh-calls').read_text().splitlines(), ['call SurfaceFlinger 1035 i32 0'])
        self.run_platform('core_refresh', overrides.replace('id=0,', 'id=1,'))
        self.assertIn('unsupported-active-mode', (self.module / 'state/status.tsv').read_text())

    def test_rear_helper_does_not_start_for_tablet_or_unverified_resources(self):
        _, effects = self.run_platform('core_rear')
        self.assertEqual(effects, '')
        self.assertIn('physical-display-prerequisite', (self.module / 'state/status.tsv').read_text())
        self.assertFalse((self.root / 'persistent/rear.guard').exists())

    def test_persistent_conflict_gate_blocks_the_native_and_platform_owner(self):
        state = self.root / 'persistent'; state.mkdir()
        (state / 'legacy-blocked.features').write_text('flutter\nserial\n')
        result, effects = self.run_platform('native_enabled flutter && exit 9; core_serial')
        self.assertEqual(result.returncode, 0); self.assertEqual(effects, '')

    def test_identity_and_status_do_not_rehash_or_rewrite_unchanged_values(self):
        overrides = '''core_context_ready() { echo checked >> "$CORE_STATE/checks"; return 0; }
core_identity_rows() { printf 'ro.product.model|Future Xiaomi\\nro.build.fingerprint|Future/fingerprint\\n'; }
getprop() { case "$1" in ro.product.model) echo 'Future Xiaomi';; ro.build.fingerprint) echo 'Future/fingerprint';; esac; }
'''
        _, effects = self.run_platform('core_identity', overrides)
        status = self.module / 'state/status.tsv'; log = self.module / 'runtime.log'
        inode = status.stat().st_ino; original_log = log.read_bytes()
        self.run_platform('core_identity', overrides)
        self.assertEqual((self.root / 'persistent/checks').read_text().splitlines(), ['checked', 'checked'])
        self.assertEqual(status.stat().st_ino, inode); self.assertEqual(log.read_bytes(), original_log)
        self.assertEqual(effects, '')

    def test_boot_gate_requires_every_reviewed_image_prerequisite(self):
        from patch_boot_services import TARGETS, AFTER, PROBE_SHA256
        state = self.root / 'persistent'; state.mkdir()
        pins = {item[0]: AFTER[name] for name, item in TARGETS.items()}
        pins['/system/bin/hyperos_kernel_probe'] = PROBE_SHA256
        rows = ''.join('boot|' + path + '|' + checksum + '\n' for path, checksum in pins.items())
        (state / 'image.tsv').write_text(rows)
        overrides = 'core_safe_file() { return 0; }\nnative_ns_hash() { case "$2" in\n'
        overrides += ''.join(shlex.quote(path) + ') echo ' + checksum + ';;\n' for path, checksum in pins.items())
        overrides += 'esac; }'
        self.run_platform('core_context_ready boot', overrides)
        for changed in (rows.replace(PROBE_SHA256, '0' * 64), ''.join(rows.splitlines(True)[:-1]), rows + rows.splitlines(True)[0]):
            (state / 'image.tsv').write_text(changed)
            result, _ = self.run_platform('core_context_ready boot', overrides, check=False)
            self.assertNotEqual(result.returncode, 0)
            # The invalid gate precedes any probe execution or service control.
            self.run_platform('core_kernel', overrides)
            self.assertIn('unverified-image-prerequisites', (self.module / 'state/status.tsv').read_text())

    def rear_process(self, pid, start=42, owned=True):
        directory = self.proc / str(pid); directory.mkdir(exist_ok=True)
        (directory / 'cmdline').write_bytes(b'app_process\0' + (b'io.github.hyperosavd.RearDisplayWake' if owned else b'foreign.Main') + b'\0')
        (directory / 'environ').write_bytes(b'CLASSPATH=/system_ext/framework/rear-display-wake.jar\0')
        (directory / 'status').write_text('Uid:\t0\t0\t0\t0\n')
        # Field 22 is the process start tick, after a parenthesized comm.
        (directory / 'stat').write_text(f'{pid} (app process) ' + ' '.join(['S'] + ['0'] * 18 + [str(start)] + ['0'] * 20) + '\n')

    def test_rear_disable_stops_only_the_exact_adopted_process_lifetime(self):
        child = subprocess.Popen(['sleep', '30'])
        self.addCleanup(lambda: child.poll() is None and child.kill())
        self.rear_process(child.pid)
        state = self.root / 'persistent'; state.mkdir()
        (state / 'rear-daemon.record').write_text(f'1|test-boot|{child.pid}|42\n')
        (self.module / 'features.disabled').write_text('rear-wake\n')
        overrides = 'native_ns_hash() { echo d38aef457215fe9f5f397af2e58ece05c3baa61f742a8a9e3d7a3b577bd5c077; }'
        self.run_platform('core_rear', overrides)
        self.assertEqual(child.wait(timeout=2), -15)
        self.assertIn('verified-daemon-stopped', (self.module / 'state/status.tsv').read_text())

    def test_rear_missing_empty_alias_dead_boot_pid_reuse_and_foreign_are_preserved(self):
        state = self.root / 'persistent'; state.mkdir()
        file = state / 'rear-daemon.record'; self.rear_process(123)
        overrides = '''native_ns_hash() { echo d38aef457215fe9f5f397af2e58ece05c3baa61f742a8a9e3d7a3b577bd5c077; }
kill() { echo unwanted-signal >> "$CORE_STATE/signals"; }
'''
        for record in (None, '', '1|earlier-boot|123|42\n', '1|test-boot|123|99\n', '1|test-boot|124|42\n'):
            if record is None: file.unlink(missing_ok=True)
            else: file.write_text(record)
            self.run_platform('core_rear_cleanup', overrides)
        self.rear_process(124, owned=False)
        file.write_text('1|test-boot|124|42\n'); self.run_platform('core_rear_cleanup', overrides)
        file.unlink(); file.symlink_to(self.root / 'missing'); self.run_platform('core_rear_cleanup', overrides)
        self.assertFalse((state / 'signals').exists()); self.assertTrue(file.is_symlink())

    def test_rear_lease_serializes_start_adoption_and_cleanup(self):
        import fcntl
        state = self.root / 'persistent'; state.mkdir()
        self.rear_process(123)
        record = state / 'rear-daemon.record'; record.write_text('1|test-boot|123|42\n')
        inode = record.stat().st_ino
        overrides = '''core_rear_ready() { return 0; }
native_ns_hash() { echo d38aef457215fe9f5f397af2e58ece05c3baa61f742a8a9e3d7a3b577bd5c077; }
kill() { echo unwanted-signal >> "$CORE_STATE/signals"; }
'''
        with (state / 'rear.guard').open('a') as lease:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.run_platform('core_rear; core_rear_cleanup', overrides)
            self.assertEqual(record.stat().st_ino, inode)
            self.assertFalse((state / 'signals').exists())
        self.run_platform('core_rear', overrides)
        self.assertEqual(record.stat().st_ino, inode)
        self.assertIn('verified-existing-daemon', (self.module / 'state/status.tsv').read_text())


class CoreContextTests(unittest.TestCase):
    def context(self):
        return {'schema': 1, 'identity_sources': [{'path': '/system/build.prop', 'sha256': 'a' * 64}], 'rear': None}

    def test_context_is_data_and_never_part_of_the_common_module_artifact(self):
        value = self.context()
        self.assertIn('identity|/system/build.prop|' + 'a' * 64, platform.context_files(value)['image.tsv'])
        before = package_native_module.module_files()
        value['identity_sources'][0]['sha256'] = 'b' * 64
        platform.context_files(value)
        self.assertEqual(before, package_native_module.module_files())
        self.assertNotIn('serial.record', before); self.assertNotIn('image.tsv', before)

    def test_unknown_path_schema_alias_and_nonreviewed_boot_prerequisite_are_rejected(self):
        for change in ({'schema': 2}, {'identity_sources': [{'path': '/data/foreign.prop', 'sha256': 'a' * 64}]},
                       {'identity_sources': [{'path': '/system/../build.prop', 'sha256': 'a' * 64}]},
                       {'boot_services': {'files': [], 'probe': {'path': '/system/bin/foreign', 'sha256': 'a' * 64}}}):
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                platform.context_files({**self.context(), **change})

    def test_reviewed_phone_serial_hex_is_preserved_instead_of_regenerated(self):
        self.assertEqual(platform.serial_record('ABCDEF0123456789'), '1|hyperos_avd_native_compat|ABCDEF0123456789\n')
        with self.assertRaises(RuntimeError):
            platform.serial_record('unknown')


class CoreMigrationShellTests(CorePlatformShellTests):
    """Host-produced guards run against disposable Android-like directories."""
    # Only run migration-specific scenarios in this subclass.
    test_serial_persists_across_ota_and_never_replays_a_device_table = None
    test_invalid_serial_and_alias_are_preserved_without_property_changes = None
    test_disable_remove_and_pending_recheck_before_effects = None
    test_serial_new_uses_xiaomi_style_once_per_userdata = None
    test_identity_is_data_and_whitelisted_public_keys_only = None
    test_unverified_identity_source_has_no_resetprop_side_effects = None
    test_refresh_requires_actual_primary_mode_and_keeps_settings_choices = None
    test_rear_helper_does_not_start_for_tablet_or_unverified_resources = None
    test_persistent_conflict_gate_blocks_the_native_and_platform_owner = None
    test_identity_and_status_do_not_rehash_or_rewrite_unchanged_values = None
    test_boot_gate_requires_every_reviewed_image_prerequisite = None
    test_rear_disable_stops_only_the_exact_adopted_process_lifetime = None
    test_rear_missing_empty_alias_dead_boot_pid_reuse_and_foreign_are_preserved = None
    test_rear_lease_serializes_start_adoption_and_cleanup = None

    def setUp(self):
        super().setUp()
        self.run_platform(':')
        self.registry = self.root / 'data/modules'
        self.registry.mkdir(parents=True)
        self.android_data = self.root / 'android-data'; self.android_data.mkdir()
        self.file_metadata(self.android_data, owner='1000:1000')
        self.owner = self.registry / 'hyperos_avd_native_compat'
        self.owner.mkdir()
        files = package_native_module.module_files()
        for name, body in files.items():
            target = self.owner / name; target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(body)
        self.checksum = platform.digest(files['SHA256SUMS'])
        self.image = self.root / 'system/build.prop'; self.image.parent.mkdir()
        self.image.write_text('ro.product.model=Future Xiaomi\n')
        self.context = {'schema': 1, 'identity_sources': [{'path': '/system/build.prop', 'sha256': platform.digest(self.image.read_bytes())}], 'rear': None}

    def guest(self, config, command):
        command = command.replace('/data/adb/ksu/bin/busybox', str(self.bb))
        command = command.replace('/data/adb/hyperos-avd-core', str(self.root / 'persistent'))
        command = command.replace('/data/adb', str(self.root / 'data'))
        command = re.sub(r'(?<=\s)/data(?=[\s);]|$)', str(self.android_data), command)
        # Translate shell path operands, preserving quoted context bytes.
        command = re.sub(r'(?<=\s)/system/build\.prop(?=[\s);]|$)', str(self.image), command)
        result = subprocess.run(['sh', '-c', 'set -e\n' + command], env=self.environment,
                                capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError('Local authenticated guard rejected unsafe state: ' + result.stderr)
        return result.stdout.strip()

    def navigation(self):
        import apply_navigation_fix as nav
        from phone_profile import profile
        selected = profile('4.0.18.0.XFRCNXM'); serial = '69704/F5XA01467'
        identity = dict(selected['properties'], **{key: serial for key in ('ro.serialno', 'ro.boot.serialno', 'ro.ril.oem.psno')})
        manifest = {'revision': nav.REVISION, 'hyperos': selected['hyperos'], 'incremental': selected['incremental'],
                    'property': nav.PROPERTY, 'value': 'com.miui.home', 'component': nav.COMPONENT,
                    'animation_backend': 'stock-sf', 'sf_animation': True, 'phone_identity': identity,
                    'serial_number': serial}
        directory = self.registry / 'hyperos_avd_navigation'; directory.mkdir()
        data = {'manifest.json': json.dumps(manifest, indent=2) + '\n',
                'identity.prop': ''.join(key + '=' + value + '\n' for key, value in identity.items()),
                'system.prop': 'ro.miui.product.home=com.miui.home\npersist.miui.home_sf_anim=true\n',
                'module.prop': f'id=hyperos_avd_navigation\nname=HyperOS AVD native Quickstep\nversion={nav.REVISION}\nversionCode={nav.REVISION}\nauthor=HyperOS-AVD\ndescription=Original phone and launcher identity, persistent Xiaomi-style simulated serial and stock SF transitions\n',
                'post-fs-data.sh': nav.startup_script(nav.EARLY_SCRIPT, selected),
                'service.sh': nav.startup_script(nav.BOOT_SCRIPT, selected)}
        for name, value in data.items():
            (directory / name).write_text(value)
        return directory

    def test_known_navigation_is_retired_without_uninstall_or_payload_rewrites(self):
        directory = self.navigation()
        inode = (directory / 'identity.prop').stat().st_ino
        result = platform.configure(self.guest, {}, self.context, '/data/adb/modules/hyperos_avd_native_compat', self.checksum)
        self.assertEqual(result['serial']['serial_number'], '69704/F5XA01467')
        self.assertEqual(result['blocked_features'], [])
        self.assertFalse(directory.exists())
        archive = next((self.root / 'persistent/retired').glob('hyperos_avd_navigation-*'))
        self.assertEqual((archive / 'identity.prop').stat().st_ino, inode)
        self.assertEqual((self.root / 'persistent/serial.record').read_text(), platform.serial_record('69704/F5XA01467'))
        self.assertEqual((self.root / 'persistent/legacy-blocked.features').read_text(), '')

    def test_android_data_system_owner_is_allowed_but_managed_children_require_root(self):
        directory = self.navigation()
        # /data uid1000 is the Android contract, not an untrusted Core owner.
        self.guest({}, platform.module_guard('/data/adb/modules/hyperos_avd_native_compat', self.checksum)
                   + platform.parent_guard(platform.STATE))
        for path, owner in ((self.android_data, '1001:1001'), (self.root / 'data', '1000:1000')):
            self.file_metadata(path, owner=owner)
            with self.assertRaises(RuntimeError):
                platform.configure(self.guest, {}, self.context, '/data/adb/modules/hyperos_avd_native_compat', self.checksum)
            self.assertTrue(directory.exists())
            self.file_metadata(path, owner='1000:1000' if path == self.android_data else '0:0')

    def test_custom_hook_disable_remove_and_pending_are_preserved(self):
        directory = self.navigation()
        for mode in ('custom', 'disable', 'remove', 'pending'):
            with self.subTest(mode=mode):
                hook = directory / 'service.sh'; original = hook.read_bytes()
                if mode == 'custom': hook.write_bytes(original + b'\n# Local customization\n')
                elif mode == 'pending':
                    marker = self.root / 'data/modules_update/hyperos_avd_navigation'; marker.parent.mkdir(exist_ok=True); marker.mkdir()
                else:
                    marker = directory / mode; marker.symlink_to(self.root / 'missing')
                result = platform.configure(self.guest, {}, self.context, '/data/adb/modules/hyperos_avd_native_compat', self.checksum)
                self.assertTrue(directory.is_dir())
                self.assertIn('navigation', result['blocked_features'])
                if mode == 'custom': hook.write_bytes(original)
                elif mode == 'pending': marker.rmdir()
                else: self.assertTrue(marker.is_symlink()); marker.unlink()

    def test_unverified_core_or_image_cannot_retire_a_legacy_owner(self):
        directory = self.navigation()
        self.image.write_text('Changed outside reviewed image\n')
        with self.assertRaises(RuntimeError):
            platform.configure(self.guest, {}, self.context, '/data/adb/modules/hyperos_avd_native_compat', self.checksum)
        self.assertTrue(directory.is_dir()); self.assertFalse((self.root / 'persistent/image.tsv').exists())
        self.image.write_text('ro.product.model=Future Xiaomi\n')
        (self.owner / 'platform.sh').write_text('# Local change\n')
        with self.assertRaises(RuntimeError):
            platform.configure(self.guest, {}, self.context, '/data/adb/modules/hyperos_avd_native_compat', self.checksum)
        self.assertTrue(directory.is_dir())

    def test_unknown_directory_or_hard_link_is_preserved(self):
        directory = self.navigation(); (directory / 'foreign').mkdir()
        items = legacy.inspect_legacy(self.guest, {}, self.context)
        self.assertEqual(items[0]['status'], 'preserved')
        (directory / 'foreign').rmdir()
        os.link(directory / 'service.sh', self.root / 'foreign-hook')
        items = legacy.inspect_legacy(self.guest, {}, self.context)
        self.assertEqual(items[0]['status'], 'preserved')

    def test_global_refresh_is_archived_only_after_exact_owner_and_pin_verification(self):
        from os4_defaults import REFRESH_WRAPPER
        path = self.root / 'data/service.d/hyperos-avd-lock-fps.sh'; path.parent.mkdir(); path.write_bytes(REFRESH_WRAPPER)
        result = platform.configure(self.guest, {}, self.context, '/data/adb/modules/hyperos_avd_native_compat', self.checksum)
        self.assertFalse(path.exists())
        self.assertEqual(result['global_hooks'][0]['status'], 'retired')
        self.assertTrue(next((self.root / 'persistent/retired').glob('hyperos-avd-lock-fps.sh-*')).is_file())

    def test_unknown_platform_state_is_preserved_and_exact_partial_publication_recovers(self):
        directory = self.navigation()
        state = self.root / 'persistent'
        (state / 'image.json').write_text('{"schema": 99}\n')
        with self.assertRaisesRegex(RuntimeError, 'preserved'):
            platform.configure(self.guest, {}, self.context, '/data/adb/modules/hyperos_avd_native_compat', self.checksum)
        self.assertTrue(directory.is_dir()); self.assertEqual((state / 'image.json').read_text(), '{"schema": 99}\n')
        (state / 'image.json').unlink()
        (state / 'image.tsv').write_text(platform.context_files(self.context)['image.tsv'])
        result = platform.configure(self.guest, {}, self.context, '/data/adb/modules/hyperos_avd_native_compat', self.checksum)
        self.assertEqual(result['blocked_features'], [])
        self.assertFalse(directory.exists())
        self.assertEqual(json.loads((state / 'image.json').read_text()), self.context)


if __name__ == '__main__':
    unittest.main()
