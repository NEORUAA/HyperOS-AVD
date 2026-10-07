#!/usr/bin/env python3
"""Stage reversible, source-pinned boot fixes on an owned hongkong OS4.0.18 AVD."""
import argparse
import json
from pathlib import Path
import shlex

from common import ROOT, adb, runtime, sha256
from apply_flutter_fix import official, root
from patch_boot_services import TARGETS, AFTER, PROBE_SHA256
from phone_profile import profile_from_build

MODULE = '/data/adb/modules/hyperos_avd_boot_services'
PERF_SCRIPT = Path(__file__).resolve().parent.parent / 'config/check_kernel_services.sh'
PACKAGES = {'qualcomm': 'com.qualcomm.location', 'registration': 'com.xiaomi.registration'}


def receipt():
    return {'schema': 1, 'firmware': 'OS4.0.18.0.XFRCNXM',
            'targets': {name: {'path': path, 'before': before, 'after': AFTER[name]}
                        for name, (path, before, _) in TARGETS.items()},
            'probe_sha256': PROBE_SHA256}


def boot_script():
    rows = '\n'.join(' '.join((name, path, before, AFTER[name]))
                     for name, (path, before, _) in TARGETS.items())
    script = r'''#!/system/bin/sh
MODDIR=${0%/*}
BB=/data/adb/ksu/bin/busybox
[ -f "$MODDIR/disable" ] && exit 0
[ "$(getprop ro.boot.hardware)" = ranchu ] || exit 1
[ "$(getprop ro.product.device)" = hongkong ] || exit 1
[ "$(getprop ro.mi.os.version.incremental)" = OS4.0.18.0.XFRCNXM ] || exit 1
[ -x "$BB" ] || exit 1
check_ns() {
    "$BB" nsenter -t "$1" -m -- sh -c '
        BB=$1; MODDIR=$2; mode=$3
        while read name target before after; do
            [ -n "$name" ] || continue
            source="$MODDIR/payload/$name"
            [ "$($BB sha256sum "$source" | $BB cut -d " " -f 1)" = "$after" ] || exit 1
            current=$($BB sha256sum "$target" | $BB cut -d " " -f 1)
            case "$current" in "$before"|"$after") ;; *) exit 1 ;; esac
            if [ "$mode" = mount ] && [ "$current" = "$before" ]; then
                "$BB" mount -o bind "$source" "$target" || exit 1
                [ "$($BB sha256sum "$target" | $BB cut -d " " -f 1)" = "$after" ] || exit 1
            fi
        done < "$MODDIR/targets.conf"
    ' sh "$BB" "$MODDIR" "$2"
}
pids="1 $(getprop init.svc_debug_pid.hyos_spawner) $(pidof zygote64)"
for pid in $pids; do
    [ -d "/proc/$pid" ] || continue
    check_ns "$pid" verify || exit 1
done
for pid in $pids; do
    [ -d "/proc/$pid" ] || continue
    check_ns "$pid" mount || exit 1
done
[ "$($BB sha256sum "$MODDIR/kernel-probe" | $BB cut -d ' ' -f 1)" = PROBE_HASH ] || exit 1
sh "$MODDIR/check-kernel-services.sh" "$MODDIR/kernel-probe"
'''
    return script.replace('PROBE_HASH', PROBE_SHA256), rows + '\n'


