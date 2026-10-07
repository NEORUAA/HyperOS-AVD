#!/bin/zsh
set -eu
PROJECT_ROOT="${0:A:h}"
export HYPEROS_AVD_WORKSPACE="$PROJECT_ROOT/work/os4-pad"
if [[ ! -f "$HYPEROS_AVD_WORKSPACE/local/build.json" ]]; then
    print -u2 'Build the tablet candidate first: python3 scripts/build_os4_pad.py'
    exit 1
fi
exec python3 "$PROJECT_ROOT/scripts/launch.py" "$@"
