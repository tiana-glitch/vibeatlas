import json
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from server import build_server


def _init_vault(root: Path) -> None:
    (root / "00-收件箱").mkdir()
    (root / "00-知识库说明.md").write_text(
        "# 说明\n\n内部链接使用 `[[文件名]]`。\n"
        "每次写成一篇或多篇知识笔记，按来源和按知识点更新目录，并在流水账写唯一一行。\n",
        encoding="utf-8",
    )
    (root / "01-知识库目录.md").write_text(
        "---\ntype: index\nupdated: 2026-09-02\n---\n\n# 知识库目录\n",
        encoding="utf-8",
    )
    (root / "02-更新流水账.md").write_text(
        "---\ntype: log\nupdated: 2026-09-02\n---\n\n# 更新流水账\n\n"
        "| 时间 | 动作 | 笔记 | 来源 | 状态 |\n| --- | --- | --- | --- | --- |\n",
        encoding="utf-8",
    )
    (root / "00-收件箱" / "会议.md").write_text(
        "---\ntitle: 会议记录\ntype: inbox\n---\n\n# 会议记录\n\n先验证用户问题。\n",
        encoding="utf-8",
    )


class PromotionEndpointTests(unittest.TestCase):
    def setUp(self):
        self.folder = TemporaryDirectory()
        self.root = Path(self.folder.name)
        _init_vault(self.root)
        self.http_server = build_server(port=0, directory=self.root)
        self.thread = threading.Thread(target=self.http_server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://%s:%d" % self.http_server.server_address

    def tearDown(self):
        self.http_server.shutdown()
        self.http_server.server_close()
        self.thread.join(timeout=3)
        self.folder.cleanup()

    def post(self, path, payload):
        request = Request(
            self.base + path,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=3) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def get(self, path):
        with urlopen(self.base + path, timeout=3) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def test_new_proposal_is_read_only_and_commit_is_idempotent(self):
        before = {path.relative_to(self.root).as_posix(): path.read_bytes() for path in self.root.rglob("*.md")}
        status, proposal = self.post(
            "/api/vault/promotion/propose",
            {
                "proposal_id": "promotion-new-1",
                "source_path": "00-收件箱/会议.md",
                "mode": "new",
                "target_name": "用户问题验证",
                "title": "用户问题验证方法",
                "content": "先验证用户问题，再设计功能。",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(proposal["proposal_id"], "promotion-new-1")
        self.assertEqual(proposal["mode"], "new")
        self.assertIn("diff", proposal)
        self.assertEqual(set(proposal["hashes"]), {"source_sha256", "target_sha256", "target_dir_sha256", "index_sha256", "log_sha256"})
        self.assertEqual(before, {path.relative_to(self.root).as_posix(): path.read_bytes() for path in self.root.rglob("*.md")})

        status, committed = self.post(
            "/api/vault/promotion/commit",
            {"proposal_id": "promotion-new-1", "expected_hashes": proposal["hashes"]},
        )
        self.assertEqual(status, 201)
        self.assertFalse(committed["idempotent"])
        target_path = self.root / committed["target"]["path"]
        self.assertTrue(target_path.is_file())
        self.assertTrue(committed["target"]["path"].startswith("08-知识点/"))
        target_text = target_path.read_text(encoding="utf-8")
        self.assertIn("type: knowledge-point", target_text)
        self.assertIn('source_path: "00-收件箱/会议.md"', target_text)
        snapshot_path = self.root / committed["source"]["snapshot_path"]
        self.assertTrue(snapshot_path.is_file())
        self.assertEqual(snapshot_path.read_bytes(), (self.root / "00-收件箱" / "会议.md").read_bytes())
        self.assertIn('source_snapshot_path: "%s"' % committed["source"]["snapshot_path"], target_text)
        self.assertIn("[[会议]]", target_text)
        self.assertNotIn("[[00-收件箱/会议.md]]", target_text)
        index_text = (self.root / "01-知识库目录.md").read_text(encoding="utf-8")
        snapshot_name = snapshot_path.name
        self.assertIn("### 按来源", index_text)
        self.assertIn("### 按知识点", index_text)
        self.assertIn("[[%s]]" % snapshot_name, index_text)
        self.assertIn("[[%s]]" % committed["target"]["name"], index_text)
        self.assertIn(
            "[[%s]]" % snapshot_name,
            next(row for row in index_text.splitlines() if "[[%s]]" % committed["target"]["name"] in row),
        )
        log_path = self.root / "02-更新流水账.md"
        self.assertEqual(log_path.read_text(encoding="utf-8").count("[[用户问题验证]]"), 1)

        validator = Path(__file__).resolve().parents[1] / ".agents/skills/ai-pm-knowledge-vault/scripts/validate_vault.py"
        validation = subprocess.run(
            [sys.executable, str(validator), str(self.root), "--expect-note", committed["target"]["name"]],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(validation.returncode, 0, validation.stdout + validation.stderr)

        graph_status, graph = self.get("/api/knowledge/graph")
        self.assertEqual(graph_status, 200)
        self.assertIn(committed["target"]["name"], {node["id"] for node in graph["knowledge_nodes"]})

        status, retry = self.post("/api/vault/promotion/commit", {"proposal_id": "promotion-new-1"})
        self.assertEqual(status, 200)
        self.assertTrue(retry["idempotent"])
        self.assertEqual(log_path.read_text(encoding="utf-8").count("[[用户问题验证]]"), 1)

    def test_append_returns_diff_and_rejects_stale_target(self):
        target = self.root / "04-经验与方法" / "已有方法.md"
        target.parent.mkdir()
        target.write_text("# 已有方法\n\n原有结论。\n", encoding="utf-8")
        status, proposal = self.post(
            "/api/vault/propose",
            {
                "proposal_id": "promotion-append-1",
                "source_path": "00-收件箱/会议.md",
                "mode": "append",
                "target_path": "04-经验与方法/已有方法.md",
                "content": "新增结论。",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(proposal["mode"], "append")
        self.assertIn("原有结论。", proposal["before"])
        self.assertIn("新增结论。", proposal["after"])
        target.write_text(target.read_text(encoding="utf-8") + "\n外部编辑。\n", encoding="utf-8")
        with self.assertRaises(HTTPError) as raised:
            self.post(
                "/api/vault/promotion/commit",
                {"proposal_id": "promotion-append-1", "expected_hashes": proposal["hashes"]},
            )
        self.assertEqual(raised.exception.code, 409)
        self.assertIn("外部编辑。", target.read_text(encoding="utf-8"))
        self.assertEqual((self.root / "02-更新流水账.md").read_text(encoding="utf-8").count("promotion-append-1"), 0)

    def test_path_and_mode_validation_does_not_write(self):
        with self.assertRaises(HTTPError) as raised:
            self.post(
                "/api/vault/promotion/propose",
                {"source_path": "../secret.md", "mode": "new", "content": "内容"},
            )
        self.assertEqual(raised.exception.code, 400)
        with self.assertRaises(HTTPError) as raised:
            self.post(
                "/api/vault/promotion/propose",
                {"source_path": "00-收件箱/会议.md", "mode": "merge", "content": "内容"},
            )
        self.assertEqual(raised.exception.code, 400)
        self.assertFalse((self.root / "04-经验与方法").exists())

    def test_append_from_two_sources_keeps_both_index_entries(self):
        target = self.root / "04-经验与方法" / "已有方法.md"
        target.parent.mkdir()
        target.write_text("# 已有方法\n\n原有结论。\n", encoding="utf-8")
        second_source = self.root / "00-收件箱" / "第二次会议.md"
        second_source.write_text("# 第二次会议\n\n补充结论。\n", encoding="utf-8")

        for proposal_id, source_path, content in (
            ("promotion-append-a", "00-收件箱/会议.md", "第一次补充。"),
            ("promotion-append-b", "00-收件箱/第二次会议.md", "第二次补充。"),
        ):
            _, proposal = self.post(
                "/api/vault/promotion/propose",
                {
                    "proposal_id": proposal_id,
                    "source_path": source_path,
                    "mode": "append",
                    "target_path": "04-经验与方法/已有方法.md",
                    "content": content,
                },
            )
            status, _ = self.post(
                "/api/vault/promotion/commit",
                {"proposal_id": proposal_id, "expected_hashes": proposal["hashes"]},
            )
            self.assertEqual(status, 201)

        index_text = (self.root / "01-知识库目录.md").read_text(encoding="utf-8")
        self.assertEqual(
            sum(
                "[[已有方法]]" in line and "来源：[[会议]]" in line
                for line in index_text.splitlines()
            ),
            1,
        )
        self.assertEqual(
            sum(
                "[[已有方法]]" in line and "来源：[[第二次会议]]" in line
                for line in index_text.splitlines()
            ),
            1,
        )

    def test_source_basename_with_wikilink_syntax_is_rejected(self):
        unsafe = self.root / "00-收件箱" / "会议#内部.md"
        unsafe.write_text("# 会议\n\n内容。\n", encoding="utf-8")
        with self.assertRaises(HTTPError) as raised:
            self.post(
                "/api/vault/promotion/propose",
                {"source_path": "00-收件箱/会议#内部.md", "mode": "new", "content": "内容。"},
            )
        self.assertEqual(raised.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
