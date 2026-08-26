#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")"
exec "${PYTHON_BIN:-python3}" nfs_online.py stop
