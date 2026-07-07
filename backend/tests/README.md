# Synapse backend test suite

Comprehensive tests for the FastAPI backend: all app routes, `api_v1`, and
`api_v2`, with deep coverage of the four core flows — **agent chat**, **agent
chat stream**, **orchestration chat**, **orchestration chat stream** — in both
v1 and v2.

## Running

Always invoke from the **repo root** (pytest reads config from `pyproject.toml`):

```bash
pip install -r backend/requirements.txt -r backend/requirements-test.txt

# Fast suite (this is the deploy gate) — excludes stress + integration:
python -m pytest

# With reports:
python -m pytest --html=report.html --cov=core --cov-report=html

# Stress suite (5-90s fake-LLM latency; writes stress-reports/):
python -m pytest backend/tests/stress -m stress
#   bound it locally: SYNAPSE_FAKE_LLM_DELAY_MAX=2 SYNAPSE_STRESS_TOTAL=8 ...

# Integration (needs real Redis+Postgres — see nightly.yml):
python -m pytest backend/tests/integration -m integration
```

## The fake LLM (no keys, no network)

Every real LLM call routes through `generate_response` in
`backend/core/llm_providers.py`. The autouse `fake_llm` fixture
(`conftest.py`) replaces it at all three binding sites with a **scriptable**
async fake that returns a plain string — exactly the production contract, so the
real ReAct loop and orchestration engine run unchanged.

Delay profiles are env-driven and read per call:

| var | default | stress |
| --- | --- | --- |
| `SYNAPSE_FAKE_LLM_DELAY_MIN` | 0 | 5 |
| `SYNAPSE_FAKE_LLM_DELAY_MAX` | 0 | 90 |
| `SYNAPSE_FAKE_LLM_DELAY_PROB` | 0 | 0.3 |

So the gate runs instantly while the stress suite simulates real latency where
*some* calls are slow.

## Layout

```
_fakes/        scriptable LLM, data seeders, fake Redis-stream + Postgres helpers
unit/          pure functions + REAL engine runs (parsing, providers, orch steps, cache)
api_app/       internal routes (/chat, /chat/stream, orchestrations, agents, smoke)
api_v1/        /api/v1 chat + orchestration (sync & SSE) + auth
api_v2/        /api/v2 contract (queue mocked) + SSE via fakeredis + status/events
stress/        concurrent load under the 5-90s profile; emits stress-reports/
integration/   V2 end-to-end against real infra (nightly; auto-skips without it)
install/       packaging/version-sync/import guards (gate) + CI wheel-install job
```

## Coverage

Run: `python -m pytest --cov=core --cov-report=html` → open `htmlcov/index.html`.

Coverage is **scoped** (see `[tool.coverage.run] omit` in `pyproject.toml`): modules
that are inherently external/integration and can't be meaningfully unit-tested are
excluded from the denominator and instead exercised by the nightly integration job
(`.github/workflows/nightly.yml`) and manual/e2e testing. These are: the ARQ worker
runtime (`scale/worker*`, `scale/sync`, `scale/db`, `scale/heartbeat`), live chat-bot
adapters (`messaging/*`), the MCP subprocess client (`mcp_client`), the cron scheduler,
the native/builder subprocess tooling, the app lifespan wiring (`server.py`), scale
admin, vector memory (`memory.py`, chromadb + embeddings), and AWS S3.

Within `llm_providers.py`, the dispatch, routing, helpers, and the HTTP-based providers
(OpenAI/Anthropic/Grok/DeepSeek/OpenAI-compatible/Ollama) are unit-tested with mocked
transport; the remaining provider bodies (Gemini/Bedrock/CLI/HuggingFace) make real
SDK/subprocess calls and are integration-tested.

CI enforces a coverage floor (`--cov-fail-under`) as a regression gate.

## Testing strategy

- **Route-contract tests** patch the engine (`run_react_loop` /
  `OrchestrationEngine`) with canned event streams to deterministically verify
  every HTTP handler + SSE-serialization branch.
- **Engine-integration tests** drive the *real* `run_agent_step` and
  `OrchestrationEngine` with the fake LLM, proving the interception works
  through the whole stack.
- Each test gets an isolated, sandboxed `SYNAPSE_DATA_DIR`; stores are reset
  between tests (see the `_isolate_data` autouse fixture).

## CI

- `.github/workflows/ci.yml` — runs the fast suite (Py 3.11/3.12/3.13) + a wheel
  install smoke on every PR/push; uploads html/coverage/junit reports.
- `.github/workflows/publish.yml` — a `gate` job runs the suite + version/tag
  check; **all** publish jobs `needs: gate`, so a failure blocks the release.
- `.github/workflows/nightly.yml` — stress + real-infra integration.
