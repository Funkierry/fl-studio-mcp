"""
MCP Streamable HTTP transport (JSON responses, no server-initiated streams), standard library only.

Useful for clients that only speak HTTP, and for sharing one FL connection between several clients
(only one process can hold the loopMIDI reply port at a time).
"""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from . import __version__
from .server import log

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def run_http(server, host="127.0.0.1", port=47821, path="/mcp"):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _reply(self, status, body=None, ctype="application/json"):
            data = b"" if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            if body is not None:
                self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _origin_allowed(self):
            # DNS-rebinding protection: browsers send Origin; only accept local pages.
            origin = self.headers.get("Origin")
            return not origin or urlparse(origin).hostname in LOCAL_HOSTS

        def do_POST(self):
            if urlparse(self.path).path != path:
                return self._reply(404, {"error": "not found"})
            if not self._origin_allowed():
                return self._reply(403, {"error": "origin not allowed"})
            length = int(self.headers.get("Content-Length") or 0)
            try:
                msg = json.loads(self.rfile.read(length).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return self._reply(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
            resp = server.handle(msg)
            if resp is None:
                return self._reply(202)
            self._reply(200, resp)

        def do_GET(self):
            if urlparse(self.path).path == "/":
                return self._reply(200, {"name": "fl-studio-mcp", "version": __version__, "endpoint": path})
            self.send_response(405)
            self.send_header("Allow", "POST")
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_DELETE = do_GET

        def log_message(self, fmt, *args):
            log("http: " + fmt % args)

    httpd = ThreadingHTTPServer((host, port), Handler)
    log(f"fl-studio-mcp {__version__} listening on http://{host}:{port}{path}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
