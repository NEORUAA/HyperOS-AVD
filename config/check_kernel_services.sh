#!/system/bin/sh
# Scope optional hardware daemons to the verified ranchu phone kernel.
[ "$(getprop ro.boot.hardware)" = ranchu ] || exit 0
[ "$(getprop ro.product.device)" = hongkong ] || exit 0
[ "$(getprop ro.mi.os.version.incremental)" = OS4.0.18.0.XFRCNXM ] || exit 0
PROBE=${1:-/system/bin/hyperos_kernel_probe}
[ -x "$PROBE" ] || exit 1
"$PROBE" "$(getprop ro.millet.netlink)"
result=$?
case "$result" in
    0) setprop sys.hyperos_avd.millet_supported 1 ;;
    2) setprop sys.hyperos_avd.millet_supported 0
       setprop ctl.stop millet_monitor ;;
    *) exit 1 ;;
esac
# The original OEM client otherwise waits forever and spawns service-manager
# lazy-start/trace requests once a second. Never stop the ranchu GNSS provider.
"$PROBE" gnss-extension
result=$?
case "$result" in
    0) ;;
    2) setprop ctl.stop loc_sys_service ;;
    *) exit 1 ;;
esac
