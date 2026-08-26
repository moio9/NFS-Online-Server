"""Transport-local state owned by the Carbon UDP rebroadcaster."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum

from carbon.rebroadcaster.retry import ReliableWindow, RetrySchedule


Address = tuple[str, int]
SourceKey = tuple[str, int]


@dataclass(frozen=True)
class RebroadcasterStats:
    datagrams_received: int
    datagrams_rejected: int
    handshakes_started: int
    endpoints_bound: int
    tickets_rejected: int


@dataclass
class EndpointWireState:
    """Destination-local ProtoTunnel, CommUDP and GameManager progress."""

    bound_at: float = 0.0
    last_activity_at: float = 0.0
    tunnel_key: bytes = b""
    # ProtoTunnel carries only the low 16 bits of the client RC4 position;
    # retain the reconstructed connection-local offset across wraparound.
    client_stream_offset_words: int | None = None
    next_offset_words: int = 0
    next_server_sequence: int = 0x101
    # NetGameLink state uses the capture-confirmed destination-local virtual
    # 0x80..0xff namespace independently of reliable GameManager sequences.
    next_server_virtual_sequence: int = 0x80
    last_world_state_footer_tick_ms: int = 0
    last_client_sequence: int = 0xFF
    last_client_acknowledgement: int = 0xFF
    last_client_footer: bytes = b""
    last_client_footer_received_tick_ms: int = 0
    world_footer_observation_count: int = 0
    world_footer_observation_log_tick_ms: int = 0
    world_footer_send_log_tick_ms: int = 0
    world_footer_remote_ack_tick_ms: int = 0
    world_footer_received_tick_ms: int = 0
    # NFSC FUN_0098b640 initializes these NetGameLink words to zero; the first
    # footer then follows FUN_0098b160's >=1024 ms reset/seed branch.
    world_footer_rtt_avg_ms: int = 0
    world_footer_jitter_avg_ms: int = 0
    world_footer_lag_repair_logged: bool = False
    footer: bytes = b""
    fallback_client_tick_ms: int = 0
    bootstrap_pending: bool = False
    bootstrap_acknowledgement: int = 0xFF
    bootstrap_sent: bool = False
    suppress_self_join_publication: bool = False
    published_player_ids: set[int] = field(default_factory=set)
    initial_hostprops_sent: bool = False

    session_bootstrap_sent: bool = False
    # The self publication and logical bootstrap records are retained as one
    # GameManager stage.  Before CommUDP ACKs the stage, retry must preserve
    # the exact encrypted bytes; after that ACK, a fresh sequence/RC4 window
    # is required so GameManager sees the publication again instead of
    # CommUDP discarding an exact duplicate below the receive window.
    session_self_join_body: bytes = b""
    session_self_join_record: bytes = b""
    session_bootstrap_specs: tuple[tuple[int, bytes], ...] = ()
    session_bootstrap_window: ReliableWindow | None = None
    session_object_id: int = 0
    session_generation: int = 0
    session_blocks: dict[int, bytes] = field(default_factory=dict)
    local_reflected_object_id: int = 0
    allocation_lock_triggered: bool = False
    pending_allocation_offset_zero_sent: bool = False
    pending_allocation_object_id: int = 0
    pending_allocation_reflected_object_id: int = 0
    pending_allocation_blocks: tuple[bytes, ...] = ()
    # The retail helper-allocation capture advances through three distinct
    # reliable receive windows.  Fast clients can upload all generation-3
    # fragments in one datagram, so retain the destination-local ACK targets
    # instead of collapsing allocation, generation reflection and room commit
    # into one server response burst.
    allocation_release_final_sequences: dict[Address, int] = field(
        default_factory=dict
    )
    allocation_reflection_final_sequences: dict[Address, int] = field(
        default_factory=dict
    )
    allocation_release_wait_logged: bool = False
    allocation_reflection_wait_logged: bool = False
    # Retail acknowledges the final helper setup record before receiving the
    # five-record co-op room commit aggregate.  Keep that predecessor window
    # distinct from the generation-3 reflection barrier above.
    room_commit_prerequisite_sequence: int = 0
    room_commit_prerequisite_wait_logged: bool = False
    remote_object_ids: set[int] = field(default_factory=set)
    published_remote_objects: dict[SourceKey, tuple[bytes, ...]] = field(
        default_factory=dict
    )
    published_session_offsets: dict[SourceKey, set[int]] = field(
        default_factory=dict
    )
    pending_session_releases: set[Address] = field(default_factory=set)
    invite_join_sequence: int = 0
    # Capture-backed helper barrier for remote-object offsets 0x1e4/0x3c8.
    # Acknowledging only the first continuation is not application progress.
    invite_host_continuation_final_sequence: int = 0
    invite_host_barrier_pending: bool = False
    invite_host_barrier_deferred_client_sequence: int = 0
    invite_host_barrier_progress_wait_logged: bool = False
    pending_player_leaves: list[bytes] = field(default_factory=list)
    # When the room coordinator disappears, keep each remaining endpoint
    # alive just long enough to deliver and acknowledge PlayerLeft.  Dropping
    # every endpoint immediately leaves stock Carbon displaying a stale host
    # until its own transport timeout fires.
    host_exit_queued_at: float = 0.0
    host_exit_player_left_sent: bool = False
    session_probe_sent: bool = False
    clock_probe_sent: bool = False
    session_token: bytes = b""
    session_confirmation_pending: bool = False
    session_confirmed: bool = False
    preconfirm_deferred_types: set[int] = field(default_factory=set)
    preconfirm_session_fragments_logged: set[tuple[int, int]] = field(
        default_factory=set
    )

    latest_latency_info: bytes = b""
    ready_requested: bool = False
    start_lock_final_sequence: int = 0
    latency_info_sent: bool = False
    race_ready_seen: bool = False
    gameplay_ready: bool = False
    active_game_ready: bool = False
    match_timer_retry: RetrySchedule | None = None
    match_timer_sequence: int = 0
    match_timer_generation_id: int = 0
    ready_seed_final_sequence: int = 0
    ready_seed_used_latency_history: bool = False
    pending_ai_registration_windows: list[ReliableWindow] = field(
        default_factory=list
    )
    ai_registration_ready_refresh_sent: bool = False
    ready_epoch_generation: int = 0
    world_state_log_not_before: float = 0.0
    world_state_footer_diagnostic_logged: bool = False
    pursuit_tag_log_not_before: float = 0.0


class ReadyStage(IntEnum):
    SEED_SENT_WAIT_GUEST_13_15 = 1
    STATE_PAIR_SENT_WAIT_HOST_NATIVE_BUNDLE = 2
    COUNTDOWN_ACTIVE = 3
    ABORTED = 4


@dataclass
class ReadyEpoch:
    generation: int
    stage: ReadyStage
    host_pid: int
    guest_pid: int
    source_first_sequence: int
    source_final_sequence: int
    source_payload_hash: int
    attributes: bytes
    wire_deadline: float
    state13: bytes = b""
    state15: bytes = b""
    guest_pre_state_sequence: int = 0
    guest_state_final_sequence: int = 0
    guest_state_window_started: bool = False
    seed_ack_wait_logged: bool = False
    native_bundle_hash: int = 0
