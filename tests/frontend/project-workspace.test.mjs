// Static contract checks for the Harness-native project workspace.
// These checks intentionally avoid a browser dependency; browser acceptance
// remains a separate environment-level check.
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
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
assert.match(script, /generation/);
assert.match(script, /ownActive\(project, generation\)/);

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
assert.match(css, /\.activity-list/);
assert.match(css, /\.delivery-section/);

console.log("project-workspace frontend contract: OK");
