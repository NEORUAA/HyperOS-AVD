"""Validate the release boundary without touching any registered AVD."""
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import package_release
import prepare_release_image
import release_pad
from os4_pad import PROFILE, SOURCE
from patch_pad_hwui import BEFORE, AFTER
from patch_assistant import profile as manifest
from patch_flutter import PROFILES
from os4_defaults import LOG_SCRIPT, OVERLAY, SCREEN_TIMEOUT


def build_info():
    engine = '9caf8bd3413b3093ae0855f86369f31ddf45b46c7ddbaa4c2f656e4f67bb5009'
    return {'source': SOURCE, 'device': 'yingtian', 'android_api': 37,
        'hyperos': PROFILE['hyperos'], 'archive_sha256': PROFILE['source_archive_sha256'],
        'display': PROFILE['display'], 'model_xml_sha256': PROFILE['model_xml_sha256'],
        'identity_source_sha256': PROFILE['source_sha256'], 'memory_limit_mib': 4096,
        'hardware_base_api': 36, 'kernel_page_size': 4096, 'gnss_patch': True,
        'experimental': True, 'finddevice_provider_disabled': True, 'adb_authentication': True,
        'hwui': {'before': BEFORE, 'after': AFTER},
        'flutter_engine': {'before': engine, 'after': PROFILES[engine]['output']},
        'assistant_render_fix': manifest(SOURCE),
        'avd_defaults': {'settings_overlay_sha256': 'test', 'settings_overlay': OVERLAY,
            'screen_off_timeout': SCREEN_TIMEOUT, 'sleep_timeout': -1,
            'first_boot_stay_on_while_plugged_in': 1, 'stay_on_while_plugged_in': 7,
            'log_script_sha256': hashlib.sha256(LOG_SCRIPT).hexdigest()}, 'vendor_fixes': {'test': True}}



class PadReleaseTests(unittest.TestCase):
    def test_metadata_is_a_separate_secure_family(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'local').mkdir()
            (root / 'local/build.json').write_text(json.dumps(build_info()))
            value = package_release.release_metadata(root, 'os4-pad')
            self.assertEqual(value['source_device'], 'yingtian')
            self.assertEqual(value['compatibility'], {'minimum_installer': '1.1.0',
                'userdata_family': 'os4-yingtian-api37-ranchu-4k', 'upgrade_from': [], 'runtime_in_bundle': True})
            for key in ('hwui', 'flutter_engine', 'display', 'identity_source_sha256', 'memory_limit_mib'):
                self.assertEqual(value['build'][key], build_info()[key])
            for variant in ('os3', 'os4-official'):
                with self.assertRaises(RuntimeError):
                    package_release.release_metadata(root, variant)

    def test_unverified_source_or_diagnostic_metadata_is_rejected(self):
        for key, invalid in (('adb_authentication', False), ('source', 'official-hongkong-ota'),
                             ('device', 'hongkong'), ('memory_limit_mib', 6144),
                             ('model_xml_sha256', 'unknown'), ('kernel_page_size', 16384)):
            with self.subTest(key=key), self.assertRaisesRegex(RuntimeError, key):
                release_pad.validate_build({**build_info(), key: invalid})

    def test_authenticated_properties_retain_all_other_defaults(self):
        original = b'# Accepted defaults\nro.adb.secure=0\nro.debuggable=0\nro.product.device=yingtian\n'
        value = prepare_release_image.authenticated_properties(original)
        self.assertEqual(value, original.replace(b'ro.adb.secure=0', b'ro.adb.secure=1'))
        self.assertEqual(prepare_release_image.authenticated_properties(value), value)
        for invalid in (original + b'ro.adb.secure=1\n', original.replace(b'ro.debuggable=0', b'ro.debuggable=1')):
            with self.assertRaises(RuntimeError):
                prepare_release_image.authenticated_properties(invalid)

    def test_old_overlay_defaults_cannot_be_published(self):
        value = build_info()
        value['avd_defaults']['screen_off_timeout'] = 120000
        with self.assertRaisesRegex(RuntimeError, 'defaults'):
            release_pad.validate_build(value)

    def test_packed_partition_content_and_size_are_verified(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw, packed = root / 'raw.img', root / 'packed.img'
            raw.write_bytes(bytes(512))
            packed.write_bytes(bytes(512))
            partition = {'name': 'system', 'size': 512, 'extents': [(1, 0, 0, 0)]}
            with patch('lp_image.read_lp', return_value=(0, [partition])):
                release_pad.verify_partition(packed, raw, 'system')
                packed.write_bytes(b'x' + bytes(511))
                with self.assertRaisesRegex(RuntimeError, 'differs'):
                    release_pad.verify_partition(packed, raw, 'system')

    def test_archive_never_accepts_personal_layers_or_cross_variant_payloads(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload = root / 'payload'
            payload.write_bytes(b'not personal data')
            for relative in ('avd/userdata-qemu.img', 'images/userdata-qemu.img',
                             'images/encryptionkey.img.qcow2', 'local/serial.json',
                             'tools/xiaomi-camera/provider', 'tools/weather-angle/original.so'):
                with self.subTest(relative=relative), self.assertRaisesRegex(RuntimeError, 'Unexpected release path'):
                    package_release.write_bundle(root / 'bundle', 'vtest',
                        {'format': 3, 'variant': 'os4-pad'}, {relative: payload}, 1)

    def test_independent_pad_tag_is_accepted_by_packager(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload = root / 'system.img'
            payload.write_bytes(b'fixture')
            result = package_release.write_bundle(root / 'bundle', 'pad-v0.1.0-a17-hyperos4-yingtian-r1',
                {'format': 3, 'variant': 'os4-pad', 'build': {}}, {'images/system.img': payload}, 1)
            self.assertEqual(result['version'], 'pad-v0.1.0-a17-hyperos4-yingtian-r1')

    def test_signed_factory_apk_keeps_original_native_inputs(self):
        data = io.BytesIO()
        with zipfile.ZipFile(data, 'w') as archive:
            archive.writestr('lib/arm64-v8a/libtest.so', b'original signed native')
        before = hashlib.sha256(b'original signed native').hexdigest()
        release_pad.verify_apk_libraries(data.getvalue(), {'libtest.so': (before, 'patched hash')})
        with self.assertRaisesRegex(RuntimeError, 'native input differs'):
            release_pad.verify_apk_libraries(data.getvalue(), {'libtest.so': ('patched hash', before)})

    def test_conflicting_properties_are_rejected(self):
        release_pad.verify_properties([b'ro.adb.secure=1'], {'ro.adb.secure': '1'})
        for lines in ([], [b'ro.adb.secure=0'], [b'ro.adb.secure=1', b'ro.adb.secure=0']):
            with self.assertRaisesRegex(RuntimeError, 'ambiguous'):
                release_pad.verify_properties(lines, {'ro.adb.secure': '1'})


if __name__ == '__main__':
    unittest.main()
