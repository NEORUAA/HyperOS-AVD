#!/system/bin/sh
SKIPMOUNT=true
BB=/data/adb/ksu/bin/busybox
[ "$ARCH" = arm64 ] || abort 'This module requires ARM64 Android.'
[ "$(getprop ro.boot.hardware)" = ranchu ] || abort 'This module requires ranchu emulator hardware.'
case "$(getprop ro.mi.os.version.incremental)" in
    OS4.*|4.*) ;;
    *) abort 'This module requires a HyperOS 4 base system.' ;;
esac
[ -x "$BB" ] || abort 'KernelSU BusyBox is unavailable.'
[ -f "$MODPATH/SHA256SUMS" ] || abort 'Module asset checksums are missing.'
(cd "$MODPATH" && "$BB" sha256sum -c SHA256SUMS >/dev/null 2>&1) || abort 'Module asset verification failed.'
# Mutable choices are deliberately excluded from the immutable package hashes.
# Preserve existing choices before consulting the earlier per-feature modules.
choices="$MODPATH/features.disabled.next"
: > "$choices"
existing=/data/adb/modules/hyperos_avd_native_compat
inherited=false
# KernelSU may extract over a pending update directory. A surviving choice file
# belongs to that pending install; otherwise consult a verified separate stage
# before falling back to the active module's choices.
pending=/data/adb/modules_update/hyperos_avd_native_compat
if [ -e "$MODPATH/features.disabled" ] || [ -L "$MODPATH/features.disabled" ]; then
    existing="$MODPATH"
elif [ "$pending" != "$MODPATH" ] && [ -d "$pending" ] && [ ! -L "$pending" ] &&
        [ -f "$pending/module.prop" ] && [ ! -L "$pending/module.prop" ] &&
        [ -f "$pending/SHA256SUMS" ] && [ ! -L "$pending/SHA256SUMS" ] &&
        "$BB" grep -Fxq 'id=hyperos_avd_native_compat' "$pending/module.prop" &&
        "$BB" grep -Fxq 'author=HyperOS-AVD' "$pending/module.prop" &&
        (cd "$pending" && "$BB" sha256sum -c SHA256SUMS >/dev/null 2>&1); then
    existing="$pending"
fi
if [ -e "$existing/features.disabled" ] || [ -L "$existing/features.disabled" ]; then
    [ -d "$existing" ] && [ ! -L "$existing" ] &&
        [ -f "$existing/features.disabled" ] && [ ! -L "$existing/features.disabled" ] || abort 'Unsafe prior native feature choices.'
    while IFS= read -r feature || [ -n "$feature" ]; do
        [ -n "$feature" ] || continue
        "$BB" awk -F '|' -v feature="$feature" '$4 == feature {found=1} END {exit !found}' "$MODPATH/profiles.tsv" || abort 'Unknown prior native feature choice.'
        printf '%s\n' "$feature" >> "$choices"
    done < "$existing/features.disabled"
    inherited=true
fi
if [ "$inherited" != true ]; then
    for pair in flutter:hyperos_avd_flutter_render navigation:hyperos_avd_navigation assistant:hyperos_avd_assistant_mgl; do
        feature=${pair%%:*}; legacy=/data/adb/modules/${pair#*:}
        if [ -e "$legacy/disable" ] || [ -L "$legacy/disable" ] ||
                [ -e "$legacy/remove" ] || [ -L "$legacy/remove" ]; then
            printf '%s\n' "$feature" >> "$choices"
        fi
    done
fi
"$BB" sort -u "$choices" > "$MODPATH/features.disabled"
rm "$choices"
set_perm_recursive "$MODPATH" 0 0 0755 0644
set_perm "$MODPATH/runtime.sh" 0 0 0755
set_perm "$MODPATH/post-fs-data.sh" 0 0 0755
set_perm "$MODPATH/service.sh" 0 0 0755
set_perm "$MODPATH/dex2oat-cpu-policy.sh" 0 0 0755
touch "$MODPATH/skip_mount"
ui_print 'Native compatibility is selected by verified code, independent of AVD names.'
ui_print 'Unknown native code is preserved. Restart Android normally for early system changes.'
