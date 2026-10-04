"""End-to-end tests of the job routes over HTTP, with Cura replaced by fakes."""

import gzip
import http.client
import json
import os
import threading
import time

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


def shifted(offset):
    """IDENTITY moved `offset` mm in X: what the fake does with new or arranged objects."""
    return IDENTITY[:3] + [float(offset)] + IDENTITY[4:]


class FakeCura:
    """Places new objects (matrix None) 50 mm apart, keeps the others as given, and records the calls."""

    def __init__(self):
        self.placements = []
        self.fail_placement = None
        self.delay = 0.0

    def info(self):
        return {}

    def resolve_profile(self, printer_id, profile):
        if printer_id != "Ender #2":
            raise ApiError(404, "printer_not_found", "no")
        return profile or {"quality": "standard", "intent": "default", "quality_changes": None}

    def place(self, request, matrices, auto_orient, arrange, moved):
        self.placements.append({
            "request": request, "matrices": [None if m is None else m.tolist() for m in matrices],
            "auto_orient": list(auto_orient), "arrange": arrange, "moved": list(moved),
        })
        if self.fail_placement:
            raise self.fail_placement
        time.sleep(self.delay)
        objects = []
        for index, matrix in enumerate(matrices):
            if index in auto_orient:
                applied = ROT_X_90
            elif matrix is None or arrange == "all":
                applied = shifted(50 * index)
            else:
                applied = [float(v) for v in numpy.asarray(matrix).reshape(16)]
            objects.append({"matrix": applied, "fits": True, "bbox": {"min": [0, 0, 0], "max": [1, 1, 1]}, "warnings": []})
        return {"objects": objects, "fits": True, "bbox": {"min": [0, 0, 0], "max": [1, 1, 1]}}


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


def form(fields):
    body = b""
    for name, value, fname in fields:
        body += b"--" + BOUNDARY.encode() + b"\r\n"
        disposition = 'Content-Disposition: form-data; name="{0}"'.format(name)
        if fname:
            disposition += '; filename="{0}"'.format(fname)
        body += disposition.encode() + b"\r\n\r\n" + value + b"\r\n"
    body += b"--" + BOUNDARY.encode() + b"--\r\n"
    return body, {"Content-Type": "multipart/form-data; boundary=" + BOUNDARY}


def upload(server, stl = None, printer_id = "Ender #2", profile = None, filename = "pieza.stl", files = None, extra = ()):
    files = files if files is not None else [(stl, filename)]
    fields = [("file", data, name) for data, name in files] + [("printer_id", printer_id.encode(), None)]
    if profile is not None:
        fields.append(("profile", json.dumps(profile).encode(), None))
    body, headers = form(fields + list(extra))
    return call(server, "POST", "/api/jobs", body, headers)


def new_job(server, count = 1):
    files = [(binary_stl(box_triangles()), "pieza{0}.stl".format(i)) for i in range(count)]
    response, data = upload(server, files = files)
    assert response.status == 201, data
    return json.loads(data)


def test_create_get_list_delete(env):
    server, cura, store = env
    response, data = upload(server, binary_stl(box_triangles()), profile = {"quality": "fine"})
    assert response.status == 201, data
    job = json.loads(data)
    assert job["state"] == "ready"
    assert job["name"] == "pieza.stl"
    assert job["profile"] == {"quality": "fine", "intent": "default", "quality_changes": None}
    assert job["placement"] == {"fits": True, "bbox": {"min": [0, 0, 0], "max": [1, 1, 1]}}
    [item] = job["objects"]
    assert item["name"] == "pieza.stl" and item["transform"] == IDENTITY
    assert item["placement"]["fits"] is True
    assert item["mesh"] == {"triangles": 12, "preview_triangles": 12, "decimated": False,
                            "bbox": {"min": [0.0, 0.0, 0.0], "max": [10.0, 20.0, 30.0]}}
    placed = cura.placements[0]
    assert placed["matrices"] == [None] and placed["auto_orient"] == [] and placed["arrange"] == "auto"
    assert placed["request"].stl_paths[0].endswith(os.path.join("load", "pieza.stl"))
    assert placed["request"].printer_id == "Ender #2"

    response, data = call(server, "GET", "/api/jobs/" + job["id"])
    assert json.loads(data) == job
    response, data = call(server, "GET", "/api/jobs")
    assert [(j["id"], j["objects"]) for j in json.loads(data)] == [(job["id"], 1)]

    response, _ = call(server, "DELETE", "/api/jobs/" + job["id"])
    assert response.status == 204
    response, data = call(server, "GET", "/api/jobs/" + job["id"])
    assert (response.status, json.loads(data)["error"]["code"]) == (404, "job_not_found")


def test_several_files_in_one_job(env):
    server, cura, store = env
    job = new_job(server, 3)
    assert [item["name"] for item in job["objects"]] == ["pieza0.stl", "pieza1.stl", "pieza2.stl"]
    assert job["name"] == "pieza0.stl"
    assert [item["transform"] for item in job["objects"]] == [shifted(0), shifted(50), shifted(100)]
    assert len({item["file"] for item in job["objects"]}) == 3
    placed = cura.placements[-1]
    assert len(placed["request"].stl_paths) == 3 and placed["moved"] == [0, 1, 2]


