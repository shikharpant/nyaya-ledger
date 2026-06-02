# Git for Law - Steel Thread MVP

A temporal Legal AST system demonstrating version-controlled legal documents with time-travel queries.

## Recommended Architecture

The project is moving to a Git-backed canonical corpus:

```text
Official PDF/text source
  -> sources/ archive with checksums and extracted spans
  -> corpus/ Akoma Ntoso-compatible XML
  -> derived/ graph, search, and vector indexes
```

Neo4j remains useful for relationship queries, but it is a derived index. The
canonical legal record lives in Git under `corpus/`.

## License

This project is released as open-source software under the MIT License. See
`LICENSE`.

## Quick Start

```bash
# 1. Start Neo4j
docker-compose up -d

# 2. Install dependencies
pip install -r requirements.txt

# 3. Copy env file and add your OpenAI key (optional, for GPT-4o parsing)
cp .env.example .env

# 4. Load the genesis block (base state of CGST Rules)
python main.py load-genesis

# 5. Query a rule
python main.py query CGST_Rules/Rule_8

# 6. Query with dependencies (follows MUTATIS_MUTANDIS edges)
python main.py query CGST_Rules/Rule_8 --with-deps

# 7. Query a form
python main.py query-form FORM_GST_REG_01
```

## Canonical Corpus Workflow

Seed the current prototype data into canonical XML:

```bash
python3 main.py corpus seed
python3 main.py corpus validate
python3 main.py corpus list
python3 main.py corpus query /in/union/rules/cgst-rules-2017/rule/10
python3 main.py corpus export-text /in/union/rules/cgst-rules-2017/rule/10/subrule/1
python3 main.py graph rebuild
python3 main.py graph cypher
python3 main.py search rebuild
python3 main.py search query "registration certificate"
python3 main.py vector chunks
python3 main.py api export
python3 main.py html build
python3 main.py pipeline verify
```

Current local artifact status as of 2026-06-03:

The main GitHub repository intentionally tracks code, schemas, docs, scripts,
and tests only. The generated legal data directories `data/`, `sources/`,
`corpus/`, and `derived/` are local artifacts ignored by Git. The local
workspace used for the latest verification contains:

- Canonical corpus: 1,465 XML documents under `corpus/`.
- Source archives: 1,356 source archives under `sources/`.
- Document mix: 137 Act XML documents, 4 CGST Rules XML documents, 107 GST
  form XML documents, and 1,216 CBIC notification XML documents.
- Graph export in `derived/graph/corpus_graph.json`: 14,281 nodes and 34,874
  edges.
- Search/vector exports: 14,284 search records and 94,787 vector chunks.
- Reference review in `derived/references/unresolved_references.json`: 1,313
  unresolved targets across 2,301 occurrences. These remain warnings; strict
  unresolved-reference gating should stay off until the allowlist and remaining
  base-law ingestion are complete.
- Raw source material: 130 top-level base-law JSON files in
  `data/Law/base_laws`, plus residual Finance Acts material under
  `data/Law/base_laws/residual`.

Ingested legal statutes and procedures include:

- GST and indirect tax: CGST Act, 2017; IGST Act, 2017; CGST Rules, 2017;
  Customs Tariff Act, 1975; GST forms from the CGST Rules Part B PDF; and CBIC
  central tax, integrated tax, union territory tax, compensation cess, and rate
  notification families.
- Direct tax and finance: Income-tax Act, 1961; Income-tax Act, 2025; Indian
  Income-tax Act, 1922 (repealed); Income-tax Rules, 2026; Wealth-tax Act, 1957;
  Gift-tax Act, 1958; Interest-tax Act, 1974; Expenditure-tax Act, 1987;
  Equalisation Levy; Commodities Transaction Tax; Securities Transaction Tax;
  Direct Tax Vivad se Vishwas Act, 2020; Direct Tax Vivad Se Vishwas Scheme,
  2024; and Finance Acts for 2022, 2023, 2024, and 2025.
- Criminal law and procedure: Bharatiya Nyaya Sanhita, 2023; Bharatiya Nagarik
  Suraksha Sanhita, 2023; Bharatiya Sakshya Adhiniyam, 2023; Indian Penal Code,
  1860; Indian Evidence Act, 1872; Code of Criminal Procedure, 1973; Prevention
  of Corruption Act, 1988; Prevention of Money Laundering Act, 2002; and Fugitive
  Economic Offenders Act, 2018.
