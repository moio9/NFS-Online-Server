"""Need for Speed U2/MW reversible password-token codec.

The implementation mirrors the more complete Underground 2 server flow.  It
is intentionally kept at the wire adapter boundary: the common credential
store receives candidate plaintext values and never stores this reversible
``$hex`` representation.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping


DEFAULT_CLASSIC_MASKS: tuple[str, ...] = (
    "$b54ca8de40238572024704cc4de73590",
    "$5075626c6963204b6579",
    "Public Key",
    "517",
    "1773180069",
)


def _rol8(value: int, count: int) -> int:
    value &= 0xFF
    count &= 7
    return ((value << count) | (value >> (8 - count))) & 0xFF


def _ror8(value: int, count: int) -> int:
    value &= 0xFF
    count &= 7
    return ((value >> count) | (value << (8 - count))) & 0xFF


def make_password_token(password: str, mask: str) -> str:
    """Encode a password exactly as the U2 classic auth implementation does."""
    mask_bytes = str(mask or "0").encode("ascii", errors="ignore") or b"0"
    state = 0
    output = ["$"]
    for index, value in enumerate(str(password or "").encode("latin-1", errors="ignore")):
        state = _rol8(value ^ state, 3) ^ mask_bytes[index % len(mask_bytes)]
        output.append(f"{state & 0xFF:02x}")
    return "".join(output)


def decode_password_token(token: str, mask: str) -> str | None:
    """Reverse one U2/MW ``$hex`` password token for a known mask."""
    text = str(token or "").strip()
    if not text.startswith("$") or len(text) < 3 or (len(text) - 1) % 2:
        return None
    try:
        encoded_values = [
            int(text[index : index + 2], 16)
            for index in range(1, len(text), 2)
        ]
    except ValueError:
        return None
    mask_bytes = str(mask or "0").encode("ascii", errors="ignore") or b"0"
    state = 0
    output = bytearray()
    for index, encoded in enumerate(encoded_values):
        mask_byte = mask_bytes[index % len(mask_bytes)]
        value = _ror8(encoded ^ mask_byte, 3) ^ state
        output.append(value & 0xFF)
        state = encoded & 0xFF
    return output.decode("latin-1", errors="ignore")


def _field(fields: Mapping[str, object], name: str) -> str:
    wanted = name.casefold()
    for key, value in fields.items():
        if str(key or "").strip().casefold() == wanted:
            return str(value or "").strip()
    return ""


def mask_candidates(
    fields: Mapping[str, object],
    *,
    fixed_mask: str = "",
    classic_masks: Iterable[str] = DEFAULT_CLASSIC_MASKS,
) -> tuple[str, ...]:
    values: list[str] = []
    for key in ("MASK", "PSES", "SESS", "CHAL", "CHALLENGE", "SKEY", "LKEY"):
        value = _field(fields, key)
        if value and value not in values:
            values.append(value)
    fixed = str(fixed_mask or "").strip()
    if fixed and fixed not in values:
        values.append(fixed)
    for value in classic_masks:
        text = str(value or "").strip()
        if text and text not in values:
            values.append(text)
    return tuple(values)


def password_candidates(
    fields: Mapping[str, object],
    supplied: str,
    *,
    fixed_mask: str = "",
    classic_masks: Iterable[str] = DEFAULT_CLASSIC_MASKS,
) -> tuple[str, ...]:
    """Return the wire token, decoded plaintext and historical token variants."""
    classic_values = tuple(
        text
        for value in classic_masks
        if (text := str(value or "").strip())
    )
    candidates: list[str] = []
    wire_value = str(supplied or "")
    if wire_value:
        candidates.append(wire_value)
    for mask in mask_candidates(
        fields,
        fixed_mask=fixed_mask,
        classic_masks=classic_values,
    ):
        decoded = decode_password_token(wire_value, mask)
        if decoded and decoded not in candidates:
            candidates.append(decoded)
        if decoded:
            for classic_mask in classic_values:
                token = make_password_token(decoded, classic_mask)
                if token and token not in candidates:
                    candidates.append(token)
    return tuple(candidates)


def storage_password_candidate(candidates: Iterable[str]) -> str:
    """Prefer decoded printable plaintext when enrolling a classic account."""
    values = tuple(str(value or "") for value in candidates)
    for candidate in values[1:]:
        if candidate and not candidate.startswith("$") and all(
            32 <= ord(char) < 127 for char in candidate
        ):
            return candidate
    return values[0] if values else ""
