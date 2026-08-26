#!/usr/bin/env python3
"""Reject private, generated, compiled, or non-English release content."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path, PurePosixPath

MAX_FILE_BYTES = 8 * 1024 * 1024

FORBIDDEN_DIRS = {
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tools",
    ".zig-cache",
    "zig-out",
}

FORBIDDEN_FILES = {
    "data/server-state.json",
    "data/carbon/auth.json",
    "data/carbon/social.json",
    "data/carbon/progression.json",
    "data/carbon/carbon_blobs.json",
    "data/carbon/carbon_race_results.jsonl",
    "data/carbon/dlc_assignments.json",
    "data/carbon/mad_impressions.jsonl",
}

FORBIDDEN_SUFFIXES = {
    ".asi",
    ".dll",
    ".exe",
    ".lib",
    ".obj",
    ".pdb",
    ".pyc",
    ".pyo",
    ".p12",
    ".pfx",
}

ARCHIVE_SUFFIXES = (".tar", ".tar.gz", ".tar.xz", ".tgz", ".txz", ".zip")
ALLOWED_DATA_PLACEHOLDERS = {
    "data/users/.gitkeep",
    "data/backups/.gitkeep",
    "data/classic/.gitkeep",
}

ROMANIAN = re.compile(
    r"[ăâîșşțţĂÂÎȘŞȚŢ]|"
    r"\b(?:pentru|trebuie|fisier(?:e)?|jucator(?:i)?|parola|porneste|"
    r"opreste|salveaza|configuratie|serverul|contul|cursa|dupa|inainte|"
    r"eroare|avertisment|gratuit(?:e)?)\b",
    re.IGNORECASE,
)

SECRET_PATTERNS = {
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"),
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    "GitHub fine-grained token": re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    "AWS access key": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "Slack token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    "Google API key": re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    "OpenAI-style key": re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b"),
}

REQUIRED_FILES = {
    "LICENSE": ("GNU AFFERO GENERAL PUBLIC LICENSE", "Version 3, 19 November 2007"),
    "LICENSE-NOTICE.md": ("AGPL-3.0-or-later", "LGPL-3.0-only", "NFSCarbonDLCUnlocker"),
    "LICENSES/GPL-3.0.txt": ("GNU GENERAL PUBLIC LICENSE",),
    "LICENSES/LGPL-3.0.txt": ("GNU LESSER GENERAL PUBLIC LICENSE",),
    "source/client-zig/LICENSES/AGPL-3.0.txt": ("GNU AFFERO GENERAL PUBLIC LICENSE",),
    "source/client-zig/LICENSES/GPL-3.0.txt": ("GNU GENERAL PUBLIC LICENSE",),
    "source/client-zig/LICENSES/LGPL-3.0.txt": ("GNU LESSER GENERAL PUBLIC LICENSE",),
    "source/client-zig/src/carbon/virus.zig": (
        "SPDX-License-Identifier: LGPL-3.0-only",
        "NFSCarbonDLCUnlocker",
    ),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "root",
        nargs="?",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    return parser.parse_args()


def runtime_reason(relative: str) -> str | None:
    path = PurePosixPath(relative)
    if relative in ALLOWED_DATA_PLACEHOLDERS:
        return None
    if relative.startswith(("logs/", "runtime/")):
        return "generated runtime file"
    if relative.startswith("data/users/"):
        return "player file"
    if relative.startswith("data/backups/"):
        return "backup file"
    if relative.startswith("data/classic/"):
        return "Classic persistent state"
    if relative == "data/accounts.sqlite3" or relative.startswith("data/accounts.sqlite3"):
        return "account database"
    if relative in FORBIDDEN_FILES:
        return "generated persistent state"
    if any(part in FORBIDDEN_DIRS for part in path.parts):
        return "build or cache file"
    if path.suffix.casefold() in FORBIDDEN_SUFFIXES:
        return "compiled or private artifact"
    if relative.endswith(ARCHIVE_SUFFIXES):
        return "nested archive"
    if ".pre-" in path.name or path.name.endswith((".rollback", ".bak", ".old")):
        return "backup or migration artifact"
    if path.name.startswith(".env") and path.name != ".env.example":
        return "environment secret file"
    if path.suffix.casefold() in {".pem", ".key"}:
        return "key material"
    return None


def text_or_none(data: bytes) -> str | None:
    if b"\x00" in data[:4096]:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def main() -> int:
    root = parse_args().root.resolve()
    if not (root / "nfs_online.py").is_file() or not (root / "config/server.toml").is_file():
        print(f"error: {root} is not the repository root", file=sys.stderr)
        return 2

    errors: list[str] = []
    files_checked = 0
    text_files_checked = 0
    total_bytes = 0

    for path in sorted(root.rglob("*")):
        relative_path = path.relative_to(root)
        if ".git" in relative_path.parts:
            continue
        relative = relative_path.as_posix()
        if path.is_symlink():
            errors.append(f"{relative}: symbolic links are not allowed")
            continue
        if not path.is_file():
            continue

        files_checked += 1
        size = path.stat().st_size
        total_bytes += size

        reason = runtime_reason(relative)
        if reason:
            errors.append(f"{relative}: {reason}")
            continue
        if size > MAX_FILE_BYTES:
            errors.append(f"{relative}: file exceeds {MAX_FILE_BYTES} bytes")
            continue

        text = text_or_none(path.read_bytes())
        if text is None:
            continue
        text_files_checked += 1

        if relative != "tools/check_public_tree.py":
            match = ROMANIAN.search(text)
            if match:
                line = text.count("\n", 0, match.start()) + 1
                errors.append(f"{relative}:{line}: Romanian text is not allowed")

        for label, pattern in SECRET_PATTERNS.items():
            match = pattern.search(text)
            if match:
                line = text.count("\n", 0, match.start()) + 1
                errors.append(f"{relative}:{line}: possible {label}")

    for relative in sorted(ALLOWED_DATA_PLACEHOLDERS):
        if not (root / relative).is_file():
            errors.append(f"{relative}: required placeholder is missing")

    for relative, markers in REQUIRED_FILES.items():
        path = root / relative
        if not path.is_file():
            errors.append(f"{relative}: required license or notice is missing")
            continue
        text = path.read_text(encoding="utf-8")
        for marker in markers:
            if marker not in text:
                errors.append(f"{relative}: missing marker: {marker}")

    if errors:
        print("Public-tree check: FAILED", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1

    print(
        "Public-tree check: OK "
        f"({files_checked} files, {text_files_checked} text files, {total_bytes} bytes)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
