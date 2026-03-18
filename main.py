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
from datetime import datetime
from pathlib import Path
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.syntax import Syntax

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
    
    args = parser.parse_args()
    
    if args.command is None:
        parser.print_help()
        return
    
    args.func(args)


if __name__ == "__main__":
    main()
