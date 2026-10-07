"""Verify source-pinned tablet identity and the compiled shared Settings RRO."""
import copy
import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from build_os4_pad import identity_values, settings_replacements, verify_identity_sources


def identity_fixture():
    """Conflicting source identities expose accidental partition merging."""
    parts = ('system', 'system_ext', 'product', 'vendor', 'odm', 'mi_ext')
    props = {}
    for part in parts:
        props[part] = (
            f'ro.product.{part}.brand=Brand-{part}\n'
            f'ro.product.{part}.device=Device-{part}\n'
            f'ro.product.{part}.manufacturer=Maker-{part}\n'
            f'ro.product.{part}.model=Model-{part}\n'
            f'ro.product.{part}.name=Name-{part}\n'
            f'ro.product.{part}.marketname=Market-{part}\n'
            f'ro.product.{part}.cert=Cert-{part}\n'
            f'ro.{part}.build.fingerprint=Brand/{part}/device:17/ID/100:user/release-keys\n'
            f'ro.product.model=Stale-global-{part}\n'
            f'ro.build.fingerprint=Stale-global-fingerprint-{part}\n'
        ).encode()
    props['odm'] += (
        b'ro.product.model_for_attestation=Attested-tablet\n'
        b'ro.product.device_for_attestation=Attested-device\n'
        b'ro.vendor.build.fingerprint=Wrong-vendor-fingerprint-from-odm\n'
    )
    props['product'] += b'ro.build.characteristics=tablet\n'
    props['vendor'] += (
        b'ro.product.first_api_level=35\n'
        b'ro.product.board=Public-board-name\n'
        b'ro.soc.model=Public-SoC-name\n'
        b'ro.soc.manufacturer=Public-SoC-maker\n'
        b'ro.boot.hardware=physical-device\n'
        b'ro.board.platform=physical-platform\n'
        b'ro.hardware.egl=physical-egl\n'
        b'ro.hardware.gralloc=physical-gralloc\n'
    )
    metadata = (b'pre-device=Fixture-tablet\npost-sdk-level=37\n'
                b'post-build=Brand/ota/tablet:17/OTA-ID/200:user/release-keys\n')
    return props, metadata


def source_profile(props, metadata):
    sources = {part + '.build.prop': data for part, data in props.items()}
    sources['ota.metadata'] = metadata
    return {'source_sha256': {name: hashlib.sha256(data).hexdigest()
                              for name, data in sources.items()},
            'properties': identity_values(props, metadata)}


