import base64
import io
import json
import subprocess
import sys
import threading
import unittest
import zipfile
import zlib
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock
from urllib.error import HTTPError
from urllib.parse import quote_plus
from urllib.request import Request, urlopen

from note_knowledge import NoteOrchestrator, OCRAgent
from note_knowledge.retrieval import ResearchAgent
import server as server_module
from server import build_server


class FakeImageOCR:
    def extract(self, image, user_note=""):
        return {
            "原始提取文本": "TypeScript 类型收窄可以减少运行时错误。",
            "噪声内容": "无",
            "文本类型": "网页截图",
        }


class FakeSearchProvider:
    def search(self, query, limit=5):
        return [{"title": "LangGraph 文档", "url": "https://example.com/langgraph", "snippet": "用于编排有状态 Agent 工作流的框架。", "source": "fake"}]


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.server = build_server(port=0, note_orchestrator=NoteOrchestrator({"retrieve": ResearchAgent(FakeSearchProvider())}))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.base_url = "http://%s:%d" % (host, port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def post(self, path, payload):
        request = Request(
            self.base_url + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=2) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def get(self, path):
        with urlopen(self.base_url + path, timeout=2) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def restart_server(self, directory):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        note_service = NoteOrchestrator({"retrieve": ResearchAgent(FakeSearchProvider())})
        self.server = build_server(port=0, directory=directory, note_orchestrator=note_service)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.base_url = "http://%s:%d" % (host, port)

    def initialize_vault(self, root):
        (root / "00-知识库说明.md").write_text(
            "# 知识库说明\n\n规则：`[[文件名]]`。\n\n"
            "每次写成一篇或多篇知识笔记，按来源和按知识点更新目录，并在流水账写唯一一行。\n\n"
            "入口：[[01-知识库目录]]、[[02-更新流水账]]。\n",
            encoding="utf-8",
        )
        (root / "01-知识库目录.md").write_text(
            "---\ntype: index\nupdated: 2026-09-02\n---\n\n# 知识库目录\n\n"
            "## 知识点（按来源文档）\n\n"
            "<!-- KNOWLEDGE_POINTS_START -->\n暂无已导入的知识点。\n<!-- KNOWLEDGE_POINTS_END -->\n\n"
            "## 保留内容\n\n这一段不能被导入覆盖。\n",
            encoding="utf-8",
        )
        (root / "02-更新流水账.md").write_text(
            "---\ntype: log\nupdated: 2026-09-02\n---\n\n# 更新流水账\n\n"
            "| 时间 | 动作 | 笔记 | 来源 | 状态 |\n| --- | --- | --- | --- | --- |\n",
            encoding="utf-8",
        )

    def test_analyze_and_retry_endpoints(self):
        status, result = self.post(
            "/api/analyze",
            {
                "target_role": "Product Engineer",
                "job_description": "Strong React skills are required; Python is a plus.",
                "resume": "Built and maintained a React workflow application.",
            },
        )
        self.assertEqual(status, 201)
        self.assertEqual(result["status"], "complete")

        status, retried = self.post(
            "/api/runs/%s/stages/matcher/retry" % result["run_id"],
            {},
        )
        self.assertEqual(status, 200)
        self.assertEqual(retried["attempts"]["matcher"], 2)
        self.assertEqual(retried["execution_counts"]["job"], 1)
        self.assertEqual(retried["execution_counts"]["matcher"], 2)

    def test_invalid_input_returns_json_error(self):
        with self.assertRaises(HTTPError) as raised:
            self.post("/api/analyze", {"job_description": "", "resume": ""})
        self.assertEqual(raised.exception.code, 400)
        payload = json.loads(raised.exception.read().decode("utf-8"))
        self.assertEqual(payload["error"]["code"], "validation_error")

    def test_note_pipeline_endpoint(self):
        status, result = self.post(
            "/api/notes/process",
            {
                "image_name": "langgraph-note.png",
                "user_note": "技术学习",
                "ocr_text": "水印：sample\nLangGraph 使用 StateGraph 编排工作流。",
            },
        )
        self.assertEqual(status, 201)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["state"]["primary_category"], "技术学习")
        self.assertIn("## 检索关键词", result["archive_markdown"])
        status, fetched = self.post("/api/notes/runs/%s/stages/archive/retry" % result["run_id"], {})
        self.assertEqual(status, 200)
        self.assertEqual(fetched["attempts"]["archive"], 2)

    def test_image_only_note_is_valid(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.server = build_server(
            port=0,
            note_orchestrator=NoteOrchestrator({"ocr": OCRAgent(FakeImageOCR())}),
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.base_url = "http://%s:%d" % (host, port)
        status, result = self.post(
            "/api/notes/process",
            {
                "image_name": "clipboard.png",
                "image_mime": "image/png",
                "image_data": "data:image/png;base64,aGVsbG8=",
            },
        )
        self.assertEqual(status, 201)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["progress"], 100)
        self.assertEqual(result["state"]["raw_ocr_text"], "TypeScript 类型收窄可以减少运行时错误。")

    def test_keyword_only_note_endpoint(self):
        status, result = self.post("/api/notes/process", {"keywords": "LangGraph、StateGraph"})
        self.assertEqual(status, 201)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["stage_order"], ["retrieve", "ocr", "classify", "summarize", "archive"])
        self.assertEqual(len(result["outputs"]["retrieve"]["来源列表"]), 1)
        self.assertIn("https://example.com/langgraph", result["archive_markdown"])

    def test_knowledge_graph_endpoint_scans_vault_deterministically(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "02-项目案例" / "已完成项目").mkdir(parents=True)
            (root / "99-模板").mkdir()
            (root / ".agents").mkdir()
            (root / "tests").mkdir()
            (root / "01-知识库目录.md").write_text(
                """---
type: index
updated: 2026-09-02
---
# 知识库目录

项目入口与经验索引。

[[项目甲]] [[项目甲]] [[不存在]] [[项目模板]] `[[代码示例]]`
""",
                encoding="utf-8",
            )
            (root / "02-项目案例" / "项目甲.md").write_text(
                """---
type: project
status: 已完成
updated: 2026-08-30
tags:
  - AI产品
  - Obsidian
summary: 把工作材料整理为可追溯的产品知识。
---
# AI PM 项目甲

正文中的第一段不会覆盖 frontmatter 摘要。

关联项目：[[项目乙]]
""",
                encoding="utf-8",
            )
            (root / "02-项目案例" / "已完成项目" / "项目乙.md").write_text(
                "# 项目乙\n\n沉淀面试与简历所需的证据。\n",
                encoding="utf-8",
            )
            (root / "99-模板" / "项目模板.md").write_text("# 模板\n[[项目甲]]\n", encoding="utf-8")
            (root / ".agents" / "内部说明.md").write_text("# 内部\n", encoding="utf-8")
            (root / "tests" / "测试说明.md").write_text("# 测试\n", encoding="utf-8")
            (root / "README.md").write_text("# README\n", encoding="utf-8")

            self.restart_server(root)
            status, graph = self.get("/api/knowledge/graph")
            second_status, second_graph = self.get("/api/knowledge/graph/")

            self.assertEqual(status, 200)
            self.assertEqual(second_status, 200)
            self.assertEqual(graph, second_graph)
            self.assertEqual(
                [node["id"] for node in graph["nodes"]],
                ["01-知识库目录", "项目乙", "项目甲"],
            )
            self.assertEqual(
                graph["edges"],
                [
                    {"source": "01-知识库目录", "target": "项目甲"},
                    {"source": "项目甲", "target": "项目乙"},
                ],
            )
            self.assertEqual(graph["unresolved_links"], [{"source": "01-知识库目录", "target": "不存在"}])
            self.assertEqual(
                graph["stats"],
                {"notes": 3, "links": 2, "categories": 2, "orphans": 0, "unresolved": 1},
            )
            self.assertEqual(
                graph["categories"],
                [
                    {"name": "根目录", "path": "", "count": 1},
                    {"name": "02-项目案例", "path": "02-项目案例", "count": 2},
                ],
            )
            project = next(node for node in graph["nodes"] if node["id"] == "项目甲")
            self.assertEqual(project["path"], "02-项目案例/项目甲.md")
            self.assertEqual(project["title"], "AI PM 项目甲")
            self.assertEqual(project["summary"], "把工作材料整理为可追溯的产品知识。")
            self.assertEqual(project["modified"], "2026-08-30")
            self.assertEqual(project["status"], "已完成")
            self.assertEqual(project["metadata"]["type"], "project")
            self.assertEqual(project["tags"], ["AI产品", "Obsidian"])
            self.assertFalse(any(Path(node["path"]).is_absolute() for node in graph["nodes"]))
            self.assertEqual(graph["tree"][0]["name"], "根目录")
            self.assertEqual(graph["tree"][1]["path"], "02-项目案例")
            self.assertEqual(graph["tree"][1]["children"][0]["path"], "02-项目案例/已完成项目")

    def test_knowledge_graph_rejects_duplicate_basenames(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "A").mkdir()
            (root / "B").mkdir()
            (root / "A" / "同名笔记.md").write_text("# 第一篇\n", encoding="utf-8")
            (root / "B" / "同名笔记.md").write_text("# 第二篇\n", encoding="utf-8")
            self.restart_server(root)

            with self.assertRaises(HTTPError) as raised:
                self.get("/api/knowledge/graph")
            self.assertEqual(raised.exception.code, 409)
            payload = json.loads(raised.exception.read().decode("utf-8"))
            self.assertEqual(payload["error"]["code"], "duplicate_note_name")
            self.assertIn("同名笔记.md", payload["error"]["message"])

    def test_ingest_markdown_persists_atomic_private_points_and_one_log_row(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.initialize_vault(root)
            self.restart_server(root)
            source = (
                "# 用户研究复盘\n\n"
                "- 访谈显示用户需要更清晰的失败反馈。\n"
                "- 上线前应定义成功指标和人工兜底。\n\n"
                "关联材料写成 [[内部/错误路径]]，导入后不能成为路径双链。\n"
            )

            status, result = self.post("/api/knowledge/ingest", {"filename": "产品复盘.md", "text": source})

            self.assertEqual(status, 201)
            self.assertEqual(result["status"], "complete")
            self.assertEqual(result["transaction"]["log_rows_added"], 1)
            self.assertGreaterEqual(len(result["knowledge_points"]), 3)
            source_path = root / result["source"]["source_path"]
            self.assertEqual(source_path.read_text(encoding="utf-8"), source)
            self.assertEqual(source_path.suffix, ".md")
            index_text = (root / "01-知识库目录.md").read_text(encoding="utf-8")
            log_text = (root / "02-更新流水账.md").read_text(encoding="utf-8")
            self.assertEqual(log_text.count("| 导入 |"), 1)
            point_ids = [point["id"] for point in result["knowledge_points"]]
            for note_id in point_ids:
                self.assertIn("[[%s]]" % note_id, index_text)
                self.assertIn("[[%s]]" % note_id, log_text)
            source_filename = Path(result["source"]["source_path"]).name
            self.assertIn("### 按来源", index_text)
            self.assertIn("### 按知识点", index_text)
            self.assertIn("## 保留内容", index_text)
            self.assertIn("[[%s]]" % source_filename, index_text)
            self.assertIn("[[%s]]" % source_filename, log_text)
            self.assertNotIn("[[%s]]" % result["source"]["summary_note"], log_text)
            point_path = root / result["knowledge_points"][0]["path"]
            point_text = point_path.read_text(encoding="utf-8")
            self.assertIn("type: knowledge-point", point_text)
            self.assertIn("evidence_status: 待核验", point_text)
            self.assertIn("confidentiality: 私密", point_text)
            self.assertIn("knowledge_kind: atomic-claim", point_text)
            self.assertIn("source_section:", point_text)
            self.assertIn("source_file: \"%s\"" % source_filename, point_text)
            self.assertIn("来源文件：[[%s]]" % source_filename, point_text)
            self.assertNotIn("[[内部/错误路径]]", point_text)
            summary_path = result["source"]["path"]
            self.assertTrue(summary_path.startswith("07-素材附件/来源摘要/"))
            self.assertFalse(summary_path.startswith("08-知识点/"))

            validator = (
                Path(__file__).resolve().parents[1]
                / ".agents/skills/ai-pm-knowledge-vault/scripts/validate_vault.py"
            )
            command = [sys.executable, str(validator), str(root)]
            for point_id in point_ids:
                command.extend(["--expect-note", point_id])
            validation = subprocess.run(command, capture_output=True, text=True, check=False)
            self.assertEqual(validation.returncode, 0, validation.stdout + validation.stderr)
            self.assertIn("[PASS]", validation.stdout)

            graph_status, graph = self.get("/api/knowledge/graph")
            self.assertEqual(graph_status, 200)
            self.assertEqual(len(graph["knowledge_nodes"]), len(result["knowledge_points"]))
            self.assertTrue(all(node["type"] == "knowledge-point" for node in graph["knowledge_nodes"]))
            self.assertNotIn(result["source"]["summary_note"], {node["id"] for node in graph["knowledge_nodes"]})
            self.assertNotIn("产品复盘", {node["id"] for node in graph["nodes"]})
            self.assertEqual(graph["sources"][0]["source_path"], result["source"]["source_path"])
            self.assertEqual(graph["sources"][0]["title"], "产品复盘.md")
            self.assertEqual(graph["sources"][0]["summary_id"], result["source"]["summary_note"])
            self.assertEqual(graph["sources"][0]["evidence_status"], "待核验")
            self.assertEqual(graph["sources"][0]["confidentiality"], "私密")
            knowledge_ids = {node["id"] for node in graph["knowledge_nodes"]}
            graph_point = next(node for node in graph["knowledge_nodes"] if node["id"] == result["knowledge_points"][0]["id"])
            self.assertEqual(graph_point["title"], result["knowledge_points"][0]["title"])
            self.assertNotEqual(graph_point["title"], graph_point["id"])
            self.assertTrue(
                all(
                    edge["source"] in knowledge_ids and edge["target"] in knowledge_ids
                    for edge in graph["knowledge_edges"]
                )
            )

    def test_ingest_text_base64_preserves_original_bytes(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.initialize_vault(root)
            self.restart_server(root)
            original = "\ufeff# 带 BOM 的原文\r\n\r\n第一条证据。\r\n第二条证据。\r\n".encode("utf-8")

            status, result = self.post(
                "/api/knowledge/ingest",
                {
                    "filename": "带BOM原文.md",
                    "base64": base64.b64encode(original).decode("ascii"),
                },
            )

            self.assertEqual(status, 201)
            self.assertEqual(result["source"]["extraction_method"], "decoded-text")
            saved = root / result["source"]["source_path"]
            self.assertEqual(saved.read_bytes(), original)
            self.assertGreaterEqual(len(result["knowledge_points"]), 2)

    def test_knowledge_preview_is_side_effect_free_and_commit_is_idempotent(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.initialize_vault(root)
            self.restart_server(root)
            before = {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file() and ".note_runs" not in path.parts
            }
            status, preview = self.post(
                "/api/knowledge/preview",
                {
                    "draft_id": "review-1",
                    "filename": "评审记录.md",
                    "text": "# 评审\n\n用户需要明确的失败反馈。\n\nTypeScript 类型收窄可以减少运行时错误。",
                },
            )
            self.assertEqual(status, 200)
            self.assertEqual(preview["draft_id"], "review-1")
            self.assertEqual(preview["preview_id"], "review-1")
            self.assertEqual(preview["candidates"], preview["knowledge_points"])
            self.assertTrue(all(candidate["selected"] for candidate in preview["candidates"]))
            self.assertTrue(all(candidate["save_mode"] == "save_and_graph" for candidate in preview["candidates"]))
            self.assertTrue(all(candidate["keywords"] for candidate in preview["candidates"]))
            after_preview = {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file() and ".note_runs" not in path.parts
            }
            self.assertEqual(before, after_preview)

            first_id = preview["candidates"][0]["id"]
            second_id = preview["candidates"][1]["id"]
            status, committed = self.post(
                "/api/knowledge/commit",
                {
                    "preview_id": "review-1",
                    "selected_points": [
                        {"id": first_id, "save_mode": "save_and_graph", "keywords": ["失败反馈"]},
                        {"id": second_id, "save_mode": "save"},
                    ],
                },
            )
            self.assertEqual(status, 201)
            self.assertEqual(committed["review"]["graph_count"], 1)
            self.assertEqual(committed["review"]["saved_only_count"], 1)
            log_before_repeat = (root / "02-更新流水账.md").read_text(encoding="utf-8")
            status, repeated = self.post("/api/knowledge/commit", {"draft_id": "review-1"})
            self.assertEqual(status, 200)
            self.assertEqual(repeated, committed)
            self.assertEqual((root / "02-更新流水账.md").read_text(encoding="utf-8"), log_before_repeat)

            graph_status, graph = self.get("/api/knowledge/graph")
            self.assertEqual(graph_status, 200)
            graph_ids = {node["id"] for node in graph["knowledge_nodes"]}
            self.assertIn(first_id, graph_ids)
            self.assertNotIn(second_id, graph_ids)

    def test_knowledge_commit_requires_a_selected_point_and_cancel_is_side_effect_free(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.initialize_vault(root)
            self.restart_server(root)
            _, preview = self.post(
                "/api/knowledge/preview",
                {"filename": "待确认.txt", "text": "一条待确认知识点。\n\n另一条待确认知识点。"},
            )
            before = {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file() and ".note_runs" not in path.parts
            }
            with self.assertRaises(HTTPError) as raised:
                self.post(
                    "/api/knowledge/commit",
                    {
                        "draft_id": preview["draft_id"],
                        "selected_points": [
                            {"id": candidate["id"], "selected": False}
                            for candidate in preview["candidates"]
                        ],
                    },
                )
            self.assertEqual(raised.exception.code, 400)
            error = json.loads(raised.exception.read().decode("utf-8"))
            self.assertEqual(error["error"]["code"], "no_selected_knowledge_points")
            after_rejected = {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file() and ".note_runs" not in path.parts
            }
            self.assertEqual(before, after_rejected)

            status, cancelled = self.post(
                "/api/knowledge/cancel", {"preview_id": preview["preview_id"]}
            )
            self.assertEqual(status, 200)
            self.assertEqual(cancelled["status"], "cancelled")
            with self.assertRaises(HTTPError) as raised:
                self.post(
                    "/api/knowledge/commit",
                    {"draft_id": preview["draft_id"], "selected_point_ids": [preview["candidates"][0]["id"]]},
                )
            self.assertEqual(raised.exception.code, 409)

    def test_ingest_docx_from_base64_preserves_original_and_uses_unique_names(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.initialize_vault(root)
            (root / "已有附件").mkdir()
            (root / "已有附件" / "会议纪要.docx").write_bytes(b"existing-name")
            self.restart_server(root)
            docx = io.BytesIO()
            xml = (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                '<w:body><w:p><w:r><w:t>项目目标是降低知识遗漏。</w:t></w:r></w:p>'
                '<w:p><w:r><w:t>每次收录必须更新目录和流水账。</w:t></w:r></w:p></w:body></w:document>'
            )
            with zipfile.ZipFile(docx, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("word/document.xml", xml)
            payload = {"filename": "会议纪要.docx", "base64": base64.b64encode(docx.getvalue()).decode("ascii")}

            first_status, first = self.post("/api/knowledge/ingest", payload)
            second_status, second = self.post("/api/knowledge/ingest", payload)

            self.assertEqual((first_status, second_status), (201, 201))
            self.assertEqual(first["source"]["extraction_method"], "docx-xml")
            self.assertEqual(Path(first["source"]["source_path"]).name, "会议纪要-2.docx")
            self.assertEqual(Path(second["source"]["source_path"]).name, "会议纪要-3.docx")
            self.assertEqual((root / first["source"]["source_path"]).read_bytes(), docx.getvalue())
            self.assertEqual((root / second["source"]["source_path"]).read_bytes(), docx.getvalue())
            self.assertNotEqual(first["source"]["source_path"], second["source"]["source_path"])
            self.assertNotEqual(first["source"]["summary_note"], second["source"]["summary_note"])
            log = (root / "02-更新流水账.md").read_text(encoding="utf-8")
            self.assertEqual(log.count("| 导入 |"), 2)
            index = (root / "01-知识库目录.md").read_text(encoding="utf-8")
            for result in (first, second):
                source_link = "[[%s]]" % Path(result["source"]["source_path"]).name
                self.assertIn(source_link, index)
                for point in result["knowledge_points"]:
                    self.assertEqual(index.count("[[%s]]" % point["id"]), 2)

    def test_ingest_text_base64_preserves_original_encoding_bytes(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.initialize_vault(root)
            self.restart_server(root)
            source = "# 编码验证\n\n原始文本必须逐字节保留。\n".encode("gb18030")

            status, result = self.post(
                "/api/knowledge/ingest",
                {"filename": "编码验证.md", "base64": base64.b64encode(source).decode("ascii")},
            )

            self.assertEqual(status, 201)
            self.assertEqual((root / result["source"]["source_path"]).read_bytes(), source)

    def test_ingest_pdf_uses_safe_text_stream_fallback(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.initialize_vault(root)
            self.restart_server(root)
            pdf = (
                b"%PDF-1.4\n1 0 obj\n<<>>\nstream\nBT\n"
                b"(First product insight.) Tj\n(Second product insight.) Tj\n"
                b"ET\nendstream\nendobj\n%%EOF\n"
            )

            status, result = self.post(
                "/api/knowledge/ingest",
                {"filename": "research.pdf", "base64": base64.b64encode(pdf).decode("ascii")},
            )

            self.assertEqual(status, 201)
            self.assertEqual(result["source"]["extraction_method"], "pdf-text-stream")
            self.assertEqual((root / result["source"]["source_path"]).read_bytes(), pdf)
            self.assertEqual(len(result["knowledge_points"]), 2)

    def test_ingest_pdf_rejects_oversized_compressed_stream(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.initialize_vault(root)
            self.restart_server(root)
            pdf = (
                b"%PDF-1.4\n1 0 obj\n<< /Filter /FlateDecode >>\nstream\n"
                + zlib.compress(b"A" * 256)
                + b"\nendstream\nendobj\n%%EOF\n"
            )

            with mock.patch.object(server_module, "MAX_EXTRACTED_TEXT_CHARS", 64), mock.patch.object(
                server_module, "_extract_pdf_with_python", return_value=""
            ), mock.patch.object(server_module, "_extract_pdf_with_pdftotext", return_value=""):
                with self.assertRaises(HTTPError) as raised:
                    self.post(
                        "/api/knowledge/ingest",
                        {"filename": "compressed.pdf", "base64": base64.b64encode(pdf).decode("ascii")},
                    )

            self.assertEqual(raised.exception.code, 413)
            body = json.loads(raised.exception.read().decode("utf-8"))
            self.assertEqual(body["error"]["code"], "pdf_stream_too_large")

    def test_ingest_rejects_unsafe_or_invalid_input_without_mutating_vault(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.initialize_vault(root)
            original_index = (root / "01-知识库目录.md").read_bytes()
            original_log = (root / "02-更新流水账.md").read_bytes()
            self.restart_server(root)

            for payload, code in (
                ({"filename": "../secret.txt", "text": "secret"}, "invalid_filename"),
                ({"filename": "资料[[另一笔记]].txt", "text": "secret"}, "invalid_filename"),
                ({"filename": "secret.exe", "text": "secret"}, "unsupported_file_type"),
                ({"filename": "secret.txt", "base64": "not-base64"}, "invalid_base64"),
            ):
                with self.assertRaises(HTTPError) as raised:
                    self.post("/api/knowledge/ingest", payload)
                body = json.loads(raised.exception.read().decode("utf-8"))
                self.assertEqual(body["error"]["code"], code)
            self.assertEqual((root / "01-知识库目录.md").read_bytes(), original_index)
            self.assertEqual((root / "02-更新流水账.md").read_bytes(), original_log)
            self.assertFalse((root / "08-知识点").exists())

    def test_ingest_conflict_preserves_external_index_edit(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.initialize_vault(root)
            index_path = root / "01-知识库目录.md"
            log_path = root / "02-更新流水账.md"
            original_log = log_path.read_text(encoding="utf-8")
            self.restart_server(root)
            original_update = server_module._updated_index_text

            def edit_index_then_render(*args, **kwargs):
                rendered = original_update(*args, **kwargs)
                index_path.write_text(
                    index_path.read_text(encoding="utf-8") + "\n外部并发编辑必须保留。\n",
                    encoding="utf-8",
                )
                return rendered

            with mock.patch.object(server_module, "_updated_index_text", side_effect=edit_index_then_render):
                with self.assertRaises(HTTPError) as raised:
                    self.post(
                        "/api/knowledge/ingest",
                        {"filename": "并发测试.txt", "text": "这是一个需要持久化的知识点。"},
                    )

            self.assertEqual(raised.exception.code, 409)
            error = json.loads(raised.exception.read().decode("utf-8"))
            self.assertEqual(error["error"]["code"], "vault_changed_during_ingest")
            self.assertIn("外部并发编辑必须保留。", index_path.read_text(encoding="utf-8"))
            self.assertEqual(log_path.read_text(encoding="utf-8"), original_log)
            self.assertFalse((root / "08-知识点").exists())

    def test_vault_inbox_endpoint_is_read_only_and_excludes_placeholder_and_hidden_files(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.initialize_vault(root)
            inbox = root / "00-收件箱"
            (inbox / "会议").mkdir(parents=True)
            (inbox / "会议" / "访谈记录.md").write_text(
                "---\n"
                "type: inbox\n"
                "status: 待整理\n"
                "updated: 2026-09-05\n"
                "tags:\n"
                "  - 用户研究\n"
                "summary: 访谈暴露了失败反馈不清晰的问题。\n"
                "---\n"
                "# 访谈记录\n\n"
                "用户需要更清晰的失败反馈。\n",
                encoding="utf-8",
            )
            (inbox / "另一个线索.md").write_text("# 另一个线索\n\n保留原文。\n", encoding="utf-8")
            (inbox / "待整理内容.md").write_text("# 待整理内容\n\n占位。\n", encoding="utf-8")
            (inbox / ".hidden.md").write_text("# Hidden\n", encoding="utf-8")
            (inbox / "README.md").write_text("# README\n", encoding="utf-8")
            before = {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file() and ".note_runs" not in path.parts
            }

            self.restart_server(root)
            status, payload = self.get("/api/vault/inbox")

            self.assertEqual(status, 200)
            self.assertEqual(payload["count"], 2)
            self.assertEqual(payload["count"], len(payload["items"]))
            self.assertEqual(payload["items"], payload["notes"])
            self.assertEqual(
                [item["path"] for item in payload["items"]],
                ["00-收件箱/会议/访谈记录.md", "00-收件箱/另一个线索.md"],
            )
            item = payload["items"][0]
            self.assertEqual(item["id"], "访谈记录")
            self.assertEqual(item["basename"], "访谈记录")
            self.assertEqual(item["filename"], "访谈记录.md")
            self.assertEqual(item["title"], "访谈记录")
            self.assertEqual(item["summary"], "访谈暴露了失败反馈不清晰的问题。")
            self.assertEqual(item["type"], "inbox")
            self.assertEqual(item["status"], "待整理")
            self.assertEqual(item["updated"], "2026-09-05")
            self.assertEqual(item["tags"], ["用户研究"])
            self.assertEqual(item["metadata"]["type"], "inbox")
            self.assertEqual(item["wikilink"], "[[访谈记录]]")
            self.assertIn("用户需要更清晰的失败反馈。", item["content"])
            self.assertFalse(item["placeholder"])

            after = {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file() and ".note_runs" not in path.parts
            }
            self.assertEqual(before, after)

            _, with_placeholder = self.get("/api/vault/inbox?include_placeholder=true")
            self.assertEqual(with_placeholder["count"], 3)
            self.assertTrue(any(item["placeholder"] for item in with_placeholder["items"]))

    def test_vault_search_endpoint_matches_metadata_and_excludes_system_and_raw_evidence(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.initialize_vault(root)
            capability = root / "03-AI产品能力"
            capability.mkdir()
            (capability / "模型评测.md").write_text(
                "---\n"
                "type: capability\n"
                "status: 已验证\n"
                "updated: 2026-09-04\n"
                "tags: [LLM, Evaluation]\n"
                "owner: AI PM\n"
                "---\n"
                "# 模型评测\n\n"
                "LangGraph 评测需要记录召回率和失败案例。\n",
                encoding="utf-8",
            )
            (root / "杂项.md").write_text("# 杂项\n\n与检索无关。\n", encoding="utf-8")
            for directory_name in (".agents", ".obsidian", ".note_runs", "99-模板", "src"):
                directory = root / directory_name
                directory.mkdir(exist_ok=True)
                (directory / "不应出现.md").write_text("LangGraph hidden\n", encoding="utf-8")
            raw = root / "07-素材附件" / "原始资料"
            raw.mkdir(parents=True)
            (raw / "原始证据.md").write_text("LangGraph raw evidence\n", encoding="utf-8")
            before = {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file() and ".note_runs" not in path.parts
            }

            self.restart_server(root)
            status, payload = self.get("/api/vault/search?q=" + quote_plus("langgraph"))

            self.assertEqual(status, 200)
            self.assertEqual(payload["query"], "langgraph")
            self.assertEqual(payload["count"], len(payload["results"]))
            self.assertEqual(payload["results"], payload["items"])
            self.assertEqual(payload["results"], payload["notes"])
            self.assertEqual([item["path"] for item in payload["results"]], ["03-AI产品能力/模型评测.md"])
            result = payload["results"][0]
            self.assertIn("body", result["matched_fields"])
            self.assertIn("LangGraph", result["snippet"])
            self.assertEqual(result["metadata"]["owner"], "AI PM")
            self.assertFalse(Path(result["path"]).is_absolute())

            _, tag_matches = self.get("/api/vault/search?q=" + quote_plus("evaluation"))
            self.assertEqual(tag_matches["results"][0]["id"], result["id"])
            self.assertIn("tags", tag_matches["results"][0]["matched_fields"])

            _, case_insensitive = self.get("/api/vault/search?q=" + quote_plus("LANGGRAPH"))
            self.assertEqual(case_insensitive["results"][0]["id"], result["id"])

            _, all_notes = self.get("/api/vault/search?q=")
            all_paths = [item["path"] for item in all_notes["results"]]
            self.assertIn("03-AI产品能力/模型评测.md", all_paths)
            self.assertNotIn(".agents/不应出现.md", all_paths)
            self.assertNotIn(".obsidian/不应出现.md", all_paths)
            self.assertNotIn(".note_runs/不应出现.md", all_paths)
            self.assertNotIn("99-模板/不应出现.md", all_paths)
            self.assertNotIn("src/不应出现.md", all_paths)
            self.assertNotIn("07-素材附件/原始资料/原始证据.md", all_paths)
            self.assertNotIn("README.md", all_paths)
            self.assertEqual(all_paths, sorted(all_paths, key=lambda value: (value.casefold(), value)))

            after = {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file() and ".note_runs" not in path.parts
            }
            self.assertEqual(before, after)

            with self.assertRaises(HTTPError) as raised:
                self.get("/api/vault/search?q=" + quote_plus("x" * 201))
            self.assertEqual(raised.exception.code, 400)
            body = json.loads(raised.exception.read().decode("utf-8"))
            self.assertEqual(body["error"]["code"], "query_too_long")


if __name__ == "__main__":
    unittest.main()
