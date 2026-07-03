"""
Git for Law - Steel Thread MVP

CLI interface for the Legal AST system with time-travel queries.

Usage:
    python main.py load-genesis          # Load initial state into Neo4j
    python main.py query <rule_id>       # Query a rule (current state)
    python main.py query <rule_id> --as-of 2025-10-31  # Time-travel query
    python main.py parse <notification>  # Parse a notification into mutations
    python main.py apply <mutations.json> --dry-run  # Validate mutations
    python main.py apply <mutations.json>            # Apply mutations
"""

import argparse
import json
import sys
import os
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))
from pathlib import Path
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text

console = Console()


def cmd_load_genesis(args):
    """Load genesis block into Neo4j."""
    from src.graph_loader import load_genesis
    
    console.print("[bold blue]Loading genesis block...[/bold blue]")
    load_genesis(args.data_dir)
    console.print("[bold green]✓ Genesis block loaded successfully![/bold green]")


def cmd_query(args):
    """Query a rule with optional time-travel."""
    from src.time_travel import TimeTravelEngine
    
    as_of = datetime.fromisoformat(args.as_of) if args.as_of else datetime.now()
    
    with TimeTravelEngine() as engine:
        if args.with_deps:
            result = engine.get_rule_with_dependencies(args.rule_id, as_of)
            rule = result["primary_rule"]
            
            if not rule:
                console.print(f"[red]Rule not found: {args.rule_id}[/red]")
                return
            
            # Display primary rule
            console.print(Panel(
                rule.to_text(),
                title=f"[bold]{rule.heading}[/bold] (v{rule.version}, as of {as_of.date()})",
                border_style="green"
            ))
            
            # Display linked forms
            if result["required_forms"]:
                table = Table(title="Required Forms")
                table.add_column("Form Number")
                table.add_column("Title")
                for form in result["required_forms"]:
                    table.add_row(form.form_number, form.title)
                console.print(table)
            
            # Display inherited rules (mutatis mutandis)
            if result["inherited_rules"]:
                console.print("\n[bold yellow]Inherited via MUTATIS MUTANDIS:[/bold yellow]")
                for inherited in result["inherited_rules"]:
                    irule = inherited["rule"]
                    console.print(f"  • {irule.heading} (via {inherited['via_subrule']})")
        
        else:
            rule = engine.get_rule(args.rule_id, as_of)
            
            if not rule:
                console.print(f"[red]Rule not found: {args.rule_id}[/red]")
                return
            
            console.print(Panel(
                rule.to_text(),
                title=f"[bold]{rule.heading}[/bold] (v{rule.version}, as of {as_of.date()})",
                border_style="green"
            ))


def cmd_query_form(args):
    """Query a form with optional time-travel."""
    from src.time_travel import TimeTravelEngine
    from src.models import schema_to_natural_language
    
    as_of = datetime.fromisoformat(args.as_of) if args.as_of else datetime.now()
    
    with TimeTravelEngine() as engine:
        form = engine.get_form(args.form_id, as_of)
        
        if not form:
            console.print(f"[red]Form not found: {args.form_id}[/red]")
            return
        
        console.print(Panel(
            f"[bold]{form.form_number}[/bold]\n{form.title}",
            title=f"Form (v{form.version}, as of {as_of.date()})",
            border_style="blue"
        ))
        
        for section in form.sections:
            schema = form.get_section_schema(section["section_label"])
            if schema:
                nl_description = schema_to_natural_language(schema)
                console.print(f"\n[bold cyan]{section['section_label']}[/bold cyan]: {section.get('heading', '')}")
                console.print(nl_description)


def cmd_compare(args):
    """Compare a rule at two different dates."""
    from src.time_travel import TimeTravelEngine
    
    date1 = datetime.fromisoformat(args.date1)
    date2 = datetime.fromisoformat(args.date2)
    
    with TimeTravelEngine() as engine:
        result = engine.compare_versions(args.rule_id, date1, date2)
        
        if result["versions_differ"]:
            console.print(f"[bold yellow]Versions differ![/bold yellow]")
            console.print(f"  {args.date1}: v{result['version1'].version}")
            console.print(f"  {args.date2}: v{result['version2'].version}")
            
            # Show diff
            console.print("\n[bold]Content at {args.date1}:[/bold]")
            console.print(result["version1"].to_text()[:500] + "...")
            
            console.print(f"\n[bold]Content at {args.date2}:[/bold]")
            console.print(result["version2"].to_text()[:500] + "...")
        else:
            console.print(f"[green]Same version at both dates[/green]")


def cmd_parse(args):
    """Parse a notification into mutations."""
    from src.mutation_parser import parse_notification_with_llm, parse_notification_offline, save_mutations_to_file
    import os
    
    # Read notification text
    with open(args.input, 'r', encoding='utf-8') as f:
        notification_text = f.read()
    
    console.print(f"[bold blue]Parsing notification: {args.input}[/bold blue]")
    
    if args.offline:
        # Use regex-based offline parser
        parsed = parse_notification_offline(notification_text)
        console.print("[yellow]Using offline parser (regex-based)[/yellow]")
    else:
        # Use DeepSeek / GPT
        api_key = os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENAI_API_KEY")
        if not api_key:
            console.print("[red]DEEPSEEK_API_KEY not set. Use --offline for regex parsing.[/red]")
            return
        
        parsed = parse_notification_with_llm(notification_text, api_key)
        console.print("[green]Using DeepSeek API parser[/green]")
    
    # Display results
    table = Table(title=f"Parsed Mutations ({len(parsed.mutations)} found)")
    table.add_column("ID")
    table.add_column("Operation")
    table.add_column("Target")
    table.add_column("Payload Preview")
    
    for mut in parsed.mutations:
        payload_preview = str(mut.payload)[:40] + "..." if len(str(mut.payload)) > 40 else str(mut.payload)
        table.add_row(
            mut.mutation_id,
            mut.operation,
            mut.target_node_path,
            payload_preview
        )
    
    console.print(table)
    
    # Save to output file
    if args.output:
        save_mutations_to_file(parsed, args.output)
        console.print(f"[green]✓ Saved to {args.output}[/green]")


