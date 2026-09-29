"""Minimal HTTP layer: routing, auth, CORS, errors and gzip. Pure Python (stdlib only).

Runs http.server.ThreadingHTTPServer in a daemon thread. Handlers never touch Cura directly:
anything that needs the scene, the settings stacks or Qt goes through MainThreadRunner.
"""

import gzip
import hmac
import json
import os
import re
import shutil
import socket
import sys
import threading
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Tuple

from .config import Config
from .errors import ApiError

LogFunction = Callable[[str, str], None]  # (level, message) with levels "d", "i", "w", "e".

GZIP_MIN_BYTES = 1024
ALLOWED_METHODS = "GET, POST, PUT, PATCH, DELETE, OPTIONS"
ALLOWED_HEADERS = "Authorization, Content-Type"


class Request:
    def __init__(self, method: str, path: str, query: Dict[str, List[str]], headers: Any, body: bytes,
                 client_ip: str = "", server_ip: str = "") -> None:
        self.method = method
        self.client_ip = client_ip
        self.server_ip = server_ip  # Local address of this connection.
        self.path = path
        self.query = query
        self.headers = headers  # email.message.Message: case-insensitive get().
        self.body = body
        self.path_params = {}  # type: Dict[str, str]

    def query_value(self, name: str, default: Optional[str] = None) -> Optional[str]:
        values = self.query.get(name)
        return values[-1] if values else default

    def json(self) -> Any:
        if not self.body:
            raise ApiError(400, "invalid_json", "Request body is empty; a JSON document was expected.")
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as e:
            raise ApiError(400, "invalid_json", "Request body is not valid JSON: {0}".format(e))


class Response:
    def __init__(self, status: int = 200, body: bytes = b"", content_type: str = "application/json",
                 headers: Optional[Dict[str, str]] = None, compressible: bool = False,
                 file_path: Optional[str] = None) -> None:
        self.status = status
        self.body = body
        self.content_type = content_type
        self.headers = headers or {}
        self.compressible = compressible
        self.file_path = file_path  # When set, the body is streamed from this file instead.

    @classmethod
    def json(cls, data: Any, status: int = 200) -> "Response":
        body = json.dumps(data, ensure_ascii = False, allow_nan = False).encode("utf-8")
        return cls(status, body, "application/json; charset=utf-8", compressible = True)

    @classmethod
    def error(cls, error: ApiError) -> "Response":
        return cls.json(error.to_dict(), error.status)


Handler = Callable[[Request], Response]


class Router:
    """Routes like "/api/jobs/{id}/mesh". Parameters match a single path segment."""

    def __init__(self) -> None:
        self._routes = []  # type: List[Tuple[str, re.Pattern, Handler, bool]]

    def add(self, method: str, pattern: str, handler: Handler, public: bool = False) -> None:
        """public routes need no token (pairing); every other route needs the bearer token."""
        regex = "^" + re.sub(r"\\\{(\w+)\\\}", r"(?P<\1>[^/]+)", re.escape(pattern)) + "$"
        self._routes.append((method.upper(), re.compile(regex), handler, public))

    def resolve(self, method: str, path: str) -> Tuple[Handler, Dict[str, str], bool]:
        path_matched = False
        for route_method, regex, handler, public in self._routes:
            match = regex.match(path)
            if not match:
                continue
            path_matched = True
            if route_method == method:
                return handler, {k: urllib.parse.unquote(v) for k, v in match.groupdict().items()}, public
        if path_matched:
            raise ApiError(405, "method_not_allowed", "Method {0} is not allowed on {1}.".format(method, path))
        raise ApiError(404, "not_found", "No route for {0}.".format(path))


