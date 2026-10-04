"""Verify Android security metadata survives a macOS EROFS conversion."""
import io
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from erofs_image import Reader, build, tar_entry


@unittest.skipUnless(shutil.which('mkfs.erofs') and shutil.which('fsck.erofs'), 'Requires erofs-utils')
class SecurityMetadataTests(unittest.TestCase):
    def test_subtree_overlay_precedence_and_replacement_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            def image(name, files):
                tar = root / (name + '.tar')
                with tarfile.open(tar, 'w', format=tarfile.PAX_FORMAT) as archive:
                    for path in ('', 'product', 'product/etc'):
                        item = tar_entry(path, {'uid': 0, 'gid': 0,
                            'mode': stat.S_IFDIR | 0o755, 'attrs': {}})
                        item.type = tarfile.DIRTYPE
                        archive.addfile(item)
                    for path, data in files.items():
                        item = tar_entry(path, {'uid': 1000, 'gid': 1000,
                            'mode': stat.S_IFREG | 0o640, 'attrs': {}})
                        item.size = len(data)
                        archive.addfile(item, io.BytesIO(data))
                output = root / (name + '.img')
                subprocess.run(['mkfs.erofs', '-b4096', '--tar=f', str(output), str(tar)],
                               check=True, capture_output=True)
                return output
            base = image('base', {'product/etc/shared': b'base',
                                  'product/etc/keep': b'keep'})
            first = image('mi_product', {'product/etc/shared': b'first',
                                        'product/etc/runtime': b'runtime'})
            last = image('mi_ext', {'product/etc/shared': b'official'})
            output = root / 'merged.img'
            build(output, [('', base), ('product:product', first), ('product:product', last)],
                  root / 'work', {'product/etc/runtime':
                                 (b'replaced', 0o644, 'u:object_r:system_file:s0'),
                                 'product/app/Preloaded/lib/arm64/libtest.so':
                                 (b'native', 0o644, 'u:object_r:system_file:s0')},
                  removals=('product/etc/keep',))
            extracted = root / 'verified'
            subprocess.run(['fsck.erofs', '--extract=' + str(extracted), str(output)],
                           check=True, capture_output=True)
            self.assertEqual((extracted / 'product/etc/shared').read_bytes(), b'official')
            self.assertFalse((extracted / 'product/etc/keep').exists())
            self.assertEqual((extracted / 'product/etc/runtime').read_bytes(), b'replaced')
            self.assertEqual((extracted / 'product/app/Preloaded/lib/arm64/libtest.so').read_bytes(), b'native')
            reader = Reader(output)
            try:
                paths = dict(reader.walk())
                self.assertNotIn('product/product', paths)
                self.assertEqual(paths['product/etc/shared']['uid'], 1000)
                for path in ('product/app', 'product/app/Preloaded',
                             'product/app/Preloaded/lib', 'product/app/Preloaded/lib/arm64'):
                    self.assertEqual(paths[path]['mode'], stat.S_IFDIR | 0o755)
                    self.assertEqual((paths[path]['uid'], paths[path]['gid']), (0, 0))
                    self.assertEqual(paths[path]['attrs'],
                                     {'security.selinux': b'u:object_r:system_file:s0\0'})
            finally:
                reader.close()

    def test_ownership_capabilities_symlinks_and_hardlinks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            capability = bytes.fromhex('01000002c0000000000000000000000000000000')
            attrs = {'security.selinux': b'u:object_r:system_file:s0',
                     'security.capability': capability}
            with tarfile.open(root / 'source.tar', 'w', format=tarfile.PAX_FORMAT) as archive:
                directory = tar_entry('', {'uid': 0, 'gid': 0, 'mode': stat.S_IFDIR | 0o755,
                                          'attrs': {'security.selinux': b'u:object_r:rootfs:s0'}})
                directory.type = tarfile.DIRTYPE
                archive.addfile(directory)
                item = tar_entry('binary', {'uid': 1000, 'gid': 2000,
                                           'mode': stat.S_IFREG | 0o750, 'attrs': attrs})
                item.size = 4
                archive.addfile(item, io.BytesIO(b'test'))
                link = tar_entry('hard', {'uid': 1000, 'gid': 2000,
                                         'mode': stat.S_IFREG | 0o750, 'attrs': attrs})
                link.type, link.linkname = tarfile.LNKTYPE, 'binary'
                archive.addfile(link)
                symbolic = tar_entry('symbolic', {'uid': 0, 'gid': 0,
                                     'mode': stat.S_IFLNK | 0o777, 'attrs': {}})
                symbolic.type, symbolic.linkname = tarfile.SYMTYPE, 'binary'
                archive.addfile(symbolic)
            subprocess.run(['mkfs.erofs', '-b4096', '--tar=f', '--sort=none',
                            str(root / 'source.img'), str(root / 'source.tar')],
                           check=True, capture_output=True)
            build(root / 'merged.img', [('', root / 'source.img')], root / 'work', {})
            reader = Reader(root / 'merged.img')
            try:
                tree = dict(reader.walk())
                self.assertEqual(tree['binary']['attrs'], attrs)
                self.assertEqual((tree['binary']['uid'], tree['binary']['gid']), (1000, 2000))
                self.assertEqual(tree['binary']['mode'], stat.S_IFREG | 0o750)
                self.assertEqual(tree['binary']['nid'], tree['hard']['nid'])
                self.assertEqual(reader.flat(tree['symbolic']), b'binary')
                self.assertEqual(reader.block, 4096)
            finally:
                reader.close()

            preserved = root / 'patched.img'
            build(preserved, [('', root / 'source.img')], root / 'patch-work',
                  {'binary': (b'patched', 0o750, 'u:object_r:system_file:s0')},
                  preserve_replacement_metadata=True)
            reader = Reader(preserved)
            try:
                binary = dict(reader.walk())['binary']
                self.assertEqual((binary['uid'], binary['gid']), (1000, 2000))
                self.assertEqual(binary['mode'], stat.S_IFREG | 0o750)
                self.assertEqual(binary['attrs'], attrs)
            finally:
                reader.close()
            self.assertEqual(subprocess.check_output(
                ['dump.erofs', '--cat', '--path=/binary', str(preserved)]), b'patched')
            with self.assertRaisesRegex(RuntimeError, 'Replacement metadata differs'):
                build(root / 'wrong-mode.img', [('', root / 'source.img')], root / 'patch-work',
                      {'binary': (b'patched', 0o755, 'u:object_r:system_file:s0')},
                      preserve_replacement_metadata=True)
            self.assertFalse((root / 'wrong-mode.img').exists())


if __name__ == '__main__':
    unittest.main()
