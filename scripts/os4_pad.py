"""Owned yingtian test profile; keep phone defaults and workspaces separate."""
import hashlib
import json
from pathlib import Path
import re

SOURCE = 'official-yingtian-ota'
NAME = 'HyperOS_4_Pad9ProMax_API_37'
PORT = 5582
DEFAULT_MEMORY = 4096
FINDDEVICE_SHA256 = 'c8fe791d76f8da8bcb23412aebaf3470a3ae4202543c9a64091e1c24cb63aad4'
CONFIG = Path(__file__).resolve().parent.parent / 'config'
PROFILE = json.loads((CONFIG / 'yingtian-identity.json').read_text())
MODEL_XML = (CONFIG / 'yingtian.xml').read_bytes()
SERIAL_FILE = '/data/adb/hyperos-avd-pad/serial.json'
SERIAL_EARLY = '/data/adb/post-fs-data.d/hyperos-avd-pad-serial.sh'
COLOR_STAMP = '/data/local/tmp/hyperos-avd-pad-color-defaults-v1'
DEFAULTS_STAMP = '/data/local/tmp/hyperos-avd-pad-settings-defaults-v1'
THERMAL_EARLY = '/data/adb/post-fs-data.d/hyperos-avd-pad-thermal.sh'
# Authenticated combined HWUI/thermal/profile hook shipped with the yingtian
# profile. Its payload stays untouched: a running process can still map it.
LEGACY_THERMAL_HWUI_SHA256 = '25f76116c3f217f45cb9b3b2d7883982698136bce8d23a10d5136f319f8cd9f1'


def _compatible_serial_firmware(previous, current):
    """Allow forward OS4 updates in the same tablet firmware family."""
    pattern = r'OS4\.([0-9]+)\.([0-9]+)\.([0-9]+)\.([A-Z0-9]+)'
    before = re.fullmatch(pattern, previous) if isinstance(previous, str) else None
    after = re.fullmatch(pattern, current) if isinstance(current, str) else None
    return (before is not None and after is not None and before[4] == after[4]
            and tuple(map(int, before.groups()[:3])) <= tuple(map(int, after.groups()[:3])))


def serial_manifest(previous=None):
    """Keep one simulated Xiaomi-style identifier per tablet userdata."""
    from apply_navigation_fix import simulated_serial
    if previous is not None:
        if (not isinstance(previous, dict)
                or set(previous) != {'revision', 'source', 'hyperos', 'serial_number'}
                or previous.get('revision') != 1 or previous.get('source') != SOURCE
                or not _compatible_serial_firmware(previous.get('hyperos'), PROFILE['hyperos'])):
            raise RuntimeError('Unknown saved tablet serial profile; identifier preserved.')
    serial = simulated_serial(previous)
    result = {'revision': 1, 'source': SOURCE, 'hyperos': PROFILE['hyperos'],
              'serial_number': serial}
    if previous is not None:
        # Firmware is provenance, not the serial's lifetime. Keep the saved
        # record byte-for-byte compatible across forward same-device upgrades.
        result = dict(previous)
        result['serial_number'] = serial
    return result


def serial_script(state):
    """Set only serial fields before Android caches device identifiers."""
    return _serial_script(state, PROFILE['hyperos'])


def _serial_script(state, firmware):
    """Render a known current or previous guard for owned-script migration."""
    import shlex
    state = serial_manifest(state)
    if not _compatible_serial_firmware(firmware, PROFILE['hyperos']):
        raise RuntimeError('Unknown tablet serial startup firmware; files preserved.')
    guards = {'ro.boot.hardware': 'ranchu', 'ro.product.device': PROFILE['device'],
              'ro.mi.os.version.incremental': firmware}
    lines = ['#!/system/bin/sh', 'set -eu',
             '# HyperOS-AVD tablet serial v1; preserve the saved userdata identity.']
    lines += ['[ "$(getprop ' + key + ')" = ' + shlex.quote(value) + ' ] || exit 1'
              for key, value in guards.items()]
    lines += ['RESETPROP=/data/adb/ksu/bin/resetprop', '[ -x "$RESETPROP" ] || exit 1',
              'serial=' + shlex.quote(state['serial_number'])]
    for key in ('ro.serialno', 'ro.boot.serialno', 'ro.ril.oem.psno'):
        lines += ['if [ "$(getprop ' + key + ')" != "$serial" ]; then',
                  '    "$RESETPROP" -n ' + key + ' "$serial"', 'fi']
    return '\n'.join(lines) + '\n'


