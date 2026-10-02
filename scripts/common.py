"""Shared host discovery and isolation rules for HyperOS-AVD."""
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import socket
import subprocess
import urllib.request

REPO_ROOT = Path(__file__).resolve().parent.parent
ROOT = Path(os.environ.get('HYPEROS_AVD_WORKSPACE', REPO_ROOT)).expanduser().resolve()
DEFAULT_NAME = 'HyperOS_3_API_36'
DEFAULT_PORT = 5566
OS4_NAME = 'HyperOS_4_Official_API_37'
OS4_PORT = 5574
OS4_SOURCE = 'official-hongkong-ota'
KSU_VERSION = 'v3.3.0'
KSU_ASSETS = {
    'ksud-aarch64-apple-darwin': '40ca97a2fb61284129909abac5325dcae790736d9b88901f4f31cc7ec6d9a705',
    'ksud-aarch64-linux-android': '8614de6cdc2233c71fd0d1c64381ea10fbe6658651bae9b5a8dab4fe08e6344b',
    'lkm-aarch64-android15-6.6_kernelsu.ko': 'c31d994aaf285e7bf4cf1ec38c2bbf2d7f303d1a4a7d616405bcd9f850d684e5',
    'lkm-aarch64-android16-6.12_kernelsu.ko': '877286f81d500c4ec546c96e9718c186b7379573c97ba5d5a35dd9a91465d076',
    'KernelSU_v3.3.0_32601-release.apk': 'c197060ecb89702e7d54a4c95e29cf5e8d97369bbbb436979ab7fd6bcde7b077',
}


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(8 * 1024**2), b''):
            digest.update(block)
    return digest.hexdigest()


def host_check():
    if platform.system() != 'Darwin' or platform.machine() != 'arm64':
        raise RuntimeError('This release supports Apple Silicon macOS only.')


def sdk_path(explicit=None):
    candidates = [explicit, os.environ.get('ANDROID_SDK_ROOT'), os.environ.get('ANDROID_HOME'),
                  Path.home() / 'Library/Android/sdk']
    for candidate in candidates:
        if candidate:
            path = Path(candidate).expanduser().resolve()
            if (path / 'emulator/emulator').is_file() and (path / 'platform-tools/adb').is_file():
                return path
    raise RuntimeError('Install Android Studio Emulator and Platform-Tools, or set ANDROID_SDK_ROOT.')


def avd_home():
    if os.environ.get('ANDROID_AVD_HOME'):
        return Path(os.environ['ANDROID_AVD_HOME']).expanduser()
    return Path(os.environ.get('ANDROID_EMULATOR_HOME', os.environ.get('ANDROID_USER_HOME',
                str(Path.home() / '.android')))).expanduser() / 'avd'


def tool(name, formula):
    found = shutil.which(name)
    if found:
        return found
    for prefix in (Path('/opt/homebrew'), Path('/usr/local')):
        for directory in ('bin', 'sbin'):
            path = prefix / 'opt' / formula / directory / name
            if path.is_file():
                return str(path)
    raise RuntimeError(f'Missing {name}. Install it with: brew install {formula}')


def port_free(port):
    # Check console and ADB endpoints, without contacting any guest.
    for endpoint in (port, port + 1):
        with socket.socket() as probe:
            probe.settimeout(1)
            if probe.connect_ex(('127.0.0.1', endpoint)) == 0:
                raise RuntimeError(f'Port {endpoint} is occupied. No emulator was stopped.')


def firmware_idle(fallback_port=DEFAULT_PORT):
    ports = set()
    for name in ('runtime.json', 'instances.json'):
        path = ROOT / 'local' / name
        if path.exists():
            data = json.loads(path.read_text())
            ports.update([data['port']] if name == 'runtime.json' else data.values())
    for port in ports or {fallback_port}:
        port_free(port)


def fetch_ksu(names):
    cache = ROOT / 'tools'
    cache.mkdir(exist_ok=True)
    for name in names:
        path = cache / name
        if not path.exists():
            url = f'https://github.com/tiann/KernelSU/releases/download/{KSU_VERSION}/{name}'
            temporary = path.with_suffix(path.suffix + '.part')
            print('Downloading official KernelSU asset:', name, flush=True)
            urllib.request.urlretrieve(url, temporary)
            if sha256(temporary) != KSU_ASSETS[name]:
                temporary.unlink()
                raise RuntimeError('KernelSU download checksum mismatch: ' + name)
            temporary.replace(path)
        if sha256(path) != KSU_ASSETS[name]:
            raise RuntimeError('KernelSU checksum mismatch: ' + name)
        if name.startswith('ksud-'):
            path.chmod(0o755)
    return cache


def runtime():
    path = ROOT / 'local/runtime.json'
    if path.exists():
        config = json.loads(path.read_text())
        config['sdk'] = str(sdk_path(config.get('sdk')))
        return config
    return {'sdk': str(sdk_path()), 'name': DEFAULT_NAME, 'port': DEFAULT_PORT}


def adb(config, *arguments, **kwargs):
    # There is deliberately no global server reset or implicit device selection.
    return subprocess.run([str(Path(config['sdk']) / 'platform-tools/adb'),
                           '-s', f"emulator-{config['port']}", *arguments], **kwargs)
