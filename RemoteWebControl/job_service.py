"""Job use cases: create, list, get, delete, preview mesh, transform, settings, slice, cancel, G-code. Pure Python.

Cura is reached through injected callables:
    resolve_profile(printer_id, profile_or_None) -> profile                  (main thread)
    place(SceneRequest, matrix_or_None, auto_orient) -> placement dict        (scene worker)
    read_settings(printer_id, profile, overrides, visibility, language, extruder) -> tree   (scene worker)
    settings_diff(printer_id, profile, overrides, scope, extruder, key, value)
        -> (new overrides, changes)                                          (scene worker)
    slice(SceneRequest, matrix, output_path, on_progress, is_cancelled)
        -> {"placement": ..., "result": ...}                                  (scene worker)
"""

import gzip
import os
import shutil
import threading
import time
import traceback
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import coords, decimate, mesh_format, settings_schema
from .errors import ApiError
from .jobs import BUSY_STATES, JobStore, parse_profile, summary
from .main_thread import MainThreadRunner
from .multipart import parse_form_data
from .scene_worker import PRIORITY_SLICE, SceneWorker
from .session import SceneRequest, SliceCancelled
from .stl import read_stl

MAX_PREVIEW_TRIS = 800000
GCODE = "output.gcode"
GCODE_GZ = "output.gcode.gz"
PROGRESS_WRITE_INTERVAL_S = 1.0

LogFunction = Callable[[str, str], None]


