# Source & Corpus Integrity Debt — Diagnostic Report

**Date:** 2026-08-03
**Scope:** `make verify` / `python3 main.py pipeline verify` failure diagnosis.
**Mission:** Diagnose and document only. This report does **not** fix the failures —
that is a separate corpus/source-integrity mission. The service-checkpoint closure
and GST rules reconciliation tracks are independent of this debt and remain green.

## TL;DR

`pipeline verify` runs three validation steps before rebuilding derived artifacts.
All three currently fail, but the failures are dominated by two
**validator-scope mismatches** (not data corruption) and one **provenance-drift**
class. The headline numbers, captured by invoking the same validators
`src/legal_corpus/verification.py` uses (`validate_sources`, `validate_corpus`,
`validate_xml_source_spans`):

| Step | Errors | Warnings | Real data-defect count |
|---|---|---|---|
| `sources` (structure has no nodes) | 13,937 | 27,917 | ~0 (validator-scope mismatch — see §1) |
| `corpus` (canonical-id / doc-type / metadata) | 577 | 231 | 141 canonical-id path drift + 6 missing metadata |
| `xml_source_spans` (sourceHash mismatch) | 1,491 | 4 | 1,491 (provenance drift — see §3) |

The 430 `unsupported document_type` corpus "errors" (355 `instruction` + 70
`regulation` + 5 `None`) are a **validator allow-list gap**, not corrupted data.

## How the numbers were produced

`pipeline verify` itself cannot complete inside the 120s shell budget because it
rebuilds derived artifacts (graph, search, vector, HTML) after the validation
steps. To get exact, reproducible failure counts I called the three validation
functions directly with the same arguments `run_verification()` uses
(`src/legal_corpus/verification.py:121-166`), e.g.:

```python
from src.legal_corpus.validator import validate_sources, validate_corpus, validate_xml_source_spans
from src.legal_corpus.verification import _source_archives_by_sha, _xml_properties
```

The full reproduction script and raw JSON live at `/tmp/gfl_pv/diag.py` and
`/tmp/gfl_pv/span_diag.json` (scratch, not committed). Total wall time for all
three validators: ~30s.

---

## §1. `sources` — "structure has no nodes" (13,937 errors / 27,917 warnings)

**Failure shape:** `validate_sources` reports `sources/.../<id>: structure has no nodes`
for 13,937 of 16,885 source archives. The accompanying 27,917 warnings are
`source_sha256 missing from extracted_text.json` and `extracted_text.json has no page records`
on the same archives.

**Breakdown by document family:**

| Family | Count |
|---|---|
| `in/union/notifications` | 10,114 |
| `in/union/circulars` | 2,997 |
| `in/union/instructions` | 355 |
| `in/union/forms` | 208 |
| `in/union/rules` | 95 |
| `in/union/orders` | 93 |
| `in/union/regulations` | 70 |
| `in/union/acts` | 5 |

**Root cause:** validator-scope mismatch. The deterministic structure parser
produces rich per-provision structural nodes for Acts (the 883 Acts are fully
structured and validate). The bulk CBIC ingestion pipeline
(`scripts/bulk_ingest_cbic_documents.py` and friends) intentionally stores
notifications, circulars, instructions, orders, and regulations as **full-text
source archives** (`extracted_text.json`) without node-level structure parsing,
because these documents are consumed as amendment-source text and search-index
input, not as structured per-provision Akoma Ntoso XML. `validate_sources`
assumes every archive must carry structural nodes, so it flags the entire CBIC
bulk corpus as erroneous.

The 5 `acts` rows are the only ones worth individual review — they are amendment
Acts (e.g. `central-goods-and-services-tax-amendment-act-2023`,
`integrated-goods-and-services-tax-amendment-act-2023`,
`provisional-collection-of-taxes-act-1931`) that were archived but not
structure-parsed because they are short amending instruments whose effect is
applied via the amendment pipeline rather than rendered as standalone XML.

