"""Loopback-only UDP adapter for a local authenticated original-host stream.

This is intended for an internal initializer. It never opens a remote socket;
public players must use the separately authenticated TLS transport.
"""
import logging
import select
import socket
import struct
import threading
import time

MAX_PACKET = 2048
log = logging.getLogger(__name__)


class LocalOriginalRaceClient:
    def __init__(self, token, *, stream_port, listen_port, client_port):
        encoded = token.encode('ascii')
        ports = (stream_port, client_port)
        if (not 1 <= len(encoded) <= 128 or len(set(ports)) != 2
                or any(not isinstance(port, int) or not 1024 <= port <= 65535 for port in ports)
                or not isinstance(listen_port, int)
                or (listen_port != 0 and (not 1024 <= listen_port <= 65535 or listen_port in ports))):
            raise ValueError('invalid local original-host stream configuration')
        self._token = encoded
        self.stream_port = stream_port
        self.listen_port = listen_port
        self.client_port = client_port
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread = None
        self._error = None
        self._sent_condition = threading.Condition()
        self._sent_count = 0
        self._send_lock = threading.Lock()
        self._stream = None
        self._finalized = False

    def start(self, *, timeout=10):
        if self._thread is not None:
            raise RuntimeError('local original-host adapter already started')
        self._thread = threading.Thread(target=self._run, name='carbon-local-race', daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            self.close()
            raise TimeoutError('local original-host stream did not start')
        if self._error is not None:
            self.close()
            raise RuntimeError('local original-host stream could not start') from self._error

    def close(self):
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(5)
            if self._thread.is_alive():
                raise TimeoutError('local original-host stream failed to stop')
        self._token = b''

    def outbound_count(self):
        with self._sent_condition:
            return self._sent_count

    def wait_for_outbound(self, previous, timeout=1):
        deadline = time.monotonic() + timeout
        with self._sent_condition:
            while self._sent_count <= previous:
                if self._thread is None or not self._thread.is_alive():
                    return False
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._sent_condition.wait(remaining)
            return True

    def send_final(self, packet):
        if not 0 < len(packet) <= MAX_PACKET:
            raise ValueError('invalid final local stream packet')
        with self._send_lock:
            if self._stream is None or self._finalized:
                raise RuntimeError('local original-host stream is unavailable')
            # Serialize with UDP forwarding and prevent any later datagram from
            # overtaking the final disconnect on the same TCP stream.
            self._finalized = True
            self._stream.sendall(struct.pack('!H', len(packet)) + packet)

    def _run(self):
        started = False
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
                udp.bind(('127.0.0.1', self.listen_port))
                self.listen_port = udp.getsockname()[1]
                if self.listen_port in (self.stream_port, self.client_port):
                    raise OSError('local adapter port conflicts with another endpoint')
                with socket.create_connection(('127.0.0.1', self.stream_port), timeout=3) as stream:
                    stream.settimeout(3)
                    stream.sendall(bytes((len(self._token),)) + self._token)
                    if stream.recv(1) != b'\x01':
                        raise ConnectionError('local stream authentication failed')
                    stream.settimeout(2)
                    with self._send_lock:
                        self._stream = stream
                    started = True
                    self._ready.set()
                    pending = bytearray()
                    next_keepalive = time.monotonic() + 30
                    while not self._stop.is_set():
                        ready, _, _ = select.select((udp, stream), (), (), 0.2)
                        if udp in ready:
                            packet, peer = udp.recvfrom(MAX_PACKET + 1)
                            if peer == ('127.0.0.1', self.client_port) and 0 < len(packet) <= MAX_PACKET:
                                with self._send_lock:
                                    if not self._finalized:
                                        stream.sendall(struct.pack('!H', len(packet)) + packet)
                                        with self._sent_condition:
                                            self._sent_count += 1
                                            self._sent_condition.notify_all()
                                next_keepalive = time.monotonic() + 30
                        if stream in ready:
                            data = stream.recv(65536)
                            if not data:
                                break
                            pending.extend(data)
                            while len(pending) >= 2:
                                length = struct.unpack_from('!H', pending)[0]
                                if not 0 < length <= MAX_PACKET:
                                    raise ValueError('invalid local stream packet length')
                                if len(pending) < length + 2:
                                    break
                                packet = bytes(pending[2:length+2])
                                del pending[:length+2]
                                udp.sendto(packet, ('127.0.0.1', self.client_port))
                        if time.monotonic() >= next_keepalive:
                            with self._send_lock:
                                if not self._finalized:
                                    stream.sendall(b'\x00\x00')
                            next_keepalive = time.monotonic() + 30
        except (OSError, ValueError) as exc:
            if not started:
                self._error = exc
            elif not self._stop.is_set():
                log.warning('Local original-host stream stopped: %s', type(exc).__name__)
        finally:
            with self._send_lock:
                self._stream = None
            self._token = b''
            self._ready.set()
            with self._sent_condition:
                self._sent_condition.notify_all()
