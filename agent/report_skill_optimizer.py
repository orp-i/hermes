from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from collections import Counter
from pathlib import Path
from typing import Any


DEFAULT_THRESHOLD = int(os.getenv("HERMES_REPORT_SKILL_OPTIMIZE_EVERY", "10"))
WORKSPACE_ROOT = Path(os.getenv("HERMES_SANDBOX_WORKSPACE", "/workspace"))
GENERATED_SKILLS_DIR = Path(os.getenv("HERMES_GENERATED_SKILLS_DIR", "/opt/data/generated_skills"))


SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_report_skill_optimizations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    review_count INTEGER NOT NULL,
    trigger_count INTEGER NOT NULL,
    generated_skill_path TEXT NOT NULL,
    source_reviews TEXT,
    findings TEXT,
    status TEXT NOT NULL,
    metadata TEXT
);
"""


PATTERNS: dict[str, tuple[str, ...]] = {
    "dense_text": ("文字密集", "阅读疲劳", "长段落", "缺乏分段", "dense", "readability"),
    "table_fit": ("表格", "列宽", "换行混乱", "拥挤", "table", "column"),
    "hierarchy_spacing": ("层次", "间距", "分隔", "标题", "spacing", "hierarchy"),
    "sequence_wrapping": ("序列", "单行长字符串", "等宽字体", "60", "80", "sequence"),
    "garbled_links": ("乱码", "链接", "方块", "问号", "garbled", "link"),
    "client_readiness": ("客户", "建议", "下一步", "行动", "client", "recommendation"),
    "scoring_professionalism": ("星级", "货币符号", "$", "评分", "成本等级", "score"),
}


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def _review_count(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute(
            "select count(*) as n from agent_executions where tool = 'review_report_layout'"
        ).fetchone()
        return int(row["n"] if row else 0)
    except sqlite3.Error:
        return 0


def _last_trigger_count(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "select max(trigger_count) as n from agent_report_skill_optimizations where status = 'ok'"
    ).fetchone()
    return int(row["n"] or 0)


def _latest_review_files(limit: int = 20) -> list[Path]:
    files = sorted(
        WORKSPACE_ROOT.glob("sessions/*/outputs/*layout-review*.md"),
        key=lambda path: path.stat().st_mtime if path.exists() else 0,
        reverse=True,
    )
    return files[:limit]


def _extract_findings(review_texts: list[str]) -> dict[str, Any]:
    joined = "\n".join(review_texts)
    counts: Counter[str] = Counter()
    examples: dict[str, list[str]] = {}
    for key, markers in PATTERNS.items():
        for marker in markers:
            hits = joined.lower().count(marker.lower())
            if hits:
                counts[key] += hits
        if counts[key]:
            snippets = []
            for line in joined.splitlines():
                if any(marker.lower() in line.lower() for marker in markers):
                    cleaned = " ".join(line.split())
                    if cleaned:
                        snippets.append(cleaned[:220])
                    if len(snippets) >= 3:
                        break
            examples[key] = snippets
    return {
        "counts": dict(counts),
        "examples": examples,
    }


def _rule_lines(findings: dict[str, Any]) -> list[str]:
    counts = Counter(findings.get("counts") or {})
    if not counts:
        return [
            "- Keep reports client-ready: concise executive summary, evidence tables, recommendations, risks, and next steps.",
            "- After layout review, revise Markdown before re-rendering instead of only acknowledging feedback.",
        ]
    rules: list[str] = []
    for key, _ in counts.most_common():
        if key == "dense_text":
            rules.append("- Break dense paragraphs into short paragraphs, bullets, or finding/implication/action tables.")
        elif key == "table_fit":
            rules.append("- Keep tables narrow and purposeful; shorten headers, move long evidence/URLs to appendix, and use wrapped text.")
        elif key == "hierarchy_spacing":
            rules.append("- Use clear heading hierarchy and consistent spacing; avoid excessive horizontal rules.")
        elif key == "sequence_wrapping":
            rules.append("- Wrap protein or nucleotide sequences at 60-80 characters and place long sequences in appendix/code blocks.")
        elif key == "garbled_links":
            rules.append("- Inspect links, diagrams, and copied text for garbled characters; replace raw URLs with source names plus appendix links.")
        elif key == "client_readiness":
            rules.append("- Make client implications and recommended actions explicit for every major technical finding.")
        elif key == "scoring_professionalism":
            rules.append("- Replace star ratings or $ symbols with defined scoring scales or named cost/risk levels.")
    return rules


def _render_generated_skill(review_count: int, findings: dict[str, Any], source_files: list[Path]) -> str:
    rules = _rule_lines(findings)
    examples = findings.get("examples") or {}
    lines = [
        "---",
        "name: report-generation-feedback",
        "description: Automatically learned report-generation guidance from recent Hermes layout reviews.",
        "always: true",
        "---",
        "",
        "# Auto-Learned Report Generation Feedback",
        "",
        f"Generated after {review_count} cumulative layout review(s).",
        "",
        "Apply these lessons when generating CRO client-facing reports:",
        "",
        *rules,
        "",
        "## Recent Review Signals",
        "",
    ]
    for key, snippets in examples.items():
        lines.append(f"### {key}")
        for snippet in snippets[:3]:
            lines.append(f"- {snippet}")
        lines.append("")
    lines.extend(
        [
            "## Source Review Files",
            "",
            *[f"- {path}" for path in source_files[:10]],
            "",
            "Do not mention this auto-learning file to the user unless asked. Use it to improve the next report draft.",
        ]
    )
    return "\n".join(lines).strip() + "\n"


def maybe_optimize_report_skill(db_path: Path, *, threshold: int = DEFAULT_THRESHOLD) -> dict[str, Any]:
    if os.getenv("HERMES_REPORT_SKILL_AUTO_OPTIMIZE", "1") in {"0", "false", "False"}:
        return {"triggered": False, "reason": "disabled"}
    threshold = max(1, int(threshold or DEFAULT_THRESHOLD))
    with _connect(db_path) as conn:
        review_count = _review_count(conn)
        last_count = _last_trigger_count(conn)
        if review_count <= 0 or review_count < last_count + threshold:
            return {
                "triggered": False,
                "review_count": review_count,
                "last_trigger_count": last_count,
                "next_trigger_count": last_count + threshold,
            }
        source_files = _latest_review_files(limit=20)
        review_texts: list[str] = []
        used_files: list[Path] = []
        for path in source_files:
            try:
                review_texts.append(path.read_text(encoding="utf-8"))
                used_files.append(path)
            except OSError:
                continue
        findings = _extract_findings(review_texts)
        GENERATED_SKILLS_DIR.mkdir(parents=True, exist_ok=True)
        skill_path = GENERATED_SKILLS_DIR / "report-generation-feedback.md"
        skill_text = _render_generated_skill(review_count, findings, used_files)
        skill_path.write_text(skill_text, encoding="utf-8")
        conn.execute(
            """insert into agent_report_skill_optimizations
                (ts, review_count, trigger_count, generated_skill_path, source_reviews,
                 findings, status, metadata)
               values (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                time.time(),
                review_count,
                review_count,
                str(skill_path),
                json.dumps([str(path) for path in used_files], ensure_ascii=False),
                json.dumps(findings, ensure_ascii=False),
                "ok",
                json.dumps({"threshold": threshold}, ensure_ascii=False),
            ),
        )
        return {
            "triggered": True,
            "review_count": review_count,
            "generated_skill_path": str(skill_path),
            "findings": findings,
        }
