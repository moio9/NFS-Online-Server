"""Game-neutral EA room/game state; adapters decide their own wire records."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from threading import RLock


class Visibility(str, Enum):
    PUBLIC = "PUBLIC"
    PRIVATE = "PRIVATE"


class SessionState(str, Enum):
    OPEN = "OPEN"
    ACTIVE = "ACTIVE"
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

    def can_join(self, user_id: int, password: str = "") -> bool:
        if user_id in self.participants or user_id == self.owner_id:
            return True
        if self.state is not SessionState.OPEN or len(self.participants) >= self.capacity:
            return False
        if self.visibility is Visibility.PRIVATE and not self.password:
            return False
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
    ) -> GameSession:
        if capacity < 1 or min_players < 1 or min_players > capacity:
            raise ValueError("invalid game capacity")
        with self._lock:
            game = GameSession(
                self._next_game_id,
                int(room_id),
                int(owner_id),
                int(capacity),
                int(min_players),
                Visibility(visibility),
                str(password),
                SessionState.OPEN,
                {int(owner_id)} if include_owner else set(),
            )
            self._games[game.game_id] = game
            self._next_game_id += 1
            return game

    def get_game(self, game_id: int) -> GameSession | None:
        with self._lock:
            return self._games.get(int(game_id))

    def close_game(self, game_id: int) -> bool:
        """Explicitly retire a game even when it never gained participants."""
        with self._lock:
            game = self._games.pop(int(game_id), None)
            if game is None:
                return False
            game.state = SessionState.CLOSED
            game.participants.clear()
            return True

    def list_games(self) -> list[GameSession]:
        with self._lock:
            return list(self._games.values())

    def join_game(self, game_id: int, user_id: int, password: str = "") -> bool:
        with self._lock:
            game = self._games.get(int(game_id))
            if game is None or not game.can_join(int(user_id), password):
                return False
            game.participants.add(int(user_id))
            return True

    def resize_game(self, game_id: int, capacity: int) -> bool:
        """Resize an open game without evicting existing participants."""

        target = int(capacity)
        with self._lock:
            game = self._games.get(int(game_id))
            if (
                game is None
                or target < max(1, int(game.min_players))
                or target < len(game.participants)
            ):
                return False
            game.capacity = target
            return True

    def leave_game(self, game_id: int, user_id: int) -> bool:
        with self._lock:
            game = self._games.get(int(game_id))
            if game is None or int(user_id) not in game.participants:
                return False
            game.participants.remove(int(user_id))
            if not game.participants:
                game.state = SessionState.CLOSED
                self._games.pop(game.game_id, None)
            return True
