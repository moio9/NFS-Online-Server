"""Client-aware endpoint projection for the Classic lobby.

This boundary owns configured control/web endpoints, race-relay publication and
LAN/public address selection.  It changes only the endpoint presented to a
viewer; lobby state and wire ordering remain owned by their existing domains.
"""

from __future__ import annotations

from typing import Callable

from classic.core.config import Endpoint
from classic.ea.directory import GameSession


class ClassicEndpointMixin:
    """Own endpoint configuration and viewer-specific address projection."""

    def _init_endpoints(
        self,
        control_endpoint: Endpoint,
        web_endpoint: Endpoint | None,
    ) -> None:
        self.control_endpoint = control_endpoint
        self.web_endpoint = web_endpoint or control_endpoint
        # Transport-only endpoint projection.  This does not alter lobby state
        # or packet ordering; it only prevents local/LAN clients from receiving
        # an unreachable public address.
        self.endpoint_resolver: Callable[[Endpoint, str], Endpoint] | None = None
        self.race_endpoint: Endpoint | None = None
        self.race_endpoints: tuple[Endpoint, ...] = ()
        self.race_registrar: Callable[[GameSession], dict[int, str]] | None = None
        self.race_unregistrar: Callable[[GameSession], bool] | None = None
        self.race_handoff: (
            Callable[[GameSession, GameSession], dict[int, str] | None] | None
        ) = None

    def set_control_endpoint(self, endpoint: Endpoint) -> None:
        self.control_endpoint = endpoint

    def set_web_endpoint(self, endpoint: Endpoint) -> None:
        self.web_endpoint = endpoint

    def set_endpoint_resolver(
        self, resolver: Callable[[Endpoint, str], Endpoint] | None
    ) -> None:
        self.endpoint_resolver = resolver

    def _endpoint_for_client(self, endpoint: Endpoint, client_ip: str) -> Endpoint:
        resolver = self.endpoint_resolver
        return resolver(endpoint, client_ip) if resolver is not None else endpoint

    def _race_endpoint_for_viewer(
        self, endpoint: Endpoint | None, viewer_id: int
    ) -> Endpoint | None:
        if endpoint is None:
            return None
        viewer = self._context_for_user(int(viewer_id)) if viewer_id else None
        # Endpoint reachability must be classified from the TCP peer address.
        # ``client_address`` is game-supplied state and can be overwritten by
        # ADDR with a private/LAN address even when the peer is remote.
        client_ip = viewer.auth.client_ip if viewer is not None else ""
        return self._endpoint_for_client(endpoint, client_ip)

    def _race_endpoint_for_participant(
        self,
        game: GameSession,
        viewer_id: int,
    ) -> Endpoint | None:
        """Return the public relay endpoint advertised to one participant.

        Current MW and U2 wrappers carry enough virtual identity to share the
        base public endpoint. Other profiles retain the historical owner-first
        channel selection below.
        """

        if not self.race_endpoints:
            return self._race_endpoint_for_viewer(self.race_endpoint, viewer_id)
        local_id = int(viewer_id or game.owner_id)
        if self._is_most_wanted or self._is_underground2:
            # The current ASI wrappers carry the virtual peer address. U2 also
            # carries its viewer-local virtual source identity, so every
            # participant can safely share the base Internet-facing port.
            # Legacy channel listeners remain available for rollback clients.
            return self._race_endpoint_for_viewer(self.race_endpoints[0], local_id)
        else:
            wire_ids = getattr(game, "participant_wire_ids", {})
            ordered = sorted(
                game.participants,
                key=lambda user_id: (
                    user_id != game.owner_id,
                    int(wire_ids.get(int(user_id), 0) or 0) or (1 << 32),
                    user_id,
                ),
            )
        try:
            endpoint_index = ordered.index(local_id)
        except ValueError:
            endpoint_index = 0
        endpoint = self.race_endpoints[
            min(endpoint_index, len(self.race_endpoints) - 1)
        ]
        return self._race_endpoint_for_viewer(endpoint, local_id)

    def set_race_relay(
        self,
        endpoint: Endpoint,
        registrar: Callable[[GameSession], dict[int, str]],
        *additional_endpoints: Endpoint,
        unregistrar: Callable[[GameSession], bool] | None = None,
        handoff: (
            Callable[[GameSession, GameSession], dict[int, str] | None] | None
        ) = None,
    ) -> None:
        self.race_endpoint = endpoint
        self.race_endpoints = (endpoint, *additional_endpoints)
        self.race_registrar = registrar
        self.race_unregistrar = unregistrar
        self.race_handoff = handoff
