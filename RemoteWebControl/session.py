"""Materialisation of a job in Cura's scene. Pure Python (Cura is behind the `ops` interface).

The scene is ephemeral. For every operation it is rebuilt from the job document:

    1. prepare   refuse if the scene is not empty; snapshot active printer, the job printer's
                 profile containers and its user containers
    2. activate  switch to the job's printer and profile (user containers cleared first)
    3. overrides write the job's overrides into the user containers
    4. load      readLocalFile() and wait for fileCompleted
    5. settle    let Qt's event loop run so BuildVolume/ConvexHull timers catch up
    6. ...       the caller's work (place, slice)
    7. restore   ALWAYS: empty the scene, restore profile, user values and active printer

`ops` is implemented by scene_ops.SceneOps; every call goes through the MainThreadRunner.
"""

import contextlib
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from .errors import ApiError
from . import settings_schema
from .main_thread import MainThreadRunner

LogFunction = Callable[[str, str], None]

LOAD_TIMEOUT_S = 180.0
SETTLE_S = 0.5  # BuildVolume timers are 100-200 ms (docs/DESIGN.md, section 7).


@dataclass
class SceneRequest:
    printer_id: str
    profile: Dict[str, Optional[str]]
    stl_path: str
    overrides: Dict[str, Any] = field(default_factory = lambda: {"global": {}, "extruders": {}})


class StageTimer:
    """Logs how long each stage of a session takes (debug level, in cura.log)."""

    def __init__(self, log: LogFunction, name: str, clock: Callable[[], float] = time.monotonic) -> None:
        self._log = log
        self._name = name
        self._clock = clock
        self._start = self._last = clock()
        self._stages = []  # type: List[str]

    def mark(self, stage: str) -> None:
        now = self._clock()
        self._stages.append("{0} {1:.2f}s".format(stage, now - self._last))
        self._last = now

    def done(self) -> None:
        self._log("d", "[RemoteWebControl] {0}: {1} (total {2:.2f}s)".format(self._name, ", ".join(self._stages), self._clock() - self._start))


@contextlib.contextmanager
def stacks_session(ops: Any, runner: MainThreadRunner, printer_id: str, profile: Dict[str, Optional[str]],
                   overrides: Dict[str, Any], log: LogFunction, timer: Optional[StageTimer] = None) -> Iterator[None]:
    """Steps 1-3 and 7: the job's printer, profile and overrides are active inside the block,
    and Cura is always restored afterwards. No model is loaded."""
    timer = timer or StageTimer(log, "session")
    snapshot = runner.run(ops.prepare, printer_id)  # Nothing changed yet if this fails.
    timer.mark("prepare")
    failed = False
    try:
        runner.run(ops.activate, printer_id, profile, snapshot)
        timer.mark("activate")
        runner.run(ops.apply_overrides, overrides)
        timer.mark("overrides")
        yield
    except BaseException:
        failed = True
        raise
    finally:
        try:
            timer.mark("work")
            runner.run(ops.restore, snapshot, timeout = 120.0)
            timer.mark("restore")
        except Exception as e:
            log("e", "[RemoteWebControl] Restoring Cura's state failed: {0!r}".format(e))
            if not failed:
                raise
        finally:
            timer.done()


@contextlib.contextmanager
def materialized(ops: Any, runner: MainThreadRunner, request: SceneRequest, log: LogFunction,
                 load_timeout: float = LOAD_TIMEOUT_S, settle: float = SETTLE_S,
                 sleep: Callable[[float], None] = time.sleep) -> Iterator[None]:
    with stacks_session(ops, runner, request.printer_id, request.profile, request.overrides, log):
        sleep(settle)
        waiter = runner.run(ops.start_load, request.stl_path)
        if not waiter.wait(load_timeout):
            raise ApiError(422, "load_failed", "Cura did not finish loading the model within {0:.0f} s.".format(load_timeout))
        sleep(settle)
        yield


class SliceCancelled(Exception):
    """The job was cancelled while it was being sliced."""


SLICE_TIMEOUT_S = 3 * 3600.0
SLICE_POLL_S = 0.25
SLICE_MAX_ATTEMPTS = 3
SLICE_START_TIMEOUT_S = 120.0  # From forceSlice() until the engine reports progress.


