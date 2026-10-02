#!/bin/zsh
set -eu
PROJECT_ROOT="${0:A:h}"
export HYPEROS_AVD_WORKSPACE="$PROJECT_ROOT/work/os4-official"
if [[ ! -f "$HYPEROS_AVD_WORKSPACE/local/runtime.json" ]]; then
    print -u2 'Install the official OS4 Release first: ./Setup.command --bundle /path/to/manifest.json'
    exit 1
fi
exec python3 "$PROJECT_ROOT/scripts/launch.py" "$@"
