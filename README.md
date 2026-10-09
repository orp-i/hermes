# Hermes

A self-hosted research agent. You give it a question, it searches, runs code in a
sandboxed container, checks its own citations against the literature, and hands back
a PDF or DOCX report.

Hermes is built around two ideas that most agent frameworks leave to the operator.
The first is that an agent allowed to run shell commands needs a real isolation
boundary, not a subprocess, so tool calls never execute in the web container. The
second is that an agent writing a research report will invent references, so the
citations in a deliverable are verified mechanically before the file is rendered,
and the ones that fail the check are reported rather than silently kept.

Everything an operator needs to set, including model endpoints and API keys, is
edited from a web config page. There is no configuration to write by hand.

## What is in here

| Component | Role |
|---|---|
| `app/` | FastAPI portal: login, device approval, SSE chat streaming, admin and config pages |
| `agent/` | The agent loop, context compaction, provider routing with fallback, skill selection |
| `agent/tools/` | `bash`, `read_file`, `write_file`, `edit_file`, `ls`, `grep`, `find`, `web_search`, `validate_references`, `render_report`, `review_report_layout` |
| `llm_router/` | A local OpenAI-compatible gateway that maps route aliases to providers |
| `runner/` | Holds the Docker socket and executes tool calls in the sandbox container |
| `skills/` | Markdown skill definitions loaded into the system prompt on demand |
| `docs/` | Design notes, in Chinese, covering the runtime topology and the agent loop in detail |

## Design notes

**Tool calls do not run where the web server runs.** The agent has no shell. It
posts to a runner service, which reaches a long-lived sandbox container through
`docker exec`. The sandbox publishes no ports and is only addressable from the
runner; the repository itself is mounted read-only at `/code`, and the write tools
refuse paths under it. The runner is the only component holding `docker.sock`.

**Four timeout layers, each clamping the one inside it.** An overall wallclock
budget for the request, a per-tool timeout clamped to whatever remains of that
budget, an HTTP timeout on the runner call, and `timeout --signal=TERM` inside the
container. A request that runs out of budget emits a sentinel carrying the last
partial answer instead of dropping the connection.

**Citations are checked, not trusted.** `validate_references` searches for each
citation and matches titles against scholarly sources, returning a per-citation
status of verified, verified_web, not_found, mismatch or unverifiable, plus an
audit JSON. Across the report runs on my own deployment it has caught fabricated
references that read as entirely plausible.

**Providers are addressed by route, not by model name.** The agent asks the local
router for `chat`, `code`, `plan` or `long_context`; the router resolves that alias
to a provider from the config file. Re-pointing a task class at a different model is
a dropdown on the config page, not a code change. `FallbackProvider` chains
providers in order, and `TieredProvider` splits short from long requests.

**Context compaction keeps old turns as a summary.** When a session exceeds its
token budget the system prompt and recent messages are kept verbatim and older
messages are compacted, preferring a model-written summary and falling back to a
deterministic one. Every compaction is recorded in an audit table.

## Running it

Requires Docker with Compose. Four containers come up: the portal, the runner, the
sandbox and the LLM router.

```sh
git clone https://github.com/orp-i/hermes.git
cd hermes
./scripts/bootstrap-env.sh     # generates .env with inter-container tokens
docker compose up -d
```

The first start writes `config/hermes.yaml` with randomly generated access codes and
prints them once to the log. Read them with:

```sh
docker compose logs hermes | grep -A 4 'generated credentials'
```

Open the portal (`http://localhost:5020` by default), log in with the generated
admin code, and go to **/admin/config**. Set at minimum:

- `server.public_base_url` to the URL users actually open.
- One provider under the Provider YAML box: `base_url`, `api_key` and
  `default_model` for any OpenAI-compatible endpoint, whether a hosted API or a
  local vLLM or Ollama.

That is enough to hold a conversation. Optionally set a Tavily key and switch the
search backend to `tavily+duckduckgo`; DuckDuckGo needs no credential and is the
default. Saving the page takes effect on the next request and the next model call,
with no restart: every component re-reads the file when its mtime changes.

Non-admin users log in with the user access code, and each of their devices then
waits for admin approval on the device page.

`config/hermes.example.yaml` documents every key, including the few the page does
not expose. Environment variables of the same name still override the file, which is
useful under an orchestrator; the older `model.env` JSON is read last and only kept
so existing deployments keep working.

## Security notes

`config/hermes.yaml`, `.env` and the SQLite database are gitignored and must stay
that way: the first two hold credentials after you configure them. The sandbox runs
model-authored shell commands, so treat it as hostile to its host. The bash tool is
restricted to an allowlist under `tools.allowed_commands`, but that is a guardrail
against accidents, not a containment boundary. Do not expose the portal to the
internet without a reverse proxy terminating TLS, and note that the generated access
codes are shared secrets, not per-user accounts.

## State of the project

Working and used in production on my own deployment: the agent loop, the sandbox
isolation, report generation to PDF and DOCX, citation validation, provider routing
and fallback, context compaction, device approval.

Known gaps, in the order I would address them: there is no retry with backoff on a
provider error, only sequential fallback to the next provider; there is no evaluation
set, so changes to the prompt or the loop are judged by reading transcripts; the
access model is shared codes rather than accounts; and the skill router's cache is
in-process, so it is cold for every replica.

`pi-main/` may appear in a working copy of this repository. It is a reference copy of
the upstream [pi agent harness](https://github.com/earendil-works/pi) (MIT), kept for
comparison during design work and excluded from the repository. None of it is mine.

## Licence

MIT, see [LICENSE](LICENSE).
