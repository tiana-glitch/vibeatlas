"""Typed data contracts shared by the Career Copilot agents."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from typing import Any, Dict, List, Optional


def to_primitive(value: Any) -> Any:
    """Recursively turn dataclass output into JSON-compatible values."""

    if is_dataclass(value):
        return {key: to_primitive(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {key: to_primitive(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_primitive(item) for item in value]
    return value


@dataclass(frozen=True)
class EvidenceSpan:
    id: str
    source: str
    start: int
    end: int
    text: str

    def __post_init__(self) -> None:
        if self.source not in {"job_description", "resume"}:
            raise ValueError("evidence source must be job_description or resume")
        if self.start < 0 or self.end <= self.start:
            raise ValueError("evidence span must have a non-empty, non-negative range")
        if not self.text:
            raise ValueError("evidence text cannot be empty")


@dataclass(frozen=True)
class Requirement:
    id: str
    label: str
    text: str
    category: str
    priority: str
    evidence: EvidenceSpan


@dataclass(frozen=True)
class JobAnalysisOutput:
    requirements: List[Requirement]


@dataclass(frozen=True)
class RequirementMatch:
    id: str
    requirement_id: str
    label: str
    status: str
    strength: str
    confidence: float
    evidence: List[EvidenceSpan] = field(default_factory=list)


@dataclass(frozen=True)
class ResumeMatchOutput:
    matches: List[RequirementMatch]


@dataclass(frozen=True)
class Gap:
    id: str
    requirement_id: str
    label: str
    severity: str
    reason: str
    next_action: str
    evidence_ids: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class GapAnalysisOutput:
    gaps: List[Gap]


@dataclass(frozen=True)
class DraftEdit:
    id: str
    target: str
    before: str
    after: str
    evidence_ids: List[str]
    review_status: str = "pending"


@dataclass(frozen=True)
class WriterOutput:
    edits: List[DraftEdit]
    status: str
    message: str


@dataclass(frozen=True)
class CriticIssue:
    id: str
    type: str
    severity: str
    message: str
    evidence_ids: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class CriticOutput:
    issues: List[CriticIssue]
    verdict: str
    checked_claims: int


@dataclass(frozen=True)
class RunEvent:
    type: str
    run_id: str
    timestamp: str
    stage: Optional[str] = None
    attempt: int = 0
    duration_ms: Optional[int] = None
    model: str = "local-deterministic"
    prompt_version: str = "heuristic-v1"
    error: Optional[str] = None


@dataclass
class RunRecord:
    id: str
    target_role: str
    job_description: str
    resume: str
    status: str
    created_at: str
    updated_at: str
    outputs: Dict[str, Any] = field(default_factory=dict)
    events: List[RunEvent] = field(default_factory=list)
    attempts: Dict[str, int] = field(default_factory=dict)
    execution_counts: Dict[str, int] = field(default_factory=dict)
    error: Optional[Dict[str, str]] = None
