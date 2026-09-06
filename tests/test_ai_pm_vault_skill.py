import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = ROOT / ".agents" / "skills" / "ai-pm-knowledge-vault"
INIT_SCRIPT = SKILL_DIR / "scripts" / "init_vault.py"
VALIDATE_SCRIPT = SKILL_DIR / "scripts" / "validate_vault.py"


class AIPMKnowledgeVaultSkillTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.vault = Path(self.temp_dir.name)
        result = subprocess.run(
            ["python3", str(INIT_SCRIPT), str(self.vault), "--profession", "AI 产品经理"],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def tearDown(self):
        self.temp_dir.cleanup()

    def validate(self, *extra):
        return subprocess.run(
            ["python3", str(VALIDATE_SCRIPT), str(self.vault), *extra],
            check=False,
            capture_output=True,
            text=True,
        )

    def write_knowledge_point(
        self,
        name,
        source_file,
        *,
        source_document="AI 产品评测指南",
        include_source_link=True,
        directory=None,
    ):
        target_dir = directory or (self.vault / "08-知识点")
        target_dir.mkdir(parents=True, exist_ok=True)
        source_line = f"- 来源文件：[[{source_file}]]\n" if include_source_link else ""
        note = target_dir / f"{name}.md"
        note.write_text(
            "\n".join(
                [
                    "---",
                    "type: knowledge-point",
                    "status: 待整理",
                    "evidence_status: 待核验",
                    f'source_document: "{source_document}"',
                    f'source_file: "{source_file}"',
                    "---",
                    "",
                    f"# {name}",
                    "",
                    "## 来源与证据",
                    "",
                    source_line.rstrip("\n"),
                    "- 证据定位：待补充",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        return note

    def index_knowledge_points(self, source_file, source_document, note_names):
        index = self.vault / "01-知识库目录.md"
        text = index.read_text(encoding="utf-8")
        placeholder = "| 待补充 | 待补充 | 待补充 |"
        note_links = "、".join(f"[[{name}]]" for name in note_names)
        source_row = f"| [[{source_file}]] | {source_document} | {note_links} |"
        point_rows = "\n".join(
            f"| [[{name}]] | {name}的检索摘要 | [[{source_file}]] |" for name in note_names
        )
        text = text.replace(placeholder, source_row, 1)
        text = text.replace(placeholder, point_rows, 1)
        index.write_text(text, encoding="utf-8")

    def append_ingest_log(self, source_file, note_names):
        log = self.vault / "02-更新流水账.md"
        note_links = "、".join(f"[[{name}]]" for name in note_names)
        log.write_text(
            log.read_text(encoding="utf-8")
            + f"\n| 2026-09-03 09:00 | 新增 | {note_links} | [[{source_file}]] | 待核验 |\n",
            encoding="utf-8",
        )

    def test_initialization_creates_knowledge_point_structure(self):
        self.assertTrue((self.vault / "08-知识点").is_dir())
        self.assertTrue((self.vault / "99-模板" / "知识点模板.md").is_file())

        result = self.validate()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_reinitialization_preserves_existing_notes(self):
        guide = self.vault / "00-知识库说明.md"
        original = guide.read_text(encoding="utf-8")
        guide.write_text(original + "\n用户自定义内容。\n", encoding="utf-8")

        result = subprocess.run(
            ["python3", str(INIT_SCRIPT), str(self.vault), "--profession", "AI 产品经理"],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("用户自定义内容。", guide.read_text(encoding="utf-8"))
        self.assertIn("EXISTS  00-知识库说明.md", result.stdout)

    def test_expected_note_requires_note_index_and_log(self):
        note_name = "多模态问答改写项目"
        note = self.vault / "02-项目案例" / "进行中项目" / f"{note_name}.md"
        note.write_text(f"# {note_name}\n", encoding="utf-8")

        missing_both = self.validate("--expect-note", note_name)
        self.assertNotEqual(missing_both.returncode, 0)
        self.assertIn("missing from 01-知识库目录.md", missing_both.stdout)
        self.assertIn("missing from 02-更新流水账.md", missing_both.stdout)

        index = self.vault / "01-知识库目录.md"
        index.write_text(index.read_text(encoding="utf-8") + f"\n- [[{note_name}]]\n", encoding="utf-8")
        missing_log = self.validate("--expect-note", note_name)
        self.assertNotEqual(missing_log.returncode, 0)
        self.assertNotIn("missing from 01-知识库目录.md", missing_log.stdout)
        self.assertIn("missing from 02-更新流水账.md", missing_log.stdout)

        log = self.vault / "02-更新流水账.md"
        log.write_text(
            log.read_text(encoding="utf-8")
            + f"\n| 2026-09-02 12:00 | 新增 | [[{note_name}]] | 用户口述 | 待核验 |\n",
            encoding="utf-8",
        )
        complete = self.validate("--expect-note", note_name)
        self.assertEqual(complete.returncode, 0, complete.stdout + complete.stderr)

    def test_path_style_wikilink_is_rejected(self):
        note = self.vault / "00-收件箱" / "错误链接示例.md"
        note.write_text("# 错误链接示例\n\n[[03-AI产品能力/AI与大模型]]\n", encoding="utf-8")

        result = self.validate()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("path-style wikilink", result.stdout)

    def test_project_note_must_use_a_status_directory(self):
        note = self.vault / "02-项目案例" / "错误层级项目.md"
        note.write_text("# 错误层级项目\n", encoding="utf-8")

        result = self.validate()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("misrouted note in 02-项目案例", result.stdout)

    def test_document_can_create_multiple_points_in_one_ingest_row(self):
        source_file = "AI产品评测指南.md"
        source = self.vault / "07-素材附件" / "原始资料" / source_file
        source.write_text(
            "# 原始文档\n\n[[不存在/但属于原文的链接]]\n\n[原文相对链接](另一页.md)\n",
            encoding="utf-8",
        )
        note_names = ["评测集需覆盖失败模式", "人工复核应记录分歧"]
        for name in note_names:
            self.write_knowledge_point(name, source_file)
        self.index_knowledge_points(source_file, "AI 产品评测指南", note_names)
        self.append_ingest_log(source_file, note_names)

        result = self.validate(
            "--expect-note",
            note_names[0],
            "--expect-note",
            note_names[1],
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_multiple_points_from_one_ingest_reject_multiple_log_rows(self):
        source_file = "多知识点来源.txt"
        (self.vault / "07-素材附件" / "原始资料" / source_file).write_text(
            "同一份原文。\n", encoding="utf-8"
        )
        note_names = ["知识点甲", "知识点乙"]
        for name in note_names:
            self.write_knowledge_point(name, source_file, source_document="多知识点来源")
        self.index_knowledge_points(source_file, "多知识点来源", note_names)
        self.append_ingest_log(source_file, note_names)
        log = self.vault / "02-更新流水账.md"
        log.write_text(
            log.read_text(encoding="utf-8")
            + f"| 2026-09-03 09:01 | 更新 | [[{note_names[0]}]] | [[{source_file}]] | 待核验 |\n",
            encoding="utf-8",
        )

        result = self.validate(
            "--expect-note",
            note_names[0],
            "--expect-note",
            note_names[1],
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must appear together in exactly one", result.stdout)

    def test_knowledge_point_requires_source_fields_and_source_wikilink(self):
        source_file = "来源材料.pdf"
        (self.vault / "07-素材附件" / "原始资料" / source_file).write_bytes(b"source")
        note = self.vault / "08-知识点" / "缺少来源信息.md"
        note.write_text(
            "---\ntype: knowledge-point\nsource_document: \"\"\nsource_file: \"\"\n---\n\n# 缺少来源信息\n",
            encoding="utf-8",
        )

        missing_fields = self.validate()

        self.assertNotEqual(missing_fields.returncode, 0)
        self.assertIn("missing grounded source_document", missing_fields.stdout)
        self.assertIn("missing source_file", missing_fields.stdout)

        note.unlink()
        self.write_knowledge_point("缺少来源回链", source_file, include_source_link=False)
        missing_link = self.validate()

        self.assertNotEqual(missing_link.returncode, 0)
        self.assertIn("missing source-file wikilink", missing_link.stdout)

    def test_knowledge_point_source_file_must_be_unique_original_evidence(self):
        self.write_knowledge_point("无原始文件", "不存在.pdf")

        missing_source = self.validate()

        self.assertNotEqual(missing_source.returncode, 0)
        self.assertIn("must resolve under 07-素材附件/原始资料", missing_source.stdout)

        (self.vault / "08-知识点" / "无原始文件.md").unlink()
        source_name = "同名来源.txt"
        (self.vault / "07-素材附件" / "原始资料" / source_name).write_text("原文", encoding="utf-8")
        (self.vault / "07-素材附件" / "图片" / source_name).write_text("另一文件", encoding="utf-8")
        self.write_knowledge_point("来源名不唯一", source_name)

        ambiguous_source = self.validate()

        self.assertNotEqual(ambiguous_source.returncode, 0)
        self.assertIn("source_file basename is ambiguous", ambiguous_source.stdout)

    def test_knowledge_point_must_be_routed_to_08_directory(self):
        source_file = "路由来源.txt"
        (self.vault / "07-素材附件" / "原始资料" / source_file).write_text("原文", encoding="utf-8")
        self.write_knowledge_point(
            "错误知识点目录",
            source_file,
            directory=self.vault / "00-收件箱",
        )

        result = self.validate()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("misrouted knowledge-point note", result.stdout)


if __name__ == "__main__":
    unittest.main()
