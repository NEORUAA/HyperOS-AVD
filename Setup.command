#!/bin/zsh
set -eu
ROOT="${0:A:h}"
exec python3 "$ROOT/scripts/setup.py" "$@"
