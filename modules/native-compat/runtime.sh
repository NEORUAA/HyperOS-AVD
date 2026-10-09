#!/system/bin/sh
# Runtime metadata is parsed as data; no catalog file is sourced or evaluated.
BB=${HYPEROS_NATIVE_BB:-/data/adb/ksu/bin/busybox}
PROC=${HYPEROS_NATIVE_PROC:-/proc}
PACKAGES_XML=${HYPEROS_NATIVE_PACKAGES_XML:-/data/system/packages.xml}
NATIVE_INTERVAL=30
NATIVE_CHAIN_LIMIT=8
# KernelSU may invoke hooks with umask 000. Runtime state is module-private.
umask 077

native_blocked() {
    [ -e "$MODDIR/disable" ] || [ -L "$MODDIR/disable" ] ||
        [ -e "$MODDIR/remove" ] || [ -L "$MODDIR/remove" ]
}

native_guard() {
    [ "$(id -u)" = 0 ] && [ "$(getprop ro.boot.hardware)" = ranchu ] || return 1
    case "$(getprop ro.mi.os.version.incremental)" in OS4.*|4.*) ;; *) return 1 ;; esac
    [ -x "$BB" ] && [ -d "$MODDIR" ] && [ ! -L "$MODDIR" ]
}

native_hash() { "$BB" sha256sum "$1" 2>/dev/null | "$BB" cut -d ' ' -f 1; }
native_ns() { local pid=$1; shift; "$BB" nsenter -t "$pid" -m -- "$@"; }
native_ns_hash() { native_ns "$1" "$BB" sha256sum "$2" 2>/dev/null | "$BB" cut -d ' ' -f 1; }
native_hex() { [ "${#1}" = 64 ] || return 1; case "$1" in *[!0-9a-f]*) return 1 ;; esac; }
native_word() { case "$1" in ''|*[!A-Za-z0-9_.-]*) return 1 ;; esac; }
native_path() {
    case "$1" in /system/*|/system_ext/*|/product/*|/vendor/*|/data/app/*|/data/app-lib/*) ;; *) return 1 ;; esac
    case "$1" in *'/../'*|*'/./'*|*'//'*) return 1 ;; esac
    [ "$(printf '%s' "$1" | "$BB" tr -d 'A-Za-z0-9_./=+~-')" = '' ]
}
native_relpath() {
    case "$1" in sites/*) ;; *) return 1 ;; esac
    case "$1" in *'..'*|*'//'*) return 1 ;; esac
    [ "$(printf '%s' "$1" | "$BB" tr -d 'A-Za-z0-9_./-')" = '' ] && [ ! -L "$MODDIR/$1" ]
}

native_log() {
    local file="$MODDIR/runtime.log"
    if [ -f "$file" ] && [ "$("$BB" stat -c %s "$file")" -gt 32768 ]; then
        "$BB" tail -c 24576 "$file" > "$file.next" && mv "$file.next" "$file"
    fi
    printf '%s %s\n' "$(date +%s)" "$*" >> "$file"
}

native_status() {
    local feature=$1 target=$2 state=$3 reason=$4 next="$MODDIR/state/status.tsv.next.$$"
    if [ -f "$MODDIR/state/status.tsv" ]; then
        "$BB" awk -F '|' -v feature="$feature" -v target="$target" \
            '!($1 == feature && $2 == target)' "$MODDIR/state/status.tsv" > "$next" || return 1
    else
        : > "$next"
    fi
    printf '%s|%s|%s|%s\n' "$feature" "$target" "$state" "$reason" >> "$next"
    mv "$next" "$MODDIR/state/status.tsv"
    native_log "$feature $target: $state ($reason)"
}

native_enabled() {
    [ ! -f "$MODDIR/features.disabled" ] || ! "$BB" grep -Fxq "$1" "$MODDIR/features.disabled"
}

native_eligible() {
    # Composer bytes are shared by phones and tablets. A matching ELF cannot
    # prove that this device has Xiaomi's physical rear-panel capability.
    [ "$1" = rear-display ] || return 0
    [ "$(getprop persist.sys.multi_display_type)" = 6 ] &&
        [ "$(getprop persist.sys.dual_screen_cover_mode_enable)" = true ] || return 1
    local secondary displays
    secondary=$(getprop persist.sys.secondary_builtin_display_id)
    case "$secondary" in ''|*[!0-9]*) return 1 ;; esac
    [ "${#secondary}" -le 10 ] && [ "$secondary" -gt 0 ] || return 1
    displays=$(getprop ro.boot.qemu.external.displays)
    case "$displays" in ''|*[!0-9,]*) return 1 ;; esac
    printf '%s\n' "$displays" | "$BB" awk -F ',' -v secondary="$secondary" '
        NF == 0 || NF % 5 != 0 {exit 1}
        {for (i=1; i<=NF; i++) if ($i !~ /^[0-9]+$/ || length($i) > 10) exit 1;
         for (i=1; i<=NF; i+=5) {
             if ($i <= 0 || $(i+1) <= 0 || $(i+2) <= 0 || $(i+3) <= 0 || seen[$i+0]++) exit 1;
             if ($i == secondary) found=1;
         } exit !found}'
}

native_assets() {
    [ -f "$MODDIR/SHA256SUMS" ] && [ ! -L "$MODDIR/SHA256SUMS" ] || return 1
    (cd "$MODDIR" && "$BB" sha256sum -c SHA256SUMS >/dev/null 2>&1) || return 1
    for file in profiles.tsv targets.tsv; do [ -f "$MODDIR/$file" ] && [ ! -L "$MODDIR/$file" ] || return 1; done
    [ ! -L "$MODDIR/cache" ] && [ ! -L "$MODDIR/state" ] || return 1
    mkdir -p "$MODDIR/cache" "$MODDIR/state" && chmod 700 "$MODDIR/cache" "$MODDIR/state"
}

native_pids() {
    { printf '1\n'; getprop init.svc_debug_pid.hyos_spawner; pidof zygote64 2>/dev/null || :;
      [ -z "$1" ] || pidof "$1" 2>/dev/null || :; } |
        "$BB" tr ' ' '\n' | "$BB" awk '/^[0-9]+$/ && !seen[$0]++ {print}'
}

# Mountinfo's root omits /data on the emulator userdata mount. Both spellings
# must refer to this module's own cache, never to another module or a user file.
native_owns_mount() {
    local pid=$1 target=$2 root1="$MODDIR/cache/" root2="${MODDIR#/data}/cache/"
    [ -r "$PROC/$pid/mountinfo" ] || return 1
    "$BB" awk -v target="$target" -v root1="$root1" -v root2="$root2" \
        '$5 == target {found=(index($4, root1) == 1 || index($4, root2) == 1)} END {exit !found}' \
        "$PROC/$pid/mountinfo"
}

native_detach() {
    local target=$1 package=$2 pid attempt
    for pid in $(native_pids "$package"); do
        [ -d "$PROC/$pid" ] || continue
        attempt=0
        while native_owns_mount "$pid" "$target"; do
            attempt=$((attempt + 1)); [ "$attempt" -le 4 ] || return 1
            native_ns "$pid" "$BB" umount "$target" || return 1
        done
    done
}

native_profile() {
    local feature=$1 checksum=$2
    native_hex "$checksum" || return 1
    "$BB" awk -F '|' -v feature="$feature" -v checksum="$checksum" \
        '$4 == feature && ($1 == checksum || index("," $3 ",", "," checksum ",") > 0) {print}' "$MODDIR/profiles.tsv"
}

native_is_output() {
    "$BB" awk -F '|' -v feature="$1" -v checksum="$2" \
        '$4 == feature && $2 == checksum {found=1} END {exit !found}' "$MODDIR/profiles.tsv"
}

native_superseded_output() {
    # An image may already contain composer alpha plus the later rear-display
    # patch. Follow catalog edges for this exact target instead of regressing
    # a verified terminal result to an intermediate composer-only payload.
    local rear=0
    native_eligible rear-display && rear=1
    "$BB" awk -F '|' -v feature="$1" -v checksum="$2" -v target="$3" -v rear="$rear" \
        'FNR == NR {if ($2 == "system" && $4 == target && ($3 != "rear-display" || rear)) allowed[$3]=1; next}
         allowed[$4] {n++; input[n]=$1; output[n]=$2; legacy[n]=$3; group[n]=$4;
             if ($4 == feature) reachable[$2]=1}
         END {for (depth=0; depth<8; depth++) {
             if (reachable[checksum]) exit 0;
             for (i=1; i<=n; i++) {hit=reachable[input[i]];
                 split(legacy[i], prior, ","); for (j in prior) if (reachable[prior[j]]) hit=1;
                 if (hit) reachable[output[i]]=1}
         } exit !reachable[checksum]}' "$MODDIR/targets.tsv" "$MODDIR/profiles.tsv"
}

native_related() {
    # An installed APK is PackageManager's signed package; verify its exact
    # embedded native code as well as the extracted library being modified.
    [ "$2" = "$3" ] && return 0
    "$BB" awk -F '|' -v feature="$1" -v entry="$2" -v actual="$3" \
        '$4 == feature && ($1 == entry || index("," $3 ",", "," entry ",") > 0) &&
            ($2 == actual || $1 == actual || index("," $3 ",", "," actual ",") > 0) {found=1}
         END {exit !found}' "$MODDIR/profiles.tsv"
}

native_patch_sites() {
    local working=$1 profile=$2 offset size before after extra current="$MODDIR/cache/site-check.$$" length
    native_word "$profile" || return 1
    [ -f "$MODDIR/sites/$profile.tsv" ] && [ ! -L "$MODDIR/sites/$profile.tsv" ] || return 1
    length=$("$BB" stat -c %s "$working") || return 1
    while IFS='|' read -r offset size before after extra; do
        [ -n "$offset" ] || continue
        [ -z "$extra" ] && native_relpath "$before" && native_relpath "$after" || return 1
        case "$offset" in 0x*) case "${offset#0x}" in ''|*[!0-9a-fA-F]*) return 1 ;; esac ;;
            ''|*[!0-9]*) return 1 ;; esac
        case "$size" in ''|*[!0-9]*) return 1 ;; esac
        [ "${#offset}" -le 12 ] && [ "${#size}" -le 6 ] || return 1
        offset=$((offset + 0)); size=$((size + 0))
        [ "$size" -gt 0 ] && [ "$size" -le 65536 ] && [ "$offset" -ge 0 ] && [ $((offset + size)) -le "$length" ] || return 1
        [ "$("$BB" stat -c %s "$MODDIR/$before")" = "$size" ] &&
            [ "$("$BB" stat -c %s "$MODDIR/$after")" = "$size" ] || return 1
        "$BB" dd if="$working" of="$current" bs=1 skip="$offset" count="$size" 2>/dev/null || return 1
        if "$BB" cmp -s "$current" "$MODDIR/$after"; then
            : # A legacy patched ELF may already carry earlier site edits.
        elif "$BB" cmp -s "$current" "$MODDIR/$before"; then
            "$BB" dd if="$MODDIR/$after" of="$working" bs=1 seek="$offset" conv=notrunc 2>/dev/null || return 1
        else
            rm -f "$current"
            return 1
        fi
    done < "$MODDIR/sites/$profile.tsv"
    rm -f "$current"
}

native_prepare() {
    # Return a content-addressed local copy. Published payload inodes are never
    # truncated, even when an older copy remains mapped by a running process.
    local target=$1 feature=$2 initial=$3 current=$3 selected before after legacy selected_feature profile chain=0
    local working="$MODDIR/cache/candidate.next.$$" mode owner uid gid extra context cached_context key result
    [ ! -e "$working" ] && [ ! -L "$working" ] || return 1
    (set -C; native_ns 1 "$BB" cat "$target" > "$working") || return 1
    [ "$(native_hash "$working")" = "$initial" ] || { rm -f "$working"; return 1; }
    while :; do
        selected=$(native_profile "$feature" "$current")
        [ -n "$selected" ] || break
        [ "$(printf '%s\n' "$selected" | "$BB" wc -l)" -eq 1 ] || { rm -f "$working"; return 1; }
        IFS='|' read -r before after legacy selected_feature profile <<EOF
$selected
EOF
        # The fifth field is the catalog profile ID, not executable content.
        native_hex "$before" && native_hex "$after" && native_word "$profile" || { rm -f "$working"; return 1; }
        chain=$((chain + 1)); [ "$chain" -le "$NATIVE_CHAIN_LIMIT" ] || { rm -f "$working"; return 1; }
        native_patch_sites "$working" "$profile" && [ "$(native_hash "$working")" = "$after" ] || { rm -f "$working"; return 1; }
        [ "$current" != "$after" ] || { rm -f "$working"; return 1; }
        current=$after
    done
    [ "$chain" -gt 0 ] || { rm -f "$working"; return 2; }
    mode=$(native_ns 1 "$BB" stat -c %a "$target") || { rm -f "$working"; return 1; }
    case "$mode" in ''|*[!0-7]*) rm -f "$working"; return 1 ;; esac
    owner=$(native_ns 1 "$BB" stat -c %u:%g "$target") || { rm -f "$working"; return 1; }
    IFS=':' read -r uid gid extra <<EOF
$owner
EOF
    case "$uid:$gid" in *[!0-9:]*) rm -f "$working"; return 1 ;; esac
    [ -n "$uid" ] && [ -n "$gid" ] && [ -z "$extra" ] || { rm -f "$working"; return 1; }
    context=$(native_ns 1 /system/bin/ls -Zd "$target" 2>/dev/null |
        "$BB" awk '{for(i=1;i<=NF;i++) if($i ~ /^u:object_r:[A-Za-z0-9_]+:s0$/) {print $i; exit}}')
    [ -n "$context" ] || { rm -f "$working"; return 1; }
    key=$(printf '%s' "$context" | "$BB" tr ':' '_')
    result="$MODDIR/cache/$current-$mode-$uid-$gid-$key.so"
    chown "$owner" "$working" && chmod "$mode" "$working" && chcon "$context" "$working" || { rm -f "$working"; return 1; }
    if [ -e "$result" ] || [ -L "$result" ]; then
        [ -f "$result" ] && [ ! -L "$result" ] && [ "$(native_hash "$result")" = "$current" ] || { rm -f "$working"; return 1; }
        cached_context=$(native_ns 1 /system/bin/ls -Zd "$result" 2>/dev/null |
            "$BB" awk '{for(i=1;i<=NF;i++) if($i ~ /^u:object_r:[A-Za-z0-9_]+:s0$/) {print $i; exit}}')
        [ "$(native_ns 1 "$BB" stat -c %a "$result")" = "$mode" ] &&
            [ "$(native_ns 1 "$BB" stat -c %u:%g "$result")" = "$owner" ] &&
            [ "$cached_context" = "$context" ] || { rm -f "$working"; return 1; }
        rm -f "$working"
    else
        mv "$working" "$result" || return 1
    fi
    printf '%s\n' "$result"
}

native_mount_rollback() {
    local target=$1 created=$2 pid
    # Only unwind binds created by this invocation. A verified output already
    # present in another namespace is outside this transaction.
    for pid in $created; do
        [ -d "$PROC/$pid" ] || continue
        if native_owns_mount "$pid" "$target"; then
            native_ns "$pid" "$BB" umount "$target" || native_log "$target: namespace $pid rollback needs restart"
        else native_log "$target: namespace $pid rollback source changed; preserving top mount"; fi
    done
}

native_mount_options() {
    local target=$1 payload=$2 final=$3 executable=0 feature mode owner context file expected_owner
    case "$target" in /vendor/bin/hw/*)
        case "${target##*/}" in *.so) ;; *) executable=1 ;; esac ;;
    esac
    if [ "$executable" = 0 ]; then
        printf '%s\n' remount,bind,ro,nosuid,nodev
        return 0
    fi
    # Only a catalog-verified HWC executable may clear the /data bind's nosuid
    # flag. The unchanged 0755 inode has no set-ID bits; the exception permits
    # the existing init -> HAL SELinux transition, not a new policy allowance.
    feature=$("$BB" awk -F '|' -v target="$target" \
        '$1 == "early" && $2 == "system" && $4 == target &&
            ($3 == "composer" || $3 == "rear-display") {print $3}' "$MODDIR/targets.tsv")
    [ -n "$feature" ] && native_hex "$final" && [ "$(native_hash "$payload")" = "$final" ] || return 1
    native_is_output composer "$final" ||
        { native_eligible rear-display && native_is_output rear-display "$final"; } || return 1
    expected_owner=$(native_ns 1 "$BB" stat -c %u:%g "$target") || return 1
    case "$expected_owner" in 0:0|0:2000) ;; *) return 1 ;; esac
    for file in "$target" "$payload"; do
        mode=$(native_ns 1 "$BB" stat -c %a "$file") || return 1
        owner=$(native_ns 1 "$BB" stat -c %u:%g "$file") || return 1
        context=$(native_ns 1 /system/bin/ls -Zd "$file" 2>/dev/null |
            "$BB" awk '{for(i=1;i<=NF;i++) if($i ~ /^u:object_r:[A-Za-z0-9_]+:s0$/) {print $i; exit}}')
        [ "$mode" = 755 ] && [ "$owner" = "$expected_owner" ] &&
            [ "$context" = u:object_r:hal_graphics_composer_default_exec:s0 ] || return 1
    done
    printf '%s\n' remount,bind,ro,suid,nodev,exec
}

