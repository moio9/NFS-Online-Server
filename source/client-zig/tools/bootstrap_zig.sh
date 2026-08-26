#!/usr/bin/env bash
set -euo pipefail

version=0.16.0
sha256=70e49664a74374b48b51e6f3fdfbf437f6395d42509050588bd49abe52ba3d00
root="$(cd "$(dirname "$0")/.." && pwd)"
tools="$root/.tools"
archive="$tools/zig-$version.tar.xz"
dir="$tools/zig-x86_64-linux-$version"

mkdir -p "$tools"
if [[ ! -x "$dir/zig" ]]; then
  curl -L --fail --retry 3 \
    "https://ziglang.org/download/$version/zig-x86_64-linux-$version.tar.xz" \
    -o "$archive"
  printf '%s  %s\n' "$sha256" "$archive" | sha256sum --check --status
  tar -xf "$archive" -C "$tools"
fi

printf '%s\n' "$dir/zig"
