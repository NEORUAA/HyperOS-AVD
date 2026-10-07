"""Check fail-closed GNSS input guards and optional real 4.0.18 DEX evidence.

Set HYPEROS_AVD_GNSS_TEST_SOURCE and HYPEROS_AVD_GNSS_TEST_PATCHED to audit a
locally supplied original services.jar and its patched output without an AVD.
"""
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


REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'scripts'))
import patch_gnss

CALLBACKS = {'reportStatus(I)V': 2,
             'reportSvStatus(I[I[I[I[F[F[F[F[F[Ljava/lang/String;[J[D)V': 13,
             'reportLocation(ZLandroid/location/Location;)V': 3}
BINDER_CALL = ('invoke-static {v0}, Landroid/os/Binder;->withCleanCallingIdentity'
               '(Lcom/android/internal/util/FunctionalUtils$ThrowingRunnable;)V')


def callback_method(text, signature):
    """Require a single exact callback and retain its complete method body."""
    marker = '.method ' + signature + '\n'
    if text.count(marker) != 1:
        raise AssertionError('Callback signature changed: ' + signature)
    start = text.index(marker)
    end = text.index('.end method', start) + len('.end method')
    return text[start:end]


class GnssInputGuardTests(unittest.TestCase):
    def test_unknown_input_is_rejected_before_any_tool_or_workspace_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'services.jar'
            source.write_bytes(b'unverified framework')
            destination = root / 'never-created.jar'
            work = root / 'untouched-workspace'
            with patch.object(patch_gnss, 'ROOT', work), \
                    patch.object(patch_gnss.urllib.request, 'urlretrieve') as download, \
                    patch.object(patch_gnss.subprocess, 'run') as command:
                with self.assertRaisesRegex(RuntimeError, 'Unsupported services.jar'):
                    patch_gnss.patch(source, destination)
                download.assert_not_called()
                command.assert_not_called()
            self.assertFalse(destination.exists())
            self.assertFalse(work.exists())
            self.assertEqual(source.read_bytes(), b'unverified framework')


class Gnss18ArtifactTests(unittest.TestCase):
    def test_only_callbacks_and_runnable_adapters_change_in_the_real_framework(self):
        source_value = os.environ.get('HYPEROS_AVD_GNSS_TEST_SOURCE')
        output_value = os.environ.get('HYPEROS_AVD_GNSS_TEST_PATCHED')
        if not source_value or not output_value:
            self.skipTest('Original and patched 4.0.18 GNSS artifacts were not supplied.')
        source, output = Path(source_value), Path(output_value)
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), patch_gnss.OS4_18_SHA256)
        cache = REPO / 'tools/smali'
        for name, (_, expected) in patch_gnss.ARTIFACTS.items():
            self.assertEqual(hashlib.sha256((cache / (name + '.jar')).read_bytes()).hexdigest(), expected)
        with zipfile.ZipFile(source) as original, zipfile.ZipFile(output) as rewritten:
            self.assertEqual(original.namelist(), rewritten.namelist())
            self.assertNotEqual(original.read('classes2.dex'), rewritten.read('classes2.dex'))
            for entry in original.namelist():
                if entry != 'classes2.dex':
                    self.assertEqual(original.read(entry), rewritten.read(entry), entry)
        command = [patch_gnss.java(), '-cp', str(cache / '*'),
                   'org.jf.baksmali.Main', 'disassemble', '-j', '2']
        native_class = 'Lcom/android/server/location/gnss/hal/GnssNative;'
        native_path = Path('com/android/server/location/gnss/hal/GnssNative.smali')
        with tempfile.TemporaryDirectory(prefix='gnss-18-dex-audit-') as temporary:
            folder = Path(temporary)
            before, after = folder / 'before', folder / 'after'
            subprocess.run(command + ['--classes', native_class, str(source) + '/classes2.dex',
                                      '-o', str(before)], check=True, capture_output=True, timeout=120)
            original_native = (before / native_path).read_text()
            adapters = {}
            for signature in CALLBACKS:
                method = callback_method(original_native, signature)
                self.assertEqual(method.count(BINDER_CALL), 1)
                names = re.findall(r'new-instance v0, Lcom/android/server/location/gnss/hal/([^;]+);', method)
                self.assertEqual(len(names), 1)
                adapters[signature] = names[0]
            adapter_classes = ','.join('Lcom/android/server/location/gnss/hal/' + name + ';'
                                       for name in adapters.values())
            subprocess.run(command + ['--classes', adapter_classes, str(source) + '/classes2.dex',
                                      '-o', str(before)], check=True, capture_output=True, timeout=120)
            subprocess.run(command + ['--classes', native_class + ',' + adapter_classes,
                                      str(output) + '/classes2.dex', '-o', str(after)],
                           check=True, capture_output=True, timeout=120)
            patched_native = (after / native_path).read_text()
            original_rest, patched_rest = original_native, patched_native
            for signature, parameter_words in CALLBACKS.items():
                old = callback_method(original_native, signature)
                new = callback_method(patched_native, signature)
                self.assertNotIn(BINDER_CALL, new)
                self.assertEqual(new.count('Lcom/android/server/FgThread;->getHandler()'), 1)
                self.assertEqual(new.count('Landroid/os/Handler;->post(Ljava/lang/Runnable;)Z'), 1)
                registers = int(re.search(r'\.registers (\d+)', new).group(1))
                self.assertGreaterEqual(registers - parameter_words, 2)
                # Constructor arguments still carry the same callback payload.
                constructor = re.search(r'invoke-direct(?:/range)?[^\n]+;-><init>[^\n]+', old).group()
                self.assertIn(constructor, new)
                original_rest = original_rest.replace(old, '', 1)
                patched_rest = patched_rest.replace(new, '', 1)
                adapter_path = native_path.parent / (adapters[signature] + '.smali')
                old_adapter = (before / adapter_path).read_text()
                new_adapter = (after / adapter_path).read_text()
                self.assertEqual(old_adapter.count('.implements Lcom/android/internal/util/FunctionalUtils$ThrowingRunnable;'), 1)
                self.assertNotIn('.implements Ljava/lang/Runnable;', old_adapter)
                self.assertEqual(new_adapter.count('.implements Ljava/lang/Runnable;'), 1)
                run_method = re.findall(r'\.method public final run\(\)V\n.*?\.end method',
                                        new_adapter, re.DOTALL)
                self.assertEqual(len(run_method), 1)
                self.assertIn('invoke-static {p0}, Landroid/os/Binder;->withCleanCallingIdentity', run_method[0])
                self.assertIn('return-void', run_method[0])
                restored = new_adapter.replace('.implements Ljava/lang/Runnable;\n', '', 1)
                restored = restored.replace(run_method[0], '', 1).rstrip()
                self.assertEqual([line for line in restored.splitlines() if line.strip()],
                                 [line for line in old_adapter.splitlines() if line.strip()])
            self.assertEqual([line for line in patched_rest.splitlines() if line.strip()],
                             [line for line in original_rest.splitlines() if line.strip()])


if __name__ == '__main__':
    unittest.main()
