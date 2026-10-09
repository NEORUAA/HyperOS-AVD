#!/system/bin/sh
# Publish boot-local capabilities before audited init services may start.
# A missing/failed probe leaves that feature gated; never change user settings.
PROBE=${1:-/system/bin/hyperos_kernel_probe}
failed=0

publish() {
    setprop "sys.hyperos_avd.cap.$1" "$2" || failed=1
    echo "HyperOS init capability: $1=$2"
}

if [ -e /dev/iorap_dev ]; then
    publish iorap 1
elif [ -r /dev ] && [ -x /dev ]; then
    publish iorap 0
else
    publish iorap unknown
    failed=1
fi

if [ -x "$PROBE" ]; then
    "$PROBE" "$(getprop ro.millet.netlink)"
    result=$?
    case "$result" in
        0) publish millet 1 ;;
        2) publish millet 0 ;;
        *) publish millet unknown; failed=1 ;;
    esac
else
    publish millet unknown
    failed=1
fi

# These are the complete effective branches of the audited QTI script.
# Public model identity must not stand in for the actual hardware platform.
platform=$(getprop ro.board.platform)
case "$platform" in
    lahaina|lito)
        soc_file=/sys/devices/system/soc/soc0/id
        [ ! -f /sys/devices/soc0/soc_id ] || soc_file=/sys/devices/soc0/soc_id
        if soc_id=$(cat "$soc_file" 2>/dev/null); then
            case "$platform:$soc_id" in
                lahaina:415|lahaina:439|lahaina:456|lito:400) publish qti_display 1 ;;
                *) publish qti_display 0 ;;
            esac
        else
            publish qti_display unknown
            failed=1
        fi
        ;;
    *) publish qti_display 0 ;;
esac

exit "$failed"
