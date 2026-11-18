const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const ts = require("typescript");

function client() {
  let cookie = "_xsrf=initial%3Dtoken; unrelated=keep";
  const sent = [];
  const shared = {
    document: {
      get cookie() {
        return cookie;
      },
    },
    URLSearchParams,
    fetch: async (url, settings) => {
      sent.push({ url, ...settings });
      return { ok: true, status: 200, json: async () => ({}) };
    },
  };
  function load(name, requires = {}) {
    const exports = {};
    const filename = path.join(__dirname, "../src/api/", name + ".ts");
    const js = ts.transpileModule(fs.readFileSync(filename, "utf8"), {
      compilerOptions: {
        module: ts.ModuleKind.CommonJS,
        target: ts.ScriptTarget.ES2022,
      },
    }).outputText;
    vm.runInNewContext(
      js,
      { ...shared, exports, require: (name) => requires[name] },
      { filename },
    );
    return exports;
  }
  const http = load("http");
  return {
    requests: load("client", { "./http": http }).requests,
    sent,
    setCookie(value) {
      cookie = value;
    },
  };
}

for (const method of ["get", "post", "put", "del"]) {
  test(
    method + " reads the current decoded XSRF cookie for every request",
    async () => {
      const c = client();
      await c.requests[method]("/api/workspaces/a/start", {});
      assert.equal(c.sent[0].headers["X-XSRFToken"], "initial=token");
      c.setCookie("_xsrf=rotated%3Dtoken; unrelated=keep");
      await c.requests[method]("/api/workspaces/a/start", {});
      assert.equal(c.sent[1].headers["X-XSRFToken"], "rotated=token");
      c.setCookie("unrelated=keep");
      await c.requests[method]("/api/workspaces/a/start", {});
      assert.equal(c.sent[2].headers["X-XSRFToken"], "");
      for (const request of c.sent)
        assert.equal(request.credentials, "same-origin");
      assert.equal(c.sent.length, 3, "no automatic mutating retry");
    },
  );
}
test("GET retains query, abort and cache controls without replacing security settings", async () => {
  const c = client(),
    signal = new AbortController().signal;
  await c.requests.get(
    "/api/workspaces",
    { search: "a b" },
    { signal, cache: "no-store" },
  );
  assert.equal(c.sent[0].url, "/api/workspaces?search=a+b");
  assert.equal(c.sent[0].signal, signal);
  assert.equal(c.sent[0].cache, "no-store");
  assert.equal(c.sent[0].headers["X-XSRFToken"], "initial=token");
  assert.equal(c.sent[0].credentials, "same-origin");
});