def apply_serial(config):
    """Install a version-guarded early script without phone identity changes."""
    import shlex
    import uuid
    from common import ROOT
    from apply_flutter_fix import official, root
    official(config, sources=(SOURCE,))
    for key, value in {'ro.boot.hardware': 'ranchu', 'ro.product.device': PROFILE['device'],
                       'ro.mi.os.version.incremental': PROFILE['hyperos']}.items():
        if root(config, 'getprop ' + key) != value:
            raise RuntimeError('Tablet serial refused a different device profile.')

    def read_owned(path):
        return root(config, 'if [ -e ' + path + ' ] || [ -L ' + path + ' ]; then\n'
                    '  [ -f ' + path + ' ] && [ -s ' + path + ' ] && [ ! -L ' + path + ' ] || exit 1\n'
                    '  cat ' + path + '\nfi')

    previous = read_owned(SERIAL_FILE)
    state = serial_manifest(json.loads(previous) if previous else None)
    script = serial_script(state)
    saved_script = read_owned(SERIAL_EARLY)
    if saved_script and saved_script != script.strip():
        guards = re.findall(r'^\[ "\$\(getprop ro\.mi\.os\.version\.incremental\)" = '
                            r'([^ ]+) \] \|\| exit 1$', saved_script, re.M)
        if (len(guards) != 1 or not _compatible_serial_firmware(guards[0], PROFILE['hyperos'])
                or saved_script != _serial_script(state, guards[0]).strip()):
            raise RuntimeError('Unknown tablet serial startup script; files preserved.')
    if not previous or saved_script != script.strip():
        suffix = '.stage-' + uuid.uuid4().hex
        writes = ['mkdir -p ' + SERIAL_FILE.rsplit('/', 1)[0] + ' ' + SERIAL_EARLY.rsplit('/', 1)[0],
                  'umask 077', 'trap ' + shlex.quote('rm -f ' + SERIAL_FILE + suffix +
                                                   ' ' + SERIAL_EARLY + suffix) + ' EXIT']
        pending = [(SERIAL_EARLY, script, '700')]
        if not previous:
            pending.insert(0, (SERIAL_FILE, json.dumps(state, indent=2) + '\n', '600'))
        for path, data, mode in pending:
            stage = path + suffix
            writes += ['[ ! -e ' + stage + ' ] && [ ! -L ' + stage + ' ] || exit 1',
                       'printf %s ' + shlex.quote(data) + ' > ' + stage,
                       'chmod ' + mode + ' ' + stage]
            if path == SERIAL_EARLY:
                writes += ['sh -n ' + stage]
            writes += ['mv -f ' + stage + ' ' + path]
        root(config, '\n'.join(writes))
    root(config, 'sh ' + SERIAL_EARLY)
    for key in ('ro.serialno', 'ro.boot.serialno', 'ro.ril.oem.psno'):
        if root(config, 'getprop ' + key) != state['serial_number']:
            raise RuntimeError('Tablet serial verification failed: ' + key)
    (ROOT / 'local/pad-serial.json').write_text(json.dumps(state, indent=2) + '\n')
    print('Pad simulated serial ready: ' + state['serial_number'], flush=True)
    return state


def display_settings_script():
    """Seed display defaults once, retaining legacy and later user choices."""
    from os4_defaults import AWAKE_STAMP
    return (f'if [ ! -e {DEFAULTS_STAMP} ]; then\n'
            # Existing color/awake stamps prove that an earlier defaults run
            # initialized this userdata; migrate without overwriting choices.
            f'  if [ ! -e {COLOR_STAMP} ] && [ ! -e {AWAKE_STAMP} ]; then\n'
            '    settings put global stay_on_while_plugged_in 7 || exit 1\n'
            '    settings put system screen_off_timeout 2147483647 || exit 1\n'
            '    settings put secure sleep_timeout -1 || exit 1\n'
            '    settings put global development_settings_enabled 1 || exit 1\n'
            '  fi\n'
            f'  touch {DEFAULTS_STAMP} || exit 1\n'
            'fi\n'
            f'if [ ! -e {COLOR_STAMP} ]; then\n'
            '  settings put system display_color_mode 0 || exit 1\n'
            f'  touch {COLOR_STAMP} || exit 1\n'
            'fi\n'
            'setprop persist.sys.gradient_blur_perf false\n'
            'input keyevent 224')


