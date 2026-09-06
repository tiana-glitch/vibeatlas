"""Small, transactional persistence boundary for整理台 notes.

The note pipeline deliberately stops at a Markdown preview.  This module is
the confirmation boundary: a caller supplies a run id and the approved
Markdown, and the module writes one inbox note plus one index entry and one
operation-log row.  It does not attempt semantic merging or graph relation
inference; those are later workflow layers.

The implementation is dependency-free so it can be used by ``server.py`` and
by tests without importing the web handler.  ``00-收件箱`` is used for the MVP
because an automatically classified note must not be mistaken for a durable
knowledge point before a person reviews it.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import tempfile
import threading
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from uuid import uuid4

from .transaction import VAULT_TRANSACTION_LOCK


INDEX_NAME = "01-知识库目录.md"
LOG_NAME = "02-更新流水账.md"
INBOX_DIRECTORY = "00-收件箱"
NOTE_MARKER_START = "<!-- NOTE_ARCHIVES_START -->"
NOTE_MARKER_END = "<!-- NOTE_ARCHIVES_END -->"
KNOWLEDGE_POINTS_MARKER_START = "<!-- KNOWLEDGE_POINTS_START -->"
KNOWLEDGE_POINTS_MARKER_END = "<!-- KNOWLEDGE_POINTS_END -->"
MAX_MARKDOWN_CHARS = 1_000_000
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
FRONTMATTER_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):(?:\s*(.*))?$")
PATH_WIKILINK_RE = re.compile(r"\[\[[^\]\n]*(?:/|\\)[^\]\n]*\]\]")
LOCAL_MARKDOWN_LINK_RE = re.compile(
    r"(?<!!)\[[^\]\n]+\]\((?!https?://|mailto:)[^)\n]+\.md(?:#[^)\n]*)?\)",
    re.IGNORECASE,
)
SOURCE_RUN_RE = re.compile(r"^source_run_id:\s*[\"']?([^\"'\s]+)[\"']?\s*$", re.MULTILINE)
PROMOTION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
PROMOTION_MARKER_RE = re.compile(r"<!--\s*promotion-id:\s*([A-Za-z0-9][A-Za-z0-9._-]{0,127})\s*-->")
# A confirmed "new" promotion is a durable graph point.  Keep it separate
# from the application/method pages so the graph projection can discover it.
PROMOTION_DEFAULT_DIRECTORY = "08-知识点/收件箱整理"
PROMOTION_PROPOSAL_STORAGE_DIRECTORY = "noteflow-promotion-proposals"


class NoteVaultError(Exception):
    """A safe error that a web adapter can map to an HTTP response."""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


# The server is threaded.  A process-local lock keeps the three-file commit
# atomic for concurrent requests; the hash checks below also protect callers
# that preview in one request and confirm in another.
# Backwards-compatible public name; all vault-changing paths share one lock.
VAULT_COMMIT_LOCK = VAULT_TRANSACTION_LOCK


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _promotion_proposal_path(root: Path, proposal_id: str) -> Path:
    vault_key = hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest()[:20]
    return Path(tempfile.gettempdir()) / PROMOTION_PROPOSAL_STORAGE_DIRECTORY / vault_key / (proposal_id + ".json")


def _resolve_runtime_relative(root: Path, value: str, field: str) -> Path:
    """Resolve a stored vault-relative path, including binary evidence files."""

    raw = str(value or "").replace("\\", "/").strip()
    if not raw or raw.startswith("/") or "\x00" in raw:
        raise NoteVaultError("invalid_%s" % field, "%s is not a safe relative path" % field, 400)
    parts = Path(raw).parts
    if any(part in {"", ".", ".."} for part in parts):
        raise NoteVaultError("invalid_%s" % field, "%s is not a safe relative path" % field, 400)
    candidate = (root.resolve() / Path(*parts)).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise NoteVaultError("invalid_%s" % field, "%s must stay inside the vault" % field, 400) from exc
    return candidate


def _promotion_proposal_payload(internal: Dict[str, Any]) -> Dict[str, Any]:
    """Reduce an in-memory proposal to JSON-safe, restartable state."""

    return {
        "proposal_id": internal["proposal_id"],
        "source_path": internal["source"].relative_to(internal["vault_root"]).as_posix(),
        "target_path": internal["target"].relative_to(internal["vault_root"]).as_posix(),
        "target_exists": bool(internal["target_exists"]),
        "mode": internal["mode"],
        "title": internal["title"],
        "source_link": internal["source_link"],
        "source_label": internal["source_label"],
        "source_snapshot_name": internal.get("source_snapshot_name", ""),
        "source_snapshot_path": internal.get("source_snapshot_path", ""),
        "before": internal["before"],
        "after": internal["after"],
        "hashes": internal["hashes"],
        "public": internal["public"],
    }


def _save_promotion_proposal(root: Path, internal: Dict[str, Any]) -> None:
    payload = _promotion_proposal_payload({**internal, "vault_root": root.resolve()})
    target = _promotion_proposal_path(root, str(payload["proposal_id"]))
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(".%s.%d.tmp" % (target.name, os.getpid()))
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _load_promotion_proposal(root: Path, proposal_id: str) -> Optional[Dict[str, Any]]:
    target = _promotion_proposal_path(root, proposal_id)
    if not target.is_file():
        return None
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or str(payload.get("proposal_id", "")) != proposal_id:
            return None
        source = _resolve_vault_markdown(root, str(payload.get("source_path", "")), "source_path", must_exist=False)
        target_path = _resolve_vault_markdown(root, str(payload.get("target_path", "")), "target_path", must_exist=False)
        snapshot_path = str(payload.get("source_snapshot_path", "") or "")
        snapshot = _resolve_runtime_relative(root, snapshot_path, "source_snapshot_path") if snapshot_path else None
        try:
            source_bytes = source.read_bytes()
        except OSError:
            source_bytes = b""
        public = payload.get("public") if isinstance(payload.get("public"), dict) else {}
        return {
            "proposal_id": proposal_id,
            "source": source,
            "source_bytes": source_bytes,
            "target": target_path,
            "target_exists": bool(payload.get("target_exists", False)),
            "mode": str(payload.get("mode", "new")),
            "title": str(payload.get("title", "")),
            "source_link": str(payload.get("source_link", "")),
            "source_label": str(payload.get("source_label", "")),
            "source_snapshot": snapshot,
            "source_snapshot_name": str(payload.get("source_snapshot_name", "")),
            "source_snapshot_path": snapshot_path,
            "before": str(payload.get("before", "")),
            "after": str(payload.get("after", "")),
            "hashes": payload.get("hashes") if isinstance(payload.get("hashes"), dict) else {},
            "public": public,
        }
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError, NoteVaultError):
        return None


def _safe_component(value: Any, fallback: str = "未命名笔记", limit: int = 80) -> str:
    """Return a single safe, readable filename component."""

    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    text = re.sub(r"[\x00-\x1f\x7f]", "", text)
    text = text.replace("/", "-").replace("\\", "-")
    text = re.sub(r"[:*?\"<>|#\[\]]", "-", text)
    text = re.sub(r"\s+", "-", text)
    text = re.sub(r"-+", "-", text).strip(" .-_\t")
    if not text or text in {".", ".."}:
        text = fallback
    return text[:limit].rstrip(" .-_\t") or fallback


def _one_line(value: Any, fallback: str = "用户输入") -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ")
    text = re.sub(r"\s+", " ", text).replace("|", "／")
    # Labels and titles are rendered into Markdown tables/headings.  Prevent
    # user-provided bracket pairs from accidentally creating vault links.
    text = text.replace("[[", "［［").replace("]]", "］］").strip()
    return text or fallback


def _refresh_frontmatter_updated(text: str, date: str) -> str:
    """Refresh/create only the ``updated`` field without rewriting body text."""

    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        closing = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
        if closing is not None:
            replaced = False
            output: List[str] = []
            for index, line in enumerate(lines):
                if 0 < index < closing and re.match(r"^updated:\s*", line):
                    output.append("updated: %s" % date)
                    replaced = True
                else:
                    output.append(line)
            if not replaced:
                output.insert(closing, "updated: %s" % date)
            return "\n".join(output)
    return text


def _frontmatter_value(text: str, key: str) -> str:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return ""
    for line in lines[1:]:
        if line.strip() == "---":
            break
        match = FRONTMATTER_KEY_RE.match(line)
        if match and match.group(1) == key:
            value = (match.group(2) or "").strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            return value
    return ""


def _iter_managed_markdown(root: Path) -> Iterable[Path]:
    """Yield Markdown notes while skipping app folders and raw evidence."""

    excluded = {
        ".agents",
        ".git",
        ".obsidian",
        ".note_runs",
        ".note_runtime",
        "__pycache__",
        "build",
        "coverage",
        "dist",
        "htmlcov",
        "node_modules",
        "note_knowledge",
        "tests",
        "venv",
        "07-素材附件",
    }
    root = root.resolve()
    for current, directories, filenames in os.walk(root):
        current_path = Path(current)
        relative_parts = current_path.relative_to(root).parts
        directories[:] = sorted(
            [name for name in directories if name.casefold() not in {item.casefold() for item in excluded} and not name.startswith(".")],
            key=lambda value: (value.casefold(), value),
        )
        for filename in sorted(filenames, key=lambda value: (value.casefold(), value)):
            if filename.casefold().endswith(".md") and filename.casefold() != "readme.md":
                path = current_path / filename
                if not path.is_symlink():
                    yield path


def _all_note_stems(root: Path) -> set[str]:
    # Basename links are resolved against the whole vault.  Include raw
    # Markdown evidence in the reservation set even though it is excluded
    # from managed-note scans and the graph projection.
    excluded = {
        ".agents",
        ".git",
        ".obsidian",
        ".note_runs",
        ".note_runtime",
        "__pycache__",
        "build",
        "coverage",
        "dist",
        "htmlcov",
        "node_modules",
        "note_knowledge",
        "tests",
        "venv",
    }
    names: set[str] = set()
    for current, directories, filenames in os.walk(root.resolve()):
        directories[:] = [
            name
            for name in directories
            if name.casefold() not in {item.casefold() for item in excluded} and not name.startswith(".")
        ]
        names.update(
            unicodedata.normalize("NFC", Path(filename).stem).casefold()
            for filename in filenames
            if filename.casefold().endswith(".md")
        )
    return names


def _unique_note_name(root: Path, desired: str) -> str:
    """Choose a globally unique basename without overwriting an existing note."""

    existing = _all_note_stems(root)
    candidate = unicodedata.normalize("NFC", _safe_component(desired))
    if candidate.casefold() not in existing:
        return candidate
    for index in range(2, 10_000):
        alternative = "%s-%d" % (candidate, index)
        if unicodedata.normalize("NFC", alternative).casefold() not in existing:
            return alternative
    raise NoteVaultError("note_name_conflict", "Could not allocate a unique note filename", 409)


def _promotion_snapshot_name(root: Path, source: Path, promotion_id: str) -> str:
    """Allocate a globally unique Markdown evidence filename for a promotion."""

    desired = _safe_component(
        "收件箱-%s-%s" % (source.stem, promotion_id[:10]),
        "收件箱来源",
        120,
    )
    return _unique_note_name(root, desired) + ".md"


def _extract_title(markdown: str) -> str:
    title = _frontmatter_value(markdown, "title")
    if title:
        return _one_line(title, "未命名笔记")[:80]
    for line in markdown.splitlines():
        match = re.match(r"^\s*#\s+(.+?)\s*$", line)
        if match:
            return _one_line(match.group(1), "未命名笔记")[:80]
    for line in markdown.splitlines():
        value = line.strip()
        if value and value != "---" and not value.startswith(">"):
            return _one_line(value.lstrip("- "), "未命名笔记")[:80]
    return "未命名笔记"


def _validate_markdown(markdown: Any) -> str:
    if not isinstance(markdown, str) or not markdown.strip():
        raise NoteVaultError("invalid_markdown", "approved_markdown must be a non-empty string", 400)
    if len(markdown) > MAX_MARKDOWN_CHARS:
        raise NoteVaultError("markdown_too_large", "approved_markdown is too large", 413)
    if "\x00" in markdown:
        raise NoteVaultError("invalid_markdown", "approved_markdown contains a NUL character", 400)
    # The vault contract uses basename wikilinks.  Rejecting malformed links
    # makes the confirmation boundary explicit instead of silently changing a
    # user's approved text.
    if PATH_WIKILINK_RE.search(markdown):
        raise NoteVaultError("path_style_wikilink", "Use basename wikilinks such as [[文件名]], not paths", 400)
    if LOCAL_MARKDOWN_LINK_RE.search(markdown):
        raise NoteVaultError("local_markdown_link", "Use [[文件名]] for local note links", 400)
    return markdown.replace("\r\n", "\n").replace("\r", "\n").strip() + "\n"


def _add_metadata(markdown: str, run_id: str, title: str, date: str, source_label: str) -> str:
    """Add stable inbox metadata while preserving the approved Markdown body."""

    metadata = {
        "type": "inbox",
        "status": "待整理",
        "created": date,
        "updated": date,
        "source_run_id": run_id,
        "source_label": _one_line(source_label),
    }
    lines = markdown.rstrip("\n").split("\n")
    if lines and lines[0].strip() == "---":
        closing = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
        if closing is not None:
            # Remove only scalar keys we own, then append canonical values. This
            # avoids duplicate source_run_id markers after a user edits a draft.
            preserved: List[str] = [lines[0]]
            owned = set(metadata) | {"title"}
            for line in lines[1:closing]:
                match = FRONTMATTER_KEY_RE.match(line)
                if match and match.group(1) in owned:
                    continue
                preserved.append(line)
            preserved.extend(
                [
                    'title: %s' % json.dumps(title, ensure_ascii=False),
                    "type: inbox",
                    "status: 待整理",
                    "created: %s" % date,
                    "updated: %s" % date,
                    'source_run_id: %s' % json.dumps(run_id, ensure_ascii=False),
                    'source_label: %s' % json.dumps(_one_line(source_label), ensure_ascii=False),
                    "---",
                ]
            )
            preserved.extend(lines[closing + 1 :])
            return "\n".join(preserved).rstrip() + "\n"
    header = [
        "---",
        'title: %s' % json.dumps(title, ensure_ascii=False),
        "type: inbox",
        "status: 待整理",
        "created: %s" % date,
        "updated: %s" % date,
        'source_run_id: %s' % json.dumps(run_id, ensure_ascii=False),
        'source_label: %s' % json.dumps(_one_line(source_label), ensure_ascii=False),
        "---",
        "",
    ]
    return "\n".join(header) + markdown.lstrip()


def _index_with_note(original: str, note_name: str, title: str, date: str) -> str:
    text = _refresh_frontmatter_updated(original, date).rstrip()
    row = "- [[%s]]：%s（收件箱，待整理）" % (note_name, _one_line(title, "未命名笔记"))
    if "[[%s]]" % note_name in text:
        return text + "\n"
    start = text.find(NOTE_MARKER_START)
    end = text.find(NOTE_MARKER_END, start + len(NOTE_MARKER_START)) if start >= 0 else -1
    if start >= 0 and end >= 0:
        insertion = end
        before = text[:insertion].rstrip()
        after = text[insertion:]
        return before + "\n" + row + "\n" + after + "\n"
    section = (
        "\n\n## 笔记整理收录\n\n"
        + NOTE_MARKER_START
        + "\n"
        + row
        + "\n"
        + NOTE_MARKER_END
        + "\n"
    )
    return text + section


def _log_with_note(original: str, note_name: str, source_label: str, date: str, timestamp: str) -> str:
    text = _refresh_frontmatter_updated(original, date).rstrip()
    row = "| %s | 整理 | [[%s]] | %s | 完成（已写入收件箱） |" % (
        timestamp,
        note_name,
        _one_line(source_label),
    )
    return text + "\n" + row + "\n"


def _find_committed_note(root: Path, run_id: str) -> Optional[Path]:
    matches: List[Path] = []
    for path in _iter_managed_markdown(root):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        marker = _frontmatter_value(text, "source_run_id")
        if marker == run_id:
            matches.append(path)
            continue
        # Be tolerant of hand-written frontmatter that uses an unquoted value,
        # but still compare the captured id; any other run must not make this
        # commit look idempotent.
        fallback = SOURCE_RUN_RE.search(text)
        if fallback and fallback.group(1).strip() == run_id:
            matches.append(path)
    if len(matches) > 1:
        rendered = ", ".join(str(path.relative_to(root)) for path in matches)
        raise NoteVaultError("duplicate_commit_marker", "Multiple notes use source run id: %s" % rendered, 409)
    return matches[0] if matches else None


def _result_for_existing(root: Path, path: Path) -> Dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    title = _frontmatter_value(text, "title") or path.stem
    run_id = _frontmatter_value(text, "source_run_id")
    return {
        "status": "committed",
        "idempotent": True,
        "run_id": run_id,
        "note": {
            "name": path.stem,
            "title": title,
            "path": path.relative_to(root).as_posix(),
        },
        "transaction": {
            "index": INDEX_NAME,
            "log": LOG_NAME,
            "log_rows_added": 0,
            "created_files": [],
            "updated_files": [],
        },
    }


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _directory_sha256(path: Path) -> str:
    """Hash a directory's relative file names and bytes deterministically."""

    if not path.is_dir():
        return _sha256_bytes(b"")
    entries: List[bytes] = []
    for current, directories, filenames in os.walk(path):
        directories[:] = sorted(name for name in directories if not name.startswith("."))
        current_path = Path(current)
        for filename in sorted(filenames, key=lambda value: (value.casefold(), value)):
            if filename.startswith("."):
                continue
            candidate = current_path / filename
            if not candidate.is_file() or candidate.is_symlink():
                continue
            try:
                data = candidate.read_bytes()
            except OSError as exc:
                raise NoteVaultError("vault_read_error", str(exc), 500) from exc
            relative = candidate.relative_to(path).as_posix().encode("utf-8")
            entries.append(relative + b"\0" + _sha256_bytes(data).encode("ascii"))
    return _sha256_bytes(b"\n".join(entries))