def test_default_profile_comes_from_the_printer(env):
    server, _, _ = env
    assert new_job(server)["profile"]["quality"] == "standard"


def test_mesh_endpoint_decimates_and_gzips(env):
    server, _, _ = env
    sphere = sphere_triangles(60)
    job = json.loads(upload(server, binary_stl(sphere))[1])
    item = job["objects"][0]
    assert item["mesh"]["decimated"] is True
    assert item["mesh"]["preview_triangles"] <= 1000

    path = "/api/jobs/{0}/objects/{1}/mesh".format(job["id"], item["id"])
    response, data = call(server, "GET", path, headers = {"Accept-Encoding": "gzip"})
    assert response.getheader("Content-Type") == "application/octet-stream"
    assert response.getheader("Content-Encoding") == "gzip"
    triangles, flags = mesh_format.decode(gzip.decompress(data))
    assert flags == mesh_format.FLAG_DECIMATED
    assert len(triangles) == item["mesh"]["preview_triangles"]

    response, data = call(server, "GET", "/api/jobs/{0}/objects/{1}/mesh".format(job["id"], "0" * 12))
    assert (response.status, json.loads(data)["error"]["code"]) == (404, "object_not_found")


def test_transform_one_object(env):
    server, cura, store = env
    job = new_job(server, 2)
    first, second = job["objects"]
    path = "/api/jobs/{0}/objects/{1}/transform".format(job["id"], second["id"])
    response, data = call(server, "PUT", path, json.dumps({"matrix": ROT_X_90}))
    assert response.status == 200, data
    updated = json.loads(data)
    assert [item["transform"] for item in updated["objects"]] == [shifted(0), ROT_X_90]
    placed = cura.placements[-1]
    assert placed["matrices"] == [numpy.array(shifted(0)).reshape(4, 4).tolist(), numpy.array(ROT_X_90).reshape(4, 4).tolist()]
    assert (placed["arrange"], placed["moved"]) == ("auto", [1])
    assert store.get(job["id"]) == updated

    response, data = call(server, "PUT", path, json.dumps({"matrix": [1, 2]}))
    assert (response.status, json.loads(data)["error"]["code"]) == (400, "invalid_matrix")


def test_transform_refused_while_busy(env):
    server, _, store = env
    job = new_job(server)
    store.update(job["id"], lambda j: j.update(state = "slicing"))
    path = "/api/jobs/{0}/objects/{1}/transform".format(job["id"], job["objects"][0]["id"])
    response, data = call(server, "PUT", path, json.dumps({"matrix": IDENTITY}))
    assert (response.status, json.loads(data)["error"]["code"]) == (409, "job_busy")


def test_add_objects(env):
    server, cura, store = env
    job = new_job(server)
    body, headers = form([("file", binary_stl(box_triangles()), "otra.stl"), ("file", binary_stl(box_triangles()), "otra.stl")])
    response, data = call(server, "POST", "/api/jobs/{0}/objects".format(job["id"]), body, headers)
    assert response.status == 200, data
    updated = json.loads(data)
    assert [item["name"] for item in updated["objects"]] == ["pieza0.stl", "otra.stl", "otra.stl"]
    assert updated["objects"][0]["transform"] == IDENTITY  # Untouched by the new ones.
    placed = cura.placements[-1]
    assert placed["matrices"][1:] == [None, None] and placed["moved"] == [1, 2]
    assert len(os.listdir(os.path.join(store.job_dir(job["id"]), "files"))) == 3


def test_failed_addition_leaves_the_job_as_it_was(env):
    server, cura, store = env
    job = new_job(server)
    cura.fail_placement = ApiError(422, "load_failed", "nope")
    body, headers = form([("file", binary_stl(box_triangles()), "otra.stl")])
    response, data = call(server, "POST", "/api/jobs/{0}/objects".format(job["id"]), body, headers)
    assert (response.status, json.loads(data)["error"]["code"]) == (422, "load_failed")
    assert store.get(job["id"])["objects"] == job["objects"]
    assert os.listdir(os.path.join(store.job_dir(job["id"]), "files")) == [job["objects"][0]["file"]]


def test_concurrent_additions_keep_each_others_files(env):
    server, cura, store = env
    job = new_job(server)
    cura.delay = 0.3  # The second upload arrives while Cura is placing the first one.
    body, headers = form([("file", binary_stl(box_triangles()), "otra.stl")])
    path = "/api/jobs/{0}/objects".format(job["id"])
    results = []
    threads = [threading.Thread(target = lambda: results.append(call(server, "POST", path, body, headers)[0].status)) for _ in range(2)]
    for thread in threads:
        thread.start()
        time.sleep(0.1)
    for thread in threads:
        thread.join()
    assert results == [200, 200]
    objects = store.get(job["id"])["objects"]
    assert len(objects) == 3
    for item in objects:
        assert os.path.exists(store.load_path(job["id"], item))


