# Remote Web Control for Cura — Design and technical notes

For anyone who wants to understand or change the plugin. It lists the internal Cura and Uranium APIs it relies on (with file and line), why things are done the way they are, and the problems found while testing it against a real Cura.

Everything was researched on the source code of Cura and Uranium at tag `5.13.0`, and verified with Cura 5.13.0 on Windows and in Docker (Linux):

| Repo | Tag | Commit |
|---|---|---|
| `Ultimaker/Cura` | `5.13.0` | `1fb8a76` |
| `Ultimaker/Uranium` | `5.13.0` | `0ae6c37` |

To follow the references, clone both repositories at that tag into a `vendor-src/` folder (not included in this repository). Short paths: `C/` = `vendor-src/Cura/`, `U/` = `vendor-src/Uranium/`.

**General principle**: the G-code must be the same one Cura's GUI would produce with the same printer, profile, settings and orientation. That is why the plugin runs inside Cura and uses its settings stack, scene, backend and G-code writer, instead of calling CuraEngine on its own. Cura's scene is treated as an ephemeral resource: it is rebuilt from the job document for every operation, and Cura is **always** left as it was.

---

## 0. Runtime environment

- **Plugin SDK**: `CuraSDKVersion = "8.12.0"` — `C/cura/ApplicationMetadata.py:17`.
  - Compatibility rule: same *major*, and the plugin's *minor* ≤ the app's *minor* — `U/UM/PluginRegistry.py:458-465`.
  - `plugin.json` must have `"version"` and either `"api"` or `"supported_sdk_versions"` — `U/UM/PluginRegistry.py:860-877`.
  - → `"supported_sdk_versions": ["8.12.0"]`.
- **Embedded Python**: 3.12 (`python312.dll`), a frozen PyInstaller build.
  - **The stdlib is NOT complete**: only the modules Cura imports are there. The module table embedded in `UltiMaker-Cura.exe` shows `http.server`, `socketserver`, `concurrent.futures`, `email.parser`, `email.message`, `email.policy`, `gzip`, `secrets`, `hmac`, `mimetypes`, `tempfile` and `urllib.parse`. As a negative control, `tkinter`, `xmlrpc.server` and `wsgiref.simple_server` do **not** appear, so the check is meaningful. The modules the plugin uses were later confirmed to work inside Cura.
  - Bundled third-party packages: `numpy`, `scipy`, `trimesh`, `shapely`, `numpy-stl` (`stl`), `pyclipper`.
  - `multipart/form-data` is not parsed with `cgi` (deprecated in 3.12, removed in 3.13) but with a custom parser (`multipart.py`), because `email.parser` copies the body several times and uploads can be hundreds of MB.
