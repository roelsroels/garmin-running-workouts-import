function refreshStepNumbers() {
  document.querySelectorAll(".step-row").forEach((row, index) => {
    const number = row.querySelector(".step-number");
    if (number) number.textContent = String(index + 1);
  });
}

function refreshGoalFields() {
  const selector = document.querySelector("#goal-type");
  if (!selector) return;
  const visible = {
    complete_distance: ["goal-distance"],
    target_time: ["goal-distance", "goal-time"],
    sustain_pace: ["goal-pace", "goal-duration"],
    endurance: [],
    speed: ["goal-pace"],
    consistency: [],
  }[selector.value] || [];
  document.querySelectorAll(".goal-field").forEach((field) => {
    field.hidden = !visible.some((name) => field.classList.contains(name));
  });
}

function refreshLLMFields(providerChanged = false) {
  const form = document.querySelector("#llm-settings");
  if (!form) return;
  const provider = form.elements.llm_provider.value;
  const defaults = JSON.parse(form.dataset.providerDefaults)[provider];
  if (providerChanged) {
    for (const field of ["base_url", "model", "api_key_env"]) {
      form.elements[`llm_${field}`].value = defaults[field];
    }
    // Never carry a typed key across providers/endpoints.
    form.elements.llm_api_key.value = "";
    form.elements.clear_llm_key.checked = false;
    form.querySelector("#llm-key-status").textContent =
      "Enter a key for this provider, or use its configured environment variable. Availability is checked when you save.";
  }
  form.querySelectorAll(".llm-field").forEach((field) => {
    field.hidden = provider === "none";
    field.querySelectorAll("input").forEach((input) => { input.disabled = provider === "none"; });
  });
  form.elements.llm_base_url.readOnly = provider === "anthropic";
  form.elements.llm_model.required = provider !== "none";
}

function showWaitState(form) {
  const overlay = document.querySelector("#wait-overlay");
  if (!overlay) return;
  const title = overlay.querySelector("#wait-title");
  const detail = overlay.querySelector("#wait-detail");
  if (title) title.textContent = form.dataset.loadingMessage || "Working on it…";
  if (detail) {
    detail.textContent =
      form.dataset.loadingDetail || "Please keep this page open while the request finishes.";
  }
  form.setAttribute("aria-busy", "true");
  document.body.classList.add("is-waiting");
  overlay.hidden = false;
}

function hideWaitState() {
  const overlay = document.querySelector("#wait-overlay");
  if (overlay) overlay.hidden = true;
  document.body.classList.remove("is-waiting");
  document.querySelectorAll('form[aria-busy="true"]').forEach((form) => form.removeAttribute("aria-busy"));
}

document.addEventListener("click", (event) => {
  if (event.target.closest("#add-step")) {
    const list = document.querySelector("#steps");
    const template = document.querySelector("#step-template");
    if (list && template && list.children.length < 20) {
      list.append(template.content.cloneNode(true));
      refreshStepNumbers();
    }
  }
  const remove = event.target.closest(".remove-step");
  if (remove) {
    const list = document.querySelector("#steps");
    if (list && list.children.length > 1) {
      remove.closest(".step-row").remove();
      refreshStepNumbers();
    }
  }
});

document.addEventListener("change", (event) => {
  if (event.target.matches("#goal-type")) refreshGoalFields();
  if (event.target.matches("#llm-provider")) refreshLLMFields(true);
});

document.addEventListener("submit", (event) => {
  const form = event.target.closest("form[data-loading-message]");
  if (!form || event.defaultPrevented || !form.checkValidity()) return;
  window.setTimeout(() => {
    if (document.documentElement.contains(form)) showWaitState(form);
  }, 120);
});

window.addEventListener("pageshow", hideWaitState);

refreshStepNumbers();
refreshGoalFields();
refreshLLMFields();
