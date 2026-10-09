#!/usr/bin/env python3
"""Stage reversible, source-pinned boot fixes on an owned hongkong OS4.0.18 AVD."""
import argparse
import hashlib
import json
from pathlib import Path
import shlex
import uuid

from module_lifecycle import (preserved as lifecycle_preserved, mutation_guard,
                              guarded_hook, refresh_hooks)
from common import ROOT, adb, runtime, sha256
from apply_flutter_fix import official, root
from patch_boot_services import TARGETS, AFTER, PROBE_SHA256
from phone_profile import profile_from_build

MODULE = '/data/adb/modules/hyperos_avd_boot_services'
PENDING = '/data/adb/modules_update/hyperos_avd_boot_services'
PERF_SCRIPT = Path(__file__).resolve().parent.parent / 'config/check_kernel_services.sh'
PACKAGES = {'qualcomm': 'com.qualcomm.location', 'registration': 'com.xiaomi.registration'}
LEGACY_KERNEL_SCRIPT_SHA256 = '4db262478ab0e60d5acd7351275f9b2dbf44f56177afcfaf90077f422956c044'
KERNEL_SCRIPT_SHA256 = '87a50a7509645ba6852db46eeca4637d596173ec001a8063928654361a6fdef9'
KERNEL_SCRIPT_HASHES = (LEGACY_KERNEL_SCRIPT_SHA256, KERNEL_SCRIPT_SHA256)
LEGACY_BOOT_HOOK_SHA256 = 'd8daa26990eb2ca760f4a5e6ff90c6f610cda616e081ba8f9220bee2e4683e88'
MODULE_PROPERTIES = ('id=hyperos_avd_boot_services\nname=HyperOS AVD boot service compatibility\n'
                     'version=2\nversionCode=2\nauthor=HyperOS-AVD\n'
                     'description=Provider, SIM, optional GNSS and kernel capability guards\n')
UNINSTALL_SCRIPT = '#!/system/bin/sh\n# No persistent user settings are changed by this module.\nexit 0\n'
UNINSTALL_SHA256 = '52372420c5c186fe57523d59692ca8f4d0f882874ec11f7cd79b0b0c71a60a27'
# Exact original installer outputs for empty/false/true/0/1 prior values. Other
# contents may be user hooks and are never interpreted or overwritten.
LEGACY_UNINSTALL_SHA256S = (
    '793eeb1e484f78b1d80e82163e7c8ad75bbbfa9a1b3949b9c0c930a942d82cae',
    '7b5ac9ec8f29631b68f9f041752f859c8c7278e6b5a1075a02e72566bb757397',
    '6f7c76c560d1194ced3c39a55396ae8339c765a67749888b8da16b0a33d4c92c',
    '4f22133caaa49c5b3011ab309426727f2ece9e261bbb480c7884dce3dfd63373',
    '37be6751a2d97076bf61ae8cc657b1dde3334e2d0fe485fb0a9de6ee7211337e',
)


def receipt():
    return {'schema': 1, 'firmware': 'OS4.0.18.0.XFRCNXM',
            'targets': {name: {'path': path, 'before': before, 'after': AFTER[name]}
                        for name, (path, before, _) in TARGETS.items()},
            'probe_sha256': PROBE_SHA256}


def boot_script(*, lifecycle=True):
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
    return (guarded_hook(script.replace('PROBE_HASH', PROBE_SHA256)) if lifecycle
            else script.replace('PROBE_HASH', PROBE_SHA256)), rows + '\n'


def _module_asset_hashes():
    """Authenticate legacy assets against host-reviewed bytes, not their receipt."""
    script, targets = boot_script(lifecycle=False)
    if hashlib.sha256(script.encode()).hexdigest() != LEGACY_BOOT_HOOK_SHA256:
        raise RuntimeError('Legacy boot hooks need an explicitly audited migration profile.')
    files = {'module.prop': MODULE_PROPERTIES,
             'manifest.json': json.dumps(receipt(), indent=2) + '\n',
             'targets.conf': targets, 'skip_mount': ''}
    expected = {name: hashlib.sha256(value.encode()).hexdigest() for name, value in files.items()}
    expected.update({'post-fs-data.sh': LEGACY_BOOT_HOOK_SHA256,
                     'service.sh': LEGACY_BOOT_HOOK_SHA256, 'kernel-probe': PROBE_SHA256})
    expected.update({'payload/' + name: after for name, after in AFTER.items()})
    return expected


