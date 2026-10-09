#!/system/bin/sh
# Platform state is persistent userdata; image identity is never an OTA table.
CORE_STATE=${HYPEROS_CORE_STATE:-/data/adb/hyperos-avd-core}
CORE_RESETPROP=${HYPEROS_CORE_RESETPROP:-/data/adb/ksu/bin/resetprop}
CORE_SYS=${HYPEROS_CORE_SYS:-/sys}
CORE_PENDING=${HYPEROS_CORE_PENDING:-/data/adb/modules_update/hyperos_avd_native_compat}

core_safe_file() {
    [ -f "$1" ] && [ ! -L "$1" ] &&
        [ "$("$BB" stat -c %u "$1")" = 0 ] && [ "$("$BB" stat -c %h "$1")" = 1 ]
}

core_state_safe() {
    local parent="$CORE_STATE"
    while [ "$parent" != / ]; do
        [ ! -L "$parent" ] || return 1
        if [ -e "$parent" ]; then [ -d "$parent" ] || return 1; fi
        parent=${parent%/*}; [ -n "$parent" ] || parent=/
    done
    [ -d "$CORE_STATE" ] && [ "$("$BB" stat -c %u "$CORE_STATE")" = 0 ]
}

core_permitted() {
    native_blocked && return 1
    native_enabled "$1" || return 1
    core_state_safe
}

core_context_ready() {
    local kind=$1 row path expected extra found=0 seen='|'
    core_state_safe && core_safe_file "$CORE_STATE/image.tsv" || return 1
    while IFS='|' read -r row path expected extra; do
        [ "$row" = "$kind" ] || continue
        [ -z "$extra" ] && native_hex "$expected" || return 1
        case "$seen" in *"|$path|"*) return 1 ;; esac
        seen="$seen$path|"
        case "$kind:$path" in
            identity:/system/build.prop|identity:/product/etc/build.prop) ;;
            rear:/system_ext/framework/rear-display-wake.jar|rear:/system_ext/bin/rear-display-wake|rear:/product/etc/hyperos-avd-rear-display.json|rear:/product/overlay/HyperOSAVDRearDisplay/RearDisplay.apk|rear:/product/etc/displayconfig/display_id_*.xml) ;;
            boot:/system_ext/priv-app/com.qualcomm.location/com.qualcomm.location.apk) [ "$expected" = 66dce6b9f484ee6d3651d3577f536d57f12aef0adb4a1940ee6fa0118eeb5134 ] || return 1 ;;
            boot:/product/priv-app/AutoRegistration/AutoRegistration.apk) [ "$expected" = 2b6c3ddad65ac3c6f65667631c2ac75fd8724a1092250d809bb4d3e7d7d8b1f9 ] || return 1 ;;
            boot:/system/framework/services.jar) [ "$expected" = 3b1c22fbae3262187af8e040e02962edd7f80ae7369d9bcd724457384a4fd6b1 ] || return 1 ;;
            boot:/system_ext/framework/miui-services.jar) [ "$expected" = 205bbc10beab0fcd668c79dc2b795c6115fe4b3eca1e3b8eb75f99f5f9702f56 ] || return 1 ;;
            boot:/system/bin/hyperos_kernel_probe) [ "$expected" = af7486c1f98e01641c10d5089fd46348ce612c6fbf15c1581f50a6bacb6f402a ] || return 1 ;;
            *) return 1 ;;
        esac
        native_path "$path" && core_safe_file "$path" || return 1
        [ "$(native_ns_hash 1 "$path")" = "$expected" ] || return 1
        found=$((found + 1))
    done < "$CORE_STATE/image.tsv"
    if [ "$kind" = boot ]; then [ "$found" -eq 5 ]; else [ "$found" -gt 0 ]; fi
}

core_identity_rows() {
    # Whitelist public product/build fields. Never overwrite ranchu HAL keys,
    # serials, boot properties, vendor API level, or the board platform.
    local row path expected extra
    while IFS='|' read -r row path expected extra; do
        [ "$row" = identity ] || continue
        "$BB" awk -F '=' '
            /^[[:space:]]*#/ {next}
            NF >= 2 {
                key=$1; value=substr($0, length(key)+2);
                if (key ~ /^ro\.product\.(.*\.)?(brand|device|manufacturer|model|name|marketname|cert|board)$/ ||
                    key ~ /^ro\.product\.(brand|device|manufacturer|model|name)_for_attestation$/ ||
                    key == "ro.product.first_api_level" ||
                    key ~ /^ro\.(build|system|system_ext|product|vendor|odm|system_dlkm|vendor_dlkm)\.build\.fingerprint$/ ||
                    key == "ro.build.fingerprint" || key == "ro.build.characteristics" ||
                    key == "ro.soc.model" || key == "ro.soc.manufacturer" ||
                    key == "ro.miui.product.home") {
                    if (key !~ /^[A-Za-z0-9_.]+$/ || value == "" || value ~ /[\r|]/ || seen[key]++) exit 1;
                    print key "|" value;
                }
            }' "$path" || return 1
    done < "$CORE_STATE/image.tsv"
}

core_identity() {
    core_permitted identity || return 0
    if ! core_context_ready identity; then
        native_status identity image skipped unverified-image-defaults; return 0
    fi
    [ -x "$CORE_RESETPROP" ] || { native_status identity image skipped resetprop-unavailable; return 0; }
    local rows="$MODDIR/state/identity.next.$$" key value extra current
    core_identity_rows > "$rows" || { rm -f "$rows"; native_status identity image skipped invalid-image-defaults; return 0; }
    while IFS='|' read -r key value extra; do
        core_permitted identity || { rm -f "$rows"; return 0; }
        [ -z "$extra" ] || { rm -f "$rows"; return 1; }
        current=$(getprop "$key")
        [ "$current" != "$value" ] || continue
        core_context_ready identity || { rm -f "$rows"; return 0; }
        if [ "${#value}" -ge 91 ] && [ -n "$current" ]; then
            "$CORE_RESETPROP" -d "$key" || { rm -f "$rows"; return 1; }
            core_permitted identity || { rm -f "$rows"; return 0; }
        fi
        "$CORE_RESETPROP" -n "$key" "$value" || { rm -f "$rows"; return 1; }
    done < "$rows"
    rm -f "$rows"
    native_status identity image ready verified-current-image
}

core_serial_valid() {
    printf '%s\n' "$1" | "$BB" grep -Eq '^([0-9]{5}/[A-HJ-NP-Z][0-9][NPQRSTUVWXYZ][1-9ABCDEFHJKMNPQRSTUVWXYZ][0-9]{5}|[0-9A-F]{16})$'
}

core_serial_new() {
    local numbers a b c year month day factory
    numbers=$("$BB" od -An -N12 -tu4 /dev/urandom) || return 1
    set -- $numbers
    [ "$#" = 3 ] || return 1
    for a in "$@"; do case "$a" in ''|*[!0-9]*) return 1 ;; esac; done
    a=$1; b=$2; c=$3
    year=$(date +%y); year=${year#?}
    month=$(date +%m); month=${month#0}; day=$(date +%d); day=${day#0}
    factory=$(printf 'ABCDEFGHJKLMNPQRSTUVWXYZ' | "$BB" cut -c "$((c % 23 + 1))")
    month=$(printf 'NPQRSTUVWXYZ' | "$BB" cut -c "$month")
    day=$(printf '123456789ABCDEFHJKMNPQRSTUVWXYZ' | "$BB" cut -c "$day")
    printf '%05d/%s%s%s%s%05d\n' "$((10000 + a % 90000))" "$factory" "$year" "$month" "$day" "$((1 + b % 99999))"
}

core_serial() {
    core_permitted serial || return 0
    [ -x "$CORE_RESETPROP" ] || return 0
    local file="$CORE_STATE/serial.record" lease="$CORE_STATE/serial.guard" schema owner serial extra next
    if [ -e "$lease" ] || [ -L "$lease" ]; then
        core_safe_file "$lease" && [ ! -s "$lease" ] || return 1
    fi
    exec 8>>"$lease" || return 1
    "$BB" flock -n 8 || { exec 8>&-; return 0; }
    chmod 600 "$lease" || { exec 8>&-; return 1; }
    if [ ! -e "$file" ] && [ ! -L "$file" ]; then
        # Refuse to mint an identifier while an unmigrated legacy record exists.
        if [ -e /data/adb/modules/hyperos_avd_navigation ] || [ -L /data/adb/modules/hyperos_avd_navigation ] ||
                [ -e /data/adb/hyperos-avd-pad/serial.json ] || [ -L /data/adb/hyperos-avd-pad/serial.json ]; then
            exec 8>&-; native_status serial userdata skipped legacy-identity-needs-authentication; return 0
        fi
        serial=$(core_serial_new) && core_serial_valid "$serial" || { exec 8>&-; return 1; }
        next="$file.next.$$"
        [ ! -e "$next" ] && [ ! -L "$next" ] || { exec 8>&-; return 1; }
        core_permitted serial || { exec 8>&-; return 0; }
        (set -C; printf '1|hyperos_avd_native_compat|%s\n' "$serial" > "$next") && chmod 600 "$next" &&
            mv "$next" "$file" || { rm -f "$next"; exec 8>&-; return 1; }
    fi
    core_safe_file "$file" && [ "$("$BB" wc -l < "$file")" -eq 1 ] || { exec 8>&-; return 1; }
    IFS='|' read -r schema owner serial extra < "$file"
    [ "$schema" = 1 ] && [ "$owner" = hyperos_avd_native_compat ] && [ -z "$extra" ] &&
        core_serial_valid "$serial" || { exec 8>&-; return 1; }
    for key in ro.serialno ro.boot.serialno ro.ril.oem.psno; do
        core_permitted serial || { exec 8>&-; return 0; }
        [ "$(getprop "$key")" = "$serial" ] || "$CORE_RESETPROP" -n "$key" "$serial" || { exec 8>&-; return 1; }
    done
    exec 8>&-
    native_status serial userdata ready persistent-identity
}

core_thermal() {
    core_permitted thermal || return 0
    local node label
    for node in "$CORE_SYS/devices/virtual/thermal/thermal_zone0/type" "$CORE_SYS/devices/virtual/thermal/thermal_zone0/temp"; do
        [ -f "$node" ] && [ ! -L "$node" ] || continue
        label=$(ls -Z "$node")
        case "$label" in
            *u:object_r:sysfs:s0*) core_permitted thermal || return 0; chcon u:object_r:sysfs_thermal:s0 "$node" || return 1 ;;
            *u:object_r:sysfs_thermal:s0*) ;;
            *) native_status thermal "$node" skipped unknown-sysfs-label; continue ;;
        esac
        native_status thermal "$node" ready capability-label
    done
}

core_refresh_capable() {
    [ "$(getprop ro.boot.qemu.vsync)" = 60 ] || return 1
    dumpsys SurfaceFlinger | "$BB" awk '
        /activeMode/ && /id=0([, }]|$)/ && /vsyncRate=60\.00 Hz/ {found=1}
        END {exit !found}'
}

core_refresh() {
    core_permitted refresh || return 0
    if ! core_refresh_capable; then native_status refresh display skipped unsupported-active-mode; return 0; fi
    if dumpsys SurfaceFlinger | "$BB" grep -Fq 'renderRate=60.00 Hz'; then
        native_status refresh display ready compositor-60hz; return 0
    fi
    local reply
    core_permitted refresh || return 0
    reply=$(service call SurfaceFlinger 1035 i32 0)
    [ "$reply" = 'Result: Parcel(NULL)' ] || { native_status refresh display skipped unsupported-sf-api; return 0; }
    core_permitted refresh || return 0
    dumpsys SurfaceFlinger | "$BB" grep -Fq 'renderRate=60.00 Hz' || {
        native_status refresh display failed render-rate-not-retained; return 1;
    }
    native_status refresh display ready compositor-60hz
}

core_kernel() {
    core_permitted boot-services || return 0
    if ! core_context_ready boot; then
        native_status boot-services kernel skipped unverified-image-prerequisites; return 0
    fi
    local probe=/system/bin/hyperos_kernel_probe code protocol service
    if ! core_safe_file "$probe" || [ ! -x "$probe" ] ||
            [ "$(native_ns_hash 1 "$probe")" != af7486c1f98e01641c10d5089fd46348ce612c6fbf15c1581f50a6bacb6f402a ]; then
        native_status boot-services kernel skipped verified-image-probe-unavailable; return 0
    fi
    protocol=$(getprop ro.millet.netlink)
    case "$protocol" in ''|*[!0-9]*) native_status boot-services millet skipped unknown-netlink-protocol ;; *)
        "$probe" "$protocol"; code=$?
        core_permitted boot-services && core_context_ready boot || return 0
        case "$code" in
            0) setprop sys.hyperos_avd.millet_supported 1 ;;
            2) setprop sys.hyperos_avd.millet_supported 0
               [ -z "$(getprop init.svc.millet_monitor)" ] || setprop ctl.stop millet_monitor ;;
            *) native_status boot-services millet failed capability-probe-error; return 1 ;;
        esac ;;
    esac
    core_permitted boot-services && core_context_ready boot || return 0
    "$probe" gnss-extension; code=$?
    core_permitted boot-services && core_context_ready boot || return 0
    case "$code" in
        0) ;;
        2) [ -z "$(getprop init.svc.loc_sys_service)" ] || setprop ctl.stop loc_sys_service ;;
        *) native_status boot-services gnss failed capability-probe-error; return 1 ;;
    esac
    native_status boot-services kernel ready verified-capability-probe
}

core_rear_owned() {
    local pid=$1 directory="$PROC/$1"
    case "$pid" in ''|*[!0-9]*) return 1 ;; esac
    [ -r "$directory/cmdline" ] && [ -r "$directory/environ" ] && [ -r "$directory/status" ] || return 1
    "$BB" tr '\000' '\n' < "$directory/cmdline" | "$BB" grep -Fxq io.github.hyperosavd.RearDisplayWake || return 1
    "$BB" tr '\000' '\n' < "$directory/environ" | "$BB" grep -Fxq CLASSPATH=/system_ext/framework/rear-display-wake.jar || return 1
    "$BB" awk '$1 == "Uid:" {found=1; for(i=2;i<=5;i++) if($i!=0) exit 1} END {exit !found}' "$directory/status"
}

core_rear_start_time() {
    [ -r "$PROC/$1/stat" ] || return 1
    "$BB" awk '{sub(/^.*\) /, ""); print $20}' "$PROC/$1/stat"
}

core_rear_receipt() {
    local pid=$1 boot start next="$CORE_STATE/rear-daemon.record.next.$$" schema prior_boot prior_pid prior_start extra row
    core_rear_owned "$pid" || return 1
    boot=$(cat "$PROC/sys/kernel/random/boot_id") && start=$(core_rear_start_time "$pid") || return 1
    case "$start" in ''|*[!0-9]*) return 1 ;; esac
    [ -n "$boot" ] && [ ! -e "$next" ] && [ ! -L "$next" ] || return 1
    if [ -e "$CORE_STATE/rear-daemon.record" ] || [ -L "$CORE_STATE/rear-daemon.record" ]; then
        core_safe_file "$CORE_STATE/rear-daemon.record" &&
            [ "$("$BB" wc -l < "$CORE_STATE/rear-daemon.record")" -eq 1 ] || return 1
        IFS='|' read -r schema prior_boot prior_pid prior_start extra < "$CORE_STATE/rear-daemon.record"
        [ "$schema" = 1 ] && [ -n "$prior_boot" ] && [ -z "$extra" ] || return 1
        case "$prior_pid:$prior_start" in *[!0-9:]*) return 1 ;; esac
        [ -n "$prior_pid" ] && [ -n "$prior_start" ] || return 1
        row="1|$boot|$pid|$start"
        [ "$(cat "$CORE_STATE/rear-daemon.record")" != "$row" ] || return 0
    fi
    core_rear_owned "$pid" && [ "$(core_rear_start_time "$pid")" = "$start" ] || return 1
    (set -C; printf '1|%s|%s|%s\n' "$boot" "$pid" "$start" > "$next") &&
        chmod 600 "$next" && mv "$next" "$CORE_STATE/rear-daemon.record"
}

core_rear_lock() {
    core_state_safe || return 1
    local lease="$CORE_STATE/rear.guard"
    if [ -e "$lease" ] || [ -L "$lease" ]; then core_safe_file "$lease" && [ ! -s "$lease" ] || return 1; fi
    exec 7>>"$lease" || return 1
    "$BB" flock -n 7 || { exec 7>&-; return 1; }
    chmod 600 "$lease" || { exec 7>&-; return 1; }
}

core_rear_cleanup_locked() {
    # Stop only the exact daemon adopted/started by this Core, from this boot
    # and process lifetime. PID reuse or a different app_process is preserved.
    core_state_safe || return 0
    local file="$CORE_STATE/rear-daemon.record" schema boot pid start extra current
    core_safe_file "$file" && [ "$("$BB" wc -l < "$file")" -eq 1 ] || return 0
    IFS='|' read -r schema boot pid start extra < "$file"
    [ "$schema" = 1 ] && [ -z "$extra" ] && [ "$boot" = "$(cat "$PROC/sys/kernel/random/boot_id")" ] || return 0
    case "$pid:$start" in *[!0-9:]*) return 0 ;; esac
    [ -n "$pid" ] && [ -n "$start" ] && core_rear_owned "$pid" || return 0
    [ "$(core_rear_start_time "$pid")" = "$start" ] || return 0
    [ "$(native_ns_hash 1 /system_ext/framework/rear-display-wake.jar)" = d38aef457215fe9f5f397af2e58ece05c3baa61f742a8a9e3d7a3b577bd5c077 ] || return 0
    # Recheck immediately before the signal; do not search or kill by name.
    core_rear_owned "$pid" && [ "$(core_rear_start_time "$pid")" = "$start" ] || return 0
    kill -TERM "$pid" || return 1
    native_status rear-wake display disabled verified-daemon-stopped
}

core_rear_cleanup() {
    core_safe_file "$CORE_STATE/rear-daemon.record" || return 0
    core_rear_lock || return 0
    core_rear_cleanup_locked
    local result=$?
    exec 7>&-
    return "$result"
}

core_rear_ready() {
    native_eligible rear-display && core_context_ready rear || return 1
    local row id group unique extra
    core_safe_file "$CORE_STATE/rear.record" || return 1
    IFS='|' read -r row id group unique extra < "$CORE_STATE/rear.record"
    [ "$row" = 1 ] && [ "$id" = 1 ] && [ "$group" = 1 ] &&
        [ "$unique" = local:4619827551948147201 ] && [ -z "$extra" ] || return 1
    [ "$(native_ns_hash 1 /system_ext/framework/rear-display-wake.jar)" = d38aef457215fe9f5f397af2e58ece05c3baa61f742a8a9e3d7a3b577bd5c077 ] &&
        [ "$(native_ns_hash 1 /system_ext/bin/rear-display-wake)" = f98ea740ae450ea4e2e0815c7e74f698ee2fe5cfcd6a367247f2f91021ed16e7 ] || return 1
    dumpsys display | "$BB" awk -v unique="$unique" '
        /DisplayInfo/ && /displayId[ =:]*1([, }]|$)/ && /displayGroupId[ =:]*1([, }]|$)/ && index($0,unique) {found=1}
        END {exit !found}'
}

core_rear() {
    if ! core_permitted rear-wake; then core_rear_cleanup; return 0; fi
    if ! core_rear_ready; then native_status rear-wake display skipped physical-display-prerequisite; return 0; fi
    local directory pid count=0
    core_rear_lock || return 0
    for directory in "$PROC"/[0-9]*; do
        if core_rear_owned "${directory##*/}"; then
            core_permitted rear-wake && core_rear_ready || { exec 7>&-; return 0; }
            core_rear_receipt "${directory##*/}" || { exec 7>&-; return 1; }
            exec 7>&-; native_status rear-wake display ready verified-existing-daemon; return 0
        fi
    done
    while [ "$count" -lt 30 ]; do
        core_permitted rear-wake && core_rear_ready || { exec 7>&-; return 0; }
        count=$((count + 1))
        "$BB" nohup /system_ext/bin/rear-display-wake >> "$MODDIR/runtime.log" 2>&1 < /dev/null 7>&- 8>&- 9>&- &
        pid=$!; sleep 2
        if core_rear_owned "$pid"; then
            core_rear_receipt "$pid" || { exec 7>&-; return 1; }
            if ! core_permitted rear-wake; then core_rear_cleanup_locked; exec 7>&-; return 0; fi
            exec 7>&-; native_status rear-wake display ready verified-started-daemon; return 0
        fi
        core_permitted rear-wake || { exec 7>&-; return 0; }
    done
    exec 7>&-
    native_status rear-wake display failed daemon-not-ready
}

core_early() {
    core_identity && core_serial && core_thermal
}

core_late() {
    core_refresh; core_kernel; core_rear
}
