# Git for Law - Steel Thread MVP

A temporal Legal AST system demonstrating version-controlled legal documents with time-travel queries.

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
│   └── time_travel.py     # Temporal queries
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