def cmd_apply(args):
    """Apply mutations from a JSON file."""
    from src.mutation_parser import ParsedMutation, ParsedNotification
    from src.mutation_applier import MutationApplier
    
    # Load mutations
    with open(args.input, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    mutations = [
        ParsedMutation(
            mutation_id=m["mutation_id"],
            target_node_path=m["target_node_path"],
            operation=m["operation"],
            payload=m["payload"],
            anchor=m.get("anchor"),
            anchor_position=m.get("anchor_position"),
            position=m.get("position"),
            anchor_child_label=m.get("anchor_child_label"),
            original_text=m.get("original_text")
        )
        for m in data.get("mutations", [])
    ]
    
    parsed = ParsedNotification(
        notification_id=data.get("notification_id", "unknown"),
        notification_date=datetime.fromisoformat(data.get("notification_date", datetime.now().isoformat())),
        effective_date=datetime.fromisoformat(data.get("effective_date", datetime.now().isoformat())),
        target_statute=data.get("target_statute", "CGST_Rules_2017"),
        mutations=mutations,
        raw_response=data
    )
    
    console.print(f"[bold blue]Applying {len(mutations)} mutations...[/bold blue]")
    
    with MutationApplier() as applier:
        result = applier.apply_batch(parsed, dry_run=args.dry_run)
        
        # Display results
        status_color = "green" if result.all_successful else "yellow" if result.failed == 0 else "red"
        
        table = Table(title=f"Application Results ({'DRY RUN' if args.dry_run else 'APPLIED'})")
        table.add_column("Mutation ID")
        table.add_column("Status")
        table.add_column("New Version")
        table.add_column("Notes")
        
        for r in result.results:
            status = "✓" if r.success else "✗"
            status_style = "green" if r.success else "red"
            notes = r.error or r.review_reason or ""
            
            table.add_row(
                r.mutation_id,
                f"[{status_style}]{status}[/{status_style}]",
                r.new_version or "-",
                notes[:50]
            )
        
        console.print(table)
        
        console.print(f"\n[{status_color}]Total: {result.total_mutations}, "
                     f"Successful: {result.successful}, "
                     f"Failed: {result.failed}, "
                     f"Needs Review: {result.needs_review}[/{status_color}]")


def cmd_test(args):
    """Run Steel Thread test - verify time-travel works."""
    from src.time_travel import TimeTravelEngine
    
    console.print("[bold blue]Running Steel Thread Test...[/bold blue]\n")
    
    # Test dates
    before_amendment = datetime(2025, 10, 31)
    after_amendment = datetime(2025, 11, 1)
    
    with TimeTravelEngine() as engine:
        # Query Rule 10 at both dates
        rule_before = engine.get_rule("CGST_Rules/Rule_10", before_amendment)
        rule_after = engine.get_rule("CGST_Rules/Rule_10", after_amendment)
        
        if not rule_before:
            console.print("[red]✗ Rule 10 not found at 2025-10-31[/red]")
            console.print("  Have you loaded the genesis block? Run: python main.py load-genesis")
            return
        
        console.print("[bold]Rule 10 on 2025-10-31:[/bold]")
        console.print(f"  Version: {rule_before.version}")
        
        if rule_after and rule_after.version != rule_before.version:
            console.print(f"\n[bold]Rule 10 on 2025-11-01:[/bold]")
            console.print(f"  Version: {rule_after.version}")
            console.print("\n[green]✓ Time-travel working! Different versions returned for different dates.[/green]")
        else:
            console.print("\n[yellow]⚠ Same version at both dates (no amendments applied yet)[/yellow]")
            console.print("  Apply mutations to test time-travel: python main.py apply mutations.json")


def cmd_source_extract(args):
    """Extract source text into a source archive."""
    from src.legal_corpus.source_archive import extract_source_text

    archive_dir = Path(args.source_dir)
    extracted = extract_source_text(archive_dir)
    console.print(f"[green]Extracted {len(extracted['text'])} characters[/green]")
    console.print(f"  Output: {archive_dir / 'extracted_text.json'}")


def cmd_source_add(args):
    """Archive a source file with profile metadata."""
    from src.legal_corpus.source_archive import archive_source

    metadata = {
        "canonical_id": args.canonical_id,
        "document_type": args.document_type,
        "title": args.title or Path(args.input).stem,
        "jurisdiction": args.jurisdiction,
        "language": args.language,
        "publication_date": args.publication_date or "",
        "effective_from": args.effective_from or "",
        "issuing_authority": args.issuing_authority,
        "review_status": "raw",
        "parser_version": "unparsed",
        "source_url": args.source_url or str(Path(args.input)),
        "source_type": "archived-source",
    }
    output = archive_source(Path(args.input), Path(args.source_dir), metadata)
    console.print(f"[green]Archived source:[/green] {output}")
    console.print(f"  Metadata: {Path(args.source_dir) / 'metadata.yaml'}")


def cmd_corpus_parse(args):
    """Create deterministic structure spans for a source archive."""
    from src.legal_corpus.source_archive import extract_source_text, read_metadata_yaml, write_structure_json
    from src.legal_corpus.structure_parser import parse_structure, validate_structure_spans

    source_dir = Path(args.source_dir)
    extracted_path = source_dir / "extracted_text.json"
    if extracted_path.exists():
        data = json.loads(extracted_path.read_text(encoding="utf-8"))
    else:
        data = extract_source_text(source_dir)

    metadata = read_metadata_yaml(source_dir / "metadata.yaml")
    structure = parse_structure(
        data,
        document_type=metadata.get("document_type", args.document_type),
        mode=args.mode,
        model=args.model,
        provider=args.provider,
        base_url=args.base_url,
    )
    errors, warnings = validate_structure_spans(data, structure)
    if errors:
        for error in errors:
            console.print(f"[red]Error:[/red] {error}")
        raise SystemExit(1)
    for warning in warnings:
        console.print(f"[yellow]Warning:[/yellow] {warning}")
    output = write_structure_json(source_dir, structure)
    console.print(f"[green]Parsed {len(structure['nodes'])} structural spans[/green]")
    console.print(f"  Parser: {structure.get('parser')}")
    console.print(f"  References: {len(structure.get('references', []))}")
    console.print(f"  Output: {output}")


def cmd_source_validate(args):
    """Validate a source archive or sources directory."""
    from src.legal_corpus.validator import validate_source_archive, validate_sources

    path = Path(args.path)
    result = validate_source_archive(path) if (path / "extracted_text.json").exists() else validate_sources(path)
    status = "green" if result.ok else "red"
    console.print(f"[{status}]Checked {result.checked_archives} source archives[/{status}]")
    for warning in result.warnings:
        console.print(f"[yellow]Warning:[/yellow] {warning}")
    for error in result.errors:
        console.print(f"[red]Error:[/red] {error}")
    if not result.ok:
        raise SystemExit(1)


def cmd_source_inventory(args):
    """Build a reviewable inventory of local source documents."""
    from src.legal_corpus.source_inventory import build_source_inventory, validate_source_inventory, write_source_inventory

    inventory = build_source_inventory(
        Path(args.root_dir),
        index_csv=Path(args.index_csv) if args.index_csv else None,
        sources_root=Path(args.sources_root),
        corpus_root=Path(args.corpus_root),
        include_unclassified=not args.no_unclassified,
        compute_checksums=not args.no_checksums,
        limit=args.limit,
    )
    if args.output:
        write_source_inventory(inventory, Path(args.output))
    validation = validate_source_inventory(inventory)

    if args.json:
        payload = dict(inventory)
        payload["validation"] = validation.to_dict()
        _print_json(payload)
        return

    stats = inventory["stats"]
    table = Table(title="Source Inventory")
    table.add_column("Metric")
    table.add_column("Value")
    table.add_row("Items", str(stats["items"]))
    table.add_row("Ready", str(stats["ready"]))
    table.add_row("Missing", str(stats["missing"]))
    table.add_row("Unclassified", str(stats["unclassified"]))
    table.add_row("Index", inventory["index_csv"] or "-")
    if args.output:
        table.add_row("Output", args.output)
    console.print(table)

    categories = Table(title="Inventory Categories")
    categories.add_column("Category")
    categories.add_column("Count")
    for category, count in stats["categories"].items():
        categories.add_row(category, str(count))
    console.print(categories)

    for warning in validation.warnings:
        console.print(f"[yellow]Warning:[/yellow] {warning}")
    for error in validation.errors:
        console.print(f"[red]Error:[/red] {error}")
    if not validation.ok:
        raise SystemExit(1)


def cmd_source_inventory_validate(args):
    """Validate a source inventory JSON file."""
    from src.legal_corpus.source_inventory import validate_source_inventory_file

    result = validate_source_inventory_file(Path(args.inventory))
    if args.json:
        _print_json(result.to_dict())
        return

    status = "green" if result.ok else "red"
    console.print(f"[{status}]Checked {result.checked_items} inventory items[/{status}]")
    for warning in result.warnings:
        console.print(f"[yellow]Warning:[/yellow] {warning}")
    for error in result.errors:
        console.print(f"[red]Error:[/red] {error}")
    if not result.ok:
        raise SystemExit(1)


def cmd_source_inventory_report(args):
    """Build a compact source inventory review report."""
    from src.legal_corpus.source_inventory import build_inventory_report_file, write_inventory_report

    report = build_inventory_report_file(Path(args.inventory))
    if args.output:
        write_inventory_report(report, Path(args.output))
    if args.json:
        _print_json(report)
        return

    stats = report["stats"]
    table = Table(title="Source Inventory Report")
    table.add_column("Metric")
    table.add_column("Value")
    table.add_row("Items", str(stats["items"]))
    table.add_row("Ready", str(stats["ready"]))
    table.add_row("Missing", str(stats["missing"]))
    table.add_row("Unclassified", str(stats["unclassified"]))
    table.add_row("Validation", "ok" if report["validation"]["ok"] else "failed")
    if args.output:
        table.add_row("Output", args.output)
    console.print(table)

    if report["missing"]:
        missing_table = Table(title="Missing Sources")
        missing_table.add_column("Category")
        missing_table.add_column("Notification")
        missing_table.add_column("Date")
        missing_table.add_column("Expected PDF")
        for item in report["missing"][:20]:
            missing_table.add_row(
                item.get("category", ""),
                item.get("notification_no", ""),
                item.get("publication_date", ""),
                item.get("expected_pdf_filename", ""),
            )
        console.print(missing_table)
        if len(report["missing"]) > 20:
            console.print(f"  Showing 20 of {len(report['missing'])} missing sources")

    if not report["validation"]["ok"]:
        for error in report["validation"]["errors"]:
            console.print(f"[red]Error:[/red] {error}")
        raise SystemExit(1)


def cmd_corpus_render(args):
    """Render a source archive into canonical XML."""
    from src.legal_corpus.renderer import render_source_document, write_xml
    from src.legal_corpus.source_archive import extract_source_text, read_metadata_yaml
    from src.mutation_parser import parse_notification_offline

    source_dir = Path(args.source_dir)
    extracted_path = source_dir / "extracted_text.json"
    structure_path = source_dir / "structure.json"

    extracted = (
        json.loads(extracted_path.read_text(encoding="utf-8"))
        if extracted_path.exists()
        else extract_source_text(source_dir)
    )
    structure = json.loads(structure_path.read_text(encoding="utf-8")) if structure_path.exists() else None
    metadata = read_metadata_yaml(source_dir / "metadata.yaml")
    metadata["source_sha256"] = extracted.get("source_sha256", metadata.get("source_sha256", ""))
    if metadata.get("document_type") == "notification":
        parsed = parse_notification_offline(extracted["text"])
        metadata["amendments"] = [
            {"operation": mutation.operation, "target": mutation.target_node_path}
            for mutation in parsed.mutations
        ]
    tree = render_source_document(extracted["text"], metadata, structure)
    output = write_xml(tree, Path(args.output))
    console.print(f"[green]Rendered canonical XML:[/green] {output}")


def cmd_corpus_ingest(args):
    """Archive, parse, render, and validate a source file."""
    from src.legal_corpus.ingest import ingest_source_file, write_ingest_report

    metadata = {
        "canonical_id": args.canonical_id,
        "document_type": args.document_type,
        "title": args.title or Path(args.input).stem,
        "jurisdiction": args.jurisdiction,
        "language": args.language,
        "publication_date": args.publication_date or "",
        "effective_from": args.effective_from or "",
        "issuing_authority": args.issuing_authority,
        "review_status": "raw",
        "parser_version": "unparsed",
        "source_url": args.source_url or str(Path(args.input)),
        "source_type": "archived-source",
    }
    report = ingest_source_file(
        Path(args.input),
        Path(args.source_dir),
        Path(args.output),
        metadata,
        mode=args.mode,
        provider=args.provider,
        model=args.model,
        base_url=args.base_url,
    )
    if args.report:
        write_ingest_report(report, Path(args.report))

    table = Table(title="Ingested Source")
    table.add_column("Metric")
    table.add_column("Value")
    table.add_row("XML", report["xml"])
    table.add_row("Source archive", report["source_dir"])
    table.add_row("Parser", report["parser"])
    table.add_row("Nodes", str(report["nodes"]))
    table.add_row("References", str(report["references"]))
    console.print(table)
    for warning in report["warnings"]:
        console.print(f"[yellow]Warning:[/yellow] {warning}")
    if args.report:
        console.print(f"  Report: {args.report}")


def cmd_corpus_ingest_inventory(args):
    """Preview or execute batch ingestion from a source inventory."""
    from src.legal_corpus.batch_ingest import ingest_inventory, write_batch_ingest_report

    def progress(event):
        if not args.progress:
            return
        console.print(
            f"[{event['index']}/{event['total']}] {event['status']} "
            f"{event.get('canonical_id', '')}",
            markup=False,
        )

    report = ingest_inventory(
        Path(args.inventory),
        execute=args.execute,
        limit=args.limit,
        status=args.status,
        document_type=args.document_type,
        category=args.category,
        mode=args.mode,
        provider=args.provider,
        model=args.model,
        base_url=args.base_url,
        skip_existing=not args.no_skip_existing,
        continue_on_error=args.continue_on_error,
        progress=progress if args.progress else None,
    )
    if args.report:
        write_batch_ingest_report(report, Path(args.report))

    if args.json:
        _print_json(report)
        return

    mode = "EXECUTE" if args.execute else "DRY RUN"
    table = Table(title=f"Inventory Ingest {mode}")
    table.add_column("Status")
    table.add_column("Count")
    for key, value in report["stats"].items():
        table.add_row(key, str(value))
    console.print(table)

    items = Table(title="Selected Items")
    items.add_column("Status")
    items.add_column("Canonical ID")
    items.add_column("Output")
    for item in report["items"][:20]:
        items.add_row(item["status"], item.get("canonical_id", ""), item.get("output_path", ""))
    console.print(items)
    if len(report["items"]) > 20:
        console.print(f"  Showing 20 of {len(report['items'])} selected items")
    if args.report:
        console.print(f"  Report: {args.report}")

    if report["stats"]["failed"] or report["stats"]["not_ingestible"]:
        raise SystemExit(1)


def cmd_corpus_seed(args):
    """Seed canonical corpus files from existing prototype data."""
    from src.legal_corpus.seed import seed_from_existing_data

    outputs = seed_from_existing_data(
        Path.cwd(),
        corpus_dir=Path(args.corpus_dir),
        sources_dir=Path(args.sources_dir),
    )
    table = Table(title="Seeded Canonical Artifacts")
    table.add_column("Path")
    for path in outputs:
        table.add_row(str(path))
    console.print(table)


def cmd_corpus_validate(args):
    """Validate canonical corpus XML files."""
    from src.legal_corpus.validator import validate_corpus

    result = validate_corpus(Path(args.corpus_dir))
    status = "green" if result.ok else "red"
    console.print(f"[{status}]Checked {result.checked_files} XML files[/{status}]")

    for warning in result.warnings:
        console.print(f"[yellow]Warning:[/yellow] {warning}")
    for error in result.errors:
        console.print(f"[red]Error:[/red] {error}")

    if not result.ok:
        raise SystemExit(1)


def cmd_corpus_quality(args):
    """Build review metrics for generated corpus XML."""
    from src.legal_corpus.quality import audit_corpus_quality, write_quality_report

    report = audit_corpus_quality(Path(args.corpus_dir), max_paragraph_chars=args.max_paragraph_chars)
    if args.output:
        write_quality_report(report, Path(args.output))

    if args.json:
        _print_json(report)
        return

    table = Table(title="Corpus Quality")
    table.add_column("Metric")
    table.add_column("Value")
    for key, value in report["stats"].items():
        table.add_row(key.replace("_", " ").title(), str(value))
    table.add_row("Flagged Documents", str(len(report["flagged_documents"])))
    if args.output:
        table.add_row("Output", args.output)
    console.print(table)

    if report["flagged_documents"]:
        flagged = Table(title="Flagged Documents")
        flagged.add_column("Canonical ID")
        flagged.add_column("Paragraphs", justify="right")
        flagged.add_column("Max Para", justify="right")
        flagged.add_column("Long", justify="right")
        flagged.add_column("Joined", justify="right")
        for item in report["flagged_documents"][: args.limit]:
            flagged.add_row(
                item["canonical_id"],
                str(item["paragraphs"]),
                str(item["max_paragraph_chars"]),
                str(item["long_paragraphs"]),
                str(item["joined_token_hits"]),
            )
        console.print(flagged)


def cmd_corpus_unresolved_references(args):
    """Build an unresolved canonical reference report."""
    from src.legal_corpus.references import (
        build_unresolved_reference_report,
        write_unresolved_reference_report,
        write_unresolved_reference_summary,
    )

    report = build_unresolved_reference_report(Path(args.corpus_dir), sample_limit=args.sample_limit)
    if args.output:
        write_unresolved_reference_report(report, Path(args.output))
    summary_output = getattr(args, "summary_output", None)
    if summary_output:
        write_unresolved_reference_summary(report, Path(summary_output))
    if args.json:
        _print_json(report)
        return

    stats = report["stats"]
    summary = Table(title="Unresolved References")
    summary.add_column("Metric")
    summary.add_column("Value")
    summary.add_row("Documents", str(stats["documents"]))
    summary.add_row("References", str(stats["references"]))
    summary.add_row("Alias Resolved Occurrences", str(stats.get("alias_resolved_occurrences", 0)))
    summary.add_row("Unresolved Occurrences", str(stats["unresolved_occurrences"]))
    summary.add_row("Unresolved Targets", str(stats["unresolved_targets"]))
    if args.output:
        summary.add_row("Output", args.output)
    if summary_output:
        summary.add_row("Summary", summary_output)
    console.print(summary)

    targets = Table(title="Top Unresolved Targets")
    targets.add_column("Occurrences", justify="right")
    targets.add_column("Sources", justify="right")
    targets.add_column("Kind")
    targets.add_column("Target")
    for item in report["targets"][: args.limit]:
        targets.add_row(
            str(item["occurrences"]),
            str(item["source_documents"]),
            item["kind"],
            item["target"],
        )
    console.print(targets)

    docs = Table(title="Top Missing Target Documents")
    docs.add_column("Targets", justify="right")
    docs.add_column("Target Document")
    for item in report["top_target_documents"][: args.limit]:
        docs.add_row(str(item["unresolved_targets"]), item["target_document"])
    console.print(docs)


def cmd_corpus_split_forms(args):
    """Split an aggregate FORM GST source archive into canonical form XML files."""
    from src.legal_corpus.forms import split_forms_archive, write_form_split_report

    report = split_forms_archive(
        Path(args.source_dir),
        Path(args.corpus_dir),
        overwrite=args.overwrite,
    )
    if args.output:
        write_form_split_report(report, Path(args.output))
    if args.json:
        _print_json(report)
        return

    table = Table(title="Form Split")
    table.add_column("Metric")
    table.add_column("Value")
    table.add_row("Generated", str(report["stats"]["generated"]))
    table.add_row("Skipped", str(report["stats"]["skipped"]))
    if args.output:
        table.add_row("Output", args.output)
    console.print(table)


def cmd_corpus_promote_batch(args):
    """Plan or apply promotion of clean generated batch XML."""
    from src.legal_corpus.review import apply_batch_promotion, plan_batch_promotion, write_promotion_plan

    plan = plan_batch_promotion(
        Path(args.ingest_report),
        Path(args.quality_report),
        target_corpus_dir=Path(args.target_corpus),
        target_sources_dir=Path(args.target_sources) if args.target_sources else None,
        include_flagged=args.include_flagged,
        overwrite=args.overwrite,
    )
    if args.output:
        write_promotion_plan(plan, Path(args.output))

    result = apply_batch_promotion(plan, approve=args.approve)
    if args.json:
        _print_json({"plan": plan, "result": result})
        return

    mode = "APPLY" if args.approve else "DRY RUN"
    table = Table(title=f"Batch Promotion {mode}")
    table.add_column("Metric")
    table.add_column("Value")
    for key, value in plan["stats"].items():
        table.add_row(key.replace("_", " ").title(), str(value))
    table.add_row("Copied XML", str(result["stats"]["copied_xml"]))
    table.add_row("Copied Sources", str(result["stats"]["copied_sources"]))
    table.add_row("Target Corpus", plan["target_corpus_dir"])
    if plan.get("target_sources_dir"):
        table.add_row("Target Sources", plan["target_sources_dir"])
    if args.output:
        table.add_row("Plan", args.output)
    console.print(table)

    if plan["excluded"]:
        excluded = Table(title="Excluded Documents")
        excluded.add_column("Reason")
        excluded.add_column("Canonical ID")
        for item in plan["excluded"][: args.limit]:
            excluded.add_row(item.get("reason", ""), item.get("canonical_id", ""))
        console.print(excluded)


def _print_json(data):
    console.print(json.dumps(data, indent=2, ensure_ascii=False), soft_wrap=True, markup=False)


def _preview_text(value: str, full: bool = False, limit: int = 1800) -> str:
    if full or len(value) <= limit:
        return value
    return value[:limit].rstrip() + "\n..."


def _print_reference_table(references):
    if not references:
        return
    table = Table(title="References")
    table.add_column("Type")
    table.add_column("Target")
    table.add_column("Show As")
    for ref in references:
        table.add_row(ref.get("type", ""), ref.get("target", ""), ref.get("showAs", ""))
    console.print(table)


def cmd_corpus_list(args):
    """List canonical corpus documents."""
    from src.legal_corpus.query import list_documents

    documents = list_documents(Path(args.corpus_dir), document_type=args.document_type)
    if args.json:
        _print_json(documents)
        return

    table = Table(title=f"Corpus Documents ({len(documents)})")
    table.add_column("Type")
    table.add_column("Canonical ID")
    table.add_column("Title")
    table.add_column("Effective")
    table.add_column("Review")
    for document in documents:
        table.add_row(
            document.get("document_type", ""),
            document.get("canonical_id", ""),
            document.get("title", ""),
            document.get("effective_from", ""),
            document.get("review_status", ""),
        )
    console.print(table)


def cmd_corpus_diff(args):
    """Compare a reviewed corpus against the canonical corpus."""
    from src.legal_corpus.diff import compare_corpora, write_diff_report

    if args.output:
        report = write_diff_report(
            Path(args.base_corpus),
            Path(args.review_corpus),
            Path(args.output),
            context=args.context,
            max_diff_lines=args.max_diff_lines,
        )
    else:
        report = compare_corpora(
            Path(args.base_corpus),
            Path(args.review_corpus),
            context=args.context,
            max_diff_lines=args.max_diff_lines,
        )

    if args.json:
        _print_json(report)
        return

    table = Table(title="Corpus Diff")
    table.add_column("Change")
    table.add_column("Count")
    for key in ["added", "modified", "removed", "unchanged"]:
        table.add_row(key.title(), str(report["stats"][key]))
    console.print(table)

    changed_docs = Table(title="Changed Documents")
    changed_docs.add_column("Change")
    changed_docs.add_column("Type")
    changed_docs.add_column("Canonical ID")
    changed_docs.add_column("Details")

    for document in report["added"]:
        changed_docs.add_row("Added", document.get("document_type", ""), document["canonical_id"], document["relative_path"])
    for document in report["removed"]:
        changed_docs.add_row(
            "Removed",
            document.get("document_type", ""),
            document["canonical_id"],
            document["relative_path"],
        )
    for document in report["modified"]:
        provision_counts = document["provisions"]
        details = (
            f"text={document['text_changed']}, "
            f"provisions +{len(provision_counts['added'])} "
            f"~{len(provision_counts['modified'])} "
            f"-{len(provision_counts['removed'])}"
        )
        changed_docs.add_row("Modified", document.get("document_type", ""), document["canonical_id"], details)

    if report["added"] or report["removed"] or report["modified"]:
        console.print(changed_docs)
    if args.output:
        console.print(f"  Output: {args.output}")


def cmd_corpus_query(args):
    """Query a canonical document or provision directly from corpus XML."""
    from src.legal_corpus.query import lookup_canonical_id

    entry = lookup_canonical_id(Path(args.corpus_dir), args.canonical_id)
    if not entry:
        console.print(f"[red]Canonical ID not found:[/red] {args.canonical_id}")
        raise SystemExit(1)

    if args.json:
        _print_json(entry)
        return

    role = args.role
    document = entry.get("document")
    provision = entry.get("provision")
    if role != "auto" and not entry.get(role):
        console.print(f"[red]Role not available for this ID:[/red] {role}")
        raise SystemExit(1)
    show_document = bool(document) and role in {"auto", "document"}
    show_provision = bool(provision) and (role == "provision" or not show_document)

    console.print(f"[bold]Canonical ID:[/bold] {entry['canonical_id']}")
    console.print(f"[bold]Roles:[/bold] {', '.join(entry.get('roles', []))}")

    if show_document:
        table = Table(title="Document")
        table.add_column("Field")
        table.add_column("Value")
        for key in ["document_type", "title", "effective_from", "publication_date", "review_status", "path"]:
            table.add_row(key, document.get(key, ""))
        table.add_row("children", str(len(document.get("children", []))))
        table.add_row("references", str(len(document.get("references", []))))
        console.print(table)
        console.print(Panel(
            Text(_preview_text(document.get("text", ""), full=args.full)),
            title="Text",
            border_style="green",
        ))
        _print_reference_table(document.get("references", []))
        if provision:
            console.print(
                f"[dim]Also indexed as provision {provision.get('element_tag', '')} "
                f"{provision.get('eId', '')} in this document. Use --role provision to view that node.[/dim]"
            )

    if show_provision:
        table = Table(title="Provision")
        table.add_column("Field")
        table.add_column("Value")
        for key in ["document_id", "document_title", "document_type", "element_tag", "eId", "number", "title", "path"]:
            table.add_row(key, provision.get(key, ""))
        table.add_row("children", str(len(provision.get("children", []))))
        table.add_row("references", str(len(provision.get("references", []))))
        console.print(table)
        console.print(Panel(
            Text(_preview_text(provision.get("text", ""), full=args.full)),
            title="Text",
            border_style="cyan",
        ))
        _print_reference_table(provision.get("references", []))


def cmd_corpus_export_text(args):
    """Export plain text for a canonical document or provision."""
    from src.legal_corpus.query import export_text, lookup_canonical_id

    text = export_text(Path(args.corpus_dir), args.canonical_id, role=args.role)
    if text is None:
        if lookup_canonical_id(Path(args.corpus_dir), args.canonical_id):
            console.print(f"[red]Role not available for this ID:[/red] {args.role}")
            raise SystemExit(1)
        console.print(f"[red]Canonical ID not found:[/red] {args.canonical_id}")
        raise SystemExit(1)
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text + "\n", encoding="utf-8")
        console.print(f"[green]Exported text:[/green] {output}")
        return
    console.print(Text(text))


def cmd_graph_rebuild(args):
    """Rebuild derived graph JSON from the canonical corpus."""
    from src.legal_corpus.graph_index import rebuild_graph_index

    graph = rebuild_graph_index(Path(args.corpus_dir), Path(args.output))
    console.print(
        f"[green]Graph index rebuilt:[/green] "
        f"{len(graph['nodes'])} nodes, {len(graph['edges'])} edges"
    )
    console.print(f"  Output: {args.output}")


def cmd_graph_cypher(args):
    """Export Neo4j Cypher payload from the canonical corpus."""
    from src.legal_corpus.graph_index import write_neo4j_payload

    payload = write_neo4j_payload(Path(args.corpus_dir), Path(args.output))
    console.print(f"[green]Neo4j payload exported:[/green] {len(payload['statements'])} statements")
    console.print(f"  Output: {args.output}")


