#!/usr/bin/env python3
"""Serve the note knowledge-base UI plus the legacy Career Copilot API."""

from __future__ import annotations

import argparse
import base64
import binascii
import csv
import hashlib
import io
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import unicodedata
import zipfile
import zlib
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple, Type
from urllib.parse import parse_qs, unquote, urlparse
from xml.etree import ElementTree
from uuid import uuid4

from career_copilot import Orchestrator
from note_knowledge import NoteOrchestrator, OCRAgent, OCRRevisionConflict, NotePromotionCommitter, NoteVaultCommitter, NoteVaultError, SourceLifecycleManager
from note_knowledge.transaction import VAULT_TRANSACTION_LOCK
from note_knowledge.providers import MacOSVisionOCRProvider, WenxinMultimodalProvider


ROOT = Path(__file__).resolve().parent
MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_NOTE_BODY_BYTES = 12 * 1024 * 1024
MAX_INGEST_BODY_BYTES = 30 * 1024 * 1024
MAX_INGEST_FILE_BYTES = 20 * 1024 * 1024
MAX_EXTRACTED_TEXT_CHARS = 20 * 1024 * 1024
MAX_DOCX_XML_BYTES = 12 * 1024 * 1024
MAX_KNOWLEDGE_POINTS = 120
MAX_POINT_CHARS = 600
MAX_KNOWLEDGE_DRAFTS = 256
KNOWLEDGE_DRAFT_STORAGE_DIRECTORY = "noteflow-knowledge-drafts"
MAX_VAULT_SEARCH_QUERY_CHARS = 200
MAX_VAULT_SEARCH_RESULTS = 200
MAX_VAULT_NOTE_CONTENT_CHARS = 100_000
VAULT_SEARCH_SNIPPET_CHARS = 240
# A custom-container proxy can forward a request body without preserving
# Content-Length or Transfer-Encoding.  In that case the body is delimited by
# connection close (or by a short idle period); keep the idle wait bounded so
# a malformed client cannot hold a worker forever.
UNKNOWN_LENGTH_BODY_IDLE_TIMEOUT = 1.0
REQUEST_BODY_READ_CHUNK_BYTES = 64 * 1024
CHUNK_LINE_MAX_BYTES = 8 * 1024
SEARCH_EXCLUDED_BASENAMES = {
    "readme.md",
    "00-知识库说明.md",
    "01-知识库目录.md",
    "02-更新流水账.md",
}
SEARCH_EXCLUDED_BASENAMES_CASEFOLD = {name.casefold() for name in SEARCH_EXCLUDED_BASENAMES}
SUPPORTED_INGEST_EXTENSIONS = {".md", ".markdown", ".txt", ".docx", ".pdf"}
RETRY_PATH = re.compile(r"^/api/runs/([^/]+)/stages/([^/]+)/retry/?$")
NOTE_RUN_PATH = re.compile(r"^/api/notes/runs/([^/]+)/?$")
NOTE_RETRY_PATH = re.compile(r"^/api/notes/runs/([^/]+)/stages/([^/]+)/retry/?$")
NOTE_OCR_CORRECT_PATH = re.compile(r"^/api/notes/runs/([^/]+)/ocr/correct/?$")
WIKILINK_PATTERN = re.compile(r"\[\[([^\[\]\n]+)\]\]")
LOCAL_MARKDOWN_LINK_PATTERN = re.compile(r"(?<!!)\[([^\]]+)\]\((?!https?://|mailto:)[^)]+\)", re.IGNORECASE)
# Keep the historical name for callers/tests while coordinating with all
# NoteVaultCommitter and promotion transactions.
KNOWLEDGE_INGEST_LOCK = VAULT_TRANSACTION_LOCK
# Drafts are deliberately process-local: a preview must not create files in the
# vault, while a commit is still able to carry the extracted source bytes.
KNOWLEDGE_DRAFTS: Dict[str, Dict[str, Any]] = {}
SYSTEM_DIRECTORIES = {
    "99-模板",
    "__pycache__",
    "build",
    "career_copilot",
    "coverage",
    "dist",
    "htmlcov",
    "node_modules",
    "note_knowledge",
    "src",
    "tests",
    "vendor",
    "venv",
}


class KnowledgeGraphError(Exception):
    """Raised when vault invariants make a basename graph ambiguous."""


