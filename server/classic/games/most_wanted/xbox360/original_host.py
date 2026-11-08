"""Experimental loopback bridge from the MW Xbox lobby to the PC LAN DLL.

The normal Python Xbox service supplies directory, authentication, news and
stats. The separately started original server owns its own lobby and rooms.
Neither the DLL nor an executable to load it is distributed by this module.
"""
from __future__ import annotations

import argparse
import asyncio
import ipaddress
import logging
import re

from classic.games.most_wanted.xbox360.client_bridge import read_frame, with_fields
from classic.protocols.frame import ClassicEAFrame

log = logging.getLogger(__name__)


def loopback(address: str) -> str:
    if not ipaddress.ip_address(address).is_loopback:
        raise ValueError("original host endpoints must use loopback")
    return address


def dll_identity_frame(frame: ClassicEAFrame, persona: str) -> bytes:
    """Change only identity fields; keep opaque Xbox fields byte-exact."""
    if not persona or len(persona) > 15 or any(c not in
            "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 _-" for c in persona):
        raise ValueError("account persona is not a valid Xbox gamertag")
    payload = frame.payload
    # Only replace complete named fields, leaving the remainder of the wire
    # payload untouched (including Xbox-specific opaque data and separators).
    pattern = re.compile(rb"(^|[\n\t])([A-Za-z0-9_]+)=([^\n\t\0]*)")
    removed = {b"PASS", b"PASSWORD", b"PWORD", b"PWD", b"PSES", b"EMAIL", b"MAIL"}
    names = {b"GTAG", b"NAME", b"PERS", b"USER", b"USERNAME", b"LOGIN"}
    found_pers = False

    def replace(match):
        nonlocal found_pers
        sep, key, _value = match.groups()
        upper = key.upper()
        if upper in removed:
            return sep
        if upper in names:
            found_pers |= upper == b"PERS"
            return sep + key + b"=" + persona.encode("ascii")
        return match.group(0)

    payload = pattern.sub(replace, payload)
    if frame.command == "pers" and not found_pers:
        body, terminator, trailer = payload.partition(b"\0")
        if body and not body.endswith((b"\n", b"\t")):
            body += b"\n"
        payload = body + b"PERS=" + persona.encode("ascii") + b"\n" + terminator + trailer
    return ClassicEAFrame(frame.command, payload, frame.reserved).encode()


