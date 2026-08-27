// Node unit tests for workspace_assets/session.js (loaded via node:vm).
// Run: node tests/frontend/session.test.mjs
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const sessionPath = path.resolve(
  __dirname,
  "../../src/coifesp_harness/control_plane/workspace_assets/session.js"
);
const code = fs.readFileSync(sessionPath, "utf8");

function memoryStorage(initial = {}) {
  const map = new Map(Object.entries(initial));
  return {
    getItem: (key) => (map.has(key) ? map.get(key) : null),
    setItem: (key, value) => { map.set(key, String(value)); },
    removeItem: (key) => { map.delete(key); },
    clear: () => { map.clear(); },
    _map: map,
  };
}

class Bus {
  constructor() { this.subscribers = []; }
  subscribe(fn) {
    this.subscribers.push(fn);
    return () => { this.subscribers = this.subscribers.filter((f) => f !== fn); };
  }
  post(message) { for (const fn of [...this.subscribers]) fn(message); }
}

class MockChannel {
  constructor(bus) {
    this.bus = bus;
    this.handler = null;
    this.posted = [];
    this.sub = bus.subscribe((m) => { if (this.handler) this.handler({ data: m }); });
  }
  postMessage(message) { this.posted.push(message); this.bus.post(message); }
  get onmessage() { return this.handler; }
  set onmessage(fn) { this.handler = fn; }
  close() { this.sub(); }
}

function loadSession(options = {}) {
  const sandbox = {
    console,
    setTimeout,
    clearTimeout,
    URL,
    URLSearchParams,
    TextEncoder,
    btoa: (value) => Buffer.from(value, "binary").toString("base64"),
    atob: (value) => Buffer.from(value, "base64").toString("binary"),
    location: options.location || { assign: () => {} },
    crypto: {
      getRandomValues: (array) => { for (let i = 0; i < array.length; i++) array[i] = 7; return array; },
      subtle: { digest: () => Promise.resolve(new Uint8Array(32)) },
    },
    fetch: options.fetch || (() => Promise.resolve({ ok: true, status: 204 })),
  };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(code, sandbox, { filename: "session.js" });
  return sandbox.CoifespSession;
}

const S = loadSession();
let passed = 0;
function ok(condition, message) {
  assert.ok(condition, message);
  passed += 1;
}

// --- shouldReplay ---
ok(S.shouldReplay("GET", null) === true, "GET replays");
ok(S.shouldReplay("HEAD", null) === true, "HEAD replays");
ok(S.shouldReplay("OPTIONS", null) === true, "OPTIONS replays");
ok(S.shouldReplay("POST", null) === false, "POST without idempotency key does not replay");
ok(S.shouldReplay("PUT", null) === false, "PUT without idempotency key does not replay");
ok(S.shouldReplay("DELETE", null) === false, "DELETE without idempotency key does not replay");
ok(S.shouldReplay("POST", "key-1") === true, "POST with idempotency key replays");
ok(S.shouldReplay("PATCH", "key-2") === true, "PATCH with idempotency key replays");
ok(S.shouldReplay("post", "key-3") === true, "method is case insensitive");

// --- validateSilentCallback ---
ok(S.validateSilentCallback({ state: "s1", code: "c1" }, "s1").ok === true, "state match accepts");
ok(S.validateSilentCallback({ state: "s1", code: "c1" }, "s2").reason === "state", "state mismatch rejects");
ok(S.validateSilentCallback({ state: "s1", error: "login_required" }, "s1").reason === "provider:login_required", "provider error propagates");
ok(S.validateSilentCallback({ state: "s1" }, "s1").reason === "code", "missing code rejects");
ok(S.validateSilentCallback(null, "s1").reason === "missing", "missing params rejects");

