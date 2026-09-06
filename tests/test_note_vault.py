import tempfile
import unittest
from pathlib import Path

from note_knowledge.vault import NoteVaultCommitter, NoteVaultError


def initialize_vault(root: Path) -> None:
    (root / "00-收件箱").mkdir()
    (root / "00-知识库说明.md").write_text(
        "# 知识库说明\n\n内部链接使用 [[文件名]]。\n"
        "每次收录写笔记、更新目录并在流水账写唯一一行。\n",
        encoding="utf-8",
    )
    (root / "01-知识库目录.md").write_text(
        "---\ntype: index\nupdated: 2026-09-02\n---\n\n"
        "# 知识库目录\n\n## 快速入口\n\n- [[00-收件箱]]\n",
        encoding="utf-8",
    )
    (root / "02-更新流水账.md").write_text(
        "---\ntype: log\nupdated: 2026-09-02\n---\n\n# 更新流水账\n\n"
        "| 时间 | 动作 | 笔记 | 来源 | 状态 |\n"
        "| --- | --- | --- | --- | --- |\n",
        encoding="utf-8",
    )


class NoteVaultCommitterTests(unittest.TestCase):
    def test_propose_does_not_mutate_vault(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            initialize_vault(root)
            before = {path: path.read_bytes() for path in root.rglob("*.md")}
            proposal = NoteVaultCommitter(root).propose(
                "note_demo_1",
                "---\ntitle: 会议结论\ncategory: 产品\n---\n\n# 会议结论\n\n- 先验证用户问题。\n",
                source_label="会议截图",
            )
            self.assertEqual(proposal["status"], "awaiting_review")
            self.assertIn("source_run_id: \"note_demo_1\"", proposal["proposal"]["markdown"])
            self.assertEqual(before, {path: path.read_bytes() for path in root.rglob("*.md")})

    def test_commit_writes_note_index_and_one_log_row(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            initialize_vault(root)
            service = NoteVaultCommitter(root)
            proposal = service.propose(
                "note_demo_2",
                "# 需求评审\n\n## 核心要点\n\n- 先验证用户问题。\n",
                source_label="产品评审文字",
            )
            result = service.commit(
                "note_demo_2",
                proposal["proposal"]["markdown"],
                title=proposal["note"]["title"],
                source_label="产品评审文字",
                expected_index_sha256=proposal["proposal"]["index_sha256"],
                expected_log_sha256=proposal["proposal"]["log_sha256"],
                note_name=proposal["note"]["name"],
            )
            self.assertEqual(result["status"], "committed")
            self.assertFalse(result["idempotent"])
            note_path = root / result["note"]["path"]
            self.assertTrue(note_path.is_file())
            note_text = note_path.read_text(encoding="utf-8")
            self.assertIn("type: inbox", note_text)
            self.assertIn('source_run_id: "note_demo_2"', note_text)
            index = (root / "01-知识库目录.md").read_text(encoding="utf-8")
            log = (root / "02-更新流水账.md").read_text(encoding="utf-8")
            self.assertIn("[[%s]]" % result["note"]["name"], index)
            self.assertEqual(log.count("[[%s]]" % result["note"]["name"]), 1)
            self.assertEqual(result["transaction"]["log_rows_added"], 1)

    def test_repeated_commit_is_idempotent_and_adds_no_second_log_row(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            initialize_vault(root)
            service = NoteVaultCommitter(root)
            markdown = "# 幂等测试\n\n- 内容只写一次。\n"
            first = service.commit("note_demo_3", markdown)
            log_before = (root / "02-更新流水账.md").read_text(encoding="utf-8")
            # A retry can carry only the idempotency key; the server may have
            # lost the browser's draft after the first response.
            second = service.commit("note_demo_3")
            log_after = (root / "02-更新流水账.md").read_text(encoding="utf-8")
            self.assertTrue(second["idempotent"])
            self.assertEqual(second["note"], first["note"])
            self.assertEqual(second["transaction"]["log_rows_added"], 0)
            self.assertEqual(log_after, log_before)

    def test_stale_preview_is_rejected_without_writing_note(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            initialize_vault(root)
            service = NoteVaultCommitter(root)
            proposal = service.propose("note_demo_4", "# 过期预览\n\n内容。")
            index_path = root / "01-知识库目录.md"
            index_path.write_text(index_path.read_text(encoding="utf-8") + "\n外部编辑。\n", encoding="utf-8")
            with self.assertRaises(NoteVaultError) as raised:
                service.commit(
                    "note_demo_4",
                    proposal["proposal"]["markdown"],
                    expected_index_sha256=proposal["proposal"]["index_sha256"],
                    expected_log_sha256=proposal["proposal"]["log_sha256"],
                    note_name=proposal["note"]["name"],
                )
            self.assertEqual(raised.exception.code, "vault_changed")
            self.assertFalse(list((root / "00-收件箱").glob("过期预览*.md")))

    def test_path_style_wikilink_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            initialize_vault(root)
            with self.assertRaises(NoteVaultError) as raised:
                NoteVaultCommitter(root).propose("note_demo_5", "# Bad\n\nSee [[03-AI产品能力/产品设计]].")
            self.assertEqual(raised.exception.code, "path_style_wikilink")


if __name__ == "__main__":
    unittest.main()
