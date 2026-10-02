#!/usr/bin/env python3
"""Create an empty ext4 userdata template; never package a running AVD's data."""
from pathlib import Path
import subprocess

from common import ROOT, tool


def create(path=None):
    path = Path(path) if path is not None else ROOT / 'images/userdata.img'
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.img.part')
    with temporary.open('wb') as output:
        output.truncate(6 * 1024**3)
    subprocess.run([tool('mke2fs', 'e2fsprogs'), '-F', '-t', 'ext4', '-b', '4096', '-I', '256',
                    '-O', 'encrypt,quota,project,casefold', '-E', 'encoding=utf8',
                    '-L', 'data', str(temporary)], check=True, capture_output=True)
    temporary.replace(path)
    print('Created empty 6 GiB userdata template.')
    return path


if __name__ == '__main__':
    create()
