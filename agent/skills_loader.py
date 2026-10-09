from __future__ import annotations

import os
import re
from pathlib import Path

import yaml

_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


def _load_skill_file(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    match = _FRONTMATTER.match(text)
    if match:
        try:
            meta = yaml.safe_load(match.group(1)) or {}
        except yaml.YAMLError:
            meta = {}
        body = match.group(2).strip()
    else:
        meta, body = {}, text.strip()
    if not isinstance(meta, dict):
        meta = {}
    meta.setdefault("name", path.stem)
    meta.setdefault("description", "")
    triggers = meta.get("triggers") or []
    if isinstance(triggers, str):
        triggers = [t.strip() for t in triggers.split(",") if t.strip()]
    meta["triggers"] = list(triggers)
    meta["body"] = body
    meta["_source_path"] = str(path)
    return meta


def load_skills(skills_dir: Path) -> list[dict]:
    skills: list[dict] = []
    dirs = [skills_dir]
    generated = Path(os.getenv("HERMES_GENERATED_SKILLS_DIR", "/opt/data/generated_skills"))
    if generated != skills_dir:
        dirs.append(generated)
    seen: set[str] = set()
    for directory in dirs:
        if not directory.exists():
            continue
        for path in sorted(directory.glob("*.md")):
            try:
                skill = _load_skill_file(path)
            except OSError:
                continue
            name = str(skill.get("name") or path.stem)
            if name in seen:
                continue
            seen.add(name)
            skills.append(skill)
    return skills


def render_system_prompt(base: str, skills: list[dict], selected: list[dict]) -> str:
    parts = [base.rstrip()]
    if skills:
        parts.append("\n\n## Available skills (catalog)\n")
        for s in skills:
            parts.append(f"- **{s['name']}**: {s.get('description', '').strip()}")
    if selected:
        parts.append("\n\n## Activated skills (full content)\n")
        for s in selected:
            parts.append(f"### {s['name']}\n")
            parts.append(s["body"])
            parts.append("")
    return "\n".join(parts)
