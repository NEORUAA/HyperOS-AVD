#!/usr/bin/env python3
"""Redirect verified Xiaomi Weather MGL libraries to a private ANGLE display."""
import hashlib
from pathlib import Path
import subprocess

from common import REPO_ROOT, sha256

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
EMPTY = hashlib.sha256(b'').hexdigest()


def patch(name, data):
    before, after = LIBRARIES[name]
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
    # The feature workaround is tied to this exact ANGLE revision.
    for name, checksum in ANGLE.items():
        if sha256(folder / name) != checksum:
            raise RuntimeError('Unsupported system ANGLE revision: ' + name)
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
