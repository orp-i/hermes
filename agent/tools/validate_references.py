"""Validate report references by web search.

The validator does NOT call NCBI directly (NCBI rate-limits / blocks many shared
egress IPs, including the proxies available to this deployment). Instead it
issues a web search per reference via Hermes' existing search backend
(`agent.search` — Tavily + DuckDuckGo) and checks whether at least one result
on a known scholarly domain (PubMed / DOI / Nature / Cell / biorxiv / ...)
has a title that fuzzy-matches the citation.

Outcomes per reference:
- `verified`     — title matched a scholarly-domain result. URL recorded.
- `verified_web` — title matched a non-scholarly result (still a real URL, but
                   downgrade the trust signal).
- `not_found`    — search ran but no result's title matched the citation.
- `mismatch`     — title matched, but cited year / first author diverges from
                   what the search result claims.
- `unverifiable` — search backend itself failed, or the citation lacks the
                   minimum fields needed to query (no title and no doi).

The tool is non-blocking. The agent must surface non-verified entries to the
user before delivering the final report.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ..sandbox import SANDBOX_WORKSPACE_ROOT
from ..search import get_backend

DEFAULT_PER_REF_TIMEOUT = float(os.getenv("HERMES_REF_VALIDATE_TIMEOUT_SEC", "12"))
DEFAULT_MAX_RESULTS = int(os.getenv("HERMES_REF_VALIDATE_MAX_RESULTS", "6"))
TITLE_SIMILARITY_THRESHOLD = float(os.getenv("HERMES_REF_VALIDATE_TITLE_THRESHOLD", "0.6"))

# Hostnames considered authoritative scholarly sources. Matched as suffix —
# subdomains like `journals.plos.org` or `www.nature.com` will hit `plos.org`
# and `nature.com` respectively.
SCHOLARLY_DOMAINS: set[str] = {
    "pubmed.ncbi.nlm.nih.gov", "ncbi.nlm.nih.gov", "pmc.ncbi.nlm.nih.gov", "europepmc.org",
    "doi.org", "dx.doi.org", "crossref.org", "openalex.org", "semanticscholar.org",
    "nature.com", "science.org", "sciencemag.org",
    "cell.com", "sciencedirect.com", "elsevier.com",
    "nejm.org", "thelancet.com", "bmj.com",
    "pnas.org", "plos.org", "plosone.org", "plosbiology.org", "plosmedicine.org",
    "frontiersin.org", "biorxiv.org", "medrxiv.org", "arxiv.org", "chemrxiv.org",
    "springer.com", "link.springer.com", "springeropen.com",
    "onlinelibrary.wiley.com", "tandfonline.com",
    "oup.com", "academic.oup.com",
    "mdpi.com", "cambridge.org", "jstor.org",
    "rsc.org", "pubs.acs.org", "aip.scitation.org", "iopscience.iop.org",
    "asm.org", "jbc.org", "asbmb.org",
    "embopress.org", "rupress.org",
}

# Common search-result title decorations to strip before comparison.
_TITLE_SUFFIX_RE = re.compile(
    r"\s*[|\-–—·•]\s*(?:"
    r"pubmed|pmc|crossref|doi|nature|science|cell|the lancet|nejm|pnas|plos|frontiers|biorxiv|medrxiv|arxiv|springer|wiley|elsevier|sciencedirect|sci[-\s]?hub|google scholar|semantic scholar"
    r").*$",
    re.IGNORECASE,
)


SCHEMA = {
    "type": "function",
    "function": {
        "name": "validate_references",
        "description": (
            "Validate that each citation in a report is real by searching the web "
            "and matching titles against scholarly sources (PubMed, DOI, Nature, Cell, "
            "biorxiv, arxiv, etc.). Call this BEFORE rendering the final client "
            "deliverable when the report cites academic literature. For each citation "
            "supply at least a 'title'; 'authors', 'year', 'doi' improve disambiguation. "
            "Returns per-citation status (verified / verified_web / not_found / mismatch / "
            "unverifiable) plus a summary and an audit JSON under outputs/. "
            "The check is non-blocking: entries flagged as not_found / mismatch must be "
            "removed or corrected; unverifiable entries (search failed or no title) must "
            "be annotated as [unverified]."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "references": {
                    "type": "array",
                    "minItems": 1,
                    "description": "List of citations to validate.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "cite_text": {
                                "type": "string",
                                "description": "Citation as it appears in the report (shown back to user).",
                            },
                            "title": {
                                "type": "string",
                                "description": "Paper title — primary key for search.",
                            },
                            "authors": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "First-author surname or all authors.",
                            },
                            "year": {
                                "type": "integer",
                                "description": "Publication year.",
                            },
                            "doi": {
                                "type": "string",
                                "description": "DOI if known — included in the query for better hit rate.",
                            },
                            "pmid": {
                                "type": "string",
                                "description": "PubMed ID — included in the query for better hit rate.",
                            },
                        },
                    },
                },
                "max_results_per_ref": {
                    "type": "integer",
                    "description": "Search results to inspect per reference. Default 6, max 10.",
                    "minimum": 1,
                    "maximum": 10,
                },
            },
            "required": ["references"],
        },
    },
}


def _normalise(text: str) -> str:
    """Lowercase + collapse non-alphanumerics to single spaces."""
    return re.sub(r"[\W_]+", " ", (text or "")).strip().lower()


def _strip_decoration(title: str) -> str:
    """Drop trailing ' - Nature', ' | PubMed', etc. introduced by search engines."""
    return _TITLE_SUFFIX_RE.sub("", title or "").strip()


def _title_similarity(a: str, b: str) -> float:
    """Token-set Jaccard with substring shortcut. Range [0, 1]."""
    na, nb = _normalise(a), _normalise(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    if na in nb or nb in na:
        return 0.95
    ta, tb = set(na.split()), set(nb.split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _hostname(url: str) -> str:
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""
    return host.removeprefix("www.")


def _is_scholarly(url: str) -> bool:
    host = _hostname(url)
    if not host:
        return False
    for domain in SCHOLARLY_DOMAINS:
        if host == domain or host.endswith("." + domain):
            return True
    return False


def _build_query(ref: dict) -> str:
    parts: list[str] = []
    title = (ref.get("title") or "").strip()
    if title:
        parts.append(f'"{title}"')
    if ref.get("doi"):
        parts.append(str(ref["doi"]).strip())
    if ref.get("pmid"):
        parts.append(str(ref["pmid"]).strip())
    authors = ref.get("authors") or []
    if authors:
        first = str(authors[0]).strip()
        if first:
            parts.append(first.split()[0])
    if ref.get("year"):
        parts.append(str(ref["year"]))
    return " ".join(parts).strip()


def _extract_year_from_text(text: str) -> int | None:
    match = re.search(r"\b(19|20)\d{2}\b", text or "")
    return int(match.group(0)) if match else None


def _classify(ref: dict, results: list[dict]) -> dict[str, Any]:
    """Pick the best matching result and label the citation."""
    cited_title = ref.get("title") or ""
    cited_year = ref.get("year")
    best_score = 0.0
    best_match: dict[str, Any] | None = None

    for r in results:
        result_title = _strip_decoration(r.get("title") or "")
        score = _title_similarity(cited_title, result_title)
        if score > best_score:
            best_score = score
            best_match = {**r, "_clean_title": result_title, "_score": score}

    if not best_match or best_score < TITLE_SIMILARITY_THRESHOLD:
        return {
            "status": "not_found",
            "reason": (
                f"Searched and inspected {len(results)} result(s); no title matched "
                f"the citation closely enough (best similarity {best_score:.2f} < "
                f"{TITLE_SIMILARITY_THRESHOLD})."
            ),
            "best_candidate": (
                {
                    "title": best_match.get("title"),
                    "url": best_match.get("url"),
                    "similarity": round(best_score, 3),
                }
                if best_match else None
            ),
        }

    scholarly = _is_scholarly(best_match.get("url") or "")
    snippet = best_match.get("snippet") or ""
    result_year = _extract_year_from_text(snippet) or _extract_year_from_text(best_match.get("_clean_title") or "")

    base = {
        "matched_title": best_match.get("_clean_title"),
        "matched_url": best_match.get("url"),
        "similarity": round(best_score, 3),
        "scholarly": scholarly,
        "snippet": (snippet or "")[:240],
    }
    if result_year:
        base["search_year"] = result_year

    if cited_year and result_year and abs(int(cited_year) - int(result_year)) > 1:
        return {
            "status": "mismatch",
            "reason": (
                f"Title matched (similarity {best_score:.2f}) but cited year "
                f"{cited_year} != year found near result ({result_year})."
            ),
            **base,
        }

    return {
        "status": "verified" if scholarly else "verified_web",
        **base,
    }


def _cite_label(ref: dict, index: int) -> str:
    return (
        (ref.get("cite_text") or "").strip()
        or (ref.get("title") or "").strip()
        or f"reference #{index}"
    )[:200]


def run(args: dict[str, Any], *, session_id: str) -> dict[str, Any]:
    refs = args.get("references")
    if not isinstance(refs, list) or not refs:
        return {
            "stdout": "",
            "stderr": "`references` must be a non-empty list.",
            "exit_code": 2,
            "duration_ms": 0,
        }
    max_results = int(args.get("max_results_per_ref") or DEFAULT_MAX_RESULTS)
    max_results = max(1, min(max_results, 10))

    try:
        backend = get_backend()
    except Exception as exc:
        return {
            "stdout": "",
            "stderr": f"Search backend unavailable: {exc}",
            "exit_code": 1,
            "duration_ms": 0,
        }

    start = time.time()
    records: list[dict[str, Any]] = []
    for idx, ref in enumerate(refs, 1):
        if not isinstance(ref, dict):
            records.append({"index": idx, "cite": str(ref)[:200], "status": "unverifiable", "reason": "Not an object."})
            continue
        label = _cite_label(ref, idx)
        title = (ref.get("title") or "").strip()
        if not title and not ref.get("doi") and not ref.get("pmid"):
            records.append({
                "index": idx, "cite": label, "status": "unverifiable",
                "reason": "Need at least 'title', 'doi', or 'pmid' to search.",
            })
            continue

        query = _build_query(ref) or title
        try:
            results = backend.search(query, max_results=max_results)
        except Exception as exc:
            records.append({
                "index": idx, "cite": label, "status": "unverifiable",
                "reason": f"Search failed: {exc}", "query": query,
            })
            continue

        classification = _classify(ref, results or [])
        records.append({"index": idx, "cite": label, "query": query, **classification})

    counts = {"verified": 0, "verified_web": 0, "not_found": 0, "mismatch": 0, "unverifiable": 0}
    for rec in records:
        status = rec.get("status", "unverifiable")
        counts[status] = counts.get(status, 0) + 1

    icon_for = {
        "verified": "[OK]",
        "verified_web": "[OK-web]",
        "not_found": "[MISSING]",
        "mismatch": "[MISMATCH]",
        "unverifiable": "[UNVERIFIABLE]",
    }
    lines = [
        f"Reference validation: {len(records)} reference(s) checked via web search.",
        f"  verified (scholarly):  {counts['verified']}",
        f"  verified (web only):   {counts['verified_web']}",
        f"  not_found:             {counts['not_found']}",
        f"  mismatch:              {counts['mismatch']}",
        f"  unverifiable:          {counts['unverifiable']}",
        "",
    ]
    for rec in records:
        icon = icon_for.get(rec.get("status"), "[UNVERIFIABLE]")
        lines.append(f"[{rec['index']}] {icon} {rec.get('cite','')}")
        if rec.get("query"):
            lines.append(f"    query: {rec['query']}")
        if rec.get("status") in {"verified", "verified_web"} and rec.get("matched_url"):
            lines.append(
                f"    matched: {rec.get('matched_title','')[:140]} ({rec.get('similarity',0):.2f})"
            )
            lines.append(f"    url:     {rec['matched_url']}")
        elif rec.get("status") != "verified" and rec.get("reason"):
            lines.append(f"    -> {rec['reason']}")
            if rec.get("best_candidate"):
                bc = rec["best_candidate"]
                lines.append(
                    f"    best candidate: {(bc.get('title') or '')[:120]} "
                    f"({(bc.get('similarity') or 0):.2f}) — {bc.get('url','')}"
                )

    if counts["not_found"] or counts["mismatch"]:
        lines.append("")
        lines.append(
            "Action: REMOVE or correct any entry flagged `not_found` / `mismatch` "
            "before rendering the final report. A `not_found` reference is almost "
            "certainly a hallucination."
        )
    if counts["unverifiable"]:
        lines.append(
            "Note: `unverifiable` items either lack a usable title/doi/pmid or the "
            "search backend failed. Re-run with better metadata, or annotate the "
            "citation `[unverified]` in the report."
        )
    if counts["verified_web"]:
        lines.append(
            "Note: `verified_web` entries matched a real URL but NOT on a known "
            "scholarly domain (PubMed/DOI/Nature/Cell/biorxiv/...). Treat them as "
            "weaker evidence; if possible, replace with a primary source."
        )

    audit_rel: str | None = None
    try:
        outputs = Path(SANDBOX_WORKSPACE_ROOT) / "sessions" / session_id / "outputs"
        outputs.mkdir(parents=True, exist_ok=True)
        audit_path = outputs / f"reference-validation-{int(time.time())}.json"
        audit_path.write_text(
            json.dumps({"counts": counts, "results": records}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        audit_rel = f"outputs/{audit_path.name}"
        lines.append("")
        lines.append(f"Audit saved: {audit_rel}")
    except OSError:
        pass

    return {
        "stdout": "\n".join(lines),
        "stderr": "",
        "exit_code": 0,
        "duration_ms": int((time.time() - start) * 1000),
        "validation": {"counts": counts, "results": records, "audit_path": audit_rel},
    }
