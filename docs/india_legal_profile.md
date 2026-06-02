# India Legal Document Profile

This project treats Git as the canonical legal history and treats databases,
search indexes, and graph stores as rebuildable derived artifacts.

## Scope

The first supported jurisdiction profile is Indian Union legal material, with a
focus on GST documents. The supported instrument types are:

- `act`
- `rules`
- `notification`
- `circular`
- `order`
- `form`
- `schedule`

The first supported hierarchy elements are:

- `chapter`
- `section`
- `rule`
- `subrule`
- `clause`
- `proviso`
- `explanation`
- `form-section`

## Canonical IDs

Canonical IDs use stable, URL-like paths. They identify legal meaning, not a
database row.

Examples:

```text
/in/union/rules/cgst-rules-2017/rule/8
/in/union/rules/cgst-rules-2017/rule/8/subrule/1
/in/union/forms/gst-reg-01
/in/union/notifications/cbic/central-tax/2025/18-2025
```

Derived systems may use separate storage IDs, but they must preserve the
canonical ID.

Canonical document paths must be derived from canonical IDs. The validator
rejects XML files that are stored at the wrong corpus path.

Examples:

```text
/in/union/rules/cgst-rules-2017/rule/10
  -> corpus/in/union/rules/cgst-rules-2017/rule-010.xml
/in/union/rules/cgst-rules-2017/rule/9a
  -> corpus/in/union/rules/cgst-rules-2017/rule-09a.xml
/in/union/forms/gst-reg-01
  -> corpus/in/union/forms/gst-reg-01/form.xml
/in/union/notifications/cbic/central-tax/2025/18-2025
  -> corpus/in/union/notifications/cbic/central-tax/2025/18-2025.xml
```

## Required Metadata

Each canonical document should carry:

- `canonical_id`
- `document_type`
- `title`
- `jurisdiction`
- `language`
- `source_type`
- `source_url`
- `source_sha256`
- `publication_date`
- `effective_from`
- `issuing_authority`
- `review_status`
- `parser_version`

Unknown values should be explicit as empty strings or `unknown`, not omitted.

## Source Archive

Original source files are evidence. They are never edited in place.

Source archives live under `sources/` and contain:

```text
source.pdf or source.txt
metadata.yaml
extracted_text.json
structure.json
```

`extracted_text.json` preserves page and character offsets. LLM parsing works
from extracted text and returns structure, spans, references, and confidence.
It must not rewrite legal text.

`structure.json` stores parser output. Every node and reference should include
`start`, `end`, `confidence`, and a source-slice `text_hash` when available.
Validation recomputes the hash from `extracted_text.json` so parser output
cannot silently drift from the source text.

Source validation also recomputes the SHA-256 of the archived source file and
compares it with `metadata.yaml` and `extracted_text.json`. The extracted page
records must round-trip back to the full extracted text, so offset/page drift is
reported before a canonical XML file is promoted.

## Canonical Corpus

Canonical documents live under `corpus/`. The preferred long-term format is an
Akoma Ntoso-compatible XML subset. The current implementation starts with a
small XML profile that can be validated deterministically and exported to richer
Akoma Ntoso later.

Current corpus coverage from the local `data/Law` import includes promoted CBIC
notification families for central tax, integrated tax, union territory tax,
compensation cess, and related rate notifications. It also includes six local
Act PDFs: Customs Tariff Act, 1975 and Finance Acts for 2022, 2023, 2024, and
2025. Act PDFs may contain long schedule/table paragraphs; these are accepted
only with visible quality flags so reviewers can decide whether deeper
table-specific structuring is needed.

Official base GST sources have also been added under `data/Law/base_laws`:
CGST Act, 2017 from CBIC Tax Information HTML; IGST Act, 2017 from India Code
PDF; and CBIC CGST Rules, 2017 Part A Rules plus Part B Forms PDFs. The Part B
Forms PDF is an aggregate source; run `corpus split-forms` against its source
archive to write individual canonical form documents.

The remaining base-law gaps should be treated as the next ingestion queue:
Income-tax Act, 1961; Customs Act, 1962; Central Excise Act, 1944; remaining
CGST/IGST section edge cases; remaining CGST Rules edge cases; and remaining
GST forms. Rebuild `derived/references/unresolved_references.json` after each
promotion batch and use its missing-target ranking to choose the next source
documents.

