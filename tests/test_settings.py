"""Settings: tree, visibility, value checks, diff, session flows and HTTP routes (Cura faked)."""

import json
import threading
from enum import Enum

import pytest

from RemoteWebControl import settings_schema as schema
from RemoteWebControl.api import build_router
from RemoteWebControl.config import Config
from RemoteWebControl.errors import ApiError
from RemoteWebControl.job_service import JobService
from RemoteWebControl.jobs import JobStore
from RemoteWebControl.main_thread import MainThreadRunner
from RemoteWebControl.scene_worker import SceneWorker
from RemoteWebControl.server import ApiHttpServer
from RemoteWebControl.session import read_settings, settings_diff
from meshes import binary_stl, box_triangles
from test_api_jobs import TOKEN, FakeCura, call, upload

DEFINITIONS = [
    {"key": "resolution", "label": "Calidad", "description": "", "type": "category", "children": [
        {"key": "layer_height", "label": "Altura de capa", "description": "d", "type": "float", "unit": "mm",
         "options": None, "settable_per_extruder": False, "children": []},
    ]},
    {"key": "infill", "label": "Relleno", "description": "", "type": "category", "children": [
        {"key": "infill_sparse_density", "label": "Densidad", "description": "", "type": "float", "unit": "%",
         "options": None, "settable_per_extruder": True, "children": [
            {"key": "infill_line_distance", "label": "Distancia", "description": "", "type": "float", "unit": "mm",
             "options": None, "settable_per_extruder": True, "children": []},
        ]},
        {"key": "infill_pattern", "label": "Patrón", "description": "", "type": "enum", "unit": "",
         "options": [{"key": "grid", "label": "Rejilla"}, {"key": "gyroid", "label": "Giroide"}],
         "settable_per_extruder": True, "children": []},
    ]},
]


# ------------------------------------------------------------------ pure schema

def test_tree_with_visibility_keeps_ancestors_of_visible_settings():
    values = {"infill_line_distance": {"value": 6.0, "enabled": True, "validation_state": "Valid"}}
    tree = schema.build_tree(DEFINITIONS, values, {"infill_line_distance"}, set())
    assert [c["key"] for c in tree] == ["infill"]
    density = tree[0]["children"][0]
    assert (density["key"], density["visible"]) == ("infill_sparse_density", False)
    distance = density["children"][0]
    assert (distance["visible"], distance["value"], distance["validation_state"]) == (True, 6.0, "Valid")
    assert distance["unit"] == "mm" and distance["overridden"] is False


def test_tree_all_and_overridden_flags():
    tree = schema.build_tree(DEFINITIONS, {}, None, {"infill_pattern"})
    keys = [s["key"] for s in schema.iter_settings(tree)]
    assert keys == ["layer_height", "infill_sparse_density", "infill_line_distance", "infill_pattern"]
    pattern = [s for s in schema.iter_settings(tree) if s["key"] == "infill_pattern"][0]
    assert pattern["overridden"] is True
    assert pattern["options"][1] == {"key": "gyroid", "label": "Giroide"}
    assert all(set(schema.VALUE_PROPERTIES) <= set(s) for s in schema.iter_settings(tree))


def test_keys_by_category():
    assert schema.setting_keys_by_category(DEFINITIONS) == [
        ("resolution", ["layer_height"]),
        ("infill", ["infill_sparse_density", "infill_line_distance", "infill_pattern"]),
    ]


@pytest.mark.parametrize("setting_type,options,value,expected", [
    ("float", None, 20, 20),
    ("float", None, 0.15, 0.15),
    ("int", None, 3.0, 3),
    ("bool", None, False, False),
    ("enum", [{"key": "grid"}], "grid", "grid"),
    ("extruder", None, 1, "1"),
    ("optional_extruder", None, -1, "-1"),
    ("str", None, "hola", "hola"),
    ("[int]", None, [0, 90], [0, 90]),
])
def test_check_value_accepts(setting_type, options, value, expected):
    assert schema.check_value("k", setting_type, options, value) == expected


