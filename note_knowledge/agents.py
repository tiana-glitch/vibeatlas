"""Four deterministic note agents with stable JSON-shaped outputs.

The deterministic implementation is intentionally useful without an API key:
the user can paste OCR text as a fallback, and the same contracts can later be
backed by a real Wenxin multimodal provider.
"""

from __future__ import annotations

import base64
import hashlib
import mimetypes
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Protocol, Sequence, Tuple

from .models import ArchiveResult, ImageInput, LabelResult, OCRResult, ResearchResult, SummaryResult


class OCRProvider(Protocol):
    def extract(self, image: ImageInput, user_note: str = "") -> Mapping[str, Any]: ...


ALLOWED_CATEGORIES = ("面试", "技术学习", "业务产品", "读书感悟", "生活记录", "其他")
TAG_TAXONOMY: Dict[str, Tuple[str, ...]] = {
    "面试": ("八股", "项目介绍", "行为面试", "算法", "复盘"),
    "技术学习": ("Python", "JavaScript", "TypeScript", "前端", "后端", "数据库", "机器学习", "大模型", "LangGraph", "提示词工程"),
    "业务产品": ("需求分析", "用户研究", "竞品分析", "用户增长", "产品设计", "项目管理", "数据分析"),
    "读书感悟": ("方法论", "管理", "心理学", "摘录", "行动清单"),
    "生活记录": ("计划", "日记", "旅行", "清单", "健康"),
    "其他": ("待整理",),
}


_NOISE_PATTERNS = (
    re.compile(r"^(水印|watermark|logo|扫码|长按识别|版权所有)\s*[:：]", re.I),
    re.compile(r"^(https?://|www\.)", re.I),
    re.compile(r"^[\W_]{1,8}$"),
)
_FILLER_PATTERNS = (
    re.compile(r"^(嗯|呃|这个|就是|然后|大家都知道)[，,。；;：: ]*", re.I),
    re.compile(r"\s+(重复一下|以上内容|todo|待补充)\s*$", re.I),
)


def _stable_id(prefix: str, *parts: object) -> str:
    value = "\x1f".join(str(part) for part in parts).encode("utf-8")
    return "%s-%s" % (prefix, hashlib.sha1(value).hexdigest()[:10])


def _decode_data_url(value: str) -> Tuple[str, str]:
    match = re.match(r"^data:([^;,]+)?(?:;[^,]+)*;base64,(.*)$", value, re.S)
    if not match:
        return "image/jpeg", value
    return match.group(1) or "image/jpeg", match.group(2)


def normalize_image(value: Any, name: str = "note-image", mime: str = "") -> ImageInput:
    """Normalize a data URL, path, bytes, or already-normalized image value."""

    if isinstance(value, ImageInput):
        return value
    if isinstance(value, bytes):
        return ImageInput(name=name, mime=mime or "image/jpeg", data=base64.b64encode(value).decode("ascii"), size=len(value))
    if isinstance(value, str):
        if value.startswith("data:"):
            detected_mime, data = _decode_data_url(value)
            try:
                size = len(base64.b64decode(data, validate=True))
            except (ValueError, base64.binascii.Error):
                size = 0
            return ImageInput(name=name, mime=mime or detected_mime, data=data, size=size)
        path = Path(value)
        if path.is_file():
            raw = path.read_bytes()
            return ImageInput(name=path.name, mime=mime or mimetypes.guess_type(path.name)[0] or "image/jpeg", data=base64.b64encode(raw).decode("ascii"), size=len(raw))
        # A plain string is accepted as a text-only fallback for local demos.
        return ImageInput(name=name, mime=mime or "text/plain", data="", size=0)
    raise ValueError("image must be a data URL, file path, bytes, or ImageInput")


def _infer_text_type(image: ImageInput, text: str) -> str:
    lowered = (image.name + " " + text).lower()
    if image.mime == "text/plain":
        return "关键词检索" if text.lstrip().startswith("检索主题：") else "文本粘贴"
    if "ppt" in lowered or "幻灯片" in lowered:
        return "PPT截图"
    if "web" in lowered or "网页" in lowered or "http" in lowered:
        return "网页截图"
    if any(marker in text for marker in ("手写", "草稿", "todo")):
        return "手写笔记"
    return "纸质拍照"


