"""Owned yingtian test profile; keep phone defaults and workspaces separate."""
import hashlib
import json
from pathlib import Path

SOURCE = 'official-yingtian-ota'
NAME = 'HyperOS_4_Pad9ProMax_API_37'
PORT = 5582
MAX_MEMORY = 4096
FINDDEVICE_SHA256 = 'c8fe791d76f8da8bcb23412aebaf3470a3ae4202543c9a64091e1c24cb63aad4'
CONFIG = Path(__file__).resolve().parent.parent / 'config'
PROFILE = json.loads((CONFIG / 'yingtian-identity.json').read_text())
MODEL_XML = (CONFIG / 'yingtian.xml').read_bytes()
SERIAL_FILE = '/data/adb/hyperos-avd-pad/serial.json'
SERIAL_EARLY = '/data/adb/post-fs-data.d/hyperos-avd-pad-serial.sh'
COLOR_STAMP = '/data/local/tmp/hyperos-avd-pad-color-defaults-v1'


def serial_manifest(previous=None):
    """Keep one simulated Xiaomi-style identifier per tablet userdata."""
    from apply_navigation_fix import simulated_serial
    serial = simulated_serial(previous)
    result = {'revision': 1, 'source': SOURCE, 'hyperos': PROFILE['hyperos'],
              'serial_number': serial}
    if previous is not None and previous != result:
        raise RuntimeError('Unknown saved tablet serial profile; identifier preserved.')
    return result


