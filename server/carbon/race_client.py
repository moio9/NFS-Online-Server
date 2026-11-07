"""Local UDP adapter over an authenticated, certificate-verified WSS stream."""
import asyncio
import logging
import socket
import ssl
import struct
import threading
from urllib.parse import urlsplit

from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

MAX_PACKET = 2048
log = logging.getLogger(__name__)


class OriginalRaceClient:
    def __init__(self, url, token, *, listen_port, client_port, ssl_context=None):
        parsed = urlsplit(url)
        if (parsed.scheme != 'wss' or not parsed.hostname or parsed.username or parsed.password
                or parsed.fragment or not 1024 <= listen_port <= 65535
                or not 1024 <= client_port <= 65535 or listen_port == client_port):
            raise ValueError('invalid secure race transport configuration')
        encoded = token.encode('ascii')
        if not 1 <= len(encoded) <= 128:
            raise ValueError('invalid authenticated session')
        self.url = url
        self._token = encoded
        self.listen_port = listen_port
        self.client_port = client_port
        self.ssl_context = ssl_context or ssl.create_default_context()
        self._ready = threading.Event()
        self._stopped = threading.Event()
        self._thread = None
        self._loop = None
        self._shutdown = None

    def start(self, *, timeout=15):
        if self._thread is not None:
            raise RuntimeError('race client already started')
        self._thread = threading.Thread(target=self._run_thread, daemon=True, name='carbon-race-client')
        self._thread.start()
        if not self._ready.wait(timeout):
            self.close()
            raise TimeoutError('secure race connection unavailable')

    def close(self):
        self._stopped.set()
        loop, shutdown = self._loop, self._shutdown
        if loop is not None and shutdown is not None and not loop.is_closed():
            loop.call_soon_threadsafe(shutdown.set)
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)
        self._token = b''

    def _run_thread(self):
        try:
            asyncio.run(self._run())
        except (OSError, RuntimeError) as exc:
            log.warning('Local race adapter stopped: %s', type(exc).__name__)

    async def _run(self):
        self._loop = asyncio.get_running_loop()
        self._shutdown = asyncio.Event()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
            udp.bind(('127.0.0.1', self.listen_port))
            udp.setblocking(False)
            while not self._stopped.is_set():
                try:
                    await self._connection(udp)
                except (OSError, asyncio.TimeoutError, ValueError, WebSocketException) as exc:
                    if not self._stopped.is_set():
                        log.warning('Secure race connection interrupted: %s', type(exc).__name__)
                if self._stopped.is_set():
                    break
                try:
                    await asyncio.wait_for(self._shutdown.wait(), timeout=1)
                except asyncio.TimeoutError:
                    pass

    async def _connection(self, udp):
        async with connect(self.url, ssl=self.ssl_context, compression=None, proxy=None,
                           max_size=65536, open_timeout=8, close_timeout=3) as ws:
            await ws.send(bytes([len(self._token)]) + self._token)
            buffer = bytearray()

            async def read_exactly(size):
                while len(buffer) < size:
                    frame = await ws.recv()
                    if not isinstance(frame, bytes) or not frame:
                        raise ValueError('invalid race stream frame')
                    buffer.extend(frame)
                    # A complete WebSocket frame can follow a partial UDP
                    # record retained from the preceding frame.
                    if len(buffer) > 65536 + MAX_PACKET + 2:
                        raise ValueError('race stream buffer exceeded')
                result = bytes(buffer[:size])
                del buffer[:size]
                return result

            if await asyncio.wait_for(read_exactly(1), 15) != b'\x01':
                raise ValueError('race stream authentication failed')
            # Drop datagrams queued for the previous room while reconnecting.
            # A new room has its own endpoint and retries its initial packets.
            for _ in range(1024):
                try:
                    udp.recvfrom(MAX_PACKET + 1)
                except BlockingIOError:
                    break
            self._ready.set()
            loop = asyncio.get_running_loop()

            async def to_server():
                while True:
                    try:
                        packet, peer = await asyncio.wait_for(loop.sock_recvfrom(udp, MAX_PACKET + 1), 30)
                    except asyncio.TimeoutError:
                        await ws.send(b'\x00\x00')
                        continue
                    if peer == ('127.0.0.1', self.client_port) and 0 < len(packet) <= MAX_PACKET:
                        await ws.send(struct.pack('!H', len(packet)) + packet)

            async def to_game():
                while True:
                    size = struct.unpack('!H', await read_exactly(2))[0]
                    if not 0 < size <= MAX_PACKET:
                        raise ValueError('invalid race datagram size')
                    packet = await read_exactly(size)
                    await loop.sock_sendto(udp, packet, ('127.0.0.1', self.client_port))

            tasks = [asyncio.create_task(to_server()), asyncio.create_task(to_game()),
                     asyncio.create_task(self._shutdown.wait())]
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    if not task.cancelled() and task.exception() is not None:
                        raise task.exception()
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
