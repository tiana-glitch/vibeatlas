import tempfile
import unittest
from pathlib import Path

from note_knowledge import NoteOrchestrator
from note_knowledge.agents import ArchiveAgent, ClassifierAgent, OCRAgent, SummarizerAgent
from note_knowledge.retrieval import ResearchAgent


TEXT = """水印：demo
LangGraph 使用 StateGraph 编排多Agent工作流。
每个 Agent 通过统一 State 传递中间结果。
LangGraph 使用 StateGraph 编排多Agent工作流。
"""


class CountingAgent:
    def __init__(self, delegate):
        self.delegate = delegate
        self.calls = 0

    def run(self, *args):
        self.calls += 1
        return self.delegate.run(*args)


class FlakyOCR:
    def __init__(self):
        self.calls = 0

    def run(self, *args):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("temporary OCR outage")
        return OCRAgent().run(*args)


class FakeSearchProvider:
    def __init__(self, results=None):
        self.results = results if results is not None else [
            {"title": "LangGraph 文档", "url": "https://langchain-ai.github.io/langgraph/", "snippet": "LangGraph 用图结构编排有状态的 Agent 工作流。", "source": "fake"},
            {"title": "StateGraph API", "url": "https://example.com/stategraph", "snippet": "StateGraph 定义节点、边和共享状态。", "source": "fake"},
        ]
        self.queries = []

    def search(self, query, limit=5):
        self.queries.append((query, limit))
        return self.results[:limit]


class FakeOCRProvider:
    def extract(self, image, user_note=""):
        return {
            "原始提取文本": "水印：provider\nPython 类型收窄可以减少运行时错误。\n???",
            "低置信度片段": ["Python 类型收窄可以减少运行时错误。"],
            "置信度": 82,
            "OCR引擎": "fake-vision",
        }