**Proposed remediation:**
1. **Validator fix (cheap, high-leverage).** Make the "structure has no nodes"
   check conditional on document type: require structural nodes only for
   document families that are supposed to be structure-parsed (acts, rules,
   forms-that-are-structured). For notifications/circulars/instructions/orders/
   regulations, treat full-text archives without nodes as valid. This collapses
   ~13,932 of 13,937 errors with no data change. The same change resolves the
   `source_sha256 missing` / `no page records` warnings for those families
   (full-text archives legitimately may not have page records).
2. **Ingestion hardening (medium).** Ensure bulk CBIC `extracted_text.json`
   records a `source_sha256` even when there are no page records, so the warning
   layer is clean for archives that do not go through structure parsing.
3. **The 5 acts (narrow).** Decide per-amending-Act whether to structure-parse
   it or to mark it as amendment-only; either way the validator should not flag
   it once §1.1 lands.

**Estimated effort:** validator change ~2-4 hours (including a regression test
that pins the new document-type-aware behavior); ingestion hardening ~1 day.

---

## §2. `corpus` — canonical-id path mismatch + document-type allow-list (577 errors / 231 warnings)

**Failure shape:** 577 errors decompose cleanly into three disjoint classes:

| Class | Count | Real defect? |
|---|---|---|
| `unsupported document_type: instruction` | 355 | No — validator allow-list gap |
| `unsupported document_type: regulation` | 70 | No — validator allow-list gap |
| `unsupported document_type: None` | 5 | Yes — metadata gap |
| `canonical_id path mismatch` | 141 | Yes — path-naming drift |
| `missing metadata fields` | 6 | Yes — metadata gap |

The 231 warnings are almost all `unresolved canonical reference` (e.g.
`/in/union/forms/gst-tran-1`, `/in/union/forms/gst-rfd-01`,
`/in/union/acts/income-tax-act-2025/section/12ab`) — cross-references the
resolver cannot find in the current corpus (forms not yet ingested as canonical
nodes; the 2025 Income-tax Act sections not yet present).

### §2.1 Document-type allow-list gap (425 of 577)

The validator's allowed `document_type` set does not include `instruction` or
`regulation`, even though both are first-class document families in the corpus
(CBIC instructions and customs regulations). This makes the validator emit
`unsupported document_type` for every one of those documents. This is a
validator bug, not a corpus defect — the documents themselves are well-formed.

**Fix:** add `instruction` and `regulation` to the validator's allowed
`document_type` set in `src/legal_corpus/validator.py`. Resolves 425 "errors"
with zero data change.

### §2.2 Canonical-id path mismatch (141)

These split as **71 circulars + 70 regulations**. Sample:

```
corpus/in/union/circulars/cbic/1994-10-27/ssi-exemption-...-ssi-655d5bff.xml:
  canonical_id path mismatch; expected corpus/in/union/circulars/cbic/1994-10-27/<slug>
```

**Root cause:** path-naming drift between the ingester and the validator's
`canonical_id → path` derivation. The on-disk filename for these older (1994-1997)
circulars and for customs regulations is a long title-derived slug with a hash
suffix (e.g. `...-655d5bff.xml`), produced by the CBIC bulk ingester. The
validator derives the expected path from the XML's `canonical_id` and arrives at
a different slug. The content is intact; only the filename↔canonical_id contract
disagrees. Concentrated in pre-1998 circulars (slugification rules differ) and
in regulations (a separate ingester with its own naming convention).

**Proposed remediation:** align the validator's canonical-id→path slugifier with
the ingester's actual slug rule for these two families (deterministic, since
both sides are pure functions of title/date). Alternatively, emit a warning
rather than an error for slug-suffix-only differences. Requires confirming the
ingester slug rule from `scripts/bulk_ingest_cbic_documents.py`.

### §2.3 Missing metadata (6) + null document_type (5)

11 genuine metadata gaps. These need per-document inspection — likely
ingestion-edge cases where the metadata header was not populated. Small enough
to fix individually.

