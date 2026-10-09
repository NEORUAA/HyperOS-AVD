"""Exercise the Android shell runtime without a guest or root mount mutations."""
import hashlib
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
import zipfile

REPO = Path(__file__).resolve().parents[1]
MODULE = REPO / 'modules/native-compat'


def digest(data):
    return hashlib.sha256(data).hexdigest()


BUSYBOX = r'''#!/usr/bin/env python3
import hashlib, os, pathlib, stat, subprocess, sys
args = sys.argv[1:]
command, args = args[0], args[1:]
if command == 'nsenter':
    args = args[args.index('--') + 1:]
    if args[0] == '/system/bin/ls':
        print('u:object_r:system_lib_file:s0 ' + args[-1]); sys.exit(0)
    os.execvp(args[0], args)
if command == 'sha256sum':
    if args and args[0] == '-c':
        for line in pathlib.Path(args[1]).read_text().splitlines():
            expected, name = line.split(None, 1)
            if hashlib.sha256(pathlib.Path(name.strip().lstrip('*')).read_bytes()).hexdigest() != expected:
                sys.exit(1)
        sys.exit(0)
    try:
        for name in args:
            print(hashlib.sha256(pathlib.Path(name).read_bytes()).hexdigest() + '  ' + name)
    except OSError:
        sys.exit(1)
    sys.exit(0)
if command == 'stat' and args[0] == '-c':
    try:
        value = pathlib.Path(args[2]).stat()
        form = args[1]
        print(form.replace('%a', format(stat.S_IMODE(value.st_mode), 'o'))
              .replace('%s', str(value.st_size)).replace('%i', str(value.st_ino))
              .replace('%Y', str(int(value.st_mtime))).replace('%d', str(value.st_dev)))
    except OSError:
        sys.exit(1)
    sys.exit(0)
os.execvp(command, [command] + args)
'''


class NativeModuleRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.module = self.root / 'module'
        self.module.mkdir()
        for name in ('cache', 'state', 'sites'):
            (self.module / name).mkdir()
        for source in MODULE.iterdir():
            if source.is_file():
                shutil.copy2(source, self.module / source.name)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.bb = self.bin / 'busybox'
        self.bb.write_text(BUSYBOX)
        self.bb.chmod(0o755)
        for name, body in {'chcon': 'exit 0', 'pidof': 'exit 1', 'id': 'echo 0',
                           'getprop': 'case "$1" in ro.boot.hardware) echo ranchu;; '
                           'ro.mi.os.version.incremental) echo OS4.9.999.0.TEST;; '
                           'sys.boot_completed) echo 1;; esac'}.items():
            file = self.bin / name
            file.write_text('#!/bin/sh\n' + body + '\n')
            file.chmod(0o755)
        self.proc = self.root / 'proc'
        (self.proc / '1').mkdir(parents=True)
        (self.proc / '1/mountinfo').write_text('')
        (self.proc / 'sys/kernel/random').mkdir(parents=True)
        (self.proc / 'sys/kernel/random/boot_id').write_text('test-boot\n')
        self.packages = self.root / 'packages.xml'
        self.packages.write_text('<packages/>')
        self.target = self.root / 'libtest.so'
        self.original = b'header-original-instructions-and-tail'
        self.target.write_bytes(self.original)
        self.target.chmod(0o644)
        self.after = self.original.replace(b'original', b'patched!')
        self.profile('test', 'fixture', self.original, self.after, [(7, b'original', b'patched!')])
        (self.module / 'targets.tsv').write_text(f'early|system|fixture|{self.target}||libtest.so\n')
        self.environment = {**os.environ, 'PATH': str(self.bin) + os.pathsep + os.environ['PATH'],
                            'HYPEROS_NATIVE_BB': str(self.bb), 'HYPEROS_NATIVE_PROC': str(self.proc),
                            'HYPEROS_NATIVE_PACKAGES_XML': str(self.packages)}

    def profile(self, name, feature, before, after, sites, legacy=(), append=False):
        rows = []
        directory = self.module / 'sites' / name
        directory.mkdir(exist_ok=True)
        for index, (offset, original, patched) in enumerate(sites):
            for suffix, data in (('before', original), ('after', patched)):
                (directory / f'{index:03d}.{suffix}').write_bytes(data)
            rows.append(f'{offset}|{len(original)}|sites/{name}/{index:03d}.before|sites/{name}/{index:03d}.after\n')
        (self.module / 'sites' / (name + '.tsv')).write_text(''.join(rows))
        row = '|'.join((digest(before), digest(after), ','.join(map(digest, legacy)), feature, name)) + '\n'
        path = self.module / 'profiles.tsv'
        path.write_text((path.read_text() if append and path.exists() else '') + row)

    def shell(self, body, *, check=True, overrides=''):
        script = 'MODDIR=' + shlex.quote(str(self.module)) + '\n'
        script += '. "$MODDIR/runtime.sh"\n' + overrides + '\n' + body
        return subprocess.run(['sh', '-c', script], env=self.environment, text=True,
                              capture_output=True, check=check)

    def safe_paths(self):
        return 'native_path() { case "$1" in ' + shlex.quote(str(self.root)) + '/*) return 0;; *) return 1;; esac; }'

    def app(self, body=None):
        apk = self.root / 'app.apk'
        with zipfile.ZipFile(apk, 'w') as archive:
            archive.writestr('lib/arm64-v8a/libtest.so', self.original if body is None else body)
        overrides = self.safe_paths() + '\n'
        overrides += 'pm() { printf "package:%s\\n" ' + shlex.quote(str(apk)) + '; }\n'
        overrides += 'dumpsys() { printf "nativeLibraryDir=%s\\n" ' + shlex.quote(str(self.root)) + '; }\n'
        return apk, overrides

    def customize(self, registry, *, check=True):
        self.checksums()
        content = (MODULE / 'customize.sh').read_text().replace(
            '/data/adb/ksu/bin/busybox', str(self.bb)).replace('/data/adb/modules', str(registry))
        helpers = ('ARCH=arm64\nMODPATH=' + shlex.quote(str(self.module)) + '\n'
                   'abort() { echo "$*" >&2; exit 1; }\nui_print() { :; }\n'
                   'set_perm_recursive() { :; }\nset_perm() { :; }\n')
        return subprocess.run(['sh', '-c', helpers + content], env=self.environment,
                              text=True, capture_output=True, check=check)

    def checksums(self):
        files = sorted(path for path in self.module.rglob('*') if path.is_file()
                       and 'cache' not in path.relative_to(self.module).parts
                       and 'state' not in path.relative_to(self.module).parts
                       and path.name != 'SHA256SUMS')
        (self.module / 'SHA256SUMS').write_text(''.join(
            digest(path.read_bytes()) + '  ' + str(path.relative_to(self.module)) + '\n'
            for path in files))

    def test_shell_syntax_and_standard_module_entrypoints(self):
        for filename in ('runtime.sh', 'post-fs-data.sh', 'service.sh', 'customize.sh'):
            with self.subTest(filename=filename):
                subprocess.run(['sh', '-n', str(MODULE / filename)], check=True, capture_output=True)
        self.assertIn('SKIPMOUNT=true', (MODULE / 'customize.sh').read_text())
        self.assertIn('id=hyperos_avd_native_compat\n', (MODULE / 'module.prop').read_text())

    def test_os4_hardware_guard_has_no_avd_or_exact_firmware_gate(self):
        self.shell('native_guard')
        self.shell('getprop() { case "$1" in ro.boot.hardware) echo ranchu;; *) echo 4.1.999;; esac; }; native_guard')
        self.shell('getprop() { echo Pixel; }; ! native_guard')
        self.shell('getprop() { case "$1" in ro.boot.hardware) echo ranchu;; *) echo OS3.1;; esac; }; ! native_guard')
        runtime = (MODULE / 'runtime.sh').read_text()
        self.assertNotIn('ro.boot.qemu.avd_name', runtime)
        self.assertNotIn('OS4.0.', runtime)

    def test_customizer_preserves_known_core_feature_choices(self):
        registry = self.root / 'modules'
        prior = registry / 'hyperos_avd_native_compat'
        prior.mkdir(parents=True)
        (prior / 'features.disabled').write_text('fixture\n')
        self.customize(registry)
        self.assertEqual((self.module / 'features.disabled').read_text(), 'fixture\n')
        self.assertEqual((prior / 'features.disabled').read_text(), 'fixture\n')

    def test_customizer_inherits_legacy_choices_without_changing_flags(self):
        registry = self.root / 'modules'
        for identifier, flag in (('hyperos_avd_flutter_render', 'disable'),
                                 ('hyperos_avd_assistant_mgl', 'remove')):
            folder = registry / identifier
            folder.mkdir(parents=True)
            (folder / flag).write_text('preserve')
        self.customize(registry)
        self.assertEqual((self.module / 'features.disabled').read_text(), 'assistant\nflutter\n')
        self.assertEqual((registry / 'hyperos_avd_flutter_render/disable').read_text(), 'preserve')
        self.assertEqual((registry / 'hyperos_avd_assistant_mgl/remove').read_text(), 'preserve')

    def test_customizer_empty_core_choices_are_authoritative_over_legacy_flags(self):
        registry = self.root / 'modules'
        prior = registry / 'hyperos_avd_native_compat'
        prior.mkdir(parents=True)
        (prior / 'features.disabled').write_text('')
        legacy = registry / 'hyperos_avd_flutter_render'
        legacy.mkdir()
        (legacy / 'disable').write_text('preserve')
        self.customize(registry)
        self.assertEqual((self.module / 'features.disabled').read_text(), '')
        self.assertEqual((legacy / 'disable').read_text(), 'preserve')

    def test_customizer_rejects_unknown_or_aliased_prior_choices(self):
        registry = self.root / 'modules'
        prior = registry / 'hyperos_avd_native_compat'
        prior.mkdir(parents=True)
        choices = prior / 'features.disabled'
        choices.write_text('unknown-feature\n')
        self.assertNotEqual(self.customize(registry, check=False).returncode, 0)
        self.assertEqual(choices.read_text(), 'unknown-feature\n')
        choices.unlink()
        unrelated = self.root / 'unrelated-choices'
        unrelated.write_text('fixture\n')
        choices.symlink_to(unrelated)
        self.assertNotEqual(self.customize(registry, check=False).returncode, 0)
        self.assertEqual(unrelated.read_text(), 'fixture\n')

    def test_customizer_prefers_surviving_staged_choices_over_active(self):
        registry = self.root / 'modules'
        prior = registry / 'hyperos_avd_native_compat'
        prior.mkdir(parents=True)
        (prior / 'features.disabled').write_text('fixture\n')
        # An empty pending choice file means the user enabled every feature.
        (self.module / 'features.disabled').write_text('')
        self.customize(registry)
        self.assertEqual((self.module / 'features.disabled').read_text(), '')
        self.assertEqual((prior / 'features.disabled').read_text(), 'fixture\n')

    def test_customizer_prefers_verified_separate_pending_choices(self):
        registry = self.root / 'modules'
        prior = registry / 'hyperos_avd_native_compat'
        prior.mkdir(parents=True)
        (prior / 'features.disabled').write_text('fixture\n')
        pending = Path(str(registry) + '_update') / 'hyperos_avd_native_compat'
        pending.mkdir(parents=True)
        properties = pending / 'module.prop'
        properties.write_text('id=hyperos_avd_native_compat\nauthor=HyperOS-AVD\n')
        (pending / 'SHA256SUMS').write_text(digest(properties.read_bytes()) + '  module.prop\n')
        (pending / 'features.disabled').write_text('')
        self.customize(registry)
        self.assertEqual((self.module / 'features.disabled').read_text(), '')
        self.assertEqual((prior / 'features.disabled').read_text(), 'fixture\n')

    def test_asset_checksums_precede_target_changes(self):
        self.checksums()
        self.shell('native_assets')
        with (self.module / 'profiles.tsv').open('a') as output:
            output.write('unexpected\n')
        self.shell('! native_assets')
        self.assertEqual(self.target.read_bytes(), self.original)

    def test_patch_copy_checks_exact_bytes_and_final_hash(self):
        result = self.shell('native_prepare ' + shlex.quote(str(self.target)) + ' fixture ' + digest(self.original))
        payload = Path(result.stdout.strip())
        self.assertEqual(payload.read_bytes(), self.after)
        self.assertEqual(self.target.read_bytes(), self.original)
        self.assertEqual(payload.stat().st_mode & 0o777, 0o644)
        self.assertFalse(list((self.module / 'cache').glob('*.next.*')))

    def test_legacy_input_with_preexisting_sites_is_supported(self):
        old = self.original.replace(b'original', b'patched!')
        final = old.replace(b'tail', b'done')
        self.profile('legacy', 'fixture', self.original, final,
                     [(7, b'original', b'patched!'), (len(old) - 4, b'tail', b'done')], legacy=(old,))
        self.target.write_bytes(old)
        result = self.shell('native_prepare ' + shlex.quote(str(self.target)) + ' fixture ' + digest(old))
        self.assertEqual(Path(result.stdout.strip()).read_bytes(), final)

    def test_bad_site_or_expected_output_preserves_input(self):
        (self.module / 'sites/test/000.before').write_bytes(b'wrong!!!')
        result = self.shell('native_prepare ' + shlex.quote(str(self.target)) + ' fixture ' + digest(self.original), check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.target.read_bytes(), self.original)
        self.assertFalse(list((self.module / 'cache').glob('candidate.next.*')))

    def test_private_native_path_allowlist(self):
        for path in ('/data/app/~~abc/com.miui.home-def/lib/arm64/libtest.so',
                     '/data/app-lib/MIUIWeather/arm64/libtest.so'):
            self.shell('native_private_target ' + shlex.quote(path))
        for path in ('/product/app/Weather/lib/arm64/libtest.so', '/data/app/libtest.so',
                     '/data/app/../../system/lib/arm64/libtest.so', '/data/app/x/lib/arm64/a/b.so'):
            self.shell('! native_private_target ' + shlex.quote(path))

    def package_dump(self, content):
        path = self.root / 'package-dump'
        path.write_text(content)
        return 'dumpsys() { cat ' + shlex.quote(str(path)) + '; }'

    def test_android17_legacy_native_root_uses_first_active_package_block(self):
        dump = textwrap.dedent('''
            Packages:
              Package [com.miui.home] (active):
                codePath=/data/app/~~current/com.miui.home-code
                legacyNativeLibraryDir=/data/app/~~current/com.miui.home-code/lib
                primaryCpuAbi=arm64-v8a
                secondaryCpuAbi=null
            Hidden system packages:
              Package [com.miui.home] (factory):
                legacyNativeLibraryDir=/product/priv-app/MiuiHome/lib
                primaryCpuAbi=arm64-v8a
        ''')
        result = self.shell('native_app_target com.miui.home libflutter.so', overrides=self.package_dump(dump))
        self.assertEqual(result.stdout.strip(), '/data/app/~~current/com.miui.home-code/lib/arm64/libflutter.so')

    def test_android17_factory_and_pad_legacy_native_roots(self):
        for directory in ('/product/app/MIUIWeather/lib', '/product/priv-app/VoiceAssist/lib',
                          '/data/app-lib/MIUIWeather'):
            dump = f'Package [com.miui.weather2] (active):\n  legacyNativeLibraryDir={directory}\n  primaryCpuAbi=arm64-v8a\n'
            with self.subTest(directory=directory):
                result = self.shell('native_app_target com.miui.weather2 libflutter.so', overrides=self.package_dump(dump))
                self.assertEqual(result.stdout.strip(), directory + '/arm64/libflutter.so')

    def test_explicit_native_directory_is_not_suffixed_twice(self):
        dump = ('Package [com.miui.home] (active):\n'
                '  nativeLibraryDir=/data/app/example/lib/arm64\n'
                '  legacyNativeLibraryDir=/data/app/example/lib\n  primaryCpuAbi=arm64-v8a\n')
        result = self.shell('native_app_target com.miui.home libflutter.so', overrides=self.package_dump(dump))
        self.assertEqual(result.stdout.strip(), '/data/app/example/lib/arm64/libflutter.so')

    def test_legacy_native_root_rejects_missing_null_and_incompatible_abi(self):
        for abi in ('', 'null', 'armeabi-v7a', 'x86_64'):
            dump = 'Package [com.miui.home] (active):\n  legacyNativeLibraryDir=/data/app/example/lib\n'
            if abi:
                dump += f'  primaryCpuAbi={abi}\n'
            with self.subTest(abi=abi):
                result = self.shell('native_app_target com.miui.home libflutter.so',
                                    overrides=self.package_dump(dump), check=False)
                self.assertNotEqual(result.returncode, 0)

    def test_ambiguous_native_fields_and_null_active_root_are_not_borrowed(self):
        dumps = [
            ('  nativeLibraryDir=/data/app/one/lib/arm64\n'
             '  nativeLibraryDir=/data/app/two/lib/arm64\n'),
            ('  nativeLibraryDir=/data/app/one/lib/arm64\n'
             '  legacyNativeLibraryDir=/data/app/two/lib\n  primaryCpuAbi=arm64-v8a\n'),
            ('  legacyNativeLibraryDir=null\n  primaryCpuAbi=arm64-v8a\n'
             'Package [com.miui.home] (factory):\n'
             '  legacyNativeLibraryDir=/product/priv-app/MiuiHome/lib\n  primaryCpuAbi=arm64-v8a\n'),
            ('  nativeLibraryDir=null\n  legacyNativeLibraryDir=/data/app/example/lib\n  primaryCpuAbi=arm64-v8a\n'),
        ]
        for fields in dumps:
            result = self.shell('native_app_target com.miui.home libflutter.so',
                                overrides=self.package_dump('Package [com.miui.home] (active):\n' + fields), check=False)
            self.assertNotEqual(result.returncode, 0)

    def test_app_requires_matching_signed_native_code(self):
        _, overrides = self.app(b'different embedded native code')
        self.shell('native_apply_target app fixture "" com.miui.home libtest.so late', overrides=overrides)
        self.assertIn('|skipped|apk-native-code-mismatch\n', (self.module / 'state/status.tsv').read_text())
        self.assertEqual(self.target.read_bytes(), self.original)

    def test_unzip_success_with_empty_output_skips_unavailable_apk_entry(self):
        apk, overrides = self.app()
        original_apk = apk.read_bytes()
        self.target.unlink()
        overrides += textwrap.dedent('''
            native_ns() {
                pid=$1; shift
                [ "$2" != unzip ] || return 0
                "$BB" nsenter -t "$pid" -m -- "$@"
            }
            native_restore_missing() { echo forbidden-publication; return 1; }
        ''')
        result = self.shell('native_apply_target app fixture "" com.miui.home libtest.so late', overrides=overrides)
        self.assertNotIn('forbidden-publication', result.stdout)
        self.assertIn('|skipped|apk-native-entry-unavailable\n', (self.module / 'state/status.tsv').read_text())
        self.assertFalse(self.target.exists())
        self.assertFalse(list((self.module / 'cache').glob('apk-entry.next.*')))
        self.assertEqual(apk.read_bytes(), original_apk)

    def test_missing_private_native_restores_original_without_changing_apk(self):
        apk, overrides = self.app()
        original_apk = apk.read_bytes()
        self.target.unlink()
        overrides += '\nnative_private_target() { native_path "$1"; }'
        overrides += '\nnative_mount() { echo "$2"; return 0; }'
        result = self.shell('NATIVE_CATALOG_HASH=test; native_apply_target app fixture "" com.miui.home libtest.so late', overrides=overrides)
        self.assertEqual(self.target.read_bytes(), self.original)
        self.assertEqual(apk.read_bytes(), original_apk)
        self.assertEqual(Path(result.stdout.strip()).read_bytes(), self.after)
        self.assertFalse(list(self.root.glob('*.hyperos-native-original.*')))

    def test_missing_private_native_never_overwrites_raced_file(self):
        apk, overrides = self.app()
        original_apk = apk.read_bytes()
        self.target.unlink()
        entry = self.module / 'cache/original-entry'
        entry.write_bytes(self.original)
        overrides += '\nnative_private_target() { native_path "$1"; }'
        # A competing extraction commits just after stable package checks.
        overrides += '\nnative_active_apk() { printf raced > ' + shlex.quote(str(self.target)) + '; printf "%s\\n" ' + shlex.quote(str(apk)) + '; }'
        command = 'native_restore_missing ' + ' '.join(shlex.quote(str(value)) for value in
                    (self.target, entry, digest(self.original), apk, digest(original_apk),
                     'com.miui.home', 'libtest.so', 'fixture'))
        result = self.shell(command, overrides=overrides, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.target.read_bytes(), b'raced')
        self.assertEqual(apk.read_bytes(), original_apk)

    def test_missing_private_native_cleans_temporary_on_label_failure(self):
        apk, overrides = self.app()
        original_apk = apk.read_bytes()
        self.target.unlink()
        entry = self.module / 'cache/original-entry'
        entry.write_bytes(self.original)
        overrides += '\nnative_private_target() { native_path "$1"; }'
        (self.bin / 'chcon').write_text('#!/bin/sh\nexit 1\n')
        command = 'native_restore_missing ' + ' '.join(shlex.quote(str(value)) for value in
                    (self.target, entry, digest(self.original), apk, digest(original_apk),
                     'com.miui.home', 'libtest.so', 'fixture'))
        result = self.shell(command, overrides=overrides, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.target.exists())
        self.assertFalse(list(self.root.glob('*.hyperos-native-original.*')))
        self.assertEqual(apk.read_bytes(), original_apk)

    def test_private_restore_rejects_alias_and_missing_parent(self):
        apk, overrides = self.app()
        entry = self.module / 'cache/original-entry'
        entry.write_bytes(self.original)
        self.target.unlink()
        unrelated = self.root / 'unrelated'
        unrelated.write_bytes(b'preserve me')
        self.target.symlink_to(unrelated)
        overrides += '\nnative_private_target() { native_path "$1"; }'
        command = 'native_restore_missing ' + ' '.join(shlex.quote(str(value)) for value in
                    (self.target, entry, digest(self.original), apk, digest(apk.read_bytes()),
                     'com.miui.home', 'libtest.so', 'fixture'))
        result = self.shell(command, overrides=overrides, check=False)
        self.assertEqual(result.returncode, 3)
        self.assertEqual(unrelated.read_bytes(), b'preserve me')
        self.target.unlink()
        missing = self.root / 'not-published/libtest.so'
        result = self.shell(command.replace(shlex.quote(str(self.target)), shlex.quote(str(missing))),
                            overrides=overrides, check=False)
        self.assertEqual(result.returncode, 3)
        self.assertFalse(missing.parent.exists())

    def test_stable_app_receipt_does_not_restart_or_remount_on_unrelated_update(self):
        _, overrides = self.app()
        marker = self.root / 'mounts'
        overrides += '\nnative_mount() { cp "$2" "$1"; echo mounted >> ' + shlex.quote(str(marker)) + '; }'
        self.shell('NATIVE_CATALOG_HASH=test; native_apply_target app fixture "" com.miui.home libtest.so late; '
                   'native_apply_target app fixture "" com.miui.home libtest.so late', overrides=overrides)
        self.assertEqual(marker.read_text(), 'mounted\n')

    def test_package_replacement_during_patch_refuses_bind(self):
        apk, overrides = self.app()
        counter = self.root / 'pm-count'
        counter.write_text('0')
        overrides += '\nnative_active_apk() { count=$(cat ' + shlex.quote(str(counter)) + '); count=$((count+1)); '
        overrides += 'echo "$count" > ' + shlex.quote(str(counter)) + '; '
        overrides += 'if [ "$count" -gt 1 ]; then echo /data/app/replaced/base.apk; else printf "%s\\n" ' + shlex.quote(str(apk)) + '; fi; }'
        overrides += '\nnative_mount() { echo forbidden; return 0; }'
        result = self.shell('native_apply_target app fixture "" com.miui.home libtest.so late', overrides=overrides)
        self.assertNotIn('forbidden', result.stdout)
        self.assertIn('|skipped|package-changing\n', (self.module / 'state/status.tsv').read_text())
        self.assertEqual(self.target.read_bytes(), self.original)

    def test_unknown_code_skips_only_its_feature(self):
        self.target.write_bytes(b'unknown')
        result = self.shell('native_apply_target system fixture ' + shlex.quote(str(self.target)) + ' "" libtest.so early',
                            overrides=self.safe_paths())
        self.assertEqual(result.returncode, 0)
        self.assertIn('|skipped|unsupported-elf\n', (self.module / 'state/status.tsv').read_text())
        self.assertEqual(self.target.read_bytes(), b'unknown')

    def test_composer_then_rear_terminal_output_is_not_regressed(self):
        final = self.after.replace(b'tail', b'done')
        self.profile('rear', 'rear', self.after, final,
                     [(len(self.after) - 4, b'tail', b'done')], append=True)
        with (self.module / 'targets.tsv').open('a') as output:
            output.write(f'early|system|rear|{self.target}||libtest.so\n')
        self.target.write_bytes(final)
        self.shell('native_apply_target system fixture ' + shlex.quote(str(self.target)) + ' "" libtest.so early',
                   overrides=self.safe_paths())
        self.assertIn('|ready|already-patched\n', (self.module / 'state/status.tsv').read_text())
        self.assertEqual(self.target.read_bytes(), final)

    def test_only_module_cache_mounts_are_owned(self):
        info = self.proc / '1/mountinfo'
        info.write_text(f'1 0 1:1 {self.module}/cache/good.so {self.target} rw - ext4 disk rw\n')
        self.shell('native_owns_mount 1 ' + shlex.quote(str(self.target)))
        info.write_text(f'1 0 1:1 /adb/modules/another/cache/good.so {self.target} rw - ext4 disk rw\n')
        self.shell('! native_owns_mount 1 ' + shlex.quote(str(self.target)))
        info.write_text(f'1 0 1:1 {self.module}/cache/good.so {self.target} rw - ext4 disk rw\n'
                        f'2 1 1:1 /adb/modules/another/cache/good.so {self.target} rw - ext4 disk rw\n')
        self.shell('! native_owns_mount 1 ' + shlex.quote(str(self.target)))

    def test_mount_failure_rolls_back_new_namespaces_only(self):
        for pid in ('2', '3'):
            (self.proc / pid).mkdir()
        states = self.root / 'namespace-state'
        states.mkdir()
        for pid, checksum in (('1', digest(self.original)), ('2', digest(self.original)),
                              ('3', digest(self.after))):
            (states / pid).write_text(checksum + '\n')
        marker = self.root / 'mount-calls'
        overrides = self.safe_paths() + '\nSTATE=' + shlex.quote(str(states)) + '\n'
        overrides += 'MARKER=' + shlex.quote(str(marker)) + '\n'
        overrides += 'native_pids() { printf "1\\n2\\n3\\n"; }\n'
        overrides += 'native_ns_hash() { cat "$STATE/$1"; }\n'
        overrides += 'native_owns_mount() { [ "$(cat "$STATE/$1")" = ' + digest(self.after) + ' ]; }\n'
        overrides += textwrap.dedent('''
            native_ns() {
                pid=$1; shift 2
                case "$*" in
                    'mount -o bind '*)
                        echo "bind:$pid" >> "$MARKER"
                        [ "$pid" != 2 ] || return 1
                        printf '%s\\n' FINAL_HASH > "$STATE/$pid" ;;
                    'mount -o remount,bind,ro '*) echo "readonly:$pid" >> "$MARKER" ;;
                    'umount '*) echo "rollback:$pid" >> "$MARKER"; printf '%s\\n' INITIAL_HASH > "$STATE/$pid" ;;
                    *) return 1 ;;
                esac
            }
        ''').replace('FINAL_HASH', digest(self.after)).replace('INITIAL_HASH', digest(self.original))
        command = 'native_mount ' + ' '.join(map(shlex.quote, (str(self.target), str(self.target),
                   '', digest(self.original), digest(self.after))))
        result = self.shell(command, overrides=overrides, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(marker.read_text(), 'bind:1\nreadonly:1\nbind:2\nrollback:1\n')
        self.assertEqual((states / '1').read_text().strip(), digest(self.original))
        self.assertEqual((states / '3').read_text().strip(), digest(self.after))

    def test_disable_and_remove_flags_and_feature_choice(self):
        (self.module / 'disable').touch()
        self.shell('native_blocked')
        (self.module / 'disable').unlink()
        (self.module / 'remove').touch()
        self.shell('native_blocked')
        (self.module / 'remove').unlink()
        (self.module / 'features.disabled').write_text('fixture\n')
        self.shell('native_apply_target system fixture ' + shlex.quote(str(self.target)) + ' "" libtest.so early',
                   overrides=self.safe_paths())
        self.assertIn('|disabled|user-choice\n', (self.module / 'state/status.tsv').read_text())
        self.assertEqual(self.target.read_bytes(), self.original)

    def test_status_updates_are_atomic_and_replace_the_same_key(self):
        self.shell('native_status fixture target skipped unknown; native_status fixture target ready verified')
        self.assertEqual((self.module / 'state/status.tsv').read_text(), 'fixture|target|ready|verified\n')
        self.assertFalse(list((self.module / 'state').glob('*.next.*')))

    def test_late_service_never_mounts_early_preloaded_targets(self):
        overrides = self.safe_paths() + '\nnative_mount() { echo forbidden-mount; return 1; }'
        result = self.shell('native_reconcile late', overrides=overrides)
        self.assertNotIn('forbidden-mount', result.stdout)
        self.assertIn('|pending|reboot-required\n', (self.module / 'state/status.tsv').read_text())

    def test_failed_owned_detach_never_rebinds_from_a_masked_source(self):
        overrides = self.safe_paths() + '\nnative_detach() { return 1; }\nnative_apply_target() { echo forbidden-rebind; }'
        result = self.shell('native_reconcile early', overrides=overrides)
        self.assertNotIn('forbidden-rebind', result.stdout)
        self.assertIn('|failed|owned-detach\n', (self.module / 'state/status.tsv').read_text())

    def test_uninstalled_app_receipt_is_retired_without_removing_original(self):
        (self.module / 'targets.tsv').write_text('late|app|fixture||com.miui.home|libtest.so\n')
        receipt = self.module / 'state/fixture-com.miui.home-libtest.so.receipt'
        receipt.write_text(f'catalog|/data/app/example/base.apk|hash|{self.target}|{digest(self.after)}\n')
        overrides = self.safe_paths() + '\nnative_resolve_target() { return 1; }\n'
        overrides += 'native_detach() { echo "detached:$1"; }\n'
        overrides += 'pm() { return 1; }; dumpsys() { return 1; }'
        result = self.shell('native_reconcile late', overrides=overrides)
        self.assertIn(f'detached:{self.target}\n', result.stdout)
        self.assertFalse(receipt.exists())
        self.assertEqual(self.target.read_bytes(), self.original)
        self.assertIn('|skipped|retired-native-path\n', (self.module / 'state/status.tsv').read_text())

    def test_cleanup_uses_old_app_receipt_when_package_is_unavailable(self):
        (self.module / 'targets.tsv').write_text('late|app|fixture||com.miui.home|libtest.so\n')
        receipt = self.module / 'state/fixture-com.miui.home-libtest.so.receipt'
        receipt.write_text(f'catalog|/data/app/example/base.apk|hash|{self.target}|{digest(self.after)}\n')
        overrides = self.safe_paths() + '\nnative_resolve_target() { return 1; }\n'
        overrides += 'native_detach() { echo "detached:$1"; }'
        result = self.shell('native_cleanup', overrides=overrides)
        self.assertEqual(result.stdout, f'detached:{self.target}\n')
        self.assertTrue(receipt.exists())
        self.assertEqual(self.target.read_bytes(), self.original)
        self.assertIn('|disabled|restart-for-loaded-code\n', (self.module / 'state/status.tsv').read_text())

    def test_failed_retired_app_detach_blocks_rebinding(self):
        (self.module / 'targets.tsv').write_text('late|app|fixture||com.miui.home|libtest.so\n')
        receipt = self.module / 'state/fixture-com.miui.home-libtest.so.receipt'
        receipt.write_text(f'catalog|/data/app/example/base.apk|hash|{self.target}|{digest(self.after)}\n')
        overrides = self.safe_paths() + '\nnative_resolve_target() { return 1; }\n'
        overrides += 'native_detach() { return 1; }\n'
        overrides += 'native_apply_target() { echo forbidden-rebind; }'
        result = self.shell('native_reconcile late', overrides=overrides)
        self.assertNotIn('forbidden-rebind', result.stdout)
        self.assertTrue(receipt.exists())
        self.assertIn('|failed|prior-app-detach\n', (self.module / 'state/status.tsv').read_text())

    def test_loaded_old_inode_requires_reboot_instead_of_reporting_ready(self):
        self.target.write_bytes(self.after)
        (self.proc / '1/maps').write_text(f'1000-2000 r-xp 00000000 fe:38 1 {self.target}\n')
        self.shell('native_apply_target system fixture ' + shlex.quote(str(self.target)) + ' "" libtest.so early',
                   overrides=self.safe_paths())
        self.assertIn('|pending|reboot-required\n', (self.module / 'state/status.tsv').read_text())

    def test_loaded_same_inode_on_different_device_is_stale(self):
        maps = self.proc / '1/maps'
        overrides = 'native_ns() { echo "65080 123"; }'
        maps.write_text(f'1000-2000 r-xp 00000000 fe:39 123 {self.target}\n')
        self.shell('native_loaded_stale ' + shlex.quote(str(self.target)), overrides=overrides)
        maps.write_text(f'1000-2000 r-xp 00000000 fe:38 123 {self.target}\n')
        self.shell('! native_loaded_stale ' + shlex.quote(str(self.target)), overrides=overrides)

    def test_hwui_preload_policy_is_early_and_requires_verified_output(self):
        self.profile('hwui', 'hwui', self.original, self.after, [(7, b'original', b'patched!')])
        marker = self.root / 'property'
        overrides = 'getprop() { [ "$1" != ro.zygote.disable_gl_preload ] || cat ' + shlex.quote(str(marker)) + '; }\n'
        overrides += 'native_resetprop() { echo "$2" > ' + shlex.quote(str(marker)) + '; }'
        marker.write_text('1\n')
        self.shell('native_hwui_policy ' + shlex.quote(str(self.target)) + ' late', overrides=overrides)
        self.assertEqual(marker.read_text(), '1\n')
        self.shell('! native_hwui_policy ' + shlex.quote(str(self.target)) + ' early', overrides=overrides)
        self.assertEqual(marker.read_text(), '1\n')
        self.target.write_bytes(self.after)
        self.shell('native_hwui_policy ' + shlex.quote(str(self.target)) + ' early', overrides=overrides)
        self.assertEqual(marker.read_text(), '0\n')

    def test_stale_lock_after_reboot_is_recovered(self):
        lock = self.module / 'service.lock'
        lock.mkdir()
        (lock / 'pid').write_text('123\n')
        (lock / 'boot').write_text('prior-boot\n')
        self.shell('sleep() { :; }; native_service_lock "$MODDIR/service.lock"')
        self.assertEqual((lock / 'boot').read_text(), 'test-boot\n')
        self.assertNotEqual((lock / 'pid').read_text(), '123\n')

    def test_service_reconciles_only_on_debounced_tokens_and_stops_when_disabled(self):
        token = self.root / 'token'
        token.write_text('original\n')
        overrides = textwrap.dedent('''
            native_guard() { return 0; }
            native_assets() { return 0; }
            native_service_lock() { return 0; }
            native_reconcile() { echo "$1"; }
            native_cleanup() { echo cleanup; }
            native_package_token() { cat "$TOKEN"; }
            counter=0
            sleep() {
                counter=$((counter + 1))
                case "$counter" in 2) echo changed > "$TOKEN";; 4) touch "$MODDIR/disable";; esac
            }
        ''')
        result = self.shell('TOKEN=' + shlex.quote(str(token)) + '\nnative_service', overrides=overrides)
        self.assertEqual(result.stdout, 'late\nlate\ncleanup\n')

    def test_real_flutter_byte_sites_match_python_patcher_when_fixture_exists(self):
        path = os.environ.get('HYPEROS_AVD_FLUTTER18_ARTIFACT')
        if not path or not Path(path).is_file():
            self.skipTest('Optional verified proprietary Flutter fixture is unavailable.')
        sys.path.insert(0, str(REPO / 'scripts'))
        from patch_flutter import patch, profile
        original = Path(path).read_bytes()
        before, selected = profile(original)
        sites = [(offset, bytes.fromhex(old), bytes.fromhex(new)) for offset, old, new in selected['sites']]
        expected = patch(original)
        self.profile('flutter', 'flutter', original, expected, sites)
        self.target.write_bytes(original)
        result = self.shell('native_prepare ' + shlex.quote(str(self.target)) + ' flutter ' + before)
        self.assertEqual(Path(result.stdout.strip()).read_bytes(), expected)


if __name__ == '__main__':
    unittest.main()
