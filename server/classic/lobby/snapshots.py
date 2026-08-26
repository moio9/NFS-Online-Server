"""Classic leaderboard and track snapshot projection.

The ranking store and post-race mutations remain in ``lobby.ranking``.  This
mixin owns only the read-side ``snap/+snp`` projection shared by U2 and MW.
"""

from __future__ import annotations

from hashlib import md5
import logging
from typing import TYPE_CHECKING

from classic.lobby.models import ClassicPreloginContext

if TYPE_CHECKING:
    from classic.protocols.frame import ClassicEAFrame


def _ea_frame():
    """Load the wire codec lazily to avoid a protocols-package import cycle."""
    from classic.protocols.frame import ClassicEAFrame

    return ClassicEAFrame


# Preserve existing log filters and operational dashboards.
log = logging.getLogger("classic.protocols.prelogin")


class ClassicSnapshotMixin:
    """Build Classic SNAP responses without owning ranking persistence."""

    def _snap_frames(
        self,
        fields: dict[str, str],
        context: ClassicPreloginContext,
    ) -> tuple[bytes, ...]:
        ClassicEAFrame = _ea_frame()
        def as_int(name: str, default: int) -> int:
            try:
                raw = str(fields.get(name, default) or default).strip()
                try:
                    return int(raw)
                except ValueError:
                    return int(raw, 16)
            except (TypeError, ValueError):
                return default

        index = as_int("INDEX", 1)
        channel = as_int("CHAN", 0)
        start = max(0, as_int("START", 0))
        requested_range = max(1, min(100, as_int("RANGE", 100)))
        board = (index or 1) if self._is_most_wanted else (channel or index or 1)
        mw_high_channel_stats = bool(
            self._is_most_wanted
            and 8 <= channel <= 10
            and self._mw_snap_stats_board(index)
        )
        stats_board = (
            self._mw_snap_stats_board(index)
            if self._is_most_wanted
            else self._u2_snap_stats_board(index, channel)
        )
        find = str(fields.get("FIND", "") or "").strip()
        persona = (
            context.auth.persona
            or (context.auth.account.persona if context.auth.account else "")
            or "Player"
        )
        rows: list[bytes] = []
        if stats_board:
            known_personas = self._snap_personas(persona)
            for known_persona in known_personas:
                self.ranking.get_or_create(self.profile.game_id, known_persona)
            query_start = 0 if find else start
            query_limit = 100 if find else requested_range
            leaderboard = self.ranking.leaderboard(
                self.profile.game_id,
                stats_board - 1,
                start=query_start,
                limit=query_limit,
                include_persona=persona,
            )
            ranked = [
                (query_start + offset + 1, stats)
                for offset, stats in enumerate(leaderboard)
            ]
            ranked = self._filter_snap_rows(ranked, find, persona)
            ranked = ranked[:requested_range]
            for visible_rank, stats in ranked:
                if self._is_most_wanted:
                    if mw_high_channel_stats:
                        row_fields = (
                            ("P", f"{visible_rank - 1:x},1,1"),
                            (
                                "S",
                                stats.mw_personal_hex_csv(stats_board - 1),
                            ),
                            ("N", stats.persona),
                            ("O", "1"),
                        )
                    else:
                        row_fields = (
                            ("P", f"{visible_rank - 1:x}"),
                            (
                                "S",
                                stats.mw_snap_hex_csv(
                                    stats_board - 1,
                                    visible_rank,
                                ),
                            ),
                            ("N", stats.persona),
                            ("O", "1"),
                        )
                else:
                    # Retail U2 stores these fields independently in its SNAP
                    # row object: R is the displayed rank, P is scalar points,
                    # and S[1]/S[2] are ranked wins/losses.  Packing rank into
                    # P or omitting S leaves Rank/Games/Wins/Losses at zero.
                    row_fields = (
                        ("N", stats.persona),
                        ("R", visible_rank),
                        ("P", stats.u2_snap_points_hex(stats_board - 1)),
                        (
                            "S",
                            stats.u2_personal_snap_hex_csv(stats_board - 1),
                        ),
                        ("O", "1"),
                    )
                rows.append(
                    ClassicEAFrame.from_fields(
                        "+snp",
                        row_fields,
                        separator="\t",
                        final_separator=False,
                    ).encode()
                )
                if (
                    self._is_underground2
                    and stats.persona.casefold() == persona.casefold()
                ):
                    log.info(
                        "U2 stats row payload: index=%d channel=%d stats_board=%d "
                        "persona=%s r=%s p=%s s=%s",
                        index,
                        channel,
                        stats_board,
                        stats.persona,
                        dict(row_fields)["R"],
                        dict(row_fields)["P"],
                        dict(row_fields).get("S", "-"),
                    )
                if self._is_most_wanted and stats.persona.casefold() == persona.casefold():
                    stat_values = dict(row_fields)["S"].split(",")
                    if mw_high_channel_stats:
                        category_offset = (stats_board - 1) * 7
                        mode_values = stat_values[category_offset : category_offset + 7]
                    else:
                        mode_values = stat_values
                    log.info(
                        "MW stats row payload: index=%d channel=%d stats_board=%d "
                        "persona=%s p=%s s_count=%d mode_s=%s",
                        index,
                        channel,
                        stats_board,
                        stats.persona,
                        dict(row_fields)["P"],
                        len(stat_values),
                        ",".join(mode_values),
                    )
        else:
            track_rows: list[tuple[int, str]] = []
            find_key = find.casefold()
            for row_persona in self._snap_personas(persona):
                if find_key and find_key != "$" and find_key not in row_persona.casefold():
                    continue
                digest = md5(
                    f"{board}:{row_persona}".encode("utf-8", errors="ignore")
                ).digest()
                value = 0x20 + (
                    int.from_bytes(digest[:2], "big") % 0x400
                    if 28 <= board <= 35
                    else digest[0] % 0x60
                )
                track_rows.append((value, row_persona))
            track_rows.sort(
                key=lambda item: (
                    -item[0] if 28 <= board <= 35 else item[0],
                    item[1].casefold(),
                )
            )
            for value, row_persona in track_rows[start : start + requested_range]:
                rows.append(
                    ClassicEAFrame.from_fields(
                        "+snp",
                        (
                            ("P", f"{value:x},1,1"),
                            ("N", row_persona),
                            ("O", "1"),
                        ),
                        separator="\t",
                        final_separator=False,
                    ).encode()
                )
        row_count = len(rows)
        if self._is_most_wanted:
            # Stock MW stores CHAN from this snap response before its +snp
            # handler will accept any rows. RANGE is the expected number of
            # cached entries for that channel; the per-row callback completes
            # and releases the active request when the cache reaches RANGE.
            # Advertise the actual count, then send exactly those rows.
            reply_fields = (
                ("INDEX", index),
                ("CHAN", channel),
                ("START", start),
                ("RANGE", row_count),
                ("SEQN", 0),
            )
            log.info(
                "MW stats snapshot completed: index=%d channel=%d board=%d "
                "stats_board=%d start=%d requested=%d find=%r rows=%d "
                "personas=%s range=%d order=header-rows",
                index,
                channel,
                board,
                stats_board,
                start,
                requested_range,
                find,
                row_count,
                ",".join(stats.persona for _rank, stats in ranked)
                if stats_board
                else "-",
                row_count,
            )
        else:
            # Stock U2 treats RANGE in the response as the exact number of
            # +snp rows that follow.  The request normally asks for 100 global
            # rows, but echoing that value while returning fewer rows leaves
            # the ranking transaction permanently incomplete and the Overall
            # Rankings screen never opens.
            reply_fields = (
                ("INDEX", index),
                ("CHAN", channel),
                ("START", start),
                ("RANGE", row_count),
                ("SEQN", 0),
                ("COUNT", row_count),
                ("TOTAL", row_count),
                ("MORE", 0),
            )
            log.info(
                "U2 stats snapshot completed: index=%d channel=%d board=%d "
                "stats_board=%d start=%d requested=%d find=%r rows=%d range=%d",
                index,
                channel,
                board,
                stats_board,
                start,
                requested_range,
                find,
                row_count,
                row_count,
            )
        reply = ClassicEAFrame.from_fields(
            "snap",
            reply_fields,
            separator="\t",
            final_separator=False,
        ).encode()
        return (reply, *rows)

    def _snap_personas(self, current: str) -> tuple[str, ...]:
        candidates = list(self.ranking.personas(self.profile.game_id))
        with self._connections_lock:
            candidates.extend(
                connection.auth.persona
                for connection in self._connections.values()
                if connection.auth.persona
            )
        candidates.append(current)
        unique: dict[str, str] = {}
        for candidate in candidates:
            display = str(candidate or "").strip()
            if display:
                unique.setdefault(display.casefold(), display)
        return tuple(unique.values())

    @staticmethod
    def _filter_snap_rows(
        rows: list[tuple[int, object]],
        find: str,
        current: str,
    ) -> list[tuple[int, object]]:
        wanted = str(find or "").strip().casefold()
        if not wanted:
            return rows
        if wanted == "$":
            wanted = str(current or "").strip().casefold()
        if not wanted:
            return rows
        exact = [
            row
            for row in rows
            if str(getattr(row[1], "persona", "")).strip().casefold() == wanted
        ]
        if exact:
            return exact
        return [
            row
            for row in rows
            if wanted in str(getattr(row[1], "persona", "")).strip().casefold()
        ]
