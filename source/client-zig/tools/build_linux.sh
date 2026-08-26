#!/usr/bin/env bash
set -euo pipefail

required_version=0.16.0
root="$(cd "$(dirname "$0")/.." && pwd)"
zig="${ZIG:-}"

if [[ -z "$zig" ]] && command -v zig >/dev/null 2>&1; then
  candidate="$(command -v zig)"
  if [[ "$($candidate version)" == "$required_version" ]]; then
    zig="$candidate"
  fi
fi

if [[ -z "$zig" ]]; then
  zig="$($root/tools/bootstrap_zig.sh)"
fi

actual_version="$($zig version)"
if [[ "$actual_version" != "$required_version" ]]; then
  echo "This source tree requires Zig $required_version (found $actual_version at $zig)." >&2
  echo "Unset ZIG or point it to a Zig $required_version executable." >&2
  exit 1
fi

cd "$root"
rm -rf zig-out
python3 tools/check_source.py
python3 tools/check_ports.py
"$zig" fmt build.zig src >/dev/null
"$zig" build -Doptimize=ReleaseSmall

# Audit fresh compiler outputs before copying the matching configs beside them.
# A failed audit leaves zig-out/bin without packaged configs.
python3 tools/audit_pe.py \
  zig-out/bin/net_u2.asi \
  zig-out/bin/net_mw.asi \
  zig-out/bin/net_carbon.asi

cp config/net_u2.ini zig-out/bin/
cp config/net_mw.ini zig-out/bin/
cp config/net_carbon.ini zig-out/bin/

file zig-out/bin/*.asi
