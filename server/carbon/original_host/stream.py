"""Experimental authenticated stream-to-UDP transport for the original host.

Run only as a loopback listener inside the Carbon process. Remote callers
require an independently authenticated TLS transport. Session tokens are
checked against the same identity store as Theater and never logged.
"""
import asyncio
from dataclasses import dataclass
import logging
import socket
import struct
import threading

from carbon.accounts.identity import ClientPlatform
from carbon.original_host.roster_observer import RosterAckObserver

MAX_PACKET = 2048
MAX_SESSIONS = 128
IDLE_SECONDS = 120
log = logging.getLogger(__name__)


@dataclass
class _Endpoint:
    identity: object
    udp: socket.socket
    loop: asyncio.AbstractEventLoop
    writer: asyncio.StreamWriter
    gid: str = ''
    admitted: bool = False
    closed: bool = False
    roster_observer: object = None


class OriginalHostStream:
    def __init__(self, identities, *, original_port):
        if not 1024 <= original_port <= 65535:
            raise ValueError('invalid original host UDP port')
        self.identities = identities
        self.original_port = original_port
        self.lock = threading.RLock()
        self.sessions = {}
        self.on_roster_confirmed = None

    def _valid(self, token, session):
        identity = self.identities.resolve_session(token)
        return (identity is not None and identity.client_platform is ClientPlatform.XBOX360
                and identity is session.identity and not session.closed)

    def endpoint_for(self, connection, fields):
        gid = str(fields.get('GID', ''))
        token = connection.session_token
        with self.lock:
            session = self.sessions.get(token)
            if (not gid or not session or not self._valid(token, session) or connection.close_requested
                    or connection.forced_logoff_reason or connection.identity is not session.identity):
                return None
            if session.admitted and session.gid != gid:
                return None
            session.gid = gid
            session.admitted = True
            pid = fields.get('PID', '')
            if self.on_roster_confirmed is not None and str(pid).isdecimal() and 0 < int(pid) <= 0xffff:
                session.roster_observer = RosterAckObserver(int(pid))
            log.info('Original host stream admitted for room %s', gid)
            return ('127.0.0.1', session.udp.getsockname()[1])

    def _close_matching(self, predicate):
        with self.lock:
            matches = [(token, session) for token, session in self.sessions.items() if predicate(session)]
            for token, session in matches:
                session.closed = True
                self.sessions.pop(token, None)
                session.loop.call_soon_threadsafe(session.writer.close)
            if matches:
                log.info('Original host streams retired: count=%d', len(matches))
            return len(matches)

    def retire_member(self, gid, user_id):
        return self._close_matching(lambda session: session.gid == str(gid)
                                    and session.identity.user_id == user_id)

    def retire_room(self, gid):
        return self._close_matching(lambda session: session.gid == str(gid))

    def retire_connection(self, connection):
        token = connection.session_token
        with self.lock:
            session = self.sessions.get(token)
            if session is None or connection.identity is not session.identity:
                return 0
            session.closed = True
            self.sessions.pop(token, None)
            session.loop.call_soon_threadsafe(session.writer.close)
            return 1

    async def handle(self, reader, writer):
        peer = writer.get_extra_info('peername')
        token = ''
        session = None
        try:
            if not peer or peer[0] != '127.0.0.1':
                return
            size = (await asyncio.wait_for(reader.readexactly(1), 5))[0]
            if not 1 <= size <= 128:
                return
            token = (await asyncio.wait_for(reader.readexactly(size), 5)).decode('ascii')
            with self.lock:
                identity = self.identities.resolve_session(token)
                if (identity is None or identity.client_platform is not ClientPlatform.XBOX360
                        or token in self.sessions or len(self.sessions) >= MAX_SESSIONS):
                    return
                udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                try:
                    udp.bind(('127.0.0.1', 0))
                    udp.connect(('127.0.0.1', self.original_port))
                    udp.setblocking(False)
                    session = _Endpoint(identity, udp, asyncio.get_running_loop(), writer)
                    self.sessions[token] = session
                except BaseException:
                    udp.close()
                    raise
            writer.write(b'\x01')
            await writer.drain()
            loop = asyncio.get_running_loop()

            async def from_client():
                while True:
                    size = struct.unpack('!H', await asyncio.wait_for(reader.readexactly(2), IDLE_SECONDS))[0]
                    if size == 0:
                        with self.lock:
                            if not self._valid(token, session):
                                return
                        continue
                    if size > MAX_PACKET:
                        return
                    packet = await asyncio.wait_for(reader.readexactly(size), 5)
                    confirmed = False
                    # Identity revocation acquires the same reentrant lock.
                    # A datagram is sent only while the session remains valid.
                    with self.lock, self.identities._lock:
                        if not self._valid(token, session) or not session.admitted:
                            return
                        try:
                            session.udp.send(packet)
                            if session.roster_observer is not None:
                                confirmed = session.roster_observer.from_guest(packet)
                        except BlockingIOError:
                            pass  # UDP retry is handled by the game protocol.
                    if confirmed and self.on_roster_confirmed is not None:
                        try:
                            self.on_roster_confirmed(session.gid, session.identity.user_id)
                        except Exception:
                            log.exception('Original host roster observer callback failed')

            async def from_host():
                while True:
                    try:
                        packet = await asyncio.wait_for(loop.sock_recv(session.udp, MAX_PACKET + 1), 5)
                    except asyncio.TimeoutError:
                        with self.lock:
                            if not self._valid(token, session):
                                return
                        continue
                    with self.lock, self.identities._lock:
                        if not self._valid(token, session) or not session.admitted or not 1 <= len(packet) <= MAX_PACKET:
                            return
                        if session.roster_observer is not None:
                            session.roster_observer.from_host(packet)
                        writer.write(struct.pack('!H', len(packet)) + packet)
                    await writer.drain()

            tasks = [asyncio.create_task(from_client()), asyncio.create_task(from_host())]
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    error = None if task.cancelled() else task.exception()
                    log.warning('Original host stream ended: %s', type(error).__name__ if error else 'clean')
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        except (asyncio.IncompleteReadError, asyncio.TimeoutError, UnicodeError, OSError):
            pass
        finally:
            with self.lock:
                if session is not None:
                    session.closed = True
                    if self.sessions.get(token) is session:
                        self.sessions.pop(token, None)
                    session.udp.close()
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
