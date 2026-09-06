(() => {
  "use strict";

  const $ = (selector, root = document) => root.querySelector(selector);
  const API_BASE = window.location.protocol === "file:" ? "http://127.0.0.1:4173" : "";
  // Keep semantic colors stable across refreshes.  The fallback palette is
  // intentionally deterministic so adding a new knowledge kind does not
  // recolor every existing node.
  const KIND_COLORS = Object.freeze({
    "atomic-claim": "#60d6a2",
    method: "#72b7f0",
    decision: "#f0c166",
    lesson: "#ef8d7b",
    definition: "#bc9be8",
    question: "#e58bc5",
    evidence: "#5fc9c5",
    example: "#a8ce72",
    knowledge: "#9ca8a2",
  });
  const KIND_ALIASES = Object.freeze({
    atomicclaim: "atomic-claim",
    claim: "atomic-claim",
    conclusion: "atomic-claim",
    fact: "atomic-claim",
    "knowledge-point": "knowledge",
    knowledgepoint: "knowledge",
    point: "knowledge",
    "知识点": "knowledge",
    "结论": "atomic-claim",
    "事实": "atomic-claim",
    "方法": "method",
    "流程": "method",
    "决策": "decision",
    "教训": "lesson",
    "复盘": "lesson",
    "定义": "definition",
    "概念": "definition",
    "问题": "question",
    "疑问": "question",
    "证据": "evidence",
    "案例": "example",
    "示例": "example",
  });
  const FALLBACK_COLORS = ["#7fc7a8", "#79a9e8", "#e3a970", "#d18ab4", "#d78378", "#76c3bd", "#a4c77d", "#a591d8"];
  const LABEL_HIDE_ZOOM = 0.72;
  const DENSE_GRAPH_NODE_LIMIT = 8;
  const DENSE_GRAPH_LABEL_ZOOM = 2.05;
  const TEXT_EXTENSIONS = new Set(["md", "markdown", "txt"]);
  const BINARY_EXTENSIONS = new Set(["docx", "pdf"]);
  const MAX_FILE_BYTES = 20 * 1024 * 1024;
  const state = {
    payload: null,
    nodes: [],
    edges: [],
    sources: [],
    sourceMap: new Map(),
    knowledgeStats: {},
    category: "all",
    query: "",
    selectedId: null,
    hoveredId: null,
    zoomScale: 1,
    priorityLabelIds: new Set(),
    visibleIds: new Set(),
    matchIds: new Set(),
    colors: new Map(),
    simulation: null,
    svg: null,
    viewport: null,
    zoom: null,
    nodeSelection: null,
    linkSelection: null,
    resizeObserver: null,
    toastTimer: null,
    fitTimer: null,
    ingesting: false,
    review: null,
    reviewing: false,
    reviewFocus: null,
    sourceMutating: null,
  };

  function value(...values) {
    return values.find((item) => item !== undefined && item !== null && item !== "");
  }

  function asArray(input) {
    if (Array.isArray(input)) return input;
    return input === undefined || input === null || input === "" ? [] : [input];
  }

  function canonicalType(input) {
    return String(input || "").trim().toLocaleLowerCase("zh-CN").replace(/[\s_]+/g, "-");
  }

  function knowledgeKindKey(input) {
    const key = canonicalType(cleanText(input, "知识点"));
    return KIND_ALIASES[key] || key || "knowledge";
  }

  // Older imports used one generic `atomic-claim` kind for every point. Keep
  // that metadata intact, but derive a stable visual kind from its evidence
  // and section so legacy graphs are readable without a migration.
  function inferVisualKind(raw) {
    const item = raw && typeof raw === "object" ? raw : {};
    const explicit = value(
      item.knowledge_type,
      item.knowledgeType,
      item.point_type,
      item.pointType,
      item.kind,
      item.metadata?.knowledge_kind,
      item.metadata?.knowledgeKind,
      item.classification,
    );
    const explicitKey = knowledgeKindKey(explicit);
    const generic = !explicit || ["atomic-claim", "knowledge", "知识点"].includes(explicitKey);
    if (!generic) return explicitKey;

    const section = cleanText(value(
      item.section,
      item.source_section,
      item.sourceSection,
      item.metadata?.source_section,
      item.metadata?.sourceSection,
    ));
    const text = cleanText(value(
      item.title,
      item.name,
      item.label,
      item.claim,
      item.summary,
      item.description,
      item.content,
    ));
    const combined = `${section} ${text}`;
    if (/(来源|证据|数据|指标|验证|结果)/u.test(combined)) return "evidence";
    if (/(方法|流程|步骤|实践|操作|架构|实现|收录事务)/u.test(section) || /(通过|步骤|流程|方法|如何|使用)/u.test(text)) return "method";
    if (/(定义|概念|内容模型|术语)/u.test(section) || /(定义|指的是|是指|概念)/u.test(text)) return "definition";
    if (/(必须|需要|应当|不得|不能|只允许|默认|不进入|规则|决策)/u.test(combined)) return "decision";
    if (/(问题|疑问|痛点|挑战)/u.test(combined)) return "question";
    if (/(教训|复盘|反思|经验)/u.test(combined)) return "lesson";
    if (/(案例|示例|例如)/u.test(combined)) return "example";
    return explicitKey || "atomic-claim";
  }

  function sourceStatusKey(input) {
    const key = canonicalType(input);
    if (["archived", "archive", "已归档", "归档", "inactive", "removed", "deleted", "已移除", "hidden", "隐藏"].includes(key)) return "archived";
    if (["pending", "待处理", "待补充", "待提取"].includes(key)) return "pending";
    return key || "active";
  }

  function sourceStatusLabel(source) {
    if (source?.archived) return "已归档";
    if (source?.statusKey === "pending") return source.status === "pending" ? "待处理" : (source.status || "待处理");
    return "";
  }

  async function changeSourceStatus(source, action, trigger) {
    if (!source || state.sourceMutating) return;
    const isArchive = action === "archive";
    const actionLabel = isArchive ? "归档" : "恢复";
    state.sourceMutating = source.id;
    if (trigger) {
      trigger.disabled = true;
      trigger.setAttribute("aria-busy", "true");
    }
    try {
      const proposalResponse = await fetch(`${API_BASE}/api/knowledge/source/propose`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action, source_id: source.id }),
      });
      const proposal = await responsePayload(proposalResponse);
      if (!proposalResponse.ok) throw new Error(proposal.error?.message || `${actionLabel}来源失败`);
      if (proposal.idempotent) {
        toast(`来源已经${isArchive ? "归档" : "恢复"}`);
        await loadGraph({ quiet: true });
        return;
      }
      const pointCount = Number(proposal.impact?.knowledge_points || source.totalCount || source.pointIds?.length || 0);
      const detail = isArchive
        ? `原始附件会保留，${pointCount} 个知识点将暂时从图谱隐藏。`
        : `${pointCount} 个知识点将重新显示在图谱中。`;
      if (typeof window.confirm === "function" && !window.confirm(`${actionLabel}“${source.title}”？\n${detail}`)) return;
      const commitResponse = await fetch(`${API_BASE}/api/knowledge/source/commit`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          proposal_id: proposal.proposal_id,
          expected_hashes: proposal.hashes || proposal.expected_hashes || {},
        }),
      });
      const committed = await responsePayload(commitResponse);
      if (!commitResponse.ok) throw new Error(committed.error?.message || `${actionLabel}来源失败，数据可能已更新`);
      toast(isArchive ? "来源已归档，原始附件仍保留" : "来源已恢复");
      await loadGraph({ quiet: true });
    } catch (error) {
      toast(error.message || `${actionLabel}来源失败`);
    } finally {
      state.sourceMutating = null;
      if (trigger) {
        trigger.disabled = false;
        trigger.removeAttribute("aria-busy");
      }
    }
  }

  function booleanFlag(input) {
    if (input === true || input === 1) return true;
    return ["true", "1", "yes", "y", "archived", "已归档", "归档"].includes(canonicalType(input));
  }

  function isKnowledgePoint(node, fromPointCollection = false) {
    if (fromPointCollection) return true;
    const type = canonicalType(value(node.type, node.node_type, node.nodeType, node.entity_type, node.entityType));
    return ["knowledge-point", "knowledgepoint", "point", "知识点"].includes(type);
  }

  function isGraphVisiblePoint(node) {
    const graphStatus = canonicalType(value(
      node?.graph_status,
      node?.graphStatus,
      node?.metadata?.graph_status,
      node?.metadata?.graphStatus,
      "active",
    ));
    return !["excluded", "draft", "inactive", "hidden", "archived", "归档", "已归档"].includes(graphStatus);
  }

  function isSystemDocument(node) {
    const type = canonicalType(value(node.type, node.node_type, node.nodeType));
    if (["system", "index", "log", "inbox", "directory", "folder", "template"].includes(type)) return true;
    const path = String(value(node.path, node.file_path, node.filePath, ""));
    const id = String(value(node.id, node.title, node.name, ""));
    return /(^|\/)(\.agents|\.obsidian|tests|99-模板|00-收件箱)(\/|$)/i.test(path)
      || /(^|\/)(README|00-知识库说明|01-知识库目录|02-更新流水账)(\.md)?$/i.test(path)
      || ["README", "00-知识库说明", "01-知识库目录", "02-更新流水账", "待整理内容"].includes(id);
  }

  function cleanText(input, fallback = "") {
    const text = String(input === undefined || input === null ? "" : input).trim();
    return text || fallback;
  }

  function clippedText(input, limit = 900) {
    const text = cleanText(input).replace(/\s+/g, " ");
    return text.length > limit ? `${text.slice(0, limit - 1)}…` : text;
  }

  function stableColorForKind(pointType) {
    const key = knowledgeKindKey(pointType);
    if (KIND_COLORS[key]) return KIND_COLORS[key];
    let hash = 0;
    for (let index = 0; index < key.length; index += 1) hash = ((hash << 5) - hash + key.charCodeAt(index)) | 0;
    return FALLBACK_COLORS[Math.abs(hash) % FALLBACK_COLORS.length];
  }

  function colorFor(pointType) {
    const label = cleanText(pointType, "知识点");
    if (!state.colors.has(label)) state.colors.set(label, stableColorForKind(label));
    return state.colors.get(label);
  }

  function icon(name, className = "") {
    const element = document.createElement("i");
    element.dataset.lucide = name;
    if (className) element.className = className;
    element.setAttribute("aria-hidden", "true");
    return element;
  }

  function refreshIcons(root = document) {
    window.lucide?.createIcons({ root, attrs: { width: 16, height: 16, "stroke-width": 1.8 } });
  }

  function toast(message) {
    const element = $("#graphToast");
    element.textContent = message;
    element.classList.add("show");
    clearTimeout(state.toastTimer);
    state.toastTimer = setTimeout(() => element.classList.remove("show"), 2600);
  }

  function showGraphState(message, { loading = false } = {}) {
    const panel = $("#graphState");
    panel.replaceChildren();
    if (loading) {
      const ring = document.createElement("span");
      ring.className = "loading-ring";
      ring.setAttribute("aria-hidden", "true");
      panel.append(ring);
    }
    const label = document.createElement("strong");
    label.textContent = message;
    panel.append(label);
    panel.hidden = false;
  }

  function graphPayload(payload) {
    if (payload?.graph && typeof payload.graph === "object") return payload.graph;
    if (payload?.data?.graph && typeof payload.data.graph === "object") return payload.data.graph;
    if (payload?.data && typeof payload.data === "object" && (payload.data.nodes || payload.data.knowledge_nodes || payload.data.knowledge_points)) return payload.data;
    return payload && typeof payload === "object" ? payload : {};
  }

  function normalizeSource(raw, index = 0) {
    const source = raw && typeof raw === "object" ? raw : { title: String(raw || "") };
    const path = cleanText(value(source.path, source.file_path, source.filePath, source.relative_path, source.relativePath));
    const sourcePath = cleanText(value(source.source_path, source.sourcePath));
    const filename = cleanText(value(source.filename, source.file_name, source.fileName, path.split("/").pop()));
    const title = cleanText(value(source.title, source.name, source.document_title, source.documentTitle, filename), `来源文档 ${index + 1}`);
    const id = cleanText(value(source.id, source.source_id, source.sourceId, source.document_id, source.documentId, path, filename, title), `source-${index + 1}`);
    const rawStatus = value(source.status, source.source_status, source.sourceStatus, source.status_label, source.ingest_status, source.ingestStatus, source.metadata?.source_status, source.metadata?.sourceStatus, source.metadata?.status);
    const rawGraphStatus = value(source.graph_status, source.graphStatus, source.metadata?.graph_status, source.metadata?.graphStatus);
    const rawArchived = value(source.archived, source.is_archived, source.isArchived, source.metadata?.archived, source.metadata?.is_archived);
    const statusProvided = [rawStatus, rawGraphStatus, rawArchived].some((item) => item !== undefined && item !== null && item !== "");
    const status = cleanText(rawStatus, "已收录");
    const statusKey = sourceStatusKey(status);
    const graphStatusKey = sourceStatusKey(rawGraphStatus);
    const archived = statusKey === "archived" || graphStatusKey === "archived" || booleanFlag(rawArchived);
    return {
      id,
      title,
      filename,
      path,
      sourcePath,
      url: cleanText(value(source.url, source.href, source.source_url, source.sourceUrl)),
      mimeType: cleanText(value(source.format, source.mime_type, source.mimeType, source.media_type, source.mediaType, source.type)),
      status,
      statusKey: archived ? "archived" : statusKey,
      graphStatus: cleanText(rawGraphStatus),
      archived,
      statusProvided,
      extractionMethod: cleanText(value(source.extraction_method, source.extractionMethod)),
      modified: cleanText(value(source.modified, source.updated, source.updated_at, source.updatedAt, source.created_at, source.createdAt)),
      pointIds: asArray(value(source.knowledge_point_ids, source.knowledgePointIds, source.point_ids, source.pointIds, source.node_ids, source.nodeIds)).map(String),
      allPointIds: asArray(value(source.all_point_ids, source.allPointIds, source.all_knowledge_point_ids, source.allKnowledgePointIds)).map(String),
      totalCount: Number(value(source.total_count, source.totalCount, source.count, 0)) || 0,
      metadata: source.metadata && typeof source.metadata === "object" ? source.metadata : {},
    };
  }

  function sourceReferences(raw) {
    const references = [];
    [raw.sources, raw.source_documents, raw.sourceDocuments, raw.documents].forEach((entry) => references.push(...asArray(entry)));
    [raw.source, raw.source_document, raw.sourceDocument, raw.document, raw.provenance?.source].forEach((entry) => references.push(...asArray(entry)));
    [raw.source_ids, raw.sourceIds, raw.document_ids, raw.documentIds].forEach((entry) => references.push(...asArray(entry)));
    [raw.source_id, raw.sourceId, raw.document_id, raw.documentId, raw.metadata?.source_summary].forEach((entry) => {
      if (entry !== undefined && entry !== null && entry !== "") references.push(entry);
    });
    return references;
  }

  function evidenceText(raw) {
    const evidence = value(raw.excerpt, raw.source_excerpt, raw.sourceExcerpt, raw.quote, raw.original_text, raw.originalText, raw.snippet, raw.evidence, raw.provenance?.excerpt, raw.metadata?.source_excerpt);
    if (Array.isArray(evidence)) {
      return clippedText(evidence.map((item) => typeof item === "object" ? value(item.text, item.quote, item.excerpt, item.content, "") : item).filter(Boolean).join(" … "));
    }
    if (evidence && typeof evidence === "object") return clippedText(value(evidence.text, evidence.quote, evidence.excerpt, evidence.content, ""));
    return clippedText(evidence);
  }

  function normalizePayload(rawPayload) {
    const payload = graphPayload(rawPayload);
    const explicitSources = [
      ...asArray(payload.sources),
      ...asArray(payload.documents),
      ...asArray(payload.source_documents),
      ...asArray(payload.sourceDocuments),
    ];
    const hasExplicitSourceSchema = [payload.sources, payload.documents, payload.source_documents, payload.sourceDocuments].some(Array.isArray);
    const sourceMap = new Map();
    const sourceAliases = new Map();

    function registerSource(raw, index = sourceMap.size) {
      const source = normalizeSource(raw, index);
      const existing = sourceMap.get(source.id);
      const merged = existing ? {
        ...existing,
        ...Object.fromEntries(Object.entries(source).filter(([key, item]) => (
          item !== "" && (!Array.isArray(item) || item.length)
          && (source.statusProvided || !["status", "statusKey", "graphStatus", "archived"].includes(key))
        ))),
        pointIds: [...new Set([...existing.pointIds, ...source.pointIds])],
        allPointIds: [...new Set([...(existing.allPointIds || []), ...(source.allPointIds || [])])],
        totalCount: Math.max(existing.totalCount || 0, source.totalCount || 0, existing.pointIds.length, source.pointIds.length),
      } : source;
      merged.statusProvided = Boolean(existing?.statusProvided || source.statusProvided);
      sourceMap.set(merged.id, merged);
      [merged.id, merged.path, merged.filename, merged.title].filter(Boolean).forEach((alias) => sourceAliases.set(String(alias), merged.id));
      return merged;
    }

    explicitSources.forEach(registerSource);

    const nestedPoints = [];
    explicitSources.forEach((source, sourceIndex) => {
      const normalizedSource = normalizeSource(source, sourceIndex);
      const points = [
        ...asArray(source.knowledge_points),
        ...asArray(source.knowledgePoints),
        ...asArray(source.points),
        ...asArray(source.nodes),
      ];
      points.forEach((point) => nestedPoints.push({ point, source: normalizedSource }));
    });

    const rawNodes = asArray(payload.nodes);
    if (!hasExplicitSourceSchema) rawNodes.filter((node) => !isKnowledgePoint(node) && !isSystemDocument(node)).forEach(registerSource);

    const pointCandidates = [
      ...asArray(payload.knowledge_nodes).map((point) => ({ point, fromCollection: true })),
      ...asArray(payload.knowledgeNodes).map((point) => ({ point, fromCollection: true })),
      ...asArray(payload.knowledge_points).map((point) => ({ point, fromCollection: true })),
      ...asArray(payload.knowledgePoints).map((point) => ({ point, fromCollection: true })),
      ...asArray(payload.points).map((point) => ({ point, fromCollection: true })),
      ...nestedPoints.map(({ point, source }) => ({ point: { ...point, __nestedSource: source }, fromCollection: true })),
      ...rawNodes.map((point) => ({ point, fromCollection: false })),
    ];

    const nodesById = new Map();
    pointCandidates.forEach(({ point: raw, fromCollection }, index) => {
      if (!raw || typeof raw !== "object" || !isKnowledgePoint(raw, fromCollection) || !isGraphVisiblePoint(raw)) return;
      const declaredType = canonicalType(value(raw.type, raw.node_type, raw.nodeType));
      if (["system", "index", "log", "inbox", "directory", "folder"].includes(declaredType)) return;
      const id = cleanText(value(raw.id, raw.knowledge_point_id, raw.knowledgePointId, raw.key, raw.slug, raw.title, raw.name), `knowledge-point-${index + 1}`);
      if (nodesById.has(id)) return;

      const references = sourceReferences(raw);
      if (raw.__nestedSource) references.unshift(raw.__nestedSource);
      const sourceIds = [];
      references.forEach((reference) => {
        if (reference && typeof reference === "object") {
          const source = registerSource(reference);
          sourceIds.push(source.id);
          return;
        }
        const referenceId = cleanText(reference);
        if (!referenceId) return;
        const existingId = sourceAliases.get(referenceId);
        if (existingId) {
          sourceIds.push(existingId);
          return;
        }
        const source = registerSource({
          id: referenceId,
          title: value(raw.source_title, raw.sourceTitle, raw.document_title, raw.documentTitle, referenceId),
          path: value(raw.source_path, raw.sourcePath, raw.document_path, raw.documentPath, ""),
        });
        sourceIds.push(source.id);
      });

      const pointType = inferVisualKind(raw);
      const tags = asArray(value(raw.tags, raw.keywords, raw.labels)).map(String);
      const node = {
        id,
        title: cleanText(value(raw.title, raw.name, raw.label, raw.claim), "未命名知识点"),
        type: "knowledge-point",
        pointType,
        rawPointType: cleanText(value(raw.knowledge_type, raw.knowledgeType, raw.point_type, raw.pointType, raw.kind, raw.metadata?.knowledge_kind, raw.metadata?.knowledgeKind, raw.classification), "知识点"),
        summary: clippedText(value(raw.summary, raw.description, raw.explanation, raw.content), 560) || "暂无摘要。",
        excerpt: evidenceText(raw),
        excerptLabel: cleanText(value(raw.location, raw.page, raw.section, raw.provenance?.location, raw.metadata?.source_section), "引用"),
        sourceIds: [...new Set(sourceIds)],
        sourceTitles: [],
        path: cleanText(value(raw.path, raw.source_path, raw.sourcePath)),
        confidence: value(raw.confidence, raw.score, raw.metadata?.confidence, "-"),
        status: cleanText(value(raw.status, raw.metadata?.status), "待核验"),
        modified: cleanText(value(raw.modified, raw.updated, raw.updated_at, raw.updatedAt, raw.metadata?.updated)),
        tags,
        metadata: raw.metadata && typeof raw.metadata === "object" ? raw.metadata : {},
        relatedIds: asArray(value(raw.related_ids, raw.relatedIds, raw.related_knowledge_point_ids, raw.relatedKnowledgePointIds)).map(String),
        degree: 0,
      };
      nodesById.set(id, node);
    });

    sourceMap.forEach((source) => {
      source.pointIds.forEach((pointId) => {
        const node = nodesById.get(pointId);
        if (node && !node.sourceIds.includes(source.id)) node.sourceIds.push(source.id);
      });
    });

    const nodes = [...nodesById.values()];
    if (nodes.some((node) => !node.sourceIds.length)) registerSource({ id: "unassigned-source", title: "未标注来源", status: "待补充" });
    nodes.forEach((node) => {
      if (!node.sourceIds.length) node.sourceIds = ["unassigned-source"];
      node.sourceTitles = node.sourceIds.map((id) => sourceMap.get(id)?.title).filter(Boolean);
      if (!node.path) node.path = node.sourceIds.map((id) => sourceMap.get(id)?.path).find(Boolean) || "";
    });

    const nodeIds = new Set(nodes.map((node) => node.id));
    const edgeCandidates = [
      ...asArray(payload.knowledge_edges),
      ...asArray(payload.knowledgeEdges),
      ...asArray(payload.edges),
      ...asArray(payload.links),
      ...asArray(payload.relationships),
    ];
    nodes.forEach((node) => node.relatedIds.forEach((target) => edgeCandidates.push({ source: node.id, target })));
    const seenEdges = new Set();
    const edges = edgeCandidates.map((edge) => {
      const source = cleanText(typeof edge.source === "object" ? value(edge.source.id, edge.source.key) : value(edge.source, edge.from, edge.source_id, edge.sourceId));
      const target = cleanText(typeof edge.target === "object" ? value(edge.target.id, edge.target.key) : value(edge.target, edge.to, edge.target_id, edge.targetId));
      return { source, target, relation: cleanText(value(edge.relation, edge.type, edge.label)) };
    }).filter((edge) => {
      if (!nodeIds.has(edge.source) || !nodeIds.has(edge.target) || edge.source === edge.target) return false;
      const key = [edge.source, edge.target].sort().join("\u0000");
      if (seenEdges.has(key)) return false;
      seenEdges.add(key);
      return true;
    });

    const degree = new Map(nodes.map((node) => [node.id, 0]));
    edges.forEach((edge) => {
      degree.set(edge.source, (degree.get(edge.source) || 0) + 1);
      degree.set(edge.target, (degree.get(edge.target) || 0) + 1);
    });
    nodes.forEach((node) => { node.degree = degree.get(node.id) || 0; });

    const referencedSources = new Set(nodes.flatMap((node) => node.sourceIds));
    const sources = [...sourceMap.values()]
      .filter((source) => !hasExplicitSourceSchema || explicitSources.length > 0 || referencedSources.has(source.id) || source.pointIds.length > 0)
      .sort((a, b) => a.title.localeCompare(b.title, "zh-CN"));
    return { ...payload, nodes, edges, sources, knowledgeStats: payload.knowledge_stats || payload.knowledgeStats || {} };
  }

  async function loadGraph({ quiet = false } = {}) {
    if (!window.d3) {
      showGraphState("图谱组件加载失败，请刷新页面");
      return;
    }
    if (!quiet) showGraphState("正在读取知识点", { loading: true });
    $("#refreshGraph").classList.add("spinning");
    $("#mobileRefresh").classList.add("spinning");
    try {
      const response = await fetch(`${API_BASE}/api/knowledge/graph`, { cache: "no-store" });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error?.message || "无法读取知识库");
      state.payload = normalizePayload(payload);
      state.nodes = state.payload.nodes;
      state.edges = state.payload.edges;
      state.sources = state.payload.sources;
      state.sourceMap = new Map(state.sources.map((source) => [source.id, source]));
      state.knowledgeStats = state.payload.knowledgeStats;
      state.colors.clear();
      [...new Set(state.nodes.map((node) => node.pointType))].sort((a, b) => a.localeCompare(b, "zh-CN")).forEach(colorFor);
      renderCategoryFilter();
      renderLegend();
      if (state.selectedId && !state.nodes.some((node) => node.id === state.selectedId)) closeInspector();
      applyFilters({ fit: true });
      $("#vaultStatus").textContent = "知识库已同步";
      $("#vaultStatusMeta").textContent = `${state.sources.length} 份来源 · ${state.nodes.length} 个知识点`;
      if (quiet) toast("知识点图谱已刷新");
    } catch (error) {
      showGraphState(error.message || "知识库读取失败");
      $("#vaultStatus").textContent = "知识库读取失败";
      $("#vaultStatusMeta").textContent = "请确认本地服务状态";
    } finally {
      $("#refreshGraph").classList.remove("spinning");
      $("#mobileRefresh").classList.remove("spinning");
    }
  }

  function updateStats(visibleNodes = state.nodes, visibleEdges = state.edges) {
    $("#nodeCount").textContent = String(visibleNodes.length);
    $("#edgeCount").textContent = String(visibleEdges.length);
    $("#sourceCount").textContent = String(value(state.knowledgeStats.sources, state.sources.length));
    $("#treeCount").textContent = `${state.sources.length} 份`;
  }

  function renderCategoryFilter() {
    const select = $("#categoryFilter");
    const previous = state.category;
    select.replaceChildren();
    const all = document.createElement("option");
    all.value = "all";
    all.textContent = "全部类型";
    select.append(all);
    const types = [...new Set(state.nodes.map((node) => node.pointType))].sort((a, b) => a.localeCompare(b, "zh-CN"));
    types.forEach((pointType) => {
      const option = document.createElement("option");
      option.value = pointType;
      option.textContent = pointType;
      select.append(option);
    });
    state.category = types.includes(previous) ? previous : "all";
    select.value = state.category;
    select.disabled = types.length === 0;
  }

  function sourcePoints(source) {
    return state.nodes.filter((node) => node.sourceIds.includes(source.id));
  }

  function sourceMatches(source, query) {
    return [source.title, source.filename, source.path, source.mimeType, source.status, source.statusKey, sourceStatusLabel(source)].join(" ").toLocaleLowerCase("zh-CN").includes(query);
  }

  function renderTree() {
    const tree = $("#vaultTree");
    tree.replaceChildren();
    const query = state.query.trim().toLocaleLowerCase("zh-CN");
    const groups = state.sources.map((source) => {
      const allPoints = sourcePoints(source);
      const sourceIsMatch = Boolean(query && sourceMatches(source, query));
      const points = allPoints.filter((node) => (state.category === "all" || node.pointType === state.category) && (!query || sourceIsMatch || matchesQuery(node, query)));
      return { source, allPoints, points, sourceIsMatch };
    }).filter(({ allPoints, points, sourceIsMatch }) => {
      if (!query && state.category === "all") return true;
      if (query && sourceIsMatch && state.category === "all") return true;
      return points.length > 0 || (allPoints.length === 0 && sourceIsMatch);
    });

    if (!groups.length) {
      const empty = document.createElement("div");
      empty.className = "tree-empty";
      empty.textContent = state.sources.length ? "没有匹配的来源文档或知识点。" : "暂无来源文档。导入工作材料后会显示在这里。";
      tree.append(empty);
      return;
    }

    groups.forEach(({ source, allPoints, points }, index) => {
      const details = document.createElement("details");
      details.open = Boolean(query) || index < 4 || groups.length <= 6;
      const summary = document.createElement("summary");
      summary.dataset.sourceStatus = source.statusKey;
      summary.classList.toggle("is-archived", source.archived);
      const sourceColor = colorFor((allPoints[0] || points[0])?.pointType || "待提取");
      summary.style.setProperty("--source-color", sourceColor);
      summary.append(icon("chevron-right", "folder-chevron"), icon("file-text", "source-icon"));
      const label = document.createElement("span");
      label.className = "source-name";
      label.textContent = source.title;
      label.title = source.title;
      const count = document.createElement("span");
      count.className = "folder-count";
      const sourceCount = Math.max(allPoints.length, source.totalCount || 0, source.allPointIds?.length || 0);
      count.textContent = String(sourceCount);
      count.title = source.archived && sourceCount !== allPoints.length
        ? `${sourceCount} 个知识点（已归档）`
        : `${sourceCount} 个知识点`;
      const status = document.createElement("span");
      status.className = "source-status";
      const statusLabel = sourceStatusLabel(source);
      if (statusLabel) {
        status.textContent = statusLabel;
        status.title = source.status || statusLabel;
        status.setAttribute("aria-label", status.title);
      } else {
        status.hidden = true;
      }
      const sourceAction = document.createElement("button");
      sourceAction.type = "button";
      sourceAction.className = "source-action";
      const sourceActionLabel = source.archived ? "恢复来源" : "归档来源";
      sourceAction.setAttribute("aria-label", sourceActionLabel);
      sourceAction.dataset.tooltip = sourceActionLabel;
      sourceAction.append(icon(source.archived ? "rotate-ccw" : "archive"));
      sourceAction.addEventListener("click", (event) => {
        event.preventDefault();
        event.stopPropagation();
        changeSourceStatus(source, source.archived ? "restore" : "archive", sourceAction);
      });
      summary.append(label, status, count, sourceAction);
      const list = document.createElement("div");
      list.className = "tree-notes";
      points.sort((a, b) => a.title.localeCompare(b.title, "zh-CN")).forEach((node) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "tree-note";
        button.dataset.nodeId = node.id;
        button.style.setProperty("--point-color", colorFor(node.pointType));
        const dot = document.createElement("span");
        dot.className = "point-dot";
        const title = document.createElement("span");
        title.className = "point-title";
        title.textContent = node.title;
        const kind = document.createElement("span");
        kind.className = "point-kind";
        kind.textContent = node.pointType;
        button.append(dot, title, kind);
        button.addEventListener("click", () => revealNode(node.id));
        list.append(button);
      });
      if (!points.length) {
        const empty = document.createElement("div");
        empty.className = "tree-note-empty";
        empty.textContent = allPoints.length
          ? "当前筛选下无知识点"
          : (source.archived && sourceCount ? "知识点已归档" : "尚未提取知识点");
        list.append(empty);
      }
      details.append(summary, list);
      tree.append(details);
    });
    refreshIcons(tree);
  }

  function renderLegend() {
    const legend = $("#graphLegend");
    legend.replaceChildren();
    state.colors.forEach((color, pointType) => {
      const item = document.createElement("span");
      item.className = "legend-item";
      item.style.setProperty("--category-color", color);
      const dot = document.createElement("span");
      dot.className = "legend-dot";
      const label = document.createElement("span");
      label.textContent = pointType;
      item.append(dot, label);
      legend.append(item);
    });
    legend.hidden = state.colors.size === 0;
  }

  function matchesQuery(node, query) {
    if (!query) return true;
    return [node.title, node.summary, node.excerpt, node.pointType, node.path, ...node.tags, ...node.sourceTitles]
      .join(" ")
      .toLocaleLowerCase("zh-CN")
      .includes(query);
  }

  function endpointId(endpoint) {
    return typeof endpoint === "object" ? endpoint.id : endpoint;
  }

  function updateLabelVisibility(scale = state.zoomScale) {
    const numericScale = Number(scale);
    const visibleCount = state.visibleIds.size || state.nodes.length;
    const denseGraph = visibleCount > DENSE_GRAPH_NODE_LIMIT;
    const compact = Number.isFinite(numericScale) && (
      numericScale < LABEL_HIDE_ZOOM
      || (denseGraph && numericScale < DENSE_GRAPH_LABEL_ZOOM)
    );
    const showPriority = denseGraph && Number.isFinite(numericScale) && numericScale >= 1.1;
    state.zoomScale = Number.isFinite(numericScale) ? numericScale : 1;
    if (state.viewport) {
      state.viewport
        .classed("labels-hidden", compact)
        .attr("data-label-mode", compact ? "compact" : "full");
    }
    if (state.nodeSelection) {
      state.nodeSelection
        .classed("is-hovered", (node) => node.id === state.hoveredId)
        .classed("is-label-visible", (node) => (
          !compact
          || node.id === state.selectedId
          || node.id === state.hoveredId
          || (showPriority && state.priorityLabelIds.has(node.id))
        ));
    }
  }

  function applyFilters({ fit = false } = {}) {
    const query = state.query.trim().toLocaleLowerCase("zh-CN");
    const base = state.nodes.filter((node) => (state.category === "all" || node.pointType === state.category) && matchesQuery(node, query));
    state.matchIds = new Set(base.map((node) => node.id));
    const visibleIds = new Set(state.matchIds);
    if (query || state.category !== "all") {
      state.edges.forEach((edge) => {
        if (state.matchIds.has(endpointId(edge.source)) || state.matchIds.has(endpointId(edge.target))) {
          visibleIds.add(endpointId(edge.source));
          visibleIds.add(endpointId(edge.target));
        }
      });
    } else {
      state.nodes.forEach((node) => visibleIds.add(node.id));
    }
    state.visibleIds = visibleIds;
    const nodes = state.nodes.filter((node) => visibleIds.has(node.id)).map((node) => ({ ...node }));
    const edges = state.edges.filter((edge) => visibleIds.has(endpointId(edge.source)) && visibleIds.has(endpointId(edge.target))).map((edge) => ({ ...edge, source: endpointId(edge.source), target: endpointId(edge.target) }));
    updateStats(nodes, edges);
    renderTree();
    if (!nodes.length) {
      state.simulation?.stop();
      window.d3.select("#knowledgeGraph").selectAll("g.graph-viewport").remove();
      const message = state.nodes.length
        ? "没有匹配的知识点"
        : state.sources.length ? "来源文档尚未提取出知识点" : "导入一份工作文档，开始构建知识网络";
      showGraphState(message);
      closeInspector();
      return;
    }
    $("#graphState").hidden = true;
    renderGraph(nodes, edges, { fit });
  }

  function deterministicOffset(input, span) {
    const text = String(input);
    let hash = 0;
    for (let index = 0; index < text.length; index += 1) hash = ((hash << 5) - hash + text.charCodeAt(index)) | 0;
    return ((Math.abs(hash) % 1000) / 999 - 0.5) * span;
  }

  function renderGraph(nodes, edges, { fit = false } = {}) {
    const d3 = window.d3;
    const stage = $("#graphStage");
    const width = Math.max(320, stage.clientWidth);
    const height = Math.max(320, stage.clientHeight);
    const svg = d3.select("#knowledgeGraph");
    svg.attr("viewBox", `0 0 ${width} ${height}`);
    svg.selectAll("g.graph-viewport").remove();
    state.simulation?.stop();

    const sourceIds = [...new Set(nodes.map((node) => node.sourceIds[0] || "unassigned-source"))];
    const priorityCount = Math.min(5, Math.max(3, Math.ceil(Math.sqrt(nodes.length))));
    state.priorityLabelIds = new Set(
      [...nodes]
        .sort((a, b) => b.degree - a.degree || a.title.localeCompare(b.title, "zh-CN"))
        .slice(0, priorityCount)
        .map((node) => node.id),
    );
    nodes.forEach((node) => {
      const sourceIndex = Math.max(0, sourceIds.indexOf(node.sourceIds[0] || "unassigned-source"));
      const angle = (sourceIndex / Math.max(1, sourceIds.length)) * Math.PI * 2;
      const radius = sourceIds.length === 1 ? 0 : Math.min(width, height) * 0.25;
      node.x = width / 2 + Math.cos(angle) * radius + deterministicOffset(node.id, 86);
      node.y = height / 2 + Math.sin(angle) * radius + deterministicOffset(`${node.id}-y`, 86);
    });

    const viewport = svg.append("g").attr("class", "graph-viewport");
    const linkLayer = viewport.append("g").attr("aria-hidden", "true");
    const nodeLayer = viewport.append("g").attr("aria-hidden", "true");
    const linkSelection = linkLayer.selectAll("line").data(edges).join("line").attr("class", "graph-link");
    const nodeSelection = nodeLayer.selectAll("g").data(nodes, (node) => node.id).join("g")
      .attr("class", (node) => `graph-node${state.matchIds.has(node.id) ? " is-match" : " is-context"}`)
      .style("--node-color", (node) => colorFor(node.pointType));
    nodeSelection.append("circle").attr("r", (node) => Math.min(17, 8 + Math.sqrt(node.degree + 1) * 2));
    nodeSelection.append("text")
      .attr("class", "node-label")
      .attr("dy", (node) => Math.min(17, 8 + Math.sqrt(node.degree + 1) * 2) + 15)
      .text((node) => node.title.length > 14 ? `${node.title.slice(0, 13)}…` : node.title);
    nodeSelection.append("title").text((node) => node.title);

    const zoom = d3.zoom().scaleExtent([0.22, 4]).on("zoom", (event) => {
      viewport.attr("transform", event.transform);
      state.zoomScale = event.transform.k;
      updateLabelVisibility(event.transform.k);
    });
    svg.call(zoom).on("dblclick.zoom", null);
    svg.on("click", () => closeInspector());

    const simulation = d3.forceSimulation(nodes)
      .force("link", d3.forceLink(edges).id((node) => node.id).distance((edge) => {
        const sharedSource = edge.source.sourceIds.some((id) => edge.target.sourceIds.includes(id));
        return sharedSource ? 84 : 122;
      }).strength(0.44))
      .force("charge", d3.forceManyBody().strength((node) => -205 - node.degree * 17))
      .force("collision", d3.forceCollide().radius((node) => Math.min(17, 8 + Math.sqrt(node.degree + 1) * 2) + 22).strength(0.94))
      .force("center", d3.forceCenter(width / 2, height / 2))
      .force("x", d3.forceX(width / 2).strength(0.022))
      .force("y", d3.forceY(height / 2).strength(0.022))
      .alpha(0.86)
      .alphaDecay(0.035);

    if (fit) simulation.on("end.fit", () => fitGraph(false));

    const drag = d3.drag()
      .on("start", (event, node) => {
        if (!event.active) simulation.alphaTarget(0.18).restart();
        node.fx = node.x;
        node.fy = node.y;
      })
      .on("drag", (event, node) => {
        node.fx = event.x;
        node.fy = event.y;
      })
      .on("end", (event, node) => {
        if (!event.active) simulation.alphaTarget(0);
        node.fx = null;
        node.fy = null;
      });
    nodeSelection.call(drag)
      .on("click", (event, node) => {
        event.stopPropagation();
        selectNode(node.id);
      })
      .on("mouseenter", (event, node) => {
        state.hoveredId = node.id;
        updateLabelVisibility();
        showTooltip(event, node);
      })
      .on("mousemove", (event, node) => showTooltip(event, node))
      .on("mouseleave", (event, node) => {
        if (state.hoveredId === node.id) state.hoveredId = null;
        updateLabelVisibility();
        hideTooltip(event);
      });

    simulation.on("tick", () => {
      linkSelection
        .attr("x1", (edge) => edge.source.x)
        .attr("y1", (edge) => edge.source.y)
        .attr("x2", (edge) => edge.target.x)
        .attr("y2", (edge) => edge.target.y);
      nodeSelection.attr("transform", (node) => `translate(${node.x},${node.y})`);
    });

    state.svg = svg;
    state.viewport = viewport;
    state.zoom = zoom;
    state.simulation = simulation;
    state.nodeSelection = nodeSelection;
    state.linkSelection = linkSelection;
    state.zoomScale = 1;
    updateLabelVisibility(1);
    if (state.selectedId && state.visibleIds.has(state.selectedId)) updateHighlight();

    clearTimeout(state.fitTimer);
    if (fit) state.fitTimer = setTimeout(() => fitGraph(false), 140);
    if (!state.resizeObserver) {
      state.resizeObserver = new ResizeObserver(() => {
        if (!state.simulation || !state.svg) return;
        const nextWidth = Math.max(320, stage.clientWidth);
        const nextHeight = Math.max(320, stage.clientHeight);
        state.svg.attr("viewBox", `0 0 ${nextWidth} ${nextHeight}`);
        state.simulation.force("center", d3.forceCenter(nextWidth / 2, nextHeight / 2));
        state.simulation.force("x", d3.forceX(nextWidth / 2).strength(0.022));
        state.simulation.force("y", d3.forceY(nextHeight / 2).strength(0.022));
        state.simulation.alpha(0.2).restart();
      });
      state.resizeObserver.observe(stage);
    }
  }

  function fitGraph(animate = true) {
    if (!state.svg || !state.viewport || !state.zoom || !state.nodeSelection?.size()) return;
    const stage = $("#graphStage");
    const width = Math.max(320, stage.clientWidth);
    const height = Math.max(320, stage.clientHeight);
    const bounds = state.viewport.node().getBBox();
    if (!bounds.width || !bounds.height) return;
    const padding = 72;
    const minScale = width < 360 ? 0.62 : width < 560 ? 0.72 : 0.22;
    const maxScale = width < 560 ? 1.55 : 1.85;
    const scale = Math.max(minScale, Math.min(maxScale, 0.9 / Math.max(bounds.width / Math.max(1, width - padding * 2), bounds.height / Math.max(1, height - padding * 2))));
    const x = width / 2 - scale * (bounds.x + bounds.width / 2);
    const y = height / 2 - scale * (bounds.y + bounds.height / 2);
    const target = animate ? state.svg.transition().duration(240) : state.svg;
    target.call(state.zoom.transform, window.d3.zoomIdentity.translate(x, y).scale(scale));
  }

  function zoomBy(factor) {
    if (!state.svg || !state.zoom) return;
    state.svg.transition().duration(160).call(state.zoom.scaleBy, factor);
  }

  function showTooltip(event, node) {
    const tooltip = $("#graphTooltip");
    const stageRect = $("#graphStage").getBoundingClientRect();
    const left = Math.min(stageRect.width - 270, Math.max(10, event.clientX - stageRect.left + 12));
    const top = Math.min(stageRect.height - 70, Math.max(10, event.clientY - stageRect.top + 12));
    tooltip.replaceChildren();
    const title = document.createElement("strong");
    title.textContent = node.title;
    const meta = document.createElement("span");
    const tooltipSource = node.sourceIds.map((sourceId) => state.sourceMap.get(sourceId)).find(Boolean);
    const sourceLabel = tooltipSource?.archived ? `${tooltipSource.title} · 已归档` : (tooltipSource?.title || node.sourceTitles[0] || "未标注来源");
    meta.textContent = `${node.pointType} · ${sourceLabel} · ${node.degree} 条关联`;
    tooltip.append(title, meta);
    tooltip.style.left = `${left}px`;
    tooltip.style.top = `${top}px`;
    tooltip.hidden = false;
  }

  function hideTooltip() {
    $("#graphTooltip").hidden = true;
  }

  function relatedNodeIds(id) {
    const ids = new Set();
    state.edges.forEach((edge) => {
      const source = endpointId(edge.source);
      const target = endpointId(edge.target);
      if (source === id) ids.add(target);
      if (target === id) ids.add(source);
    });
    return ids;
  }

  function confidenceLabel(input) {
    if (input === undefined || input === null || input === "") return "-";
    const number = Number(input);
    if (!Number.isFinite(number)) return String(input);
    if (number >= 0 && number <= 1) return `${Math.round(number * 100)}%`;
    if (number > 1 && number <= 100) return `${Math.round(number)}%`;
    return String(input);
  }

  function localFileHref(path) {
    if (!path) return "";
    if (/^https?:\/\//i.test(path)) return path;
    return `${API_BASE}/${path.split("/").filter(Boolean).map(encodeURIComponent).join("/")}`;
  }

  function selectNode(id) {
    const node = state.nodes.find((item) => item.id === id);
    if (!node) return;
    state.selectedId = id;
    $("#graphApp").classList.add("inspector-open");
    $("#noteInspector").setAttribute("aria-hidden", "false");
    $("#detailCategory").textContent = node.pointType;
    $("#detailDot").style.setProperty("--detail-color", colorFor(node.pointType));
    $("#noteInspector").style.setProperty("--detail-color", colorFor(node.pointType));
    $("#detailTitle").textContent = node.title;
    $("#detailSummary").textContent = node.summary || "暂无摘要。";
    const detailSources = node.sourceIds.map((sourceId) => state.sourceMap.get(sourceId)).filter(Boolean);
    $("#detailSource").textContent = detailSources.length
      ? detailSources.map((source) => source.archived ? `${source.title}（已归档）` : source.title).join("、")
      : (node.sourceTitles.join("、") || "未标注来源");
    const primarySource = node.sourceIds.map((sourceId) => state.sourceMap.get(sourceId)).find(Boolean);
    $("#detailPath").textContent = primarySource?.sourcePath || primarySource?.path || node.path || "-";
    $("#detailType").textContent = node.pointType;
    $("#detailStatus").textContent = node.status || confidenceLabel(node.confidence);
    $("#detailUpdated").textContent = node.modified || node.metadata?.updated || "-";
    $("#detailExcerpt").textContent = node.excerpt || "暂无原文片段";
    $("#excerptLabel").textContent = node.excerptLabel || "引用";
    const sourceHref = localFileHref(primarySource?.url || primarySource?.sourcePath || primarySource?.path || node.path);
    $("#openNote").hidden = !sourceHref;
    if (sourceHref) $("#openNote").href = sourceHref;
    renderRelations(node);
    updateHighlight();
    document.querySelectorAll(".tree-note").forEach((button) => button.classList.toggle("active", button.dataset.nodeId === id));
    clearTimeout(state.fitTimer);
    state.fitTimer = setTimeout(() => fitGraph(false), 220);
  }

  function renderRelations(node) {
    const related = [...relatedNodeIds(node.id)]
      .map((id) => state.nodes.find((item) => item.id === id))
      .filter(Boolean)
      .sort((a, b) => b.degree - a.degree || a.title.localeCompare(b.title, "zh-CN"));
    const list = $("#relationList");
    list.replaceChildren();
    $("#relationCount").textContent = String(related.length);
    if (!related.length) {
      const empty = document.createElement("span");
      empty.className = "relation-empty";
      empty.textContent = "暂无关联知识点";
      list.append(empty);
      return;
    }
    related.forEach((item) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "relation-button";
      button.style.setProperty("--relation-color", colorFor(item.pointType));
      const dot = document.createElement("span");
      dot.className = "relation-dot";
      const text = document.createElement("span");
      text.textContent = item.title;
      button.append(dot, text, icon("arrow-up-right"));
      button.addEventListener("click", () => revealNode(item.id));
      list.append(button);
    });
    refreshIcons(list);
  }

  function updateHighlight() {
    if (!state.nodeSelection || !state.linkSelection) return;
    const related = relatedNodeIds(state.selectedId);
    state.nodeSelection
      .classed("is-selected", (node) => node.id === state.selectedId)
      .classed("is-neighbor", (node) => related.has(node.id))
      .classed("is-muted", (node) => Boolean(state.selectedId) && node.id !== state.selectedId && !related.has(node.id));
    state.linkSelection
      .classed("is-active", (edge) => endpointId(edge.source) === state.selectedId || endpointId(edge.target) === state.selectedId)
      .classed("is-muted", (edge) => Boolean(state.selectedId) && endpointId(edge.source) !== state.selectedId && endpointId(edge.target) !== state.selectedId);
    updateLabelVisibility();
  }

  function closeInspector() {
    const wasOpen = Boolean(state.selectedId);
    state.selectedId = null;
    state.hoveredId = null;
    $("#graphApp").classList.remove("inspector-open");
    $("#noteInspector").setAttribute("aria-hidden", "true");
    document.querySelectorAll(".tree-note.active").forEach((button) => button.classList.remove("active"));
    if (state.nodeSelection) state.nodeSelection.classed("is-selected", false).classed("is-neighbor", false).classed("is-muted", false);
    if (state.linkSelection) state.linkSelection.classed("is-active", false).classed("is-muted", false);
    updateLabelVisibility();
    if (wasOpen) {
      clearTimeout(state.fitTimer);
      state.fitTimer = setTimeout(() => fitGraph(false), 220);
    }
  }

  function revealNode(id) {
    if (!state.visibleIds.has(id)) {
      state.query = "";
      state.category = "all";
      $("#graphSearch").value = "";
      $("#searchClear").hidden = true;
      $("#categoryFilter").value = "all";
      applyFilters({ fit: true });
      setTimeout(() => selectNode(id), 40);
    } else {
      selectNode(id);
    }
    $("#graphApp").classList.remove("sidebar-open");
  }

  function toggleSidebar(open) {
    $("#graphApp").classList.toggle("sidebar-open", open);
  }

  function setIngestStatus(status, title, meta, iconName) {
    const panel = $("#ingestStatus");
    panel.dataset.state = status;
    $("#ingestStatusTitle").textContent = title;
    $("#ingestStatusMeta").textContent = meta;
    const holder = $(".ingest-status-icon", panel);
    holder.replaceChildren(icon(iconName || (status === "success" ? "check" : status === "error" ? "triangle-alert" : "circle-dashed")));
    refreshIcons(holder);
  }

  function extensionFor(file) {
    return (file.name.split(".").pop() || "").toLocaleLowerCase("en-US");
  }

  function arrayBufferToBase64(buffer) {
    const bytes = new Uint8Array(buffer);
    let binary = "";
    const chunkSize = 0x8000;
    for (let offset = 0; offset < bytes.length; offset += chunkSize) {
      binary += String.fromCharCode(...bytes.subarray(offset, Math.min(offset + chunkSize, bytes.length)));
    }
    return btoa(binary);
  }

  async function ingestBody(file) {
    const extension = extensionFor(file);
    if (!TEXT_EXTENSIONS.has(extension) && !BINARY_EXTENSIONS.has(extension)) throw new Error(`${file.name} 不是支持的文档格式`);
    if (file.size > MAX_FILE_BYTES) throw new Error(`${file.name} 超过 20 MB`);
    const common = {
      filename: file.name,
      mime_type: file.type || (extension === "pdf" ? "application/pdf" : extension === "docx" ? "application/vnd.openxmlformats-officedocument.wordprocessingml.document" : "text/plain"),
      extension,
      last_modified: file.lastModified ? new Date(file.lastModified).toISOString() : "",
    };
    return { ...common, base64: arrayBufferToBase64(await file.arrayBuffer()) };
  }

  async function responsePayload(response) {
    const text = await response.text();
    if (!text) return {};
    try { return JSON.parse(text); } catch (_) { return { message: text }; }
  }

  function ingestedPointCount(payload) {
    const normalized = graphPayload(payload);
    const array = value(normalized.knowledge_points, normalized.knowledgePoints, normalized.points);
    if (Array.isArray(array)) return array.length;
    const count = value(normalized.knowledge_point_count, normalized.knowledgePointCount, normalized.points_created, normalized.pointsCreated, normalized.stats?.knowledge_points);
    return Number.isFinite(Number(count)) ? Number(count) : 0;
  }

  function reviewRoot(rawPayload) {
    if (!rawPayload || typeof rawPayload !== "object") return {};
    if (rawPayload.data && typeof rawPayload.data === "object" && !Array.isArray(rawPayload.data)) return rawPayload.data;
    if (rawPayload.preview && typeof rawPayload.preview === "object") return rawPayload.preview;
    if (rawPayload.draft && typeof rawPayload.draft === "object") return rawPayload.draft;
    return rawPayload;
  }

  function keywordValues(input) {
    const values = Array.isArray(input) ? input : String(input || "").split(/[,，、;；|\n]/);
    return [...new Set(values.map((item) => {
      if (item && typeof item === "object") return cleanText(value(item.name, item.label, item.keyword, item.text));
      return cleanText(item);
    }).filter(Boolean))].slice(0, 18);
  }

  function normalizeReviewPayload(rawPayload, file) {
    const root = reviewRoot(rawPayload);
    const rawPoints = [
      ...asArray(root.knowledge_points),
      ...asArray(root.knowledgePoints),
      ...asArray(root.candidates),
      ...asArray(root.candidate_points),
      ...asArray(root.candidatePoints),
      ...asArray(root.candidate_knowledge_points),
      ...asArray(root.candidateKnowledgePoints),
      ...asArray(root.points),
      ...asArray(root.knowledge_nodes),
      ...asArray(root.knowledgeNodes),
    ].filter((item) => item && typeof item === "object");
    const points = [];
    const usedIds = new Set();
    rawPoints.forEach((raw, index) => {
      let id = cleanText(value(raw.id, raw.knowledge_point_id, raw.knowledgePointId, raw.candidate_id, raw.candidateId, raw.key, raw.slug));
      if (!id) id = `candidate-${index + 1}`;
      const baseId = id;
      let suffix = 2;
      while (usedIds.has(id)) id = `${baseId}-${suffix++}`;
      usedIds.add(id);
      const claim = cleanText(value(raw.claim, raw.statement, raw.conclusion, raw.content, raw.text, raw.description, raw.summary));
      const title = cleanText(value(raw.title, raw.name, raw.label, raw.heading, claim), `候选知识点 ${index + 1}`);
      const summary = cleanText(value(raw.summary, raw.description, raw.claim, raw.statement, raw.content, raw.text), claim);
      const excerpt = cleanText(value(raw.excerpt, raw.source_excerpt, raw.sourceExcerpt, raw.quote, raw.original_text, raw.originalText, raw.evidence));
      const keywords = keywordValues(value(raw.keywords, raw.tags, raw.key_terms, raw.keyTerms));
      points.push({ id, title, summary, excerpt, keywords, pointType: cleanText(value(raw.point_type, raw.pointType, raw.knowledge_kind, raw.knowledgeKind, raw.type), "知识点"), confidence: value(raw.confidence, raw.score), section: cleanText(value(raw.section, raw.source_section, raw.sourceSection)), raw });
    });
    const sourceRaw = value(root.source, root.source_document, root.sourceDocument, root.document, root.source_summary, root.sourceSummary, root.file, file ? { filename: file.name } : null);
    const source = normalizeSource(sourceRaw || {}, 0);
    const keywords = keywordValues(value(root.keywords, root.tags, root.key_terms, root.keyTerms, root.source_keywords, root.sourceKeywords));
    const mergedKeywords = keywords.length ? keywords : [...new Set(points.flatMap((point) => point.keywords))].slice(0, 18);
    points.forEach((point) => {
      if (!point.keywords.length) point.keywords = mergedKeywords.slice(0, 8);
    });
    const previewId = cleanText(value(root.preview_id, root.previewId, root.draft_id, root.draftId, root.review_id, root.reviewId, root.run_id, root.runId, rawPayload.preview_id, rawPayload.draft_id));
    return {
      id: previewId,
      source,
      filename: cleanText(value(root.filename, root.file_name, root.fileName, source.filename, file?.name), "未命名文档"),
      keywords: mergedKeywords,
      points,
      relations: asArray(value(root.relations, root.relationships, root.relation_candidates, root.relationCandidates)),
      raw: rawPayload,
    };
  }

  function parseReviewKeywords(input) {
    return keywordValues(input);
  }

  function selectedReviewRows() {
    if (!state.review) return [];
    return [...document.querySelectorAll(".review-point")].filter((row) => $(".review-point-check", row)?.checked).map((row) => {
      const id = row.dataset.pointId;
      const point = state.review.points.find((item) => item.id === id);
      const mode = $(".review-point-mode select", row)?.value || "save";
      const keywordInput = $(".review-point-keywords-input", row);
      return {
        id,
        point_id: id,
        knowledge_point_id: id,
        candidate_id: point?.raw?.candidate_id || point?.raw?.candidateId || id,
        mode,
        graph_status: mode === "save_and_graph" ? "active" : "excluded",
        keywords: parseReviewKeywords(keywordInput?.value || point?.keywords || ""),
      };
    });
  }

  function updateReviewSelection() {
    if (!state.review) return;
    const rows = [...document.querySelectorAll(".review-point")];
    const selected = rows.filter((row) => $(".review-point-check", row)?.checked);
    rows.forEach((row) => {
      const checked = $(".review-point-check", row)?.checked;
      row.classList.toggle("is-selected", Boolean(checked));
      const select = $(".review-point-mode select", row);
      if (select) select.disabled = !checked;
    });
    const graphCount = selected.filter((row) => $(".review-point-mode select", row)?.value === "save_and_graph").length;
    $("#reviewSummary").textContent = `${selected.length}/${rows.length} 个知识点已选择${graphCount ? ` · ${graphCount} 个进入图谱` : ""}`;
    $("#reviewCommit").disabled = selected.length === 0 || state.reviewing;
    $("#reviewFootnote").textContent = selected.length ? `将保存 ${selected.length} 个知识点；其中 ${graphCount} 个进入图谱。` : "至少选择一个知识点后才能收录。";
    const allGraph = rows.length > 0 && rows.every((row) => $(".review-point-check", row)?.checked && $(".review-point-mode select", row)?.value === "save_and_graph");
    $("#reviewSelectAll").textContent = allGraph ? "取消全选" : "全选入图";
  }

  function renderReview() {
    const review = state.review;
    const modal = $("#reviewModal");
    const pointsHolder = $("#reviewPoints");
    const keywordSection = $("#reviewKeywords");
    const keywordHolder = $("#reviewKeywordList");
    $("#reviewCommit span").textContent = "确认收录";
    $("#reviewCancel").disabled = false;
    $("#reviewClose").disabled = false;
    $("#reviewSource").textContent = `${review.filename} · 预览草稿，尚未写入知识库`;
    keywordHolder.replaceChildren();
    if (review.keywords.length) {
      review.keywords.forEach((keyword) => {
        const chip = document.createElement("span");
        chip.className = "keyword-chip";
        chip.textContent = keyword;
        keywordHolder.append(chip);
      });
      keywordSection.hidden = false;
    } else {
      keywordSection.hidden = true;
    }
    pointsHolder.replaceChildren();
    if (!review.points.length) {
      const empty = document.createElement("div");
      empty.className = "review-empty";
      empty.textContent = "没有提炼出可审核的知识点。";
      pointsHolder.append(empty);
    } else {
      review.points.forEach((point, index) => {
        const row = document.createElement("article");
        row.className = "review-point";
        row.dataset.pointId = point.id;

        const checkbox = document.createElement("input");
        checkbox.className = "review-point-check";
        checkbox.type = "checkbox";
        checkbox.dataset.pointId = point.id;
        checkbox.setAttribute("aria-label", `选择${point.title}`);
        checkbox.addEventListener("change", updateReviewSelection);

        const main = document.createElement("div");
        main.className = "review-point-main";
        const title = document.createElement("div");
        title.className = "review-point-title";
        title.textContent = point.title;
        const summary = document.createElement("div");
        summary.className = "review-point-summary";
        summary.textContent = point.summary || "暂无摘要";
        main.append(title, summary);
        if (point.excerpt) {
          const excerpt = document.createElement("div");
          excerpt.className = "review-point-excerpt";
          excerpt.textContent = point.excerpt;
          main.append(excerpt);
        }
        const meta = document.createElement("div");
        meta.className = "review-point-meta";
        [point.pointType, point.section, point.confidence !== undefined && point.confidence !== "" ? `置信度 ${confidenceLabel(point.confidence)}` : ""].filter(Boolean).forEach((text) => {
          const tag = document.createElement("span");
          tag.className = "review-point-keyword";
          tag.textContent = text;
          meta.append(tag);
        });
        if (meta.childElementCount) main.append(meta);
        const keywordLabel = document.createElement("label");
        keywordLabel.className = "review-point-keywords-label";
        keywordLabel.textContent = "关键词";
        const keywordInput = document.createElement("input");
        keywordInput.className = "review-point-keywords-input";
        keywordInput.type = "text";
        keywordInput.value = point.keywords.join("、");
        keywordInput.placeholder = "关键词，用顿号分隔";
        keywordInput.setAttribute("aria-label", `${point.title}的关键词`);
        keywordLabel.append(keywordInput);
        main.append(keywordLabel);

        const mode = document.createElement("div");
        mode.className = "review-point-mode";
        const modeLabel = document.createElement("label");
        modeLabel.textContent = "收录方式";
        const select = document.createElement("select");
        select.setAttribute("aria-label", `${point.title}的收录方式`);
        [{ value: "save", label: "仅保存为笔记" }, { value: "save_and_graph", label: "保存并加入图谱" }].forEach((optionData) => {
          const option = document.createElement("option");
          option.value = optionData.value;
          option.textContent = optionData.label;
          select.append(option);
        });
        select.value = "save";
        select.disabled = true;
        select.addEventListener("change", updateReviewSelection);
        modeLabel.append(select);
        mode.append(modeLabel);

        row.append(checkbox, main, mode);
        pointsHolder.append(row);
      });
    }
    modal.hidden = false;
    state.reviewFocus = document.activeElement;
    refreshIcons(modal);
    updateReviewSelection();
    setTimeout(() => $(".review-point-check", modal)?.focus(), 0);
  }

  function closeReview({ status = "idle", focus = true } = {}) {
    const modal = $("#reviewModal");
    const draftId = state.review?.id;
    modal.hidden = true;
    state.review = null;
    state.reviewing = false;
    $("#reviewCommit").disabled = false;
    $("#sourceFileInput").disabled = false;
    $("#ingestButton").disabled = false;
    $("#ingestButton").classList.remove("is-disabled");
    if (status === "idle") {
      setIngestStatus("idle", "等待导入", "选择工作材料开始提炼", "circle-dashed");
      if (draftId) {
        fetch(`${API_BASE}/api/knowledge/cancel`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ preview_id: draftId, draft_id: draftId }),
        }).catch(() => {});
      }
    }
    if (focus && state.reviewFocus && typeof state.reviewFocus.focus === "function") state.reviewFocus.focus();
    state.reviewFocus = null;
  }

  async function previewDocument(file) {
    const body = await ingestBody(file);
    const response = await fetch(`${API_BASE}/api/knowledge/preview`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const payload = await responsePayload(response);
    if (!response.ok) {
      const fallback = response.status === 404 ? "文档预览服务尚未启用" : "文档预览失败";
      throw new Error(payload.error?.message || payload.message || fallback);
    }
    const review = normalizeReviewPayload(payload, file);
    if (!review.id) throw new Error("预览服务未返回预览编号，请稍后重试");
    state.review = review;
    renderReview();
    setIngestStatus("review", "等待审核", `${review.points.length} 个候选知识点`, "list-checks");
  }

  async function commitReview() {
    if (!state.review || state.reviewing) return;
    const selected = selectedReviewRows();
    if (!selected.length) {
      toast("请至少选择一个知识点");
      updateReviewSelection();
      return;
    }
    const review = state.review;
    state.reviewing = true;
    $("#reviewCommit").disabled = true;
    $("#reviewCancel").disabled = true;
    $("#reviewClose").disabled = true;
    $("#reviewCommit span").textContent = "正在收录";
    try {
      const response = await fetch(`${API_BASE}/api/knowledge/commit`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          preview_id: review.id,
          draft_id: review.id,
          source_id: review.source.id || "",
          source_filename: review.filename,
          source_keywords: review.keywords,
          selected_points: selected,
        }),
      });
      const payload = await responsePayload(response);
      if (!response.ok) {
        const fallback = response.status === 404 ? "知识库提交服务尚未启用" : "知识点收录失败";
        throw new Error(payload.error?.message || payload.message || fallback);
      }
      const count = ingestedPointCount(payload) || selected.length;
      closeReview({ status: "success" });
      setIngestStatus("success", "知识点已收录", `${count} 个知识点已写入知识库`, "check");
      await loadGraph();
      toast(`已收录 ${count} 个知识点`);
    } catch (error) {
      state.reviewing = false;
      $("#reviewCommit").disabled = false;
      $("#reviewCancel").disabled = false;
      $("#reviewClose").disabled = false;
      $("#reviewCommit span").textContent = "确认收录";
      setIngestStatus("error", "收录失败", error.message || "请稍后重试", "triangle-alert");
      toast(error.message || "知识点收录失败");
    }
  }

  async function ingestDocuments(fileList) {
    const files = Array.from(fileList || []);
    if (!files.length || state.ingesting) return;
    if (files.length > 1) {
      setIngestStatus("error", "一次选择一份文档", "审核完成后再导入下一份", "triangle-alert");
      toast("最小审核流程一次处理一份文档");
      return;
    }
    state.ingesting = true;
    $("#sourceFileInput").disabled = true;
    $("#ingestButton").disabled = true;
    $("#ingestButton").classList.add("is-disabled");
    try {
      setIngestStatus("uploading", "正在分析文档", files[0].name, "loader-circle");
      await previewDocument(files[0]);
    } catch (error) {
      setIngestStatus("error", "预览失败", error.message || "请稍后重试", "triangle-alert");
      toast(error.message || "文档预览失败");
    } finally {
      state.ingesting = false;
      const reviewOpen = Boolean(state.review);
      $("#sourceFileInput").disabled = reviewOpen;
      $("#ingestButton").disabled = reviewOpen;
      $("#sourceFileInput").value = "";
      $("#ingestButton").classList.toggle("is-disabled", reviewOpen);
    }
  }

  $("#graphSearch").addEventListener("input", (event) => {
    state.query = event.target.value.trim();
    $("#searchClear").hidden = !state.query;
    closeInspector();
    applyFilters({ fit: true });
  });
  $("#searchClear").addEventListener("click", () => {
    $("#graphSearch").value = "";
    state.query = "";
    $("#searchClear").hidden = true;
    applyFilters({ fit: true });
    $("#graphSearch").focus();
  });
  $("#categoryFilter").addEventListener("change", (event) => {
    state.category = event.target.value;
    closeInspector();
    applyFilters({ fit: true });
  });
  $("#sourceFileInput").addEventListener("change", (event) => ingestDocuments(event.target.files));
  $("#ingestButton").addEventListener("click", () => $("#sourceFileInput").click());
  $("#reviewCommit").addEventListener("click", commitReview);
  $("#reviewCancel").addEventListener("click", () => {
    if (!state.reviewing) closeReview();
  });
  $("#reviewClose").addEventListener("click", () => {
    if (!state.reviewing) closeReview();
  });
  $("#reviewSelectAll").addEventListener("click", () => {
    const rows = [...document.querySelectorAll(".review-point")];
    const allGraph = rows.length > 0 && rows.every((row) => $(".review-point-check", row)?.checked && $(".review-point-mode select", row)?.value === "save_and_graph");
    rows.forEach((row) => {
      const checkbox = $(".review-point-check", row);
      const select = $(".review-point-mode select", row);
      if (!checkbox || !select) return;
      checkbox.checked = !allGraph;
      select.value = "save_and_graph";
    });
    updateReviewSelection();
  });
  $("#zoomIn").addEventListener("click", () => zoomBy(1.3));
  $("#zoomOut").addEventListener("click", () => zoomBy(0.77));
  $("#fitGraph").addEventListener("click", () => fitGraph());
  $("#refreshGraph").addEventListener("click", () => loadGraph({ quiet: true }));
  $("#mobileRefresh").addEventListener("click", () => loadGraph({ quiet: true }));
  $("#inspectorClose").addEventListener("click", closeInspector);
  $("#treeToggle").addEventListener("click", () => toggleSidebar(true));
  $("#sidebarClose").addEventListener("click", () => toggleSidebar(false));
  $("#sidebarBackdrop").addEventListener("click", () => toggleSidebar(false));
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      if (!$("#reviewModal").hidden && !state.reviewing) closeReview();
      else if ($("#graphApp").classList.contains("sidebar-open")) toggleSidebar(false);
      else closeInspector();
    }
  });

  refreshIcons();
  loadGraph();
})();
