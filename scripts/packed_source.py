"""Prepare isolated candidates from the current verified packed firmware."""
from contextlib import contextmanager
import json
from pathlib import Path
import re
import tempfile

from common import sha256
from lp_image import pack, read_lp, unpack


def source_info(root, expected_source=None):
    """Reject mixed source identities and any present stale image receipt."""
    root = Path(root)
    path = root / 'local/build.json'
    try:
        info = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise RuntimeError('A packed candidate requires a verified OS4 build receipt.') from error
    if not isinstance(info, dict) or expected_source is not None and info.get('source') != expected_source:
        raise RuntimeError('Packed candidate firmware source does not match this operation.')
    if info.get('source') == 'official-hongkong-ota':
        from phone_profile import profile_from_build
        selected = profile_from_build(info)
        if info.get('archive_sha256') != selected['archive_sha256']:
            raise RuntimeError('Expected the source-pinned official hongkong image.')
    elif info.get('source') == 'official-yingtian-ota':
        from os4_pad import PROFILE, SOURCE
        expected = {'source': SOURCE, 'device': PROFILE['device'], 'hyperos': PROFILE['hyperos'],
                    'archive_sha256': PROFILE['source_archive_sha256'], 'display': PROFILE['display'],
                    'model_xml_sha256': PROFILE['model_xml_sha256'],
                    'identity_source_sha256': PROFILE['source_sha256']}
        if any(info.get(key) != value for key, value in expected.items()):
            raise RuntimeError('Expected the source-pinned official yingtian image.')
    else:
        raise RuntimeError('A packed candidate requires an owned official OS4 build.')
    packed = root / 'images/system.img'
    digest = sha256(packed)
    if 'system_sha256' in info and info['system_sha256'] != digest:
        raise RuntimeError('Source packed image differs from its build receipt.')
    receipts = {path: path.read_bytes()}
    installed = root / 'local/installed-release.json'
    if installed.exists() or installed.is_symlink():
        try:
            data = installed.read_bytes()
            manifest = json.loads(data)
            entry = manifest['files']['images/system.img']
            verified = (manifest.get('project') == 'HyperOS-AVD'
                        and entry['sha256'] == digest
                        and ('size' not in entry or entry['size'] == packed.stat().st_size))
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
            raise RuntimeError('Invalid installed packed-image receipt.') from error
        if not verified:
            raise RuntimeError('Source packed image differs from its installed receipt.')
        receipts[installed] = data
    else:
        receipts[installed] = None
    return info, packed, digest, receipts


class PackedCandidate:
    def __init__(self, source, source_hash, receipts, work, destination, names):
        self.source, self.source_hash, self.receipts = source, source_hash, receipts
        self.work, self.destination, self.names = work, destination, names
        self.raw = work / 'accepted/system.img'

    def unchanged(self):
        if sha256(self.source) != self.source_hash or any(
                (path.exists() or path.is_symlink()) if data is None else not path.is_file() or path.read_bytes() != data
                for path, data in self.receipts.items()):
            raise RuntimeError('Source firmware or receipt changed during candidate preparation.')

    def finish(self, raw, metadata):
        """Publish only a verified candidate, retaining every logical partition."""
        raw = Path(raw)
        if self.work not in raw.resolve().parents:
            raise RuntimeError('Candidate raw image must remain inside its isolated staging directory.')
        self.unchanged()
        packed = self.work / 'system.img'
        inputs = [(name, raw if name == 'system' else self.work / 'accepted' / (name + '.img'))
                  for name in self.names]
        pack(self.source, packed, inputs)
        self.unchanged()
        manifest = {**metadata, 'source_packed_sha256': self.source_hash,
                    'source_raw_sha256': sha256(self.raw), 'raw_sha256': sha256(raw),
                    'system_sha256': sha256(packed), 'logical_partitions': self.names}
        result = self.work / 'result'
        result.mkdir()
        raw.rename(result / 'hyperos-system.img')
        packed.rename(result / 'system.img')
        (result / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
        self.unchanged()
        if self.destination.exists() or self.destination.is_symlink():
            raise RuntimeError('Candidate output already exists; choose or remove an owned candidate first.')
        result.rename(self.destination)
        return self.destination / 'system.img'


@contextmanager
def packed_candidate(root, destination, expected_source=None):
    """Unpack all accepted LP inputs in metadata order and clean temporary trees."""
    requested = Path(destination)
    if requested.exists() or requested.is_symlink():
        raise RuntimeError('Candidate output already exists; choose or remove an owned candidate first.')
    root, destination = Path(root).resolve(), requested.resolve()
    if destination.exists() or destination.is_symlink():
        raise RuntimeError('Candidate output already exists; choose or remove an owned candidate first.')
    _, source, digest, receipts = source_info(root, expected_source)
    _, partitions = read_lp(source)
    names = [entry['name'] for entry in partitions]
    if (any(not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_]{1,35}', name) for name in names)
            or len(names) != len(set(names)) or 'system' not in names):
        raise RuntimeError('Unsupported or ambiguous packed logical partition names.')
    # The owned emulator format repacks read-only LP entries. Slot, disabled
    # and unknown attributes must never be silently normalized by pack().
    if any(type(entry.get('attributes')) is not int or entry['attributes'] != 1
           for entry in partitions):
        raise RuntimeError('Unsupported packed partition attributes; source image preserved.')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.' + destination.name + '-', dir=destination.parent) as temporary:
        work = Path(temporary)
        unpack(source, work / 'accepted')
        candidate = PackedCandidate(source, digest, receipts, work, destination, names)
        candidate.unchanged()
        yield candidate
