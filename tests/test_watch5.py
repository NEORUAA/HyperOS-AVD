"""Check the isolated watch firmware edits without starting any AVD."""
from pathlib import Path
import subprocess
import struct
import sys
import tempfile
import unittest
import json
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / 'scripts'))
from watch5 import (check_metadata, patch_ramdisk, properties, set_properties,
                    original_overlays, control)
from common import tool
from watch5_native import needed, needed32, system_closure


def native_elf(*dependencies):
    """Build a small ELF64 dependency table for namespace regression checks."""
    strings = b'\0'
    offsets = []
    for name in dependencies:
        offsets.append(len(strings))
        strings += name.encode() + b'\0'
    start = 64 + 2 * 56
    entries = [(5, start + (len(offsets) + 2) * 16)]
    entries.extend((1, n) for n in offsets)
    entries.append((0, 0))
    dynamic = b''.join(struct.pack('<qQ', *entry) for entry in entries)
    size = start + len(dynamic) + len(strings)
    header = bytearray(64)
    header[:6] = b'\x7fELF\x02\x01'
    struct.pack_into('<H', header, 18, 183)
    struct.pack_into('<Q', header, 32, 64)
    struct.pack_into('<HH', header, 54, 56, 2)
    load = struct.pack('<IIQQQQQQ', 1, 5, 0, 0, 0, size, size, 4096)
    dyn = struct.pack('<IIQQQQQQ', 2, 4, start, start, 0, len(dynamic), len(dynamic), 8)
    return bytes(header) + load + dyn + dynamic + strings


def archive(entries):
    output = bytearray()
    for index, (name, content) in enumerate(entries + [('TRAILER!!!', b'')]):
        fields = [index, 0o100644, 0, 0, 1, 0, len(content), 0, 0, 0, 0,
                  len(name.encode()) + 1, 0]
        output.extend(b'070701' + ''.join(f'{n:08x}' for n in fields).encode())
        output.extend(name.encode() + b'\0')
        output.extend(bytes(-len(output) % 4))
        output.extend(content)
        output.extend(bytes(-len(output) % 4))
    output.extend(bytes(-len(output) % 512))
    return bytes(output)


def entries(raw):
    cursor, result = 0, []
    while cursor < len(raw):
        if raw[cursor:cursor + 6] != b'070701':
            following = raw.find(b'070701', cursor)
            if following < 0 or any(raw[cursor:following]):
                break
            cursor = following
        header = raw[cursor:cursor + 110]
        size, namesize = int(header[54:62], 16), int(header[94:102], 16)
        name = raw[cursor + 110:cursor + 110 + namesize].rstrip(b'\0').decode()
        start = (cursor + 110 + namesize + 3) // 4 * 4
        result.append((name, header, raw[start:start + size]))
        cursor = (start + size + 3) // 4 * 4
    return result


