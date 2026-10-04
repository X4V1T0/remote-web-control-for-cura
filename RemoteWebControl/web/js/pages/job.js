// Job page: Place (3D placement), Settings and Slice (slice + G-code).

import { api, errorMessage, messageFor } from "../api.js";
import { Viewer, multiply, rotationList, translationList } from "../viewer.js";
import {
  curaLanguage, el, filenameFromDisposition, formatDuration, formatNumber, jobTitle, saveBlob, stateBadge, toast, warningLabel,
} from "../util.js";
import { t } from "../i18n.js";

const TABS = ["place", "settings", "slice"];
const BUSY = new Set(["queued", "slicing"]);
const meshCache = new Map(); // "job id/file id" -> ArrayBuffer (survives tab changes)

export async function renderJob(view, [jobId, tab = "place"], context) {
  context.setBack("#/");
  context.setTitle(t("job.title"));

  let job, printers;
  try {
    [job, printers] = await Promise.all([api.job(jobId), api.printers()]);
  } catch (e) {
    view.append(el("div", { class: "card muted", text: errorMessage(e) }));
    return;
  }
  const printer = printers.find((p) => p.id === job.printer_id);
  context.setTitle(jobTitle(job));

  const state = { job, printer, listeners: new Set() };
  state.update = (newJob) => {
    state.job = newJob;
    context.setTitle(jobTitle(newJob));
    state.listeners.forEach((listener) => listener(newJob));
  };

  const header = el("div", { class: "card" });
  function renderHeader() {
    const j = state.job;
    const overrides = Object.keys(j.overrides.global || {}).length +
      Object.values(j.overrides.extruders || {}).reduce((n, values) => n + Object.keys(values).length, 0);
    header.replaceChildren(
      el("div", { class: "row" },
        el("div", { class: "grow" },
          el("div", { class: "muted small", text: j.printer_id }),
          el("div", { class: "small", text: describeProfile(j.profile) + (overrides ? " · " + (overrides > 1 ? t("job.changes_many", { n: overrides }) : t("job.changes_one")) : "") })),
        stateBadge(j.state),
        el("button", { class: "small danger", text: t("job.delete"), disabled: BUSY.has(j.state), onclick: remove })));
  }
  state.listeners.add(renderHeader);
  renderHeader();

  async function remove() {
    if (!confirm(t("job.confirm_delete", { name: state.job.name }))) return;
    try {
      await api.deleteJob(jobId);
      context.navigate("#/");
    } catch (e) {
      toast(errorMessage(e), { error: true });
    }
  }

  const tabs = el("div", { class: "tabs", role: "tablist" },
    ...TABS.map((id) => el("button", {
      class: id === tab ? "active" : "", role: "tab", text: t("job.tab." + id),
      onclick: () => location.replace(`#/job/${jobId}/${id}`),
    })));
  const content = el("div", { class: "stack" });
  view.append(el("div", { class: "stack" }, header, tabs, content));

  const render = { place: renderPlace, settings: renderSettings, slice: renderSlice }[tab];
  const cleanup = await render(content, state);
  return () => {
    if (cleanup) cleanup();
    state.listeners.clear();
  };
}

function describeProfile(profile) {
  if (profile.quality_changes) return profile.quality_changes;
  const intent = profile.intent && profile.intent !== "default" ? ` · ${profile.intent}` : "";
  return `${profile.quality}${intent}`;
}

// ================================================================== Place

const selectedObjects = new Map(); // job id -> selected object id (survives tab changes)

// Copies share a name: number them so they can be told apart in the list.
function objectLabels(objects) {
  const totals = {};
  objects.forEach((item) => { totals[item.name] = (totals[item.name] || 0) + 1; });
  const seen = {};
  return new Map(objects.map((item) => {
    seen[item.name] = (seen[item.name] || 0) + 1;
    return [item.id, totals[item.name] > 1 ? `${item.name} #${seen[item.name]}` : item.name];
  }));
}

function boxSize(bbox) {
  return bbox ? bbox.max.map((v, i) => formatNumber(v - bbox.min[i], 1)).join(" × ") + " mm" : "";
}

