"""Reading setting definitions and evaluated values from Cura. EVERY method runs on the main thread.

The values are always evaluated by Uranium (the settings stacks); nothing is recomputed here.
See docs/DESIGN.md, section 12 and "Problems found > Settings".
"""

import os
from typing import Any, Dict, List, Optional, Set, Tuple

from UM.i18n import i18nCatalog
from UM.Resources import Resources
from UM.Settings.PropertyEvaluationContext import PropertyEvaluationContext
from UM.Settings.SettingDefinition import SettingDefinition

from cura.Settings.SettingVisibilityPreset import SettingVisibilityPreset

from .errors import ApiError
from .settings_schema import EXCLUDED_KEYS, jsonable

_EVALUATED_PROPERTIES = ("enabled", "minimum_value", "maximum_value", "minimum_value_warning", "maximum_value_warning")


class SettingsOps:
    def __init__(self, application: Any) -> None:
        self._application = application
        self._definition_cache = {}  # type: Dict[Tuple[str, str], List[Dict[str, Any]]]

    def _global_stack(self) -> Any:
        stack = self._application.getGlobalContainerStack()
        if stack is None:
            raise ApiError(500, "no_active_printer", "Cura has no active printer.")
        return stack

    # ------------------------------------------------------------------ definitions

    def definitions(self, language: Optional[str]) -> List[Dict[str, Any]]:
        """The setting tree of the active printer, translated like SettingDefinitionsModel does."""
        definition = self._global_stack().definition
        language = language or self._application.getApplicationLanguage()
        cache_key = (definition.getId(), language)
        if cache_key not in self._definition_cache:
            catalog = self._catalog(definition, language)
            self._definition_cache[cache_key] = [self._node(d, catalog) for d in definition.findDefinitions(type = "category")
                                                 if d.key not in EXCLUDED_KEYS]
        return self._definition_cache[cache_key]

    @staticmethod
    def _catalog(definition: Any, language: str) -> Optional[i18nCatalog]:
        # Same lookup as UM.Settings.Models.SettingDefinitionsModel._update (the last loaded one wins).
        found = None
        for file_name in definition.getInheritedFiles():
            catalog = i18nCatalog(os.path.basename(file_name), language)
            if catalog.hasTranslationLoaded():
                found = catalog
        return found

    def _node(self, definition: Any, catalog: Optional[i18nCatalog]) -> Dict[str, Any]:
        def translate(suffix: str, text: str) -> str:
            if catalog is None or not text:
                return text
            return catalog.i18nc("{0} {1}".format(definition.key, suffix), text)

        node = {
            "key": definition.key,
            "label": translate("label", definition.label),
            "description": translate("description", definition.description),
            "type": definition.type,
            "children": [self._node(child, catalog) for child in definition.children if child.key not in EXCLUDED_KEYS],
        }
        if definition.type != "category":
            node["unit"] = definition.unit or ""
            options = definition.options
            node["options"] = [{"key": key, "label": translate("option " + key, label)} for key, label in options.items()] if options else None
            node["settable_per_extruder"] = bool(getattr(definition, "settable_per_extruder", False))
        return node

    # ------------------------------------------------------------------ visibility

    def visible_keys(self, preset_id: str) -> Optional[Set[str]]:
        """Keys of a visibility preset (resources/setting_visibility/*.cfg). None = all."""
        if preset_id == "all":
            return None
        for path in Resources.getAllResourcesOfType(self._application.ResourceTypes.SettingVisibilityPreset):
            preset = SettingVisibilityPreset()
            preset.loadFromFile(path)
            if preset.presetId == preset_id:
                return set(preset.settings)
        raise ApiError(500, "visibility_preset_missing", "Cura has no '{0}' visibility preset.".format(preset_id))

    # ------------------------------------------------------------------ evaluation

    def _stack_for(self, key: str, extruder_index: int) -> Tuple[Any, Optional[int]]:
        """Stack the GUI would use for a setting (C/resources/qml/Settings/SettingView.qml:276-304):
        global unless settable per extruder; then the limit_to_extruder stack, else the chosen extruder."""
        global_stack = self._global_stack()
        extruders = list(global_stack.extruderList)
        if not global_stack.getProperty(key, "settable_per_extruder") or not extruders:
            return global_stack, None
        limit = global_stack.getProperty(key, "limit_to_extruder")
        try:
            limit_index = int(limit)
        except (TypeError, ValueError):
            limit_index = -1
        if 0 <= limit_index < len(extruders):
            return extruders[limit_index], limit_index
        if extruder_index >= len(extruders):
            raise ApiError(422, "invalid_extruder", "The printer has no extruder {0}.".format(extruder_index))
        return extruders[extruder_index], extruder_index

    @staticmethod
    def _validation_state(stack: Any, key: str) -> Optional[str]:
        # Same as UM.Settings.Models.SettingPropertyProvider._getPropertyValue("validationState").
        state = stack.getProperty(key, "validationState")
        if state is None:
            definition = stack.getSettingDefinition(key)
            validator_type = SettingDefinition.getValidatorForType(definition.type) if definition else None
            if validator_type:
                state = validator_type(key)(stack)
        return jsonable(state)

    def evaluate(self, keys: List[str], extruder_index: int) -> Dict[str, Dict[str, Any]]:
        """Evaluated properties of the given settings, for the API schema."""
        result = {}
        for key in keys:
            stack, _ = self._stack_for(key, extruder_index)
            context = PropertyEvaluationContext(stack)
            context.context["evaluate_from_container_index"] = 1  # Skip the user container: the profile's value.
            values = {
                "value": jsonable(stack.getProperty(key, "value")),
                "default": jsonable(stack.getProperty(key, "value", context)),
                "validation_state": self._validation_state(stack, key),
                "limit_to_extruder": jsonable(self._limit(stack, key)),
            }
            for prop in _EVALUATED_PROPERTIES:
                values[prop] = jsonable(stack.getProperty(key, prop))
            values["enabled"] = bool(values["enabled"]) if values["enabled"] is not None else True
            result[key] = values
        return result

    @staticmethod
    def _limit(stack: Any, key: str) -> Optional[int]:
        limit = stack.getProperty(key, "limit_to_extruder")
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            return None
        return limit if limit >= 0 else None

    def state(self, keys: List[str]) -> Dict[str, Dict[str, Any]]:
        """value/enabled/validation_state of each setting on every stack where it is evaluated
        (the global stack, or each extruder for settings settable per extruder). For the diff."""
        global_stack = self._global_stack()
        extruder_count = len(global_stack.extruderList)
        result = {}
        for key in keys:
            positions = [None]  # type: List[Optional[int]]
            if global_stack.getProperty(key, "settable_per_extruder") and extruder_count:
                positions = list(range(extruder_count))
            seen = set()
            for position in positions:
                stack, used = self._stack_for(key, position or 0)
                if (id(stack), key) in seen:
                    continue  # limit_to_extruder: several tabs show the same stack.
                seen.add((id(stack), key))
                result["{0}:{1}".format("global" if used is None else used, key)] = {
                    "key": key,
                    "extruder": used,
                    "value": jsonable(stack.getProperty(key, "value")),
                    "enabled": bool(stack.getProperty(key, "enabled")),
                    "validation_state": self._validation_state(stack, key),
                }
        return result

    def check_override(self, scope: str, extruder: Optional[int], key: str) -> Dict[str, Any]:
        """Validates that key can be overridden in that scope, like the GUI would write it.
        Returns {"type", "options"} for the value check."""
        global_stack = self._global_stack()
        definitions = global_stack.definition.findDefinitions(key = key)
        if not definitions or definitions[0].type == "category":
            raise ApiError(422, "unknown_setting", "Unknown setting '{0}'.".format(key))
        definition = definitions[0]
        per_extruder = bool(global_stack.getProperty(key, "settable_per_extruder"))
        extruders = list(global_stack.extruderList)
        if scope == "extruder":
            if not per_extruder:
                raise ApiError(422, "not_settable_per_extruder", "Setting '{0}' is global: use scope \"global\".".format(key))
            if extruder is None or not 0 <= extruder < len(extruders):
                raise ApiError(422, "invalid_extruder", "The printer has no extruder {0}.".format(extruder))
        elif per_extruder and extruders:
            # The GUI writes these on the extruder stack; a global value would be shadowed by the
            # extruder's own profile containers and silently ignored.
            raise ApiError(422, "use_extruder_scope",
                           "Setting '{0}' is set per extruder: use scope \"extruder\" (with \"extruder\": 0 for single-extruder printers).".format(key))
        options = definition.options
        return {
            "type": definition.type,
            "options": [{"key": k, "label": v} for k, v in options.items()] if options else None,
        }
