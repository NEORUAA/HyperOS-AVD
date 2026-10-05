#!/bin/zsh
set -e
cd "${0:A:h}"
exec python3 scripts/watch5.py start