- Corporate, securities, and financial regulation: Companies Act, 1956
  (repealed); Companies Act, 2013; LLP Act, 2008; Competition Act, 2002; SEBI
  Act, 1992; Securities Contracts (Regulation) Act, 1956; Depositories Act,
  1996; SARFAESI Act, 2002; Recovery of Debts and Bankruptcy Act, 1993; Banking
  Cash Transaction Tax; RBI Act, 1934; IRDA Act, 1999; Credit Information
  Companies (Regulation) Act, 2005; and Prohibition of Benami Property
  Transactions Act, 1988.
- Civil, property, IP, and general law: Indian Contract Act, 1872; Transfer of
  Property Act, 1882; Registration Act, 1908; Indian Stamp Act, 1899; Indian
  Trusts Act, 1882; Specific Relief Act, 1963; Limitation Act, 1963; Sale of
  Goods Act, 1930; Partnership Act, 1932; Hindu Marriage, Succession, Adoption
  and Maintenance, and Minority and Guardianship Acts; Patents Act, 1970;
  Copyright Act, 1957; Trade Marks Act, 1999; Designs Act, 2000; Information
  Technology Act, 2000; and Digital Personal Data Protection Act, 2023.
- Labour, welfare, environment, and public law: Employees' State Insurance Act,
  1948; Employees Provident Funds and Miscellaneous Provisions Act, 1952;
  Employees Compensation Act, 1923; Payment of Wages Act, 1936; Minimum Wages
  Act, 1948; Payment of Bonus Act, 1965; Payment of Gratuity Act, 1972;
  Maternity Benefit Act, 1961; Industrial Disputes Act, 1947; Factories Act,
  1948; Environment Protection Act, 1986; Air Act, 1981; Water Act, 1974; Right
  to Information Act, 2005; Consumer Protection Act, 2019; Aadhaar Act, 2016;
  Legal Metrology Act, 2009; and National Green Tribunal Act, 2010.

Recently completed ingestion:

- Income-tax Rules, 2026 is fully ingested at
  `sources/in/union/rules/income-tax-rules-2026/` and
  `corpus/in/union/rules/income-tax-rules-2026/rules.xml`: 333 rules and 707
  cross-references to Income-tax Act sections and other rules.
- `scripts/ingest_it_act_2025.py` was fixed to read
  `data/Law/base_laws/income_tax_act_2025.json` instead of the Income-tax Rules
  JSON, and the Income-tax Act, 2025 corpus was re-ingested.
- `python3 main.py pipeline verify` and the test suite both pass in the current
  state: 54 tests passed and pipeline verification is all ok.

Staged source data not yet fully promoted to canonical XML:

- The Income-tax Rules, 2026 PDF is 976 pages and also contains appendices/forms
  that still need a dedicated appendix/form splitter before separate form
  promotion.
- The Income Tax India catalog download completed for all listed Acts:
  128 downloaded, 2 skipped because already present, and 0 failed downloads.
- `data/Law/base_laws/residual/finance_acts.json` is a raw aggregate source,
  not one canonical Act. It has been split into 81 year/variant files under
  `data/Law/base_laws/residual/finance_acts_split`, with 336 entries retained in
  `finance_acts_unsplit.json` because their scraped text lacks reliable
  year/Act metadata.
- The Indian Stamp Act, 1899 download has 100 unique entries: sections 1-79,
  inserted alphanumeric provisions, and Schedule I plus Schedule II.

The next high-value work is to split and promote the Income-tax Rules, 2026
appendices/forms, reduce unresolved GST form and rule references, and add an
allowlist for expected external references. Use
`derived/references/unresolved_references.json` as the prioritized ingestion and
parser-improvement queue.

This creates:

```text
corpus/     # Akoma Ntoso-compatible canonical XML
sources/    # archived source text plus metadata/checksums/spans
derived/    # rebuildable graph/search/vector/API/HTML artifacts, not committed
derived/amendments/ # amendment plans and application reports
```

Extract text from a source archive:

