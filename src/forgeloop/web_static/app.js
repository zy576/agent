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
    workspacePopover: document.querySelector("#workspace-popover"),
    workspaceList: document.querySelector("#workspace-list"),
    workspacePath: document.querySelector("#workspace-path"),
    workspaceUp: document.querySelector("#workspace-up"),
    workspaceRoots: document.querySelector("#workspace-roots"),
    workspaceGo: document.querySelector("#workspace-go"),
    workspaceSelectCurrent: document.querySelector("#workspace-select-current"),
    workspaceHint: document.querySelector("#workspace-hint"),
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
    workspaceBrowsing: false,
    browseSequence: 0,
  };

  let browseView = null;

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
    syncWorkspaceControls();
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

  function syncWorkspaceControls() {
    const browserLocked = runtime.workspaceSwitching || runtime.workspaceBrowsing;
    ui.workspaceSwitcher.disabled = runtime.busy || runtime.workspaceSwitching;
    ui.workspaceUp.disabled = browserLocked || !browseView || !browseView.parent;
    ui.workspaceRoots.disabled = browserLocked;
    ui.workspacePath.disabled = browserLocked;
    ui.workspaceGo.disabled = browserLocked;
    ui.workspaceSelectCurrent.disabled =
      browserLocked || !browseView || !browseView.path;
  }

  async function browseDirectories(path) {
    if (runtime.workspaceSwitching) {
      return;
    }
    const requestId = ++runtime.browseSequence;
    runtime.workspaceBrowsing = true;
    ui.workspacePopover.setAttribute("aria-busy", "true");
    ui.workspaceHint.classList.remove("error");
    ui.workspaceHint.textContent = "正在读取文件夹…";
    syncWorkspaceControls();
    try {
      const query = path ? `?path=${encodeURIComponent(path)}` : "";
      const payload = await fetch(`/api/browse${query}`, {
        method: "GET",
        headers: apiHeaders(),
        cache: "no-store",
        credentials: "same-origin",
      }).then(readJson);
      if (requestId !== runtime.browseSequence) {
        return;
      }
      browseView = payload;
      ui.workspacePath.value = String(payload.path || "");
      ui.workspaceList.replaceChildren();
      const entries = Array.isArray(payload.entries) ? payload.entries : [];
      for (const item of entries) {
        const row = node("li");
        const button = node("button", "workspace-entry");
        button.type = "button";
        button.append(node("span", "workspace-entry-icon", "▸"));
        const name = node("span", "workspace-entry-name", String(item.name || ""));
        name.title = String(item.path || "");
        button.append(name);
        button.addEventListener("click", () => browseDirectories(String(item.path || "")));
        row.append(button);
        ui.workspaceList.append(row);
      }
      ui.workspaceHint.textContent = payload.truncated
        ? `${entries.length}+ 个子文件夹（已截断）`
        : `${entries.length} 个子文件夹`;
    } catch (error) {
      if (requestId !== runtime.browseSequence) {
        return;
      }
      ui.workspaceHint.classList.add("error");
      ui.workspaceHint.textContent = error.message || "无法读取该目录。";
      showToast(error.message || "无法读取该目录。", "error");
    } finally {
      if (requestId === runtime.browseSequence) {
        runtime.workspaceBrowsing = false;
        ui.workspacePopover.removeAttribute("aria-busy");
        syncWorkspaceControls();
        if (
          !ui.workspacePopover.hidden &&
          (document.activeElement === document.body ||
            document.activeElement === ui.workspaceSwitcher)
        ) {
          ui.workspacePath.focus();
          ui.workspacePath.select();
        }
      }
    }
  }

  function setWorkspacePopover(open) {
    ui.workspacePopover.hidden = !open;
    ui.workspaceSwitcher.setAttribute("aria-expanded", open ? "true" : "false");
    if (open) {
      window.requestAnimationFrame(() => {
        if (!ui.workspacePopover.hidden) {
          ui.workspacePath.focus();
          ui.workspacePath.select();
        }
      });
    } else if (ui.workspacePopover.contains(document.activeElement)) {
      ui.workspaceSwitcher.focus();
    }
  }

  async function switchWorkspace(path) {
    if (runtime.busy) {
      showToast("任务执行中，暂时不能切换工作区。", "error");
      setWorkspacePopover(false);
      return;
    }
    if (runtime.workspaceSwitching) {
      return;
    }
    runtime.workspaceSwitching = true;
    ui.workspacePopover.setAttribute("aria-busy", "true");
    ui.workspaceHint.classList.remove("error");
    ui.workspaceHint.textContent = "正在切换并创建新会话…";
    syncWorkspaceControls();
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
      setWorkspacePopover(false);
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
      ui.workspaceHint.classList.add("error");
      ui.workspaceHint.textContent = error.message || "切换工作区失败。";
      showToast(error.message || "切换工作区失败。", "error");
    } finally {
      runtime.workspaceSwitching = false;
      ui.workspacePopover.removeAttribute("aria-busy");
      syncWorkspaceControls();
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
    ui.pet.addEventListener("click", petSpeak);
    ui.pet.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        petSpeak();
      }
    });
  }

  ui.workspaceSwitcher.addEventListener("click", () => {
    if (ui.workspacePopover.hidden) {
      setWorkspacePopover(true);
      browseDirectories(ui.workspace.textContent || "");
    } else {
      setWorkspacePopover(false);
    }
  });

  ui.workspaceRoots.addEventListener("click", () => {
    browseDirectories("");
  });

  ui.workspaceUp.addEventListener("click", () => {
    if (browseView && browseView.parent) {
      browseDirectories(browseView.parent);
    }
  });

  ui.workspaceGo.addEventListener("click", () => {
    browseDirectories(ui.workspacePath.value.trim());
  });

  ui.workspacePath.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      browseDirectories(ui.workspacePath.value.trim());
    }
  });

  ui.workspaceSelectCurrent.addEventListener("click", () => {
    const path = browseView ? String(browseView.path || "") : "";
    if (path) {
      switchWorkspace(path);
    }
  });

  document.addEventListener("click", (event) => {
    if (
      !ui.workspacePopover.hidden &&
      !(ui.workspaceIdentity && ui.workspaceIdentity.contains(event.target))
    ) {
      setWorkspacePopover(false);
    }
  });

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
    if (event.key === "Escape" && !ui.workspacePopover.hidden) {
      setWorkspacePopover(false);
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
