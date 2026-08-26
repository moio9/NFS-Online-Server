"""Stock NFS Underground 2 bootstrap/pre-login transport.

This module keeps the shared account and social services, but implements the
actual wire transport used by the retail client: plaintext directory frames
and the secure RSA+RC4 20921 branch.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import md5
import logging
import socket
import struct
import time
from threading import Event
from uuid import uuid4

from classic.core.catalog import GameId
from classic.ea.social import SocialService

from .auth import ClassicAuthContext
from .bootstrap import ClassicBootstrapService, ClassicDirectoryRegistry
from .frame import ClassicEAFrame
from .prelogin import ClassicPreloginContext, ClassicPreloginService
from .stream import ClassicEAShortFrame


log = logging.getLogger(__name__)

_CERT_PREFIX = bytes.fromhex("833e04000100020320000300103082031c30820285a00302010202144b1b66348f3d4c270132b5353e120beb203e46f7300d06092a864886f70d01010505003081a0310b30090603550406130255533113301106035504080c0a43616c69666f726e69613115301306035504070c0c526564776f6f642043697479311e301c060355040a0c15456c656374726f6e696320417274732c20496e632e3120301e060355040b0c174f6e6c696e6520546563686e6f6c6f67792047726f75703123302106035504030c1a4f54473320436572746966696361746520417574686f72697479301e170d3233303231343231303833315a170d3333303231313231303833315a3081a0310b30090603550406130255533113301106035504080c0a43616c69666f726e69613115301306035504070c0c526564776f6f642043697479311e301c060355040a0c15456c656374726f6e696320417274732c20496e632e3120301e060355040b0c174f6e6c696e6520546563686e6f6c6f67792047726f75703123302106035504030c1a4f54473320436572746966696361746520417574686f7269747930819d300d06092a864886f70d010101050003818b0030818702818100a57f9654cef57339f0e2bb79ba01c1faaee27b7361d8e87a504c5e453f7dc446fc14831d70fd873e01280df596bba6519f8f7f6b787184c8c7f863cdca67b90731587b82b251a337975da6c0c190f9e62da5981c0b2b4a76f4ab86c1ea11260248dde33a2bc49e61047a2d4bd4b95dd58926bbb4228028f2e125da977c358ad9020103a3533051301d0603551d0e04160414f337137f5c3401459a3b4160170fe9ab417fad75301f0603551d23041830168014f337137f5c3401459a3b4160170fe9ab417fad75300f0603551d130101ff040530030101ff300d06092a864886f70d0101050500038181001353cb98bfdcd704e2294066eb8af179f39b4224db3dbefdd73e3d7406556a130aa39d9d08dca648299e71e047cb10aae59ce6cfcc75f3d005a06e67286b66fbd62187c28ffbd4c8545b4a59d9ec3e48b277983281789b21a3b70852f945e39e80f649080917451284f3b0450502e009a0ecafe5517468bbca250417c96a2ca9010080210b42e60e5930d73d588847c0fa3341")
_RSA_N = int("00a252be7324af32c7ec6fd39f5dd3ea77f6fe6a7c5943f72dece7b4dc33d024d4c494576c5aefae246654f620d636e9c02371d5f1fff9b3ab88e67bedaf3a2ca9bc9ed576639e3295f333423e28f30c47566a6f6d9c050d3c49f8b9fcfccf6d03bb3188f290f3f99d337e2fccde2d6f04ac76060d2907b53e846e58564671ef6d", 16)
_RSA_D = int("6c3729a21874cc85484a8d14e937f1a54f5446fd90d7fa1e9defcde8228ac338830d8f9d91f51ec2eee34ec08ecf468017a1394bfffbcd1d05eefd491f7c1dc56dea50a6952d31b9b0dce95a355ba10a46a6057d2c61bbd6591f55b4b0a9534f725078e54d4d56cc5d98daa496ffac8f5c22e15b6198f65bc6fb4ba1b2efa713", 16)
_CHALLENGE = bytes.fromhex("AD4A9CF47E309966DC257ECE71C26A6E")
_SHORT_FRAME_TAGS = {b"newsbadc", b"userbadc"}


class U2StockTransportError(ValueError):
    pass


@dataclass
class _SecureState:
    step: int = 0
    token: bytes = b""
    peer_blob: bytes = _CHALLENGE
    recv_md5: bytes = b""
    send_md5: bytes = b""
    recv_state: bytes = b""
    send_state: bytes = b""
    # The stock OTG3 client numbers server-to-client secure records from one.
    # Starting at zero keeps the RC4 stream aligned, so requests still decrypt,
    # but makes the client reject the record MAC (most visibly the @dir reply).
    send_seq: int = 1
    plain_buffer: bytearray | None = None

    def __post_init__(self) -> None:
        if self.plain_buffer is None:
            self.plain_buffer = bytearray()


class U2StockBootstrapTransport:
    """Serve a real U2 client while reusing the common auth/social backend."""

    def __init__(
        self,
        *,
        bootstrap: ClassicBootstrapService,
        directory_registry: ClassicDirectoryRegistry,
        prelogin: ClassicPreloginService,
        social: SocialService,
        max_frame_size: int,
        connection_timeout: float,
    ) -> None:
        self.bootstrap = bootstrap
        self.directory_registry = directory_registry
        self.prelogin = prelogin
        self.social = social
        self.max_frame_size = int(max_frame_size)
        self.connection_timeout = float(connection_timeout)

    @staticmethod
    def _printable4(raw: bytes) -> bool:
        return len(raw) == 4 and all(32 <= value <= 126 for value in raw)

    @staticmethod
    def _rc4_apply(state258: bytes, data: bytes) -> tuple[bytes, bytes]:
        if len(state258) != 258:
            raise U2StockTransportError(f"invalid RC4 state length: {len(state258)}")
        state = bytearray(state258)
        i = state[0]
        j = state[1]
        s = state[2:]
        output = bytearray()
        for value in data:
            i = (i + 1) & 0xFF
            j = (j + s[i]) & 0xFF
            s[i], s[j] = s[j], s[i]
            output.append(value ^ s[(s[i] + s[j]) & 0xFF])
        return bytes((i, j)) + bytes(s), bytes(output)

    @staticmethod
    def _ksa(key: bytes, rounds: int = 1) -> bytes:
        state = bytearray(258)
        for index in range(256):
            state[2 + index] = index
        if key and rounds > 0:
            carry = 0
            for _ in range(rounds):
                pointer = 2
                while pointer - 2 < 256:
                    current = state[pointer]
                    slot = (carry + key[(pointer - 2) % len(key)] + current) & 0xFF
                    state[pointer] = state[2 + slot]
                    state[2 + slot] = current

                    current = state[pointer + 1]
                    slot = (slot + key[(pointer - 1) % len(key)] + current) & 0xFF
                    state[pointer + 1] = state[2 + slot]
                    state[2 + slot] = current

                    current = state[pointer + 2]
                    slot = (slot + key[pointer % len(key)] + current) & 0xFF
                    state[pointer + 2] = state[2 + slot]
                    state[2 + slot] = current

                    current = state[pointer + 3]
                    carry = (slot + key[(pointer + 1) % len(key)] + current) & 0xFF
                    state[pointer + 3] = state[2 + carry]
                    state[2 + carry] = current
                    pointer += 4
        return bytes(state)

    @classmethod
    def _make_secure_frame(
        cls, md5_key: bytes, state: bytes, sequence: int, body: bytes
    ) -> tuple[bytes, bytes]:
        mac = md5(md5_key + body + struct.pack(">I", sequence)).digest()
        next_state, encrypted = cls._rc4_apply(state, mac + body)
        return next_state, struct.pack("!H", 0x8000 | len(encrypted)) + encrypted

    @classmethod
    def _decrypt_secure_frame(cls, state: bytes, frame: bytes) -> tuple[bytes, bytes]:
        next_state, plain = cls._rc4_apply(state, frame[2:])
        if len(plain) < 16:
            raise U2StockTransportError("secure frame is shorter than its MAC")
        return next_state, plain[16:]

    @staticmethod
    def _rsa_unpad(block: bytes) -> bytes | None:
        if len(block) < 11 or not block.startswith(b"\x00\x02"):
            return None
        separator = block.find(b"\x00", 2)
        if separator < 10 or separator + 1 >= len(block):
            return None
        return block[separator + 1 :]

    @staticmethod
    def _make_cert_frame(challenge: bytes) -> bytes:
        frame = bytearray(_CERT_PREFIX)
        body = frame[2:]
        der_length = struct.unpack_from(">H", body, 5)[0]
        der_offset = 13
        der = bytearray(frame[der_offset : der_offset + der_length])
        marker = b"\x02\x81\x81\x00"
        position = der.find(marker)
        if position < 0:
            raise U2StockTransportError("certificate modulus marker was not found")
        modulus = _RSA_N.to_bytes(128, "big")
        start = position + len(marker)
        der[start : start + 128] = modulus
        frame[der_offset : der_offset + der_length] = der
        frame[-16:] = challenge
        return bytes(frame)

    @staticmethod
    def _secure_packet_length(buffer: bytes | bytearray) -> int | None:
        if len(buffer) < 2:
            return None
        word = struct.unpack("!H", bytes(buffer[:2]))[0]
        if not (word & 0x8000):
            return 0
        total = (word & 0x7FFF) + 2
        if total < 2 or total > 65_535:
            raise U2StockTransportError(f"invalid secure packet length: {total}")
        return total

    @classmethod
    def _parse_plain_packet(
        cls, buffer: bytes | bytearray, *, max_frame_size: int
    ) -> tuple[ClassicEAFrame | ClassicEAShortFrame, int] | None:
        if len(buffer) < 12:
            return None
        wire = bytes(buffer)
        if wire[:8] in _SHORT_FRAME_TAGS and struct.unpack(">I", wire[8:12])[0] == 12:
            return ClassicEAShortFrame(wire[:8].decode("latin-1")), 12
        if not cls._printable4(wire[:4]):
            raise U2StockTransportError(f"non-frame bytes: {wire[:16].hex()}")
        total = struct.unpack(">I", wire[8:12])[0]
        if total < 12 or total > max_frame_size:
            raise U2StockTransportError(f"invalid frame length: {total}")
        if len(wire) < total:
            return None
        command = wire[:4].decode("latin-1")
        reserved = struct.unpack(">I", wire[4:8])[0]
        return ClassicEAFrame(command, wire[12:total], reserved), total

    @classmethod
    def _extract_prelogin_messages(
        cls, buffer: bytearray, *, max_frame_size: int
    ) -> tuple[ClassicEAFrame | ClassicEAShortFrame, ...]:
        packets: list[ClassicEAFrame | ClassicEAShortFrame] = []
        offset = 0
        length = len(buffer)
        while offset + 12 <= length:
            raw = bytes(buffer[offset:])
            if raw[:8] in _SHORT_FRAME_TAGS and struct.unpack(">I", raw[8:12])[0] == 12:
                packets.append(ClassicEAShortFrame(raw[:8].decode("latin-1")))
                offset += 12
                continue
            if not cls._printable4(raw[:4]):
                offset += 1
                continue
            declared = struct.unpack(">I", raw[8:12])[0]
            candidates: list[int] = []
            if 12 <= declared <= max_frame_size and offset + declared <= length:
                candidates.append(declared)
            payload_style = declared + 12
            if 12 <= payload_style <= max_frame_size and offset + payload_style <= length:
                candidates.append(payload_style)
            if not candidates:
                if (12 <= declared <= max_frame_size and offset + declared > length) or (
                    12 <= payload_style <= max_frame_size and offset + payload_style > length
                ):
                    break
                offset += 1
                continue
            message_length = candidates[0]
            for candidate in candidates:
                end = offset + candidate
                if end == length:
                    message_length = candidate
                    break
                if end + 4 <= length and cls._printable4(bytes(buffer[end : end + 4])):
                    message_length = candidate
                    break
            wire = bytes(buffer[offset : offset + message_length])
            packets.append(
                ClassicEAFrame(
                    wire[:4].decode("latin-1"),
                    wire[12:message_length],
                    struct.unpack(">I", wire[4:8])[0],
                )
            )
            offset += message_length
        if offset:
            del buffer[:offset]
        if len(buffer) > 131_072:
            del buffer[:-32_768]
        return tuple(packets)

    @staticmethod
    def _refresh_challenge(
        context: ClassicPreloginContext,
        registry: ClassicDirectoryRegistry,
        client_ip: str,
    ) -> None:
        challenge = registry.recent(client_ip)
        if challenge is None:
            return
        context.auth.session_challenge = challenge.session
        context.auth.mask = challenge.mask

    def _register_social(
        self,
        context: ClassicPreloginContext,
        *,
        connection_id: str,
        client_ip: str,
        registered_persona: str,
    ) -> str:
        persona = context.auth.persona
        if context.auth.identity is None or not persona:
            return registered_persona
        if persona.casefold() == registered_persona.casefold():
            return registered_persona
        account_name = (
            context.auth.account.account_name
            if context.auth.account is not None
            else context.auth.identity.account_name
        )
        self.social.register_lobby(
            connection_id,
            account_name,
            persona,
            client_ip,
            game_id=GameId.UNDERGROUND2.value,
            session_token=context.auth.session_token,
        )
        return persona

    def run(
        self,
        conn: socket.socket,
        addr: tuple[str, int],
        stop_event: Event,
    ) -> str:
        return _U2StockSession(
            self,
            conn,
            addr,
            stop_event,
        ).run()


class _U2StockSession:
    """Mutable per-connection state for the stock U2 transport."""

    def __init__(
        self,
        transport: U2StockBootstrapTransport,
        conn: socket.socket,
        addr: tuple[str, int],
        stop_event: Event,
    ) -> None:
        self.transport = transport
        self.conn = conn
        self.addr = addr
        self.stop_event = stop_event
        self.connection_id = f"u2-stock:{addr[0]}:{addr[1]}:{uuid4().hex}"
        auth = ClassicAuthContext(
            connection_id=self.connection_id,
            client_ip=addr[0],
        )
        self.context = ClassicPreloginContext(
            auth=auth,
            client_address=addr[0],
            client_port=addr[1],
        )
        self.buffer = bytearray()
        self.raw_logged = False
        self.registered_persona = ""
        self.registered_game_session = ""
        self.secure = _SecureState()
        self.last_activity = time.monotonic()
        self.next_session_touch = self.last_activity + 30.0
        conn.settimeout(min(0.5, transport.connection_timeout))

    def _send_raw(self, payload: bytes) -> None:
        if payload:
            self.conn.sendall(payload)

    def _send_prelogin(
        self,
        frames: tuple[bytes, ...],
        *,
        secure_mode: bool = False,
    ) -> None:
        payload = b"".join(frames)
        if not payload:
            return
        secure = self.secure
        if secure_mode:
            if secure.step < 3 or not secure.send_state or not secure.send_md5:
                raise U2StockTransportError(
                    "secure send attempted before handshake"
                )
            secure.send_state, wire = self.transport._make_secure_frame(
                secure.send_md5,
                secure.send_state,
                secure.send_seq,
                payload,
            )
            secure.send_seq += 1
            self._send_raw(wire)
            return
        self._send_raw(payload)

    def _dispatch_prelogin(
        self,
        packet: ClassicEAFrame | ClassicEAShortFrame,
        *,
        secure_mode: bool = False,
    ) -> bool:
        transport = self.transport
        reply = transport.prelogin.dispatch(packet, self.context)
        self.registered_persona = transport._register_social(
            self.context,
            connection_id=self.connection_id,
            client_ip=self.addr[0],
            registered_persona=self.registered_persona,
        )
        session_id = (
            str(int(self.context.lobby_game_id or 0))
            if self.registered_persona
            else ""
        )
        if session_id != self.registered_game_session:
            transport.social.set_game_session(
                self.connection_id,
                self.registered_persona,
                GameId.UNDERGROUND2.value,
                session_id if session_id != "0" else "",
            )
            self.registered_game_session = (
                session_id if session_id != "0" else ""
            )
        command = (
            packet.tag
            if isinstance(packet, ClassicEAShortFrame)
            else packet.command
        )
        log.info(
            "underground2 stock prelogin command=%s result=%s encrypted=%s",
            command,
            reply.reason,
            secure_mode,
        )
        if secure_mode:
            self._send_prelogin(reply.frames, secure_mode=True)
        else:
            for frame in reply.frames:
                self._send_raw(frame)
        return reply.close_connection

    def _dispatch_plain(
        self,
        packet: ClassicEAFrame | ClassicEAShortFrame,
        *,
        secure_mode: bool = False,
    ) -> bool:
        if isinstance(packet, ClassicEAShortFrame):
            return self._dispatch_prelogin(packet, secure_mode=secure_mode)
        command = packet.command.casefold()
        if command == "@tic":
            log.info(
                "underground2 stock bootstrap command=%s payload_len=%d",
                packet.command,
                len(packet.payload),
            )
            return False
        if command in {"@dir", "?dir"}:
            reply = self.transport.bootstrap.dispatch(
                packet,
                client_ip=self.addr[0],
            )
            self.transport._refresh_challenge(
                self.context,
                self.transport.directory_registry,
                self.addr[0],
            )
            if secure_mode:
                self._send_prelogin(reply.frames, secure_mode=True)
            else:
                for frame in reply.frames:
                    self._send_raw(frame)
            advertised = self.transport.bootstrap.endpoint_for_client(
                self.addr[0]
            )
            log.info(
                "underground2 stock directory reply challenge=%s secure=%s lobby=%s:%d",
                "yes" if self.context.auth.mask else "no",
                secure_mode,
                advertised.host,
                advertised.port,
            )
            return reply.close_connection
        return self._dispatch_prelogin(packet, secure_mode=secure_mode)

    def _accept_secure_handshake_frame(self, frame: bytes) -> None:
        secure = self.secure
        transport = self.transport
        if secure.step == 0:
            if len(frame) != 30:
                raise U2StockTransportError(
                    f"secure hello length mismatch: {len(frame)}"
                )
            secure.token = frame[-16:]
            self._send_raw(transport._make_cert_frame(secure.peer_blob))
            secure.step = 1
            log.info("underground2 secure bootstrap hello accepted")
            return
        if secure.step == 1:
            if len(frame) != 140:
                raise U2StockTransportError(
                    f"secure RSA length mismatch: {len(frame)}"
                )
            cipher = int.from_bytes(frame[-128:], "big")
            block = pow(cipher, _RSA_D, _RSA_N).to_bytes(128, "big")
            unpadded = transport._rsa_unpad(block)
            if unpadded is None or len(unpadded) < 16:
                raise U2StockTransportError("secure RSA unpadding failed")
            work = unpadded[-16:]
            secure.recv_md5 = md5(
                work + b"1" + secure.token + secure.peer_blob
            ).digest()
            secure.send_md5 = md5(
                work + b"0" + secure.token + secure.peer_blob
            ).digest()
            secure.recv_state = transport._ksa(secure.recv_md5)
            secure.send_state = transport._ksa(secure.send_md5)
            secure.send_state, reply = transport._make_secure_frame(
                secure.send_md5,
                secure.send_state,
                secure.send_seq,
                b"Q",
            )
            secure.send_seq += 1
            self._send_raw(reply)
            secure.step = 2
            log.info("underground2 secure bootstrap RSA accepted")
            return
        if secure.step == 2:
            if len(frame) != 35:
                raise U2StockTransportError(
                    f"secure confirmation length mismatch: {len(frame)}"
                )
            secure.recv_state, body = transport._decrypt_secure_frame(
                secure.recv_state,
                frame,
            )
            secure.send_state, reply = transport._make_secure_frame(
                secure.send_md5,
                secure.send_state,
                secure.send_seq,
                b"7",
            )
            secure.send_seq += 1
            self._send_raw(reply)
            secure.step = 3
            log.info(
                "underground2 secure bootstrap established peer_echo=%s",
                (
                    "ok"
                    if len(body) >= 17 and body[1:17] == secure.peer_blob
                    else "different"
                ),
            )

    def _consume_secure_payload(self, frame: bytes) -> bool:
        secure = self.secure
        secure.recv_state, plain = self.transport._decrypt_secure_frame(
            secure.recv_state,
            frame,
        )
        assert secure.plain_buffer is not None
        secure.plain_buffer.extend(plain)
        close = False
        while True:
            try:
                parsed = self.transport._parse_plain_packet(
                    secure.plain_buffer,
                    max_frame_size=self.transport.max_frame_size,
                )
            except U2StockTransportError:
                packets = self.transport._extract_prelogin_messages(
                    secure.plain_buffer,
                    max_frame_size=self.transport.max_frame_size,
                )
                if not packets:
                    break
                for packet in packets:
                    close = (
                        self._dispatch_plain(packet, secure_mode=True)
                        or close
                    )
                continue
            if parsed is None:
                break
            packet, used = parsed
            del secure.plain_buffer[:used]
            close = self._dispatch_plain(packet, secure_mode=True) or close
        secure.step = max(secure.step, 5)
        return close

    def _consume_secure(self) -> tuple[int, bool]:
        consumed = 0
        close = False
        while True:
            total = self.transport._secure_packet_length(self.buffer[consumed:])
            if total is None or total == 0:
                break
            if len(self.buffer) - consumed < total:
                break
            frame = bytes(self.buffer[consumed : consumed + total])
            if self.secure.step < 3:
                self._accept_secure_handshake_frame(frame)
            else:
                close = self._consume_secure_payload(frame) or close
            consumed += total
            if close:
                break
        return consumed, close

    def _timeout_reason(self) -> str | None:
        now = time.monotonic()
        if self.context.authenticated and now >= self.next_session_touch:
            active_sessions = self.transport.prelogin.auth.active_sessions
            if hasattr(active_sessions, "touch"):
                if not active_sessions.touch(self.connection_id):
                    self.context.authenticated = False
                    return "session-revoked"
            self.next_session_touch = now + 30.0
        if (
            not self.context.authenticated
            and now - self.last_activity >= self.transport.connection_timeout
        ):
            return "idle-timeout"
        return None

    def _consume_buffer(self) -> str | None:
        while self.buffer:
            secure_length = self.transport._secure_packet_length(self.buffer)
            if self.secure.step or secure_length not in (None, 0):
                used, close = self._consume_secure()
                if used:
                    del self.buffer[:used]
                if close:
                    return "protocol-close"
                if not used:
                    break
                continue
            try:
                parsed = self.transport._parse_plain_packet(
                    self.buffer,
                    max_frame_size=self.transport.max_frame_size,
                )
            except U2StockTransportError as exc:
                log.warning(
                    "underground2 stock undecoded bytes len=%d head=%s error=%s",
                    len(self.buffer),
                    bytes(self.buffer[:64]).hex(),
                    exc,
                )
                return "invalid-stock-frame"
            if parsed is None:
                break
            packet, used = parsed
            del self.buffer[:used]
            if self._dispatch_plain(packet):
                return "protocol-close"
        return None

    def run(self) -> str:
        reason = "server-stop"
        try:
            while not self.stop_event.is_set():
                try:
                    data = self.conn.recv(8192)
                except socket.timeout:
                    timeout_reason = self._timeout_reason()
                    if timeout_reason is not None:
                        reason = timeout_reason
                        break
                    continue
                except OSError as exc:
                    reason = f"recv-error:{exc.errno or 'unknown'}"
                    break
                if not data:
                    reason = "peer-eof"
                    break
                self.last_activity = time.monotonic()
                if not self.raw_logged:
                    self.raw_logged = True
                    log.info(
                        "underground2 stock raw first recv len=%d hex=%s",
                        len(data),
                        data[:128].hex(),
                    )
                self.buffer.extend(data)
                protocol_reason = self._consume_buffer()
                if protocol_reason is not None:
                    reason = protocol_reason
                    break
        finally:
            if self.registered_persona:
                self.transport.social.unregister_lobby(self.connection_id)
            self.transport.prelogin.release(self.context)
        return reason
