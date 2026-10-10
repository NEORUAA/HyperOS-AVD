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