native_mount_flags() {
    local pid=$1 target=$2 options=$3 executable=0
    case ",$options," in *,suid,*) executable=1 ;; esac
    [ -r "$PROC/$pid/mountinfo" ] && native_owns_mount "$pid" "$target" || return 1
    "$BB" awk -v target="$target" -v executable="$executable" '
        $5 == target {options=$6; found=1}
        END {if (!found) exit 1; n=split(options, parts, ","); for(i=1; i<=n; i++) flags[parts[i]]=1;
             if (!flags["ro"]) exit 1;
             if (executable) exit flags["nosuid"] || flags["noexec"];
             exit !flags["nosuid"]}' "$PROC/$pid/mountinfo"
}

native_mount() {
    local target=$1 payload=$2 package=$3 initial=$4 final=$5 pid actual pids created= options
    options=$(native_mount_options "$target" "$payload" "$final") || return 1
    pids=$(native_pids "$package")
    # Complete namespace preflight before creating the first bind.
    for pid in $pids; do
        [ -d "$PROC/$pid" ] || continue
        actual=$(native_ns_hash "$pid" "$target")
        [ "$actual" = "$initial" ] || [ "$actual" = "$final" ] || return 1
    done
    for pid in $pids; do
        [ -d "$PROC/$pid" ] || continue
        actual=$(native_ns_hash "$pid" "$target")
        [ "$actual" != "$final" ] || continue
        if ! native_ns "$pid" "$BB" mount -o bind "$payload" "$target"; then
            native_mount_rollback "$target" "$created"; return 1
        fi
        created="$pid $created"
        if ! native_ns "$pid" "$BB" mount -o "$options" "$target" ||
                [ "$(native_ns_hash "$pid" "$target")" != "$final" ] ||
                ! native_mount_flags "$pid" "$target" "$options"; then
            native_mount_rollback "$target" "$created"; return 1
        fi
    done
}