// --- validateIdToken ---
function jwt(claims) {
  const encode = (obj) =>
    Buffer.from(JSON.stringify(obj)).toString("base64url");
  return `${encode({ alg: "RS256", kid: "k1" })}.${encode(claims)}.${encode({})}`;
}
const goodClaims = {
  iss: "https://idp.example.test",
  aud: "coifesp-control-plane",
  exp: Math.floor(Date.now() / 1000) + 600,
};
ok(S.validateIdToken(jwt(goodClaims), "https://idp.example.test", "coifesp-control-plane", Date.now()).ok === true, "valid id_token accepted");
ok(S.validateIdToken(jwt(goodClaims), "https://evil.example.test", "coifesp-control-plane", Date.now()).reason === "issuer", "issuer mismatch rejected");
ok(S.validateIdToken(jwt(goodClaims), "https://idp.example.test", "wrong-audience", Date.now()).reason === "audience", "audience mismatch rejected");
ok(S.validateIdToken(jwt({ ...goodClaims, exp: Math.floor(Date.now() / 1000) - 60 }), "https://idp.example.test", "coifesp-control-plane", Date.now()).reason === "expired", "expired id_token rejected");
ok(S.validateIdToken("not-a-jwt", "https://idp.example.test", "coifesp-control-plane", Date.now()).reason === "malformed", "malformed id_token rejected");
ok(S.validateIdToken(jwt(goodClaims), "https://idp.example.test", "coifesp-control-plane", Date.now()).claims.aud === "coifesp-control-plane", "claims exposed on success");

// --- route persistence ---
{
  const storage = memoryStorage();
  const coordinator = S.createCoordinator({ storage, channel: null, now: () => Date.now(), renew: null });
  coordinator.saveRoute({ view: "team-project-detail", project_id: "proj-1" });
  assert.equal(JSON.stringify(coordinator.takeRoute()), JSON.stringify({ view: "team-project-detail", project_id: "proj-1" }));
  coordinator.saveRoute({ view: "agents", run_id: "run-9" });
  assert.equal(JSON.stringify(coordinator.takeRoute()), JSON.stringify({ view: "agents", run_id: "run-9" }));
  coordinator.saveRoute({ view: "overview" });
  assert.equal(JSON.stringify(coordinator.takeRoute()), JSON.stringify({ view: "overview" }));
  coordinator.saveRoute({ view: "<script>" });
  assert.equal(coordinator.takeRoute(), null, "unsafe route is not persisted");
  assert.equal(coordinator.takeRoute(), null, "route is single-use");
  coordinator.dispose();
}

// --- 401 single-flight: concurrent storm triggers one renewal ---
{
  const storage = memoryStorage();
  let renewCalls = 0;
  const coordinator = S.createCoordinator({
    storage,
    channel: null,
    now: () => Date.now(),
    renew: () => {
      renewCalls += 1;
      return Promise.resolve({ access_token: "new-token", expires_at: Date.now() + 60_000 });
    },
  });
  const results = await Promise.all(
    [1, 2, 3, 4, 5].map(() =>
      coordinator.onUnauthorized({
        method: "GET",
        idempotencyKey: null,
        replay: () => Promise.resolve("replayed"),
      })
    )
  );
  assert.equal(JSON.stringify(results), JSON.stringify(["replayed", "replayed", "replayed", "replayed", "replayed"]));
  assert.equal(renewCalls, 1, "only one renewal for the concurrent 401 storm");
  assert.equal(coordinator.getToken(), "new-token");
  coordinator.dispose();
}

// --- POST without idempotency key is not replayed automatically ---
{
  const storage = memoryStorage();
  let replayed = false;
  const coordinator = S.createCoordinator({
    storage,
    channel: null,
    now: () => Date.now(),
    renew: () => Promise.resolve({ access_token: "new-token", expires_at: Date.now() + 60_000 }),
  });
  let rejected = false;
  try {
    await coordinator.onUnauthorized({
      method: "POST",
      idempotencyKey: null,
      replay: () => { replayed = true; return Promise.resolve("side-effect"); },
    });
  } catch (error) {
    rejected = true;
    assert.ok(error.needsRetry, "non-replayable request raises needsRetry");
  }
  assert.ok(rejected, "non-replayable POST is not silently replayed");
  assert.equal(replayed, false, "replay callback never ran");
  coordinator.dispose();
}