def profile_properties_script():
    """Restore public tablet identity and shared log levels before zygote."""
    import re
    import shlex
    from os4_defaults import LOG_TAGS
    values = dict(PROFILE['properties'])
    values.update({'log.tag.' + tag: 'S' for tag in LOG_TAGS})
    lines = ['# Restore public OTA identity without changing ranchu HAL selectors.']
    for key, value in values.items():
        if (not re.fullmatch(r'[A-Za-z0-9_.]+', key) or not isinstance(value, str)
                or any(key.startswith(prefix) for prefix in
                       ('ro.hardware', 'ro.boot.', 'ro.vndk.', 'ro.vendor.api_level'))
                or key in ('ro.board.platform', 'ro.serialno', 'ro.ril.oem.psno')
                or '\n' in value or '\r' in value):
            raise RuntimeError('Unsupported tablet public identity property: ' + key)
        lines += ['key=' + shlex.quote(key), 'value=' + shlex.quote(value),
                  'current=$(getprop "$key")',
                  'if [ "$current" != "$value" ]; then',
                  '    if [ "${#value}" -ge 91 ] && [ -n "$current" ]; then',
                  '        /data/adb/ksu/bin/resetprop -d "$key" || exit 1',
                  '    fi',
                  '    /data/adb/ksu/bin/resetprop -n "$key" "$value" || exit 1',
                  'fi']
    return '\n'.join(lines) + '\n'


def template(data):
    values = dict(line.split('=', 1) for line in data.splitlines() if '=' in line)
    values.update({'avd.ini.displayname': 'Xiaomi Pad 9 Pro Max - HyperOS 4 Test',
                   'target': 'android-37.0', 'hw.ramSize': str(DEFAULT_MEMORY),
                   'hw.cpu.ncore': '4', 'hw.lcd.width': '2272',
                   'hw.lcd.height': '3408', 'hw.lcd.density': '400',
                   'hw.initialOrientation': 'portrait',
                   'hw.gsmModem': 'no', 'hw.audioOutput': 'yes',
                   'hw.camera.back': 'virtualscene', 'hw.camera.front': 'emulated',
                   'hw.device.name': 'yingtian', 'hw.device.manufacturer': 'Xiaomi',
                   'hw.device.hash2': '', 'disk.dataPartition.size': '6G'})
    return ''.join(key + '=' + value + '\n' for key, value in values.items())


def properties(data):
    """Keep original public identity; select only the ranchu hardware backends."""
    values = dict(PROFILE['properties'])
    values.update({'ro.sf.lcd_density': '400', 'ro.miui.product.home': 'com.miui.home',
                   # Ranchu exposes the portrait panel without the physical
                   # mounting transform; WindowManager performs its rotation.
                   'ro.surface_flinger.primary_display_orientation': 'ORIENTATION_0',
                   'persist.sys.miui_resolution': '2272,3408,400',
                   'ro.debuggable': '0', 'ro.mediaserver.64b.enable': 'true',
                   'debug.hwui.renderer': 'skiavk',
                   'debug.renderengine.backend': 'skiavkthreaded',
                   'persist.sys.gradient_blur_perf': 'false',
                   'persist.miui.home_sf_anim': 'true',
                   'persist.sys.sf.color_saturation': '1.0'})
    lines = data.splitlines()
    for key, value in values.items():
        prefix = (key + '=').encode()
        matches = [i for i, line in enumerate(lines) if line.startswith(prefix)]
        if len(matches) > 1:
            raise RuntimeError('Duplicate yingtian property: ' + key)
        line = prefix + value.encode()
        if matches:
            lines[matches[0]] = line
        else:
            lines.append(line)
    from os4_defaults import log_properties
    return log_properties(b'\n'.join(lines) + b'\n')


def model_config():
    if hashlib.sha256(MODEL_XML).hexdigest() != PROFILE['model_xml_sha256']:
        raise RuntimeError('Original yingtian.xml checksum mismatch.')
    return MODEL_XML


