import gzip
import http.client
import json
import threading

import pytest

from RemoteWebControl.api import build_router
from RemoteWebControl.config import Config
from RemoteWebControl.errors import ApiError
from RemoteWebControl.main_thread import MainThreadRunner
from RemoteWebControl.server import ApiHttpServer, Response, Router

TOKEN = "secret-token"
ORIGIN = "https://pwa.local"


class FakeService:
    def info(self):
        return {"plugin": "RemoteWebControl", "plugin_version": "0.1.0"}

    def list_printers(self):
        return [{"id": "p1", "name": "Printer " + "x" * 2000}]  # Long enough to be gzipped.

    def get_profiles(self, printer_id):
        if printer_id != "p1":
            raise ApiError(404, "printer_not_found", "Printer '{0}' does not exist.".format(printer_id))
        return {"printer_id": printer_id}


@pytest.fixture
def server():
    config = Config("127.0.0.1", 0, TOKEN, (ORIGIN,), 1, 7)
    runner = MainThreadRunner(lambda f: f(), main_thread = threading.main_thread())
    router = build_router(FakeService(), runner, jobs = None)

    def crash(request):
        raise RuntimeError("boom")

    def echo(request):
        return Response.json({"received": request.json(), "q": request.query_value("q")})

    router.add("GET", "/api/crash", crash)
    router.add("POST", "/api/echo", echo)
    logs = []
    srv = ApiHttpServer(router, lambda: config, lambda level, msg: logs.append((level, msg)))
    srv.start()
    srv.logs = logs
    yield srv
    srv.stop()


def request(server, method, path, body = None, headers = None, token = TOKEN):
    host, port = server.server_address
    conn = http.client.HTTPConnection(host, port, timeout = 5)
    all_headers = {"Authorization": "Bearer " + token} if token else {}
    all_headers.update(headers or {})
    conn.request(method, path, body = body, headers = all_headers)
    response = conn.getresponse()
    data = response.read()
    conn.close()
    return response, data


def test_health(server):
    response, data = request(server, "GET", "/api/health")
    assert response.status == 200
    assert json.loads(data) == {"plugin": "RemoteWebControl", "plugin_version": "0.1.0", "status": "ok"}


@pytest.mark.parametrize("token", [None, "wrong", ""])
def test_auth_required(server, token):
    response, data = request(server, "GET", "/api/health", token = token)
    assert response.status == 401
    assert response.getheader("WWW-Authenticate") == "Bearer"
    assert json.loads(data)["error"]["code"] == "unauthorized"


def test_auth_scheme_is_case_insensitive(server):
    response, _ = request(server, "GET", "/api/health", token = None, headers = {"Authorization": "bearer " + TOKEN})
    assert response.status == 200


def test_not_found_and_method_not_allowed(server):
    response, data = request(server, "GET", "/api/nope")
    assert (response.status, json.loads(data)["error"]["code"]) == (404, "not_found")
    response, data = request(server, "DELETE", "/api/printers")
    assert (response.status, json.loads(data)["error"]["code"]) == (405, "method_not_allowed")


def test_path_params_and_service_errors(server):
    response, data = request(server, "GET", "/api/printers/p1/profiles")
    assert (response.status, json.loads(data)) == (200, {"printer_id": "p1"})
    response, data = request(server, "GET", "/api/printers/some%20printer/profiles")
    assert response.status == 404
    assert json.loads(data)["error"] == {"code": "printer_not_found", "message": "Printer 'some printer' does not exist."}


def test_gzip_only_when_accepted(server):
    response, data = request(server, "GET", "/api/printers", headers = {"Accept-Encoding": "gzip, deflate"})
    assert response.getheader("Content-Encoding") == "gzip"
    assert json.loads(gzip.decompress(data))[0]["id"] == "p1"
    response, data = request(server, "GET", "/api/printers")
    assert response.getheader("Content-Encoding") is None
    assert json.loads(data)[0]["id"] == "p1"
    response, _ = request(server, "GET", "/api/printers", headers = {"Accept-Encoding": "gzip;q=0"})
    assert response.getheader("Content-Encoding") is None


def test_cors(server):
    response, data = request(server, "OPTIONS", "/api/printers", token = None,
                             headers = {"Origin": ORIGIN, "Access-Control-Request-Method": "GET"})
    assert response.status == 204
    assert response.getheader("Access-Control-Allow-Origin") == ORIGIN
    assert "Authorization" in response.getheader("Access-Control-Allow-Headers")

    response, _ = request(server, "GET", "/api/health", headers = {"Origin": ORIGIN})
    assert response.getheader("Access-Control-Allow-Origin") == ORIGIN

    response, _ = request(server, "GET", "/api/health", headers = {"Origin": "https://evil.example"})
    assert response.getheader("Access-Control-Allow-Origin") is None

    # Errors must carry CORS headers too, or the browser hides them from the PWA.
    response, _ = request(server, "GET", "/api/health", token = "wrong", headers = {"Origin": ORIGIN})
    assert response.status == 401
    assert response.getheader("Access-Control-Allow-Origin") == ORIGIN


def test_json_body_and_query(server):
    response, data = request(server, "POST", "/api/echo?q=1", body = json.dumps({"a": 1}),
                             headers = {"Content-Type": "application/json"})
    assert json.loads(data) == {"received": {"a": 1}, "q": "1"}
    response, data = request(server, "POST", "/api/echo", body = "{not json")
    assert (response.status, json.loads(data)["error"]["code"]) == (400, "invalid_json")


def test_payload_too_large(server):
    # Only announce the size: the server must reject it from the header, without reading the body.
    host, port = server.server_address
    conn = http.client.HTTPConnection(host, port, timeout = 5)
    conn.putrequest("POST", "/api/echo")
    conn.putheader("Authorization", "Bearer " + TOKEN)
    conn.putheader("Content-Length", str(1024 * 1024 + 1))
    conn.endheaders()
    response = conn.getresponse()
    data = response.read()
    conn.close()
    assert (response.status, json.loads(data)["error"]["code"]) == (413, "payload_too_large")


def test_unhandled_exception_is_500_and_logged(server):
    response, data = request(server, "GET", "/api/crash")
    assert (response.status, json.loads(data)["error"]["code"]) == (500, "internal_error")
    assert any(level == "e" and "RuntimeError: boom" in msg for level, msg in server.logs)


def test_router_unit():
    router = Router()
    router.add("GET", "/api/jobs/{job_id}/mesh", lambda r: Response())
    _, params, public = router.resolve("GET", "/api/jobs/abc-123/mesh")
    assert public is False
    assert params == {"job_id": "abc-123"}
    with pytest.raises(ApiError):
        router.resolve("GET", "/api/jobs/a/b/mesh")


def test_second_server_cannot_take_the_same_port(server):
    host, port = server.server_address
    config = Config(host, port, TOKEN, (), 1, 7)
    other = ApiHttpServer(Router(), lambda: config, lambda *a: None)
    with pytest.raises(OSError):
        other.start()
    assert not other.is_running
    response, _ = request(server, "GET", "/api/health")  # The first one keeps working.
    assert response.status == 200


def test_stop_is_idempotent(server):
    server.stop()
    server.stop()
    assert not server.is_running
