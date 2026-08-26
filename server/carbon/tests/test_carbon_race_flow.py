"""End-to-end tests for the modular Carbon race transition and world relay."""

from dataclasses import replace
import time
import struct
import unittest
from unittest import mock

from carbon.tests import test_carbon_gamemanager_session as session_tests
from carbon.transport.commudp import game_manager_body
from carbon.gamemanager.race_state import (
    GameRaceState,
    RacePhase,
    RoomAccess,
)
from carbon.gamemanager.race_session import (
    anonymous_state,
    latency_info,
    locked_host_properties,
    named_state,
    reopen_host_properties,
    session_attributes,
)
from carbon.core.config import Endpoint
from carbon.rebroadcaster.service import (
    CarbonRebroadcasterService,
    EndpointWireState,
    ReadyEpoch,
    ReadyStage,
)
from carbon.rebroadcaster.world_state import NetGameLinkWorldState
from carbon.theater.directory import CarbonGameDirectory
from carbon.transport.commudp import CommUDPActive, parse_channel_one
from carbon.transport.prototunnel import (
    TunnelDatagram,
    TunnelPacket,
    decode_datagram,
)


def _client_active(sequence: int, acknowledgement: int, logical: bytes = b"") -> bytes:
    return (
        int(sequence).to_bytes(4, "big")
        + int(acknowledgement).to_bytes(4, "big")
        + bytes(logical)
        + b"\x04"
    )


def _send(service, addr, *, outer: int, sequence: int, acknowledgement: int, logical: bytes = b""):
    raw = TunnelDatagram(
        outer,
        (TunnelPacket(1, _client_active(sequence, acknowledgement, logical)),),
    ).encode(session_tests.EKEY)
    return service.handle_datagram(raw, addr)


def _logical_replies(replies, target=None):
    return [
        game_manager_body(active.payload)
        for raw, destination in replies
        if target is None or destination == target
        for active in session_tests._active_messages(raw)
    ]


def _native_commudp_records(active: CommUDPActive) -> tuple[bytes, tuple[bytes, ...]]:
    """Apply NFSC's native tail-length split to one reliable aggregate."""
    history_count = (int(active.sequence) >> 28) & 0x0F
    remaining = bytes(active.payload[8:])
    historical: list[bytes] = []
    for _ in range(history_count):
        if not remaining:
            raise AssertionError("truncated CommUDP aggregate")
        record_length = remaining[-1]
        newest_length = len(remaining) - 1 - record_length
        if record_length <= 0 or newest_length < 0:
            raise AssertionError(
                f"invalid CommUDP history length: remaining={len(remaining)} "
                f"record={record_length} newest={newest_length}"
            )
        historical.append(remaining[newest_length:-1])
        remaining = remaining[:newest_length]
    if not remaining:
        raise AssertionError("CommUDP aggregate has no current record")
    return remaining, tuple(historical)


def _finalized_flow():
    flow = session_tests.CarbonGameManagerFlowTests(methodName="runTest")
    flow.setUp()
    flow._bind_host()
    _send(
        flow.service,
        flow.host_addr,
        outer=24,
        sequence=0x101,
        acknowledgement=0x102,
        logical=bytes.fromhex("0184"),
    )
    for index, offset in enumerate((0, 0x1E4, 0x3C8), start=2):
        _send(
            flow.service,
            flow.host_addr,
            outer=30 + index,
            sequence=index,
            acknowledgement=0x102,
            logical=session_tests._session_block(1, "Host", offset),
        )
    _send(
        flow.service,
        flow.host_addr,
        outer=40,
        sequence=6,
        acknowledgement=0x108,
        logical=bytes.fromhex("0000000002427f6e98"),
    )
    _send(
        flow.service,
        flow.host_addr,
        outer=41,
        sequence=7,
        acknowledgement=0x109,
    )

    flow._bind_guest()
    _send(
        flow.service,
        flow.guest_addr,
        outer=28,
        sequence=0x101,
        acknowledgement=0x103,
        logical=bytes.fromhex("0184"),
    )
    for index, offset in enumerate((0, 0x1E4, 0x3C8), start=10):
        _send(
            flow.service,
            flow.guest_addr,
            outer=50 + index,
            sequence=index,
            acknowledgement=0x104,
            logical=session_tests._session_block(2, "Guest", offset),
        )
    _send(
        flow.service,
        flow.host_addr,
        outer=70,
        sequence=8,
        acknowledgement=0x110,
        logical=bytes.fromhex("000000000242800000"),
    )
    _send(
        flow.service,
        flow.guest_addr,
        outer=80,
        sequence=0x10,
        acknowledgement=0x115,
        logical=bytes.fromhex("0000000002427f0000"),
    )
    _send(
        flow.service,
        flow.guest_addr,
        outer=81,
        sequence=0x11,
        acknowledgement=0x118,
    )
    return flow


def _install_ready_epoch(flow, stage, timer=None):
    timer = timer or (
        bytes.fromhex("000000001b") + struct.pack(">Iff", 5, 67.894, 20.5)
    )
    generation = 1
    epoch = ReadyEpoch(
        generation=generation,
        stage=stage,
        host_pid=flow.service._bindings[flow.host_addr].participant.player_id,
        guest_pid=flow.service._bindings[flow.guest_addr].participant.player_id,
        source_first_sequence=0x140,
        source_final_sequence=0x144,
        source_payload_hash=0x12345678,
        attributes=session_attributes(flow.game.properties),
        wire_deadline=flow.service._timer_logical_deadline(timer),
    )
    flow.service._ready_epochs[flow.game.gid] = epoch
    flow.service._ready_generations[flow.game.gid] = generation
    for addr in (flow.host_addr, flow.guest_addr):
        flow.service._wire[addr].ready_epoch_generation = generation
    return epoch


def _ready_request_actives(flow, timer):
    logicals = [
        timer + b"\x04" + bytes.fromhex("0000000013000131") + b"\x04",
        session_attributes(flow.game.properties),
        anonymous_state(1),
        bytes.fromhex("0000000014000130"),
        bytes.fromhex("00000000180000"),
    ]
    flags = [2, 0, 0, 1, 2]
    actives = []
    for index, (logical, flag) in enumerate(zip(logicals, flags)):
        sequence = (flag << 28) | (0x140 + index)
        actives.append(
            CommUDPActive(
                sequence,
                0x120,
                _client_active(sequence, 0x120, logical),
                None,
            )
        )
    return actives


def _advance_to_countdown_expired(flow) -> None:
    host_ready = (
        bytes.fromhex("0000000014000130")
        + bytes.fromhex("0000000018000004")
    )
    _send(
        flow.service,
        flow.host_addr,
        outer=90,
        sequence=0x12,
        acknowledgement=0x120,
        logical=host_ready,
    )
    race = flow.service._race[flow.game.gid]
    _send(
        flow.service,
        flow.host_addr,
        outer=91,
        sequence=0x13,
        acknowledgement=0x121,
        logical=bytes.fromhex("000000001c000000000002"),
    )
    if race.phase != RacePhase.COUNTDOWN_EXPIRED:
        raise AssertionError(f"expected COUNTDOWN_EXPIRED, got {race.phase.name}")


class CarbonStartLockWireTests(unittest.TestCase):
    def test_start_lock_bundle_matches_capture_sequences_and_cumulative_bodies(self) -> None:
        service = CarbonRebroadcasterService(
            CarbonGameDirectory(Endpoint("127.0.0.1", 19118))
        )
        destination = ("192.0.2.50", 1042)
        service._wire[destination] = EndpointWireState(
            next_server_sequence=0x130,
            last_client_sequence=0x131,
            last_client_acknowledgement=0x12F,
            footer=bytes.fromhex("00cdab240ec648bb00140000"),
        )
        replies = []
        service._append_start_lock_bundle(replies, destination)
        self.assertEqual(len(replies), 1)

        decoded = decode_datagram(replies[0][0], session_tests.EKEY)
        active = [
            parsed
            for packet in decoded.packets
            if isinstance((parsed := parse_channel_one(packet)), CommUDPActive)
        ]
        self.assertEqual(
            [item.sequence for item in active],
            [0x00000130, 0x10000131, 0x20000132, 0x20000133, 0x40000134],
        )
        self.assertEqual(
            [item.acknowledgement for item in active],
            [0x12F, 0x131, 0x131, 0x131, 0x131],
        )
        self.assertEqual(
            [item.payload[8:].hex() for item in active],
            [
                "00cdab240ec648bb0014000040",
                "018c0001018080000000080400cdab240ec648bb00140000400d",
                "018c00010080800000000804018c000101808000000008040c"
                "00cdab240ec648bb00140000400d",
                "018c00000080800000000804018c000100808000000008040c"
                "018c000101808000000008040c",
                "018c00000082800000000804018c000000808000000008040c"
                "018c000100808000000008040c018c000101808000000008040c"
                "00cdab240ec648bb00140000400d",
            ],
        )


