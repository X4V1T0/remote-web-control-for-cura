"""End-to-end tests of the job routes over HTTP, with Cura replaced by fakes."""

import gzip
import http.client
import json
import threading

import numpy
import pytest

from RemoteWebControl.api import build_router
from RemoteWebControl.config import Config
from RemoteWebControl.errors import ApiError
from RemoteWebControl.job_service import JobService
from RemoteWebControl.jobs import JobStore
from RemoteWebControl.main_thread import MainThreadRunner
from RemoteWebControl import mesh_format
from RemoteWebControl.scene_worker import SceneWorker
from RemoteWebControl.server import ApiHttpServer
from meshes import binary_stl, box_triangles, sphere_triangles

TOKEN = "t0ken"
BOUNDARY = "xXxBoundaryxXx"
IDENTITY = [1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0]
ROT_X_90 = [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, -1.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0]


class FakeCura:
    def __init__(self):
        self.placements = []
        self.fail_placement = None

    def info(self):
        return {}

    def resolve_profile(self, printer_id, profile):
        if printer_id != "Ender #2":
            raise ApiError(404, "printer_not_found", "no")
        return profile or {"quality": "standard", "intent": "default", "quality_changes": None}

    def place(self, request, matrix, auto_orient):
        self.placements.append((request, None if matrix is None else matrix.tolist(), auto_orient))
        if self.fail_placement:
            raise self.fail_placement
        if auto_orient:
            applied = ROT_X_90
        else:
            applied = IDENTITY if matrix is None else [float(v) for v in numpy.asarray(matrix).reshape(16)]
        return {"matrix": applied, "fits": True, "bbox": {"min": [0, 0, 0], "max": [1, 1, 1]}, "warnings": []}


@pytest.fixture
def env(tmp_path):
    cura = FakeCura()
    runner = MainThreadRunner(lambda f: f(), main_thread = threading.main_thread())
    worker = SceneWorker(lambda *a: None)
    worker.start()
    store = JobStore(str(tmp_path / "jobs"))
    jobs = JobService(store, worker, runner, cura.resolve_profile, cura.place, max_preview_tris = 1000)
    config = Config("127.0.0.1", 0, TOKEN, (), 5, 7)
    server = ApiHttpServer(build_router(cura, runner, jobs), lambda: config, lambda *a: None)
    server.start()
    yield server, cura, store
    server.stop()
    worker.stop()


def call(server, method, path, body = None, headers = None):
    host, port = server.server_address
    conn = http.client.HTTPConnection(host, port, timeout = 10)
    all_headers = {"Authorization": "Bearer " + TOKEN}
    all_headers.update(headers or {})
    conn.request(method, path, body = body, headers = all_headers)
    response = conn.getresponse()
    data = response.read()
    conn.close()
    return response, data


def upload(server, stl, printer_id = "Ender #2", profile = None, filename = "pieza.stl"):
    fields = [("file", stl, filename), ("printer_id", printer_id.encode(), None)]
    if profile is not None:
        fields.append(("profile", json.dumps(profile).encode(), None))
    body = b""
    for name, value, fname in fields:
        body += b"--" + BOUNDARY.encode() + b"\r\n"
        disposition = 'Content-Disposition: form-data; name="{0}"'.format(name)
        if fname:
            disposition += '; filename="{0}"'.format(fname)
        body += disposition.encode() + b"\r\n\r\n" + value + b"\r\n"
    body += b"--" + BOUNDARY.encode() + b"--\r\n"
    return call(server, "POST", "/api/jobs", body, {"Content-Type": "multipart/form-data; boundary=" + BOUNDARY})


def test_create_get_list_delete(env):
    server, cura, store = env
    response, data = upload(server, binary_stl(box_triangles()), profile = {"quality": "fine"})
    assert response.status == 201, data
    job = json.loads(data)
    assert job["state"] == "ready"
    assert job["name"] == "pieza.stl"
    assert job["profile"] == {"quality": "fine", "intent": "default", "quality_changes": None}
    assert job["transform"] == IDENTITY
    assert job["placement"]["fits"] is True
    assert job["mesh"] == {"triangles": 12, "preview_triangles": 12, "decimated": False,
                           "bbox": {"min": [0.0, 0.0, 0.0], "max": [10.0, 20.0, 30.0]}}
    request, matrix, auto_orient = cura.placements[0]
    assert matrix is None and not auto_orient
    assert request.stl_path.endswith("pieza.stl") and request.printer_id == "Ender #2"

    response, data = call(server, "GET", "/api/jobs/" + job["id"])
    assert json.loads(data) == job
    response, data = call(server, "GET", "/api/jobs")
    assert [j["id"] for j in json.loads(data)] == [job["id"]]

    response, _ = call(server, "DELETE", "/api/jobs/" + job["id"])
    assert response.status == 204
    response, data = call(server, "GET", "/api/jobs/" + job["id"])
    assert (response.status, json.loads(data)["error"]["code"]) == (404, "job_not_found")


