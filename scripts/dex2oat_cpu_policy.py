#!/usr/bin/env python3
"""Deliver topology-aware OS4 ART properties without patching ART or app data."""
import hashlib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
POLICY = REPO / 'config/dex2oat_cpu_policy.sh'
SYSTEM_TARGET = '/system/etc/hyperos-dex2oat-cpu.sh'
CPU_PROPERTIES = ('dalvik.vm.default-dex2oat-cpu-set',) + tuple(
    'dalvik.vm.' + channel + '-cpu-set'
    for channel in ('dex2oat', 'boot-dex2oat', 'background-dex2oat', 'restore-dex2oat'))
THREAD_PROPERTIES = tuple('dalvik.vm.' + channel + '-threads'
                          for channel in ('dex2oat', 'boot-dex2oat',
                                          'background-dex2oat', 'restore-dex2oat'))

# The image route uses the project's existing enforcing init -> su bridge.
# These are the extra property types, not permission to modify ART executables.
SEPOLICY = b'''(allow su dalvik_config_prop (property_service (set)))
(allow su dalvik_dynamic_config_prop (property_service (set)))
(allow su dalvik_config_prop (file (read open getattr map)))
(allow su dalvik_dynamic_config_prop (file (read open getattr map)))
(allow su sysfs_devices_system_cpu (dir (read open getattr search)))
(allow su sysfs_devices_system_cpu (file (read open getattr)))
'''
BOOT_INIT = ('''\n# Bound every ART compilation priority to the guest CPU topology.
service hyperos-dex2oat-cpu /system/bin/sh ''' + SYSTEM_TARGET + '''
    user root
    group root system
    seclabel u:r:su:s0
    disabled
    oneshot

on post-fs
    exec_start hyperos-dex2oat-cpu

on post-fs-data
    exec_start hyperos-dex2oat-cpu

on property:ro.persistent_properties.ready=true
    exec_start hyperos-dex2oat-cpu

on property:sys.boot_completed=1
    exec_start hyperos-dex2oat-cpu
''').encode()


def policy_script():
    return POLICY.read_bytes()


def receipt():
    return {'schema': 1, 'delivery': 'shared-image-and-native-compat',
            'policy_sha256': hashlib.sha256(policy_script()).hexdigest(),
            'properties': list(CPU_PROPERTIES + THREAD_PROPERTIES)}


def image_replacements():
    """Return a small standalone script for the early init delivery route."""
    return {SYSTEM_TARGET.lstrip('/'): (policy_script(), 0o755, 'u:object_r:system_file:s0')}
