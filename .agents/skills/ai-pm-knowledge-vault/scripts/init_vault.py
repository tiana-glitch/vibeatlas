#!/usr/bin/env python3
"""Initialize an AI PM Obsidian vault without overwriting existing files."""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = SKILL_DIR / "assets" / "vault-template"

EMPTY_DIRS = (
    "01-工作经历/公司与岗位",
    "01-工作经历/阶段复盘",
    "02-项目案例/已完成项目",
    "02-项目案例/进行中项目",
    "07-素材附件/图片",
    "07-素材附件/原始资料",
    "08-知识点",
)


def render(text: str, root: Path, profession: str, vault_name: str) -> str:
    return (
        text.replace("{{DATE}}", date.today().isoformat())
        .replace("{{PROFESSION}}", profession)
        .replace("{{VAULT_NAME}}", vault_name or root.name)
    )


def initialize(root: Path, profession: str, vault_name: str, dry_run: bool = False) -> tuple[int, int]:
    root = root.expanduser().resolve()
    if not TEMPLATE_DIR.is_dir():
        raise FileNotFoundError(f"vault template not found: {TEMPLATE_DIR}")

    created = 0
    skipped = 0
    if not dry_run:
        root.mkdir(parents=True, exist_ok=True)
        for relative in EMPTY_DIRS:
            (root / relative).mkdir(parents=True, exist_ok=True)

    for source in sorted(TEMPLATE_DIR.rglob("*")):
        if not source.is_file():
            continue
        relative = source.relative_to(TEMPLATE_DIR)
        destination = root / relative
        if destination.exists():
            print(f"EXISTS  {relative}")
            skipped += 1
            continue
        print(f"CREATE  {relative}")
        created += 1
        if dry_run:
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        text = source.read_text(encoding="utf-8")
        destination.write_text(render(text, root, profession, vault_name), encoding="utf-8")

    print(f"\nVault root: {root}")
    print(f"Created: {created}; preserved existing: {skipped}")
    return created, skipped


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", default=".", help="Obsidian vault root")
    parser.add_argument("--profession", default="AI 产品经理")
    parser.add_argument("--vault-name", default="")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    initialize(Path(args.root), args.profession.strip() or "AI 产品经理", args.vault_name.strip(), args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
