(() => {
  "use strict";

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
  const API_BASE = window.location.protocol === "file:" ? "http://127.0.0.1:4173" : "";
  const state = { runId: null, fileData: "", fileName: "", fileMime: "", result: null, noteProposal: null, noteCommitting: false, ocrCorrectionBusy: false, toastTimer: null };
  const sampleText = "LangGraph 使用 StateGraph 编排多Agent工作流。\n每个 Agent 通过统一 State 传递中间结果。\nOCR 负责提取有效文字，分类 Agent 输出主题标签，精简 Agent 合并重复片段，归档 Agent 生成可检索 Markdown。\nLangGraph 使用 StateGraph 编排多Agent工作流。";

  function toast(message) {
    const element = $("#toast");
    element.textContent = message;
    element.classList.add("show");
    clearTimeout(state.toastTimer);
    state.toastTimer = setTimeout(() => element.classList.remove("show"), 2800);
  }

  function setConnection(label, running = false) {
    const badge = $("#connectionBadge");
    badge.textContent = label;
    badge.classList.toggle("running", running);
  }

  function setImage(file, source = "file") {
    if (!file) return;
    if (!file.type?.startsWith("image/")) {
      toast("剪贴板或文件中没有可用图片");
      return;
    }
    const extension = (file.type.split("/")[1] || "png").replace("jpeg", "jpg");
    const generatedName = `screenshot-${new Date().toISOString().replace(/[:.]/g, "-")}.${extension}`;
    state.fileName = file.name || generatedName;
    state.fileMime = file.type || "image/jpeg";
    const reader = new FileReader();
    reader.onload = () => {
      state.fileData = String(reader.result || "");
      const preview = $("#imagePreview");
      preview.src = state.fileData;
      preview.hidden = false;
      $("#uploadTitle").textContent = state.fileName;
      $("#uploadHint").textContent = `${Math.max(1, Math.round(file.size / 1024))} KB · ${source === "paste" ? "已从剪贴板载入" : "已载入"}`;
      $("#imageMeta").textContent = state.fileName;
      const pasteStatus = $("#pasteStatus");
      pasteStatus.textContent = source === "paste" ? "截图已粘贴" : "图片已载入";
      pasteStatus.hidden = false;
      if (source === "paste") toast("截图已粘贴，可以开始整理");
    };
    reader.onerror = () => toast("图片读取失败，请重新选择或粘贴");
    reader.readAsDataURL(file);
  }

  function pastedImage(event) {
    const items = Array.from(event.clipboardData?.items || []);
    const imageItem = items.find((item) => item.kind === "file" && item.type.startsWith("image/"));
    if (imageItem) return imageItem.getAsFile();
    return Array.from(event.clipboardData?.files || []).find((file) => file.type.startsWith("image/")) || null;
  }

  function renderStages(result) {
    const stages = result?.stages || {};
    const names = { retrieve: "关键词公开检索", ocr: "OCR & 内容提取", classify: "主题归类打标", summarize: "信息压缩去重", archive: "生成归档草稿" };
    $$("[data-stage]").forEach((row) => {
      const key = row.dataset.stage;
      const value = stages[key];
      row.hidden = key === "retrieve" && !value;
      if (!value) return;
      row.classList.remove("queued", "running", "done", "failed");
      const status = value.status === "completed" ? "完成" : value.status === "failed" ? "失败" : value.status === "running" ? "处理中" : "等待";
      row.classList.add(value.status === "completed" ? "done" : value.status);
      $(".stage-status", row).textContent = status;
      const retry = $(".retry-button", row);
      retry.hidden = value.status !== "failed";
      retry.dataset.retry = key;
      row.title = value.status === "failed" ? (result.errors || []).join(" ") : names[key];
    });
    $("#progressLabel").textContent = `${result.progress || 0}% · ${result.status === "complete" ? "待确认" : "处理中"}`;
  }

  function renderEvents(events = []) {
    const list = $("#eventList");
    $("#eventCount").textContent = `${events.length} 个事件`;
    list.replaceChildren();
    if (!events.length) { list.innerHTML = '<span class="muted">提交后显示每个阶段的开始、完成和失败事件。</span>'; return; }
    events.slice(-12).forEach((event) => {
      const row = document.createElement("div");
      row.className = "event-row";
      const icon = document.createElement("span");
      icon.className = `event-icon ${event.type.includes("failed") ? "failed" : event.type.includes("completed") || event.type.includes("run.completed") ? "done" : ""}`;
      const text = document.createElement("span");
      const stageName = { retrieve: "公开检索", ocr: "OCR", classify: "主题打标", summarize: "信息精简", archive: "归档草稿" }[event.stage] || "运行";
      const eventLabel = event.type === "run.created"
        ? "创建运行"
        : event.type === "run.completed"
          ? "运行完成"
          : event.type === "ocr.corrected"
            ? "OCR · 人工校正"
            : `${stageName} · ${event.type.replace("stage.", "")}`;
      text.textContent = `${eventLabel} ${event.attempt ? `（第 ${event.attempt} 次）` : ""}`;
      const time = document.createElement("time");
      time.textContent = event.timestamp ? new Date(event.timestamp).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "";
      row.append(icon, text, time);
      list.append(row);
    });
  }

  function renderResult(result) {
    state.result = result;
    state.runId = result.run_id;
    state.noteProposal = null;
    state.noteCommitting = false;
    $("#runIdLabel").textContent = result.run_id;
    $("#emptyState").hidden = true;
    $("#resultContent").hidden = false;
    const archiveMarkdown = result.archive_markdown || result.outputs?.archive?.["归档Markdown"] || "";
    $("#downloadBtn").disabled = !archiveMarkdown;
    $("#saveProposalBtn").disabled = !archiveMarkdown;
    $("#saveProposalBtn span").textContent = "生成保存提案";
    $("#noteProposalPanel").hidden = true;
    $("#noteProposalCommit").disabled = false;
    $("#noteProposalCommit span").textContent = "确认写入收件箱";
    const output = result.outputs || {};
    const ocr = output.ocr || {};
    const ocrState = result.state || {};
    const label = output.classify || {};
    const summary = output.summarize || {};
    const research = output.retrieve || null;
    $("#textType").textContent = ocr["文本类型"] || "-";
    $("#category").textContent = label["一级分类"] || "-";
    $("#resultProgress").textContent = `${result.progress || 0}%`;
    $("#markdownPreview").textContent = archiveMarkdown;
    const originalOcr = ocr["原始OCR文本"] || ocrState.original_ocr_text || ocrState.ocr_source_text || ocr["原始提取文本"] || "";
    const correctedOcr = ocr["校正后文本"] || ocrState.corrected_ocr_text || "";
    const effectiveOcr = ocr["当前有效文本"] || ocrState.effective_ocr_text || ocr["原始提取文本"] || "";
    $("#ocrPreview").textContent = originalOcr;
    $("#ocrCorrectedText").value = correctedOcr || effectiveOcr;
    const revision = Number(result.ocr_revision ?? ocrState.ocr_revision ?? 0);
    $("#ocrRevisionLabel").textContent = revision ? `修订版本 ${revision}` : "尚未提交校正";
    const engine = ocr["OCR引擎"] || ocrState.ocr_engine || "本地识别";
    $("#ocrEngineLabel").textContent = engine;
    $("#noiseLabel").textContent = ocr["噪声内容"] && ocr["噪声内容"] !== "无" ? `已过滤：${ocr["噪声内容"]}` : "未发现明显噪声";
    renderLowConfidence(ocr["低置信度片段"] || ocrState.low_confidence_segments || []);
    const correctionButton = $("#applyOcrCorrectionBtn");
    correctionButton.disabled = !effectiveOcr || state.ocrCorrectionBusy;
    $("#categoryDetail").textContent = label["一级分类"] || "-";
    $("#topicSummary").textContent = label["一句话主题概括"] || "-";
    $("#usageScene").textContent = label["适用场景"] || "-";
    const tags = $("#tagList");
    tags.replaceChildren(...(label["二级标签"] || []).map((tag) => { const item = document.createElement("span"); item.className = "tag"; item.textContent = tag; return item; }));
    $("#summaryPreview").textContent = summary["精简版要点文本"] || "";
    $("#redundancyLabel").textContent = summary["冗余内容"] && summary["冗余内容"] !== "无" ? "已合并重复内容" : "无冗余删除";
    const keyList = $("#keySentenceList");
    keyList.replaceChildren(...(summary["重点短句"] || []).map((line) => { const item = document.createElement("li"); item.textContent = line; return item; }));
    renderResearch(research);
    renderStages(result);
    renderEvents(result.events);
  }

  function renderLowConfidence(segments) {
    const list = $("#lowConfidenceList");
    list.replaceChildren();
    const values = Array.isArray(segments) ? segments.filter((item) => String(item).trim()) : [];
    if (!values.length) {
      const empty = document.createElement("span");
      empty.className = "muted";
      empty.textContent = "未发现或未返回低置信度片段";
      list.append(empty);
      return;
    }
    values.forEach((segment) => {
      const item = document.createElement("span");
      item.className = "confidence-item";
      item.textContent = String(segment);
      list.append(item);
    });
  }

  function proposalTitle() {
    return String(state.result?.outputs?.archive?.["标题"] || state.result?.title || state.fileName || "NoteFlow 整理").trim();
  }

  function proposalSourceLabel() {
    return String(state.fileName || $("#userNote").value.trim() || "NoteFlow 整理").trim();
  }

  async function responsePayload(response) {
    const text = await response.text();
    if (!text) return {};
    try { return JSON.parse(text); } catch (_) { return { message: text }; }
  }

  function renderNoteProposal(payload) {
    const proposal = payload?.proposal || {};
    const note = payload?.note || {};
    const markdown = String(proposal.markdown || payload?.archive_markdown || payload?.approved_markdown || state.result?.archive_markdown || "");
    state.noteProposal = {
      ...payload,
      markdown,
      note,
      title: String(note.title || proposalTitle()),
      sourceLabel: proposalSourceLabel(),
      noteName: String(note.name || ""),
      indexSha256: String(proposal.index_sha256 || ""),
      logSha256: String(proposal.log_sha256 || ""),
    };
    const alreadyCommitted = payload?.status === "committed" || proposal.already_committed === true || payload?.idempotent === true;
    const panel = $("#noteProposalPanel");
    panel.classList.toggle("is-committed", alreadyCommitted);
    $("#noteProposalStatus").textContent = alreadyCommitted ? "已写入" : "待确认";
    $("#noteProposalStatus").classList.toggle("committed", alreadyCommitted);
    $("#noteProposalStatus").classList.remove("failed");
    const targetPath = note.path ? `目标：${note.path}` : "目标：00-收件箱";
    $("#noteProposalMeta").textContent = alreadyCommitted ? `${targetPath} · 这次运行已经写入。` : `${targetPath} · 预览不会修改知识库。`;
    $("#noteProposalPreview").textContent = markdown || "暂无可保存的 Markdown";
    $("#noteProposalCommit").disabled = alreadyCommitted || !markdown;
    $("#noteProposalCommit span").textContent = alreadyCommitted ? "已确认写入" : "确认写入收件箱";
    panel.hidden = false;
    $("#saveProposalBtn").disabled = alreadyCommitted;
    $("#saveProposalBtn span").textContent = alreadyCommitted ? "已生成提案" : "重新生成提案";
  }

  async function proposeNote() {
    const markdown = state.result?.archive_markdown || state.result?.outputs?.archive?.["归档Markdown"] || "";
    if (!state.runId || !markdown || state.noteCommitting) return;
    const button = $("#saveProposalBtn");
    button.disabled = true;
    button.querySelector("span:last-child").textContent = "生成中...";
    try {
      const response = await fetch(`${API_BASE}/api/notes/propose`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ run_id: state.runId, archive_markdown: markdown, title: proposalTitle(), source_label: proposalSourceLabel() }),
      });
      const payload = await responsePayload(response);
      if (!response.ok) throw new Error(payload.error?.message || payload.message || "保存提案生成失败");
      renderNoteProposal(payload);
      toast(payload.status === "committed" ? "这份笔记已经写入收件箱" : "保存提案已生成，请确认写入");
    } catch (error) {
      button.disabled = false;
      button.querySelector("span:last-child").textContent = "生成保存提案";
      toast(error.message || "保存提案生成失败");
    }
  }

  async function commitNoteProposal() {
    const proposal = state.noteProposal;
    if (!proposal || state.noteCommitting || !state.runId || !proposal.markdown) return;
    state.noteCommitting = true;
    const commitButton = $("#noteProposalCommit");
    commitButton.disabled = true;
    $("#noteProposalCancel").disabled = true;
    commitButton.querySelector("span:last-child").textContent = "写入中...";
    try {
      const response = await fetch(`${API_BASE}/api/notes/commit`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          run_id: state.runId,
          approved_markdown: proposal.markdown,
          title: proposal.title,
          source_label: proposal.sourceLabel,
          expected_index_sha256: proposal.indexSha256,
          expected_log_sha256: proposal.logSha256,
          note_name: proposal.noteName,
        }),
      });
      const payload = await responsePayload(response);
      if (!response.ok) throw new Error(payload.error?.message || payload.message || "写入收件箱失败");
      const committed = { ...proposal, ...payload, status: payload.status || "committed", note: payload.note || proposal.note };
      state.noteProposal = committed;
      const note = committed.note || {};
      $("#noteProposalPanel").classList.add("is-committed");
      $("#noteProposalStatus").textContent = "已写入";
      $("#noteProposalStatus").classList.add("committed");
      $("#noteProposalMeta").textContent = `已写入：${note.path || "00-收件箱"} · 目录和流水账已更新。`;
      $("#noteProposalCommit").disabled = true;
      $("#noteProposalCommit").querySelector("span:last-child").textContent = "已确认写入";
      $("#saveProposalBtn").disabled = true;
      $("#saveProposalBtn span").textContent = "已写入收件箱";
      setConnection("已写入");
      toast(payload.idempotent ? "这份笔记已写入，重复确认未新增记录" : "笔记已写入收件箱");
    } catch (error) {
      commitButton.disabled = false;
      commitButton.querySelector("span:last-child").textContent = "确认写入收件箱";
      toast(error.message || "写入收件箱失败");
    } finally {
      state.noteCommitting = false;
      $("#noteProposalCancel").disabled = false;
    }
  }

  function closeNoteProposal() {
    if (state.noteCommitting) return;
    state.noteProposal = null;
    $("#noteProposalPanel").hidden = true;
    const markdown = state.result?.archive_markdown || state.result?.outputs?.archive?.["归档Markdown"] || "";
    $("#saveProposalBtn").disabled = !markdown;
    $("#saveProposalBtn span").textContent = "生成保存提案";
  }

  function renderResearch(research) {
    const tab = $("[data-tab=\"retrieve\"]");
    const panel = $("[data-panel=\"retrieve\"]");
    const hasResearch = Boolean(research);
    tab.hidden = !hasResearch;
    panel.hidden = !hasResearch;
    if (!hasResearch) {
      if (tab.classList.contains("active")) {
        tab.classList.remove("active");
        $("[data-tab=\"archive\"]").classList.add("active");
        panel.classList.remove("active");
        $("[data-panel=\"archive\"]").classList.add("active");
      }
      $("#sourceList").replaceChildren();
      return;
    }
    const sources = Array.isArray(research["来源列表"]) ? research["来源列表"] : [];
    $("#retrievalQuery").textContent = research["检索查询"] ? `检索查询：${research["检索查询"]}` : "";
    const warning = research["提示"] && research["提示"] !== "无" ? String(research["提示"]) : "";
    const warningElement = $("#retrievalWarning");
    warningElement.textContent = warning;
    warningElement.hidden = !warning;
    $("#retrievalMeta").textContent = `${research["检索状态"] === "completed" ? "已完成" : "无可用结果"} · ${sources.length} 个来源`;
    const list = $("#sourceList");
    list.replaceChildren();
    sources.forEach((source, index) => {
      const item = document.createElement("article");
      item.className = "source-item";
      const heading = document.createElement("div");
      heading.className = "source-heading";
      const number = document.createElement("span");
      number.className = "source-number";
      number.textContent = String(index + 1).padStart(2, "0");
      const link = document.createElement("a");
      link.className = "source-title";
      link.textContent = source["标题"] || source.title || "未命名来源";
      link.href = source["URL"] || source.url || "#";
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      heading.append(number, link);
      const snippet = document.createElement("p");
      snippet.className = "source-snippet";
      snippet.textContent = source["摘要"] || source.snippet || "暂无摘要";
      const meta = document.createElement("div");
      meta.className = "source-meta";
      const sourceName = document.createElement("span");
      sourceName.textContent = source["来源"] || source.source || "公开网页";
      const retrievedAt = document.createElement("time");
      retrievedAt.textContent = source["检索时间"] || source.retrieved_at || "";
      meta.append(sourceName, retrievedAt);
      item.append(heading, snippet, meta);
      list.append(item);
    });
    $("#sourceEmpty").hidden = sources.length > 0;
  }

  async function processNote() {
    const note = $("#userNote").value.trim();
    const text = $("#ocrText").value.trim();
    const keywords = $("#keywordsInput").value.trim();
    if (!state.fileData && !note && !text && !keywords) { toast("请上传图片，或填写至少一个检索关键词"); return; }
    if (state.noteProposal) closeNoteProposal();
    const button = $("#processBtn");
    button.disabled = true;
    button.querySelector("span:last-child").textContent = "整理中...";
    setConnection("处理中", true);
    try {
      const response = await fetch(`${API_BASE}/api/notes/process`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ image_data: state.fileData, image_name: state.fileName || "note-image", image_mime: state.fileMime, user_note: note, ocr_text: text, keywords }) });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error?.message || "处理失败");
      renderResult(payload);
      setConnection(payload.status === "complete" ? "待确认" : "部分完成");
      toast(payload.status === "complete" ? "笔记整理完成，请生成保存提案" : "流程暂停，请查看失败阶段");
    } catch (error) {
      setConnection("请求失败");
      const message = API_BASE && error instanceof TypeError
        ? "无法连接本地服务，请先运行 python3 server.py --port 4173"
        : error.message || "请求失败";
      toast(message);
    } finally {
      button.disabled = false;
      button.querySelector("span:last-child").textContent = "开始整理";
    }
  }

  async function retryStage(stage) {
    if (!state.runId) return;
    try {
      const response = await fetch(`${API_BASE}/api/notes/runs/${encodeURIComponent(state.runId)}/stages/${encodeURIComponent(stage)}/retry`, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error?.message || "重试失败");
      renderResult(payload);
      setConnection(payload.status === "complete" ? "待确认" : "部分完成");
      toast(`${stage.toUpperCase()} 阶段已重试`);
    } catch (error) { toast(error.message || "重试失败"); }
  }

  async function applyOcrCorrection() {
    if (!state.runId || state.ocrCorrectionBusy) return;
    const field = $("#ocrCorrectedText");
    const correctedText = field.value.trim();
    if (!correctedText) { toast("请先填写校正后的文本"); field.focus(); return; }
    const button = $("#applyOcrCorrectionBtn");
    state.ocrCorrectionBusy = true;
    button.disabled = true;
    button.querySelector("span:last-child").textContent = "重新整理中...";
    try {
      const revision = Number(state.result?.ocr_revision ?? state.result?.state?.ocr_revision ?? 0);
      const response = await fetch(`${API_BASE}/api/notes/runs/${encodeURIComponent(state.runId)}/ocr/correct`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ corrected_text: correctedText, expected_ocr_revision: revision }),
      });
      const payload = await responsePayload(response);
      if (!response.ok) throw new Error(payload.error?.message || payload.message || "OCR 校正失败");
      renderResult(payload);
      setConnection(payload.status === "complete" ? "待确认" : "部分完成");
      toast(payload.ocr_correction?.idempotent ? "文本未变化，已保持当前结果" : "OCR 已校正，分类、摘要和归档已重新生成");
    } catch (error) {
      toast(error.message || "OCR 校正失败");
    } finally {
      state.ocrCorrectionBusy = false;
      button.disabled = !state.result?.state?.effective_ocr_text && !state.result?.outputs?.ocr?.["原始提取文本"];
      button.querySelector("span:last-child").textContent = "应用修订并重新整理";
    }
  }

  $("#imageInput").addEventListener("change", (event) => setImage(event.target.files[0]));
  const dropzone = $("#dropzone");
  ["dragenter", "dragover"].forEach((name) => dropzone.addEventListener(name, (event) => { event.preventDefault(); dropzone.classList.add("dragover"); }));
  ["dragleave", "drop"].forEach((name) => dropzone.addEventListener(name, (event) => { event.preventDefault(); dropzone.classList.remove("dragover"); }));
  dropzone.addEventListener("drop", (event) => setImage(event.dataTransfer.files[0]));
  document.addEventListener("paste", (event) => {
    const image = pastedImage(event);
    if (!image) return;
    event.preventDefault();
    dropzone.classList.add("paste-active");
    setImage(image, "paste");
    setTimeout(() => dropzone.classList.remove("paste-active"), 500);
  });
  $("#pasteShortcut").textContent = /Mac|iPhone|iPad/.test(navigator.platform) ? "⌘V" : "Ctrl+V";
  $("#sampleBtn").addEventListener("click", () => { $("#ocrText").value = sampleText; if (!$("#userNote").value) $("#userNote").value = "LangGraph 多 Agent 学习笔记"; });
  $("#processBtn").addEventListener("click", processNote);
  $("#saveProposalBtn").addEventListener("click", proposeNote);
  $("#noteProposalCommit").addEventListener("click", commitNoteProposal);
  $("#noteProposalCancel").addEventListener("click", closeNoteProposal);
  $("#applyOcrCorrectionBtn").addEventListener("click", applyOcrCorrection);
  $$(".retry-button").forEach((button) => button.addEventListener("click", () => retryStage(button.dataset.retry)));
  $$(".tab").forEach((tab) => tab.addEventListener("click", () => { $$(".tab").forEach((item) => item.classList.toggle("active", item === tab)); $$(".tab-panel").forEach((panel) => panel.classList.toggle("active", panel.dataset.panel === tab.dataset.tab)); }));
  $("#downloadBtn").addEventListener("click", () => { if (!state.result?.archive_markdown) return; const blob = new Blob([state.result.archive_markdown], { type: "text/markdown;charset=utf-8" }); const url = URL.createObjectURL(blob); const link = document.createElement("a"); link.href = url; link.download = `${(state.result.outputs?.archive?.["标题"] || "note").replace(/[\\/:*?"<>|]/g, "_")}.md`; link.click(); URL.revokeObjectURL(url); });
  window.lucide?.createIcons({ attrs: { width: 16, height: 16, "stroke-width": 1.8 } });
})();