async function renderPlace(content, state) {
  const jobId = state.job.id;
  const viewerBox = el("div", { class: "viewer" });
  const fitBadge = el("span", { class: "badge" });
  viewerBox.append(el("div", { class: "overlay" }, fitBadge));
  const info = el("div", { class: "stack small" });
  const list = el("div", { class: "object-list" });
  const objectInfo = el("div", { class: "stack small" });
  const busyNote = el("div", { class: "muted small", text: t("place.busy") });
  const progress = el("div", { class: "progress hidden" }, el("div"));
  const buttons = [];

  function button(text, onclick, extra = {}) {
    const b = el("button", { class: "small", text, onclick, ...extra });
    buttons.push(b);
    return b;
  }

  const grid = el("div", { class: "rotate-grid" });
  for (const axis of ["x", "y", "z"]) {
    grid.append(el("span", { class: `axis ${axis}`, text: axis.toUpperCase() }),
      button("↺ 90°", () => rotate(axis, 90), { "aria-label": t("place.rotate", { degrees: 90, axis: axis.toUpperCase() }) }),
      button("↻ 90°", () => rotate(axis, -90), { "aria-label": t("place.rotate", { degrees: -90, axis: axis.toUpperCase() }) }));
  }
  const autoButton = button(t("place.auto_orient"), autoOrient);
  const duplicateButton = button(t("place.duplicate"), duplicate);
  const removeButton = button(t("place.remove"), removeSelected, { class: "small danger" });
  const resetViewButton = el("button", { class: "small", text: t("place.reset_view") });

  // No "accept" filter: iOS does not know the .stl type and would grey the files out.
  const fileInput = el("input", { type: "file", multiple: true, class: "hidden" });
  const addButton = button(t("place.add"), () => fileInput.click());
  const arrangeButton = button(t("place.arrange"), arrange);
  fileInput.addEventListener("change", addFiles);

  const objectsCard = el("div", { class: "card stack" },
    el("h2", { class: "objects-title" }), list,
    el("div", { class: "row wrap" }, addButton, arrangeButton), progress, fileInput);
  content.append(viewerBox,
    el("div", { class: "card stack" }, info),
    objectsCard,
    el("div", { class: "card stack" },
      objectInfo, grid,
      el("div", { class: "row wrap" }, autoButton, duplicateButton, removeButton, el("span", { class: "grow" }), resetViewButton),
      busyNote));

  const viewer = new Viewer(viewerBox);
  resetViewButton.addEventListener("click", () => viewer.resetCamera());
  viewer.onSelect = (id) => select(id);
  if (state.printer) viewer.setPrinter(state.printer);

  let working = false;
  const selected = () => {
    const objects = state.job.objects;
    return objects.find((item) => item.id === selectedObjects.get(jobId)) || objects[0];
  };

  function select(id) {
    selectedObjects.set(jobId, id);
    refresh();
  }

  function refresh() {
    const job = state.job;
    const objects = job.objects;
    const current = selected();
    const several = objects.length > 1;
    const placement = job.placement || {};
    const busy = BUSY.has(job.state);
    const labels = objectLabels(objects);

    buttons.forEach((b) => { b.disabled = working || busy; });
    removeButton.classList.toggle("hidden", !several);
    arrangeButton.classList.toggle("hidden", !several);
    busyNote.classList.toggle("hidden", !busy);

    fitBadge.textContent = placement.fits === false ? t("place.no_fit_badge") : t("place.fits_badge");
    fitBadge.className = "badge " + (placement.fits === false ? "error" : "ok");
    const fitText = several
      ? (placement.fits === false ? t("place.some_no_fit") : t("place.all_fit", { n: objects.length }))
      : (placement.fits === false ? t("place.no_fit") : t("place.fits"));
    info.replaceChildren(el("div", { class: "row" },
      el("span", { class: "grow fit " + (placement.fits === false ? "bad" : "ok"), text: fitText }),
      el("span", { class: "muted", text: boxSize(placement.bbox) })));

    objectsCard.querySelector(".objects-title").textContent = t("place.objects", { n: objects.length });
    list.replaceChildren(...objects.map((item) => {
      const fits = !item.placement || item.placement.fits !== false;
      return el("button", {
        type: "button", class: "object-row" + (item.id === current.id ? " selected" : ""),
        "aria-pressed": item.id === current.id ? "true" : "false", onclick: () => select(item.id),
      },
      el("span", { class: "grow name", text: labels.get(item.id) }),
      fits ? null : el("span", { class: "badge error", text: t("place.no_fit_badge") }),
      el("span", { class: "muted small", text: boxSize(item.placement && item.placement.bbox) }));
    }));

    const warnings = ((current.placement && current.placement.warnings) || []).map((w) => el("li", { text: warningLabel(w) }));
    objectInfo.replaceChildren(...[
      several ? el("b", { text: labels.get(current.id) }) : null,
      warnings.length ? el("ul", { style: "margin: 0; padding-left: 18px" }, ...warnings) : null,
      current.mesh.decimated ? el("div", { class: "muted", text: t("place.decimated", {
        shown: formatNumber(current.mesh.preview_triangles, 0), total: formatNumber(current.mesh.triangles, 0) }) }) : null,
    ].filter(Boolean));

    viewer.setObjects(objects.map((item) => ({
      id: item.id, key: item.file, matrix: item.transform, fits: !item.placement || item.placement.fits !== false,
    })));
    viewer.setSelected(current.id);
    loadMeshes();
  }
  state.listeners.add(refresh);

  // One download per file: copies share it. Cached across tab changes.
  const loading = new Set();
  async function loadMeshes() {
    for (const item of state.job.objects) {
      const key = `${jobId}/${item.file}`;
      if (viewer.hasGeometry(item.file) || loading.has(key)) continue;
      loading.add(key);
      try {
        if (!meshCache.has(key)) meshCache.set(key, await api.mesh(jobId, item.id));
        viewer.addGeometry(item.file, meshCache.get(key));
      } catch (e) {
        toast(errorMessage(e), { error: true });
      } finally {
        loading.delete(key);
      }
      refresh();
    }
  }

  refresh();

  async function apply(request) {
    working = true;
    refresh();
    try {
      state.update(await request());
    } catch (e) {
      toast(errorMessage(e), { error: true, duration: 5000 });
    } finally {
      working = false;
      refresh();
    }
  }

  // Around the model's own centre, so it does not swing across the plate; Cura then drops it
  // onto the plate (and makes room for it if it now overlaps another model).
  function rotate(axis, degrees) {
    const item = selected();
    const { min, max } = item.placement.bbox;
    const [cx, cy, cz] = [0, 1, 2].map((i) => (min[i] + max[i]) / 2);
    const matrix = multiply(translationList(cx, cy, cz),
      multiply(rotationList(axis, degrees), multiply(translationList(-cx, -cy, -cz), item.transform)));
    viewer.setObjectMatrix(item.id, matrix); // Immediate feedback.
    apply(() => api.transform(jobId, item.id, matrix));
  }

  function autoOrient() {
    apply(() => api.autoOrient(jobId, selected().id));
  }

  function duplicate() {
    const before = new Set(state.job.objects.map((item) => item.id));
    apply(async () => {
      const job = await api.duplicate(jobId, selected().id);
      const copy = job.objects.find((item) => !before.has(item.id));
      if (copy) selectedObjects.set(jobId, copy.id);
      return job;
    });
  }

  function removeSelected() {
    const item = selected();
    if (!confirm(t("place.confirm_remove", { name: objectLabels(state.job.objects).get(item.id) }))) return;
    apply(() => api.removeObject(jobId, item.id));
  }

  function arrange() {
    apply(() => api.arrange(jobId));
  }

  async function addFiles() {
    const files = [...fileInput.files];
    fileInput.value = "";
    if (!files.length) return;
    if (files.some((file) => !/\.stl$/i.test(file.name))) {
      toast(t("new.not_stl"), { error: true });
      return;
    }
    const form = new FormData();
    files.forEach((file) => form.append("file", file, file.name));
    const bar = progress.firstChild;
    bar.style.width = "0%";
    progress.classList.remove("hidden");
    const before = new Set(state.job.objects.map((item) => item.id));
    await apply(async () => {
      const job = await api.addObjects(jobId, form, (fraction) => { bar.style.width = Math.round(fraction * 100) + "%"; });
      const added = job.objects.find((item) => !before.has(item.id));
      if (added) selectedObjects.set(jobId, added.id);
      return job;
    });
    progress.classList.add("hidden");
  }

  return () => viewer.dispose();
}

