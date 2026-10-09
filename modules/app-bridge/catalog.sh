#!/system/bin/sh
# One scheduler, immutable content recipes and isolated ownership contexts.
catalog_assets() {
    local file parent
    OWNERDIR=$MODDIR
    [ -d "$OWNERDIR" ] && [ ! -L "$OWNERDIR" ] || return 1
    for parent in /data /data/adb /data/adb/modules /data/adb/modules_update; do
        [ ! -L "$parent" ] || return 1
    done
    for file in catalog.tsv manifest.json SHA256SUMS module.prop runtime.sh catalog.sh service.sh post-fs-data.sh uninstall.sh; do
        [ -f "$OWNERDIR/$file" ] && [ ! -L "$OWNERDIR/$file" ] &&
            [ "$("$BB" stat -c %h "$OWNERDIR/$file")" = 1 ] || return 1
    done
    while read -r checksum file; do
        case "$file" in /*|*'..'*|*'//'*) return 1 ;; esac
        [ -f "$OWNERDIR/$file" ] && [ ! -L "$OWNERDIR/$file" ] &&
            [ "$("$BB" stat -c %h "$OWNERDIR/$file")" = 1 ] || return 1
        parent=${file%/*}
        while [ "$parent" != "$file" ]; do
            [ -d "$OWNERDIR/$parent" ] && [ ! -L "$OWNERDIR/$parent" ] || return 1
            file=$parent; parent=${file%/*}
        done
    done < "$OWNERDIR/SHA256SUMS"
    (cd "$OWNERDIR" && "$BB" sha256sum -c SHA256SUMS >/dev/null 2>&1) || return 1
    [ ! -L "$OWNERDIR/state" ] || return 1
    mkdir -p "$OWNERDIR/state" && chmod 700 "$OWNERDIR/state" || return 1
    [ "$("$BB" stat -c %u "$OWNERDIR/state")" = 0 ] || return 1
    for file in "$OWNERDIR"/state/*; do
        [ -e "$file" ] || [ -L "$file" ] || continue
        bridge_state_file "$file" || return 1
    done
}
catalog_context_assets() {
    local file
    for file in profiles.tsv libraries.tsv systems.tsv callers.tsv embedded.tsv passthrough.tsv targets.tsv mirror.txt angle.tsv package.txt; do
        [ -f "$MODDIR/$file" ] && [ ! -L "$MODDIR/$file" ] &&
            [ "$("$BB" stat -c %h "$MODDIR/$file")" = 1 ] || return 1
    done
    for file in cache state; do [ ! -L "$MODDIR/$file" ] || return 1; done
    mkdir -p "$MODDIR/cache" "$MODDIR/state" && chmod 700 "$MODDIR/cache" "$MODDIR/state" || return 1
    for file in "$MODDIR"/state/*; do
        [ -e "$file" ] || [ -L "$file" ] || continue
        bridge_state_file "$file" || return 1
    done
    PACKAGE=$(cat "$MODDIR/package.txt")
    bridge_word "$PACKAGE"
}
catalog_choice() {
    local feature=$1 default=$2 path="${HYPEROS_APP_COMPAT_PREFS:-/data/adb/hyperos_app_compat/features}/$1" value
    local parent=${path%/*}
    while [ "$parent" != / ]; do
        [ ! -L "$parent" ] || return 1
        parent=${parent%/*}; [ -n "$parent" ] || parent=/
    done
    if [ -e "$path" ] || [ -L "$path" ]; then
        bridge_state_file "$path" || return 1
        value=$(cat "$path")
        case "$value" in on) return 0 ;; off) return 1 ;; auto) ;; *) return 1 ;; esac
    fi
    [ "$default" = 1 ]
}
catalog_early_target() {
    local target=$1 factory
    "$BB" awk -F '|' -v target="$target" '$1 == target {found=1} END {exit !found}' "$MODDIR/targets.tsv" && return 0
    [ "$(cat "$MODDIR/mirror.txt")" = 1 ] || return 1
    factory=$("$BB" cut -d '|' -f 3 "$MODDIR/profiles.tsv")
    [ "$target" = "${factory%/*}" ]
}
catalog_token() {
    local apk=$1 native=$2 pid selected path expected extra target before after flags token
    token="$apk|$(bridge_ns 1 "$BB" stat -c '%d:%i:%s:%Y' "$apk")|$(bridge_ns 1 "$BB" stat -c '%d:%i:%Y' "$native" 2>/dev/null)"
    while IFS='|' read -r selected path expected extra; do
        token="$token|$path:$(bridge_ns 1 "$BB" stat -c '%d:%i:%s:%Y' "$path" 2>/dev/null)"
    done < "$MODDIR/systems.tsv"
    while IFS='|' read -r selected path expected extra; do
        token="$token|$path:$(bridge_ns 1 "$BB" stat -c '%d:%i:%s:%Y' "$native/$path" 2>/dev/null)"
    done < "$MODDIR/callers.tsv"
    while IFS='|' read -r target before after flags; do
        token="$token|$target:$(bridge_ns 1 "$BB" stat -c '%d:%i:%s:%Y' "$target" 2>/dev/null)"
    done < "$MODDIR/targets.tsv"
    while IFS='|' read -r selected path before after flags extra; do
        token="$token|$path:$(bridge_ns 1 "$BB" stat -c '%d:%i:%s:%Y' "$native/$path" 2>/dev/null)"
    done < "$MODDIR/libraries.tsv"
    if [ "$(cat "$MODDIR/mirror.txt")" = 1 ] && [ "$apk" = "$("$BB" cut -d '|' -f 3 "$MODDIR/profiles.tsv")" ]; then
        # Cheap inventory notices new, removed or replaced factory assets without
        # repeatedly hashing unchanged APKs or precompiled application files.
        for pid in $(bridge_pids); do
            token="$token|tree:$pid:$(bridge_ns "$pid" sh -c 'cd "$1" && find . -mindepth 1 -exec "$2" stat -c "%n:%d:%i:%s:%Y:%a:%u:%g:%h" {} \;' sh "${apk%/*}" "$BB" | "$BB" sort)"
        done
    fi
    for pid in $(bridge_pids); do
        token="$token|$pid:$("$BB" stat -c %i "$PROC/$pid/ns/mnt" 2>/dev/null):$("$BB" awk '{print $22}' "$PROC/$pid/stat" 2>/dev/null)"
        token="$token|$("$BB" awk -v root="$MODDIR/cache/" 'index($4,root)==1 {printf "%s:%s:%s;",$4,$5,$6}' "$PROC/$pid/mountinfo" 2>/dev/null)"
    done
    printf %s "$token"
}
catalog_status() {
    printf '%s|%s|%s|%s\n' "$1" "$2" "$3" "$4" >> "$CATALOG_STATUS"
}
catalog_anchors() {
    local apk=$1 native=$2 pid selected path expected extra entry checksum
    for pid in $(bridge_pids); do
        [ -d "$PROC/$pid" ] || continue
        bridge_regular "$pid" "$apk" 0 &&
            [ "$(bridge_ns_hash "$pid" "$apk")" = "$APK_HASH" ] || return 1
        while IFS='|' read -r selected path expected extra; do
            [ -z "$extra" ] && bridge_regular "$pid" "$path" 0 &&
                [ "$(bridge_ns_hash "$pid" "$path")" = "$expected" ] || return 1
        done < "$MODDIR/systems.tsv"
        while IFS='|' read -r entry checksum extra; do
            [ -z "$extra" ] &&
                [ "$(bridge_ns "$pid" "$BB" unzip -p "$apk" "$entry" | "$BB" sha256sum | "$BB" cut -d ' ' -f 1)" = "$checksum" ] || return 1
        done < "$MODDIR/embedded.tsv"
        while IFS='|' read -r selected path expected extra; do
            [ -z "$extra" ] && bridge_regular "$pid" "$native/$path" 0 &&
                [ "$(bridge_ns_hash "$pid" "$native/$path")" = "$expected" ] || return 1
        done < "$MODDIR/callers.tsv"
    done
}
catalog_system_apply() {
    local pid target before after flags actual payload transaction="$MODDIR/state/transaction.$$"
    [ -s "$MODDIR/targets.tsv" ] || return 0
    for pid in $(bridge_pids); do
        [ -d "$PROC/$pid" ] || continue
        while IFS='|' read -r target before after flags; do
            bridge_regular "$pid" "$target" 0 || return 1
            actual=$(bridge_ns_hash "$pid" "$target")
            case "$actual" in "$before"|"$after") ;; *) return 1 ;; esac
            [ "$(bridge_hash "$OWNERDIR/objects/$after.so")" = "$after" ] || return 1
            # A running provider/HWL cannot consume a replaced ELF safely.
            if [ "$actual" != "$after" ] && bridge_loaded "$target"; then return 2; fi
            if bridge_is_mount "$pid" "$target" && ! bridge_owned_mount "$pid" "$target"; then return 1; fi
        done < "$MODDIR/targets.tsv"
    done
    [ ! -e "$transaction" ] && [ ! -L "$transaction" ] || return 1
    : > "$transaction" || return 1
    while IFS='|' read -r target before after flags; do
        payload=$(bridge_payload "$target" ignored "$after" 0) || { bridge_rollback "$transaction"; return 1; }
        for pid in $(bridge_pids); do
            [ -d "$PROC/$pid" ] || continue
            bridge_guard || { bridge_rollback "$transaction"; return 1; }
            actual=$(bridge_ns_hash "$pid" "$target")
            if [ "$actual" = "$after" ]; then
                if bridge_owned_mount "$pid" "$target"; then
                    bridge_guard && bridge_ns "$pid" "$BB" mount -o "remount,bind,$flags" "$target" ||
                        { bridge_rollback "$transaction"; return 1; }
                fi
                continue
            fi
            bridge_ns "$pid" "$BB" mount -o bind "$payload" "$target" || { bridge_rollback "$transaction"; return 1; }
            printf '%s|%s\n' "$pid" "$target" >> "$transaction"
            bridge_remember "$target" bind - || { bridge_rollback "$transaction"; return 1; }
            bridge_ns "$pid" "$BB" mount -o "remount,bind,$flags" "$target" &&
                [ "$(bridge_ns_hash "$pid" "$target")" = "$after" ] &&
                bridge_owned_mount "$pid" "$target" || { bridge_rollback "$transaction"; return 1; }
        done
    done < "$MODDIR/targets.tsv"
    rm -f "$transaction"
}
catalog_mirror_regular() {
    local pid=$1 target=$2 parent=$2
    case "$target" in "$MODDIR/cache/"*)
        case "$target" in *'/../'*|*'/./'*|*'//'*) return 1 ;; esac
        while [ "$parent" != "$MODDIR" ]; do
            bridge_ns "$pid" test ! -L "$parent" || return 1
            parent=${parent%/*}
            bridge_ns "$pid" test -d "$parent" || return 1
        done
        bridge_ns "$pid" test -f "$target" &&
            [ "$(bridge_ns "$pid" "$BB" stat -c %h "$target")" = 1 ] ;;
        *) bridge_regular "$pid" "$target" 0 ;;
    esac
}
catalog_mirror_inventory() {
    local pid=$1 parent=$2 apk_name=$3 item name
    for item in $(bridge_ns "$pid" sh -c 'cd "$1" && find . -mindepth 1' sh "$parent"); do
        case "$item" in
            "./$apk_name") catalog_mirror_regular "$pid" "$parent/$apk_name" || return 1 ;;
            ./lib|./lib/arm64)
                bridge_ns "$pid" test -d "$parent/${item#./}" &&
                    bridge_ns "$pid" test ! -L "$parent/${item#./}" || return 1 ;;
            ./lib/arm64/*)
                name=${item##*/}
                [ "$item" = "./lib/arm64/$name" ] &&
                    "$BB" awk -F '|' -v name="$name" '$2 == name {found=1} END {exit !found}' "$MODDIR/libraries.tsv" &&
                    catalog_mirror_regular "$pid" "$parent/${item#./}" || return 1 ;;
            ./oat|./oat/arm64)
                [ -s "$MODDIR/passthrough.tsv" ] &&
                    bridge_ns "$pid" test -d "$parent/${item#./}" &&
                    bridge_ns "$pid" test ! -L "$parent/${item#./}" || return 1 ;;
            ./oat/arm64/*)
                "$BB" awk -F '|' -v path="${item#./}" '$1 == path {found=1} END {exit !found}' "$MODDIR/passthrough.tsv" &&
                    catalog_mirror_regular "$pid" "$parent/${item#./}" || return 1 ;;
            *) return 1 ;;
        esac
    done
}
catalog_passthrough_verify() {
    local pid=$1 parent=$2 path checksum extra
    while IFS='|' read -r path checksum extra; do
        [ -z "$extra" ] && catalog_mirror_regular "$pid" "$parent/$path" &&
            [ "$(bridge_ns_hash "$pid" "$parent/$path")" = "$checksum" ] || return 1
    done < "$MODDIR/passthrough.tsv"
}
catalog_mirror_validate() {
    local apk=$1 native=$2 pid name before after placeholder legacy actual selected parent=${1%/*}
    for pid in $(bridge_pids); do
        [ -d "$PROC/$pid" ] || continue
        # The complete factory tree is an allowlist: signed APK, explicit
        # private libraries and content-pinned unchanged preopt files only.
        catalog_mirror_inventory "$pid" "$parent" "${apk##*/}" &&
            catalog_passthrough_verify "$pid" "$parent" || return 1
        while IFS='|' read -r selected name before after placeholder legacy; do
            if bridge_ns "$pid" test -e "$native/$name"; then
                bridge_regular "$pid" "$native/$name" 0 || return 1
                actual=$(bridge_ns_hash "$pid" "$native/$name")
                case "$actual" in "$before"|"$after") ;; *) return 1 ;; esac
            fi
        done < "$MODDIR/libraries.tsv"
    done
}
catalog_copy_metadata() {
    local source=$1 target=$2 owner mode context
    owner=$(bridge_ns 1 "$BB" stat -c %u:%g "$source")
    mode=$(bridge_ns 1 "$BB" stat -c %a "$source")
    context=$(bridge_context 1 "$source")
    case "$mode:$owner" in *[!0-9:]*) return 1 ;; esac
    [ -n "$mode" ] && [ -n "$owner" ] && [ -n "$context" ] &&
        chown "$owner" "$target" && chmod "$mode" "$target" && chcon "$context" "$target" &&
        [ "$("$BB" stat -c %u:%g "$target")" = "$owner" ] &&
        [ "$("$BB" stat -c %a "$target")" = "$mode" ] &&
        [ "$(bridge_context 1 "$target")" = "$context" ]
}
catalog_mirror_build() {
    local apk=$1 parent=$2 stage=$3 selected name before after placeholder legacy source path checksum extra owner context
    mkdir -p "$stage/lib/arm64" || return 1
    bridge_ns 1 cat "$apk" > "$stage/${apk##*/}" &&
        [ "$(bridge_hash "$stage/${apk##*/}")" = "$APK_HASH" ] &&
        catalog_copy_metadata "$apk" "$stage/${apk##*/}" || return 1
    owner=$(bridge_ns 1 "$BB" stat -c %u:%g "$apk")
    context=$(bridge_context 1 "$apk")
    while IFS='|' read -r selected name before after placeholder legacy; do
        source="$OWNERDIR/objects/$after.so"
        [ "$(bridge_hash "$source")" = "$after" ] &&
            cp "$source" "$stage/lib/arm64/$name" &&
            chown "$owner" "$stage/lib/arm64/$name" && chmod 644 "$stage/lib/arm64/$name" &&
            chcon "$context" "$stage/lib/arm64/$name" || return 1
    done < "$MODDIR/libraries.tsv"
    for source in "$stage" "$stage/lib" "$stage/lib/arm64"; do
        catalog_copy_metadata "$parent" "$source" || return 1
    done
    if [ -s "$MODDIR/passthrough.tsv" ]; then
        mkdir -p "$stage/oat/arm64" &&
            catalog_copy_metadata "$parent/oat" "$stage/oat" &&
            catalog_copy_metadata "$parent/oat/arm64" "$stage/oat/arm64" || return 1
    fi
    while IFS='|' read -r path checksum extra; do
        [ -z "$extra" ] && bridge_regular 1 "$parent/$path" 0 &&
            bridge_ns 1 cat "$parent/$path" > "$stage/$path" &&
            [ "$(bridge_hash "$stage/$path")" = "$checksum" ] &&
            catalog_copy_metadata "$parent/$path" "$stage/$path" || return 1
    done < "$MODDIR/passthrough.tsv"
}
catalog_mirror_apply() {
    local apk=$1 native=$2 mirror stage selected name before after placeholder legacy pid
    local transaction="$MODDIR/state/transaction.$$" parent=${1%/*} changed=0
    catalog_mirror_validate "$apk" "$native" || return 1
    mirror="$MODDIR/cache/mirror-$APK_HASH"
    if [ ! -e "$mirror" ] && [ ! -L "$mirror" ]; then
        stage=$("$BB" mktemp -d "$MODDIR/cache/mirror.XXXXXX") || return 1
        catalog_mirror_build "$apk" "$parent" "$stage" && mv "$stage" "$mirror" || {
            rm -rf "$stage"; return 1
        }
    fi
    [ -d "$mirror" ] && [ ! -L "$mirror" ] &&
        catalog_mirror_inventory 1 "$mirror" "${apk##*/}" &&
        catalog_passthrough_verify 1 "$mirror" &&
        [ -d "$mirror/lib" ] && [ ! -L "$mirror/lib" ] &&
        [ -d "$mirror/lib/arm64" ] && [ ! -L "$mirror/lib/arm64" ] &&
        catalog_mirror_regular 1 "$mirror/${apk##*/}" &&
        [ "$(bridge_hash "$mirror/${apk##*/}")" = "$APK_HASH" ] || return 1
    while IFS='|' read -r selected name before after placeholder legacy; do
        catalog_mirror_regular 1 "$mirror/lib/arm64/$name" &&
            [ "$(bridge_hash "$mirror/lib/arm64/$name")" = "$after" ] || return 1
    done < "$MODDIR/libraries.tsv"
    [ ! -e "$transaction" ] && [ ! -L "$transaction" ] || return 1
    : > "$transaction"
    for pid in $(bridge_pids); do
        [ -d "$PROC/$pid" ] || continue
        if bridge_owned_mount "$pid" "$parent"; then
            bridge_guard && bridge_ns "$pid" "$BB" mount -o remount,bind,ro,nosuid,nodev "$parent" ||
                { bridge_rollback "$transaction"; return 1; }
        else
            bridge_is_mount "$pid" "$parent" && { bridge_rollback "$transaction"; return 1; }
            bridge_guard || { bridge_rollback "$transaction"; return 1; }
            bridge_ns "$pid" "$BB" mount -o bind "$mirror" "$parent" || { bridge_rollback "$transaction"; return 1; }
            printf '%s|%s\n' "$pid" "$parent" >> "$transaction"
            bridge_remember "$parent" bind - || { bridge_rollback "$transaction"; return 1; }
            bridge_ns "$pid" "$BB" mount -o remount,bind,ro,nosuid,nodev "$parent" ||
                { bridge_rollback "$transaction"; return 1; }
            changed=1
        fi
        [ "$(bridge_ns_hash "$pid" "$apk")" = "$APK_HASH" ] &&
            bridge_owned_mount "$pid" "$parent" && catalog_passthrough_verify "$pid" "$parent" ||
            { bridge_rollback "$transaction"; return 1; }
        while IFS='|' read -r selected name before after placeholder legacy; do
            [ "$(bridge_ns_hash "$pid" "$native/$name")" = "$after" ] ||
                { bridge_rollback "$transaction"; return 1; }
        done < "$MODDIR/libraries.tsv"
    done
    rm -f "$transaction"
    if [ "$changed" = 1 ] && [ "${CATALOG_PHASE:-early}" = late ] && pidof "$PACKAGE" >/dev/null 2>&1; then
        bridge_guard && [ "$(pm path "$PACKAGE" | "$BB" sed -n 's/^package://p')" = "$apk" ] &&
            [ "$(bridge_ns_hash 1 "$apk")" = "$APK_HASH" ] && am force-stop "$PACKAGE" || return 1
    fi
}
catalog_settings() {
    local key value
    for key in angle_gl_driver_selection_pkgs angle_gl_driver_selection_values angle_egl_features; do
        value=$(settings get global "$key") || return 1
        [ "$value" != null ] || value=
        [ "$(printf %s "$value" | "$BB" tr -d 'A-Za-z0-9_.,-')" = '' ] || return 1
        printf '%s\n' "$value"
    done
}
catalog_policy_write() {
    local packages=$1 values=$2 features=$3 guard=${4:-1}
    [ "$guard" != 1 ] || bridge_guard || return 1
    settings put global angle_gl_driver_selection_pkgs "$packages" &&
        { [ "$guard" != 1 ] || bridge_guard; } &&
        settings put global angle_gl_driver_selection_values "$values" &&
        { [ "$guard" != 1 ] || bridge_guard; } &&
        settings put global angle_egl_features "$features" || return 1
    [ "$(catalog_settings)" = "$(printf '%s\n%s\n%s\n' "$packages" "$values" "$features")" ]
}
catalog_angle() {
    local driver features extra packages values old_features index desired_packages desired_values desired_features prior stage receipt="$MODDIR/state/angle-owned.tsv"
    [ -s "$MODDIR/angle.tsv" ] || return 0
    IFS='|' read -r driver features extra < "$MODDIR/angle.tsv"
    [ "$driver" = angle ] && [ -z "$extra" ] || return 1
    prior=$(catalog_settings) || return 1
    packages=$(printf '%s\n' "$prior" | "$BB" sed -n 1p)
    values=$(printf '%s\n' "$prior" | "$BB" sed -n 2p)
    old_features=$(printf '%s\n' "$prior" | "$BB" sed -n 3p)
    [ "$(printf %s "$packages" | "$BB" awk -F , '{print NF}')" = "$(printf %s "$values" | "$BB" awk -F , '{print NF}')" ] || return 1
    index=$(printf %s "$packages" | "$BB" awk -F , -v package="$PACKAGE" '{for(i=1;i<=NF;i++) if($i==package) {if(found) exit 1; found=i}} END {if(found) print found}') || return 1
    desired_packages=$packages; desired_values=$values
    if [ -z "$index" ]; then
        desired_packages="${packages:+$packages,}$PACKAGE"; desired_values="${values:+$values,}angle"
    else desired_values=$(printf %s "$values" | "$BB" awk -v position="$index" 'BEGIN{FS=OFS=","} {$position="angle"; print}') || return 1; fi
    desired_features=$old_features
    for extra in $(printf %s "$features" | "$BB" tr ',' ' '); do
        case ",$desired_features," in *",$extra,"*) ;; *) desired_features="${desired_features:+$desired_features,}$extra" ;; esac
    done
    if [ "$packages|$values|$old_features" = "$desired_packages|$desired_values|$desired_features" ]; then return 0; fi
    # Record the before/after triplets before the first write. Recovery never
    # restores a global setting if a different owner has since changed it.
    if [ -e "$receipt" ] || [ -L "$receipt" ]; then
        bridge_state_file "$receipt" || return 1
        catalog_angle_restore || return 1
        [ ! -e "$receipt" ] || return 1
        prior=$(catalog_settings) || return 1
        [ "$prior" = "$(printf '%s\n%s\n%s\n' "$packages" "$values" "$old_features")" ] || return 1
    fi
    stage=$("$BB" mktemp "$MODDIR/state/angle.XXXXXX") || return 1
    printf '%s|%s|%s\n%s|%s|%s\n' "$packages" "$values" "$old_features" "$desired_packages" "$desired_values" "$desired_features" > "$stage" &&
        mv "$stage" "$receipt" || return 1
    catalog_policy_write "$desired_packages" "$desired_values" "$desired_features" || {
        # The partial transaction is owned only while each field remains a
        # before/after value. Unknown external edits are preserved and deferred.
        catalog_angle_restore || :
        return 1
    }
}
catalog_angle_restore() {
    local receipt="$MODDIR/state/angle-owned.tsv" before_packages before_values before_features after_packages after_values after_features current packages values features extra
    [ -e "$receipt" ] || [ -L "$receipt" ] || return 0
    bridge_state_file "$receipt" && [ "$("$BB" wc -l < "$receipt")" -eq 2 ] || return 1
    IFS='|' read -r before_packages before_values before_features extra < "$receipt"
    [ -z "$extra" ] || return 1
    IFS='|' read -r after_packages after_values after_features extra <<EOF
$("$BB" tail -n 1 "$receipt")
EOF
    [ -z "$extra" ] || return 1
    for current in "$before_packages" "$before_values" "$before_features" "$after_packages" "$after_values" "$after_features"; do
        [ "$(printf %s "$current" | "$BB" tr -d 'A-Za-z0-9_.,-')" = '' ] || return 1
    done
    current=$(catalog_settings) || return 1
    packages=$(printf '%s\n' "$current" | "$BB" sed -n 1p)
    values=$(printf '%s\n' "$current" | "$BB" sed -n 2p)
    features=$(printf '%s\n' "$current" | "$BB" sed -n 3p)
    # Other catalog recipes or users may have appended unrelated packages.
    # Retire only this package's exact owned driver choice, preserving those
    # entries. Global capability flags are restored only when still exact.
    local index original replacement new_packages new_values new_features
    [ "$(printf %s "$packages" | "$BB" awk -F , '{print NF}')" = "$(printf %s "$values" | "$BB" awk -F , '{print NF}')" ] || return 1
    index=$(printf %s "$packages" | "$BB" awk -F , -v package="$PACKAGE" '{for(i=1;i<=NF;i++) if($i==package) {if(found) exit 1; found=i}} END {if(found) print found}') || return 1
    original=$(printf %s "$before_packages" | "$BB" awk -F , -v package="$PACKAGE" '{for(i=1;i<=NF;i++) if($i==package) print i}')
    replacement=-
    [ -z "$original" ] || replacement=$(printf %s "$before_values" | "$BB" cut -d , -f "$original")
    if [ -n "$index" ]; then
        current=$(printf %s "$values" | "$BB" cut -d , -f "$index")
        [ "$current" = angle ] || [ "$current" = "$replacement" ] || return 1
    elif [ "$replacement" != '-' ]; then return 1; fi
    new_packages=$packages; new_values=$values; new_features=$features
    if [ -n "$index" ]; then
        if [ "$replacement" = '-' ]; then
            new_packages=$(printf %s "$packages" | "$BB" awk -F , -v position="$index" '{for(i=1;i<=NF;i++) if(i!=position) {printf "%s%s",sep,$i; sep=","}}') || return 1
            new_values=$(printf %s "$values" | "$BB" awk -F , -v position="$index" '{for(i=1;i<=NF;i++) if(i!=position) {printf "%s%s",sep,$i; sep=","}}') || return 1
        else new_values=$(printf %s "$values" | "$BB" awk -v position="$index" -v value="$replacement" 'BEGIN{FS=OFS=","} {$position=value; print}') || return 1; fi
    fi
    [ "$features" != "$after_features" ] || new_features=$before_features
    catalog_policy_write "$new_packages" "$new_values" "$new_features" 0 && rm -f "$receipt"
}
catalog_cleanup_context() {
    BRIDGE_FEATURE=; BRIDGE_KEEP_EARLY=0; BRIDGE_CATALOG=1
    bridge_assets && bridge_recover && bridge_cleanup && catalog_angle_restore || return 1
    rm -f "$MODDIR/state/reconciled-token" "$MODDIR/state/last-token"
}
catalog_cleanup_feature() {
    local feature=$1 id selected package default result=0
    while IFS='|' read -r id selected package default; do
        [ "$selected" = "$feature" ] || continue
        MODDIR="$OWNERDIR/contexts/$id"
        catalog_cleanup_context || result=1
    done < "$OWNERDIR/catalog.tsv"
    [ "$result" = 0 ]
}
catalog_select() {
    local feature=$1 phase=$2 package=$3 id selected default profile expected factory native paths apk token cached selection cache stage
    cache="$OWNERDIR/state/select-$phase-$feature"
    if [ "$phase" = late ]; then
        paths=$(pm path "$package" 2>/dev/null | "$BB" sed -n 's/^package://p')
        [ -n "$paths" ] && [ "$(printf '%s\n' "$paths" | "$BB" wc -l)" -eq 1 ] || return 1
        apk=$paths; bridge_regular 1 "$apk" 0 || return 1
        token="$apk|$(bridge_ns 1 "$BB" stat -c '%d:%i:%s:%Y' "$apk")"
        if [ -e "$cache" ]; then
            bridge_state_file "$cache" || return 1
            cached=$("$BB" head -n 1 "$cache")
            if [ "$cached" = "$token" ]; then
                selection=$("$BB" tail -n 1 "$cache"); [ "$selection" != '-' ] || return 1
                bridge_word "$selection" &&
                    "$BB" awk -F '|' -v id="$selection" -v feature="$feature" -v package="$package" '$1 == id && $2 == feature && $3 == package {found=1} END {exit !found}' "$OWNERDIR/catalog.tsv" || return 1
                printf '%s\n' "$selection"; return 0
            fi
        fi
        expected=$(bridge_ns_hash 1 "$apk")
    fi
    selection=-
    while IFS='|' read -r id selected package default; do
        [ "$selected" = "$feature" ] || continue
        IFS='|' read -r profile checksum factory native < "$OWNERDIR/contexts/$id/profiles.tsv"
        if [ "$phase" = early ]; then
            [ -s "$OWNERDIR/contexts/$id/targets.tsv" ] || [ "$(cat "$OWNERDIR/contexts/$id/mirror.txt")" = 1 ] || continue
            [ "$factory" != '-' ] && bridge_regular 1 "$factory" 0 || continue
            [ "$(bridge_ns_hash 1 "$factory")" = "$checksum" ] || continue
        else [ "$expected" = "$checksum" ] || continue; fi
        [ "$selection" = '-' ] || return 1
        selection=$id
    done < "$OWNERDIR/catalog.tsv"
    if [ "$phase" = late ]; then
        stage=$("$BB" mktemp "$OWNERDIR/state/selection.XXXXXX") || return 1
        printf '%s\n%s\n' "$token" "$selection" > "$stage" && mv "$stage" "$cache" || return 1
    fi
    [ "$selection" != '-' ] || return 1
    printf '%s\n' "$selection"
}
catalog_pass() {
    local phase=$1 feature package id default selected profile checksum factory native apk result token previous stage failed
    CATALOG_PHASE=$phase
    CATALOG_STATUS=$("$BB" mktemp "$OWNERDIR/state/status.XXXXXX") || return 1
    for feature in $("$BB" cut -d '|' -f 2 "$OWNERDIR/catalog.tsv" | "$BB" sort -u); do
        BRIDGE_FEATURE=; BRIDGE_KEEP_EARLY=0
        package=$("$BB" awk -F '|' -v feature="$feature" '$2 == feature {print $3; exit}' "$OWNERDIR/catalog.tsv")
        id=$(catalog_select "$feature" "$phase" "$package") || {
            if catalog_cleanup_feature "$feature"; then
                catalog_status "$feature" "$package" unsupported 'No audited installed workload matched'
            else catalog_status "$feature" "$package" pending 'Unsupported workload; owned mappings await safe release'; fi
            continue
        }
        default=$("$BB" awk -F '|' -v id="$id" '$1 == id {print $4}' "$OWNERDIR/catalog.tsv")
        if ! catalog_choice "$feature" "$default"; then
            if catalog_cleanup_feature "$feature"; then
                catalog_status "$feature" "$package" disabled 'Local choice or audited recipe default'
            else catalog_status "$feature" "$package" pending 'Disabled; owned mappings await safe release'; fi
            continue
        fi
        failed=0
        while IFS='|' read -r selected profile package default; do
            [ "$profile" = "$feature" ] && [ "$selected" != "$id" ] || continue
            MODDIR="$OWNERDIR/contexts/$selected"
            catalog_cleanup_context || failed=1
        done < "$OWNERDIR/catalog.tsv"
        if [ "$failed" = 1 ]; then
            catalog_status "$feature" "$package" pending 'Previous recipe retains loaded owned mappings'; continue
        fi
        MODDIR="$OWNERDIR/contexts/$id"; BRIDGE_CATALOG=1; BRIDGE_FEATURE=$feature
        BRIDGE_DEFAULT=$("$BB" awk -F '|' -v id="$id" '$1 == id {print $4}' "$OWNERDIR/catalog.tsv")
        BRIDGE_KEEP_EARLY=1
        bridge_assets && bridge_recover || { catalog_status "$feature" "$PACKAGE" pending 'Owned rollback awaiting safe release'; continue; }
        IFS='|' read -r profile APK_HASH factory native < "$MODDIR/profiles.tsv"
        apk=$factory
        if [ "$phase" = late ]; then
            apk=$(pm path "$PACKAGE" | "$BB" sed -n 's/^package://p')
            [ "$apk" = "$factory" ] || native="${apk%/*}/lib/arm64"
        fi
        token=$(catalog_token "$apk" "$native")
        previous=
        if [ -f "$MODDIR/state/reconciled-token" ]; then
            bridge_state_file "$MODDIR/state/reconciled-token" || continue
            previous=$(cat "$MODDIR/state/reconciled-token")
        fi
        if [ "$token" != "$previous" ]; then
            # PM installation is atomic; debounce the selected inode before
            # hashing it or extracting embedded ABI anchors.
            if [ "$phase" = late ]; then sleep 2; fi
            [ "$(catalog_token "$apk" "$native")" = "$token" ] || {
                catalog_status "$feature" "$PACKAGE" pending 'PackageManager transaction in progress'; continue
            }
            catalog_anchors "$apk" "$native" || {
                catalog_cleanup_context || :
                catalog_status "$feature" "$PACKAGE" unsupported 'Pinned native or system ABI differs'; continue
            }
            if [ "$(cat "$MODDIR/mirror.txt")" = 1 ] && [ "$apk" = "$factory" ]; then
                catalog_mirror_validate "$apk" "$native" || {
                    catalog_status "$feature" "$PACKAGE" unsupported 'Factory layout contains unaudited assets'; continue
                }
            fi
            catalog_system_apply; result=$?
            if [ "$result" = 2 ]; then catalog_status "$feature" "$PACKAGE" reboot-required 'A loaded provider needs normal cold boot'; continue; fi
            [ "$result" = 0 ] || { catalog_status "$feature" "$PACKAGE" pending 'System namespace overlay deferred'; continue; }
            if [ "$(cat "$MODDIR/mirror.txt")" = 1 ] && [ "$apk" = "$factory" ]; then
                catalog_mirror_apply "$apk" "$native" || { catalog_status "$feature" "$PACKAGE" pending 'Factory overlay safely deferred'; continue; }
            elif [ "$phase" = late ]; then
                BRIDGE_LAST_TOKEN=
                bridge_once || { catalog_status "$feature" "$PACKAGE" pending 'Private application preflight deferred'; continue; }
            fi
            stage=$("$BB" mktemp "$MODDIR/state/reconciled.XXXXXX") || continue
            catalog_token "$apk" "$native" > "$stage" && mv "$stage" "$MODDIR/state/reconciled-token" || continue
        fi
        if [ "$phase" = late ]; then
            catalog_angle || { catalog_status "$feature" "$PACKAGE" pending 'ANGLE policy transaction deferred'; continue; }
        fi
        catalog_status "$feature" "$PACKAGE" verified 'Exact content and owned overlays verified; not a loaded-state claim'
    done
    BRIDGE_FEATURE=; BRIDGE_KEEP_EARLY=0; BRIDGE_CATALOG=0; MODDIR=$OWNERDIR
    mv "$CATALOG_STATUS" "$OWNERDIR/state/status.tsv"
}
catalog_early() {
    catalog_assets && bridge_lock && bridge_guard || return 1
    catalog_pass early
}
catalog_service() {
    local count=0
    catalog_assets && bridge_lock || return 1
    while [ "$(getprop sys.boot_completed)" != 1 ]; do
        bridge_guard || return 0
        count=$((count+1)); [ "$count" -lt 300 ] || return 1; sleep 1
    done
    while bridge_guard; do
        catalog_pass late || :
        [ "${HYPEROS_BRIDGE_ONCE:-0}" = 1 ] && return 0
        sleep "$BRIDGE_INTERVAL"
    done
    catalog_uninstall_contexts
}
catalog_uninstall_contexts() {
    local feature
    for feature in $("$BB" cut -d '|' -f 2 "$OWNERDIR/catalog.tsv" | "$BB" sort -u); do catalog_cleanup_feature "$feature" || :; done
}
catalog_uninstall() {
    catalog_assets && bridge_lock || return 1
    catalog_uninstall_contexts
}
