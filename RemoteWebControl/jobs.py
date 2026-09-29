"""Declarative jobs persisted on disk. Pure Python.

Layout: <root>/<job_id>/
    job.json      the job document
    model.stl     the uploaded file, untouched
    preview.bin   preview mesh (mesh_format)
    load/<name>   copy of model.stl with its original name, which is what Cura loads so that the
                  job name ({jobname} in the G-code) is the same as when loading from the GUI
    output.gcode  (phase 3)
"""

import datetime
import json
import os
import re
import shutil
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

from .errors import ApiError

STATES = ("created", "ready", "queued", "slicing", "done", "error")
BUSY_STATES = frozenset({"queued", "slicing"})

_JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_UNSAFE_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


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
    return {key: job.get(key) for key in keys}


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

    def load_path(self, job: Dict[str, Any]) -> str:
        return os.path.join(self.job_dir(job["id"]), "load", job["name"])

    def create(self, stl_data: bytes, original_name: Optional[str], printer_id: str,
               profile: Dict[str, Optional[str]], mesh_info: Dict[str, Any], preview: bytes) -> Dict[str, Any]:
        job_id = uuid.uuid4().hex
        now = utc_iso(self._clock())
        job = {
            "id": job_id,
            "name": safe_filename(original_name),
            "created_at": now,
            "updated_at": now,
            "printer_id": printer_id,
            "profile": profile,
            "overrides": {"global": {}, "extruders": {}},
            "transform": None,
            "state": "created",
            "progress": 0.0,
            "error": None,
            "mesh": mesh_info,
            "placement": None,
            "result": None,
        }
        directory = self.job_dir(job_id)
        with self._lock:
            os.makedirs(os.path.join(directory, "load"))
            try:
                self._write_bytes(os.path.join(directory, "model.stl"), stl_data)
                self._write_bytes(os.path.join(directory, "preview.bin"), preview)
                self._link_or_copy(os.path.join(directory, "model.stl"), self.load_path(job))
                self._write_json(os.path.join(directory, "job.json"), job)
            except BaseException:
                shutil.rmtree(directory, ignore_errors = True)
                raise
        return job

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
