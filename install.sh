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

# BEGIN EMBEDDED VERIFIED HTTPS TRANSPORT
"""Use verified macOS HTTPS when Python's separate CA store cannot verify TLS.

The bootstrap embeds this exact source before the installer is available.
Never import all keychain certificates as anchors or disable certificate checks.
"""
import http.client as _tls_http
import io as _tls_io
import os as _tls_os
import platform as _tls_platform
import ssl as _tls_ssl
import subprocess as _tls_subprocess
import tempfile as _tls_tempfile
import urllib.error as _tls_error
import urllib.parse as _tls_parse
import urllib.request as _tls_request


def _certificate_failure(error):
    if isinstance(error, _tls_error.URLError):
        error = error.reason
    return isinstance(error, _tls_ssl.SSLCertVerificationError)


class _MacHTTPSResponse:
    """Stream curl headers and body without buffering release archives in RAM."""
    def __init__(self, request, timeout):
        self.url = request.full_url
        self.timeout = timeout
        self.closed = False
        self.finished = False
        self.process = None
        self.stderr = _tls_tempfile.TemporaryFile()
        args = ['/usr/bin/curl', '--disable', '--silent', '--show-error', '--fail',
                '--location', '--globoff', '--max-redirs', '10', '--proto', '=https',
                '--proto-redir', '=https', '--connect-timeout', str(timeout),
                '--speed-limit', '1', '--speed-time', str(timeout),
                '--dump-header', '-']
        # Explicit CA files must retain their authority over native system trust.
        if _tls_os.environ.get('SSL_CERT_FILE'):
            args += ['--cacert', _tls_os.environ['SSL_CERT_FILE']]
        # Python also discovers macOS system proxies; curl only discovers env.
        proxies = _tls_request.getproxies()
        proxy = proxies.get('https') or proxies.get('all')
        if proxy:
            args += ['--proxy', proxy]
        host = _tls_parse.urlsplit(self.url).hostname
        if 'no' in proxies:
            # Keep the complete exclusion list for CDN redirects, not just the
            # first hostname; --noproxy overrides curl's inherited NO_PROXY.
            args += ['--noproxy', proxies['no']]
        elif host and _tls_request.proxy_bypass(request.host):
            args += ['--noproxy', host]
        for name, value in request.header_items():
            if any(char in name + value for char in '\r\n\x00'):
                self.stderr.close()
                raise ValueError('Invalid HTTPS request header.')
            args += ['--header', name + ': ' + value]
        args += ['--url', self.url]
        try:
            self.process = _tls_subprocess.Popen(args, stdin=_tls_subprocess.DEVNULL,
                stdout=_tls_subprocess.PIPE, stderr=self.stderr)
            self._headers()
        except BaseException:
            self.close()
            raise

    def _failure(self):
        # curl errors can contain proxy credentials or signed redirect URLs.
        # Return only its stable exit code, never stderr or the command line.
        code = self.process.returncode
        return _tls_error.URLError('macOS HTTPS transfer failed (curl exit '
            + str(code) + '); TLS certificate and hostname verification remain enabled.')

    def _complete(self):
        if self.finished:
            return
        try:
            self.process.wait(timeout=self.timeout + 2)
        except _tls_subprocess.TimeoutExpired:
            raise _tls_error.URLError('macOS HTTPS transfer timed out.') from None
        self.finished = True
        if self.process.returncode:
            raise self._failure()

    def _headers(self):
        # --location emits each response header block, but only the final body.
        # Skip informational, CONNECT proxy and followed redirect responses.
        for _ in range(32):
            line = self.process.stdout.readline(65537)
            if not line:
                self._complete()
                raise _tls_error.URLError('Missing HTTPS response headers.')
            if len(line) > 65536 or not line.startswith(b'HTTP/'):
                raise _tls_error.URLError('Invalid HTTPS response status.')
            fields = line.rstrip(b'\r\n').split(b' ', 2)
            if len(fields) < 2 or not fields[1].isdigit():
                raise _tls_error.URLError('Invalid HTTPS response status.')
            status = int(fields[1])
            block = bytearray()
            for _ in range(100):
                header = self.process.stdout.readline(65537)
                block.extend(header)
                if not header or len(block) > 65536:
                    raise _tls_error.URLError('Invalid or oversized HTTPS response headers.')
                if header in (b'\r\n', b'\n'):
                    break
            else:
                raise _tls_error.URLError('Too many HTTPS response headers.')
            headers = _tls_http.parse_headers(_tls_io.BytesIO(block))
            if status < 200 or (status == 200 and len(fields) == 3
                    and fields[2].lower() == b'connection established'):
                continue
            if 300 <= status < 400 and headers.get('Location'):
                continue
            if not 200 <= status < 300:
                error = _tls_error.HTTPError(self.url, status, 'HTTPS request failed', headers, None)
                error.close()
                raise error
            self.status = status
            self.headers = headers
            return
        raise _tls_error.URLError('Too many HTTPS response header blocks.')

    def getcode(self):
        return self.status

    def read(self, size=-1):
        if self.closed:
            raise ValueError('HTTPS response is closed.')
        if size == 0:
            return b''
        data = self.process.stdout.read(size)
        if size < 0 or not data or len(data) < size:
            self._complete()
        return data

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            if self.process is not None and self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=2)
                except _tls_subprocess.TimeoutExpired:
                    self.process.kill()
                    try:
                        self.process.wait(timeout=2)
                    except _tls_subprocess.TimeoutExpired:
                        raise _tls_error.URLError('Could not reap owned macOS HTTPS transfer.') from None
        finally:
            if self.process is not None:
                self.process.stdout.close()
            self.stderr.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def _mac_certificate_response(value, timeout, error):
    request = value if isinstance(value, _tls_request.Request) else _tls_request.Request(value)
    if (not _certificate_failure(error) or _tls_platform.system() != 'Darwin'
            or _tls_parse.urlsplit(request.full_url).scheme != 'https'
            or request.get_method() != 'GET' or request.data is not None):
        raise error
    # SecureTransport does not reliably support OpenSSL hashed CA directories.
    # Never silently replace an explicit directory restriction with native CAs.
    if _tls_os.environ.get('SSL_CERT_DIR'):
        raise _tls_error.URLError('TLS verification failed with explicit SSL_CERT_DIR; '
            'macOS fallback cannot preserve that CA directory. Configure a trusted SSL_CERT_FILE.') from error
    if not _tls_os.path.isfile('/usr/bin/curl'):
        raise error
    return _MacHTTPSResponse(request, timeout)


def secure_urlopen(value, timeout=30):
    try:
        return _tls_request.urlopen(value, timeout=timeout)
    except (_tls_ssl.SSLCertVerificationError, _tls_error.URLError) as error:
        return _mac_certificate_response(value, timeout, error)


def secure_urlretrieve(url, filename):
    """Retain urllib's normal path and use the same verified streaming fallback."""
    try:
        return _tls_request.urlretrieve(url, filename)
    except (_tls_ssl.SSLCertVerificationError, _tls_error.URLError) as error:
        with _mac_certificate_response(url, 30, error) as response:
            with open(filename, 'wb') as output:
                while block := response.read(128 * 1024):
                    output.write(block)
            return str(filename), response.headers
# END EMBEDDED VERIFIED HTTPS TRANSPORT

API = os.environ.get('HYPEROS_AVD_RELEASE_API',
    'https://api.github.com/repos/NEORUAA/HyperOS-AVD/releases')
LIMIT = 64 * 1024**2


def remote_json(url):
    request = urllib.request.Request(url, headers={'User-Agent': 'HyperOS-AVD-bootstrap',
        'Accept': 'application/vnd.github+json'})
    with secure_urlopen(request, timeout=30) as response:
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
            with secure_urlopen(request, timeout=30) as source:
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
