"""Cura-side steps of a scene session (see session.py). EVERY method runs on the main thread.

APIs used here are documented in docs/DESIGN.md (sections 4-7, 13 and "Design decisions").
"""

import importlib
import json
import math
import os
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import numpy
from PyQt6.QtCore import QTimer, QUrl

from UM.Backend.Backend import BackendState
from UM.Math.Matrix import Matrix
from UM.Mesh.MeshWriter import MeshWriter
from UM.OutputDevice.OutputDevice import OutputDevice
from UM.Math.Quaternion import Quaternion
from UM.Math.Vector import Vector
from UM.Scene.Iterator.DepthFirstIterator import DepthFirstIterator
from UM.Scene.SceneNode import SceneNode
from UM.Scene.Selection import Selection

from cura.Machines.ContainerTree import ContainerTree
from cura.Settings.IntentManager import IntentManager

from . import coords
from .cura_service import CuraService
from .errors import ApiError
from .memory import release_memory


@dataclass
class _StackState:
    stack: Any
    quality: Any
    quality_changes: Any
    intent: Any
    user_values: Dict[str, Any]


@dataclass
class Snapshot:
    active_machine_id: Optional[str]
    printer_id: str
    stacks: List[_StackState] = field(default_factory = list)
    preferences: Dict[str, Any] = field(default_factory = dict)  # Original values of overridden preferences.


# Preferences of other plugins that modify a model asynchronously after it is loaded. In the API
# the client decides the orientation, and a late rotation would make the G-code differ from the
# job's matrix, so they are switched off while a job is in the scene (docs/DESIGN.md, "Scene and placement").
ORIENTATION_PLUGIN_ID = "OrientationPlugin"
ORIENTATION_MIN_VOLUME_PREFERENCE = "OrientationPlugin/min_volume"

SESSION_PREFERENCES = {
    # preference key: (owning plugin id or None for Cura itself, value during the session)
    "OrientationPlugin/do_auto_orientation": ("OrientationPlugin", False),
    # IntentManager.selectIntent() has no "no_dialog" option: with user values in the stacks (or a
    # stale MachineManager.hasUserSettings right after switching printer) and cura/active_mode == 1
    # it opens the modal "Discard or keep changes" dialog, which blocks the GUI (seen live). With
    # "always_keep" Cura takes the no-dialog path; the user containers are already empty anyway.
    "cura/choice_on_profile_override": (None, "always_keep"),
}


class LoadWaiter:
    """Set when CuraApplication.fileCompleted fires for our file."""

    def __init__(self, application: Any, path: str) -> None:
        self._application = application
        self._path = _normalize(path)
        self._event = threading.Event()
        self._connected = False

    def connect(self) -> None:
        self._application.fileCompleted.connect(self._on_file_completed)
        self._connected = True

    def disconnect(self) -> None:
        if self._connected:
            self._connected = False
            try:
                self._application.fileCompleted.disconnect(self._on_file_completed)
            except (TypeError, RuntimeError):
                pass

    def _on_file_completed(self, file_name: str) -> None:
        if _normalize(file_name) == self._path:
            self.disconnect()
            self._event.set()

    def wait(self, timeout: float) -> bool:  # Called from the scene worker thread.
        return self._event.wait(timeout)


