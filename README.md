<p align="center">
  <img src="https://img.shields.io/badge/status-active-success" alt="status" />
  <img src="https://img.shields.io/badge/license-MIT-blue" alt="license" />
  <img src="https://img.shields.io/badge/python-3.10+-blue" alt="python" />
  <img src="https://img.shields.io/badge/corpus-2%2C094%20XML%20docs-orange" alt="corpus" />
  <img src="https://img.shields.io/badge/MCP-agent--ready-green" alt="mcp" />
</p>

<h1 align="center">Nyaya Ledger</h1>

<p align="center">
  <strong>The open-source legal data pipeline that makes Indian legislation machine-readable, versionable, and queryable.</strong>
</p>

<p align="center">
  <em>Nyaya</em> (Sanskrit: न्याय) &mdash; justice, logic, method.
</p>

---

## The Problem

India's legal system generates thousands of pages of legislation each year
across hundreds of statutes, rules, notifications, and circulars. This
material lives in PDFs, scanned gazettes, and fragmented government portals.
There is no single machine-readable, version-controlled, cross-referenced
source of truth.

A chartered accountant tracing why Section 112A of the Income-tax Act says
what it says &mdash; which Finance Act inserted it, which notification amended
it, which rule operationalises it &mdash; must manually cross-reference
dozens of documents. A legal AI assistant cannot answer that question because
no structured data exists.

**Nyaya Ledger** exists to change that.

---

## What It Does

Nyaya Ledger is a deterministic pipeline that converts Indian legal documents
into structured, provenance-tracked, cross-referenced knowledge artifacts:

| Output | Description | Use Case |
|---|---|---|
| **Akoma Ntoso XML** | Per-provision legal text with cryptographic source provenance | Authoritative legal reference |
| **Knowledge Graph** | 65K+ cross-reference edges across 574 statutes | Regulatory impact analysis |
| **Search Index** | 33K+ full-text search records | Legal research tools |
| **Vector Chunks** | 156K+ RAG-ready text chunks | AI legal assistants |
| **MCP Server** | Model Context Protocol interface | Agent-native legal intelligence |

Every XML element carries a `sourceHash` (SHA-256 of the exact source-text
span), so any provision can be independently audited against the original
government document.

---

## Corpus at a Glance

| Metric | Value |
|---|---|
| **Statutes** | 574 Acts of Parliament |
| **Notifications** | 1,216 CBIC notifications |
| **Rules** | Income-tax Rules, CGST Rules, and more |
| **Forms** | 297 prescribed legal forms |
| **Total XML documents** | **2,094** |
| **Provisions** | 33,766 |
| **Cross-references** | **65,313** (97% resolved) |
| **RAG-ready chunks** | 156,882 |
| **Source archives** | 1,985 |

### Legal Domain Coverage

| Domain | Statutes |
|---|---|
| **Tax & Revenue** | Income-tax Act (935 sections), CGST Act, Customs Act, Central Excise Act, 46 total |
| **Criminal Law** | Bharatiya Nyaya Sanhita, BNSS, BSA, IPC, CrPC, PMLA, 11 total |
| **Corporate** | Companies Act 2013, IBC, Competition Act, SEBI Act, 21 total |
| **Intellectual Property** | Patents Act 1970, Copyright Act 1957, Trade Marks Act 1999 |
| **Labour & Employment** | Industrial Disputes Act, EPF Act, Code on Wages, 22 total |
| **Banking & Finance** | RBI Act, Banking Regulation Act, SARFAESI Act, 12 total |
| **Environment** | Environment Protection Act, Forest Act, Wildlife Act, 19 total |
| **Civil & Property** | Indian Contract Act, Transfer of Property Act, 24 total |
| **Constitutional** | Representation of the People Act, Electoral Bond Scheme, 9 total |
| **Digital & Tech** | IT Act 2000, DPDP Act 2023, Aadhaar Act, 5 total |

---

## Architecture