def test_duplicate_and_remove(env):
    server, cura, store = env
    job = new_job(server)
    source = job["objects"][0]
    path = "/api/jobs/{0}/objects/{1}".format(job["id"], source["id"])
    response, data = call(server, "POST", path + "/duplicate", json.dumps({"count": 2}))
    assert response.status == 200, data
    objects = json.loads(data)["objects"]
    assert len(objects) == 3 and {item["file"] for item in objects} == {source["file"]}
    placed = cura.placements[-1]
    assert placed["matrices"][1] == numpy.array(source["transform"]).reshape(4, 4).tolist()  # Same orientation.
    assert placed["moved"] == [1, 2]

    response, data = call(server, "POST", path + "/duplicate")  # No body: one copy.
    assert len(json.loads(data)["objects"]) == 4
    response, data = call(server, "POST", path + "/duplicate", json.dumps({"count": 0}))
    assert (response.status, json.loads(data)["error"]["code"]) == (400, "invalid_request")

    # Removing the original keeps the file, which the copies still use.
    response, data = call(server, "DELETE", path)
    assert response.status == 200, data
    objects = json.loads(data)["objects"]
    assert len(objects) == 3 and source["id"] not in [item["id"] for item in objects]
    assert os.listdir(os.path.join(store.job_dir(job["id"]), "files")) == [source["file"]]
    assert json.loads(data)["name"] == source["name"]


def test_removing_an_object_deletes_its_file(env):
    server, _, store = env
    job = new_job(server, 2)
    first, second = job["objects"]
    response, data = call(server, "DELETE", "/api/jobs/{0}/objects/{1}".format(job["id"], first["id"]))
    assert response.status == 200, data
    updated = json.loads(data)
    assert [item["id"] for item in updated["objects"]] == [second["id"]]
    assert updated["name"] == "pieza1.stl"
    assert os.listdir(os.path.join(store.job_dir(job["id"]), "files")) == [second["file"]]

    response, data = call(server, "DELETE", "/api/jobs/{0}/objects/{1}".format(job["id"], second["id"]))
    assert (response.status, json.loads(data)["error"]["code"]) == (409, "last_object")


def test_arrange(env):
    server, cura, _ = env
    job = new_job(server, 2)
    response, data = call(server, "POST", "/api/jobs/{0}/arrange".format(job["id"]))
    assert response.status == 200, data
    assert cura.placements[-1]["arrange"] == "all"


@pytest.mark.parametrize("kwargs,status,code", [
    ({"stl": b"not an stl at all" * 10}, 422, "invalid_stl"),
    ({"stl": binary_stl(box_triangles()), "printer_id": "nope"}, 404, "printer_not_found"),
    ({"stl": binary_stl(box_triangles()), "printer_id": ""}, 400, "missing_printer_id"),
    ({"stl": binary_stl(box_triangles()), "profile": {"bad": 1}}, 400, "invalid_profile"),
    ({"files": []}, 400, "missing_file"),
    ({"files": [(binary_stl(box_triangles()), "a.stl"), (b"bad" * 30, "b.stl")]}, 422, "invalid_stl"),
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


def test_auto_orient_on_upload(env):
    server, cura, _ = env
    files = [(binary_stl(box_triangles()), "a.stl"), (binary_stl(box_triangles()), "b.stl")]
    response, data = upload(server, files = files, extra = [("auto_orient", b"true", None)])
    assert response.status == 201, data
    assert cura.placements[-1]["auto_orient"] == [0, 1]
    assert [item["transform"] for item in json.loads(data)["objects"]] == [ROT_X_90, ROT_X_90]


def test_auto_orient_endpoints(env):
    server, cura, store = env
    job = new_job(server, 2)
    assert cura.placements[-1]["auto_orient"] == []
    second = job["objects"][1]
    response, data = call(server, "POST", "/api/jobs/{0}/objects/{1}/auto-orient".format(job["id"], second["id"]))
    assert response.status == 200, data
    assert [item["transform"] for item in json.loads(data)["objects"]] == [shifted(0), ROT_X_90]
    assert (cura.placements[-1]["auto_orient"], cura.placements[-1]["moved"]) == ([1], [1])

    response, data = call(server, "POST", "/api/jobs/{0}/auto-orient".format(job["id"]))
    assert response.status == 200, data
    assert (cura.placements[-1]["auto_orient"], cura.placements[-1]["moved"]) == ([0, 1], [0, 1])
    assert [item["transform"] for item in store.get(job["id"])["objects"]] == [ROT_X_90, ROT_X_90]


def test_auto_orient_invalid_value(env):
    server, _, store = env
    response, data = upload(server, binary_stl(box_triangles()), extra = [("auto_orient", b"maybe", None)])
    assert (response.status, json.loads(data)["error"]["code"]) == (400, "invalid_form")
    assert store.list() == []
