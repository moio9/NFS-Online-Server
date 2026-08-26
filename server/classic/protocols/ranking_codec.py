"""Binary race-result codecs shared by the Classic lobby services.

Decoding result payloads is deliberately independent from lobby mutation.
Callers validate the returned record against session state and decide whether
to persist it; this module owns only wire parsing and field normalization.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
import math
import struct
from urllib.parse import unquote

from classic.ea.directory import GameSession
from classic.ea.ranking import MW_STAT_CATEGORY_COUNT, STAT_CATEGORY_COUNT


@dataclass(frozen=True)
class MWRankResult:
    """The reporter's validated row from MW's base64 ``RESU`` payload."""

    category: int
    reporter_index: int
    place: int
    flags: int
    elapsed: float
    nos_used: float
    participant_count: int
    record_gap: int = 0


@dataclass(frozen=True)
class U2RankResult:
    """The reporter's validated row from Underground 2's ``RESU`` payload."""

    category: int
    reporter_index: int
    place: int
    finish_mark: int
    disconnected: bool
    participant_count: int
    race_type: int
    track: int
    direction: int
    laps: int
    best_lap_ms: int
    best_drift: int


def result_category(fields: dict[str, str]) -> int:
    """Read the normalized ranking category from legacy fallback fields."""
    for key in ("CATEGORY", "CAT", "INDEX", "MODE", "TYPE"):
        if key not in fields:
            continue
        try:
            value = int(str(fields[key] or "0").strip(), 0)
        except (TypeError, ValueError):
            continue
        if 1 <= value <= STAT_CATEGORY_COUNT:
            return value - 1
        if 0 <= value < STAT_CATEGORY_COUNT:
            return value
    return 0


def result_outcome(fields: dict[str, str]) -> str:
    """Read the normalized result outcome from legacy fallback fields."""
    for key in ("OUTCOME", "RESULT", "STATUS"):
        value = str(fields.get(key, "") or "").strip().upper()
        if value in {"WIN", "WON", "1ST", "FIRST"}:
            return "WIN"
        if value in {"DISC", "DISCONNECT", "DNF", "DROPPED"}:
            return "DISCONNECT"
        if value in {"LOSS", "LOSE", "LOST", "0", "FINISH"}:
            return "LOSS"
    if str(fields.get("DISCONNECT", "") or "").strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return "DISCONNECT"
    if str(fields.get("WIN", "") or "").strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return "WIN"
    try:
        place = int(str(fields.get("PLACE", "") or "").strip())
    except (TypeError, ValueError):
        place = 0
    if place:
        return "WIN" if place == 1 else "LOSS"
    return ""


def decode_mw_rank_result(
    fields: dict[str, str],
    persona: str,
    game: GameSession | None = None,
) -> MWRankResult | None:
    """Decode the stock MW result report without guessing telemetry fields.

    Captures use an eight-byte header followed by one 51-byte record per
    participant. Records are not guaranteed to be in NAME<n> order, so byte
    zero of each record is the authoritative NAME index. Some stock reports
    append opaque bytes after the records, while others insert the same small
    extension between records.
    """

    encoded = unquote(str(fields.get("RESU", "") or "").strip())
    if not encoded:
        return None
    try:
        payload = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        return None
    if len(payload) < 8:
        return None
    version = payload[0]
    participant_count = payload[1]
    raw_category = payload[2]
    reporter_index = payload[3]
    if (
        version != 3
        or not 1 <= participant_count <= 8
        or reporter_index >= participant_count
    ):
        return None
    if game is not None and participant_count != len(game.participants):
        return None
    records_end = 8 + participant_count * 51
    if len(payload) < records_end:
        return None

    field_names = [
        str(fields.get(f"NAME{index}", "") or "").strip()
        for index in range(participant_count)
    ]
    normalized_field_names = [name.casefold() for name in field_names]
    if (
        any(not name for name in field_names)
        or len(set(normalized_field_names)) != participant_count
    ):
        return None
    if game is not None:
        expected_names = {
            str(game.participant_personas.get(user_id, "") or "")
            .strip()
            .casefold()
            for user_id in game.participants
        }
        if "" in expected_names or expected_names != set(normalized_field_names):
            return None

    reporter_name = field_names[reporter_index]
    reported_name = str(fields.get("REPT", "") or "").strip()
    expected_name = str(persona or "").strip()
    if not reporter_name:
        return None
    if reported_name and reporter_name.casefold() != reported_name.casefold():
        return None
    if expected_name and reporter_name.casefold() != expected_name.casefold():
        return None

    # Try the capture-default contiguous layout first. A live Sprint report
    # also demonstrated a two-byte extension between records. Accept a bounded
    # uniform gap only when every record resolves to a unique NAME index and a
    # valid finishing place, so opaque trailer bytes cannot shift a valid row.
    extra_bytes = len(payload) - records_end
    max_record_gap = (
        min(8, extra_bytes // (participant_count - 1))
        if participant_count > 1
        else 0
    )
    records: dict[int, tuple[int, int, float, float]] | None = None
    record_gap = 0
    for candidate_gap in range(max_record_gap + 1):
        candidate_records: dict[int, tuple[int, int, float, float]] = {}
        valid_layout = True
        for record_index in range(participant_count):
            offset = 8 + record_index * (51 + candidate_gap)
            record = payload[offset : offset + 51]
            if len(record) != 51:
                valid_layout = False
                break
            name_index = record[0]
            place = record[1]
            flags = record[2]
            if (
                name_index >= participant_count
                or name_index in candidate_records
                or not 1 <= place <= participant_count
            ):
                valid_layout = False
                break
            elapsed = struct.unpack(">f", record[8:12])[0]
            if not math.isfinite(elapsed) or elapsed < 0.0 or elapsed > 86400.0:
                elapsed = 0.0
            nos_used = struct.unpack(">f", record[44:48])[0]
            if (
                not math.isfinite(nos_used)
                or nos_used < 0.0
                or nos_used > 100000.0
            ):
                nos_used = 0.0
            candidate_records[name_index] = (
                place,
                flags,
                elapsed,
                nos_used,
            )
        if valid_layout and len(candidate_records) == participant_count:
            records = candidate_records
            record_gap = candidate_gap
            break

    if records is None:
        return None
    reporter = records.get(reporter_index)
    if reporter is None:
        return None
    place, flags, elapsed, nos_used = reporter
    if not 1 <= place <= participant_count:
        return None

    # RESU encodes MW race modes zero-based. Durable stats reserve slot zero
    # for the aggregate total, so concrete modes begin at slot one.
    category = (
        raw_category + 1
        if raw_category < MW_STAT_CATEGORY_COUNT - 1
        else 0
    )
    return MWRankResult(
        category=category,
        reporter_index=reporter_index,
        place=place,
        flags=flags,
        elapsed=elapsed,
        nos_used=nos_used,
        participant_count=participant_count,
        record_gap=record_gap,
    )


def u2_stat_category(race_type: int) -> int:
    """Map retail U2 race types to its six ranked statistic blocks."""
    normalized = int(race_type)
    return (
        normalized
        if 0 <= normalized < STAT_CATEGORY_COUNT
        else -1
    )


def decode_u2_rank_result(
    fields: dict[str, str],
    persona: str,
    game: GameSession | None = None,
) -> U2RankResult | None:
    """Decode retail U2 V3 reports and the older nfsuserver layout.

    Retail PC build 1.2 writes an eight-byte V3 header followed by one
    variable record per participant. Each record has a 55-byte base and a
    two-byte value for every bit set in its final 16-bit mask. The older
    nfsuserver implementation omitted the leading version byte and assumed
    fixed ``58 + 8 * laps`` records; retain that layout for compatibility with
    existing tools and fixtures.
    """

    encoded = unquote(str(fields.get("RESU", "") or "").strip())
    if not encoded:
        return None
    try:
        payload = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        return None
    if len(payload) < 7:
        return None

    wire_name_count = sum(
        1
        for index in range(6)
        if str(fields.get(f"NAME{index}", "") or "").strip()
    )
    compact_v3 = bool(
        len(payload) >= 8
        and payload[0] == 3
        and 1 <= payload[1] <= 6
        and payload[1] == wire_name_count
    )
    if compact_v3:
        participant_count = payload[1]
        race_type = payload[2]
        reporter_index = payload[3]
        track = int.from_bytes(payload[4:6], "big")
        direction = payload[6]
        laps = payload[7]
    else:
        participant_count = payload[0]
        race_type = payload[1]
        reporter_index = payload[2]
        track = int.from_bytes(payload[3:5], "big")
        direction = payload[5]
        laps = payload[6]
    if (
        not 1 <= participant_count <= (6 if compact_v3 else 4)
        or race_type > (6 if compact_v3 else 3)
        or reporter_index >= participant_count
        or not (0 if compact_v3 else 1) <= laps <= 32
        or track == 1099
    ):
        return None
    if game is not None and participant_count != len(game.participants):
        return None

    field_names = [
        str(fields.get(f"NAME{index}", "") or "").strip()
        for index in range(participant_count)
    ]
    normalized_names = [name.casefold() for name in field_names]
    if (
        any(not name for name in field_names)
        or len(set(normalized_names)) != participant_count
    ):
        return None
    if game is not None:
        expected_names = {
            str(game.participant_personas.get(user_id, "") or "")
            .strip()
            .casefold()
            for user_id in game.participants
        }
        if "" in expected_names or expected_names != set(normalized_names):
            return None

    reporter_name = field_names[reporter_index]
    reported_name = str(fields.get("REPT", "") or "").strip()
    expected_name = str(persona or "").strip()
    if reported_name and reporter_name.casefold() != reported_name.casefold():
        return None
    if expected_name and reporter_name.casefold() != expected_name.casefold():
        return None

    record: bytes | None = None
    seen_seeds: set[int] = set()
    if compact_v3:
        offset = 8
        for _record_index in range(participant_count):
            if len(payload) < offset + 55:
                return None
            base = payload[offset : offset + 55]
            flags = int.from_bytes(base[53:55], "little")
            record_size = 55 + 2 * flags.bit_count()
            if len(payload) < offset + record_size:
                return None
            candidate = payload[offset : offset + record_size]
            seed = candidate[0]
            if seed >= participant_count or seed in seen_seeds:
                return None
            seen_seeds.add(seed)
            if seed == reporter_index:
                record = candidate
            offset += record_size
        if offset != len(payload):
            return None
    else:
        block_size = 58 + 8 * laps
        required_size = 7 + participant_count * block_size
        if len(payload) < required_size:
            return None
        for record_index in range(participant_count):
            offset = 7 + record_index * block_size
            candidate = payload[offset : offset + block_size]
            seed = candidate[0]
            if seed >= participant_count or seed in seen_seeds:
                return None
            seen_seeds.add(seed)
            if seed == reporter_index:
                record = candidate
    if record is None:
        return None

    place = record[1] if compact_v3 else record[2]
    finish_mark = record[2] if compact_v3 else record[3]
    if not 1 <= place <= participant_count:
        return None
    best_lap = struct.unpack(">f", record[12:16])[0]
    if not math.isfinite(best_lap) or best_lap < 0.0 or best_lap > 86400.0:
        best_lap = 0.0
    if compact_v3:
        disconnected_value = struct.unpack(">f", record[44:48])[0]
        disconnected = (
            math.isfinite(disconnected_value) and disconnected_value == -1.0
        ) or finish_mark >= 9
        # V3 stores opaque counters after the ten floats. Captured Circuit
        # reports do not identify either as drift score.
        best_drift = 0
    else:
        disc_offset = 12 + laps * 4 + 24
        drift_offset = 12 + laps * 4 + 32
        disconnected_value = struct.unpack(
            ">f",
            record[disc_offset : disc_offset + 4],
        )[0]
        disconnected = (
            math.isfinite(disconnected_value) and disconnected_value == -1.0
        )
        best_drift = int.from_bytes(
            record[drift_offset : drift_offset + 4],
            "big",
            signed=True,
        )
    return U2RankResult(
        category=u2_stat_category(race_type),
        reporter_index=reporter_index,
        place=place,
        finish_mark=finish_mark,
        disconnected=disconnected,
        participant_count=participant_count,
        race_type=race_type,
        track=track,
        direction=direction,
        laps=laps,
        best_lap_ms=max(0, int(round(best_lap * 1000.0))),
        best_drift=max(0, best_drift),
    )


def mw_rank_payload_trace(fields: dict[str, str]) -> str:
    """Return bounded structural evidence for a rejected MW RESU report."""
    raw_encoded = str(fields.get("RESU", "") or "").strip()
    encoded = unquote(raw_encoded)
    try:
        payload = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        return f"decode_error={type(exc).__name__} resu={raw_encoded!r}"
    participant_count = payload[1] if len(payload) >= 2 else 0
    records_end = 8 + participant_count * 51
    record_heads = []
    for index, offset in enumerate(
        range(8, min(len(payload), records_end), 51)
    ):
        record_heads.append(f"{index}:{payload[offset:offset + 12].hex()}")
    trailer = payload[records_end:] if len(payload) >= records_end else b""
    return (
        f"decoded={len(payload)} header={payload[:8].hex()} "
        f"record_heads={','.join(record_heads) or '-'} "
        f"trailer={trailer.hex() or '-'} resu={raw_encoded!r}"
    )
