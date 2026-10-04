"""Slicing orchestration (session.run_slice) and scene worker priorities, with Cura faked."""

import threading
import time

import pytest

from RemoteWebControl.errors import ApiError
from RemoteWebControl.main_thread import MainThreadRunner
from RemoteWebControl.scene_worker import PRIORITY_SLICE, SceneWorker
from RemoteWebControl.session import SceneRequest, SliceCancelled, run_slice

REQUEST = SceneRequest("Printer", {"quality": "standard", "intent": "default", "quality_changes": None}, ["C:/x/pieza.stl"])


class Waiter:
    def wait(self, timeout):
        return True


class SliceOps:
    """statuses: one list per attempt; each is the sequence returned by slice_status()
    (the last element repeats)."""

    def __init__(self, statuses, fits = True):
        self.calls = []
        self.attempts = [list(s) for s in statuses]
        self.fits = fits
        self.current = None

    def prepare(self, printer_id):
        self.calls.append("prepare")
        return "snapshot"

    def activate(self, printer_id, profile, snapshot):
        self.calls.append("activate")

    def apply_overrides(self, overrides):
        self.calls.append("apply_overrides")

    def start_load(self, path, first):
        self.calls.append("start_load")
        return Waiter()

    def place(self, matrices, arrange, moved):
        assert (arrange, moved) == ("exact", [])  # Slicing never re-arranges the job.
        self.calls.append("place")
        warnings = [] if self.fits else [{"code": "outside_build_volume", "message": "x"}]
        placed = {"matrix": "M", "fits": self.fits, "bbox": {}, "warnings": warnings}
        return {"objects": [placed for _ in matrices], "fits": self.fits, "bbox": {}}

    def start_slice(self):
        self.calls.append("start_slice")
        self.current = self.attempts.pop(0)

    def slice_status(self):
        state = self.current.pop(0) if len(self.current) > 1 else self.current[0]
        if isinstance(state, dict):
            return state
        return {"state": state, "progress": 0.5 if state == "processing" else None, "error": None}

    def stop_slice(self):
        self.calls.append("stop_slice")

    def finish_slice(self, output_path):
        self.calls.append("finish_slice")
        return {"print_time_s": 60, "material": []}

    def restore(self, snapshot):
        self.calls.append("restore")


def direct_runner():
    return MainThreadRunner(lambda f: f(), main_thread = threading.main_thread())


def slice_run(ops, cancelled = lambda: False, progress = None, **kwargs):
    return run_slice(ops, direct_runner(), REQUEST, ["M"], "out.gcode", lambda *a: None,
                     progress.append if progress is not None else (lambda p: None), cancelled,
                     sleep = lambda s: None, poll = 0, settle = 0, **kwargs)


def test_slice_success():
    progress = []
    ops = SliceOps([["waiting", "processing", "processing", "done"]])
    outcome = slice_run(ops, progress = progress)
    assert outcome["result"] == {"print_time_s": 60, "material": [], "fits": True}
    assert outcome["placement"]["objects"][0]["matrix"] == "M"
    assert ops.calls == ["prepare", "activate", "apply_overrides", "start_load", "place",
                         "start_slice", "finish_slice", "restore"]
    assert progress == [0.5, 0.5]


def test_slice_refused_when_it_does_not_fit():
    ops = SliceOps([["done"]], fits = False)
    with pytest.raises(ApiError) as info:
        slice_run(ops)
    assert info.value.code == "does_not_fit" and "outside_build_volume" in info.value.message
    assert "start_slice" not in ops.calls and ops.calls[-1] == "restore"


def test_slice_error_is_reported():
    error = {"state": "error", "progress": None, "error": {"code": "setting_error", "message": "Bad: infill_line_distance."}}
    ops = SliceOps([["processing", error]])
    with pytest.raises(ApiError) as info:
        slice_run(ops)
    assert (info.value.code, info.value.message) == ("setting_error", "Bad: infill_line_distance.")
    assert ops.calls[-1] == "restore"


def test_aborted_slice_is_retried():
    ops = SliceOps([["processing", "aborted"], ["processing", "done"]])
    assert slice_run(ops)["result"]["print_time_s"] == 60
    assert ops.calls.count("start_slice") == 2


def test_too_many_aborts_fail():
    ops = SliceOps([["aborted"]] * 3)
    with pytest.raises(ApiError) as info:
        slice_run(ops)
    assert info.value.code == "slice_failed"
    assert ops.calls[-1] == "restore"


def test_stalled_start_is_retried():
    ticks = iter(range(0, 10**7, 50))
    ops = SliceOps([["waiting"], ["done"]])
    assert slice_run(ops, clock = lambda: next(ticks))["result"]["print_time_s"] == 60
    assert ops.calls.count("start_slice") == 2


def test_cancel_stops_the_slice():
    ops = SliceOps([["processing"]])
    polls = []

    def cancelled():
        polls.append(1)
        return len(polls) > 3

    with pytest.raises(SliceCancelled):
        slice_run(ops, cancelled = cancelled)
    assert ops.calls[-2:] == ["stop_slice", "restore"]


def test_slice_timeout():
    ticks = iter(range(0, 10**7, 1000))
    ops = SliceOps([["processing"]])
    with pytest.raises(ApiError) as info:
        slice_run(ops, slice_timeout = 5000, clock = lambda: next(ticks))
    assert info.value.code == "slice_timeout"
    assert ops.calls[-2:] == ["stop_slice", "restore"]


def test_interactive_tasks_go_before_pending_slices():
    worker = SceneWorker(lambda *a: None)
    release = threading.Event()
    order = []
    worker.submit(release.wait)  # Occupies the worker as soon as it starts.
    worker.start()
    time.sleep(0.05)
    worker.submit(order.append, "slice1", priority = PRIORITY_SLICE)
    worker.submit(order.append, "slice2", priority = PRIORITY_SLICE)
    worker.submit(order.append, "place")
    release.set()
    last = worker.submit(lambda: None, priority = PRIORITY_SLICE + 1)
    last.result(timeout = 5)
    worker.stop()
    assert order == ["place", "slice1", "slice2"]
