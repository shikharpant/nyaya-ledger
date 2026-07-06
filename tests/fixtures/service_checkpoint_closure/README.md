# Service Checkpoint Closure Fixtures

This fixture set is a tracked copy of the minimum service-rate inputs needed to
prove the 11/2017-ct-rate closure baseline in a clean checkout:

- `rate-schedules/base_11-2017-ct-rate.json`
- `rate-schedules/llm_svc_events.jsonl`
- `rate-schedules/checkpoints/checkpoint_svc_2019-04-01.json`
- `rate-schedules/checkpoints/checkpoint_svc_2024-10-24.json`
- `rate-schedules/checkpoints/checkpoint_svc_2025-03-31.json`

The runtime copies under `derived/version_history/rate-schedules/` remain
gitignored. `scripts/regenerate_svc_reconciliation_report.py` prefers complete
runtime inputs when present and falls back to this fixture set when `derived/`
is absent. The regression test intentionally uses this fixture set so the
40/40, 41/41, 41/41 service closure gate is reproducible from tracked repo
state.

The service event ledger includes three consolidated gap-fill events whose CBIC
citations are placeholders pending per-clause verification. That provenance
debt is intentionally documented in `service_checkpoint_closure_classification.md`
and is not court-ready.
