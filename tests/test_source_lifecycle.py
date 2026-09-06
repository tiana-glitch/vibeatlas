import tempfile
import unittest
from pathlib import Path
from unittest import mock

from note_knowledge.source_lifecycle import (
    RAW_SOURCE_DIRECTORY,
    SOURCE_ACTION_ARCHIVE,
    SourceLifecycleManager,
    SOURCE_STATUS_ACTIVE,
    SOURCE_STATUS_ARCHIVED,
)
from note_knowledge.vault import NoteVaultError


def _initialize_source_vault(root: Path) -> dict:
    summary = root / "07-素材附件/来源摘要/会议"
    raw = root / RAW_SOURCE_DIRECTORY
    points = root / "08-知识点/会议"
    summary.mkdir(parents=True)
    raw.mkdir(parents=True)
    points.mkdir(parents=True)
    (root / "01-知识库目录.md").write_text(
        "---\ntype: index\nupdated: 2026-09-05\n---\n\n# 知识库目录\n",
        encoding="utf-8",
    )
    (root / "02-更新流水账.md").write_text(
        "---\ntype: log\nupdated: 2026-09-05\n---\n\n# 更新流水账\n\n"
        "| 时间 | 动作 | 笔记 | 来源 | 状态 |\n"
        "| --- | --- | --- | --- | --- |\n",
        encoding="utf-8",
    )
    raw_path = raw / "会议纪要.md"
    raw_bytes = "# original\r\n会议证据\r\n".encode("utf-8")
    raw_path.write_bytes(raw_bytes)
    summary_path = summary / "会议纪要-来源摘要.md"
    summary_path.write_text(
        "---\n"
        "type: source-summary\n"
        "status: 待核验\n"
        "source_document: \"会议纪要.md\"\n"
        "source_file: \"会议纪要.md\"\n"
        "source_path: \"07-素材附件/原始资料/会议纪要.md\"\n"
        "graph_status: active\n"
        "---\n\n"
        "# 会议纪要\n\n内容概览。\n",
        encoding="utf-8",
    )
    point_paths = []
    for index, title in enumerate(("结论甲", "方法乙"), start=1):
        path = points / ("会议纪要-知识点-%02d" % index + ".md")
        path.write_text(
            "---\n"
            "type: knowledge-point\n"
            "title: \"%s\"\n"
            "graph_status: active\n"
            "source_summary: \"会议纪要-来源摘要\"\n"
            "source_file: \"会议纪要.md\"\n"
            "source_path: \"07-素材附件/原始资料/会议纪要.md\"\n"
            "---\n\n# %s\n\n证据。\n"
            % (title, title),
            encoding="utf-8",
        )
        point_paths.append(path)
    return {
        "summary": summary_path,
        "raw": raw_path,
        "points": point_paths,
        "raw_bytes": raw_bytes,
    }


