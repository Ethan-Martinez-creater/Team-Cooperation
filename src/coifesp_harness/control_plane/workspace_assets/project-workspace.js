/* COIFESP Agent-first project workspace.
   The project page is a continuous ChatGPT-style conversation. Tasks, data,
   exchanges and plans are context drawers on the same page, never separate
   top-level pages. Internal objects (run IDs, tokens, events) stay hidden.
*/
(function () {
  "use strict";

  let W = null;
  let active = null; // { projectId, conversationId, eventController, lastSequence }
  let pendingAttachments = [];
  let openGeneration = 0;
  let bound = false;

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
      const snapshot = await W.api(`/v1/projects/${encodeURIComponent(projectId)}/workspace`);
      const conversation = await W.api(`/v1/projects/${encodeURIComponent(projectId)}/conversation`, { method: "PUT" });
      if (generation !== openGeneration) return;
      active = {
        projectId,
        conversationId: conversation.conversation_id,
        eventController: null,
        snapshot,
      };
      renderProjectHead(snapshot);
      const page = await W.api(
        `/v1/projects/${encodeURIComponent(projectId)}/conversation/messages?after_sequence=0`
      );
      if (generation !== openGeneration) return;
      active.lastSequence = conversation.last_message_sequence;
      renderConversation(page.items);
      startEventStream(projectId);
      await refreshDrawer("overview");
      await refreshDrawer("data");
      await refreshDrawer("collab");
      await refreshDrawer("plan");
      await refreshDrawer("inbox");
      if (typeof show === "function") show("project-workspace");
      if (typeof state !== "undefined" && state.sessionCoordinator) {
        state.sessionCoordinator.saveRoute({ view: "project-workspace", project_id: projectId });
      }
      $("#ws-input").focus();
    } catch (e) {
      if (generation === openGeneration) toast(e.message, true);
    }
  }

  function renderProjectHead(snapshot) {
    $("#ws-project-id").textContent = snapshot.project.project_id;
    $("#ws-project-name").textContent = snapshot.project.name;
    $("#ws-project-desc").textContent = snapshot.project.description || "";
    const canAddTeam = W.teamId && snapshot.project.owner_team_id === W.teamId;
    $("#ws-project-teams").innerHTML = [
      ...(snapshot.teams || []).map((t) => `<span class="pill">${esc(t.name)}</span>`),
      canAddTeam ? `<button type="button" class="secondary ws-add-team">添加参与团队</button>` : "",
    ].join("");
    const addTeam = $("#ws-project-teams .ws-add-team");
    if (addTeam) addTeam.addEventListener("click", () => addProjectTeam(snapshot));
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
    if (active.lastSequence != null) body.expected_last_sequence = active.lastSequence;
    try {
      const result = await W.api(
        `/v1/projects/${encodeURIComponent(projectId)}/conversation/messages`,
        { method: "POST", body: JSON.stringify(body) }
      );
      // Ignore the response if the user switched projects while awaiting.
      if (!active || active.projectId !== projectId) return;
      renderConversation([result.message]);
      active.lastSequence = result.message.sequence;
      active.pendingTurn = result.turn?.turn_id;
      if (result.run) {
        toast("Agent 已开始执行");
      } else {
        renderTurnPending();
      }
    } catch (e) {
      if (!active || active.projectId !== projectId) return;
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

  function startEventStream(projectId) {
    if (active?.eventController) active.eventController.abort();
    const controller = new AbortController();
    active.eventController = controller;
    const path = `/v1/projects/${encodeURIComponent(projectId)}/conversation/events`;
    let attempt = 0;

    const openStream = () => {
      const cursor = active && active.lastSequence ? active.lastSequence : 0;
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
        if (!response.ok || !active || active.projectId !== projectId) return;
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
                if (active && active.projectId === projectId) appendStreamedMessage(message);
              } catch (e) {}
            }
            if (eventId != null && active && active.projectId === projectId && eventId > (active.lastSequence || 0)) {
              active.lastSequence = Math.max(active.lastSequence || 0, eventId);
            }
          }
          if (chunk.done) break;
        }
        scheduleReconnect();
      } catch (e) {
        if (!controller.signal.aborted && active && active.projectId === projectId) scheduleReconnect();
      }
    };

    const scheduleReconnect = () => {
      if (controller.signal.aborted || !active) return;
      const delay = Math.min(30000, 1000 * Math.pow(2, attempt));
      attempt += 1;
      setTimeout(() => {
        if (controller.signal.aborted || !active) return;
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
    refreshDrawer("plan").catch(() => {});
  }

  function switchDrawerTab(tab) {
    document.querySelectorAll("[data-ws-tab]").forEach((b) => b.classList.toggle("active", b.dataset.wsTab === tab));
    document.querySelectorAll("[data-ws-pane]").forEach((p) => p.classList.toggle("active", p.dataset.wsPane === tab));
    refreshDrawer(tab);
  }

  async function refreshDrawer(tab) {
    if (!active) return;
    const pane = document.querySelector(`[data-ws-pane="${tab}"]`);
    if (!pane || !pane.classList.contains("active")) return;
    const project = active.projectId;
    let html;
    try {
      if (tab === "overview") html = await overviewPane(project);
      else if (tab === "data") html = await dataPane(project);
      else if (tab === "collab") html = await collabPane(project);
      else if (tab === "plan") html = await planPane(project);
      else if (tab === "inbox") html = await inboxPane(project);
      // A slow response from a previously opened project must never overwrite
      // the drawer of the project the user is looking at now.
      if (!active || active.projectId !== project) return;
      pane.innerHTML = html;
      bindPaneActions(pane, tab);
    } catch (e) {
      if (!active || active.projectId !== project) return;
      pane.innerHTML = `<p class="muted small">加载失败：${esc(e.message)}</p>`;
    }
  }

  async function overviewPane(projectId) {
    const snapshot = await W.api(`/v1/projects/${encodeURIComponent(projectId)}/workspace`);
    const planDrafts = await W.api(`/v1/projects/${encodeURIComponent(projectId)}/plan-drafts`);
    const plan = planDrafts.find((d) => d.status === "approved") || planDrafts[0];
    return `
      <h4>目标与计划</h4>
      ${plan ? `<div class="card"><strong>${esc(plan.goals)}</strong>
        <p class="muted">${esc(plan.scope)}</p>
        <div class="meta"><span class="pill green">${stateName(plan.status)}</span></div></div>` : `<p class="muted small">Agent 尚未生成计划草案；在对话中描述项目目标即可。</p>`}
      <h4>统计</h4>
      <div class="meta">任务 ${snapshot.task_count} · 资料 ${snapshot.resource_count} · 待确认草案 ${snapshot.pending_draft_count}</div>
      <h4>参与团队</h4>
      ${(snapshot.teams || []).map((t) => `<div class="meta"><span>${esc(t.name)}</span><span class="pill">${esc(t.kind)}</span></div>`).join("") || `<p class="muted small">尚无参与团队</p>`}`;
  }

  async function dataPane(projectId) {
    const resources = await W.api(`/v1/projects/${encodeURIComponent(projectId)}/resources`);
    const ownTeam = state?.account?.team_id || state?.identity?.tenant_id;
    if (!resources || !resources.length) {
      return `<div class="actions"><button class="primary" data-ws-upload>上传资料</button></div><p class="muted small">还没有项目资料。上传的文件默认仅本团队可见，可稍后切换为项目共享。</p>`;
    }
    return `<div class="actions"><button class="primary" data-ws-upload>上传资料</button></div>` +
      resources.map((r) => `
        <article class="card">
          <div class="card-head"><div><strong>${esc(r.title)}</strong><div class="meta"><span class="pill ${r.propagation === "team_private" ? "orange" : "green"}">${propagationLabel[r.propagation] || esc(r.propagation)}</span><span>${esc(r.owner_team_id)}</span></div></div></div>
          ${r.owner_team_id === ownTeam && r.propagation === "team_private"
            ? `<button class="secondary" data-ws-share-resource="${esc(r.resource_id)}">共享到项目</button>`
            : ""}
        </article>`).join("");
  }

  async function collabPane(projectId) {
    let drafts = [];
    let exchanges = [];
    try { drafts = await W.api(`/v1/projects/${encodeURIComponent(projectId)}/agent-exchange-drafts`); } catch (e) {}
    try { exchanges = await W.api(`/v1/projects/${encodeURIComponent(projectId)}/agent-exchanges`); } catch (e) {}
    const ownTeam = state?.account?.team_id || state?.identity?.tenant_id;
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
      let actions = "";
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
      exchangeRows.push(`<article class="card">
        <div class="card-head"><div><strong>${esc(x.purpose)}</strong><div class="meta"><span class="pill ${inbound ? "orange" : "green"}">${inbound ? "收到的请求" : "已发出"}</span><span>${esc(x.source_team_id)}</span><span>${stateName(x.status)}</span></div></div></div>
        <p class="muted">${esc(x.summary)}</p>
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
  async function planPane(projectId) {
    let drafts = [];
    try { drafts = await W.api(`/v1/projects/${encodeURIComponent(projectId)}/plan-drafts`); } catch (e) {}
    const cards = (drafts || []).map((d) => `
      <article class="card">
        <div class="card-head"><div><strong>计划草案</strong><div class="meta"><span class="pill ${d.status === "approved" ? "green" : "orange"}">${stateName(d.status)}</span><span>v${d.version}</span></div></div></div>
        <p>${esc(d.goals)}</p>
        <p class="muted">阶段 ${(d.phases || []).length} · 风险 ${(d.risks || []).length} · 验收 ${(d.acceptance_criteria || []).length}</p>
        ${d.status === "drafting" ? `<div class="task-actions"><button class="secondary" data-ws-reject-plan="${esc(d.draft_id)}">拒绝</button><button class="primary" data-ws-approve-plan="${esc(d.draft_id)}">确认计划</button></div>` : ""}
      </article>`).join("");
    return `<p class="muted small">Agent 会在项目创建或你描述目标后给出计划、团队类别与人数建议；确认后才生成正式业务对象。</p>` + (cards || `<p class="muted small">暂无计划草案。</p>`);
  }

  async function inboxPane(projectId) {
    let tasks = [];
    try { tasks = await W.api(`/v1/projects/${encodeURIComponent(projectId)}/tasks`); } catch (e) {}
    if (!tasks.length) return `<p class="muted small">没有团队任务。由 Agent 起草或人工布置的任务会出现在这里。</p>`;
    return tasks.map((t) => `<article class="card"><div class="card-head"><div><strong>${esc(t.title)}</strong><div class="meta"><span class="pill ${t.status === "verified" ? "green" : "orange"}">${stateName(t.status)}</span><span>${esc(t.source_team_id)} → ${esc(t.target_team_id)}</span></div></div></div><p class="muted">${esc(t.acceptance_criteria || "")}</p></article>`).join("");
  }

  function bindPaneActions(pane, tab) {
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
        refreshDrawer("data");
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
        refreshDrawer("data");
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
    const ownTeam = state?.account?.team_id || state?.identity?.tenant_id;
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
      const ownTeam = state?.account?.team_id || state?.identity?.tenant_id;
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
        refreshDrawer("plan");
        refreshDrawer("overview");
      } catch (err) {
        toast(err.message, true);
      }
    };
  }

  window.CoifespWorkspace = { init, openProject, refresh, showProjects };
})();
