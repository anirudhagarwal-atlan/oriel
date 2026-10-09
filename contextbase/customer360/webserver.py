"""Loopback-only HTTP surface for one trusted local operator.

The startup session exists only in memory. Exact Host/Origin checks and a
SameSite/HttpOnly cookie plus a CSRF header isolate browser requests from other
origins. This is not a hosted or multi-user authentication implementation.
"""
from __future__ import annotations

import hmac
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import secrets
from urllib.parse import parse_qs, urlsplit

from ..model import strict_json_loads
from .application import Application
from .contracts import canonical

ASSETS = {"/": ("index.html", "text/html; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/style.css": ("style.css", "text/css; charset=utf-8")}
MAX_BODY = 1_000_000
MAX_BROWSER_INTEGER = (1 << 53) - 1


def browser_values(value):
    """Preserve integer digits at the JSON/JavaScript boundary.

    Stored artifacts and their approval hashes keep the original typed values.
    Browser previews represent integers outside JavaScript's exact range as
    decimal strings, as metric amounts already do. Never convert via float.
    """
    if type(value) is int and not -MAX_BROWSER_INTEGER <= value <= MAX_BROWSER_INTEGER:
        return str(value)
    if isinstance(value, dict):
        return {key: browser_values(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [browser_values(item) for item in value]
    return value


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, application, port=0):
        if type(port) is not int or not 0 <= port <= 65535:
            raise ValueError("Select a local port between 0 and 65535.")
        self.application = application
        self.session_key = secrets.token_urlsafe(32)
        self.csrf_key = secrets.token_urlsafe(32)
        super().__init__(("127.0.0.1", port), Handler)
        self.origin = "http://127.0.0.1:" + str(self.server_port)


class Handler(BaseHTTPRequestHandler):
    server_version = "OrielLocal"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, format, *args):
        # URLs, headers and source/model content must never become access logs.
        pass

    def _headers(self, status, kind, size, *, cookie=False):
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if kind.startswith("application/json"):
            self.send_header("X-Oriel-Integer-Encoding", "unsafe-integers-as-decimal-strings")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
        if cookie:
            self.send_header("Set-Cookie", "OrielSession=" + self.server.session_key + "; HttpOnly; SameSite=Strict; Path=/")
        self.end_headers()

    def _json(self, status, value):
        raw = canonical(browser_values(value)).encode()
        self._headers(status, "application/json; charset=utf-8", len(raw))
        self.wfile.write(raw)

    def _error(self, status, code, message):
        self._json(status, {"error": {"code": code, "message": message}})

    def _origin_ok(self, *, mutation=False):
        if len(self.headers.get_all("Host", [])) != 1 or self.headers.get("Host") != urlsplit(self.server.origin).netloc:
            return False
        origins = self.headers.get_all("Origin", [])
        if len(origins) > 1 or (origins and origins[0] != self.server.origin):
            return False
        if self.headers.get("Sec-Fetch-Site") not in (None, "none", "same-origin"):
            return False
        return not mutation or origins == [self.server.origin]

    def _authenticated(self):
        raw = self.headers.get("Cookie", "")
        if len(raw) > 4096 or len(self.headers.get_all("Cookie", [])) != 1:
            return False
        try:
            cookie = SimpleCookie(raw)
            value = cookie.get("OrielSession")
            return value is not None and hmac.compare_digest(value.value, self.server.session_key)
        except Exception:
            return False

    def do_GET(self):
        if not self._origin_ok():
            return self._error(403, "origin_refused", "Open this application directly at its numeric loopback address.")
        parsed = urlsplit(self.path)
        if parsed.scheme or parsed.netloc or parsed.fragment or len(self.path) > 2048:
            return self._error(400, "invalid_path", "Use a declared local application route.")
        if parsed.path in ASSETS and not parsed.query:
            if parsed.path != "/" and not self._authenticated():
                return self._error(401, "session_required", "Open the application home page first.")
            filename, kind = ASSETS[parsed.path]
            path = Path(__file__).with_name("web") / filename
            if not path.is_file():
                return self._error(503, "assets_unavailable", "The application assets are unavailable in this installation.")
            raw = path.read_bytes()
            self._headers(200, kind, len(raw), cookie=parsed.path == "/")
            return self.wfile.write(raw)
        if not self._authenticated():
            return self._error(401, "session_required", "Open the application home page first.")
        try:
            app = self.server.application
            if parsed.path == "/api/session" and not parsed.query:
                return self._json(200, {"csrf": self.server.csrf_key, "model_origin": app.model_origin})
            if parsed.path == "/api/state" and not parsed.query:
                return self._json(200, app.state())
            if parsed.path == "/api/catalog" and not parsed.query:
                return self._json(200, app.catalog())
            if parsed.path == "/api/connector":
                query = parse_qs(parsed.query, strict_parsing=True)
                if set(query) != {"type"} or len(query["type"]) != 1:
                    raise ValueError("Select exactly one connector type.")
                return self._json(200, app.connector(query["type"][0]))
            if parsed.path == "/api/source-plan":
                try:
                    query = parse_qs(parsed.query, strict_parsing=True, keep_blank_values=True,
                                     encoding="utf-8", errors="strict", max_num_fields=257)
                except ValueError:
                    raise ValueError("Use one connector type and bounded resource parameters.") from None
                if (set(query) - {"type", "resource"} or len(query.get("type", [])) != 1
                        or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", query["type"][0])):
                    raise ValueError("Use one connector type and bounded resource parameters.")
                return self._json(200, app.source_plan(query["type"][0], query.get("resource", [])))
            if parsed.path.startswith("/api/jobs/") and not parsed.query:
                return self._json(200, app.job(parsed.path[len("/api/jobs/"):]))
            self._error(404, "unknown_route", "Unknown local application route.")
        except ValueError as error:
            self._error(400, "request_refused", str(error)[:1000])
        except Exception:
            self._error(500, "application_error", "Application state is unavailable. Inspect the local workspace before retrying.")

    def do_POST(self):
        if not self._origin_ok(mutation=True) or not self._authenticated():
            return self._error(403, "origin_or_session_refused", "Use this application's current local session.")
        csrf = self.headers.get_all("X-Oriel-CSRF", [])
        if len(csrf) != 1 or not csrf[0].isascii() or not hmac.compare_digest(csrf[0], self.server.csrf_key):
            return self._error(403, "csrf_refused", "Reload the application before submitting another operation.")
        if self.path != "/api/jobs":
            return self._error(404, "unknown_route", "Unknown local application route.")
        if (self.headers.get("Content-Type") != "application/json" or self.headers.get("Transfer-Encoding") is not None
                or len(self.headers.get_all("Content-Length", [])) != 1):
            return self._error(400, "invalid_body", "Send one bounded JSON body with its explicit byte length.")
        try:
            length = int(self.headers["Content-Length"])
            if not 0 < length <= MAX_BODY:
                raise ValueError()
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError()
            request = strict_json_loads(raw.decode("utf-8"))
            if not isinstance(request, dict) or set(request) - {"operation", "arguments", "request_id"} or not {"operation", "arguments"} <= set(request):
                raise ValueError()
        except Exception:
            return self._error(400, "invalid_body", "Supply a bounded JSON object with unique keys and finite values.")
        try:
            value = self.server.application.submit(request["operation"], request["arguments"], request.get("request_id"))
            return self._json(202, value)
        except ValueError as error:
            self._error(400, "request_refused", str(error)[:1000])
        except RuntimeError as error:
            self._error(409, "application_busy", str(error)[:1000])
        except Exception:
            self._error(500, "application_error", "The operation could not be queued. Inspect local state before trying again.")


def serve(workspace, *, port=8787):
    application = Application(workspace)
    server = None
    try:
        server = Server(application, port=port)
        print("Oriel Customer 360: " + server.origin, flush=True)
        print("Local operator workspace. Open this address directly; model calls require consent in the app.", flush=True)
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        if server is not None:
            server.server_close()
        application.close()
    return {"status": "stopped"}