class JobService:
    def __init__(self, store: JobStore, worker: SceneWorker, runner: MainThreadRunner,
                 resolve_profile: Callable[[str, Optional[Dict[str, Optional[str]]]], Dict[str, Optional[str]]],
                 place: Callable[[SceneRequest, Any, bool], Dict[str, Any]],
                 slice: Optional[Callable[..., Dict[str, Any]]] = None,
                 read_settings: Optional[Callable[..., List[Dict[str, Any]]]] = None,
                 settings_diff: Optional[Callable[..., Tuple[Dict[str, Any], List[Dict[str, Any]]]]] = None,
                 log: LogFunction = lambda level, message: None,
                 max_preview_tris: int = MAX_PREVIEW_TRIS) -> None:
        self._store = store
        self._worker = worker
        self._runner = runner
        self._resolve_profile = resolve_profile
        self._place = place
        self._slice = slice
        self._read_settings = read_settings
        self._settings_diff = settings_diff
        self._log = log
        self._max_preview_tris = max_preview_tris
        self._cancel_requests = set()  # type: set
        self._cancel_lock = threading.Lock()

    # ------------------------------------------------------------------ create

    def create(self, content_type: str, body: bytes) -> Dict[str, Any]:
        form = parse_form_data(content_type, body)
        upload = form.get("file")
        if upload is None or not upload.data:
            raise ApiError(400, "missing_file", "The form needs a 'file' field with an STL file.")
        printer_field = form.get("printer_id")
        printer_id = printer_field.text().strip() if printer_field is not None else ""
        if not printer_id:
            raise ApiError(400, "missing_printer_id", "The form needs a 'printer_id' field.")
        profile_field = form.get("profile")
        profile = parse_profile(profile_field.text()) if profile_field is not None and profile_field.data.strip() else None
        auto_orient = _parse_bool(form["auto_orient"].text(), "auto_orient") if "auto_orient" in form else False

        triangles = read_stl(upload.data)
        preview = decimate.decimate(triangles, self._max_preview_tris)
        decimated = len(preview) < len(triangles)
        mesh_info = {
            "triangles": int(len(triangles)),
            "preview_triangles": int(len(preview)),
            "decimated": decimated,
            "bbox": coords.points_bbox(triangles),
        }

        profile = self._runner.run(self._resolve_profile, printer_id, profile)
        job = self._store.create(upload.data, upload.filename, printer_id, profile, mesh_info,
                                 mesh_format.encode(preview, decimated))
        try:
            placement = self._worker.call(self._place, self._scene_request(job), None, auto_orient)
        except BaseException:
            self._store.delete(job["id"])
            raise
        return self._store.update(job["id"], lambda j: _apply_placement(j, placement))

    # ------------------------------------------------------------------ queries

    def list(self) -> List[Dict[str, Any]]:
        return [summary(job) for job in self._store.list()]

    def get(self, job_id: str) -> Dict[str, Any]:
        return self._store.get(job_id)

    def delete(self, job_id: str) -> None:
        self._store.delete(job_id)

    def mesh(self, job_id: str) -> bytes:
        self._store.get(job_id)  # 404 if missing.
        with open(self._store.path(job_id, "preview.bin"), "rb") as f:
            return f.read()

    # ------------------------------------------------------------------ transform

    def set_transform(self, job_id: str, payload: Any) -> Dict[str, Any]:
        if not isinstance(payload, dict) or "matrix" not in payload:
            raise ApiError(400, "invalid_matrix", "Body must be {\"matrix\": [16 numbers]}.")
        return self._replace(job_id, coords.matrix_from_list(payload["matrix"]), auto_orient = False)

    def auto_orient(self, job_id: str) -> Dict[str, Any]:
        return self._replace(job_id, None, auto_orient = True)

    def _replace(self, job_id: str, matrix: Any, auto_orient: bool) -> Dict[str, Any]:
        job = self._store.get(job_id)
        _ensure_not_busy(job)
        placement = self._worker.call(self._place, self._scene_request(job), matrix, auto_orient)

        def change(j: Dict[str, Any]) -> None:
            _ensure_not_busy(j)  # Could have been queued while we were placing.
            _apply_placement(j, placement)

        self._store.update(job_id, change)
        self._remove_output(job_id)
        return placement

    # ------------------------------------------------------------------ slice

    def slice(self, job_id: str) -> Dict[str, Any]:
        """Queues the job. The HTTP request returns at once (202); poll GET /api/jobs/{id}."""
        if self._slice is None:
            raise ApiError(501, "not_implemented", "Slicing is not available.")

        def change(job: Dict[str, Any]) -> None:
            _ensure_not_busy(job)
            if job.get("transform") is None:
                raise ApiError(409, "job_not_ready", "The job has no placement yet.")
            job.update(state = "queued", progress = 0.0, error = None, result = None)

        job = self._store.update(job_id, change)
        self._remove_output(job_id)
        self._worker.submit(self._run_slice, job_id, priority = PRIORITY_SLICE)
        return job

    def cancel(self, job_id: str) -> Dict[str, Any]:
        job = self._store.get(job_id)
        if job["state"] == "queued":
            def unqueue(j: Dict[str, Any]) -> None:
                if j["state"] == "queued":  # The worker skips jobs that are no longer queued.
                    j.update(state = "ready", progress = 0.0)
            return self._store.update(job_id, unqueue)
        if job["state"] == "slicing":
            with self._cancel_lock:
                self._cancel_requests.add(job_id)
            return job  # Becomes "ready" as soon as Cura stops the slice.
        raise ApiError(409, "job_not_running", "The job is {0}; only queued or slicing jobs can be cancelled.".format(job["state"]))

    def _is_cancelled(self, job_id: str) -> bool:
        with self._cancel_lock:
            return job_id in self._cancel_requests

    def _run_slice(self, job_id: str) -> None:
        """Scene worker task."""
        try:
            job = self._store.get(job_id)
        except ApiError:
            return  # Deleted meanwhile (only possible if it was cancelled first).
        if job["state"] != "queued":
            return  # Cancelled while queued.
        job = self._store.update(job_id, lambda j: j.update(state = "slicing", progress = 0.0))

        progress_writer = _ProgressWriter(self._store, job_id)
        partial = self._store.path(job_id, GCODE + ".part")
        try:
            matrix = coords.matrix_from_list(job["transform"])
            outcome = self._slice(self._scene_request(job), matrix, partial, progress_writer, lambda: self._is_cancelled(job_id))
            os.replace(partial, self._store.path(job_id, GCODE))
            self._write_gzip_copy(job_id)
        except SliceCancelled:
            self._store.update(job_id, lambda j: j.update(state = "ready", progress = 0.0, error = None))
        except ApiError as e:
            self._store.update(job_id, lambda j: j.update(state = "error", progress = 0.0, error = {"code": e.code, "message": e.message}))
        except Exception:
            self._log("e", "[RemoteWebControl] Slicing job {0} failed:\n{1}".format(job_id, traceback.format_exc()))
            self._store.update(job_id, lambda j: j.update(state = "error", progress = 0.0,
                                                          error = {"code": "internal_error", "message": "Internal error; see cura.log."}))
        else:
            gcode_size = os.path.getsize(self._store.path(job_id, GCODE))

            def done(j: Dict[str, Any]) -> None:
                j["placement"] = {key: outcome["placement"][key] for key in ("fits", "bbox", "warnings")}
                j["result"] = dict(outcome["result"], gcode_bytes = gcode_size)
                j.update(state = "done", progress = 1.0, error = None)
            self._store.update(job_id, done)
        finally:
            with self._cancel_lock:
                self._cancel_requests.discard(job_id)
            if os.path.exists(partial):
                os.remove(partial)

    def _write_gzip_copy(self, job_id: str) -> None:
        """Served to clients that accept gzip: G-code compresses about 5:1."""
        source = self._store.path(job_id, GCODE)
        target = self._store.path(job_id, GCODE_GZ)
        with open(source, "rb") as src, gzip.open(target + ".part", "wb", compresslevel = 6) as dst:
            shutil.copyfileobj(src, dst, 1024 * 1024)
        os.replace(target + ".part", target)

    # ------------------------------------------------------------------ settings

    def printer_settings(self, printer_id: str, query: Dict[str, Optional[str]]) -> Dict[str, Any]:
        """Settings of a printer with the profile it currently has in Cura (no overrides)."""
        visibility, language, extruder = _settings_query(query)
        profile = self._runner.run(self._resolve_profile, printer_id, None)
        tree = self._worker.call(self._read_settings, printer_id, profile, {"global": {}, "extruders": {}},
                                 visibility, language, extruder)
        return {"printer_id": printer_id, "profile": profile, "visibility": visibility, "extruder": extruder, "categories": tree}

    def job_settings(self, job_id: str, query: Dict[str, Optional[str]]) -> Dict[str, Any]:
        """Settings with the job's printer, profile and overrides."""
        visibility, language, extruder = _settings_query(query)
        job = self._store.get(job_id)
        tree = self._worker.call(self._read_settings, job["printer_id"], job["profile"], job["overrides"],
                                 visibility, language, extruder)
        return {"job_id": job_id, "printer_id": job["printer_id"], "profile": job["profile"], "overrides": job["overrides"],
                "visibility": visibility, "extruder": extruder, "categories": tree}

    def patch_settings(self, job_id: str, payload: Any) -> Dict[str, Any]:
        """Sets (or removes, with null) one override and returns what Cura recalculated."""
        scope, extruder, key, value = settings_schema.parse_patch(payload)
        job = self._store.get(job_id)
        _ensure_not_busy(job)
        new_overrides, changes = self._worker.call(self._settings_diff, job["printer_id"], job["profile"], job["overrides"],
                                                   scope, extruder, key, value)

        def change(j: Dict[str, Any]) -> None:
            _ensure_not_busy(j)
            j["overrides"] = new_overrides
            if j["state"] in ("done", "error"):
                j.update(state = "ready", progress = 0.0, error = None, result = None)

        self._store.update(job_id, change)
        self._remove_output(job_id)
        return {"overrides": new_overrides, "changed": changes}

    # ------------------------------------------------------------------ G-code

    def gcode(self, job_id: str) -> Tuple[str, Optional[str], str]:
        """Returns (path, gzip path or None, download file name)."""
        job = self._store.get(job_id)
        path = self._store.path(job_id, GCODE)
        if job["state"] != "done" or not os.path.exists(path):
            raise ApiError(409, "gcode_not_ready", "The job is {0}; the G-code is available when it is done.".format(job["state"]))
        gz_path = self._store.path(job_id, GCODE_GZ)
        name = (job.get("result") or {}).get("job_name") or os.path.splitext(job["name"])[0]
        return path, gz_path if os.path.exists(gz_path) else None, name + ".gcode"

    # ------------------------------------------------------------------ helpers

    def _scene_request(self, job: Dict[str, Any]) -> SceneRequest:
        return SceneRequest(job["printer_id"], job["profile"], self._store.load_path(job), job["overrides"])

    def _remove_output(self, job_id: str) -> None:
        for name in (GCODE, GCODE_GZ):
            try:
                os.remove(self._store.path(job_id, name))
            except FileNotFoundError:
                pass


