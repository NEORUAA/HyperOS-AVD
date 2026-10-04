#!/usr/bin/env python3
"""Install the scoped XiaoAI MGL overlay on the official OS4 AVD."""
import argparse
import json
from pathlib import Path
import shlex
import subprocess

from common import ROOT, adb, runtime, sha256
from apply_flutter_fix import official, root
from patch_assistant import APK, APK_SHA256, NATIVE, AFTER, MANIFEST, native_from_apk

MODULE = '/data/adb/modules/hyperos_avd_assistant_mgl'
BOOT_SCRIPT = r'''#!/system/bin/sh
MODDIR=${0%/*}
BB=/data/adb/ksu/bin/busybox
TARGET=/product/priv-app/VoiceAssistAndroidT
APK="$TARGET/VoiceAssistAndroidT.apk"
NATIVE="$TARGET/lib/arm64/libmglnative2.so"
APK_HASH=70b833c96947b17fc58a3cb7d84c06577b97b58798ce3a7d8f9dd585db5d061d
LIB_HASH=7a7b0b363c4222320c6ff7f741ea1559e9f1f9a7ad9f01b78b98b4dd35445374
[ -f "$MODDIR/disable" ] && exit 0
[ "$(getprop ro.miui.product.home)" = com.miui.home ] || exit 1
[ -x "$BB" ] || exit 1
changed=0
for pid in 1 $(getprop init.svc_debug_pid.hyos_spawner) $(pidof zygote64); do
    [ -d "/proc/$pid" ] || continue
    nsenter -t "$pid" -m -- sh -c '
        BB=$1; MODDIR=$2; TARGET=$3; APK=$4; NATIVE=$5; APK_HASH=$6; LIB_HASH=$7
        [ "$($BB sha256sum "$APK" | $BB cut -d " " -f 1)" = "$APK_HASH" ] || exit 1
        if [ -f "$NATIVE" ]; then
            [ "$($BB sha256sum "$NATIVE" | $BB cut -d " " -f 1)" = "$LIB_HASH" ] || exit 1
            exit 0
        fi
        [ "$($BB ls -A "$TARGET")" = VoiceAssistAndroidT.apk ] || exit 1
        [ "$($BB sha256sum "$MODDIR/payload/VoiceAssistAndroidT.apk" | $BB cut -d " " -f 1)" = "$APK_HASH" ] || exit 1
        [ "$($BB sha256sum "$MODDIR/payload/lib/arm64/libmglnative2.so" | $BB cut -d " " -f 1)" = "$LIB_HASH" ] || exit 1
        $BB mount -o bind "$MODDIR/payload" "$TARGET" || exit 1
        exit 2
    ' sh "$BB" "$MODDIR" "$TARGET" "$APK" "$NATIVE" "$APK_HASH" "$LIB_HASH"
    result=$?
    case "$result" in
        0) ;;
        2) changed=1 ;;
        *) echo "Refused unsupported XiaoAI layout in $pid" >> "$MODDIR/assistant-fix.log"; exit 1 ;;
    esac
done
# Do not call PackageManager during the early boot phase.
if [ "${0##*/}" != post-fs-data.sh ] && [ "$changed" = 1 ]; then
    am force-stop com.miui.voiceassist
fi
'''


