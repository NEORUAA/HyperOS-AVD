"""Guard OS4 user-build and official primary-display defaults."""
from pathlib import Path
import hashlib
import sys
import unittest
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from os4_defaults import (AOD_INIT, DISPLAY, MODEL_SHA256, MODEL_XML, boot_defaults, disable_debug_console,
                          display_template, production_properties)


class OS4DefaultsTests(unittest.TestCase):
    def test_user_build_preserves_adb_and_launcher(self):
        original = b'ro.debuggable=1\nro.adb.secure=1\nro.build.type=user\n'
        patched = production_properties(original)
        self.assertIn(b'ro.debuggable=0\n', patched)
        self.assertIn(b'ro.adb.secure=1\n', patched)
        self.assertIn(b'ro.build.type=user\n', patched)
        self.assertIn(b'ro.miui.product.home=com.miui.home\n', patched)
        self.assertEqual(production_properties(patched), patched)

    def test_ambiguous_properties_are_rejected(self):
        for source in (b'', b'ro.debuggable=0\nro.debuggable=1\n'):
            with self.assertRaises(RuntimeError):
                production_properties(source)

    def test_only_custom_console_trigger_is_removed(self):
        original = b'on post-fs-data\n    setprop sys.usb.config adb\n'
        trigger = b'\non property:ro.debuggable=1\n    start console\n'
        self.assertEqual(disable_debug_console(original + trigger), original + b'\n')
        self.assertEqual(disable_debug_console(original), original)
        boot = boot_defaults(original + trigger)
        self.assertIn(AOD_INIT, boot)
        self.assertNotIn(trigger, boot)
        self.assertEqual(boot_defaults(boot), boot)

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
