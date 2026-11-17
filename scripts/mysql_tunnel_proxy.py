#!/usr/bin/env python3
"""Bounded, source-filtered TCP bridge to a local SSH reverse tunnel (Python 3.9+)."""
import argparse
import ipaddress
import logging
import select
import socket
import socketserver
import threading
import time

LOG = logging.getLogger("mysql_tunnel_proxy")


def validate_config(bind, allow_network, target_host):
    address = ipaddress.IPv4Address(bind)
    network = ipaddress.IPv4Network(allow_network, strict=True)
    # is_private also includes reserved ranges; accept only RFC1918 or loopback.
    private = any(address in ipaddress.IPv4Network(cidr) for cidr in (
        "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8"))
    if not private or address not in network:
        raise ValueError("bind must be an RFC1918 or loopback IPv4 within allow-network")
    if ipaddress.IPv4Address(target_host) != ipaddress.IPv4Address("127.0.0.1"):
        raise ValueError("target must be 127.0.0.1")
    return network


def relay(client, upstream, idle_timeout):
    sockets = (client, upstream)
    buffers = {client: bytearray(), upstream: bytearray()}
    readable = set(sockets)
    write_closed = set()
    peer = {client: upstream, upstream: client}
    for connection in sockets:
        connection.setblocking(False)
    last_activity = time.monotonic()
    while True:
        for connection in sockets:
            if peer[connection] not in readable and not buffers[connection] and connection not in write_closed:
                connection.shutdown(socket.SHUT_WR)
                write_closed.add(connection)
        if not readable and not any(buffers.values()):
            return
        remaining = idle_timeout - (time.monotonic() - last_activity)
        if remaining <= 0:
            raise TimeoutError("idle timeout")
        # ponytail: bounded buffers and threads suit this small bridge; use an event loop for larger fleets.
        readers = [s for s in readable if len(buffers[peer[s]]) < 65536]
        writers = [s for s in sockets if buffers[s]]
        ready_read, ready_write, _ = select.select(readers, writers, [], remaining)
        for connection in ready_read:
            try:
                data = connection.recv(65536 - len(buffers[peer[connection]]))
            except BlockingIOError:
                continue
            if data:
                buffers[peer[connection]].extend(data)
                last_activity = time.monotonic()
            else:
                readable.remove(connection)
        for connection in ready_write:
            try:
                sent = connection.send(buffers[connection])
            except BlockingIOError:
                continue
            if sent == 0:
                raise ConnectionError("closed while writing")
            del buffers[connection][:sent]
            last_activity = time.monotonic()


class TunnelHandler(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            with socket.create_connection(self.server.target, timeout=5) as upstream:
                relay(self.request, upstream, self.server.idle_timeout)
        except OSError as exc:
            # Exception messages may include addresses; log only error type and source metadata.
            LOG.warning("connection ended source=%s error=%s", self.client_address[0], type(exc).__name__)


class TunnelServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, bind, port, allow_network, target_host="127.0.0.1", target_port=13306,
                 max_connections=32, idle_timeout=300):
        self.allowed_network = validate_config(bind, allow_network, target_host)
        if not 0 <= port <= 65535 or not 1 <= target_port <= 65535:
            raise ValueError("invalid port")
        if max_connections < 1 or idle_timeout <= 0:
            raise ValueError("connection limit and idle timeout must be positive")
        self.target = (target_host, target_port)
        self.idle_timeout = idle_timeout
        self.slots = threading.BoundedSemaphore(max_connections)
        super().__init__((bind, port), TunnelHandler)

    def verify_request(self, request, client_address):
        allowed = ipaddress.IPv4Address(client_address[0]) in self.allowed_network
        if not allowed:
            LOG.warning("source rejected source=%s", client_address[0])
        return allowed

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            LOG.warning("capacity rejected source=%s", client_address[0])
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind", required=True, help="explicit RFC1918 Docker gateway IPv4")
    parser.add_argument("--port", type=int, default=13307)
    parser.add_argument("--allow-network", required=True, help="explicit source IPv4 CIDR")
    parser.add_argument("--target-host", default="127.0.0.1")
    parser.add_argument("--target-port", type=int, default=13306)
    parser.add_argument("--max-connections", type=int, default=32)
    parser.add_argument("--idle-timeout", type=float, default=300)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        server = TunnelServer(**vars(args))
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    with server:
        try:
            server.serve_forever(poll_interval=0.2)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
