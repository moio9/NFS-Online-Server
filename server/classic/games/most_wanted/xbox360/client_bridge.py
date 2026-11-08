#!/data/data/com.termux/files/usr/bin/python
"""One-client local Xbox-to-Classic password bridge, for laboratory use only.

Both endpoints are required to be loopback. For a remote server, a separately
authenticated encrypted tunnel would be required; do not expose this bridge.
"""
import argparse
import asyncio
import getpass
import ipaddress
from urllib.parse import urlsplit
from classic.protocols.frame import ClassicEAFrame
from classic.protocols.password import make_password_token


def require_loopback(value):
    if not ipaddress.ip_address(value).is_loopback:
        raise ValueError('bridge endpoints must be loopback')
    return value


async def read_frame(reader):
    header = await reader.readexactly(12)
    length = int.from_bytes(header[8:12], 'big')
    if not 12 <= length <= 65535:
        raise ValueError('invalid Classic frame length')
    return ClassicEAFrame.decode_one(header + await reader.readexactly(length - 12))[0]


def with_fields(frame, values):
    body = b''.join(str(k).encode('ascii') + b'=' + str(v).encode('latin-1') + b'\n'
                    for k, v in values.items()) + b'\0'
    return ClassicEAFrame(frame.command, body, frame.reserved).encode()


def local_news(frame, local, port):
    """Point only MW's web documents at this client's optional local proxy."""
    if frame.reserved != 0x6e657737:  # Leave errors and other profiles untouched.
        return frame.encode()
    body = frame.payload.split(b'\0', 1)[0]
    lines = []
    rewritten = False
    for line in body.splitlines():
        key, separator, value = line.partition(b'=')
        if separator and key in (b'TOSA_URL', b'TOSAC_URL', b'NEWS_URL'):
            parsed = urlsplit(value.decode('ascii'))
            if parsed.scheme != 'http' or not parsed.netloc or not parsed.path.startswith('/'):
                raise ValueError('unsupported web document URL')
            path = parsed.path + (('?' + parsed.query) if parsed.query else '')
            line = key + b'=' + f'http://{local}:{port}{path}'.encode('ascii')
            rewritten = True
        lines.append(line)
    if not rewritten:
        return frame.encode()
    prefix = b'\n'.join(lines) + b'\n\0'
    return ClassicEAFrame.signed('news', prefix, max(len(frame.payload), len(prefix) + 8),
                                 reserved=frame.reserved).encode()


