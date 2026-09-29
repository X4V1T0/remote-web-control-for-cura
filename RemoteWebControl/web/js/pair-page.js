// Pairing page. On Cura's PC it works right away (opened from "Extensions > Remote Web Control >
// Pair a phone"); from any other computer (e.g. Cura in Docker) it needs the API token once.

import { ApiError, api, errorMessage, setToken } from "./api.js";
import { t, translatePage } from "./i18n.js";

const $ = (id) => document.getElementById(id);
const LOOPBACK = new Set(["127.0.0.1", "localhost", "[::1]", "::1"]);
let countdown = null;

function qrDataUrl(text) {
  const qr = window.qrcode(0, "M"); // vendor/qrcode.js (qrcode-generator), type 0 = automatic size.
  qr.addData(text);
  qr.make();
  return qr.createDataURL(8, 2);
}

// The phone must open the app with an address it can reach. On Cura's PC the page is on
// 127.0.0.1, so use the LAN address the server reports; elsewhere, the address this page was
// opened with (inside Docker the server only knows its container address).
function appUrl(serverUrls) {
  if (LOOPBACK.has(location.hostname) && serverUrls.length) return serverUrls[0];
  return location.origin + "/";
}

async function renew() {
  $("error").textContent = "";
  $("renew").disabled = true;
  try {
    const pairing = await api.startPairing();
    $("token-form").classList.add("hidden");
    const base = appUrl(pairing.urls);
    $("pin").textContent = pairing.pin;
    $("url").textContent = base;
    $("token").textContent = pairing.token;
    $("qr").src = qrDataUrl(`${base}?pin=${pairing.pin}`);
    startCountdown(pairing.expires_in);
  } catch (e) {
    if (e instanceof ApiError && (e.code === "local_only" || e.code === "unauthorized")) {
      $("token-form").classList.remove("hidden");
      $("token-input").focus();
    } else {
      $("error").textContent = errorMessage(e);
    }
  } finally {
    $("renew").disabled = false;
  }
}

function startCountdown(seconds) {
  clearInterval(countdown);
  const end = Date.now() + seconds * 1000;
  const tick = () => {
    const left = Math.max(0, Math.round((end - Date.now()) / 1000));
    $("expires").textContent = left
      ? t("pairpage.expires_in", { time: `${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}` })
      : t("pairpage.expired");
    $("qr").style.opacity = left ? "1" : "0.2";
    if (!left) clearInterval(countdown);
  };
  tick();
  countdown = setInterval(tick, 1000);
}

translatePage();
document.title = t("pairpage.title");
$("renew").addEventListener("click", renew);
$("token-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const token = $("token-input").value.trim();
  if (!token) return;
  setToken(token);
  renew();
});
renew();
