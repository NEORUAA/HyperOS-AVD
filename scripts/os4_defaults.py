#!/usr/bin/env python3
"""Persist verified OS4 provisioning, display and first-boot defaults."""
import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess

FINDDEVICE = 'com.xiaomi.finddevice'
PROVIDER = FINDDEVICE + '.v2.FindDeviceStatusManagerProvider'
COMPONENT = FINDDEVICE + '/' + PROVIDER
APK_SHA256 = 'e57888e1721680fece2961ec0cf23c646693be2e58d0ae6b772c285ce317cfd1'
OVERLAY = 'org.hyperos.avd.settings.defaults'
SCREEN_TIMEOUT = 2147483647
COLOR_SATURATION = '1.0'
COLOR_STAMP = '/data/local/tmp/hyperos-avd-color-defaults-v2'
GRADIENT_BLUR_PROPERTY = 'persist.sys.gradient_blur_perf'
HWUI_SHA256 = '6dfbac6a533ce08b6ace8e7cd7df3c4067350a1f1410729f8c0acd4bc8a78bd4'
# MiGradientBlurEffect loses the gradient on the emulator. The stock generic
# Skia path renders it correctly in both SystemUI and the View-based Theme app.
GRADIENT_BLUR_INIT = b'''
# Select the original generic gradient blur before HWUI is loaded by zygote.
on post-fs-data
    setprop persist.sys.gradient_blur_perf false

on property:ro.persistent_properties.ready=true
    setprop persist.sys.gradient_blur_perf false
'''
# Official hongkong product/etc/displayconfig/display_id_4630947121878579347.xml.
DISPLAY = {'width': 1120, 'height': 2436, 'density': 480}
SENSOR_DEFAULTS = {'proximity': '5', 'light': '200'}
CONFIG = Path(__file__).resolve().parent.parent / 'config'
REFRESH_SCRIPT = (CONFIG / 'lock_fps.sh').read_bytes()
REFRESH_INIT = b'\n' + (CONFIG / 'lock_refresh_rate.rc').read_bytes()
REFRESH_SERVICE = '/data/adb/service.d/hyperos-avd-lock-fps.sh'
REFRESH_WRAPPER = b'''#!/system/bin/sh
# HyperOS-AVD refresh service v1; remove this file to disable the runtime override.
count=0
while [ "$(getprop sys.boot_completed)" != 1 ]; do
    count=$((count + 1)); [ "$count" -lt 300 ] || exit 1
    sleep 1
done
''' + REFRESH_SCRIPT.removeprefix(b'#!/system/bin/sh\n')
AOD_STAMP = '/data/local/tmp/hyperos-avd-aod-defaults-v1'
AOD_SCRIPT = f'''#!/system/bin/sh
# Seed once; preserve later choices. KernelSU is not required for this service.
[ "$(getprop ro.boot.hardware)" = ranchu ] || exit 1
[ "$(getprop ro.mi.os.version.incremental)" = OS4.0.17.0.XFRCNXM ] || exit 1
set -e
# The color service restores Boosted mode (1.1) from Settings on each boot.
# Select Natural once; subsequent manual color choices remain persistent.
if [ ! -e {COLOR_STAMP} ]; then
    settings --user 0 put system display_color_mode 0
    touch {COLOR_STAMP}
fi
[ ! -e {AOD_STAMP} ] || exit 0
settings --user 0 put secure doze_always_on 1
settings --user 0 put secure aod_show_style 2
settings --user 0 put secure aod_mode_user_set 1
touch {AOD_STAMP}
log -t HyperOSAVDDefaults 'Initial AOD defaults applied: enabled, always visible.'
'''.encode()
AOD_INIT = b'''
# Seed the primary user's AOD settings once on a clean or upgraded AVD.
service hyperos-aod-defaults /system/bin/sh /system_ext/bin/hyperos-avd-aod-defaults.sh
    user root
    group root shell
    seclabel u:r:shell:s0
    disabled
    oneshot

on property:sys.boot_completed=1
    start hyperos-aod-defaults
'''
# FeatureParser uses Build.DEVICE (emu64a), not ro.product.mod_device (hongkong).
# Use the complete original model configuration as requested by the user.
MODEL_XML = (Path(__file__).resolve().parent.parent / 'config/hongkong.xml').read_bytes()
MODEL_SHA256 = '807318324b95a9b92e6f95c0c5cb809a41408b009df27acce437a1b063a74db4'
PHONE_IDENTITY = json.loads((Path(__file__).resolve().parent.parent /
                            'config/hongkong-identity.json').read_text())['properties']
