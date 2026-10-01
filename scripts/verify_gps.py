#!/usr/bin/env python3
"""Build/install a temporary GPS probe and check hardware fixes plus start/stop."""
import argparse
from pathlib import Path
import os
import shlex
import subprocess
import time
import zipfile

from common import ROOT, adb, runtime
from patch_gnss import java


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--keep-app', action='store_true')
    args = parser.parse_args()
    config = runtime()
    sdk = Path(config['sdk'])
    tools = sdk / 'build-tools/36.0.0'
    android = sdk / 'platforms/android-36/android.jar'
    jdk = Path(java()).parent.parent
    work = ROOT / 'work/gps-probe'
    work.mkdir(parents=True, exist_ok=True)
    classes = work / 'classes'
    classes.mkdir(exist_ok=True)
    subprocess.run([str(jdk / 'bin/javac'), '-source', '8', '-target', '8', '-classpath', str(android),
                    '-d', str(classes), str(ROOT / 'tests/gps_probe/MainActivity.java')], check=True)
    subprocess.run([str(tools / 'd8'), '--lib', str(android), '--output', str(work),
                    *map(str, classes.rglob('*.class'))], check=True)
    unsigned = work / 'unsigned.apk'
    subprocess.run([str(tools / 'aapt'), 'package', '-f', '-M',
                    str(ROOT / 'tests/gps_probe/AndroidManifest.xml'), '-I', str(android),
                    '-F', str(unsigned)], check=True)
    with zipfile.ZipFile(unsigned, 'a') as archive:
        archive.write(work / 'classes.dex', 'classes.dex')
    key = work / 'debug.jks'
    if not key.exists():
        subprocess.run([str(jdk / 'bin/keytool'), '-genkeypair', '-keystore', str(key),
                        '-storepass', 'android', '-keypass', 'android', '-alias', 'debug',
                        '-dname', 'CN=HyperOS-AVD GPS Probe', '-keyalg', 'RSA', '-validity', '3650'],
                       check=True, capture_output=True)
    apk = work / 'probe.apk'
    subprocess.run([str(tools / 'apksigner'), 'sign', '--ks', str(key), '--ks-pass', 'pass:android',
                    '--out', str(apk), str(unsigned)], check=True,
                   env={**os.environ, 'JAVA_HOME': str(jdk)})
    package = 'io.github.hyperosavd.gpsprobe'
    adb(config, 'install', '--no-incremental', '-r', str(apk), check=True, timeout=60)
    # HyperOS normalizes permissions on an app's first launch. Let that finish
    # before granting this temporary probe's permissions and starting the test.
    warmup = f'input keyevent 224; wm dismiss-keyguard; am start -W -n {package}/.MainActivity'
    adb(config, 'shell', 'su -c ' + shlex.quote(warmup), check=True, timeout=30)
    time.sleep(5)
    command = (f'input keyevent 224; wm dismiss-keyguard; '
               f'pm grant {package} android.permission.ACCESS_COARSE_LOCATION; '
               f'pm grant {package} android.permission.ACCESS_FINE_LOCATION; '
               f'am force-stop {package}; am start -W -n {package}/.MainActivity')
    adb(config, 'shell', 'su -c ' + shlex.quote(command), check=True, timeout=30)
    before = adb(config, 'shell', 'pidof system_server', capture_output=True, text=True, timeout=15).stdout.strip()
    for longitude, latitude, delay in [('114.1733', '22.3200', 32), ('-0.1278', '51.5074', 38)]:
        adb(config, 'emu', 'geo', 'fix', longitude, latitude, check=True, timeout=15)
        time.sleep(delay)
    result = adb(config, 'shell',
                 f"su -c 'cat /data/user/0/{package}/files/gps-probe.txt'",
                 capture_output=True, text=True, check=True, timeout=60).stdout
    after = adb(config, 'shell', 'pidof system_server', capture_output=True, text=True, timeout=15).stdout.strip()
    # Restrict results to this probe launch, not historical logcat successes.
    start = result.rfind('CYCLE 0 REQUEST GPS/FUSED')
    current = result[start:] if start >= 0 else ''
    logs = ROOT / 'logs'
    logs.mkdir(exist_ok=True)
    (logs / 'gps-probe.txt').write_text(current)
    successful = (before == after and bool(before) and current.count('GNSS STARTED') == 4
                  and current.count('GNSS STOPPED') == 4 and 'COMPLETE' in current
                  and 'lat=22.32' in current and 'lat=51.507' in current and 'ERROR ' not in current)
    if not args.keep_app:
        adb(config, 'uninstall', package, timeout=30)
    if not successful:
        raise RuntimeError('GPS regression failed. Inspect logs/gps-probe.txt.')
    print('PASS: two injected coordinates, four GNSS start/stop cycles, unchanged system_server.')


if __name__ == '__main__':
    main()
