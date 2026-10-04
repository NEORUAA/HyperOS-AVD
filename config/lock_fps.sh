#!/system/bin/sh
# HyperOS-AVD refresh defaults, adapted from the supplied lock_fps.sh.
# This verified ranchu display has one physical mode: mode 0 at 60 Hz.
[ "$(getprop ro.boot.hardware)" = ranchu ] || exit 1
[ "$(getprop ro.miui.product.home)" = com.miui.home ] || exit 1
[ "$(getprop ro.mi.os.version.incremental)" = OS4.0.17.0.XFRCNXM ] || exit 1
[ "$(getprop ro.boot.qemu.vsync)" = 60 ] || exit 1
set -e
settings --user 0 put system is_smart_fps 0
settings --user 0 put system min_refresh_rate 60
settings --user 0 put system peak_refresh_rate 60
# Xiaomi's render vote ignores the generic minimum and can select 20 Hz.
# Lock both physical and render ranges through this verified SF debug API.
reply=$(service call SurfaceFlinger 1035 i32 0)
[ "$reply" = 'Result: Parcel(NULL)' ] || exit 1
log -t HyperOSAVDRefresh 'Physical and compositor render rates locked to 60 Hz.'