COMPONENT_XML = f'''<?xml version="1.0" encoding="utf-8"?>
<permissions>
    <!-- The ranchu vendor has no Xiaomi MTD / QSEE / RPMB storage service. -->
    <component-override package="{FINDDEVICE}">
        <component class="{PROVIDER}" enabled="false" />
    </component-override>
</permissions>
'''.encode()
DEFAULTS_XML = f'''<resources>
    <integer name="def_screen_off_timeout">{SCREEN_TIMEOUT}</integer>
    <integer name="def_sleep_timeout">-1</integer>
    <bool name="def_stay_on_while_plugged_in">true</bool>
</resources>
'''


def apply_sensor_defaults(config):
    """Start only the owned OS4 AVD with unobstructed, indoor sensor readings."""
    from common import adb
    from apply_flutter_fix import official
    official(config)
    actual = adb(config, 'shell', 'getprop ro.boot.qemu.avd_name',
                 check=True, capture_output=True, text=True, timeout=10).stdout.strip()
    if actual != config['name']:
        raise RuntimeError('Sensor initialization refused a different AVD.')
    for sensor, value in SENSOR_DEFAULTS.items():
        result = adb(config, 'emu', 'sensor', 'set', sensor, value,
                     check=True, capture_output=True, text=True, timeout=10)
        if 'OK' not in result.stdout or 'KO:' in result.stdout:
            raise RuntimeError('Emulator sensor initialization failed: ' + sensor)
    print('OS4 sensors initialized: proximity 5 cm, ambient light 200 lux.', flush=True)


def quickstep_properties(data):
    """Set the launcher identity before PackageManager evaluates stock RROs."""
    key = b'ro.miui.product.home='
    lines = data.splitlines()
    matches = [i for i, line in enumerate(lines) if line.startswith(key)]
    if len(matches) > 1:
        raise RuntimeError('Duplicate launcher identity properties.')
    value = key + b'com.miui.home'
    if matches:
        lines[matches[0]] = value
    else:
        lines += [b'', b'# Activate original Xiaomi Quickstep on the first boot.', value]
    return b'\n'.join(lines) + b'\n'


def production_properties(data):
    """Keep the stock user build; KernelSU shell access is independent of this."""
    lines = quickstep_properties(data).splitlines()
    matches = [i for i, line in enumerate(lines) if line.startswith(b'ro.debuggable=')]
    if len(matches) != 1 or lines[matches[0]] not in (b'ro.debuggable=0', b'ro.debuggable=1'):
        raise RuntimeError('Unexpected OS4 debuggable property.')
    lines[matches[0]] = b'ro.debuggable=0'
    # Restore stock SF animation after correcting the compositor to 60 Hz.
    key = b'persist.miui.home_sf_anim='
    matches = [i for i, line in enumerate(lines) if line.startswith(key)]
    if len(matches) > 1:
        raise RuntimeError('Duplicate launcher SF animation properties.')
    if matches:
        lines[matches[0]] = key + b'true'
    else:
        lines += [b'# Preserve stock Xiaomi SF transitions at 60 Hz.', key + b'true']
    key = b'persist.sys.sf.color_saturation='
    matches = [i for i, line in enumerate(lines) if line.startswith(key)]
    if len(matches) > 1:
        raise RuntimeError('Duplicate display saturation properties.')
    value = key + COLOR_SATURATION.encode()
    if matches:
        lines[matches[0]] = value
    else:
        lines += [b'# Use neutral saturation for the emulator display.', value]
    key = (GRADIENT_BLUR_PROPERTY + '=').encode()
    matches = [i for i, line in enumerate(lines) if line.startswith(key)]
    if len(matches) > 1:
        raise RuntimeError('Duplicate gradient blur properties.')
    if matches:
        lines[matches[0]] = key + b'false'
    else:
        lines += [b'# Use the stock generic gradient blur on ranchu.', key + b'false']
    return identity_properties(b'\n'.join(lines) + b'\n')


