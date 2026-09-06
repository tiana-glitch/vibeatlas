"""Event-recording orchestration for the five Career Copilot agents."""

from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional

from .agents import Critic, GapAnalyst, JobAnalyst, ResumeMatcher, Writer
from .models import (
    CriticOutput,
    EvidenceSpan,
    GapAnalysisOutput,
    JobAnalysisOutput,
    ResumeMatchOutput,
    RunEvent,
    RunRecord,
    WriterOutput,
    to_primitive,
)


STAGES = ("job", "matcher", "gap", "writer", "critic")
STAGE_ALIASES = {
    "job": "job",
    "job_analyst": "job",
    "jobanalyst": "job",
    "matcher": "matcher",
    "resume_matcher": "matcher",
    "resumematcher": "matcher",
    "gap": "gap",
    "gap_analyst": "gap",
    "gapanalyst": "gap",
    "writer": "writer",
    "writer_agent": "writer",
    "critic": "critic",
    "critic_agent": "critic",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _evidence_view(span: EvidenceSpan) -> Dict[str, Any]:
    return {
        "id": span.id,
        "source": span.source,
        "start": span.start,
        "end": span.end,
        "text": span.text,
        "quote": span.text,
    }


class Orchestrator:
    """Run agents in order and retain immutable event history in memory."""

    def __init__(self, agents: Optional[Mapping[str, Any]] = None) -> None:
        defaults: Dict[str, Any] = {
            "job": JobAnalyst(),
            "matcher": ResumeMatcher(),
            "gap": GapAnalyst(),
            "writer": Writer(),
            "critic": Critic(),
        }
        if agents:
            defaults.update(agents)
        missing = [stage for stage in STAGES if stage not in defaults]
        if missing:
            raise ValueError("missing agents: %s" % ", ".join(missing))
        self.agents = defaults
        self._runs: Dict[str, RunRecord] = {}
        self._lock = threading.RLock()

    def analyze(self, job_description: str, resume: str, target_role: str = "") -> Dict[str, Any]:
        if not isinstance(job_description, str) or not job_description.strip():
            raise ValueError("job_description must be a non-empty string")
        if not isinstance(resume, str) or not resume.strip():
            raise ValueError("resume must be a non-empty string")
        if target_role is None:
            target_role = ""
        if not isinstance(target_role, str):
            raise ValueError("target_role must be a string")

        timestamp = _now()
        run_id = "run_%s" % uuid.uuid4().hex
        record = RunRecord(
            id=run_id,
            target_role=target_role.strip(),
            job_description=job_description,
            resume=resume,
            status="running",
            created_at=timestamp,
            updated_at=timestamp,
            attempts={stage: 0 for stage in STAGES},
            execution_counts={stage: 0 for stage in STAGES},
        )
        record.events.append(RunEvent(type="run.created", run_id=run_id, timestamp=timestamp))
        with self._lock:
            self._runs[run_id] = record

        for stage in STAGES:
            if not self._execute_stage(record, stage, retry=False):
                break

        if all(stage in record.outputs for stage in STAGES):
            record.status = "complete"
            record.updated_at = _now()
            record.events.append(RunEvent(type="run.completed", run_id=run_id, timestamp=record.updated_at))
        return self._public_view(record)

    def retry_stage(self, run_id: str, stage: str) -> Dict[str, Any]:
        normalized = STAGE_ALIASES.get(stage.lower().replace("-", "_")) if isinstance(stage, str) else None
        if normalized is None:
            raise ValueError("unknown stage; expected one of: %s" % ", ".join(STAGES))
        with self._lock:
            record = self._runs.get(run_id)
            if record is None:
                raise KeyError(run_id)
            succeeded = self._execute_stage(record, normalized, retry=True)
            if succeeded:
                record.error = None
                record.status = "complete" if all(item in record.outputs for item in STAGES) else "partial"
            return self._public_view(record)

    def get_run(self, run_id: str) -> Dict[str, Any]:
        with self._lock:
            record = self._runs.get(run_id)
            if record is None:
                raise KeyError(run_id)
            return self._public_view(record)

    def _execute_stage(self, record: RunRecord, stage: str, retry: bool) -> bool:
        attempt = record.attempts[stage] + 1
        record.attempts[stage] = attempt
        record.execution_counts[stage] += 1
        record.status = "running"
        record.updated_at = _now()
        if retry:
            record.events.append(
                RunEvent(
                    type="stage.retry",
                    run_id=record.id,
                    timestamp=record.updated_at,
                    stage=stage,
                    attempt=attempt,
                )
            )
        record.events.append(
            RunEvent(
                type="stage.started",
                run_id=record.id,
                timestamp=_now(),
                stage=stage,
                attempt=attempt,
            )
        )
        started = time.perf_counter()
        try:
            output = self._call_agent(record, stage)
        except Exception as exc:  # Agent failures are part of the run's event contract.
            duration_ms = max(0, round((time.perf_counter() - started) * 1000))
            message = "%s: %s" % (exc.__class__.__name__, exc)
            record.status = "failed"
            record.updated_at = _now()
            record.error = {"stage": stage, "message": message}
            record.events.append(
                RunEvent(
                    type="stage.failed",
                    run_id=record.id,
                    timestamp=record.updated_at,
                    stage=stage,
                    attempt=attempt,
                    duration_ms=duration_ms,
                    error=message,
                )
            )
            return False

        duration_ms = max(0, round((time.perf_counter() - started) * 1000))
        record.outputs[stage] = output
        record.updated_at = _now()
        record.events.append(
            RunEvent(
                type="stage.completed",
                run_id=record.id,
                timestamp=record.updated_at,
                stage=stage,
                attempt=attempt,
                duration_ms=duration_ms,
            )
        )
        return True

    def _call_agent(self, record: RunRecord, stage: str) -> Any:
        if stage == "job":
            return self.agents[stage].run(record.job_description)
        if stage == "matcher":
            job_output = self._output(record, "job", JobAnalysisOutput)
            return self.agents[stage].run(job_output.requirements, record.resume)
        if stage == "gap":
            job_output = self._output(record, "job", JobAnalysisOutput)
            match_output = self._output(record, "matcher", ResumeMatchOutput)
            return self.agents[stage].run(job_output.requirements, match_output)
        if stage == "writer":
            match_output = self._output(record, "matcher", ResumeMatchOutput)
            return self.agents[stage].run(match_output, record.resume)
        if stage == "critic":
            return self.agents[stage].run(
                record.job_description,
                record.resume,
                self._output(record, "job", JobAnalysisOutput),
                self._output(record, "matcher", ResumeMatchOutput),
                self._output(record, "writer", WriterOutput),
            )
        raise ValueError("unknown stage: %s" % stage)

    @staticmethod
    def _output(record: RunRecord, stage: str, output_type: Any) -> Any:
        output = record.outputs.get(stage)
        if not isinstance(output, output_type):
            raise RuntimeError("stage %s has no valid output" % stage)
        return output

    def _public_view(self, record: RunRecord) -> Dict[str, Any]:
        job_output = record.outputs.get("job")
        match_output = record.outputs.get("matcher")
        gap_output = record.outputs.get("gap")
        writer_output = record.outputs.get("writer")
        critic_output = record.outputs.get("critic")

        requirements = job_output.requirements if isinstance(job_output, JobAnalysisOutput) else []
        raw_matches = match_output.matches if isinstance(match_output, ResumeMatchOutput) else []
        supported = [match for match in raw_matches if match.status == "supported" and match.evidence]
        requirement_by_id = {requirement.id: requirement for requirement in requirements}

        requirement_view = []
        for requirement in requirements:
            item = to_primitive(requirement)
            item["jobEvidence"] = _evidence_view(requirement.evidence)
            requirement_view.append(item)

        match_view = []
        for match in supported:
            requirement = requirement_by_id.get(match.requirement_id)
            item = to_primitive(match)
            item["resumeEvidence"] = _evidence_view(match.evidence[0])
            item["jobEvidence"] = _evidence_view(requirement.evidence) if requirement else None
            match_view.append(item)

        gap_view = []
        if isinstance(gap_output, GapAnalysisOutput):
            for gap in gap_output.gaps:
                requirement = requirement_by_id.get(gap.requirement_id)
                item = to_primitive(gap)
                item["action"] = gap.next_action
                item["jobEvidence"] = _evidence_view(requirement.evidence) if requirement else None
                gap_view.append(item)

        edits = writer_output.edits if isinstance(writer_output, WriterOutput) else []
        ratio = len(supported) / len(requirements) if requirements else 0.0
        score = round(50 + 40 * ratio) if requirements else 0
        evidence_claims = len(requirements) + len(supported) + len(edits)
        linked_claims = len(requirements) + sum(bool(match.evidence) for match in supported) + sum(bool(edit.evidence_ids) for edit in edits)
        coverage = round(100 * linked_claims / evidence_claims) if evidence_claims else 0

        stage_view: Dict[str, Any] = {}
        failed_stage = record.error.get("stage") if record.error else None
        for stage in STAGES:
            if failed_stage == stage:
                status = "failed"
            elif stage in record.outputs:
                status = "completed"
            elif record.attempts[stage]:
                status = "running"
            else:
                status = "queued"
            stage_view[stage] = {
                "status": status,
                "attempt": record.attempts[stage],
                "execution_count": record.execution_counts[stage],
                "output": to_primitive(record.outputs[stage]) if stage in record.outputs else None,
            }

        return {
            "run_id": record.id,
            "status": record.status,
            "target_role": record.target_role,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
            "requirements": requirement_view,
            "matches": match_view,
            "gaps": gap_view,
            "draft": edits[0].after if edits else "",
            "edits": to_primitive(edits),
            "critic": to_primitive(critic_output) if isinstance(critic_output, CriticOutput) else None,
            "score": score,
            "score_definition": "根据已获得证据支持的岗位信号计算 50-90 分，仅作辅助判断，不代表概率。",
            "coverage": coverage,
            "linkedClaims": linked_claims,
            "totalClaims": evidence_claims,
            "stages": stage_view,
            "outputs": {
                public_key: to_primitive(record.outputs[stage])
                for public_key, stage in (
                    ("job_analysis", "job"),
                    ("resume_match", "matcher"),
                    ("gap_analysis", "gap"),
                    ("writer", "writer"),
                    ("critic", "critic"),
                )
                if stage in record.outputs
            },
            "events": to_primitive(record.events),
            "attempts": dict(record.attempts),
            "execution_counts": dict(record.execution_counts),
            "error": record.error,
        }


__all__ = ["Orchestrator", "STAGES"]
