#!/usr/bin/env python3
"""Structural checks that do not replace `zig fmt` or `zig build`."""
from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
ERRORS: list[str] = []


def fail(message: str) -> None:
    ERRORS.append(message)


def strip_literals_and_comments(text: str) -> str:
    out: list[str] = []
    i = 0
    state = "code"
    while i < len(text):
        ch = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if state == "code":
            if ch == '/' and nxt == '/':
                state = "line_comment"
                out.extend("  ")
                i += 2
                continue
            if ch == '/' and nxt == '*':
                state = "block_comment"
                out.extend("  ")
                i += 2
                continue
            if ch == '"':
                state = "string"
                out.append(' ')
                i += 1
                continue
            if ch == "'":
                state = "char"
                out.append(' ')
                i += 1
                continue
            out.append(ch)
            i += 1
            continue
        if state == "line_comment":
            if ch == '\n':
                state = "code"
                out.append('\n')
            else:
                out.append(' ')
            i += 1
            continue
        if state == "block_comment":
            if ch == '*' and nxt == '/':
                state = "code"
                out.extend("  ")
                i += 2
            else:
                out.append('\n' if ch == '\n' else ' ')
                i += 1
            continue
        if state in {"string", "char"}:
            quote = '"' if state == "string" else "'"
            if ch == '\\':
                out.append(' ')
                if i + 1 < len(text):
                    out.append('\n' if text[i + 1] == '\n' else ' ')
                i += 2
                continue
            if ch == quote:
                state = "code"
            out.append('\n' if ch == '\n' else ' ')
            i += 1
    if state in {"string", "char", "block_comment"}:
        fail(f"unterminated {state}")
    return ''.join(out)


def check_balance(path: pathlib.Path, text: str) -> None:
    clean = strip_literals_and_comments(text)
    pairs = {')': '(', ']': '[', '}': '{'}
    stack: list[tuple[str, int]] = []
    for index, ch in enumerate(clean):
        if ch in "([{":
            stack.append((ch, index))
        elif ch in pairs:
            if not stack or stack[-1][0] != pairs[ch]:
                fail(f"{path.relative_to(ROOT)}: unmatched {ch} at byte {index}")
                return
            stack.pop()
    if stack:
        fail(f"{path.relative_to(ROOT)}: unclosed {stack[-1][0]} at byte {stack[-1][1]}")


def check_imports(path: pathlib.Path, text: str) -> None:
    for match in re.finditer(r'@import\("([^"\n]+)"\)', text):
        target = match.group(1)
        if target in {"std", "shared"}:
            continue
        imported = (path.parent / target).resolve()
        try:
            imported.relative_to(ROOT.resolve())
        except ValueError:
            fail(f"{path.relative_to(ROOT)} imports outside project: {target}")
            continue
        if not imported.is_file():
            fail(f"{path.relative_to(ROOT)} missing import: {target}")


def require(path: str, terms: tuple[str, ...]) -> None:
    file = ROOT / path
    if not file.is_file():
        fail(f"missing required file: {path}")
        return
    text = file.read_text(encoding="utf-8")
    for term in terms:
        if term not in text:
            fail(f"{path} missing required marker: {term}")


zig_files = sorted(SRC.rglob("*.zig"))
if len(zig_files) < 20:
    fail(f"expected at least 20 Zig source files, found {len(zig_files)}")

for path in zig_files + [ROOT / "build.zig"]:
    text = path.read_text(encoding="utf-8")
    check_balance(path, text)
    check_imports(path, text)

foreign_sources = [
    path for path in ROOT.rglob("*")
    if path.is_file() and path.suffix.lower() in {".c", ".cc", ".cpp", ".cxx", ".rs"}
]
if foreign_sources:
    fail("non-Zig implementation files found: " + ", ".join(str(p.relative_to(ROOT)) for p in foreign_sources))

require("build.zig", ("net_u2", "net_mw", "net_carbon", ".cpu_arch = .x86", ".os_tag = .windows"))
require("src/shared/socket_runtime.zig", ("hookIat", "myGetProcAddress", "WSASendTo", "WSARecvFrom"))
require("src/shared/process_guard.zig", ("CreateMutexA", "GetLastError", "error_already_exists"))
require("src/u2/profile.zig", ("sendUdp", "recvUdp", "BOOTSTRAPHOST=", "CONTROLHOST=", "isLanTcpPeer"))
require("src/u2/lan.zig", ("seedProvider", "handleLan", "0x000f3040", "callconv(win.THISCALL)", "U2 LAN hook entered"))
require("src/mw/profile.zig", ("beforeUdpSend", "looksLikeLanDiscovery", "BOOTSTRAPHOST=", "CONTROLHOST=", "isLanTcpPeer"))
require("src/mw/lan.zig", ("hookParse", "hookHostGetter", "hookPortGetter", "30920", "bootstrap.port", "parser-only LAN mode"))
require("src/carbon/online.zig", ("rva_conn_manager", "hookStuffOverrides", "xttps"))
require("src/carbon/mad.zig", ("hookResolve", "hookSessionKey", "session_key"))
require("src/carbon/virus.zig", ("hookVirus", "Carbon Virus vinyl hook installed"))


# All imported Windows functions must name their source DLL. Without this,
# Zig x86 may emit a direct CALL to an IAT data slot.
win_text = (ROOT / "src/shared/win.zig").read_text(encoding="utf-8")
if re.search(r"pub\s+extern\s+fn\s+", win_text):
    fail("src/shared/win.zig contains an extern function without a DLL namespace")
if 'extern "kernel32" fn' not in win_text or 'extern "ws2_32" fn' not in win_text:
    fail("src/shared/win.zig is missing namespaced Windows imports")
if not (ROOT / "tools/audit_pe.py").is_file():
    fail("missing tools/audit_pe.py")

# InitializeASI must not call InterlockedCompareExchange. Zig 0.16 x86 can
# lower that imported intrinsic into a direct CALL to the IAT data slot.
for main_file in ("src/u2/main.zig", "src/mw/main.zig", "src/carbon/main.zig"):
    main_text = (ROOT / main_file).read_text(encoding="utf-8")
    if "InterlockedCompareExchange" in main_text:
        fail(f"{main_file} must not use InterlockedCompareExchange")
    if "var started = false;" not in main_text:
        fail(f"{main_file} is missing the plain InitializeASI start guard")
    if "process_guard.acquire" not in main_text:
        fail(f"{main_file} is missing the process-wide duplicate-load guard")

for config in ("net_u2.ini", "net_mw.ini", "net_carbon.ini"):
    if not (ROOT / "config" / config).is_file():
        fail(f"missing config/{config}")

if ERRORS:
    print("source check: FAILED", file=sys.stderr)
    for error in ERRORS:
        print(f"- {error}", file=sys.stderr)
    raise SystemExit(1)

print(f"source check: OK ({len(zig_files)} Zig files)")
print("note: this validates structure only; run Zig 0.16.0 for syntax/codegen validation")
