#!/system/bin/sh
# Bounded, read-only status collector. No shell commands come from page input.
BB=${HYPEROS_STATUS_BB:-/data/adb/ksu/bin/busybox}
BASE=${HYPEROS_STATUS_BASE:-/data/adb}
ID=@MODULE_ID@
MODDIR=$BASE/modules/$ID
[ -x "$BB" ] || exit 1
safe_dir() { [ -d "$1" ] && [ ! -L "$1" ]; }
safe_file() { [ -f "$1" ] && [ ! -L "$1" ] && [ "$("$BB" stat -c '%h:%u' "$1")" = 1:0 ]; }
safe_dir "$BASE" && safe_dir "$BASE/modules" && safe_dir "$MODDIR" || exit 1
safe_file "$MODDIR/module.prop" || exit 1
[ "$("$BB" sed -n 's/^id=//p' "$MODDIR/module.prop")" = "$ID" ] || exit 1
printf '[module]\nid=%s\n' "$ID"
state=enabled
if [ -e "$MODDIR/remove" ] || [ -L "$MODDIR/remove" ]; then state=pending-removal
elif [ -e "$MODDIR/disable" ] || [ -L "$MODDIR/disable" ]; then state=disabled
elif [ -e "$BASE/modules_update/$ID" ] || [ -L "$BASE/modules_update/$ID" ]; then state=pending
elif ! safe_dir "$MODDIR/state" || ! safe_file "$MODDIR/state/status.tsv"; then state=not-activated
fi
printf 'state=%s\n[health]\n' "$state"
printf 'selinux=%s\nrenderer=%s\n' "$(getenforce 2>/dev/null)" "$(getprop debug.hwui.renderer 2>/dev/null)"
printf '[status]\n'
if safe_dir "$MODDIR/state" && safe_file "$MODDIR/state/status.tsv"; then
    "$BB" head -c 24576 "$MODDIR/state/status.tsv" | "$BB" head -n 160
    printf '\n'
fi
printf '[log]\n'
if safe_file "$MODDIR/runtime.log"; then "$BB" tail -c 8192 "$MODDIR/runtime.log"; fi
printf '\n'
