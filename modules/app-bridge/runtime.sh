#!/system/bin/sh
# Exact signed-workload overlays; all profiles are data, never shell input.
BB=${HYPEROS_BRIDGE_BB:-/data/adb/ksu/bin/busybox}
PROC=${HYPEROS_BRIDGE_PROC:-/proc}
BRIDGE_INTERVAL=30
umask 077

bridge_blocked() {
    [ -e "$MODDIR/disable" ] || [ -L "$MODDIR/disable" ] ||
        [ -e "$MODDIR/remove" ] || [ -L "$MODDIR/remove" ] ||
        [ -e "/data/adb/modules_update/${MODDIR##*/}" ] ||
        [ -L "/data/adb/modules_update/${MODDIR##*/}" ]
}
bridge_hash() { "$BB" sha256sum "$1" 2>/dev/null | "$BB" cut -d ' ' -f 1; }
bridge_ns() { local pid=$1; shift; "$BB" nsenter -t "$pid" -m -- "$@"; }
bridge_ns_hash() { bridge_ns "$1" "$BB" sha256sum "$2" 2>/dev/null | "$BB" cut -d ' ' -f 1; }
bridge_word() { case "$1" in ''|*[!A-Za-z0-9_.-]*) return 1 ;; esac; }
bridge_path() {
    case "$1" in /product/*|/system/*|/system_ext/*|/data/app/*|/data/app-lib/*) ;; *) return 1 ;; esac
    case "$1" in *'/../'*|*'/./'*|*'//'*) return 1 ;; esac
    [ "$(printf '%s' "$1" | "$BB" tr -d 'A-Za-z0-9_./=+~-')" = '' ]
}
bridge_hex() { [ "${#1}" = 64 ] || return 1; case "$1" in *[!0-9a-f]*) return 1 ;; esac; }
bridge_log() {
    [ ! -L "$MODDIR/runtime.log" ] || return 1
    if [ -f "$MODDIR/runtime.log" ] && [ "$("$BB" stat -c %s "$MODDIR/runtime.log")" -gt 32768 ]; then
        "$BB" tail -c 24576 "$MODDIR/runtime.log" > "$MODDIR/runtime.log.next" &&
            mv "$MODDIR/runtime.log.next" "$MODDIR/runtime.log"
    fi
    printf '%s %s\n' "$(date +%s)" "$*" >> "$MODDIR/runtime.log"
}
bridge_assets() {
    local file
    [ -d "$MODDIR" ] && [ ! -L "$MODDIR" ] && [ -x "$BB" ] || return 1
    for file in SHA256SUMS profiles.tsv libraries.tsv systems.tsv callers.tsv package.txt payloads cache state; do
        [ ! -L "$MODDIR/$file" ] || return 1
    done
    for file in "$MODDIR"/*.sh "$MODDIR"/*.tsv "$MODDIR/module.prop" "$MODDIR/package.txt" \
            "$MODDIR/manifest.json" "$MODDIR/SHA256SUMS"; do
        [ -f "$file" ] && [ ! -L "$file" ] && [ "$("$BB" stat -c %h "$file")" = 1 ] || return 1
    done
    (cd "$MODDIR" && "$BB" sha256sum -c SHA256SUMS >/dev/null 2>&1) || return 1
    for file in "$MODDIR"/payloads/*; do
        [ -f "$file" ] && [ ! -L "$file" ] && [ "$("$BB" stat -c %h "$file")" = 1 ] || return 1
    done
    mkdir -p "$MODDIR/cache" "$MODDIR/state" && chmod 700 "$MODDIR/cache" "$MODDIR/state" || return 1
    for file in "$MODDIR"/state/*; do
        [ -e "$file" ] || [ -L "$file" ] || continue
        bridge_state_file "$file" || return 1
    done
    PACKAGE=$(cat "$MODDIR/package.txt")
    bridge_word "$PACKAGE"
}
bridge_state_file() {
    [ -f "$1" ] && [ ! -L "$1" ] && [ "$("$BB" stat -c '%h:%u' "$1")" = 1:0 ]
}
bridge_guard() {
    [ "$(id -u)" = 0 ] && [ "$(getprop ro.boot.hardware)" = ranchu ] || return 1
    case "$(getprop ro.mi.os.version.incremental)" in OS4.*|4.*) ;; *) return 1 ;; esac
    ! bridge_blocked
}
bridge_pids() {
    { printf '1\n'; getprop init.svc_debug_pid.hyos_spawner; pidof zygote64 2>/dev/null || :;
      pidof "$PACKAGE" 2>/dev/null || :; } |
        "$BB" tr ' ' '\n' | "$BB" awk '/^[0-9]+$/ && !seen[$0]++ {print}'
}
bridge_regular() {
    local pid=$1 target=$2 allow_missing=$3 parent=$2
    bridge_path "$target" || return 1
    while [ "$parent" != / ]; do
        bridge_ns "$pid" test ! -L "$parent" || return 1
        parent=${parent%/*}; [ -n "$parent" ] || parent=/
        bridge_ns "$pid" test -d "$parent" || return 1
    done
    if bridge_ns "$pid" test -e "$target"; then
        bridge_ns "$pid" test -f "$target" &&
            [ "$(bridge_ns "$pid" "$BB" stat -c %h "$target")" = 1 ]
    else [ "$allow_missing" = 1 ]; fi
}
bridge_owned_mount() {
    local pid=$1 target=$2
    [ -r "$PROC/$pid/mountinfo" ] || return 1
    "$BB" awk -v target="$target" -v root1="$MODDIR/cache/" -v root2="${MODDIR#/data}/cache/" \
        '$5 == target {found=(index($4, root1)==1 || index($4, root2)==1)} END {exit !found}' "$PROC/$pid/mountinfo"
}
bridge_any_owned_mount() {
    [ -r "$PROC/$1/mountinfo" ] || return 1
    "$BB" awk -v target="$2" -v root1="$MODDIR/cache/" -v root2="${MODDIR#/data}/cache/" \
        '$5 == target && (index($4, root1)==1 || index($4, root2)==1) {found=1} END {exit !found}' \
        "$PROC/$1/mountinfo"
}
bridge_remember() {
    local target=$1 marker=$2 identity=$3 receipt="$MODDIR/state/owned.tsv" next
    [ ! -L "$MODDIR/state/owned.tsv" ] || return 1
    [ ! -e "$MODDIR/state/owned.tsv" ] || bridge_state_file "$MODDIR/state/owned.tsv" || return 1
    if [ "$marker" = placeholder ]; then
        # A failed transaction may have removed/restored the earlier inode.
        # Authenticate the newly created empty inode and replace its receipt
        # atomically, so a later disable/uninstall owns the current placeholder.
        bridge_regular 1 "$target" 0 &&
            [ "$(bridge_ns 1 "$BB" stat -c '%d:%i' "$target")" = "$identity" ] &&
            [ "$(bridge_ns_hash 1 "$target")" = e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855 ] || return 1
        next=$("$BB" mktemp "$MODDIR/state/owned.remember.XXXXXX") || return 1
        if [ -e "$receipt" ]; then
            "$BB" awk -F '|' -v target="$target" '$1 != target {print}' "$receipt" > "$next" ||
                { rm -f "$next"; return 1; }
        fi
        printf '%s|%s|%s\n' "$target" "$marker" "$identity" >> "$next" &&
            mv "$next" "$receipt"
        return
    fi
    "$BB" grep -Fq "$target|" "$MODDIR/state/owned.tsv" 2>/dev/null ||
        printf '%s|%s|%s\n' "$target" "$marker" "$identity" >> "$MODDIR/state/owned.tsv"
}
bridge_is_mount() {
    [ -r "$PROC/$1/mountinfo" ] || return 1
    "$BB" awk -v target="$2" '$5 == target {found=1} END {exit !found}' "$PROC/$1/mountinfo"
}
bridge_loaded() {
    local target=$1 directory
    for directory in "$PROC"/[0-9]*; do
        [ -r "$directory/maps" ] || continue
        "$BB" awk -v target="$target" '$6 == target && $5 != 0 {found=1} END {exit !found}' "$directory/maps" &&
            return 0
    done
    return 1
}
bridge_cleanup() {
    local keep=${1:-} target pid marker identity current remaining="$MODDIR/state/owned.next.$$"
    [ -f "$MODDIR/state/owned.tsv" ] || return 0
    bridge_state_file "$MODDIR/state/owned.tsv" && [ ! -e "$remaining" ] && [ ! -L "$remaining" ] || return 1
    : > "$remaining" || return 1
    while IFS='|' read -r target marker identity; do
        bridge_path "$target" || { rm -f "$remaining"; return 1; }
        if { [ -n "$keep" ] && [ "${target%/*}" = "$keep" ]; } || bridge_loaded "$target"; then
            printf '%s|%s|%s\n' "$target" "$marker" "$identity" >> "$remaining"; continue
        fi
        current=0
        for pid in $(bridge_pids); do
            [ -d "$PROC/$pid" ] || continue
            # Never peel a foreign top bind or use a lazy unmount.
            if bridge_owned_mount "$pid" "$target"; then
                bridge_ns "$pid" "$BB" umount "$target" || current=1
            fi
            bridge_any_owned_mount "$pid" "$target" && current=1
        done
        if [ "$current" = 1 ] ||
                { [ "$marker" = placeholder ] && bridge_target_mounted "$target"; }; then
            # A foreign bind may hide our empty inode after our own bind has
            # gone. Keep its identity until the foreign cover is released.
            printf '%s|%s|%s\n' "$target" "$marker" "$identity" >> "$remaining"; continue
        fi
        if [ "$marker" = placeholder ] && bridge_regular 1 "$target" 0; then
            current=$(bridge_ns 1 "$BB" stat -c '%d:%i' "$target")
            if [ "$current" = "$identity" ] && [ "$(bridge_ns_hash 1 "$target")" = e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855 ]; then
                bridge_ns 1 rm "$target" || return 1
            fi
        fi
    done < "$MODDIR/state/owned.tsv"
    mv "$remaining" "$MODDIR/state/owned.tsv"
}
bridge_rollback() {
    local transaction=$1 pid target identity backup checksum remaining unresolved=0
    bridge_state_file "$transaction" || return 1
    remaining=$("$BB" mktemp "$transaction.next.XXXXXX") || return 1
    while IFS='|' read -r pid target; do
        bridge_path "$target" || return 1
        [ -d "$PROC/$pid" ] || continue
        if bridge_owned_mount "$pid" "$target" && ! bridge_loaded "$target"; then
            bridge_ns "$pid" "$BB" umount "$target" || :
        fi
        if bridge_any_owned_mount "$pid" "$target"; then
            printf '%s|%s\n' "$pid" "$target" >> "$remaining"; unresolved=1
        fi
    done < "$transaction"
    mv "$remaining" "$transaction" || return 1
    # Atomic restoration never truncates the original helper inode or detaches
    # a live mapping. Keep the receipt until every namespace has released it.
    if [ -e "$transaction.legacy" ] || [ -L "$transaction.legacy" ]; then
        bridge_state_file "$transaction.legacy" || return 1
        : > "$remaining" || return 1
        while IFS='|' read -r target identity backup checksum; do
            bridge_path "$target" && bridge_hex "$checksum" || return 1
            case "$backup" in "$MODDIR"/cache/legacy-*.original) ;; *) return 1 ;; esac
            if bridge_target_mounted "$target" ||
                    ! bridge_regular 1 "$target" 0 ||
                    [ "$(bridge_ns 1 "$BB" stat -c '%d:%i' "$target")" != "$identity" ] ||
                    [ "$(bridge_ns_hash 1 "$target")" != e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855 ] ||
                    [ ! -f "$backup" ] || [ -L "$backup" ] ||
                    [ "$("$BB" stat -c %h "$backup")" != 1 ] ||
                    [ "$(bridge_hash "$backup")" != "$checksum" ] ||
                    ! bridge_ns 1 mv "$backup" "$target"; then
                printf '%s|%s|%s|%s\n' "$target" "$identity" "$backup" "$checksum" >> "$remaining"
                unresolved=1
            fi
        done < "$transaction.legacy"
        mv "$remaining" "$transaction.legacy" || return 1
        [ -s "$transaction.legacy" ] || rm -f "$transaction.legacy"
    fi
    if [ -e "$transaction.placeholders" ] || [ -L "$transaction.placeholders" ]; then
        bridge_state_file "$transaction.placeholders" || return 1
        : > "$remaining" || return 1
        while IFS='|' read -r target identity; do
            bridge_path "$target" || return 1
            if bridge_target_mounted "$target"; then
                printf '%s|%s\n' "$target" "$identity" >> "$remaining"; unresolved=1
            elif bridge_regular 1 "$target" 0 &&
                    [ "$(bridge_ns 1 "$BB" stat -c '%d:%i' "$target")" = "$identity" ] &&
                    [ "$(bridge_ns_hash 1 "$target")" = e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855 ]; then
                bridge_ns 1 rm "$target" || {
                    printf '%s|%s\n' "$target" "$identity" >> "$remaining"; unresolved=1;
                }
            fi
        done < "$transaction.placeholders"
        mv "$remaining" "$transaction.placeholders" || return 1
        [ -s "$transaction.placeholders" ] || rm -f "$transaction.placeholders"
    fi
    [ "$unresolved" = 1 ] || rm -f "$transaction"
    [ "$unresolved" = 0 ]
}
bridge_target_mounted() {
    local pid
    for pid in $(bridge_pids); do
        [ -d "$PROC/$pid" ] || continue
        bridge_is_mount "$pid" "$1" && return 0
    done
    return 1
}
bridge_recover() {
    local transaction result=0
    for transaction in "$MODDIR"/state/transaction.*; do
        case "$transaction" in *.legacy|*.placeholders|*.next|*.next.*) continue ;; esac
        [ -e "$transaction" ] || continue
        bridge_rollback "$transaction" || result=1
    done
    [ "$result" = 0 ]
}
bridge_context() {
    bridge_ns "$1" /system/bin/ls -Zd "$2" 2>/dev/null |
        "$BB" awk '{for(i=1;i<=NF;i++) if($i ~ /^u:object_r:[A-Za-z0-9_]+:s0$/) {print $i; exit}}'
}
bridge_payload() {
    local target=$1 name=$2 after=$3 placeholder=$4 mode owner context key working result
    if bridge_ns 1 test -f "$target"; then
        mode=$(bridge_ns 1 "$BB" stat -c %a "$target")
        owner=$(bridge_ns 1 "$BB" stat -c %u:%g "$target")
        context=$(bridge_context 1 "$target")
    else
        [ "$placeholder" = 1 ] || return 1
        mode=644; owner=$(bridge_ns 1 "$BB" stat -c %u:%g "${target%/*}")
        context=u:object_r:apk_data_file:s0
    fi
    case "$mode:$owner" in *[!0-9:]*) return 1 ;; esac
    [ -n "$mode" ] && [ -n "$owner" ] && [ -n "$context" ] || return 1
    key=$(printf '%s' "$context" | "$BB" tr ':' '_')
    result="$MODDIR/cache/$after-$mode-${owner%:*}-${owner#*:}-$key.so"
    if [ -e "$result" ] || [ -L "$result" ]; then
        [ -f "$result" ] && [ ! -L "$result" ] && [ "$(bridge_hash "$result")" = "$after" ] &&
            [ "$("$BB" stat -c %h "$result")" = 1 ] &&
            [ "$("$BB" stat -c %a "$result")" = "$mode" ] &&
            [ "$("$BB" stat -c %u:%g "$result")" = "$owner" ] &&
            [ "$(bridge_context 1 "$result")" = "$context" ] || return 1
    else
        working="$result.next.$$"; [ ! -e "$working" ] && [ ! -L "$working" ] || return 1
        (set -C; cat "$MODDIR/payloads/$name" > "$working") &&
            [ "$(bridge_hash "$working")" = "$after" ] &&
            chown "$owner" "$working" && chmod "$mode" "$working" && chcon "$context" "$working" &&
            mv "$working" "$result" || { rm -f "$working"; return 1; }
    fi
    printf '%s\n' "$result"
}
bridge_placeholder() {
    local target=$1 payload=$2 transaction=$3 temporary identity
    temporary="$target.hyperos-avd-placeholder.$$"
    [ ! -e "$transaction.placeholders" ] || bridge_state_file "$transaction.placeholders" || return 1
    [ ! -L "$transaction.placeholders" ] || return 1
    bridge_ns 1 sh -c '
        set -e
        [ ! -e "$2" ] && [ ! -L "$2" ]
        (set -C; : > "$2")
        trap '"'"'rm -f "$2"'"'"' EXIT
        chown "$("$4" stat -c %u:%g "$3")" "$2"
        chmod "$("$4" stat -c %a "$3")" "$2"
        chcon "$5" "$2"
        ln "$2" "$1"
    ' sh "$target" "$temporary" "$payload" "$BB" "$(bridge_context 1 "$payload")" || return 1
    identity=$(bridge_ns 1 "$BB" stat -c '%d:%i' "$target")
    bridge_remember "$target" placeholder "$identity" || return 1
    printf '%s|%s\n' "$target" "$identity" >> "$transaction.placeholders"
}
bridge_migrate_direct() {
    local target=$1 payload=$2 actual=$3 transaction=$4 temporary backup identity
    # Exact obsolete project helpers are replaced by an empty owned inode.
    # A hard-linked backup retains the old inode for any live mappings and
    # rollback; it is never chmodded or overwritten.
    bridge_is_mount 1 "$target" && return 0
    temporary="$target.hyperos-avd-placeholder.$$"
    [ ! -e "$transaction.legacy" ] || bridge_state_file "$transaction.legacy" || return 1
    [ ! -L "$transaction.legacy" ] || return 1
    BRIDGE_LEGACY_COUNTER=$((${BRIDGE_LEGACY_COUNTER:-0}+1))
    backup="$MODDIR/cache/legacy-$actual-$$-$BRIDGE_LEGACY_COUNTER.original"
    bridge_ns 1 sh -c '
        set -e
        [ -f "$1" ] && [ ! -L "$1" ]
        [ "$("$5" stat -c %h "$1")" = 1 ]
        [ "$("$5" sha256sum "$1" | "$5" cut -d " " -f 1)" = "$6" ]
        [ ! -e "$2" ] && [ ! -L "$2" ] && [ ! -e "$4" ] && [ ! -L "$4" ]
        (set -C; : > "$2")
        trap '"'"'rm -f "$2"'"'"' EXIT
        chown "$("$5" stat -c %u:%g "$3")" "$2"
        chmod "$("$5" stat -c %a "$3")" "$2"
        chcon "$7" "$2"
        ln "$1" "$4"
        mv -f "$2" "$1" || { rm -f "$4"; exit 1; }
    ' sh "$target" "$temporary" "$payload" "$backup" "$BB" "$actual" \
        "$(bridge_context 1 "$payload")" || return 1
    identity=$(bridge_ns 1 "$BB" stat -c '%d:%i' "$target")
    bridge_remember "$target" placeholder "$identity" || return 1
    printf '%s|%s|%s|%s\n' "$target" "$identity" "$backup" "$actual" >> "$transaction.legacy"
    BRIDGE_DIRECT_MIGRATED=1
}
bridge_apply() {
    local apk=$1 profile=$2 native=$3 pids pid selected name before after placeholder legacy extra target actual payload
    local system expected transaction="$MODDIR/state/transaction.$$" changed=0 current
    pids=$(bridge_pids)
    for pid in $pids; do
        [ -d "$PROC/$pid" ] || continue
        bridge_regular "$pid" "$apk" 0 &&
            [ "$(bridge_ns_hash "$pid" "$apk")" = "$APK_HASH" ] || return 1
        while IFS='|' read -r selected system expected extra; do
            [ "$selected" = "$profile" ] || continue
            [ -z "$extra" ] && bridge_path "$system" && bridge_hex "$expected" &&
                bridge_regular "$pid" "$system" 0 &&
                [ "$(bridge_ns_hash "$pid" "$system")" = "$expected" ] || return 1
        done < "$MODDIR/systems.tsv"
        while IFS='|' read -r selected name expected extra; do
            [ "$selected" = "$profile" ] || continue
            [ -z "$extra" ] && bridge_word "$name" && bridge_hex "$expected" &&
                bridge_regular "$pid" "$native/$name" 0 &&
                [ "$(bridge_ns_hash "$pid" "$native/$name")" = "$expected" ] || return 1
        done < "$MODDIR/callers.tsv"
        while IFS='|' read -r selected name before after placeholder legacy extra; do
            [ "$selected" = "$profile" ] || continue
            target="$native/$name"
            [ -z "$extra" ] && bridge_word "$name" && bridge_hex "$before" && bridge_hex "$after" &&
                bridge_regular "$pid" "$target" "$placeholder" || return 1
            [ "$(bridge_hash "$MODDIR/payloads/$name")" = "$after" ] || return 1
            actual=$(bridge_ns_hash "$pid" "$target")
            [ -n "$actual" ] || actual=e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
            case ",$before,$after,$legacy," in *",$actual,"*) ;; *) return 1 ;; esac
        done < "$MODDIR/libraries.tsv"
    done
    [ ! -e "$transaction" ] && [ ! -L "$transaction" ] || return 1
    : > "$transaction" || return 1
    while IFS='|' read -r selected name before after placeholder legacy extra; do
        [ "$selected" = "$profile" ] || continue
        bridge_guard || { bridge_rollback "$transaction"; return 1; }
        target="$native/$name"
        payload=$(bridge_payload "$target" "$name" "$after" "$placeholder") || { bridge_rollback "$transaction"; return 1; }
        actual=$(bridge_ns_hash 1 "$target")
        BRIDGE_DIRECT_MIGRATED=0
        if [ "$placeholder" = 1 ] && [ -n "$legacy" ]; then
            case ",$legacy," in *",$actual,"*)
                bridge_migrate_direct "$target" "$payload" "$actual" "$transaction" || { bridge_rollback "$transaction"; return 1; } ;;
            esac
        fi
        [ "$BRIDGE_DIRECT_MIGRATED" = 0 ] || changed=1
        if ! bridge_ns 1 test -e "$target"; then
            bridge_placeholder "$target" "$payload" "$transaction" || { bridge_rollback "$transaction"; return 1; }
        fi
        for pid in $pids; do
            [ -d "$PROC/$pid" ] || continue
            bridge_guard || { bridge_rollback "$transaction"; return 1; }
            actual=$(bridge_ns_hash "$pid" "$target")
            [ "$actual" != "$after" ] || continue
            case ",$before,$legacy," in *",$actual,"*) ;; *) bridge_rollback "$transaction"; return 1 ;; esac
            bridge_ns "$pid" "$BB" mount -o bind "$payload" "$target" || { bridge_rollback "$transaction"; return 1; }
            printf '%s|%s\n' "$pid" "$target" >> "$transaction"
            bridge_remember "$target" bind - || { bridge_rollback "$transaction"; return 1; }
            changed=1
            bridge_ns "$pid" "$BB" mount -o remount,bind,ro,nosuid,nodev "$target" &&
                [ "$(bridge_ns_hash "$pid" "$target")" = "$after" ] &&
                bridge_owned_mount "$pid" "$target" || {
                    bridge_rollback "$transaction"; return 1
                }
        done
    done < "$MODDIR/libraries.tsv"
    rm -f "$transaction" "$transaction.legacy" "$transaction.placeholders"
    if [ "$changed" = 1 ] && [ -n "$(pidof "$PACKAGE" 2>/dev/null)" ]; then
        current=$(pm path "$PACKAGE" 2>/dev/null | "$BB" sed -n 's/^package://p')
        if bridge_guard && [ "$current" = "$apk" ] && [ "$(bridge_ns_hash 1 "$apk")" = "$APK_HASH" ]; then
            # Reload only this still-verified app's private libraries. Never
            # restart zygote/HALs or stop a newer unknown signed workload.
            am force-stop "$PACKAGE" || return 1
            bridge_log "$PACKAGE: app stopped once to reload verified private libraries"
        fi
    fi
}
bridge_once() {
    local paths apk token checksum row profile factory native extra directory pose pid confirm
    bridge_guard || return 1
    bridge_recover || { bridge_log 'Private bridge rollback is awaiting safe namespace release.'; return 1; }
    paths=$(pm path "$PACKAGE" 2>/dev/null | "$BB" sed -n 's/^package://p')
    if [ -z "$paths" ]; then BRIDGE_LAST_TOKEN=; bridge_cleanup; return 0; fi
    [ "$(printf '%s\n' "$paths" | "$BB" wc -l)" -eq 1 ] || { bridge_cleanup; return 0; }
    apk=$paths; bridge_path "$apk" && bridge_regular 1 "$apk" 0 || { bridge_cleanup; return 0; }
    directory="${apk%/*}/lib/arm64"
    row=$("$BB" awk -F '|' -v apk="$apk" '$3 == apk {print $4}' "$MODDIR/profiles.tsv")
    [ -z "$row" ] || directory=$row
    token="$apk|$(bridge_ns 1 "$BB" stat -c '%d:%i:%s:%Y' "$apk")|$(bridge_ns 1 "$BB" stat -c '%d:%i:%Y' "$directory" 2>/dev/null)"
    pose=
    for pid in $(bridge_pids); do
        pose="$pose|$pid:$("$BB" stat -c %i "$PROC/$pid/ns/mnt" 2>/dev/null):$("$BB" awk '{print $22}' "$PROC/$pid/stat" 2>/dev/null)"
    done
    token="$token$pose"
    [ "$token" != "$BRIDGE_LAST_TOKEN" ] || return 0
    # Debounce PackageManager's atomic replace; never hash a changing APK.
    sleep 2
    confirm=$(pm path "$PACKAGE" 2>/dev/null | "$BB" sed -n 's/^package://p')
    [ "$confirm" = "$apk" ] || return 0
    confirm="$apk|$(bridge_ns 1 "$BB" stat -c '%d:%i:%s:%Y' "$apk")|$(bridge_ns 1 "$BB" stat -c '%d:%i:%Y' "$directory" 2>/dev/null)$pose"
    [ "$confirm" = "$token" ] || return 0
    BRIDGE_LAST_TOKEN=$token
    APK_HASH=$(bridge_ns_hash 1 "$apk")
    row=$("$BB" awk -F '|' -v checksum="$APK_HASH" '$2 == checksum {print}' "$MODDIR/profiles.tsv")
    [ "$(printf '%s\n' "$row" | "$BB" wc -l)" -eq 1 ] && [ -n "$row" ] || {
        bridge_cleanup; bridge_log 'Unsupported or absent signed workload; preserving package files.'; return 0;
    }
    IFS='|' read -r profile checksum factory native extra <<EOF
$row
EOF
    [ -z "$extra" ] && bridge_word "$profile" && bridge_hex "$checksum" || return 1
    if [ "$apk" != "$factory" ]; then
        case "$apk" in /data/app/*/"$PACKAGE"-*/base.apk|/data/app/"$PACKAGE"-*/base.apk) ;; *) return 1 ;; esac
        native="${apk%/*}/lib/arm64"
    fi
    bridge_path "$native" || return 1
    bridge_cleanup "$native" || return 1
    bridge_apply "$apk" "$profile" "$native" && bridge_log "$PACKAGE: verified private bridge ready" || {
        BRIDGE_LAST_TOKEN=; bridge_log "$PACKAGE: private namespace or native library preflight deferred"; return 1;
    }
}
bridge_lock() {
    local lock="$MODDIR/state/worker.lock" identity
    [ ! -L "$lock" ] || return 1
    if [ ! -e "$lock" ]; then (set -C; : > "$lock") || return 1; fi
    [ -f "$lock" ] && [ ! -L "$lock" ] && [ "$("$BB" stat -c '%h:%s:%u' "$lock")" = 1:0:0 ] || return 1
    identity=$("$BB" stat -c '%d:%i' "$lock") || return 1
    exec 9>>"$lock" || return 1
    [ "$("$BB" stat -c '%d:%i' "$lock")" = "$identity" ] &&
        [ "$("$BB" stat -c '%h:%s:%u' "$lock")" = 1:0:0 ] || return 1
    # flock is released by the kernel after exit/crash; no stale PID lease.
    "$BB" flock -n 9
}
bridge_service() {
    local count=0
    bridge_assets && bridge_lock || return 1
    while [ "$(getprop sys.boot_completed)" != 1 ]; do
        bridge_guard || return 0
        count=$((count+1)); [ "$count" -lt 300 ] || return 1; sleep 1
    done
    BRIDGE_LAST_TOKEN=
    while bridge_guard; do
        bridge_once || :
        [ "${HYPEROS_BRIDGE_ONCE:-0}" = 1 ] && return 0
        sleep "$BRIDGE_INTERVAL"
    done
    bridge_cleanup
}
bridge_uninstall() {
    bridge_assets && bridge_lock || return 1
    bridge_cleanup
}
