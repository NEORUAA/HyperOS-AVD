#!/system/bin/sh
# ARTD's OS4 install fallback is a fixed eight-CPU list. Override its supported
# properties before first compilation, using actual guest topology instead.

[ "$#" -le 1 ] || exit 2
case "${1:-apply}" in apply) ;; *) exit 2 ;; esac
case "$(getprop ro.boot.hardware)" in ranchu|goldfish) ;; *) exit 0 ;; esac
case "$(getprop ro.mi.os.version.name)" in OS4|OS4.*) ;; *) exit 0 ;; esac

CPU_ROOT=${DEX2OAT_CPU_ROOT:-/sys/devices/system/cpu}
CPU_ONLINE=$(cat "$CPU_ROOT/online" 2>/dev/null) || exit 0
CPU_POSSIBLE=$(cat "$CPU_ROOT/possible" 2>/dev/null) || exit 0
CPU_CONF=$(getconf _NPROCESSORS_CONF 2>/dev/null) || exit 0

# nprocessors is also the compiler parser's upper bound. Keep actual CPU IDs;
# an online set such as 0,2,5 must never become the invented range 0-2.
CPU_AVAILABLE=$(awk -v online="$CPU_ONLINE" -v possible="$CPU_POSSIBLE" -v count="$CPU_CONF" '
function parse(text, result,    n,parts,i,ends,a,b,j) {
    if (text == "") return 0
    n = split(text, parts, ",")
    for (i = 1; i <= n; i++) {
        if (parts[i] !~ /^[0-9]+(-[0-9]+)?$/) return 0
        split(parts[i], ends, "-")
        a = ends[1] + 0; b = (ends[2] == "" ? a : ends[2] + 0)
        if (a > b || b >= count) return 0
        for (j = a; j <= b; j++) result[j] = 1
    }
    return 1
}
BEGIN {
    if (count !~ /^[0-9]+$/ || count < 1 || count > 1048576) exit 1
    if (!parse(online, active) || !parse(possible, present)) exit 1
    answer = ""
    for (i = 0; i < count; i++) {
        if (active[i] && !present[i]) exit 1
        if (active[i]) answer = answer (answer == "" ? "" : " ") i
    }
    if (answer == "") exit 1
    print answer
}') || exit 0

cpu_list() {
    # Property values are limited to 91 bytes. The compiler accepts comma CPU
    # IDs, not kernel range syntax; normalize ranges without unbounded loops.
    awk -v requested="$1" -v fallback="$2" -v available="$CPU_AVAILABLE" '
    function select(text, result,    n,parts,i,ends,a,b,k) {
        if (text == "") return 0
        n = split(text, parts, ",")
        for (i = 1; i <= n; i++) {
            if (parts[i] !~ /^[0-9]+(-[0-9]+)?$/) return 0
            split(parts[i], ends, "-")
            a = ends[1] + 0; b = (ends[2] == "" ? a : ends[2] + 0)
            if (a > b) return 0
            for (k = 1; k <= total; k++)
                if (ids[k] >= a && ids[k] <= b) result[ids[k]] = 1
        }
        return 1
    }
    BEGIN {
        total = split(available, ids, " ")
        valid = select(requested, chosen)
        number = 0
        if (valid) for (k = 1; k <= total; k++) if (chosen[ids[k]]) number++
        if (!number) {
            for (k in chosen) delete chosen[k]
            if (!select(fallback, chosen)) exit 1
        }
        answer = ""
        for (k = 1; k <= total; k++) if (chosen[ids[k]]) {
            next_value = answer (answer == "" ? "" : ",") ids[k]
            if (length(next_value) > 91) break
            answer = next_value
        }
        if (answer == "") exit 1
        # Preserve an already valid user comma-list and ordering.
        if (valid && requested ~ /^[0-9]+(,[0-9]+)*$/ && length(requested) <= 91) {
            n = split(requested, parts, ","); unchanged = 1
            for (k = 1; k <= n; k++) if (!chosen[parts[k] + 0]) unchanged = 0
            if (unchanged) { print requested; exit }
        }
        print answer
    }'
}

cpu_write() {
    # Refuse stale writes if another policy/user changed the property during
    # calculation. A later boot invocation can reconcile the new choice.
    [ "$(getprop "$1")" = "$2" ] || exit 0
    [ "$2" = "$3" ] && return 0
    setprop "$1" "$3" || return 1
    [ "$(getprop "$1")" = "$3" ] || return 1
}

CPU_FALLBACK=$(printf '%s\n' "$CPU_AVAILABLE" | tr ' ' ',')
CPU_MAIN_OLD=$(getprop dalvik.vm.dex2oat-cpu-set)
CPU_DEFAULT_OLD=$(getprop dalvik.vm.default-dex2oat-cpu-set)
if [ -z "$CPU_DEFAULT_OLD" ]; then
    # A user general affinity also constrains the OEM-specific install default.
    # Otherwise the later install argument would override that valid subset.
    [ -z "$CPU_MAIN_OLD" ] || CPU_FALLBACK=$(cpu_list "$CPU_MAIN_OLD" "$CPU_FALLBACK") || exit 1
    [ "$(getprop dalvik.vm.dex2oat-cpu-set)" = "$CPU_MAIN_OLD" ] || exit 0
fi
CPU_DEFAULT=$(cpu_list "$CPU_DEFAULT_OLD" "$CPU_FALLBACK") || exit 1
cpu_write dalvik.vm.default-dex2oat-cpu-set "$CPU_DEFAULT_OLD" "$CPU_DEFAULT" || exit 1

for CPU_CHANNEL in dex2oat boot-dex2oat background-dex2oat restore-dex2oat; do
    CPU_KEY=dalvik.vm.$CPU_CHANNEL-cpu-set
    CPU_OLD=$(getprop "$CPU_KEY")
    CPU_VALUE=$(cpu_list "$CPU_OLD" "$CPU_DEFAULT") || exit 1
    # Empty main affinity is valid: cmdline compilation needs no new override.
    # Boot/background/restore and the install default have OEM fixed fallbacks.
    if [ "$CPU_CHANNEL" != dex2oat ] || [ -n "$CPU_OLD" ]; then
        cpu_write "$CPU_KEY" "$CPU_OLD" "$CPU_VALUE" || exit 1
    fi

    CPU_LIMIT=$(printf '%s\n' "$CPU_VALUE" | awk -F, '{for (i = 1; i <= NF; i++) seen[$i + 0] = 1; for (i in seen) n++; print n}')
    CPU_THREAD_KEY=dalvik.vm.$CPU_CHANNEL-threads
    CPU_THREADS=$(getprop "$CPU_THREAD_KEY")
    CPU_THREAD_VALUE=$(awk -v old="$CPU_THREADS" -v limit="$CPU_LIMIT" 'BEGIN {
        if (old ~ /^[0-9]+$/ && old + 0 >= 1 && old + 0 <= limit) print old
        else print limit
    }') || exit 1
    cpu_write "$CPU_THREAD_KEY" "$CPU_THREADS" "$CPU_THREAD_VALUE" || exit 1
done