def _normalize(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


class _RemoteWebControlOutputDevice(OutputDevice):
    """Only used as the argument of OutputDeviceManager.writeStarted, which is what triggers the
    post-processing scripts (and their thumbnail) when the GUI saves a file."""

    def __init__(self) -> None:
        super().__init__("remotewebcontrol")


# StartSliceJob.StartJobResult (C/plugins/CuraEngineBackend/StartSliceJob.py:40-48).
_START_JOB_ERRORS = {
    2: ("slice_failed", "Cura could not start slicing."),
    3: ("setting_error", "Some settings have invalid values"),
    4: ("nothing_to_slice", "Nothing to slice: the model is outside the build area or has no printable parts."),
    5: ("material_incompatible", "The material is not compatible with the printer or nozzle."),
    6: ("build_plate_error", "The build volume has errors (e.g. models outside of it)."),
    7: ("object_setting_error", "Some per-model settings have invalid values."),
    8: ("extruder_disabled", "The model is assigned to a disabled extruder."),
}


class SliceWatcher:
    """Follows CuraEngineBackend during one forceSlice(). Its slots run on the main thread; status()
    is read from there too (through the runner), the lock is only a safety net.

    States: waiting -> processing -> done | error | aborted (Cura went back to NotStarted, e.g.
    because a setting or the scene changed: needsSlicing()/_onSceneChanged() call stopSlicing()).
    """

    def __init__(self, application: Any) -> None:
        self._application = application
        self._backend = application.getBackend()
        self._lock = threading.Lock()
        self._state = "waiting"
        self._progress = None  # type: Optional[float]
        self._error = None  # type: Optional[Dict[str, str]]
        self._connected = False

    def connect(self) -> None:
        # UM Signals keep weak references to bound methods: SceneOps keeps this object alive.
        self._backend.backendStateChange.connect(self._on_state)
        self._backend.processingProgress.connect(self._on_progress)
        self._backend.backendError.connect(self._on_error)
        self._connected = True

    def disconnect(self) -> None:
        if not self._connected:
            return
        self._connected = False
        for signal, slot in ((self._backend.backendStateChange, self._on_state),
                             (self._backend.processingProgress, self._on_progress),
                             (self._backend.backendError, self._on_error)):
            try:
                signal.disconnect(slot)
            except (TypeError, ValueError, RuntimeError):
                pass

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return {"state": self._state, "progress": self._progress, "error": self._error}

    def _on_state(self, state: Any) -> None:
        with self._lock:
            if self._state in ("done", "error", "aborted"):
                return
            if state == BackendState.Processing:
                self._state = "processing"
            elif state == BackendState.Done:
                self._state = "done"
                self._progress = 1.0
            elif state == BackendState.Error:
                self._state = "error"
                if self._error is None:
                    self._error = {"code": "engine_error", "message": "CuraEngine failed while slicing; see cura.log."}
            elif state == BackendState.Disabled:
                self._state = "error"
                self._error = {"code": "slicing_disabled", "message": "Slicing is disabled in Cura (is a G-code file loaded?)."}
            elif state == BackendState.NotStarted and self._state == "processing":
                self._state = "aborted"

    def _on_progress(self, amount: Any) -> None:
        with self._lock:
            # _onProgressMessage emits the progress before setState(Processing), so accept it while waiting too.
            if self._state in ("waiting", "processing"):
                self._progress = max(0.0, min(1.0, float(amount)))

    def _on_error(self, job: Any) -> None:
        result = job.getResult() if hasattr(job, "getResult") else None
        code, message = _START_JOB_ERRORS.get(int(result) if result is not None else 2, _START_JOB_ERRORS[2])
        if code == "setting_error":
            message = "{0}: {1}.".format(message, ", ".join(self._setting_error_keys()) or "unknown")
        with self._lock:
            self._error = {"code": code, "message": message}
            self._state = "error"

    def _setting_error_keys(self) -> List[str]:
        global_stack = self._application.getGlobalContainerStack()
        if global_stack is None:
            return []
        keys = []  # type: List[str]
        for stack in [global_stack] + list(global_stack.extruderList):
            for key in stack.getErrorKeys():
                if key not in keys:
                    keys.append(key)
        return keys


class SceneOps:
    def __init__(self, application: Any, service: CuraService) -> None:
        self._application = application
        self._service = service
        self._waiter = None  # type: Optional[LoadWaiter]
        self._slice_watcher = None  # type: Optional[SliceWatcher]

    # ------------------------------------------------------------------ helpers

    def _machine_manager(self) -> Any:
        return self._application.getMachineManager()

    def _scene_root(self) -> Any:
        return self._application.getController().getScene().getRoot()

    def _model_nodes(self) -> List[Any]:
        return [node for node in DepthFirstIterator(self._scene_root())
                if node.callDecoration("isSliceable") or node.callDecoration("isBlockSlicing")]

    def _active_stacks(self) -> List[Any]:
        global_stack = self._application.getGlobalContainerStack()
        return [global_stack] + list(global_stack.extruderList)

    # ------------------------------------------------------------------ 1. prepare

    def prepare(self, printer_id: str) -> Snapshot:
        if self._model_nodes():
            raise ApiError(409, "scene_not_empty",
                           "Cura's build plate has models on it. RemoteWebControl only works with an empty build plate.")
        stack = self._service.find_printer(printer_id)
        active = self._application.getGlobalContainerStack()
        snapshot = Snapshot(active.getId() if active is not None else None, printer_id)
        for item in [stack] + self._service.extruders_of(stack):
            user = item.userChanges
            snapshot.stacks.append(_StackState(
                stack = item,
                quality = item.quality,
                quality_changes = item.qualityChanges,
                intent = item.intent,
                user_values = {key: user.getProperty(key, "value") for key in user.getAllKeys()},
            ))
        return snapshot

    # ------------------------------------------------------------------ 2. activate

    def override_preferences(self, snapshot: Snapshot) -> None:
        preferences = self._application.getPreferences()
        registry = self._application.getPluginRegistry()
        for key, (plugin_id, value) in SESSION_PREFERENCES.items():
            if plugin_id is not None and not registry.isActivePlugin(plugin_id):
                continue  # Not installed: its preference does not exist.
            current = preferences.getValue(key)
            if current is not None and current != value:
                snapshot.preferences[key] = current
                preferences.setValue(key, value)

    def activate(self, printer_id: str, profile: Dict[str, Optional[str]], snapshot: Snapshot) -> None:
        # Preference signals are queued, so set them well before loading the model.
        self.override_preferences(snapshot)
        self._application.deleteAll()  # Leftover layer data (sliceable models were refused in prepare).
        machine_manager = self._machine_manager()
        active = self._application.getGlobalContainerStack()
        if active is None or active.getId() != printer_id:
            machine_manager.setActiveMachine(printer_id)
            active = self._application.getGlobalContainerStack()
            if active is None or active.getId() != printer_id:
                raise ApiError(500, "printer_activation_failed", "Cura could not activate printer '{0}'.".format(printer_id))

        # With user values present, changing the profile could open the "keep or discard" dialog.
        for stack in self._active_stacks():
            if stack.userChanges.getAllKeys():
                stack.userChanges.clear()

        if not self._profile_mismatch(profile):
            return  # Already active: re-selecting it costs about a second of Cura recalculations.

        if profile.get("quality_changes"):
            name = profile["quality_changes"]
            groups = [g for g in ContainerTree.getInstance().getCurrentQualityChangesGroups() if g.name == name]
            if not groups:
                raise ApiError(422, "profile_not_found", "Custom profile '{0}' does not exist for this printer.".format(name))
            if not groups[0].is_available:
                raise ApiError(422, "profile_not_available", "Custom profile '{0}' is not available with the current material/nozzle.".format(name))
            machine_manager.setQualityChangesGroup(groups[0], no_dialog = True)
        else:
            quality_type = profile["quality"]
            group = ContainerTree.getInstance().getCurrentQualityGroups().get(quality_type)
            if group is None or not group.is_available:
                raise ApiError(422, "profile_not_available", "Quality '{0}' is not available for this printer.".format(quality_type))
            IntentManager.getInstance().selectIntent(profile.get("intent") or "default", quality_type)

        mismatched = self._profile_mismatch(profile)
        if mismatched:
            raise ApiError(422, "profile_not_applied",
                           "Cura did not apply the requested profile (got {0}).".format(json.dumps(mismatched)))

    def _profile_mismatch(self, profile: Dict[str, Optional[str]]) -> Dict[str, Any]:
        """Differences between the requested profile and the one active in Cura ({} = same)."""
        global_stack = self._application.getGlobalContainerStack()
        applied = self._service.active_profile(global_stack, list(global_stack.extruderList))
        expected = {"quality_changes": profile.get("quality_changes")}  # type: Dict[str, Any]
        if not profile.get("quality_changes"):
            expected.update(quality = profile["quality"], intent = profile.get("intent") or "default")
        return {key: applied.get(key) for key, value in expected.items() if applied.get(key) != value}

    # ------------------------------------------------------------------ 3. overrides

    def apply_overrides(self, overrides: Dict[str, Any]) -> None:
        global_stack = self._application.getGlobalContainerStack()
        extruders = list(global_stack.extruderList)
        definition = global_stack.definition  # The machine definition holds the whole setting tree.
        for key, value in (overrides.get("global") or {}).items():
            self._set_user_value(definition, global_stack, key, value, per_extruder = False)
        for position, values in (overrides.get("extruders") or {}).items():
            index = int(position)
            if not 0 <= index < len(extruders):
                raise ApiError(422, "invalid_extruder", "The printer has no extruder {0}.".format(position))
            for key, value in values.items():
                self._set_user_value(definition, extruders[index], key, value, per_extruder = True)

    def replace_overrides(self, overrides: Dict[str, Any]) -> None:
        """Empties the user containers of the active printer and writes these overrides instead."""
        for stack in self._active_stacks():
            stack.userChanges.clear()
        self.apply_overrides(overrides)

    @staticmethod
    def _set_user_value(definition: Any, stack: Any, key: str, value: Any, per_extruder: bool) -> None:
        if not definition.findDefinitions(key = key):
            raise ApiError(422, "unknown_setting", "Unknown setting '{0}'.".format(key))
        if per_extruder and not stack.getProperty(key, "settable_per_extruder"):
            raise ApiError(422, "not_settable_per_extruder", "Setting '{0}' cannot be set per extruder.".format(key))
        stack.userChanges.setProperty(key, "value", to_container_value(value))

    # ------------------------------------------------------------------ 4. load

    def start_load(self, path: str) -> LoadWaiter:
        # Same reset as JobSpecs.qml does when the build plate becomes empty; without it the
        # job name ({jobname} in the G-code) could keep the previous model's name.
        self._application.getPrintInformation().setBaseName("")
        waiter = LoadWaiter(self._application, path)
        waiter.connect()
        self._waiter = waiter
        self._application.readLocalFile(QUrl.fromLocalFile(path), "open_as_model", False)
        return waiter

    # ------------------------------------------------------------------ place

    def _single_model_node(self) -> Any:
        nodes = [node for node in self._model_nodes() if node.callDecoration("isSliceable")]
        if len(nodes) != 1:
            raise ApiError(422, "load_failed", "Expected one model in the scene after loading, found {0}.".format(len(nodes)))
        return nodes[0]

    @staticmethod
    def mesh_center(node: Any) -> List[float]:
        center = node.getMeshData().getCenterPosition()
        return [0.0, 0.0, 0.0] if center is None else [center.x, center.y, center.z]

    def place(self, matrix: Optional[numpy.ndarray]) -> Dict[str, Any]:
        """Applies the job matrix (None = keep Cura's load placement), drops the model on the
        plate, centres it and checks whether it fits. Returns the effective placement."""
        node = self._single_model_node()
        if node.getBoundingBox() is None:
            raise ApiError(422, "load_failed", "The loaded model has no geometry.")
        center = self.mesh_center(node)
        if matrix is not None:
            node.setTransformation(Matrix(coords.scene_matrix_from_printer(matrix, center)))

        bbox = node.getBoundingBox()
        node.translate(Vector(-bbox.center.x, -bbox.bottom, -bbox.center.z), SceneNode.TransformSpace.World)
        effective = coords.printer_matrix_from_scene(node.getWorldTransformation().getData(), center)

        volume = self._application.getBuildVolume()
        volume.checkBoundsAndUpdate(node)
        fits = not node.isOutsideBuildArea()

        warnings = []
        volume_box = volume.getBoundingBox()
        if volume_box is not None and node.collidesWithBbox(volume_box.set(bottom = -9001)):
            warnings.append({"code": "outside_build_volume", "message": "The model does not fit in the build volume."})
        if node.collidesWithAreas(volume.getDisallowedAreas()):
            warnings.append({"code": "disallowed_area", "message": "The model overlaps a disallowed area (clips, borders, brim/skirt space...)."})
        position = node.callDecoration("getActiveExtruderPosition")
        extruders = self._application.getGlobalContainerStack().extruderList
        if position is not None and int(position) < len(extruders) and not extruders[int(position)].isEnabled:
            warnings.append({"code": "extruder_disabled", "message": "The model is assigned to a disabled extruder."})
        if not fits and not warnings:
            warnings.append({"code": "not_printable", "message": "Cura marks the model as outside the build area."})
        rigid, mirrored = coords.describe_linear_part(effective)
        if not rigid:
            warnings.append({"code": "scaled", "message": "The transformation scales the model."})
        if mirrored:
            warnings.append({"code": "mirrored", "message": "The transformation mirrors the model."})

        bbox = node.getBoundingBox()
        return {
            "matrix": coords.matrix_to_list(effective),
            "fits": fits,
            "bbox": coords.scene_box_to_printer([bbox.left, bbox.bottom, bbox.back], [bbox.right, bbox.top, bbox.front]),
            "warnings": warnings,
        }

    # ------------------------------------------------------------------ auto-orientation
    #
    # Uses the Tweaker of the "Auto Orientation" plugin (OrientationPlugin, LGPLv3), when it is
    # installed, exactly like its CalculateOrientationJob does, so the result is the same as in
    # the GUI. The heavy part (compute_orientation) runs on the scene worker thread, not on the
    # main thread; the other two methods run on the main thread.

    def orientation_input(self) -> Dict[str, Any]:
        registry = self._application.getPluginRegistry()
        if not registry.isActivePlugin(ORIENTATION_PLUGIN_ID):
            raise ApiError(422, "auto_orient_unavailable",
                           "Auto-orientation needs the 'Auto Orientation' plugin (Cura Marketplace) installed and enabled.")
        node = self._single_model_node()
        return {
            "vertices": numpy.array(node.getMeshDataTransformed().getVertices(), copy = True),
            "min_volume": bool(self._application.getPreferences().getValue(ORIENTATION_MIN_VOLUME_PREFERENCE)),
        }

    @staticmethod
    def compute_orientation(vertices: numpy.ndarray, min_volume: bool) -> Dict[str, Any]:
        """Worker thread. Same call as OrientationPlugin's CalculateOrientationJob.run()."""
        try:
            tweaker = importlib.import_module(ORIENTATION_PLUGIN_ID + ".MeshTweaker")
        except ImportError as e:
            raise ApiError(422, "auto_orient_unavailable", "Could not load the Auto Orientation plugin: {0}".format(e))
        result = tweaker.Tweak(vertices, extended_mode = True, verbose = False, min_volume = min_volume)
        axis, angle = result.euler_parameter
        return {"axis": [float(a) for a in axis], "angle": float(angle)}

    def apply_orientation(self, orientation: Dict[str, Any]) -> None:
        """Same quaternion as CalculateOrientationJob; place() re-centres and drops the model afterwards."""
        axis, angle = orientation["axis"], orientation["angle"]
        rotation = Quaternion.fromAngleAxis(angle, Vector(-axis[0], -axis[1], -axis[2]))
        rotation = Quaternion.fromAngleAxis(-0.5 * math.pi, Vector(1, 0, 0)) * rotation
        self._single_model_node().rotate(rotation, SceneNode.TransformSpace.World)

    # ------------------------------------------------------------------ slice

    def start_slice(self) -> None:
        backend = self._application.getBackend()
        if self._slice_watcher is not None:
            self._slice_watcher.disconnect()
        self._slice_watcher = SliceWatcher(self._application)
        self._slice_watcher.connect()
        backend.forceSlice()  # Same as the "Slice" button.

    def slice_status(self) -> Dict[str, Any]:
        if self._slice_watcher is None:
            return {"state": "waiting", "progress": None, "error": None}
        return self._slice_watcher.status()

    def stop_slice(self) -> None:
        self._application.getBackend().stopSlicing()

    def finish_slice(self, output_path: str) -> Dict[str, Any]:
        """Collects the estimates and writes the G-code like LocalFileOutputDevice._performWrite
        (docs/DESIGN.md, section 10): writeStarted (post-processing scripts, thumbnail) and then
        GCodeWriter.write on a text stream."""
        info = self._application.getPrintInformation()
        names = list(info.materialNames or [])
        lengths = list(info.materialLengths or [])
        weights = list(info.materialWeights or [])
        costs = list(info.materialCosts or [])
        materials = []
        for index, name in enumerate(names):
            materials.append({
                "extruder": index,
                "name": name,
                "length_m": float(lengths[index]) if index < len(lengths) else 0.0,
                "weight_g": round(float(weights[index]), 2) if index < len(weights) else 0.0,
                "cost": round(float(costs[index]), 2) if index < len(costs) else 0.0,
            })
        result = {
            "print_time_s": int(info.currentPrintTime),
            "material": materials,
            "features": {label: int(duration) for label, duration in info.getFeaturePrintTimes().items() if int(duration) > 0},
            "job_name": info.jobName,
            "currency": str(self._application.getPreferences().getValue("cura/currency") or ""),
        }

        self._application.getOutputDeviceManager().writeStarted.emit(_RemoteWebControlOutputDevice())
        writer = self._application.getMeshFileHandler().getWriter("GCodeWriter")
        if writer is None:
            raise ApiError(500, "gcode_write_failed", "Cura's G-code writer is not available.")
        nodes = [self._scene_root()]  # GCodeWriter ignores the nodes: it always writes the whole plate.
        with open(output_path, "wt", encoding = "utf-8") as stream:  # Same mode as LocalFileOutputDevice.
            written = writer.write(stream, nodes, MeshWriter.OutputMode.TextMode)
        if not written:
            raise ApiError(500, "gcode_write_failed", writer.getInformation() or "Cura did not produce G-code.")
        return result

    # ------------------------------------------------------------------ 7. restore

    def restore(self, snapshot: Snapshot) -> None:
        if self._waiter is not None:
            self._waiter.disconnect()
            self._waiter = None
        if self._slice_watcher is not None:
            self._slice_watcher.disconnect()
            self._slice_watcher = None
        Selection.clear()
        self._application.deleteAll()

        for state in snapshot.stacks:
            stack = state.stack
            if stack.quality is not state.quality:
                stack.setQuality(state.quality)
            if stack.qualityChanges is not state.quality_changes:
                stack.setQualityChanges(state.quality_changes)
            if stack.intent is not state.intent:
                stack.setIntent(state.intent)
            user = stack.userChanges
            user.clear()
            for key, value in state.user_values.items():
                user.setProperty(key, "value", value)

        machine_manager = self._machine_manager()
        active = self._application.getGlobalContainerStack()
        if snapshot.active_machine_id and (active is None or active.getId() != snapshot.active_machine_id):
            machine_manager.setActiveMachine(snapshot.active_machine_id)
        else:
            # The profile containers of the active printer were swapped directly: refresh the GUI.
            machine_manager.activeQualityGroupChanged.emit()
            machine_manager.activeQualityChangesGroupChanged.emit()
            machine_manager.activeIntentChanged.emit()
        self._application.getPrintInformation().setBaseName("")
        preferences = self._application.getPreferences()
        for key, value in snapshot.preferences.items():
            preferences.setValue(key, value)
        # Later, so that Qt has destroyed the scene objects it deletes with deleteLater().
        QTimer.singleShot(3000, release_memory)


def to_container_value(value: Any) -> Any:
    """JSON value -> what the GUI would store in a user container (docs/DESIGN.md, section 13):
    text fields store strings, check boxes store bools, lists are written as text."""
    if isinstance(value, bool) or isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return repr(value) if isinstance(value, float) else str(value)
    return json.dumps(value)