def refresh_script(data):
    """Retain the physical-mode guard while selecting only this OTA version."""
    old = b'OS4.0.17.0.XFRCNXM'
    if data.count(old) != 1:
        raise RuntimeError('Unexpected refresh script version guard.')
    return data.replace(old, PROFILE['hyperos'].encode())


def align_window(config):
    """Match the emulator's native skin rotation to the guest projection."""
    import math
    import re
    from common import adb
    from apply_flutter_fix import official, root
    official(config, sources=(SOURCE,))
    response = adb(config, 'emu', 'sensor', 'get', 'orientation',
                   capture_output=True, text=True, check=True, timeout=10).stdout
    match = re.search(r'^orientation = [^:]+:[^:]+:([^\r\n]+)', response, re.M)
    if not match:
        raise RuntimeError('Cannot read the tablet emulator window orientation.')
    roll = float(match[1]) / (math.pi / 2)
    if abs(roll - round(roll)) > 0.02:
        raise RuntimeError('Unexpected tablet orientation pose; no window changes applied.')
    current = round(-roll) % 4
    # A native clockwise quarter-turn cancels the guest's counterclockwise
    # projection. With autorotation enabled, start from landscape by default.
    target_rotation = 3
    locked = root(config, 'settings get system accelerometer_rotation') == '0'
    if locked:
        target_rotation = int(root(config, 'settings get system user_rotation'))
    if target_rotation not in range(4):
        raise RuntimeError('Invalid saved tablet rotation.')
    turns = (-target_rotation - current) % 4
    for _ in range(turns):
        adb(config, 'emu', 'rotate', capture_output=True, text=True,
            check=True, timeout=10)
    if turns and locked:
        # The console rotate command enables the guest accelerometer. Restore
        # the user's rotation lock after positioning the native window.
        root(config, 'settings put system accelerometer_rotation 0\n'
             'wm user-rotation lock ' + str(target_rotation))


def thermal_profile_script():
    """Keep identity and thermal labels independent of native patch ownership."""
    import shlex
    from os4_defaults import THERMAL_LABEL_SCRIPT
    return ('#!/system/bin/sh\n'
            '# HyperOS-AVD tablet thermal/profile v2; native-compat owns HWUI.\n'
            'set -eu\n'
            '[ "$(getprop ro.boot.hardware)" = ranchu ] || exit 1\n'
            '[ "$(getprop ro.product.device)" = ' + shlex.quote(PROFILE['device']) + ' ] || exit 1\n'
            '[ "$(getprop ro.mi.os.version.incremental)" = ' + shlex.quote(PROFILE['hyperos']) + ' ] || exit 1\n'
            '[ -x /data/adb/ksu/bin/resetprop ] || exit 1\n'
            + THERMAL_LABEL_SCRIPT + profile_properties_script())


def _thermal_parent_guard():
    """Refuse aliases in every parent before inspecting or replacing a hook."""
    return '''for parent in /data /data/adb /data/adb/post-fs-data.d; do
    [ ! -L "$parent" ] || exit 1
    if [ -e "$parent" ]; then [ -d "$parent" ] || exit 1; fi
done'''