native_loaded_stale() {
    local target=$1 identity device inode major minor expected directory
    identity=$(native_ns 1 "$BB" stat -c '%d %i' "$target" 2>/dev/null) || return 1
    IFS=' ' read -r device inode <<EOF
$identity
EOF
    case "$device" in ''|*[!0-9]*) return 1 ;; esac
    case "$inode" in ''|*[!0-9]*) return 1 ;; esac
    # Linux dev_t uses the same encoding on Android ARM64. Compare both device
    # and inode: EROFS and /data can legitimately reuse an inode number.
    major=$(((device >> 8) & 4095 | (device >> 32) & -4096))
    minor=$((device & 255 | (device >> 12) & -256))
    expected=$(printf '%x:%x' "$major" "$minor")
    for directory in "$PROC"/[0-9]*; do
        [ -r "$directory/maps" ] || continue
        if "$BB" awk -v target="$target" -v expected="$expected" -v inode="$inode" \
            'function normal(value) {sub(/^0+/, "", value); return value == "" ? "0" : value}
             $6 == target && $5 != 0 {split($4, parts, ":"); device=normal(parts[1]) ":" normal(parts[2]);
                 if ($5 != inode || device != expected) found=1} END {exit !found}' "$directory/maps"; then return 0; fi
    done
    return 1
}

