"""Carbon post-race OLMSG codecs and authoritative result arbitration.

Reverse-engineered from the PC rebroadcaster and the symbol-rich PS3 build.
The wire layout is compact (not the padded 388-byte C structure): one common
header followed by eight fixed racer slots. Ranked progression prefers the
server's finish order. Race types that omit LeaderFinished may fall back to a
complete finishing order unanimously reported by every authenticated player.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
import math
import struct
from typing import Iterable, Mapping, Sequence

from carbon.gamemanager.protocol import LOGICAL_PREFIX, OLMessageType


MAX_RACERS = 8
LEADER_FINISHED = int(OLMessageType.LEADER_FINISHED)
RACER_FINISHED = int(OLMessageType.RACER_FINISHED)
GAME_RESULTS = int(OLMessageType.GAME_RESULTS)
FINAL_GAME_RESULTS = int(OLMessageType.FINAL_GAME_RESULTS)

_PLAYER_SLOT_WIRE_SIZE = struct.calcsize(">BBBfi9f")
_PLAYER_RESULTS_WIRE_SIZE = 10 + MAX_RACERS * _PLAYER_SLOT_WIRE_SIZE
_GAME_RESULTS_WIRE_SIZE = len(LOGICAL_PREFIX) + 1 + _PLAYER_RESULTS_WIRE_SIZE
_FINAL_DATE_WIRE_SIZE = 10
_FINAL_RESULTS_WIRE_SIZE = _GAME_RESULTS_WIRE_SIZE + _FINAL_DATE_WIRE_SIZE


class ResultCodecError(ValueError):
    """Raised for malformed or semantically impossible result packets."""


@dataclass(frozen=True)
class FinishSignal:
    server_car_id: int
    finish_reason: int = 0

    @classmethod
    def decode(cls, logical: bytes, *, expected_type: int | None = None) -> "FinishSignal":
        raw = bytes(logical)
        if len(raw) < 13 or raw[:4] != LOGICAL_PREFIX:
            raise ResultCodecError("finish signal is truncated or lacks OLMSG prefix")
        kind = raw[4]
        if expected_type is not None and kind != int(expected_type):
            raise ResultCodecError(
                f"unexpected finish signal type 0x{kind:02x}; expected 0x{int(expected_type):02x}"
            )
        if kind not in (LEADER_FINISHED, RACER_FINISHED):
            raise ResultCodecError(f"not a finish signal: 0x{kind:02x}")
        # Capture/decompilation order is FinishReason followed by ServerCarId.
        return cls(
            server_car_id=int.from_bytes(raw[9:13], "big", signed=True),
            finish_reason=int.from_bytes(raw[5:9], "big", signed=True),
        )

    def encode(self, message_type: int = LEADER_FINISHED) -> bytes:
        kind = int(message_type)
        if kind not in (LEADER_FINISHED, RACER_FINISHED):
            raise ResultCodecError(f"invalid finish message type: 0x{kind:02x}")
        return (
            LOGICAL_PREFIX
            + bytes((kind,))
            + int(self.finish_reason).to_bytes(4, "big", signed=True)
            + int(self.server_car_id).to_bytes(4, "big", signed=True)
        )


@dataclass(frozen=True)
class RacerResult:
    lost_connection: bool = False
    ranking: int = 0
    finish_reason: int = 0
    canyon_points: float = 0.0
    server_car_id: int = 0
    laps_completed: float = 0.0
    race_time: float = 0.0
    best_lap_time: float = 0.0
    average_speed: float = 0.0
    top_speed: float = 0.0
    lbs_nos_used: float = 0.0
    delta_xp: float = 0.0
    custom: float = 0.0
    custom2: float = 0.0

    def clean(self) -> "RacerResult":
        ranking = int(self.ranking)
        if not 0 <= ranking <= MAX_RACERS:
            raise ResultCodecError(f"invalid racer ranking: {ranking}")
        return replace(
            self,
            lost_connection=bool(self.lost_connection),
            ranking=ranking,
            finish_reason=int(self.finish_reason) & 0xFF,
            server_car_id=int(self.server_car_id),
            canyon_points=_finite(self.canyon_points),
            laps_completed=_finite(self.laps_completed),
            race_time=_finite(self.race_time),
            best_lap_time=_finite(self.best_lap_time),
            average_speed=_finite(self.average_speed),
            top_speed=_finite(self.top_speed),
            lbs_nos_used=_finite(self.lbs_nos_used),
            delta_xp=_finite(self.delta_xp),
            custom=_finite(self.custom),
            custom2=_finite(self.custom2),
        )


_EMPTY_RACER = RacerResult()


@dataclass(frozen=True)
class PlayerGameResults:
    number_of_racers: int = 0
    race_mode: int = 0
    track_number: int = 0
    track_direction: int = 0
    number_of_laps: int = 0
    reporting_server_car_id: int = 0
    racers: tuple[RacerResult, ...] = (_EMPTY_RACER,) * MAX_RACERS

    def normalized(self) -> "PlayerGameResults":
        count = int(self.number_of_racers)
        if not 0 <= count <= MAX_RACERS:
            raise ResultCodecError(f"invalid number of racers: {count}")
        racers = tuple(item.clean() for item in self.racers[:MAX_RACERS])
        racers += (_EMPTY_RACER,) * (MAX_RACERS - len(racers))
        return replace(
            self,
            number_of_racers=count,
            race_mode=int(self.race_mode) & 0xFF,
            track_number=int(self.track_number) & 0xFFFF,
            track_direction=int(self.track_direction) & 0xFF,
            number_of_laps=int(self.number_of_laps) & 0xFF,
            reporting_server_car_id=int(self.reporting_server_car_id),
            racers=racers,
        )

    def row_for_car(self, server_car_id: int) -> RacerResult | None:
        target = int(server_car_id)
        for row in self.racers:
            if row.server_car_id == target:
                return row
        return None

    def encode(self, message_type: int = GAME_RESULTS, *, when: datetime | None = None) -> bytes:
        kind = int(message_type)
        if kind not in (GAME_RESULTS, FINAL_GAME_RESULTS):
            raise ResultCodecError(f"invalid result message type: 0x{kind:02x}")
        result = self.normalized()
        body = bytearray(LOGICAL_PREFIX + bytes((kind,)))
        body.extend(
            struct.pack(
                ">BBHBBi",
                result.number_of_racers,
                result.race_mode,
                result.track_number,
                result.track_direction,
                result.number_of_laps,
                result.reporting_server_car_id,
            )
        )
        for row in result.racers:
            row = row.clean()
            body.extend(
                struct.pack(
                    ">BBBfi9f",
                    int(row.lost_connection),
                    row.ranking,
                    row.finish_reason,
                    row.canyon_points,
                    row.server_car_id,
                    row.laps_completed,
                    row.race_time,
                    row.best_lap_time,
                    row.average_speed,
                    row.top_speed,
                    row.lbs_nos_used,
                    row.delta_xp,
                    row.custom,
                    row.custom2,
                )
            )
        if kind == FINAL_GAME_RESULTS:
            body.extend(_encode_final_datetime(when or datetime.now()))
        return bytes(body)

    @classmethod
    def decode(cls, logical: bytes) -> "DecodedGameResults":
        raw = bytes(logical)
        if len(raw) < 5 or raw[:4] != LOGICAL_PREFIX:
            raise ResultCodecError("result message lacks OLMSG prefix")
        kind = raw[4]
        minimum = _FINAL_RESULTS_WIRE_SIZE if kind == FINAL_GAME_RESULTS else _GAME_RESULTS_WIRE_SIZE
        if kind not in (GAME_RESULTS, FINAL_GAME_RESULTS):
            raise ResultCodecError(f"not a result message: 0x{kind:02x}")
        if len(raw) < minimum:
            raise ResultCodecError(
                f"result message 0x{kind:02x} truncated: {len(raw)} < {minimum}"
            )
        offset = 5
        number, mode, track, direction, laps, reporting = struct.unpack_from(">BBHBBi", raw, offset)
        offset += 10
        rows: list[RacerResult] = []
        for _index in range(MAX_RACERS):
            values = struct.unpack_from(">BBBfi9f", raw, offset)
            offset += _PLAYER_SLOT_WIRE_SIZE
            rows.append(
                RacerResult(
                    lost_connection=bool(values[0]),
                    ranking=values[1],
                    finish_reason=values[2],
                    canyon_points=values[3],
                    server_car_id=values[4],
                    laps_completed=values[5],
                    race_time=values[6],
                    best_lap_time=values[7],
                    average_speed=values[8],
                    top_speed=values[9],
                    lbs_nos_used=values[10],
                    delta_xp=values[11],
                    custom=values[12],
                    custom2=values[13],
                ).clean()
            )
        timestamp = _decode_final_datetime(raw[offset : offset + _FINAL_DATE_WIRE_SIZE]) if kind == FINAL_GAME_RESULTS else None
        return DecodedGameResults(
            message_type=kind,
            results=cls(number, mode, track, direction, laps, reporting, tuple(rows)).normalized(),
            timestamp=timestamp,
            trailing=raw[minimum:],
        )


@dataclass(frozen=True)
class DecodedGameResults:
    message_type: int
    results: PlayerGameResults
    timestamp: "ResultTimestamp | None" = None
    trailing: bytes = b""


@dataclass(frozen=True)
class ResultTimestamp:
    year_day: int
    hour: int
    daylight_saving: int
    month_day: int
    minute: int
    month: int
    second: int
    week_day: int
    year_since_1900: int


@dataclass(frozen=True)
class AuthoritativePlacement:
    profile_id: int
    player_id: int
    server_car_id: int
    ranking: int
    finish_reason: int
    finished: bool
    lost_connection: bool = False


@dataclass(frozen=True)
class FinalizedRace:
    results: PlayerGameResults
    placements: tuple[AuthoritativePlacement, ...]
    winner_profile_ids: tuple[int, ...]
    ranked: bool


@dataclass
class RaceResultTracker:
    """Room-scoped evidence collector with server-owned finish order."""

    ranked: bool = False
    car_by_profile: dict[int, int] = field(default_factory=dict)
    profile_by_car: dict[int, int] = field(default_factory=dict)
    finish_signals: dict[int, FinishSignal] = field(default_factory=dict)
    finish_order: list[int] = field(default_factory=list)
    reports: dict[int, PlayerGameResults] = field(default_factory=dict)
    final_sent: bool = False

    def bind_car(self, profile_id: int, server_car_id: int) -> bool:
        profile = int(profile_id)
        car = int(server_car_id)
        if car <= 0:
            return False
        previous_car = self.car_by_profile.get(profile)
        previous_profile = self.profile_by_car.get(car)
        if previous_car not in (None, car) or previous_profile not in (None, profile):
            return False
        self.car_by_profile[profile] = car
        self.profile_by_car[car] = profile
        return True

    def record_finish(self, profile_id: int, signal: FinishSignal) -> bool:
        if not self.bind_car(profile_id, signal.server_car_id):
            return False
        if signal.server_car_id in self.finish_signals:
            return False
        self.finish_signals[signal.server_car_id] = signal
        self.finish_order.append(signal.server_car_id)
        return True

    def record_report(self, profile_id: int, report: PlayerGameResults) -> bool:
        normalized = report.normalized()
        profile = int(profile_id)
        car = int(normalized.reporting_server_car_id)
        if car > 0 and not self.bind_car(profile, car):
            return False
        previous = self.reports.get(profile)
        self.reports[profile] = normalized
        return previous != normalized

    def ranked_report_order(
        self,
        profile_ids: Iterable[int],
    ) -> tuple[int, ...] | None:
        """Return a unanimous authenticated human-car order, if available.

        Some ranked race types publish complete GameResults but no
        LeaderFinished messages. One report is not authority: every
        participant must report, every report must contain every bound human
        car, and all reports must agree on non-zero, unique rankings. Any
        observed LeaderFinished prefix must also agree with that order.
        """

        expected = tuple(dict.fromkeys(int(item) for item in profile_ids))
        if not expected or not set(expected).issubset(self.car_by_profile):
            return None
        if not set(expected).issubset(self.reports):
            return None

        ranks_by_car: dict[int, int] = {}
        for profile in expected:
            car = self.car_by_profile[profile]
            reported_ranks: set[int] = set()
            for reporter in expected:
                row = self.reports[reporter].row_for_car(car)
                if row is None or row.lost_connection or row.ranking <= 0:
                    return None
                reported_ranks.add(int(row.ranking))
            if len(reported_ranks) != 1:
                return None
            ranks_by_car[car] = reported_ranks.pop()

        if len(set(ranks_by_car.values())) != len(ranks_by_car):
            return None
        order = tuple(
            car
            for car, _rank in sorted(
                ranks_by_car.items(),
                key=lambda item: (item[1], item[0]),
            )
        )
        observed = tuple(
            car
            for car in self.finish_order
            if car in ranks_by_car
        )
        if order[: len(observed)] != observed:
            return None
        return order

    def is_complete(self, profile_ids: Iterable[int]) -> bool:
        expected = {int(item) for item in profile_ids}
        if not expected or not expected.issubset(self.car_by_profile):
            return False
        if self.ranked:
            has_native_finish_order = all(
                self.car_by_profile[profile] in self.finish_signals
                for profile in expected
            )
            return (
                has_native_finish_order
                or self.ranked_report_order(expected) is not None
            )
        # Retail Challenge clients can publish RacerFinished for AI cars and
        # then both complete GameResults without every human publishing a
        # LeaderFinished. For unranked rooms, two authenticated self reports
        # are the completion quorum.
        return expected.issubset(self.reports)

    def finalize(
        self,
        participants: Sequence[tuple[int, int]],
        *,
        race_mode_fallback: int = 0,
    ) -> FinalizedRace:
        """Build one authoritative result from finish order plus self metrics.

        ``participants`` contains ``(profile_id, player_id)``. A player's own
        result report may supply time/speed fields. Ranking and winner
        selection prefer native server-observed finish order, with unanimous
        authenticated reports as the ranked fallback.
        """
        if self.final_sent:
            raise ResultCodecError("race results were already finalized")
        ordered_participants = [(int(profile), int(player)) for profile, player in participants]
        if not ordered_participants:
            raise ResultCodecError("cannot finalize a race without participants")

        # Prefer the report with the fullest roster. Challenge reports include
        # coordinator-owned AI rows which must survive FinalGameResults.
        header = max(
            self.reports.values(),
            key=lambda item: item.number_of_racers,
            default=PlayerGameResults(race_mode=race_mode_fallback),
        )
        participant_profiles = tuple(
            profile for profile, _player in ordered_participants
        )
        native_finish_complete = self.ranked and all(
            self.car_by_profile.get(profile) in self.finish_signals
            for profile in participant_profiles
        )
        ranked_report_order = (
            self.ranked_report_order(participant_profiles)
            if self.ranked and not native_finish_complete
            else None
        )
        effective_finish_order = (
            tuple(self.finish_order)
            if native_finish_complete or not self.ranked
            else (ranked_report_order or tuple(self.finish_order))
        )
        order_index = {
            car: index + 1
            for index, car in enumerate(effective_finish_order)
        }
        placements: list[AuthoritativePlacement] = []
        rows: list[RacerResult] = []

        for profile_id, player_id in ordered_participants:
            car = self.car_by_profile.get(profile_id, player_id)
            signal = self.finish_signals.get(car)
            metric = _self_report_metric(self.reports.get(profile_id), car)
            reported_rank = _consensus_ranking(self.reports.values(), car)
            rank = order_index.get(
                car,
                0 if self.ranked else (reported_rank or metric.ranking),
            )
            lost = _consensus_lost_connection(self.reports.values(), car)
            finished = (
                signal is not None and rank > 0
                if self.ranked and ranked_report_order is None
                else rank > 0 and not lost
            )
            finish_reason = signal.finish_reason if signal is not None else _consensus_finish_reason(self.reports.values(), car)
            lost = not finished or lost
            row = replace(
                metric,
                lost_connection=lost,
                ranking=rank,
                finish_reason=finish_reason & 0xFF,
                server_car_id=car,
            ).clean()
            rows.append(row)
            placements.append(
                AuthoritativePlacement(
                    profile_id=profile_id,
                    player_id=player_id,
                    server_car_id=car,
                    ranking=rank,
                    finish_reason=finish_reason,
                    finished=finished,
                    lost_connection=lost,
                )
            )

        winners = tuple(
            placement.profile_id for placement in placements if placement.ranking == 1
        )
        winner_cars = [
            placement.server_car_id
            for placement in placements
            if placement.ranking == 1
        ]
        if self.ranked:
            # DNF/unresolved racers remain after all finishers, in
            # deterministic player-id order. Their zero ranking is preserved.
            rows.extend((_EMPTY_RACER,) * (MAX_RACERS - len(rows)))
            results = PlayerGameResults(
                number_of_racers=len(ordered_participants),
                race_mode=header.race_mode or int(race_mode_fallback),
                track_number=header.track_number,
                track_direction=header.track_direction,
                number_of_laps=header.number_of_laps,
                reporting_server_car_id=(
                    effective_finish_order[0] if effective_finish_order else 0
                ),
                racers=tuple(rows),
            ).normalized()
        else:
            # The authenticated reports already agree on the shared race-car
            # namespace (human plus AI). Preserve that full roster instead of
            # collapsing FinalGameResults to GameManager participants only.
            results = replace(
                header,
                reporting_server_car_id=(
                    winner_cars[0]
                    if winner_cars
                    else header.reporting_server_car_id
                ),
            ).normalized()
        self.final_sent = True
        return FinalizedRace(results, tuple(placements), winners, bool(self.ranked))


def _self_report_metric(report: PlayerGameResults | None, car: int) -> RacerResult:
    if report is None:
        return _EMPTY_RACER
    row = report.row_for_car(car)
    return row if row is not None else _EMPTY_RACER


def _consensus_finish_reason(reports: Iterable[PlayerGameResults], car: int) -> int:
    values = {
        row.finish_reason
        for report in reports
        if (row := report.row_for_car(car)) is not None
    }
    return next(iter(values)) if len(values) == 1 else 0


def _consensus_ranking(reports: Iterable[PlayerGameResults], car: int) -> int:
    values = {
        row.ranking
        for report in reports
        if (row := report.row_for_car(car)) is not None and row.ranking > 0
    }
    return next(iter(values)) if len(values) == 1 else 0


def _consensus_lost_connection(reports: Iterable[PlayerGameResults], car: int) -> bool:
    values = {
        row.lost_connection
        for report in reports
        if (row := report.row_for_car(car)) is not None
    }
    return next(iter(values)) if len(values) == 1 else False


def _finite(value: float) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ResultCodecError(f"non-finite result value: {value!r}")
    return parsed


def _encode_final_datetime(value: datetime) -> bytes:
    timetuple = value.timetuple()
    # Exact order observed in OLMSG_FinalGameResults::Unpack. C's tm_mon and
    # tm_year are zero-/1900-based; tm_wday is Sunday=0.
    return struct.pack(
        ">H8B",
        max(0, timetuple.tm_yday - 1) & 0xFFFF,
        timetuple.tm_hour & 0xFF,
        1 if timetuple.tm_isdst > 0 else 0,
        timetuple.tm_mday & 0xFF,
        timetuple.tm_min & 0xFF,
        (timetuple.tm_mon - 1) & 0xFF,
        timetuple.tm_sec & 0xFF,
        (timetuple.tm_wday + 1) % 7,
        (timetuple.tm_year - 1900) & 0xFF,
    )


def _decode_final_datetime(raw: bytes) -> ResultTimestamp:
    if len(raw) < _FINAL_DATE_WIRE_SIZE:
        raise ResultCodecError("final result timestamp is truncated")
    values = struct.unpack_from(">H8B", raw)
    return ResultTimestamp(*values)


__all__ = [
    "AuthoritativePlacement",
    "DecodedGameResults",
    "FINAL_GAME_RESULTS",
    "FinalizedRace",
    "FinishSignal",
    "GAME_RESULTS",
    "LEADER_FINISHED",
    "MAX_RACERS",
    "PlayerGameResults",
    "RACER_FINISHED",
    "RaceResultTracker",
    "RacerResult",
    "ResultCodecError",
    "ResultTimestamp",
]