```bash
python3 main.py source extract sources/cbic/central-tax/2025/18-2025
python3 main.py corpus parse sources/cbic/central-tax/2025/18-2025
python3 main.py source validate sources/cbic/central-tax/2025/18-2025
python3 main.py corpus render sources/cbic/central-tax/2025/18-2025 corpus/in/union/notifications/cbic/central-tax/2025/18-2025.xml
```

Parser modes:

```bash
python3 main.py corpus parse <source-dir> --mode deterministic
python3 main.py corpus parse <source-dir> --mode paragraph
python3 main.py corpus parse <source-dir> --mode llm --provider deepseek
python3 main.py corpus parse <source-dir> --mode llm --provider local \
  --base-url http://100.79.90.123:8000/v1 --model <model-name>
```

LLM parsing returns spans only; source text still comes from extraction and is
validated by `source validate`.
Source validation recomputes the archived source file SHA-256, checks extracted
page offsets, and verifies parser spans against the extracted text.

Build a local source inventory before bulk ingestion:

```bash
python3 main.py source inventory data/Law \
  --output derived/sources/source_inventory.json

python3 main.py source inventory-validate derived/sources/source_inventory.json

python3 main.py source inventory-report derived/sources/source_inventory.json \
  --output derived/sources/source_inventory_report.json

python3 main.py corpus ingest-inventory derived/sources/source_inventory.json \
  --category central-tax --limit 5 --progress

python3 main.py corpus quality \
  --corpus-dir /tmp/git_for_law_sample/corpus \
  --output /tmp/git_for_law_sample/quality_report.json

python3 main.py corpus unresolved-references \
  --output derived/references/unresolved_references.json
```

For CBIC notification PDFs, the inventory uses
`data/Law/GST_Notifications_CBIC/_notification_index.csv` when present. It
records the source file, SHA-256, official URL, canonical ID, suggested source
archive path, suggested corpus output path, and an ingest command. PDFs that
are not covered by a known metadata index are marked `unclassified`. Top-level
Finance Act and Customs Tariff PDFs are classified as `act` candidates, and
macOS `._` resource-fork files are ignored.
`source inventory-report` writes a compact review artifact with category counts,
missing PDFs, unclassified files, validation status, and sample ready items.
`corpus ingest-inventory` is a dry run by default; add `--execute` to write
source archives and canonical XML. Use `--progress` for long batch runs.
`corpus quality` summarizes review risks in generated XML, including very long
paragraphs and joined-token extraction artifacts.
`corpus unresolved-references` ranks missing canonical targets by occurrence
and source-document count. Use it to choose the next base Acts, rules, and forms
to ingest before turning on strict unresolved-reference gates.
`corpus promote-batch` combines the ingest and quality reports into a dry-run
promotion plan. By default it selects only ingested documents that were not
flagged by quality review and does not overwrite existing target XML unless
`--overwrite` is provided. Pass `--target-sources sources` to promote matching
source archives with the XML. Add `--approve` only after reviewing the plan.

For a safe real-PDF smoke test, point the inventory at temporary or derived
roots instead of `corpus/`:

```bash
python3 main.py source inventory data/Law \
  --sources-root /tmp/git_for_law_sample/sources \
  --corpus-root /tmp/git_for_law_sample/corpus \
  --output /tmp/git_for_law_sample/source_inventory.json \
  --limit 1 --no-unclassified

python3 main.py source inventory-validate /tmp/git_for_law_sample/source_inventory.json

python3 main.py corpus ingest-inventory /tmp/git_for_law_sample/source_inventory.json \
  --execute --limit 1 --mode deterministic

python3 main.py source validate /tmp/git_for_law_sample/sources
python3 main.py corpus validate --corpus-dir /tmp/git_for_law_sample/corpus

python3 main.py corpus promote-batch \
  /tmp/git_for_law_sample/ingest_report.json \
  /tmp/git_for_law_sample/quality_report.json \
  --target-corpus corpus \
  --target-sources sources \
  --output derived/review/batch_promotion_plan.json
```

Run the full source-to-XML conversion in one command:

```bash
python3 main.py corpus ingest path/to/circular.pdf \
  sources/cbic/circulars/2026/example \
  corpus/in/union/circulars/cbic/2026/example.xml \
  --canonical-id /in/union/circulars/cbic/2026/example \
  --document-type circular \
  --title "Circular Example" \
  --mode llm --provider local \
  --base-url http://100.79.90.123:8000/v1 --model <model-name>
```

