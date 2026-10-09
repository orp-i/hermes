---
name: cro-consulting-report
description: Client-facing CRO consulting report style for analysis deliverables, recommendations, executive summaries, and final PDF/DOCX reports.
triggers:
  - 报告
  - 文档
  - 咨询
  - 客户
  - CRO
  - deliverable
  - report
  - recommendation
  - executive summary
  - pdf
  - docx
---

# CRO Consulting Report Skill

When producing a final report for a CRO consulting client, write as a practical consulting deliverable.

## Required orientation

- Start from the client's decision question, not from theory.
- Prefer concrete findings, evidence, tables, and recommended actions.
- Avoid long textbook background unless it changes interpretation or action.
- State assumptions, data limitations, and confidence clearly.
- Make every major section answer: "So what should the client do next?"

## Recommended structure

1. Title and project context
2. Executive summary
   - 3-6 bullets with the most important findings
   - 2-4 immediate recommendations
3. Client question and scope
4. Data/materials reviewed
5. Methods actually performed
6. Key findings
   - Use tables for quantitative findings
   - Include exact counts, rates, thresholds, filenames, or sample IDs when available
7. Interpretation and implications
8. Recommendations
   - Prioritize by impact and feasibility
   - Distinguish immediate actions from follow-up work
9. Risks, limitations, and assumptions
10. Next-step workplan
11. Appendix with technical detail, commands, parameters, and generated files

## Historical Hermes report issues to avoid

Past generated protein/CRO reports tended to read like technical notebooks. Correct these patterns before final rendering:

- Do not start with long background sections. Start with an executive summary and client decision.
- Do not leave recommendations buried inside technical sections such as "表达体系建议" or "综合表达建议"; also include a dedicated "建议" section with prioritized actions.
- Always include "下一步" with concrete work items, owner/action, and expected output.
- Always include "风险与局限" even when evidence is strong.
- Break dense paragraphs into bullets or short paragraphs.
- For protein sequences, wrap long amino acid strings into 60-80 character lines and place them in code blocks or appendix.
- Avoid star ratings, "$/$$/$$$" symbols, vague "high/medium/low" without definitions. Use defined levels or numeric scoring.
- Avoid raw long URLs in tables. Use source names or short references; put long links in appendix.
- Every important technical finding should include a client implication and an action.

## CRO protein/reagent consulting pattern

For recombinant protein, antibody, assay, or reagent feasibility reports, include these client-facing tables when applicable:

- Decision summary: recommendation, confidence, rationale, immediate action.
- Expression strategy matrix: system, construct/domain, tag, expected solubility/folding, purification route, risk, recommendation.
- Risk register: risk, evidence, impact, mitigation, residual uncertainty.
- Next-step workplan: step, objective, input required, output, priority.

## Style rules

- Use concise business/scientific language.
- Prefer short paragraphs and tables over long prose.
- Avoid phrases like "in theory", "generally", "it is important to note" unless followed by a concrete implication.
- Do not overclaim. If evidence is insufficient, say what additional data would resolve the uncertainty.
- For generated files, reference the exact output filenames.

## Reference validation (mandatory before final render)

If the report cites academic literature (papers, reviews, clinical trials, preprints), you MUST call the `validate_references` tool BEFORE the final `render_report` pass. The validator searches the web for each citation and matches titles against known scholarly domains (PubMed, DOI, Nature, Cell, Science, biorxiv, arxiv, PLOS, Frontiers, Elsevier, Wiley, etc.).

For each cited paper supply at least a `title`; include `cite_text` (the exact citation text as it appears in the report), `authors`, `year`, and `doi`/`pmid` when known so the validator can disambiguate.

Handle the validator output as follows:

- `verified` — title matched a scholarly-domain result. Keep the citation; consider adding the matched URL.
- `verified_web` — title matched a real URL but NOT on a scholarly domain. Treat as weaker evidence; replace with a primary source if possible.
- `not_found` — REMOVE the citation. This is almost certainly a hallucination; do not ship it. If you cannot find a real replacement, drop the claim or qualify it ("unpublished observation").
- `mismatch` — title matched but year/author does not line up. Investigate; either correct the year/author or remove the citation.
- `unverifiable` — search failed or the citation lacked usable metadata. Annotate with `[unverified]` in the report so reviewers know.

Reference the audit JSON saved under `outputs/reference-validation-*.json` in the report's methods/appendix so reviewers can audit citation provenance.