def install(config, payload):
    official(config)
    if profile_from_build()['hyperos'] != '4.0.18.0.XFRCNXM':
        raise RuntimeError('Boot service fixes require the pinned 4.0.18 phone firmware.')
    payload = Path(payload)
    saved = root(config, f'if [ -d {MODULE} ]; then cat {MODULE}/manifest.json; fi')
    previous = json.loads(saved) if saved else None
    if previous and previous != receipt():
        raise RuntimeError('An unrelated boot service module exists.')
    for name, (path, before, _) in TARGETS.items():
        suffix = '.apk' if name in ('qualcomm', 'registration') else '.jar'
        if sha256(payload / (name + suffix)) != AFTER[name]:
            raise RuntimeError('Unsupported boot service payload: ' + name)
        if name in PACKAGES and root(config, 'pm path ' + PACKAGES[name]) != 'package:' + path:
            raise RuntimeError('An updated or missing boot service APK is active: ' + name)
        current = root(config, 'sha256sum ' + shlex.quote(path)).split()[0]
        if current not in (before, AFTER[name]):
            raise RuntimeError('An unrelated app update or framework is installed: ' + name)
        if previous:
            expected = previous['targets'][name]['after']
            if root(config, f'sha256sum {MODULE}/payload/{name}').split()[0] != expected:
                raise RuntimeError('Existing boot service payload differs from its receipt: ' + name)
    if sha256(payload / 'kernel-probe') != PROBE_SHA256:
        raise RuntimeError('Unsupported kernel probe.')
    if previous:
        for relative, expected in (('kernel-probe', PROBE_SHA256),
                                   ('check-kernel-services.sh', sha256(PERF_SCRIPT))):
            if root(config, f'sha256sum {MODULE}/{relative}').split()[0] != expected:
                raise RuntimeError('Existing boot service policy differs: ' + relative)
    if root(config, f'if [ -f {MODULE}/disable ]; then echo yes; fi'):
        print('Boot service module is disabled; preserving this choice.')
        return
    if previous == receipt():
        print('Verified boot service module is already staged.')
        return
    stage = '/data/local/tmp/hyperos-avd-boot-services-stage'
    if root(config, f'if [ -e {stage} ]; then echo yes; fi'):
        raise RuntimeError('A boot service staging directory already exists.')
    folder = ROOT / 'work/boot-service-module'
    folder.mkdir(parents=True, exist_ok=True)
    script, targets = boot_script()
    prior = root(config, 'getprop persist.sys.stability.PrereadEnable')
    uninstall = (
                 '#!/system/bin/sh\n[ "$(getprop persist.sys.stability.PrereadEnable)" = false ] && '
                 'setprop persist.sys.stability.PrereadEnable ' + shlex.quote(prior) + '\n')
    files = {'manifest.json': json.dumps(receipt(), indent=2) + '\n',
             'module.prop': 'id=hyperos_avd_boot_services\nname=HyperOS AVD boot service compatibility\nversion=2\nversionCode=2\nauthor=HyperOS-AVD\ndescription=Provider, SIM, optional GNSS and kernel capability guards\n',
             'post-fs-data.sh': script, 'service.sh': script, 'targets.conf': targets,
             'skip_mount': '', 'uninstall.sh': uninstall,
             'check-kernel-services.sh': PERF_SCRIPT.read_text()}
    root(config, f'mkdir -p {stage}/payload; chmod 777 {stage} {stage}/payload')
    for name, content in files.items():
        path = folder / name
        path.write_text(content)
        adb(config, 'push', str(path), stage + '/' + name, check=True, capture_output=True)
    for name in TARGETS:
        suffix = '.apk' if name in ('qualcomm', 'registration') else '.jar'
        adb(config, 'push', str(payload / (name + suffix)), stage + '/payload/' + name,
            check=True, capture_output=True)
    adb(config, 'push', str(payload / 'kernel-probe'), stage + '/kernel-probe', check=True, capture_output=True)
    root(config, f'chown -R 0:0 {stage}; chmod 755 {stage} {stage}/payload; '
         f'chmod 644 {stage}/*.json {stage}/module.prop {stage}/targets.conf {stage}/skip_mount {stage}/payload/*; '
         f'chmod 755 {stage}/*.sh {stage}/kernel-probe; '
         f'chcon -R u:object_r:system_file:s0 {stage}/payload')
    root(config, f'mv {stage} {MODULE}')
    print('Boot service module staged. Cold-boot to load framework fixes.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--payload', required=True, type=Path)
    args = parser.parse_args()
    install(runtime(), args.payload)


if __name__ == '__main__':
    main()
