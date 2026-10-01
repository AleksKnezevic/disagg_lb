#!/usr/bin/env python3
"""Forward one host TCP port to an IRD; run on the bare-metal host."""

import argparse
import logging
import selectors
import socket
import socketserver


def endpoint(value):
    try:
        host, port = value.rsplit(":", 1)
        port = int(port)
        if not host or not 1 <= port <= 65535:
            raise ValueError()
        return host, port
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected HOST:PORT, port 1..65535") from exc


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            with socket.create_connection(self.server.target, timeout=10) as upstream:
                upstream.settimeout(None)
                with selectors.DefaultSelector() as selector:
                    selector.register(self.request, selectors.EVENT_READ, upstream)
                    selector.register(upstream, selectors.EVENT_READ, self.request)
                    while selector.get_map():
                        for key, _ in selector.select():
                            data = key.fileobj.recv(65536)
                            if data:
                                key.data.sendall(data)
                            else:
                                selector.unregister(key.fileobj)
                                key.data.shutdown(socket.SHUT_WR)
        except OSError as exc:
            logging.warning("connection %s: %s", self.client_address, exc)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, listen, target):
        self.target = target
        super().__init__(listen, Handler)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--listen", required=True, type=endpoint)
    p.add_argument("--target", required=True, type=endpoint)
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    with Server(args.listen, args.target) as server:
        logging.info("forwarding %s:%s -> %s:%s", *args.listen, *args.target)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
