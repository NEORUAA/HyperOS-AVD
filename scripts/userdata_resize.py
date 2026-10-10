"""Grow stopped Emulator userdata filesystems without replacing encrypted data."""
from datetime import datetime, timezone
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import tempfile
import uuid

from common import tool

DATA = 'userdata-qemu.img'
LAYER_NAMES = (DATA, DATA + '.qcow2', 'encryptionkey.img', 'encryptionkey.img.qcow2')
PENDING = '.userdata-resize-pending'
SCHEMA = 'hyperos-avd-userdata-resize-v1'
VERIFIED = '.userdata-filesystem.json'
VERIFIED_SCHEMA = 'hyperos-avd-prepared-filesystem-v1'
GUEST_VERIFIED = '.userdata-guest-filesystem.json'
GUEST_VERIFIED_SCHEMA = 'hyperos-avd-guest-filesystem-v1'
GUEST_TOKEN_SCHEMA = 'hyperos-avd-guest-resize-token-v1'
GUEST_RECORD_SCHEMA = 'hyperos-avd-guest-capacity-v1'


def _valid_geometry(value):
    return (isinstance(value, dict) and set(value) == {'bytes', 'block_size', 'uuid', 'incompatible'}
            and all(type(value[key]) is int for key in ('bytes', 'block_size', 'incompatible'))
            and value['bytes'] > 0 and value['block_size'] in (1024, 2048, 4096, 8192, 16384, 32768, 65536)
            and value['bytes'] % value['block_size'] == 0 and 0 <= value['incompatible'] <= 0xffffffff
            and not value['incompatible'] & 0x04
            and isinstance(value['uuid'], str) and re.fullmatch('[0-9a-f]{32}', value['uuid']) is not None)


def _valid_key_pin(value):
    return (isinstance(value, dict) and set(value) == {'sha256', 'virtual_size'}
            and isinstance(value['sha256'], str) and re.fullmatch('[0-9a-f]{64}', value['sha256']) is not None
            and type(value['virtual_size']) is int and value['virtual_size'] > 0)


def _valid_layer_identity(value):
    return (isinstance(value, dict) and set(value) == set(LAYER_NAMES)
            and all(isinstance(identity, dict) and set(identity) == {'dev', 'ino'}
                    and all(type(identity[key]) is int and identity[key] >= 0 for key in ('dev', 'ino'))
                    for identity in value.values()))


class OpaqueFilesystemError(RuntimeError):
    """The effective device cannot be decoded by host ext4 tools."""


class OfflineBackupRequiredError(OpaqueFilesystemError):
    """An otherwise eligible encrypted growth needs its complete offline backup."""


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


def _prefix_digest(path, size):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        remaining = size
        while remaining:
            block = stream.read(min(remaining, 8 * 1024**2))
            if not block:
                raise RuntimeError('Staged userdata lost original bytes; activation refused.')
            value.update(block)
            remaining -= len(block)
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
        raise OpaqueFilesystemError('Userdata is not a directly readable ext4 filesystem. Original encrypted/partitioned data was preserved.')
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


def _template_key_pin(qemu, avd, *, require_manifest=False):
    """Pin a clean metadata base before the Emulator creates its writable layers."""
    workspace = avd.parent.parent
    template = workspace / 'images/encryptionkey.img'
    if template.is_symlink() or not template.is_file():
        return None
    info = _info(qemu, template)
    if info.get('format') != 'raw' or info.get('encrypted'):
        return None
    checksum = digest(template)
    if require_manifest:
        manifest = workspace / 'local/installed-release.json'
        if manifest.is_symlink() or not manifest.is_file():
            return None
        value = json.loads(manifest.read_text())
        if not isinstance(value, dict):
            return None
        expected = value.get('files', {}).get('images/encryptionkey.img', {})
        if expected.get('sha256') != checksum or expected.get('size') != template.stat().st_size:
            return None
    return {'sha256': checksum, 'virtual_size': info['virtual-size']}


