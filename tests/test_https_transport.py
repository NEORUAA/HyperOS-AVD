"""Exercise verified macOS TLS fallback and installer streaming contracts."""
import hashlib
import io
import json
from pathlib import Path
import ssl
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import https_transport as transport
import manage
import package_release
import package_installer
import setup


def certificate_error():
    return urllib.error.URLError(ssl.SSLCertVerificationError(1, 'certificate verify failed'))


class Process:
    def __init__(self, data, code=0):
        self.stdout = io.BytesIO(data)
        self.returncode = None
        self.code = code
        self.terminated = False
        self.killed = False

    def wait(self, timeout):
        self.returncode = self.code
        return self.code

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


def reply(body=b'payload', status=200, headers=b'', code=0):
    return Process(b'HTTP/2 ' + str(status).encode() + b'\r\n' + headers + b'\r\n' + body, code)


class TransportTests(unittest.TestCase):
    def fallback(self, process):
        real_isfile = transport._tls_os.path.isfile
        return (patch.object(transport._tls_request, 'urlopen', side_effect=certificate_error()),
                patch.object(transport._tls_platform, 'system', return_value='Darwin'),
                patch.object(transport._tls_os.path, 'isfile',
                    side_effect=lambda path: True if path == '/usr/bin/curl' else real_isfile(path)),
                patch.object(transport._tls_subprocess, 'Popen', return_value=process),
                patch.dict(transport._tls_os.environ, {}, clear=True),
                patch.object(transport._tls_request, 'getproxies', return_value={}),
                patch.object(transport._tls_request, 'proxy_bypass', return_value=False))

    def run_fallback(self, process, callback):
        from contextlib import ExitStack
        with ExitStack() as stack:
            mocks = [stack.enter_context(item) for item in self.fallback(process)]
            return callback(mocks)

    def test_default_python_transport_is_retained(self):
        expected = object()
        with patch.object(transport._tls_request, 'urlopen', return_value=expected) as opened, \
                patch.object(transport._tls_subprocess, 'Popen') as native:
            self.assertIs(transport.secure_urlopen('https://example.test/', timeout=60), expected)
        opened.assert_called_once_with('https://example.test/', timeout=60)
        native.assert_not_called()

    def test_only_macos_https_certificate_failure_can_fallback(self):
        failures = [urllib.error.URLError('DNS failure'), TimeoutError('timeout'),
                    ssl.SSLError('handshake failure'),
                    urllib.error.HTTPError('https://example.test/', 403, 'Forbidden', {}, None)]
        for error in failures:
            with self.subTest(error=type(error).__name__), \
                    patch.object(transport._tls_request, 'urlopen', side_effect=error), \
                    patch.object(transport._tls_platform, 'system', return_value='Darwin'), \
                    patch.object(transport._tls_subprocess, 'Popen') as native:
                with self.assertRaises(type(error)):
                    transport.secure_urlopen('https://example.test/')
                native.assert_not_called()
                if hasattr(error, 'close'):
                    error.close()
        for platform, url in [('Linux', 'https://example.test/'), ('Darwin', 'http://example.test/'),
                              ('Darwin', 'file:///tmp/fixture')]:
            with self.subTest(platform=platform, url=url), \
                    patch.object(transport._tls_request, 'urlopen', side_effect=certificate_error()), \
                    patch.object(transport._tls_platform, 'system', return_value=platform), \
                    patch.object(transport._tls_subprocess, 'Popen') as native:
                with self.assertRaises(urllib.error.URLError):
                    transport.secure_urlopen(url)
                native.assert_not_called()

    def test_final_headers_skip_proxy_redirect_and_information_blocks(self):
        process = Process(b'HTTP/1.1 200 Connection established\r\n\r\n'
            b'HTTP/2 302\r\nLocation: https://cdn.example.test/archive\r\n\r\n'
            b'HTTP/2 103\r\nLink: fixture\r\n\r\n'
            b'HTTP/2 206\r\nContent-Range: bytes 2-6/7\r\n\r\nyload')
        request = urllib.request.Request('https://example.test/archive',
            headers={'User-Agent': 'HyperOS-test', 'Accept': 'application/json', 'Range': 'bytes=2-'})
        def check(mocks):
            with transport.secure_urlopen(request, timeout=60) as response:
                self.assertEqual(response.getcode(), 206)
                self.assertEqual(response.headers['Content-Range'], 'bytes 2-6/7')
                self.assertEqual(response.read(128 * 1024), b'yload')
            args = mocks[3].call_args.args[0]
            self.assertEqual(args[0], '/usr/bin/curl')
            self.assertEqual(args[1], '--disable')
            self.assertNotIn('--insecure', args)
            self.assertNotIn('-k', args)
            self.assertIn('--globoff', args)
            self.assertEqual(args[args.index('--proto') + 1], '=https')
            self.assertEqual(args[args.index('--proto-redir') + 1], '=https')
            self.assertNotIn('--max-time', args)
            for header in ('User-agent: HyperOS-test', 'Accept: application/json', 'Range: bytes=2-'):
                self.assertIn(header, args)
        self.run_fallback(process, check)

    def test_explicit_ca_file_and_proxy_are_preserved_without_error_disclosure(self):
        process = reply()
        def check(mocks):
            with patch.dict(transport._tls_os.environ, {'SSL_CERT_FILE': '/trusted/custom.pem'}), \
                    patch.object(transport._tls_request, 'getproxies',
                        return_value={'https': 'http://user:secret@proxy.test:8080'}):
                with transport.secure_urlopen('https://example.test/') as response:
                    self.assertEqual(response.read(1024), b'payload')
            args = mocks[3].call_args.args[0]
            self.assertEqual(args[args.index('--cacert') + 1], '/trusted/custom.pem')
            self.assertEqual(args[args.index('--proxy') + 1], 'http://user:secret@proxy.test:8080')
        self.run_fallback(process, check)

    def test_explicit_ca_directory_refuses_native_fallback(self):
        with patch.object(transport._tls_request, 'urlopen', side_effect=certificate_error()), \
                patch.object(transport._tls_platform, 'system', return_value='Darwin'), \
                patch.dict(transport._tls_os.environ, {'SSL_CERT_DIR': '/custom/anchors'}), \
                patch.object(transport._tls_subprocess, 'Popen') as native:
            with self.assertRaisesRegex(urllib.error.URLError, 'explicit SSL_CERT_DIR'):
                transport.secure_urlopen('https://example.test/')
            native.assert_not_called()

    def test_proxy_exclusions_remain_complete_across_cdn_redirect(self):
        process = Process(b'HTTP/2 302\r\nLocation: https://release-assets.githubusercontent.com/file\r\n\r\n'
                          b'HTTP/2 200\r\n\r\npayload')
        excluded = 'github.com,release-assets.githubusercontent.com'
        def check(mocks):
            with patch.object(transport._tls_request, 'getproxies',
                    return_value={'https': 'http://proxy.test:8080', 'no': excluded}), \
                    patch.object(transport._tls_request, 'proxy_bypass', return_value=True):
                with transport.secure_urlopen('https://github.com/file') as response:
                    self.assertEqual(response.read(1024), b'payload')
            args = mocks[3].call_args.args[0]
            self.assertEqual(args[args.index('--noproxy') + 1], excluded)
        self.run_fallback(process, check)

    def test_macos_system_proxy_bypass_receives_request_port(self):
        def check(mocks):
            with patch.object(transport._tls_request, 'proxy_bypass', return_value=True) as bypass:
                with transport.secure_urlopen('https://example.test:8443/') as response:
                    response.read(1024)
                bypass.assert_called_once_with('example.test:8443')
            args = mocks[3].call_args.args[0]
            self.assertEqual(args[args.index('--noproxy') + 1], 'example.test')
        self.run_fallback(reply(), check)

    def test_direct_setup_bounds_remote_manifest_before_parsing(self):
        response = io.BytesIO(b' ' * (2 * 1024**2 + 1))
        with patch.object(setup, 'secure_urlopen', return_value=response), \
                patch.object(setup.json, 'loads') as decoded:
            with self.assertRaisesRegex(RuntimeError, 'Oversized remote release manifest'):
                setup.read_manifest('https://example.test/manifest.json')
            decoded.assert_not_called()

    def test_real_untrusted_peer_and_http_errors_are_not_accepted(self):
        for process in [Process(b'', 60), reply(status=403), reply(status=302)]:
            def check(_):
                with self.assertRaises(urllib.error.URLError) as caught:
                    transport.secure_urlopen('https://example.test/')
                self.assertNotIn('secret', str(caught.exception))
                self.assertTrue(process.stdout.closed)
            self.run_fallback(process, check)

    def test_truncated_transfer_and_early_close_wait_for_owned_process(self):
        process = reply(b'partial', code=18)
        def broken(_):
            with self.assertRaisesRegex(urllib.error.URLError, 'curl exit 18'):
                with transport.secure_urlopen('https://example.test/') as response:
                    response.read(128 * 1024)
            self.assertTrue(process.stdout.closed)
        self.run_fallback(process, broken)
        process = reply(b'large body not consumed')
        def prefix(_):
            with transport.secure_urlopen('https://example.test/') as response:
                self.assertEqual(response.read(5), b'large')
            self.assertTrue(process.terminated)
            self.assertIsNotNone(process.returncode)
            self.assertTrue(process.stdout.closed)
        self.run_fallback(process, prefix)

    def test_timeout_reaps_owned_process_and_redacts_command_credentials(self):
        class StalledProcess(Process):
            waits = 0
            def wait(self, timeout):
                self.waits += 1
                if self.waits <= 2:
                    raise subprocess.TimeoutExpired(['curl', '--proxy', 'http://user:secret@proxy.test'], timeout)
                return super().wait(timeout)
        process = StalledProcess(b'HTTP/2 200\r\n\r\nshort')
        def check(_):
            with self.assertRaisesRegex(urllib.error.URLError, 'transfer timed out') as caught:
                with transport.secure_urlopen('https://example.test/') as response:
                    response.read(1024)
            self.assertNotIn('secret', str(caught.exception))
            self.assertTrue(caught.exception.__suppress_context__)
            self.assertTrue(process.terminated)
            self.assertTrue(process.killed)
            self.assertIsNotNone(process.returncode)
            self.assertTrue(process.stdout.closed)
        self.run_fallback(process, check)

    def test_urlretrieve_fallback_streams_file_and_verifies_process_success(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'part'
            process = reply(b'payload')
            def check(_):
                with patch.object(transport._tls_request, 'urlretrieve', side_effect=certificate_error()):
                    returned, _ = transport.secure_urlretrieve('https://example.test/file', target)
                self.assertEqual(returned, str(target))
                self.assertEqual(target.read_bytes(), b'payload')
                self.assertEqual(process.returncode, 0)
            self.run_fallback(process, check)

    def test_bootstrap_embeds_identical_transport_before_installer_download(self):
        source = (ROOT / 'install.sh').read_text()
        embedded = source.split('# BEGIN EMBEDDED VERIFIED HTTPS TRANSPORT\n', 1)[1].split(
            '# END EMBEDDED VERIFIED HTTPS TRANSPORT\n', 1)[0]
        self.assertEqual(embedded, (ROOT / 'scripts/https_transport.py').read_text())
        bootstrap = types.ModuleType('standalone_bootstrap')
        code = source.split("<<'PY'\n", 1)[1].rsplit('\nPY\n', 1)[0]
        exec(compile(code, str(ROOT / 'install.sh'), 'exec'), bootstrap.__dict__)
        def check(_):
            self.assertEqual(bootstrap.remote_json('https://example.test/releases'), [{'tag_name': 'installer-v1.2.1'}])
        self.run_fallback(reply(json.dumps([{'tag_name': 'installer-v1.2.1'}]).encode()), check)
        self.assertIn('scripts/https_transport.py', package_release.runtime_files())

    def test_installer_pins_helper_and_imports_it_from_packaged_scripts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            metadata = package_installer.package(root / 'release')
            helper = 'scripts/https_transport.py'
            with zipfile.ZipFile(root / 'release' / metadata['archive']['name']) as archive:
                data = archive.read('HyperOS-AVD/' + helper)
                self.assertEqual(hashlib.sha256(data).hexdigest(), metadata['files'][helper]['sha256'])
                archive.extractall(root / 'Package with spaces')
            unrelated = root / 'unrelated cwd';unrelated.mkdir()
            marker = unrelated / 'wrong-import'
            (unrelated / 'https_transport.py').write_text('from pathlib import Path\n'
                + 'Path(' + repr(str(marker)) + ').touch()\nraise RuntimeError("Wrong helper")\n')
            result = subprocess.run([sys.executable,
                str(root / 'Package with spaces/HyperOS-AVD/scripts/manage.py'), '--help'],
                cwd=unrelated, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(marker.exists())

    def test_manager_api_and_independent_checksum_share_tls_fallback(self):
        self.run_fallback(reply(b'[{"tag_name":"fixture"}]'),
                          lambda _: self.assertEqual(manage.remote_json('https://example.test/releases'),
                                                     [{'tag_name': 'fixture'}]))
        checksum = hashlib.sha256(b'manifest').hexdigest()
        release = {'tag_name': 'v0.2.3-a17-hyperos4-hongkong-r4', 'asset_map': {
            'manifest.json': {'browser_download_url': 'https://example.test/manifest.json', 'size': 8},
            'SHA256SUMS': {'browser_download_url': 'https://example.test/SHA256SUMS'}}}
        with tempfile.TemporaryDirectory() as directory:
            def check(mocks):
                manifest = {'version': release['tag_name'], 'parts': []}
                with patch.object(manage, 'download', return_value=Path(directory) / 'manifest.json') as download, \
                        patch.object(manage.setup, 'read_manifest', return_value=(manifest, Path(directory))):
                    manage.fetch_release(release, directory)
                self.assertEqual(download.call_args.args[3], checksum)
                args = mocks[3].call_args.args[0]
                self.assertEqual(args[args.index('--url') + 1], 'https://example.test/SHA256SUMS')
                self.assertIn('User-agent: HyperOS-AVD/' + manage.VERSION, args)
            self.run_fallback(reply((checksum + '  manifest.json\n').encode()), check)

    def test_installer_resumes_206_and_restarts_when_range_ignored(self):
        for status, headers, payload in [(206, b'Content-Range: bytes 2-6/7\r\n', b'yload'),
                                          (200, b'', b'payload')]:
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / 'asset'
                target.with_name('asset.partial').write_bytes(b'pa')
                def check(mocks):
                    manage.download('https://example.test/asset', target, 7,
                                    hashlib.sha256(b'payload').hexdigest())
                    self.assertEqual(target.read_bytes(), b'payload')
                    self.assertFalse(target.with_name('asset.partial').exists())
                    self.assertIn('Range: bytes=2-', mocks[3].call_args.args[0])
                self.run_fallback(reply(payload, status, headers), check)

    def test_invalid_range_and_broken_tls_stream_never_replace_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'asset';target.write_bytes(b'old cache')
            partial = target.with_name('asset.partial');partial.write_bytes(b'pa')
            def invalid(_):
                with self.assertRaisesRegex(RuntimeError, 'partial download response'):
                    manage.download('https://example.test/asset', target, 7,
                                    hashlib.sha256(b'payload').hexdigest())
            self.run_fallback(reply(b'payload', 206, b'Content-Range: bytes 1-6/7\r\n'), invalid)
            self.assertEqual(partial.read_bytes(), b'pa')
            self.assertEqual(target.read_bytes(), b'old cache')
            processes = [reply(b'partial', 200, code=18) for _ in range(3)]
            def broken(mocks):
                mocks[3].side_effect = processes
                with self.assertRaises(urllib.error.URLError):
                    manage.download('https://example.test/asset', target, 7,
                                    hashlib.sha256(b'payload').hexdigest())
            self.run_fallback(processes[0], broken)
            self.assertEqual(target.read_bytes(), b'old cache')


if __name__ == '__main__':
    unittest.main()
