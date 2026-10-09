"""Derive per-image platform pins without making the common module variant-specific."""
import hashlib
import json
from pathlib import Path
import tempfile

from common import sha256


def canonical(value):
    return (json.dumps(value, sort_keys=True, indent=2) + '\n').encode()


def _regular(path):
    if not path.is_file() or path.is_symlink() or path.stat().st_nlink != 1:
        raise RuntimeError('Refused an aliased platform context asset: ' + str(path))


def context_from_image(build, read):
    """Pin actual mounted source files; OTA names never select runtime behavior."""
    def pin(path, expected=None):
        checksum = hashlib.sha256(read(path)).hexdigest()
        if expected is not None and checksum != expected:
            raise RuntimeError('Platform image prerequisite differs: ' + path)
        return {'path': path, 'sha256': checksum}
    identity = [pin(path) for path in ('/system/build.prop', '/product/etc/build.prop')]
    result = {'schema': 1, 'identity_sources': identity, 'rear': None, 'boot_services': None}
    if build.get('rear_display_wake_fix') is not None:
        from apply_rear_display_fix import EXPECTED_WAKE_MANIFEST, validate_build
        from rear_display_config import MARKER_PATH
        marker = validate_build(build)
        wake = build['rear_display_wake_fix']
        if wake != EXPECTED_WAKE_MANIFEST:
            raise RuntimeError('Unknown rear-panel wake image prerequisite.')
        marker_data = (json.dumps(marker, indent=2) + '\n').encode()
        files = [pin('/' + MARKER_PATH, hashlib.sha256(marker_data).hexdigest()),
                 pin(wake['jar_path'], wake['jar_sha256']),
                 pin(wake['script_path'], wake['script_sha256']),
                 pin(marker['overlay_path'], marker['overlay_sha256'])]
        files += [pin(row['config'], row['config_sha256']) for row in marker['displays']]
        result['rear'] = {'files': files, 'display_id': wake['display_id'],
                          'group_id': wake['display_group_id'],
                          'unique_id': wake['display_unique_id'],
                          'launcher_path': wake['script_path']}
    services = build.get('boot_service_fix')
    if services is not None:
        from patch_boot_services import AFTER, TARGETS, PROBE_SHA256
        files = []
        for name, (path, _, _) in TARGETS.items():
            if services.get('targets', {}).get(name, {}).get('after') != AFTER[name]:
                raise RuntimeError('Unknown boot-service image prerequisite: ' + name)
            files.append(pin(path, AFTER[name]))
        if services.get('probe_sha256') != PROBE_SHA256:
            raise RuntimeError('Unknown kernel capability probe prerequisite.')
        result['boot_services'] = {'files': files,
                                  'probe': pin('/system/bin/hyperos_kernel_probe', PROBE_SHA256)}
    return result


def prepare(workspace):
    """Produce small verified context assets; remove extracted partitions immediately."""
    from packed_source import source_info
    from lp_image import read_lp, copy_range
    from build_image import erofs
    workspace = Path(workspace)
    build, packed, checksum, originals = source_info(workspace)
    destination = workspace / 'tools/os4-core'
    if destination.exists() or destination.is_symlink():
        return load(workspace)
    destination.parent.mkdir(parents=True, exist_ok=True)
    base, parts = read_lp(packed)
    matches = [row for row in parts if row['name'] == 'system']
    if len(matches) != 1:
        raise RuntimeError('Missing or ambiguous platform system partition.')
    row = matches[0]
    with tempfile.TemporaryDirectory(prefix='.core-context-', dir=destination.parent) as temporary:
        stage = Path(temporary)
        raw = stage / 'system.img'
        with packed.open('rb') as source, raw.open('xb') as output:
            for length, kind, offset, device in row['extents']:
                if kind != 0 or device != 0:
                    raise RuntimeError('Unsupported platform source extent.')
                copy_range(source, output, base + offset * 512, length * 512)
        context = context_from_image(build, lambda path: erofs(raw, path))
        raw.unlink()
        if sha256(packed) != checksum or any(
                (path.exists() or path.is_symlink()) if data is None else path.read_bytes() != data
                for path, data in originals.items()):
            raise RuntimeError('Platform image changed during context preparation.')
        (stage / 'context.json').write_bytes(canonical(context))
        (stage / 'receipt.json').write_bytes(canonical({'schema': 1,
            'source_packed_sha256': checksum,
            'context_sha256': sha256(stage / 'context.json')}))
        stage.rename(destination)
    return load(workspace)


def load(workspace):
    """Verify packaged context and provenance; the guest also verifies each file pin."""
    workspace = Path(workspace)
    directory = workspace / 'tools/os4-core'
    if not directory.is_dir() or directory.is_symlink():
        raise RuntimeError('Missing verified platform context; prepare the runtime bundle first.')
    for name in ('context.json', 'receipt.json'):
        _regular(directory / name)
    try:
        receipt = json.loads((directory / 'receipt.json').read_text())
        context = json.loads((directory / 'context.json').read_text())
        build = json.loads((workspace / 'local/build.json').read_text())
        expected = build.get('system_sha256')
        installed = workspace / 'local/installed-release.json'
        if installed.is_file():
            _regular(installed)
            image = json.loads(installed.read_text())['files']['images/system.img']['sha256']
            if expected is not None and expected != image:
                raise RuntimeError('Platform context image receipts disagree.')
            expected = image
        if expected is None:
            from packed_source import source_info
            expected = source_info(workspace)[2]
        if (set(receipt) != {'schema', 'source_packed_sha256', 'context_sha256'}
                or receipt['schema'] != 1 or not expected
                or receipt['source_packed_sha256'] != expected
                or receipt['context_sha256'] != sha256(directory / 'context.json')):
            raise RuntimeError('Platform context source or content changed.')
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise RuntimeError('Invalid platform context receipt.') from error
    from core_platform import context_files
    context_files(context)
    return context