def _capacity_marker(avd):
    destination = avd / VERIFIED
    for path in (destination, destination.with_suffix('.next')):
        if path.is_symlink() or path.exists() and not path.is_file():
            raise RuntimeError('Refused a foreign userdata capacity receipt.')
    if not destination.exists():
        return None
    try:
        saved = json.loads(destination.read_text())
        if (not isinstance(saved, dict) or saved.get('schema') != VERIFIED_SCHEMA
                or set(saved) != {'schema', 'filesystem', 'data_base_sha256', 'key_base'}):
            raise ValueError('foreign schema')
        geometry = saved['filesystem']
        if (not isinstance(geometry, dict) or set(geometry) != {'bytes', 'block_size', 'uuid', 'incompatible'}
                or any(type(geometry[key]) is not int for key in ('bytes', 'block_size', 'incompatible'))
                or not isinstance(geometry['uuid'], str) or len(geometry['uuid']) != 32
                or not isinstance(saved['data_base_sha256'], str) or len(saved['data_base_sha256']) != 64):
            raise ValueError('foreign geometry')
        pin = saved['key_base']
        if pin is not None and (not isinstance(pin, dict) or set(pin) != {'sha256', 'virtual_size'}
                or not isinstance(pin['sha256'], str) or len(pin['sha256']) != 64
                or type(pin['virtual_size']) is not int or pin['virtual_size'] <= 0):
            raise ValueError('foreign key pin')
        return saved
    except (ValueError, KeyError, TypeError) as error:
        raise RuntimeError('Refused a malformed or foreign userdata capacity receipt.') from error


GUEST_IDENTITY_FIELDS = {'name', 'serial', 'boot_id', 'hardware', 'product_device', 'incremental',
                         'root_uid', 'selinux', 'mounted_type', 'mapped_device', 'mapped_device_bytes'}