```mermaid
flowchart LR
    subgraph Sources["Official Sources"]
        PDF["Government PDFs"]
        PORTAL["India Code Portal"]
        CBIC["CBIC Notifications"]
    end

    subgraph Pipeline["Nyaya Ledger Pipeline"]
        EXTRACT["Source Extraction<br/>+ SHA-256 Archiving"]
        PARSE["Deterministic<br/>Structure Parsing"]
        RENDER["Akoma Ntoso<br/>XML Rendering"]
        VALIDATE["SourceHash<br/>Validation"]
    end

    subgraph Corpus["Canonical Corpus"]
        XML["2,094 XML Documents<br/>Git-Versioned"]
    end

    subgraph Derived["Derived Artifacts"]
        GRAPH["Knowledge Graph<br/>33K nodes · 65K edges"]
        SEARCH["Search Index"]
        VECTOR["Vector Chunks<br/>156K for RAG"]
        MCP["MCP Server"]
    end

    Sources --> EXTRACT --> PARSE --> RENDER --> VALIDATE --> Corpus
    Corpus --> GRAPH & SEARCH & VECTOR & MCP
```

### Design Principles

1. **Git is the canonical history.** Databases and indexes are rebuildable
   derived artifacts. `corpus/` is the single source of truth.
2. **Deterministic parsing.** The same source document always produces the
   same XML. No LLM required for the core pipeline.
3. **Cryptographic provenance.** Every XML element carries `sourceStart`,
   `sourceEnd`, and `sourceHash` linking it to the exact bytes of the
   original government document.
4. **Cross-reference graph.** 65,313 edges connect provisions across 574
   statutes, enabling regulatory impact analysis.
5. **Agent-native.** MCP server exposes the entire corpus as tools for
   AI agents and LLM-based workflows.

---

## Knowledge Graph Snapshots

### Income-tax Act, 1961 &mdash; Cross-Reference Network

The most interconnected statute: 935 sections with 2,805 references spanning
40+ other acts. The Income-tax Rules, 2026 add 584 references back to IT Act
sections.

```mermaid
graph LR
    ITA["Income-tax Act, 1961<br/>935 sections · 2,805 refs"]

    subgraph Hub["Most Referenced Provisions"]
        S2["§2 Definitions<br/>252 refs"]
        S112A["§112A<br/>123 refs"]
        S111A["§111A<br/>124 refs"]
    end

    ITA --- Hub

    COMP["Companies Act<br/>40 refs"]
    CUST["Customs Act<br/>4 refs"]
    ITR["IT Rules, 2026<br/>333 rules · 584 refs"]

    ITA -->|"REFERS_TO"| COMP
    ITA -->|"REFERS_TO"| CUST
    ITR -->|"REFERS_TO"| ITA

    style ITA fill:#1a5276,color:#fff
    style ITR fill:#117a65,color:#fff
    style COMP fill:#7d3c98,color:#fff
    style S2 fill:#2e86c1,color:#fff
    style S112A fill:#2e86c1,color:#fff
    style S111A fill:#2e86c1,color:#fff
```

### Patents Act, 1970 &mdash; Internal Hub Structure

176 sections with 187 internal cross-references. §84 (compulsory licences)
is the most-referenced provision, serving as the central hub.

```mermaid
graph TD
    PA["Patents Act, 1970<br/>176 sections"]

    PA --- S117A["§117A Appeals<br/>24 outgoing refs"]
    PA --- SCH["First Schedule<br/>24 outgoing refs"]

    S84["§84 Compulsory Licences<br/>11 incoming refs"]
    S35["§35 Secret Inventions<br/>9 incoming refs"]
    S64["§64 Revocation<br/>7 incoming refs"]

    S117A --> S84 & S35 & S64
    SCH --> S84

    style PA fill:#117a65,color:#fff
    style S84 fill:#e74c3c,color:#fff
    style S117A fill:#f39c12,color:#000
```

### BNSS, 2023 &mdash; Sentencing Hub Cluster

India's new criminal procedure code: 532 sections, 680 internal
cross-references. Sections 64-71 (sentencing limits) form a dense hub
targeted by 50+ referring provisions.

```mermaid
graph TD
    BNSS["BNSS, 2023<br/>532 sections · 680 refs"]

    subgraph Hub["Sentencing Hub §64-71"]
        S64["§64 · 15 in"]
        S70["§70 · 15 in"]
        S65["§65 · 14 in"]
    end

    BNSS --- Hub

    S243["§243 Maintenance · 21 out"]
    SCHEDULE["Schedule · 50 out"]

    S243 --> S64 & S70
    SCHEDULE --> S64 & S70

    style BNSS fill:#1a5276,color:#fff
    style S64 fill:#e74c3c,color:#fff
    style S70 fill:#e74c3c,color:#fff
    style S243 fill:#f39c12,color:#000
    style SCHEDULE fill:#f39c12,color:#000
```

