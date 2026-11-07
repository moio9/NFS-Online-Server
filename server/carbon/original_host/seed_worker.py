"""Opt-in in-process initializer for one original-host Unranked room.

The service identity must already exist with password login disabled. All
network sockets opened here are loopback-only. This component is not enabled
by default and does not provision accounts or expose a listener.
"""
import logging
import re
import socket
import threading
import time

from carbon.accounts.identity import ClientPlatform
from carbon.fesl.frame import FESLFrame, FESLStreamDecoder
from carbon.gamemanager.race_session import session_attributes
from carbon.original_host.local_client import LocalOriginalRaceClient
from carbon.original_host.roster_observer import _clear_packets
from carbon.rebroadcaster.handshake import channel7_identity, control_payload, hash_sar_decimal
from carbon.transport.commudp import CommUDPActive, CommUDPControl, CommUDPType, parse_channel_one
from carbon.transport.prototunnel import TunnelDatagram, TunnelPacket, cipher_region_size

log = logging.getLogger(__name__)


def _wire(datagram):
    headers = b''.join(bytes((len(p.payload) >> 4, ((len(p.payload) & 15) << 4) | p.channel))
                       for p in datagram.packets)
    return cipher_region_size(datagram.packets).to_bytes(2, 'little') + headers + b''.join(p.payload for p in datagram.packets)


def _request(sock, command, fields):
    sock.sendall(FESLFrame.from_fields(command, fields, transaction=0).encode())


def _response(sock, decoder, command, stop, *, gid=None, timeout=5):
    deadline = time.monotonic() + timeout
    while not stop.is_set() and time.monotonic() < deadline:
        try:
            data = sock.recv(65536)
        except socket.timeout:
            continue
        if not data:
            raise ConnectionError('local Theater closed')
        for reply in decoder.feed(data):
            if reply.command == 'EGAM' and reply.fields.get('ERR'):
                raise RuntimeError('original host rejected initializer')
            if reply.command == command and (gid is None or reply.fields.get('GID') == gid):
                return reply.fields
    raise TimeoutError('local Theater response unavailable')


