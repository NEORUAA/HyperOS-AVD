"""Guard the tablet identity, defaults and audited native engine profile."""
import hashlib
from pathlib import Path
import struct
import sys
import unittest
import json
import tempfile
import subprocess
from unittest.mock import patch as mock_patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import os4_pad
import patch_flutter


class TabletProfileTests(unittest.TestCase):
    def test_public_identity_script_preserves_hal_selectors_and_serial(self):
        script = os4_pad.profile_properties_script()
        subprocess.run(['sh', '-n'], input=script, text=True, check=True)
        self.assertIn('key=ro.product.device\nvalue=yingtian', script)
        self.assertIn('key=log.tag.RenderEngine\nvalue=S', script)
        for key in ('ro.board.platform', 'ro.hardware.egl', 'ro.boot.hardware',
                    'ro.vendor.api_level', 'ro.serialno', 'ro.ril.oem.psno'):
            with mock_patch.object(os4_pad, 'PROFILE',
                                   dict(os4_pad.PROFILE, properties={key: 'unsafe'})):
                with self.assertRaisesRegex(RuntimeError, 'Unsupported tablet public'):
                    os4_pad.profile_properties_script()

    def test_serial_manifest_preserves_identifier_and_refuses_other_profiles(self):
        serial = '69704/F6X601467'
        with mock_patch('apply_navigation_fix.simulated_serial', return_value=serial):
            state = os4_pad.serial_manifest()
        self.assertEqual(state['source'], os4_pad.SOURCE)
        self.assertEqual(os4_pad.serial_manifest(state), state)
        for field, value in (('source', 'official-hongkong-ota'), ('revision', 2),
                             ('hyperos', 'OS4.0.17.0.XFRCNXM'), ('serial_number', 'unknown')):
            with self.assertRaises(RuntimeError):
                os4_pad.serial_manifest(dict(state, **{field: value}))
        with self.assertRaises(RuntimeError):
            os4_pad.serial_manifest(dict(state, unrelated=True))

    def test_serial_manifest_survives_forward_same_device_firmware(self):
        state = {'revision': 1, 'source': os4_pad.SOURCE,
                 'hyperos': 'OS4.0.15.0.XBMCNXM', 'serial_number': '69704/F6X601467'}
        updated = dict(os4_pad.PROFILE, hyperos='OS4.0.16.0.XBMCNXM')
        with mock_patch.object(os4_pad, 'PROFILE', updated):
            self.assertEqual(os4_pad.serial_manifest(state), state)
            self.assertIn(updated['hyperos'], os4_pad.serial_script(state))
            for firmware in ('OS4.0.17.0.XBMCNXM', 'OS4.0.15.0.XFRCNXM',
                             'OS3.0.15.0.XBMCNXM', 'unknown', None):
                with self.subTest(firmware=firmware), self.assertRaises(RuntimeError):
                    os4_pad.serial_manifest(dict(state, hyperos=firmware))
        for changed in ({**state, 'extra': True}, {**state, 'revision': 2},
                        {**state, 'source': 'official-hongkong-ota'},
                        {**state, 'serial_number': 'unknown'}):
            with self.subTest(state=changed), self.assertRaises(RuntimeError):
                os4_pad.serial_manifest(changed)

    def test_serial_early_script_changes_only_identifier_fields(self):
        state = {'revision': 1, 'source': os4_pad.SOURCE, 'hyperos': os4_pad.PROFILE['hyperos'],
                 'serial_number': '69704/F6X601467'}
        script = os4_pad.serial_script(state)
        self.assertIn('getprop ro.product.device', script)
        self.assertIn('yingtian', script)
        self.assertIn(os4_pad.PROFILE['hyperos'], script)
        self.assertEqual([line.strip().split()[2] for line in script.splitlines()
                          if line.strip().startswith('"$RESETPROP" -n')],
                         ['ro.serialno', 'ro.boot.serialno', 'ro.ril.oem.psno'])
        self.assertNotIn('hongkong', script)
        self.assertNotIn(os4_pad.NAME, script)
        self.assertNotIn('5582', script)
        subprocess.run(['sh', '-n'], input=script, text=True, check=True)

    def test_serial_wrong_live_device_is_rejected_before_writes(self):
        with mock_patch('apply_flutter_fix.official'), \
                mock_patch('apply_flutter_fix.root', side_effect=['ranchu', 'hongkong']) as root:
            with self.assertRaisesRegex(RuntimeError, 'different device'):
                os4_pad.apply_serial({'name': 'Renamed Pad', 'port': 6000})
        self.assertEqual(root.call_count, 2)
        self.assertTrue(all(call.args[1].startswith('getprop ') for call in root.call_args_list))

    def test_serial_restart_keeps_saved_identity_without_rewriting(self):
        state = {'revision': 1, 'source': os4_pad.SOURCE, 'hyperos': os4_pad.PROFILE['hyperos'],
                 'serial_number': '69704/F6X601467'}
        responses = ['ranchu', 'yingtian', state['hyperos'], json.dumps(state),
                     os4_pad.serial_script(state).strip(), '', *([state['serial_number']] * 3)]
        config = {'name': 'Renamed Pad', 'port': 6000}
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary); (folder / 'local').mkdir()
            with mock_patch('common.ROOT', folder), mock_patch('apply_flutter_fix.official') as official, \
                    mock_patch('apply_flutter_fix.root', side_effect=responses) as root:
                self.assertEqual(os4_pad.apply_serial(config), state)
            official.assert_called_once_with(config, sources=(os4_pad.SOURCE,))
            self.assertFalse(any('mv -f' in call.args[1] for call in root.call_args_list))
            self.assertEqual(json.loads((folder / 'local/pad-serial.json').read_text()), state)

    def test_serial_upgrade_migrates_only_a_verified_owned_early_script(self):
        state = {'revision': 1, 'source': os4_pad.SOURCE,
                 'hyperos': 'OS4.0.15.0.XBMCNXM', 'serial_number': '69704/F6X601467'}
        old_script = os4_pad.serial_script(state).strip()
        updated = dict(os4_pad.PROFILE, hyperos='OS4.0.17.0.XBMCNXM')
        for saved in (old_script, old_script.replace('OS4.0.15.0.', 'OS4.0.16.0.')):
            responses = ['ranchu', 'yingtian', updated['hyperos'], json.dumps(state),
                         saved, '', '', *([state['serial_number']] * 3)]
            with self.subTest(script=saved), tempfile.TemporaryDirectory() as temporary:
                folder = Path(temporary); (folder / 'local').mkdir()
                with mock_patch.object(os4_pad, 'PROFILE', updated), \
                        mock_patch('common.ROOT', folder), mock_patch('apply_flutter_fix.official'), \
                        mock_patch('apply_flutter_fix.root', side_effect=responses) as root:
                    self.assertEqual(os4_pad.apply_serial({'name': 'Renamed Pad', 'port': 6000}), state)
                writes = root.call_args_list[5].args[1]
                self.assertIn(updated['hyperos'], writes)
                self.assertIn('mv -f ' + os4_pad.SERIAL_EARLY, writes)
                self.assertNotIn('mv -f ' + os4_pad.SERIAL_FILE, writes)
                self.assertEqual(json.loads((folder / 'local/pad-serial.json').read_text()), state)

    def test_serial_upgrade_rejects_changed_payloads_before_writing(self):
        state = {'revision': 1, 'source': os4_pad.SOURCE,
                 'hyperos': 'OS4.0.15.0.XBMCNXM', 'serial_number': '69704/F6X601467'}
        old_script = os4_pad.serial_script(state).strip()
        updated = dict(os4_pad.PROFILE, hyperos='OS4.0.16.0.XBMCNXM')
        for saved in (old_script + '\n# unrelated',
                      old_script.replace('getprop ro.product.device', 'getprop ro.product.model'),
                      old_script.replace('OS4.0.15.0.XBMCNXM', 'OS4.0.15.0.XFRCNXM'),
                      old_script.replace('OS4.0.15.0.XBMCNXM', 'OS4.0.17.0.XBMCNXM')):
            responses = ['ranchu', 'yingtian', updated['hyperos'], json.dumps(state), saved]
            with self.subTest(script=saved), mock_patch.object(os4_pad, 'PROFILE', updated), \
                    mock_patch('apply_flutter_fix.official'), \
                    mock_patch('apply_flutter_fix.root', side_effect=responses) as root:
                with self.assertRaisesRegex(RuntimeError, 'Unknown tablet serial startup'):
                    os4_pad.apply_serial({'name': 'Renamed Pad', 'port': 6000})
                self.assertEqual(root.call_count, 5)

    def test_serial_first_install_stages_and_validates_before_use(self):
        serial = '69704/F6X601467'
        responses = ['ranchu', 'yingtian', os4_pad.PROFILE['hyperos'], '', '', '', '',
                     serial, serial, serial]
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary); (folder / 'local').mkdir()
            with mock_patch('common.ROOT', folder), mock_patch('apply_flutter_fix.official'), \
                    mock_patch('apply_navigation_fix.simulated_serial', return_value=serial), \
                    mock_patch('apply_flutter_fix.root', side_effect=responses) as root:
                state = os4_pad.apply_serial({'name': 'Renamed Pad', 'port': 6000})
            writes = root.call_args_list[5].args[1]
            self.assertIn('trap ', writes)
            self.assertIn('sh -n ', writes)
            self.assertIn('chmod 600 ', writes)
            self.assertIn('chmod 700 ', writes)
            self.assertIn('.stage-', writes)
            self.assertEqual(state['serial_number'], serial)

    def test_serial_refuses_unrelated_early_script_without_writes(self):
        state = {'revision': 1, 'source': os4_pad.SOURCE, 'hyperos': os4_pad.PROFILE['hyperos'],
                 'serial_number': '69704/F6X601467'}
        responses = ['ranchu', 'yingtian', state['hyperos'], json.dumps(state), '# unrelated']
        with mock_patch('apply_flutter_fix.official'), \
                mock_patch('apply_flutter_fix.root', side_effect=responses) as root:
            with self.assertRaisesRegex(RuntimeError, 'Unknown tablet serial startup'):
                os4_pad.apply_serial({'name': 'Renamed Pad', 'port': 6000})
        self.assertEqual(root.call_count, 5)

    def test_display_defaults_seed_color_once_and_keep_screen_awake(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            with mock_patch.object(os4_pad, 'COLOR_STAMP', str(folder / 'color-stamp')), \
                    mock_patch.object(os4_pad, 'DEFAULTS_STAMP', str(folder / 'settings-stamp')), \
                    mock_patch('os4_defaults.AWAKE_STAMP', str(folder / 'awake-stamp')):
                script = os4_pad.display_settings_script()
            mock = 'settings() { printf "settings %s\\n" "$*"; }; setprop() { :; }; input() { :; };\n'
            first = subprocess.run(['sh'], input=mock + script, text=True,
                                   check=True, capture_output=True).stdout
            second = subprocess.run(['sh'], input=mock + script, text=True,
                                    check=True, capture_output=True).stdout
            self.assertIn('settings put secure sleep_timeout -1', first)
            self.assertIn('settings put system screen_off_timeout 2147483647', first)
            self.assertIn('settings put system display_color_mode 0', first)
            self.assertIn('settings put global development_settings_enabled 1', first)
            self.assertEqual(second, '')

    def test_legacy_defaults_markers_preserve_existing_user_choices(self):
        for previous in ('color-stamp', 'awake-stamp'):
            with self.subTest(previous=previous), tempfile.TemporaryDirectory() as temporary:
                folder = Path(temporary)
                (folder / previous).touch()
                with mock_patch.object(os4_pad, 'COLOR_STAMP', str(folder / 'color-stamp')), \
                        mock_patch.object(os4_pad, 'DEFAULTS_STAMP', str(folder / 'settings-stamp')), \
                        mock_patch('os4_defaults.AWAKE_STAMP', str(folder / 'awake-stamp')):
                    script = os4_pad.display_settings_script()
                mock = 'settings() { printf "settings %s\\n" "$*"; }; setprop() { :; }; input() { :; };\n'
                output = subprocess.run(['sh'], input=mock + script, text=True,
                                        check=True, capture_output=True).stdout
                for setting in ('screen_off_timeout', 'sleep_timeout',
                                'stay_on_while_plugged_in', 'development_settings_enabled'):
                    self.assertNotIn(setting, output)
                self.assertEqual('display_color_mode' in output, previous == 'awake-stamp')
                self.assertTrue((folder / 'settings-stamp').is_file())

    def test_failed_default_write_does_not_mark_settings_initialized(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            with mock_patch.object(os4_pad, 'COLOR_STAMP', str(folder / 'color-stamp')), \
                    mock_patch.object(os4_pad, 'DEFAULTS_STAMP', str(folder / 'settings-stamp')), \
                    mock_patch('os4_defaults.AWAKE_STAMP', str(folder / 'awake-stamp')):
                script = os4_pad.display_settings_script()
            result = subprocess.run(['sh'], input='settings() { return 1; };\n' + script,
                                    text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((folder / 'settings-stamp').exists())
            self.assertFalse((folder / 'color-stamp').exists())

    def test_real_tablet_profile_is_separate_from_phone(self):
        self.assertEqual(os4_pad.PROFILE['device'], 'yingtian')
        self.assertEqual(os4_pad.PROFILE['properties']['ro.product.model'], 'M367FC')
        self.assertEqual(os4_pad.PROFILE['properties']['ro.build.characteristics'], 'tablet')
        self.assertEqual(hashlib.sha256(os4_pad.model_config()).hexdigest(),
                         os4_pad.PROFILE['model_xml_sha256'])
        self.assertIn(b'<bool name="is_pad">true</bool>', os4_pad.model_config())
        self.assertIn(b'<bool name="support_aod">false</bool>', os4_pad.model_config())

    def test_template_uses_source_display_and_no_modem(self):
        values = dict(line.split('=', 1) for line in os4_pad.template('').splitlines())
        self.assertEqual((values['hw.lcd.width'], values['hw.lcd.height'],
                          values['hw.lcd.density']), ('2272', '3408', '400'))
        self.assertEqual(values['hw.initialOrientation'], 'portrait')
        self.assertEqual(values['hw.audioOutput'], 'yes')
        self.assertEqual(values['hw.gsmModem'], 'no')
        self.assertEqual(values['hw.ramSize'], '4096')

    def test_identity_does_not_replace_hardware_selector(self):
        original = b'ro.boot.hardware=ranchu\nro.hardware.egl=emulation\nro.debuggable=0\n'
        result = os4_pad.properties(original)
        self.assertIn(b'ro.boot.hardware=ranchu\n', result)
        self.assertIn(b'ro.hardware.egl=emulation\n', result)
        self.assertIn(b'ro.product.device=yingtian\n', result)
        self.assertNotIn(b'hongkong', result)
        from os4_defaults import LOG_TAGS
        for tag in LOG_TAGS:
            self.assertIn(('log.tag.' + tag + '=S\n').encode(), result)

    def test_refresh_override_keeps_the_tablet_version_guard(self):
        from os4_defaults import REFRESH_WRAPPER
        data = os4_pad.refresh_script(REFRESH_WRAPPER)
        self.assertIn(b'OS4.0.15.0.XBMCNXM', data)
        self.assertNotIn(b'OS4.0.17.0.XFRCNXM', data)
        self.assertIn(b'[ "$(getprop ro.boot.hardware)" = ranchu ]', data)

    def test_phone_runtime_patches_refuse_tablet_before_adb(self):
        import apply_flutter_fix
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'local').mkdir()
            (root / 'local/build.json').write_text(json.dumps({'source': os4_pad.SOURCE}))
            with mock_patch.object(apply_flutter_fix, 'ROOT', root), \
                    mock_patch.object(apply_flutter_fix, 'adb') as adb:
                with self.assertRaisesRegex(RuntimeError, 'firmware profile'):
                    apply_flutter_fix.official({'name': os4_pad.NAME, 'port': os4_pad.PORT})
                adb.assert_not_called()

    def test_real_tablet_engine_when_available(self):
        source = Path(__file__).resolve().parents[1] / 'work/os4-pad/work/source-audit/flutter.so'
        if not source.is_file():
            self.skipTest('Proprietary OTA engine is not distributed in Git.')
        data = source.read_bytes()
        fixed = patch_flutter.patch(data)
        self.assertEqual(patch_flutter.patch(fixed), fixed)
        self.assertEqual(len(fixed), len(data))
        self.assertEqual(struct.unpack_from('<2f', data, 0x123780), (0.0, 1.0))
        self.assertEqual(struct.unpack_from('<2f', data, 0x123790), (1.0, 0.0))
        # The two BL sites must branch exactly to the audited RX padding.
        for offset in (0xac3db8, 0xac434c):
            instruction = struct.unpack_from('<I', fixed, offset)[0]
            delta = instruction & 0x3ffffff
            if delta & 0x2000000:
                delta -= 0x4000000
            self.assertEqual(offset + delta * 4, 0x613ca8)
        allowed = bytearray(len(data))
        _, profile = patch_flutter.profile(data)
        for offset, before, _ in profile['sites']:
            allowed[offset:offset + len(bytes.fromhex(before))] = bytes([1]) * len(bytes.fromhex(before))
        self.assertFalse(any(a != b and not ok for a, b, ok in zip(data, fixed, allowed)))


if __name__ == '__main__':
    unittest.main()