- **User plugin installation**: `%APPDATA%\cura\5.13\plugins\<PluginId>\<PluginId>\`.
- **Data folder**: `Resources.getDataStoragePath()` — `U/UM/Resources.py:281`. On Windows it is the same as the configuration folder (`%APPDATA%\cura\5.13`) — `U/UM/Resources.py:493-500`. Jobs live in `<data>/RemoteWebControl/jobs/<id>/`.

## 1. Structure of an Extension plugin

- `plugin.json` + `__init__.py` with `getMetaData()` and `register(app)`, which returns `{"extension": Obj()}`. Example: `C/plugins/PostProcessingPlugin/plugin.json` and `C/plugins/PostProcessingPlugin/__init__.py`.
- The class inherits from `UM.Extension.Extension` — `U/UM/Extension.py:9`.
  - Menu: `setMenuName(name)` (`:30`) and `addMenuItem(name, callable)` (`:21`). Usage example in `C/plugins/PostProcessingPlugin/PostProcessingPlugin.py:35-36`. The plugin adds "Remote Web Control → Pair a phone / Show URL and token", in Cura's language.
- Application shutdown: the `Application.applicationShuttingDown` signal (`U/UM/Application.py:309`), emitted in `QtApplication.windowClosed` (`U/UM/Qt/QtApplication.py:529`) **before** `getBackend().close()`. The HTTP server is stopped there.
- Preferences: `app.getPreferences().addPreference(key, default)` / `getValue` / `setValue` — `U/UM/Preferences.py:49,96,105`. They are stored in `cura.cfg`.
- Logger: `UM.Logger.Logger.log("i"|"w"|"e"|"d", msg)`; the log goes to `%APPDATA%\cura\5.13\cura.log`.

## 2. Main thread

- `Application.callLater(func, *args, **kwargs)` — `U/UM/Application.py:489`. It creates a `CallFunctionEvent` and hands it to `functionEvent`, which in Qt does `QCoreApplication.postEvent(...)` — `U/UM/Qt/QtApplication.py:503-513`. `postEvent` is thread-safe, so `callLater` can be called from the HTTP thread. It returns no result, so the plugin wraps it in a `concurrent.futures.Future` that the HTTP thread waits on with a timeout (`main_thread.py`).
- There is also the `call_on_qt_thread` decorator, used in `CuraEngineBackend._createSnapshot`. The plugin does not use it: it blocks with no timeout.

## 3. Printers and extruders

- Listing: `CuraContainerRegistry.getInstance().findContainerStacks(type="machine")`, filtering out those with `hidden` in their metadata. This is what the GUI does in `C/cura/Machines/Models/GlobalStacksModel.py:132-150`. The visible name is `stack.getMetaDataEntry("group_name", stack.getName())` (`:157`).
- Extruders of a global stack: `global_stack.extruderList` — `C/cura/Settings/GlobalStack.py:63`. Each has `.isEnabled` (`C/cura/Settings/ExtruderStack.py:62`), `getMetaDataEntry("position")`, `.material` and `.variant` (`C/cura/Settings/CuraContainerStack.py:147,164`).
- Build volume: `getProperty("machine_width" | "machine_depth" | "machine_height" | "machine_shape" | "machine_center_is_zero" | "machine_disallowed_areas", "value")`.
- ⚠️ Extruders are **only linked to their global stack when that printer is activated**: `ExtruderManager.addMachineExtruders` calls `setNextStack` (`C/cura/Settings/ExtruderManager.py:390-414`), and it is invoked by `setActiveMachine` (`MachineManager.py:374`). `ExtruderStack.deserialize` does not do it (`C/cura/Settings/ExtruderStack.py:172-175`). On a printer that is not active, `extruderList` can be empty and `extruder.getProperty` raises `NoGlobalStackError`. To list without activating: `findContainerStacks(type="extruder_train", machine=<id>)`, reading only metadata and containers (`.material`, `.variant`, `.intent`, `isEnabled`), never evaluated settings.
- Activating a printer: `MachineManager.setActiveMachine(stack_id)` — `C/cura/Settings/MachineManager.py:346`. It is synchronous and emits `globalContainerChanged`.
- Active printer: `app.getGlobalContainerStack()` (`C/cura/CuraApplication.py:746`) or `MachineManager.activeMachine`.

## 4. Profiles: quality, intent and quality_changes

- Qualities **of any printer, without activating it**: `ContainerTree.getInstance().machines[definition_id].getQualityGroups(variant_names, material_bases, extruder_enabled)` — `C/cura/Machines/MachineNode.py:55`. For the active printer there is a shortcut in `C/cura/Machines/ContainerTree.py:44-59` that builds those three arguments from `extruderList`.
  - `QualityGroup`: `.name`, `.quality_type`, `.is_available`, `.node_for_global`, `.nodes_for_extruders` — `C/cura/Machines/QualityGroup.py:33-45`.
- Custom profiles: `MachineNode.getQualityChangesGroups(...)` — `C/cura/Machines/MachineNode.py:107`. `QualityChangesGroup` has `.name`, `.quality_type`, `.intent_category` and `.is_available` — `C/cura/Machines/QualityChangesGroup.py:16-24`.
- Intents: `IntentManager.intentCategories(definition_id, nozzle_name, material_base_file)` (`C/cura/Settings/IntentManager.py:61`; always includes `"default"`). `getCurrentAvailableIntents()` returns the valid `(intent_category, quality_type)` pairs for the active printer (`:77`).
- Active profile: `extruder.intent.getMetaDataEntry("intent_category")`, `global_stack.quality.getMetaDataEntry("quality_type")` and `global_stack.qualityChanges` (empty if it is `empty_quality_changes_container`). See `MachineManager.activeQualityType`, `activeIntentCategory` and `activeQualityChangesGroup` (`MachineManager.py:617,645,1764`).
- **Activation, like the GUI**:
  - Quality + intent: `IntentManager.selectIntent(intent_category, quality_type)` — `C/cura/Settings/IntentManager.py:168`. Used by `QualitiesWithIntentMenu.qml:127` and `RecommendedQualityProfileSelector.qml:47`. Internally it calls `MachineManager.setQualityGroupByQualityType` (`:205`).
  - Custom profile: `MachineManager.setQualityChangesGroup(group, no_dialog=True)` — `MachineManager.py:1745`. It also applies its intent (`:1232`).
  - ⚠️ `setQualityGroup` and `setQualityChangesGroup` open the "Discard or keep changes" dialog if the *user* container has values and `cura/active_mode == 1` (`MachineManager.py:1631,1751`, `CuraApplication.discardOrKeepProfileChanges` at `:773`). `selectIntent` does **not** accept `no_dialog`. So **the user container must be empty before changing profiles**.

## 5. Loading a file into the scene

- `CuraApplication.readLocalFile(QUrl, project_mode=None, add_to_recent_files=True)` — `C/cura/CuraApplication.py:1959`. It starts an asynchronous `ReadMeshJob` and, when done, calls `_readMeshFinished` (`:2034`).
- **End of loading**: the `fileCompleted(str file_name)` signal (`:1838`), emitted at the end of `_readMeshFinished` (`:2154`), after adding the node, placing it and running `arrange`. `fileLoaded` (`:1837`, emitted at `:2052`) comes **before** the node is added and is no use here.
- ⚠️ **If loading fails** (`nodes is None`, no global stack or no build volume), `_readMeshFinished` returns **without emitting anything** (`:2035-2046`). A timeout is needed.
- What loading does to the node (this defines the "initial transformation"):
  1. `MeshFileHandler.readerRead` centres the mesh on the centre of its bounding box: `node.setCenterPosition(extents.center)`, which moves the vertices and stores `mesh_data.getCenterPosition()` — `U/UM/Mesh/MeshFileHandler.py:42-47` and `U/UM/Scene/SceneNode.py:119-131`.
  2. `ReadMeshJob` may **auto-scale** according to the `mesh/scale_to_fit` and `mesh/scale_tiny_meshes` preferences — `U/UM/Mesh/ReadMeshJob.py:45-77`.
  3. A `CuraSceneNode` is created with the `SliceableObjectDecorator`, `ConvexHullDecorator` and `BuildPlateDecorator` decorators, and added with `AddSceneNodeOperation(...).push()`, i.e. it **goes onto the undo stack** — `C/cura/CuraApplication.py:2071-2141`.
  4. `Nest2DArrange(...).arrange()` places the model on the plate and **may rotate it around the vertical axis**, since `lock_rotation=False` by default — `C/cura/Arranging/Nest2DArrange.py:33,130,154-156`, called at `CuraApplication.py:2143-2145`.
  5. It drops it: `translate(0, -bbox.bottom, 0)` in world space (`:2150-2151`).
  6. `fileLoaded` makes `PrintInformation.setBaseName` change the job name in the GUI (`C/cura/UI/PrintInformation.py:75`).
- Preferences that affect loading: `cura/select_models_on_load` (`CuraApplication.py:1995`) and `cura/choice_on_open_project` (projects only).

## 6. Clearing the scene

- `CuraApplication.deleteAll(only_selectable=True)` — `C/cura/CuraApplication.py:2242`. It calls `QtApplication.deleteAll` → `Controller.deleteAllNodesWithMeshData` (`U/UM/Controller.py:511`) and also removes nodes with layer data. Internally it uses operations that also go onto the undo stack.

## 7. Transformations, build plate and build volume

- `SceneNode.setTransformation(Matrix)` replaces the local matrix, recomputes the decomposition and the AABB, and emits `transformationChanged` — `U/UM/Scene/SceneNode.py:562-564,827-830`. There are also `getWorldTransformation()` (`:538`), `getLocalTransformation()` (`:550`), `rotate`, `setOrientation`, `translate`, `setPosition` and `scale` (`:574-715`) and `getBoundingBox()` (`:793`). Model nodes are direct children of the root, so local = world.
- `UM.Math.Matrix` convention: a *row-major* numpy 4×4 with column vectors (`p' = M·p`); the translation is in `[:3, 3]`. This is how `StartSliceJob.py:485-491` uses it.
- Dropping onto the plate: `node.translate(Vector(0, -bbox.bottom, 0), SceneNode.TransformSpace.World)`, like `CuraApplication.py:2151`. `PlatformPhysics` does it too, but on a timer (`C/cura/PlatformPhysics.py:54-96`), so the plugin does it explicitly.
- Centring: translate in scene X and Z so that the centre of the AABB is at (0, ·, 0), the centre of the plate (see §9).
- Does it fit? `BuildVolume.checkBoundsAndUpdate(node)` — `C/cura/BuildVolume.py:339` — and then `node.isOutsideBuildArea()` (`C/cura/Scene/CuraSceneNode.py:42`). It checks the AABB against the volume (`collidesWithBbox`), the convex hull against `getDisallowedAreas()` (`collidesWithAreas`, `CuraSceneNode.py:114`), and whether the assigned extruder is enabled.
  - The convex hull is computed **synchronously and on demand**, cached per world transformation (`C/cura/Scene/ConvexHullDecorator.py:102-116,231,271`), so it is valid right after `setTransformation`.
  - ⚠️ `BuildVolume` recomputes its dimensions and disallowed areas on **timers**: 100 ms after a stack change and 150 ms after a setting change (`BuildVolume.py:108-135,649-700,769-775`). After changing printer, profile or overrides, the event loop must run (≈300 ms) before checking whether the model fits.

