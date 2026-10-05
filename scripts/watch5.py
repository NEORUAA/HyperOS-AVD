#!/usr/bin/env python3
"""Build and boot the isolated 32-bit grasslte experiment with an ARM64 kernel."""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import time
import zipfile

REPO = Path(__file__).resolve().parent.parent
WORKSPACE = REPO / 'work/watch5'
NAME = 'HyperOS_Watch5_API_34'
PORT = 5576
SOURCE = 'official-grasslte-ota'
ARCHIVE = 'grasslte-ota_full-OS3.0.190.0.VOFAECNXM-user-15.0-5f05bdfd2e.zip'
ARCHIVE_SHA256 = '25028d3b26ef77f997c2c0518d8d7326459426483b9701aed8126d4a90adcb7f'
PARTITIONS = ('system', 'system_ext', 'product', 'mi_ext', 'vendor', 'odm')


def properties(data):
    return dict(line.split('=', 1) for line in data.decode().splitlines()
                if '=' in line and not line.startswith('#'))


def set_properties(data, values):
    """Replace complete keys, retaining comments and unrelated properties."""
    pending = dict(values)
    lines = []
    for line in data.decode().splitlines():
        key, sep, _ = line.partition('=')
        if sep and key in values:
            if key in pending:
                lines.append(key + '=' + pending.pop(key))
        else:
            lines.append(line)
    lines.extend(key + '=' + value for key, value in pending.items())
    return ('\n'.join(lines) + '\n').encode()


def add_mi_ext_fstab(data):
    if b'mi_ext ' in data:
        raise RuntimeError('Unexpected existing mi_ext mount entry.')
    return data.rstrip(b'\n') + b'\nmi_ext /mi_ext ext4 ro wait,logical,first_stage_mount,nofail\n'


def patch_ramdisk(path):
    """Resize the newc fstab entry without changing other ramdisk files."""
    from common import tool
    raw = subprocess.check_output([tool('lz4', 'lz4'), '-dc', str(path)])
    output, cursor, found = bytearray(), 0, 0
    while cursor < len(raw):
        if raw[cursor:cursor + 6] != b'070701':
            # Android vendor ramdisks can concatenate multiple newc archives,
            # separated by zero padding after each TRAILER!!! entry.
            following = raw.find(b'070701', cursor)
            if following < 0 or any(raw[cursor:following]):
                break
            output.extend(raw[cursor:following])
            cursor = following
        header = bytearray(raw[cursor:cursor + 110])
        size, namesize = int(header[54:62], 16), int(header[94:102], 16)
        name = raw[cursor + 110:cursor + 110 + namesize]
        start = (cursor + 110 + namesize + 3) // 4 * 4
        data = raw[start:start + size]
        if name.rstrip(b'\0') == b'first_stage_ramdisk/fstab.ranchu':
            data = add_mi_ext_fstab(data)
            header[54:62] = f'{len(data):08x}'.encode()
            found += 1
        output.extend(header + name)
        output.extend(bytes(-len(output) % 4))
        output.extend(data)
        output.extend(bytes(-len(output) % 4))
        cursor = (start + size + 3) // 4 * 4
    if found != 1:
        raise RuntimeError('Expected one ranchu first-stage mount table.')
    output.extend(raw[cursor:])
    temporary = path.with_suffix('.next.img')
    subprocess.run([tool('lz4', 'lz4'), '-l', '-f', '-', str(temporary)],
                   input=output, check=True, capture_output=True)
    if subprocess.check_output([tool('lz4', 'lz4'), '-dc', str(temporary)]) != output:
        raise RuntimeError('Watch ramdisk verification failed.')
    temporary.replace(path)


def check_metadata(metadata):
    if (metadata.get('pre-device') != 'grasslte'
            or metadata.get('post-sdk-level') != '34'
            or metadata.get('post-build-incremental') != 'OS3.0.190.0.VOFAECNXM'):
        raise RuntimeError('Use the supplied grasslte OS3.0.190.0 OTA.')