class OriginalHostBridge:
    def __init__(self, listen_ip, python_ip, dll_ip, *, directory_port=30960,
                 lobby_port=10901, dll_port=9900, timeout=10):
        self.listen_ip = loopback(listen_ip)
        self.python_ip = loopback(python_ip)
        self.dll_ip = loopback(dll_ip)
        self.directory_port = int(directory_port)
        self.lobby_port = int(lobby_port)
        self.dll_port = int(dll_port)
        self.timeout = timeout
        if not all(1 <= p <= 65535 for p in (self.directory_port, self.lobby_port, self.dll_port)):
            raise ValueError("invalid port")

    async def directory(self, reader, writer):
        upstream = None
        try:
            source, upstream = await asyncio.open_connection(self.python_ip, self.directory_port)
            async def request():
                while True:
                    frame = await read_frame(reader)
                    upstream.write(frame.encode())
                    await upstream.drain()
            async def response():
                while True:
                    frame = await read_frame(source)
                    if frame.command == "@dir":
                        fields = frame.fields()
                        fields.update(ADDR=self.listen_ip, PORT=str(self.lobby_port))
                        writer.write(with_fields(frame, fields))
                    else:
                        writer.write(frame.encode())
                    await writer.drain()
            await self._run_pair(request(), response())
        except (OSError, ValueError, asyncio.IncompleteReadError):
            pass
        finally:
            await self._close(upstream)
            await self._close(writer)

    @staticmethod
    async def _close(writer):
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, ConnectionError):
                pass

    @staticmethod
    async def _run_pair(first, second):
        tasks = [asyncio.create_task(first), asyncio.create_task(second)]
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*done, *pending, return_exceptions=True)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def lobby(self, reader, writer):
        py_writer = dll_writer = None
        py_task = None
        try:
            py_reader, py_writer = await asyncio.open_connection(self.python_ip, self.lobby_port)
            dll_reader, dll_writer = await asyncio.open_connection(self.dll_ip, self.dll_port)
            queues = {name: asyncio.Queue(maxsize=4) for name in ("news", "auth", "pers", "user")}
            authenticated = False
            selected = False
            persona = ""

            async def read_python():
                while True:
                    frame = await read_frame(py_reader)
                    if frame.command == "~png":
                        py_writer.write(ClassicEAFrame.from_fields("~png", {}).encode())
                        await py_writer.drain()
                    elif frame.command in queues:
                        await queues[frame.command].put(frame)

            py_task = asyncio.create_task(read_python())

            async def ask_python(frame):
                py_writer.write(frame.encode())
                await py_writer.drain()
                return await asyncio.wait_for(queues[frame.command].get(), self.timeout)

            async def client_to_dll():
                nonlocal authenticated, selected, persona
                while True:
                    frame = await read_frame(reader)
                    command = frame.command
                    if command == "news":
                        reply = await ask_python(frame)
                        writer.write(reply.encode())
                        await writer.drain()
                        continue
                    if command == "auth":
                        if authenticated:
                            raise ValueError("duplicate authentication")
                        reply = await ask_python(frame)
                        if reply.reserved:
                            writer.write(reply.encode())
                            await writer.drain()
                            continue
                        persona = reply.fields().get("NAME", "")
                        wire = dll_identity_frame(frame, persona)
                        authenticated = True
                    elif command == "pers":
                        if not authenticated or selected:
                            raise ValueError("invalid persona selection")
                        reply = await ask_python(frame)
                        if reply.reserved:
                            writer.write(reply.encode())
                            await writer.drain()
                            continue
                        wire = dll_identity_frame(frame, persona)
                        selected = True
                    elif command == "user":
                        if not selected:
                            raise ValueError("user before persona selection")
                        reply = await ask_python(frame)
                        writer.write(reply.encode())
                        await writer.drain()
                        continue
                    else:
                        if not authenticated and command not in ("addr", "skey", "~png"):
                            raise ValueError("command before authentication")
                        if authenticated and not selected and command not in ("addr", "skey", "~png"):
                            raise ValueError("command before persona selection")
                        wire = frame.encode()
                    dll_writer.write(wire)
                    await dll_writer.drain()

            async def dll_to_client():
                while True:
                    frame = await read_frame(dll_reader)
                    writer.write(frame.encode())
                    await writer.drain()

            await self._run_pair(client_to_dll(), dll_to_client())
        except (OSError, ValueError, asyncio.IncompleteReadError):
            pass
        finally:
            if py_task is not None:
                py_task.cancel()
                await asyncio.gather(py_task, return_exceptions=True)
            await self._close(py_writer)
            await self._close(dll_writer)
            await self._close(writer)

    async def serve(self):
        # Do not announce a usable backend when the separately managed DLL
        # host is missing. The check sends no game or account information.
        try:
            _reader, probe = await asyncio.wait_for(
                asyncio.open_connection(self.dll_ip, self.dll_port), self.timeout)
        except (OSError, asyncio.TimeoutError) as exc:
            raise ConnectionError("original MW DLL host is not reachable") from exc
        await self._close(probe)
        directory = await asyncio.start_server(self.directory, self.listen_ip, self.directory_port)
        try:
            lobby = await asyncio.start_server(self.lobby, self.listen_ip, self.lobby_port)
            async with directory, lobby:
                log.info("Experimental original MW host ready on %s", self.listen_ip)
                await asyncio.Future()
        finally:
            directory.close()
            await directory.wait_closed()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen-ip", default="127.0.0.9")
    parser.add_argument("--python-ip", default="127.0.0.8")
    parser.add_argument("--dll-ip", default="127.0.0.1")
    parser.add_argument("--dll-port", type=int, default=9900)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    asyncio.run(OriginalHostBridge(args.listen_ip, args.python_ip, args.dll_ip,
                                   dll_port=args.dll_port).serve())


if __name__ == "__main__":
    main()