def install(config):
    official(config)
    if root(config, 'getprop ro.boot.qemu.avd_name') != config['name']:
        raise RuntimeError('XiaoAI overlay is restricted to the official OS4 AVD.')
    if root(config, 'pm path com.miui.voiceassist') != 'package:' + APK:
        raise RuntimeError('Unsupported XiaoAI update; no files were changed.')
    if root(config, 'sha256sum ' + APK).split()[0] != APK_SHA256:
        raise RuntimeError('Unsupported XiaoAI APK; no files were changed.')
    saved = root(config, f'if [ -d {MODULE} ]; then cat {MODULE}/manifest.json; fi')
    if saved and json.loads(saved) != MANIFEST:
        raise RuntimeError('An unrelated XiaoAI module exists.')
    if root(config, f'if [ -f {MODULE}/disable ]; then echo yes; fi') == 'yes':
        print('XiaoAI module is disabled; preserving this choice.', flush=True)
        return dict(MANIFEST)
    existing = root(config, 'if [ -f ' + NATIVE + ' ]; then sha256sum ' + NATIVE + '; fi')
    if existing:
        if existing.split()[0] != AFTER:
            raise RuntimeError('Refused to replace an unrelated XiaoAI native library.')
        print('Verified XiaoAI MGL fix is already present.', flush=True)
        return dict(MANIFEST)
    if saved:
        root(config, f'sh {MODULE}/service.sh')
        if root(config, 'sha256sum ' + NATIVE).split()[0] != AFTER:
            raise RuntimeError('Saved XiaoAI overlay failed verification.')
        return dict(MANIFEST)
    folder = ROOT / 'work/assistant-render-fix'
    folder.mkdir(parents=True, exist_ok=True)
    original = folder / 'VoiceAssistAndroidT.apk'
    if not original.is_file() or sha256(original) != APK_SHA256:
        adb(config, 'pull', APK, str(original), check=True, capture_output=True, timeout=60)
    fixed = folder / 'libmglnative2.so'
    fixed.write_bytes(native_from_apk(original.read_bytes()))
    stage = '/data/adb/hyperos-assistant-stage-' + AFTER[:12]
    if root(config, f'if [ -e {stage} ]; then echo yes; fi'):
        raise RuntimeError('A XiaoAI staging directory already exists.')
    root(config, f'mkdir -p {stage}/payload/lib/arm64')
    files = {'manifest.json': json.dumps(MANIFEST, indent=2) + '\n',
             'module.prop': 'id=hyperos_avd_assistant_mgl\nname=HyperOS AVD XiaoAI MGL fix\nversion=1\nversionCode=1\nauthor=HyperOS-AVD\ndescription=Original wakeup light effect with GLSL 300 and EGL alpha compatibility\n',
             'post-fs-data.sh': BOOT_SCRIPT, 'service.sh': BOOT_SCRIPT}
    for name, content in files.items():
        local = folder / name
        local.write_text(content)
        remote = '/data/local/tmp/hyperos-assistant-' + name
        adb(config, 'push', str(local), remote, check=True, capture_output=True, timeout=30)
        mode = '755' if name.endswith('.sh') else '644'
        root(config, f'cp {shlex.quote(remote)} {stage}/{name}\nchmod {mode} {stage}/{name}\nrm {shlex.quote(remote)}')
    remote = '/data/local/tmp/hyperos-assistant-mgl2.so'
    adb(config, 'push', str(fixed), remote, check=True, capture_output=True, timeout=30)
    root(config, f'''cp {APK} {stage}/payload/VoiceAssistAndroidT.apk
cp {remote} {stage}/payload/lib/arm64/libmglnative2.so
chmod 755 {stage}/payload {stage}/payload/lib {stage}/payload/lib/arm64
chmod 644 {stage}/payload/VoiceAssistAndroidT.apk {stage}/payload/lib/arm64/libmglnative2.so
chcon -R u:object_r:system_file:s0 {stage}/payload
chcon u:object_r:system_lib_file:s0 {stage}/payload/lib/arm64/libmglnative2.so
test "$(sha256sum {stage}/payload/VoiceAssistAndroidT.apk | cut -d ' ' -f 1)" = {APK_SHA256}
test "$(sha256sum {stage}/payload/lib/arm64/libmglnative2.so | cut -d ' ' -f 1)" = {AFTER}
test ! -e {MODULE}
mv {stage} {MODULE}
rm {remote}
sh {MODULE}/service.sh''')
    if root(config, 'sha256sum ' + NATIVE).split()[0] != AFTER:
        raise RuntimeError('XiaoAI overlay checksum mismatch.')
    (ROOT / 'local/assistant-render-fix.json').write_text(json.dumps(MANIFEST, indent=2) + '\n')
    print('XiaoAI MGL fix installed; signed APK and user data preserved.', flush=True)
    return dict(MANIFEST)


if __name__ == '__main__':
    argparse.ArgumentParser(description=__doc__).parse_args()
    try:
        install(runtime())
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        raise SystemExit(str(error))
