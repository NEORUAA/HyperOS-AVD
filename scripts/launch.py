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


def initialize(config, bypass_oobe=False):
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
    if build.is_file() and json.loads(build.read_text()).get('source') == 'official-hongkong-ota':
        from os4_defaults import apply_runtime
        apply_runtime(config)
        from apply_flutter_fix import install
        install(config)
        from apply_navigation_fix import install as install_navigation
        install_navigation(config)
        from apply_weather_fix import install as install_weather
        install_weather(config)
    if bypass_oobe:
        skip_oobe(config)
    manager = adb(config, 'shell', 'pm path me.weishu.kernelsu', capture_output=True, text=True, timeout=15)
    if 'package:' not in manager.stdout:
        adb(config, 'install', '--no-incremental', str(ROOT / 'tools/KernelSU_v3.3.0_32601-release.apk'),
            check=True, timeout=60)
    adb(config, 'shell', 'rm -f /data/local/tmp/hyperos-avd-ksud', timeout=15)
    (ROOT / 'local').mkdir(exist_ok=True)
    (ROOT / 'local/last-boot.json').write_text(json.dumps(
        {'name': config['name'], 'serial': f"emulator-{config['port']}",
         'validated_at': datetime.now().astimezone().isoformat(), 'root': result.stdout}, indent=2) + '\n')
    print(f"HyperOS is ready: emulator-{config['port']}, KernelSU root, SELinux Enforcing.", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--headless', action='store_true')
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
    userdata = ROOT / 'avd' / (config['name'] + '.avd') / 'userdata-qemu.img'
    if not userdata.exists():
        print('Creating fresh userdata from the clean release template.', flush=True)
        shutil.copyfile(ROOT / 'images/userdata.img', userdata)
    logs = ROOT / 'logs'
    logs.mkdir(exist_ok=True)
    log = logs / 'emulator-current.log'
    if log.exists():
        log.rename(logs / ('emulator-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '.log'))
    avd_config = ROOT / 'avd' / (config['name'] + '.avd') / 'config.ini'
    properties = dict(line.split('=', 1) for line in avd_config.read_text().splitlines() if '=' in line)
    memory = str(int(properties.get('hw.ramSize', '2560')))
    cores = str(int(properties.get('hw.cpu.ncore', '2')))
    command = [str(Path(config['sdk']) / 'emulator/emulator'), '-avd', config['name'],
               '-sysdir', str(ROOT / 'images'), '-port', str(config['port']),
               '-no-snapshot-load', '-no-snapshot-save', '-accel', 'on', '-gpu', 'host',
               '-memory', memory, '-cores', cores, '-no-audio', '-show-kernel', '-verbose']
    if config['name'] == 'HyperOS_4_Official_API_37':
        command += ['-crash-report-mode', 'never']
    if args.headless:
        command += ['-no-window']
    with log.open('wb') as output:
        process = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
    print(f"Starting {config['name']} (PID {process.pid}). Log: {log}", flush=True)
    # The emulator stays running if setup fails; evidence and userdata are retained.
    initialize(config, bypass_oobe=args.skip_oobe)


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        raise SystemExit(str(error))