def identity_properties(data):
    """Copy verified public phone identity without changing ranchu HAL selectors."""
    lines = data.splitlines()
    for name, value in PHONE_IDENTITY.items():
        key = (name + '=').encode()
        matches = [i for i, line in enumerate(lines) if line.startswith(key)]
        if len(matches) > 1:
            raise RuntimeError('Duplicate phone identity property: ' + name)
        replacement = key + value.encode()
        if matches:
            lines[matches[0]] = replacement
        else:
            lines.append(replacement)
    return b'\n'.join(lines) + b'\n'


def disable_debug_console(data):
    trigger = b'\non property:ro.debuggable=1\n    start console\n'
    if data.count(trigger) > 1:
        raise RuntimeError('Duplicate AVD console triggers.')
    return data.replace(trigger, b'\n')


def boot_defaults(data):
    data = disable_debug_console(data)
    if GRADIENT_BLUR_INIT not in data:
        data += GRADIENT_BLUR_INIT
    for block, name in ((AOD_INIT, b'hyperos-aod-defaults'),
                        (REFRESH_INIT, b'hyperos-lock-fps')):
        if block not in data:
            if b'service ' + name + b' ' in data:
                raise RuntimeError('Unexpected existing defaults service: ' + name.decode())
            data += block
    return data


def display_template(data):
    """Use the official primary display geometry, leaving OS3 unchanged."""
    values = {'hw.lcd.width': DISPLAY['width'], 'hw.lcd.height': DISPLAY['height'],
              'hw.lcd.density': DISPLAY['density']}
    lines = data.splitlines()
    for key, value in values.items():
        matches = [i for i, line in enumerate(lines) if line.startswith(key + '=')]
        if len(matches) != 1:
            raise RuntimeError('Missing or duplicate AVD display key: ' + key)
        lines[matches[0]] = f'{key}={value}'
    return '\n'.join(lines) + '\n'


def image_replacements(sdk, folder):
    """Use native component overrides and an RRO; keep signed APKs intact."""
    from common import sha256
    from patch_gnss import java
    import hashlib
    if hashlib.sha256(MODEL_XML).hexdigest() != MODEL_SHA256:
        raise RuntimeError('Original hongkong model configuration checksum mismatch.')
    sdk, folder = Path(sdk), Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    tools = sdk / 'build-tools/37.0.0'
    framework = sdk / 'platforms/android-36/android.jar'
    resources = folder / 'res/values'
    resources.mkdir(parents=True, exist_ok=True)
    (resources / 'defaults.xml').write_text(DEFAULTS_XML)
    manifest = folder / 'AndroidManifest.xml'
    manifest.write_text(f'''<manifest xmlns:android="http://schemas.android.com/apk/res/android"
    package="{OVERLAY}" android:versionCode="1" android:versionName="1">
    <uses-sdk android:minSdkVersion="21" android:targetSdkVersion="28" />
    <overlay android:targetPackage="com.android.providers.settings"
        android:isStatic="true" android:priority="999" />
    <application android:hasCode="false" />
</manifest>
''')
    compiled, unsigned, signed = (folder / name for name in ('resources.zip', 'unsigned.apk', 'SettingsDefaults.apk'))
    subprocess.run([str(tools / 'aapt2'), 'compile', '--dir', str(folder / 'res'),
                    '-o', str(compiled)], check=True, capture_output=True)
    subprocess.run([str(tools / 'aapt2'), 'link', '-o', str(unsigned), '-I', str(framework),
                    '--manifest', str(manifest), str(compiled)], check=True, capture_output=True)
    # A private, local development certificate signs this resource-only RRO.
    # It is unrelated to the original Xiaomi APKs and is never distributed.
    keystore = folder / 'overlay.jks'
    if not keystore.exists():
        subprocess.run([str(Path(java()).parent / 'keytool'), '-genkeypair', '-keystore', str(keystore),
                        '-storepass', 'android', '-keypass', 'android', '-alias', 'overlay',
                        '-dname', 'CN=HyperOS AVD Defaults', '-keyalg', 'RSA', '-keysize', '2048',
                        '-validity', '10000', '-noprompt'], check=True, capture_output=True)
    environment = dict(os.environ, JAVA_HOME=str(Path(java()).parent.parent))
    subprocess.run([str(tools / 'apksigner'), 'sign', '--ks', str(keystore), '--ks-key-alias', 'overlay',
                    '--ks-pass', 'pass:android', '--key-pass', 'pass:android',
                    '--out', str(signed), str(unsigned)], check=True, capture_output=True, env=environment)
    subprocess.run([str(tools / 'apksigner'), 'verify', str(signed)],
                   check=True, capture_output=True, env=environment)
    marker = {'schema': 2, 'finddevice_disabled_component': PROVIDER,
              'finddevice_apk_sha256': APK_SHA256, 'settings_overlay': OVERLAY,
              'settings_overlay_sha256': sha256(signed), 'screen_off_timeout': SCREEN_TIMEOUT,
              'sleep_timeout': -1, 'stay_on_while_plugged_in': 7, 'emulator_ac_online': True,
              'debuggable': False, 'serial_console': False, 'display': DISPLAY,
              'color_mode': 0, 'color_saturation': COLOR_SATURATION,
              'gradient_blur_perf': False,
              'refresh': {'physical_hz': 60, 'render_hz': 60, 'mode_id': 0,
                          'script_sha256': hashlib.sha256(REFRESH_SCRIPT).hexdigest()},
              'aod': {'doze_always_on': 1, 'aod_show_style': 2, 'aod_mode_user_set': 1,
                      'model': 'emu64a', 'model_xml_sha256': hashlib.sha256(MODEL_XML).hexdigest(),
                      'support_aod_fullscreen': True}}
    edits = {
        'system/etc/sysconfig/hyperos-avd-components.xml': (COMPONENT_XML, 0o644, 'u:object_r:system_file:s0'),
        'product/overlay/HyperOSAVDSettingsDefaults/SettingsDefaults.apk':
            (signed.read_bytes(), 0o644, 'u:object_r:system_file:s0'),
        'product/etc/hyperos-avd-defaults.json':
            ((json.dumps(marker, indent=2) + '\n').encode(), 0o644, 'u:object_r:system_file:s0'),
        'product/etc/device_features/emu64a.xml':
            (MODEL_XML, 0o644, 'u:object_r:system_file:s0'),
        'system_ext/bin/hyperos-avd-aod-defaults.sh':
            (AOD_SCRIPT, 0o755, 'u:object_r:system_file:s0'),
        'product/etc/init/lock_fps.sh':
            (REFRESH_SCRIPT, 0o755, 'u:object_r:system_file:s0'),
    }
    return edits, marker


