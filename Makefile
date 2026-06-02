.PHONY: verify test compile pipeline diff-check inventory

PYTHON ?= python3

verify: test compile pipeline diff-check

test:
	$(PYTHON) -m pytest

compile:
	$(PYTHON) -m compileall -q main.py src

pipeline:
	$(PYTHON) main.py pipeline verify

diff-check:
	git diff --check

inventory:
	$(PYTHON) main.py source inventory data/Law --output derived/sources/source_inventory.json
	$(PYTHON) main.py source inventory-validate derived/sources/source_inventory.json
	$(PYTHON) main.py source inventory-report derived/sources/source_inventory.json --output derived/sources/source_inventory_report.json