def serial_script(state):
    """Set only serial fields before Android caches device identifiers."""
    import shlex
    state = serial_manifest(state)
    guards = {'ro.boot.hardware': 'ranchu', 'ro.product.device': PROFILE['device'],
              'ro.mi.os.version.incremental': PROFILE['hyperos']}
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
        raise RuntimeError('Unknown tablet serial startup script; files preserved.')
    if not previous or not saved_script:
        suffix = '.stage-' + uuid.uuid4().hex
        writes = ['mkdir -p ' + SERIAL_FILE.rsplit('/', 1)[0] + ' ' + SERIAL_EARLY.rsplit('/', 1)[0],
                  'umask 077', 'trap ' + shlex.quote('rm -f ' + SERIAL_FILE + suffix +
                                                   ' ' + SERIAL_EARLY + suffix) + ' EXIT']
        for path, data, mode in ((SERIAL_FILE, json.dumps(state, indent=2) + '\n', '600'),
                                 (SERIAL_EARLY, script, '700')):
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
    """Keep the test screen awake and seed Natural only on first setup."""
    return ('settings put global stay_on_while_plugged_in 7\n'
            'settings put system screen_off_timeout 2147483647\n'
            'settings put secure sleep_timeout -1\n'
            'settings put global development_settings_enabled 1\n'
            f'if [ ! -e {COLOR_STAMP} ]; then\n'
            '  settings put system display_color_mode 0\n'
            f'  touch {COLOR_STAMP}\n'
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


def memory_limit(properties, hardware=None):
    """Reject overrides before QEMU can exceed the requested test RAM ceiling."""
    value = int((hardware or {}).get('hw.ramSize', properties.get('hw.ramSize', 0)))
    if not 1024 <= value <= MAX_MEMORY:
        raise RuntimeError('The yingtian test AVD requires 1024-4096 MiB RAM.')
    return value


def template(data):
    values = dict(line.split('=', 1) for line in data.splitlines() if '=' in line)
    values.update({'avd.ini.displayname': 'Xiaomi Pad 9 Pro Max - HyperOS 4 Test',
                   'target': 'android-37.0', 'hw.ramSize': str(MAX_MEMORY),
                   'hw.cpu.ncore': '4', 'hw.lcd.width': '2272',
                   'hw.lcd.height': '3408', 'hw.lcd.density': '400',
                   'hw.initialOrientation': 'portrait',
                   'hw.gsmModem': 'no', 'hw.audioOutput': 'yes',
                   'hw.camera.back': 'virtualscene', 'hw.camera.front': 'emulated',
                   'hw.device.name': 'yingtian', 'hw.device.manufacturer': 'Xiaomi',
                   'hw.device.hash2': '', 'disk.dataPartition.size': '6G'})
    memory_limit(values)
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


def apply_runtime(config):
    """Seed emulator-only settings; never apply hongkong identity or AOD."""
    import shlex
    from common import ROOT, adb
    from apply_flutter_fix import official, root
    from patch_pad_hwui import AFTER, BEFORE, NATIVE, patch as hwui_patch
    official(config, sources=(SOURCE,))
    info = json.loads((ROOT / 'local/build.json').read_text())
    if info.get('source') != SOURCE or info.get('device') != PROFILE['device']:
        raise RuntimeError('Refused a different tablet firmware profile.')
    busybox = '/data/adb/ksu/bin/busybox'
    payload = '/data/adb/hyperos-avd-pad/libhwui.so'
    early_path = '/data/adb/post-fs-data.d/hyperos-avd-pad-thermal.sh'
    # Check the init target and payload before enabling Vulkan. Never replace
    # an unknown library or truncate the inode of an already loaded bind mount.
    current = root(config, f'[ -x {busybox} ] || exit 1\n'
                   f'{busybox} nsenter -t 1 -m -- {busybox} sha256sum {NATIVE}').split()[0]
    if current not in (BEFORE, AFTER):
        raise RuntimeError('Unsupported tablet HWUI target SHA-256: ' + current)
    saved = root(config, f'if [ -e {payload} ] || [ -L {payload} ]; then {busybox} sha256sum {payload}; fi')
    if saved and saved.split()[0] != AFTER:
        raise RuntimeError('Unsupported existing tablet HWUI payload; no renderer changes applied.')
    folder = ROOT / 'work/pad-hwui'
    folder.mkdir(parents=True, exist_ok=True)
    original = folder / 'current.so'
    fixed = folder / 'libhwui.so'
    adb(config, 'pull', NATIVE, str(original), check=True, capture_output=True, timeout=60)
    data = original.read_bytes()
    # adbd may retain the original mount namespace. The patch helper accepts
    # only the same pinned original or candidate, independent of that namespace.
    fixed.write_bytes(hwui_patch(data))
    if not saved:
        stage = '/data/local/tmp/hyperos-avd-pad-hwui.so'
        adb(config, 'push', str(fixed), stage, check=True, capture_output=True, timeout=30)
        root(config, f'[ "$({busybox} sha256sum {stage} | {busybox} cut -d " " -f 1)" = {AFTER} ] || exit 1\n'
             f'mkdir -p {payload.rsplit("/", 1)[0]}\n'
             f'if [ -e {payload} ] || [ -L {payload} ]; then\n'
             f'  [ "$({busybox} sha256sum {payload} | {busybox} cut -d " " -f 1)" = {AFTER} ] || exit 1\n'
             'else\n'
             f'  rm -f {payload}.next\n'
             f'  cp {stage} {payload}.next\n'
             f'  chmod 644 {payload}.next\n'
             f'  chcon u:object_r:system_lib_file:s0 {payload}.next\n'
             f'  [ "$({busybox} sha256sum {payload}.next | {busybox} cut -d " " -f 1)" = {AFTER} ] || exit 1\n'
             f'  mv -f {payload}.next {payload}\n'
             'fi\n'
             f'rm -f {stage}')
    root(config, f'[ "$({busybox} sha256sum {payload} | {busybox} cut -d " " -f 1)" = {AFTER} ] || exit 1\n'
         f'chmod 644 {payload}\n'
         f'chcon u:object_r:system_lib_file:s0 {payload}')
    from os4_defaults import apply_thermal_runtime, THERMAL_LABEL_SCRIPT
    apply_thermal_runtime(config)
    early = ("#!/system/bin/sh\n"
             "[ \"$(getprop ro.boot.hardware)\" = ranchu ] || exit 1\n"
             "[ \"$(getprop ro.product.device)\" = yingtian ] || exit 1\n"
             f"[ \"$(getprop ro.mi.os.version.incremental)\" = {PROFILE['hyperos']} ] || exit 1\n"
             "[ -x /data/adb/ksu/bin/resetprop ] || exit 1\n"
             f"BB={busybox}\n"
             "[ -x \"$BB\" ] || exit 1\n"
             f"SOURCE={payload}\nTARGET={NATIVE}\n"
             "source_hash=$(\"$BB\" nsenter -t 1 -m -- \"$BB\" sha256sum \"$SOURCE\" | \"$BB\" cut -d ' ' -f 1)\n"
             f"[ \"$source_hash\" = {AFTER} ] || exit 1\n"
             "target_hash=$(\"$BB\" nsenter -t 1 -m -- \"$BB\" sha256sum \"$TARGET\" | \"$BB\" cut -d ' ' -f 1)\n"
             f"case \"$target_hash\" in\n"
             f"  {BEFORE}) \"$BB\" nsenter -t 1 -m -- \"$BB\" mount -o bind \"$SOURCE\" \"$TARGET\" || exit 1 ;;\n"
             f"  {AFTER}) ;;\n"
             "  *) exit 1 ;;\nesac\n"
             "target_hash=$(\"$BB\" nsenter -t 1 -m -- \"$BB\" sha256sum \"$TARGET\" | \"$BB\" cut -d ' ' -f 1)\n"
             f"[ \"$target_hash\" = {AFTER} ] || exit 1\n"
             "/data/adb/ksu/bin/resetprop -n ro.zygote.disable_gl_preload 0 || exit 1\n"
             "setprop debug.hwui.renderer skiavk || exit 1\n"
             + THERMAL_LABEL_SCRIPT + profile_properties_script())
    root(config, 'mkdir -p /data/adb/post-fs-data.d\n'
         'printf %s ' + shlex.quote(early) + ' > ' + early_path + '.next\n'
         'chmod 700 ' + early_path + '.next\n'
         'sh -n ' + early_path + '.next\n'
         'mv -f ' + early_path + '.next ' + early_path + '\n'
         'sh ' + early_path)
    from os4_defaults import COMPONENT, FINDDEVICE
    paths = root(config, 'pm path ' + FINDDEVICE).splitlines()
    if len(paths) != 1 or not paths[0].startswith('package:'):
        raise RuntimeError('Expected the original yingtian FindDevice APK.')
    apk = paths[0].removeprefix('package:')
    if root(config, 'sha256sum ' + shlex.quote(apk)).split()[0] != FINDDEVICE_SHA256:
        raise RuntimeError('Unsupported tablet FindDevice APK; no component changes applied.')
    result = root(config, 'pm disable --user 0 ' + shlex.quote(COMPONENT))
    if 'new state: disabled' not in result:
        raise RuntimeError('Tablet FindDevice provider disable failed: ' + result)
    root(config, 'am force-stop ' + FINDDEVICE)
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
    apply_refresh_runtime(config)
    apply_serial(config)
    print('Pad test defaults ready: 4 GiB ceiling, original tablet identity, 60 Hz.', flush=True)
