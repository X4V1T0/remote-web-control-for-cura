// New job: STL file + printer + profile (+ auto-orientation), uploaded with progress.

import { api, errorMessage } from "../api.js";
import { el, formatNumber, toast } from "../util.js";
import { t } from "../i18n.js";

const AUTO_ORIENT_KEY = "rwc.autoOrient";

export async function renderNewJob(view, _params, context) {
  context.setTitle(t("new.title"));
  context.setBack("#/");

  // No "accept" filter: iOS does not know the .stl type and would grey the files out.
  const fileInput = el("input", { type: "file", class: "hidden" });
  const fileLabel = el("span", { class: "grow muted", text: t("new.no_file") });
  const pickButton = el("button", { type: "button", text: t("new.pick"), onclick: () => fileInput.click() });
  const printerSelect = el("select", { "aria-label": t("new.printer") });
  const profileSelect = el("select", { "aria-label": t("new.profile") });
  const autoOrient = el("input", { type: "checkbox" });
  autoOrient.checked = localStorage.getItem(AUTO_ORIENT_KEY) === "1";
  const submit = el("button", { class: "primary block", type: "submit", text: t("new.submit"), disabled: true });
  const progress = el("div", { class: "progress hidden" }, el("div"));
  const status = el("div", { class: "muted small center" });

  fileInput.addEventListener("change", () => {
    const file = fileInput.files[0];
    fileLabel.textContent = file ? `${file.name} (${formatNumber(file.size / 1048576, 1)} MB)` : t("new.no_file");
    fileLabel.classList.toggle("muted", !file);
    updateSubmit();
  });

  function updateSubmit() {
    submit.disabled = !(fileInput.files[0] && printerSelect.value && profileSelect.value);
  }

  view.append(el("form", { class: "stack", onsubmit: upload },
    el("div", { class: "card stack" },
      el("div", { class: "row" }, fileLabel, pickButton), fileInput),
    el("div", { class: "card stack" },
      el("label", { class: "field" }, el("span", { text: t("new.printer") }), printerSelect),
      el("label", { class: "field" }, el("span", { text: t("new.profile") }), profileSelect),
      el("label", { class: "row" },
        el("span", { class: "grow" }, t("new.auto_orient"),
          el("div", { class: "muted small", text: t("new.auto_orient_hint") })),
        el("span", { class: "switch" }, autoOrient, el("span")))),
    submit, progress, status));

  let printers;
  try {
    printers = await api.printers();
  } catch (e) {
    status.textContent = errorMessage(e);
    return;
  }
  printerSelect.replaceChildren(...printers.map((p) => el("option", { value: p.id, text: p.name })));
  const active = printers.find((p) => p.active) || printers[0];
  if (active) printerSelect.value = active.id;
  printerSelect.addEventListener("change", loadProfiles);
  profileSelect.addEventListener("change", updateSubmit);
  await loadProfiles();

  async function loadProfiles() {
    profileSelect.replaceChildren(el("option", { value: "", text: t("new.loading_profiles") }));
    updateSubmit();
    try {
      const profiles = await api.profiles(printerSelect.value);
      fillProfiles(profileSelect, profiles);
    } catch (e) {
      profileSelect.replaceChildren(el("option", { value: "", text: errorMessage(e) }));
    }
    updateSubmit();
  }

  async function upload(event) {
    event.preventDefault();
    const file = fileInput.files[0];
    if (!file) return;
    if (!/\.stl$/i.test(file.name)) {
      toast(t("new.not_stl"), { error: true });
      return;
    }
    localStorage.setItem(AUTO_ORIENT_KEY, autoOrient.checked ? "1" : "0");
    const form = new FormData();
    form.append("file", file, file.name);
    form.append("printer_id", printerSelect.value);
    form.append("profile", profileSelect.value);
    form.append("auto_orient", autoOrient.checked ? "true" : "false");

    submit.disabled = true;
    progress.classList.remove("hidden");
    const bar = progress.firstChild;
    status.textContent = t("new.uploading");
    try {
      const job = await api.createJob(form, (fraction) => {
        bar.style.width = Math.round(fraction * 100) + "%";
        if (fraction >= 1) status.textContent = autoOrient.checked
          ? t("new.placing_orienting") : t("new.placing");
      });
      context.navigate(`#/job/${job.id}/place`);
    } catch (e) {
      toast(errorMessage(e), { error: true, duration: 6000 });
      status.textContent = "";
      progress.classList.add("hidden");
      updateSubmit();
    }
  }
}

export function profileLabel(quality, intent) {
  const layer = quality.layer_height ? ` · ${formatNumber(quality.layer_height, 3)} mm` : "";
  return intent && intent.intent_category !== "default" ? `${intent.name} · ${quality.name}${layer}` : `${quality.name}${layer}`;
}

function fillProfiles(select, profiles) {
  const options = [];
  const builtIn = el("optgroup", { label: t("new.cura_profiles") });
  for (const intent of profiles.intents) {
    for (const quality of profiles.qualities) {
      if (!quality.available || !intent.quality_types.includes(quality.quality_type)) continue;
      const value = JSON.stringify({ quality: quality.quality_type, intent: intent.intent_category, quality_changes: null });
      builtIn.append(el("option", { value, text: profileLabel(quality, intent) }));
      options.push([value, { quality: quality.quality_type, intent: intent.intent_category, quality_changes: null }]);
    }
  }
  const custom = el("optgroup", { label: t("new.my_profiles") });
  for (const profile of profiles.quality_changes.filter((p) => p.available)) {
    const value = JSON.stringify({ quality_changes: profile.name });
    custom.append(el("option", { value, text: profile.name }));
    options.push([value, { quality_changes: profile.name }]);
  }
  select.replaceChildren(...[custom, builtIn].filter((group) => group.children.length));

  const active = profiles.active;
  const match = options.find(([, p]) => active.quality_changes
    ? p.quality_changes === active.quality_changes
    : !p.quality_changes && p.quality === active.quality && p.intent === active.intent);
  if (match) select.value = match[0];
}
