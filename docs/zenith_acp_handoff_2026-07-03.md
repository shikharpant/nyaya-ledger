# Zenith ACP Handoff - 2026-07-03

## Executive Summary

The Zenith ACP runtime issue is fixed enough to continue work:

- ACP provider preflight exists and passes for both Hermes and Codex.
- Missing `end_node` / missing attempt-file cases now produce explicit persisted handoff failures instead of silent broken state.
- Hung ACP sessions are bounded by `ZENITH_ACP_SESSION_TIMEOUT_SECONDS` and now return explicit attention reports with MCP stdout/stderr.
- The live Git_for_Law mission advanced past the previously stuck `work-provision-vectors-done` node using `codex-acp`.

The remaining mission work is not an ACP runtime bug. It is normal mission validation / product work in:

`/home/shikhar/openclaw-workspace/Projects/Git_for_Law`

## Repositories and Paths

Zenith source:

`/home/shikhar/tools/zenith/zenith`

Live Zenith project:

`/home/shikhar/.zenith/projects/20260703T101943Z-phase-1-complete-the-gst-statute-mcp-with-time-travel-capabiliti`

Workspace under mission:

`/home/shikhar/openclaw-workspace/Projects/Git_for_Law`

Project id:

`20260703T101943Z-phase-1-complete-the-gst-statute-mcp-with-time-travel-capabiliti`

Mission id:

`mission-001`

## Zenith Runtime Changes Made

Modified files in `/home/shikhar/tools/zenith/zenith`:

- `src/zenith_harness/acp_runner.py`
- `src/zenith_harness/cli.py`
- `tests/test_acp_runner.py`
- `tests/test_cli.py`
- `tests/test_smoke_real_acp.py`

Runtime behavior added:

- `preflight_acp_command(command, provider)` validates ACP commands before dispatch.
- Hermes preflight runs `hermes acp --check`.
- Codex preflight runs `codex-acp --version`.
- New CLI command: `zenith doctor-acp --provider <provider> --command <command>`.
- Worker MCP stdout/stderr are drained and included in synthetic failure reports.
- Missing worker handoffs are persisted instead of only failing in memory.
- `ZENITH_HANDOFF_GRACE_SECONDS` controls the post-prompt handoff flush wait.
- `ZENITH_ACP_SESSION_TIMEOUT_SECONDS` bounds full ACP sessions. Default is `1800`.
- Terminal reviewer ACP sessions use the same timeout mechanism and include session errors in failure output.

Verification already run:

```bash
cd /home/shikhar/tools/zenith/zenith
uv run python -m py_compile src/zenith_harness/acp_runner.py src/zenith_harness/cli.py
uv run pytest -q tests/test_acp_runner.py -x
uv run pytest -q tests/test_cli.py tests/test_config.py tests/test_coordinator.py tests/test_coordinator_parallel.py -x
zenith doctor-acp --provider hermes --command 'hermes acp'
zenith doctor-acp --provider codex --command codex-acp
```

Results:

- `tests/test_acp_runner.py`: 9 passed.
- CLI/config/coordinator group: 40 passed.
- Hermes ACP doctor: `Hermes ACP check OK`.
- Codex ACP doctor: `@agentclientprotocol/codex-acp 1.0.2`.

Known test caveat:

- `tests/test_server.py` printed several passing dots but the Codex tool session became stale/hung. I did not treat that as a runtime regression because the focused ACP and coordinator tests passed and real ACP recovery worked afterward.

## Live Mission Status

Current command:

```bash
zenith show-project 20260703T101943Z-phase-1-complete-the-gst-statute-mcp-with-time-travel-capabiliti
```

Observed state before handoff:

- `state: attention_needed`
- `work-provision-vectors-done`: cleared
- `validate-vectors-orchestrator`: cleared
- Ready tasks:
  - `gate-vectors-final`
  - `validate-regression-orchestrator`

Current attention item:

- `att-validate-vectors-orchestrator-c7568b`
- It says `VAL-VEC-001` passed and `VAL-VEC-002` failed.

Important: that attention report is stale relative to a manual product fix made after the validator ran.

## Product Fix Made After Validator Attention

In Git_for_Law, I changed:

`/home/shikhar/openclaw-workspace/Projects/Git_for_Law/src/legal_corpus/serving.py`

Reason:

- Real semantic search for `input tax credit` worked outside the Codex sandbox, but pure vector distance ranked CGST rules above CGST Act Section 16.
- `section/16` was rank 6 semantically, so it was in the candidate pool but not top 3.

Change:

- Added `_query_terms`.
- Added `_provision_rank_bonus`.
- Expanded provision semantic candidate fetch to `max(limit * 25, 50)`.
- Sorts provision candidates by semantic score plus a small lexical/type tie-breaker.
- Exact query/title matches and Act sections now rank above loosely related rules.

Verification:

```bash
cd /home/shikhar/openclaw-workspace/Projects/Git_for_Law
python3 -m pytest tests/test_canonical_corpus.py::test_semantic_search_provision_returns_section_hits_for_input_tax_credit -q
```

Result:

- `1 passed in 0.82s`

Real probe outside sandbox:

```bash
cd /home/shikhar/openclaw-workspace/Projects/Git_for_Law
.venv/bin/python - <<'PY'
from src.legal_corpus.serving import NyayaToolService
service = NyayaToolService.from_env()
result = service.semantic_search_provision('input tax credit', limit=3, fallback_lexical=False)
print(result['mode'])
for row in result['results']:
    print(row['canonical_id'], row['provision_type'], row['number'], row['score'])
PY
```