class CarbonRaceTransitionTests(unittest.TestCase):
    def test_reply_spacing_stops_when_destination_enters_racing(self) -> None:
        flow = _finalized_flow()
        race = flow.service._race[flow.game.gid]
        self.assertEqual(
            flow.service.reply_spacing_seconds_for(flow.host_addr),
            0.012,
        )
        race.phase = RacePhase.RACING
        self.assertEqual(
            flow.service.reply_spacing_seconds_for(flow.host_addr),
            0.0,
        )
        race.phase = RacePhase.FINISHED
        self.assertEqual(
            flow.service.reply_spacing_seconds_for(flow.host_addr),
            0.0,
        )

    def test_compound_ready_and_native_state2_are_room_synchronized(self) -> None:
        flow = _finalized_flow()
        race = flow.service._race[flow.game.gid]
        self.assertEqual(race.phase, RacePhase.COUNTDOWN)

        host_ready = (
            bytes.fromhex("0000000014000130")
            + bytes.fromhex("0000000018000004")
        )
        host_ready_replies = _send(
                flow.service,
                flow.host_addr,
                outer=90,
                sequence=0x12,
                acknowledgement=0x120,
                logical=host_ready,
            )
        ready_hostprops = [
            active.game_manager.message_type
            for raw, _ in host_ready_replies
            for active in session_tests._active_messages(raw)
            if active.game_manager is not None
        ]
        self.assertEqual(ready_hostprops, [0x0C, 0x0C])
        self.assertEqual(race.room_access, RoomAccess.LOCKED)
        self.assertTrue(flow.game.quick_join_locked)
        self.assertEqual(race.phase, RacePhase.COUNTDOWN)

        expiry = _send(
            flow.service,
            flow.host_addr,
            outer=91,
            sequence=0x14,
            acknowledgement=0x121,
            logical=bytes.fromhex("000000001c000000000002"),
        )
        state2_targets = {
            target
            for raw, target in expiry
            if any(
                game_manager_body(active.payload).startswith(
                    bytes.fromhex("000000001c000000000002")
                )
                for active in session_tests._active_messages(raw)
            )
        }
        self.assertEqual(state2_targets, {flow.host_addr, flow.guest_addr})
        self.assertEqual(race.phase, RacePhase.COUNTDOWN_EXPIRED)
        self.assertFalse(any(
            body.startswith(bytes.fromhex("000000001c"))
            and body.endswith(bytes.fromhex("00000001"))
            for body in _logical_replies(expiry)
        ))

    def test_host_startloading_infers_missing_state2_inside_final_timer_window(self) -> None:
        flow = _finalized_flow()
        host_ready = (
            bytes.fromhex("0000000014000130")
            + bytes.fromhex("0000000018000004")
        )
        _send(
            flow.service,
            flow.host_addr,
            outer=90,
            sequence=0x12,
            acknowledgement=0x120,
            logical=host_ready,
        )
        race = flow.service._race[flow.game.gid]
        self.assertEqual(race.phase, RacePhase.COUNTDOWN)
        self.assertEqual(race.room_access, RoomAccess.LOCKED)
        race.countdown_deadline = time.monotonic() + 0.5

        startloading = bytes.fromhex("000000000800000001")
        replies = _send(
            flow.service,
            flow.host_addr,
            outer=91,
            sequence=0x13,
            acknowledgement=0x121,
            logical=startloading,
        )

        self.assertEqual(
            {
                target
                for raw, target in replies
                if startloading in _logical_replies([(raw, target)], target)
            },
            {flow.host_addr, flow.guest_addr},
        )
        self.assertEqual(race.phase, RacePhase.LOADING)

    def test_host_startloading_remains_deferred_before_final_timer_window(self) -> None:
        flow = _finalized_flow()
        host_ready = (
            bytes.fromhex("0000000014000130")
            + bytes.fromhex("0000000018000004")
        )
        _send(
            flow.service,
            flow.host_addr,
            outer=90,
            sequence=0x12,
            acknowledgement=0x120,
            logical=host_ready,
        )
        race = flow.service._race[flow.game.gid]
        race.countdown_deadline = time.monotonic() + 10.0

        startloading = bytes.fromhex("000000000800000001")
        replies = _send(
            flow.service,
            flow.host_addr,
            outer=91,
            sequence=0x13,
            acknowledgement=0x121,
            logical=startloading,
        )

        self.assertFalse(any(
            startloading in _logical_replies([(raw, target)], target)
            for raw, target in replies
        ))
        self.assertEqual(race.phase, RacePhase.COUNTDOWN)

    def test_startloading_ready_startsync_and_v822_native_world_footer(self) -> None:
        flow = _finalized_flow()
        _advance_to_countdown_expired(flow)
        startloading = bytes.fromhex("000000000800000001")
        replies = _send(
            flow.service,
            flow.host_addr,
            outer=100,
            sequence=0x20,
            acknowledgement=0x130,
            logical=startloading,
        )
        self.assertEqual(
            {
                target
                for raw, target in replies
                if startloading in _logical_replies([(raw, target)], target)
            },
            {flow.host_addr, flow.guest_addr},
        )
        self.assertEqual(flow.service._race[flow.game.gid].phase, RacePhase.LOADING)

        host_ready = _send(
            flow.service,
            flow.host_addr,
            outer=101,
            sequence=0x21,
            acknowledgement=0x131,
            logical=bytes.fromhex("000000000f"),
        )
        self.assertEqual([body for body in _logical_replies(host_ready) if body], [])
        startsync = _send(
            flow.service,
            flow.guest_addr,
            outer=102,
            sequence=0x22,
            acknowledgement=0x132,
            logical=bytes.fromhex("000000000f"),
        )
        sync_targets = {
            target
            for raw, target in startsync
            if any(
                game_manager_body(active.payload).startswith(bytes.fromhex("000000000a"))
                for active in session_tests._active_messages(raw)
            )
        }
        self.assertEqual(sync_targets, {flow.host_addr, flow.guest_addr})
        self.assertTrue(all(
            bytes.fromhex("000000000f") in body
            for body in _logical_replies(startsync)
            if body.startswith(bytes.fromhex("000000000a"))
        ))
        self.assertTrue(flow.service._wire[flow.host_addr].gameplay_ready)
        self.assertTrue(flow.service._wire[flow.guest_addr].gameplay_ready)

        reliable_before_world = {
            endpoint: flow.service._wire[endpoint].next_server_sequence
            for endpoint in (flow.host_addr, flow.guest_addr)
        }
        # Official fullchalangerace frames 1308/1311-1312:
        #   peer footer  228145bd 002417e3 001bd602
        #   server send  002417ed 228145f8 0016xxxx
        # The final two bytes are ignored stack residue in the retail sender;
        # V818 deterministically zeroes them.
        for endpoint in (flow.host_addr, flow.guest_addr):
            wire = flow.service._wire[endpoint]
            wire.last_client_footer = bytes.fromhex(
                "228145bd002417e3001bd602"
            )
            wire.last_client_footer_received_tick_ms = 0x228145EE
            wire.world_footer_remote_ack_tick_ms = 0x002417E3
            wire.world_footer_received_tick_ms = 0x228145EE
            wire.world_footer_rtt_avg_ms = 20
            wire.world_footer_jitter_avg_ms = 23
        world = bytes.fromhex("000000000600000002112233445566778899")
        with mock.patch.object(
            CarbonRebroadcasterService,
            "_server_tick_ms",
            return_value=0x228145F8,
        ):
            with self.assertLogs(
                "carbon.rebroadcaster.service",
                level="INFO",
            ) as captured:
                relayed = _send(
                    flow.service,
                    flow.guest_addr,
                    outer=103,
                    sequence=0x80,
                    acknowledgement=0x133,
                    logical=world,
                )
        footer_diagnostics = [
            message
            for message in captured.output
            if "V823 native world-state footer" in message
        ]
        self.assertEqual(len(footer_diagnostics), 2)
        self.assertTrue(all("virtual_seq=80" in item for item in footer_diagnostics))
        self.assertEqual(
            {
                marker
                for marker in ("ack=0000021", "ack=0000022")
                if any(marker in item for item in footer_diagnostics)
            },
            {"ack=0000021", "ack=0000022"},
        )
        self.assertTrue(all("kind=0x06" in item for item in footer_diagnostics))
        self.assertTrue(all("body_handle=02" in item for item in footer_diagnostics))
        self.assertTrue(all(
            "outbound_footer=002417ed228145f800160000" in item
            for item in footer_diagnostics
        ))
        self.assertTrue(all(
            "suffix=45 cadence=time>250ms transform=native-send-clock" in item
            for item in footer_diagnostics
        ))
        self.assertEqual(_logical_replies(relayed, flow.host_addr), [world])
        self.assertEqual(_logical_replies(relayed, flow.guest_addr), [world])
        for endpoint in (flow.host_addr, flow.guest_addr):
            active = [
                item
                for raw, target in relayed
                if target == endpoint
                for item in session_tests._active_messages(raw)
            ]
            self.assertEqual([item.sequence for item in active], [0x80])
            self.assertEqual(active[0].payload[-1], 0x45)
            self.assertEqual(
                active[0].payload[-13:-1],
                bytes.fromhex("002417ed228145f800160000"),
            )
            self.assertEqual(
                flow.service._wire[endpoint].next_server_sequence,
                reliable_before_world[endpoint],
            )
            self.assertTrue(
                flow.service._wire[endpoint].world_state_footer_diagnostic_logged
            )
        # The guest's virtual upload must not replace its reliable 0x22 ACK
        # anchor with virtual sequence 0x80.
        self.assertEqual(flow.service._wire[flow.guest_addr].last_client_sequence, 0x22)

        # Challenge hosts submit the four AI transforms in one ProtoTunnel
        # datagram. Retail preserves all four opaque type-6 bodies and sends
        # the same batch to both endpoints, including the source.
        recent_footer_tick = flow.service._server_tick_ms()
        for endpoint in (flow.host_addr, flow.guest_addr):
            wire = flow.service._wire[endpoint]
            wire.next_server_virtual_sequence = 0xFE
            wire.last_world_state_footer_tick_ms = recent_footer_tick
        world_batch = [
            bytes.fromhex(f"0000000006000003d{car:x}112233445566778899")
            for car in range(4, 8)
        ]
        raw = TunnelDatagram(
            104,
            tuple(
                TunnelPacket(1, _client_active(0x81 + index, 0x134, body))
                for index, body in enumerate(world_batch)
            ),
        ).encode(session_tests.EKEY)
        batched = flow.service.handle_datagram(raw, flow.guest_addr)
        self.assertEqual(_logical_replies(batched, flow.host_addr), world_batch)
        self.assertEqual(_logical_replies(batched, flow.guest_addr), world_batch)
        for endpoint in (flow.host_addr, flow.guest_addr):
            active = [
                item
                for raw_reply, target in batched
                if target == endpoint
                for item in session_tests._active_messages(raw_reply)
            ]
            self.assertEqual(
                [item.sequence for item in active],
                [0xFE, 0xFF, 0x80, 0x81],
            )
            self.assertEqual([item.payload[-1] for item in active], [0x05] * 4)
            wire = flow.service._wire[endpoint]
            self.assertTrue(all(
                item.acknowledgement == wire.last_client_sequence
                for item in active
            ))
            self.assertEqual(wire.next_server_virtual_sequence, 0x82)
            self.assertEqual(
                wire.next_server_sequence,
                reliable_before_world[endpoint],
            )
        self.assertEqual(flow.service._wire[flow.guest_addr].last_client_sequence, 0x22)

        opaque_block = bytes.fromhex(
            "0000000007"
            "02000009"
            "00000000000000000000000000000000000000000000000000000000"
            "0009112233445566778899"
        )
        relayed_block = _send(
            flow.service,
            flow.host_addr,
            outer=105,
            sequence=0x80,
            acknowledgement=0x135,
            logical=opaque_block,
        )
        self.assertEqual(_logical_replies(relayed_block, flow.host_addr), [opaque_block])
        self.assertEqual(_logical_replies(relayed_block, flow.guest_addr), [opaque_block])
        for endpoint in (flow.host_addr, flow.guest_addr):
            active = [
                item
                for raw_reply, target in relayed_block
                if target == endpoint
                for item in session_tests._active_messages(raw_reply)
            ]
            self.assertEqual([item.sequence for item in active], [0x82])
            self.assertEqual(active[0].payload[-1], 0x05)
            self.assertEqual(
                flow.service._wire[endpoint].next_server_sequence,
                reliable_before_world[endpoint],
            )
        self.assertEqual(flow.service._wire[flow.host_addr].last_client_sequence, 0x21)

    def test_startloading_stays_deferred_for_unconfirmed_guest(self) -> None:
        flow = _finalized_flow()
        _advance_to_countdown_expired(flow)
        guest_wire = flow.service._wire[flow.guest_addr]
        guest_wire.session_confirmed = False
        startloading = bytes.fromhex("000000000800000001")

        replies = _send(
            flow.service,
            flow.host_addr,
            outer=100,
            sequence=0x20,
            acknowledgement=0x130,
            logical=startloading,
        )

        self.assertFalse(any(
            startloading in _logical_replies([(raw, target)], target)
            for raw, target in replies
        ))
        self.assertEqual(
            flow.service._race[flow.game.gid].phase,
            RacePhase.COUNTDOWN_EXPIRED,
        )

    def test_v831_pursuit_tag_sync_reflects_current_record_to_both_clients(self) -> None:
        flow = _finalized_flow()
        race = flow.service._race.setdefault(flow.game.gid, GameRaceState())
        race.phase = RacePhase.RACING
        for endpoint in (flow.host_addr, flow.guest_addr):
            flow.service._wire[endpoint].gameplay_ready = True

        # Official compleatepursuittag frame 3173 transfers ownership from
        # car 0x78 to car 0x82. The same CommUDP upload may retain older 0x1F
        # and latency records; only this leading 17-byte record is current.
        transfer = bytes.fromhex("000000001f007800820000000000000000")
        redundancy = bytes.fromhex(
            "04000000001f0078fff8000000000000000004"
            "12000000001f0078fff8000000000000000004"
        )
        with self.assertLogs(
            "carbon.rebroadcaster.service",
            level="INFO",
        ) as captured:
            replies = _send(
                flow.service,
                flow.host_addr,
                outer=105,
                sequence=0x200001C4,
                acknowledgement=0x280,
                logical=transfer + redundancy,
            )

        self.assertEqual(_logical_replies(replies, flow.host_addr), [transfer])
        self.assertEqual(_logical_replies(replies, flow.guest_addr), [transfer])
        for endpoint in (flow.host_addr, flow.guest_addr):
            active = [
                item
                for raw, target in replies
                if target == endpoint
                for item in session_tests._active_messages(raw)
            ]
            self.assertEqual(len(active), 1)
            self.assertEqual(active[0].payload[-1], 0x04)
        self.assertTrue(any(
            "V831 PursuitTagSync relayed" in message
            and "car=0078" in message
            and "state=0082" in message
            and "endpoints=2" in message
            for message in captured.output
        ))

    def test_v831_pursuit_tag_sync_is_ignored_outside_racing(self) -> None:
        flow = _finalized_flow()
        flow.service._race[flow.game.gid].phase = RacePhase.LOADING
        for endpoint in (flow.host_addr, flow.guest_addr):
            flow.service._wire[endpoint].gameplay_ready = True

        sync = bytes.fromhex("000000001f0078fff80000000000000000")
        replies = _send(
            flow.service,
            flow.host_addr,
            outer=106,
            sequence=0x1C4,
            acknowledgement=0x280,
            logical=sync,
        )
        self.assertEqual(
            [body for body in _logical_replies(replies, flow.host_addr) if body],
            [],
        )
        self.assertEqual(
            [body for body in _logical_replies(replies, flow.guest_addr) if body],
            [],
        )

    def test_v825_relays_sanitized_player_controlled_ai_window_to_room(self) -> None:
        flow = _finalized_flow()
        service = flow.service
        registrations = (
            bytes.fromhex(
                "0000000005000000790004d1a01768000756494e43454e54"
            ),
            bytes.fromhex(
                "00000000050000007a000450d247ab00064445454e454e"
            ),
            bytes.fromhex(
                "00000000050000007b000412ac767000064b494552414e"
            ),
        )
        normalized = []
        for body in registrations:
            sanitized = bytearray(body)
            sanitized[11:15] = b"\x00\x00\x00\x00"
            normalized.append(bytes(sanitized))

        service._wire[flow.host_addr].next_server_sequence = 0x1B7
        service._wire[flow.guest_addr].next_server_sequence = 0x192
        service._wire[flow.guest_addr].last_client_sequence = 0x167
        incoming_sequences = (0x191, 0x10000192, 0x10000193)
        incoming_records = (
            (registrations[0],),
            (registrations[1], registrations[0]),
            (registrations[2], registrations[1]),
        )
        raw = TunnelDatagram(
            104,
            tuple(
                TunnelPacket(
                    1,
                    int(sequence).to_bytes(4, "big")
                    + (0x17B).to_bytes(4, "big")
                    + service._commudp_aggregate_payload(records),
                )
                for sequence, records in zip(
                    incoming_sequences,
                    incoming_records,
                )
            ),
        ).encode(session_tests.EKEY)

        with self.assertLogs(
            "carbon.rebroadcaster.service",
            level="INFO",
        ) as captured:
            replies = service.handle_datagram(raw, flow.host_addr)
        self.assertTrue(any(
            "V825 player-controlled AI registration relay" in message
            and "cars=00000079,0000007a,0000007b" in message
            for message in captured.output
        ))

        for endpoint, base, acknowledgement in (
            (flow.host_addr, 0x1B7, 0x193),
            (flow.guest_addr, 0x192, 0x167),
        ):
            active = [
                item
                for raw_reply, target in replies
                if target == endpoint
                for item in session_tests._active_messages(raw_reply)
            ]
            self.assertEqual(len(active), 4)
            self.assertEqual(
                [item.sequence for item in active],
                [
                    base,
                    0x10000000 | (base + 1),
                    0x10000000 | (base + 2),
                    0x20000000 | (base + 3),
                ],
            )
            self.assertTrue(all(
                item.acknowledgement == acknowledgement
                for item in active
            ))
            decoded = [_native_commudp_records(item) for item in active]
            self.assertEqual(
                [current for current, _history in decoded],
                [
                    normalized[0] + b"\x04",
                    normalized[1] + b"\x04",
                    normalized[2] + b"\x04",
                    bytes.fromhex("000000000f04"),
                ],
            )
            self.assertEqual(decoded[0][1], ())
            self.assertEqual(decoded[1][1], (normalized[0] + b"\x04",))
            self.assertEqual(decoded[2][1], (normalized[1] + b"\x04",))
            self.assertEqual(
                decoded[3][1],
                (
                    normalized[1] + b"\x04",
                    normalized[2] + b"\x04",
                ),
            )
            self.assertEqual(
                service._wire[endpoint].next_server_sequence,
                base + 4,
            )
            pending = service._wire[endpoint].pending_ai_registration_windows
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0].base_sequence, base)
            self.assertEqual(pending[0].final_sequence, base + 3)

        duplicate = service.handle_datagram(raw, flow.host_addr)
        self.assertFalse(any(
            game_manager_body(item.payload).startswith(
                bytes.fromhex("0000000005")
            )
            for raw_reply, _target in duplicate
            for item in session_tests._active_messages(raw_reply)
        ))

    def test_v827_retries_unacked_ai_window_with_backoff_until_ack(self) -> None:
        flow = _finalized_flow()
        service = flow.service
        registration = bytes.fromhex(
            "0000000005000000790004d1a01768000756494e43454e54"
        )
        service._wire[flow.guest_addr].next_server_sequence = 0x192
        initial: list[tuple[bytes, tuple[str, int]]] = []
        service.gameplay_relay.relay_player_controlled_ai(
            initial,
            flow.host_addr,
            service._bindings[flow.host_addr],
            (registration,),
        )

        guest_wire = service._wire[flow.guest_addr]
        window = guest_wire.pending_ai_registration_windows[0]
        self.assertEqual(window.base_sequence, 0x192)
        self.assertEqual(window.final_sequence, 0x193)
        next_reliable = guest_wire.next_server_sequence
        window.retry.retry_not_before = time.monotonic() - 0.01

        with self.assertLogs(
            "carbon.rebroadcaster.service",
            level="INFO",
        ) as retry_logs:
            retried = _send(
                service,
                flow.guest_addr,
                outer=105,
                sequence=0x168,
                acknowledgement=0x192,
            )
        active = [
            item
            for raw_reply, target in retried
            if target == flow.guest_addr
            for item in session_tests._active_messages(raw_reply)
        ]
        self.assertEqual(
            [item.sequence for item in active],
            [0x192, 0x10000193],
        )
        self.assertEqual(guest_wire.next_server_sequence, next_reliable)
        self.assertEqual(window.retry.retries_sent, 1)
        self.assertGreater(window.retry.retry_not_before, time.monotonic())
        self.assertTrue(any(
            "V827 AI registration retry" in message
            and "seq=0000192-0000193" in message
            and "attempt=1" in message
            for message in retry_logs.output
        ))

        throttled = _send(
            service,
            flow.guest_addr,
            outer=106,
            sequence=0x169,
            acknowledgement=0x192,
        )
        self.assertFalse(any(
            game_manager_body(item.payload).startswith(
                bytes.fromhex("0000000005")
            )
            for raw_reply, target in throttled
            if target == flow.guest_addr
            for item in session_tests._active_messages(raw_reply)
        ))

        with self.assertLogs(
            "carbon.rebroadcaster.service",
            level="INFO",
        ) as confirmation_logs:
            _send(
                service,
                flow.guest_addr,
                outer=107,
                sequence=0x16A,
                acknowledgement=window.final_sequence,
            )
        self.assertEqual(guest_wire.pending_ai_registration_windows, [])
        self.assertTrue(any(
            "V827 AI registration confirmed" in message
            and "ack=0000193" in message
            and "retries=1" in message
            for message in confirmation_logs.output
        ))

    def test_v827_retries_unconfirmed_ai_before_start_race_sync(self) -> None:
        flow = _finalized_flow()
        service = flow.service
        race = service._race[flow.game.gid]
        race.phase = RacePhase.LOADING
        registration = bytes.fromhex(
            "0000000005000000790004d1a01768000756494e43454e54"
        )
        for endpoint, base in (
            (flow.host_addr, 0x1B7),
            (flow.guest_addr, 0x192),
        ):
            wire = service._wire[endpoint]
            wire.next_server_sequence = base
            wire.last_client_acknowledgement = base - 1
        initial: list[tuple[bytes, tuple[str, int]]] = []
        service.gameplay_relay.relay_player_controlled_ai(
            initial,
            flow.host_addr,
            service._bindings[flow.host_addr],
            (registration,),
        )
        for endpoint in (flow.host_addr, flow.guest_addr):
            service._wire[endpoint].race_ready_seen = True

        replies: list[tuple[bytes, tuple[str, int]]] = []
        service._maybe_broadcast_startsync(
            replies,
            flow.game,
            flow.host_addr,
        )

        self.assertEqual(race.phase, RacePhase.RACING)
        for endpoint in (flow.host_addr, flow.guest_addr):
            window = service._wire[endpoint].pending_ai_registration_windows[0]
            self.assertEqual(window.retry.retries_sent, 1)
            registrations = [
                item
                for raw_reply, target in replies
                if target == endpoint
                for item in session_tests._active_messages(raw_reply)
                if game_manager_body(item.payload).startswith(
                    bytes.fromhex("0000000005")
                )
            ]
            self.assertEqual(len(registrations), 1)
            self.assertEqual(
                registrations[0].sequence & 0x0FFFFFFF,
                window.base_sequence,
            )

    def test_v828_refreshes_acked_ai_for_ready_guest_with_fresh_sequences(self) -> None:
        flow = _finalized_flow()
        service = flow.service
        race = service._race[flow.game.gid]
        race.phase = RacePhase.LOADING
        registration = bytes.fromhex(
            "0000000005000000790004d1a01768000756494e43454e54"
        )
        for endpoint, base in (
            (flow.host_addr, 0x1B7),
            (flow.guest_addr, 0x192),
        ):
            service._wire[endpoint].next_server_sequence = base

        initial: list[tuple[bytes, tuple[str, int]]] = []
        service.gameplay_relay.relay_player_controlled_ai(
            initial,
            flow.host_addr,
            service._bindings[flow.host_addr],
            (registration,),
        )
        for endpoint in (flow.host_addr, flow.guest_addr):
            wire = service._wire[endpoint]
            window = wire.pending_ai_registration_windows[0]
            wire.last_client_acknowledgement = window.final_sequence
            service.gameplay_relay.update_ai_registration_delivery(
                [],
                endpoint,
                service._bindings[endpoint],
            )
            self.assertEqual(wire.pending_ai_registration_windows, [])
            wire.race_ready_seen = True

        guest_wire = service._wire[flow.guest_addr]
        fresh_base = guest_wire.next_server_sequence
        with self.assertLogs(
            "carbon.rebroadcaster.service",
            level="INFO",
        ) as captured:
            replies: list[tuple[bytes, tuple[str, int]]] = []
            service._maybe_broadcast_startsync(
                replies,
                flow.game,
                flow.guest_addr,
            )

        self.assertEqual(race.phase, RacePhase.RACING)
        self.assertTrue(guest_wire.ai_registration_ready_refresh_sent)
        self.assertEqual(len(guest_wire.pending_ai_registration_windows), 1)
        refreshed = guest_wire.pending_ai_registration_windows[0]
        self.assertEqual(refreshed.base_sequence, fresh_base)
        self.assertEqual(refreshed.final_sequence, fresh_base + 1)

        guest_registration_packets = [
            item
            for raw_reply, target in replies
            if target == flow.guest_addr
            for item in session_tests._active_messages(raw_reply)
            if game_manager_body(item.payload).startswith(
                bytes.fromhex("0000000005")
            )
        ]
        self.assertEqual(len(guest_registration_packets), 1)
        self.assertEqual(
            guest_registration_packets[0].sequence & 0x0FFFFFFF,
            fresh_base,
        )
        host_registration_packets = [
            item
            for raw_reply, target in replies
            if target == flow.host_addr
            for item in session_tests._active_messages(raw_reply)
            if game_manager_body(item.payload).startswith(
                bytes.fromhex("0000000005")
            )
        ]
        self.assertEqual(host_registration_packets, [])
        self.assertTrue(any(
            "V828 AI registration ready refresh" in message
            and "reason=acked-before-roster-ready" in message
            for message in captured.output
        ))

    def test_v823_sustained_kind5_world_state_footer_time_cadence(self) -> None:
        flow = _finalized_flow()
        for endpoint in (flow.host_addr, flow.guest_addr):
            wire = flow.service._wire[endpoint]
            wire.gameplay_ready = True
            wire.next_server_virtual_sequence = 0x80
            wire.last_world_state_footer_tick_ms = 0

        reliable_before = {
            endpoint: flow.service._wire[endpoint].next_server_sequence
            for endpoint in (flow.host_addr, flow.guest_addr)
        }
        world_batch = tuple(
            bytes.fromhex(f"0000000006000004{car:02x}112233445566778899")
            for car in range(4)
        )
        replies: list[tuple[bytes, tuple[str, int]]] = []
        binding = flow.service._bindings[flow.guest_addr]
        # One four-state source batch is serialized to both destinations at
        # effectively the same tick. Five batches at 52 ms produce the
        # capture's apparent every-twentieth-record footer while the actual
        # rule remains elapsed time > 250 ms.
        ticks = iter(
            0x100000 + (index // 8) * 52
            for index in range(512)
        )
        with mock.patch.object(
            CarbonRebroadcasterService,
            "_server_tick_ms",
            side_effect=lambda: next(ticks),
        ):
            for _ in range(64):
                flow.service.gameplay_relay.relay_world_states(
                    replies,
                    flow.guest_addr,
                    binding,
                    world_batch,
                )

        for endpoint in (flow.host_addr, flow.guest_addr):
            active = [
                item
                for raw, target in replies
                if target == endpoint
                for item in session_tests._active_messages(raw)
            ]
            sequences = [item.sequence for item in active]
            self.assertEqual(len(sequences), 256)
            self.assertEqual(sequences[:4], [0x80, 0x81, 0x82, 0x83])
            self.assertEqual(sequences[-4:], [0xFC, 0xFD, 0xFE, 0xFF])
            footer_indexes = [
                index
                for index, item in enumerate(active)
                if item.payload[-1] == 0x45
            ]
            self.assertEqual(footer_indexes, list(range(0, 256, 20)))
            self.assertTrue(all(
                item.payload[-1] == (0x45 if index % 20 == 0 else 0x05)
                for index, item in enumerate(active)
            ))
            wire = flow.service._wire[endpoint]
            self.assertEqual(wire.next_server_virtual_sequence, 0x80)
            self.assertEqual(wire.next_server_sequence, reliable_before[endpoint])

    def test_v823_world_state_footer_requires_more_than_250_ms(self) -> None:
        flow = _finalized_flow()
        endpoint = flow.guest_addr
        wire = flow.service._wire[endpoint]
        wire.gameplay_ready = True
        wire.next_server_virtual_sequence = 0x80
        wire.last_world_state_footer_tick_ms = 0
        body = bytes.fromhex("000000000600000464112233445566778899")
        replies: list[tuple[bytes, tuple[str, int]]] = []

        with mock.patch.object(
            CarbonRebroadcasterService,
            "_server_tick_ms",
            side_effect=(0x100000, 0x1000FA, 0x1000FB),
        ):
            for _ in range(3):
                flow.service.gameplay_relay.append_virtual_world_bodies(
                    replies,
                    endpoint,
                    (body,),
                )

        active = [
            item
            for raw, target in replies
            if target == endpoint
            for item in session_tests._active_messages(raw)
        ]
        self.assertEqual([item.sequence for item in active], [0x80, 0x81, 0x82])
        self.assertEqual([item.payload[-1] for item in active], [0x45, 0x05, 0x45])

    def test_v822_world_footer_uses_raw_peer_clock_anchor_without_mutating_receive(self) -> None:
        flow = _finalized_flow()
        endpoint = flow.guest_addr
        wire = flow.service._wire[endpoint]
        wire.last_client_footer = bytes.fromhex(
            "00a2b41600a2b46d04958302"
        )
        wire.last_client_footer_received_tick_ms = 0x00A2B000
        wire.world_footer_remote_ack_tick_ms = 0x00A27B82
        wire.world_footer_received_tick_ms = 0x00A2AF00
        wire.world_footer_rtt_avg_ms = 900
        wire.world_footer_jitter_avg_ms = 470

        with self.assertLogs(
            "carbon.rebroadcaster.service",
            level="INFO",
        ) as captured:
            footer = flow.service.world_state.build_footer(
                wire,
                endpoint,
                flow.service._bindings[endpoint],
                local_now=0x00A2B416,
            )

        self.assertEqual(footer[:4], bytes.fromhex("00a2b883"))
        self.assertEqual(footer[4:8], bytes.fromhex("00a2b416"))
        self.assertEqual(footer[8:10], bytes.fromhex("02ad"))
        self.assertEqual(wire.world_footer_remote_ack_tick_ms, 0x00A27B82)
        self.assertEqual(wire.world_footer_received_tick_ms, 0x00A2AF00)
        self.assertEqual(wire.last_client_footer_received_tick_ms, 0x00A2B000)
        self.assertTrue(wire.world_footer_lag_repair_logged)
        repair_logs = [
            message
            for message in captured.output
            if "V822 raw peer-clock anchor selected" in message
        ]
        self.assertEqual(len(repair_logs), 1)
        self.assertIn("action=use-last-observed-peer-send-plus-elapsed", repair_logs[0])

    def test_v821_world_footer_uses_native_zero_seed_and_first_sample_reset(self) -> None:
        wire = EndpointWireState()
        self.assertEqual(wire.world_footer_rtt_avg_ms, 0)
        self.assertEqual(wire.world_footer_jitter_avg_ms, 0)

        # NFSC FUN_0098b640 starts words 8/9 at zero. FUN_0098b160 compares
        # the first receive tick against the zero-initialized previous tick;
        # a normal uptime therefore takes the >=0x400 reset branch.
        NetGameLinkWorldState.observe_footer(
            wire,
            bytes.fromhex("00100000000ffff000208302"),
            0x00100020,
        )

        self.assertEqual(wire.world_footer_rtt_avg_ms, 0x20)
        self.assertEqual(wire.world_footer_jitter_avg_ms, 0)
        self.assertEqual(wire.world_footer_observation_count, 1)
        self.assertEqual(wire.world_footer_received_tick_ms, 0x00100020)
        self.assertEqual(wire.last_client_footer_received_tick_ms, 0x00100020)
        self.assertEqual(wire.world_footer_remote_ack_tick_ms, 0x000FFFF0)

    def test_host_match_begins_timer_is_rebased_to_shared_clock_for_both_players(self) -> None:
        flow = _finalized_flow()
        before_timer = time.monotonic()
        malformed_host_timer = bytes.fromhex(
            "000000001b0000000244f2399641f40000"
        )
        replies = _send(
            flow.service,
            flow.host_addr,
            outer=90,
            sequence=0x12,
            acknowledgement=0x120,
            logical=malformed_host_timer,
        )
        timers = [
            (target, logical)
            for raw, target in replies
            for logical in _logical_replies([(raw, target)], target)
            if logical.startswith(bytes.fromhex("000000001b"))
        ]
        self.assertEqual({target for target, _ in timers}, {flow.host_addr, flow.guest_addr})
        rebased_clocks = []
        for _target, timer in timers:
            timer_id, shared_clock, duration = struct.unpack(">Iff", timer[5:17])
            self.assertEqual(timer_id, 2)
            self.assertLess(shared_clock, 1900.0)
            self.assertAlmostEqual(duration, 30.5)
            self.assertNotEqual(timer, malformed_host_timer)
            rebased_clocks.append(shared_clock)
        self.assertEqual(rebased_clocks[0], rebased_clocks[1])
        self.assertAlmostEqual(
            flow.service._race[flow.game.gid].countdown_deadline,
            before_timer + 30.5,
            delta=0.1,
        )

        guest_active = _send(
            flow.service,
            flow.guest_addr,
            outer=91,
            sequence=0x13,
            acknowledgement=0x120,
            logical=bytes.fromhex("000000001c00000000000f"),
        )
        self.assertFalse(any(
            logical.startswith(bytes.fromhex("000000001b"))
            for logical in _logical_replies(guest_active, flow.guest_addr)
        ))
        timer_retry = flow.service._wire[flow.guest_addr].match_timer_retry
        self.assertIsNotNone(timer_retry)
        assert timer_retry is not None
        timer_retry.retry_not_before = time.monotonic() - 0.01
        repeated_active = _send(
            flow.service,
            flow.guest_addr,
            outer=92,
            sequence=0x14,
            acknowledgement=0x121,
            logical=bytes.fromhex("000000001c0006706c617965720000000d"),
        )
        retried = [
            logical
            for logical in _logical_replies(repeated_active, flow.guest_addr)
            if logical.startswith(bytes.fromhex("000000001b"))
        ]
        self.assertEqual(len(retried), 1)
        retry_id, retry_clock, retry_duration = struct.unpack(">Iff", retried[0][5:17])
        original_id, original_clock, original_duration = struct.unpack(
            ">Iff",
            timers[0][1][5:17],
        )
        self.assertEqual(retry_id, original_id)
        self.assertGreaterEqual(retry_clock, original_clock)
        self.assertGreater(retry_duration, 0.5)
        self.assertLessEqual(retry_duration, original_duration)
        self.assertNotEqual(retried[0], timers[0][1])
        second_heartbeat = _send(
            flow.service,
            flow.guest_addr,
            outer=93,
            sequence=0x15,
            acknowledgement=0x122,
            logical=bytes.fromhex("000000001c0006706c617965720000000d"),
        )
        self.assertFalse(any(
            logical.startswith(bytes.fromhex("000000001b"))
            for logical in _logical_replies(second_heartbeat, flow.guest_addr)
        ))

    def test_invalid_match_begins_timer_is_ignored_without_breaking_room(self) -> None:
        flow = _finalized_flow()
        invalid_timer = bytes.fromhex("000000001b") + struct.pack(">Iff", 2, 10.0, 0.0)

        replies = _send(
            flow.service,
            flow.host_addr,
            outer=90,
            sequence=0x12,
            acknowledgement=0x120,
            logical=invalid_timer,
        )

        self.assertFalse(any(
            logical.startswith(bytes.fromhex("000000001b"))
            for logical in _logical_replies(replies)
        ))
        self.assertEqual(flow.service._race[flow.game.gid].latest_match_timer, b"")

    def test_guest_compound_ready_retries_exact_unacknowledged_timer_generation(self) -> None:
        flow = _finalized_flow()
        flow.game.properties["B-U-game_type"] = "2"
        flow.service._race[flow.game.gid].phase = RacePhase.SESSION_SETUP
        host_timer = bytes.fromhex("000000001b") + struct.pack(">Iff", 5, 130.0, 20.0)
        _send(
            flow.service,
            flow.host_addr,
            outer=90,
            sequence=0x12,
            acknowledgement=0x120,
            logical=host_timer,
        )
        race = flow.service._race[flow.game.gid]
        race.countdown_deadline = time.monotonic() + 12.0
        guest_wire = flow.service._wire[flow.guest_addr]
        self.assertIsNotNone(guest_wire.match_timer_retry)
        unacknowledged = (guest_wire.match_timer_sequence - 1) & 0x0FFFFFFF

        compound_ready = (
            bytes.fromhex("0000000014000130")
            + b"\x04"
            + bytes.fromhex("00000000180000")
        )
        replies = _send(
            flow.service,
            flow.guest_addr,
            outer=91,
            sequence=0x13,
            acknowledgement=unacknowledged,
            logical=compound_ready,
        )

        timers = [
            logical
            for logical in _logical_replies(replies, flow.guest_addr)
            if logical.startswith(bytes.fromhex("000000001b"))
        ]
        self.assertEqual(len(timers), 1)
        timer_id, _clock, duration = struct.unpack(">Iff", timers[0][5:17])
        self.assertEqual(timer_id, 5)
        self.assertAlmostEqual(duration, 20.0)
        self.assertEqual(timers[0], race.latest_match_timer)
        self.assertIsNone(guest_wire.match_timer_retry)

    def test_guest_timer_retry_is_suppressed_when_sequence_is_acked(self) -> None:
        flow = _finalized_flow()
        flow.game.properties["B-U-game_type"] = "2"
        flow.service._race[flow.game.gid].phase = RacePhase.SESSION_SETUP
        host_timer = bytes.fromhex("000000001b") + struct.pack(">Iff", 5, 130.0, 20.0)
        _send(
            flow.service,
            flow.host_addr,
            outer=90,
            sequence=0x12,
            acknowledgement=0x120,
            logical=host_timer,
        )
        guest_wire = flow.service._wire[flow.guest_addr]
        self.assertIsNotNone(guest_wire.match_timer_retry)
        replies = _send(
            flow.service,
            flow.guest_addr,
            outer=91,
            sequence=0x13,
            acknowledgement=guest_wire.match_timer_sequence,
            logical=bytes.fromhex("00000000140001300400000000180000"),
        )
        self.assertFalse(any(
            logical.startswith(bytes.fromhex("000000001b"))
            for logical in _logical_replies(replies, flow.guest_addr)
        ))
        self.assertIsNone(guest_wire.match_timer_retry)

    def test_official_room_wait_timer_is_relayed_without_starting_race_countdown(self) -> None:
        flow = _finalized_flow()
        race = flow.service._race[flow.game.gid]
        race.phase = RacePhase.SESSION_SETUP
        race.countdown_deadline = 0.0
        race.latest_match_timer = b""
        before = time.monotonic()
        snapshot = bytes.fromhex("000000001b") + struct.pack(">Iff", 0, 7.87025, 600.5)
        replies = []
        flow.service._broadcast_room_timer(replies, flow.game, snapshot)

        self.assertEqual(race.phase, RacePhase.SESSION_SETUP)
        self.assertEqual(race.latest_match_timer, b"")
        self.assertTrue(race.latest_room_timer.startswith(bytes.fromhex("000000001b")))
        self.assertAlmostEqual(race.room_wait_deadline, before + 600.5, delta=0.1)
        timers = [
            (target, logical)
            for raw, target in replies
            for logical in _logical_replies([(raw, target)], target)
            if logical.startswith(bytes.fromhex("000000001b"))
        ]
        self.assertEqual({target for target, _ in timers}, {flow.host_addr, flow.guest_addr})
        for _target, timer in timers:
            timer_id, _shared_clock, duration = struct.unpack(">Iff", timer[5:17])
            self.assertEqual(timer_id, 0)
            self.assertAlmostEqual(duration, 600.5)

    def test_dedicated_helper_cannot_author_room_wait_timer(self) -> None:
        flow = _finalized_flow()
        flow.game.server_hosted = True
        flow.game.session.capacity = 2
        flow.game.properties["B-U-game_type"] = "2"
        race = flow.service._race[flow.game.gid]
        race.latest_room_timer = b""
        snapshot = bytes.fromhex("000000001b") + struct.pack(">Iff", 0, 18.54, 600.5)

        helper_replies: list[tuple[bytes, tuple[str, int]]] = []
        flow.service._broadcast_room_timer(
            helper_replies,
            flow.game,
            snapshot,
            source=flow.guest_addr,
        )

        self.assertEqual(helper_replies, [])
        self.assertEqual(race.latest_room_timer, b"")

        host_replies: list[tuple[bytes, tuple[str, int]]] = []
        flow.service._broadcast_room_timer(
            host_replies,
            flow.game,
            snapshot,
            source=flow.host_addr,
        )
        self.assertTrue(race.latest_room_timer.startswith(bytes.fromhex("000000001b")))
        self.assertEqual(
            {target for _raw, target in host_replies},
            {flow.host_addr, flow.guest_addr},
        )

    def test_official_timer_ids_separate_countdown_and_post_race_windows(self) -> None:
        flow = _finalized_flow()
        race = flow.service._race[flow.game.gid]
        race.phase = RacePhase.SESSION_SETUP
        before = time.monotonic()

        countdown = bytes.fromhex("000000001b") + struct.pack(">Iff", 5, 20.112, 20.5)
        countdown_replies = []
        flow.service._broadcast_room_timer(countdown_replies, flow.game, countdown)
        self.assertEqual(race.phase, RacePhase.COUNTDOWN)
        self.assertAlmostEqual(race.countdown_deadline, before + 20.5, delta=0.1)
        self.assertEqual(race.latest_match_timer, countdown)
        self.assertEqual(race.countdown_initial_timer, countdown)
        self.assertEqual(race.countdown_latest_timer, countdown)
        self.assertEqual(race.countdown_generation_id, 1)
        self.assertAlmostEqual(race.countdown_wire_deadline, 40.612, places=3)
        countdown_timers = [
            logical
            for logical in _logical_replies(countdown_replies)
            if logical.startswith(bytes.fromhex("000000001b"))
        ]
        self.assertEqual(countdown_timers, [countdown, countdown])

        phase_before_post = race.phase
        post = bytes.fromhex("000000001b") + struct.pack(">Iff", 4, 197.712, 120.5)
        flow.service._broadcast_room_timer([], flow.game, post)
        self.assertEqual(race.phase, phase_before_post)
        self.assertEqual(int.from_bytes(race.latest_post_race_timer[5:9], "big"), 4)
        self.assertAlmostEqual(race.post_race_deadline, time.monotonic() + 120.5, delta=0.1)

    def test_retail_timer5_refinement_keeps_one_wire_generation(self) -> None:
        flow = _finalized_flow()
        race = flow.service._race[flow.game.gid]
        seed = bytes.fromhex("000000001b") + struct.pack(
            ">Iff",
            5,
            33.894001,
            20.5,
        )
        refinement = bytes.fromhex("000000001b") + struct.pack(
            ">Iff",
            5,
            34.518002,
            19.881001,
        )

        first_generation, first_deadline, first_drift = (
            flow.service._record_countdown_wire_timer(race, seed)
        )
        second_generation, second_deadline, second_drift = (
            flow.service._record_countdown_wire_timer(race, refinement)
        )

        self.assertEqual(first_generation, 1)
        self.assertEqual(second_generation, 1)
        self.assertEqual(first_drift, 0.0)
        self.assertAlmostEqual(first_deadline, 54.394001, places=5)
        self.assertAlmostEqual(second_deadline, 54.399003, places=5)
        self.assertAlmostEqual(second_drift, 0.005002, places=5)
        self.assertEqual(race.countdown_initial_timer, seed)
        self.assertEqual(race.countdown_latest_timer, refinement)



    def test_retail_player_countdown_compounds_match_official_bytes(self) -> None:
        state7 = anonymous_state(7)
        state14 = named_state("player", 14)
        self.assertEqual(
            (state14 + b"\x04" + state7 + b"\x04").hex(),
            "000000001c0006706c617965720000000e04000000001c00000000000704",
        )
        locked_chain = (
            locked_host_properties(2, wire_flag0=False).encode()
            + b"\x04"
            + state14
            + b"\x04"
            + bytes((len(state14) + 1,))
            + state7
            + b"\x04"
        )
        self.assertEqual(
            locked_chain.hex(),
            "018c00000082800000000204000000001c0006706c617965720000000e"
            "0412000000001c00000000000704",
        )

    def test_coop_countdown_relays_one_safe_timer_without_synthetic_bundle(self) -> None:
        flow = _finalized_flow()
        flow.game.session.capacity = 2
        flow.game.properties.update(
            {
                "B-U-game_type": "2",
                "B-U-matchmaking_state": "0",
                "B-U-help_type": "0",
                "B-U-game_mode": "0",
                "B-U-car_tier": "3",
                "B-U-max_online_player": "2",
                "B-U-race_type_sprint": "cs.2.3",
            }
        )
        race = flow.service._race[flow.game.gid]
        race.phase = RacePhase.SESSION_SETUP
        race.room_commit_sent = True
        race.countdown_transition_sent = False
        race.attributes = session_attributes(flow.game.properties)

        snapshot = bytes.fromhex("000000001b") + struct.pack(">Iff", 5, 15.740, 20.5)
        replies: list[tuple[bytes, tuple[str, int]]] = []
        flow.service._broadcast_room_timer(replies, flow.game, snapshot)

        self.assertEqual(race.phase, RacePhase.COUNTDOWN)
        self.assertFalse(race.countdown_transition_sent)
        for destination in (flow.host_addr, flow.guest_addr):
            active = [
                message
                for raw, target in replies
                if target == destination
                for message in session_tests._active_messages(raw)
            ]
            self.assertEqual(len(active), 1)
            self.assertEqual(active[0].sequence >> 28, 0)
            body = game_manager_body(active[0].payload)
            self.assertEqual(int.from_bytes(body[5:9], "big"), 5)
            self.assertAlmostEqual(struct.unpack(">f", body[13:17])[0], 20.5, places=3)
            self.assertTrue(active[0].payload.endswith(b"\x04"))


if __name__ == "__main__":
    unittest.main()

class CarbonCountdownContextRelayTests(unittest.TestCase):
    def test_activegame_countdown_compounds_are_reflected_to_host_and_guest(self) -> None:
        flow = _finalized_flow()
        compound = (
            bytes.fromhex("000000001c0004486f73740000000e")
            + b"\x04"
            + bytes.fromhex("000000001c000000000007")
        )
        replies = _send(
            flow.service,
            flow.host_addr,
            outer=120,
            sequence=0x30,
            acknowledgement=0x140,
            logical=compound,
        )
        reflected = {
            target: [
                game_manager_body(active.payload)
                for raw, destination in replies
                if destination == target
                for active in session_tests._active_messages(raw)
                if game_manager_body(active.payload).startswith(compound)
            ]
            for target in (flow.host_addr, flow.guest_addr)
        }
        self.assertEqual(reflected[flow.host_addr], [compound])
        self.assertEqual(reflected[flow.guest_addr], [compound])


class CarbonJoinerAllocationWindowTests(unittest.TestCase):
    def test_host_generation_does_not_lock_dedicated_room(self) -> None:
        flow = _finalized_flow()
        flow.game.server_hosted = True
        flow.game.properties["B-U-game_type"] = "2"
        race = flow.service._race[flow.game.gid]
        race.room_access = RoomAccess.OPEN
        wire = flow.service._wire[flow.host_addr]

        for index, offset in enumerate((0, 0x1E4, 0x3C8), start=230):
            body = bytearray(session_tests._session_block(1, "Host", offset))
            body[5:9] = (33).to_bytes(4, "big")
            _send(
                flow.service,
                flow.host_addr,
                outer=index,
                sequence=index,
                acknowledgement=0x140,
                logical=bytes(body),
            )

        self.assertGreaterEqual(wire.session_generation, 2)
        self.assertFalse(wire.allocation_lock_triggered)
        self.assertEqual(race.room_access, RoomAccess.OPEN)

    def test_new_session_object_generation_does_not_reuse_old_offsets(self) -> None:
        flow = _finalized_flow()
        flow.game.server_hosted = True
        flow.game.session.capacity = 2
        flow.game.properties["B-U-game_type"] = "2"
        race = flow.service._race[flow.game.gid]
        race.room_access = RoomAccess.OPEN
        # Host state 7 can arrive before generation 2 completes.  It is not a
        # substitute for the helper's real generation 3 allocation window.
        race.coop_host_state7_seen = True
        wire = flow.service._wire[flow.guest_addr]

        def generation_block(offset: int) -> bytes:
            body = bytearray(session_tests._session_block(2, "Guest", offset))
            body[5:9] = (22).to_bytes(4, "big")
            return bytes(body)

        first_replies = _send(
            flow.service,
            flow.guest_addr,
            outer=130,
            sequence=0x220,
            acknowledgement=0x140,
            logical=generation_block(0),
        )
        self.assertEqual(wire.session_object_id, 22)
        self.assertEqual(wire.session_generation, 2)
        self.assertEqual(set(wire.session_blocks), {0})
        self.assertFalse(any(
            body.startswith(bytes.fromhex("000000001e"))
            for body in _logical_replies(first_replies, flow.guest_addr)
        ))

        _send(
            flow.service,
            flow.guest_addr,
            outer=131,
            sequence=0x221,
            acknowledgement=0x140,
            logical=generation_block(0x1E4),
        )
        held_replies = _send(
            flow.service,
            flow.guest_addr,
            outer=132,
            sequence=0x222,
            acknowledgement=0x140,
            logical=generation_block(0x3C8),
        )
        self.assertEqual(set(wire.session_blocks), {0, 0x1E4, 0x3C8})
        self.assertEqual(
            {int.from_bytes(body[5:9], "big") for body in wire.session_blocks.values()},
            {22},
        )
        self.assertEqual(len(wire.pending_allocation_blocks), 3)
        self.assertFalse(wire.allocation_lock_triggered)
        self.assertEqual(race.room_access, RoomAccess.OPEN)
        self.assertFalse(any(
            body.startswith(bytes.fromhex("000000001e"))
            for body in _logical_replies(held_replies, flow.guest_addr)
        ))

        # A following non-session datagram revisits the still-complete object
        # from _finish_bound_datagram.  The allocation hold must remain
        # idempotently active; otherwise generation 2 leaks through the
        # ordinary local/remote reflector before the client emits generation 3.
        held_reentry_replies: list[tuple[bytes, tuple[str, int]]] = []
        flow.service.invite_session.handle_complete_session_object(
            held_reentry_replies,
            flow.guest_addr,
            flow.service._bindings[flow.guest_addr],
        )
        self.assertEqual(len(wire.pending_allocation_blocks), 3)
        self.assertFalse(any(
            body.startswith(bytes.fromhex("000000001e"))
            for endpoint in (flow.guest_addr, flow.host_addr)
            for body in _logical_replies(held_reentry_replies, endpoint)
        ))

        def next_generation_block(offset: int) -> bytes:
            body = bytearray(session_tests._session_block(2, "Guest", offset))
            body[5:9] = (23).to_bytes(4, "big")
            return bytes(body)

        partial_replies = _send(
            flow.service,
            flow.guest_addr,
            outer=133,
            sequence=0x223,
            acknowledgement=0x140,
            logical=next_generation_block(0),
        )
        self.assertFalse(wire.allocation_lock_triggered)
        self.assertFalse(any(
            active.game_manager is not None
            and active.game_manager.message_type == 0x0C
            for raw, _target in partial_replies
            for active in session_tests._active_messages(raw)
        ))
        self.assertFalse(any(
            body.startswith(bytes.fromhex("000000001e"))
            for body in _logical_replies(partial_replies, flow.guest_addr)
        ))

        offset_zero_replies = _send(
            flow.service,
            flow.guest_addr,
            outer=134,
            sequence=0x224,
            acknowledgement=0x140,
            logical=next_generation_block(0x1E4),
        )
        self.assertEqual(wire.session_object_id, 23)
        self.assertEqual(wire.session_generation, 3)
        self.assertEqual(set(wire.session_blocks), {0, 0x1E4})
        self.assertEqual(len(wire.pending_allocation_blocks), 3)
        self.assertTrue(wire.pending_allocation_offset_zero_sent)
        self.assertFalse(wire.allocation_lock_triggered)
        self.assertEqual(race.room_access, RoomAccess.OPEN)
        offset_zero = [
            body
            for body in _logical_replies(offset_zero_replies, flow.guest_addr)
            if body.startswith(bytes.fromhex("000000001e"))
        ]
        self.assertEqual(len(offset_zero), 1)
        self.assertEqual(int.from_bytes(offset_zero[0][13:17], "big"), 0)
        self.assertFalse(any(
            active.game_manager is not None
            and active.game_manager.message_type == 0x0C
            for raw, _target in offset_zero_replies
            for active in session_tests._active_messages(raw)
        ))

        release_replies = _send(
            flow.service,
            flow.guest_addr,
            outer=135,
            sequence=0x225,
            acknowledgement=0x140,
            logical=next_generation_block(0x3C8),
        )
        self.assertEqual(wire.pending_allocation_blocks, ())
        self.assertFalse(wire.pending_allocation_offset_zero_sent)
        reflected = [
            body
            for body in _logical_replies(release_replies, flow.guest_addr)
            if body.startswith(bytes.fromhex("000000001e"))
        ]
        # The allocation response owns only held generation 2.  Generation 3
        # is a dependent reliable window and must wait until both destinations
        # acknowledge the six-record allocation bundle.
        self.assertEqual(len(reflected), 2)
        self.assertTrue(wire.allocation_lock_triggered)
        self.assertEqual(
            set(wire.allocation_release_final_sequences),
            {flow.guest_addr, flow.host_addr},
        )
        self.assertEqual(wire.allocation_reflection_final_sequences, {})
        self.assertEqual(race.room_access, RoomAccess.LOCKED)
        self.assertEqual(release_replies[0][1], flow.guest_addr)
        first_messages = session_tests._active_messages(release_replies[0][0])
        self.assertEqual(len(first_messages), 6)
        lows = [message.sequence & 0x0FFFFFFF for message in first_messages]
        self.assertEqual(lows, [lows[0] + offset for offset in range(6)])
        self.assertEqual(
            [message.sequence >> 28 for message in first_messages],
            [0, 0, 0, 1, 2, 3],
        )
        first_types = [
            message.game_manager.message_type
            for message in first_messages
            if message.game_manager is not None
        ]
        self.assertTrue(first_types)
        self.assertEqual(first_types, [0x0C, 0x0C, 0x0C, 0x0C])
        self.assertEqual(
            [
                message.game_manager.body[:11].hex()
                for message in first_messages
                if message.game_manager is not None
            ],

            [
                "018c000101808000000002",
                "018c000100808000000002",
                "018c000000808000000002",
                "018c000000828000000002",
            ],
        )

        completed_replies = _send(
            flow.service,
            flow.guest_addr,
            outer=135,
            sequence=0x225,
            acknowledgement=0x140,
            logical=next_generation_block(0x3C8),
        )
        current_reflection = [
            body
            for body in _logical_replies(completed_replies, flow.guest_addr)
            if body.startswith(bytes.fromhex("000000001e"))
        ]
        self.assertEqual(current_reflection, [])

        for endpoint, target in wire.allocation_release_final_sequences.items():
            flow.service._wire[endpoint].last_client_acknowledgement = target
        generation_three_replies: list[tuple[bytes, tuple[str, int]]] = []
        blocked = flow.service.room_commit.advance_helper_generation_barrier(
            generation_three_replies,
            flow.game,
            current_address=flow.guest_addr,
        )
        self.assertTrue(blocked)
        self.assertEqual(wire.allocation_release_final_sequences, {})
        self.assertEqual(
            set(wire.allocation_reflection_final_sequences),
            {flow.guest_addr, flow.host_addr},
        )
        for endpoint in (flow.guest_addr, flow.host_addr):
            reflected_generation_three = [
                body
                for body in _logical_replies(
                    generation_three_replies,
                    endpoint,
                )
                if body.startswith(bytes.fromhex("000000001e"))
            ]
            self.assertEqual(len(reflected_generation_three), 3)

        for endpoint, target in wire.allocation_reflection_final_sequences.items():
            flow.service._wire[endpoint].last_client_acknowledgement = target
        final_replies: list[tuple[bytes, tuple[str, int]]] = []
        blocked = flow.service.room_commit.advance_helper_generation_barrier(
            final_replies,
            flow.game,
            current_address=flow.guest_addr,
        )
        self.assertFalse(blocked)
        self.assertEqual(wire.allocation_reflection_final_sequences, {})
        self.assertFalse(any(
            body.startswith(bytes.fromhex("000000001e"))
            for endpoint in (flow.guest_addr, flow.host_addr)
            for body in _logical_replies(final_replies, endpoint)
        ))

    def test_preconfirm_generation_two_survives_same_datagram_generation_three(self) -> None:
        flow = _finalized_flow()
        flow.game.server_hosted = True
        flow.game.session.capacity = 2
        flow.game.properties["B-U-game_type"] = "2"
        wire = flow.service._wire[flow.guest_addr]
        binding = flow.service._bindings[flow.guest_addr]
        wire.session_confirmed = False
        wire.session_confirmation_pending = False
        wire.session_generation = 2
        wire.session_object_id = 22
        wire.pending_allocation_blocks = ()
        wire.pending_allocation_offset_zero_sent = False
        wire.allocation_lock_triggered = False

        def block(object_id: int, offset: int) -> bytes:
            body = bytearray(
                session_tests._session_block(2, "Guest", offset)
            )
            body[5:9] = int(object_id).to_bytes(4, "big")
            return bytes(body)

        wire.session_blocks = {
            0: block(22, 0),
            0x1E4: block(22, 0x1E4),
        }
        replies: list[tuple[bytes, tuple[str, int]]] = []
        # A slow-frame client can finish generation 2 and start generation 3
        # in one UDP receive. Preserve generation 2 before the ordinary
        # object-transition cleanup clears the active fragment map.
        flow.service._ingest_session_blocks(
            replies,
            flow.guest_addr,
            wire,
            block(22, 0x3C8),
        )
        flow.service._ingest_session_blocks(
            replies,
            flow.guest_addr,
            wire,
            block(23, 0),
        )

        self.assertEqual(wire.session_generation, 3)
        self.assertEqual(wire.session_object_id, 23)
        self.assertEqual(set(wire.session_blocks), {0})
        self.assertEqual(wire.pending_allocation_object_id, 22)
        self.assertEqual(len(wire.pending_allocation_blocks), 3)
        self.assertEqual(replies, [])

        flow.service._ingest_session_blocks(
            replies,
            flow.guest_addr,
            wire,
            block(23, 0x1E4),
        )
        flow.service._ingest_session_blocks(
            replies,
            flow.guest_addr,
            wire,
            block(23, 0x3C8),
        )
        self.assertFalse(wire.pending_allocation_offset_zero_sent)
        self.assertFalse(wire.allocation_lock_triggered)
        self.assertEqual(replies, [])

        wire.session_confirmed = True
        offset_zero_replies: list[tuple[bytes, tuple[str, int]]] = []
        flow.service.room_commit.release_pending_helper_allocation(
            offset_zero_replies,
            flow.guest_addr,
            binding,
        )
        self.assertTrue(wire.pending_allocation_offset_zero_sent)
        self.assertFalse(wire.allocation_lock_triggered)
        held_zero = [
            body
            for body in _logical_replies(
                offset_zero_replies,
                flow.guest_addr,
            )
            if body.startswith(bytes.fromhex("000000001e"))
        ]
        self.assertEqual(len(held_zero), 1)
        self.assertEqual(int.from_bytes(held_zero[0][13:17], "big"), 0)

        allocation_replies: list[tuple[bytes, tuple[str, int]]] = []
        flow.service.room_commit.release_pending_helper_allocation(
            allocation_replies,
            flow.guest_addr,
            binding,
        )
        self.assertTrue(wire.allocation_lock_triggered)
        self.assertEqual(wire.pending_allocation_blocks, ())
        self.assertTrue(any(
            active.game_manager is not None
            and active.game_manager.message_type == 0x0C
            for raw, _target in allocation_replies
            for active in session_tests._active_messages(raw)
        ))

    def test_host_state7_releases_stalled_complete_generation2(self) -> None:
        flow = _finalized_flow()
        flow.game.server_hosted = True
        flow.game.session.capacity = 2
        flow.game.properties["B-U-game_type"] = "2"
        race = flow.service._race[flow.game.gid]
        race.room_access = RoomAccess.OPEN
        wire = flow.service._wire[flow.guest_addr]

        def generation_block(offset: int) -> bytes:
            body = bytearray(session_tests._session_block(2, "Guest", offset))
            body[5:9] = (22).to_bytes(4, "big")
            return bytes(body)

        for index, offset in enumerate((0, 0x1E4, 0x3C8), start=130):
            _send(
                flow.service,
                flow.guest_addr,
                outer=index,
                sequence=0x220 + index - 130,
                acknowledgement=0x140,
                logical=generation_block(offset),
            )

        self.assertEqual(wire.session_generation, 2)
        self.assertEqual(len(wire.pending_allocation_blocks), 3)
        self.assertFalse(wire.allocation_lock_triggered)

        race.coop_host_state7_seen = True
        replies: list[tuple[bytes, tuple[str, int]]] = []
        flow.service.room_commit.maybe_finalize_room_session(
            replies,
            flow.game,
            barrier_host=flow.host_addr,
            barrier_token=bytes.fromhex("01020304"),
        )

        self.assertTrue(wire.allocation_lock_triggered)
        self.assertEqual(wire.pending_allocation_blocks, ())
        self.assertEqual(wire.session_generation, 2)
        self.assertEqual(race.room_access, RoomAccess.LOCKED)
        self.assertFalse(race.room_commit_sent)

        guest_bundles = [
            session_tests._active_messages(raw)
            for raw, target in replies
            if target == flow.guest_addr
            and len(session_tests._active_messages(raw)) == 6
        ]
        host_bundles = [
            session_tests._active_messages(raw)
            for raw, target in replies
            if target == flow.host_addr
            and len(session_tests._active_messages(raw)) == 6
        ]
        self.assertEqual(len(guest_bundles), 1)
        self.assertEqual(len(host_bundles), 1)
        self.assertEqual(
            [message.sequence >> 28 for message in guest_bundles[0]],
            [0, 0, 0, 1, 2, 3],
        )

        reply_count = len(replies)
        self.assertFalse(
            flow.service.room_commit.release_stalled_helper_allocation_on_state7(
                replies,
                flow.guest_addr,
                flow.service._bindings[flow.guest_addr],
            )
        )
        self.assertEqual(len(replies), reply_count)

    def test_guest_local_object_is_reflected_before_session_confirmation(self) -> None:
        flow = _finalized_flow()
        flow.game.properties["B-U-game_type"] = "2"
        guest_binding = flow.service._bindings[flow.guest_addr]
        invited_participant = replace(
            guest_binding.participant,
            invite_remote_player_id=flow.service._bindings[
                flow.host_addr
            ].participant.player_id,
        )
        flow.service._bindings[flow.guest_addr] = replace(
            guest_binding,
            participant=invited_participant,
        )
        wire = flow.service._wire[flow.guest_addr]
        wire.session_confirmed = False
        wire.session_confirmation_pending = False
        wire.session_token = b""
        wire.published_session_offsets.pop(
            flow.service._source_key(flow.service._bindings[flow.guest_addr]),
            None,
        )

        replies = _send(
            flow.service,
            flow.guest_addr,
            outer=120,
            sequence=0x1FF,
            acknowledgement=0x140,
            logical=bytes.fromhex("0000000002") + bytes.fromhex("01020304"),
        )

        guest_bodies = [
            game_manager_body(item.payload)
            for raw, target in replies
            if target == flow.guest_addr
            for item in session_tests._active_messages(raw)
        ]
        object_indexes = [
            index
            for index, body in enumerate(guest_bodies)
            if body.startswith(bytes.fromhex("000000001e"))
        ]
        confirmation_index = next(
            index
            for index, body in enumerate(guest_bodies)
            if body.startswith(bytes.fromhex("0000000003"))
        )
        self.assertEqual(len(object_indexes), 3)
        self.assertLess(max(object_indexes), confirmation_index)
        uploaded_object_id = int.from_bytes(next(iter(wire.session_blocks.values()))[5:9], "big")
        reflected_ids = {
            int.from_bytes(guest_bodies[index][5:9], "big")
            for index in object_indexes
        }
        self.assertEqual(reflected_ids, {wire.local_reflected_object_id})
        self.assertNotEqual(wire.local_reflected_object_id, uploaded_object_id)
    def test_guest_state13_is_intercepted_by_datagram_pipeline(self) -> None:
        flow = _finalized_flow()
        flow.game.properties["B-U-game_type"] = "2"
        race = flow.service._race[flow.game.gid]
        race.phase = RacePhase.SESSION_SETUP
        race.room_commit_sent = True
        state13 = named_state("Guest", 13)

        guest_wire = flow.service._wire[flow.guest_addr]
        host_wire = flow.service._wire[flow.host_addr]
        guest_wire.next_server_sequence = 0x122
        guest_wire.last_client_sequence = 0x113
        client_footer = bytes.fromhex("00496faa111111110019d402")
        expected_guest_footer = bytes.fromhex("00496faa27fc991b00000000")
        guest_wire.footer = bytes.fromhex("aaaaaaaaaaaaaaaaaaaaaaaa")
        guest_wire.latest_latency_info = bytes.fromhex(
            "00000000120000005a41e80000"
        )
        host_wire.next_server_sequence = 0x141
        host_wire.last_client_sequence = 0x12E
        host_wire.footer = bytes.fromhex("00496f7527fc991b01aacc00")
        host_wire.latest_latency_info = bytes.fromhex(
            "00000000120000005744000000"
        )

        state5 = named_state("Guest", 5)
        client_state13_record = state13 + client_footer + b"\x44"
        request = TunnelDatagram(
            121,
            (
                TunnelPacket(
                    1,
                    (0x112).to_bytes(4, "big")
                    + (0x121).to_bytes(4, "big")
                    + client_state13_record,
                ),
                TunnelPacket(
                    1,
                    (0x10000113).to_bytes(4, "big")
                    + (0x121).to_bytes(4, "big")
                    + state5
                    + b"\x04"
                    + client_state13_record
                    + bytes((len(client_state13_record),)),
                ),
            ),
        ).encode(session_tests.EKEY)
        with mock.patch.object(
            CarbonRebroadcasterService,
            "_server_tick_ms",
            return_value=0x27FC991B,
        ):
            replies = flow.service.handle_datagram(request, flow.guest_addr)

        self.assertIn(
            (flow.game.gid, flow.guest.user_id),
            flow.service._joiner_state13_window_sent,
        )
        self.assertEqual(len(replies), 2)
        self.assertEqual(
            [target for _raw, target in replies],
            [flow.guest_addr, flow.host_addr],
        )
        guest_outbound = [
            item
            for raw, target in replies
            if target == flow.guest_addr
            for item in session_tests._active_messages(raw)
        ]
        host_outbound = [
            item
            for raw, target in replies
            if target == flow.host_addr
            for item in session_tests._active_messages(raw)
        ]
        self.assertEqual([item.sequence >> 28 for item in guest_outbound], [0, 1])
        self.assertEqual([item.sequence >> 28 for item in host_outbound], [2, 2])
        self.assertEqual(
            guest_outbound[0].payload.hex(),
            "0000012200000113"
            "000000001c000547756573740000000d"
            + expected_guest_footer.hex()
            + "44",
        )
        self.assertEqual(
            guest_outbound[1].payload.hex(),
            "1000012300000113"
            "000000001c0005477565737400000005"
            "04"
            "000000001c000547756573740000000d"
            + expected_guest_footer.hex()
            + "441d",
        )
        self.assertEqual(
            host_outbound[0].payload.hex(),
            "200001410000012e"
            "000000001c000547756573740000000d"
            "00496f7527fc991b01aacc0044"
            "00000000120000005a41e80000040e"
            "00000000120000005744000000040e",
        )
        self.assertEqual(
            host_outbound[1].payload.hex(),
            "200001420000012e"
            "000000001c0005477565737400000005"
            "04"
            "000000001c000547756573740000000d"
            "00496f7527fc991b01aacc00441d"
            "00000000120000005a41e80000040e",
        )
        self.assertEqual(guest_wire.next_server_sequence, 0x124)
        self.assertEqual(host_wire.next_server_sequence, 0x143)
        self.assertEqual(guest_wire.footer, expected_guest_footer)
        for outbound in (*guest_outbound, *host_outbound):
            current, history = _native_commudp_records(outbound)
            self.assertTrue(current)
            self.assertEqual(
                len(history),
                (int(outbound.sequence) >> 28) & 0x0F,
            )

    def test_joiner_history_length_avoids_live_v786_minus_nine(self) -> None:
        flow = _finalized_flow()
        flow.game.properties["B-U-game_type"] = "2"
        race = flow.service._race[flow.game.gid]
        race.phase = RacePhase.SESSION_SETUP
        race.room_commit_sent = True

        guest_binding = flow.service._bindings[flow.guest_addr]
        identity = replace(
            guest_binding.participant.identity,
            account_name="testdriver",
            persona="testdriver",
        )
        flow.service._bindings[flow.guest_addr] = replace(
            guest_binding,
            participant=replace(guest_binding.participant, identity=identity),
        )

        guest_wire = flow.service._wire[flow.guest_addr]
        host_wire = flow.service._wire[flow.host_addr]
        guest_wire.next_server_sequence = 0x113
        guest_wire.last_client_sequence = 0x114
        guest_wire.latest_latency_info = bytes.fromhex(
            "000000001200000ce900000000"
        )
        host_wire.next_server_sequence = 0x152
        host_wire.last_client_sequence = 0x119
        host_wire.footer = bytes.fromhex("04248b0904248c1100000000")
        host_wire.latest_latency_info = bytes.fromhex(
            "00000000120000462200000000"
        )

        state13 = named_state("testdriver", 13)
        state5 = named_state("testdriver", 5)
        client_footer = bytes.fromhex("04248b09111111110084d402")
        expected_guest_footer = bytes.fromhex("04248b0904248c1100000000")
        client_state13_record = state13 + client_footer + b"\x44"
        request = TunnelDatagram(
            126,
            (
                TunnelPacket(
                    1,
                    (0x113).to_bytes(4, "big")
                    + (0x11A).to_bytes(4, "big")
                    + client_state13_record,
                ),
                TunnelPacket(
                    1,
                    (0x10000114).to_bytes(4, "big")
                    + (0x11A).to_bytes(4, "big")
                    + state5
                    + b"\x04"
                    + client_state13_record
                    + bytes((len(client_state13_record),)),
                ),
            ),
        ).encode(session_tests.EKEY)
        with mock.patch.object(
            CarbonRebroadcasterService,
            "_server_tick_ms",
            return_value=0x04248C11,
        ):
            replies = flow.service.handle_datagram(request, flow.guest_addr)

        self.assertEqual(len(replies), 2)
        guest_outbound = [
            item
            for raw, target in replies
            if target == flow.guest_addr
            for item in session_tests._active_messages(raw)
        ]
        self.assertEqual([item.sequence >> 28 for item in guest_outbound], [0, 1])
        current, history = _native_commudp_records(guest_outbound[1])
        expected_history = state13 + expected_guest_footer + b"\x44"
        self.assertEqual(current, state5 + b"\x04")
        self.assertEqual(history, (expected_history,))
        self.assertEqual(len(expected_history), 0x22)
        self.assertEqual(guest_outbound[1].payload[-1], 0x22)

        # Exact V786 live body: 56 bytes ending in 0x40. NFSC computes -9.
        old_v786_body = state5 + b"\x04" + state13 + expected_guest_footer + b"\x40"
        self.assertEqual(len(old_v786_body), 56)
        self.assertEqual(len(old_v786_body) - 1 - old_v786_body[-1], -9)

    def test_joiner_clean_state13_alone_completes_split_safe_window(self) -> None:
        flow = _finalized_flow()
        flow.game.properties["B-U-game_type"] = "2"
        race = flow.service._race[flow.game.gid]
        race.phase = RacePhase.SESSION_SETUP
        race.room_commit_sent = True

        guest_wire = flow.service._wire[flow.guest_addr]
        host_wire = flow.service._wire[flow.host_addr]
        guest_wire.next_server_sequence = 0x122
        guest_wire.last_client_sequence = 0x113
        guest_wire.latest_latency_info = bytes.fromhex(
            "00000000120000005a41e80000"
        )
        host_wire.next_server_sequence = 0x141
        host_wire.last_client_sequence = 0x12E
        host_wire.latest_latency_info = bytes.fromhex(
            "00000000120000005744000000"
        )

        replies = _send(
            flow.service,
            flow.guest_addr,
            outer=121,
            sequence=0x113,
            acknowledgement=0x121,
            logical=named_state("Guest", 13),
        )

        self.assertIn(
            (flow.game.gid, flow.guest.user_id),
            flow.service._joiner_state13_window_sent,
        )
        self.assertEqual([target for _raw, target in replies], [
            flow.guest_addr,
            flow.host_addr,
        ])
        guest_outbound = [
            item
            for raw, target in replies
            if target == flow.guest_addr
            for item in session_tests._active_messages(raw)
        ]
        host_outbound = [
            item
            for raw, target in replies
            if target == flow.host_addr
            for item in session_tests._active_messages(raw)
        ]
        self.assertEqual([item.sequence >> 28 for item in guest_outbound], [0, 1])
        self.assertEqual([item.sequence >> 28 for item in host_outbound], [2, 2])

    def test_joiner_history_companion_alone_completes_split_safe_window(self) -> None:
        flow = _finalized_flow()
        flow.game.properties["B-U-game_type"] = "2"
        race = flow.service._race[flow.game.gid]
        race.phase = RacePhase.SESSION_SETUP
        race.room_commit_sent = True

        guest_wire = flow.service._wire[flow.guest_addr]
        host_wire = flow.service._wire[flow.host_addr]
        guest_wire.next_server_sequence = 0x122
        guest_wire.last_client_sequence = 0x113
        guest_wire.latest_latency_info = bytes.fromhex(
            "00000000120000005a41e80000"
        )
        host_wire.next_server_sequence = 0x141
        host_wire.last_client_sequence = 0x12E
        host_wire.latest_latency_info = bytes.fromhex(
            "00000000120000005744000000"
        )

        state13 = named_state("Guest", 13)
        state5 = named_state("Guest", 5)
        client_record = state13 + bytes.fromhex("00496faa111111110019d40244")
        request = TunnelDatagram(
            121,
            (
                TunnelPacket(
                    1,
                    (0x10000113).to_bytes(4, "big")
                    + (0x121).to_bytes(4, "big")
                    + state5
                    + b"\x04"
                    + client_record
                    + bytes((len(client_record),)),
                ),
            ),
        ).encode(session_tests.EKEY)
        replies = flow.service.handle_datagram(request, flow.guest_addr)

        self.assertIn(
            (flow.game.gid, flow.guest.user_id),
            flow.service._joiner_state13_window_sent,
        )
        self.assertEqual([target for _raw, target in replies], [
            flow.guest_addr,
            flow.host_addr,
        ])
        guest_outbound = [
            item
            for raw, target in replies
            if target == flow.guest_addr
            for item in session_tests._active_messages(raw)
        ]
        host_outbound = [
            item
            for raw, target in replies
            if target == flow.host_addr
            for item in session_tests._active_messages(raw)
        ]
        self.assertEqual([item.sequence >> 28 for item in guest_outbound], [0, 1])
        self.assertEqual([item.sequence >> 28 for item in host_outbound], [2, 2])

    def test_late_joiner_history_companion_is_not_relayed_to_host(self) -> None:
        flow = _finalized_flow()
        flow.game.properties["B-U-game_type"] = "2"
        race = flow.service._race[flow.game.gid]
        race.phase = RacePhase.SESSION_SETUP
        race.room_commit_sent = True

        guest_wire = flow.service._wire[flow.guest_addr]
        host_wire = flow.service._wire[flow.host_addr]
        guest_wire.next_server_sequence = 0x122
        guest_wire.last_client_sequence = 0x113
        guest_wire.latest_latency_info = bytes.fromhex(
            "00000000120000005a41e80000"
        )
        host_wire.next_server_sequence = 0x141
        host_wire.last_client_sequence = 0x12E
        host_wire.latest_latency_info = bytes.fromhex(
            "00000000120000005744000000"
        )

        _send(
            flow.service,
            flow.guest_addr,
            outer=121,
            sequence=0x113,
            acknowledgement=0x121,
            logical=named_state("Guest", 13),
        )
        host_sequence_after_transaction = host_wire.next_server_sequence

        state13 = named_state("Guest", 13)
        state5 = named_state("Guest", 5)
        client_record = state13 + bytes.fromhex("00496faa111111110019d40244")
        request = TunnelDatagram(
            122,
            (
                TunnelPacket(
                    1,
                    (0x10000114).to_bytes(4, "big")
                    + (0x123).to_bytes(4, "big")
                    + state5
                    + b"\x04"
                    + client_record
                    + bytes((len(client_record),)),
                ),
            ),
        ).encode(session_tests.EKEY)
        replies = flow.service.handle_datagram(request, flow.guest_addr)

        self.assertEqual(host_wire.next_server_sequence, host_sequence_after_transaction)
        self.assertFalse(any(target == flow.host_addr for _raw, target in replies))
        self.assertTrue(any(target == flow.guest_addr for _raw, target in replies))
        self.assertEqual(
            [
                game_manager_body(item.payload)
                for raw, target in replies
                if target == flow.guest_addr
                for item in session_tests._active_messages(raw)
            ],
            [b""],
        )

    def test_challenge_allocation_controls_do_not_trigger_premature_ready_lock(self) -> None:
        flow = _finalized_flow()
        flow.game.properties["B-U-game_type"] = "2"
        race = flow.service._race[flow.game.gid]
        race.phase = RacePhase.SESSION_SETUP
        race.room_access = RoomAccess.OPEN
        race.room_commit_sent = False
        flow.service._ready_epochs.pop(flow.game.gid, None)
        for endpoint in (flow.host_addr, flow.guest_addr):
            flow.service._wire[endpoint].ready_requested = False

        all_replies = []
        for outer, addr, logical in (
            (122, flow.host_addr, bytes.fromhex("00000000180000")),
            (123, flow.host_addr, bytes.fromhex("00000000160000")),
            (124, flow.guest_addr, bytes.fromhex("00000000180000")),
            (125, flow.guest_addr, bytes.fromhex("00000000160000")),
        ):
            all_replies.extend(
                _send(
                    flow.service,
                    addr,
                    outer=outer,
                    sequence=outer,
                    acknowledgement=0x140,
                    logical=logical,
                )
            )

        self.assertEqual(race.room_access, RoomAccess.OPEN)
        self.assertFalse(flow.game.challenge_ready)
        self.assertFalse(flow.service._wire[flow.host_addr].ready_requested)
        self.assertFalse(flow.service._wire[flow.guest_addr].ready_requested)
        self.assertFalse(any(
            active.game_manager is not None
            and active.game_manager.message_type == 0x0C
            for raw, _target in all_replies
            for active in session_tests._active_messages(raw)
        ))


class CarbonRetailReadyCaptureTests(unittest.TestCase):
    def test_retail_ready_seed_matches_capture_flags_and_bodies(self) -> None:
        flow = _finalized_flow()
        flow.game.properties["B-U-game_type"] = "2"
        flow.service._race[flow.game.gid].phase = RacePhase.SESSION_SETUP
        # Isolate the Ready generation from earlier room/session windows so
        # its exact retry ownership can be asserted below.
        flow.service.confirmations.clear_endpoint(flow.host_addr)
        flow.service.confirmations.clear_endpoint(flow.guest_addr)
        timer = bytes.fromhex("000000001b") + struct.pack(">Iff", 5, 67.894, 20.5)
        attrs = session_attributes(flow.game.properties)
        state1 = anonymous_state(1)
        logicals = [
            timer + b"\x04" + bytes.fromhex("0000000013000131") + b"\x04",
            attrs,
            state1,
            bytes.fromhex("0000000014000130"),
            bytes.fromhex("00000000180000"),
        ]
        source_flags = [2, 0, 0, 1, 2]
        actives = []
        for index, (logical, flag) in enumerate(zip(logicals, source_flags)):
            sequence = (flag << 28) | (index + 1)
            actives.append(
                CommUDPActive(
                    sequence,
                    0x120,
                    _client_active(sequence, 0x120, logical),
                    None,
                )
            )
        replies: list[tuple[bytes, tuple[str, int]]] = []
        consumed = flow.service.ready_state.relay_retail_ready_seed(
            replies,
            flow.host_addr,
            flow.service._bindings[flow.host_addr],
            actives,
        )
        self.assertEqual(consumed, {id(item) for item in actives})
        control80 = bytes.fromhex("018c000000828000000002")
        control81 = bytes.fromhex("018c000000828100000002")
        expected_timer = (
            timer + b"\x04" + control80 + b"\x04\x0c" + control81 + b"\x04"
        )
        host_datagrams = [
            session_tests._active_messages(raw)
            for raw, target in replies
            if target == flow.host_addr
        ]
        self.assertEqual([len(items) for items in host_datagrams], [2, 3])
        host_outbound = [item for items in host_datagrams for item in items]
        self.assertEqual([item.sequence >> 28 for item in host_outbound], [0, 1, 2, 0, 0])
        host_lows = [item.sequence & 0x0FFFFFFF for item in host_outbound]
        self.assertEqual(
            host_lows,
            [((host_lows[0] + index) & 0x0FFFFFFF) for index in range(5)],
        )
        self.assertEqual(game_manager_body(host_outbound[0].payload), control81)
        self.assertEqual(
            game_manager_body(host_outbound[1].payload),
            control80 + b"\x04" + control81 + b"\x04",
        )
        self.assertEqual(game_manager_body(host_outbound[2].payload), expected_timer)
        self.assertEqual(game_manager_body(host_outbound[3].payload), attrs)
        self.assertEqual(game_manager_body(host_outbound[4].payload), state1)
        self.assertEqual(
            flow.service._wire[flow.host_addr].ready_seed_final_sequence,
            host_lows[-1],
        )

        guest_datagrams = [
            session_tests._active_messages(raw)
            for raw, target in replies
            if target == flow.guest_addr
        ]
        self.assertEqual([len(items) for items in guest_datagrams], [3])
        guest_outbound = [item for items in guest_datagrams for item in items]
        self.assertEqual(
            [item.sequence >> 28 for item in guest_outbound],
            [2, 0, 0],
        )
        guest_lows = [item.sequence & 0x0FFFFFFF for item in guest_outbound]
        self.assertEqual(
            guest_lows,
            [((guest_lows[0] + index) & 0x0FFFFFFF) for index in range(3)],
        )
        guest_bodies = [game_manager_body(item.payload) for item in guest_outbound]
        guest_timer = guest_bodies[0]
        guest_timer_id, guest_sender_clock, guest_duration = struct.unpack(
            ">Iff", guest_timer[5:17]
        )
        original_timer_id, original_sender_clock, original_duration = struct.unpack(
            ">Iff", timer[5:17]
        )
        self.assertEqual(guest_timer_id, original_timer_id)
        self.assertAlmostEqual(guest_sender_clock, original_sender_clock, places=5)
        self.assertAlmostEqual(original_duration, 20.5, places=5)
        self.assertAlmostEqual(guest_duration, 20.5, places=5)
        self.assertEqual(guest_timer[:17], timer)
        self.assertEqual(
            guest_timer[17:],
            b"\x04" + control80 + b"\x04\x0c" + control81 + b"\x04",
        )
        self.assertEqual(guest_bodies[1:], [attrs, state1])
        self.assertEqual(
            flow.service._wire[flow.guest_addr].ready_seed_final_sequence,
            guest_lows[-1],
        )
        epoch = flow.service._ready_epochs[flow.game.gid]
        self.assertEqual(epoch.stage, ReadyStage.SEED_SENT_WAIT_GUEST_13_15)
        self.assertTrue(flow.game.challenge_ready)
        self.assertTrue(flow.game.quick_join_locked)
        self.assertEqual(flow.service._race[flow.game.gid].phase, RacePhase.SESSION_SETUP)
        self.assertEqual(flow.service._race[flow.game.gid].room_access, RoomAccess.OPEN)

        host_windows = flow.service.confirmations.pending(flow.host_addr)
        guest_windows = flow.service.confirmations.pending(flow.guest_addr)
        self.assertEqual(
            [window.label for window in host_windows],
            ["ready-seed-host-prelude", "ready-seed-host"],
        )
        self.assertEqual(
            [window.label for window in guest_windows],
            ["ready-seed-guest"],
        )
        expected_retry = [
            (record, destination)
            for destination, windows in (
                (flow.host_addr, host_windows),
                (flow.guest_addr, guest_windows),
            )
            for window in windows
            for record in window.records
        ]
        host_wire = flow.service._wire[flow.host_addr]
        guest_wire = flow.service._wire[flow.guest_addr]
        transport_state = (
            host_wire.next_server_sequence,
            host_wire.next_offset_words,
            guest_wire.next_server_sequence,
            guest_wire.next_offset_words,
        )
        retry_at = max(
            window.retry.retry_not_before
            for window in (*host_windows, *guest_windows)
        )
        retried = flow.service.confirmations.poll(now=retry_at)
        self.assertCountEqual(retried, expected_retry)
        self.assertEqual(
            (
                host_wire.next_server_sequence,
                host_wire.next_offset_words,
                guest_wire.next_server_sequence,
                guest_wire.next_offset_words,
            ),
            transport_state,
        )

        flow.service.confirmations.acknowledge(
            flow.host_addr,
            host_windows[-1].final_sequence,
        )
        self.assertEqual(flow.service.confirmations.pending(flow.host_addr), ())
        self.assertEqual(
            flow.service.confirmations.pending(flow.guest_addr),
            guest_windows,
        )

    def test_retail_ready_guest_uses_latency_history_window_when_available(self) -> None:
        flow = _finalized_flow()
        flow.game.properties["B-U-game_type"] = "2"
        flow.service._race[flow.game.gid].phase = RacePhase.SESSION_SETUP
        host_latency = latency_info(
            flow.service._bindings[flow.host_addr].participant.player_id,
            29.0,
        )
        guest_latency = latency_info(
            flow.service._bindings[flow.guest_addr].participant.player_id,
            105.0,
        )
        flow.service._wire[flow.host_addr].latest_latency_info = host_latency
        flow.service._wire[flow.guest_addr].latest_latency_info = guest_latency
        timer = bytes.fromhex("000000001b") + struct.pack(">Iff", 5, 67.894, 20.5)
        attrs = session_attributes(flow.game.properties)
        state1 = anonymous_state(1)
        logicals = [
            timer + b"\x04" + bytes.fromhex("0000000013000131") + b"\x04",
            attrs,
            state1,
            bytes.fromhex("0000000014000130"),
            bytes.fromhex("00000000180000"),
        ]
        source_flags = [2, 0, 0, 1, 2]
        actives = []
        for index, (logical, flag) in enumerate(zip(logicals, source_flags)):
            sequence = (flag << 28) | (index + 1)
            actives.append(
                CommUDPActive(
                    sequence,
                    0x120,
                    _client_active(sequence, 0x120, logical),
                    None,
                )
            )
        replies: list[tuple[bytes, tuple[str, int]]] = []

        consumed = flow.service.ready_state.relay_retail_ready_seed(
            replies,
            flow.host_addr,
            flow.service._bindings[flow.host_addr],
            actives,
        )

        self.assertEqual(consumed, {id(item) for item in actives})
        guest_datagrams = [
            session_tests._active_messages(raw)
            for raw, target in replies
            if target == flow.guest_addr
        ]
        self.assertEqual([len(items) for items in guest_datagrams], [2, 2, 3])
        guest_outbound = [item for items in guest_datagrams for item in items]
        self.assertEqual(
            [item.sequence >> 28 for item in guest_outbound],
            [0, 1, 2, 2, 2, 0, 0],
        )
        guest_lows = [item.sequence & 0x0FFFFFFF for item in guest_outbound]
        self.assertEqual(
            guest_lows,
            [((guest_lows[0] + index) & 0x0FFFFFFF) for index in range(7)],
        )
        control80 = bytes.fromhex("018c000000828000000002")
        control81 = bytes.fromhex("018c000000828100000002")
        guest_bodies = [game_manager_body(item.payload) for item in guest_outbound]
        self.assertEqual(
            guest_outbound[0].payload[8:],
            host_latency + b"\x04",
        )
        self.assertEqual(
            guest_outbound[1].payload[8:],
            flow.service._commudp_aggregate_payload(
                (guest_latency, host_latency)
            ),
        )
        self.assertEqual(
            guest_outbound[2].payload[8:],
            flow.service._commudp_aggregate_payload(
                (control81, guest_latency, host_latency)
            ),
        )
        self.assertEqual(
            guest_outbound[3].payload[8:],
            flow.service._commudp_aggregate_payload(
                (control80, control81, guest_latency)
            ),
        )
        guest_timer = guest_bodies[4]
        self.assertEqual(
            guest_outbound[4].payload[8:],
            flow.service._commudp_aggregate_payload(
                (timer, control80, control81)
            ),
        )
        timer_id, sender_clock, duration = struct.unpack(">Iff", guest_timer[5:17])
        self.assertEqual(timer_id, 5)
        self.assertAlmostEqual(sender_clock, 67.894, places=3)
        self.assertAlmostEqual(duration, 20.5, places=5)
        self.assertEqual(guest_bodies[5:], [attrs, state1])
        guest_wire = flow.service._wire[flow.guest_addr]
        self.assertTrue(guest_wire.ready_seed_used_latency_history)
        self.assertEqual(guest_wire.ready_seed_final_sequence, guest_lows[-1])

    def test_retail_ready_guest_prelude_is_omitted_without_complete_latency_pair(self) -> None:
        flow = _finalized_flow()
        flow.game.properties["B-U-game_type"] = "2"
        flow.service._race[flow.game.gid].phase = RacePhase.SESSION_SETUP
        guest_latency = latency_info(
            flow.service._bindings[flow.guest_addr].participant.player_id,
            105.0,
        )
        flow.service._wire[flow.guest_addr].latest_latency_info = guest_latency
        timer = bytes.fromhex("000000001b") + struct.pack(">Iff", 5, 67.894, 20.5)
        replies: list[tuple[bytes, tuple[str, int]]] = []
        flow.service.ready_state.relay_retail_ready_seed(
            replies,
            flow.host_addr,
            flow.service._bindings[flow.host_addr],
            _ready_request_actives(flow, timer),
        )
        guest_outbound = [
            item
            for raw, target in replies
            if target == flow.guest_addr
            for item in session_tests._active_messages(raw)
        ]
        self.assertEqual(
            [item.sequence >> 28 for item in guest_outbound],
            [2, 0, 0],
        )

    def test_ready_seed_duplicate_does_not_create_second_generation(self) -> None:
        flow = _finalized_flow()
        flow.game.properties["B-U-game_type"] = "2"
        flow.service._race[flow.game.gid].phase = RacePhase.SESSION_SETUP
        timer = bytes.fromhex("000000001b") + struct.pack(">Iff", 5, 67.894, 20.5)
        actives = _ready_request_actives(flow, timer)
        first: list[tuple[bytes, tuple[str, int]]] = []
        flow.service.ready_state.relay_retail_ready_seed(
            first,
            flow.host_addr,
            flow.service._bindings[flow.host_addr],
            actives,
        )
        second: list[tuple[bytes, tuple[str, int]]] = []
        consumed = flow.service.ready_state.relay_retail_ready_seed(
            second,
            flow.host_addr,
            flow.service._bindings[flow.host_addr],
            actives,
        )
        self.assertEqual(consumed, {id(item) for item in actives})
        self.assertEqual(second, [])
        self.assertEqual(flow.service._ready_epochs[flow.game.gid].generation, 1)

    def test_v830_finished_room_ready_seed_resets_only_race_state(self) -> None:
        flow = _finalized_flow()
        flow.game.properties["B-U-game_type"] = "2"
        old_race = flow.service._race[flow.game.gid]
        old_race.phase = RacePhase.FINISHED
        old_race.room_access = RoomAccess.LOCKED
        flow.service.games.set_quick_join_locked(
            flow.game.gid,
            True,
            reason="test-finished",
        )
        old_race.player_controlled_ai[0x65] = bytes.fromhex(
            "000000000500000065000000000000000141"
        )
        flow.service.race_results.trackers[flow.game.gid] = object()
        old_epoch = _install_ready_epoch(
            flow,
            ReadyStage.STATE_PAIR_SENT_WAIT_HOST_NATIVE_BUNDLE,
        )
        old_epoch.source_payload_hash = 0

        transport_before = {}
        world_footer_before = {}
        for index, endpoint in enumerate((flow.host_addr, flow.guest_addr)):
            wire = flow.service._wire[endpoint]
            wire.next_server_sequence = 0x250 + index * 0x10
            wire.client_stream_offset_words = 0x1234 + index
            wire.session_generation = 7 + index
            wire.race_ready_seen = True
            wire.gameplay_ready = True
            wire.active_game_ready = True
            wire.latency_info_sent = True
            wire.start_lock_final_sequence = 0x222
            wire.pending_ai_registration_windows.append(object())
            wire.ai_registration_ready_refresh_sent = True
            wire.next_server_virtual_sequence = 0xD4
            wire.last_world_state_footer_tick_ms = 0x00200000 + index
            wire.world_state_log_not_before = 42.5 + index
            wire.world_state_footer_diagnostic_logged = True
            # These receive anchors and smoothed timing samples belong to the
            # live connection, not to one race in that connection.
            wire.last_client_footer = (
                (0x00100000 + index).to_bytes(4, "big")
                + (0x000FFFE0 + index).to_bytes(4, "big")
                + bytes.fromhex("00208302")
            )
            wire.last_client_footer_received_tick_ms = 0x00100020 + index
            wire.world_footer_observation_count = 3 + index
            wire.world_footer_observation_log_tick_ms = 0x00100030 + index
            wire.world_footer_send_log_tick_ms = 0x00100040 + index
            wire.world_footer_remote_ack_tick_ms = 0x000FFFE0 + index
            wire.world_footer_received_tick_ms = 0x00100020 + index
            wire.world_footer_rtt_avg_ms = 0x20 + index
            wire.world_footer_jitter_avg_ms = 0x08 + index
            wire.world_footer_lag_repair_logged = True
            transport_before[endpoint] = (
                wire.client_stream_offset_words,
                wire.session_generation,
                wire.next_server_sequence,
            )
            world_footer_before[endpoint] = (
                wire.last_client_footer,
                wire.last_client_footer_received_tick_ms,
                wire.world_footer_observation_count,
                wire.world_footer_observation_log_tick_ms,
                wire.world_footer_send_log_tick_ms,
                wire.world_footer_remote_ack_tick_ms,
                wire.world_footer_received_tick_ms,
                wire.world_footer_rtt_avg_ms,
                wire.world_footer_jitter_avg_ms,
                wire.world_footer_lag_repair_logged,
            )

        timer = bytes.fromhex("000000001b") + struct.pack(
            ">Iff",
            5,
            204.398514,
            20.5,
        )
        with self.assertLogs(
            "carbon.rebroadcaster.service",
            level="INFO",
        ) as captured:
            replies: list[tuple[bytes, tuple[str, int]]] = []
            actives = _ready_request_actives(flow, timer)
            consumed = flow.service.ready_state.relay_retail_ready_seed(
                replies,
                flow.host_addr,
                flow.service._bindings[flow.host_addr],
                actives,
            )

        self.assertEqual(consumed, {id(item) for item in actives})
        new_race = flow.service._race[flow.game.gid]
        self.assertIsNot(new_race, old_race)
        self.assertEqual(new_race.phase, RacePhase.SESSION_SETUP)
        self.assertEqual(new_race.room_access, RoomAccess.OPEN)
        self.assertTrue(flow.game.quick_join_locked)
        self.assertFalse(new_race.post_race_reopened)
        self.assertEqual(new_race.player_controlled_ai, {})
        self.assertNotIn(flow.game.gid, flow.service.race_results.trackers)
        self.assertEqual(
            flow.service._ready_epochs[flow.game.gid].generation,
            2,
        )

        for endpoint in (flow.host_addr, flow.guest_addr):
            wire = flow.service._wire[endpoint]
            old_offset, old_session_generation, old_reliable = transport_before[
                endpoint
            ]
            self.assertEqual(wire.client_stream_offset_words, old_offset)
            self.assertEqual(wire.session_generation, old_session_generation)
            self.assertGreater(wire.next_server_sequence, old_reliable)
            self.assertFalse(wire.race_ready_seen)
            self.assertFalse(wire.gameplay_ready)
            self.assertFalse(wire.active_game_ready)
            self.assertFalse(wire.latency_info_sent)
            self.assertEqual(wire.start_lock_final_sequence, 0)
            self.assertEqual(wire.pending_ai_registration_windows, [])
            self.assertFalse(wire.ai_registration_ready_refresh_sent)
            self.assertEqual(
                wire.next_server_virtual_sequence,
                0x80,
            )
            self.assertEqual(wire.last_world_state_footer_tick_ms, 0)
            self.assertEqual(wire.world_state_log_not_before, 0.0)
            self.assertFalse(wire.world_state_footer_diagnostic_logged)
            self.assertEqual(
                (
                    wire.last_client_footer,
                    wire.last_client_footer_received_tick_ms,
                    wire.world_footer_observation_count,
                    wire.world_footer_observation_log_tick_ms,
                    wire.world_footer_send_log_tick_ms,
                    wire.world_footer_remote_ack_tick_ms,
                    wire.world_footer_received_tick_ms,
                    wire.world_footer_rtt_avg_ms,
                    wire.world_footer_jitter_avg_ms,
                    wire.world_footer_lag_repair_logged,
                ),
                world_footer_before[endpoint],
            )
        self.assertTrue(any(
            "V830 rematch state reset" in message
            and "transport=preserved" in message
            for message in captured.output
        ))

    def test_v832_post_race_enable_joins_reopens_room_before_ready(self) -> None:
        flow = _finalized_flow()
        service = flow.service
        old_race = service._race[flow.game.gid]
        old_race.phase = RacePhase.FINISHED
        old_race.room_access = RoomAccess.LOCKED
        service.games.set_quick_join_locked(
            flow.game.gid,
            True,
            reason="test-finished",
        )
        old_race.player_controlled_ai[0x65] = bytes.fromhex(
            "000000000500000065000000000000000141"
        )
        old_race.relayed_racer_finished.add(0x65)
        service.race_results.trackers[flow.game.gid] = object()
        old_epoch = _install_ready_epoch(
            flow,
            ReadyStage.COUNTDOWN_ACTIVE,
        )

        service._wire[flow.host_addr].next_server_sequence = 0x216
        service._wire[flow.host_addr].last_client_sequence = 0x27E
        service._wire[flow.guest_addr].next_server_sequence = 0x1F4
        raw = TunnelDatagram(
            104,
            (
                TunnelPacket(
                    1,
                    _client_active(
                        0x2B5,
                        0x1F2,
                        bytes.fromhex("00000000170000"),
                    ),
                ),
                TunnelPacket(
                    1,
                    _client_active(
                        0x2B6,
                        0x1F2,
                        bytes.fromhex("00000000150000"),
                    ),
                ),
            ),
        ).encode(session_tests.EKEY)

        with self.assertLogs(
            "carbon.rebroadcaster.service",
            level="INFO",
        ) as captured:
            replies = service.handle_datagram(raw, flow.guest_addr)

        self.assertEqual(old_epoch.stage, ReadyStage.ABORTED)
        self.assertNotIn(flow.game.gid, service._ready_epochs)
        new_race = service._race[flow.game.gid]
        self.assertIsNot(new_race, old_race)
        self.assertEqual(new_race.phase, RacePhase.SESSION_SETUP)
        self.assertEqual(new_race.room_access, RoomAccess.OPEN)
        self.assertFalse(flow.game.quick_join_locked)
        self.assertTrue(new_race.post_race_reopened)
        self.assertEqual(new_race.player_controlled_ai, {})
        self.assertEqual(new_race.relayed_racer_finished, set())
        self.assertNotIn(flow.game.gid, service.race_results.trackers)

        expected = tuple(
            item.encode()
            for item in reopen_host_properties(flow.game.session.capacity)
        )
        source_packets = [
            item
            for raw_reply, target in replies
            if target == flow.guest_addr
            for item in session_tests._active_messages(raw_reply)
        ]
        self.assertEqual(
            [item.sequence >> 28 for item in source_packets],
            [0, 1, 2, 3],
        )
        self.assertTrue(all(
            item.acknowledgement == 0x2B6
            for item in source_packets
        ))
        source_records = [
            _native_commudp_records(item)
            for item in source_packets
        ]
        self.assertEqual(
            [current for current, _history in source_records],
            [item + b"\x04" for item in expected],
        )
        self.assertEqual(
            [history for _current, history in source_records],
            [
                (),
                (expected[0] + b"\x04",),
                (
                    expected[0] + b"\x04",
                    expected[1] + b"\x04",
                ),
                (
                    expected[0] + b"\x04",
                    expected[1] + b"\x04",
                    expected[2] + b"\x04",
                ),
            ],
        )

        peer_packets = [
            item
            for raw_reply, target in replies
            if target == flow.host_addr
            for item in session_tests._active_messages(raw_reply)
        ]
        self.assertEqual(
            [item.sequence >> 28 for item in peer_packets],
            [0, 1, 2, 2, 2],
        )
        self.assertTrue(all(
            item.acknowledgement == 0x27E
            for item in peer_packets
        ))
        peer_records = [
            _native_commudp_records(item)
            for item in peer_packets
        ]
        footer_record = peer_records[0][0]
        self.assertEqual(len(footer_record), 13)
        self.assertEqual(footer_record[-1], 0x40)
        self.assertEqual(
            [current for current, _history in peer_records[1:]],
            [item + b"\x04" for item in expected],
        )
        self.assertEqual(
            [history for _current, history in peer_records[1:]],
            [
                (footer_record,),
                (footer_record, expected[0] + b"\x04"),
                (
                    expected[0] + b"\x04",
                    expected[1] + b"\x04",
                ),
                (
                    expected[1] + b"\x04",
                    expected[2] + b"\x04",
                ),
            ],
        )
        self.assertTrue(any(
            "V832 post-race room reopened" in message
            and "source_flags=0,1,2,3" in message
            for message in captured.output
        ))

        duplicate = _send(
            service,
            flow.guest_addr,
            outer=105,
            sequence=0x2B7,
            acknowledgement=0x1F7,
            logical=bytes.fromhex("00000000170000"),
        )
        self.assertFalse(any(
            game_manager_body(item.payload).startswith(bytes.fromhex("018c"))
            for raw_reply, _target in duplicate
            for item in session_tests._active_messages(raw_reply)
        ))

    def test_guest_seed_ack_does_not_invent_state13_state15(self) -> None:
        flow = _finalized_flow()
        epoch = _install_ready_epoch(
            flow,
            ReadyStage.SEED_SENT_WAIT_GUEST_13_15,
        )
        guest_wire = flow.service._wire[flow.guest_addr]
        guest_wire.ready_seed_final_sequence = 0x155
        guest_wire.last_client_acknowledgement = 0x155
        self.assertEqual(epoch.stage, ReadyStage.SEED_SENT_WAIT_GUEST_13_15)
        self.assertEqual(epoch.state13, b"")
        self.assertEqual(epoch.state15, b"")
        self.assertFalse(hasattr(flow.service, "_maybe_begin_post_ready_fallback"))

    def test_post_ready_native_state13_waits_for_native_state15(self) -> None:
        flow = _finalized_flow()
        flow.game.properties["B-U-game_type"] = "2"
        epoch = _install_ready_epoch(
            flow,
            ReadyStage.SEED_SENT_WAIT_GUEST_13_15,
        )
        guest_persona = flow.service._bindings[flow.guest_addr].participant.identity.persona
        state13 = named_state(guest_persona, 13)
        history = bytes.fromhex("0000000013000131") + b"\x04" + bytes.fromhex("00000000170000")
        logical13 = state13 + b"\x04" + history
        active13 = CommUDPActive(
            0x5000014A,
            0x157,
            _client_active(0x5000014A, 0x157, logical13),
            None,
        )
        replies: list[tuple[bytes, tuple[str, int]]] = []
        consumed = flow.service.ready_state.relay_clean_state13_ack(
            replies,
            flow.guest_addr,
            flow.service._bindings[flow.guest_addr],
            [active13],
        )
        self.assertEqual(consumed, {id(active13)})
        self.assertEqual(epoch.state13, state13)
        self.assertEqual(epoch.state15, b"")
        self.assertEqual(epoch.stage, ReadyStage.SEED_SENT_WAIT_GUEST_13_15)
        self.assertEqual(replies, [])

    def test_invited_guest_pre_state_sequence_freezes_at_native_window(self) -> None:
        flow = _finalized_flow()
        epoch = _install_ready_epoch(
            flow,
            ReadyStage.SEED_SENT_WAIT_GUEST_13_15,
        )
        guest_wire = flow.service._wire[flow.guest_addr]
        guest_wire.last_client_sequence = 0x123
        latency = latency_info(
            flow.service._bindings[
                flow.guest_addr
            ].participant.player_id,
            61.0,
        )
        latency_active = CommUDPActive(
            0x123,
            0x13D,
            _client_active(0x123, 0x13D, latency),
            None,
        )
        flow.service.ready_state.relay_clean_state13_ack(
            [],
            flow.guest_addr,
            flow.service._bindings[flow.guest_addr],
            [latency_active],
        )
        self.assertEqual(epoch.guest_pre_state_sequence, 0x123)
        self.assertFalse(epoch.guest_state_window_started)

        guest_wire.last_client_sequence = 0x12B
        enable_joins = bytes.fromhex("00000000170000")
        control_active = CommUDPActive(
            0x4000012B,
            0x13D,
            _client_active(0x4000012B, 0x13D, enable_joins),
            None,
        )
        flow.service.ready_state.relay_clean_state13_ack(
            [],
            flow.guest_addr,
            flow.service._bindings[flow.guest_addr],
            [control_active],
        )
        self.assertTrue(epoch.guest_state_window_started)
        self.assertEqual(epoch.guest_pre_state_sequence, 0x123)

    def test_post_ready_state_pair_waits_for_both_seed_acknowledgements(self) -> None:
        flow = _finalized_flow()
        epoch = _install_ready_epoch(
            flow,
            ReadyStage.SEED_SENT_WAIT_GUEST_13_15,
        )
        host_wire = flow.service._wire[flow.host_addr]
        guest_wire = flow.service._wire[flow.guest_addr]
        host_wire.ready_seed_final_sequence = 0x143
        guest_wire.ready_seed_final_sequence = 0x12D
        host_wire.last_client_acknowledgement = 0x13E
        guest_wire.last_client_acknowledgement = 0x12D

        guest_persona = (
            flow.service._bindings[flow.guest_addr].participant.identity.persona
        )
        state13 = named_state(guest_persona, 13)
        state15 = anonymous_state(15)
        active13 = CommUDPActive(
            0x50000128,
            0x12D,
            _client_active(0x50000128, 0x12D, state13),
            None,
        )
        active15 = CommUDPActive(
            0x40000129,
            0x12D,
            _client_active(0x40000129, 0x12D, state15),
            None,
        )
        early: list[tuple[bytes, tuple[str, int]]] = []
        consumed = flow.service.ready_state.relay_clean_state13_ack(
            early,
            flow.guest_addr,
            flow.service._bindings[flow.guest_addr],
            [active13, active15],
        )

        self.assertEqual(consumed, {id(active13), id(active15)})
        self.assertEqual(early, [])
        self.assertEqual(epoch.stage, ReadyStage.SEED_SENT_WAIT_GUEST_13_15)
        self.assertEqual(epoch.state13, state13)
        self.assertEqual(epoch.state15, state15)
        self.assertTrue(epoch.seed_ack_wait_logged)

        # A later ordinary host transport ACK releases the cached pair.  The
        # preceding 0x8281 control itself remains asynchronously acknowledged.
        host_wire.last_client_acknowledgement = 0x143
        host_wire.last_client_sequence = 0x145
        released: list[tuple[bytes, tuple[str, int]]] = []
        consumed = flow.service.ready_state.relay_clean_state13_ack(
            released,
            flow.host_addr,
            flow.service._bindings[flow.host_addr],
            [],
        )

        self.assertEqual(consumed, set())
        self.assertEqual(
            epoch.stage,
            ReadyStage.STATE_PAIR_SENT_WAIT_HOST_NATIVE_BUNDLE,
        )
        for destination in (flow.host_addr, flow.guest_addr):
            outbound = [
                item
                for raw, target in released
                if target == destination
                for item in session_tests._active_messages(raw)
            ]
            self.assertEqual([item.sequence >> 28 for item in outbound], [0, 1, 2])
            expected_ack = (
                epoch.source_final_sequence
                if destination == flow.host_addr
                else guest_wire.last_client_sequence
            )
            self.assertEqual(
                [item.acknowledgement for item in outbound],
                [expected_ack] * 3,
            )

    def test_post_ready_host_pair_acks_ready_request_not_empty_transport(self) -> None:
        flow = _finalized_flow()
        epoch = _install_ready_epoch(
            flow,
            ReadyStage.SEED_SENT_WAIT_GUEST_13_15,
        )
        epoch.source_first_sequence = 0x173
        epoch.source_final_sequence = 0x177
        host_wire = flow.service._wire[flow.host_addr]
        guest_wire = flow.service._wire[flow.guest_addr]
        host_wire.last_client_sequence = 0x178
        guest_wire.last_client_sequence = 0x14B

        guest_persona = (
            flow.service._bindings[flow.guest_addr].participant.identity.persona
        )
        state13 = named_state(guest_persona, 13)
        state15 = anonymous_state(15)
        active13 = CommUDPActive(
            0x5000014A,
            0x157,
            _client_active(0x5000014A, 0x157, state13),
            None,
        )
        active15 = CommUDPActive(
            0x4000014B,
            0x157,
            _client_active(0x4000014B, 0x157, state15),
            None,
        )
        replies: list[tuple[bytes, tuple[str, int]]] = []
        flow.service.ready_state.relay_clean_state13_ack(
            replies,
            flow.guest_addr,
            flow.service._bindings[flow.guest_addr],
            [active13, active15],
        )

        host_outbound = [
            item
            for raw, target in replies
            if target == flow.host_addr
            for item in session_tests._active_messages(raw)
        ]
        guest_outbound = [
            item
            for raw, target in replies
            if target == flow.guest_addr
            for item in session_tests._active_messages(raw)
        ]
        self.assertEqual(
            [item.acknowledgement for item in host_outbound],
            [0x177, 0x177, 0x177],
        )
        self.assertEqual(
            [item.acknowledgement for item in guest_outbound],
            [0x14B, 0x14B, 0x14B],
        )

    def test_invited_dedicated_endpoints_get_capture_state_windows(self) -> None:
        flow = _finalized_flow()
        flow.game.server_hosted = True
        flow.game.allocator_user_id = (
            flow.service._bindings[flow.host_addr].participant.identity.user_id
        )
        guest_binding = flow.service._bindings[flow.guest_addr]
        flow.service._bindings[flow.guest_addr] = replace(
            guest_binding,
            participant=replace(
                guest_binding.participant,
                invite_remote_player_id=flow.service._bindings[
                    flow.host_addr
                ].participant.player_id,
            ),
        )
        timer = (
            bytes.fromhex("000000001b")
            + struct.pack(">Iff", 5, 67.894, 20.5)
        )
        epoch = _install_ready_epoch(
            flow,
            ReadyStage.SEED_SENT_WAIT_GUEST_13_15,
            timer=timer,
        )
        epoch.guest_pre_state_sequence = 0x123
        host_wire = flow.service._wire[flow.host_addr]
        host_wire.next_server_sequence = 0x15D
        host_wire.last_client_sequence = 0x14F
        host_wire.ready_seed_final_sequence = 0
        host_wire.footer = bytes.fromhex("0041ff33377b093e00230000")
        guest_wire = flow.service._wire[flow.guest_addr]
        guest_wire.next_server_sequence = 0x13E
        guest_wire.ready_seed_final_sequence = 0
        guest_wire.last_client_sequence = 0x131
        guest_wire.footer = bytes.fromhex("0041ff3a377b093e02970000")
        guest_wire.latest_latency_info = bytes.fromhex(
            "00000000120000005a43740000"
        )

        guest_persona = (
            flow.service._bindings[flow.guest_addr].participant.identity.persona
        )
        state13 = named_state(guest_persona, 13)
        state15 = anonymous_state(15)
        active13 = CommUDPActive(
            0x50000130,
            0x13B,
            _client_active(0x50000130, 0x13B, state13),
            None,
        )
        active15 = CommUDPActive(
            0x40000131,
            0x13B,
            _client_active(0x40000131, 0x13B, state15),
            None,
        )
        replies: list[tuple[bytes, tuple[str, int]]] = []
        consumed = flow.service.ready_state.relay_clean_state13_ack(
            replies,
            flow.guest_addr,
            flow.service._bindings[flow.guest_addr],
            [active13, active15],
        )

        self.assertEqual(consumed, {id(active13), id(active15)})
        host_outbound = [
            item
            for raw, target in replies
            if target == flow.host_addr
            for item in session_tests._active_messages(raw)
        ]
        self.assertEqual(
            [item.sequence for item in host_outbound],
            [
                0x0000015D,
                0x1000015E,
                0x2000015F,
                0x20000160,
                0x20000161,
            ],
        )
        self.assertEqual(
            [item.acknowledgement for item in host_outbound],
            [0x14F] * 5,
        )
        footer_record = host_wire.footer + b"\x40"
        control_record = bytes.fromhex("018c00000082810000000204")
        state13_record = state13 + b"\x04"
        state15_record = state15 + b"\x04"
        self.assertEqual(
            [_native_commudp_records(item) for item in host_outbound],
            [
                (footer_record, ()),
                (control_record, (footer_record,)),
                (state13_record, (footer_record, control_record)),
                (state15_record, (control_record, state13_record)),
                (state13_record, (state13_record, state15_record)),
            ],
        )

        guest_outbound = [
            item
            for raw, target in replies
            if target == flow.guest_addr
            for item in session_tests._active_messages(raw)
        ]
        self.assertEqual(
            [item.sequence for item in guest_outbound],
            [
                0x1000013E,
                0x1000013F,
                0x20000140,
                0x30000141,
                0x30000142,
            ],
        )
        self.assertEqual(
            [item.acknowledgement for item in guest_outbound],
            [0x123, 0x131, 0x131, 0x131, 0x131],
        )
        guest_footer_record = guest_wire.footer + b"\x40"
        latency_record = guest_wire.latest_latency_info + b"\x04"
        self.assertEqual(
            [item.payload[8:] for item in guest_outbound],
            [
                (
                    guest_footer_record
                    + latency_record
                    + bytes((len(latency_record),))
                ),
                (
                    control_record
                    + guest_footer_record
                    + bytes((len(guest_footer_record),))
                ),
                (
                    state13_record
                    + control_record
                    + bytes((len(control_record),))
                    + guest_footer_record
                    + bytes((len(guest_footer_record),))
                ),
                (
                    state15_record
                    + state13_record
                    + bytes((len(state13_record),))
                    + control_record
                    + bytes((len(control_record),))
                    + guest_footer_record
                    + bytes((len(guest_footer_record),))
                ),
                (
                    state13_record
                    + state15_record
                    + bytes((len(state15_record),))
                    + state13_record
                    + bytes((len(state13_record),))
                    + control_record
                    + bytes((len(control_record),))
                ),
            ],
        )
        self.assertEqual(epoch.guest_state_final_sequence, 0x131)
        self.assertEqual(
            epoch.stage,
            ReadyStage.STATE_PAIR_SENT_WAIT_HOST_NATIVE_BUNDLE,
        )

        # Retail frame 1448 advances the helper's transport sequence to 0x132,
        # but frame 1449 still acknowledges the final native state sequence
        # 0x131 across the relayed eight-packet countdown bundle.
        guest_wire.last_client_sequence = 0x132
        attrs = session_attributes(flow.game.properties)
        state7 = anonymous_state(7)
        state14 = named_state("Guest", 14) + b"\x04" + state7
        timer_history = (
            timer
            + b"\x04"
            + state14
            + b"\x04\x12"
            + state7
        )
        logicals = [
            attrs,
            timer,
            attrs,
            state7,
            state14,
            timer_history,
            attrs,
            state7,
        ]
        flags = [0, 0, 0, 0, 1, 2, 0, 0]
        native = [
            CommUDPActive(
                (flag << 28) | (0x151 + index),
                0x161,
                _client_active(
                    (flag << 28) | (0x151 + index),
                    0x161,
                    logical,
                ),
                None,
            )
            for index, (flag, logical) in enumerate(zip(flags, logicals))
        ]
        native_replies: list[tuple[bytes, tuple[str, int]]] = []
        consumed = flow.service.ready_state.relay_native_ready_bundle(
            native_replies,
            flow.host_addr,
            flow.service._bindings[flow.host_addr],
            native,
        )
        self.assertEqual(consumed, {id(item) for item in native})
        guest_native = [
            item
            for raw, target in native_replies
            if target == flow.guest_addr
            for item in session_tests._active_messages(raw)
        ]
        self.assertEqual(
            [item.acknowledgement for item in guest_native],
            [0x131] * 8,
        )

    def test_post_ready_state13_control_then_state15_pair(self) -> None:
        flow = _finalized_flow()
        flow.game.properties["B-U-game_type"] = "2"
        epoch = _install_ready_epoch(
            flow,
            ReadyStage.SEED_SENT_WAIT_GUEST_13_15,
        )
        guest_persona = flow.service._bindings[flow.guest_addr].participant.identity.persona
        state13 = named_state(guest_persona, 13)
        state15 = anonymous_state(15)
        history = bytes.fromhex("0000000013000131") + b"\x04" + bytes.fromhex("00000000170000")
        logical13 = state13 + b"\x04" + history
        active13 = CommUDPActive(0x5000014A, 0x157, _client_active(0x5000014A, 0x157, logical13), None)
        active15 = CommUDPActive(0x4000014B, 0x157, _client_active(0x4000014B, 0x157, state15), None)
        stage1: list[tuple[bytes, tuple[str, int]]] = []
        consumed = flow.service.ready_state.relay_clean_state13_ack(
            stage1,
            flow.guest_addr,
            flow.service._bindings[flow.guest_addr],
            [active13, active15],
        )
        self.assertEqual(consumed, {id(active13), id(active15)})
        control = bytes.fromhex("018c000000828100000002")
        for destination in (flow.host_addr, flow.guest_addr):
            outbound = [
                item
                for raw, target in stage1
                if target == destination
                for item in session_tests._active_messages(raw)
            ]
            self.assertEqual(len(outbound), 3)
            self.assertEqual([item.sequence >> 28 for item in outbound], [0, 1, 2])
            self.assertEqual(outbound[0].payload[8:], control)
            self.assertEqual(
                game_manager_body(outbound[1].payload),
                state13 + b"\x04" + control,
            )
            self.assertEqual(
                game_manager_body(outbound[2].payload),
                state15 + b"\x04" + state13 + b"\x04\x12" + control,
            )
        self.assertEqual(
            epoch.stage,
            ReadyStage.STATE_PAIR_SENT_WAIT_HOST_NATIVE_BUNDLE,
        )

    def test_post_ready_pair_does_not_wait_for_control_ack(self) -> None:
        flow = _finalized_flow()
        epoch = _install_ready_epoch(
            flow,
            ReadyStage.SEED_SENT_WAIT_GUEST_13_15,
        )
        guest_persona = flow.service._bindings[flow.guest_addr].participant.identity.persona
        state13 = named_state(guest_persona, 13)
        state15 = anonymous_state(15)
        cumulative = state15 + b"\x04" + state13
        active = CommUDPActive(
            0x5000014A,
            0x157,
            _client_active(0x5000014A, 0x157, cumulative),
            None,
        )
        for addr in (flow.host_addr, flow.guest_addr):
            flow.service._wire[addr].last_client_acknowledgement = 0
        replies: list[tuple[bytes, tuple[str, int]]] = []
        consumed = flow.service.ready_state.relay_clean_state13_ack(
            replies,
            flow.guest_addr,
            flow.service._bindings[flow.guest_addr],
            [active],
        )
        self.assertEqual(consumed, {id(active)})
        self.assertEqual(
            epoch.stage,
            ReadyStage.STATE_PAIR_SENT_WAIT_HOST_NATIVE_BUNDLE,
        )
        for destination in (flow.host_addr, flow.guest_addr):
            outbound = [
                item
                for raw, target in replies
                if target == destination
                for item in session_tests._active_messages(raw)
            ]
            self.assertEqual([item.sequence >> 28 for item in outbound], [0, 1, 2])

    def test_native_eight_packet_bundle_is_relayed_with_original_flags(self) -> None:
        flow = _finalized_flow()
        attrs = session_attributes(flow.game.properties)
        timer = bytes.fromhex("000000001b") + struct.pack(">Iff", 5, 69.035, 19.881)
        epoch = _install_ready_epoch(
            flow,
            ReadyStage.STATE_PAIR_SENT_WAIT_HOST_NATIVE_BUNDLE,
            timer,
        )
        state7 = anonymous_state(7)
        state14 = named_state("player", 14) + b"\x04" + state7
        timer_history = timer + b"\x04" + named_state("player", 14) + b"\x04\x12" + state7
        logicals = [attrs, timer, attrs, state7, state14, timer_history, attrs, state7]
        flags = [0, 0, 0, 0, 1, 2, 0, 0]
        actives = []
        for index, (logical, flag) in enumerate(zip(logicals, flags)):
            sequence = (flag << 28) | (0x170 + index)
            actives.append(CommUDPActive(sequence, 0x18E, _client_active(sequence, 0x18E, logical), None))
        replies: list[tuple[bytes, tuple[str, int]]] = []
        consumed = flow.service.ready_state.relay_native_ready_bundle(
            replies,
            flow.host_addr,
            flow.service._bindings[flow.host_addr],
            actives,
        )
        self.assertEqual(consumed, {id(item) for item in actives})
        host_outbound = [
            item
            for raw, target in replies
            if target == flow.host_addr
            for item in session_tests._active_messages(raw)
        ]
        self.assertEqual(len(host_outbound), 8)
        self.assertEqual([item.sequence >> 28 for item in host_outbound], flags)
        self.assertEqual([game_manager_body(item.payload) for item in host_outbound], logicals)
        guest_outbound = [
            item
            for raw, target in replies
            if target == flow.guest_addr
            for item in session_tests._active_messages(raw)
        ]
        self.assertEqual(len(guest_outbound), 8)
        self.assertEqual([item.sequence >> 28 for item in guest_outbound], flags)
        self.assertEqual([game_manager_body(item.payload) for item in guest_outbound], logicals)
        self.assertEqual(epoch.stage, ReadyStage.COUNTDOWN_ACTIVE)
        self.assertEqual(flow.service._race[flow.game.gid].phase, RacePhase.COUNTDOWN)
        self.assertEqual(flow.service._race[flow.game.gid].room_access, RoomAccess.LOCKED)

    def test_native_ready_snapshot_is_exact_and_ready_owned(self) -> None:
        flow = _finalized_flow()
        seed_timer = (
            bytes.fromhex("000000001b") + struct.pack(">Iff", 5, 69.035, 19.881)
        )
        epoch = _install_ready_epoch(
            flow,
            ReadyStage.COUNTDOWN_ACTIVE,
            seed_timer,
        )
        snapshot_timer = (
            bytes.fromhex("000000001b") + struct.pack(">Iff", 5, 70.035, 18.891)
        )
        state7 = anonymous_state(7)
        state14 = named_state("player", 14) + b"\x04" + state7
        logicals = [snapshot_timer, state7, state14]
        flags = [0, 0, 1]
        actives = []
        for index, (logical, flag) in enumerate(zip(logicals, flags)):
            sequence = (flag << 28) | (0x190 + index)
            actives.append(
                CommUDPActive(
                    sequence,
                    0x18E,
                    _client_active(sequence, 0x18E, logical),
                    None,
                )
            )
        replies: list[tuple[bytes, tuple[str, int]]] = []
        consumed = flow.service.ready_state.relay_native_ready_snapshot(
            replies,
            flow.host_addr,
            flow.service._bindings[flow.host_addr],
            actives,
        )
        self.assertEqual(consumed, {id(item) for item in actives})
        for destination in (flow.host_addr, flow.guest_addr):
            outbound = [
                item
                for raw, target in replies
                if target == destination
                for item in session_tests._active_messages(raw)
            ]
            self.assertEqual([item.sequence >> 28 for item in outbound], flags)
            self.assertEqual(
                [game_manager_body(item.payload) for item in outbound],
                logicals,
            )
        self.assertAlmostEqual(
            epoch.wire_deadline,
            flow.service._timer_logical_deadline(snapshot_timer),
            places=5,
        )

    def test_ready_epoch_aborts_when_guest_endpoint_drops(self) -> None:
        flow = _finalized_flow()
        _install_ready_epoch(
            flow,
            ReadyStage.STATE_PAIR_SENT_WAIT_HOST_NATIVE_BUNDLE,
        )
        race = flow.service._race[flow.game.gid]
        race.phase = RacePhase.COUNTDOWN
        race.room_access = RoomAccess.LOCKED
        flow.service.games.set_quick_join_locked(
            flow.game.gid,
            True,
            reason="test-ready",
        )
        flow.service._drop_endpoint(flow.guest_addr)
        self.assertNotIn(flow.game.gid, flow.service._ready_epochs)
        host_wire = flow.service._wire[flow.host_addr]
        self.assertEqual(host_wire.ready_epoch_generation, 0)
        self.assertIsNone(host_wire.match_timer_retry)
        self.assertEqual(race.phase, RacePhase.SESSION_SETUP)
        self.assertEqual(race.room_access, RoomAccess.OPEN)
        self.assertFalse(flow.game.quick_join_locked)
