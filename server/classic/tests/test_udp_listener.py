import errno
import socket
import unittest
from unittest import mock

from classic.core.config import Endpoint
from classic.core.udp import UDPListener


class UDPListenerTests(unittest.TestCase):
    def test_peer_error_does_not_stop_next_race_datagram(self):
        # Winsock reports a previous send to a closed client port through
        # recvfrom. That client leaving must not stop the shared MW/U2 port.
        errors = [
            ConnectionResetError(errno.ECONNRESET, "peer reset"),
            ConnectionRefusedError(errno.ECONNREFUSED, "port unreachable"),
            OSError(errno.ENETRESET, "network reset"),
            OSError(10054, "WSAECONNRESET"),
            OSError(10052, "WSAENETRESET"),
            OSError(10061, "WSAECONNREFUSED"),
        ]
        win_error = OSError("Windows port unreachable")
        win_error.winerror = 10054
        errors.append(win_error)
        for error in errors:
            with self.subTest(error=error):
                handler = mock.Mock()
                listener = UDPListener(Endpoint("127.0.0.1", 0), handler, name="race")
                sock = mock.Mock()
                listener._socket = sock
                peer = ("192.0.2.10", 3658)
                sock.recvfrom.side_effect = [error, (b"next-race", peer)]

                def receive(payload, address):
                    listener.stop_event.set()
                    return ()

                handler.side_effect = receive
                with mock.patch("classic.core.udp.log"):
                    listener._loop()
                handler.assert_called_once_with(b"next-race", peer)

    def test_fatal_receive_error_is_logged_and_stops(self):
        handler = mock.Mock()
        listener = UDPListener(Endpoint("127.0.0.1", 0), handler, name="race")
        listener._socket = mock.Mock()
        listener._socket.recvfrom.side_effect = OSError(errno.EBADF, "bad socket")
        with self.assertLogs("classic.core.udp", level="ERROR"):
            listener._loop()
        handler.assert_not_called()
        self.assertEqual(listener._socket.recvfrom.call_count, 1)

    def test_stop_during_receive_is_quiet(self):
        listener = UDPListener(Endpoint("127.0.0.1", 0), mock.Mock(), name="race")
        listener._socket = mock.Mock()

        def stopped_receive(_size):
            listener.stop_event.set()
            raise OSError(errno.EBADF, "closed during shutdown")

        listener._socket.recvfrom.side_effect = stopped_receive
        with self.assertNoLogs("classic.core.udp"):
            listener._loop()

    def test_windows_start_disables_udp_connection_reset_reporting(self):
        listener = UDPListener(Endpoint("127.0.0.1", 0), mock.Mock(), name="race")
        with (
            mock.patch("classic.core.udp.socket.socket") as factory,
            mock.patch.object(socket, "SIO_UDP_CONNRESET", 0x9800000C, create=True),
            mock.patch("classic.core.udp.Thread"),
        ):
            factory.return_value.getsockname.return_value = ("127.0.0.1", 20000)
            listener.start()
            factory.return_value.ioctl.assert_called_once_with(0x9800000C, False)
            listener.stop()

    def test_ioctl_failure_still_starts_listener(self):
        listener = UDPListener(Endpoint("127.0.0.1", 0), mock.Mock(), name="race")
        with (
            mock.patch("classic.core.udp.socket.socket") as factory,
            mock.patch.object(socket, "SIO_UDP_CONNRESET", 0x9800000C, create=True),
            mock.patch("classic.core.udp.Thread") as thread,
        ):
            factory.return_value.getsockname.return_value = ("127.0.0.1", 20000)
            factory.return_value.ioctl.side_effect = OSError("unsupported ioctl")
            with self.assertLogs("classic.core.udp", level="WARNING"):
                self.assertEqual(listener.start(), Endpoint("127.0.0.1", 20000))
            thread.return_value.start.assert_called_once()
            listener.stop()

    def test_real_udp_socket_receives_and_replies_after_injected_reset(self):
        received = []

        def echo(payload, peer):
            received.append(payload)
            return ((payload, peer),)

        listener = UDPListener(Endpoint("127.0.0.1", 0), echo, name="race")
        real_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        wrapped_socket = mock.Mock(wraps=real_socket)
        receive_count = 0

        def receive(size):
            nonlocal receive_count
            receive_count += 1
            if receive_count == 2:
                raise ConnectionResetError(errno.ECONNRESET, "departed peer")
            return real_socket.recvfrom(size)

        wrapped_socket.recvfrom.side_effect = receive
        self.addCleanup(real_socket.close)
        self.addCleanup(listener.stop)
        with mock.patch("classic.core.udp.socket.socket", return_value=wrapped_socket):
            endpoint = listener.start()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
            client.settimeout(2.0)
            for payload in (b"first-race", b"second-race"):
                client.sendto(payload, (endpoint.host, endpoint.port))
                reply, peer = client.recvfrom(1024)
                self.assertEqual(reply, payload)
                self.assertEqual(peer, (endpoint.host, endpoint.port))
        self.assertEqual(received, [b"first-race", b"second-race"])


if __name__ == "__main__":
    unittest.main()