@pytest.mark.parametrize("setting_type,options,value,code", [
    ("float", None, "20", "invalid_value"),
    ("float", None, True, "invalid_value"),
    ("float", None, float("inf"), "invalid_value"),
    ("int", None, 2.5, "invalid_value"),
    ("bool", None, 1, "invalid_value"),
    ("enum", [{"key": "grid"}], "zigzag", "invalid_value"),
    ("extruder", None, -1, "invalid_value"),
    ("str", None, 5, "invalid_value"),
    ("[int]", None, "0,90", "invalid_value"),
    ("str", None, "=__import__('os')", "formulas_not_allowed"),
    ("float", None, " =infill_line_width * 2", "formulas_not_allowed"),
])
def test_check_value_rejects(setting_type, options, value, code):
    with pytest.raises(ApiError) as info:
        schema.check_value("k", setting_type, options, value)
    assert (info.value.status, info.value.code) == (422, code)


def test_apply_override_set_replace_remove():
    base = {"global": {"adhesion_type": "brim"}, "extruders": {}}
    changed = schema.apply_override(base, "extruder", 0, "infill_sparse_density", 40)
    assert changed == {"global": {"adhesion_type": "brim"}, "extruders": {"0": {"infill_sparse_density": 40}}}
    assert base == {"global": {"adhesion_type": "brim"}, "extruders": {}}  # Not mutated.
    removed = schema.apply_override(changed, "extruder", 0, "infill_sparse_density", None)
    assert removed == {"global": {"adhesion_type": "brim"}, "extruders": {}}
    assert schema.apply_override(removed, "global", None, "adhesion_type", None) == {"global": {}, "extruders": {}}


@pytest.mark.parametrize("payload", [
    None, [], {"scope": "both", "key": "a", "value": 1}, {"scope": "global", "value": 1},
    {"scope": "global", "key": "a"}, {"scope": "extruder", "extruder": -1, "key": "a", "value": 1},
    {"scope": "extruder", "extruder": True, "key": "a", "value": 1},
])
def test_parse_patch_rejects(payload):
    with pytest.raises(ApiError) as info:
        schema.parse_patch(payload)
    assert info.value.status == 400


def test_parse_patch_defaults_extruder_zero():
    assert schema.parse_patch({"scope": "extruder", "key": "k", "value": None}) == ("extruder", 0, "k", None)
    assert schema.parse_patch({"scope": "global", "key": "k", "value": 1}) == ("global", None, "k", 1)


def test_diff_reports_value_enabled_and_validation_changes():
    def entry(key, extruder, value, enabled = True, validation = "Valid"):
        return {"key": key, "extruder": extruder, "value": value, "enabled": enabled, "validation_state": validation}
    before = {
        "0:infill_sparse_density": entry("infill_sparse_density", 0, 20),
        "0:infill_line_distance": entry("infill_line_distance", 0, 6.0),
        "global:layer_height": entry("layer_height", None, 0.2),
        "0:support_angle": entry("support_angle", 0, 50, enabled = False),
    }
    after = dict(before)
    after["0:infill_sparse_density"] = entry("infill_sparse_density", 0, 40)
    after["0:infill_line_distance"] = entry("infill_line_distance", 0, 3.0)
    after["0:support_angle"] = entry("support_angle", 0, 50, enabled = True)
    changes = schema.diff_states(before, after)
    assert [(c["key"], c["extruder"]) for c in changes] == [
        ("infill_line_distance", 0), ("infill_sparse_density", 0), ("support_angle", 0)]
    assert changes[0]["value"] == 3.0 and changes[0]["before"]["value"] == 6.0
    assert changes[2]["enabled"] is True and changes[2]["before"]["enabled"] is False


def test_query_parsing():
    assert schema.parse_visibility(None) == "basic"
    assert schema.parse_visibility("EXPERT") == "expert"
    assert schema.parse_language("es-es") == "es_ES"
    assert schema.parse_language("de") == "de"
    assert schema.parse_language("") is None
    assert schema.parse_extruder("1") == 1
    for call_, value in [(schema.parse_visibility, "custom"), (schema.parse_language, "../../x"),
                         (schema.parse_extruder, "-1"), (schema.parse_extruder, "a")]:
        with pytest.raises(ApiError):
            call_(value)


def test_jsonable():
    class State(Enum):
        Valid = "Valid"
    assert schema.jsonable(State.Valid) == "Valid"
    assert schema.jsonable(float("nan")) is None
    assert schema.jsonable((1, [2.5, True])) == [1, [2.5, True]]
    assert schema.jsonable({1: "a"}) == {"1": "a"}


