"""Verify or grow ext4 through an owned guest's decrypted /data mapping.

Online growth cannot roll back guest writes. A verified offline backup is
required before growth; failures retain that backup and never restore it while
the guest is running. A timed-out resize may still be running in the guest.
"""
import copy
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shlex
import subprocess
import uuid

from common import adb, sha256
from userdata_resize import LAYER_NAMES, ext4_geometry

SCHEMA = 'hyperos-avd-guest-capacity-v1'
QUERY_TIMEOUT = 15
GROW_TIMEOUT = 120
DEVICE = re.compile(r'/dev/block/dm-[0-9]+\Z')
UUID = re.compile(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z')
IDENTITY_QUERY = '''id -u
getenforce
getprop ro.boot.qemu.avd_name
getprop ro.boot.hardware
getprop ro.product.device
getprop ro.mi.os.version.incremental
getprop sys.boot_completed
cat /proc/sys/kernel/random/boot_id
cat /proc/mounts'''


class GuestCapacityError(RuntimeError):
    """A failed verification; resize_attempted also covers uncertain timeouts."""
    def __init__(self, message, *, resize_attempted=False):
        super().__init__(message)
        self.resize_attempted = resize_attempted


def _config(config):
    if (not isinstance(config, dict) or type(config.get('port')) is not int
            or config['port'] % 2 or not 5554 <= config['port'] <= 5682
            or not isinstance(config.get('name'), str)
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', config['name'])
            or not config.get('sdk')):
        raise GuestCapacityError('Use an explicit SDK, AVD name and even Emulator port.')
    value = copy.deepcopy(config)
    serial = f"emulator-{value['port']}"
    if value.get('serial', serial) != serial:
        raise GuestCapacityError('ADB serial does not match the configured Emulator port.')
    value['serial'] = serial
    return value


def _adb(config, *arguments, binary=False, timeout=QUERY_TIMEOUT, attempted=False):
    try:
        result = adb(config, *arguments, capture_output=True, text=not binary, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as error:
        message = ('Guest filesystem resize timed out or failed to complete; it may still be running. '
                   'Keep the offline backup and inspect this guest before retrying.' if attempted
                   else 'Guest capacity verification could not complete.')
        raise GuestCapacityError(message, resize_attempted=attempted) from error
    if result.returncode:
        raise GuestCapacityError('Guest capacity command failed. Backup retained; no automatic live rollback.',
                                 resize_attempted=attempted)
    return result.stdout


def _root(config, command, *, binary=False, timeout=QUERY_TIMEOUT, attempted=False):
    return _adb(config, 'exec-out' if binary else 'shell',
                'su -W -c ' + shlex.quote('set -eu\n' + command),
                binary=binary, timeout=timeout, attempted=attempted)


def _identity_mount(config):
    if _adb(config, 'get-serialno').strip() != config['serial']:
        raise GuestCapacityError('Connected ADB serial differs from the requested guest.')
    lines = _root(config, IDENTITY_QUERY).splitlines()
    if (len(lines) < 9 or lines[0] != '0' or lines[1] != 'Enforcing'
            or lines[2] != config['name'] or lines[3] != 'ranchu'
            or not lines[4] or lines[6] != '1' or not UUID.fullmatch(lines[7])):
        raise GuestCapacityError('Guest identity, root, enforcing mode or completed boot was not verified.')
    for key, actual in (('product_device', lines[4]), ('incremental', lines[5])):
        if config.get(key) is not None and config[key] != actual:
            raise GuestCapacityError('Guest firmware identity differs from the requested instance.')
    mounts = [line.split() for line in lines[8:] if len(line.split()) >= 4 and line.split()[1] == '/data']
    if (len(mounts) != 1 or mounts[0][2] != 'ext4'
            or not DEVICE.fullmatch(mounts[0][0]) or 'rw' not in mounts[0][3].split(',')
            or 'ro' in mounts[0][3].split(',')):
        raise GuestCapacityError('Expected one writable ext4 /data mount on a decrypted dm device.')
    return {'name': lines[2], 'serial': config['serial'], 'hardware': lines[3],
            'product_device': lines[4], 'incremental': lines[5], 'boot_id': lines[7],
            'root_uid': 0, 'selinux': 'Enforcing', 'mounted_type': 'ext4',
            'mapped_device': mounts[0][0]}


def _guard(identity):
    """Recheck the same boot and mount inside every sensitive root command."""
    quote = shlex.quote
    return '\n'.join([
        'test "$(id -u)" = 0', 'test "$(getenforce)" = Enforcing',
        'test "$(getprop sys.boot_completed)" = 1',
        f'test "$(getprop ro.boot.qemu.avd_name)" = {quote(identity["name"])}',
        'test "$(getprop ro.boot.hardware)" = ranchu',
        f'test "$(getprop ro.product.device)" = {quote(identity["product_device"])}',
        f'test "$(getprop ro.mi.os.version.incremental)" = {quote(identity["incremental"])}',
        f'test "$(cat /proc/sys/kernel/random/boot_id)" = {quote(identity["boot_id"])}',
        'data_mounts=0',
        'while read -r source point type options rest; do',
        '    [ "$point" = /data ] || continue',
        f'    test "$source" = {quote(identity["mapped_device"])}',
        '    test "$type" = ext4',
        '    case ",$options," in *,ro,*) exit 1 ;; esac',
        '    case ",$options," in *,rw,*) ;; *) exit 1 ;; esac',
        '    data_mounts=$((data_mounts + 1))',
        'done < /proc/mounts', 'test "$data_mounts" = 1',
        f'test -b {quote(identity["mapped_device"])}',
    ])


def _snapshot(config, *, expected=None):
    identity = _identity_mount(config)
    if expected is not None and identity != expected:
        raise GuestCapacityError('Guest rebooted or its identity/decrypted mapping changed.')
    device = shlex.quote(identity['mapped_device'])
    size = _root(config, _guard(identity) + f'\n/system/bin/blockdev --getsize64 {device}').strip()
    if not re.fullmatch(r'[0-9]+', size):
        raise GuestCapacityError('Guest mapped-device capacity could not be verified.')
    header = _root(config, _guard(identity) + f'\ndd if={device} bs=4096 count=1 2>/dev/null', binary=True)
    if len(header) != 4096:
        raise GuestCapacityError('Guest decrypted superblock read was incomplete.')
    try:
        geometry = ext4_geometry(header)
    except RuntimeError as error:
        raise GuestCapacityError('Guest /data superblock is not a supported readable ext4 filesystem.') from error
    if _identity_mount(config) != identity:
        raise GuestCapacityError('Guest identity or decrypted mapping changed while reading its superblock.')
    return identity, int(size), geometry


def _backup(config, backup):
    if backup is None:
        raise GuestCapacityError('Online growth requires a verified offline userdata/key backup directory.')
    folder = Path(backup).expanduser()
    receipt = folder / 'backup.json'
    data = folder / 'avd'
    if (folder.is_symlink() or not folder.is_dir() or receipt.is_symlink() or not receipt.is_file()
            or data.is_symlink() or not data.is_dir()):
        raise GuestCapacityError('Offline userdata backup ownership was not verified.')
    try:
        saved = json.loads(receipt.read_text())
        if not isinstance(saved, dict) or saved.get('name') != config['name'] or not isinstance(saved.get('files'), dict):
            raise ValueError('foreign receipt')
        for name in LAYER_NAMES:
            relative = 'avd/' + name
            checksum = saved['files'].get(relative)
            path = folder / relative
            if (PurePosixPath(relative).as_posix() != relative or path.is_symlink() or not path.is_file()
                    or not isinstance(checksum, str) or not re.fullmatch('[0-9a-f]{64}', checksum)
                    or sha256(path) != checksum):
                raise ValueError('incomplete backup')
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise GuestCapacityError('Offline userdata/key backup is incomplete, foreign or failed its checksum.') from error
    return str(folder.resolve())


def _probe_check(config, identity, path, checksum, *, remove=False):
    probe = shlex.quote(path)
    command = (_guard(identity) + f'\ntest -f {probe}\ntest ! -L {probe}\n'
               f'test "$(stat -c %u {probe})" = 0\n'
               f'test "$(stat -c %a {probe})" = 600\nsha256sum {probe}')
    output = _root(config, command).split()
    if len(output) != 2 or output != [checksum, path]:
        raise GuestCapacityError('The root-owned userdata probe changed; backup and probe retained.')
    if remove:
        _root(config, _guard(identity) + f'\ntest ! -L {probe}\n'
              f'test "$(stat -c %u {probe})" = 0\n'
              f'test "$(sha256sum {probe} | cut -d " " -f 1)" = {checksum}\nrm {probe}')


def verify_and_grow(config, target_bytes, allow_grow=True, *, backup=None):
    """Return a guest capacity attestation only after exact superblock verification.

    An already-sized guest is read-only. Growth requires an offline four-layer
    backup; errors after resize starts never imply that online growth rolled back.
    The optional host token is copied unchanged for offline continuity binding.
    """
    config = _config(config)
    if type(target_bytes) is not int or target_bytes <= 0 or type(allow_grow) is not bool:
        raise GuestCapacityError('Use a positive integer capacity and a boolean growth policy.')
    identity, capacity, before = _snapshot(config)
    if capacity != target_bytes or before['bytes'] > target_bytes or target_bytes % before['block_size']:
        raise GuestCapacityError('Requested capacity does not match the mapped device, or would shrink userdata.')
    record = {'schema': SCHEMA, **identity, 'requested_bytes': target_bytes,
              'mapped_device_bytes': capacity, 'filesystem': before, 'verified': True,
              'changed': False, 'capacity_proof': 'verified-in-guest',
              'host_token': config.get('userdata_capacity_token'), 'probe_verified': None, 'backup': None}
    if before['bytes'] == target_bytes:
        return record
    if not allow_grow:
        raise GuestCapacityError('Guest ext4 is smaller than requested; online growth is disabled.')
    record['backup'] = _backup(config, backup if backup is not None else config.get('userdata_backup'))
    _, fresh_capacity, fresh = _snapshot(config, expected=identity)
    if fresh_capacity != capacity or fresh != before:
        raise GuestCapacityError('Guest filesystem changed before online growth; repair refused.')
    _root(config, _guard(identity) + '\ntest -f /system/bin/resize2fs\ntest -x /system/bin/resize2fs')
    nonce = uuid.uuid4().hex + uuid.uuid4().hex
    probe = '/data/local/tmp/hyperos-avd-userdata-probe-' + uuid.uuid4().hex
    checksum = hashlib.sha256((nonce + '\n').encode()).hexdigest()
    _root(config, _guard(identity) + '\ntest -d /data/local/tmp\ntest ! -L /data/local/tmp\n'
          f'if [ -e {probe} ] || [ -L {probe} ]; then exit 1; fi\n'
          f'umask 077\nprintf \'%s\\n\' {nonce} > {probe}\nchmod 600 {probe}')
    _probe_check(config, identity, probe, checksum)
    _, ready_capacity, ready = _snapshot(config, expected=identity)
    if ready_capacity != capacity or ready != before:
        raise GuestCapacityError('Guest superblock changed before resize; backup and probe retained.')
    try:
        device = shlex.quote(identity['mapped_device'])
        _root(config, _guard(identity) +
              f'\ntest "$(/system/bin/blockdev --getsize64 {device})" = {target_bytes}\n'
              f'/system/bin/resize2fs {device}',
              timeout=GROW_TIMEOUT, attempted=True)
        _, after_capacity, after = _snapshot(config, expected=identity)
        if (after_capacity != target_bytes or after['bytes'] != target_bytes
                or after['uuid'] != before['uuid'] or after['block_size'] != before['block_size']
                or after['incompatible'] != before['incompatible']):
            raise GuestCapacityError('Online resize did not preserve the expected ext4 geometry/UUID.')
        _probe_check(config, identity, probe, checksum, remove=True)
        _root(config, _guard(identity) + '\nsync')
    except GuestCapacityError as error:
        raise GuestCapacityError(str(error) + ' Online growth cannot be rolled back here; '
                                 'keep the offline backup and inspect this guest before retrying.',
                                 resize_attempted=True) from error
    record.update(filesystem=after, changed=True, probe_verified=True)
    return record