def _resolve_vault_markdown(root: Path, value: Any, field: str, must_exist: bool = True) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise NoteVaultError("invalid_%s" % field, "%s is required" % field, 400)
    raw = value.strip().replace("\\", "/")
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw) or "\x00" in raw:
        raise NoteVaultError("invalid_%s" % field, "%s must be a vault-relative Markdown path" % field, 400)
    parts = Path(raw).parts
    if any(part in {"", ".", ".."} for part in parts) or not raw.casefold().endswith(".md"):
        raise NoteVaultError("invalid_%s" % field, "%s must be a vault-relative .md path" % field, 400)
    if parts[0].startswith(".") or parts[0].casefold() in {
        "tests",
        "node_modules",
        "note_knowledge",
        "career_copilot".casefold(),
        "99-模板".casefold(),
        "07-素材附件".casefold(),
        "vendor",
        "src",
    }:
        raise NoteVaultError("invalid_%s" % field, "%s points to a protected vault directory" % field, 400)
    if len(parts) == 1 and parts[0].casefold() in {
        "readme.md",
        "00-知识库说明.md".casefold(),
        INDEX_NAME.casefold(),
        LOG_NAME.casefold(),
    }:
        raise NoteVaultError("invalid_%s" % field, "%s points to a protected vault file" % field, 400)
    candidate = (root.resolve() / Path(*parts)).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise NoteVaultError("invalid_%s" % field, "%s must stay inside the vault" % field, 400) from exc
    if must_exist and not candidate.is_file():
        raise NoteVaultError("source_not_found" if field == "source_path" else "target_not_found", "%s does not exist" % field, 404)
    return candidate


