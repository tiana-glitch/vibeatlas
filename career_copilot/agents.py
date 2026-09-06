"""Deterministic, dependency-free agents for the Career Copilot MVP."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Pattern, Sequence, Tuple

from .models import (
    CriticIssue,
    CriticOutput,
    DraftEdit,
    EvidenceSpan,
    Gap,
    GapAnalysisOutput,
    JobAnalysisOutput,
    Requirement,
    RequirementMatch,
    ResumeMatchOutput,
    WriterOutput,
)


@dataclass(frozen=True)
class SkillSignal:
    label: str
    category: str
    pattern: Pattern[str]
    next_action: str


SKILL_SIGNALS: Tuple[SkillSignal, ...] = (
    SkillSignal("React", "frontend", re.compile(r"\breact(?:\.js)?\b", re.I), "准备一个端到端的 React 架构案例。"),
    SkillSignal("TypeScript", "frontend", re.compile(r"\btypescript\b", re.I), "说明一次 TypeScript 设计决策及其结果。"),
    SkillSignal("产品思维", "product", re.compile(r"product[- ]minded|product thinking|customer[- ]facing|product decisions?|产品思维|面向客户|产品决策", re.I), "把用户信号、产品取舍和最终结果串成一个案例。"),
    SkillSignal("API", "backend", re.compile(r"\bapi(?:s)?\b|application programming interface|接口", re.I), "整理一个你设计或集成过的 API 契约案例。"),
    SkillSignal("AI / LLM 产品", "ai", re.compile(r"\bllms?\b|language models?|ai[- ]assisted|generative ai|大模型|人工智能|AI 辅助", re.I), "把 AI 原型整理成简洁的产品案例。"),
    SkillSignal("Python", "backend", re.compile(r"\bpython\b", re.I), "围绕真实工作流构建一个小型 Python 服务。"),
    SkillSignal("用户研究", "research", re.compile(r"qualitative|user research|customer research|customer signals?|user feedback|用户研究|定性|用户反馈", re.I), "准备一个用户研究改变实现方案的案例。"),
    SkillSignal("数据建模", "data", re.compile(r"data model(?:ing|ling)?|postgres(?:ql)?|database design|数据建模|数据库", re.I), "说明已上线功能中的一次数据建模取舍。"),
    SkillSignal("前端架构", "frontend", re.compile(r"front[- ]end architecture|component system|design system|前端架构|组件系统|设计系统", re.I), "画出一次前端架构决策及其工程影响。"),
    SkillSignal("工作流产品", "product", re.compile(r"workflow(?: products?| tools?)?|\bb2b\b|工作流", re.I), "量化你在多步骤工作流产品上的经验。"),
)

_SIGNALS_BY_LABEL: Dict[str, SkillSignal] = {signal.label: signal for signal in SKILL_SIGNALS}


def _stable_id(prefix: str, *parts: object) -> str:
    value = "\x1f".join(str(part) for part in parts).encode("utf-8")
    return "%s-%s" % (prefix, hashlib.sha1(value).hexdigest()[:10])


def _context_span(text: str, start: int, end: int, source: str, prefix: str) -> EvidenceSpan:
    markers = ("\n", ".", "!", "?", "。", "！", "？", "；")

    def is_decimal_point(marker: str, position: int) -> bool:
        return (
            marker == "."
            and position > 0
            and position + 1 < len(text)
            and text[position - 1].isdigit()
            and text[position + 1].isdigit()
        )

    def previous_boundary(marker: str) -> int:
        position = text.rfind(marker, 0, start)
        while position >= 0 and is_decimal_point(marker, position):
            position = text.rfind(marker, 0, position)
        return position

    left_boundaries = [previous_boundary(marker) for marker in markers]
    span_start = max(left_boundaries) + 1
    right_candidates = []
    for marker in markers:
        position = text.find(marker, end)
        while position >= 0 and is_decimal_point(marker, position):
            position = text.find(marker, position + 1)
        if position >= 0:
            right_candidates.append(position + (0 if marker == "\n" else 1))
    span_end = min(right_candidates) if right_candidates else len(text)

    while span_start < span_end and text[span_start].isspace():
        span_start += 1
    while span_end > span_start and text[span_end - 1].isspace():
        span_end -= 1
    quote = text[span_start:span_end]
    return EvidenceSpan(
        id=_stable_id(prefix, source, span_start, span_end, quote),
        source=source,
        start=span_start,
        end=span_end,
        text=quote,
    )


def _find_evidence(text: str, pattern: Pattern[str], source: str, prefix: str) -> Optional[EvidenceSpan]:
    match = pattern.search(text)
    if match is None:
        return None
    return _context_span(text, match.start(), match.end(), source, prefix)


def _priority_for(text: str) -> str:
    lowered = text.lower()
    if any(marker in lowered for marker in ("plus", "preferred", "nice to have", "bonus", "优先", "加分")):
        return "low"
    if any(marker in lowered for marker in ("must", "required", "strong", "what you'll bring", "what you will bring", "岗位要求", "必须", "扎实")):
        return "high"
    return "medium"


class JobAnalyst:
    """Extract a stable list of known role signals with JD source spans."""

    def run(self, job_description: str) -> JobAnalysisOutput:
        if not isinstance(job_description, str) or not job_description.strip():
            raise ValueError("job_description must be a non-empty string")

        requirements: List[Requirement] = []
        for signal in SKILL_SIGNALS:
            evidence = _find_evidence(job_description, signal.pattern, "job_description", "jd")
            if evidence is None:
                continue
            requirements.append(
                Requirement(
                    id=_stable_id("req", signal.label, evidence.start, evidence.end),
                    label=signal.label,
                    text=evidence.text,
                    category=signal.category,
                    priority=_priority_for(evidence.text),
                    evidence=evidence,
                )
            )
        return JobAnalysisOutput(requirements=requirements)


class ResumeMatcher:
    """Link each detected requirement to resume text, or mark it unsupported."""

    def run(self, requirements: Sequence[Requirement], resume: str) -> ResumeMatchOutput:
        if not isinstance(resume, str) or not resume.strip():
            raise ValueError("resume must be a non-empty string")

        matches: List[RequirementMatch] = []
        for requirement in requirements:
            signal = _SIGNALS_BY_LABEL.get(requirement.label)
            evidence = _find_evidence(resume, signal.pattern, "resume", "resume") if signal else None
            supported = evidence is not None
            matches.append(
                RequirementMatch(
                    id=_stable_id("match", requirement.id),
                    requirement_id=requirement.id,
                    label=requirement.label,
                    status="supported" if supported else "unsupported",
                    strength="strong" if supported else "missing",
                    confidence=0.9 if supported else 1.0,
                    evidence=[evidence] if evidence else [],
                )
            )
        return ResumeMatchOutput(matches=matches)


class GapAnalyst:
    """Rank role requirements that have no resume evidence."""

    _SEVERITY = {"high": "high", "medium": "medium", "low": "low"}

    def run(
        self,
        requirements: Sequence[Requirement],
        match_output: ResumeMatchOutput,
    ) -> GapAnalysisOutput:
        matches_by_requirement = {match.requirement_id: match for match in match_output.matches}
        gaps: List[Gap] = []
        for requirement in requirements:
            match = matches_by_requirement.get(requirement.id)
            if match is not None and match.status == "supported":
                continue
            signal = _SIGNALS_BY_LABEL.get(requirement.label)
            action = signal.next_action if signal else "补充一个真实案例，否则保留为未支持项。"
            gaps.append(
                Gap(
                    id=_stable_id("gap", requirement.id),
                    requirement_id=requirement.id,
                    label=requirement.label,
                    severity=self._SEVERITY[requirement.priority],
                    reason="在提供的简历中未找到支持该要求的原文。",
                    next_action=action,
                    evidence_ids=[requirement.evidence.id],
                )
            )
        order = {"high": 0, "medium": 1, "low": 2}
        gaps.sort(key=lambda item: (order[item.severity], item.label.lower()))
        return GapAnalysisOutput(gaps=gaps)


class Writer:
    """Propose resume text using only spans already present in the resume."""

    def run(self, match_output: ResumeMatchOutput, resume: str) -> WriterOutput:
        del resume  # Source text is carried by validated evidence spans.
        supported = [match for match in match_output.matches if match.status == "supported" and match.evidence]
        if not supported:
            return WriterOutput(
                edits=[],
                status="unsupported",
                message="当前无法生成有证据支持的简历草稿。",
            )

        source = supported[0].evidence[0]
        edit = DraftEdit(
            id=_stable_id("edit", supported[0].id, source.id),
            target="resume_bullet",
            before="",
            after=source.text,
            evidence_ids=[source.id],
        )
        return WriterOutput(
            edits=[edit],
            status="review_required",
            message="草稿来自简历原文证据，需要人工审阅。",
        )


class Critic:
    """Verify every linked source span and block unsupported writer claims."""

    def run(
        self,
        job_description: str,
        resume: str,
        job_output: JobAnalysisOutput,
        match_output: ResumeMatchOutput,
        writer_output: WriterOutput,
    ) -> CriticOutput:
        evidence: Dict[str, EvidenceSpan] = {}
        for requirement in job_output.requirements:
            evidence[requirement.evidence.id] = requirement.evidence
        for match in match_output.matches:
            for span in match.evidence:
                evidence[span.id] = span

        issues: List[CriticIssue] = []
        checked_claims = 0
        source_texts = {"job_description": job_description, "resume": resume}
        for span in evidence.values():
            checked_claims += 1
            source = source_texts[span.source]
            valid_range = 0 <= span.start < span.end <= len(source)
            if not valid_range or source[span.start:span.end] != span.text:
                issues.append(
                    CriticIssue(
                        id=_stable_id("issue", "invalid_span", span.id),
                        type="invalid_evidence_span",
                        severity="blocking",
                        message="证据 %s 无法映射到对应原文。" % span.id,
                        evidence_ids=[span.id],
                    )
                )

        for edit in writer_output.edits:
            checked_claims += 1
            linked = [evidence[item] for item in edit.evidence_ids if item in evidence]
            missing_ids = [item for item in edit.evidence_ids if item not in evidence]
            is_grounded = bool(linked) and edit.after in {span.text for span in linked}
            if missing_ids or not is_grounded:
                issues.append(
                    CriticIssue(
                        id=_stable_id("issue", "unsupported", edit.id),
                        type="unsupported_claim",
                        severity="blocking",
                        message="草稿 %s 的内容无法由关联的简历证据直接支持。" % edit.id,
                        evidence_ids=list(edit.evidence_ids),
                    )
                )

        verdict = "blocked" if any(issue.severity == "blocking" for issue in issues) else "pass"
        return CriticOutput(issues=issues, verdict=verdict, checked_claims=checked_claims)


__all__ = ["Critic", "GapAnalyst", "JobAnalyst", "ResumeMatcher", "Writer"]