---

## Source Provenance

Every XML element carries cryptographic proof of origin:

```xml
<section eId="section_112a"
         refersTo="/in/union/acts/income-tax-act-1961/section/112a"
         sourceStart="48912"
         sourceEnd="49301"
         sourceHash="a1b2c3d4..."
         sourceNodeType="section"
         sourceConfidence="0.9">
  <num>112A</num>
  <content>
    <p>* Section 112A. Tax on long term capital gains.-</p>
    <paragraph eId="section_112a__para_1"
               sourceStart="49302" sourceEnd="49650"
               sourceHash="e5f6a7b8...">
      <content><p>(1) Notwithstanding anything contained in ...</p></content>
      <references>
        <ref eId="section_112a__para_1__ref_1"
             href="/in/union/acts/income-tax-act-1961/section/112"
             type="REFERS_TO"/>
      </references>
    </paragraph>
  </content>
</section>
```

The `sourceHash` is `sha256(extracted_text["text"][sourceStart:sourceEnd])` &mdash;
independently verifiable against the archived source extraction.

---

## Quick Start

```bash
git clone https://github.com/shikharpant/git-for-law.git
cd git-for-law
pip install -r requirements.txt
make test
```

### Ingesting New Legislation

```bash
# From a scraped JSON
python3 scripts/bulk_ingest_acts.py

# From a PDF source
python3 main.py corpus ingest path/to/act.pdf \
  sources/in/union/acts/my-act \
  corpus/in/union/acts/my-act/act.xml \
  --canonical-id /in/union/acts/my-act \
  --title "My Act, 2026" --mode deterministic
```

### Querying the Corpus

```bash
# List all acts
python3 main.py corpus list --type act

# Query a specific provision
python3 main.py corpus query /in/union/acts/income-tax-act-1961/section/112a

# Full-text search
python3 main.py search query "capital gains tax"

# Time-travel query
python3 main.py query CGST_Rules/Rule_10 --as-of 2025-10-31
```

### Building Derived Artifacts

```bash
make verify          # Full verification gate (tests + compile + pipeline)
python3 main.py graph rebuild      # Knowledge graph
python3 main.py search rebuild     # Search index
python3 main.py vector chunks      # RAG-ready chunks
python3 main.py pipeline verify    # 12-step verification
```

---

## Data Sources