def _valid_guest_identity(value, wanted):
    if not isinstance(value, dict) or set(value) != GUEST_IDENTITY_FIELDS:
        return False
    serial = value['serial']
    return (isinstance(value['name'], str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', value['name']) is not None
            and isinstance(serial, str) and re.fullmatch(r'emulator-[0-9]+', serial) is not None
            and 5554 <= int(serial.removeprefix('emulator-')) <= 5682
            and int(serial.removeprefix('emulator-')) % 2 == 0
            and isinstance(value['boot_id'], str)
            and re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', value['boot_id']) is not None
            and value['hardware'] == 'ranchu' and value['selinux'] == 'Enforcing'
            and type(value['root_uid']) is int and value['root_uid'] == 0
            and value['mounted_type'] == 'ext4'
            and isinstance(value['mapped_device'], str) and re.fullmatch(r'/dev/block/dm-[0-9]+', value['mapped_device']) is not None
            and type(value['mapped_device_bytes']) is int and value['mapped_device_bytes'] == wanted
            and isinstance(value['product_device'], str) and bool(value['product_device'])
            and isinstance(value['incremental'], str))


def _guest_capacity_marker(avd):
    """Read only our exact guest-proof schema; never adopt an arbitrary JSON record."""
    destination = avd / GUEST_VERIFIED
    for path in (destination, destination.with_suffix('.next')):
        if path.is_symlink() or path.exists() and not path.is_file():
            raise RuntimeError('Refused a foreign guest userdata capacity receipt.')
    if not destination.exists():
        return None
    try:
        saved = json.loads(destination.read_text())
        if (not isinstance(saved, dict) or set(saved) != {'schema', 'requested_bytes', 'base_filesystem',
                'data_base_sha256', 'key_base', 'filesystem', 'guest', 'layer_identity'}
                or saved['schema'] != GUEST_VERIFIED_SCHEMA
                or type(saved['requested_bytes']) is not int or saved['requested_bytes'] <= 0
                or not _valid_geometry(saved['base_filesystem']) or not _valid_geometry(saved['filesystem'])
                or saved['base_filesystem']['bytes'] > saved['requested_bytes']
                or saved['filesystem']['bytes'] != saved['requested_bytes']
                or not isinstance(saved['data_base_sha256'], str)
                or re.fullmatch('[0-9a-f]{64}', saved['data_base_sha256']) is None
                or not _valid_key_pin(saved['key_base'])
                or not _valid_layer_identity(saved['layer_identity'])
                or not _valid_guest_identity(saved['guest'], saved['requested_bytes'])):
            raise ValueError('foreign guest proof')
        return saved
    except (ValueError, KeyError, TypeError) as error:
        raise RuntimeError('Refused a malformed or foreign guest userdata capacity receipt.') from error


def _chain_unchanged(qemu, avd, layers, before):
    assert_offline(avd)
    if _layers(qemu, avd) != layers or any(
            getattr(before[name], field) != getattr((avd / name).stat(), field)
            for name in layers for field in ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')):
        raise OpaqueFilesystemError('Userdata/key chain changed during capacity verification; start refused.')


def _opaque_context(qemu, avd, layers, wanted):
    """Bind a stopped four-layer encrypted chain without running host fs tools."""
    refusal = ('Encrypted userdata needs guest-scoped capacity verification/repair; '
               'host filesystem tools cannot resize it. No userdata or keys were changed.')
    if (set(layers) != set(LAYER_NAMES) or any(layers[name]['virtual-size'] != wanted
            for name in (DATA, DATA + '.qcow2'))
            or layers['encryptionkey.img']['virtual-size'] != layers['encryptionkey.img.qcow2']['virtual-size']):
        raise OpaqueFilesystemError(refusal)
    marker = avd / 'qemu-version.txt'
    if marker.is_symlink() or not marker.is_file() or marker.read_text().strip() != '2':
        raise OpaqueFilesystemError(refusal)
    before = {name: (avd / name).stat() for name in layers}
    with tempfile.TemporaryDirectory(prefix='.userdata-base-probe-', dir=avd) as temporary:
        base = _geometry(qemu, avd / DATA, Path(temporary) / 'superblock.img')
    if base['bytes'] > wanted:
        raise OpaqueFilesystemError(refusal)
    context = {'base_filesystem': base, 'data_base_sha256': digest(avd / DATA),
               'key_base': {'sha256': digest(avd / 'encryptionkey.img'),
                            'virtual_size': layers['encryptionkey.img']['virtual-size']},
               'layer_identity': {name: {'dev': before[name].st_dev, 'ino': before[name].st_ino} for name in layers}}
    if not _valid_geometry(base) or not _valid_key_pin(context['key_base']):
        raise OpaqueFilesystemError(refusal)
    _chain_unchanged(qemu, avd, layers, before)
    return context


def _defer_guest(qemu, avd, layers, wanted):
    _capacity_marker(avd)
    _guest_capacity_marker(avd)
    context = _opaque_context(qemu, avd, layers, wanted)
    token = {'schema': GUEST_TOKEN_SCHEMA, 'avd': str(avd), 'name': avd.name.removesuffix('.avd'),
             'requested_bytes': wanted, 'nonce': uuid.uuid4().hex, **context}
    return {'changed': False, 'filesystem_bytes': None, 'metadata_encrypted': True,
            'guest_required': True, 'userdata_capacity_token': token}


def _verified_offline_backup(avd, folder, hashes):
    """Require the complete manager backup to match this stopped original chain."""
    refusal = 'Encrypted disk growth requires a matching complete offline userdata/key backup.'
    if folder is None:
        raise OfflineBackupRequiredError(refusal)
    folder = Path(folder).expanduser()
    receipt, data = folder / 'backup.json', folder / 'avd'
    if (folder.is_symlink() or not folder.is_dir() or data.is_symlink() or not data.is_dir()
            or receipt.is_symlink() or not receipt.is_file() or receipt.stat().st_nlink != 1
            or folder.resolve() == avd or avd in folder.resolve().parents):
        raise OpaqueFilesystemError(refusal)
    try:
        saved = json.loads(receipt.read_text())
        if (not isinstance(saved, dict) or saved.get('name') != avd.name.removesuffix('.avd')
                or not isinstance(saved.get('files'), dict) or set(hashes) != set(LAYER_NAMES)):
            raise ValueError('foreign backup')
        for name, checksum in hashes.items():
            path = data / name
            if (saved['files'].get('avd/' + name) != checksum or path.is_symlink()
                    or not path.is_file() or path.stat().st_nlink != 1 or digest(path) != checksum):
                raise ValueError('incomplete or mismatched backup')
        marker = data / 'qemu-version.txt'
        if (marker.is_symlink() or not marker.is_file() or marker.stat().st_nlink != 1
                or marker.read_bytes() != (avd / 'qemu-version.txt').read_bytes()):
            raise ValueError('missing backing version')
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise OpaqueFilesystemError(refusal) from error
    return str(folder.resolve())


def _grow_opaque_capacity(qemu, avd, layers, wanted, folder):
    """Extend only disk capacity; leave ciphertext and ext4 growth to the guest."""
    current = layers[DATA]['virtual-size']
    # This requires equal original raw/overlay capacities before any write.
    # A mismatched chain is not an authenticated encrypted-growth source.
    context = _opaque_context(qemu, avd, layers, current)
    if wanted <= current or wanted % context['base_filesystem']['block_size']:
        raise OpaqueFilesystemError('Encrypted disk growth requires a larger aligned capacity.')
    _capacity_marker(avd)
    _guest_capacity_marker(avd)
    marker = avd / 'qemu-version.txt'
    marker_bytes, marker_before = marker.read_bytes(), marker.stat()
    before = {name: (avd / name).stat() for name in layers}
    if marker_before.st_nlink != 1 or any(before[name].st_nlink != 1 for name in layers):
        raise OpaqueFilesystemError('Refused aliased userdata/key files before encrypted disk growth.')
    hashes = {name: digest(avd / name) for name in layers}
    external_backup = _verified_offline_backup(avd, folder, hashes)
    _chain_unchanged(qemu, avd, layers, before)
    names = (DATA, DATA + '.qcow2')
    allocated = sum(before[name].st_blocks * 512 for name in layers)
    if shutil.disk_usage(avd).free < allocated + wanted // 50 + 64 * 1024**2:
        raise RuntimeError('Insufficient free host storage for staged encrypted disk growth; originals were preserved.')
    pending = avd / PENDING
    pending.mkdir(mode=0o700)
    receipt = {'schema': SCHEMA, 'avd': str(avd), 'original_sha256': hashes,
               'requested_bytes': wanted, 'before': context['base_filesystem'],
               'mode': 'opaque-disk-capacity', 'previous_disk_bytes': current,
               'offline_backup': external_backup}
    try:
        (pending / 'original').mkdir()
        (pending / 'staged').mkdir()
        _json(pending / 'transaction.json', receipt)
    except BaseException:
        shutil.rmtree(pending)
        raise
    log = pending / 'commands.log'
    try:
        staged = pending / 'staged'
        # Copy the raw base alone, never flatten/decrypt the effective top.
        _run([qemu, 'convert', '-O', 'raw', avd / DATA, staged / DATA], log=log)
        shutil.copystat(avd / DATA, staged / DATA)
        shutil.copy2(avd / names[1], staged / names[1])
        _run([qemu, 'check', staged / names[1]], log=log)
        # Sparse conversion may change allocation without changing any byte.
        if _info(qemu, staged / DATA)['virtual-size'] != current or digest(staged / DATA) != hashes[DATA]:
            raise RuntimeError('Staged encrypted base differs from the complete original bytes.')
        _run([qemu, 'compare', avd / DATA, staged / DATA], log=log)
        # Identical QCOW2 bytes can inherit a different allocation map from
        # the sparsified raw base. Pin the overlay itself and compare sectors.
        if (_info(qemu, staged / names[1])['virtual-size'] != current
                or digest(staged / names[1]) != hashes[names[1]]):
            raise RuntimeError('Staged encrypted overlay differs from the complete original bytes.')
        _run([qemu, 'compare', avd / names[1], staged / names[1]], log=log)
        _run([qemu, 'resize', staged / DATA, wanted], log=log)
        _run([qemu, 'resize', staged / names[1], wanted], log=log)
        staged_layers = _layers(qemu, staged)
        if any(staged_layers[name]['virtual-size'] != wanted for name in names):
            raise RuntimeError('Staged encrypted disk capacity did not match the request.')
        if _prefix_digest(staged / DATA, current) != hashes[DATA]:
            raise RuntimeError('Staged encrypted disk changed original base bytes.')
        # Non-strict compare permits a larger device only when the extra
        # sectors read as zero; all sectors in the original range must match.
        _run([qemu, 'compare', avd / DATA, staged / DATA], log=log)
        _run([qemu, 'compare', avd / names[1], staged / names[1]], log=log)
        _run([qemu, 'check', staged / names[1]], log=log)
        for name in names:
            with (staged / name).open('rb') as stream:
                os.fsync(stream.fileno())
        for name in layers:
            if name.startswith('encryptionkey'):
                saved = pending / 'original' / name
                shutil.copy2(avd / name, saved)
                with saved.open('rb') as stream:
                    os.fsync(stream.fileno())
        _sync_directory(staged)
        _sync_directory(pending / 'original')
        _chain_unchanged(qemu, avd, layers, before)
        if (marker.is_symlink() or not marker.is_file() or marker.read_bytes() != marker_bytes or any(
                getattr(marker_before, field) != getattr(marker.stat(), field)
                for field in ('st_dev', 'st_ino', 'st_nlink', 'st_size', 'st_mtime_ns', 'st_ctime_ns'))):
            raise RuntimeError('Original backing version changed during staging; activation refused.')
        if any(digest(avd / name) != checksum for name, checksum in hashes.items()):
            raise RuntimeError('Original userdata/key bytes changed during staging; activation refused.')
        prepared = {name: digest(staged / name) for name in names}
        _json(pending / 'transaction.json', {**receipt, 'staged_sha256': prepared})
        _activate(avd, pending, names)
        final_layers = _layers(qemu, avd)
        result = _defer_guest(qemu, avd, final_layers, wanted)
        if (any(digest(avd / name) != prepared[name] for name in names)
                or any(digest(avd / name) != hashes[name] for name in layers if name.startswith('encryptionkey'))):
            raise RuntimeError('Activated encrypted disk or key bytes failed verification.')
        retained = avd / ('.userdata-resize-backup-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
        pending.rename(retained)
        _sync_directory(avd)
    except BaseException:
        _recover(avd)
        raise
    return {**result, 'changed': True, 'disk_capacity_bytes': wanted,
            'previous_disk_capacity_bytes': current, 'backup': external_backup,
            'transaction_backup': str(retained)}


def _remember_filesystem(qemu, avd, layers, geometry, *, base_sha256=None, publish=True):
    """Record measured plaintext capacity; never infer it from a virtual disk size."""
    current = _capacity_marker(avd)
    with tempfile.TemporaryDirectory(prefix='.userdata-base-probe-', dir=avd) as temporary:
        try:
            base = _geometry(qemu, avd / DATA, Path(temporary) / 'superblock.img')
        except OpaqueFilesystemError:
            return
    if base != geometry:
        return
    key = 'encryptionkey.img'
    pin = ({'sha256': digest(avd / key), 'virtual_size': layers[key]['virtual-size']}
           if key in layers else _template_key_pin(qemu, avd))
    receipt = {'schema': VERIFIED_SCHEMA, 'filesystem': geometry,
               'data_base_sha256': base_sha256 or digest(avd / DATA), 'key_base': pin}
    if publish and current != receipt:
        _json(avd / VERIFIED, receipt)
    return receipt


def _opaque_continuity(qemu, avd, layers, wanted):
    """Allow historical prepared capacity to start, without resizing ciphertext."""
    refusal = ('Encrypted userdata needs guest-scoped capacity verification/repair; '
               'host filesystem tools cannot resize it. No userdata or keys were changed.')
    required = set(LAYER_NAMES)
    if (set(layers) != required or any(layers[name]['virtual-size'] != wanted
            for name in (DATA, DATA + '.qcow2'))):
        raise OpaqueFilesystemError(refusal)
    marker = avd / 'qemu-version.txt'
    if marker.is_symlink() or not marker.is_file() or marker.read_text().strip() != '2':
        raise OpaqueFilesystemError(refusal)
    before = {name: (avd / name).stat() for name in layers}
    with tempfile.TemporaryDirectory(prefix='.userdata-base-probe-', dir=avd) as temporary:
        base = _geometry(qemu, avd / DATA, Path(temporary) / 'superblock.img')
    proofs = []
    try:
        saved = _capacity_marker(avd)
        guest = _guest_capacity_marker(avd)
    except RuntimeError as error:
        raise OpaqueFilesystemError(refusal) from error
    if (guest is not None and guest['requested_bytes'] == wanted
            and guest['base_filesystem'] == base
            and json.dumps(guest['base_filesystem'], sort_keys=True) == json.dumps(base, sort_keys=True)):
        context = _opaque_context(qemu, avd, layers, wanted)
        if (context['data_base_sha256'] == guest['data_base_sha256']
                and context['key_base'] == guest['key_base']
                and context['layer_identity'] == guest['layer_identity']):
            _chain_unchanged(qemu, avd, layers, before)
            return {'changed': False, 'filesystem_bytes': None, 'prepared_filesystem_bytes': wanted,
                    'uuid': guest['filesystem']['uuid'], 'metadata_encrypted': True,
                    'capacity_proof': 'verified-in-guest'}
        # A completed proof binds immutable bases; do not fall back to an
        # unrelated prepared proof if those bases have changed.
        raise OpaqueFilesystemError(refusal)
    if base['bytes'] != wanted:
        raise OpaqueFilesystemError(refusal)
    if saved is not None and saved['filesystem'] == base:
        proofs.append((saved.get('filesystem'), saved.get('data_base_sha256'), saved.get('key_base')))
    else:
        # Older installers already retain a completed, verified activation
        # receipt. Only this post-activation directory can supply continuity.
        for directory in sorted(avd.glob('.userdata-resize-backup-*'), reverse=True):
            path = directory / 'transaction.json'
            if directory.is_symlink() or not directory.is_dir() or path.is_symlink() or not path.is_file():
                continue
            saved = json.loads(path.read_text())
            if (not isinstance(saved, dict) or saved.get('schema') != SCHEMA
                    or type(saved.get('requested_bytes')) is not int or saved['requested_bytes'] != wanted):
                continue
            hashes = saved.get('original_sha256', {})
            staged = saved.get('staged_sha256', {})
            if not isinstance(hashes, dict) or not isinstance(staged, dict):
                continue
            key = hashes.get('encryptionkey.img')
            if 'key_base' in saved:
                pin = saved['key_base']
            else:
                pin = ({'sha256': key, 'virtual_size': layers['encryptionkey.img']['virtual-size']}
                       if key else _template_key_pin(qemu, avd, require_manifest=True))
            proofs.append((saved.get('after'), staged.get(DATA), pin))
    eligible = [(checksum, pin) for geometry, checksum, pin in proofs
                if geometry == base and json.dumps(geometry, sort_keys=True) == json.dumps(base, sort_keys=True)
                and isinstance(checksum, str) and len(checksum) == 64
                and isinstance(pin, dict) and set(pin) == {'sha256', 'virtual_size'}
                and isinstance(pin['sha256'], str) and len(pin['sha256']) == 64
                and type(pin['virtual_size']) is int and pin['virtual_size'] > 0]
    if not eligible:
        raise OpaqueFilesystemError(refusal)
    data_hash, key_hash = digest(avd / DATA), digest(avd / 'encryptionkey.img')
    if not any(checksum == data_hash and pin['sha256'] == key_hash
               and all(layers[name]['virtual-size'] == pin['virtual_size'] for name in
                       ('encryptionkey.img', 'encryptionkey.img.qcow2')) for checksum, pin in eligible):
        raise OpaqueFilesystemError(refusal)
    assert_offline(avd)
    if _layers(qemu, avd) != layers or any(
            (getattr(before[name], field) != getattr((avd / name).stat(), field))
            for name in layers for field in ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')):
        raise OpaqueFilesystemError('Userdata/key chain changed during continuity verification; start refused.')
    return {'changed': False, 'filesystem_bytes': None, 'prepared_filesystem_bytes': wanted,
            'uuid': base['uuid'], 'metadata_encrypted': True,
            'capacity_proof': 'verified-before-encryption'}


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


def _resize_userdata(sdk, avd, wanted, *, allow_guest=False, backup=None):
    """Grow the effective ext4 and rebuild its coherent raw/QCOW2 chain offline."""
    avd = Path(avd).resolve()
    if type(wanted) is not int or wanted <= 0 or type(allow_guest) is not bool:
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
        try:
            geometry = _geometry(qemu, top, Path(temporary) / 'superblock.img')
        except OpaqueFilesystemError:
            if DATA + '.qcow2' not in layers:
                raise
            if allow_guest and any(layers[name]['virtual-size'] < wanted for name in data_names):
                return _grow_opaque_capacity(qemu, avd, layers, wanted, backup)
            try:
                return _opaque_continuity(qemu, avd, layers, wanted)
            except OpaqueFilesystemError:
                if allow_guest:
                    return _defer_guest(qemu, avd, layers, wanted)
                raise
            except (OSError, ValueError, TypeError, KeyError) as error:
                raise OpaqueFilesystemError('Encrypted userdata has no valid capacity proof; '
                    'guest-scoped verification/repair is required. Original data was preserved.') from error
    if geometry['bytes'] > layers[data_names[-1]]['virtual-size'] or geometry['bytes'] > wanted:
        raise RuntimeError('Ext4 exceeds its virtual disk; userdata was preserved for inspection.')
    if wanted % geometry['block_size']:
        raise RuntimeError('Requested userdata size is not aligned to its ext4 block size.')
    _capacity_marker(avd)
    _guest_capacity_marker(avd)
    if geometry['bytes'] == wanted and all(layers[name]['virtual-size'] == wanted for name in data_names):
        _remember_filesystem(qemu, avd, layers, geometry)
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
        prepared_hashes = {name: digest(pending / 'staged' / name) for name in data_names}
        _json(pending / 'transaction.json', {**receipt, 'after': after,
              'staged_sha256': prepared_hashes})
        _activate(avd, pending, data_names)
        _layers(qemu, avd)
        final = _geometry(qemu, avd / data_names[-1], pending / 'final-superblock.img')
        if final != after or any(digest(avd / name) != hashes[name] for name in layers if name.startswith('encryptionkey')):
            raise RuntimeError('Activated userdata filesystem or encryption keys failed verification.')
        proof = _remember_filesystem(qemu, avd, _layers(qemu, avd), after,
                                     base_sha256=prepared_hashes[DATA], publish=False)
        if proof is None:
            raise RuntimeError('Activated filesystem capacity proof failed verification.')
        completed = json.loads((pending / 'transaction.json').read_text())
        _json(pending / 'transaction.json', {**completed, 'key_base': proof['key_base']})
        backup = avd / ('.userdata-resize-backup-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
        pending.rename(backup)
        _sync_directory(avd)
    except BaseException:
        _recover(avd)
        raise
    try:
        _capacity_marker(avd)
        _json(avd / VERIFIED, proof)
    except (OSError, RuntimeError):
        # The durable completed backup already contains this capacity/key
        # proof. A redundant marker publication error is not a failed resize.
        print('Capacity marker unavailable; verified proof retained in the completed resize backup.', flush=True)
    print(f'Userdata ext4 grew from {geometry["bytes"] / 1024**3:g} to {wanted / 1024**3:g} GiB; original chain: {backup}', flush=True)
    return {'changed': True, 'filesystem_bytes': wanted, 'uuid': after['uuid'], 'backup': str(backup)}


@contextmanager
def _resize_lock(avd):
    avd = Path(avd)
    if avd.is_symlink():
        raise RuntimeError('Refused a symlink userdata directory.')
    avd = avd.resolve()
    if not avd.is_dir():
        yield avd
        return
    lock = avd / '.userdata-resize.lock'
    if lock.is_symlink() or lock.exists() and not lock.is_file():
        raise RuntimeError('Refused a foreign userdata resize lock.')
    with lock.open('a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Another userdata resize is working on this AVD.') from None
        yield avd


def resize_userdata(sdk, avd, wanted, *, allow_guest=False, backup=None):
    """Grow offline, or explicitly prepare/defer an opaque chain for the guest.

    A same-size deferral changes no images. Disk-only encrypted growth requires
    explicit allow_guest and a matching complete offline backup. Neither path
    claims filesystem capacity: pass its token to the owned guest helper.
    """
    with _resize_lock(avd) as folder:
        return _resize_userdata(sdk, folder, wanted, allow_guest=allow_guest, backup=backup)


def publish_guest_capacity(sdk, avd, wanted, verified_guest_record):
    """Publish an internal guest-helper result after that guest has stopped.

    This API is a trust boundary for the caller, not a JSON import interface.
    The caller must pass the record returned by userdata_guest unchanged.
    Raw bases and the original four layer identities must survive that boot;
    writable overlay bytes may legitimately change. Disk-container growth
    preserves original ciphertext; only the guest grows the decrypted ext4.
    """
    with _resize_lock(avd) as folder:
        return _publish_guest_capacity(sdk, folder, wanted, verified_guest_record)


def _publish_guest_capacity(sdk, avd, wanted, record):
    refusal = 'Guest capacity proof did not match the prepared stopped userdata/key chain; no proof was published.'
    if type(wanted) is not int or wanted <= 0 or not avd.is_dir():
        raise RuntimeError(refusal)
    fields = GUEST_IDENTITY_FIELDS | {'schema', 'requested_bytes', 'filesystem', 'verified', 'changed',
                                     'capacity_proof', 'host_token', 'probe_verified', 'backup'}
    if (not isinstance(record, dict) or set(record) != fields or record['schema'] != GUEST_RECORD_SCHEMA
            or type(record['requested_bytes']) is not int or record['requested_bytes'] != wanted
            or record['verified'] is not True or type(record['changed']) is not bool
            or record['capacity_proof'] != 'verified-in-guest' or not _valid_geometry(record['filesystem'])
            or record['filesystem']['bytes'] != wanted
            or not _valid_guest_identity({key: record[key] for key in GUEST_IDENTITY_FIELDS}, wanted)
            or (record['changed'] and (record['probe_verified'] is not True
                or not isinstance(record['backup'], str) or not Path(record['backup']).is_absolute()))
            or (not record['changed'] and (record['probe_verified'] is not None or record['backup'] is not None))):
        raise RuntimeError(refusal)
    token = record['host_token']
    if (not isinstance(token, dict) or set(token) != {'schema', 'avd', 'name', 'requested_bytes', 'nonce',
            'base_filesystem', 'data_base_sha256', 'key_base', 'layer_identity'}
            or token['schema'] != GUEST_TOKEN_SCHEMA or token['avd'] != str(avd)
            or token['name'] != avd.name.removesuffix('.avd') or token['name'] != record['name']
            or type(token['requested_bytes']) is not int or token['requested_bytes'] != wanted
            or not isinstance(token['nonce'], str) or re.fullmatch('[0-9a-f]{32}', token['nonce']) is None
            or not _valid_geometry(token['base_filesystem']) or token['base_filesystem']['bytes'] > wanted
            or not isinstance(token['data_base_sha256'], str)
            or re.fullmatch('[0-9a-f]{64}', token['data_base_sha256']) is None
            or not _valid_key_pin(token['key_base']) or not _valid_layer_identity(token['layer_identity'])):
        raise RuntimeError(refusal)
    assert_offline(avd)
    if (avd / PENDING).exists() or (avd / PENDING).is_symlink():
        raise RuntimeError('Finish userdata resize recovery before publishing a guest capacity proof.')
    _capacity_marker(avd)
    _guest_capacity_marker(avd)
    qemu = Path(sdk) / 'emulator/qemu-img'
    layers = _layers(qemu, avd)
    before = {name: (avd / name).stat() for name in layers}
    context = _opaque_context(qemu, avd, layers, wanted)
    if any(context[key] != token[key] for key in context):
        raise RuntimeError(refusal)
    # The attestation only applies to an effective opaque device. Plaintext
    # already has a direct host measurement and cannot adopt this guest proof.
    with tempfile.TemporaryDirectory(prefix='.userdata-guest-probe-', dir=avd) as temporary:
        try:
            _geometry(qemu, avd / (DATA + '.qcow2'), Path(temporary) / 'superblock.img')
        except OpaqueFilesystemError:
            pass
        else:
            raise RuntimeError(refusal)
    proof = {'schema': GUEST_VERIFIED_SCHEMA, 'requested_bytes': wanted,
             'base_filesystem': context['base_filesystem'], 'data_base_sha256': context['data_base_sha256'],
             'key_base': context['key_base'], 'filesystem': record['filesystem'],
             'layer_identity': context['layer_identity'],
             'guest': {key: record[key] for key in sorted(GUEST_IDENTITY_FIELDS)}}
    _chain_unchanged(qemu, avd, layers, before)
    _json(avd / GUEST_VERIFIED, proof)
    return {'changed': False, 'filesystem_bytes': None, 'prepared_filesystem_bytes': wanted,
            'uuid': record['filesystem']['uuid'], 'metadata_encrypted': True,
            'capacity_proof': 'verified-in-guest'}