## 8. Slicing

- Backend: `app.getBackend()` (`U/UM/Application.py:446`) → `CuraEngineBackend`.
- Starting a slice like the "Slice" button: `forceSlice()` = `markSliceAll()` + `slice()` — `C/plugins/CuraEngineBackend/CuraEngineBackend.py:330-335`.
- States: `UM.Backend.Backend.BackendState` (`U/UM/Backend/Backend.py:35-39`): `NotStarted=1`, `Processing=2`, `Done=3`, `Error=4`, `Disabled=5`. Signals `backendStateChange(state)` and `processingProgress(float 0..1)` (`Backend.py:65-66`).
  - Successful end: `_onSlicingFinishedMessage` → `setState(Done)` and `processingProgress(1.0)` (`CuraEngineBackend.py:855-864`).
  - Preparation errors (`SettingError`, `ObjectSettingError`, `BuildPlateError`, `NothingToSlice`, `MaterialIncompatible`, `ObjectsWithDisabledExtruder`, socket error): `setState(Error)` + `backendError.emit(job)`, plus a pop-up `Message` in the GUI (`:470-613`). The reason comes from `job.getResult()` (`StartJobResult`).
  - Engine crash: `_onBackendQuit` (`:1108`).
  - Progress: `processingProgress.emit(message.amount)` (`:839`).