def cmd_graph_load(args):
    """Load derived corpus graph into Neo4j."""
    from src.legal_corpus.graph_index import load_graph_to_neo4j

    result = load_graph_to_neo4j(
        Path(args.corpus_dir),
        uri=args.uri,
        user=args.user,
        password=args.password,
        clear=args.clear,
    )
    console.print(f"[green]Loaded graph into Neo4j:[/green] {result['nodes']} nodes, {result['edges']} edges")


def cmd_search_rebuild(args):
    """Rebuild derived search JSONL from the canonical corpus."""
    from src.legal_corpus.search_index import write_search_index

    records = write_search_index(Path(args.corpus_dir), Path(args.output))
    table = Table(title="Search Index")
    table.add_column("Metric")
    table.add_column("Value")
    table.add_row("Records", str(len(records)))
    table.add_row("Output", args.output)
    console.print(table)


def cmd_search_query(args):
    """Search a derived corpus search index."""
    from src.legal_corpus.search_index import search_index

    results = search_index(
        Path(args.index),
        args.query,
        limit=args.limit,
        document_type=args.document_type,
        role=args.role,
    )
    if args.json:
        _print_json(results)
        return

    table = Table(title=f"Search Results ({len(results)})")
    table.add_column("Score")
    table.add_column("Role")
    table.add_column("Type")
    table.add_column("Canonical ID")
    table.add_column("Title")
    table.add_column("Snippet")
    for result in results:
        table.add_row(
            str(result.get("score", "")),
            result.get("role", ""),
            result.get("document_type", ""),
            result.get("canonical_id", ""),
            result.get("title", ""),
            result.get("snippet", ""),
        )
    console.print(table)


def cmd_vector_chunks(args):
    """Export vector/RAG-ready JSONL chunks from canonical corpus XML."""
    from src.legal_corpus.vector_index import write_vector_chunks

    chunks = write_vector_chunks(
        Path(args.corpus_dir),
        Path(args.output),
        max_chars=args.max_chars,
        overlap=args.overlap,
        include_documents=not args.no_documents,
        include_provisions=not args.no_provisions,
    )
    table = Table(title="Vector Chunks")
    table.add_column("Metric")
    table.add_column("Value")
    table.add_row("Chunks", str(len(chunks)))
    table.add_row("Output", args.output)
    table.add_row("Max chars", str(args.max_chars))
    table.add_row("Overlap", str(args.overlap))
    console.print(table)


def cmd_api_export(args):
    """Export API-ready JSON payload from canonical corpus XML."""
    from src.legal_corpus.api_payload import write_api_payload

    payload = write_api_payload(Path(args.corpus_dir), Path(args.output))
    if args.json:
        _print_json(payload)
        return

    table = Table(title="API Payload")
    table.add_column("Metric")
    table.add_column("Value")
    table.add_row("Documents", str(payload["stats"]["documents"]))
    table.add_row("Provisions", str(payload["stats"]["provisions"]))
    table.add_row("References", str(payload["stats"]["references"]))
    table.add_row("Output", args.output)
    console.print(table)


def cmd_html_build(args):
    """Render static HTML from canonical corpus XML."""
    from src.legal_corpus.html_renderer import write_html_site

    result = write_html_site(Path(args.corpus_dir), Path(args.output_dir))
    table = Table(title="HTML Site")
    table.add_column("Metric")
    table.add_column("Value")
    table.add_row("Files", str(result["files"]))
    table.add_row("Documents", str(result["documents"]))
    table.add_row("Output", args.output_dir)
    console.print(table)


def cmd_pipeline_verify(args):
    """Run the canonical corpus verification gate."""
    from src.legal_corpus.verification import run_verification

    result = run_verification(
        corpus_dir=Path(args.corpus_dir),
        sources_dir=Path(args.sources_dir),
        derived_dir=Path(args.derived_dir),
        manifest_path=Path(args.manifest) if args.manifest else None,
        inventory_path=Path(args.inventory) if args.inventory else None,
        strict_warnings=args.strict_warnings,
        vector_max_chars=args.vector_max_chars,
        vector_overlap=args.vector_overlap,
    )
    if args.json:
        _print_json(result.to_dict())
    else:
        table = Table(title="Pipeline Verification")
        table.add_column("Step")
        table.add_column("Status")
        table.add_column("Counts")
        table.add_column("Output")
        for step in result.steps:
            status = "[green]ok[/green]" if step.ok else "[red]failed[/red]"
            counts = ", ".join(f"{key}={value}" for key, value in step.counts.items())
            table.add_row(step.name, status, counts, step.output)
        console.print(table)

        for warning in result.warnings:
            console.print(f"[yellow]Warning:[/yellow] {warning}")
        for error in result.errors:
            console.print(f"[red]Error:[/red] {error}")
        if args.manifest:
            console.print(f"  Manifest: {args.manifest}")

    if not result.ok:
        raise SystemExit(1)


def cmd_amendment_plan(args):
    """Plan canonical corpus amendments from a notification source archive."""
    from src.legal_corpus.amendments import plan_amendments, write_plan

    plan = plan_amendments(Path(args.source_dir), Path(args.corpus_dir))
    if args.output:
        write_plan(plan, Path(args.output))

    table = Table(title="Amendment Plan")
    table.add_column("Mutation")
    table.add_column("Operation")
    table.add_column("Target")
    table.add_column("Status")
    table.add_column("Notes")
    for item in plan.items:
        table.add_row(
            item.mutation_id,
            item.operation,
            item.canonical_target,
            item.status,
            "; ".join(item.notes)[:80],
        )
    console.print(table)
    console.print(f"Ready: {plan.ready_count}, Unresolved: {plan.unresolved_count}")
    if args.output:
        console.print(f"  Output: {args.output}")


def cmd_amendment_apply(args):
    """Apply supported amendments into a separate output corpus."""
    from src.legal_corpus.amendments import apply_amendments, write_plan

    plan = apply_amendments(
        Path(args.source_dir),
        Path(args.corpus_dir),
        Path(args.output_corpus),
        allow_partial=args.allow_partial,
    )
    if args.plan_output:
        write_plan(plan, Path(args.plan_output))

    if plan.unresolved_count and not args.allow_partial:
        console.print("[red]Amendment application blocked by unresolved mutations.[/red]")
        console.print("Use --allow-partial to apply supported mutations into the output corpus.")
        if args.plan_output:
            console.print(f"  Plan: {args.plan_output}")
        raise SystemExit(1)

    table = Table(title="Applied Amendments")
    table.add_column("Mutation")
    table.add_column("Operation")
    table.add_column("Status")
    table.add_column("Output")
    for item in plan.items:
        table.add_row(item.mutation_id, item.operation, item.status, item.output_file or "-")
    console.print(table)
    console.print(f"  Output corpus: {args.output_corpus}")


def cmd_amendment_promote(args):
    """Validate and promote a reviewed output corpus into the canonical corpus."""
    from src.legal_corpus.amendments import promote_amended_corpus

    result = promote_amended_corpus(
        Path(args.review_corpus),
        Path(args.target_corpus),
        Path(args.manifest),
        approve=args.approve,
        git_commit=args.git_commit,
        commit_message=args.message,
        repo_root=Path.cwd(),
    )

    status = "green" if result.ok else "red"
    mode = "APPROVED" if args.approve else "DRY RUN"
    table = Table(title=f"Promotion {mode}")
    table.add_column("Change")
    table.add_column("Count")
    table.add_row("Added", str(len(result.added)))
    table.add_row("Modified", str(len(result.modified)))
    table.add_row("Removed", str(len(result.removed)))
    table.add_row("Unchanged", str(len(result.unchanged)))
    console.print(table)
    for warning in result.warnings:
        console.print(f"[yellow]Warning:[/yellow] {warning}")
    for error in result.errors:
        console.print(f"[red]Error:[/red] {error}")
    if result.commit_sha:
        console.print(f"[green]Committed:[/green] {result.commit_sha}")
    console.print(f"[{status}]Manifest:[/{status}] {args.manifest}")
    if not result.ok:
        raise SystemExit(1)


def cmd_version_registry_validate(args):
    """Validate the curated legal work identity registry."""
    from src.legal_corpus.identity_registry import validate_registry

    result = validate_registry(Path(args.registry))
    console.print_json(json.dumps(result, ensure_ascii=False))
    if not result["ok"]:
        raise SystemExit(1)


def cmd_version_llm_smoke(args):
    """Smoke-test the configured OMLX endpoint."""
    from src.legal_corpus.omlx_client import OmlxConfig, OmlxError, smoke_test

    config = OmlxConfig.from_env(
        base_url=args.llm_base_url,
        model=args.llm_model,
        api_key_env=args.llm_api_key_env,
        timeout=args.timeout,
    )
    try:
        result = smoke_test(config)
    except OmlxError as exc:
        result = {
            "ok": False,
            "reason": getattr(exc, "reason", "llm_unavailable"),
            "error": str(exc),
            "base_url": config.base_url,
            "model": config.model,
        }
    console.print_json(json.dumps(result, ensure_ascii=False))
    if not result.get("ok"):
        raise SystemExit(1)


def cmd_version_compile_events(args):
    """Compile deterministic amendment events from CBIC notification JSON."""
    if args.source_family in {"finance-acts", "cbic-acts", "taxation-acts"}:
        from src.legal_corpus.act_amendment_events import compile_act_events

        result = compile_act_events(
            registry_path=Path(args.registry),
            source_dir=Path(args.source_dir),
            source_family=args.source_family,
            target_work=args.target_work,
            output=Path(args.output),
            review_output=Path(args.review_output),
            commencement_dir=Path(args.commencement_dir),
        )
        console.print_json(json.dumps(result, ensure_ascii=False))
        return

    from src.legal_corpus.amendment_events import compile_events

    result = compile_events(
        registry_path=Path(args.registry),
        source_dir=Path(args.source_dir),
        category=args.category,
        target_work=args.target_work,
        output=Path(args.output),
        review_output=Path(args.review_output),
        corpus_dir=Path(args.corpus_dir),
        source_archive_root=Path(args.source_archive_root),
        extract_pdf_text=args.extract_pdf_text,
        use_llm=args.use_llm,
        llm_base_url=args.llm_base_url,
        llm_model=args.llm_model,
        llm_api_key_env=args.llm_api_key_env,
        llm_cache=Path(args.llm_cache) if args.llm_cache else None,
        llm_limit=args.llm_limit,
        llm_concurrency=args.llm_concurrency,
    )
    console.print_json(json.dumps({k: v for k, v in result.items() if k != "review_report"}, ensure_ascii=False))


def cmd_version_build_baseline(args):
    """Build a 2017 baseline for a registered work."""
    from src.legal_corpus.baselines import build_baseline

    result = build_baseline(
        target_work=args.target_work,
        registry_path=Path(args.registry),
        output_dir=Path(args.output_dir) if args.output_dir else None,
        rules_pdf=Path(args.rules_pdf),
        rules_repair_notification=Path(args.rules_repair_notification) if args.rules_repair_notification else None,
        act_json=Path(args.act_json),
        use_llm=args.use_llm,
        llm_base_url=args.llm_base_url,
        llm_model=args.llm_model,
        llm_api_key_env=args.llm_api_key_env,
    )
    console.print_json(json.dumps(result, ensure_ascii=False))
    if not result.get("ok"):
        raise SystemExit(1)


def cmd_version_materialize(args):
    """Materialize component versions and dated snapshots."""
    from src.legal_corpus.version_snapshots import materialize_versions

    manifest = materialize_versions(
        target_work=args.target_work,
        events_path=Path(args.events),
        registry_path=Path(args.registry),
        corpus_dir=Path(args.corpus_dir),
        output_dir=Path(args.output_dir),
        write_snapshots=args.write_snapshots,
    )
    console.print_json(json.dumps(manifest, ensure_ascii=False))


def cmd_version_context_recovery(args):
    """Recover full-text rule context and route reviewed work-ID events."""
    from src.legal_corpus.context_recovery import recover_context

    result = recover_context(
        events_path=Path(args.events),
        output=Path(args.output),
        decisions_output=Path(args.decisions_output),
        report_output=Path(args.report_output),
        source_archive_root=Path(args.source_archive_root),
        notifications_dir=Path(args.notifications_dir),
    )
    console.print_json(json.dumps(result, ensure_ascii=False))


def cmd_version_corrigendum_application(args):
    """Apply deterministic corrigendum corrections to amendment-event payloads."""
    from src.legal_corpus.corrigenda import apply_corrigenda

    result = apply_corrigenda(
        events_path=Path(args.events),
        output=Path(args.output),
        report_output=Path(args.report_output),
    )
    console.print_json(json.dumps({k: v for k, v in result.items() if k != "applications"}, ensure_ascii=False))


def cmd_version_merge_events(args):
    """Merge event ledgers and surface cross-source conflicts."""
    from src.legal_corpus.event_ledgers import merge_event_ledgers

    result = merge_event_ledgers(
        inputs=[Path(path) for path in args.inputs],
        output=Path(args.output),
        review_output=Path(args.review_output) if args.review_output else None,
    )
    console.print_json(json.dumps(result, ensure_ascii=False))


def cmd_version_missing_anchor_backfill(args):
    """Write source-backed events for known missing materializer anchors."""
    from src.legal_corpus.missing_anchor_backfill import write_missing_anchor_backfill_events

    result = write_missing_anchor_backfill_events(
        corpus_dir=Path(args.corpus_dir),
        output=Path(args.output),
        report_output=Path(args.report_output) if args.report_output else None,
    )
    console.print_json(json.dumps(result, ensure_ascii=False))


def cmd_version_compare(args):
    """Compare materialized component versions between two dates."""
    from src.legal_corpus.version_compare import compare_component_versions

    result = compare_component_versions(
        args.component_id,
        from_date=args.from_date,
        to_date=args.to_date,
        temporal_dimension=args.temporal_dimension,
        version_dir=Path(args.version_dir) if args.version_dir else None,
        registry_path=Path(args.registry),
        target_work=args.target_work,
    )
    console.print_json(json.dumps(result, ensure_ascii=False))


def cmd_version_reconstruct(args):
    """Reconstruct a single component's provision text at a given date."""
    from src.legal_corpus.version_reconstruct import reconstruct_component

    result = reconstruct_component(
        args.component_id,
        date=args.date,
        version_dir=Path(args.version_dir) if args.version_dir else None,
        registry_path=Path(args.registry),
    )
    console.print_json(json.dumps(result, ensure_ascii=False))


def cmd_version_surgical_gap_queue(args):
    """Build an actionable queue for remaining surgical Rules materializer gaps."""
    from src.legal_corpus.surgical_gap_queue import build_surgical_gap_queue

    result = build_surgical_gap_queue(
        coverage_gaps_path=Path(args.coverage_gaps),
        output=Path(args.output),
        focus_rules=args.focus_rule,
        limit=args.limit,
    )
    console.print_json(json.dumps(result["summary"], ensure_ascii=False))


def cmd_version_reconcile(args):
    """Reconcile reconstructed history against a consolidated checkpoint."""
    from src.legal_corpus.reconciliation import reconcile

    report = reconcile(
        target_work=args.target_work,
        checkpoint_path=Path(args.checkpoint_path),
        checkpoint_date=args.checkpoint_date,
        output=Path(args.output),
        registry_path=Path(args.registry),
        version_dir=Path(args.version_dir) if args.version_dir else None,
        events_path=Path(args.events),
        audit_output=Path(args.audit_output) if args.audit_output else None,
    )
    console.print_json(json.dumps(report, ensure_ascii=False))


def cmd_version_materialize_forms(args):
    """Build the conservative GST forms version-history lane."""
    from src.legal_corpus.form_version_snapshots import materialize_form_versions

    result = materialize_form_versions(
        events_path=Path(args.events),
        corpus_dir=Path(args.corpus_dir),
        output_dir=Path(args.output_dir),
        base_as_of=args.base_as_of,
        form_registry_path=Path(args.form_registry) if args.form_registry else None,
    )
    console.print_json(json.dumps(result, ensure_ascii=False))


def cmd_version_phase3_backlog(args):
    """Write a consolidated Phase 3 backlog from current version-history artifacts."""
    from src.legal_corpus.phase3_backlog import build_phase3_backlog

    result = build_phase3_backlog(
        rules_coverage_path=Path(args.rules_coverage),
        forms_manifest_path=Path(args.forms_manifest),
        portal_completeness_path=Path(args.portal_completeness),
        confidence_tiers_path=Path(args.confidence_tiers),
        act_audit_path=Path(args.act_audit),
        reconciliation_report_path=Path(args.reconciliation_report),
        output_path=Path(args.output),
        sample_limit=args.sample_limit,
    )
    console.print_json(json.dumps(result, ensure_ascii=False))


def cmd_version_html_report(args):
    """Write a static HTML report for version-history review."""
    from src.legal_corpus.version_history_report import write_version_history_report

    result = write_version_history_report(
        output=Path(args.output),
        review_report=Path(args.review_report),
        rules_manifest=Path(args.rules_manifest),
        rules_coverage=Path(args.rules_coverage),
        reconciliation_report=Path(args.reconciliation_report),
        forms_manifest=Path(args.forms_manifest),
        forms_coverage=Path(args.forms_coverage),
        review_triage=Path(args.review_triage),
        review_decisions=Path(args.review_decisions),
        auto_review_decisions=Path(args.auto_review_decisions),
        dependency_review_decisions=Path(args.dependency_review_decisions),
        codex_review_decisions=Path(args.codex_review_decisions),
        review_completion=Path(args.review_completion),
        amendment_events=Path(args.amendment_events),
        node_versions=Path(args.node_versions),
        act_confidence_tiers=Path(args.act_confidence_tiers),
        act_reconciliation_report=Path(args.act_reconciliation_report),
        act_manifest=Path(args.act_manifest),
    )
    console.print_json(json.dumps(result, ensure_ascii=False))


