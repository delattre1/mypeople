#!/usr/bin/env python3
"""Tiny port-shift proxy used by Verify J6b."""
import http.client
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LISTEN = int(sys.argv[1])
TARGET = int(sys.argv[2])


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _proxy(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0") or 0))
        headers = {k: v for k, v in self.headers.items()
                   if k.lower() not in ("host", "connection", "content-length")}
        conn = http.client.HTTPConnection("127.0.0.1", TARGET, timeout=10)
        conn.request(self.command, self.path, body=body or None, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        self.send_response(resp.status)
        for key, value in resp.getheaders():
            if key.lower() not in ("connection", "transfer-encoding", "content-length"):
                self.send_header(key, value)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = _proxy
    do_HEAD = _proxy
    do_POST = _proxy


ThreadingHTTPServer(("127.0.0.1", LISTEN), Handler).serve_forever()