- Slicing steps: `slice()` calls `_createSnapshot()`, which only works if `CuraApplication.isVisible` (`:337-347`).

## 9. Axis convention (critical)

- **Uranium scene: Y up.** The plate is on the Y=0 plane and the scene origin is the **centre of the plate**. The volume spans X ∈ [-w/2, w/2], Y ∈ [0, h] and Z ∈ [-d/2, d/2] — `C/cura/BuildVolume.py:559-564`.
- **STL reader** (`U/plugins/FileHandlers/STLReader/STLReader.py:75-85`): negates Y and then swaps columns 1 and 2. Result: `STL (x, y, z) → scene (x, z, -y)`.
- **Towards the engine** (`C/plugins/CuraEngineBackend/StartSliceJob.py:488-495`): applies the world matrix and converts `scene (x, y, z) → (x, -z, y)`, the exact inverse of the above. So the "printer coordinates" Cura uses are: **Z up, mm, origin at the centre of the plate**. `machine_center_is_zero` only decides whether CuraEngine offsets the G-code when writing it; the G-code origin is drawn at scene `(min_w, 0, max_d)`, i.e. front left (`BuildVolume.py:572-576`).
- `machine_disallowed_areas` and the 2D hulls are on the `(x, z_scene)` plane (`BuildVolume.py:965-968` and `ConvexHullDecorator.py:286`). In printer coordinates they are `(x, -y)`.
- **Formula for `coords.py`**, derived from the above. With `A` = printer→scene axis change (homogeneous), `A = [[1,0,0,0],[0,0,1,0],[0,-1,0,0],[0,0,0,1]]`; `W` = the node's world transformation; `c` = `mesh_data.getCenterPosition()` (scene); and `v` = a vertex of the original STL:
  - scene position = `W · T(-c) · A · v`
  - **the job's printer matrix**: `M = A⁻¹ · W · T(-c) · A`
  - inverse, to apply a received `M`: `W = A · M · A⁻¹ · T(c)`

## 10. G-code, post-processing and thumbnail

- The G-code ends up in `scene.gcode_dict[build_plate]`, a list of strings. It is reset in `slice()` (`CuraEngineBackend.py:376-408`) and filled by `_onGCodeLayerMessage` and `_onGCodePrefixMessage` (`:908-931`). When done, `{print_time}`, `{filament_amount}`, `{filament_weight}`, `{filament_cost}` and `{jobname}` are replaced with the values from `PrintInformation` (`:875-879`). Active plate: `app.getMultiBuildPlateModel().activeBuildPlate` (`CuraApplication.py:1142`).
- Serialising: `app.getMeshFileHandler().getWriter("GCodeWriter")` (`U/UM/FileHandler/FileHandler.py:167`; writers are indexed by plugin id, `:141`) and then `.write(stream, nodes, MeshWriter.OutputMode.TextMode)` — `C/plugins/GCodeWriter/GCodeWriter.py:59`. If it is missing, it appends the `;SETTING_3` block.
- ⚠️ **Post-processing is NOT run by `GCodeWriter`.** `PostProcessingPlugin.execute` is connected to `OutputDeviceManager.writeStarted` (`C/plugins/PostProcessingPlugin/PostProcessingPlugin.py:51,71-105`). That signal is only emitted when an *OutputDevice* starts writing; the GUI does it in `LocalFileOutputDevice._performWrite`, just before starting `WriteFileJob` with the writer (`U/plugins/LocalFileOutputDevice/LocalFileOutputDevice.py:165`). The plugin changes `scene.gcode_dict` in place and marks it `;POSTPROCESSED` to avoid running twice. Scripts are **per printer** (metadata `post_processing_scripts`) and are reloaded on `globalContainerStackChanged` (`:52,305`).
- ⚠️ **Thumbnail**: in a plain `.gcode` it only exists through the `CreateThumbnail` post-processing script, if the user has set it up. It uses `cura.Snapshot.Snapshot.snapshot()`, which renders with OpenGL on the main thread (`C/plugins/PostProcessingPlugin/scripts/CreateThumbnail.py:14-19`). `GCodeWriter` does not generate thumbnails.
- Other listeners of `OutputDeviceManager.writeStarted` in Cura: `PrintInformation._onOutputStart`, which only acts if the device is a `ProjectOutputDevice` (`C/cura/UI/PrintInformation.py:77,501-506`).

## 11. Time and material

- `app.getPrintInformation()` (`C/cura/CuraApplication.py:1237`). It is fed by `CuraEngineBackend.printDurationMessage` (`PrintInformation.py:55,218`), emitted in `_onPrintTimeMaterialEstimates` (`CuraEngineBackend.py:971-1009`).
  - `currentPrintTime` → `Duration`, with `int(duration)` in seconds (`U/UM/Qt/Duration.py:145`).
  - `materialLengths` (m), `materialWeights` (g), `materialCosts` and `materialNames`: lists per extruder (`PrintInformation.py:151-172`, computed at `:252-313`). The currency is the `cura/currency` preference.
  - `getFeaturePrintTimes()` → time per line type (`:462`).