def test_default_profile_comes_from_the_printer(env):
    server, _, _ = env
    response, data = upload(server, binary_stl(box_triangles()))
    assert json.loads(data)["profile"]["quality"] == "standard"


def test_mesh_endpoint_decimates_and_gzips(env):
    server, _, _ = env
    sphere = sphere_triangles(60)
    job = json.loads(upload(server, binary_stl(sphere))[1])
    assert job["mesh"]["decimated"] is True
    assert job["mesh"]["preview_triangles"] <= 1000

    response, data = call(server, "GET", "/api/jobs/{0}/mesh".format(job["id"]), headers = {"Accept-Encoding": "gzip"})
    assert response.getheader("Content-Type") == "application/octet-stream"
    assert response.getheader("Content-Encoding") == "gzip"
    triangles, flags = mesh_format.decode(gzip.decompress(data))
    assert flags == mesh_format.FLAG_DECIMATED
    assert len(triangles) == job["mesh"]["preview_triangles"]


def test_transform(env):
    server, cura, store = env
    job = json.loads(upload(server, binary_stl(box_triangles()))[1])
    response, data = call(server, "PUT", "/api/jobs/{0}/transform".format(job["id"]), json.dumps({"matrix": ROT_X_90}))
    assert response.status == 200, data
    result = json.loads(data)
    assert set(result) == {"matrix", "fits", "bbox", "warnings"}
    assert cura.placements[-1][1] == numpy.array(ROT_X_90).reshape(4, 4).tolist()
    assert store.get(job["id"])["transform"] == ROT_X_90

    response, data = call(server, "PUT", "/api/jobs/{0}/transform".format(job["id"]), json.dumps({"matrix": [1, 2]}))
    assert (response.status, json.loads(data)["error"]["code"]) == (400, "invalid_matrix")


def test_transform_refused_while_busy(env):
    server, _, store = env
    job = json.loads(upload(server, binary_stl(box_triangles()))[1])
    store.update(job["id"], lambda j: j.update(state = "slicing"))
    response, data = call(server, "PUT", "/api/jobs/{0}/transform".format(job["id"]), json.dumps({"matrix": IDENTITY}))
    assert (response.status, json.loads(data)["error"]["code"]) == (409, "job_busy")


@pytest.mark.parametrize("kwargs,status,code", [
    ({"stl": b"not an stl at all" * 10}, 422, "invalid_stl"),
    ({"stl": binary_stl(box_triangles()), "printer_id": "nope"}, 404, "printer_not_found"),
    ({"stl": binary_stl(box_triangles()), "printer_id": ""}, 400, "missing_printer_id"),
    ({"stl": binary_stl(box_triangles()), "profile": {"bad": 1}}, 400, "invalid_profile"),
])
def test_create_errors_leave_no_job(env, kwargs, status, code):
    server, _, store = env
    response, data = upload(server, **kwargs)
    assert (response.status, json.loads(data)["error"]["code"]) == (status, code)
    assert store.list() == []


def test_failed_placement_removes_the_job(env):
    server, cura, store = env
    cura.fail_placement = ApiError(409, "scene_not_empty", "busy plate")
    response, data = upload(server, binary_stl(box_triangles()))
    assert (response.status, json.loads(data)["error"]["code"]) == (409, "scene_not_empty")
    assert store.list() == []


def upload_with(server, extra_fields):
    fields = [("file", binary_stl(box_triangles()), "pieza.stl"), ("printer_id", b"Ender #2", None)] + extra_fields
    body = b""
    for name, value, fname in fields:
        body += b"--" + BOUNDARY.encode() + b"\r\n"
        disposition = 'Content-Disposition: form-data; name="{0}"'.format(name)
        if fname:
            disposition += '; filename="{0}"'.format(fname)
        body += disposition.encode() + b"\r\n\r\n" + value + b"\r\n"
    body += b"--" + BOUNDARY.encode() + b"--\r\n"
    return call(server, "POST", "/api/jobs", body, {"Content-Type": "multipart/form-data; boundary=" + BOUNDARY})


def test_auto_orient_on_upload(env):
    server, cura, _ = env
    response, data = upload_with(server, [("auto_orient", b"true", None)])
    assert response.status == 201, data
    assert cura.placements[-1][2] is True
    assert json.loads(data)["transform"] == ROT_X_90


def test_auto_orient_endpoint(env):
    server, cura, store = env
    job = json.loads(upload(server, binary_stl(box_triangles()))[1])
    assert cura.placements[-1][2] is False
    response, data = call(server, "POST", "/api/jobs/{0}/auto-orient".format(job["id"]))
    assert response.status == 200, data
    assert json.loads(data)["matrix"] == ROT_X_90
    assert cura.placements[-1][1:] == (None, True)
    assert store.get(job["id"])["transform"] == ROT_X_90


def test_auto_orient_invalid_value(env):
    server, _, store = env
    response, data = upload_with(server, [("auto_orient", b"maybe", None)])
    assert (response.status, json.loads(data)["error"]["code"]) == (400, "invalid_form")
    assert store.list() == []