def apply_color_runtime(config, force=False):
    """Seed neutral display saturation once, preserving later user choices."""
    from apply_flutter_fix import official, root
    official(config)
    if root(config, 'getprop ro.boot.hardware') != 'ranchu':
        raise RuntimeError('OS4 color defaults are restricted to ranchu hardware.')
    if not force and root(config, f'if [ -e {COLOR_STAMP} ]; then echo yes; fi') == 'yes':
        return
    root(config, 'settings --user 0 put system display_color_mode 0')
    reply = root(config, f'service call SurfaceFlinger 1022 f {COLOR_SATURATION}')
    if reply != 'Result: Parcel(NULL)':
        raise RuntimeError('Unexpected SurfaceFlinger saturation response: ' + reply)
    root(config, f'setprop persist.sys.sf.color_saturation {COLOR_SATURATION}')
    if root(config, 'getprop persist.sys.sf.color_saturation') != COLOR_SATURATION:
        raise RuntimeError('Display saturation was not persisted.')
    if root(config, 'settings --user 0 get system display_color_mode') != '0':
        raise RuntimeError('Natural display mode was not persisted.')
    root(config, f'touch {COLOR_STAMP}')
    print('OS4 display saturation restored to 1.0.', flush=True)


def apply_refresh_runtime(config):
    """Persist the tested refresh fix without replacing images or userdata."""
    from apply_flutter_fix import official, root
    from common import ROOT, adb, sha256
    official(config)
    expected = {'ro.boot.hardware': 'ranchu', 'ro.boot.qemu.avd_name': config['name'],
                'ro.mi.os.version.incremental': 'OS4.0.17.0.XFRCNXM',
                'ro.boot.qemu.vsync': '60'}
    for key, value in expected.items():
        if root(config, 'getprop ' + key) != value:
            raise RuntimeError('Unsupported display for refresh override: ' + key)
    folder = ROOT / 'work/refresh-fix'
    folder.mkdir(parents=True, exist_ok=True)
    script = folder / 'service.sh'
    script.write_bytes(REFRESH_WRAPPER)
    checksum = sha256(script)
    marker = ROOT / 'local/refresh-fix.json'
    old = json.loads(marker.read_text()) if marker.is_file() else {}
    current = root(config, f'if [ -f {REFRESH_SERVICE} ]; then sha256sum {REFRESH_SERVICE}; fi')
    current = current.split()[0] if current else ''
    owned = {checksum}
    if old.get('target') == REFRESH_SERVICE:
        owned.add(old['sha256'])
    if current and current not in owned:
        raise RuntimeError('Refused to replace an unrelated refresh service.')
    remote = '/data/local/tmp/hyperos-avd-lock-fps.next'
    adb(config, 'push', str(script), remote, check=True, capture_output=True, timeout=30)
    root(config, f'''test "$(sha256sum {remote} | cut -d ' ' -f 1)" = {checksum}
mkdir -p /data/adb/service.d
cp {remote} {REFRESH_SERVICE}.next
chmod 755 {REFRESH_SERVICE}.next
mv {REFRESH_SERVICE}.next {REFRESH_SERVICE}
rm {remote}
sh {REFRESH_SERVICE}''')
    state = root(config, 'dumpsys SurfaceFlinger')
    if 'renderRate=60.00 Hz' not in state:
        raise RuntimeError('SurfaceFlinger did not retain the verified 60 Hz render rate.')
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({'revision': 1, 'target': REFRESH_SERVICE,
                                 'sha256': checksum, 'physical_hz': 60,
                                 'render_hz': 60, 'mode_id': 0}, indent=2) + '\n')
    print('OS4 refresh service installed: physical and compositor render rates are 60 Hz.', flush=True)