// ================================================================== Settings

const VISIBILITY_KEY = "rwc.visibility";
const SHOW_DISABLED_KEY = "rwc.showDisabled";
const QUICK_KEYS = ["support_enable", "support_structure", "adhesion_type", "infill_sparse_density", "layer_height"];
const openCategories = new Set();

async function renderSettings(content, state) {
  const visibility = el("select", { "aria-label": t("settings.visibility") },
    ...["basic", "advanced", "expert", "all"].map((value) => el("option", { value, text: t("settings." + value) })));
  visibility.value = localStorage.getItem(VISIBILITY_KEY) || "basic";
  const showDisabled = el("input", { type: "checkbox" });
  showDisabled.checked = localStorage.getItem(SHOW_DISABLED_KEY) === "1";
  const extruders = (state.printer && state.printer.extruders) || [];
  const extruderSelect = el("select", { "aria-label": t("settings.extruder") },
    ...extruders.map((e) => el("option", { value: String(e.position), text: e.name })));
  const status = el("div", { class: "muted small" });
  const quick = el("div", { class: "card stack" });
  const tree = el("div");

  content.append(
    el("div", { class: "card stack" },
      el("div", { class: "row" }, el("span", { class: "grow", text: t("settings.show") }), el("span", { style: "width: 60%" }, visibility)),
      extruders.length > 1 ? el("div", { class: "row" }, el("span", { class: "grow", text: t("settings.extruder") }),
        el("span", { style: "width: 60%" }, extruderSelect)) : null,
      el("label", { class: "row" }, el("span", { class: "grow", text: t("settings.show_disabled") }),
        el("span", { class: "switch" }, showDisabled, el("span"))),
      status),
    quick, tree);

  let data = null;
  let pending = false;

  const busy = () => BUSY.has(state.job.state);

  async function load() {
    status.textContent = t("settings.loading");
    try {
      data = await api.settings(state.job.id, {
        visibility: visibility.value, lang: curaLanguage(), extruder: Number(extruderSelect.value || 0),
      });
      status.textContent = busy() ? t("settings.busy") : "";
      draw();
    } catch (e) {
      status.textContent = errorMessage(e);
    }
  }

  function allSettings(nodes, out = []) {
    for (const node of nodes) {
      if (node.type !== "category") out.push(node);
      allSettings(node.children || [], out);
    }
    return out;
  }

  function draw() {
    if (!data) return;
    const settings = allSettings(data.categories);
    const byKey = new Map(settings.map((s) => [s.key, s]));
    const quickNodes = QUICK_KEYS.map((key) => byKey.get(key)).filter((node) => node && (node.enabled || showDisabled.checked));
    quick.classList.toggle("hidden", !quickNodes.length);
    quick.replaceChildren(el("h2", { text: t("settings.quick") }), ...quickNodes.map((node) => settingRow(node, 0, true)));

    tree.replaceChildren(...data.categories.map((category) => {
      const rows = [];
      const walk = (nodes, depth) => {
        for (const node of nodes) {
          if (node.visible && (node.enabled || showDisabled.checked)) rows.push(settingRow(node, depth));
          walk(node.children || [], node.visible ? depth + 1 : depth);
        }
      };
      walk(category.children || [], 0);
      if (!rows.length) return null;
      const details = el("details", { class: "category", open: openCategories.has(category.key) },
        el("summary", {}, el("span", { text: category.label }), el("span", { class: "muted small", text: String(rows.length) })),
        el("div", { class: "settings" }, ...rows));
      details.addEventListener("toggle", () => {
        if (details.open) openCategories.add(category.key); else openCategories.delete(category.key);
      });
      return details;
    }).filter(Boolean));
  }

  function settingRow(node, depth, quickRow = false) {
    const disabled = pending || busy() || !node.enabled;
    const row = el("div", { class: "setting" + (depth ? " child" : "") + (node.overridden ? " overridden" : "") +
      validationClass(node.validation_state) + (pending ? " pending" : "") });
    if (depth > 1) row.style.marginLeft = Math.min(depth, 3) * 14 + "px";
    const description = el("div", { class: "description hidden", text: node.description });
    const infoButton = el("button", { class: "info", type: "button", "aria-label": t("settings.description"), text: "ⓘ",
      onclick: () => description.classList.toggle("hidden") });
    const reset = node.overridden ? el("button", { class: "small", type: "button", text: "↺", title: t("settings.profile_value"),
      "aria-label": t("settings.reset"), disabled, onclick: () => change(node, null) }) : null;
    row.append(
      el("div", { class: "label" }, node.label, node.description ? infoButton : null),
      description,
      el("div", { class: "control" }, control(node, disabled), reset));
    const message = validationMessage(node);
    if (message) row.append(el("div", { class: "validation", text: message }));
    if (quickRow) row.style.borderTop = "none";
    return row;
  }

  function control(node, disabled) {
    const commit = (value) => change(node, value);
    if (node.type === "bool") {
      const input = el("input", { type: "checkbox", disabled });
      input.checked = !!node.value;
      input.addEventListener("change", () => commit(input.checked));
      return el("span", { class: "grow" }, el("span", { class: "switch" }, input, el("span")));
    }
    if (node.type === "enum" && node.options) {
      const select = el("select", { disabled }, ...node.options.map((o) => el("option", { value: o.key, text: o.label })));
      select.value = node.value;
      select.addEventListener("change", () => commit(select.value));
      return select;
    }
    if (node.type === "float" || node.type === "int") {
      const input = el("input", { type: "text", inputmode: node.type === "int" ? "numeric" : "decimal", disabled });
      input.value = formatValue(node.value);
      const save = () => {
        const text = input.value.trim().replace(",", ".");
        if (text === formatValue(node.value).replace(",", ".")) return;
        const number = Number(text);
        if (text === "" || !isFinite(number)) {
          toast(t("settings.enter_number"), { error: true });
          input.value = formatValue(node.value);
          return;
        }
        commit(node.type === "int" ? Math.round(number) : number);
      };
      input.addEventListener("change", save);
      input.addEventListener("keydown", (event) => { if (event.key === "Enter") input.blur(); });
      return el("span", { class: "row grow" }, input, node.unit ? el("span", { class: "unit", text: node.unit }) : null);
    }
    if ((node.type === "extruder" || node.type === "optional_extruder") && extruders.length) {
      const options = extruders.map((e) => el("option", { value: String(e.position), text: e.name }));
      if (node.type === "optional_extruder") options.unshift(el("option", { value: "-1", text: t("settings.none") }));
      const select = el("select", { disabled }, ...options);
      select.value = String(node.value);
      select.addEventListener("change", () => commit(Number(select.value)));
      return select;
    }
    if (node.type === "str") {
      const input = el("input", { type: "text", disabled });
      input.value = node.value || "";
      input.addEventListener("change", () => commit(input.value));
      return input;
    }
    return el("span", { class: "grow muted small", text: JSON.stringify(node.value) }); // Lists, polygons: read only.
  }

  async function change(node, value) {
    if (pending) return;
    pending = true;
    status.textContent = t("settings.recalculating");
    const previous = node.value;
    if (value !== null) node.value = value; // Show the new value while Cura recalculates.
    draw();
    const request = node.settable_per_extruder
      ? { scope: "extruder", extruder: Number(extruderSelect.value || 0), key: node.key, value }
      : { scope: "global", key: node.key, value };
    try {
      const result = await api.patchSetting(state.job.id, request);
      const changed = result.changed.length;
      state.update({ ...state.job, overrides: result.overrides, state: "ready", result: null, error: null });
      toast(changed > 1 ? t("settings.recalculated", { n: changed }) : t("settings.saved"));
    } catch (e) {
      node.value = previous;
      toast(errorMessage(e), { error: true, duration: 5000 });
    } finally {
      pending = false;
    }
    await load(); // Fresh values, limits and validation from Cura.
  }

  visibility.addEventListener("change", () => { localStorage.setItem(VISIBILITY_KEY, visibility.value); load(); });
  showDisabled.addEventListener("change", () => { localStorage.setItem(SHOW_DISABLED_KEY, showDisabled.checked ? "1" : "0"); draw(); });
  extruderSelect.addEventListener("change", load);
  await load();
}

