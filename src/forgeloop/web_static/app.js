(() => {
  "use strict";

  const tokenNode = document.querySelector('meta[name="forgeloop-token"]');
  const token = tokenNode ? tokenNode.content : "";

  const ui = {
    workspace: document.querySelector("#workspace-label"),
    model: document.querySelector("#model-label"),
    turn: document.querySelector("#turn-label"),
    elapsed: document.querySelector("#elapsed-label"),
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
    verificationTitle: document.querySelector("#verification-title"),
    verificationDetail: document.querySelector("#verification-detail"),
    activityToggle: document.querySelector("#activity-toggle"),
    activityPanel: document.querySelector("#activity-panel"),
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
  };

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
    ui.runState.classList.remove("running", "success", "error");
    if (kind) {
      ui.runState.classList.add(kind);
    }
    ui.runStateLabel.textContent = label;
  }

  function setBusy(value) {
    runtime.busy = value;
    ui.input.disabled = runtime.poisoned;
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
    requestAnimationFrame(() => {
      ui.conversationScroll.scrollTop = ui.conversationScroll.scrollHeight;
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

  function startElapsedClock() {
    window.clearInterval(runtime.elapsedTimer);
    runtime.startedAt = Date.now();
    ui.elapsed.textContent = "0s";
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
    ui.stepCount.textContent = "0 步";
    ui.verification.classList.remove("success", "pending");
    ui.verificationTitle.textContent = "等待验证结果";
    ui.verificationDetail.textContent = "ForgeLoop 会在修改后主动运行相关检查。";
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
      runtime.toolItems.set(key, { item, iconNode, copy, detailNode, stepNode });
    }
    if (step > runtime.maxStep) {
      runtime.maxStep = step;
      ui.stepCount.textContent = `${runtime.maxStep} 步`;
    }
    item.scrollIntoView({ block: "nearest", behavior: "smooth" });
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

  function completePlanning(success = true) {
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
      title.textContent = title.textContent.replace("正在规划", "已完成");
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
    const status = String(event.status || "unknown");
    ui.verification.classList.remove("success", "pending");
    if (status !== "completed") {
      ui.verification.classList.add("pending");
      ui.verificationTitle.textContent = "任务未完全完成";
      ui.verificationDetail.textContent = verifications[0] || `结束状态：${status}`;
      return;
    }
    if (event.verification_pending) {
      ui.verification.classList.add("pending");
      ui.verificationTitle.textContent = "仍需补充验证";
      ui.verificationDetail.textContent = verifications[0] || "本轮修改尚未完成充分验证。";
      return;
    }
    ui.verification.classList.add("success");
    ui.verificationTitle.textContent = verifications.length ? "验证已完成" : "任务已闭环";
    ui.verificationDetail.textContent = verifications.slice(0, 2).join(" · ") || "ForgeLoop 未报告待处理的验证项。";
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
    if (event.type === "final") {
      completePlanning(true);
      addTimelineItem({ icon: "≡", title: "已生成执行报告", detail: `状态：${event.status || "unknown"}`, status: "success" });
      ui.thinkingLabel.textContent = "正在提交会话结果…";
      return;
    }
    if (event.type === "gap") {
      addTimelineItem({ icon: "…", title: "较早记录已压缩", detail: event.message || "", status: "warning" });
      return;
    }
    if (event.type === "turn_complete") {
      completePlanning(String(event.status || "") === "completed");
      runtime.runTerminal = true;
      const status = String(event.status || "completed");
      const success = status === "completed";
      addTimelineItem({
        icon: success ? "✓" : "!",
        title: success ? "本轮任务已完成" : "本轮任务已结束",
        detail: `${event.steps || 0} 步 · ${(event.changed_files || []).length} 个变更文件 · ${formatDuration(event.duration_ms || 0)}`,
        status: success ? "success" : "warning",
      });
      appendMessage("assistant", event.summary || "任务已完成。", status);
      stopElapsedClock(Number(event.duration_ms));
      updateVerification(event);
      setRunState(success ? "success" : "error", success ? "执行完成" : "需要关注");
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
          for (const line of lines) {
            if (!line.trim()) {
              continue;
            }
            try {
              handleEvent(JSON.parse(line));
            } catch (_error) {
              addTimelineItem({ icon: "!", title: "忽略了一条无效事件", status: "warning" });
            }
          }
          if (result.done) {
            if (buffer.trim()) {
              handleEvent(JSON.parse(buffer));
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
        startElapsedClock();
      }
      setBusy(true);
      streamRun(runtime.activeRunId);
    } else {
      if (runtime.elapsedTimer) {
        stopElapsedClock(Date.now() - runtime.startedAt);
      }
      runtime.activeRunId = null;
      setBusy(false);
      const conversation = Array.isArray(snapshot.conversation)
        ? snapshot.conversation
        : [];
      const latestAssistant = [...conversation]
        .reverse()
        .find((item) => item.role === "assistant");
      if (latestAssistant) {
        const status = String(latestAssistant.status || "unknown");
        settlePendingTrace(status);
        updateVerification({
          status,
          verification_pending: snapshot.verification_pending === true,
          verifications: [],
        });
        const success = status === "completed";
        setRunState(success ? "success" : "error", success ? "执行完成" : "需要关注");
      } else {
        setRunState("", runtime.turn ? "等待后续任务" : "等待任务");
      }
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
    if (
      event.key === "Tab" &&
      mobileActivity.matches &&
      document.body.classList.contains("activity-open")
    ) {
      event.preventDefault();
      ui.drawerClose.focus();
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
  mobileActivity.addEventListener("change", () => syncActivityAccessibility(false));
  syncActivityAccessibility(false);

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
