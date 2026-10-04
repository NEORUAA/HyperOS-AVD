#!/bin/bash
# Bootstrap the stable installer; firmware is selected in its interactive menu.
set -euo pipefail
command -v python3 >/dev/null || { echo 'Python 3 is required / 需要 Python 3。' >&2; exit 1; }
python3 - "$@" <<'PY'
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import stat
import sys
import tempfile
import unicodedata
import urllib.request
import zipfile

API = os.environ.get('HYPEROS_AVD_RELEASE_API',
    'https://api.github.com/repos/NEORUAA/HyperOS-AVD/releases')
LIMIT = 64 * 1024**2


def remote_json(url):
    request = urllib.request.Request(url, headers={'User-Agent': 'HyperOS-AVD-bootstrap',
        'Accept': 'application/vnd.github+json'})
    with urllib.request.urlopen(request, timeout=30) as response:
        data = response.read(2 * 1024**2 + 1)
    if len(data) > 2 * 1024**2:
        raise RuntimeError('Remote metadata is too large.')
    return json.loads(data)


def latest(api=API):
    candidates = []
    for page in range(1, 11):
        rows = remote_json(api + f'?per_page=100&page={page}')
        if not isinstance(rows, list):
            raise RuntimeError('Invalid release list.')
        candidates.extend(r for r in rows if not r.get('draft') and not r.get('prerelease')
            and re.fullmatch(r'installer-v\d+\.\d+\.\d+', r.get('tag_name', '')))
        if len(rows) < 100:
            break
    if not candidates:
        raise RuntimeError('No stable Installer Release is published / 尚未发布正式安装器。')
    return max(candidates, key=lambda r: tuple(map(int, r['tag_name'][11:].split('.'))))


def metadata(release):
    assets = {a['name']: a for a in release.get('assets', [])}
    tag = release['tag_name']
    name = f'HyperOS-AVD-Installer-v{tag[11:]}-macos-arm64.zip'
    if 'installer.json' not in assets or name not in assets:
        raise RuntimeError('The installer release is missing required assets.')
    data = remote_json(assets['installer.json']['browser_download_url'])
    if not isinstance(data, dict):
        raise RuntimeError('Invalid installer metadata.')
    if (data.get('project') != 'HyperOS-AVD' or data.get('type') != 'installer'
            or data.get('schema') != 1 or data.get('platform') != 'macos-arm64'
            or data.get('tag') != tag or data.get('version') != tag[11:]
            or data.get('prerelease') is not False):
        raise RuntimeError('Installer metadata does not match the stable release.')
    archive = data.get('archive', {})
    if (archive.get('name') != name or type(archive.get('size')) is not int
            or not 0 < archive['size'] <= LIMIT
            or archive['size'] != assets[name].get('size')
            or not re.fullmatch(r'[0-9a-f]{64}', archive.get('sha256', ''))):
        raise RuntimeError('Invalid installer archive metadata.')
    files = data.get('files', {})
    if not isinstance(files, dict) or not {'Install.command', 'scripts/manage.py'} <= files.keys():
        raise RuntimeError('Incomplete installer file list.')
    for path, item in files.items():
        if not isinstance(path, str) or not isinstance(item, dict):
            raise RuntimeError('Invalid installer source metadata.')
        parts = PurePosixPath(path).parts
        if (not parts or PurePosixPath(path).as_posix() != path
                or PurePosixPath(path).is_absolute() or '..' in parts or '\\' in path
                or path == 'installer.json' or any(p in parts for p in ('images', 'avd', 'local', 'backups'))
                or type(item.get('size')) is not int or not 0 <= item['size'] <= LIMIT
                or not re.fullmatch(r'[0-9a-f]{64}', item.get('sha256', ''))):
            raise RuntimeError('Invalid installer source path or checksum.')
    return data, assets[name]['browser_download_url']


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024**2), b''):
            h.update(block)
    return h.hexdigest()


