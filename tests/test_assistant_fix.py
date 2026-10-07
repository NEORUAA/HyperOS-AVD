"""Guard the XiaoAI native patch and the unchanged signed APK boundary."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import unittest
import tempfile
from unittest.mock import patch as mock_patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'scripts'))
from patch_assistant import (APK, APK_SHA256, PAD_APK_SHA256, PHONE_SOURCE, PAD_SOURCE,
                             AFTER, MANIFEST, NATIVE, image_replacements, native_from_apk,
                             patch, profile)
import apply_assistant_fix
from apply_assistant_fix import BOOT_SCRIPT, boot_script

SOURCE = REPO / 'work/os4-official/work/assistant-transition'
PAD_APK = REPO / ('work/os4-pad/work/official-tree/extract-product-c2fede3d9b16b1b5/'
                 'priv-app/VoiceAssistAndroidT/VoiceAssistAndroidT.apk')


class AssistantGuardTest(unittest.TestCase):
    def test_unknown_engine_and_apk_are_rejected(self):
        for operation in (patch, native_from_apk):
            with self.assertRaisesRegex(RuntimeError, 'Unsupported XiaoAI'):
                operation(b'unknown update')
        for source in (PHONE_SOURCE, PAD_SOURCE):
            with self.assertRaisesRegex(RuntimeError, 'Unsupported XiaoAI APK'):
                native_from_apk(b'unknown update', source=source)
        with self.assertRaisesRegex(RuntimeError, 'Unsupported XiaoAI firmware'):
            profile('unknown-ota')

    def test_profiles_and_default_phone_script_remain_pinned(self):
        self.assertEqual(profile(), MANIFEST)
        self.assertEqual(profile(PHONE_SOURCE), MANIFEST)
        self.assertEqual(profile(PAD_SOURCE), {**MANIFEST, 'apk_sha256': PAD_APK_SHA256})
        self.assertEqual(boot_script(), BOOT_SCRIPT)
        self.assertEqual(hashlib.sha256(BOOT_SCRIPT.encode()).hexdigest(),
                         'a07892d0f08ac8e35c87929c2a7114998ee880bfdcef89137a96d060468ddd1f')
        script = boot_script(PAD_SOURCE)
        self.assertIn('APK_HASH=' + PAD_APK_SHA256, script)
        self.assertNotIn(APK_SHA256, script)
        self.assertIn('getprop ro.boot.hardware', script)
        self.assertIn('= yingtian ]', script)
        self.assertIn('= OS4.0.15.0.XBMCNXM ]', script)
        self.assertIn('LIB_HASH=' + AFTER, script)
        with self.assertRaisesRegex(RuntimeError, 'Unsupported XiaoAI firmware'):
            boot_script('unknown-ota')

    def test_phone_default_refuses_tablet_before_adb(self):
        import apply_flutter_fix
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / 'local').mkdir()
            (folder / 'local/build.json').write_text(json.dumps({'source': PAD_SOURCE}))
            with mock_patch.object(apply_flutter_fix, 'ROOT', folder), \
                    mock_patch.object(apply_flutter_fix, 'adb') as adb:
                with self.assertRaisesRegex(RuntimeError, 'firmware profile'):
                    apply_assistant_fix.install({'name': 'HyperOS_4_Pad9ProMax_API_37', 'port': 5582})
                adb.assert_not_called()

    def test_pad_disabled_module_is_preserved_without_mounts_or_files(self):
        config = {'name': 'HyperOS_4_Pad9ProMax_API_37', 'port': 5582}
        selected = profile(PAD_SOURCE)
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / 'local').mkdir()
            (folder / 'local/build.json').write_text(json.dumps({'source': PAD_SOURCE}))
            with mock_patch.object(apply_assistant_fix, 'ROOT', folder), \
                    mock_patch.object(apply_assistant_fix, 'official') as official, \
                    mock_patch.object(apply_assistant_fix, 'root', side_effect=[
                        config['name'], 'package:' + APK, PAD_APK_SHA256 + '  ' + APK,
                        json.dumps(selected), 'yes']) as root, \
                    mock_patch.object(apply_assistant_fix, 'adb') as adb:
                self.assertEqual(apply_assistant_fix.install(config, sources=(PAD_SOURCE,)), selected)
                official.assert_called_once_with(config, sources=(PAD_SOURCE,))
                self.assertEqual(root.call_count, 5)
                adb.assert_not_called()
                self.assertFalse((folder / 'work').exists())

    def test_pad_existing_module_checks_script_and_all_namespaces(self):
        config = {'name': 'HyperOS_4_Pad9ProMax_API_37', 'port': 5582}
        selected = profile(PAD_SOURCE)
        script_hash = hashlib.sha256(boot_script(PAD_SOURCE).encode()).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / 'local').mkdir()
            (folder / 'local/build.json').write_text(json.dumps({'source': PAD_SOURCE}))
            with mock_patch.object(apply_assistant_fix, 'ROOT', folder), \
                    mock_patch.object(apply_assistant_fix, 'official'), \
                    mock_patch.object(apply_assistant_fix, 'root', side_effect=[
                        config['name'], 'package:' + APK, PAD_APK_SHA256 + '  ' + APK,
                        json.dumps(selected), '', AFTER + '  ' + NATIVE,
                        script_hash + '  service.sh', '', '']) as root, \
                    mock_patch.object(apply_assistant_fix, 'adb') as adb:
                self.assertEqual(apply_assistant_fix.install(config, sources=(PAD_SOURCE,)), selected)
                self.assertEqual(root.call_args_list[-2].args[1],
                                 'sh ' + apply_assistant_fix.MODULE + '/service.sh')
                command = root.call_args_list[-1].args[1]
                self.assertIn('for pid in 1 ', command)
                self.assertIn('init.svc_debug_pid.hyos_spawner', command)
                self.assertIn('pidof zygote64', command)
                self.assertIn(PAD_APK_SHA256, command)
                self.assertIn(AFTER, command)
                subprocess.run(['sh', '-n'], input=command, text=True, check=True,
                               capture_output=True)
                adb.assert_not_called()

    def test_boot_script_is_valid_shell(self):
        for script in (BOOT_SCRIPT, boot_script(PAD_SOURCE)):
            subprocess.run(['sh', '-n'], input=script, text=True, check=True,
                           capture_output=True)

    @unittest.skipUnless((SOURCE / 'libmglnative2.so').is_file(), 'Verified private firmware is not bundled')
    def test_pinned_engine_has_only_two_changes(self):
        original = (SOURCE / 'libmglnative2.so').read_bytes()
        result = patch(original)
        self.assertEqual(hashlib.sha256(result).hexdigest(), AFTER)
        self.assertEqual(len(result), len(original))
        changed = [index for index, (a, b) in enumerate(zip(original, result)) if a != b]
        self.assertEqual(changed, [0x5527f, 0xa017d, 0xa017e, 0xa017f])
        self.assertEqual(patch(result), result)
        tampered = bytearray(original)
        tampered[-1] ^= 1
        with self.assertRaisesRegex(RuntimeError, 'Unsupported XiaoAI'):
            patch(tampered)

    @unittest.skipUnless((SOURCE / 'VoiceAssist.apk').is_file(), 'Verified private firmware is not bundled')
    def test_image_adds_native_library_without_rewriting_apk(self):
        apk = (SOURCE / 'VoiceAssist.apk').read_bytes()
        replacements, manifest = image_replacements(apk)
        self.assertEqual(hashlib.sha256(apk).hexdigest(), APK_SHA256)
        self.assertEqual(set(replacements), {NATIVE.lstrip('/')})
        data, mode, label = replacements[NATIVE.lstrip('/')]
        self.assertEqual(hashlib.sha256(data).hexdigest(), AFTER)
        self.assertEqual((mode, label), (0o644, 'u:object_r:system_lib_file:s0'))
        self.assertEqual(manifest, MANIFEST)

    @unittest.skipUnless(PAD_APK.is_file(), 'Verified private tablet firmware is not bundled')
    def test_tablet_signed_apk_uses_exact_shared_native_engine(self):
        apk = PAD_APK.read_bytes()
        self.assertEqual(hashlib.sha256(apk).hexdigest(), PAD_APK_SHA256)
        with self.assertRaisesRegex(RuntimeError, 'Unsupported XiaoAI APK'):
            native_from_apk(apk)
        replacements, manifest = image_replacements(apk, source=PAD_SOURCE)
        self.assertEqual(set(replacements), {NATIVE.lstrip('/')})
        data, mode, label = replacements[NATIVE.lstrip('/')]
        self.assertEqual(hashlib.sha256(data).hexdigest(), AFTER)
        self.assertEqual((mode, label), (0o644, 'u:object_r:system_lib_file:s0'))
        self.assertEqual(manifest, profile(PAD_SOURCE))
        self.assertEqual(hashlib.sha256(apk).hexdigest(), PAD_APK_SHA256)


if __name__ == '__main__':
    unittest.main()