- If an enabled extruder is left unused, a warning `Message` appears in the GUI (`CuraEngineBackend.py:990-1006`).

## 12. Settings tree, evaluated values and i18n

- Definitions: `global_stack.definition` (a `DefinitionContainer`) → `findDefinitions(key=...)` (`U/UM/Settings/DefinitionContainer.py:441`). Each `SettingDefinition` has `.key`, `.label`, `.description`, `.type`, `.unit`, `.options`, `.children`, `.settable_per_extruder`...; the GUI uses `SettingDefinitionsModel` and excludes the `machine_settings` and `command_line_settings` categories and the `*_mesh` settings (`C/resources/qml/Settings/SettingView.qml:205-211`).
- **Evaluated value**: `stack.getProperty(key, prop)` for `"value"`, `"enabled"`, `"minimum_value"`, `"maximum_value"`, `"minimum_value_warning"`, `"maximum_value_warning"`, `"limit_to_extruder"`, `"settable_per_extruder"`, `"options"`... Functions (`SettingFunction`) come already evaluated by the stack. This is what `SettingPropertyProvider._getPropertyValue` does (`U/UM/Settings/Models/SettingPropertyProvider.py:437-472`).
  - `GlobalStack.getProperty` resolves `resolve` and `limit_to_extruder` (`C/cura/Settings/GlobalStack.py:202-245`); `ExtruderStack.getProperty` redirects to the global stack if the setting is not `settable_per_extruder` (`C/cura/Settings/ExtruderStack.py:117-160`).
  - Which stack the GUI queries for each setting (`SettingView.qml:276-304`): if it is not `settable_per_extruder`, the global one; if it has `limit_to_extruder >= 0`, that extruder's; otherwise the active extruder's.
- **validation_state**: if `getProperty(key, "validationState")` returns `None`, `SettingDefinition.getValidatorForType(type)(key)` is built and called with the stack (`SettingPropertyProvider.py:452-463`). Possible states: `Exception`, `Unknown`, `Valid`, `Invalid`, `MinimumError`, `MinimumWarning`, `MaximumError`, `MaximumWarning` (`U/UM/Settings/Validator.py:16-24`).
- **i18n**: `i18nCatalog(name, language)` takes a language per instance (`U/UM/i18n.py:35`). Catalogs are named after the definition file (`"fdmprinter.def.json"`, `"fdmextruder.def.json"`), as in `SettingDefinitionsModel._update` (`U/UM/Settings/Models/SettingDefinitionsModel.py:624-627`). Message contexts: `"<key> label"`, `"<key> description"` and `"<key> option <opt>"` (`:565-572`). The installation ships e.g. `share/cura/resources/i18n/es_ES/LC_MESSAGES/fdmprinter.def.json.mo`, `fdmextruder.def.json.mo` and `cura.mo`. There is no `en_US` catalog: the definitions are already in English.
- **Visibility presets**: `C/resources/setting_visibility/basic.cfg`, `advanced.cfg` and `expert.cfg` (INI format: section = category, lines = keys). They are loaded with `SettingVisibilityPreset.loadFromFile` (`C/cura/Settings/SettingVisibilityPreset.py:64`) through `Resources.getAllResourcesOfType(CuraApplication.ResourceTypes.SettingVisibilityPreset)` (`C/cura/Machines/Models/SettingVisibilityPresetsModel.py:73`). `"all"` = `MachineManager.getAllSettingKeys()` (`:83-84`).

## 13. Writing to and clearing the *user* container

- `stack.userChanges` → `InstanceContainer` — `C/cura/Settings/CuraContainerStack.py:79`. `CuraContainerStack.setProperty` always writes to *user* (`:228-242`).
- Writing: `stack.userChanges.setProperty(key, "value", v)` — `U/UM/Settings/InstanceContainer.py:380`. The GUI writes **strings** (`SettingTextField.qml:154`) and, when reading, `SettingInstance` converts them with `SettingDefinition.settingValueFromString` (`U/UM/Settings/SettingInstance.py:150`). From JSON to string: `SettingDefinition.settingValueToString(type, value)` (`U/UM/Settings/SettingDefinition.py:657`). A string starting with `=` is stored as a formula (`SettingInstance.py:172-173`).
- Removing a key: `userChanges.removeInstance(key)` (`InstanceContainer.py:733`). Removing everything: `userChanges.clear()` (`:432`). Present keys: `getAllKeys()` (`:443`).
- ⚠️ The *user* container **is persisted to disk** (`%APPDATA%\cura\5.13\user\*.inst.cfg`) when Cura saves. If Cura closes in the middle of a job, the overrides would stay saved.


---

## Design decisions