def build(archive, diagnostic_adb=False):
    from common import ROOT, sdk_path, sha256, tool, port_free, avd_home
    from lp_image import pack, unpack
    from build_image import debugfs, install
    from init_userdata import create
    from setup import configure
    from watch5_native import support
    port_free(PORT)
    registry = avd_home() / (NAME + '.ini')
    if registry.exists():
        registered = properties(registry.read_bytes()).get('path', '')
        if Path(registered).resolve() != ROOT / 'avd' / (NAME + '.avd'):
            raise RuntimeError('Watch AVD registration belongs to another workspace.')
    if sha256(archive) != ARCHIVE_SHA256:
        raise RuntimeError('Watch OTA checksum mismatch; no images were changed.')
    with zipfile.ZipFile(archive) as z:
        metadata = properties(z.read('META-INF/com/android/metadata'))
    check_metadata(metadata)
    base = sdk_path() / 'system-images/android-34/android-wear/arm64-v8a'
    if not (base / 'system.img').is_file():
        raise RuntimeError('Install system-images;android-34;android-wear;arm64-v8a first.')
    if properties((base / 'source.properties').read_bytes()).get('Pkg.Revision') != '1':
        raise RuntimeError('This experiment is pinned to Wear API 34 ARM64 revision 1.')
    for directory in ('work', 'images', 'logs', 'config', 'local', 'tools'):
        (ROOT / directory).mkdir(parents=True, exist_ok=True)
    parts = ROOT / 'input/grasslte-3.0.190'
    if any(not (parts / (part + '.img')).is_file() for part in PARTITIONS):
        subprocess.run([str(REPO / 'tools/payload-dumper-go'), '-c', '2', '-p',
                        ','.join(PARTITIONS), '-o', str(parts), str(archive)], check=True)
    prop = debugfs(parts / 'system.img', 'cat /system/build.prop')
    if properties(prop).get('ro.product.cpu.abi') != 'armeabi-v7a':
        raise RuntimeError('Unexpected watch userspace architecture.')
    init = debugfs(parts / 'system.img', 'cat /system/bin/init')
    if init[:5] != b'\x7fELF\x01' or init[18:20] != b'\x28\x00':
        raise RuntimeError('Expected the original ARM32 watch init.')
    if not (ROOT / 'work/base/vendor.img').is_file():
        unpack(base / 'system.img', ROOT / 'work/base')
    system, vendor, ext = [ROOT / 'work' / name for name in
                           ('watch-system.img', 'watch-vendor.img', 'watch-system_ext.img')]
    shutil.copyfile(parts / 'system.img', system)
    shutil.copyfile(ROOT / 'work/base/vendor.img', vendor)
    shutil.copyfile(parts / 'system_ext.img', ext)
    prop = set_properties(prop, {
        'ro.sf.lcd_density': '320', 'ro.zygote': 'zygote32',
        'debug.hwui.renderer': 'skiagl',
    })
    if diagnostic_adb:
        prop = set_properties(prop, {'ro.adb.secure': '0', 'ro.debuggable': '1'})
    install(system, '/system/build.prop', prop, '0100600',
            'u:object_r:system_file:s0', replace=True)
    vendor_prop = set_properties(debugfs(vendor, 'cat /build.prop'), {
        'ro.zygote': 'zygote32', 'ro.bionic.arch': 'arm',
        'ro.vendor.product.cpu.abilist': 'armeabi-v7a,armeabi',
        'ro.vendor.product.cpu.abilist32': 'armeabi-v7a,armeabi',
        'ro.vendor.product.cpu.abilist64': '',
        'dalvik.vm.dex2oat64.enabled': 'false',
    })
    install(vendor, '/build.prop', vendor_prop, '0100600',
            'u:object_r:vendor_file:s0', replace=True)
    install(vendor, '/etc/fstab.ranchu', add_mi_ext_fstab(debugfs(vendor,
            'cat /etc/fstab.ranchu')), '0100644', 'u:object_r:vendor_configs_file:s0', replace=True)
    init_rc = (b'on post-fs-data\n    setprop persist.sys.usb.config adb\n'
               b'    setprop sys.usb.config adb\n')
    install(ext, '/etc/init/init.watch5_avd.rc', init_rc, '0100644', 'u:object_r:system_file:s0')
    native = support(system, vendor, ROOT / 'work/base/system.img')
    for path in base.iterdir():
        if path.is_file() and path.name not in ('system.img', 'package.xml'):
            shutil.copy2(path, ROOT / 'images' / path.name)
    shutil.copytree(base / 'data', ROOT / 'images/data', dirs_exist_ok=True)
    # This host-side ABI declaration also prevents the emulator's ARM64 glue
    # from unconditionally enabling HVF despite -accel off.
    (ROOT / 'images/build.prop').write_bytes(prop)
    source_props = (ROOT / 'images/source.properties').read_bytes()
    (ROOT / 'images/source.properties').write_bytes(set_properties(source_props,
                                                    {'SystemImage.Abi': 'armeabi-v7a'}))
    patch_ramdisk(ROOT / 'images/ramdisk.img')
    pack(base / 'system.img', ROOT / 'images/system.img', [
        ('system', system), ('vendor', vendor), ('system_ext', ext),
        ('product', parts / 'product.img'), ('mi_ext', parts / 'mi_ext.img'),
        ('system_dlkm', ROOT / 'work/base/system_dlkm.img')])
    create()
    shutil.copy2(REPO / 'config/watch5.ini', ROOT / 'config/avd.ini')
    manifest = {'source': SOURCE, 'hyperos': 'OS3.0.190.0.VOFAECNXM', 'android_api': 34,
                'archive_sha256': ARCHIVE_SHA256, 'system_sha256': sha256(ROOT / 'images/system.img'),
                'userspace_abi': 'armeabi-v7a', 'kernel_arch': 'arm64', 'acceleration': 'tcg',
                'physical_ppi': 312, 'logical_density': 320, 'diagnostic_adb': diagnostic_adb,
                'experimental': True, 'hardware_base': 'android-34/android-wear/arm64-v8a',
                'ota_metadata': metadata, 'native_support': native}
    (ROOT / 'local/build.json').write_text(json.dumps(manifest, indent=2) + '\n')
    configure(sdk_path(), NAME, PORT)
    print('Watch candidate built. Boot success has not been established.', flush=True)


