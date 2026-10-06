"""Guard source-only physical-display aliases and the minimal rear-display RRO."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'scripts'))
import rear_display_config as rear
from phone_profile import profile

AUDIT = REPO / 'work/os4-r3-build/work/rear-display-audit'
SDK = Path.home() / 'Library/Android/sdk'
FIXTURE_PATHS = {path: AUDIT / Path(path).name for path in rear.SOURCE_SHA256}


def reference_tables():
    """Aapt2's textual format with the audited source reference ordering."""
    pairs = {
        'config_displayCutoutPathArray':
            ('@string/config_mainBuiltInDisplayCutout', '@string/config_secondaryBuiltInDisplayCutout'),
        'config_displayCutoutApproximationRectArray':
            ('@string/config_mainBuiltInDisplayCutoutRectApproximation',
             '@string/config_secondaryBuiltInDisplayCutoutRectApproximation'),
        'config_roundedCornerRadiusArray':
            ('@dimen/rounded_corner_radius', '@dimen/secondary_rounded_corner_radius'),
        'config_roundedCornerTopRadiusArray':
            ('@dimen/rounded_corner_radius_top', '@dimen/secondary_rounded_corner_radius_top'),
        'config_roundedCornerBottomRadiusArray':
            ('@dimen/rounded_corner_radius_bottom', '@dimen/secondary_rounded_corner_radius_bottom'),
    }
    framework = ''.join(f'    resource 0x01070001 array/{name}\n'
                        f'      () (array) size=2\n        [{items[0]}, {items[1]}]\n'
                        for name, items in pairs.items())
    aosp = ('    resource 0x7f010008 array/config_displayUniqueIdArray\n'
            '      () (array) size=2\n'
            '        ["local:4630947121878579347", "local:4630947121878579348"]\n')
    devices = ('    resource 0x7f070001 string/config_mainBuiltInDisplayCutout\n'
               '      () "M 0,0 H -34 V 140 H 34 V 0 H 0 Z"\n'
               '    resource 0x7f070003 string/config_secondaryBuiltInDisplayCutout\n'
               '      () "M -456,0 L -456,596 L -160,596 L -160,0 Z @bind_left_cutout"\n')
    for name, value in (('rounded_corner_radius', 180), ('rounded_corner_radius_top', 180),
                        ('rounded_corner_radius_bottom', 180),
                        ('secondary_rounded_corner_radius_top', 106),
                        ('secondary_rounded_corner_radius_bottom', 106)):
        devices += (f'    resource 0x7f040002 dimen/{name}\n'
                    f'      () {value}.000000px\n      (anydpi) {value}.000000px\n')
    return framework, aosp, devices


