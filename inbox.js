(() => {
  "use strict";

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
  const API_BASE = window.location.protocol === "file:" ? "http://127.0.0.1:4173" : "";
  const state = {
    mode: "new",
    inbox: [],
    searchable: [],
    source: null,
    target: null,
    proposal: null,
    loading: false,
    proposing: false,
    committing: false,
    queryTimer: null,
    toastTimer: null,
    statementTouched: false,
  };

  function clean(value, fallback = "") {
    const result = String(value === undefined || value === null ? "" : value).trim();
    return result || fallback;
  }

  function asItems(payload) {
    if (Array.isArray(payload?.items)) return payload.items;
    if (Array.isArray(payload?.notes)) return payload.notes;
    if (Array.isArray(payload?.results)) return payload.results;
    if (Array.isArray(payload?.data?.items)) return payload.data.items;
    if (Array.isArray(payload?.data?.notes)) return payload.data.notes;
    return [];
  }

  function normalizeItem(raw, index = 0) {
    const item = raw && typeof raw === "object" ? raw : { title: String(raw || "") };
    const path = clean(item.path || item.relative_path || item.file_path || item.filePath);
    const basename = clean(item.basename || item.filename || item.file_name || (path ? path.split("/").pop() : ""));
    const title = clean(item.title || item.name || item.document_title || basename, `笔记 ${index + 1}`);
    const id = clean(item.id || item.note_id || item.noteId || path || basename || title, `note-${index + 1}`);
    const metadata = item.metadata && typeof item.metadata === "object" ? item.metadata : {};
    const tags = Array.isArray(item.tags) ? item.tags.map(String) : (Array.isArray(metadata.tags) ? metadata.tags.map(String) : []);
    return {
      ...item,
      id,
      basename,
      title,
      path,
      summary: clean(item.summary || item.excerpt || item.snippet || item.preview, "暂无摘要"),
      preview: clean(item.preview || item.excerpt || item.snippet || item.summary, ""),
      type: clean(item.type || item.note_type || metadata.type, "note"),
      status: clean(item.status || item.state || metadata.status, ""),
      modified: clean(item.modified || item.updated || item.updated_at || metadata.updated, ""),
      tags,
      metadata,
    };
  }

  function toast(message) {
    const element = $("#toast");
    element.textContent = message;
    element.classList.add("show");
    clearTimeout(state.toastTimer);
    state.toastTimer = setTimeout(() => element.classList.remove("show"), 3000);
  }

  function setListState(message = "", error = false) {
    const element = $("#listState");
    element.textContent = message;
    element.classList.toggle("error", error);
  }

  function setLoading(button, loading, label) {
    if (!button) return;
    button.disabled = loading;
    button.classList.toggle("is-loading", loading);
    const span = button.querySelector("span");
    if (span && label) span.textContent = loading ? "处理中…" : label;
  }

  async function readResponse(response) {
    const text = await response.text();
    let payload = {};
    if (text) {
      try { payload = JSON.parse(text); } catch (_) { payload = { message: text }; }
    }
    if (!response.ok) {
      const error = new Error(payload?.error?.message || payload?.message || `请求失败（${response.status}）`);
      error.status = response.status;
      error.code = payload?.error?.code || "request_failed";
      error.payload = payload;
      throw error;
    }
    return payload;
  }

  async function getJSON(path) {
    return readResponse(await fetch(`${API_BASE}${path}`, { cache: "no-store" }));
  }

  async function postJSON(path, body) {
    return readResponse(await fetch(`${API_BASE}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }));
  }

  function itemIsInbox(item) {
    return item.path && /^(?:00-收件箱)(?:\/|$)/i.test(item.path);
  }

  function itemIsSystem(item) {
    const path = item.path || "";
    const type = item.type.toLocaleLowerCase("zh-CN");
    return ["index", "log", "system", "template", "directory", "inbox"].includes(type) && !itemIsInbox(item)
      || /(^|\/)(?:\.agents|\.obsidian|99-模板|07-素材附件|tests|note_knowledge|career_copilot)(?:\/|$)/i.test(path)
      || /^(?:00-知识库说明|01-知识库目录|02-更新流水账)(?:\.md)?$/i.test(item.basename || path.split("/").pop());
  }

  function renderInboxList() {
    const list = $("#inboxList");
    list.replaceChildren();
    const query = clean($("#inboxSearch").value).toLocaleLowerCase("zh-CN");
    const rows = state.inbox.filter((item) => {
      if (!query) return true;
      return [item.title, item.path, item.summary, item.preview, item.type, item.status, ...item.tags]
        .join(" ").toLocaleLowerCase("zh-CN").includes(query);
    });
    $("#inboxCount").textContent = String(rows.length);
    $("#searchClear").hidden = !query;
    if (!rows.length) {
      const empty = document.createElement("div");
      empty.className = "list-empty";
      empty.textContent = state.inbox.length ? "没有匹配的收件箱笔记。" : "收件箱暂时为空。先在笔记整理台生成一份草稿。";
      list.append(empty);
      return;
    }
    rows.forEach((item) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = `note-item${state.source?.id === item.id ? " active" : ""}`;
      button.dataset.noteId = item.id;
      const icon = document.createElement("i");
      icon.className = "note-item-icon";
      icon.dataset.lucide = "file-text";
      icon.setAttribute("aria-hidden", "true");
      const copy = document.createElement("span");
      copy.className = "note-item-copy";
      const title = document.createElement("strong");
      title.className = "note-item-title";
      title.textContent = item.title;
      const path = document.createElement("code");
      path.className = "note-item-path";
      path.textContent = item.path || item.basename;
      const summary = document.createElement("small");
      summary.className = "note-item-summary";
      summary.textContent = item.summary;
      copy.append(title, path, summary);
      const status = document.createElement("span");
      status.className = "note-item-status";
      status.textContent = item.status || item.type;
      button.append(icon, copy, status);
      button.addEventListener("click", () => selectSource(item));
      list.append(button);
    });
    window.lucide?.createIcons({ root: list, attrs: { width: 16, height: 16, "stroke-width": 1.8 } });
  }

  function renderTargetOptions() {
    const select = $("#targetSelect");
    const previous = state.target?.path || select.value;
    select.replaceChildren();
    const targets = state.searchable
      .filter((item) => !itemIsSystem(item) && !itemIsInbox(item) && item.path && item.path.toLocaleLowerCase("zh-CN").endsWith(".md"))
      .filter((item) => item.path !== state.source?.path)
      .sort((a, b) => a.title.localeCompare(b.title, "zh-CN") || a.path.localeCompare(b.path, "zh-CN"));
    if (!targets.length) {
      const option = document.createElement("option");
      option.value = "";
      option.textContent = "没有可追加的目标笔记";
      select.append(option);
      select.disabled = true;
      state.target = null;
      return;
    }
    targets.forEach((item) => {
      const option = document.createElement("option");
      option.value = item.path;
      option.textContent = `${item.title} · ${item.path}`;
      select.append(option);
    });
    select.disabled = false;
    select.value = targets.some((item) => item.path === previous) ? previous : targets[0].path;
    state.target = targets.find((item) => item.path === select.value) || targets[0];
  }

  function updateSourceDetail() {
    const source = state.source;
    $("#sourceEmpty").hidden = Boolean(source);
    $("#sourceDetail").hidden = !source;
    $("#proposeButton").disabled = !source || (state.mode === "append" && !state.target) || state.proposing;
    $("#sourceMeta").textContent = source ? "已选择" : "未选择";
    if (!source) return;
    $("#sourceTitle").textContent = source.title;
    $("#sourcePath").textContent = source.path || source.basename;
    $("#sourceStatus").textContent = source.status || source.type || "待整理";
    $("#sourceStatus").dataset.state = source.status ? "" : "review";
    $("#sourceSummary").textContent = source.summary;
    $("#sourceType").textContent = source.type || "inbox";
    $("#sourceUpdated").textContent = source.modified || "未记录时间";
    $("#sourcePreview").textContent = source.preview || source.summary || "暂无原笔记片段";
    if (!state.statementTouched) $("#statementInput").value = source.preview || source.summary || "";
    if (!$("#newTitle").value || !state.statementTouched) $("#newTitle").value = source.title;
  }

  function selectSource(item) {
    state.source = item;
    state.proposal = null;
    state.statementTouched = false;
    $("#proposalContent").hidden = true;
    $("#proposalEmpty").hidden = false;
    $("#proposalStatus").textContent = "等待提案";
    $("#proposalStatus").dataset.state = "";
    $("#conflictNote").hidden = true;
    renderInboxList();
    updateSourceDetail();
    $("#inboxSidebar").closest(".inbox-app").classList.remove("sidebar-open");
  }

  function setMode(mode) {
    state.mode = mode === "append" ? "append" : "new";
    $$(".mode-button").forEach((button) => {
      const active = button.dataset.mode === state.mode;
      button.classList.toggle("active", active);
      button.setAttribute("aria-selected", String(active));
    });
    $("#newFields").hidden = state.mode !== "new";
    $("#appendFields").hidden = state.mode !== "append";
    updateSourceDetail();
  }

  function proposalData(payload) {
    const proposal = payload?.proposal && typeof payload.proposal === "object" ? payload.proposal : payload || {};
    const target = proposal.target || payload?.target || {};
    const source = proposal.source || payload?.source || state.source || {};
    const before = proposal.before ?? proposal.original ?? proposal.old_content ?? payload?.before ?? "";
    const after = proposal.after ?? proposal.next ?? proposal.new_content ?? proposal.markdown ?? payload?.after ?? "";
    const diff = proposal.diff ?? proposal.unified_diff ?? payload?.diff ?? "";
    const hashes = proposal.hashes || proposal.expected_hashes || payload?.hashes || payload?.expected_hashes || {};
    return {
      ...payload,
      ...proposal,
      proposal_id: clean(proposal.proposal_id || proposal.id || payload?.proposal_id || payload?.id),
      mode: clean(proposal.mode || payload?.mode || state.mode),
      source,
      target,
      before: String(before || ""),
      after: String(after || ""),
      diff: typeof diff === "string" ? diff : JSON.stringify(diff, null, 2),
      hashes,
    };
  }

  function renderProposal(payload) {
    const proposal = proposalData(payload);
    state.proposal = proposal;
    const target = proposal.target || {};
    const committed = proposal.status === "committed" || proposal.idempotent === true;
    $("#proposalEmpty").hidden = true;
    $("#proposalContent").hidden = false;
    $("#proposalStatus").textContent = committed ? "已写入" : "待确认";
    $("#proposalStatus").dataset.state = committed ? "committed" : "";
    const summary = $("#proposalSummary");
    summary.replaceChildren();
    const heading = document.createElement("strong");
    heading.textContent = proposal.mode === "append" ? "追加到已有笔记" : "新建知识点";
    const targetLine = document.createElement("span");
    targetLine.textContent = `目标：${clean(target.title || target.path, "待分配")}`;
    const path = document.createElement("code");
    path.textContent = clean(target.path || proposal.target_path, "尚未分配目标路径");
    summary.append(heading, targetLine, path);
    $("#beforeDiff").textContent = proposal.before || "（目标文件不存在，将创建新笔记）";
    $("#afterDiff").textContent = proposal.after || "（无变更内容）";
    $("#unifiedDiff").textContent = proposal.diff || "（服务端未提供统一差异）";
    $("#beforeLabel").textContent = proposal.mode === "append" ? "目标当前版本" : "不存在";
    $("#afterLabel").textContent = committed ? "已写入版本" : "待确认版本";
    $("#commitButton").disabled = committed;
    $("#proposalHint").textContent = committed ? "本次提案已完成三件套事务，重复确认不会新增流水账。" : "确认后会写入笔记、更新目录并追加一行流水账。";
    $("#conflictNote").hidden = true;
    window.lucide?.createIcons({ root: $("#proposalContent"), attrs: { width: 16, height: 16, "stroke-width": 1.8 } });
  }

  async function loadVault() {
    if (state.loading) return;
    state.loading = true;
    $("#refreshButton").classList.add("spinning");
    $("#mobileRefresh").classList.add("spinning");
    setListState("正在读取收件箱…");
    try {
      const [inboxPayload, searchPayload] = await Promise.all([
        getJSON("/api/vault/inbox"),
        getJSON("/api/vault/search?q="),
      ]);
      state.inbox = asItems(inboxPayload).map(normalizeItem);
      state.searchable = asItems(searchPayload).map(normalizeItem);
      // Some servers intentionally omit the inbox list from a general search.
      state.inbox.forEach((item) => {
        if (!state.searchable.some((candidate) => candidate.path === item.path)) state.searchable.push(item);
      });
      renderInboxList();
      renderTargetOptions();
      updateSourceDetail();
      $("#vaultStatus").textContent = "知识库已同步";
      $("#vaultStatusMeta").textContent = `${state.inbox.length} 条收件箱笔记`;
      $("#syncLabel").textContent = `刚刚同步 · ${state.searchable.length} 篇可检索笔记`;
      setListState(state.inbox.length ? "选择一条笔记开始整理" : "");
    } catch (error) {
      state.inbox = [];
      state.searchable = [];
      renderInboxList();
      renderTargetOptions();
      $("#vaultStatus").textContent = "知识库读取失败";
      $("#vaultStatusMeta").textContent = "请确认本地服务状态";
      $("#syncLabel").textContent = "同步失败";
      setListState(error.message || "无法读取收件箱", true);
    } finally {
      state.loading = false;
      $("#refreshButton").classList.remove("spinning");
      $("#mobileRefresh").classList.remove("spinning");
    }
  }

  async function createProposal() {
    if (!state.source || state.proposing) return;
    if (state.mode === "append" && !state.target) {
      toast("请先选择目标笔记");
      return;
    }
    state.proposing = true;
    setLoading($("#proposeButton"), true, "生成差异预览");
    try {
      const content = clean($("#statementInput").value);
      const targetPath = state.mode === "append" ? state.target.path : "";
      const payload = {
        source_path: state.source.path,
        source: state.source.path,
        source_id: state.source.id,
        mode: state.mode,
        target_path: targetPath,
        target: targetPath,
        title: clean($("#newTitle").value, state.source.title),
        content,
        statement: content,
      };
      const response = await postJSON("/api/vault/promotion/propose", payload);
      renderProposal(response);
      toast("提案已生成，确认前不会写入知识库");
    } catch (error) {
      toast(error.message || "生成提案失败");
    } finally {
      state.proposing = false;
      setLoading($("#proposeButton"), false, "生成差异预览");
      updateSourceDetail();
    }
  }

  function commitPayload() {
    const proposal = state.proposal || {};
    const hashes = proposal.hashes || {};
    return {
      proposal_id: proposal.proposal_id || proposal.id,
      id: proposal.proposal_id || proposal.id,
      expected_hashes: hashes,
      hashes,
      expected_source_sha256: hashes.source_sha256 || proposal.source_sha256 || "",
      expected_target_sha256: hashes.target_sha256 || proposal.target_sha256 || "",
      expected_target_dir_sha256: hashes.target_dir_sha256 || proposal.target_dir_sha256 || "",
      expected_index_sha256: hashes.index_sha256 || proposal.index_sha256 || "",
      expected_log_sha256: hashes.log_sha256 || proposal.log_sha256 || "",
    };
  }

  async function commitProposal() {
    if (!state.proposal || state.committing || state.proposal.status === "committed" || state.proposal.idempotent) return;
    state.committing = true;
    setLoading($("#commitButton"), true, "确认写入");
    $("#conflictNote").hidden = true;
    try {
      const response = await postJSON("/api/vault/promotion/commit", commitPayload());
      renderProposal(response);
      toast(response.idempotent ? "已确认过这次提案" : "已完成：笔记、目录、流水账");
      await loadVault();
    } catch (error) {
      if (error.status === 409 || error.code === "vault_changed" || error.code === "promotion_conflict") {
        const note = $("#conflictNote");
        note.hidden = false;
        $("#conflictNote span").textContent = "目标文件或目录在预览后发生变化，系统拒绝覆盖。请刷新后重新生成提案。";
        $("#proposalStatus").textContent = "需要重新预览";
        $("#proposalStatus").dataset.state = "error";
        $("#proposalHint").textContent = "未写入任何文件；重新预览会读取最新版本。";
        toast("检测到内容变化，已拒绝提交");
      } else {
        toast(error.message || "确认写入失败");
      }
    } finally {
      state.committing = false;
      setLoading($("#commitButton"), false, "确认写入");
      if (state.proposal && state.proposal.status !== "committed" && !state.proposal.idempotent) $("#commitButton").disabled = false;
    }
  }

  function setup() {
    $$(".mode-button").forEach((button) => button.addEventListener("click", () => setMode(button.dataset.mode)));
    $("#proposeButton").addEventListener("click", createProposal);
    $("#commitButton").addEventListener("click", commitProposal);
    $("#statementInput").addEventListener("input", () => { state.statementTouched = true; });
    $("#targetSelect").addEventListener("change", () => { state.target = state.searchable.find((item) => item.path === $("#targetSelect").value) || null; updateSourceDetail(); });
    $("#inboxSearch").addEventListener("input", () => {
      renderInboxList();
      clearTimeout(state.queryTimer);
      state.queryTimer = setTimeout(async () => {
        const query = clean($("#inboxSearch").value);
        if (!query) return;
        try {
          const payload = await getJSON(`/api/vault/search?q=${encodeURIComponent(query)}`);
          const matches = asItems(payload).map(normalizeItem).filter(itemIsInbox);
          if (matches.length || state.inbox.length === 0) {
            state.inbox = matches;
            renderInboxList();
          }
        } catch (_) { /* local filtering remains available when a search request fails */ }
      }, 180);
    });
    $("#searchClear").addEventListener("click", () => { $("#inboxSearch").value = ""; renderInboxList(); $("#inboxSearch").focus(); });
    $("#refreshButton").addEventListener("click", loadVault);
    $("#mobileRefresh").addEventListener("click", loadVault);
    $("#mobileListToggle").addEventListener("click", () => $("#inboxApp").classList.add("sidebar-open"));
    $("#sidebarClose").addEventListener("click", () => $("#inboxApp").classList.remove("sidebar-open"));
    $("#sidebarBackdrop").addEventListener("click", () => $("#inboxApp").classList.remove("sidebar-open"));
    window.lucide?.createIcons({ attrs: { width: 16, height: 16, "stroke-width": 1.8 } });
    loadVault();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", setup);
  else setup();
})();
