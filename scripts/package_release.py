#!/usr/bin/env python3
"""Package verified firmware into GitHub-sized parts, excluding all personal data."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import tarfile

from common import ROOT, fetch_ksu, sha256
from init_userdata import create as create_userdata


class PartsWriter:
    def __init__(self, directory, stem, limit):
        self.directory, self.stem, self.limit = directory, stem, limit
        self.parts, self.stream, self.used = [], None, 0
        self.digest = None

    def write(self, data):
        length = len(data)
        while data:
            if self.stream is None:
                name = f'{self.stem}.tar.gz.part{len(self.parts) + 1:03d}'
                self.stream = (self.directory / name).open('xb')
                self.digest = hashlib.sha256()
                self.parts.append({'name': name})
                self.used = 0
            chunk, data = data[:self.limit - self.used], data[self.limit - self.used:]
            self.stream.write(chunk)
            self.digest.update(chunk)
            self.used += len(chunk)
            if self.used == self.limit:
                self.close_part()
        return length

    def close_part(self):
        if self.stream:
            self.stream.close()
            self.parts[-1].update(size=self.used, sha256=self.digest.hexdigest())
            self.stream = None

    def flush(self):
        if self.stream:
            self.stream.flush()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', default='v0.1.0')
    parser.add_argument('--part-mib', type=int, default=1536)
    args = parser.parse_args()
    if not re.fullmatch(r'v[0-9A-Za-z_.-]+', args.version):
        raise RuntimeError('Invalid release version.')
    if not 1 <= args.part_mib < 2048:
        raise RuntimeError('Every GitHub release asset must be smaller than 2 GiB.')
    create_userdata()
    fetch_ksu(['ksud-aarch64-linux-android', 'KernelSU_v3.3.0_32601-release.apk'])
    directory = ROOT / 'releases' / args.version
    directory.mkdir(parents=True, exist_ok=False)
    # Deliberately whitelist firmware and two official runtime assets. Never read avd/.
    paths = sorted(path for path in (ROOT / 'images').rglob('*') if path.is_file())
    paths += [ROOT / 'tools' / name for name in
              ('ksud-aarch64-linux-android', 'KernelSU_v3.3.0_32601-release.apk')]
    files = {}
    for path in paths:
        if path.is_symlink():
            raise RuntimeError('Release files must not be symlinks.')
        relative = path.relative_to(ROOT).as_posix()
        files[relative] = {'size': path.stat().st_size, 'sha256': sha256(path)}
    writer = PartsWriter(directory, 'HyperOS-AVD-' + args.version + '-macos-arm64',
                         args.part_mib * 1024**2)
    try:
        with tarfile.open(fileobj=writer, mode='w|gz', compresslevel=6) as archive:
            for path in paths:
                relative = path.relative_to(ROOT).as_posix()
                entry = archive.gettarinfo(str(path), arcname=relative)
                entry.uid = entry.gid = 0
                entry.uname = entry.gname = ''
                entry.mtime = 0
                entry.mode = 0o644
                with path.open('rb') as source:
                    archive.addfile(entry, source)
                # Do not publish an image that changed during packaging.
                if sha256(path) != files[relative]['sha256']:
                    raise RuntimeError('Firmware changed while packaging: ' + relative)
    finally:
        writer.close_part()
    manifest = {'project': 'HyperOS-AVD', 'format': 1, 'version': args.version,
                'platform': 'macos-arm64', 'hyperos': '3.0.2.0.WMCCNXM', 'android_api': 36,
                'kernelsu': '3.3.0 (32601)',
                'gnss_patch': 'status-satellite-first-fix-async',
                'parts': writer.parts, 'files': files}
    (directory / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    (directory / 'SHA256SUMS').write_text(''.join(
        f'{entry["sha256"]}  {entry["name"]}\n' for entry in writer.parts)
        + sha256(directory / 'manifest.json') + '  manifest.json\n')
    print(json.dumps({'directory': str(directory), 'parts': writer.parts}, indent=2))


if __name__ == '__main__':
    main()