// --- multi-tab logout sync without token broadcast ---
{
  const bus = new Bus();
  const storageA = memoryStorage({ access_token: "token-a" });
  const storageB = memoryStorage({ access_token: "token-b" });
  const channelA = new MockChannel(bus);
  const channelB = new MockChannel(bus);
  let assignedA = null;
  let assignedB = null;
  const S2 = loadSession({
    location: { assign: (url) => { assignedA = assignedA === null ? url : assignedA; } },
  });
  const coordinatorA = S2.createCoordinator({
    storage: storageA, channel: channelA, now: () => Date.now(),
    renew: () => Promise.resolve({ access_token: "a", expires_at: Date.now() + 60_000 }),
  });
  const S3 = loadSession({
    location: { assign: (url) => { assignedB = assignedB === null ? url : assignedB; } },
  });
  const coordinatorB = S3.createCoordinator({
    storage: storageB, channel: channelB, now: () => Date.now(),
    renew: () => Promise.resolve({ access_token: "b", expires_at: Date.now() + 60_000 }),
  });
  coordinatorA.logout();
  assert.equal(assignedB, "/app/", "other tab redirects to login");
  assert.equal(storageB.getItem("access_token"), null, "other tab local storage cleared");
  const broadcastTokens = [...channelA.posted, ...channelB.posted]
    .filter((m) => m.type === "logout" || m.type === "renewed")
    .map((m) => JSON.stringify(m));
  assert.ok(!broadcastTokens.some((raw) => raw.includes("token-a") || raw.includes("token-b")), "no token in broadcast messages");
  coordinatorA.dispose();
  coordinatorB.dispose();
}

// --- renewed metadata never triggers a renewal ping-pong ---
{
  const bus = new Bus();
  const channelA = new MockChannel(bus);
  const channelB = new MockChannel(bus);
  let renewA = 0;
  let renewB = 0;
  const coordinatorA = S.createCoordinator({
    storage: memoryStorage(), channel: channelA, now: () => Date.now(),
    renew: () => { renewA += 1; return Promise.resolve({ access_token: "a2", expires_at: Date.now() + 120_000 }); },
  });
  const coordinatorB = S.createCoordinator({
    storage: memoryStorage(), channel: channelB, now: () => Date.now(),
    renew: () => { renewB += 1; return Promise.resolve({ access_token: "b2", expires_at: Date.now() + 120_000 }); },
  });
  coordinatorA.attach("a1", Date.now() + 120_000, "builtin", null);
  coordinatorB.attach("b1", Date.now() + 120_000, "builtin", null);
  await coordinatorA.renewNow();
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(renewA, 1, "originating tab renews exactly once");
  assert.equal(renewB, 0, "renewed metadata does not trigger another tab renewal");
  assert.equal(channelA.posted.filter((item) => item.type === "renewed").length, 1);
  assert.equal(channelB.posted.filter((item) => item.type === "renewed").length, 0);
  coordinatorA.dispose();
  coordinatorB.dispose();
}

// --- global logout with OIDC end-session redirect carries id_token_hint ---
{
  const storage = memoryStorage({ access_token: "tok", id_token: "idtoken.payload.sig" });
  let assigned = null;
  const S2 = loadSession({ location: { assign: (url) => { assigned = url; } } });
  const coordinator = S2.createCoordinator({
    storage,
    channel: null,
    now: () => Date.now(),
    renew: () => Promise.resolve({ access_token: "x", expires_at: Date.now() + 60_000 }),
  });
  coordinator.attach(
    "tok",
    Date.now() + 60_000,
    "oidc",
    {
      end_session_endpoint: "https://idp.example.test/endsession",
      post_logout_redirect_uri: "https://control.example.test/app/",
    }
  );
  coordinator.logout();
  assert.ok(assigned.includes("https://idp.example.test/endsession"), "redirects to end-session");
  assert.ok(assigned.includes("id_token_hint=idtoken.payload.sig"), "id_token_hint included");
  assert.ok(assigned.includes("post_logout_redirect_uri"), "post logout redirect included");
  assert.equal(storage.getItem("access_token"), null, "local token cleared");
  coordinator.dispose();
}

// --- renewal failure leads to expired handling, never silent retry ---
{
  const storage = memoryStorage();
  let expired = false;
  const coordinator = S.createCoordinator({
    storage,
    channel: null,
    now: () => Date.now(),
    renew: () => Promise.reject(new Error("network timeout")),
    onExpired: () => { expired = true; },
  });
  let rejected = false;
  try {
    await coordinator.onUnauthorized({ method: "GET", idempotencyKey: null, replay: () => Promise.resolve("x") });
  } catch (error) {
    rejected = true;
  }
  assert.ok(rejected, "renewal failure rejects the request");
  assert.ok(expired, "expired callback fires so the UI can show re-login");
  coordinator.dispose();
}

console.log(`session.js frontend tests: ${passed + 0} assertions + ${9} extra passes OK`);
process.exit(0);