class _ExclusiveHTTPServer(ThreadingHTTPServer):
    """On Windows, SO_REUSEADDR (which http.server enables) lets a SECOND process bind the same
    port without any error, e.g. a second Cura instance. Use SO_EXCLUSIVEADDRUSE there instead."""

    allow_reuse_address = sys.platform != "win32"
    daemon_threads = True

    def server_bind(self) -> None:
        if sys.platform == "win32" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class ApiHttpServer:
    def __init__(self, router: Router, get_config: Callable[[], Config], log: LogFunction,
                 static: Optional[Any] = None) -> None:
        self._router = router
        self._static = static  # StaticFiles serving the web app outside /api, or None.
        self._get_config = get_config
        self._log = log
        self._httpd = None  # type: Optional[ThreadingHTTPServer]
        self._thread = None  # type: Optional[threading.Thread]

    @property
    def is_running(self) -> bool:
        return self._httpd is not None

    @property
    def server_address(self) -> Tuple[str, int]:
        if self._httpd is None:
            raise RuntimeError("Server is not running")
        return self._httpd.server_address[:2]

    def start(self) -> None:
        if self._httpd is not None:
            return
        config = self._get_config()
        handler_class = _make_handler_class(self._router, self._get_config, self._log, self._static)
        httpd = _ExclusiveHTTPServer((config.bind_address, config.port), handler_class)
        self._httpd = httpd
        self._thread = threading.Thread(target = httpd.serve_forever, name = "RemoteWebControlHttp", daemon = True)
        self._thread.start()

    def stop(self) -> None:
        httpd, self._httpd = self._httpd, None
        if httpd is None:
            return
        httpd.shutdown()  # Blocks until serve_forever() returns.
        httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout = 5)
            self._thread = None


def _make_handler_class(router: Router, get_config: Callable[[], Config], log: LogFunction,
                        static: Optional[Any] = None) -> type:

    class RequestHandler(BaseHTTPRequestHandler):
        server_version = "RemoteWebControl"

        def do_GET(self) -> None: self._handle()
        def do_HEAD(self) -> None: self._handle()
        def do_POST(self) -> None: self._handle()
        def do_PUT(self) -> None: self._handle()
        def do_PATCH(self) -> None: self._handle()
        def do_DELETE(self) -> None: self._handle()
        def do_OPTIONS(self) -> None: self._handle()

        def log_message(self, format: str, *args: Any) -> None:
            log("d", "[RemoteWebControl] {0} - {1}".format(self.address_string(), format % args))

        def _handle(self) -> None:
            config = get_config()
            origin = self.headers.get("Origin", "")
            cors_headers = {}  # type: Dict[str, str]
            if config.is_origin_allowed(origin):
                cors_headers = {
                    "Access-Control-Allow-Origin": origin,
                    "Access-Control-Expose-Headers": "Content-Disposition, Content-Length",
                    "Vary": "Origin",
                }

            if self.command == "OPTIONS":  # CORS preflight: never authenticated by browsers.
                headers = dict(cors_headers)
                if cors_headers:
                    headers["Access-Control-Allow-Methods"] = ALLOWED_METHODS
                    headers["Access-Control-Allow-Headers"] = ALLOWED_HEADERS
                    headers["Access-Control-Max-Age"] = "600"
                self._send(Response(204, b"", "", headers), cors_headers = {})
                return

            try:
                parsed = urllib.parse.urlsplit(self.path)
                if not (parsed.path == "/api" or parsed.path.startswith("/api/")):
                    response = self._static_response(parsed.path)
                else:
                    method = "GET" if self.command == "HEAD" else self.command
                    handler, params, public = router.resolve(method, parsed.path)
                    if not public:
                        self._check_auth(config)
                    body = self._read_body(config)
                    request = Request(self.command, parsed.path, urllib.parse.parse_qs(parsed.query), self.headers, body,
                                      client_ip = self.client_address[0], server_ip = self.connection.getsockname()[0])
                    request.path_params = params
                    response = handler(request)
            except ApiError as e:
                response = Response.error(e)
                if e.status == 401:
                    response.headers["WWW-Authenticate"] = "Bearer"
                if e.status == 413:
                    self.close_connection = True  # The body was not read.
            except Exception:
                log("e", "[RemoteWebControl] Unhandled error in {0} {1}:\n{2}".format(self.command, self.path, traceback.format_exc()))
                response = Response.error(ApiError(500, "internal_error", "Internal error; see cura.log for details."))
            self._send(response, cors_headers)

        def _static_response(self, path: str) -> Response:
            if static is None or self.command not in ("GET", "HEAD"):
                raise ApiError(404, "not_found", "No route for {0}.".format(path))
            return static.response(path)

        def _check_auth(self, config: Config) -> None:
            if not bearer_token_matches(self.headers.get("Authorization", ""), config.token):
                raise ApiError(401, "unauthorized", "Missing or invalid bearer token.")

        def _read_body(self, config: Config) -> bytes:
            length_header = self.headers.get("Content-Length")
            if not length_header:
                return b""
            try:
                length = int(length_header)
            except ValueError:
                raise ApiError(400, "bad_request", "Invalid Content-Length header.")
            if length < 0:
                raise ApiError(400, "bad_request", "Invalid Content-Length header.")
            if length > config.max_upload_bytes:
                raise ApiError(413, "payload_too_large",
                               "Request body exceeds the limit of {0} MB.".format(config.max_upload_mb))
            return self.rfile.read(length)

        def _send(self, response: Response, cors_headers: Dict[str, str]) -> None:
            if response.file_path is not None:
                self._send_file(response, cors_headers)
                return
            body = response.body
            headers = dict(cors_headers)
            headers.update(response.headers)
            if response.compressible:
                headers["Vary"] = ", ".join(filter(None, [headers.get("Vary"), "Accept-Encoding"]))
                if len(body) >= GZIP_MIN_BYTES and _accepts_gzip(self.headers.get("Accept-Encoding", "")):
                    body = gzip.compress(body, compresslevel = 6)
                    headers["Content-Encoding"] = "gzip"
            try:
                self.send_response(response.status)
                if response.content_type:
                    self.send_header("Content-Type", response.content_type)
                self.send_header("Content-Length", str(len(body)))
                for name, value in headers.items():
                    self.send_header(name, value)
                self.end_headers()
                if self.command != "HEAD" and body:
                    self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                log("d", "[RemoteWebControl] Client closed the connection before the response was sent.")

        def _send_file(self, response: Response, cors_headers: Dict[str, str]) -> None:
            try:
                stream = open(response.file_path, "rb")
            except OSError:
                self._send(Response.error(ApiError(404, "not_found", "The file is no longer available.")), cors_headers)
                return
            with stream:
                size = os.fstat(stream.fileno()).st_size
                try:
                    self.send_response(response.status)
                    self.send_header("Content-Type", response.content_type)
                    self.send_header("Content-Length", str(size))
                    for name, value in dict(cors_headers, **response.headers).items():
                        self.send_header(name, value)
                    self.end_headers()
                    if self.command != "HEAD":
                        shutil.copyfileobj(stream, self.wfile, 256 * 1024)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    log("d", "[RemoteWebControl] Client closed the connection during a download.")

    return RequestHandler


