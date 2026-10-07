#!/usr/bin/env python3
"""Install the source-pinned rear double-tap KernelSU startup service."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shlex
import subprocess

from common import ROOT, adb, runtime
from apply_flutter_fix import official, root
import rear_display_config as rear
from rear_display_wake import CLASS, JAVA_SOURCE, LAUNCHER

MODULE_ID = 'hyperos_avd_rear_display'
MODULE = '/data/adb/modules/' + MODULE_ID
REVISION = 2
EXPECTED_WAKE_MANIFEST = {
    'schema': 1, 'revision': 2, 'display_id': 1, 'display_group_id': 1,
    'display_unique_id': 'local:4619827551948147201',
    'input_name': 'virtio_input_multi_touch_7', 'axis_range': [0, 32767],
    'panel_geometry': [912, 596], 'max_tap_ms': 250, 'max_gap_ms': 400,
    'movement_pixels': 24, 'tap_distance_pixels': 100, 'state_poll_ms': 20,
    'require_down_after_observed_sleep': True,
    'sleep_states': ['OFF', 'DOZE', 'DOZE_SUSPEND'],
    'source_sha256': 'ff380a811addde053dbc9272b17ad940a39843cf199db4e9f279c0541e2c6172',
    'jar_path': '/system_ext/framework/rear-display-wake.jar',
    'jar_sha256': 'd38aef457215fe9f5f397af2e58ece05c3baa61f742a8a9e3d7a3b577bd5c077',
    'script_path': '/system_ext/bin/rear-display-wake',
    'script_sha256': 'f98ea740ae450ea4e2e0815c7e74f698ee2fe5cfcd6a367247f2f91021ed16e7',
}
MODULE_PROP = (f'id={MODULE_ID}\nname=HyperOS AVD Rear Display Wake\nversion=2\n'
               'versionCode=2\nauthor=HyperOS-AVD\n'
               'description=Verified rear-panel double-tap wake using original Xiaomi power behavior\n')
OWNERSHIP_SCRIPT = r'''owned() {
    case "$1" in ''|*[!0-9]*) return 1 ;; esac
    local directory="$PROC/$1"
    [ -r "$directory/cmdline" ] && [ -r "$directory/environ" ] || return 1
    "$BB" tr '\000' '\n' < "$directory/cmdline" | "$BB" grep -Fxq "$CLASS" || return 1
    "$BB" tr '\000' '\n' < "$directory/environ" | "$BB" grep -Fxq "CLASSPATH=$JAR" || return 1
    "$BB" awk '$1 == "Uid:" { found=1; if ($2 != 0 || $3 != 0 || $4 != 0 || $5 != 0) exit 1 } END { if (!found) exit 1 }' "$directory/status"
}
'''


def digest(data):
    return hashlib.sha256(data).hexdigest()


def encode(value):
    return (json.dumps(value, indent=2) + '\n').encode()


def same(actual, expected):
    """JSON equality also distinguishes bool/int and int/float substitutions."""
    return json.dumps(actual, sort_keys=True) == json.dumps(expected, sort_keys=True)


def validate_build(build):
    """Reject unsupported metadata before contacting or changing a guest."""
    from phone_profile import profile_from_build
    if (not isinstance(build, dict) or build.get('source') != 'official-hongkong-ota'
            or build.get('hyperos') != rear.VERSION
            or build.get('archive_sha256') != rear.ARCHIVE_SHA256):
        raise RuntimeError('Rear wake requires the verified official hongkong 4.0.18.0 source.')
    rear.validate_profile(profile_from_build(build))
    rear.runtime_options(build)
    marker = build.get('rear_display')
    overlay = marker.get('overlay_sha256') if isinstance(marker, dict) else None
    if not isinstance(overlay, str) or not re.fullmatch('[0-9a-f]{64}', overlay):
        raise RuntimeError('Invalid rear-display resource hash.')
    displays = []
    for original, physical, geometry in zip(rear.SOURCE_IDS, rear.PHYSICAL_IDS,
                                            ((1120, 2436, 480), (912, 596, 450))):
        source = f'product/etc/displayconfig/display_id_{original}.xml'
        displays.append({'source_physical_id': original, 'physical_id': physical,
                         'unique_id': 'local:' + str(physical),
                         'config': f'/product/etc/displayconfig/display_id_{physical}.xml',
                         'config_sha256': rear.SOURCE_SHA256[source],
                         'width': geometry[0], 'height': geometry[1], 'density': geometry[2]})
    expected = {'schema': 1, 'hyperos': rear.VERSION, 'archive_sha256': rear.ARCHIVE_SHA256,
                'source_sha256': dict(rear.SOURCE_SHA256), 'mapping_sha256': rear.MAPPING_SHA256,
                'overlay': rear.OVERLAY, 'overlay_path': '/' + rear.OVERLAY_PATH,
                'overlay_sha256': overlay, 'overlay_priority': rear.PRIORITY,
                'source_overlay_priority': 1000, 'requires_runtime_resource_verification': True,
                'overridden_resources': ['config_displayUniqueIdArray'], 'displays': displays,
                'rear_safe_inset_left': 296, 'rear_corner_radius': 106}
    if not same(marker, expected):
        raise RuntimeError('Unverified rear-display resource identity.')
    if (not same(build.get('rear_display_wake_fix'), EXPECTED_WAKE_MANIFEST)
            or digest(JAVA_SOURCE.encode()) != EXPECTED_WAKE_MANIFEST['source_sha256']
            or digest(LAUNCHER) != EXPECTED_WAKE_MANIFEST['script_sha256']):
        raise RuntimeError('Unverified rear-display wake assets or source.')
    return expected


def display_ready(text):
    """Require all identifiers on one logical DisplayInfo line."""
    unique = re.escape(EXPECTED_WAKE_MANIFEST['display_unique_id'])
    return any('DisplayInfo' in line
               and re.search(r'\bdisplayId\s*[=:]?\s*1\b', line)
               and re.search(r'\bdisplayGroupId\s*[=:]?\s*1\b', line)
               and re.search(r'\buniqueId\s*[=:]?\s*["\']?' + unique + r'(?=["\',\s}]|$)', line)
               for line in text.splitlines())


def boot_script(manifest):
    """Start only verified baked code; never mount files or change user state."""
    wake = manifest['wake']
    values = {'JAR': wake['jar_path'],
              'LAUNCHER': wake['script_path'], 'MARKER': manifest['marker']['path'],
              'JAR_HASH': wake['jar_sha256'], 'LAUNCHER_HASH': wake['script_sha256'],
              'MARKER_HASH': manifest['marker']['sha256'],
              'MANIFEST_HASH': digest(encode(manifest)), 'PROP_HASH': digest(MODULE_PROP.encode()),
              'CLASS': CLASS}
    assignments = ''.join(key + '=' + shlex.quote(value) + '\n' for key, value in values.items())
    return '''#!/system/bin/sh
MODDIR=${0%/*}
BB=/data/adb/ksu/bin/busybox
PROC=/proc
''' + assignments + r'''blocked() { [ -e "$MODDIR/disable" ] || [ -L "$MODDIR/disable" ] || [ -e "$MODDIR/remove" ] || [ -L "$MODDIR/remove" ]; }
blocked && exit 0
[ -x "$BB" ] || exit 1
count=0
while [ "$(getprop sys.boot_completed)" != 1 ]; do
    blocked && exit 0
    count=$((count + 1)); [ "$count" -lt 300 ] || exit 1
    sleep 1
done
hyperos_file_sha() { "$BB" sha256sum "$1" 2>/dev/null | "$BB" cut -d ' ' -f 1; }
guard() {
    [ "$(id -u)" = 0 ] && [ "$(getprop ro.boot.hardware)" = ranchu ] || return 1
    [ "$(getprop ro.product.device)" = hongkong ] || return 1
    [ "$(getprop ro.mi.os.version.incremental)" = OS4.0.18.0.XFRCNXM ] || return 1
    [ "$(hyperos_file_sha "$JAR")" = "$JAR_HASH" ] && [ "$(hyperos_file_sha "$LAUNCHER")" = "$LAUNCHER_HASH" ] || return 1
    [ "$(hyperos_file_sha "$MARKER")" = "$MARKER_HASH" ] || return 1
    [ "$(hyperos_file_sha "$MODDIR/manifest.json")" = "$MANIFEST_HASH" ] || return 1
    [ "$(hyperos_file_sha "$MODDIR/module.prop")" = "$PROP_HASH" ] || return 1
    [ -f "$MODDIR/skip_mount" ] || return 1
}
guard || exit 1
''' + OWNERSHIP_SCRIPT + r'''find_owned() {
    local directory
    for directory in "$PROC"/[0-9]*; do
        [ -d "$directory" ] || continue
        if owned "${directory##*/}"; then echo "${directory##*/}"; return 0; fi
    done
    return 1
}
LOCK="$MODDIR/startup.lock"
boot=$(cat "$PROC/sys/kernel/random/boot_id") || exit 1
if ! mkdir "$LOCK" 2>/dev/null; then
    # A concurrent start gets time to publish its lock owner. Only the two
    # private lock records are removed when their original service is gone.
    sleep 1
    oldpid=$(cat "$LOCK/pid" 2>/dev/null)
    oldboot=$(cat "$LOCK/boot" 2>/dev/null)
    [ -n "$oldpid" ] && [ -n "$oldboot" ] || exit 1
    case "$oldpid" in *[!0-9]*) exit 1 ;; esac
    if [ "$oldboot" = "$boot" ] && [ -r "$PROC/$oldpid/cmdline" ] &&
            "$BB" tr '\000' '\n' < "$PROC/$oldpid/cmdline" | "$BB" grep -Fxq "$MODDIR/service.sh"; then
        exit 0
    fi
    rm -f "$LOCK/pid" "$LOCK/boot" || exit 1
    rmdir "$LOCK" || exit 1
    mkdir "$LOCK" || exit 1
fi
echo "$$" > "$LOCK/pid"
echo "$boot" > "$LOCK/boot"
trap 'rm -f "$LOCK/pid" "$LOCK/boot"; rmdir "$LOCK" 2>/dev/null' EXIT
blocked && exit 0
guard || exit 1
pid=$(find_owned)
if [ -n "$pid" ]; then echo "$pid" > "$MODDIR/daemon.pid"; exit 0; fi
attempt=0
while [ "$attempt" -lt 30 ]; do
    blocked && exit 0
    guard || exit 1
    attempt=$((attempt + 1))
    "$BB" nohup "$LAUNCHER" >> "$MODDIR/wake.log" 2>&1 < /dev/null &
    pid=$!
    sleep 2
    if owned "$pid"; then echo "$pid" > "$MODDIR/daemon.pid"; exit 0; fi
    # A display missing at boot makes the Java entrypoint exit before opening
    # getevent. Retry boundedly; the Java Binder validates display 1 identity.
done
echo "Rear wake startup failed after 30 attempts" >> "$MODDIR/wake.log"
exit 1
'''


def _hashes(config, expected):
    output = root(config, 'sha256sum ' + ' '.join(shlex.quote(path) for path in expected))
    actual = {}
    for line in output.splitlines():
        fields = line.split(None, 1)
        if len(fields) == 2:
            actual[fields[1].strip().removeprefix('*')] = fields[0]
    if actual != expected:
        raise RuntimeError('Rear display file checksum mismatch; no activation was performed.')


def _start(config):
    root(config, 'set -e\n' +
         f'if [ -e {MODULE}/disable ] || [ -L {MODULE}/disable ] || '
         f'[ -e {MODULE}/remove ] || [ -L {MODULE}/remove ]; then exit 0; fi\n'
         f'nohup sh {MODULE}/service.sh >> {MODULE}/startup.log 2>&1 < /dev/null &')
    verify_start(config)


def readiness_script():
    """Confirm the service receipt belongs to the verified root daemon."""
    return ('MODDIR=' + shlex.quote(MODULE) + '\nBB=/data/adb/ksu/bin/busybox\nPROC=/proc\n'
            'JAR=' + shlex.quote(EXPECTED_WAKE_MANIFEST['jar_path']) + '\nCLASS=' + shlex.quote(CLASS) +
            '\n' + OWNERSHIP_SCRIPT + r'''attempt=0
while [ "$attempt" -lt 10 ]; do
    if [ -e "$MODDIR/disable" ] || [ -L "$MODDIR/disable" ] ||
            [ -e "$MODDIR/remove" ] || [ -L "$MODDIR/remove" ]; then exit 0; fi
    pid=$(cat "$MODDIR/daemon.pid" 2>/dev/null || :)
    if owned "$pid"; then echo ready; exit 0; fi
    attempt=$((attempt + 1))
    sleep 1
done
echo "Rear display wake daemon was not ready after 10 seconds" >&2
exit 1
''')


def verify_start(config):
    try:
        root(config, readiness_script())
    except RuntimeError as error:
        raise RuntimeError('Rear display wake daemon failed its 10-second readiness check.') from error


def install(config, build=None):
    """Install without compilation; optionally accept the caller's build data."""
    if build is None:
        build = json.loads((ROOT / 'local/build.json').read_text())
    marker = validate_build(build)
    if (not isinstance(config.get('name'), str) or not config['name']
            or any(c in config['name'] for c in '\n\r\0')):
        raise RuntimeError('Invalid configured AVD name.')
    official(config)
    properties = root(config, '\n'.join('getprop ' + key for key in (
        'ro.boot.hardware', 'ro.product.device', 'ro.mi.os.version.incremental', 'ro.boot.qemu.avd_name')))
    if properties.splitlines() != ['ranchu', 'hongkong', 'OS' + rear.VERSION, config['name']]:
        raise RuntimeError('Rear display installer refused a different guest identity.')
    marker_hash = digest(encode(marker))
    wake = dict(EXPECTED_WAKE_MANIFEST)
    _hashes(config, {'/' + rear.MARKER_PATH: marker_hash,
                     wake['jar_path']: wake['jar_sha256'], wake['script_path']: wake['script_sha256']})
    if not display_ready(root(config, 'dumpsys display')):
        raise RuntimeError('Verified physical rear display 1 is not ready.')
    manifest = {'schema': 1, 'revision': REVISION, 'module_id': MODULE_ID,
                'firmware': {'source': build['source'], 'hyperos': rear.VERSION,
                             'incremental': 'OS' + rear.VERSION, 'archive_sha256': rear.ARCHIVE_SHA256,
                             'hardware': 'ranchu', 'device': 'hongkong'},
                'marker': {'path': '/' + rear.MARKER_PATH, 'sha256': marker_hash}, 'wake': wake}
    files = {'manifest.json': encode(manifest), 'module.prop': MODULE_PROP.encode(),
             'service.sh': boot_script(manifest).encode(), 'skip_mount': b''}
    existing = root(config, f'if [ -L {MODULE} ]; then echo foreign; elif [ -d {MODULE} ]; then echo directory; elif [ -e {MODULE} ]; then echo foreign; fi')
    if existing:
        if existing != 'directory':
            raise RuntimeError('Refused a foreign rear-display module path.')
        saved = root(config, f'cat {MODULE}/manifest.json')
        try:
            previous = json.loads(saved)
        except ValueError as error:
            raise RuntimeError('Refused an invalid existing rear-display manifest.') from error
        if not same(previous, manifest) or root(config, f'cat {MODULE}/module.prop') != MODULE_PROP.strip():
            raise RuntimeError('Refused a foreign existing rear-display module.')
        _hashes(config, {MODULE + '/' + name: digest(data) for name, data in files.items()})
        flags = root(config, f'for flag in disable remove; do if [ -e {MODULE}/$flag ] || [ -L {MODULE}/$flag ]; then echo "$flag"; fi; done')
        if flags:
            print('Rear display module is disabled or marked for removal; preserving this choice.', flush=True)
            return manifest
        _start(config)
        return manifest
    stage = '/data/adb/hyperos-rear-display-stage-v2-' + digest(files['manifest.json'])[:12]
    remote = '/data/local/tmp/hyperos-rear-display-' + digest(files['manifest.json'])[:12] + '-'
    paths = [stage, *(remote + name for name in files)]
    if root(config, '\n'.join(f'if [ -e {shlex.quote(path)} ] || [ -L {shlex.quote(path)} ]; then echo occupied; fi' for path in paths)):
        raise RuntimeError('Refused an existing rear-display staging path.')
    folder = ROOT / 'work/rear-display-fix'
    folder.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        (folder / name).write_bytes(data)
    root(config, f'set -e\nmkdir -p /data/adb/modules\nmkdir {stage}')
    for name, data in files.items():
        target = remote + name
        adb(config, 'push', str(folder / name), target, check=True, capture_output=True, timeout=30)
        mode = '755' if name == 'service.sh' else '644'
        root(config, f'set -e\ntest "$(sha256sum {target} | cut -d " " -f 1)" = {digest(data)}\n'
                     f'cp {target} {stage}/{name}\nchmod {mode} {stage}/{name}\nrm {target}')
    _hashes(config, {stage + '/' + name: digest(data) for name, data in files.items()})
    root(config, f'set -e\nif [ -e {MODULE} ] || [ -L {MODULE} ]; then exit 1; fi\n'
                 f'mv {stage} {MODULE}\nsync')
    _start(config)
    return manifest


if __name__ == '__main__':
    argparse.ArgumentParser(description=__doc__).parse_args()
    try:
        install(runtime())
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as error:
        raise SystemExit(str(error))