def _lifecycle_state_script():
    return f'''if [ ! -e {MODULE} ] && [ ! -L {MODULE} ]; then
    printf 'absent\\n'
    exit 0
fi
[ -d {MODULE} ] && [ ! -L {MODULE} ] || exit 1
choice=0
for flag in disable remove; do
    if [ -e {MODULE}/"$flag" ] || [ -L {MODULE}/"$flag" ]; then
        printf 'preserved:%s\\n' "$flag"
        choice=1
    fi
done
[ "$choice" = 0 ] || exit 0
if [ -e {PENDING} ] || [ -L {PENDING} ]; then
    printf 'pending\\n'
else
    printf 'mutable\\n'
fi'''


def _migration_guard(expected_helper=None):
    """Recheck lifecycle, file identities and ownership immediately before rename."""
    commands = ['BB=/data/adb/ksu/bin/busybox', '[ -x "$BB" ] || exit 1',
                f'[ -d {MODULE} ] && [ ! -L {MODULE} ] || exit 1',
                f'[ -d {MODULE}/payload ] && [ ! -L {MODULE}/payload ] || exit 1',
                f'[ "$("$BB" stat -c %u {MODULE})" = 0 ] || exit 1',
                f'[ ! -e {PENDING} ] && [ ! -L {PENDING} ] || exit 1']
    for flag in ('disable', 'remove'):
        commands.append(f'[ ! -e {MODULE}/{flag} ] && [ ! -L {MODULE}/{flag} ] || exit 1')
    for key, value in (('ro.boot.hardware', 'ranchu'), ('ro.product.device', 'hongkong'),
                       ('ro.mi.os.version.incremental', receipt()['firmware'])):
        commands.append(f'[ "$(getprop {key})" = {shlex.quote(value)} ] || exit 1')
    for name, expected in _module_asset_hashes().items():
        path = shlex.quote(MODULE + '/' + name)
        commands.extend((f'[ -f {path} ] && [ ! -L {path} ] || exit 1',
                         f'[ "$("$BB" stat -c %u {path})" = 0 ] || exit 1',
                         (f'case "$("$BB" sha256sum {path} | "$BB" cut -d " " -f 1)" in {expected}|'
                          + hashlib.sha256(boot_script()[0].encode()).hexdigest() + ') ;; *) exit 1 ;; esac'
                          if name in ('post-fs-data.sh', 'service.sh') else
                          f'[ "$("$BB" sha256sum {path} | "$BB" cut -d " " -f 1)" = {expected} ] || exit 1')))
    commands.append(f'[ -x {MODULE}/kernel-probe ] || exit 1')
    helper = shlex.quote(MODULE + '/check-kernel-services.sh')
    commands.extend((f'[ -f {helper} ] && [ ! -L {helper} ] || exit 1',
                     f'[ "$("$BB" stat -c %u {helper})" = 0 ] || exit 1',
                     f'helper_hash=$("$BB" sha256sum {helper} | "$BB" cut -d " " -f 1)'))
    if expected_helper is None:
        commands.append(f'case "$helper_hash" in {"|".join(KERNEL_SCRIPT_HASHES)}) ;; *) exit 1 ;; esac')
    else:
        if expected_helper not in KERNEL_SCRIPT_HASHES:
            raise RuntimeError('Unknown kernel helper migration identity.')
        commands.append(f'[ "$helper_hash" = {expected_helper} ] || exit 1')
    return '\n'.join(commands) + '\n'


