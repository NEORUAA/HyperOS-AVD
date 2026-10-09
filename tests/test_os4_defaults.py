"""Guard OS4 user-build and official primary-display defaults."""
from pathlib import Path
from datetime import date
import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from os4_defaults import (AOD_INIT, GRADIENT_BLUR_INIT, REFRESH_INIT, REFRESH_SCRIPT, DISPLAY, MODEL_SHA256, MODEL_XML, boot_defaults, disable_debug_console,
                          display_template, identity_properties, production_properties)
from os4_defaults import apply_gradient_blur_runtime, apply_sensor_defaults
from os4_defaults import LOG_SCRIPT, LOG_TAGS, log_properties
from os4_defaults import AWAKE_STAMP, AWAKE_SCRIPT, SCREEN_TIMEOUT
from os4_defaults import apply_runtime, COMPONENT
import os4_defaults
from apply_navigation_fix import simulated_serial


class FindDeviceUpdateTests(unittest.TestCase):
    TREE = '''E: manifest (line=1)
  A: package="com.xiaomi.finddevice" (Raw: "com.xiaomi.finddevice")
  E: application (line=2)
    A: android:label(0x01010001)=@0x7f010001
    E: provider (line=3)
      A: android:name(0x01010003)=".v2.FindDeviceStatusManagerProvider"
      A: android:authorities(0x01010018)="com.xiaomi.finddevice.status"
      A: android:exported(0x01010010)=(type 0x12)0x0
      A: android:enabled(0x0101000e)=(type 0x12)0xffffffff
      A: android:permission(0x01010006)="com.xiaomi.permission.FIND_DEVICE"
'''

    def package(self, *, flags='SYSTEM UPDATED_SYSTEM_APP', choice='', enabled=0):
        text = ('  Package [com.xiaomi.finddevice] (123):\n'
                '    pkgFlags=[ ' + flags + ' ]\n'
                '    User 0: installed=true enabled=' + str(enabled) + '\n')
        if choice:
            text += '      ' + choice + 'Components:\n        ' + os4_defaults.PROVIDER + '\n'
        # A disabled factory package must not override the active user choice.
        return text + '\nHidden system packages:\n  Package [com.xiaomi.finddevice] (456):\n    User 0: enabled=3\n'

    def fixture(self, *, updated=True, choice='', enabled=0, flags='SYSTEM UPDATED_SYSTEM_APP',
                factory_bad=False, identities=None, raced=False, unavailable=False):
        factory, active = b'trusted factory APK', b'signed updated APK'
        factory_sha, active_sha = (hashlib.sha256(value).hexdigest() for value in (factory, active))
        apk = '/data/app/~~token/com.xiaomi.finddevice-token/base.apk' if updated else os4_defaults.FINDDEVICE_FACTORY_APK
        commands, reads = [], 0
        def root(config, command):
            nonlocal reads
            commands.append(command)
            if command.startswith('pm path '):
                reads += 1
                return 'package:' + (apk + '.replaced' if raced and reads > 1 else apk)
            if command.startswith('dumpsys package '):
                return self.package(choice=choice, enabled=enabled, flags=flags)
            if command.startswith('sha256sum '):
                return (('f' * 64 if factory_bad else factory_sha) if os4_defaults.FINDDEVICE_FACTORY_APK in command else active_sha) + '  apk'
            if command == 'pm disable --user 0 ' + COMPONENT:
                return 'Component ' + COMPONENT + ' new state: disabled'
            return ''
        def adb(config, action, remote, local, **kwargs):
            self.assertEqual(action, 'pull')
            Path(local).write_bytes(factory if remote == os4_defaults.FINDDEVICE_FACTORY_APK else active)
        identity = ('certificate', os4_defaults.finddevice_provider_contract(self.TREE))
        with patch('apply_flutter_fix.root', side_effect=root), patch('common.adb', side_effect=adb), \
                patch.object(os4_defaults, 'finddevice_apk_identity',
                             side_effect=RuntimeError('verification tools unavailable') if unavailable else (identities or [identity, identity])) as verify:
            result = os4_defaults.apply_finddevice_workaround({'sdk': '/unused'}, factory_sha)
        return result, commands, verify.call_count

    def test_verified_factory_and_updated_system_provider_use_only_component_workaround(self):
        for updated in (False, True):
            with self.subTest(updated=updated):
                result, commands, verified = self.fixture(updated=updated)
                self.assertEqual(result, 'applied')
                self.assertEqual(verified, 2 if updated else 0)
                self.assertEqual([command for command in commands if command.startswith('pm disable')],
                                 ['pm disable --user 0 ' + COMPONENT])

    def test_update_requires_factory_trust_system_provenance_and_stable_active_apk(self):
        for arguments in ({'factory_bad': True}, {'flags': 'SYSTEM'}, {'flags': 'UPDATED_SYSTEM_APP'},
                          {'raced': True}, {'unavailable': True}):
            with self.subTest(arguments=arguments):
                result, commands, _ = self.fixture(**arguments)
                self.assertEqual(result, 'skipped')
                self.assertFalse(any(command.startswith(('pm disable', 'am force-stop')) for command in commands))

    def test_update_signer_or_provider_capability_change_is_skipped(self):
        identity = ('certificate', os4_defaults.finddevice_provider_contract(self.TREE))
        for other in (('unrelated-certificate', identity[1]), ('certificate', ('changed',))):
            result, commands, _ = self.fixture(identities=[identity, other])
            self.assertEqual(result, 'skipped')
            self.assertFalse(any(command.startswith('pm disable') for command in commands))

    def test_explicit_component_and_package_choices_survive_updates(self):
        for choice, enabled, expected in (('enabled', 0, 'enabled'), ('disabled', 0, 'disabled'),
                                          ('', 1, 'explicit-package-choice'), ('', 3, 'explicit-package-choice')):
            result, commands, verified = self.fixture(choice=choice, enabled=enabled)
            self.assertEqual(result, expected)
            self.assertEqual(verified, 0)
            self.assertFalse(any(command.startswith(('pm disable', 'am force-stop')) for command in commands))

    def test_literal_provider_contract_ignores_unrelated_app_resources_and_normalizes_class(self):
        contract = os4_defaults.finddevice_provider_contract(self.TREE)
        self.assertEqual(contract, os4_defaults.finddevice_provider_contract(
            self.TREE.replace('".v2.FindDeviceStatusManagerProvider"', '"' + os4_defaults.PROVIDER + '"')))
        changed = self.TREE.replace('(type 0x12)0x0', '(type 0x12)0xffffffff')
        self.assertNotEqual(contract, os4_defaults.finddevice_provider_contract(changed))
        for text in (self.TREE.replace('com.xiaomi.finddevice" (Raw:', 'unrelated.package" (Raw:'),
                     self.TREE.replace('".v2.FindDeviceStatusManagerProvider"', '".OtherProvider"'),
                     self.TREE.replace('(type 0x12)0x0', '@0x7f010001')):
            with self.assertRaises(RuntimeError):
                os4_defaults.finddevice_provider_contract(text)

    def test_apk_identity_requires_successful_apksigner_and_parses_literal_provider(self):
        import common
        certificate = 'a' * 64
        with tempfile.TemporaryDirectory() as temporary:
            tools = Path(temporary) / 'build-tools/37.0.0'
            tools.mkdir(parents=True)
            (tools / 'apksigner').touch()
            (tools / 'aapt2').touch()
            with patch.object(common, 'sdk_path', return_value=Path(temporary)), \
                    patch('patch_gnss.java', return_value='/sdk/jbr/bin/java'), \
                    patch.object(os4_defaults.subprocess, 'run', side_effect=[
                        Mock(stdout='Signer #1 certificate SHA-256 digest: ' + certificate + '\n'),
                        Mock(stdout=self.TREE)]) as run:
                identity = os4_defaults.finddevice_apk_identity(Path(temporary) / 'apk', temporary)
            self.assertEqual(identity, ((certificate,), os4_defaults.finddevice_provider_contract(self.TREE)))
            self.assertEqual(run.call_args_list[0].args[0][1:3], ['verify', '--print-certs'])
            self.assertTrue(all(call.kwargs['check'] for call in run.call_args_list))
            with patch.object(common, 'sdk_path', return_value=Path(temporary)), \
                    patch('patch_gnss.java', return_value='/sdk/jbr/bin/java'), \
                    patch.object(os4_defaults.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, 'apksigner')):
                with self.assertRaises(subprocess.CalledProcessError):
                    os4_defaults.finddevice_apk_identity(Path(temporary) / 'apk', temporary)

    def test_active_package_state_wins_over_disabled_factory_block(self):
        text = self.package(choice='enabled')
        block = os4_defaults.finddevice_package_block(text)
        self.assertNotIn('Hidden system packages', block)
        self.assertEqual(os4_defaults.finddevice_user_choice(block), 'enabled')
        self.assertEqual(os4_defaults.finddevice_user_choice(''), 'unknown')
        short = text.replace(os4_defaults.PROVIDER, '.v2.FindDeviceStatusManagerProvider')
        short = short.replace('      enabledComponents:', '\n      enabledComponents:')
        self.assertEqual(os4_defaults.finddevice_user_choice(os4_defaults.finddevice_package_block(short)), 'enabled')


