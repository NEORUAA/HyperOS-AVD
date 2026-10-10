"""Verify r3 HWUI preload scope, renderer selection and runtime compatibility."""
import hashlib
from pathlib import Path
import struct
import sys
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'scripts'))
from build_os4_official import native_hwui
from launch import vulkan_features
from os4_defaults import apply_gradient_blur_runtime
from patch_pad_hwui import (AFTER, BEFORE, PHONE_AFTER, PHONE_BEFORE, PHONE_PROFILE,
                            PROFILES, patch as patch_hwui, preload_symbol)
from phone_profile import profile


def build_info():
    value = profile('4.0.18.0.XFRCNXM')
    return {'source': 'official-hongkong-ota', 'hyperos': value['hyperos'],
            'archive_sha256': value['archive_sha256'], 'hwui_renderer': 'skiavk',
            'hwui': {'before': PHONE_BEFORE, 'after': PHONE_AFTER, 'profile': PHONE_PROFILE}}


class PhoneVulkanNativeTests(unittest.TestCase):
    def binary(self):
        path = REPO / 'work/os4-r3-build/input/hongkong-4.0.18/hwui.bin'
        if not path.is_file():
            self.skipTest('Original proprietary OTA library is not distributed in Git.')
        data = path.read_bytes()
        self.assertEqual(hashlib.sha256(data).hexdigest(), PHONE_BEFORE)
        return data

    def test_exact_function_branch_and_oem_shader_calls_are_preserved(self):
        data = self.binary()
        self.assertEqual(preload_symbol(data), (0x9ecc68, 0x50, 0x9ecc68))
        fixed = patch_hwui(data)
        self.assertEqual(hashlib.sha256(fixed).hexdigest(), PHONE_AFTER)
        self.assertEqual(patch_hwui(fixed), fixed)
        expected = bytearray(data)
        expected[0x9ecc78:0x9ecc7c] = bytes.fromhex('09000014')
        self.assertEqual(fixed, expected)
        instruction = struct.unpack_from('<I', fixed, 0x9ecc78)[0]
        self.assertEqual(instruction & 0xfc000000, 0x14000000)
        self.assertEqual(0x9ecc78 + (instruction & 0x3ffffff) * 4, 0x9ecc9c)
        # Keep force-dark preload, the blur singleton and CPU SkSL initialization.
        for site, target in ((0x9ecc9c, 0x9e81f0), (0x9ecca0, 0x44f1e8),
                             (0x9ecca4, 0xa4aba4)):
            call = struct.unpack_from('<I', fixed, site)[0]
            self.assertEqual(call & 0xfc000000, 0x94000000)
            immediate = call & 0x3ffffff
            if immediate & 0x2000000:
                immediate -= 0x4000000
            self.assertEqual(site + immediate * 4, target)

    def test_builder_binds_native_patch_to_r3_and_retains_r2_bytes(self):
        data = self.binary()
        self.assertEqual(native_hwui(profile(), data), (data, None))
        fixed, receipt = native_hwui(profile('4.0.18.0.XFRCNXM'), data)
        self.assertEqual(receipt, build_info()['hwui'])
        self.assertEqual(hashlib.sha256(fixed).hexdigest(), PHONE_AFTER)
        corrupted = bytearray(data)
        corrupted[0x9ecc9c] ^= 1
        with self.assertRaisesRegex(RuntimeError, 'Unsupported Xiaomi HWUI'):
            patch_hwui(corrupted)

    def test_pad_profile_and_output_are_unchanged(self):
        self.assertEqual(PROFILES[BEFORE]['after'], AFTER)
        self.assertEqual(PROFILES[BEFORE]['site'], 0x94adec)
        self.assertEqual(PROFILES[BEFORE]['target'], 0x94ae10)
        path = REPO / 'work/os4-pad/work/source-audit/libhwui-original.so'
        if path.is_file():
            data = path.read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), BEFORE)
            self.assertEqual(hashlib.sha256(patch_hwui(data)).hexdigest(), AFTER)