class NotePipelineTests(unittest.TestCase):
    def test_ocr_keeps_provider_text_separate_from_cleaned_text(self):
        result = OCRAgent(FakeOCRProvider()).run(b"image-bytes", user_note="技术学习")

        self.assertEqual(result.raw_text, "Python 类型收窄可以减少运行时错误。")
        self.assertIn("水印：provider", result.original_text)
        self.assertIn("???", result.original_text)
        self.assertEqual(result.corrected_text, "")
        self.assertEqual(result.confidence_score, 0.82)
        self.assertEqual(result.engine, "fake-vision")
        self.assertIn("Python 类型收窄可以减少运行时错误。", result.low_confidence_segments)
        self.assertIn("???", result.low_confidence_segments)

    def test_agents_return_strict_chinese_contracts(self):
        ocr = OCRAgent().run("", provided_text=TEXT)
        self.assertEqual(ocr.to_dict()["文本类型"], "文本粘贴")
        self.assertNotIn("水印：demo", ocr.raw_text)
        label = ClassifierAgent().run(ocr.raw_text)
        self.assertEqual(label.primary_category, "技术学习")
        summary = SummarizerAgent().run(ocr.raw_text, label)
        self.assertIn("LangGraph", summary.condensed_text)
        self.assertEqual(summary.condensed_text.count("LangGraph"), 1)
        archive = ArchiveAgent().run(ocr, label, summary)
        self.assertTrue(archive.markdown.startswith("---\n"))
        self.assertIn("## 检索关键词", archive.markdown)

    def test_pipeline_events_and_state(self):
        service = NoteOrchestrator()
        result = service.process(user_note="技术学习", provided_text=TEXT)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["progress"], 100)
        self.assertEqual(result["current_step"], "complete")
        self.assertEqual([event["type"] for event in result["events"]].count("stage.completed"), 4)
        self.assertEqual(result["state"]["primary_category"], "技术学习")

    def test_retry_only_executes_requested_stage(self):
        agents = {
            "ocr": CountingAgent(OCRAgent()),
            "classify": CountingAgent(ClassifierAgent()),
            "summarize": CountingAgent(SummarizerAgent()),
            "archive": CountingAgent(ArchiveAgent()),
        }
        service = NoteOrchestrator(agents)
        result = service.process(provided_text=TEXT)
        before = {name: agent.calls for name, agent in agents.items()}
        retried = service.retry_stage(result["run_id"], "summarize")
        self.assertEqual(agents["summarize"].calls, before["summarize"] + 1)
        self.assertEqual(agents["ocr"].calls, before["ocr"])
        self.assertEqual(agents["classify"].calls, before["classify"])
        self.assertEqual(agents["archive"].calls, before["archive"])
        self.assertEqual(retried["attempts"]["summarize"], 2)
        self.assertEqual([event["type"] for event in retried["events"][-3:]], ["stage.retry", "stage.started", "stage.completed"])

    def test_retry_failed_stage_resumes_missing_downstream_stages(self):
        flaky = FlakyOCR()
        service = NoteOrchestrator({"ocr": flaky})
        failed = service.process(provided_text=TEXT)
        self.assertEqual(failed["status"], "failed")
        recovered = service.retry_stage(failed["run_id"], "ocr")
        self.assertEqual(recovered["status"], "complete")
        self.assertEqual(recovered["execution_counts"]["ocr"], 2)
        self.assertEqual(recovered["execution_counts"]["archive"], 1)

    def test_ocr_correction_preserves_original_and_reruns_downstream(self):
        agents = {
            "ocr": CountingAgent(OCRAgent()),
            "classify": CountingAgent(ClassifierAgent()),
            "summarize": CountingAgent(SummarizerAgent()),
            "archive": CountingAgent(ArchiveAgent()),
        }
        service = NoteOrchestrator(agents)
        first = service.process(provided_text="原始识别错字。\n产品需求需要验证。")
        corrected = service.correct_ocr(first["run_id"], "修订后的产品需求结论。", expected_revision=0)

        self.assertEqual(corrected["status"], "complete")
        self.assertEqual(corrected["ocr_revision"], 1)
        self.assertEqual(corrected["state"]["raw_ocr_text"], "原始识别错字。\n产品需求需要验证。")
        self.assertEqual(corrected["state"]["original_ocr_text"], "原始识别错字。\n产品需求需要验证。")
        self.assertEqual(corrected["state"]["corrected_ocr_text"], "修订后的产品需求结论。")
        self.assertEqual(corrected["state"]["effective_ocr_text"], "修订后的产品需求结论。")
        self.assertEqual(corrected["outputs"]["classify"]["一句话主题概括"], "修订后的产品需求结论")
        self.assertEqual(agents["classify"].calls, 2)
        self.assertEqual(agents["summarize"].calls, 2)
        self.assertEqual(agents["archive"].calls, 2)
        self.assertIn("## 人工校正文本", corrected["archive_markdown"])
        self.assertIn("修订后的产品需求结论。", corrected["archive_markdown"])

    def test_ocr_correction_survives_reload_and_rejects_stale_revision(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            service = NoteOrchestrator(storage_dir=root)
            first = service.process(provided_text="OCR 原始内容")
            service.correct_ocr(first["run_id"], "人工校正内容", expected_revision=0)

            restored_service = NoteOrchestrator(storage_dir=root)
            restored = restored_service.get_run(first["run_id"])
            self.assertEqual(restored["ocr_revision"], 1)
            self.assertEqual(restored["state"]["original_ocr_text"], "OCR 原始内容")
            self.assertEqual(restored["state"]["corrected_ocr_text"], "人工校正内容")
            with self.assertRaises(ValueError):
                restored_service.correct_ocr(first["run_id"], "过期修改", expected_revision=0)
            self.assertEqual(restored_service.get_run(first["run_id"])["state"]["corrected_ocr_text"], "人工校正内容")

    def test_same_correction_can_finish_a_previous_partial_rerun(self):
        service = NoteOrchestrator()
        first = service.process(provided_text="产品需求需要验证")
        partial = service.correct_ocr(first["run_id"], "产品需求需要验证并记录决策", rerun_downstream=False)
        self.assertEqual(partial["status"], "partial")
        finished = service.correct_ocr(first["run_id"], "产品需求需要验证并记录决策", expected_revision=1)
        self.assertEqual(finished["status"], "complete")
        self.assertTrue(finished["ocr_correction"]["idempotent"])

    def test_run_metadata_survives_disk_reload(self):
        with tempfile.TemporaryDirectory() as folder:
            service = NoteOrchestrator(storage_dir=Path(folder))
            result = service.process(provided_text=TEXT, image_name="learning.png")
            restored = NoteOrchestrator(storage_dir=Path(folder)).get_run(result["run_id"])
            self.assertEqual(restored["run_id"], result["run_id"])
            self.assertEqual(restored["image"]["name"], "learning.png")
            self.assertEqual(restored["archive_markdown"], result["archive_markdown"])

    def test_keyword_only_research_becomes_a_cited_note(self):
        provider = FakeSearchProvider()
        service = NoteOrchestrator({"retrieve": ResearchAgent(provider)})
        result = service.process(keywords="LangGraph、StateGraph、Agent 工作流")
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["progress"], 100)
        self.assertEqual(result["stage_order"], ["retrieve", "ocr", "classify", "summarize", "archive"])
        self.assertEqual(result["keywords"], ["LangGraph", "StateGraph", "Agent 工作流"])
        self.assertEqual(result["outputs"]["retrieve"]["检索状态"], "completed")
        self.assertEqual(result["outputs"]["ocr"]["文本类型"], "关键词检索")
        self.assertEqual(len(result["state"]["retrieval_sources"]), 2)
        self.assertIn("## 公开检索来源", result["archive_markdown"])
        self.assertIn("https://langchain-ai.github.io/langgraph/", result["archive_markdown"])
        self.assertEqual(provider.queries[0][0], "LangGraph StateGraph Agent 工作流")

    def test_keyword_research_failure_does_not_forge_sources(self):
        service = NoteOrchestrator({"retrieve": ResearchAgent(FakeSearchProvider([]))})
        result = service.process(keywords=["unknown topic"])
        research = result["outputs"]["retrieve"]
        self.assertEqual(research["检索状态"], "empty")
        self.assertEqual(research["来源列表"], [])
        self.assertIn("暂无可引用来源", result["archive_markdown"])


if __name__ == "__main__":
    unittest.main()
