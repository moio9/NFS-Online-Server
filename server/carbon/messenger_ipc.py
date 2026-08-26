"""Publish Carbon sessions and room state to the shared Messenger process."""

from __future__ import annotations

from typing import Callable

from common.ipc import AuthenticatedJSONIPCPublisher
from carbon.accounts.identity import Identity, IdentityStore
from carbon.core.config import Endpoint
from carbon.theater.directory import CarbonGameDirectory


class CarbonMessengerIPCPublisher:
    def __init__(
        self,
        endpoint: Endpoint,
        *,
        secret: str,
        identities: IdentityStore,
        games: CarbonGameDirectory,
        known_identities: Callable[[], tuple[Identity, ...]] | None = None,
        poll_interval: float = 0.1,
        heartbeat_interval: float = 1.0,
    ) -> None:
        self.identities = identities
        self.games = games
        self.known_identities = known_identities or (lambda: ())
        self.publisher = AuthenticatedJSONIPCPublisher(
            endpoint,
            secret=secret,
            snapshot_factory=self.snapshot,
            name="carbon-messenger-ipc-publisher",
            poll_interval=poll_interval,
            heartbeat_interval=heartbeat_interval,
        )

    def snapshot(self) -> dict[str, object]:
        sessions: dict[str, object] = {}
        for token, identity, wire_player_id in self.identities.active_sessions():
            sessions[token] = {
                "account_name": identity.account_name,
                "persona": identity.persona,
                "profile_id": int(identity.profile_id),
                "user_id": int(identity.user_id),
                "wire_player_id": int(wire_player_id),
            }
        forced_logoffs: dict[str, object] = {}
        for token, identity, reason, wire_player_id in self.identities.forced_logoffs():
            forced_logoffs[token] = {
                "account_name": identity.account_name,
                "persona": identity.persona,
                "profile_id": int(identity.profile_id),
                "user_id": int(identity.user_id),
                "wire_player_id": int(wire_player_id),
                "reason": reason,
            }
        known: dict[str, dict[str, object]] = {}
        for identity in self.known_identities():
            known[identity.persona.casefold()] = {
                "account_name": identity.account_name,
                "persona": identity.persona,
                "profile_id": int(identity.profile_id),
                "user_id": int(identity.user_id),
                "wire_player_id": int(self.identities.wire_player_id(identity)),
            }
        for _token, identity, wire_player_id in self.identities.active_sessions():
            known[identity.persona.casefold()] = {
                "account_name": identity.account_name,
                "persona": identity.persona,
                "profile_id": int(identity.profile_id),
                "user_id": int(identity.user_id),
                "wire_player_id": int(wire_player_id),
            }
        return {
            "game": "carbon",
            "sessions": sessions,
            "forced_logoffs": forced_logoffs,
            "known_identities": [known[key] for key in sorted(known)],
            "rooms": self.games.messenger_snapshot(),
        }

    @property
    def instance_id(self) -> str:
        return self.publisher.instance_id

    def start(self) -> None:
        self.publisher.start()

    def stop(self) -> None:
        self.publisher.stop()