- **Empty build plate required.** If Cura's GUI has models on the plate, the job is rejected (`409 scene_not_empty`) instead of removing them and putting them back. Remote Web Control is meant for a Cura instance dedicated to the API.
- **Setting changes only in the *user* container.** Nothing is ever written to `quality_changes` or to any saved profile: an existing profile is selected and the job's changes are applied on top. At the start of each session a snapshot of the printer's *user* container is taken, and it is restored at the end.
- **Post-processing and thumbnail as in the GUI.** The G-code is written by emitting `OutputDeviceManager.writeStarted` with the plugin's own `OutputDevice` and then calling `GCodeWriter.write`, the same sequence as `LocalFileOutputDevice`. This runs the printer's post-processing scripts (including the `CreateThumbnail` thumbnail).
- **Printer coordinates throughout the API**: Z up, mm, origin at the centre of the plate (§9). The conversion to and from Uranium's scene lives only in `coords.py`.
- **Loading like the GUI.** The auto-scaling and placement (`arrange`) Cura applies on load are kept, and returned as the job's initial transformation. The material and nozzle are the ones configured in Cura for that printer.
- **Accepted limitations**: Cura's error `Message`s are not suppressed (they are not modal), and loading goes through the GUI's undo stack.

## Running without a display (Docker)

- `--headless` **does not work**. `Application` reads it at `U/UM/Application.py:153,165`; with it, `CuraApplication.run` calls `runWithoutGUI()` (`C/cura/CuraApplication.py:940,988-991`), which **does not create `BuildVolume` or `PlatformPhysics`** (they are created in `runWithGUI`, `:993-1016`). Without `BuildVolume`, `_readMeshFinished` discards the mesh ("Can't load meshes before the build volume is initialized", `:2039-2041`).
- So in a container Cura starts **in normal GUI mode** on a virtual display (Xvfb) with software OpenGL (Mesa llvmpipe). This also lets `Snapshot` render the thumbnail.

## Problems found and how they are solved

### Scene and placement

- **Third-party plugins that change the model after loading.** The *Auto Orientation* plugin (`OrientationPlugin`), with its auto-orient-on-load option, listens to `sceneChanged`, `fileLoaded` and `fileCompleted` and starts a `CalculateOrientationJob` in **another thread** which, when finished, pushes a `RotateOperation`. With large models that rotation can arrive **after** the model has been placed, or during slicing, and the G-code would not match the job's matrix. Solution: during each session the `OrientationPlugin/do_auto_orientation` preference is set to `False` and restored afterwards (`scene_ops.SESSION_PREFERENCES`). Preference signals are queued (`Preferences.preferenceChanged = Signal(Signal.Queued)`, `U/UM/Preferences.py:175`), so it is changed in `activate`, well before loading. The API's on-demand auto-orientation uses the same computation (`MeshTweaker.Tweak`, extended mode) in a controlled way.
- **"Discard or keep changes" modal dialog.** `IntentManager.selectIntent` does not accept `no_dialog` and ends up in `MachineManager.setQualityGroup`, which opens the dialog if `hasUserSettings` and `cura/active_mode == 1` (`MachineManager.py:1631`). `hasUserSettings` comes from a counter that is only recomputed on certain signals (`MachineManager.py:133,163-176`): after `setActiveMachine` it can keep the previous printer's value even though the *user* container has just been emptied. Symptom: the API keeps working, but Cura's window cannot be closed, and if someone clicks "Discard" the active printer's user settings are wiped. Solution: during the session, `cura/choice_on_profile_override = "always_keep"` (the dialog-free path of `CuraApplication.discardOrKeepProfileChanges`, `:773-789`), restored at the end.
- **Job name.** The GUI sets `PrintInformation.baseName = ""` from QML when the plate becomes empty (`C/resources/qml/JobSpecs.qml:26-34`), and `setBaseName` only accepts a new name if the previous one is empty (`C/cura/UI/PrintInformation.py:396-441`). `setBaseName("")` is called before each load and when restoring; otherwise `{jobname}` could carry over the previous model's name.
- **`fileCompleted`** emits the path with forward slashes (it comes from `QUrl.toLocalFile()`); it is compared with `normcase(normpath())`.
- **Preview performance**: with a 2-million-triangle STL (100 MB), `read_stl` takes ~0.5 s and decimation to 800,000 triangles ~4.4 s. Cura bundles **numpy 1.x** (it has `numpy/core`, not `numpy/_core`); the tests pass with 1.x and 2.x.

