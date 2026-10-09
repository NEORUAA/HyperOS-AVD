#!/usr/bin/env python3
"""Select the verified system ANGLE driver for the original yingtian camera."""
import argparse
import shlex
import subprocess

from patch_outcome import UnsupportedPatch
from common import runtime
from apply_flutter_fix import official, root

SOURCE = 'official-yingtian-ota'
PACKAGE = 'com.android.camera'
APK = '/product/priv-app/MiuiCamera/MiuiCamera.apk'
PROPERTIES = {'ro.boot.hardware': 'ranchu', 'ro.product.device': 'yingtian',
              'ro.mi.os.version.incremental': 'OS4.0.15.0.XBMCNXM'}
HASHES = {
    APK: '70e32c772c406d2a0ccd21af23d001d26acbef4b46d07cee6b4b55cf1070e8c0',
    '/system/lib64/libEGL_angle.so':
        'f61e6dae0cf6ec9f3c2c6668b59df7c3b9e6b1eaea47b7bd6d30ba1a09695f4f',
    '/system/lib64/libGLESv2_angle.so':
        '1c83f375408a6c6cf5e247c1ff382fbe50ff3c97848e0ce85a1a13db70cc3fd7',
}
KEYS = ('angle_gl_driver_selection_pkgs', 'angle_gl_driver_selection_values',
        'angle_egl_features')
FEATURE = 'exposeES32ForTesting'


def _entries(value):
    if not isinstance(value, str):
        raise RuntimeError('Malformed ANGLE settings: expected a string.')
    if value in ('', 'null'):
        return []
    entries = value.split(',')
    if any(not entry or any(char.isspace() for char in entry) for entry in entries):
        raise RuntimeError('Malformed ANGLE settings: empty or whitespace entry.')
    return entries


def angle_settings(packages, values, features):
    """Preserve unrelated selections and reject ambiguous paired settings."""
    packages, values, features = map(_entries, (packages, values, features))
    if len(packages) != len(values) or len(set(packages)) != len(packages):
        raise RuntimeError('Malformed ANGLE paired settings.')
    if PACKAGE in packages:
        values[packages.index(PACKAGE)] = 'angle'
    else:
        packages.append(PACKAGE)
        values.append('angle')
    if FEATURE in features:
        first = features.index(FEATURE)
        features = [entry for index, entry in enumerate(features)
                    if entry != FEATURE or index == first]
    else:
        features.append(FEATURE)
    return tuple(','.join(entries) for entries in (packages, values, features))


def _read_settings(config):
    return tuple(root(config, 'settings get global ' + key) for key in KEYS)


def _restore_setting(config, key, value):
    if value == 'null':
        root(config, 'settings delete global ' + key)
    else:
        root(config, 'settings put global ' + key + ' ' + shlex.quote(value))


def install(config):
    """Change only three persistent settings after all firmware guards pass."""
    official(config, sources=(SOURCE,))
    for key, expected in PROPERTIES.items():
        if root(config, 'getprop ' + key) != expected:
            raise RuntimeError('Pad camera ANGLE fix refused this device: ' + key)
    if root(config, 'pm path ' + PACKAGE) != 'package:' + APK:
        raise UnsupportedPatch('Unsupported Pad camera update; no settings changed.')
    for path, expected in HASHES.items():
        actual = root(config, 'sha256sum ' + shlex.quote(path)).split()
        if not actual or actual[0] != expected:
            raise UnsupportedPatch('Unsupported Pad camera ANGLE input: ' + path)
    previous = _read_settings(config)
    desired = angle_settings(*previous)
    attempted = []
    try:
        for key, old, new in zip(KEYS, previous, desired):
            if old == new:
                continue
            # Include the failing write: it may have committed before an ADB
            # or verification error was reported to this process.
            attempted.append((key, old))
            root(config, 'settings put global ' + key + ' ' + shlex.quote(new))
        if attempted and _read_settings(config) != desired:
            raise RuntimeError('Pad camera ANGLE settings verification failed.')
    except Exception as error:
        failures = []
        for key, old in reversed(attempted):
            try:
                _restore_setting(config, key, old)
            except Exception as restore_error:
                failures.append(key + ': ' + str(restore_error))
        try:
            if _read_settings(config) != previous:
                failures.append('Original settings verification failed.')
        except Exception as verify_error:
            failures.append(str(verify_error))
        if failures:
            raise RuntimeError('Pad camera ANGLE update failed; rollback incomplete: '
                               + '; '.join(failures)) from error
        raise RuntimeError('Pad camera ANGLE update failed; original settings restored.') from error
    print('Pad camera ANGLE selection verified; original APK and native libraries preserved.',
          flush=True)
    return dict(zip(KEYS, desired))


if __name__ == '__main__':
    argparse.ArgumentParser(description=__doc__).parse_args()
    try:
        install(runtime())
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        raise SystemExit(str(error))