| Source | Coverage | Access Method |
|---|---|---|
| [India Code](https://www.indiacode.nic.in) | 846 Central Acts | DSpace catalog + Section API |
| [Income Tax Department](https://www.incometaxindia.gov.in) | IT Act, IT Rules, Forms | Liferay Search API |
| [CBIC](https://www.cbic.gov.in) | 1,216 GST/customs notifications | PDF archive |

All data is sourced from official government portals. No proprietary or
third-party legal databases are used.

---

## Project Structure

```
git-for-law/
├── main.py                       # CLI (40+ commands)
├── src/
│   ├── legal_corpus/              # Core pipeline (21 modules)
│   │   ├── source_archive.py      # Immutable source archiving
│   │   ├── structure_parser.py    # Deterministic structure parsing
│   │   ├── renderer.py            # Akoma Ntoso XML rendering
│   │   ├── validator.py           # SourceHash verification
│   │   ├── graph_index.py         # Knowledge graph builder
│   │   ├── search_index.py        # Full-text search builder
│   │   ├── vector_index.py        # RAG chunk builder
│   │   └── ...
│   ├── models.py                  # Pydantic data models
│   └── schemas/                   # JSON Schema definitions
├── scripts/                       # Ingestion and scraping scripts
│   ├── scrape_india_code_missing_acts.py   # India Code scraper
│   ├── download_india_code_catalog.py      # Catalog downloader
│   ├── ingest_it_act.py                    # IT Act 1961
│   ├── ingest_it_rules_2026.py             # IT Rules 2026
│   ├── split_ingest_it_rules_forms.py      # Forms extraction
│   ├── bulk_ingest_acts.py                 # Bulk ingestion
│   └── ...
├── tests/
│   └── test_canonical_corpus.py   # 56 tests
├── docs/
│   └── india_legal_profile.md     # Jurisdiction profile
├── Makefile
├── requirements.txt
├── docker-compose.yml
└── LICENSE                        # MIT
```

**Local-only directories** (generated, gitignored):

| Directory | Purpose | Size |
|---|---|---|
| `data/` | Raw PDFs, scraped JSONs | Variable |
| `sources/` | Extracted text + metadata + checksums | ~506 MB |
| `corpus/` | Canonical Akoma Ntoso XML (2,094 files) | ~122 MB |
| `derived/` | Rebuildable graph, search, vector artifacts | ~14 GB |

---

## How It Works

```mermaid
sequenceDiagram
    participant Gov as Government PDF/Portal
    participant Extract as Source Extractor
    participant Parse as Structure Parser
    participant Render as XML Renderer
    participant Validate as Validator
    participant Corpus as Git Corpus

    Gov->>Extract: Extract text + archive SHA-256
    Extract->>Parse: extracted_text.json
    Parse->>Parse: Identify provisions, cross-refs
    Parse->>Render: structure.json (spans + refs)
    Render->>Validate: Akoma Ntoso XML
    Validate->>Validate: Check metadata, sourceHash, paths
    Validate->>Corpus: Validated XML
    Corpus->>Corpus: graph rebuild · search rebuild · vector chunks
```

1. **Source Extraction** &mdash; Government PDFs and portal HTML are extracted
   into `extracted_text.json` with page-level offsets. SHA-256 archived.
2. **Structure Parsing** &mdash; Deterministic regex parser identifies provision
   boundaries (sections, rules, forms, provisos, explanations).
3. **Cross-Reference Extraction** &mdash; Scans provision text for references
   to other sections, rules, and forms across the corpus.
4. **XML Rendering** &mdash; Structure spans and references rendered into
   Akoma Ntoso XML with full source provenance.
5. **Validation** &mdash; Checks required metadata, `sourceHash` integrity,
   canonical ID to file-path mapping.

---

## Amendment & Time-Travel

```bash
# Query a rule as of a specific date
python3 main.py query CGST_Rules/Rule_10 --as-of 2025-10-31

# Compare a rule at two dates
python3 main.py compare CGST_Rules/Rule_10 2025-10-31 2025-11-01

# Plan + apply amendments
python3 main.py amendment plan sources/cbic/.../18-2025 \
  --output derived/amendments/plan.json
python3 main.py amendment apply ... --output-corpus derived/corpus-amended
python3 main.py corpus diff derived/corpus-amended --base-corpus corpus
```

Amendments apply copy-on-write mutations with effective dates, preserving
full version history.

---

## Roadmap

- [ ] **Full India Code coverage** &mdash; complete ingestion of all 846 Central Acts
- [ ] **State legislation** &mdash; India Code hosts state acts; extend pipeline
- [ ] **Case law integration** &mdash; Supreme Court and High Court judgments
- [ ] **Subordinate legislation** &mdash; Rules, regulations, and circulars at scale
- [ ] **Real-time monitoring** &mdash; Gazette watch for new amendments
- [ ] **REST API** &mdash; Public API for programmatic access
- [ ] **Multi-language** &mdash; Hindi and regional language support
- [ ] **Community ingestion** &mdash; CLI tool for community-contributed acts

---

## Use Cases

| Who | How |
|---|---|
| **Legal AI companies** | RAG-ready vector chunks + knowledge graph for regulatory reasoning |
| **Law firms** | Cross-reference analysis across 574 statutes for due diligence |
| **Compliance teams** | Time-travel queries to determine applicable law on any date |
| **Legal researchers** | Open, auditable dataset for empirical legal studies |
| **Government agencies** | Version-controlled legislation with amendment tracking |
| **Legal aid platforms** | Structured, searchable access to the law of the land |

---

## Contributing

1. Fork the repository
2. Create a feature branch
3. Run `make verify` to ensure all tests pass
4. Submit a pull request

CI runs tests, Python compilation, and the full verification gate on every push.

---

## License

MIT License. See [LICENSE](LICENSE).

---

## Acknowledgements

- [Akoma Ntoso](http://www.akomantoso.org/) &mdash; XML standard for legislative documents
- [India Code](https://www.indiacode.nic.in) &mdash; Official legislative database by National Informatics Centre
- [Income Tax Department](https://www.incometaxindia.gov.in) &mdash; Publicly accessible tax law resources
- [CBIC](https://www.cbic.gov.in) &mdash; Central Board of Indirect Taxes and Customs
- Built with Python, Pydantic, Neo4j, Rich, and the Model Context Protocol