def cmd_version_triage_review(args):
    """Triage needs_review amendment events into smaller review buckets."""
    from src.legal_corpus.review_triage import triage_review_items

    result = triage_review_items(
        events_path=Path(args.events),
        output=Path(args.output),
        use_llm=args.use_llm,
        llm_base_url=args.llm_base_url,
        llm_model=args.llm_model,
        llm_api_key_env=args.llm_api_key_env,
        llm_cache=Path(args.llm_cache) if args.llm_cache else None,
        llm_limit=args.llm_limit,
        llm_concurrency=args.llm_concurrency,
    )
    console.print_json(json.dumps({k: v for k, v in result.items() if k not in {"items", "groups"}}, ensure_ascii=False))


def cmd_version_apply_review_decisions(args):
    """Apply curated review decisions to produce a promoted event ledger."""
    from src.legal_corpus.review_decisions import apply_review_decisions

    decision_paths = args.decisions or ["derived/version_history/review_decisions.json"]
    result = apply_review_decisions(
        events_path=Path(args.events),
        decisions_path=[Path(path) for path in decision_paths],
        output=Path(args.output),
    )
    console.print_json(json.dumps(result, ensure_ascii=False))


def cmd_version_auto_review_decisions(args):
    """Generate conservative auto-approval decisions for mechanically verifiable review items."""
    from src.legal_corpus.review_decisions import generate_auto_review_decisions

    result = generate_auto_review_decisions(
        events_path=Path(args.events),
        output=Path(args.output),
        triage_path=Path(args.triage) if args.triage else None,
        existing_decisions_path=Path(args.existing_decisions) if args.existing_decisions else None,
        limit=args.limit,
    )
    console.print_json(json.dumps(result, ensure_ascii=False))


def cmd_version_dependency_review_decisions(args):
    """Generate auto-approval decisions for source events that create missing anchor components."""
    from src.legal_corpus.review_decisions import generate_dependency_review_decisions

    existing_decisions = args.existing_decisions or [
        "derived/version_history/review_decisions.json",
        "derived/version_history/auto_review_decisions.json",
    ]
    result = generate_dependency_review_decisions(
        events_path=Path(args.events),
        coverage_gaps_path=Path(args.coverage_gaps),
        output=Path(args.output),
        existing_decision_paths=[Path(path) for path in existing_decisions],
        limit=args.limit,
    )
    console.print_json(json.dumps(result, ensure_ascii=False))


def cmd_version_codex_review_decisions(args):
    """Generate Codex-approved decisions for mechanically verified review items."""
    from src.legal_corpus.review_decisions import generate_codex_review_decisions

    existing_decisions = args.existing_decisions or [
        "derived/version_history/review_decisions.json",
        "derived/version_history/auto_review_decisions.json",
        "derived/version_history/dependency_review_decisions.json",
    ]
    result = generate_codex_review_decisions(
        events_path=Path(args.events),
        node_versions_path=Path(args.node_versions),
        output=Path(args.output),
        existing_decision_paths=[Path(path) for path in existing_decisions],
        source_archive_root=Path(args.source_archive_root) if args.source_archive_root else None,
        limit=args.limit,
    )
    console.print_json(json.dumps(result, ensure_ascii=False))


def cmd_version_complete_review(args):
    """Classify pending review items into terminal audit states."""
    from src.legal_corpus.review_completion import complete_review

    decision_paths = args.decisions or [
        "derived/version_history/review_decisions.json",
        "derived/version_history/auto_review_decisions.json",
        "derived/version_history/dependency_review_decisions.json",
        "derived/version_history/codex_review_decisions.json",
    ]
    result = complete_review(
        events_path=Path(args.events),
        rules_manifest_path=Path(args.rules_manifest),
        rules_coverage_path=Path(args.rules_coverage),
        forms_manifest_path=Path(args.forms_manifest),
        forms_coverage_path=Path(args.forms_coverage),
        reconciliation_report_path=Path(args.reconciliation_report),
        review_triage_path=Path(args.review_triage),
        decision_paths=[Path(path) for path in decision_paths],
        report_output=Path(args.report_output),
        decisions_output=Path(args.decisions_output),
    )
    console.print_json(json.dumps(result, ensure_ascii=False))


def cmd_version_fetch_consolidated(args):
    """Fetch a consolidated source file for later reconciliation."""
    from src.legal_corpus.consolidated_fetch import DEFAULT_RULES_CONSOLIDATED_URL, fetch_consolidated

    url = args.url or DEFAULT_RULES_CONSOLIDATED_URL
    manifest = fetch_consolidated(
        target_work=args.target_work,
        url=url,
        output_dir=Path(args.output_dir),
        timeout=args.timeout,
        verify_tls=not args.insecure,
        section_limit=args.section_limit,
        section_timeout=args.section_timeout,
        section_concurrency=args.section_concurrency,
    )
    console.print_json(json.dumps(manifest, ensure_ascii=False))


def cmd_version_confidence_tiers(args):
    """Compute per-component confidence tiers (A/B/C/D) for litigation readiness."""
    from src.legal_corpus.confidence_tiers import compute_confidence_tiers

    version_dir = Path(args.version_dir)
    recon_report = Path(args.reconciliation_report) if args.reconciliation_report else version_dir / "reconciliation_report.json"
    baseline_components = Path(args.baseline_components) if args.baseline_components else None

    result = compute_confidence_tiers(
        node_versions_path=version_dir / "node_versions.jsonl",
        coverage_gaps_path=version_dir / "coverage_gaps.json",
        reconciliation_report_path=recon_report if recon_report.exists() else None,
        amendment_events_path=Path(args.amendment_events) if args.amendment_events else None,
        baseline_components_path=baseline_components if baseline_components and baseline_components.exists() else None,
        portal_completeness_path=Path(args.portal_completeness) if args.portal_completeness and Path(args.portal_completeness).exists() else None,
    )

    # Print summary
    tier_counts = result["tier_counts"]
    total = result["total_components"]
    console.print(f"\n[bold]Confidence Tiers[/bold] ({total} components)")
    for tier in ["A", "B", "C", "D"]:
        count = tier_counts.get(tier, 0)
        pct = 100 * count / max(total, 1)
        desc = result["tier_descriptions"][tier]
        console.print(f"  Tier {tier}: {count:4d} ({pct:5.1f}%)  {desc}")

    tier_a = result["tier_a_components"]
    if tier_a:
        console.print(f"\n[green]Tier A components ({len(tier_a)}):[/green]")
        for cid in tier_a[:20]:
            console.print(f"  {cid}")
        if len(tier_a) > 20:
            console.print(f"  ... and {len(tier_a) - 20} more")

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    console.print(f"\nOutput: {output}")


