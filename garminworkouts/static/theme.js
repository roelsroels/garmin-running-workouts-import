// Apply saved preferences before styles load to avoid flashing the wrong theme.
(() => {
  const storageKey = "garmin-running-planner-theme";
  const modes = ["system", "light", "dark"];
  const system = window.matchMedia("(prefers-color-scheme: dark)");
  const normalize = (value) => modes.includes(value) ? value : "system";

  function readPreference() {
    try {
      return normalize(window.localStorage.getItem(storageKey));
    } catch {
      return "system";
    }
  }

  let preference = readPreference();

  function applyTheme() {
    document.documentElement.dataset.theme = preference === "system"
      ? (system.matches ? "dark" : "light") : preference;
    document.querySelectorAll("[data-theme-choice]").forEach((button) => {
      button.setAttribute("aria-pressed", String(button.dataset.themeChoice === preference));
    });
  }

  applyTheme();
  system.addEventListener("change", applyTheme);

  document.addEventListener("DOMContentLoaded", () => {
    const control = document.querySelector("#theme-switch");
    if (!control) return;
    control.hidden = false;
    control.addEventListener("click", (event) => {
      const button = event.target.closest("[data-theme-choice]");
      if (!button || !control.contains(button)) return;
      preference = normalize(button.dataset.themeChoice);
      try {
        if (preference === "system") window.localStorage.removeItem(storageKey);
        else window.localStorage.setItem(storageKey, preference);
      } catch {
        // Manual switching still works if the browser disallows storage.
      }
      applyTheme();
    });
    applyTheme();
  });

  window.addEventListener("storage", (event) => {
    if (event.key === storageKey || event.key === null) {
      preference = normalize(event.newValue);
      applyTheme();
    }
  });
  window.addEventListener("pageshow", (event) => {
    if (event.persisted) {
      preference = readPreference();
      applyTheme();
    }
  });
})();
