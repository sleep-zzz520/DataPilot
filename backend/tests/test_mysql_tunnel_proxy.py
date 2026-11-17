"""Real loopback TCP checks; unittest also runs without backend dependencies."""
import importlib.util
from pathlib import Path
import socket
import socketserver
import threading
import unittest
from contextlib import contextmanager

spec = importlib.util.spec_from_file_location(
    "mysql_tunnel_proxy", Path(__file__).resolve().parents[2] / "scripts/mysql_tunnel_proxy.py")
proxy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proxy)


class Echo(socketserver.BaseRequestHandler):
    def handle(self):
        while True:
            data = self.request.recv(4096)
            if not data:
                self.request.sendall(b"EOF")
                return
            self.request.sendall(data)


class EchoServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True


@contextmanager
def running(server):
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def receive(connection, count):
    result = b""
    while len(result) < count:
        part = connection.recv(count - len(result))
        if not part:
            break
        result += part
    return result


class TunnelTests(unittest.TestCase):
    def bridge(self, echo, **kwargs):
        return proxy.TunnelServer("127.0.0.1", 0, "127.0.0.0/8",
                                  target_port=echo.server_address[1], **kwargs)

    def connect(self, server):
        return socket.create_connection(server.server_address, timeout=2)

    def test_forward_and_half_close(self):
        with running(EchoServer(("127.0.0.1", 0), Echo)) as echo, running(self.bridge(echo)) as bridge:
            with self.connect(bridge) as client:
                payload = bytes(range(256)) * 1024
                client.sendall(payload)
                self.assertEqual(receive(client, len(payload)), payload)
                client.shutdown(socket.SHUT_WR)
                self.assertEqual(receive(client, 3), b"EOF")
                self.assertEqual(client.recv(1), b"")

    def test_reject_source(self):
        with running(EchoServer(("127.0.0.1", 0), Echo)) as echo:
            # Bind must remain inside CIDR; 127.0.0.2 is outside this exact host allowlist.
            server = proxy.TunnelServer("127.0.0.1", 0, "127.0.0.1/32", target_port=echo.server_address[1])
            self.assertFalse(server.verify_request(None, ("192.168.50.2", 23456)))
            self.assertTrue(server.verify_request(None, ("127.0.0.1", 23456)))
            with running(server) as bridge, socket.socket() as client:
                try:
                    client.bind(("127.0.0.2", 0))
                except OSError:
                    self.skipTest("OS does not expose alternate loopback address")
                client.settimeout(2)
                client.connect(bridge.server_address)
                self.assertEqual(client.recv(1), b"")

    def test_source_filter(self):
        with proxy.TunnelServer("127.0.0.1", 0, "127.0.0.1/32") as server:
            self.assertFalse(server.verify_request(None, ("192.168.50.2", 23456)))
            self.assertTrue(server.verify_request(None, ("127.0.0.1", 23456)))

    def test_configuration_rejected(self):
        for bind, network, target in [
            ("0.0.0.0", "0.0.0.0/0", "127.0.0.1"),
            ("8.8.8.8", "8.8.8.0/24", "127.0.0.1"),
            ("127.0.0.1", "172.16.0.0/16", "127.0.0.1"),
            ("127.0.0.1", "127.0.0.0/8", "192.168.1.1"),
            ("::1", "::1/128", "127.0.0.1"),
        ]:
            with self.subTest(bind=bind, network=network), self.assertRaises(ValueError):
                proxy.validate_config(bind, network, target)

    def test_capacity_rejects_and_slot_recovers(self):
        with running(EchoServer(("127.0.0.1", 0), Echo)) as echo, running(self.bridge(echo, max_connections=1)) as bridge:
            with self.connect(bridge) as first:
                first.sendall(b"ready")
                self.assertEqual(receive(first, 5), b"ready")
                with self.connect(bridge) as second:
                    self.assertEqual(second.recv(1), b"")
                first.shutdown(socket.SHUT_WR)
                self.assertEqual(receive(first, 3), b"EOF")
                self.assertEqual(first.recv(1), b"")
            # Acquiring the semaphore confirms handler cleanup before opening the next client.
            self.assertTrue(bridge.slots.acquire(timeout=2))
            bridge.slots.release()
            with self.connect(bridge) as third:
                third.sendall(b"again")
                self.assertEqual(receive(third, 5), b"again")

    def test_idle_timeout(self):
        with running(EchoServer(("127.0.0.1", 0), Echo)) as echo, running(self.bridge(echo, idle_timeout=0.05)) as bridge:
            with self.connect(bridge) as client:
                self.assertEqual(client.recv(1), b"")


if __name__ == "__main__":
    unittest.main()