def run_slice(ops: Any, runner: MainThreadRunner, request: SceneRequest, matrix: Any, output_path: str,
              log: LogFunction, on_progress: Callable[[float], None], is_cancelled: Callable[[], bool],
              slice_timeout: float = SLICE_TIMEOUT_S, poll: float = SLICE_POLL_S,
              clock: Callable[[], float] = time.monotonic, **kwargs: Any) -> Dict[str, Any]:
    """Materialises the job, slices it and writes the G-code exactly like "Save" in the GUI.

    Returns {"placement": ..., "result": ...}. Raises SliceCancelled or ApiError.
    """
    sleep = kwargs.get("sleep", time.sleep)
    settle = kwargs.get("settle", SETTLE_S)
    with materialized(ops, runner, request, log, **kwargs):
        placement = runner.run(ops.place, matrix)
        if not placement["fits"]:
            reasons = ", ".join(w["code"] for w in placement["warnings"]) or "not_printable"
            raise ApiError(422, "does_not_fit", "The model does not fit on the build plate ({0}).".format(reasons))
        sleep(settle)  # Scene changes stop an ongoing slice; let Cura's timers finish first.

        deadline = clock() + slice_timeout
        for attempt in range(1, SLICE_MAX_ATTEMPTS + 1):
            runner.run(ops.start_slice)
            started = clock()
            while True:
                if is_cancelled():
                    runner.run(ops.stop_slice)
                    raise SliceCancelled()
                status = runner.run(ops.slice_status)
                if status["progress"] is not None:
                    on_progress(status["progress"])
                if status["state"] == "done":
                    result = runner.run(ops.finish_slice, output_path, timeout = 300.0)
                    return {"placement": placement, "result": dict(result, fits = True)}
                if status["state"] == "error":
                    raise ApiError(422, status["error"]["code"], status["error"]["message"])
                aborted = status["state"] == "aborted"
                stalled = status["state"] == "waiting" and clock() - started > SLICE_START_TIMEOUT_S
                if aborted or stalled:
                    log("w", "[RemoteWebControl] Slice attempt {0} was {1}; retrying.".format(attempt, "aborted by Cura" if aborted else "not started"))
                    break
                if clock() > deadline:
                    runner.run(ops.stop_slice)
                    raise ApiError(504, "slice_timeout", "Slicing took longer than {0:.0f} minutes.".format(slice_timeout / 60))
                sleep(poll)
        raise ApiError(500, "slice_failed", "Cura stopped the slice {0} times; see cura.log.".format(SLICE_MAX_ATTEMPTS))


def read_settings(ops: Any, settings_ops: Any, runner: MainThreadRunner, printer_id: str,
                  profile: Dict[str, Optional[str]], overrides: Dict[str, Any], visibility: str,
                  language: Optional[str], extruder: int, log: LogFunction) -> List[Dict[str, Any]]:
    """Setting tree evaluated by Cura with the printer, profile and overrides active."""
    timer = StageTimer(log, "read_settings")
    with stacks_session(ops, runner, printer_id, profile, overrides, log, timer):
        definitions = runner.run(settings_ops.definitions, language)
        visible = runner.run(settings_ops.visible_keys, visibility)
        timer.mark("definitions")
        # Only the settings that end up in the response are evaluated (47 instead of ~640 for "basic").
        shown = settings_schema.build_tree(definitions, {}, visible, set())
        values = {}  # type: Dict[str, Dict[str, Any]]
        for _, keys in settings_schema.setting_keys_by_category(shown):
            values.update(runner.run(settings_ops.evaluate, keys, extruder))  # One category per call: Qt stays responsive.
        timer.mark("evaluate")
    return settings_schema.build_tree(definitions, values, visible, settings_schema.overridden_keys(overrides, extruder))


def settings_diff(ops: Any, settings_ops: Any, runner: MainThreadRunner, printer_id: str,
                  profile: Dict[str, Optional[str]], overrides: Dict[str, Any], scope: str, extruder: Optional[int],
                  key: str, value: Any, log: LogFunction) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Applies one override change on top of the job's overrides and returns
    (new overrides, settings whose value/enabled/validation_state changed). Cura is restored afterwards,
    so the change only lives in the job document."""
    timer = StageTimer(log, "settings_diff")
    with stacks_session(ops, runner, printer_id, profile, overrides, log, timer):
        info = runner.run(settings_ops.check_override, scope, extruder, key)
        checked = None if value is None else settings_schema.check_value(key, info["type"], info["options"], value)
        definitions = runner.run(settings_ops.definitions, None)
        groups = [keys for _, keys in settings_schema.setting_keys_by_category(definitions)]

        def state() -> Dict[str, Dict[str, Any]]:
            result = {}  # type: Dict[str, Dict[str, Any]]
            for keys in groups:
                result.update(runner.run(settings_ops.state, keys))
            return result

        before = state()
        new_overrides = settings_schema.apply_override(overrides, scope, extruder, key, checked)
        runner.run(ops.replace_overrides, new_overrides)
        after = state()
    return new_overrides, settings_schema.diff_states(before, after)


def run_placement(ops: Any, runner: MainThreadRunner, request: SceneRequest, matrix: Any, log: LogFunction,
                  auto_orient: bool = False, **kwargs: Any) -> Dict[str, Any]:
    """Materialises the job and validates the placement of `matrix` (None = keep Cura's load placement).

    With auto_orient, the orientation is computed from Cura's load placement instead (matrix is ignored).
    """
    with materialized(ops, runner, request, log, **kwargs):
        if auto_orient:
            data = runner.run(ops.orientation_input)
            orientation = ops.compute_orientation(data["vertices"], data["min_volume"])  # Heavy: stays on this thread.
            runner.run(ops.apply_orientation, orientation)
            matrix = None
        return runner.run(ops.place, matrix)