def apply_gradient_blur_runtime(config):
    """Persist the verified stock HWUI path; cold boot reloads its native flag."""
    from apply_flutter_fix import official, root
    official(config)
    for key, value in (('ro.boot.hardware', 'ranchu'),
                       ('ro.boot.qemu.avd_name', config['name'])):
        if root(config, 'getprop ' + key) != value:
            raise RuntimeError('Gradient blur override refused different hardware: ' + key)
    checksum = root(config, 'sha256sum /system/lib64/libhwui.so').split()[0]
    if checksum != HWUI_SHA256:
        raise RuntimeError('Unsupported HWUI for the verified gradient blur override.')
    if root(config, 'getprop ' + GRADIENT_BLUR_PROPERTY) != 'false':
        root(config, 'setprop ' + GRADIENT_BLUR_PROPERTY + ' false')
        if root(config, 'getprop ' + GRADIENT_BLUR_PROPERTY) != 'false':
            raise RuntimeError('Gradient blur override was not persisted.')
        print('OS4 generic gradient blur selected; cold-boot to reload native HWUI.', flush=True)


def apply_runtime(config):
    """Apply the same defaults to existing userdata without resetting OOBE."""
    from apply_flutter_fix import official, root
    from common import ROOT, adb
    official(config)
    if root(config, 'getprop ro.boot.hardware') != 'ranchu':
        raise RuntimeError('OS4 defaults are restricted to ranchu hardware.')
    paths = root(config, 'pm path ' + FINDDEVICE).splitlines()
    if len(paths) != 1 or not paths[0].startswith('package:'):
        raise RuntimeError('Expected the verified original FindDevice APK.')
    apk = paths[0].removeprefix('package:')
    if root(config, 'sha256sum ' + shlex.quote(apk)).split()[0] != APK_SHA256:
        raise RuntimeError('Unsupported FindDevice APK; no component changes applied.')
    before = ROOT / 'local/defaults-before.json'
    if not before.exists():
        before.parent.mkdir(parents=True, exist_ok=True)
        before.write_text(json.dumps({
            'package_state': root(config, 'dumpsys package ' + FINDDEVICE),
            'screen_off_timeout': root(config, 'settings get system screen_off_timeout'),
            'sleep_timeout': root(config, 'settings get secure sleep_timeout'),
            'stay_on_while_plugged_in': root(config, 'settings get global stay_on_while_plugged_in'),
        }, indent=2) + '\n')
    output = root(config, 'pm disable --user 0 ' + shlex.quote(COMPONENT))
    if 'new state: disabled' not in output:
        raise RuntimeError('FindDevice provider disable validation failed: ' + output)
    root(config, 'am force-stop ' + FINDDEVICE + '\n'
         f'settings put system screen_off_timeout {SCREEN_TIMEOUT}\n'
         'settings put secure sleep_timeout -1\n'
         'settings put global stay_on_while_plugged_in 7')
    # The default emulator battery is unplugged. Supply AC so PowerManager's
    # stay-awake mode is indefinite, rather than relying on a 24-day timeout.
    adb(config, 'emu', 'power', 'ac', 'on', check=True, capture_output=True, timeout=15)
    # Seed once for both existing and fresh userdata. Later user choices survive
    # cold boots; show style 2 means always, 1 means scheduled, 0 means temporary.
    root(config, f'if [ ! -e {AOD_STAMP} ]; then\n'
         '    settings put secure doze_always_on 1\n'
         '    settings put secure aod_show_style 2\n'
         '    settings put secure aod_mode_user_set 1\n'
         f'    touch {AOD_STAMP}\nfi')
    apply_color_runtime(config)
    apply_refresh_runtime(config)
    apply_gradient_blur_runtime(config)
    print('OS4 defaults applied: FindDevice workaround, awake AC and initial always-on AOD.', flush=True)


