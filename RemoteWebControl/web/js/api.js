// Client of the Remote Web Control API (same origin). Keeps the token in localStorage.

import { has, t } from "./i18n.js";

const TOKEN_KEY = "rwc.token";

export class ApiError extends Error {
  constructor(status, code, message) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

// Translated messages for the error codes the user can do something about (i18n.js, "error.*").
const message = (code) => (has("error." + code) ? t("error." + code) : "");

// For {code, message} objects stored in jobs.
export function messageFor(error) {
  if (!error) return "";
  if (error.code === "setting_error" && error.message && error.message.includes(": ")) {
    return message("setting_error").replace(/\.$/, "") + ": " + error.message.split(": ").slice(1).join(": ");
  }
  return message(error.code) || error.message || "";
}

export function errorMessage(error) {
  if (error instanceof ApiError) return messageFor(error);
  return error && error.message ? error.message : String(error);
}

export function getToken() {
  try { return localStorage.getItem(TOKEN_KEY) || ""; } catch { return ""; }
}

export function setToken(token) {
  try { localStorage.setItem(TOKEN_KEY, token); } catch { /* Private mode: works until reload. */ }
  memoryToken = token;
}

export function clearToken() {
  try { localStorage.removeItem(TOKEN_KEY); } catch { /* ignore */ }
  memoryToken = "";
}

let memoryToken = "";
const unauthorizedListeners = new Set();

export function onUnauthorized(listener) {
  unauthorizedListeners.add(listener);
}

function authHeaders() {
  const token = getToken() || memoryToken;
  return token ? { Authorization: "Bearer " + token } : {};
}

async function toApiError(response) {
  let code = "http_" + response.status;
  let message = response.statusText || "Error " + response.status;
  try {
    const body = await response.json();
    if (body && body.error) ({ code, message } = body.error);
  } catch { /* Not JSON. */ }
  return new ApiError(response.status, code, message);
}

async function request(method, path, { json, body, headers = {}, as = "json", auth = true } = {}) {
  const init = { method, headers: { ...(auth ? authHeaders() : {}), ...headers } };
  if (json !== undefined) {
    init.body = JSON.stringify(json);
    init.headers["Content-Type"] = "application/json";
  } else if (body !== undefined) {
    init.body = body;
  }
  let response;
  try {
    response = await fetch(path, init);
  } catch (e) {
    throw new ApiError(0, "network", e.message);
  }
  if (!response.ok) {
    const error = await toApiError(response);
    if (response.status === 401) unauthorizedListeners.forEach((listener) => listener(error));
    throw error;
  }
  if (response.status === 204) return null;
  if (as === "json") return response.json();
  if (as === "arrayBuffer") return response.arrayBuffer();
  return response;
}

const enc = encodeURIComponent;

export const api = {
  health: () => request("GET", "/api/health"),
  printers: () => request("GET", "/api/printers"),
  profiles: (printerId) => request("GET", `/api/printers/${enc(printerId)}/profiles`),

  jobs: () => request("GET", "/api/jobs"),
  job: (id) => request("GET", `/api/jobs/${id}`),
  deleteJob: (id) => request("DELETE", `/api/jobs/${id}`),
  mesh: (id) => request("GET", `/api/jobs/${id}/mesh`, { as: "arrayBuffer" }),
  transform: (id, matrix) => request("PUT", `/api/jobs/${id}/transform`, { json: { matrix } }),
  autoOrient: (id) => request("POST", `/api/jobs/${id}/auto-orient`),
  settings: (id, { visibility, lang, extruder }) =>
    request("GET", `/api/jobs/${id}/settings?visibility=${enc(visibility)}&lang=${enc(lang)}&extruder=${extruder}`),
  patchSetting: (id, change) => request("PATCH", `/api/jobs/${id}/settings`, { json: change }),
  slice: (id) => request("POST", `/api/jobs/${id}/slice`),
  cancel: (id) => request("POST", `/api/jobs/${id}/cancel`),
  gcode: (id) => request("GET", `/api/jobs/${id}/gcode`, { as: "response" }),

  claimPin: (pin) => request("POST", "/api/pairing/claim", { json: { pin }, auth: false }),
  // Sends the token when there is one: needed when the page is not opened on Cura's own PC (Docker).
  startPairing: () => request("POST", "/api/pairing/start"),

  // XMLHttpRequest instead of fetch: fetch has no upload progress.
  createJob(formData, onProgress) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", "/api/jobs");
      for (const [name, value] of Object.entries(authHeaders())) xhr.setRequestHeader(name, value);
      xhr.upload.onprogress = (event) => {
        if (event.lengthComputable && onProgress) onProgress(event.loaded / event.total);
      };
      xhr.onload = () => {
        let body = null;
        try { body = JSON.parse(xhr.responseText); } catch { /* ignore */ }
        if (xhr.status >= 200 && xhr.status < 300) return resolve(body);
        const error = body && body.error
          ? new ApiError(xhr.status, body.error.code, body.error.message)
          : new ApiError(xhr.status, "http_" + xhr.status, "Error " + xhr.status);
        if (xhr.status === 401) unauthorizedListeners.forEach((listener) => listener(error));
        reject(error);
      };
      xhr.onerror = () => reject(new ApiError(0, "network", "Network error"));
      xhr.send(formData);
    });
  },
};