class RearResourceGuardTests(unittest.TestCase):
    def test_hardware_properties_are_idempotent_and_keep_user_preferences(self):
        selected = profile('4.0.18.0.XFRCNXM')
        original = (b'ro.test.keep=value\npersist.sys.multi_display_type=0\n'
                    b'persist.sys.rear_user_preference=false\n')
        fixed = rear.properties(original, selected)
        self.assertEqual(rear.properties(fixed, selected), fixed)
        self.assertIn(b'ro.test.keep=value\n', fixed)
        self.assertIn(b'persist.sys.rear_user_preference=false\n', fixed)
        for key, value in rear.PROPERTIES.items():
            self.assertEqual(fixed.count((key + '=' + value + '\n').encode()), 1)
        with self.assertRaisesRegex(RuntimeError, 'Duplicate'):
            rear.properties(original + b'persist.sys.multi_display_type=6\n', selected)
        with self.assertRaisesRegex(RuntimeError, 'Unsupported official'):
            rear.properties(original, profile('4.0.17.0.XFRCNXM'))

    def test_boot_capabilities_are_restored_after_persistent_property_loading(self):
        init = rear.boot_init(profile('4.0.18.0.XFRCNXM')).decode()
        self.assertIn('on post-fs-data\n', init)
        self.assertIn('on property:ro.persistent_properties.ready=true\n', init)
        for key, value in rear.PROPERTIES.items():
            self.assertEqual(init.count(f'    setprop {key} {value}\n'), 2)

    def test_static_physical_boot_requires_verified_resources_and_both_native_fixes(self):
        from patch_rear_display import MANIFEST as COMPOSER
        from patch_goldfish_sync import MANIFEST as SYNC
        selected = profile('4.0.18.0.XFRCNXM')
        build = {'source': 'official-hongkong-ota', 'hyperos': selected['hyperos'],
                 'archive_sha256': selected['archive_sha256'],
                 'rear_display': {'schema': 1, 'hyperos': rear.VERSION,
                     'archive_sha256': rear.ARCHIVE_SHA256,
                     'source_sha256': rear.SOURCE_SHA256, 'mapping_sha256': rear.MAPPING_SHA256,
                     'overlay': rear.OVERLAY, 'overlay_priority': rear.PRIORITY},
                 'rear_display_composer_fix': dict(COMPOSER), 'goldfish_sync_fix': dict(SYNC)}
        expected = ['-append-userspace-opt', rear.BOOT_PROPERTY]
        for name in ('Any_Renamed_AVD', 'HyperOS_4_R3_API_37'):
            self.assertEqual(rear.runtime_options({**build, 'name': name}), expected)
        for key in ('rear_display_composer_fix', 'goldfish_sync_fix'):
            with self.subTest(key=key), self.assertRaisesRegex(RuntimeError, 'Unverified'):
                rear.runtime_options({**build, key: {}})
        for field in ('schema', 'source_sha256', 'mapping_sha256', 'overlay_priority'):
            altered = {**build, 'rear_display': {**build['rear_display'], field: 'unknown'}}
            with self.subTest(field=field), self.assertRaisesRegex(RuntimeError, 'Unverified'):
                rear.runtime_options(altered)
        for old in ({}, {'source': 'gsi'}, {'source': 'official-yingtian-ota'},
                    {'hyperos': '4.0.17.0.XFRCNXM'}):
            self.assertEqual(rear.runtime_options(old), [])

    def test_unknown_profile_fails_before_source_reads_or_output_creation(self):
        selected = profile('4.0.18.0.XFRCNXM')
        for field, value in (('hyperos', '4.0.17.0.XFRCNXM'), ('device', 'yingtian'),
                             ('archive_sha256', 'f' * 64), ('incremental', 'unverified'),
                             ('display', {'width': 912, 'height': 596, 'density': 450}),
                             ('source_partition_sha256', {'product': 'f' * 64})):
            altered = copy.deepcopy(selected)
            altered[field] = value
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temporary, \
                    patch('build_image.erofs') as read:
                output = Path(temporary) / 'output'
                with self.assertRaisesRegex(RuntimeError, 'Unsupported official hongkong'):
                    rear.image_replacements(altered, Path(temporary), SDK, output)
                read.assert_not_called()
                self.assertFalse(output.exists())

    def test_overlay_xml_contains_only_fixed_primary_and_rear_id_mapping(self):
        resources = ET.fromstring(rear.RESOURCE_XML)
        self.assertEqual(resources.tag, 'resources')
        self.assertEqual(len(resources), 1)
        array = resources[0]
        self.assertEqual((array.tag, array.attrib['name']),
                         ('string-array', 'config_displayUniqueIdArray'))
        self.assertEqual([item.text for item in array],
                         ['local:4619827259835644672', 'local:4619827551948147201'])
        self.assertEqual(rear.MAPPING_SHA256, hashlib.sha256(rear.RESOURCE_XML).hexdigest())
        self.assertGreater(rear.PRIORITY, 1000)

    def test_incomplete_or_unverified_source_bytes_are_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'Incomplete'):
            rear.validate_sources({})
        with self.assertRaisesRegex(RuntimeError, 'Unexpected original'):
            rear.validate_sources({path: b'unverified' for path in rear.SOURCE_SHA256})

    def test_source_array_order_and_original_geometry_are_required(self):
        framework, aosp, devices = reference_tables()
        rear.validate_resource_tables(framework, aosp, devices)
        mutations = (
            (framework.replace('@string/config_secondaryBuiltInDisplayCutout]',
                               '@string/config_mainBuiltInDisplayCutout]'), aosp, devices),
            (framework, aosp.replace('4630947121878579348', '4619827551948147201'), devices),
            (framework, aosp, devices.replace('L -160,596', 'L -159,596')),
            (framework, aosp, devices.replace('106.000000px', '180.000000px')),
            (framework.replace('array/config_roundedCornerTopRadiusArray',
                               'array/missing'), aosp, devices),
        )
        for tables in mutations:
            with self.subTest(tables=tables), self.assertRaises(RuntimeError):
                rear.validate_resource_tables(*tables)


@unittest.skipUnless(all(path.is_file() for path in FIXTURE_PATHS.values()),
                     'Pinned original Xiaomi source files are not bundled')
class PinnedRearResourcesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sources = {name: path.read_bytes() for name, path in FIXTURE_PATHS.items()}
        cls.selected = profile('4.0.18.0.XFRCNXM')

    def test_exact_original_fixture_density_mapping_is_preserved(self):
        rear.validate_profile(self.selected)
        rear.validate_sources(self.sources)
        xml = ET.fromstring(self.sources['product/etc/displayconfig/display_id_4630947121878579348.xml'])
        self.assertEqual([node.text for node in xml.findall('densityMapping/density/*')],
                         ['596', '912', '450'])
        self.assertEqual(xml.findtext('lightSensor/type'), 'xiaomi.sensor.back_lux')

    def test_each_original_xml_and_signed_resource_package_rejects_byte_tampering(self):
        for path in self.sources:
            with self.subTest(path=path):
                sources = dict(self.sources)
                sources[path] += b'changed'
                with self.assertRaisesRegex(RuntimeError, 'Unexpected original rear-display source'):
                    rear.validate_sources(sources)

    def test_image_edits_add_aliases_without_replacing_official_files(self):
        def read(image, path):
            part = Path(image).stem
            key = part + '/' + (path.removeprefix('/system/') if part == 'system'
                                else path.removeprefix('/'))
            return self.sources[key]
        with tempfile.TemporaryDirectory() as temporary, \
                patch('build_image.erofs', side_effect=read), \
                patch.object(rear, 'build_overlay', return_value=b'new standalone signed RRO'):
            edits, marker = rear.image_replacements(self.selected, Path(temporary), SDK, temporary)
        self.assertEqual(set(edits), {
            'product/etc/displayconfig/display_id_4619827259835644672.xml',
            'product/etc/displayconfig/display_id_4619827551948147201.xml',
            rear.OVERLAY_PATH, rear.MARKER_PATH})
        self.assertFalse(set(edits) & set(self.sources))
        for source, target in zip(rear.SOURCE_IDS, rear.PHYSICAL_IDS):
            self.assertEqual(edits[f'product/etc/displayconfig/display_id_{target}.xml'][0],
                             self.sources[f'product/etc/displayconfig/display_id_{source}.xml'])
        self.assertTrue(all(mode == 0o644 and label == 'u:object_r:system_file:s0'
                            for _, mode, label in edits.values()))
        self.assertEqual(json.loads(edits[rear.MARKER_PATH][0]), marker)
        self.assertEqual(marker['source_sha256'], rear.SOURCE_SHA256)
        self.assertEqual(marker['mapping_sha256'], rear.MAPPING_SHA256)
        self.assertEqual(marker['overridden_resources'], ['config_displayUniqueIdArray'])
        self.assertEqual([entry['density'] for entry in marker['displays']], [480, 450])
        self.assertTrue(marker['requires_runtime_resource_verification'])

    def test_tampered_source_fails_before_overlay_build(self):
        def read(image, path):
            part = Path(image).stem
            key = part + '/' + (path.removeprefix('/system/') if part == 'system'
                                else path.removeprefix('/'))
            return self.sources[key] + b'tamper'
        with tempfile.TemporaryDirectory() as temporary, \
                patch('build_image.erofs', side_effect=read), \
                patch.object(rear, 'build_overlay') as compile:
            with self.assertRaisesRegex(RuntimeError, 'Unexpected original'):
                rear.image_replacements(self.selected, temporary, SDK, Path(temporary) / 'output')
            compile.assert_not_called()
            self.assertFalse((Path(temporary) / 'output').exists())

    @unittest.skipUnless((SDK / 'build-tools/37.0.0/aapt2').is_file() and
                         (SDK / 'platforms/android-36/android.jar').is_file(),
                         'Pinned official Android build tools are unavailable')
    def test_real_aapt_compilation_has_one_resource_and_a_code_free_static_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            signed = rear.build_overlay(SDK, output, self.sources)
            self.assertTrue(signed.startswith(b'PK'))
            dump = subprocess.run([str(SDK / 'build-tools/37.0.0/aapt2'), 'dump', 'xmltree',
                                   str(output / 'RearDisplay.apk'), '--file', 'AndroidManifest.xml'],
                                  check=True, capture_output=True, text=True).stdout
            self.assertIn('android:priority(0x0101001c)=1001', dump)
            self.assertIn('android:targetPackage(0x01010021)="android"', dump)
            self.assertIn('android:isStatic(0x0101055a)=true', dump)
            self.assertIn('android:hasCode(0x0101000c)=false', dump)
            self.assertIn('android:targetSdkVersion(0x01010270)=28', dump)
            for path in rear.SOURCE_SHA256:
                if path.endswith('.apk'):
                    self.assertEqual((output / ('original-' + Path(path).name)).read_bytes(),
                                     self.sources[path])


if __name__ == '__main__':
    unittest.main()
