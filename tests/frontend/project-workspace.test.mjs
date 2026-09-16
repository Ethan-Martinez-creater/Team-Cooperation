// Static contract checks for the Harness-native project workspace.
// These checks intentionally avoid a browser dependency; browser acceptance
// remains a separate environment-level check.
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const assetRoot = path.resolve(here, "../../src/coifesp_harness/control_plane/workspace_assets");
const html = fs.readFileSync(path.join(assetRoot, "index.html"), "utf8");
const script = fs.readFileSync(path.join(assetRoot, "project-workspace.js"), "utf8");
const css = fs.readFileSync(path.join(assetRoot, "project-workspace.css"), "utf8");

const tabs = [...html.matchAll(/data-ws-tab="([^"]+)"/g)].map((match) => match[1]);
assert.deepEqual(tabs, ["overview", "work-graph", "tasks", "activity", "collab", "delivery"]);
assert.ok(!html.includes('data-ws-tab="data"'), "data is an overview capability, not a tab");
assert.ok(!html.includes('data-ws-tab="plan"'), "plan is an overview capability, not a tab");
assert.ok(!html.includes('data-ws-tab="inbox"'), "inbox is not a project workspace tab");
assert.ok(!html.includes('id="ws-project-id"'), "the project header must not expose an internal project ID");

assert.match(script, /\/v1\/projects\/\$\{encodeURIComponent\(projectId\)\}\/harness-view/);
assert.match(script, /function renderProcessSummary\(process\)/);
assert.match(script, /function workGraphPane\(graph\)/);
assert.match(script, /function activityPane\(activityValue\)/);
assert.match(script, /function deliveryPane\(harnessView = \{\}\)/);
assert.match(script, /semantic_status/);
assert.match(script, /occurred_at/);
assert.match(script, /verification\.passed/);
assert.match(script, /执行契约待确认/);
assert.match(script, /generation/);
assert.match(script, /ownActive\(project, generation\)/);
assert.match(script, /\/code\/repositories/);
assert.match(script, /代码仓库/);
assert.match(script, /repository_bound/);
assert.match(script, /status\.reason/);
assert.ok(script.includes("data-ws-preview-resource"), "visible project resources expose a preview action");
assert.ok(script.includes("data-ws-attach-resource"), "text-readable project resources can be attached to chat");
assert.ok(
  script.includes("/resources/${encodeURIComponent(resourceId)}/preview"),
  "workspace preview uses the policy-aware preview endpoint",
);
assert.ok(script.includes('pre.textContent = text.slice'), "text and HTML previews are rendered as inert text");

const activityStart = script.indexOf("function activityPane(activityValue)");
const deliveryStart = script.indexOf("function deliveryPane", activityStart);
assert.ok(activityStart >= 0 && deliveryStart > activityStart);
const activityCode = script.slice(activityStart, deliveryStart);
for (const forbidden of ["run_id", "payload", "prompt", "tool_args", "secret", "private_context"]) {
  assert.equal(activityCode.includes(forbidden), false, `Activity must not reference ${forbidden}`);
}

const messageStart = script.indexOf("function messageNode(m)");
const sendStart = script.indexOf("async function sendMessage", messageStart);
const messageCode = script.slice(messageStart, sendStart);
assert.equal(messageCode.includes("你"), false, "message bubbles do not add user/agent labels");
assert.equal(messageCode.includes("Agent"), false, "message bubbles do not add user/agent labels");

const workspaceStart = html.indexOf('<section id="view-project-workspace"');
const workspaceEnd = html.indexOf("</section>", workspaceStart);
const workspaceMarkup = html.slice(workspaceStart, workspaceEnd);
assert.match(workspaceMarkup, /id="ws-send"[^>]*>发送/);
assert.equal(workspaceMarkup.includes("继续追问"), false, "workspace composer has one send action");
assert.equal(workspaceMarkup.includes("调整方向"), false, "workspace composer has one send action");

for (const operation of [
  "/resources:upload",
  "/propagation",
  "data-ws-share-resource",
  "data-ws-approve-plan",
  "/agent-exchange-drafts",
  "data-ws-new-exchange",
]) {
  assert.ok(script.includes(operation), `legacy operation remains reachable: ${operation}`);
}

assert.match(css, /\.work-graph-tree/);
assert.match(css, /\.work-node-children/);
assert.match(css, /\.activity-list/);
assert.match(css, /\.delivery-section/);
assert.match(css, /\.repository-summary-card/);

console.log("project-workspace frontend contract: OK");

// Execute the real Work Graph renderer and verify that persisted relations,
// rather than hard-coded node types, determine the visual hierarchy.
const graphCode = script.slice(script.indexOf("  function normalizeItems("),
  script.indexOf("  async function tasksPane("));