# ------------------------------------------------------------------ session flows

class StackOps:
    def __init__(self):
        self.calls = []
        self.overrides = None

    def prepare(self, printer_id):
        self.calls.append("prepare")
        return "snapshot"

    def activate(self, printer_id, profile, snapshot):
        self.calls.append("activate")

    def apply_overrides(self, overrides):
        self.calls.append("apply_overrides")
        self.overrides = overrides

    def replace_overrides(self, overrides):
        self.calls.append("replace_overrides")
        self.overrides = overrides

    def restore(self, snapshot):
        self.calls.append("restore")


class FakeSettingsOps:
    """Cura as a tiny formula engine: infill_line_distance = 120 / density."""

    def __init__(self, stack_ops):
        self.stack_ops = stack_ops
        self.evaluate_calls = 0

    def _density(self):
        return (self.stack_ops.overrides or {}).get("extruders", {}).get("0", {}).get("infill_sparse_density", 20)

    def definitions(self, language):
        return DEFINITIONS

    def visible_keys(self, preset):
        return None if preset == "all" else {"layer_height", "infill_sparse_density"}

    def evaluate(self, keys, extruder):
        self.evaluate_calls += 1
        return {key: {"value": self._value(key), "enabled": True, "validation_state": "Valid"} for key in keys}

    def _value(self, key):
        return {"layer_height": 0.2, "infill_sparse_density": self._density(),
                "infill_line_distance": 120 / self._density(), "infill_pattern": "grid"}[key]

    def state(self, keys):
        result = {}
        for key in keys:
            extruder = None if key == "layer_height" else 0
            result["{0}:{1}".format("global" if extruder is None else 0, key)] = {
                "key": key, "extruder": extruder, "value": self._value(key), "enabled": True,
                "validation_state": "MaximumError" if key == "infill_sparse_density" and self._density() > 100 else "Valid"}
        return result

    def check_override(self, scope, extruder, key):
        if key == "nope":
            raise ApiError(422, "unknown_setting", "Unknown setting 'nope'.")
        return {"type": "float", "options": None}


def direct_runner():
    return MainThreadRunner(lambda f: f(), main_thread = threading.main_thread())


def test_read_settings_restores_and_chunks_per_category():
    ops = StackOps()
    settings_ops = FakeSettingsOps(ops)
    overrides = {"global": {}, "extruders": {"0": {"infill_sparse_density": 40}}}
    tree = read_settings(ops, settings_ops, direct_runner(), "P", {"quality": "q"}, overrides, "basic", "es_ES", 0, lambda *a: None)
    assert ops.calls == ["prepare", "activate", "apply_overrides", "restore"]
    assert settings_ops.evaluate_calls == 2  # One main-thread call per category.
    density = [s for s in schema.iter_settings(tree) if s["key"] == "infill_sparse_density"][0]
    assert (density["value"], density["overridden"], density["visible"]) == (40, True, True)


def test_settings_diff_reports_dependent_settings():
    ops = StackOps()
    new_overrides, changes = settings_diff(ops, FakeSettingsOps(ops), direct_runner(), "P", {"quality": "q"},
                                           {"global": {}, "extruders": {}}, "extruder", 0, "infill_sparse_density", 40,
                                           lambda *a: None)
    assert new_overrides == {"global": {}, "extruders": {"0": {"infill_sparse_density": 40}}}
    assert [(c["key"], c["value"], c["before"]["value"]) for c in changes] == [
        ("infill_line_distance", 3.0, 6.0), ("infill_sparse_density", 40, 20)]
    assert ops.calls == ["prepare", "activate", "apply_overrides", "replace_overrides", "restore"]


def test_settings_diff_validation_error_still_restores():
    ops = StackOps()
    with pytest.raises(ApiError) as info:
        settings_diff(ops, FakeSettingsOps(ops), direct_runner(), "P", {}, {}, "global", None, "nope", 1, lambda *a: None)
    assert info.value.code == "unknown_setting"
    assert ops.calls[-1] == "restore"


# ------------------------------------------------------------------ HTTP

