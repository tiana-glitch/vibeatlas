"""Serial orchestration for the four note knowledge-base agents."""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

from .agents import ArchiveAgent, ClassifierAgent, OCRAgent, SummarizerAgent, normalize_image
from .models import (
    ArchiveResult,
    ImageInput,
    LabelResult,
    NoteEvent,
    NoteRunRecord,
    OCRResult,
    ResearchResult,
    SearchSource,
    SummaryResult,
    now_iso,
    to_primitive,
)
from .retrieval import ResearchAgent, normalize_keywords


STAGES: Tuple[str, ...] = ("ocr", "classify", "summarize", "archive")
RESEARCH_STAGE = "retrieve"
STAGE_ALIASES = {
    "retrieve": "retrieve",
    "research": "retrieve",
    "search": "retrieve",
    "检索": "retrieve",
    "关键词检索": "retrieve",
    "ocr": "ocr",
    "extract": "ocr",
    "extractor": "ocr",
    "内容提取": "ocr",
    "classify": "classify",
    "classifier": "classify",
    "label": "classify",
    "打标": "classify",
    "summarize": "summarize",
    "summary": "summarize",
    "compress": "summarize",
    "精简": "summarize",
    "archive": "archive",
    "归档": "archive",
}


class OCRRevisionConflict(ValueError):
    """Raised when an editor submits a correction against a stale revision."""

    code = "ocr_revision_conflict"
    status = 409


