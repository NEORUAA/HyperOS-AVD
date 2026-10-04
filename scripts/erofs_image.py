"""Build a standalone EROFS image while retaining Android inode metadata."""
import io
import mmap
from pathlib import Path
import stat
import struct
import subprocess
import tarfile
import tempfile

from common import sha256, tool


class Reader:
    """Read inode metadata and uncompressed directories from verified OTA images.

    fsck.erofs handles compressed file data; this reader retains uid/gid,
    hardlinks, mode, SELinux labels and binary Linux capability xattrs that
    cannot be represented faithfully by a macOS extracted directory.
    """
    def __init__(self, path):
        self.file = Path(path).open('rb')
        self.data = mmap.mmap(self.file.fileno(), 0, access=mmap.ACCESS_READ)
        if struct.unpack_from('<I', self.data, 1024)[0] != 0xe0f5e1e2:
            raise RuntimeError('Expected a raw EROFS partition: ' + str(path))
        self.block = 1 << self.data[1036]
        self.root = struct.unpack_from('<H', self.data, 1038)[0]
        self.meta = struct.unpack_from('<I', self.data, 1064)[0] * self.block
        self.shared = struct.unpack_from('<I', self.data, 1068)[0] * self.block
        if self.data[1115]:
            raise RuntimeError('Long xattr prefixes are not supported by this OTA reader.')

    def close(self):
        self.data.close()
        self.file.close()

    def xattr(self, offset):
        length, index, size = struct.unpack_from('<BBH', self.data, offset)
        prefixes = {1: 'user.', 2: 'system.posix_acl_access',
                    3: 'system.posix_acl_default', 4: 'trusted.', 6: 'security.'}
        if index not in prefixes:
            raise RuntimeError('Unsupported xattr namespace: ' + str(index))
        name = prefixes[index] + self.data[offset + 4:offset + 4 + length].decode()
        value = self.data[offset + 4 + length:offset + 4 + length + size]
        return name, value, (4 + length + size + 3) // 4 * 4

    def inode(self, nid):
        offset = self.meta + nid * 32
        fmt, count, mode = struct.unpack_from('<HHH', self.data, offset)
        extended = bool(fmt & 1)
        size = struct.unpack_from('<Q' if extended else '<I', self.data, offset + 8)[0]
        uid, gid = struct.unpack_from('<II' if extended else '<HH', self.data, offset + 24)
        inode_size = 64 if extended else 32
        xsize = 12 + (count - 1) * 4 if count else 0
        attrs = {}
        if count:
            start = offset + inode_size
            shared_count = self.data[start + 4]
            cursor = start + 12
            for _ in range(shared_count):
                index = struct.unpack_from('<I', self.data, cursor)[0]
                key, value, _ = self.xattr(self.shared + index * 4)
                attrs[key] = value
                cursor += 4
            while cursor < start + xsize:
                key, value, length = self.xattr(cursor)
                attrs[key] = value
                cursor += length
            if cursor != start + xsize:
                raise RuntimeError('Malformed inode xattrs.')
        return {'nid': nid, 'mode': mode, 'size': size, 'uid': uid, 'gid': gid,
                'attrs': attrs, 'layout': (fmt >> 1) & 7,
                'address': struct.unpack_from('<I', self.data, offset + 16)[0] * self.block,
                'inline': offset + inode_size + xsize}

    def flat(self, inode):
        size = inode['size']
        if inode['layout'] == 0:
            return self.data[inode['address']:inode['address'] + size]
        if inode['layout'] != 2:
            raise RuntimeError('Expected a flat directory or symlink.')
        full = size // self.block * self.block
        return (self.data[inode['address']:inode['address'] + full]
                + self.data[inode['inline']:inode['inline'] + size - full])

    def walk(self, nid=None, path=''):
        inode = self.inode(self.root if nid is None else nid)
        yield path, inode
        if not stat.S_ISDIR(inode['mode']):
            return
        data = self.flat(inode)
        for base in range(0, len(data), self.block):
            block = data[base:base + self.block]
            count = struct.unpack_from('<H', block, 8)[0] // 12
            for i in range(count):
                child, start = struct.unpack_from('<QH', block, i * 12)
                end = struct.unpack_from('<H', block, (i + 1) * 12 + 8)[0] if i + 1 < count else len(block)
                name = block[start:end].rstrip(b'\0').decode('utf-8', 'surrogateescape')
                if name in ('.', '..'):
                    continue
                if not name or '/' in name or '\0' in name:
                    raise RuntimeError('Invalid directory entry.')
                yield from self.walk(child, path + '/' + name if path else name)


def tar_entry(path, inode):
    entry = tarfile.TarInfo(path or '.')
    entry.uid, entry.gid = inode['uid'], inode['gid']
    entry.mode = stat.S_IMODE(inode['mode'])
    entry.mtime = 1230768000
    entry.pax_headers = {'SCHILY.xattr.' + name: value.decode('utf-8', 'surrogateescape')
                         for name, value in inode['attrs'].items()}
    return entry