def _clean_lines(text: str) -> Tuple[str, str]:
    kept: List[str] = []
    noise: List[str] = []
    for raw_line in text.replace("\r\n", "\n").split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        if any(pattern.search(line) for pattern in _NOISE_PATTERNS):
            noise.append(line)
            continue
        kept.append(line)
    return "\n".join(kept), "\n".join(noise) if noise else "无"


def _coerce_confidence(value: Any) -> Optional[float]:
    """Normalize provider confidence values to a 0..1 float when available."""

    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number > 1:
        number /= 100
    return max(0.0, min(1.0, number))


def _coerce_low_confidence(value: Any) -> List[str]:
    """Accept common provider shapes while keeping the public list stable."""

    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if not isinstance(value, (list, tuple)):
        return [str(value)] if str(value).strip() else []
    segments: List[str] = []
    for item in value:
        if isinstance(item, Mapping):
            item = item.get("text", item.get("片段", item.get("segment", item.get("value", ""))))
        text = str(item).strip()
        if text and text not in segments:
            segments.append(text)
    return segments


def _detect_low_confidence_segments(text: str) -> List[str]:
    """Flag obvious OCR uncertainty without pretending to score the whole image.

    This deliberately conservative heuristic only marks replacement characters,
    long question-mark runs, and lines with an unusually high symbol ratio. A
    real provider can supply a more precise list, which is merged by ``OCRAgent``.
    """

    detected: List[str] = []
    for raw_line in text.replace("\r\n", "\n").split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        suspicious = (
            "�" in line
            or bool(re.search(r"[?？]{3,}", line))
            or (len(line) >= 6 and sum(char in "|_~^<>[]{}" for char in line) / len(line) >= 0.35)
        )
        if suspicious and line not in detected:
            detected.append(line)
    return detected


class OCRAgent:
    """Extract readable note text and remove common screenshot noise."""

    def __init__(self, provider: Optional[OCRProvider] = None) -> None:
        self.provider = provider

    def run(self, image: Any, user_note: str = "", provided_text: str = "") -> OCRResult:
        normalized = normalize_image(image)
        raw_text = provided_text.strip() if isinstance(provided_text, str) else ""
        source_text = provided_text if isinstance(provided_text, str) else ""
        noise_content = "无"
        low_confidence: List[str] = []
        quality_issue = "无"
        confidence_score: Optional[float] = None
        engine = "provided-text" if raw_text else "local-deterministic"

        if self.provider is not None and normalized.data:
            payload = self.provider.extract(normalized, user_note)
            if not isinstance(payload, Mapping):
                raise ValueError("OCR provider must return a mapping")
            provider_text = str(
                payload.get(
                    "原始提取文本",
                    payload.get("原始OCR文本", payload.get("raw_text", payload.get("text", raw_text))),
                )
                or ""
            )
            raw_text = provider_text.strip()
            source_text = provider_text
            noise_content = str(payload.get("噪声内容", payload.get("noise_content", "无")) or "无")
            low_confidence = _coerce_low_confidence(
                payload.get("低置信度片段", payload.get("low_confidence_segments", payload.get("low_confidence", [])))
            )
            quality_issue = str(payload.get("图片质量问题", payload.get("image_quality_issue", payload.get("quality_issue", "无"))) or "无")
            confidence_score = _coerce_confidence(payload.get("置信度", payload.get("confidence", payload.get("confidence_score"))))
            engine = str(payload.get("OCR引擎", payload.get("engine", type(self.provider).__name__)) or type(self.provider).__name__)

        # User notes are a transparent fallback, never presented as OCR certainty.
        if not raw_text and user_note.strip():
            raw_text = user_note.strip()
            source_text = user_note
            quality_issue = "未配置多模态 OCR，使用用户备注作为文本输入"
            engine = "user-note"
        if not raw_text:
            raise ValueError("无法提取笔记文本：请配置多模态 OCR，或填写文本备注作为本地演示输入")

        # Keep the provider response untouched for evidence/review.  ``raw_text``
        # retains the historical cleaned value consumed by downstream clients.
        source_text = source_text or raw_text
        low_confidence = list(dict.fromkeys(low_confidence + _detect_low_confidence_segments(source_text)))
        cleaned, detected_noise = _clean_lines(raw_text)
        if detected_noise != "无":
            noise_content = detected_noise if noise_content == "无" else noise_content + "\n" + detected_noise
        if not cleaned:
            raise ValueError("提取结果仅包含噪声内容")
        return OCRResult(
            raw_text=cleaned,
            noise_content=noise_content,
            text_type=_infer_text_type(normalized, cleaned),
            low_confidence_segments=low_confidence,
            image_quality_issue=quality_issue,
            source_text=source_text,
            confidence_score=confidence_score,
            engine=engine,
        )