def _promotion_source_link(path: Path) -> str:
    name = path.name
    if re.search(r"[\x00-\x1f\x7f\[\]\|#]", name):
        raise NoteVaultError(
            "invalid_source_path",
            "source basename contains unsupported wikilink characters",
            400,
        )
    return path.stem


def _strip_markdown_frontmatter(markdown: str) -> str:
    lines = markdown.splitlines()
    if lines and lines[0].strip() == "---":
        closing = next((index for index in range(1, len(lines)) if lines[index].strip() == "---"), None)
        if closing is not None:
            return "\n".join(lines[closing + 1 :])
    return markdown


def _promotion_diff(before: str, after: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(True),
            after.splitlines(True),
            fromfile="before.md",
            tofile="after.md",
        )
    )


def _promotion_index_table_rows(text: str, heading: str) -> List[str]:
    """Read data rows from one of the two knowledge-point index tables."""

    match = re.search(
        r"^###\s+%s\s*$\n(.*?)(?=^#{1,3}\s|\Z)" % re.escape(heading),
        text,
        flags=re.MULTILINE | re.DOTALL,
    )
    if not match:
        return []
    rows: List[str] = []
    for line in match.group(1).splitlines():
        value = line.strip()
        if not (value.startswith("|") and value.endswith("|")):
            continue
        cells = [cell.strip() for cell in value.strip("|").split("|")]
        if not cells or all(re.fullmatch(r":?-+:?", cell) for cell in cells):
            continue
        if cells[0] in {"来源文件", "知识点"} or all(cell == "待补充" for cell in cells):
            continue
        rows.append(value)
    return rows


def _promotion_table_first_cell(row: str) -> str:
    cells = [cell.strip() for cell in row.strip().strip("|").split("|")]
    return cells[0] if cells else ""


def _promotion_index_views(
    existing: str,
    note_name: str,
    title: str,
    source_file: str,
    source_document: str,
) -> str:
    """Render the canonical source and point views for a promoted point.

    A promotion's ``source_file`` is the byte-for-byte evidence snapshot.  It
    is therefore the link used in both tables (rather than the inbox note
    link, which is only a convenient navigation link in the note body).
    """

    source_link = "[[%s]]" % source_file
    note_link = "[[%s]]" % note_name
    source_rows = [
        row
        for row in _promotion_index_table_rows(existing, "按来源")
        if _promotion_table_first_cell(row) != source_link
    ]
    point_rows = [
        row
        for row in _promotion_index_table_rows(existing, "按知识点")
        if _promotion_table_first_cell(row) != note_link
    ]
    source_rows.append(
        "| %s | %s | %s |"
        % (
            source_link,
            _one_line(source_document or source_file, "待补充"),
            note_link,
        )
    )
    point_rows.append(
        "| %s | %s | %s |"
        % (
            note_link,
            _one_line(title, "未命名知识文档"),
            source_link,
        )
    )
    return (
        "### 按来源\n\n"
        "| 来源文件 | 来源文档 | 知识点 |\n"
        "| --- | --- | --- |\n"
        + "\n".join(source_rows)
        + "\n\n### 按知识点\n\n"
        "| 知识点 | 检索摘要 | 来源文件 |\n"
        "| --- | --- | --- |\n"
        + "\n".join(point_rows)
    )


