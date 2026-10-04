#!/usr/bin/env python3
"""Package the standalone stable installer without firmware or personal state."""
import argparse
import json
from pathlib import Path
import zipfile

from common import REPO_ROOT, sha256
from manage import VERSION
from package_release import runtime_files


def package(directory, version=VERSION):
    import re
    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise RuntimeError('Use a semantic installer version.')
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    paths = runtime_files()
    paths['docs/installing.md'] = REPO_ROOT / 'docs/installing.md'
    files = {name: {'size': p.stat().st_size, 'sha256': sha256(p)} for name, p in paths.items()}
    archive = directory / f'HyperOS-AVD-Installer-v{version}-macos-arm64.zip'
    metadata = {'project': 'HyperOS-AVD', 'type': 'installer', 'schema': 1,
                'version': version, 'tag': 'installer-v' + version, 'platform': 'macos-arm64',
                'prerelease': False, 'files': files}
    with zipfile.ZipFile(archive, 'x', compression=zipfile.ZIP_DEFLATED) as zip:
        for relative, path in paths.items():
            entry = zipfile.ZipInfo('HyperOS-AVD/' + relative)
            entry.external_attr = (0o100755 if relative.endswith('.command') else 0o100644) << 16
            entry.compress_type = zipfile.ZIP_DEFLATED
            zip.writestr(entry, path.read_bytes())
            if sha256(path) != files[relative]['sha256']:
                raise RuntimeError('Installer source changed during packaging: ' + relative)
        zip.writestr('HyperOS-AVD/installer.json', json.dumps(metadata, indent=2) + '\n')
    metadata['archive'] = {'name': archive.name, 'size': archive.stat().st_size, 'sha256': sha256(archive)}
    manifest = directory / 'installer.json'
    manifest.write_text(json.dumps(metadata, indent=2) + '\n')
    (directory / 'SHA256SUMS').write_text(''.join(sha256(p) + '  ' + p.name + '\n' for p in (archive, manifest)))
    print('Prepared stable installer: ' + str(directory))
    return metadata


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    package(args.output or REPO_ROOT / 'releases' / ('installer-v' + VERSION))
