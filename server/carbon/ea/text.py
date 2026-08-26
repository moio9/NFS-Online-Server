"""Shared EA-style key/value text codec.

This covers only the common textual grammar used by the current Carbon, Most
Wanted and Underground 2 servers.  Game adapters own their command names and
their record fields.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any


_FIELD = re.compile(r'(\w+)=("(?:[^"\\]|\\.)*"|\S+)')


def encode_message(tag: str, fields: Mapping[str, Any] | None = None) -> str:
    """Serialize a single EA text command with a newline terminator."""
    name = str(tag or "").strip().upper()
    if not name or any(char.isspace() for char in name):
        raise ValueError(f"invalid EA message tag: {tag!r}")
    values: list[str] = []
    for key, value in (fields or {}).items():
        field_name = str(key or "").strip().upper()
        if not field_name or not field_name.replace("_", "").isalnum():
            raise ValueError(f"invalid EA field name: {key!r}")
        if isinstance(value, float):
            text = f"{value:.6f}"
        else:
            text = str(value)
        if any(char.isspace() for char in text):
            text = '"' + text.replace('"', '\\"') + '"'
        values.append(f"{field_name}={text}")
    return f"+{name} {' '.join(values)}\n" if values else f"+{name}\n"


def parse_message(raw: str) -> tuple[str, str, dict[str, int | float | str]]:
    """Parse sign, command tag and typed fields from an EA text line."""
    text = str(raw or "").strip()
    if not text:
        return "", "", {}
    sign = ""
    if text[0] in "+-":
        sign, text = text[0], text[1:]
    head, _, tail = text.partition(" ")
    fields: dict[str, int | float | str] = {}
    for match in _FIELD.finditer(tail):
        key = match.group(1).upper()
        value = match.group(2).strip('"')
        try:
            fields[key] = int(value)
        except ValueError:
            try:
                fields[key] = float(value)
            except ValueError:
                fields[key] = value
    return sign, head.upper(), fields
