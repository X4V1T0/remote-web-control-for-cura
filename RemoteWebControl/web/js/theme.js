// Colour theme: "dark" (the default), "light" (UltiMaker Cura's light palette) or "auto" (follows
// the system). A classic script loaded in <head>, so the theme is set before the first paint (the
// Content-Security-Policy allows no inline scripts). The modules use window.rwcTheme and listen
// to the "rwc-themechange" event.
(function () {
  "use strict";

  var KEY = "rwc.theme";
  var CHOICES = ["dark", "light", "auto"];
  // Browser bar: the page background in dark, Cura's header blue in light.
  var BAR_COLORS = { dark: "#181c24", light: "#08073f" };
  var systemLight = window.matchMedia ? window.matchMedia("(prefers-color-scheme: light)") : null;

  function stored() {
    try {
      var value = localStorage.getItem(KEY);
      return CHOICES.indexOf(value) >= 0 ? value : "dark";
    } catch (e) {
      return "dark";
    }
  }

  var choice = stored();

  function resolved() {
    if (choice !== "auto") return choice;
    return systemLight && systemLight.matches ? "light" : "dark";
  }

  function apply() {
    var theme = resolved();
    var root = document.documentElement;
    var changed = root.dataset.theme !== theme;
    root.dataset.theme = theme;
    var meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute("content", BAR_COLORS[theme]);
    if (changed) window.dispatchEvent(new CustomEvent("rwc-themechange", { detail: { theme: theme } }));
  }

  window.rwcTheme = {
    choices: CHOICES.slice(),
    // What the user picked ("auto" included) and the theme in use ("dark" or "light").
    choice: function () { return choice; },
    theme: function () { return resolved(); },
    set: function (value) {
      if (CHOICES.indexOf(value) < 0) return;
      choice = value;
      try { localStorage.setItem(KEY, value); } catch (e) { /* Private mode: works until reload. */ }
      apply();
    },
  };

  if (systemLight) {
    var onSystemChange = function () { if (choice === "auto") apply(); };
    if (systemLight.addEventListener) systemLight.addEventListener("change", onSystemChange);
    else if (systemLight.addListener) systemLight.addListener(onSystemChange); // Safari < 14.
  }

  apply();
})();
