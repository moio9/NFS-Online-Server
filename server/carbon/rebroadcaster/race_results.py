"""Authoritative Carbon race-result orchestration, independent of UDP wire I/O.

The GameManager result message names and numeric types come from the existing
capture/decomp-backed codec.  This module decides result authority, progression
and journaling, then returns opaque logical bodies for the rebroadcaster to
publish with destination-local CommUDP state.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
import logging
import time

from carbon.gamemanager.race_state import GameRaceState, RacePhase
from carbon.gamemanager.results import (
    FINAL_GAME_RESULTS,
    LEADER_FINISHED,
    RACER_FINISHED,
    FinishSignal,
    AuthoritativePlacement,
    PlayerGameResults,
    RaceResultTracker,
    ResultCodecError,
)
from carbon.progression import (
    CarbonProgressionStore,
    RaceAwards,
    RankedRaceProgression,
)
from carbon.rebroadcaster.state import Address
from carbon.theater.directory import CarbonGame, CarbonTicketResolution


@dataclass(frozen=True)
class AcceptedLeaderFinished:
    gid: str
    ranked: bool
    profile_id: int
    player_id: int
    server_car_id: int
    finish_reason: int
    rank: int


@dataclass(frozen=True)
class RelayedRacerFinished:
    gid: str
    source: Address
    player_id: int
    server_car_id: int
    finish_reason: int
    endpoint_count: int


@dataclass(frozen=True)
class FinalResultCommit:
    game: CarbonGame
    ranked: bool
    body: bytes
    authoritative: PlayerGameResults
    placements: tuple[AuthoritativePlacement, ...]
    progression: RankedRaceProgression
    winner_profile_ids: tuple[int, ...]
    result_authority: str


@dataclass(frozen=True)
class ResultOutcome:
    publications: tuple[bytes, ...] = ()
    final: FinalResultCommit | None = None
    accepted_leader: AcceptedLeaderFinished | None = None
    relayed_racer: RelayedRacerFinished | None = None


class RaceResultCoordinator:
    """Own room result trackers and return logical publications to the wire."""

    def __init__(
        self,
        progression: CarbonProgressionStore,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self.progression = progression
        self.log = logger or logging.getLogger(__name__)
        self.trackers: dict[str, RaceResultTracker] = {}

    def discard(self, gid: str) -> bool:
        return self.trackers.pop(str(gid), None) is not None

    def award_race(
        self,
        game: CarbonGame,
        *,
        event_type: int,
        winner_profile_ids: set[int] | tuple[int, ...] | list[int],
    ) -> RaceAwards:
        participants = tuple(
            participant.identity for participant in game.participants.values()
        )
        awards = self.progression.award_race(
            participants,
            event_type=int(event_type),
            winners=winner_profile_ids,
            ranked=game.is_ranked,
        )
        self.log.info(
            "Carbon progression race awards: gid=%s event=%d virus=%s "
            "viral_recipients=%s carbon_plague=%s beat_moderator=%s",
            game.gid,
            int(event_type),
            awards.viral_stat or "none",
            ",".join(map(str, awards.viral_recipients)) or "none",
            ",".join(map(str, awards.carbon_plague_recipients)) or "none",
            ",".join(map(str, awards.beat_moderator_recipients)) or "none",
        )
        return awards

    def tracker(self, game: CarbonGame) -> RaceResultTracker:
        tracker = self.trackers.get(game.gid)
        if tracker is None:
            tracker = RaceResultTracker(ranked=game.is_ranked)
            self.trackers[game.gid] = tracker
        else:
            tracker.ranked = bool(game.is_ranked)
        # The race-car namespace is independent of GameManager player ids.
        # Live Challenge reports use ids such as 100/110 while bound player
        # ids can be 17954/3305. Learn only the authenticated sender's car.
        return tracker

    def handle_leader_finished(
        self,
        source: Address,
        binding: CarbonTicketResolution,
        race: GameRaceState,
        logical: bytes,
    ) -> ResultOutcome:
        if race.phase != RacePhase.RACING:
            self.log.info(
                "Carbon GM LeaderFinished ignored outside race: "
                "gid=%s phase=%s src=%s:%d",
                binding.game.gid,
                race.phase.name,
                source[0],
                source[1],
            )
            return ResultOutcome()
        try:
            signal = FinishSignal.decode(logical, expected_type=LEADER_FINISHED)
        except ResultCodecError as exc:
            self.log.warning(
                "Carbon GM malformed LeaderFinished: gid=%s src=%s:%d "
                "error=%s body=%s",
                binding.game.gid,
                source[0],
                source[1],
                exc,
                bytes(logical[:64]).hex(),
            )
            return ResultOutcome()

        tracker = self.tracker(binding.game)
        profile_id = binding.participant.identity.profile_id
        expected_car = tracker.car_by_profile.get(profile_id)
        if expected_car is None:
            if not tracker.bind_car(profile_id, signal.server_car_id):
                self.log.warning(
                    "Carbon GM LeaderFinished car binding rejected: "
                    "gid=%s pid=%d profile=%d car=%d",
                    binding.game.gid,
                    binding.participant.player_id,
                    profile_id,
                    signal.server_car_id,
                )
                return ResultOutcome()
            expected_car = signal.server_car_id
        if signal.server_car_id != expected_car:
            self.log.warning(
                "Carbon GM rejected LeaderFinished car mismatch: "
                "gid=%s pid=%d profile=%d raw_car=%d bound_car=%d reason=%d",
                binding.game.gid,
                binding.participant.player_id,
                profile_id,
                signal.server_car_id,
                expected_car,
                signal.finish_reason,
            )
            return ResultOutcome()
        if not tracker.record_finish(profile_id, signal):
            self.log.warning(
                "Carbon GM rejected LeaderFinished ownership/duplicate: "
                "gid=%s pid=%d profile=%d car=%d expected=%s reason=%d",
                binding.game.gid,
                binding.participant.player_id,
                profile_id,
                signal.server_car_id,
                expected_car,
                signal.finish_reason,
            )
            return ResultOutcome()

        # fullchalangerace frame 13521: retail republishes 0x0D to both
        # destinations and normalizes the client reason to zero.
        leader_publication = FinishSignal(
            signal.server_car_id,
            0,
        ).encode(LEADER_FINISHED)
        # Finalization intentionally remains a separate service step. Retail's
        # observable order queues LeaderFinished for every destination before
        # progression, FinalGameResults encoding and final publication begin.
        return ResultOutcome(
            publications=(leader_publication,),
            accepted_leader=AcceptedLeaderFinished(
                gid=binding.game.gid,
                ranked=bool(binding.game.is_ranked),
                profile_id=profile_id,
                player_id=binding.participant.player_id,
                server_car_id=signal.server_car_id,
                finish_reason=signal.finish_reason,
                rank=len(tracker.finish_order),
            ),
        )

    def handle_client_racer_finished(
        self,
        source: Address,
        binding: CarbonTicketResolution,
        race: GameRaceState,
        logical: bytes,
        *,
        endpoint_count: int,
    ) -> ResultOutcome:
        """Accept notification-only 0x0E without granting result authority."""
        if race.phase != RacePhase.RACING:
            self.log.info(
                "Carbon GM client RacerFinished ignored outside race: "
                "gid=%s phase=%s src=%s:%d pid=%d",
                binding.game.gid,
                race.phase.name,
                source[0],
                source[1],
                binding.participant.player_id,
            )
            return ResultOutcome()
        try:
            signal = FinishSignal.decode(logical, expected_type=RACER_FINISHED)
        except ResultCodecError as exc:
            self.log.warning(
                "Carbon GM malformed client RacerFinished: "
                "gid=%s src=%s:%d error=%s body=%s",
                binding.game.gid,
                source[0],
                source[1],
                exc,
                bytes(logical[:64]).hex(),
            )
            return ResultOutcome()
        if signal.server_car_id <= 0:
            self.log.warning(
                "Carbon GM rejected client RacerFinished car: "
                "gid=%s src=%s:%d car=%d reason=%d",
                binding.game.gid,
                source[0],
                source[1],
                signal.server_car_id,
                signal.finish_reason,
            )
            return ResultOutcome()
        if signal.server_car_id in race.relayed_racer_finished:
            return ResultOutcome()
        race.relayed_racer_finished.add(signal.server_car_id)
        return ResultOutcome(
            publications=(signal.encode(RACER_FINISHED),),
            relayed_racer=RelayedRacerFinished(
                gid=binding.game.gid,
                source=source,
                player_id=binding.participant.player_id,
                server_car_id=signal.server_car_id,
                finish_reason=signal.finish_reason,
                endpoint_count=int(endpoint_count),
            ),
        )

    def handle_result_report(
        self,
        source: Address,
        binding: CarbonTicketResolution,
        logical: bytes,
    ) -> ResultOutcome:
        try:
            decoded = PlayerGameResults.decode(logical)
        except ResultCodecError as exc:
            self.log.warning(
                "Carbon GM malformed result report: gid=%s src=%s:%d "
                "error=%s bytes=%d",
                binding.game.gid,
                source[0],
                source[1],
                exc,
                len(logical),
            )
            return ResultOutcome()
        if decoded.message_type == FINAL_GAME_RESULTS:
            self.log.warning(
                "Carbon GM ignored client FinalGameResults authority: "
                "gid=%s src=%s:%d reporting_car=%d",
                binding.game.gid,
                source[0],
                source[1],
                decoded.results.reporting_server_car_id,
            )
            return ResultOutcome()
        tracker = self.tracker(binding.game)
        profile_id = binding.participant.identity.profile_id
        accepted = tracker.record_report(profile_id, decoded.results)
        self.log.info(
            "Carbon GM GameResults captured: gid=%s ranked=%s profile=%d "
            "reporting_car=%d racers=%d mode=%d accepted=%s",
            binding.game.gid,
            int(binding.game.is_ranked),
            profile_id,
            decoded.results.reporting_server_car_id,
            decoded.results.number_of_racers,
            decoded.results.race_mode,
            int(accepted),
        )
        return self.finalize_if_complete(binding.game)

    def finalize_if_complete(self, game: CarbonGame) -> ResultOutcome:
        """Prepare the authoritative final publication once quorum is met."""
        finalized = self._maybe_finalize(game)
        return ResultOutcome(
            () if finalized is None else (finalized.body,),
            finalized,
        )

    def _maybe_finalize(
        self,
        game: CarbonGame,
    ) -> FinalResultCommit | None:
        tracker = self.tracker(game)
        participants = sorted(
            game.participants.values(),
            key=lambda participant: participant.player_id,
        )
        profile_ids = [
            participant.identity.profile_id for participant in participants
        ]
        if tracker.final_sent:
            return None
        native_finish_complete = bool(profile_ids) and all(
            tracker.car_by_profile.get(profile_id) in tracker.finish_signals
            for profile_id in profile_ids
        )
        ranked_report_order = (
            tracker.ranked_report_order(profile_ids)
            if tracker.ranked and not native_finish_complete
            else None
        )
        if not tracker.is_complete(profile_ids):
            self.log.info(
                "Carbon GM FinalGameResults pending: gid=%s ranked=%s "
                "participants=%d reports=%d leader_finishes=%d "
                "unanimous_report_order=%s",
                game.gid,
                int(tracker.ranked),
                len(profile_ids),
                len(tracker.reports),
                len(tracker.finish_signals),
                int(ranked_report_order is not None),
            )
            return None
        result_authority = (
            "native-leader-order"
            if tracker.ranked and native_finish_complete
            else (
                "unanimous-authenticated-reports"
                if tracker.ranked
                else "authenticated-report-quorum"
            )
        )
        try:
            race_mode = int(
                str(game.properties.get("B-U-game_mode", "0")).strip() or 0
            )
        except ValueError:
            race_mode = 0
        finalized = tracker.finalize(
            [
                (participant.identity.profile_id, participant.player_id)
                for participant in participants
            ],
            race_mode_fallback=race_mode,
        )
        rankings = {
            placement.profile_id: placement.ranking
            for placement in finalized.placements
        }
        finished_profiles = {
            placement.profile_id
            for placement in finalized.placements
            if placement.finished
        }
        progression = self.progression.record_authoritative_race(
            tuple(participant.identity for participant in participants),
            event_type=finalized.results.race_mode,
            rankings=rankings,
            finished_profile_ids=finished_profiles,
            ranked=finalized.ranked,
        )
        self.log.info(
            "Carbon progression race awards: gid=%s ranked=%s event=%d "
            "virus=%s viral_recipients=%s carbon_plague=%s beat_moderator=%s",
            game.gid,
            int(finalized.ranked),
            int(finalized.results.race_mode),
            progression.awards.viral_stat or "none",
            ",".join(map(str, progression.awards.viral_recipients)) or "none",
            ",".join(map(str, progression.awards.carbon_plague_recipients))
            or "none",
            ",".join(map(str, progression.awards.beat_moderator_recipients))
            or "none",
        )

        rows = list(finalized.results.racers)
        for placement in finalized.placements:
            row_index = next(
                (
                    index
                    for index, row in enumerate(rows)
                    if row.server_car_id == placement.server_car_id
                ),
                None,
            )
            if row_index is None:
                continue
            rows[row_index] = replace(
                rows[row_index],
                delta_xp=float(
                    progression.rep_awards.get(placement.profile_id, 0.0)
                ),
            )
        authoritative = replace(finalized.results, racers=tuple(rows))
        return FinalResultCommit(
            game=game,
            ranked=bool(finalized.ranked),
            body=authoritative.encode(FINAL_GAME_RESULTS),
            authoritative=authoritative,
            placements=tuple(finalized.placements),
            progression=progression,
            winner_profile_ids=tuple(finalized.winner_profile_ids),
            result_authority=result_authority,
        )

    def commit(self, outcome: ResultOutcome, race: GameRaceState) -> None:
        """Apply post-publication state in the original wire order."""
        accepted_leader = outcome.accepted_leader
        if accepted_leader is not None:
            self.log.info(
                "Carbon GM LeaderFinished accepted: "
                "gid=%s ranked=%s profile=%d pid=%d car=%d reason=%d rank=%d",
                accepted_leader.gid,
                int(accepted_leader.ranked),
                accepted_leader.profile_id,
                accepted_leader.player_id,
                accepted_leader.server_car_id,
                accepted_leader.finish_reason,
                accepted_leader.rank,
            )
        relayed_racer = outcome.relayed_racer
        if relayed_racer is not None:
            self.log.info(
                "Carbon GM release V833 client RacerFinished relayed: "
                "gid=%s src=%s:%d pid=%d car=%d reason=%d endpoints=%d "
                "authority=notification-only",
                relayed_racer.gid,
                relayed_racer.source[0],
                relayed_racer.source[1],
                relayed_racer.player_id,
                relayed_racer.server_car_id,
                relayed_racer.finish_reason,
                relayed_racer.endpoint_count,
            )
        final = outcome.final
        if final is None:
            return
        if race.phase == RacePhase.RACING:
            race.mark_finished()
        self._append_journal(
            final.game,
            final.authoritative,
            final.placements,
            final.progression,
        )
        self.log.info(
            "Carbon GM authoritative FinalGameResults sent: gid=%s ranked=%s "
            "racers=%d winners=%s skills=%s rep=%s authority=%s",
            final.game.gid,
            int(final.ranked),
            final.authoritative.number_of_racers,
            ",".join(map(str, final.winner_profile_ids)) or "none",
            final.progression.skill_levels or "unchanged",
            final.progression.rep_awards or "none",
            final.result_authority,
        )

    def _append_journal(self, game, results, placements, progression) -> None:
        if self.progression.path is None:
            return
        journal = self.progression.path.with_name("carbon_race_results.jsonl")
        payload = {
            "gid": game.gid,
            "timestamp": int(time.time()),
            "ranked": bool(game.is_ranked),
            "race_mode": results.race_mode,
            "track_number": results.track_number,
            "track_direction": results.track_direction,
            "number_of_laps": results.number_of_laps,
            "players": [],
        }
        for placement in placements:
            row = results.row_for_car(placement.server_car_id) or results.racers[0]
            payload["players"].append(
                {
                    "profile_id": placement.profile_id,
                    "player_id": placement.player_id,
                    "server_car_id": placement.server_car_id,
                    "ranking": placement.ranking,
                    "finish_reason": placement.finish_reason,
                    "finished": placement.finished,
                    "lost_connection": placement.lost_connection,
                    "race_time": row.race_time,
                    "best_lap_time": row.best_lap_time,
                    "laps_completed": row.laps_completed,
                    "average_speed": row.average_speed,
                    "top_speed": row.top_speed,
                    "canyon_points": row.canyon_points,
                    "delta_xp": row.delta_xp,
                    "skill_level": progression.skill_levels.get(
                        placement.profile_id
                    ),
                }
            )
        try:
            journal.parent.mkdir(parents=True, exist_ok=True)
            with journal.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, sort_keys=True) + "\n")
        except OSError as exc:
            self.log.warning(
                "Carbon result journal write failed: path=%s error=%s",
                journal,
                exc,
            )