def apply_thermal_profile(config):
    """Atomically migrate known hooks, never touch native payloads or mounts."""
    import shlex
    import uuid
    from apply_flutter_fix import root
    busybox = '/data/adb/ksu/bin/busybox'
    # Return a local diagnostic for an unknown/aliased hook. Do not execute it,
    # follow it, or replace it merely because its filename resembles ours.
    inspection = root(config, f'''BB={busybox}
[ -x "$BB" ] || exit 1
if ! (
{_thermal_parent_guard()}
); then printf 'unknown\\n'
elif [ -e {THERMAL_EARLY} ] || [ -L {THERMAL_EARLY} ]; then
    if [ -f {THERMAL_EARLY} ] && [ ! -L {THERMAL_EARLY} ] &&
            [ "$("$BB" stat -c %h {THERMAL_EARLY})" = 1 ]; then
        printf 'owned:'; "$BB" sha256sum {THERMAL_EARLY} | "$BB" cut -d ' ' -f 1
    else printf 'unknown\\n'; fi
else printf 'absent\\n'
fi''')
    script = thermal_profile_script()
    digest = hashlib.sha256(script.encode()).hexdigest()
    expected = inspection.removeprefix('owned:') if inspection.startswith('owned:') else None
    if inspection != 'absent' and expected not in (LEGACY_THERMAL_HWUI_SHA256, digest):
        print('Unknown or aliased Pad thermal startup hook is preserved; native-compat remains the HWUI owner.',
              flush=True)
        return {'status': 'skipped', 'reason': 'unknown-startup-hook'}
    if expected != digest:
        stage = THERMAL_EARLY + '.stage-' + uuid.uuid4().hex
        guard = (f'[ ! -e {THERMAL_EARLY} ] && [ ! -L {THERMAL_EARLY} ] || exit 1' if expected is None
                 else f'''[ -f {THERMAL_EARLY} ] && [ ! -L {THERMAL_EARLY} ] || exit 1
[ "$("$BB" stat -c %h {THERMAL_EARLY})" = 1 ] || exit 1
[ "$("$BB" sha256sum {THERMAL_EARLY} | "$BB" cut -d ' ' -f 1)" = {expected} ] || exit 1''')
        root(config, f'''BB={busybox}
[ -x "$BB" ] || exit 1
{_thermal_parent_guard()}
{guard}
mkdir -p /data/adb/post-fs-data.d || exit 1
umask 077
trap {shlex.quote('rm -f ' + stage)} EXIT
[ ! -e {stage} ] && [ ! -L {stage} ] || exit 1
printf %s {shlex.quote(script)} > {stage} || exit 1
chmod 700 {stage} || exit 1
sh -n {stage} || exit 1
[ "$("$BB" sha256sum {stage} | "$BB" cut -d ' ' -f 1)" = {digest} ] || exit 1
{_thermal_parent_guard()}
{guard}
mv -f {stage} {THERMAL_EARLY} || exit 1''')
    # Identity/label restoration is safe in the running guest; this hook has
    # no native library mount or renderer/preload property operation.
    root(config, f'''BB={busybox}
{_thermal_parent_guard()}
[ -f {THERMAL_EARLY} ] && [ ! -L {THERMAL_EARLY} ] || exit 1
[ "$("$BB" stat -c %h {THERMAL_EARLY})" = 1 ] || exit 1
[ "$("$BB" sha256sum {THERMAL_EARLY} | "$BB" cut -d ' ' -f 1)" = {digest} ] || exit 1
sh {THERMAL_EARLY}''')
    return {'status': 'ready', 'migrated': expected == LEGACY_THERMAL_HWUI_SHA256}


def apply_runtime(config, managed=False):
    """Seed emulator-only settings; native-compat is the sole HWUI owner."""
    from common import ROOT, adb
    from apply_flutter_fix import official, root
    official(config, sources=(SOURCE,))
    info = json.loads((ROOT / 'local/build.json').read_text())
    if info.get('source') != SOURCE or info.get('device') != PROFILE['device']:
        raise RuntimeError('Refused a different tablet firmware profile.')
    from os4_defaults import apply_thermal_runtime
    apply_thermal_runtime(config)
    if not managed:
        apply_thermal_profile(config)
    from os4_defaults import apply_finddevice_workaround
    apply_finddevice_workaround(config, FINDDEVICE_SHA256)
    for sensor, value in (('proximity', '5'), ('light', '200')):
        adb(config, 'emu', 'sensor', 'set', sensor, value,
            capture_output=True, text=True, check=True, timeout=10)
    adb(config, 'emu', 'power', 'ac', 'on',
        capture_output=True, text=True, check=True, timeout=10)
    root(config, display_settings_script())
    # Xiaomi's Flutter desktop assumes 0/2 are portrait and 1/3 landscape.
    # Seed only once; preserve later manual rotation choices.
    root(config, 'if [ ! -e /data/local/tmp/hyperos-avd-pad-orientation-v3 ]; then\n'
         '  settings put system accelerometer_rotation 0 || exit 1\n'
         '  wm user-rotation lock 3 || exit 1\n'
         '  touch /data/local/tmp/hyperos-avd-pad-orientation-v3\n'
         'fi\n'
         'setprop persist.sys.miui_resolution 2272,3408,400')
    from os4_defaults import apply_refresh_runtime
    if not managed:
        apply_refresh_runtime(config)
        apply_serial(config)
    print('Pad defaults ready: original tablet identity, 60 Hz.', flush=True)
