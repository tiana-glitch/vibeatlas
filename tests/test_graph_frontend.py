import json
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GRAPH_JS = ROOT / "graph.js"


def _node_binary():
    candidates = [
        shutil.which("node"),
        "/Applications/ChatGPT.app/Contents/Resources/cua_node/bin/node",
    ]
    return next((candidate for candidate in candidates if candidate and Path(candidate).is_file()), None)


NODE_BINARY = _node_binary()


class GraphFrontendNormalizationTests(unittest.TestCase):
    """Exercise the browser normalizer without requiring a browser or DOM package."""

    @unittest.skipUnless(NODE_BINARY, "Node.js is required for the graph normalizer test")
    def test_excluded_points_from_legacy_nodes_are_not_rendered(self):
        payload = {
            "knowledge_nodes": [
                {
                    "id": "active-point",
                    "type": "knowledge-point",
                    "title": "Active point",
                    "source_summary": "source-summary",
                    "metadata": {"graph_status": "active"},
                }
            ],
            # The API keeps the complete note list for compatibility.  The
            # client must not re-introduce excluded points from this list.
            "nodes": [
                {
                    "id": "active-point",
                    "type": "knowledge-point",
                    "title": "Active point",
                    "source_summary": "source-summary",
                    "metadata": {"graph_status": "active"},
                },
                {
                    "id": "excluded-point",
                    "type": "knowledge-point",
                    "title": "Excluded point",
                    "source_summary": "source-summary",
                    "metadata": {"graph_status": "excluded"},
                },
                {
                    "id": "hidden-point",
                    "type": "knowledge-point",
                    "title": "Hidden point",
                    "source_summary": "source-summary",
                    "metadata": {"graph_status": "hidden"},
                },
            ],
            "sources": [{"id": "source-summary", "title": "Source", "point_ids": ["active-point"]}],
            "knowledge_edges": [],
        }
        runner = r'''
const fs = require("fs");
const vm = require("vm");

function element() {
  return {
    hidden: false,
    dataset: {},
    classList: { add() {}, remove() {}, toggle() {} },
    style: { setProperty() {} },
    replaceChildren() {}, append() {}, appendChild() {},
    addEventListener() {}, setAttribute() {}, focus() {}, click() {},
    querySelector() { return element(); }, querySelectorAll() { return []; },
    textContent: "", value: "", disabled: false,
  };
}

const document = {
  activeElement: element(),
  querySelector() { return element(); },
  querySelectorAll() { return []; },
  createElement() { return element(); },
  addEventListener() {},
};
const context = {
  window: { location: { protocol: "http:" }, d3: null, lucide: null },
  document, console, ResizeObserver: class {},
  setTimeout, clearTimeout, Map, Set, Math, JSON, URL, URLSearchParams,
  Promise, Date, Array, Uint8Array,
};
context.globalThis = context;

let source = fs.readFileSync(process.argv[1], "utf8");
const marker = "})();";
const markerIndex = source.lastIndexOf(marker);
if (markerIndex < 0) throw new Error("graph.js IIFE marker not found");
source = source.slice(0, markerIndex)
  + "globalThis.__normalizePayload = normalizePayload;\n"
  + source.slice(markerIndex);
vm.runInNewContext(source, context, { filename: "graph.js" });
const normalized = context.__normalizePayload(JSON.parse(process.argv[2]));
process.stdout.write(JSON.stringify(normalized.nodes.map((node) => node.id)));
'''
        completed = subprocess.run(
            [NODE_BINARY, "-e", runner, str(GRAPH_JS), json.dumps(payload)],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(json.loads(completed.stdout), ["active-point"])

    @unittest.skipUnless(NODE_BINARY, "Node.js is required for the graph color test")
    def test_knowledge_kind_colors_are_stable_and_keep_atomic_claim_compatibility(self):
        runner = r'''
const fs = require("fs");
const vm = require("vm");

function element() {
  return {
    hidden: false,
    dataset: {},
    classList: { add() {}, remove() {}, toggle() {} },
    style: { setProperty() {} },
    replaceChildren() {}, append() {}, appendChild() {},
    addEventListener() {}, setAttribute() {}, focus() {}, click() {},
    querySelector() { return element(); }, querySelectorAll() { return []; },
    textContent: "", value: "", disabled: false,
  };
}

const document = {
  activeElement: element(),
  querySelector() { return element(); },
  querySelectorAll() { return []; },
  createElement() { return element(); },
  addEventListener() {},
};
const context = {
  window: { location: { protocol: "http:" }, d3: null, lucide: null },
  document, console, ResizeObserver: class {},
  setTimeout, clearTimeout, Map, Set, Math, JSON, URL, URLSearchParams,
  Promise, Date, Array, Uint8Array,
};
context.globalThis = context;

let source = fs.readFileSync(process.argv[1], "utf8");
const marker = "})();";
const markerIndex = source.lastIndexOf(marker);
if (markerIndex < 0) throw new Error("graph.js IIFE marker not found");
source = source.slice(0, markerIndex)
  + "globalThis.__colorFor = colorFor;\n"
  + "globalThis.__inferVisualKind = inferVisualKind;\n"
  + source.slice(markerIndex);
vm.runInNewContext(source, context, { filename: "graph.js" });
const colorFor = context.__colorFor;
const inferVisualKind = context.__inferVisualKind;
const ordered = ["decision", "method", "atomic-claim", "custom-kind"].map((kind) => [kind, colorFor(kind)]);
const reversed = ["custom-kind", "atomic-claim", "method", "decision"].map((kind) => [kind, colorFor(kind)]);
const legacyKinds = [
  { title: "原始文件需要保留证据", metadata: { knowledge_kind: "atomic-claim", source_section: "来源与证据" } },
  { title: "通过步骤整理材料", metadata: { knowledge_kind: "atomic-claim", source_section: "收录事务" } },
  { title: "默认关系图只显示知识点", metadata: { knowledge_kind: "atomic-claim", source_section: "展示模型" } },
].map((point) => inferVisualKind(point));
const result = {
  ordered,
  reversed,
  atomicAlias: colorFor("atomic_claim") === colorFor("atomic-claim"),
  semanticContrast: colorFor("decision") !== colorFor("method"),
  legacyKinds,
  legacyColorCount: new Set(legacyKinds.map((kind) => colorFor(kind))).size,
};
process.stdout.write(JSON.stringify(result));
'''
        completed = subprocess.run(
            [NODE_BINARY, "-e", runner, str(GRAPH_JS)],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(dict(result["ordered"]), dict(result["reversed"]))
        self.assertTrue(result["atomicAlias"])
        self.assertTrue(result["semanticContrast"])
        self.assertEqual(result["legacyKinds"], ["evidence", "method", "decision"])
        self.assertEqual(result["legacyColorCount"], 3)

    @unittest.skipUnless(NODE_BINARY, "Node.js is required for the source status test")
    def test_archived_source_status_is_normalized_and_not_lost_on_point_reference(self):
        payload = {
            "sources": [
                {"id": "archived-source", "title": "旧材料", "status": "archived", "all_point_ids": ["point-1", "point-2"], "total_count": 2},
                {"id": "flagged-source", "title": "另一份旧材料", "status": "active", "archived": True},
            ],
            "knowledge_points": [{
                "id": "point-1",
                "title": "保留的知识点",
                "source_id": "archived-source",
                "knowledge_kind": "atomic-claim",
            }],
        }
        runner = r'''
const fs = require("fs");
const vm = require("vm");

function element() {
  return {
    hidden: false,
    dataset: {},
    classList: { add() {}, remove() {}, toggle() {} },
    style: { setProperty() {} },
    replaceChildren() {}, append() {}, appendChild() {},
    addEventListener() {}, setAttribute() {}, focus() {}, click() {},
    querySelector() { return element(); }, querySelectorAll() { return []; },
    textContent: "", value: "", disabled: false,
  };
}

const document = {
  activeElement: element(),
  querySelector() { return element(); },
  querySelectorAll() { return []; },
  createElement() { return element(); },
  addEventListener() {},
};
const context = {
  window: { location: { protocol: "http:" }, d3: null, lucide: null },
  document, console, ResizeObserver: class {},
  setTimeout, clearTimeout, Map, Set, Math, JSON, URL, URLSearchParams,
  Promise, Date, Array, Uint8Array,
};
context.globalThis = context;

let source = fs.readFileSync(process.argv[1], "utf8");
const marker = "})();";
const markerIndex = source.lastIndexOf(marker);
if (markerIndex < 0) throw new Error("graph.js IIFE marker not found");
source = source.slice(0, markerIndex)
  + "globalThis.__normalizePayload = normalizePayload;\n"
  + source.slice(markerIndex);
vm.runInNewContext(source, context, { filename: "graph.js" });
const normalized = context.__normalizePayload(JSON.parse(process.argv[2]));
const sourceItem = normalized.sources.find((item) => item.id === "archived-source");
const flaggedItem = normalized.sources.find((item) => item.id === "flagged-source");
process.stdout.write(JSON.stringify({
  archived: sourceItem?.archived,
  statusKey: sourceItem?.statusKey,
  totalCount: sourceItem?.totalCount,
  allPointIds: sourceItem?.allPointIds,
  flaggedArchived: flaggedItem?.archived,
  pointSource: normalized.nodes[0]?.sourceIds,
}));
'''
        completed = subprocess.run(
            [NODE_BINARY, "-e", runner, str(GRAPH_JS), json.dumps(payload)],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        result = json.loads(completed.stdout)
        self.assertTrue(result["archived"])
        self.assertEqual(result["statusKey"], "archived")
        self.assertEqual(result["totalCount"], 2)
        self.assertEqual(result["allPointIds"], ["point-1", "point-2"])
        self.assertTrue(result["flaggedArchived"])
        self.assertEqual(result["pointSource"], ["archived-source"])


class GraphFrontendVisualContractTests(unittest.TestCase):
    def test_zoom_label_and_archive_visual_contract_is_present(self):
        script = GRAPH_JS.read_text(encoding="utf-8")
        stylesheet = (ROOT / "graph.css").read_text(encoding="utf-8")
        self.assertIn("LABEL_HIDE_ZOOM", script)
        self.assertIn("priorityLabelIds", script)
        self.assertIn("DENSE_GRAPH_LABEL_ZOOM", script)
        self.assertIn("updateLabelVisibility", script)
        self.assertIn("labels-hidden", script)
        self.assertIn("source-status", script)
        self.assertIn("changeSourceStatus", script)
        self.assertIn("/api/knowledge/source/propose", script)
        self.assertIn("/api/knowledge/source/commit", script)
        self.assertIn("source-action", stylesheet)
        self.assertIn("graph-viewport.labels-hidden", stylesheet)
        self.assertIn("graph-link.is-active", stylesheet)
        self.assertIn("--graph-archived", stylesheet)


if __name__ == "__main__":
    unittest.main()