def fetch(url, target, info):
    if target.is_symlink():
        raise RuntimeError('Refused a symlink download target.')
    if target.is_file() and target.stat().st_size == info['size'] and digest(target) == info['sha256']:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as output:
            temporary = Path(output.name)
            request = urllib.request.Request(url, headers={'User-Agent': 'HyperOS-AVD-bootstrap'})
            with urllib.request.urlopen(request, timeout=30) as source:
                received = 0
                while block := source.read(128 * 1024):
                    received += len(block)
                    if received > info['size']:
                        raise RuntimeError('Installer download exceeds its declared size.')
                    output.write(block)
        if temporary.stat().st_size != info['size'] or digest(temporary) != info['sha256']:
            raise RuntimeError('Installer download checksum mismatch.')
        temporary.replace(target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def verify(folder, info):
    if folder.is_symlink():
        raise RuntimeError('Refused a symlink installer root.')
    for name, item in info['files'].items():
        path = folder / name
        if (path.is_symlink() or not path.resolve().is_relative_to(folder.resolve())
                or not path.is_file() or path.stat().st_size != item['size']
                or digest(path) != item['sha256']):
            raise RuntimeError('Existing installer differs; use another directory: ' + str(folder))
    internal = json.loads((folder / 'installer.json').read_text())
    if internal != {k: v for k, v in info.items() if k != 'archive'}:
        raise RuntimeError('Installer receipt mismatch.')


def extract(archive, destination, info):
    if destination.is_symlink():
        raise RuntimeError('Refused a symlink installer directory.')
    if destination.exists():
        verify(destination / 'HyperOS-AVD', info)
        return destination / 'HyperOS-AVD'
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.bootstrap-', dir=destination.parent) as temporary:
        staging = Path(temporary) / 'ready'
        staging.mkdir()
        with zipfile.ZipFile(archive) as bundle:
            expected = {'HyperOS-AVD/' + name for name in info['files']} | {'HyperOS-AVD/installer.json'}
            entries = bundle.infolist()
            if len(entries) != len(expected) or {p.filename for p in entries} != expected:
                raise RuntimeError('Unexpected or duplicate installer archive entries.')
            if sum(p.file_size for p in entries) > LIMIT:
                raise RuntimeError('Expanded installer is too large.')
            for entry in entries:
                if stat.S_ISLNK(entry.external_attr >> 16):
                    raise RuntimeError('Installer archive contains a symlink.')
                data = bundle.read(entry)
                path = staging / entry.filename
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                path.chmod(0o755 if path.name.endswith(('.command', '.sh')) else 0o644)
        verify(staging / 'HyperOS-AVD', info)
        staging.rename(destination)
    return destination / 'HyperOS-AVD'


def panel(rows):
    print('+' + '-' * 62 + '+')
    for row in rows:
        width = sum(2 if unicodedata.east_asian_width(c) in ('W', 'F') else 1 for c in row)
        print('| ' + row + ' ' * max(0, 60 - width) + ' |')
    print('+' + '-' * 62 + '+')


def main():
    if platform.system() != 'Darwin' or platform.machine() != 'arm64':
        raise RuntimeError('Apple Silicon macOS is required / 需要 Apple Silicon Mac。')
    # curl | bash and Python's source heredoc both consume stdin; reopen the TTY
    # for prompts, then inherit it when exec starts the downloaded installer.
    with open('/dev/tty', 'r') as terminal:
        os.dup2(terminal.fileno(), 0)
        sys.stdin = terminal
        panel(['HyperOS-AVD / Online installer', '[1] 中文    [2] English'])
        language = 'en' if input('Language / 语言 [1]: ').strip() == '2' else 'zh'
        zh = language == 'zh'
        default = str(Path.home() / 'HyperOS-AVD')
        prompt = '安装与下载目录' if zh else 'Installation and download directory'
        value = input(f'{prompt} [{default}]: ').strip() or default
        base = Path(value).expanduser().resolve()
        if base == Path('/') or base.is_file():
            raise RuntimeError('Choose a directory for HyperOS-AVD.')
        print('正在获取正式安装器...' if zh else 'Fetching the stable installer...', flush=True)
        release = latest()
        info, url = metadata(release)
        folder = base / 'installer'
        folder.mkdir(parents=True, exist_ok=True)
        with (folder / '.bootstrap.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            cached = base / 'downloads/installer' / info['archive']['name']
            fetch(url, cached, info['archive'])
            installed = extract(cached, folder / info['tag'], info)
        print(info['tag'] + ' -> ' + str(installed), flush=True)
        environment = dict(os.environ, HYPEROS_AVD_HOME=str(base))
        os.execve('/bin/zsh', ['zsh', str(installed / 'Install.command'), '--language', language], environment)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit('\nCancelled / 已取消。')
    except (OSError, ValueError, KeyError, RuntimeError, zipfile.BadZipFile) as error:
        raise SystemExit('Bootstrap failed / 启动失败: ' + str(error))
PY