class VaultSearchError(ValueError):
    """Raised when a vault-search query cannot be served safely."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class KnowledgeIngestError(Exception):
    """A safe, user-facing document-ingest failure."""

    def __init__(self, status: HTTPStatus, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


def _is_system_directory(name: str) -> bool:
    return name.startswith(".") or name.casefold() in {item.casefold() for item in SYSTEM_DIRECTORIES}


def _is_raw_source_path(parts: Tuple[str, ...]) -> bool:
    lowered = tuple(part.casefold() for part in parts)
    prefix = ("07-素材附件".casefold(), "原始资料".casefold())
    return lowered[: len(prefix)] == prefix


def _iter_vault_markdown(root: Path) -> Iterable[Path]:
    """Yield vault Markdown files in a stable order without entering app folders."""

    for current, directories, filenames in os.walk(root):
        current_path = Path(current)
        relative_parts = current_path.relative_to(root).parts
        if _is_raw_source_path(relative_parts):
            directories[:] = []
            continue
        directories[:] = sorted(
            (
                name
                for name in directories
                if not _is_system_directory(name) and not _is_raw_source_path(relative_parts + (name,))
            ),
            key=lambda value: (value.casefold(), value),
        )
        for filename in sorted(filenames, key=lambda value: (value.casefold(), value)):
            if filename.startswith(".") or not filename.casefold().endswith(".md") or filename.casefold() == "readme.md":
                continue
            path = current_path / filename
            if not path.is_symlink():
                yield path


def _excluded_wikilink_targets(root: Path) -> Set[str]:
    """Collect intentionally hidden note names so they are not reported as broken."""

    excluded: Set[str] = set()
    template_name = "99-模板".casefold()
    for current, directories, filenames in os.walk(root):
        current_path = Path(current)
        relative_parts = current_path.relative_to(root).parts
        if _is_raw_source_path(relative_parts):
            excluded.update(Path(filename).stem.casefold() for filename in filenames if filename.casefold().endswith(".md"))
            directories[:] = []
            continue
        inside_templates = any(part.casefold() == template_name for part in relative_parts)
        if inside_templates:
            directories[:] = sorted(
                (name for name in directories if not name.startswith(".")),
                key=lambda value: (value.casefold(), value),
            )
            excluded.update(Path(filename).stem.casefold() for filename in filenames if filename.casefold().endswith(".md"))
            continue
        directories[:] = sorted(
            (
                name
                for name in directories
                if (name.casefold() == template_name or not _is_system_directory(name))
                and not _is_raw_source_path(relative_parts + (name,))
            ),
            key=lambda value: (value.casefold(), value),
        )
        excluded.update(Path(filename).stem.casefold() for filename in filenames if filename.casefold() == "readme.md")
    return excluded


def _parse_frontmatter_scalar(raw: str) -> Any:
    value = raw.strip()
    if not value:
        return ""
    if len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value[1:-1]
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1]
    lowered = value.casefold()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if lowered in {"null", "none", "~"}:
        return None
    if value.startswith("[") and value.endswith("]"):
        content = value[1:-1].strip()
        if not content:
            return []
        try:
            parsed = next(csv.reader([content], skipinitialspace=True))
        except (csv.Error, StopIteration):
            parsed = content.split(",")
        return [_parse_frontmatter_scalar(item) for item in parsed]
    if re.fullmatch(r"[-+]?\d+", value):
        try:
            return int(value)
        except ValueError:
            pass
    if re.fullmatch(r"[-+]?(?:\d+\.\d*|\d*\.\d+)", value):
        try:
            return float(value)
        except ValueError:
            pass
    return value


def _parse_frontmatter(text: str) -> Tuple[Dict[str, Any], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    closing = next((index for index in range(1, len(lines)) if lines[index].strip() == "---"), None)
    if closing is None:
        return {}, text

    metadata: Dict[str, Any] = {}
    active_key: Optional[str] = None
    block_style: Optional[str] = None
    block_lines: List[str] = []

    def finish_block() -> None:
        nonlocal block_style, block_lines
        if active_key is not None and block_style is not None:
            separator = "\n" if block_style == "|" else " "
            metadata[active_key] = separator.join(block_lines).strip()
        block_style = None
        block_lines = []

    for raw_line in lines[1:closing]:
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            if block_style is not None and stripped == "":
                block_lines.append("")
            continue
        if raw_line[:1].isspace() and active_key is not None:
            if block_style is not None:
                block_lines.append(stripped)
            elif stripped.startswith("-"):
                current = metadata.get(active_key)
                if not isinstance(current, list):
                    current = []
                    metadata[active_key] = current
                current.append(_parse_frontmatter_scalar(stripped[1:].strip()))
            continue
        finish_block()
        if ":" not in raw_line:
            active_key = None
            continue
        key, raw_value = raw_line.split(":", 1)
        key = key.strip()
        if not key:
            active_key = None
            continue
        active_key = key
        raw_value = raw_value.strip()
        if raw_value in {"|", ">"}:
            block_style = raw_value
            block_lines = []
        else:
            metadata[key] = _parse_frontmatter_scalar(raw_value)
    finish_block()
    return metadata, "\n".join(lines[closing + 1 :])


def _without_markdown_code(text: str) -> str:
    text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    text = re.sub(r"~~~.*?~~~", "", text, flags=re.DOTALL)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    return re.sub(r"`[^`\n]*`", "", text)


def _display_text(text: str) -> str:
    def wikilink_label(match: re.Match[str]) -> str:
        inner = match.group(1)
        if "|" in inner:
            return inner.rsplit("|", 1)[1].strip()
        return inner.split("#", 1)[0].strip()

    value = WIKILINK_PATTERN.sub(wikilink_label, text)
    value = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", value)
    value = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", value)
    value = re.sub(r"[*_~]", "", value)
    value = re.sub(r"^\s*(?:[-+*]|\d+[.)])\s+", "", value)
    return re.sub(r"\s+", " ", value).strip()


def _note_title(body: str, fallback: str) -> str:
    for line in body.splitlines():
        match = re.match(r"^#\s+(.+?)\s*$", line)
        if match:
            return _display_text(match.group(1)) or fallback
    return fallback


def _note_summary(body: str, metadata: Dict[str, Any], title: str, limit: int = 180) -> str:
    supplied = metadata.get("summary", metadata.get("description", metadata.get("excerpt", "")))
    if isinstance(supplied, str) and supplied.strip():
        summary = _display_text(supplied)
    else:
        cleaned = _without_markdown_code(body)
        paragraph: List[str] = []
        for raw_line in cleaned.splitlines():
            line = raw_line.strip()
            if not line:
                if paragraph:
                    break
                continue
            if line.startswith("#") or line.startswith("|") or re.fullmatch(r"[-:| ]+", line):
                continue
            if line.startswith("![[") or line.startswith("!["):
                continue
            if line.startswith(">"):
                line = line.lstrip("> ")
            display = _display_text(line)
            if display:
                paragraph.append(display)
        summary = " ".join(paragraph) or title
    if len(summary) <= limit:
        return summary
    return summary[: limit - 3].rstrip() + "..."


def _wikilink_targets(body: str) -> Iterable[str]:
    for match in WIKILINK_PATTERN.finditer(_without_markdown_code(body)):
        raw_target = match.group(1).split("|", 1)[0].split("#", 1)[0].strip()
        if not raw_target or "/" in raw_target or "\\" in raw_target:
            continue
        suffix = Path(raw_target).suffix.casefold()
        if suffix and suffix != ".md":
            continue
        target = Path(raw_target).stem if suffix == ".md" else raw_target
        if target:
            yield target


def _modified_value(path: Path, metadata: Dict[str, Any]) -> str:
    for key in ("updated", "modified", "created"):
        value = metadata.get(key)
        if value not in (None, ""):
            return str(value)
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _note_tags(metadata: Dict[str, Any]) -> List[str]:
    raw_tags = metadata.get("tags", [])
    if isinstance(raw_tags, str):
        raw_tags = [raw_tags]
    elif not isinstance(raw_tags, list):
        return []
    return [str(tag).strip() for tag in raw_tags if str(tag).strip()]


def _metadata_text(value: Any) -> str:
    """Flatten parsed frontmatter into deterministic searchable text."""

    if isinstance(value, dict):
        parts: List[str] = []
        for key in sorted(value, key=lambda item: (str(item).casefold(), str(item))):
            parts.extend((str(key), _metadata_text(value[key])))
        return " ".join(part for part in parts if part)
    if isinstance(value, list):
        return " ".join(_metadata_text(item) for item in value)
    return "" if value is None else str(value)


def _normalised_search_text(value: Any) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value or "")).casefold()).strip()


def _is_inbox_placeholder(path: Path, metadata: Dict[str, Any]) -> bool:
    marker = metadata.get("placeholder", False)
    explicit_marker = marker is True or (isinstance(marker, str) and marker.casefold() in {"1", "true", "yes"})
    return explicit_marker or path.stem.casefold() == "待整理内容".casefold()


def _vault_note_record(root: Path, path: Path, include_content: bool = False) -> Tuple[Dict[str, Any], str]:
    """Read one managed Markdown note into the shared client schema."""

    text = path.read_text(encoding="utf-8")
    metadata, body = _parse_frontmatter(text)
    supplied_title = metadata.get("title", "")
    if isinstance(supplied_title, str) and supplied_title.strip():
        title = _display_text(supplied_title) or path.stem
    else:
        title = _note_title(body, path.stem)
    relative_path = path.relative_to(root).as_posix()
    modified = _modified_value(path, metadata)
    tags = _note_tags(metadata)
    note_type = str(metadata.get("type", "note") or "note")
    status = str(metadata.get("status", "") or "")
    placeholder = _is_inbox_placeholder(path, metadata)
    record: Dict[str, Any] = {
        "id": path.stem,
        "note_id": path.stem,
        "basename": path.stem,
        "filename": path.name,
        "title": title,
        "path": relative_path,
        "relative_path": relative_path,
        "directory": path.parent.relative_to(root).as_posix(),
        "category": relative_path.split("/", 1)[0] if "/" in relative_path else "根目录",
        "summary": _note_summary(body, metadata, title),
        "type": note_type,
        "status": status,
        "created": str(metadata.get("created", "") or ""),
        "updated": str(metadata.get("updated", "") or modified),
        "modified": modified,
        "tags": tags,
        "metadata": metadata,
        "wikilink": "[[%s]]" % path.stem,
        "placeholder": placeholder,
        "is_placeholder": placeholder,
    }
    if include_content:
        truncated = len(body) > MAX_VAULT_NOTE_CONTENT_CHARS
        content = body[:MAX_VAULT_NOTE_CONTENT_CHARS]
        record.update(
            {
                "body": content,
                "content": content,
                "preview": content,
                "content_truncated": truncated,
            }
        )
    return record, body


def list_vault_inbox(directory: Path, include_placeholder: bool = False) -> Dict[str, Any]:
    """List pending notes below ``00-收件箱`` without mutating the vault."""

    root = directory.resolve()
    items: List[Dict[str, Any]] = []
    for path in _iter_vault_markdown(root):
        relative_parts = path.relative_to(root).parts
        if not relative_parts or relative_parts[0].casefold() != "00-收件箱".casefold():
            continue
        item, _ = _vault_note_record(root, path, include_content=True)
        if item["placeholder"] and not include_placeholder:
            continue
        if not item["status"]:
            item["status"] = "待整理"
        if item["type"] == "note":
            item["type"] = "inbox"
        items.append(item)
    items.sort(key=lambda item: (item["path"].casefold(), item["path"]))
    return {
        "items": items,
        "notes": items,
        "count": len(items),
        "directory": "00-收件箱",
        "include_placeholder": include_placeholder,
    }


def _search_snippet(source: str, query: str, limit: int = VAULT_SEARCH_SNIPPET_CHARS) -> str:
    clean = _display_text(_without_markdown_code(source))
    if len(clean) <= limit:
        return clean
    normalised = _normalised_search_text(clean)
    query_normalised = _normalised_search_text(query)
    needles = [query_normalised] if query_normalised else []
    needles.extend(token for token in query_normalised.split() if token and token not in needles)
    match_at = next((normalised.find(needle) for needle in needles if normalised.find(needle) >= 0), 0)
    start = max(0, match_at - limit // 3)
    end = min(len(clean), start + limit)
    if end == len(clean):
        start = max(0, end - limit)
    snippet = clean[start:end].strip()
    return ("..." if start else "") + snippet + ("..." if end < len(clean) else "")


def search_vault(directory: Path, query: str = "", limit: int = MAX_VAULT_SEARCH_RESULTS) -> Dict[str, Any]:
    """Search managed vault notes using deterministic, dependency-free ranking."""

    if not isinstance(query, str):
        raise VaultSearchError("invalid_query", "q must be a string")
    query_text = query.strip()
    if len(query_text) > MAX_VAULT_SEARCH_QUERY_CHARS:
        raise VaultSearchError(
            "query_too_long",
            "q must be at most %d characters" % MAX_VAULT_SEARCH_QUERY_CHARS,
        )
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise VaultSearchError("invalid_limit", "limit must be a positive integer")
    limit = min(limit, MAX_VAULT_SEARCH_RESULTS)

    root = directory.resolve()
    query_normalised = _normalised_search_text(query_text)
    query_tokens = [token for token in query_normalised.split() if token]
    matches: List[Dict[str, Any]] = []
    field_weights = {
        "title": 40,
        "summary": 24,
        "body": 12,
        "basename": 32,
        "path": 8,
        "tags": 20,
        "metadata": 6,
    }

    for path in _iter_vault_markdown(root):
        record, body = _vault_note_record(root, path)
        if path.name.casefold() in SEARCH_EXCLUDED_BASENAMES_CASEFOLD:
            continue
        if record["placeholder"]:
            continue
        if str(record.get("type", "") or "").casefold() in {"system", "index", "log", "template", "directory"}:
            continue
        field_values = {
            "title": record["title"],
            "summary": record["summary"],
            "body": body,
            "basename": record["basename"],
            "path": record["path"],
            "tags": " ".join(record["tags"]),
            "metadata": _metadata_text(record["metadata"]),
        }
        normalised_fields = {name: _normalised_search_text(value) for name, value in field_values.items()}
        if query_normalised:
            phrase_match = any(query_normalised in value for value in normalised_fields.values())
            token_match = bool(query_tokens) and all(
                any(token in value for value in normalised_fields.values()) for token in query_tokens
            )
            if not phrase_match and not token_match:
                continue
            matched_fields = [
                name
                for name, value in normalised_fields.items()
                if query_normalised in value or any(token in value for token in query_tokens)
            ]
            score = sum(field_weights[name] for name in matched_fields)
            if query_normalised == normalised_fields["title"]:
                score += 100
            elif query_normalised in normalised_fields["title"]:
                score += 50
        else:
            matched_fields = []
            score = 0

        if "body" in matched_fields:
            snippet_source = body
        elif "summary" in matched_fields:
            snippet_source = str(record["summary"])
        elif "title" in matched_fields:
            snippet_source = str(record["title"])
        else:
            snippet_source = str(record["summary"] or record["title"])
        result = dict(record)
        result.update(
            {
                "snippet": _search_snippet(snippet_source, query_text),
                "matched_fields": matched_fields,
                "matches": matched_fields,
                "score": score,
            }
        )
        matches.append(result)

    matches.sort(
        key=lambda item: (
            -item["score"] if query_normalised else 0,
            item["path"].casefold(),
            item["path"],
        )
    )
    total_count = len(matches)
    results = matches[:limit]
    return {
        "query": query_text,
        "results": results,
        "items": results,
        "notes": results,
        "count": len(results),
        "total_count": total_count,
        "limit": limit,
        "truncated": total_count > len(results),
    }


def _directory_tree(nodes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    root: Dict[str, Any] = {"notes": [], "children": {}}
    for node in nodes:
        parent_parts = Path(node["path"]).parent.parts
        branch = root
        traversed: List[str] = []
        for part in parent_parts:
            if part == ".":
                continue
            traversed.append(part)
            branch = branch["children"].setdefault(
                part,
                {"name": part, "path": "/".join(traversed), "notes": [], "children": {}},
            )
        branch["notes"].append({"id": node["id"], "title": node["title"], "path": node["path"]})

    def serialize(branch: Dict[str, Any]) -> Dict[str, Any]:
        children = [
            serialize(child)
            for _, child in sorted(branch["children"].items(), key=lambda item: (item[0].casefold(), item[0]))
        ]
        notes = sorted(branch["notes"], key=lambda note: (note["path"].casefold(), note["path"]))
        return {
            "name": branch["name"],
            "path": branch["path"],
            "count": len(notes) + sum(child["count"] for child in children),
            "notes": notes,
            "children": children,
        }

    tree: List[Dict[str, Any]] = []
    if root["notes"]:
        root_notes = sorted(root["notes"], key=lambda note: (note["path"].casefold(), note["path"]))
        tree.append({"name": "根目录", "path": "", "count": len(root_notes), "notes": root_notes, "children": []})
    tree.extend(
        serialize(child)
        for _, child in sorted(root["children"].items(), key=lambda item: (item[0].casefold(), item[0]))
    )
    return tree


def build_knowledge_graph(directory: Path) -> Dict[str, Any]:
    """Build a deterministic Obsidian-style graph from a vault directory."""

    root = directory.resolve()
    records: List[Tuple[Dict[str, Any], str]] = []
    names: Dict[str, str] = {}
    for path in _iter_vault_markdown(root):
        relative_path = path.relative_to(root).as_posix()
        note_id = path.stem
        normalized_name = note_id.casefold()
        if normalized_name in names:
            raise KnowledgeGraphError(
                "Duplicate note filename '%s.md': %s and %s" % (note_id, names[normalized_name], relative_path)
            )
        names[normalized_name] = relative_path
        text = path.read_text(encoding="utf-8")
        metadata, body = _parse_frontmatter(text)
        category = relative_path.split("/", 1)[0] if "/" in relative_path else "根目录"
        title = _note_title(body, note_id)
        tags = metadata.get("tags", [])
        if isinstance(tags, str):
            tags = [tags]
        elif not isinstance(tags, list):
            tags = []
        node = {
            "id": note_id,
            "title": title,
            "path": relative_path,
            "category": category,
            "summary": _note_summary(body, metadata, title),
            "modified": _modified_value(path, metadata),
            "type": str(metadata.get("type", "note") or "note"),
            "status": str(metadata.get("status", "") or ""),
            "tags": [str(tag) for tag in tags],
            "metadata": metadata,
        }
        records.append((node, body))

    records.sort(key=lambda record: (record[0]["path"].casefold(), record[0]["path"]))
    nodes = [record[0] for record in records]
    id_by_normalized_name = {node["id"].casefold(): node["id"] for node in nodes}
    excluded_targets = _excluded_wikilink_targets(root)
    edge_pairs: Set[Tuple[str, str]] = set()
    unresolved_pairs: Set[Tuple[str, str]] = set()
    for node, body in records:
        for requested_target in _wikilink_targets(body):
            target = id_by_normalized_name.get(requested_target.casefold())
            if target is None:
                if requested_target.casefold() not in excluded_targets:
                    unresolved_pairs.add((node["id"], requested_target))
            else:
                edge_pairs.add((node["id"], target))

    edges = [{"source": source, "target": target} for source, target in sorted(edge_pairs, key=lambda pair: (pair[0].casefold(), pair[0], pair[1].casefold(), pair[1]))]
    unresolved_links = [
        {"source": source, "target": target}
        for source, target in sorted(unresolved_pairs, key=lambda pair: (pair[0].casefold(), pair[0], pair[1].casefold(), pair[1]))
    ]
    connected_ids = {endpoint for pair in edge_pairs for endpoint in pair}
    category_counts: Dict[str, int] = {}
    for node in nodes:
        category_counts[node["category"]] = category_counts.get(node["category"], 0) + 1
    categories = [
        {"name": name, "path": "" if name == "根目录" else name, "count": count}
        for name, count in sorted(category_counts.items(), key=lambda item: (item[0] != "根目录", item[0].casefold(), item[0]))
    ]

    return {
        "nodes": nodes,
        "edges": edges,
        "tree": _directory_tree(nodes),
        "categories": categories,
        "unresolved_links": unresolved_links,
        **_knowledge_graph_projection(nodes, edges),
        "stats": {
            "notes": len(nodes),
            "links": len(edges),
            "categories": len(categories),
            "orphans": len(nodes) - len(connected_ids),
            "unresolved": len(unresolved_links),
        },
    }


def _knowledge_graph_projection(nodes: List[Dict[str, Any]], edges: List[Dict[str, str]]) -> Dict[str, Any]:
    """Expose imported knowledge separately while preserving the original graph schema."""

    def source_status_key(value: Any) -> str:
        normalized = unicodedata.normalize("NFKC", str(value or "")).strip().casefold()
        if normalized in {
            "archived",
            "archive",
            "hidden",
            "inactive",
            "removed",
            "deleted",
            "已归档",
            "归档",
            "已移除",
        }:
            return "archived"
        if normalized in {"pending", "待处理", "待补充", "待提取"}:
            return "pending"
        if normalized in {"active", "正常", "已收录", "待核验", "完成", "启用"}:
            return "active"
        return normalized or "active"

    def source_reference_key_early(value: Any) -> str:
        raw = str(value or "").strip()
        if raw.startswith("[[") and raw.endswith("]]" ):
            raw = raw[2:-2]
        raw = raw.split("|", 1)[0].split("#", 1)[0].strip().replace(chr(92), "/")
        if raw.casefold().endswith(".md"):
            raw = raw[:-3]
        return Path(raw).name.casefold()

    summary_nodes = [node for node in nodes if node["type"] == "source-summary"]
    archived_summary_keys: Set[str] = set()
    for summary_node in summary_nodes:
        summary_metadata = summary_node.get("metadata", {})
        if not isinstance(summary_metadata, dict):
            continue
        summary_status = source_status_key(
            summary_metadata.get("source_status", summary_metadata.get("status", ""))
        )
        summary_graph_status = str(summary_metadata.get("graph_status", "") or "").strip().casefold()
        if summary_status == "archived" or summary_graph_status in {"hidden", "archived", "inactive"}:
            for reference in (
                summary_node.get("id", ""),
                summary_metadata.get("source_summary", ""),
                summary_metadata.get("source_file", ""),
                summary_metadata.get("source_path", ""),
                summary_metadata.get("source_document", ""),
            ):
                key = source_reference_key_early(reference)
                if key:
                    archived_summary_keys.add(key)

    def point_source_archived(node: Dict[str, Any]) -> bool:
        metadata = node.get("metadata", {})
        if not isinstance(metadata, dict):
            return False
        # source_status is separate from a point's own evidence/status field:
        # archiving a source hides its points from the default graph while
        # retaining the notes for restoration.
        if source_status_key(metadata.get("source_status", "")) == "archived":
            return True
        return any(
            source_reference_key_early(metadata.get(field, "")) in archived_summary_keys
            for field in ("source_summary", "source_summary_id", "source_id", "source_document", "source_file", "source_path")
            if metadata.get(field, "")
        )

    # A saved note can intentionally stay out of the graph.  Older notes do
    # not have graph_status, so missing metadata remains active for backwards
    # compatibility.
    knowledge_nodes = [
        node
        for node in nodes
        if node["type"] == "knowledge-point"
        and not point_source_archived(node)
        and str(node["metadata"].get("graph_status", "active") or "active").casefold()
        not in {"excluded", "draft", "inactive"}
    ]
    knowledge_ids = {node["id"] for node in knowledge_nodes}
    knowledge_edges: List[Dict[str, str]] = []
    seen_relations: Set[Tuple[str, str, str]] = set()
    for edge in edges:
        source = edge["source"]
        target = edge["target"]
        relation = "related"
        if source in knowledge_ids and target in knowledge_ids and (source, target, relation) not in seen_relations:
            seen_relations.add((source, target, relation))
            knowledge_edges.append({"source": source, "target": target, "relation": relation})
    knowledge_edges.sort(
        key=lambda edge: (
            edge["source"].casefold(),
            edge["source"],
            edge["target"].casefold(),
            edge["target"],
            edge["relation"],
        )
    )

    sources: List[Dict[str, Any]] = []
    for summary_node in summary_nodes:
        metadata = summary_node["metadata"]
        source_status = source_status_key(
            metadata.get("source_status", metadata.get("status", ""))
        )
        graph_status = str(metadata.get("graph_status", "") or "").strip().casefold()
        archived = source_status == "archived" or graph_status in {"hidden", "archived", "inactive"}
        if archived:
            source_status = "archived"
        all_point_ids = sorted(
            (
                node["id"]
                for node in nodes
                if node["type"] == "knowledge-point"
                and source_reference_key_early(node["metadata"].get("source_summary", ""))
                == source_reference_key_early(summary_node["id"])
            ),
            key=lambda value: (value.casefold(), value),
        )
        point_ids = sorted(
            (
                node["id"]
                for node in knowledge_nodes
                if source_reference_key_early(node["metadata"].get("source_summary", ""))
                == source_reference_key_early(summary_node["id"])
            ),
            key=lambda value: (value.casefold(), value),
        )
        sources.append(
            {
                "id": summary_node["id"],
                "title": str(metadata.get("source_document", "")) or summary_node["title"],
                "status": source_status,
                "source_status": source_status,
                "graph_status": "hidden" if archived else (graph_status or "active"),
                "archived": archived,
                "status_label": "已归档" if archived else ("待处理" if source_status == "pending" else "正常"),
                "path": summary_node["path"],
                "summary_id": summary_node["id"],
                "summary_path": summary_node["path"],
                "filename": str(metadata.get("source_document", "")),
                "format": str(metadata.get("source_format", "")),
                "source_file": str(metadata.get("source_file", "")),
                "source_path": str(metadata.get("source_path", "")),
                "extraction_method": str(metadata.get("extraction_method", "")),
                "evidence_status": str(metadata.get("evidence_status", "")),
                "confidentiality": str(metadata.get("confidentiality", "")),
                "summary": summary_node["summary"],
                "modified": summary_node["modified"],
                "point_ids": point_ids,
                "count": len(point_ids),
                "all_point_ids": all_point_ids,
                "total_count": len(all_point_ids),
            }
        )
    sources.sort(key=lambda source: (source["path"].casefold(), source["path"]))
    return {
        "knowledge_nodes": knowledge_nodes,
        "knowledge_edges": knowledge_edges,
        "sources": sources,
        "knowledge_stats": {
            "points": len(knowledge_nodes),
            "sources": len(sources),
            "relations": len(knowledge_edges),
        },
    }


def _validated_upload_filename(value: Any) -> Tuple[str, str]:
    if not isinstance(value, str) or not value.strip():
        raise KnowledgeIngestError(HTTPStatus.BAD_REQUEST, "invalid_filename", "filename is required")
    filename = unicodedata.normalize("NFC", value.strip())
    if (
        filename in {".", ".."}
        or filename != Path(filename).name
        or "/" in filename
        or "\\" in filename
        or re.search(r"[\x00-\x1f\x7f\[\]]", filename)
        or len(filename.encode("utf-8")) > 240
    ):
        raise KnowledgeIngestError(
            HTTPStatus.BAD_REQUEST,
            "invalid_filename",
            "filename must be a safe local basename without a directory path",
        )
    extension = Path(filename).suffix.casefold()
    if extension not in SUPPORTED_INGEST_EXTENSIONS:
        raise KnowledgeIngestError(
            HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
            "unsupported_file_type",
            "Supported document types are .md, .markdown, .txt, .docx, and .pdf",
        )
    return filename, extension


def _decoded_base64(value: Any) -> bytes:
    if not isinstance(value, str) or not value.strip():
        raise KnowledgeIngestError(HTTPStatus.BAD_REQUEST, "invalid_base64", "base64 must be a non-empty string")
    encoded = value.strip()
    if encoded.startswith("data:"):
        header, separator, encoded = encoded.partition(",")
        if not separator or ";base64" not in header.casefold():
            raise KnowledgeIngestError(HTTPStatus.BAD_REQUEST, "invalid_base64", "Data URL must contain base64 data")
    encoded = re.sub(r"\s+", "", encoded)
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise KnowledgeIngestError(HTTPStatus.BAD_REQUEST, "invalid_base64", "base64 is not valid") from exc
    if len(decoded) > MAX_INGEST_FILE_BYTES:
        raise KnowledgeIngestError(
            HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            "file_too_large",
            "Decoded document is larger than %d MB" % (MAX_INGEST_FILE_BYTES // (1024 * 1024)),
        )
    return decoded


def _decoded_text_file(data: bytes) -> str:
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise KnowledgeIngestError(
        HTTPStatus.UNPROCESSABLE_ENTITY,
        "text_decode_failed",
        "Text document must use UTF-8 or GB18030 encoding",
    )


def _extract_docx_text(data: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            try:
                info = archive.getinfo("word/document.xml")
            except KeyError as exc:
                raise KnowledgeIngestError(
                    HTTPStatus.UNPROCESSABLE_ENTITY,
                    "invalid_docx",
                    "DOCX does not contain word/document.xml",
                ) from exc
            if info.file_size > MAX_DOCX_XML_BYTES or info.compress_size > MAX_INGEST_FILE_BYTES:
                raise KnowledgeIngestError(
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    "docx_too_large",
                    "DOCX document XML is too large to process safely",
                )
            xml_data = archive.read(info)
    except KnowledgeIngestError:
        raise
    except (zipfile.BadZipFile, RuntimeError, OSError) as exc:
        raise KnowledgeIngestError(HTTPStatus.UNPROCESSABLE_ENTITY, "invalid_docx", "DOCX archive is invalid") from exc
    try:
        document = ElementTree.fromstring(xml_data)
    except ElementTree.ParseError as exc:
        raise KnowledgeIngestError(HTTPStatus.UNPROCESSABLE_ENTITY, "invalid_docx", "DOCX XML is invalid") from exc

    paragraphs: List[str] = []
    for paragraph in document.iter():
        if not paragraph.tag.endswith("}p"):
            continue
        pieces: List[str] = []
        for element in paragraph.iter():
            if element.tag.endswith("}t") and element.text:
                pieces.append(element.text)
            elif element.tag.endswith("}tab"):
                pieces.append("\t")
            elif element.tag.endswith(("}br", "}cr")):
                pieces.append("\n")
        value = "".join(pieces).strip()
        if value:
            paragraphs.append(value)
    return "\n\n".join(paragraphs)


def _extract_pdf_with_python(data: bytes) -> str:
    for module_name in ("pypdf", "PyPDF2"):
        try:
            module = __import__(module_name, fromlist=["PdfReader"])
        except ImportError:
            continue
        try:
            reader = module.PdfReader(io.BytesIO(data))
            if getattr(reader, "is_encrypted", False):
                try:
                    reader.decrypt("")
                except Exception:
                    return ""
            if len(reader.pages) > 300:
                raise KnowledgeIngestError(
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    "pdf_too_large",
                    "PDF has more than 300 pages",
                )
            text = "\n\n".join((page.extract_text() or "") for page in reader.pages)
            if text.strip():
                return text
        except KnowledgeIngestError:
            raise
        except Exception:
            continue
    return ""


def _extract_pdf_with_pdftotext(data: bytes) -> str:
    executable = shutil.which("pdftotext")
    if not executable:
        return ""
    temporary_path = ""
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as temporary:
            temporary.write(data)
            temporary_path = temporary.name
        completed = subprocess.run(
            [executable, "-layout", temporary_path, "-"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )
        if completed.returncode == 0 and completed.stdout:
            return _decoded_text_file(completed.stdout)
    except (OSError, subprocess.SubprocessError, KnowledgeIngestError):
        return ""
    finally:
        if temporary_path:
            try:
                Path(temporary_path).unlink()
            except OSError:
                pass
    return ""


def _pdf_literal(value: bytes) -> str:
    value = value[1:-1]

    def escaped(match: re.Match[bytes]) -> bytes:
        token = match.group(1)
        translations = {b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b", b"f": b"\f"}
        if token in translations:
            return translations[token]
        if re.fullmatch(rb"[0-7]{1,3}", token):
            return bytes([int(token, 8) & 0xFF])
        if token in {b"\n", b"\r"}:
            return b""
        return token

    decoded = re.sub(rb"\\([0-7]{1,3}|.)", escaped, value)
    if decoded.startswith((b"\xfe\xff", b"\xff\xfe")):
        try:
            return decoded.decode("utf-16")
        except UnicodeDecodeError:
            pass
    for encoding in ("utf-8", "cp1252"):
        try:
            return decoded.decode(encoding)
        except UnicodeDecodeError:
            continue
    return ""


def _extract_pdf_heuristic(data: bytes) -> str:
    payloads = [data]
    expanded_bytes = 0
    for match in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", data, flags=re.DOTALL):
        stream = match.group(1)
        payloads.append(stream)
        try:
            remaining = MAX_EXTRACTED_TEXT_CHARS - expanded_bytes
            decompressor = zlib.decompressobj()
            expanded = decompressor.decompress(stream, remaining + 1)
            if len(expanded) > remaining or decompressor.unconsumed_tail:
                raise KnowledgeIngestError(
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    "pdf_stream_too_large",
                    "A compressed PDF stream exceeds the safe extraction limit",
                )
            tail = decompressor.flush(remaining - len(expanded) + 1)
            expanded += tail
            if len(expanded) > remaining:
                raise KnowledgeIngestError(
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    "pdf_stream_too_large",
                    "A compressed PDF stream exceeds the safe extraction limit",
                )
            expanded_bytes += len(expanded)
            payloads.append(expanded)
        except KnowledgeIngestError:
            raise
        except zlib.error:
            pass

    fragments: List[str] = []
    for payload in payloads:
        blocks = re.findall(rb"BT(.*?)ET", payload, flags=re.DOTALL) or [payload]
        for block in blocks:
            for match in re.finditer(rb"(\((?:\\.|[^\\()])*\))\s*(?:Tj|'|\")", block):
                value = _pdf_literal(match.group(1)).strip()
                if value:
                    fragments.append(value)
            for array in re.findall(rb"\[(.*?)\]\s*TJ", block, flags=re.DOTALL):
                values = [_pdf_literal(value) for value in re.findall(rb"\((?:\\.|[^\\()])*\)", array)]
                joined = "".join(values).strip()
                if joined:
                    fragments.append(joined)
    return "\n".join(dict.fromkeys(fragments))


def _extract_pdf_text(data: bytes) -> Tuple[str, str]:
    if not data.startswith(b"%PDF-"):
        raise KnowledgeIngestError(HTTPStatus.UNPROCESSABLE_ENTITY, "invalid_pdf", "PDF header is invalid")
    text = _extract_pdf_with_python(data)
    if text.strip():
        return text, "python-pdf"
    text = _extract_pdf_with_pdftotext(data)
    if text.strip():
        return text, "pdftotext"
    text = _extract_pdf_heuristic(data)
    if text.strip():
        return text, "pdf-text-stream"
    raise KnowledgeIngestError(
        HTTPStatus.UNPROCESSABLE_ENTITY,
        "pdf_text_unavailable",
        "PDF contains no extractable text; send extracted text or install pypdf/pdftotext (scans require OCR)",
    )


def _extract_ingest_text(
    payload: Dict[str, Any], filename: str, extension: str
) -> Tuple[str, str, bytes, str]:
    text_supplied = "text" in payload and payload.get("text") is not None
    encoded_fields = [name for name in ("base64", "content_base64", "data_base64") if payload.get(name) is not None]
    if text_supplied and encoded_fields:
        raise KnowledgeIngestError(
            HTTPStatus.BAD_REQUEST,
            "ambiguous_content",
            "Send either text or base64, not both",
        )
    if len(encoded_fields) > 1:
        raise KnowledgeIngestError(HTTPStatus.BAD_REQUEST, "ambiguous_content", "Send only one base64 field")
    if text_supplied:
        value = payload.get("text")
        if not isinstance(value, str) or not value.strip():
            raise KnowledgeIngestError(HTTPStatus.BAD_REQUEST, "empty_document", "text must be a non-empty string")
        text = value
        method = "provided-text"
        source_data = value.encode("utf-8")
        if extension in {".md", ".markdown", ".txt"}:
            source_filename = filename
        else:
            source_filename = "%s-提取文本.txt" % Path(filename).stem
    elif encoded_fields:
        data = _decoded_base64(payload[encoded_fields[0]])
        source_data = data
        source_filename = filename
        if extension in {".md", ".markdown", ".txt"}:
            text = _decoded_text_file(data)
            method = "decoded-text"
        elif extension == ".docx":
            text = _extract_docx_text(data)
            method = "docx-xml"
        else:
            text, method = _extract_pdf_text(data)
    else:
        raise KnowledgeIngestError(
            HTTPStatus.BAD_REQUEST,
            "missing_content",
            "Provide document content in text or base64",
        )
    text = unicodedata.normalize("NFC", text).replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
    if len(text) > MAX_EXTRACTED_TEXT_CHARS:
        raise KnowledgeIngestError(
            HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            "extracted_text_too_large",
            "Extracted text is too large to process safely",
        )
    if not text.strip():
        raise KnowledgeIngestError(HTTPStatus.UNPROCESSABLE_ENTITY, "empty_document", "Document contains no text")
    return text, method, source_data, source_filename


def _plain_ingested_text(value: str) -> str:
    value = WIKILINK_PATTERN.sub(lambda match: match.group(1).split("|", 1)[-1].split("#", 1)[0], value)
    value = LOCAL_MARKDOWN_LINK_PATTERN.sub(r"\1", value)
    value = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", value)
    value = re.sub(r"[*_~]", "", value)
    return re.sub(r"\s+", " ", value).strip()


def _bounded_point_parts(value: str) -> List[str]:
    sentences = re.split(r"(?<=[。！？!?；;])\s*|(?<=\.)\s+(?=[A-Z0-9\u3400-\u9fff])", value)
    parts: List[str] = []
    for sentence in sentences:
        sentence = sentence.strip()
        while len(sentence) > MAX_POINT_CHARS:
            boundary = max(
                sentence.rfind(mark, 0, MAX_POINT_CHARS) for mark in ("，", ",", "：", ":", "、", " ")
            )
            if boundary < MAX_POINT_CHARS // 3:
                boundary = MAX_POINT_CHARS
            parts.append(sentence[: boundary + (boundary < MAX_POINT_CHARS)].strip())
            sentence = sentence[boundary + (boundary < MAX_POINT_CHARS) :].strip()
        if sentence:
            parts.append(sentence)
    return parts


def _split_atomic_points(text: str, extension: str) -> List[Dict[str, str]]:
    if extension in {".md", ".markdown"}:
        _, text = _parse_frontmatter(text)
    points: List[Dict[str, str]] = []
    paragraph: List[str] = []
    section = ""

    def add_value(raw_value: str, active_section: str) -> None:
        value = _plain_ingested_text(raw_value)
        for part in _bounded_point_parts(value):
            if len(part) >= 2:
                points.append({"text": part, "section": _plain_ingested_text(active_section)})

    def flush() -> None:
        if paragraph:
            add_value(" ".join(paragraph), section)
            paragraph.clear()

    for raw_line in text.splitlines():
        line = raw_line.strip()
        heading = re.match(r"^#{1,6}\s+(.+?)\s*#*$", line)
        bullet = re.match(r"^(?:[-+*]|\d+[.)])\s+(.+)$", line)
        if heading:
            flush()
            section = heading.group(1)
        elif bullet:
            flush()
            add_value(bullet.group(1), section)
        elif not line:
            flush()
        elif re.fullmatch(r"[-=_]{3,}", line):
            flush()
        else:
            paragraph.append(line)
    flush()

    unique: List[Dict[str, str]] = []
    seen: Set[str] = set()
    for point in points:
        key = re.sub(r"\s+", " ", point["text"]).casefold()
        if key not in seen:
            seen.add(key)
            unique.append(point)
    if not unique:
        raise KnowledgeIngestError(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "no_knowledge_points",
            "No usable knowledge points were found in the document",
        )
    if len(unique) > MAX_KNOWLEDGE_POINTS:
        raise KnowledgeIngestError(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "too_many_knowledge_points",
            "Document produced more than %d knowledge points; split it into smaller documents" % MAX_KNOWLEDGE_POINTS,
        )
    return unique


def _truncate_utf8(value: str, maximum_bytes: int) -> str:
    while len(value.encode("utf-8")) > maximum_bytes:
        value = value[:-1]
    return value


def _safe_note_component(value: str, fallback: str, maximum_bytes: int = 80) -> str:
    value = unicodedata.normalize("NFC", value)
    value = re.sub(r"[\x00-\x1f<>:\"/\\|?*\[\]]+", "-", value)
    value = re.sub(r"\s+", "-", value).strip(" .-_")
    value = _truncate_utf8(value, maximum_bytes).rstrip(" .-_")
    return value or fallback


def _all_visible_note_names(root: Path) -> Set[str]:
    names: Set[str] = set()
    for current, directories, filenames in os.walk(root):
        directories[:] = [name for name in directories if not name.startswith(".")]
        for filename in filenames:
            if filename.casefold().endswith(".md"):
                names.add(unicodedata.normalize("NFC", Path(filename).stem).casefold())
    return names


def _unique_note_name(candidate: str, reserved: Set[str]) -> str:
    candidate = _safe_note_component(candidate, "知识点", 190)
    value = candidate
    suffix = 2
    while unicodedata.normalize("NFC", value).casefold() in reserved:
        suffix_text = "-%d" % suffix
        value = _truncate_utf8(candidate, 190 - len(suffix_text)) + suffix_text
        suffix += 1
    reserved.add(unicodedata.normalize("NFC", value).casefold())
    return value


def _unique_attachment_filename(root: Path, requested: str) -> str:
    extension = Path(requested).suffix.casefold()
    stem = _safe_note_component(Path(requested).stem, "导入文档", 150)
    existing: Set[str] = set()
    for current, directories, filenames in os.walk(root):
        directories[:] = [name for name in directories if not name.startswith(".")]
        existing.update(unicodedata.normalize("NFC", filename).casefold() for filename in filenames)
    candidate = stem + extension
    suffix = 2
    while candidate.casefold() in existing:
        suffix_text = "-%d" % suffix
        candidate = _truncate_utf8(stem, 190 - len(suffix_text)) + suffix_text + extension
        suffix += 1
    return candidate


def _yaml_value(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _point_label(value: str) -> str:
    label = re.sub(r"[。！？!?；;，,:：]+$", "", value.strip())
    return _safe_note_component(label, "知识点", 72)


def _point_title(value: str, limit: int = 56) -> str:
    title = re.sub(r"[。！？!?；;]+$", "", _plain_ingested_text(value)).strip()
    if len(title) > limit:
        title = title[: limit - 3].rstrip() + "..."
    return title or "知识点"


_SAVE_MODE_ALIASES = {
    "save_and_graph": "save_and_graph",
    "save- and-graph": "save_and_graph",  # kept harmlessly for malformed clients
    "graph": "save_and_graph",
    "knowledge-point": "save_and_graph",
    "knowledge_point": "save_and_graph",
    "active": "save_and_graph",
    "save": "save",
    "note": "save",
    "vault": "save",
    "save_only": "save",
    "save-only": "save",
    "excluded": "save",
    "exclude": "save",
    "skip": "skip",
    "discard": "skip",
    "ignore": "skip",
}


def _normalise_save_mode(value: Any, selected: bool = True) -> str:
    """Return the small, stable set of review decisions used by the API."""

    if not selected:
        return "skip"
    if value is None or (isinstance(value, str) and not value.strip()):
        return "save_and_graph"
    if not isinstance(value, str):
        raise KnowledgeIngestError(
            HTTPStatus.BAD_REQUEST,
            "invalid_save_mode",
            "save_mode must be a string",
        )
    raw = value.strip().casefold().replace(" ", "_")
    # A few natural-language values are convenient for a small UI and do not
    # change the persisted contract.
    raw = {
        "仅保存": "save",
        "保存": "save",
        "存入图谱": "save_and_graph",
        "保存并入图": "save_and_graph",
        "跳过": "skip",
    }.get(raw, raw)
    mode = _SAVE_MODE_ALIASES.get(raw)
    if mode is None:
        raise KnowledgeIngestError(
            HTTPStatus.BAD_REQUEST,
            "invalid_save_mode",
            "save_mode must be save_and_graph, save, or skip",
        )
    return mode


def _normalise_keywords(value: Any, fallback: Optional[List[str]] = None) -> List[str]:
    if value is None:
        values: Any = fallback or []
    elif isinstance(value, str):
        values = re.split(r"[,，、;；\n]+", value)
    elif isinstance(value, (list, tuple)):
        values = value
    else:
        raise KnowledgeIngestError(
            HTTPStatus.BAD_REQUEST,
            "invalid_keywords",
            "keywords must be a list or string",
        )
    result: List[str] = []
    seen: Set[str] = set()
    for item in values:
        if not isinstance(item, str):
            continue
        cleaned = _display_text(item)
        if not cleaned:
            continue
        cleaned = cleaned[:80]
        key = cleaned.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(cleaned)
        if len(result) >= 12:
            break
    return result


def _candidate_keywords(value: str, limit: int = 8) -> List[str]:
    """Extract lightweight, deterministic keywords for review and search.

    This deliberately does not pretend to be semantic extraction. It gives
    the review UI useful defaults without introducing a model dependency into
    the minimal ingest loop.
    """

    cleaned = _plain_ingested_text(value)
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9_+#.-]{2,}|[\u3400-\u9fff]{2,16}", cleaned)
    stopwords = {
        "这个",
        "我们",
        "需要",
        "可以",
        "应该",
        "以及",
        "进行",
        "相关",
        "一个",
        "为了",
        "用户",
    }
    result: List[str] = []
    seen: Set[str] = set()
    for token in tokens:
        token = token.strip("._-+")
        if len(token) < 2 or token in stopwords:
            continue
        key = token.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(token)
        if len(result) >= limit:
            break
    if not result:
        words = [word for word in re.split(r"\s+", cleaned) if len(word) >= 2]
        result = _normalise_keywords(words[:limit])
    return result


def _draft_identifier(value: Any) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise KnowledgeIngestError(
            HTTPStatus.BAD_REQUEST,
            "invalid_draft_id",
            "draft_id must be a non-empty string",
        )
    value = value.strip()
    if len(value) > 128 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value):
        raise KnowledgeIngestError(
            HTTPStatus.BAD_REQUEST,
            "invalid_draft_id",
            "draft_id contains unsupported characters",
        )
    return value


def _requested_draft_id(payload: Dict[str, Any]) -> Optional[str]:
    first = payload.get("draft_id")
    second = payload.get("preview_id")
    if first is not None and second is not None and str(first).strip() != str(second).strip():
        raise KnowledgeIngestError(
            HTTPStatus.BAD_REQUEST,
            "ambiguous_draft_id",
            "draft_id and preview_id must match when both are provided",
        )
    return _draft_identifier(first if first is not None else second)


def _clone_json(value: Dict[str, Any]) -> Dict[str, Any]:
    """Copy a public draft/result without exposing mutable store state."""

    return json.loads(json.dumps(value, ensure_ascii=False))


def _knowledge_draft_path(root: Path, draft_id: str) -> Path:
    """Return the private on-disk location for one review draft."""

    # ``draft_id`` has already gone through _draft_identifier.  Keeping the
    # path construction in one helper makes it harder for a future caller to
    # accidentally turn a client value into a path traversal.
    vault_key = hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest()[:20]
    return Path(tempfile.gettempdir()) / KNOWLEDGE_DRAFT_STORAGE_DIRECTORY / vault_key / (draft_id + ".json")


def _save_knowledge_draft(root: Path, draft: Dict[str, Any]) -> None:
    """Persist a draft without exposing its binary source bytes in the API."""

    draft_id = str(draft["draft_id"])
    payload = dict(draft)
    source_data = payload.pop("source_data", b"")
    if not isinstance(source_data, bytes):
        raise KnowledgeIngestError(HTTPStatus.INTERNAL_SERVER_ERROR, "draft_storage_error", "Draft source data is invalid")
    payload["source_data_base64"] = base64.b64encode(source_data).decode("ascii")
    target = _knowledge_draft_path(root, draft_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(".%s.%d.tmp" % (target.name, os.getpid()))
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _load_knowledge_draft(root: Path, draft_id: str) -> Optional[Dict[str, Any]]:
    """Load a persisted review draft, returning None for stale/corrupt data."""

    target = _knowledge_draft_path(root, draft_id)
    if not target.is_file():
        return None
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or str(payload.get("draft_id", "")) != draft_id:
            return None
        encoded = payload.pop("source_data_base64", "")
        if not isinstance(encoded, str):
            return None
        source_data = base64.b64decode(encoded, validate=True)
        payload["source_data"] = source_data
        return payload
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        return None


def _draft_public_response(draft: Dict[str, Any]) -> Dict[str, Any]:
    candidates: List[Dict[str, Any]] = []
    for point in draft["points"]:
        candidates.append(
            {
                "id": point["id"],
                "candidate_id": point["id"],
                "title": point["title"],
                "text": point["text"],
                "summary": point["text"][:180],
                "section": point["section"],
                "index": point["index"],
                "keywords": list(point["keywords"]),
                "selected": bool(point.get("selected", True)),
                "save_mode": point.get("save_mode", "save_and_graph"),
                "mode": point.get("save_mode", "save_and_graph"),
                "graph_status": point.get("graph_status", "active"),
            }
        )
    source = dict(draft["source"])
    return {
        "status": draft.get("status", "awaiting_review"),
        "draft_id": draft["draft_id"],
        "preview_id": draft["draft_id"],
        "source": source,
        "keywords": list(draft.get("keywords", [])),
        # Both names are exposed while the UI settles on one contract.
        "candidates": candidates,
        "knowledge_points": candidates,
    }


def _prune_knowledge_drafts() -> None:
    if len(KNOWLEDGE_DRAFTS) < MAX_KNOWLEDGE_DRAFTS:
        return
    for key, value in list(KNOWLEDGE_DRAFTS.items()):
        if value.get("status") in {"committed", "cancelled"}:
            KNOWLEDGE_DRAFTS.pop(key, None)
            if len(KNOWLEDGE_DRAFTS) < MAX_KNOWLEDGE_DRAFTS:
                return


def _render_point_note(
    name: str,
    point: Dict[str, str],
    index: int,
    filename: str,
    source_summary: str,
    source_format: str,
    extraction_method: str,
    source_path: str,
    date: str,
    keywords: Optional[List[str]] = None,
    graph_status: str = "active",
    save_mode: str = "save_and_graph",
) -> str:
    stored_source_filename = Path(source_path).name
    keywords = _normalise_keywords(keywords)
    graph_status = "active" if graph_status not in {"excluded", "draft"} else graph_status
    metadata = [
        "---",
        "type: knowledge-point",
        "status: 待核验",
        "graph_status: %s" % graph_status,
        "save_mode: %s" % save_mode,
        "evidence_status: 待核验",
        "confidentiality: 私密",
        "created: %s" % date,
        "updated: %s" % date,
        "source_document: %s" % _yaml_value(filename),
        "source_file: %s" % _yaml_value(stored_source_filename),
        "source_format: %s" % _yaml_value(source_format),
        "source_summary: %s" % _yaml_value(source_summary),
        "source_index: %d" % index,
        "extraction_method: %s" % _yaml_value(extraction_method),
        "source_path: %s" % _yaml_value(source_path),
        "source_section: %s" % _yaml_value(point["section"]),
        "source_excerpt: %s" % _yaml_value(point["text"][:240]),
        "summary: %s" % _yaml_value(point["text"][:180]),
        "knowledge_kind: atomic-claim",
        "keywords: %s" % _yaml_value(keywords),
        "tags: [文档导入, 知识点]",
        "---",
    ]
    locator = point["section"] or "待补充"
    return (
        "\n".join(metadata)
        + "\n\n# %s\n\n## 原子结论\n\n%s\n\n"
        + "## 来源与证据\n\n- 来源文档：%s\n- 来源文件：[[%s]]\n"
        + "- 来源摘要：[[%s]]\n- 证据定位：%s\n- 原文证据：%s\n\n"
        + "## 理解与适用边界\n\n待核验。\n\n## 待确认\n\n- 待核验\n"
    ) % (point["title"], point["text"], filename, stored_source_filename, source_summary, locator, point["text"])


def _render_source_summary(
    name: str,
    filename: str,
    source_format: str,
    extraction_method: str,
    source_path: str,
    point_records: List[Dict[str, Any]],
    date: str,
) -> str:
    first_summary = point_records[0]["text"][:180]
    stored_source_filename = Path(source_path).name
    lines = [
        "---",
        "type: source-summary",
        "status: 待核验",
        "evidence_status: 待核验",
        "confidentiality: 私密",
        "created: %s" % date,
        "updated: %s" % date,
        "source_document: %s" % _yaml_value(filename),
        "source_file: %s" % _yaml_value(stored_source_filename),
        "source_format: %s" % _yaml_value(source_format),
        "extraction_method: %s" % _yaml_value(extraction_method),
        "source_path: %s" % _yaml_value(source_path),
        "knowledge_point_count: %d" % len(point_records),
        "summary: %s" % _yaml_value(first_summary),
        "tags: [文档导入, 来源摘要]",
        "---",
        "",
        "# %s" % name,
        "",
        "原始文件：[[%s]]" % stored_source_filename,
        "",
        "## 内容概览",
        "",
        first_summary,
        "",
        "## 原子知识点",
        "",
    ]
    lines.extend("- [[%s]]：%s" % (record["id"], record["text"][:120]) for record in point_records)
    return "\n".join(lines) + "\n"


def _refresh_frontmatter_updated(text: str, date: str) -> str:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return text
    closing = next((index for index in range(1, len(lines)) if lines[index].strip() == "---"), None)
    if closing is None:
        return text
    for index in range(1, closing):
        if re.match(r"^updated\s*:", lines[index], flags=re.IGNORECASE):
            lines[index] = "updated: %s" % date
            return "\n".join(lines) + ("\n" if text.endswith("\n") else "")
    lines.insert(closing, "updated: %s" % date)
    return "\n".join(lines) + ("\n" if text.endswith("\n") else "")


def _index_table_rows(text: str, heading: str) -> List[str]:
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


def _safe_table_text(value: str, limit: int = 120) -> str:
    value = re.sub(r"\s+", " ", value).replace("|", "／").strip()
    return value[:limit]


def _knowledge_index_views(
    existing: str,
    source_document: str,
    source_file: str,
    point_records: List[Dict[str, Any]],
) -> str:
    source_link = "[[%s]]" % source_file
    point_links = {"[[%s]]" % record["id"] for record in point_records}
    source_rows = [row for row in _index_table_rows(existing, "按来源") if source_link not in row]
    point_rows = [
        row
        for row in _index_table_rows(existing, "按知识点")
        if source_link not in row and not any(link in row for link in point_links)
    ]
    source_rows.append(
        "| %s | %s | %s |"
        % (
            source_link,
            _safe_table_text(source_document),
            "、".join("[[%s]]" % record["id"] for record in point_records),
        )
    )
    point_rows.extend(
        "| [[%s]] | %s | %s |" % (record["id"], _safe_table_text(record["text"]), source_link)
        for record in point_records
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


def _updated_index_text(
    original: str,
    source_document: str,
    source_file: str,
    point_records: List[Dict[str, Any]],
    date: str,
) -> str:
    text = _refresh_frontmatter_updated(original, date)
    marker_start = "<!-- KNOWLEDGE_POINTS_START -->"
    marker_end = "<!-- KNOWLEDGE_POINTS_END -->"
    if marker_start in text and marker_end in text:
        prefix, remainder = text.split(marker_start, 1)
        existing, suffix = remainder.split(marker_end, 1)
        views = _knowledge_index_views(existing, source_document, source_file, point_records)
        legacy = existing.strip()
        if legacy in {"", "暂无已导入的知识点。"} or ("### 按来源" in legacy and "### 按知识点" in legacy):
            legacy = ""
        elif legacy:
            legacy = "\n\n#### 既有目录记录\n\n" + re.sub(r"^#{1,6}\s+", "##### ", legacy, flags=re.MULTILINE)
        return prefix.rstrip() + "\n\n" + marker_start + "\n" + views + legacy + "\n" + marker_end + suffix

    source_heading = re.search(r"^###\s+按来源\s*$", text, flags=re.MULTILINE)
    if source_heading:
        next_section = re.search(r"^##\s+", text[source_heading.end() :], flags=re.MULTILINE)
        end = source_heading.end() + next_section.start() if next_section else len(text)
        existing = text[source_heading.start() : end]
        views = _knowledge_index_views(existing, source_document, source_file, point_records)
        return text[: source_heading.start()] + views + text[end:]

    views = _knowledge_index_views("", source_document, source_file, point_records)
    return text.rstrip() + "\n\n## 知识点\n\n" + views + "\n"


def _updated_log_text(
    original: str,
    source_file: str,
    point_records: List[Dict[str, Any]],
    timestamp: str,
    date: str,
) -> str:
    text = _refresh_frontmatter_updated(original, date).rstrip()
    links = "、".join("[[%s]]" % record["id"] for record in point_records)
    return (
        text
        + "\n| %s | 导入 | %s | [[%s]] | 完成（%d 个知识点） |\n"
        % (timestamp, links, source_file, len(point_records))
    )


def _atomic_replace_text(path: Path, value: str) -> None:
    temporary = path.with_name(".%s.%d.%d.tmp" % (path.name, os.getpid(), threading.get_ident()))
    try:
        temporary.write_text(value, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _prepare_knowledge_draft(root: Path, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Extract a document and reserve deterministic note names in memory only."""

    filename, extension = _validated_upload_filename(payload.get("filename"))
    extracted_text, extraction_method, source_data, requested_source_filename = _extract_ingest_text(
        payload, filename, extension
    )
    points = _split_atomic_points(extracted_text, extension)
    root = root.resolve()
    index_path = root / "01-知识库目录.md"
    log_path = root / "02-更新流水账.md"
    requested_id = _requested_draft_id(payload)
    source_hash = hashlib.sha256(source_data).hexdigest()
    signature = hashlib.sha256(
        (filename + "\0" + extension + "\0" + source_hash).encode("utf-8")
    ).hexdigest()

    with KNOWLEDGE_INGEST_LOCK:
        if not index_path.is_file() or not log_path.is_file():
            raise KnowledgeIngestError(
                HTTPStatus.CONFLICT,
                "vault_not_initialized",
                "Vault requires 01-知识库目录.md and 02-更新流水账.md before ingest",
            )
        if requested_id is not None and requested_id in KNOWLEDGE_DRAFTS:
            existing = KNOWLEDGE_DRAFTS[requested_id]
            if existing.get("root") != str(root) or existing.get("signature") != signature:
                raise KnowledgeIngestError(
                    HTTPStatus.CONFLICT,
                    "draft_id_conflict",
                    "draft_id is already associated with a different document",
                )
            return existing

        if requested_id is not None:
            persisted = _load_knowledge_draft(root, requested_id)
            if persisted is not None:
                if persisted.get("root") != str(root) or persisted.get("signature") != signature:
                    raise KnowledgeIngestError(
                        HTTPStatus.CONFLICT,
                        "draft_id_conflict",
                        "draft_id is already associated with a different document",
                    )
                KNOWLEDGE_DRAFTS[requested_id] = persisted
                return persisted

        draft_id = requested_id or uuid4().hex
        _prune_knowledge_drafts()
        original_index = index_path.read_text(encoding="utf-8")
        original_log = log_path.read_text(encoding="utf-8")
        reserved = _all_visible_note_names(root)
        source_stem = _safe_note_component(Path(filename).stem, "导入文档", 80)
        summary_name = _unique_note_name("%s-来源摘要" % source_stem, reserved)
        source_directory = root / "07-素材附件" / "原始资料"
        stored_source_filename = _unique_attachment_filename(root, requested_source_filename)
        source_path = (source_directory / stored_source_filename).relative_to(root).as_posix()
        point_records: List[Dict[str, Any]] = []
        for index, point in enumerate(points, start=1):
            candidate = "%s-知识点-%02d-%s" % (source_stem, index, _point_label(point["text"]))
            note_name = _unique_note_name(candidate, reserved)
            point_records.append(
                {
                    "id": note_name,
                    "title": _point_title(point["text"]),
                    "text": point["text"],
                    "section": point["section"],
                    "index": index,
                    "keywords": _candidate_keywords(point["text"]),
                    "selected": True,
                    "save_mode": "save_and_graph",
                    "graph_status": "active",
                }
            )
        draft = {
            "draft_id": draft_id,
            "root": str(root),
            "signature": signature,
            "filename": filename,
            "extension": extension,
            "extracted_text": extracted_text,
            "extraction_method": extraction_method,
            "source_data": source_data,
            "requested_source_filename": requested_source_filename,
            "source_stem": source_stem,
            "summary_name": summary_name,
            "stored_source_filename": stored_source_filename,
            "source_path": source_path,
            "summary_path": (root / "07-素材附件" / "来源摘要" / source_stem / (summary_name + ".md")).relative_to(root).as_posix(),
            "source": {
                "id": summary_name,
                "filename": filename,
                "format": extension.lstrip("."),
                "extraction_method": extraction_method,
                "summary_note": summary_name,
                "path": (root / "07-素材附件" / "来源摘要" / source_stem / (summary_name + ".md")).relative_to(root).as_posix(),
                "source_path": source_path,
                "sha256": source_hash,
            },
            "keywords": _normalise_keywords(
                [keyword for point in point_records for keyword in point["keywords"]]
            ),
            "points": point_records,
            "status": "awaiting_review",
        }
        KNOWLEDGE_DRAFTS[draft_id] = draft
        _save_knowledge_draft(root, draft)
        return draft