`corpus ingest` archives the source, extracts text, parses source spans,
renders AKN-compatible XML, validates the source archive and XML, and can use
the deterministic, paragraph, or LLM parser modes.
Rendered paragraphs include source offset/hash provenance (`sourceStart`,
`sourceEnd`, `sourceHash`, `sourceNodeType`, and `sourceConfidence`) so XML can
be traced back to `extracted_text.json`.
For Act PDFs, deterministic parsing promotes numbered section blocks to
provision-level `<section refersTo=".../section/n">` elements while preserving
source spans; long schedule/table blocks may still appear in the quality report
for review before promotion.

Query the Git-backed corpus without Neo4j:

```bash
python3 main.py corpus list --type rule
python3 main.py corpus query /in/union/forms/gst-reg-01
python3 main.py corpus query CGST_Rules/Rule_10/SubRule_1 --role provision
python3 main.py corpus query /in/union/rules/cgst-rules-2017/rule/10 --json
python3 main.py corpus export-text /in/union/rules/cgst-rules-2017/rule/10 \
  --output derived/rule-10.txt
```

The corpus validator also enforces the canonical ID to path mapping. For
example, `/in/union/rules/cgst-rules-2017/rule/10` belongs at
`corpus/in/union/rules/cgst-rules-2017/rule-010.xml`.

Archive a new source file:

```bash
python3 main.py source add path/to/source.pdf sources/cbic/central-tax/2026/example \
  --canonical-id /in/union/notifications/cbic/central-tax/2026/example \
  --document-type notification \
  --title "Notification Example"
```

Plan and apply supported amendments into a separate output corpus:

```bash
python3 main.py amendment plan sources/cbic/central-tax/2025/18-2025 \
  --output derived/amendments/18-2025-plan.json

python3 main.py amendment apply sources/cbic/central-tax/2025/18-2025 \
  --output-corpus derived/corpus-amended \
  --allow-partial

python3 main.py corpus diff derived/corpus-amended \
  --base-corpus corpus \
  --output derived/diffs/18-2025-corpus-diff.json

python3 main.py amendment promote derived/corpus-amended \
  --target-corpus corpus \
  --manifest derived/amendments/promotion_manifest.json
```

By default, amendment application is blocked if any mutation target cannot be
resolved. `--allow-partial` is intended for review workflows and writes to a
separate output corpus. `corpus diff` compares the reviewed output corpus
against `corpus/` by canonical document ID and writes a JSON report with added,
removed, modified, unchanged, provision-level changes, and unified text diffs.
Promotion is a dry run unless `--approve` is provided; `--git-commit` can be
added to create a commit for promoted paths.

The India legal document profile is documented in
`docs/india_legal_profile.md`.

Neo4j is rebuilt from the corpus, not loaded from genesis JSON:

```bash
python3 main.py graph rebuild --corpus-dir corpus \
  --output derived/graph/corpus_graph.json

python3 main.py graph cypher --corpus-dir corpus \
  --output derived/graph/corpus_neo4j_payload.json

python3 main.py graph load --corpus-dir corpus --clear
```

`graph load` requires the `neo4j` Python package and a running Neo4j instance.

Search is also a derived artifact:

```bash
python3 main.py search rebuild --corpus-dir corpus \
  --output derived/search/corpus_search.jsonl

python3 main.py search query "Permanent Account Number" --role provision
python3 main.py search query "Aadhaar authentication" --type notification --json
```

The search index is JSONL so it can be committed for inspection, regenerated in
CI, or used as input for a later BM25/vector service.

Export vector/RAG-ready chunks from the same corpus:

```bash
python3 main.py vector chunks --corpus-dir corpus \
  --output derived/vector/corpus_chunks.jsonl

python3 main.py vector chunks --no-documents --max-chars 700 --overlap 100
```

The chunk export does not call an embedding model. It is the stable, reviewable
input for a later embedding job.

Export an API-ready JSON payload for apps or services:

```bash
python3 main.py api export --corpus-dir corpus \
  --output derived/api/corpus_api.json
```

Render a static HTML corpus browser:

```bash
python3 main.py html build --corpus-dir corpus \
  --output-dir derived/html
```

Run the full offline verification gate:

```bash
make verify
python3 main.py pipeline verify
python3 main.py pipeline verify --strict-warnings
```

The gate validates `sources/` and `corpus/`, verifies XML source spans against
matching source archives, rebuilds quality/unresolved-reference/graph/API/HTML/
search/vector artifacts, validates `derived/sources/source_inventory.json` when
present, round-trips JSON and JSONL outputs, and writes
`derived/verification/latest.json`. `make verify` also runs tests, Python
compilation, and `git diff --check`; the same checks are wired into
`.github/workflows/verify.yml`. `--strict-warnings` is useful once the corpus is
complete enough that unresolved references should fail CI.

## Time-Travel Queries

```bash
# Query rule as of a specific date
python main.py query CGST_Rules/Rule_10 --as-of 2025-10-31

# Compare rule at two different dates
python main.py compare CGST_Rules/Rule_10 2025-10-31 2025-11-01
```

## Parsing & Applying Mutations

```bash
# Parse a notification (uses GPT-4o)
python main.py parse data/notifications/18_2025_CT.txt -o mutations.json

# Parse offline (regex-based, no LLM)
python main.py parse data/notifications/18_2025_CT.txt -o mutations.json --offline

# Validate mutations (dry run)
python main.py apply mutations.json --dry-run

# Apply mutations
python main.py apply mutations.json
```

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         LEGAL AST ARCHITECTURE                          │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  ┌───────────────┐     ┌───────────────┐     ┌───────────────┐         │
│  │  Notification │────▶│   Mutation    │────▶│    Neo4j      │         │
│  │     (PDF)     │     │   Compiler    │     │    Graph      │         │
│  └───────────────┘     └───────────────┘     └───────────────┘         │
│                              │                      │                   │
│                              │ JSON-Patch           │ Cypher            │
│                              ▼                      ▼                   │
│                        ┌───────────────┐     ┌───────────────┐         │
│                        │    Anchor     │     │  Time-Travel  │         │
│                        │   Resolver    │     │    Engine     │         │
│                        └───────────────┘     └───────────────┘         │
│                                                    │                    │
│                                                    ▼                    │
│                                             ┌───────────────┐          │
│                                             │  RAG Pipeline │          │
│                                             │  (with deps)  │          │
│                                             └───────────────┘          │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

## Project Structure

```
git-for-law/
├── corpus/               # Canonical Akoma Ntoso-compatible XML
├── sources/              # Immutable source archive + metadata
├── derived/              # Rebuildable graph/search/vector artifacts
├── docs/                 # Legal corpus profile and architecture notes
├── data/
│   ├── genesis/           # Base state (2017)
│   │   ├── cgst_rules_chapter3.json
│   │   └── form_gst_reg_01.json
│   ├── mutations/         # Parsed mutations
│   └── notifications/     # Source PDFs/text
├── src/
│   ├── models.py          # Pydantic models
│   ├── schemas/           # JSON Schema definitions
│   ├── graph_loader.py    # Load genesis into Neo4j
│   ├── mutation_parser.py # GPT-4o parser
│   ├── anchor_resolver.py # Strict text matching
│   ├── mutation_applier.py# Copy-on-Write mutations
│   ├── time_travel.py     # Temporal queries
│   └── legal_corpus/      # Canonical corpus pipeline
├── tests/                 # Pipeline and validation tests
├── docker-compose.yml     # Neo4j setup
├── requirements.txt
└── main.py                # CLI interface
```

## Key Concepts

### Copy-on-Write Versioning
Instead of modifying nodes in place, we create new versions:
```
(Rule_10:v1) --[NEXT_VERSION {eff: 2025-11-01}]--> (Rule_10:v2)
```

### Semantic Edges
Rules link to Forms and other Rules:
- `REQUIRES_FORM` - Rule requires a specific form
- `MUTATIS_MUTANDIS` - Rule inherits logic from another
- `SUBJECT_TO` - Rule is subject to another provision

### Hybrid Form Storage
Forms use graph nodes for structure, JSON Schema for fields:
```
(:Form)─[:HAS_SECTION]─>(:FormSection {schema_payload: {...}})
```

## License
MIT
