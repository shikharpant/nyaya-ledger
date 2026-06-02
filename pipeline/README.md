# Pipeline

Pipeline commands are currently exposed through `main.py`:

```bash
python3 main.py corpus seed
python3 main.py corpus validate
python3 main.py corpus list
python3 main.py corpus diff <review-corpus> --base-corpus corpus
python3 main.py corpus query <canonical-id>
python3 main.py corpus export-text <canonical-id>
python3 main.py corpus ingest <source-file> <source-dir> <output-xml> --canonical-id <id>
python3 main.py source inventory data/Law
python3 main.py source inventory-validate derived/sources/source_inventory.json
python3 main.py source inventory-report derived/sources/source_inventory.json
python3 main.py corpus ingest-inventory derived/sources/source_inventory.json
python3 main.py source extract <source-dir>
python3 main.py graph rebuild
python3 main.py search rebuild
python3 main.py search query <query>
python3 main.py vector chunks
python3 main.py api export
python3 main.py html build
python3 main.py corpus quality
python3 main.py corpus unresolved-references
python3 main.py pipeline verify
```

Run the same local verification gate used by CI:

```bash
make verify
```

The GitHub Actions workflow in `.github/workflows/verify.yml` runs tests,
compilation, `pipeline verify`, and `git diff --check`.
