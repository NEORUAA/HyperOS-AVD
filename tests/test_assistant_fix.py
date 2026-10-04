"""Guard the XiaoAI native patch and the unchanged signed APK boundary."""
import hashlib
from pathlib import Path
import subprocess
import sys
import unittest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'scripts'))
from patch_assistant import APK_SHA256, AFTER, MANIFEST, NATIVE, image_replacements, native_from_apk, patch
from apply_assistant_fix import BOOT_SCRIPT

SOURCE = REPO / 'work/os4-official/work/assistant-transition'


class AssistantGuardTest(unittest.TestCase):
    def test_unknown_engine_and_apk_are_rejected(self):
        for operation in (patch, native_from_apk):
            with self.assertRaisesRegex(RuntimeError, 'Unsupported XiaoAI'):
                operation(b'unknown update')

    def test_boot_script_is_valid_shell(self):
        subprocess.run(['sh', '-n'], input=BOOT_SCRIPT, text=True, check=True,
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


if __name__ == '__main__':
    unittest.main()