@pytest.fixture
def env(tmp_path):
    cura = FakeCura()
    runner = MainThreadRunner(lambda f: f(), main_thread = threading.main_thread())
    worker = SceneWorker(lambda *a: None)
    worker.start()
    store = JobStore(str(tmp_path / "jobs"))
    stack_ops = StackOps()
    settings_ops = FakeSettingsOps(stack_ops)
    log = lambda *a: None
    jobs = JobService(
        store, worker, runner, cura.resolve_profile, cura.place,
        read_settings = lambda p, prof, o, v, l, e: read_settings(stack_ops, settings_ops, runner, p, prof, o, v, l, e, log),
        settings_diff = lambda p, prof, o, s, e, k, v: settings_diff(stack_ops, settings_ops, runner, p, prof, o, s, e, k, v, log),
        max_preview_tris = 1000)
    config = Config("127.0.0.1", 0, TOKEN, (), 5, 7)
    server = ApiHttpServer(build_router(cura, runner, jobs), lambda: config, lambda *a: None)
    server.start()
    yield server, store
    server.stop()
    worker.stop()


def test_printer_settings_route(env):
    server, _ = env
    response, data = call(server, "GET", "/api/printers/Ender%20%232/settings?visibility=all&lang=es_ES")
    assert response.status == 200, data
    body = json.loads(data)
    assert body["printer_id"] == "Ender #2"
    assert body["visibility"] == "all"
    assert [c["key"] for c in body["categories"]] == ["resolution", "infill"]
    response, data = call(server, "GET", "/api/printers/Ender%20%232/settings?visibility=weird")
    assert (response.status, json.loads(data)["error"]["code"]) == (400, "invalid_visibility")


def test_patch_then_job_settings(env):
    server, store = env
    job = json.loads(upload(server, binary_stl(box_triangles()))[1])
    response, data = call(server, "PATCH", "/api/jobs/{0}/settings".format(job["id"]),
                          json.dumps({"scope": "extruder", "extruder": 0, "key": "infill_sparse_density", "value": 40}))
    assert response.status == 200, data
    body = json.loads(data)
    assert body["overrides"] == {"global": {}, "extruders": {"0": {"infill_sparse_density": 40}}}
    assert {c["key"] for c in body["changed"]} == {"infill_sparse_density", "infill_line_distance"}
    assert store.get(job["id"])["overrides"] == body["overrides"]

    body = json.loads(call(server, "GET", "/api/jobs/{0}/settings?visibility=all".format(job["id"]))[1])
    density = [s for s in schema.iter_settings(body["categories"]) if s["key"] == "infill_sparse_density"][0]
    assert (density["value"], density["overridden"]) == (40, True)

    response, data = call(server, "PATCH", "/api/jobs/{0}/settings".format(job["id"]),
                          json.dumps({"scope": "extruder", "extruder": 0, "key": "infill_sparse_density", "value": None}))
    assert json.loads(data)["overrides"] == {"global": {}, "extruders": {}}


def test_patch_rejected_while_busy_and_for_formulas(env):
    server, store = env
    job = json.loads(upload(server, binary_stl(box_triangles()))[1])
    response, data = call(server, "PATCH", "/api/jobs/{0}/settings".format(job["id"]),
                          json.dumps({"scope": "global", "key": "layer_height", "value": "=1/0"}))
    assert (response.status, json.loads(data)["error"]["code"]) == (422, "formulas_not_allowed")
    assert store.get(job["id"])["overrides"] == {"global": {}, "extruders": {}}

    store.update(job["id"], lambda j: j.update(state = "queued"))
    response, data = call(server, "PATCH", "/api/jobs/{0}/settings".format(job["id"]),
                          json.dumps({"scope": "global", "key": "layer_height", "value": 0.1}))
    assert (response.status, json.loads(data)["error"]["code"]) == (409, "job_busy")


def test_patch_resets_a_finished_job(env):
    server, store = env
    job = json.loads(upload(server, binary_stl(box_triangles()))[1])
    store.update(job["id"], lambda j: j.update(state = "done", progress = 1.0, result = {"print_time_s": 1}))
    call(server, "PATCH", "/api/jobs/{0}/settings".format(job["id"]),
         json.dumps({"scope": "global", "key": "layer_height", "value": 0.1}))
    after = store.get(job["id"])
    assert (after["state"], after["result"]) == ("ready", None)
