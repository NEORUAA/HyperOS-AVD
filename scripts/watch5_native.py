"""Supply isolated native support for the Watch5 emulator hardware services."""
from pathlib import Path
import hashlib
import io
import json
import re
import struct
import subprocess
import zipfile


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


def needed32(data):
    """Read ARM32 dependencies without mixing in ARM64 or host libraries."""
    if data[:6] != b'\x7fELF\x01\x01' or data[18:20] != b'\x28\x00':
        raise RuntimeError('Expected a little-endian ARM32 ELF.')
    offset = struct.unpack_from('<I', data, 28)[0]
    size, count = struct.unpack_from('<HH', data, 42)
    segments = [struct.unpack_from('<IIIIIIII', data, offset + i * size)
                for i in range(count)]
    dynamic = next((x for x in segments if x[0] == 2), None)
    if dynamic is None:
        return []
    entries = [struct.unpack_from('<iI', data, k)
               for k in range(dynamic[1], dynamic[1] + dynamic[4], 8)]
    address = next((value for tag, value in entries if tag == 5), None)
    if address is None:
        return []
    strings = next(x[1] + address - x[2] for x in segments
                   if x[0] == 1 and x[2] <= address < x[2] + x[4])
    return [data[strings + value:].split(b'\0')[0].decode()
            for tag, value in entries if tag == 1]


def diagnostics(vendor, system_ext):
    """Expose selected early service failures without disabling SELinux."""
    from build_image import debugfs, install
    policy_path = '/etc/selinux/vendor_sepolicy.cil'
    policy = debugfs(vendor, 'cat ' + policy_path)
    for domain in ('hal_keymint_default', 'hal_atrace_default', 'dlkm_loader',
                   'qemu_props', 'bpfloader', 'shell'):
        rule = f'(allow {domain} kmsg_debug_device (chr_file (write)))\n'.encode()
        if rule not in policy:
            policy += rule
    for rule in (b'(allow init logcat_exec (file (getattr read open execute map)))\n',
                 b'(allow shell logcat_exec (file (read open getattr execute map entrypoint)))\n'):
        if rule not in policy:
            policy += rule
    install(vendor, policy_path, policy, '0100644',
            'u:object_r:vendor_configs_file:s0', replace=True)
    ranchu_path = '/etc/init/hw/init.ranchu.rc'
    ranchu = debugfs(vendor, 'cat ' + ranchu_path)
    restore = b'on early-init\n    restorecon /dev/kmsg_debug\n'
    if restore not in ranchu:
        install(vendor, ranchu_path, ranchu.replace(b'on early-init\n', restore, 1),
                '0100644', 'u:object_r:vendor_configs_file:s0', replace=True)
    rc_path = '/etc/init/init.watch5_avd.rc'
    rc = debugfs(system_ext, 'cat ' + rc_path)
    action = b'\non early-init\n    restorecon /dev/kmsg_debug\n'
    if action not in rc:
        install(system_ext, rc_path, rc + action, '0100644',
                'u:object_r:system_file:s0', replace=True)
        rc += action
    logcat = (b'\nservice watch5-early-logcat /system/bin/logcat -T 1 -b main -b system -b crash -v threadtime derive_classpath:W vold_prepare_subdirs:W *:I\n'
              b'    class core\n    user root\n    group root log logd\n'
              b'    seclabel u:r:shell:s0\n    stdio_to_kmsg\n    disabled\n'
              b'\non init\n    start watch5-early-logcat\n')
    if b'service watch5-early-logcat ' not in rc:
        install(system_ext, rc_path, rc + logcat, '0100644',
                'u:object_r:system_file:s0', replace=True)
    elif b'service watch5-early-logcat /system/bin/logcat -b ' in rc:
        install(system_ext, rc_path, rc.replace(
            b'service watch5-early-logcat /system/bin/logcat -b ',
            b'service watch5-early-logcat /system/bin/logcat -T 1 -b '),
            '0100644', 'u:object_r:system_file:s0', replace=True)
    contexts_path = '/etc/selinux/vendor_file_contexts'
    contexts = debugfs(vendor, 'cat ' + contexts_path)
    metadata = b'/dev/block/vdd[0-9]* u:object_r:metadata_block_device:s0\n'
    if metadata not in contexts:
        contexts += metadata
    install(vendor, contexts_path, contexts, '0100644',
            'u:object_r:vendor_configs_file:s0', replace=True)