Source archives can be converted to canonical XML in one step with
`corpus ingest`. The command supports deterministic, paragraph-only, and LLM
structure parsing modes. LLM mode may propose structure, but validation still
requires exact source offsets and hashes before XML is accepted.

For local PDF collections, create a source inventory first:

```bash
python3 main.py source inventory data/Law \
  --output derived/sources/source_inventory.json

python3 main.py source inventory-validate derived/sources/source_inventory.json

python3 main.py source inventory-report derived/sources/source_inventory.json \
  --output derived/sources/source_inventory_report.json

python3 main.py corpus ingest-inventory derived/sources/source_inventory.json \
  --category central-tax --limit 5 --progress
```

For `data/Law/GST_Notifications_CBIC`, the inventory reads the CBIC notification
CSV, resolves local PDF filenames, computes checksums, and proposes canonical
IDs plus source/corpus target paths. Files not covered by a known metadata index
remain `unclassified` until a profile-specific classifier is added. Top-level
Finance Act and Customs Tariff PDFs are classified as `act` candidates, while
macOS `._` resource-fork files are ignored. Batch ingestion from the inventory
is a dry run unless `--execute` is provided. Use `--progress` for long runs. The
inventory report is the compact review artifact for missing PDFs, category
counts, unclassified files, validation status, and sample ready items.

Use alternate roots for initial smoke tests so the canonical `corpus/` is not
changed until a reviewed batch is ready:

```bash
python3 main.py source inventory data/Law \
  --sources-root /tmp/git_for_law_sample/sources \
  --corpus-root /tmp/git_for_law_sample/corpus \
  --output /tmp/git_for_law_sample/source_inventory.json \
  --limit 1 --no-unclassified

python3 main.py source inventory-validate /tmp/git_for_law_sample/source_inventory.json

python3 main.py corpus ingest-inventory /tmp/git_for_law_sample/source_inventory.json \
  --execute --limit 1 --mode deterministic --progress

python3 main.py corpus quality \
  --corpus-dir /tmp/git_for_law_sample/corpus \
  --output /tmp/git_for_law_sample/quality_report.json
```

Use the quality report before promoting generated XML into the canonical
corpus. It flags review risks such as overlong paragraphs and joined-token PDF
extraction artifacts.

Build a promotion plan from the batch reports before copying generated XML into
the canonical corpus:

```bash
python3 main.py corpus promote-batch \
  /tmp/git_for_law_sample/ingest_report.json \
  /tmp/git_for_law_sample/quality_report.json \
  --target-corpus corpus \
  --target-sources sources \
  --output derived/review/batch_promotion_plan.json
```

This command is a dry run by default. `--target-sources` keeps canonical XML
and source archives promoted together. Add `--approve` only after reviewing the
selected and excluded documents in the plan.

```bash
python3 main.py corpus ingest source.pdf sources/example corpus/example.xml \
  --canonical-id /in/union/circulars/example \
  --document-type circular \
  --mode llm \
  --provider local \
  --base-url http://100.79.90.123:8000/v1 \
  --model <model-name>
```

Split an aggregate forms archive after ingestion:

```bash
python3 main.py corpus split-forms \
  sources/in/union/forms/cgst-rules-2017-forms \
  --corpus-dir corpus \
  --output derived/review/form_split_report.json \
  --overwrite
```

Rendered source paragraphs preserve provenance attributes:

- `sourceStart`
- `sourceEnd`
- `sourceHash`
- `sourceNodeType`
- `sourceConfidence`

These are derived from `structure.json` and can be checked against
`extracted_text.json`, so reviewers can trace canonical XML back to exact
source offsets.
For Act PDFs, deterministic parsing emits numbered sections as provision-level
`section` elements with canonical `/section/{number}` IDs. Ambiguous numbered
schedule rows remain paragraphs and are surfaced through the quality report if
they are too long for comfortable review.

## Validation Rules

Corpus updates must pass:

- XML well-formedness
- required metadata checks
- canonical ID format checks
- legal hierarchy checks, including duplicate `eId` detection and local provision IDs staying under the document canonical ID
- source text round-trip checks when source spans are available
- source file checksum checks against archive metadata and extracted text
- unresolved-reference reporting
- low-confidence structure reporting