class _ProgressWriter:
    """Persists slicing progress at most once per second (and always the first time)."""

    def __init__(self, store: JobStore, job_id: str, clock: Callable[[], float] = time.monotonic) -> None:
        self._store = store
        self._job_id = job_id
        self._clock = clock
        self._last_write = None  # type: Optional[float]
        self._last_value = -1.0

    def __call__(self, progress: float) -> None:
        progress = round(float(progress), 3)
        now = self._clock()
        if progress == self._last_value:
            return
        if self._last_write is not None and now - self._last_write < PROGRESS_WRITE_INTERVAL_S:
            return
        self._last_write, self._last_value = now, progress
        self._store.update(self._job_id, lambda j: j.update(progress = progress) if j["state"] == "slicing" else None)


def _settings_query(query: Dict[str, Optional[str]]) -> Tuple[str, Optional[str], int]:
    return (settings_schema.parse_visibility(query.get("visibility")),
            settings_schema.parse_language(query.get("lang")),
            settings_schema.parse_extruder(query.get("extruder")))


def _parse_bool(value: str, name: str) -> bool:
    text = value.strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("", "0", "false", "no", "off"):
        return False
    raise ApiError(400, "invalid_form", "'{0}' must be true or false.".format(name))


def _ensure_not_busy(job: Dict[str, Any]) -> None:
    if job["state"] in BUSY_STATES:
        raise ApiError(409, "job_busy", "The job is {0}; wait until it finishes.".format(job["state"]))


def _apply_placement(job: Dict[str, Any], placement: Dict[str, Any]) -> None:
    """A new placement makes any previous slice result stale."""
    job["transform"] = placement["matrix"]
    job["placement"] = {key: placement[key] for key in ("fits", "bbox", "warnings")}
    job["state"] = "ready"
    job["progress"] = 0.0
    job["error"] = None
    job["result"] = None