def _promotion_index_legacy(existing: str) -> str:
    """Keep non-table material inside a knowledge-point marker block."""

    spans: List[Tuple[int, int]] = []
    headings = list(
        re.finditer(r"^###\s+(?:按来源|按知识点)\s*$", existing, flags=re.MULTILINE)
    )
    for index, heading in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(existing)
        spans.append((heading.start(), end))
    if not spans:
        legacy = existing.strip()
    else:
        pieces: List[str] = []
        cursor = 0
        for start, end in spans:
            pieces.append(existing[cursor:start])
            cursor = end
        pieces.append(existing[cursor:])
        legacy = "\n".join(pieces).strip()
    if not legacy or legacy in {"暂无已导入的知识点。", "待补充"}:
        return ""
    return "\n\n#### 既有目录记录\n\n" + re.sub(r"^#{1,6}\s+", "##### ", legacy, flags=re.MULTILINE).strip()


def _promotion_replace_knowledge_section(text: str, views: str) -> str:
    """Insert/update the marked knowledge-point section without touching the rest."""

    start_marker = KNOWLEDGE_POINTS_MARKER_START
    end_marker = KNOWLEDGE_POINTS_MARKER_END
    if start_marker in text and end_marker in text:
        prefix, remainder = text.split(start_marker, 1)
        existing, suffix = remainder.split(end_marker, 1)
        block = views + _promotion_index_legacy(existing)
        return prefix.rstrip() + "\n\n" + start_marker + "\n" + block + "\n" + end_marker + suffix

    # Older vaults may have the two tables without marker comments or a
    # wrapping ``## 知识点`` heading.  Replace that block in place so the
    # validator and Obsidian both see one authoritative pair of views.
    bare_heading = re.search(r"^###\s+按来源\s*$", text, flags=re.MULTILINE)
    if bare_heading:
        after_heading = text[bare_heading.end() :]
        next_section = re.search(r"^##\s+", after_heading, flags=re.MULTILINE)
        end = bare_heading.end() + (next_section.start() if next_section else len(after_heading))
        existing = text[bare_heading.start() : end]
        block = KNOWLEDGE_POINTS_MARKER_START + "\n" + views + _promotion_index_legacy(existing) + "\n" + KNOWLEDGE_POINTS_MARKER_END + "\n"
        return text[: bare_heading.start()] + block + text[end:]

    heading = re.search(r"^##\s+知识点\s*$", text, flags=re.MULTILINE)
    if heading:
        remainder = text[heading.end() :]
        next_section = re.search(r"^##\s+", remainder, flags=re.MULTILINE)
        end = heading.end() + (next_section.start() if next_section else len(remainder))
        existing = text[heading.end() : end]
        block = "\n\n" + KNOWLEDGE_POINTS_MARKER_START + "\n" + views + _promotion_index_legacy(existing) + "\n" + KNOWLEDGE_POINTS_MARKER_END + "\n"
        return text[: heading.end()] + block + text[end:]

    block = "\n\n## 知识点\n\n" + KNOWLEDGE_POINTS_MARKER_START + "\n" + views + "\n" + KNOWLEDGE_POINTS_MARKER_END + "\n"
    return text.rstrip() + block


def _promotion_index_text(
    original: str,
    note_name: str,
    title: str,
    source_link: str,
    mode: str,
    date: str,
    source_file: str = "",
    source_document: str = "",
) -> str:
    text = _refresh_frontmatter_updated(original, date).rstrip()

    # New promotions are durable graph points and must be represented in both
    # index views.  Append promotions keep the lightweight整理 entry; an
    # existing knowledge-point's original source mapping remains untouched.
    if mode == "new" and source_file:
        views_existing = ""
        if KNOWLEDGE_POINTS_MARKER_START in text and KNOWLEDGE_POINTS_MARKER_END in text:
            views_existing = text.split(KNOWLEDGE_POINTS_MARKER_START, 1)[1].split(KNOWLEDGE_POINTS_MARKER_END, 1)[0]
        elif re.search(r"^###\s+按来源\s*$", text, flags=re.MULTILINE):
            views_existing = text
        text = _promotion_replace_knowledge_section(
            text,
            _promotion_index_views(views_existing, note_name, title, source_file, source_document),
        ).rstrip()

    note_link = "[[%s]]" % note_name
    action = "新增知识点" if mode == "new" else "补充已有笔记"
    row = "- %s：%s（知识整理，%s；来源：[[%s]]）" % (
        note_link,
        _one_line(title, "未命名知识文档"),
        action,
        source_link,
    )
    # Do not duplicate the human-facing promotion entry if a caller retries
    # index rendering, but still allow the knowledge tables above to refresh.
    existing_row_pattern = r"^-\s+%s：.*?来源：\[\[%s\]\]" % (
        re.escape(note_link),
        re.escape(source_link),
    )
    if not re.search(existing_row_pattern, text, flags=re.MULTILINE):
        start = text.find(NOTE_MARKER_START)
        end = text.find(NOTE_MARKER_END, start + len(NOTE_MARKER_START)) if start >= 0 else -1
        if start >= 0 and end >= 0:
            before = text[:end].rstrip()
            after = text[end:]
            text = before + "\n" + row + "\n" + after
        else:
            section = "\n\n## 知识整理收录\n\n%s\n%s\n" % (NOTE_MARKER_START, row)
            text = text + section + NOTE_MARKER_END
    return text.rstrip() + "\n"


def _promotion_log_text(
    original: str,
    note_name: str,
    source_link: str,
    mode: str,
    date: str,
    timestamp: str,
    source_file: str = "",
) -> str:
    text = _refresh_frontmatter_updated(original, date).rstrip()
    action = "新增知识点" if mode == "new" else "补充已有笔记"
    source_value = "[[%s]]" % source_link
    if source_file:
        source_value += "、[[%s]]" % source_file
    row = "| %s | 整理 | [[%s]] | %s | 完成（%s） |" % (
        timestamp,
        note_name,
        source_value,
        action,
    )
    return text + "\n" + row + "\n"


def _render_new_promotion_note(
    title: str,
    body: str,
    source_link: str,
    source_path: str,
    promotion_id: str,
    date: str,
    source_file: str = "",
    source_document: str = "",
    source_snapshot_path: str = "",
) -> str:
    normalized = _validate_markdown(body)
    content = _strip_markdown_frontmatter(normalized).strip()
    if not re.search(r"^#\s+", content, flags=re.MULTILINE):
        content = "# %s\n\n%s" % (title, content)
    source_file = source_file or Path(source_path).name
    source_document = _one_line(source_document or source_link, source_link)
    source_excerpt = _one_line(_strip_markdown_frontmatter(normalized), "待补充")[:240]
    source_section = (
        "\n\n## 来源与证据\n\n"
        "- 来源笔记：[[%s]]\n"
        "- 原始证据：[[%s]]\n"
        "- 证据状态：待核验\n"
        % (source_link, source_file)
    )
    metadata = [
        "---",
        'title: %s' % json.dumps(title, ensure_ascii=False),
        "type: knowledge-point",
        "status: 待核验",
        "graph_status: active",
        "knowledge_kind: atomic-claim",
        "evidence_status: 待核验",
        "confidentiality: 私密",
        "created: %s" % date,
        "updated: %s" % date,
        'source_document: %s' % json.dumps(source_document, ensure_ascii=False),
        'source_path: %s' % json.dumps(source_path, ensure_ascii=False),
        'source_file: %s' % json.dumps(source_file, ensure_ascii=False),
        *([
            'source_snapshot_path: %s' % json.dumps(source_snapshot_path, ensure_ascii=False)
        ] if source_snapshot_path else []),
        'source_summary: %s' % json.dumps(source_link, ensure_ascii=False),
        'source_excerpt: %s' % json.dumps(source_excerpt, ensure_ascii=False),
        'summary: %s' % json.dumps(_one_line(content, title)[:180], ensure_ascii=False),
        'promotion_id: %s' % json.dumps(promotion_id, ensure_ascii=False),
        "tags: [笔记整理, 知识点]",
        "---",
        "",
    ]
    return "\n".join(metadata) + content.rstrip() + source_section + "\n<!-- promotion-id: %s -->\n" % promotion_id


