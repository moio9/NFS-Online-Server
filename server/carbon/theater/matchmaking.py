"""Carbon PlayNow request parsing, property resolution and fit scoring.

The retail client uses two distinct operations:

* ``findServer`` searches existing dedicated sessions using set-valued filters;
* ``resetServer`` allocates a new dedicated session using concrete values.

Search filters such as ``game_type=0|2`` must never be published in GDAT.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


PLAY_NOW_PREFIX = "players.0.props."

RACE_PREFERENCES = (
    "race_type_circuit",
    "race_type_knockout",
    "race_type_speedtrap",
    "race_type_pursuit_tag",
    "race_type_canyon_due",
    "race_type_sprint",
)

GAME_MODE_RACE_PROPERTY: dict[str, str] = {
    "0": "race_type_sprint",
    "1": "race_type_circuit",
    "5": "race_type_speedtrap",
    "13": "race_type_canyon_due",
    "14": "race_type_pursuit_tag",
    "15": "race_type_knockout",
}
RACE_PROPERTY_GAME_MODE: dict[str, str] = {
    race_property: game_mode
    for game_mode, race_property in GAME_MODE_RACE_PROPERTY.items()
}


def selected_race_property(
    properties: dict[str, str],
    *,
    prefix: str = "",
) -> str | None:
    """Resolve the concrete race field, tolerating a stale game_mode value.

    Challenge setup can briefly carry the generic Sprint/Circuit game-mode
    default while exactly one race_type_* field already names the selected
    event.  The concrete event is authoritative in that case.
    """

    selected = [
        race_property
        for race_property in GAME_MODE_RACE_PROPERTY.values()
        if str(properties.get(f"{prefix}{race_property}", "")).strip().upper()
        not in {"", "ABSTAIN"}
    ]
    preferred = GAME_MODE_RACE_PROPERTY.get(
        str(properties.get(f"{prefix}game_mode", "")).strip()
    )
    if preferred in selected:
        return preferred
    if len(selected) == 1:
        return selected[0]
    return None


def selected_challenge_event(
    properties: Mapping[str, object],
    *,
    prefix: str = "",
) -> tuple[str, str] | None:
    """Return the single concrete ``cs.*`` event in a Challenge snapshot.

    Carbon can briefly retain a normal online event while switching from a
    local/Unranked session into Career Challenge Assist.  Such values (for
    example ``mu.2.2``) must never become the authoritative Challenge event.
    Accept exactly one Challenge event and ignore unrelated stale race slots.
    """

    selected: list[tuple[str, str]] = []
    for race_property in RACE_PREFERENCES:
        value = str(properties.get(f"{prefix}{race_property}", "") or "").strip()
        if value.casefold().startswith("cs."):
            selected.append((race_property, value))
    return selected[0] if len(selected) == 1 else None

STRING_PREFERENCES = (
    "car_tier",
    "collision_detection",
    "game_mode",
    "help_type",
    "length",
    "n2o",
    *RACE_PREFERENCES,
    "team_play",
)

INTEGER_PREFERENCES = (
    "max_online_player",
    "player_dnf",
    "skill",
)

# Stable concrete choices used only when the client votes ABSTAIN while asking
# resetServer to resolve an unused dedicated server.  These are the values in
# the official ranked PlayNow example rather than the old single sprint-only
# fixture.
DEDICATED_DEFAULTS: dict[str, str] = {
    "game_mode": "1",
    "help_type": "0",
    "car_tier": "1",
    "collision_detection": "1",
    "n2o": "1",
    "length": "1",
    "team_play": "1",
    "race_type_circuit": "ex.5.1",
    "race_type_knockout": "qr.5.1",
    "race_type_speedtrap": "mu.2.2",
    "race_type_pursuit_tag": "qr.6.1",
    "race_type_canyon_due": "qr.3.3",
    "race_type_sprint": "ct.4.2",
}

# Initial retail identity of a dedicated Career/Challenge Assist room. Search
# preferences such as help_type=1/2/3 remain private matchmaking hints. The
# Initial game_mode=0 matches the captured GameManager Sprint Challenge
# identity.  It is only the neutral allocation default: once the host publishes
# a concrete Challenge event, GameManager retains that event's native mode and
# race_type_* slot (for example Circuit mode 1 or Speedtrap mode 5).
CHALLENGE_ROOM_IDENTITY: dict[str, str] = {
    "game_type": "2",
    "matchmaking_state": "0",
    "help_type": "0",
    "game_mode": "0",
    "max_online_player": "2",
    "skill": "",
    "player_dnf": "",
    "team_play": "1",
}
CHALLENGE_ROOM_IDENTITY_PROPERTIES: dict[str, str] = {
    f"B-U-{name}": value
    for name, value in CHALLENGE_ROOM_IDENTITY.items()
}


@dataclass(frozen=True)
class PlayNowRequest:
    session_type: str
    allowed_game_types: frozenset[str]
    concrete_game_type: str | None
    matchmaking_states: frozenset[str]
    version: str
    fit_threshold: float
    requested_help_type: str

    @property
    def is_find(self) -> bool:
        return self.session_type == "findserver"

    @property
    def creates_dedicated(self) -> bool:
        return self.session_type in {
            "resetserver",
            "createserver",
            "createsession",
            "hostserver",
        }


def _value(fields: dict[str, str], *keys: str, default: str = "") -> str:
    for key in keys:
        value = fields.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return str(default)


def preference_key(kind: str, name: str) -> str:
    return f"{PLAY_NOW_PREFIX}{{{kind}-{name}}}"


def split_values(value: str) -> frozenset[str]:
    return frozenset(
        item.strip()
        for item in str(value).split("|")
        if item.strip() and item.strip().casefold() != "abstain"
    )


def concrete(value: str, default: str = "") -> str:
    candidate = str(value).strip()
    if not candidate or "|" in candidate or candidate.casefold() in {"abstain", "random"}:
        return str(default)
    return candidate


def parse_threshold(value: str, default: float = 0.0) -> float:
    """Read the first PlayNow threshold entry (EA uses a 0..10000 scale)."""
    text = str(value).strip()
    if not text:
        return float(default)
    first = text.split(",", 1)[0].strip()
    if ":" in first:
        first = first.split(":", 1)[1].strip()
    try:
        parsed = float(first)
    except ValueError:
        return float(default)
    if parsed > 1.0:
        parsed /= 10_000.0
    return min(1.0, max(0.0, parsed))


def parse_request(fields: dict[str, str]) -> PlayNowRequest:
    session_type = _value(
        fields,
        f"{PLAY_NOW_PREFIX}{{sessionType}}",
        "sessionType",
    ).casefold()
    raw_types = _value(
        fields,
        preference_key("filter", "game_type"),
        "B-U-game_type",
    )
    allowed_types = split_values(raw_types)
    concrete_type = concrete(raw_types)
    if not raw_types and session_type in {
        "resetserver",
        "createserver",
        "createsession",
        "hostserver",
    }:
        # playnowconfig.txt defines ranked (0) as the filter default.  Retail
        # normally sends the field explicitly, but retain the official default
        # for incomplete diagnostic requests.
        concrete_type = "0"
        allowed_types = frozenset({"0"})
    elif concrete_type not in {"0", "1", "2"}:
        concrete_type = None
    raw_states = _value(
        fields,
        preference_key("filter", "matchmaking_state"),
        "B-U-matchmaking_state",
    )
    states = split_values(raw_states)
    version = _value(
        fields,
        preference_key("filter", "version"),
        "B-U-version",
        "B-version",
    )
    threshold = parse_threshold(
        _value(fields, f"{PLAY_NOW_PREFIX}{{fitThreshold}}"),
        default=0.0,
    )
    requested_help_type = concrete(
        _value(
            fields,
            "B-U-help_type",
            preference_key("pref", "help_type"),
            preference_key("filter", "help_type"),
        )
    )
    return PlayNowRequest(
        session_type=session_type,
        allowed_game_types=allowed_types,
        concrete_game_type=concrete_type,
        matchmaking_states=states,
        version=version,
        fit_threshold=threshold,
        requested_help_type=requested_help_type,
    )


def _requested_preference(fields: dict[str, str], name: str, default: str = "") -> str:
    return _value(
        fields,
        f"B-U-{name}",
        preference_key("pref", name),
        preference_key("filter", name),
        default=default,
    )


def resolved_dedicated_properties(fields: dict[str, str], request: PlayNowRequest) -> dict[str, str]:
    """Resolve one concrete resetServer request into publishable GDAT fields."""
    game_type = request.concrete_game_type
    if game_type not in {"0", "1", "2"}:
        raise ValueError("dedicated PlayNow creation requires concrete game_type 0, 1 or 2")

    version = request.version or "298_prod_server+22012b18"
    state = next(iter(request.matchmaking_states), "1")
    if state not in {"0", "1"}:
        state = "1"

    properties: dict[str, str] = {
        "B-version": version,
        "B-U-version": version,
        "B-U-game_type": game_type,
        "B-U-matchmaking_state": state,
        "B-U-track": "",
    }

    for name in STRING_PREFERENCES:
        default = DEDICATED_DEFAULTS[name]
        resolved = concrete(_requested_preference(fields, name), default)
        # Retail uses help_type=1/2/3 only as a PlayNow compatibility hint.
        # Once a game_type=2 Career/Challenge Assist room is allocated, both
        # GDAT and the authoritative 0x1D15 publish help_type=0.  The original
        # request value is retained separately by the directory matcher.
        if game_type == "2" and name == "help_type":
            resolved = "0"
        properties[f"B-U-{name}"] = resolved

    max_players = concrete(_requested_preference(fields, "max_online_player"), "8")
    try:
        parsed_max = int(max_players)
    except ValueError:
        parsed_max = 8
    # The available retail directory captures begin with a two-player neutral
    # requester/helper allocation. The stock host's first concrete Challenge
    # GameAttributes later replaces this with the actual event capacity.
    properties["B-U-max_online_player"] = (
        "2" if game_type == "2" else str(min(8, max(2, parsed_max)))
    )

    if game_type == "2":
        # Both official Challenge invite captures publish these fields as
        # empty strings in GDAT and in the late-join 0x1D15 snapshot:
        #
        #   attribute 05 (skill)      = 05 0000
        #   attribute 0d (player_dnf) = 0d 0000
        #
        # Applying the ranked resetServer averaging rule here produced
        # skill=500/player_dnf=0 and made the helper's room signature differ
        # from the native Challenge room before its frontend transition.
        properties.update(CHALLENGE_ROOM_IDENTITY_PROPERTIES)

        # A Challenge allocation has no authoritative event until the host's
        # GameManager snapshot names one concrete cs.* entry.  Resolving every
        # ABSTAIN vote through DEDICATED_DEFAULTS previously produced a mixed
        # room containing Circuit, Speedtrap, Sprint, Knockout and Pursuit
        # events at once.  Fast clients replaced it quickly; slower clients
        # rendered that stale normal-race context or crashed while resolving it.
        requested_races = {
            name: _requested_preference(fields, name)
            for name in RACE_PREFERENCES
        }
        challenge_event = selected_challenge_event(requested_races)
        for name in RACE_PREFERENCES:
            properties[f"B-U-{name}"] = "ABSTAIN"
        if challenge_event is not None:
            race_property, event = challenge_event
            properties[f"B-U-{race_property}"] = event
            properties["B-U-game_mode"] = RACE_PROPERTY_GAME_MODE[race_property]
        properties["B-U-track"] = ""
    else:
        # The official ranked resetServer example resolves an unused server
        # plus one player preference: skill 1000 -> 500 and DNF 25 -> 12.
        for name, default in (("skill", 1000), ("player_dnf", 0)):
            raw = concrete(_requested_preference(fields, name), str(default))
            try:
                requested = int(float(raw))
            except ValueError:
                requested = default
            properties[f"B-U-{name}"] = str(max(0, requested // 2))

    return properties

def _strip_quotes(value: str) -> str:
    text = str(value).strip()
    if len(text) >= 2 and text[0] == text[-1] == '"':
        return text[1:-1]
    return text


def _parse_fit_values(value: str) -> list[str]:
    return [item.strip() for item in _strip_quotes(value).split(",") if item.strip()]


def _parse_fit_table(value: str) -> list[list[float]]:
    table: list[list[float]] = []
    for row in str(value).split("|"):
        parsed_row: list[float] = []
        for item in row.split(";"):
            try:
                parsed_row.append(float(item.strip()))
            except ValueError:
                parsed_row.append(0.0)
        table.append(parsed_row)
    return table


def _float_field(fields: dict[str, str], key: str, default: float) -> float:
    try:
        return float(str(fields.get(key, default)).strip())
    except (TypeError, ValueError):
        return float(default)


def _string_preference_fit(fields: dict[str, str], properties: dict[str, str], name: str) -> float | None:
    requested = fields.get(preference_key("pref", name))
    if requested is None:
        return 1.0
    game_value = properties.get(f"B-U-{name}")
    if game_value is None:
        # Location selects the EA data center and is not present in retail GDAT.
        return 1.0

    values = _parse_fit_values(fields.get(preference_key("fitValues", name), ""))
    table = _parse_fit_table(fields.get(preference_key("fitTable", name), ""))
    if not values or not table:
        if str(requested).casefold() == "abstain" or str(game_value).casefold() == "abstain":
            return 1.0
        return 1.0 if str(requested) == str(game_value) else 0.0
    try:
        request_index = values.index(str(requested))
        game_index = values.index(str(game_value))
    except ValueError:
        return 0.0
    if request_index >= len(table) or game_index >= len(table[request_index]):
        return 0.0
    score = table[request_index][game_index]
    return None if score < 0.0 else min(1.0, max(0.0, score))


def _integer_preference_fit(fields: dict[str, str], properties: dict[str, str], name: str) -> float:
    requested = fields.get(preference_key("pref", name))
    game_value = properties.get(f"B-U-{name}")
    if requested is None or game_value is None:
        return 1.0
    try:
        requested_number = float(requested)
        game_number = float(game_value)
    except ValueError:
        return 0.0
    scale = max(1.0, _float_field(fields, preference_key("fitScale", name), 1.0))
    return min(1.0, max(0.0, 1.0 - abs(requested_number - game_number) / scale))


def match_fit(
    fields: dict[str, str],
    properties: dict[str, str],
    *,
    reject_incompatible: bool = True,
) -> float | None:
    """Return weighted 0..1 compatibility or ``None`` for a forbidden pair."""
    total_weight = 0.0
    weighted_fit = 0.0

    for name in STRING_PREFERENCES:
        if preference_key("pref", name) not in fields:
            continue
        fit = _string_preference_fit(fields, properties, name)
        if fit is None:
            if reject_incompatible:
                return None
            fit = 0.0
        weight = max(0.0, _float_field(fields, preference_key("fitWeight", name), 1.0))
        total_weight += weight
        weighted_fit += fit * weight

    for name in INTEGER_PREFERENCES:
        if preference_key("pref", name) not in fields:
            continue
        fit = _integer_preference_fit(fields, properties, name)
        weight = max(0.0, _float_field(fields, preference_key("fitWeight", name), 1.0))
        total_weight += weight
        weighted_fit += fit * weight

    return weighted_fit / total_weight if total_weight > 0.0 else 1.0



def is_direct_coop_helper_reset(fields: dict[str, str], request: PlayNowRequest) -> bool:
    """Identify Carbon's second helper-side co-op PlayNow form.

    Besides ``findServer game_type=1|2``, retail can issue a direct
    ``resetServer`` with concrete Unranked type 1, state 0 and ABSTAIN event
    preferences after it has discovered a waiting Career/Challenge Assist
    room through Theater GLST.  In that form the request carries no explicit
    game_type 2/help_type 1|2 marker; the dedicated pool is expected to pair
    it with an already active co-op requester before allocating a new room.

    Keep the signature narrow so a real Custom Match reset with a concrete
    mode or race selection still allocates its own unranked/ranked room.
    """
    if request.session_type != "resetserver":
        return False
    if request.concrete_game_type != "1":
        return False
    if request.matchmaking_states and request.matchmaking_states != frozenset({"0"}):
        return False

    help_type = _requested_preference(fields, "help_type")
    if concrete(help_type) not in {"", "0"}:
        return False

    game_mode = _requested_preference(fields, "game_mode")
    if concrete(game_mode):
        return False

    # A direct helper reset is deliberately event-agnostic.  Any concrete
    # track/mode vote means this is a genuine room allocation request.
    for name in RACE_PREFERENCES:
        if concrete(_requested_preference(fields, name)):
            return False
    return True

def is_coop_state_bridge(request: PlayNowRequest, properties: dict[str, str]) -> bool:
    """Return whether a normal PlayNow search may enter a waiting co-op room.

    Carbon uses asymmetric filters for Challenge/Career Assist:

    * the requester allocates ``game_type=2`` with ``matchmaking_state=0``;
    * an Unranked helper searches ``game_type=1|2`` with
      ``matchmaking_state=1``.

    Therefore state 0 is not a closed room for game type 2. It is the waiting
    requester side of the co-op bridge. The high-weight ``help_type`` fit table
    still decides Career/Challenge compatibility.
    """
    return (
        str(properties.get("B-U-game_type", "")) == "2"
        and "2" in request.allowed_game_types
        and "1" in request.allowed_game_types
        and "0" not in request.allowed_game_types
        and str(properties.get("B-U-matchmaking_state", "")) == "0"
        and "1" in request.matchmaking_states
    )


def strict_match(request: PlayNowRequest, properties: dict[str, str]) -> bool:
    game_type = str(properties.get("B-U-game_type", ""))
    if request.allowed_game_types and game_type not in request.allowed_game_types:
        return False
    state = str(properties.get("B-U-matchmaking_state", ""))
    if (
        request.matchmaking_states
        and state not in request.matchmaking_states
        and not is_coop_state_bridge(request, properties)
    ):
        return False
    if request.version and str(properties.get("B-U-version", "")) != request.version:
        return False
    return True
