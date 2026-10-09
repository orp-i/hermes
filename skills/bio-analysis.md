---
name: bio-analysis
description: Bioinformatics analysis workflow. Trigger on requests involving FASTA, FASTQ, BAM, VCF, BED, sequence alignment, variant calling, or biological sequence analysis.
triggers:
  - fasta
  - fastq
  - bam
  - vcf
  - bed
  - 生信
  - 测序
  - 比对
  - 序列
  - alignment
  - variant
  - genome
  - sequencing
  - biopython
  - pysam
---

# Bioinformatics Skill

The sandbox has the following tools preinstalled:

**Command-line:**
- `samtools`, `bcftools` — BAM/SAM/CRAM and VCF/BCF manipulation
- `bwa`, `bowtie2`, `minimap2` — sequence aligners
- `bedtools` — interval operations
- `seqkit` — FASTA/FASTQ Swiss army knife
- `tabix` — index and query bgzipped files

**Python:**
- `biopython` (Bio.SeqIO, Bio.Align, etc.)
- `pysam` — BAM/VCF programmatic access
- `pyfaidx` — random access to FASTA
- `pyvcf3` — VCF parsing
- `scikit-bio`, `pybedtools`, `gffutils`, `mygene`
- standard data stack: `numpy`, `pandas`, `scipy`, `matplotlib`, `seaborn`

## Workflow

1. **Inspect inputs first.** Before processing:
   - `seqkit stats <file>` for FASTA/FASTQ
   - `samtools view -H <bam>` for BAM headers
   - `bcftools view -h <vcf>` for VCF headers
   - `head` and `wc -l` for plain text formats

2. **Stage data correctly.**
   - User uploads land in `../uploads/` (relative to your `work/` cwd).
   - Do not modify files in `../uploads/`; copy or symlink into `work/` for processing.
   - Write all results the user should download to `../outputs/` with descriptive filenames.

3. **For long-running steps,** prefer command-line tools over pure-Python loops (samtools/bcftools/bwa are orders of magnitude faster).

4. **For plots,** save to `../outputs/<name>.png` with `dpi=120` minimum, and explicitly call `plt.close()` after saving.

5. **State persists** across calls: pip packages and intermediate files in `work/` survive between turns. Use this to avoid re-running expensive steps.

6. **Report results** with concrete numbers (read counts, mapping rates, variant counts, etc.) and link to the output files you produced.

## Client-facing CRO deliverables

When the user asks for a report, PDF, DOCX, consulting document, product/reagent feasibility analysis, or client-facing recommendation:

- Structure the output as a CRO consulting deliverable, not a literature review.
- Put the client decision and recommended action near the top.
- Include explicit sections for 执行摘要, 建议, 风险与局限, and 下一步.
- For recombinant protein/expression reports, include a strategy matrix comparing expression systems, construct choices, tags, purification, risks, and recommended next experiments.
- Convert technical observations into client implications. Example: "No signal peptide" should lead to an implication such as "secreted mammalian expression is not the default route unless a heterologous signal peptide is engineered."
- Keep long sequences and command details in an appendix; wrap sequences at 60-80 characters.
- Any literature citation must be checked via the `validate_references` tool before the final render pass. The validator searches the web and matches titles against scholarly domains. Remove or correct entries marked `not_found` / `mismatch`; flag `unverifiable` entries with `[unverified]`.
