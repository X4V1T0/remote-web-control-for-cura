"""Setting tree, visibility, value validation and diff. Pure Python.

The Cura side (settings_ops.py) produces plain data:
    definitions: list of category nodes
        {"key", "label", "description", "type", "unit", "options": [{"key", "label"}] | None,
         "settable_per_extruder", "children": [...]}
    values: {key: {"value", "default", "enabled", "minimum_value", "maximum_value",
                   "minimum_value_warning", "maximum_value_warning", "validation_state",
                   "limit_to_extruder"}}
and this module shapes the API responses from it.
"""

import copy
import math
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from .errors import ApiError

VISIBILITY_PRESETS = ("basic", "advanced", "expert", "all")

# Same exclusions as the GUI's setting panel (C/resources/qml/Settings/SettingView.qml:210).
EXCLUDED_KEYS = frozenset({
    "machine_settings", "command_line_settings",
    "infill_mesh", "infill_mesh_order", "cutting_mesh", "support_mesh", "anti_overhang_mesh",
})

VALUE_PROPERTIES = ("value", "default", "enabled", "minimum_value", "maximum_value",
                    "minimum_value_warning", "maximum_value_warning", "validation_state", "limit_to_extruder")

DIFF_PROPERTIES = ("value", "enabled", "validation_state")


# ------------------------------------------------------------------ query parameters

def parse_visibility(value: Optional[str]) -> str:
    visibility = (value or "basic").strip().lower()
    if visibility not in VISIBILITY_PRESETS:
        raise ApiError(400, "invalid_visibility", "visibility must be one of: {0}.".format(", ".join(VISIBILITY_PRESETS)))
    return visibility


def parse_extruder(value: Optional[str]) -> int:
    if value is None or value == "":
        return 0
    try:
        index = int(value)
    except ValueError:
        raise ApiError(400, "invalid_extruder", "extruder must be an integer.")
    if index < 0:
        raise ApiError(400, "invalid_extruder", "extruder must be >= 0.")
    return index


def parse_language(value: Optional[str]) -> Optional[str]:
    """None = Cura's language. Accepts es_ES, es-ES, es."""
    if not value:
        return None
    text = value.strip().replace("-", "_")
    parts = text.split("_")
    if len(parts) == 1 and len(parts[0]) == 2 and parts[0].isalpha():
        return parts[0].lower()
    if len(parts) == 2 and len(parts[0]) == 2 and len(parts[1]) == 2 and text.replace("_", "").isalpha():
        return "{0}_{1}".format(parts[0].lower(), parts[1].upper())
    raise ApiError(400, "invalid_language", "lang must look like es_ES.")


# ------------------------------------------------------------------ tree

def iter_settings(nodes: Iterable[Dict[str, Any]]) -> Iterable[Dict[str, Any]]:
    """All non-category nodes, depth first."""
    for node in nodes:
        if node.get("type") != "category":
            yield node
        yield from iter_settings(node.get("children") or [])


def setting_keys_by_category(definitions: List[Dict[str, Any]]) -> List[Tuple[str, List[str]]]:
    return [(category["key"], [s["key"] for s in iter_settings(category.get("children") or [])]) for category in definitions]


