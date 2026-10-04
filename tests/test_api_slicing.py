"""Slicing, cancelling and G-code download over HTTP, with Cura faked."""

import gzip
import json
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
from RemoteWebControl.scene_worker import SceneWorker
from RemoteWebControl.server import ApiHttpServer, content_disposition
from RemoteWebControl.session import SliceCancelled
from meshes import binary_stl, box_triangles
from test_api_jobs import IDENTITY, ROT_X_90, TOKEN, FakeCura, call, upload

GCODE = ";FLAVOR:Marlin\n;Generated with Cura_SteamEngine 5.13.0\nG28\n" + "G1 X10 Y10 E1\n" * 2000 + ";End of Gcode\n"


class SlicingCura(FakeCura):
    def __init__(self):
        super().__init__()
        self.gate = threading.Event()
        self.gate.set()
        self.slice_error = None

    def slice(self, request, matrices, output_path, on_progress, is_cancelled):
        self.sliced = (request, [m.tolist() for m in matrices])
        on_progress(0.25)
        while not self.gate.wait(0.01):
            if is_cancelled():
                raise SliceCancelled()
        if self.slice_error:
            raise self.slice_error
        with open(output_path, "w", encoding = "utf-8", newline = "") as f:  # Keep "\n" also on Windows.
            f.write(GCODE)
        placed = {"matrix": IDENTITY, "fits": True, "bbox": {"min": [0, 0, 0], "max": [1, 1, 1]}, "warnings": []}
        placement = {"objects": [placed for _ in matrices], "fits": True, "bbox": placed["bbox"]}
        return {"placement": placement, "result": {
            "print_time_s": 1234, "fits": True, "job_name": "CE3PRO_pieza",
            "material": [{"extruder": 0, "name": "PLA", "length_m": 1.5, "weight_g": 4.47, "cost": 0.0}]}}


@pytest.fixture
def env(tmp_path):
    cura = SlicingCura()
    runner = MainThreadRunner(lambda f: f(), main_thread = threading.main_thread())
    worker = SceneWorker(lambda *a: None)
    worker.start()
    store = JobStore(str(tmp_path / "jobs"))
    jobs = JobService(store, worker, runner, cura.resolve_profile, cura.place, slice = cura.slice, max_preview_tris = 1000)
    config = Config("127.0.0.1", 0, TOKEN, (), 5, 7)
    server = ApiHttpServer(build_router(cura, runner, jobs), lambda: config, lambda *a: None)
    server.start()
    yield server, cura, store
    cura.gate.set()
    server.stop()
    worker.stop()


def new_job(server):
    return json.loads(upload(server, binary_stl(box_triangles()))[1])


def wait_state(server, job_id, states, timeout = 5):
    deadline = time.time() + timeout
    job = None
    while time.time() < deadline:
        job = json.loads(call(server, "GET", "/api/jobs/" + job_id)[1])
        if job["state"] in states:
            return job
        time.sleep(0.02)
    raise AssertionError("job did not reach {0}: {1}".format(states, job and job["state"]))