class PadIdentitySourcesTests(unittest.TestCase):
    def setUp(self):
        self.props, self.metadata = identity_fixture()
        self.profile = source_profile(self.props, self.metadata)

    def test_global_identity_uses_odm_and_ota_without_merging_partition_fingerprints(self):
        before = self.props.copy()
        selected = identity_values(self.props, self.metadata)
        for field in ('brand', 'device', 'manufacturer', 'model', 'name', 'marketname'):
            self.assertEqual(selected['ro.product.' + field],
                             selected['ro.product.odm.' + field])
        self.assertEqual(selected['ro.product.model'], 'Model-odm')
        self.assertEqual(selected['ro.product.model_for_attestation'], 'Attested-tablet')
        self.assertEqual(selected['ro.build.fingerprint'],
                         'Brand/ota/tablet:17/OTA-ID/200:user/release-keys')
        for part in ('system', 'system_ext', 'product', 'vendor', 'odm'):
            self.assertEqual(selected[f'ro.{part}.build.fingerprint'],
                             f'Brand/{part}/device:17/ID/100:user/release-keys')
        self.assertEqual(self.props, before)

    def test_public_board_and_soc_do_not_admit_hardware_selectors(self):
        selected = identity_values(self.props, self.metadata)
        self.assertEqual(selected['ro.product.board'], 'Public-board-name')
        self.assertEqual(selected['ro.product.first_api_level'], '35')
        self.assertEqual(selected['ro.soc.model'], 'Public-SoC-name')
        self.assertEqual(selected['ro.build.characteristics'], 'tablet')
        for key in ('ro.boot.hardware', 'ro.board.platform',
                    'ro.hardware.egl', 'ro.hardware.gralloc'):
            self.assertNotIn(key, selected)

    def test_all_partition_bytes_and_metadata_are_pinned_even_without_identity_changes(self):
        verify_identity_sources(self.profile, self.props, self.metadata)
        for part in self.props:
            changed = self.props.copy()
            changed[part] += b'# Unselected source bytes changed.\n'
            with self.subTest(part=part), self.assertRaisesRegex(RuntimeError, 'verified OTA'):
                verify_identity_sources(self.profile, changed, self.metadata)
        with self.assertRaisesRegex(RuntimeError, 'verified OTA'):
            verify_identity_sources(self.profile, self.props, self.metadata + b'ignored-key=changed\n')

    def test_forged_source_hashes_and_incomplete_pin_maps_are_rejected(self):
        for alias in ('system.build.prop', 'ota.metadata'):
            forged = copy.deepcopy(self.profile)
            forged['source_sha256'][alias] = hashlib.sha256(b'other source').hexdigest()
            with self.subTest(alias=alias), self.assertRaisesRegex(RuntimeError, 'verified OTA'):
                verify_identity_sources(forged, self.props, self.metadata)
        for operation in ('missing', 'extra'):
            forged = copy.deepcopy(self.profile)
            if operation == 'missing':
                del forged['source_sha256']['mi_ext.build.prop']
            else:
                forged['source_sha256']['unverified.build.prop'] = hashlib.sha256(b'').hexdigest()
            with self.subTest(operation=operation), self.assertRaisesRegex(RuntimeError, 'verified OTA'):
                verify_identity_sources(forged, self.props, self.metadata)

    def test_profile_cannot_inject_hal_selectors_despite_valid_source_hashes(self):
        for key in ('ro.boot.hardware', 'ro.board.platform',
                    'ro.hardware.egl', 'ro.hardware.gralloc'):
            forged = copy.deepcopy(self.profile)
            forged['properties'][key] = 'forged-selector'
            with self.subTest(key=key), self.assertRaisesRegex(RuntimeError, 'public identity'):
                verify_identity_sources(forged, self.props, self.metadata)

    def test_profile_cannot_forge_public_model_or_global_fingerprint(self):
        for key in ('ro.product.model', 'ro.build.fingerprint', 'ro.vendor.build.fingerprint'):
            forged = copy.deepcopy(self.profile)
            forged['properties'][key] = 'different-public-identity'
            with self.subTest(key=key), self.assertRaisesRegex(RuntimeError, 'public identity'):
                verify_identity_sources(forged, self.props, self.metadata)