class SourceLifecycleManagerTests(unittest.TestCase):
    def test_propose_is_read_only_and_exposes_impact_and_snapshot(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            _initialize_source_vault(root)
            before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}

            proposal = SourceLifecycleManager(root).propose(
                SOURCE_ACTION_ARCHIVE,
                source_id="会议纪要-来源摘要",
                operation_id="archive-1",
            )

            self.assertEqual(proposal["status"], "awaiting_review")
            self.assertEqual(proposal["action"], "archive")
            self.assertEqual(proposal["impact"]["knowledge_points"], 2)
            self.assertEqual(proposal["impact"]["raw_attachments"], 1)
            self.assertEqual(proposal["source"]["source_status"], SOURCE_STATUS_ACTIVE)
            self.assertFalse(proposal["idempotent"])
            self.assertTrue(proposal["snapshot"]["sha256"])
            roles = {item["role"] for item in proposal["snapshot"]["files"]}
            self.assertEqual(roles, {"source-summary", "knowledge-point", "raw-attachment", "index", "operation-log"})
            self.assertIn("source_status: archived", proposal["diff"])
            after = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
            self.assertEqual(before, after)

    def test_archive_updates_statuses_index_and_log_but_preserves_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            files = _initialize_source_vault(root)
            manager = SourceLifecycleManager(root)
            proposal = manager.propose("archive", source_path="07-素材附件/原始资料/会议纪要.md", operation_id="archive-2")
            result = manager.commit(proposal_id="archive-2", expected_hashes=proposal["hashes"])

            self.assertEqual(result["status"], "committed")
            self.assertFalse(result["idempotent"])
            self.assertEqual(result["source"]["source_status"], SOURCE_STATUS_ARCHIVED)
            self.assertEqual(result["impact"]["archived_knowledge_points"], 2)
            self.assertEqual(files["raw"].read_bytes(), files["raw_bytes"])
            summary = files["summary"].read_text(encoding="utf-8")
            self.assertIn("source_status: archived", summary)
            self.assertIn("graph_status: hidden", summary)
            for point in files["points"]:
                self.assertIn("source_status: archived", point.read_text(encoding="utf-8"))
            index = (root / "01-知识库目录.md").read_text(encoding="utf-8")
            self.assertIn("## 来源生命周期", index)
            self.assertIn("[[会议纪要-来源摘要]]：已归档", index)
            log = (root / "02-更新流水账.md").read_text(encoding="utf-8")
            self.assertEqual(log.count("<!-- source-lifecycle-id: archive-2 -->"), 1)
            self.assertEqual(result["transaction"]["log_rows_added"], 1)

            retry = manager.commit(proposal_id="archive-2", action="archive", source_id="会议纪要-来源摘要")
            self.assertTrue(retry["idempotent"])
            self.assertEqual(
                (root / "02-更新流水账.md").read_text(encoding="utf-8").count("<!-- source-lifecycle-id: archive-2 -->"),
                1,
            )

    def test_restore_reactivates_summary_and_points_without_touching_body_or_raw(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            files = _initialize_source_vault(root)
            manager = SourceLifecycleManager(root)
            archived = manager.archive(source_id="会议纪要-来源摘要")
            self.assertEqual(archived["source"]["source_status"], SOURCE_STATUS_ARCHIVED)
            point_bodies_before = [path.read_text(encoding="utf-8").split("---", 2)[-1] for path in files["points"]]

            proposal = manager.propose("restore", source_id="会议纪要-来源摘要", operation_id="restore-1")
            result = manager.commit(proposal_id="restore-1", expected_hashes=proposal["hashes"])

            self.assertEqual(result["source"]["source_status"], SOURCE_STATUS_ACTIVE)
            self.assertEqual(result["source"]["graph_status"], "active")
            self.assertEqual(files["raw"].read_bytes(), files["raw_bytes"])
            self.assertIn("source_status: active", files["summary"].read_text(encoding="utf-8"))
            for path, before_body in zip(files["points"], point_bodies_before):
                text = path.read_text(encoding="utf-8")
                self.assertIn("source_status: active", text)
                self.assertEqual(text.split("---", 2)[-1], before_body)
            index = (root / "01-知识库目录.md").read_text(encoding="utf-8")
            self.assertIn("[[会议纪要-来源摘要]]：已恢复", index)
            log = (root / "02-更新流水账.md").read_text(encoding="utf-8")
            self.assertIn("<!-- source-lifecycle-id: restore-1 -->", log)

    def test_stale_hash_rejects_without_partial_writes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            files = _initialize_source_vault(root)
            manager = SourceLifecycleManager(root)
            proposal = manager.propose("archive", source_id="会议纪要-来源摘要", operation_id="archive-stale")
            files["points"][0].write_text(files["points"][0].read_text(encoding="utf-8") + "外部编辑。\n", encoding="utf-8")
            changed = files["points"][0].read_bytes()

            with self.assertRaises(NoteVaultError) as raised:
                manager.commit(proposal_id="archive-stale", expected_hashes=proposal["hashes"])
            self.assertEqual(raised.exception.code, "source_lifecycle_stale")
            self.assertEqual(files["summary"].read_text(encoding="utf-8").count("source_status"), 0)
            self.assertEqual(files["points"][0].read_bytes(), changed)
            self.assertNotIn("source-lifecycle-id", (root / "02-更新流水账.md").read_text(encoding="utf-8"))

    def test_failed_replacement_rolls_back_all_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            _initialize_source_vault(root)
            manager = SourceLifecycleManager(root)
            proposal = manager.propose("archive", source_id="会议纪要-来源摘要", operation_id="archive-rollback")
            before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
            import note_knowledge.source_lifecycle as lifecycle
            original_replace = lifecycle._atomic_replace_bytes
            calls = {"count": 0}

            def fail_on_second(path, value):
                calls["count"] += 1
                if calls["count"] == 2:
                    raise OSError("simulated disk failure")
                return original_replace(path, value)

            with mock.patch("note_knowledge.source_lifecycle._atomic_replace_bytes", side_effect=fail_on_second):
                with self.assertRaises(NoteVaultError) as raised:
                    manager.commit(proposal_id="archive-rollback", expected_hashes=proposal["hashes"])
            self.assertEqual(raised.exception.code, "vault_write_error")
            after = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
            self.assertEqual(before, after)

    def test_raw_selector_is_supported_but_delete_is_not(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            _initialize_source_vault(root)
            manager = SourceLifecycleManager(root)
            inspection = manager.inspect(source_path="07-素材附件/原始资料/会议纪要.md")
            self.assertEqual(inspection["source"]["id"], "会议纪要-来源摘要")
            with self.assertRaises(NoteVaultError) as raised:
                manager.propose("delete", source_id="会议纪要-来源摘要")
            self.assertEqual(raised.exception.code, "permanent_delete_unsupported")

    def test_non_markdown_raw_attachment_selector_and_hash_are_supported(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            files = _initialize_source_vault(root)
            pdf = files["raw"].with_name("会议纪要.pdf")
            files["raw"].rename(pdf)
            summary = files["summary"].read_text(encoding="utf-8").replace(
                "会议纪要.md", "会议纪要.pdf"
            )
            files["summary"].write_text(summary, encoding="utf-8")
            for point in files["points"]:
                point.write_text(
                    point.read_text(encoding="utf-8").replace("会议纪要.md", "会议纪要.pdf"),
                    encoding="utf-8",
                )

            inspection = SourceLifecycleManager(root).inspect(
                source_path="07-素材附件/原始资料/会议纪要.pdf"
            )
            self.assertEqual(inspection["impact"]["raw_attachments"], 1)
            self.assertEqual(inspection["source"]["source_file"], "会议纪要.pdf")


if __name__ == "__main__":
    unittest.main()