class Bridge:
    def __init__(self, local, upstream, account, password, *, web_upstream_port=None,
                 web_upstream_ip=None, web_listen_port=18080):
        self.local = require_loopback(local)
        self.upstream = require_loopback(upstream)
        for value in (account, password):
            if not value or len(value) > 128 or any(c in value for c in '\r\n\t\0'):
                raise ValueError('invalid credential format')
            value.encode('latin-1')
        self.account = account
        self.password = password
        self.mask = None
        self.directory_lock = asyncio.Lock()
        self.lobby_active = False
        self.web_upstream_ip = require_loopback(web_upstream_ip or upstream)
        self.web_upstream_port = web_upstream_port
        self.web_listen_port = int(web_listen_port)
        if not 1 <= self.web_listen_port <= 65535 or (
            web_upstream_port is not None and not 1 <= int(web_upstream_port) <= 65535
        ):
            raise ValueError('invalid web port')

    async def handle(self, client_reader, client_writer, directory):
        if directory:
            await self.directory_lock.acquire()
        elif self.lobby_active:
            client_writer.close()
            await client_writer.wait_closed()
            return
        else:
            self.lobby_active = True
        upstream_writer = None
        try:
            if not directory and not self.mask:
                raise ValueError('directory challenge is missing')
            upstream_reader, upstream_writer = await asyncio.open_connection(
                self.upstream, 30960 if directory else 10901)

            async def forward(reader, writer, from_client):
                while True:
                    frame = await read_frame(reader)
                    wire = frame.encode()
                    if directory and not from_client and frame.command == '@dir':
                        fields = frame.fields()
                        self.mask = fields.get('MASK')
                        if not self.mask:
                            raise ValueError('directory supplied no challenge')
                        fields.update(ADDR=self.local, PORT='10901')
                        wire = with_fields(frame, fields)
                    elif not directory and from_client and frame.command == 'auth':
                        values = frame.fields()
                        if not values.get('GTAG') or not self.mask:
                            raise ValueError('invalid Xbox login')
                        values['NAME'] = self.account
                        values['PASS'] = make_password_token(self.password, self.mask)
                        # Concurrent tunnel connections can share the same
                        # server-side peer IP. Carry this connection's own
                        # challenge so the password decoder can select it.
                        values['PSES'] = self.mask
                        wire = with_fields(frame, values)
                    elif not directory and not from_client and frame.command == 'news' and self.web_upstream_port is not None:
                        wire = local_news(frame, self.local, self.web_listen_port)
                    writer.write(wire)
                    await writer.drain()

            tasks = [asyncio.create_task(forward(client_reader, upstream_writer, True)),
                     asyncio.create_task(forward(upstream_reader, client_writer, False))]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*done, *pending, return_exceptions=True)
        except (OSError, ValueError, asyncio.IncompleteReadError):
            pass
        finally:
            if directory:
                self.directory_lock.release()
            else:
                self.lobby_active = False
            if upstream_writer is not None:
                upstream_writer.close()
                await upstream_writer.wait_closed()
            client_writer.close()
            await client_writer.wait_closed()

    async def handle_web(self, reader, writer):
        upstream_writer = None
        try:
            upstream_reader, upstream_writer = await asyncio.open_connection(
                self.web_upstream_ip, self.web_upstream_port)
            async def copy(source, target):
                while data := await source.read(65536):
                    target.write(data)
                    await target.drain()
                if target.can_write_eof():
                    target.write_eof()
            await asyncio.wait_for(asyncio.gather(copy(reader, upstream_writer),
                                                   copy(upstream_reader, writer)), 30)
        except (OSError, asyncio.TimeoutError):
            pass
        finally:
            if upstream_writer is not None:
                upstream_writer.close()
                await upstream_writer.wait_closed()
            writer.close()
            await writer.wait_closed()

    async def run(self):
        directory = await asyncio.start_server(lambda r, w: self.handle(r, w, True), self.local, 30960)
        try:
            lobby = await asyncio.start_server(lambda r, w: self.handle(r, w, False), self.local, 10901)
            try:
                web = None
                try:
                    if self.web_upstream_port is not None:
                        web = await asyncio.start_server(self.handle_web, self.local, self.web_listen_port)
                    print('Local Xbox authentication bridge ready', flush=True)
                    await asyncio.Event().wait()
                finally:
                    if web is not None:
                        web.close()
                        await web.wait_closed()
            finally:
                lobby.close()
                await lobby.wait_closed()
        finally:
            directory.close()
            await directory.wait_closed()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--listen-ip', default='127.0.0.4')
    parser.add_argument('--upstream-ip', default='127.0.0.1')
    parser.add_argument('--account', required=True)
    parser.add_argument('--web-upstream-ip', default=None,
                        help='loopback address of web service or local end of encrypted tunnel')
    parser.add_argument('--web-upstream-port', type=int, default=None,
                        help='enable the web proxy and forward to this local port')
    parser.add_argument('--web-listen-port', type=int, default=18080)
    args = parser.parse_args()
    password = getpass.getpass('Password: ')
    bridge = Bridge(args.listen_ip, args.upstream_ip, args.account, password,
                    web_upstream_ip=args.web_upstream_ip, web_upstream_port=args.web_upstream_port,
                    web_listen_port=args.web_listen_port)
    del password
    try:
        asyncio.run(bridge.run())
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