def system_closure(system, base_system, roots, apex_libraries=None):
    """Resolve system dependencies within the system linker namespace."""
    from build_image import debugfs, install
    bionic = {'libc.so', 'libdl.so', 'libm.so', 'libdl_android.so'}
    seen = set()

    def library(name):
        if name in seen:
            return
        seen.add(name)
        directory = '/system/lib64/bootstrap/' if name in bionic else '/system/lib64/'
        if apex_libraries and name in apex_libraries:
            data = apex_libraries[name]
        else:
            data = debugfs(base_system, 'cat ' + directory + name)
        children = needed(data)
        target = '/system/lib64/' + name
        try:
            debugfs(system, 'stat ' + target)
            replace = True
        except RuntimeError:
            replace = False
        install(system, target, data, '0100644', 'u:object_r:system_lib_file:s0', replace=replace)
        for child in children:
            library(child)

    for name in roots:
        library(name)
    return sorted(seen)


def native_apex_libraries(base_system, module):
    """Read native libraries from the pinned donor without changing its APEX."""
    from common import ROOT
    from build_image import debugfs
    prefix = 'com.google.android.' + module
    listing = debugfs(base_system, 'ls -p /system/apex').decode()
    names = [line.split('/')[5] for line in listing.splitlines()
             if len(line.split('/')) >= 7]
    matches = [name for name in names if name in (prefix + '.capex', prefix + '.apex')]
    if len(matches) != 1:
        raise RuntimeError('Expected one donor APEX for ' + module)
    name = matches[0]
    data = debugfs(base_system, 'cat /system/apex/' + name)
    if name.endswith('.capex'):
        with zipfile.ZipFile(io.BytesIO(data)) as capex:
            data = capex.read('original_apex')
    with zipfile.ZipFile(io.BytesIO(data)) as apex:
        payload = apex.read('apex_payload.img')
    image = ROOT / 'work' / ('native-' + module + '.img')
    image.write_bytes(payload)
    result = {}
    for line in debugfs(image, 'ls -p /lib64').decode().splitlines():
        fields = line.split('/')
        if len(fields) >= 7 and fields[2].startswith('10') and fields[5].endswith('.so'):
            library = debugfs(image, 'cat /lib64/' + fields[5])
            needed(library)
            result[fields[5]] = library
    return result


def network_support(system, base_system):
    """Keep the kernel-facing network service native on post-6.1 kernels."""
    from build_image import debugfs, install
    libraries = {}
    for module in ('tethering', 'resolv', 'os.statsd'):
        libraries.update(native_apex_libraries(base_system, module))
    data = debugfs(base_system, 'cat /system/bin/netd')
    roots = needed(data)
    install(system, '/system/bin/netd', data, '0100755',
            'u:object_r:netd_exec:s0', replace=True)
    added = system_closure(system, base_system, roots, libraries)
    return {'service': 'netd', 'libraries': added}


def crash_support(system, base_system):
    """Keep native HAL stack dumps available without replacing the ARM32 APEX."""
    from common import ROOT
    from build_image import debugfs, install
    data = debugfs(base_system, 'cat /system/apex/com.android.runtime.apex')
    with zipfile.ZipFile(io.BytesIO(data)) as apex:
        payload = apex.read('apex_payload.img')
    image = ROOT / 'work/native-runtime.img'
    image.write_bytes(payload)
    helper = debugfs(image, 'cat /bin/crash_dump64')
    libraries = system_closure(system, base_system, needed(helper))
    try:
        debugfs(system, 'stat /system/bin/crash_dump64')
        replace = True
    except RuntimeError:
        replace = False
    install(system, '/system/bin/crash_dump64', helper, '0100755',
            'u:object_r:crash_dump_exec:s0', replace=replace)
    old = b'/apex/com.android.runtime/bin/crash_dump64\0'
    new = b'/system/bin/crash_dump64\0'.ljust(len(old), b'\0')
    for path in ('/system/bin/linker64', '/system/bin/bootstrap/linker64'):
        linker = debugfs(system, 'cat ' + path)
        if linker.count(old) == 1:
            linker = linker.replace(old, new)
        elif linker.count(new) != 1:
            raise RuntimeError('Unexpected native linker crash helper path.')
        install(system, path, linker, '0100755',
                'u:object_r:system_linker_exec:s0', replace=True)
    return {'helper': '/system/bin/crash_dump64', 'libraries': libraries}