function formatValue(value) {
  if (value === null || value === undefined) return "";
  if (typeof value === "number") return String(Math.round(value * 10000) / 10000);
  return String(value);
}

function validationClass(state) {
  if (state === "MinimumWarning" || state === "MaximumWarning") return " warning";
  if (state && state !== "Valid" && state !== "Unknown") return " error";
  return "";
}

const limit = (value) => (value != null ? ` (${formatValue(value)})` : "");

function validationMessage(node) {
  switch (node.validation_state) {
    case "MinimumWarning": return t("settings.below_warning") + limit(node.minimum_value_warning);
    case "MaximumWarning": return t("settings.above_warning") + limit(node.maximum_value_warning);
    case "MinimumError": return t("settings.below_min", { limit: limit(node.minimum_value) });
    case "MaximumError": return t("settings.above_max", { limit: limit(node.maximum_value) });
    case "Invalid": case "Exception": return t("settings.invalid");
    default: return "";
  }
}

// ================================================================== Slice

async function renderSlice(content, state) {
  const panel = el("div", { class: "stack" });
  content.append(panel);
  let timer = null;
  let stopped = false;

  function draw() {
    const job = state.job;
    const fits = !job.placement || job.placement.fits !== false;
    const parts = [];
    if (job.state === "queued" || job.state === "slicing") {
      const percent = Math.round((job.progress || 0) * 100);
      parts.push(el("div", { class: "card stack" },
        el("div", { class: "row" },
          el("span", { class: "grow", text: job.state === "queued" ? t("slice.queued") : t("slice.slicing") }),
          el("b", { text: `${percent} %` })),
        el("div", { class: "progress" }, el("div", { style: `width: ${percent}%` })),
        el("button", { class: "block danger", text: t("slice.cancel"), onclick: cancel })));
    } else {
      if (job.state === "error" && job.error) {
        parts.push(el("div", { class: "card", style: "border-color: var(--error)" },
          el("b", { text: t("slice.failed") }), el("div", { class: "small", text: messageFor(job.error) })));
      }
      if (job.state === "done" && job.result) parts.push(resultCard(job));
      parts.push(el("button", {
        class: job.state === "done" ? "block" : "primary block", disabled: !fits,
        text: job.state === "done" ? t("slice.again") : t("slice.slice"), onclick: slice,
      }));
      if (!fits) parts.push(el("div", { class: "muted small center", text: t("slice.no_fit") }));
    }
    panel.replaceChildren(...parts);
  }

  function resultCard(job) {
    const result = job.result;
    const materials = (result.material || []).filter((m) => m.length_m > 0 || m.weight_g > 0);
    const features = Object.entries(result.features || {}).sort((a, b) => b[1] - a[1]);
    const canShare = !!(navigator.canShare && navigator.share);
    return el("div", { class: "card stack" },
      el("div", {},
        el("div", { class: "stat" }, el("span", { text: t("slice.time") }), el("b", { text: formatDuration(result.print_time_s) })),
        ...materials.map((m) => el("div", { class: "stat" },
          el("span", { text: materials.length > 1 ? t("slice.material_n", { n: m.extruder + 1, name: m.name }) : t("slice.material", { name: m.name }) }),
          el("b", { text: `${formatNumber(m.length_m)} m · ${formatNumber(m.weight_g, 0)} g` +
            (m.cost > 0 ? ` · ${formatNumber(m.cost)} ${result.currency || "€"}` : "") }))),
        el("div", { class: "stat" }, el("span", { text: "G-code" }), el("b", { text: `${formatNumber((result.gcode_bytes || 0) / 1048576, 1)} MB` }))),
      features.length ? el("details", {},
        el("summary", { class: "muted small", text: t("slice.feature_times") }),
        ...features.map(([label, seconds]) => el("div", { class: "stat small" }, el("span", { text: label }), el("span", { text: formatDuration(seconds) })))) : null,
      el("div", { class: "row" },
        el("button", { class: "primary grow", text: t("slice.download"), onclick: () => download(false) }),
        canShare ? el("button", { class: "grow", text: t("slice.share"), onclick: () => download(true) }) : null));
  }

  async function poll() {
    clearTimeout(timer);
    if (stopped) return;
    try {
      state.update(await api.job(state.job.id));
    } catch (e) {
      toast(errorMessage(e), { error: true });
    }
    if (!stopped && BUSY.has(state.job.state)) timer = setTimeout(poll, 1500);
  }

  async function slice() {
    try {
      state.update(await api.slice(state.job.id));
      poll();
    } catch (e) {
      toast(errorMessage(e), { error: true, duration: 5000 });
    }
  }

  async function cancel() {
    try {
      state.update(await api.cancel(state.job.id));
      poll();
    } catch (e) {
      toast(errorMessage(e), { error: true });
    }
  }

  async function download(share) {
    toast(t("slice.downloading"), { duration: 60000 });
    try {
      const response = await api.gcode(state.job.id);
      const blob = await response.blob();
      const fallback = (state.job.result && state.job.result.job_name ? state.job.result.job_name : t("slice.default_name")) + ".gcode";
      const filename = filenameFromDisposition(response.headers.get("Content-Disposition"), fallback);
      if (share) {
        const file = new File([blob], filename, { type: "text/plain" });
        if (navigator.canShare({ files: [file] })) {
          document.getElementById("toast").hidden = true;
          await navigator.share({ files: [file], title: filename });
          return;
        }
      }
      saveBlob(blob, filename);
      toast(t("slice.downloaded"));
    } catch (e) {
      if (e && e.name === "AbortError") { document.getElementById("toast").hidden = true; return; } // Share sheet closed.
      toast(errorMessage(e), { error: true });
    }
  }

  state.listeners.add(draw);
  draw();
  if (BUSY.has(state.job.state)) poll();
  return () => {
    stopped = true;
    clearTimeout(timer);
  };
}