**Estimated effort for §2:** allow-list fix ~1 hour (with test); slugifier
alignment ~0.5-1 day (mostly reading the two ingesters); metadata gaps ~2-3
hours.

---

## §3. `xml_source_spans` — sourceHash does not match (1,491 errors / 4 warnings)

**Failure shape:** for 3,046 XML files that have a matching source archive (the
others have no archive to check against and are skipped), `validate_xml_source_spans`
reports 1,491 `sourceHash does not match extracted text` errors. Every error is
this single reason; the element ids (`section_11a`, `section_11a__para_1`,
`section_74a`, …) indicate the failures are concentrated in structured Acts whose
sections were rendered against an extraction whose bytes later shifted.

**Root cause:** provenance drift. The XML records `sourceHash = sha256(extracted_text[start:end])`
at render time. The check recomputes `sha256` over the same span in the current
`extracted_text.json` and they disagree. The most likely cause is that the
source text was **re-extracted or normalized after the XML was rendered**
(whitespace/encoding normalization, or a re-scrape), so the byte slice the span
points at no longer hashes to the recorded value. The concentration in
Income-tax Act sections (112A, 74A) suggests one re-extraction batch is
responsible for the bulk of the 1,491.

This is the most serious of the three classes because `sourceHash` is the
cryptographic provenance contract — an audit would reject these spans. It does
not affect text content (the text is still the right text), but it breaks the
independent-verifiability guarantee.

**Important scope note:** only 3,046 of 17,057 corpus XML files are even
checked, because the loop matches on `source_sha256` against
`_source_archives_by_sha(sources)`. The other ~14,011 XML files have no
matching archive and are silently skipped. So the true provenance coverage is
thin (18%), and the 1,491 failures are within that 18%.

**Proposed remediation:**
1. **Reconcile, don't re-render.** For each failing span, determine whether the
   *current* `extracted_text` or the *recorded* hash is authoritative. If the
   re-extraction is the better source (more likely), recompute and rewrite the
   `sourceHash`/`sourceStart`/`sourceEnd` in the XML from the current extraction,
   and capture the recompute event in an audit log. Do **not** silently
   overwrite; record old→new hash pairs.
2. **Widen coverage.** Investigate why 14,011 XML files have no matching source
   archive by `source_sha256`. This is likely the same CBIC-bulk-vs-structured
   split as §1 — structured Acts got archives with a `source_sha256`, bulk CBIC
   documents did not.
3. **Pin the extraction.** Add a content-hash of the extraction to the archive
   so future re-extractions are detectable (the `source_sha256 missing` warning
   in §1 is the same root issue).

**Estimated effort:** large. ~1,491 spans to reconcile across ~3,000 files,
plus a coverage-widening investigation. Treat as a dedicated provenance mission.

---

## Recommended execution order for the fix mission

1. **§2.1 allow-list fix** (1h) — instantly removes 425 of 577 corpus "errors";
   pure validator correctness fix; safe, tested.
2. **§1.1 document-type-aware structure check** (2-4h) — removes ~13,932 of 13,937
   source "errors"; validator-scope fix; safe with a regression test.
3. **§2.2 slugifier alignment** (0.5-1 day) — removes the 141 canonical-id path
   mismatches.
4. **§2.3 + §3 metadata gaps + provenance reconciliation** (dedicated mission) —
   the only true data-integrity work; largest effort, highest stakes.

After 1-3, `pipeline verify`'s error count drops from ~16,005 to the ~1,497 real
provenance/metadata defects in §3, which then becomes a focused integrity mission
with a clean signal.

## Verification boundary (what this report is not)

This is a diagnostic, not a claim that `make verify` is green. The three
validation steps still fail as described above. The GST rules reconciliation
(A+B = 100%, Tier D = 0 — see `docs/reconciliation_status.md`) and the service
checkpoint closure (40/40, 41/41, 41/41) are independent of this debt and remain
green. No code or corpus data was changed to produce this report.
