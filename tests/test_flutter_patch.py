"""Check fail-closed binary patching, APK preservation and AVD isolation."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch as mock_patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import patch_flutter
import apply_flutter_fix
import apply_navigation_fix


class FlutterPatchTests(unittest.TestCase):
    def test_patch_is_guarded_and_idempotent(self):
        original = b'ELF header' + bytes.fromhex('12345678') + b'payload'
        fixed = b'ELF header' + bytes.fromhex('abcdef01') + b'payload'
        profiles = {patch_flutter.digest(original): {
            'name': 'fixture', 'output': patch_flutter.digest(fixed),
            'sites': [(10, '12345678', 'abcdef01')]}}
        with mock_patch.object(patch_flutter, 'PROFILES', profiles):
            self.assertEqual(patch_flutter.patch(original), fixed)
            self.assertEqual(patch_flutter.patch(fixed), fixed)
            with self.assertRaisesRegex(RuntimeError, 'Unsupported'):
                patch_flutter.patch(original + b'changed')

    def test_wrong_instruction_and_output_are_rejected(self):
        original = b'abcd'
        profile = {'name': 'fixture', 'output': patch_flutter.digest(b'efgh'),
                   'sites': [(0, b'ijkl'.hex(), b'efgh'.hex())]}
        with mock_patch.object(patch_flutter, 'PROFILES', {patch_flutter.digest(original): profile}):
            with self.assertRaisesRegex(RuntimeError, 'Unexpected'):
                patch_flutter.patch(original)
            profile['sites'] = [(0, original.hex(), b'zzzz'.hex())]
            with self.assertRaisesRegex(RuntimeError, 'output checksum'):
                patch_flutter.patch(original)

    def test_pinned_legacy_patch_migrates_with_padding(self):
        original = b'ABCD' + bytes(24) + b'tail'
        legacy = b'EFGH' + bytes(24) + b'tail'
        fixed = b'EFGH' + b'X' * 24 + b'tail'
        profile = {'name': 'fixture', 'output': patch_flutter.digest(fixed),
                   'legacy': (patch_flutter.digest(legacy),),
                   'sites': [(0, b'ABCD'.hex(), b'EFGH'.hex()),
                             (4, bytes(24).hex(), (b'X' * 24).hex())]}
        with mock_patch.object(patch_flutter, 'PROFILES', {patch_flutter.digest(original): profile}):
            self.assertEqual(patch_flutter.patch(original), fixed)
            self.assertEqual(patch_flutter.patch(legacy), fixed)
            self.assertEqual(patch_flutter.patch(fixed), fixed)
            with self.assertRaisesRegex(RuntimeError, 'Unsupported'):
                patch_flutter.patch(legacy[:-1])

    def test_real_engine_profiles_when_available(self):
        root = Path(__file__).resolve().parents[1] / 'work/os4-official/work'
        paths = [root / 'flutter-render-fix/system-original.so',
                 root / 'flutter-render-fix/com.miui.home.so.original',
                 root / 'nav-research/weather-flutter.so']
        if not all(p.is_file() for p in paths):
            self.skipTest('Verified proprietary engines are not distributed in Git.')
        for path in paths:
            original = path.read_bytes()
            _, profile = patch_flutter.profile(original)
            fixed = patch_flutter.patch(original)
            self.assertEqual(patch_flutter.digest(fixed), profile['output'])
            self.assertEqual(patch_flutter.patch(fixed), fixed)
            self.assertEqual(len(fixed), len(original))
            covered = bytearray(len(original))
            legacy = bytearray(original)
            for offset, before, after in profile['sites']:
                size = len(bytes.fromhex(before))
                covered[offset:offset + size] = bytes([1]) * size
                if (size == 4 and before not in ('f30300aa', 'f30308aa')
                        and offset not in profile.get('shadow_sites', ())):
                    legacy[offset:offset + size] = bytes.fromhex(after)
            self.assertIn(patch_flutter.digest(legacy), profile['legacy'])
            self.assertEqual(patch_flutter.patch(legacy), fixed)
            self.assertFalse(any(a != b and not allowed for a, b, allowed
                                 in zip(original, fixed, covered)))
            if profile.get('shadow_sites'):
                previous = bytearray(fixed)
                for offset, before, _ in profile['sites']:
                    if offset in profile['shadow_sites']:
                        previous[offset:offset + 4] = bytes.fromhex(before)
                self.assertIn(patch_flutter.digest(previous), profile['legacy'])
                self.assertEqual(patch_flutter.patch(previous), fixed)

    def test_apk_overlay_preserves_other_entries_and_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            original, fixed = Path(tmp) / 'original.apk', Path(tmp) / 'fixed.apk'
            entry = zipfile.ZipInfo('assets/user-data.bin')
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = 0o644 << 16
            with zipfile.ZipFile(original, 'w') as archive:
                archive.writestr(entry, b'untouched')
                archive.writestr(apply_flutter_fix.ENGINE_ENTRY, b'original engine')
            before = original.read_bytes()
            apply_flutter_fix.rewrite_apk(original, fixed, b'fixed engine')
            self.assertEqual(original.read_bytes(), before)
            with zipfile.ZipFile(fixed) as archive:
                self.assertEqual(archive.read('assets/user-data.bin'), b'untouched')
                self.assertEqual(archive.getinfo('assets/user-data.bin').external_attr, entry.external_attr)
                self.assertEqual(archive.read(apply_flutter_fix.ENGINE_ENTRY), b'fixed engine')

    def test_missing_engine_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            original, fixed = Path(tmp) / 'original.apk', Path(tmp) / 'fixed.apk'
            with zipfile.ZipFile(original, 'w') as archive:
                archive.writestr('assets/file', b'no engine')
            with self.assertRaisesRegex(RuntimeError, 'exactly one'):
                apply_flutter_fix.rewrite_apk(original, fixed, b'fixed')

    def test_other_avds_are_refused_before_adb(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'local').mkdir()
            (root / 'local/build.json').write_text(json.dumps({'source': 'official-hongkong-ota'}))
            with mock_patch.object(apply_flutter_fix, 'ROOT', root), mock_patch.object(apply_flutter_fix, 'root') as adb:
                for config in ({'name': 'Pixel_10_Pro', 'port': 5554},
                               {'name': 'HyperOS_4_Official_API_37', 'port': 5554}):
                    with self.assertRaisesRegex(RuntimeError, 'restricted'):
                        apply_flutter_fix.install(config)
                adb.assert_not_called()

    def test_unknown_launcher_aot_is_refused(self):
        with self.assertRaisesRegex(RuntimeError, 'Unsupported launcher AOT'):
            apply_navigation_fix.patch_aot(b'unknown launcher')
        with self.assertRaisesRegex(RuntimeError, 'Unsupported launcher watchdog'):
            apply_navigation_fix.patch_watchdog(b'unknown launcher')

    def test_real_launcher_watchdog_patch_when_available(self):
        apk = Path(__file__).resolve().parents[1] / 'work/os4-official/work/flutter-render-fix/home-original.apk'
        if not apk.is_file():
            self.skipTest('Verified launcher APK is not distributed in Git.')
        with zipfile.ZipFile(apk) as archive:
            original = archive.read('lib/arm64-v8a/libapp_launcher.so')
        fixed = apply_navigation_fix.patch_watchdog(original)
        self.assertEqual(patch_flutter.digest(fixed), apply_navigation_fix.WATCHDOG_AFTER)
        self.assertEqual(apply_navigation_fix.patch_watchdog(fixed), fixed)
        offset = apply_navigation_fix.WATCHDOG_OFFSET
        self.assertEqual(len(fixed), len(original))
        self.assertEqual(fixed[:offset], original[:offset])
        self.assertEqual(fixed[offset + 4:], original[offset + 4:])
        with self.assertRaisesRegex(RuntimeError, 'Unsupported launcher watchdog'):
            apply_navigation_fix.patch_watchdog(original + b'changed')

    def test_real_launcher_deadline_patch_when_available(self):
        apk = Path(__file__).resolve().parents[1] / 'work/os4-official/work/flutter-render-fix/home-original.apk'
        if not apk.is_file():
            self.skipTest('Verified launcher APK is not distributed in Git.')
        with zipfile.ZipFile(apk) as archive:
            original = archive.read('lib/arm64-v8a/libapp.so')
        fixed = apply_navigation_fix.patch_aot(original)
        self.assertEqual(patch_flutter.digest(fixed), apply_navigation_fix.AOT_AFTER)
        self.assertEqual(len(fixed), len(original))
        self.assertEqual(apply_navigation_fix.patch_aot(fixed), fixed)
        offset = apply_navigation_fix.AOT_OFFSET
        self.assertEqual(fixed[:offset], original[:offset])
        self.assertEqual(fixed[offset + 8:], original[offset + 8:])
        with self.assertRaisesRegex(RuntimeError, 'Unsupported launcher AOT'):
            apply_navigation_fix.patch_aot(original + b'changed')


if __name__ == '__main__':
    unittest.main()
