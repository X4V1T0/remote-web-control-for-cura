// Pairing: exchange the PIN shown on the PC for the API token (or paste the token).

import { api, errorMessage, setToken } from "../api.js";
import { el, toast } from "../util.js";
import { t } from "../i18n.js";

export function renderPair(view, _params, context) {
  context.setTitle(t("pair.title"));

  const pinInput = el("input", {
    class: "pin-input", inputmode: "numeric", autocomplete: "one-time-code", maxlength: "6",
    pattern: "[0-9]*", placeholder: "······", "aria-label": t("pair.pin_label"),
  });
  const pairButton = el("button", { class: "primary block", type: "submit", text: t("pair.button") });

  async function claim(event) {
    event.preventDefault();
    const pin = pinInput.value.replace(/\D/g, "");
    if (pin.length !== 6) {
      toast(t("pair.pin_digits"), { error: true });
      return;
    }
    pairButton.disabled = true;
    try {
      const { token } = await api.claimPin(pin);
      setToken(token);
      await api.health();
      toast(t("app.paired"));
      context.navigate("#/");
    } catch (e) {
      toast(errorMessage(e), { error: true, duration: 5000 });
    } finally {
      pairButton.disabled = false;
    }
  }

  pinInput.addEventListener("input", () => {
    pinInput.value = pinInput.value.replace(/\D/g, "").slice(0, 6);
  });

  const tokenInput = el("textarea", { rows: "3", placeholder: t("pair.token_placeholder"), autocomplete: "off", spellcheck: "false" });
  const tokenButton = el("button", { class: "block", type: "submit", text: t("pair.use_token") });

  async function useToken(event) {
    event.preventDefault();
    const token = tokenInput.value.trim();
    if (!token) return;
    setToken(token);
    tokenButton.disabled = true;
    try {
      await api.health();
      toast(t("app.paired"));
      context.navigate("#/");
    } catch (e) {
      toast(errorMessage(e), { error: true });
    } finally {
      tokenButton.disabled = false;
    }
  }

  view.append(el("div", { class: "stack" },
    el("div", { class: "card stack" },
      el("h2", { text: t("pair.heading") }),
      el("p", { class: "muted", text: t("pair.help") }),
      el("form", { class: "stack", onsubmit: claim }, pinInput, pairButton),
    ),
    el("details", { class: "card" },
      el("summary", { text: t("pair.have_token") }),
      el("form", { class: "stack", onsubmit: useToken, style: "margin-top: 12px" }, tokenInput, tokenButton),
    ),
    el("p", { class: "muted small", text: t("pair.ios_note") }),
  ));
  pinInput.focus();
}
