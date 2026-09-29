"""Static web app serving, pairing with a PIN and public routes."""

import json
import threading

import pytest

from RemoteWebControl.api import build_router
from RemoteWebControl.config import Config
from RemoteWebControl.errors import ApiError
from RemoteWebControl.main_thread import MainThreadRunner
from RemoteWebControl.pairing import Pairing
from RemoteWebControl.server import ApiHttpServer, Request, is_local_client
from RemoteWebControl.static_files import StaticFiles
from test_api_jobs import TOKEN, call


class FakeService:
    def info(self):
        return {}


@pytest.fixture
def web_root(tmp_path):
    root = tmp_path / "web"
    (root / "js").mkdir(parents = True)
    (root / "index.html").write_text("<!doctype html><title>RemoteWebControl</title>", encoding = "utf-8")
    (root / "js" / "app.js").write_text("console.log('hi');", encoding = "utf-8")
    (root / ".secret").write_text("nope", encoding = "utf-8")
    (root / "notes.bin").write_bytes(b"\x00")
    (tmp_path / "outside.html").write_text("outside", encoding = "utf-8")
    return root


class Clock:
    now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def server(web_root):
    clock = Clock()
    pairing = Pairing(lambda: TOKEN, clock = clock, ttl = 60, max_attempts = 3)
    runner = MainThreadRunner(lambda f: f(), main_thread = threading.main_thread())
    router = build_router(FakeService(), runner, jobs = None, pairing = pairing,
                          app_urls = lambda: ["http://192.168.1.10:8765/"], get_token = lambda: TOKEN)
    config = Config("127.0.0.1", 0, TOKEN, (), 5, 7)
    srv = ApiHttpServer(router, lambda: config, lambda *a: None, static = StaticFiles(str(web_root)))
    srv.start()
    srv.clock = clock
    yield srv
    srv.stop()


def get_public(server, path, method = "GET", body = None):
    return call(server, method, path, body, headers = {"Authorization": ""})


def test_static_files_without_token(server):
    response, data = get_public(server, "/")
    assert response.status == 200
    assert response.getheader("Content-Type") == "text/html; charset=utf-8"
    assert "default-src 'self'" in response.getheader("Content-Security-Policy")
    assert response.getheader("Cache-Control") == "no-cache"
    assert b"RemoteWebControl" in data
    response, data = get_public(server, "/js/app.js")
    assert response.getheader("Content-Type").startswith("text/javascript")
    response, data = get_public(server, "/js/app.js", method = "HEAD")
    assert response.status == 200 and data == b"" and response.getheader("Content-Length") == "18"


@pytest.mark.parametrize("path", ["/.secret", "/notes.bin", "/../outside.html", "/%2e%2e/outside.html",
                                  "/js/..%2f..%2foutside.html", "/js%5c..%5c..%5coutside.html", "/missing.js"])
def test_static_files_are_confined(server, path):
    response, _ = get_public(server, path)
    assert response.status == 404


def test_static_only_for_get(server):
    response, _ = get_public(server, "/index.html", method = "POST", body = b"x")
    assert response.status == 404


def test_api_still_needs_token(server):
    response, _ = get_public(server, "/api/health")
    assert response.status == 401


def test_pairing_flow(server):
    response, data = get_public(server, "/api/pairing/start", method = "POST")
    assert response.status == 200, data
    started = json.loads(data)
    assert len(started["pin"]) == 6 and started["pin"].isdigit()
    assert started["urls"] == ["http://192.168.1.10:8765/"] and started["token"] == TOKEN

    response, data = get_public(server, "/api/pairing/claim", method = "POST", body = json.dumps({"pin": started["pin"]}))
    assert (response.status, json.loads(data)) == (200, {"token": TOKEN})


