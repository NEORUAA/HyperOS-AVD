"""Reject mixed OTA identities and preserve partition-specific public values."""
import copy
import hashlib
from pathlib import Path
import sys
import unittest
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from phone_profile import (PROP_PATHS, identity_values, profile,
                           profile_for_archive, profile_from_build, verify_source)


class PhoneProfileTests(unittest.TestCase):
    def fixture_partitions(self, directory):
        value = profile('4.0.18.0.XFRCNXM')
        value['source_partition_sha256'] = {}
        for part in set(PROP_PATHS) | {'mi_product'}:
            body = part.encode()
            (Path(directory) / (part + '.img')).write_bytes(body)
            value['source_partition_sha256'][part] = hashlib.sha256(body).hexdigest()
        return value

    def test_stale_mi_product_and_incomplete_partition_map_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            value = self.fixture_partitions(directory)
            (Path(directory) / 'mi_product.img').write_bytes(b'stale previous firmware')
            with patch('build_image.erofs') as read:
                with self.assertRaisesRegex(RuntimeError, 'partition: mi_product'):
                    verify_source(value, directory, b'')
                read.assert_not_called()
            del value['source_partition_sha256']['mi_product']
            with self.assertRaisesRegex(RuntimeError, 'partition pins'):
                verify_source(value, directory, b'')

    def test_empty_or_partial_native_pins_are_refused_before_reading_files(self):
        with tempfile.TemporaryDirectory() as directory:
            value = self.fixture_partitions(directory)
            for pins in ({}, {'hwui': value['pins']['hwui']}):
                with self.subTest(pins=pins), patch('build_image.erofs') as read:
                    value['pins'] = pins
                    with self.assertRaisesRegex(RuntimeError, 'source pins'):
                        verify_source(value, directory, b'')
                    read.assert_not_called()

    def test_new_build_cannot_omit_archive_identity(self):
        with self.assertRaisesRegex(RuntimeError, 'archive identity'):
            profile_from_build({'source': 'official-hongkong-ota', 'hyperos': '4.0.18.0.XFRCNXM'})

    def test_unknown_firmware_and_cross_version_archive_are_refused(self):
        with self.assertRaises(RuntimeError):
            profile_for_archive('0' * 64)
        with self.assertRaises(RuntimeError):
            profile('4.0.19.0.XFRCNXM')
        with self.assertRaisesRegex(RuntimeError, 'archive identity'):
            profile_from_build({'source': 'official-hongkong-ota',
                                'hyperos': '4.0.17.0.XFRCNXM',
                                'archive_sha256': '0' * 64})
        with self.assertRaisesRegex(RuntimeError, 'different firmware'):
            profile_from_build({'source': 'official-yingtian-ota'})

    def test_partition_identity_cannot_be_overwritten_by_later_partition(self):
        expected = profile()['properties']
        # Give every source a copy, with deliberate conflicts in non-owners.
        sources = {part: dict(expected) for part in PROP_PATHS}
        sources['odm']['ro.vendor.build.fingerprint'] = 'wrong-vendor'
        sources['system_dlkm']['ro.product.model'] = 'wrong-global'
        sources['vendor']['ro.product.model'] = 'wrong-vendor-global'
        encoded = {part: ''.join(k + '=' + v + '\n' for k, v in values.items()).encode()
                   for part, values in sources.items()}
        metadata = ('post-build=' + expected['ro.build.fingerprint'] + '\n').encode()
        self.assertEqual(identity_values(encoded, metadata, expected), expected)
        changed = copy.deepcopy(encoded)
        changed['vendor'] += b'ro.vendor.build.fingerprint=updated-vendor\n'
        self.assertEqual(identity_values(changed, metadata, expected)['ro.vendor.build.fingerprint'],
                         'updated-vendor')

    def test_profile_cannot_add_hal_selectors_or_drop_required_identity(self):
        expected = profile()['properties']
        sources = {part: ''.join(k + '=' + v + '\n' for k, v in expected.items()).encode()
                   for part in PROP_PATHS}
        metadata = ('post-build=' + expected['ro.build.fingerprint'] + '\n').encode()
        forged = {**expected, 'ro.boot.hardware': 'qualcomm'}
        with self.assertRaisesRegex(RuntimeError, 'property set'):
            identity_values(sources, metadata, forged)
        with self.assertRaisesRegex(RuntimeError, 'property set'):
            identity_values(sources, metadata, set(expected) - {'ro.product.model'})


if __name__ == '__main__':
    unittest.main()
