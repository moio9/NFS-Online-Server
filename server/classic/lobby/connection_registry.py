"""Live Classic lobby connection registry and fan-out helpers."""

from __future__ import annotations

from threading import RLock

from classic.lobby.models import ClassicPreloginContext


class ClassicConnectionRegistryMixin:
    """Track authenticated lobby sockets by durable user identity."""

    def _init_connection_registry(self) -> None:
        self._connections_lock = RLock()
        self._connections: dict[int, ClassicPreloginContext] = {}
        self._participant_aux: dict[int, str] = {}

    @staticmethod
    def _user_id(context: ClassicPreloginContext) -> int:
        identity = context.auth.identity
        return int(identity.user_id) if identity is not None else 0

    def _register(self, context: ClassicPreloginContext) -> None:
        user_id = self._user_id(context)
        if not user_id or context.send_wire is None:
            return
        with self._connections_lock:
            self._connections[user_id] = context

        if not self._is_most_wanted:
            return

        restored_userset = None
        with self._usersets_lock:
            candidates = [
                userset
                for userset in self._usersets.values()
                if userset.members is not None and user_id in userset.members
            ]
            if candidates:
                restored_userset = max(
                    candidates,
                    key=lambda userset: (bool(userset.game_id), userset.userset_id),
                )

        if restored_userset is None:
            return

        context.userset_id = restored_userset.userset_id
        game = self.sessions.get_game(restored_userset.game_id)
        if game is not None and user_id in game.participants:
            context.lobby_game_id = game.game_id

    def _context_for_user(self, user_id: int) -> ClassicPreloginContext | None:
        with self._connections_lock:
            return self._connections.get(int(user_id))

    def _send_users(
        self,
        user_ids: set[int],
        frames: tuple[bytes, ...],
        *,
        exclude: int = 0,
    ) -> None:
        with self._connections_lock:
            recipients = [
                context
                for user_id, context in self._connections.items()
                if user_id in user_ids and user_id != int(exclude)
            ]
        for recipient in recipients:
            sender = recipient.send_wire
            if sender is None:
                continue
            for frame in frames:
                if not sender(frame):
                    break
