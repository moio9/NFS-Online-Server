#!/usr/bin/env python3
"""Static endpoint/config audit for the three client profiles."""
from __future__ import annotations

import configparser
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = ROOT.parents[1]
errors: list[str] = []


def fail(message: str) -> None:
    errors.append(message)


def read_ini(name: str) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None)
    path = ROOT / "config" / name
    if not path.is_file():
        fail(f"missing {path.relative_to(ROOT)}")
        return parser
    parser.read(path, encoding="utf-8")
    return parser


def require_keys(parser: configparser.ConfigParser, filename: str, section: str, keys: set[str]) -> None:
    if not parser.has_section(section):
        fail(f"{filename}: missing [{section}]")
        return
    missing = sorted(keys.difference(parser[section].keys()))
    if missing:
        fail(f"{filename}: [{section}] missing {', '.join(missing)}")


common_network = {
    "enabled",
    "host",
    "bootstrap_port",
    "lobby_port",
    "control_port",
    "control_alias_port",
    "race_port",
}
u2_lan = {"enabled", "host", "port", "inject_server"}
mw_lan = {
    "host",
    "port",
    "inject_server",
    "server_name",
    "server_racers",
    "selected_host_hook",
    "refresh_state",
}

u2 = read_ini("net_u2.ini")
mw = read_ini("net_mw.ini")
carbon = read_ini("net_carbon.ini")

for packaged_name, published_name in (
    ("net_u2.ini", "underground2/net_u2.ini"),
    ("net_mw.ini", "most-wanted/net_mw.ini"),
    ("net_carbon.ini", "carbon/net_carbon.ini"),
):
    packaged = ROOT / "config" / packaged_name
    published = REPOSITORY_ROOT / "clients" / published_name
    if not published.is_file():
        fail(f"missing published client config: {published.relative_to(REPOSITORY_ROOT)}")
    elif packaged.read_bytes() != published.read_bytes():
        fail(
            f"published client config differs from build config: "
            f"{published.relative_to(REPOSITORY_ROOT)}"
        )

require_keys(u2, "net_u2.ini", "network", common_network)
require_keys(mw, "net_mw.ini", "network", common_network)
require_keys(u2, "net_u2.ini", "lan", u2_lan)
require_keys(mw, "net_mw.ini", "lan", mw_lan)
for filename, parser in (("net_u2.ini", u2), ("net_mw.ini", mw)):
    require_keys(parser, filename, "patches", {"enabled"})
    require_keys(parser, filename, "logging", {"enabled"})

require_keys(
    carbon,
    "net_carbon.ini",
    "network",
    {"enabled", "host", "plasma_host", "messenger_host", "messenger_port"},
)
require_keys(carbon, "net_carbon.ini", "mad", {"enabled", "host", "port", "force_all_ports"})
require_keys(carbon, "net_carbon.ini", "content", {"virus"})
require_keys(carbon, "net_carbon.ini", "logging", {"enabled"})

try:
    if int(mw["network"]["bootstrap_port"]) == int(mw["network"]["lobby_port"]):
        fail("net_mw.ini: bootstrap_port and lobby_port must be different")
except (KeyError, ValueError):
    pass

checks = {
    "src/u2/profile.zig": (
        "state.bootstrap",
        "state.lobby",
        "state.control",
        "state.control_alias",
        "state.race",
        "BOOTSTRAPHOST=",
        "LOBBYHOST=",
        "CONTROLHOST=",
        "CONTROLALIASHOST=",
        "UDPHOST=",
    ),
    "src/mw/profile.zig": (
        "state.bootstrap",
        "state.lobby",
        "state.control",
        "state.control_alias",
        "state.race",
        "BOOTSTRAPHOST=",
        "LOBBYHOST=",
        "CONTROLHOST=",
        "CONTROLALIASHOST=",
        "UDPHOST=",
    ),
    "src/mw/lan.zig": (
        "port == 30920",
        "state.bootstrap.port",
        "MW port route lobby={d} -> bootstrap={d}",
    ),
}

for relative, markers in checks.items():
    path = ROOT / relative
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    for marker in markers:
        if marker not in text:
            fail(f"{relative}: missing port-audit marker {marker!r}")

# Carbon intentionally has no classic bootstrap/lobby router.
carbon_state = (ROOT / "src/carbon/state.zig").read_text(encoding="utf-8")
if "bootstrap_port" in carbon_state or "lobby_port" in carbon_state:
    fail("Carbon profile unexpectedly contains classic bootstrap/lobby ports")

if errors:
    print("port audit: FAILED", file=sys.stderr)
    for error in errors:
        print(f"- {error}", file=sys.stderr)
    raise SystemExit(1)

print("port audit: OK")
print("- U2/MW common config structure verified")
print("- published and build-time client configs match")
print("- MW 30920 LAN selection -> 30921 bootstrap route verified")
print("- advertised bootstrap/lobby/control/alias/race parsing markers verified")
print("- Carbon kept on its protocol-specific FESL/Messenger/MAD configuration")
