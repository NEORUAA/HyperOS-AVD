"""Grow stopped Emulator userdata filesystems without replacing encrypted data."""
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile

from common import tool

DATA = 'userdata-qemu.img'
LAYER_NAMES = (DATA, DATA + '.qcow2', 'encryptionkey.img', 'encryptionkey.img.qcow2')
PENDING = '.userdata-resize-pending'
SCHEMA = 'hyperos-avd-userdata-resize-v1'


def check_dependencies(sdk):
    """Let installation entrypoints report missing offline tools before writes."""
    qemu = Path(sdk) / 'emulator/qemu-img'
    if not qemu.is_file():
        raise RuntimeError('Missing Android SDK emulator/qemu-img; install the SDK Emulator first.')
    dependencies = {'qemu': str(qemu), 'fsck': tool('e2fsck', 'e2fsprogs'),
                    'resizer': tool('resize2fs', 'e2fsprogs')}
    if not (shutil.which('lsof') or Path('/usr/sbin/lsof').is_file()):
        raise RuntimeError('Missing lsof; install it before resizing userdata.')
    return dependencies


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(8 * 1024**2), b''):
            value.update(block)
    return value.hexdigest()


def _json(path, value):
    temporary = path.with_suffix('.next')
    with temporary.open('w') as stream:
        json.dump(value, stream, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    _sync_directory(path.parent)


def _sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def assert_offline(avd):
    """Refuse an open image, including an Emulator using a different port."""
    paths = [folder / name for folder in (avd, avd / PENDING / 'original')
             for name in LAYER_NAMES if (folder / name).exists()]
    if not paths:
        return
    executable = shutil.which('lsof')
    if executable is None and Path('/usr/sbin/lsof').is_file():
        executable = '/usr/sbin/lsof'
    if executable is None:
        raise RuntimeError('Missing lsof; cannot safely resize open userdata. Install lsof first.')
    result = subprocess.run([executable, '-t', '--', *map(str, paths)], capture_output=True, text=True)
    if result.returncode == 0 or result.stdout.strip():
        raise RuntimeError('Close this AVD and every process holding its userdata before resizing. No files were changed.')
    if result.returncode != 1:
        raise RuntimeError('Could not check open userdata files with lsof; no resize was performed.')


def _run(arguments, *, allowed=(0,), log=None):
    result = subprocess.run(list(map(str, arguments)), capture_output=True)
    if log is not None:
        with log.open('ab') as stream:
            stream.write(json.dumps(list(map(str, arguments))).encode() + b'\n')
            stream.write(result.stdout + result.stderr + b'\n')
    if result.returncode not in allowed:
        detail = (result.stderr + result.stdout).decode(errors='replace').strip()[-1200:]
        raise RuntimeError(f'Userdata resize failed ({Path(arguments[0]).name}, exit {result.returncode}): {detail}')
    return result.stdout


def _info(qemu, path):
    return json.loads(_run([qemu, 'info', '--output=json', path]))


def ext4_geometry(header):
    """Read the direct ext4 superblock; do not guess an encrypted/partitioned layout."""
    if len(header) < 2048 or struct.unpack_from('<H', header, 1024 + 0x38)[0] != 0xEF53:
        raise RuntimeError('Userdata is not a directly readable ext4 filesystem. Original encrypted/partitioned data was preserved.')
    block_shift = struct.unpack_from('<I', header, 1024 + 0x18)[0]
    if block_shift > 6:
        raise RuntimeError('Unsupported ext4 block size; userdata was preserved.')
    incompatible = struct.unpack_from('<I', header, 1024 + 0x60)[0]
    blocks = struct.unpack_from('<I', header, 1024 + 0x04)[0]
    if incompatible & 0x80:
        blocks |= struct.unpack_from('<I', header, 1024 + 0x150)[0] << 32
    block_size = 1024 << block_shift
    if not blocks:
        raise RuntimeError('Invalid ext4 block count; userdata was preserved.')
    return {'bytes': blocks * block_size, 'block_size': block_size,
            'uuid': header[1024 + 0x68:1024 + 0x78].hex(),
            'incompatible': incompatible & ~0x04}


def _geometry(qemu, path, output):
    _run([qemu, 'dd', 'bs=4096', 'count=1', 'if=' + str(path), 'of=' + str(output)])
    return ext4_geometry(output.read_bytes())


def _layers(qemu, avd):
    layers = {}
    for name in LAYER_NAMES:
        path = avd / name
        if path.is_symlink() or path.exists() and not path.is_file():
            raise RuntimeError('Refused a nonregular userdata/key image: ' + name)
        if not path.exists():
            continue
        info = _info(qemu, path)
        expected = 'qcow2' if name.endswith('.qcow2') else 'raw'
        if info.get('format') != expected or info.get('encrypted'):
            raise RuntimeError('Unsupported userdata/key image format; original data was preserved: ' + name)
        if name.endswith('.qcow2'):
            backing = name.removesuffix('.qcow2')
            if (info.get('backing-filename') != backing or not (avd / backing).is_file()
                    or (avd / backing).is_symlink()
                    or info.get('backing-filename-format', 'raw') != 'raw'):
                raise RuntimeError('Unexpected userdata/key backing chain; original data was preserved: ' + name)
            if name.startswith(DATA) and info.get('snapshots'):
                raise RuntimeError('Remove or separately preserve userdata internal snapshots before resizing.')
        layers[name] = info
    if DATA not in layers and DATA + '.qcow2' in layers:
        raise RuntimeError('Userdata overlay has no base; original data was preserved.')
    return layers


def _recover(avd):
    """Restore a complete original chain after an interrupted two-file activation."""
    pending = avd / PENDING
    if not pending.exists() and not pending.is_symlink():
        return
    if pending.is_symlink() or not pending.is_dir():
        raise RuntimeError('Refused an unknown userdata resize transaction: ' + str(pending))
    assert_offline(avd)
    try:
        if (pending / 'transaction.json').is_symlink():
            raise ValueError('foreign receipt')
        receipt = json.loads((pending / 'transaction.json').read_text())
        hashes = receipt['original_sha256']
        if (receipt.get('schema') != SCHEMA or receipt.get('avd') != str(avd)
                or not isinstance(hashes, dict) or DATA not in hashes
                or set(hashes) - set(LAYER_NAMES)):
            raise ValueError('unverified transaction')
        original = pending / 'original'
        if original.is_symlink() or not original.is_dir():
            raise ValueError('unverified original directory')
        staged = pending / 'staged'
        if staged.is_symlink() or not staged.is_dir():
            raise ValueError('unverified staged directory')
        for name in (DATA, DATA + '.qcow2'):
            saved = original / name
            if (saved.exists() or saved.is_symlink()) and (
                    name not in hashes or saved.is_symlink() or not saved.is_file()):
                raise ValueError('unrecorded or nonregular saved userdata')
        if not any((original / name).exists() for name in (DATA, DATA + '.qcow2')):
            # Preparation never changes active layers. A stopped guest may
            # legitimately have written new data since this staging attempt.
            _sync_directory(avd)
            shutil.rmtree(pending)
            _sync_directory(avd)
            return
        # Validate every old file before restoring any file.
        for name, expected in hashes.items():
            saved = pending / 'original' / name
            source = saved if name.startswith(DATA) and saved.exists() else avd / name
            if source.is_symlink() or not source.is_file() or digest(source) != expected:
                raise ValueError('original chain checksum mismatch')
        staged_hashes = receipt.get('staged_sha256', {})
        if not isinstance(staged_hashes, dict) or set(staged_hashes) - {DATA, DATA + '.qcow2'}:
            raise ValueError('unverified staged hashes')
        for name in (DATA, DATA + '.qcow2'):
            active = avd / name
            if not active.exists() and not active.is_symlink():
                continue
            if (active.is_symlink() or not active.is_file() or
                    digest(active) not in (hashes.get(name), staged_hashes.get(name))):
                # A later guest may have used a fully activated image before
                # an interrupted transaction was noticed. Never erase writes.
                raise ValueError('active userdata changed after activation')
        for name in (DATA, DATA + '.qcow2'):
            saved = pending / 'original' / name
            if saved.exists():
                saved.replace(avd / name)
                _sync_directory(avd)
                _sync_directory(original)
    except (OSError, ValueError, KeyError) as error:
        raise RuntimeError('Userdata resize recovery needs inspection; original transaction retained at ' + str(pending)) from error
    shutil.rmtree(pending)
    _sync_directory(avd)


def _activate(avd, pending, names):
    for name in names:
        (avd / name).replace(pending / 'original' / name)
        _sync_directory(avd)
        _sync_directory(pending / 'original')
    for name in names:
        (pending / 'staged' / name).replace(avd / name)
        _sync_directory(avd)


def _resize_userdata(sdk, avd, wanted):
    """Grow the effective ext4 and rebuild its coherent raw/QCOW2 chain offline."""
    avd = Path(avd).resolve()
    if type(wanted) is not int or wanted <= 0:
        raise RuntimeError('Use a positive integer userdata size.')
    if not avd.is_dir():
        return {'changed': False, 'filesystem_bytes': None}
    assert_offline(avd)
    _recover(avd)
    qemu = Path(sdk) / 'emulator/qemu-img'
    layers = _layers(qemu, avd)
    if DATA not in layers:
        return {'changed': False, 'filesystem_bytes': None}
    data_names = [name for name in (DATA, DATA + '.qcow2') if name in layers]
    if any(layers[name]['virtual-size'] > wanted for name in data_names):
        raise RuntimeError('Storage cannot be reduced without erasing data; choose a larger size.')
    top = avd / data_names[-1]
    with tempfile.TemporaryDirectory(prefix='.userdata-resize-probe-', dir=avd) as temporary:
        geometry = _geometry(qemu, top, Path(temporary) / 'superblock.img')
    if geometry['bytes'] > layers[data_names[-1]]['virtual-size'] or geometry['bytes'] > wanted:
        raise RuntimeError('Ext4 exceeds its virtual disk; userdata was preserved for inspection.')
    if wanted % geometry['block_size']:
        raise RuntimeError('Requested userdata size is not aligned to its ext4 block size.')
    if geometry['bytes'] == wanted and all(layers[name]['virtual-size'] == wanted for name in data_names):
        return {'changed': False, 'filesystem_bytes': wanted, 'uuid': geometry['uuid']}
    dependencies = check_dependencies(sdk)
    fsck, resizer = dependencies['fsck'], dependencies['resizer']
    allocated = sum((avd / name).stat().st_blocks * 512 for name in data_names)
    if shutil.disk_usage(avd).free < allocated + wanted // 50 + 64 * 1024**2:
        raise RuntimeError('Insufficient free host storage for a staged filesystem resize; originals were preserved.')
    hashes = {name: digest(avd / name) for name in layers}
    pending = avd / PENDING
    pending.mkdir(mode=0o700)
    receipt = {'schema': SCHEMA, 'avd': str(avd), 'original_sha256': hashes,
               'requested_bytes': wanted, 'before': geometry}
    try:
        (pending / 'original').mkdir()
        (pending / 'staged').mkdir()
        _json(pending / 'transaction.json', receipt)
    except BaseException:
        shutil.rmtree(pending)
        raise
    log = pending / 'commands.log'
    try:
        staged = pending / 'staged' / DATA
        _run([qemu, 'convert', '-O', 'raw', top, staged], log=log)
        _run([qemu, 'compare', top, staged], log=log)
        if layers[data_names[-1]]['virtual-size'] < wanted:
            _run([qemu, 'resize', staged, wanted], log=log)
        # Preen repairs only issues considered safe by e2fsck. Refuse errors
        # requiring manual intervention; never use a blanket yes/force resize.
        _run([fsck, '-pf', staged], allowed=(0, 1, 2, 3), log=log)
        _run([resizer, staged], log=log)
        _run([fsck, '-fn', staged], log=log)
        with staged.open('rb') as stream:
            after = ext4_geometry(stream.read(4096))
        if (after['bytes'] != wanted or after['uuid'] != geometry['uuid']
                or after['incompatible'] != geometry['incompatible']):
            raise RuntimeError('Resized ext4 geometry/identity differs from the expected filesystem.')
        if len(data_names) == 2:
            _run([qemu, 'create', '-f', 'qcow2', '-F', 'raw', '-b', DATA,
                  pending / 'staged' / (DATA + '.qcow2')], log=log)
            _run([qemu, 'check', pending / 'staged' / (DATA + '.qcow2')], log=log)
            _run([qemu, 'compare', staged, pending / 'staged' / (DATA + '.qcow2')], log=log)
        for name in data_names:
            with (pending / 'staged' / name).open('rb') as stream:
                os.fsync(stream.fileno())
        # Preserve all key bytes and backing names; they are never resized.
        for name in layers:
            if name.startswith('encryptionkey'):
                saved = pending / 'original' / name
                shutil.copy2(avd / name, saved)
                with saved.open('rb') as stream:
                    os.fsync(stream.fileno())
        _sync_directory(pending / 'original')
        assert_offline(avd)
        if any(digest(avd / name) != checksum for name, checksum in hashes.items()):
            raise RuntimeError('Original userdata/key files changed during staging; activation refused.')
        _json(pending / 'transaction.json', {**receipt, 'after': after,
              'staged_sha256': {name: digest(pending / 'staged' / name) for name in data_names}})
        _activate(avd, pending, data_names)
        _layers(qemu, avd)
        final = _geometry(qemu, avd / data_names[-1], pending / 'final-superblock.img')
        if final != after or any(digest(avd / name) != hashes[name] for name in layers if name.startswith('encryptionkey')):
            raise RuntimeError('Activated userdata filesystem or encryption keys failed verification.')
        backup = avd / ('.userdata-resize-backup-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
        pending.rename(backup)
        _sync_directory(avd)
    except BaseException:
        _recover(avd)
        raise
    print(f'Userdata ext4 grew from {geometry["bytes"] / 1024**3:g} to {wanted / 1024**3:g} GiB; original chain: {backup}', flush=True)
    return {'changed': True, 'filesystem_bytes': wanted, 'uuid': after['uuid'], 'backup': str(backup)}


def resize_userdata(sdk, avd, wanted):
    """Serialize each private resize transaction independently of the manager."""
    avd = Path(avd)
    if avd.is_symlink():
        raise RuntimeError('Refused a symlink userdata directory.')
    avd = avd.resolve()
    if not avd.is_dir():
        return {'changed': False, 'filesystem_bytes': None}
    lock = avd / '.userdata-resize.lock'
    if lock.is_symlink() or lock.exists() and not lock.is_file():
        raise RuntimeError('Refused a foreign userdata resize lock.')
    with lock.open('a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Another userdata resize is working on this AVD.') from None
        return _resize_userdata(sdk, avd, wanted)
