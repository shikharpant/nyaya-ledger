# Pipeline Reference

The Nyaya Ledger pipeline converts source legal documents into canonical XML and
derived artifacts. All commands are invoked through `main.py`.

## Full Verification

```bash
make verify
```

Runs tests, Python compilation, `pipeline verify`, and `git diff --check`.
This is the same gate run by CI (`.github/workflows/verify.yml`).

## Source Management

```bash
# Create a source archive from a PDF
python3 main.py source add path/to/act.pdf sources/in/union/acts/my-act \
  --canonical-id /in/union/acts/my-act \
  --document-type act \
  --title "My Act, 2026"

# Extract text from an existing archive
python3 main.py source extract sources/in/union/acts/my-act

# Validate archive integrity
python3 main.py source validate sources/in/union/acts/my-act

# Build an inventory of all source files
python3 main.py source inventory data/Law \
  --output derived/sources/source_inventory.json

# Validate the inventory
python3 main.py source inventory-validate derived/sources/source_inventory.json

# Review inventory summary
python3 main.py source inventory-report derived/sources/source_inventory.json \
  --output derived/sources/source_inventory_report.json
```

## Corpus Management

```bash
# One-shot ingest: source -> archive -> parse -> render -> validate
python3 main.py corpus ingest path/to/act.pdf \
  sources/in/union/acts/my-act \
  corpus/in/union/acts/my-act/act.xml \
  --canonical-id /in/union/acts/my-act \
  --document-type act \
  --title "My Act, 2026" \
  --mode deterministic

# Bulk ingest from an inventory
python3 main.py corpus ingest-inventory derived/sources/source_inventory.json \
  --category central-tax --limit 5 --progress

# Validate all corpus XML
python3 main.py corpus validate

# List documents
python3 main.py corpus list --type act

# Query a specific provision
python3 main.py corpus query /in/union/acts/income-tax-act-1961/section/112a

# Export provision text
python3 main.py corpus export-text /in/union/acts/patents-act-1970/section/84

# Quality report
python3 main.py corpus quality

# Unresolved references report
python3 main.py corpus unresolved-references \
  --output derived/references/unresolved_references.json

# Compare corpora (for amendment review)
python3 main.py corpus diff derived/corpus-amended \
  --base-corpus corpus \
  --output derived/diffs/report.json
```

### Parser Modes

| Mode | Description |
|---|---|
| `deterministic` | Regex-based, reproducible, no external dependencies |
| `paragraph` | Paragraph-level splitting, minimal structure |
| `llm` | LLM-assisted span detection (requires `--provider`) |

LLM providers: `openai`, `deepseek`, or `local` (with `--base-url`).

## Derived Artifacts

```bash
# Knowledge graph
python3 main.py graph rebuild
python3 main.py graph cypher     # Neo4j Cypher payload
python3 main.py graph load --clear  # Load into Neo4j

# Search index
python3 main.py search rebuild
python3 main.py search query "capital gains"

# Vector chunks (for RAG)
python3 main.py vector chunks

# API payload
python3 main.py api export

# Static HTML browser
python3 main.py html build
```

## Amendment Workflow

```bash
# Plan amendments from a notification
python3 main.py amendment plan sources/cbic/central-tax/2025/18-2025 \
  --output derived/amendments/plan.json

# Apply to a separate output corpus
python3 main.py amendment apply sources/cbic/central-tax/2025/18-2025 \
  --output-corpus derived/corpus-amended --allow-partial

# Promote reviewed amendments
python3 main.py amendment promote derived/corpus-amended \
  --target-corpus corpus --approve
```
