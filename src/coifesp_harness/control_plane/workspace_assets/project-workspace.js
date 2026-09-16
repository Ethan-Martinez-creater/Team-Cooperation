/* COIFESP Agent-first project workspace.
   The project page is a continuous ChatGPT-style conversation. Tasks, data,
   exchanges and plans are context drawers on the same page, never separate
   top-level pages. Internal objects (run IDs, tokens, events) stay hidden.
*/
(function () {
  "use strict";

  let W = null;
  let active = null; // { projectId, generation, conversationId, eventController, lastSequence }
  let pendingAttachments = [];
  let openGeneration = 0;
  let workspacePreviewUrl = null;
  let bound = false;

  const DRAWER_TABS = new Set(["overview", "work-graph", "tasks", "activity", "collab", "delivery"]);
  const PROCESS_LABELS = {
    intake: "准备中",
    planning: "规划中",
    executing: "执行中",
    verifying: "验收中",
    waiting: "等待中",
    paused: "已暂停",
    completed: "已完成",
    terminal: "已完成",
    failed: "需要处理",
    cancelled: "已取消",
  };
  const ACTIVITY_STATUS = {
    completed: "已完成",
    complete: "已完成",
    succeeded: "已完成",
    success: "已完成",
    running: "进行中",
    active: "进行中",
    pending: "待处理",
    waiting: "等待中",
    failed: "失败",
  };

  const esc = (v) =>
    String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const $ = (s) => document.querySelector(s);
  const uid = (p) => `${p}-${crypto.randomUUID()}`;
  const stateName = (v) =>
    ({ proposed: "待响应", accepted: "待开始", in_progress: "进行中", submitted: "待验收", verified: "已完成", rejected: "已拒绝", changes_requested: "需修改", drafting: "起草中", approved: "已批准", sent: "已发送", responded: "已回复", pending: "待处理", active: "进行中", running: "运行中", completed: "已完成", failed: "失败" }[v] || v);
  const propagationLabel = {
    team_private: "仅本团队",
    project_readonly: "项目共享",
    portable: "可带走共享",
  };

  function currentTeamId() {
    if (typeof state === "undefined" || !state) return W?.teamId || null;
    return state.account?.team_id || state.identity?.tenant_id || W?.teamId || null;
  }

  function ownActive(projectId, generation) {
    return Boolean(active && active.projectId === projectId && active.generation === generation);
  }

  function firstValue(item, keys, fallback = "") {
    for (const key of keys) {
      const value = item?.[key];
      if (value !== undefined && value !== null && String(value).trim()) return value;
    }
    return fallback;
  }

  function displayValue(value, fallback = "") {
    if (value === undefined || value === null) return fallback;
    if (Array.isArray(value)) return value.map((item) => displayValue(item)).filter(Boolean).join("、") || fallback;
    if (typeof value === "object") return firstValue(value, ["label", "summary", "reason", "title", "name"], fallback);
    return String(value);
  }

  function semanticState(value) {
    const normalized = String(value || "").toLowerCase();
    return PROCESS_LABELS[normalized] || stateName(normalized) || "状态未知";
  }

  function semanticActivityStatus(value) {
    const normalized = String(value || "").toLowerCase();
    return ACTIVITY_STATUS[normalized] || (value ? String(value) : "进行中");
  }

  function toast(message, error) {
    const el = $("#toast");
    el.textContent = message;
    el.className = `toast show${error ? " error" : ""}`;
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => (el.className = "toast"), 3500);
  }

  async function api(path, options = {}) {
    const headers = { Accept: "application/json", Authorization: `Bearer ${W.token}`, ...options.headers };
    if (options.body && !headers["Content-Type"]) headers["Content-Type"] = "application/json";
    const r = await fetch(path, { ...options, headers, cache: "no-store" });
    if (!r.ok) {
      let p = {};
      try { p = await r.json(); } catch (e) {}
      throw Error(p.detail || p.title || `请求失败 (${r.status})`);
    }
    return r.status === 204 ? null : r.json();
  }

  function init(apiFn, token, context = {}) {
    W = { api: apiFn, token, teamId: context.teamId || null };
    refresh("team-projects");
    bind();
  }

  function bind() {
    if (bound) return;
    bound = true;
    $("#new-team-project").addEventListener("click", () => createProject());
    $("[data-workspace-back]").addEventListener("click", () => showProjects());
    $("#ws-send").addEventListener("click", sendMessage);
    $("#ws-attach").addEventListener("click", uploadAttachment);
    $("#ws-input").addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        sendMessage();
      }
    });
    document.querySelectorAll("[data-ws-tab]").forEach((button) => {
      button.addEventListener("click", () => switchDrawerTab(button.dataset.wsTab));
    });
  }

  async function refreshScroll(viewName) {
    if (typeof state === "undefined" || !state.token) return;
  }

  function showProjects() {
    if (typeof show === "function") show("team-projects");
    refresh("team-projects");
  }

  async function refresh(viewName) {
    if (viewName !== "team-projects") return;
    try {
      const projects = await W.api("/v1/workspace/projects");
      renderProjects(projects);
    } catch (e) {
      // Product workspace may be unavailable; keep existing list rendering.
    }
  }

  function renderProjects(projects) {
    const list = $("#workspace-project-list");
    if (!list) return;
    if (!projects || !projects.length) {
      list.innerHTML = `<div class="empty-state">还没有项目。创建一个项目，Agent 会从规划开始全程参与。</div>`;
      return;
    }
    list.innerHTML = projects
      .map(
        (item) => `
        <article class="card">
          <div class="card-head">
            <div>
              <h3>${esc(item.project.name)} ${item.pending_count ? `<span class="badge">${item.pending_count}</span>` : ""}</h3>
              <div class="meta"><span>${esc(item.project.project_id)}</span></div>
            </div>
            <button class="primary" data-open-workspace="${esc(item.project.project_id)}">打开</button>
          </div>
          <p class="muted">${esc(item.project.description || "（无项目说明）")}</p>
        </article>`
      )
      .join("");
    list.querySelectorAll("[data-open-workspace]").forEach((button) => {
      button.addEventListener("click", () => openProject(button.dataset.openWorkspace));
    });
  }

  function createProject() {
    const dialog = $("#modal");
    $("#modal-title").textContent = "创建项目";
    $("#modal-fields").innerHTML = `
      <label class="field">项目名称<input name="name" required maxlength="256" placeholder="例如：多团队协作演示"></label>
      <label class="field">项目目标<textarea name="goals" required placeholder="这个项目要达成什么？"></textarea></label>
      <label class="field">背景说明<textarea name="background" placeholder="现状、动机与约束"></textarea></label>
      <label class="field">期望交付<textarea name="deliverables" placeholder="你希望最终得到什么？"></textarea></label>
      <p class="muted small">创建后系统会为你建立唯一的项目会话，Agent 将基于目标给出计划与团队建议。</p>`;
    dialog.showModal();
    $("#modal-form").onsubmit = async (e) => {
      e.preventDefault();
      const values = Object.fromEntries(new FormData(e.target));
      const briefParts = [
        values.goals && `目标：${values.goals}`,
        values.background && `背景与约束：${values.background}`,
        values.deliverables && `期望交付：${values.deliverables}`,
      ].filter(Boolean);
      try {
        const result = await W.api("/v1/projects", {
          method: "POST",
          body: JSON.stringify({
            name: values.name,
            description: values.background || "",
            owner_assignment_name: "产品统筹",
            owner_kind: "product",
            initial_brief: briefParts.join("\n"),
          }),
        });
        dialog.close();
        const projectId = result.project_id || result.project?.project_id;
        if (projectId) await openProject(projectId);
        else showProjects();
        toast("项目已创建，Agent 正在分析项目目标并准备计划建议");
      } catch (err) {
        toast(err.message, true);
      }
    };
  }

  async function openProject(projectId) {
    // Rapidly clicking two projects must not let a stale response overwrite
    // the newer one: each open claims a generation and late responses are
    // discarded once a newer open has started.
    const generation = ++openGeneration;
    try {
      // Switching projects must first stop the previous project's event
      // stream and clear all per-project UI state, otherwise stale SSE
      // frames, messages and pending attachments leak into the new project.
      if (active?.eventController) active.eventController.abort();
      active = null;
      pendingAttachments = [];
      renderAttachmentChips();
      const conversationBox = $("#ws-conversation");
      if (conversationBox) conversationBox.innerHTML = "";
      const encodedProjectId = encodeURIComponent(projectId);
      const [snapshot, conversation, harnessView] = await Promise.all([
        W.api(`/v1/projects/${encodedProjectId}/workspace`),
        W.api(`/v1/projects/${encodedProjectId}/conversation`, { method: "PUT" }),
        loadHarnessView(projectId),
      ]);
      if (generation !== openGeneration) return;
      active = {
        projectId,
        generation,
        conversationId: conversation.conversation_id,
        eventController: null,
        snapshot,
        harnessView,
      };
      renderProjectHead(snapshot, harnessView);
      const page = await W.api(
        `/v1/projects/${encodedProjectId}/conversation/messages?after_sequence=0`
      );
      if (generation !== openGeneration) return;
      active.lastSequence = conversation.last_message_sequence;
      renderConversation(page.items);
      startEventStream(projectId, generation);
      await refreshDrawer("overview");
      await refreshDrawer("work-graph");
      await refreshDrawer("tasks");
      await refreshDrawer("activity");
      await refreshDrawer("collab");
      await refreshDrawer("delivery");
      if (typeof show === "function") show("project-workspace");
      if (typeof state !== "undefined" && state.sessionCoordinator) {
        state.sessionCoordinator.saveRoute({ view: "project-workspace", project_id: projectId });
      }
      $("#ws-input").focus();
    } catch (e) {
      if (generation === openGeneration) toast(e.message, true);
    }
  }

  async function loadHarnessView(projectId) {
    try {
      const view = await W.api(`/v1/projects/${encodeURIComponent(projectId)}/harness-view`);
      return view && typeof view === "object" ? view : {};
    } catch (error) {
      // Older deployments do not expose the aggregate yet. The workspace
      // remains useful with the legacy project, plan and task endpoints.
      return {};
    }
  }

  function renderProjectHead(snapshot, harnessView = {}) {
    $("#ws-project-name").textContent = snapshot?.project?.name || "项目工作台";
    $("#ws-project-desc").textContent = snapshot?.project?.description || "";
    renderProcessSummary(harnessView?.process);
    const canAddTeam = W.teamId && snapshot.project.owner_team_id === W.teamId;
    $("#ws-project-teams").innerHTML = [
      ...(snapshot.teams || []).map((t) => `<span class="pill">${esc(t.name)}</span>`),
      canAddTeam ? `<button type="button" class="secondary ws-add-team">添加参与团队</button>` : "",
    ].join("");
    const addTeam = $("#ws-project-teams .ws-add-team");
    if (addTeam) addTeam.addEventListener("click", () => addProjectTeam(snapshot));
  }

  function renderProcessSummary(process) {
    const stateEl = $("#ws-process-state");
    const blockerEl = $("#ws-process-blocker");
    const nextEl = $("#ws-process-next");
    if (!stateEl || !blockerEl || !nextEl) return;
    if (!process) {
      stateEl.textContent = "Harness 尚未启动";
      stateEl.dataset.state = "ready";
      blockerEl.textContent = "";
      nextEl.textContent = "下一步：在对话中说明项目目标并确认计划";
      blockerEl.classList.add("hidden");
      nextEl.classList.remove("hidden");
      return;
    }
    const phase = firstValue(process, ["phase_label", "phase", "current_phase", "stage"]);
    const status = firstValue(process, ["status", "state", "current_status"]);
    const semantic = firstValue(process, ["semantic_status"]);
    const statusText = semantic || semanticState(displayValue(status || phase));
    stateEl.textContent = phase ? `${displayValue(phase)} · ${statusText}` : statusText;
    stateEl.dataset.state = displayValue(status || phase || "unknown").toLowerCase();
    const blocker = firstValue(process, ["blocker", "blocking_reason", "wait_reason", "waiting_for"]);
    const hasBlocker = blocker && String(blocker).toUpperCase() !== "NONE";
    blockerEl.textContent = hasBlocker ? `等待原因：${displayValue(blocker)}` : "";
    const next = firstValue(process, ["next_step", "next_action", "recommended_action"]);
    nextEl.textContent = next ? `下一步：${displayValue(next)}` : "";
    blockerEl.classList.toggle("hidden", !hasBlocker);
    nextEl.classList.toggle("hidden", !next);
  }

  async function addProjectTeam(snapshot) {
    if (!active) return;
    try {
      const relations = await W.api("/v1/team-relations");
      const current = new Set((snapshot.teams || []).map((team) => team.team_id));
      const candidates = (relations || []).filter((relation) => !current.has(relation.team.team_id));
      if (!candidates.length) {
        toast("没有可添加的关联团队，请先在团队页建立合作关系", true);
        return;
      }
      const dialog = $("#modal");
      $("#modal-title").textContent = "添加参与团队";
      $("#modal-fields").innerHTML = `
        <p class="muted">加入项目后，该团队会获得自己的唯一项目会话，并可参与 Agent 协作。</p>
        <label class="field">团队<select name="team_id">${candidates.map((relation) => `<option value="${esc(relation.team.team_id)}">${esc(relation.team.name)} · ${esc(relation.team.handle)}</option>`).join("")}</select></label>
        <label class="field">项目职责名称<input name="assignment_name" required maxlength="128" placeholder="例如：工程交付"></label>
        <label class="field">职责类型<select name="kind"><option value="engineering">工程</option><option value="quality">质量</option><option value="product">产品</option><option value="design">设计</option><option value="operations">运维</option><option value="custom">自定义</option></select></label>`;
      dialog.showModal();
      $("#modal-form").onsubmit = async (event) => {
        event.preventDefault();
        const values = Object.fromEntries(new FormData(event.target));
        const projectId = active?.projectId;
        if (!projectId) return;
        try {
          await W.api(`/v1/projects/${encodeURIComponent(projectId)}/teams`, {
            method: "POST",
            body: JSON.stringify(values),
          });
          dialog.close();
          await openProject(projectId);
          toast("参与团队已加入项目");
        } catch (error) {
          toast(error.message, true);
        }
      };
    } catch (error) {
      toast(error.message, true);
    }
  }

  function renderConversation(messages) {
    const box = $("#ws-conversation");
    if (!messages || !messages.length) box.innerHTML = "";
    const nodes = (messages || []).map((m) => messageNode(m));
    if (nodes.length) box.append(...nodes);
    box.scrollTop = box.scrollHeight;
  }

  function messageNode(m) {
    const div = document.createElement("div");
    if (m.message_kind === "system") {
      // Server-initiated status notes (exchange trigger messages) render as a
      // quiet system line instead of a user bubble.
      div.className = "chat-message system";
      const note = document.createElement("div");
      note.className = "bubble system-note";
      note.textContent = m.content;
      div.appendChild(note);
      return div;
    }
    div.className = `chat-message ${m.role === "user" ? "user" : "agent"}`;
    div.dataset.sequence = m.sequence;
    const bubble = document.createElement("div");
    bubble.className = "bubble";
    bubble.textContent = m.content;
    if (m.attachment_resource_ids && m.attachment_resource_ids.length) {
      const meta = document.createElement("div");
      meta.className = "bubble-meta";
      meta.textContent = "附件：" + m.attachment_resource_ids.join("、");
      bubble.appendChild(meta);
    }
    div.appendChild(bubble);
    return div;
  }

  async function sendMessage() {
    const input = $("#ws-input");
    const content = input.value.trim();
    if (!active || (!content && pendingAttachments.length === 0)) return;
    const projectId = active.projectId;
    const generation = active.generation;
    const conversationId = active.conversationId;
    const attachments = [...pendingAttachments];
    pendingAttachments = [];
    renderAttachmentChips();
    input.value = "";
    const body = {
      content,
      idempotency_key: uid("msg"),
      attachment_resource_ids: attachments,
    };
    // The optimistic lock uses the last sequence we actually observed.
    if (ownActive(projectId, generation) && active.lastSequence != null) body.expected_last_sequence = active.lastSequence;
    try {
      const result = await W.api(
        `/v1/projects/${encodeURIComponent(projectId)}/conversation/messages`,
        { method: "POST", body: JSON.stringify(body) }
      );
      // Ignore the response if the user switched projects while awaiting.
      if (!ownActive(projectId, generation)) return;
      renderConversation([result.message]);
      active.lastSequence = result.message.sequence;
      active.pendingTurn = result.turn?.turn_id;
      if (result.run) {
        toast("Agent 已开始执行");
      } else {
        renderTurnPending();
      }
    } catch (e) {
      if (!ownActive(projectId, generation)) return;
      pendingAttachments = pendingAttachments.concat(attachments);
      renderAttachmentChips();
      renderTurnFailed(e.message);
      toast(e.message, true);
    }
  }

  function renderAttachmentChips() {
    const area = $("#ws-attachments");
    if (!area) return;
    area.innerHTML = pendingAttachments
      .map((id, index) => `<span class="attachment-chip">${esc(id)} <button type="button" data-remove-attachment="${index}">×</button></span>`)
      .join("");
    area.querySelectorAll("[data-remove-attachment]").forEach((button) =>
      button.addEventListener("click", () => {
        pendingAttachments.splice(Number(button.dataset.removeAttachment), 1);
        renderAttachmentChips();
      })
    );
  }

  function renderTurnPending() {
    const box = $("#ws-conversation");
    const div = document.createElement("div");
    div.className = "chat-message agent turn-pending";
    div.innerHTML = `<div class="bubble muted small">Agent 正在处理…</div>`;
    box.appendChild(div);
    box.scrollTop = box.scrollHeight;
  }

  function renderTurnFailed(message) {
    const box = $("#ws-conversation");
    const div = document.createElement("div");
    div.className = "chat-message agent turn-pending";
    div.innerHTML = `<div class="bubble muted small">Agent 执行失败：${esc(message)}（消息已保留，可直接重发）</div>`;
    box.appendChild(div);
    box.scrollTop = box.scrollHeight;
  }

  function startEventStream(projectId, generation = active?.generation) {
    if (active?.eventController) active.eventController.abort();
    const controller = new AbortController();
    if (!ownActive(projectId, generation)) return;
    active.eventController = controller;
    const path = `/v1/projects/${encodeURIComponent(projectId)}/conversation/events`;
    let attempt = 0;

    const openStream = () => {
      const cursor = ownActive(projectId, generation) && active.lastSequence ? active.lastSequence : 0;
      return fetch(path, {
        headers: {
          Authorization: `Bearer ${W.token}`,
          ...(cursor ? { "Last-Event-ID": String(cursor) } : {}),
        },
        cache: "no-store",
        signal: controller.signal,
      });
    };

    const readLoop = async () => {
      try {
        const response = await openStream();
        // A stale response from a previously aborted stream must not render
        // into a different project the user just opened.
        if (!response.ok || !ownActive(projectId, generation)) return;
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        const newline = String.fromCharCode(10);
        const separator = newline + newline;
        let buffer = "";
        while (true) {
          const chunk = await reader.read();
          buffer += decoder.decode(chunk.value || new Uint8Array(), { stream: !chunk.done });
          let boundary;
          while ((boundary = buffer.indexOf(separator)) >= 0) {
            const block = buffer.slice(0, boundary);
            buffer = buffer.slice(boundary + separator.length);
            let eventId = null;
            let data = null;
            block.split(newline).forEach((line) => {
              if (line.startsWith("id: ")) eventId = Number(line.slice(4));
              else if (line.startsWith("data: ")) data = line.slice(6);
            });
            if (data) {
              try {
                const message = JSON.parse(data);
                if (ownActive(projectId, generation)) appendStreamedMessage(message);
              } catch (e) {}
            }
            if (eventId != null && ownActive(projectId, generation) && eventId > (active.lastSequence || 0)) {
              active.lastSequence = Math.max(active.lastSequence || 0, eventId);
            }
          }
          if (chunk.done) break;
        }
        scheduleReconnect();
      } catch (e) {
        if (!controller.signal.aborted && ownActive(projectId, generation)) scheduleReconnect();
      }
    };

    const scheduleReconnect = () => {
      if (controller.signal.aborted || !ownActive(projectId, generation)) return;
      const delay = Math.min(30000, 1000 * Math.pow(2, attempt));
      attempt += 1;
      setTimeout(() => {
        if (controller.signal.aborted || !ownActive(projectId, generation)) return;
        readLoop();
      }, delay);
    };

    readLoop();
  }

  function appendStreamedMessage(message) {
    const box = $("#ws-conversation");
    if (box.querySelector(`[data-sequence="${message.sequence}"]`)) return;
    box.querySelectorAll(".turn-pending").forEach((n) => n.remove());
    renderConversation([message]);
    refreshDrawer("overview").catch(() => {});
    refreshDrawer("activity").catch(() => {});
    refreshDrawer("tasks").catch(() => {});
  }

  function switchDrawerTab(tab) {
    if (!DRAWER_TABS.has(tab)) return;
    document.querySelectorAll("[data-ws-tab]").forEach((b) => b.classList.toggle("active", b.dataset.wsTab === tab));
    document.querySelectorAll("[data-ws-pane]").forEach((p) => p.classList.toggle("active", p.dataset.wsPane === tab));
    refreshDrawer(tab);
  }

  async function refreshDrawer(tab) {
    if (!active || !DRAWER_TABS.has(tab)) return;
    const pane = document.querySelector(`[data-ws-pane="${tab}"]`);
    if (!pane || !pane.classList.contains("active")) return;
    const project = active.projectId;
    const generation = active.generation;
    let html;
    try {
      const harnessView = await loadHarnessView(project);
      if (!ownActive(project, generation)) return;
      active.harnessView = harnessView;
      renderProcessSummary(harnessView?.process);
      if (tab === "overview") html = await overviewPane(project, harnessView);
      else if (tab === "work-graph") html = workGraphPane(harnessView?.work_graph);
      else if (tab === "tasks") html = await tasksPane(project, harnessView?.tasks);
      else if (tab === "activity") html = activityPane(harnessView?.activity);
      else if (tab === "collab") html = await collabPane(project);
      else if (tab === "delivery") html = deliveryPane(harnessView);
      // A slow response from a previously opened project must never overwrite
      // the drawer of the project the user is looking at now.
      if (!ownActive(project, generation)) return;
      pane.innerHTML = html;
      bindPaneActions(pane, tab);
    } catch (e) {
      if (!ownActive(project, generation)) return;
      pane.innerHTML = `<p class="muted small">加载失败：${esc(e.message)}</p>`;
    }
  }

  async function overviewPane(projectId, harnessView = active?.harnessView || {}) {
    const encodedProjectId = encodeURIComponent(projectId);
    const [snapshot, planDrafts, resources, repositories] = await Promise.all([
      W.api(`/v1/projects/${encodedProjectId}/workspace`),
      W.api(`/v1/projects/${encodedProjectId}/plan-drafts`).catch(() => []),
      W.api(`/v1/projects/${encodedProjectId}/resources`).catch(() => []),
      W.api(`/v1/projects/${encodedProjectId}/code/repositories`).catch(() => []),
    ]);
    const repositoryStatuses = await Promise.all((repositories || []).map(async (repository) => {
      try {
        return await W.api(`/v1/projects/${encodedProjectId}/code/repositories/${encodeURIComponent(repository.repository_id)}/status`);
      } catch (error) {
        return {
          repository_bound: true,
          repository_id: repository.repository_id,
          default_branch: repository.default_branch,
          reason: "暂时无法确认源码读取能力",
        };
      }
    }));
    const plan = (planDrafts || []).find((d) => d.status === "approved") || (planDrafts || [])[0];
    const ownTeam = currentTeamId();
    const process = harnessView?.process || {};
    const blockers = normalizeItems(harnessView?.blockers);
    const planCard = plan
      ? `<article class="card overview-plan-card"><div class="card-head"><div><strong>${esc(firstValue(plan, ["goals", "title"], "项目计划建议"))}</strong><div class="meta"><span class="pill ${plan.status === "approved" ? "green" : "orange"}">${stateName(plan.status)}</span></div></div></div><p class="muted">${esc(firstValue(plan, ["scope", "description"], "Agent 已生成项目计划建议。"))}</p><p class="muted small">阶段 ${(plan.phases || []).length} · 风险 ${(plan.risks || []).length} · 验收 ${(plan.acceptance_criteria || []).length}</p>${plan.status === "drafting" ? `<div class="task-actions"><button class="secondary" data-ws-reject-plan="${esc(plan.draft_id)}">拒绝</button><button class="primary" data-ws-approve-plan="${esc(plan.draft_id)}">确认计划</button></div>` : ""}</article>`
      : `<p class="muted small">Agent 尚未生成计划草案；在对话中描述项目目标即可。</p>`;
    const resourceCards = (resources || []).map((resource) => {
      const canShare = resource.owner_team_id === ownTeam && resource.propagation === "team_private";
      const canDownload = resource.owner_team_id === ownTeam || resource.propagation === "portable";
      const mediaType = String(resource.media_type || "").toLowerCase();
      const canAttach = mediaType.startsWith("text/") || ["application/json", "application/xml", "application/yaml", "application/x-yaml", "application/javascript"].includes(mediaType);
      return `<article class="card resource-summary-card"><div class="card-head"><div><strong>${esc(resource.title || "未命名资料")}</strong><div class="meta"><span class="pill ${resource.propagation === "team_private" ? "orange" : "green"}">${propagationLabel[resource.propagation] || "项目可见"}</span></div></div></div><div class="task-actions"><button class="secondary" data-ws-preview-resource="${esc(resource.resource_id)}" data-resource-title="${esc(resource.title || "项目资料")}">预览</button>${canAttach ? `<button class="primary" data-ws-attach-resource="${esc(resource.resource_id)}">加入对话</button>` : ""}${canDownload ? `<button class="secondary" data-ws-download-resource="${esc(resource.resource_id)}" data-resource-title="${esc(resource.title || "项目资料")}">下载资料</button>` : ""}${canShare ? `<button class="secondary" data-ws-share-resource="${esc(resource.resource_id)}">共享到项目</button>` : ""}</div></article>`;
    }).join("");
    const operationLabels = {
      read_tree: "浏览目录",
      read_blob: "读取文件",
      search_code: "搜索代码",
      read_issue: "读取 Issue",
      read_pull_request: "读取 Pull Request",
    };
    const repositoryCards = (repositories || []).map((repository, index) => {
      const status = repositoryStatuses[index] || {};
      const readable = status.repository_bound === true && !status.reason;
      const operations = (repository.available_operations || [])
        .map((operation) => operationLabels[operation] || operation)
        .map((label) => `<span class="pill">${esc(label)}</span>`)
        .join("");
      return `<article class="card repository-summary-card">
        <div class="card-head"><div><strong>${esc(repository.remote_repository_id || repository.repository_id)}</strong>
          <div class="meta"><span>${esc(repository.default_branch || "未设置默认分支")}</span><span>${esc(repository.connector_id)}</span><span class="pill ${readable ? "green" : "orange"}">${readable ? "源码读取可用" : "仓库已绑定"}</span></div>
        </div></div>
        <div class="meta repository-operations">${operations}</div>
        ${status.reason ? `<p class="muted small repository-capability-note">${esc(status.reason)}</p>` : `<p class="muted small repository-capability-note">Agent 可在固定提交上读取此仓库的源码上下文。</p>`}
      </article>`;
    }).join("");
    const blockerSummary = blockers.length
      ? `<div class="overview-blockers"><strong>当前阻塞</strong>${blockers.map((item) => `<p class="muted small">${esc(firstValue(item, ["label", "summary", "reason"], item))}</p>`).join("")}</div>`
      : `<p class="muted small">当前没有已记录的阻塞。</p>`;
    return `
      <div class="overview-actions"><button class="primary" data-ws-upload>上传资料</button></div>
      <section class="overview-section"><h4>项目进度</h4><div class="overview-process-card"><span class="process-state">${esc(firstValue(process, ["semantic_status"], semanticState(firstValue(process, ["status", "phase"], ""))))}</span>${firstValue(process, ["next_step", "next_action"]) ? `<span class="muted small">下一步：${esc(firstValue(process, ["next_step", "next_action"]))}</span>` : ""}</div>${blockerSummary}</section>
      <section class="overview-section"><h4>目标与计划</h4>${planCard}</section>
      <section class="overview-section"><h4>项目概况</h4><div class="meta overview-stats"><span>任务 ${Number(snapshot?.task_count || 0)}</span><span>资料 ${Number(snapshot?.resource_count || 0)}</span><span>待处理 ${Number(snapshot?.pending_draft_count || 0)}</span></div></section>
      <section class="overview-section"><h4>参与团队</h4>${(snapshot?.teams || []).map((team) => `<div class="meta"><span>${esc(team.name)}</span><span class="pill">${esc(team.kind)}</span></div>`).join("") || `<p class="muted small">尚无参与团队</p>`}</section>
      <section class="overview-section"><h4>代码仓库</h4>${repositoryCards || `<p class="muted small">尚未绑定代码仓库。绑定后可在这里查看仓库及其可用能力。</p>`}</section>
      <section class="overview-section"><h4>项目资料</h4>${resourceCards || `<p class="muted small">还没有项目资料。上传的文件默认仅本团队可见，可稍后共享到项目。</p>`}</section>`;
  }

  function normalizeItems(value) {
    if (Array.isArray(value)) return value;
    if (!value || typeof value !== "object") return [];
    const items = value.items || value.nodes || value.results || value.entries || [];
    return Array.isArray(items) ? items : [];
  }

  function graphNodes(graph) {
    const nodes = normalizeItems(graph);
    if (nodes.length) return nodes;
    if (!graph || typeof graph !== "object") return [];
    return [
      ...(graph.goals || []).map((item) => ({ ...item, type: "goal" })),
      ...(graph.milestones || []).map((item) => ({ ...item, type: "milestone" })),
      ...(graph.phases || []).map((item) => ({ ...item, type: "phase" })),
      ...(graph.tasks || []).map((item) => ({ ...item, type: "task" })),
      ...(graph.risks || []).map((item) => ({ ...item, type: "risk" })),
      ...(graph.artifacts || []).map((item) => ({ ...item, type: "artifact" })),
    ];
  }

  function workGraphPane(graph) {
    const nodes = graphNodes(graph);
    if (!nodes.length) return `<p class="muted small">Work Graph 尚未形成。确认项目计划后，目标、阶段、任务和风险会在这里按层级展示。</p>`;
    const relations = normalizeItems(graph?.relations || graph?.edges);
    const nodeById = new Map();
    nodes.forEach((node) => {
      const nodeId = firstValue(node, ["node_id", "id", "work_node_id"], "");
      if (nodeId) nodeById.set(nodeId, node);
    });
    const parentById = new Map();
    relations.forEach((relation) => {
      const kind = String(firstValue(relation, ["kind", "relation_type", "type"], "")).toLowerCase();
      const source = firstValue(relation, ["source_id", "source", "from"], "");
      const target = firstValue(relation, ["target_id", "target", "to"], "");
      const sourceNode = nodeById.get(source);
      const sourceType = String(firstValue(sourceNode, ["type", "node_type", "kind"], "")).toLowerCase();
      const isHierarchy = kind === "part_of" || kind === "derived_from" || (kind === "relates_to" && sourceType === "risk");
      if (isHierarchy && source !== target && nodeById.has(source) && nodeById.has(target) && !parentById.has(source)) {
        parentById.set(source, target);
      }
    });

    // Older plan projections did not persist milestone -> goal relations. Keep
    // those graphs readable without inventing a parent when more than one goal
    // exists; new explicit relations always win over this compatibility rule.
    const goalIds = nodes
      .filter((node) => String(firstValue(node, ["type", "node_type", "kind"], "")).toLowerCase() === "goal")
      .map((node) => firstValue(node, ["node_id", "id", "work_node_id"], ""))
      .filter(Boolean);
    if (goalIds.length === 1) {
      nodes.forEach((node) => {
        const nodeId = firstValue(node, ["node_id", "id", "work_node_id"], "");
        const nodeType = String(firstValue(node, ["type", "node_type", "kind"], "")).toLowerCase();
        if (nodeType === "milestone" && nodeId && !parentById.has(nodeId)) parentById.set(nodeId, goalIds[0]);
      });
    }

    const dependencyCount = new Map();
    relations.forEach((relation) => {
      const kind = String(firstValue(relation, ["kind", "relation_type", "type"], "")).toLowerCase();
      if (kind !== "depends_on" && kind !== "dependency") return;
      const dependent = firstValue(relation, ["source_id", "source", "from"], "");
      if (dependent) dependencyCount.set(dependent, (dependencyCount.get(dependent) || 0) + 1);
    });
    const childrenById = new Map();
    parentById.forEach((parentId, childId) => {
      if (!childrenById.has(parentId)) childrenById.set(parentId, []);
      childrenById.get(parentId).push(childId);
    });
    const typeOrder = { goal: 0, requirement: 1, milestone: 2, phase: 3, task: 4, risk: 5, artifact: 6, verification: 7 };
    const typeLabel = { goal: "目标", requirement: "需求", milestone: "里程碑", phase: "阶段", task: "任务", risk: "风险", artifact: "资料", verification: "验收" };
    const compareNodes = (left, right) => {
      const a = String(firstValue(left, ["type", "node_type", "kind"], "")).toLowerCase();
      const b = String(firstValue(right, ["type", "node_type", "kind"], "")).toLowerCase();
      const order = (typeOrder[a] ?? 9) - (typeOrder[b] ?? 9);
      if (order) return order;
      return String(firstValue(left, ["title", "name", "label"], "")).localeCompare(String(firstValue(right, ["title", "name", "label"], "")), "zh-CN");
    };
    childrenById.forEach((childIds) => childIds.sort((left, right) => compareNodes(nodeById.get(left), nodeById.get(right))));
    const roots = nodes.filter((node) => {
      const nodeId = firstValue(node, ["node_id", "id", "work_node_id"], "");
      return !nodeId || !parentById.has(nodeId);
    }).sort(compareNodes);
    const rendered = new Set();
    const renderNode = (node, ancestors = new Set()) => {
      const nodeType = String(firstValue(node, ["type", "node_type", "kind"], "work")).toLowerCase();
      const nodeId = firstValue(node, ["node_id", "id", "work_node_id"], "");
      if (nodeId) rendered.add(nodeId);
      const count = dependencyCount.get(nodeId) || 0;
      const nextAncestors = new Set(ancestors);
      if (nodeId) nextAncestors.add(nodeId);
      const childRows = (childrenById.get(nodeId) || [])
        .filter((childId) => !nextAncestors.has(childId))
        .map((childId) => renderNode(nodeById.get(childId), nextAncestors))
        .join("");
      return `<li class="work-node" data-work-node-id="${esc(nodeId)}"><div class="work-node-line"><span class="work-node-type">${esc(typeLabel[nodeType] || "工作项")}</span><strong>${esc(firstValue(node, ["title", "name", "label"], "未命名工作项"))}</strong><span class="pill work-node-status">${esc(stateName(firstValue(node, ["status", "state"], "待处理")))}</span>${count ? `<span class="dependency-badge">依赖 ${count}</span>` : ""}</div>${firstValue(node, ["description", "summary"]) ? `<p class="muted small">${esc(firstValue(node, ["description", "summary"]))}</p>` : ""}${childRows ? `<ul class="work-node-children">${childRows}</ul>` : ""}</li>`;
    };
    let rows = roots.map((node) => renderNode(node)).join("");
    // Malformed cyclic relations must never hide nodes from the participant.
    nodes.slice().sort(compareNodes).forEach((node) => {
      const nodeId = firstValue(node, ["node_id", "id", "work_node_id"], "");
      if (nodeId && !rendered.has(nodeId)) rows += renderNode(node);
    });
    return `<p class="muted small">项目事实以 Work Graph 为准；依赖关系会决定任务何时可以开始。</p><ul class="work-graph-tree">${rows}</ul>`;
  }

  async function tasksPane(projectId, tasksValue) {
    let tasks = normalizeItems(tasksValue);
    if (!tasks.length) {
      try { tasks = await W.api(`/v1/projects/${encodeURIComponent(projectId)}/tasks`); } catch (e) { tasks = []; }
    }
    if (!tasks.length) return `<p class="muted small">没有团队任务。由 Agent 起草或人工布置的任务会出现在这里。</p>`;
    return `<p class="muted small">任务状态来自项目 Work Graph；Agent 执行记录会在 Agent Activity 中以安全摘要显示。</p>` + tasks.map((task) => {
      const team = firstValue(task, ["target_team_name", "team_name", "team"], "参与团队");
      const status = firstValue(task, ["status", "state"], "pending");
      const dependencies = Array.isArray(task.depends_on) ? task.depends_on : (Array.isArray(task.dependencies) ? task.dependencies : []);
      const contract = task.contract_ready ? `契约 v${Number(task.contract_version || 1)} 已确认` : "执行契约待确认";
      return `<article class="card task-summary-card"><div class="card-head"><div><strong>${esc(firstValue(task, ["title", "name", "label"], "未命名任务"))}</strong><div class="meta"><span class="pill ${status === "verified" || status === "completed" ? "green" : "orange"}">${esc(stateName(status))}</span><span>${esc(team)}</span><span class="pill ${task.contract_ready ? "green" : "orange"}">${contract}</span>${dependencies.length ? `<span class="dependency-badge">依赖 ${dependencies.length}</span>` : ""}</div></div></div>${firstValue(task, ["acceptance_criteria", "description", "summary"]) ? `<p class="muted">${esc(firstValue(task, ["acceptance_criteria", "description", "summary"]))}</p>` : ""}</article>`;
    }).join("");
  }

  function activityPane(activityValue) {
    const activities = normalizeItems(activityValue);
    if (!activities.length) return `<p class="muted small">暂无 Agent Activity。项目 Agent 开始分析、执行或等待时，安全摘要会显示在这里。</p>`;
    const rows = activities.map((item) => {
      const label = firstValue(item, ["label"], "Agent 活动");
      const status = semanticActivityStatus(firstValue(item, ["status"], "pending"));
      const time = firstValue(item, ["occurred_at", "time"], "");
      const team = firstValue(item, ["team_name", "team"], "项目 Harness");
      return `<li class="activity-row"><span class="activity-dot" aria-hidden="true"></span><div class="activity-body"><strong>${esc(label)}</strong><div class="meta"><span>${esc(status)}</span><span>${esc(team)}</span>${time ? `<time datetime="${esc(time)}">${esc(time)}</time>` : ""}</div></div></li>`;
    }).join("");
    return `<p class="muted small">这里仅显示可共享的语义进展，不展示内部执行细节。</p><ol class="activity-list">${rows}</ol>`;
  }

  function deliveryPane(harnessView = {}) {
    const verification = harnessView.verification || {};
    const completion = harnessView.completion || {};
    const verificationTotal = Number(verification.total || 0);
    const verificationPassed = Number(verification.passed || 0);
    const verificationFailed = Number(verification.failed || 0);
    const verificationPending = Number(verification.pending || 0);
    const tasksDone = Number(completion.tasks_done || 0);
    const tasksTotal = Number(completion.tasks_total || 0);
    const verificationState = verificationFailed ? "需要处理" : verificationPending ? "验证中" : verificationTotal ? "已通过" : "待开始";
    const completionState = completion.delivery_status || completion.contract_status || "待准备";
    const evaluation = completion.evaluation_passed === true ? "完成条件已满足" : completion.evaluation_passed === false ? "完成条件尚未满足" : "尚未执行完成评估";
    return `<section class="delivery-section"><h4>验收</h4><div class="delivery-status"><span class="pill ${verificationFailed ? "orange" : verificationTotal && verificationPassed === verificationTotal ? "green" : ""}">${verificationState}</span><span class="muted small">通过 ${verificationPassed} · 处理中 ${verificationPending} · 未通过 ${verificationFailed}</span></div></section><section class="delivery-section"><h4>完成进度</h4><div class="delivery-status"><span class="pill ${String(completion.delivery_status || "").toUpperCase() === "ACCEPTED" ? "green" : "orange"}">${esc(stateName(completionState))}</span><span class="muted small">任务 ${tasksDone}/${tasksTotal} · ${evaluation}</span></div></section>`;
  }

  async function collabPane(projectId) {
    let drafts = [];
    let exchanges = [];
    try { drafts = await W.api(`/v1/projects/${encodeURIComponent(projectId)}/agent-exchange-drafts`); } catch (e) {}
    try { exchanges = await W.api(`/v1/projects/${encodeURIComponent(projectId)}/agent-exchanges`); } catch (e) {}
    const ownTeam = currentTeamId();
    const draftCards = (drafts || []).map((d) => `
      <article class="card">
        <div class="card-head"><div><strong>${esc(d.purpose)}</strong><div class="meta"><span class="pill ${d.status === "approved" ? "green" : "orange"}">${stateName(d.status)}</span><span>v${d.version}</span></div></div></div>
        <p class="muted">${esc(d.summary)}</p>
        <div class="meta"><span>接收：${(d.recipient_team_ids || []).join("、")}</span></div>
        ${d.status === "drafting" ? `<div class="task-actions"><button class="secondary" data-ws-edit-draft="${esc(d.draft_id)}">编辑</button><button class="secondary" data-ws-reject-draft="${esc(d.draft_id)}">拒绝</button><button class="primary" data-ws-confirm-draft="${esc(d.draft_id)}">确认并发送</button></div>` : ""}
      </article>`).join("");
    const exchangeRows = [];
    for (const x of exchanges || []) {
      const inbound = x.source_team_id !== ownTeam;
      let receive = null;
      let responses = [];
      let actions = "";
      try {
        responses = await W.api(`/v1/projects/${encodeURIComponent(projectId)}/agent-exchanges/${encodeURIComponent(x.exchange_id)}/responses`);
      } catch (e) {}
      if (inbound) {
        try {
          const recipients = await W.api(`/v1/projects/${encodeURIComponent(projectId)}/agent-exchanges/${encodeURIComponent(x.exchange_id)}/recipients`);
          receive = recipients.find((r) => r.recipient_team_id === ownTeam) || null;
        } catch (e) {}
        // A multi-team exchange must not hide this team's confirm entry just
        // because another recipient already replied; base the actions on this
        // team's own recipient state.
        const ownResponded = receive?.status === "responded";
        if (x.status !== "closed" && !ownResponded) {
          if (receive && receive.status === "drafting" && receive.draft_content) {
            actions = `<div class="draft-box"><p class="muted small">本团队 Agent 起草的回复（待你确认）：</p><p>${esc(receive.draft_content)}</p><div class="task-actions"><button class="secondary" data-ws-draft-reply="${esc(x.exchange_id)}">重新起草</button><button class="primary" data-ws-confirm-reply="${esc(x.exchange_id)}">确认并回复</button></div></div>`;
          } else if (receive && receive.draft_turn_id) {
            actions = `<p class="muted small">Agent 正在起草回复…</p>`;
          } else {
            actions = `<div class="task-actions"><button class="secondary" data-ws-respond-exchange="${esc(x.exchange_id)}">手动回复</button><button class="primary" data-ws-draft-reply="${esc(x.exchange_id)}">让本团队 Agent 起草回复</button></div>`;
          }
        }
      }
      const responseCards = (responses || []).map((response) => `
        <div class="draft-box exchange-response">
          <p class="muted small">${inbound ? "本团队已确认的回复" : `来自 ${esc(response.recipient_team_id)} 的回复`}</p>
          <p>${esc(response.content)}</p>
        </div>`).join("");
      exchangeRows.push(`<article class="card">
        <div class="card-head"><div><strong>${esc(x.purpose)}</strong><div class="meta"><span class="pill ${inbound ? "orange" : "green"}">${inbound ? "收到的请求" : "已发出"}</span><span>${esc(x.source_team_id)}</span><span>${stateName(x.status)}</span></div></div></div>
        <p class="muted">${esc(x.summary)}</p>
        ${responseCards}
        ${actions}
      </article>`);
    }
    const button = `<div class="actions"><button class="primary" data-ws-new-exchange>发起 Agent 共享草稿</button></div>`;
    const draftSection = draftCards
      ? `<h4>待确认共享草稿</h4>${draftCards}`
      : "";
    return button + draftSection +
      (exchangeRows.join("") || `<p class="muted small">还没有已发送的跨团队 Agent 共享。草稿经你确认后才会发送给对方团队 Agent。</p>`);
  }
  function bindPaneActions(pane, tab) {
    pane.querySelectorAll("[data-ws-attach-resource]").forEach((button) =>
      button.addEventListener("click", () => attachResourceToConversation(button.dataset.wsAttachResource))
    );
    pane.querySelectorAll("[data-ws-preview-resource]").forEach((button) =>
      button.addEventListener("click", () => previewResource(button.dataset.wsPreviewResource, button.dataset.resourceTitle))
    );
    pane.querySelectorAll("[data-ws-download-resource]").forEach((button) =>
      button.addEventListener("click", () => downloadResource(button.dataset.wsDownloadResource, button.dataset.resourceTitle))
    );
    pane.querySelectorAll("[data-ws-upload]").forEach((b) => b.addEventListener("click", uploadAttachment));
    pane.querySelectorAll("[data-ws-share-resource]").forEach((b) =>
      b.addEventListener("click", () => shareResource(b.dataset.wsShareResource))
    );
    pane.querySelectorAll("[data-ws-new-exchange]").forEach((b) => b.addEventListener("click", newExchange));
    pane.querySelectorAll("[data-ws-edit-draft]").forEach((b) =>
      b.addEventListener("click", () => editDraft(b.dataset.wsEditDraft))
    );
    pane.querySelectorAll("[data-ws-confirm-draft]").forEach((b) =>
      b.addEventListener("click", () => confirmDraft(b.dataset.wsConfirmDraft))
    );
    pane.querySelectorAll("[data-ws-reject-draft]").forEach((b) =>
      b.addEventListener("click", () => rejectDraft(b.dataset.wsRejectDraft))
    );
    pane.querySelectorAll("[data-ws-respond-exchange]").forEach((b) =>
      b.addEventListener("click", () => respondExchange(b.dataset.wsRespondExchange))
    );
    pane.querySelectorAll("[data-ws-draft-reply]").forEach((b) =>
      b.addEventListener("click", () => requestDraftReply(b.dataset.wsDraftReply))
    );
    pane.querySelectorAll("[data-ws-confirm-reply]").forEach((b) =>
      b.addEventListener("click", () => confirmDraftReply(b.dataset.wsConfirmReply))
    );
    pane.querySelectorAll("[data-ws-approve-plan]").forEach((b) =>
      b.addEventListener("click", () => decidePlan(b.dataset.wsApprovePlan, "approve"))
    );
    pane.querySelectorAll("[data-ws-reject-plan]").forEach((b) =>
      b.addEventListener("click", () => decidePlan(b.dataset.wsRejectPlan, "reject"))
    );
  }

  function attachResourceToConversation(resourceId) {
    if (!active || !resourceId) return;
    if (pendingAttachments.includes(resourceId)) {
      toast("这份资料已在待发送附件中");
      return;
    }
    pendingAttachments.push(resourceId);
    renderAttachmentChips();
    $("#ws-input")?.focus();
    toast("资料已加入对话，请输入分析要求后发送");
  }

  async function previewResource(resourceId, title) {
    if (!active) return;
    const projectId = active.projectId, generation = active.generation;
    try {
      const response = await fetch(`/v1/projects/${encodeURIComponent(projectId)}/resources/${encodeURIComponent(resourceId)}/preview`, {
        headers: { Authorization: `Bearer ${W.token}` }, cache: "no-store",
      });
      if (!response.ok) throw Error("资料不可预览或访问权限已改变");
      const blob = await response.blob();
      if (!ownActive(projectId, generation)) return;
      const type = String(blob.type || response.headers.get("content-type") || "application/octet-stream").toLowerCase();
      const dialog = $("#resource-preview"), body = $("#resource-preview-body");
      if (workspacePreviewUrl) URL.revokeObjectURL(workspacePreviewUrl);
      workspacePreviewUrl = null;
      body.replaceChildren();
      $("#resource-preview-title").textContent = title || "项目资料预览";
      if (type.startsWith("text/") || ["application/json", "application/xml", "application/yaml", "application/x-yaml", "application/javascript"].includes(type)) {
        const text = await blob.text();
        if (!ownActive(projectId, generation)) return;
        const pre = document.createElement("pre"), limit = 200000;
        pre.textContent = text.slice(0, limit) + (text.length > limit ? "\n\n[预览已截断]" : "");
        body.append(pre);
      } else if (["image/png", "image/jpeg", "image/gif", "image/webp"].includes(type)) {
        workspacePreviewUrl = URL.createObjectURL(blob);
        const image = document.createElement("img");
        image.src = workspacePreviewUrl;
        image.alt = title || "项目资料";
        body.append(image);
      } else if (type === "application/pdf") {
        workspacePreviewUrl = URL.createObjectURL(blob);
        const frame = document.createElement("iframe");
        frame.src = workspacePreviewUrl;
        frame.title = title || "PDF 项目资料";
        body.append(frame);
      } else {
        body.textContent = "该文件类型不支持网页预览；如有下载权限，可下载后使用本地应用打开。";
      }
      dialog.onclose = () => {
        if (workspacePreviewUrl) URL.revokeObjectURL(workspacePreviewUrl);
        workspacePreviewUrl = null;
        body.replaceChildren();
      };
      dialog.showModal();
    } catch (error) {
      if (ownActive(projectId, generation)) toast(error.message, true);
    }
  }

  async function downloadResource(resourceId, title) {
    if (!active) return;
    const projectId = active.projectId, generation = active.generation;
    try {
      const response = await fetch(`/v1/projects/${encodeURIComponent(projectId)}/resources/${encodeURIComponent(resourceId)}/content`, {
        headers: { Authorization: `Bearer ${W.token}` }, cache: "no-store",
      });
      if (!response.ok) throw Error("资料不可下载或访问权限已改变");
      const blob = await response.blob();
      if (!ownActive(projectId, generation)) return;
      const url = URL.createObjectURL(blob), link = document.createElement("a");
      link.href = url;
      link.download = String(title || "项目资料").replace(/[\\/:*?"<>|]/g, "_") +
        (response.headers.get("content-type")?.includes("application/json") ? ".json" : "");
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (error) {
      if (ownActive(projectId, generation)) toast(error.message, true);
    }
  }

  function uploadAttachment() {
    if (!active) return;
    const dialog = $("#modal");
    $("#modal-title").textContent = "上传项目资料";
    $("#modal-fields").innerHTML = `
      <p class="muted">文件进入项目资料库。默认仅本团队可见，Agent 可以用它分析；共享到项目后其他团队 Agent 才能看到。</p>
      <label class="field">文件<input name="content" type="file" required></label>
      <label class="field">资料标题<input name="title" required maxlength="256"></label>
      <label class="field">可见范围<select name="propagation"><option value="team_private" selected>仅本团队</option><option value="project_readonly">项目共享（其他团队只读）</option></select></label>`;
    const fileInput = paneOr(dialog, "form [name=content]");
    const titleInput = dialog.querySelector("[name=title]");
    fileInput.addEventListener("change", () => {
      if (!titleInput.value && fileInput.files[0]) titleInput.value = fileInput.files[0].name;
    });
    dialog.showModal();
    $("#modal-form").onsubmit = async (e) => {
      e.preventDefault();
      const values = new FormData(e.target);
      const body = new FormData();
      body.set("title", values.get("title"));
      body.set("propagation", values.get("propagation"));
      body.set("content", values.get("content"));
      try {
        const r = await fetch(`/v1/projects/${encodeURIComponent(active.projectId)}/resources:upload`, {
          method: "POST",
          headers: { Authorization: `Bearer ${W.token}`, "Idempotency-Key": uid("up") },
          body,
          cache: "no-store",
        });
        if (!r.ok) {
          let p = {};
          try { p = await r.json(); } catch (err) {}
          throw Error(p.detail || p.title || `上传失败 (${r.status})`);
        }
        const created = await r.json();
        dialog.close();
        if (created && created.resource_id) {
          pendingAttachments.push(created.resource_id);
          renderAttachmentChips();
          toast("资料已上传并加入待发送附件");
        } else {
          toast("资料已上传");
        }
        refreshDrawer("overview");
      } catch (err) {
        toast(err.message, true);
      }
    };
  }

  function paneOr(dialog, selector) {
    return dialog.querySelector(selector);
  }

  async function shareResource(resourceId) {
    if (!active) return;
    const dialog = $("#modal");
    $("#modal-title").textContent = "共享到项目";
    $("#modal-fields").innerHTML = `<p class="muted">将「仅本团队」资料改为「项目共享」后，其他参与团队的项目 Agent 可以引用它。此操作会改变可见边界。</p><label class="field">确认操作<select name="confirm"><option value="no">再想想</option><option value="yes">确认共享到项目</option></select></label>`;
    dialog.showModal();
    $("#modal-form").onsubmit = async (e) => {
      e.preventDefault();
      const values = Object.fromEntries(new FormData(e.target));
      if (values.confirm !== "yes") { dialog.close(); return; }
      try {
        await W.api(`/v1/projects/${encodeURIComponent(active.projectId)}/resources/${encodeURIComponent(resourceId)}/propagation`, {
          method: "PATCH",
          body: JSON.stringify({
            propagation: "project_readonly",
            expected_propagation: "team_private",
          }),
        });
        dialog.close();
        toast("已共享到项目");
        refreshDrawer("overview");
      } catch (err) {
        toast(err.message, true);
      }
    };
  }

  function projectTeamOptions() {
    const snapshot = active?.teams || [];
    return snapshot;
  }

  async function newExchange() {
    if (!active) return;
    let project = {};
    try { project = await W.api(`/v1/projects/${encodeURIComponent(active.projectId)}/workspace`); } catch (e) {}
    active.teams = project.teams || [];
    const dialog = $("#modal");
    const ownTeam = currentTeamId();
    const teamOptions = (project.teams || [])
      .filter((t) => t.team_id !== ownTeam)
      .map((t) => `<label class="field"><input type="checkbox" name="recipient" value="${esc(t.team_id)}"> ${esc(t.name)}（${esc(t.team_id)}）</label>`)
      .join("");
    $("#modal-title").textContent = "发起 Agent 共享（草稿）";
    $("#modal-fields").innerHTML = `
      <p class="muted">让 Agent 从当前对话生成草稿，或手工填写；提交后你会在协作面板再次确认。只有你确认的文字与项目共享资料会离开团队；本团队私有文件永不外发。</p>
      <button type="button" class="secondary" data-assistant-generate>由 Agent 从当前对话生成草稿</button>
      <label class="field">目的<input name="purpose" required maxlength="512" placeholder="例如：确认排期"></label>
      <label class="field">接收团队（可多选）${teamOptions || '<span class="muted">暂无其他参与团队</span>'}</label>
      <label class="field">摘要<textarea name="summary" required placeholder="对方 Agent 看到的摘要"></textarea></label>
      <label class="field">请求内容<textarea name="request" required placeholder="你希望对方团队做什么或确认什么"></textarea></label>
      <label class="field">约束（可选）<textarea name="constraints" placeholder="时间、范围或其他限制"></textarea></label>`;
    dialog.showModal();
    dialog.querySelector("[data-assistant-generate]")?.addEventListener("click", async () => {
      const formData = new FormData($("#modal-form"));
      const recipients = formData.getAll("recipient").filter((v) => typeof v === "string" && v);
      if (!recipients.length) return toast("至少选择一个接收团队", true);
      try {
        await W.api(`/v1/projects/${encodeURIComponent(active.projectId)}/agent-exchange-drafts:generate`, {
          method: "POST",
          body: JSON.stringify({
            recipient_team_ids: recipients,
            shared_resource_ids: pendingAttachments.slice(),
            source_conversation_id: active.conversationId,
          }),
        });
        dialog.close();
        toast("本团队 Agent 正在根据对话起草共享草稿，完成后会出现在协作面板");
        refreshDrawer("collab");
      } catch (err) {
        toast(err.message, true);
      }
    });
    $("#modal-form").onsubmit = async (e) => {
      e.preventDefault();
      const formData = new FormData(e.target);
      const recipients = formData.getAll("recipient").filter((v) => typeof v === "string" && v);
      if (!recipients.length) return toast("至少选择一个接收团队", true);
      try {
        await W.api(`/v1/projects/${encodeURIComponent(active.projectId)}/agent-exchange-drafts`, {
          method: "POST",
          body: JSON.stringify({
            purpose: formData.get("purpose"),
            summary: formData.get("summary"),
            request: formData.get("request"),
            constraints: formData.get("constraints") || "",
            recipient_team_ids: recipients,
            shared_resource_ids: pendingAttachments.slice(),
            source_conversation_id: active.conversationId,
            source_turn_id: active.pendingTurn || null,
          }),
        });
        dialog.close();
        toast("共享草稿已创建，请在协作面板确认后发送");
        refreshDrawer("collab");
      } catch (err) {
        toast(err.message, true);
      }
    };
  }

  async function requestDraftReply(exchangeId) {
    if (!active) return;
    try {
      await W.api(`/v1/projects/${encodeURIComponent(active.projectId)}/agent-exchanges/${encodeURIComponent(exchangeId)}:draft-turn`, {
        method: "POST",
        body: JSON.stringify({}),
      });
      toast("本团队 Agent 已开始起草回复");
      refreshDrawer("collab");
    } catch (err) {
      toast(err.message, true);
    }
  }

  async function confirmDraftReply(exchangeId) {
    if (!active) return;
    let receive = null;
    try {
      const recipients = await W.api(`/v1/projects/${encodeURIComponent(active.projectId)}/agent-exchanges/${encodeURIComponent(exchangeId)}/recipients`);
      const ownTeam = currentTeamId();
      receive = recipients.find((r) => r.recipient_team_id === ownTeam) || null;
    } catch (e) {}
    const dialog = $("#modal");
    $("#modal-title").textContent = "确认并回复 Agent 共享";
    $("#modal-fields").innerHTML = `
      <p class="muted">确认后这份回复才会发回来源团队；你可以先修改再发送。</p>
      <label class="field">回复内容<textarea name="content" required>${esc(receive?.draft_content || "")}</textarea></label>`;
    dialog.showModal();
    $("#modal-form").onsubmit = async (e) => {
      e.preventDefault();
      const v = Object.fromEntries(new FormData(e.target));
      try {
        await W.api(`/v1/projects/${encodeURIComponent(active.projectId)}/agent-exchanges/${encodeURIComponent(exchangeId)}/responses`, {
          method: "POST",
          body: JSON.stringify({ content: v.content }),
        });
        dialog.close();
        toast("回复已发送回来源团队");
        refreshDrawer("collab");
      } catch (err) {
        toast(err.message, true);
      }
    };
  }

  function respondExchange(exchangeId) {
    if (!active) return;
    const dialog = $("#modal");
    $("#modal-title").textContent = "回复 Agent 共享";
    $("#modal-fields").innerHTML = `<label class="field">回复内容<textarea name="content" required placeholder="对方团队 Agent 只会看到你确认的回复"></textarea></label>`;
    dialog.showModal();
    $("#modal-form").onsubmit = async (e) => {
      e.preventDefault();
      const formData = new FormData(e.target);
      try {
        const response = await W.api(`/v1/projects/${encodeURIComponent(active.projectId)}/agent-exchanges/${encodeURIComponent(exchangeId)}/responses`, {
          method: "POST",
          body: JSON.stringify({ content: formData.get("content") }),
        });
        dialog.close();
        toast("回复已发送回来源团队");
        refreshDrawer("collab");
      } catch (err) {
        toast(err.message, true);
      }
    };
  }

  async function editDraft(draftId) {
    if (!active) return;
    let draft = null;
    try {
      const drafts = await W.api(`/v1/projects/${encodeURIComponent(active.projectId)}/agent-exchange-drafts`);
      draft = drafts.find((d) => d.draft_id === draftId);
    } catch (e) {}
    if (!draft) return toast("草稿不存在", true);
    const dialog = $("#modal");
    $("#modal-title").textContent = "编辑共享草稿";
    $("#modal-fields").innerHTML = `
      <label class="field">目的<input name="purpose" required maxlength="512" value="${esc(draft.purpose)}"></label>
      <label class="field">摘要<textarea name="summary" required>${esc(draft.summary)}</textarea></label>
      <label class="field">请求内容<textarea name="request" required>${esc(draft.request)}</textarea></label>
      <label class="field">约束（可选）<textarea name="constraints">${esc(draft.constraints)}</textarea></label>`;
    dialog.showModal();
    $("#modal-form").onsubmit = async (e) => {
      e.preventDefault();
      const formData = new FormData(e.target);
      try {
        await W.api(`/v1/projects/${encodeURIComponent(active.projectId)}/agent-exchange-drafts/${encodeURIComponent(draftId)}`, {
          method: "PATCH",
          body: JSON.stringify({
            expected_version: draft.version,
            purpose: formData.get("purpose"),
            summary: formData.get("summary"),
            request: formData.get("request"),
            constraints: formData.get("constraints") || "",
            recipient_team_ids: draft.recipient_team_ids,
            shared_resource_ids: draft.shared_resource_ids,
          }),
        });
        dialog.close();
        toast("草稿已更新");
        refreshDrawer("collab");
      } catch (err) {
        toast(err.message, true);
      }
    };
  }

  async function confirmDraft(draftId) {
    if (!active) return;
    let draft = null;
    try {
      const drafts = await W.api(`/v1/projects/${encodeURIComponent(active.projectId)}/agent-exchange-drafts`);
      draft = drafts.find((d) => d.draft_id === draftId);
    } catch (e) {}
    if (!draft) return toast("草稿不存在", true);
    const dialog = $("#modal");
    $("#modal-title").textContent = "确认并发送 Agent 共享";
    $("#modal-fields").innerHTML = `
      <p class="muted">确认后该上下文包将发送给接收团队的项目 Agent。服务端会校验：本团队私有文件绝不会包含在共享包中。</p>
      <div class="card"><strong>${esc(draft.purpose)}</strong><p class="muted">${esc(draft.summary)}</p><div class="meta"><span>接收：${(draft.recipient_team_ids || []).join("、")}</span></div><div class="meta"><span>共享资料：${(draft.shared_resource_ids || []).join("、") || "无"}</span></div></div>`;
    dialog.showModal();
    $("#modal-form").onsubmit = async (e) => {
      e.preventDefault();
      try {
        await W.api(`/v1/projects/${encodeURIComponent(active.projectId)}/agent-exchange-drafts/${encodeURIComponent(draftId)}:approve`, {
          method: "POST",
          body: JSON.stringify({ expected_version: draft.version }),
        });
        dialog.close();
        toast("共享包已确认并发送给对方团队 Agent");
        refreshDrawer("collab");
      } catch (err) {
        toast(err.message, true);
      }
    };
  }

  async function rejectDraft(draftId) {
    if (!active) return;
    let draft = null;
    try {
      const drafts = await W.api(`/v1/projects/${encodeURIComponent(active.projectId)}/agent-exchange-drafts`);
      draft = drafts.find((d) => d.draft_id === draftId);
    } catch (e) {}
    if (!draft) return toast("草稿不存在", true);
    const dialog = $("#modal");
    $("#modal-title").textContent = "拒绝共享草稿";
    $("#modal-fields").innerHTML = `<label class="field">拒绝原因<textarea name="reason" required placeholder="为什么不需要发送"></textarea></label>`;
    dialog.showModal();
    $("#modal-form").onsubmit = async (e) => {
      e.preventDefault();
      const v = Object.fromEntries(new FormData(e.target));
      try {
        await W.api(`/v1/projects/${encodeURIComponent(active.projectId)}/agent-exchange-drafts/${encodeURIComponent(draftId)}:reject`, {
          method: "POST",
          body: JSON.stringify({ expected_version: draft.version, reason: v.reason || "" }),
        });
        dialog.close();
        toast("草稿已拒绝");
        refreshDrawer("collab");
      } catch (err) {
        toast(err.message, true);
      }
    };
  }

  async function decidePlan(draftId, action) {
    if (!active) return;
    const dialog = $("#modal");
    if (action === "reject") {
      $("#modal-title").textContent = "拒绝计划草案";
      $("#modal-fields").innerHTML = `<label class="field">拒绝原因<textarea name="reason" required placeholder="告诉 Agent 计划需要如何调整"></textarea></label>`;
    } else {
      $("#modal-title").textContent = "确认项目计划";
      $("#modal-fields").innerHTML = `<p class="muted">确认后，计划与团队需求建议才会投影为正式的业务对象。</p>`;
    }
    dialog.showModal();
    $("#modal-form").onsubmit = async (e) => {
      e.preventDefault();
      const v = Object.fromEntries(new FormData(e.target));
      try {
        await W.api(`/v1/projects/${encodeURIComponent(active.projectId)}/plan-drafts/${encodeURIComponent(draftId)}:${action}`, {
          method: "POST",
          body: JSON.stringify(action === "reject" ? { reason: v.reason || "" } : {}),
        });
        dialog.close();
        toast(action === "approve" ? "计划已确认并投影" : "计划草案已拒绝");
        refreshDrawer("overview");
      } catch (err) {
        toast(err.message, true);
      }
    };
  }

  window.CoifespWorkspace = { init, openProject, refresh, showProjects };
})();
