"""Ranking and post-race handlers for the Classic lobby service.

The mixin keeps the historical ``ClassicPreloginService`` API intact while
moving ranking-specific behavior out of the protocol router.
"""

from __future__ import annotations

import logging
import time

from classic.ea.directory import GameSession
from classic.ea.ranking import MW_STAT_CATEGORY_COUNT, STAT_CATEGORY_COUNT
from classic.lobby.constants import U2_POSTRACE_UDP_GRACE_SECONDS
from classic.lobby.models import ClassicPreloginContext, ClassicPreloginReply

# Preserve the existing logger category during this compatibility refactor.
log = logging.getLogger("classic.protocols.prelogin")


class ClassicRankingMixin:
    """Ranking behavior shared by Underground 2 and Most Wanted."""

    def _stats_csv(self, context: ClassicPreloginContext) -> str:
        persona = (
            context.auth.persona
            or (context.auth.account.persona if context.auth.account else "")
            or "Player"
        )
        return self.ranking.profile_hex_csv(self.profile.game_id, persona)

    def _finalize_mw_missing_rank_reports(self, game: GameSession) -> tuple[int, ...]:
        """Mark missing stock MW reports as disconnects at owner replacement.

        A race abort does not carry a distinct DNF bit in RESU. The surviving
        client reports normal placements while the aborting participant sends
        no rank command. Owner ``gcre`` is the capture-backed terminal signal;
        unlike a timeout it cannot race a legitimately late report.
        """

        captured_results = [
            result
            for result in game.results.values()
            if result.get("source") == "mw_resu"
        ]
        if not captured_results:
            return ()
        missing = sorted(game.participants - game.reported_participants)
        if not missing:
            return ()
        category = self._lobby_int(captured_results[0].get("category", 0), 0)
        category = max(0, min(MW_STAT_CATEGORY_COUNT - 1, category))
        elapsed = max(
            (self._lobby_int(result.get("time", 0), 0) for result in captured_results),
            default=0,
        )
        finalized: list[int] = []
        for user_id in missing:
            persona = game.participant_personas.get(user_id, f"Player{user_id}")
            accepted, _complete = self.sessions.record_result(
                game.game_id,
                user_id,
                {
                    "persona": persona,
                    "outcome": "DISCONNECT",
                    "category": category,
                    "time": elapsed,
                    "source": "mw_resu_missing",
                },
            )
            if not accepted:
                continue
            opponents = [
                opponent_persona
                for opponent_id, opponent_persona in game.participant_personas.items()
                if opponent_id != user_id
            ]
            self.ranking.record_result(
                self.profile.game_id,
                persona,
                category_index=category,
                outcome="DISCONNECT",
                opponent_personas=opponents,
                race_key=str(game.game_id),
                reporter_key=user_id,
                persona_id=self.ranking.persona_id_for_profile(user_id),
                race_metadata={
                    "status": "complete",
                },
                result_metadata={
                    "elapsed_ms": elapsed * 1000,
                    "source": "mw_resu_missing",
                },
            )
            finalized.append(user_id)
        if finalized:
            log.info(
                "%s finalized missing MW rank reports as disconnects: "
                "game=%d users=%s category=%d",
                self.profile.game_id,
                game.game_id,
                ",".join(str(user_id) for user_id in finalized),
                category,
            )
        return tuple(finalized)

    @staticmethod
    def _u2_snap_stats_board(index: int, channel: int) -> int:
        """Map stock U2 SNAP channels to its six ranked race modes.

        The client uses CHAN 6..11 for global rows and 12..17 for its personal
        row. Its menu orders Drift before Drag, while race reports and durable
        storage use Drag before Drift. INDEX identifies one of ten tier/track
        boards inside that mode.
        Older direct-index probes without CHAN remain supported as 1..6.
        """

        channel_to_stats_board = {
            6: 1,   # Circuit (global)
            7: 2,   # Sprint (global)
            8: 4,   # Drift (global)
            9: 3,   # Drag (global)
            10: 5,  # Street X (global)
            11: 6,  # URL (global)
            12: 1,  # Circuit (personal)
            13: 2,  # Sprint (personal)
            14: 4,  # Drift (personal)
            15: 3,  # Drag (personal)
            16: 5,  # Street X (personal)
            17: 6,  # URL (personal)
        }
        if channel in channel_to_stats_board:
            return channel_to_stats_board[channel]
        if channel == 0 and 1 <= index <= STAT_CATEGORY_COUNT:
            return index
        return 0

    @staticmethod
    def _mw_snap_stats_board(index: int) -> int:
        """Map MW's request INDEX to one of its three event-mode boards.

        CHAN is only the asynchronous response slot.  Each INDEX group is
        requested twice with different CHAN values (for example INDEX=4 uses
        CHAN=5 and CHAN=8), and both requests must expose the same statistics.
        """

        for first, last, stats_board in (
            (1, 4, 2),
            (5, 8, 3),
            (9, 13, 4),
            (14, 17, 2),
            (18, 21, 3),
            (22, 26, 4),
            (27, 30, 2),
            (31, 34, 3),
            (35, 39, 4),
        ):
            if first <= index <= last:
                return stats_board
        return 0

    def _dispatch_u2_rank(
        self,
        packet: ClassicEAFrame,
        context: ClassicPreloginContext,
        fields: dict[str, str],
    ) -> ClassicPreloginReply:
        # Lazy imports keep ``classic.lobby`` independently importable while
        # ``classic.protocols`` re-exports the prelogin service.
        from classic.protocols.frame import ClassicEAFrame
        from classic.protocols.ranking_codec import decode_u2_rank_result

        identity = context.auth.identity
        persona = context.auth.persona or "Player"
        pending_game_id = (
            self._u2_pending_games.get(identity.user_id, 0)
            if identity is not None
            else 0
        )
        game = self.sessions.get_game(pending_game_id) if pending_game_id else None
        result = decode_u2_rank_result(fields, persona, game)
        response = ClassicEAFrame.from_fields(
            "rank",
            (("RANK", "Unranked"), ("TIME", 866)),
            separator="\t",
            final_separator=False,
        ).encode()
        if result is None:
            log.warning(
                "%s rejected U2 rank payload: user=%d persona=%s game=%d "
                "rept=%s resu_len=%d names=%s",
                self.profile.game_id,
                identity.user_id if identity is not None else 0,
                persona,
                game.game_id if game is not None else 0,
                str(fields.get("REPT", "") or "-"),
                len(str(fields.get("RESU", "") or "")),
                ",".join(
                    str(fields.get(f"NAME{index}", "") or "-")
                    for index in range(4)
                    if f"NAME{index}" in fields
                ) or "-",
            )
            return ClassicPreloginReply((response,), "game_report_rejected")

        room = self._u2_room(game.room_id) if game is not None else None
        ranked = room is not None and room[0] <= 4
        outcome = (
            "DISCONNECT"
            if result.disconnected
            else ("WIN" if result.place == 1 else "LOSS")
        )
        accepted = False
        complete = False
        if identity is not None and game is not None:
            accepted, complete = self.sessions.record_result(
                game.game_id,
                identity.user_id,
                {
                    "persona": persona,
                    "outcome": outcome,
                    "category": result.category,
                    "source": "u2_resu",
                    "place": result.place,
                    "finish_mark": result.finish_mark,
                    "track": result.track,
                    "direction": result.direction,
                    "laps": result.laps,
                    "best_lap_ms": result.best_lap_ms,
                    "best_drift": result.best_drift,
                    "ranked": ranked,
                },
            )
            if accepted and ranked and 0 <= result.category < STAT_CATEGORY_COUNT:
                opponents = [
                    opponent_persona
                    for user_id, opponent_persona in game.participant_personas.items()
                    if user_id != identity.user_id
                ]
                self.ranking.record_result(
                    self.profile.game_id,
                    persona,
                    category_index=result.category,
                    outcome=outcome,
                    opponent_personas=opponents,
                    race_key=str(game.game_id),
                    reporter_key=identity.user_id,
                    persona_id=identity.persona_id,
                    race_metadata={
                        "ranked": ranked,
                        "track": result.track,
                        "direction": result.direction,
                        "laps": result.laps,
                        "status": "complete" if complete else "reported",
                    },
                    result_metadata={
                        "place": result.place,
                        "finish_mark": result.finish_mark,
                        "best_lap_ms": result.best_lap_ms,
                        "best_drift": result.best_drift,
                        "source": "u2_resu",
                    },
                )
            if accepted and complete:
                self._schedule_u2_transport_retirement(game)
                for user_id in game.participants:
                    if self._u2_pending_games.get(user_id) == game.game_id:
                        self._u2_pending_games.pop(user_id, None)
        if accepted:
            log.info(
                "%s decoded U2 rank result: user=%d game=%d room=%s "
                "ranked=%d index=%d place=%d finish=%d disconnect=%d "
                "participants=%d type=%d category=%d track=%d dir=%d "
                "laps=%d best_lap_ms=%d best_drift=%d complete=%d",
                self.profile.game_id,
                identity.user_id if identity is not None else 0,
                game.game_id if game is not None else 0,
                room[1] if room is not None else "-",
                1 if ranked else 0,
                result.reporter_index,
                result.place,
                result.finish_mark,
                1 if result.disconnected else 0,
                result.participant_count,
                result.race_type,
                result.category,
                result.track,
                result.direction,
                result.laps,
                result.best_lap_ms,
                result.best_drift,
                1 if complete else 0,
            )
        else:
            log.info(
                "%s ignored duplicate U2 rank result: user=%d game=%d "
                "index=%d place=%d category=%d",
                self.profile.game_id,
                identity.user_id if identity is not None else 0,
                game.game_id if game is not None else 0,
                result.reporter_index,
                result.place,
                result.category,
            )
        log.info(
            "%s sent stock U2 rank acknowledgement only: user=%d game=%d "
            "postrace_lobby_frames=0 udp_grace=%ss",
            self.profile.game_id,
            identity.user_id if identity is not None else 0,
            game.game_id if game is not None else 0,
            U2_POSTRACE_UDP_GRACE_SECONDS,
        )
        return ClassicPreloginReply((response,), "game_report")

    def _dispatch_mw_rank(
        self,
        packet: ClassicEAFrame,
        context: ClassicPreloginContext,
        fields: dict[str, str],
    ) -> ClassicPreloginReply:
        # See ``_dispatch_u2_rank`` for why these imports are local.
        from classic.protocols.frame import ClassicEAFrame
        from classic.protocols.ranking_codec import (
            decode_mw_rank_result,
            mw_rank_payload_trace,
            result_category,
            result_outcome,
        )

        identity = context.auth.identity
        persona = context.auth.persona or "Player"
        game = (
            self.sessions.get_game(context.lobby_game_id)
            if context.lobby_game_id
            else None
        )
        postrace_phase1 = bool(
            game is not None
            and context.mw_postrace_return_pending
            and context.mw_postrace_snapshot_game_id == game.game_id
        )
        with self._connections_lock:
            postrace_room_view_report = bool(
                game is not None
                and context.mw_postrace_return_pending
                and game.game_id in self._mw_postrace_room_view_games
            )
        mw_result = decode_mw_rank_result(fields, persona, game)
        if mw_result is None:
            participant_names = (
                ",".join(
                    str(game.participant_personas.get(user_id, user_id))
                    for user_id in sorted(game.participants)
                )
                if game is not None
                else "-"
            )
            supplied_names = ",".join(
                str(fields.get(f"NAME{index}", "") or "-")
                for index in range(8)
                if f"NAME{index}" in fields
            )
            log.warning(
                "%s rejected MW rank payload: user=%d persona=%s game=%d "
                "rept=%s resu_len=%d names=%s participants=%s trace=%s",
                self.profile.game_id,
                identity.user_id if identity is not None else 0,
                persona,
                game.game_id if game is not None else 0,
                str(fields.get("REPT", "") or "-"),
                len(str(fields.get("RESU", "") or "")),
                supplied_names or "-",
                participant_names,
                mw_rank_payload_trace(fields),
            )
        if mw_result is not None and game is not None:
            prior_categories = {
                self._lobby_int(result.get("category", 0), 0)
                for result in game.results.values()
                if result.get("source") == "mw_resu"
            }
            if prior_categories and mw_result.category not in prior_categories:
                log.warning(
                    "%s rejected inconsistent MW rank category: "
                    "user=%d game=%d category=%d existing=%s",
                    self.profile.game_id,
                    identity.user_id if identity is not None else 0,
                    game.game_id,
                    mw_result.category,
                    ",".join(str(value) for value in sorted(prior_categories)),
                )
                mw_result = None
        category = (
            mw_result.category
            if mw_result is not None
            else result_category(fields)
        )
        outcome = (
            ("WIN" if mw_result.place == 1 else "LOSS")
            if mw_result is not None
            else result_outcome(fields)
        )
        elapsed = self._lobby_int(fields.get("TIME", "0"), 0)
        if not elapsed and game is not None and game.started_at:
            elapsed = int(max(0.0, time.time() - game.started_at))
        result_elapsed = (
            max(0, int(round(mw_result.elapsed)))
            if mw_result is not None and mw_result.elapsed > 0.0
            else elapsed
        )
        accepted = False
        complete = False
        if identity is not None and game is not None and outcome:
            accepted, complete = self.sessions.record_result(
                game.game_id,
                identity.user_id,
                {
                    "persona": persona,
                    "outcome": outcome,
                    "category": category,
                    "time": result_elapsed,
                    "source": "mw_resu" if mw_result is not None else "fields",
                    "place": mw_result.place if mw_result is not None else 0,
                    "flags": mw_result.flags if mw_result is not None else 0,
                    "nos_used": mw_result.nos_used if mw_result is not None else 0.0,
                },
            )
            if accepted:
                opponents = [
                    opponent_persona
                    for user_id, opponent_persona in game.participant_personas.items()
                    if user_id != identity.user_id
                ]
                self.ranking.record_result(
                    self.profile.game_id,
                    persona,
                    category_index=category,
                    outcome=outcome,
                    opponent_personas=opponents,
                    nos_used=(
                        mw_result.nos_used
                        if mw_result is not None
                        else 0.0
                    ),
                    race_key=str(game.game_id),
                    reporter_key=identity.user_id,
                    persona_id=identity.persona_id,
                    race_metadata={
                        "status": "complete" if complete else "reported",
                    },
                    result_metadata={
                        "place": mw_result.place if mw_result is not None else 0,
                        "elapsed_ms": (
                            int(round(mw_result.elapsed * 1000.0))
                            if mw_result is not None
                            else result_elapsed * 1000
                        ),
                        "flags": mw_result.flags if mw_result is not None else 0,
                        "source": "mw_resu" if mw_result is not None else "fields",
                    },
                )
                if mw_result is not None:
                    log.info(
                        "%s decoded MW rank result: user=%d game=%d "
                        "index=%d place=%d flags=%d elapsed=%.3f "
                        "nos_used=%.3f participants=%d category=%d "
                        "record_gap=%d",
                        self.profile.game_id,
                        identity.user_id,
                        game.game_id,
                        mw_result.reporter_index,
                        mw_result.place,
                        mw_result.flags,
                        mw_result.elapsed,
                        mw_result.nos_used,
                        mw_result.participant_count,
                        category,
                        mw_result.record_gap,
                    )
            elif mw_result is not None:
                user_id = identity.user_id
                log.warning(
                    "%s ignored decoded MW rank result: user=%d game=%d "
                    "member=%d already_reported=%d state=%s index=%d "
                    "place=%d category=%d",
                    self.profile.game_id,
                    user_id,
                    game.game_id,
                    1 if user_id in game.participants else 0,
                    1 if user_id in game.reported_participants else 0,
                    game.state.value,
                    mw_result.reporter_index,
                    mw_result.place,
                    category,
                )
        summary = self.ranking.summary(
            self.profile.game_id,
            persona,
            category,
        )
        response_fields: tuple[tuple[str, object], ...] = (
            # Retail captures currently show this textual label even while
            # the numeric ranking/stat block is active.
            ("RANK", "Unranked"),
            ("POSITION", summary["rank"]),
            ("WINS", summary["wins"]),
            ("LOSSES", summary["losses"]),
            ("DISCONNECTS", summary["disconnects"]),
            ("REP", summary["rep"]),
            ("TIME", elapsed),
            ("RECORDED", 1 if accepted else 0),
            ("COMPLETE", 1 if complete else 0),
        )
        reply = ClassicEAFrame.from_fields(
            packet.command,
            response_fields,
            reserved=packet.reserved,
            separator="\t",
            final_separator=False,
        ).encode()
        if postrace_room_view_report:
            # Phase 2 already retired this game for every participant.
            # The owner can still submit its report on the way back to
            # the lobby, but +gam/+sst or +ses would republish the old
            # race object and prevent the following replacement gcre.
            return ClassicPreloginReply(
                (reply,),
                "game_report_postrace_room_view",
            )
        if postrace_phase1 and game is not None:
            active_fields = self._mw_game_fields(
                game,
                active=True,
                viewer_id=self._user_id(context),
            )
            seed = self._mw_session_seeds.get(
                game.game_id,
                int(game.started_at or game.created_at) & 0x7FFFFFFF,
            )
            session = ClassicEAFrame.from_fields(
                "+ses",
                (*active_fields, ("SEED", seed), ("SELF", persona)),
                separator="\t",
                final_separator=False,
            ).encode()

            def publish_postrace_room_view() -> None:
                # The retail trace leaves one client tick between +ses
                # and the G=0 room view.  Without that phase boundary MW
                # treats the bare +mgm as a vanished session and submits
                # gjoi for the completed game.
                time.sleep(0.075)
                if (
                    context.mw_postrace_snapshot_game_id
                    != game.game_id
                    or context.send_wire is None
                ):
                    return
                context.mw_postrace_snapshot_game_id = 0
                context.mw_postrace_room_view_game_id = game.game_id
                with self._connections_lock:
                    self._mw_postrace_room_view_games.add(game.game_id)
                room_frames = self._mw_postrace_room_frames(context, game)
                sent = all(context.send_wire(frame) for frame in room_frames)
                for user_id in set(game.participants):
                    if user_id == self._user_id(context):
                        continue
                    peer = self._context_for_user(user_id)
                    if peer is not None and peer.send_wire is not None:
                        peer.send_wire(self._mw_who_frame(peer, game=game))
                log.info(
                    "%s published phase-2 post-race room view after "
                    "rank: user=%d game=%d participants=%d sent=%d",
                    self.profile.game_id,
                    self._user_id(context),
                    game.game_id,
                    len(game.participants),
                    1 if sent else 0,
                )

            return ClassicPreloginReply(
                (reply, session),
                "game_report_postrace_phase2",
                after_send=publish_postrace_room_view,
            )
        frames: list[bytes] = [reply]
        if game is not None:
            frames.append(
                ClassicEAFrame.from_fields(
                    "+gam",
                    self._mw_game_fields(game),
                    separator="\t",
                    final_separator=False,
                ).encode()
            )
        frames.append(
            ClassicEAFrame.from_fields(
                "+sst",
                (
                    ("GCR", 0),
                    ("UIL", 1),
                    ("UIR", 0),
                    ("GIP", 0 if complete else (1 if game is not None else 0)),
                ),
                separator="\t",
                final_separator=False,
            ).encode()
        )
        return ClassicPreloginReply(tuple(frames), "game_report")


__all__ = ["ClassicRankingMixin"]
