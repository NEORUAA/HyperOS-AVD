#!/system/bin/sh
MODDIR=${0%/*}
. "$MODDIR/runtime.sh"
if [ -f "$MODDIR/catalog.tsv" ]; then
    . "$MODDIR/catalog.sh"
    catalog_service
else bridge_service; fi
