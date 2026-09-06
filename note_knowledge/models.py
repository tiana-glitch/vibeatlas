"""Data contracts for the multimodal note pipeline."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Tuple, TypedDict


def to_primitive(value: Any) -> Any:
    """Convert dataclasses and nested containers to JSON-compatible values."""

    if is_dataclass(value):
        return {key: to_primitive(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {key: to_primitive(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_primitive(item) for item in value]
    return value


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class NoteState(TypedDict, total=False):
    """LangGraph-compatible state shape shared by all four nodes."""

    run_id: str
    image_name: str
    image_mime: str
    image_size: int
    image_data: str
    user_note: str
    provided_text: str
    keywords: List[str]
    search_query: str
    retrieved_text: str
    retrieval_status: str
    retrieval_warning: str
    retrieval_sources: List[Dict[str, Any]]
    retrieved_at: str
    raw_ocr_text: str
    # ``raw_ocr_text`` is kept as the backwards-compatible, cleaned OCR value.
    # The fields below keep the provider response and a user correction separate.
    original_ocr_text: str
    ocr_source_text: str
    corrected_ocr_text: str
    effective_ocr_text: str
    ocr_revision: int
    ocr_correction_status: str
    ocr_corrected_at: str
    ocr_confidence_score: Optional[float]
    ocr_engine: str
    noise_content: str
    text_type: str
    low_confidence_segments: List[str]
    image_quality_issue: str
    primary_category: str
    sub_tags: List[str]
    topic_summary: str
    usage_scene: str
    condensed_text: str
    key_sentences: List[str]
    removed_redundancy: str
    archive_markdown: str
    retrieval_keywords: List[str]
    current_step: str
    retry_count: int
    errors: List[str]


@dataclass(frozen=True)
class ImageInput:
    """Normalized image payload. ``data`` is a base64 string without the prefix."""

    name: str = "note-image"
    mime: str = "image/jpeg"
    data: str = ""
    size: int = 0


@dataclass(frozen=True)
class OCRResult:
    raw_text: str
    noise_content: str = "无"
    text_type: str = "纸质拍照"
    low_confidence_segments: List[str] = field(default_factory=list)
    image_quality_issue: str = "无"
    # ``source_text`` is the unmodified provider/fallback response.  ``raw_text``
    # remains the cleaned value exposed by the original API contract.
    source_text: str = ""
    corrected_text: str = ""
    correction_status: str = "unreviewed"
    correction_updated_at: str = ""
    confidence_score: Optional[float] = None
    engine: str = ""

    @property
    def original_text(self) -> str:
        """Return the exact OCR response, falling back to legacy ``raw_text``."""

        return self.source_text or self.raw_text

    @property
    def effective_text(self) -> str:
        """Text that downstream classification and summarization should consume."""

        return self.corrected_text.strip() or self.raw_text

    @property
    def is_corrected(self) -> bool:
        return bool(self.corrected_text.strip())

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "原始提取文本": self.raw_text,
            "噪声内容": self.noise_content,
            "文本类型": self.text_type,
            "低置信度片段": list(self.low_confidence_segments),
            "图片质量问题": self.image_quality_issue,
            # New review-loop fields.  Existing consumers can continue reading
            # ``原始提取文本`` unchanged.
            "原始OCR文本": self.original_text,
            "原始识别文本": self.original_text,
            "校正后文本": self.corrected_text,
            "修订后文本": self.corrected_text,
            "当前有效文本": self.effective_text,
            "校正状态": self.correction_status,
            "校正时间": self.correction_updated_at,
            "OCR引擎": self.engine,
        }
        if self.confidence_score is not None:
            payload["置信度"] = self.confidence_score
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "OCRResult":
        """Reconstruct an OCR result from either legacy or review-loop JSON."""

        if not isinstance(payload, Mapping):
            raise TypeError("OCR payload must be a mapping")
        raw_text = str(payload.get("原始提取文本", payload.get("raw_text", "")) or "")
        source_text = str(
            payload.get(
                "原始OCR文本",
                payload.get("原始识别文本", payload.get("source_text", raw_text)),
            )
            or raw_text
        )
        corrected_text = str(
            payload.get("校正后文本", payload.get("修订后文本", payload.get("corrected_text", "")))
            or ""
        )
        confidence = payload.get("置信度", payload.get("confidence_score"))
        try:
            confidence_value = float(confidence) if confidence not in (None, "") else None
        except (TypeError, ValueError):
            confidence_value = None
        raw_segments = payload.get("低置信度片段", payload.get("low_confidence_segments", []))
        if isinstance(raw_segments, str):
            segments = [raw_segments] if raw_segments.strip() else []
        elif isinstance(raw_segments, (list, tuple)):
            segments = []
            for item in raw_segments:
                if isinstance(item, Mapping):
                    item = item.get("text", item.get("片段", item.get("segment", item.get("value", ""))))
                text = str(item).strip()
                if text and text not in segments:
                    segments.append(text)
        else:
            segments = [str(raw_segments)] if str(raw_segments).strip() else []
        return cls(
            raw_text=raw_text,
            noise_content=str(payload.get("噪声内容", payload.get("noise_content", "无")) or "无"),
            text_type=str(payload.get("文本类型", payload.get("text_type", "纸质拍照")) or "纸质拍照"),
            low_confidence_segments=segments,
            image_quality_issue=str(payload.get("图片质量问题", payload.get("image_quality_issue", "无")) or "无"),
            source_text=source_text,
            corrected_text=corrected_text,
            correction_status=str(payload.get("校正状态", payload.get("correction_status", "unreviewed")) or "unreviewed"),
            correction_updated_at=str(payload.get("校正时间", payload.get("correction_updated_at", "")) or ""),
            confidence_score=confidence_value,
            engine=str(payload.get("OCR引擎", payload.get("engine", "")) or ""),
        )


@dataclass(frozen=True)
class SearchSource:
    """A public source used to build a keyword-driven note."""

    title: str
    url: str
    snippet: str
    source: str = "web"
    retrieved_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "标题": self.title,
            "URL": self.url,
            "摘要": self.snippet,
            "来源": self.source,
            "检索时间": self.retrieved_at,
        }


@dataclass(frozen=True)
class ResearchResult:
    """Structured output of the optional keyword research stage."""

    keywords: List[str]
    query: str
    sources: List[SearchSource]
    research_text: str
    status: str = "completed"
    warning: str = "无"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "关键词": list(self.keywords),
            "检索查询": self.query,
            "来源列表": [source.to_dict() for source in self.sources],
            "检索文本": self.research_text,
            "检索状态": self.status,
            "提示": self.warning,
        }


@dataclass(frozen=True)
class LabelResult:
    primary_category: str
    sub_tags: List[str]
    topic_summary: str
    usage_scene: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "一级分类": self.primary_category,
            "二级标签": list(self.sub_tags),
            "一句话主题概括": self.topic_summary,
            "适用场景": self.usage_scene,
        }


@dataclass(frozen=True)
class SummaryResult:
    condensed_text: str
    key_sentences: List[str]
    removed_redundancy: str = "无"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "精简版要点文本": self.condensed_text,
            "重点短句": list(self.key_sentences),
            "冗余内容": self.removed_redundancy,
        }


@dataclass(frozen=True)
class ArchiveResult:
    title: str
    markdown: str
    retrieval_keywords: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "标题": self.title,
            "归档Markdown": self.markdown,
            "检索关键词": list(self.retrieval_keywords),
        }


@dataclass(frozen=True)
class NoteEvent:
    type: str
    run_id: str
    timestamp: str
    stage: Optional[str] = None
    attempt: int = 0
    duration_ms: Optional[int] = None
    error: Optional[str] = None
    model: str = "local-deterministic"
    prompt_version: str = "note-pipeline-v1"


@dataclass
class NoteRunRecord:
    id: str
    image_name: str
    image_mime: str
    image_size: int
    image_data: str
    user_note: str
    keywords: List[str]
    stage_order: Tuple[str, ...]
    status: str
    created_at: str
    updated_at: str
    outputs: Dict[str, Any] = field(default_factory=dict)
    events: List[NoteEvent] = field(default_factory=list)
    attempts: Dict[str, int] = field(default_factory=dict)
    execution_counts: Dict[str, int] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
    # Monotonically increasing revision used to reject stale editor writes.
    ocr_revision: int = 0
