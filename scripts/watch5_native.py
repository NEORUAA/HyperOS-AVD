"""Supply isolated native support for the Watch5 emulator hardware services."""
from pathlib import Path
import struct
import subprocess


def needed(data):
    """Read DT_NEEDED from an ELF64 file without running foreign code."""
    if data[:6] != b'\x7fELF\x02\x01' or data[18:20] != b'\xb7\x00':
        raise RuntimeError('Expected a little-endian AArch64 ELF.')
    offset = struct.unpack_from('<Q', data, 32)[0]
    size, count = struct.unpack_from('<HH', data, 54)
    segments = [struct.unpack_from('<IIQQQQQQ', data, offset + i * size)
                for i in range(count)]
    dynamic = next((x for x in segments if x[0] == 2), None)
    if dynamic is None:
        return []
    entries = [struct.unpack_from('<qQ', data, k)
               for k in range(dynamic[2], dynamic[2] + dynamic[5], 16)]
    address = next((value for tag, value in entries if tag == 5), None)
    if address is None:
        return []
    strings = next(x[2] + address - x[3] for x in segments
                   if x[0] == 1 and x[3] <= address < x[3] + x[5])
    return [data[strings + value:].split(b'\0')[0].decode()
            for tag, value in entries if tag == 1]


def support(system, vendor, base_system):
    from common import ROOT, tool
    from build_image import debugfs, install
    if ROOT.name != 'watch5' or ROOT.parent.name != 'work':
        raise RuntimeError('Native support is restricted to the Watch5 workspace.')

    # OEM images are tightly sized; add room only in the disposable build copies.
    for image, minimum in ((system, 1536 * 1024**2), (vendor, 256 * 1024**2)):
        image = Path(image)
        if image.stat().st_size < minimum:
            with image.open('r+b') as output:
                output.truncate(minimum)
            subprocess.run([tool('resize2fs', 'e2fsprogs'), '-f', str(image),
                            str(minimum // 4096)], check=True, capture_output=True)

    def listing(image, path):
        names = {}
        for line in debugfs(image, 'ls -p ' + path).decode().splitlines():
            fields = line.split('/')
            if len(fields) >= 7 and fields[5] not in ('.', '..'):
                names[fields[5]] = fields[2]
        return names

    def mkdir(image, path, label):
        try:
            debugfs(image, 'stat ' + path)
        except RuntimeError:
            debugfs(image, 'mkdir ' + path, True)
            debugfs(image, 'set_inode_field ' + path + ' mode 040755', True)
            label_file = ROOT / 'work/native-dir-context'
            label_file.write_bytes(label.encode() + b'\0')
            debugfs(image, 'ea_set -f "' + str(label_file) + '" ' + path + ' security.selinux', True)

    label = 'u:object_r:system_lib_file:s0'
    for directory in ('/system/lib64', '/system/lib64/bootstrap'):
        mkdir(system, directory, label)
    loader = debugfs(base_system, 'cat /system/bin/bootstrap/linker64')
    needed(loader)
    install(system, '/system/bin/linker64', loader, '0100755', 'u:object_r:system_linker_exec:s0')
    install(system, '/system/bin/bootstrap/linker64', loader, '0100755', 'u:object_r:system_linker_exec:s0')
    vendor_names = listing(vendor, '/lib64')
    system_names = listing(base_system, '/system/lib64')
    bionic = {'libc.so', 'libdl.so', 'libm.so', 'libdl_android.so'}
    seen, added = set(), []

    def library(name):
        if name in seen:
            return
        seen.add(name)
        if name in bionic:
            data = debugfs(base_system, 'cat /system/lib64/bootstrap/' + name)
            install(system, '/system/lib64/bootstrap/' + name, data, '0100644', label)
            install(system, '/system/lib64/' + name, data, '0100644', label)
            install(vendor, '/lib64/' + name, data, '0100644', 'u:object_r:vendor_file:s0')
            added.append(name)
        elif name in vendor_names:
            data = debugfs(vendor, 'cat /lib64/' + name)
        elif name in system_names:
            if system_names[name].startswith('12'):
                raise RuntimeError('APEX symlink needs explicit resolution: ' + name)
            data = debugfs(base_system, 'cat /system/lib64/' + name)
            install(system, '/system/lib64/' + name, data, '0100644', label)
            added.append(name)
        else:
            raise RuntimeError('Unresolved hardware service dependency: ' + name)
        for child in needed(data):
            library(child)

    services = ('/bin/qemu-props', '/bin/dlkm_loader',
                '/bin/hw/android.hardware.security.keymint-service',
                '/bin/hw/android.hardware.atrace@1.0-service')
    for path in services:
        for name in needed(debugfs(vendor, 'cat ' + path)):
            library(name)
    for path in ('/etc/init/hw/init.ranchu.rc',
                 '/etc/init/android.hardware.security.keymint-service.rc',
                 '/etc/init/android.hardware.atrace@1.0-service.rc'):
        data = debugfs(vendor, 'cat ' + path)
        output = b''.join(line + (b'    stdio_to_kmsg\n' if line.startswith(b'service ') else b'')
                          for line in data.splitlines(keepends=True))
        install(vendor, path, output, '0100644', 'u:object_r:vendor_configs_file:s0', replace=True)
    return {'services': list(services), 'dependencies': sorted(seen),
            'added_system_libraries': sorted(added), 'loader': '/system/bin/linker64'}
