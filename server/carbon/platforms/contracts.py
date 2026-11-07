"""Shared interface for platform-dependent session conventions."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SessionPolicy:
    use_account_profile_id: bool
    wait_for_self_join_ack: bool
    defer_peer_publications: bool
    permit_clear_loopback_ticket: bool
