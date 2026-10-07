"""Verify r3 release receipts against actual reader bytes without making images."""
from contextlib import ExitStack
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'scripts'))
import package_release
import apply_rear_display_fix as rear_install
import patch_composer
import patch_goldfish_sync as sync
import patch_rear_display as composer
import rear_display_config as rear
import rear_display_wake as wake
from phone_profile import profile

OLD = '4.0.17.0.XFRCNXM'
NEW = '4.0.18.0.XFRCNXM'


def density_xml(width, height, density):
    return (f'<displayConfiguration><densityMapping><density><width>{width}</width>'
            f'<height>{height}</height><density>{density}</density></density>'
            '</densityMapping></displayConfiguration>').encode()


class RearReleaseTests(unittest.TestCase):
    def setUp(self):
        # Use small deterministic byte fixtures; only artifact pins are replaced.
        # The verifier still computes actual hashes and parses the XML/JSON/init.
        self.selected = profile(NEW)
        self.sources = {
            f'product/etc/displayconfig/display_id_{rear.SOURCE_IDS[0]}.xml': density_xml(1120, 2436, 480),
            f'product/etc/displayconfig/display_id_{rear.SOURCE_IDS[1]}.xml': density_xml(912, 596, 450),
            'product/overlay/AospFrameworkResOverlay.apk': b'original AOSP signed RRO',
            'product/overlay/DevicesAndroidOverlay.apk': b'original Xiaomi signed RRO',
            'system/framework/framework-res.apk': b'original signed framework resources',
        }
        pins = {path: hashlib.sha256(data).hexdigest() for path, data in self.sources.items()}
        self.vendor = {composer.NATIVE: b'composer with alpha and secondary 60 Hz fixes',
                       sync.NATIVE: b'goldfish sync with bounded threaded IRQ draining'}
        self.composer = {**composer.MANIFEST,
                         'native_sha256': hashlib.sha256(self.vendor[composer.NATIVE]).hexdigest()}
        self.sync = {**sync.MANIFEST, 'native_sha256': hashlib.sha256(self.vendor[sync.NATIVE]).hexdigest()}
        self.wake_jar = b'reviewed rear-display double-tap wake JAR fixture'
        self.wake = {**rear_install.EXPECTED_WAKE_MANIFEST,
                     'jar_sha256': hashlib.sha256(self.wake_jar).hexdigest()}
        context = ExitStack()
        self.addCleanup(context.close)
        context.enter_context(patch.object(rear, 'SOURCE_SHA256', pins))
        context.enter_context(patch.object(composer, 'MANIFEST', self.composer))
        context.enter_context(patch.object(sync, 'MANIFEST', self.sync))
        context.enter_context(patch.object(rear_install, 'EXPECTED_WAKE_MANIFEST', self.wake))
        self.compile_wake = context.enter_context(patch.object(wake, 'compile_assets',
            side_effect=AssertionError('Release verification must not compile wake assets')))
        overlay = b'locally signed ID-only rear display overlay'
        displays = []
        for original, physical, geometry in zip(rear.SOURCE_IDS, rear.PHYSICAL_IDS,
                                                ((1120, 2436, 480), (912, 596, 450))):
            source = f'product/etc/displayconfig/display_id_{original}.xml'
            displays.append({'source_physical_id': original, 'physical_id': physical,
                             'unique_id': 'local:' + str(physical),
                             'config': f'/product/etc/displayconfig/display_id_{physical}.xml',
                             'config_sha256': pins[source], 'width': geometry[0],
                             'height': geometry[1], 'density': geometry[2]})
        self.marker = {'schema': 1, 'hyperos': NEW, 'archive_sha256': rear.ARCHIVE_SHA256,
                       'source_sha256': dict(pins), 'mapping_sha256': rear.MAPPING_SHA256,
                       'overlay': rear.OVERLAY, 'overlay_path': '/' + rear.OVERLAY_PATH,
                       'overlay_sha256': hashlib.sha256(overlay).hexdigest(),
                       'overlay_priority': 1001, 'source_overlay_priority': 1000,
                       'requires_runtime_resource_verification': True,
                       'overridden_resources': ['config_displayUniqueIdArray'],
                       'displays': displays, 'rear_safe_inset_left': 296, 'rear_corner_radius': 106}
        self.build = {'source': 'official-hongkong-ota', 'hyperos': NEW,
                      'archive_sha256': self.selected['archive_sha256'],
                      'rear_display': copy.deepcopy(self.marker),
                      'rear_display_composer_fix': dict(self.composer),
                      'composer_alpha_fix': {**patch_composer.MANIFEST,
                                             'native_sha256': self.composer['native_sha256']},
                      'goldfish_sync_fix': dict(self.sync),
                      'rear_display_wake_fix': copy.deepcopy(self.wake)}
        self.system = {'/' + path: data for path, data in self.sources.items()}
        self.system.update({'/' + rear.MARKER_PATH: json.dumps(self.marker).encode(),
                            '/' + rear.OVERLAY_PATH: overlay,
                            self.wake['jar_path']: self.wake_jar,
                            self.wake['script_path']: wake.LAUNCHER,
                            '/system/build.prop': rear.properties(b'ro.adb.secure=1\n', self.selected),
                            '/system_ext/etc/init/init.hyperos_avd.rc':
                                b'on boot\n    setprop unrelated.value preserved\n' + rear.boot_init(self.selected)})
        for display, source in zip(displays, rear.SOURCE_IDS):
            self.system[display['config']] = self.sources[f'product/etc/displayconfig/display_id_{source}.xml']
        self.template = {'hw.multi_display_window': 'yes'}

    def readers(self, system=None, vendor=None):
        return (Mock(side_effect=(self.system if system is None else system).__getitem__),
                Mock(side_effect=(self.vendor if vendor is None else vendor).__getitem__))

    def verify(self, build=None, system=None, vendor=None, template=None):
        read, read_vendor = self.readers(system, vendor)
        return package_release.verify_rear_display(self.selected, self.build if build is None else build,
            read, read_vendor, self.template if template is None else template)

    def assert_metadata_rejected_before_reads(self, build, message):
        read, read_vendor = self.readers()
        with self.assertRaisesRegex(RuntimeError, message):
            package_release.verify_rear_display(self.selected, build, read, read_vendor, self.template)
        read.assert_not_called()
        read_vendor.assert_not_called()

    def test_exact_artifacts_receipts_geometry_properties_and_init_are_accepted(self):
        read, read_vendor = self.readers()
        self.assertEqual(package_release.verify_rear_display(
            self.selected, self.build, read, read_vendor, self.template), 12)
        self.assertEqual([call.args[0] for call in read_vendor.call_args_list], [composer.NATIVE, sync.NATIVE])
        self.assertEqual(set(call.args[0] for call in read.call_args_list), set(self.system))
        self.compile_wake.assert_not_called()

    def test_wake_receipt_requires_exact_reviewed_revision_source_and_schema(self):
        for marker in (None, {}, {**self.wake, 'revision': 1},
                       {**self.wake, 'schema': True}, {**self.wake, 'revision': 2.0},
                       {**self.wake, 'require_down_after_observed_sleep': 1},
                       {**self.wake, 'source_sha256': '0' * 64},
                       {**self.wake, 'jar_sha256': '0' * 64},
                       {**self.wake, 'script_sha256': '0' * 64},
                       {**self.wake, 'extra': True}):
            with self.subTest(receipt=marker):
                self.assert_metadata_rejected_before_reads(
                    {**self.build, 'rear_display_wake_fix': marker}, 'rear-display wake metadata')
        for key in self.wake:
            marker = copy.deepcopy(self.wake)
            del marker[key]
            with self.subTest(missing=key):
                self.assert_metadata_rejected_before_reads(
                    {**self.build, 'rear_display_wake_fix': marker}, 'rear-display wake metadata')
        build = copy.deepcopy(self.build)
        del build['rear_display_wake_fix']
        self.assert_metadata_rejected_before_reads(build, 'rear-display wake metadata')

    def test_changed_wake_source_or_launcher_is_refused_before_image_reads(self):
        for attribute, replacement in (('JAVA_SOURCE', wake.JAVA_SOURCE + '\n// changed\n'),
                                       ('LAUNCHER', wake.LAUNCHER + b'# changed\n')):
            with self.subTest(source=attribute), patch.object(wake, attribute, replacement):
                self.assert_metadata_rejected_before_reads(self.build, 'wake source or launcher')

    def test_baked_wake_jar_and_launcher_must_match_reviewed_actual_bytes(self):
        for path in (self.wake['jar_path'], self.wake['script_path']):
            for data in (b'old revision 1 wake artifact', self.system[path] + b'changed'):
                with self.subTest(path=path, bytes=data[:32]):
                    with self.assertRaisesRegex(RuntimeError, 'wake checksum mismatch: ' + path):
                        self.verify(system={**self.system, path: data})

    def test_missing_or_stale_native_receipts_never_fall_back_to_alpha_only(self):
        for key in ('rear_display_composer_fix', 'composer_alpha_fix', 'goldfish_sync_fix'):
            for replacement in (None, {}, {**self.build[key], 'revision': 0},
                                {**self.build[key], 'native_sha256': patch_composer.AFTER},
                                {**self.build[key], 'extra': True}):
                with self.subTest(key=key, receipt=replacement):
                    build = {**self.build, key: replacement}
                    self.assert_metadata_rejected_before_reads(build, 'composer or goldfish sync metadata')
        build = copy.deepcopy(self.build)
        build['goldfish_sync_fix']['kernel_vermagic'] = 'another kernel'
        self.assert_metadata_rejected_before_reads(build, 'composer or goldfish sync metadata')

    def test_missing_or_incomplete_resource_receipts_are_rejected_before_reads(self):
        for marker in (None, {}, {**self.marker, 'overlay_sha256': 'unverified'}):
            with self.subTest(marker=marker):
                self.assert_metadata_rejected_before_reads({**self.build, 'rear_display': marker},
                                                           'rear-display resource metadata')
        for key in self.marker:
            marker = copy.deepcopy(self.marker)
            del marker[key]
            with self.subTest(missing=key):
                self.assert_metadata_rejected_before_reads({**self.build, 'rear_display': marker},
                                                           'rear-display resource metadata')

    def test_every_static_marker_field_and_display_order_are_guarded(self):
        for key in self.marker.keys() - {'overlay_sha256'}:
            marker = copy.deepcopy(self.marker)
            marker[key] = 'stale metadata'
            with self.subTest(field=key):
                self.assert_metadata_rejected_before_reads({**self.build, 'rear_display': marker},
                                                           'rear-display resource metadata')
        cases = [{'extra': True}, {'schema': True}, {'overlay_priority': 1001.0},
                 {'displays': list(reversed(self.marker['displays']))}]
        for values in cases:
            with self.subTest(values=values):
                self.assert_metadata_rejected_before_reads(
                    {**self.build, 'rear_display': {**self.marker, **values}}, 'rear-display resource metadata')
        for key in self.marker['displays'][1]:
            marker = copy.deepcopy(self.marker)
            marker['displays'][1][key] = 'stale alias'
            with self.subTest(alias=key):
                self.assert_metadata_rejected_before_reads({**self.build, 'rear_display': marker},
                                                           'rear-display resource metadata')

    def test_original_alpha_only_or_corrupt_vendor_artifacts_are_rejected(self):
        for path in (composer.NATIVE, sync.NATIVE):
            for replacement in (b'original unpatched artifact', b'alpha-only composer', self.vendor[path] + b'changed'):
                with self.subTest(path=path, replacement=replacement):
                    with self.assertRaisesRegex(RuntimeError, 'vendor checksum mismatch: ' + path):
                        self.verify(vendor={**self.vendor, path: replacement})

    def test_baked_marker_and_signed_overlay_must_match_release_receipt(self):
        for data in (b'not json', json.dumps({**self.marker, 'rear_corner_radius': 180}).encode(),
                     json.dumps({**self.marker, 'schema': True}).encode()):
            with self.subTest(marker=data[:32]), self.assertRaisesRegex(RuntimeError, 'rear-display resource metadata'):
                self.verify(system={**self.system, '/' + rear.MARKER_PATH: data})
        with self.assertRaisesRegex(RuntimeError, 'overlay checksum mismatch'):
            self.verify(system={**self.system, '/' + rear.OVERLAY_PATH: self.system['/' + rear.OVERLAY_PATH] + b'changed'})

    def test_every_preserved_source_and_both_display_xml_aliases_are_checked(self):
        for path in self.sources:
            with self.subTest(source=path), self.assertRaisesRegex(RuntimeError, 'original rear-display source'):
                self.verify(system={**self.system, '/' + path: self.system['/' + path] + b'changed'})
        for display in self.marker['displays']:
            path = display['config']
            for replacement in (density_xml(912, 596, 480), b'other display XML'):
                with self.subTest(alias=path), self.assertRaisesRegex(RuntimeError, 'XML alias checksum mismatch'):
                    self.verify(system={**self.system, path: replacement})

    def test_missing_conflicting_or_duplicate_hardware_properties_are_rejected(self):
        path = '/system/build.prop'
        for key, value in rear.PROPERTIES.items():
            line = f'{key}={value}\n'.encode()
            for replacement in (self.system[path].replace(line, b''),
                                self.system[path] + line,
                                self.system[path] + f'{key}=old\n'.encode()):
                with self.subTest(key=key), self.assertRaisesRegex(RuntimeError, 'hardware property: ' + key):
                    self.verify(system={**self.system, path: replacement})

    def test_both_init_phases_are_required_without_conflicting_overrides(self):
        path = '/system_ext/etc/init/init.hyperos_avd.rc'
        block = rear.boot_init(self.selected)
        for replacement in (b'on boot\n', self.system[path] + block,
                            block.split(b'\non property:', 1)[0],
                            self.system[path].replace(b'ro.persistent_properties.ready=true', b'sys.boot_completed=1')):
            with self.subTest(init=replacement[:32]), self.assertRaisesRegex(RuntimeError, 'hardware initialization'):
                self.verify(system={**self.system, path: replacement})
        key = next(iter(rear.PROPERTIES))
        with self.assertRaisesRegex(RuntimeError, 'init property override: ' + key):
            self.verify(system={**self.system, path: self.system[path] + f'\non boot\n    setprop\t{key} old\n'.encode()})

    def test_bootconfig_interface_and_separate_window_are_required(self):
        for template in ({}, {'hw.multi_display_window': 'no'}):
            read, read_vendor = self.readers()
            with self.subTest(template=template), self.assertRaisesRegex(RuntimeError, 'boot/window configuration'):
                package_release.verify_rear_display(self.selected, self.build, read, read_vendor, template)
            read.assert_not_called()
            read_vendor.assert_not_called()
        with patch.object(rear, 'runtime_options', return_value=[]):
            with self.assertRaisesRegex(RuntimeError, 'boot/window configuration'):
                self.verify()

    def test_non_r3_phone_pad_and_os3_do_not_read_or_require_new_artifacts(self):
        for version in (OLD, '4.0.6.0.XOCCNXM', '3.0.2.0.WMCCNXM'):
            read, read_vendor = Mock(), Mock()
            with self.subTest(version=version):
                self.assertEqual(package_release.verify_rear_display(
                    {'hyperos': version}, {}, read, read_vendor, {}), 0)
            read.assert_not_called()
            read_vendor.assert_not_called()

    def test_foreign_or_unpinned_r3_profile_is_refused_before_image_reads(self):
        for changes in ({'device': 'yingtian'}, {'archive_sha256': 'unverified'},
                        {'source_partition_sha256': {}}, {'display': {'width': 912, 'height': 596, 'density': 450}}):
            read, read_vendor = Mock(), Mock()
            with self.subTest(profile=changes), self.assertRaisesRegex(RuntimeError, 'rear-display profile'):
                package_release.verify_rear_display({**self.selected, **changes}, self.build, read, read_vendor, self.template)
            read.assert_not_called()
            read_vendor.assert_not_called()

    def test_release_metadata_preserves_rear_receipts_only_for_r3(self):
        keys = ('rear_display', 'rear_display_composer_fix', 'goldfish_sync_fix',
                'rear_display_wake_fix')
        for version in (OLD, NEW):
            selected = profile(version)
            build = {**self.build, 'hyperos': version, 'archive_sha256': selected['archive_sha256'],
                     'android_api': 37, 'adb_authentication': True, 'flutter_render_fix': 6,
                     'native_quickstep_identity': True, 'preinstalled_apps': {'apps': ['fixture']},
                     'avd_defaults': {'fixture': True}}
            with self.subTest(version=version), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / 'local').mkdir()
                (root / 'local/build.json').write_text(json.dumps(build))
                metadata = package_release.release_metadata(root, 'os4-official')
                for key in keys:
                    if version == NEW:
                        self.assertEqual(metadata['build'][key], build[key])
                    else:
                        self.assertNotIn(key, metadata['build'])


if __name__ == '__main__':
    unittest.main()