def _contains(text: str, terms: Iterable[str]) -> bool:
    return any(term.lower() in text.lower() for term in terms)


class ClassifierAgent:
    """Assign one stable top-level category and normalized secondary tags."""

    def __init__(self, taxonomy: Optional[Mapping[str, Sequence[str]]] = None) -> None:
        self.taxonomy = {key: tuple(value) for key, value in (taxonomy or TAG_TAXONOMY).items()}

    def run(self, extracted_text: str, user_note: str = "") -> LabelResult:
        if not isinstance(extracted_text, str) or not extracted_text.strip():
            raise ValueError("extracted_text must be a non-empty string")
        corpus = (extracted_text + " " + (user_note or "")).strip()
        category_scores = {
            "面试": ("面试", "面经", "八股", "面试题", "行为题", "算法题"),
            "技术学习": ("python", "typescript", "javascript", "react", "vue", "langgraph", "api", "数据库", "向量库", "代码", "模型", "技术", "编程", "ocr", "提示词", "多模态"),
            "业务产品": ("产品", "需求", "用户", "增长", "竞品", "业务", "指标", "项目"),
            "读书感悟": ("读书", "书摘", "读后感", "作者", "章节", "感悟"),
            "生活记录": ("计划", "日记", "旅行", "购物", "健身", "生活"),
        }
        scores = {category: sum(corpus.lower().count(term.lower()) for term in terms) for category, terms in category_scores.items()}
        category = max(scores, key=scores.get) if scores and max(scores.values()) else "其他"
        tags: List[str] = []
        for tag in self.taxonomy.get(category, ()):
            if _contains(corpus, (tag,)):
                tags.append(tag)
        if not tags:
            for tag in self.taxonomy.get("其他", ()):
                if _contains(corpus, (tag,)):
                    tags.append(tag)
        tags = list(dict.fromkeys(tags))[:5] or ["待整理"]
        first_sentence = re.split(r"[。！？!?\n]", extracted_text.strip())[0].strip()
        topic = first_sentence[:80] + ("..." if len(first_sentence) > 80 else "")
        usage = {"面试": "面试复习与案例准备", "技术学习": "技术检索与学习复盘", "业务产品": "需求分析与产品复盘", "读书感悟": "读书回顾与行动提炼", "生活记录": "日常检索与计划回看"}.get(category, "待整理资料检索")
        return LabelResult(primary_category=category, sub_tags=tags, topic_summary=topic or "未命名笔记", usage_scene=usage)


def _normalize_sentence(sentence: str) -> str:
    value = re.sub(r"\s+", "", sentence).strip("-•· ")
    return value.lower()


class SummarizerAgent:
    """Remove duplicated/filler sentences without inventing new facts."""

    def run(self, clean_text: str, label_result: Optional[LabelResult] = None) -> SummaryResult:
        if not isinstance(clean_text, str) or not clean_text.strip():
            raise ValueError("clean_text must be a non-empty string")
        raw_sentences = [part.strip() for part in re.split(r"[\n。！？!?；;]+", clean_text) if part.strip()]
        seen = set()
        kept: List[str] = []
        removed: List[str] = []
        for sentence in raw_sentences:
            normalized = _normalize_sentence(sentence)
            for filler in _FILLER_PATTERNS:
                sentence = filler.sub("", sentence).strip()
            if not sentence:
                removed.append(normalized)
                continue
            normalized = _normalize_sentence(sentence)
            if normalized in seen:
                removed.append(sentence)
                continue
            seen.add(normalized)
            kept.append(sentence)
        if not kept:
            raise ValueError("精简后没有可保留的有效内容")
        body = "\n".join("- " + sentence for sentence in kept)
        key_sentences = [sentence for sentence in kept if len(sentence) >= 8][:5] or kept[:3]
        return SummaryResult(condensed_text=body, key_sentences=key_sentences, removed_redundancy="；".join(removed) if removed else "无")


