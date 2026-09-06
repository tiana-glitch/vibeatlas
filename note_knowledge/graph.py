"""Optional LangGraph adapter for the note pipeline.

Importing this module never requires LangGraph. ``build_note_graph`` returns a
compiled StateGraph when the package is installed and raises a clear error when
the optional dependency is absent, while ``NoteOrchestrator`` remains the
dependency-free runtime used by the web demo.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from .agents import ArchiveAgent, ClassifierAgent, OCRAgent, SummarizerAgent, normalize_image
from .models import ArchiveResult, ImageInput, LabelResult, NoteState, OCRResult, ResearchResult, SearchSource, SummaryResult
from .retrieval import ResearchAgent


def build_note_graph(agents: Optional[Mapping[str, Any]] = None) -> Any:
    try:
        from langgraph.graph import END, START, StateGraph
    except ImportError as exc:
        raise RuntimeError("LangGraph is not installed; use NoteOrchestrator or install langgraph") from exc

    configured = {
        "ocr": OCRAgent(),
        "classify": ClassifierAgent(),
        "summarize": SummarizerAgent(),
        "archive": ArchiveAgent(),
        "retrieve": ResearchAgent(),
    }
    if agents:
        configured.update(agents)

    def retrieve_node(state: NoteState) -> NoteState:
        keywords = state.get("keywords", [])
        if not keywords:
            return {"retrieval_status": "skipped"}
        output: ResearchResult = configured["retrieve"].run(keywords, state.get("user_note", ""), state.get("provided_text", ""))
        return {"search_query": output.query, "retrieved_text": output.research_text, "retrieval_status": output.status, "retrieval_warning": output.warning, "retrieval_sources": [source.to_dict() for source in output.sources], "retrieved_at": output.sources[0].retrieved_at if output.sources else ""}

    def ocr_node(state: NoteState) -> NoteState:
        image = ImageInput(name=state.get("image_name", "note-image"), mime=state.get("image_mime", "image/jpeg"), data=state.get("image_data", ""), size=state.get("image_size", 0))
        provided_text = state.get("provided_text", "") or state.get("retrieved_text", "")
        output: OCRResult = configured["ocr"].run(image, state.get("user_note", ""), provided_text)
        return {"raw_ocr_text": output.raw_text, "noise_content": output.noise_content, "text_type": output.text_type, "low_confidence_segments": output.low_confidence_segments, "image_quality_issue": output.image_quality_issue}

    def classify_node(state: NoteState) -> NoteState:
        output: LabelResult = configured["classify"].run(state["raw_ocr_text"], state.get("user_note", ""))
        return {"primary_category": output.primary_category, "sub_tags": output.sub_tags, "topic_summary": output.topic_summary, "usage_scene": output.usage_scene}

    def summarize_node(state: NoteState) -> NoteState:
        label = LabelResult(state.get("primary_category", "其他"), list(state.get("sub_tags", [])), state.get("topic_summary", ""), state.get("usage_scene", ""))
        output: SummaryResult = configured["summarize"].run(state["raw_ocr_text"], label)
        return {"condensed_text": output.condensed_text, "key_sentences": output.key_sentences, "removed_redundancy": output.removed_redundancy}

    def archive_node(state: NoteState) -> NoteState:
        ocr = OCRResult(state["raw_ocr_text"], state.get("noise_content", "无"), state.get("text_type", "纸质拍照"), list(state.get("low_confidence_segments", [])), state.get("image_quality_issue", "无"))
        label = LabelResult(state.get("primary_category", "其他"), list(state.get("sub_tags", [])), state.get("topic_summary", ""), state.get("usage_scene", ""))
        summary = SummaryResult(state["condensed_text"], list(state.get("key_sentences", [])), state.get("removed_redundancy", "无"))
        research = None
        if state.get("keywords") or state.get("retrieved_text"):
            sources = []
            for item in state.get("retrieval_sources", []):
                if not isinstance(item, Mapping):
                    continue
                sources.append(SearchSource(title=str(item.get("标题", item.get("title", ""))), url=str(item.get("URL", item.get("url", ""))), snippet=str(item.get("摘要", item.get("snippet", ""))), source=str(item.get("来源", item.get("source", "web"))), retrieved_at=str(item.get("检索时间", item.get("retrieved_at", "")))))
            research = ResearchResult(keywords=list(state.get("keywords", [])), query=str(state.get("search_query", "")), sources=sources, research_text=str(state.get("retrieved_text", "")), status=str(state.get("retrieval_status", "empty")), warning=str(state.get("retrieval_warning", "无")))
        output: ArchiveResult = configured["archive"].run(ocr, label, summary, research)
        return {"archive_markdown": output.markdown, "retrieval_keywords": output.retrieval_keywords, "current_step": "complete"}

    graph = StateGraph(NoteState)
    graph.add_node("retrieve", retrieve_node)
    graph.add_node("ocr", ocr_node)
    graph.add_node("classify", classify_node)
    graph.add_node("summarize", summarize_node)
    graph.add_node("archive", archive_node)
    graph.add_edge(START, "retrieve")
    graph.add_edge("retrieve", "ocr")
    graph.add_edge("ocr", "classify")
    graph.add_edge("classify", "summarize")
    graph.add_edge("summarize", "archive")
    graph.add_edge("archive", END)
    return graph.compile()


__all__ = ["build_note_graph"]
