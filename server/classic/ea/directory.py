"""Game-neutral EA room/game state; adapters decide their own wire records."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from threading import RLock
import time


class Visibility(str, Enum):
    PUBLIC = "PUBLIC"
    PRIVATE = "PRIVATE"


class SessionState(str, Enum):
    OPEN = "OPEN"
    ACTIVE = "ACTIVE"
    FINISHED = "FINISHED"
    CLOSED = "CLOSED"


@dataclass
class Room:
    room_id: int
    owner_id: int
    name: str
    capacity: int = 8
    min_players: int = 2
    visibility: Visibility = Visibility.PUBLIC
    password: str = ""
    members: set[int] = field(default_factory=set)

    def visible_to(self, user_id: int) -> bool:
        return self.visibility is Visibility.PUBLIC or user_id in self.members or user_id == self.owner_id

    def can_join(self, user_id: int, password: str = "") -> bool:
        if user_id in self.members or user_id == self.owner_id:
            return True
        if len(self.members) >= self.capacity:
            return False
        if self.visibility is Visibility.PRIVATE and not self.password:
            return False
        return not self.password or self.password == password


@dataclass
class GameSession:
    game_id: int
    room_id: int
    owner_id: int
    capacity: int = 8
    min_players: int = 2
    visibility: Visibility = Visibility.PUBLIC
    password: str = ""
    state: SessionState = SessionState.OPEN
    participants: set[int] = field(default_factory=set)
    name: str = ""
    params: str = ""
    custflags: str = "0"
    sysflags: str = "0"
    host_persona: str = ""
    host_address: str = ""
    participant_personas: dict[int, str] = field(default_factory=dict)
    participant_addresses: dict[int, str] = field(default_factory=dict)
    participant_race_addresses: dict[int, str] = field(default_factory=dict)
    participant_wire_ids: dict[int, int] = field(default_factory=dict)
    participant_order: list[int] = field(default_factory=list)
    participant_aux: dict[int, str] = field(default_factory=dict)
    ready_participants: set[int] = field(default_factory=set)
    kicked_participants: set[int] = field(default_factory=set)
    invited_participants: set[int] = field(default_factory=set)
    reported_participants: set[int] = field(default_factory=set)
    results: dict[int, dict[str, object]] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    finished_at: float = 0.0

    def ordered_participants(self) -> tuple[int, ...]:
        """Return owner-first participants without moving established slots.

        MW keeps an existing opponent in the same OPPO/ADDR slot when another
        player joins.  Wire user IDs are global lobby identities and therefore
        cannot safely double as room join order.
        """

        current = {int(user_id) for user_id in self.participants}
        ordered: list[int] = []
        owner_id = int(self.owner_id)
        if owner_id in current:
            ordered.append(owner_id)
        for user_id in self.participant_order:
            uid = int(user_id)
            if uid in current and uid not in ordered:
                ordered.append(uid)
        # Support old/persisted and test-created sessions which predate the
        # explicit join-order field.  Once observed, their remaining order is
        # anchored so a later join cannot reshuffle it.
        wire_ids = self.participant_wire_ids
        remaining = sorted(
            current - set(ordered),
            key=lambda user_id: (
                int(wire_ids.get(user_id, 0) or 0) or (1 << 32),
                user_id,
            ),
        )
        ordered.extend(remaining)
        self.participant_order = list(ordered)
        return tuple(ordered)

    def visible_to(self, user_id: int) -> bool:
        uid = int(user_id)
        return (
            self.visibility is Visibility.PUBLIC
            or bool(self.password)
            or uid == self.owner_id
            or uid in self.participants
            or uid in self.invited_participants
        )

    def can_join(self, user_id: int, password: str = "") -> bool:
        uid = int(user_id)
        if uid in self.participants or uid == self.owner_id:
            return True
        if (
            self.state is not SessionState.OPEN
            or len(self.participants) >= self.capacity
        ):
            return False
        if uid in self.invited_participants:
            return True
        if self.visibility is Visibility.PRIVATE:
            return bool(self.password) and self.password == password
        return not self.password or self.password == password


class SessionDirectory:
    """Thread-safe shared lifecycle; no game packet serialization lives here."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._next_room_id = 1
        self._next_game_id = 1
        self._rooms: dict[int, Room] = {}
        self._games: dict[int, GameSession] = {}

    def create_room(
        self,
        owner_id: int,
        name: str,
        *,
        capacity: int = 8,
        min_players: int = 2,
        visibility: Visibility = Visibility.PUBLIC,
        password: str = "",
    ) -> Room:
        if capacity < 1 or min_players < 1 or min_players > capacity:
            raise ValueError("invalid room capacity")
        with self._lock:
            room = Room(
                self._next_room_id,
                int(owner_id),
                str(name),
                int(capacity),
                int(min_players),
                Visibility(visibility),
                str(password),
                {int(owner_id)},
            )
            self._rooms[room.room_id] = room
            self._next_room_id += 1
            return room

    def join_room(self, room_id: int, user_id: int, password: str = "") -> bool:
        with self._lock:
            room = self._rooms.get(int(room_id))
            if room is None or not room.can_join(int(user_id), password):
                return False
            room.members.add(int(user_id))
            return True

    def leave_room(self, room_id: int, user_id: int) -> bool:
        with self._lock:
            room = self._rooms.get(int(room_id))
            if room is None or int(user_id) not in room.members:
                return False
            room.members.remove(int(user_id))
            if not room.members:
                self._rooms.pop(room.room_id, None)
            return True

    def visible_rooms(self, user_id: int) -> list[Room]:
        with self._lock:
            return [room for room in self._rooms.values() if room.visible_to(int(user_id))]

    def create_game(
        self,
        room_id: int,
        owner_id: int,
        *,
        capacity: int = 8,
        min_players: int = 2,
        visibility: Visibility = Visibility.PUBLIC,
        password: str = "",
        include_owner: bool = True,
        name: str = "",
        params: str = "",
        custflags: str = "0",
        sysflags: str = "0",
        host_persona: str = "",
        host_address: str = "",
    ) -> GameSession:
        if capacity < 1 or min_players < 1 or min_players > capacity:
            raise ValueError("invalid game capacity")
        with self._lock:
            game = GameSession(
                game_id=self._next_game_id,
                room_id=int(room_id),
                owner_id=int(owner_id),
                capacity=int(capacity),
                min_players=int(min_players),
                visibility=Visibility(visibility),
                password=str(password),
                state=SessionState.OPEN,
                participants={int(owner_id)} if include_owner else set(),
                name=str(name),
                params=str(params),
                custflags=str(custflags),
                sysflags=str(sysflags),
                host_persona=str(host_persona),
                host_address=str(host_address),
                participant_personas=(
                    {int(owner_id): str(host_persona)} if include_owner else {}
                ),
                participant_addresses=(
                    {int(owner_id): str(host_address)} if include_owner else {}
                ),
                participant_order=[int(owner_id)] if include_owner else [],
            )
            self._games[game.game_id] = game
            self._next_game_id += 1
            return game

    def get_game(self, game_id: int) -> GameSession | None:
        with self._lock:
            return self._games.get(int(game_id))

    def list_games(self) -> list[GameSession]:
        with self._lock:
            return list(self._games.values())

    def status_snapshot(self) -> list[dict[str, object]]:
        """Return a sanitized room/game snapshot for external status tools."""
        with self._lock:
            snapshot: list[dict[str, object]] = []
            for game in sorted(self._games.values(), key=lambda value: value.game_id):
                personas = [
                    str(game.participant_personas.get(user_id, "")).strip()
                    for user_id in sorted(game.participants)
                ]
                snapshot.append(
                    {
                        "id": game.game_id,
                        "room_id": game.room_id,
                        "name": str(game.name or ""),
                        "host": str(game.host_persona or ""),
                        "state": game.state.value,
                        "visibility": game.visibility.value,
                        "players": len(game.participants),
                        "capacity": game.capacity,
                        "personas": [value for value in personas if value],
                        "created_at": float(game.created_at),
                        "started_at": float(game.started_at or 0.0),
                        "finished_at": float(game.finished_at or 0.0),
                    }
                )
            return snapshot

    def join_game(
        self,
        game_id: int,
        user_id: int,
        password: str = "",
        *,
        persona: str = "",
        address: str = "",
    ) -> bool:
        with self._lock:
            game = self._games.get(int(game_id))
            uid = int(user_id)
            if (
                game is None
                or uid in game.kicked_participants
                or not game.can_join(uid, password)
            ):
                return False
            game.participants.add(uid)
            if uid not in game.participant_order:
                game.participant_order.append(uid)
            game.invited_participants.discard(uid)
            if persona:
                game.participant_personas[uid] = str(persona)
            if address:
                game.participant_addresses[uid] = str(address)
            return True

    def invite_user(self, game_id: int, user_id: int) -> bool:
        with self._lock:
            game = self._games.get(int(game_id))
            if game is None:
                return False
            uid = int(user_id)
            game.kicked_participants.discard(uid)
            game.invited_participants.add(uid)
            return True

    def leave_game(self, game_id: int, user_id: int, *, kicked: bool = False) -> bool:
        with self._lock:
            game = self._games.get(int(game_id))
            uid = int(user_id)
            if game is None or uid not in game.participants:
                return False
            game.participants.remove(uid)
            game.participant_personas.pop(uid, None)
            game.participant_addresses.pop(uid, None)
            game.participant_race_addresses.pop(uid, None)
            game.participant_wire_ids.pop(uid, None)
            game.participant_order = [
                participant_id
                for participant_id in game.participant_order
                if int(participant_id) != uid
            ]
            game.participant_aux.pop(uid, None)
            game.ready_participants.discard(uid)
            if kicked:
                game.kicked_participants.add(uid)
            if not game.participants:
                game.state = SessionState.CLOSED
                self._games.pop(game.game_id, None)
            return True

    def close_game(self, game_id: int) -> GameSession | None:
        with self._lock:
            game = self._games.pop(int(game_id), None)
            if game is not None:
                game.state = SessionState.CLOSED
            return game

    def set_ready(self, game_id: int, user_id: int, ready: bool) -> bool:
        with self._lock:
            game = self._games.get(int(game_id))
            uid = int(user_id)
            if game is None or uid not in game.participants:
                return False
            if ready:
                game.ready_participants.add(uid)
            else:
                game.ready_participants.discard(uid)
            return True

    def set_state(self, game_id: int, state: SessionState) -> bool:
        with self._lock:
            game = self._games.get(int(game_id))
            if game is None:
                return False
            game.state = SessionState(state)
            if game.state is SessionState.ACTIVE and not game.started_at:
                game.started_at = time.time()
            if game.state is SessionState.FINISHED and not game.finished_at:
                game.finished_at = time.time()
            return True

    def record_result(
        self,
        game_id: int,
        user_id: int,
        result: dict[str, object],
    ) -> tuple[bool, bool]:
        """Record one participant report.

        Returns ``(accepted, complete)``. Duplicate reports are ignored, and a
        game becomes FINISHED once every participant has submitted a report.
        """
        with self._lock:
            game = self._games.get(int(game_id))
            uid = int(user_id)
            if game is None or uid not in game.participants:
                return False, False
            if uid in game.reported_participants:
                return False, game.reported_participants >= game.participants
            game.reported_participants.add(uid)
            game.results[uid] = dict(result)
            complete = game.reported_participants >= game.participants
            if complete:
                game.state = SessionState.FINISHED
                game.finished_at = time.time()
            return True, complete
