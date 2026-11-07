"""Loopback-only FESL/Theater relay for the original rebroadcaster."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal


class LoopbackRelay:
    def __init__(self, listen_port: int, target_port: int, *, max_connections=16):
        if not 0 <= listen_port <= 65535 or not 1 <= target_port <= 65535:
            raise ValueError('invalid relay port')
        if max_connections < 1:
            raise ValueError('max_connections must be positive')
        self.listen_port = listen_port
        self.target_port = target_port
        self.max_connections = max_connections
        self.server = None
        self.connections = set()

    async def start(self):
        if self.server is not None:
            raise RuntimeError('relay already running')
        self.server = await asyncio.start_server(self._accept, '127.0.0.1', self.listen_port)
        return self.server.sockets[0].getsockname()[1]

    async def _accept(self, reader, writer):
        task = asyncio.current_task()
        if len(self.connections) >= self.max_connections:
            writer.close()
            await writer.wait_closed()
            return
        self.connections.add(task)
        upstream_writer = None
        pipes = []
        try:
            upstream_reader, upstream_writer = await asyncio.wait_for(
                asyncio.open_connection('127.0.0.1', self.target_port), 5)
            async def pipe(source, destination):
                while data := await source.read(65536):
                    destination.write(data)
                    await destination.drain()
            pipes = [asyncio.create_task(pipe(reader, upstream_writer)),
                     asyncio.create_task(pipe(upstream_reader, writer))]
            await asyncio.wait(pipes, return_when=asyncio.FIRST_COMPLETED)
        except (OSError, asyncio.TimeoutError):
            pass
        finally:
            for pipe_task in pipes:
                pipe_task.cancel()
            if pipes:
                await asyncio.gather(*pipes, return_exceptions=True)
            for endpoint in (writer, upstream_writer):
                if endpoint is not None:
                    endpoint.close()
                    with contextlib.suppress(OSError):
                        await endpoint.wait_closed()
            self.connections.discard(task)

    async def close(self):
        server, self.server = self.server, None
        if server is not None:
            server.close()
        tasks = tuple(self.connections)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if server is not None:
            await server.wait_closed()


async def serve(args):
    relays = [LoopbackRelay(args.fesl_listen, args.fesl_target),
              LoopbackRelay(args.theater_listen, args.theater_target)]
    if args.fesl_listen == args.theater_listen:
        raise ValueError('relay listener ports must differ')
    done = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, done.set)
    try:
        for relay in relays:
            await relay.start()
        print('Original host loopback relays ready', flush=True)
        await done.wait()
    finally:
        await asyncio.gather(*(relay.close() for relay in relays))


def main(argv=None):
    parser = argparse.ArgumentParser(description='Run original-host loopback relays')
    parser.add_argument('--fesl-listen', type=int, default=17220)
    parser.add_argument('--fesl-target', type=int, default=18210)
    parser.add_argument('--theater-listen', type=int, default=17225)
    parser.add_argument('--theater-target', type=int, default=18215)
    args = parser.parse_args(argv)
    asyncio.run(serve(args))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