def prepare_image():
    from common import ROOT, sdk_path, sha256
    from build_image import erofs
    from erofs_image import build
    from lp_image import pack
    source = ROOT / 'work/hyperos-system.img'
    folder = ROOT / 'work/defaults-candidate'
    edits, marker = image_replacements(sdk_path(), folder / 'overlay')
    edits['system/build.prop'] = (
        production_properties(erofs(source, '/system/build.prop')), 0o600, 'u:object_r:system_file:s0')
    edits['system_ext/etc/init/init.hyperos_avd.rc'] = (
        boot_defaults(erofs(source, '/system_ext/etc/init/init.hyperos_avd.rc')),
        0o644, 'u:object_r:system_file:s0')
    if erofs(source, '/product/etc/device_features/hongkong.xml') != MODEL_XML:
        raise RuntimeError('The source hongkong configuration differs from the verified original.')
    original = erofs(source, '/product/priv-app/MIUIFindDeviceCN/MIUIFindDeviceCN.apk')
    import hashlib
    if hashlib.sha256(original).hexdigest() != APK_SHA256:
        raise RuntimeError('Candidate source contains an unsupported FindDevice APK.')
    raw, packed = folder / 'hyperos-system.img', folder / 'system.img'
    source_hash = sha256(source)
    build(raw, [('', source)], folder / 'tree', edits)
    for path, (data, _, _) in edits.items():
        if erofs(raw, '/' + path) != data:
            raise RuntimeError('OS4 defaults content mismatch: ' + path)
    pack(ROOT / 'images/system.img', packed, [
        ('system', raw), ('vendor', ROOT / 'work/vendor.img'),
        ('system_dlkm', ROOT / 'work/base/system_dlkm.img')])
    (folder / 'manifest.json').write_text(json.dumps({
        'source_raw_sha256': source_hash, 'raw_sha256': sha256(raw),
        'system_sha256': sha256(packed), 'defaults': marker}, indent=2) + '\n')
    print('Verified defaults candidate ready: ' + str(packed), flush=True)


def main():
    repo = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', type=Path, default=repo / 'work/os4-official')
    parser.add_argument('--prepare-image', action='store_true')
    parser.add_argument('--color-only', action='store_true',
                        help='Restore neutral saturation on the running official OS4 AVD')
    parser.add_argument('--refresh-only', action='store_true',
                        help='Persist the verified 60 Hz compositor override on the running OS4 AVD')
    parser.add_argument('--blur-only', action='store_true',
                        help='Select the verified generic gradient blur; requires a cold boot')
    args = parser.parse_args()
    os.environ['HYPEROS_AVD_WORKSPACE'] = str(args.workspace.resolve())
    from common import host_check, runtime
    host_check()
    if sum((args.prepare_image, args.color_only, args.refresh_only, args.blur_only)) > 1:
        parser.error('Choose only one of --prepare-image, --color-only, --refresh-only or --blur-only')
    if args.prepare_image:
        prepare_image()
    elif args.color_only:
        apply_color_runtime(runtime(), force=True)
    elif args.refresh_only:
        apply_refresh_runtime(runtime())
    elif args.blur_only:
        apply_gradient_blur_runtime(runtime())
    else:
        apply_runtime(runtime())


if __name__ == '__main__':
    main()
