#!/usr/bin/env python3
"""Validate Obsidian links and the note/index/log ingest transaction."""

from __future__ import annotations

import argparse
import re
from collections import defaultdict
from pathlib import Path


REQUIRED_FILES = ("00-知识库说明.md", "01-知识库目录.md", "02-更新流水账.md")
EXCLUDED_TOP_LEVEL = {".agents", ".git", ".obsidian", ".note_runs", ".note_runtime", "__pycache__"}
RAW_EVIDENCE_DIR = ("07-素材附件", "原始资料")
KNOWLEDGE_POINT_DIR = "08-知识点"
TEMPLATE_DIR = "99-模板"
WIKILINK_RE = re.compile(r"!?\[\[([^\[\]]+)\]\]")
MARKDOWN_LINK_RE = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+\.md(?:#[^)]+)?)\)", re.IGNORECASE)
INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
FRONTMATTER_FIELD_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):(?:\s*(.*))?$")


def strip_code(text: str) -> str:
    lines: list[str] = []
    fence: str | None = None
    for line in text.splitlines():
        stripped = line.lstrip()
        if fence:
            if stripped.startswith(fence):
                fence = None
            continue
        if stripped.startswith("```"):
            fence = "```"
            continue
        if stripped.startswith("~~~"):
            fence = "~~~"
            continue
        lines.append(INLINE_CODE_RE.sub("", line))
    return "\n".join(lines)


def visible_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if relative.parts and relative.parts[0] in EXCLUDED_TOP_LEVEL:
            continue
        if any(part.startswith(".") for part in relative.parts):
            continue
        files.append(path)
    return sorted(files)


def is_under(relative: Path, directory: tuple[str, ...]) -> bool:
    return relative.parts[: len(directory)] == directory


def is_raw_evidence(path: Path, root: Path) -> bool:
    return is_under(path.relative_to(root), RAW_EVIDENCE_DIR)


def managed_markdown_files(root: Path, files: list[Path]) -> list[Path]:
    return [
        path
        for path in files
        if path.suffix.lower() == ".md" and not is_raw_evidence(path, root)
    ]


def parse_frontmatter(text: str) -> dict[str, str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}

    metadata: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            return metadata
        match = FRONTMATTER_FIELD_RE.match(line)
        if not match:
            continue
        value = (match.group(2) or "").strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1].strip()
        metadata[match.group(1)] = value
    return {}


def normalized_target(raw_target: str) -> str:
    target = raw_target.split("|", 1)[0].split("#", 1)[0].strip()
    if target.lower().endswith(".md"):
        target = target[:-3]
    return target


def source_file_target(raw_value: str) -> str:
    value = raw_value.strip()
    match = re.fullmatch(r"!?\[\[([^\[\]]+)\]\]", value)
    if match:
        value = match.group(1).split("|", 1)[0].split("#", 1)[0].strip()
    return value


def wikilink_pattern(stem: str) -> re.Pattern[str]:
    return re.compile(r"\[\[" + re.escape(stem) + r"(?:[|#][^\]]*)?\]\]")


def markdown_table_rows(text: str) -> list[str]:
    rows: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not (stripped.startswith("|") and stripped.endswith("|")):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if cells and all(re.fullmatch(r":?-+:?", cell) for cell in cells):
            continue
        rows.append(stripped)
    return rows


def section_text(text: str, heading: str) -> str:
    match = re.search(
        rf"^###\s+{re.escape(heading)}\s*$\n(.*?)(?=^#{{1,3}}\s|\Z)",
        text,
        flags=re.MULTILINE | re.DOTALL,
    )
    return match.group(1) if match else ""


