"""Exercise configured transport destinations using real loopback sockets."""
import asyncio
import importlib.util
import os
from pathlib import Path
import socket
import sys
import unittest


HAS_TRANSPORT = all(importlib.util.find_spec(name) for name in ('starlette', 'uvicorn', 'websockets'))


@unittest.skipUnless(HAS_TRANSPORT, 'WebSocket transport dependencies are optional')
class WebTunnelRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_services_route_to_the_configured_nondefault_listeners(self):
        from websockets.asyncio.client import connect

        servers = []
        env = os.environ.copy()
        root = Path(__file__).resolve().parents[3]
        env['PYTHONPATH'] = os.pathsep.join((str(root / 'server'), str(root), env.get('PYTHONPATH', '')))
        for name in ('fesl', 'theater', 'messenger', 'race'):
            async def reply(reader, writer, service=name):
                try:
                    data = await reader.read(128)
                    writer.write(service.encode() + b':' + data)
                    await writer.drain()
                finally:
                    writer.close()
                    await writer.wait_closed()
            server = await asyncio.start_server(reply, '127.0.0.1', 0)
            servers.append(server)
            env[f'CARBON_TRANSPORT_{name.upper()}_PORT'] = str(server.sockets[0].getsockname()[1])

        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        process = await asyncio.create_subprocess_exec(
            sys.executable, '-m', 'uvicorn', 'carbon.transport.web_app:app',
            '--host', '127.0.0.1', '--port', str(port), '--no-access-log',
            cwd=root, env=env, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        try:
            for _ in range(100):
                if process.returncode is not None:
                    self.fail('transport exited before becoming ready')
                try:
                    reader, writer = await asyncio.open_connection('127.0.0.1', port)
                    writer.close()
                    await writer.wait_closed()
                    break
                except OSError:
                    await asyncio.sleep(0.05)
            else:
                self.fail('transport did not start')
            for name in ('fesl', 'theater', 'messenger', 'race'):
                async with connect(f'ws://127.0.0.1:{port}/api/carbon-transport/{name}', proxy=None) as ws:
                    await ws.send(b'probe')
                    self.assertEqual(await asyncio.wait_for(ws.recv(), 3), name.encode() + b':probe')
        finally:
            if process.returncode is None:
                process.terminate()
            await asyncio.wait_for(process.wait(), 5)
            for server in servers:
                server.close()
            await asyncio.gather(*(server.wait_closed() for server in servers))