def test_pairing_rejects_wrong_expired_and_brute_force(server):
    pin = json.loads(get_public(server, "/api/pairing/start", method = "POST")[1])["pin"]
    wrong = "{0:06d}".format((int(pin) + 1) % 1000000)
    for expected in ("invalid_pin", "invalid_pin", "pin_blocked"):
        response, data = get_public(server, "/api/pairing/claim", method = "POST", body = json.dumps({"pin": wrong}))
        assert (response.status, json.loads(data)["error"]["code"]) == (403, expected)
    response, data = get_public(server, "/api/pairing/claim", method = "POST", body = json.dumps({"pin": pin}))
    assert json.loads(data)["error"]["code"] == "no_pairing"  # Blocked: the right PIN no longer works.

    pin = json.loads(get_public(server, "/api/pairing/start", method = "POST")[1])["pin"]
    server.clock.now += 61
    response, data = get_public(server, "/api/pairing/claim", method = "POST", body = json.dumps({"pin": pin}))
    assert json.loads(data)["error"]["code"] == "pin_expired"


def test_pairing_claim_validates_input(server):
    for body in (json.dumps({"pin": 123456}), json.dumps({"pin": "12a456"}), json.dumps([])):
        response, data = get_public(server, "/api/pairing/claim", method = "POST", body = body)
        assert response.status == 400


def test_is_local_client():
    assert is_local_client(Request("GET", "/", {}, {}, b"", client_ip = "127.0.0.1", server_ip = "127.0.0.1"))
    assert is_local_client(Request("GET", "/", {}, {}, b"", client_ip = "192.168.1.10", server_ip = "192.168.1.10"))
    assert not is_local_client(Request("GET", "/", {}, {}, b"", client_ip = "192.168.1.50", server_ip = "192.168.1.10"))


def test_pairing_start_is_local_only():
    pairing = Pairing(lambda: TOKEN)
    runner = MainThreadRunner(lambda f: f(), main_thread = threading.main_thread())
    router = build_router(FakeService(), runner, jobs = None, pairing = pairing)
    handler, _, public = router.resolve("POST", "/api/pairing/start")
    assert public
    with pytest.raises(ApiError) as info:
        handler(Request("POST", "/api/pairing/start", {}, {}, b"", client_ip = "192.168.1.50", server_ip = "192.168.1.10"))
    assert (info.value.status, info.value.code) == (403, "local_only")


def test_import_map_hash_in_csp(tmp_path):
    import base64
    import hashlib
    from RemoteWebControl.static_files import inline_script_hashes
    body = '\n{ "imports": { "three": "./vendor/three.module.min.js" } }\n'
    page = tmp_path / "index.html"
    page.write_text('<!doctype html><script type="importmap">' + body + '</script><script type="module" src="js/app.js"></script>',
                    encoding = "utf-8")
    expected = "'sha256-" + base64.b64encode(hashlib.sha256(body.encode()).digest()).decode() + "'"
    assert inline_script_hashes(page.read_text(encoding = "utf-8")) == [expected]
    csp = StaticFiles(str(tmp_path)).response("/").headers["Content-Security-Policy"]
    assert "script-src 'self' " + expected + ";" in csp
    assert "'unsafe-inline'" not in csp.split("script-src")[1].split(";")[0]


def test_pairing_start_with_token_from_another_computer():
    """Cura in Docker: requests never come from localhost, so the token authorises the pairing page."""
    pairing = Pairing(lambda: TOKEN)
    runner = MainThreadRunner(lambda f: f(), main_thread = threading.main_thread())
    router = build_router(FakeService(), runner, jobs = None, pairing = pairing, get_token = lambda: TOKEN)
    handler, _, _ = router.resolve("POST", "/api/pairing/start")
    remote = dict(client_ip = "172.17.0.1", server_ip = "172.17.0.2")
    body = json.loads(handler(Request("POST", "/", {}, {"Authorization": "Bearer " + TOKEN}, b"", **remote)).body)
    assert len(body["pin"]) == 6 and body["token"] == TOKEN
    with pytest.raises(ApiError) as info:
        handler(Request("POST", "/", {}, {"Authorization": "Bearer wrong"}, b"", **remote))
    assert info.value.code == "local_only"
