"""Recoverable lifecycle operations for imported source documents.

This module owns the small domain boundary needed by the source-management
UI.  A source is represented by the Markdown summary generated during an
import (under ``07-素材附件/来源摘要``).  Archiving a source never removes the
original attachment or any knowledge-point note.  It marks the summary and
its linked points with a durable status, updates the vault index and appends
one audit row to the operation log.

The public API is deliberately independent from the HTTP server:

``SourceLifecycleManager.propose(...)``
    Read-only preview.  It returns impact counts, a file/hash snapshot and a
    unified diff.  The returned ``hashes`` object is the optimistic-concurrency
    contract for ``commit``.

``SourceLifecycleManager.commit(...)``
    Applies one ``archive`` or ``restore`` transition atomically.  It accepts a
    proposal id, or the selector and hashes returned by ``propose``.  Repeating
    an operation is idempotent and does not add a second log row.

Permanent deletion is intentionally outside this boundary.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import tempfile
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from uuid import uuid4

from .transaction import VAULT_TRANSACTION_LOCK
from .vault import INDEX_NAME, LOG_NAME, NoteVaultError


SOURCE_SUMMARY_DIRECTORY = "07-素材附件/来源摘要"
RAW_SOURCE_DIRECTORY = "07-素材附件/原始资料"
KNOWLEDGE_POINT_DIRECTORY = "08-知识点"
SOURCE_STATUS_ACTIVE = "active"
SOURCE_STATUS_ARCHIVED = "archived"
SOURCE_ACTION_ARCHIVE = "archive"
SOURCE_ACTION_RESTORE = "restore"
SOURCE_ACTIONS = {SOURCE_ACTION_ARCHIVE, SOURCE_ACTION_RESTORE}
SOURCE_ACTION_ALIASES = {
    "archive": SOURCE_ACTION_ARCHIVE,
    "archived": SOURCE_ACTION_ARCHIVE,
    "hide": SOURCE_ACTION_ARCHIVE,
    "hidden": SOURCE_ACTION_ARCHIVE,
    "归档": SOURCE_ACTION_ARCHIVE,
    "隐藏": SOURCE_ACTION_ARCHIVE,
    "restore": SOURCE_ACTION_RESTORE,
    "unarchive": SOURCE_ACTION_RESTORE,
    "unhide": SOURCE_ACTION_RESTORE,
    "恢复": SOURCE_ACTION_RESTORE,
}

SOURCE_LIFECYCLE_MARKER_START = "<!-- SOURCE_LIFECYCLE_START -->"
SOURCE_LIFECYCLE_MARKER_END = "<!-- SOURCE_LIFECYCLE_END -->"
SOURCE_LIFECYCLE_ID_RE = re.compile(
    r"<!--\s*source-lifecycle-id:\s*([A-Za-z0-9][A-Za-z0-9._-]{0,127})\s*-->"
)
SAFE_OPERATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
FRONTMATTER_LINE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):(?:\s*(.*))?$")
WIKILINK_RE = re.compile(r"\[\[([^\]\n]+)\]\]")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _normalise(value: Any) -> str:
    return unicodedata.normalize("NFC", str(value or "")).strip()


def _one_line(value: Any, fallback: str = "待补充") -> str:
    text = _normalise(value).replace("\r", " ").replace("\n", " ")
    text = re.sub(r"\s+", " ", text).replace("|", "／").strip()
    return text or fallback


def _safe_relative_path(
    root: Path,
    value: Any,
    field: str,
    must_exist: bool = True,
    require_markdown: bool = True,
) -> Path:
    """Resolve a vault-relative Markdown path without allowing traversal."""

    raw = _normalise(value).replace("\\", "/")
    if not raw or raw.startswith("/") or re.match(r"^[A-Za-z]:", raw) or "\x00" in raw:
        raise NoteVaultError("invalid_%s" % field, "%s must be a vault-relative Markdown path" % field, 400)
    parts = Path(raw).parts
    if any(part in {"", ".", ".."} for part in parts) or (
        require_markdown and not raw.casefold().endswith(".md")
    ):
        raise NoteVaultError("invalid_%s" % field, "%s must be a vault-relative .md path" % field, 400)
    root_resolved = root.resolve()
    candidate = (root_resolved / Path(*parts)).resolve()
    try:
        candidate.relative_to(root_resolved)
    except ValueError as exc:
        raise NoteVaultError("invalid_%s" % field, "%s must stay inside the vault" % field, 400) from exc
    if must_exist and not candidate.is_file():
        raise NoteVaultError("source_not_found", "%s does not exist" % field, 404)
    return candidate


def _parse_frontmatter(text: str) -> Dict[str, str]:
    """Parse scalar frontmatter values while leaving the original text intact."""

    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    closing = next((index for index in range(1, len(lines)) if lines[index].strip() == "---"), None)
    if closing is None:
        return {}
    metadata: Dict[str, str] = {}
    for line in lines[1:closing]:
        match = FRONTMATTER_LINE_RE.match(line)
        if not match:
            continue
        value = (match.group(2) or "").strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            try:
                if value[0] == '"':
                    decoded = json.loads(value)
                    value = str(decoded) if not isinstance(decoded, (dict, list)) else value
                else:
                    value = value[1:-1]
            except (ValueError, TypeError):
                value = value[1:-1]
        metadata[match.group(1)] = value
    return metadata


def _frontmatter_bounds(text: str) -> Optional[Tuple[List[str], int]]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    closing = next((index for index in range(1, len(lines)) if lines[index].strip() == "---"), None)
    if closing is None:
        return None
    return lines, closing


def _render_frontmatter_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value)
    # Human-readable enum/date values can stay unquoted.  JSON quoting keeps
    # Chinese labels and punctuation valid YAML scalars.
    if re.fullmatch(r"[A-Za-z0-9._:-]+", text):
        return text
    return json.dumps(text, ensure_ascii=False)


def _set_frontmatter_fields(text: str, fields: Mapping[str, Any]) -> str:
    """Set/remove scalar keys in frontmatter while preserving the body."""

    bounds = _frontmatter_bounds(text)
    if bounds is None:
        # Source and point notes generated by the application always have
        # frontmatter, but adding one here makes the lifecycle safe for older
        # hand-written summaries too.
        lines = ["---"]
        for key, value in fields.items():
            if value is not None:
                lines.append("%s: %s" % (key, _render_frontmatter_value(value)))
        lines.extend(["---", ""])
        return "\n".join(lines) + text.lstrip()

    lines, closing = bounds
    owned = set(fields)
    output: List[str] = [lines[0]]
    for line in lines[1:closing]:
        match = FRONTMATTER_LINE_RE.match(line)
        if match and match.group(1) in owned:
            continue
        output.append(line)
    for key, value in fields.items():
        if value is not None:
            output.append("%s: %s" % (key, _render_frontmatter_value(value)))
    output.append("---")
    output.extend(lines[closing + 1 :])
    return "\n".join(output).rstrip("\n") + "\n"


def _refresh_updated(text: str, date: str) -> str:
    return _set_frontmatter_fields(text, {"updated": date})


def _iter_markdown(root: Path, directory: str) -> Iterable[Path]:
    base = (root / directory).resolve()
    if not base.is_dir():
        return
    for current, directories, filenames in os.walk(base):
        directories[:] = sorted(
            (name for name in directories if not name.startswith(".")),
            key=str.casefold,
        )
        current_path = Path(current)
        for filename in sorted(filenames, key=lambda value: (value.casefold(), value)):
            if filename.startswith(".") or not filename.casefold().endswith(".md"):
                continue
            path = current_path / filename
            if path.is_file() and not path.is_symlink():
                yield path


def _wikilink_target(value: Any) -> str:
    text = _normalise(value)
    if text.startswith("[[") and text.endswith("]]"):
        text = text[2:-2]
    text = text.split("|", 1)[0].split("#", 1)[0].strip()
    text = text.replace("\\", "/")
    return Path(text).stem if text.casefold().endswith(".md") else text


def _source_status(metadata: Mapping[str, Any]) -> str:
    value = _normalise(metadata.get("source_status", "" )).casefold()
    if value in {SOURCE_STATUS_ARCHIVED, "archive", "hidden", "已归档", "归档", "inactive"}:
        return SOURCE_STATUS_ARCHIVED
    return SOURCE_STATUS_ACTIVE


def _canonical_relative(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _manifest_hash(entries: Sequence[Tuple[str, str]]) -> str:
    payload = "\n".join("%s\0%s" % (path, digest) for path, digest in sorted(entries))
    return _sha256_text(payload)


def _read_utf8(path: Path) -> Tuple[bytes, str]:
    try:
        data = path.read_bytes()
        return data, data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise NoteVaultError("source_read_error", "%s must be UTF-8 Markdown" % path.name, 422) from exc
    except OSError as exc:
        raise NoteVaultError("vault_read_error", str(exc), 500) from exc


def _atomic_replace_bytes(path: Path, value: bytes) -> None:
    """Write bytes through a sibling temporary and atomically replace path."""

    fd, temporary_name = tempfile.mkstemp(
        prefix=".%s-%s-" % (path.name, os.getpid()),
        suffix=".tmp",
        dir=str(path.parent),
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary), str(path))
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


class SourceLifecycleManager:
    """Plan and apply recoverable source archive/restore transitions."""

    def __init__(self, root: Path):
        self.root = Path(root).expanduser().resolve()
        self._proposals: Dict[str, Dict[str, Any]] = {}

    @staticmethod
    def _validate_action(value: Any) -> str:
        action = _normalise(value).casefold()
        if action in {"delete", "永久删除", "remove", "purge", "permanent-delete"}:
            raise NoteVaultError(
                "permanent_delete_unsupported",
                "Permanent source deletion is intentionally not supported; use archive or restore",
                409,
            )
        action = SOURCE_ACTION_ALIASES.get(action, action)
        if action not in SOURCE_ACTIONS:
            raise NoteVaultError("invalid_source_action", "action must be archive or restore", 400)
        return action

    @staticmethod
    def _validate_operation_id(value: Any) -> str:
        raw = _normalise(value)
        if not raw:
            return uuid4().hex
        if not SAFE_OPERATION_ID_RE.fullmatch(raw):
            raise NoteVaultError(
                "invalid_operation_id",
                "operation_id must contain only letters, numbers, dot, underscore, or hyphen",
                400,
            )
        return raw

    def _summary_candidates(self) -> List[Path]:
        return list(_iter_markdown(self.root, SOURCE_SUMMARY_DIRECTORY))

    def _resolve_summary(
        self,
        source_path: str = "",
        summary_path: str = "",
        source_id: str = "",
    ) -> Path:
        supplied = [value for value in (summary_path, source_path) if _normalise(value)]
        if len(supplied) > 1 and _normalise(summary_path) != _normalise(source_path):
            raise NoteVaultError("ambiguous_source", "summary_path and source_path must refer to the same summary", 400)
        path_value = _normalise(summary_path or source_path)
        if path_value:
            raw_attachment_selector = path_value.casefold().startswith(
                RAW_SOURCE_DIRECTORY.casefold().replace("\\", "/") + "/"
            )
            path = _safe_relative_path(
                self.root,
                path_value,
                "summary_path",
                require_markdown=not raw_attachment_selector,
            )
            summary_prefix = SOURCE_SUMMARY_DIRECTORY.casefold() + "/"
            relative = _canonical_relative(self.root, path)
            if relative.casefold().startswith(summary_prefix):
                return path
            # The graph payload also exposes source_path for the original
            # attachment. Accept that path as a selector while mutating only
            # the summary and knowledge-point notes.
            raw_prefix = RAW_SOURCE_DIRECTORY.casefold() + "/"
            if relative.casefold().startswith(raw_prefix):
                matches: List[Path] = []
                for candidate in self._summary_candidates():
                    try:
                        _, text = _read_utf8(candidate)
                    except NoteVaultError:
                        continue
                    metadata = _parse_frontmatter(text)
                    candidate_source = _normalise(metadata.get("source_path", "")).replace("\\", "/")
                    if candidate_source.casefold() == relative.casefold():
                        matches.append(candidate)
                if len(matches) == 1:
                    return matches[0]
                if len(matches) > 1:
                    raise NoteVaultError("ambiguous_source", "source_path resolves to multiple summaries", 409)
            raise NoteVaultError(
                "invalid_source_path",
                "source lifecycle only accepts a source summary or original attachment path",
                400,
            )

        identifier = _normalise(source_id)
        if not identifier:
            raise NoteVaultError("invalid_source", "summary_path or source_id is required", 400)
        target = _wikilink_target(identifier)
        matches = [path for path in self._summary_candidates() if path.stem.casefold() == target.casefold()]
        if len(matches) > 1:
            raise NoteVaultError("ambiguous_source", "source_id resolves to multiple summaries", 409)
        if not matches:
            raise NoteVaultError("source_not_found", "source summary does not exist", 404)
        return matches[0]

    def _read_base(self) -> Tuple[Path, Path, bytes, bytes]:
        index_path = self.root / INDEX_NAME
        log_path = self.root / LOG_NAME
        if not index_path.is_file() or not log_path.is_file():
            raise NoteVaultError(
                "vault_not_initialized",
                "Vault requires %s and %s before source lifecycle changes" % (INDEX_NAME, LOG_NAME),
                409,
            )
        try:
            return index_path, log_path, index_path.read_bytes(), log_path.read_bytes()
        except OSError as exc:
            raise NoteVaultError("vault_read_error", str(exc), 500) from exc

    def _linked_points(self, summary_path: Path, summary_metadata: Mapping[str, Any]) -> List[Path]:
        summary_stem = summary_path.stem
        source_path = _normalise(summary_metadata.get("source_path", "")).replace("\\", "/")
        source_file = Path(_normalise(summary_metadata.get("source_file", ""))).name
        linked: List[Path] = []
        for path in _iter_markdown(self.root, KNOWLEDGE_POINT_DIRECTORY):
            try:
                _, text = _read_utf8(path)
            except NoteVaultError:
                continue
            metadata = _parse_frontmatter(text)
            summary_value = _wikilink_target(metadata.get("source_summary", ""))
            metadata_source_path = _normalise(metadata.get("source_path", "")).replace("\\", "/")
            metadata_source_file = Path(_normalise(metadata.get("source_file", ""))).name
            body_linked = any(
                _wikilink_target(match.group(1)).casefold() == summary_stem.casefold()
                for match in WIKILINK_RE.finditer(text)
            )
            if (
                summary_value.casefold() == summary_stem.casefold()
                or (source_path and metadata_source_path == source_path)
                or (source_file and metadata_source_file.casefold() == source_file.casefold())
                or body_linked
            ):
                linked.append(path)
        linked.sort(key=lambda path: (_canonical_relative(self.root, path).casefold(), _canonical_relative(self.root, path)))
        return linked

    def _raw_attachment_paths(self, summary_metadata: Mapping[str, Any]) -> List[Path]:
        raw_root = (self.root / RAW_SOURCE_DIRECTORY).resolve()
        candidates: List[Path] = []
        source_path = _normalise(summary_metadata.get("source_path", "")).replace("\\", "/")
        if source_path:
            try:
                candidate = _safe_relative_path(
                    self.root,
                    source_path,
                    "source_attachment",
                    must_exist=False,
                    require_markdown=False,
                )
            except NoteVaultError:
                candidate = None
            if candidate is not None:
                try:
                    candidate.relative_to(raw_root)
                    if candidate.is_file():
                        candidates.append(candidate)
                except ValueError:
                    pass
        source_file = Path(_normalise(summary_metadata.get("source_file", ""))).name
        if source_file:
            for path in _iter_all_files(raw_root):
                if path.name.casefold() == source_file.casefold() and path not in candidates:
                    candidates.append(path)
        candidates.sort(key=lambda path: _canonical_relative(self.root, path))
        return candidates

    def _build_plan(self, summary_path: Path) -> Dict[str, Any]:
        index_path, log_path, index_bytes, log_bytes = self._read_base()
        summary_bytes, summary_text = _read_utf8(summary_path)
        summary_metadata = _parse_frontmatter(summary_text)
        note_type = _normalise(summary_metadata.get("type", ""))
        if note_type and note_type.casefold() != "source-summary":
            raise NoteVaultError("invalid_source", "selected note is not a source summary", 400)
        points = self._linked_points(summary_path, summary_metadata)
        point_records: List[Dict[str, Any]] = []
        originals: Dict[str, bytes] = {
            _canonical_relative(self.root, summary_path): summary_bytes,
            INDEX_NAME: index_bytes,
            LOG_NAME: log_bytes,
        }
        for point_path in points:
            point_bytes, point_text = _read_utf8(point_path)
            relative = _canonical_relative(self.root, point_path)
            originals[relative] = point_bytes
            point_records.append(
                {
                    "path": relative,
                    "name": point_path.stem,
                    "metadata": _parse_frontmatter(point_text),
                    "text": point_text,
                }
            )
        raw_paths = self._raw_attachment_paths(summary_metadata)
        raw_records: List[Dict[str, Any]] = []
        for path in raw_paths:
            try:
                data = path.read_bytes()
            except OSError:
                continue
            relative = _canonical_relative(self.root, path)
            raw_records.append({"path": relative, "name": path.name, "sha256": _sha256_bytes(data)})
            originals[relative] = data

        file_entries = [(path, _sha256_bytes(data)) for path, data in originals.items()]
        point_entries = [(record["path"], _sha256_bytes(originals[record["path"]])) for record in point_records]
        raw_entries = [(record["path"], record["sha256"]) for record in raw_records]
        summary_relative = _canonical_relative(self.root, summary_path)
        current_status = _source_status(summary_metadata)
        summary_graph_status = _normalise(summary_metadata.get("graph_status", "")).casefold()
        if current_status == SOURCE_STATUS_ACTIVE and summary_graph_status in {"hidden", "archived", "inactive"}:
            current_status = SOURCE_STATUS_ARCHIVED
        title = _one_line(summary_metadata.get("source_document", ""), summary_metadata.get("title", summary_path.stem))
        source_file = Path(_normalise(summary_metadata.get("source_file", ""))).name
        source_path = _normalise(summary_metadata.get("source_path", "")).replace("\\", "/")
        return {
            "summary_path": summary_path,
            "summary_relative": summary_relative,
            "summary_bytes": summary_bytes,
            "summary_text": summary_text,
            "summary_metadata": summary_metadata,
            "title": title,
            "source_file": source_file,
            "source_path": source_path,
            "current_status": current_status,
            "points": point_records,
            "raw": raw_records,
            "originals": originals,
            "hashes": {
                "source_summary_sha256": _sha256_bytes(summary_bytes),
                "summary_sha256": _sha256_bytes(summary_bytes),
                "points_sha256": _manifest_hash(point_entries),
                "raw_attachments_sha256": _manifest_hash(raw_entries),
                "index_sha256": _sha256_bytes(index_bytes),
                "log_sha256": _sha256_bytes(log_bytes),
                "snapshot_sha256": _manifest_hash(file_entries),
            },
        }

    @staticmethod
    def _public_source(plan: Mapping[str, Any], status: Optional[str] = None) -> Dict[str, Any]:
        metadata = plan["summary_metadata"]
        resolved_status = status or plan["current_status"]
        return {
            "id": plan["summary_path"].stem,
            "name": plan["summary_path"].stem,
            "title": plan["title"],
            "status": resolved_status,
            "source_status": resolved_status,
            "status_label": "已归档" if resolved_status == SOURCE_STATUS_ARCHIVED else "正常",
            "graph_status": "hidden" if resolved_status == SOURCE_STATUS_ARCHIVED else "active",
            "summary_path": plan["summary_relative"],
            "path": plan["summary_relative"],
            "source_path": plan["source_path"],
            "source_file": plan["source_file"],
            "wikilink": "[[%s]]" % plan["summary_path"].stem,
            "raw_attachment_count": len(plan["raw"]),
            "raw_attachments": [dict(item) for item in plan["raw"]],
            "metadata": dict(metadata),
        }

    @staticmethod
    def _impact(plan: Mapping[str, Any]) -> Dict[str, Any]:
        points = list(plan["points"])
        archived = sum(_source_status(point["metadata"]) == SOURCE_STATUS_ARCHIVED for point in points)
        return {
            "source_summaries": 1,
            "knowledge_points": len(points),
            "active_knowledge_points": len(points) - archived,
            "archived_knowledge_points": archived,
            "raw_attachments": len(plan["raw"]),
            "missing_raw_attachments": 1 if not plan["raw"] else 0,
            "index_files": 1,
            "log_files": 1,
        }

    @staticmethod
    def _public_points(plan: Mapping[str, Any]) -> List[Dict[str, Any]]:
        points: List[Dict[str, Any]] = []
        for item in plan["points"]:
            metadata = item["metadata"]
            points.append(
                {
                    "id": item["name"],
                    "name": item["name"],
                    "path": item["path"],
                    "status": _source_status(metadata),
                    "source_status": _source_status(metadata),
                    "title": _one_line(metadata.get("title", ""), item["name"]),
                }
            )
        return points

    def _snapshot(self, plan: Mapping[str, Any]) -> Dict[str, Any]:
        files: List[Dict[str, Any]] = []
        summary_relative = plan["summary_relative"]
        for path, digest in sorted(
            ((path, _sha256_bytes(data)) for path, data in plan["originals"].items()),
            key=lambda item: item[0],
        ):
            if path == summary_relative:
                role = "source-summary"
            elif path == INDEX_NAME:
                role = "index"
            elif path == LOG_NAME:
                role = "operation-log"
            elif path.startswith(KNOWLEDGE_POINT_DIRECTORY + "/"):
                role = "knowledge-point"
            elif path.startswith(RAW_SOURCE_DIRECTORY + "/"):
                role = "raw-attachment"
            else:
                role = "vault-file"
            files.append({"path": path, "sha256": digest, "role": role})
        return {"files": files, "sha256": plan["hashes"]["snapshot_sha256"]}

    @staticmethod
    def _source_selector(plan: Mapping[str, Any]) -> Dict[str, str]:
        return {
            "summary_path": plan["summary_relative"],
            "source_id": plan["summary_path"].stem,
        }

    def inspect(
        self,
        source_path: str = "",
        summary_path: str = "",
        source_id: str = "",
    ) -> Dict[str, Any]:
        """Return current source status and impact without changing files."""

        with VAULT_TRANSACTION_LOCK:
            plan = self._build_plan(self._resolve_summary(source_path, summary_path, source_id))
            impact = self._impact(plan)
            status = plan["current_status"]
            return {
                "status": "ok",
                "action": None,
                "idempotent": False,
                "source": self._public_source(plan),
                "impact": impact,
                "knowledge_points": self._public_points(plan),
                "hashes": dict(plan["hashes"]),
                "snapshot": self._snapshot(plan),
                "selector": self._source_selector(plan),
                "capabilities": {
                    "can_archive": status != SOURCE_STATUS_ARCHIVED,
                    "can_restore": status == SOURCE_STATUS_ARCHIVED,
                    "permanent_delete": False,
                },
            }

    @staticmethod
    def _index_lifecycle_text(original: str, plan: Mapping[str, Any], action: str, date: str) -> str:
        text = _refresh_updated(original, date).rstrip()
        summary_link = "[[%s]]" % plan["summary_path"].stem
        source_name = plan["source_file"] or Path(plan["source_path"]).name or "待补充"
        source_link = "[[%s]]" % source_name
        label = "已归档" if action == SOURCE_ACTION_ARCHIVE else "已恢复"
        row = "- %s：%s（来源文档：%s；关联知识点：%d；原始附件保留）" % (
            summary_link,
            label,
            source_link,
            len(plan["points"]),
        )
        start = text.find(SOURCE_LIFECYCLE_MARKER_START)
        end = text.find(SOURCE_LIFECYCLE_MARKER_END, start + len(SOURCE_LIFECYCLE_MARKER_START)) if start >= 0 else -1
        if start >= 0 and end >= 0:
            block = text[start + len(SOURCE_LIFECYCLE_MARKER_START) : end]
            lines = [line for line in block.splitlines() if summary_link not in line]
            replacement = "\n".join(lines + [row]).strip()
            return (
                text[:start]
                + SOURCE_LIFECYCLE_MARKER_START
                + "\n"
                + replacement
                + "\n"
                + SOURCE_LIFECYCLE_MARKER_END
                + text[end + len(SOURCE_LIFECYCLE_MARKER_END) :]
            ).rstrip() + "\n"
        section = (
            "\n\n## 来源生命周期\n\n"
            + SOURCE_LIFECYCLE_MARKER_START
            + "\n"
            + row
            + "\n"
            + SOURCE_LIFECYCLE_MARKER_END
            + "\n"
        )
        return text + section

    @staticmethod
    def _log_lifecycle_text(
        original: str,
        plan: Mapping[str, Any],
        action: str,
        operation_id: str,
        timestamp: str,
    ) -> str:
        summary_link = "[[%s]]" % plan["summary_path"].stem
        source_name = plan["source_file"] or Path(plan["source_path"]).name or "待补充"
        source_link = "[[%s]]" % source_name
        action_label = "归档来源" if action == SOURCE_ACTION_ARCHIVE else "恢复来源"
        status_label = "已归档" if action == SOURCE_ACTION_ARCHIVE else "已恢复"
        row = (
            "| %s | %s | %s | %s | 完成（%s，关联 %d 个知识点；原始附件保留） "
            "<!-- source-lifecycle-id: %s --> |"
            % (timestamp, action_label, summary_link, source_link, status_label, len(plan["points"]), operation_id)
        )
        return original.rstrip() + "\n" + row + "\n"

    @staticmethod
    def _diff(before: str, after: str, path: str) -> str:
        return "".join(
            difflib.unified_diff(
                before.splitlines(True),
                after.splitlines(True),
                fromfile=path + ".before",
                tofile=path + ".after",
            )
        )

    def _transition(self, plan: Mapping[str, Any], action: str, operation_id: str) -> Dict[str, Any]:
        date_now = datetime.now().astimezone()
        date = date_now.date().isoformat()
        timestamp = date_now.strftime("%Y-%m-%d %H:%M")
        target_status = SOURCE_STATUS_ARCHIVED if action == SOURCE_ACTION_ARCHIVE else SOURCE_STATUS_ACTIVE
        summary_metadata = plan["summary_metadata"]
        summary_fields: Dict[str, Any] = {
            "source_status": target_status,
            "source_lifecycle_action": action,
            "source_lifecycle_operation_id": operation_id,
            "source_lifecycle_updated": timestamp,
            "updated": date,
        }
        if action == SOURCE_ACTION_ARCHIVE:
            if _normalise(summary_metadata.get("graph_status", "" )).casefold() not in {"", "hidden"}:
                summary_fields["source_previous_graph_status"] = summary_metadata.get("graph_status")
            summary_fields["graph_status"] = "hidden"
        else:
            previous_graph = _normalise(summary_metadata.get("source_previous_graph_status", ""))
            summary_fields["graph_status"] = previous_graph or "active"
            summary_fields["source_previous_graph_status"] = None

        updates: Dict[str, bytes] = {}
        summary_after = _set_frontmatter_fields(plan["summary_text"], summary_fields)
        updates[plan["summary_relative"]] = summary_after.encode("utf-8")
        for point in plan["points"]:
            metadata = point["metadata"]
            fields: Dict[str, Any] = {
                "source_status": target_status,
                "source_lifecycle_action": action,
                "source_lifecycle_operation_id": operation_id,
                "source_lifecycle_updated": timestamp,
                "updated": date,
            }
            if action == SOURCE_ACTION_ARCHIVE:
                previous = _normalise(metadata.get("source_status", ""))
                if previous and previous.casefold() != SOURCE_STATUS_ARCHIVED:
                    fields["source_previous_status"] = previous
            else:
                fields["source_status"] = _normalise(metadata.get("source_previous_status", "")) or SOURCE_STATUS_ACTIVE
                fields["source_previous_status"] = None
            after = _set_frontmatter_fields(point["text"], fields)
            updates[point["path"]] = after.encode("utf-8")

        original_index = plan["originals"][INDEX_NAME].decode("utf-8")
        original_log = plan["originals"][LOG_NAME].decode("utf-8")
        next_index = self._index_lifecycle_text(original_index, plan, action, date)
        next_log = self._log_lifecycle_text(original_log, plan, action, operation_id, timestamp)
        updates[INDEX_NAME] = next_index.encode("utf-8")
        updates[LOG_NAME] = next_log.encode("utf-8")
        changes = []
        for path, after_bytes in sorted(updates.items()):
            before_bytes = plan["originals"][path]
            if before_bytes == after_bytes:
                continue
            before_text = before_bytes.decode("utf-8")
            after_text = after_bytes.decode("utf-8")
            changes.append(
                {
                    "path": path,
                    "sha256_before": _sha256_bytes(before_bytes),
                    "sha256_after": _sha256_bytes(after_bytes),
                    "diff": self._diff(before_text, after_text, path),
                }
            )
        return {
            "updates": updates,
            "changes": changes,
            "target_status": target_status,
            "date": date,
            "timestamp": timestamp,
        }

    def propose(
        self,
        action: str,
        source_path: str = "",
        summary_path: str = "",
        source_id: str = "",
        operation_id: str = "",
        proposal_id: str = "",
    ) -> Dict[str, Any]:
        """Create a read-only archive/restore proposal."""

        normalized_action = self._validate_action(action)
        with VAULT_TRANSACTION_LOCK:
            plan = self._build_plan(self._resolve_summary(source_path, summary_path, source_id))
            desired = SOURCE_STATUS_ARCHIVED if normalized_action == SOURCE_ACTION_ARCHIVE else SOURCE_STATUS_ACTIVE
            normalized_operation = self._validate_operation_id(operation_id or proposal_id)
            already = plan["current_status"] == desired
            transition = self._transition(plan, normalized_action, normalized_operation) if not already else None
            proposal_id = normalized_operation
            public: Dict[str, Any] = {
                "status": "committed" if already else "awaiting_review",
                "idempotent": already,
                "proposal_id": proposal_id,
                "operation_id": normalized_operation,
                "action": normalized_action,
                "source": self._public_source(plan, desired if already else plan["current_status"]),
                "impact": self._impact(plan),
                "knowledge_points": self._public_points(plan),
                "selector": self._source_selector(plan),
                "hashes": dict(plan["hashes"]),
                "expected_hashes": dict(plan["hashes"]),
                "snapshot": self._snapshot(plan),
                "permanent_delete_supported": False,
                "transaction": {
                    "index": INDEX_NAME,
                    "log": LOG_NAME,
                    "log_rows_added": 0 if already else 1,
                    "created_files": [],
                    "preserved_files": [record["path"] for record in plan["raw"]],
                    "updated_files": [change["path"] for change in (transition["changes"] if transition else [])],
                },
            }
            if transition is not None:
                public["changes"] = transition["changes"]
                public["diff"] = "".join(change["diff"] for change in transition["changes"])
                self._proposals[proposal_id] = {
                    "public": public,
                    "plan": plan,
                    "action": normalized_action,
                    "operation_id": normalized_operation,
                }
            else:
                public["changes"] = []
                public["diff"] = ""
            return json.loads(json.dumps(public, ensure_ascii=False))

    def _find_operation(self, operation_id: str) -> bool:
        log_path = self.root / LOG_NAME
        if not log_path.is_file():
            return False
        try:
            text = log_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return False
        return bool(re.search(r"<!--\s*source-lifecycle-id:\s*%s\s*-->" % re.escape(operation_id), text))

    @staticmethod
    def _check_expected_hashes(plan: Mapping[str, Any], expected: Mapping[str, Any]) -> None:
        # Adapters may wrap optimistic-concurrency fields under hashes or
        # expected_hashes. Accept both shapes at this boundary.
        if isinstance(expected.get("expected_hashes"), Mapping):
            expected = expected["expected_hashes"]
        elif isinstance(expected.get("hashes"), Mapping):
            expected = expected["hashes"]
        aliases = {
            "summary_sha256": "source_summary_sha256",
            "source_sha256": "source_summary_sha256",
            "snapshot_hash": "snapshot_sha256",
            "manifest_sha256": "snapshot_sha256",
        }
        for supplied_key, expected_value in expected.items():
            key = aliases.get(str(supplied_key), str(supplied_key))
            if key not in plan["hashes"] or expected_value in (None, ""):
                continue
            if str(expected_value) != str(plan["hashes"][key]):
                raise NoteVaultError(
                    "source_lifecycle_stale",
                    "%s changed after source lifecycle preview" % key,
                    409,
                )
        snapshot = expected.get("snapshot")
        if isinstance(snapshot, Mapping):
            expected_files = snapshot.get("files")
            if isinstance(expected_files, list):
                actual = {item["path"]: item["sha256"] for item in SourceLifecycleManager._snapshot(plan)["files"]}
                for item in expected_files:
                    if not isinstance(item, Mapping) or not item.get("path"):
                        continue
                    path = str(item["path"])
                    if path in actual and str(item.get("sha256", "")) != actual[path]:
                        raise NoteVaultError("source_lifecycle_stale", "%s changed after preview" % path, 409)

    def _commit_plan(
        self,
        plan: Dict[str, Any],
        action: str,
        operation_id: str,
        expected_hashes: Optional[Mapping[str, Any]],
    ) -> Dict[str, Any]:
        desired = SOURCE_STATUS_ARCHIVED if action == SOURCE_ACTION_ARCHIVE else SOURCE_STATUS_ACTIVE
        if self._find_operation(operation_id):
            current = self._build_plan(plan["summary_path"])
            return self._result(current, action, operation_id, idempotent=True, log_rows_added=0)
        if expected_hashes:
            self._check_expected_hashes(plan, expected_hashes)
        if plan["current_status"] == desired:
            return self._result(plan, action, operation_id, idempotent=True, log_rows_added=0)

        transition = self._transition(plan, action, operation_id)
        updates: Dict[str, bytes] = transition["updates"]
        # Check all source files again immediately before replacement.  This
        # catches edits made while a caller was preparing a large proposal.
        for relative, original in plan["originals"].items():
            path = self.root / relative
            try:
                current = path.read_bytes()
            except OSError as exc:
                raise NoteVaultError("source_lifecycle_stale", "%s disappeared during commit" % relative, 409) from exc
            if current != original:
                raise NoteVaultError("source_lifecycle_stale", "%s changed during commit" % relative, 409)

        replaced: List[Tuple[Path, bytes]] = []
        # Keep the source and point notes first, then control files.  Any
        # failure rolls every replaced file back to its exact original bytes.
        order = [path for path in updates if path not in {INDEX_NAME, LOG_NAME}]
        order.extend([INDEX_NAME, LOG_NAME])
        try:
            for relative in order:
                path = self.root / relative
                before = plan["originals"][relative]
                after = updates[relative]
                if before == after:
                    continue
                _atomic_replace_bytes(path, after)
                replaced.append((path, before))
        except NoteVaultError:
            for path, before in reversed(replaced):
                try:
                    _atomic_replace_bytes(path, before)
                except OSError:
                    pass
            raise
        except Exception as exc:
            for path, before in reversed(replaced):
                try:
                    _atomic_replace_bytes(path, before)
                except OSError:
                    pass
            raise NoteVaultError("vault_write_error", "Could not persist source lifecycle transaction", 500) from exc

        after_plan = self._build_plan(plan["summary_path"])
        result = self._result(
            after_plan,
            action,
            operation_id,
            idempotent=False,
            log_rows_added=1,
            before_plan=plan,
        )
        result["changes"] = transition["changes"]
        result["diff"] = "".join(change["diff"] for change in transition["changes"])
        return result

    def _result(
        self,
        plan: Mapping[str, Any],
        action: str,
        operation_id: str,
        idempotent: bool,
        log_rows_added: int,
        before_plan: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        impact = self._impact(plan)
        return {
            "status": "committed",
            "idempotent": idempotent,
            "action": action,
            "operation_id": operation_id,
            "proposal_id": operation_id,
            "source": self._public_source(plan),
            "knowledge_points": self._public_points(plan),
            "impact": impact,
            "hashes": dict(plan["hashes"]),
            "snapshot": self._snapshot(plan),
            "permanent_delete_supported": False,
            "transaction": {
                "index": INDEX_NAME,
                "log": LOG_NAME,
                "log_rows_added": log_rows_added,
                "created_files": [],
                "preserved_files": [record["path"] for record in plan["raw"]],
                "updated_files": [
                    plan["summary_relative"],
                    *[record["path"] for record in plan["points"]],
                    INDEX_NAME,
                    LOG_NAME,
                ],
                "before_snapshot_sha256": before_plan["hashes"]["snapshot_sha256"] if before_plan else "",
                "after_snapshot_sha256": plan["hashes"]["snapshot_sha256"],
            },
            "selector": self._source_selector(plan),
        }

    def commit(
        self,
        proposal_id: str = "",
        action: str = "",
        source_path: str = "",
        summary_path: str = "",
        source_id: str = "",
        expected_hashes: Optional[Mapping[str, Any]] = None,
        operation_id: str = "",
    ) -> Dict[str, Any]:
        """Apply a reviewed transition with optimistic concurrency checks."""

        normalized_action = self._validate_action(action) if _normalise(action) else ""
        requested_id = _normalise(proposal_id or operation_id)
        with VAULT_TRANSACTION_LOCK:
            proposal = self._proposals.get(requested_id) if requested_id else None
            if proposal is not None:
                plan = proposal["plan"]
                normalized_action = normalized_action or proposal["action"]
                normalized_id = proposal["operation_id"]
            else:
                if not normalized_action:
                    raise NoteVaultError("invalid_source_action", "action must be archive or restore", 400)
                normalized_id = self._validate_operation_id(requested_id)
                plan = self._build_plan(self._resolve_summary(source_path, summary_path, source_id))
            result = self._commit_plan(plan, normalized_action, normalized_id, expected_hashes)
            # Keep the proposal briefly in process memory so a retry carrying
            # only proposal_id can still resolve its action/selector.  The
            # operation marker in the log makes the retry idempotent even
            # after a fresh manager instance is created.
            return result

    def archive(self, source_path: str = "", summary_path: str = "", source_id: str = "", **kwargs: Any) -> Dict[str, Any]:
        """Convenience wrapper for a direct archive transition."""

        proposal = self.propose(
            SOURCE_ACTION_ARCHIVE,
            source_path,
            summary_path,
            source_id,
            operation_id=kwargs.get("operation_id", ""),
            proposal_id=kwargs.get("proposal_id", ""),
        )
        if proposal.get("idempotent"):
            return proposal
        return self.commit(proposal_id=proposal["proposal_id"], expected_hashes=kwargs.get("expected_hashes"))

    def restore(self, source_path: str = "", summary_path: str = "", source_id: str = "", **kwargs: Any) -> Dict[str, Any]:
        """Convenience wrapper for a direct restore transition."""

        proposal = self.propose(
            SOURCE_ACTION_RESTORE,
            source_path,
            summary_path,
            source_id,
            operation_id=kwargs.get("operation_id", ""),
            proposal_id=kwargs.get("proposal_id", ""),
        )
        if proposal.get("idempotent"):
            return proposal
        return self.commit(proposal_id=proposal["proposal_id"], expected_hashes=kwargs.get("expected_hashes"))


def _iter_all_files(root: Path) -> Iterable[Path]:
    if not root.is_dir():
        return
    for current, directories, filenames in os.walk(root):
        directories[:] = sorted(
            (name for name in directories if not name.startswith(".")),
            key=str.casefold,
        )
        current_path = Path(current)
        for filename in sorted(filenames, key=lambda value: (value.casefold(), value)):
            path = current_path / filename
            if path.is_file() and not path.is_symlink() and not filename.startswith("."):
                yield path


__all__ = [
    "SourceLifecycleManager",
    "SOURCE_SUMMARY_DIRECTORY",
    "RAW_SOURCE_DIRECTORY",
    "KNOWLEDGE_POINT_DIRECTORY",
    "SOURCE_STATUS_ACTIVE",
    "SOURCE_STATUS_ARCHIVED",
    "SOURCE_ACTION_ARCHIVE",
    "SOURCE_ACTION_RESTORE",
    "SOURCE_ACTION_ALIASES",
    "SOURCE_LIFECYCLE_MARKER_START",
    "SOURCE_LIFECYCLE_MARKER_END",
]
