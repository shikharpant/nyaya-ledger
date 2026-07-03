"""Curated legal work identity registry."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


REQUIRED_WORK_FIELDS = {
    "work_id",
    "title",
    "document_type",
    "jurisdiction",
    "authority",
    "aliases",
    "canonical_corpus_ids",
    "preferred_base_corpus_id",
    "base_as_of",
    "base_source",
    "source_priority",
    "notes",
}


def _key(value: str) -> str:
    return re.sub(r"[^a-z0-9/]+", "", value.strip().lower())


@dataclass(frozen=True)
class RegistryDiagnostics:
    duplicate_aliases: dict[str, list[str]]
    duplicate_corpus_ids: dict[str, list[str]]
    cgst_act_aliases: list[str]
    cgst_rules_aliases: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "duplicate_aliases": self.duplicate_aliases,
            "duplicate_corpus_ids": self.duplicate_corpus_ids,
            "cgst_act_aliases": self.cgst_act_aliases,
            "cgst_rules_aliases": self.cgst_rules_aliases,
        }


class StatuteIdentityRegistry:
    """Resolve corpus-local and external identifiers to curated work IDs."""

    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data
        self.works = data.get("works", [])
        self._by_work_id: dict[str, dict[str, Any]] = {}
        self._alias_index: dict[str, str] = {}
        self._corpus_index: dict[str, str] = {}
        self.errors: list[str] = []
        self._build_indexes()

    @classmethod
    def load(cls, path: Path | str) -> "StatuteIdentityRegistry":
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    def validate(self) -> list[str]:
        errors = list(self.errors)
        for work in self.works:
            missing = sorted(REQUIRED_WORK_FIELDS - set(work))
            if missing:
                errors.append(f"{work.get('work_id', '<missing work_id>')}: missing fields {', '.join(missing)}")
            for list_field in ("aliases", "canonical_corpus_ids", "source_priority"):
                if list_field in work and not isinstance(work[list_field], list):
                    errors.append(f"{work.get('work_id', '<missing work_id>')}: {list_field} must be a list")
        return errors

    def resolve_alias(self, value: str) -> str | None:
        if value in self._by_work_id:
            return value
        return self._alias_index.get(_key(value))

    def resolve_corpus_id(self, value: str) -> str | None:
        if value in self._by_work_id:
            return value
        return self._corpus_index.get(_key(value)) or self._alias_index.get(_key(value))

    def preferred_corpus_id(self, work_id: str) -> str | None:
        work = self._by_work_id.get(work_id)
        return work.get("preferred_base_corpus_id") if work else None

    def work(self, work_id: str) -> dict[str, Any] | None:
        return self._by_work_id.get(work_id)

    def base_as_of(self, work_id: str) -> str | None:
        work = self._by_work_id.get(work_id)
        return work.get("base_as_of") if work else None

    def baseline_path(self, work_id: str) -> str | None:
        work = self._by_work_id.get(work_id)
        return work.get("baseline_path") if work else None

    def version_history_dir(self, work_id: str) -> str | None:
        work = self._by_work_id.get(work_id)
        return work.get("version_history_dir") if work else None

    def diagnostics(self) -> RegistryDiagnostics:
        alias_owners: dict[str, list[str]] = {}
        corpus_owners: dict[str, list[str]] = {}
        for work in self.works:
            work_id = work.get("work_id", "")
            for alias in work.get("aliases", []):
                alias_owners.setdefault(_key(str(alias)), []).append(work_id)
            for corpus_id in work.get("canonical_corpus_ids", []):
                corpus_owners.setdefault(_key(str(corpus_id)), []).append(work_id)

        duplicate_aliases = {
            key: owners for key, owners in alias_owners.items() if len(set(owners)) > 1
        }
        duplicate_corpus_ids = {
            key: owners for key, owners in corpus_owners.items() if len(set(owners)) > 1
        }
        cgst_act = self._by_work_id.get("/in/union/acts/cgst-act-2017", {})
        cgst_rules = self._by_work_id.get("/in/union/rules/cgst-rules-2017", {})
        return RegistryDiagnostics(
            duplicate_aliases=duplicate_aliases,
            duplicate_corpus_ids=duplicate_corpus_ids,
            cgst_act_aliases=[
                str(item)
                for item in [cgst_act.get("work_id"), *cgst_act.get("aliases", []), *cgst_act.get("canonical_corpus_ids", [])]
                if item
            ],
            cgst_rules_aliases=[
                str(item)
                for item in [
                    cgst_rules.get("work_id"),
                    *cgst_rules.get("aliases", []),
                    *cgst_rules.get("canonical_corpus_ids", []),
                ]
                if item
            ],
        )

    def _build_indexes(self) -> None:
        for work in self.works:
            work_id = work.get("work_id")
            if not isinstance(work_id, str) or not work_id:
                self.errors.append("Registry work is missing work_id")
                continue
            if work_id in self._by_work_id:
                self.errors.append(f"Duplicate work_id: {work_id}")
            self._by_work_id[work_id] = work
            self._alias_index[_key(work_id)] = work_id
            self._corpus_index[_key(work_id)] = work_id

            for alias in work.get("aliases", []):
                self._add_index(self._alias_index, str(alias), work_id, "alias")
            for corpus_id in work.get("canonical_corpus_ids", []):
                self._add_index(self._corpus_index, str(corpus_id), work_id, "corpus_id")

    def _add_index(self, index: dict[str, str], value: str, work_id: str, kind: str) -> None:
        key = _key(value)
        existing = index.get(key)
        if existing and existing != work_id:
            self.errors.append(f"Duplicate {kind} {value!r}: {existing} and {work_id}")
        index[key] = work_id


def load_registry(path: Path | str) -> StatuteIdentityRegistry:
    return StatuteIdentityRegistry.load(path)


def validate_registry(path: Path | str) -> dict[str, Any]:
    registry = load_registry(path)
    errors = registry.validate()
    return {
        "ok": not errors,
        "errors": errors,
        "work_count": len(registry.works),
        "diagnostics": registry.diagnostics().to_dict(),
    }


__all__ = ["StatuteIdentityRegistry", "load_registry", "validate_registry", "RegistryDiagnostics"]
