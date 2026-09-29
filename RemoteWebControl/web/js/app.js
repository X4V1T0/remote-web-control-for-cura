// App shell: hash router, pairing on start-up and the top bar.

import { api, clearToken, errorMessage, getToken, onUnauthorized, setToken } from "./api.js";
import { toast } from "./util.js";
import { t, translatePage } from "./i18n.js";
import { renderJobs } from "./pages/jobs.js";
import { renderNewJob } from "./pages/new-job.js";
import { renderJob } from "./pages/job.js";
import { renderPair } from "./pages/pair.js";

const view = document.getElementById("view");
const title = document.getElementById("title");
const back = document.getElementById("back");

const routes = [
  [/^#?\/?$/, renderJobs],
  [/^#\/new$/, renderNewJob],
  [/^#\/job\/([0-9a-f]{32})(?:\/(place|settings|slice))?$/, renderJob],
  [/^#\/pair$/, renderPair],
];

let cleanup = null;
let backTarget = null;

const context = {
  setTitle(text) {
    title.textContent = text;
    document.title = text === t("app.name") ? text : text + " · " + t("app.name");
  },
  setBack(target) {
    backTarget = target;
    back.hidden = !target;
  },
  navigate(hash) {
    if (location.hash === hash) route();
    else location.hash = hash;
  },
};

back.addEventListener("click", () => {
  if (backTarget) context.navigate(backTarget);
});

async function route() {
  const hash = location.hash || "#/";
  if (!getToken() && hash !== "#/pair") {
    location.replace("#/pair");
    return;
  }
  if (cleanup) {
    try { cleanup(); } catch (e) { console.error(e); }
    cleanup = null;
  }
  view.replaceChildren();
  context.setBack(null);
  window.scrollTo(0, 0);
  for (const [pattern, render] of routes) {
    const match = pattern.exec(hash);
    if (match) {
      try {
        cleanup = (await render(view, match.slice(1), context)) || null;
      } catch (e) {
        console.error(e);
        toast(errorMessage(e), { error: true });
      }
      return;
    }
  }
  location.replace("#/");
}

onUnauthorized(() => {
  clearToken();
  if (location.hash !== "#/pair") location.replace("#/pair");
});

async function start() {
  translatePage();
  // Pairing QR codes open "/?pin=123456": claim the token and clean the URL.
  const params = new URLSearchParams(location.search);
  const pin = params.get("pin");
  if (pin) {
    history.replaceState(null, "", "/" + (location.hash || ""));
    try {
      const { token } = await api.claimPin(pin);
      setToken(token);
      toast(t("app.paired"));
      if (location.hash === "#/pair") location.replace("#/");
    } catch (e) {
      toast(errorMessage(e), { error: true, duration: 6000 });
    }
  }
  window.addEventListener("hashchange", route);
  route();
}

start();
