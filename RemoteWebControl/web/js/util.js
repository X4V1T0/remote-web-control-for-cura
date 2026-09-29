// Small DOM and formatting helpers. Text always goes through textContent (never innerHTML),
// so names coming from files or Cura cannot inject markup.

import { LANG, LOCALE, t } from "./i18n.js";

export function el(tag, attributes = {}, ...children) {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(attributes || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (name === "class") node.className = value;
    else if (name === "text") node.textContent = value;
    else if (name.startsWith("on") && typeof value === "function") node.addEventListener(name.slice(2), value);
    else if (value === true) node.setAttribute(name, "");
    else node.setAttribute(name, value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

let toastTimer = null;

export function toast(message, { error = false, duration = 3500 } = {}) {
  const node = document.getElementById("toast");
  node.textContent = message;
  node.className = error ? "error" : "";
  node.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { node.hidden = true; }, duration);
}

export function formatDuration(seconds) {
  seconds = Math.max(0, Math.round(seconds || 0));
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.round((seconds % 3600) / 60);
  if (days) return `${days} d ${hours} h`;
  if (hours) return `${hours} h ${minutes} min`;
  return `${Math.max(1, minutes)} min`;
}

export function formatDate(iso) {
  const date = new Date(iso);
  if (isNaN(date)) return "";
  const now = new Date();
  const time = date.toLocaleTimeString(LOCALE, { hour: "2-digit", minute: "2-digit" });
  if (date.toDateString() === now.toDateString()) return t("date.today", { time });
  const yesterday = new Date(now);
  yesterday.setDate(now.getDate() - 1);
  if (date.toDateString() === yesterday.toDateString()) return t("date.yesterday", { time });
  return date.toLocaleDateString(LOCALE, { day: "numeric", month: "short" }) + " " + time;
}

export function formatNumber(value, decimals = 2) {
  return Number(value).toLocaleString(LOCALE, { maximumFractionDigits: decimals });
}

const STATE_KINDS = { created: "busy", ready: "", queued: "busy", slicing: "busy", done: "ok", error: "error" };

export function stateBadge(state) {
  const label = state in STATE_KINDS ? t("state." + state) : state;
  return el("span", { class: "badge " + (STATE_KINDS[state] || ""), text: label });
}

const WARNING_CODES = new Set([
  "outside_build_volume", "disallowed_area", "extruder_disabled", "not_printable", "scaled", "mirrored",
]);

export function warningLabel(warning) {
  return WARNING_CODES.has(warning.code) ? t("warning." + warning.code) : warning.message;
}

// The language for setting labels, the same as the app. Cura's texts are English; en_US has no
// translation file, so Cura returns them untranslated.
export function curaLanguage() {
  return LANG === "es" ? "es_ES" : "en_US";
}

export function saveBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const link = el("a", { href: url, download: filename });
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 60000);
}

// filename*=UTF-8''... wins over filename="..." (RFC 6266).
export function filenameFromDisposition(header, fallback) {
  if (!header) return fallback;
  const encoded = /filename\*=UTF-8''([^;]+)/i.exec(header);
  if (encoded) {
    try { return decodeURIComponent(encoded[1]); } catch { /* fall through */ }
  }
  const plain = /filename="([^"]+)"/i.exec(header);
  return plain ? plain[1] : fallback;
}