def _atomic_replace_script(relative, source, before, helper_hash=None):
    if relative not in ('check-kernel-services.sh', 'uninstall.sh'):
        raise RuntimeError('Unknown boot service migration target.')
    after = hashlib.sha256(source).hexdigest()
    marker = 'HYPEROS_BOOT_HOOK_' + uuid.uuid4().hex
    stage = shlex.quote(MODULE + '/.kernel-helper-' + uuid.uuid4().hex)
    target = shlex.quote(MODULE + '/' + relative)
    guard = (_migration_guard(helper_hash) +
             f'[ -f {target} ] && [ ! -L {target} ] || exit 1\n'
             f'[ "$("$BB" stat -c %u {target})" = 0 ] || exit 1\n'
             f'[ "$("$BB" sha256sum {target} | "$BB" cut -d " " -f 1)" = {before} ] || exit 1\n')
    # Preserve the existing mode, owner and SELinux context. Only the helper is
    # atomically replaced; APK/JAR payloads, hooks and user files are untouched.
    return (guard + f'''umask 077
stage={stage}
"$BB" mkdir -m 700 "$stage"
trap '"$BB" rm -rf "$stage"' EXIT HUP INT TERM
"$BB" cp -p {target} "$stage/replacement.sh"
cat > "$stage/replacement.sh" <<'{marker}'
''' + source.decode() + f'''{marker}
[ "$("$BB" sha256sum "$stage/replacement.sh" | "$BB" cut -d " " -f 1)" = {after} ] || exit 1
context=$(/system/bin/ls -Zd {target} | "$BB" awk '{{print $1}}')
printf '%s\\n' "$context" | "$BB" grep -Eq '^u:object_r:[a-zA-Z0-9_]+:s0$' || exit 1
/system/bin/chcon "$context" "$stage/replacement.sh"
''' + guard + f'''"$BB" mv -f "$stage/replacement.sh" {target}
"$BB" rmdir "$stage"
trap - EXIT HUP INT TERM
printf 'migrated\\n'
''')


def _kernel_migration_script():
    source = PERF_SCRIPT.read_bytes()
    if hashlib.sha256(source).hexdigest() != KERNEL_SCRIPT_SHA256:
        raise RuntimeError('Kernel helper source differs from its audited migration profile.')
    return _atomic_replace_script('check-kernel-services.sh', source,
                                  LEGACY_KERNEL_SCRIPT_SHA256, LEGACY_KERNEL_SCRIPT_SHA256)


def _preserved_state(config):
    state = root(config, _lifecycle_state_script())
    if state == 'absent':
        return {'migrated': False, 'present': False}
    flags = state.splitlines()
    if flags and all(flag in ('preserved:disable', 'preserved:remove') for flag in flags):
        return {'migrated': False, 'preserved': [flag.split(':', 1)[1] for flag in flags]}
    if state == 'pending':
        return {'migrated': False, 'pending': True, 'deferred': True}
    if state != 'mutable':
        raise RuntimeError('Unknown boot service module lifecycle state.')
    return None


def migrate_kernel_helper(config):
    """Remove the known legacy iorap opt-out without disabling framework repairs.

    The caller supplies its registered guest configuration. No hooks execute and
    no service, persistent property or restart is requested. Unknown ownership,
    payloads, hooks, probe or helper contents fail closed without replacement.
    """
    preserved = _preserved_state(config)
    if preserved is not None:
        return preserved
    helper = root(config, _migration_guard() + 'printf "%s\\n" "$helper_hash"')
    if helper == KERNEL_SCRIPT_SHA256:
        return {'migrated': False, 'present': True, 'reused': True}
    if helper != LEGACY_KERNEL_SCRIPT_SHA256:
        raise RuntimeError('Unknown boot service helper identity.')
    if root(config, _kernel_migration_script()) != 'migrated':
        raise RuntimeError('Kernel helper migration did not complete.')
    return {'migrated': True, 'present': True, 'reboot_required': True}