class PadSettingsRroTests(unittest.TestCase):
    def test_stale_extra_resources_are_refused_before_building_or_signing(self):
        for relative in ('res/values/phone_aod.xml', 'res/drawable/phone_logo.xml'):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as temporary:
                folder = Path(temporary)
                extra = folder / relative
                extra.parent.mkdir(parents=True)
                extra.write_text('<resources><bool name="phone_only">true</bool></resources>')
                with patch('build_os4_pad.subprocess.run') as command:
                    with self.assertRaisesRegex(RuntimeError, 'Unexpected cached resources'):
                        settings_replacements('/unused/sdk', folder)
                    command.assert_not_called()

    def test_signed_rro_contains_only_shared_awake_defaults(self):
        from patch_gnss import java
        candidates = (os.environ.get('ANDROID_SDK_ROOT'), os.environ.get('ANDROID_HOME'),
                      Path.home() / 'Library/Android/sdk')
        required = ('build-tools/37.0.0/aapt2', 'build-tools/37.0.0/apksigner',
                    'platforms/android-36/android.jar')
        sdk = next((Path(item).expanduser() for item in candidates if item and
                    all((Path(item).expanduser() / name).is_file() for name in required)), None)
        if sdk is None:
            self.skipTest('Android SDK 37 build-tools and the API 36 framework are unavailable.')
        try:
            java_binary = Path(java())
        except RuntimeError as error:
            self.skipTest(str(error))
        if not (java_binary.parent / 'keytool').is_file():
            self.skipTest('A JDK with keytool is needed to sign the resource-only RRO.')
        environment = dict(os.environ, JAVA_HOME=str(java_binary.parent.parent))
        version = subprocess.run([str(java_binary), '-version'], env=environment,
                                 check=False, capture_output=True, timeout=15)
        if version.returncode:
            self.skipTest('The configured Java runtime is unavailable.')
        tools = sdk / 'build-tools/37.0.0'
        with tempfile.TemporaryDirectory(prefix='pad-settings-rro-test-') as temporary:
            folder = Path(temporary)
            edits, marker = settings_replacements(sdk, folder)
            apk_key = 'product/overlay/HyperOSAVDSettingsDefaults/SettingsDefaults.apk'
            log_key = 'system_ext/bin/kill_HyperOS_Log.sh'
            self.assertEqual(set(edits), {apk_key, log_key})
            apk_data, mode, label = edits[apk_key]
            self.assertEqual((mode, label), (0o644, 'u:object_r:system_file:s0'))
            self.assertEqual(edits[log_key][1:], (0o755, 'u:object_r:system_file:s0'))
            self.assertEqual(marker['settings_overlay_sha256'], hashlib.sha256(apk_data).hexdigest())
            self.assertEqual(marker['log_script_sha256'], hashlib.sha256(edits[log_key][0]).hexdigest())
            self.assertEqual(marker['sleep_timeout'], -1)
            self.assertEqual(marker['first_boot_stay_on_while_plugged_in'], 1)
            self.assertEqual(marker['stay_on_while_plugged_in'], 7)
            apk = folder / 'SettingsDefaults.apk'
            self.assertEqual(apk.read_bytes(), apk_data)
            with zipfile.ZipFile(apk) as archive:
                self.assertIn('resources.arsc', archive.namelist())
                self.assertFalse(any(name.endswith('.dex') for name in archive.namelist()))
            verified = subprocess.run([str(tools / 'apksigner'), 'verify', '--verbose', str(apk)],
                                      env=environment, check=True, capture_output=True, text=True)
            self.assertIn('Verifies', verified.stdout)
            manifest = subprocess.run([str(tools / 'aapt2'), 'dump', 'xmltree', str(apk),
                                       '--file', 'AndroidManifest.xml'],
                                      check=True, capture_output=True, text=True).stdout
            self.assertRegex(manifest, r'android:targetPackage[^\n]*="com\.android\.providers\.settings"')
            self.assertRegex(manifest, r'android:isStatic[^\n]*(?:0xffffffff|true)')
            self.assertRegex(manifest, r'android:hasCode[^\n]*(?:=false|0x0\b)')
            self.assertRegex(manifest, r'android:priority[^\n]*(?:0x3e8|1000)\b')
            resources = subprocess.run([str(tools / 'aapt2'), 'dump', 'resources', str(apk)],
                                       check=True, capture_output=True, text=True).stdout
            blocks = {}
            current = None
            for line in resources.splitlines():
                match = re.search(r'\bresource 0x[0-9a-f]+ ([a-z]+/[A-Za-z0-9_]+)', line)
                if match:
                    current = match[1]
                    blocks[current] = []
                elif current:
                    blocks[current].append(line.strip())
            expected_resources = {'integer/def_screen_off_timeout': '2147483647',
                                  'integer/def_sleep_timeout': '-1',
                                  'bool/def_stay_on_while_plugged_in': 'true'}
            self.assertEqual(set(blocks), set(expected_resources), resources)
            for name, value in expected_resources.items():
                self.assertIn('() ' + value, blocks[name], resources)


if __name__ == '__main__':
    unittest.main()