def content_disposition(filename: str) -> str:
    """attachment header with an ASCII fallback and the UTF-8 name (RFC 6266 / RFC 5987)."""
    ascii_name = filename.encode("ascii", "replace").decode("ascii").replace("?", "_").replace('"', "_").replace("\\", "_")
    return "attachment; filename=\"{0}\"; filename*=UTF-8''{1}".format(ascii_name, urllib.parse.quote(filename, safe = ""))


def bearer_token_matches(authorization: str, token: str) -> bool:
    scheme, _, given = (authorization or "").partition(" ")
    return bool(token) and scheme.lower() == "bearer" and \
        hmac.compare_digest(given.strip().encode("utf-8"), token.encode("utf-8"))


def has_bearer_token(request: Request, token: str) -> bool:
    return bearer_token_matches(request.headers.get("Authorization", "") if request.headers else "", token)


def is_local_client(request: Request) -> bool:
    """True for requests made on the PC running Cura: loopback, or the client uses the same
    address as the server end of the connection (e.g. when bind_address is a LAN IP)."""
    loopback = ("127.0.0.1", "::1", "::ffff:127.0.0.1")
    return request.client_ip in loopback or (bool(request.client_ip) and request.client_ip == request.server_ip)


def accepts_gzip(accept_encoding: str) -> bool:
    return _accepts_gzip(accept_encoding)


def _accepts_gzip(accept_encoding: str) -> bool:
    for part in accept_encoding.split(","):
        name, _, params = part.strip().partition(";")
        if name.strip().lower() in ("gzip", "*"):
            q = params.strip()
            return not (q.startswith("q=") and q[2:].strip() in ("0", "0.0", "0.00", "0.000"))
    return False