def _render_append_promotion_note(before: str, content: str, source_link: str, promotion_id: str, target_section: str = "") -> str:
    normalized = _validate_markdown(content).strip()
    block = "%s\n\n来源笔记：[[%s]]\n\n<!-- promotion-id: %s -->" % (normalized, source_link, promotion_id)
    base = before.rstrip()
    if target_section:
        heading_pattern = re.compile(r"^(#{1,6})\s+%s\s*$" % re.escape(target_section.strip()), re.MULTILINE)
        match = heading_pattern.search(base)
        if not match:
            raise NoteVaultError("target_section_not_found", "target_section was not found in target note", 400)
        level = len(match.group(1))
        next_heading = re.search(r"^#{1,%d}\s+" % level, base[match.end() :], re.MULTILINE)
        insertion = match.end() + (next_heading.start() if next_heading else len(base[match.end() :]))
        left = base[:insertion].rstrip()
        right = base[insertion:].lstrip()
        return left + "\n\n" + block + ("\n\n" + right if right else "") + "\n"
    return base + "\n\n## 增量补充\n\n" + block + "\n"


def _find_committed_promotion(root: Path, promotion_id: str) -> Optional[Path]:
    for path in _iter_managed_markdown(root):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        if _frontmatter_value(text, "promotion_id") == promotion_id or PROMOTION_MARKER_RE.search(text):
            match = PROMOTION_MARKER_RE.search(text)
            if _frontmatter_value(text, "promotion_id") == promotion_id or (match and match.group(1) == promotion_id):
                return path
    return None


def _promotion_existing_result(root: Path, path: Path, promotion_id: str) -> Dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    title = _frontmatter_value(text, "title") or _extract_title(text) or path.stem
    mode = "new" if _frontmatter_value(text, "promotion_id") == promotion_id else "append"
    return {
        "status": "committed",
        "idempotent": True,
        "proposal_id": promotion_id,
        "mode": mode,
        "target": {"name": path.stem, "title": title, "path": path.relative_to(root).as_posix(), "exists": True},
        "note": {"name": path.stem, "title": title, "path": path.relative_to(root).as_posix()},
        "transaction": {
            "index": INDEX_NAME,
            "log": LOG_NAME,
            "log_rows_added": 0,
            "created_files": [],
            "updated_files": [],
        },
    }


