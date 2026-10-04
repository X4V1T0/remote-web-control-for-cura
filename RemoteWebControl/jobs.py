"""Declarative jobs persisted on disk. Pure Python.

A job is one build plate: one or more objects (models), each with its own matrix. Objects that
are copies of each other share the uploaded file.

Layout: <root>/<job_id>/
    job.json                      the job document
    files/<file_id>/model.stl     an uploaded file, untouched
    files/<file_id>/preview.bin   its preview mesh (mesh_format)
    files/<file_id>/load/<name>   copy of model.stl with its original name, which is what Cura loads
                                  so that the job name ({jobname} in the G-code) is the same as when
                                  loading from the GUI
    output.gcode                  the sliced G-code (and output.gcode.gz)
"""

import datetime
import json
import os
import re
import shutil
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional

from .errors import ApiError

STATES = ("created", "ready", "queued", "slicing", "done", "error")
BUSY_STATES = frozenset({"queued", "slicing"})
MAX_OBJECTS = 100

_JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_ITEM_ID_RE = re.compile(r"^[0-9a-f]{12}$")
_UNSAFE_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


@dataclass
class NewFile:
    """An uploaded STL, already validated, with its preview mesh."""
    data: bytes
    name: Optional[str]
    mesh: Dict[str, Any]
    preview: bytes


# ------------------------------------------------------------------ validation helpers

def utc_iso(timestamp: float) -> str:
    return datetime.datetime.fromtimestamp(timestamp, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc_iso(value: str) -> float:
    return datetime.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo = datetime.timezone.utc).timestamp()


def safe_filename(name: Optional[str]) -> str:
    """Original upload name made safe for any file system, always ending in .stl."""
    base = os.path.basename((name or "").replace("\\", "/"))
    base = _UNSAFE_CHARS_RE.sub("_", base).strip(" .")
    stem, ext = os.path.splitext(base)
    stem = stem[:120].strip(" .")
    if not stem or stem.upper() in {"CON", "PRN", "AUX", "NUL"} or re.fullmatch(r"(?i)(COM|LPT)\d", stem):
        stem = "model"
    return stem + ".stl"


def parse_profile(value: Any) -> Dict[str, Optional[str]]:
    """Validates the shape of a profile selection: {quality, intent, quality_changes}.

    quality_changes (a custom profile name) takes precedence, like in the GUI. Otherwise quality
    (a quality_type) is required and intent defaults to "default". Existence is checked in Cura.
    """
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            raise ApiError(400, "invalid_profile", "profile must be a JSON object.")
    if not isinstance(value, dict):
        raise ApiError(400, "invalid_profile", "profile must be a JSON object.")
    unknown = set(value) - {"quality", "intent", "quality_changes"}
    if unknown:
        raise ApiError(400, "invalid_profile", "Unknown profile fields: {0}.".format(", ".join(sorted(unknown))))

    def text(key: str) -> Optional[str]:
        item = value.get(key)
        if item is None or item == "":
            return None
        if not isinstance(item, str):
            raise ApiError(400, "invalid_profile", "profile.{0} must be a string.".format(key))
        return item

    profile = {"quality": text("quality"), "intent": text("intent") or "default", "quality_changes": text("quality_changes")}
    if profile["quality_changes"] is None and profile["quality"] is None:
        raise ApiError(400, "invalid_profile", "profile needs either 'quality' or 'quality_changes'.")
    return profile


def summary(job: Dict[str, Any]) -> Dict[str, Any]:
    keys = ("id", "name", "created_at", "updated_at", "printer_id", "state", "progress", "error")
    return dict({key: job.get(key) for key in keys}, objects = len(job.get("objects") or []))


# ------------------------------------------------------------------ objects

def new_item_id() -> str:
    return uuid.uuid4().hex[:12]


def new_object(file_id: str, name: str, mesh: Dict[str, Any], transform: Optional[List[float]] = None) -> Dict[str, Any]:
    return {"id": new_item_id(), "file": file_id, "name": name, "mesh": mesh, "transform": transform, "placement": None}


