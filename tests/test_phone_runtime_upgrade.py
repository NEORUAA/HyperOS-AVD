"""Keep OTA-specific runtime overlays and user identity separate."""
import copy
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import apply_flutter_fix as flutter
import apply_navigation_fix as navigation
import os4_defaults as defaults


class PhoneRuntimeUpgradeTests(unittest.TestCase):
    def profile(self):
        return {'hyperos': '4.0.18.0.XFRCNXM', 'incremental': 'OS4.0.18.0.XFRCNXM',
                'properties': {'ro.build.fingerprint': 'verified-new-source',
                               'ro.product.device': 'hongkong'},
                'display': {'width': 1120, 'height': 2436, 'density': 480}}

    def test_selected_identity_does_not_copy_the_previous_fingerprint(self):
        source = b'ro.debuggable=0\nro.hardware=ranchu\nro.build.fingerprint=old\n'
        result = defaults.production_properties(source, self.profile())
        self.assertIn(b'ro.build.fingerprint=verified-new-source\n', result)
        self.assertNotIn(b'ro.build.fingerprint=old\n', result)
        self.assertIn(b'ro.hardware=ranchu\n', result)
        self.assertEqual(defaults.production_properties(result, self.profile()), result)

    def test_versioned_defaults_keep_legacy_pad_anchors_and_once_only_stamps(self):
        legacy = (defaults.AOD_SCRIPT, defaults.REFRESH_SCRIPT, defaults.REFRESH_WRAPPER)
        aod, refresh = defaults.phone_scripts(self.profile())
        wrapper = defaults.refresh_wrapper(self.profile())
        for script in (aod, refresh, wrapper):
            self.assertIn(b'OS4.0.18.0.XFRCNXM', script)
            self.assertNotIn(b'OS4.0.17.0.XFRCNXM', script)
        for stamp in (defaults.AOD_STAMP, defaults.COLOR_STAMP):
            self.assertIn(stamp.encode(), aod)
        self.assertEqual(legacy, (defaults.AOD_SCRIPT, defaults.REFRESH_SCRIPT, defaults.REFRESH_WRAPPER))
        self.assertEqual(defaults.phone_scripts(), legacy[:2])
        self.assertEqual(defaults.refresh_wrapper(), legacy[2])
        self.assertEqual(defaults.display_template(
            'hw.lcd.width=1080\nhw.lcd.height=2400\nhw.lcd.density=440\n', self.profile()),
            'hw.lcd.width=1120\nhw.lcd.height=2436\nhw.lcd.density=480\n')

    def test_navigation_mismatched_firmware_exits_before_identity_writes(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            getprop = folder / 'getprop'
            getprop.write_text('#!/bin/sh\ncase "$1" in\n'
                               'ro.boot.hardware) echo ranchu;;\n'
                               'ro.mi.os.version.incremental) echo OS4.0.17.0.XFRCNXM;;\nesac\n')
            getprop.chmod(0o700)
            script = folder / 'post-fs-data.sh'
            script.write_text(navigation.startup_script(navigation.EARLY_SCRIPT, self.profile()))
            (folder / 'identity.prop').write_text('ro.build.fingerprint=must-not-be-written\n')
            result = subprocess.run(['sh', str(script)], capture_output=True,
                                    env=dict(os.environ, PATH=str(folder) + ':' + os.environ['PATH']))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((folder / 'navigation.log').exists())
            self.assertEqual((folder / 'identity.prop').read_text(),
                             'ro.build.fingerprint=must-not-be-written\n')

    def test_flutter_reuse_requires_ota_shared_library_and_apk_content(self):
        firmware = {'source': 'official-hongkong-ota', 'incremental': 'OS4.0.18.0.XFRCNXM',
                    'shared_input_sha256': 'a' * 64}
        targets = {'com.miui.home': '/product/priv-app/MiuiHome/MiuiHome.apk',
                   'com.miui.weather2': '/data/app/weather/base.apk'}
        apks = {'com.miui.home': 'b' * 64, 'com.miui.weather2': 'c' * 64}
        script_hash = hashlib.sha256(flutter.boot_script(firmware=firmware).encode()).hexdigest()
        saved = {'revision': flutter.REVISION, 'firmware': firmware, 'packages': targets,
                 'apk_hashes': apks, 'startup_script_sha256': script_hash,
                 'system': {'target': flutter.SYSTEM_LIB, 'before': 'a' * 64, 'after': 'd' * 64},
                 'apks': []}
        self.assertTrue(flutter.reusable_manifest(saved, targets, firmware, apks, 'd' * 64, script_hash))
        legacy = copy.deepcopy(saved)
        legacy['revision'] = 6
        self.assertFalse(flutter.reusable_manifest(legacy, targets, firmware, apks, 'd' * 64, script_hash))
        newer_apks = dict(apks, **{'com.miui.home': 'e' * 64})
        self.assertFalse(flutter.reusable_manifest(saved, targets, firmware, newer_apks, 'd' * 64, script_hash))
        self.assertFalse(flutter.reusable_manifest(saved, targets, firmware, apks, 'e' * 64, script_hash))
        other_firmware = dict(firmware, incremental='OS4.0.17.0.XFRCNXM')
        self.assertFalse(flutter.reusable_manifest(saved, targets, other_firmware, apks, 'd' * 64, script_hash))
        self.assertFalse(flutter.reusable_manifest(saved, targets, firmware, apks, 'd' * 64, 'e' * 64))

    def test_flutter_previous_shared_patch_is_a_literal_scoped_upgrade_input(self):
        from patch_flutter import PROFILES
        original = '7f88d7f4d9a464fdd56fb255642c9d300f28f50272ccf5fd31f082c1a52e17f4'
        item = PROFILES[original]
        firmware = {'source': 'official-hongkong-ota', 'incremental': 'OS4.0.18.0.XFRCNXM',
                    'shared_input_sha256': original}
        script = flutter.boot_script(firmware=firmware)
        expected = '|'.join((flutter.SYSTEM_LIB, original, item['output'], item['legacy'][0]))
        self.assertIn(expected, script)
        self.assertIn('case "$target|$before|$after|$actual"', script)
        guard = script.split('    if [ "$actual" != "$before" ]; then\n', 1)[1].split('    fi\n', 1)[0]
        shell = 'guard() { if [ "$actual" != "$before" ]; then\n' + guard + 'fi; }; log() { :; }; guard'
        for target, actual, allowed in ((flutter.SYSTEM_LIB, item['legacy'][0], True),
                                       (flutter.SYSTEM_LIB, original, True),
                                       ('/data/app/unknown/lib/arm64/libhyper_os_flutter.so', item['legacy'][0], False),
                                       (flutter.SYSTEM_LIB, 'f' * 64, False)):
            env = dict(os.environ, target=target, actual=actual, before=original, after=item['output'])
            result = subprocess.run(['sh', '-c', shell], env=env, capture_output=True)
            self.assertEqual(result.returncode == 0, allowed)
        old = {'revision': 7, 'system': {'target': flutter.SYSTEM_LIB}, 'apks': []}
        self.assertEqual(flutter.old_targets(old), [old['system']])
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'module.sh'
            path.write_text(script)
            self.assertEqual(subprocess.run(['sh', '-n', str(path)]).returncode, 0)

    def test_flutter_guard_precedes_mounts_and_weather_rescan_is_deferred(self):
        firmware = {'source': 'official-hongkong-ota', 'incremental': 'OS4.0.18.0.XFRCNXM',
                    'shared_input_sha256': 'a' * 64}
        script = flutter.boot_script(firmware=firmware)
        self.assertLess(script.index('OS4.0.18.0.XFRCNXM'), script.index('. "$MODDIR/targets.conf"'))
        early = script.split('if [ "${0##*/}" = post-fs-data.sh ]; then', 1)[1].split('    exit 0', 1)[0]
        self.assertIn('[ "$package" = com.miui.weather2 ] && continue', early)
        self.assertIn('"$current_sha" = "$apk_sha"', script)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'module.sh'
            path.write_text(script)
            result = subprocess.run(['sh', '-n', str(path)], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