Unresolved references are warnings by default because a partial corpus may refer
to Acts, forms, or rules that have not been ingested yet. Amendment application
is stricter: it is blocked unless every mutation target resolves, unless the
operator explicitly uses a partial-application workflow for review.

Build the unresolved-reference report after each promotion batch:

```bash
python3 main.py corpus unresolved-references \
  --output derived/references/unresolved_references.json
```

The report ranks missing canonical targets and missing target documents, which
turns unresolved-reference warnings into the next source-ingestion queue.

## Amendment Workflow

Notifications are parsed into mutation plans before canonical files are changed.

```text
source archive
  -> amendment plan
  -> target resolution against corpus/
  -> output corpus with supported mutations applied
  -> validation and corpus diff report
  -> legal review
  -> promotion manifest
  -> Git commit after approval
```

The current implementation supports:

- `SPLICE` into a resolved provision paragraph
- `INSERT_SIBLING` for new rule documents

Unsupported or unresolved mutations remain in the plan with a status such as
`target_missing` or `unsupported`.

Before promotion, generate a corpus diff report:

```bash
python3 main.py corpus diff derived/corpus-amended \
  --base-corpus corpus \
  --output derived/diffs/18-2025-corpus-diff.json
```

The report is keyed by canonical document ID and includes added, removed,
modified, and unchanged documents, provision-level changes, checksums, paths,
and unified text diffs. Promotion is dry-run by default. A reviewed output
corpus is copied into `corpus/` only after explicit approval, and the promotion
manifest records added, modified, removed, and unchanged XML files. Git commits
are opt-in and should include only promoted corpus files and the manifest.

## Derived Artifacts

Derived outputs live under `derived/` and are rebuildable from `corpus/`:

- Neo4j citation and amendment graph
- search index
- vector index
- rendered HTML
- API payloads

Neo4j is not the source of truth for legal versions.

The derived graph uses `LegalNode` records keyed by canonical IDs. Document and
provision roles are represented as node properties, containment is represented
with `CONTAINS`, and legal relationships such as `REQUIRES_FORM`, `REFERS_TO`,
and amendment operations are rebuilt from XML references.

The derived search index is a deterministic JSONL export. It stores one record
per canonical document and one record per provision, preserving the canonical
ID, role, document type, title, source path, text, and token counts. It is meant
to be rebuilt from `corpus/`, not edited by hand.

```bash
python3 main.py search rebuild
python3 main.py search query "registration certificate"
```

The vector/RAG chunk export is also deterministic JSONL. It stores chunk IDs,
canonical IDs, document IDs, document types, source paths, chunk order, text, and
token estimates. Embeddings should be generated from this export so the
embedding backend can be changed without changing canonical legal files.

```bash
python3 main.py vector chunks
```

The API payload export is a deterministic JSON document for downstream apps. It
contains document records, provision records, references, profile metadata, and
summary counts.

```bash
python3 main.py api export
```

The rendered HTML export is a static corpus browser built from the same payload.
It includes an index page, one page per canonical document, CSS, and a copy of
the API payload for client-side reuse.

```bash
python3 main.py html build
```

## Verification Gate

The pipeline verification command is the CI-friendly gate for this profile.
It validates source archives and canonical XML, verifies XML source spans
against matching source archives, rebuilds derived quality,
unresolved-reference, graph, Neo4j payload, API payload, HTML, search, and
vector chunk artifacts, validates `derived/sources/source_inventory.json` when
present, verifies JSON/JSONL round-trips, and writes a manifest.

```bash
python3 main.py pipeline verify
```

Validation warnings do not fail the gate by default because partial corpora can
have legitimate unresolved references. Use `--strict-warnings` when operating a
curated corpus that is expected to resolve every internal canonical reference.
Use `--inventory` to point at a non-default inventory file.

## Corpus Lookup

Basic legal lookups should read directly from `corpus/` before consulting
Neo4j, search, or vector indexes. The corpus lookup layer resolves canonical
document IDs and provision IDs, accepts known legacy prototype IDs, and exports
plain text for review, diffing, or downstream indexing.

Examples:

```bash
python3 main.py corpus list --type notification
python3 main.py corpus query /in/union/rules/cgst-rules-2017/rule/10/subrule/1
python3 main.py corpus export-text /in/union/forms/gst-reg-01
```

If a canonical ID is both a document and a provision, such as a single-rule
document, lookup defaults to the document role. Use `--role provision` to inspect
the structural provision node.
