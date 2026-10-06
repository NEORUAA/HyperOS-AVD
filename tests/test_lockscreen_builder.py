"""Check phone r3 native video baking and release preflight source guards."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch


REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'scripts'))
from build_os4_official import lockscreen_video_replacements
import package_release
import patch_lockscreen_video as video
from phone_profile import profile

FIXTURES = REPO / 'work/os4-r3-build/work/fastplayer-analysis'
OLD = '4.0.17.0.XFRCNXM'
NEW = '4.0.18.0.XFRCNXM'


class LockscreenBuilderTests(unittest.TestCase):
    def apk(self):
        path = FIXTURES / 'MIUIAod.apk'
        if not path.is_file():
            self.skipTest('Original proprietary signed MIUIAod APK is not distributed in Git.')
        data = path.read_bytes()
        self.assertEqual(hashlib.sha256(data).hexdigest(), video.APK_SHA256)
        return data

    def test_r3_adds_only_the_external_native_library_and_preserves_signed_apk(self):
        apk = self.apk()
        before = hashlib.sha256(apk).hexdigest()
        replacements, manifest = lockscreen_video_replacements(profile(NEW), apk)
        self.assertEqual(set(replacements), {video.NATIVE.lstrip('/')})
        native, mode, label = replacements[video.NATIVE.lstrip('/')]
        self.assertEqual(hashlib.sha256(native).hexdigest(), video.AFTER)
        self.assertEqual((mode, label), (0o644, 'u:object_r:system_lib_file:s0'))
        self.assertEqual(manifest, video.MANIFEST)
        self.assertIsNot(manifest, video.MANIFEST)
        self.assertEqual(hashlib.sha256(apk).hexdigest(), before)
        self.assertNotIn(video.APK.lstrip('/'), replacements)

    def test_legacy_phone_has_no_new_replacements_or_receipt(self):
        with patch.object(video, 'native_from_apk') as extract:
            self.assertEqual(lockscreen_video_replacements(profile(OLD), b'unused'), ({}, None))
        extract.assert_not_called()

    def test_foreign_firmware_and_unpinned_r3_sources_are_refused_before_extraction(self):
        selected = profile(NEW)
        cases = (({'hyperos': '4.0.18.0.XOCCNXM'}, 'profile'),
                 ({**selected, 'pins': {}}, 'source pin'),
                 ({**selected, 'pins': {**selected['pins'], 'aod_apk': 'unverified'}}, 'source pin'))
        for value, message in cases:
            with self.subTest(profile=value['hyperos']), patch.object(video, 'native_from_apk') as extract:
                with self.assertRaisesRegex(RuntimeError, message):
                    lockscreen_video_replacements(value, b'unknown APK')
                extract.assert_not_called()
        with self.assertRaisesRegex(RuntimeError, 'Unsupported MIUIAod APK'):
            lockscreen_video_replacements(selected, b'unknown APK')

    def test_preflight_reads_and_accepts_exact_signed_apk_and_patched_native(self):
        apk = self.apk()
        native = video.native_from_apk(apk)
        read = Mock(side_effect={video.APK: apk, video.NATIVE: native}.__getitem__)
        self.assertEqual(package_release.verify_lockscreen_video(
            profile(NEW), {'lockscreen_video_fix': dict(video.MANIFEST)}, read), 2)
        self.assertEqual([call.args[0] for call in read.call_args_list], [video.APK, video.NATIVE])

    def test_preflight_requires_exact_receipt_and_source_pin_before_image_reads(self):
        selected = profile(NEW)
        receipts = [None, {}, {**video.MANIFEST, 'revision': 0},
                    *[{**video.MANIFEST, key: 'unverified'} for key in video.MANIFEST
                      if key != 'revision']]
        for receipt in receipts:
            read = Mock()
            with self.subTest(receipt=receipt), self.assertRaisesRegex(RuntimeError, 'metadata or source pin'):
                package_release.verify_lockscreen_video(selected, {'lockscreen_video_fix': receipt}, read)
            read.assert_not_called()
        read = Mock()
        with self.assertRaisesRegex(RuntimeError, 'metadata or source pin'):
            package_release.verify_lockscreen_video({**selected, 'pins': {}},
                                                    {'lockscreen_video_fix': video.MANIFEST}, read)
        read.assert_not_called()

    def test_preflight_refuses_apk_changes_and_original_or_corrupt_external_native(self):
        apk = self.apk()
        native = video.native_from_apk(apk)
        original = (FIXTURES / 'libfastplayer.so').read_bytes()
        for path, files in ((video.APK, {video.APK: apk + b'changed', video.NATIVE: native}),
                            (video.NATIVE, {video.APK: apk, video.NATIVE: original}),
                            (video.NATIVE, {video.APK: apk, video.NATIVE: native + b'changed'})):
            with self.subTest(path=path), self.assertRaisesRegex(RuntimeError, 'checksum mismatch: ' + path):
                package_release.verify_lockscreen_video(profile(NEW),
                    {'lockscreen_video_fix': video.MANIFEST}, files.__getitem__)

    def test_preflight_leaves_legacy_phone_and_pad_requirements_unchanged(self):
        for version in (OLD, '4.0.6.0.XOCCNXM'):
            read = Mock()
            self.assertEqual(package_release.verify_lockscreen_video({'hyperos': version}, {}, read), 0)
            read.assert_not_called()

    def test_release_metadata_keeps_receipt_only_for_phone_r3(self):
        for version in (OLD, NEW):
            selected = profile(version)
            build = {'source': 'official-hongkong-ota', 'hyperos': version,
                     'archive_sha256': selected['archive_sha256'], 'android_api': 37,
                     'adb_authentication': True, 'flutter_render_fix': 6,
                     'native_quickstep_identity': True, 'preinstalled_apps': {'apps': ['test']},
                     'avd_defaults': {'test': True}, 'lockscreen_video_fix': dict(video.MANIFEST)}
            with self.subTest(version=version), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / 'local').mkdir()
                (root / 'local/build.json').write_text(json.dumps(build))
                metadata = package_release.release_metadata(root, 'os4-official')
                if version == NEW:
                    self.assertEqual(metadata['build']['lockscreen_video_fix'], video.MANIFEST)
                else:
                    self.assertNotIn('lockscreen_video_fix', metadata['build'])


if __name__ == '__main__':
    unittest.main()