def cmd_version_evidence_bundle(args):
    """Generate a citation-grade evidence bundle for a component/date range."""
    from src.legal_corpus.evidence_bundles import generate_evidence_bundle

    version_dir = Path(args.version_dir)
    bundle = generate_evidence_bundle(
        component_id=args.component_id,
        from_date=args.from_date,
        to_date=args.to_date,
        version_dir=version_dir,
        events_path=Path(args.events),
        baseline_dir=Path(args.baseline_dir) if args.baseline_dir else version_dir.parent / "baselines" / "cgst-rules-2017" / "2017-06-19",
        coverage_gaps_path=version_dir / "coverage_gaps.json",
        confidence_tiers_path=Path(args.confidence_tiers) if args.confidence_tiers else Path("derived/version_history/confidence_tiers.json"),
        portal_completeness_path=Path(args.portal_completeness) if args.portal_completeness else Path("derived/version_history/portal_completeness_report.json"),
        corrigendum_ledger_path=version_dir / "corrigendum_ledger.jsonl",
    )

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(bundle, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")

    readiness = bundle.get("citation_readiness", {})
    console.print(f"\n[bold]Evidence Bundle[/bold]")
    console.print(f"  Component: {args.component_id}")
    console.print(f"  Date range: {args.from_date} → {args.to_date}")
    console.print(f"  Tier: {readiness.get('tier','?')}")
    console.print(f"  Events: {len(bundle.get('event_chain',[]))}")
    console.print(f"  Versions: {len(bundle.get('node_versions',[]))}")
    console.print(f"  Gaps: {readiness.get('unresolved_gaps',0)}")
    console.print(f"  Can cite: {readiness.get('can_cite',False)}")
    console.print(f"  Warning: {readiness.get('warning','')}")
    console.print(f"\nOutput: {output}")


def cmd_version_evidence_bundle_html(args):
    """Render an evidence bundle JSON as a self-contained HTML document."""
    from src.legal_corpus.evidence_bundles import render_evidence_bundle_html

    bundle = json.loads(Path(args.bundle).read_text(encoding="utf-8"))
    html = render_evidence_bundle_html(bundle)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(html, encoding="utf-8")
    console.print(f"Output: {output}")


def cmd_version_act_audit(args):
    """Write a non-blocking CGST Act pipeline audit report."""
    from src.legal_corpus.act_pipeline_audit import audit_act_pipeline

    result = audit_act_pipeline(
        events_path=Path(args.events),
        coverage_gaps_path=Path(args.coverage_gaps),
        materialization_manifest_path=Path(args.materialization_manifest),
        baseline_components_path=Path(args.baseline_components),
        baseline_reconciliation_path=Path(args.baseline_reconciliation),
        confidence_tiers_path=Path(args.confidence_tiers),
        output_path=Path(args.output),
        sample_limit=args.sample_limit,
    )
    console.print_json(json.dumps(result, ensure_ascii=False))


def cmd_version_portal_completeness(args):
    """Check portal-listed notification references against the event ledger."""
    from src.legal_corpus.portal_completeness import (
        annotate_top10_gap_report,
        build_portal_completeness_report,
        rebuild_top10_gap_report,
    )

    result = build_portal_completeness_report(
        html_dir=Path(args.html_dir),
        events_path=Path(args.events),
        output=Path(args.output),
        top_rules=args.rule,
    )
    if args.top10_gap_report:
        result["top10_gap_report_rebuild"] = rebuild_top10_gap_report(
            coverage_gaps_path=Path(args.coverage_gaps),
            output=Path(args.top10_gap_report),
            top_n=args.top_n,
        )
        result["top10_gap_report_update"] = annotate_top10_gap_report(
            top10_path=Path(args.top10_gap_report),
            portal_report_path=Path(args.output),
        )
    console.print_json(json.dumps(result, ensure_ascii=False))


def cmd_version_alias_checkpoint(args):
    """Alias an already-downloaded checkpoint into a provenanced reconciliation source directory."""
    from src.legal_corpus.consolidated_fetch import alias_downloaded_checkpoint

    manifest = alias_downloaded_checkpoint(
        source_dir=Path(args.source_dir),
        output_dir=Path(args.output_dir),
        checkpoint_date=args.checkpoint_date,
        source_label=args.source_label,
        required_labels=args.required_label,
    )
    console.print_json(json.dumps(manifest, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(
        description="Git for Law - Legal AST with Time-Travel Queries",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    
    subparsers = parser.add_subparsers(dest="command", help="Available commands")
    
    # load-genesis
    p_load = subparsers.add_parser("load-genesis", help="Load genesis block into Neo4j")
    p_load.add_argument("--data-dir", default="data/genesis", help="Genesis data directory")
    p_load.set_defaults(func=cmd_load_genesis)
    
    # query
    p_query = subparsers.add_parser("query", help="Query a rule")
    p_query.add_argument("rule_id", help="Rule ID, e.g., 'CGST_Rules/Rule_8'")
    p_query.add_argument("--as-of", help="Date for time-travel query (ISO format)")
    p_query.add_argument("--with-deps", action="store_true", help="Include MUTATIS_MUTANDIS dependencies")
    p_query.set_defaults(func=cmd_query)
    
    # query-form
    p_form = subparsers.add_parser("query-form", help="Query a form")
    p_form.add_argument("form_id", help="Form ID, e.g., 'FORM_GST_REG_01'")
    p_form.add_argument("--as-of", help="Date for time-travel query (ISO format)")
    p_form.set_defaults(func=cmd_query_form)
    
    # compare
    p_compare = subparsers.add_parser("compare", help="Compare rule at two dates")
    p_compare.add_argument("rule_id", help="Rule ID")
    p_compare.add_argument("date1", help="First date (ISO format)")
    p_compare.add_argument("date2", help="Second date (ISO format)")
    p_compare.set_defaults(func=cmd_compare)
    
    # parse
    p_parse = subparsers.add_parser("parse", help="Parse notification into mutations")
    p_parse.add_argument("input", help="Input notification file (.txt)")
    p_parse.add_argument("--output", "-o", help="Output mutations file (.json)")
    p_parse.add_argument("--offline", action="store_true", help="Use offline parser (no LLM)")
    p_parse.set_defaults(func=cmd_parse)
    
    # apply
    p_apply = subparsers.add_parser("apply", help="Apply mutations to graph")
    p_apply.add_argument("input", help="Mutations JSON file")
    p_apply.add_argument("--dry-run", action="store_true", help="Validate without applying")
    p_apply.set_defaults(func=cmd_apply)
    
    # test
    p_test = subparsers.add_parser("test", help="Run Steel Thread test")
    p_test.set_defaults(func=cmd_test)

    # source
    p_source = subparsers.add_parser("source", help="Manage immutable source archives")
    source_sub = p_source.add_subparsers(dest="source_command")
    p_source_add = source_sub.add_parser("add", help="Archive a source PDF/text file")
    p_source_add.add_argument("input", help="Input source file")
    p_source_add.add_argument("source_dir", help="Destination source archive directory")
    p_source_add.add_argument("--canonical-id", required=True, help="Canonical ID for the source")
    p_source_add.add_argument("--document-type", default="notification", help="Document type")
    p_source_add.add_argument("--title", help="Document title")
    p_source_add.add_argument("--jurisdiction", default="IN-UNION", help="Jurisdiction code")
    p_source_add.add_argument("--language", default="eng", help="Language code")
    p_source_add.add_argument("--publication-date", help="Publication date")
    p_source_add.add_argument("--effective-from", help="Effective date")
    p_source_add.add_argument("--issuing-authority", default="/in/authority/unknown", help="Issuing authority ID")
    p_source_add.add_argument("--source-url", help="Official source URL")
    p_source_add.set_defaults(func=cmd_source_add)

    p_source_extract = source_sub.add_parser("extract", help="Extract text from a source archive")
    p_source_extract.add_argument("source_dir", help="Directory containing source.txt/source.pdf")
    p_source_extract.set_defaults(func=cmd_source_extract)

    p_source_validate = source_sub.add_parser("validate", help="Validate source archive span integrity")
    p_source_validate.add_argument("path", help="Source archive or sources directory")
    p_source_validate.set_defaults(func=cmd_source_validate)

    p_source_inventory = source_sub.add_parser("inventory", help="Build an inventory of local source PDFs")
    p_source_inventory.add_argument("root_dir", help="Root directory containing local source PDFs, e.g. data/Law")
    p_source_inventory.add_argument("--index-csv", help="Optional CBIC notification index CSV")
    p_source_inventory.add_argument("--sources-root", default="sources", help="Suggested source archive root")
    p_source_inventory.add_argument("--corpus-root", default="corpus", help="Suggested corpus output root")
    p_source_inventory.add_argument(
        "--output",
        default="derived/sources/source_inventory.json",
        help="Output inventory JSON",
    )
    p_source_inventory.add_argument("--limit", type=int, help="Limit inventory items for sampling")
    p_source_inventory.add_argument("--no-checksums", action="store_true", help="Skip source file SHA-256 checksums")
    p_source_inventory.add_argument(
        "--no-unclassified",
        action="store_true",
        help="Only include files referenced by known metadata indexes",
    )
    p_source_inventory.add_argument("--json", action="store_true", help="Print JSON")
    p_source_inventory.set_defaults(func=cmd_source_inventory)

    p_source_inventory_validate = source_sub.add_parser("inventory-validate", help="Validate a source inventory JSON")
    p_source_inventory_validate.add_argument("inventory", help="Source inventory JSON")
    p_source_inventory_validate.add_argument("--json", action="store_true", help="Print JSON")
    p_source_inventory_validate.set_defaults(func=cmd_source_inventory_validate)

    p_source_inventory_report = source_sub.add_parser("inventory-report", help="Build a source inventory review report")
    p_source_inventory_report.add_argument("inventory", help="Source inventory JSON")
    p_source_inventory_report.add_argument(
        "--output",
        default="derived/sources/source_inventory_report.json",
        help="Output inventory report JSON",
    )
    p_source_inventory_report.add_argument("--json", action="store_true", help="Print JSON")
    p_source_inventory_report.set_defaults(func=cmd_source_inventory_report)

    # corpus
    p_corpus = subparsers.add_parser("corpus", help="Manage canonical corpus files")
    corpus_sub = p_corpus.add_subparsers(dest="corpus_command")
    p_corpus_seed = corpus_sub.add_parser("seed", help="Seed corpus from existing data")
    p_corpus_seed.add_argument("--corpus-dir", default="corpus", help="Canonical corpus directory")
    p_corpus_seed.add_argument("--sources-dir", default="sources", help="Source archive directory")
    p_corpus_seed.set_defaults(func=cmd_corpus_seed)

    p_corpus_parse = corpus_sub.add_parser("parse", help="Parse source text into deterministic structure spans")
    p_corpus_parse.add_argument("source_dir", help="Source archive directory")
    p_corpus_parse.add_argument("--document-type", default="notification", help="Document type")
    p_corpus_parse.add_argument(
        "--mode",
        choices=["deterministic", "paragraph", "llm"],
        default="deterministic",
        help="Structure parser mode",
    )
    p_corpus_parse.add_argument("--provider", choices=["deepseek", "openai", "local"], default="deepseek", help="LLM provider")
    p_corpus_parse.add_argument("--model", help="LLM model override")
    p_corpus_parse.add_argument("--base-url", help="OpenAI-compatible base URL for LLM parsing")
    p_corpus_parse.set_defaults(func=cmd_corpus_parse)

    p_corpus_render = corpus_sub.add_parser("render", help="Render a source archive into canonical XML")
    p_corpus_render.add_argument("source_dir", help="Source archive directory")
    p_corpus_render.add_argument("output", help="Output XML path")
    p_corpus_render.set_defaults(func=cmd_corpus_render)

    p_corpus_ingest = corpus_sub.add_parser("ingest", help="Archive, parse, render, and validate a source file")
    p_corpus_ingest.add_argument("input", help="Input PDF/text/html source file")
    p_corpus_ingest.add_argument("source_dir", help="Destination source archive directory")
    p_corpus_ingest.add_argument("output", help="Output canonical XML path")
    p_corpus_ingest.add_argument("--canonical-id", required=True, help="Canonical ID for the document")
    p_corpus_ingest.add_argument(
        "--document-type",
        default="notification",
        choices=["act", "rules", "rule", "notification", "circular", "order", "form", "schedule"],
        help="Document type",
    )
    p_corpus_ingest.add_argument("--title", help="Document title")
    p_corpus_ingest.add_argument("--jurisdiction", default="IN-UNION", help="Jurisdiction code")
    p_corpus_ingest.add_argument("--language", default="eng", help="Language code")
    p_corpus_ingest.add_argument("--publication-date", help="Publication date")
    p_corpus_ingest.add_argument("--effective-from", help="Effective date")
    p_corpus_ingest.add_argument("--issuing-authority", default="/in/authority/unknown", help="Issuing authority ID")
    p_corpus_ingest.add_argument("--source-url", help="Official source URL")
    p_corpus_ingest.add_argument(
        "--mode",
        choices=["deterministic", "paragraph", "llm"],
        default="deterministic",
        help="Structure parser mode",
    )
    p_corpus_ingest.add_argument("--provider", choices=["deepseek", "openai", "local"], default="deepseek", help="LLM provider")
    p_corpus_ingest.add_argument("--model", help="LLM model override")
    p_corpus_ingest.add_argument("--base-url", help="OpenAI-compatible base URL for LLM parsing")
    p_corpus_ingest.add_argument("--report", help="Optional ingest report JSON")
    p_corpus_ingest.set_defaults(func=cmd_corpus_ingest)

    p_corpus_ingest_inventory = corpus_sub.add_parser(
        "ingest-inventory",
        help="Preview or execute ingestion from a source inventory",
    )
    p_corpus_ingest_inventory.add_argument("inventory", help="Source inventory JSON")
    p_corpus_ingest_inventory.add_argument("--execute", action="store_true", help="Write source archives and corpus XML")
    p_corpus_ingest_inventory.add_argument("--limit", type=int, help="Limit selected inventory items")
    p_corpus_ingest_inventory.add_argument(
        "--status",
        choices=["ready", "missing", "unclassified", "any"],
        default="ready",
        help="Inventory status to select",
    )
    p_corpus_ingest_inventory.add_argument("--type", dest="document_type", help="Filter by document_type")
    p_corpus_ingest_inventory.add_argument("--category", help="Filter by category_slug, e.g. central-tax")
    p_corpus_ingest_inventory.add_argument(
        "--mode",
        choices=["deterministic", "paragraph", "llm"],
        default="deterministic",
        help="Structure parser mode",
    )
    p_corpus_ingest_inventory.add_argument(
        "--provider",
        choices=["deepseek", "openai", "local"],
        default="deepseek",
        help="LLM provider",
    )
    p_corpus_ingest_inventory.add_argument("--model", help="LLM model override")
    p_corpus_ingest_inventory.add_argument("--base-url", help="OpenAI-compatible base URL for LLM parsing")
    p_corpus_ingest_inventory.add_argument("--no-skip-existing", action="store_true", help="Reingest existing outputs")
    p_corpus_ingest_inventory.add_argument("--continue-on-error", action="store_true", help="Continue after item failures")
    p_corpus_ingest_inventory.add_argument("--progress", action="store_true", help="Print progress for each selected item")
    p_corpus_ingest_inventory.add_argument(
        "--report",
        default="derived/ingest/inventory_ingest_report.json",
        help="Output batch ingest report JSON",
    )
    p_corpus_ingest_inventory.add_argument("--json", action="store_true", help="Print JSON")
    p_corpus_ingest_inventory.set_defaults(func=cmd_corpus_ingest_inventory)

    p_corpus_validate = corpus_sub.add_parser("validate", help="Validate canonical corpus")
    p_corpus_validate.add_argument("--corpus-dir", default="corpus", help="Canonical corpus directory")
    p_corpus_validate.set_defaults(func=cmd_corpus_validate)

    p_corpus_quality = corpus_sub.add_parser("quality", help="Build corpus XML quality metrics for review")
    p_corpus_quality.add_argument("--corpus-dir", default="corpus", help="Canonical corpus directory")
    p_corpus_quality.add_argument("--max-paragraph-chars", type=int, default=2000, help="Long paragraph threshold")
    p_corpus_quality.add_argument("--limit", type=int, default=20, help="Number of flagged documents to display")
    p_corpus_quality.add_argument("--output", help="Optional quality report JSON")
    p_corpus_quality.add_argument("--json", action="store_true", help="Print JSON")
    p_corpus_quality.set_defaults(func=cmd_corpus_quality)

    p_corpus_unresolved = corpus_sub.add_parser(
        "unresolved-references",
        help="Report unresolved canonical references by target and source",
    )
    p_corpus_unresolved.add_argument("--corpus-dir", default="corpus", help="Canonical corpus directory")
    p_corpus_unresolved.add_argument(
        "--output",
        default="derived/references/unresolved_references.json",
        help="Output unresolved reference report JSON",
    )
    p_corpus_unresolved.add_argument("--limit", type=int, default=20, help="Number of targets to display")
    p_corpus_unresolved.add_argument("--sample-limit", type=int, default=5, help="Samples per unresolved target")
    p_corpus_unresolved.add_argument(
        "--summary-output",
        default="derived/references/unresolved_references_summary.md",
        help="Output Markdown triage summary",
    )
    p_corpus_unresolved.add_argument("--json", action="store_true", help="Print JSON")
    p_corpus_unresolved.set_defaults(func=cmd_corpus_unresolved_references)

    p_corpus_split_forms = corpus_sub.add_parser(
        "split-forms",
        help="Split an aggregate FORM GST source archive into canonical form XML files",
    )
    p_corpus_split_forms.add_argument("source_dir", help="Source archive containing extracted_text.json and structure.json")
    p_corpus_split_forms.add_argument("--corpus-dir", default="corpus", help="Canonical corpus directory")
    p_corpus_split_forms.add_argument(
        "--output",
        default="derived/review/form_split_report.json",
        help="Output form split report JSON",
    )
    p_corpus_split_forms.add_argument("--overwrite", action="store_true", help="Overwrite existing form XML outputs")
    p_corpus_split_forms.add_argument("--json", action="store_true", help="Print JSON")
    p_corpus_split_forms.set_defaults(func=cmd_corpus_split_forms)

    p_corpus_promote_batch = corpus_sub.add_parser(
        "promote-batch",
        help="Plan or apply promotion of clean generated batch XML",
    )
    p_corpus_promote_batch.add_argument("ingest_report", help="Batch ingest report JSON")
    p_corpus_promote_batch.add_argument("quality_report", help="Corpus quality report JSON for generated XML")
    p_corpus_promote_batch.add_argument("--target-corpus", default="corpus", help="Canonical corpus destination")
    p_corpus_promote_batch.add_argument(
        "--target-sources",
        help="Optional canonical source archive destination to promote with selected XML",
    )
    p_corpus_promote_batch.add_argument(
        "--include-flagged",
        action="store_true",
        help="Include documents flagged by the quality report",
    )
    p_corpus_promote_batch.add_argument(
        "--overwrite",
        action="store_true",
        help="Allow replacing existing target XML and source archives",
    )
    p_corpus_promote_batch.add_argument("--approve", action="store_true", help="Actually copy selected XML and sources")
    p_corpus_promote_batch.add_argument(
        "--output",
        default="derived/review/batch_promotion_plan.json",
        help="Output promotion plan JSON",
    )
    p_corpus_promote_batch.add_argument("--limit", type=int, default=20, help="Number of excluded documents to display")
    p_corpus_promote_batch.add_argument("--json", action="store_true", help="Print JSON")
    p_corpus_promote_batch.set_defaults(func=cmd_corpus_promote_batch)

    p_corpus_list = corpus_sub.add_parser("list", help="List canonical corpus documents")
    p_corpus_list.add_argument("--corpus-dir", default="corpus", help="Canonical corpus directory")
    p_corpus_list.add_argument("--type", dest="document_type", help="Filter by document_type")
    p_corpus_list.add_argument("--json", action="store_true", help="Print JSON")
    p_corpus_list.set_defaults(func=cmd_corpus_list)

    p_corpus_diff = corpus_sub.add_parser("diff", help="Compare a reviewed corpus against a base corpus")
    p_corpus_diff.add_argument("review_corpus", help="Reviewed or amended corpus directory")
    p_corpus_diff.add_argument("--base-corpus", default="corpus", help="Base canonical corpus directory")
    p_corpus_diff.add_argument("--output", help="Optional JSON diff report path")
    p_corpus_diff.add_argument("--context", type=int, default=3, help="Unified text diff context lines")
    p_corpus_diff.add_argument("--max-diff-lines", type=int, default=200, help="Maximum text diff lines per document")
    p_corpus_diff.add_argument("--json", action="store_true", help="Print JSON")
    p_corpus_diff.set_defaults(func=cmd_corpus_diff)

    p_corpus_query = corpus_sub.add_parser("query", help="Query a canonical document or provision")
    p_corpus_query.add_argument("canonical_id", help="Canonical ID or known legacy prototype ID")
    p_corpus_query.add_argument("--corpus-dir", default="corpus", help="Canonical corpus directory")
    p_corpus_query.add_argument(
        "--role",
        choices=["auto", "document", "provision"],
        default="auto",
        help="Which role to display when an ID is both a document and provision",
    )
    p_corpus_query.add_argument("--full", action="store_true", help="Print full text instead of a preview")
    p_corpus_query.add_argument("--json", action="store_true", help="Print JSON")
    p_corpus_query.set_defaults(func=cmd_corpus_query)

    p_corpus_export_text = corpus_sub.add_parser("export-text", help="Export plain text by canonical ID")
    p_corpus_export_text.add_argument("canonical_id", help="Canonical ID or known legacy prototype ID")
    p_corpus_export_text.add_argument("--corpus-dir", default="corpus", help="Canonical corpus directory")
    p_corpus_export_text.add_argument(
        "--role",
        choices=["auto", "document", "provision"],
        default="auto",
        help="Which role to export when an ID is both a document and provision",
    )
    p_corpus_export_text.add_argument("--output", "-o", help="Output text file")
    p_corpus_export_text.set_defaults(func=cmd_corpus_export_text)

    # graph
    p_graph = subparsers.add_parser("graph", help="Manage derived graph artifacts")
    graph_sub = p_graph.add_subparsers(dest="graph_command")
    p_graph_rebuild = graph_sub.add_parser("rebuild", help="Rebuild derived graph JSON from corpus")
    p_graph_rebuild.add_argument("--corpus-dir", default="corpus", help="Canonical corpus directory")
    p_graph_rebuild.add_argument("--output", default="derived/graph/corpus_graph.json", help="Output graph JSON")
    p_graph_rebuild.set_defaults(func=cmd_graph_rebuild)

    p_graph_cypher = graph_sub.add_parser("cypher", help="Export Neo4j Cypher payload from corpus")
    p_graph_cypher.add_argument("--corpus-dir", default="corpus", help="Canonical corpus directory")
    p_graph_cypher.add_argument("--output", default="derived/graph/corpus_neo4j_payload.json", help="Output payload JSON")
    p_graph_cypher.set_defaults(func=cmd_graph_cypher)

    p_graph_load = graph_sub.add_parser("load", help="Load derived corpus graph into Neo4j")
    p_graph_load.add_argument("--corpus-dir", default="corpus", help="Canonical corpus directory")
    p_graph_load.add_argument("--uri", help="Neo4j URI")
    p_graph_load.add_argument("--user", help="Neo4j username")
    p_graph_load.add_argument("--password", help="Neo4j password")
    p_graph_load.add_argument("--clear", action="store_true", help="Clear existing LegalNode graph before loading")
    p_graph_load.set_defaults(func=cmd_graph_load)

    # search
    p_search = subparsers.add_parser("search", help="Manage derived corpus search artifacts")
    search_sub = p_search.add_subparsers(dest="search_command")
    p_search_rebuild = search_sub.add_parser("rebuild", help="Rebuild JSONL search index from corpus")
    p_search_rebuild.add_argument("--corpus-dir", default="corpus", help="Canonical corpus directory")
    p_search_rebuild.add_argument("--output", default="derived/search/corpus_search.jsonl", help="Output search JSONL")
    p_search_rebuild.set_defaults(func=cmd_search_rebuild)

    p_search_query = search_sub.add_parser("query", help="Search the derived corpus index")
    p_search_query.add_argument("query", help="Search query")
    p_search_query.add_argument("--index", default="derived/search/corpus_search.jsonl", help="Search JSONL index")
    p_search_query.add_argument("--limit", type=int, default=10, help="Maximum results")
    p_search_query.add_argument("--type", dest="document_type", help="Filter by document_type")
    p_search_query.add_argument(
        "--role",
        choices=["document", "provision"],
        help="Filter by search record role",
    )
    p_search_query.add_argument("--json", action="store_true", help="Print JSON")
    p_search_query.set_defaults(func=cmd_search_query)

    # vector
    p_vector = subparsers.add_parser("vector", help="Manage vector/RAG derived artifacts")
    vector_sub = p_vector.add_subparsers(dest="vector_command")
    p_vector_chunks = vector_sub.add_parser("chunks", help="Export JSONL chunks for embeddings or RAG")
    p_vector_chunks.add_argument("--corpus-dir", default="corpus", help="Canonical corpus directory")
    p_vector_chunks.add_argument("--output", default="derived/vector/corpus_chunks.jsonl", help="Output chunk JSONL")
    p_vector_chunks.add_argument("--max-chars", type=int, default=900, help="Maximum characters per chunk")
    p_vector_chunks.add_argument("--overlap", type=int, default=120, help="Character overlap between chunks")
    p_vector_chunks.add_argument("--no-documents", action="store_true", help="Exclude document-level records")
    p_vector_chunks.add_argument("--no-provisions", action="store_true", help="Exclude provision-level records")
    p_vector_chunks.set_defaults(func=cmd_vector_chunks)

    # api
    p_api = subparsers.add_parser("api", help="Manage API-ready derived payloads")
    api_sub = p_api.add_subparsers(dest="api_command")
    p_api_export = api_sub.add_parser("export", help="Export corpus API JSON payload")
    p_api_export.add_argument("--corpus-dir", default="corpus", help="Canonical corpus directory")
    p_api_export.add_argument("--output", default="derived/api/corpus_api.json", help="Output API JSON")
    p_api_export.add_argument("--json", action="store_true", help="Print payload JSON")
    p_api_export.set_defaults(func=cmd_api_export)

    # html
    p_html = subparsers.add_parser("html", help="Manage rendered HTML derived artifacts")
    html_sub = p_html.add_subparsers(dest="html_command")
    p_html_build = html_sub.add_parser("build", help="Render static HTML from corpus")
    p_html_build.add_argument("--corpus-dir", default="corpus", help="Canonical corpus directory")
    p_html_build.add_argument("--output-dir", default="derived/html", help="Output HTML directory")
    p_html_build.set_defaults(func=cmd_html_build)

    # pipeline
    p_pipeline = subparsers.add_parser("pipeline", help="Run canonical corpus pipeline gates")
    pipeline_sub = p_pipeline.add_subparsers(dest="pipeline_command")
    p_pipeline_verify = pipeline_sub.add_parser("verify", help="Validate corpus and rebuild derived artifacts")
    p_pipeline_verify.add_argument("--corpus-dir", default="corpus", help="Canonical corpus directory")
    p_pipeline_verify.add_argument("--sources-dir", default="sources", help="Source archive directory")
    p_pipeline_verify.add_argument("--derived-dir", default="derived", help="Derived artifact root")
    p_pipeline_verify.add_argument(
        "--inventory",
        help="Optional source inventory JSON; defaults to derived/sources/source_inventory.json if present",
    )
    p_pipeline_verify.add_argument(
        "--manifest",
        default="derived/verification/latest.json",
        help="Verification manifest JSON",
    )
    p_pipeline_verify.add_argument(
        "--strict-warnings",
        action="store_true",
        help="Treat validation warnings as failures",
    )
    p_pipeline_verify.add_argument("--vector-max-chars", type=int, default=900, help="Maximum vector chunk characters")
    p_pipeline_verify.add_argument("--vector-overlap", type=int, default=120, help="Vector chunk character overlap")
    p_pipeline_verify.add_argument("--json", action="store_true", help="Print JSON")
    p_pipeline_verify.set_defaults(func=cmd_pipeline_verify)

    # amendment
    p_amendment = subparsers.add_parser("amendment", help="Plan and apply canonical corpus amendments")
    amendment_sub = p_amendment.add_subparsers(dest="amendment_command")
    p_amendment_plan = amendment_sub.add_parser("plan", help="Plan amendments from a notification source archive")
    p_amendment_plan.add_argument("source_dir", help="Notification source archive directory")
    p_amendment_plan.add_argument("--corpus-dir", default="corpus", help="Canonical corpus directory")
    p_amendment_plan.add_argument(
        "--output",
        default="derived/amendments/amendment_plan.json",
        help="Output amendment plan JSON",
    )
    p_amendment_plan.set_defaults(func=cmd_amendment_plan)

    p_amendment_apply = amendment_sub.add_parser("apply", help="Apply supported amendments to an output corpus")
    p_amendment_apply.add_argument("source_dir", help="Notification source archive directory")
    p_amendment_apply.add_argument("--corpus-dir", default="corpus", help="Canonical corpus directory")
    p_amendment_apply.add_argument(
        "--output-corpus",
        required=True,
        help="Destination corpus directory; overwritten if it exists",
    )
    p_amendment_apply.add_argument(
        "--plan-output",
        default="derived/amendments/applied_plan.json",
        help="Output applied plan JSON",
    )
    p_amendment_apply.add_argument("--allow-partial", action="store_true", help="Apply ready mutations despite unresolved ones")
    p_amendment_apply.set_defaults(func=cmd_amendment_apply)

    p_amendment_promote = amendment_sub.add_parser("promote", help="Promote reviewed output corpus into canonical corpus")
    p_amendment_promote.add_argument("review_corpus", help="Reviewed output corpus directory")
    p_amendment_promote.add_argument("--target-corpus", default="corpus", help="Canonical corpus directory")
    p_amendment_promote.add_argument(
        "--manifest",
        default="derived/amendments/promotion_manifest.json",
        help="Promotion manifest path",
    )
    p_amendment_promote.add_argument("--approve", action="store_true", help="Actually copy reviewed XML into target corpus")
    p_amendment_promote.add_argument("--git-commit", action="store_true", help="Create a Git commit for promoted paths")
    p_amendment_promote.add_argument("--message", help="Git commit message")
    p_amendment_promote.set_defaults(func=cmd_amendment_promote)

    # version
    p_version = subparsers.add_parser("version", help="Compile and query event-sourced legal version history")
    version_sub = p_version.add_subparsers(dest="version_command")

    p_version_registry = version_sub.add_parser("registry-validate", help="Validate statute identity registry")
    p_version_registry.add_argument(
        "--registry",
        default="data/Law/statute_identity_registry.json",
        help="Curated statute identity registry JSON",
    )
    p_version_registry.set_defaults(func=cmd_version_registry_validate)

    p_version_llm_smoke = version_sub.add_parser("llm-smoke", help="Smoke-test configured OMLX JSON output")
    p_version_llm_smoke.add_argument("--llm-base-url", default=None, help="OMLX/OpenAI-compatible base URL")
    p_version_llm_smoke.add_argument("--llm-model", default=None, help="OMLX model ID")
    p_version_llm_smoke.add_argument("--llm-api-key-env", default="OMLX_API_KEY", help="Environment variable containing API key")
    p_version_llm_smoke.add_argument("--timeout", type=int, default=60, help="HTTP timeout in seconds")
    p_version_llm_smoke.set_defaults(func=cmd_version_llm_smoke)

    p_version_baseline = version_sub.add_parser("build-baseline", help="Build 2017 baseline XML/components")
    p_version_baseline.add_argument(
        "--registry",
        default="data/Law/statute_identity_registry.json",
        help="Curated statute identity registry JSON",
    )
    p_version_baseline.add_argument("--target-work", required=True, help="Canonical target work ID")
    p_version_baseline.add_argument("--output-dir", default=None, help="Override baseline output directory")
    p_version_baseline.add_argument(
        "--rules-pdf",
        default="data/Law/base_laws/cgst-rules-2017-part-a-rules.pdf",
        help="Original CGST Rules PDF",
    )
    p_version_baseline.add_argument(
        "--rules-repair-notification",
        default="data/Law/cbic_tax_portal/notifications/3-2017-central-tax-notifying-the-cgst-rules-2017-on-registration-and-composition_1000872.json",
        help="Optional CBIC notification JSON used as a source-priority repair track for original Rules components it covers",
    )
    p_version_baseline.add_argument(
        "--act-json",
        default="data/Law/base_laws/central_goods_and_services_tax_act_2017.json",
        help="Original CGST Act JSON",
    )
    p_version_baseline.add_argument("--use-llm", action="store_true", help="Run OMLX baseline reconciliation track")
    p_version_baseline.add_argument("--llm-base-url", default=None, help="OMLX/OpenAI-compatible base URL")
    p_version_baseline.add_argument("--llm-model", default=None, help="OMLX model ID")
    p_version_baseline.add_argument("--llm-api-key-env", default="OMLX_API_KEY", help="Environment variable containing API key")
    p_version_baseline.set_defaults(func=cmd_version_build_baseline)

    p_version_compile = version_sub.add_parser("compile-events", help="Compile CBIC notification amendment events")
    p_version_compile.add_argument(
        "--registry",
        default="data/Law/statute_identity_registry.json",
        help="Curated statute identity registry JSON",
    )
    p_version_compile.add_argument(
        "--source-dir",
        default="data/Law/cbic_tax_portal/notifications",
        help="Directory containing CBIC notification JSON records",
    )
    p_version_compile.add_argument(
        "--source-family",
        default="cbic-central-tax",
        choices=["cbic-central-tax", "finance-acts", "cbic-acts", "taxation-acts"],
        help="Source family to compile",
    )
    p_version_compile.add_argument("--category", default="Central Tax", help="CBIC notification category")
    p_version_compile.add_argument(
        "--target-work",
        default="/in/union/rules/cgst-rules-2017",
        help="Canonical target work ID",
    )
    p_version_compile.add_argument("--corpus-dir", default="corpus", help="Canonical corpus directory")
    p_version_compile.add_argument(
        "--source-archive-root",
        default="sources",
        help="Source archive root used as text fallback",
    )
    p_version_compile.add_argument(
        "--output",
        default="derived/version_history/amendment_events.jsonl",
        help="Output event JSONL path",
    )
    p_version_compile.add_argument(
        "--review-output",
        default="derived/version_history/review_report.json",
        help="Output review report JSON path",
    )
    p_version_compile.add_argument(
        "--extract-pdf-text",
        action="store_true",
        help="Fall back to extracting text from contentPdfBase64 when no text/source archive exists",
    )
    p_version_compile.add_argument("--use-llm", action="store_true", help="Use OMLX to propose unresolved candidates")
    p_version_compile.add_argument("--llm-base-url", default=None, help="OMLX/OpenAI-compatible base URL")
    p_version_compile.add_argument("--llm-model", default=None, help="OMLX model ID")
    p_version_compile.add_argument("--llm-api-key-env", default="OMLX_API_KEY", help="Environment variable containing API key")
    p_version_compile.add_argument(
        "--llm-cache",
        default="derived/version_history/llm_candidates.jsonl",
        help="JSONL cache for OMLX candidate responses",
    )
    p_version_compile.add_argument("--llm-limit", type=int, default=None, help="Maximum new OMLX calls for this run")
    p_version_compile.add_argument("--llm-concurrency", type=int, default=1, help="Maximum concurrent OMLX calls")
    p_version_compile.add_argument(
        "--commencement-dir",
        default="data/Law/cbic_tax_portal/notifications",
        help="CBIC notification directory for Finance Act commencement joins",
    )
    p_version_compile.set_defaults(func=cmd_version_compile_events)

    p_version_merge = version_sub.add_parser("merge-events", help="Merge event JSONL ledgers and flag conflicts")
    p_version_merge.add_argument("inputs", nargs="+", help="Input event JSONL files")
    p_version_merge.add_argument("--output", required=True, help="Merged output event JSONL")
    p_version_merge.add_argument("--review-output", default=None, help="Merged review report JSON")
    p_version_merge.set_defaults(func=cmd_version_merge_events)

    p_version_backfill = version_sub.add_parser(
        "missing-anchor-backfill",
        help="Write source-backed events for known missing rule anchors",
    )
    p_version_backfill.add_argument("--corpus-dir", default="corpus", help="Canonical corpus directory")
    p_version_backfill.add_argument(
        "--output",
        default="derived/version_history/missing_anchor_backfill_events.jsonl",
        help="Output backfill event JSONL",
    )
    p_version_backfill.add_argument(
        "--report-output",
        default="derived/version_history/missing_anchor_backfill_report.json",
        help="Output backfill report JSON",
    )
    p_version_backfill.set_defaults(func=cmd_version_missing_anchor_backfill)

    p_version_materialize = version_sub.add_parser("materialize", help="Materialize CGST Rules component versions")
    p_version_materialize.add_argument(
        "--target-work",
        default="/in/union/rules/cgst-rules-2017",
        help="Canonical target work ID",
    )
    p_version_materialize.add_argument(
        "--events",
        default="derived/version_history/amendment_events_reviewed.jsonl",
        help="Input event JSONL path",
    )
    p_version_materialize.add_argument(
        "--registry",
        default="data/Law/statute_identity_registry.json",
        help="Curated statute identity registry JSON",
    )
    p_version_materialize.add_argument("--corpus-dir", default="corpus", help="Canonical corpus directory")
    p_version_materialize.add_argument(
        "--output-dir",
        default="derived/version_history/cgst-rules-2017",
        help="Output version-history directory",
    )
    p_version_materialize.add_argument(
        "--write-snapshots",
        action="store_true",
        help="Write dated full XML snapshots in addition to node_versions.jsonl",
    )
    p_version_materialize.set_defaults(func=cmd_version_materialize)

    p_version_context_recovery = version_sub.add_parser(
        "context-recovery",
        help="Recover full-notification rule context and route work-ID review rows",
    )
    p_version_context_recovery.add_argument(
        "--events",
        default="derived/version_history/amendment_events_reviewed.jsonl",
        help="Input reviewed event JSONL path",
    )
    p_version_context_recovery.add_argument(
        "--output",
        default="derived/version_history/amendment_events_reviewed.jsonl",
        help="Output event JSONL path",
    )
    p_version_context_recovery.add_argument(
        "--decisions-output",
        default="derived/version_history/context_recovery_decisions.json",
        help="Context recovery decisions JSON path",
    )
    p_version_context_recovery.add_argument(
        "--report-output",
        default="derived/version_history/context_recovery_report.json",
        help="Context recovery report JSON path",
    )
    p_version_context_recovery.add_argument(
        "--source-archive-root",
        default="sources",
        help="Local source archive root",
    )
    p_version_context_recovery.add_argument(
        "--notifications-dir",
        default="data/Law/cbic_tax_portal/notifications",
        help="Fallback CBIC notification JSON directory",
    )
    p_version_context_recovery.set_defaults(func=cmd_version_context_recovery)

    p_version_corrigendum_application = version_sub.add_parser(
        "corrigendum-application",
        help="Apply deterministic corrigendum corrections to reviewed event payloads",
    )
    p_version_corrigendum_application.add_argument(
        "--events",
        default="derived/version_history/amendment_events_reviewed.jsonl",
        help="Input reviewed event JSONL path",
    )
    p_version_corrigendum_application.add_argument(
        "--output",
        default="derived/version_history/amendment_events_reviewed.jsonl",
        help="Output corrected event JSONL path",
    )
    p_version_corrigendum_application.add_argument(
        "--report-output",
        default="derived/version_history/corrigendum_application_report.json",
        help="Corrigendum application report JSON path",
    )
    p_version_corrigendum_application.set_defaults(func=cmd_version_corrigendum_application)

    p_version_compare = version_sub.add_parser("compare", help="Compare materialized component versions")
    p_version_compare.add_argument("component_id", help="Canonical or legacy component ID")
    p_version_compare.add_argument("--from-date", required=True, help="From date YYYY-MM-DD")
    p_version_compare.add_argument("--to-date", required=True, help="To date YYYY-MM-DD")
    p_version_compare.add_argument(
        "--temporal-dimension",
        default="applicability",
        choices=["applicability", "system"],
        help="Temporal dimension for comparison",
    )
    p_version_compare.add_argument(
        "--version-dir",
        default=None,
        help="Materialized version-history directory",
    )
    p_version_compare.add_argument(
        "--registry",
        default="data/Law/statute_identity_registry.json",
        help="Curated statute identity registry JSON",
    )
    p_version_compare.add_argument("--target-work", default=None, help="Canonical target work ID")
    p_version_compare.set_defaults(func=cmd_version_compare)

    p_version_reconstruct = version_sub.add_parser("reconstruct", help="Reconstruct a component's provision text at a date")
    p_version_reconstruct.add_argument("--component-id", required=True, help="Canonical or legacy component ID")
    p_version_reconstruct.add_argument("--date", required=True, help="As-of date YYYY-MM-DD")
    p_version_reconstruct.add_argument("--version-dir", default=None, help="Materialized version-history directory")
    p_version_reconstruct.add_argument(
        "--registry",
        default="data/Law/statute_identity_registry.json",
        help="Curated statute identity registry JSON",
    )
    p_version_reconstruct.set_defaults(func=cmd_version_reconstruct)

    p_version_surgical_queue = version_sub.add_parser(
        "surgical-gap-queue",
        help="Build an actionable queue of remaining materializer-facing Rules gaps",
    )
    p_version_surgical_queue.add_argument(
        "--coverage-gaps",
        default="derived/version_history/cgst-rules-2017/coverage_gaps.json",
        help="Rules coverage gaps JSON",
    )
    p_version_surgical_queue.add_argument(
        "--output",
        default="derived/version_history/cgst-rules-2017/surgical_gap_queue.json",
        help="Output surgical gap queue JSON",
    )
    p_version_surgical_queue.add_argument(
        "--focus-rule",
        action="append",
        default=["36", "45", "46", "138"],
        help="Rule number to prioritize; repeatable",
    )
    p_version_surgical_queue.add_argument("--limit", type=int, default=None, help="Maximum queue items to emit")
    p_version_surgical_queue.set_defaults(func=cmd_version_surgical_gap_queue)

    p_version_reconcile = version_sub.add_parser("reconcile", help="Reconcile reconstructed history with a checkpoint")
    p_version_reconcile.add_argument("--target-work", required=True, help="Canonical target work ID")
    p_version_reconcile.add_argument("--checkpoint-path", required=True, help="Checkpoint XML file or directory")
    p_version_reconcile.add_argument("--checkpoint-date", required=True, help="Checkpoint date YYYY-MM-DD")
    p_version_reconcile.add_argument(
        "--output",
        default="derived/version_history/reconciliation_report.json",
        help="Output reconciliation report JSON",
    )
    p_version_reconcile.add_argument(
        "--registry",
        default="data/Law/statute_identity_registry.json",
        help="Curated statute identity registry JSON",
    )
    p_version_reconcile.add_argument("--version-dir", default=None, help="Override version-history directory")
    p_version_reconcile.add_argument(
        "--events",
        default="derived/version_history/amendment_events_reviewed.jsonl",
        help="Reviewed amendment events JSONL used to attribute unresolved reconciliation items",
    )
    p_version_reconcile.add_argument(
        "--audit-output",
        default=None,
        help="Output unresolved reconciliation audit JSON; defaults next to the reconciliation report",
    )
    p_version_reconcile.set_defaults(func=cmd_version_reconcile)

    p_version_forms = version_sub.add_parser("materialize-forms", help="Build conservative GST forms version history")
    p_version_forms.add_argument(
        "--events",
        default="derived/version_history/amendment_events_reviewed.jsonl",
        help="Rules amendment event JSONL containing form targets",
    )
    p_version_forms.add_argument("--corpus-dir", default="corpus", help="Canonical corpus directory")
    p_version_forms.add_argument(
        "--output-dir",
        default="derived/version_history/forms",
        help="Output directory for forms version-history artifacts",
    )
    p_version_forms.add_argument("--base-as-of", default="2017-06-19", help="Forms baseline date")
    p_version_forms.add_argument(
        "--form-registry",
        default="derived/version_history/form_registry.json",
        help="Form baseline registry with pending/ready status",
    )
    p_version_forms.set_defaults(func=cmd_version_materialize_forms)

    p_version_phase3 = version_sub.add_parser(
        "phase3-backlog",
        help="Build the consolidated next backlog from current version-history artifacts",
    )
    p_version_phase3.add_argument(
        "--rules-coverage",
        default="derived/version_history/cgst-rules-2017/coverage_gaps.json",
        help="Rules coverage gaps JSON",
    )
    p_version_phase3.add_argument(
        "--forms-manifest",
        default="derived/version_history/forms/materialization_manifest.json",
        help="Forms materialization manifest JSON",
    )
    p_version_phase3.add_argument(
        "--portal-completeness",
        default="derived/version_history/portal_completeness_report.json",
        help="Portal completeness report JSON",
    )
    p_version_phase3.add_argument(
        "--confidence-tiers",
        default="derived/version_history/confidence_tiers.json",
        help="Confidence tiers JSON",
    )
    p_version_phase3.add_argument(
        "--act-audit",
        default="derived/version_history/cgst-act-2017/act_pipeline_audit.json",
        help="CGST Act audit JSON",
    )
    p_version_phase3.add_argument(
        "--reconciliation-report",
        default="derived/version_history/cgst-rules-2017/reconciliation_report.json",
        help="Rules reconciliation report JSON with unresolved audit data",
    )
    p_version_phase3.add_argument(
        "--output",
        default="derived/version_history/phase3_backlog.json",
        help="Output Phase 3 backlog JSON",
    )
    p_version_phase3.add_argument("--sample-limit", type=int, default=10, help="Maximum sample IDs per item")
    p_version_phase3.set_defaults(func=cmd_version_phase3_backlog)

    p_version_html = version_sub.add_parser("html-report", help="Write version-history HTML review report")
    p_version_html.add_argument(
        "--output",
        default="derived/version_history/review_report.html",
        help="Output HTML report path",
    )
    p_version_html.add_argument(
        "--review-report",
        default="derived/version_history/review_report.json",
        help="Compiler review report JSON",
    )
    p_version_html.add_argument(
        "--rules-manifest",
        default="derived/version_history/cgst-rules-2017/materialization_manifest.json",
        help="Rules materialization manifest JSON",
    )
    p_version_html.add_argument(
        "--rules-coverage",
        default="derived/version_history/cgst-rules-2017/coverage_gaps.json",
        help="Rules coverage gaps JSON",
    )
    p_version_html.add_argument(
        "--reconciliation-report",
        default="derived/version_history/cgst-rules-2017/reconciliation_report.json",
        help="Rules reconciliation report JSON",
    )
    p_version_html.add_argument(
        "--forms-manifest",
        default="derived/version_history/forms/materialization_manifest.json",
        help="Forms materialization manifest JSON",
    )
    p_version_html.add_argument(
        "--forms-coverage",
        default="derived/version_history/forms/coverage_gaps.json",
        help="Forms coverage gaps JSON",
    )
    p_version_html.add_argument(
        "--review-triage",
        default="derived/version_history/review_triage.json",
        help="Review triage JSON",
    )
    p_version_html.add_argument(
        "--review-decisions",
        default="derived/version_history/review_decisions.json",
        help="Manual review decisions JSON",
    )
    p_version_html.add_argument(
        "--auto-review-decisions",
        default="derived/version_history/auto_review_decisions.json",
        help="Auto review decisions JSON",
    )
    p_version_html.add_argument(
        "--dependency-review-decisions",
        default="derived/version_history/dependency_review_decisions.json",
        help="Dependency review decisions JSON",
    )
    p_version_html.add_argument(
        "--codex-review-decisions",
        default="derived/version_history/codex_review_decisions.json",
        help="Codex review decisions JSON",
    )
    p_version_html.add_argument(
        "--review-completion",
        default="derived/version_history/review_completion_report.json",
        help="Review completion report JSON",
    )
    p_version_html.add_argument(
        "--amendment-events",
        default="derived/version_history/amendment_events_reviewed.jsonl",
        help="Amendment-event ledger JSONL shown by the board",
    )
    p_version_html.add_argument(
        "--node-versions",
        default="derived/version_history/cgst-rules-2017/node_versions.jsonl",
        help="Rules node versions JSONL sample source",
    )
    p_version_html.add_argument(
        "--act-confidence-tiers",
        default="derived/version_history/cgst-act-2017/confidence_tiers.json",
        help="CGST Act confidence tiers JSON",
    )
    p_version_html.add_argument(
        "--act-reconciliation-report",
        default="derived/version_history/cgst-act-2017/reconciliation_report.json",
        help="CGST Act reconciliation report JSON",
    )
    p_version_html.add_argument(
        "--act-manifest",
        default="derived/version_history/cgst-act-2017/materialization_manifest.json",
        help="CGST Act materialization manifest JSON",
    )
    p_version_html.set_defaults(func=cmd_version_html_report)

    p_version_triage = version_sub.add_parser("triage-review", help="Triage needs_review events into smaller review groups")
    p_version_triage.add_argument(
        "--events",
        default="derived/version_history/amendment_events.jsonl",
        help="Input amendment events JSONL",
    )
    p_version_triage.add_argument(
        "--output",
        default="derived/version_history/review_triage.json",
        help="Output review triage JSON",
    )
    p_version_triage.add_argument("--use-llm", action="store_true", help="Use OMLX for ambiguous triage items")
    p_version_triage.add_argument("--llm-base-url", default=None, help="OMLX/OpenAI-compatible base URL")
    p_version_triage.add_argument("--llm-model", default=None, help="OMLX model ID")
    p_version_triage.add_argument("--llm-api-key-env", default="OMLX_API_KEY", help="Environment variable containing API key")
    p_version_triage.add_argument(
        "--llm-cache",
        default="derived/version_history/review_triage_llm_cache.jsonl",
        help="JSONL cache for OMLX triage responses",
    )
    p_version_triage.add_argument("--llm-limit", type=int, default=None, help="Maximum new OMLX calls for this run")
    p_version_triage.add_argument("--llm-concurrency", type=int, default=1, help="Maximum concurrent OMLX calls")
    p_version_triage.set_defaults(func=cmd_version_triage_review)

    p_version_apply_decisions = version_sub.add_parser(
        "apply-review-decisions",
        help="Apply curated review approvals to produce a promoted event ledger",
    )
    p_version_apply_decisions.add_argument(
        "--events",
        default="derived/version_history/amendment_events.jsonl",
        help="Input amendment events JSONL",
    )
    p_version_apply_decisions.add_argument(
        "--decisions",
        action="append",
        default=None,
        help="Curated review decisions JSON",
    )
    p_version_apply_decisions.add_argument(
        "--output",
        default="derived/version_history/amendment_events_promoted.jsonl",
        help="Output promoted amendment events JSONL",
    )
    p_version_apply_decisions.set_defaults(func=cmd_version_apply_review_decisions)

    p_version_auto_decisions = version_sub.add_parser(
        "auto-review-decisions",
        help="Generate conservative auto-approvals for mechanically verifiable review items",
    )
    p_version_auto_decisions.add_argument(
        "--events",
        default="derived/version_history/amendment_events.jsonl",
        help="Input amendment events JSONL",
    )
    p_version_auto_decisions.add_argument(
        "--triage",
        default="derived/version_history/review_triage.json",
        help="Review triage JSON",
    )
    p_version_auto_decisions.add_argument(
        "--existing-decisions",
        default="derived/version_history/review_decisions.json",
        help="Existing manual review decisions JSON to avoid duplicating",
    )
    p_version_auto_decisions.add_argument(
        "--output",
        default="derived/version_history/auto_review_decisions.json",
        help="Output auto review decisions JSON",
    )
    p_version_auto_decisions.add_argument("--limit", type=int, default=None, help="Maximum auto decisions to generate")
    p_version_auto_decisions.set_defaults(func=cmd_version_auto_review_decisions)

    p_version_dependency_decisions = version_sub.add_parser(
        "dependency-review-decisions",
        help="Generate conservative auto-approvals for events that create missing materializer anchors",
    )
    p_version_dependency_decisions.add_argument(
        "--events",
        default="derived/version_history/amendment_events.jsonl",
        help="Input amendment events JSONL",
    )
    p_version_dependency_decisions.add_argument(
        "--coverage-gaps",
        default="derived/version_history/cgst-rules-2017/coverage_gaps.json",
        help="Coverage gaps JSON produced by materialization",
    )
    p_version_dependency_decisions.add_argument(
        "--existing-decisions",
        action="append",
        default=None,
        help="Existing review decisions JSON to avoid duplicating",
    )
    p_version_dependency_decisions.add_argument(
        "--output",
        default="derived/version_history/dependency_review_decisions.json",
        help="Output dependency review decisions JSON",
    )
    p_version_dependency_decisions.add_argument("--limit", type=int, default=None, help="Maximum dependency decisions")
    p_version_dependency_decisions.set_defaults(func=cmd_version_dependency_review_decisions)

    p_version_codex_decisions = version_sub.add_parser(
        "codex-review-decisions",
        help="Generate Codex-approved exact text edit decisions",
    )
    p_version_codex_decisions.add_argument(
        "--events",
        default="derived/version_history/amendment_events_promoted.jsonl",
        help="Input amendment events JSONL",
    )
    p_version_codex_decisions.add_argument(
        "--node-versions",
        default="derived/version_history/cgst-rules-2017/node_versions.jsonl",
        help="Materialized node versions used to verify exact target text",
    )
    p_version_codex_decisions.add_argument(
        "--existing-decisions",
        action="append",
        default=None,
        help="Existing review decisions JSON to avoid duplicating",
    )
    p_version_codex_decisions.add_argument(
        "--output",
        default="derived/version_history/codex_review_decisions.json",
        help="Output Codex review decisions JSON",
    )
    p_version_codex_decisions.add_argument(
        "--source-archive-root",
        default="sources",
        help="Source archive root used to verify full source spans",
    )
    p_version_codex_decisions.add_argument("--limit", type=int, default=None, help="Maximum Codex decisions")
    p_version_codex_decisions.set_defaults(func=cmd_version_codex_review_decisions)

    p_version_complete_review = version_sub.add_parser(
        "complete-review",
        help="Classify every pending review item into a terminal audit state",
    )
    p_version_complete_review.add_argument(
        "--events",
        default="derived/version_history/amendment_events_reviewed.jsonl",
        help="Input amendment events JSONL",
    )
    p_version_complete_review.add_argument(
        "--rules-manifest",
        default="derived/version_history/cgst-rules-2017/materialization_manifest.json",
        help="Rules materialization manifest JSON",
    )
    p_version_complete_review.add_argument(
        "--rules-coverage",
        default="derived/version_history/cgst-rules-2017/coverage_gaps.json",
        help="Rules coverage gaps JSON",
    )
    p_version_complete_review.add_argument(
        "--forms-manifest",
        default="derived/version_history/forms/materialization_manifest.json",
        help="Forms materialization manifest JSON",
    )
    p_version_complete_review.add_argument(
        "--forms-coverage",
        default="derived/version_history/forms/coverage_gaps.json",
        help="Forms coverage gaps JSON",
    )
    p_version_complete_review.add_argument(
        "--reconciliation-report",
        default="derived/version_history/cgst-rules-2017/reconciliation_report.json",
        help="Rules reconciliation report JSON",
    )
    p_version_complete_review.add_argument(
        "--review-triage",
        default="derived/version_history/review_triage.json",
        help="Review triage JSON",
    )
    p_version_complete_review.add_argument(
        "--decisions",
        action="append",
        default=None,
        help="Review decision JSON inputs",
    )
    p_version_complete_review.add_argument(
        "--report-output",
        default="derived/version_history/review_completion_report.json",
        help="Output detailed review completion report JSON",
    )
    p_version_complete_review.add_argument(
        "--decisions-output",
        default="derived/version_history/review_completion_decisions.json",
        help="Output compact terminal review decisions JSON",
    )
    p_version_complete_review.set_defaults(func=cmd_version_complete_review)

    p_version_fetch = version_sub.add_parser("fetch-consolidated", help="Fetch consolidated source for reconciliation")
    p_version_fetch.add_argument("--target-work", required=True, help="Canonical target work ID")
    p_version_fetch.add_argument("--url", default=None, help="Consolidated source URL")
    p_version_fetch.add_argument(
        "--output-dir",
        default="derived/version_history/reconciliation_sources/current",
        help="Directory for fetched source and manifest",
    )
    p_version_fetch.add_argument("--timeout", type=int, default=60, help="HTTP timeout in seconds")
    p_version_fetch.add_argument(
        "--section-limit",
        type=int,
        default=None,
        help="Maximum taxinformation rule HTML sections to fetch; omit for full checkpoint",
    )
    p_version_fetch.add_argument(
        "--section-timeout",
        type=int,
        default=None,
        help="Per-section timeout for taxinformation HTML fetches; defaults to min(--timeout, 10)",
    )
    p_version_fetch.add_argument(
        "--section-concurrency",
        type=int,
        default=8,
        help="Concurrent taxinformation HTML section fetches; defaults to 8",
    )
    p_version_fetch.add_argument(
        "--insecure",
        action="store_true",
        help="Disable TLS certificate verification for official endpoints with broken local trust chains",
    )
    p_version_fetch.set_defaults(func=cmd_version_fetch_consolidated)

    p_version_alias_checkpoint = version_sub.add_parser(
        "alias-checkpoint",
        help="Alias an already-downloaded checkpoint into a provenanced reconciliation source directory",
    )
    p_version_alias_checkpoint.add_argument(
        "--source-dir",
        default="derived/version_history/reconciliation_sources/taxinformation-2022-12-26",
        help="Directory containing downloaded checkpoint.xml and checkpoint_components.jsonl",
    )
    p_version_alias_checkpoint.add_argument(
        "--output-dir",
        default="derived/version_history/reconciliation_sources/current-taxinformation",
        help="Directory for the aliased current checkpoint",
    )
    p_version_alias_checkpoint.add_argument(
        "--checkpoint-date",
        default="2026-06-17",
        help="Legal checkpoint date represented by this consolidated source",
    )
    p_version_alias_checkpoint.add_argument(
        "--source-label",
        default="current-cbic-taxinformation",
        help="Human-readable source label written into checkpoint_manifest.json",
    )
    p_version_alias_checkpoint.add_argument(
        "--required-label",
        action="append",
        default=["31C", "88C"],
        help="Rule label that must be present in the aliased checkpoint; repeatable",
    )
    p_version_alias_checkpoint.set_defaults(func=cmd_version_alias_checkpoint)

    p_version_portal = version_sub.add_parser(
        "portal-completeness",
        help="Compare TaxInformation/CBIC rule-page notification references against the event ledger",
    )
    p_version_portal.add_argument(
        "--html-dir",
        default="derived/version_history/reconciliation_sources/current-taxinformation/html",
        help="Directory containing current TaxInformation/CBIC rule HTML pages",
    )
    p_version_portal.add_argument(
        "--events",
        default="derived/version_history/amendment_events_reviewed.jsonl",
        help="Reviewed amendment events JSONL",
    )
    p_version_portal.add_argument(
        "--output",
        default="derived/version_history/portal_completeness_report.json",
        help="Output portal completeness report JSON",
    )
    p_version_portal.add_argument(
        "--rule",
        action="append",
        default=None,
        help="Limit to a rule number; repeatable. Defaults to all HTML pages found.",
    )
    p_version_portal.add_argument(
        "--top10-gap-report",
        default="derived/version_history/cgst-rules-2017/top10_gap_report.json",
        help="Optional top-10 gap report to annotate with portal missing-notification blockers",
    )
    p_version_portal.add_argument(
        "--coverage-gaps",
        default="derived/version_history/cgst-rules-2017/coverage_gaps.json",
        help="Rules coverage gaps JSON used to rebuild the top-10 gap report before portal annotation",
    )
    p_version_portal.add_argument(
        "--top-n",
        type=int,
        default=10,
        help="Number of high-gap rules to include when rebuilding the top-10 gap report",
    )
    p_version_portal.set_defaults(func=cmd_version_portal_completeness)

    p_version_tiers = version_sub.add_parser(
        "confidence-tiers",
        help="Compute per-component confidence tiers (A/B/C/D) for litigation readiness",
    )
    p_version_tiers.add_argument(
        "--version-dir",
        default="derived/version_history/cgst-rules-2017",
        help="Version-history directory containing node_versions.jsonl and coverage_gaps.json",
    )
    p_version_tiers.add_argument(
        "--reconciliation-report",
        default=None,
        help="Reconciliation report JSON (defaults to <version-dir>/reconciliation_report.json)",
    )
    p_version_tiers.add_argument(
        "--amendment-events",
        default="derived/version_history/amendment_events_reviewed.jsonl",
        help="Reviewed amendment events JSONL",
    )
    p_version_tiers.add_argument(
        "--baseline-components",
        default=None,
        help="Baseline components JSONL (defaults to the baseline dir from registry)",
    )
    p_version_tiers.add_argument(
        "--output",
        default="derived/version_history/confidence_tiers.json",
        help="Output confidence tiers JSON",
    )
    p_version_tiers.add_argument(
        "--portal-completeness",
        default="derived/version_history/portal_completeness_report.json",
        help="Portal completeness report JSON included as missing-source blockers when present",
    )
    p_version_tiers.set_defaults(func=cmd_version_confidence_tiers)

    p_version_act_audit = version_sub.add_parser(
        "act-audit",
        help="Write a non-blocking CGST Act pipeline audit report",
    )
    p_version_act_audit.add_argument(
        "--events",
        default="derived/version_history/cgst-act-2017/merged_amendment_events.jsonl",
        help="Merged CGST Act amendment events JSONL",
    )
    p_version_act_audit.add_argument(
        "--coverage-gaps",
        default="derived/version_history/cgst-act-2017/coverage_gaps.json",
        help="CGST Act coverage gaps JSON",
    )
    p_version_act_audit.add_argument(
        "--materialization-manifest",
        default="derived/version_history/cgst-act-2017/materialization_manifest.json",
        help="CGST Act materialization manifest JSON",
    )
    p_version_act_audit.add_argument(
        "--baseline-components",
        default="derived/version_history/baselines/cgst-act-2017/2017-04-12/baseline_components.jsonl",
        help="CGST Act baseline components JSONL",
    )
    p_version_act_audit.add_argument(
        "--baseline-reconciliation",
        default="derived/version_history/baselines/cgst-act-2017/2017-04-12/baseline_reconciliation.json",
        help="CGST Act baseline reconciliation JSON",
    )
    p_version_act_audit.add_argument(
        "--confidence-tiers",
        default="derived/version_history/cgst-act-2017/confidence_tiers.json",
        help="Act-specific confidence tiers JSON",
    )
    p_version_act_audit.add_argument(
        "--output",
        default="derived/version_history/cgst-act-2017/act_pipeline_audit.json",
        help="Output Act pipeline audit JSON",
    )
    p_version_act_audit.add_argument("--sample-limit", type=int, default=20, help="Maximum examples per audit queue")
    p_version_act_audit.set_defaults(func=cmd_version_act_audit)

    p_version_evidence = version_sub.add_parser(
        "evidence-bundle",
        help="Generate a citation-grade evidence bundle for a component and date range",
    )
    p_version_evidence.add_argument("--component-id", required=True, help="Canonical component ID")
    p_version_evidence.add_argument("--from-date", required=True, help="Start date YYYY-MM-DD")
    p_version_evidence.add_argument("--to-date", required=True, help="End date YYYY-MM-DD")
    p_version_evidence.add_argument("--version-dir", default="derived/version_history/cgst-rules-2017", help="Version directory")
    p_version_evidence.add_argument("--events", default="derived/version_history/amendment_events_reviewed.jsonl", help="Events JSONL")
    p_version_evidence.add_argument("--baseline-dir", default=None, help="Baseline components directory")
    p_version_evidence.add_argument("--confidence-tiers", default=None, help="Confidence tiers JSON")
    p_version_evidence.add_argument("--portal-completeness", default=None, help="Portal completeness JSON")
    p_version_evidence.add_argument("--output", default="derived/version_history/evidence_bundles/bundle.json", help="Output bundle JSON")
    p_version_evidence.set_defaults(func=cmd_version_evidence_bundle)

    p_version_evidence_html = version_sub.add_parser(
        "evidence-bundle-html",
        help="Render an evidence bundle JSON as a self-contained HTML document",
    )
    p_version_evidence_html.add_argument("--bundle", required=True, help="Evidence bundle JSON")
    p_version_evidence_html.add_argument("--output", default="derived/version_history/evidence_bundles/bundle.html", help="Output HTML")
    p_version_evidence_html.set_defaults(func=cmd_version_evidence_bundle_html)

    # ── rate schedule commands ───────────────────────────────────────────
    p_rate_parse = version_sub.add_parser("rate-parse-base", help="Parse a CT(Rate) notification XML into structured JSON")
    p_rate_parse.add_argument("--xml", required=True, help="Notification XML path")
    p_rate_parse.add_argument("--output", required=True, help="Output JSON path")
    p_rate_parse.add_argument("--type", default="", help="Instrument type override")

    p_rate_compile = version_sub.add_parser("rate-compile", help="Compile CT(Rate) amending notifications into rate events")
    p_rate_compile.add_argument("--corpus-dir", default="corpus/in/union/notifications/cbic/central-tax-rate")
    p_rate_compile.add_argument("--output", default="derived/version_history/rate-schedules/rate_amendment_events.jsonl")

    p_rate_materialize = version_sub.add_parser("rate-materialize", help="Materialize rate schedules by replaying events")
    p_rate_materialize.add_argument("--base", required=True, help="Base notification JSON")
    p_rate_materialize.add_argument("--events", required=True, help="Rate events JSONL")
    p_rate_materialize.add_argument("--target", required=True, help="Target notification e.g. 1/2017-ct-rate")
    p_rate_materialize.add_argument("--checkpoint-date", default="", help="Checkpoint date YYYY-MM-DD")
    p_rate_materialize.add_argument("--output", default="", help="Output snapshot JSON")

    p_rate_reconcile = version_sub.add_parser("rate-reconcile", help="Reconcile materialized schedules against checkpoints")
    p_rate_reconcile.add_argument("--base", default="derived/version_history/rate-schedules/base_1-2017.json")
    p_rate_reconcile.add_argument("--events", default="derived/version_history/rate-schedules/rate_amendment_events.jsonl")
    p_rate_reconcile.add_argument("--target", default="1/2017-ct-rate")
    p_rate_reconcile.add_argument("--checkpoint-date", default="2022-05-01")
    p_rate_reconcile.add_argument("--checkpoint-dir", default="derived/version_history/rate-schedules/checkpoints")
    p_rate_reconcile.add_argument("--output", default="derived/version_history/rate-schedules/reconciliation_report.json")

    p_rate_ledger = version_sub.add_parser("rate-ledger", help="Generate the HSN-to-rate ledger")
    p_rate_ledger.add_argument("--base", default="derived/version_history/rate-schedules/base_1-2017.json")
    p_rate_ledger.add_argument("--events", default="derived/version_history/rate-schedules/rate_amendment_events.jsonl")
    p_rate_ledger.add_argument("--target", default="1/2017-ct-rate")
    p_rate_ledger.add_argument("--checkpoint-date", default="")
    p_rate_ledger.add_argument("--output", default="derived/version_history/rate-schedules/rate_ledger.json")

    p_rate_validate = version_sub.add_parser("rate-validate", help="Horizontal HSN consistency validation")
    p_rate_validate.add_argument("--base", default="derived/version_history/rate-schedules/base_1-2017.json")
    p_rate_validate.add_argument("--events", default="derived/version_history/rate-schedules/rate_amendment_events.jsonl")
    p_rate_validate.add_argument("--target", default="1/2017-ct-rate")
    p_rate_validate.add_argument("--checkpoint-date", default="")

    p_rate_concessional = version_sub.add_parser("rate-parse-concessional", help="Parse concessional rate notifications")
    p_rate_concessional.add_argument("--corpus-dir", default="corpus/in/union/notifications/cbic/central-tax-rate")
    p_rate_concessional.add_argument("--output-dir", default="derived/version_history/rate-schedules")

    p_rate_query = version_sub.add_parser("rate-query", help="Query rate for HSN across all instruments")
    p_rate_query.add_argument("--hsn", required=True, help="HSN code e.g. 6815")
    p_rate_query.add_argument("--date", required=True, help="Query date YYYY-MM-DD")
    p_rate_query.add_argument("--base", default="derived/version_history/rate-schedules/base_1-2017.json")
    p_rate_query.add_argument("--events", default="derived/version_history/rate-schedules/rate_amendment_events.jsonl")
    p_rate_query.add_argument("--target", default="1/2017-ct-rate")

    p_rate_adjudicate = version_sub.add_parser("rate-adjudicate", help="Adjudicate mismatches: identify CBIC discrepancies")
    p_rate_adjudicate.add_argument("--base", default="derived/version_history/rate-schedules/base_1-2017.json")
    p_rate_adjudicate.add_argument("--events", default="derived/version_history/rate-schedules/rate_amendment_events.jsonl")
    p_rate_adjudicate.add_argument("--target", default="1/2017-ct-rate")
    p_rate_adjudicate.add_argument("--checkpoint-date", default="2022-05-01")
    p_rate_adjudicate.add_argument("--checkpoint-dir", default="derived/version_history/rate-schedules/checkpoints")
    p_rate_adjudicate.add_argument("--output", default="derived/version_history/rate-schedules/adjudication_report.json")
    
    # ── rate schedule command handlers ───────────────────────────────────
    def cmd_rate_parse_base(args):
        from legal_corpus.rate_schedule_parser import parse_and_save
        rn = parse_and_save(args.xml, args.output, args.type)
        print(f"Parsed {rn.cbic_no}: {len(rn.schedules)} schedules, {rn.entry_count()} entries")
        for sid, sched in sorted(rn.schedules.items()):
            print(f"  Schedule {sid} ({sched.rate_pct}%): {len(sched.entries)} entries")

    def cmd_rate_compile(args):
        from legal_corpus.rate_schedule_compiler import compile_all_amendments
        events = compile_all_amendments(args.corpus_dir, output_path=args.output)
        print(f"Compiled {len(events)} events → {args.output}")
        from collections import Counter
        for op, cnt in Counter(e.operation for e in events).most_common():
            print(f"  {op}: {cnt}")

    def cmd_rate_materialize(args):
        from legal_corpus.rate_schedule_materializer import materialize_schedule
        result = materialize_schedule(
            args.base, args.events, args.target,
            output_path=args.output or None,
            checkpoint_date=args.checkpoint_date or None,
        )
        print(f"Target: {args.target}")
        if args.checkpoint_date:
            print(f"Checkpoint: {args.checkpoint_date}")
        print(f"Events applied: {result['events_applied']}, failed: {result['events_failed']}")
        print(f"Total entries: {result['total_entries']}")
        for sid, sched in sorted(result["schedules"].items()):
            active = len([e for e in sched["entries"] if not e.get("is_omitted")])
            print(f"  Schedule {sid} ({sched['rate_pct']}%): {active} active, {len(sched['entries'])} total")

    def cmd_rate_reconcile(args):
        from legal_corpus.rate_schedule_materializer import materialize_schedule
        from legal_corpus.rate_reconciliation import reconcile_schedule
        import json
        snap = materialize_schedule(
            args.base, args.events, args.target,
            checkpoint_date=args.checkpoint_date or None,
        )
        import os
        cp_files = [f for f in os.listdir(args.checkpoint_dir) if f.startswith("checkpoint_")]
        for cpf in sorted(cp_files):
            cp_date = cpf.replace("checkpoint_", "").replace(".json", "")
            if args.checkpoint_date and cp_date != args.checkpoint_date:
                continue
            with open(os.path.join(args.checkpoint_dir, cpf)) as f:
                cp = json.load(f)
            report = reconcile_schedule(snap, cp, args.target)
            cp_entries = sum(s["entry_counts"].get("exact_match",0)+s["entry_counts"].get("format_only_match",0)+
                            s["entry_counts"].get("description_mismatch",0)+s["entry_counts"].get("tariff_mismatch",0)+
                            s["entry_counts"].get("missing_in_materialized",0)+s["entry_counts"].get("omitted_match",0)
                            for s in report.get("schedules",{}).values())
            mat_entries = sum(s["entry_counts"].get("exact_match",0)+s["entry_counts"].get("format_only_match",0)+
                             s["entry_counts"].get("description_mismatch",0)+s["entry_counts"].get("tariff_mismatch",0)+
                             s["entry_counts"].get("missing_in_checkpoint",0)+s["entry_counts"].get("omitted_match",0)
                             for s in report.get("schedules",{}).values())
            matched = sum(s["entry_counts"].get("exact_match",0)+s["entry_counts"].get("format_only_match",0)
                         for s in report.get("schedules",{}).values())
            print(f"Checkpoint {cp_date}: cp={cp_entries} mat={mat_entries} matched={matched} ({matched/max(cp_entries,1)*100:.1f}%)")
            for sid in sorted(report.get("schedules",{}).keys()):
                s = report["schedules"][sid]
                ec = s["entry_counts"]
                print(f"  Sched {sid}: exact={ec.get('exact_match',0)} fmt={ec.get('format_only_match',0)} "
                      f"miss_cp={ec.get('missing_in_materialized',0)} miss_mat={ec.get('missing_in_checkpoint',0)} "
                      f"tariff_mm={ec.get('tariff_mismatch',0)} desc_mm={ec.get('description_mismatch',0)}")
        if args.output:
            with open(args.output, "w") as f:
                json.dump(report, f, indent=2, ensure_ascii=False)

    def cmd_rate_ledger(args):
        from legal_corpus.rate_schedule_materializer import materialize_schedule
        from legal_corpus.rate_ledger import build_rate_ledger
        snap = materialize_schedule(
            args.base, args.events, args.target,
            checkpoint_date=args.checkpoint_date or None,
        )
        ledger = build_rate_ledger([snap], args.output)
        print(f"Rate ledger: {ledger['total_hsns']} HSNs")
        print(f"Saved to {args.output}")

    def cmd_rate_validate(args):
        from legal_corpus.rate_schedule_materializer import materialize_schedule
        from legal_corpus.rate_horizontal_validator import validate_horizontal
        snap = materialize_schedule(
            args.base, args.events, args.target,
            checkpoint_date=args.checkpoint_date or None,
        )
        result = validate_horizontal(snap)
        print(f"Valid: {result['valid']}")
        print(f"Errors: {len(result['errors'])}")
        print(f"Warnings: {len(result['warnings'])}")
        print(f"Stats: {result['stats']}")
        for err in result["errors"][:10]:
            print(f"  ERROR: {err}")
        for warn in result["warnings"][:5]:
            print(f"  WARN: {warn}")

    def cmd_rate_concessional(args):
        from legal_corpus.rate_concessional_parser import parse_all_concessional
        results = parse_all_concessional(args.corpus_dir, args.output_dir)
        print(f"Parsed {len(results)} concessional instruments:")
        for sid, result in sorted(results.items()):
            entries = result.get("schedules", {}).get("I", {}).get("entries", [])
            rate = result.get("rate_pct", 0)
            conds = result.get("conditions", [])
            print(f"  {sid}: rate={rate}%, entries={len(entries)}, conditions={len(conds)}")

    def cmd_rate_query(args):
        from legal_corpus.rate_schedule_materializer import materialize_schedule
        from legal_corpus.rate_ledger import build_rate_ledger, query_rate_multi_instrument
        from legal_corpus.rate_concessional_parser import parse_all_concessional
        snap = materialize_schedule(
            args.base, args.events, args.target,
            checkpoint_date=args.date,
        )
        ledger = build_rate_ledger([snap])
        concessional = parse_all_concessional()
        result = query_rate_multi_instrument(ledger, concessional, args.hsn, args.date)
        print(f"HSN: {result['hsn']} @ {result['query_date']}")
        if result["primary_rate"]:
            pr = result["primary_rate"]
            print(f"  Primary:      CGST={pr['cgst_rate_pct']}% GST={pr['gst_rate_pct']}% "
                  f"Schedule={pr['schedule']} ({pr['notification']})")
        for cr in result["concessional_rates"]:
            print(f"  Concessional: CGST={cr['cgst_rate_pct']}% GST={cr['gst_rate_pct']}% "
                  f"({cr['notification']}) — {cr['description'][:60]}")
        if result["exemption"]:
            print(f"  Exemption:    {result['exemption']}")

    def cmd_rate_adjudicate(args):
        from legal_corpus.rate_schedule_materializer import materialize_schedule
        from legal_corpus.rate_reconciliation import reconcile_schedule
        from legal_corpus.rate_confidence import generate_adjudication_report
        import os
        snap = materialize_schedule(
            args.base, args.events, args.target,
            checkpoint_date=args.checkpoint_date or None,
        )
        cp_file = os.path.join(args.checkpoint_dir, f"checkpoint_{args.checkpoint_date}.json")
        with open(cp_file) as f:
            cp = json.load(f)
        report = reconcile_schedule(snap, cp, args.target)
        adj = generate_adjudication_report(report, args.events, output_path=args.output)
        s = adj["summary"]
        print(f"Adjudication: {args.target} @ {args.checkpoint_date}")
        print(f"  Total:          {s['total']}")
        print(f"  Matched:        {s['matched']} ({s['match_rate']}%)")
        print(f"  Conf match:     {s['confidence_match_rate']}% (incl CP errors + CBIC discrepancies)")
        print(f"  Mat errors:     {s['materializer_errors']}")
        print(f"  CBIC discr:     {s['cbic_discrepancies']}")
        print(f"  Ambiguous:      {s['ambiguous']}")
        cbic = [e for e in adj["entries"] if e["verdict"] == "cbic_discrepancy"]
        if cbic:
            print(f"\n  CBIC Discrepancies:")
            for e in cbic[:10]:
                print(f"    Sched {e['schedule']} sno={e['sno']}: {e['evidence'][:100]}")

    p_rate_parse.set_defaults(func=cmd_rate_parse_base)
    p_rate_compile.set_defaults(func=cmd_rate_compile)
    p_rate_materialize.set_defaults(func=cmd_rate_materialize)
    p_rate_reconcile.set_defaults(func=cmd_rate_reconcile)
    p_rate_ledger.set_defaults(func=cmd_rate_ledger)
    p_rate_validate.set_defaults(func=cmd_rate_validate)
    p_rate_concessional.set_defaults(func=cmd_rate_concessional)
    p_rate_query.set_defaults(func=cmd_rate_query)
    p_rate_adjudicate.set_defaults(func=cmd_rate_adjudicate)

    args = parser.parse_args()
    
    if args.command is None or not hasattr(args, "func"):
        parser.print_help()
        return
    
    args.func(args)


if __name__ == "__main__":
    main()