native_resetprop() {
    if [ -x /data/adb/ksu/bin/resetprop ]; then /data/adb/ksu/bin/resetprop -n "$@";
    elif [ -x /data/adb/ksud ]; then /data/adb/ksud resetprop -n "$@";
    else return 1; fi
}

native_hwui_policy() {
    [ "$2" = early ] || return 0
    native_is_output hwui "$(native_ns_hash 1 "$1")" || return 1
    [ "$(getprop ro.zygote.disable_gl_preload)" != 0 ] || return 0
    native_resetprop ro.zygote.disable_gl_preload 0 || return 1
    [ "$(getprop ro.zygote.disable_gl_preload)" = 0 ]
}

native_final_status() {
    local feature=$1 target=$2 kind=$3 phase=$4 reason=$5
    if [ "$feature" = hwui ] && ! native_hwui_policy "$target" "$phase"; then
        native_status "$feature" "$target" failed hwui-preload-policy-unavailable
    elif [ "$kind" = system ] && native_loaded_stale "$target"; then
        native_status "$feature" "$target" pending reboot-required
    else native_status "$feature" "$target" ready "$reason"; fi
}

native_active_apk() { pm path "$1" 2>/dev/null | "$BB" sed -n 's/^package://p' | "$BB" head -n 1; }
native_app_target() {
    local package=$1 library=$2 fields directory legacy abi extra
    native_word "$package" && native_word "$library" || return 1
    fields=$(dumpsys package "$package" 2>/dev/null | "$BB" awk -v package="$package" '
        function capture(key, value) {
            if (present[key] && values[key] != value) invalid=1;
            present[key]=1; values[key]=value;
        }
        /^[[:space:]]*Package \[/ {
            if (active) exit;
            name=$0; sub(/^[[:space:]]*Package \[/, "", name); sub(/\].*$/, "", name);
            if (name == package) active=1;
            headers=1; next;
        }
        /^[[:space:]]*(Hidden|Disabled) system packages:/ {if (active || !headers) exit}
        !headers || active {
            line=$0; sub(/^[[:space:]]*/, "", line);
            if (line ~ /^(nativeLibraryDir|legacyNativeLibraryDir|primaryCpuAbi)=/) {
                key=line; sub(/=.*/, "", key); value=line; sub(/^[^=]*=/, "", value);
                if (value ~ /[[:space:]]/) invalid=1;
                capture(key, value);
            }
        }
        END {
            if (invalid) exit 1;
            printf "%s|%s|%s", values["nativeLibraryDir"], values["legacyNativeLibraryDir"], values["primaryCpuAbi"];
        }') || return 1
    IFS='|' read -r directory legacy abi extra <<EOF
$fields
EOF
    [ -z "$extra" ] || return 1
    # Android 17 exposes a legacy root plus the ABI instead of the resolved
    # native directory. Never borrow fields from its disabled factory package.
    if [ -n "$directory" ]; then
        [ "$directory" != null ] || return 1
        [ -z "$abi" ] || [ "$abi" = arm64-v8a ] || return 1
        if [ -n "$legacy" ] && [ "$legacy" != null ]; then
            [ "$abi" = arm64-v8a ] && [ "$directory" = "$legacy/arm64" ] || return 1
        fi
    else
        [ -n "$legacy" ] && [ "$legacy" != null ] && [ "$abi" = arm64-v8a ] || return 1
        directory="$legacy/arm64"
    fi
    native_path "$directory/$library" || return 1
    printf '%s\n' "$directory/$library"
}

native_private_target() {
    native_path "$1" || return 1
    case "$1" in /data/app/*/lib/arm64/*|/data/app-lib/*/arm64/*) ;; *) return 1 ;; esac
    # A basename is required; do not accept nested paths after arm64.
    case "${1##*/arm64/}" in */*) return 1 ;; esac
    native_word "${1##*/}" && [ "${1##*/}" != . ] && [ "${1##*/}" != .. ]
}

native_data_parents() {
    # Reject aliases in every existing ancestor, including a target alias. Do
    # not manufacture a directory that PackageManager did not publish itself.
    native_ns "$1" "$BB" sh -c '
        target=$1
        [ ! -L "$target" ] || exit 1
        directory=${target%/*}
        while [ "$directory" != / ]; do
            [ -d "$directory" ] && [ ! -L "$directory" ] || exit 1
            directory=${directory%/*}; [ -n "$directory" ] || directory=/
        done
    ' sh "$2"
}

native_restore_missing() {
    local target=$1 entry=$2 entry_hash=$3 apk=$4 apk_hash=$5 package=$6 library=$7 feature=$8
    native_private_target "$target" || return 2
    native_data_parents 1 "$target" || return 3
    [ -n "$(native_profile "$feature" "$entry_hash")" ] || native_is_output "$feature" "$entry_hash" || return 2
    [ "$(native_active_apk "$package")" = "$apk" ] &&
        [ "$(native_app_target "$package" "$library")" = "$target" ] &&
        [ "$(native_ns_hash 1 "$apk")" = "$apk_hash" ] || return 4
    native_ns 1 "$BB" sh -c '
        set -e
        target=$1; entry=$2; before=$3; BB=$4
        [ ! -e "$target" ] && [ ! -L "$target" ]
        temporary="$target.hyperos-native-original.$$"
        [ ! -e "$temporary" ] && [ ! -L "$temporary" ]
        (set -C; cat "$entry" > "$temporary")
        trap '\''rm -f "$temporary"'\'' EXIT
        chmod 644 "$temporary"
        chcon u:object_r:apk_data_file:s0 "$temporary"
        [ "$("$BB" sha256sum "$temporary" | "$BB" cut -d " " -f 1)" = "$before" ]
        ln "$temporary" "$target"
    ' sh "$target" "$entry" "$entry_hash" "$BB"
}

native_app_unchanged() {
    local feature=$1 package=$2 library=$3 apk target checksum receipt final
    native_enabled "$feature" || return 1
    native_word "$feature" && native_word "$package" && native_word "$library" || return 1
    receipt="$MODDIR/state/$feature-$package-$library.receipt"
    [ -f "$receipt" ] && [ ! -L "$receipt" ] || return 1
    apk=$(native_active_apk "$package"); native_path "$apk" || return 1
    target=$(native_app_target "$package" "$library") || return 1
    checksum=$(native_ns_hash 1 "$apk"); final=$(native_ns_hash 1 "$target")
    native_hex "$checksum" && native_hex "$final" || return 1
    [ "$(cat "$receipt")" = "$NATIVE_CATALOG_HASH|$apk|$checksum|$target|$final" ]
}

native_save_app_receipt() {
    local feature=$1 package=$2 library=$3 apk=$4 apk_hash=$5 target=$6 final=$7 file
    native_word "$feature" && native_word "$package" && native_word "$library" || return 1
    file="$MODDIR/state/$feature-$package-$library.receipt"
    printf '%s|%s|%s|%s|%s\n' "$NATIVE_CATALOG_HASH" "$apk" "$apk_hash" "$target" "$final" > "$file.next.$$" &&
        mv "$file.next.$$" "$file"
}

native_old_app_target() {
    local file="$MODDIR/state/$1-$2-$3.receipt" value
    native_word "$1" && native_word "$2" && native_word "$3" || return 1
    [ -f "$file" ] && [ ! -L "$file" ] || return 1
    value=$("$BB" awk -F '|' 'NF == 5 {print $4}' "$file")
    native_path "$value" || return 1
    printf '%s\n' "$value"
}

native_apply_target() {
    local kind=$1 feature=$2 target=$3 package=$4 library=$5 phase=$6
    local initial payload final apk apk_hash entry entry_hash result
    if ! native_enabled "$feature"; then
        [ "$kind" != app ] || target=$(native_app_target "$package" "$library")
        native_status "$feature" "${target:-$package/$library}" disabled user-choice
        return
    fi
    if ! native_eligible "$feature"; then
        native_status "$feature" "${target:-$package/$library}" skipped device-capability-unavailable
        return
    fi
    if [ "$kind" = app ]; then
        native_word "$package" || { native_status "$feature" "$package/$library" skipped invalid-package; return; }
        native_app_unchanged "$feature" "$package" "$library" && return
        apk=$(native_active_apk "$package")
        native_path "$apk" || { native_status "$feature" "$package/$library" skipped package-unavailable; return; }
        target=$(native_app_target "$package" "$library") || { native_status "$feature" "$package/$library" skipped native-directory-unavailable; return; }
        apk_hash=$(native_ns_hash 1 "$apk")
        native_hex "$apk_hash" || { native_status "$feature" "$target" skipped apk-unreadable; return; }
        entry="$MODDIR/cache/apk-entry.next.$$"
        [ ! -e "$entry" ] && [ ! -L "$entry" ] || { native_status "$feature" "$target" failed staging-occupied; return; }
        native_ns 1 "$BB" unzip -p "$apk" "lib/arm64-v8a/$library" > "$entry" 2>/dev/null
        result=$?
        entry_hash=$(native_hash "$entry")
        [ "$result" = 0 ] && [ -s "$entry" ] && native_hex "$entry_hash" || { rm -f "$entry"; native_status "$feature" "$target" skipped apk-native-entry-unavailable; return; }
    fi
    native_path "$target" || { [ -z "$entry" ] || rm -f "$entry"; native_status "$feature" "${target:-invalid}" skipped invalid-target; return; }
    initial=$(native_ns_hash 1 "$target")
    if [ "$kind" = app ] && ! native_hex "$initial"; then
        native_restore_missing "$target" "$entry" "$entry_hash" "$apk" "$apk_hash" "$package" "$library" "$feature"
        result=$?
        case "$result" in
            0) initial=$(native_ns_hash 1 "$target") ;;
            2) rm -f "$entry"; native_status "$feature" "$target" skipped unsupported-missing-native; return ;;
            3) rm -f "$entry"; native_status "$feature" "$target" skipped missing-native-parent-or-alias; return ;;
            *) rm -f "$entry"; native_status "$feature" "$target" skipped native-publication-race; return ;;
        esac
    fi
    if [ "$kind" = app ]; then
        rm -f "$entry"
        case "$target" in /data/*) native_data_parents 1 "$target" || { native_status "$feature" "$target" skipped native-path-alias; return; } ;; esac
    fi
    native_hex "$initial" || { native_status "$feature" "$target" skipped unsupported-missing-native; return; }
    if [ "$kind" = app ]; then
        native_related "$feature" "$entry_hash" "$initial" || { native_status "$feature" "$target" skipped apk-native-code-mismatch; return; }
    fi
    if [ -z "$(native_profile "$feature" "$initial")" ]; then
        if native_is_output "$feature" "$initial" || { [ "$kind" = system ] && native_superseded_output "$feature" "$initial" "$target"; }; then
            native_final_status "$feature" "$target" "$kind" "$phase" already-patched
            [ "$kind" != app ] || native_save_app_receipt "$feature" "$package" "$library" "$apk" "$apk_hash" "$target" "$initial"
        else native_status "$feature" "$target" skipped unsupported-elf; fi
        return
    fi
    payload=$(native_prepare "$target" "$feature" "$initial")
    result=$?
    [ "$result" = 0 ] || { native_status "$feature" "$target" failed patch-verification; return; }
    final=$(native_hash "$payload")
    if [ "$kind" = app ]; then
        # The PackageManager replacement and extraction must have settled before
        # using a path captured earlier. Preserve the signed APK byte-for-byte.
        [ "$(native_active_apk "$package")" = "$apk" ] &&
            [ "$(native_app_target "$package" "$library")" = "$target" ] &&
            [ "$(native_ns_hash 1 "$apk")" = "$apk_hash" ] || { native_status "$feature" "$target" skipped package-changing; return; }
    fi
    native_blocked && return
    if native_mount "$target" "$payload" "$package" "$initial" "$final"; then
        native_final_status "$feature" "$target" "$kind" "$phase" verified-bind
        if [ "$kind" = app ]; then
            native_save_app_receipt "$feature" "$package" "$library" "$apk" "$apk_hash" "$target" "$final"
            printf '%s\n' "$package" >> "$MODDIR/restart-packages.next"
        fi
    else
        native_status "$feature" "$target" failed namespace-mount
    fi
}

native_resolve_target() {
    if [ "$1" = app ]; then native_app_target "$3" "$4"; else printf '%s\n' "$2"; fi
}

native_reconcile() {
    local phase=$1 row_phase kind feature target package library extra resolved initial previous_target blocked="$MODDIR/state/blocked.next.$$"
    NATIVE_CATALOG_HASH=$(native_hash "$MODDIR/profiles.tsv")
    : > "$MODDIR/restart-packages.next"
    : > "$blocked"
    # Strip only our bindings from this phase before proving underlying code.
    # Replaying targets in catalog order also permits composer -> rear chains.
    while IFS='|' read -r row_phase kind feature target package library extra; do
        [ "$row_phase" = "$phase" ] || continue
        [ -z "$extra" ] && native_word "$feature" && native_word "$library" || continue
        case "$kind" in system|app) ;; *) continue ;; esac
        if [ "$kind" = app ] && native_app_unchanged "$feature" "$package" "$library"; then continue; fi
        resolved=$(native_resolve_target "$kind" "$target" "$package" "$library") || resolved=
        if [ "$kind" = app ]; then
            previous_target=$(native_old_app_target "$feature" "$package" "$library") || previous_target=
            if [ -n "$previous_target" ] && [ "$previous_target" != "$resolved" ]; then
                if ! native_detach "$previous_target" "$package"; then
                    printf '%s\n' "${resolved:-$package/$library}" >> "$blocked"
                    native_status "$feature" "$previous_target" failed prior-app-detach
                    continue
                fi
                # The package may have been uninstalled, so PM cannot resolve
                # its old directory. Retire our receipt and bind, not PM files.
                rm -f "$MODDIR/state/$feature-$package-$library.receipt"
                native_status "$feature" "$previous_target" skipped retired-native-path
            fi
        fi
        native_path "$resolved" || continue
        if ! native_detach "$resolved" "$package"; then
            printf '%s\n' "$resolved" >> "$blocked"
            native_status "$feature" "$resolved" failed owned-detach
        fi
    done < "$MODDIR/targets.tsv"
    while IFS='|' read -r row_phase kind feature target package library extra; do
        native_blocked && break
        [ -z "$extra" ] && native_word "$feature" && native_word "$library" || continue
        case "$kind" in system|app) ;; *) continue ;; esac
        if [ "$row_phase" != "$phase" ]; then
            if [ "$phase" = late ] && [ "$row_phase" = early ]; then
                resolved=$(native_resolve_target "$kind" "$target" "$package" "$library") || continue
                native_path "$resolved" || continue
                if ! native_enabled "$feature"; then
                    native_status "$feature" "$resolved" disabled restart-for-loaded-code
                    continue
                fi
                if ! native_eligible "$feature"; then
                    native_status "$feature" "$resolved" skipped device-capability-unavailable
                    continue
                fi
                initial=$(native_ns_hash 1 "$resolved")
                if [ -n "$(native_profile "$feature" "$initial")" ] || native_loaded_stale "$resolved"; then native_status "$feature" "$resolved" pending reboot-required; fi
            fi
            continue
        fi
        resolved=$(native_resolve_target "$kind" "$target" "$package" "$library") || resolved=
        if "$BB" grep -Fxq "${resolved:-$package/$library}" "$blocked"; then continue; fi
        native_apply_target "$kind" "$feature" "$target" "$package" "$library" "$phase"
    done < "$MODDIR/targets.tsv"
    if [ "$phase" = late ] && ! native_blocked; then
        "$BB" sort -u "$MODDIR/restart-packages.next" | while IFS= read -r package; do
            [ -n "$package" ] || continue
            am force-stop "$package" >/dev/null 2>&1 || native_log "$package: could not stop stale native process"
        done
    fi
    rm -f "$MODDIR/restart-packages.next" "$blocked"
}

native_cleanup() {
    local phase kind feature target package library extra resolved previous
    while IFS='|' read -r phase kind feature target package library extra; do
        [ -z "$extra" ] && native_word "$feature" && native_word "$library" || continue
        case "$kind" in system|app) ;; *) continue ;; esac
        if [ "$kind" = app ]; then
            previous=$(native_old_app_target "$feature" "$package" "$library") || previous=
            if [ -n "$previous" ]; then
                native_detach "$previous" "$package" || native_log "$previous: owned bind remains until restart"
                native_status "$feature" "$previous" disabled restart-for-loaded-code
            fi
        else previous=; fi
        resolved=$(native_resolve_target "$kind" "$target" "$package" "$library") || continue
        native_path "$resolved" || continue
        [ "$resolved" != "$previous" ] || continue
        native_detach "$resolved" "$package" || native_log "$resolved: owned bind remains until restart"
        native_status "$feature" "$resolved" disabled restart-for-loaded-code
    done < "$MODDIR/targets.tsv"
}

native_start() {
    native_guard || return 1
    native_blocked && return 0
    native_assets || { native_log 'Module asset verification failed; preserving all native targets.'; return 1; }
    "$BB" sh "$MODDIR/dex2oat-cpu-policy.sh" apply || native_log 'ART CPU policy could not be applied.'
    native_reconcile "$1"
}

native_package_token() {
    "$BB" stat -c '%i:%Y:%s' "$PACKAGES_XML" 2>/dev/null || printf 'missing\n'
    "$BB" stat -c '%i:%Y:%s' "$MODDIR/features.disabled" 2>/dev/null || printf 'enabled\n'
}

native_lock_directory_safe() {
    local lock=$1 entry
    [ -d "$lock" ] && [ ! -L "$lock" ] || return 1
    for entry in "$lock"/* "$lock"/.[!.]* "$lock"/..?*; do
        [ -e "$entry" ] || [ -L "$entry" ] || continue
        case "$entry" in "$lock/pid"|"$lock/boot") ;; *) return 1 ;; esac
        [ -f "$entry" ] && [ ! -L "$entry" ] || return 1
    done
}

native_service_owner_live() {
    local directory pid
    for directory in "$PROC"/[0-9]*; do
        pid=${directory##*/}; [ "$pid" != "$$" ] || continue
        [ -r "$directory/cmdline" ] || continue
        if "$BB" tr '\000' '\n' < "$directory/cmdline" | "$BB" grep -Fxq "$MODDIR/service.sh"; then return 0; fi
    done
    return 1
}

native_service_release() {
    # The trap runs after native_service has returned. These captured ownership
    # values deliberately outlive its local variables.
    if [ "${NATIVE_LOCK_HELD:-false}" = true ] &&
            [ "${NATIVE_LOCK_PATH:-}" = "$MODDIR/service.lock" ] &&
            native_lock_directory_safe "$NATIVE_LOCK_PATH" &&
            [ "$(cat "$NATIVE_LOCK_PATH/pid" 2>/dev/null)" = "$NATIVE_LOCK_PID" ] &&
            [ "$(cat "$NATIVE_LOCK_PATH/boot" 2>/dev/null)" = "$NATIVE_LOCK_BOOT" ]; then
        rm -f "$NATIVE_LOCK_PATH/pid" "$NATIVE_LOCK_PATH/boot"
        rmdir "$NATIVE_LOCK_PATH" 2>/dev/null || :
    fi
    exec 9>&-
    NATIVE_LOCK_HELD=false
}

native_service_lock() {
    local lock=$1 guard="$1.guard" boot oldpid oldboot
    [ "$lock" = "$MODDIR/service.lock" ] || return 1
    boot=$(cat "$PROC/sys/kernel/random/boot_id") || return 1
    [ -n "$boot" ] || return 1
    [ ! -L "$guard" ] && { [ ! -e "$guard" ] || [ -f "$guard" ]; } || return 1
    # This lease serializes recovery with acquisition and is dropped by the
    # kernel on a crash. The directory remains a human-readable owner receipt.
    exec 9>"$guard" || return 1
    if ! "$BB" flock -n 9; then exec 9>&-; return 1; fi
    NATIVE_LOCK_HELD=true
    NATIVE_LOCK_PATH="$lock"; NATIVE_LOCK_PID=$$; NATIVE_LOCK_BOOT="$boot"
    chmod 600 "$guard" || { native_service_release; return 1; }
    if ! mkdir "$lock" 2>/dev/null; then
        native_lock_directory_safe "$lock" || { native_service_release; return 1; }
        # Give a legacy hook time to publish owner fields, then reject any live
        # exact owner, including one still acquiring an incomplete lock.
        sleep 1
        native_lock_directory_safe "$lock" || { native_service_release; return 1; }
        native_service_owner_live && { native_service_release; return 1; }
        oldpid=$(cat "$lock/pid" 2>/dev/null); oldboot=$(cat "$lock/boot" 2>/dev/null)
        case "$oldpid" in *[!0-9]*) native_service_release; return 1 ;; esac
        if [ "$oldboot" = "$boot" ] && [ -r "$PROC/$oldpid/cmdline" ] &&
            "$BB" tr '\000' '\n' < "$PROC/$oldpid/cmdline" | "$BB" grep -Fxq "$MODDIR/service.sh"; then native_service_release; return 1; fi
        rm -f "$lock/pid" "$lock/boot"
        rmdir "$lock" && mkdir "$lock" || { native_service_release; return 1; }
    fi
    chmod 700 "$lock" &&
        printf '%s\n' "$$" > "$lock/pid" && printf '%s\n' "$boot" > "$lock/boot" || { native_service_release; return 1; }
}

native_service() {
    local count=0 previous token stable lock="$MODDIR/service.lock"
    native_guard || return 1
    native_blocked && return 0
    native_assets || { native_log 'Module asset verification failed; preserving all native targets.'; return 1; }
    native_service_lock "$lock" || return 0
    trap 'native_service_release' EXIT
    while [ "$(getprop sys.boot_completed)" != 1 ]; do
        native_blocked && return 0
        count=$((count + 1)); [ "$count" -lt 300 ] || return 1
        sleep 1
    done
    native_blocked && return 0
    native_assets || { native_log 'Module assets changed while waiting for boot; preserving targets.'; return 1; }
    "$BB" sh "$MODDIR/dex2oat-cpu-policy.sh" apply || native_log 'ART CPU policy could not be applied.'
    native_reconcile late
    previous=$(native_package_token)
    while :; do
        sleep "$NATIVE_INTERVAL"
        if native_blocked; then native_cleanup; return 0; fi
        token=$(native_package_token)
        [ "$token" != "$previous" ] || continue
        sleep 2
        stable=$(native_package_token)
        [ "$stable" = "$token" ] || continue
        native_reconcile late
        previous=$stable
    done
}
