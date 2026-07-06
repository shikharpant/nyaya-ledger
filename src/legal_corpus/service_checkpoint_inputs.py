"""Shared paths for service-rate checkpoint closure artifacts.

The canonical runtime artifacts live under ``derived/`` and are intentionally
gitignored in this repository. The closure regression gate also needs a
tracked fixture copy so clean checkouts can prove the service checkpoint
baseline without depending on local ignored state.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_RATE_DIR = PROJECT_ROOT / "derived" / "version_history" / "rate-schedules"
FIXTURE_RATE_DIR = (
    PROJECT_ROOT / "tests" / "fixtures" / "service_checkpoint_closure" / "rate-schedules"
)
SERVICE_RATE_DIR_ENV = "GFL_SERVICE_RATE_DIR"
SERVICE_DATES = ("2019-04-01", "2024-10-24", "2025-03-31")


@dataclass(frozen=True)
class ServiceRateInputs:
    rate_dir: Path
    source: str
    base: Path
    events: Path
    checkpoint_dir: Path


def _required_paths(rate_dir: Path) -> list[Path]:
    return [
        rate_dir / "base_11-2017-ct-rate.json",
        rate_dir / "llm_svc_events.jsonl",
        *[
            rate_dir / "checkpoints" / f"checkpoint_svc_{date}.json"
            for date in SERVICE_DATES
        ],
    ]


def missing_service_rate_inputs(rate_dir: Path) -> list[Path]:
    """Return the required service fixture/runtime files absent from ``rate_dir``."""

    return [path for path in _required_paths(rate_dir) if not path.exists()]


def resolve_service_rate_inputs(
    rate_dir: str | Path | None = None,
    *,
    prefer_fixture: bool = False,
) -> ServiceRateInputs:
    """Resolve service checkpoint inputs from explicit, runtime, or fixture paths.

    Resolution order:
    1. explicit ``rate_dir`` argument, if supplied;
    2. ``GFL_SERVICE_RATE_DIR`` environment override, if supplied;
    3. runtime ``derived/version_history/rate-schedules`` unless
       ``prefer_fixture`` is true;
    4. tracked fixture directory.
    """

    explicit = Path(rate_dir) if rate_dir is not None else None
    if explicit is None:
        env_rate_dir = os.environ.get(SERVICE_RATE_DIR_ENV)
        explicit = Path(env_rate_dir) if env_rate_dir else None

    if explicit is not None:
        return _inputs_from_dir(explicit, "explicit")

    candidates = (
        ((FIXTURE_RATE_DIR, "tracked_fixture"), (RUNTIME_RATE_DIR, "runtime"))
        if prefer_fixture
        else ((RUNTIME_RATE_DIR, "runtime"), (FIXTURE_RATE_DIR, "tracked_fixture"))
    )
    for candidate, source in candidates:
        if not missing_service_rate_inputs(candidate):
            return _inputs_from_dir(candidate, source)

    missing = missing_service_rate_inputs(RUNTIME_RATE_DIR)
    fixture_missing = missing_service_rate_inputs(FIXTURE_RATE_DIR)
    raise FileNotFoundError(
        "Missing service checkpoint inputs. Runtime missing: "
        + ", ".join(str(p) for p in missing)
        + "; fixture missing: "
        + ", ".join(str(p) for p in fixture_missing)
    )


def _inputs_from_dir(rate_dir: Path, source: str) -> ServiceRateInputs:
    rate_dir = rate_dir.resolve()
    missing = missing_service_rate_inputs(rate_dir)
    if missing:
        raise FileNotFoundError(
            f"Missing service checkpoint inputs in {rate_dir}: "
            + ", ".join(str(path) for path in missing)
        )
    return ServiceRateInputs(
        rate_dir=rate_dir,
        source=source,
        base=rate_dir / "base_11-2017-ct-rate.json",
        events=rate_dir / "llm_svc_events.jsonl",
        checkpoint_dir=rate_dir / "checkpoints",
    )
