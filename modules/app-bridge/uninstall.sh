#!/system/bin/sh
MODDIR=${0%/*}
. "$MODDIR/runtime.sh"
if [ -f "$MODDIR/catalog.tsv" ]; then
    . "$MODDIR/catalog.sh"
    catalog_uninstall
else bridge_uninstall; fi
