import json
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from urllib.parse import quote

from server import build_server


def initialize(root: Path) -> None:
    (root / "00-收件箱").mkdir()
    (root / "07-素材附件" / "原始资料").mkdir(parents=True)
    (root / ".agents").mkdir()
    (root / "00-知识库说明.md").write_text(
        "# 说明\n\n内部链接使用 [[文件名]]。\n每次收录写笔记、更新目录并在流水账记录唯一一行。\n",
        encoding="utf-8",
    )
    (root / "01-知识库目录.md").write_text("# 目录\n", encoding="utf-8")
    (root / "02-更新流水账.md").write_text("# 流水账\n", encoding="utf-8")
    (root / "00-收件箱" / "会议记录.md").write_text(
        "---\ntitle: 会议记录\ntype: inbox\nstatus: 待整理\ntags: [用户研究]\n---\n\n# 会议记录\n\n"
        "失败反馈需要让用户知道下一步。\n",
        encoding="utf-8",
    )
    (root / "00-收件箱" / "待整理内容.md").write_text("# 占位\n", encoding="utf-8")
    (root / "04-经验与方法" ).mkdir()
    (root / "04-经验与方法" / "反馈方法.md").write_text(
        "---\ntype: method-index\ntags: [用户研究]\n---\n\n# 反馈方法\n\n失败反馈的可见性。\n",
        encoding="utf-8",
    )
    (root / "07-素材附件" / "原始资料" / "隐藏证据.md").write_text("秘密", encoding="utf-8")
    (root / ".agents" / "内部.md").write_text("不应返回", encoding="utf-8")


class VaultBrowseEndpointTests(unittest.TestCase):
    def setUp(self):
        self.folder = TemporaryDirectory()
        self.root = Path(self.folder.name)
        initialize(self.root)
        self.server = build_server(port=0, directory=self.root)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://%s:%d" % self.server.server_address

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.folder.cleanup()

    def get(self, path):
        with urlopen(self.base + path, timeout=3) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def test_inbox_is_read_only_and_hides_placeholder(self):
        before = {path.relative_to(self.root).as_posix(): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        status, payload = self.get("/api/vault/inbox")
        self.assertEqual(status, 200)
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["items"][0]["basename"], "会议记录")
        self.assertEqual(payload["items"][0]["title"], "会议记录")
        self.assertIn("失败反馈", payload["items"][0]["preview"])
        after = {path.relative_to(self.root).as_posix(): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)

    def test_search_matches_body_and_excludes_system_and_raw_evidence(self):
        status, payload = self.get("/api/vault/search?q=" + quote("失败反馈"))
        self.assertEqual(status, 200)
        paths = [item["path"] for item in payload["items"]]
        self.assertIn("00-收件箱/会议记录.md", paths)
        self.assertIn("04-经验与方法/反馈方法.md", paths)
        self.assertNotIn("07-素材附件/原始资料/隐藏证据.md", paths)
        self.assertNotIn("00-知识库说明.md", paths)
        self.assertTrue(all("body" not in item for item in payload["items"]))
        self.assertTrue(all(item["matched_fields"] for item in payload["items"]))

    def test_empty_search_lists_candidates_and_long_query_is_rejected(self):
        status, payload = self.get("/api/vault/search?q=")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(payload["total_count"], 2)
        self.assertEqual(payload["count"], len(payload["items"]))
        candidate_paths = [item["path"] for item in payload["items"]]
        self.assertNotIn("00-知识库说明.md", candidate_paths)
        self.assertNotIn("01-知识库目录.md", candidate_paths)
        self.assertNotIn("02-更新流水账.md", candidate_paths)
        self.assertNotIn("00-收件箱/待整理内容.md", candidate_paths)
        with self.assertRaises(HTTPError) as raised:
            self.get("/api/vault/search?q=" + ("x" * 201))
        self.assertEqual(raised.exception.code, 400)
        error = json.loads(raised.exception.read().decode("utf-8"))
        self.assertEqual(error["error"]["code"], "query_too_long")


if __name__ == "__main__":
    unittest.main()