def _persist_knowledge_transaction(
    root: Path,
    draft: Dict[str, Any],
    point_records: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Persist one reviewed document using the existing three-file transaction."""

    root = root.resolve()
    filename = str(draft["filename"])
    extension = str(draft["extension"])
    extraction_method = str(draft["extraction_method"])
    source_data = draft["source_data"]
    source_stem = str(draft["source_stem"])
    summary_name = str(draft["summary_name"])
    stored_source_filename = str(draft["stored_source_filename"])
    index_path = root / "01-知识库目录.md"
    log_path = root / "02-更新流水账.md"
    source_directory = root / "07-素材附件" / "原始资料"
    target_directory = root / "08-知识点" / source_stem
    summary_directory = root / "07-素材附件" / "来源摘要" / source_stem
    source_path = (source_directory / stored_source_filename).relative_to(root).as_posix()

    with KNOWLEDGE_INGEST_LOCK:
        if not index_path.is_file() or not log_path.is_file():
            raise KnowledgeIngestError(
                HTTPStatus.CONFLICT,
                "vault_not_initialized",
                "Vault requires 01-知识库目录.md and 02-更新流水账.md before ingest",
            )
        original_index = index_path.read_text(encoding="utf-8")
        original_log = log_path.read_text(encoding="utf-8")

        # A preview reserves names in memory.  Refuse a blind overwrite if the
        # vault changed before commit; the caller can simply preview again.
        if (source_directory / stored_source_filename).exists() or any(
            (target_directory / (record["id"] + ".md")).exists() for record in point_records
        ) or (summary_directory / (summary_name + ".md")).exists():
            raise KnowledgeIngestError(
                HTTPStatus.CONFLICT,
                "note_name_conflict",
                "A generated note filename already exists; preview the document again",
            )

        now = datetime.now().astimezone()
        date = now.date().isoformat()
        timestamp = now.strftime("%Y-%m-%d %H:%M")
        new_index = _updated_index_text(
            original_index, filename, stored_source_filename, point_records, date
        )
        new_log = _updated_log_text(
            original_log, stored_source_filename, point_records, timestamp, date
        )
        note_payloads: List[Tuple[str, str, Path]] = [
            (
                summary_name,
                _render_source_summary(
                    summary_name,
                    filename,
                    extension.lstrip("."),
                    extraction_method,
                    source_path,
                    point_records,
                    date,
                ),
                summary_directory / (summary_name + ".md"),
            )
        ]
        note_payloads.extend(
            (
                record["id"],
                _render_point_note(
                    record["id"],
                    {
                        "title": record["title"],
                        "text": record["text"],
                        "section": record.get("section", ""),
                    },
                    int(record.get("index", index)),
                    filename,
                    summary_name,
                    extension.lstrip("."),
                    extraction_method,
                    source_path,
                    date,
                    keywords=_normalise_keywords(record.get("keywords")),
                    graph_status=str(record.get("graph_status", "active") or "active"),
                    save_mode=str(record.get("save_mode", "save_and_graph") or "save_and_graph"),
                ),
                target_directory / (record["id"] + ".md"),
            )
            for index, record in enumerate(point_records, start=1)
        )

        created_paths: List[Path] = []
        managed_directories = {
            target_directory,
            target_directory.parent,
            summary_directory,
            summary_directory.parent,
            source_directory,
            source_directory.parent,
        }
        directories_to_cleanup = [path for path in managed_directories if not path.exists()]
        with tempfile.TemporaryDirectory(prefix=".knowledge-ingest-", dir=root) as staging_value:
            staging = Path(staging_value)
            staged_notes: List[Tuple[Path, Path]] = []
            staged_source = staging / stored_source_filename
            staged_source.write_bytes(source_data)
            for name, content, target in note_payloads:
                staged = staging / (name + ".md")
                staged.write_text(content, encoding="utf-8")
                staged_notes.append((staged, target))
            replaced_index = False
            replaced_log = False
            try:
                target_directory.mkdir(parents=True, exist_ok=True)
                summary_directory.mkdir(parents=True, exist_ok=True)
                source_directory.mkdir(parents=True, exist_ok=True)
                source_target = source_directory / stored_source_filename
                if source_target.exists():
                    raise FileExistsError(str(source_target))
                os.link(staged_source, source_target)
                created_paths.append(source_target)
                for staged, target in staged_notes:
                    if target.exists():
                        raise FileExistsError(str(target))
                    os.link(staged, target)
                    created_paths.append(target)
                if index_path.read_text(encoding="utf-8") != original_index or log_path.read_text(encoding="utf-8") != original_log:
                    raise KnowledgeIngestError(
                        HTTPStatus.CONFLICT,
                        "vault_changed_during_ingest",
                        "Index or operation log changed during ingest; retry the request",
                    )
                _atomic_replace_text(index_path, new_index)
                replaced_index = True
                _atomic_replace_text(log_path, new_log)
                replaced_log = True
            except Exception as exc:
                for path in reversed(created_paths):
                    try:
                        path.unlink()
                    except OSError:
                        pass
                if replaced_index:
                    _atomic_replace_text(index_path, original_index)
                if replaced_log:
                    _atomic_replace_text(log_path, original_log)
                for directory in sorted(
                    directories_to_cleanup, key=lambda path: len(path.parts), reverse=True
                ):
                    try:
                        directory.rmdir()
                    except OSError:
                        pass
                if isinstance(exc, KnowledgeIngestError):
                    raise
                if isinstance(exc, FileExistsError):
                    raise KnowledgeIngestError(
                        HTTPStatus.CONFLICT,
                        "note_name_conflict",
                        "A generated note filename already exists; retry the request",
                    ) from exc
                raise KnowledgeIngestError(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    "ingest_write_failed",
                    "Could not persist the knowledge transaction",
                ) from exc

        summary_path = (summary_directory / (summary_name + ".md")).relative_to(root).as_posix()
        output_points: List[Dict[str, Any]] = []
        for record in point_records:
            output = dict(record)
            output["path"] = (target_directory / (record["id"] + ".md")).relative_to(root).as_posix()
            output["summary"] = output.pop("text")[:180]
            output_points.append(output)
        return {
            "status": "complete",
            "source": {
                "id": summary_name,
                "filename": filename,
                "format": extension.lstrip("."),
                "extraction_method": extraction_method,
                "summary_note": summary_name,
                "path": summary_path,
                "source_path": source_path,
                "sha256": hashlib.sha256(source_data).hexdigest(),
            },
            "knowledge_points": output_points,
            "transaction": {
                "index": "01-知识库目录.md",
                "log": "02-更新流水账.md",
                "log_rows_added": 1,
                "created_files": [path.relative_to(root).as_posix() for path in created_paths],
            },
        }


def _normalise_draft_selection(
    draft: Dict[str, Any], payload: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """Apply user choices to draft candidates and return persistable points."""

    candidates = {str(point["id"]): point for point in draft["points"]}
    choices: Dict[str, Dict[str, Any]] = {
        key: {
            "selected": bool(point.get("selected", True)),
            "save_mode": _normalise_save_mode(point.get("save_mode", "save_and_graph"), bool(point.get("selected", True))),
            "keywords": list(point.get("keywords", [])),
        }
        for key, point in candidates.items()
    }
    raw_points = payload.get("selected_points", payload.get("knowledge_points"))
    raw_ids = payload.get("selected_point_ids")
    if raw_points is not None and raw_ids is not None:
        raise KnowledgeIngestError(
            HTTPStatus.BAD_REQUEST,
            "ambiguous_selection",
            "Send selected_points or selected_point_ids, not both",
        )
    if raw_ids is not None:
        if not isinstance(raw_ids, list):
            raise KnowledgeIngestError(
                HTTPStatus.BAD_REQUEST,
                "invalid_selection",
                "selected_point_ids must be a list",
            )
        selected_ids: Set[str] = set()
        for value in raw_ids:
            if not isinstance(value, str) or value not in candidates:
                raise KnowledgeIngestError(
                    HTTPStatus.BAD_REQUEST,
                    "unknown_knowledge_point",
                    "selected_point_ids contains an unknown candidate",
                )
            selected_ids.add(value)
        for key in choices:
            choices[key]["selected"] = key in selected_ids
            choices[key]["save_mode"] = "save_and_graph" if key in selected_ids else "skip"
    elif raw_points is not None:
        if not isinstance(raw_points, list):
            raise KnowledgeIngestError(
                HTTPStatus.BAD_REQUEST,
                "invalid_selection",
                "selected_points must be a list",
            )
        seen: Set[str] = set()
        # An explicit list is treated as the complete user selection.  This
        # makes an empty list unambiguously mean "save nothing".
        for key in choices:
            choices[key]["selected"] = False
            choices[key]["save_mode"] = "skip"
        for item in raw_points:
            if isinstance(item, str):
                point_id = item
                selected = True
                mode_value = "save_and_graph"
                keywords_value = None
            elif isinstance(item, dict):
                point_id = item.get("id", item.get("candidate_id", item.get("note_id")))
                selected = item.get("selected", True)
                if not isinstance(selected, bool):
                    raise KnowledgeIngestError(
                        HTTPStatus.BAD_REQUEST,
                        "invalid_selection",
                        "selected must be a boolean",
                    )
                mode_value = item.get("save_mode", item.get("mode"))
                keywords_value = item.get("keywords")
            else:
                raise KnowledgeIngestError(
                    HTTPStatus.BAD_REQUEST,
                    "invalid_selection",
                    "selected_points entries must be objects or ids",
                )
            if not isinstance(point_id, str) or point_id not in candidates:
                raise KnowledgeIngestError(
                    HTTPStatus.BAD_REQUEST,
                    "unknown_knowledge_point",
                    "selected_points contains an unknown candidate",
                )
            if point_id in seen:
                raise KnowledgeIngestError(
                    HTTPStatus.BAD_REQUEST,
                    "duplicate_selection",
                    "selected_points contains a duplicate candidate",
                )
            seen.add(point_id)
            mode = _normalise_save_mode(mode_value, selected)
            choices[point_id] = {
                "selected": selected and mode != "skip",
                "save_mode": mode,
                "keywords": _normalise_keywords(keywords_value, candidates[point_id].get("keywords", [])),
            }

    selected_points: List[Dict[str, Any]] = []
    for point in draft["points"]:
        choice = choices[str(point["id"])]
        mode = _normalise_save_mode(choice.get("save_mode"), bool(choice.get("selected", True)))
        if mode == "skip" or not choice.get("selected", True):
            continue
        record = dict(point)
        record["keywords"] = _normalise_keywords(choice.get("keywords"), point.get("keywords", []))
        record["save_mode"] = mode
        record["graph_status"] = "active" if mode == "save_and_graph" else "excluded"
        record["selected"] = True
        selected_points.append(record)
    if not selected_points:
        raise KnowledgeIngestError(
            HTTPStatus.BAD_REQUEST,
            "no_selected_knowledge_points",
            "Select at least one knowledge point to commit",
        )
    return selected_points


def ingest_knowledge_document(root: Path, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Legacy one-step ingest; retained for existing clients."""

    with KNOWLEDGE_INGEST_LOCK:
        draft = _prepare_knowledge_draft(root, payload)
        try:
            result = _persist_knowledge_transaction(root, draft, [dict(point) for point in draft["points"]])
        finally:
            # Legacy calls do not need to leave review drafts behind.
            KNOWLEDGE_DRAFTS.pop(draft["draft_id"], None)
            _knowledge_draft_path(root, draft["draft_id"]).unlink(missing_ok=True)
        return result


def _commit_knowledge_draft(root: Path, payload: Dict[str, Any]) -> Dict[str, Any]:
    requested_id = _requested_draft_id(payload)
    if requested_id is None:
        raise KnowledgeIngestError(
            HTTPStatus.BAD_REQUEST,
            "missing_draft_id",
            "draft_id (or preview_id) is required",
        )
    with KNOWLEDGE_INGEST_LOCK:
        draft = KNOWLEDGE_DRAFTS.get(requested_id)
        if draft is None:
            draft = _load_knowledge_draft(root, requested_id)
            if draft is not None:
                KNOWLEDGE_DRAFTS[requested_id] = draft
        if draft is None:
            raise KnowledgeIngestError(HTTPStatus.NOT_FOUND, "draft_not_found", "Knowledge draft not found")
        if draft.get("root") != str(root.resolve()):
            raise KnowledgeIngestError(HTTPStatus.NOT_FOUND, "draft_not_found", "Knowledge draft not found")
        if draft.get("status") == "committed":
            result = _clone_json(draft["result"])
            result["_idempotent"] = True
            return result
        if draft.get("status") == "cancelled":
            raise KnowledgeIngestError(
                HTTPStatus.CONFLICT,
                "draft_cancelled",
                "Knowledge draft has been cancelled",
            )
        selected_points = _normalise_draft_selection(draft, payload)
        result = _persist_knowledge_transaction(root, draft, selected_points)
        result["draft_id"] = requested_id
        result["preview_id"] = requested_id
        result["review"] = {
            "selected_count": len(selected_points),
            "graph_count": sum(1 for point in selected_points if point.get("save_mode") == "save_and_graph"),
            "saved_only_count": sum(1 for point in selected_points if point.get("save_mode") == "save"),
        }
        draft["status"] = "committed"
        draft["result"] = _clone_json(result)
        _save_knowledge_draft(root, draft)
        return result


def _cancel_knowledge_draft(root: Path, payload: Dict[str, Any]) -> Dict[str, Any]:
    requested_id = _requested_draft_id(payload)
    if requested_id is None:
        raise KnowledgeIngestError(
            HTTPStatus.BAD_REQUEST,
            "missing_draft_id",
            "draft_id (or preview_id) is required",
        )
    with KNOWLEDGE_INGEST_LOCK:
        draft = KNOWLEDGE_DRAFTS.get(requested_id)
        if draft is None:
            draft = _load_knowledge_draft(root, requested_id)
            if draft is not None:
                KNOWLEDGE_DRAFTS[requested_id] = draft
        if draft is None or draft.get("root") != str(root.resolve()):
            raise KnowledgeIngestError(HTTPStatus.NOT_FOUND, "draft_not_found", "Knowledge draft not found")
        if draft.get("status") == "committed":
            raise KnowledgeIngestError(
                HTTPStatus.CONFLICT,
                "draft_already_committed",
                "Knowledge draft has already been committed",
            )
        draft["status"] = "cancelled"
        _save_knowledge_draft(root, draft)
        return {
            "status": "cancelled",
            "draft_id": requested_id,
            "preview_id": requested_id,
        }


def build_note_orchestrator(storage_dir: Path) -> NoteOrchestrator:
    api_url = os.environ.get("WENXIN_API_URL", "").strip()
    api_key = os.environ.get("WENXIN_API_KEY", "").strip()
    if api_url and api_key:
        provider = WenxinMultimodalProvider(api_url=api_url, api_key=api_key)
    else:
        try:
            provider = MacOSVisionOCRProvider()
        except RuntimeError:
            provider = None
    agents = {"ocr": OCRAgent(provider)} if provider else None
    return NoteOrchestrator(agents=agents, storage_dir=storage_dir)


def make_handler(
    orchestrator: Orchestrator,
    directory: Path = ROOT,
    note_orchestrator: Optional[NoteOrchestrator] = None,
    vault_root: Optional[Path] = None,
) -> Type[SimpleHTTPRequestHandler]:
    static_path = Path(directory).expanduser().resolve()
    static_directory = str(static_path)
    # Keep the historical behavior when vault_root is omitted, while allowing
    # a public deployment to serve code from one directory and use a separate
    # (sanitized) vault for API reads and writes.
    vault_directory = Path(vault_root if vault_root is not None else static_path).expanduser().resolve()
    runtime_directory = vault_directory / ".note_runs" if vault_root is not None else static_path / ".note_runs"
    note_service = note_orchestrator or build_note_orchestrator(runtime_directory)
    note_committer = NoteVaultCommitter(vault_directory)
    note_promoter = NotePromotionCommitter(vault_directory)
    source_lifecycle = SourceLifecycleManager(vault_directory)

    class CareerCopilotHandler(SimpleHTTPRequestHandler):
        server_version = "CareerCopilot/1.0"

        _PRIVATE_STATIC_DIRECTORIES = {
            ".agents",
            ".git",
            ".obsidian",
            ".note_runs",
            ".note_runtime",
            "career_copilot",
            "note_knowledge",
            "tests",
        }

        def _static_path_allowed(self, request_path: str) -> bool:
            """Keep runtime state, source code, and hidden vault metadata private."""

            parts = [part for part in unquote(request_path).split("/") if part]
            if any(part in {".", ".."} or part.startswith(".") for part in parts):
                return False
            if any(part.casefold() in {item.casefold() for item in self._PRIVATE_STATIC_DIRECTORIES} for part in parts):
                return False
            # A separately configured vault may live beneath the static root
            # (as demo_vault does).  Do not let SimpleHTTPRequestHandler serve
            # its notes or attachments directly; API projections remain the
            # only public view of that vault.
            if vault_directory != static_path:
                candidate = (static_path / Path(*parts)).resolve()
                try:
                    candidate.relative_to(vault_directory)
                except ValueError:
                    pass
                else:
                    return False
            return True

        def _reject_private_static(self, request_path: str) -> bool:
            if self._static_path_allowed(request_path):
                return False
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return True

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, directory=static_directory, **kwargs)

        def end_headers(self) -> None:
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, HEAD, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            super().end_headers()

        def do_OPTIONS(self) -> None:
            self.send_response(HTTPStatus.NO_CONTENT)
            self.send_header("Allow", "GET, HEAD, POST, OPTIONS")
            self.end_headers()

        def do_GET(self) -> None:
            parsed_url = urlparse(self.path)
            path = parsed_url.path
            if path == "/api/health":
                self._send_json(HTTPStatus.OK, {"status": "ok", "services": ["career_copilot", "note_knowledge"]})
                return
            if path.rstrip("/") == "/api/vault/inbox":
                self._handle_vault_inbox(parse_qs(parsed_url.query, keep_blank_values=True))
                return
            if path.rstrip("/") == "/api/vault/search":
                self._handle_vault_search(parse_qs(parsed_url.query, keep_blank_values=True))
                return
            if path.rstrip("/") == "/api/knowledge/graph":
                self._handle_knowledge_graph()
                return
            if path.rstrip("/") in {"/api/knowledge/source", "/api/knowledge/source/inspect", "/api/vault/source"}:
                self._handle_source_inspect(parse_qs(parsed_url.query, keep_blank_values=True))
                return
            if path.rstrip("/") in {"/api/vault/promotion/propose", "/api/vault/propose"}:
                self._handle_promotion_propose(parse_qs(urlparse(self.path).query))
                return
            note_match = NOTE_RUN_PATH.match(path)
            if note_match:
                self._handle_note_get(unquote(note_match.group(1)))
                return
            if path.startswith("/api/"):
                self._send_error_json(HTTPStatus.NOT_FOUND, "not_found", "API route not found")
                return
            if self._reject_private_static(path):
                return
            super().do_GET()

        def do_HEAD(self) -> None:
            path = urlparse(self.path).path
            if self._reject_private_static(path):
                return
            super().do_HEAD()

        def _handle_knowledge_graph(self) -> None:
            try:
                graph = build_knowledge_graph(vault_directory)
            except KnowledgeGraphError as exc:
                self._send_error_json(HTTPStatus.CONFLICT, "duplicate_note_name", str(exc))
                return
            except (OSError, UnicodeError) as exc:
                self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, "vault_read_error", str(exc))
                return
            self._send_json(HTTPStatus.OK, graph)

        @staticmethod
        def _source_query_payload(params: Dict[str, List[str]]) -> Dict[str, Any]:
            return {key: (values[-1] if values else "") for key, values in params.items()}

        def _handle_source_inspect(self, params: Dict[str, List[str]]) -> None:
            payload = self._source_query_payload(params)
            try:
                result = source_lifecycle.inspect(
                    source_path=str(payload.get("source_path", payload.get("source", "")) or ""),
                    summary_path=str(payload.get("summary_path", "") or ""),
                    source_id=str(payload.get("source_id", payload.get("id", "")) or ""),
                )
            except NoteVaultError as exc:
                self._send_error_json(HTTPStatus(exc.status), exc.code, str(exc))
                return
            except (OSError, UnicodeError) as exc:
                self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, "vault_read_error", str(exc))
                return
            self._send_json(HTTPStatus.OK, result)

        def _handle_source_propose(self) -> None:
            try:
                payload = self._read_json(max_bytes=MAX_NOTE_BODY_BYTES)
                result = source_lifecycle.propose(
                    action=payload.get("action", payload.get("operation", "")),
                    source_path=str(payload.get("source_path", payload.get("source", "")) or ""),
                    summary_path=str(payload.get("summary_path", "") or ""),
                    source_id=str(payload.get("source_id", payload.get("id", "")) or ""),
                    operation_id=str(payload.get("operation_id", payload.get("proposal_id", "")) or ""),
                )
            except RequestError as exc:
                self._send_error_json(exc.status, exc.code, str(exc))
                return
            except NoteVaultError as exc:
                self._send_error_json(HTTPStatus(exc.status), exc.code, str(exc))
                return
            except (OSError, UnicodeError) as exc:
                self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, "vault_read_error", str(exc))
                return
            self._send_json(HTTPStatus.OK, result)

        def _handle_source_commit(self) -> None:
            try:
                payload = self._read_json(max_bytes=MAX_NOTE_BODY_BYTES)
                expected = payload.get("expected_hashes", payload.get("hashes", None))
                if expected is not None and not isinstance(expected, dict):
                    raise NoteVaultError("invalid_expected_hashes", "expected_hashes must be an object", 400)
                result = source_lifecycle.commit(
                    proposal_id=str(payload.get("proposal_id", payload.get("operation_id", "")) or ""),
                    action=str(payload.get("action", payload.get("operation", "")) or ""),
                    source_path=str(payload.get("source_path", payload.get("source", "")) or ""),
                    summary_path=str(payload.get("summary_path", "") or ""),
                    source_id=str(payload.get("source_id", payload.get("id", "")) or ""),
                    expected_hashes=expected,
                    operation_id=str(payload.get("operation_id", "") or ""),
                )
            except RequestError as exc:
                self._send_error_json(exc.status, exc.code, str(exc))
                return
            except NoteVaultError as exc:
                self._send_error_json(HTTPStatus(exc.status), exc.code, str(exc))
                return
            except (OSError, UnicodeError) as exc:
                self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, "vault_write_error", str(exc))
                return
            self._send_json(HTTPStatus.OK if result.get("idempotent") else HTTPStatus.CREATED, result)

        def _handle_source_direct(self, action: str) -> None:
            """Convenience route: still performs propose + commit with hashes."""

            try:
                payload = self._read_json(max_bytes=MAX_NOTE_BODY_BYTES)
                proposal = source_lifecycle.propose(
                    action=action,
                    source_path=str(payload.get("source_path", payload.get("source", "")) or ""),
                    summary_path=str(payload.get("summary_path", "") or ""),
                    source_id=str(payload.get("source_id", payload.get("id", "")) or ""),
                    operation_id=str(payload.get("operation_id", payload.get("proposal_id", "")) or ""),
                )
                if proposal.get("idempotent"):
                    self._send_json(HTTPStatus.OK, proposal)
                    return
                result = source_lifecycle.commit(
                    proposal_id=str(proposal.get("proposal_id", "")),
                    expected_hashes=proposal.get("hashes", {}),
                )
            except RequestError as exc:
                self._send_error_json(exc.status, exc.code, str(exc))
                return
            except NoteVaultError as exc:
                self._send_error_json(HTTPStatus(exc.status), exc.code, str(exc))
                return
            except (OSError, UnicodeError) as exc:
                self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, "vault_write_error", str(exc))
                return
            self._send_json(HTTPStatus.OK if result.get("idempotent") else HTTPStatus.CREATED, result)

        def _handle_vault_inbox(self, params: Dict[str, List[str]]) -> None:
            raw_placeholder = (params.get("include_placeholder", [""])[0] or "").strip().casefold()
            include_placeholder = raw_placeholder in {"1", "true", "yes", "on"}
            try:
                result = list_vault_inbox(vault_directory, include_placeholder=include_placeholder)
            except (OSError, UnicodeError) as exc:
                self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, "vault_read_error", str(exc))
                return
            self._send_json(HTTPStatus.OK, result)

        def _handle_vault_search(self, params: Dict[str, List[str]]) -> None:
            query = params.get("q", [""])[0]
            raw_limit = (params.get("limit", [""])[0] or "").strip()
            limit = MAX_VAULT_SEARCH_RESULTS
            if raw_limit:
                try:
                    limit = int(raw_limit)
                except ValueError:
                    self._send_error_json(HTTPStatus.BAD_REQUEST, "invalid_limit", "limit must be a positive integer")
                    return
            try:
                result = search_vault(vault_directory, query=query, limit=limit)
            except VaultSearchError as exc:
                self._send_error_json(HTTPStatus.BAD_REQUEST, exc.code, str(exc))
                return
            except (OSError, UnicodeError) as exc:
                self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, "vault_read_error", str(exc))
                return
            self._send_json(HTTPStatus.OK, result)

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            if path.rstrip("/") == "/api/knowledge/preview":
                self._handle_knowledge_preview()
                return
            if path.rstrip("/") == "/api/knowledge/commit":
                self._handle_knowledge_commit()
                return
            if path.rstrip("/") == "/api/knowledge/cancel":
                self._handle_knowledge_cancel()
                return
            if path.rstrip("/") == "/api/knowledge/ingest":
                self._handle_knowledge_ingest()
                return
            if path.rstrip("/") in {"/api/knowledge/source/propose", "/api/vault/source/propose"}:
                self._handle_source_propose()
                return
            if path.rstrip("/") in {"/api/knowledge/source/commit", "/api/vault/source/commit"}:
                self._handle_source_commit()
                return
            if path.rstrip("/") in {"/api/knowledge/source/archive", "/api/vault/source/archive"}:
                self._handle_source_direct("archive")
                return
            if path.rstrip("/") in {"/api/knowledge/source/restore", "/api/vault/source/restore"}:
                self._handle_source_direct("restore")
                return
            if path.rstrip("/") == "/api/notes/propose":
                self._handle_note_propose()
                return
            if path.rstrip("/") == "/api/notes/commit":
                self._handle_note_commit()
                return
            if path.rstrip("/") in {"/api/vault/promotion/propose", "/api/vault/propose"}:
                self._handle_promotion_propose()
                return
            if path.rstrip("/") in {"/api/vault/promotion/commit", "/api/vault/commit"}:
                self._handle_promotion_commit()
                return
            if path in {"/api/notes/process", "/api/note/process", "/api/notes/analyze"}:
                self._handle_note_process()
                return
            note_retry_match = NOTE_RETRY_PATH.match(path)
            if note_retry_match:
                self._handle_note_retry(unquote(note_retry_match.group(1)), unquote(note_retry_match.group(2)))
                return
            ocr_correct_match = NOTE_OCR_CORRECT_PATH.match(path)
            if ocr_correct_match:
                self._handle_ocr_correction(unquote(ocr_correct_match.group(1)))
                return
            if path == "/api/analyze":
                self._handle_analyze()
                return
            retry_match = RETRY_PATH.match(path)
            if retry_match:
                self._handle_retry(unquote(retry_match.group(1)), unquote(retry_match.group(2)))
                return
            self._send_error_json(HTTPStatus.NOT_FOUND, "not_found", "API route not found")

        def _handle_knowledge_preview(self) -> None:
            try:
                payload = self._read_json(max_bytes=MAX_INGEST_BODY_BYTES)
                draft = _prepare_knowledge_draft(vault_directory, payload)
                result = _draft_public_response(draft)
            except RequestError as exc:
                self._send_error_json(exc.status, exc.code, str(exc))
                return
            except KnowledgeIngestError as exc:
                self._send_error_json(exc.status, exc.code, str(exc))
                return
            except (OSError, UnicodeError) as exc:
                self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, "vault_read_error", str(exc))
                return
            self._send_json(HTTPStatus.OK, result)

        def _handle_knowledge_commit(self) -> None:
            try:
                payload = self._read_json(max_bytes=MAX_INGEST_BODY_BYTES)
                action = payload.get("action")
                if payload.get("cancel") is True or payload.get("cancelled") is True or action in {"cancel", "cancelled"}:
                    result = _cancel_knowledge_draft(vault_directory, payload)
                    self._send_json(HTTPStatus.OK, result)
                    return
                result = _commit_knowledge_draft(vault_directory, payload)
                idempotent = bool(result.pop("_idempotent", False))
            except RequestError as exc:
                self._send_error_json(exc.status, exc.code, str(exc))
                return
            except KnowledgeIngestError as exc:
                self._send_error_json(exc.status, exc.code, str(exc))
                return
            except (OSError, UnicodeError) as exc:
                self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, "vault_write_error", str(exc))
                return
            self._send_json(HTTPStatus.OK if idempotent else HTTPStatus.CREATED, result)

        def _handle_knowledge_cancel(self) -> None:
            try:
                payload = self._read_json(max_bytes=MAX_INGEST_BODY_BYTES)
                result = _cancel_knowledge_draft(vault_directory, payload)
            except RequestError as exc:
                self._send_error_json(exc.status, exc.code, str(exc))
                return
            except KnowledgeIngestError as exc:
                self._send_error_json(exc.status, exc.code, str(exc))
                return
            self._send_json(HTTPStatus.OK, result)

        def _handle_knowledge_ingest(self) -> None:
            try:
                payload = self._read_json(max_bytes=MAX_INGEST_BODY_BYTES)
                result = ingest_knowledge_document(vault_directory, payload)
            except RequestError as exc:
                self._send_error_json(exc.status, exc.code, str(exc))
                return
            except KnowledgeIngestError as exc:
                self._send_error_json(exc.status, exc.code, str(exc))
                return
            except (OSError, UnicodeError) as exc:
                self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, "vault_write_error", str(exc))
                return
            self._send_json(HTTPStatus.CREATED, result)

        def _handle_note_process(self) -> None:
            try:
                payload = self._read_json(max_bytes=MAX_NOTE_BODY_BYTES)
                image = payload.get("image_data", payload.get("image", ""))
                if isinstance(image, dict):
                    image = image.get("data", image.get("url", ""))
                result = note_service.process(
                    image=image,
                    image_name=str(payload.get("image_name", payload.get("filename", "note-image")) or "note-image"),
                    image_mime=str(payload.get("image_mime", payload.get("mime_type", "")) or ""),
                    user_note=str(payload.get("user_note", payload.get("note", "")) or ""),
                    provided_text=str(payload.get("ocr_text", payload.get("text", "")) or ""),
                    keywords=payload.get("keywords", payload.get("query", payload.get("search_keywords", None))),
                )
            except RequestError as exc:
                self._send_error_json(exc.status, exc.code, str(exc))
                return
            except (TypeError, ValueError) as exc:
                self._send_error_json(HTTPStatus.BAD_REQUEST, "validation_error", str(exc))
                return
            self._send_json(HTTPStatus.CREATED, result)

        def _note_archive_input(self, payload: Dict[str, Any]) -> Tuple[str, str, str]:
            """Resolve a NoteFlow run into the Markdown used by the commit boundary."""

            run_id = payload.get("run_id", payload.get("note_run_id"))
            if run_id is None:
                raise NoteVaultError("missing_run_id", "run_id is required", 400)
            if not isinstance(run_id, str) or not run_id.strip():
                raise NoteVaultError("invalid_run_id", "run_id must be a non-empty string", 400)
            run_id = run_id.strip()
            markdown_keys = [key for key in ("archive_markdown", "approved_markdown", "markdown") if key in payload]
            if len(markdown_keys) > 1:
                raise NoteVaultError(
                    "ambiguous_markdown",
                    "Send only one of archive_markdown, approved_markdown, or markdown",
                    400,
                )
            markdown_key = markdown_keys[0] if markdown_keys else None
            markdown = payload.get(markdown_key, "") if markdown_key else ""
            if markdown_key is not None and not isinstance(markdown, str):
                raise NoteVaultError(
                    "invalid_markdown",
                    "archive_markdown must be a string",
                    400,
                )
            title = str(payload.get("title", "") or "").strip()
            source_label = str(payload.get("source_label", payload.get("source", "NoteFlow 整理")) or "NoteFlow 整理").strip()
            if not isinstance(markdown, str) or not markdown.strip():
                try:
                    run = note_service.get_run(run_id)
                except KeyError as exc:
                    raise NoteVaultError("run_not_found", "Note run not found", 404) from exc
                markdown = str(run.get("archive_markdown", "") or "")
                if not title:
                    title = str((run.get("outputs", {}).get("archive", {}) or {}).get("标题", "") or "")
                if source_label == "NoteFlow 整理":
                    image = run.get("image", {}) or {}
                    source_label = str(image.get("name", "") or "NoteFlow 整理")
            if not markdown.strip():
                raise NoteVaultError("archive_unavailable", "This run has no archive Markdown", 422)
            return run_id, markdown, title or ""

        def _handle_note_propose(self) -> None:
            try:
                payload = self._read_json(max_bytes=MAX_NOTE_BODY_BYTES)
                run_id, markdown, title = self._note_archive_input(payload)
                source_label = str(payload.get("source_label", payload.get("source", "NoteFlow 整理")) or "NoteFlow 整理")
                result = note_committer.propose(run_id, markdown, title=title, source_label=source_label)
            except RequestError as exc:
                self._send_error_json(exc.status, exc.code, str(exc))
                return
            except NoteVaultError as exc:
                self._send_error_json(HTTPStatus(exc.status), exc.code, str(exc))
                return
            except (OSError, UnicodeError) as exc:
                self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, "vault_read_error", str(exc))
                return
            self._send_json(HTTPStatus.OK, result)

        def _handle_note_commit(self) -> None:
            try:
                payload = self._read_json(max_bytes=MAX_NOTE_BODY_BYTES)
                # A committed note carries its run id in frontmatter, so a
                # retry after the transient NoteFlow run has expired can be
                # answered idempotently without reconstructing the archive.
                raw_run_id = payload.get("run_id", payload.get("note_run_id"))
                supplied_markdown = payload.get(
                    "archive_markdown",
                    payload.get("approved_markdown", payload.get("markdown", "")),
                )
                if (
                    isinstance(raw_run_id, str)
                    and raw_run_id.strip()
                    and (not isinstance(supplied_markdown, str) or not supplied_markdown.strip())
                ):
                    existing = note_committer.find_committed(raw_run_id.strip())
                    if existing is not None:
                        self._send_json(HTTPStatus.OK, existing)
                        return
                run_id, markdown, title = self._note_archive_input(payload)
                source_label = str(payload.get("source_label", payload.get("source", "NoteFlow 整理")) or "NoteFlow 整理")
                result = note_committer.commit(
                    run_id,
                    markdown,
                    title=title,
                    source_label=source_label,
                    expected_index_sha256=str(payload.get("expected_index_sha256", payload.get("index_sha256", "")) or ""),
                    expected_log_sha256=str(payload.get("expected_log_sha256", payload.get("log_sha256", "")) or ""),
                    note_name=str(payload.get("note_name", "") or ""),
                )
            except RequestError as exc:
                self._send_error_json(exc.status, exc.code, str(exc))
                return
            except NoteVaultError as exc:
                self._send_error_json(HTTPStatus(exc.status), exc.code, str(exc))
                return
            except (OSError, UnicodeError) as exc:
                self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, "vault_write_error", str(exc))
                return
            status = HTTPStatus.OK if result.get("idempotent") else HTTPStatus.CREATED
            self._send_json(status, result)

        def _promotion_payload_from_query(self, query: Dict[str, List[str]]) -> Dict[str, Any]:
            return {key: values[-1] for key, values in query.items() if values}

        def _handle_promotion_propose(self, query: Optional[Dict[str, List[str]]] = None) -> None:
            try:
                payload = self._promotion_payload_from_query(query or {}) if query is not None else self._read_json(max_bytes=MAX_NOTE_BODY_BYTES)
                source_path = payload.get("source_path", payload.get("source", ""))
                if isinstance(source_path, dict):
                    source_path = source_path.get("path", source_path.get("source_path", ""))
                result = note_promoter.propose(
                    source_path=source_path,
                    mode=str(payload.get("mode", "new") or "new"),
                    target_path=str(payload.get("target_path", "") or ""),
                    target_name=str(payload.get("target_name", "") or ""),
                    title=str(payload.get("title", "") or ""),
                    content=payload.get("content", payload.get("markdown", None)),
                    target_section=str(payload.get("target_section", "") or ""),
                    proposal_id=str(payload.get("proposal_id", payload.get("promotion_id", "")) or ""),
                    source_label=str(payload.get("source_label", "笔记整理") or "笔记整理"),
                )
            except RequestError as exc:
                self._send_error_json(exc.status, exc.code, str(exc))
                return
            except NoteVaultError as exc:
                self._send_error_json(HTTPStatus(exc.status), exc.code, str(exc))
                return
            except (OSError, UnicodeError) as exc:
                self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, "vault_read_error", str(exc))
                return
            self._send_json(HTTPStatus.OK, result)

        def _handle_promotion_commit(self) -> None:
            try:
                payload = self._read_json(max_bytes=MAX_NOTE_BODY_BYTES)
                proposal_id = payload.get("proposal_id", payload.get("promotion_id", payload.get("id", "")))
                expected_hashes = payload.get("expected_hashes", payload.get("hashes", {}))
                approved_content = payload.get("approved_content", payload.get("approved_markdown", None))
                extra_payload = {
                    key: value
                    for key, value in payload.items()
                    if key
                    not in {
                        "proposal_id",
                        "promotion_id",
                        "id",
                        "expected_hashes",
                        "hashes",
                        "approved_content",
                        "approved_markdown",
                    }
                }
                result = note_promoter.commit(
                    proposal_id=str(proposal_id or ""),
                    expected_hashes=expected_hashes if isinstance(expected_hashes, dict) else None,
                    approved_content=approved_content,
                    **extra_payload,
                )
            except RequestError as exc:
                self._send_error_json(exc.status, exc.code, str(exc))
                return
            except NoteVaultError as exc:
                self._send_error_json(HTTPStatus(exc.status), exc.code, str(exc))
                return
            except (OSError, UnicodeError) as exc:
                self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, "vault_write_error", str(exc))
                return
            self._send_json(HTTPStatus.OK if result.get("idempotent") else HTTPStatus.CREATED, result)

        def _handle_note_get(self, run_id: str) -> None:
            try:
                result = note_service.get_run(run_id)
            except KeyError:
                self._send_error_json(HTTPStatus.NOT_FOUND, "run_not_found", "Note run not found")
                return
            self._send_json(HTTPStatus.OK, result)

        def _handle_note_retry(self, run_id: str, stage: str) -> None:
            try:
                self._read_json(allow_empty=True, max_bytes=MAX_NOTE_BODY_BYTES)
                result = note_service.retry_stage(run_id, stage)
            except RequestError as exc:
                self._send_error_json(exc.status, exc.code, str(exc))
                return
            except KeyError:
                self._send_error_json(HTTPStatus.NOT_FOUND, "run_not_found", "Note run not found")
                return
            except ValueError as exc:
                self._send_error_json(HTTPStatus.BAD_REQUEST, "validation_error", str(exc))
                return
            self._send_json(HTTPStatus.OK, result)

        def _handle_ocr_correction(self, run_id: str) -> None:
            try:
                payload = self._read_json(max_bytes=MAX_NOTE_BODY_BYTES)
                corrected_text = payload.get(
                    "corrected_text",
                    payload.get(
                        "ocr_corrected_text",
                        payload.get("校正后文本", payload.get("修订后文本", payload.get("text", ""))),
                    ),
                )
                expected_revision = payload.get(
                    "expected_ocr_revision",
                    payload.get("expected_revision", payload.get("ocr_revision", None)),
                )
                rerun_downstream = payload.get("rerun_downstream", True)
                if not isinstance(rerun_downstream, bool):
                    raise ValueError("rerun_downstream must be a boolean")
                result = note_service.correct_ocr(
                    run_id,
                    corrected_text,
                    expected_revision=expected_revision,
                    rerun_downstream=rerun_downstream,
                )
            except RequestError as exc:
                self._send_error_json(exc.status, exc.code, str(exc))
                return
            except KeyError:
                self._send_error_json(HTTPStatus.NOT_FOUND, "run_not_found", "Note run not found")
                return
            except OCRRevisionConflict as exc:
                self._send_error_json(HTTPStatus.CONFLICT, OCRRevisionConflict.code, str(exc))
                return
            except ValueError as exc:
                self._send_error_json(HTTPStatus.BAD_REQUEST, "validation_error", str(exc))
                return
            self._send_json(HTTPStatus.OK, result)

        def _handle_analyze(self) -> None:
            try:
                payload = self._read_json()
                job_description = payload.get("job_description", payload.get("job", ""))
                resume = payload.get("resume", payload.get("profile", ""))
                target_role = payload.get("target_role", payload.get("title", ""))
                result = orchestrator.analyze(job_description, resume, target_role)
            except RequestError as exc:
                self._send_error_json(exc.status, exc.code, str(exc))
                return
            except ValueError as exc:
                self._send_error_json(HTTPStatus.BAD_REQUEST, "validation_error", str(exc))
                return
            self._send_json(HTTPStatus.CREATED, result)

        def _handle_retry(self, run_id: str, stage: str) -> None:
            try:
                self._read_json(allow_empty=True)
                result = orchestrator.retry_stage(run_id, stage)
            except RequestError as exc:
                self._send_error_json(exc.status, exc.code, str(exc))
                return
            except KeyError:
                self._send_error_json(HTTPStatus.NOT_FOUND, "run_not_found", "Analysis run not found")
                return
            except ValueError as exc:
                self._send_error_json(HTTPStatus.BAD_REQUEST, "validation_error", str(exc))
                return
            self._send_json(HTTPStatus.OK, result)

        def _read_json(self, allow_empty: bool = False, max_bytes: int = MAX_BODY_BYTES) -> Dict[str, Any]:
            raw = self._read_request_body(allow_empty=allow_empty, max_bytes=max_bytes)
            if not raw and allow_empty:
                return {}
            try:
                value = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RequestError(HTTPStatus.BAD_REQUEST, "invalid_json", "Body must be valid UTF-8 JSON") from exc
            if not isinstance(value, dict):
                raise RequestError(HTTPStatus.BAD_REQUEST, "invalid_json", "JSON body must be an object")
            return value

        def _read_request_body(self, allow_empty: bool, max_bytes: int) -> bytes:
            """Read a request body while tolerating Vercel's framing proxy.

            Normal clients send ``Content-Length``.  HTTP/1.1 clients may use
            chunked transfer encoding, which ``BaseHTTPRequestHandler`` does
            not decode for us.  Vercel custom-container forwarding can expose
            neither header to the application; those requests are read until
            the proxy closes the stream or goes idle.  The latter path is
            bounded and closes the connection after one request.
            """

            transfer_encoding = self.headers.get("Transfer-Encoding", "")
            if transfer_encoding:
                codings = [part.strip().casefold() for part in transfer_encoding.split(",") if part.strip()]
                if codings != ["chunked"]:
                    raise RequestError(
                        HTTPStatus.NOT_IMPLEMENTED,
                        "unsupported_transfer_encoding",
                        "Only chunked transfer encoding is supported",
                    )
                return self._read_chunked_body(max_bytes)

            raw_length = self.headers.get("Content-Length")
            if raw_length is not None:
                try:
                    length = int(raw_length)
                except (TypeError, ValueError) as exc:
                    raise RequestError(HTTPStatus.BAD_REQUEST, "invalid_length", "Invalid Content-Length") from exc
                if length < 0 or length > max_bytes:
                    raise RequestError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "body_too_large", "JSON body is too large")
                if length == 0:
                    return b""
                raw = self._read_exact_body(length)
                if len(raw) != length:
                    raise RequestError(HTTPStatus.BAD_REQUEST, "incomplete_body", "Request body ended before Content-Length")
                return raw

            # Vercel's custom-container proxy currently strips request framing
            # headers before forwarding to a plain HTTP server.  A missing
            # length is still rejected when no bytes arrive, preserving the
            # historical 411 response for an actually empty request.
            self.close_connection = True
            raw = self._read_close_delimited_body(max_bytes)
            if not raw and not allow_empty:
                raise RequestError(HTTPStatus.LENGTH_REQUIRED, "length_required", "Content-Length is required")
            return raw

        def _read_exact_body(self, length: int) -> bytes:
            chunks: List[bytes] = []
            remaining = length
            while remaining:
                chunk = self.rfile.read(min(remaining, REQUEST_BODY_READ_CHUNK_BYTES))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            return b"".join(chunks)

        def _read_close_delimited_body(self, max_bytes: int) -> bytes:
            """Read an unframed body until EOF or a bounded idle timeout."""

            chunks: List[bytes] = []
            total = 0
            connection = getattr(self, "connection", None)
            previous_timeout: Optional[float] = None
            timeout_changed = False
            if connection is not None:
                try:
                    previous_timeout = connection.gettimeout()
                    connection.settimeout(UNKNOWN_LENGTH_BODY_IDLE_TIMEOUT)
                    timeout_changed = True
                except (AttributeError, OSError):
                    connection = None
            try:
                while total <= max_bytes:
                    try:
                        # ``read1`` returns bytes already available from the
                        # buffered socket without waiting for a full chunk.
                        reader = getattr(self.rfile, "read1", self.rfile.read)
                        chunk = reader(min(REQUEST_BODY_READ_CHUNK_BYTES, max_bytes - total + 1))
                    except socket.timeout:
                        break
                    except (ConnectionError, OSError):
                        break
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise RequestError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "body_too_large", "JSON body is too large")
                    chunks.append(chunk)
            finally:
                if timeout_changed and connection is not None:
                    try:
                        connection.settimeout(previous_timeout)
                    except OSError:
                        pass
            return b"".join(chunks)

        def _read_chunked_body(self, max_bytes: int) -> bytes:
            """Decode an HTTP/1.1 chunked request body with a byte limit."""

            chunks: List[bytes] = []
            total = 0
            while True:
                line = self.rfile.readline(CHUNK_LINE_MAX_BYTES + 1)
                if not line or len(line) > CHUNK_LINE_MAX_BYTES or not line.endswith(b"\r\n"):
                    raise RequestError(HTTPStatus.BAD_REQUEST, "invalid_chunked_body", "Invalid chunk framing")
                size_token = line[:-2].split(b";", 1)[0].strip()
                try:
                    size = int(size_token, 16)
                except (TypeError, ValueError) as exc:
                    raise RequestError(HTTPStatus.BAD_REQUEST, "invalid_chunked_body", "Invalid chunk size") from exc
                if size < 0:
                    raise RequestError(HTTPStatus.BAD_REQUEST, "invalid_chunked_body", "Invalid chunk size")
                if size == 0:
                    # Consume optional trailer fields through the terminating
                    # empty line.  Trailer values are not used by the API.
                    while True:
                        trailer = self.rfile.readline(CHUNK_LINE_MAX_BYTES + 1)
                        if not trailer or len(trailer) > CHUNK_LINE_MAX_BYTES:
                            raise RequestError(HTTPStatus.BAD_REQUEST, "invalid_chunked_body", "Invalid chunk trailers")
                        if trailer == b"\r\n":
                            return b"".join(chunks)

                if size > max_bytes - total:
                    raise RequestError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "body_too_large", "JSON body is too large")
                chunk = self._read_exact_body(size)
                if len(chunk) != size or self.rfile.read(2) != b"\r\n":
                    raise RequestError(HTTPStatus.BAD_REQUEST, "invalid_chunked_body", "Invalid chunk framing")
                chunks.append(chunk)
                total += size

        def _send_json(self, status: HTTPStatus, value: Dict[str, Any]) -> None:
            body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_error_json(self, status: HTTPStatus, code: str, message: str) -> None:
            self._send_json(status, {"error": {"code": code, "message": message}})

    return CareerCopilotHandler


class RequestError(Exception):
    def __init__(self, status: HTTPStatus, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


def build_server(
    host: str = "127.0.0.1",
    port: int = 8000,
    orchestrator: Optional[Orchestrator] = None,
    directory: Path = ROOT,
    note_orchestrator: Optional[NoteOrchestrator] = None,
    vault_root: Optional[Path] = None,
) -> ThreadingHTTPServer:
    service = orchestrator or Orchestrator()
    return ThreadingHTTPServer(
        (host, port),
        make_handler(
            service,
            directory,
            note_orchestrator,
            vault_root=vault_root,
        ),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the multimodal note knowledge-base server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8000, type=int)
    parser.add_argument(
        "--vault-root",
        default="",
        help="API knowledge-vault root; defaults to the static directory",
    )
    args = parser.parse_args()
    vault_root = Path(args.vault_root).expanduser() if args.vault_root else None
    server = build_server(args.host, args.port, vault_root=vault_root)
    print("Note Knowledge Base running at http://%s:%d" % server.server_address)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