def start():
    from common import ROOT, runtime, port_free
    from setup import configure
    from manage import resize, validate_userdata
    config = runtime()
    if config.get('name') != NAME or config.get('port') != PORT:
        raise RuntimeError('Build the isolated Watch5 candidate first.')
    manifest = json.loads((ROOT / 'local/build.json').read_text())
    if manifest.get('source') != SOURCE:
        raise RuntimeError('Refusing to label a stock Wear reference as HyperOS.')
    if properties((ROOT / 'images/build.prop').read_bytes()).get('ro.product.cpu.abi') != 'armeabi-v7a':
        raise RuntimeError('Watch host ABI must select TCG; refusing an accidental HVF boot.')
    port_free(PORT)
    sdk = Path(config['sdk'])
    configure(sdk, NAME, PORT)
    avd = ROOT / 'avd' / (NAME + '.avd')
    userdata = avd / 'userdata-qemu.img'
    if not userdata.exists():
        shutil.copyfile(ROOT / 'images/userdata.img', userdata)
    validate_userdata(sdk, avd)
    resize(sdk, avd, 32)
    # Invoke the ARM64 core directly: the kernel is ARM64, but every original
    # watch process is ARM32. The generic launcher may select the ARM32 core.
    command = [str(sdk / 'emulator/qemu/darwin-aarch64/qemu-system-aarch64'),
               '-avd', NAME, '-sysdir', str(ROOT / 'images'), '-port', str(PORT),
               '-no-snapshot-load', '-no-snapshot-save', '-accel', 'off', '-gpu', 'host',
               '-memory', '2048', '-cores', '4', '-show-kernel', '-verbose',
               '-crash-report-mode', 'never', '-qemu', '-cpu', 'cortex-a53', '-m', '2048']
    environment = os.environ.copy()
    environment['DYLD_LIBRARY_PATH'] = str(sdk / 'emulator/lib64') + ':' + str(sdk / 'emulator/lib64/qt/lib')
    log = ROOT / 'logs' / ('watch-' + datetime.now().strftime('%Y%m%d-%H%M%S') + '.log')
    with log.open('wb') as output:
        process = subprocess.Popen(command, env=environment, stdout=output,
                                   stderr=subprocess.STDOUT, start_new_session=True)
    (ROOT / 'local/watch-process.json').write_text(json.dumps(
        {'pid': process.pid, 'command': command, 'log': str(log)}, indent=2) + '\n')
    time.sleep(1)
    if process.poll() is not None:
        raise RuntimeError('Watch emulator exited. Inspect ' + str(log))
    print(f'Started {NAME}: emulator-{PORT}, PID {process.pid}. Log: {log}')


def stop():
    """Stop only the owned Watch5 process, refusing a recycled PID."""
    from common import ROOT, runtime, adb
    config = runtime()
    if config.get('name') != NAME or config.get('port') != PORT:
        raise RuntimeError('Unexpected Watch5 runtime identity.')
    record = ROOT / 'local/watch-process.json'
    state = json.loads(record.read_text())
    pid = state['pid']
    found = subprocess.run(['ps', '-p', str(pid), '-o', 'command='],
                           capture_output=True, text=True)
    if found.returncode:
        print('Owned Watch5 process is already stopped.')
        return
    if (f'-avd {NAME} ' not in found.stdout or f'-port {PORT} ' not in found.stdout
            or str(Path(config['sdk']) / 'emulator/qemu/darwin-aarch64/qemu-system-aarch64')
            not in found.stdout):
        raise RuntimeError('PID does not identify the owned Watch5 emulator; preserved.')
    result = adb(config, 'emu', 'kill', capture_output=True, timeout=5)
    if result.returncode:
        os.kill(pid, signal.SIGTERM)
    for _ in range(40):
        if subprocess.run(['kill', '-0', str(pid)], capture_output=True).returncode:
            state['stopped'] = True
            record.write_text(json.dumps(state, indent=2) + '\n')
            print('Stopped only Watch5. Userdata and logs were retained.')
            return
        time.sleep(.25)
    raise RuntimeError('Watch5 did not stop; no disk files were changed.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('build', 'start', 'stop'))
    parser.add_argument('--zip', type=Path, default=REPO / ARCHIVE)
    parser.add_argument('--diagnostic-adb', action='store_true')
    args = parser.parse_args()
    os.environ['HYPEROS_AVD_WORKSPACE'] = str(WORKSPACE)
    from common import host_check
    host_check()
    if args.action == 'build':
        build(args.zip.resolve(), args.diagnostic_adb)
    elif args.action == 'start':
        start()
    else:
        stop()


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        raise SystemExit(str(error))
