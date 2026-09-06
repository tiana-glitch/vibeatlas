import json
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from note_knowledge import NoteOrchestrator
from server import build_server


def _post(base: str, path: str, payload: dict):
    request = Request(
        base + path,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=3) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body = json.loads(exc.read().decode("utf-8"))
        raise AssertionError("HTTP %s: %s" % (exc.code, body)) from exc


def _post_error(base: str, path: str, payload: dict):
    request = Request(
        base + path,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=3):
        raise AssertionError("expected an HTTP error")


def _get_json(base: str, path: str):
    with urlopen(base + path, timeout=3) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def _initialize_lifecycle_vault(root: Path):
    (root / "07-素材附件/来源摘要/会议").mkdir(parents=True)
    (root / "07-素材附件/原始资料").mkdir(parents=True)
    (root / "08-知识点/会议").mkdir(parents=True)
    (root / "01-知识库目录.md").write_text(
        "---\ntype: index\n---\n\n# 知识库目录\n",
        encoding="utf-8",
    )
    (root / "02-更新流水账.md").write_text(
        "---\ntype: log\n---\n\n# 更新流水账\n\n"
        "| 时间 | 动作 | 笔记 | 来源 | 状态 |\n"
        "| --- | --- | --- | --- | --- |\n",
        encoding="utf-8",
    )
    raw = root / "07-素材附件/原始资料/会议纪要.pdf"
    raw_bytes = b"pdf evidence bytes\x00\x01"
    raw.write_bytes(raw_bytes)
    (root / "07-素材附件/来源摘要/会议/会议纪要-来源摘要.md").write_text(
        "---\n"
        "type: source-summary\n"
        "source_document: 会议纪要.pdf\n"
        "source_file: 会议纪要.pdf\n"
        "source_path: 07-素材附件/原始资料/会议纪要.pdf\n"
        "source_status: active\n"
        "graph_status: active\n"
        "---\n\n# 会议纪要\n\n摘要。\n",
        encoding="utf-8",
    )
    point = root / "08-知识点/会议/会议纪要-知识点.md"
    point.write_text(
        "---\n"
        "type: knowledge-point\n"
        "title: 会议结论\n"
        "source_summary: 会议纪要-来源摘要\n"
        "graph_status: active\n"
        "---\n\n# 会议结论\n\n证据。\n",
        encoding="utf-8",
    )
    return raw, point


class ImprovementEndpointTests(unittest.TestCase):
    def test_ocr_correction_keeps_original_and_rejects_stale_revision(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            service = NoteOrchestrator(storage_dir=root / ".note_runs")
            http_server = build_server(port=0, directory=root, note_orchestrator=service)
            thread = threading.Thread(target=http_server.serve_forever, daemon=True)
            thread.start()
            base = "http://%s:%d" % http_server.server_address
            try:
                status, processed = _post(base, "/api/notes/process", {"ocr_text": "原始识别错字。\n需求需要验证。"})
                self.assertEqual(status, 201)
                run_id = processed["run_id"]
                status, corrected = _post(
                    base,
                    "/api/notes/runs/%s/ocr/correct" % run_id,
                    {"corrected_text": "修订后的产品结论。", "expected_ocr_revision": 0},
                )
                self.assertEqual(status, 200)
                self.assertEqual(corrected["ocr_revision"], 1)
                self.assertEqual(corrected["state"]["original_ocr_text"], "原始识别错字。\n需求需要验证。")
                self.assertEqual(corrected["state"]["corrected_ocr_text"], "修订后的产品结论。")
                self.assertIn("修订后的产品结论", corrected["archive_markdown"])

                request = Request(
                    base + "/api/notes/runs/%s/ocr/correct" % run_id,
                    data=json.dumps({"corrected_text": "过期修改", "expected_ocr_revision": 0}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with self.assertRaises(HTTPError) as raised:
                    urlopen(request, timeout=3)
                self.assertEqual(raised.exception.code, 409)
                self.assertEqual(json.loads(raised.exception.read().decode("utf-8"))["error"]["code"], "ocr_revision_conflict")
            finally:
                http_server.shutdown()
                http_server.server_close()
                thread.join(timeout=3)

    def test_source_archive_restore_hides_points_and_preserves_raw_bytes(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            raw, point = _initialize_lifecycle_vault(root)
            original_raw = raw.read_bytes()
            http_server = build_server(port=0, directory=root, note_orchestrator=NoteOrchestrator())
            thread = threading.Thread(target=http_server.serve_forever, daemon=True)
            thread.start()
            base = "http://%s:%d" % http_server.server_address
            try:
                status, graph = _get_json(base, "/api/knowledge/graph")
                self.assertEqual(status, 200)
                self.assertIn("会议纪要-知识点", {node["id"] for node in graph["knowledge_nodes"]})

                status, proposal = _post(
                    base,
                    "/api/knowledge/source/propose",
                    {"action": "archive", "source_id": "会议纪要-来源摘要", "operation_id": "endpoint-archive"},
                )
                self.assertEqual(status, 200)
                self.assertEqual(proposal["status"], "awaiting_review")
                self.assertNotIn("source_status: archived", (root / "07-素材附件/来源摘要/会议/会议纪要-来源摘要.md").read_text(encoding="utf-8"))

                status, committed = _post(
                    base,
                    "/api/knowledge/source/commit",
                    {"proposal_id": proposal["proposal_id"], "expected_hashes": proposal["hashes"]},
                )
                self.assertEqual(status, 201)
                self.assertEqual(committed["source"]["source_status"], "archived")
                _, archived_graph = _get_json(base, "/api/knowledge/graph")
                self.assertNotIn("会议纪要-知识点", {node["id"] for node in archived_graph["knowledge_nodes"]})
                archived_source = next(item for item in archived_graph["sources"] if item["id"] == "会议纪要-来源摘要")
                self.assertTrue(archived_source["archived"])
                self.assertEqual(archived_source["all_point_ids"], ["会议纪要-知识点"])
                self.assertEqual(raw.read_bytes(), original_raw)

                status, restored = _post(
                    base,
                    "/api/knowledge/source/restore",
                    {"source_id": "会议纪要-来源摘要", "operation_id": "endpoint-restore"},
                )
                self.assertEqual(status, 201)
                self.assertEqual(restored["source"]["source_status"], "active")
                _, restored_graph = _get_json(base, "/api/knowledge/graph")
                self.assertIn("会议纪要-知识点", {node["id"] for node in restored_graph["knowledge_nodes"]})
                self.assertEqual(raw.read_bytes(), original_raw)
                self.assertTrue(point.is_file())
            finally:
                http_server.shutdown()
                http_server.server_close()
                thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