def validate(root: Path, expected: list[str]) -> list[str]:
    root = root.expanduser().resolve()
    errors: list[str] = []

    for name in REQUIRED_FILES:
        if not (root / name).is_file():
            errors.append(f"missing required file: {name}")

    all_files = visible_files(root) if root.is_dir() else []
    markdown_files = managed_markdown_files(root, all_files)
    by_stem: dict[str, list[Path]] = defaultdict(list)
    by_filename: dict[str, list[Path]] = defaultdict(list)
    for path in all_files:
        by_filename[path.name].append(path)
        if path in markdown_files:
            by_stem[path.stem].append(path)

    for stem, paths in sorted(by_stem.items()):
        if len(paths) > 1:
            rendered = ", ".join(str(path.relative_to(root)) for path in paths)
            errors.append(f"duplicate note basename '{stem}': {rendered}")

    reserved_parents = {
        root / "01-工作经历": "工作经历总览.md",
        root / "02-项目案例": "项目案例总览.md",
    }
    for parent, allowed_name in reserved_parents.items():
        if not parent.is_dir():
            continue
        for path in sorted(parent.glob("*.md")):
            if path.name != allowed_name:
                errors.append(
                    f"misrouted note in {parent.relative_to(root)}: {path.name}; "
                    "use the appropriate child directory"
                )

    knowledge_point_sources: dict[str, str] = {}
    for path in markdown_files:
        relative = path.relative_to(root)
        text = path.read_text(encoding="utf-8")
        clean = strip_code(text)
        metadata = parse_frontmatter(text)
        note_type = metadata.get("type", "")
        in_knowledge_point_dir = relative.parts[:1] == (KNOWLEDGE_POINT_DIR,)
        is_template = relative.parts[:1] == (TEMPLATE_DIR,)

        if in_knowledge_point_dir and note_type != "knowledge-point":
            errors.append(f"note in {KNOWLEDGE_POINT_DIR} must have type=knowledge-point: {relative}")
        if note_type == "knowledge-point" and not in_knowledge_point_dir and not is_template:
            errors.append(f"misrouted knowledge-point note: {relative}; use {KNOWLEDGE_POINT_DIR}/")

        if note_type == "knowledge-point" and not is_template:
            source_document = metadata.get("source_document", "").strip()
            raw_source_file = metadata.get("source_file", "").strip()
            if not source_document or source_document == "待补充":
                errors.append(f"knowledge-point missing grounded source_document in {relative}")
            if not raw_source_file or raw_source_file == "待补充":
                errors.append(f"knowledge-point missing source_file in {relative}")
            else:
                source_file = source_file_target(raw_source_file)
                if "/" in source_file or "\\" in source_file or Path(source_file).name != source_file:
                    errors.append(
                        f"knowledge-point source_file must be a basename without a path in {relative}: "
                        f"{raw_source_file}"
                    )
                elif not Path(source_file).suffix:
                    errors.append(
                        f"knowledge-point source_file must include its extension in {relative}: {source_file}"
                    )
                else:
                    matches = by_filename.get(source_file, [])
                    evidence_matches = [candidate for candidate in matches if is_raw_evidence(candidate, root)]
                    if len(matches) > 1:
                        rendered = ", ".join(str(candidate.relative_to(root)) for candidate in matches)
                        errors.append(
                            f"knowledge-point source_file basename is ambiguous in {relative}: "
                            f"{source_file} ({rendered})"
                        )
                    elif len(evidence_matches) != 1:
                        errors.append(
                            f"knowledge-point source_file must resolve under "
                            f"07-素材附件/原始资料 in {relative}: {source_file}"
                        )
                    if not wikilink_pattern(source_file).search(clean):
                        errors.append(
                            f"knowledge-point missing source-file wikilink in {relative}: [[{source_file}]]"
                        )
                    knowledge_point_sources[path.stem] = source_file

        for raw_target in WIKILINK_RE.findall(clean):
            target_part = raw_target.split("|", 1)[0].split("#", 1)[0].strip()
            if "/" in target_part or "\\" in target_part:
                errors.append(f"path-style wikilink in {relative}: [[{raw_target}]]")
                continue
            target = normalized_target(raw_target)
            if not target:
                errors.append(f"empty wikilink in {relative}: [[{raw_target}]]")
                continue
            if Path(target_part).suffix:
                matches = by_filename.get(target_part, [])
            else:
                matches = by_stem.get(target, [])
            if not matches:
                errors.append(f"unresolved wikilink in {relative}: [[{raw_target}]]")
            elif len(matches) > 1:
                errors.append(f"ambiguous wikilink in {relative}: [[{raw_target}]]")

        for target in MARKDOWN_LINK_RE.findall(clean):
            if not target.startswith(("http://", "https://")):
                errors.append(f"local Markdown note link in {relative}: ({target}); use [[文件名]]")

    guide = root / "00-知识库说明.md"
    if guide.is_file():
        guide_text = guide.read_text(encoding="utf-8")
        if "[[文件名]]" not in guide_text:
            errors.append("00-知识库说明.md does not state the basename wikilink rule")
        for phrase in ("写成一篇或多篇知识笔记", "按来源和按知识点", "唯一一行"):
            if phrase not in guide_text:
                errors.append(f"00-知识库说明.md does not contain ingest rule phrase: {phrase}")

    index_path = root / "01-知识库目录.md"
    log_path = root / "02-更新流水账.md"
    index_text = index_path.read_text(encoding="utf-8") if index_path.is_file() else ""
    log_text = log_path.read_text(encoding="utf-8") if log_path.is_file() else ""
    expected_stems: list[str] = []
    expected_patterns: list[re.Pattern[str]] = []
    for value in expected:
        stem = Path(value).stem.strip()
        if not stem:
            continue
        expected_stems.append(stem)
        expected_patterns.append(wikilink_pattern(stem))
        matches = by_stem.get(stem, [])
        if len(matches) != 1:
            errors.append(f"expected note must resolve exactly once: {stem}")
        pattern = wikilink_pattern(stem)
        if not pattern.search(index_text):
            errors.append(f"expected note missing from 01-知识库目录.md: [[{stem}]]")
        if not pattern.search(log_text):
            errors.append(f"expected note missing from 02-更新流水账.md: [[{stem}]]")

    log_rows = markdown_table_rows(log_text)
    if expected_patterns and all(pattern.search(log_text) for pattern in expected_patterns):
        matching_rows = [row for row in log_rows if any(pattern.search(row) for pattern in expected_patterns)]
        complete_rows = [row for row in matching_rows if all(pattern.search(row) for pattern in expected_patterns)]
        if len(matching_rows) != 1 or len(complete_rows) != 1:
            errors.append(
                "expected notes from one ingest must appear together in exactly one "
                "02-更新流水账.md row"
            )
        else:
            transaction_row = complete_rows[0]
            for source_file in sorted(
                {knowledge_point_sources[stem] for stem in expected_stems if stem in knowledge_point_sources}
            ):
                if not wikilink_pattern(source_file).search(transaction_row):
                    errors.append(
                        f"knowledge-point ingest row missing source-file wikilink: [[{source_file}]]"
                    )

    source_rows = markdown_table_rows(section_text(index_text, "按来源"))
    point_rows = markdown_table_rows(section_text(index_text, "按知识点"))
    for stem in expected_stems:
        source_file = knowledge_point_sources.get(stem)
        if not source_file:
            continue
        note_pattern = wikilink_pattern(stem)
        source_pattern = wikilink_pattern(source_file)
        if not any(note_pattern.search(row) and source_pattern.search(row) for row in source_rows):
            errors.append(
                f"knowledge-point missing source mapping in 01-知识库目录.md: "
                f"[[{source_file}]] -> [[{stem}]]"
            )
        if not any(note_pattern.search(row) and source_pattern.search(row) for row in point_rows):
            errors.append(
                f"knowledge-point missing point mapping in 01-知识库目录.md: "
                f"[[{stem}]] -> [[{source_file}]]"
            )

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", default=".", help="Obsidian vault root")
    parser.add_argument("--expect-note", action="append", default=[], help="note basename required in both index and log")
    args = parser.parse_args()
    root = Path(args.root).expanduser().resolve()
    errors = validate(root, args.expect_note)
    if errors:
        print(f"[FAIL] {len(errors)} vault issue(s)")
        for error in errors:
            print(f"- {error}")
        return 1
    files = visible_files(root)
    note_count = len(managed_markdown_files(root, files))
    print(f"[PASS] vault is valid: {note_count} Markdown file(s), 0 issue(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