class NoteOrchestrator:
    """Run OCR -> classify -> summarize -> archive and retain event history."""

    def __init__(self, agents: Optional[Mapping[str, Any]] = None, storage_dir: Optional[Path] = None) -> None:
        defaults: Dict[str, Any] = {
            "retrieve": ResearchAgent(),
            "ocr": OCRAgent(),
            "classify": ClassifierAgent(),
            "summarize": SummarizerAgent(),
            "archive": ArchiveAgent(),
        }
        if agents:
            defaults.update(agents)
        missing = [stage for stage in STAGES if stage not in defaults]
        if missing:
            raise ValueError("missing agents: %s" % ", ".join(missing))
        self.agents = defaults
        self.storage_dir = Path(storage_dir).resolve() if storage_dir else None
        if self.storage_dir:
            self.storage_dir.mkdir(parents=True, exist_ok=True)
        self._runs: Dict[str, NoteRunRecord] = {}
        self._lock = threading.RLock()

    def process(
        self,
        image: Any = "",
        user_note: str = "",
        provided_text: str = "",
        image_name: str = "note-image",
        image_mime: str = "",
        keywords: Any = None,
    ) -> Dict[str, Any]:
        if user_note is None:
            user_note = ""
        if not isinstance(user_note, str):
            raise ValueError("user_note must be a string")
        if not isinstance(provided_text, str):
            raise ValueError("provided_text must be a string")
        normalized_keywords = normalize_keywords(keywords)
        normalized = normalize_image(image, name=image_name or "note-image", mime=image_mime or "")
        if not normalized.data and not user_note.strip() and not provided_text.strip() and not normalized_keywords:
            raise ValueError("请上传图片、填写检索关键词，或填写文字回退输入")
        stage_order: Tuple[str, ...] = ((RESEARCH_STAGE,) + STAGES) if normalized_keywords else STAGES
        timestamp = now_iso()
        run_id = "note_%s" % uuid.uuid4().hex
        record = NoteRunRecord(
            id=run_id,
            image_name=normalized.name,
            image_mime=normalized.mime,
            image_size=normalized.size,
            image_data=normalized.data,
            user_note=user_note.strip(),
            keywords=normalized_keywords,
            stage_order=stage_order,
            status="running",
            created_at=timestamp,
            updated_at=timestamp,
            attempts={stage: 0 for stage in stage_order},
            execution_counts={stage: 0 for stage in stage_order},
        )
        record.events.append(NoteEvent(type="run.created", run_id=run_id, timestamp=timestamp))
        with self._lock:
            self._runs[run_id] = record
            self._save(record)

        # The text fallback is kept outside the public record because it can be
        # sensitive; it is used only by the OCR node for this run.
        record._provided_text = provided_text  # type: ignore[attr-defined]
        record._image = normalized  # type: ignore[attr-defined]
        for stage in stage_order:
            if not self._execute_stage(record, stage, retry=False):
                break
        if all(stage in record.outputs for stage in record.stage_order):
            record.status = "complete"
            record.updated_at = now_iso()
            record.events.append(NoteEvent(type="run.completed", run_id=run_id, timestamp=record.updated_at))
            self._save(record)
        return self._public_view(record)

    # Familiar aliases make the service easy to embed in existing demos.
    def analyze(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.process(*args, **kwargs)

    def run(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.process(*args, **kwargs)

    def retry_stage(self, run_id: str, stage: str) -> Dict[str, Any]:
        normalized_stage = self._normalize_stage(stage)
        with self._lock:
            record = self._runs.get(run_id)
            if record is None:
                raise KeyError(run_id)
            if normalized_stage not in record.stage_order:
                raise ValueError("当前运行没有%s阶段" % normalized_stage)
            succeeded = self._execute_stage(record, normalized_stage, retry=True)
            if succeeded:
                record.errors = [item for item in record.errors if not item.startswith(normalized_stage + ":")]
                start_index = record.stage_order.index(normalized_stage) + 1
                for next_stage in record.stage_order[start_index:]:
                    if next_stage in record.outputs:
                        continue
                    if not self._execute_stage(record, next_stage, retry=False):
                        break
                record.status = "complete" if all(item in record.outputs for item in record.stage_order) else "partial"
                record.updated_at = now_iso()
                if record.status == "complete" and not any(event.type == "run.completed" for event in record.events):
                    record.events.append(NoteEvent(type="run.completed", run_id=record.id, timestamp=record.updated_at))
                self._save(record)
            return self._public_view(record)

    def correct_ocr(
        self,
        run_id: str,
        corrected_text: str,
        expected_revision: Optional[int] = None,
        *,
        rerun_downstream: bool = True,
    ) -> Dict[str, Any]:
        """Persist an OCR correction and optionally rebuild downstream stages.

        The original provider response remains in ``OCRResult.source_text`` and
        the legacy cleaned value remains in ``raw_text``.  A monotonically
        increasing revision lets the HTTP layer reject an editor tab that was
        opened before another correction was saved.
        """

        if not isinstance(corrected_text, str) or not corrected_text.strip():
            raise ValueError("corrected_text must be a non-empty string")
        normalized_text = corrected_text.replace("\r\n", "\n").strip()
        with self._lock:
            record = self._runs.get(run_id)
            if record is None and self.storage_dir:
                record = self._load_record(run_id)
                if record:
                    self._runs[run_id] = record
            if record is None:
                raise KeyError(run_id)
            ocr = self._output(record, "ocr", OCRResult)
            current_revision = int(record.ocr_revision or 0)
            if expected_revision is not None:
                try:
                    expected = int(expected_revision)
                except (TypeError, ValueError) as exc:
                    raise ValueError("expected_revision must be an integer") from exc
                if expected != current_revision:
                    raise OCRRevisionConflict(
                        "OCR revision conflict: expected %d, current %d" % (expected, current_revision)
                    )

            # A repeated submission of the same text is idempotent and does not
            # create another revision.  If a previous downstream run stopped
            # part-way through, however, use this request as a chance to finish
            # the missing stages.
            if ocr.corrected_text.strip() == normalized_text:
                missing_downstream = [
                    stage
                    for stage in record.stage_order
                    if stage in {"classify", "summarize", "archive"} and stage not in record.outputs
                ]
                if rerun_downstream:
                    for stage in missing_downstream:
                        if not self._execute_stage(record, stage, retry=False):
                            break
                    if all(stage in record.outputs for stage in record.stage_order):
                        record.status = "complete"
                    elif record.status != "failed":
                        record.status = "partial"
                    record.updated_at = now_iso()
                    self._save(record)
                view = self._public_view(record)
                view["ocr_correction"] = {
                    "revision": current_revision,
                    "idempotent": True,
                    "rerun": bool(rerun_downstream and missing_downstream),
                    "rerun_stages": missing_downstream if rerun_downstream else [],
                }
                return view

            revision = current_revision + 1
            revised = replace(
                ocr,
                corrected_text=normalized_text,
                correction_status="corrected",
                correction_updated_at=now_iso(),
            )
            record.outputs["ocr"] = revised
            record.ocr_revision = revision
            # Downstream material is derived from OCR text.  Invalidate it before
            # rebuilding so a failed retry cannot leave a stale archive visible.
            downstream = [stage for stage in record.stage_order if stage in {"classify", "summarize", "archive"}]
            for stage in downstream:
                record.outputs.pop(stage, None)
                record.errors = [item for item in record.errors if not item.startswith(stage + ":")]
            record.status = "partial"
            record.updated_at = now_iso()
            record.events.append(
                NoteEvent(
                    type="ocr.corrected",
                    run_id=record.id,
                    timestamp=record.updated_at,
                    stage="ocr",
                    attempt=revision,
                    model="human-editor",
                    prompt_version="ocr-review-v1",
                )
            )
            self._save(record)

            if rerun_downstream:
                for stage in downstream:
                    if not self._execute_stage(record, stage, retry=False):
                        break
                if all(stage in record.outputs for stage in record.stage_order):
                    record.status = "complete"
                    record.updated_at = now_iso()
                    record.events.append(NoteEvent(type="run.completed", run_id=record.id, timestamp=record.updated_at))
                elif record.status != "failed":
                    record.status = "partial"
                self._save(record)
            view = self._public_view(record)
            view["ocr_correction"] = {
                "revision": revision,
                "idempotent": False,
                "rerun": bool(rerun_downstream),
                "rerun_stages": downstream if rerun_downstream else [],
            }
            return view

    # Naming aliases make the operation discoverable to API adapters and
    # preserve room for clients that call it a revision rather than correction.
    def revise_ocr(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.correct_ocr(*args, **kwargs)

    def update_ocr_correction(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.correct_ocr(*args, **kwargs)

    def update_ocr(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        """Short alias for adapters that expose this as an OCR update."""

        return self.correct_ocr(*args, **kwargs)

    def get_run(self, run_id: str) -> Dict[str, Any]:
        with self._lock:
            record = self._runs.get(run_id)
            if record is None and self.storage_dir:
                record = self._load_record(run_id)
                if record:
                    self._runs[run_id] = record
            if record is None:
                raise KeyError(run_id)
            return self._public_view(record)

    @staticmethod
    def _normalize_stage(stage: str) -> str:
        key = stage.strip().lower().replace("-", "_") if isinstance(stage, str) else ""
        normalized = STAGE_ALIASES.get(key)
        if normalized is None:
            raise ValueError("unknown stage; expected one of: %s" % ", ".join((RESEARCH_STAGE,) + STAGES))
        return normalized

    def _execute_stage(self, record: NoteRunRecord, stage: str, retry: bool) -> bool:
        attempt = record.attempts[stage] + 1
        record.attempts[stage] = attempt
        record.execution_counts[stage] += 1
        record.status = "running"
        record.updated_at = now_iso()
        if retry:
            record.events.append(NoteEvent(type="stage.retry", run_id=record.id, timestamp=record.updated_at, stage=stage, attempt=attempt))
        record.events.append(NoteEvent(type="stage.started", run_id=record.id, timestamp=now_iso(), stage=stage, attempt=attempt))
        started = time.perf_counter()
        try:
            output = self._call_agent(record, stage)
            self._validate_output(stage, output)
        except Exception as exc:
            duration_ms = max(0, round((time.perf_counter() - started) * 1000))
            message = "%s: %s" % (exc.__class__.__name__, exc)
            record.status = "failed"
            record.updated_at = now_iso()
            record.errors.append(stage + ": " + message)
            record.events.append(NoteEvent(type="stage.failed", run_id=record.id, timestamp=record.updated_at, stage=stage, attempt=attempt, duration_ms=duration_ms, error=message))
            self._save(record)
            return False
        duration_ms = max(0, round((time.perf_counter() - started) * 1000))
        record.outputs[stage] = output
        record.updated_at = now_iso()
        record.events.append(NoteEvent(type="stage.completed", run_id=record.id, timestamp=record.updated_at, stage=stage, attempt=attempt, duration_ms=duration_ms))
        self._save(record)
        return True

    @staticmethod
    def _validate_output(stage: str, output: Any) -> None:
        expected = {"retrieve": ResearchResult, "ocr": OCRResult, "classify": LabelResult, "summarize": SummaryResult, "archive": ArchiveResult}[stage]
        if not isinstance(output, expected):
            raise TypeError("%s agent returned %s, expected %s" % (stage, type(output).__name__, expected.__name__))

    def _call_agent(self, record: NoteRunRecord, stage: str) -> Any:
        image: ImageInput = getattr(record, "_image", ImageInput(name=record.image_name, mime=record.image_mime, data=record.image_data, size=record.image_size))
        provided_text = getattr(record, "_provided_text", "")
        if stage == RESEARCH_STAGE:
            return self.agents[stage].run(record.keywords, record.user_note, provided_text)
        if stage == "ocr":
            research = record.outputs.get(RESEARCH_STAGE)
            if isinstance(research, ResearchResult):
                provided_text = research.research_text
            return self.agents[stage].run(image, record.user_note, provided_text)
        ocr = self._output(record, "ocr", OCRResult)
        if stage == "classify":
            return self.agents[stage].run(ocr.effective_text, record.user_note)
        label = self._output(record, "classify", LabelResult)
        if stage == "summarize":
            return self.agents[stage].run(ocr.effective_text, label)
        summary = self._output(record, "summarize", SummaryResult)
        if stage == "archive":
            research = record.outputs.get(RESEARCH_STAGE)
            return self.agents[stage].run(ocr, label, summary, research if isinstance(research, ResearchResult) else None)
        raise ValueError("unknown stage: " + stage)

    @staticmethod
    def _output(record: NoteRunRecord, stage: str, output_type: Any) -> Any:
        output = record.outputs.get(stage)
        if not isinstance(output, output_type):
            raise RuntimeError("stage %s has no valid output" % stage)
        return output

    def _public_view(self, record: NoteRunRecord) -> Dict[str, Any]:
        outputs: Dict[str, Any] = {}
        for stage in record.stage_order:
            output = record.outputs.get(stage)
            if output is not None:
                outputs[stage] = output.to_dict() if hasattr(output, "to_dict") else to_primitive(output)
        ocr = record.outputs.get("ocr")
        label = record.outputs.get("classify")
        summary = record.outputs.get("summarize")
        archive = record.outputs.get("archive")
        research = record.outputs.get(RESEARCH_STAGE)
        stages: Dict[str, Dict[str, Any]] = {}
        for stage in record.stage_order:
            stages[stage] = {
                "status": "completed" if stage in record.outputs else ("failed" if any(event.stage == stage and event.type == "stage.failed" for event in record.events) else "queued"),
                "attempt": record.attempts[stage],
                "execution_count": record.execution_counts[stage],
                "output": outputs.get(stage),
            }
        completed = sum(item["status"] == "completed" for item in stages.values())
        return {
            "run_id": record.id,
            "status": record.status,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
            "image": {"name": record.image_name, "mime": record.image_mime, "size": record.image_size},
            "user_note": record.user_note,
            "keywords": list(record.keywords),
            "search_query": research.query if isinstance(research, ResearchResult) else "",
            "ocr_revision": int(record.ocr_revision or 0),
            "stage_order": list(record.stage_order),
            "current_step": next((stage for stage in record.stage_order if stage not in record.outputs), "complete"),
            "progress": round(completed / len(record.stage_order) * 100),
            "outputs": outputs,
            "state": {
                "raw_ocr_text": ocr.raw_text if isinstance(ocr, OCRResult) else "",
                "original_ocr_text": ocr.original_text if isinstance(ocr, OCRResult) else "",
                "ocr_source_text": ocr.original_text if isinstance(ocr, OCRResult) else "",
                "corrected_ocr_text": ocr.corrected_text if isinstance(ocr, OCRResult) else "",
                "ocr_corrected_text": ocr.corrected_text if isinstance(ocr, OCRResult) else "",
                "corrected_text": ocr.corrected_text if isinstance(ocr, OCRResult) else "",
                "effective_ocr_text": ocr.effective_text if isinstance(ocr, OCRResult) else "",
                "effective_text": ocr.effective_text if isinstance(ocr, OCRResult) else "",
                "ocr_revision": int(record.ocr_revision or 0),
                "ocr_correction_status": ocr.correction_status if isinstance(ocr, OCRResult) else "",
                "ocr_corrected_at": ocr.correction_updated_at if isinstance(ocr, OCRResult) else "",
                "ocr_confidence_score": ocr.confidence_score if isinstance(ocr, OCRResult) else None,
                "ocr_engine": ocr.engine if isinstance(ocr, OCRResult) else "",
                "noise_content": ocr.noise_content if isinstance(ocr, OCRResult) else "",
                "text_type": ocr.text_type if isinstance(ocr, OCRResult) else "",
                "low_confidence_segments": list(ocr.low_confidence_segments) if isinstance(ocr, OCRResult) else [],
                "image_quality_issue": ocr.image_quality_issue if isinstance(ocr, OCRResult) else "",
                "primary_category": label.primary_category if isinstance(label, LabelResult) else "",
                "sub_tags": list(label.sub_tags) if isinstance(label, LabelResult) else [],
                "topic_summary": label.topic_summary if isinstance(label, LabelResult) else "",
                "usage_scene": label.usage_scene if isinstance(label, LabelResult) else "",
                "condensed_text": summary.condensed_text if isinstance(summary, SummaryResult) else "",
                "key_sentences": list(summary.key_sentences) if isinstance(summary, SummaryResult) else [],
                "removed_redundancy": summary.removed_redundancy if isinstance(summary, SummaryResult) else "",
                "archive_markdown": archive.markdown if isinstance(archive, ArchiveResult) else "",
                "retrieval_keywords": list(archive.retrieval_keywords) if isinstance(archive, ArchiveResult) else [],
                "retrieved_text": research.research_text if isinstance(research, ResearchResult) else "",
                "retrieval_status": research.status if isinstance(research, ResearchResult) else "",
                "retrieval_warning": research.warning if isinstance(research, ResearchResult) else "",
                "retrieval_sources": [source.to_dict() for source in research.sources] if isinstance(research, ResearchResult) else [],
                "retrieved_at": research.sources[0].retrieved_at if isinstance(research, ResearchResult) and research.sources else "",
            },
            "stages": stages,
            "events": to_primitive(record.events),
            "attempts": dict(record.attempts),
            "execution_counts": dict(record.execution_counts),
            "errors": list(record.errors),
            "error": record.errors[-1] if record.errors else None,
            "archive_markdown": archive.markdown if isinstance(archive, ArchiveResult) else "",
        }

    def _save(self, record: NoteRunRecord) -> None:
        if not self.storage_dir:
            return
        view = self._public_view(record)
        view["_internal"] = {
            "image_data": record.image_data,
            "provided_text": getattr(record, "_provided_text", ""),
        }
        view["ocr_revision"] = int(record.ocr_revision or 0)
        target = self.storage_dir / (record.id + ".json")
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(view, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(target)

    def _load_record(self, run_id: str) -> Optional[NoteRunRecord]:
        if not self.storage_dir:
            return None
        target = self.storage_dir / (run_id + ".json")
        if not target.is_file():
            return None
        payload = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return None
        outputs = payload.get("outputs", {})
        reconstructed: Dict[str, Any] = {}
        try:
            ocr = outputs.get("ocr")
            if isinstance(ocr, dict):
                reconstructed["ocr"] = OCRResult.from_dict(ocr)
            label = outputs.get("classify")
            if isinstance(label, dict):
                reconstructed["classify"] = LabelResult(
                    primary_category=str(label.get("一级分类", "其他")),
                    sub_tags=[str(item) for item in label.get("二级标签", [])],
                    topic_summary=str(label.get("一句话主题概括", "")),
                    usage_scene=str(label.get("适用场景", "待整理资料检索")),
                )
            summary = outputs.get("summarize")
            if isinstance(summary, dict):
                reconstructed["summarize"] = SummaryResult(
                    condensed_text=str(summary.get("精简版要点文本", "")),
                    key_sentences=[str(item) for item in summary.get("重点短句", [])],
                    removed_redundancy=str(summary.get("冗余内容", "无")),
                )
            archive = outputs.get("archive")
            if isinstance(archive, dict):
                reconstructed["archive"] = ArchiveResult(
                    title=str(archive.get("标题", "未命名笔记")),
                    markdown=str(archive.get("归档Markdown", "")),
                    retrieval_keywords=[str(item) for item in archive.get("检索关键词", [])],
                )
            research = outputs.get(RESEARCH_STAGE)
            if isinstance(research, dict):
                sources = []
                for item in research.get("来源列表", []):
                    if not isinstance(item, dict):
                        continue
                    sources.append(
                        SearchSource(
                            title=str(item.get("标题", "")),
                            url=str(item.get("URL", "")),
                            snippet=str(item.get("摘要", "")),
                            source=str(item.get("来源", "web")),
                            retrieved_at=str(item.get("检索时间", "")),
                        )
                    )
                reconstructed[RESEARCH_STAGE] = ResearchResult(
                    keywords=[str(item) for item in research.get("关键词", [])],
                    query=str(research.get("检索查询", "")),
                    sources=sources,
                    research_text=str(research.get("检索文本", "")),
                    status=str(research.get("检索状态", "empty")),
                    warning=str(research.get("提示", "无")),
                )
        except (TypeError, ValueError, AttributeError):
            return None
        image = payload.get("image", {}) if isinstance(payload.get("image"), dict) else {}
        internal = payload.get("_internal", {}) if isinstance(payload.get("_internal"), dict) else {}
        record = NoteRunRecord(
            id=str(payload.get("run_id", run_id)),
            image_name=str(image.get("name", "note-image")),
            image_mime=str(image.get("mime", "image/jpeg")),
            image_size=int(image.get("size", 0) or 0),
            image_data=str(internal.get("image_data", "")),
            user_note=str(payload.get("user_note", "")),
            keywords=[str(item) for item in payload.get("keywords", [])],
            stage_order=tuple(str(item) for item in payload.get("stage_order", ((RESEARCH_STAGE,) + STAGES if payload.get("keywords") else STAGES))),
            status=str(payload.get("status", "partial")),
            created_at=str(payload.get("created_at", now_iso())),
            updated_at=str(payload.get("updated_at", now_iso())),
            outputs=reconstructed,
            attempts={stage: int((payload.get("attempts", {}) or {}).get(stage, 0)) for stage in tuple(str(item) for item in payload.get("stage_order", ((RESEARCH_STAGE,) + STAGES if payload.get("keywords") else STAGES)))},
            execution_counts={stage: int((payload.get("execution_counts", {}) or {}).get(stage, 0)) for stage in tuple(str(item) for item in payload.get("stage_order", ((RESEARCH_STAGE,) + STAGES if payload.get("keywords") else STAGES)))},
            errors=[str(item) for item in payload.get("errors", [])],
            ocr_revision=int(
                payload.get(
                    "ocr_revision",
                    1 if isinstance(reconstructed.get("ocr"), OCRResult) and reconstructed["ocr"].corrected_text else 0,
                )
                or 0
            ),
        )
        events = []
        for item in payload.get("events", []):
            if not isinstance(item, dict):
                continue
            events.append(NoteEvent(type=str(item.get("type", "")), run_id=record.id, timestamp=str(item.get("timestamp", now_iso())), stage=item.get("stage"), attempt=int(item.get("attempt", 0) or 0), duration_ms=item.get("duration_ms"), error=item.get("error"), model=str(item.get("model", "local-deterministic")), prompt_version=str(item.get("prompt_version", "note-pipeline-v1"))))
        record.events = events
        record._provided_text = str(internal.get("provided_text", ""))  # type: ignore[attr-defined]
        record._image = ImageInput(name=record.image_name, mime=record.image_mime, data=record.image_data, size=record.image_size)  # type: ignore[attr-defined]
        return record


__all__ = ["NoteOrchestrator", "OCRRevisionConflict", "STAGES"]
