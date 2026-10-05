#!/usr/bin/env python3
"""Redirect verified Xiaomi Weather MGL libraries to a private ANGLE display."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import uuid

from common import REPO_ROOT, ROOT, sha256

LIBRARIES = {
    'libhyper_opengl.so': ('d35b0a3ca29bc08e4822a70705eea3db846e3cf5a48a7cf01bc0baf78a93bcce',
                         '8d4562cf050c01369e5a2c764ebe1594230b06f4d5b40ec899727a2023fbbc50'),
    'libmglnative.so': ('241623acab6ae56379701adf9ae542fb67cbd7e475e40463e3336dd229171cc5',
                      '99c5e15d91d01c148b072c98e1aa96363a620524ac2f43aa0c869867efa13fe5'),
}
ANGLE = {
    'libEGL_angle.so': 'ce0dba9be7b69be54a1e64ade4aec71645b276175c2c143973bdd7f0512a5dc9',
    'libGLESv2_angle.so': 'efd2a33a303f8f4271afe49536db3f13360f1eb340025c74b322ff8a05d0e999',
}
APK_SHA256 = '017d613137c0512a937dc6b8565da856099e988feab47b9eabc5c3d0a13f06e6'
PAD_APK_SHA256 = '50f5dd5a06818f9613bf92ae2861beb58426895e6ae86364ed472e4c0aec5652'
PAD_LIBRARIES = {
    'libhyper_opengl.so': ('ab39421d8b046cee3ebf7265e949a69c5820cbda4e1be3c77b5380a6fbc3c993',
                         '8a9d12020e4cf576487927eff94d30fb6bdabfbe8d4309de69711bfa666c90e9'),
    'libmglnative.so': LIBRARIES['libmglnative.so'],
}
PAD_APK = '/product/data-app/MIUIWeather/MIUIWeather.apk'
PAD_NATIVE = '/data/app-lib/MIUIWeather/arm64'
EMPTY = hashlib.sha256(b'').hexdigest()
BRIDGE_SOURCE_SHA256 = 'db47707f4fe9a6c0af7b230bc2fc7b815f0b0de0979f8da0f0a0ce2df03bfe59'
BRIDGE_SHA256 = 'bfaad823f56f9aa0c7b78a3d52bdbae26195a7ec8e04e1f686d4da577835a1cc'


def bridge_prebuilt_receipt():
    """Return the exact portable receipt for the audited private ANGLE bridge."""
    return {'revision': 1, 'source_sha256': BRIDGE_SOURCE_SHA256,
            'files': {**ANGLE, 'libhgl.so': BRIDGE_SHA256}}


def verify_bridge_prebuilt(folder):
    """Require complete source and payload pins before accepting a release build."""
    folder = Path(folder)
    source = REPO_ROOT / 'native/weather_angle.c'
    if (not source.is_file() or source.is_symlink()
            or sha256(source) != BRIDGE_SOURCE_SHA256):
        raise RuntimeError('Weather bridge source differs from its verified release build.')
    if not folder.is_dir() or folder.is_symlink():
        raise RuntimeError('Invalid precompiled Weather bridge directory.')
    receipt = folder / 'receipt.json'
    if not receipt.is_file() or receipt.is_symlink():
        raise RuntimeError('Missing verified precompiled Weather bridge receipt.')
    try:
        metadata = json.loads(receipt.read_text())
    except (OSError, ValueError) as error:
        raise RuntimeError('Invalid precompiled Weather bridge receipt.') from error
    expected = bridge_prebuilt_receipt()
    if (metadata != expected or not isinstance(metadata, dict)
            or type(metadata.get('revision')) is not int):
        raise RuntimeError('Unknown precompiled Weather bridge receipt.')
    for name, checksum in expected['files'].items():
        path = folder / name
        if not path.is_file() or path.is_symlink() or sha256(path) != checksum:
            raise RuntimeError('Precompiled Weather bridge payload changed: ' + name)
    return expected


def copy_bridge_prebuilt(source, destination):
    """Publish a verified library atomically without reusing a loaded inode."""
    source, destination = Path(source), Path(destination)
    if source.resolve() == destination.resolve():
        return destination
    temporary = destination.with_name(destination.name + '.stage-' + uuid.uuid4().hex)
    try:
        with source.open('rb') as original, temporary.open('xb') as output:
            shutil.copyfileobj(original, output)
        if sha256(temporary) != BRIDGE_SHA256:
            raise RuntimeError('Precompiled Weather bridge copy verification failed.')
        temporary.chmod(0o644)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def profile(source):
    """Select only the two audited signed Weather workloads."""
    if source == 'official-hongkong-ota':
        return {'id': 'phone-hongkong', 'apk_sha256': APK_SHA256, 'libraries': LIBRARIES}
    if source == 'official-yingtian-ota':
        return {'id': 'tablet-yingtian', 'apk_sha256': PAD_APK_SHA256,
                'libraries': PAD_LIBRARIES, 'apk': PAD_APK, 'native': PAD_NATIVE}
    raise RuntimeError('Unsupported Weather firmware profile.')


def patch(name, data, libraries=LIBRARIES):
    if libraries not in (LIBRARIES, PAD_LIBRARIES) or name not in libraries:
        raise RuntimeError('Unsupported Weather MGL library profile: ' + name)
    before, after = libraries[name]
    actual = hashlib.sha256(data).hexdigest()
    if actual == after:
        return data
    if actual != before:
        raise RuntimeError('Unsupported Weather MGL library: ' + name)
    # Equal-size substitutions in the verified ELF dynamic string table.
    # Both dependencies now resolve through libhgl's ANGLE dependencies.
    for original in (b'libEGL.so\0', b'libGLESv3.so\0'):
        if data.count(original) != 1:
            raise RuntimeError('Unexpected Weather ELF dependency: ' + name)
        data = data.replace(original, b'libhgl.so\0'.ljust(len(original), b'\0'))
    if hashlib.sha256(data).hexdigest() != after:
        raise RuntimeError('Weather MGL output checksum mismatch: ' + name)
    return data


def build_bridge(config, folder):
    folder = Path(folder)
    # The feature workaround is tied to this exact ANGLE revision.
    for name, checksum in ANGLE.items():
        if sha256(folder / name) != checksum:
            raise RuntimeError('Unsupported system ANGLE revision: ' + name)
    prebuilt = ROOT / 'tools/weather-angle'
    if ((prebuilt.exists() or prebuilt.is_symlink())
            and (not prebuilt.is_dir() or prebuilt.is_symlink())):
        raise RuntimeError('Invalid precompiled Weather bridge directory.')
    # An ANGLE-only cache remains a valid source-build input. Once either the
    # bridge or its receipt exists, require the complete precompiled bundle.
    if any((prebuilt / name).exists() or (prebuilt / name).is_symlink()
           for name in ('libhgl.so', 'receipt.json')):
        verify_bridge_prebuilt(prebuilt)
        return copy_bridge_prebuilt(prebuilt / 'libhgl.so', folder / 'libhgl.so')
    compilers = list((Path(config['sdk']) / 'ndk').glob('*/toolchains/llvm/prebuilt/darwin-*/bin/aarch64-linux-android36-clang'))
    if not compilers:
        raise RuntimeError('Install Android NDK (API 36+) in SDK Manager to build the Weather bridge.')
    compiler = max(compilers, key=lambda p: tuple(int(v) for v in p.parents[5].name.split('.')))
    destination = folder / 'libhgl.so'
    subprocess.run([str(compiler), str(REPO_ROOT / 'native/weather_angle.c'),
                    '-shared', '-fPIC', '-O2', '-Wl,-soname,libhgl.so',
                    '-Wl,-z,max-page-size=16384', '-Wl,--no-as-needed',
                    *[str(folder / name) for name in ANGLE], '-o', str(destination)], check=True)
    return destination