def graphics_support(vendor):
    """Install only audited ARM32 guest drivers from the local build cache."""
    from common import ROOT
    from build_image import debugfs, install
    cache = ROOT / 'work/graphics/artifacts'
    receipt = cache / 'manifest.json'
    if not receipt.is_file():
        return {'installed': False, 'reason': 'ARM32 graphics have not been built'}
    manifest = json.loads(receipt.read_text())
    expected = {
        'libEGL_emulation.so', 'libGLESv1_CM_emulation.so', 'libGLESv2_emulation.so',
        'libGoldfishProfiler.so', 'libOpenglSystemCommon.so', 'libOpenglCodecCommon.so',
        'libGLESv1_enc.so', 'libGLESv2_enc.so', 'lib_renderControl_enc.so',
        'libandroidemu.so', 'libvulkan_enc.so',
        'android.hardware.graphics.mapper@3.0-impl-ranchu.so', 'libdrm.so',
    }
    files = manifest['files']
    if set(files) != expected:
        raise RuntimeError('Unexpected ARM32 graphics artifact set.')
    blobs = {}
    for name, metadata in files.items():
        data = (cache / name).read_bytes()
        if (data[:6] != b'\x7fELF\x01\x01' or data[18:20] != b'\x28\x00'
                or hashlib.sha256(data).hexdigest() != metadata['sha256']):
            raise RuntimeError('Invalid ARM32 graphics artifact: ' + name)
        blobs[name] = data
    # The Wear donor ships only ARM64 vendor runtime libraries. Resolve the
    # corresponding ARM32 SP-HAL dependencies from the official watch OTA.
    # Bionic and LLNDK buffer APIs must continue sharing the system instance.
    shared = {'libc.so', 'libdl.so', 'libdl_android.so', 'libm.so', 'liblog.so',
              'libnativewindow.so'}
    pending = [dependency for data in blobs.values() for dependency in needed32(data)]
    added = {}
    while pending:
        name = pending.pop()
        if name in shared or name in blobs or name in added:
            continue
        if '/' in name or not name.endswith('.so'):
            raise RuntimeError('Unexpected ARM32 dependency: ' + name)
        try:
            data = debugfs(ROOT / 'input/grasslte-3.0.190/vendor.img', 'cat /lib/' + name)
        except RuntimeError:
            data = debugfs(ROOT / 'input/grasslte-3.0.190/system.img',
                           'cat /system/lib/' + name)
        pending.extend(needed32(data))
        added[name] = data
    blobs.update(added)
    label = 'u:object_r:same_process_hal_file:s0'
    for directory, directory_label in (
            ('/lib/egl', label), ('/lib/hw', 'u:object_r:vendor_hal_file:s0')):
        try:
            debugfs(vendor, 'stat ' + directory)
        except RuntimeError:
            debugfs(vendor, 'mkdir ' + directory, True)
            debugfs(vendor, 'set_inode_field ' + directory + ' mode 040755', True)
        context = ROOT / 'work/graphics-dir-context'
        context.write_bytes(directory_label.encode() + b'\0')
        debugfs(vendor, 'ea_set -f "' + str(context) + '" ' + directory
                + ' security.selinux', True)
    for name, data in blobs.items():
        directory = ('/lib/egl/' if name.endswith('_emulation.so') else
                     '/lib/hw/' if name == 'android.hardware.graphics.mapper@3.0-impl-ranchu.so'
                     else '/lib/')
        target = directory + name
        try:
            debugfs(vendor, 'stat ' + target)
            replace = True
        except RuntimeError:
            replace = False
        install(vendor, target, data, '0100644', label, replace=replace)
    contexts_path = '/etc/selinux/vendor_file_contexts'
    contexts = debugfs(vendor, 'cat ' + contexts_path)
    # Remove only earlier misplaced copies produced by this experiment.
    for name, data in added.items():
        old = '/lib/hw/' + name
        try:
            previous = debugfs(vendor, 'cat ' + old)
        except RuntimeError:
            continue
        if previous != data:
            raise RuntimeError('Refusing to remove an unrelated HAL file: ' + old)
        debugfs(vendor, 'rm ' + old, True)
        contexts = contexts.replace((re.escape('/vendor' + old) + ' ' + label + '\n').encode(), b'')
    for name in blobs:
        directory = ('/vendor/lib/egl/' if name.endswith('_emulation.so') else
                     '/vendor/lib/hw/' if name == 'android.hardware.graphics.mapper@3.0-impl-ranchu.so'
                     else '/vendor/lib/')
        rule = (re.escape(directory + name) + ' ' + label + '\n').encode()
        if rule not in contexts:
            contexts += rule
    install(vendor, contexts_path, contexts, '0100644',
            'u:object_r:vendor_configs_file:s0', replace=True)
    return {'installed': True, 'files': files, 'source': manifest['source'],
            'arm32_vendor_runtime': sorted(added)}