class NotePromotionCommitter:
    """Propose and confirm promotion of a整理 note into a durable document."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self._proposals: Dict[str, Dict[str, Any]] = {}

    def _read_base(self) -> Tuple[Path, Path, str, str]:
        index_path = self.root / INDEX_NAME
        log_path = self.root / LOG_NAME
        if not index_path.is_file() or not log_path.is_file():
            raise NoteVaultError(
                "vault_not_initialized",
                "Vault requires %s and %s before promotion" % (INDEX_NAME, LOG_NAME),
                409,
            )
        try:
            index = index_path.read_text(encoding="utf-8")
            log = log_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise NoteVaultError("vault_read_error", str(exc), 500) from exc
        return index_path, log_path, index, log

    @staticmethod
    def _validate_id(value: Any) -> str:
        if not isinstance(value, str) or not PROMOTION_ID_RE.fullmatch(value.strip()):
            raise NoteVaultError("invalid_proposal_id", "proposal_id must contain only letters, numbers, dot, underscore, or hyphen", 400)
        return value.strip()

    @staticmethod
    def _validate_mode(value: Any) -> str:
        mode = str(value or "new").strip().casefold()
        if mode not in {"new", "append"}:
            raise NoteVaultError("invalid_promotion_mode", "mode must be new or append", 400)
        return mode

    def _target_by_name(self, name: str) -> Path:
        if not isinstance(name, str) or any(character in name for character in ("/", "\\", "\x00", "[", "]")):
            raise NoteVaultError("invalid_target_name", "target_name must be a safe basename", 400)
        cleaned = _safe_component(name, "未命名知识文档", 120)
        if cleaned.casefold().endswith(".md"):
            cleaned = Path(cleaned).stem
        matches = [path for path in _iter_managed_markdown(self.root) if path.stem.casefold() == cleaned.casefold()]
        if len(matches) > 1:
            raise NoteVaultError("ambiguous_target", "target_name resolves to multiple notes", 409)
        if not matches:
            raise NoteVaultError("target_not_found", "target_name does not resolve to an existing note", 404)
        return matches[0]

    def _new_target(self, target_path: str, target_name: str, title: str) -> Path:
        if target_path:
            path = _resolve_vault_markdown(self.root, target_path, "target_path", must_exist=False)
            if path.exists():
                raise NoteVaultError("note_name_conflict", "target_path already exists", 409)
            relative_parts = path.relative_to(self.root).parts
            if not relative_parts or relative_parts[0].casefold() != "08-知识点".casefold():
                raise NoteVaultError(
                    "invalid_target_path",
                    "new knowledge points must be placed under 08-知识点",
                    400,
                )
            name = path.stem
        else:
            desired = target_name or title or "未命名知识文档"
            if "/" in desired or "\\" in desired:
                raise NoteVaultError("invalid_target_name", "target_name must be a basename", 400)
            name = _safe_component(Path(desired).stem, "未命名知识文档", 120)
            allocated = _unique_note_name(self.root, name)
            path = self.root / PROMOTION_DEFAULT_DIRECTORY / (allocated + ".md")
            name = allocated
        existing_stems = _all_note_stems(self.root)
        if name.casefold() in existing_stems:
            raise NoteVaultError("note_name_conflict", "target basename already exists in the vault", 409)
        return path

    def propose(
        self,
        source_path: str,
        mode: str = "new",
        target_path: str = "",
        target_name: str = "",
        title: str = "",
        content: Optional[str] = None,
        target_section: str = "",
        proposal_id: str = "",
        source_label: str = "笔记整理",
    ) -> Dict[str, Any]:
        normalized_mode = self._validate_mode(mode)
        source = _resolve_vault_markdown(self.root, source_path, "source_path", must_exist=True)
        source_bytes = source.read_bytes()
        try:
            source_text = source_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise NoteVaultError("source_read_error", "source note must be UTF-8 Markdown", 422) from exc
        source_link = _promotion_source_link(source)
        source_title = _extract_title(source_text)
        with VAULT_COMMIT_LOCK:
            _, _, index, log = self._read_base()
            requested_id = proposal_id.strip() if proposal_id else ""
            if requested_id:
                normalized_id = self._validate_id(requested_id)
                existing_proposal = self._proposals.get(normalized_id)
                if existing_proposal is not None:
                    return existing_proposal["public"]
                persisted_proposal = _load_promotion_proposal(self.root, normalized_id)
                if persisted_proposal is not None:
                    self._proposals[normalized_id] = persisted_proposal
                    return persisted_proposal["public"]
                existing = _find_committed_promotion(self.root, normalized_id)
                if existing is not None:
                    return _promotion_existing_result(self.root, existing, normalized_id)
            else:
                normalized_id = uuid4().hex

            raw_content = source_text if content is None or (isinstance(content, str) and not content.strip()) else content
            if not isinstance(raw_content, str):
                raise NoteVaultError("invalid_content", "content must be a string", 400)
            raw_content = _strip_markdown_frontmatter(raw_content).strip()
            if not raw_content:
                raise NoteVaultError("invalid_content", "content must not be empty", 400)
            note_title = _one_line(title, _extract_title(raw_content) or source_title or "未命名知识文档")[:80]
            source_snapshot: Optional[Path] = None
            source_snapshot_name = ""
            source_snapshot_path = ""
            if normalized_mode == "new":
                target = self._new_target(target_path, target_name, note_title)
                before = ""
                source_snapshot_name = _promotion_snapshot_name(self.root, source, normalized_id)
                source_snapshot = self.root / "07-素材附件" / "原始资料" / source_snapshot_name
                source_snapshot_path = source_snapshot.relative_to(self.root).as_posix()
                after = _render_new_promotion_note(
                    note_title,
                    raw_content,
                    source_link,
                    source.relative_to(self.root).as_posix(),
                    normalized_id,
                    datetime.now().astimezone().date().isoformat(),
                    source_file=source_snapshot_name,
                    source_document=source_title,
                    source_snapshot_path=source_snapshot_path,
                )
                target_exists = False
            else:
                if target_path:
                    target = _resolve_vault_markdown(self.root, target_path, "target_path", must_exist=True)
                elif target_name:
                    target = self._target_by_name(target_name)
                else:
                    raise NoteVaultError("missing_target", "append mode requires target_path or target_name", 400)
                if target == source:
                    raise NoteVaultError("invalid_target", "source_path and target_path must be different", 400)
                try:
                    before = target.read_text(encoding="utf-8")
                except (OSError, UnicodeError) as exc:
                    raise NoteVaultError("target_read_error", str(exc), 500) from exc
                after = _validate_markdown(
                    _render_append_promotion_note(before, raw_content, source_link, normalized_id, target_section)
                )
                target_exists = True
                note_title = _extract_title(before) or note_title

            target_relative = target.relative_to(self.root).as_posix()
            hashes = {
                "source_sha256": _sha256_bytes(source_bytes),
                "target_sha256": _sha256_bytes(before.encode("utf-8")) if before else "",
                "target_dir_sha256": _directory_sha256(target.parent),
                "index_sha256": _sha256_text(index),
                "log_sha256": _sha256_text(log),
            }
            source_info = {
                "path": source.relative_to(self.root).as_posix(),
                "name": source.name,
                "title": source_title,
                "wikilink": "[[%s]]" % source_link,
                "sha256": hashes["source_sha256"],
                "label": _one_line(source_label),
            }
            if source_snapshot is not None:
                source_info["snapshot_path"] = source_snapshot_path
                source_info["snapshot_name"] = source_snapshot_name
                source_info["snapshot_wikilink"] = "[[%s]]" % source_snapshot_name
            target_info = {
                "path": target_relative,
                "name": target.stem,
                "title": note_title,
                "exists": target_exists,
            }
            public_proposal = {
                "proposal_id": normalized_id,
                "source": source_info,
                "target": target_info,
                "mode": normalized_mode,
                "before": before,
                "after": after,
                "before_markdown": before,
                "after_markdown": after,
                "diff": _promotion_diff(before, after),
                "hashes": hashes,
                "source_sha256": hashes["source_sha256"],
                "source_hash": hashes["source_sha256"],
                "target_sha256": hashes["target_sha256"],
                "target_hash": hashes["target_sha256"],
                "target_dir_sha256": hashes["target_dir_sha256"],
                "target_directory_sha256": hashes["target_dir_sha256"],
                "index_sha256": hashes["index_sha256"],
                "index_hash": hashes["index_sha256"],
                "log_sha256": hashes["log_sha256"],
                "log_hash": hashes["log_sha256"],
                "target_section": target_section,
            }
            internal = {
                "proposal_id": normalized_id,
                "source": source,
                "source_bytes": source_bytes,
                "target": target,
                "target_exists": target_exists,
                "mode": normalized_mode,
                "title": note_title,
                "source_link": source_link,
                "source_label": source_label,
                "source_snapshot": source_snapshot,
                "source_snapshot_name": source_snapshot_name,
                "source_snapshot_path": source_snapshot_path,
                "before": before,
                "after": after,
                "hashes": hashes,
                "public": {
                    "status": "awaiting_review",
                    "idempotent": False,
                    "proposal_id": normalized_id,
                    "mode": normalized_mode,
                    "source": source_info,
                    "target": target_info,
                    "before": before,
                    "after": after,
                    "before_markdown": before,
                    "after_markdown": after,
                    "diff": public_proposal["diff"],
                    "hashes": hashes,
                    "source_sha256": hashes["source_sha256"],
                    "target_sha256": hashes["target_sha256"],
                    "target_dir_sha256": hashes["target_dir_sha256"],
                    "index_sha256": hashes["index_sha256"],
                    "log_sha256": hashes["log_sha256"],
                    "proposal": public_proposal,
                },
            }
            self._proposals[normalized_id] = internal
            _save_promotion_proposal(self.root, internal)
            return internal["public"]

    def _expected_hashes(self, payload: Optional[Dict[str, Any]], expected_hashes: Optional[Dict[str, Any]]) -> Dict[str, str]:
        merged: Dict[str, str] = {}
        if isinstance(expected_hashes, dict):
            merged.update({str(key): str(value) for key, value in expected_hashes.items() if value not in (None, "")})
        if isinstance(payload, dict):
            aliases = {
                "source_sha256": ("expected_source_sha256", "source_sha256", "expected_source_hash", "source_hash"),
                "target_sha256": ("expected_target_sha256", "target_sha256", "before_hash", "expected_before_hash", "target_hash"),
                "target_dir_sha256": ("expected_target_dir_sha256", "target_dir_sha256", "target_directory_sha256", "target_dir_hash"),
                "index_sha256": ("expected_index_sha256", "index_sha256", "index_hash"),
                "log_sha256": ("expected_log_sha256", "log_sha256", "log_hash"),
            }
            for key, names in aliases.items():
                if key in merged:
                    continue
                for name in names:
                    if payload.get(name) not in (None, ""):
                        merged[key] = str(payload[name])
                        break
        return merged

    def commit(
        self,
        proposal_id: str,
        expected_hashes: Optional[Dict[str, Any]] = None,
        approved_content: Optional[str] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        normalized_id = self._validate_id(proposal_id)
        with VAULT_COMMIT_LOCK:
            completed = self._proposals.get(normalized_id)
            if completed is not None and completed.get("status") == "committed" and completed.get("result") is not None:
                return {**completed["result"], "idempotent": True}
            persisted = _find_committed_promotion(self.root, normalized_id)
            if persisted is not None:
                return _promotion_existing_result(self.root, persisted, normalized_id)
            proposal = self._proposals.get(normalized_id)
            if proposal is None:
                proposal = _load_promotion_proposal(self.root, normalized_id)
                if proposal is not None:
                    self._proposals[normalized_id] = proposal
            if proposal is None:
                raise NoteVaultError("proposal_not_found", "Promotion proposal not found; create a new proposal", 404)
            index_path, log_path, original_index, original_log = self._read_base()
            source = proposal["source"]
            target = proposal["target"]
            try:
                source_bytes = source.read_bytes()
            except OSError as exc:
                raise NoteVaultError("source_read_error", str(exc), 500) from exc
            if _sha256_bytes(source_bytes) != proposal["hashes"]["source_sha256"]:
                raise NoteVaultError("source_changed", "Source note changed after proposal", 409)
            target_exists = target.is_file()
            before_now = target.read_text(encoding="utf-8") if target_exists else ""
            current_hashes = {
                "source_sha256": _sha256_bytes(source_bytes),
                "target_sha256": _sha256_bytes(before_now.encode("utf-8")) if before_now else "",
                "target_dir_sha256": _directory_sha256(target.parent),
                "index_sha256": _sha256_text(original_index),
                "log_sha256": _sha256_text(original_log),
            }
            expected = dict(proposal["hashes"])
            expected.update(self._expected_hashes(kwargs, expected_hashes))
            for key, value in expected.items():
                if value != current_hashes.get(key, ""):
                    raise NoteVaultError("vault_changed", "%s changed after proposal" % key, 409)
            supplied_content = approved_content
            if supplied_content is None:
                supplied_content = kwargs.get("approved_markdown", kwargs.get("after", kwargs.get("content")))
            if supplied_content is not None:
                if not isinstance(supplied_content, str):
                    raise NoteVaultError("invalid_content", "approved content must be a string", 400)
                supplied_content = supplied_content.replace("\r\n", "\n").replace("\r", "\n")
                if supplied_content.strip() + "\n" != proposal["after"]:
                    raise NoteVaultError("proposal_changed", "approved content differs from the reviewed proposal", 409)
            if target_exists != bool(proposal["target_exists"]):
                raise NoteVaultError("target_changed", "Target note existence changed after proposal", 409)

            now = datetime.now().astimezone()
            date = now.date().isoformat()
            timestamp = now.strftime("%Y-%m-%d %H:%M")
            next_index = _promotion_index_text(
                original_index,
                target.stem,
                proposal["title"],
                proposal["source_link"],
                proposal["mode"],
                date,
                proposal.get("source_snapshot_name", ""),
                proposal.get("public", {}).get("source", {}).get("title", ""),
            )
            next_log = _promotion_log_text(
                original_log,
                target.stem,
                proposal["source_link"],
                proposal["mode"],
                date,
                timestamp,
                proposal.get("source_snapshot_name", ""),
            )
            self._write_transaction(
                target,
                proposal["after"],
                index_path,
                log_path,
                original_index,
                original_log,
                next_index,
                next_log,
                current_hashes,
                source_snapshot=proposal.get("source_snapshot"),
                source_bytes=proposal.get("source_bytes", b""),
            )
            result = {
                "status": "committed",
                "idempotent": False,
                "proposal_id": normalized_id,
                "mode": proposal["mode"],
                "source": proposal["public"]["source"],
                "target": {**proposal["public"]["target"], "exists": True},
                "hashes": {
                    **current_hashes,
                    "target_sha256": _sha256_bytes(proposal["after"].encode("utf-8")),
                    "target_dir_sha256": _directory_sha256(target.parent),
                    "index_sha256": _sha256_text(next_index),
                    "log_sha256": _sha256_text(next_log),
                },
                "transaction": {
                    "index": INDEX_NAME,
                    "log": LOG_NAME,
                    "log_rows_added": 1,
                    "created_files": (
                        [target.relative_to(self.root).as_posix()]
                        + ([proposal["source_snapshot_path"]] if proposal.get("source_snapshot_path") else [])
                        if proposal["mode"] == "new"
                        else []
                    ),
                    "updated_files": (
                        [target.relative_to(self.root).as_posix(), INDEX_NAME, LOG_NAME]
                        if proposal["mode"] == "append"
                        else [INDEX_NAME, LOG_NAME]
                    ),
                },
            }
            result["note"] = dict(result["target"])
            proposal["status"] = "committed"
            proposal["result"] = result
            _promotion_proposal_path(self.root, normalized_id).unlink(missing_ok=True)
            return result

    def _write_transaction(
        self,
        target: Path,
        after: str,
        index_path: Path,
        log_path: Path,
        original_index: str,
        original_log: str,
        next_index: str,
        next_log: str,
        current_hashes: Dict[str, str],
        source_snapshot: Optional[Path] = None,
        source_bytes: bytes = b"",
    ) -> None:
        target_parent = target.parent
        created_directories: List[Path] = []
        original_target_exists = target.exists()
        original_target = target.read_bytes() if original_target_exists else b""
        original_snapshot_exists = bool(source_snapshot and source_snapshot.exists())
        original_snapshot = source_snapshot.read_bytes() if original_snapshot_exists and source_snapshot else b""
        temporary: Optional[Path] = None
        temporary_snapshot: Optional[Path] = None
        replaced_target = False
        replaced_snapshot = False
        replaced_index = False
        replaced_log = False

        def ensure_parent(path: Path) -> None:
            missing: List[Path] = []
            current = path
            while not current.exists():
                missing.append(current)
                current = current.parent
            for directory in reversed(missing):
                directory.mkdir()
                created_directories.append(directory)

        try:
            ensure_parent(target_parent)
            if source_snapshot is not None:
                if original_snapshot_exists:
                    raise NoteVaultError("source_snapshot_conflict", "Source evidence snapshot already exists", 409)
                ensure_parent(source_snapshot.parent)
            fd, temporary_name = tempfile.mkstemp(prefix=".%s-" % target.stem, suffix=".tmp", dir=str(target_parent))
            temporary = Path(temporary_name)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(after)
                handle.flush()
                os.fsync(handle.fileno())
            if source_snapshot is not None:
                fd, temporary_name = tempfile.mkstemp(
                    prefix=".%s-" % source_snapshot.stem,
                    suffix=".tmp",
                    dir=str(source_snapshot.parent),
                )
                temporary_snapshot = Path(temporary_name)
                with os.fdopen(fd, "wb") as handle:
                    handle.write(source_bytes)
                    handle.flush()
                    os.fsync(handle.fileno())
            if _sha256_text(index_path.read_text(encoding="utf-8")) != current_hashes["index_sha256"] or _sha256_text(log_path.read_text(encoding="utf-8")) != current_hashes["log_sha256"] or _directory_sha256(target_parent) != current_hashes["target_dir_sha256"]:
                raise NoteVaultError("vault_changed", "Vault changed while staging promotion", 409)
            if original_target_exists:
                if not target.exists() or _sha256_bytes(target.read_bytes()) != current_hashes["target_sha256"]:
                    raise NoteVaultError("target_changed", "Target note changed while staging promotion", 409)
            elif target.exists():
                raise NoteVaultError("note_name_conflict", "Target note appeared while staging promotion", 409)
            if source_snapshot is not None and source_snapshot.exists():
                raise NoteVaultError("source_snapshot_conflict", "Source evidence snapshot appeared while staging promotion", 409)
            os.replace(str(temporary), str(target))
            temporary = None
            replaced_target = True
            if source_snapshot is not None and temporary_snapshot is not None:
                os.replace(str(temporary_snapshot), str(source_snapshot))
                temporary_snapshot = None
                replaced_snapshot = True
            _atomic_replace_text(index_path, next_index)
            replaced_index = True
            _atomic_replace_text(log_path, next_log)
            replaced_log = True
        except Exception as exc:
            if temporary is not None:
                try:
                    temporary.unlink()
                except OSError:
                    pass
            if temporary_snapshot is not None:
                try:
                    temporary_snapshot.unlink()
                except OSError:
                    pass
            if replaced_snapshot and source_snapshot is not None:
                try:
                    if original_snapshot_exists:
                        source_snapshot.write_bytes(original_snapshot)
                    else:
                        source_snapshot.unlink()
                except OSError:
                    pass
            if replaced_target:
                try:
                    if original_target_exists:
                        target.write_bytes(original_target)
                    else:
                        target.unlink()
                except OSError:
                    pass
            if replaced_index:
                _atomic_replace_text(index_path, original_index)
            if replaced_log:
                _atomic_replace_text(log_path, original_log)
            for directory in sorted(created_directories, key=lambda path: len(path.parts), reverse=True):
                try:
                    directory.rmdir()
                except OSError:
                    pass
            if isinstance(exc, NoteVaultError):
                raise
            raise NoteVaultError("vault_write_error", "Could not persist promotion transaction", 500) from exc


class NoteVaultCommitter:
    """Prepare and commit the MVP note整理 transaction."""

    def __init__(self, root: Path, inbox_directory: str = INBOX_DIRECTORY) -> None:
        self.root = Path(root).expanduser().resolve()
        self.inbox_directory = _safe_component(inbox_directory, INBOX_DIRECTORY, 80)

    def _read_base(self) -> Tuple[Path, Path, str, str]:
        index_path = self.root / INDEX_NAME
        log_path = self.root / LOG_NAME
        if not index_path.is_file() or not log_path.is_file():
            raise NoteVaultError(
                "vault_not_initialized",
                "Vault requires %s and %s before note commit" % (INDEX_NAME, LOG_NAME),
                409,
            )
        try:
            index = index_path.read_text(encoding="utf-8")
            log = log_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise NoteVaultError("vault_read_error", str(exc), 500) from exc
        return index_path, log_path, index, log

    @staticmethod
    def _validate_run_id(run_id: Any) -> str:
        if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id.strip()):
            raise NoteVaultError(
                "invalid_run_id",
                "run_id must contain only letters, numbers, dot, underscore, or hyphen",
                400,
            )
        return run_id.strip()

    def propose(
        self,
        run_id: str,
        archive_markdown: str,
        title: str = "",
        source_label: str = "用户输入",
    ) -> Dict[str, Any]:
        """Build a preview; this method never mutates the vault."""

        normalized_run_id = self._validate_run_id(run_id)
        normalized_markdown = _validate_markdown(archive_markdown)
        with VAULT_COMMIT_LOCK:
            _, _, index, log = self._read_base()
            existing = _find_committed_note(self.root, normalized_run_id)
            if existing is not None:
                result = _result_for_existing(self.root, existing)
                result["proposal"] = {"already_committed": True}
                return result
            now = datetime.now().astimezone()
            date = now.date().isoformat()
            note_title = _one_line(title, _extract_title(normalized_markdown))[:80]
            desired = _safe_component(note_title)
            note_name = _unique_note_name(self.root, desired)
            content = _add_metadata(normalized_markdown, normalized_run_id, note_title, date, source_label)
            next_index = _index_with_note(index, note_name, note_title, date)
            next_log = _log_with_note(
                log,
                note_name,
                source_label,
                date,
                now.strftime("%Y-%m-%d %H:%M"),
            )
            return {
                "status": "awaiting_review",
                "idempotent": False,
                "run_id": normalized_run_id,
                "note": {
                    "name": note_name,
                    "title": note_title,
                    "path": (Path(self.inbox_directory) / (note_name + ".md")).as_posix(),
                },
                "proposal": {
                    "markdown": content,
                    "index_markdown": next_index,
                    "log_row": next_log.splitlines()[-1],
                    "content_sha256": _sha256_text(content),
                    "index_sha256": _sha256_text(index),
                    "log_sha256": _sha256_text(log),
                },
            }

    def find_committed(self, run_id: str) -> Optional[Dict[str, Any]]:
        """Return an existing commit result for ``run_id`` without mutation.

        This small lookup lets an HTTP adapter honor an idempotent retry that
        carries only the run id, even when the transient NoteFlow run record
        is no longer available after a process restart.
        """

        normalized_run_id = self._validate_run_id(run_id)
        with VAULT_COMMIT_LOCK:
            self._read_base()
            existing = _find_committed_note(self.root, normalized_run_id)
            return _result_for_existing(self.root, existing) if existing is not None else None

    def commit(
        self,
        run_id: str,
        archive_markdown: str = "",
        title: str = "",
        source_label: str = "用户输入",
        expected_index_sha256: str = "",
        expected_log_sha256: str = "",
        note_name: str = "",
    ) -> Dict[str, Any]:
        """Persist one inbox note, one index entry, and one log row.

        Repeating the same ``run_id`` returns the original note and never adds
        another log row.  Optional base hashes let the UI reject a stale
        preview if somebody edited the vault between preview and confirmation.
        """

        normalized_run_id = self._validate_run_id(run_id)
        with VAULT_COMMIT_LOCK:
            index_path, log_path, original_index, original_log = self._read_base()
            existing = _find_committed_note(self.root, normalized_run_id)
            if existing is not None:
                # The run id is the idempotency key.  A confirmation retry may
                # contain only that key (the UI can have discarded its draft),
                # so do not require the original body or labels again.
                return _result_for_existing(self.root, existing)

            normalized_markdown = _validate_markdown(archive_markdown)

            if expected_index_sha256 and expected_index_sha256 != _sha256_text(original_index):
                raise NoteVaultError("vault_changed", "01-知识库目录.md changed after preview", 409)
            if expected_log_sha256 and expected_log_sha256 != _sha256_text(original_log):
                raise NoteVaultError("vault_changed", "02-更新流水账.md changed after preview", 409)

            now = datetime.now().astimezone()
            date = now.date().isoformat()
            timestamp = now.strftime("%Y-%m-%d %H:%M")
            note_title = _one_line(title, _extract_title(normalized_markdown))[:80]
            desired = _safe_component(note_name or note_title)
            allocated_name = _unique_note_name(self.root, desired)
            # A caller may send the name from propose().  If another note took
            # it meanwhile, allocate a suffix instead of overwriting that note.
            target_directory = self.root / self.inbox_directory
            target_path = target_directory / (allocated_name + ".md")
            content = _add_metadata(normalized_markdown, normalized_run_id, note_title, date, source_label)
            next_index = _index_with_note(original_index, allocated_name, note_title, date)
            next_log = _log_with_note(original_log, allocated_name, source_label, date, timestamp)

            created_note = False
            replaced_index = False
            replaced_log = False
            target_directory_created = False
            temporary_note: Optional[Path] = None
            try:
                if not target_directory.exists():
                    target_directory.mkdir(parents=True, exist_ok=True)
                    target_directory_created = True
                if target_path.exists():
                    raise NoteVaultError("note_name_conflict", "Target note already exists; retry the commit", 409)
                fd, temporary_name = tempfile.mkstemp(
                    prefix=".%s-" % allocated_name,
                    suffix=".tmp",
                    dir=str(target_directory),
                )
                temporary_note = Path(temporary_name)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                # Recheck both control files immediately before replacing any
                # of them.  This catches an external edit made during staging.
                if index_path.read_text(encoding="utf-8") != original_index or log_path.read_text(encoding="utf-8") != original_log:
                    raise NoteVaultError("vault_changed", "Index or operation log changed; retry the commit", 409)
                os.replace(str(temporary_note), str(target_path))
                temporary_note = None
                created_note = True
                _atomic_replace_text(index_path, next_index)
                replaced_index = True
                _atomic_replace_text(log_path, next_log)
                replaced_log = True
            except NoteVaultError:
                if temporary_note is not None:
                    try:
                        temporary_note.unlink()
                    except OSError:
                        pass
                if created_note:
                    try:
                        target_path.unlink()
                    except OSError:
                        pass
                if replaced_index:
                    _atomic_replace_text(index_path, original_index)
                if replaced_log:
                    _atomic_replace_text(log_path, original_log)
                if target_directory_created:
                    try:
                        target_directory.rmdir()
                    except OSError:
                        pass
                raise
            except Exception as exc:
                if temporary_note is not None:
                    try:
                        temporary_note.unlink()
                    except OSError:
                        pass
                if created_note:
                    try:
                        target_path.unlink()
                    except OSError:
                        pass
                if replaced_index:
                    _atomic_replace_text(index_path, original_index)
                if replaced_log:
                    _atomic_replace_text(log_path, original_log)
                if target_directory_created:
                    try:
                        target_directory.rmdir()
                    except OSError:
                        pass
                raise NoteVaultError("vault_write_error", "Could not persist note transaction", 500) from exc

            return {
                "status": "committed",
                "idempotent": False,
                "run_id": normalized_run_id,
                "note": {
                    "name": allocated_name,
                    "title": note_title,
                    "path": target_path.relative_to(self.root).as_posix(),
                },
                "transaction": {
                    "index": INDEX_NAME,
                    "log": LOG_NAME,
                    "log_rows_added": 1,
                    "created_files": [target_path.relative_to(self.root).as_posix()],
                    "updated_files": [INDEX_NAME, LOG_NAME],
                },
            }


def _atomic_replace_text(path: Path, value: str) -> None:
    temporary = path.with_name(".%s.%d.%d.tmp" % (path.name, os.getpid(), threading.get_ident()))
    try:
        temporary.write_text(value, encoding="utf-8")
        os.replace(str(temporary), str(path))
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


__all__ = [
    "NoteVaultCommitter",
    "NotePromotionCommitter",
    "NoteVaultError",
    "INBOX_DIRECTORY",
    "INDEX_NAME",
    "LOG_NAME",
]
