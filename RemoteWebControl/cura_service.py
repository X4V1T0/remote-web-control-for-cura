"""Read-only queries against Cura: printers and profiles. MUST run on the main thread.

APIs used here are documented in docs/DESIGN.md (sections 3 and 4).
"""

from typing import Any, Dict, List, Optional

from UM.Settings.SettingFunction import SettingFunction
from UM.Util import parseBool

from cura.Machines.ContainerTree import ContainerTree
from cura.Machines.Models.IntentCategoryModel import IntentCategoryModel
from cura.Machines.Models.IntentTranslations import IntentTranslations
from cura.Settings.CuraContainerRegistry import CuraContainerRegistry
from cura.Settings.IntentManager import IntentManager
from cura.Settings.cura_empty_instance_containers import (
    EMPTY_MATERIAL_CONTAINER_ID,
    EMPTY_QUALITY_CHANGES_CONTAINER_ID,
    EMPTY_QUALITY_CONTAINER_ID,
    EMPTY_VARIANT_CONTAINER_ID,
)

from . import coords
from .errors import ApiError


class CuraService:
    def __init__(self, application: Any, plugin_version: str) -> None:
        self._application = application
        # Collected once, on the main thread, so that info() never needs Cura.
        self._info = {
            "plugin": "RemoteWebControl",
            "plugin_version": plugin_version,
            "cura_version": application.getVersion(),
            "sdk_version": str(application.getAPIVersion()),
        }

    def info(self) -> Dict[str, Any]:
        return dict(self._info)

    # ---------------------------------------------------------------- printers

    def list_printers(self) -> List[Dict[str, Any]]:
        active = self._application.getGlobalContainerStack()
        active_id = active.getId() if active is not None else None
        result = []
        for stack in self._printer_stacks():
            result.append(self._printer_to_dict(stack, stack.getId() == active_id))
        result.sort(key = lambda p: p["name"].lower())
        return result

    def _printer_stacks(self) -> List[Any]:
        stacks = CuraContainerRegistry.getInstance().findContainerStacks(type = "machine")
        return [s for s in stacks if not parseBool(s.getMetaDataEntry("hidden", False))]

    def find_printer(self, printer_id: str) -> Any:
        stacks = CuraContainerRegistry.getInstance().findContainerStacks(type = "machine", id = printer_id)
        if not stacks or parseBool(stacks[0].getMetaDataEntry("hidden", False)):
            raise ApiError(404, "printer_not_found", "Printer '{0}' does not exist.".format(printer_id))
        return stacks[0]

    @staticmethod
    def extruders_of(stack: Any) -> List[Any]:
        """Extruder stacks of a printer, sorted by position.

        Read from the registry instead of stack.extruderList because extruders are only linked to
        their global stack once that printer has been activated (ExtruderManager.addMachineExtruders).
        Only metadata and containers of these stacks are used, never evaluated settings.
        """
        extruders = CuraContainerRegistry.getInstance().findContainerStacks(type = "extruder_train", machine = stack.getId())
        extruders = sorted(extruders, key = lambda e: int(e.getMetaDataEntry("position", 0)))
        count = stack.getProperty("machine_extruder_count", "value")
        return extruders[:int(count)] if count else extruders

    def _printer_to_dict(self, stack: Any, is_active: bool) -> Dict[str, Any]:
        def prop(key: str) -> Any:
            return stack.getProperty(key, "value")

        disallowed = []
        for polygon in prop("machine_disallowed_areas") or []:
            if polygon:
                disallowed.append(coords.scene_plane_to_printer_xy(polygon))

        return {
            "id": stack.getId(),
            "name": stack.getMetaDataEntry("group_name", stack.getName()),
            "definition": stack.definition.getId(),
            "definition_name": stack.definition.getName(),
            "active": is_active,
            "build_volume": {
                "x": float(prop("machine_width")),
                "y": float(prop("machine_depth")),
                "z": float(prop("machine_height")),
            },
            "shape": prop("machine_shape"),
            "machine_center_is_zero": bool(prop("machine_center_is_zero")),
            "disallowed_areas": disallowed,
            "extruders": [self._extruder_to_dict(e) for e in self.extruders_of(stack)],
        }

    @staticmethod
    def _extruder_to_dict(extruder: Any) -> Dict[str, Any]:
        material = extruder.material
        variant = extruder.variant
        material_info = None
        if material.getId() != EMPTY_MATERIAL_CONTAINER_ID:
            material_info = {
                "name": material.getName(),
                "base_file": material.getMetaDataEntry("base_file"),
                "brand": material.getMetaDataEntry("brand"),
                "type": material.getMetaDataEntry("material"),
                "color": material.getMetaDataEntry("color_code"),
                "guid": material.getMetaDataEntry("GUID"),
            }
        return {
            "position": int(extruder.getMetaDataEntry("position", 0)),
            "id": extruder.getId(),
            "name": extruder.getName(),
            "enabled": bool(extruder.isEnabled),
            "nozzle": variant.getName() if variant.getId() != EMPTY_VARIANT_CONTAINER_ID else None,
            "material": material_info,
        }

    # ---------------------------------------------------------------- profiles

    def get_profiles(self, printer_id: str) -> Dict[str, Any]:
        stack = self.find_printer(printer_id)
        extruders = self.extruders_of(stack)
        definition_id = stack.definition.getId()

        variant_names = [e.variant.getName() for e in extruders]
        material_bases = [e.material.getMetaDataEntry("base_file") for e in extruders]
        extruder_enabled = [bool(e.isEnabled) for e in extruders]
        machine_node = ContainerTree.getInstance().machines[definition_id]
        quality_groups = machine_node.getQualityGroups(variant_names, material_bases, extruder_enabled)
        quality_changes_groups = machine_node.getQualityChangesGroups(variant_names, material_bases, extruder_enabled)

        active = self.active_profile(stack, extruders)

        qualities = []
        for group in quality_groups.values():
            qualities.append({
                "quality_type": group.quality_type,
                "name": group.name,
                "layer_height": self._layer_height(stack, group),
                "available": bool(group.is_available),
                "experimental": bool(group.is_experimental),
                "active": active["quality_changes"] is None and group.quality_type == active["quality"],
            })
        qualities.sort(key = lambda q: (q["layer_height"] is None, q["layer_height"] or 0.0))

        available_types = sorted(q["quality_type"] for q in qualities if q["available"])
        intents = self._intents(definition_id, extruders, available_types, active)

        custom = []
        for group in quality_changes_groups:
            custom.append({
                "name": group.name,
                "quality_type": group.quality_type,
                "intent_category": group.intent_category,
                "available": bool(group.is_available),
                "active": group.name == active["quality_changes"],
            })
        custom.sort(key = lambda c: c["name"].lower())

        return {
            "printer_id": stack.getId(),
            "active": active,
            "qualities": qualities,
            "intents": intents,
            "quality_changes": custom,
        }

    def resolve_profile(self, printer_id: str, profile: Optional[Dict[str, Optional[str]]]) -> Dict[str, Optional[str]]:
        """Checks that the profile exists and is available on the printer. Without a profile,
        returns the one the printer currently has in Cura."""
        profiles = self.get_profiles(printer_id)
        if profile is None:
            profile = dict(profiles["active"])
            if profile["quality_changes"] is None and profile["quality"] is None:
                raise ApiError(422, "profile_not_available", "The printer has no usable profile selected; send one explicitly.")
            return profile

        if profile["quality_changes"] is not None:
            matches = [c for c in profiles["quality_changes"] if c["name"] == profile["quality_changes"]]
            if not matches:
                raise ApiError(422, "profile_not_found", "Custom profile '{0}' does not exist for this printer.".format(profile["quality_changes"]))
            if not matches[0]["available"]:
                raise ApiError(422, "profile_not_available", "Custom profile '{0}' is not available with the current material/nozzle.".format(profile["quality_changes"]))
            return {"quality": None, "intent": None, "quality_changes": profile["quality_changes"]}

        qualities = [q for q in profiles["qualities"] if q["quality_type"] == profile["quality"]]
        if not qualities:
            raise ApiError(422, "profile_not_found", "Quality '{0}' does not exist for this printer.".format(profile["quality"]))
        if not qualities[0]["available"]:
            raise ApiError(422, "profile_not_available", "Quality '{0}' is not available with the current material/nozzle.".format(profile["quality"]))
        intents = [i for i in profiles["intents"] if i["intent_category"] == profile["intent"]]
        if not intents or profile["quality"] not in intents[0]["quality_types"]:
            raise ApiError(422, "profile_not_available", "Intent '{0}' is not available with quality '{1}'.".format(profile["intent"], profile["quality"]))
        return dict(profile)

    @staticmethod
    def active_profile(stack: Any, extruders: List[Any]) -> Dict[str, Optional[str]]:
        quality_type = None
        if stack.quality.getId() != EMPTY_QUALITY_CONTAINER_ID:
            quality_type = stack.quality.getMetaDataEntry("quality_type")

        # Same rule as IntentManager.currentIntentCategory: the non-default intent of any enabled extruder wins.
        intent_category = "default"
        for extruder in extruders:
            if not extruder.isEnabled:
                continue
            category = extruder.intent.getMetaDataEntry("intent_category", "")
            if category and category != "default":
                intent_category = category

        quality_changes = None
        if stack.qualityChanges.getId() != EMPTY_QUALITY_CHANGES_CONTAINER_ID:
            quality_changes = stack.qualityChanges.getName()

        return {"quality": quality_type, "intent": intent_category, "quality_changes": quality_changes}

    @staticmethod
    def _layer_height(stack: Any, quality_group: Any) -> Optional[float]:
        """Same as cura.Machines.Models.MachineModelUtils.fetchLayerHeight, but for any printer
        (that helper always uses the active one)."""
        value = stack.definition.getProperty("layer_height", "value")
        node = quality_group.node_for_global
        container = node.container if node is not None else None
        if container is not None and container.hasProperty("layer_height", "value"):
            value = container.getProperty("layer_height", "value")
        if isinstance(value, SettingFunction):
            value = value(stack)
        try:
            return round(float(value), 3)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _intents(definition_id: str, extruders: List[Any], available_types: List[str],
                 active: Dict[str, Optional[str]]) -> List[Dict[str, Any]]:
        """Intent categories and the quality types each one can be combined with.

        Mirrors IntentManager.getCurrentAvailableIntents, but for any printer. The "default"
        category (no intent profile) is always combinable with every available quality type.
        """
        quality_types_per_category = {"default": set(available_types)}  # type: Dict[str, set]
        manager = IntentManager.getInstance()
        for extruder in extruders:
            if not extruder.isEnabled:
                continue
            nozzle_name = extruder.variant.getMetaDataEntry("name")
            material_base = extruder.material.getMetaDataEntry("base_file")
            for metadata in manager.intentMetadatas(definition_id, nozzle_name, material_base):
                quality_type = metadata.get("quality_type")
                if quality_type in available_types:
                    category = metadata.get("intent_category", "default")
                    quality_types_per_category.setdefault(category, set()).add(quality_type)

        def weight(category: str) -> int:
            try:
                return IntentTranslations.getInstance().index(category)
            except ValueError:
                return 99

        result = []
        for category, quality_types in quality_types_per_category.items():
            result.append({
                "intent_category": category,
                "name": IntentCategoryModel.translation(category, "name", category.title()),
                "description": IntentCategoryModel.translation(category, "description", None),
                "quality_types": sorted(quality_types),
                "active": active["quality_changes"] is None and category == active["intent"],
            })
        result.sort(key = lambda i: (weight(i["intent_category"]), i["intent_category"]))
        return result