class PhoneVulkanRuntimeTests(unittest.TestCase):
    def test_submission_workaround_requires_verified_r3_marker_and_keeps_pad(self):
        expected = ['-feature', 'VulkanBatchedDescriptorSetUpdate,-GLAsyncSwap,'
                               '-VulkanQueueSubmitWithCommands']
        current = build_info()
        self.assertEqual(vulkan_features(current), expected)
        self.assertEqual(vulkan_features({'source': 'official-yingtian-ota'}),
                         ['-feature', 'VulkanBatchedDescriptorSetUpdate'])
        for value in ({}, {'source': 'gsi', 'hyperos': '3.0.2.0.WMCCNXM'},
                      {'source': 'official-hongkong-ota'},
                      {**current, 'source': 'gsi'},
                      {**current, 'hyperos': '3.0.2.0.WMCCNXM'},
                      {**current, 'hyperos': '4.0.17.0.XFRCNXM'},
                      {**current, 'hyperos': '4.0.19.0.XFRCNXM'},
                      {**current, 'hwui_renderer': 'skiagl'},
                      {**current, 'hwui': None}, {**current, 'hwui': {'after': PHONE_AFTER}},
                      {**current, 'hwui': {**current['hwui'], 'before': 'unverified'}},
                      {**current, 'hwui': {**current['hwui'], 'after': 'unverified'}},
                      {**current, 'hwui': {**current['hwui'], 'profile': 'unverified'}}):
            with self.subTest(value=value):
                self.assertEqual(vulkan_features(value), [])
        for digest in (None, 'unknown'):
            with self.subTest(digest=digest), \
                    self.assertRaisesRegex(RuntimeError, 'OTA archive identity disagree'):
                vulkan_features({**current, 'archive_sha256': digest})

    def test_workarounds_follow_image_identity_after_avd_rename(self):
        current = build_info()
        for name in ('HyperOS_4_Official_API_37', 'Any_Renamed_Phone_AVD',
                     'HyperOS_3_API_36'):
            with self.subTest(name=name):
                self.assertEqual(vulkan_features({**current, 'name': name}),
                                 ['-feature', 'VulkanBatchedDescriptorSetUpdate,-GLAsyncSwap,'
                                              '-VulkanQueueSubmitWithCommands'])
                self.assertEqual(vulkan_features({**current, 'name': name,
                                                  'hyperos': '4.0.17.0.XFRCNXM'}), [])

    def test_queue_disable_remains_phone_only_and_tracks_the_image_not_runtime_name(self):
        current = build_info()
        renamed = {**current, 'name': 'Any_Pad_Name', 'port': 5600,
                   'avd_path': '/custom/renamed.avd'}
        phone = vulkan_features(renamed)
        flags = phone[1].split(',')
        self.assertEqual(flags.count('-VulkanQueueSubmitWithCommands'), 1)
        self.assertIn('-GLAsyncSwap', flags)
        self.assertNotIn('VulkanQueueSubmitWithCommands', flags)
        self.assertEqual(vulkan_features({**renamed, 'source': 'official-yingtian-ota'}),
                         ['-feature', 'VulkanBatchedDescriptorSetUpdate'])
        self.assertEqual(vulkan_features({**renamed, 'source': 'unknown-ota'}), [])
        self.assertEqual(vulkan_features({**renamed, 'hwui_renderer': 'skiagl'}), [])

    def test_gradient_override_accepts_patched_r3_but_rejects_it_for_legacy_r2(self):
        config = {'name': 'Any_Renamed_Phone_AVD', 'port': 5584}
        for version in ('4.0.17.0.XFRCNXM', '4.0.18.0.XFRCNXM'):
            with self.subTest(version=version), patch('apply_flutter_fix.official'), \
                    patch('phone_profile.profile_from_build', return_value=profile(version)), \
                    patch('apply_flutter_fix.root', side_effect=[
                        'ranchu', config['name'], PHONE_AFTER + ' /system/lib64/libhwui.so',
                        'false']) as root:
                if '4.0.17.' in version:
                    with self.assertRaisesRegex(RuntimeError, 'Unsupported HWUI'):
                        apply_gradient_blur_runtime(config)
                else:
                    apply_gradient_blur_runtime(config)
                    self.assertEqual(root.call_count, 4)
                self.assertTrue(all(not call.args[1].startswith('setprop ')
                                    for call in root.call_args_list))


if __name__ == '__main__':
    unittest.main()
