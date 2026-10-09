#!/system/bin/sh
[ "$ARCH" = arm64 ] || abort "This bridge requires an ARM64 guest."
set_perm_recursive "$MODPATH" 0 0 0755 0644
set_perm "$MODPATH/service.sh" 0 0 0755
set_perm "$MODPATH/uninstall.sh" 0 0 0755
set_perm "$MODPATH/runtime.sh" 0 0 0755
if [ -f "$MODPATH/catalog.sh" ]; then set_perm "$MODPATH/catalog.sh" 0 0 0755; fi
if [ -f "$MODPATH/post-fs-data.sh" ]; then set_perm "$MODPATH/post-fs-data.sh" 0 0 0755; fi
