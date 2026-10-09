"""Keep generic native delivery equivalent to the audited original patchers."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'scripts'))
import native_patch_catalog as native
import patch_flutter


def generic_patch(data, profile):
    """Model the equal-size content-guarded runtime; never guess an offset."""
    checksum = hashlib.sha256(data).hexdigest()
    if checksum == profile['after']:
        return data
    if checksum not in (profile['before'], *profile['legacy']):
        raise RuntimeError('Unsupported input content.')
    result = bytearray(data)
    for offset, before, after in profile['sites']:
        original, replacement = bytes.fromhex(before), bytes.fromhex(after)
        current = result[offset:offset + len(original)]
        if current == replacement:
            continue
        if current != original:
            raise RuntimeError('Unexpected native patch instruction.')
        result[offset:offset + len(original)] = replacement
    if len(result) != len(data) or hashlib.sha256(result).hexdigest() != profile['after']:
        raise RuntimeError('Unexpected output content.')
    return bytes(result)


class NativePatchCatalogTests(unittest.TestCase):
    def setUp(self):
        self.value = native.catalog()

    def reject(self, edit, message=''):
        value = copy.deepcopy(self.value)
        edit(value)
        with self.assertRaisesRegex(RuntimeError, message):
            native.validate_catalog(value)

    def test_catalog_is_json_and_deterministic(self):
        data = native.dump_catalog()
        self.assertEqual(data, native.dump_catalog())
        self.assertEqual(json.loads(data), self.value)
        self.assertEqual(native.validate_catalog(self.value), self.value)
        self.assertEqual(len(self.value['profiles']), 13)
        self.assertEqual(len(self.value['targets']), 10)
        self.assertEqual(set(row['feature'] for row in self.value['profiles']), native.FEATURES)
        self.assertEqual([row['id'] for row in self.value['profiles']],
                         sorted(row['id'] for row in self.value['profiles']))
        self.assertNotIn('avd_name', data)
        self.assertNotIn('incremental', data)

    def test_all_flutter_profiles_have_exact_imported_sites_and_legacy_inputs(self):
        rows = {row['before']: row for row in self.value['profiles'] if row['feature'] == 'flutter'}
        self.assertEqual(set(rows), set(patch_flutter.PROFILES))
        for before, source in patch_flutter.PROFILES.items():
            row = rows[before]
            self.assertEqual(row['after'], source['output'])
            self.assertEqual(row['legacy'], list(source.get('legacy', ())))
            self.assertEqual(row['sites'], [list(site) for site in sorted(source['sites'])])

    def test_hwui_and_remaining_sites_use_the_original_patchers(self):
        import apply_navigation_fix as navigation
        import patch_assistant as assistant
        import patch_composer as composer
        import patch_lockscreen_video as lockscreen
        import patch_pad_hwui as hwui
        import patch_rear_display as rear
        rows = {row['before']: row for row in self.value['profiles']}
        for before, source in hwui.PROFILES.items():
            self.assertEqual(rows[before]['after'], source['after'])
            self.assertEqual(rows[before]['sites'], [[source['site'], source['original'].hex(),
                                                    hwui.REPLACEMENT.hex()]])
        for before, after, sites in ((assistant.BEFORE, assistant.AFTER, assistant.SITES),
                                     (composer.BEFORE, composer.AFTER, composer.SITES),
                                     (rear.BEFORE, rear.AFTER, rear.SITES),
                                     (lockscreen.BEFORE, lockscreen.AFTER, lockscreen.SITES),
                                     (navigation.AOT_BEFORE, navigation.AOT_AFTER, navigation.AOT_SITES),
                                     (navigation.WATCHDOG_BEFORE, navigation.WATCHDOG_AFTER,
                                      navigation.WATCHDOG_SITES)):
            self.assertEqual(rows[before]['after'], after)
            self.assertEqual(rows[before]['sites'], [[offset, left.hex(), right.hex()]
                                                    for offset, left, right in sorted(sites)])

    def test_composer_alpha_and_rear_timing_form_one_audited_chain(self):
        rows = {row['feature']: row for row in self.value['profiles']
                if row['feature'] in ('composer', 'rear-display')}
        self.assertEqual(rows['composer']['after'], rows['rear-display']['before'])
        targets = {row['feature']: row for row in self.value['targets']
                   if row['feature'] in rows}
        self.assertEqual(targets['composer']['path'], targets['rear-display']['path'])
        self.assertEqual(targets['composer']['phase'], 'early')
        self.assertEqual(targets['rear-display']['phase'], 'early')
        self.reject(lambda value: next(row for row in value['targets']
                                        if row['feature'] == 'rear-display').update(phase='late'),
                    'shared target and boot phase')

    def test_system_and_app_targets_are_explicit_and_scoped(self):
        system = {row['feature']: row for row in self.value['targets'] if row['kind'] == 'system'}
        self.assertEqual(set(system), {'flutter', 'hwui', 'composer', 'rear-display'})
        self.assertTrue(all(row['phase'] == 'early' and not row['package'] for row in system.values()))
        self.assertEqual(system['flutter']['path'], '/system_ext/lib64/libhyper_os_flutter.so')
        self.assertEqual(system['hwui']['path'], '/system/lib64/libhwui.so')
        apps = [row for row in self.value['targets'] if row['kind'] == 'app']
        self.assertEqual(set(row['package'] for row in apps), native.APP_PACKAGES)
        self.assertTrue(all(row['phase'] == 'late' and row['path'] == '' for row in apps))
        self.assertEqual({row['library'] for row in apps if row['feature'] == 'navigation'},
                         {'libapp.so', 'libapp_launcher.so'})
        self.assertEqual([row['library'] for row in apps if row['feature'] == 'lockscreen-video'],
                         ['libfastplayer.so'])

    def test_rejects_schema_unknown_keys_and_unsafe_identifiers(self):
        self.reject(lambda value: value.update(schema=True), 'schema')
        self.reject(lambda value: value.update(extra=True), 'schema')
        self.reject(lambda value: value['profiles'][0].update(extra=True), 'profile')
        self.reject(lambda value: value['profiles'][0].update(id='../unsafe'), 'profile')
        self.reject(lambda value: value['profiles'][0].update(feature=[]), 'profile')

    def test_rejects_bad_hashes_and_duplicate_hash_ownership(self):
        self.reject(lambda value: value['profiles'][0].update(before='A' * 64), 'hash')
        self.reject(lambda value: value['profiles'][0].update(legacy=[None]), 'hash')
        self.reject(lambda value: value['profiles'][0].update(legacy=[value['profiles'][0]['before']]), 'hash')
        self.reject(lambda value: value['profiles'][1].update(before=value['profiles'][0]['before']),
                    'input hash')
        self.reject(lambda value: value['profiles'][1].update(legacy=[value['profiles'][0]['before']]),
                    'input hash')
        self.reject(lambda value: value['profiles'][1].update(after=value['profiles'][0]['after']),
                    'output hash')
        self.reject(lambda value: value['profiles'][1].update(before=value['profiles'][0]['after']),
                    'hash chain')

    def test_rejects_invalid_hex_length_offsets_and_overlap(self):
        for site in ((0, '0g', '00'), (0, '0', '00'), (0, '00', '0000'),
                     (0, '00', '00'), (-1, '00', '01'), (True, '00', '01'),
                     (0x100000000, '00', '01'), (0xffffffff, '0000', '0101')):
            with self.subTest(site=site):
                self.reject(lambda value: value['profiles'][0].update(sites=[list(site)]))
        self.reject(lambda value: value['profiles'][0].update(
            sites=[[10, '0000', '0101'], [11, '00', '01']]), 'Overlapping')

    def test_rejects_unsafe_paths_unknown_apps_and_unmatched_libraries(self):
        def system(value):
            return next(row for row in value['targets'] if row['kind'] == 'system')
        def app(value):
            return next(row for row in value['targets'] if row['kind'] == 'app')
        for path in ('/data/local/tmp/lib.so', '/system/../lib.so', '/system//lib.so',
                     '/system/lib.so\n', 'system/lib.so'):
            self.reject(lambda value: system(value).update(path=path), 'Unsafe system')
        self.reject(lambda value: system(value).update(library='libunrelated.so'), 'Unsafe system')
        self.reject(lambda value: app(value).update(library='../lib.so'), 'target')
        self.reject(lambda value: app(value).update(package='com.unknown.app'), 'Unsafe app')
        self.reject(lambda value: app(value).update(path='/data/app/base.apk'), 'Unsafe app')
        self.reject(lambda value: value['targets'].append(value['targets'][0]), 'Duplicate')

    def test_real_flutter_fixture_and_previous_patch_match_generic_sites(self):
        path = Path(os.environ.get('HYPEROS_AVD_FLUTTER18_ARTIFACT', str(
            REPO / 'work/os4-r3-build/input/hongkong-4.0.18/flutter.bin')))
        if not path.is_file():
            self.skipTest('Original proprietary Flutter artifact is not distributed in Git.')
        data = path.read_bytes()
        checksum = hashlib.sha256(data).hexdigest()
        self.assertIn(checksum, patch_flutter.PROFILES)
        row = next(row for row in self.value['profiles'] if row['before'] == checksum)
        result = generic_patch(data, row)
        self.assertEqual(result, patch_flutter.patch(data))
        self.assertEqual(generic_patch(result, row), result)
        source = patch_flutter.PROFILES[checksum]
        if source.get('glyph_raster_site'):
            offset = source['glyph_raster_site']
            previous = bytearray(result)
            previous[offset:offset + 4] = data[offset:offset + 4]
            self.assertIn(hashlib.sha256(previous).hexdigest(), row['legacy'])
            self.assertEqual(generic_patch(previous, row), patch_flutter.patch(previous))
        changed = bytearray(data)
        changed[-1] ^= 1
        with self.assertRaisesRegex(RuntimeError, 'Unsupported input content'):
            generic_patch(changed, row)

    def test_cli_emits_the_same_catalog_without_workspace_dependencies(self):
        result = subprocess.run([sys.executable, str(REPO / 'scripts/native_patch_catalog.py')],
                                check=True, capture_output=True, text=True, cwd=REPO)
        self.assertEqual(result.stdout, native.dump_catalog())


if __name__ == '__main__':
    unittest.main()
