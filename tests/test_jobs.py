import json
import os

import pytest

from RemoteWebControl.errors import ApiError
from RemoteWebControl.jobs import JobStore, parse_profile, safe_filename, summary

PROFILE = {"quality": "standard", "intent": "default", "quality_changes": None}
MESH = {"triangles": 12, "preview_triangles": 12, "decimated": False, "bbox": {"min": [0, 0, 0], "max": [1, 1, 1]}}


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(tmp_path, clock):
    return JobStore(str(tmp_path / "jobs"), clock = clock)


def create(store, name = "pieza.stl"):
    return store.create(b"solid x", name, "Printer #2", PROFILE, MESH, b"CRM1")


def test_create_persists_files_and_document(store):
    job = create(store, "Soporte móvil v2.stl")
    directory = store.job_dir(job["id"])
    assert sorted(os.listdir(directory)) == ["job.json", "load", "model.stl", "preview.bin"]
    assert os.listdir(os.path.join(directory, "load")) == ["Soporte móvil v2.stl"]
    with open(store.load_path(job), "rb") as f:
        assert f.read() == b"solid x"
    assert store.get(job["id"]) == job
    assert job["state"] == "created"
    assert job["created_at"] == "2027-01-15T08:00:00Z"
    assert job["overrides"] == {"global": {}, "extruders": {}}
    assert job["transform"] is None


def test_list_is_newest_first_and_summary(store, clock):
    first = create(store)
    clock.now += 60
    second = create(store)
    assert [j["id"] for j in store.list()] == [second["id"], first["id"]]
    assert set(summary(first)) == {"id", "name", "created_at", "updated_at", "printer_id", "state", "progress", "error"}


def test_update_and_delete(store, clock):
    job = create(store)
    clock.now += 5
    updated = store.update(job["id"], lambda j: j.update(state = "ready"))
    assert updated["state"] == "ready"
    assert updated["updated_at"] != job["updated_at"]
    store.delete(job["id"])
    with pytest.raises(ApiError) as info:
        store.get(job["id"])
    assert info.value.code == "job_not_found"


def test_busy_job_cannot_be_deleted(store):
    job = create(store)
    store.update(job["id"], lambda j: j.update(state = "slicing"))
    with pytest.raises(ApiError) as info:
        store.delete(job["id"])
    assert (info.value.status, info.value.code) == (409, "job_busy")


@pytest.mark.parametrize("job_id", ["..", "../../etc", "ABC", "0" * 31, "g" * 32, ""])
def test_invalid_ids_are_not_found(store, job_id):
    with pytest.raises(ApiError) as info:
        store.get(job_id)
    assert info.value.status == 404


def test_cleanup_removes_old_jobs_and_marks_interrupted(store, clock):
    old = create(store)
    clock.now += 8 * 86400
    busy = create(store)
    store.update(busy["id"], lambda j: j.update(state = "slicing"))
    fresh = create(store)
    os.makedirs(os.path.join(store.root, "not-a-job"))

    result = store.cleanup(retention_days = 7)
    assert result == {"removed": [old["id"]], "interrupted": [busy["id"]]}
    assert store.get(busy["id"])["state"] == "error"
    assert store.get(busy["id"])["error"]["code"] == "interrupted"
    assert "slicing" in store.get(busy["id"])["error"]["message"]
    assert store.get(fresh["id"])["state"] == "created"


def test_corrupt_job_is_skipped_in_list(store):
    job = create(store)
    with open(os.path.join(store.job_dir(job["id"]), "job.json"), "w") as f:
        f.write("{broken")
    assert store.list() == []


@pytest.mark.parametrize("name,expected", [
    ("pieza.stl", "pieza.stl"),
    ("PIEZA.STL", "PIEZA.stl"),
    ("C:\\Users\\x\\Desktop\\soporte.stl", "soporte.stl"),
    ("../../etc/passwd", "passwd.stl"),
    ('a<b>c:d"e|f?g*h.stl', "a_b_c_d_e_f_g_h.stl"),
    ("", "model.stl"),
    (None, "model.stl"),
    ("CON.stl", "model.stl"),
    ("...", "model.stl"),
])
def test_safe_filename(name, expected):
    assert safe_filename(name) == expected


def test_parse_profile():
    assert parse_profile('{"quality": "fine"}') == {"quality": "fine", "intent": "default", "quality_changes": None}
    assert parse_profile({"quality_changes": "Mi perfil"}) == {"quality": None, "intent": "default", "quality_changes": "Mi perfil"}
    assert parse_profile({"quality": "fine", "intent": "engineering", "quality_changes": ""})["intent"] == "engineering"
    for bad in ["nope", "[]", {}, {"quality": 3}, {"quality": "x", "extra": 1}]:
        with pytest.raises(ApiError) as info:
            parse_profile(bad)
        assert info.value.code == "invalid_profile"
