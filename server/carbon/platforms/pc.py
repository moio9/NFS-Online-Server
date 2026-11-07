"""Capture-derived PC session conventions and the legacy unknown default."""

from carbon.platforms.contracts import SessionPolicy

POLICY = SessionPolicy(
    use_account_profile_id=False,
    wait_for_self_join_ack=False,
    defer_peer_publications=False,
    permit_clear_loopback_ticket=False,
)