- **Several models on one plate.** The job's files are loaded one at a time, waiting for each `fileCompleted`, like dropping several files on the GUI: Cura arranges each new model around the ones already loaded (`CuraApplication._readMeshFinished`, `Nest2DArrange` with the others as fixed nodes), and the job name (`{jobname}`) comes from the first file, because `PrintInformation.setBaseName` only accepts a name while the current one is empty (it is reset only before the first load). The models are matched to the job's objects by load order (the scene root's children). Copies load the same file again.
- **Placing several models.** `SceneOps.place` applies each stored matrix and drops each model onto the plate. Overlaps are detected with the 2D convex hulls Cura itself uses (`getConvexHull`, `Polygon.intersectsPolygon`). When a change leaves a model overlapping another one or outside the build volume, `Nest2DArrange` (the arrange behind "Arrange All", `cura/Arranging/Nest2DArrange.py`) re-arranges only the models that changed, with the others fixed; then all of them; and if there is no room for every one, it places those that fit and parks the rest beside the plate, as the GUI does. `arrange(only_if_full_success = True)` is what makes the first two attempts move nothing when they fail. Slicing uses mode `exact` (no arrange at all), so the G-code always matches the stored matrices; checked by comparing the G-code's `;MINX`/`;MAXX`/... with the job's bounding box plus the skirt.
- **Concurrent changes.** Every change to a job's objects is a single task on the scene worker (read the job, place, save), so two quick changes cannot overwrite each other. Files of an addition are written before the task and, if it fails, only those files are deleted: another addition may be in flight with files that are not in the job yet (seen live, with two uploads at once).

### Server

- **Shared port on Windows.** `http.server` sets `SO_REUSEADDR`, and on Windows that lets **two processes** listen on the same port without an error (for example, two Cura instances). The server uses `SO_EXCLUSIVEADDRUSE` on Windows (`server._ExclusiveHTTPServer`).

### Slicing

- **Slices that stop by themselves.** `CuraEngineBackend._onSceneChanged` (`C/plugins/CuraEngineBackend/CuraEngineBackend.py:668-721`) and `needsSlicing` (`:776-789`, called from `_onSettingChanged`) call `stopSlicing()`: the backend goes back to `NotStarted` silently. Therefore: (1) the event loop is allowed to run before `forceSlice()`; (2) if the state goes back to `NotStarted` after `Processing`, the attempt is considered aborted and retried up to 3 times (`session.run_slice`); (3) if it does not reach `Processing` within 120 s, it is retried too.
- **Preparation errors.** `StartSliceJob` waits for `MachineErrorChecker` on its own (`StartSliceJob.py:303-309`). Errors arrive through `backendError(job)` with `job.getResult()` (`StartJobResult`, `:40-48`); for `SettingError` the keys come from `stack.getErrorKeys()`.
- **Progress ordering.** `_onProgressMessage` emits `processingProgress` **before** `setState(Processing)` (`:833-840`), so progress is also accepted in the `waiting` state.
- **Line endings.** `LocalFileOutputDevice` opens the file with `open(..., "wt", encoding="utf-8")` (`U/plugins/LocalFileOutputDevice/LocalFileOutputDevice.py:171`), which writes **CRLF** on Windows and LF on Linux. The plugin does the same so the file is identical to the GUI's on each platform.
- **Download name.** `PrintInformation.jobName` includes the machine prefix (preference `cura/jobname_prefix`), like the name the GUI suggests when saving.
- **Determinism.** On Linux, two slices of the same job give byte-identical files. The **Windows CuraEngine is not deterministic**: two slices in a row can differ in the order of some moves within a layer. Between Windows and Linux, with the same configuration, the moves match one of the Windows variants; only the line endings and the thumbnail pixels differ (GPU vs llvmpipe).

### Settings

- **Evaluating settings requires activating the printer.** On a printer that is not active the extruders are not linked (§3), so the schema and the diff are computed in a session: activate printer and profile, apply overrides, read, restore. No model is loaded.
- **Stack for each setting**: the same one the GUI uses (`SettingView.qml:276-304`). Global if it is not `settable_per_extruder`; if it is, the `limit_to_extruder` one (if ≥ 0) or the requested extruder's.
- **`default`** = `getProperty(key, "value", ctx)` with `ctx.context["evaluate_from_container_index"] = 1`, which skips the *user* container throughout the evaluation (`U/UM/Settings/ContainerStack.py:283-289`), dependencies included.
- **`validation_state`**: like `SettingPropertyProvider` (`getValidatorForType(type)(key)(stack)` when the stack returns `None`).
- **Translations**: `i18nCatalog(<definition file>, language)` for each inherited file; the last one with a translation wins (like `SettingDefinitionsModel._update`). Contexts: `"<key> label"`, `"<key> description"`, `"<key> option <opt>"`.
- **Per-extruder settings with global scope**: rejected (`use_extruder_scope`). The GUI writes them to the extruder's stack, and a value in the global *user* container would be hidden by the extruder's own containers (`C/cura/Settings/ExtruderStack.py:117-160`).
- **Formulas**: a string starting with `=` becomes a `SettingFunction` (`SettingInstance.py:172-173`) and is evaluated as Python code. The API rejects them (`formulas_not_allowed`).
- **Performance**: evaluating the 9 properties of ~640 settings costs ~1–1.5 s on the main thread (split by category so Qt does not freeze). Only the settings in the response are evaluated (the "Basic" preset went from ~12 s to ~0.1 s), and the profile is not re-selected if it is already active. Switching printers and back costs 2–3 s.

## Phone app (PWA served by the plugin)

- **No build step**: HTML, CSS and JavaScript with native modules; three.js and qrcode-generator bundled in `web/vendor` (MIT).
- **Languages**: English and Spanish (`web/js/i18n.js`), chosen from the browser's language. Setting labels are requested from Cura in the same language (`es_ES` or `en_US`).
- **PIN pairing**: on iOS, a web app added to the home screen has storage separate from Safari's, so a token handed to Safari through a QR code does not reach the app on the icon. Cura's computer generates a 6-digit PIN (only from that computer, or with the token) that expires after 10 min and is revoked after 10 failures. The QR code contains `http://<ip>:8765/?pin=NNNNNN` and the app redeems it by itself.
- **CSP with an import map**: three.js (`OrbitControls`) imports the `"three"` specifier, which needs an inline `<script type="importmap">`. `static_files.inline_script_hashes` computes the SHA-256 of each import map when serving the HTML and adds it to `script-src`, without `'unsafe-inline'` for scripts.
- **iOS and files**: `<input type=file accept=".stl">` greys out STL files on iOS (it does not know the type), so there is no filter and the name is validated instead. Downloading uses `fetch` with the token, a `Blob` and `<a download>`; sharing uses `navigator.share({files})` with type `text/plain`.
- **No service worker**: service workers only exist in secure contexts (HTTPS or `localhost`). Over HTTP the app works and can be added to the home screen, but without an offline mode.

## Docker

- **File layout on Linux** (`U/UM/Resources.py:186-195`, `:490-501`): `Resources.Preferences` (`cura.cfg`) and `plugins.json` (`U/UM/PluginRegistry.py:125-129`) go to `~/.config/cura/<series>`; everything else (containers, `packages.json`, `scripts`, user plugins in `<data>/plugins`, `Application.py:254-255`) to `~/.local/share/cura/<series>`. On Windows and macOS everything is in one folder. `scripts/export_cura_config.sh` does that split.
- **Image**: Ubuntu 24.04 with the official AppImage extracted (`--appimage-extract`, since containers have no FUSE), Xvfb, Mesa llvmpipe (`LIBGL_ALWAYS_SOFTWARE=1`), `QT_QPA_PLATFORM=xcb` and `LANG=C.UTF-8`. Cura runs as the `cura` user (uid 1000); the entrypoint starts as root only to fix the ownership of the volumes (and prune resources, see below) and then switches user with `setpriv`, keeping the environment.
- **Plugin**: linked from the image (`/opt/remote-web-control/RemoteWebControl`) into the plugins folder on every start, so updating the image updates the plugin.
- **Pairing in Docker**: requests never come from `localhost`, so `POST /api/pairing/start` also accepts the token. Outside `localhost`, the pairing page uses `location.origin` for the QR code, because the server only knows the container's internal IP. `RWC_PUBLIC_URL` replaces the URLs the plugin announces.
- **Restarts**: after a `docker restart`, `/tmp/.X99-lock` was left behind, Xvfb did not start and the container stopped. The entrypoint removes the lock and checks that Xvfb starts.

### Memory

- **Breakdown** (anonymous memory measured with `smaps_rollup`): Cura ~418 MB, Xvfb ~18 MB, idle CuraEngine ~2 MB. Reading `/proc/<pid>/smaps_rollup` requires entering as the `cura` user: Docker drops `CAP_SYS_PTRACE` even for root.
- **Tried with no noticeable effect**: `MALLOC_ARENA_MAX=2` (−1 MB), a 640×480 display (−5 MB) and disabling 34 non-essential plugins (−5/6 MB). Cura re-enables by itself the plugins it marks as required (`CuraApplication.py:537-560`).
- **What weighs**: on start-up Cura loads the metadata of all its resources: 708 definitions, 6,019 qualities, 1,749 variants, 641 intents and 281 materials. Branded materials multiply the `ContainerTree` nodes. `docker/prune_resources.sh` (with `CURA_KEEP_VENDORS`) removes, inside the container, other brands' resources and the unused non-generic materials. It first checks that every container referenced by the printers and extruders still exists; if any is missing, it removes nothing. Measured: ~475 → ~375 MB on start-up, with byte-identical G-code.
- **Growth after working**: memory freed by Python and glibc does not go back to the system (~375 → ~460 MB after three slices). `memory.release_memory()` (`gc.collect()` + glibc's `malloc_trim(0)` through `ctypes`, 3 s after each restore) brings it down to ~425 MB.

### Image size

- ~1.3 GB. The extracted Cura takes ~1 GB (`usr/` is a mini-distribution packaged by UltiMaker; only `usr/share/{doc,locale,perl}` is removed, since it is unused at runtime; icons are kept for the dialogs seen through noVNC). `libllvm20` and `mesa-libgallium` are essential for llvmpipe.
- Ubuntu's `novnc`/`websockify` packages pull in Node.js, ICU, numpy/LAPACK, Babel... (~170 MB). They are replaced by noVNC and websockify from GitHub (pinned versions) on top of `python3`. In websockify, numpy is optional (it only speeds up unmasking) and `requests`/`jwcrypto`/`redis` are only imported by the *token plugins*.
- The base image does not affect RAM: only processes are in memory. Alpine does not work (musl vs the AppImage's glibc).

## Other details

- If loading fails, `_readMeshFinished` does not emit `fileCompleted`: the wait has a timeout.
- `BuildVolume` updates on timers: before validating the placement, the event loop runs for ≈0.5 s (never `sleep` on the main thread).
- `readLocalFile` clears the scene by itself if there is an `isBlockSlicing` node (a loaded G-code); with the empty plate requirement this does not apply.
- The preview uses its own numpy STL reader (`stl.py`), testable outside Cura; slicing always uses Cura's reader.