const graphContext = {
  firstValue: (value, keys, fallback = "") => {
    for (const key of keys) if (value && value[key] !== undefined && value[key] !== null && value[key] !== "") return value[key];
    return fallback;
  },
  esc: (value) => String(value),
  stateName: (value) => String(value),
};
vm.createContext(graphContext);
vm.runInContext(graphCode, graphContext);
const graphHtml = graphContext.workGraphPane({
  nodes: [
    { node_id: "goal", type: "goal", label: "交付 MVP" },
    { node_id: "requirement", type: "requirement", label: "创建任务" },
    { node_id: "milestone", type: "milestone", label: "可验收版本" },
    { node_id: "phase", type: "phase", label: "工程实现" },
    { node_id: "task", type: "task", label: "实现看板" },
    { node_id: "risk", type: "risk", label: "范围膨胀" },
  ],
  edges: [
    { source: "requirement", target: "goal", type: "derived_from" },
    // Legacy Plan v2 graphs omitted this milestone -> goal edge; the renderer
    // intentionally attaches a root milestone only when there is one goal.
    { source: "phase", target: "milestone", type: "part_of" },
    { source: "task", target: "phase", type: "part_of" },
    { source: "risk", target: "goal", type: "relates_to" },
    { source: "task", target: "requirement", type: "depends_on" },
  ],
});
for (const id of ["goal", "requirement", "milestone", "phase", "task", "risk"]) {
  assert.ok(graphHtml.includes(`data-work-node-id="${id}"`), `${id} remains visible`);
}
assert.ok(graphHtml.indexOf('data-work-node-id="goal"') < graphHtml.indexOf('data-work-node-id="requirement"'));
assert.ok(graphHtml.indexOf('data-work-node-id="goal"') < graphHtml.indexOf('data-work-node-id="milestone"'));
assert.ok(graphHtml.indexOf('data-work-node-id="milestone"') < graphHtml.indexOf('data-work-node-id="phase"'));
assert.ok(graphHtml.indexOf('data-work-node-id="phase"') < graphHtml.indexOf('data-work-node-id="task"'));
assert.equal((graphHtml.match(/dependency-badge/g) || []).length, 1, "part_of is not shown as a dependency");
assert.equal(graphHtml.includes("level-"), false, "fixed type indentation is removed");
console.log("work graph hierarchy behavior: OK");

// Execute the real download function against a bounded browser double.
const downloadCode = script.slice(script.indexOf("  async function downloadResource("),
  script.indexOf("  function uploadAttachment()"));
const downloads = [], requests = [], errors = [], revoked = [];
const context = {
  active: { projectId: "project-a", generation: 1 }, W: { token: "test-token" },
  ownActive: (id, generation) => context.active.projectId === id && context.active.generation === generation,
  fetch: async (url, options) => {
    requests.push({ url, options });
    return { ok: true, blob: async () => ({}), headers: { get: () => "application/json" } };
  },
  URL: { createObjectURL: () => "blob:test", revokeObjectURL: (url) => revoked.push(url) },
  document: { body: { appendChild() {} }, createElement: () => ({
    click() { downloads.push(this.download); }, remove() {},
  }) },
  setTimeout: (callback) => callback(), toast: (message) => errors.push(message),
};
vm.createContext(context);
vm.runInContext(downloadCode, context);
await context.downloadResource("receipt:1", "checks/result");
assert.equal(requests[0].url, "/v1/projects/project-a/resources/receipt%3A1/content");
assert.equal(requests[0].options.headers.Authorization, "Bearer test-token");
assert.deepEqual(downloads, ["checks_result.json"]);
assert.deepEqual(revoked, ["blob:test"]);
context.fetch = async () => ({ ok: true, blob: async () => {
  context.active = { projectId: "project-b", generation: 2 }; return {};
} });
await context.downloadResource("receipt:2", "stale");
assert.equal(downloads.length, 1, "stale response must not trigger a download");
context.fetch = async () => ({ ok: false });
await context.downloadResource("receipt:3", "denied");
assert.equal(downloads.length, 1);
assert.equal(errors.length, 1, "permission failure is visible without downloading");
console.log("resource download behavior: OK");

// Existing project resources can be explicitly attached to the conversation,
// but duplicate clicks must not send duplicate context items.
const attachCode = script.slice(
  script.indexOf("  function attachResourceToConversation("),
  script.indexOf("  async function previewResource("),
);
const attachmentMessages = [];
let attachmentRenders = 0, inputFocuses = 0;
const attachContext = {
  active: { projectId: "project-a" },
  pendingAttachments: [],
  renderAttachmentChips: () => { attachmentRenders += 1; },
  toast: (message) => attachmentMessages.push(message),
  $: () => ({ focus: () => { inputFocuses += 1; } }),
};
vm.createContext(attachContext);
vm.runInContext(attachCode, attachContext);
attachContext.attachResourceToConversation("resource:shared-html");
attachContext.attachResourceToConversation("resource:shared-html");
assert.deepEqual(attachContext.pendingAttachments, ["resource:shared-html"]);
assert.equal(attachmentRenders, 1);
assert.equal(inputFocuses, 1);
assert.equal(attachmentMessages.length, 2);
assert.match(attachmentMessages[1], /已在待发送附件/);
console.log("existing resource attachment behavior: OK");