def build(destination, partitions, work, replacements, removals=(),
          preserve_replacement_metadata=False):
    """Merge trees in order; prefix:subtree selects an OTA overlay subtree.

    Resolve precedence before emitting the tar so duplicate members cannot
    silently retain an older file's content or hardlink target.
    """
    work, destination = Path(work), Path(destination)
    work.mkdir(parents=True, exist_ok=True)
    expected, sources, readers = {}, {}, []
    temporary = destination.with_suffix('.next.img')
    try:
        for selection, image in partitions:
            prefix, _, subtree = selection.partition(':')
            source_hash = sha256(image)
            cache_name = 'extract-' + (prefix or 'system')
            if subtree:
                cache_name = 'extract-overlay-' + Path(image).stem + '-' + subtree
            # A new source must not overwrite old hardlinks in a cached tree.
            extracted = work / (cache_name + '-' + source_hash[:16])
            marker = extracted / '.extraction-complete'
            if not marker.exists() or marker.read_text() != source_hash:
                with tempfile.TemporaryDirectory(prefix='extract-staging-', dir=work) as staging:
                    tree = Path(staging) / 'tree'
                    subprocess.run([tool('fsck.erofs', 'erofs-utils'), '-d0',
                                    '--extract=' + str(tree), '--preserve-perms', str(image)], check=True)
                    (tree / '.extraction-complete').write_text(source_hash)
                    if extracted.exists():
                        old = Path(tempfile.mkdtemp(prefix='incomplete-extract-', dir=work)) / 'tree'
                        extracted.rename(old)
                    tree.rename(extracted)
            reader = Reader(image)
            readers.append(reader)
            tree = dict(reader.walk())
            entries = reader.walk(tree[subtree]['nid']) if subtree else tree.items()
            for relative, inode in entries:
                path = '/'.join(p for p in (prefix, relative) if p)
                expected[path] = inode
                sources[path] = (reader, extracted / subtree / relative, str(Path(image).resolve()))
            print('Imported official partition: ' + (selection or 'system'), flush=True)
        def safe_path(path):
            if not path or any(part in ('', '.', '..') for part in path.split('/')) or '\0' in path:
                raise RuntimeError('Invalid image edit path: ' + repr(path))
        for path in removals:
            safe_path(path)
            for existing in list(expected):
                if existing == path or existing.startswith(path + '/'):
                    del expected[existing]
                    sources.pop(existing, None)
        # System APK sidecars need real directory inodes, not implicit tar
        # parents whose ownership and SELinux labels would be unverified.
        for path in replacements:
            safe_path(path)
            if path in expected and not stat.S_ISREG(expected[path]['mode']):
                raise RuntimeError('Replacement is not a regular file: ' + path)
            if preserve_replacement_metadata:
                _, mode, label = replacements[path]
                inode = expected.get(path)
                if (inode is None or inode['mode'] != (stat.S_IFREG | mode) or
                        inode['attrs'].get('security.selinux', b'').rstrip(b'\0') != label.encode()):
                    raise RuntimeError('Replacement metadata differs for ' + path)
            parents = path.split('/')[:-1]
            for index in range(1, len(parents) + 1):
                parent = '/'.join(parents[:index])
                if parent in expected:
                    if not stat.S_ISDIR(expected[parent]['mode']):
                        raise RuntimeError('Replacement parent is not a directory: ' + parent)
                else:
                    expected[parent] = {'uid': 0, 'gid': 0, 'mode': stat.S_IFDIR | 0o755,
                        'attrs': {'security.selinux': b'u:object_r:system_file:s0\0'}}
        command = [tool('mkfs.erofs', 'erofs-utils'), '-b4096', '-z', 'lz4',
                   '--workers=2', '--tar=f', '--sort=none', str(temporary)]
        with subprocess.Popen(command, stdin=subprocess.PIPE) as process:
            try:
                hardlinks = {}
                with tarfile.open(fileobj=process.stdin, mode='w|', format=tarfile.PAX_FORMAT,
                                  encoding='utf-8') as archive:
                    for path in sorted(expected, key=lambda path: (path.count('/'), path)):
                        if path in replacements:
                            continue
                        inode = expected[path]
                        entry = tar_entry(path, inode)
                        if stat.S_ISDIR(inode['mode']):
                            entry.type = tarfile.DIRTYPE
                            archive.addfile(entry)
                        elif stat.S_ISLNK(inode['mode']):
                            reader, extracted_file, source_id = sources[path]
                            entry.type = tarfile.SYMTYPE
                            entry.linkname = reader.flat(inode).decode('utf-8', 'surrogateescape')
                            archive.addfile(entry)
                        elif stat.S_ISREG(inode['mode']):
                            reader, extracted_file, source_id = sources[path]
                            identity = (source_id, inode['nid'])
                            if identity in hardlinks:
                                entry.type = tarfile.LNKTYPE
                                entry.linkname = hardlinks[identity]
                                archive.addfile(entry)
                            else:
                                entry.size = inode['size']
                                with extracted_file.open('rb') as source:
                                    archive.addfile(entry, source)
                                hardlinks[identity] = path
                        else:
                            raise RuntimeError('Unexpected special file: ' + path)
                    for path, (data, mode, label) in replacements.items():
                        if preserve_replacement_metadata:
                            inode = dict(expected[path])
                        else:
                            inode = {'uid': 0, 'gid': 0, 'mode': stat.S_IFREG | mode,
                                     'attrs': {'security.selinux': label.encode() + b'\0'}}
                        entry = tar_entry(path, inode)
                        entry.size = len(data)
                        archive.addfile(entry, io.BytesIO(data))
                        expected[path] = inode
            finally:
                process.stdin.close()
            if process.wait():
                raise RuntimeError('EROFS conversion failed.')
    finally:
        for reader in readers:
            reader.close()
    subprocess.run([tool('fsck.erofs', 'erofs-utils'), '--extract', str(temporary)], check=True)
    reader = Reader(temporary)
    try:
        actual = dict(reader.walk())
        if actual.keys() != expected.keys():
            raise RuntimeError('Merged EROFS paths differ from the source trees.')
        for path, inode in expected.items():
            for field in ('mode', 'uid', 'gid', 'attrs'):
                if actual[path][field] != inode[field]:
                    raise RuntimeError('Metadata changed for ' + path + ': ' + field)
    finally:
        reader.close()
    temporary.replace(destination)
    print('Verified all ' + str(len(expected)) + ' paths, modes, owners and xattrs.', flush=True)
