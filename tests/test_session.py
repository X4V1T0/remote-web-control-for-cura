import threading
import time

import pytest

from RemoteWebControl.errors import ApiError
from RemoteWebControl.main_thread import MainThreadRunner
from RemoteWebControl.scene_worker import SceneWorker
from RemoteWebControl.session import SceneRequest, materialized, run_placement

REQUEST = SceneRequest("Printer", {"quality": "standard", "intent": "default", "quality_changes": None}, ["C:/x/pieza.stl"])


class Waiter:
    def __init__(self, loaded):
        self.loaded = loaded

    def wait(self, timeout):
        return self.loaded


class FakeOps:
    def __init__(self, fail_at = None, loaded = True):
        self.calls = []
        self.fail_at = fail_at
        self.loaded = loaded
        self.loads = []
        self.places = []

    def _step(self, name, *args):
        self.calls.append(name)
        if name == self.fail_at:
            raise ApiError(422, "boom_" + name, name)

    def prepare(self, printer_id):
        self._step("prepare", printer_id)
        return "snapshot"

    def activate(self, printer_id, profile, snapshot):
        assert snapshot == "snapshot"
        self._step("activate")

    def apply_overrides(self, overrides):
        self._step("apply_overrides")

    def start_load(self, path, first):
        self.loads.append((path, first))
        self._step("start_load")
        return Waiter(self.loaded)

    def place(self, matrices, arrange, moved):
        self.places.append((matrices, arrange, moved))
        self._step("place")
        return {"matrices": matrices}

    def orientation_input(self, index):
        assert index == 0
        self._step("orientation_input")
        return {"vertices": "V", "min_volume": True}

    def compute_orientation(self, vertices, min_volume):
        assert (vertices, min_volume) == ("V", True)
        self.compute_thread = threading.current_thread()
        self._step("compute_orientation")
        return {"axis": [0, 0, 1], "angle": 1.0}

    def apply_orientation(self, index, orientation):
        assert index == 0
        assert orientation == {"axis": [0, 0, 1], "angle": 1.0}
        self._step("apply_orientation")

    def restore(self, snapshot):
        assert snapshot == "snapshot"
        self._step("restore")


def direct_runner():
    return MainThreadRunner(lambda f: f(), main_thread = threading.main_thread())


def run(ops, matrices = (None,), request = REQUEST, **kwargs):
    return run_placement(ops, direct_runner(), request, matrices, lambda *a: None, settle = 0, sleep = lambda s: None, **kwargs)


def test_happy_path_order():
    ops = FakeOps()
    assert run(ops, ["M"], arrange = "all", moved = [0]) == {"matrices": ["M"]}
    assert ops.calls == ["prepare", "activate", "apply_overrides", "start_load", "place", "restore"]
    assert ops.places == [(["M"], "all", [0])]


def test_files_are_loaded_in_order():
    ops = FakeOps()
    request = SceneRequest("Printer", REQUEST.profile, ["C:/x/a.stl", "C:/x/b.stl", "C:/x/a.stl"])
    run(ops, ["A", None, "C"], request = request)
    assert ops.loads == [("C:/x/a.stl", True), ("C:/x/b.stl", False), ("C:/x/a.stl", False)]
    assert ops.places == [(["A", None, "C"], "auto", ())]


def test_auto_orient_order_and_threads():
    loop_thread = threading.Thread(target = lambda: None)
    main_calls = []
    runner = MainThreadRunner(lambda f: (main_calls.append(True), f()), main_thread = loop_thread)
    ops = FakeOps()
    result = run_placement(ops, runner, REQUEST, ["ignored"], lambda *a: None, auto_orient = [0], moved = [0],
                           settle = 0, sleep = lambda s: None)
    assert result == {"matrices": [None]}  # The matrix is replaced by the computed orientation.
    assert ops.calls == ["prepare", "activate", "apply_overrides", "start_load", "place",
                         "orientation_input", "compute_orientation", "apply_orientation", "place", "restore"]
    # The others' matrices are applied first; the oriented one starts from Cura's load placement.
    assert ops.places == [([None], "exact", []), ([None], "auto", [0])]
    # The heavy computation is not posted to the main thread.
    assert ops.compute_thread is threading.current_thread()
    assert len(main_calls) == 9


@pytest.mark.parametrize("step", ["activate", "apply_overrides", "start_load", "orientation_input", "compute_orientation", "place"])
def test_restore_always_runs(step):
    ops = FakeOps(fail_at = step)
    with pytest.raises(ApiError) as info:
        run_placement(ops, direct_runner(), REQUEST, [None], lambda *a: None, auto_orient = [0], settle = 0, sleep = lambda s: None)
    assert info.value.code == "boom_" + step
    assert ops.calls[-1] == "restore"


def test_nothing_to_restore_when_prepare_fails():
    ops = FakeOps(fail_at = "prepare")
    with pytest.raises(ApiError):
        run(ops)
    assert ops.calls == ["prepare"]


def test_load_timeout():
    ops = FakeOps(loaded = False)
    with pytest.raises(ApiError) as info:
        run(ops)
    assert info.value.code == "load_failed"
    assert "place" not in ops.calls and ops.calls[-1] == "restore"


def test_restore_error_is_raised_only_without_other_error():
    logs = []
    ops = FakeOps(fail_at = "restore")
    with pytest.raises(ApiError) as info:
        run_placement(ops, direct_runner(), REQUEST, [None], lambda *a: logs.append(a), settle = 0, sleep = lambda s: None)
    assert info.value.code == "boom_restore"
    assert logs

    class FailTwice(FakeOps):
        def restore(self, snapshot):
            self.calls.append("restore")
            raise ApiError(500, "restore_failed", "x")
    ops = FailTwice(fail_at = "place")
    with pytest.raises(ApiError) as info:
        run_placement(ops, direct_runner(), REQUEST, [None], lambda *a: None, settle = 0, sleep = lambda s: None)
    assert info.value.code == "boom_place"  # The original error wins.


def test_exception_in_caller_block_still_restores():
    ops = FakeOps()
    with pytest.raises(RuntimeError):
        with materialized(ops, direct_runner(), REQUEST, lambda *a: None, settle = 0, sleep = lambda s: None):
            raise RuntimeError("slice failed")
    assert ops.calls[-1] == "restore"


# ------------------------------------------------------------------ worker

def test_worker_runs_tasks_one_at_a_time():
    worker = SceneWorker(lambda *a: None)
    worker.start()
    active, overlaps = [0], []

    def task(i):
        active[0] += 1
        overlaps.append(active[0])
        time.sleep(0.02)
        active[0] -= 1
        return i

    futures = [worker.submit(task, i) for i in range(5)]
    assert [f.result(timeout = 5) for f in futures] == list(range(5))
    assert max(overlaps) == 1
    worker.stop()


def test_worker_call_propagates_errors_and_times_out():
    worker = SceneWorker(lambda *a: None)
    worker.start()

    def fail():
        raise ApiError(409, "scene_not_empty", "x")
    with pytest.raises(ApiError) as info:
        worker.call(fail)
    assert info.value.code == "scene_not_empty"

    release = threading.Event()
    worker.submit(release.wait)
    with pytest.raises(ApiError) as info:
        worker.call(lambda: None, timeout = 0.05)
    assert (info.value.status, info.value.code) == (503, "scene_busy")
    release.set()
    worker.stop()