def find_object(job: Dict[str, Any], object_id: str) -> Dict[str, Any]:
    for item in job["objects"]:
        if item["id"] == object_id:
            return item
    raise ApiError(404, "object_not_found", "The job has no object '{0}'.".format(object_id))


# ------------------------------------------------------------------ store

class JobStore:
    def __init__(self, root: str, clock: Callable[[], float] = time.time) -> None:
        self._root = root
        self._clock = clock
        self._lock = threading.RLock()
        os.makedirs(root, exist_ok = True)

    @property
    def root(self) -> str:
        return self._root

    def job_dir(self, job_id: str) -> str:
        if not isinstance(job_id, str) or not _JOB_ID_RE.match(job_id):
            raise ApiError(404, "job_not_found", "Job '{0}' does not exist.".format(job_id))
        return os.path.join(self._root, job_id)

    def path(self, job_id: str, name: str) -> str:
        return os.path.join(self.job_dir(job_id), name)

    def file_dir(self, job_id: str, file_id: str) -> str:
        if not isinstance(file_id, str) or not _ITEM_ID_RE.match(file_id):
            raise ApiError(404, "object_not_found", "Unknown file '{0}'.".format(file_id))
        return os.path.join(self.job_dir(job_id), "files", file_id)

    def load_path(self, job_id: str, item: Dict[str, Any]) -> str:
        """What Cura loads for an object: its file, with the original name."""
        return os.path.join(self.file_dir(job_id, item["file"]), "load", item["name"])

    def preview_path(self, job_id: str, item: Dict[str, Any]) -> str:
        return os.path.join(self.file_dir(job_id, item["file"]), "preview.bin")

    def create(self, files: List[NewFile], printer_id: str, profile: Dict[str, Optional[str]]) -> Dict[str, Any]:
        """A new job with one object per file, not placed yet (state "created")."""
        if not files:
            raise ApiError(400, "missing_file", "The job needs at least one STL file.")
        job_id = uuid.uuid4().hex
        now = utc_iso(self._clock())
        job = {
            "id": job_id,
            "name": "",
            "created_at": now,
            "updated_at": now,
            "printer_id": printer_id,
            "profile": profile,
            "overrides": {"global": {}, "extruders": {}},
            "objects": [],
            "state": "created",
            "progress": 0.0,
            "error": None,
            "placement": None,
            "result": None,
        }
        directory = self.job_dir(job_id)
        with self._lock:
            os.makedirs(directory)
            try:
                job["objects"] = self.write_files(job_id, files)
                job["name"] = job["objects"][0]["name"]
                self._write_json(os.path.join(directory, "job.json"), job)
            except BaseException:
                shutil.rmtree(directory, ignore_errors = True)
                raise
        return job

    def write_files(self, job_id: str, files: List[NewFile]) -> List[Dict[str, Any]]:
        """Stores uploaded files in the job folder and returns a new object for each one. The job
        document is not changed: the caller adds the objects once Cura has placed them."""
        objects = []  # type: List[Dict[str, Any]]
        try:
            for upload in files:
                item = new_object(new_item_id(), safe_filename(upload.name), upload.mesh)
                directory = self.file_dir(job_id, item["file"])
                objects.append(item)
                os.makedirs(os.path.join(directory, "load"))
                self._write_bytes(os.path.join(directory, "model.stl"), upload.data)
                self._write_bytes(os.path.join(directory, "preview.bin"), upload.preview)
                self._link_or_copy(os.path.join(directory, "model.stl"), self.load_path(job_id, item))
        except BaseException:
            self.remove_files(job_id, [item["file"] for item in objects])
            raise
        return objects

    def remove_files(self, job_id: str, file_ids: Iterable[str]) -> None:
        for file_id in file_ids:
            shutil.rmtree(self.file_dir(job_id, file_id), ignore_errors = True)

    def remove_unused_files(self, job_id: str, file_ids: Iterable[str]) -> None:
        """Deletes those of `file_ids` that no object uses (a removed object, a failed addition).
        Only the caller's own files: another request may be adding files that are not in the job yet."""
        with self._lock:
            used = {item["file"] for item in self.get(job_id)["objects"]}
            self.remove_files(job_id, [file_id for file_id in set(file_ids) if file_id not in used])

    def get(self, job_id: str) -> Dict[str, Any]:
        path = os.path.join(self.job_dir(job_id), "job.json")
        with self._lock:
            try:
                with open(path, encoding = "utf-8") as f:
                    return json.load(f)
            except FileNotFoundError:
                raise ApiError(404, "job_not_found", "Job '{0}' does not exist.".format(job_id))

    def list(self) -> List[Dict[str, Any]]:
        jobs = []
        with self._lock:
            for entry in os.listdir(self._root):
                if not _JOB_ID_RE.match(entry):
                    continue
                try:
                    jobs.append(self.get(entry))
                except (ApiError, ValueError, OSError):
                    continue  # Half-written or corrupt job: ignored (cleaned up by retention).
        jobs.sort(key = lambda j: (j.get("created_at", ""), j["id"]), reverse = True)
        return jobs

    def update(self, job_id: str, change: Callable[[Dict[str, Any]], None]) -> Dict[str, Any]:
        """Atomic read-modify-write of job.json."""
        with self._lock:
            job = self.get(job_id)
            change(job)
            job["updated_at"] = utc_iso(self._clock())
            self._write_json(os.path.join(self.job_dir(job_id), "job.json"), job)
            return job

    def delete(self, job_id: str) -> None:
        with self._lock:
            job = self.get(job_id)
            if job["state"] in BUSY_STATES:
                raise ApiError(409, "job_busy", "The job is {0}; it cannot be deleted now.".format(job["state"]))
            shutil.rmtree(self.job_dir(job_id), ignore_errors = True)

    def cleanup(self, retention_days: int) -> Dict[str, List[str]]:
        """Run at start-up: deletes jobs older than the retention and marks jobs that were
        queued/slicing when Cura stopped as failed."""
        removed, interrupted = [], []
        limit = self._clock() - retention_days * 86400
        with self._lock:
            for entry in os.listdir(self._root):
                directory = os.path.join(self._root, entry)
                if not _JOB_ID_RE.match(entry) or not os.path.isdir(directory):
                    continue
                try:
                    job = self.get(entry)
                    created = parse_utc_iso(job["created_at"])
                except (ApiError, ValueError, KeyError, OSError):
                    created = os.path.getmtime(directory)  # Corrupt job: judge by age only.
                    job = None
                if created < limit:
                    shutil.rmtree(directory, ignore_errors = True)
                    removed.append(entry)
                elif job is not None and job.get("state") in BUSY_STATES:
                    self.update(entry, _mark_interrupted)
                    interrupted.append(entry)
        return {"removed": removed, "interrupted": interrupted}

    # ------------------------------------------------------------------ io

    @staticmethod
    def _write_bytes(path: str, data: bytes) -> None:
        temporary = path + ".tmp"
        with open(temporary, "wb") as f:
            f.write(data)
        os.replace(temporary, path)

    @classmethod
    def _write_json(cls, path: str, data: Dict[str, Any]) -> None:
        cls._write_bytes(path, json.dumps(data, ensure_ascii = False, indent = 2, allow_nan = False).encode("utf-8"))

    @staticmethod
    def _link_or_copy(source: str, target: str) -> None:
        try:
            os.link(source, target)
        except OSError:
            shutil.copyfile(source, target)


def _mark_interrupted(job: Dict[str, Any]) -> None:
    previous = job["state"]
    job["state"] = "error"
    job["progress"] = 0.0
    job["error"] = {"code": "interrupted", "message": "Cura was closed while this job was {0}.".format(previous)}
