const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = readFileSync(join(__dirname, "../garminworkouts/static/theme.js"), "utf8");
const key = "garmin-running-planner-theme";

function fixture({ dark = false, saved = null, blocked = false } = {}) {
  const events = {};
  const values = new Map(saved === null ? [] : [[key, saved]]);
  const buttons = ["system", "light", "dark"].map(mode => ({
    dataset: { themeChoice: mode },
    setAttribute(name, value) { this[name] = value; },
  }));
  const root = { dataset: {} };
  const control = {
    hidden: true,
    contains: button => buttons.includes(button),
    addEventListener(name, callback) { events[name] = callback; },
  };
  let ready = false;
  const media = {
    matches: dark,
    addEventListener(name, callback) { events.media = callback; },
  };
  const storage = {
    getItem(name) { if (blocked) throw Error("blocked"); return values.get(name) ?? null; },
    setItem(name, value) { if (blocked) throw Error("blocked"); values.set(name, value); },
    removeItem(name) { if (blocked) throw Error("blocked"); values.delete(name); },
  };
  const context = {
    document: {
      documentElement: root,
      querySelectorAll() { return ready ? buttons : []; },
      querySelector() { return control; },
      addEventListener(name, callback) { events[name] = callback; },
    },
    window: {
      matchMedia(query) { assert.equal(query, "(prefers-color-scheme: dark)"); return media; },
      localStorage: storage,
      addEventListener(name, callback) { events[name] = callback; },
    },
  };
  vm.runInNewContext(source, context);
  const initial = root.dataset.theme;
  ready = true;
  events.DOMContentLoaded();
  return {
    initial, root, buttons, control, values,
    click(mode) {
      events.click({ target: { closest: () => buttons.find(button => button.dataset.themeChoice === mode) } });
    },
    system(darkMode) { media.matches = darkMode; events.media(); },
    storage(value, eventKey = key) { events.storage({ key: eventKey, newValue: value }); },
    restore() { events.pageshow({ persisted: true }); },
    active() { return buttons.filter(button => button["aria-pressed"] === "true").map(button => button.dataset.themeChoice); },
  };
}

test("first paint and the default control follow the system", () => {
  for (const dark of [false, true]) {
    const page = fixture({ dark });
    assert.equal(page.initial, dark ? "dark" : "light");
    assert.equal(page.control.hidden, false);
    assert.deepEqual(page.active(), ["system"]);
  }
});

test("system changes update automatically, but never override a manual choice", () => {
  const page = fixture();
  page.system(true);
  assert.equal(page.root.dataset.theme, "dark");
  page.click("light");
  page.system(true);
  assert.equal(page.root.dataset.theme, "light");
  assert.equal(page.values.get(key), "light");
  page.click("dark");
  page.system(false);
  assert.equal(page.root.dataset.theme, "dark");
  assert.deepEqual(page.active(), ["dark"]);
});

test("saved choices take precedence before first paint", () => {
  assert.equal(fixture({ dark: false, saved: "dark" }).initial, "dark");
  assert.equal(fixture({ dark: true, saved: "light" }).initial, "light");
});

test("System clears the saved override and follows the current system", () => {
  const page = fixture({ saved: "dark", dark: false });
  page.click("system");
  assert.equal(page.root.dataset.theme, "light");
  assert.equal(page.values.has(key), false);
  assert.deepEqual(page.active(), ["system"]);
});

test("invalid saved values and unavailable storage fail safely", () => {
  assert.equal(fixture({ dark: true, saved: "invalid" }).initial, "dark");
  const page = fixture({ blocked: true });
  page.click("dark");
  assert.equal(page.root.dataset.theme, "dark");
  assert.deepEqual(page.active(), ["dark"]);
});

test("cross-tab changes and clearing storage update the current page", () => {
  const page = fixture();
  page.storage("dark");
  assert.equal(page.root.dataset.theme, "dark");
  assert.deepEqual(page.active(), ["dark"]);
  page.storage("light", "unrelated-preference");
  assert.equal(page.root.dataset.theme, "dark");
  page.storage(null, null);
  assert.equal(page.root.dataset.theme, "light");
  assert.deepEqual(page.active(), ["system"]);
});

test("back/forward-cached pages pick up the stored theme", () => {
  const page = fixture();
  page.values.set(key, "dark");
  page.restore();
  assert.equal(page.root.dataset.theme, "dark");
});

test("clicks outside the theme choices have no effect", () => {
  const page = fixture();
  page.click("not-a-button");
  assert.deepEqual(page.active(), ["system"]);
});
