#!/usr/bin/env python3
"""Cold-boot only the project AVD and initialize KernelSU on fresh userdata."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import time

from common import ROOT, adb, fetch_ksu, host_check, port_free, runtime
from setup import configure


def is_os4():
    build = ROOT / 'local/build.json'
    return build.is_file() and json.loads(build.read_text()).get('source') in (
        'official-hongkong-ota', 'official-yingtian-ota')


def vulkan_features(build):
    """Select image-specific descriptor and presentation workarounds."""
    if build.get('source') == 'official-yingtian-ota':
        return ['-feature', 'VulkanBatchedDescriptorSetUpdate']
    if (build.get('source') == 'official-hongkong-ota'
            and build.get('hyperos') == '4.0.18.0.XFRCNXM'):
        from phone_profile import profile_from_build
        from patch_pad_hwui import PHONE_BEFORE, PHONE_AFTER, PHONE_PROFILE
        profile = profile_from_build(build)
        expected = {'before': PHONE_BEFORE, 'after': PHONE_AFTER, 'profile': PHONE_PROFILE}
        if (profile['pins']['hwui'] == PHONE_BEFORE and build.get('hwui') == expected
                and build.get('hwui_renderer') == 'skiavk'):
            # Keep the verified presentation workaround. The separately baked
            # sync driver bounds command bursts when both panels are active.
            return ['-feature', 'VulkanBatchedDescriptorSetUpdate,-GLAsyncSwap']
    return []


def prepare_userdata(config):
    """Repair disk/filesystem mismatches before the owned Emulator can open them."""
    avd = ROOT / 'avd' / (config['name'] + '.avd')
    properties = dict(line.split('=', 1) for line in (avd / 'config.ini').read_text().splitlines()
                      if '=' in line)
    storage = config.get('hardware', {}).get('disk.dataPartition.size',
                                             properties.get('disk.dataPartition.size'))
    if (not isinstance(storage, str) or not storage.endswith('G') or not storage[:-1].isdigit()
            or not 6 <= int(storage[:-1]) <= 1024):
        raise RuntimeError('Missing or invalid userdata capacity in this AVD configuration.')
    from manage import resize, validate_userdata
    from userdata_resize import PENDING
    pending = avd / PENDING
    recovered = None
    if pending.exists() or pending.is_symlink():
        # An interrupted activation can temporarily have no active base. Its
        # original chain must be recovered before considering a fresh template.
        recovered = resize(Path(config['sdk']), avd, int(storage[:-1]), allow_guest=True)
    userdata = avd / 'userdata-qemu.img'
    if not userdata.exists():
        from userdata_resize import check_dependencies
        check_dependencies(Path(config['sdk']))
        print('Creating fresh userdata from the clean release template.', flush=True)
        shutil.copyfile(ROOT / 'images/userdata.img', userdata)
    validate_userdata(Path(config['sdk']), avd)
    result = recovered if recovered is not None else resize(
        Path(config['sdk']), avd, int(storage[:-1]), allow_guest=True)
    if isinstance(result, dict) and result.get('guest_required'):
        from manage import backup, prepare_storage
        folder = backup(ROOT, config['name'])
        result = prepare_storage(ROOT, config['name'], config['port'],
                                 Path(config['sdk']), int(storage[:-1]), folder)
    return result


def skip_oobe(config):
    command = '\n'.join([
        'set -e',
        'settings put global device_provisioned 1',
        'settings put secure user_setup_complete 1',
        'pm disable-user --user 0 com.android.provision',
        'am force-stop com.android.provision',
        'input keyevent 224',
        'wm dismiss-keyguard',
        'am start -W -a android.intent.action.MAIN -c android.intent.category.HOME '
        '-n com.miui.home/com.miui.home.launcher.Launcher',
    ])
    adb(config, 'shell', 'su -W -c ' + shlex.quote(command), check=True, timeout=30)
    state = adb(config, 'shell',
                'settings get global device_provisioned\n'
                'settings get secure user_setup_complete\n'
                'pm list packages -d com.android.provision',
                capture_output=True, text=True, check=True, timeout=15).stdout.splitlines()
    if state != ['1', '1', 'package:com.android.provision']:
        raise RuntimeError('OOBE bypass validation failed: ' + repr(state))
    print('OOBE skipped. Existing userdata was preserved.', flush=True)


def migrate_boot_service_helpers(config):
    """Refresh only authenticated legacy hooks; keep optional failures local."""
    from apply_boot_service_fix import migrate_kernel_helper, migrate_uninstall_hook, migrate_lifecycle_hooks
    results = {}
    for name, migrate in (('kernel helper', migrate_kernel_helper),
                          ('uninstall hook', migrate_uninstall_hook),
                          ('lifecycle hooks', migrate_lifecycle_hooks)):
        try:
            outcome = migrate(config)
        except (RuntimeError, OSError, subprocess.SubprocessError) as error:
            reason = str(error).splitlines()[0][:240] if str(error) else type(error).__name__
            print(f'Legacy boot-service {name} is preserved: {reason}', flush=True)
            results[name] = {'migrated': False, 'preserved': True, 'error': reason}
            continue
        results[name] = outcome
        if outcome.get('migrated'):
            print(f'Legacy boot-service {name} migrated; current user settings are preserved.', flush=True)
        elif outcome.get('unsupported'):
            print(outcome['reason'], flush=True)
        elif outcome.get('pending') or outcome.get('preserved'):
            print(f'Legacy boot-service {name} lifecycle choice is preserved.', flush=True)
    return results


def optional_patch(outcomes, feature, install, *arguments, **options):
    """Skip only an explicitly unsupported, untouched optional workload."""
    from patch_outcome import UnsupportedPatch
    try:
        result = install(*arguments, **options)
    except UnsupportedPatch as error:
        reason = str(error).splitlines()[0][:240]
        outcomes[feature] = {'state': 'unsupported', 'reason': reason}
        print(f'{feature}: original workload preserved; {reason}', flush=True)
        return None
    outcomes[feature] = {'state': 'checked'}
    return result


def initialize(config, bypass_oobe=False, rotate_window=True):
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        try:
            result = adb(config, 'shell', 'id', capture_output=True,
                         text=True, timeout=10)
            if result.returncode == 0 and 'uid=' in result.stdout:
                break
        except subprocess.TimeoutExpired:
            pass
        time.sleep(3)
    else:
        raise RuntimeError('ADB did not become ready within 5 minutes. Inspect logs/emulator-current.log.')
    if is_os4():
        from os4_defaults import apply_sensor_defaults
        apply_sensor_defaults(config)
    root = adb(config, 'shell', "su -W -c 'id'", capture_output=True, text=True, timeout=15)
    if 'uid=0(' not in root.stdout:
        binary = ROOT / 'tools/ksud-aarch64-linux-android'
        remote = '/data/local/tmp/hyperos-avd-ksud'
        adb(config, 'push', str(binary), remote, check=True, capture_output=True, timeout=30)
        adb(config, 'shell', f'chmod 755 {remote}', check=True, timeout=15)
        result = adb(config, 'shell', remote + ' debug su', input=remote + ' install\nexit\n',
                     text=True, capture_output=True, timeout=30)
        if result.returncode:
            raise RuntimeError('KernelSU initialization failed: ' + result.stderr)
    result = adb(config, 'shell', "su -W -c 'id; getenforce; /data/adb/ksud feature set selinux_hide 0; /data/adb/ksud feature save'",
                 capture_output=True, text=True, timeout=20)
    if 'uid=0(' not in result.stdout or 'Enforcing' not in result.stdout or result.returncode:
        raise RuntimeError('KernelSU root/enforcing validation failed: ' + result.stdout + result.stderr)
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        completed = adb(config, 'shell', 'getprop sys.boot_completed', capture_output=True,
                        text=True, timeout=15)
        if completed.returncode == 0 and completed.stdout.strip() == '1':
            break
        time.sleep(3)
    else:
        raise RuntimeError('Boot did not finish within 5 minutes. Inspect logs/emulator-current.log.')
    build = ROOT / 'local/build.json'
    metadata = json.loads(build.read_text()) if build.is_file() else {}
    source = metadata.get('source')
    outcomes = {}
    if source == 'official-hongkong-ota':
        from os4_defaults import apply_runtime
        apply_runtime(config, managed=True)
    elif source == 'official-yingtian-ota':
        from os4_pad import apply_runtime, align_window
        apply_runtime(config, managed=True)
        if rotate_window:
            align_window(config)
    if is_os4():
        migrate_boot_service_helpers(config)
        # Both OS4 variants install the same two module artifacts. Guest
        # services select individual recipes by content and capability.
        from apply_native_compat import install as install_native_compat
        from core_context import load as load_core_context
        outcomes['core'] = install_native_compat(config, platform_context=load_core_context(ROOT))
        from apply_app_compat import install_prebuilt as install_apps
        outcomes['apps'] = install_apps(config, workspace=ROOT)
    if bypass_oobe:
        skip_oobe(config)
    manager = adb(config, 'shell', 'pm path me.weishu.kernelsu', capture_output=True, text=True, timeout=15)
    if 'package:' not in manager.stdout:
        apk = ROOT / 'tools/KernelSU_v3.3.0_32601-release.apk'
        if build.is_file() and json.loads(build.read_text()).get('source') == 'official-yingtian-ota':
            # MIUI's fresh tablet setup rejects shell installs; use verified KernelSU root.
            remote = '/data/local/tmp/hyperos-avd-ksu.apk'
            adb(config, 'push', str(apk), remote, check=True, capture_output=True, timeout=30)
            try:
                adb(config, 'shell', 'su -W -c ' + shlex.quote('pm install -r ' + remote),
                    check=True, timeout=60)
            finally:
                adb(config, 'shell', 'rm -f ' + remote, timeout=15)
        else:
            adb(config, 'install', '--no-incremental', str(apk), check=True, timeout=60)
    adb(config, 'shell', 'rm -f /data/local/tmp/hyperos-avd-ksud', timeout=15)
    (ROOT / 'local').mkdir(exist_ok=True)
    (ROOT / 'local/last-boot.json').write_text(json.dumps(
        {'name': config['name'], 'serial': f"emulator-{config['port']}",
         'validated_at': datetime.now().astimezone().isoformat(), 'root': result.stdout,
         'patch_outcomes': outcomes}, indent=2) + '\n')
    print(f"HyperOS is ready: emulator-{config['port']}, KernelSU root, SELinux Enforcing.", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--headless', action='store_true')
    parser.add_argument('--no-host-color-fix', action='store_true',
                        help='Disable the OS4 macOS sRGB window tag for this launch')
    parser.add_argument('--skip-oobe', action='store_true',
                        help='Skip Xiaomi provisioning and open the native launcher')
    args = parser.parse_args()
    host_check()
    config = runtime()
    port_free(config['port'])
    if not (ROOT / 'images/userdata.img').is_file():
        raise RuntimeError('Run Setup.command with the release manifest first.')
    # Refresh relocated paths before every cold start; collision checks protect other AVDs.
    configure(Path(config['sdk']), config['name'], config['port'])
    fetch_ksu(['ksud-aarch64-linux-android', 'KernelSU_v3.3.0_32601-release.apk'])
    prepare_userdata(config)
    logs = ROOT / 'logs'
    logs.mkdir(exist_ok=True)
    log = logs / 'emulator-current.log'
    if log.exists():
        log.rename(logs / ('emulator-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '.log'))
    avd_config = ROOT / 'avd' / (config['name'] + '.avd') / 'config.ini'
    properties = dict(line.split('=', 1) for line in avd_config.read_text().splitlines() if '=' in line)
    memory = str(int(properties.get('hw.ramSize', '2560')))
    build = ROOT / 'local/build.json'
    cores = str(int(properties.get('hw.cpu.ncore', '2')))
    command = [str(Path(config['sdk']) / 'emulator/emulator'), '-avd', config['name'],
               '-sysdir', str(ROOT / 'images'), '-port', str(config['port']),
               '-no-snapshot-load', '-no-snapshot-save', '-accel', 'on', '-gpu', 'host',
               '-memory', memory, '-cores', cores, '-show-kernel', '-verbose']
    if is_os4():
        command += ['-crash-report-mode', 'never']
    if build.is_file():
        # The guest supports batched updates; gfxstream also masks inline uniform
        # blocks in this mode, avoiding the observed MoltenVK descriptor crash.
        image_build = json.loads(build.read_text())
        command += vulkan_features(image_build)
        from rear_display_config import runtime_options
        command += runtime_options(image_build)
    if args.headless:
        command += ['-no-window']
    from host_color import environment
    launch_environment = environment(ROOT, config['name'],
                                     enabled=not (args.headless or args.no_host_color_fix))
    with log.open('wb') as output:
        process = subprocess.Popen(command, env=launch_environment, stdout=output,
                                   stderr=subprocess.STDOUT, start_new_session=True)
    print(f"Starting {config['name']} (PID {process.pid}). Log: {log}", flush=True)
    # The emulator stays running if setup fails; evidence and userdata are retained.
    initialize(config, bypass_oobe=args.skip_oobe, rotate_window=not args.headless)


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        raise SystemExit(str(error))