def build_tree(definitions: List[Dict[str, Any]], values: Dict[str, Dict[str, Any]],
               visible_keys: Optional[Set[str]], overridden_keys: Set[str]) -> List[Dict[str, Any]]:
    """Merges definitions and evaluated values. With visible_keys (a preset), nodes that are not
    visible and have no visible descendant are dropped; the rest carry "visible"."""

    def convert(node: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        children = [c for c in (convert(child) for child in node.get("children") or []) if c is not None]
        visible = visible_keys is None or node["key"] in visible_keys
        if not visible and not children:
            return None
        result = {k: node.get(k) for k in ("key", "label", "description", "type")}
        if node.get("type") != "category":
            result["unit"] = node.get("unit") or ""
            result["options"] = node.get("options")
            result["settable_per_extruder"] = bool(node.get("settable_per_extruder"))
            evaluated = values.get(node["key"], {})
            for prop in VALUE_PROPERTIES:
                result[prop] = evaluated.get(prop)
            result["overridden"] = node["key"] in overridden_keys
        result["visible"] = visible
        result["children"] = children
        return result

    return [c for c in (convert(category) for category in definitions) if c is not None]


# ------------------------------------------------------------------ JSON-safe values

def jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if hasattr(value, "value") and not callable(value.value):  # Enums such as ValidatorState.
        return jsonable(value.value)
    try:
        number = float(value)  # numpy scalars
        return int(number) if number.is_integer() and "int" in type(value).__name__ else number
    except (TypeError, ValueError):
        return str(value)


# ------------------------------------------------------------------ overrides

def check_value(key: str, setting_type: str, options: Optional[List[Dict[str, str]]], value: Any) -> Any:
    """Validates the JSON type of a new override. Range checks are Cura's job (validation_state)."""
    if isinstance(value, str) and value.strip().startswith("="):
        raise ApiError(422, "formulas_not_allowed", "Setting '{0}': formulas (values starting with '=') are not allowed.".format(key))
    is_number = isinstance(value, (int, float)) and not isinstance(value, bool)
    if setting_type == "float":
        if not is_number or not math.isfinite(float(value)):
            raise ApiError(422, "invalid_value", "Setting '{0}' needs a number.".format(key))
    elif setting_type == "int":
        if not is_number or float(value) != int(value):
            raise ApiError(422, "invalid_value", "Setting '{0}' needs an integer.".format(key))
        value = int(value)
    elif setting_type == "bool":
        if not isinstance(value, bool):
            raise ApiError(422, "invalid_value", "Setting '{0}' needs true or false.".format(key))
    elif setting_type == "enum":
        allowed = [o["key"] for o in options or []]
        if value not in allowed:
            raise ApiError(422, "invalid_value", "Setting '{0}' must be one of: {1}.".format(key, ", ".join(allowed)))
    elif setting_type in ("extruder", "optional_extruder"):
        try:
            index = int(value)
        except (TypeError, ValueError):
            raise ApiError(422, "invalid_value", "Setting '{0}' needs an extruder number.".format(key))
        if isinstance(value, bool) or index < (-1 if setting_type == "optional_extruder" else 0):
            raise ApiError(422, "invalid_value", "Setting '{0}' needs an extruder number.".format(key))
        value = str(index)
    elif setting_type == "str":
        if not isinstance(value, str):
            raise ApiError(422, "invalid_value", "Setting '{0}' needs text.".format(key))
    elif setting_type in ("[int]", "polygon", "polygons"):
        if not isinstance(value, list):
            raise ApiError(422, "invalid_value", "Setting '{0}' needs a list.".format(key))
    return value


def apply_override(overrides: Dict[str, Any], scope: str, extruder: Optional[int], key: str, value: Any) -> Dict[str, Any]:
    """Returns a copy of the job overrides with the change applied (value None removes it)."""
    result = copy.deepcopy(overrides) if overrides else {"global": {}, "extruders": {}}
    result.setdefault("global", {})
    result.setdefault("extruders", {})
    if scope == "global":
        target = result["global"]
    else:
        target = result["extruders"].setdefault(str(extruder), {})
    if value is None:
        target.pop(key, None)
    else:
        target[key] = value
    result["extruders"] = {k: v for k, v in result["extruders"].items() if v}
    return result


def parse_patch(payload: Any) -> Tuple[str, Optional[int], str, Any]:
    if not isinstance(payload, dict):
        raise ApiError(400, "invalid_request", "Body must be {\"scope\", \"extruder\"?, \"key\", \"value\"}.")
    scope = payload.get("scope")
    if scope not in ("global", "extruder"):
        raise ApiError(400, "invalid_request", "scope must be \"global\" or \"extruder\".")
    extruder = None
    if scope == "extruder":
        extruder = payload.get("extruder", 0)
        if isinstance(extruder, bool) or not isinstance(extruder, int) or extruder < 0:
            raise ApiError(400, "invalid_request", "extruder must be an integer >= 0.")
    key = payload.get("key")
    if not isinstance(key, str) or not key:
        raise ApiError(400, "invalid_request", "key must be a setting key.")
    if "value" not in payload:
        raise ApiError(400, "invalid_request", "value is required (null removes the override).")
    return scope, extruder, key, payload["value"]


def overridden_keys(overrides: Dict[str, Any], extruder: int) -> Set[str]:
    keys = set((overrides or {}).get("global", {}))
    keys |= set(((overrides or {}).get("extruders", {}) or {}).get(str(extruder), {}))
    return keys


# ------------------------------------------------------------------ diff

def diff_states(before: Dict[str, Dict[str, Any]], after: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """before/after: {state_id: {"key", "extruder", "value", "enabled", "validation_state"}}.

    state_id identifies where the setting was evaluated ("global:key" or "<n>:key")."""
    changes = []
    for state_id, new in after.items():
        old = before.get(state_id)
        if old is not None and all(old.get(p) == new.get(p) for p in DIFF_PROPERTIES):
            continue
        entry = {"key": new["key"], "extruder": new.get("extruder")}
        entry.update({p: new.get(p) for p in DIFF_PROPERTIES})
        entry["before"] = {p: old.get(p) for p in DIFF_PROPERTIES} if old is not None else None
        changes.append(entry)
    changes.sort(key = lambda c: (c["key"], -1 if c["extruder"] is None else c["extruder"]))
    return changes