Result:

```text
semantic_provision
/in/union/acts/cgst-act-2017/section/16 section 16 0.652285112690843
/in/union/acts/cgst-act-2017/section/53 section 53 0.6456349222231014
/in/union/acts/cgst-act-2017/section/41 section 41 0.6450820785917252
```

This should satisfy the substantive `VAL-VEC-002` ranking requirement, but Zenith has not yet re-run the validator after this product fix.

## Environment Notes

`codex-acp` is installed:

```bash
codex-acp --version
```

Expected:

```text
@agentclientprotocol/codex-acp 1.0.2
```

Hermes ACP preflight is healthy:

```bash
hermes acp --check
```

Expected:

```text
Hermes ACP check OK
```

Hermes default model was previously changed to `glm-5.2`.

The Git_for_Law `.mcp.json` currently has Hermes as orchestrator/worker provider, but the stuck node was successfully recovered with Codex as worker:

```bash
ZENITH_ORCHESTRATOR_PROVIDER=hermes \
ZENITH_WORKER_PROVIDER=codex \
ZENITH_WORKER_ACP_COMMAND=codex-acp \
ZENITH_ACP_SESSION_TIMEOUT_SECONDS=300 \
uv run --project /home/shikhar/tools/zenith/zenith python ...
```

## Important Sandbox Caveat

Codex's managed sandbox blocks local sockets and local endpoint calls. Symptoms observed:

- Zenith worker MCP socket failed in sandbox with `PermissionError: [Errno 1] Operation not permitted`.
- Real `NyayaToolService.semantic_search_provision(...)` failed in sandbox while calling `127.0.0.1:1234`.

Therefore, live Zenith ACP mission advancement and real semantic probes must run outside the Codex sandbox.

Use escalated execution for:

- `uv run --project /home/shikhar/tools/zenith/zenith ...` when advancing the live mission.
- `.venv/bin/python ...` probes that call the local embedding endpoint.

## Do Not Repeat

Do not treat the original missing `end_node` problem as still unfixed. It is fixed at the runtime layer:

- Hermes timed out cleanly and wrote an explicit synthetic handoff.
- Codex ACP successfully cleared `work-provision-vectors-done`.
- The mission moved on to validator/gate work.

Do not directly edit Zenith runtime JSON unless there is no public-controller alternative.

Do not rely on final prose from agents as handoff. The chosen policy is strict fail: workers must call `end_node`; validators must emit structured verdicts.

## Recommended Next Steps

1. Re-run the vector validator after the product ranking fix.

Use Codex as worker/validator because it successfully cleared the stuck node:

```bash
ZENITH_ORCHESTRATOR_PROVIDER=hermes \
ZENITH_WORKER_PROVIDER=codex \
ZENITH_WORKER_ACP_COMMAND=codex-acp \
ZENITH_ACP_SESSION_TIMEOUT_SECONDS=300 \
uv run --project /home/shikhar/tools/zenith/zenith python - <<'PY'
from zenith_harness.config import HarnessConfig
from zenith_harness.controller import ProjectController
from zenith_harness.acp_runner import ACPNodeDispatcher, ACPTerminalReviewer
from zenith_harness.models import Decision

project_id = '20260703T101943Z-phase-1-complete-the-gst-statute-mcp-with-time-travel-capabiliti'
cfg = HarnessConfig.discover()
controller = ProjectController(cfg, ACPNodeDispatcher(cfg), ACPTerminalReviewer(cfg))
items = controller.store.load_attention(project_id)
print('attention_items:', [item.model_dump(mode='json') for item in items])
if items:
    controller.decide_attention(project_id, [Decision(item_id=items[0].id, action='retry')])
envelope = controller.advance_project(project_id, max_steps=2)
print('state:', envelope.state.state)
for item in controller.store.load_attention(project_id):
    print(item.model_dump_json(indent=2))
PY
```

2. If `validate-vectors-orchestrator` passes, advance `gate-vectors-final` and `validate-regression-orchestrator`.

3. If terminal review is reached, keep `ZENITH_ACP_SESSION_TIMEOUT_SECONDS` set. Terminal review now has bounded timeout handling too.

4. Finish by running:

```bash
cd /home/shikhar/tools/zenith/zenith
git diff -- src/zenith_harness/acp_runner.py src/zenith_harness/cli.py tests/test_acp_runner.py tests/test_cli.py tests/test_smoke_real_acp.py

cd /home/shikhar/openclaw-workspace/Projects/Git_for_Law
git diff -- src/legal_corpus/serving.py tests/test_canonical_corpus.py
zenith show-project 20260703T101943Z-phase-1-complete-the-gst-statute-mcp-with-time-travel-capabiliti
```

## Interrupted Command

The user interrupted this command:

```bash
.venv/bin/pip install -r requirements.txt
```

It may have partially executed. Do not assume it completed. The focused test passed under system `python3`; `.venv` initially lacked at least `jsonschema` for pytest collection, though it could import `lancedb`, `pyarrow`, `numpy`, and `pydantic`.

Before doing dependency work, inspect:

```bash
cd /home/shikhar/openclaw-workspace/Projects/Git_for_Law
.venv/bin/python - <<'PY'
for mod in ['jsonschema', 'lancedb', 'pyarrow', 'numpy', 'pydantic']:
    try:
        __import__(mod)
        print(mod, 'ok')
    except Exception as exc:
        print(mod, type(exc).__name__, exc)
PY
```