def bpf_support(system, base_system):
    """Use native kernel-facing loaders while retaining the ARM32 APEX."""
    from common import ROOT, REPO_ROOT, sdk_path
    from build_image import debugfs, install
    roots = set()
    for name in ('netbpfload', 'bpfloader'):
        path = '/system/bin/' + name
        data = debugfs(base_system, 'cat ' + path)
        roots.update(needed(data))
        install(system, path, data, '0100755',
                'u:object_r:bpfloader_exec:s0', replace=True)
    libraries = system_closure(system, base_system, roots)
    compilers = list((sdk_path() / 'ndk').glob(
        '*/toolchains/llvm/prebuilt/darwin-*/bin/aarch64-linux-android34-clang'))
    if not compilers:
        raise RuntimeError('Install an Android NDK to build the native BPF launch bridge.')
    compiler = max(compilers, key=lambda p: tuple(int(v) for v in p.parents[5].name.split('.')))
    bridge = ROOT / 'work/watch5-bpf-launch'
    subprocess.run([str(compiler), '-nostdlib', '-static', '-Wl,-e,_start',
                    str(REPO_ROOT / 'native/watch5_bpf_launch.S'), '-o', str(bridge)],
                   check=True, capture_output=True)
    if needed(bridge.read_bytes()):
        raise RuntimeError('The BPF launch bridge must not need any runtime libraries.')
    target = '/system/bin/watch5-bpf-launch'
    try:
        debugfs(system, 'stat ' + target)
        replace = True
    except RuntimeError:
        replace = False
    install(system, target, bridge.read_bytes(), '0100755',
            'u:object_r:bpfloader_exec:s0', replace=replace)
    path = '/system/etc/init/netbpfload.rc'
    rc = debugfs(system, 'cat ' + path)
    trigger = b'    exec_start bpfloader\n'
    native_trigger = b'    exec_start watch5-bpfloader\n'
    if rc.count(trigger) == 1:
        rc = rc.replace(trigger, native_trigger)
    elif rc.count(native_trigger) != 1:
        raise RuntimeError('Expected one original BPF loading trigger.')
    # The ARM32 tethering APEX overrides the ordinary bpfloader service.
    # A separate service selects the platform loader after APEX activation.
    if b'\nservice watch5-bpfloader ' not in rc:
        rc += (b'\nservice watch5-bpfloader /system/bin/watch5-bpf-launch\n'
               b'    capabilities CHOWN SYS_ADMIN NET_ADMIN\n'
               b'    group root graphics network_stack net_admin net_bw_acct net_bw_stats net_raw system\n'
               b'    user root\n    rlimit memlock 1073741824 1073741824\n'
               b'    oneshot\n    reboot_on_failure reboot,bpfloader-failed\n'
               b'    updatable\n    stdio_to_kmsg\n')
    rc = rc.replace(b'service watch5-bpfloader /system/bin/netbpfload\n',
                    b'service watch5-bpfloader /system/bin/watch5-bpf-launch\n')
    install(system, path, rc, '0100644', 'u:object_r:system_file:s0', replace=True)
    return {'service': 'watch5-bpfloader', 'libraries': libraries,
            'bridge': '/system/bin/watch5-bpf-launch'}


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
    vendor_names = {name: '/lib64/' + name for name in listing(vendor, '/lib64')}
    vendor_names.update({name: '/lib64/hw/' + name for name in listing(vendor, '/lib64/hw')})
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
            # Vendor LLNDK links must share one Bionic instance with system.
            # A vendor-local libc would create separate allocator/tag state.
            added.append(name)
        elif name in vendor_names:
            data = debugfs(vendor, 'cat ' + vendor_names[name])
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

    services = []
    for directory in ('/bin', '/bin/hw'):
        for name, mode in listing(vendor, directory).items():
            if not mode.startswith('10'):
                continue
            path = directory + '/' + name
            data = debugfs(vendor, 'cat ' + path)
            if data[:5] != b'\x7fELF\x02':
                continue
            services.append(path)
            for dependency in needed(data):
                library(dependency)
    # Audio, graphics and sensor passthrough implementations load after init.
    for name in listing(vendor, '/lib64/hw'):
        if name.endswith('.so'):
            library(name)
    added = system_closure(system, base_system, added)
    for path in ('/etc/init/hw/init.ranchu.rc',
                 '/etc/init/android.hardware.security.keymint-service.rc',
                 '/etc/init/android.hardware.atrace@1.0-service.rc'):
        data = debugfs(vendor, 'cat ' + path)
        output = b''.join(line + (b'    stdio_to_kmsg\n' if line.startswith(b'service ') else b'')
                          for line in data.splitlines(keepends=True))
        install(vendor, path, output, '0100644', 'u:object_r:vendor_configs_file:s0', replace=True)
    crash = crash_support(system, base_system)
    bpf = bpf_support(system, base_system)
    network = network_support(system, base_system)
    graphics = graphics_support(vendor)
    return {'services': list(services), 'dependencies': sorted(seen),
            'added_system_libraries': sorted(set(added) | set(bpf['libraries'])
                                            | set(network['libraries'])
                                            | set(crash['libraries'])),
            'loader': '/system/bin/linker64', 'bionic': 'shared-system-namespace',
            'bpf': bpf, 'network': network, 'graphics': graphics, 'crash': crash}