class OS4DefaultsTests(unittest.TestCase):
    def test_runtime_keeps_ac_supply_without_overwriting_seeded_awake_choices(self):
        import common
        import os4_defaults
        from phone_profile import profile
        selected = profile('4.0.18.0.XFRCNXM')
        config = {'name': 'Renamed-phone', 'port': 5584}
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / 'local').mkdir()
            (folder / 'local/defaults-before.json').write_text('{}')
            stamp, events = folder / 'stamp', folder / 'events'
            stamp.touch()
            events.write_text('timeout=123456\nsleep=654321\nstay=0\n')
            for name, code in (('am', 'exit 0'), ('settings', 'echo "$*" >> "$TEST_EVENTS"')):
                path = folder / name
                path.write_text('#!/bin/sh\n' + code + '\n')
                path.chmod(0o755)
            def root(device, command):
                self.assertEqual(device, config)
                if command == 'getprop ro.boot.hardware':
                    return 'ranchu'
                if command.startswith('pm path '):
                    return 'package:' + os4_defaults.FINDDEVICE_FACTORY_APK
                if command.startswith('dumpsys package '):
                    return '  Package [com.xiaomi.finddevice] (123):\n    User 0: enabled=0\n'
                if command.startswith('sha256sum '):
                    return selected['pins']['finddevice_apk'] + '  apk'
                if command == 'pm disable --user 0 ' + COMPONENT:
                    return 'Component ' + COMPONENT + ' new state: disabled'
                if AWAKE_SCRIPT in command:
                    subprocess.run(['sh', '-e'], input=command.replace(AWAKE_STAMP, str(stamp)),
                        text=True, check=True, env=dict(os.environ,
                            PATH=str(folder) + ':' + os.environ['PATH'], TEST_EVENTS=str(events)))
                return ''
            with patch.object(common, 'ROOT', folder), patch('apply_flutter_fix.official'), \
                    patch('phone_profile.profile_from_build', return_value=selected), \
                    patch('apply_flutter_fix.root', side_effect=root), patch('common.adb') as adb, \
                    patch.object(os4_defaults, 'apply_color_runtime'), \
                    patch.object(os4_defaults, 'apply_refresh_runtime'), \
                    patch.object(os4_defaults, 'apply_gradient_blur_runtime'):
                apply_runtime(config)
            self.assertEqual(events.read_text(), 'timeout=123456\nsleep=654321\nstay=0\n')
            self.assertEqual(adb.call_args.args, (config, 'emu', 'power', 'ac', 'on'))

    def test_unverifiable_finddevice_does_not_block_phone_awake_aod_or_other_defaults(self):
        import common
        from phone_profile import profile
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / 'local').mkdir()
            (folder / 'local/defaults-before.json').write_text('{}')
            calls = []
            def root(config, command):
                calls.append(command)
                return 'ranchu' if command == 'getprop ro.boot.hardware' else ''
            with patch.object(common, 'ROOT', folder), patch('apply_flutter_fix.official'), \
                    patch('phone_profile.profile_from_build', return_value=profile('4.0.18.0.XFRCNXM')), \
                    patch('apply_flutter_fix.root', side_effect=root), patch('common.adb') as adb, \
                    patch.object(os4_defaults, 'apply_color_runtime') as color, \
                    patch.object(os4_defaults, 'apply_refresh_runtime') as refresh, \
                    patch.object(os4_defaults, 'apply_gradient_blur_runtime') as blur:
                apply_runtime({'name': 'Renamed'})
            self.assertIn(AWAKE_SCRIPT, calls)
            self.assertTrue(any('doze_always_on 1' in command for command in calls))
            self.assertFalse(any('pm disable' in command for command in calls))
            self.assertEqual(adb.call_count, 1)
            for function in (color, refresh, blur):
                function.assert_called_once()

    def test_awake_defaults_seed_once_and_preserve_later_user_choices(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            stamp, events = folder / 'seeded', folder / 'events'
            settings = folder / 'settings'
            settings.write_text('#!/bin/sh\necho "$*" >> "$TEST_EVENTS"\n')
            settings.chmod(0o755)
            env = dict(os.environ, PATH=str(folder) + ':' + os.environ['PATH'], TEST_EVENTS=str(events))
            script = AWAKE_SCRIPT.replace(AWAKE_STAMP, str(stamp))
            subprocess.run(['sh', '-e'], input=script, text=True, env=env, check=True)
            self.assertEqual(events.read_text().splitlines(), [
                f'put system screen_off_timeout {SCREEN_TIMEOUT}',
                'put secure sleep_timeout -1', 'put global stay_on_while_plugged_in 7'])
            self.assertTrue(stamp.is_file())
            # A retained-user migration already marks this stamp and keeps the
            # user's timeout/sleep/plugged choices, regardless of their values.
            events.write_text('user timeout 123456\nuser sleep 654321\nuser stay 0\n')
            subprocess.run(['sh', '-e'], input=script, text=True, env=env, check=True)
            self.assertEqual(events.read_text(), 'user timeout 123456\nuser sleep 654321\nuser stay 0\n')

    def test_supplied_log_filter_changes_only_its_selected_tags(self):
        self.assertEqual(hashlib.sha256(LOG_SCRIPT).hexdigest(),
                         'b3dc26dc1ace9121ff4f9e71fe6526af933148e4a0e7d9f962b42395f294d6c7')
        self.assertEqual(len(LOG_TAGS), 17)
        source = b'log.tag.RenderEngine=D\nlog.tag.Other=V\nro.debuggable=0\n'
        patched = log_properties(source)
        for tag in LOG_TAGS:
            self.assertEqual(patched.splitlines().count(('log.tag.' + tag + '=S').encode()), 1)
        self.assertIn(b'log.tag.Other=V\n', patched)
        self.assertIn(b'ro.debuggable=0\n', patched)
        self.assertEqual(log_properties(patched), patched)
        with self.assertRaisesRegex(RuntimeError, 'Duplicate'):
            log_properties(source + b'log.tag.RenderEngine=E\n')

    def test_simulated_serial_is_unique_on_creation_and_preserved_on_update(self):
        with patch('apply_navigation_fix.date') as clock, \
                patch('apply_navigation_fix.secrets.randbelow', side_effect=[12345, 1466, 23456, 4356]) as random, \
                patch('apply_navigation_fix.secrets.choice', side_effect=['F', 'G']):
            clock.today.return_value = date(2026, 10, 4)
            first, second = simulated_serial(None), simulated_serial({'revision': 7})
            self.assertEqual(first, '22345/F6X401467')
            self.assertEqual(second, '33456/G6X404357')
            self.assertNotEqual(first, second)
            self.assertEqual(simulated_serial({'serial_number': first}), first)
            self.assertEqual(random.call_count, 4)
        for value in ('unknown', '123', 'bad\nvalue', 123, '12345/F6X001467', '12345/F6I401467'):
            with self.assertRaisesRegex(RuntimeError, 'Invalid saved'):
                simulated_serial({'serial_number': value})

    def test_legacy_serial_migrates_once_and_new_serial_survives_date_changes(self):
        with patch('apply_navigation_fix.date') as clock:
            clock.today.return_value = date(2026, 12, 31)
            serial = simulated_serial({'revision': 8, 'serial_number': '8743EC33744F0265'})
            self.assertRegex(serial, r'^[0-9]{5}/[A-HJ-NP-Z]6ZZ[0-9]{5}$')
            clock.today.return_value = date(2027, 1, 1)
            self.assertEqual(simulated_serial({'revision': 9, 'serial_number': serial}), serial)
        with self.assertRaisesRegex(RuntimeError, 'Invalid saved'):
            simulated_serial({'revision': 9, 'serial_number': '8743EC33744F0265'})

    def test_sensor_initialization_refuses_a_different_connected_avd(self):
        config = {'name': 'HyperOS_4_Official_API_37', 'port': 5574}
        with patch('apply_flutter_fix.official'), patch('common.adb', return_value=Mock(stdout='Pixel_10_Pro\n')) as adb:
            with self.assertRaisesRegex(RuntimeError, 'different AVD'):
                apply_sensor_defaults(config)
            self.assertEqual(adb.call_count, 1)

    def test_sensor_defaults_use_only_the_owned_emulator_console(self):
        config = {'name': 'HyperOS_4_Official_API_37', 'port': 5574}
        with patch('apply_flutter_fix.official'), patch('common.adb', side_effect=[
                Mock(stdout=config['name'] + '\n'), Mock(stdout='OK\n'), Mock(stdout='OK\n')]) as adb:
            apply_sensor_defaults(config)
        self.assertEqual([call.args[:5] for call in adb.call_args_list[1:]], [
            (config, 'emu', 'sensor', 'set', 'proximity'),
            (config, 'emu', 'sensor', 'set', 'light')])

    def test_user_build_preserves_adb_and_launcher(self):
        original = b'ro.debuggable=1\nro.adb.secure=1\nro.build.type=user\n'
        patched = production_properties(original)
        self.assertIn(b'ro.debuggable=0\n', patched)
        self.assertIn(b'ro.adb.secure=1\n', patched)
        self.assertIn(b'ro.build.type=user\n', patched)
        self.assertIn(b'ro.miui.product.home=com.miui.home\n', patched)
        self.assertIn(b'persist.miui.home_sf_anim=true\n', patched)
        self.assertIn(b'persist.sys.sf.color_saturation=1.0\n', patched)
        self.assertIn(b'persist.sys.gradient_blur_perf=false\n', patched)
        self.assertEqual(production_properties(patched), patched)

    def test_ambiguous_properties_are_rejected(self):
        for source in (b'', b'ro.debuggable=0\nro.debuggable=1\n',
                       b'ro.debuggable=0\npersist.sys.gradient_blur_perf=true\n'
                       b'persist.sys.gradient_blur_perf=false\n'):
            with self.assertRaises(RuntimeError):
                production_properties(source)

    def test_gradient_override_refuses_unverified_hwui_without_writing_properties(self):
        config = {'name': 'HyperOS_4_Official_API_37', 'port': 5574}
        with patch('apply_flutter_fix.official'), patch('apply_flutter_fix.root', side_effect=[
                'ranchu', config['name'], 'unverified /system/lib64/libhwui.so']) as root:
            with self.assertRaisesRegex(RuntimeError, 'Unsupported HWUI'):
                apply_gradient_blur_runtime(config)
        self.assertTrue(all(not call.args[1].startswith('setprop ') for call in root.call_args_list))

    def test_phone_identity_preserves_emulator_drivers(self):
        source = (b'ro.product.device=emu64a\nro.product.model=sdk_gphone64_arm64\n'
                  b'ro.hardware=ranchu\nro.hardware.egl=emulation\n'
                  b'ro.hardware.vulkan=ranchu\nro.boot.hardware=ranchu\n')
        output = identity_properties(source)
        self.assertIn(b'ro.product.device=hongkong\n', output)
        self.assertIn(b'ro.product.model=M610BB\n', output)
        for line in source.splitlines()[2:]:
            self.assertIn(line + b'\n', output)
        self.assertEqual(identity_properties(output), output)
        with self.assertRaises(RuntimeError):
            identity_properties(source + b'ro.product.device=other\n')

    def test_stock_sf_animation_migrates_the_old_override_and_is_unambiguous(self):
        source = b'ro.debuggable=0\npersist.miui.home_sf_anim=false\n'
        result = production_properties(source)
        self.assertEqual(result.count(b'persist.miui.home_sf_anim='), 1)
        self.assertIn(b'persist.miui.home_sf_anim=true\n', result)
        self.assertEqual(production_properties(result), result)
        with self.assertRaises(RuntimeError):
            production_properties(source + b'persist.miui.home_sf_anim=false\n')

    def test_only_custom_console_trigger_is_removed(self):
        original = b'on post-fs-data\n    setprop sys.usb.config adb\n'
        trigger = b'\non property:ro.debuggable=1\n    start console\n'
        self.assertEqual(disable_debug_console(original + trigger), original + b'\n')
        self.assertEqual(disable_debug_console(original), original)
        boot = boot_defaults(original + trigger)
        self.assertIn(AOD_INIT, boot)
        self.assertIn(REFRESH_INIT, boot)
        self.assertIn(GRADIENT_BLUR_INIT, boot)
        self.assertNotIn(trigger, boot)
        self.assertEqual(boot_defaults(boot), boot)

    def test_upgrade_adds_refresh_service_without_duplicate_aod(self):
        original = b'on post-fs-data\n' + AOD_INIT
        output = boot_defaults(original)
        self.assertEqual(output.count(AOD_INIT), 1)
        self.assertEqual(output.count(REFRESH_INIT), 1)
        self.assertEqual(boot_defaults(output), output)
        with self.assertRaises(RuntimeError):
            boot_defaults(b'service hyperos-lock-fps /unrelated/script\n')

    def test_refresh_script_guards_hardware_and_checks_binder_result(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            commands = {
                'getprop': '''case "$1" in
ro.boot.hardware) echo "$TEST_HARDWARE" ;;
ro.miui.product.home) echo com.miui.home ;;
ro.mi.os.version.incremental) echo OS4.0.17.0.XFRCNXM ;;
ro.boot.qemu.vsync) echo 60 ;;
esac''',
                'settings': 'echo "settings $*" >> "$TEST_EVENTS"',
                'service': 'echo "service $*" >> "$TEST_EVENTS"; echo "$TEST_PARCEL"',
                'log': 'echo done >> "$TEST_EVENTS"',
            }
            for name, code in commands.items():
                path = folder / name
                path.write_text('#!/bin/sh\n' + code + '\n')
                path.chmod(0o755)
            script = folder / 'refresh.sh'
            script.write_bytes(REFRESH_SCRIPT)
            events = folder / 'events'
            env = dict(os.environ, PATH=str(folder) + ':' + os.environ['PATH'],
                       TEST_HARDWARE='ranchu', TEST_EVENTS=str(events),
                       TEST_PARCEL='Result: Parcel(NULL)')
            result = subprocess.run(['sh', str(script)], env=env, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(events.read_text().splitlines(), [
                'settings --user 0 put system is_smart_fps 0',
                'settings --user 0 put system min_refresh_rate 60',
                'settings --user 0 put system peak_refresh_rate 60',
                'service call SurfaceFlinger 1035 i32 0', 'done'])
            events.unlink()
            result = subprocess.run(['sh', str(script)], env=dict(env, TEST_HARDWARE='other'),
                                    capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(events.exists())
            result = subprocess.run(['sh', str(script)], env=dict(env, TEST_PARCEL='Permission denied'),
                                    capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn('done', events.read_text().splitlines())

    def test_primary_display_template_does_not_change_other_hardware(self):
        source = 'hw.lcd.width=1080\nhw.lcd.height=2400\nhw.lcd.density=440\nhw.cpu.ncore=4\n'
        output = display_template(source)
        for key, value in DISPLAY.items():
            self.assertIn(f'hw.lcd.{key}={value}\n', output)
        self.assertIn('hw.cpu.ncore=4\n', output)
        self.assertEqual(display_template(output), output)
        with self.assertRaises(RuntimeError):
            display_template(source + 'hw.lcd.width=1080\n')

    def test_emulator_features_match_the_complete_official_model(self):
        features = {item.attrib['name']: item.text for item in ET.fromstring(MODEL_XML)}
        self.assertEqual(hashlib.sha256(MODEL_XML).hexdigest(), MODEL_SHA256)
        self.assertEqual(features['support_aod'], 'true')
        self.assertEqual(features['support_aod_fullscreen'], 'true')
        self.assertEqual(features['support_aod_aon'], 'true')


if __name__ == '__main__':
    unittest.main()
