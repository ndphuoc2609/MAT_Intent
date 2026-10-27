const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

class Element {
  constructor() {
    this.value = "";
    this.textContent = "";
    this.hidden = false;
    this.disabled = false;
    this.handlers = {};
    this.dataset = {};
    this.classList = { toggle() {} };
  }
  addEventListener(name, handler) { this.handlers[name] = handler; }
  append() {}
  replaceChildren() {}
  setAttribute() {}
  focus() {}
}

test("uncertain publish keeps its idempotency key through a 423 retry", async () => {
  const elements = new Map();
  const document = {
    querySelector(selector) {
      if (!elements.has(selector)) elements.set(selector, new Element());
      return elements.get(selector);
    },
    querySelectorAll() { return []; },
    createElement() { return new Element(); },
  };
  const keys = [];
  let writes = 0;
  const fetch = async (url, options = {}) => {
    if (url === "/health") return response(200, { outbound_enabled: true });
    assert.equal(options.method, "POST");
    keys.push(options.headers.get("Idempotency-Key"));
    writes += 1;
    if (writes === 1) throw new TypeError("simulated lost response");
    if (writes === 2) return response(423, { error: "outbound_disabled" });
    return response(200, { id: "published" });
  };
  const context = vm.createContext({
    document,
    fetch,
    Headers,
    URLSearchParams,
    crypto: { randomUUID: () => "11111111-2222-3333-4444-555555555555" },
    window: { confirm: () => true },
  });
  const scriptPath = path.join(__dirname, "../meta_services/static/admin.js");
  vm.runInContext(fs.readFileSync(scriptPath, "utf8"), context);
  await new Promise(setImmediate);

  document.querySelector("#post-message").value = "Test post";
  const submit = document.querySelector("#publish-form").handlers.submit;
  const event = { preventDefault() {} };
  await submit(event);
  assert.equal(vm.runInContext('pendingWrites.get("publish").uncertain', context), true);

  await submit(event);
  await new Promise(setImmediate);
  assert.equal(vm.runInContext("pendingWrites.size", context), 1);

  vm.runInContext("health.outbound_enabled = true", context);
  await submit(event);
  assert.equal(keys.length, 3);
  assert.equal(new Set(keys).size, 1);
  assert.equal(vm.runInContext("pendingWrites.size", context), 0);
});

function response(status, body) {
  return { ok: status >= 200 && status < 300, status, text: async () => JSON.stringify(body) };
}
