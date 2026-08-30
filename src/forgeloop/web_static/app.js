(() => {
  "use strict";

  const tokenNode = document.querySelector('meta[name="forgeloop-token"]');
  const token = tokenNode ? tokenNode.content : "";
  const MAX_TIMELINE_ITEMS = 600;
  const MAX_CONVERSATION_ITEMS = 100;
  const EVENT_BATCH_SIZE = 100;

  const ui = {
    workspace: document.querySelector("#workspace-label"),
    workspaceIdentity: document.querySelector("#workspace-identity"),
    workspaceSwitcher: document.querySelector("#workspace-switcher"),
    workspaceRowMenu: document.querySelector("#workspace-row-menu"),
    rowMenuRename: document.querySelector("#row-menu-rename"),
    rowMenuFork: document.querySelector("#row-menu-fork"),
    rowMenuArchive: document.querySelector("#row-menu-archive"),
    rowMenuUnarchive: document.querySelector("#row-menu-unarchive"),
    rowMenuDelete: document.querySelector("#row-menu-delete"),
    renameModal: document.querySelector("#rename-modal"),
    renameMask: document.querySelector("#rename-mask"),
    renameClose: document.querySelector("#rename-close"),
    renameTitle: document.querySelector("#rename-title"),
    renameInput: document.querySelector("#rename-input"),
    renameCancel: document.querySelector("#rename-cancel"),
    renameConfirm: document.querySelector("#rename-confirm"),
    deleteModal: document.querySelector("#delete-modal"),
    deleteMask: document.querySelector("#delete-mask"),
    deleteClose: document.querySelector("#delete-close"),
    deleteTitle: document.querySelector("#delete-title"),
    deleteDesc: document.querySelector("#delete-desc"),
    deleteCancel: document.querySelector("#delete-cancel"),
    deleteConfirm: document.querySelector("#delete-confirm"),
    sidebar: document.querySelector("#sidebar"),
    sidebarToggle: document.querySelector("#sidebar-toggle"),
    sidebarBackdrop: document.querySelector("#sidebar-backdrop"),
    sidebarNewSession: document.querySelector("#sidebar-new-session"),
    sidebarTree: document.querySelector("#sidebar-tree"),
    sidebarSearchInput: document.querySelector("#sidebar-search-input"),
    sidebarSearchClear: document.querySelector("#sidebar-search-clear"),
    sidebarEmpty: document.querySelector("#sidebar-empty"),
    sidebarArchivedToggle: document.querySelector("#sidebar-archived-toggle"),
    sidebarHoverCard: document.querySelector("#sidebar-hover-card"),
    sidebarHoverTitle: document.querySelector("#sidebar-hover-title"),
    sidebarHoverPath: document.querySelector("#sidebar-hover-path"),
    sidebarHoverTime: document.querySelector("#sidebar-hover-time"),
    sidebarHoverCopy: document.querySelector("#sidebar-hover-copy"),
    pet: document.querySelector("#pet"),
    petBubble: document.querySelector("#pet-bubble"),
    model: document.querySelector("#model-label"),
    turn: document.querySelector("#turn-label"),
    agentCount: document.querySelector("#agent-count-label"),
    elapsed: document.querySelector("#elapsed-label"),
    executionMode: document.querySelector("#execution-mode"),
    executionModeSymbol: document.querySelector("#execution-mode-symbol"),
    executionModeLabel: document.querySelector("#execution-mode-label"),
    connection: document.querySelector("#connection-state"),
    connectionLabel: document.querySelector("#connection-label"),
    welcome: document.querySelector("#welcome-state"),
    messages: document.querySelector("#message-list"),
    conversationScroll: document.querySelector("#conversation-scroll"),
    thinking: document.querySelector("#thinking-indicator"),
    thinkingLabel: document.querySelector("#thinking-label"),
    form: document.querySelector("#composer-form"),
    input: document.querySelector("#composer-input"),
    send: document.querySelector("#send-button"),
    sendLabel: document.querySelector("#send-label"),
    runState: document.querySelector("#run-state"),
    runStateLabel: document.querySelector("#run-state-label"),
    stepCount: document.querySelector("#step-count"),
    emptyTrace: document.querySelector("#empty-trace"),
    timeline: document.querySelector("#timeline"),
    verification: document.querySelector("#verification-card"),
    verificationIcon: document.querySelector(".verification-icon"),
    verificationTitle: document.querySelector("#verification-title"),
    verificationDetail: document.querySelector("#verification-detail"),
    activityToggle: document.querySelector("#activity-toggle"),
    activityPanel: document.querySelector("#activity-panel"),
    activityContent: document.querySelector(".activity-content"),
    drawerClose: document.querySelector("#drawer-close"),
    drawerBackdrop: document.querySelector("#drawer-backdrop"),
    topbar: document.querySelector(".topbar"),
    conversationPane: document.querySelector(".conversation-pane"),
    toasts: document.querySelector("#toast-region"),
  };

  const runtime = {
    busy: false,
    poisoned: false,
    activeRunId: null,
    cursor: 0,
    composing: false,
    turn: 0,
    maxStep: 0,
    startedAt: 0,
    elapsedTimer: null,
    runTerminal: false,
    toolItems: new Map(),
    planningItem: null,
    trimmedTraceItems: 0,
    trimNotice: null,
    conversationScrollFrame: null,
    timelineScrollFrame: null,
    followTimeline: true,
    workspaceSwitching: false,
    workspaces: [],
    activeWorkspaceId: "",
    expandedWorkspaces: new Set(),
    archivedOpen: false,
    lastSnapshot: null,
  };

  let rowMenuTarget = null;

  function node(tag, className, text) {
    const element = document.createElement(tag);
    if (className) {
      element.className = className;
    }
    if (text !== undefined && text !== null) {
      element.textContent = String(text);
    }
    return element;
  }

  function apiHeaders(json = false) {
    const headers = { "X-ForgeLoop-Token": token };
    if (json) {
      headers["Content-Type"] = "application/json";
    }
    return headers;
  }

  async function readJson(response) {
    let payload = {};
    try {
      payload = await response.json();
    } catch (_error) {
      payload = {};
    }
    if (!response.ok) {
      const error = new Error(payload.error || `请求失败（${response.status}）`);
      error.status = response.status;
      throw error;
    }
    return payload;
  }

  async function fetchStatus() {
    const response = await fetch("/api/status", {
      method: "GET",
      headers: apiHeaders(),
      cache: "no-store",
      credentials: "same-origin",
    });
    return readJson(response);
  }

  function setConnection(kind, label) {
    ui.connection.classList.remove("connected", "error");
    if (kind) {
      ui.connection.classList.add(kind);
    }
    ui.connectionLabel.textContent = label;
  }

  function setRunState(kind, label) {
    ui.runState.classList.remove("running", "success", "warning", "error");
    if (kind) {
      ui.runState.classList.add(kind);
    }
    ui.runStateLabel.textContent = label;
  }

  function classifyOutcome(status, verificationPending = false) {
    if (status === "completed" && !verificationPending) {
      return { success: true, kind: "success", label: "执行完成" };
    }
    if (status === "completed_with_verification_risk" || verificationPending) {
      return { success: false, kind: "warning", label: "需补充验证" };
    }
    if (status === "step_limit") {
      return { success: false, kind: "warning", label: "已达到自定义步骤上限" };
    }
    if (["tool_call_limit", "runtime_limit", "repetition_limit"].includes(status)) {
      return { success: false, kind: "warning", label: "已暂停，可继续" };
    }
    return { success: false, kind: "error", label: "需要关注" };
  }

  function setBusy(value) {
    runtime.busy = value;
    ui.input.disabled = runtime.poisoned;
    ui.workspaceSwitcher.disabled = value;
    ui.sidebarNewSession.disabled = value;
    for (const row of document.querySelectorAll(".sidebar-row")) {
      row.disabled = value;
    }
    ui.input.placeholder = runtime.poisoned
      ? "会话已关闭，请在终端重启 ForgeLoop Web"
      : value
        ? "当前任务执行中；可以先写好下一条指令…"
        : "描述你希望 ForgeLoop 完成的编码任务…";
    ui.sendLabel.textContent = value ? "执行中" : "运行任务";
    ui.thinking.hidden = !value;
    updateSendState();
    if (value) {
      ui.welcome.hidden = true;
      setRunState("running", "正在执行");
      scrollConversation();
    }
  }

  function updateSendState() {
    const hasTask = ui.input.value.trim().length > 0;
    ui.send.disabled = runtime.busy || runtime.poisoned || !hasTask;
  }

  function resizeComposer() {
    ui.input.rows = 1;
    const lineHeight = Number.parseFloat(getComputedStyle(ui.input).lineHeight) || 21;
    const verticalPadding = 24;
    const rows = Math.ceil(
      Math.max(0, ui.input.scrollHeight - verticalPadding) / lineHeight
    );
    ui.input.rows = Math.max(1, Math.min(9, rows));
  }

  function scrollConversation() {
    if (runtime.conversationScrollFrame !== null) {
      return;
    }
    runtime.conversationScrollFrame = requestAnimationFrame(() => {
      runtime.conversationScrollFrame = null;
      ui.conversationScroll.scrollTop = ui.conversationScroll.scrollHeight;
    });
  }

  function scheduleTimelineScroll() {
    if (!runtime.followTimeline || runtime.timelineScrollFrame !== null) {
      return;
    }
    runtime.timelineScrollFrame = requestAnimationFrame(() => {
      runtime.timelineScrollFrame = null;
      if (runtime.followTimeline) {
        ui.activityContent.scrollTop = ui.activityContent.scrollHeight;
      }
    });
  }

  function showToast(message, kind = "info") {
    const toast = node("div", `toast ${kind}`, message);
    ui.toasts.append(toast);
    window.setTimeout(() => toast.remove(), 5200);
  }

  function formatTurn(value) {
    return String(Math.max(0, Number(value) || 0)).padStart(2, "0");
  }

  function formatDuration(milliseconds) {
    const totalSeconds = Math.max(0, Math.round(milliseconds / 1000));
    if (totalSeconds < 60) {
      return `${totalSeconds}s`;
    }
    const minutes = Math.floor(totalSeconds / 60);
    const seconds = totalSeconds % 60;
    return `${minutes}m ${String(seconds).padStart(2, "0")}s`;
  }

  function startElapsedClock(initialMilliseconds = 0) {
    window.clearInterval(runtime.elapsedTimer);
    const parsedMilliseconds = Number(initialMilliseconds);
    const elapsedMilliseconds = Number.isFinite(parsedMilliseconds)
      ? Math.max(0, parsedMilliseconds)
      : 0;
    runtime.startedAt = Date.now() - elapsedMilliseconds;
    ui.elapsed.textContent = formatDuration(elapsedMilliseconds);
    runtime.elapsedTimer = window.setInterval(() => {
      ui.elapsed.textContent = formatDuration(Date.now() - runtime.startedAt);
    }, 1000);
  }

  function stopElapsedClock(milliseconds) {
    window.clearInterval(runtime.elapsedTimer);
    runtime.elapsedTimer = null;
    if (Number.isFinite(milliseconds)) {
      ui.elapsed.textContent = formatDuration(milliseconds);
    }
  }

  function appendInline(parent, text) {
    const pattern = /(\*\*[^*]+\*\*|`[^`]+`)/g;
    let cursor = 0;
    for (const match of text.matchAll(pattern)) {
      if (match.index > cursor) {
        parent.append(document.createTextNode(text.slice(cursor, match.index)));
      }
      const value = match[0];
      if (value.startsWith("**")) {
        parent.append(node("strong", "", value.slice(2, -2)));
      } else {
        parent.append(node("code", "", value.slice(1, -1)));
      }
      cursor = match.index + value.length;
    }
    if (cursor < text.length) {
      parent.append(document.createTextNode(text.slice(cursor)));
    }
  }

  function renderRichText(container, source) {
    const lines = String(source || "").replace(/\r\n/g, "\n").split("\n");
    let codeBlock = null;
    let list = null;
    let listKind = "";

    function closeList() {
      list = null;
      listKind = "";
    }

    for (const line of lines) {
      if (line.trim().startsWith("```")) {
        closeList();
        if (codeBlock) {
          codeBlock = null;
        } else {
          const pre = node("pre", "message-code");
          codeBlock = node("code");
          pre.append(codeBlock);
          container.append(pre);
        }
        continue;
      }
      if (codeBlock) {
        codeBlock.append(document.createTextNode(`${line}\n`));
        continue;
      }
      if (!line.trim()) {
        closeList();
        continue;
      }

      const heading = line.match(/^#{1,3}\s+(.+)$/);
      if (heading) {
        closeList();
        const headingNode = node("h3", "message-heading");
        appendInline(headingNode, heading[1]);
        container.append(headingNode);
        continue;
      }

      const unordered = line.match(/^\s*[-*]\s+(.+)$/);
      const ordered = line.match(/^\s*\d+[.)]\s+(.+)$/);
      if (unordered || ordered) {
        const kind = unordered ? "ul" : "ol";
        if (!list || listKind !== kind) {
          list = node(kind, "message-list-block");
          listKind = kind;
          container.append(list);
        }
        const item = node("li");
        appendInline(item, (unordered || ordered)[1]);
        list.append(item);
        continue;
      }

      closeList();
      const paragraph = node("p");
      appendInline(paragraph, line);
      container.append(paragraph);
    }
  }

  function appendMessage(role, content, status = "", animate = true) {
    ui.welcome.hidden = true;
    const item = node("article", `message ${role}`);
    if (!animate) {
      item.classList.add("no-animation");
    }
    const avatar = node("div", "message-avatar", role === "user" ? "YOU" : "FL");
    avatar.setAttribute("aria-hidden", "true");
    const body = node("div", "message-body");
    const meta = node("div", "message-meta");
    meta.append(node("strong", "", role === "user" ? "你" : "ForgeLoop"));
    meta.append(node("span", "", role === "user" ? "任务" : "执行报告"));
    if (role === "assistant" && status) {
      const statusLabel = status === "completed" ? "COMPLETED" : status.toUpperCase();
      const statusKind = status === "completed"
        ? "success"
        : status === "error"
          ? "error"
          : "warning";
      const statusNode = node("span", `message-status ${statusKind}`, statusLabel);
      meta.append(statusNode);
    }
    const contentNode = node("div", "message-content");
    if (role === "assistant") {
      renderRichText(contentNode, content);
    } else {
      contentNode.textContent = content;
    }
    body.append(meta, contentNode);
    item.append(avatar, body);
    ui.messages.append(item);
    while (ui.messages.children.length > MAX_CONVERSATION_ITEMS) {
      ui.messages.firstElementChild.remove();
    }
    scrollConversation();
    return item;
  }

  function renderConversation(conversation) {
    ui.messages.replaceChildren();
    const items = Array.isArray(conversation) ? conversation : [];
    ui.welcome.hidden = items.length > 0;
    for (const item of items) {
      const role = item.role === "user" ? "user" : "assistant";
      appendMessage(role, String(item.content || ""), String(item.status || ""), false);
    }
  }

  function resetTrace() {
    ui.timeline.replaceChildren();
    ui.emptyTrace.hidden = false;
    runtime.toolItems.clear();
    runtime.maxStep = 0;
    runtime.cursor = 0;
    runtime.runTerminal = false;
    runtime.planningItem = null;
    runtime.trimmedTraceItems = 0;
    runtime.trimNotice = null;
    runtime.followTimeline = true;
    ui.stepCount.textContent = "0 步";
    ui.verification.classList.remove("success", "pending", "error");
    ui.verificationIcon.textContent = "·";
    ui.verificationTitle.textContent = "等待验证结果";
    ui.verificationDetail.textContent = "ForgeLoop 会在修改后主动运行相关检查。";
  }

  function trimTimeline() {
    let removed = 0;
    const visibleLimit = MAX_TIMELINE_ITEMS + (runtime.trimNotice ? 1 : 0);
    while (ui.timeline.children.length > visibleLimit) {
      let oldest = ui.timeline.firstElementChild;
      if (oldest === runtime.trimNotice) {
        oldest = oldest.nextElementSibling;
      }
      if (!oldest) {
        break;
      }
      const toolKey = oldest.getAttribute("data-tool-key") || "";
      const stored = toolKey ? runtime.toolItems.get(toolKey) : null;
      if (stored && stored.item === oldest) {
        runtime.toolItems.delete(toolKey);
      }
      if (runtime.planningItem === oldest) {
        runtime.planningItem = null;
      }
      oldest.remove();
      removed += 1;
    }
    if (!removed) {
      return;
    }
    runtime.trimmedTraceItems += removed;
    if (!runtime.trimNotice) {
      const notice = node("li", "timeline-item warning");
      const iconNode = node("span", "timeline-icon", "…");
      iconNode.setAttribute("aria-hidden", "true");
      const copy = node("div", "timeline-copy");
      copy.append(node("div", "timeline-title", "较早轨迹已省略"));
      copy.append(node("div", "timeline-detail", ""));
      notice.append(iconNode, copy, node("span", "timeline-step", ""));
      notice.setAttribute("aria-hidden", "true");
      ui.timeline.prepend(notice);
      runtime.trimNotice = notice;
    }
    const detail = runtime.trimNotice.querySelector(".timeline-detail");
    if (detail) {
      detail.textContent = `为保持长任务流畅，已省略 ${runtime.trimmedTraceItems} 条较早记录`;
    }
  }

  function addTimelineItem({ icon, title, detail = "", step = 0, status = "running", key = "" }) {
    ui.emptyTrace.hidden = true;
    const item = node("li", `timeline-item ${status}`);
    const iconNode = node("span", "timeline-icon", icon);
    iconNode.setAttribute("aria-hidden", "true");
    const copy = node("div", "timeline-copy");
    copy.append(node("div", "timeline-title", title));
    const detailNode = node("div", "timeline-detail", detail);
    if (detail) {
      copy.append(detailNode);
    }
    const stepNode = node("span", "timeline-step", step ? `STEP ${step}` : "");
    item.append(iconNode, copy, stepNode);
    ui.timeline.append(item);
    if (key) {
      item.setAttribute("data-tool-key", key);
      runtime.toolItems.set(key, { item, iconNode, copy, detailNode, stepNode });
    }
    trimTimeline();
    if (step > runtime.maxStep) {
      runtime.maxStep = step;
      ui.stepCount.textContent = `${runtime.maxStep} 步`;
    }
    scheduleTimelineScroll();
    return item;
  }

  function updateToolItem(event) {
    const key = String(event.call_id || "");
    const stored = runtime.toolItems.get(key);
    const status = event.ok ? "success" : "error";
    const icon = event.ok ? "✓" : "!";
    if (!stored) {
      addTimelineItem({
        icon,
        title: `${event.tool || "工具"}${event.ok ? " 已完成" : " 执行失败"}`,
        detail: event.detail || "",
        step: Number(event.step) || 0,
        status,
      });
      return;
    }
    stored.item.classList.remove("running", "success", "error", "warning");
    stored.item.classList.add(status);
    stored.iconNode.textContent = icon;
    const title = stored.copy.querySelector(".timeline-title");
    title.textContent = `${event.tool || "工具"}${event.ok ? " 已完成" : " 执行失败"}`;
    stored.detailNode.textContent = event.detail || stored.detailNode.textContent;
    if (stored.detailNode.textContent && !stored.detailNode.isConnected) {
      stored.copy.append(stored.detailNode);
    }
  }

  function completePlanning(success = true, titleOverride = "") {
    const item = runtime.planningItem;
    if (!item) {
      return;
    }
    item.classList.remove("running", "success", "error", "warning");
    item.classList.add(success ? "success" : "warning");
    const icon = item.querySelector(".timeline-icon");
    const title = item.querySelector(".timeline-title");
    if (icon) {
      icon.textContent = success ? "✓" : "!";
    }
    if (title) {
      if (titleOverride) {
        title.textContent = titleOverride;
      } else if (title.textContent.includes("正在整理最终报告")) {
        title.textContent = success ? "最终报告已整理" : "最终报告未生成";
      } else if (title.textContent.includes("正在规划")) {
        title.textContent = title.textContent.replace(
          "正在规划",
          success ? "已完成" : "规划未完成",
        );
      }
    }
    runtime.planningItem = null;
  }

  function settlePendingTrace(status) {
    completePlanning(status === "completed");
    for (const stored of runtime.toolItems.values()) {
      if (!stored.item.classList.contains("running")) {
        continue;
      }
      stored.item.classList.remove("running");
      stored.item.classList.add("warning");
      stored.iconNode.textContent = "↻";
      const title = stored.copy.querySelector(".timeline-title");
      if (title) {
        title.textContent = `${title.textContent}（状态已同步）`;
      }
    }
    runtime.runTerminal = true;
  }

  function enterUncertainState(message) {
    if (runtime.elapsedTimer) {
      stopElapsedClock(Date.now() - runtime.startedAt);
    }
    setBusy(true);
    setConnection("error", "状态未知");
    setRunState("error", "等待状态同步");
    ui.thinkingLabel.textContent = message;
  }

  function updateVerification(event) {
    const verifications = Array.isArray(event.verifications) ? event.verifications : [];
    const latestVerification = verifications.length
      ? verifications[verifications.length - 1]
      : "";
    const status = String(event.status || "unknown");
    ui.verification.classList.remove("success", "pending", "error");
    if (status === "error") {
      ui.verification.classList.add("error");
      ui.verificationIcon.textContent = "×";
      ui.verificationTitle.textContent = "任务已中断";
      ui.verificationDetail.textContent = latestVerification || "需要重启后检查工作区状态。";
      return;
    }
    if (status !== "completed") {
      ui.verification.classList.add("pending");
      ui.verificationIcon.textContent = "!";
      ui.verificationTitle.textContent = verifications.length && !event.verification_pending
        ? "检查已通过，任务未确认完成"
        : "任务未完全完成";
      ui.verificationDetail.textContent = verifications.length
        ? `已记录 ${verifications.length} 项检查；最新结果：${latestVerification}`
        : `结束状态：${status}`;
      return;
    }
    if (event.verification_pending) {
      ui.verification.classList.add("pending");
      ui.verificationIcon.textContent = "!";
      ui.verificationTitle.textContent = "仍需补充验证";
      ui.verificationDetail.textContent = latestVerification || "本轮修改尚未完成充分验证。";
      return;
    }
    ui.verification.classList.add("success");
    ui.verificationIcon.textContent = "✓";
    ui.verificationTitle.textContent = verifications.length ? "验证已完成" : "任务已闭环";
    ui.verificationDetail.textContent = verifications.slice(-2).join(" · ") || "ForgeLoop 未报告待处理的验证项。";
  }

  function handleEvent(event) {
    if (Number.isInteger(event.id)) {
      runtime.cursor = Math.max(runtime.cursor, event.id + 1);
    }
    const step = Number(event.step) || 0;
    if (event.type === "run_started") {
      addTimelineItem({ icon: "↗", title: "任务已进入执行队列", detail: `第 ${event.turn || runtime.turn} 轮会话`, status: "success" });
      ui.thinkingLabel.textContent = "正在理解任务…";
      return;
    }
    if (event.type === "delegation_started") {
      const count = Math.max(0, Number(event.count) || 0);
      addTimelineItem({
        icon: "◇",
        title: count ? `已派发 ${count} 个只读调查任务` : "正在准备并行调查",
        detail: "子 Agent 仅可读取文件；主 Agent 仍是唯一写入者",
        status: "success",
      });
      ui.thinkingLabel.textContent = "只读子 Agent 正在并行调查…";
      return;
    }
    if (event.type === "subtask_started") {
      const label = String(event.label || event.subtask_id || "子 Agent");
      addTimelineItem({
        icon: "↗",
        title: `${label} 开始只读调查`,
        detail: event.objective || "",
        status: "success",
      });
      return;
    }
    if (event.type === "subtask_completed") {
      const label = String(event.label || event.subtask_id || "子 Agent");
      const completed = event.status === "completed";
      addTimelineItem({
        icon: completed ? "✓" : "!",
        title: completed ? `${label} 已返回调查结果` : `${label} 调查未完成`,
        detail: event.summary || `状态：${event.status || "unknown"}`,
        status: completed ? "success" : "warning",
      });
      return;
    }
    if (event.type === "delegation_completed") {
      const completed = Math.max(0, Number(event.completed) || 0);
      const failed = Math.max(0, Number(event.failed) || 0);
      const workspaceStable = event.workspace_stable === true;
      addTimelineItem({
        icon: failed || !workspaceStable ? "!" : "✓",
        title: !workspaceStable
          ? "调查期间工作区发生变化，关键证据需重读"
          : failed
            ? "并行调查已汇总（部分未完成）"
            : "并行调查已汇总",
        detail: `${completed} 项完成 · ${failed} 项未完成 · ${formatDuration(event.duration_ms || 0)}`,
        status: failed || !workspaceStable ? "warning" : "success",
      });
      ui.thinkingLabel.textContent = "主 Agent 正在整合调查结果…";
      return;
    }
    if (event.type === "model_request") {
      completePlanning(true);
      runtime.planningItem = addTimelineItem({
        icon: "D",
        title: `DeepSeek 正在规划第 ${step} 步`,
        detail: `${event.message_count || 0} 条上下文消息`,
        step,
        status: "running",
      });
      ui.thinkingLabel.textContent = `正在规划第 ${step} 步…`;
      return;
    }
    if (event.type === "finalization_request") {
      completePlanning(true);
      runtime.planningItem = addTimelineItem({
        icon: "≡",
        title: "正在整理最终报告",
        detail: "工具阶段已结束；只根据现有执行证据收尾",
        status: "running",
      });
      ui.thinkingLabel.textContent = "正在整理最终报告…";
      return;
    }
    if (event.type === "tool_start") {
      completePlanning(true);
      addTimelineItem({
        icon: "›",
        title: `调用 ${event.tool || "工具"}`,
        detail: event.arguments || "",
        step,
        status: "running",
        key: String(event.call_id || `tool-${Date.now()}`),
      });
      ui.thinkingLabel.textContent = `正在执行 ${event.tool || "工具"}…`;
      return;
    }
    if (event.type === "tool_end") {
      updateToolItem(event);
      ui.thinkingLabel.textContent = event.ok ? "正在检查结果…" : "正在处理工具错误…";
      return;
    }
    if (event.type === "warning") {
      completePlanning(false);
      addTimelineItem({ icon: "!", title: "运行提示", detail: event.message || "", step, status: "warning" });
      return;
    }
    if (event.type === "workspace_changed") {
      ui.workspace.textContent = String(event.path || ui.workspace.textContent);
      ui.workspace.title = ui.workspace.textContent;
      addTimelineItem({
        icon: "⌁",
        title: "已切换目标工作区",
        detail: event.path || "",
        step,
        status: "success",
      });
      return;
    }
    if (event.type === "final") {
      const status = String(event.status || "unknown");
      const outcome = classifyOutcome(status);
      completePlanning(
        outcome.success,
        outcome.success ? "最终报告已整理" : "最终报告已整理（任务未完成）",
      );
      addTimelineItem({
        icon: outcome.success ? "≡" : "!",
        title: outcome.success ? "已生成执行报告" : "已生成未完成报告",
        detail: `状态：${status}`,
        status: outcome.kind === "error" ? "error" : outcome.kind,
      });
      ui.thinkingLabel.textContent = "正在提交会话结果…";
      return;
    }
    if (event.type === "gap") {
      addTimelineItem({ icon: "…", title: "较早记录已压缩", detail: event.message || "", status: "warning" });
      return;
    }
    if (event.type === "turn_complete") {
      const status = String(event.status || "unknown");
      const outcome = classifyOutcome(
        status,
        event.verification_pending === true,
      );
      completePlanning(outcome.success);
      runtime.runTerminal = true;
      addTimelineItem({
        icon: outcome.success ? "✓" : "!",
        title: outcome.success ? "本轮任务已完成" : "本轮任务已结束",
        detail: `${event.steps || 0} 步 · ${(event.changed_files || []).length} 个变更文件 · ${formatDuration(event.duration_ms || 0)}`,
        status: outcome.kind === "error" ? "error" : outcome.kind,
      });
      appendMessage("assistant", event.summary || "任务已结束，未收到有效报告。", status);
      stopElapsedClock(Number(event.duration_ms));
      updateVerification(event);
      setRunState(outcome.kind, outcome.label);
      return;
    }
    if (event.type === "turn_error") {
      completePlanning(false);
      runtime.runTerminal = true;
      runtime.poisoned = true;
      addTimelineItem({ icon: "!", title: "会话已安全关闭", detail: event.message || "", status: "error" });
      appendMessage("assistant", event.message || "任务中断，请重启 ForgeLoop。", "error");
      stopElapsedClock(Date.now() - runtime.startedAt);
      setRunState("error", "会话已关闭");
      updateVerification({
        status: "error",
        verification_pending: true,
        verifications: ["任务中断，需重启后检查工作区。"],
      });
    }
  }

  async function finishStream(runId) {
    if (runtime.activeRunId !== runId) {
      return;
    }
    if (!runtime.runTerminal && !runtime.poisoned) {
      try {
        const snapshot = await fetchStatus();
        applySnapshot(snapshot, true);
      } catch (_error) {
        enterUncertainState("实时连接已结束；刷新页面以重新确认任务状态。");
      }
      return;
    }
    setBusy(false);
    runtime.activeRunId = null;
  }

  async function processEventLines(lines) {
    let processed = 0;
    for (const line of lines) {
      if (!line.trim()) {
        continue;
      }
      try {
        handleEvent(JSON.parse(line));
      } catch (_error) {
        addTimelineItem({ icon: "!", title: "忽略了一条无效事件", status: "warning" });
      }
      processed += 1;
      if (processed % EVENT_BATCH_SIZE === 0) {
        await new Promise((resolve) => window.setTimeout(resolve, 0));
      }
    }
  }

  async function streamRun(runId) {
    let attempts = 0;
    while (runtime.activeRunId === runId && runtime.busy) {
      try {
        const response = await fetch(`/api/events?run_id=${encodeURIComponent(runId)}&cursor=${runtime.cursor}`, {
          method: "GET",
          headers: apiHeaders(),
          cache: "no-store",
          credentials: "same-origin",
        });
        if (!response.ok || !response.body) {
          await readJson(response);
          throw new Error("浏览器不支持实时事件流。");
        }
        setConnection("connected", "本机已连接");
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        while (true) {
          const result = await reader.read();
          buffer += decoder.decode(result.value || new Uint8Array(), { stream: !result.done });
          const lines = buffer.split("\n");
          buffer = lines.pop() || "";
          await processEventLines(lines);
          if (result.done) {
            if (buffer.trim()) {
              await processEventLines([buffer]);
            }
            break;
          }
        }
        await finishStream(runId);
        return;
      } catch (error) {
        attempts += 1;
        setConnection("error", "正在重连");
        if (attempts > 4) {
          showToast(error.message || "实时连接中断。", "error");
          try {
            const snapshot = await fetchStatus();
            applySnapshot(snapshot, true);
          } catch (_statusError) {
            enterUncertainState("无法确认任务是否完成；刷新页面以重新同步。");
          }
          return;
        }
        await new Promise((resolve) => window.setTimeout(resolve, attempts * 650));
      }
    }
  }

  function applySnapshot(snapshot, replaceConversation = true) {
    setConnection("connected", "本机已连接");
    renderSidebar(snapshot);
    ui.workspace.textContent = snapshot.workspace || "未知工作区";
    ui.workspace.title = snapshot.workspace || "";
    ui.model.textContent = snapshot.model || "DeepSeek";
    const maxSubagents = Math.max(0, Number(snapshot.max_subagents) || 0);
    ui.agentCount.textContent = maxSubagents ? `≤${maxSubagents}` : "OFF";
    ui.agentCount.title = maxSubagents
      ? `最多 ${maxSubagents} 个并行只读子 Agent`
      : "未启用并行子 Agent";
    const maxSteps = Number(snapshot.max_steps);
    if (Number.isInteger(maxSteps) && maxSteps > 0) {
      ui.executionModeSymbol.textContent = "≤";
      ui.executionModeLabel.textContent = `最多 ${maxSteps} 步`;
      ui.executionMode.title = `本次服务配置了 ${maxSteps} 步决策上限`;
    } else {
      ui.executionModeSymbol.textContent = "∞";
      ui.executionModeLabel.textContent = "无固定步数";
      ui.executionMode.title = "默认持续运行到模型完成；工具与时间安全保护仍然有效";
    }
    runtime.turn = Number(snapshot.turn_count) || 0;
    ui.turn.textContent = formatTurn(runtime.turn);
    runtime.poisoned = snapshot.poisoned === true;
    if (replaceConversation) {
      renderConversation(snapshot.conversation);
    }
    if (runtime.poisoned) {
      if (runtime.elapsedTimer) {
        stopElapsedClock(Date.now() - runtime.startedAt);
      }
      settlePendingTrace("error");
      updateVerification({
        status: "error",
        verification_pending: true,
        verifications: ["会话已关闭，重启后请先检查工作区状态。"],
      });
      runtime.activeRunId = null;
      setBusy(false);
      setConnection("error", "会话需重启");
      setRunState("error", "需要重启");
      return;
    }
    if (snapshot.busy && snapshot.active_run_id) {
      const changedRun = runtime.activeRunId !== snapshot.active_run_id;
      runtime.activeRunId = snapshot.active_run_id;
      if (changedRun) {
        resetTrace();
      }
      if (changedRun || !runtime.elapsedTimer) {
        startElapsedClock(snapshot.active_elapsed_ms);
      }
      setBusy(true);
      streamRun(runtime.activeRunId);
    } else {
      if (runtime.elapsedTimer) {
        stopElapsedClock(Date.now() - runtime.startedAt);
      }
      runtime.activeRunId = null;
      setBusy(false);
      const latestOutcome = snapshot.latest_outcome && typeof snapshot.latest_outcome === "object"
        ? snapshot.latest_outcome
        : null;
      if (latestOutcome) {
        const status = String(latestOutcome.status || "unknown");
        settlePendingTrace(status);
        const verificationPending = latestOutcome.verification_pending === true;
        updateVerification({
          status,
          verification_pending: verificationPending,
          verifications: Array.isArray(latestOutcome.verifications)
            ? latestOutcome.verifications
            : [],
        });
        runtime.maxStep = Number(latestOutcome.steps) || 0;
        ui.stepCount.textContent = `${runtime.maxStep} 步`;
        ui.elapsed.textContent = formatDuration(Number(latestOutcome.duration_ms) || 0);
        if (!ui.timeline.children.length) {
          const restoredOutcome = classifyOutcome(status, verificationPending);
          addTimelineItem({
            icon: restoredOutcome.success ? "✓" : "!",
            title: restoredOutcome.success ? "最近一轮任务已完成" : "最近一轮任务已结束",
            detail: `${runtime.maxStep} 步 · ${(latestOutcome.changed_files || []).length} 个变更文件 · ${formatDuration(Number(latestOutcome.duration_ms) || 0)}`,
            status: restoredOutcome.kind === "error" ? "error" : restoredOutcome.kind,
          });
        }
        const outcome = classifyOutcome(status, verificationPending);
        setRunState(outcome.kind, outcome.label);
      } else {
        setRunState("", runtime.turn ? "等待后续任务" : "等待任务");
      }
    }
  }

  function folderIcon(open) {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 16 16");
    svg.setAttribute("width", "16");
    svg.setAttribute("height", "16");
    svg.setAttribute("aria-hidden", "true");
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("fill", "currentColor");
    path.setAttribute(
      "d",
      open
        ? "M1.75 5A1.75 1.75 0 0 1 3.5 3.25h2.53c.33 0 .65.13.88.37l.62.63a.5.5 0 0 0 .35.14h4.62A1.75 1.75 0 0 1 14.25 6.14V12A1.75 1.75 0 0 1 12.5 13.75h-9A1.75 1.75 0 0 1 1.75 12V5z"
        : "M1.75 5A1.75 1.75 0 0 1 3.5 3.25h2.53c.33 0 .65.13.88.37l.62.63a.5.5 0 0 0 .35.14h4.62A1.75 1.75 0 0 1 14.25 6.14V12A1.75 1.75 0 0 1 12.5 13.75h-9A1.75 1.75 0 0 1 1.75 12V5z"
    );
    svg.append(path);
    return svg;
  }

  async function switchWorkspace(path) {
    if (runtime.busy) {
      showToast("任务执行中，暂时不能切换工作区。", "error");
      return;
    }
    if (runtime.workspaceSwitching) {
      return;
    }
    runtime.workspaceSwitching = true;
    ui.workspaceSwitcher.disabled = true;
    try {
      const response = await fetch("/api/workspace", {
        method: "POST",
        headers: apiHeaders(true),
        credentials: "same-origin",
        body: JSON.stringify({ path }),
      });
      const payload = await readJson(response);
      const snapshot = payload.state && typeof payload.state === "object"
        ? payload.state
        : await fetchStatus();
      if (payload.session_reset === true) {
        stopElapsedClock();
        ui.elapsed.textContent = "—";
        resetTrace();
      }
      applySnapshot(snapshot, true);
      showToast(
        payload.session_reset === true
          ? `已切换工作区并开始新会话：${payload.workspace || path}`
          : "当前目录已经是目标工作区。",
        "success",
      );
      if (!runtime.poisoned && !runtime.busy) {
        ui.input.focus();
      }
    } catch (error) {
      showToast(error.message || "切换工作区失败。", "error");
    } finally {
      runtime.workspaceSwitching = false;
      ui.workspaceSwitcher.disabled = runtime.busy;
    }
  }

  function statusDotClass(status) {
    if (status === "completed") {
      return "success";
    }
    if (status === "error") {
      return "error";
    }
    if (
      status === "completed_with_verification_risk" ||
      status === "step_limit" ||
      status === "tool_call_limit" ||
      status === "runtime_limit" ||
      status === "repetition_limit"
    ) {
      return "warning";
    }
    return "";
  }

  function relativeTimeLabel(updatedAt) {
    const now = Date.now();
    const MIN = 60000;
    const HOUR = 3600000;
    const DAY = 86400000;
    const diff = Math.max(0, now - Number(updatedAt || 0) * 1000);
    if (diff < MIN) return "刚刚";
    if (diff < HOUR) return `${Math.floor(diff / MIN)}分钟`;
    if (diff < DAY) return `${Math.floor(diff / HOUR)}小时`;
    if (diff < 30 * DAY) return `${Math.floor(diff / DAY)}天`;
    if (diff < 365 * DAY) return `${Math.floor(diff / (30 * DAY))}个月`;
    return `${Math.floor(diff / (365 * DAY))}年`;
  }

  function workspaceCreatedLabel(createdAt) {
    const date = new Date(Number(createdAt || 0) * 1000);
    const pad = (value) => String(value).padStart(2, "0");
    return (
      `${date.getFullYear()}年${date.getMonth() + 1}月${date.getDate()}日 ` +
      `${pad(date.getHours())}:${pad(date.getMinutes())}`
    );
  }

  let hoverCardTimer = null;

  function showWorkspaceHoverCard(anchor, item) {
    ui.sidebarHoverTitle.textContent = String(item.title || item.path || "");
    ui.sidebarHoverPath.textContent = String(item.path || "");
    ui.sidebarHoverPath.title = String(item.path || "");
    ui.sidebarHoverTime.textContent = `创建于 ${workspaceCreatedLabel(item.created_at)}`;
    ui.sidebarHoverCard.hidden = false;
    const rect = anchor.getBoundingClientRect();
    const cardWidth = ui.sidebarHoverCard.offsetWidth || 240;
    const cardHeight = ui.sidebarHoverCard.offsetHeight || 96;
    let left = rect.right + 8;
    if (left + cardWidth > window.innerWidth - 8) {
      left = Math.max(8, rect.left - cardWidth - 8);
    }
    let top = rect.top;
    if (top + cardHeight > window.innerHeight - 8) {
      top = Math.max(8, window.innerHeight - cardHeight - 8);
    }
    ui.sidebarHoverCard.style.left = `${left.toFixed(0)}px`;
    ui.sidebarHoverCard.style.top = `${top.toFixed(0)}px`;
  }

  function sessionRow(item, activeSessionId, archived) {
    const row = node("li", "sidebar-row-seat");
    const button = node("button", "sidebar-row sidebar-session-row");
    button.type = "button";
    if (item.id === activeSessionId) {
      button.classList.add("active");
    }
    button.disabled = runtime.busy;
    const dot = node(
      "span",
      `sidebar-status-dot ${statusDotClass(String(item.status || ""))}`,
    );
    dot.title = String(item.status || "");
    button.append(dot);
    const title = node("span", "sidebar-row-title", String(item.title || "新会话"));
    button.append(title);
    const time = node("span", "sidebar-row-time", relativeTimeLabel(item.updated_at));
    time.title = `${relativeTimeLabel(item.updated_at)}前更新`;
    button.append(time);
    button.addEventListener("click", () => {
      if (runtime.busy) {
        showToast("任务执行中，暂时不能切换会话。", "error");
        return;
      }
      selectSession(String(item.id || ""));
    });
    const trigger = node("button", "sidebar-row-menu-trigger", "⋯");
    trigger.type = "button";
    trigger.title = "更多操作";
    trigger.setAttribute("aria-label", `会话“${item.title}”的操作`);
    trigger.disabled = runtime.busy;
    trigger.addEventListener("click", (event) => {
      event.stopPropagation();
      openRowMenu(trigger, {
        target: "session",
        id: String(item.id || ""),
        title: String(item.title || "新会话"),
        archived: Boolean(archived),
      });
    });
    row.append(button, trigger);
    return row;
  }

  function groupHeaderRow(item, expanded, hasCurrent, onToggle, onAdd, onMenu) {
    const headerRow = node("div", "sidebar-row-seat sidebar-group-seat");
    const header = node("button", "sidebar-row sidebar-group-row");
    header.type = "button";
    if (hasCurrent) {
      header.classList.add("contains-current");
    }
    header.disabled = runtime.busy;
    const chevron = node(
      "span",
      `sidebar-group-chevron${expanded ? " open" : ""}`,
      "▸",
    );
    header.append(chevron);
    const icon = folderIcon(hasCurrent);
    icon.classList.add("sidebar-row-icon");
    header.append(icon);
    const title = node(
      "span",
      "sidebar-row-title",
      String(item.title || item.path || ""),
    );
    title.title = String(item.path || "");
    header.append(title);
    header.addEventListener("click", onToggle);
    if (onAdd) {
      const addButton = node("button", "sidebar-row-add", "＋");
      addButton.type = "button";
      addButton.title = `在“${item.title}”中新建会话`;
      addButton.addEventListener("click", (event) => {
        event.stopPropagation();
        onAdd();
      });
      headerRow.append(addButton);
    }
    if (onMenu) {
      const trigger = node("button", "sidebar-row-menu-trigger", "⋯");
      trigger.type = "button";
      trigger.title = "更多操作";
      trigger.setAttribute("aria-label", `工作区“${item.title}”的操作`);
      trigger.disabled = runtime.busy;
      trigger.addEventListener("click", (event) => {
        event.stopPropagation();
        onMenu(trigger);
      });
      headerRow.append(trigger);
    }
    headerRow.prepend(header);
    return headerRow;
  }

  function workspaceGroupRow(item, expanded, hasCurrent, groupSessions, activeSessionId) {
    const group = node("li", "sidebar-group");
    const headerRow = groupHeaderRow(
      item,
      expanded,
      hasCurrent,
      () => {
        if (expanded) {
          runtime.expandedWorkspaces.delete(item.id);
        } else {
          runtime.expandedWorkspaces.add(item.id);
        }
        if (runtime.lastSnapshot) {
          renderSidebar(runtime.lastSnapshot);
        }
      },
      () => {
        if (runtime.busy) {
          showToast("任务执行中。", "error");
          return;
        }
        newSession(String(item.id || ""));
      },
      (trigger) => {
        openRowMenu(trigger, {
          target: "workspace",
          id: String(item.id || ""),
          title: String(item.title || item.path || ""),
          archived: false,
        });
      },
    );
    headerRow.addEventListener("mouseenter", () => {
      if (!window.matchMedia("(hover: hover)").matches) {
        return;
      }
      window.clearTimeout(hoverCardTimer);
      hoverCardTimer = window.setTimeout(
        () => showWorkspaceHoverCard(headerRow, item),
        400,
      );
    });
    headerRow.addEventListener("mouseleave", () => {
      window.clearTimeout(hoverCardTimer);
    });
    group.append(headerRow);
    if (expanded) {
      const list = node("ul", "sidebar-session-sublist");
      for (const session of groupSessions) {
        list.append(sessionRow(session, activeSessionId, false));
      }
      if (!groupSessions.length) {
        list.append(node("li", "sidebar-group-empty", "暂无会话"));
      }
      group.append(list);
    }
    return group;
  }

  function renderSidebar(snapshot) {
    runtime.lastSnapshot = snapshot;
    const workspaces = Array.isArray(snapshot.workspaces) ? snapshot.workspaces : [];
    const sessions = Array.isArray(snapshot.sessions) ? snapshot.sessions : [];
    const activeWorkspaceId = String(snapshot.active_workspace_id || "");
    const activeSessionId = String(snapshot.active_session_id || "");
    runtime.workspaces = workspaces;
    runtime.activeWorkspaceId = activeWorkspaceId;
    const workspaceIds = new Set(workspaces.map((item) => item.id));
    for (const id of Array.from(runtime.expandedWorkspaces)) {
      if (!workspaceIds.has(id)) {
        runtime.expandedWorkspaces.delete(id);
      }
    }
    if (activeWorkspaceId) {
      runtime.expandedWorkspaces.add(activeWorkspaceId);
    }

    const archivedSessions = sessions
      .filter((item) => item.archived)
      .sort((a, b) => Number(b.updated_at || 0) - Number(a.updated_at || 0));
    ui.sidebarArchivedToggle.hidden = archivedSessions.length === 0;
    ui.sidebarArchivedToggle.textContent = runtime.archivedOpen
      ? `收起已归档（${archivedSessions.length}）`
      : `已归档（${archivedSessions.length}）`;

    const query = ui.sidebarSearchInput.value.trim().toLowerCase();
    ui.sidebarSearchClear.hidden = !query;
    ui.sidebarTree.replaceChildren();

    if (query) {
      const matched = sessions
        .filter(
          (item) =>
            !item.archived &&
            String(item.title || "").toLowerCase().includes(query),
        )
        .sort((a, b) => Number(b.updated_at || 0) - Number(a.updated_at || 0));
      for (const item of matched) {
        ui.sidebarTree.append(sessionRow(item, activeSessionId, false));
      }
      ui.sidebarEmpty.hidden = matched.length > 0;
      ui.sidebarEmpty.textContent = "无匹配结果";
    } else {
      for (const item of workspaces) {
        const groupSessions = sessions
          .filter((session) => session.workspace_id === item.id && !session.archived)
          .sort((a, b) => Number(b.updated_at || 0) - Number(a.updated_at || 0));
        const expanded = runtime.expandedWorkspaces.has(item.id);
        const hasCurrent =
          item.id === activeWorkspaceId ||
          groupSessions.some((session) => session.id === activeSessionId);
        ui.sidebarTree.append(
          workspaceGroupRow(item, expanded, hasCurrent, groupSessions, activeSessionId),
        );
      }
      const orphans = sessions
        .filter(
          (session) =>
            !session.archived &&
            session.workspace_id &&
            !workspaceIds.has(session.workspace_id),
        )
        .sort((a, b) => Number(b.updated_at || 0) - Number(a.updated_at || 0));
      if (orphans.length) {
        const group = node("li", "sidebar-group");
        const headerRow = groupHeaderRow(
          { id: "__ungrouped__", title: "未分组", path: "" },
          runtime.expandedWorkspaces.has("__ungrouped__"),
          orphans.some((session) => session.id === activeSessionId),
          () => {
            if (runtime.expandedWorkspaces.has("__ungrouped__")) {
              runtime.expandedWorkspaces.delete("__ungrouped__");
            } else {
              runtime.expandedWorkspaces.add("__ungrouped__");
            }
            if (runtime.lastSnapshot) {
              renderSidebar(runtime.lastSnapshot);
            }
          },
          null,
          null,
        );
        group.append(headerRow);
        if (runtime.expandedWorkspaces.has("__ungrouped__")) {
          const list = node("ul", "sidebar-session-sublist");
          for (const session of orphans) {
            list.append(sessionRow(session, activeSessionId, false));
          }
          group.append(list);
        }
        ui.sidebarTree.append(group);
      }
      if (runtime.archivedOpen && archivedSessions.length) {
        const group = node("li", "sidebar-group");
        const headerRow = groupHeaderRow(
          { id: "__archived__", title: "已归档", path: "" },
          true,
          false,
          () => {
            runtime.archivedOpen = false;
            renderSidebar(runtime.lastSnapshot);
          },
          null,
          null,
        );
        group.append(headerRow);
        const list = node("ul", "sidebar-session-sublist");
        for (const session of archivedSessions) {
          list.append(sessionRow(session, activeSessionId, true));
        }
        group.append(list);
        ui.sidebarTree.append(group);
      }
      const hasContent = ui.sidebarTree.children.length > 0;
      ui.sidebarEmpty.hidden = hasContent;
      ui.sidebarEmpty.textContent = "暂无会话";
    }
    ui.sidebarNewSession.disabled = runtime.busy;
  }

  function openRowMenu(anchor, target) {
    rowMenuTarget = target;
    const menu = ui.workspaceRowMenu;
    ui.rowMenuRename.hidden = false;
    ui.rowMenuDelete.hidden = false;
    ui.rowMenuFork.hidden = target.target !== "session" || target.archived;
    ui.rowMenuArchive.hidden = target.target !== "session" || target.archived;
    ui.rowMenuUnarchive.hidden = target.target !== "session" || !target.archived;
    ui.rowMenuDelete.textContent =
      target.target === "workspace" ? "删除工作区" : "删除会话";
    menu.hidden = false;
    const rect = anchor.getBoundingClientRect();
    const width = menu.offsetWidth || 120;
    const height = menu.offsetHeight || 72;
    let left = rect.right - width;
    let top = rect.bottom + 4;
    if (left < 8) {
      left = 8;
    }
    if (top + height > window.innerHeight - 8) {
      top = Math.max(8, rect.top - height - 4);
    }
    menu.style.left = `${left.toFixed(0)}px`;
    menu.style.top = `${top.toFixed(0)}px`;
  }

  function closeRowMenu() {
    rowMenuTarget = null;
    ui.workspaceRowMenu.hidden = true;
  }

  function openRenameDialog(target) {
    ui.renameTitle.textContent =
      target.target === "workspace" ? "重命名工作区" : "重命名会话";
    ui.renameInput.value = target.title;
    ui.renameConfirm.disabled = !ui.renameInput.value.trim();
    ui.renameModal.hidden = false;
    window.requestAnimationFrame(() => {
      ui.renameInput.focus();
      ui.renameInput.select();
    });
  }

  function closeRenameDialog() {
    ui.renameModal.hidden = true;
  }

  async function confirmRename() {
    if (!rowMenuTarget) {
      return;
    }
    const target = rowMenuTarget;
    const title = ui.renameInput.value.trim();
    if (!title) {
      return;
    }
    ui.renameConfirm.disabled = true;
    try {
      const response = await fetch("/api/store", {
        method: "POST",
        headers: apiHeaders(true),
        credentials: "same-origin",
        body: JSON.stringify({
          target: target.target,
          action: "rename",
          id: target.id,
          title,
        }),
      });
      const payload = await readJson(response);
      closeRenameDialog();
      applySessionState(payload.state);
      showToast("已重命名。", "success");
    } catch (error) {
      showToast(error.message || "重命名失败。", "error");
      ui.renameConfirm.disabled = !ui.renameInput.value.trim();
    }
  }

  function openDeleteDialog(target) {
    ui.deleteTitle.textContent =
      target.target === "workspace" ? "删除工作区" : "删除会话";
    ui.deleteDesc.textContent =
      target.target === "workspace"
        ? `将把“${target.title}”从工作区列表中移除。文件夹与会话记录会保留。`
        : `将删除会话“${target.title}”，其对话历史会一并移除，但不会修改或删除磁盘文件。`;
    ui.deleteModal.hidden = false;
    window.requestAnimationFrame(() => {
      ui.deleteConfirm.focus();
    });
  }

  function closeDeleteDialog() {
    ui.deleteModal.hidden = true;
  }

  async function confirmDelete() {
    if (!rowMenuTarget) {
      return;
    }
    const target = rowMenuTarget;
    ui.deleteConfirm.disabled = true;
    try {
      const response = await fetch("/api/store", {
        method: "POST",
        headers: apiHeaders(true),
        credentials: "same-origin",
        body: JSON.stringify({
          target: target.target,
          action: "delete",
          id: target.id,
        }),
      });
      const payload = await readJson(response);
      closeDeleteDialog();
      applySessionState(payload.state);
      showToast("已删除。", "success");
    } catch (error) {
      showToast(error.message || "删除失败。", "error");
      ui.deleteConfirm.disabled = false;
    }
  }

  async function storeAction(action) {
    const target = rowMenuTarget;
    closeRowMenu();
    if (!target) {
      return;
    }
    try {
      const response = await fetch("/api/store", {
        method: "POST",
        headers: apiHeaders(true),
        credentials: "same-origin",
        body: JSON.stringify({
          target: target.target,
          action,
          id: target.id,
        }),
      });
      const payload = await readJson(response);
      applySessionState(payload.state);
      const labels = {
        fork: "已分叉为新会话。",
        archive: "已归档会话。",
        unarchive: "已恢复会话。",
      };
      showToast(labels[action] || "操作完成。", "success");
    } catch (error) {
      showToast(error.message || "操作失败。", "error");
    }
  }

  async function applySessionState(state) {
    stopElapsedClock();
    ui.elapsed.textContent = "—";
    resetTrace();
    applySnapshot(state, true);
  }

  async function newSession(workspaceId = null) {
    if (runtime.busy) {
      return;
    }
    const body = workspaceId
      ? { action: "new", workspace_id: workspaceId }
      : { action: "new" };
    try {
      const response = await fetch("/api/session", {
        method: "POST",
        headers: apiHeaders(true),
        credentials: "same-origin",
        body: JSON.stringify(body),
      });
      const payload = await readJson(response);
      applySessionState(payload.state);
      showToast("已新建会话。", "success");
      closeSidebar(false);
    } catch (error) {
      showToast(error.message || "新建会话失败。", "error");
    }
  }

  async function pickNativeDirectory() {
    if (runtime.busy || runtime.workspaceSwitching) {
      return;
    }
    try {
      const response = await fetch("/api/pick-native", {
        method: "POST",
        headers: apiHeaders(true),
        credentials: "same-origin",
        body: JSON.stringify({}),
      });
      const payload = await readJson(response);
      if (payload.path) {
        await switchWorkspace(String(payload.path));
      }
    } catch (error) {
      showToast(error.message || "无法打开系统文件选择器。", "error");
    }
  }

  async function pickForNewSession() {
    if (runtime.busy || runtime.workspaceSwitching) {
      return;
    }
    try {
      const response = await fetch("/api/pick-native", {
        method: "POST",
        headers: apiHeaders(true),
        credentials: "same-origin",
        body: JSON.stringify({}),
      });
      const payload = await readJson(response);
      if (!payload.path) {
        return;
      }
      const currentWorkspace = String(
        (runtime.lastSnapshot && runtime.lastSnapshot.workspace) || "",
      );
      if (String(payload.path) === currentWorkspace) {
        await newSession();
      } else {
        await switchWorkspace(String(payload.path));
      }
    } catch (error) {
      showToast(error.message || "无法打开系统文件选择器。", "error");
    }
  }

  async function selectSession(sessionId) {
    if (runtime.busy) {
      return;
    }
    try {
      const response = await fetch("/api/session", {
        method: "POST",
        headers: apiHeaders(true),
        credentials: "same-origin",
        body: JSON.stringify({ action: "select", session_id: sessionId }),
      });
      const payload = await readJson(response);
      applySessionState(payload.state);
      showToast("已切换到该会话。", "success");
      closeSidebar(false);
    } catch (error) {
      showToast(error.message || "切换会话失败。", "error");
    }
  }

  function openSidebar() {
    document.body.classList.add("sidebar-open");
    ui.sidebarToggle.setAttribute("aria-expanded", "true");
  }

  function closeSidebar(restoreFocus = true) {
    document.body.classList.remove("sidebar-open");
    ui.sidebarToggle.setAttribute("aria-expanded", "false");
    if (restoreFocus) {
      ui.sidebarToggle.focus();
    }
  }

  async function submitTask(task) {
    const value = task.trim();
    if (!value || runtime.busy || runtime.poisoned) {
      return;
    }
    const original = value;
    const turnBeforeSubmit = runtime.turn;
    ui.input.value = "";
    resizeComposer();
    appendMessage("user", original);
    resetTrace();
    runtime.turn += 1;
    ui.turn.textContent = formatTurn(runtime.turn);
    setBusy(true);
    startElapsedClock();

    try {
      const response = await fetch("/api/turn", {
        method: "POST",
        headers: apiHeaders(true),
        credentials: "same-origin",
        body: JSON.stringify({ task: original }),
      });
      const payload = await readJson(response);
      runtime.activeRunId = payload.run_id;
      runtime.turn = Number(payload.turn) || runtime.turn;
      ui.turn.textContent = formatTurn(runtime.turn);
      await streamRun(runtime.activeRunId);
    } catch (error) {
      stopElapsedClock(Date.now() - runtime.startedAt);
      showToast(error.message || "任务提交失败。", "error");
      setRunState("error", "提交失败");
      let restoreDraft = error.status === 409;
      try {
        const snapshot = await fetchStatus();
        const accepted =
          error.status !== 409 &&
          Number(snapshot.turn_count) > turnBeforeSubmit;
        restoreDraft = restoreDraft || !accepted;
        applySnapshot(snapshot, true);
      } catch (_statusError) {
        enterUncertainState("无法确认任务是否已提交；刷新页面以重新同步。");
        restoreDraft = true;
      }
      if (!runtime.poisoned && restoreDraft) {
        ui.input.value = original;
        resizeComposer();
        updateSendState();
      }
    }
  }

  const mobileActivity = window.matchMedia("(max-width: 760px)");

  function setBackgroundInert(value) {
    ui.topbar.inert = value;
    ui.conversationPane.inert = value;
  }

  function syncActivityAccessibility(open = false) {
    ui.activityToggle.setAttribute(
      "aria-expanded",
      mobileActivity.matches && open ? "true" : "false"
    );
    if (!mobileActivity.matches) {
      document.body.classList.remove("activity-open");
      ui.activityPanel.inert = false;
      ui.activityPanel.removeAttribute("aria-hidden");
      ui.activityPanel.removeAttribute("role");
      ui.activityPanel.removeAttribute("aria-modal");
      setBackgroundInert(false);
      return;
    }
    ui.activityPanel.setAttribute("role", "dialog");
    ui.activityPanel.setAttribute("aria-modal", "true");
    ui.activityPanel.inert = !open;
    ui.activityPanel.setAttribute("aria-hidden", open ? "false" : "true");
    setBackgroundInert(open);
  }

  function openActivity() {
    document.body.classList.add("activity-open");
    ui.activityToggle.setAttribute("aria-expanded", "true");
    syncActivityAccessibility(true);
    ui.drawerClose.focus();
  }

  function closeActivity(restoreFocus = true) {
    document.body.classList.remove("activity-open");
    ui.activityToggle.setAttribute("aria-expanded", "false");
    syncActivityAccessibility(false);
    if (restoreFocus && mobileActivity.matches) {
      ui.activityToggle.focus();
    }
  }

  function trapActivityFocus(event) {
    const focusable = Array.from(ui.activityPanel.querySelectorAll(
      'button:not([disabled]), [href], [tabindex]:not([tabindex="-1"])'
    )).filter((element) => !element.hidden);
    if (!focusable.length) {
      return;
    }
    const current = focusable.indexOf(document.activeElement);
    const direction = event.shiftKey ? -1 : 1;
    const next = current < 0
      ? 0
      : (current + direction + focusable.length) % focusable.length;
    event.preventDefault();
    focusable[next].focus();
  }

  const PET_LINES = [
    "在呢在呢～",
    "今天也要加油哦 ✿",
    "休息一下也没关系",
    "需要我帮你看看工作区吗？",
    "敲代码要记得喝水～",
  ];
  let petBubbleTimer = null;

  function petSpeak() {
    ui.pet.classList.remove("wiggling");
    void ui.pet.offsetWidth;
    ui.pet.classList.add("wiggling");
    ui.petBubble.textContent =
      PET_LINES[Math.floor(Math.random() * PET_LINES.length)];
    ui.petBubble.hidden = false;
    window.clearTimeout(petBubbleTimer);
    petBubbleTimer = window.setTimeout(() => {
      ui.petBubble.hidden = true;
    }, 2600);
  }

  function initPet() {
    let drag = null;

    function movePet(event) {
      if (!drag) {
        return;
      }
      const gallery = ui.pet.closest(".muse-gallery");
      if (!gallery) {
        return;
      }
      if (
        Math.abs(event.clientX - drag.startClientX) +
          Math.abs(event.clientY - drag.startClientY) >
        4
      ) {
        drag.moved = true;
      }
      const galleryRect = gallery.getBoundingClientRect();
      const size = ui.pet.offsetWidth || 64;
      let left = event.clientX - galleryRect.left - drag.offsetX;
      let top = event.clientY - galleryRect.top - drag.offsetY;
      left = Math.max(4, Math.min(left, galleryRect.width - size - 4));
      top = Math.max(4, Math.min(top, galleryRect.height - size - 4));
      // CSSOM, not the style attribute: CSP (style-src 'self') blocks inline
      // style="" attributes, so position through el.style instead.
      ui.pet.style.left = `${left.toFixed(1)}px`;
      ui.pet.style.top = `${top.toFixed(1)}px`;
      ui.pet.style.right = "auto";
    }

    function endPetDrag(event) {
      if (!drag) {
        return;
      }
      const wasDrag = drag;
      drag = null;
      ui.pet.classList.remove("dragging");
      window.removeEventListener("pointermove", movePet);
      window.removeEventListener("pointerup", endPetDrag);
      window.removeEventListener("pointercancel", endPetDrag);
      try {
        ui.pet.releasePointerCapture(event.pointerId);
      } catch (_error) {
        /* not captured */
      }
      if (!wasDrag.moved) {
        petSpeak();
      }
    }

    ui.pet.addEventListener("pointerdown", (event) => {
      if (event.button !== undefined && event.button !== 0) {
        return;
      }
      event.preventDefault();
      const rect = ui.pet.getBoundingClientRect();
      drag = {
        offsetX: event.clientX - rect.left,
        offsetY: event.clientY - rect.top,
        startClientX: event.clientX,
        startClientY: event.clientY,
        moved: false,
      };
      ui.pet.classList.add("dragging");
      try {
        ui.pet.setPointerCapture(event.pointerId);
      } catch (_error) {
        /* pointer capture unavailable; window-level listeners still track the drag */
      }
      window.addEventListener("pointermove", movePet);
      window.addEventListener("pointerup", endPetDrag);
      window.addEventListener("pointercancel", endPetDrag);
    });
    ui.pet.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        petSpeak();
      }
    });
  }

  ui.workspaceSwitcher.addEventListener("click", () => {
    pickNativeDirectory();
  });

  ui.sidebarNewSession.addEventListener("click", () => {
    pickForNewSession();
  });
  ui.rowMenuRename.addEventListener("click", () => {
    const target = rowMenuTarget;
    closeRowMenu();
    if (target) {
      openRenameDialog(target);
    }
  });
  ui.rowMenuDelete.addEventListener("click", () => {
    const target = rowMenuTarget;
    closeRowMenu();
    if (target) {
      openDeleteDialog(target);
    }
  });
  ui.rowMenuFork.addEventListener("click", () => storeAction("fork"));
  ui.rowMenuArchive.addEventListener("click", () => storeAction("archive"));
  ui.rowMenuUnarchive.addEventListener("click", () => storeAction("unarchive"));
  ui.sidebarSearchInput.addEventListener("input", () => {
    if (runtime.lastSnapshot) {
      renderSidebar(runtime.lastSnapshot);
    }
  });
  ui.sidebarSearchClear.addEventListener("click", () => {
    ui.sidebarSearchInput.value = "";
    if (runtime.lastSnapshot) {
      renderSidebar(runtime.lastSnapshot);
    }
    ui.sidebarSearchInput.focus();
  });
  ui.sidebarArchivedToggle.addEventListener("click", () => {
    runtime.archivedOpen = !runtime.archivedOpen;
    if (runtime.lastSnapshot) {
      renderSidebar(runtime.lastSnapshot);
    }
  });
  ui.sidebarHoverCopy.addEventListener("click", () => {
    const path = ui.sidebarHoverPath.textContent || "";
    if (!path) {
      return;
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(path).then(
        () => showToast("已复制", "success"),
        () => showToast("复制失败。", "error"),
      );
    }
  });
  ui.sidebarHoverCard.addEventListener("mouseleave", () => {
    ui.sidebarHoverCard.hidden = true;
  });
  ui.renameClose.addEventListener("click", closeRenameDialog);
  ui.renameCancel.addEventListener("click", closeRenameDialog);
  ui.renameMask.addEventListener("click", closeRenameDialog);
  ui.renameConfirm.addEventListener("click", confirmRename);
  ui.renameInput.addEventListener("input", () => {
    ui.renameConfirm.disabled = !ui.renameInput.value.trim();
  });
  ui.renameInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      confirmRename();
    }
    if (event.key === "Escape") {
      event.stopPropagation();
      closeRenameDialog();
    }
  });
  ui.deleteClose.addEventListener("click", closeDeleteDialog);
  ui.deleteCancel.addEventListener("click", closeDeleteDialog);
  ui.deleteMask.addEventListener("click", closeDeleteDialog);
  ui.deleteConfirm.addEventListener("click", confirmDelete);
  document.addEventListener("click", (event) => {
    if (
      !ui.workspaceRowMenu.hidden &&
      !ui.workspaceRowMenu.contains(event.target) &&
      !(event.target instanceof Element && event.target.classList.contains("sidebar-row-menu-trigger"))
    ) {
      closeRowMenu();
    }
  });
  ui.sidebarToggle.addEventListener("click", () => {
    if (document.body.classList.contains("sidebar-open")) {
      closeSidebar(false);
    } else {
      openSidebar();
    }
  });
  ui.sidebarBackdrop.addEventListener("click", () => closeSidebar());

  ui.form.addEventListener("submit", (event) => {
    event.preventDefault();
    submitTask(ui.input.value);
  });

  ui.input.addEventListener("input", () => {
    resizeComposer();
    updateSendState();
  });
  ui.input.addEventListener("compositionstart", () => { runtime.composing = true; });
  ui.input.addEventListener("compositionend", () => { runtime.composing = false; });
  ui.input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !runtime.composing && !event.isComposing) {
      if (runtime.busy) {
        return;
      }
      event.preventDefault();
      submitTask(ui.input.value);
    }
  });

  document.addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
      event.preventDefault();
      if (!ui.input.disabled) {
        ui.input.focus();
      }
    }
    if (event.key === "Escape" && document.body.classList.contains("activity-open")) {
      closeActivity();
    }
    if (event.key === "Escape" && document.body.classList.contains("sidebar-open")) {
      closeSidebar();
    }
    if (event.key === "Escape" && !ui.workspaceRowMenu.hidden) {
      closeRowMenu();
      return;
    }
    if (event.key === "Escape" && !ui.renameModal.hidden) {
      closeRenameDialog();
      return;
    }
    if (event.key === "Escape" && !ui.deleteModal.hidden) {
      closeDeleteDialog();
      return;
    }
    if (
      event.key === "Tab" &&
      mobileActivity.matches &&
      document.body.classList.contains("activity-open")
    ) {
      trapActivityFocus(event);
    }
  });

  for (const suggestion of document.querySelectorAll(".suggestion")) {
    suggestion.addEventListener("click", () => {
      ui.input.value = suggestion.dataset.prompt || "";
      resizeComposer();
      updateSendState();
      ui.input.focus();
    });
  }

  ui.activityToggle.addEventListener("click", openActivity);
  ui.drawerClose.addEventListener("click", closeActivity);
  ui.drawerBackdrop.addEventListener("click", closeActivity);
  ui.activityContent.addEventListener("scroll", () => {
    const distanceFromBottom =
      ui.activityContent.scrollHeight
      - ui.activityContent.scrollTop
      - ui.activityContent.clientHeight;
    runtime.followTimeline = distanceFromBottom < 96;
  }, { passive: true });
  mobileActivity.addEventListener("change", () => syncActivityAccessibility(false));
  syncActivityAccessibility(false);
  initPet();

  window.addEventListener("error", (event) => {
    showToast(`前端错误：${event.message}`, "error");
  });

  async function bootstrap() {
    if (!token) {
      setConnection("error", "安全令牌缺失");
      runtime.poisoned = true;
      setBusy(false);
      setRunState("error", "服务不可用");
      return;
    }
    try {
      const snapshot = await fetchStatus();
      setConnection("connected", "本机已连接");
      applySnapshot(snapshot, true);
      resizeComposer();
      updateSendState();
      if (!snapshot.busy && !snapshot.poisoned) {
        ui.input.focus();
      }
    } catch (error) {
      setConnection("error", "连接失败");
      setRunState("error", "服务不可用");
      runtime.poisoned = true;
      setBusy(false);
      showToast(error.message || "无法连接 ForgeLoop 本地服务。", "error");
    }
  }

  bootstrap();
})();
