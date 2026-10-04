# Remote Web Control API

Reference of the HTTP API used by the phone app. It can also be used from scripts or other integrations.

Contents: [errors and conventions](#errors-and-conventions) · [printers and profiles](#get-apihealth) ·
[settings](#settings) · [jobs](#jobs) · [pairing](#pairing)

## Errors and conventions

Every route lives under `/api` and requires `Authorization: Bearer <token>`. Responses are JSON, gzip-compressed if the client accepts it. Errors look like this:

```json
{ "error": { "code": "printer_not_found", "message": "Printer 'X' does not exist." } }
```

Common HTTP codes:

| HTTP | `code` | When |
|---|---|---|
| 400 | `invalid_json`, `invalid_form`, `invalid_profile`, `invalid_matrix`, `missing_file`, `missing_printer_id` | Malformed request. |
| 401 | `unauthorized` | The token is missing or wrong. |
| 404 | `not_found`, `printer_not_found`, `job_not_found`, `object_not_found` | It does not exist. |
| 405 | `method_not_allowed` | |
| 409 | `scene_not_empty` | The build plate of Cura's GUI has models on it. Remote Web Control only works with an empty plate. |
| 409 | `job_busy` | The job is queued or being sliced. |
| 409 | `last_object` | Removing the only model of a job. |
| 409 | `job_not_running`, `gcode_not_ready` | Cancelling a job that is neither queued nor slicing, or asking for the G-code before `done`. |
| 413 | `payload_too_large` | Larger than `max_upload_mb`. |
| 415 | `unsupported_media_type` | `POST /api/jobs` without `multipart/form-data`. |
| 422 | `invalid_stl`, `profile_not_found`, `profile_not_available`, `profile_not_applied`, `load_failed`, `auto_orient_unavailable`, `too_many_objects` | Well-formed data that Cura cannot use (a job has at most 100 models). |
| 422 | `does_not_fit` | Slicing a job whose models do not fit on the plate. |
| 422 | `unknown_setting`, `invalid_value`, `formulas_not_allowed`, `use_extruder_scope`, `not_settable_per_extruder`, `invalid_extruder` | A setting change that cannot be applied. |
| 400 | `invalid_visibility`, `invalid_language`, `invalid_request` | Malformed settings or copy parameters. |
| 500 | `internal_error` | Unexpected error; the details are in `cura.log`. |
| 503 | `main_thread_timeout`, `scene_busy` | Cura is busy or has a modal dialog open. |

**Coordinates**: the whole API uses printer coordinates: **Z up, millimetres, origin at the centre of the build plate**. This is Cura's internal convention; `machine_center_is_zero` tells where the G-code puts the origin.

```sh
TOKEN=$(cat "<Cura data folder>/RemoteWebControl/token.txt")   # with Docker: cura-data/data/5.13/RemoteWebControl/token.txt
H="Authorization: Bearer $TOKEN"
```

## `GET /api/health`

```sh
curl -H "$H" http://127.0.0.1:8765/api/health
```
```json
{ "plugin": "RemoteWebControl", "plugin_version": "0.1.0", "cura_version": "5.13.0", "sdk_version": "8.12.0", "status": "ok" }
```

It answers even when Cura is busy (it does not touch the main thread).

## `GET /api/printers`

Example (illustrative values):

```json
[
  {
    "id": "Creality Ender-3 Pro",
    "name": "Creality Ender-3 Pro",
    "definition": "creality_ender3pro",
    "definition_name": "Creality Ender-3 Pro",
    "active": true,
    "build_volume": { "x": 220.0, "y": 220.0, "z": 250.0 },
    "shape": "rectangular",
    "machine_center_is_zero": false,
    "disallowed_areas": [ [[-117.5, 117.5], [-117.5, 108], [117.5, 108], [117.5, 117.5]] ],
    "extruders": [
      { "position": 0, "id": "...", "name": "Extruder 1", "enabled": true, "nozzle": "0.4mm Nozzle",
        "material": { "name": "Generic PLA", "base_file": "generic_pla", "brand": "Generic",
                      "type": "PLA", "color": "#ffc924", "guid": "..." } }
    ]
  }
]
```

`disallowed_areas` are the machine's **fixed** disallowed areas (`machine_disallowed_areas`), in printer coordinates (X, Y). Cura also adds margins that depend on the settings (brim, nozzle offsets...). Those are taken into account when the placement is validated, not here.

## `GET /api/printers/{id}/profiles`

The `id` is URL-encoded (spaces → `%20`, `#` → `%23`).

```json
{
  "printer_id": "Creality Ender-3 Pro",
  "active": { "quality": "standard", "intent": "default", "quality_changes": null },
  "qualities": [
    { "quality_type": "standard", "name": "Standard Quality", "layer_height": 0.2,
      "available": true, "experimental": false, "active": true }
  ],
  "intents": [
    { "intent_category": "default", "name": "Default", "description": "...",
      "quality_types": ["draft", "standard", "super"], "active": true }
  ],
  "quality_changes": [
    { "name": "My profile", "quality_type": "standard", "intent_category": "default",
      "available": true, "active": false }
  ]
}
```

A job's profile is written as `{"quality": "<quality_type>", "intent": "<intent_category>", "quality_changes": "<name>" | null}`. If there is a `quality_changes`, it takes precedence over the other two, as in the GUI. `active` is the profile **that printer** has selected in Cura, even if it is not the GUI's active printer.

## Settings

### `GET /api/printers/{id}/settings?visibility=basic|advanced|expert|all&lang=es_ES&extruder=0`

The settings tree by category, **evaluated by Cura** with the profile that printer has (without user changes):
- `visibility`: Cura's presets (Basic, Advanced, Expert) or `all`. Default: `basic`.
- `lang`: language of labels, descriptions and options (`en_US`, `es_ES`, `de_DE`...). Default: Cura's language.
- `extruder`: the extruder "tab" for per-extruder settings, as in the GUI. Default: 0.

```json
{
  "printer_id": "Creality Ender-3 Pro", "profile": {...}, "visibility": "basic", "extruder": 0,
  "categories": [
    { "key": "infill", "label": "Infill", "description": "...", "type": "category", "visible": true,
      "children": [
        { "key": "infill_sparse_density", "label": "Infill Density", "description": "...",
          "type": "float", "unit": "%", "options": null,
          "value": 20.0, "default": 20.0, "enabled": true,
          "minimum_value": 0, "maximum_value": null, "minimum_value_warning": null, "maximum_value_warning": null,
          "validation_state": "Valid", "settable_per_extruder": true, "limit_to_extruder": null,
          "overridden": false, "visible": true, "children": [...] } ] } ]
}
```

- `value`: the effective value. `default`: the profile's value, without the job's changes.
- `enabled`: as in the GUI; Cura hides settings with `enabled: false`.
- `validation_state`: `Valid`, `MinimumWarning`, `MaximumWarning`, `MinimumError`, `MaximumError`, `Invalid`, `Exception` or `Unknown`.
- `options`: for `enum` settings, `[{key, label}]` with the translated label.
- With a visibility preset, only visible settings and their ancestors are returned. Ancestors have `visible: false`.

### `GET /api/jobs/{id}/settings` (same parameters)

The same tree, with the job's printer, profile and **changes**. `overridden: true` marks the settings changed in the job.

### `PATCH /api/jobs/{id}/settings` — change a setting

```sh
# Infill density is per extruder → scope "extruder"
curl -H "$H" -X PATCH -d '{"scope": "extruder", "extruder": 0, "key": "infill_sparse_density", "value": 37}' \
     http://127.0.0.1:8765/api/jobs/$ID/settings
# Supports are global
curl -H "$H" -X PATCH -d '{"scope": "global", "key": "support_enable", "value": true}' ...
# Remove the change (back to the profile value)
curl -H "$H" -X PATCH -d '{"scope": "global", "key": "support_enable", "value": null}' ...
```

Response: the job's changes and the list of settings whose `value`, `enabled` or `validation_state` changed. Cura computes them, so they include the settings that depend on the changed one:

```json
{ "overrides": { "global": {}, "extruders": { "0": { "infill_sparse_density": 37 } } },
  "changed": [
    { "key": "infill_line_distance", "extruder": 0, "value": 3.24, "enabled": true, "validation_state": "Valid",
      "before": { "value": 6.0, "enabled": true, "validation_state": "Valid" } },
    { "key": "infill_sparse_density", "extruder": 0, "value": 37, ... } ] }
```

Rules:
- **Scope**: settings Cura allows per extruder (`settable_per_extruder: true`) go with `"scope": "extruder"`, also on single-extruder printers. That is where the GUI writes them; a global value would be hidden by the extruder's profile. If the scope is wrong, the API answers `422` with a hint.
- **Types**: the type is checked (number, integer, boolean, an `enum` option, text, list). Ranges do not block: Cura flags them in `validation_state`, and an error prevents slicing (`setting_error`).
- **No formulas**: values starting with `=` are rejected, because Cura would run them as code.
- **Profiles are never touched**: changes are saved **in the job only**, never in Cura's profiles. They are applied to the *user* container only while evaluating or slicing, and Cura goes back to how it was afterwards.
- **Invalidates the slice**: if the job was already sliced, it goes back to `ready` and the G-code is discarded.

**Observed timings** (Windows 11 PC):
- `basic` schema of the active printer: about 0.1 s. `all` schema: about 1 s.
- If the printer is not the GUI's active one, add 2–3 s to switch printers and switch back.
- A PATCH takes about 3 s, because it evaluates every setting before and after the change.

## Jobs

A job is one build plate: one or more **objects** (models) sliced together, each with its own STL and matrix. Copies of a model share its file. The job is stored in `<Cura data folder>/RemoteWebControl/jobs/<id>/`. Cura's scene is rebuilt from scratch for each operation: the plate is checked to be empty, the job's printer and profile are activated, the STLs are loaded one after another (like dropping several files on the GUI), the models are placed... and Cura is **always** left as it was (active printer, profiles, unsaved settings, preferences).

States: `created → ready → queued → slicing → done | error`. Any change to the objects (orientation, copies, new files...) takes a sliced job back to `ready` and discards its G-code.

Changes to the objects of a job run one at a time, in Cura's own queue, and every one of them answers with the **full, updated job**.

### `POST /api/jobs` — upload one or more STLs

`multipart/form-data` with these fields:
- `file`: an STL (binary or ASCII). **Repeat the field** to put several models on the same plate.
- `printer_id`: the printer.
- `profile`: optional, JSON as above. If missing, the profile that printer has in Cura is used.
- `auto_orient`: optional, `true` to auto-orient every model on upload (see [Auto-orientation](#auto-orientation)).

```sh
curl -H "$H" -F "file=@samples/l_bracket.stl" -F "file=@samples/l_bracket.stl" \
     -F "printer_id=Creality Ender-3 Pro" -F 'profile={"quality": "standard"}' http://127.0.0.1:8765/api/jobs
```

`max_upload_mb` applies to the whole request. It returns `201` with the full job, including the placement Cura gives the models on load (auto-scaling if enabled in Cura, arranged on the plate and resting on it; a single model is centred):

```json
{
  "id": "83fbbe713e9b4db7a99aee11383f13a4",
  "name": "l_bracket.stl",
  "created_at": "2026-09-28T08:22:00Z",
  "updated_at": "2026-09-28T08:22:06Z",
  "printer_id": "Creality Ender-3 Pro",
  "profile": { "quality": "standard", "intent": "default", "quality_changes": null },
  "overrides": { "global": {}, "extruders": {} },
  "objects": [
    { "id": "b3cc448f4b77", "file": "8ade1648e41f", "name": "l_bracket.stl",
      "mesh": { "triangles": 28, "preview_triangles": 28, "decimated": false,
                "bbox": { "min": [0, 0, 0], "max": [30, 10, 20] } },
      "transform": [1, 0, 0, -15,  0, 1, 0, -5,  0, 0, 1, 0,  0, 0, 0, 1],
      "placement": { "fits": true, "bbox": { "min": [-15, -5, 0], "max": [15, 5, 20] }, "warnings": [] } },
    { "id": "76b94895e9c5", "file": "16dab112427b", "name": "l_bracket.stl", "...": "..." }
  ],
  "state": "ready",
  "progress": 0.0,
  "error": null,
  "placement": { "fits": true, "bbox": { "min": [-15, -40, 0], "max": [15, 5, 20] } },
  "result": null
}
```

- `name`: the first model's file name. Cura names the job after it (`{jobname}` in the G-code), as when several files are loaded in the GUI. File names with characters that are invalid on Windows are sanitised.
- `objects[].transform`: a *row-major* 4×4 matrix that takes the vertices **of the original STL** to their final position on the plate, in printer coordinates. It is applied as `p' = M · p`, with the translation in the last column.
- `objects[].mesh.bbox`: bounding box of the original STL. `objects[].placement.bbox`: bounding box once placed.
- `objects[].placement.fits`: the same test Cura uses to mark a model as not printable (build volume, disallowed areas with brim or skirt margins, disabled extruder), plus not overlapping another model.
- `objects[].placement.warnings`: a list of `{code, message}` with the codes `outside_build_volume`, `disallowed_area`, `extruder_disabled`, `overlapping`, `not_printable`, `scaled` and `mirrored`.
- `placement`: the whole plate. `fits` is true only if every model fits.

### `GET /api/jobs` · `GET /api/jobs/{id}` · `DELETE /api/jobs/{id}`

The list returns a summary per job (`id`, `name`, `created_at`, `updated_at`, `printer_id`, `state`, `progress`, `error` and `objects`, the number of models), newest first. `DELETE` answers `204`, or `409 job_busy` if the job is queued or slicing.

### `GET /api/jobs/{id}/objects/{object}/mesh` — mesh for the preview

`application/octet-stream` (gzip if the client accepts it), little-endian:

```
magic   4 bytes  "CRM1"
flags   uint32   bit0 = decimated
count   uint32   number of triangles
data    float32[count * 9]  (x, y, z) × 3 per triangle, printer coordinates of the UNTRANSFORMED STL
```

The client applies the object's `transform` itself. Copies have the same `file`, so their mesh only needs downloading once. STLs with more than 800,000 triangles are simplified for the preview only (grid vertex clustering); slicing always uses the original.

### How the models are placed

- **One model**: Cura applies its matrix, then **drops it onto the plate and centres it**, so the translation you send is not kept.
- **Several models**: each one keeps its X/Y position and is dropped onto the plate. If a change leaves a model outside the build volume or overlapping another one, Cura's own arrange (the one behind "Arrange All") finds room for the models that changed, around the others; if there is none, it re-arranges all of them; and if they do not all fit, the ones that fit are placed and the rest are left beside the plate (`outside_build_volume`), as the GUI does.
- The arrange may rotate models around the vertical axis, like in the GUI.

The effective matrices are stored in the job, and slicing applies them exactly as they are, so the G-code always matches the job.

### `PUT /api/jobs/{id}/objects/{object}/transform` — orient a model

```sh
# Rotate 90° around X
curl -H "$H" -X PUT -d '{"matrix": [1,0,0,0, 0,0,-1,0, 0,1,0,0, 0,0,0,1]}' \
     http://127.0.0.1:8765/api/jobs/$ID/objects/$OBJECT/transform
```

It answers with the job, which has the effective matrix. To rotate a model in place, rotate around its own centre: `T(c) · R · T(-c) · transform`, with `c` the centre of its `placement.bbox`. Scaling and mirroring are allowed (any non-singular affine matrix).

### `POST /api/jobs/{id}/objects` — add models

`multipart/form-data` with one or more `file` fields, and optionally `auto_orient=true`. Cura places the new models around the others, which do not move.

### `POST /api/jobs/{id}/objects/{object}/duplicate` — copies

Optional body `{"count": n}` (1 by default). The copies keep the model's orientation and are placed around the others.

### `DELETE /api/jobs/{id}/objects/{object}` — remove a model

It answers with the job. The last model cannot be removed (`409 last_object`): delete the job instead.

### `POST /api/jobs/{id}/arrange` — arrange all

Re-arranges every model, like "Arrange All" in the GUI.

### Auto-orientation

- **On upload**: add the field `auto_orient=true` to the `POST /api/jobs` (or `POST /api/jobs/{id}/objects`) form.
- **One model**: `POST /api/jobs/{id}/objects/{object}/auto-orient`, with no body.
- **Every model**: `POST /api/jobs/{id}/auto-orient`, with no body.

It uses the same computation as Cura's **Auto Orientation** plugin (Marketplace; Christoph Schranz's MeshTweaker), in extended mode and with its `min_volume` preference, just like auto-orientation on load in the GUI. The plugin must be installed and enabled in Cura; otherwise the answer is `422 auto_orient_unavailable`. Large models can take a while: it runs outside the main thread, so the API keeps answering in the meantime.

### Slicing: `POST /api/jobs/{id}/slice`

The job becomes `queued` and the response comes back straight away with `202`. Jobs are sliced **one at a time**, in order of arrival, because Cura has a single scene and a single CuraEngine. Changes to the models (orienting, adding, copies...) jump ahead of slices that have not started yet; a running slice is not interrupted. A job whose models do not fit is refused at once (`422 does_not_fit`).

To follow it, poll `GET /api/jobs/{id}` every 1 or 2 seconds:
- `state`: `queued`, then `slicing`, and finally `done` or `error`.
- `progress`: from 0 to 1.
- `error`: `{code, message}` when it fails. Common reasons: `does_not_fit`, `setting_error` (with the settings involved), `nothing_to_slice`, `material_incompatible`, `extruder_disabled`, `engine_error` and `slice_timeout`.

When it is done, the job has the result (real values for an 80 mm sphere):

```json
"result": {
  "print_time_s": 23562,
  "material": [ { "extruder": 0, "name": "Generic PLA", "length_m": 29.7, "weight_g": 88.58, "cost": 0.0 } ],
  "features": { "Outer Wall": 1506, "Inner Walls": 1865, "Infill": 16898, "Travel": 2351 },
  "job_name": "CE3PRO_sphere_80mm",
  "currency": "€",
  "fits": true,
  "gcode_bytes": 59443829
}
```

`features` are the seconds per line type, labelled in Cura's language. `cost` comes from the material prices set in Cura, in Cura's `currency`.

### Cancelling: `POST /api/jobs/{id}/cancel`

If the job is queued, it is taken out of the queue. If it is slicing, CuraEngine is stopped. Either way it goes back to `ready` and the job is returned. If it is neither queued nor slicing, the answer is `409 job_not_running`.

### Downloading: `GET /api/jobs/{id}/gcode`

`text/x-gcode` with `Content-Disposition: attachment`. The file name is the one the GUI would suggest when saving (`CE3PRO_sphere_80mm.gcode`). If the client accepts gzip, it is sent compressed (about 4 times smaller).

The G-code is produced exactly like "Save" in the GUI:
- Same backend and same writer (`GCodeWriter`).
- The printer's **post-processing scripts** run, including the **thumbnail** (`CreateThumbnail`).
- Same file format: on Windows, CRLF line endings.

```sh
curl -H "$H" -X POST http://127.0.0.1:8765/api/jobs/$ID/slice
curl -H "$H" http://127.0.0.1:8765/api/jobs/$ID            # repeat until "state": "done"
curl -H "$H" --compressed -OJ http://127.0.0.1:8765/api/jobs/$ID/gcode
```


## Pairing

Routes **without a token**, used by the `pair.html` page and by the app when pairing with a PIN.

### `POST /api/pairing/start`

Generates a 6-digit PIN valid for 10 minutes (the previous one stops working). Since it returns the token, it is only allowed:
- from the computer Cura runs on (a loopback IP, or the same client and server IP), or
- sending the token in `Authorization: Bearer ...`. This is what Docker needs, since nothing arrives from `localhost` there.

```json
{ "pin": "482913", "expires_in": 600, "urls": ["http://192.168.1.10:8765/"], "token": "..." }
```

`urls` are the app addresses the server believes are reachable from the local network. With Docker, set it with `RWC_PUBLIC_URL`.

### `POST /api/pairing/claim`

```sh
curl -X POST -H "Content-Type: application/json" -d '{"pin": "482913"}' http://192.168.1.10:8765/api/pairing/claim
```
```json
{ "token": "..." }
```

`403` errors: `invalid_pin`, `pin_expired`, `no_pairing` (no PIN has been generated) and `pin_blocked` (the PIN is revoked after 10 failed attempts).
