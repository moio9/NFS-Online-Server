"""Observed and experimental Xbox-specific session conventions.

The self-join barrier and peer deferral are existing compatibility behavior,
not independently proven reproductions of the original Xbox service.
"""

from carbon.platforms.contracts import SessionPolicy

POLICY = SessionPolicy(
    use_account_profile_id=True,
    wait_for_self_join_ack=True,
    defer_peer_publications=True,
    permit_clear_loopback_ticket=True,
)
