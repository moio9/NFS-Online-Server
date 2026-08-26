#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")"

PYTHON_BIN="${PYTHON_BIN:-python3}"
case "${1:-}" in
    u2|mw|carbon|all)
        games="$1"
        shift
        exec "$PYTHON_BIN" nfs_online.py start --games "$games" "$@"
        ;;
    *)
        exec "$PYTHON_BIN" nfs_online.py start "$@"
        ;;
esac
