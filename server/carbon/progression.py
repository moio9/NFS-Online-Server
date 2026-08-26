"""Authoritative Carbon account roles, viral vinyls and race awards.

The original rebroadcaster reads ``Moderator`` and ``Virus1``/``Virus2``/
``Virus3`` from RankingService, then writes ``Beat_Moderator`` and the viral
unlock selected by the event type.  This module keeps those values server-side
and persists them as JSON.  Clients are never trusted to grant privileged
roles or unlocks to themselves.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from threading import RLock
from typing import Iterable, Mapping, Sequence

from carbon.accounts.identity import Identity, IdentityStore


MODERATOR_STAT = "Moderator"
BEAT_MODERATOR_STAT = "Beat_Moderator"
VIRUS_STATS = ("Virus1", "Virus2", "Virus3")
SKILL_LEVEL_STAT = "Skill_Level"
DEFAULT_SKILL_LEVEL = 1000.0
ONLINE_REP_STAT = "Online_Rep"
TOTAL_GAMES_STARTED_STAT = "Total_Online_Games_Started"
TOTAL_GAMES_FINISHED_STAT = "Total_Online_Games_Finished"
DNF_LOSSES_STAT = "DNF_Losses"
RANKED_STATS = (
    SKILL_LEVEL_STAT,
    ONLINE_REP_STAT,
    TOTAL_GAMES_STARTED_STAT,
    TOTAL_GAMES_FINISHED_STAT,
    DNF_LOSSES_STAT,
)
SERVER_MANAGED_STATS = frozenset(
    (MODERATOR_STAT, BEAT_MODERATOR_STAT, *VIRUS_STATS, *RANKED_STATS)
)
EA_MODERATOR_ROLE = "ea_moderator"

# Confirmed in ONLINE/rebroadcaster.exe.c:
# event 3 -> Virus2, event 4 -> Virus3, event 5 -> Virus1.
# Retail Theater captures advertise the corresponding public game modes as
# 13=Canyon Duel, 14=Pursuit Tag and 15=Pursuit Knockout.  Accept both forms
# because some GameResults reports omit their internal race mode and fall back
# to the Theater room property.
VIRUS_BY_EVENT_TYPE: Mapping[int, str] = {
    3: "Virus2",
    4: "Virus3",
    5: "Virus1",
    13: "Virus2",
    14: "Virus3",
    15: "Virus1",
}

VIRUS_STAT_TO_TOKEN: Mapping[str, str] = {
    "Virus1": "VIRUS_KNOCKOUT_FEVER",
    "Virus2": "VIRUS_CANYON_CRAZE",
    "Virus3": "VIRUS_PURSUIT_PANDEMIC",
}
VIRUS_TOKEN_TO_STAT: Mapping[str, str] = {
    token: stat for stat, token in VIRUS_STAT_TO_TOKEN.items()
}
CARBON_PLAGUE_TOKEN = "VIRUS_CARBON_PLAGUE"


@dataclass(frozen=True)
class RaceAwards:
    event_type: int
    viral_stat: str | None = None
    viral_recipients: tuple[int, ...] = ()
    carbon_plague_recipients: tuple[int, ...] = ()
    beat_moderator_recipients: tuple[int, ...] = ()


@dataclass(frozen=True)
class RankedRaceProgression:
    awards: RaceAwards
    ranked: bool
    skill_levels: Mapping[int, float] = field(default_factory=dict)
    rep_awards: Mapping[int, float] = field(default_factory=dict)


# Exact CalculateRepScore lookup table at rebroadcaster.exe DAT_00499040.
# Rows are player counts 2..8; columns are rankings 1..8.
_REP_SCORE_TABLE: tuple[tuple[float, ...], ...] = (
    (28.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
    (30.0, 14.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
    (32.0, 20.0, 12.0, 0.0, 0.0, 0.0, 0.0, 0.0),
    (34.0, 24.0, 16.0, 10.0, 0.0, 0.0, 0.0, 0.0),
    (36.0, 26.0, 20.0, 16.0, 10.0, 0.0, 0.0, 0.0),
    (38.0, 30.0, 24.0, 20.0, 14.0, 8.0, 0.0, 0.0),
    (40.0, 32.0, 28.0, 24.0, 18.0, 12.0, 6.0, 0.0),
)


def calculate_rep_score(player_count: int, ranking: int, *, finished: bool) -> float:
    count = int(player_count)
    rank = int(ranking)
    if not finished or not 2 <= count <= 8 or not 1 <= rank <= 8:
        return 0.0
    return _REP_SCORE_TABLE[count - 2][rank - 1]


def calculate_skill_levels(
    current: Sequence[float],
    rankings: Sequence[int],
) -> tuple[float, ...]:
    """Port of rebroadcaster ``FUN_00404310`` (0..3000 skill pool).

    Carbon uses a linear expected-score window clamped to +/-500 and applies
    ``50 / player_count - 1`` per pair. Overflow/underflow is redistributed so
    the room's total rating remains approximately conserved.
    """
    if len(current) != len(rankings):
        raise ValueError("skill/ranking lengths differ")
    count = len(current)
    if count < 2:
        return tuple(max(0.0, min(3000.0, float(item))) for item in current)
    skills = [float(item) for item in current]
    ranks = [int(item) for item in rankings]
    factor = 50.0 / float(count) - 1.0
    deltas: list[float] = []
    for index, skill in enumerate(skills):
        delta = 0.0
        for opponent, other_skill in enumerate(skills):
            if opponent == index:
                continue
            difference = max(-500.0, min(500.0, skill - other_skill))
            expected = (difference + 500.0) * 0.001
            actual = 1.0 if ranks[index] < ranks[opponent] else 0.0
            delta += (actual - expected) * factor
        deltas.append(delta)
    updated = [skill + delta for skill, delta in zip(skills, deltas)]

    # Match the original sequential clamp/redistribution behavior.
    for index in range(count):
        if updated[index] > 3000.0:
            excess = updated[index] - 3000.0
            share = excess / float(count - 1)
            for opponent in range(count):
                if opponent != index:
                    updated[opponent] += share
            updated[index] = 3000.0
        elif updated[index] < 0.0:
            deficit = updated[index]
            share = deficit / float(count - 1)
            for opponent in range(count):
                if opponent != index:
                    updated[opponent] += share
            updated[index] = 0.0
    return tuple(max(0.0, min(3000.0, item)) for item in updated)


@dataclass
class _AccountProgress:
    account_name: str
    persona: str = ""
    profile_id: int = 0
    roles: set[str] = field(default_factory=set)
    stats: dict[str, float] = field(default_factory=dict)
    stat_texts: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> dict[str, object]:
        return {
            "persona": self.persona,
            "profile_id": self.profile_id,
            "roles": sorted(self.roles),
            "stats": {key: float(value) for key, value in sorted(self.stats.items())},
            "stat_texts": {
                key: str(value)
                for key, value in sorted(self.stat_texts.items())
                if value
            },
        }


class CarbonProgressionStore:
    """Thread-safe, optionally persistent Carbon progression database."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else None
        self._lock = RLock()
        self._accounts: dict[str, _AccountProgress] = {}
        self._profile_to_account: dict[int, str] = {}
        if self.path is not None:
            self._load()

    @staticmethod
    def _account_key(value: str) -> str:
        return str(value or "").strip().casefold()

    @staticmethod
    def _clean_role(value: str) -> str:
        return str(value or "").strip().casefold()

    @staticmethod
    def _clean_stat(value: str) -> str:
        return str(value or "").strip()

    def _load(self) -> None:
        assert self.path is not None
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            raise ValueError(f"invalid Carbon account data {self.path}: {exc}") from exc
        accounts = raw.get("accounts", {}) if isinstance(raw, dict) else {}
        if not isinstance(accounts, dict):
            raise ValueError(f"invalid Carbon account data {self.path}: accounts must be an object")
        for raw_name, value in accounts.items():
            if not isinstance(value, dict):
                continue
            key = self._account_key(raw_name)
            if not key:
                continue
            roles_raw = value.get("roles", [])
            roles = {
                self._clean_role(item)
                for item in roles_raw
                if isinstance(item, str) and self._clean_role(item)
            } if isinstance(roles_raw, list) else set()
            stats_raw = value.get("stats", {})
            stats: dict[str, float] = {}
            if isinstance(stats_raw, dict):
                for stat_name, stat_value in stats_raw.items():
                    name = self._clean_stat(stat_name)
                    if not name:
                        continue
                    try:
                        stats[name] = float(stat_value)
                    except (TypeError, ValueError):
                        continue
            stat_texts_raw = value.get("stat_texts", {})
            stat_texts: dict[str, str] = {}
            if isinstance(stat_texts_raw, dict):
                for stat_name, stat_text in stat_texts_raw.items():
                    name = self._clean_stat(stat_name)
                    text = str(stat_text or "")
                    if name and text:
                        stat_texts[name] = text
            try:
                profile_id = int(value.get("profile_id", 0) or 0)
            except (TypeError, ValueError):
                profile_id = 0
            progress = _AccountProgress(
                account_name=str(raw_name),
                persona=str(value.get("persona", "") or ""),
                profile_id=max(0, profile_id),
                roles=roles,
                stats=stats,
                stat_texts=stat_texts,
            )
            self._accounts[key] = progress
            if progress.profile_id:
                self._profile_to_account[progress.profile_id] = key

    def _save_locked(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "accounts": {
                progress.account_name: progress.to_json()
                for _key, progress in sorted(self._accounts.items())
            },
        }
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(self.path)

    def bind_identity(self, identity: Identity) -> None:
        """Attach a logged-in profile id to its server-side account record."""
        key = self._account_key(identity.account_name)
        if not key:
            return
        with self._lock:
            progress = self._accounts.get(key)
            if progress is None:
                progress = _AccountProgress(identity.account_name)
                self._accounts[key] = progress
            changed = (
                progress.persona != identity.persona
                or progress.profile_id != identity.profile_id
                or progress.account_name != identity.account_name
            )
            if progress.profile_id and progress.profile_id != identity.profile_id:
                self._profile_to_account.pop(progress.profile_id, None)
            progress.account_name = identity.account_name
            progress.persona = identity.persona
            progress.profile_id = identity.profile_id
            self._profile_to_account[identity.profile_id] = key
            if changed:
                self._save_locked()

    def known_identities(self) -> tuple[Identity, ...]:
        """Return persisted account personas for Messenger buddy snapshots."""
        with self._lock:
            identities = []
            for progress in self._accounts.values():
                persona = str(progress.persona or progress.account_name).strip()
                if not persona:
                    continue
                profile_id = int(progress.profile_id or IdentityStore.profile_id_for(persona))
                identities.append(
                    Identity(
                        str(progress.account_name or persona),
                        persona,
                        profile_id,
                        profile_id,
                    )
                )
        return tuple(sorted(identities, key=lambda item: item.persona.casefold()))

    def _progress_for_identity_locked(self, identity: Identity) -> _AccountProgress:
        key = self._account_key(identity.account_name)
        progress = self._accounts.get(key)
        if progress is None:
            progress = _AccountProgress(
                identity.account_name,
                identity.persona,
                identity.profile_id,
            )
            self._accounts[key] = progress
        self._profile_to_account[identity.profile_id] = key
        return progress

    def _progress_for_profile_locked(self, profile_id: int) -> _AccountProgress | None:
        key = self._profile_to_account.get(int(profile_id))
        return self._accounts.get(key) if key is not None else None

    def set_role(self, account_name: str, role: str, enabled: bool = True) -> bool:
        key = self._account_key(account_name)
        clean_role = self._clean_role(role)
        if not key or not clean_role:
            raise ValueError("account name and role are required")
        with self._lock:
            progress = self._accounts.setdefault(key, _AccountProgress(str(account_name).strip()))
            before = clean_role in progress.roles
            if enabled:
                progress.roles.add(clean_role)
            else:
                progress.roles.discard(clean_role)
            changed = before != enabled
            if changed:
                self._save_locked()
            return changed

    def set_stat(self, account_name: str, stat: str, value: float) -> bool:
        """Administrative setter; intended for trusted local configuration."""
        key = self._account_key(account_name)
        clean_stat = self._clean_stat(stat)
        if not key or not clean_stat:
            raise ValueError("account name and stat are required")
        numeric = float(value)
        with self._lock:
            progress = self._accounts.setdefault(key, _AccountProgress(str(account_name).strip()))
            changed = progress.stats.get(clean_stat) != numeric
            progress.stats[clean_stat] = numeric
            if changed:
                self._save_locked()
            return changed

    def stat_for_identity(self, identity: Identity, stat: str, default: float = 0.0) -> float:
        clean_stat = self._clean_stat(stat)
        with self._lock:
            progress = self._progress_for_identity_locked(identity)
            if clean_stat == MODERATOR_STAT:
                return 1.0 if EA_MODERATOR_ROLE in progress.roles else 0.0
            if clean_stat == SKILL_LEVEL_STAT:
                return float(progress.stats.get(clean_stat, DEFAULT_SKILL_LEVEL))
            return float(progress.stats.get(clean_stat, default))

    def stat_for_profile(self, profile_id: int, stat: str, default: float = 0.0) -> float:
        clean_stat = self._clean_stat(stat)
        with self._lock:
            progress = self._progress_for_profile_locked(int(profile_id))
            if progress is None:
                return float(default)
            if clean_stat == MODERATOR_STAT:
                return 1.0 if EA_MODERATOR_ROLE in progress.roles else 0.0
            if clean_stat == SKILL_LEVEL_STAT:
                return float(progress.stats.get(clean_stat, DEFAULT_SKILL_LEVEL))
            return float(progress.stats.get(clean_stat, default))

    def has_stat_for_profile(self, profile_id: int, stat: str) -> bool:
        clean_stat = self._clean_stat(stat)
        with self._lock:
            progress = self._progress_for_profile_locked(int(profile_id))
            if progress is None:
                return False
            if clean_stat in (MODERATOR_STAT, SKILL_LEVEL_STAT):
                return True
            return clean_stat in progress.stats

    def stat_text_for_profile(self, profile_id: int, stat: str) -> str:
        clean_stat = self._clean_stat(stat)
        with self._lock:
            progress = self._progress_for_profile_locked(int(profile_id))
            if progress is None:
                return ""
            return str(progress.stat_texts.get(clean_stat, ""))

    def stats_for_profile(self, profile_id: int, keys: Iterable[str]) -> dict[str, float]:
        return {str(key): self.stat_for_profile(profile_id, str(key)) for key in keys}

    def import_viral_tokens(
        self,
        identity: Identity,
        tokens: Iterable[str],
    ) -> tuple[str, ...]:
        """Seed persistent viral flags from trusted static DLC assignments.

        This keeps explicit administrator assignments and the ``all`` preset
        useful as original-style carrier accounts.  Once imported, a viral
        flag remains attached to the account even if its static assignment is
        later removed, matching the permanent nature of the retail unlock.
        ``VIRUS_CARBON_PLAGUE`` is derived from all three base viruses and is
        therefore never stored as an independent RankingService stat.
        """

        requested_stats = {
            VIRUS_TOKEN_TO_STAT[token]
            for token in (str(item or "").strip() for item in tokens)
            if token in VIRUS_TOKEN_TO_STAT
        }
        if not requested_stats:
            return ()
        imported: list[str] = []
        with self._lock:
            progress = self._progress_for_identity_locked(identity)
            for stat in VIRUS_STATS:
                if stat not in requested_stats:
                    continue
                if float(progress.stats.get(stat, 0.0)) == 0.0:
                    progress.stats[stat] = 1.0
                    imported.append(stat)
            if imported:
                self._save_locked()
        return tuple(imported)

    def viral_tokens_for_profile(self, profile_id: int) -> tuple[str, ...]:
        """Return DOBJ entitlement tokens derived from persistent viral stats."""

        with self._lock:
            progress = self._progress_for_profile_locked(int(profile_id))
            if progress is None:
                return ()
            tokens = tuple(
                VIRUS_STAT_TO_TOKEN[stat]
                for stat in VIRUS_STATS
                if float(progress.stats.get(stat, 0.0)) != 0.0
            )
        if len(tokens) == len(VIRUS_STATS):
            return (*tokens, CARBON_PLAGUE_TOKEN)
        return tokens

    def apply_rank_update(
        self,
        profile_id: int,
        stat: str,
        value: float,
        *,
        update_type: int,
        trusted_server: bool,
        text: str = "",
    ) -> bool:
        """Apply a RankingService update while protecting server-managed flags.

        Carbon uses update type 0 for assignment, 1 for minimum, 2 for maximum,
        and 3 for increment.  Event leaderboard rows attach a comma-separated
        metadata string to the winning value.  Server-managed unlocks are
        accepted only from a FESL connection which identified itself as
        ``clientType=server``.  The ``Moderator`` role is never writable through
        RankingService.
        """
        clean_stat = self._clean_stat(stat)
        if not clean_stat or clean_stat == MODERATOR_STAT:
            return False
        if clean_stat in SERVER_MANAGED_STATS and not trusted_server:
            return False
        numeric = float(value)
        clean_text = str(text or "")
        with self._lock:
            progress = self._progress_for_profile_locked(int(profile_id))
            if progress is None:
                return False
            had_old = clean_stat in progress.stats
            old = float(progress.stats.get(clean_stat, 0.0))
            operation = int(update_type)
            old_text = progress.stat_texts.get(clean_stat, "")
            # Builds before textual leaderboard persistence left valid-looking
            # Evt_Bst values without the car/lap/speed payload Carbon needs.
            # The first complete client update replaces such an incomplete row
            # so it can become displayable without manually deleting old stats.
            incomplete_text_row = had_old and bool(clean_text) and not old_text
            if operation == 1 and had_old and not incomplete_text_row:
                new = min(old, numeric)
            elif operation == 2 and had_old and not incomplete_text_row:
                new = max(old, numeric)
            elif operation == 3:
                new = old + numeric
            else:
                new = numeric
            value_changed = not had_old or old != new
            update_won = not had_old or operation not in (1, 2) or new == numeric
            if incomplete_text_row:
                update_won = True
            text_changed = bool(clean_text) and update_won and old_text != clean_text
            if not value_changed and not text_changed:
                return False
            progress.stats[clean_stat] = new
            if clean_text and update_won:
                progress.stat_texts[clean_stat] = clean_text
            self._save_locked()
            return True

    def record_authoritative_race(
        self,
        participants: Sequence[Identity],
        *,
        event_type: int,
        rankings: Mapping[int, int],
        finished_profile_ids: Iterable[int],
        ranked: bool,
    ) -> RankedRaceProgression:
        """Persist one server-arbitrated race and original ranked progression.

        Ranking/winner input must come from the rebroadcaster's result tracker,
        never directly from a client-supplied ``ranking`` field.
        """
        unique = {identity.profile_id: identity for identity in participants}
        finished = {int(item) for item in finished_profile_ids} & unique.keys()
        normalized_ranks = {
            profile_id: int(rankings.get(profile_id, 0))
            for profile_id in unique
        }
        winners = {
            profile_id for profile_id, rank in normalized_ranks.items()
            if rank == 1 and profile_id in finished
        }
        awards = self.award_race(
            tuple(unique.values()),
            event_type=int(event_type),
            winners=winners,
            ranked=bool(ranked),
        )

        skill_levels: dict[int, float] = {}
        rep_awards: dict[int, float] = {}
        with self._lock:
            progresses = {
                profile_id: self._progress_for_identity_locked(identity)
                for profile_id, identity in unique.items()
            }
            for profile_id, progress in progresses.items():
                progress.stats[TOTAL_GAMES_STARTED_STAT] = (
                    float(progress.stats.get(TOTAL_GAMES_STARTED_STAT, 0.0)) + 1.0
                )
                if profile_id in finished:
                    progress.stats[TOTAL_GAMES_FINISHED_STAT] = (
                        float(progress.stats.get(TOTAL_GAMES_FINISHED_STAT, 0.0)) + 1.0
                    )
                else:
                    progress.stats[DNF_LOSSES_STAT] = (
                        float(progress.stats.get(DNF_LOSSES_STAT, 0.0)) + 1.0
                    )

            ordered = sorted(
                unique,
                key=lambda profile_id: (
                    normalized_ranks.get(profile_id, 0) <= 0,
                    normalized_ranks.get(profile_id, 0) or 999,
                    profile_id,
                ),
            )
            if ranked and len(ordered) >= 2:
                old_skills = [
                    float(progresses[profile_id].stats.get(SKILL_LEVEL_STAT, DEFAULT_SKILL_LEVEL))
                    for profile_id in ordered
                ]
                fallback_rank = len(ordered)
                rank_values = [
                    normalized_ranks.get(profile_id, 0) or fallback_rank
                    for profile_id in ordered
                ]
                new_skills = calculate_skill_levels(old_skills, rank_values)
                for profile_id, new_skill in zip(ordered, new_skills):
                    progresses[profile_id].stats[SKILL_LEVEL_STAT] = float(new_skill)
                    skill_levels[profile_id] = float(new_skill)
                    rep = calculate_rep_score(
                        len(ordered),
                        normalized_ranks.get(profile_id, 0),
                        finished=profile_id in finished,
                    )
                    if rep:
                        progresses[profile_id].stats[ONLINE_REP_STAT] = (
                            float(progresses[profile_id].stats.get(ONLINE_REP_STAT, 0.0))
                            + rep
                        )
                    rep_awards[profile_id] = rep
            self._save_locked()
        return RankedRaceProgression(
            awards=awards,
            ranked=bool(ranked),
            skill_levels=skill_levels,
            rep_awards=rep_awards,
        )

    def award_race(
        self,
        participants: Sequence[Identity],
        *,
        event_type: int,
        winners: Iterable[int | Identity],
        ranked: bool = True,
    ) -> RaceAwards:
        """Apply original Carbon viral and EA-moderator race rewards.

        ``winners`` may contain profile ids or ``Identity`` objects.  The
        original code grants the selected viral flag to every participant when
        at least one participant already owns it.  If any participant is an EA
        moderator in a ranked race, each winner receives ``Beat_Moderator``.
        """
        unique: dict[int, Identity] = {identity.profile_id: identity for identity in participants}
        winner_ids = {
            item.profile_id if isinstance(item, Identity) else int(item)
            for item in winners
        }
        viral_stat = VIRUS_BY_EVENT_TYPE.get(int(event_type))
        viral_recipients: list[int] = []
        carbon_plague_recipients: list[int] = []
        beat_recipients: list[int] = []
        with self._lock:
            progresses = {
                profile_id: self._progress_for_identity_locked(identity)
                for profile_id, identity in unique.items()
            }
            has_moderator = bool(ranked) and any(
                EA_MODERATOR_ROLE in progress.roles
                for progress in progresses.values()
            )
            virus_present = bool(
                viral_stat
                and any(float(progress.stats.get(viral_stat, 0.0)) != 0.0 for progress in progresses.values())
            )
            already_had_plague = {
                profile_id
                for profile_id, progress in progresses.items()
                if all(
                    float(progress.stats.get(stat, 0.0)) != 0.0
                    for stat in VIRUS_STATS
                )
            }
            changed = False
            if viral_stat and virus_present:
                for profile_id, progress in progresses.items():
                    if float(progress.stats.get(viral_stat, 0.0)) == 0.0:
                        progress.stats[viral_stat] = 1.0
                        viral_recipients.append(profile_id)
                        changed = True
            now_has_plague = {
                profile_id
                for profile_id, progress in progresses.items()
                if all(
                    float(progress.stats.get(stat, 0.0)) != 0.0
                    for stat in VIRUS_STATS
                )
            }
            carbon_plague_recipients.extend(
                sorted(now_has_plague - already_had_plague)
            )
            if has_moderator:
                for profile_id in sorted(winner_ids & unique.keys()):
                    progress = progresses[profile_id]
                    if float(progress.stats.get(BEAT_MODERATOR_STAT, 0.0)) == 0.0:
                        progress.stats[BEAT_MODERATOR_STAT] = 1.0
                        beat_recipients.append(profile_id)
                        changed = True
            if changed:
                self._save_locked()
        return RaceAwards(
            event_type=int(event_type),
            viral_stat=viral_stat if virus_present else None,
            viral_recipients=tuple(sorted(viral_recipients)),
            carbon_plague_recipients=tuple(carbon_plague_recipients),
            beat_moderator_recipients=tuple(sorted(beat_recipients)),
        )