def wait_until(predicate, timeout = 5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_slice_and_download(env):
    server, _, _ = env
    job = new_job(server)
    response, data = call(server, "GET", "/api/jobs/{0}/gcode".format(job["id"]))
    assert (response.status, json.loads(data)["error"]["code"]) == (409, "gcode_not_ready")

    response, data = call(server, "POST", "/api/jobs/{0}/slice".format(job["id"]))
    assert response.status == 202
    assert json.loads(data)["state"] == "queued"
    done = wait_state(server, job["id"], {"done", "error"})
    assert done["state"] == "done", done
    assert done["progress"] == 1.0
    assert done["result"]["print_time_s"] == 1234
    assert done["result"]["gcode_bytes"] == len(GCODE.encode())

    response, data = call(server, "GET", "/api/jobs/{0}/gcode".format(job["id"]))
    assert response.status == 200
    assert response.getheader("Content-Type").startswith("text/x-gcode")
    assert response.getheader("Content-Encoding") is None
    assert 'filename="CE3PRO_pieza.gcode"' in response.getheader("Content-Disposition")
    assert data.decode("utf-8") == GCODE

    response, data = call(server, "GET", "/api/jobs/{0}/gcode".format(job["id"]), headers = {"Accept-Encoding": "gzip"})
    assert response.getheader("Content-Encoding") == "gzip"
    assert int(response.getheader("Content-Length")) == len(data) < len(GCODE) / 5
    assert gzip.decompress(data).decode("utf-8") == GCODE


def test_busy_job_rejects_changes_and_cancel_while_slicing(env):
    server, cura, store = env
    job = new_job(server)
    cura.gate.clear()
    call(server, "POST", "/api/jobs/{0}/slice".format(job["id"]))
    wait_state(server, job["id"], {"slicing"})
    assert wait_until(lambda: store.get(job["id"])["progress"] == 0.25)

    item = "/objects/" + job["objects"][0]["id"]
    for method, path, body in [("POST", "/slice", None), ("PUT", item + "/transform", json.dumps({"matrix": IDENTITY})),
                               ("DELETE", "", None), ("POST", "/auto-orient", None), ("POST", "/arrange", None),
                               ("POST", item + "/duplicate", None), ("DELETE", item, None),
                               ("POST", item + "/auto-orient", None)]:
        response, data = call(server, method, "/api/jobs/{0}{1}".format(job["id"], path), body)
        assert (response.status, json.loads(data)["error"]["code"]) == (409, "job_busy"), path

    response, _ = call(server, "POST", "/api/jobs/{0}/cancel".format(job["id"]))
    assert response.status == 200
    ready = wait_state(server, job["id"], {"ready"})
    assert ready["progress"] == 0.0 and ready["error"] is None
    response, data = call(server, "POST", "/api/jobs/{0}/cancel".format(job["id"]))
    assert (response.status, json.loads(data)["error"]["code"]) == (409, "job_not_running")


def test_cancel_while_queued(env):
    server, cura, _ = env
    first, second = new_job(server), new_job(server)
    cura.gate.clear()
    call(server, "POST", "/api/jobs/{0}/slice".format(first["id"]))
    wait_state(server, first["id"], {"slicing"})
    call(server, "POST", "/api/jobs/{0}/slice".format(second["id"]))
    assert wait_state(server, second["id"], {"queued"})["state"] == "queued"
    response, data = call(server, "POST", "/api/jobs/{0}/cancel".format(second["id"]))
    assert json.loads(data)["state"] == "ready"
    cura.gate.set()
    assert wait_state(server, first["id"], {"done"})["state"] == "done"
    time.sleep(0.1)
    assert json.loads(call(server, "GET", "/api/jobs/" + second["id"])[1])["state"] == "ready"  # It was skipped.


def test_status_requests_are_not_blocked_by_a_slice(env):
    server, cura, _ = env
    job = new_job(server)
    cura.gate.clear()
    call(server, "POST", "/api/jobs/{0}/slice".format(job["id"]))
    wait_state(server, job["id"], {"slicing"})
    started = time.time()
    mesh = "/api/jobs/{0}/objects/{1}/mesh".format(job["id"], job["objects"][0]["id"])
    for path in ("/api/jobs", "/api/jobs/" + job["id"], "/api/health", mesh):
        assert call(server, "GET", path)[0].status == 200
    assert time.time() - started < 1.0


def test_slice_uses_every_stored_matrix(env):
    server, cura, store = env
    job = json.loads(upload(server, files = [(binary_stl(box_triangles()), "a.stl"), (binary_stl(box_triangles()), "b.stl")])[1])
    call(server, "POST", "/api/jobs/{0}/slice".format(job["id"]))
    done = wait_state(server, job["id"], {"done"})
    request, matrices = cura.sliced
    assert matrices == [numpy.array(item["transform"]).reshape(4, 4).tolist() for item in job["objects"]]
    assert len(request.stl_paths) == 2
    assert done["placement"] == {"fits": True, "bbox": {"min": [0, 0, 0], "max": [1, 1, 1]}}


def test_slice_refused_at_once_when_the_job_does_not_fit(env):
    server, cura, store = env
    job = new_job(server)
    warning = {"code": "outside_build_volume", "message": "x"}

    def misplace(j):
        j["placement"]["fits"] = False
        j["objects"][0]["placement"].update(fits = False, warnings = [warning])
    store.update(job["id"], misplace)
    response, data = call(server, "POST", "/api/jobs/{0}/slice".format(job["id"]))
    error = json.loads(data)["error"]
    assert (response.status, error["code"]) == (422, "does_not_fit") and "outside_build_volume" in error["message"]
    assert store.get(job["id"])["state"] == "ready"


def test_slice_error_is_stored(env):
    server, cura, _ = env
    cura.slice_error = ApiError(422, "does_not_fit", "The model does not fit on the build plate (outside_build_volume).")
    job = new_job(server)
    call(server, "POST", "/api/jobs/{0}/slice".format(job["id"]))
    failed = wait_state(server, job["id"], {"error"})
    assert failed["error"]["code"] == "does_not_fit"


def test_new_transform_invalidates_the_gcode(env):
    server, _, store = env
    job = new_job(server)
    call(server, "POST", "/api/jobs/{0}/slice".format(job["id"]))
    wait_state(server, job["id"], {"done"})
    path = "/api/jobs/{0}/objects/{1}/transform".format(job["id"], job["objects"][0]["id"])
    call(server, "PUT", path, json.dumps({"matrix": ROT_X_90}))
    after = store.get(job["id"])
    assert (after["state"], after["result"]) == ("ready", None)
    response, _ = call(server, "GET", "/api/jobs/{0}/gcode".format(job["id"]))
    assert response.status == 409


def test_content_disposition_keeps_utf8_name():
    header = content_disposition('CE3PRO_piña "v2".gcode')
    assert header == "attachment; filename=\"CE3PRO_pi_a _v2_.gcode\"; filename*=UTF-8''CE3PRO_pi%C3%B1a%20%22v2%22.gcode"