class WatchEditsTest(unittest.TestCase):
    def test_controls_refuse_a_recycled_emulator_pid(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'local').mkdir()
            (root / 'local/watch-process.json').write_text(json.dumps({'pid': 1234}))
            config = {'name': 'HyperOS_Watch5_API_34', 'port': 5576, 'sdk': '/sdk'}
            process = subprocess.CompletedProcess([], 0, '/sdk/emulator/qemu/darwin-aarch64/'
                'qemu-system-aarch64 -avd Pixel_10_Pro -port 5554 ')
            with patch('common.ROOT', root), patch('common.runtime', return_value=config), \
                    patch('watch5.subprocess.run', return_value=process), patch('common.adb') as adb:
                with self.assertRaisesRegex(RuntimeError, 'owned Watch5'):
                    control('home')
                adb.assert_not_called()

    def test_controls_wait_for_guest_boot_completion(self):
        config = {'name': 'HyperOS_Watch5_API_34', 'port': 5576, 'sdk': '/sdk'}
        process = subprocess.CompletedProcess([], 0, b'0\n', b'')
        with patch('watch5.owned_runtime', return_value=(config, {'pid': 1234}, True)), \
                patch('common.adb', return_value=process) as adb:
            with self.assertRaisesRegex(RuntimeError, 'completed Android boot'):
                control('crown')
            self.assertEqual(adb.call_count, 1)
            self.assertEqual(adb.call_args.args[1:],
                             ('exec-out', '/system/bin/getprop', 'sys.boot_completed'))

    def test_native_dependencies_and_architecture_boundary(self):
        self.assertEqual(needed(native_elf('libc.so', 'libc++.so')), ['libc.so', 'libc++.so'])
        self.assertEqual(needed(native_elf()), [])
        with self.assertRaises(RuntimeError):
            needed(b'\x7fELF\x01\x01' + bytes(180))
        with self.assertRaises(RuntimeError):
            needed32(native_elf('libc.so'))

    def test_system_namespace_gets_its_own_cpp_dependency(self):
        libraries = {'/system/lib64/liblog.so': native_elf('libc++.so'),
                     '/system/lib64/libc++.so': native_elf('liblog.so')}
        installed = {}

        def read(image, command):
            if image == 'base' and command.startswith('cat '):
                return libraries[command[4:]]
            raise RuntimeError('File not found')

        def put(image, target, data, mode, label, replace=False):
            self.assertEqual(image, 'watch-system')
            installed[target] = data

        with patch('build_image.debugfs', side_effect=read), patch('build_image.install', side_effect=put):
            result = system_closure('watch-system', 'base', ['liblog.so'])
        self.assertEqual(result, ['libc++.so', 'liblog.so'])
        self.assertEqual(installed, libraries)

    def test_property_replacement_keeps_comments_and_other_keys(self):
        original = b'# ro.zygote=zygote64\nro.zygote=zygote64\nro.zygote=duplicate\nother=ok\n'
        changed = set_properties(original, {'ro.zygote': 'zygote32', 'added': '1'})
        self.assertEqual(properties(changed), {'ro.zygote': 'zygote32', 'other': 'ok', 'added': '1'})
        self.assertEqual(changed.count(b'\nro.zygote='), 1)
        self.assertTrue(changed.startswith(b'# ro.zygote=zygote64\n'))

    def test_metadata_rejects_phone_or_wrong_api(self):
        correct = {'pre-device': 'grasslte', 'post-sdk-level': '34',
                   'post-build-incremental': 'OS3.0.190.0.VOFAECNXM'}
        check_metadata(correct)
        for key, value in [('pre-device', 'hongkong'), ('post-sdk-level', '35')]:
            with self.assertRaises(RuntimeError):
                check_metadata({**correct, key: value})

    def test_ramdisk_retains_other_file_bytes_and_headers(self):
        before = (archive([('init', bytes(range(256)))])
                  + archive([('first_stage_ramdisk/fstab.ranchu',
                              b'system /system ext4 ro wait,logical\n'),
                             ('after', b'\x00\x07\xff')]))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'ramdisk.img'
            subprocess.run([tool('lz4', 'lz4'), '-l', '-f', '-', str(path)],
                           input=before, check=True, capture_output=True)
            patch_ramdisk(path)
            after = subprocess.check_output([tool('lz4', 'lz4'), '-dc', str(path)])
            for old, new in zip(entries(before), entries(after), strict=True):
                self.assertEqual(old[0], new[0])
                if old[0] == 'first_stage_ramdisk/fstab.ranchu':
                    self.assertEqual(old[1][:54] + old[1][62:], new[1][:54] + new[1][62:])
                    self.assertTrue(new[2].startswith(old[2]))
                    self.assertIn(b'mi_ext /mnt/vendor/mi_ext ext4 ro', new[2])
                else:
                    self.assertEqual(old, new)

    def test_no_fstab_keeps_original_ramdisk(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'ramdisk.img'
            subprocess.run([tool('lz4', 'lz4'), '-l', '-f', '-', str(path)],
                           input=archive([('init', b'untouched')]), check=True, capture_output=True)
            original = path.read_bytes()
            with self.assertRaises(RuntimeError):
                patch_ramdisk(path)
            self.assertEqual(path.read_bytes(), original)

    def test_overlays_exclude_physical_vendor_devices(self):
        data = (b'/dev/block/bootdevice/by-name/userdata /data f2fs rw wait\n'
                b'overlay /system/app overlay ro,lowerdir=/mnt/vendor/mi_ext/system/app:/system/app check,nofail\n'
                b'/mnt/vendor/mi_ext /mi_ext ext4 ro,bind wait,nofail\n'
                b'mi_ext /mnt/vendor/mi_ext ext4 ro wait,logical,first_stage_mount\n')
        result = original_overlays(data)
        self.assertNotIn(b'bootdevice', result)
        self.assertEqual(len(result.splitlines()), 2)


if __name__ == '__main__':
    unittest.main()