def migrate_uninstall_hook(config):
    """Retire only known legacy preference restoration; never run the hook."""
    preserved = _preserved_state(config)
    if preserved is not None:
        return preserved
    target = shlex.quote(MODULE + '/uninstall.sh')
    current = root(config, _migration_guard() + f'''if [ ! -e {target} ] && [ ! -L {target} ]; then
    printf 'absent\\n'
else
    [ -f {target} ] && [ ! -L {target} ] || exit 1
    [ "$("$BB" stat -c %u {target})" = 0 ] || exit 1
    "$BB" sha256sum {target} | "$BB" cut -d ' ' -f 1
fi''')
    if current == 'absent':
        return {'migrated': False, 'present': True, 'hook_present': False}
    if current == UNINSTALL_SHA256:
        return {'migrated': False, 'present': True, 'reused': True}
    if current not in LEGACY_UNINSTALL_SHA256S:
        return {'migrated': False, 'present': True, 'unsupported': True,
                'reason': 'Unknown uninstall hook is preserved; historical preference restoration may remain.'}
    source = UNINSTALL_SCRIPT.encode()
    if hashlib.sha256(source).hexdigest() != UNINSTALL_SHA256:
        raise RuntimeError('Uninstall source differs from its audited migration profile.')
    if root(config, _atomic_replace_script('uninstall.sh', source, current)) != 'migrated':
        raise RuntimeError('Uninstall hook migration did not complete.')
    return {'migrated': True, 'present': True}


def migrate_lifecycle_hooks(config):
    """Upgrade only exact authenticated legacy hooks without executing them."""
    choice = _preserved_state(config)
    if choice is not None:
        return choice
    root(config, _migration_guard())
    legacy = boot_script(lifecycle=False)[0]
    current = boot_script()[0]
    refresh_hooks(root, config, MODULE, {name: (legacy, current)
                                       for name in ('post-fs-data.sh', 'service.sh')})
    return {'present': True, 'lifecycle_verified': True}


def install(config, payload):
    official(config)
    lifecycle = lifecycle_preserved(root, config, MODULE)
    if lifecycle:
        return lifecycle
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
        for relative, expected in (('kernel-probe', (PROBE_SHA256,)),
                                   ('check-kernel-services.sh', KERNEL_SCRIPT_HASHES)):
            if root(config, f'sha256sum {MODULE}/{relative}').split()[0] not in expected:
                raise RuntimeError('Existing boot service policy differs: ' + relative)
    if root(config, f'if [ -e {MODULE}/disable ] || [ -L {MODULE}/disable ] || '
            f'[ -e {MODULE}/remove ] || [ -L {MODULE}/remove ]; then echo yes; fi'):
        print('Boot service module is disabled; preserving this choice.')
        return
    if previous == receipt():
        result = migrate_kernel_helper(config)
        result['uninstall_hook'] = migrate_uninstall_hook(config)
        result['lifecycle_hooks'] = migrate_lifecycle_hooks(config)
        print('Verified boot service helper migrated.' if result.get('migrated')
              else 'Verified boot service module is already staged; lifecycle is preserved.')
        return result
    if root(config, f'if [ -e {PENDING} ] || [ -L {PENDING} ]; then echo yes; fi'):
        print('Boot service update is pending; preserving KernelSU staging.')
        return {'installed': False, 'pending': True, 'deferred': True}
    stage = '/data/local/tmp/hyperos-avd-boot-services-stage'
    if root(config, f'if [ -e {stage} ]; then echo yes; fi'):
        raise RuntimeError('A boot service staging directory already exists.')
    folder = ROOT / 'work/boot-service-module'
    folder.mkdir(parents=True, exist_ok=True)
    script, targets = boot_script()
    files = {'manifest.json': json.dumps(receipt(), indent=2) + '\n',
             'module.prop': MODULE_PROPERTIES,
             'post-fs-data.sh': script, 'service.sh': script, 'targets.conf': targets,
             'skip_mount': '', 'uninstall.sh': UNINSTALL_SCRIPT,
             'check-kernel-services.sh': PERF_SCRIPT.read_text()}
    root(config, 'set -e\n' + mutation_guard(MODULE) + f'mkdir -p {stage}/payload; chmod 777 {stage} {stage}/payload')
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
    root(config, f'[ ! -e {MODULE} ] && [ ! -L {MODULE} ] && '
         f'[ ! -e {PENDING} ] && [ ! -L {PENDING} ] || exit 1\nmv {stage} {MODULE}')
    print('Boot service module staged. Cold-boot to load framework fixes.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--payload', type=Path)
    action.add_argument('--migrate-kernel-helper', action='store_true')
    args = parser.parse_args()
    if args.migrate_kernel_helper:
        print(json.dumps(migrate_kernel_helper(runtime()), sort_keys=True))
    else:
        install(runtime(), args.payload)


if __name__ == '__main__':
    main()
