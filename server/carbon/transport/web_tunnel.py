"""Opt-in WebSocket transport for fixed Carbon TCP services.

The caller mounts ``tunnel`` on a WebSocket route and terminates TLS before
the ASGI application. No arbitrary destination or public listener is created
by this module. The server must support WebSockets and enforce TLS at its
reverse proxy before exposing the route.

Destinations use loopback and the CARBON_TRANSPORT_{FESL,THEATER,
MESSENGER,RACE,MAD}_PORT environment variables, with legacy defaults when
unset. The original-host stack derives these values from the configured
listeners.
"""
import asyncio
from contextlib import suppress
import logging
import os

from starlette.websockets import WebSocketDisconnect

def _configured_port(service, default):
    name = f'CARBON_TRANSPORT_{service.upper()}_PORT'
    try:
        port = int(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise ValueError(f'{name} must be a valid port') from exc
    if not 1 <= port <= 65535:
        raise ValueError(f'{name} must be a valid port')
    return port


PORTS = {name: _configured_port(name, default) for name, default in (
    ('fesl', 18210), ('theater', 18215), ('messenger', 13505), ('race', 19141), ('mad', 9000),
)}
MAX_PACKET = 65536
MAX_CONNECTIONS = 128
_slots = asyncio.Semaphore(MAX_CONNECTIONS)
log = logging.getLogger(__name__)


async def tunnel(websocket):
    service = websocket.path_params.get('service', '')
    port = PORTS.get(service)
    # Browser origins have no use for this binary game transport. Reject them
    # to avoid making third-party webpages into uncontrolled clients.
    if port is None or websocket.headers.get('origin'):
        await websocket.close(code=1008)
        return
    if _slots.locked():
        await websocket.close(code=1013)
        return
    async with _slots:
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection('127.0.0.1', port), 5)
        except (OSError, asyncio.TimeoutError):
            await websocket.close(code=1013)
            return
        try:
            await websocket.accept()

            async def from_client():
                while True:
                    frame = await websocket.receive()
                    if frame['type'] == 'websocket.disconnect':
                        return
                    data = frame.get('bytes')
                    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_PACKET:
                        return
                    writer.write(data)
                    await writer.drain()

            async def from_server():
                while True:
                    data = await reader.read(MAX_PACKET)
                    if not data:
                        return
                    await websocket.send_bytes(data)

            tasks = [asyncio.create_task(from_client()), asyncio.create_task(from_server())]
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    if not task.cancelled() and task.exception() is not None:
                        log.warning('Carbon transport closed: %s', type(task.exception()).__name__)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        except (WebSocketDisconnect, OSError, asyncio.TimeoutError):
            pass
        finally:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()
            with suppress(RuntimeError, WebSocketDisconnect):
                await websocket.close()