class OriginalSeedWorker:
    def __init__(self, identities, credentials, games, *, account, theater_port, stream_port):
        if (not account or not 1024 <= theater_port <= 65535
                or not 1024 <= stream_port <= 65535 or theater_port == stream_port):
            raise ValueError('invalid original-host initializer settings')
        self.identities = identities
        self.credentials = credentials
        self.games = games
        self.account = account
        self.theater_port = theater_port
        self.stream_port = stream_port
        self._lock = threading.Lock()
        self._thread = None
        self._stop = threading.Event()

    def is_running(self):
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def start(self, version, gid):
        if (not re.fullmatch(r'[A-Za-z0-9_.+\-]{1,64}', version or '')
                or not str(gid).isascii() or not str(gid).isdecimal() or int(gid) <= 0):
            raise ValueError('invalid original-host initializer target')
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError('original-host initializer already running')
            account = self.credentials.get_account(self.account)
            if account is None or account.enabled:
                raise ValueError('initializer requires an existing disabled service account')
            identity, token = self.identities.login(self.account, client_platform=ClientPlatform.XBOX360)
            self._stop = threading.Event()
            thread = threading.Thread(target=self._run, args=(identity, token, version, str(gid), self._stop),
                                      name='carbon-original-seed', daemon=True)
            self._thread = thread
            try:
                thread.start()
            except BaseException:
                self.identities.revoke_session(token)
                self._thread = None
                raise

    def release(self):
        self._stop.set()

    def close(self):
        self.release()
        with self._lock:
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(8)
            if thread.is_alive():
                raise TimeoutError('original-host initializer did not stop')

    def _run(self, identity, token, version, gid, stop):
        try:
            self._serve(identity, token, version, gid, stop)
        except Exception as exc:
            if not stop.is_set():
                log.warning('Original host initializer stopped: %s', type(exc).__name__)
        finally:
            self.identities.revoke_session(token)

    def _wait_departure(self, gid, user_id, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.games._lock:
                game = self.games.get(gid)
                if game is None or user_id not in game.participants:
                    return True
            time.sleep(.05)
        return False

    def _serve(self, identity, token, version, gid, stop):
        with socket.create_connection(('127.0.0.1', self.theater_port), timeout=3) as theater, \
             socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
            theater.settimeout(.5)
            udp.bind(('127.0.0.1', 0))
            udp.settimeout(.5)
            adapter = LocalOriginalRaceClient(token, stream_port=self.stream_port,
                                              listen_port=0, client_port=udp.getsockname()[1])
            try:
                adapter.start()
                peer = ('127.0.0.1', adapter.listen_port)
                decoder = FESLStreamDecoder()
                _request(theater, 'USER', {'TID': '1', 'LKEY': token})
                _response(theater, decoder, 'USER', stop)
                _request(theater, 'GLST', {'TID': '2', 'LID': '257'})
                _response(theater, decoder, 'GDAT', stop, gid=gid)
                port = str(udp.getsockname()[1])
                _request(theater, 'EGAM', {'TID': '3', 'LID': '257', 'GID': gid,
                                           'PORT': port, 'R-INT-PORT': port, 'R-INT-IP': '127.0.0.1'})
                entry = _response(theater, decoder, 'EGEG', stop, gid=gid)
                ticket = entry['TICKET'].encode('ascii')
                if not 1 <= len(ticket) <= 255:
                    raise ValueError('invalid initializer ticket')
                hello = TunnelDatagram(0, (TunnelPacket(7, channel7_identity(hash_sar_decimal(identity.user_id), stage=0)),
                                           TunnelPacket(1, control_payload(CommUDPType.CONNECT, 7))))
                udp.sendto(_wire(hello), peer)
                expected = hash_sar_decimal(entry['HUID']).to_bytes(4, 'big')
                deadline = time.monotonic() + 5
                connection_id = None
                while not stop.is_set() and time.monotonic() < deadline:
                    try: data, source = udp.recvfrom(2049)
                    except socket.timeout: continue
                    if source != peer: continue
                    packets = _clear_packets(data)
                    sender = next((p.payload for p in packets if p.channel == 7), b'')
                    if len(sender) != 6 or sender[1:5] != expected: continue
                    control = next((c for p in packets if isinstance((c := parse_channel_one(p)), CommUDPControl)
                                    and c.kind == CommUDPType.CONNECT), None)
                    if control is not None:
                        connection_id = control.connection_id
                        break
                if connection_id is None:
                    raise TimeoutError('original host CONNECT unavailable')
                confirm = TunnelDatagram(5, (hello.packets[0],
                    TunnelPacket(1, control_payload(CommUDPType.CONNECT_ACK, connection_id)),
                    TunnelPacket(1, control_payload(CommUDPType.CONNECT, connection_id))))
                udp.sendto(_wire(confirm), peer)
                deadline = time.monotonic() + 5
                confirmed = False
                while not stop.is_set() and time.monotonic() < deadline:
                    try: data, source = udp.recvfrom(2049)
                    except socket.timeout: continue
                    if source != peer: continue
                    confirmed = any(isinstance((c := parse_channel_one(p)), CommUDPControl)
                                    and c.kind == CommUDPType.CONNECT_ACK and c.connection_id == connection_id
                                    for p in _clear_packets(data))
                    if confirmed: break
                if not confirmed:
                    raise TimeoutError('original host CONNECT_ACK unavailable')
                body = bytes.fromhex('01808004800000') + bytes((len(ticket),)) + ticket
                final = TunnelDatagram(5, (TunnelPacket(1, control_payload(CommUDPType.CONNECT_ACK, connection_id)),
                    TunnelPacket(1, bytes.fromhex('00000100000000ff')),
                    TunnelPacket(1, bytes.fromhex('00000100000000ff') + body + b'\x04')))
                udp.sendto(_wire(final), peer)
                deadline = time.monotonic() + 5
                seen = set()
                roster_ack = False
                self_join_sequence = None
                seq = 0
                while not stop.is_set() and time.monotonic() < deadline:
                    try: data, source = udp.recvfrom(2049)
                    except socket.timeout: continue
                    if source != peer: continue
                    active = [c for p in _clear_packets(data) if isinstance((c := parse_channel_one(p)), CommUDPActive)]
                    for item in active:
                        message = item.game_manager
                        if message is not None:
                            seen.add(message.message_type)
                            if (message.message_type == 5 and len(message.body) >= 4
                                    and int.from_bytes(message.body[2:4], 'big') == int(entry['PID'])):
                                self_join_sequence = item.sequence & 0x0fffffff
                    if 3 in seen and not roster_ack and active:
                        seq = max(c.sequence & 0x0fffffff for c in active)
                        ack = TunnelDatagram(0, (TunnelPacket(1, (0x101).to_bytes(4, 'big') +
                                                                seq.to_bytes(4, 'big') + bytes.fromhex('018404')),))
                        udp.sendto(_wire(ack), peer)
                        roster_ack = True
                    if {2, 3, 5}.issubset(seen) and self_join_sequence is not None:
                        break
                if not {2, 3, 5}.issubset(seen) or self_join_sequence is None:
                    raise TimeoutError('original host roster incomplete')
                ack = TunnelDatagram(0, (TunnelPacket(1, (0x102).to_bytes(4, 'big') +
                                                        self_join_sequence.to_bytes(4, 'big')),))
                udp.sendto(_wire(ack), peer)
                attributes = session_attributes({'B-U-game_type': '1', 'B-U-game_mode': '0',
                                                 'B-U-version': version, 'B-U-max_online_player': '2'})
                publication = TunnelDatagram(0, (TunnelPacket(1, (0x102).to_bytes(4, 'big') +
                                                                seq.to_bytes(4, 'big') + attributes + b'\x04'),))
                udp.sendto(_wire(publication), peer)
                log.info('Original host initializer published Unranked attributes')
                deadline = time.monotonic() + 60
                next_repeat = time.monotonic() + 1
                outbound_seq, last_seq = 0x103, 0x100
                while not stop.is_set() and time.monotonic() < deadline:
                    if time.monotonic() >= next_repeat:
                        keepalive = TunnelDatagram(0, (TunnelPacket(1, outbound_seq.to_bytes(4, 'big') +
                            last_seq.to_bytes(4, 'big') + bytes.fromhex('000000001d0004')),))
                        udp.sendto(_wire(keepalive), peer)
                        outbound_seq += 1
                        next_repeat = time.monotonic() + 1
                    try: data, source = udp.recvfrom(2049)
                    except socket.timeout: continue
                    if source != peer: continue
                    packets = _clear_packets(data)
                    if any(isinstance((c := parse_channel_one(p)), CommUDPControl)
                           and c.kind == CommUDPType.DISCONNECT for p in packets):
                        raise ConnectionError('original host closed initializer')
                    active = [c for p in packets if isinstance((c := parse_channel_one(p)), CommUDPActive)]
                    if active:
                        last_seq = max(last_seq, *(c.sequence & 0x0fffffff for c in active))
                        ack = TunnelDatagram(0, (TunnelPacket(1, (0x102).to_bytes(4, 'big') +
                                                                last_seq.to_bytes(4, 'big')),))
                        udp.sendto(_wire(ack), peer)
                disconnect = TunnelDatagram(0, (TunnelPacket(1, control_payload(CommUDPType.DISCONNECT, connection_id)),))
                adapter.send_final(_wire(disconnect))
                if not self._wait_departure(gid, identity.user_id):
                    log.warning('Original host initializer departure was not confirmed')
                log.info('Original host initializer released')
            finally:
                adapter.close()
