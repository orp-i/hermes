---
name: code-review
description: Review code in the read-only /code mount or in the session work/ dir. Trigger on requests to look at, review, audit, or check code.
triggers:
  - 代码
  - review
  - 审查
  - 检查代码
  - 看一下代码
  - look at the code
  - 报错
  - 这段代码
---

# Code Review Skill

When the user asks you to review code, follow this workflow:

1. **Locate the code first.**
   - If the user mentions a path under `/code`, that is the read-only host hermes project. Use `bash` with `ls /code` and `read_file` to inspect it.
   - If the user uploaded a file, it lives at `/workspace/sessions/<id>/uploads/`. Run `ls ../uploads/` from your default `work/` cwd to list it.
   - If unsure, ask which file or directory.

2. **Survey before reading line-by-line.**
   - For directories, run `tree -L 2 <path>` or `ls -la <path>` first.
   - For unfamiliar files, `wc -l` and `head -n 50` before full read.
   - Use `rg` (ripgrep) for any cross-file search; it is preinstalled in the sandbox.

3. **Review focus (in order):**
   - Correctness bugs and logic errors
   - Security issues (command injection, path traversal, SSRF, secrets in code)
   - Concurrency / race conditions
   - Error handling gaps
   - Code style and clarity (only mention if material)

4. **Report format:**
   - Group findings by severity: Critical / Major / Minor.
   - For each, cite `file:line` and quote the exact problematic snippet.
   - Suggest a concrete fix, not just "consider improving X".

Do **not** modify files under `/code` — it is read-only. If a fix is requested, write the patched version to `outputs/` so the user can download it.