def _keywords(text: str) -> List[str]:
    candidates = re.findall(r"[A-Za-z][A-Za-z0-9+#./-]{1,30}|[\u4e00-\u9fff]{2,12}", text)
    stop = {"这个", "我们", "可以", "需要", "进行", "以及", "相关", "内容", "笔记", "一个"}
    return list(dict.fromkeys(item for item in candidates if item not in stop))


class ArchiveAgent:
    """Create a stable, searchable Markdown note with YAML metadata."""

    def run(
        self,
        ocr_result: OCRResult,
        label_result: LabelResult,
        summary_result: SummaryResult,
        research_result: Optional[ResearchResult] = None,
    ) -> ArchiveResult:
        if not isinstance(ocr_result, OCRResult) or not isinstance(label_result, LabelResult) or not isinstance(summary_result, SummaryResult):
            raise ValueError("archive agent requires OCR, label, and summary results")
        if research_result is not None and not isinstance(research_result, ResearchResult):
            raise ValueError("research_result must be a ResearchResult")
        title_seed = label_result.topic_summary.strip(" .。") or "未命名笔记"
        title = title_seed[:48]
        keywords = list(dict.fromkeys(label_result.sub_tags + _keywords(label_result.topic_summary + " " + summary_result.condensed_text)))[:12]
        metadata_tags = ", ".join('"%s"' % tag.replace('"', "'") for tag in label_result.sub_tags)
        content = [
                "---",
                "title: \"%s\"" % title.replace('"', "'"),
                "category: \"%s\"" % label_result.primary_category,
                "tags: [%s]" % metadata_tags,
                "source_type: \"%s\"" % ocr_result.text_type,
                "---",
                "",
                "# %s" % title,
                "",
                "> 一句话摘要：%s" % label_result.topic_summary,
                "> 适用场景：%s" % label_result.usage_scene,
                "",
                "## 核心要点",
                "",
                summary_result.condensed_text,
                "",
                "## 重点短句",
                "",
                *["- " + sentence for sentence in summary_result.key_sentences],
                "",
                "## 检索关键词",
                "",
                *["- " + keyword for keyword in keywords],
                "",
        ]
        if research_result is not None:
            source_lines = ["", "## 公开检索来源", "", "> 检索关键词：%s" % "、".join(research_result.keywords)]
            if research_result.warning != "无":
                source_lines.extend(["> 检索提示：%s" % research_result.warning])
            if research_result.sources:
                for index, source in enumerate(research_result.sources, 1):
                    source_lines.extend(["", "%d. **%s**" % (index, source.title), "   - 摘要：%s" % source.snippet, "   - URL：%s" % source.url, "   - 检索时间：%s" % source.retrieved_at])
            else:
                source_lines.extend(["", "暂无可引用来源，请检查网络或改用更具体的关键词。"])
            content.extend(source_lines)
        # OCR evidence is additive: the original recognition result is never
        # replaced by an editor correction.  Only include extra sections when
        # they carry information beyond the legacy cleaned text.
        original_text = ocr_result.original_text.strip()
        if original_text and original_text != ocr_result.raw_text.strip():
            content.extend(["", "## OCR 原始识别文本", "", original_text])
        if ocr_result.corrected_text.strip():
            content.extend(["", "## 人工校正文本", "", ocr_result.corrected_text.strip()])
        if ocr_result.low_confidence_segments:
            content.extend(["", "## OCR 待核验片段", "", *["- " + item for item in ocr_result.low_confidence_segments]])
        markdown = "\n".join(content)
        return ArchiveResult(title=title, markdown=markdown, retrieval_keywords=keywords)


TopicClassifierAgent = ClassifierAgent
CompressionAgent = SummarizerAgent
KnowledgeArchiveAgent = ArchiveAgent

__all__ = [
    "ArchiveAgent",
    "ClassifierAgent",
    "CompressionAgent",
    "KnowledgeArchiveAgent",
    "OCRAgent",
    "SummarizerAgent",
    "TopicClassifierAgent",
    "normalize_image",
]
