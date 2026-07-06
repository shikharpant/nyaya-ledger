# HANDOFF - Service Checkpoint Reproducibility Closure

Date: Mon Jul 6 2026

## Current State

The previous follow-up Zenith project is no longer stuck. Direct inspection on
Jul 6 2026 with `inspect_project` reports:

- Project: `20260705T055346Z-follow-up-mission-after-done-project-20260703t101943z-phase-1-co`
- State: `done`
- DAG: 36 tasks total, 26 cleared, 10 superseded

This file supersedes the earlier stale handoff text that recommended editing
Zenith runtime state. No runtime-state patch is needed for that old project.

## Active Follow-Up

New Zenith project:

- Project: `20260706T145403Z-make-the-service-checkpoint-100-closure-reproducible-documented-`
- State at handoff update: `mission_running`
- Mission id: `mission-001`
- Mission: make service checkpoint 100% closure reproducible, documented, and
  commit-ready.

## Product State

Service checkpoint closure is 100% at all three dates:

| Checkpoint | Closure |
| --- | --- |
| `svc_2019-04-01` | 40/40 |
| `svc_2024-10-24` | 41/41 |
| `svc_2025-03-31` | 41/41 |

The materializer fixes are in
`src/legal_corpus/rate_schedule_materializer.py`:

1. Keep substantive inline `Provided that...` service-rate conditions instead
   of treating them as leaked column text.
2. Extend item substitution spans when replacement text restates sequential
   sibling markers.
3. Skip `RATE_INSERT_WORDS` events anchored only to bare item markers like
   `(i)` or `(a)` to avoid cross-serial contamination.

## Reproducibility Packaging

Runtime inputs under `derived/version_history/rate-schedules/` are still
gitignored. The clean-checkout proof path is the fixture package below; these
non-ignored files must be included in the service checkpoint closure commit so
the fixture path is tracked in a clean checkout:

- `tests/fixtures/service_checkpoint_closure/rate-schedules/base_11-2017-ct-rate.json`
- `tests/fixtures/service_checkpoint_closure/rate-schedules/llm_svc_events.jsonl`
- `tests/fixtures/service_checkpoint_closure/rate-schedules/checkpoints/checkpoint_svc_2019-04-01.json`
- `tests/fixtures/service_checkpoint_closure/rate-schedules/checkpoints/checkpoint_svc_2024-10-24.json`
- `tests/fixtures/service_checkpoint_closure/rate-schedules/checkpoints/checkpoint_svc_2025-03-31.json`

`src/legal_corpus/service_checkpoint_inputs.py` resolves service checkpoint
inputs. Tests prefer the tracked fixture. The report regenerator prefers local
runtime inputs when complete and falls back to the tracked fixture when
`derived/` is absent; `--prefer-fixture` forces the clean-checkout path and
records `inputs.source = tracked_fixture` in the regenerated report.

## Evidence Commands

Focused regression:

```bash
python3 -m pytest tests/test_service_checkpoint_closure.py -q
```

Clean-checkout-style regeneration:

```bash
python3 scripts/regenerate_svc_reconciliation_report.py --prefer-fixture
```

Expected summaries:

- `2019-04-01`: `matched=40/40`
- `2024-10-24`: `matched=41/41`
- `2025-03-31`: `matched=41/41`

The script writes:

- `derived/version_history/rate-schedules/reconciliation_report_svc.json`
- `derived/version_history/rate-schedules/adjudication_report_svc_2019-04-01.json`
- `derived/version_history/rate-schedules/adjudication_report_svc_2024-10-24.json`
- `derived/version_history/rate-schedules/adjudication_report_svc_2025-03-31.json`

These report files are regenerated evidence artifacts under ignored
`derived/` state. The commit-ready source path is the fixture inputs, the
helper resolver, the regression test, and the regenerator command.

## Verification Boundary

This closure is commit-ready for the service checkpoint package: staged diff
hygiene passes, the focused service closure gate passes, the full pytest suite
passes, and fixture/runtime regeneration both reproduce 40/40, 41/41, 41/41.

The repository-wide pipeline gate is not green in this checkout. A terminal
review run of `python3 main.py pipeline verify --derived-dir /tmp/gfl_pipeline_verify --manifest /tmp/gfl_pipeline_verify/latest.json`
failed with broad pre-existing source/corpus integrity errors, including
thousands of `sources` structure errors, hundreds of `corpus` canonical-id
path mismatches, and XML source-span hash mismatches. That failure is outside
the service checkpoint closure diff and remains a separate corpus/source
integrity mission. Do not claim `make verify` is green for this closure.

## Orchestration Commit Scope

Intentional orchestration files for this closure:

- `.mcp.json`: switches the Zenith MCP provider and worker ACP command from
  Hermes to opencode (`opencode acp`); no secrets are embedded.
- `.opencode/orchestrator_prompt.md`: reusable opencode Zenith orchestrator
  guidance; review found no credentials or API tokens.

Excluded local state:

- `.opencode/goals/state.json` and `.opencode/goals/state.json.ledger.jsonl`:
  opencode goal runtime state; remove them from the tracked checkout for this
  closure and keep `.opencode/goals/` ignored.
- `.opencode/node_modules/`, `.opencode/package.json`, and
  `.opencode/package-lock.json`: local opencode install/runtime files.
- `.opencode/skills`: local symlink to the current Zenith runtime skills
  directory, not a repo-owned skills package.
- `.opencode/.gitignore`: local ignore file for opencode package/runtime
  files; it was inspected for scope but is not part of the closure source
  package.

Product commit scope must also include the non-ignored fixture/test/helper files
that make the clean-checkout proof reproducible, especially
`tests/fixtures/service_checkpoint_closure/`,
`tests/test_service_checkpoint_closure.py`,
`src/legal_corpus/service_checkpoint_inputs.py`, and the updated regenerator
and materializer files.

## Remaining Provenance Debt

The service event ledger includes three consolidated gap-fill events with
placeholder CBIC citations. This is documented in
`service_checkpoint_closure_classification.md`; it is not court-ready
provenance and should be replaced by a separate citation-verification mission.
