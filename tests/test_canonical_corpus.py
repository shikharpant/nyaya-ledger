import json
import base64
import hashlib
import re
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

import src.legal_corpus.structure_parser as structure_parser
import src.legal_corpus.source_archive as source_archive
from src.legal_corpus.api_payload import build_api_payload, write_api_payload
from src.legal_corpus.amendment_events import (
    SourceRecord,
    amendment_blocks,
    compile_events,
    compile_events_from_text,
    parse_legal_date,
    read_events,
)
from src.legal_corpus.amendments import apply_amendments, plan_amendments, promote_amended_corpus
from src.legal_corpus.batch_ingest import ingest_inventory, write_batch_ingest_report
from src.legal_corpus.diff import compare_corpora, write_diff_report
from src.legal_corpus.graph_index import build_graph_index, build_neo4j_payload, rebuild_graph_index, write_neo4j_payload
from src.legal_corpus.forms import split_forms_archive
from src.legal_corpus.html_renderer import build_html_site, write_html_site
from src.legal_corpus.identity_registry import load_registry, validate_registry
from src.legal_corpus.ingest import ingest_source_file
from src.legal_corpus.paths import expected_corpus_relative_path
from src.legal_corpus.quality import audit_corpus_quality, write_quality_report
from src.legal_corpus.query import export_text, list_documents, lookup_canonical_id, normalize_query_id
from src.legal_corpus.references import (
    build_reference_resolver,
    build_unresolved_reference_report,
    write_unresolved_reference_report,
    write_unresolved_reference_summary,
)
from src.legal_corpus.review import apply_batch_promotion, plan_batch_promotion, write_promotion_plan
from src.legal_corpus.renderer import canonicalize_legacy_reference, render_source_document, write_xml
from src.legal_corpus.search_index import build_search_records, read_search_index, search_index, search_records, write_search_index
from src.legal_corpus.seed import seed_from_existing_data
from src.legal_corpus.serving import NyayaToolService
import src.legal_corpus.source_inventory as source_inventory
from src.legal_corpus.source_inventory import (
    build_source_inventory,
    build_inventory_report,
    canonical_cbic_notification_id,
    category_slug,
    validate_source_inventory,
    validate_source_inventory_file,
    write_inventory_report,
    write_source_inventory,
)
from src.legal_corpus.source_archive import archive_source, extract_source_text, read_metadata_yaml
from src.legal_corpus.structure_parser import parse_structure, parse_structure_deterministic, validate_structure_spans
from src.legal_corpus.validator import validate_corpus, validate_source_archive, validate_xml_file, validate_xml_source_spans
from src.legal_corpus.vector_index import build_vector_chunks, read_vector_chunks, write_vector_chunks
from src.legal_corpus.version_compare import compare_component_versions
from src.legal_corpus.review_decisions import (
    apply_review_decisions,
    generate_auto_review_decisions,
    generate_codex_review_decisions,
    generate_dependency_review_decisions,
)
from src.legal_corpus.review_completion import complete_review
from src.legal_corpus.context_recovery import recover_context, recover_context_from_text, triage_event
from src.legal_corpus.version_snapshots import materialize_versions
from src.legal_corpus.verification import run_verification


ROOT = Path(__file__).resolve().parents[1]


def test_canonicalize_legacy_reference():
    assert canonicalize_legacy_reference("CGST_Rules/Rule_10/SubRule_1") == (
        "/in/union/rules/cgst-rules-2017/rule/10/subrule/1"
    )


def test_context_recovery_searches_full_text_backward_for_rule_context():
    full_text = (
        "In the Central Goods and Services Tax Rules, 2017, in rule 36, "
        "after the words input tax credit, the following words shall be inserted."
    )
    start = full_text.index("after the words")

    recovered = recover_context_from_text(full_text, start, "after the words input tax credit")

    assert recovered["component_id"] == "/in/union/rules/cgst-rules-2017/rule/36"
    assert recovered["matched_text"].lower().endswith("rule 36")


def test_context_recovery_promotes_subrule_from_excerpt():
    full_text = "In the said rules, in rule 88, after sub-rule (2), the following sub-rule shall be inserted."
    start = full_text.index("after sub-rule")

    recovered = recover_context_from_text(full_text, start, "in sub-rule (3), after the words")

    assert recovered["component_id"] == "/in/union/rules/cgst-rules-2017/rule/88/subrule/3"
    assert recovered["subrule_promoted_from_excerpt"] is True


def test_context_recovery_inherits_rule_then_subrule_from_full_text_variant():
    full_text = (
        "In rule 45 of the said rules, in sub-rule (3), with effect from the 1st day of October, 2021, - "
        "(i) for the words during a quarter, the words during a specified period shall be substituted; "
        "(ii) for the words the said quarter, the words the said period shall be substituted;"
    )
    start = full_text.index("(ii)")

    recovered = recover_context_from_text(full_text, start, "for the words the said quarter")

    assert recovered["component_id"] == "/in/union/rules/cgst-rules-2017/rule/45/subrule/3"
    assert recovered["matched_text"].lower().startswith("in rule 45 of the said rules, in sub-rule (3")


def test_context_recovery_prefers_explicit_excerpt_rule_context():
    full_text = (
        "In the said rules, in sub-rule (4A) of rule 8, for the words old words, new words shall be substituted. "
        "Later amendment text says in rule 46, in clause (f), the following proviso shall be inserted."
    )
    start = full_text.index("the following proviso")

    recovered = recover_context_from_text(
        full_text,
        start,
        "In the said rules, in rule 46, in clause (f), the following proviso shall be inserted.",
    )

    assert recovered["component_id"] == "/in/union/rules/cgst-rules-2017/rule/46"
    assert recovered["matched_in_excerpt"] is True


def test_context_recovery_prefers_rule_then_subrule_target_over_quoted_rule_reference():
    excerpt = (
        "in rule 19, in sub-rule (1), in the second proviso, for the words "
        "“the said rule”, the words, brackets and figures “sub-rule (2) of rule 8” shall be substituted"
    )
    recovered = recover_context_from_text("In the said rules, " + excerpt, 20, excerpt)

    assert recovered["component_id"] == "/in/union/rules/cgst-rules-2017/rule/19/subrule/1"
    assert recovered["matched_in_excerpt"] is True


def test_context_recovery_prefers_instruction_rule_over_later_cross_reference():
    excerpt = (
        "In the said rules, in rule 46, after clause (r), the following clause shall be inserted, namely: "
        "“(s) a declaration that invoice is not required to be issued under sub-rule (4) of rule 48.”"
    )
    recovered = recover_context_from_text("prefix " + excerpt, 20, excerpt)

    assert recovered["component_id"] == "/in/union/rules/cgst-rules-2017/rule/46"
    assert recovered["matched_instruction_prefix"] is True


def test_context_recovery_prefers_source_archive_and_routes_lanes(tmp_path):
    events_path = tmp_path / "events.jsonl"
    output_path = tmp_path / "out.jsonl"
    decisions_path = tmp_path / "decisions.json"
    report_path = tmp_path / "report.json"
    source_text = "PDF text says in rule 1. Archive text says in rule 36, after the words."
    start = source_text.index("after the words")
    event = {
        "event_id": "evt_context",
        "operation": "SPLICE",
        "status": "needs_review",
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": "/in/union/rules/cgst-rules-2017/rule/58",
        },
        "payload": {"insert_text": "x"},
        "evidence": {"excerpt": "after the words", "source_span": {"start": start, "end": start + 15}},
        "source": {
            "document_id": "/in/union/notifications/cbic/central-tax/2020/82-2020",
            "record_id": "record-82",
        },
        "review": {"required": True, "review_reasons": ["target_not_resolved"]},
        "validation": {"materializable": False},
    }
    form_event = {
        **event,
        "event_id": "evt_form",
        "evidence": {"excerpt": "in FORM GST DRC-03", "source_span": {"start": 0, "end": 18}},
    }
    metadata_event = {
        **event,
        "event_id": "evt_meta",
        "payload": {},
        "evidence": {"excerpt": "short title and commencement", "source_span": {"start": 4, "end": 4}},
    }
    malformed_event = {
        **event,
        "event_id": "evt_malformed",
        "target": {"work_id": "/in/union/rules/cgst-rules-2017", "component_id": "CGST_Rule_88"},
    }
    events_path.write_text(
        "\n".join(json.dumps(row, sort_keys=True) for row in [event, form_event, metadata_event, malformed_event]) + "\n",
        encoding="utf-8",
    )
    archive_dir = tmp_path / "sources" / "cbic" / "central-tax" / "2020" / "82-2020"
    archive_dir.mkdir(parents=True)
    (archive_dir / "extracted_text.json").write_text(json.dumps({"text": source_text}), encoding="utf-8")

    report = recover_context(
        events_path=events_path,
        output=output_path,
        decisions_output=decisions_path,
        report_output=report_path,
        source_archive_root=tmp_path / "sources",
        notifications_dir=tmp_path / "notifications",
    )
    rows = [json.loads(line) for line in output_path.read_text(encoding="utf-8").splitlines()]
    by_id = {row["event_id"]: row for row in rows}

    assert by_id["evt_context"]["target"]["component_id"] == "/in/union/rules/cgst-rules-2017/rule/36"
    assert by_id["evt_context"]["payload"]["context_recovery"]["text_source"] == "source_archive"
    assert by_id["evt_form"]["payload"]["triage_lane"] == "forms_lane_pending_baseline"
    assert by_id["evt_meta"]["payload"]["metadata_only"] is True
    assert by_id["evt_malformed"]["target"]["component_id"] == "/in/union/rules/cgst-rules-2017/rule/36"
    assert report["context_recovered_count"] == 2
    assert report["forms_lane_pending_baseline_count"] == 1
    assert report["metadata_only_count"] == 1


def test_context_recovery_skips_validated_events(tmp_path):
    events_path = tmp_path / "events.jsonl"
    output_path = tmp_path / "out.jsonl"
    event = {
        "event_id": "evt_validated",
        "operation": "SPLICE",
        "status": "validated",
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": "/in/union/rules/cgst-rules-2017/rule/58",
        },
        "payload": {"insert_text": "x"},
        "evidence": {"excerpt": "after the words", "source_span": {"start": 40, "end": 55}},
        "source": {
            "document_id": "/in/union/notifications/cbic/central-tax/2020/82-2020",
            "record_id": "record-82",
        },
        "review": {"required": False, "review_reasons": []},
        "validation": {"materializable": True},
    }
    events_path.write_text(json.dumps(event, sort_keys=True) + "\n", encoding="utf-8")
    archive_dir = tmp_path / "sources" / "cbic" / "central-tax" / "2020" / "82-2020"
    archive_dir.mkdir(parents=True)
    (archive_dir / "extracted_text.json").write_text(
        json.dumps({"text": "In the said rules, in rule 36, after the words."}),
        encoding="utf-8",
    )

    report = recover_context(
        events_path=events_path,
        output=output_path,
        decisions_output=tmp_path / "decisions.json",
        report_output=tmp_path / "context_recovery_report.json",
        source_archive_root=tmp_path / "sources",
        notifications_dir=tmp_path / "notifications",
    )
    [recovered] = [json.loads(line) for line in output_path.read_text(encoding="utf-8").splitlines()]

    assert report["context_recovered_count"] == 0
    assert recovered == event


def test_context_recovery_preserves_structural_insert_targets_from_payload(tmp_path):
    events_path = tmp_path / "events.jsonl"
    output_path = tmp_path / "out.jsonl"
    decisions_path = tmp_path / "decisions.json"
    report_path = tmp_path / "report.json"
    source_text = (
        "In the Central Goods and Services Tax Rules, 2017, after rule 44, "
        "the following rule shall be inserted, namely: 44A. Credit transfer. "
        "In the said rules, in rule 127, after clause (iii), the following clause shall be inserted."
    )
    sibling_start = source_text.index("the following rule")
    child_start = source_text.index("after clause")
    sibling_event = {
        "event_id": "evt_sibling",
        "operation": "INSERT_SIBLING",
        "status": "needs_review",
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": "/in/union/rules/cgst-rules-2017",
            "anchor_component_id": "/in/union/rules/cgst-rules-2017/rule/44",
        },
        "payload": {"node_type": "rule", "label": "44A", "content": "Credit transfer."},
        "evidence": {"excerpt": "the following rule shall be inserted", "source_span": {"start": sibling_start, "end": sibling_start + 30}},
        "source": {
            "document_id": "/in/union/notifications/cbic/central-tax/2017/22-2017",
            "record_id": "record-22",
        },
        "review": {"required": True, "review_reasons": ["target_not_resolved"]},
        "validation": {"materializable": False},
    }
    child_event = {
        **sibling_event,
        "event_id": "evt_child",
        "operation": "INSERT_CHILD",
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": "/in/union/rules/cgst-rules-2017/rule/58",
        },
        "payload": {
            "node_type": "clause",
            "label": "(iv)",
            "parent_component_id": "/in/union/rules/cgst-rules-2017/rule/127",
            "content": "to furnish a performance report.",
        },
        "evidence": {"excerpt": "the following clause shall be inserted", "source_span": {"start": child_start, "end": child_start + 30}},
    }
    subrule_event = {
        **child_event,
        "event_id": "evt_subrule",
        "payload": {
            "node_type": "sub-rule",
            "label": "sub-rule (a)",
            "parent_component_id": "/in/union/rules/cgst-rules-2017/rule/2",
            "content": "a new definition.",
        },
    }
    explanation_event = {
        **child_event,
        "event_id": "evt_explanation",
        "payload": {
            "node_type": "explanation",
            "label": "Explanation",
            "parent_component_id": "/in/union/rules/cgst-rules-2017/rule/44",
            "content": "For the purposes of this rule.",
        },
    }
    inserted_subrule_event = {
        **child_event,
        "event_id": "evt_inserted_subrule_label_from_content",
        "payload": {
            "node_type": "sub-rule",
            "label": "sub-rule (5)",
            "parent_component_id": "/in/union/rules/cgst-rules-2017/rule/92",
            "content": "- “(4A) The Central Government shall disburse the refund.”;",
        },
    }
    inserted_subrule_label_from_excerpt_event = {
        **child_event,
        "event_id": "evt_inserted_subrule_label_from_excerpt",
        "payload": {
            "node_type": "sub-rule",
            "label": "1",
            "parent_component_id": "/in/union/rules/cgst-rules-2017/rule/36",
            "content": "Input tax credit to be availed by a registered person.",
        },
        "evidence": {
            "excerpt": "after sub-rule (3), the following sub-rule shall be inserted, namely:- “(4) Input tax credit to be availed by a registered person.”",
            "source_span": {"start": child_start, "end": child_start + 30},
        },
    }
    inserted_subrule_label_from_pdf_quote_event = {
        **child_event,
        "event_id": "evt_inserted_subrule_label_from_pdf_quote",
        "payload": {
            "node_type": "sub-rule",
            "label": "1",
            "parent_component_id": "/in/union/rules/cgst-rules-2017/rule/80",
            "content": "Notwithstanding anything contained in sub-rule (1).",
        },
        "evidence": {
            "excerpt": "after sub-rule (1), the following sub-rule shall be inserted, namely:- ―(1A) Notwithstanding anything contained in sub-rule (1).",
            "source_span": {"start": child_start, "end": child_start + 30},
        },
    }
    composite_sibling_event = {
        **sibling_event,
        "event_id": "evt_composite_sibling",
        "payload": {"node_type": "rule", "label": "46/PROVISO/PROVIDEDTHAT-314A7FDD98", "content": "bad label"},
    }
    paragraph_sibling_event = {
        **sibling_event,
        "event_id": "evt_paragraph_sibling",
        "payload": {"node_type": "rule", "label": "18", "content": "paragraph amendment"},
        "evidence": {
            "excerpt": "18. In the said rules, in rule 138, in sub-rule (10), in the Table, words shall be inserted.",
            "source_span": {"start": child_start, "end": child_start + 30},
        },
        "validation": {"materializable": True},
    }
    events_path.write_text(
        "\n".join(
            json.dumps(row, sort_keys=True)
            for row in [
                sibling_event,
                child_event,
                subrule_event,
                explanation_event,
                inserted_subrule_event,
                inserted_subrule_label_from_excerpt_event,
                inserted_subrule_label_from_pdf_quote_event,
                composite_sibling_event,
                paragraph_sibling_event,
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    archive_dir = tmp_path / "sources" / "cbic" / "central-tax" / "2017" / "22-2017"
    archive_dir.mkdir(parents=True)
    (archive_dir / "extracted_text.json").write_text(json.dumps({"text": source_text}), encoding="utf-8")

    recover_context(
        events_path=events_path,
        output=output_path,
        decisions_output=decisions_path,
        report_output=report_path,
        source_archive_root=tmp_path / "sources",
        notifications_dir=tmp_path / "notifications",
    )
    rows = [json.loads(line) for line in output_path.read_text(encoding="utf-8").splitlines()]
    by_id = {row["event_id"]: row for row in rows}

    assert by_id["evt_sibling"]["target"]["component_id"] == "/in/union/rules/cgst-rules-2017/rule/44a"
    assert by_id["evt_sibling"]["payload"]["context_recovery"]["structural_target_from"] == "insert_sibling_rule_label"
    assert by_id["evt_child"]["target"]["component_id"] == "/in/union/rules/cgst-rules-2017/rule/127/clause/iv"
    assert by_id["evt_child"]["payload"]["context_recovery"]["structural_target_from"] == "insert_child_clause_label"
    assert by_id["evt_subrule"]["target"]["component_id"] == "/in/union/rules/cgst-rules-2017/rule/2/subrule/a"
    assert by_id["evt_explanation"]["target"]["component_id"] == "/in/union/rules/cgst-rules-2017/rule/44/explanation/explanation"
    assert by_id["evt_inserted_subrule_label_from_content"]["target"]["component_id"] == (
        "/in/union/rules/cgst-rules-2017/rule/92/subrule/4a"
    )
    assert by_id["evt_inserted_subrule_label_from_content"]["payload"]["label"] == "4A"
    assert by_id["evt_inserted_subrule_label_from_excerpt"]["target"]["component_id"] == (
        "/in/union/rules/cgst-rules-2017/rule/36/subrule/4"
    )
    assert by_id["evt_inserted_subrule_label_from_excerpt"]["payload"]["label"] == "4"
    assert by_id["evt_inserted_subrule_label_from_pdf_quote"]["target"]["component_id"] == (
        "/in/union/rules/cgst-rules-2017/rule/80/subrule/1a"
    )
    assert by_id["evt_inserted_subrule_label_from_pdf_quote"]["payload"]["label"] == "1A"
    assert by_id["evt_composite_sibling"]["operation"] == "INSERT_CHILD"
    assert by_id["evt_composite_sibling"]["target"]["component_id"] == (
        "/in/union/rules/cgst-rules-2017/rule/46/proviso/providedthat-314a7fdd98"
    )
    assert by_id["evt_paragraph_sibling"]["target"]["component_id"] == (
        "/in/union/rules/cgst-rules-2017/rule/138/subrule/10"
    )
    assert by_id["evt_paragraph_sibling"]["validation"]["materializable"] is False
    assert by_id["evt_paragraph_sibling"]["payload"]["context_recovery"]["requires_validation"] == (
        "context_only_insert_sibling_rule"
    )


def test_context_recovery_converts_structural_insert_sibling_child(tmp_path):
    events_path = tmp_path / "events.jsonl"
    output_path = tmp_path / "out.jsonl"
    source_text = (
        "In the said rules, in rule 46, after clause (r), the following clause shall be inserted, namely: "
        "“(s) declaration text.”"
    )
    start = source_text.index("after clause")
    event = {
        "event_id": "evt_clause_s",
        "operation": "INSERT_SIBLING",
        "status": "needs_review",
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": "/in/union/rules/cgst-rules-2017",
        },
        "payload": {"node_type": "clause", "label": "(s)", "content": "declaration text."},
        "evidence": {"excerpt": source_text, "source_span": {"start": start, "end": start + 20}},
        "source": {
            "document_id": "/in/union/notifications/cbic/central-tax/2022/14-2022",
            "record_id": "record-14",
        },
        "review": {"required": True, "review_reasons": ["target_not_resolved"]},
        "validation": {"materializable": False, "source_span_verified": True},
    }
    events_path.write_text(json.dumps(event, sort_keys=True) + "\n", encoding="utf-8")
    archive_dir = tmp_path / "sources" / "cbic" / "central-tax" / "2022" / "14-2022"
    archive_dir.mkdir(parents=True)
    (archive_dir / "extracted_text.json").write_text(json.dumps({"text": source_text}), encoding="utf-8")

    recover_context(
        events_path=events_path,
        output=output_path,
        decisions_output=tmp_path / "decisions.json",
        report_output=tmp_path / "context_recovery_report.json",
        source_archive_root=tmp_path / "sources",
        notifications_dir=tmp_path / "notifications",
    )
    recovered = json.loads(output_path.read_text(encoding="utf-8").splitlines()[0])

    assert recovered["operation"] == "INSERT_CHILD"
    assert recovered["target"]["component_id"] == "/in/union/rules/cgst-rules-2017/rule/46/clause/s"
    assert recovered["payload"]["parent_component_id"] == "/in/union/rules/cgst-rules-2017/rule/46"


def test_context_recovery_llm_candidates_are_candidate_only(tmp_path):
    events_path = tmp_path / "events.jsonl"
    output_path = tmp_path / "out.jsonl"
    source_text = "In the said rules, in rule 46, after the proviso, the following shall be inserted."
    start = source_text.index("after the proviso")
    event = {
        "event_id": "evt_llm",
        "operation": "SUBSTITUTE",
        "status": "needs_review",
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": "/in/union/rules/cgst-rules-2017",
        },
        "payload": {"old_text": "", "new_text": "", "anchor_text": ""},
        "evidence": {"excerpt": "after the proviso", "source_span": {"start": start, "end": start + 16}},
        "source": {"document_id": "/in/union/notifications/cbic/central-tax/2020/1-2020", "record_id": "r1"},
        "review": {"required": True, "review_reasons": ["incomplete_text_edit_payload"]},
        "validation": {"materializable": False},
    }
    events_path.write_text(json.dumps(event) + "\n", encoding="utf-8")
    archive_dir = tmp_path / "sources" / "cbic" / "central-tax" / "2020" / "1-2020"
    archive_dir.mkdir(parents=True)
    (archive_dir / "extracted_text.json").write_text(json.dumps({"text": source_text}), encoding="utf-8")

    recover_context(
        events_path=events_path,
        output=output_path,
        decisions_output=tmp_path / "decisions.json",
        report_output=tmp_path / "context_recovery_report.json",
        source_archive_root=tmp_path / "sources",
        notifications_dir=tmp_path / "notifications",
    )

    recovered = json.loads(output_path.read_text(encoding="utf-8").splitlines()[0])
    candidates = json.loads((tmp_path / "llm_reextraction_candidates.json").read_text(encoding="utf-8"))
    llm_report = json.loads((tmp_path / "llm_reextraction_report.json").read_text(encoding="utf-8"))

    assert recovered["target"]["component_id"] == "/in/union/rules/cgst-rules-2017/rule/46"
    assert recovered["validation"]["materializable"] is False
    assert candidates[0]["event_id"] == "evt_llm"
    assert llm_report["promotion_policy"] == "candidate_only_requires_deterministic_validation"


def test_context_recovery_deterministically_reextracts_simple_text_edit_payloads(tmp_path):
    source_text = (
        "In the Central Goods and Services Tax Rules, 2017, in rule 96, in sub-rule (2), "
        "for the words \"relevant export invoices\", the words \"relevant export invoices in respect of export of goods\" "
        "shall be substituted;"
    )
    events_path = tmp_path / "events.jsonl"
    output_path = tmp_path / "reviewed.jsonl"
    event = {
        "event_id": "evt_reextract_substitute",
        "operation": "UNKNOWN",
        "status": "needs_review",
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": "/in/union/rules/cgst-rules-2017",
        },
        "evidence": {
            "excerpt": (
                "in sub-rule (2), for the words \"relevant export invoices\", the words "
                "\"relevant export invoices in respect of export of goods\" shall be substituted;"
            ),
            "source_span": {"start": source_text.index("for the words"), "end": len(source_text)},
        },
        "source": {"document_id": "/in/union/notifications/cbic/central-tax/2017/1-2017", "record_id": "r1"},
        "review": {"required": True, "review_reasons": ["llm_candidate_not_validated", "unsupported_materializer_operation"]},
        "validation": {"materializable": False},
        "payload": {},
    }
    events_path.write_text(json.dumps(event) + "\n", encoding="utf-8")
    archive_dir = tmp_path / "sources" / "cbic" / "central-tax" / "2017" / "1-2017"
    archive_dir.mkdir(parents=True)
    (archive_dir / "extracted_text.json").write_text(json.dumps({"text": source_text}), encoding="utf-8")

    report = recover_context(
        events_path=events_path,
        output=output_path,
        decisions_output=tmp_path / "decisions.json",
        report_output=tmp_path / "context_recovery_report.json",
        source_archive_root=tmp_path / "sources",
        notifications_dir=tmp_path / "notifications",
    )

    recovered = json.loads(output_path.read_text(encoding="utf-8").splitlines()[0])
    assert report["deterministic_reextraction_count"] == 1
    assert recovered["operation"] == "SUBSTITUTE"
    assert recovered["target"]["component_id"] == "/in/union/rules/cgst-rules-2017/rule/96/subrule/2"
    assert recovered["payload"]["old_text"] == "relevant export invoices"
    assert recovered["payload"]["new_text"] == "relevant export invoices in respect of export of goods"
    assert recovered["validation"]["materializable"] is False
    assert "unsupported_materializer_operation" not in recovered["review"]["review_reasons"]


def test_context_recovery_reextracts_inserted_child_from_full_source_window(tmp_path):
    source_text = (
        "In the Central Goods and Services Tax Rules, 2017, in rule 43, "
        "after clause (h), the following clause shall be inserted, namely,- "
        "\"(i) The amount Te shall be computed separately for input tax credit of central tax, "
        "State tax, Union territory tax and integrated tax and declared in FORM GSTR-3B.\";"
    )
    events_path = tmp_path / "events.jsonl"
    output_path = tmp_path / "reviewed.jsonl"
    event = {
        "event_id": "evt_reextract_clause_insert",
        "operation": "UNKNOWN",
        "status": "needs_review",
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": "/in/union/rules/cgst-rules-2017",
        },
        "evidence": {
            "excerpt": "after clause (h), the following clause shall be inserted, namely,- \"",
            "source_span": {"start": source_text.index("after clause"), "end": source_text.index("namely") + 10},
        },
        "source": {"document_id": "/in/union/notifications/cbic/central-tax/2017/1-2017", "record_id": "r1"},
        "review": {"required": True, "review_reasons": ["llm_candidate_not_validated", "unsupported_materializer_operation"]},
        "validation": {"materializable": False},
        "payload": {},
    }
    events_path.write_text(json.dumps(event) + "\n", encoding="utf-8")
    archive_dir = tmp_path / "sources" / "cbic" / "central-tax" / "2017" / "1-2017"
    archive_dir.mkdir(parents=True)
    (archive_dir / "extracted_text.json").write_text(json.dumps({"text": source_text}), encoding="utf-8")

    report = recover_context(
        events_path=events_path,
        output=output_path,
        decisions_output=tmp_path / "decisions.json",
        report_output=tmp_path / "context_recovery_report.json",
        source_archive_root=tmp_path / "sources",
        notifications_dir=tmp_path / "notifications",
    )

    recovered = json.loads(output_path.read_text(encoding="utf-8").splitlines()[0])
    assert report["deterministic_reextraction_count"] == 1
    assert recovered["operation"] == "INSERT_CHILD"
    assert recovered["target"]["component_id"] == "/in/union/rules/cgst-rules-2017/rule/43/clause/i"
    assert recovered["payload"]["parent_component_id"] == "/in/union/rules/cgst-rules-2017/rule/43"
    assert recovered["payload"]["label"] == "i"
    assert recovered["payload"]["node_type"] == "clause"
    assert "The amount Te shall be computed separately" in recovered["payload"]["content"]
    assert recovered["validation"]["materializable"] is False
    assert "unsupported_materializer_operation" not in recovered["review"]["review_reasons"]


def test_context_recovery_does_not_reextract_next_instruction_as_child_insert(tmp_path):
    source_text = (
        "In the Central Goods and Services Tax Rules, 2017, in rule 96, in sub-rule (2), "
        "both the provisos shall be omitted. "
        "In rule 108, the following proviso shall be inserted, namely:- "
        "\"Provided that an appeal may be filed manually.\";"
    )
    events_path = tmp_path / "events.jsonl"
    output_path = tmp_path / "reviewed.jsonl"
    event = {
        "event_id": "evt_omit_before_insert",
        "operation": "UNKNOWN",
        "status": "needs_review",
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": "/in/union/rules/cgst-rules-2017",
        },
        "evidence": {
            "excerpt": "in rule 96, in sub-rule (2), both the provisos shall be omitted.",
            "source_span": {"start": source_text.index("both the provisos"), "end": source_text.index("omitted") + 7},
        },
        "source": {"document_id": "/in/union/notifications/cbic/central-tax/2017/1-2017", "record_id": "r1"},
        "review": {"required": True, "review_reasons": ["llm_candidate_not_validated", "unsupported_materializer_operation"]},
        "validation": {"materializable": False},
        "payload": {},
    }
    events_path.write_text(json.dumps(event) + "\n", encoding="utf-8")
    archive_dir = tmp_path / "sources" / "cbic" / "central-tax" / "2017" / "1-2017"
    archive_dir.mkdir(parents=True)
    (archive_dir / "extracted_text.json").write_text(json.dumps({"text": source_text}), encoding="utf-8")

    report = recover_context(
        events_path=events_path,
        output=output_path,
        decisions_output=tmp_path / "decisions.json",
        report_output=tmp_path / "context_recovery_report.json",
        source_archive_root=tmp_path / "sources",
        notifications_dir=tmp_path / "notifications",
    )

    recovered = json.loads(output_path.read_text(encoding="utf-8").splitlines()[0])
    assert report["deterministic_reextraction_count"] == 0
    assert recovered["operation"] == "UNKNOWN"
    assert recovered["target"]["component_id"] == "/in/union/rules/cgst-rules-2017/rule/96/subrule/2"


def test_context_recovery_cleans_up_stale_source_window_child_insert(tmp_path):
    source_text = (
        "In the Central Goods and Services Tax Rules, 2017, in rule 96, in sub-rule (2), "
        "both the provisos shall be omitted. "
        "In rule 108, the following proviso shall be inserted, namely:- "
        "\"Provided that an appeal may be filed manually.\";"
    )
    events_path = tmp_path / "events.jsonl"
    output_path = tmp_path / "reviewed.jsonl"
    event = {
        "event_id": "evt_stale_insert",
        "operation": "INSERT_CHILD",
        "status": "needs_review",
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": "/in/union/rules/cgst-rules-2017/rule/96/subrule/2/proviso/provided",
        },
        "evidence": {
            "excerpt": "in rule 96, in sub-rule (2), both the provisos shall be omitted.",
            "source_span": {"start": source_text.index("both the provisos"), "end": source_text.index("omitted") + 7},
        },
        "source": {"document_id": "/in/union/notifications/cbic/central-tax/2017/1-2017", "record_id": "r1"},
        "review": {"required": True, "review_reasons": ["context_recovered_target_pending_validation"]},
        "validation": {"materializable": False},
        "payload": {
            "content": "Provided that an appeal may be filed manually.",
            "context_recovered_target": True,
            "context_recovery": {"parent_component_id": "/in/union/rules/cgst-rules-2017/rule/96/subrule/2"},
            "deterministic_reextraction": {
                "strategy": "following_child_insert_from_source_window",
                "requires_materializer_validation": True,
            },
            "label": "provided",
            "node_type": "proviso",
            "parent_component_id": "/in/union/rules/cgst-rules-2017/rule/96/subrule/2",
        },
    }
    events_path.write_text(json.dumps(event) + "\n", encoding="utf-8")
    archive_dir = tmp_path / "sources" / "cbic" / "central-tax" / "2017" / "1-2017"
    archive_dir.mkdir(parents=True)
    (archive_dir / "extracted_text.json").write_text(json.dumps({"text": source_text}), encoding="utf-8")

    report = recover_context(
        events_path=events_path,
        output=output_path,
        decisions_output=tmp_path / "decisions.json",
        report_output=tmp_path / "context_recovery_report.json",
        source_archive_root=tmp_path / "sources",
        notifications_dir=tmp_path / "notifications",
    )

    recovered = json.loads(output_path.read_text(encoding="utf-8").splitlines()[0])
    assert report["stale_source_window_cleanup_count"] == 1
    assert recovered["operation"] == "UNKNOWN"
    assert recovered["target"]["component_id"] == "/in/union/rules/cgst-rules-2017/rule/96/subrule/2"
    assert "content" not in recovered["payload"]
    assert "deterministic_reextraction" not in recovered["payload"]
    assert "unsupported_materializer_operation" in recovered["review"]["review_reasons"]


def test_context_recovery_routes_rule_table_lane_before_context_recovery(tmp_path):
    source_text = (
        "In the Central Goods and Services Tax Rules, 2017, in rule 58, "
        "in clause 6, for the Table, the following Table shall be substituted, namely:- "
        "\"Rate of tax Total Out of turnover reported\";"
    )
    events_path = tmp_path / "events.jsonl"
    output_path = tmp_path / "reviewed.jsonl"
    event = {
        "event_id": "evt_rule_table",
        "operation": "UNKNOWN",
        "status": "needs_review",
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": "/in/union/rules/cgst-rules-2017/rule/58",
        },
        "evidence": {
            "excerpt": "in clause 6, for the Table, the following Table shall be substituted, namely:- \"Rate of tax\";",
            "source_span": {"start": source_text.index("in clause 6"), "end": len(source_text)},
        },
        "source": {"document_id": "/in/union/notifications/cbic/central-tax/2017/1-2017", "record_id": "r1"},
        "review": {"required": True, "review_reasons": ["llm_candidate_not_validated"]},
        "validation": {"materializable": False},
        "payload": {},
    }
    events_path.write_text(json.dumps(event) + "\n", encoding="utf-8")
    archive_dir = tmp_path / "sources" / "cbic" / "central-tax" / "2017" / "1-2017"
    archive_dir.mkdir(parents=True)
    (archive_dir / "extracted_text.json").write_text(json.dumps({"text": source_text}), encoding="utf-8")

    report = recover_context(
        events_path=events_path,
        output=output_path,
        decisions_output=tmp_path / "decisions.json",
        report_output=tmp_path / "context_recovery_report.json",
        source_archive_root=tmp_path / "sources",
        notifications_dir=tmp_path / "notifications",
    )

    recovered = json.loads(output_path.read_text(encoding="utf-8").splitlines()[0])
    assert report["rules_table_lane_count"] == 1
    assert recovered["payload"]["triage_lane"] == "rules_table_lane"
    assert recovered["validation"]["materializable"] is False


def test_context_recovery_routes_inline_table_variants_to_table_lane(tmp_path):
    source_text = (
        "In the Central Goods and Services Tax Rules, 2017, in rule 138, in sub-rule (14), "
        "in the Annexure, in column (2) of the table, after the brackets, the words “(Chapter 71)”, "
        "the words “excepting Imitation Jewellery (7117)” shall be inserted."
    )
    events_path = tmp_path / "events.jsonl"
    output_path = tmp_path / "reviewed.jsonl"
    event = {
        "event_id": "evt_rule_table_column_variant",
        "operation": "UNKNOWN",
        "status": "needs_review",
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": "/in/union/rules/cgst-rules-2017/rule/138",
        },
        "evidence": {
            "excerpt": (
                "(iv) in the Table, for the brackets, the words “(Chapter 71)” and words "
                "“excepting Imitation Jewellery (7117)” shall be inserted;"
            ),
            "source_span": {"start": source_text.index("in the Annexure"), "end": len(source_text)},
        },
        "source": {"document_id": "/in/union/notifications/cbic/central-tax/2017/1-2017", "record_id": "r2"},
        "review": {"required": True, "review_reasons": ["llm_candidate_not_validated"]},
        "validation": {"materializable": False},
        "payload": {},
    }
    events_path.write_text(json.dumps(event) + "\n", encoding="utf-8")
    archive_dir = tmp_path / "sources" / "cbic" / "central-tax" / "2017" / "1-2017"
    archive_dir.mkdir(parents=True)
    (archive_dir / "extracted_text.json").write_text(json.dumps({"text": source_text}), encoding="utf-8")

    report = recover_context(
        events_path=events_path,
        output=output_path,
        decisions_output=tmp_path / "decisions.json",
        report_output=tmp_path / "context_recovery_report.json",
        source_archive_root=tmp_path / "sources",
        notifications_dir=tmp_path / "notifications",
    )
    recovered = json.loads(output_path.read_text(encoding="utf-8").splitlines()[0])

    assert report["rules_table_lane_count"] == 1
    assert recovered["payload"]["triage_lane"] == "rules_table_lane"
    assert recovered["validation"]["materializable"] is False


def test_context_recovery_triage_classifies_form_metadata_and_malformed_rule_id():
    assert triage_event({"evidence": {"excerpt": "in FORM GSTR- 1"}, "target": {}}) == "forms_lane_pending_baseline"
    assert (
        triage_event(
            {
                "operation": "UNKNOWN",
                "evidence": {
                    "excerpt": (
                        "Table 6 to capture amendment of information rate-wise. GSTIN Invoice details "
                        "Taxable value Integrated Tax Central Tax State/UT Tax"
                    )
                },
                "target": {},
            }
        )
        == "forms_lane_pending_baseline"
    )
    assert (
        triage_event(
            {
                "operation": "UNKNOWN",
                "evidence": {
                    "excerpt": (
                        "(a) in Table 7, in clause (g), for the words \"Recipient of deemed export\", "
                        "the words \"Recipient of deemed export supplies/ Supplier of deemed export supplies\" "
                        "shall be substituted;"
                    )
                },
                "target": {"component_id": "/in/union/rules/cgst-rules-2017/rule/89"},
            }
        )
        == "rules_table_lane"
    )
    assert (
        triage_event(
            {
                "operation": "UNKNOWN",
                "evidence": {
                    "excerpt": (
                        "in the said rules, in rule 138, in sub-rule (14), in column (2) of the table, "
                        "against S.No. 5, after the brackets, word and figures “(Chapter 71)” "
                        "the words, brackets and figures “excepting Imitation Jewellery (7117)” shall be inserted."
                    )
                },
                "target": {"component_id": "/in/union/rules/cgst-rules-2017/rule/138"},
            }
        )
        == "rules_table_lane"
    )
    assert (
        triage_event(
            {
                "operation": "UNKNOWN",
                "evidence": {
                    "excerpt": "(a) in clause 14, in sub-clause (a), in the Table, for the brackets, figures and words “x”, the following words “y” shall be substituted"
                },
                "target": {"component_id": "/in/union/rules/cgst-rules-2017/rule/89"},
            }
        )
        == "rules_table_lane"
    )
    assert (
        triage_event(
            {
                "operation": "UNKNOWN",
                "evidence": {
                    "excerpt": (
                        "(i) under the heading Instructions, in paragraph 7, for the letters, words "
                        "and figures \"GSTR-1\", the letters, words and figures \"(GSTR-1 or GSTR-1A)\" "
                        "shall be substituted;"
                    )
                },
                "target": {"component_id": "/in/union/rules/cgst-rules-2017/rule/37a"},
            }
        )
        == "forms_lane_pending_baseline"
    )
    assert (
        triage_event(
            {
                "operation": "UNKNOWN",
                "evidence": {"excerpt": "4. Demand table at serial no. 7 shall not be filled up if an order issued under section 129 is being withdrawn."},
                "target": {"component_id": "/in/union/rules/cgst-rules-2017/rule/142"},
            }
        )
        == "forms_lane_pending_baseline"
    )
    assert (
        triage_event(
            {
                "operation": "UNKNOWN",
                "evidence": {
                    "excerpt": "3. It has come to my notice that the above said order requires rectification (Reason for rectification as per attached annexure)"
                },
                "target": {"component_id": "/in/union/rules/cgst-rules-2017/rule/100"},
            }
        )
        == "forms_lane_pending_baseline"
    )
    assert (
        triage_event(
            {
                "operation": "UNKNOWN",
                "evidence": {
                    "excerpt": (
                        "3. It may be noted that where any amount remains unpaid within a period of seven days "
                        "the said amount shall be recoverable in accordance with the provisions of section 79 of the Act."
                    )
                },
                "target": {"component_id": "/in/union/rules/cgst-rules-2017/rule/109c"},
            }
        )
        == "forms_lane_pending_baseline"
    )
    assert (
        triage_event(
            {
                "operation": "UNKNOWN",
                "evidence": {
                    "excerpt": "1. In columns 1 to 6 of Table 2, the details of the order against which the application under section 128A is filed needs to be filled in by the applicant."
                },
                "target": {"component_id": "/in/union/rules/cgst-rules-2017/rule/164"},
            }
        )
        == "forms_lane_pending_baseline"
    )
    assert (
        triage_event(
            {
                "operation": "UNKNOWN",
                "evidence": {
                    "excerpt": (
                        "7. Reference number of appeal filed originally but subsequently withdrawn. "
                        "Subject: Undertaking submitted in respect of Rule 164(15)(b)(ii). "
                        "I hereby undertake not to file an appeal against the order."
                    )
                },
                "target": {"component_id": "/in/union/rules/cgst-rules-2017/rule/164"},
            }
        )
        == "forms_lane_pending_baseline"
    )
    assert (
        triage_event(
            {
                "operation": "UNKNOWN",
                "evidence": {
                    "excerpt": (
                        "(a) in clause 6, for the Table, the following Table shall be substituted, "
                        "namely:- \"Rate of tax Total Out of turnover reported Composition tax amount\";"
                    )
                },
                "target": {"component_id": "/in/union/rules/cgst-rules-2017/rule/58"},
            }
        )
        == "rules_table_lane"
    )
    assert (
        triage_event(
            {
                "operation": "INSERT_SIBLING",
                "evidence": {
                    "excerpt": (
                        "after rule 138, the following shall be inserted, namely:- "
                        "Chapter - XVII Inspection, Search and Seizure"
                    )
                },
                "target": {"component_id": "/in/union/rules/cgst-rules-2017/rule/138"},
            }
        )
        == "baseline_source_only"
    )
    assert (
        triage_event(
            {
                "operation": "UNKNOWN",
                "evidence": {
                    "excerpt": (
                        "(b) a financial year in any other case.”; "
                        "(5) In rule 59 of the said rules, in sub-rule (6), "
                        "with effect from the 1st day of January, 2022, -"
                    )
                },
                "target": {"component_id": "/in/union/rules/cgst-rules-2017/rule/59/subrule/6"},
            }
        )
        == "baseline_source_only"
    )
    assert (
        triage_event(
            {
                "operation": "UNKNOWN",
                "evidence": {
                    "excerpt": (
                        "(3A) The Commissioner shall determine the compounding amount under sub-rule (3) "
                        "as per the Table below:- TABLE S.No. Offence Compounding amount"
                    )
                },
                "target": {"component_id": "/in/union/rules/cgst-rules-2017/rule/162"},
            }
        )
        == "rules_table_lane"
    )
    assert (
        triage_event(
            {
                "operation": "UNKNOWN",
                "evidence": {
                    "excerpt": (
                        "Offence specified in clause (i) of sub-section (1) of section 132 of the Act "
                        "Attempt to commit the offences or abets the commission of offences"
                    )
                },
                "target": {"component_id": "/in/union/rules/cgst-rules-2017/rule/162"},
            }
        )
        == "rules_table_lane"
    )
    assert (
        triage_event(
            {
                "operation": "UNKNOWN",
                "evidence": {
                    "excerpt": (
                        "4. For every 20 km. or part thereof thereafter One additional day in case of "
                        "Over Dimensional Cargo: Provided that the Commissioner may extend the validity "
                        "period of an e-way bill."
                    )
                },
                "target": {"component_id": "/in/union/rules/cgst-rules-2017/rule/138b"},
            }
        )
        == "rules_table_lane"
    )
    assert (
        triage_event(
            {
                "operation": "UNKNOWN",
                "evidence": {
                    "excerpt": (
                        "5. Amount of tax credit carried forward in the return filed under existing laws: "
                        "(a) Amount of Cenvat credit carried forward to electronic credit ledger as central tax "
                        "Sl. no. Registration no. Tax period to which the Date"
                    )
                },
                "target": {"component_id": "/in/union/rules/cgst-rules-2017/rule/99"},
            }
        )
        == "forms_lane_pending_baseline"
    )
    assert (
        triage_event(
            {
                "operation": "UNKNOWN",
                "evidence": {
                    "excerpt": (
                        "3. Registered persons having aggregate turnover more Quarterly return than 1.5 crore rupees "
                        "and up to 5 crore rupees in the preceding financial year"
                    )
                },
                "target": {"component_id": "/in/union/rules/cgst-rules-2017/rule/61a"},
            }
        )
        == "forms_lane_pending_baseline"
    )
    assert (
        triage_event(
            {
                "operation": "UNKNOWN",
                "evidence": {
                    "excerpt": (
                        "3. I also undertake that on issue of an order concluding demand proceedings issued under section 128A, "
                        "no writ shall be filed against the order mentioned in Table 2 of this form."
                    )
                },
                "target": {"component_id": "/in/union/rules/cgst-rules-2017/rule/164"},
            }
        )
        == "forms_lane_pending_baseline"
    )
    assert (
        triage_event(
            {
                "operation": "UNKNOWN",
                "evidence": {
                    "excerpt": (
                        "8. Whether any particular thing done by the applicant Mention section results in supply "
                        "of goods or services or both and rule and Schedule specified in Appellate/ Revisionary order"
                    )
                },
                "target": {"component_id": "/in/union/rules/cgst-rules-2017/rule/110"},
            }
        )
        == "forms_lane_pending_baseline"
    )
    assert (
        triage_event(
            {
                "operation": "UNKNOWN",
                "evidence": {
                    "excerpt": "7. Table 4.1 will not include zero rated supplies made without payment of taxes."
                },
                "target": {"component_id": "/in/union/rules/cgst-rules-2017/rule/42"},
            }
        )
        == "forms_lane_pending_baseline"
    )
    assert (
        triage_event(
            {
                "operation": "UNKNOWN",
                "evidence": {"excerpt": "7. Old Registration No. << Auto, editable>>"},
                "target": {"component_id": "/in/union/rules/cgst-rules-2017/rule/142a"},
            }
        )
        == "forms_lane_pending_baseline"
    )
    assert (
        triage_event(
            {
                "operation": "UNKNOWN",
                "evidence": {
                    "excerpt": (
                        "9. Table 9 covers the Amendments in respect of B2C outward supplies "
                        "where invoice value is more than Rs 250000/-."
                    )
                },
                "target": {"component_id": "/in/union/rules/cgst-rules-2017/rule/62"},
            }
        )
        == "forms_lane_pending_baseline"
    )
    assert (
        triage_event(
            {
                "operation": "UNKNOWN",
                "evidence": {"excerpt": "7. Table 7 captures information on a gross value level."},
                "target": {},
            }
        )
        == "forms_lane_pending_baseline"
    )
    assert (
        triage_event(
            {
                "operation": "UNKNOWN",
                "evidence": {
                    "excerpt": (
                        "4. Table 3 consists of details of import of goods, bill of entry wise and taxpayer "
                        "has to specify the amount of ITC eligible on such import of goods."
                    )
                },
                "target": {"component_id": "/in/union/rules/cgst-rules-2017/rule/61"},
            }
        )
        == "forms_lane_pending_baseline"
    )
    assert (
        triage_event(
            {
                "operation": "EXTRACT",
                "evidence": {
                    "excerpt": (
                        "4. Total ITC/Eligible ITC/Ineligible ITC to be distributed for tax period "
                        "(From Table No. 3) Description Integrated Central State / UT CESS tax"
                    )
                },
                "target": {"component_id": "/in/union/rules/cgst-rules-2017/rule/62"},
            }
        )
        == "forms_lane_pending_baseline"
    )
    assert (
        triage_event(
            {
                "operation": "UNKNOWN",
                "evidence": {
                    "excerpt": (
                        "2. Columns (2) & (3) in Table (A) and Table (B) are mandatory in cases "
                        "where fresh challan are required to be issued by the job worker."
                    )
                },
                "target": {"component_id": "/in/union/rules/cgst-rules-2017/rule/45"},
            }
        )
        == "forms_lane_pending_baseline"
    )
    assert (
        triage_event(
            {
                "operation": "UNKNOWN",
                "evidence": {"excerpt": "2. Column nos. 2, 3, 4 and 5 of the above Table i.e. tax rate are not mandatory."},
                "target": {},
            }
        )
        == "forms_lane_pending_baseline"
    )
    assert (
        triage_event(
            {
                "operation": "UNKNOWN",
                "evidence": {"excerpt": "3. Summary of self-assessed liability (net of advances) (Amount in ₹ in all tables)."},
                "target": {},
            }
        )
        == "forms_lane_pending_baseline"
    )
    assert (
        triage_event(
            {
                "operation": "UNKNOWN",
                "evidence": {
                    "excerpt": (
                        "Details relating to import of goods from overseas on bill of entry. "
                        "Table 5 Rate of tax Integrated Tax Taxable value."
                    )
                },
                "target": {},
            }
        )
        == "forms_lane_pending_baseline"
    )
    assert (
        triage_event(
            {
                "operation": "UNKNOWN",
                "evidence": {
                    "excerpt": "5. Declaration - I hereby declare that bank guarantee has been furnished for the case under dispute."
                },
                "target": {},
            }
        )
        == "forms_lane_pending_baseline"
    )
    assert (
        triage_event(
            {
                "operation": "SUBSTITUTE",
                "evidence": {
                    "excerpt": (
                        "47. Time limit for issuing tax invoice.- The invoice referred to in rule 46 "
                        "shall be issued within thirty days."
                    )
                },
                "target": {},
            }
        )
        == "amendment_language"
    )
    assert (
        triage_event(
            {
                "operation": "UNKNOWN",
                "evidence": {
                    "excerpt": (
                        "47. Time limit for issuing tax invoice.- The invoice referred to in rule 46 "
                        "shall be issued within thirty days."
                    )
                },
                "target": {},
            }
        )
        == "baseline_source_only"
    )
    assert (
        triage_event({"operation": "UNKNOWN", "evidence": {"excerpt": "(ii) in rule 96,"}, "target": {}})
        == "baseline_source_only"
    )
    assert (
        triage_event({"operation": "UNKNOWN", "evidence": {"excerpt": "(g) in rule 24,-"}, "target": {}})
        == "baseline_source_only"
    )
    assert (
        triage_event(
            {
                "operation": "UNKNOWN",
                "evidence": {"excerpt": "(g) in rule 24,-"},
                "target": {"component_id": "/in/union/rules/cgst-rules-2017/rule/24"},
            }
        )
        == "baseline_source_only"
    )
    assert (
        triage_event(
            {
                "operation": "UNKNOWN",
                "evidence": {"excerpt": "(a) in rule 21, for clause (b), the following clauses shall be substituted"},
                "target": {},
            }
        )
        == "amendment_language"
    )
    assert (
        triage_event(
            {
                "operation": "UNKNOWN",
                "evidence": {"excerpt": "(v) with effect from 23rd October, 2017, in rule 96 –"},
                "target": {"component_id": "/in/union/rules/cgst-rules-2017/rule/96"},
            }
        )
        == "baseline_source_only"
    )
    assert (
        triage_event(
            {
                "operation": "UNKNOWN",
                "evidence": {"excerpt": "(iv) in rule 142, with effect from the 1st day of January, 2022,–"},
                "target": {"component_id": "/in/union/rules/cgst-rules-2017/rule/142"},
            }
        )
        == "baseline_source_only"
    )
    assert (
        triage_event(
            {
                "operation": "UNKNOWN",
                "evidence": {"excerpt": "(vi) in rule 124, -"},
                "target": {"component_id": "/in/union/rules/cgst-rules-2017/rule/124"},
            }
        )
        == "baseline_source_only"
    )
    assert (
        triage_event(
            {
                "operation": "UNKNOWN",
                "evidence": {"excerpt": "(b) of paragraph 6 of Schedule II"},
                "target": {"component_id": "/in/union/rules/cgst-rules-2017/rule/26"},
            }
        )
        == "baseline_source_only"
    )
    assert (
        triage_event({"evidence": {"excerpt": "short title"}, "payload": {}, "evidence": {"source_span": {"start": 1, "end": 1}, "excerpt": "short title"}})
        == "metadata_only"
    )
    assert triage_event({"target": {"component_id": "CGST_Rule_88"}}) == "canonical_id_normalized"
    assert canonicalize_legacy_reference("FORM_GST_REG_01") == "/in/union/forms/gst-reg-01"
    assert canonicalize_legacy_reference("CGST_Act_2017/Section_25") == (
        "/in/union/acts/cgst-act-2017/section/25"
    )


def test_expected_corpus_relative_paths():
    assert expected_corpus_relative_path("/in/union/rules/cgst-rules-2017/rule/10", "rule") == Path(
        "in/union/rules/cgst-rules-2017/rule-010.xml"
    )
    assert expected_corpus_relative_path("/in/union/rules/cgst-rules-2017/rule/9a", "rule") == Path(
        "in/union/rules/cgst-rules-2017/rule-09a.xml"
    )
    assert expected_corpus_relative_path("/in/union/forms/gst-reg-01", "form") == Path(
        "in/union/forms/gst-reg-01/form.xml"
    )
    assert expected_corpus_relative_path("/in/union/notifications/cbic/central-tax/2025/18-2025", "notification") == Path(
        "in/union/notifications/cbic/central-tax/2025/18-2025.xml"
    )


def test_source_archive_extracts_text_and_metadata(tmp_path):
    source = tmp_path / "notification.txt"
    source.write_text("NOTIFICATION\n\nBody text", encoding="utf-8")

    archive_dir = tmp_path / "sources/cbic/test"
    archive_source(
        source,
        archive_dir,
        {
            "canonical_id": "/in/union/notifications/cbic/test",
            "document_type": "notification",
        },
    )
    extracted = extract_source_text(archive_dir)
    metadata = read_metadata_yaml(archive_dir / "metadata.yaml")
    structure = parse_structure_deterministic(extracted)
    archive_dir.joinpath("structure.json").write_text(json.dumps(structure, indent=2), encoding="utf-8")

    assert extracted["text"] == "NOTIFICATION\n\nBody text"
    assert extracted["pages"][0]["start"] == 0
    assert metadata["source_file"] == "source.txt"
    assert len(metadata["source_sha256"]) == 64

    validation = validate_source_archive(archive_dir)
    assert validation.ok, validation.errors

    metadata_path = archive_dir / "metadata.yaml"
    metadata_path.write_text(
        metadata_path.read_text(encoding="utf-8").replace(metadata["source_sha256"], "0" * 64),
        encoding="utf-8",
    )
    validation = validate_source_archive(archive_dir)
    assert not validation.ok
    assert any("metadata source_sha256 does not match source file" in error for error in validation.errors)


def test_source_archive_extracts_pdf_with_tight_text_tolerance(tmp_path, monkeypatch):
    calls = []

    class FakePage:
        def extract_text(self, **kwargs):
            calls.append(kwargs)
            return "To be published in the Gazette of India"

    class FakePdf:
        pages = [FakePage()]

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class FakePdfPlumber:
        @staticmethod
        def open(path):
            return FakePdf()

    monkeypatch.setattr(source_archive, "pdfplumber", FakePdfPlumber, raising=False)
    monkeypatch.setitem(__import__("sys").modules, "pdfplumber", FakePdfPlumber)

    source = tmp_path / "notification.pdf"
    source.write_bytes(b"%PDF test fixture")
    archive_dir = tmp_path / "sources/cbic/test"
    archive_source(
        source,
        archive_dir,
        {
            "canonical_id": "/in/union/notifications/cbic/test",
            "document_type": "notification",
        },
    )

    extracted = extract_source_text(archive_dir)

    assert extracted["text"] == "To be published in the Gazette of India"
    assert calls == [source_archive.PDF_EXTRACT_TEXT_OPTIONS]


def test_source_archive_extracts_visible_text_from_html(tmp_path):
    source = tmp_path / "cgst-act.html"
    source.write_text(
        """
        <html>
          <head><style>.hidden { display:none; }</style><script>alert("x")</script></head>
          <body>
            <h1>The Central Goods and Services Tax Act, 2017</h1>
            <p>1. Short title, extent and commencement.</p>
            <table><tr><td>Section 2.</td><td>Definitions.</td></tr></table>
          </body>
        </html>
        """,
        encoding="utf-8",
    )
    archive_dir = tmp_path / "sources/in/union/acts/cgst-act-2017"
    archive_source(
        source,
        archive_dir,
        {
            "canonical_id": "/in/union/acts/cgst-act-2017",
            "document_type": "act",
        },
    )

    extracted = extract_source_text(archive_dir)

    assert "The Central Goods and Services Tax Act, 2017" in extracted["text"]
    assert "1. Short title, extent and commencement." in extracted["text"]
    assert "Section 2." in extracted["text"]
    assert "<h1>" not in extracted["text"]
    assert "alert" not in extracted["text"]


def test_source_archive_detects_extracted_text_drift(tmp_path):
    source = tmp_path / "notification.txt"
    source.write_text("NOTIFICATION\n\nBody text", encoding="utf-8")
    archive_dir = tmp_path / "sources/cbic/test"
    archive_source(
        source,
        archive_dir,
        {
            "canonical_id": "/in/union/notifications/cbic/test",
            "document_type": "notification",
        },
    )
    extracted = extract_source_text(archive_dir)
    structure = parse_structure_deterministic(extracted)
    archive_dir.joinpath("structure.json").write_text(json.dumps(structure, indent=2), encoding="utf-8")

    extracted["pages"][0]["text"] = "TAMPERED"
    archive_dir.joinpath("extracted_text.json").write_text(json.dumps(extracted, indent=2), encoding="utf-8")

    validation = validate_source_archive(archive_dir)

    assert not validation.ok
    assert any("extracted text does not match page text round-trip" in error for error in validation.errors)
    assert any("page 1: span does not match extracted text" in error for error in validation.errors)


def test_structure_parser_emits_typed_spans_and_references():
    text = (ROOT / "data/notifications/18_2025_CT.txt").read_text(encoding="utf-8")
    extracted = {"text": text, "source_sha256": "seed"}

    structure = parse_structure_deterministic(extracted)
    errors, warnings = validate_structure_spans(extracted, structure)

    assert not errors
    assert not warnings
    assert structure["parser"] == "deterministic-india-profile-v1"
    assert [node["type"] for node in structure["nodes"][:4]] == [
        "notification_title",
        "publication_date",
        "preamble",
        "commencement",
    ]
    assert sum(1 for node in structure["nodes"] if node["type"] == "amendment") == 4
    targets = {reference["target"] for reference in structure["references"]}
    assert "/in/union/rules/cgst-rules-2017/rule/10" in targets
    assert "/in/union/forms/gst-reg-01" in targets

    tampered = json.loads(json.dumps(structure))
    tampered["nodes"][0]["text_hash"] = "bad"
    errors, _warnings = validate_structure_spans(extracted, tampered)
    assert "node 1: text_hash does not match extracted text" in errors

    low_confidence = json.loads(json.dumps(structure))
    low_confidence["nodes"][0]["confidence"] = 0.4
    errors, warnings = validate_structure_spans(extracted, low_confidence)
    assert not errors
    assert "node 1: low confidence 0.4" in warnings


def test_structure_parser_ignores_gazette_header_section_reference():
    text = (
        "[To be published in the Gazette of India, Extraordinary, Part II, Section 3, Sub-section (i)]\n"
        "[TO BE PUBLISHED IN THE GAZETTE OF INDIA, EXTRAORDINARY, PART II, SECTION 3,\n"
        "SUB- SECTION (ii)]\n"
        "The principal rules were published in the Gazette of India, Extraordinary, Part II, Section 3, Sub-\n"
        "section (i), vide notification No. 3/2017-Central Tax.\n"
        "G.S.R... (E). In exercise of the powers conferred by section 164 of the Central Goods and Services Tax Act."
    )
    structure = parse_structure_deterministic({"text": text, "source_sha256": "seed"})

    targets = [reference["target"] for reference in structure["references"]]

    assert "/in/union/acts/cgst-act-2017/section/3" not in targets
    assert "/in/union/acts/cgst-act-2017/section/164" in targets


def test_structure_parser_disambiguates_gst_act_section_references():
    text = (
        "In exercise of the powers conferred by section 3 read with section 5 of the Central Goods and "
        "Services Tax Act, 2017 and section 3 of the Integrated Goods and Services Tax Act, 2017."
    )
    structure = parse_structure_deterministic({"text": text, "source_sha256": "seed"})

    targets = [reference["target"] for reference in structure["references"]]

    assert targets.count("/in/union/acts/cgst-act-2017/section/3") == 1
    assert "/in/union/acts/cgst-act-2017/section/5" in targets
    assert "/in/union/acts/igst-act-2017/section/3" in targets


def test_structure_parser_resolves_non_gst_act_section_references():
    extracted = {
        "text": (
            "4. In section 9 of the Income-tax Act, in sub-section (1), with effect from the "
            "1st April, 2026, the following shall be substituted."
        )
    }

    structure = parse_structure_deterministic(extracted, document_type="act")
    targets = [reference["target"] for reference in structure["references"]]

    assert "/in/union/acts/income-tax-act-1961/section/9" in targets
    assert "/in/union/acts/cgst-act-2017/section/9" not in targets


def test_structure_parser_uses_corpus_canonical_the_act_targets():
    text = (
        "The amendment refers to section 11 of the Customs Act, 1962, section 5A of the "
        "Central Excise Act, 1944, and section 9A of the Customs Tariff Act, 1975."
    )
    structure = parse_structure_deterministic({"text": text, "source_sha256": "seed"}, document_type="notification")
    targets = [reference["target"] for reference in structure["references"]]

    assert "/in/union/acts/the-customs-act-1962/section/11" in targets
    assert "/in/union/acts/the-central-excise-act-1944/section/5a" in targets
    assert "/in/union/acts/the-customs-tariff-act-1975/section/9a" in targets


def test_structure_parser_detects_compact_rule_headings_and_merges_body():
    text = (
        "23. Revocation of cancellation of registration.-(1) Body for rule 23.\n"
        "Continuation for rule 23.\n"
        "24.Migration of persons registered under the existing law.-(1) Body for rule 24.\n"
        "26.Method of authentication.- (1) Body for rule 26.\n"
        "65. Form and manner of submission of return by an Input Service Distributor.-Every ISD shall submit."
    )

    structure = parse_structure_deterministic({"text": text, "source_sha256": "seed"}, document_type="rules")
    rules = [node for node in structure["nodes"] if node["type"] == "rule"]

    assert [node["label"] for node in rules] == ["23", "24", "26", "65"]
    assert "Continuation for rule 23" in text[rules[0]["start"] : rules[0]["end"]]


def test_structure_parser_merges_act_section_body_blocks():
    text = (
        "44. Annual return.\n"
        "(1) Every registered person shall furnish an annual return.\n"
        "Provided that the Commissioner may exempt a class of persons.\n"
        "45. Final return.\n"
        "Every registered person whose registration has been cancelled shall furnish a final return."
    )

    structure = parse_structure_deterministic({"text": text, "source_sha256": "seed"}, document_type="act")
    sections = [node for node in structure["nodes"] if node["type"] == "section"]

    assert [node["label"] for node in sections] == ["44", "45"]
    assert "Provided that the Commissioner" in text[sections[0]["start"] : sections[0]["end"]]


def test_act_ingest_renders_section_level_provisions(tmp_path):
    source = tmp_path / "cgst-act.txt"
    source.write_text(
        "1. Short title, extent and commencement.\n"
        "This Act may be called the Central Goods and Services Tax Act, 2017.\n\n"
        "25. Procedure for registration.\n"
        "Every person who is liable to be registered under section 22 shall apply for registration.",
        encoding="utf-8",
    )
    source_dir = tmp_path / "sources/in/union/acts/cgst-act-2017"
    output_xml = tmp_path / "corpus/in/union/acts/cgst-act-2017/act.xml"

    report = ingest_source_file(
        source,
        source_dir,
        output_xml,
        metadata={
            "canonical_id": "/in/union/acts/cgst-act-2017",
            "document_type": "act",
            "title": "Central Goods and Services Tax Act, 2017",
            "jurisdiction": "IN-UNION",
            "language": "eng",
            "publication_date": "2017-04-12",
            "effective_from": "2017-07-01",
            "issuing_authority": "/in/authority/parliament-of-india",
            "source_url": str(source),
        },
    )

    assert report["nodes"] == 2
    tree = ET.parse(output_xml)
    sections = tree.findall(".//section")
    assert [section.attrib["refersTo"] for section in sections] == [
        "/in/union/acts/cgst-act-2017/section/1",
        "/in/union/acts/cgst-act-2017/section/25",
    ]
    assert sections[0].attrib["sourceNodeType"] == "section"
    entry = lookup_canonical_id(tmp_path / "corpus", "/in/union/acts/cgst-act-2017/section/25")
    assert entry is not None
    assert entry["roles"] == ["provision"]
    assert "Procedure for registration" in entry["provision"]["text"]


def test_unresolved_reference_report_groups_targets(tmp_path):
    corpus_dir = tmp_path / "corpus"
    source = tmp_path / "notification.txt"
    source.write_text("Notification under section 39 and rule 61.", encoding="utf-8")
    output_xml = corpus_dir / "in/union/notifications/cbic/central-tax/2026/1-2026.xml"
    ingest_source_file(
        source,
        tmp_path / "sources/cbic/central-tax/2026/1-2026",
        output_xml,
        metadata={
            "canonical_id": "/in/union/notifications/cbic/central-tax/2026/1-2026",
            "document_type": "notification",
            "title": "Notification No. 1/2026",
            "jurisdiction": "IN-UNION",
            "language": "eng",
            "publication_date": "2026-01-01",
            "effective_from": "2026-01-01",
            "issuing_authority": "/in/authority/cbic",
            "source_url": str(source),
        },
    )

    report = build_unresolved_reference_report(corpus_dir)
    output = write_unresolved_reference_report(report, tmp_path / "unresolved.json")
    targets = {item["target"]: item for item in report["targets"]}

    assert output.exists()
    assert report["stats"]["documents"] == 1
    assert report["stats"]["unresolved_targets"] == 2
    assert targets["/in/union/acts/cgst-act-2017/section/39"]["kind"] == "act_section"
    assert targets["/in/union/rules/cgst-rules-2017/rule/61"]["kind"] == "rule"
    assert targets["/in/union/acts/cgst-act-2017/section/39"]["source_documents"] == 1


def test_reference_resolver_maps_high_confidence_act_and_form_aliases(tmp_path):
    corpus_dir = tmp_path / "corpus"
    act_path = corpus_dir / "in/union/acts/the-customs-act-1962/act.xml"
    form_path = corpus_dir / "in/union/forms/gst-pct-01/form.xml"
    act_path.parent.mkdir(parents=True)
    form_path.parent.mkdir(parents=True)
    act_path.write_text(
        """
<akomaNtoso><act><meta><proprietary>
<property name="canonical_id" value="/in/union/acts/the-customs-act-1962"/>
</proprietary></meta><body>
<section refersTo="/in/union/acts/the-customs-act-1962/section/11"><num>11</num><content><p>Power.</p></content></section>
</body></act></akomaNtoso>
""".strip(),
        encoding="utf-8",
    )
    form_path.write_text(
        """
<akomaNtoso><doc><meta><proprietary>
<property name="canonical_id" value="/in/union/forms/gst-pct-01"/>
</proprietary></meta><mainBody><paragraph><content><p>Form.</p></content></paragraph></mainBody></doc></akomaNtoso>
""".strip(),
        encoding="utf-8",
    )

    resolver = build_reference_resolver(corpus_dir)

    assert resolver.resolve("/in/union/acts/customs-act-1962/section/11") == (
        "/in/union/acts/the-customs-act-1962/section/11",
        "resolved_by_alias",
        "apply_alias",
    )
    assert resolver.resolve("/in/union/forms/gst-pct-1") == (
        "/in/union/forms/gst-pct-01",
        "resolved_by_alias",
        "apply_alias",
    )
    assert resolver.resolve("/in/union/acts/customs-act-1962/section/65a") == (
        "/in/union/acts/the-customs-act-1962/section/65a",
        "alias_document_exists_child_missing",
        "refresh_source_or_parser",
    )


def test_graph_builder_normalizes_alias_targets_and_preserves_original(tmp_path):
    corpus_dir = tmp_path / "corpus"
    act_path = corpus_dir / "in/union/acts/the-customs-act-1962/act.xml"
    notification_path = corpus_dir / "in/union/notifications/cbic/customs/2026/1-2026.xml"
    act_path.parent.mkdir(parents=True)
    notification_path.parent.mkdir(parents=True)
    act_path.write_text(
        """
<akomaNtoso><act><meta><proprietary>
<property name="canonical_id" value="/in/union/acts/the-customs-act-1962"/>
<property name="document_type" value="act"/>
</proprietary></meta><body>
<section refersTo="/in/union/acts/the-customs-act-1962/section/11"><num>11</num><content><p>Power.</p></content></section>
</body></act></akomaNtoso>
""".strip(),
        encoding="utf-8",
    )
    notification_path.write_text(
        """
<akomaNtoso><doc><meta><proprietary>
<property name="canonical_id" value="/in/union/notifications/cbic/customs/2026/1-2026"/>
<property name="document_type" value="notification"/>
</proprietary></meta><mainBody><paragraph>
<content><p>See section 11.</p></content>
<references><ref eId="r1" href="/in/union/acts/customs-act-1962/section/11" showAs="section 11" type="REFERS_TO"/></references>
</paragraph></mainBody></doc></akomaNtoso>
""".strip(),
        encoding="utf-8",
    )

    graph = build_graph_index(corpus_dir)
    edge = next(edge for edge in graph["edges"] if edge.get("eId") == "r1")

    assert edge["target"] == "/in/union/acts/the-customs-act-1962/section/11"
    assert edge["originalTarget"] == "/in/union/acts/customs-act-1962/section/11"
    assert edge["showAs"] == "section 11"


def test_unresolved_reference_report_classifies_and_writes_summary(tmp_path):
    corpus_dir = tmp_path / "corpus"
    act_path = corpus_dir / "in/union/acts/the-customs-act-1962/act.xml"
    notification_path = corpus_dir / "in/union/notifications/cbic/customs/2026/1-2026.xml"
    act_path.parent.mkdir(parents=True)
    notification_path.parent.mkdir(parents=True)
    act_path.write_text(
        """
<akomaNtoso><act><meta><proprietary>
<property name="canonical_id" value="/in/union/acts/the-customs-act-1962"/>
<property name="document_type" value="act"/>
</proprietary></meta><body>
<section refersTo="/in/union/acts/the-customs-act-1962/section/11"><num>11</num><content><p>Power.</p></content></section>
</body></act></akomaNtoso>
""".strip(),
        encoding="utf-8",
    )
    notification_path.write_text(
        """
<akomaNtoso><doc><meta><proprietary>
<property name="canonical_id" value="/in/union/notifications/cbic/customs/2026/1-2026"/>
<property name="document_type" value="notification"/>
</proprietary></meta><mainBody><paragraph>
<content><p>Notification under section 11 and FORM GST SPL-02.</p></content>
<references>
<ref eId="r1" href="/in/union/acts/customs-act-1962/section/11" showAs="section 11" type="REFERS_TO"/>
<ref eId="r2" href="/in/union/forms/gst-spl-02" showAs="FORM GST SPL-02" type="REFERS_TO"/>
</references>
</paragraph></mainBody></doc></akomaNtoso>
""".strip(),
        encoding="utf-8",
    )

    report = build_unresolved_reference_report(corpus_dir)
    summary = write_unresolved_reference_summary(report, tmp_path / "summary.md")
    targets = {item["target"]: item for item in report["targets"]}

    assert report["stats"]["alias_resolved_occurrences"] == 1
    assert "/in/union/acts/the-customs-act-1962/section/11" not in targets
    assert targets["/in/union/forms/gst-spl-02"]["classification"] == "form_missing"
    assert targets["/in/union/forms/gst-spl-02"]["suggested_action"] == "ingest_missing_form"
    assert "Unresolved Reference Triage" in summary.read_text(encoding="utf-8")


def test_quality_report_flags_long_paragraphs_and_joined_tokens(tmp_path):
    corpus_dir = tmp_path / "corpus"
    xml_path = corpus_dir / "in/union/notifications/cbic/central-tax/2025/1-2025.xml"
    xml_path.parent.mkdir(parents=True)
    text = "Government ofIndia " + ("x" * 2200)
    write_xml(
        render_source_document(
            text,
            {
                "canonical_id": "/in/union/notifications/cbic/central-tax/2025/1-2025",
                "document_type": "notification",
                "title": "Notification 1/2025",
                "jurisdiction": "IN-UNION",
                "language": "eng",
                "source_type": "archived-source",
                "source_url": "official",
                "source_sha256": "0" * 64,
                "publication_date": "2025-01-01",
                "effective_from": "2025-01-01",
                "issuing_authority": "/in/authority/cbic",
                "review_status": "raw",
                "parser_version": "test",
            },
            {
                "document_type": "notification",
                "nodes": [
                    {
                        "type": "paragraph",
                        "label": "1",
                        "start": 0,
                        "end": len(text),
                        "text_hash": "0" * 64,
                        "confidence": 1.0,
                    }
                ],
                "references": [],
            },
        ),
        xml_path,
    )

    report = audit_corpus_quality(corpus_dir, max_paragraph_chars=2000)
    output = write_quality_report(report, tmp_path / "quality.json")

    assert output.exists()
    assert report["profile"] == "git-for-law-corpus-quality-v1"
    assert report["stats"]["documents"] == 1
    assert report["stats"]["long_paragraphs"] == 1
    assert report["stats"]["joined_token_hits"] >= 1
    assert report["flagged_documents"][0]["canonical_id"] == "/in/union/notifications/cbic/central-tax/2025/1-2025"


def test_quality_report_ignores_common_camel_case_names(tmp_path):
    corpus_dir = tmp_path / "corpus"
    xml_path = corpus_dir / "in/union/notifications/cbic/central-tax/2025/2-2025.xml"
    xml_path.parent.mkdir(parents=True)
    text = "PoS AppuGhar kwH are table or proper-name tokens."
    write_xml(
        render_source_document(
            text,
            {
                "canonical_id": "/in/union/notifications/cbic/central-tax/2025/2-2025",
                "document_type": "notification",
                "title": "Notification 2/2025",
                "jurisdiction": "IN-UNION",
                "language": "eng",
                "source_type": "archived-source",
                "source_url": "official",
                "source_sha256": "0" * 64,
                "publication_date": "2025-01-01",
                "effective_from": "2025-01-01",
                "issuing_authority": "/in/authority/cbic",
                "review_status": "raw",
                "parser_version": "test",
            },
            {
                "document_type": "notification",
                "nodes": [
                    {
                        "type": "paragraph",
                        "label": "1",
                        "start": 0,
                        "end": len(text),
                        "text_hash": "0" * 64,
                        "confidence": 1.0,
                    }
                ],
                "references": [],
            },
        ),
        xml_path,
    )

    report = audit_corpus_quality(corpus_dir)

    assert report["stats"]["joined_token_hits"] == 0
    assert not report["flagged_documents"]


def test_act_parser_ignores_obvious_schedule_rows():
    text = (
        "9. Kisan Vikas Patra Scheme, 2019\n\n"
        "10. PM CARES for Children Scheme, 2021\n\n"
        "9. 1202 42 Groundnut kernel Rs. 1,500 per tonne\n\n"
        "10. In section 17 of the Income-tax Act,— Amendment of section 17."
    )
    structure = parse_structure_deterministic({"text": text, "source_sha256": "seed"}, document_type="act")
    section_labels = [node["label"] for node in structure["nodes"] if node["type"] == "section"]

    assert section_labels == ["10"]


def test_act_parser_recognizes_official_section_prefix_headings():
    text = (
        "* Section 73. Determination of tax not paid or short paid.-\n"
        "(1) Where it appears to the proper officer that any tax has not been paid, he shall serve notice."
    )
    structure = parse_structure_deterministic({"text": text, "source_sha256": "seed"}, document_type="act")

    assert structure["nodes"][0]["type"] == "section"
    assert structure["nodes"][0]["label"] == "73"


def test_act_renderer_uniquifies_repeated_section_eids(tmp_path):
    text = "9. Amendment of section 9.\n\n9. Amendment of another section 9."
    structure = parse_structure_deterministic({"text": text, "source_sha256": "seed"}, document_type="act")
    xml_path = tmp_path / "act.xml"

    write_xml(
        render_source_document(
            text,
            {
                "canonical_id": "/in/union/acts/test-act-2026",
                "document_type": "act",
                "title": "Test Act, 2026",
                "jurisdiction": "IN-UNION",
                "language": "eng",
                "source_type": "test",
                "source_url": "test",
                "source_sha256": "0" * 64,
                "publication_date": "2026-01-01",
                "effective_from": "2026-01-01",
                "issuing_authority": "/in/authority/parliament-of-india",
                "review_status": "test",
            },
            structure,
        ),
        xml_path,
    )

    root = ET.parse(xml_path).getroot()
    sections = root.findall(".//section")

    assert [section.attrib["eId"] for section in sections] == ["section_9", "section_9_2"]
    assert {section.attrib["refersTo"] for section in sections} == {"/in/union/acts/test-act-2026/section/9"}
    errors, _warnings, _canonical_id, _local_ids, _references = validate_xml_file(xml_path)
    assert not errors


def test_rules_parser_renders_rule_provisions(tmp_path):
    text = (
        "8. Application for registration. - Every applicant shall use FORM GST REG-01.\n"
        "9. Verification of the application. - The proper officer shall verify the application."
    )
    structure = parse_structure_deterministic({"text": text, "source_sha256": "seed"}, document_type="rules")
    assert [node["type"] for node in structure["nodes"]] == ["rule", "rule"]

    output = tmp_path / "corpus/in/union/rules/cgst-rules-2017/rules.xml"
    tree = render_source_document(
        text,
        {
            "canonical_id": "/in/union/rules/cgst-rules-2017",
            "document_type": "rules",
            "title": "Central Goods and Services Tax Rules, 2017",
        },
        structure,
    )
    write_xml(tree, output)

    entry = lookup_canonical_id(tmp_path / "corpus", "/in/union/rules/cgst-rules-2017/rule/8")

    assert entry is not None
    assert entry["provision"]["element_tag"] == "article"
    assert "Application for registration" in entry["provision"]["text"]


def test_corpus_lookup_keeps_richer_duplicate_provision(tmp_path):
    output = tmp_path / "corpus/in/union/acts/cgst-act-2017/act.xml"
    text = (
        "9. Levy and collection. - There shall be levied a tax called central goods and services tax.\n"
        "9. Footnote."
    )
    structure = parse_structure_deterministic({"text": text, "source_sha256": "seed"}, document_type="act")
    tree = render_source_document(
        text,
        {
            "canonical_id": "/in/union/acts/cgst-act-2017",
            "document_type": "act",
            "title": "The Central Goods and Services Tax Act, 2017",
        },
        structure,
    )
    write_xml(tree, output)

    entry = lookup_canonical_id(tmp_path / "corpus", "/in/union/acts/cgst-act-2017/section/9")

    assert entry is not None
    assert "Levy and collection" in entry["provision"]["text"]
    assert entry["provision"]["eId"] == "section_9"


def test_structure_parser_splits_pdf_line_heavy_notifications():
    text = (
        "NOTIFICATION\n"
        "No. 20/2025-Central Tax\n"
        "New Delhi, the 31st day of December, 2025\n"
        "G.S.R... (E). In exercise of the powers conferred by section 164 of the Central Goods and Services Tax Act,\n"
        "2017 (12 of 2017), the Central Government hereby makes the following rules.\n"
        "1. Short title and commencement. (1) These rules may be called as the Central Goods and Services Tax Rules.\n"
        "(2) They shall come into force from 1st day of February, 2026.\n"
        "2. In the Central Goods and Services Tax Rules, 2017, after rule 31C,\n"
        "the following rule shall be inserted, namely: -\n"
    )

    structure = parse_structure_deterministic({"text": text}, document_type="notification")
    errors, warnings = validate_structure_spans({"text": text}, structure)

    assert not errors
    assert not warnings
    assert len(structure["nodes"]) >= 7
    assert [node["type"] for node in structure["nodes"][:4]] == [
        "notification_title",
        "paragraph",
        "publication_date",
        "preamble",
    ]
    assert any(node["type"] == "amendment" and node["label"] == "2" for node in structure["nodes"])
    assert {reference["target"] for reference in structure["references"]} >= {
        "/in/union/acts/cgst-act-2017/section/164",
        "/in/union/rules/cgst-rules-2017/rule/31c",
    }


def test_amendment_event_date_parser_handles_cross_line_commencement_clause():
    text = (
        "1. Short title and commencement. -(1) These rules may be called the Central Goods and Services Tax\n"
        "(Second Amendment) Rules, 2022.\n"
        "(2) Save as otherwise provided in these rules, they shall come into force with effect from the 1st day of\n"
        "October, 2022.\n"
        "2. In the Central Goods and Services Tax Rules, 2017, in rule 21, after clause (g), the following clauses shall be inserted."
    )

    assert parse_legal_date(text) == "2022-10-01"


def test_amendment_event_date_parser_handles_from_without_the():
    text = (
        "1. Short title and commencement. (1) These rules may be called as the Central Goods and Services Tax "
        "(Fifth Amendment) Rules, 2025. (2) They shall come into force from 1st day of February, 2026."
    )

    assert parse_legal_date(text) == "2026-02-01"


def test_compound_child_fragments_inherit_parent_rule_context_before_event_generation():
    text = (
        "1. These rules shall come into force on the 1st day of January, 2024.\n"
        "2. In the said rules, in rule 37,- "
        "(a) in sub-rule (2), for the words \"old amount\", the words \"new amount\" shall be substituted; "
        "(b) in sub-rule (3), after the words \"suppression of facts\", the words \"under section 74\" "
        "shall be inserted."
    )
    source = SourceRecord(
        record={"no": "1/2024-Central Tax", "category": "Central Tax", "id": 1},
        json_path=Path("fixture.json"),
        text=text,
        text_source="contentText",
        source_file_sha256="file-sha",
        source_text_sha256="text-sha",
        source_url="",
        document_id="/in/union/notifications/cbic/central-tax/2024/1-2024",
        publication_date="2024-01-01",
        commencement_date="2024-01-01",
        date_basis="explicit_commencement_clause",
    )
    corpus_lookup = {
        "/in/union/rules/cgst-rules-2017": {"canonical_id": "/in/union/rules/cgst-rules-2017", "roles": ["document"]},
        "/in/union/rules/cgst-rules-2017/rule/37": {
            "canonical_id": "/in/union/rules/cgst-rules-2017/rule/37",
            "roles": ["provision"],
            "provision": {"text": "37. Rule text."},
        },
        "/in/union/rules/cgst-rules-2017/rule/37/subrule/2": {
            "canonical_id": "/in/union/rules/cgst-rules-2017/rule/37/subrule/2",
            "roles": ["provision"],
            "provision": {"text": "(2) old amount"},
        },
        "/in/union/rules/cgst-rules-2017/rule/37/subrule/3": {
            "canonical_id": "/in/union/rules/cgst-rules-2017/rule/37/subrule/3",
            "roles": ["provision"],
            "provision": {"text": "(3) demand confirmed on account of suppression of facts."},
        },
    }

    blocks = [block for block in amendment_blocks(text) if str(block.get("label", "")).startswith("2.")]
    assert {block.get("context_rule") for block in blocks} == {"37"}
    assert any("in rule 37, in sub-rule (3)" in block.get("parse_text", "") for block in blocks)

    events = compile_events_from_text(
        source,
        target_work="/in/union/rules/cgst-rules-2017",
        corpus_lookup=corpus_lookup,
    )
    by_target = {event["target"]["component_id"]: event for event in events}
    child_event = by_target["/in/union/rules/cgst-rules-2017/rule/37/subrule/3"]

    assert child_event["operation"] == "SPLICE"
    assert child_event["payload"]["insert_text"] == "under section 74"
    assert child_event["evidence"]["parser_trace"]["context_rule"] == "37"
    assert child_event["evidence"]["parser_trace"]["context_inherited"] is True
    assert "in rule 37, in sub-rule (3)" in child_event["evidence"]["parser_trace"]["context_parse_text"]
    assert child_event["validation"]["materializable"] is True


def test_nested_compound_children_inherit_parent_rule_and_subrule_context_before_event_generation():
    text = (
        "1. These rules shall come into force on the 1st day of January, 2024.\n"
        "2. In the said rules, in rule 37, in sub-rule (2),- "
        "(i) for the words \"old amount\", the words \"new amount\" shall be substituted; "
        "(ii) after the words \"registered person\", the words \"under section 74\" shall be inserted."
    )
    source = SourceRecord(
        record={"no": "1/2024-Central Tax", "category": "Central Tax", "id": 1},
        json_path=Path("fixture.json"),
        text=text,
        text_source="contentText",
        source_file_sha256="file-sha",
        source_text_sha256="text-sha",
        source_url="",
        document_id="/in/union/notifications/cbic/central-tax/2024/1-2024",
        publication_date="2024-01-01",
        commencement_date="2024-01-01",
        date_basis="explicit_commencement_clause",
    )
    corpus_lookup = {
        "/in/union/rules/cgst-rules-2017": {"canonical_id": "/in/union/rules/cgst-rules-2017", "roles": ["document"]},
        "/in/union/rules/cgst-rules-2017/rule/37": {
            "canonical_id": "/in/union/rules/cgst-rules-2017/rule/37",
            "roles": ["provision"],
            "provision": {"text": "37. Rule text."},
        },
        "/in/union/rules/cgst-rules-2017/rule/37/subrule/2": {
            "canonical_id": "/in/union/rules/cgst-rules-2017/rule/37/subrule/2",
            "roles": ["provision"],
            "provision": {"text": "(2) old amount registered person."},
        },
    }

    blocks = [block for block in amendment_blocks(text) if str(block.get("label", "")).startswith("2.")]
    assert {block.get("context_rule") for block in blocks} == {"37"}
    assert {block.get("context_subrule") for block in blocks} == {"2"}
    assert all("in rule 37, in sub-rule (2)" in block.get("parse_text", "") for block in blocks)

    events = compile_events_from_text(
        source,
        target_work="/in/union/rules/cgst-rules-2017",
        corpus_lookup=corpus_lookup,
    )
    subrule_events = [
        event
        for event in events
        if event["target"]["component_id"] == "/in/union/rules/cgst-rules-2017/rule/37/subrule/2"
    ]

    assert {event["operation"] for event in subrule_events} == {"SUBSTITUTE", "SPLICE"}
    for event in subrule_events:
        trace = event["evidence"]["parser_trace"]
        assert trace["context_rule"] == "37"
        assert trace["context_subrule"] == "2"
        assert trace["context_inherited"] is True
        assert "in rule 37, in sub-rule (2)" in trace["context_parse_text"]
        assert event["validation"]["materializable"] is True


def test_structure_parser_prefers_line_blocks_when_double_newline_blocks_are_large():
    long_intro = "Introductory material " + ("x" * 2100)
    text = (
        "NOTIFICATION\n\n"
        f"{long_intro}\n"
        "1. First amendment text.\n"
        "2. Second amendment text.\n\n"
        "Note: publication history."
    )

    structure = parse_structure_deterministic({"text": text}, document_type="notification")

    labels = [node["label"] for node in structure["nodes"]]
    assert "1" in labels
    assert "2" in labels
    assert len(structure["nodes"]) >= 3


def test_structure_parser_splits_numeric_table_rows_in_large_blocks():
    text = (
        "TABLE\n"
        "S. No. Name and Address Notice Details Authority\n"
        "1 Memo Technology Private Limited ; 3D-282, Kanpur DGGI/INV/GST/1 Joint Commissioner\n"
        "2 Rocketfy Technology Private Limited ; Delhi DGGI/INV/GST/2 Additional Commissioner\n"
        + ("Continuation text " * 180)
    )

    structure = parse_structure_deterministic({"text": text}, document_type="notification")

    labels = [node["label"] for node in structure["nodes"]]
    assert "1" in labels
    assert "2" in labels


def test_structure_parser_splits_embedded_form_boundaries():
    text = (
        "45. In the said rules, for FORM GST DRC-01A, the following Form shall be substituted, namely:-\n"
        "FORM GST DRC-01A\n"
        "Intimation of tax ascertained as being payable under section 73(5)/74(5)\n"
        "Part A\n"
        "No.: Date:\n"
        "Verification\n"
        "I hereby declare that the information is true.\n"
    )

    structure = parse_structure_deterministic({"text": text}, document_type="notification")

    assert [node["type"] for node in structure["nodes"]] == [
        "amendment",
        "form",
        "form_part",
        "verification",
    ]


def test_structure_parser_splits_form_option_boundaries():
    text = (
        "4. Cancellation shall not affect liability to pay tax.\n"
        "OR\n"
        "Order of Cancellation of Registration as Tax Deductor at source\n"
        "This has reference to the show-cause notice issued dated...\n"
        "o Whereas no reply to the show cause notice has been submitted,\n"
        "and whereas, the undersigned is of the opinion that your registration is liable to be cancelled.\n"
        "The effective date of cancellation of registration is <<DD/MM/YYYY>>.\n"
    )

    structure = parse_structure_deterministic({"text": text}, document_type="notification")

    assert len(structure["nodes"]) >= 5
    assert any("Order of Cancellation" in text[node["start"] : node["end"]] for node in structure["nodes"])
    assert any("o Whereas" in text[node["start"] : node["end"]] for node in structure["nodes"])


def test_structure_parser_splits_annexure_statement_and_schema_rows():
    text = (
        "10. Verification\n"
        "I hereby declare the information is true.\n"
        "Annexure-1\n"
        "Statement -1 [rule 89(5)]\n"
        "4C Aggregate value of exports shall be declared here.\n"
        "A.1.0 ShipTo_Legal_Name 1..1 Ship To Legal Name Mandatory String.\n"
        "DECLARATION [section 54(3)]\n"
        "I hereby declare that the refund claim is correct.\n"
    )

    structure = parse_structure_deterministic({"text": text}, document_type="notification")

    types = [node["type"] for node in structure["nodes"]]
    assert "annexure" in types
    assert "statement" in types
    assert "declaration" in types
    assert any("4C Aggregate" in text[node["start"] : node["end"]] for node in structure["nodes"])
    assert any("A.1.0 ShipTo" in text[node["start"] : node["end"]] for node in structure["nodes"])


def test_structure_parser_splits_table_keys_and_provisos():
    text = (
        "Table No. Instructions\n"
        "4C Aggregate value of exports shall be declared here.\n"
        "4D Aggregate value of supplies to SEZs shall be declared here.\n"
        "Provided that the transporter may furnish information.\n"
        "Provided further that the consignor may authorize a courier agency.\n"
    )

    structure = parse_structure_deterministic({"text": text}, document_type="notification")

    blocks = [text[node["start"] : node["end"]] for node in structure["nodes"]]
    assert any(block.startswith("4C Aggregate") for block in blocks)
    assert any(block.startswith("4D Aggregate") for block in blocks)
    assert sum(1 for block in blocks if block.startswith("Provided")) == 2


def test_generic_source_document_rendering_supports_circulars(tmp_path):
    text = "CIRCULAR\n\nGuidance for registration under rule 10."
    extracted = {"text": text, "source_sha256": "seed"}
    structure = parse_structure_deterministic(extracted, document_type="circular")
    metadata = {
        "canonical_id": "/in/union/circulars/cbic/2026/test",
        "document_type": "circular",
        "title": "Registration guidance circular",
        "jurisdiction": "IN-UNION",
        "language": "eng",
        "source_type": "test",
        "source_url": "test.txt",
        "source_sha256": "seed",
        "publication_date": "2026-01-01",
        "effective_from": "2026-01-01",
        "issuing_authority": "/in/authority/cbic",
        "review_status": "test",
        "parser_version": structure["parser"],
    }

    output = tmp_path / "circular.xml"
    write_xml(render_source_document(text, metadata, structure), output)
    errors, warnings, canonical_id, _local_ids, references = validate_xml_file(output)
    tree = ET.parse(output)
    props = {node.attrib["name"]: node.attrib["value"] for node in tree.findall(".//property")}

    assert not errors
    assert canonical_id == "/in/union/circulars/cbic/2026/test"
    assert props["document_type"] == "circular"
    assert tree.getroot().find("./doc").attrib["name"] == "circular"
    assert references == [(output.as_posix(), "/in/union/rules/cgst-rules-2017/rule/10")]
    assert not warnings
    paragraph = tree.find(".//paragraph")
    assert paragraph.attrib["sourceStart"] == "0"
    assert paragraph.attrib["sourceEnd"] == "8"
    assert paragraph.attrib["sourceNodeType"] == "paragraph"
    assert paragraph.attrib["sourceHash"] == structure["nodes"][0]["text_hash"]

    span_errors, span_warnings = validate_xml_source_spans(output, extracted)
    assert not span_errors
    assert not span_warnings


def test_local_llm_provider_uses_openai_compatible_endpoint(monkeypatch):
    calls = []

    def fake_parse_structure_with_llm(extracted, api_key, model, base_url=None):
        calls.append({"api_key": api_key, "model": model, "base_url": base_url})
        return {
            "parser": f"llm:{model}",
            "nodes": [
                {
                    "type": "paragraph",
                    "start": 0,
                    "end": len(extracted["text"]),
                    "text_hash": structure_parser.text_hash(extracted["text"]),
                    "confidence": 0.9,
                }
            ],
            "references": [],
        }

    monkeypatch.delenv("LOCAL_LLM_API_KEY", raising=False)
    monkeypatch.delenv("LOCAL_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("LOCAL_LLM_MODEL", "indian-law-local")
    monkeypatch.setattr(structure_parser, "parse_structure_with_llm", fake_parse_structure_with_llm)

    structure = parse_structure({"text": "local llm text"}, mode="llm", provider="local")

    assert structure["parser"] == "llm:indian-law-local"
    assert calls == [
        {
            "api_key": "local",
            "model": "indian-law-local",
            "base_url": "http://100.79.90.123:8000/v1",
        }
    ]


def test_ingest_source_file_converts_text_to_canonical_xml(tmp_path):
    source = tmp_path / "circular.txt"
    source.write_text("CIRCULAR\n\nGuidance for registration under rule 10.", encoding="utf-8")
    source_dir = tmp_path / "sources/cbic/circulars/2026/test"
    output = tmp_path / "corpus/in/union/circulars/cbic/2026/test.xml"

    report = ingest_source_file(
        source,
        source_dir,
        output,
        {
            "canonical_id": "/in/union/circulars/cbic/2026/test",
            "document_type": "circular",
            "title": "Registration guidance circular",
            "jurisdiction": "IN-UNION",
            "language": "eng",
            "publication_date": "2026-01-01",
            "effective_from": "2026-01-01",
            "issuing_authority": "/in/authority/cbic",
            "review_status": "raw",
            "parser_version": "unparsed",
            "source_url": "official-test-url",
            "source_type": "archived-source",
        },
        mode="deterministic",
    )

    assert output.exists()
    assert source_dir.joinpath("metadata.yaml").exists()
    assert source_dir.joinpath("extracted_text.json").exists()
    assert source_dir.joinpath("structure.json").exists()
    assert report["xml"] == str(output)
    assert report["nodes"] == 2
    assert report["references"] == 1

    validation = validate_source_archive(source_dir)
    assert validation.ok, validation.errors
    errors, _warnings, canonical_id, _local_ids, references = validate_xml_file(output)
    assert not errors
    assert canonical_id == "/in/union/circulars/cbic/2026/test"
    assert references == [(output.as_posix(), "/in/union/rules/cgst-rules-2017/rule/10")]
    extracted = json.loads(source_dir.joinpath("extracted_text.json").read_text(encoding="utf-8"))
    span_errors, span_warnings = validate_xml_source_spans(output, extracted)
    assert not span_errors
    assert not span_warnings


def test_cbic_source_inventory_maps_local_pdfs_to_canonical_targets(tmp_path):
    root_dir = tmp_path / "data/Law"
    cbic_dir = root_dir / "GST_Notifications_CBIC"
    central_tax_dir = cbic_dir / "Central_Tax"
    central_tax_dir.mkdir(parents=True)
    source_pdf = central_tax_dir / "1010504_gst-ct-18-2025.pdf"
    duplicate_pdf = central_tax_dir / "gst-ct-18-2025.pdf"
    source_pdf.write_bytes(b"%PDF-1.4 test")
    duplicate_pdf.write_bytes(b"%PDF-1.4 duplicate")
    index_csv = cbic_dir / "_notification_index.csv"
    index_csv.write_text(
        "\ufeffcategory,notification_no,date,description,content_id,record_id,pdf_filename,original_filename,pdf_url\n"
        "Central Tax,18/2025-Central Tax,2025-10-31,Fourth Amendment Rules,1501010504,1010504,"
        "1010504_gst-ct-18-2025.pdf,gst-ct-18-2025.pdf,https://example.test/18\n"
        "Central Tax,19/2025-Central Tax,2025-12-31,Valuation notification,1501010545,1010545,"
        "1010545_19-2025-ct.pdf,19-2025-ct.pdf,https://example.test/19\n",
        encoding="utf-8",
    )

    inventory = build_source_inventory(
        root_dir,
        sources_root=tmp_path / "sources",
        corpus_root=tmp_path / "corpus",
        include_unclassified=False,
    )

    assert category_slug("Central Tax (Rate)") == "central-tax-rate"
    assert inventory["stats"]["items"] == 2
    assert inventory["stats"]["ready"] == 1
    assert inventory["stats"]["missing"] == 1
    ready = inventory["items"][0]
    assert ready["status"] == "ready"
    assert ready["canonical_id"] == "/in/union/notifications/cbic/central-tax/2025/18-2025"
    assert ready["source_sha256"]
    assert ready["source_dir"].endswith("sources/cbic/central-tax/2025/18-2025")
    assert ready["output_path"].endswith("corpus/in/union/notifications/cbic/central-tax/2025/18-2025.xml")
    assert ready["ingest_command"][:4] == ["python3", "main.py", "corpus", "ingest"]
    assert inventory["items"][1]["status"] == "missing"
    assert all(item["source_path"] != str(duplicate_pdf) for item in inventory["items"])
    assert validate_source_inventory(inventory).ok

    output = tmp_path / "derived/sources/source_inventory.json"
    write_source_inventory(inventory, output)
    assert json.loads(output.read_text(encoding="utf-8")) == inventory
    assert validate_source_inventory_file(output).ok

    tampered = json.loads(json.dumps(inventory))
    tampered["items"][0]["source_sha256"] = "bad"
    validation = validate_source_inventory(tampered)
    assert not validation.ok
    assert any("source_sha256 must be a SHA-256 hex digest" in error for error in validation.errors)

    report = build_inventory_report(inventory)
    assert report["profile"] == "git-for-law-source-inventory-report-v1"
    assert report["stats"]["ready"] == 1
    assert report["stats"]["missing"] == 1
    assert report["validation"]["ok"]
    assert report["missing"][0]["expected_pdf_filename"] == "1010545_19-2025-ct.pdf"

    report_path = tmp_path / "derived/sources/source_inventory_report.json"
    write_inventory_report(report, report_path)
    assert json.loads(report_path.read_text(encoding="utf-8")) == report


def test_cbic_inventory_matches_missing_rows_by_pdf_text(monkeypatch, tmp_path):
    root_dir = tmp_path / "data/Law"
    cbic_dir = root_dir / "GST_Notifications_CBIC"
    central_tax_dir = cbic_dir / "Central_Tax"
    central_tax_dir.mkdir(parents=True)
    loose_notification = central_tax_dir / "NN-19_2019-CT.pdf"
    loose_corrigendum = central_tax_dir / "MINISTRY OF FINANCE.pdf"
    loose_notification.write_bytes(b"%PDF notification")
    loose_corrigendum.write_bytes(b"%PDF corrigendum")
    index_csv = cbic_dir / "_notification_index.csv"
    index_csv.write_text(
        "\ufeffcategory,notification_no,date,description,content_id,record_id,pdf_filename,original_filename,pdf_url\n"
        "Central Tax,19/2019-Central Tax,2019-04-22,Return extension,1501000696,1000696,"
        "1000696_19_2019-Central_Tax_EN.pdf,,https://example.test/19\n"
        "Central Tax,Corrigendum,2019-02-05,Corrigendum to Notification No. 03/2019-Central Tax.,"
        "1501000714,1000714,1000714_Eng_Corring_notfn_no_03-19_0502.pdf,"
        "Eng_Corring_notfn_no_03-19_0502.pdf,https://example.test/corr\n",
        encoding="utf-8",
    )

    def fake_preview(path):
        if path == loose_notification:
            return "Notification No. 19/2019 - Central Tax New Delhi, the 22nd April, 2019"
        if path == loose_corrigendum:
            return "CENTRAL BOARD OF INDIRECT TAXES AND CUSTOMS CORRIGENDUM No.3/2019-Central Tax"
        return ""

    monkeypatch.setattr(source_inventory, "_pdf_preview", fake_preview)

    inventory = build_source_inventory(
        root_dir,
        sources_root=tmp_path / "sources",
        corpus_root=tmp_path / "corpus",
        include_unclassified=True,
    )

    assert inventory["stats"]["ready"] == 2
    assert inventory["stats"]["missing"] == 0
    assert inventory["stats"]["unclassified"] == 0
    ids = {item["canonical_id"]: item for item in inventory["items"]}
    assert "/in/union/notifications/cbic/central-tax/2019/19-2019" in ids
    corr_id = "/in/union/notifications/cbic/central-tax/2019/3-2019/corrigenda/2019-02-05"
    assert corr_id in ids
    assert ids[corr_id]["subtype"] == "corrigendum"
    assert ids[corr_id]["source_dir"].endswith("sources/cbic/central-tax/2019/3-2019/corrigenda/2019-02-05")
    assert ids[corr_id]["output_path"].endswith(
        "corpus/in/union/notifications/cbic/central-tax/2019/3-2019/corrigenda/2019-02-05.xml"
    )


def test_cbic_corrigendum_canonical_ids_are_target_and_date_scoped():
    assert canonical_cbic_notification_id(
        "Central Tax",
        "Corrigendum",
        "2019-02-05",
        "Corrigendum to Notification No. 03/2019-Central Tax.",
    ) == "/in/union/notifications/cbic/central-tax/2019/3-2019/corrigenda/2019-02-05"


def test_source_inventory_classifies_root_acts_and_skips_resource_forks(tmp_path):
    root_dir = tmp_path / "data/Law"
    root_dir.mkdir(parents=True)
    finance_act = root_dir / "Finance Act (8 of 2024) 2024.pdf"
    customs_tariff = root_dir / "Customs Tariff.pdf"
    resource_fork = root_dir / "._Finance Act (8 of 2024) 2024.pdf"
    finance_act.write_bytes(b"%PDF-1.4 finance")
    customs_tariff.write_bytes(b"%PDF-1.4 customs")
    resource_fork.write_bytes(b"not a legal source")

    inventory = build_source_inventory(
        root_dir,
        sources_root=tmp_path / "sources",
        corpus_root=tmp_path / "corpus",
    )

    ids = {item["canonical_id"]: item for item in inventory["items"]}
    assert inventory["stats"]["items"] == 2
    assert inventory["stats"]["ready"] == 2
    assert inventory["stats"]["unclassified"] == 0
    assert "/in/union/acts/finance-act-2024-8-of-2024" in ids
    assert ids["/in/union/acts/finance-act-2024-8-of-2024"]["document_type"] == "act"
    assert ids["/in/union/acts/finance-act-2024-8-of-2024"]["output_path"].endswith(
        "corpus/in/union/acts/finance-act-2024-8-of-2024/act.xml"
    )
    assert "/in/union/acts/customs-tariff-act-1975" in ids
    assert all("._" not in item["source_path"] for item in inventory["items"])


def test_split_forms_archive_writes_individual_form_documents(tmp_path):
    source_dir = tmp_path / "sources/in/union/forms/cgst-rules-2017-forms"
    source_dir.mkdir(parents=True)
    text = (
        "[FORM GST EWB-01\nDetails of goods.\n\n"
        "FORM GST RFD -01\nApplication for refund.\n\n"
        "FORM GST EWB-01\nDuplicate reference."
    )
    source_dir.joinpath("metadata.yaml").write_text(
        "\n".join(
            [
                "canonical_id: /in/union/forms/cgst-rules-2017-forms",
                "document_type: form",
                "title: CGST Rules Forms",
                "source_type: source-text",
                "review_status: extracted",
                "source_sha256: abc",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    source_dir.joinpath("extracted_text.json").write_text(
        json.dumps(
            {
                "source_sha256": "abc",
                "text": text,
                "pages": [{"page_number": 1, "start": 0, "end": len(text), "text": text}],
            }
        ),
        encoding="utf-8",
    )
    structure = parse_structure_deterministic({"text": text, "source_sha256": "abc"}, document_type="form")
    source_dir.joinpath("structure.json").write_text(json.dumps(structure), encoding="utf-8")

    report = split_forms_archive(source_dir, tmp_path / "corpus")

    assert report["stats"] == {"generated": 2, "skipped": 1}
    assert (tmp_path / "corpus/in/union/forms/gst-ewb-01/form.xml").exists()
    assert (tmp_path / "corpus/in/union/forms/gst-rfd-01/form.xml").exists()
    assert lookup_canonical_id(tmp_path / "corpus", "/in/union/forms/gst-ewb-01") is not None


def test_form_reference_canonicalization_collapses_spaced_hyphens():
    assert canonicalize_legacy_reference("FORM_GST_ADT___01") == "/in/union/forms/gst-adt-01"
    assert canonicalize_legacy_reference("/in/union/forms/gst-adt---01") == "/in/union/forms/gst-adt-01"


def test_batch_ingest_inventory_previews_and_executes_selected_items(tmp_path):
    source = tmp_path / "local-circular.txt"
    source.write_text("CIRCULAR\n\nGuidance for registration under rule 10.", encoding="utf-8")
    source_dir = tmp_path / "sources/cbic/circulars/2026/local"
    output_xml = tmp_path / "corpus/in/union/circulars/cbic/2026/local.xml"
    inventory_path = tmp_path / "derived/sources/source_inventory.json"
    inventory = {
        "profile": "git-for-law-source-inventory-v1",
        "items": [
            {
                "kind": "test",
                "status": "ready",
                "canonical_id": "/in/union/circulars/cbic/2026/local",
                "document_type": "circular",
                "title": "Local circular",
                "description": "Test circular",
                "category_slug": "circulars",
                "publication_date": "2026-01-01",
                "effective_from": "2026-01-01",
                "jurisdiction": "IN-UNION",
                "language": "eng",
                "issuing_authority": "/in/authority/cbic",
                "source_url": "local-circular.txt",
                "source_path": str(source),
                "source_dir": str(source_dir),
                "output_path": str(output_xml),
                "record_id": "test-record",
                "content_id": "test-content",
            }
        ],
    }
    inventory_path.parent.mkdir(parents=True)
    inventory_path.write_text(json.dumps(inventory), encoding="utf-8")

    preview = ingest_inventory(inventory_path, category="circulars", limit=1)

    assert preview["stats"]["planned"] == 1
    assert not output_xml.exists()

    executed = ingest_inventory(inventory_path, execute=True, category="circulars", limit=1)

    assert executed["stats"]["ingested"] == 1
    assert output_xml.exists()
    assert source_dir.joinpath("metadata.yaml").exists()
    assert validate_corpus(tmp_path / "corpus").ok

    report_path = tmp_path / "derived/ingest/report.json"
    write_batch_ingest_report(executed, report_path)
    assert json.loads(report_path.read_text(encoding="utf-8")) == executed


def test_batch_ingest_inventory_reports_progress(tmp_path):
    source = tmp_path / "local-circular.txt"
    source.write_text("CIRCULAR\n\nGuidance for registration under rule 10.", encoding="utf-8")
    output_xml = tmp_path / "corpus/in/union/circulars/cbic/2026/local.xml"
    inventory_path = tmp_path / "inventory.json"
    inventory = {
        "items": [
            {
                "kind": "test",
                "status": "ready",
                "canonical_id": "/in/union/circulars/cbic/2026/local",
                "document_type": "circular",
                "title": "Local circular",
                "category_slug": "circulars",
                "publication_date": "2026-01-01",
                "effective_from": "2026-01-01",
                "jurisdiction": "IN-UNION",
                "language": "eng",
                "issuing_authority": "/in/authority/cbic",
                "source_url": "local-circular.txt",
                "source_path": str(source),
                "source_dir": str(tmp_path / "sources/cbic/circulars/2026/local"),
                "output_path": str(output_xml),
            }
        ],
    }
    inventory_path.write_text(json.dumps(inventory), encoding="utf-8")
    events = []

    report = ingest_inventory(inventory_path, execute=True, progress=events.append)

    assert report["stats"]["ingested"] == 1
    assert len(events) == 1
    assert events[0]["index"] == 1
    assert events[0]["total"] == 1
    assert events[0]["status"] == "ingested"
    assert events[0]["canonical_id"] == "/in/union/circulars/cbic/2026/local"


def test_batch_promotion_plan_selects_unflagged_generated_xml(tmp_path):
    generated_corpus = tmp_path / "generated-corpus"
    generated_sources = tmp_path / "sources"
    target_corpus = tmp_path / "target-corpus"
    target_sources = tmp_path / "target-sources"
    clean_xml = generated_corpus / "in/union/notifications/cbic/central-tax/2025/1-2025.xml"
    flagged_xml = generated_corpus / "in/union/notifications/cbic/central-tax/2025/2-2025.xml"
    clean_source = generated_sources / "cbic/central-tax/2025/1-2025"
    flagged_source = generated_sources / "cbic/central-tax/2025/2-2025"
    clean_xml.parent.mkdir(parents=True)
    clean_xml.write_text("<akomaNtoso />", encoding="utf-8")
    flagged_xml.write_text("<akomaNtoso />", encoding="utf-8")
    clean_source.mkdir(parents=True)
    flagged_source.mkdir(parents=True)
    clean_source.joinpath("metadata.yaml").write_text("canonical_id: clean\n", encoding="utf-8")
    flagged_source.joinpath("metadata.yaml").write_text("canonical_id: flagged\n", encoding="utf-8")
    ingest_report_path = tmp_path / "ingest.json"
    quality_report_path = tmp_path / "quality.json"
    ingest_report = {
        "items": [
            {
                "canonical_id": "/in/union/notifications/cbic/central-tax/2025/1-2025",
                "status": "ingested",
                "source_path": "source-1.pdf",
                "source_dir": str(clean_source),
                "output_path": str(clean_xml),
            },
            {
                "canonical_id": "/in/union/notifications/cbic/central-tax/2025/2-2025",
                "status": "ingested",
                "source_path": "source-2.pdf",
                "source_dir": str(flagged_source),
                "output_path": str(flagged_xml),
            },
        ],
    }
    quality_report = {
        "corpus_dir": str(generated_corpus),
        "flagged_documents": [
            {
                "canonical_id": "/in/union/notifications/cbic/central-tax/2025/2-2025",
                "long_paragraphs": 1,
            }
        ],
    }
    ingest_report_path.write_text(json.dumps(ingest_report), encoding="utf-8")
    quality_report_path.write_text(json.dumps(quality_report), encoding="utf-8")

    plan = plan_batch_promotion(
        ingest_report_path,
        quality_report_path,
        target_corpus_dir=target_corpus,
        target_sources_dir=target_sources,
    )
    output = write_promotion_plan(plan, tmp_path / "plan.json")
    dry_run = apply_batch_promotion(plan)

    assert output.exists()
    assert plan["stats"]["selected"] == 1
    assert plan["stats"]["quality_flagged"] == 1
    assert plan["selected"][0]["canonical_id"] == "/in/union/notifications/cbic/central-tax/2025/1-2025"
    assert plan["selected"][0]["target_source_dir"] == str(
        target_sources / "cbic/central-tax/2025/1-2025"
    )
    assert dry_run["stats"]["copied"] == 0
    assert not target_corpus.exists()
    assert not target_sources.exists()

    applied = apply_batch_promotion(plan, approve=True)

    assert applied["stats"]["copied"] == 1
    assert applied["stats"]["copied_sources"] == 1
    assert target_corpus.joinpath("in/union/notifications/cbic/central-tax/2025/1-2025.xml").exists()
    assert target_sources.joinpath("cbic/central-tax/2025/1-2025/metadata.yaml").exists()

    existing_target_plan = plan_batch_promotion(
        ingest_report_path,
        quality_report_path,
        target_corpus_dir=target_corpus,
        target_sources_dir=target_sources,
    )
    assert existing_target_plan["stats"]["target_exists"] == 1


def test_batch_promotion_copies_shared_source_target_once(tmp_path):
    generated_corpus = tmp_path / "generated-corpus"
    generated_sources = tmp_path / "sources"
    target_corpus = tmp_path / "target-corpus"
    target_sources = tmp_path / "target-sources"
    source_dir = generated_sources / "cbic/central-tax/2022/20-2022/corrigenda/2022-09-29"
    source_dir.mkdir(parents=True)
    source_dir.joinpath("metadata.yaml").write_text("canonical_id: source\n", encoding="utf-8")
    first_xml = generated_corpus / "in/union/notifications/cbic/central-tax/2022/20-2022.xml"
    second_xml = generated_corpus / "in/union/notifications/cbic/central-tax/2022/20-2022-corrigendum.xml"
    first_xml.parent.mkdir(parents=True)
    first_xml.write_text("<akomaNtoso />", encoding="utf-8")
    second_xml.write_text("<akomaNtoso />", encoding="utf-8")
    ingest_report_path = tmp_path / "ingest.json"
    quality_report_path = tmp_path / "quality.json"
    ingest_report = {
        "items": [
            {
                "canonical_id": "/in/union/notifications/cbic/central-tax/2022/20-2022",
                "status": "ingested",
                "source_dir": str(source_dir),
                "output_path": str(first_xml),
            },
            {
                "canonical_id": "/in/union/notifications/cbic/central-tax/2022/20-2022-corrigendum",
                "status": "ingested",
                "source_dir": str(source_dir),
                "output_path": str(second_xml),
            },
        ],
    }
    quality_report = {"corpus_dir": str(generated_corpus), "flagged_documents": []}
    ingest_report_path.write_text(json.dumps(ingest_report), encoding="utf-8")
    quality_report_path.write_text(json.dumps(quality_report), encoding="utf-8")

    plan = plan_batch_promotion(
        ingest_report_path,
        quality_report_path,
        target_corpus_dir=target_corpus,
        target_sources_dir=target_sources,
    )
    applied = apply_batch_promotion(plan, approve=True)

    assert plan["stats"]["selected"] == 2
    assert applied["stats"]["copied_xml"] == 2
    assert applied["stats"]["copied_sources"] == 1
    assert target_corpus.joinpath("in/union/notifications/cbic/central-tax/2022/20-2022.xml").exists()
    assert target_corpus.joinpath(
        "in/union/notifications/cbic/central-tax/2022/20-2022-corrigendum.xml"
    ).exists()
    assert target_sources.joinpath("cbic/central-tax/2022/20-2022/corrigenda/2022-09-29/metadata.yaml").exists()


def test_seed_outputs_valid_corpus_and_graph(tmp_path):
    corpus_dir = tmp_path / "corpus"
    sources_dir = tmp_path / "sources"

    outputs = seed_from_existing_data(ROOT, corpus_dir=corpus_dir, sources_dir=sources_dir)

    assert corpus_dir.joinpath("in/union/rules/cgst-rules-2017/rule-008.xml").exists()
    assert corpus_dir.joinpath("in/union/forms/gst-reg-01/form.xml").exists()
    assert sources_dir.joinpath("cbic/central-tax/2025/18-2025/extracted_text.json").exists()
    assert len(outputs) == 9

    validation = validate_corpus(corpus_dir)
    assert validation.ok, validation.errors
    assert validation.checked_files == 5
    source_validation = validate_source_archive(sources_dir / "cbic/central-tax/2025/18-2025")
    assert source_validation.ok, source_validation.errors

    tree = ET.parse(corpus_dir / "in/union/rules/cgst-rules-2017/rule-010.xml")
    props = {node.attrib["name"]: node.attrib["value"] for node in tree.findall(".//property")}
    assert props["canonical_id"] == "/in/union/rules/cgst-rules-2017/rule/10"
    assert props["source_sha256"]

    notification_xml = corpus_dir / "in/union/notifications/cbic/central-tax/2025/18-2025.xml"
    notification_extracted = json.loads(
        sources_dir.joinpath("cbic/central-tax/2025/18-2025/extracted_text.json").read_text(encoding="utf-8")
    )
    span_errors, span_warnings = validate_xml_source_spans(notification_xml, notification_extracted)
    assert not span_errors
    assert not span_warnings
    assert ET.parse(notification_xml).find(".//paragraph").attrib["sourceHash"]

    graph_path = tmp_path / "derived/graph/corpus_graph.json"
    graph = rebuild_graph_index(corpus_dir, graph_path)
    assert graph_path.exists()
    assert len(graph["nodes"]) == 19
    rule_10_node = next(node for node in graph["nodes"] if node["id"] == "/in/union/rules/cgst-rules-2017/rule/10")
    assert rule_10_node["kinds"] == ["document", "provision"]
    assert {
        "source": "/in/union/rules/cgst-rules-2017/rule/10",
        "target": "/in/union/rules/cgst-rules-2017/rule/10/subrule/1",
        "type": "CONTAINS",
    } in graph["edges"]
    assert {
        "source": "/in/union/notifications/cbic/central-tax/2025/18-2025",
        "target": "/in/union/rules/cgst-rules-2017/rule/10/subrule/1",
        "type": "SPLICE",
        "eId": "mod_3",
    } in graph["edges"]

    raw_graph = json.loads(graph_path.read_text(encoding="utf-8"))
    assert raw_graph == graph

    payload_path = tmp_path / "derived/graph/corpus_neo4j_payload.json"
    payload = write_neo4j_payload(corpus_dir, payload_path)
    assert payload_path.exists()
    assert payload["statements"][0]["cypher"].startswith("CREATE CONSTRAINT legal_node_id")
    assert any("UNWIND $nodes AS node" in statement["cypher"] for statement in payload["statements"])
    assert any("SPLICE" in statement["cypher"] for statement in payload["statements"])

    in_memory_payload = build_neo4j_payload(graph)
    assert len(in_memory_payload["statements"]) == len(payload["statements"])


def test_corpus_validation_rejects_duplicate_eids_and_hierarchy_drift(tmp_path):
    corpus_dir = tmp_path / "corpus"
    sources_dir = tmp_path / "sources"
    seed_from_existing_data(ROOT, corpus_dir=corpus_dir, sources_dir=sources_dir)
    path = corpus_dir / "in/union/rules/cgst-rules-2017/rule-010.xml"

    tree = ET.parse(path)
    root = tree.getroot()
    eid_nodes = root.findall(".//*[@eId]")
    eid_nodes[1].attrib["eId"] = eid_nodes[0].attrib["eId"]
    refers_to_nodes = root.findall(".//*[@refersTo]")
    refers_to_nodes[1].attrib["refersTo"] = "/in/union/rules/cgst-rules-2017/rule/999"
    tree.write(path, encoding="utf-8", xml_declaration=True)

    validation = validate_corpus(corpus_dir)

    assert not validation.ok
    assert any("duplicate eId in file" in error for error in validation.errors)
    assert any("local provision ID is outside document hierarchy" in error for error in validation.errors)


def test_corpus_validation_rejects_canonical_path_drift(tmp_path):
    corpus_dir = tmp_path / "corpus"
    sources_dir = tmp_path / "sources"
    seed_from_existing_data(ROOT, corpus_dir=corpus_dir, sources_dir=sources_dir)
    expected = corpus_dir / "in/union/rules/cgst-rules-2017/rule-010.xml"
    drifted = corpus_dir / "in/union/rules/cgst-rules-2017/rule-999.xml"
    expected.rename(drifted)

    validation = validate_corpus(corpus_dir)

    assert not validation.ok
    assert any("canonical_id path mismatch" in error for error in validation.errors)


def test_amendment_plan_and_partial_apply(tmp_path):
    corpus_dir = tmp_path / "corpus"
    sources_dir = tmp_path / "sources"
    seed_from_existing_data(ROOT, corpus_dir=corpus_dir, sources_dir=sources_dir)
    source_dir = sources_dir / "cbic/central-tax/2025/18-2025"

    plan = plan_amendments(source_dir, corpus_dir)

    assert plan.ready_count == 2
    assert plan.unresolved_count == 1
    assert [item.status for item in plan.items] == ["ready", "target_missing", "ready"]

    blocked = apply_amendments(source_dir, corpus_dir, tmp_path / "blocked")
    assert blocked.unresolved_count == 1
    assert not (tmp_path / "blocked").exists()

    output_corpus = tmp_path / "corpus-amended"
    applied = apply_amendments(source_dir, corpus_dir, output_corpus, allow_partial=True)
    assert [item.status for item in applied.items] == ["applied", "target_missing", "applied"]

    new_rule = output_corpus / "in/union/rules/cgst-rules-2017/rule-09a.xml"
    amended_rule = output_corpus / "in/union/rules/cgst-rules-2017/rule-010.xml"
    assert new_rule.exists()
    assert "Grant of registration electronically" in new_rule.read_text(encoding="utf-8")
    assert "under rule 9, rule 9A and rule 14A, a certificate" in amended_rule.read_text(encoding="utf-8")

    validation = validate_corpus(output_corpus)
    assert validation.ok
    assert validation.checked_files == 6
    assert any("/in/union/rules/cgst-rules-2017/rule/14" in warning for warning in validation.warnings)


def test_promote_amended_corpus_requires_approval(tmp_path):
    corpus_dir = tmp_path / "corpus"
    sources_dir = tmp_path / "sources"
    seed_from_existing_data(ROOT, corpus_dir=corpus_dir, sources_dir=sources_dir)
    source_dir = sources_dir / "cbic/central-tax/2025/18-2025"
    output_corpus = tmp_path / "corpus-amended"
    apply_amendments(source_dir, corpus_dir, output_corpus, allow_partial=True)

    manifest = tmp_path / "derived/amendments/promotion_manifest.json"
    dry_run = promote_amended_corpus(output_corpus, corpus_dir, manifest)

    assert dry_run.ok
    assert not dry_run.approved
    assert len(dry_run.added) == 1
    assert len(dry_run.modified) == 1
    assert not corpus_dir.joinpath("in/union/rules/cgst-rules-2017/rule-09a.xml").exists()
    assert manifest.exists()

    approved = promote_amended_corpus(output_corpus, corpus_dir, manifest, approve=True)

    assert approved.ok
    assert approved.approved
    assert corpus_dir.joinpath("in/union/rules/cgst-rules-2017/rule-09a.xml").exists()
    assert "rule 9A and rule 14A" in corpus_dir.joinpath(
        "in/union/rules/cgst-rules-2017/rule-010.xml"
    ).read_text(encoding="utf-8")
    validation = validate_corpus(corpus_dir)
    assert validation.ok
    assert validation.checked_files == 6


def test_corpus_diff_reports_added_and_modified_documents(tmp_path):
    corpus_dir = tmp_path / "corpus"
    sources_dir = tmp_path / "sources"
    seed_from_existing_data(ROOT, corpus_dir=corpus_dir, sources_dir=sources_dir)
    source_dir = sources_dir / "cbic/central-tax/2025/18-2025"
    output_corpus = tmp_path / "corpus-amended"
    apply_amendments(source_dir, corpus_dir, output_corpus, allow_partial=True)

    report = compare_corpora(corpus_dir, output_corpus)

    assert report["stats"] == {"added": 1, "removed": 0, "modified": 1, "unchanged": 4}
    assert report["added"][0]["canonical_id"] == "/in/union/rules/cgst-rules-2017/rule/9a"
    modified = report["modified"][0]
    assert modified["canonical_id"] == "/in/union/rules/cgst-rules-2017/rule/10"
    assert modified["text_changed"]
    assert len(modified["provisions"]["modified"]) == 2
    assert any(
        item["canonical_id"] == "/in/union/rules/cgst-rules-2017/rule/10/subrule/1"
        for item in modified["provisions"]["modified"]
    )
    assert any("approved under rule 9, rule 9A and rule 14A" in line for line in modified["unified_text_diff"])

    output = tmp_path / "derived/diffs/18-2025-corpus-diff.json"
    written = write_diff_report(corpus_dir, output_corpus, output)
    loaded = json.loads(output.read_text(encoding="utf-8"))

    assert output.exists()
    assert loaded == written
    assert loaded["stats"]["added"] == 1


def test_corpus_native_query_and_text_export(tmp_path):
    corpus_dir = tmp_path / "corpus"
    sources_dir = tmp_path / "sources"
    seed_from_existing_data(ROOT, corpus_dir=corpus_dir, sources_dir=sources_dir)

    assert normalize_query_id("CGST_Rules/Rule_10/SubRule_1") == (
        "/in/union/rules/cgst-rules-2017/rule/10/subrule/1"
    )

    documents = list_documents(corpus_dir, document_type="form")
    assert [document["canonical_id"] for document in documents] == ["/in/union/forms/gst-reg-01"]
    assert documents[0]["title"] == "Application for Registration"

    form_entry = lookup_canonical_id(corpus_dir, "/in/union/forms/gst-reg-01")
    assert form_entry is not None
    assert form_entry["roles"] == ["document"]
    assert form_entry["document"]["document_type"] == "form"
    assert "Preliminary Information" in export_text(corpus_dir, "/in/union/forms/gst-reg-01")

    subrule_entry = lookup_canonical_id(corpus_dir, "CGST_Rules/Rule_10/SubRule_1")
    assert subrule_entry is not None
    assert subrule_entry["roles"] == ["provision"]
    assert subrule_entry["provision"]["document_id"] == "/in/union/rules/cgst-rules-2017/rule/10"
    assert "approved under rule 9" in export_text(corpus_dir, "/in/union/rules/cgst-rules-2017/rule/10/subrule/1")

    rule_entry = lookup_canonical_id(corpus_dir, "/in/union/rules/cgst-rules-2017/rule/10")
    assert rule_entry is not None
    assert rule_entry["roles"] == ["document", "provision"]
    assert "Issue of registration certificate" in export_text(
        corpus_dir,
        "/in/union/rules/cgst-rules-2017/rule/10",
        role="document",
    )

    assert lookup_canonical_id(corpus_dir, "/in/union/rules/cgst-rules-2017/rule/999") is None


def test_statute_identity_registry_resolves_cgst_aliases():
    registry_path = ROOT / "data/Law/statute_identity_registry.json"
    registry = load_registry(registry_path)
    validation = validate_registry(registry_path)

    assert validation["ok"], validation["errors"]
    assert registry.resolve_alias("CGST Rules, 2017") == "/in/union/rules/cgst-rules-2017"
    assert registry.resolve_corpus_id("AC_CEN_2_2_00042_201712_1517807328102") == (
        "/in/union/acts/cgst-act-2017"
    )
    diagnostics = registry.diagnostics().to_dict()
    assert "/in/union/acts/cgst-act-2017" in diagnostics["cgst_act_aliases"]
    assert "AC_CEN_2_2_00042_201712_1517807328102" in diagnostics["cgst_act_aliases"]
    assert "/in/union/rules/cgst-rules-2017" in diagnostics["cgst_rules_aliases"]


def _valid_amendment_event():
    return {
        "event_id": "evt_cbic_ct_2025_18_0003",
        "event_type": "TEXTUAL_AMENDMENT",
        "operation": "SPLICE",
        "source": {
            "document_id": "/in/union/notifications/cbic/central-tax/2025/18-2025",
            "record_id": "1010504",
            "instrument_number": "18/2025-Central Tax",
            "issuing_authority": "/in/authority/cbic",
            "publication_date": "2025-10-31",
            "source_url": "sources/cbic/central-tax/2025/18-2025/source.txt",
            "source_file_sha256": "a" * 64,
            "source_text_sha256": "b" * 64,
        },
        "legal_time": {
            "commencement_date": "2025-11-01",
            "applicability_start": "2025-11-01",
            "applicability_end": None,
            "retrospective": False,
            "date_basis": "explicit_commencement_clause",
        },
        "system_time": {
            "observed_at": "2026-06-16T00:00:00Z",
            "compiled_at": "2026-06-16T00:00:00Z",
            "compiler_version": "amendment-events-v1",
        },
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": "/in/union/rules/cgst-rules-2017/rule/10/subrule/1",
            "anchor_text": "under rule 9,",
            "anchor_hash": "c" * 64,
            "anchor_occurrence": 1,
        },
        "payload": {"insert_text": "rule 9A and rule 14A,"},
        "evidence": {
            "source_span": {"start": 794, "end": 914, "text_hash": "d" * 64},
            "excerpt": "short source excerpt",
            "parser_trace": {"pattern_id": "splice_after_words_v1", "confidence": 0.94},
        },
        "validation": {
            "target_resolved": True,
            "anchor_resolved": True,
            "date_resolved": True,
            "source_span_verified": True,
            "materializable": True,
        },
        "status": "validated",
        "review": {"required": False, "review_reasons": [], "reviewed_by": None, "reviewed_at": None},
    }


def test_amendment_event_schema_validates_required_bitemporal_shape():
    schema = json.loads((ROOT / "src/schemas/amendment_event.schema.json").read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    event = _valid_amendment_event()

    validator.validate(event)
    for key in ["legal_time", "system_time"]:
        invalid = dict(event)
        invalid.pop(key)
        with pytest.raises(Exception):
            validator.validate(invalid)

    invalid = json.loads(json.dumps(event))
    invalid["evidence"].pop("source_span")
    with pytest.raises(Exception):
        validator.validate(invalid)

    invalid = json.loads(json.dumps(event))
    invalid["target"].pop("work_id")
    with pytest.raises(Exception):
        validator.validate(invalid)

    with pytest.raises(Exception):
        validator.validate(dict(event, status="applied"))
    with pytest.raises(Exception):
        validator.validate(dict(event, operation="DELETE"))


def _compile_18_2025_fixture(tmp_path):
    corpus_dir = tmp_path / "corpus"
    sources_dir = tmp_path / "sources"
    seed_from_existing_data(ROOT, corpus_dir=corpus_dir, sources_dir=sources_dir)
    source_json_dir = tmp_path / "raw-notifications"
    source_json_dir.mkdir()
    shutil.copyfile(
        ROOT
        / "data/Law/cbic_tax_portal/notifications/18-2025-central-tax-seeks-to-notify-the-central-goods-and-services-tax-fourth-am_1010504.json",
        source_json_dir / "18-2025-central-tax_1010504.json",
    )
    event_path = tmp_path / "derived/version_history/amendment_events.jsonl"
    review_path = tmp_path / "derived/version_history/review_report.json"
    compile_events(
        registry_path=ROOT / "data/Law/statute_identity_registry.json",
        source_dir=source_json_dir,
        category="Central Tax",
        target_work="/in/union/rules/cgst-rules-2017",
        output=event_path,
        review_output=review_path,
        corpus_dir=corpus_dir,
        source_archive_root=sources_dir,
    )
    return corpus_dir, event_path, review_path


def test_amendment_event_compiler_and_review_report_for_18_2025(tmp_path):
    _corpus_dir, event_path, review_path = _compile_18_2025_fixture(tmp_path)
    events = read_events(event_path)

    assert [(event["operation"], event["status"]) for event in events] == [
        ("INSERT_SIBLING", "validated"),
        ("SPLICE", "validated"),
        ("INSERT_SIBLING", "validated"),
        ("UNKNOWN", "needs_review"),
    ]
    assert events[0]["target"]["component_id"] == "/in/union/rules/cgst-rules-2017/rule/9a"
    assert events[0]["event_id"].startswith("evt_cbic_")
    assert events[0]["legacy_event_id"] == "evt_cbic_central_tax_2025_18_0001"
    assert events[1]["target"]["component_id"] == "/in/union/rules/cgst-rules-2017/rule/10/subrule/1"
    assert events[1]["legacy_event_id"] == "evt_cbic_central_tax_2025_18_0002"
    assert events[1]["target"]["anchor_text"] == "under rule 9,"
    assert events[1]["validation"]["source_span_verified"]
    assert events[2]["target"]["component_id"] == "/in/union/rules/cgst-rules-2017/rule/14a"
    assert events[2]["review"]["review_reasons"] == []
    assert "unsupported_form_or_table_mutation" in events[3]["review"]["review_reasons"]

    report = json.loads(review_path.read_text(encoding="utf-8"))
    assert report["counts"]["by_status"] == {"needs_review": 1, "validated": 3}
    assert report["counts"]["by_operation"]["SPLICE"] == 1
    assert report["counts"]["by_year"] == {"2025": 4}
    assert report["counts"]["by_target"] == {"/in/union/rules/cgst-rules-2017": 4}
    assert len(report["non_validated_events"]) == 1
    assert report["non_validated_events"][0]["source_span"]["text_hash"]
    assert report["non_validated_events"][0]["parser_pattern"]


def _write_notification_record(path, no, name, text):
    path.write_text(
        json.dumps(
            {
                "source": "cbic_tax_portal",
                "type": "notification",
                "name": name,
                "no": no,
                "id": int(re.sub(r"\D+", "", no)[:7] or "1"),
                "category": "Central Tax",
                "issueDt": "2025-12-01T05:30:00+05:30",
                "contentText": text,
                "contentPdfBase64": "",
            }
        ),
        encoding="utf-8",
    )


def test_compiler_classifies_substitute_omit_rescind_corrigendum_and_missing_anchor(tmp_path):
    corpus_dir = tmp_path / "corpus"
    sources_dir = tmp_path / "sources"
    seed_from_existing_data(ROOT, corpus_dir=corpus_dir, sources_dir=sources_dir)
    source_json_dir = tmp_path / "raw-notifications"
    source_json_dir.mkdir()
    preamble = (
        "NOTIFICATION\n"
        "1. (1) These rules may be called test rules. "
        "(2) They shall come into force on the 1st day of December, 2025.\n"
    )
    _write_notification_record(
        source_json_dir / "substitute.json",
        "21/2025-Central Tax",
        "Substitute fixture",
        preamble
        + '2. In rule 10, in sub-rule (1), for the words "under rule 9," the words "under rule 9 and rule 9A," shall be substituted.',
    )
    _write_notification_record(
        source_json_dir / "omit.json",
        "22/2025-Central Tax",
        "Omit fixture",
        preamble + "2. In rule 10, sub-rule (3) shall be omitted.",
    )
    _write_notification_record(
        source_json_dir / "rescind.json",
        "23/2025-Central Tax",
        "Rescind fixture",
        preamble + "2. The notification No. 1/2020-Central Tax is hereby rescinded.",
    )
    _write_notification_record(
        source_json_dir / "corrigendum.json",
        "24/2025-Central Tax",
        "Corrigendum to notification",
        "Corrigendum\n1. In the notification, line 2 shall be read as line 3.",
    )
    _write_notification_record(
        source_json_dir / "missing-anchor.json",
        "25/2025-Central Tax",
        "Missing anchor fixture",
        preamble
        + '2. In rule 10, in sub-rule (1), after the words "not present," the words "new words," shall be inserted.',
    )
    _write_notification_record(
        source_json_dir / "unparsed-target-block.json",
        "26/2025-Central Tax",
        "Unparsed CGST Rules amendment fixture",
        preamble
        + "2. In the said rules, in rule 8, in the Explanation, after clause (a), the following clause shall be inserted, namely:- clause text.",
    )

    event_path = tmp_path / "events.jsonl"
    review_path = tmp_path / "review.json"
    compile_events(
        registry_path=ROOT / "data/Law/statute_identity_registry.json",
        source_dir=source_json_dir,
        category="Central Tax",
        target_work="/in/union/rules/cgst-rules-2017",
        output=event_path,
        review_output=review_path,
        corpus_dir=corpus_dir,
        source_archive_root=sources_dir,
    )
    events = read_events(event_path)
    by_operation = {event["operation"]: event for event in events if event["operation"] != "SPLICE"}
    assert by_operation["SUBSTITUTE"]["status"] == "validated"
    assert by_operation["OMIT"]["status"] == "validated"
    assert by_operation["RESCIND"]["status"] == "needs_review"
    assert by_operation["CORRIGENDUM"]["status"] == "needs_review"
    missing_anchor = [event for event in events if event["operation"] == "SPLICE"][0]
    assert missing_anchor["status"] == "needs_review"
    assert "anchor_not_resolved" in missing_anchor["review"]["review_reasons"]
    unparsed = [
        event
        for event in events
        if event["evidence"]["parser_trace"]["pattern_id"] == "unparsed_target_work_amendment_v1"
    ]
    assert unparsed
    assert unparsed[0]["status"] == "needs_review"
    assert "unparsed_target_work_amendment" in unparsed[0]["review"]["review_reasons"]


def test_compiler_handles_rule_level_splice_word_figures_phrase(tmp_path):
    from src.legal_corpus.amendment_events import SourceRecord, compile_events_from_text

    text = (
        "8. In the said rules, in rule 42, in sub-rule (1), in clause (i), "
        "in the Explanation, after the word and figures \u201centry 84\u201d, "
        "the word, figures and letter \u201cand entry 92A\u201d shall be inserted."
    )
    source = SourceRecord(
        record={"id": "1", "no": "1/2019-Central Tax", "category": "Central Tax"},
        json_path=tmp_path / "record.json",
        text=text,
        text_source="contentText",
        source_file_sha256="0" * 64,
        source_text_sha256="1" * 64,
        source_url="",
        document_id="/in/union/notifications/cbic/central-tax/2019/1-2019",
        publication_date="2019-01-29",
        commencement_date="2019-01-29",
        date_basis="fixture",
    )
    lookup = {
        "/in/union/rules/cgst-rules-2017/rule/42": {
            "provision": {"text": "Rule text with entry 84 in the Explanation."}
        }
    }

    events = compile_events_from_text(
        source,
        target_work="/in/union/rules/cgst-rules-2017",
        corpus_lookup=lookup,
    )

    assert len(events) == 1
    assert events[0]["operation"] == "SPLICE"
    assert events[0]["status"] == "validated"
    assert events[0]["target"]["component_id"] == "/in/union/rules/cgst-rules-2017/rule/42"
    assert events[0]["target"]["anchor_text"] == "entry 84"
    assert events[0]["payload"]["insert_text"] == "and entry 92A"


def test_compiler_handles_curly_quote_substitute_with_compound_descriptor(tmp_path):
    from src.legal_corpus.amendment_events import SourceRecord, compile_events_from_text

    text = (
        "2. In the Central Goods and Services Tax Rules, 2017, in rule 117,- "
        "(a) in sub-rule (1A), with effect from the 31st December 2019, "
        "for the figures, letters and word \u201c31st December, 2019\u201d, "
        "the figures, letters and word \u201c31st March, 2020\u201d shall be substituted."
    )
    source = SourceRecord(
        record={"id": "1", "no": "1/2020-Central Tax", "category": "Central Tax"},
        json_path=tmp_path / "record.json",
        text=text,
        text_source="contentText",
        source_file_sha256="0" * 64,
        source_text_sha256="1" * 64,
        source_url="",
        document_id="/in/union/notifications/cbic/central-tax/2020/1-2020",
        publication_date="2020-01-01",
        commencement_date="2020-01-01",
        date_basis="fixture",
    )
    lookup = {
        "/in/union/rules/cgst-rules-2017/rule/117": {
            "provision": {"text": "Rule text with date 31st December, 2019."}
        }
    }

    events = compile_events_from_text(
        source,
        target_work="/in/union/rules/cgst-rules-2017",
        corpus_lookup=lookup,
    )

    assert len(events) == 1
    assert events[0]["operation"] == "SUBSTITUTE"
    assert events[0]["status"] == "validated"
    assert events[0]["target"]["component_id"] == "/in/union/rules/cgst-rules-2017/rule/117"
    assert events[0]["payload"] == {"old_text": "31st December, 2019", "new_text": "31st March, 2020"}


def test_compiler_keeps_rule_target_when_form_text_is_embedded(tmp_path):
    from src.legal_corpus.amendment_events import SourceRecord, compile_events_from_text

    text = (
        "16. In the said rules, in rule 108, in sub-rule (1), – (a) for the words “either electronically or "
        "otherwise as may be notified by the Commissioner”, the word “electronically” shall be substituted; "
        "(b) the following proviso shall be inserted, namely:- “Provided that an appeal to the Appellate Authority "
        "may be filed manually in FORM GST APL-01, along with the relevant documents.”"
    )
    source = SourceRecord(
        record={"id": "1", "no": "38/2023-Central Tax", "category": "Central Tax"},
        json_path=tmp_path / "record.json",
        text=text,
        text_source="contentText",
        source_file_sha256="0" * 64,
        source_text_sha256="1" * 64,
        source_url="",
        document_id="/in/union/notifications/cbic/central-tax/2023/38-2023",
        publication_date="2023-06-07",
        commencement_date="2023-06-07",
        date_basis="fixture",
    )
    lookup = {
        "/in/union/rules/cgst-rules-2017/rule/108": {
            "provision": {"text": "Rule 108 text."}
        },
        "/in/union/rules/cgst-rules-2017/rule/108/subrule/1": {
            "provision": {"text": "Sub-rule (1) text."}
        },
    }

    events = compile_events_from_text(
        source,
        target_work="/in/union/rules/cgst-rules-2017",
        corpus_lookup=lookup,
    )

    operations = [(event["operation"], event["target"]["component_id"]) for event in events]
    assert operations == [
        ("SUBSTITUTE", "/in/union/rules/cgst-rules-2017/rule/108/subrule/1"),
    ] + [("UNKNOWN", "/in/union/rules/cgst-rules-2017/rule/108")]
    assert all("/in/union/forms/" not in event["target"]["component_id"] for event in events)


def test_compiler_does_not_omit_whole_rule_for_proviso_omission(tmp_path):
    from src.legal_corpus.amendment_events import SourceRecord, compile_events_from_text

    text = (
        "4. In the said rules, in rule 8, in sub rule (1),- "
        "(a) the first proviso shall be omitted; "
        "(b) in the second proviso, for the words \u201cProvided further\u201d, "
        "the word \u201cProvided\u201d shall be substituted."
    )
    source = SourceRecord(
        record={"id": "1", "no": "1/2020-Central Tax", "category": "Central Tax"},
        json_path=tmp_path / "record.json",
        text=text,
        text_source="contentText",
        source_file_sha256="0" * 64,
        source_text_sha256="1" * 64,
        source_url="",
        document_id="/in/union/notifications/cbic/central-tax/2020/1-2020",
        publication_date="2020-01-01",
        commencement_date="2020-01-01",
        date_basis="fixture",
    )
    lookup = {
        "/in/union/rules/cgst-rules-2017/rule/8": {
            "provision": {"text": "Rule 8 text with Provided further."}
        }
    }

    events = compile_events_from_text(
        source,
        target_work="/in/union/rules/cgst-rules-2017",
        corpus_lookup=lookup,
    )

    assert events[0]["operation"] != "OMIT"
    assert events[0]["status"] == "needs_review"


def test_compiler_splits_compound_numbered_block_into_stable_subspans(tmp_path):
    from src.legal_corpus.amendment_events import SourceRecord, compile_events_from_text

    text = (
        "2. In the Central Goods and Services Tax Rules, 2017, - "
        "(i) in rule 10, in sub-rule (1), after the words \u201cunder rule 9,\u201d, "
        "the words \u201crule 9A,\u201d shall be inserted; "
        "(ii) in rule 117, for the figures, letters and word \u201c31st March, 2019\u201d, "
        "the figures, letters and word \u201c31st December, 2019\u201d shall be substituted."
    )
    source = SourceRecord(
        record={"id": "1", "no": "1/2020-Central Tax", "category": "Central Tax"},
        json_path=tmp_path / "record.json",
        text=text,
        text_source="contentText",
        source_file_sha256="0" * 64,
        source_text_sha256="1" * 64,
        source_url="",
        document_id="/in/union/notifications/cbic/central-tax/2020/1-2020",
        publication_date="2020-01-01",
        commencement_date="2020-01-01",
        date_basis="fixture",
    )
    lookup = {
        "/in/union/rules/cgst-rules-2017/rule/10/subrule/1": {
            "provision": {"text": "approved under rule 9, a certificate"}
        },
        "/in/union/rules/cgst-rules-2017/rule/117": {
            "provision": {"text": "date 31st March, 2019."}
        },
    }

    events = compile_events_from_text(
        source,
        target_work="/in/union/rules/cgst-rules-2017",
        corpus_lookup=lookup,
    )

    assert [(event["operation"], event["status"]) for event in events] == [
        ("SPLICE", "validated"),
        ("SUBSTITUTE", "validated"),
    ]
    assert events[0]["evidence"]["source_span"]["start"] < events[1]["evidence"]["source_span"]["start"]
    assert events[0]["event_id"] != events[1]["event_id"]
    assert all("compound_block_contains_multiple_amendments" not in event["review"]["review_reasons"] for event in events)


def test_compiler_splits_nested_compound_block_with_rule_context(tmp_path):
    from src.legal_corpus.amendment_events import SourceRecord, compile_events_from_text

    text = (
        "5. In the Central Goods and Services Tax Rules, 2017, in rule 44,- "
        "(i) in sub-rule (2), for the words \u201cold amount\u201d, the words \u201cnew amount\u201d shall be substituted; "
        "(ii) in sub-rule (3), after the words \u201creturn filed\u201d, the words \u201con the portal\u201d shall be inserted."
    )
    source = SourceRecord(
        record={"id": "1", "no": "1/2020-Central Tax", "category": "Central Tax"},
        json_path=tmp_path / "record.json",
        text=text,
        text_source="contentText",
        source_file_sha256="0" * 64,
        source_text_sha256="1" * 64,
        source_url="",
        document_id="/in/union/notifications/cbic/central-tax/2020/1-2020",
        publication_date="2020-01-01",
        commencement_date="2020-01-01",
        date_basis="fixture",
    )
    lookup = {
        "/in/union/rules/cgst-rules-2017/rule/44/subrule/2": {
            "provision": {"text": "This rule mentions old amount."}
        },
        "/in/union/rules/cgst-rules-2017/rule/44/subrule/3": {
            "provision": {"text": "The return filed is available."}
        },
    }

    events = compile_events_from_text(
        source,
        target_work="/in/union/rules/cgst-rules-2017",
        corpus_lookup=lookup,
    )

    assert [(event["operation"], event["target"]["component_id"], event["status"]) for event in events] == [
        ("SUBSTITUTE", "/in/union/rules/cgst-rules-2017/rule/44/subrule/2", "validated"),
        ("SPLICE", "/in/union/rules/cgst-rules-2017/rule/44/subrule/3", "validated"),
    ]
    assert all("compound_block_contains_multiple_amendments" not in event["review"]["review_reasons"] for event in events)
    assert "in rule 44" not in events[0]["evidence"]["excerpt"]


def test_compiler_normalizes_anchor_whitespace_to_materializable_text(tmp_path):
    from src.legal_corpus.amendment_events import SourceRecord, compile_events_from_text

    text = (
        "2. In the said rules, in rule 26, in sub-rule (3), after the words \u201cdigitally\n"
        "signed\u201d, the words \u201cor verified\u201d shall be inserted."
    )
    source = SourceRecord(
        record={"id": "1", "no": "1/2020-Central Tax", "category": "Central Tax"},
        json_path=tmp_path / "record.json",
        text=text,
        text_source="contentText",
        source_file_sha256="0" * 64,
        source_text_sha256="1" * 64,
        source_url="",
        document_id="/in/union/notifications/cbic/central-tax/2020/1-2020",
        publication_date="2020-01-01",
        commencement_date="2020-01-01",
        date_basis="fixture",
    )
    lookup = {
        "/in/union/rules/cgst-rules-2017/rule/26/subrule/3": {
            "provision": {"text": "The application shall be digitally signed by the applicant."}
        }
    }

    [event] = compile_events_from_text(
        source,
        target_work="/in/union/rules/cgst-rules-2017",
        corpus_lookup=lookup,
    )

    assert event["status"] == "validated"
    assert event["target"]["anchor_text"] == "digitally signed"


def test_compiler_collapses_principal_rules_notification_to_baseline_marker(tmp_path):
    from src.legal_corpus.amendment_events import SourceRecord, compile_events_from_text

    text = (
        "In exercise of the powers conferred by section 164 of the Central Goods and Services Tax Act, "
        "the Central Government hereby makes the following rules, namely:-\n"
        "1. Short title, commencement and application.- These rules may be called the Central Goods and "
        "Services Tax Rules, 2017.\n"
        "2. Definitions.- In these rules, unless the context otherwise requires,-\n"
        "3. Officers under the Act.- The Board may appoint officers."
    )
    source = SourceRecord(
        record={
            "id": "1000872",
            "no": "3/2017-Central Tax",
            "category": "Central Tax",
            "name": "Notifying the CGST Rules, 2017 on registration and composition levy",
        },
        json_path=tmp_path / "record.json",
        text=text,
        text_source="contentText",
        source_file_sha256="0" * 64,
        source_text_sha256="1" * 64,
        source_url="",
        document_id="/in/union/notifications/cbic/central-tax/2017/3-2017",
        publication_date="2017-06-19",
        commencement_date="2017-06-19",
        date_basis="fixture",
    )

    events = compile_events_from_text(
        source,
        target_work="/in/union/rules/cgst-rules-2017",
        corpus_lookup={},
    )

    assert len(events) == 1
    [event] = events
    assert event["operation"] == "COMMENCE"
    assert event["status"] == "rejected"
    assert event["payload"]["baseline_source_only"] is True
    assert event["review"]["required"] is False
    assert event["evidence"]["parser_trace"]["pattern_id"] == "principal_rules_baseline_notification_v1"


def test_materializer_does_not_count_principal_baseline_marker_as_gap(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso>
  <act>
    <body>
      <chapter>
        <article refersTo="/in/union/rules/cgst-rules-2017/rule/1">
          <num>1</num>
          <heading>Short title, commencement and application</heading>
          <content><p>These rules may be called the Central Goods and Services Tax Rules, 2017.</p></content>
        </article>
      </chapter>
    </body>
  </act>
</akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-06-19"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")

    event = {
        "event_id": "evt_principal_rules_baseline",
        "event_type": "TEXTUAL_AMENDMENT",
        "operation": "COMMENCE",
        "source": {
            "document_id": "/in/union/notifications/cbic/central-tax/2017/3-2017",
            "record_id": "1000872",
            "instrument_number": "3/2017-Central Tax",
            "issuing_authority": "/in/authority/cbic",
            "publication_date": "2017-06-19",
            "source_url": "",
            "source_file_sha256": "0" * 64,
            "source_text_sha256": "1" * 64,
        },
        "legal_time": {
            "commencement_date": "2017-06-19",
            "applicability_start": "2017-06-19",
            "applicability_end": None,
            "retrospective": False,
            "date_basis": "fixture",
        },
        "system_time": {
            "observed_at": "2026-06-16T00:00:00Z",
            "compiled_at": "2026-06-16T00:00:00Z",
            "compiler_version": "test",
        },
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": "/in/union/rules/cgst-rules-2017",
        },
        "payload": {"baseline_source_only": True},
        "evidence": {
            "source_span": {"start": 0, "end": 10, "text_hash": "3" * 64},
            "excerpt": "Principal CGST Rules notification",
            "parser_trace": {"pattern_id": "fixture", "confidence": 1.0},
        },
        "validation": {
            "target_resolved": True,
            "anchor_resolved": True,
            "date_resolved": True,
            "source_span_verified": True,
            "materializable": False,
        },
        "status": "rejected",
        "review": {"required": False, "review_reasons": []},
    }
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(json.dumps(event) + "\n", encoding="utf-8")

    out = tmp_path / "versions"
    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "missing-corpus",
        output_dir=out,
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["applied_count"] == 0
    assert manifest["coverage_gap_count"] == 0


def test_materializer_and_compare_use_event_sourced_node_versions(tmp_path):
    corpus_dir, event_path, _review_path = _compile_18_2025_fixture(tmp_path)
    rule14_path = corpus_dir / "in/union/rules/cgst-rules-2017/rule-014.xml"
    rule14_path.write_text(
        """
<akomaNtoso>
  <doc>
    <body>
      <article refersTo="/in/union/rules/cgst-rules-2017/rule/14">
        <num>14</num>
        <heading>Grant of registration to a person supplying online information and database access or retrieval services from a place outside India</heading>
        <content><p>Rule 14 baseline text.</p></content>
      </article>
    </body>
  </doc>
</akomaNtoso>
""",
        encoding="utf-8",
    )
    output_dir = tmp_path / "derived/version_history/cgst-rules-2017"
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work.pop("baseline_path", None)
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")
    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=event_path,
        registry_path=registry_path,
        corpus_dir=corpus_dir,
        output_dir=output_dir,
        refresh_baseline=False,
    )

    assert manifest["applied_count"] == 3
    assert manifest["coverage_gap_count"] == 1
    snapshot = output_dir / "snapshots/2025-11-01/in/union/rules/cgst-rules-2017/rule-010.xml"
    assert "under rule 9, rule 9A and rule 14A, a certificate" in snapshot.read_text(encoding="utf-8")
    assert (output_dir / "snapshots/2025-11-01/in/union/rules/cgst-rules-2017/rule-09a.xml").exists()
    assert (output_dir / "snapshots/2025-11-01/in/union/rules/cgst-rules-2017/rule-14a.xml").exists()

    rows = [json.loads(line) for line in (output_dir / "node_versions.jsonl").read_text(encoding="utf-8").splitlines()]
    versions_by_component = {}
    for row in rows:
        versions_by_component.setdefault(row["component_id"], []).append(row)
    assert len(versions_by_component["/in/union/rules/cgst-rules-2017/rule/10/subrule/1"]) == 2
    assert len(versions_by_component["/in/union/rules/cgst-rules-2017/rule/8"]) == 1

    gaps = json.loads((output_dir / "coverage_gaps.json").read_text(encoding="utf-8"))
    assert {gap["event_id"] for gap in gaps["gaps"]} == {"evt_cbic_73bc8157337c7c45"}

    comparison = compare_component_versions(
        "/in/union/rules/cgst-rules-2017/rule/10/subrule/1",
        from_date="2025-10-31",
        to_date="2025-11-01",
        version_dir=output_dir,
    )
    assert comparison["status"] == "ok"
    assert comparison["coverage"] == "incomplete"
    assert comparison["text_changed"]
    assert comparison["events_between"][0]["event_id"] == "evt_cbic_eb14f791423994d9"
    assert comparison["events_between"][0]["source_span"]["text_hash"]
    assert "rule 9A and rule 14A" in comparison["unified_diff"]
    assert comparison["coverage_gaps"]

    partial = compare_component_versions(
        "/in/union/rules/cgst-rules-2017/rule/10/subrule/1",
        from_date="2017-01-01",
        to_date="2025-11-01",
        version_dir=output_dir,
    )
    assert partial["status"] == "partial_history"


def test_materializer_applies_missing_child_text_edit_to_parent_when_anchor_matches(tmp_path):
    corpus_dir = tmp_path / "corpus"
    rule_path = corpus_dir / "in/union/rules/cgst-rules-2017/rule-021a.xml"
    rule_path.parent.mkdir(parents=True)
    rule_path.write_text(
        """
<akomaNtoso>
  <doc>
    <body>
      <article refersTo="/in/union/rules/cgst-rules-2017/rule/21a">
        <num>21A</num>
        <heading>Suspension of registration</heading>
        <content><p>(3) the show cause issued under sub- rule (1) shall be handled. (4) reply furnished under sub-rule (2) shall be handled.</p></content>
      </article>
    </body>
  </doc>
</akomaNtoso>
""",
        encoding="utf-8",
    )
    event = {
        "event_id": "evt_parent_fallback_splice",
        "operation": "SPLICE",
        "status": "validated",
        "legal_time": {
            "applicability_start": "2020-12-22",
            "commencement_date": "2020-12-22",
        },
        "source": {"publication_date": "2020-12-22", "document_id": "fixture"},
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": "/in/union/rules/cgst-rules-2017/rule/21a/subrule/3",
            "anchor_text": "the show cause issued under sub-rule (1)",
        },
        "payload": {"insert_text": " or under sub-rule (2A) of rule 21A", "position": "after"},
        "evidence": {
            "source_span": {"start": 0, "end": 1, "text_hash": "1" * 64},
            "excerpt": "fixture",
        },
        "validation": {
            "target_resolved": True,
            "anchor_resolved": True,
            "date_resolved": True,
            "source_span_verified": True,
            "materializable": True,
        },
        "review": {"required": False, "review_reasons": []},
    }
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(json.dumps(event) + "\n", encoding="utf-8")
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work.pop("baseline_path", None)
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")

    out = tmp_path / "versions"
    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=corpus_dir,
        output_dir=out,
        refresh_baseline=False,
        write_snapshots=False,
    )

    assert manifest["applied_count"] == 1
    assert manifest["coverage_gap_count"] == 0
    rows = [json.loads(line) for line in (out / "node_versions.jsonl").read_text(encoding="utf-8").splitlines()]
    parent = [row for row in rows if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/21a"][-1]
    child = [row for row in rows if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/21a/subrule/3"][-1]
    assert "the show cause issued under sub- rule (1) or under sub-rule (2A) of rule 21A" in parent["text"]
    assert "or under sub-rule (2A) of rule 21A" in child["text"]


def test_materializer_replaces_baseline_contaminated_inserted_sibling(tmp_path):
    corpus_dir = tmp_path / "corpus"
    base_dir = corpus_dir / "in/union/rules/cgst-rules-2017"
    base_dir.mkdir(parents=True)
    (base_dir / "rule-031.xml").write_text(
        """
<akomaNtoso><doc><body><article refersTo="/in/union/rules/cgst-rules-2017/rule/31"><num>31</num><heading>Residual method</heading><content><p>Rule 31 baseline.</p></content></article></body></doc></akomaNtoso>
""",
        encoding="utf-8",
    )
    (base_dir / "rule-031a.xml").write_text(
        """
<akomaNtoso><doc><body><article refersTo="/in/union/rules/cgst-rules-2017/rule/31a"><num>31A</num><heading>Later contaminated text</heading><content><p>Later contaminated version.</p></content></article></body></doc></akomaNtoso>
""",
        encoding="utf-8",
    )
    event = {
        "event_id": "evt_insert_31a",
        "operation": "INSERT_SIBLING",
        "status": "validated",
        "legal_time": {"applicability_start": "2018-01-23", "commencement_date": "2018-01-23"},
        "source": {"publication_date": "2018-01-23", "document_id": "fixture"},
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": "/in/union/rules/cgst-rules-2017/rule/31a",
            "anchor_component_id": "/in/union/rules/cgst-rules-2017/rule/31",
        },
        "payload": {
            "node_type": "rule",
            "label": "31A",
            "heading": "Value of supply in case of lottery",
            "content": "Initial inserted rule text.",
        },
        "evidence": {"source_span": {"start": 0, "end": 1, "text_hash": "2" * 64}, "excerpt": "after rule 31"},
        "validation": {
            "target_resolved": True,
            "anchor_resolved": True,
            "date_resolved": True,
            "source_span_verified": True,
            "materializable": True,
        },
        "review": {"required": False, "review_reasons": []},
    }
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(json.dumps(event) + "\n", encoding="utf-8")
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work.pop("baseline_path", None)
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")

    out = tmp_path / "versions"
    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=corpus_dir,
        output_dir=out,
        refresh_baseline=False,
        write_snapshots=False,
    )

    assert manifest["applied_count"] == 1
    rows = [json.loads(line) for line in (out / "node_versions.jsonl").read_text(encoding="utf-8").splitlines()]
    rule31a = [row for row in rows if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/31a"]
    assert len(rule31a) == 2
    assert rule31a[0]["created_by_event_id"] is None
    assert rule31a[1]["created_by_event_id"] == "evt_insert_31a"
    assert "Initial inserted rule text" in rule31a[1]["text"]


def test_materializer_repairs_rule_31a_complete_2018_insertion(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso><act><body>
<article refersTo="/in/union/rules/cgst-rules-2017/rule/31">
<num>31</num><heading>Residual method</heading><content><p>Rule 31 baseline.</p></content>
</article>
</body></act></akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-06-19"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")
    event = {
        "event_id": "evt_cbic_7a6dd14dc9007d76",
        "operation": "INSERT_SIBLING",
        "status": "validated",
        "source": {
            "document_id": "/in/union/notifications/cbic/central-tax/2018/3-2018",
            "publication_date": "2018-01-23",
            "record_id": "1000795",
        },
        "legal_time": {"applicability_start": "2018-01-23"},
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": "/in/union/rules/cgst-rules-2017/rule/31a",
            "anchor_component_id": "/in/union/rules/cgst-rules-2017/rule/31",
        },
        "payload": {
            "anchor_rule": "31",
            "content": "(1) truncated (2) (a) The value of supply of lottery run by State Governments",
            "heading": "Value of supply in case of lottery, betting, gambling and horse racing",
            "label": "31A",
            "node_type": "rule",
            "position": "after",
        },
        "evidence": {"source_span": {"start": 1740, "end": 2843, "text_hash": "rule31a-2018"}},
        "review": {"required": False, "review_reasons": []},
        "validation": {"anchor_resolved": True, "materializable": True, "target_resolved": True},
    }
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(json.dumps(event) + "\n", encoding="utf-8")

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["coverage_gap_count"] == 0
    assert manifest["applied_count"] == 1
    rows = [
        json.loads(line)
        for line in (tmp_path / "out" / "node_versions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    rule31a = [row for row in rows if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/31a"][-1]
    assert rule31a["created_by_event_id"] == "evt_cbic_7a6dd14dc9007d76"
    assert '"lottery authorised by State Governments" means a lottery which is authorised' in rule31a["text"]
    assert '"Organising State" has the same meaning' in rule31a["text"]
    assert "amount paid into the totalisator" in rule31a["text"]


def test_materializer_repairs_rule_31a_clean_2020_subrule2_substitution(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso><act><body>
<article refersTo="/in/union/rules/cgst-rules-2017/rule/31a">
<num>31A</num><heading>Value of supply in case of lottery, betting, gambling and horse racing</heading>
<content><p>31A. Value of supply in case of lottery, betting, gambling and horse racing. (1) Notwithstanding anything contained in the provisions of this Chapter, the value in respect of supplies specified below shall be determined in the manner provided hereinafter. (2) (a) Old lottery text. (b) Old authorised lottery text. Explanation:- old explanation. (3) The value of supply of actionable claim in the form of chance to win in betting, gambling or horse racing in a race club shall be 100% of the face value of the bet or the amount paid into the totalisator.</p></content>
</article>
</body></act></akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-06-19"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")
    event = {
        "event_id": "evt_cbic_5bde6512dd0562b2",
        "operation": "SUBSTITUTE",
        "status": "validated",
        "source": {
            "document_id": "/in/union/notifications/cbic/central-tax/2020/8-2020",
            "publication_date": "2020-03-02",
            "record_id": "1000628",
        },
        "legal_time": {"applicability_start": "2020-03-02"},
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": "/in/union/rules/cgst-rules-2017/rule/31a",
            "anchor_text": "sub-rule (2)",
        },
        "payload": {
            "structural_text": "- \"(2) The value of supply of lottery shall be deemed... [F. No. note]",
        },
        "evidence": {"source_span": {"start": 801, "end": 1848, "text_hash": "rule31a-2020"}},
        "review": {"required": False, "review_reasons": []},
        "validation": {"anchor_resolved": True, "materializable": True, "target_resolved": True},
    }
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(json.dumps(event) + "\n", encoding="utf-8")

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["coverage_gap_count"] == 0
    assert manifest["applied_count"] == 1
    rows = [
        json.loads(line)
        for line in (tmp_path / "out" / "node_versions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    rule31a = [row for row in rows if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/31a"][-1]
    assert rule31a["created_by_event_id"] == "evt_cbic_5bde6512dd0562b2"
    assert "(1) Notwithstanding anything contained in the provisions of this Chapter" in rule31a["text"]
    assert "100/128 of the face value of ticket" in rule31a["text"]
    assert "amount paid into the totalisator" in rule31a["text"]
    assert "F. No." not in rule31a["text"]
    assert "Director, Government of India" not in rule31a["text"]


def test_materializer_splits_rule_129_130_antiprofiteering_compound_substitution(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso><act><body>
<article refersTo="/in/union/rules/cgst-rules-2017/rule/129">
<num>129</num><heading>Initiation and conduct of proceedings</heading>
<content><p>129. The Director General of Safeguards shall conduct the proceedings. The Director General of Safeguards may seek information.</p></content>
</article>
<article refersTo="/in/union/rules/cgst-rules-2017/rule/130">
<num>130</num><heading>Confidentiality of information</heading>
<content><p>130. Confidentiality. (2) The Director General of Safeguards shall keep information confidential and the Director General of Safeguards may disclose it only as permitted.</p></content>
</article>
<paragraph refersTo="/in/union/rules/cgst-rules-2017/rule/130/subrule/2">
<num>(2)</num><content><p>(2) The Director General of Safeguards shall keep information confidential and the Director General of Safeguards may disclose it only as permitted.</p></content>
</paragraph>
</body></act></akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-06-19"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")
    event = {
        "event_id": "evt_cbic_f89d802d02978dc1",
        "operation": "SUBSTITUTE",
        "status": "needs_review",
        "source": {
            "document_id": "/in/union/notifications/cbic/central-tax/2018/29-2018",
            "publication_date": "2018-06-12",
            "record_id": "1000769",
        },
        "legal_time": {"applicability_start": "2018-06-12"},
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": "/in/union/rules/cgst-rules-2017/rule/130/subrule/2",
            "anchor_text": "Director General of Safeguards",
        },
        "payload": {
            "old_text": "Director General of Safeguards",
            "new_text": "Director General of Anti-profiteering",
        },
        "evidence": {
            "excerpt": (
                "(ii) in rule 129, for the words \"Director General of Safeguards\", "
                "wherever they occur, the words \"Director General of Anti-profiteering\" "
                "shall be substituted; (iii) in rule 130, in sub-rule (2), for the words "
                "\"Director General of Safeguards\", at both places where they occur, the words "
                "\"Director General of Anti-profiteering\" shall be substituted;"
            ),
            "source_span": {"start": 919, "end": 1265, "text_hash": "rule129-130"},
        },
        "review": {
            "required": True,
            "review_reasons": [
                "compound_block_contains_multiple_amendments",
                "context_recovered_target_pending_validation",
            ],
        },
        "validation": {"anchor_resolved": True, "materializable": False, "target_resolved": True},
    }
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(json.dumps(event) + "\n", encoding="utf-8")

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["coverage_gap_count"] == 0
    assert manifest["applied_count"] == 2
    rows = [
        json.loads(line)
        for line in (tmp_path / "out" / "node_versions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    rule129 = [row for row in rows if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/129"][-1]
    rule130 = [row for row in rows if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/130"][-1]
    assert "Director General of Safeguards" not in rule129["text"]
    assert rule129["text"].count("Director General of Anti-profiteering") == 2
    assert "Director General of Safeguards" not in rule130["text"]
    assert rule130["text"].count("Director General of Anti-profiteering") == 2


def test_materializer_retargets_rule_119_time_limit_substitution(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso><act><body>
<article refersTo="/in/union/rules/cgst-rules-2017/rule/119">
<num>119</num><heading>Declaration of stock held by a principal and agent</heading>
<content><p>119. Declaration of stock held by a principal and agent.- Every person to whom the provisions of section 141 apply shall, within ninety days of the appointed day, submit a declaration electronically in FORM GST TRAN-1.</p></content>
</article>
<article refersTo="/in/union/rules/cgst-rules-2017/rule/117">
<num>117</num><heading>Tax or duty credit carried forward</heading>
<content><p>117. Tax or duty credit carried forward.- A registered person may submit a declaration.</p></content>
</article>
</body></act></akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-06-19"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")
    common = {
        "event_type": "TEXTUAL_AMENDMENT",
        "operation": "SUBSTITUTE",
        "status": "validated",
        "source": {
            "document_id": "/in/union/notifications/cbic/central-tax/2017/36-2017",
            "publication_date": "2017-09-29",
            "record_id": "1000837",
        },
        "legal_time": {"applicability_start": "2017-09-29"},
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": "/in/union/rules/cgst-rules-2017/rule/117",
            "anchor_text": "ninety days of the appointed day",
        },
        "payload": {
            "old_text": "ninety days of the appointed day",
            "new_text": "the period specified in rule 117 or such further period as extended by the Commissioner",
            "context_recovered_target": True,
        },
        "evidence": {
            "excerpt": (
                "(iii) in rule 119, for the words \"ninety days of the appointed day\", "
                "the words and figures \"the period specified in rule 117 or such further "
                "period as extended by the Commissioner\" shall be substituted;"
            ),
            "source_span": {"start": 1041, "end": 1243, "text_hash": "rule119-time-limit"},
        },
        "validation": {"anchor_resolved": True, "materializable": True, "target_resolved": True},
    }
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(json.dumps({**common, "event_id": "evt_cbic_92944a08390351bb"}) + "\n", encoding="utf-8")

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["coverage_gap_count"] == 0
    rows = [
        json.loads(line)
        for line in (tmp_path / "out" / "node_versions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    rule119 = [row for row in rows if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/119"][-1]
    rule117 = [row for row in rows if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/117"][-1]
    assert "within the period specified in rule 117 or such further period as extended by the Commissioner" in rule119["text"]
    assert "ninety days of the appointed day" not in rule119["text"]
    assert "the period specified in rule 117" not in rule117["text"]


def test_materializer_repairs_rule_120a_insertion_and_marginal_heading(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso><act><body>
<article refersTo="/in/union/rules/cgst-rules-2017/rule/120">
<num>120</num><heading>Details of goods sent on approval basis</heading>
<content><p>120. Details of goods sent on approval basis.</p></content>
</article>
<article refersTo="/in/union/rules/cgst-rules-2017/rule/120a">
<num>120A</num><heading></heading>
<content><p>within the time period specified in rule 117, rule 118, rule 119 and rule 120 may revise such declaration once and submit the revised declaration in FORM GST TRAN-1electronically on the common portal within the time period specified in the said rules or such further period as may be extended by the Commissioner in this behalf.</p></content>
</article>
</body></act></akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-06-19"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")
    common = {
        "event_type": "TEXTUAL_AMENDMENT",
        "status": "needs_review",
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": "/in/union/forms/gst-tran-1",
        },
        "review": {"required": True, "review_reasons": ["forms_lane_pending_baseline"]},
        "validation": {"materializable": False},
    }
    events = [
        {
            **common,
            "event_id": "evt_cbic_7a9ce35c5a1275d5",
            "operation": "INSERT_SIBLING",
            "source": {
                "document_id": "/in/union/notifications/cbic/central-tax/2017/34-2017",
                "publication_date": "2017-09-15",
                "record_id": "1000839",
            },
            "legal_time": {"applicability_start": "2017-09-15"},
            "target": {
                "work_id": "/in/union/rules/cgst-rules-2017",
                "component_id": "/in/union/rules/cgst-rules-2017/rule/120a",
                "anchor_component_id": "/in/union/rules/cgst-rules-2017/rule/120",
            },
            "payload": {
                "label": "120A",
                "content": "within the time period specified in rule 117, rule 118, rule 119 and rule 120 may revise such declaration once",
                "triage_lane": "forms_lane_pending_baseline",
                "forms_lane_pending_baseline": True,
            },
            "evidence": {
                "excerpt": "after rule 120, the following rule shall be inserted, namely:- \"120A. Every registered person who has submitted a declaration electronically in FORM GST TRAN-1 ...",
                "source_span": {"start": 1904, "end": 2423, "text_hash": "rule120a-insert"},
            },
        },
        {
            **common,
            "event_id": "evt_cbic_2bba0f5762979321",
            "operation": "UNKNOWN",
            "source": {
                "document_id": "/in/union/notifications/cbic/central-tax/2017/36-2017",
                "publication_date": "2017-09-29",
                "record_id": "1000837",
            },
            "legal_time": {"applicability_start": "2017-09-29"},
            "payload": {
                "text": "(v) in rule 120A, the marginal heading \"Revision of declaration in FORM GST TRAN-1\" shall be inserted;",
                "triage_lane": "forms_lane_pending_baseline",
                "forms_lane_pending_baseline": True,
            },
            "evidence": {
                "excerpt": "(v) in rule 120A, the marginal heading \"Revision of declaration in FORM GST TRAN-1\" shall be inserted;",
                "source_span": {"start": 1446, "end": 1548, "text_hash": "rule120a-heading"},
            },
        },
    ]
    events_path = tmp_path / "events.jsonl"
    events_path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["coverage_gap_count"] == 0
    assert manifest["applied_count"] == 2
    rows = [
        json.loads(line)
        for line in (tmp_path / "out" / "node_versions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    rule120a = [row for row in rows if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/120a"][-1]
    assert "Rule 120A. Revision of declaration in FORM GST TRAN-1" in rule120a["text"]
    assert "Every registered person who has submitted a declaration electronically in FORM GST TRAN-1" in rule120a["text"]
    assert "FORM GST TRAN-1 electronically on the common portal" in rule120a["text"]


def test_materializer_applies_validated_forms_lane_blocked_rule_amendment(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso><act><body>
<article refersTo="/in/union/rules/cgst-rules-2017/rule/100">
<num>100</num><heading>Resolved rules</heading><content><p>100. The taxable value shall include integrated tax and central tax.</p></content>
</article>
</body></act></akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-06-19"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")

    event = {
        "event_id": "evt_cbic_validated_forms_lane_unblock",
        "operation": "SUBSTITUTE",
        "status": "validated",
        "source": {
            "document_id": "/in/union/notifications/cbic/central-tax/2017/15-2017",
            "publication_date": "2017-07-01",
            "record_id": "1000001",
        },
        "legal_time": {"applicability_start": "2017-07-01"},
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": "/in/union/rules/cgst-rules-2017/rule/100",
            "anchor_text": "integrated tax and central tax",
        },
        "payload": {
            "old_text": "integrated tax and central tax",
            "new_text": "integrated tax and GST",
            "triage_lane": "forms_lane_pending_baseline",
            "forms_lane_pending_baseline": True,
        },
        "evidence": {"source_span": {"start": 100, "end": 300, "text_hash": "rule-100-tax"}},
        "review": {"required": False, "review_reasons": []},
    }
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(json.dumps(event) + "\n", encoding="utf-8")

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    versions = [
        json.loads(line)
        for line in (tmp_path / "out" / "node_versions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    rule_version = [row for row in versions if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/100"][-1]

    assert manifest["coverage_gap_count"] == 0
    assert manifest["applied_count"] == 1
    assert "integrated tax and GST" in rule_version["text"]


def test_materializer_repairs_rule_49_electronic_bill_provisos(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso><act><body>
<article refersTo="/in/union/rules/cgst-rules-2017/rule/49">
<num>49</num><heading>Bill of supply</heading>
<content><p>49. Bill of supply.- A bill of supply referred to in clause (c) of sub-section (3) of section 31 shall be issued by the supplier containing the following details, namely,- (h) signature or digital signature of the supplier or his authorised representative: Provided that the provisos to rule 46 shall, mutatis mutandis, apply to the bill of supply issued under this rule: Provided further that any tax invoice or any other similar document issued under any other Act for the time being in force in respect of any non-taxable supply shall be treated as a bill of supply for the purposes of the Act.</p></content>
</article>
</body></act></akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-06-19"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")
    signature_event = {
        "event_id": "evt_cbic_b3d6c5e7c8d7f87d",
        "event_type": "TEXTUAL_AMENDMENT",
        "operation": "INSERT_CHILD",
        "status": "needs_review",
        "source": {
            "document_id": "/in/union/notifications/cbic/central-tax/2018/74-2018",
            "publication_date": "2018-12-31",
            "record_id": "1000712",
        },
        "legal_time": {"applicability_start": "2018-12-31"},
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": "/in/union/rules/cgst-rules-2017/rule/49/proviso/e-bill-signature-2019",
            "anchor_component_id": "/in/union/rules/cgst-rules-2017/rule/49",
            "anchor_text": "after the second proviso",
        },
        "payload": {
            "label": "proviso",
            "node_type": "proviso",
            "content": "Provided that...",
        },
        "evidence": {
            "excerpt": (
                "In the said rules, in rule 49, after the second proviso, the following "
                "proviso shall be inserted, namely:- Provided also that the signature or "
                "digital signature of the supplier or his authorised representative shall "
                "not be required in the case of issuance of an electronic bill of supply "
                "in accordance with the provisions of the Information Technology Act, 2000 "
                "(21 of 2000)."
            ),
            "source_span": {"start": 1, "end": 2, "text_hash": "rule49-e-bill-signature"},
        },
        "review": {"required": True, "review_reasons": ["payload_placeholder"]},
        "validation": {"anchor_resolved": True, "materializable": False, "target_resolved": True},
    }
    qr_event = {
        **signature_event,
        "event_id": "evt_cbic_49c35f127ce65025",
        "source": {
            "document_id": "/in/union/notifications/cbic/central-tax/2019/31-2019",
            "publication_date": "2019-06-28",
            "record_id": "1000684",
        },
        "legal_time": {"applicability_start": "2019-06-28"},
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": "/in/union/rules/cgst-rules-2017/rule/49/clause/qr",
            "anchor_component_id": "/in/union/rules/cgst-rules-2017",
        },
        "payload": {
            "label": "QR",
            "node_type": "clause",
            "content": (
                "- \"Provided also that the Government may, by notification, on the "
                "recommendations of the Council, and subject to such conditions and "
                "restrictions as mentioned therein, specify that the bill of supply "
                "shall have Quick Response (QR) code.\"."
            ),
        },
        "evidence": {
            "excerpt": (
                "In the said rules, in rule 49, after the third proviso, with effect "
                "from a date to be notified later, the following proviso shall be "
                "inserted, namely:- \"Provided also that the Government may, by "
                "notification, on the recommendations of the Council, and subject to "
                "such conditions and restrictions as mentioned therein, specify that "
                "the bill of supply shall have Quick Response (QR) code.\"."
            ),
            "source_span": {"start": 3, "end": 4, "text_hash": "rule49-qr-code"},
        },
    }
    events_path = tmp_path / "events.jsonl"
    events_path.write_text("\n".join(json.dumps(event) for event in [signature_event, qr_event]) + "\n", encoding="utf-8")

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["coverage_gap_count"] == 0
    rows = [
        json.loads(line)
        for line in (tmp_path / "out" / "node_versions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    rule49 = [row for row in rows if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/49"][-1]
    assert manifest["applied_count"] == 2
    assert "Provided that..." not in rule49["text"]
    assert "/clause/qr" not in "\n".join(row["component_id"] for row in rows)
    assert "issuance of an electronic bill of supply" in rule49["text"]
    assert "Information Technology Act, 2000 (21 of 2000)" in rule49["text"]
    assert "shall have Quick Response (QR) code." in rule49["text"]


def test_materializer_repairs_rule_94_renumbered_subrule_insertion(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso><act><body>
<article refersTo="/in/union/rules/cgst-rules-2017/rule/94">
<num>94</num><heading>Order sanctioning interest on delayed refunds</heading>
<content><p>94. Order sanctioning interest on delayed refunds.- Where any interest is due and payable to the applicant under section 56, the proper officer shall make an order along with a payment order in FORM GST RFD-05, specifying therein the amount of refund which is delayed, the period of delay for which interest is payable and the amount of interest payable, and such amount of interest shall be electronically credited to any of the bank accounts of the applicant mentioned in his registration particulars and as specified in the application for refund.</p></content>
</article>
</body></act></akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-06-19"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")
    event = {
        "event_id": "evt_cbic_deb8b1120845cc27",
        "event_type": "TEXTUAL_AMENDMENT",
        "operation": "INSERT_CHILD",
        "status": "needs_review",
        "source": {
            "document_id": "/in/union/notifications/cbic/central-tax/2023/38-2023",
            "publication_date": "2023-08-04",
            "record_id": "1009820",
        },
        "legal_time": {"applicability_start": "2023-10-01"},
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": "/in/union/rules/cgst-rules-2017/rule/94/subrule/(2)",
            "anchor_component_id": "/in/union/rules/cgst/94/1",
            "anchor_text": (
                "rule 94 shall, with effect from the 1st day of October, 2023, be "
                "renumbered as sub-rule (1) and after the sub-rule as so renumbered"
            ),
        },
        "payload": {
            "label": "(2)",
            "node_type": "sub-rule",
            "parent_component_id": "/in/union/rules/cgst-rules-2017/rule/94",
            "content": (
                "The following periods shall not be included in the period of delay "
                "under sub- rule (1), namely:- (a) any period of time beyond fifteen "
                "days of receipt of notice in FORM GST RFD- 08 under sub-rule (3) of "
                "rule 92, that the applicant takes to- (i) furnish a reply in FORM GST RFD-09"
            ),
            "forms_lane_pending_baseline": True,
            "triage_lane": "forms_lane_pending_baseline",
        },
        "evidence": {
            "excerpt": (
                "In the said rules, rule 94 shall, with effect from the 1st day of "
                "October, 2023, be renumbered as sub-rule (1) and after the sub-rule "
                "as so renumbered, the following sub-rule shall be inserted, namely:- "
                "\"(2) The following periods shall not be included in the period of delay "
                "under sub-rule (1), namely:- (a) any period of time beyond fifteen days "
                "of receipt of notice in FORM GST RFD-08 under sub-rule (3) of rule 92..."
            ),
            "source_span": {"start": 11092, "end": 11955, "text_hash": "rule94-subrule2"},
        },
        "review": {"required": True, "review_reasons": ["anchor_not_resolved", "forms_lane_pending_baseline"]},
        "validation": {"anchor_resolved": False, "materializable": False, "target_resolved": False},
    }
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(json.dumps(event) + "\n", encoding="utf-8")

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["coverage_gap_count"] == 0
    rows = [
        json.loads(line)
        for line in (tmp_path / "out" / "node_versions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    rule94 = [row for row in rows if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/94"][-1]
    subrule2 = [row for row in rows if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/94/subrule/2"][-1]
    assert "(1) Where any interest is due and payable to the applicant under section 56" in rule94["text"]
    assert "any period of time beyond fifteen days of receipt of notice in FORM GST RFD-08" in rule94["text"]
    assert "furnishing the correct details of the bank account" in rule94["text"]
    assert "where the amount of refund sanctioned could not be credited" in subrule2["text"]


def test_materializer_applies_unique_normalized_text_edits(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso>
  <act>
    <body>
      <chapter>
        <article refersTo="/in/union/rules/cgst-rules-2017/rule/137">
          <num>137</num>
          <heading>Tenure of Authority</heading>
          <content><p>The Authority shall cease to exist after the expiry of four
years from the date on which the Chairman enters upon his office.</p></content>
        </article>
        <article refersTo="/in/union/rules/cgst-rules-2017/rule/43">
          <num>43</num>
          <heading>Capital goods credit</heading>
          <content><p>FORM GSTR-2 and FORM GSTR-3B shall be checked; FORM GSTR-2 and FORM GSTR-3B shall be archived.</p></content>
        </article>
      </chapter>
    </body>
  </act>
</akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-06-19"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")

    def event(event_id, operation, component_id, payload, excerpt):
        return {
            "event_id": event_id,
            "event_type": "TEXTUAL_AMENDMENT",
            "operation": operation,
            "source": {
                "document_id": f"/test/{event_id}",
                "record_id": event_id,
                "instrument_number": event_id,
                "issuing_authority": "/in/authority/cbic",
                "publication_date": "2021-12-01",
                "source_url": "",
                "source_file_sha256": "0" * 64,
                "source_text_sha256": "1" * 64,
            },
            "legal_time": {
                "commencement_date": "2021-12-01",
                "applicability_start": "2021-12-01",
                "applicability_end": None,
                "retrospective": False,
                "date_basis": "fixture",
            },
            "system_time": {
                "observed_at": "2026-06-16T00:00:00Z",
                "compiled_at": "2026-06-16T00:00:00Z",
                "compiler_version": "test",
            },
            "target": {
                "work_id": "/in/union/rules/cgst-rules-2017",
                "component_id": component_id,
                "anchor_text": payload.get("old_text") or payload.get("omit_text"),
                "anchor_occurrence": 1,
            },
            "payload": payload,
            "evidence": {
                "source_span": {"start": 0, "end": 10, "text_hash": "3" * 64},
                "excerpt": excerpt,
                "parser_trace": {"pattern_id": "fixture", "confidence": 1.0},
            },
            "validation": {
                "target_resolved": True,
                "anchor_resolved": True,
                "date_resolved": True,
                "source_span_verified": True,
                "materializable": True,
            },
            "status": "validated",
            "review": {"required": False, "review_reasons": []},
        }

    events = [
        event(
            "evt_line_wrapped_substitute",
            "SUBSTITUTE",
            "/in/union/rules/cgst-rules-2017/rule/137",
            {"old_text": "four years", "new_text": "five years"},
            "for the words “four years”, the words “five years” shall be substituted",
        ),
        event(
            "evt_both_places_omit",
            "OMIT",
            "/in/union/rules/cgst-rules-2017/rule/43",
            {"omit_text": "FORM GSTR-2 and", "whole_component": False},
            "the words, letters and figure, “FORM GSTR-2 and” at both the places where they occur, shall be omitted",
        ),
    ]
    events_path = tmp_path / "events.jsonl"
    events_path.write_text("\n".join(json.dumps(row) for row in events) + "\n", encoding="utf-8")

    out = tmp_path / "versions"
    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "missing-corpus",
        output_dir=out,
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["applied_count"] == 2
    assert manifest["coverage_gap_count"] == 0
    rows = [json.loads(line) for line in (out / "node_versions.jsonl").read_text(encoding="utf-8").splitlines()]
    by_component = {}
    for row in rows:
        by_component.setdefault(row["component_id"], []).append(row)
    assert "five years" in by_component["/in/union/rules/cgst-rules-2017/rule/137"][-1]["text"]
    assert "four\nyears" not in by_component["/in/union/rules/cgst-rules-2017/rule/137"][-1]["text"]
    assert "FORM GSTR-2 and" not in by_component["/in/union/rules/cgst-rules-2017/rule/43"][-1]["text"]


def test_search_index_is_rebuilt_from_corpus(tmp_path):
    corpus_dir = tmp_path / "corpus"
    sources_dir = tmp_path / "sources"
    seed_from_existing_data(ROOT, corpus_dir=corpus_dir, sources_dir=sources_dir)

    records = build_search_records(corpus_dir)
    record_ids = {record["id"] for record in records}

    assert "/in/union/forms/gst-reg-01#document" in record_ids
    assert "/in/union/rules/cgst-rules-2017/rule/10/subrule/1#provision" in record_ids
    assert all(record["canonical_id"].startswith("/in/") for record in records)

    provision_results = search_records(records, "checksum character", role="provision")
    assert provision_results
    assert provision_results[0]["canonical_id"] == "/in/union/rules/cgst-rules-2017/rule/10/subrule/1"
    assert provision_results[0]["matched_terms"]["checksum"] >= 1
    assert "checksum character" in provision_results[0]["snippet"]

    index_path = tmp_path / "derived/search/corpus_search.jsonl"
    written = write_search_index(corpus_dir, index_path)
    loaded = read_search_index(index_path)

    assert index_path.exists()
    assert loaded == written
    assert len(index_path.read_text(encoding="utf-8").splitlines()) == len(written)

    notification_results = search_index(index_path, "Aadhaar authentication", document_type="notification", limit=1)
    assert len(notification_results) == 1
    assert notification_results[0]["canonical_id"] == "/in/union/notifications/cbic/central-tax/2025/18-2025"


def test_api_payload_is_rebuilt_from_corpus(tmp_path):
    corpus_dir = tmp_path / "corpus"
    sources_dir = tmp_path / "sources"
    seed_from_existing_data(ROOT, corpus_dir=corpus_dir, sources_dir=sources_dir)

    payload = build_api_payload(corpus_dir)

    assert payload["profile"] == "git-for-law-india-v1"
    assert payload["stats"]["documents"] == 5
    assert payload["stats"]["provisions"] == 17
    assert any(document["canonical_id"] == "/in/union/forms/gst-reg-01" for document in payload["documents"])
    notification = next(
        document
        for document in payload["documents"]
        if document["canonical_id"] == "/in/union/notifications/cbic/central-tax/2025/18-2025"
    )
    assert len(notification["source_spans"]) == 9
    assert notification["source_spans"][0]["sourceNodeType"] == "notification_title"
    assert any(
        provision["canonical_id"] == "/in/union/rules/cgst-rules-2017/rule/10/subrule/1"
        and "checksum character" in provision["text"]
        for provision in payload["provisions"]
    )
    assert any(reference["target"] == "/in/union/forms/gst-reg-06" for reference in payload["references"])

    output = tmp_path / "derived/api/corpus_api.json"
    written = write_api_payload(corpus_dir, output)
    loaded = json.loads(output.read_text(encoding="utf-8"))

    assert output.exists()
    assert loaded == written


def test_html_site_is_rendered_from_corpus(tmp_path):
    corpus_dir = tmp_path / "corpus"
    sources_dir = tmp_path / "sources"
    seed_from_existing_data(ROOT, corpus_dir=corpus_dir, sources_dir=sources_dir)

    files = build_html_site(corpus_dir)

    assert "index.html" in files
    assert "assets/style.css" in files
    assert "data/corpus_api.json" in files
    assert "Git for Law Corpus" in files["index.html"]
    assert "Application for Registration" in files["index.html"]
    assert any(path.startswith("documents/in__union__rules__cgst-rules-2017__rule__10") for path in files)

    output_dir = tmp_path / "derived/html"
    result = write_html_site(corpus_dir, output_dir)

    assert result["documents"] == 5
    assert result["files"] == 8
    assert output_dir.joinpath("index.html").exists()
    assert output_dir.joinpath("assets/style.css").exists()
    assert "checksum character" in output_dir.joinpath(
        "documents/in__union__rules__cgst-rules-2017__rule__10.html"
    ).read_text(encoding="utf-8")


def test_vector_chunks_are_exported_from_corpus(tmp_path):
    corpus_dir = tmp_path / "corpus"
    sources_dir = tmp_path / "sources"
    seed_from_existing_data(ROOT, corpus_dir=corpus_dir, sources_dir=sources_dir)

    chunks = build_vector_chunks(corpus_dir, max_chars=220, overlap=40, include_documents=False)

    assert chunks
    assert all(chunk["role"] == "provision" for chunk in chunks)
    assert all(chunk["chunk_id"].endswith(f"chunk-{chunk['chunk_index']:04d}") for chunk in chunks)
    assert all(chunk["token_estimate"] > 0 for chunk in chunks)
    assert any(
        chunk["canonical_id"] == "/in/union/rules/cgst-rules-2017/rule/10/subrule/1"
        and "checksum character" in chunk["text"]
        for chunk in chunks
    )

    output = tmp_path / "derived/vector/corpus_chunks.jsonl"
    written = write_vector_chunks(
        corpus_dir,
        output,
        max_chars=220,
        overlap=40,
        include_documents=False,
    )
    loaded = read_vector_chunks(output)

    assert output.exists()
    assert loaded == written
    assert len(output.read_text(encoding="utf-8").splitlines()) == len(written)


def test_pipeline_verification_rebuilds_derived_artifacts(tmp_path):
    corpus_dir = tmp_path / "corpus"
    sources_dir = tmp_path / "sources"
    derived_dir = tmp_path / "derived"
    seed_from_existing_data(ROOT, corpus_dir=corpus_dir, sources_dir=sources_dir)

    manifest = derived_dir / "verification/latest.json"
    result = run_verification(
        corpus_dir=corpus_dir,
        sources_dir=sources_dir,
        derived_dir=derived_dir,
        manifest_path=manifest,
        vector_max_chars=220,
        vector_overlap=40,
    )

    assert result.ok
    assert manifest.exists()
    assert {step.name for step in result.steps} == {
        "sources",
        "corpus",
        "xml_source_spans",
        "quality",
        "unresolved_references",
        "graph",
        "neo4j_payload",
        "api_payload",
        "html",
        "search",
        "vector_chunks",
    }
    assert derived_dir.joinpath("graph/corpus_graph.json").exists()
    assert derived_dir.joinpath("quality/corpus_quality.json").exists()
    assert derived_dir.joinpath("references/unresolved_references.json").exists()
    assert derived_dir.joinpath("graph/corpus_neo4j_payload.json").exists()
    assert derived_dir.joinpath("api/corpus_api.json").exists()
    assert derived_dir.joinpath("html/index.html").exists()
    assert derived_dir.joinpath("search/corpus_search.jsonl").exists()
    assert derived_dir.joinpath("vector/corpus_chunks.jsonl").exists()

    manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
    assert manifest_data == result.to_dict()
    assert next(step for step in manifest_data["steps"] if step["name"] == "sources")["counts"]["archives"] == 1
    assert next(step for step in manifest_data["steps"] if step["name"] == "corpus")["counts"]["xml_files"] == 5
    assert next(step for step in manifest_data["steps"] if step["name"] == "xml_source_spans")["counts"]["xml_files"] == 1
    assert next(step for step in manifest_data["steps"] if step["name"] == "quality")["counts"]["documents"] == 5
    assert next(step for step in manifest_data["steps"] if step["name"] == "unresolved_references")["counts"][
        "unresolved_targets"
    ] > 0
    assert next(step for step in manifest_data["steps"] if step["name"] == "api_payload")["counts"]["documents"] == 5
    assert next(step for step in manifest_data["steps"] if step["name"] == "html")["counts"]["documents"] == 5
    assert next(step for step in manifest_data["steps"] if step["name"] == "search")["counts"]["records"] == 22

    strict_result = run_verification(
        corpus_dir=corpus_dir,
        sources_dir=sources_dir,
        derived_dir=derived_dir,
        manifest_path=None,
        strict_warnings=True,
        vector_max_chars=220,
        vector_overlap=40,
    )
    assert not strict_result.ok
    assert any("unresolved canonical reference" in warning for warning in strict_result.warnings)


def test_pipeline_verification_validates_source_inventory_when_present(tmp_path):
    corpus_dir = tmp_path / "corpus"
    sources_dir = tmp_path / "sources"
    derived_dir = tmp_path / "derived"
    seed_from_existing_data(ROOT, corpus_dir=corpus_dir, sources_dir=sources_dir)
    inventory = build_source_inventory(
        ROOT / "data/Law",
        sources_root=tmp_path / "sources-inventory",
        corpus_root=tmp_path / "corpus-inventory",
        limit=1,
        include_unclassified=False,
    )
    inventory_path = derived_dir / "sources/source_inventory.json"
    write_source_inventory(inventory, inventory_path)

    result = run_verification(
        corpus_dir=corpus_dir,
        sources_dir=sources_dir,
        derived_dir=derived_dir,
        manifest_path=None,
        vector_max_chars=220,
        vector_overlap=40,
    )

    inventory_step = next(step for step in result.steps if step.name == "source_inventory")
    assert inventory_step.ok
    assert inventory_step.counts == {"items": 1}
    assert inventory_step.output == str(inventory_path)

    tampered = json.loads(inventory_path.read_text(encoding="utf-8"))
    tampered["stats"]["items"] = 99
    inventory_path.write_text(json.dumps(tampered), encoding="utf-8")

    failed = run_verification(
        corpus_dir=corpus_dir,
        sources_dir=sources_dir,
        derived_dir=derived_dir,
        manifest_path=None,
        vector_max_chars=220,
        vector_overlap=40,
    )

    failed_step = next(step for step in failed.steps if step.name == "source_inventory")
    assert not failed.ok
    assert not failed_step.ok
    assert any("stats.items mismatch" in error for error in failed_step.errors)


def _seed_tool_service(tmp_path):
    corpus_dir = tmp_path / "corpus"
    sources_dir = tmp_path / "sources"
    seed_from_existing_data(ROOT, corpus_dir=corpus_dir, sources_dir=sources_dir)
    return NyayaToolService(
        corpus_dir=corpus_dir,
        search_index_path=tmp_path / "derived/search/missing.jsonl",
        graph_json_path=tmp_path / "derived/graph/missing.json",
        version_history_dir=tmp_path / "derived/version_history/cgst-rules-2017",
        lancedb_path=tmp_path / "missing-lancedb",
        falkor_port=0,
    )


def test_serving_tools_resolve_lookup_search_and_graph_paths(tmp_path):
    service = _seed_tool_service(tmp_path)
    subrule_id = "/in/union/rules/cgst-rules-2017/rule/10/subrule/1"
    form_id = "/in/union/forms/gst-reg-06"
    section_id = "/in/union/acts/cgst-act-2017/section/25"

    lookup = service.lookup_provision("CGST_Rules/Rule_10/SubRule_1", include_text=True)
    assert lookup["found"]
    assert lookup["canonical_id"] == subrule_id
    assert "checksum character" in lookup["provision"]["text"]

    resolved_rule = service.resolve_citation("rule 10 CGST Rules")
    assert resolved_rule["candidates"][0]["canonical_id"] == "/in/union/rules/cgst-rules-2017/rule/10"
    assert resolved_rule["candidates"][0]["exists"]

    resolved_form = service.resolve_citation("FORM GST REG-06")
    assert resolved_form["candidates"][0]["canonical_id"] == form_id

    lexical = service.lexical_search("checksum character", role="provision", limit=3)
    assert lexical
    assert lexical[0]["canonical_id"] in {subrule_id, "/in/union/rules/cgst-rules-2017/rule/10"}

    outgoing = service.get_outgoing_refs(subrule_id)
    outgoing_targets = {edge["target"] for edge in outgoing["references"]}
    assert form_id in outgoing_targets
    assert section_id in outgoing_targets

    incoming = service.get_incoming_refs(form_id)
    incoming_sources = {edge["source"] for edge in incoming["references"]}
    assert subrule_id in incoming_sources

    forms = service.get_forms_for_rule("CGST_Rules/Rule_10")
    assert {edge["target"] for edge in forms["forms"]} == {form_id}

    trace = service.trace_rule_to_act(subrule_id)
    assert any(path["nodes"][-1]["canonical_id"] == section_id for path in trace["paths"])

    related = service.find_related_provisions(subrule_id)
    assert form_id in {item["canonical_id"] for item in related["related"]}

    explained = service.explain_reference_path(subrule_id, section_id)
    assert explained["paths"]

    comparison = service.compare_versions(subrule_id, from_date="2025-01-01", to_date="2026-01-01")
    assert comparison["status"] in ("no_materialized_history", "not_found", "ok")


def test_semantic_search_provision_returns_section_hits_for_input_tax_credit(tmp_path):
    service = _seed_tool_service(tmp_path)

    class FakeSearch:
        def limit(self, _limit):
            return self

        def to_list(self):
            return [
                {
                    "_distance": 0.05,
                    "chunk_id": "/in/union/acts/cgst-act-2017/section/16#provision-0001",
                    "canonical_id": "/in/union/acts/cgst-act-2017/section/16",
                    "provision_type": "section",
                    "number": "16",
                    "document_id": "/in/union/acts/cgst-act-2017",
                    "document_type": "act",
                    "document_title": "Central Goods and Services Tax Act, 2017",
                    "title": "Eligibility and conditions for taking input tax credit",
                    "path": "corpus/in/union/acts/cgst-act-2017.xml",
                    "text": "16. Eligibility and conditions for taking input tax credit.",
                },
                {
                    "_distance": 0.15,
                    "chunk_id": "/in/union/rules/cgst-rules-2017/rule/36#provision-0001",
                    "canonical_id": "/in/union/rules/cgst-rules-2017/rule/36",
                    "provision_type": "rule",
                    "number": "36",
                    "document_id": "/in/union/rules/cgst-rules-2017",
                    "document_type": "rules",
                    "document_title": "Central Goods and Services Tax Rules, 2017",
                    "title": "Documentary requirements and conditions for claiming input tax credit",
                    "path": "corpus/in/union/rules/cgst-rules-2017.xml",
                    "text": "36. Documentary requirements and conditions for claiming input tax credit.",
                },
            ]

    class FakeTable:
        def search(self, vector):
            assert vector == [0.1, 0.2, 0.3]
            return FakeSearch()

    service._embed = lambda query: [0.1, 0.2, 0.3]  # type: ignore[method-assign]
    service._provision_lance_table = FakeTable()

    result = service.semantic_search_provision("input tax credit", limit=3)

    assert result["mode"] == "semantic_provision"
    assert any(
        item["canonical_id"].startswith("/in/union/acts/cgst-act-2017/section/")
        for item in result["results"][:3]
    )
    assert result["results"][0]["canonical_id"] == "/in/union/acts/cgst-act-2017/section/16"
    assert result["results"][0]["provision_type"] == "section"


def test_get_provision_as_of_date_returns_text_and_amendments(tmp_path):
    """Time-travel query returns correct text, provenance, and filtered amendment chain."""
    service = _seed_tool_service(tmp_path)
    rule_id = "/in/union/rules/cgst-rules-2017/rule/10"

    result = service.get_provision_as_of_date(rule_id, date="2017-06-22")

    assert result["status"] in ("ok", "ok_with_gaps")
    assert result["canonical_id"] == rule_id
    assert result["date"] == "2017-06-22"
    assert result["text"]
    assert result["version_id"]
    assert result["text_sha256"]
    assert "event_chain" in result
    assert "source_basis" in result
    assert "amendments" in result
    # Amendments filtered to effective_date <= queried date
    for a in result["amendments"]:
        assert (a.get("effective_date") or "") <= "2017-06-22"


def test_get_provision_as_of_date_returns_not_found_for_pre_existence(tmp_path):
    """Querying before the provision existed returns not_found."""
    service = _seed_tool_service(tmp_path)
    result = service.get_provision_as_of_date(
        "/in/union/rules/cgst-rules-2017/rule/10", date="2010-01-01"
    )
    assert result["status"] == "not_found"
    assert result["text"] == ""


def test_list_amendments_returns_ordered_chain(tmp_path):
    """list_amendments returns amendments ordered by effective date."""
    service = _seed_tool_service(tmp_path)
    rule_id = "/in/union/rules/cgst-rules-2017/rule/10"
    result = service.list_amendments(rule_id)
    assert "amendments" in result
    dates = [a.get("effective_date") or "" for a in result["amendments"]]
    assert dates == sorted(dates)


def test_compare_versions_returns_diff(tmp_path):
    """compare_versions returns text_changed and events for a known amended rule."""
    service = _seed_tool_service(tmp_path)
    rule_id = "/in/union/rules/cgst-rules-2017/rule/10"
    result = service.compare_versions(rule_id, from_date="2017-06-22", to_date="2025-01-01")
    assert "from_version" in result or "status" in result


def test_get_provision_timeline_returns_versions(tmp_path):
    """get_provision_timeline returns multiple versions for an amended provision."""
    service = _seed_tool_service(tmp_path)
    rule_id = "/in/union/rules/cgst-rules-2017/rule/10"
    result = service.get_provision_timeline(rule_id)
    assert "versions" in result
    assert result["count"] >= 1


def test_rest_api_routes_use_shared_service(tmp_path):
    from scripts import serve_api

    previous = serve_api._SERVICE
    serve_api._SERVICE = _seed_tool_service(tmp_path)
    try:
        resolved = serve_api.resolve_citation(serve_api.ResolveCitationRequest(citation="rule 10 CGST Rules"))
        assert resolved["candidates"][0]["canonical_id"] == "/in/union/rules/cgst-rules-2017/rule/10"

        payload = serve_api.lookup_provision(
            serve_api.LookupRequest(canonical_id="CGST_Rules/Rule_10/SubRule_1", include_text=False)
        )
        assert payload["found"]
        assert "text" not in payload["provision"]
    finally:
        serve_api._SERVICE = previous


def test_rules_baseline_reconciliation_flags_llm_disagreement():
    from src.legal_corpus.baselines import BaselineComponent, reconcile_baseline_tracks

    deterministic = [
        BaselineComponent(
            component_id="/in/union/rules/cgst-rules-2017/rule/1",
            label="1",
            heading="Short title",
            text="1. Short title.",
            component_type="rule",
        )
    ]
    llm = [
        BaselineComponent(
            component_id="/in/union/rules/cgst-rules-2017/rule/1",
            label="1",
            heading="Different heading",
            text="Different text",
            component_type="rule",
        )
    ]

    components, reconciliation = reconcile_baseline_tracks(deterministic, llm, llm_attempted=True)

    assert components[0].blocked
    assert reconciliation["strategy"] == "deterministic_plus_omlx_reconciliation"
    assert reconciliation["llm_attempted"]
    assert reconciliation["llm_coverage"] == "complete"
    assert reconciliation["blocked_count"] == 1
    assert reconciliation["blocked_components"][0]["reason"] == "baseline_track_disagreement"


def test_rules_baseline_reconciliation_does_not_overclaim_without_llm():
    from src.legal_corpus.baselines import BaselineComponent, reconcile_baseline_tracks

    deterministic = [
        BaselineComponent(
            component_id="/in/union/rules/cgst-rules-2017/rule/1",
            label="1",
            heading="Short title",
            text="1. Short title.",
            component_type="rule",
        )
    ]

    _components, reconciliation = reconcile_baseline_tracks(deterministic, [])

    assert reconciliation["strategy"] == "deterministic_only"
    assert not reconciliation["llm_attempted"]
    assert reconciliation["llm_component_count"] == 0
    assert reconciliation["llm_coverage"] == "none"


def test_rules_baseline_reconciliation_reports_empty_llm_attempt():
    from src.legal_corpus.baselines import BaselineComponent, reconcile_baseline_tracks

    deterministic = [
        BaselineComponent(
            component_id="/in/union/rules/cgst-rules-2017/rule/1",
            label="1",
            heading="Short title",
            text="1. Short title.",
            component_type="rule",
        )
    ]

    _components, reconciliation = reconcile_baseline_tracks(deterministic, [], llm_attempted=True)

    assert reconciliation["strategy"] == "deterministic_with_omlx_reconciliation_empty"
    assert reconciliation["llm_attempted"]
    assert reconciliation["llm_coverage"] == "none"
    assert "omlx_returned_no_components" in reconciliation["warnings"]


def test_rules_baseline_reconciliation_reports_partial_llm_coverage():
    from src.legal_corpus.baselines import BaselineComponent, reconcile_baseline_tracks

    deterministic = [
        BaselineComponent(
            component_id="/in/union/rules/cgst-rules-2017/rule/1",
            label="1",
            heading="Short title",
            text="1. Short title.",
            component_type="rule",
        ),
        BaselineComponent(
            component_id="/in/union/rules/cgst-rules-2017/rule/2",
            label="2",
            heading="Definitions",
            text="2. Definitions.",
            component_type="rule",
        ),
    ]
    llm = [
        BaselineComponent(
            component_id="/in/union/rules/cgst-rules-2017/rule/1",
            label="1",
            heading="Short title",
            text="1. Short title.",
            component_type="rule",
        )
    ]

    _components, reconciliation = reconcile_baseline_tracks(deterministic, llm, llm_attempted=True)

    assert reconciliation["strategy"] == "deterministic_plus_omlx_reconciliation"
    assert reconciliation["llm_coverage"] == "partial"
    assert "omlx_reconciliation_partial_coverage" in reconciliation["warnings"]


def test_rules_baseline_reconciliation_blocks_contaminated_components():
    from src.legal_corpus.baselines import BaselineComponent, reconcile_baseline_tracks

    deterministic = [
        BaselineComponent(
            component_id="/in/union/rules/cgst-rules-2017/rule/24/subrule/3",
            label="3",
            heading="",
            text="(3) Original text. [(3A) Later inserted text.]",
            component_type="subrule",
        ),
        BaselineComponent(
            component_id="/in/union/rules/cgst-rules-2017/rule/100",
            label="100",
            heading="2201 Waters, including natural or artificial mineral waters",
            text="100. 2201 Waters, including natural or artificial mineral waters",
            component_type="rule",
        ),
        BaselineComponent(
            component_id="/in/union/rules/cgst-rules-2017/rule/25",
            label="25",
            heading="Physical verification",
            text="25 Physical verification. 48Inserted vide Notf no. 7/2017-CT dt. 27.06.2017",
            component_type="rule",
        ),
    ]

    components, reconciliation = reconcile_baseline_tracks(deterministic, [])
    blocked = {component.component_id: component.block_reasons for component in components if component.blocked}

    assert reconciliation["blocked_count"] == 3
    assert reconciliation["quality_blocked_count"] == 3
    assert "baseline_quality_flags_present" in reconciliation["warnings"]
    assert blocked["/in/union/rules/cgst-rules-2017/rule/24/subrule/3"] == (
        "embedded_later_subrule_marker_in_baseline",
    )
    assert blocked["/in/union/rules/cgst-rules-2017/rule/100"] == (
        "table_or_tariff_row_misparsed_as_rule",
    )
    assert "post_2017_amendment_annotation_in_baseline" in blocked[
        "/in/union/rules/cgst-rules-2017/rule/25"
    ]


def test_rules_baseline_decontamination_strips_trailing_chapter_heading_bleed():
    from src.legal_corpus.baselines import decontaminate_baseline_text

    text = (
        "107. Certification of copies of the advance rulings pronounced by the Appellate "
        "Authority. - A copy of the advance ruling pronounced by the Appellate Authority "
        "for Advance Ruling and duly signed by the Members shall be sent to- (a) the "
        "applicant and the appellant; (b) the concerned officer of central tax and State "
        "or Union territory tax; (c) the jurisdictional officer of central tax and State "
        "or Union territory tax; and (d) the Authority, in accordance with the provisions "
        "of sub-section (4) of section 101 of the Act. Chapter - XIII Appeals and Revision"
    )

    cleaned = decontaminate_baseline_text(text)

    assert cleaned.endswith("section 101 of the Act.")
    assert "Chapter - XIII" not in cleaned


def test_act_baseline_uses_canonical_corpus_section_ids():
    from src.legal_corpus.baselines import parse_act_corpus_xml

    components = parse_act_corpus_xml(ROOT / "corpus/in/union/acts/cgst-act-2017/act.xml")
    by_id = {component.component_id: component for component in components}

    assert "Power to arrest" in by_id["/in/union/acts/cgst-act-2017/section/69"].text
    assert "General provisions relating to determination of tax" in by_id[
        "/in/union/acts/cgst-act-2017/section/75"
    ].text
    assert "Appeals to Appellate Tribunal" in by_id["/in/union/acts/cgst-act-2017/section/112"].text
    assert "rectify any error apparent" not in by_id["/in/union/acts/cgst-act-2017/section/112"].text


def test_act_compiler_resolves_finance_act_commencement_and_conflicts(tmp_path):
    from src.legal_corpus.act_amendment_events import compile_act_events

    source_dir = tmp_path / "finance"
    source_dir.mkdir()
    data = {
        "source": "fixture",
        "act": "Finance Act, 2021",
        "year": "2021",
        "sections": [
            {
                "section_number": "112",
                "description": "Amendment of section 50",
                "full_text": 'In section 50 of the Central Goods and Services Tax Act, 2017, for the words "old", the words "new" shall be substituted.',
            },
            {
                "section_number": "113",
                "description": "Amendment of section 50 again",
                "full_text": 'In section 50 of the Central Goods and Services Tax Act, 2017, for the words "old", the words "newer" shall be substituted.',
            },
        ],
    }
    (source_dir / "finance_act_2021.json").write_text(json.dumps(data), encoding="utf-8")
    notifications = tmp_path / "notifications"
    notifications.mkdir()
    for name, text in [
        (
            "16-2021.json",
            "Seeks to appoint 01.06.2021 as the day from which the provisions of section 112 of Finance Act, 2021 shall come into force.",
        ),
        (
            "39-2021.json",
            "Seeks to notify 01.06.2021 as the date on which provisions of section 113 of the Finance Act, 2021 shall come into force.",
        ),
    ]:
        (notifications / name).write_text(
            json.dumps(
                {
                    "category": "Central Tax",
                    "name": text,
                    "no": name,
                    "id": name,
                    "issueDt": "2021-06-01T05:30:00+05:30",
                    "contentText": "",
                }
            ),
            encoding="utf-8",
        )

    output = tmp_path / "events.jsonl"
    review = tmp_path / "review.json"
    compile_act_events(
        registry_path=ROOT / "data/Law/statute_identity_registry.json",
        source_dir=source_dir,
        source_family="finance-acts",
        target_work="/in/union/acts/cgst-act-2017",
        output=output,
        review_output=review,
        commencement_dir=notifications,
    )

    events = read_events(output)
    assert {event["legal_time"]["applicability_start"] for event in events} == {"2021-06-01"}
    assert all(event["validation"]["date_resolved"] for event in events)
    assert all("date_not_resolved" not in event["review"]["review_reasons"] for event in events)
    assert all("same_effective_date_conflict" in event["review"]["review_reasons"] for event in events)
    assert all(not event["validation"]["materializable"] for event in events)


def test_act_compiler_extracts_clause_span_from_noisy_finance_act_text():
    from src.legal_corpus.act_amendment_events import _clause_span, _effective_date_from_act, _target_section_number

    noisy = (
        "Amendment of section 50 INDIRECT TAXES CHAPTER IV 111 0000000000000000000000111 "
        "Finance Acts|abc 2022|def 111. In section 50 of the Central Goods and Services Tax Act, "
        'for the words "old", the words "new" shall be substituted.'
    )
    start, end, clause = _clause_span(noisy, "111")

    assert start > 0
    assert end == len(noisy)
    assert clause.startswith("111. In section 50")
    assert _target_section_number(clause) == "50"

    data = {
        "sections": [
            {
                "section_number": "1",
                "full_text": (
                    "1. (1) This Act may be called the Finance Act, 2023. "
                    "(2) sections 128 to 163 shall come into force on the 1st day of October, 2023;"
                ),
            }
        ]
    }
    assert _effective_date_from_act(data, "147") == {
        "date": "2023-10-01",
        "basis": "finance_act_commencement_clause",
    }


def test_act_compiler_validates_direct_substitute_against_baseline(tmp_path):
    from src.legal_corpus.act_amendment_events import compile_act_events

    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    baseline_component = {
        "component_id": "/in/union/acts/cgst-act-2017/section/50",
        "text": "Interest is payable on old amount under this section.",
    }
    (baseline_dir / "baseline_components.jsonl").write_text(json.dumps(baseline_component) + "\n", encoding="utf-8")

    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/acts/cgst-act-2017":
            work["baseline_path"] = str(baseline_dir)
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")

    source_dir = tmp_path / "finance"
    source_dir.mkdir()
    (source_dir / "finance_act_2021.json").write_text(
        json.dumps(
            {
                "source": "fixture",
                "act": "Finance Act, 2021",
                "year": "2021",
                "sections": [
                    {
                        "section_number": "112",
                        "description": "Amendment of section 50",
                        "full_text": (
                            'In section 50 of the Central Goods and Services Tax Act, 2017, '
                            'for the words "old amount", the words "net tax liability" shall be substituted.'
                        ),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    notifications = tmp_path / "notifications"
    notifications.mkdir()
    (notifications / "16-2021.json").write_text(
        json.dumps(
            {
                "category": "Central Tax",
                "name": (
                    "Seeks to appoint 01.06.2021 as the day from which the provisions "
                    "of section 112 of Finance Act, 2021 shall come into force."
                ),
                "no": "16/2021-Central Tax",
                "id": "16-2021",
                "issueDt": "2021-06-01T05:30:00+05:30",
                "contentText": "",
            }
        ),
        encoding="utf-8",
    )

    output = tmp_path / "events.jsonl"
    review = tmp_path / "review.json"
    compile_act_events(
        registry_path=registry_path,
        source_dir=source_dir,
        source_family="finance-acts",
        target_work="/in/union/acts/cgst-act-2017",
        output=output,
        review_output=review,
        commencement_dir=notifications,
    )

    [event] = read_events(output)
    assert event["status"] == "validated"
    assert event["operation"] == "SUBSTITUTE"
    assert event["payload"] == {"old_text": "old amount", "new_text": "net tax liability"}
    assert event["validation"]["anchor_resolved"]
    assert event["validation"]["materializable"]
    assert event["review"]["review_reasons"] == []


def test_act_compiler_materializes_source_proven_inserted_section(tmp_path):
    from src.legal_corpus.act_amendment_events import compile_act_events

    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso><act name="cgst-act-2017"><body>
<section refersTo="/in/union/acts/cgst-act-2017/section/122a">
<num>122A</num><heading>Penalty for certain offences</heading>
<content><p>122A. Existing penalty section.</p></content>
</section>
</body></act></akomaNtoso>
""",
        encoding="utf-8",
    )
    (baseline_dir / "baseline_components.jsonl").write_text(
        json.dumps(
            {
                "component_id": "/in/union/acts/cgst-act-2017/section/122a",
                "label": "122A",
                "heading": "Penalty for certain offences",
                "text": "122A. Existing penalty section.",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/acts/cgst-act-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-04-12"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")

    source_dir = tmp_path / "finance"
    source_dir.mkdir()
    (source_dir / "finance_act_2024.json").write_text(
        json.dumps(
            {
                "source": "fixture",
                "act": "Finance Act, 2024",
                "year": "2024",
                "sections": [
                    {
                        "section_number": "1",
                        "description": "Short title and commencement",
                        "full_text": (
                            "1. (1) This Act may be called the Finance Act, 2024. "
                            "(2) sections 131 to 131 shall come into force on the 1st day of October, 2024;"
                        ),
                    },
                    {
                        "section_number": "131",
                        "description": "Insertion of new section 122B",
                        "full_text": (
                            "131. After section 122A of the Central Goods and Services Tax Act, "
                            "the following section shall be inserted, namely:- "
                            "\"122B. Penalty for failure to register certain machines. "
                            "Any person who contravenes the special procedure shall be liable to pay a penalty.\""
                        ),
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    events_path = tmp_path / "events.jsonl"
    review_path = tmp_path / "review.json"
    compile_act_events(
        registry_path=registry_path,
        source_dir=source_dir,
        source_family="finance-acts",
        target_work="/in/union/acts/cgst-act-2017",
        output=events_path,
        review_output=review_path,
        commencement_dir=tmp_path / "notifications",
    )

    [event] = read_events(events_path)
    assert event["status"] == "validated"
    assert event["operation"] == "INSERT_SIBLING"
    assert event["target"]["component_id"] == "/in/union/acts/cgst-act-2017/section/122b"
    assert event["target"]["anchor_component_id"] == "/in/union/acts/cgst-act-2017/section/122a"
    assert event["payload"]["label"] == "122B"
    assert event["payload"]["node_type"] == "section"
    assert event["validation"]["materializable"]

    manifest = materialize_versions(
        target_work="/in/union/acts/cgst-act-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["applied_count"] == 1
    assert manifest["coverage_gap_count"] == 0
    node_versions = [
        json.loads(line)
        for line in (tmp_path / "out" / "node_versions.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    inserted = [row for row in node_versions if row["component_id"].endswith("/section/122b")]
    assert inserted
    assert "Penalty for failure to register certain machines" in inserted[-1]["text"]


def test_act_compiler_splits_and_materializes_compound_inserted_sections(tmp_path):
    from src.legal_corpus.act_amendment_events import compile_act_events

    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso><act name="cgst-act-2017"><body>
<section refersTo="/in/union/acts/cgst-act-2017/section/101">
<num>101</num><heading>Orders of Appellate Authority</heading>
<content><p>101. Existing section.</p></content>
</section>
</body></act></akomaNtoso>
""",
        encoding="utf-8",
    )
    (baseline_dir / "baseline_components.jsonl").write_text(
        json.dumps({"component_id": "/in/union/acts/cgst-act-2017/section/101", "text": "101. Existing section."})
        + "\n",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/acts/cgst-act-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-04-12"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")

    source_dir = tmp_path / "finance"
    source_dir.mkdir()
    (source_dir / "finance_no_2_act_2019.json").write_text(
        json.dumps(
            {
                "source": "fixture",
                "act": "Finance (No. 2) Act, 2019",
                "year": "2019",
                "sections": [
                    {
                        "section_number": "1",
                        "full_text": (
                            "1. (1) This Act may be called the Finance (No. 2) Act, 2019. "
                            "(2) sections 105 to 105 shall come into force on the 1st day of January, 2020;"
                        ),
                    },
                    {
                        "section_number": "105",
                        "description": "Insertion of new sections 101A, 101B and 101C",
                        "full_text": (
                            "105. After section 101 of the Central Goods and Services Tax Act, "
                            "the following sections shall be inserted, namely:- "
                            "Constitution of National Appellate Authority for Advance Ruling. "
                            "\"101A. (1) The Government shall constitute the National Appellate Authority. "
                            "Appeal to National Appellate Authority. 101B. (1) Any applicant may prefer an appeal. "
                            "Order of National Appellate Authority. 101C. (1) The National Appellate Authority may pass orders.\""
                        ),
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    events_path = tmp_path / "events.jsonl"
    compile_act_events(
        registry_path=registry_path,
        source_dir=source_dir,
        source_family="finance-acts",
        target_work="/in/union/acts/cgst-act-2017",
        output=events_path,
        review_output=tmp_path / "review.json",
        commencement_dir=tmp_path / "notifications",
    )

    events = read_events(events_path)
    assert [event["target"]["component_id"] for event in events] == [
        "/in/union/acts/cgst-act-2017/section/101a",
        "/in/union/acts/cgst-act-2017/section/101b",
        "/in/union/acts/cgst-act-2017/section/101c",
    ]
    assert [event["target"]["anchor_component_id"] for event in events] == [
        "/in/union/acts/cgst-act-2017/section/101",
        "/in/union/acts/cgst-act-2017/section/101a",
        "/in/union/acts/cgst-act-2017/section/101b",
    ]
    assert all(event["operation"] == "INSERT_SIBLING" for event in events)
    assert all(event["status"] == "validated" for event in events)
    assert all(event["payload"]["compound_parent_event_id"] for event in events)

    manifest = materialize_versions(
        target_work="/in/union/acts/cgst-act-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["applied_count"] == 3
    assert manifest["coverage_gap_count"] == 0


def test_act_compiler_routes_notification_effect_rows_to_metadata_lane(tmp_path):
    from src.legal_corpus.act_amendment_events import compile_act_events

    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        "<akomaNtoso><act name=\"cgst-act-2017\"><body /></act></akomaNtoso>",
        encoding="utf-8",
    )
    (baseline_dir / "baseline_components.jsonl").write_text("", encoding="utf-8")
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/acts/cgst-act-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-04-12"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")

    source_dir = tmp_path / "finance"
    source_dir.mkdir()
    (source_dir / "finance_act_2022.json").write_text(
        json.dumps(
            {
                "source": "fixture",
                "act": "Finance Act, 2022",
                "year": "2022",
                "sections": [
                    {
                        "section_number": "117",
                        "description": "Retrospective exemption from central tax",
                        "full_text": (
                            "117. Retrospective exemption under the Central Goods and Services Tax Act. "
                            "Notwithstanding anything contained in the notification of the Government "
                            "of India in the Ministry of Finance (Department of Revenue) number G.S.R. 673(E), "
                            "dated the 28th June, 2017, no central tax shall be levied or collected "
                            "retrospectively in respect of specified supply."
                        ),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    events_path = tmp_path / "events.jsonl"
    compile_act_events(
        registry_path=registry_path,
        source_dir=source_dir,
        source_family="finance-acts",
        target_work="/in/union/acts/cgst-act-2017",
        output=events_path,
        review_output=tmp_path / "review.json",
        commencement_dir=tmp_path / "notifications",
    )

    [event] = read_events(events_path)
    assert event["operation"] == "UNKNOWN"
    assert event["payload"]["metadata_only"] is True
    assert event["payload"]["triage_lane"] == "metadata_only"
    assert "metadata_only" in event["review"]["review_reasons"]

    manifest = materialize_versions(
        target_work="/in/union/acts/cgst-act-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["metadata_only_count"] == 1
    assert manifest["coverage_gap_count"] == 0


def test_act_compiler_routes_deemed_notification_commencement_to_metadata_lane(tmp_path):
    from src.legal_corpus.act_amendment_events import compile_act_events

    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        "<akomaNtoso><act name=\"cgst-act-2017\"><body /></act></akomaNtoso>",
        encoding="utf-8",
    )
    (baseline_dir / "baseline_components.jsonl").write_text("", encoding="utf-8")
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/acts/cgst-act-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-04-12"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")

    source_dir = tmp_path / "finance"
    source_dir.mkdir()
    (source_dir / "finance_act_2020.json").write_text(
        json.dumps(
            {
                "source": "fixture",
                "act": "Finance Act, 2020",
                "year": "2020",
                "sections": [
                    {
                        "section_number": "133",
                        "description": "Retrospective commencement of notification",
                        "full_text": (
                            "133. The notification of the Government of India number G.S.R. 708(E), "
                            "issued under clause (ii) of the proviso to sub-section (3) of section 54 "
                            "of the Central Goods and Services Tax Act, 2017 shall be deemed to have, "
                            "and always to have, for all purposes, come into force on and from "
                            "the 1st day of July, 2017."
                        ),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    events_path = tmp_path / "events.jsonl"
    compile_act_events(
        registry_path=registry_path,
        source_dir=source_dir,
        source_family="finance-acts",
        target_work="/in/union/acts/cgst-act-2017",
        output=events_path,
        review_output=tmp_path / "review.json",
        commencement_dir=tmp_path / "notifications",
    )

    [event] = read_events(events_path)
    assert event["operation"] == "UNKNOWN"
    assert event["payload"]["metadata_only"] is True
    assert event["payload"]["metadata_only_reason"] == "act_notification_level_retrospective_effect"
    assert "metadata_only" in event["review"]["review_reasons"]


def test_act_compiler_routes_specific_non_target_act_rows_out_of_scope(tmp_path):
    from src.legal_corpus.act_amendment_events import compile_act_events

    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        "<akomaNtoso><act name=\"cgst-act-2017\"><body /></act></akomaNtoso>",
        encoding="utf-8",
    )
    (baseline_dir / "baseline_components.jsonl").write_text("", encoding="utf-8")
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/acts/cgst-act-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-04-12"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")

    source_dir = tmp_path / "finance"
    source_dir.mkdir()
    (source_dir / "finance_act_2024.json").write_text(
        json.dumps(
            {
                "source": "fixture",
                "act": "Finance Act, 2024",
                "year": "2024",
                "sections": [
                    {
                        "section_number": "120",
                        "description": "Amendment of Integrated Goods and Services Tax Act",
                        "full_text": (
                            "120. In the Central Goods and Services Tax Act and related enactments, "
                            "(i) In section 16 of the Integrated Goods and Services Tax Act, 2017, "
                            "for the words \"zero-rated supply\", the words \"specified supply\" shall be substituted; "
                            "(ii) In section 16 of the Central Goods and Services Tax Act, 2017, "
                            "after the words \"input tax credit\", the words \"or integrated tax credit\" shall be inserted."
                        ),
                    },
                    {
                        "section_number": "121",
                        "description": "Insertion in Central Excise Act with CGST reference",
                        "full_text": (
                            "121. In the Central Excise Act, after section 38A, the following section shall be inserted, namely: "
                            "\"38B. Savings of references. Notwithstanding the repeal of the Central Excise Tariff Act, 1985 "
                            "by section 174 of the Central Goods and Services Tax Act, 2017, references shall continue.\"."
                        ),
                    },
                    {
                        "section_number": "122",
                        "description": "Cross statute relaxation",
                        "full_text": (
                            "122. Notwithstanding anything contained in the Central Excise Act, 1944, the Customs Act, 1962 "
                            "or Chapter V of the Finance Act, 1994, as it stood prior to its omission by section 173 of the "
                            "Central Goods and Services Tax Act, 2017, the time limit specified in, or prescribed or notified "
                            "under, the said Acts shall stand extended."
                        ),
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    events_path = tmp_path / "events.jsonl"
    compile_act_events(
        registry_path=registry_path,
        source_dir=source_dir,
        source_family="finance-acts",
        target_work="/in/union/acts/cgst-act-2017",
        output=events_path,
        review_output=tmp_path / "review.json",
        commencement_dir=tmp_path / "notifications",
    )

    events = read_events(events_path)
    out_of_scope = next(event for event in events if "Integrated Goods and Services Tax Act" in event["evidence"]["excerpt"])
    in_scope = next(event for event in events if "input tax credit" in event["evidence"]["excerpt"])
    central_excise = next(event for event in events if "Central Excise Act" in event["evidence"]["excerpt"])
    cross_statute = next(event for event in events if "time limit specified" in event["evidence"]["excerpt"])
    assert out_of_scope["target"]["component_id"] == "/in/union/acts/cgst-act-2017/section/16"
    assert out_of_scope["payload"]["triage_lane"] == "act_out_of_scope"
    assert out_of_scope["payload"]["act_out_of_scope_reason"] == "non_target_act_reference"
    assert "act_out_of_scope" in out_of_scope["review"]["review_reasons"]
    assert central_excise["payload"]["triage_lane"] == "act_out_of_scope"
    assert cross_statute["payload"]["triage_lane"] == "act_out_of_scope"
    assert in_scope["payload"].get("triage_lane") != "act_out_of_scope"


def test_act_compiler_uses_default_act_commencement_for_taxation_act_insertions(tmp_path):
    from src.legal_corpus.act_amendment_events import compile_act_events

    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        "<akomaNtoso><act name=\"cgst-act-2017\"><body /></act></akomaNtoso>",
        encoding="utf-8",
    )
    (baseline_dir / "baseline_components.jsonl").write_text("", encoding="utf-8")
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/acts/cgst-act-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-04-12"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")

    source_dir = tmp_path / "taxation"
    source_dir.mkdir()
    (source_dir / "taxation_and_other_laws_relaxation_and_amendment_of_certain_provisions_act_2020.json").write_text(
        json.dumps(
            {
                "source": "fixture",
                "act": "Taxation and Other Laws (Relaxation and Amendment of Certain Provisions) Act, 2020",
                "sections": [
                    {
                        "section_number": "1",
                        "description": "Short title and commencement",
                        "full_text": (
                            "1. (1) This Act may be called the Taxation and Other Laws Act, 2020. "
                            "(2) Save as otherwise provided, it shall be deemed to have come into force "
                            "on the 31st day of March, 2020."
                        ),
                    },
                    {
                        "section_number": "7",
                        "description": "Insertion of new section 168A in Act 12 of 2017",
                        "full_text": (
                            "7. After section 168 of the Central Goods and Services Tax Act, 2017, "
                            "the following section shall be inserted, namely: "
                            "\"168A. Power of Government to extend time limit in special circumstances. "
                            "The Government may extend the time limit.\"."
                        ),
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    events_path = tmp_path / "events.jsonl"
    compile_act_events(
        registry_path=registry_path,
        source_dir=source_dir,
        source_family="taxation-acts",
        target_work="/in/union/acts/cgst-act-2017",
        output=events_path,
        review_output=tmp_path / "review.json",
        commencement_dir=tmp_path / "notifications",
    )

    [event] = read_events(events_path)
    assert event["target"]["component_id"] == "/in/union/acts/cgst-act-2017/section/168a"
    assert event["legal_time"]["applicability_start"] == "2020-03-31"
    assert event["legal_time"]["date_basis"] == "act_default_commencement_clause"
    assert "date_not_resolved" not in event["review"]["review_reasons"]
    assert event["status"] == "validated"


def test_act_compiler_splits_compound_finance_act_clause(tmp_path):
    from src.legal_corpus.act_amendment_events import compile_act_events

    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    baseline_components = [
        {
            "component_id": "/in/union/acts/cgst-act-2017/section/50",
            "text": "Section 50 contains old amount and old phrase.",
        },
        {
            "component_id": "/in/union/acts/cgst-act-2017/section/51",
            "text": "Section 51 contains old words.",
        },
    ]
    (baseline_dir / "baseline_components.jsonl").write_text(
        "\n".join(json.dumps(row) for row in baseline_components) + "\n",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/acts/cgst-act-2017":
            work["baseline_path"] = str(baseline_dir)
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")

    source_dir = tmp_path / "finance"
    source_dir.mkdir()
    (source_dir / "finance_act_2021.json").write_text(
        json.dumps(
            {
                "source": "fixture",
                "act": "Finance Act, 2021",
                "year": "2021",
                "sections": [
                    {
                        "section_number": "112",
                        "description": "Amendment of sections 50 and 51",
                        "full_text": (
                            "112. In the Central Goods and Services Tax Act, 2017,- "
                            "(i) in section 50, for the words \"old amount\", the words \"net tax\" shall be substituted; "
                            "(ii) in section 51, for the words \"old words\", the words \"new words\" shall be substituted."
                        ),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    notifications = tmp_path / "notifications"
    notifications.mkdir()
    (notifications / "16-2021.json").write_text(
        json.dumps(
            {
                "category": "Central Tax",
                "name": (
                    "Seeks to appoint 01.06.2021 as the day from which the provisions "
                    "of section 112 of Finance Act, 2021 shall come into force."
                ),
                "no": "16/2021-Central Tax",
                "id": "16-2021",
                "issueDt": "2021-06-01T05:30:00+05:30",
                "contentText": "",
            }
        ),
        encoding="utf-8",
    )

    output = tmp_path / "events.jsonl"
    compile_act_events(
        registry_path=registry_path,
        source_dir=source_dir,
        source_family="finance-acts",
        target_work="/in/union/acts/cgst-act-2017",
        output=output,
        review_output=tmp_path / "review.json",
        commencement_dir=notifications,
    )

    events = read_events(output)
    assert sorted(event["target"]["component_id"] for event in events) == [
        "/in/union/acts/cgst-act-2017/section/50",
        "/in/union/acts/cgst-act-2017/section/51",
    ]
    assert all(event["status"] == "validated" for event in events)
    assert events[0]["event_id"] != events[1]["event_id"]


def test_act_compiler_reads_cbic_act_chapter_sections(tmp_path):
    from src.legal_corpus.act_amendment_events import compile_act_events

    source_dir = tmp_path / "acts"
    source_dir.mkdir()
    (source_dir / "the-central-goods-and-services-tax-amendment-act-2023.json").write_text(
        json.dumps(
            {
                "source": "cbic_tax_portal",
                "type": "act",
                "act": "The Central Goods and Services Tax Amendment Act, 2023",
                "issueDt": "2023-08-18T05:30:00+05:30",
                "chapters": [
                    {
                        "chapterNo": "The Central Goods and Services Tax Amendment Act, 2023",
                        "sections": [
                            {
                                "sectionNo": "Section 3",
                                "sectionName": "Amendment of section 24",
                                "contentText": (
                                    "In section 24 of the principal Act, "
                                    "after clause (xi), the following clause shall be inserted, namely: "
                                    '"(xia) every person supplying online money gaming."'
                                ),
                            },
                            {
                                "sectionNo": "Section 4",
                                "sectionName": "Amendment of Schedule III",
                                "contentText": (
                                    "In Schedule III to the principal Act, after paragraph 6, "
                                    "the following paragraph shall be inserted, namely: "
                                    '"7. Supply of specified actionable claims."'
                                ),
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (source_dir / "the-integrated-goods-and-services-tax-amendment-act-2023.json").write_text(
        json.dumps(
            {
                "source": "cbic_tax_portal",
                "type": "act",
                "act": "The Integrated Goods and Services Tax Amendment Act, 2023",
                "issueDt": "2023-08-18T05:30:00+05:30",
                "sections": [
                    {
                        "sectionNo": "Section 2",
                        "sectionName": "Amendment of section 2",
                        "contentText": (
                            "In section 2 of the Integrated Goods and Services Tax Act, 2017, "
                            "for the words \"old\", the words \"new\" shall be substituted."
                        ),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (source_dir / "the-central-goods-and-services-tax-second-amendment-act-2023.json").write_text(
        json.dumps(
            {
                "source": "cbic_tax_portal",
                "type": "act",
                "act": "THE CENTRAL GOODS AND SERVICES TAX (SECOND AMENDMENT) ACT, 2023",
                "issueDt": "2023-12-28T05:30:00+05:30",
                "sections": [
                    {
                        "sectionNo": "Section 2",
                        "sectionName": "Amendment of section 110",
                        "contentText": (
                            "Section 2. Amendment of section 110. In section 110 of the Central Goods "
                            "and Services Tax Act, 2017, in sub-section (9), for the words \"old\", "
                            "the words \"new\" shall be substituted."
                        ),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    output = tmp_path / "events.jsonl"
    review = tmp_path / "review.json"
    compile_act_events(
        registry_path=ROOT / "data/Law/statute_identity_registry.json",
        source_dir=source_dir,
        source_family="cbic-acts",
        target_work="/in/union/acts/cgst-act-2017",
        output=output,
        review_output=review,
        commencement_dir=tmp_path / "missing-notifications",
    )

    events = read_events(output)
    assert len(events) == 3
    event = next(item for item in events if item["source"]["instrument_number"] == "The Central Goods and Services Tax Amendment Act, 2023")
    second_event = next(item for item in events if "SECOND AMENDMENT" in item["source"]["instrument_number"])
    schedule_event = next(item for item in events if "Schedule III" in item["evidence"]["excerpt"])
    assert event["source"]["publication_date"] == "2023-08-18"
    assert event["source"]["text_source"] == "cbic-acts"
    assert event["operation"] == "SPLICE"
    assert event["target"]["component_id"] == "/in/union/acts/cgst-act-2017/section/24"
    assert event["status"] == "needs_review"
    assert schedule_event["target"]["component_id"] == "/in/union/acts/cgst-act-2017"
    assert "target_not_resolved" in schedule_event["review"]["review_reasons"]
    assert schedule_event["payload"]["triage_lane"] == "schedule_lane_pending_baseline"
    assert "schedule_lane_pending_baseline" in schedule_event["review"]["review_reasons"]
    assert second_event["target"]["component_id"] == "/in/union/acts/cgst-act-2017/section/110"


def test_merge_event_ledgers_flags_cross_source_conflicts(tmp_path):
    from src.legal_corpus.event_ledgers import merge_event_ledgers

    def event(event_id, document_id, new_text):
        return {
            "event_id": event_id,
            "event_type": "TEXTUAL_AMENDMENT",
            "operation": "SUBSTITUTE",
            "source": {"document_id": document_id, "record_id": "1"},
            "legal_time": {"applicability_start": "2023-10-01", "commencement_date": "2023-10-01"},
            "target": {
                "work_id": "/in/union/acts/cgst-act-2017",
                "component_id": "/in/union/acts/cgst-act-2017/section/110",
            },
            "payload": {"old_text": "old", "new_text": new_text},
            "evidence": {"source_span": {"start": 0, "end": 10, "text_hash": "a" * 64}, "excerpt": "", "parser_trace": {}},
            "validation": {
                "target_resolved": True,
                "anchor_resolved": True,
                "date_resolved": True,
                "source_span_verified": True,
                "materializable": True,
            },
            "status": "validated",
            "review": {"required": False, "review_reasons": []},
        }

    first = tmp_path / "finance.jsonl"
    second = tmp_path / "cbic.jsonl"
    first.write_text(json.dumps(event("evt_finance", "/finance-act-2023", "new")) + "\n", encoding="utf-8")
    second.write_text(json.dumps(event("evt_cbic", "/cgst-amendment-act-2023", "newer")) + "\n", encoding="utf-8")
    output = tmp_path / "merged.jsonl"
    review = tmp_path / "review.json"

    result = merge_event_ledgers(inputs=[first, second], output=output, review_output=review)

    assert result["conflict_count"] == 1
    merged = read_events(output)
    assert {item["status"] for item in merged} == {"needs_review"}
    assert all(not item["validation"]["materializable"] for item in merged)
    assert all("same_effective_date_conflict" in item["review"]["review_reasons"] for item in merged)


def test_merge_event_ledgers_keeps_single_clean_validated_event_over_candidate(tmp_path):
    from src.legal_corpus.event_ledgers import merge_event_ledgers

    def event(event_id, document_id, status, materializable, required):
        return {
            "event_id": event_id,
            "event_type": "TEXTUAL_AMENDMENT",
            "operation": "INSERT_SIBLING",
            "source": {"document_id": document_id, "record_id": "1"},
            "legal_time": {"applicability_start": "2022-12-26", "commencement_date": "2022-12-26"},
            "target": {
                "work_id": "/in/union/rules/cgst-rules-2017",
                "component_id": "/in/union/rules/cgst-rules-2017/rule/88c",
            },
            "payload": {"label": "88C", "content": event_id},
            "evidence": {"source_span": {"start": 0, "end": 10, "text_hash": "a" * 64}, "excerpt": "", "parser_trace": {}},
            "validation": {
                "target_resolved": materializable,
                "anchor_resolved": materializable,
                "date_resolved": True,
                "source_span_verified": True,
                "materializable": materializable,
            },
            "status": status,
            "review": {"required": required, "review_reasons": [] if not required else ["target_not_resolved"]},
        }

    candidate = tmp_path / "candidate.jsonl"
    backfill = tmp_path / "backfill.jsonl"
    candidate.write_text(
        json.dumps(event("evt_candidate", "/in/union/notifications/cbic/central-tax/2022/26-2022", "needs_review", False, True)) + "\n",
        encoding="utf-8",
    )
    backfill.write_text(
        json.dumps(event("evt_backfill", "/in/union/notifications/cbic/central-tax/2022/26-2022-central-tax", "validated", True, False)) + "\n",
        encoding="utf-8",
    )
    output = tmp_path / "merged.jsonl"

    result = merge_event_ledgers(inputs=[candidate, backfill], output=output)

    assert result["conflict_count"] == 0
    merged = {item["event_id"]: item for item in read_events(output)}
    assert merged["evt_backfill"]["status"] == "validated"
    assert merged["evt_backfill"]["validation"]["materializable"] is True
    assert merged["evt_candidate"]["status"] == "needs_review"


def test_consolidated_fetch_hash_helper():
    from src.legal_corpus.consolidated_fetch import sha256_bytes

    assert sha256_bytes(b"abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def test_consolidated_checkpoint_alias_marks_required_current_rules(tmp_path):
    from src.legal_corpus.consolidated_fetch import alias_downloaded_checkpoint

    source = tmp_path / "misdated"
    source.mkdir()
    (source / "checkpoint.xml").write_text("<akomaNtoso />", encoding="utf-8")
    (source / "checkpoint_components.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "component_id": "/in/union/rules/cgst-rules-2017/rule/31c",
                        "label": "31C",
                        "text": "31C text",
                    }
                ),
                json.dumps(
                    {
                        "component_id": "/in/union/rules/cgst-rules-2017/rule/88c",
                        "label": "88C",
                        "text": "88C text",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (source / "fetch_manifest.json").write_text(
        json.dumps(
            {
                "observed_at": "2026-06-16T13:52:20Z",
                "source_url": "https://taxinformation.cbic.gov.in/api/cbic-rule-msts/download/1000006",
                "source_sha256": "abc",
                "checkpoint_source_type": "taxinformation_section_html",
            }
        ),
        encoding="utf-8",
    )

    manifest = alias_downloaded_checkpoint(
        source_dir=source,
        output_dir=tmp_path / "current-taxinformation",
        checkpoint_date="2026-06-17",
        required_labels=["31C", "88C"],
    )

    assert manifest["ok"] is True
    assert manifest["checkpoint_date"] == "2026-06-17"
    assert manifest["present_required_labels"] == ["31C", "88C"]
    assert manifest["missing_required_labels"] == []
    assert (tmp_path / "current-taxinformation/checkpoint_manifest.json").exists()


def test_consolidated_checkpoint_alias_rebuilds_from_downloaded_taxinformation_html(tmp_path):
    from src.legal_corpus.consolidated_fetch import alias_downloaded_checkpoint

    source = tmp_path / "partial"
    source.mkdir()
    (source / "checkpoint.xml").write_text("<akomaNtoso />", encoding="utf-8")
    (source / "checkpoint_components.jsonl").write_text(
        json.dumps(
            {
                "component_id": "/in/union/rules/cgst-rules-2017/rule/31c",
                "label": "31C",
                "text": "31C text",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (source / "taxinformation_sections.json").write_text(
        json.dumps(
            [
                {
                    "id": 1,
                    "sectionNo": "Rule 31C",
                    "sectionName": "Value of supply of actionable claims in case of casino",
                    "contentFilePath": "tax_repository\\gst\\rules\\cgst_rules\\active\\chapter4\\rule31c_v1.00.html",
                },
                {
                    "id": 2,
                    "sectionNo": "Rule 88C",
                    "sectionName": "Manner of dealing with difference",
                    "contentFilePath": "tax_repository\\gst\\rules\\cgst_rules\\active\\chapter9\\rule88c_v1.00.html",
                },
            ]
        ),
        encoding="utf-8",
    )
    html = source / "html"
    html.mkdir()
    (html / "tax_repository_gst_rules_cgst_rules_active_chapter4_rule31c_v1.00.html").write_text(
        "<html><body><h1>Rule 31C</h1><p>31C text</p></body></html>",
        encoding="utf-8",
    )
    (html / "tax_repository_gst_rules_cgst_rules_active_chapter9_rule88c_v1.00.html").write_text(
        "<html><body><h1>Rule 88C</h1><p>88C text</p></body></html>",
        encoding="utf-8",
    )

    manifest = alias_downloaded_checkpoint(
        source_dir=source,
        output_dir=tmp_path / "current-taxinformation",
        checkpoint_date="2026-06-17",
        required_labels=["31C", "88C"],
    )

    assert manifest["ok"] is True
    assert manifest["rebuilt_from_downloaded_html"] is True
    assert manifest["checkpoint_component_count"] == 2
    assert manifest["present_required_labels"] == ["31C", "88C"]
    assert "rule/88c" in (tmp_path / "current-taxinformation/checkpoint_components.jsonl").read_text(
        encoding="utf-8"
    )


def test_consolidated_fetch_discovers_rules_pdf_from_html(monkeypatch, tmp_path):
    import src.legal_corpus.consolidated_fetch as consolidated_fetch

    responses = {
        "https://example.test/landing": (
            b'<html><a href="/docs/rates.pdf">GST rates</a>'
            b'<a href="/docs/cgst-rules-2017.pdf">Central Goods and Services Tax Rules, 2017</a></html>',
            "text/html",
        ),
        "https://example.test/docs/cgst-rules-2017.pdf": (b"%PDF-1.4 rules", "application/pdf"),
    }

    def fake_fetch(url, timeout, *, verify_tls=True):
        return responses[url]

    monkeypatch.setattr(consolidated_fetch, "_fetch_bytes", fake_fetch)
    monkeypatch.setattr(
        consolidated_fetch,
        "_write_rules_checkpoint",
        lambda source_path, output_dir, observed_at: {
            "checkpoint_path": str(output_dir / "checkpoint.xml"),
            "checkpoint_component_count": 2,
        },
    )

    manifest = consolidated_fetch.fetch_consolidated(
        target_work="/in/union/rules/cgst-rules-2017",
        url="https://example.test/landing",
        output_dir=tmp_path,
    )

    assert manifest["landing_url"] == "https://example.test/landing"
    assert manifest["source_url"] == "https://example.test/docs/cgst-rules-2017.pdf"
    assert manifest["discovered_pdf_url"] == "https://example.test/docs/cgst-rules-2017.pdf"
    assert len(manifest["scanned_pdf_candidates"]) == 2
    assert any(not item["accepted"] and item["url"] == "https://example.test/docs/rates.pdf" for item in manifest["scanned_pdf_candidates"])
    assert (tmp_path / "consolidated.pdf").read_bytes() == b"%PDF-1.4 rules"
    assert manifest["checkpoint_component_count"] == 2


def test_consolidated_fetch_decodes_taxinformation_json_pdf(monkeypatch, tmp_path):
    import src.legal_corpus.consolidated_fetch as consolidated_fetch

    pdf = b"%PDF-1.4 official rules"
    payload = json.dumps({"data": base64.b64encode(pdf).decode("ascii"), "fileName": "rules.pdf"}).encode("utf-8")

    monkeypatch.setattr(
        consolidated_fetch,
        "_fetch_bytes",
        lambda url, timeout, *, verify_tls=True: (payload, "application/json"),
    )
    monkeypatch.setattr(
        consolidated_fetch,
        "_write_rules_checkpoint",
        lambda source_path, output_dir, observed_at: {
            "checkpoint_path": str(output_dir / "checkpoint.xml"),
            "checkpoint_component_count": 1,
        },
    )

    manifest = consolidated_fetch.fetch_consolidated(
        target_work="/in/union/rules/cgst-rules-2017",
        url="https://taxinformation.cbic.gov.in/api/cbic-rule-msts/download/1000006",
        output_dir=tmp_path,
    )

    assert manifest["ok"] is True
    assert manifest["content_type"] == "application/pdf"
    assert manifest["source_metadata"]["json_pdf_wrapper"] is True
    assert manifest["source_metadata"]["fileName"] == "rules.pdf"
    assert (tmp_path / "consolidated.pdf").read_bytes() == pdf
    assert manifest["checkpoint_component_count"] == 1


def test_consolidated_fetch_builds_taxinformation_section_html_checkpoint(monkeypatch, tmp_path):
    import src.legal_corpus.consolidated_fetch as consolidated_fetch

    stale_html_dir = tmp_path / "html"
    stale_html_dir.mkdir()
    (stale_html_dir / "stale.html").write_text("old", encoding="utf-8")
    pdf = b"%PDF-1.4 official rules"
    download_payload = json.dumps({"data": base64.b64encode(pdf).decode("ascii"), "fileName": "rules.pdf"}).encode("utf-8")
    sections = [
        {
            "id": 1000080,
            "sectionNo": "Rule 1",
            "sectionName": "Short title and Commencement",
            "contentFilePath": "tax_repository\\gst\\rules\\cgst_rules\\active\\chapter1\\rule1_v1.00.html",
        },
        {
            "id": 1000081,
            "sectionNo": "Rule 2",
            "sectionName": "Definitions",
            "contentFilePath": "tax_repository\\gst\\rules\\cgst_rules\\active\\chapter1\\rule2_v1.00.html",
        },
        {
            "id": 1000081,
            "sectionNo": "Introduction",
            "sectionName": "Preliminary",
            "contentFilePath": "tax_repository\\gst\\rules\\cgst_rules\\active\\chapter1\\introduction_v1.00.html",
        },
    ]

    def fake_fetch(url, timeout, *, verify_tls=True):
        if url.endswith("/api/cbic-rule-msts/download/1000006"):
            return download_payload, "application/json"
        if url.endswith("/api/cbic-rule-section-msts/viewBySectionAllRules/1000006"):
            return json.dumps(sections).encode("utf-8"), "application/json"
        if url.endswith("/content/html/tax_repository/gst/rules/cgst_rules/active/chapter1/rule1_v1.00.html"):
            return b"<p><strong>Rule 1. Short title.</strong></p><p>(1) These rules may be called test rules.</p>", "text/html"
        raise AssertionError(url)

    monkeypatch.setattr(consolidated_fetch, "_fetch_bytes", fake_fetch)

    manifest = consolidated_fetch.fetch_consolidated(
        target_work="/in/union/rules/cgst-rules-2017",
        url="https://taxinformation.cbic.gov.in/api/cbic-rule-msts/download/1000006",
        output_dir=tmp_path,
        section_limit=1,
        section_concurrency=2,
    )

    assert manifest["ok"] is True
    assert manifest["checkpoint_source_type"] == "taxinformation_section_html"
    assert manifest["checkpoint_component_count"] == 1
    assert manifest["taxinformation_section_limit"] == 1
    assert manifest["taxinformation_section_limit_reached"] is True
    assert manifest["taxinformation_section_concurrency"] == 2
    assert manifest["taxinformation_skipped_count"] == 0
    assert not (tmp_path / "html" / "stale.html").exists()
    checkpoint = (tmp_path / "checkpoint.xml").read_text(encoding="utf-8")
    assert "/in/union/rules/cgst-rules-2017/rule/1" in checkpoint
    assert "These rules may be called test rules" in checkpoint


def test_consolidated_fetch_records_pdf_fetch_error(monkeypatch, tmp_path):
    import urllib.error

    import src.legal_corpus.consolidated_fetch as consolidated_fetch

    def fake_fetch(url, timeout, *, verify_tls=True):
        if url == "https://example.test/landing":
            return (
                b'<html><a href="/docs/cgst-rules-2017.pdf">Central Goods and Services Tax Rules PDF</a></html>',
                "text/html",
            )
        raise urllib.error.URLError("timeout")

    monkeypatch.setattr(consolidated_fetch, "_fetch_bytes", fake_fetch)

    manifest = consolidated_fetch.fetch_consolidated(
        target_work="/in/union/rules/cgst-rules-2017",
        url="https://example.test/landing",
        output_dir=tmp_path,
    )

    assert manifest["discovered_pdf_url"] == "https://example.test/docs/cgst-rules-2017.pdf"
    assert "timeout" in manifest["fetch_error"]
    assert manifest["source_url"] == "https://example.test/landing"
    assert (tmp_path / "consolidated.bin").read_bytes().startswith(b"<html>")
    saved = json.loads((tmp_path / "fetch_manifest.json").read_text(encoding="utf-8"))
    assert saved["fetch_error"] == manifest["fetch_error"]


def test_consolidated_fetch_records_landing_fetch_error(monkeypatch, tmp_path):
    import urllib.error

    import src.legal_corpus.consolidated_fetch as consolidated_fetch

    def fake_fetch(url, timeout, *, verify_tls=True):
        raise urllib.error.URLError("landing timeout")

    monkeypatch.setattr(consolidated_fetch, "_fetch_bytes", fake_fetch)

    manifest = consolidated_fetch.fetch_consolidated(
        target_work="/in/union/rules/cgst-rules-2017",
        url="https://example.test/landing",
        output_dir=tmp_path,
    )

    assert manifest["ok"] is False
    assert "landing timeout" in manifest["fetch_error"]
    assert not (tmp_path / "checkpoint.xml").exists()
    saved = json.loads((tmp_path / "fetch_manifest.json").read_text(encoding="utf-8"))
    assert saved["ok"] is False


def test_consolidated_fetch_marks_html_without_rules_pdf_unusable(monkeypatch, tmp_path):
    import src.legal_corpus.consolidated_fetch as consolidated_fetch

    monkeypatch.setattr(
        consolidated_fetch,
        "_fetch_bytes",
        lambda url, timeout, *, verify_tls=True: (
            b'<html><a href="/docs/rates.pdf">GST rates</a>'
            b'<a href="/docs/advisory.pdf">Please Click here for Advisory.</a></html>',
            "text/html",
        ),
    )

    manifest = consolidated_fetch.fetch_consolidated(
        target_work="/in/union/rules/cgst-rules-2017",
        url="https://example.test/landing",
        output_dir=tmp_path,
    )

    assert manifest["ok"] is False
    assert manifest["checkpoint_error"] == "no_rules_pdf_discovered"
    assert manifest["discovered_pdf_url"] is None
    assert [item["accepted"] for item in manifest["scanned_pdf_candidates"]] == [False, False]
    assert manifest["scanned_pdf_candidates"][0]["url"] == "https://example.test/docs/rates.pdf"
    assert not (tmp_path / "checkpoint.xml").exists()


def test_act_materializer_applies_verified_section_substitute(tmp_path):
    output_dir = tmp_path / "version_history/cgst-act-2017"
    event = {
        "event_id": "evt_finance_acts_test_substitute",
        "event_type": "TEXTUAL_AMENDMENT",
        "operation": "SUBSTITUTE",
        "source": {
            "document_id": "/in/union/acts/source/finance-acts/2025/finance-act-2025",
            "record_id": "999",
            "instrument_number": "Finance Act, 2025",
            "issuing_authority": "/in/authority/parliament-of-india",
            "publication_date": "2025-04-01",
            "source_url": "",
            "source_file_sha256": "0" * 64,
            "source_text_sha256": "1" * 64,
        },
        "legal_time": {
            "commencement_date": "2025-04-01",
            "applicability_start": "2025-04-01",
            "applicability_end": None,
            "retrospective": False,
            "date_basis": "test",
        },
        "system_time": {
            "observed_at": "2026-06-16T00:00:00Z",
            "compiled_at": "2026-06-16T00:00:00Z",
            "compiler_version": "test",
        },
        "target": {
            "work_id": "/in/union/acts/cgst-act-2017",
            "component_id": "/in/union/acts/cgst-act-2017/section/7",
            "anchor_component_id": None,
            "anchor_text": None,
            "anchor_hash": None,
            "anchor_occurrence": None,
        },
        "payload": {"old_text": "supply of goods or services or both", "new_text": "supply of goods, services, or both"},
        "evidence": {
            "source_span": {"start": 0, "end": 10, "text_hash": "2" * 64},
            "excerpt": "test",
            "parser_trace": {"pattern_id": "test", "confidence": 1.0},
        },
        "validation": {
            "target_resolved": True,
            "anchor_resolved": False,
            "date_resolved": True,
            "source_span_verified": True,
            "materializable": True,
        },
        "status": "validated",
        "review": {
            "required": False,
            "review_reasons": [],
            "reviewed_by": None,
            "reviewed_at": None,
        },
    }
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(json.dumps(event) + "\n", encoding="utf-8")

    manifest = materialize_versions(
        target_work="/in/union/acts/cgst-act-2017",
        events_path=events_path,
        registry_path=ROOT / "data/Law/statute_identity_registry.json",
        corpus_dir=tmp_path / "missing-corpus",
        output_dir=output_dir,
        write_snapshots=False,
    )

    assert manifest["applied_count"] == 1
    rows = [json.loads(line) for line in (output_dir / "node_versions.jsonl").read_text().splitlines()]
    section_versions = [row for row in rows if row["component_id"] == "/in/union/acts/cgst-act-2017/section/7"]
    assert len(section_versions) == 2
    assert "supply of goods, services, or both" in section_versions[-1]["text"]


def test_compare_includes_reconciliation_gaps(tmp_path):
    version_dir = tmp_path / "vh"
    version_dir.mkdir()
    component_id = "/in/union/rules/cgst-rules-2017/rule/1"
    (version_dir / "node_versions.jsonl").write_text(
        json.dumps(
            {
                "version_id": "v1",
                "work_id": "/in/union/rules/cgst-rules-2017",
                "component_id": component_id,
                "valid_from": "2017-06-19",
                "valid_to": None,
                "applicability_start": "2017-06-19",
                "applicability_end": None,
                "text": "baseline",
                "text_sha256": "x",
                "created_by_event_id": None,
                "event_chain": [],
                "source_basis": {"type": "baseline"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (version_dir / "materialization_manifest.json").write_text(
        json.dumps({"base_as_of": "2017-06-19"}),
        encoding="utf-8",
    )
    (version_dir / "coverage_gaps.json").write_text(json.dumps({"gaps": []}), encoding="utf-8")
    (version_dir / "reconciliation_report.json").write_text(
        json.dumps(
            {
                "checkpoint_date": "2021-05-18",
                "checkpoint_path": "checkpoint.xml",
                "mismatched_components": [{"component_id": component_id}],
                "missing_components": [],
            }
        ),
        encoding="utf-8",
    )

    result = compare_component_versions(
        component_id,
        from_date="2021-01-01",
        to_date="2021-12-31",
        version_dir=version_dir,
    )

    assert result["coverage"] == "incomplete"
    assert result["reconciliation_gaps"][0]["reason"] == "checkpoint_mismatch"


def test_omlx_enhancement_uses_cache_limit_and_normalizes_candidate(tmp_path, monkeypatch):
    import src.legal_corpus.amendment_events as amendment_events
    from src.legal_corpus.amendment_events import (
        SourceRecord,
        compile_events_from_text,
        enhance_events_with_omlx,
    )
    from src.legal_corpus.omlx_client import OmlxConfig

    source = SourceRecord(
        record={
            "id": "1",
            "no": "1/2025-Central Tax",
            "category": "Central Tax",
            "name": "Seeks to amend the Central Goods and Services Tax Rules, 2017",
        },
        json_path=tmp_path / "source.json",
        text=(
            "NOTIFICATION\n"
            "1. These rules shall come into force on the 1st day of April, 2025.\n"
            "2. In the said rules, in rule 10, after clause (a), the following text shall be inserted."
        ),
        text_source="contentText",
        source_file_sha256="0" * 64,
        source_text_sha256="1" * 64,
        source_url="",
        document_id="/in/union/notifications/cbic/central-tax/2025/1-2025",
        publication_date="2025-04-01",
        commencement_date="2025-04-01",
        date_basis="explicit_commencement_clause",
    )
    corpus_lookup = {
        "/in/union/rules/cgst-rules-2017": {"document": {"text": ""}},
        "/in/union/rules/cgst-rules-2017/rule/10": {"provision": {"text": "rule 10"}},
        "/in/union/rules/cgst-rules-2017/rule/10/subrule/1": {
            "provision": {"text": "approved under rule 9, a certificate"}
        },
    }
    events = compile_events_from_text(
        source,
        target_work="/in/union/rules/cgst-rules-2017",
        corpus_lookup=corpus_lookup,
    )
    assert events and events[0]["status"] == "needs_review"

    calls = {"count": 0}

    def fake_chat_json(prompt, *, config, max_tokens=2048, system=None):
        calls["count"] += 1
        return {
            "operation": "splice",
            "rule": "10",
            "subrule": "1",
            "anchor": "under rule 9,",
            "insert_text": "rule 9A,",
            "position": "after",
            "confidence": 0.91,
        }

    monkeypatch.setattr(amendment_events, "chat_json", fake_chat_json)
    cache = tmp_path / "llm-cache.jsonl"
    enhanced = enhance_events_with_omlx(
        source=source,
        events=events,
        target_work="/in/union/rules/cgst-rules-2017",
        corpus_lookup=corpus_lookup,
        config=OmlxConfig(api_key_env="MISSING"),
        cache_path=cache,
        max_attempts=1,
    )

    assert calls["count"] == 1
    assert enhanced[0]["operation"] == "SPLICE"
    assert enhanced[0]["status"] == "validated"
    assert cache.exists()

    def fail_chat_json(*args, **kwargs):
        raise AssertionError("cache should avoid another OMLX call")

    monkeypatch.setattr(amendment_events, "chat_json", fail_chat_json)
    cached = enhance_events_with_omlx(
        source=source,
        events=events,
        target_work="/in/union/rules/cgst-rules-2017",
        corpus_lookup=corpus_lookup,
        config=OmlxConfig(api_key_env="MISSING"),
        cache_path=cache,
        max_attempts=0,
    )
    assert cached[0]["status"] == "validated"


def test_omlx_enhancement_limit_marks_unattempted(tmp_path):
    from src.legal_corpus.amendment_events import enhance_events_with_omlx
    from src.legal_corpus.omlx_client import OmlxConfig

    event = {
        "event_id": "evt",
        "operation": "UNKNOWN",
        "target": {"component_id": "/in/union/rules/cgst-rules-2017", "work_id": "/in/union/rules/cgst-rules-2017"},
        "evidence": {"source_span": {"start": 0, "end": 4, "text_hash": "x"}, "excerpt": "test"},
        "review": {"review_reasons": ["unparsed_target_work_amendment"]},
        "status": "needs_review",
    }
    source = type(
        "Source",
        (),
        {
            "text": "test",
            "document_id": "/doc",
            "record": {"id": "1", "no": "1/2025-Central Tax", "category": "Central Tax"},
            "publication_date": "2025-01-01",
            "commencement_date": "2025-01-01",
            "date_basis": "test",
            "source_file_sha256": "0" * 64,
            "source_text_sha256": "1" * 64,
            "source_url": "",
            "text_source": "contentText",
        },
    )()

    enhanced = enhance_events_with_omlx(
        source=source,
        events=[event],
        target_work="/in/union/rules/cgst-rules-2017",
        corpus_lookup={},
        config=OmlxConfig(api_key_env="MISSING"),
        max_attempts=0,
    )

    assert "llm_limit_not_attempted" in enhanced[0]["review"]["review_reasons"]


def test_omlx_candidate_can_promote_insert_child(monkeypatch, tmp_path):
    import src.legal_corpus.amendment_events as amendment_events
    from src.legal_corpus.amendment_events import SourceRecord, enhance_events_with_omlx
    from src.legal_corpus.omlx_client import OmlxConfig

    text = "2. In rule 80, after sub-rule (1A), the following sub-rule shall be inserted, namely: (1B) Test inserted text."
    source = SourceRecord(
        record={"id": "1", "no": "1/2025-Central Tax", "category": "Central Tax"},
        json_path=tmp_path / "record.json",
        text=text,
        text_source="contentText",
        source_file_sha256="0" * 64,
        source_text_sha256="1" * 64,
        source_url="",
        document_id="/in/union/notifications/cbic/central-tax/2025/1-2025",
        publication_date="2025-01-01",
        commencement_date="2025-01-01",
        date_basis="fixture",
    )
    event = {
        "event_id": "evt_child",
        "operation": "UNKNOWN",
        "target": {
            "component_id": "/in/union/rules/cgst-rules-2017/rule/80",
            "work_id": "/in/union/rules/cgst-rules-2017",
        },
        "evidence": {
            "source_span": {"start": 0, "end": len(text), "text_hash": "x"},
            "excerpt": text,
        },
        "review": {"review_reasons": ["unparsed_target_work_amendment"]},
        "status": "needs_review",
    }

    def fake_chat_json(prompt, *, config, max_tokens=2048, system=None):
        return {
            "operation": "INSERT_CHILD",
            "rule": "80",
            "subrule": "1B",
            "payload": {
                "node_type": "subrule",
                "label": "1B",
                "content": "(1B) Test inserted text.",
                "position": "after",
                "anchor_component_id": "/in/union/rules/cgst-rules-2017/rule/80/subrule/1a",
                "parent_component_id": "/in/union/rules/cgst-rules-2017/rule/80",
            },
            "confidence": 0.93,
        }

    monkeypatch.setattr(amendment_events, "chat_json", fake_chat_json)
    corpus_lookup = {
        "/in/union/rules/cgst-rules-2017/rule/80/subrule/1a": {
            "provision": {"text": "(1A) Existing subrule."}
        }
    }

    enhanced = enhance_events_with_omlx(
        source=source,
        events=[event],
        target_work="/in/union/rules/cgst-rules-2017",
        corpus_lookup=corpus_lookup,
        config=OmlxConfig(api_key_env="MISSING"),
        cache_path=tmp_path / "cache.jsonl",
        max_attempts=1,
    )

    assert enhanced[0]["operation"] == "INSERT_CHILD"
    assert enhanced[0]["target"]["component_id"] == "/in/union/rules/cgst-rules-2017/rule/80/subrule/1b"
    assert enhanced[0]["target"]["anchor_component_id"] == "/in/union/rules/cgst-rules-2017/rule/80/subrule/1a"
    assert enhanced[0]["status"] == "validated"


def test_omlx_insert_sibling_subrule_shape_normalizes_to_insert_child(monkeypatch, tmp_path):
    import src.legal_corpus.amendment_events as amendment_events
    from src.legal_corpus.amendment_events import SourceRecord, enhance_events_with_omlx
    from src.legal_corpus.omlx_client import OmlxConfig

    text = "2. In rule 59, after sub-rule (5), the following sub-rule shall be inserted, namely: (6) Test inserted text."
    source = SourceRecord(
        record={"id": "1", "no": "1/2025-Central Tax", "category": "Central Tax"},
        json_path=tmp_path / "record.json",
        text=text,
        text_source="contentText",
        source_file_sha256="0" * 64,
        source_text_sha256="1" * 64,
        source_url="",
        document_id="/in/union/notifications/cbic/central-tax/2025/1-2025",
        publication_date="2025-01-01",
        commencement_date="2025-01-01",
        date_basis="fixture",
    )
    event = {
        "event_id": "evt_child_cached_shape",
        "operation": "UNKNOWN",
        "target": {
            "component_id": "/in/union/rules/cgst-rules-2017/rule/59",
            "work_id": "/in/union/rules/cgst-rules-2017",
        },
        "evidence": {
            "source_span": {"start": 0, "end": len(text), "text_hash": "x"},
            "excerpt": text,
        },
        "review": {"review_reasons": ["unparsed_target_work_amendment"]},
        "status": "needs_review",
    }

    def fake_chat_json(prompt, *, config, max_tokens=2048, system=None):
        return {
            "operation": "INSERT_SIBLING",
            "component_id": "/in/union/rules/cgst-rules-2017/rule/59",
            "anchor_text": "after sub-rule (5)",
            "payload": {"insert_text": "(6) Test inserted text.", "position": "after"},
            "confidence": 0.93,
        }

    monkeypatch.setattr(amendment_events, "chat_json", fake_chat_json)
    corpus_lookup = {
        "/in/union/rules/cgst-rules-2017/rule/59/subrule/5": {
            "provision": {"text": "(5) Existing subrule."}
        }
    }

    enhanced = enhance_events_with_omlx(
        source=source,
        events=[event],
        target_work="/in/union/rules/cgst-rules-2017",
        corpus_lookup=corpus_lookup,
        config=OmlxConfig(api_key_env="MISSING"),
        cache_path=tmp_path / "cache.jsonl",
        max_attempts=1,
    )

    assert enhanced[0]["operation"] == "INSERT_CHILD"
    assert enhanced[0]["target"]["component_id"] == "/in/union/rules/cgst-rules-2017/rule/59/subrule/6"
    assert enhanced[0]["status"] == "validated"


def test_omlx_candidate_cannot_materialize_document_scope_substitution(monkeypatch, tmp_path):
    import src.legal_corpus.amendment_events as amendment_events
    from src.legal_corpus.amendment_events import SourceRecord, enhance_events_with_omlx
    from src.legal_corpus.omlx_client import OmlxConfig

    text = (
        "2. In the said rules, in FORM GSTR-4, for the Table, the following Table "
        "shall be substituted, namely: New table text."
    )
    source = SourceRecord(
        record={"id": "1", "no": "1/2025-Central Tax", "category": "Central Tax"},
        json_path=tmp_path / "record.json",
        text=text,
        text_source="contentText",
        source_file_sha256="0" * 64,
        source_text_sha256="1" * 64,
        source_url="",
        document_id="/in/union/notifications/cbic/central-tax/2025/1-2025",
        publication_date="2025-01-01",
        commencement_date="2025-01-01",
        date_basis="fixture",
    )
    event = {
        "event_id": "evt_doc_scope",
        "operation": "UNKNOWN",
        "target": {
            "component_id": "/in/union/rules/cgst-rules-2017",
            "work_id": "/in/union/rules/cgst-rules-2017",
        },
        "evidence": {"source_span": {"start": 0, "end": len(text), "text_hash": "x"}, "excerpt": text},
        "review": {"review_reasons": ["unparsed_target_work_amendment"]},
        "status": "needs_review",
    }

    def fake_chat_json(prompt, *, config, max_tokens=2048, system=None):
        return {
            "operation": "SUBSTITUTE",
            "component_id": "/in/union/rules/cgst-rules-2017",
            "payload": {"old_text": "Table", "new_text": "New table text"},
            "confidence": 0.95,
        }

    monkeypatch.setattr(amendment_events, "chat_json", fake_chat_json)
    enhanced = enhance_events_with_omlx(
        source=source,
        events=[event],
        target_work="/in/union/rules/cgst-rules-2017",
        corpus_lookup={"/in/union/rules/cgst-rules-2017": {"document": {"text": "Table"}}},
        config=OmlxConfig(api_key_env="MISSING"),
        cache_path=tmp_path / "cache.jsonl",
        max_attempts=1,
    )

    assert enhanced[0]["operation"] == "SUBSTITUTE"
    assert enhanced[0]["status"] == "needs_review"
    assert "document_scope_target_not_materializable" in enhanced[0]["review"]["review_reasons"]


def test_omlx_candidate_with_date_to_be_notified_is_not_validated(monkeypatch, tmp_path):
    import src.legal_corpus.amendment_events as amendment_events
    from src.legal_corpus.amendment_events import SourceRecord, enhance_events_with_omlx
    from src.legal_corpus.omlx_client import OmlxConfig

    text = (
        "2. In the said rules, with effect from a date to be notified, in rule 87, "
        "after the words \"common portal\", the words \"as per rule 16A\" shall be inserted."
    )
    source = SourceRecord(
        record={"id": "1", "no": "1/2025-Central Tax", "category": "Central Tax"},
        json_path=tmp_path / "record.json",
        text=text,
        text_source="contentText",
        source_file_sha256="0" * 64,
        source_text_sha256="1" * 64,
        source_url="",
        document_id="/in/union/notifications/cbic/central-tax/2025/1-2025",
        publication_date="2025-01-01",
        commencement_date="2025-01-01",
        date_basis="publication_date_fallback",
    )
    event = {
        "event_id": "evt_future_date",
        "operation": "UNKNOWN",
        "target": {
            "component_id": "/in/union/rules/cgst-rules-2017/rule/87",
            "work_id": "/in/union/rules/cgst-rules-2017",
        },
        "evidence": {"source_span": {"start": 0, "end": len(text), "text_hash": "x"}, "excerpt": text},
        "review": {"review_reasons": ["unparsed_target_work_amendment"]},
        "status": "needs_review",
    }

    def fake_chat_json(prompt, *, config, max_tokens=2048, system=None):
        return {
            "operation": "SPLICE",
            "component_id": "/in/union/rules/cgst-rules-2017/rule/87",
            "anchor_text": "common portal",
            "payload": {"insert_text": "as per rule 16A", "position": "after"},
            "confidence": 0.95,
        }

    monkeypatch.setattr(amendment_events, "chat_json", fake_chat_json)
    enhanced = enhance_events_with_omlx(
        source=source,
        events=[event],
        target_work="/in/union/rules/cgst-rules-2017",
        corpus_lookup={
            "/in/union/rules/cgst-rules-2017/rule/87": {
                "provision": {"text": "Payment on the common portal shall be made."}
            }
        },
        config=OmlxConfig(api_key_env="MISSING"),
        cache_path=tmp_path / "cache.jsonl",
        max_attempts=1,
    )

    assert enhanced[0]["operation"] == "SPLICE"
    assert enhanced[0]["status"] == "needs_review"
    assert not enhanced[0]["validation"]["date_resolved"]
    assert "date_not_resolved" in enhanced[0]["review"]["review_reasons"]


def test_materializer_applies_insert_child_rule_subrule(tmp_path):
    output_dir = tmp_path / "version_history/cgst-rules-2017"
    event = {
        "event_id": "evt_cbic_test_insert_child",
        "event_type": "TEXTUAL_AMENDMENT",
        "operation": "INSERT_CHILD",
        "source": {
            "document_id": "/in/union/notifications/cbic/central-tax/2025/1-2025",
            "record_id": "1",
            "instrument_number": "1/2025-Central Tax",
            "issuing_authority": "/in/authority/cbic",
            "publication_date": "2025-01-01",
            "source_url": "",
            "source_file_sha256": "0" * 64,
            "source_text_sha256": "1" * 64,
        },
        "legal_time": {
            "commencement_date": "2025-01-01",
            "applicability_start": "2025-01-01",
            "applicability_end": None,
            "retrospective": False,
            "date_basis": "fixture",
        },
        "system_time": {
            "observed_at": "2026-06-16T00:00:00Z",
            "compiled_at": "2026-06-16T00:00:00Z",
            "compiler_version": "test",
        },
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": "/in/union/rules/cgst-rules-2017/rule/80/subrule/1b",
            "anchor_component_id": "/in/union/rules/cgst-rules-2017/rule/80/subrule/1a",
            "anchor_text": None,
            "anchor_hash": None,
            "anchor_occurrence": None,
        },
        "payload": {
            "node_type": "subrule",
            "label": "1B",
            "content": "(1B) Test inserted text.",
            "position": "after",
            "anchor_component_id": "/in/union/rules/cgst-rules-2017/rule/80/subrule/1a",
            "parent_component_id": "/in/union/rules/cgst-rules-2017/rule/80",
        },
        "evidence": {
            "source_span": {"start": 0, "end": 10, "text_hash": "2" * 64},
            "excerpt": "test",
            "parser_trace": {"pattern_id": "test", "confidence": 1.0},
        },
        "validation": {
            "target_resolved": True,
            "anchor_resolved": True,
            "date_resolved": True,
            "source_span_verified": True,
            "materializable": True,
        },
        "status": "validated",
        "review": {"required": False, "review_reasons": [], "reviewed_by": None, "reviewed_at": None},
    }
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(json.dumps(event) + "\n", encoding="utf-8")

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=ROOT / "data/Law/statute_identity_registry.json",
        corpus_dir=tmp_path / "missing-corpus",
        output_dir=output_dir,
        write_snapshots=False,
    )

    assert manifest["applied_count"] == 1
    rows = [json.loads(line) for line in (output_dir / "node_versions.jsonl").read_text().splitlines()]
    child_versions = [
        row for row in rows if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/80/subrule/1b"
    ]
    assert len(child_versions) == 1
    assert "Test inserted text" in child_versions[0]["text"]


def test_act_materializer_applies_safe_section_substitute(tmp_path):
    events_path = tmp_path / "act_events.jsonl"
    event = {
        "event_id": "evt_finance_acts_test_substitute",
        "event_type": "TEXTUAL_AMENDMENT",
        "operation": "SUBSTITUTE",
        "source": {
            "document_id": "/test/fa",
            "record_id": "1",
            "instrument_number": "Finance Act test",
            "issuing_authority": "/in/authority/parliament-of-india",
            "publication_date": "2021-06-01",
            "source_url": "",
            "source_file_sha256": "0" * 64,
            "source_text_sha256": "1" * 64,
        },
        "legal_time": {
            "commencement_date": "2021-06-01",
            "applicability_start": "2021-06-01",
            "applicability_end": None,
            "retrospective": False,
            "date_basis": "fixture",
        },
        "system_time": {
            "observed_at": "2026-06-16T00:00:00Z",
            "compiled_at": "2026-06-16T00:00:00Z",
            "compiler_version": "test",
        },
        "target": {
            "work_id": "/in/union/acts/cgst-act-2017",
            "component_id": "/in/union/acts/cgst-act-2017/section/1",
            "anchor_text": None,
        },
        "payload": {"old_text": "whole of India", "new_text": "whole taxable territory"},
        "evidence": {"source_span": {"start": 0, "end": 10, "text_hash": "2" * 64}, "excerpt": "", "parser_trace": {}},
        "validation": {
            "target_resolved": True,
            "anchor_resolved": False,
            "date_resolved": True,
            "source_span_verified": True,
            "materializable": True,
        },
        "status": "validated",
        "review": {"required": False, "review_reasons": []},
    }
    events_path.write_text(json.dumps(event) + "\n", encoding="utf-8")
    out = tmp_path / "act_versions"

    manifest = materialize_versions(
        target_work="/in/union/acts/cgst-act-2017",
        events_path=events_path,
        registry_path=ROOT / "data/Law/statute_identity_registry.json",
        corpus_dir=tmp_path / "missing-corpus",
        output_dir=out,
        write_snapshots=False,
    )

    assert manifest["applied_count"] == 1
    comparison = compare_component_versions(
        "/in/union/acts/cgst-act-2017/section/1",
        from_date="2021-05-31",
        to_date="2021-06-01",
        version_dir=out,
    )
    assert comparison["text_changed"]
    assert "whole taxable territory" in comparison["to_version"]["text"]


def test_compare_marks_reconciliation_gap_incomplete(tmp_path):
    events_path = tmp_path / "empty.jsonl"
    events_path.write_text("", encoding="utf-8")
    out = tmp_path / "rules_versions"
    materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=ROOT / "data/Law/statute_identity_registry.json",
        corpus_dir=tmp_path / "missing-corpus",
        output_dir=out,
        write_snapshots=False,
    )
    report = {
        "checkpoint_date": "2021-05-18",
        "checkpoint_path": "fixture.xml",
        "mismatched_components": [
            {"component_id": "/in/union/rules/cgst-rules-2017/rule/10/subrule/1"}
        ],
        "missing_components": [],
    }
    (out / "reconciliation_report.json").write_text(json.dumps(report), encoding="utf-8")

    comparison = compare_component_versions(
        "/in/union/rules/cgst-rules-2017/rule/10/subrule/1",
        from_date="2020-01-01",
        to_date="2022-01-01",
        version_dir=out,
    )

    assert comparison["coverage"] == "incomplete"
    assert comparison["reconciliation_gaps"][0]["reason"] == "checkpoint_mismatch"


def test_reconciliation_normalizes_padded_rule_ids(tmp_path):
    from src.legal_corpus.reconciliation import reconcile

    version_dir = tmp_path / "versions"
    version_dir.mkdir()
    component_id = "/in/union/rules/cgst-rules-2017/rule/7"
    (version_dir / "node_versions.jsonl").write_text(
        json.dumps(
            {
                "version_id": "v1",
                "work_id": "/in/union/rules/cgst-rules-2017",
                "component_id": component_id,
                "valid_from": "2017-06-19",
                "valid_to": None,
                "applicability_start": "2017-06-19",
                "applicability_end": None,
                "text": "7\nTax rates\nsame text",
                "text_sha256": "x",
                "created_by_event_id": None,
                "event_chain": [],
                "source_basis": {"type": "baseline"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    checkpoint = tmp_path / "checkpoint.xml"
    checkpoint.write_text(
        """
<akomaNtoso>
  <rule refersTo="/in/union/rules/cgst-rules-2017/rule/007">
    <num>7</num>
    <heading>Tax rates</heading>
    <content><p>same text</p></content>
  </rule>
</akomaNtoso>
""",
        encoding="utf-8",
    )

    report = reconcile(
        target_work="/in/union/rules/cgst-rules-2017",
        checkpoint_path=checkpoint,
        checkpoint_date="2021-05-18",
        output=tmp_path / "report.json",
        version_dir=version_dir,
    )

    assert report["matched_components"] == [component_id]
    assert report["mismatched_count"] == 0
    assert report["missing_count"] == 0


def test_reconciliation_includes_checkpoint_source_manifest(tmp_path):
    from src.legal_corpus.reconciliation import reconcile

    version_dir = tmp_path / "versions"
    version_dir.mkdir()
    component_id = "/in/union/rules/cgst-rules-2017/rule/88c"
    (version_dir / "node_versions.jsonl").write_text(
        json.dumps(
            {
                "version_id": "v1",
                "work_id": "/in/union/rules/cgst-rules-2017",
                "component_id": component_id,
                "valid_from": "2022-12-26",
                "valid_to": None,
                "applicability_start": "2022-12-26",
                "applicability_end": None,
                "text": "88C\nManner of dealing with difference\nsame text",
                "text_sha256": "x",
                "created_by_event_id": "evt_88c",
                "event_chain": ["evt_88c"],
                "source_basis": {"type": "event"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    checkpoint_dir = tmp_path / "checkpoint"
    checkpoint_dir.mkdir()
    checkpoint = checkpoint_dir / "checkpoint.xml"
    checkpoint.write_text(
        """
<akomaNtoso>
  <rule refersTo="/in/union/rules/cgst-rules-2017/rule/88c">
    <num>88C</num>
    <heading>Manner of dealing with difference</heading>
    <content><p>same text</p></content>
  </rule>
</akomaNtoso>
""",
        encoding="utf-8",
    )
    (checkpoint_dir / "checkpoint_manifest.json").write_text(
        json.dumps(
            {
                "source_label": "current-cbic-taxinformation",
                "source_url": "https://taxinformation.cbic.gov.in/api/cbic-rule-msts/download/1000006",
                "observed_at": "2026-06-16T13:52:20Z",
                "checkpoint_source_type": "taxinformation_section_html",
                "checkpoint_component_count": 2,
                "required_labels": ["31C", "88C"],
                "present_required_labels": ["31C", "88C"],
                "missing_required_labels": [],
            }
        ),
        encoding="utf-8",
    )

    report = reconcile(
        target_work="/in/union/rules/cgst-rules-2017",
        checkpoint_path=checkpoint,
        checkpoint_date="2026-06-17",
        output=tmp_path / "report.json",
        version_dir=version_dir,
    )

    source_manifest = report["checkpoint_source_manifest"]
    assert source_manifest["source_label"] == "current-cbic-taxinformation"
    assert source_manifest["present_required_labels"] == ["31C", "88C"]
    assert source_manifest["missing_required_labels"] == []


def test_reconciliation_prioritizes_related_coverage_gaps(tmp_path):
    from src.legal_corpus.reconciliation import reconcile

    version_dir = tmp_path / "versions"
    version_dir.mkdir()
    component_id = "/in/union/rules/cgst-rules-2017/rule/7"
    (version_dir / "node_versions.jsonl").write_text(
        json.dumps(
            {
                "version_id": "v1",
                "work_id": "/in/union/rules/cgst-rules-2017",
                "component_id": component_id,
                "valid_from": "2017-06-19",
                "valid_to": None,
                "applicability_start": "2017-06-19",
                "applicability_end": None,
                "text": "7\nTax rates\nreconstructed text",
                "text_sha256": "x",
                "created_by_event_id": None,
                "event_chain": [],
                "source_basis": {"type": "baseline"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (version_dir / "coverage_gaps.json").write_text(
        json.dumps(
            [
                {
                    "event_id": "evt_gap_1",
                    "target": {"component_id": component_id},
                    "review_reasons": ["anchor_not_resolved"],
                }
            ]
        ),
        encoding="utf-8",
    )
    checkpoint = tmp_path / "checkpoint.xml"
    checkpoint.write_text(
        """
<akomaNtoso>
  <rule refersTo="/in/union/rules/cgst-rules-2017/rule/7">
    <num>7</num>
    <heading>Tax rates</heading>
    <content><p>checkpoint text</p></content>
  </rule>
</akomaNtoso>
""",
        encoding="utf-8",
    )

    report = reconcile(
        target_work="/in/union/rules/cgst-rules-2017",
        checkpoint_path=checkpoint,
        checkpoint_date="2021-05-18",
        output=tmp_path / "report.json",
        version_dir=version_dir,
    )

    assert report["priority_review_count"] == 1
    assert report["priority_review_queue"][0]["component_id"] == component_id
    assert report["priority_review_queue"][0]["related_event_ids"] == ["evt_gap_1"]


def test_reconciliation_classifies_missing_rule_with_deferred_commencement(tmp_path):
    from src.legal_corpus.reconciliation import reconcile

    version_dir = tmp_path / "versions"
    version_dir.mkdir()
    (version_dir / "node_versions.jsonl").write_text("", encoding="utf-8")
    component_id = "/in/union/rules/cgst-rules-2017/rule/83b"
    (version_dir / "coverage_gaps.json").write_text(
        json.dumps(
            [
                {
                    "event_id": "evt_83b_rule",
                    "target": {"component_id": "/in/union/forms/gst-pct-06"},
                    "source_document_id": "/in/union/notifications/cbic/central-tax/2019/33-2019",
                    "review_reasons": ["unsupported_form_or_table_mutation"],
                    "excerpt": (
                        "5. In the said rules, after rule 83A, with effect from such date as may be "
                        "notified by the Central Government, the following rule shall be inserted, "
                        'namely:- "83B. Surrender of enrolment."'
                    ),
                }
            ]
        ),
        encoding="utf-8",
    )
    checkpoint = tmp_path / "checkpoint.xml"
    checkpoint.write_text(
        """
<akomaNtoso>
  <rule refersTo="/in/union/rules/cgst-rules-2017/rule/83B">
    <num>83B</num>
    <heading>Surrender of enrolment</heading>
    <content><p>Rule 83B. Surrender of enrolment.</p></content>
  </rule>
</akomaNtoso>
""",
        encoding="utf-8",
    )

    report = reconcile(
        target_work="/in/union/rules/cgst-rules-2017",
        checkpoint_path=checkpoint,
        checkpoint_date="2026-06-17",
        output=tmp_path / "report.json",
        version_dir=version_dir,
    )

    assert report["missing_components"] == [component_id]
    assert report["commencement_blocked_count"] == 1
    row = report["priority_review_queue"][0]
    assert row["component_id"] == component_id
    assert row["related_event_ids"] == ["evt_83b_rule"]
    assert row["blocker"] == "unresolved_commencement"
    assert row["recommended_action"] == "find_commencement_notification_before_materialization"


def test_reconciliation_ignores_synthetic_inserted_rule_placeholders(tmp_path):
    from src.legal_corpus.reconciliation import reconcile

    version_dir = tmp_path / "versions"
    version_dir.mkdir()
    (version_dir / "node_versions.jsonl").write_text("", encoding="utf-8")
    checkpoint = tmp_path / "checkpoint.xml"
    checkpoint.write_text(
        """
<akomaNtoso>
  <rule refersTo="/in/union/rules/cgst-rules-2017/rule/7A">
    <num>7A</num>
    <heading>Rule 7A (inserted by amendment notification)</heading>
    <content><p>[Content to be extracted from amendment notification.]</p></content>
  </rule>
  <rule refersTo="/in/union/rules/cgst-rules-2017/rule/68B">
    <num>68B</num>
    <heading>Rule 68B (inserted by amendment notification)</heading>
    <content><p>[Content to be extracted from amendment notification.]</p></content>
  </rule>
</akomaNtoso>
""",
        encoding="utf-8",
    )

    report = reconcile(
        target_work="/in/union/rules/cgst-rules-2017",
        checkpoint_path=checkpoint,
        checkpoint_date="2026-06-17",
        output=tmp_path / "report.json",
        version_dir=version_dir,
    )

    assert report["missing_components"] == []
    assert report["component_outcomes"] == {}
    assert report["unresolved_reconciliation_count"] == 0


def test_reconciliation_does_not_link_work_level_gap_to_every_component(tmp_path):
    from src.legal_corpus.reconciliation import reconcile

    version_dir = tmp_path / "versions"
    version_dir.mkdir()
    component_id = "/in/union/rules/cgst-rules-2017/rule/116"
    (version_dir / "node_versions.jsonl").write_text(
        json.dumps(
            {
                "version_id": "v1",
                "work_id": "/in/union/rules/cgst-rules-2017",
                "component_id": component_id,
                "valid_from": "2017-06-19",
                "valid_to": None,
                "applicability_start": "2017-06-19",
                "applicability_end": None,
                "text": "116\nOrders\nreconstructed text",
                "text_sha256": "x",
                "created_by_event_id": None,
                "event_chain": [],
                "source_basis": {"type": "baseline"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (version_dir / "coverage_gaps.json").write_text(
        json.dumps(
            [
                {
                    "event_id": "evt_work_level",
                    "target": {"component_id": "/in/union/rules/cgst-rules-2017"},
                    "review_reasons": ["date_not_resolved"],
                    "excerpt": "6. In the said rules, in rule 49, after the third proviso, with effect from a date to be notified later, text shall be inserted.",
                }
            ]
        ),
        encoding="utf-8",
    )
    checkpoint = tmp_path / "checkpoint.xml"
    checkpoint.write_text(
        """
<akomaNtoso>
  <rule refersTo="/in/union/rules/cgst-rules-2017/rule/116">
    <num>116</num>
    <heading>Orders</heading>
    <content><p>checkpoint text</p></content>
  </rule>
</akomaNtoso>
""",
        encoding="utf-8",
    )

    empty_events = tmp_path / "empty_events.jsonl"
    empty_events.write_text("", encoding="utf-8")

    report = reconcile(
        target_work="/in/union/rules/cgst-rules-2017",
        checkpoint_path=checkpoint,
        checkpoint_date="2026-06-17",
        output=tmp_path / "report.json",
        version_dir=version_dir,
        events_path=empty_events,
    )

    row = report["priority_review_queue"][0]
    assert row["component_id"] == component_id
    assert row["related_event_ids"] == []
    assert "blocker" not in row


def test_reconciliation_ignores_reference_refers_to_targets(tmp_path):
    from src.legal_corpus.reconciliation import reconcile

    version_dir = tmp_path / "versions"
    version_dir.mkdir()
    component_id = "/in/union/rules/cgst-rules-2017/rule/7"
    (version_dir / "node_versions.jsonl").write_text(
        json.dumps(
            {
                "version_id": "v1",
                "work_id": "/in/union/rules/cgst-rules-2017",
                "component_id": component_id,
                "valid_from": "2017-06-19",
                "valid_to": None,
                "applicability_start": "2017-06-19",
                "applicability_end": None,
                "text": "7\nTax rates\nsame text",
                "text_sha256": "x",
                "created_by_event_id": None,
                "event_chain": [],
                "source_basis": {"type": "baseline"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    checkpoint = tmp_path / "checkpoint.xml"
    checkpoint.write_text(
        """
<akomaNtoso>
  <rule refersTo="/in/union/rules/cgst-rules-2017/rule/7">
    <num>7</num>
    <heading>Tax rates</heading>
    <content>
      <p>same text <ref refersTo="/in/union/rules/cgst-rules-2017/rule/2020">2020</ref></p>
    </content>
  </rule>
</akomaNtoso>
""",
        encoding="utf-8",
    )

    report = reconcile(
        target_work="/in/union/rules/cgst-rules-2017",
        checkpoint_path=checkpoint,
        checkpoint_date="2021-05-18",
        output=tmp_path / "report.json",
        version_dir=version_dir,
    )

    assert "/in/union/rules/cgst-rules-2017/rule/2020" not in report["missing_components"]


def test_reconciliation_chooses_closest_duplicate_checkpoint_candidate(tmp_path):
    from src.legal_corpus.reconciliation import reconcile

    version_dir = tmp_path / "versions"
    version_dir.mkdir()
    component_id = "/in/union/rules/cgst-rules-2017/rule/1"
    (version_dir / "node_versions.jsonl").write_text(
        json.dumps(
            {
                "version_id": "v1",
                "work_id": "/in/union/rules/cgst-rules-2017",
                "component_id": component_id,
                "valid_from": "2017-06-19",
                "valid_to": None,
                "applicability_start": "2017-06-19",
                "applicability_end": None,
                "text": "1\nShort title\n1. Short title and Commencement.",
                "text_sha256": "x",
                "created_by_event_id": None,
                "event_chain": [],
                "source_basis": {"type": "baseline"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    checkpoint = tmp_path / "checkpoint.xml"
    checkpoint.write_text(
        """
<akomaNtoso>
  <article refersTo="/in/union/rules/cgst-rules-2017/rule/1">
    <num>1</num>
    <content><p>1. Registered persons whose principal place of business is in a table row.</p></content>
  </article>
  <article refersTo="/in/union/rules/cgst-rules-2017/rule/1">
    <num>1</num>
    <heading>Short title</heading>
    <content><p>1. Short title and Commencement.</p></content>
  </article>
</akomaNtoso>
""",
        encoding="utf-8",
    )

    report = reconcile(
        target_work="/in/union/rules/cgst-rules-2017",
        checkpoint_path=checkpoint,
        checkpoint_date="2021-05-18",
        output=tmp_path / "report.json",
        version_dir=version_dir,
    )

    assert report["matched_components"] == [component_id]
    assert report["mismatched_components"] == []


def test_reconciliation_tolerates_reconstructed_heading_line(tmp_path):
    from src.legal_corpus.reconciliation import reconcile

    version_dir = tmp_path / "versions"
    version_dir.mkdir()
    component_id = "/in/union/rules/cgst-rules-2017/rule/101"
    (version_dir / "node_versions.jsonl").write_text(
        json.dumps(
            {
                "version_id": "v1",
                "work_id": "/in/union/rules/cgst-rules-2017",
                "component_id": component_id,
                "valid_from": "2017-06-19",
                "valid_to": None,
                "applicability_start": "2017-06-19",
                "applicability_end": None,
                "text": "101\nAudit\n101. Audit.-The proper officer shall issue a notice.",
                "text_sha256": "x",
                "created_by_event_id": None,
                "event_chain": [],
                "source_basis": {"type": "baseline"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    checkpoint = tmp_path / "checkpoint.xml"
    checkpoint.write_text(
        """
<akomaNtoso>
  <article refersTo="/in/union/rules/cgst-rules-2017/rule/101">
    <num>101</num>
    <content><p>101. Audit.-The proper officer shall issue a notice.</p></content>
  </article>
</akomaNtoso>
""",
        encoding="utf-8",
    )

    report = reconcile(
        target_work="/in/union/rules/cgst-rules-2017",
        checkpoint_path=checkpoint,
        checkpoint_date="2021-05-18",
        output=tmp_path / "report.json",
        version_dir=version_dir,
    )

    assert report["matched_components"] == [component_id]
    assert report["mismatched_components"] == []


def test_reconciliation_separates_format_only_checkpoint_drift(tmp_path):
    from src.legal_corpus.reconciliation import reconcile

    version_dir = tmp_path / "versions"
    version_dir.mkdir()
    component_id = "/in/union/rules/cgst-rules-2017/rule/101"
    (version_dir / "node_versions.jsonl").write_text(
        json.dumps(
            {
                "version_id": "v1",
                "work_id": "/in/union/rules/cgst-rules-2017",
                "component_id": component_id,
                "valid_from": "2017-06-19",
                "valid_to": None,
                "applicability_start": "2017-06-19",
                "applicability_end": None,
                "text": "101\nAudit\n101. Audit.-(1) The proper officer shall issue a notice in FORM GST ADT-01in accordance with the provisions ofsub-section (3). Page 117 of 164",
                "text_sha256": "x",
                "created_by_event_id": None,
                "event_chain": [],
                "source_basis": {"type": "baseline"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    checkpoint = tmp_path / "checkpoint.xml"
    checkpoint.write_text(
        """
<akomaNtoso>
  <article refersTo="/in/union/rules/cgst-rules-2017/rule/101">
    <num>101</num>
    <heading>Audit</heading>
    <content><p>Rule 101. Audit.- (1) The proper officer shall issue a notice in FORM GST ADT-01 in accordance with the provisions of sub-section (3). 1. Inserted vide Notification No.74/2018-CT dated 31.12.2018.</p></content>
  </article>
</akomaNtoso>
""",
        encoding="utf-8",
    )

    report = reconcile(
        target_work="/in/union/rules/cgst-rules-2017",
        checkpoint_path=checkpoint,
        checkpoint_date="2026-06-17",
        output=tmp_path / "report.json",
        version_dir=version_dir,
    )

    assert report["coverage"] == "complete"
    assert report["matched_count"] == 0
    assert report["strict_mismatch_count"] == 1
    assert report["format_only_mismatch_count"] == 1
    assert report["substantive_mismatch_count"] == 0
    assert report["priority_review_queue"] == []
    outcome = report["component_outcomes"][component_id]
    assert outcome["status"] == "format_only_match"
    assert outcome["selected_checkpoint_candidate_count"] == 1


def test_reconciliation_strips_substituted_for_word_footnote_annotations(tmp_path):
    from src.legal_corpus.reconciliation import reconcile

    version_dir = tmp_path / "versions"
    version_dir.mkdir()
    component_id = "/in/union/rules/cgst-rules-2017/rule/130"
    rule_text = (
        "130\nConfidentiality of information\n"
        "130. Confidentiality of information.- (1) Notwithstanding anything contained in "
        "sub-rules (3) and (5) of rule 129 and sub-rule (2) of rule 133, the provisions "
        "of section 11 of the Right to Information Act, 2005 (22 of 2005), shall apply "
        "mutatis mutandis to the disclosure of any information which is provided on a "
        "confidential basis. (2) The Director General of Anti-profiteering may require "
        "the parties providing information on confidential basis to furnish "
        "non-confidential summary thereof and if, in the opinion of the party providing "
        "such information, the said information cannot be summarised, such party may "
        "submit to the Director General of Anti-profiteering a statement of reasons as "
        "to why summarisation is not possible."
    )
    (version_dir / "node_versions.jsonl").write_text(
        json.dumps(
            {
                "version_id": "v1",
                "work_id": "/in/union/rules/cgst-rules-2017",
                "component_id": component_id,
                "valid_from": "2018-06-12",
                "valid_to": None,
                "applicability_start": "2018-06-12",
                "applicability_end": None,
                "text": rule_text,
                "text_sha256": "x",
                "created_by_event_id": "evt_rule_130",
                "event_chain": ["evt_rule_130"],
                "source_basis": {"type": "event"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    checkpoint = tmp_path / "checkpoint.xml"
    checkpoint.write_text(
        """
<akomaNtoso>
  <article refersTo="/in/union/rules/cgst-rules-2017/rule/130">
    <num>130</num>
    <heading>Confidentiality of information</heading>
    <content><p>Rule 130. Confidentiality of information. - (1) Notwithstanding anything contained in sub-rules (3) and (5) of rule 129 and sub-rule (2) of rule 133, the provisions of section 11 of the Right to Information Act, 2005 (22 of 2005), shall apply mutatis mutandis to the disclosure of any information which is provided on a confidential basis. (2) The 1[Director General of Anti-profiteering] may require the parties providing information on confidential basis to furnish Non-confidential summary thereof and if, in the opinion of the party providing such information, the said information cannot be summarised, such party may submit to the 1[Director General of Anti-profiteering] a statement of reasons as to why summarisation is Not possible. 1. Substituted for the word "Safeguards" vide Notification No. 29/2018-CT dated 06.07.2018 w.e.f. 12.06.2018.</p></content>
  </article>
</akomaNtoso>
""",
        encoding="utf-8",
    )

    report = reconcile(
        target_work="/in/union/rules/cgst-rules-2017",
        checkpoint_path=checkpoint,
        checkpoint_date="2026-06-17",
        output=tmp_path / "report.json",
        version_dir=version_dir,
    )

    assert report["format_only_mismatch_count"] == 1
    assert report["substantive_mismatch_count"] == 0
    assert report["component_outcomes"][component_id]["status"] == "format_only_match"


def test_reconciliation_strips_renumbered_subrule_heading_and_inline_marker_noise():
    from src.legal_corpus.reconciliation import _substantive_reconciliation_text

    component_id = "/in/union/rules/cgst-rules-2017/rule/94"
    reconstructed = (
        "94\nOrder sanctioning interest on delayed refunds\n"
        "94. Order sanctioning interest on delayed refunds.- (1) Where any interest "
        "is due and payable to the applicant under section 56, the proper officer "
        "shall make an order along with a payment order in FORM GST RFD-05."
    )
    checkpoint = (
        "94 Order sanctioning interest on delayed refunds Rule 94. Order sanctioning "
        "interest on delayed refunds.- 2[(1)] Where any interest is due and payable "
        "to the applicant undersection 56, the proper officer shall make an order "
        "along with a1[payment order] inFORM GST RFD-05. 1. Substituted "
        "videNotification No. 31/2019 - CTdated 28.06.2019 with effect from "
        "24.09.2019 as notified byNotification No. 42/2019-CTdated 24.09.2019 "
        "for \"payment advice\". 2. Renumbered (w.e.f. 01.10.2023) vide "
        "Notification No. 38/2023 - CTdated 04.08.2023."
    )

    assert _substantive_reconciliation_text(reconstructed, component_id) == _substantive_reconciliation_text(
        checkpoint,
        component_id,
    )


def test_reconciliation_classifies_post_checkpoint_component_as_not_applicable(tmp_path):
    from src.legal_corpus.reconciliation import reconcile

    version_dir = tmp_path / "versions"
    version_dir.mkdir()
    component_id = "/in/union/rules/cgst-rules-2017/rule/31c"
    (version_dir / "node_versions.jsonl").write_text(
        json.dumps(
            {
                "version_id": "v1",
                "work_id": "/in/union/rules/cgst-rules-2017",
                "component_id": component_id,
                "valid_from": "2023-10-01",
                "valid_to": None,
                "applicability_start": "2023-10-01",
                "applicability_end": None,
                "text": "31C\nValue of supply\nfuture text",
                "text_sha256": "x",
                "created_by_event_id": "evt_31c",
                "event_chain": ["evt_31c"],
                "source_basis": {"type": "event"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    checkpoint = tmp_path / "checkpoint.xml"
    checkpoint.write_text(
        """
<akomaNtoso>
  <rule refersTo="/in/union/rules/cgst-rules-2017/rule/31C">
    <num>31C</num>
    <heading>Value of supply</heading>
    <content><p>checkpoint source should not contain this yet</p></content>
  </rule>
</akomaNtoso>
""",
        encoding="utf-8",
    )

    report = reconcile(
        target_work="/in/union/rules/cgst-rules-2017",
        checkpoint_path=checkpoint,
        checkpoint_date="2022-12-26",
        output=tmp_path / "report.json",
        version_dir=version_dir,
    )

    assert report["missing_components"] == []
    assert report["priority_review_queue"] == []
    assert report["component_outcomes"][component_id]["status"] == "post_checkpoint_not_applicable"


def test_reconciliation_classifies_source_backed_omission(tmp_path):
    from src.legal_corpus.reconciliation import reconcile

    version_dir = tmp_path / "versions"
    version_dir.mkdir()
    component_id = "/in/union/rules/cgst-rules-2017/rule/69"
    (version_dir / "node_versions.jsonl").write_text(
        json.dumps(
            {
                "version_id": "omit",
                "work_id": "/in/union/rules/cgst-rules-2017",
                "component_id": component_id,
                "valid_from": "2022-10-01",
                "valid_to": None,
                "applicability_start": "2022-10-01",
                "applicability_end": None,
                "text": "",
                "text_sha256": "x",
                "created_by_event_id": "evt_omit_69",
                "event_chain": ["evt_omit_69"],
                "source_basis": {"type": "event", "operation": "OMIT"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    checkpoint = tmp_path / "checkpoint.xml"
    checkpoint.write_text(
        """
<akomaNtoso>
  <rule refersTo="/in/union/rules/cgst-rules-2017/rule/69">
    <num>69</num>
    <heading>Matching claim</heading>
    <content><p>stale checkpoint text</p></content>
  </rule>
</akomaNtoso>
""",
        encoding="utf-8",
    )

    report = reconcile(
        target_work="/in/union/rules/cgst-rules-2017",
        checkpoint_path=checkpoint,
        checkpoint_date="2022-12-26",
        output=tmp_path / "report.json",
        version_dir=version_dir,
    )

    assert report["missing_components"] == []
    assert report["component_outcomes"][component_id]["status"] == "omitted_correct"


def test_reconciliation_flags_checkpoint_manifest_date_mismatch_and_source_incomplete(tmp_path):
    from src.legal_corpus.reconciliation import reconcile

    version_dir = tmp_path / "versions"
    version_dir.mkdir()
    (version_dir / "node_versions.jsonl").write_text("", encoding="utf-8")
    checkpoint_dir = tmp_path / "checkpoint"
    checkpoint_dir.mkdir()
    checkpoint = checkpoint_dir / "checkpoint.xml"
    checkpoint.write_text("<akomaNtoso />", encoding="utf-8")
    (checkpoint_dir / "checkpoint_manifest.json").write_text(
        json.dumps(
            {
                "checkpoint_date": "2026-06-17",
                "checkpoint_source_type": "taxinformation_section_html",
                "missing_required_labels": ["88C"],
            }
        ),
        encoding="utf-8",
    )

    report = reconcile(
        target_work="/in/union/rules/cgst-rules-2017",
        checkpoint_path=checkpoint,
        checkpoint_date="2022-12-26",
        output=tmp_path / "report.json",
        version_dir=version_dir,
    )

    assert "checkpoint_date_mismatch" in report["checkpoint_source_warnings"]
    assert "checkpoint_source_missing_required_labels" in report["checkpoint_source_warnings"]
    outcome = report["component_outcomes"]["/in/union/rules/cgst-rules-2017/rule/88c"]
    assert outcome["status"] == "checkpoint_source_incomplete"


def test_reconciliation_keeps_real_checkpoint_text_difference_in_queue(tmp_path):
    from src.legal_corpus.reconciliation import reconcile

    version_dir = tmp_path / "versions"
    version_dir.mkdir()
    component_id = "/in/union/rules/cgst-rules-2017/rule/88c"
    (version_dir / "node_versions.jsonl").write_text(
        json.dumps(
            {
                "version_id": "v1",
                "work_id": "/in/union/rules/cgst-rules-2017",
                "component_id": component_id,
                "valid_from": "2022-12-26",
                "valid_to": None,
                "applicability_start": "2022-12-26",
                "applicability_end": None,
                "text": "88C\nManner of dealing with difference\n88C. Manner of dealing with difference.- Statement furnished in FORM GSTR-1 or using the Invoice Furnishing Facility.",
                "text_sha256": "x",
                "created_by_event_id": "evt_88c",
                "event_chain": ["evt_88c"],
                "source_basis": {"type": "event"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (version_dir / "coverage_gaps.json").write_text(
        json.dumps([{"event_id": "evt_gap_88c", "target": {"component_id": component_id}}]),
        encoding="utf-8",
    )
    checkpoint = tmp_path / "checkpoint.xml"
    checkpoint.write_text(
        """
<akomaNtoso>
  <rule refersTo="/in/union/rules/cgst-rules-2017/rule/88c">
    <num>88C</num>
    <heading>Manner of dealing with difference</heading>
    <content><p>Rule 88 C. Manner of dealing with difference.- Statement furnished in FORM GSTR-1, as amended in FORM GSTR-1A if any, or using the Invoice Furnishing Facility.</p></content>
  </rule>
</akomaNtoso>
""",
        encoding="utf-8",
    )

    report = reconcile(
        target_work="/in/union/rules/cgst-rules-2017",
        checkpoint_path=checkpoint,
        checkpoint_date="2026-06-17",
        output=tmp_path / "report.json",
        version_dir=version_dir,
    )

    assert report["coverage"] == "incomplete"
    assert report["format_only_mismatch_count"] == 0
    assert report["substantive_mismatch_count"] == 1
    assert report["priority_review_queue"][0]["component_id"] == component_id
    assert report["priority_review_queue"][0]["related_event_ids"] == ["evt_gap_88c"]


def test_reconciliation_writes_unresolved_audit_and_event_attribution(tmp_path):
    from src.legal_corpus.reconciliation import reconcile

    version_dir = tmp_path / "versions"
    version_dir.mkdir()
    component_id = "/in/union/rules/cgst-rules-2017/rule/40"
    (version_dir / "node_versions.jsonl").write_text(
        json.dumps(
            {
                "version_id": "v1",
                "work_id": "/in/union/rules/cgst-rules-2017",
                "component_id": component_id,
                "valid_from": "2017-06-19",
                "valid_to": None,
                "applicability_start": "2017-06-19",
                "applicability_end": None,
                "text": "40\nManner of claiming credit\nold credit text",
                "text_sha256": "x",
                "created_by_event_id": None,
                "event_chain": [],
                "source_basis": {"type": "baseline"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (version_dir / "coverage_gaps.json").write_text(json.dumps({"coverage_gap_count": 0, "gaps": []}), encoding="utf-8")
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(
        json.dumps(
            {
                "event_id": "evt_rule_40_compound",
                "operation": "UNKNOWN",
                "status": "needs_review",
                "source_document_id": "/in/union/notifications/cbic/central-tax/2020/1-2020",
                "target": {"component_id": "/in/union/rules/cgst-rules-2017"},
                "evidence": {"excerpt": "In the said rules, in rule 40, for the words old credit text, the words new credit text shall be substituted."},
                "review": {"review_reasons": ["compound_block_contains_multiple_amendments"]},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    checkpoint = tmp_path / "checkpoint.xml"
    checkpoint.write_text(
        """
<akomaNtoso>
  <rule refersTo="/in/union/rules/cgst-rules-2017/rule/40">
    <num>40</num>
    <heading>Manner of claiming credit</heading>
    <content><p>40. Manner of claiming credit.- new credit text with additional legally relevant words.</p></content>
  </rule>
</akomaNtoso>
""",
        encoding="utf-8",
    )

    report = reconcile(
        target_work="/in/union/rules/cgst-rules-2017",
        checkpoint_path=checkpoint,
        checkpoint_date="2026-06-17",
        output=tmp_path / "report.json",
        version_dir=version_dir,
        events_path=events_path,
    )

    audit_path = tmp_path / "reconciliation_unresolved_audit.json"
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    assert report["unresolved_reconciliation_audit_path"] == str(audit_path)
    assert audit["unresolved_count"] == 1
    row = audit["rows"][0]
    assert row["component_id"] == component_id
    assert row["audit_class"] == "compound_split_needed"
    assert row["candidate_event_ids"] == ["evt_rule_40_compound"]
    queue_row = report["priority_review_queue"][0]
    assert queue_row["audit_class"] == "compound_split_needed"
    assert queue_row["related_event_ids"] == ["evt_rule_40_compound"]


def test_reconciliation_audit_ignores_form_lane_unknown_noise_for_classification(tmp_path):
    from src.legal_corpus.reconciliation import reconcile

    version_dir = tmp_path / "versions"
    version_dir.mkdir()
    component_id = "/in/union/rules/cgst-rules-2017/rule/85"
    (version_dir / "node_versions.jsonl").write_text(
        json.dumps(
            {
                "component_id": component_id,
                "valid_from": "2017-06-19",
                "text": "85. Electronic liability register old text.",
                "text_sha256": "x",
                "created_by_event_id": None,
                "event_chain": [],
                "source_basis": {"type": "baseline"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (version_dir / "coverage_gaps.json").write_text(json.dumps({"coverage_gap_count": 0, "gaps": []}), encoding="utf-8")
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(
        json.dumps(
            {
                "event_id": "evt_form_noise",
                "operation": "UNKNOWN",
                "status": "needs_review",
                "target": {"component_id": "/in/union/forms/gst-pmt-01"},
                "evidence": {"excerpt": "FORM GST PMT-01 [See rule 85(1)] Electronic Liability Register"},
                "review": {
                    "review_reasons": [
                        "forms_lane_pending_baseline",
                        "target_component_outside_work",
                        "unsupported_form_or_table_mutation",
                        "unsupported_materializer_operation",
                    ]
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    checkpoint = tmp_path / "checkpoint.xml"
    checkpoint.write_text(
        """
<akomaNtoso>
  <rule refersTo="/in/union/rules/cgst-rules-2017/rule/85">
    <num>85</num>
    <heading>Electronic liability register</heading>
    <content><p>85. Electronic liability register materially different text.</p></content>
  </rule>
</akomaNtoso>
""",
        encoding="utf-8",
    )

    report = reconcile(
        target_work="/in/union/rules/cgst-rules-2017",
        checkpoint_path=checkpoint,
        checkpoint_date="2026-06-17",
        output=tmp_path / "report.json",
        version_dir=version_dir,
        events_path=events_path,
    )

    row = json.loads((tmp_path / "reconciliation_unresolved_audit.json").read_text(encoding="utf-8"))["rows"][0]
    assert row["component_id"] == component_id
    assert row["candidate_event_ids"] == ["evt_form_noise"]
    assert row["audit_class"] == "manual_backfill_needed"
    assert report["priority_review_queue"][0]["audit_class"] == "manual_backfill_needed"


def test_reconciliation_prefers_validated_typed_candidate_over_unknown_noise(tmp_path):
    from src.legal_corpus.reconciliation import reconcile

    version_dir = tmp_path / "versions"
    version_dir.mkdir()
    component_id = "/in/union/rules/cgst-rules-2017/rule/86"
    (version_dir / "node_versions.jsonl").write_text(
        json.dumps(
            {
                "version_id": "v1",
                "work_id": "/in/union/rules/cgst-rules-2017",
                "component_id": component_id,
                "valid_from": "2017-06-19",
                "valid_to": None,
                "applicability_start": "2017-06-19",
                "applicability_end": None,
                "text": "86. The supplier shall apply the old substitution rule text.",
                "text_sha256": "x",
                "created_by_event_id": "evt_validated",
                "event_chain": ["evt_noise", "evt_validated"],
                "source_basis": {"type": "event"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (version_dir / "coverage_gaps.json").write_text(json.dumps({"coverage_gap_count": 0, "gaps": []}), encoding="utf-8")
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "event_id": "evt_noise",
                        "operation": "UNKNOWN",
                        "status": "validated",
                        "target": {"component_id": component_id},
                        "evidence": {"excerpt": "Form content for rule 86"},
                        "review": {
                            "required": True,
                            "review_reasons": [
                                "forms_lane_pending_baseline",
                                "unsupported_form_or_table_mutation",
                            ],
                        },
                    }
                ),
                json.dumps(
                    {
                        "event_id": "evt_validated",
                        "operation": "SUBSTITUTE",
                        "status": "validated",
                        "source": {
                            "document_id": "/in/union/notifications/cbic/central-tax/2017/20-2017",
                            "publication_date": "2017-07-10",
                            "record_id": "1000002",
                        },
                        "legal_time": {"applicability_start": "2017-07-10"},
                        "target": {"component_id": component_id, "anchor_text": "old substitution"},
                        "payload": {"old_text": "old substitution", "new_text": "new substitution"},
                        "evidence": {"excerpt": "in rule 86, substitute the words old substitution with new substitution"},
                        "review": {"required": False, "review_reasons": []},
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    checkpoint = tmp_path / "checkpoint.xml"
    checkpoint.write_text(
        """
<akomaNtoso>
  <rule refersTo="/in/union/rules/cgst-rules-2017/rule/86">
    <num>86</num>
    <heading>Late fee</heading>
    <content><p>86. The supplier shall apply the updated substitution rule text.</p></content>
  </rule>
</akomaNtoso>
""",
        encoding="utf-8",
    )

    report = reconcile(
        target_work="/in/union/rules/cgst-rules-2017",
        checkpoint_path=checkpoint,
        checkpoint_date="2026-06-17",
        output=tmp_path / "report.json",
        version_dir=version_dir,
        events_path=events_path,
    )

    row = json.loads((tmp_path / "reconciliation_unresolved_audit.json").read_text(encoding="utf-8"))["rows"][0]
    assert row["component_id"] == component_id
    assert sorted(row["candidate_event_ids"]) == ["evt_noise", "evt_validated"]
    assert row["audit_class"] == "missing_substitution"
    assert report["priority_review_queue"][0]["audit_class"] == "missing_substitution"


def test_reconciliation_ignores_cbic_footnote_wrapped_rule_intro(tmp_path):
    from src.legal_corpus.reconciliation import reconcile

    version_dir = tmp_path / "versions"
    version_dir.mkdir()
    component_id = "/in/union/rules/cgst-rules-2017/rule/88c"
    (version_dir / "node_versions.jsonl").write_text(
        json.dumps(
            {
                "version_id": "v1",
                "work_id": "/in/union/rules/cgst-rules-2017",
                "component_id": component_id,
                "valid_from": "2024-07-10",
                "valid_to": None,
                "applicability_start": "2024-07-10",
                "applicability_end": None,
                "text": "88C\nManner of dealing with difference\n(1) Statement furnished in FORM GSTR-1, as amended in FORM GSTR-1A if any, or using the Invoice Furnishing Facility.",
                "text_sha256": "x",
                "created_by_event_id": "evt_88c_splice",
                "event_chain": ["evt_88c", "evt_88c_splice"],
                "source_basis": {"type": "event"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    checkpoint = tmp_path / "checkpoint.xml"
    checkpoint.write_text(
        """
<akomaNtoso>
  <rule refersTo="/in/union/rules/cgst-rules-2017/rule/88c">
    <num>88C</num>
    <heading>Manner of dealing with difference</heading>
    <content><p>Rule 88C 1[Rule 88 C. Manner of dealing with difference.- (1) Statement furnished in FORM GSTR-12[, as amended in FORM GSTR-1A if any,] or using the Invoice Furnishing Facility.] 1. Inserted vide Notification No. 26/2022-CT dated 26.12.2022. 2. Inserted vide Notification No. 12/2024-CT dated 10.07.2024.</p></content>
  </rule>
</akomaNtoso>
""",
        encoding="utf-8",
    )

    report = reconcile(
        target_work="/in/union/rules/cgst-rules-2017",
        checkpoint_path=checkpoint,
        checkpoint_date="2026-06-17",
        output=tmp_path / "report.json",
        version_dir=version_dir,
    )

    assert report["matched_count"] == 0
    assert report["strict_mismatch_count"] == 1
    assert report["format_only_mismatch_count"] == 1
    assert report["substantive_mismatch_count"] == 0
    assert report["priority_review_queue"] == []


def test_reconciliation_treats_duplicated_rule_intro_as_format_only(tmp_path):
    from src.legal_corpus.reconciliation import reconcile

    version_dir = tmp_path / "versions"
    version_dir.mkdir()
    component_id = "/in/union/rules/cgst-rules-2017/rule/55a"
    (version_dir / "node_versions.jsonl").write_text(
        json.dumps(
            {
                "version_id": "v1",
                "work_id": "/in/union/rules/cgst-rules-2017",
                "component_id": component_id,
                "valid_from": "2018-01-23",
                "valid_to": None,
                "applicability_start": "2018-01-23",
                "applicability_end": None,
                "text": "The person-in-charge of the conveyance shall carry a copy of the tax invoice.",
                "text_sha256": "x",
                "created_by_event_id": "evt_55a",
                "event_chain": ["evt_55a"],
                "source_basis": {"type": "event"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    checkpoint = tmp_path / "checkpoint.xml"
    checkpoint.write_text(
        """
<akomaNtoso>
  <rule refersTo="/in/union/rules/cgst-rules-2017/rule/55A">
    <num>55A</num>
    <heading>Tax Invoice or bill of supply to accompany transport of goods</heading>
    <content><p>1[Rule 55A. Tax Invoice or bill of supply to accompany transport of goods.- The person-in-charge of the conveyance shall carry a copy of the tax invoice.]</p></content>
  </rule>
</akomaNtoso>
""",
        encoding="utf-8",
    )

    report = reconcile(
        target_work="/in/union/rules/cgst-rules-2017",
        checkpoint_path=checkpoint,
        checkpoint_date="2026-06-17",
        output=tmp_path / "report.json",
        version_dir=version_dir,
        events_path=tmp_path / "events.jsonl",
    )

    assert report["component_outcomes"][component_id]["status"] == "format_only_match"
    assert report["substantive_mismatch_count"] == 0
    assert report["priority_review_queue"] == []


def test_reconciliation_ignores_standalone_proviso_label_before_same_proviso(tmp_path):
    from src.legal_corpus.reconciliation import reconcile

    version_dir = tmp_path / "versions"
    version_dir.mkdir()
    component_id = "/in/union/rules/cgst-rules-2017/rule/46a"
    (version_dir / "node_versions.jsonl").write_text(
        json.dumps(
            {
                "version_id": "v1",
                "work_id": "/in/union/rules/cgst-rules-2017",
                "component_id": component_id,
                "valid_from": "2022-12-26",
                "valid_to": None,
                "applicability_start": "2022-12-26",
                "applicability_end": None,
                "text": (
                    "46A.\n"
                    "Invoice-cum-bill of supply\n"
                    "Notwithstanding anything contained in rule 46 or rule 49 or rule 54, "
                    "where a registered person is supplying taxable as well as exempted "
                    "goods or services or both to an unregistered person, a single "
                    '"invoice-cum-bill of supply" may be issued for all such supplies.\n'
                    "provided\n"
                    "Provided that the said single \"invoice-cum-bill of supply\" shall "
                    "contain the particulars as specified under rule 46 or rule 54, as "
                    "the case may be, and rule 49."
                ),
                "text_sha256": "x",
                "created_by_event_id": "evt_46a_proviso",
                "event_chain": ["evt_46a_insert", "evt_46a_proviso"],
                "source_basis": {"type": "event"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    checkpoint = tmp_path / "checkpoint.xml"
    checkpoint.write_text(
        """
<akomaNtoso>
  <rule refersTo="/in/union/rules/cgst-rules-2017/rule/46A">
    <num>46A</num>
    <heading>Invoice-cum-bill of supply</heading>
    <content><p>1[Rule 46A. Invoice-cum-bill of supply. - Notwithstanding anything contained in rule 46 or rule 49 or rule 54, where a registered person is supplying taxable as well as exempted goods or services or both to an unregistered person, a single "invoice-cum-bill of supply" may be issued for all such supplies.] 2[Provided that the said single "invoice-cum-bill of supply" shall contain the particulars as specified under rule 46 or rule 54, as the case may be, and rule 49.] 1. Inserted vide Notification No. 45/2017-CT dated 13.10.2017. 2. Inserted vide Notification No. 26/2022-CT dated 26.12.2022.</p></content>
  </rule>
</akomaNtoso>
""",
        encoding="utf-8",
    )

    report = reconcile(
        target_work="/in/union/rules/cgst-rules-2017",
        checkpoint_path=checkpoint,
        checkpoint_date="2026-06-17",
        output=tmp_path / "report.json",
        version_dir=version_dir,
        events_path=tmp_path / "events.jsonl",
    )

    assert report["component_outcomes"][component_id]["status"] == "format_only_match"
    assert report["substantive_mismatch_count"] == 0
    assert report["priority_review_queue"] == []


def test_reconciliation_ignores_short_label_heading_prefix_even_with_low_similarity(tmp_path):
    from src.legal_corpus.reconciliation import reconcile

    version_dir = tmp_path / "versions"
    version_dir.mkdir()
    component_id = "/in/union/rules/cgst-rules-2017/rule/97a"
    operative_text = (
        "Notwithstanding anything contained in this Chapter, in respect of any process or procedure "
        "prescribed herein, any reference to electronic filing of an application, intimation, reply, "
        "declaration, statement or electronic issuance of a notice, order or certificate on the common "
        "portal shall, in respect of that process or procedure, include manual filing of the said "
        "application, intimation, reply, declaration, statement or issuance of the said notice, order or "
        "certificate in such Forms as appended to these rules."
    )
    (version_dir / "node_versions.jsonl").write_text(
        json.dumps(
            {
                "version_id": "v1",
                "work_id": "/in/union/rules/cgst-rules-2017",
                "component_id": component_id,
                "valid_from": "2017-11-15",
                "valid_to": None,
                "applicability_start": "2017-11-15",
                "applicability_end": None,
                "text": f"97A.\nManual filing and processing\n- {operative_text}",
                "text_sha256": "x",
                "created_by_event_id": "evt_97a",
                "event_chain": ["evt_97a"],
                "source_basis": {"type": "event"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    checkpoint = tmp_path / "checkpoint.xml"
    checkpoint.write_text(
        f"""
<akomaNtoso>
  <rule refersTo="/in/union/rules/cgst-rules-2017/rule/97A">
    <num>97A</num>
    <heading>Manual filing and processing</heading>
    <content><p>1[Rule 97A. Manual filing and processing. - {operative_text}] 1. Inserted vide Notification No.55/2017-CT dated 15.11.2017.</p></content>
  </rule>
</akomaNtoso>
""",
        encoding="utf-8",
    )

    report = reconcile(
        target_work="/in/union/rules/cgst-rules-2017",
        checkpoint_path=checkpoint,
        checkpoint_date="2026-06-17",
        output=tmp_path / "report.json",
        version_dir=version_dir,
        events_path=tmp_path / "events.jsonl",
    )

    assert report["component_outcomes"][component_id]["status"] == "format_only_match"
    assert report["substantive_mismatch_count"] == 0
    assert report["priority_review_queue"] == []


def test_reconciliation_does_not_hide_missing_opening_sentence_as_heading_prefix(tmp_path):
    from src.legal_corpus.reconciliation import reconcile

    version_dir = tmp_path / "versions"
    version_dir.mkdir()
    component_id = "/in/union/rules/cgst-rules-2017/rule/120a"
    suffix_text = (
        "within the time period specified in rule 117, rule 118, rule 119 and rule 120 may revise "
        "such declaration once and submit the revised declaration in FORM GST TRAN-1 electronically "
        "on the common portal within the time period specified in the said rules or such further "
        "period as may be extended by the Commissioner in this behalf."
    )
    (version_dir / "node_versions.jsonl").write_text(
        json.dumps(
            {
                "version_id": "v1",
                "work_id": "/in/union/rules/cgst-rules-2017",
                "component_id": component_id,
                "valid_from": "2017-09-29",
                "valid_to": None,
                "applicability_start": "2017-09-29",
                "applicability_end": None,
                "text": f"1 {suffix_text}",
                "text_sha256": "x",
                "created_by_event_id": "evt_120a",
                "event_chain": ["evt_120a"],
                "source_basis": {"type": "event"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    checkpoint = tmp_path / "checkpoint.xml"
    checkpoint.write_text(
        f"""
<akomaNtoso>
  <rule refersTo="/in/union/rules/cgst-rules-2017/rule/120A">
    <num>120A</num>
    <heading>Revision of declaration in FORM GST TRAN-1</heading>
    <content><p>1[Rule 120A. Revision of declaration in FORM GST TRAN-1 Every registered person who has submitted a declaration electronically in FORM GST TRAN-1 {suffix_text}] 1. Inserted vide Notification No. 34/2017-CT dated 15.09.2017.</p></content>
  </rule>
</akomaNtoso>
""",
        encoding="utf-8",
    )

    report = reconcile(
        target_work="/in/union/rules/cgst-rules-2017",
        checkpoint_path=checkpoint,
        checkpoint_date="2026-06-17",
        output=tmp_path / "report.json",
        version_dir=version_dir,
        events_path=tmp_path / "events.jsonl",
    )

    assert report["component_outcomes"][component_id]["status"] == "true_substantive_mismatch"
    assert report["substantive_mismatch_count"] == 1


def test_reconciliation_ignores_trailing_chapter_heading_bleed(tmp_path):
    from src.legal_corpus.reconciliation import reconcile

    version_dir = tmp_path / "versions"
    version_dir.mkdir()
    component_id = "/in/union/rules/cgst-rules-2017/rule/121"
    operative_text = (
        "The amount credited under sub-rule (3) of rule 117 may be verified and proceedings under "
        "section 73 or section 74 or section 74A, as the case may be shall be initiated in respect "
        "of any credit wrongly availed, whether wholly or partly."
    )
    (version_dir / "node_versions.jsonl").write_text(
        json.dumps(
            {
                "version_id": "v1",
                "work_id": "/in/union/rules/cgst-rules-2017",
                "component_id": component_id,
                "valid_from": "2024-11-01",
                "valid_to": None,
                "applicability_start": "2024-11-01",
                "applicability_end": None,
                "text": f"121\nRecovery of credit wrongly availed\n121. Recovery of credit wrongly availed.- {operative_text} Chapter XV Anti-Profiteering",
                "text_sha256": "x",
                "created_by_event_id": "evt_121",
                "event_chain": ["evt_121"],
                "source_basis": {"type": "event"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    checkpoint = tmp_path / "checkpoint.xml"
    checkpoint.write_text(
        f"""
<akomaNtoso>
  <rule refersTo="/in/union/rules/cgst-rules-2017/rule/121">
    <num>121</num>
    <heading>Recovery of credit wrongly availed</heading>
    <content><p>Rule 121. Recovery of credit wrongly availed.- {operative_text} 1. Substituted vide Notification No. 20/2024-CT dated 08.10.2024.</p></content>
  </rule>
</akomaNtoso>
""",
        encoding="utf-8",
    )

    report = reconcile(
        target_work="/in/union/rules/cgst-rules-2017",
        checkpoint_path=checkpoint,
        checkpoint_date="2026-06-17",
        output=tmp_path / "report.json",
        version_dir=version_dir,
        events_path=tmp_path / "events.jsonl",
    )

    assert report["component_outcomes"][component_id]["status"] == "format_only_match"
    assert report["substantive_mismatch_count"] == 0


def test_reconciliation_strips_inserted_wef_vide_annotation(tmp_path):
    from src.legal_corpus.reconciliation import reconcile

    version_dir = tmp_path / "versions"
    version_dir.mkdir()
    component_id = "/in/union/rules/cgst-rules-2017/rule/47a"
    rule_text = (
        "47A.\n"
        "Time limit for issuing tax invoice in cases where recipient is required to issue invoice\n"
        "Notwithstanding anything contained in rule 47, where an invoice referred to in rule 46 "
        "is required to be issued under clause (f) of sub-section (3) of section 31 by a "
        "registered person, who is liable to pay tax under sub-section (3) or sub-section (4) "
        "of section 9, he shall issue the said invoice within a period of thirty days from the "
        "date of receipt of the said supply of goods or services, or both, as the case may be."
    )
    (version_dir / "node_versions.jsonl").write_text(
        json.dumps(
            {
                "version_id": "v1",
                "work_id": "/in/union/rules/cgst-rules-2017",
                "component_id": component_id,
                "valid_from": "2024-11-01",
                "valid_to": None,
                "applicability_start": "2024-11-01",
                "applicability_end": None,
                "text": rule_text,
                "text_sha256": "x",
                "created_by_event_id": "evt_47a",
                "event_chain": ["evt_47a"],
                "source_basis": {"type": "event"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    checkpoint = tmp_path / "checkpoint.xml"
    checkpoint.write_text(
        """
<akomaNtoso>
  <rule refersTo="/in/union/rules/cgst-rules-2017/rule/47A">
    <num>47A</num>
    <heading>Time limit for issuing tax invoice in cases where recipient is required to issue invoice</heading>
    <content><p>Rule 47A. Time limit for issuing tax invoice in cases where recipient is required to issue invoice.- Notwithstanding anything contained in rule 47, where an invoice referred to in rule 46 is required to be issued under clause (f) of sub-section (3) of section 31 by a registered person, who is liable to pay tax under sub-section (3) or sub-section (4) of section 9, he shall issue the said invoice within a period of thirty days from the date of receipt of the said supply of goods or services, or both, as the case may be. 1. Inserted (w.e.f. 01.11.2024) videNotification No. 20/2024-CT dated 08.10.2024.</p></content>
  </rule>
</akomaNtoso>
""",
        encoding="utf-8",
    )

    report = reconcile(
        target_work="/in/union/rules/cgst-rules-2017",
        checkpoint_path=checkpoint,
        checkpoint_date="2026-06-17",
        output=tmp_path / "report.json",
        version_dir=version_dir,
    )

    assert report["component_outcomes"][component_id]["status"] == "format_only_match"
    assert report["substantive_mismatch_count"] == 0
    assert report["priority_review_queue"] == []


def test_reconciliation_tolerates_act_section_heading_line(tmp_path):
    from src.legal_corpus.reconciliation import reconcile

    version_dir = tmp_path / "versions"
    version_dir.mkdir()
    component_id = "/in/union/acts/cgst-act-2017/section/100"
    (version_dir / "node_versions.jsonl").write_text(
        json.dumps(
            {
                "version_id": "v1",
                "work_id": "/in/union/acts/cgst-act-2017",
                "component_id": component_id,
                "valid_from": "2017-07-01",
                "valid_to": None,
                "applicability_start": "2017-07-01",
                "applicability_end": None,
                "text": "100\nAppeal to Appellate Authority\n100 * Section 100. Appeal to Appellate Authority. - Text.",
                "text_sha256": "x",
                "created_by_event_id": None,
                "event_chain": [],
                "source_basis": {"type": "baseline"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    checkpoint = tmp_path / "checkpoint.xml"
    checkpoint.write_text(
        """
<akomaNtoso>
  <section refersTo="/in/union/acts/cgst-act-2017/section/100">
    <num>100</num>
    <content><p>100 * Section 100. Appeal to Appellate Authority. - Text.</p></content>
  </section>
</akomaNtoso>
""",
        encoding="utf-8",
    )

    report = reconcile(
        target_work="/in/union/acts/cgst-act-2017",
        checkpoint_path=checkpoint,
        checkpoint_date="2023-07-31",
        output=tmp_path / "report.json",
        version_dir=version_dir,
    )

    assert report["matched_components"] == [component_id]
    assert report["mismatched_components"] == []


def test_auto_review_decisions_skip_candidate_when_clean_event_covers_same_slot(tmp_path):
    component_id = "/in/union/rules/cgst-rules-2017/rule/88c"

    def event(event_id, status, materializable, required):
        return {
            "event_id": event_id,
            "event_type": "TEXTUAL_AMENDMENT",
            "operation": "INSERT_SIBLING",
            "source": {"document_id": f"/test/{event_id}", "record_id": event_id},
            "legal_time": {"applicability_start": "2022-12-26", "commencement_date": "2022-12-26"},
            "target": {
                "work_id": "/in/union/rules/cgst-rules-2017",
                "component_id": component_id,
                "anchor_component_id": "/in/union/rules/cgst-rules-2017/rule/88b",
            },
            "payload": {},
            "evidence": {
                "source_span": {"start": 0, "end": 10, "text_hash": "a" * 64},
                "excerpt": (
                    "11. In the said rules, after rule 88B, the following rule shall be inserted, namely:- "
                    "“88C. Manner of dealing with difference.- (1) Complete source text for the new rule.”"
                ),
                "parser_trace": {},
            },
            "validation": {
                "date_resolved": True,
                "source_span_verified": True,
                "materializable": materializable,
            },
            "status": status,
            "review": {"required": required, "review_reasons": [] if not required else ["target_not_resolved"]},
        }

    events_path = tmp_path / "events.jsonl"
    events_path.write_text(
        "\n".join(
            [
                json.dumps(event("evt_candidate", "needs_review", False, True)),
                json.dumps(event("evt_backfill", "validated", True, False)),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    result = generate_auto_review_decisions(
        events_path=events_path,
        output=tmp_path / "auto_review_decisions.json",
    )

    assert result["decision_count"] == 0
    assert result["skipped_existing_count"] == 1


def test_dependency_review_decisions_promote_clear_missing_anchor_insertions(tmp_path):
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(
        json.dumps(
            {
                "event_id": "evt_rule_163",
                "operation": "INSERT_SIBLING",
                "status": "needs_review",
                "source": {"document_id": "/in/union/notifications/cbic/central-tax/2023/38-2023"},
                "legal_time": {
                    "applicability_start": "2023-08-04",
                    "commencement_date": "2023-08-04",
                    "date_basis": "publication_date_fallback",
                },
                "target": {
                    "work_id": "/in/union/rules/cgst-rules-2017",
                    "component_id": "CGST_Rule_96",
                },
                "payload": {},
                "evidence": {
                    "excerpt": (
                        "21. In the said Rules, after rule 162, with effect from the 1st day of October, 2023, "
                        "the following rule, shall be inserted, namely:- "
                        "“163. Consent based sharing of information.- Body text.”"
                    )
                },
                "validation": {"date_resolved": True, "source_span_verified": True},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    gaps_path = tmp_path / "coverage_gaps.json"
    gaps_path.write_text(
        json.dumps(
            {
                "gaps": [
                    {
                        "event_id": "evt_rule_164",
                        "skip_reason": (
                            "apply_failed: Anchor component missing: "
                            "/in/union/rules/cgst-rules-2017/rule/163"
                        ),
                    },
                    {
                        "event_id": "evt_rule_88d",
                        "skip_reason": (
                            "apply_failed: Anchor component missing: "
                            "/in/union/rules/cgst-rules-2017/rule/88c"
                        ),
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    result = generate_dependency_review_decisions(
        events_path=events_path,
        coverage_gaps_path=gaps_path,
        output=tmp_path / "dependency_review_decisions.json",
    )

    assert result["decision_count"] == 1
    assert result["approved_component_ids"] == ["/in/union/rules/cgst-rules-2017/rule/163"]
    assert result["unresolved_count"] == 1
    assert result["unresolved"][0]["missing_component_id"] == "/in/union/rules/cgst-rules-2017/rule/88c"
    decision_payload = json.loads((tmp_path / "dependency_review_decisions.json").read_text(encoding="utf-8"))
    decision = decision_payload["decisions"][0]
    assert decision["event_id"] == "evt_rule_163"
    assert decision["promote"]["operation"] == "INSERT_SIBLING"
    assert decision["promote"]["anchor_component_id"] == "/in/union/rules/cgst-rules-2017/rule/162"
    assert decision["promote"]["component_id"] == "/in/union/rules/cgst-rules-2017/rule/163"
    assert decision["promote"]["applicability_start"] == "2023-10-01"


def test_missing_anchor_backfill_generates_source_backed_31c_and_88c():
    from src.legal_corpus.missing_anchor_backfill import build_missing_anchor_backfill_events

    events = build_missing_anchor_backfill_events(corpus_dir=ROOT / "corpus")
    by_event_id = {event["event_id"]: event for event in events}
    by_component: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        by_component.setdefault(event["target"]["component_id"], []).append(event)
    by_component_operation = {
        (event["target"]["component_id"], event["operation"]): event
        for event in events
    }

    assert "/in/union/rules/cgst-rules-2017/rule/31c" in by_component
    assert "/in/union/rules/cgst-rules-2017/rule/88c" in by_component
    assert "/in/union/rules/cgst-rules-2017/rule/88b" in by_component
    assert "/in/union/rules/cgst-rules-2017/rule/67a" in by_component
    assert "/in/union/rules/cgst-rules-2017/rule/10b" in by_component
    assert "/in/union/rules/cgst-rules-2017/rule/37a" in by_component
    assert "/in/union/rules/cgst-rules-2017/rule/109c" in by_component
    assert "/in/union/rules/cgst-rules-2017/rule/95b" in by_component
    assert "/in/union/rules/cgst-rules-2017/rule/113a" in by_component
    assert "/in/union/rules/cgst-rules-2017/rule/88d" in by_component
    assert "/in/union/rules/cgst-rules-2017/rule/31d" in by_component
    assert "/in/union/rules/cgst-rules-2017/rule/83b" in by_component
    for label in ("69", "70", "71", "72", "73", "74", "75", "76", "77", "79"):
        assert f"/in/union/rules/cgst-rules-2017/rule/{label}" in by_component
    assert "/in/union/rules/cgst-rules-2017/rule/96c" not in by_component
    assert "/in/union/rules/cgst-rules-2017/rule/16a" not in by_component

    rule_31c = by_component["/in/union/rules/cgst-rules-2017/rule/31c"][0]
    assert rule_31c["status"] == "validated"
    assert rule_31c["target"]["anchor_component_id"] == "/in/union/rules/cgst-rules-2017/rule/31b"
    assert rule_31c["legal_time"]["applicability_start"] == "2023-10-01"
    assert "casino" in rule_31c["payload"]["heading"].lower()
    assert rule_31c["validation"]["materializable"] is True

    rule_88c = by_component["/in/union/rules/cgst-rules-2017/rule/88c"][0]
    assert rule_88c["target"]["anchor_component_id"] == "/in/union/rules/cgst-rules-2017/rule/88b"
    assert rule_88c["legal_time"]["applicability_start"] == "2022-12-26"
    assert "difference in liability" in rule_88c["payload"]["heading"].lower()

    rule_88c_splice = by_component_operation[("/in/union/rules/cgst-rules-2017/rule/88c", "SPLICE")]
    assert rule_88c_splice["status"] == "validated"
    assert rule_88c_splice["legal_time"]["applicability_start"] == "2024-07-10"
    assert rule_88c_splice["target"]["anchor_text"] == "FORM GSTR-1"
    assert rule_88c_splice["payload"]["insert_text"] == ", as amended in FORM GSTR-1A if any,"
    assert rule_88c_splice["evidence"]["parser_trace"]["pattern_id"] == "canonical_notification_xml_splice_backfill_v1"

    rule_88d = by_component["/in/union/rules/cgst-rules-2017/rule/88d"][0]
    assert rule_88d["event_id"] == "evt_cbic_291eb5ea9ade9727"
    assert rule_88d["target"]["anchor_component_id"] == "/in/union/rules/cgst-rules-2017/rule/88c"
    assert rule_88d["legal_time"]["applicability_start"] == "2023-08-04"
    assert "auto- generated statement" in rule_88d["payload"]["heading"]
    assert "section 73 or section 74" in rule_88d["payload"]["content"]

    rule_31d = by_component["/in/union/rules/cgst-rules-2017/rule/31d"][0]
    assert rule_31d["event_id"] == "evt_cbic_d8f097a337c10b84"
    assert rule_31d["target"]["anchor_component_id"] == "/in/union/rules/cgst-rules-2017/rule/31c"
    assert rule_31d["legal_time"]["applicability_start"] == "2026-02-01"
    assert "Tax amount" in rule_31d["payload"]["content"]
    assert "retail sale price relates" in rule_31d["payload"]["content"]

    rule_88b = by_component["/in/union/rules/cgst-rules-2017/rule/88b"][0]
    assert rule_88b["event_id"] == "evt_cbic_11b43e13ef68e44e"
    assert rule_88b["legal_time"]["applicability_start"] == "2017-07-01"
    assert rule_88b["legal_time"]["retrospective"] is True

    rule_67a = by_component_operation[("/in/union/rules/cgst-rules-2017/rule/67a", "INSERT_SIBLING")]
    assert rule_67a["status"] == "validated"
    assert rule_67a["target"]["anchor_component_id"] == "/in/union/rules/cgst-rules-2017/rule/67"
    assert rule_67a["legal_time"]["applicability_start"] == "2020-06-08"
    assert rule_67a["legal_time"]["date_basis"] == "commencement_notification_44_2020_rule_3"
    assert "short messaging service" in rule_67a["payload"]["heading"].lower()

    rule_67a_substitute = by_component_operation[("/in/union/rules/cgst-rules-2017/rule/67a", "SUBSTITUTE")]
    assert rule_67a_substitute["event_id"] == "evt_cbic_f1ec09b93bd1ac51"
    assert rule_67a_substitute["status"] == "validated"
    assert rule_67a_substitute["legal_time"]["applicability_start"] == "2020-07-01"
    assert "outward supplies" in rule_67a_substitute["payload"]["heading"].lower()
    assert "FORM GSTR-1" in rule_67a_substitute["payload"]["content"]

    rule_10b = by_component["/in/union/rules/cgst-rules-2017/rule/10b"][0]
    assert rule_10b["status"] == "validated"
    assert rule_10b["target"]["anchor_component_id"] == "/in/union/rules/cgst-rules-2017/rule/10a"
    assert rule_10b["legal_time"]["applicability_start"] == "2022-01-01"
    assert rule_10b["legal_time"]["date_basis"] == "commencement_notification_38_2021_rule_2_subrule_2"
    assert "aadhaar authentication" in rule_10b["payload"]["heading"].lower()

    rule_37a = by_component["/in/union/rules/cgst-rules-2017/rule/37a"][0]
    assert rule_37a["target"]["anchor_component_id"] == "/in/union/rules/cgst-rules-2017/rule/37"
    assert rule_37a["legal_time"]["applicability_start"] == "2022-12-26"
    assert "reversal of input tax credit" in rule_37a["payload"]["heading"].lower()

    rule_83b = by_component["/in/union/rules/cgst-rules-2017/rule/83b"][0]
    assert rule_83b["target"]["anchor_component_id"] == "/in/union/rules/cgst-rules-2017/rule/83a"
    assert rule_83b["legal_time"]["date_basis"] == "deferred_commencement_text_present_in_current_checkpoint"
    assert "surrender of enrolment" in rule_83b["payload"]["heading"].lower()
    assert "FORM GST PCT-06" in rule_83b["payload"]["content"]

    rule_109c = by_component["/in/union/rules/cgst-rules-2017/rule/109c"][0]
    assert rule_109c["target"]["anchor_component_id"] == "/in/union/rules/cgst-rules-2017/rule/109b"
    assert rule_109c["legal_time"]["applicability_start"] == "2022-12-26"
    assert "withdrawal of appeal" in rule_109c["payload"]["heading"].lower()

    rule_95b = by_component["/in/union/rules/cgst-rules-2017/rule/95b"][0]
    assert rule_95b["target"]["anchor_component_id"] == "/in/union/rules/cgst-rules-2017/rule/95"
    assert rule_95b["legal_time"]["applicability_start"] == "2024-07-10"
    assert "canteen stores department" in rule_95b["payload"]["heading"].lower()

    rule_113a = by_component["/in/union/rules/cgst-rules-2017/rule/113a"][0]
    assert rule_113a["target"]["anchor_component_id"] == "/in/union/rules/cgst-rules-2017/rule/113"
    assert rule_113a["legal_time"]["applicability_start"] == "2024-07-10"
    assert "appellate tribunal" in rule_113a["payload"]["heading"].lower()

    omitted_rule = by_component_operation[("/in/union/rules/cgst-rules-2017/rule/69", "OMIT")]
    assert omitted_rule["status"] == "validated"
    assert omitted_rule["payload"]["whole_component"] is True
    assert omitted_rule["legal_time"]["applicability_start"] == "2022-10-01"
    assert omitted_rule["legal_time"]["date_basis"] == "publication_date_general_commencement_clause"
    assert omitted_rule["evidence"]["parser_trace"]["pattern_id"] == (
        "canonical_notification_xml_whole_rule_omit_backfill_v1"
    )
    assert "rules 69, 70, 71, 72, 73, 74, 75, 76, 77 and 79" in omitted_rule["evidence"]["excerpt"]

    assert by_event_id["evt_cbic_8739d092c4f28ae8"]["payload"]["match_occurrence"] == 1
    assert by_event_id["evt_cbic_8739d092c4f28ae8"]["target"]["component_id"].endswith("/rule/54/subrule/2")
    assert by_event_id["evt_cbic_177fad0ed5c5d04f"]["payload"]["noop_if_already_reflected"] is True
    assert by_event_id["evt_cbic_177fad0ed5c5d04f"]["legal_time"]["applicability_start"] == "2017-07-01"
    assert by_event_id["evt_cbic_e9f5c2ee072808ab"]["payload"]["noop_if_already_reflected"] is True
    assert by_event_id["evt_cbic_1b5dc6be014a997a"]["payload"]["noop_if_already_reflected"] is True
    assert by_event_id["evt_cbic_030358a06f4f6e69"]["payload"]["noop_if_already_reflected"] is True
    assert by_event_id["evt_cbic_8d1e9b596d074bea"]["payload"]["match_occurrence"] == 1
    assert by_event_id["evt_cbic_a8b48891c38dc61e"]["target"]["component_id"].endswith("/rule/9/subrule/1")


def test_materializer_repair_flags_are_explicit():
    from src.legal_corpus.version_snapshots import _omit_text_from_paragraph, _replace_unique_text

    original = "may issue a tax invoice or a consolidated tax invoice."
    event = {"payload": {"match_occurrence": 1}}
    assert (
        _replace_unique_text(original, "tax invoice", "consolidated tax invoice", event)
        == "may issue a consolidated tax invoice or a consolidated tax invoice."
    )

    reflected = "within the period specified in rule 117 or such further period as extended by the Commissioner"
    event = {"payload": {"noop_if_already_reflected": True}}
    assert _replace_unique_text(reflected, "ninety days of the appointed day", reflected, event) == reflected

    omit_event = {"payload": {"match_occurrence": 2}}
    assert _omit_text_from_paragraph(
        "outward and inward supplies and inward supplies",
        "and inward",
        omit_event,
    ) == "outward and inward supplies  supplies"

    noop_omit = {"payload": {"noop_if_already_reflected": True}}
    assert _omit_text_from_paragraph("FORM GSTR-3B balance", "FORM GSTR-2 and", noop_omit) == "FORM GSTR-3B balance"


def test_materializer_retries_retrospective_insert_after_later_anchor(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso>
  <act>
    <body>
      <chapter>
        <num>IX</num>
        <heading>Payment of Tax</heading>
        <article refersTo="/in/union/rules/cgst-rules-2017/rule/88">
          <num>88</num>
          <heading>Liability</heading>
          <content><p>88. Liability to pay tax.</p></content>
        </article>
      </chapter>
    </body>
  </act>
</akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-06-19"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")

    def insert_event(event_id, component, anchor, label, date):
        return {
            "event_id": event_id,
            "event_type": "TEXTUAL_AMENDMENT",
            "operation": "INSERT_SIBLING",
            "source": {
                "document_id": f"/test/{event_id}",
                "record_id": event_id,
                "instrument_number": event_id,
                "issuing_authority": "/in/authority/cbic",
                "publication_date": "2022-01-01",
                "source_url": "",
                "source_file_sha256": "0" * 64,
                "source_text_sha256": "1" * 64,
            },
            "legal_time": {
                "commencement_date": date,
                "applicability_start": date,
                "applicability_end": None,
                "retrospective": date < "2022-01-01",
                "date_basis": "fixture",
            },
            "system_time": {
                "observed_at": "2026-06-16T00:00:00Z",
                "compiled_at": "2026-06-16T00:00:00Z",
                "compiler_version": "test",
            },
            "target": {
                "work_id": "/in/union/rules/cgst-rules-2017",
                "component_id": component,
                "anchor_component_id": anchor,
                "anchor_text": anchor,
                "anchor_hash": "2" * 64,
                "anchor_occurrence": 1,
            },
            "payload": {
                "node_type": "rule",
                "label": label,
                "heading": f"Rule {label}",
                "content": f"Inserted rule {label}.",
                "position": "after",
                "anchor_component_id": anchor,
            },
            "evidence": {
                "source_span": {"start": 0, "end": 10, "text_hash": "3" * 64},
                "excerpt": f"rule {label}",
                "parser_trace": {"pattern_id": "fixture", "confidence": 1.0},
            },
            "validation": {
                "target_resolved": True,
                "anchor_resolved": True,
                "date_resolved": True,
                "source_span_verified": True,
                "materializable": True,
            },
            "status": "validated",
            "review": {"required": False, "review_reasons": []},
        }

    events = [
        insert_event(
            "evt_88b",
            "/in/union/rules/cgst-rules-2017/rule/88b",
            "/in/union/rules/cgst-rules-2017/rule/88a",
            "88B",
            "2017-07-01",
        ),
        insert_event(
            "evt_88a",
            "/in/union/rules/cgst-rules-2017/rule/88a",
            "/in/union/rules/cgst-rules-2017/rule/88",
            "88A",
            "2019-03-29",
        ),
    ]
    events_path = tmp_path / "events.jsonl"
    events_path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")

    out = tmp_path / "versions"
    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "missing-corpus",
        output_dir=out,
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["applied_count"] == 2
    assert not any(gap["event_id"] == "evt_88b" for gap in json.loads((out / "coverage_gaps.json").read_text())["gaps"])
    rows = [json.loads(line) for line in (out / "node_versions.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any(row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/88a" for row in rows)
    assert any(row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/88b" for row in rows)


def test_codex_review_decisions_approve_exact_text_substitute(tmp_path):
    component_id = "/in/union/rules/cgst-rules-2017/rule/125"
    events_path = tmp_path / "events.jsonl"
    event = {
        "event_id": "evt_exact_substitute",
        "event_type": "TEXTUAL_AMENDMENT",
        "operation": "SUBSTITUTE",
        "source": {
            "document_id": "/test/notification",
            "record_id": "1",
            "instrument_number": "1/2020-Central Tax",
            "issuing_authority": "/in/authority/cbic",
            "publication_date": "2020-01-01",
            "source_url": "",
            "source_file_sha256": "0" * 64,
            "source_text_sha256": "1" * 64,
        },
        "legal_time": {
            "commencement_date": "2020-01-01",
            "applicability_start": "2020-01-01",
            "applicability_end": None,
            "retrospective": False,
            "date_basis": "fixture",
        },
        "system_time": {
            "observed_at": "2026-06-16T00:00:00Z",
            "compiled_at": "2026-06-16T00:00:00Z",
            "compiler_version": "test",
        },
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": component_id,
            "anchor_text": "Directorate General of Safeguards",
        },
        "payload": {"old_text": "Directorate General of Safeguards", "new_text": "Directorate General of Anti-profiteering"},
        "evidence": {
            "source_span": {"start": 0, "end": 10, "text_hash": "2" * 64},
            "excerpt": "for the words ... shall be substituted",
            "parser_trace": {"pattern_id": "fixture", "confidence": 1.0},
        },
        "validation": {
            "target_resolved": True,
            "anchor_resolved": False,
            "date_resolved": True,
            "source_span_verified": True,
            "materializable": False,
        },
        "status": "needs_review",
        "review": {"required": True, "review_reasons": ["anchor_not_resolved"]},
    }
    events_path.write_text(json.dumps(event) + "\n", encoding="utf-8")
    node_versions = tmp_path / "node_versions.jsonl"
    node_versions.write_text(
        json.dumps(
            {
                "component_id": component_id,
                "applicability_start": "2017-06-19",
                "applicability_end": None,
                "valid_from": "2017-06-19",
                "valid_to": None,
                "text": "125\nAuthority\nThe Directorate General of Safeguards shall assist.",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    decisions_path = tmp_path / "codex_review_decisions.json"

    result = generate_codex_review_decisions(
        events_path=events_path,
        node_versions_path=node_versions,
        output=decisions_path,
    )

    assert result["decision_count"] == 1
    payload = json.loads(decisions_path.read_text(encoding="utf-8"))
    decision = payload["decisions"][0]
    assert decision["reviewed_by"] == "codex-review-decisions-v1"
    assert decision["promote"]["strategy"] == "exact_substitute_payload"

    promoted = tmp_path / "promoted.jsonl"
    apply_review_decisions(events_path=events_path, decisions_path=decisions_path, output=promoted)
    promoted_event = json.loads(promoted.read_text(encoding="utf-8").strip())
    assert promoted_event["status"] == "validated"
    assert promoted_event["payload"]["old_text"] == "Directorate General of Safeguards"
    assert promoted_event["target"]["anchor_resolved_by_codex"] is True


def test_codex_review_decisions_approve_already_reflected_substitute(tmp_path):
    component_id = "/in/union/rules/cgst-rules-2017/rule/10/subrule/4"
    event = {
        "event_id": "evt_already_reflected_substitute",
        "event_type": "TEXTUAL_AMENDMENT",
        "operation": "SUBSTITUTE",
        "source": {
            "document_id": "/test/notification",
            "record_id": "1",
            "instrument_number": "1/2017-Central Tax",
            "issuing_authority": "/in/authority/cbic",
            "publication_date": "2017-06-22",
            "source_url": "",
            "source_file_sha256": "0" * 64,
            "source_text_sha256": "1" * 64,
        },
        "legal_time": {
            "commencement_date": "2017-06-22",
            "applicability_start": "2017-06-22",
            "applicability_end": None,
            "retrospective": False,
            "date_basis": "fixture",
        },
        "system_time": {
            "observed_at": "2026-06-16T00:00:00Z",
            "compiled_at": "2026-06-16T00:00:00Z",
            "compiler_version": "test",
        },
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": component_id,
            "anchor_text": "digitally signed",
        },
        "payload": {"old_text": "digitally signed", "new_text": "duly signed or verified through electronic verification code"},
        "evidence": {
            "source_span": {"start": 0, "end": 10, "text_hash": "2" * 64},
            "excerpt": "for the words digitally signed ... shall be substituted",
            "parser_trace": {"pattern_id": "fixture", "confidence": 1.0},
        },
        "validation": {
            "target_resolved": True,
            "anchor_resolved": False,
            "date_resolved": True,
            "source_span_verified": True,
            "materializable": False,
        },
        "status": "needs_review",
        "review": {"required": True, "review_reasons": ["anchor_not_resolved"]},
    }
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(json.dumps(event) + "\n", encoding="utf-8")
    node_versions = tmp_path / "node_versions.jsonl"
    node_versions.write_text(
        json.dumps(
            {
                "component_id": component_id,
                "applicability_start": "2017-06-19",
                "applicability_end": None,
                "valid_from": "2017-06-19",
                "valid_to": None,
                "text": "(4) Every certificate shall be duly signed or verified through electronic verification code.",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    decisions_path = tmp_path / "codex_review_decisions.json"

    result = generate_codex_review_decisions(
        events_path=events_path,
        node_versions_path=node_versions,
        output=decisions_path,
        existing_decision_paths=[],
    )

    assert result["new_decision_count"] == 1
    decision = json.loads(decisions_path.read_text(encoding="utf-8"))["decisions"][0]
    assert decision["promote"]["strategy"] == "already_reflected_substitute_payload"
    assert decision["promote"]["noop_if_already_reflected"] is True

    promoted = tmp_path / "promoted.jsonl"
    apply_review_decisions(events_path=events_path, decisions_path=decisions_path, output=promoted)
    promoted_event = json.loads(promoted.read_text(encoding="utf-8").strip())
    assert promoted_event["status"] == "validated"
    assert promoted_event["payload"]["noop_if_already_reflected"] is True


def test_codex_review_decisions_approve_substitute_when_old_text_only_inside_replacement(tmp_path):
    component_id = "/in/union/rules/cgst-rules-2017/rule/13/subrule/4"
    event = {
        "event_id": "evt_old_inside_replacement_substitute",
        "event_type": "TEXTUAL_AMENDMENT",
        "operation": "SUBSTITUTE",
        "source": {
            "document_id": "/test/notification",
            "record_id": "1",
            "instrument_number": "1/2017-Central Tax",
            "issuing_authority": "/in/authority/cbic",
            "publication_date": "2017-06-22",
            "source_url": "",
            "source_file_sha256": "0" * 64,
            "source_text_sha256": "1" * 64,
        },
        "legal_time": {
            "commencement_date": "2017-06-22",
            "applicability_start": "2017-06-22",
            "applicability_end": None,
            "retrospective": True,
            "date_basis": "fixture",
        },
        "system_time": {"compiled_at": "2026-06-16T00:00:00Z"},
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": component_id,
            "anchor_text": "signed",
        },
        "payload": {
            "old_text": "signed",
            "new_text": "duly signed or verified through electronic verification code",
        },
        "evidence": {
            "source_span": {"start": 0, "end": 10, "text_hash": "2" * 64},
            "excerpt": "for the word signed, the words duly signed ... shall be substituted",
            "parser_trace": {"pattern_id": "fixture", "confidence": 1.0},
        },
        "validation": {
            "target_resolved": True,
            "anchor_resolved": True,
            "date_resolved": True,
            "source_span_verified": True,
            "materializable": False,
        },
        "status": "needs_review",
        "review": {"required": True, "review_reasons": ["unsafe_generic_substitution_anchor"]},
    }
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(json.dumps(event) + "\n", encoding="utf-8")
    node_versions = tmp_path / "node_versions.jsonl"
    node_versions.write_text(
        json.dumps(
            {
                "component_id": component_id,
                "applicability_start": "2017-06-19",
                "applicability_end": None,
                "valid_from": "2017-06-19",
                "valid_to": None,
                "text": "(4) The application shall be duly signed or verified through electronic verification code.",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    decisions_path = tmp_path / "codex_review_decisions.json"

    result = generate_codex_review_decisions(
        events_path=events_path,
        node_versions_path=node_versions,
        output=decisions_path,
        existing_decision_paths=[],
    )

    assert result["new_decision_count"] == 1
    decision = json.loads(decisions_path.read_text(encoding="utf-8"))["decisions"][0]
    assert decision["promote"]["strategy"] == "already_reflected_substitute_payload"
    assert decision["promote"]["noop_if_already_reflected"] is True


def test_codex_review_decisions_approve_exact_splice_payload(tmp_path):
    component_id = "/in/union/rules/cgst-rules-2017/rule/92/subrule/4"
    event = {
        "event_id": "evt_exact_splice",
        "event_type": "TEXTUAL_AMENDMENT",
        "operation": "SPLICE",
        "source": {
            "document_id": "/in/union/notifications/cbic/central-tax/2020/fixture",
            "record_id": "1",
            "instrument_number": "1/2020-Central Tax",
            "issuing_authority": "/in/authority/cbic",
            "publication_date": "2020-01-01",
            "source_url": "",
            "source_file_sha256": "0" * 64,
            "source_text_sha256": "1" * 64,
        },
        "legal_time": {
            "commencement_date": "2020-01-01",
            "applicability_start": "2020-01-01",
            "applicability_end": None,
            "retrospective": False,
            "date_basis": "fixture",
        },
        "system_time": {
            "observed_at": "2026-06-16T00:00:00Z",
            "compiled_at": "2026-06-16T00:00:00Z",
            "compiler_version": "test",
        },
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": component_id,
            "anchor_text": "application for refund",
        },
        "payload": {
            "insert_text": "on the basis of a consolidated payment advice",
            "position": "after",
        },
        "evidence": {
            "source_span": {"start": 0, "end": 10, "text_hash": "2" * 64},
            "excerpt": "after the words application for refund, the words ... shall be inserted",
            "parser_trace": {"pattern_id": "fixture", "confidence": 1.0},
        },
        "validation": {
            "target_resolved": True,
            "anchor_resolved": False,
            "date_resolved": True,
            "source_span_verified": True,
            "materializable": False,
        },
        "status": "needs_review",
        "review": {"required": True, "review_reasons": ["anchor_not_resolved", "llm_candidate_not_validated"]},
    }
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(json.dumps(event) + "\n", encoding="utf-8")
    node_versions = tmp_path / "node_versions.jsonl"
    node_versions.write_text(
        json.dumps(
            {
                "component_id": component_id,
                "applicability_start": "2017-06-19",
                "applicability_end": None,
                "valid_from": "2017-06-19",
                "valid_to": None,
                "text": "(4) The proper officer may issue an order after examining the application for refund.",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    decisions_path = tmp_path / "codex_review_decisions.json"

    result = generate_codex_review_decisions(
        events_path=events_path,
        node_versions_path=node_versions,
        output=decisions_path,
        existing_decision_paths=[],
    )

    assert result["new_decision_count"] == 1
    decision = json.loads(decisions_path.read_text(encoding="utf-8"))["decisions"][0]
    assert decision["promote"]["strategy"] == "exact_splice_payload"
    assert decision["promote"]["anchor_text"] == "application for refund"

    promoted = tmp_path / "promoted.jsonl"
    apply_review_decisions(events_path=events_path, decisions_path=decisions_path, output=promoted)
    promoted_event = json.loads(promoted.read_text(encoding="utf-8").strip())
    assert promoted_event["status"] == "validated"
    assert promoted_event["operation"] == "SPLICE"
    assert promoted_event["payload"]["insert_text"] == "on the basis of a consolidated payment advice"
    assert promoted_event["target"]["anchor_occurrence"] == 1
    assert promoted_event["target"]["anchor_resolved_by_codex"] is True


def test_codex_review_decisions_approve_already_reflected_splice(tmp_path):
    component_id = "/in/union/rules/cgst-rules-2017/rule/128/subrule/1"
    event = {
        "event_id": "evt_already_reflected_splice",
        "event_type": "TEXTUAL_AMENDMENT",
        "operation": "SPLICE",
        "source": {
            "document_id": "/in/union/notifications/cbic/central-tax/2019/fixture",
            "record_id": "1",
            "instrument_number": "1/2019-Central Tax",
            "issuing_authority": "/in/authority/cbic",
            "publication_date": "2019-06-28",
            "source_url": "",
            "source_file_sha256": "0" * 64,
            "source_text_sha256": "1" * 64,
        },
        "legal_time": {
            "commencement_date": "2019-06-28",
            "applicability_start": "2019-06-28",
            "applicability_end": None,
            "retrospective": False,
            "date_basis": "fixture",
        },
        "system_time": {
            "observed_at": "2026-06-16T00:00:00Z",
            "compiled_at": "2026-06-16T00:00:00Z",
            "compiler_version": "test",
        },
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": component_id,
            "anchor_text": "receipt of a written application,",
        },
        "payload": {
            "insert_text": "or within such extended period not exceeding a further period of one month",
            "position": "after",
        },
        "evidence": {
            "source_span": {"start": 0, "end": 10, "text_hash": "2" * 64},
            "excerpt": "after the words receipt of a written application ... shall be inserted",
            "parser_trace": {"pattern_id": "fixture", "confidence": 1.0},
        },
        "validation": {
            "target_resolved": True,
            "anchor_resolved": False,
            "date_resolved": True,
            "source_span_verified": True,
            "materializable": False,
        },
        "status": "needs_review",
        "review": {"required": True, "review_reasons": ["anchor_not_resolved"]},
    }
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(json.dumps(event) + "\n", encoding="utf-8")
    node_versions = tmp_path / "node_versions.jsonl"
    node_versions.write_text(
        json.dumps(
            {
                "component_id": component_id,
                "applicability_start": "2017-06-19",
                "applicability_end": None,
                "valid_from": "2017-06-19",
                "valid_to": None,
                "text": (
                    "(1) The Standing Committee shall, within a period of two months from the date "
                    "of the receipt of a written application or within such extended period not "
                    "exceeding a further period of one month."
                ),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    decisions_path = tmp_path / "codex_review_decisions.json"

    result = generate_codex_review_decisions(
        events_path=events_path,
        node_versions_path=node_versions,
        output=decisions_path,
        existing_decision_paths=[],
    )

    assert result["new_decision_count"] == 1
    decision = json.loads(decisions_path.read_text(encoding="utf-8"))["decisions"][0]
    assert decision["promote"]["strategy"] == "already_reflected_splice_payload"
    assert decision["promote"]["noop_if_already_reflected"] is True

    promoted = tmp_path / "promoted.jsonl"
    apply_review_decisions(events_path=events_path, decisions_path=decisions_path, output=promoted)
    promoted_event = json.loads(promoted.read_text(encoding="utf-8").strip())
    assert promoted_event["status"] == "validated"
    assert promoted_event["payload"]["noop_if_already_reflected"] is True


def test_codex_review_decisions_approve_exact_splice_from_compound_block(tmp_path):
    component_id = "/in/union/rules/cgst-rules-2017/rule/12"
    event = {
        "event_id": "evt_exact_splice_compound",
        "event_type": "TEXTUAL_AMENDMENT",
        "operation": "SPLICE",
        "source": {
            "document_id": "/test/notification",
            "record_id": "1",
            "instrument_number": "1/2020-Central Tax",
            "issuing_authority": "/in/authority/cbic",
            "publication_date": "2020-01-01",
            "source_url": "",
            "source_file_sha256": "0" * 64,
            "source_text_sha256": "1" * 64,
        },
        "legal_time": {
            "commencement_date": "2020-01-01",
            "applicability_start": "2020-01-01",
            "applicability_end": None,
            "retrospective": False,
            "date_basis": "fixture",
        },
        "system_time": {
            "observed_at": "2026-06-16T00:00:00Z",
            "compiled_at": "2026-06-16T00:00:00Z",
            "compiler_version": "test",
        },
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": component_id,
            "anchor_text": "A person applying for registration to",
        },
        "payload": {"insert_text": "deduct or", "position": "after"},
        "evidence": {
            "source_span": {"start": 0, "end": 10, "text_hash": "2" * 64},
            "excerpt": "(a) after the words ... shall be inserted; (b) after the words ... shall be inserted",
            "parser_trace": {"pattern_id": "fixture", "confidence": 1.0},
        },
        "validation": {
            "target_resolved": True,
            "anchor_resolved": False,
            "date_resolved": True,
            "source_span_verified": True,
            "materializable": False,
        },
        "status": "needs_review",
        "review": {"required": True, "review_reasons": ["compound_block_contains_multiple_amendments"]},
    }
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(json.dumps(event) + "\n", encoding="utf-8")
    node_versions = tmp_path / "node_versions.jsonl"
    node_versions.write_text(
        json.dumps(
            {
                "component_id": component_id,
                "applicability_start": "2017-06-19",
                "applicability_end": None,
                "valid_from": "2017-06-19",
                "valid_to": None,
                "text": "12. A person applying for registration to collect tax shall submit an application.",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    decisions_path = tmp_path / "codex_review_decisions.json"

    result = generate_codex_review_decisions(
        events_path=events_path,
        node_versions_path=node_versions,
        output=decisions_path,
        existing_decision_paths=[],
    )

    assert result["new_decision_count"] == 1
    decision = json.loads(decisions_path.read_text(encoding="utf-8"))["decisions"][0]
    assert decision["promote"]["strategy"] == "exact_splice_payload"


def test_codex_review_decisions_approve_source_backed_rule_insert(tmp_path):
    events_path = tmp_path / "events.jsonl"
    event = {
        "event_id": "evt_rule_21a",
        "operation": "INSERT_SIBLING",
        "status": "needs_review",
        "source": {"document_id": "/in/union/notifications/cbic/central-tax/2019/3-2019"},
        "legal_time": {
            "applicability_start": "2019-01-29",
            "commencement_date": "2019-01-29",
            "date_basis": "publication_date_fallback",
        },
        "system_time": {"compiled_at": "2026-06-16T00:00:00Z"},
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": "/in/union/rules/cgst-rules-2017/rule/21",
            "anchor_component_id": "/in/union/rules/cgst-rules-2017/rule/21",
        },
        "payload": {
            "label": "",
            "heading": "- “Rule 21A",
            "content": (
                "Suspension of registration.- (1) Where a registered person has applied for cancellation "
                "of registration under rule 20, the registration shall be deemed to be suspended from the "
                "date of submission of the application. (2) Where the proper officer has reasons to "
                "believe that the registration is liable to be cancelled, he may suspend the registration."
            ),
        },
        "evidence": {
            "excerpt": (
                "6. In the said rules, after rule 21, the following rule shall be inserted, namely:- "
                "“Rule 21A. Suspension of registration.- (1) Where a registered person has applied for "
                "cancellation of registration under rule 20"
            ),
            "source_span": {"start": 1, "end": 2, "text_hash": "abc"},
        },
        "validation": {
            "date_resolved": True,
            "source_span_verified": True,
            "target_resolved": True,
            "anchor_resolved": True,
            "materializable": False,
        },
        "review": {
            "required": True,
            "review_reasons": ["inserted_component_already_exists", "inserted_rule_label_not_found"],
        },
    }
    events_path.write_text(json.dumps(event) + "\n", encoding="utf-8")
    node_versions = tmp_path / "node_versions.jsonl"
    node_versions.write_text(
        json.dumps(
            {
                "component_id": "/in/union/rules/cgst-rules-2017/rule/21",
                "applicability_start": "2017-06-19",
                "applicability_end": None,
                "text": "21\nRegistration to be cancelled in certain cases\ntext",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    decisions_path = tmp_path / "codex_review_decisions.json"

    result = generate_codex_review_decisions(
        events_path=events_path,
        node_versions_path=node_versions,
        output=decisions_path,
        existing_decision_paths=[],
    )

    assert result["new_decision_count"] == 1
    decision = json.loads(decisions_path.read_text(encoding="utf-8"))["decisions"][0]
    assert decision["promote"]["strategy"] == "source_backed_insert_rule"
    assert decision["promote"]["component_id"] == "/in/union/rules/cgst-rules-2017/rule/21a"
    promoted = tmp_path / "promoted.jsonl"
    apply_review_decisions(events_path=events_path, decisions_path=decisions_path, output=promoted)
    promoted_event = json.loads(promoted.read_text(encoding="utf-8").splitlines()[0])
    assert promoted_event["status"] == "validated"
    assert promoted_event["target"]["component_id"] == "/in/union/rules/cgst-rules-2017/rule/21a"
    assert promoted_event["target"]["anchor_resolved_by_codex"] is True
    assert promoted_event["payload"]["label"] == "21A"
    assert promoted_event["payload"]["heading"] == "Suspension of registration"


def test_codex_review_decisions_use_verified_source_archive_span(tmp_path):
    source_text = (
        "6. In the said rules, after rule 61, the following rule shall be inserted, namely: -\n"
        "“61A. Manner of opting for furnishing quarterly return.- (1) Every registered person "
        "intending to furnish return on a quarterly basis shall indicate his preference "
        "electronically on the common portal. (2) A registered person whose aggregate turnover "
        "exceeds five crore rupees during the current financial year shall opt for furnishing "
        "return on a monthly basis.”."
    )
    source_archive = tmp_path / "sources" / "cbic" / "central-tax" / "2020" / "82-2020"
    source_archive.mkdir(parents=True)
    (source_archive / "extracted_text.json").write_text(json.dumps({"text": source_text}), encoding="utf-8")

    event = {
        "event_id": "evt_rule_61a",
        "operation": "INSERT_SIBLING",
        "status": "needs_review",
        "source": {"document_id": "/in/union/notifications/cbic/central-tax/2020/82-2020"},
        "legal_time": {
            "applicability_start": "2020-11-10",
            "commencement_date": "2020-11-10",
            "date_basis": "publication_date_fallback",
        },
        "system_time": {"compiled_at": "2026-06-16T00:00:00Z"},
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": "/in/union/rules/cgst-rules-2017/rule/61",
            "anchor_component_id": "/in/union/rules/cgst-rules-2017/rule/61",
        },
        "payload": {
            "label": "",
            "heading": "- “61A",
            "content": "Manner of opting for furnishing quarterly return.- (1) truncated",
        },
        "evidence": {
            "excerpt": source_text[:220],
            "source_span": {
                "start": 0,
                "end": len(source_text),
                "text_hash": hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
            },
        },
        "validation": {
            "date_resolved": True,
            "source_span_verified": True,
            "target_resolved": True,
            "anchor_resolved": True,
            "materializable": False,
        },
        "review": {
            "required": True,
            "review_reasons": [
                "inserted_component_already_exists",
                "inserted_rule_label_not_found",
                "same_effective_date_conflict",
            ],
        },
    }
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(json.dumps(event) + "\n", encoding="utf-8")
    node_versions = tmp_path / "node_versions.jsonl"
    node_versions.write_text(
        json.dumps(
            {
                "component_id": "/in/union/rules/cgst-rules-2017/rule/61",
                "applicability_start": "2017-06-19",
                "applicability_end": None,
                "text": "61\nForm and manner of furnishing of return\ntext",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    decisions_path = tmp_path / "codex_review_decisions.json"

    result = generate_codex_review_decisions(
        events_path=events_path,
        node_versions_path=node_versions,
        output=decisions_path,
        existing_decision_paths=[],
        source_archive_root=tmp_path / "sources",
    )

    assert result["new_decision_count"] == 1
    decision = json.loads(decisions_path.read_text(encoding="utf-8"))["decisions"][0]
    assert decision["promote"]["component_id"] == "/in/union/rules/cgst-rules-2017/rule/61a"
    assert decision["promote"]["content"].endswith("return on a monthly basis.")


def test_codex_review_decisions_approve_whole_rule_omit_instruction(tmp_path):
    component_id = "/in/union/rules/cgst-rules-2017/rule/95a"
    event = {
        "event_id": "evt_rule_95a_omit",
        "operation": "OMIT",
        "status": "needs_review",
        "source": {
            "document_id": "/in/union/notifications/cbic/central-tax/2022/14-2022",
            "publication_date": "2022-07-05",
        },
        "legal_time": {
            "applicability_start": "2022-07-05",
            "commencement_date": "2022-07-05",
            "date_basis": "publication_date_fallback",
        },
        "system_time": {"compiled_at": "2026-06-16T00:00:00Z"},
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": component_id,
        },
        "payload": {},
        "evidence": {
            "excerpt": "9. In the said rules, rule 95A shall be deemed to have been omitted with effect from the 1st July, 2019;",
            "source_span": {"start": 0, "end": 105, "text_hash": "abc"},
        },
        "validation": {
            "date_resolved": True,
            "source_span_verified": True,
            "target_resolved": False,
            "anchor_resolved": True,
            "materializable": False,
        },
        "review": {
            "required": True,
            "review_reasons": ["llm_candidate_not_validated", "omit_phrase_not_found", "target_not_resolved"],
        },
    }
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(json.dumps(event) + "\n", encoding="utf-8")
    node_versions = tmp_path / "node_versions.jsonl"
    node_versions.write_text(
        json.dumps(
            {
                "component_id": component_id,
                "applicability_start": "2019-07-01",
                "applicability_end": None,
                "text": "95A\nRefund of taxes to retail outlets\nOriginal text.",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    decisions_path = tmp_path / "codex_review_decisions.json"

    result = generate_codex_review_decisions(
        events_path=events_path,
        node_versions_path=node_versions,
        output=decisions_path,
        existing_decision_paths=[],
    )

    assert result["new_decision_count"] == 1
    decision = json.loads(decisions_path.read_text(encoding="utf-8"))["decisions"][0]
    assert decision["promote"]["strategy"] == "whole_rule_omit_instruction"
    assert decision["promote"]["component_id"] == component_id
    assert decision["promote"]["applicability_start"] == "2019-07-01"

    promoted = tmp_path / "promoted.jsonl"
    apply_review_decisions(events_path=events_path, decisions_path=decisions_path, output=promoted)
    promoted_event = json.loads(promoted.read_text(encoding="utf-8").splitlines()[0])
    assert promoted_event["status"] == "validated"
    assert promoted_event["payload"]["whole_component"] is True
    assert promoted_event["legal_time"]["applicability_start"] == "2019-07-01"
    assert promoted_event["target"]["anchor_resolved_by_codex"] is True


def test_codex_review_decisions_approve_whole_subrule_omit_when_component_exists(tmp_path):
    component_id = "/in/union/rules/cgst-rules-2017/rule/96/subrule/10"
    event = {
        "event_id": "evt_rule_96_subrule_10_omit",
        "operation": "OMIT",
        "status": "needs_review",
        "source": {"document_id": "/in/union/notifications/cbic/central-tax/2024/20-2024"},
        "legal_time": {
            "applicability_start": "2024-10-08",
            "commencement_date": "2024-10-08",
            "date_basis": "publication_date_fallback",
        },
        "system_time": {"compiled_at": "2026-06-16T00:00:00Z"},
        "target": {"work_id": "/in/union/rules/cgst-rules-2017", "component_id": component_id},
        "payload": {},
        "evidence": {
            "excerpt": "10. In the said rules, in rule 96, sub-rule (10) shall be omitted.",
            "source_span": {"start": 0, "end": 66, "text_hash": "abc"},
        },
        "validation": {
            "date_resolved": True,
            "source_span_verified": True,
            "target_resolved": False,
            "anchor_resolved": True,
            "materializable": False,
        },
        "review": {"required": True, "review_reasons": ["llm_candidate_not_validated", "target_not_resolved"]},
    }
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(json.dumps(event) + "\n", encoding="utf-8")
    node_versions = tmp_path / "node_versions.jsonl"
    node_versions.write_text(
        json.dumps(
            {
                "component_id": component_id,
                "applicability_start": "2017-10-23",
                "applicability_end": None,
                "text": "(10) Existing refund restriction.",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    decisions_path = tmp_path / "codex_review_decisions.json"

    result = generate_codex_review_decisions(
        events_path=events_path,
        node_versions_path=node_versions,
        output=decisions_path,
        existing_decision_paths=[],
    )

    assert result["new_decision_count"] == 1
    decision = json.loads(decisions_path.read_text(encoding="utf-8"))["decisions"][0]
    assert decision["promote"]["strategy"] == "whole_component_omit_instruction"
    assert decision["promote"]["component_id"] == component_id

    promoted = tmp_path / "promoted.jsonl"
    apply_review_decisions(events_path=events_path, decisions_path=decisions_path, output=promoted)
    promoted_event = json.loads(promoted.read_text(encoding="utf-8").splitlines()[0])
    assert promoted_event["payload"]["whole_component"] is True
    assert promoted_event["target"]["component_id"] == component_id


def test_codex_review_decisions_approve_already_reflected_omit(tmp_path):
    component_id = "/in/union/rules/cgst-rules-2017/rule/86"
    event = {
        "event_id": "evt_already_reflected_omit",
        "event_type": "TEXTUAL_AMENDMENT",
        "operation": "OMIT",
        "source": {
            "document_id": "/in/union/notifications/cbic/central-tax/2024/fixture",
            "record_id": "1",
            "instrument_number": "1/2024-Central Tax",
            "issuing_authority": "/in/authority/cbic",
            "publication_date": "2024-10-08",
            "source_url": "",
            "source_file_sha256": "0" * 64,
            "source_text_sha256": "1" * 64,
        },
        "legal_time": {
            "commencement_date": "2024-10-08",
            "applicability_start": "2024-10-08",
            "applicability_end": None,
            "retrospective": False,
            "date_basis": "fixture",
        },
        "system_time": {
            "observed_at": "2026-06-16T00:00:00Z",
            "compiled_at": "2026-06-16T00:00:00Z",
            "compiler_version": "test",
        },
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": component_id,
            "anchor_text": "in contravention of sub-rule (10) of rule 96,",
        },
        "payload": {
            "omit_text": "in contravention of sub-rule (10) of rule 96,",
            "whole_component": False,
        },
        "evidence": {
            "source_span": {"start": 0, "end": 10, "text_hash": "2" * 64},
            "excerpt": "the words, brackets and figures ... shall be omitted",
            "parser_trace": {"pattern_id": "fixture", "confidence": 1.0},
        },
        "validation": {
            "target_resolved": True,
            "anchor_resolved": False,
            "date_resolved": True,
            "source_span_verified": True,
            "materializable": False,
        },
        "status": "needs_review",
        "review": {"required": True, "review_reasons": ["anchor_not_resolved", "llm_candidate_not_validated"]},
    }
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(json.dumps(event) + "\n", encoding="utf-8")
    node_versions = tmp_path / "node_versions.jsonl"
    node_versions.write_text(
        json.dumps(
            {
                "component_id": component_id,
                "applicability_start": "2017-06-19",
                "applicability_end": None,
                "valid_from": "2017-06-19",
                "valid_to": None,
                "text": "86. Electronic Credit Ledger.- Refund re-credit text without the deleted phrase.",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    decisions_path = tmp_path / "codex_review_decisions.json"

    result = generate_codex_review_decisions(
        events_path=events_path,
        node_versions_path=node_versions,
        output=decisions_path,
        existing_decision_paths=[],
    )

    assert result["new_decision_count"] == 1
    decision = json.loads(decisions_path.read_text(encoding="utf-8"))["decisions"][0]
    assert decision["promote"]["strategy"] == "already_reflected_omit_payload"
    assert decision["promote"]["noop_if_already_reflected"] is True

    promoted = tmp_path / "promoted.jsonl"
    apply_review_decisions(events_path=events_path, decisions_path=decisions_path, output=promoted)
    promoted_event = json.loads(promoted.read_text(encoding="utf-8").strip())
    assert promoted_event["status"] == "validated"
    assert promoted_event["payload"]["noop_if_already_reflected"] is True


def test_codex_review_decisions_parse_unknown_omit_instruction(tmp_path):
    component_id = "/in/union/rules/cgst-rules-2017/rule/22"
    event = {
        "event_id": "evt_unknown_omit_words",
        "event_type": "TEXTUAL_AMENDMENT",
        "operation": "UNKNOWN",
        "source": {
            "document_id": "/in/union/notifications/cbic/central-tax/2017/fixture",
            "record_id": "1",
            "instrument_number": "1/2017-Central Tax",
            "issuing_authority": "/in/authority/cbic",
            "publication_date": "2017-06-22",
            "source_url": "",
            "source_file_sha256": "0" * 64,
            "source_text_sha256": "1" * 64,
        },
        "legal_time": {
            "commencement_date": "2017-06-22",
            "applicability_start": "2017-06-22",
            "applicability_end": None,
            "retrospective": True,
            "date_basis": "fixture",
        },
        "system_time": {"compiled_at": "2026-06-16T00:00:00Z"},
        "target": {"work_id": "/in/union/rules/cgst-rules-2017", "component_id": component_id},
        "payload": {"text": "in rule 22, in sub-rule (3), the words, brackets and figure “sub-rule (1) of ” shall be omitted;"},
        "evidence": {
            "source_span": {"start": 0, "end": 10, "text_hash": "2" * 64},
            "excerpt": "in rule 22, in sub-rule (3), the words, brackets and figure “sub-rule (1) of ” shall be omitted;",
            "parser_trace": {"pattern_id": "fixture", "confidence": 1.0},
        },
        "validation": {
            "target_resolved": True,
            "anchor_resolved": True,
            "date_resolved": True,
            "source_span_verified": True,
            "materializable": False,
        },
        "status": "needs_review",
        "review": {
            "required": True,
            "review_reasons": [
                "compound_block_contains_unsupported_omission",
                "llm_limit_not_attempted",
                "unparsed_target_work_amendment",
                "unsupported_materializer_operation",
            ],
        },
    }
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(json.dumps(event) + "\n", encoding="utf-8")
    node_versions = tmp_path / "node_versions.jsonl"
    node_versions.write_text(
        json.dumps(
            {
                "component_id": component_id,
                "applicability_start": "2017-06-19",
                "applicability_end": None,
                "text": "(3) The proper officer may act under sub-rule (1) of this rule.",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    decisions_path = tmp_path / "codex_review_decisions.json"

    result = generate_codex_review_decisions(
        events_path=events_path,
        node_versions_path=node_versions,
        output=decisions_path,
        existing_decision_paths=[],
    )

    assert result["new_decision_count"] == 1
    decision = json.loads(decisions_path.read_text(encoding="utf-8"))["decisions"][0]
    assert decision["promote"]["operation"] == "OMIT"
    assert decision["promote"]["strategy"] == "source_backed_unknown_omit_payload"

    promoted = tmp_path / "promoted.jsonl"
    apply_review_decisions(events_path=events_path, decisions_path=decisions_path, output=promoted)
    promoted_event = json.loads(promoted.read_text(encoding="utf-8").strip())
    assert promoted_event["status"] == "validated"
    assert promoted_event["operation"] == "OMIT"
    assert promoted_event["payload"]["omit_text"] == "sub-rule (1) of"


def test_codex_review_decisions_parse_unknown_already_reflected_substitute(tmp_path):
    component_id = "/in/union/rules/cgst-rules-2017/rule/119"
    event = {
        "event_id": "evt_unknown_heading_substitute",
        "event_type": "TEXTUAL_AMENDMENT",
        "operation": "UNKNOWN",
        "source": {
            "document_id": "/in/union/notifications/cbic/central-tax/2017/fixture",
            "record_id": "1",
            "instrument_number": "1/2017-Central Tax",
            "issuing_authority": "/in/authority/cbic",
            "publication_date": "2017-07-01",
            "source_url": "",
            "source_file_sha256": "0" * 64,
            "source_text_sha256": "1" * 64,
        },
        "legal_time": {
            "commencement_date": "2017-07-01",
            "applicability_start": "2017-07-01",
            "applicability_end": None,
            "retrospective": False,
            "date_basis": "fixture",
        },
        "system_time": {"compiled_at": "2026-06-16T00:00:00Z"},
        "target": {"work_id": "/in/union/rules/cgst-rules-2017", "component_id": component_id},
        "payload": {"text": "in rule 119, in the heading, for the word “agent”, the word “job-worker” shall be substituted;"},
        "evidence": {
            "source_span": {"start": 0, "end": 10, "text_hash": "2" * 64},
            "excerpt": "in rule 119, in the heading, for the word “agent”, the word “job-worker” shall be substituted;",
            "parser_trace": {"pattern_id": "fixture", "confidence": 1.0},
        },
        "validation": {
            "target_resolved": True,
            "anchor_resolved": True,
            "date_resolved": True,
            "source_span_verified": True,
            "materializable": False,
        },
        "status": "needs_review",
        "review": {
            "required": True,
            "review_reasons": [
                "llm_limit_not_attempted",
                "unparsed_target_work_amendment",
                "unsupported_materializer_operation",
            ],
        },
    }
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(json.dumps(event) + "\n", encoding="utf-8")
    node_versions = tmp_path / "node_versions.jsonl"
    node_versions.write_text(
        json.dumps(
            {
                "component_id": component_id,
                "applicability_start": "2017-06-19",
                "applicability_end": None,
                "text": "119. Declaration of stock held by a principal and job-worker.",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    decisions_path = tmp_path / "codex_review_decisions.json"

    result = generate_codex_review_decisions(
        events_path=events_path,
        node_versions_path=node_versions,
        output=decisions_path,
        existing_decision_paths=[],
    )

    assert result["new_decision_count"] == 1
    decision = json.loads(decisions_path.read_text(encoding="utf-8"))["decisions"][0]
    assert decision["promote"]["operation"] == "SUBSTITUTE"
    assert decision["promote"]["strategy"] == "source_backed_unknown_already_reflected_substitute_payload"

    promoted = tmp_path / "promoted.jsonl"
    apply_review_decisions(events_path=events_path, decisions_path=decisions_path, output=promoted)
    promoted_event = json.loads(promoted.read_text(encoding="utf-8").strip())
    assert promoted_event["status"] == "validated"
    assert promoted_event["operation"] == "SUBSTITUTE"
    assert promoted_event["payload"]["noop_if_already_reflected"] is True


def test_codex_review_decisions_do_not_approve_missing_subrule_component(tmp_path):
    component_id = "/in/union/rules/cgst-rules-2017/rule/96/subrule/10"
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(
        json.dumps(
            {
                "event_id": "evt_missing_subrule_omit",
                "operation": "OMIT",
                "status": "needs_review",
                "source": {"document_id": "/in/union/notifications/cbic/central-tax/2024/20-2024"},
                "legal_time": {"applicability_start": "2024-10-08", "commencement_date": "2024-10-08"},
                "target": {"work_id": "/in/union/rules/cgst-rules-2017", "component_id": component_id},
                "payload": {},
                "evidence": {
                    "excerpt": "10. In the said rules, in rule 96, sub-rule (10) shall be omitted.",
                    "source_span": {"start": 0, "end": 66, "text_hash": "abc"},
                },
                "validation": {"date_resolved": True, "source_span_verified": True},
                "review": {"required": True, "review_reasons": ["target_not_resolved"]},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    node_versions = tmp_path / "node_versions.jsonl"
    node_versions.write_text(
        json.dumps(
            {
                "component_id": "/in/union/rules/cgst-rules-2017/rule/96",
                "applicability_start": "2017-06-19",
                "applicability_end": None,
                "text": "96. Parent rule only.",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = generate_codex_review_decisions(
        events_path=events_path,
        node_versions_path=node_versions,
        output=tmp_path / "codex_review_decisions.json",
        existing_decision_paths=[],
    )

    assert result["new_decision_count"] == 0


def test_codex_review_decisions_approve_source_backed_clause_insert(tmp_path):
    event = {
        "event_id": "evt_rule_46_clause_r",
        "operation": "INSERT_CHILD",
        "status": "needs_review",
        "source": {"document_id": "/in/union/notifications/cbic/central-tax/2020/72-2020"},
        "legal_time": {
            "applicability_start": "2020-09-30",
            "commencement_date": "2020-09-30",
            "date_basis": "publication_date_fallback",
        },
        "system_time": {"compiled_at": "2026-06-16T00:00:00Z"},
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": "/in/union/rules/cgst-rules-2017/rule/46/subrule/(r)",
        },
        "payload": {},
        "evidence": {
            "excerpt": (
                "2. In the Central Goods and Services Tax Rules, 2017, in rule 46, after clause (q), "
                "the following clause shall be inserted, namely:- “(r) Quick Reference code, having "
                "embedded Invoice Reference Number (IRN) in it, in case invoice has been issued in "
                "the manner prescribed under sub-rule (4) of rule 48.”."
            ),
            "source_span": {"start": 0, "end": 310, "text_hash": "abc"},
        },
        "validation": {
            "date_resolved": True,
            "source_span_verified": True,
            "target_resolved": False,
            "anchor_resolved": False,
            "materializable": False,
        },
        "review": {
            "required": True,
            "review_reasons": ["anchor_not_resolved", "llm_candidate_not_validated", "target_not_resolved"],
        },
    }
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(json.dumps(event) + "\n", encoding="utf-8")
    node_versions = tmp_path / "node_versions.jsonl"
    node_versions.write_text(
        json.dumps(
            {
                "component_id": "/in/union/rules/cgst-rules-2017/rule/46",
                "applicability_start": "2017-06-19",
                "applicability_end": None,
                "text": "46. Tax invoice.",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    decisions_path = tmp_path / "codex_review_decisions.json"

    result = generate_codex_review_decisions(
        events_path=events_path,
        node_versions_path=node_versions,
        output=decisions_path,
        existing_decision_paths=[],
    )

    assert result["new_decision_count"] == 1
    decision = json.loads(decisions_path.read_text(encoding="utf-8"))["decisions"][0]
    assert decision["promote"]["strategy"] == "source_backed_insert_child_clause"
    assert decision["promote"]["component_id"] == "/in/union/rules/cgst-rules-2017/rule/46/clause/r"

    promoted = tmp_path / "promoted.jsonl"
    apply_review_decisions(events_path=events_path, decisions_path=decisions_path, output=promoted)
    promoted_event = json.loads(promoted.read_text(encoding="utf-8").splitlines()[0])
    assert promoted_event["status"] == "validated"
    assert promoted_event["operation"] == "INSERT_CHILD"
    assert promoted_event["target"]["component_id"] == "/in/union/rules/cgst-rules-2017/rule/46/clause/r"
    assert promoted_event["payload"]["node_type"] == "clause"


def test_codex_review_decisions_parse_clause_insert_with_low_quote(tmp_path):
    event = {
        "event_id": "evt_rule_46_clause_s",
        "operation": "INSERT_CHILD",
        "status": "needs_review",
        "source": {"document_id": "/in/union/notifications/cbic/central-tax/2022/14-2022"},
        "legal_time": {
            "applicability_start": "2022-07-05",
            "commencement_date": "2022-07-05",
            "date_basis": "publication_date_fallback",
        },
        "system_time": {"compiled_at": "2026-06-16T00:00:00Z"},
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": "/in/union/rules/cgst-rules-2017/rule/46/clause/s",
        },
        "payload": {},
        "evidence": {
            "excerpt": (
                "4. In the said rules, in rule 46, after clause (r), the following clause shall be inserted, "
                "namely: - ‗(s) a declaration as below, that invoice is not required to be issued in the "
                "manner specified under sub-rule (4) of rule 48."
            )
        },
        "validation": {
            "date_resolved": True,
            "source_span_verified": True,
            "target_resolved": False,
            "anchor_resolved": False,
            "materializable": False,
        },
        "review": {
            "required": True,
            "review_reasons": [
                "anchor_not_resolved",
                "inserted_component_already_exists",
                "llm_candidate_not_validated",
                "context_recovered_target_pending_validation",
            ],
        },
    }
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(json.dumps(event) + "\n", encoding="utf-8")
    node_versions = tmp_path / "node_versions.jsonl"
    node_versions.write_text(
        "\n".join(
            json.dumps(row)
            for row in [
                {
                    "component_id": "/in/union/rules/cgst-rules-2017/rule/46",
                    "applicability_start": "2017-06-19",
                    "applicability_end": None,
                    "text": "46. Tax invoice.",
                },
                {
                    "component_id": "/in/union/rules/cgst-rules-2017/rule/46/clause/r",
                    "applicability_start": "2020-09-30",
                    "applicability_end": None,
                    "text": "(r) Quick Reference code.",
                },
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    decisions_path = tmp_path / "codex_review_decisions.json"

    result = generate_codex_review_decisions(
        events_path=events_path,
        node_versions_path=node_versions,
        output=decisions_path,
        existing_decision_paths=[],
    )

    assert result["new_decision_count"] == 1
    decision = json.loads(decisions_path.read_text(encoding="utf-8"))["decisions"][0]
    assert decision["promote"]["component_id"] == "/in/union/rules/cgst-rules-2017/rule/46/clause/s"
    assert decision["promote"]["anchor_component_id"] == "/in/union/rules/cgst-rules-2017/rule/46/clause/r"


def test_codex_review_decisions_approve_subrule_proviso_clause_insert_from_unknown(tmp_path):
    event = {
        "event_id": "evt_rule_8_clause_aa",
        "operation": "UNKNOWN",
        "status": "needs_review",
        "source": {"document_id": "/in/union/notifications/cbic/central-tax/2022/26-2022"},
        "legal_time": {
            "applicability_start": "2022-12-26",
            "commencement_date": "2022-12-26",
            "date_basis": "publication_date_fallback",
        },
        "system_time": {"compiled_at": "2026-06-16T00:00:00Z"},
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": "/in/union/rules/cgst-rules-2017/rule/8",
        },
        "payload": {
            "text": (
                "(ii) in sub-rule (2), in the proviso, after clause (a), the following clause shall be "
                "inserted, namely: - “(aa) a person, who has undergone authentication of Aadhaar number "
                "as specified in sub-rule (4A) of rule 8, is identified on the common portal, based on "
                "data analysis and risk parameters, for carrying out physical verification of places of business; or”."
            )
        },
        "evidence": {
            "excerpt": "",
            "source_span": {"start": 0, "end": 350, "text_hash": "abc"},
        },
        "validation": {
            "date_resolved": True,
            "source_span_verified": True,
            "target_resolved": True,
            "anchor_resolved": True,
            "materializable": False,
        },
        "review": {
            "required": True,
            "review_reasons": [
                "llm_limit_not_attempted",
                "same_effective_date_conflict",
                "unparsed_target_work_amendment",
                "unsupported_materializer_operation",
            ],
        },
    }
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(json.dumps(event) + "\n", encoding="utf-8")
    node_versions = tmp_path / "node_versions.jsonl"
    node_versions.write_text(
        json.dumps(
            {
                "component_id": "/in/union/rules/cgst-rules-2017/rule/8/subrule/2",
                "applicability_start": "2017-06-19",
                "applicability_end": None,
                "text": "(2) Existing subrule text.",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    decisions_path = tmp_path / "codex_review_decisions.json"

    result = generate_codex_review_decisions(
        events_path=events_path,
        node_versions_path=node_versions,
        output=decisions_path,
        existing_decision_paths=[],
    )

    assert result["new_decision_count"] == 1
    decision = json.loads(decisions_path.read_text(encoding="utf-8"))["decisions"][0]
    assert decision["promote"]["strategy"] == "source_backed_insert_child_clause"
    assert decision["promote"]["component_id"] == "/in/union/rules/cgst-rules-2017/rule/8/subrule/2/clause/aa"
    assert decision["promote"]["parent_component_id"] == "/in/union/rules/cgst-rules-2017/rule/8/subrule/2"

    promoted = tmp_path / "promoted.jsonl"
    apply_review_decisions(events_path=events_path, decisions_path=decisions_path, output=promoted)
    promoted_event = json.loads(promoted.read_text(encoding="utf-8").splitlines()[0])
    assert promoted_event["status"] == "validated"
    assert promoted_event["operation"] == "INSERT_CHILD"
    assert promoted_event["target"]["component_id"] == "/in/union/rules/cgst-rules-2017/rule/8/subrule/2/clause/aa"
    assert promoted_event["payload"]["label"] == "(aa)"


def test_codex_review_decisions_infer_rule_context_for_unknown_subrule_substitute(tmp_path):
    header = {
        "event_id": "evt_rule_44_header",
        "operation": "UNKNOWN",
        "status": "needs_review",
        "source": {"document_id": "/in/union/notifications/cbic/central-tax/2017/15-2017"},
        "legal_time": {"applicability_start": "2017-07-01", "commencement_date": "2017-07-01"},
        "system_time": {"compiled_at": "2026-06-16T00:00:00Z"},
        "target": {"work_id": "/in/union/rules/cgst-rules-2017", "component_id": "/in/union/rules/cgst-rules-2017"},
        "payload": {"text": "(i) in rule 44,"},
        "evidence": {"excerpt": "(i) in rule 44,", "source_span": {"start": 100, "end": 115, "text_hash": "a"}},
        "validation": {"date_resolved": True, "source_span_verified": True, "target_resolved": True},
        "review": {"required": True, "review_reasons": ["unparsed_target_work_amendment"]},
    }
    fragment = {
        **header,
        "event_id": "evt_rule_44_fragment",
        "payload": {
            "text": (
                "(a) in sub-rule (2), for the words “integrated tax and central tax”, "
                "the words “central tax, State tax, Union territory tax and integrated tax” shall be substituted;"
            )
        },
        "evidence": {
            "excerpt": (
                "(a) in sub-rule (2), for the words “integrated tax and central tax”, "
                "the words “central tax, State tax, Union territory tax and integrated tax” shall be substituted;"
            ),
            "source_span": {"start": 116, "end": 290, "text_hash": "b"},
        },
        "review": {
            "required": True,
            "review_reasons": [
                "llm_limit_not_attempted",
                "same_effective_date_conflict",
                "unparsed_target_work_amendment",
                "unsupported_materializer_operation",
            ],
        },
    }
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(json.dumps(header) + "\n" + json.dumps(fragment) + "\n", encoding="utf-8")
    node_versions = tmp_path / "node_versions.jsonl"
    node_versions.write_text(
        json.dumps(
            {
                "component_id": "/in/union/rules/cgst-rules-2017/rule/44/subrule/2",
                "applicability_start": "2017-06-19",
                "applicability_end": None,
                "text": "The amount of integrated tax and central tax shall be calculated.",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    decisions_path = tmp_path / "codex_review_decisions.json"

    result = generate_codex_review_decisions(
        events_path=events_path,
        node_versions_path=node_versions,
        output=decisions_path,
        existing_decision_paths=[],
    )

    assert result["new_decision_count"] == 1
    decision = json.loads(decisions_path.read_text(encoding="utf-8"))["decisions"][0]
    assert decision["event_id"] == "evt_rule_44_fragment"
    assert decision["promote"]["operation"] == "SUBSTITUTE"
    assert decision["promote"]["component_id"] == "/in/union/rules/cgst-rules-2017/rule/44/subrule/2"


def test_codex_review_decisions_approve_source_backed_subrule_insert(tmp_path):
    event = {
        "event_id": "evt_rule_36_subrule_4",
        "operation": "INSERT_CHILD",
        "status": "needs_review",
        "source": {"document_id": "/in/union/notifications/cbic/central-tax/2019/49-2019"},
        "legal_time": {"applicability_start": "2019-10-09", "commencement_date": "2019-10-09"},
        "system_time": {"compiled_at": "2026-06-16T00:00:00Z"},
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": "/in/union/rules/cgst-rules-2017/rule/36/subrule/(4)",
        },
        "payload": {},
        "evidence": {
            "excerpt": (
                "3. In the said rules, in rule 36, after sub-rule (3), the following sub-rule shall "
                "be inserted, namely:- “(4) Input tax credit to be availed by a registered person "
                "in respect of invoices or debit notes shall not exceed 20 per cent. of the eligible credit.”."
            ),
            "source_span": {"start": 0, "end": 260, "text_hash": "abc"},
        },
        "validation": {
            "date_resolved": True,
            "source_span_verified": True,
            "target_resolved": True,
            "anchor_resolved": False,
            "materializable": False,
        },
        "review": {"required": True, "review_reasons": ["anchor_not_resolved", "llm_candidate_not_validated"]},
    }
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(json.dumps(event) + "\n", encoding="utf-8")
    node_versions = tmp_path / "node_versions.jsonl"
    node_versions.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "component_id": "/in/union/rules/cgst-rules-2017/rule/36",
                        "applicability_start": "2017-06-19",
                        "applicability_end": None,
                        "text": "36. Documentary requirements.",
                    }
                ),
                json.dumps(
                    {
                        "component_id": "/in/union/rules/cgst-rules-2017/rule/36/subrule/3",
                        "applicability_start": "2017-06-19",
                        "applicability_end": None,
                        "text": "(3) Existing text.",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    decisions_path = tmp_path / "codex_review_decisions.json"

    result = generate_codex_review_decisions(
        events_path=events_path,
        node_versions_path=node_versions,
        output=decisions_path,
        existing_decision_paths=[],
    )

    assert result["new_decision_count"] == 1
    decision = json.loads(decisions_path.read_text(encoding="utf-8"))["decisions"][0]
    assert decision["promote"]["strategy"] == "source_backed_insert_child"
    assert decision["promote"]["component_id"] == "/in/union/rules/cgst-rules-2017/rule/36/subrule/4"
    assert decision["promote"]["node_type"] == "subrule"


def test_codex_review_decisions_approve_source_backed_proviso_insert(tmp_path):
    event = {
        "event_id": "evt_rule_36_proviso",
        "operation": "INSERT_CHILD",
        "status": "needs_review",
        "source": {"document_id": "/in/union/notifications/cbic/central-tax/2018/39-2018"},
        "legal_time": {"applicability_start": "2018-09-04", "commencement_date": "2018-09-04"},
        "system_time": {"compiled_at": "2026-06-16T00:00:00Z"},
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": "/in/union/rules/cgst-rules-2017/rule/36/subrule/provided that",
        },
        "payload": {},
        "evidence": {
            "excerpt": (
                "3. In the said rules, in rule 36, in sub-rule (2), the following proviso shall be "
                "inserted, namely:- “Provided that if the said document does not contain all the "
                "specified particulars, input tax credit may be availed by such registered person.”."
            ),
            "source_span": {"start": 0, "end": 245, "text_hash": "abc"},
        },
        "validation": {
            "date_resolved": True,
            "source_span_verified": True,
            "target_resolved": True,
            "anchor_resolved": False,
            "materializable": False,
        },
        "review": {"required": True, "review_reasons": ["anchor_not_resolved", "llm_candidate_not_validated"]},
    }
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(json.dumps(event) + "\n", encoding="utf-8")
    node_versions = tmp_path / "node_versions.jsonl"
    node_versions.write_text(
        json.dumps(
            {
                "component_id": "/in/union/rules/cgst-rules-2017/rule/36/subrule/2",
                "applicability_start": "2017-06-19",
                "applicability_end": None,
                "text": "(2) Existing text.",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    decisions_path = tmp_path / "codex_review_decisions.json"

    result = generate_codex_review_decisions(
        events_path=events_path,
        node_versions_path=node_versions,
        output=decisions_path,
        existing_decision_paths=[],
    )

    assert result["new_decision_count"] == 1
    decision = json.loads(decisions_path.read_text(encoding="utf-8"))["decisions"][0]
    assert decision["promote"]["strategy"] == "source_backed_insert_child"
    assert decision["promote"]["node_type"] == "proviso"
    assert decision["promote"]["parent_component_id"] == "/in/union/rules/cgst-rules-2017/rule/36/subrule/2"


def test_codex_review_decisions_approve_source_backed_explanation_insert(tmp_path):
    event = {
        "event_id": "evt_rule_43_explanation",
        "operation": "INSERT_CHILD",
        "status": "needs_review",
        "source": {"document_id": "/in/union/notifications/cbic/central-tax/2017/55-2017"},
        "legal_time": {"applicability_start": "2017-11-15", "commencement_date": "2017-11-15"},
        "system_time": {"compiled_at": "2026-06-16T00:00:00Z"},
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": "/in/union/rules/cgst-rules-2017/rule/43/subrule/explanation",
        },
        "payload": {},
        "evidence": {
            "excerpt": (
                "in rule 43, after sub-rule (2), the following explanation shall be inserted, namely:- "
                "“Explanation - For the purposes of rule 42 and this rule, exempt supplies shall "
                "exclude the value of supply of services specified in the notification.”"
            ),
            "source_span": {"start": 0, "end": 240, "text_hash": "abc"},
        },
        "validation": {
            "date_resolved": True,
            "source_span_verified": True,
            "target_resolved": True,
            "anchor_resolved": False,
            "materializable": False,
        },
        "review": {"required": True, "review_reasons": ["anchor_not_resolved", "llm_candidate_not_validated"]},
    }
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(json.dumps(event) + "\n", encoding="utf-8")
    node_versions = tmp_path / "node_versions.jsonl"
    node_versions.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "component_id": "/in/union/rules/cgst-rules-2017/rule/43",
                        "applicability_start": "2017-06-19",
                        "applicability_end": None,
                        "text": "43. Rule text.",
                    }
                ),
                json.dumps(
                    {
                        "component_id": "/in/union/rules/cgst-rules-2017/rule/43/subrule/2",
                        "applicability_start": "2017-06-19",
                        "applicability_end": None,
                        "text": "(2) Existing text.",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    decisions_path = tmp_path / "codex_review_decisions.json"

    result = generate_codex_review_decisions(
        events_path=events_path,
        node_versions_path=node_versions,
        output=decisions_path,
        existing_decision_paths=[],
    )

    assert result["new_decision_count"] == 1
    decision = json.loads(decisions_path.read_text(encoding="utf-8"))["decisions"][0]
    assert decision["promote"]["strategy"] == "source_backed_insert_child"
    assert decision["promote"]["node_type"] == "explanation"
    assert decision["promote"]["parent_component_id"] == "/in/union/rules/cgst-rules-2017/rule/43"


def test_codex_review_decisions_approve_structural_subrule_substitute_when_component_exists(tmp_path):
    component_id = "/in/union/rules/cgst-rules-2017/rule/96/subrule/10"
    event = {
        "event_id": "evt_rule_96_subrule_10_substitute",
        "operation": "SUBSTITUTE",
        "status": "needs_review",
        "source": {"document_id": "/in/union/notifications/cbic/central-tax/2018/53-2018"},
        "legal_time": {"applicability_start": "2017-10-23", "commencement_date": "2017-10-23"},
        "system_time": {"compiled_at": "2026-06-16T00:00:00Z"},
        "target": {"work_id": "/in/union/rules/cgst-rules-2017", "component_id": component_id},
        "payload": {},
        "evidence": {
            "excerpt": (
                "2. In the Central Goods and Services Tax Rules, 2017, in rule 96, for sub-rule (10), "
                "the following sub-rule shall be substituted, namely:- “(10) The persons claiming refund "
                "of integrated tax paid on exports of goods or services should not have received supplies "
                "on which the supplier has availed the notified benefit and shall furnish such declaration.”."
            ),
            "source_span": {"start": 0, "end": 350, "text_hash": "abc"},
        },
        "validation": {"date_resolved": True, "source_span_verified": True},
        "review": {
            "required": True,
            "review_reasons": ["anchor_not_resolved", "llm_candidate_not_validated", "target_not_resolved"],
        },
    }
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(json.dumps(event) + "\n", encoding="utf-8")
    node_versions = tmp_path / "node_versions.jsonl"
    node_versions.write_text(
        json.dumps(
            {
                "component_id": component_id,
                "applicability_start": "2017-06-19",
                "applicability_end": None,
                "text": "(10) Existing refund restriction.",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    decisions_path = tmp_path / "codex_review_decisions.json"

    result = generate_codex_review_decisions(
        events_path=events_path,
        node_versions_path=node_versions,
        output=decisions_path,
        existing_decision_paths=[],
    )

    assert result["new_decision_count"] == 1
    decision = json.loads(decisions_path.read_text(encoding="utf-8"))["decisions"][0]
    assert decision["promote"]["strategy"] == "source_backed_structural_subrule_substitute"
    assert decision["promote"]["component_id"] == component_id

    promoted = tmp_path / "promoted.jsonl"
    apply_review_decisions(events_path=events_path, decisions_path=decisions_path, output=promoted)
    promoted_event = json.loads(promoted.read_text(encoding="utf-8").splitlines()[0])
    assert promoted_event["payload"]["node_type"] == "subrule"
    assert promoted_event["payload"]["structural_text"].startswith("(10) The persons claiming refund")


def test_component_spans_find_real_subrule_marker_not_references_or_history():
    from src.legal_corpus.component_spans import find_top_level_subrule_span

    text = (
        "96. Refund.-(4) The claim refers to sub-section (10) or sub-section (11). "
        "(9) Earlier subrule text. [[(10) Current subrule text applies to exporters. "
        "227 Substituted vide Notf no. 54/2018-CT for: “(10) Old quoted history.”"
    )

    span = find_top_level_subrule_span(text, "10")

    assert span is not None
    assert text[span[0] : span[1]].startswith("(10) Current subrule text")
    assert "Old quoted history" not in text[span[0] : span[1]]


def test_codex_review_decisions_approve_parent_span_subrule_substitute_when_subrule_not_first_class(tmp_path):
    component_id = "/in/union/rules/cgst-rules-2017/rule/96/subrule/10"
    parent_component_id = "/in/union/rules/cgst-rules-2017/rule/96"
    event = {
        "event_id": "evt_rule_96_subrule_10_parent_substitute",
        "operation": "SUBSTITUTE",
        "status": "needs_review",
        "source": {"document_id": "/in/union/notifications/cbic/central-tax/2018/53-2018"},
        "legal_time": {"applicability_start": "2017-10-23", "commencement_date": "2017-10-23"},
        "system_time": {"compiled_at": "2026-06-16T00:00:00Z"},
        "target": {"work_id": "/in/union/rules/cgst-rules-2017", "component_id": component_id},
        "payload": {},
        "evidence": {
            "excerpt": (
                "2. In the Central Goods and Services Tax Rules, 2017, in rule 96, for sub-rule (10), "
                "the following sub-rule shall be substituted, namely:- “(10) The persons claiming refund "
                "of integrated tax paid on exports of goods or services should not have received supplies "
                "on which the supplier has availed the notified benefit and shall furnish such declaration.”."
            ),
            "source_span": {"start": 0, "end": 350, "text_hash": "abc"},
        },
        "validation": {"date_resolved": True, "source_span_verified": True},
        "review": {
            "required": True,
            "review_reasons": ["anchor_not_resolved", "llm_candidate_not_validated", "target_not_resolved"],
        },
    }
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(json.dumps(event) + "\n", encoding="utf-8")
    node_versions = tmp_path / "node_versions.jsonl"
    node_versions.write_text(
        json.dumps(
            {
                "component_id": parent_component_id,
                "applicability_start": "2017-06-19",
                "applicability_end": None,
                "text": (
                    "96. Refund.-(9) Earlier text. (10) Existing refund restriction. "
                    "227 Substituted vide Notf no. 54/2018 for: “(10) Old history.”"
                ),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    decisions_path = tmp_path / "codex_review_decisions.json"

    result = generate_codex_review_decisions(
        events_path=events_path,
        node_versions_path=node_versions,
        output=decisions_path,
        existing_decision_paths=[],
    )

    assert result["new_decision_count"] == 1
    decision = json.loads(decisions_path.read_text(encoding="utf-8"))["decisions"][0]
    assert decision["promote"]["strategy"] == "source_backed_parent_span_subrule_substitute"
    assert decision["promote"]["apply_to_parent_subrule_span"] is True
    assert decision["promote"]["parent_component_id"] == parent_component_id


def test_codex_review_decisions_approve_payload_backed_parent_span_subrule_substitute(tmp_path):
    component_id = "/in/union/rules/cgst-rules-2017/rule/133/subrule/3"
    parent_component_id = "/in/union/rules/cgst-rules-2017/rule/133"
    event = {
        "event_id": "evt_rule_133_payload_parent_substitute",
        "operation": "SUBSTITUTE",
        "status": "needs_review",
        "source": {"document_id": "/in/union/notifications/cbic/central-tax/2018/26-2018"},
        "legal_time": {"applicability_start": "2018-06-13", "commencement_date": "2018-06-13"},
        "system_time": {"compiled_at": "2026-06-16T00:00:00Z"},
        "target": {"work_id": "/in/union/rules/cgst-rules-2017", "component_id": component_id},
        "payload": {
            "label": "3",
            "node_type": "subrule",
            "parent_component_id": parent_component_id,
            "structural_text": (
                "―(3) Where the Authority determines that a registered person has not passed on the benefit "
                "of the reduction in the rate of tax on the supply of goods or services, the Authority may "
                "order reduction in prices, return of the amount, deposit in the Fund, penalty, and cancellation."
            ),
        },
        "evidence": {
            "excerpt": (
                "(vi) in rule 133, for sub-rule (3), the following shall be substituted, namely:- "
                "―(3) Where the Authority determines..."
            ),
            "source_span": {"start": 0, "end": 250, "text_hash": "abc"},
        },
        "validation": {"date_resolved": True, "source_span_verified": True},
        "review": {
            "required": True,
            "review_reasons": ["anchor_not_resolved", "llm_candidate_not_validated", "target_not_resolved"],
        },
    }
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(json.dumps(event) + "\n", encoding="utf-8")
    node_versions = tmp_path / "node_versions.jsonl"
    node_versions.write_text(
        json.dumps(
            {
                "component_id": parent_component_id,
                "applicability_start": "2017-06-19",
                "applicability_end": None,
                "text": (
                    "133. Order.-(1) First. (2) Second. "
                    "(3) Existing anti-profiteering order text to be replaced. (4) Fourth."
                ),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    decisions_path = tmp_path / "codex_review_decisions.json"

    result = generate_codex_review_decisions(
        events_path=events_path,
        node_versions_path=node_versions,
        output=decisions_path,
        existing_decision_paths=[],
    )

    assert result["new_decision_count"] == 1
    decision = json.loads(decisions_path.read_text(encoding="utf-8"))["decisions"][0]
    assert decision["promote"]["strategy"] == "source_backed_parent_span_subrule_substitute"
    assert decision["promote"]["apply_to_parent_subrule_span"] is True
    assert decision["promote"]["parent_component_id"] == parent_component_id


def test_codex_review_decisions_approve_parent_span_proviso_insert(tmp_path):
    parent_component_id = "/in/union/rules/cgst-rules-2017/rule/97"
    event = {
        "event_id": "evt_rule_97_parent_span_proviso",
        "operation": "INSERT_CHILD",
        "status": "needs_review",
        "source": {"document_id": "/in/union/notifications/cbic/central-tax/2018/26-2018"},
        "legal_time": {"applicability_start": "2018-06-13", "commencement_date": "2018-06-13"},
        "system_time": {"compiled_at": "2026-06-16T00:00:00Z"},
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": "/in/union/rules/cgst-rules-2017/rule/97/subrule/1/proviso/provided-further-that",
        },
        "payload": {},
        "evidence": {
            "excerpt": (
                "(v) in rule 97, in sub-rule (1), after the proviso, the following proviso shall be "
                "inserted, namely:- “Provided further that an amount equivalent to fifty per cent. "
                "of the amount of cess shall be deposited in the Fund.”;"
            ),
            "source_span": {"start": 0, "end": 240, "text_hash": "abc"},
        },
        "validation": {"date_resolved": True, "source_span_verified": True, "target_resolved": True},
        "review": {"required": True, "review_reasons": ["anchor_not_resolved", "llm_candidate_not_validated"]},
    }
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(json.dumps(event) + "\n", encoding="utf-8")
    node_versions = tmp_path / "node_versions.jsonl"
    node_versions.write_text(
        json.dumps(
            {
                "component_id": parent_component_id,
                "applicability_start": "2017-06-19",
                "applicability_end": None,
                "text": "97. Fund.-(1) Amounts shall be credited to the Fund: Provided that existing proviso applies. (2) Use of fund.",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    decisions_path = tmp_path / "codex_review_decisions.json"

    result = generate_codex_review_decisions(
        events_path=events_path,
        node_versions_path=node_versions,
        output=decisions_path,
        existing_decision_paths=[],
    )

    assert result["new_decision_count"] == 1
    decision = json.loads(decisions_path.read_text(encoding="utf-8"))["decisions"][0]
    assert decision["promote"]["strategy"] == "source_backed_parent_span_insert_child"
    assert decision["promote"]["apply_to_parent_subrule_span"] is True
    assert decision["promote"]["parent_component_id"] == parent_component_id
    assert decision["promote"]["subrule_label"] == "1"


def _completion_event(
    event_id,
    *,
    status="needs_review",
    operation="UNKNOWN",
    component_id="/in/union/rules/cgst-rules-2017/rule/10",
    reasons=None,
    excerpt="In the said rules, rule text requires review.",
    date="2020-01-01",
):
    return {
        "event_id": event_id,
        "event_type": "TEXTUAL_AMENDMENT",
        "operation": operation,
        "source": {
            "document_id": "/in/union/notifications/cbic/central-tax/2020/1-2020",
            "record_id": event_id,
            "instrument_number": "1/2020-Central Tax",
            "issuing_authority": "/in/authority/cbic",
            "publication_date": date,
            "source_url": "",
            "source_file_sha256": hashlib.sha256(event_id.encode()).hexdigest(),
            "source_text_sha256": hashlib.sha256(excerpt.encode()).hexdigest(),
        },
        "legal_time": {
            "commencement_date": date,
            "applicability_start": date,
            "applicability_end": None,
            "retrospective": False,
            "date_basis": "fixture",
        },
        "system_time": {
            "observed_at": "2026-06-16T00:00:00Z",
            "compiled_at": "2026-06-16T00:00:00Z",
            "compiler_version": "fixture",
        },
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": component_id,
            "anchor_text": None,
            "anchor_hash": None,
            "anchor_occurrence": None,
        },
        "payload": {},
        "evidence": {
            "source_span": {"start": 0, "end": len(excerpt), "text_hash": hashlib.sha256(excerpt.encode()).hexdigest()},
            "excerpt": excerpt,
            "parser_trace": {"pattern_id": "fixture", "confidence": 1.0},
        },
        "validation": {
            "target_resolved": status == "validated",
            "anchor_resolved": status == "validated",
            "date_resolved": True,
            "source_span_verified": True,
            "materializable": status == "validated",
        },
        "status": status,
        "review": {
            "required": status != "validated",
            "review_reasons": reasons or [],
            "reviewed_by": None,
            "reviewed_at": None,
        },
    }


def _write_completion_jsonl(path, rows):
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")


def test_review_completion_assigns_terminal_states(tmp_path):
    events = [
        _completion_event("evt_applied", status="validated", operation="SUBSTITUTE"),
        _completion_event(
            "evt_form",
            operation="UNKNOWN",
            component_id="/in/union/forms/gst-reg-01",
            reasons=["unsupported_form_or_table_mutation"],
            excerpt="FORM GST REG-01 shall be substituted.",
        ),
        _completion_event("evt_commence", operation="COMMENCE", reasons=["date_not_resolved"]),
        _completion_event(
            "evt_legal",
            operation="SUBSTITUTE",
            component_id="/in/union/rules/cgst-rules-2017/rule/11",
            reasons=["anchor_not_resolved"],
            excerpt="In rule 11, for the words old text, the words new text shall be substituted.",
        ),
    ]
    events_path = tmp_path / "events.jsonl"
    _write_completion_jsonl(events_path, events)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({"applied_events": [{"event_id": "evt_applied"}]}), encoding="utf-8")
    rules_coverage_path = tmp_path / "rules_coverage.json"
    rules_coverage_path.write_text(
        json.dumps({"gaps": [{"event_id": "evt_form"}, {"event_id": "evt_commence"}, {"event_id": "evt_legal"}]}),
        encoding="utf-8",
    )
    forms_coverage_path = tmp_path / "forms_coverage.json"
    forms_coverage_path.write_text(json.dumps({"gaps": [{"event_id": "evt_form"}]}), encoding="utf-8")
    triage_path = tmp_path / "triage.json"
    triage_path.write_text(
        json.dumps(
            {
                "items": [
                    {"event_id": "evt_form", "triage_class": "forms_lane", "recommended_action": "move_to_forms_lane"},
                    {
                        "event_id": "evt_commence",
                        "triage_class": "commencement_only",
                        "recommended_action": "review_commencement_mapping",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    reconciliation_path = tmp_path / "reconciliation.json"
    reconciliation_path.write_text(
        json.dumps(
            {
                "priority_review_queue": [
                    {
                        "component_id": "/in/union/rules/cgst-rules-2017/rule/10",
                        "reason": "checkpoint_mismatch",
                        "blocker": "unresolved_commencement",
                        "related_event_ids": ["evt_commence"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    result = complete_review(
        events_path=events_path,
        rules_manifest_path=manifest_path,
        rules_coverage_path=rules_coverage_path,
        forms_coverage_path=forms_coverage_path,
        reconciliation_report_path=reconciliation_path,
        review_triage_path=triage_path,
        decision_paths=[],
        report_output=tmp_path / "report.json",
        decisions_output=tmp_path / "decisions.json",
    )

    assert result["open_count"] == 1
    assert result["counts_by_terminal_state"]["materialized"] == 1
    assert result["counts_by_terminal_state"]["forms_lane_resolved"] == 1
    assert result["counts_by_terminal_state"]["commencement_blocked"] == 1
    assert result["counts_by_terminal_state"]["requires_legal_review"] == 1
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    states = {row["event_id"]: row["terminal_state"] for row in report["items"]}
    assert states["evt_applied"] == "materialized"
    assert states["evt_form"] == "forms_lane_resolved"
    assert states["evt_commence"] == "commencement_blocked"
    assert states["evt_legal"] == "requires_legal_review"


def test_review_completion_marks_source_backed_covered_event(tmp_path):
    excerpt = "In rule 10, for the words old text, the words new text shall be substituted."
    applied = _completion_event(
        "evt_applied_cover",
        status="validated",
        operation="SUBSTITUTE",
        excerpt=excerpt,
    )
    shadow = _completion_event(
        "evt_shadow",
        operation="SUBSTITUTE",
        reasons=["same_effective_date_conflict", "llm_candidate_not_validated"],
        excerpt=excerpt,
    )
    events_path = tmp_path / "events.jsonl"
    _write_completion_jsonl(events_path, [applied, shadow])
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({"applied_events": [{"event_id": "evt_applied_cover"}]}), encoding="utf-8")
    rules_coverage_path = tmp_path / "rules_coverage.json"
    rules_coverage_path.write_text(json.dumps({"gaps": [{"event_id": "evt_shadow"}]}), encoding="utf-8")
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({}), encoding="utf-8")
    triage_path = tmp_path / "triage.json"
    triage_path.write_text(json.dumps({"items": []}), encoding="utf-8")

    complete_review(
        events_path=events_path,
        rules_manifest_path=manifest_path,
        rules_coverage_path=rules_coverage_path,
        forms_coverage_path=empty,
        reconciliation_report_path=empty,
        review_triage_path=triage_path,
        decision_paths=[],
        report_output=tmp_path / "report.json",
        decisions_output=tmp_path / "decisions.json",
    )

    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    shadow_row = next(row for row in report["items"] if row["event_id"] == "evt_shadow")
    assert shadow_row["terminal_state"] == "covered_by_source_backed_event"
    assert shadow_row["covered_by_event_id"] == "evt_applied_cover"


def test_review_completion_rejects_initial_rules_publication_text(tmp_path):
    event = _completion_event(
        "evt_initial_rule_body",
        operation="UNKNOWN",
        reasons=["llm_candidate_not_validated", "unsupported_materializer_operation"],
        excerpt=(
            "46. Tax invoice.- Subject to rule 54, a tax invoice referred to in section 31 "
            "shall be issued by the registered person containing the following particulars."
        ),
    )
    event["source"]["document_id"] = "/in/union/notifications/cbic/central-tax/2017/10-2017"
    events_path = tmp_path / "events.jsonl"
    _write_completion_jsonl(events_path, [event])
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({}), encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({"applied_events": []}), encoding="utf-8")
    coverage_path = tmp_path / "coverage.json"
    coverage_path.write_text(json.dumps({"gaps": [{"event_id": "evt_initial_rule_body"}]}), encoding="utf-8")
    triage_path = tmp_path / "triage.json"
    triage_path.write_text(json.dumps({"items": []}), encoding="utf-8")

    result = complete_review(
        events_path=events_path,
        rules_manifest_path=manifest_path,
        rules_coverage_path=coverage_path,
        forms_coverage_path=empty,
        reconciliation_report_path=empty,
        review_triage_path=triage_path,
        decision_paths=[],
        report_output=tmp_path / "report.json",
        decisions_output=tmp_path / "decisions.json",
    )

    assert result["open_count"] == 0
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["items"][0]["terminal_state"] == "rejected_non_rules_text"


def test_review_completion_routes_statement_and_declaration_to_forms_lane(tmp_path):
    statement = _completion_event(
        "evt_statement",
        operation="UNKNOWN",
        reasons=["llm_candidate_not_validated", "unsupported_materializer_operation"],
        excerpt="after Statement 5A, the following Statement 5B [rule 89(2)(g)] shall be inserted.",
    )
    declaration = _completion_event(
        "evt_declaration",
        operation="SUBSTITUTE",
        reasons=["anchor_not_resolved", "llm_candidate_not_validated"],
        excerpt="for the DECLARATION [rule 89(2)(g)], the following shall be substituted.",
    )
    serial = _completion_event(
        "evt_serial",
        operation="UNKNOWN",
        reasons=["unparsed_target_work_amendment", "unsupported_materializer_operation"],
        excerpt="in serial number 7, in clause (ii), for the figures Rs. 2,50,000, the figures Rs. 1,00,000 shall be substituted.",
    )
    ecommerce = _completion_event(
        "evt_ecommerce",
        operation="UNKNOWN",
        reasons=["unparsed_target_work_amendment", "unsupported_materializer_operation"],
        excerpt="Details of the supplies made through e-commerce operators on which e-commerce operator is liable to pay tax.",
    )
    events_path = tmp_path / "events.jsonl"
    _write_completion_jsonl(events_path, [statement, declaration, serial, ecommerce])
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({}), encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({"applied_events": []}), encoding="utf-8")
    coverage_path = tmp_path / "coverage.json"
    coverage_path.write_text(
        json.dumps(
            {
                "gaps": [
                    {"event_id": "evt_statement"},
                    {"event_id": "evt_declaration"},
                    {"event_id": "evt_serial"},
                    {"event_id": "evt_ecommerce"},
                ]
            }
        ),
        encoding="utf-8",
    )
    triage_path = tmp_path / "triage.json"
    triage_path.write_text(json.dumps({"items": []}), encoding="utf-8")

    result = complete_review(
        events_path=events_path,
        rules_manifest_path=manifest_path,
        rules_coverage_path=coverage_path,
        forms_coverage_path=empty,
        reconciliation_report_path=empty,
        review_triage_path=triage_path,
        decision_paths=[],
        report_output=tmp_path / "report.json",
        decisions_output=tmp_path / "decisions.json",
    )

    assert result["open_count"] == 0
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert {row["terminal_state"] for row in report["items"]} == {"forms_lane_resolved"}


def test_review_completion_rejects_boilerplate_and_context_headers(tmp_path):
    boilerplate = _completion_event(
        "evt_boilerplate",
        operation="UNKNOWN",
        reasons=["llm_candidate_not_validated", "unsupported_materializer_operation"],
        excerpt=(
            "To be published in the Gazette of India, Extraordinary, Part II, Section 3, "
            "Government of India Ministry of Finance Notification No. 63/2018."
        ),
    )
    header = _completion_event(
        "evt_context_header",
        operation="UNKNOWN",
        reasons=["llm_limit_not_attempted", "unparsed_target_work_amendment", "unsupported_materializer_operation"],
        excerpt="(ii) in rule 96,",
    )
    effective_header = _completion_event(
        "evt_effective_context_header",
        operation="UNKNOWN",
        reasons=["llm_candidate_not_validated", "unsupported_materializer_operation"],
        excerpt="(iv) in rule 142, with effect from the 1st day of January, 2022,–",
    )
    preposed_effective_header = _completion_event(
        "evt_preposed_effective_context_header",
        operation="UNKNOWN",
        reasons=["llm_candidate_not_validated", "unsupported_materializer_operation"],
        excerpt="(x) with effect from 23rd October, 2017, in rule 96,",
    )
    events_path = tmp_path / "events.jsonl"
    _write_completion_jsonl(events_path, [boilerplate, header, effective_header, preposed_effective_header])
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({}), encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({"applied_events": []}), encoding="utf-8")
    coverage_path = tmp_path / "coverage.json"
    coverage_path.write_text(
        json.dumps(
            {
                "gaps": [
                    {"event_id": "evt_boilerplate"},
                    {"event_id": "evt_context_header"},
                    {"event_id": "evt_effective_context_header"},
                    {"event_id": "evt_preposed_effective_context_header"},
                ]
            }
        ),
        encoding="utf-8",
    )
    triage_path = tmp_path / "triage.json"
    triage_path.write_text(json.dumps({"items": []}), encoding="utf-8")

    result = complete_review(
        events_path=events_path,
        rules_manifest_path=manifest_path,
        rules_coverage_path=coverage_path,
        forms_coverage_path=empty,
        reconciliation_report_path=empty,
        review_triage_path=triage_path,
        decision_paths=[],
        report_output=tmp_path / "report.json",
        decisions_output=tmp_path / "decisions.json",
    )

    assert result["open_count"] == 0
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert {row["terminal_state"] for row in report["items"]} == {"rejected_non_rules_text"}


def test_review_completion_routes_parser_backlog_out_of_legal_review(tmp_path):
    event = _completion_event(
        "evt_parser_backlog",
        operation="SUBSTITUTE",
        reasons=["anchor_not_resolved", "llm_candidate_not_validated", "target_not_resolved"],
        excerpt="in rule 89, for sub-rule (4), the following shall be substituted.",
    )
    events_path = tmp_path / "events.jsonl"
    _write_completion_jsonl(events_path, [event])
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({}), encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({"applied_events": []}), encoding="utf-8")
    coverage_path = tmp_path / "coverage.json"
    coverage_path.write_text(json.dumps({"gaps": [{"event_id": "evt_parser_backlog"}]}), encoding="utf-8")
    triage_path = tmp_path / "triage.json"
    triage_path.write_text(
        json.dumps(
            {
                "items": [
                    {
                        "event_id": "evt_parser_backlog",
                        "triage_class": "needs_parser_support",
                        "recommended_action": "improve_resolver_or_materializer",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    result = complete_review(
        events_path=events_path,
        rules_manifest_path=manifest_path,
        rules_coverage_path=coverage_path,
        forms_coverage_path=empty,
        reconciliation_report_path=empty,
        review_triage_path=triage_path,
        decision_paths=[],
        report_output=tmp_path / "report.json",
        decisions_output=tmp_path / "decisions.json",
    )

    assert result["open_count"] == 0
    assert result["counts_by_terminal_state"]["parser_support_required"] == 1
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["items"][0]["coverage_impact"] == "incomplete"


def test_review_completion_routes_llm_extraction_backlog_out_of_legal_review(tmp_path):
    event = _completion_event(
        "evt_llm_backlog",
        operation="UNKNOWN",
        reasons=["llm_limit_not_attempted", "unparsed_target_work_amendment", "unsupported_materializer_operation"],
        excerpt="after sub-rule (1), the following sub-rule shall be inserted.",
    )
    events_path = tmp_path / "events.jsonl"
    _write_completion_jsonl(events_path, [event])
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({}), encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({"applied_events": []}), encoding="utf-8")
    coverage_path = tmp_path / "coverage.json"
    coverage_path.write_text(json.dumps({"gaps": [{"event_id": "evt_llm_backlog"}]}), encoding="utf-8")
    triage_path = tmp_path / "triage.json"
    triage_path.write_text(
        json.dumps(
            {
                "items": [
                    {
                        "event_id": "evt_llm_backlog",
                        "triage_class": "human_review",
                        "recommended_action": "llm_extract_or_human_review",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    result = complete_review(
        events_path=events_path,
        rules_manifest_path=manifest_path,
        rules_coverage_path=coverage_path,
        forms_coverage_path=empty,
        reconciliation_report_path=empty,
        review_triage_path=triage_path,
        decision_paths=[],
        report_output=tmp_path / "report.json",
        decisions_output=tmp_path / "decisions.json",
    )

    assert result["open_count"] == 0
    assert result["counts_by_terminal_state"]["llm_extraction_required"] == 1
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["items"][0]["coverage_impact"] == "incomplete"


def test_materializer_applies_parent_span_subrule_substitute_and_omit(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso>
  <act>
    <body>
      <rule refersTo="/in/union/rules/cgst-rules-2017/rule/96">
        <num>96</num>
        <heading>Refund</heading>
        <content><p>96. Refund.-(9) Earlier text. (10) Existing refund restriction. 227 Substituted vide Notf no. 54/2018 for: "(10) Old history."</p></content>
      </rule>
    </body>
  </act>
</akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-06-19"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")

    component_id = "/in/union/rules/cgst-rules-2017/rule/96/subrule/10"
    parent_component_id = "/in/union/rules/cgst-rules-2017/rule/96"
    events = [
        {
            "event_id": "evt_parent_subrule_substitute",
            "operation": "SUBSTITUTE",
            "status": "validated",
            "source": {"document_id": "/in/union/notifications/cbic/central-tax/2018/53-2018"},
            "legal_time": {"applicability_start": "2017-10-23", "commencement_date": "2017-10-23"},
            "system_time": {"compiled_at": "2026-06-16T00:00:00Z"},
            "target": {"work_id": "/in/union/rules/cgst-rules-2017", "component_id": component_id},
            "payload": {
                "apply_to_parent_subrule_span": True,
                "label": "10",
                "parent_component_id": parent_component_id,
                "structural_text": "(10) Replacement refund restriction.",
            },
            "evidence": {"source_span": {"start": 0, "end": 1, "text_hash": "abc"}, "excerpt": ""},
            "validation": {
                "target_resolved": True,
                "anchor_resolved": True,
                "date_resolved": True,
                "source_span_verified": True,
                "materializable": True,
            },
            "review": {"required": False, "review_reasons": []},
        },
        {
            "event_id": "evt_parent_subrule_omit",
            "operation": "OMIT",
            "status": "validated",
            "source": {"document_id": "/in/union/notifications/cbic/central-tax/2024/20-2024"},
            "legal_time": {"applicability_start": "2024-10-08", "commencement_date": "2024-10-08"},
            "system_time": {"compiled_at": "2026-06-16T00:00:00Z"},
            "target": {"work_id": "/in/union/rules/cgst-rules-2017", "component_id": component_id},
            "payload": {
                "apply_to_parent_subrule_span": True,
                "label": "10",
                "parent_component_id": parent_component_id,
                "whole_component": True,
            },
            "evidence": {"source_span": {"start": 0, "end": 1, "text_hash": "def"}, "excerpt": ""},
            "validation": {
                "target_resolved": True,
                "anchor_resolved": True,
                "date_resolved": True,
                "source_span_verified": True,
                "materializable": True,
            },
            "review": {"required": False, "review_reasons": []},
        },
    ]
    events_path = tmp_path / "events.jsonl"
    events_path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")

    result = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert result["applied_count"] == 2
    rows = [json.loads(line) for line in (tmp_path / "out/node_versions.jsonl").read_text().splitlines()]
    subrule_rows = [row for row in rows if row["component_id"] == component_id]
    parent_rows = [row for row in rows if row["component_id"] == parent_component_id]
    assert [row["text"] for row in subrule_rows] == ["(10) Replacement refund restriction.", "[Omitted]"]
    assert "Replacement refund restriction" in parent_rows[-2]["text"]
    assert "[Omitted]" in parent_rows[-1]["text"]


def test_materializer_detaches_subrule_substitute_when_parent_span_missing(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso>
  <act>
    <body>
      <rule refersTo="/in/union/rules/cgst-rules-2017/rule/96">
        <num>96</num>
        <heading>Refund</heading>
        <content><p>96. Refund.-(8) Earlier text. Section references include (10) and (11), but no top-level subrule ten.</p></content>
      </rule>
    </body>
  </act>
</akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-06-19"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")

    component_id = "/in/union/rules/cgst-rules-2017/rule/96/subrule/10"
    parent_component_id = "/in/union/rules/cgst-rules-2017/rule/96"
    events = [
        {
            "event_id": "evt_detached_subrule_substitute",
            "operation": "SUBSTITUTE",
            "status": "validated",
            "source": {"document_id": "/in/union/notifications/cbic/central-tax/2018/53-2018"},
            "legal_time": {"applicability_start": "2017-10-23", "commencement_date": "2017-10-23"},
            "system_time": {"compiled_at": "2026-06-16T00:00:00Z"},
            "target": {"work_id": "/in/union/rules/cgst-rules-2017", "component_id": component_id},
            "payload": {
                "apply_to_parent_subrule_span": True,
                "label": "10",
                "parent_component_id": parent_component_id,
                "structural_text": "(10) Detached replacement refund restriction.",
            },
            "evidence": {"source_span": {"start": 0, "end": 1, "text_hash": "abc"}, "excerpt": ""},
            "validation": {
                "target_resolved": True,
                "anchor_resolved": True,
                "date_resolved": True,
                "source_span_verified": True,
                "materializable": True,
            },
            "review": {"required": False, "review_reasons": []},
        }
    ]
    events_path = tmp_path / "events.jsonl"
    events_path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")

    result = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert result["applied_count"] == 1
    assert result["coverage_gap_count"] == 1
    assert result["applied_events"][0]["changed_components"] == [component_id]
    assert result["coverage_gaps"].endswith("coverage_gaps.json")
    gaps = json.loads((tmp_path / "out/coverage_gaps.json").read_text(encoding="utf-8"))["gaps"]
    assert gaps[0]["skip_reason"].startswith("partial_apply: parent_subrule_span_missing")
    rows = [json.loads(line) for line in (tmp_path / "out/node_versions.jsonl").read_text().splitlines()]
    subrule_rows = [row for row in rows if row["component_id"] == component_id]
    parent_rows = [row for row in rows if row["component_id"] == parent_component_id]
    assert [row["text"] for row in subrule_rows] == ["(10) Detached replacement refund restriction."]
    assert len(parent_rows) == 1
    assert "Detached replacement" not in parent_rows[0]["text"]


def test_materializer_allows_same_date_insert_then_whole_rule_omit(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso>
  <act>
    <body>
      <chapter>
        <num>XI</num>
        <heading>Refund</heading>
        <article refersTo="/in/union/rules/cgst-rules-2017/rule/95">
          <num>95</num>
          <heading>Refund by certain persons</heading>
          <content><p>95. Refund by certain persons.</p></content>
        </article>
      </chapter>
    </body>
  </act>
</akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-06-19"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")

    def event_template(event_id, operation, publication_date, payload):
        return {
            "event_id": event_id,
            "event_type": "TEXTUAL_AMENDMENT",
            "operation": operation,
            "source": {
                "document_id": f"/test/{event_id}",
                "record_id": event_id,
                "instrument_number": event_id,
                "issuing_authority": "/in/authority/cbic",
                "publication_date": publication_date,
                "source_url": "",
                "source_file_sha256": "0" * 64,
                "source_text_sha256": "1" * 64,
            },
            "legal_time": {
                "commencement_date": "2019-07-01",
                "applicability_start": "2019-07-01",
                "applicability_end": None,
                "retrospective": publication_date > "2019-07-01",
                "date_basis": "fixture",
            },
            "system_time": {
                "observed_at": "2026-06-16T00:00:00Z",
                "compiled_at": "2026-06-16T00:00:00Z",
                "compiler_version": "test",
            },
            "target": {
                "work_id": "/in/union/rules/cgst-rules-2017",
                "component_id": "/in/union/rules/cgst-rules-2017/rule/95a",
                "anchor_component_id": "/in/union/rules/cgst-rules-2017/rule/95",
                "anchor_text": "/in/union/rules/cgst-rules-2017/rule/95",
                "anchor_occurrence": 1,
            },
            "payload": payload,
            "evidence": {
                "source_span": {"start": 0, "end": 10, "text_hash": "3" * 64},
                "excerpt": event_id,
                "parser_trace": {"pattern_id": "fixture", "confidence": 1.0},
            },
            "validation": {
                "target_resolved": True,
                "anchor_resolved": True,
                "date_resolved": True,
                "source_span_verified": True,
                "materializable": True,
            },
            "status": "validated",
            "review": {"required": False, "review_reasons": []},
        }

    events = [
        event_template(
            "evt_95a_insert",
            "INSERT_SIBLING",
            "2019-06-28",
            {
                "node_type": "rule",
                "label": "95A",
                "heading": "Refund of taxes to retail outlets",
                "content": "Original rule 95A text.",
                "position": "after",
                "anchor_component_id": "/in/union/rules/cgst-rules-2017/rule/95",
            },
        ),
        event_template("evt_95a_omit", "OMIT", "2022-07-05", {"whole_component": True}),
    ]
    events_path = tmp_path / "events.jsonl"
    events_path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")

    out = tmp_path / "versions"
    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "missing-corpus",
        output_dir=out,
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["applied_count"] == 2
    assert manifest["conflict_count"] == 0
    rows = [json.loads(line) for line in (out / "node_versions.jsonl").read_text(encoding="utf-8").splitlines()]
    rule_95a_rows = [row for row in rows if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/95a"]
    assert [row["created_by_event_id"] for row in rule_95a_rows] == ["evt_95a_insert", "evt_95a_omit"]
    assert rule_95a_rows[-1]["valid_from"] == "2019-07-01"
    assert "[Omitted]" in rule_95a_rows[-1]["text"]


def test_materializer_splits_compound_rule_124_125_omission(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso>
  <act>
    <body>
      <article refersTo="/in/union/rules/cgst-rules-2017/rule/124">
        <num>124</num>
        <heading>Appointment, salary, allowances and other terms</heading>
        <content><p>124. Appointment, salary, allowances and other terms and conditions of the Chairman and Members of the Authority.</p></content>
      </article>
      <article refersTo="/in/union/rules/cgst-rules-2017/rule/125">
        <num>125</num>
        <heading>Secretary to the Authority</heading>
        <content><p>125. Secretary to the Authority.- An officer not below the rank of Additional Commissioner shall be the Secretary to the Authority.</p></content>
      </article>
    </body>
  </act>
</akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-06-19"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")

    event = {
        "event_id": "evt_cbic_55425afaccddf783",
        "event_type": "TEXTUAL_AMENDMENT",
        "operation": "OMIT",
        "status": "needs_review",
        "source": {
            "document_id": "/in/union/notifications/cbic/central-tax/2022/24-2022",
            "record_id": "1009557",
            "publication_date": "2022-11-23",
        },
        "legal_time": {"applicability_start": "2022-12-01", "commencement_date": "2022-12-01"},
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": "/in/union/rules/cgst-rules-2017/rule/122",
            "anchor_text": "rules 124 and 125 shall be omitted",
        },
        "payload": {"target_rules": ["124", "125"]},
        "evidence": {
            "excerpt": "(b) rules 124 and 125 shall be omitted;",
            "source_span": {"start": 867, "end": 906, "text_hash": "rule124-125-omit"},
        },
        "review": {
            "required": True,
            "review_reasons": ["document_scope_target_not_materializable", "llm_candidate_not_validated"],
        },
        "validation": {
            "target_resolved": True,
            "anchor_resolved": False,
            "date_resolved": True,
            "source_span_verified": True,
            "materializable": False,
        },
    }
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(json.dumps(event) + "\n", encoding="utf-8")

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["coverage_gap_count"] == 0
    assert manifest["applied_count"] == 2
    rows = [json.loads(line) for line in (tmp_path / "out/node_versions.jsonl").read_text().splitlines()]
    by_component = {}
    for row in rows:
        by_component.setdefault(row["component_id"], []).append(row)
    rule_124 = by_component["/in/union/rules/cgst-rules-2017/rule/124"][-1]
    rule_125 = by_component["/in/union/rules/cgst-rules-2017/rule/125"][-1]
    assert rule_124["text"] == "[Omitted]"
    assert rule_125["text"] == "[Omitted]"
    assert rule_124["created_by_event_id"] == "evt_cbic_55425afaccddf783_rule_124"
    assert rule_125["created_by_event_id"] == "evt_cbic_55425afaccddf783_rule_125"


def test_materializer_retargets_rule_109a_subrule_substitutions_to_parent(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso>
  <act>
    <body>
      <article refersTo="/in/union/rules/cgst-rules-2017/rule/109a">
        <num>109A</num>
        <heading>Appointment of Appellate Authority</heading>
        <content><p>109A. Appointment of Appellate Authority.- (1) Any person may appeal to the Additional Commissioner (Appeals) within three months. (2) An officer may appeal to the Additional Commissioner (Appeals) within six months.</p></content>
      </article>
    </body>
  </act>
</akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-06-19"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")

    base_event = {
        "event_type": "TEXTUAL_AMENDMENT",
        "operation": "SUBSTITUTE",
        "status": "validated",
        "source": {
            "document_id": "/in/union/notifications/cbic/central-tax/2018/60-2018",
            "record_id": "1000737",
            "publication_date": "2018-10-30",
        },
        "legal_time": {"applicability_start": "2018-10-30", "commencement_date": "2018-10-30"},
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": "/in/union/rules/cgst-rules-2017/rule/109a/subrule/1",
        },
        "payload": {
            "old_text": "the Additional Commissioner (Appeals)",
            "new_text": "any officer not below the rank of Joint Commissioner (Appeals)",
        },
        "evidence": {
            "excerpt": "in sub-rule (1), in clause (b), for the words and brackets \"the Additional Commissioner (Appeals)\", the following words and brackets shall be substituted, namely:- \"any officer not below the rank of Joint Commissioner (Appeals)\";",
            "source_span": {"start": 7907, "end": 8140, "text_hash": "rule109a-subrule1"},
        },
        "validation": {
            "target_resolved": True,
            "anchor_resolved": True,
            "date_resolved": True,
            "source_span_verified": True,
            "materializable": True,
        },
    }
    subrule_1 = {**base_event, "event_id": "evt_cbic_548f3453e01d95a1"}
    subrule_2 = {
        **base_event,
        "event_id": "evt_cbic_0e042222febcbbb3",
        "target": {
            **base_event["target"],
            "component_id": "/in/union/rules/cgst-rules-2017/rule/109a/subrule/2",
        },
        "payload": {"old_text": None, "new_text": None},
        "evidence": {
            "excerpt": "in sub-rule (2), in clause (b), for the words and brackets \"the Additional Commissioner (Appeals)\", the following words and brackets shall be substituted, namely:- \"any officer not below the rank of Joint Commissioner (Appeals)\".",
            "source_span": {"start": 8141, "end": 8374, "text_hash": "rule109a-subrule2"},
        },
    }
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(
        json.dumps(subrule_1) + "\n" + json.dumps(subrule_2) + "\n",
        encoding="utf-8",
    )

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["coverage_gap_count"] == 0
    assert manifest["applied_count"] == 2
    rows = [json.loads(line) for line in (tmp_path / "out/node_versions.jsonl").read_text().splitlines()]
    rule_109a = [
        row for row in rows if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/109a"
    ][-1]
    assert rule_109a["text"].count("any officer not below the rank of Joint Commissioner (Appeals)") == 2
    assert "the Additional Commissioner (Appeals)" not in rule_109a["text"]


def test_materializer_allows_same_source_same_date_ordered_text_edits(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso>
  <act>
    <body>
      <chapter>
        <article refersTo="/in/union/rules/cgst-rules-2017/rule/8">
          <num>8</num>
          <heading>Application for registration</heading>
          <content><p>8. Every person shall make an application electronically and furnish the prescribed particulars.</p></content>
        </article>
      </chapter>
    </body>
  </act>
</akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-06-19"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")

    def splice_event(event_id, start, anchor, insert_text):
        return {
            "event_id": event_id,
            "event_type": "TEXTUAL_AMENDMENT",
            "operation": "SPLICE",
            "source": {
                "document_id": "/in/union/notifications/cbic/central-tax/2020/fixture",
                "record_id": "1",
                "instrument_number": "1/2020-Central Tax",
                "issuing_authority": "/in/authority/cbic",
                "publication_date": "2020-01-01",
                "source_url": "",
                "source_file_sha256": "0" * 64,
                "source_text_sha256": "1" * 64,
            },
            "legal_time": {
                "commencement_date": "2020-01-01",
                "applicability_start": "2020-01-01",
                "applicability_end": None,
                "retrospective": False,
                "date_basis": "fixture",
            },
            "system_time": {
                "observed_at": "2026-06-16T00:00:00Z",
                "compiled_at": "2026-06-16T00:00:00Z",
                "compiler_version": "test",
            },
            "target": {
                "work_id": "/in/union/rules/cgst-rules-2017",
                "component_id": "/in/union/rules/cgst-rules-2017/rule/8",
                "anchor_text": anchor,
                "anchor_occurrence": 1,
            },
            "payload": {"insert_text": insert_text, "position": "after"},
            "evidence": {
                "source_span": {"start": start, "end": start + 10, "text_hash": hashlib.sha256(event_id.encode()).hexdigest()},
                "excerpt": event_id,
                "parser_trace": {"pattern_id": "fixture", "confidence": 1.0},
            },
            "validation": {
                "target_resolved": True,
                "anchor_resolved": True,
                "date_resolved": True,
                "source_span_verified": True,
                "materializable": True,
            },
            "status": "validated",
            "review": {"required": False, "review_reasons": []},
        }

    events = [
        splice_event("evt_rule_8_first_splice", 10, "application", "for registration"),
        splice_event("evt_rule_8_second_splice", 40, "prescribed particulars", "and documents"),
    ]
    events_path = tmp_path / "events.jsonl"
    events_path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
    out = tmp_path / "versions"

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "missing-corpus",
        output_dir=out,
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["applied_count"] == 2
    assert manifest["conflict_count"] == 0
    rows = [json.loads(line) for line in (out / "node_versions.jsonl").read_text(encoding="utf-8").splitlines()]
    rule_rows = [row for row in rows if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/8"]
    assert "application for registration electronically" in rule_rows[-1]["text"]
    assert "prescribed particulars and documents" in rule_rows[-1]["text"]


def test_materializer_creates_missing_subrule_target_from_parent_span(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso>
  <act>
    <body>
      <article refersTo="/in/union/rules/cgst-rules-2017/rule/142">
        <num>142</num>
        <heading>Notice and order for demand</heading>
        <content><p>142. Notice and order.- (1) First subrule. (5) A summary of the order issued of section 76 shall be uploaded electronically in FORM GST DRC-07.</p></content>
      </article>
    </body>
  </act>
</akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-06-19"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(
        json.dumps(
            {
                "event_id": "evt_rule_142_subrule_5_splice",
                "operation": "SPLICE",
                "status": "needs_review",
                "source": {
                    "document_id": "/in/union/notifications/cbic/central-tax/2018/28-2018",
                    "publication_date": "2018-06-19",
                },
                "legal_time": {"applicability_start": "2018-06-19"},
                "target": {
                    "work_id": "/in/union/rules/cgst-rules-2017",
                    "component_id": "/in/union/rules/cgst-rules-2017/rule/142/subrule/5",
                    "anchor_text": "of section 76",
                },
                "payload": {"insert_text": "or section 129 or section 130", "position": "after"},
                "evidence": {"source_span": {"start": 0, "text_hash": "abc"}},
                "review": {"review_reasons": ["target_not_resolved", "anchor_not_resolved"]},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["applied_count"] == 1
    rows = [json.loads(line) for line in (tmp_path / "out/node_versions.jsonl").read_text(encoding="utf-8").splitlines()]
    subrule_rows = [
        row for row in rows
        if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/142/subrule/5"
    ]
    assert len(subrule_rows) == 2
    assert "section 76 or section 129 or section 130" in subrule_rows[-1]["text"]


def test_materializer_repairs_rule_117_1a_source_wrapper_event(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso>
  <act>
    <body>
      <article refersTo="/in/union/rules/cgst-rules-2017/rule/117">
        <num>117</num>
        <heading>Tax or duty credit carried forward</heading>
        <content><p>117. Tax or duty credit carried forward.- (1) Existing subrule one text. (4) (a) Opening text. (b) Conditions. (iii) The scheme shall be available for six tax periods from the appointed date.</p></content>
        <subrule refersTo="/in/union/rules/cgst-rules-2017/rule/117/subrule/4">
          <num>4</num>
          <content><p>(4) (a) Opening text. (b) Conditions. (iii) The scheme shall be available for six tax periods from the appointed date.</p></content>
        </subrule>
      </article>
    </body>
  </act>
</akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-06-19"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")
    source = {
        "document_id": "/in/union/notifications/cbic/central-tax/2018/48-2018",
        "publication_date": "2018-09-10",
    }
    wrapper_event = {
        "event_id": "evt_cbic_91c3aa7f59985dec",
        "operation": "UNKNOWN",
        "status": "needs_review",
        "source": source,
        "legal_time": {"applicability_start": "2018-09-10"},
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": "/in/union/rules/cgst-rules-2017/rule/117",
        },
        "payload": {},
        "evidence": {"source_span": {"start": 0, "text_hash": "abc"}},
        "review": {"review_reasons": ["unsupported_materializer_operation"]},
    }
    amend_2019 = {
        "event_id": "evt_cbic_252fae793995454f",
        "operation": "SUBSTITUTE",
        "status": "needs_review",
        "source": {"document_id": "/in/union/notifications/cbic/central-tax/2019/49-2019", "publication_date": "2019-10-09"},
        "legal_time": {"applicability_start": "2019-10-09"},
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": "/in/union/rules/cgst-rules-2017/rule/117/subrule/1a",
        },
        "payload": {"old_text": None, "new_text": None},
        "evidence": {"source_span": {"start": 1, "text_hash": "def"}},
        "review": {"review_reasons": ["incomplete_text_edit_payload", "target_not_resolved"]},
    }
    amend_2020 = {
        "event_id": "evt_rule_117_1a_date_substitute",
        "operation": "SUBSTITUTE",
        "status": "needs_review",
        "source": {"document_id": "/in/union/notifications/cbic/central-tax/2020/2-2020", "publication_date": "2020-01-01"},
        "legal_time": {"applicability_start": "2020-01-01"},
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": "/in/union/rules/cgst-rules-2017/rule/117/subrule/1a",
            "anchor_text": "31st December, 2019",
        },
        "payload": {"old_text": "31st December, 2019", "new_text": "31st March, 2020"},
        "evidence": {"source_span": {"start": 1, "text_hash": "def"}},
        "review": {"review_reasons": ["target_not_resolved", "anchor_not_resolved"]},
    }
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(
        "\n".join(json.dumps(event) for event in [wrapper_event, amend_2019, amend_2020]) + "\n",
        encoding="utf-8",
    )

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["applied_count"] == 4
    rows = [json.loads(line) for line in (tmp_path / "out/node_versions.jsonl").read_text(encoding="utf-8").splitlines()]
    subrule_rows = [
        row for row in rows
        if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/117/subrule/1a"
    ]
    assert len(subrule_rows) == 3
    assert "31st March, 2019" in subrule_rows[0]["text"]
    assert "31st December, 2019" in subrule_rows[1]["text"]
    assert "31st March, 2020" in subrule_rows[2]["text"]


def test_materializer_repairs_rule_89_third_proviso_substitution(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso>
  <act>
    <body>
      <article refersTo="/in/union/rules/cgst-rules-2017/rule/89">
        <num>89</num>
        <heading>Application for refund</heading>
        <content><p>89. Application for refund.- (1) Any person may file an application: Provided also that in respect of supplies regarded as deemed exports, the application shall be filed by the recipient of deemed export supplies: Provided also that other refund text continues. (2) Supporting documents follow.</p></content>
      </article>
    </body>
  </act>
</akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-06-19"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")
    event = {
        "event_id": "evt_cbic_a55addc1351a6797",
        "operation": "SUBSTITUTE",
        "status": "validated",
        "source": {
            "document_id": "/in/union/notifications/cbic/central-tax/2017/47-2017",
            "publication_date": "2017-10-18",
        },
        "legal_time": {"applicability_start": "2017-10-18"},
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": "/in/union/rules/cgst-rules-2017/rule/89",
            "anchor_text": "third proviso",
        },
        "payload": {
            "old_text": "third proviso",
            "new_text": (
                "Provided also that in respect of supplies regarded as deemed exports, the application may be filed by, - "
                "(a) the recipient of deemed export supplies; or (b) the supplier of deemed export supplies in cases "
                "where the recipient does not avail of input tax credit on such supplies and furnishes an undertaking "
                "to the effect that the supplier may claim the refund"
            ),
            "noop_if_already_reflected": True,
        },
        "validation": {
            "target_resolved": True,
            "anchor_resolved": True,
            "date_resolved": True,
            "source_span_verified": True,
            "materializable": True,
        },
        "evidence": {"source_span": {"start": 0, "text_hash": "abc"}},
        "review": {"required": False, "review_reasons": []},
    }
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(json.dumps(event) + "\n", encoding="utf-8")

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["applied_count"] == 1
    assert manifest["coverage_gap_count"] == 0
    rows = [json.loads(line) for line in (tmp_path / "out/node_versions.jsonl").read_text(encoding="utf-8").splitlines()]
    rule89_rows = [row for row in rows if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/89"]
    assert len(rule89_rows) == 2
    assert "application shall be filed by the recipient" in rule89_rows[0]["text"]
    assert "the supplier of deemed export supplies" in rule89_rows[1]["text"]
    assert "application shall be filed by the recipient" not in rule89_rows[1]["text"]


def test_materializer_syncs_parent_subrule_substitution_when_markers_ambiguous(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso>
  <act>
    <body>
      <article refersTo="/in/union/rules/cgst-rules-2017/rule/42">
        <num>42</num>
        <heading>Ambiguous sub-rule form</heading>
        <content><p>42. Ambiguous sub-rule form.- 1 The supplier shall pay no fee. 2 The importer shall pay no fee.</p></content>
        <paragraph refersTo="/in/union/rules/cgst-rules-2017/rule/42/subrule/1">
          <num>1</num>
          <content><p>1 The supplier shall pay no fee.</p></content>
        </paragraph>
      </article>
    </body>
  </act>
</akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-06-19"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")

    events_path = tmp_path / "events.jsonl"
    events_path.write_text(
        json.dumps(
            {
                "event_id": "evt_ambiguous_subrule_marker",
                "operation": "SUBSTITUTE",
                "status": "validated",
                "source": {
                    "document_id": "/in/union/notifications/cbic/central-tax/2019/fixture",
                    "publication_date": "2019-01-01",
                },
                "legal_time": {"applicability_start": "2019-01-01"},
                "target": {
                    "work_id": "/in/union/rules/cgst-rules-2017",
                    "component_id": "/in/union/rules/cgst-rules-2017/rule/42/subrule/1",
                },
                "payload": {"old_text": "pay no fee", "new_text": "pay zero fee"},
                "evidence": {"source_span": {"start": 0, "text_hash": "fixture"}},
                "validation": {
                    "target_resolved": True,
                    "anchor_resolved": True,
                    "date_resolved": True,
                    "source_span_verified": True,
                    "materializable": True,
                },
                "review": {"required": False, "review_reasons": []},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["coverage_gap_count"] == 0
    rows = [
        json.loads(line)
        for line in (tmp_path / "out/node_versions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    subrule_row = [
        row
        for row in rows
        if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/42/subrule/1"
    ][-1]
    rule_row = [
        row
        for row in rows
        if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/42"
    ][-1]
    assert "1 The supplier shall pay zero fee." in subrule_row["text"]
    assert "The supplier shall pay zero fee" in rule_row["text"]


def test_materializer_repairs_rule_89_subrule_2_clause_f_substitution(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso>
  <act>
    <body>
      <article refersTo="/in/union/rules/cgst-rules-2017/rule/89">
        <num>89</num>
        <heading>Application for refund</heading>
        <content><p>89. Application for refund.- (2) The application shall be accompanied by documents. (e) evidence regarding receipt of goods or services; (f) a declaration to the effect that the Special Economic Zone unit or the Special Economic Zone developer has not availed the input tax credit of the tax paid by the supplier of goods or services or both, in a case where the refund is on account of supply of goods or services made to a Special Economic Zone unit or a Special Economic Zone developer; (g) deemed export statement.</p></content>
      </article>
    </body>
  </act>
</akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-06-19"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")
    event = {
        "event_id": "evt_cbic_9cc935da632e9aac",
        "operation": "SUBSTITUTE",
        "status": "needs_review",
        "source": {
            "document_id": "/in/union/notifications/cbic/central-tax/2019/3-2019",
            "publication_date": "2019-01-29",
        },
        "legal_time": {"applicability_start": "2019-02-18"},
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": "/in/union/rules/cgst-rules-2017/rule/89/subrule/2/clause/f",
            "anchor_text": "clause (f)",
        },
        "payload": {
            "old_text": "clause (f)",
            "new_text": (
                "(f) a declaration to the effect that tax has not been collected from the Special Economic Zone unit "
                "or the Special Economic Zone developer, in a case where the refund is on account of supply of goods "
                "or services or both made to a Special Economic Zone unit or a Special Economic Zone developer;"
            ),
        },
        "validation": {
            "target_resolved": False,
            "anchor_resolved": False,
            "date_resolved": True,
            "source_span_verified": True,
            "materializable": False,
        },
        "evidence": {"source_span": {"start": 0, "text_hash": "abc"}},
        "review": {"required": True, "review_reasons": ["target_not_resolved"]},
    }
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(json.dumps(event) + "\n", encoding="utf-8")

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["applied_count"] == 1
    assert manifest["coverage_gap_count"] == 0
    rows = [json.loads(line) for line in (tmp_path / "out/node_versions.jsonl").read_text(encoding="utf-8").splitlines()]
    rule89_rows = [row for row in rows if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/89"]
    assert len(rule89_rows) == 2
    assert "has not availed the input tax credit" in rule89_rows[0]["text"]
    assert "tax has not been collected from the Special Economic Zone unit" in rule89_rows[1]["text"]
    assert "has not availed the input tax credit" not in rule89_rows[1]["text"]


def test_materializer_repairs_rule_89_subrule_4_clause_c_substitution(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso>
  <act>
    <body>
      <article refersTo="/in/union/rules/cgst-rules-2017/rule/89">
        <num>89</num>
        <heading>Application for refund</heading>
        <content><p>89. Application for refund.- (4) Refund Amount = (Turnover of zero-rated supply of goods + Turnover of zero-rated supply of services) x Net ITC ÷ Adjusted Total Turnover Where, - (A) Refund amount means maximum refund; (B) Net ITC means input tax credit; (C) "Turnover of zero-rated supply of goods" means the value of zero-rated supply of goods made during the relevant period without payment of tax under bond or letter of undertaking, other than the turnover of supplies in respect of which refund is claimed under sub-rules (4A) or (4B) or both; (D) Turnover of zero-rated supply of services follows.</p></content>
      </article>
    </body>
  </act>
</akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-06-19"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")
    event = {
        "event_id": "evt_cbic_e23ce17aa2de96c4",
        "operation": "SUBSTITUTE",
        "status": "needs_review",
        "source": {
            "document_id": "/in/union/notifications/cbic/central-tax/2020/16-2020",
            "publication_date": "2020-03-23",
        },
        "legal_time": {"applicability_start": "2020-03-23"},
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": "/in/union/rules/cgst-rules-2017/rule/89",
            "anchor_text": "clause (4)",
        },
        "payload": {"old_text": "clause (4)", "new_text": None},
        "validation": {
            "target_resolved": True,
            "anchor_resolved": False,
            "date_resolved": True,
            "source_span_verified": True,
            "materializable": False,
        },
        "evidence": {"source_span": {"start": 0, "text_hash": "abc"}},
        "review": {"required": True, "review_reasons": ["incomplete_text_edit_payload"]},
    }
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(json.dumps(event) + "\n", encoding="utf-8")

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["applied_count"] == 1
    assert manifest["coverage_gap_count"] == 0
    rows = [json.loads(line) for line in (tmp_path / "out/node_versions.jsonl").read_text(encoding="utf-8").splitlines()]
    rule89_rows = [row for row in rows if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/89"]
    assert len(rule89_rows) == 2
    assert "1.5 times the value of like goods domestically supplied" not in rule89_rows[0]["text"]
    assert "1.5 times the value of like goods domestically supplied" in rule89_rows[1]["text"]
    assert "or the value which is 1.5 times" in rule89_rows[1]["text"]


def test_materializer_repairs_rule_89_subrule_5_formula_substitution(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso>
  <act>
    <body>
      <article refersTo="/in/union/rules/cgst-rules-2017/rule/89">
        <num>89</num>
        <heading>Application for refund</heading>
        <content><p>89. Application for refund.- (5) In the case of refund on account of inverted duty structure, refund of input tax credit shall be granted as per the following formula - Maximum Refund Amount = {(Turnover of inverted rated supply of goods) x Net ITC ÷ Adjusted Total Turnover} - tax payable on such inverted rated supply of goods Explanation.- For the purposes of this sub rule, the expressions "Net ITC" and "Adjusted Total turnover" shall have the same meanings as assigned to them in sub-rule (4).</p></content>
      </article>
    </body>
  </act>
</akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-06-19"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")
    event = {
        "event_id": "evt_cbic_d8f4a0a217fe1492",
        "operation": "SUBSTITUTE",
        "status": "needs_review",
        "source": {
            "document_id": "/in/union/notifications/cbic/central-tax/2022/14-2022",
            "publication_date": "2022-07-05",
        },
        "legal_time": {"applicability_start": "2022-07-05"},
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": "/in/union/rules/cgst-rules-2017/rule/89/subrule/5",
        },
        "payload": {"old_text": "", "new_text": ""},
        "validation": {
            "target_resolved": False,
            "anchor_resolved": True,
            "date_resolved": True,
            "source_span_verified": True,
            "materializable": False,
        },
        "evidence": {"source_span": {"start": 0, "text_hash": "abc"}},
        "review": {"required": True, "review_reasons": ["incomplete_text_edit_payload"]},
    }
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(json.dumps(event) + "\n", encoding="utf-8")

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["applied_count"] == 1
    assert manifest["coverage_gap_count"] == 0
    rows = [json.loads(line) for line in (tmp_path / "out/node_versions.jsonl").read_text(encoding="utf-8").splitlines()]
    rule89_rows = [row for row in rows if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/89"]
    assert len(rule89_rows) == 2
    assert "{tax payable on such inverted rated supply of goods and services x" not in rule89_rows[0]["text"]
    assert "{tax payable on such inverted rated supply of goods and services x" in rule89_rows[1]["text"]
    assert "ITC availed on inputs and input services" in rule89_rows[1]["text"]


def test_materializer_repairs_rule_89_19_2022_compound_subrule_1_edits(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso>
  <act>
    <body>
      <article refersTo="/in/union/rules/cgst-rules-2017/rule/89">
        <num>89</num>
        <heading>Application for refund</heading>
        <content><p>89. Application for refund.- (1) Any person, except the persons covered under notification issued under section 55, claiming refund of any tax, interest, penalty, fees or any other amount paid by him, other than refund of integrated tax paid on goods exported out of India, may file , subject to the provisions of rule 10B, an application electronically in FORM GST RFD-01 through the common portal, either directly or through a Facilitation Centre notified by the Commissioner: Provided that any claim for refund relating to balance in the electronic cash ledger in accordance with the provisions of sub-section (6) of section 49 may be made through the return furnished for the relevant tax period in FORM GSTR-3 or FORM GSTR-4 or FORM GSTR-7, as the case may be: Provided further that in respect of supplies to a Special Economic Zone unit or a Special Economic Zone developer, the application for refund shall be filed by the - (a) supplier of goods after such goods have been admitted in full in the Special Economic Zone for authorised operations, as endorsed by the specified officer of the Zone; (b) supplier of services along with such evidence regarding receipt of services for authorised operations as endorsed by the specified officer of the Zone: Provided also that in respect of supplies regarded as deemed exports, the application shall be filed by the recipient of deemed export supplies: Provided also that refund of any amount, after adjusting tax payable by the applicant, shall be claimed in the last return. (2) Documentary evidence shall be furnished.</p></content>
      </article>
    </body>
  </act>
</akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-06-19"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")
    event = {
        "event_id": "evt_cbic_28517a6b6d58aacb",
        "operation": "SPLICE",
        "status": "needs_review",
        "source": {
            "document_id": "/in/union/notifications/cbic/central-tax/2022/19-2022",
            "publication_date": "2022-09-28",
        },
        "legal_time": {"applicability_start": "2022-10-01"},
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": "/in/union/rules/cgst-rules-2017/rule/89",
            "anchor_text": "claiming refund of",
            "anchor_occurrence": 1,
        },
        "payload": {
            "position": "after",
            "insert_text": (
                "any balance in the electronic cash ledger in accordance with the provisions "
                "of sub-section (6) of section 49 or"
            ),
        },
        "validation": {
            "target_resolved": True,
            "anchor_resolved": True,
            "date_resolved": True,
            "source_span_verified": True,
            "materializable": False,
        },
        "evidence": {"source_span": {"start": 4559, "text_hash": "abc"}},
        "review": {
            "required": True,
            "review_reasons": [
                "compound_block_contains_multiple_amendments",
                "compound_block_contains_unsupported_omission",
            ],
        },
    }
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(json.dumps(event) + "\n", encoding="utf-8")

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["applied_count"] == 1
    assert manifest["coverage_gap_count"] == 0
    rows = [json.loads(line) for line in (tmp_path / "out/node_versions.jsonl").read_text(encoding="utf-8").splitlines()]
    rule89_rows = [row for row in rows if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/89"]
    final_text = rule89_rows[-1]["text"]
    assert "claiming refund of any balance in the electronic cash ledger" in final_text
    assert "may be made through the return furnished for the relevant tax period" not in final_text
    assert "Provided that in respect of supplies to a Special Economic Zone" in final_text
    assert "Provided further that in respect of supplies regarded as deemed exports" in final_text
    assert "Provided also that refund of any amount" in final_text


def test_materializer_repairs_rule_89_35_2021_commenced_subrule_1_and_1a(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso>
  <act>
    <body>
      <article refersTo="/in/union/rules/cgst-rules-2017/rule/89">
        <num>89</num>
        <heading>Application for refund</heading>
        <content><p>89. Application for refund.- (1) Any person, except the persons covered under notification issued under section 55, claiming refund of any tax, interest, penalty, fees or any other amount paid by him, other than refund of integrated tax paid on goods exported out of India, may file an application electronically in FORM GST RFD-01 through the common portal, either directly or through a Facilitation Centre notified by the Commissioner. (2) Documentary evidence shall be furnished.</p></content>
      </article>
    </body>
  </act>
</akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-06-19"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")
    event = {
        "event_id": "evt_cbic_777f0208dde2bedb",
        "operation": "UNKNOWN",
        "status": "needs_review",
        "source": {
            "document_id": "/in/union/notifications/cbic/central-tax/2021/35-2021",
            "instrument_number": "35/2021-Central Tax",
            "publication_date": "2021-09-24",
        },
        "legal_time": {"applicability_start": "2021-09-24"},
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": "/in/union/rules/cgst-rules-2017/rule/89",
        },
        "payload": {},
        "validation": {
            "target_resolved": True,
            "anchor_resolved": False,
            "date_resolved": True,
            "source_span_verified": True,
            "materializable": False,
        },
        "evidence": {
            "excerpt": "(c) shall be omitted; (6) In rule 89 of the said rules, -",
            "source_span": {"start": 4500, "text_hash": "abc"},
        },
        "review": {"required": True, "review_reasons": ["unsupported_materializer_operation"]},
    }
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(json.dumps(event) + "\n", encoding="utf-8")

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["applied_count"] == 2
    assert manifest["coverage_gap_count"] == 0
    rows = [json.loads(line) for line in (tmp_path / "out/node_versions.jsonl").read_text(encoding="utf-8").splitlines()]
    rule89_rows = [row for row in rows if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/89"]
    subrule_1a_rows = [
        row for row in rows if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/89/subrule/1a"
    ]
    assert "subject to the provisions of rule 10B" in rule89_rows[-1]["text"]
    assert subrule_1a_rows
    assert subrule_1a_rows[-1]["applicability_start"] == "2022-01-01"
    assert "claiming refund under section 77 of the Act" in subrule_1a_rows[-1]["text"]
    assert "FORM GST RFD-01" in subrule_1a_rows[-1]["text"]


def test_materializer_creates_missing_insert_child_subrule_parent_from_parent_span(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso>
  <act>
    <body>
      <article refersTo="/in/union/rules/cgst-rules-2017/rule/138a">
        <num>138A</num>
        <heading>Documents and devices to be carried</heading>
        <content><p>138A. Documents and devices to be carried.- (1) The person in charge of a conveyance shall carry the invoice: Provided that rail movement is excluded. (2) A registered person may obtain an Invoice Reference Number.</p></content>
      </article>
    </body>
  </act>
</akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-06-19"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")
    event = {
        "event_id": "evt_rule_138a_subrule_1_proviso",
        "operation": "INSERT_CHILD",
        "status": "validated",
        "source": {
            "document_id": "/in/union/notifications/cbic/central-tax/2018/39-2018",
            "publication_date": "2018-09-04",
        },
        "legal_time": {"applicability_start": "2018-09-04"},
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": "/in/union/rules/cgst-rules-2017/rule/138a/subrule/1/proviso/imported-goods",
        },
        "payload": {
            "parent_component_id": "/in/union/rules/cgst-rules-2017/rule/138a/subrule/1",
            "label": "Provided further that",
            "node_type": "proviso",
            "content": "Provided further that in case of imported goods, the person in charge shall carry a copy of the bill of entry.",
        },
        "validation": {
            "target_resolved": True,
            "anchor_resolved": True,
            "date_resolved": True,
            "source_span_verified": True,
            "materializable": True,
        },
        "evidence": {"source_span": {"start": 0, "text_hash": "abc"}},
        "review": {"required": False, "review_reasons": []},
    }
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(json.dumps(event) + "\n", encoding="utf-8")

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["applied_count"] == 1
    assert manifest["coverage_gap_count"] == 0
    rows = [json.loads(line) for line in (tmp_path / "out/node_versions.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any(row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/138a/subrule/1" for row in rows)
    proviso_rows = [
        row for row in rows
        if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/138a/subrule/1/proviso/imported-goods"
    ]
    assert len(proviso_rows) == 1
    assert "bill of entry" in proviso_rows[0]["text"]


def test_materializer_repairs_rule_138e_parent_then_applies_clause_child(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso>
  <act>
    <body>
      <article refersTo="/in/union/rules/cgst-rules-2017/rule/138d">
        <num>138D</num>
        <heading>Facility for uploading information regarding detention of vehicle</heading>
        <content><p>138D. Facility for uploading information regarding detention of vehicle.- Where a vehicle has been intercepted and detained for a period exceeding thirty minutes, the transporter may upload the said information in FORM GST EWB-04.</p></content>
      </article>
    </body>
  </act>
</akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-06-19"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")
    parent_event = {
        "event_id": "evt_cbic_aafc21449b573369",
        "operation": "INSERT_SIBLING",
        "status": "needs_review",
        "source": {"document_id": "/in/union/notifications/cbic/central-tax/2018/74-2018", "publication_date": "2018-12-31"},
        "legal_time": {"applicability_start": "2018-12-31"},
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": "CGST_Rule_96",
            "anchor_text": "Rule 95",
        },
        "payload": {},
        "validation": {"source_span_verified": True},
        "evidence": {"source_span": {"start": 0, "text_hash": "abc"}},
        "review": {"review_reasons": ["target_not_resolved"]},
    }
    child_event = {
        "event_id": "evt_cbic_249183a480b8385f",
        "operation": "INSERT_CHILD",
        "status": "needs_review",
        "source": {"document_id": "/in/union/notifications/cbic/central-tax/2019/75-2019", "publication_date": "2019-12-26"},
        "legal_time": {"applicability_start": "2019-12-26"},
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": "/in/union/rules/cgst-rules-2017/rule/138e/subrule/(c)",
            "anchor_component_id": "/in/union/rules/cgst-rules-2017/rule/138e/clause/b",
        },
        "payload": {
            "parent_component_id": "/in/union/rules/cgst-rules-2017/rule/138e",
            "anchor_component_id": "/in/union/rules/cgst-rules-2017/rule/138e/clause/b",
            "label": "(c)",
            "node_type": "clause",
            "content": "being a person other than a person specified in clause (a), has not furnished the statement of outward supplies for any two months or quarters, as the case may be.",
        },
        "validation": {"source_span_verified": True, "date_resolved": True},
        "evidence": {"source_span": {"start": 1, "text_hash": "def"}},
        "review": {"review_reasons": ["target_not_resolved"]},
    }
    covid_2020_event = {
        "event_id": "evt_cbic_ed2a8531ecc39fe2",
        "operation": "UNKNOWN",
        "status": "needs_review",
        "source": {"document_id": "/in/union/notifications/cbic/central-tax/2020/79-2020", "publication_date": "2020-10-15"},
        "legal_time": {"applicability_start": "2020-10-15"},
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": "/in/union/forms/gst-cmp",
        },
        "payload": {"text": "in rule 138E, after the third proviso, the following proviso shall be inserted"},
        "validation": {"source_span_verified": True, "date_resolved": True},
        "evidence": {"source_span": {"start": 2, "text_hash": "ghi"}},
        "review": {"review_reasons": ["target_not_resolved"]},
    }
    opening_substitute_event = {
        "event_id": "evt_cbic_0c43dc0b8e195471",
        "operation": "SUBSTITUTE",
        "status": "needs_review",
        "source": {"document_id": "/in/union/notifications/cbic/central-tax/2021/15-2021", "publication_date": "2021-05-18"},
        "legal_time": {"applicability_start": "2021-05-18"},
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": "/in/union/rules/cgst/rules",
        },
        "payload": {"old_text": None, "new_text": None},
        "validation": {"source_span_verified": True, "date_resolved": True},
        "evidence": {"source_span": {"start": 3, "text_hash": "jkl"}},
        "review": {"review_reasons": ["target_not_resolved"]},
    }
    covid_2021_event = {
        "event_id": "evt_cbic_cbcd524d7f43f130",
        "operation": "UNKNOWN",
        "status": "needs_review",
        "source": {"document_id": "/in/union/notifications/cbic/central-tax/2021/32-2021", "publication_date": "2021-08-29"},
        "legal_time": {"applicability_start": "2021-08-29"},
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": "/in/union/forms/gst-cmp-08",
        },
        "payload": {"text": "in rule 138E, after the fourth proviso, the following proviso shall be inserted"},
        "validation": {"source_span_verified": True, "date_resolved": True},
        "evidence": {"source_span": {"start": 4, "text_hash": "mno"}},
        "review": {"review_reasons": ["target_not_resolved"]},
    }
    rule_138f_event = {
        "event_id": "evt_cbic_649de854081f52c7",
        "operation": "INSERT_SIBLING",
        "status": "needs_review",
        "source": {"document_id": "/in/union/notifications/cbic/central-tax/2023/38-2023", "publication_date": "2023-08-04"},
        "legal_time": {"applicability_start": "2023-08-04"},
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": "rule_10",
            "anchor_text": "Rule 10",
        },
        "payload": {},
        "validation": {"source_span_verified": True, "date_resolved": True},
        "evidence": {"source_span": {"start": 5, "text_hash": "pqr"}},
        "review": {"review_reasons": ["target_not_resolved"]},
    }
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(
        "\n".join(
            json.dumps(event)
            for event in [
                parent_event,
                child_event,
                covid_2020_event,
                opening_substitute_event,
                covid_2021_event,
                rule_138f_event,
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["applied_count"] == 6
    assert manifest["coverage_gap_count"] == 0
    rows = [json.loads(line) for line in (tmp_path / "out/node_versions.jsonl").read_text(encoding="utf-8").splitlines()]
    rule_rows = [row for row in rows if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/138e"]
    assert len(rule_rows) == 5
    assert "Restriction on furnishing" in rule_rows[-1]["text"]
    assert "any outward movement of goods" in rule_rows[-1]["text"]
    clause_rows = [
        row for row in rows
        if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/138e/subrule/(c)"
    ]
    assert len(clause_rows) == 1
    assert "outward supplies" in clause_rows[0]["text"]
    covid_2020_rows = [
        row for row in rows
        if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/138e/proviso/covid-2020"
    ]
    assert len(covid_2020_rows) == 1
    assert covid_2020_rows[0]["valid_from"] == "2020-03-20"
    assert "February, 2020 to August, 2020" in covid_2020_rows[0]["text"]
    assert "Provided also Provided also" not in covid_2020_rows[0]["text"]
    covid_2021_rows = [
        row for row in rows
        if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/138e/proviso/covid-2021"
    ]
    assert len(covid_2021_rows) == 1
    assert covid_2021_rows[0]["valid_from"] == "2021-05-01"
    assert "March, 2021 to May, 2021" in covid_2021_rows[0]["text"]
    assert "Provided also Provided also" not in covid_2021_rows[0]["text"]
    rule_138f_rows = [
        row for row in rows
        if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/138f"
    ]
    assert len(rule_138f_rows) == 1
    assert rule_138f_rows[0]["valid_from"] == "2023-08-04"
    assert "intra-State movement of gold" in rule_138f_rows[0]["text"]
    assert "not below rupees two lakhs" in rule_138f_rows[0]["text"]


def test_normalize_document_id_strips_tax_suffix():
    from src.legal_corpus.version_snapshots import _normalize_document_id

    assert _normalize_document_id("/in/union/notifications/cbic/central-tax/2017/45-2017-central-tax") == "/in/union/notifications/cbic/central-tax/2017/45-2017"
    assert _normalize_document_id("/in/union/notifications/cbic/central-tax/2017/45-2017") == "/in/union/notifications/cbic/central-tax/2017/45-2017"
    assert _normalize_document_id("/in/union/notifications/cbic/integrated-tax/2017/45-2017-integrated-tax") == "/in/union/notifications/cbic/integrated-tax/2017/45-2017"


def test_retryable_apply_error_includes_component_missing():
    from src.legal_corpus.version_snapshots import _retryable_apply_error

    assert _retryable_apply_error(ValueError("Target component missing: rule/44"))
    assert _retryable_apply_error(ValueError("Parent component missing: rule/44"))
    assert _retryable_apply_error(ValueError("Anchor component missing: rule/97"))
    assert not _retryable_apply_error(ValueError("Substitution text not found in rule/44"))
    assert not _retryable_apply_error(ValueError("Anchor not found: 'test' in node rule/12"))
    assert not _retryable_apply_error(ValueError("Inserted sibling already exists: rule/10a"))


def test_same_source_ordered_edits_allowed_with_normalized_doc_id():
    from src.legal_corpus.version_snapshots import _same_source_ordered_text_edits_allowed

    event_a = {
        "operation": "SPLICE",
        "source": {"document_id": "/in/union/notifications/cbic/central-tax/2017/45-2017"},
        "evidence": {"source_span": {"start": 100, "text_hash": "abc123"}},
        "payload": {"insert_text": "test"},
    }
    event_b = {
        "operation": "SUBSTITUTE",
        "source": {"document_id": "/in/union/notifications/cbic/central-tax/2017/45-2017-central-tax"},
        "evidence": {"source_span": {"start": 200, "text_hash": "def456"}},
        "payload": {"old_text": "a", "new_text": "b"},
    }
    assert _same_source_ordered_text_edits_allowed([event_a, event_b])


def test_parse_corrigendum_extracts_for_read_corrections():
    from src.legal_corpus.version_snapshots import _parse_corrigendum

    text = (
        'In the notification No. 16/2017-Central Tax, '
        'in line 6, for \u201cparagraph 5\u201d read \u201cparagraphs 3.20 and 3.21\u201d.'
    )
    result = _parse_corrigendum(text)
    assert result["refers_to_notifications"] == ["16/2017"]
    assert result["targets_rules"] is False
    assert len(result["corrections"]) == 1
    assert result["corrections"][0]["old_text"] == "paragraph 5"
    assert result["corrections"][0]["new_text"] == "paragraphs 3.20 and 3.21"


def test_parse_corrigendum_with_rule_reference():
    from src.legal_corpus.version_snapshots import _parse_corrigendum

    text = (
        'In rule 89, for \u201cthe Central Board of Excise and Customs\u201d '
        'read \u201cthe Government\u201d.'
    )
    result = _parse_corrigendum(text)
    assert result["targets_rules"] is True
    assert result["rule_references"] == ["89"]
    assert len(result["corrections"]) == 1


def test_corrigendum_application_patches_matching_event_payloads_with_provenance(tmp_path):
    from src.legal_corpus.corrigenda import apply_corrigenda

    events_path = tmp_path / "events.jsonl"
    corrected_event = {
        "event_id": "evt_target",
        "operation": "SUBSTITUTE",
        "status": "needs_review",
        "source": {
            "document_id": "/in/union/notifications/cbic/central-tax/2017/16-2017",
            "instrument_number": "16/2017-Central Tax",
            "publication_date": "2017-07-07",
            "record_id": "16",
        },
        "legal_time": {"applicability_start": "2017-07-07"},
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": "/in/union/rules/cgst-rules-2017/rule/89",
            "anchor_text": "paragraph 5",
        },
        "payload": {"old_text": "paragraph 5", "new_text": "paragraph 5 applies"},
        "evidence": {
            "excerpt": "for paragraph 5, substitute paragraph 5 applies",
            "source_span": {"start": 10, "end": 20, "text_hash": "target-hash"},
        },
    }
    corrigendum_event = {
        "event_id": "evt_corr",
        "operation": "CORRIGENDUM",
        "status": "needs_review",
        "source": {
            "document_id": "/in/union/notifications/cbic/central-tax/2017/corrigendum-16",
            "publication_date": "2017-09-01",
        },
        "legal_time": {"applicability_start": "2017-09-01"},
        "target": {"work_id": "/in/union/rules/cgst-rules-2017", "component_id": "/in/union/rules/cgst-rules-2017"},
        "payload": {
            "text": (
                "In the notification No. 16/2017-Central Tax, in rule 89, "
                "for \u201cparagraph 5\u201d read \u201cparagraphs 3.20 and 3.21\u201d."
            )
        },
        "evidence": {"source_span": {"start": 0, "end": 80, "text_hash": "corr-hash"}},
    }
    events_path.write_text(
        json.dumps(corrected_event, sort_keys=True) + "\n" + json.dumps(corrigendum_event, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    result = apply_corrigenda(
        events_path=events_path,
        output=tmp_path / "events_corrected.jsonl",
        report_output=tmp_path / "corrigendum_application_report.json",
    )

    rows = [json.loads(line) for line in (tmp_path / "events_corrected.jsonl").read_text().splitlines()]
    patched = rows[0]
    assert result["applied_event_count"] == 1
    assert patched["payload"]["old_text"] == "paragraphs 3.20 and 3.21"
    assert patched["target"]["anchor_text"] == "paragraphs 3.20 and 3.21"
    application = patched["payload"]["corrigendum_applications"][0]
    assert application["corrigendum_event_id"] == "evt_corr"
    assert application["corrigendum_source_span_hash"] == "corr-hash"
    report = json.loads((tmp_path / "corrigendum_application_report.json").read_text(encoding="utf-8"))
    assert report["applications"][0]["original_event_id"] == "evt_target"
    assert report["applications"][0]["original_source_span_hash"] == "target-hash"


def test_parse_rescind_extracts_notification_number():
    from src.legal_corpus.version_snapshots import _parse_rescind

    text = (
        'hereby rescinds the notification of the Government of India '
        'in the Ministry of Finance, Department of Revenue '
        'No. 6/2018-Central Tax, dated the 25th January, 2018.'
    )
    result = _parse_rescind(text)
    assert "6/2018" in result["rescinds_notifications"]


def test_doc_id_matches_rescinded():
    from src.legal_corpus.version_snapshots import _doc_id_matches_rescinded

    assert _doc_id_matches_rescinded(
        "/in/union/notifications/cbic/central-tax/2018/6-2018", {"6/2018"}
    )
    assert _doc_id_matches_rescinded("6/2018-Central Tax", {"6/2018"})
    assert not _doc_id_matches_rescinded(
        "/in/union/notifications/cbic/central-tax/2018/13-2018", {"6/2018"}
    )
    assert not _doc_id_matches_rescinded(
        "/in/union/notifications/cbic/central-tax/2018/26-2018", {"6/2018"}
    )
    assert not _doc_id_matches_rescinded("26/2018-Central Tax", {"6/2018"})


def test_preprocess_special_ops_flags_rescinded_events():
    from src.legal_corpus.version_snapshots import _preprocess_special_ops

    events = [
        {
            "event_id": "evt_rescind_1",
            "operation": "RESCIND",
            "payload": {"text": "rescinds notification No. 6/2018-Central Tax"},
            "source": {"document_id": "/in/union/notifications/cbic/central-tax/2018/13-2018"},
        },
        {
            "event_id": "evt_amend_1",
            "operation": "SUBSTITUTE",
            "source": {"document_id": "/in/union/notifications/cbic/central-tax/2018/6-2018"},
        },
        {
            "event_id": "evt_amend_2",
            "operation": "INSERT_SIBLING",
            "source": {"document_id": "/in/union/notifications/cbic/central-tax/2019/14-2019"},
        },
    ]
    enriched, meta = _preprocess_special_ops(events)
    assert "6/2018" in meta["rescinded_notifications"]
    assert "evt_amend_1" in meta["rescinded_event_ids"]
    assert "evt_amend_2" not in meta["rescinded_event_ids"]


def test_special_ops_constant_defined():
    from src.legal_corpus.version_snapshots import SPECIAL_OPS

    assert "CORRIGENDUM" in SPECIAL_OPS
    assert "RESCIND" in SPECIAL_OPS
    assert "COMMENCE" in SPECIAL_OPS


def test_is_retry_eligible_allows_anchor_not_resolved():
    from src.legal_corpus.version_snapshots import _is_retry_eligible

    event = {
        "status": "needs_review",
        "operation": "SUBSTITUTE",
        "target": {
            "component_id": "/in/union/rules/cgst-rules-2017/rule/89",
            "work_id": "/in/union/rules/cgst-rules-2017",
        },
        "source": {"publication_date": "2019-01-01"},
        "payload": {"old_text": "some old text", "new_text": "new text"},
        "review": {"review_reasons": ["anchor_not_resolved", "llm_candidate_not_validated"]},
    }
    assert _is_retry_eligible(event)


def test_is_retry_eligible_rejects_unknown_op():
    from src.legal_corpus.version_snapshots import _is_retry_eligible

    event = {
        "status": "needs_review",
        "operation": "UNKNOWN",
        "target": {
            "component_id": "/in/union/rules/cgst-rules-2017/rule/89",
            "work_id": "/in/union/rules/cgst-rules-2017",
        },
        "source": {"publication_date": "2019-01-01"},
        "payload": {"text": "some text"},
        "review": {"review_reasons": ["anchor_not_resolved"]},
    }
    assert not _is_retry_eligible(event)


def test_is_retry_eligible_rejects_hard_blockers():
    from src.legal_corpus.version_snapshots import _is_retry_eligible

    event = {
        "status": "needs_review",
        "operation": "SUBSTITUTE",
        "target": {
            "component_id": "/in/union/rules/cgst-rules-2017/rule/10a/subrule/1",
            "work_id": "/in/union/rules/cgst-rules-2017",
        },
        "source": {"publication_date": "2019-01-01"},
        "payload": {"old_text": "x", "new_text": "y"},
        "review": {"review_reasons": ["document_scope_target_not_materializable"]},
    }
    assert not _is_retry_eligible(event)


def test_compound_block_with_payload_is_retry_eligible():
    from src.legal_corpus.version_snapshots import _is_retry_eligible

    event = {
        "status": "needs_review",
        "operation": "SUBSTITUTE",
        "target": {
            "component_id": "/in/union/rules/cgst-rules-2017/rule/10a/subrule/1",
            "work_id": "/in/union/rules/cgst-rules-2017",
        },
        "source": {"publication_date": "2019-01-01"},
        "payload": {"old_text": "x", "new_text": "y"},
        "review": {"review_reasons": ["compound_block_contains_multiple_amendments"]},
    }
    assert _is_retry_eligible(event)


def test_is_retry_eligible_rejects_work_level_target():
    from src.legal_corpus.version_snapshots import _is_retry_eligible

    event = {
        "status": "needs_review",
        "operation": "SUBSTITUTE",
        "target": {
            "component_id": "/in/union/rules/cgst-rules-2017",
            "work_id": "/in/union/rules/cgst-rules-2017",
        },
        "source": {"publication_date": "2019-01-01"},
        "payload": {"old_text": "x", "new_text": "y"},
        "review": {"review_reasons": ["anchor_not_resolved"]},
    }
    assert not _is_retry_eligible(event)


def test_reclassify_unknown_operation():
    from src.legal_corpus.version_snapshots import _reclassify_unknown_operation

    assert _reclassify_unknown_operation({
        "operation": "UNKNOWN",
        "evidence": {"excerpt": "shall be substituted"},
        "payload": {},
    }) == "SUBSTITUTE"

    assert _reclassify_unknown_operation({
        "operation": "UNKNOWN",
        "evidence": {"excerpt": "shall be inserted"},
        "payload": {},
    }) == "INSERT_CHILD"

    assert _reclassify_unknown_operation({
        "operation": "UNKNOWN",
        "evidence": {"excerpt": "shall be omitted"},
        "payload": {},
    }) == "OMIT"

    assert _reclassify_unknown_operation({
        "operation": "UNKNOWN",
        "evidence": {"excerpt": "no amendment language here"},
        "payload": {},
    }) == "UNKNOWN"


def test_extract_payload_from_excerpt_substitute():
    from src.legal_corpus.version_snapshots import _extract_payload_from_excerpt

    event = {
        "operation": "SPLICE",
        "payload": {},
        "evidence": {
            "excerpt": 'for the words \u201ctwo and a half lakh rupees\u201d wherever they occur, the words \u201cone lakh rupees\u201d shall be substituted;'
        },
    }
    result = _extract_payload_from_excerpt(event)
    assert result is True
    assert event["operation"] == "SUBSTITUTE"
    assert event["payload"]["old_text"] == "two and a half lakh rupees"
    assert event["payload"]["new_text"] == "one lakh rupees"
    assert event["payload"].get("replace_all") is True


def test_context_recovery_removes_document_scope_blocker():
    from src.legal_corpus.version_snapshots import _is_retry_eligible

    event = {
        "status": "needs_review",
        "operation": "SUBSTITUTE",
        "target": {
            "component_id": "/in/union/rules/cgst-rules-2017/rule/21",
            "work_id": "/in/union/rules/cgst-rules-2017",
        },
        "source": {"publication_date": "2019-01-01"},
        "payload": {"old_text": "x", "new_text": "y", "context_recovered_target": True},
        "review": {"review_reasons": ["document_scope_target_not_materializable"]},
    }
    assert _is_retry_eligible(event)


def test_inserted_already_exists_not_hard_blocker():
    from src.legal_corpus.version_snapshots import _is_retry_eligible

    event = {
        "status": "needs_review",
        "operation": "INSERT_CHILD",
        "target": {
            "component_id": "/in/union/rules/cgst-rules-2017/rule/10a",
            "work_id": "/in/union/rules/cgst-rules-2017",
        },
        "source": {"publication_date": "2019-01-01"},
        "payload": {"content": "text", "label": "1"},
        "review": {"review_reasons": ["inserted_component_already_exists"]},
    }
    assert _is_retry_eligible(event)


def test_replace_unique_text_all_occurrences():
    from src.legal_corpus.version_snapshots import _replace_unique_text

    original = "goods and goods and goods"
    event = {"payload": {}, "evidence": {"excerpt": "at all places where they occur"}}
    result = _replace_unique_text(original, "goods", "items", event)
    assert result == "items and items and items"


def test_replace_unique_text_payload_replace_all():
    from src.legal_corpus.version_snapshots import _replace_unique_text

    original = "goods and goods and goods"
    event = {"payload": {"replace_all": True}, "evidence": {"excerpt": ""}}
    result = _replace_unique_text(original, "goods", "items", event)
    assert result == "items and items and items"


def test_normalized_text_match_helper():
    from src.legal_corpus.version_snapshots import (
        _normalized_text_match,
        _normalized_find_spans,
        _replace_unique_text,
        _omit_text_from_paragraph,
    )

    # Exact match is preferred and reports normalized=False.
    assert _normalized_text_match("hello world", "world") == (True, 6, False)

    # Smart quotes / em-dash recovered via normalization.
    assert _normalized_text_match("the \u201cgoods\u201d are \u2014 here", 'the "goods" are - here') == (True, 0, True)

    # \u2016 / \u2015 (Indian legal quote marks) folded to ASCII quotes.
    assert _normalized_text_match("a \u2016term\u2015 b", 'a "term" b')[0] is True

    # Footnote leading-number markers stripped (inner content kept).
    spans = _normalized_find_spans("credit 1 [of eligible duties] carried", "credit of eligible duties carried")
    assert len(spans) == 1 and spans[0][0] == 0

    # Bracketed footnote marker [4] stripped.
    assert _normalized_text_match("see rule [4] applies", "see rule applies")[0] is True

    # No false positives.
    assert _normalized_text_match("completely different", "tax invoice") == (False, -1, False)


def test_replace_and_omit_use_normalization_fallback():
    from src.legal_corpus.version_snapshots import _replace_unique_text, _omit_text_from_paragraph

    ev = {"payload": {}}
    res = _replace_unique_text("the \u201ctax\u201d invoice \u2013 due", 'the "tax" invoice - due', "X", ev)
    assert res == "X"
    assert ev["_normalization_provenance"] and ev["_normalization_provenance"][0]["steps"]

    ev2 = {"payload": {}}
    res2 = _omit_text_from_paragraph("keep this \u2013 part", "this - part", ev2)
    assert res2 == "keep "
    assert ev2["_normalization_provenance"]


def test_extract_form_amendments_finds_substitute():
    from src.legal_corpus.form_version_snapshots import _extract_form_amendments

    events = [
        {
            "event_id": "evt_test_1",
            "evidence": {"excerpt": 'for FORM GST REG-01, the following form shall be substituted, namely:- "FORM GST REG-01..."'},
            "source": {"publication_date": "2019-01-01"},
        }
    ]
    amendments = _extract_form_amendments(events)
    assert len(amendments) == 1
    assert amendments[0]["operation"] == "SUBSTITUTE"
    assert amendments[0]["form_slug"] == "gst-reg-01"
    assert amendments[0]["date"] == "2019-01-01"


def test_extract_form_amendments_multi_form():
    from src.legal_corpus.form_version_snapshots import _extract_form_amendments

    events = [
        {
            "event_id": "evt_test_multi",
            "evidence": {
                "excerpt": (
                    "for FORM GST EWB-01 and FORM GST EWB-02, "
                    "the following forms shall be substituted, namely:- ..."
                )
            },
            "source": {"publication_date": "2018-01-23"},
        }
    ]
    amendments = _extract_form_amendments(events)
    slugs = {a["form_slug"] for a in amendments}
    assert "gst-ewb-01" in slugs
    assert "gst-ewb-02" in slugs


def test_extract_form_amendments_finds_insert():
    from src.legal_corpus.form_version_snapshots import _extract_form_amendments

    events = [
        {
            "event_id": "evt_test_insert",
            "evidence": {"excerpt": "after FORM GST INV-1, the following FORM shall be inserted"},
            "source": {"publication_date": "2020-06-01"},
        }
    ]
    amendments = _extract_form_amendments(events)
    assert len(amendments) == 1
    assert amendments[0]["operation"] == "INSERT"
    assert amendments[0]["form_slug"] == "gst-inv-1"


def test_extract_form_amendments_dedup():
    from src.legal_corpus.form_version_snapshots import _extract_form_amendments

    events = [
        {
            "event_id": "evt_dup",
            "evidence": {"excerpt": "for FORM GST RFD-01, the following form shall be substituted"},
            "source": {"publication_date": "2019-01-01"},
        }
    ]
    amendments = _extract_form_amendments(events * 3)
    assert len(amendments) == 1


def test_portal_completeness_reports_missing_rule_89_notification(tmp_path):
    from src.legal_corpus.portal_completeness import build_portal_completeness_report

    html_dir = tmp_path / "html"
    html_dir.mkdir()
    (html_dir / "tax_repository_gst_rules_cgst_rules_active_chapter10_rule89_v1.00.html").write_text(
        "<html><body>Rule 89 amended by Notification No. 16/2020-Central Tax.</body></html>",
        encoding="utf-8",
    )
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(
        json.dumps(
            {
                "event_id": "evt_rule_89_other",
                "target": {"component_id": "/in/union/rules/cgst-rules-2017/rule/89"},
                "source": {"document_id": "/in/union/notifications/cbic/central-tax/2020/15-2020"},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    report = build_portal_completeness_report(
        html_dir=html_dir,
        events_path=events_path,
        output=tmp_path / "portal.json",
        top_rules=["89"],
    )

    assert report["missing_source_notification_count"] == 1
    missing = report["rules"]["89"]["missing_source_notifications"][0]
    assert missing["classification"] == "missing_source_notification"
    assert missing["notification_ref"] == "16/2020"


def test_portal_completeness_excludes_rate_and_customs_references(tmp_path):
    from src.legal_corpus.portal_completeness import build_portal_completeness_report

    html_dir = tmp_path / "html"
    html_dir.mkdir()
    (html_dir / "tax_repository_gst_rules_cgst_rules_active_chapter10_rule89_v1.00.html").write_text(
        (
            "<html><body>"
            "Rule source vide Notification No. 20/2024-CT dated 08.10.2024. "
            "External rate reference notification No. 40/2017-Central Tax (Rate). "
            "External customs reference notification No. 79/2017-Customs. "
            "Supplier has availed the benefit of notification No. 48/2017-Central Tax."
            "</body></html>"
        ),
        encoding="utf-8",
    )
    events_path = tmp_path / "events.jsonl"
    events_path.write_text("", encoding="utf-8")

    report = build_portal_completeness_report(
        html_dir=html_dir,
        events_path=events_path,
        top_rules=["89"],
    )

    refs = {row["notification_ref"]: row["notification_class"] for row in report["rules"]["89"]["portal_notification_refs"]}
    assert refs["20/2024"] == "rules_source_notification"
    assert refs["40/2017"] == "external_reference_notification"
    assert refs["48/2017"] == "external_reference_notification"
    assert refs["79/2017"] == "external_reference_notification"
    assert [row["notification_ref"] for row in report["rules"]["89"]["missing_source_notifications"]] == ["20/2024"]
    assert report["external_reference_notification_count"] == 3


def test_portal_completeness_excludes_date_basis_and_contextual_references(tmp_path):
    from src.legal_corpus.portal_completeness import build_portal_completeness_report

    html_dir = tmp_path / "html"
    html_dir.mkdir()
    (html_dir / "tax_repository_gst_rules_cgst_rules_active_chapter3_rule9_v1.00.html").write_text(
        (
            "<html><body>"
            "Actual source vide Notification No. 94/2020-CT dated 22.12.2020. "
            "Brought into force w.e.f. 15.04.2021 by Notification No. 14/2021-CT dated 01.05.2021. "
            "Kindly also refer to Notification No. 24/2021-CT dated 01.06.2021. "
            "Inserted vide Notification No. 12/2024-CT dated 10.07.2024, "
            "appointed vide Notification No. 09/2025-CT dated 11.02.2025. "
            "NACIN means as notified by notification No. 24/2018-Central Tax."
            "</body></html>"
        ),
        encoding="utf-8",
    )
    events_path = tmp_path / "events.jsonl"
    events_path.write_text("", encoding="utf-8")

    report = build_portal_completeness_report(
        html_dir=html_dir,
        events_path=events_path,
        top_rules=["9"],
    )

    refs = {row["notification_ref"]: row["notification_class"] for row in report["rules"]["9"]["portal_notification_refs"]}
    assert refs["94/2020"] == "rules_source_notification"
    assert refs["14/2021"] == "date_basis_notification"
    assert refs["24/2021"] == "contextual_reference_notification"
    assert refs["9/2025"] == "date_basis_notification"
    assert refs["24/2018"] == "contextual_reference_notification"
    assert [row["notification_ref"] for row in report["rules"]["9"]["missing_source_notifications"]] == [
        "12/2024",
        "94/2020",
    ]


def test_portal_completeness_distinguishes_present_but_unlinked_sources(tmp_path):
    from src.legal_corpus.portal_completeness import build_portal_completeness_report

    html_dir = tmp_path / "html"
    html_dir.mkdir()
    (html_dir / "tax_repository_gst_rules_cgst_rules_active_chapter10_rule89_v1.00.html").write_text(
        (
            "<html><body>"
            "Rule 89 source vide Notification No. 20/2024-CT dated 08.10.2024. "
            "Brought into force vide Notification No. 38/2021-C.T., dated 21.12.2021. "
            "Missing source vide Notification No. 99/2024-CT."
            "</body></html>"
        ),
        encoding="utf-8",
    )
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "event_id": "evt_present_elsewhere",
                        "source": {"instrument_number": "20/2024-Central Tax"},
                        "legal_time": {},
                        "target": {"component_id": "/in/union/rules/cgst-rules-2017/rule/142"},
                    }
                ),
                json.dumps(
                    {
                        "event_id": "evt_commenced",
                        "source": {"instrument_number": "35/2021-Central Tax"},
                        "legal_time": {"date_basis": "commencement_notification_38_2021_rule_2_subrule_2"},
                        "target": {"component_id": "/in/union/rules/cgst-rules-2017/rule/89"},
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    report = build_portal_completeness_report(
        html_dir=html_dir,
        events_path=events_path,
        top_rules=["89"],
    )

    rule89 = report["rules"]["89"]
    by_ref = {row["notification_ref"]: row["classification"] for row in rule89["missing_source_notifications"]}
    assert by_ref == {
        "20/2024": "source_present_unlinked_notification",
        "99/2024": "missing_source_notification",
    }
    assert "38/2021" in rule89["event_notification_refs"]
    assert report["missing_source_notification_count"] == 1
    assert report["source_present_unlinked_notification_count"] == 1


def test_portal_completeness_corrects_portal_notification_year_typo_when_event_ref_exists(tmp_path):
    from src.legal_corpus.portal_completeness import build_portal_completeness_report

    html_dir = tmp_path / "html"
    html_dir.mkdir()
    (html_dir / "tax_repository_gst_rules_cgst_rules_active_chapter4_rule28_v1.00.html").write_text(
        (
            "<html><body>"
            "Inserted vide Notification No. 52/2021 - CT dated 26.10.2023. "
            "Inserted vide Notification No. 99/2021 - CT dated 26.10.2023."
            "</body></html>"
        ),
        encoding="utf-8",
    )
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(
        json.dumps(
            {
                "event_id": "evt_rule_28_52_2023",
                "target": {"component_id": "/in/union/rules/cgst-rules-2017/rule/28/subrule/2"},
                "source": {"instrument_number": "52/2023-Central Tax"},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    report = build_portal_completeness_report(
        html_dir=html_dir,
        events_path=events_path,
        top_rules=["28"],
    )

    missing = report["rules"]["28"]["missing_source_notifications"]
    assert [row["notification_ref"] for row in missing] == ["99/2021"]
    assert report["rules"]["28"]["portal_notification_refs"][0]["notification_ref"] == "52/2023"
    assert report["rules"]["28"]["portal_notification_refs"][0]["portal_notification_ref"] == "52/2021"


def test_top10_portal_annotation_replaces_stale_portal_rows(tmp_path):
    from src.legal_corpus.portal_completeness import annotate_top10_gap_report

    top10 = tmp_path / "top10.json"
    top10.write_text(
        json.dumps(
            {
                "rules": {
                    "rule/89": {
                        "gap_count": 2,
                        "lane_counts": {"portal_completeness": 1, "target_creation": 1},
                        "gaps": [
                            {"event_id": "portal_missing::40/2017", "lane": "portal_completeness"},
                            {"event_id": "evt_real", "lane": "target_creation"},
                        ],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    portal = tmp_path / "portal.json"
    portal.write_text(
        json.dumps(
            {
                "missing_source_notifications": [
                    {
                        "rule": "89",
                        "component_id": "/in/union/rules/cgst-rules-2017/rule/89",
                        "notification_ref": "20/2024",
                        "excerpt": "Rule source",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    result = annotate_top10_gap_report(top10_path=top10, portal_report_path=portal)

    assert result["added"] == 1
    updated = json.loads(top10.read_text(encoding="utf-8"))
    row = updated["rules"]["rule/89"]
    assert row["gap_count"] == 2
    assert row["lane_counts"]["portal_completeness"] == 1
    assert {gap["event_id"] for gap in row["gaps"]} == {"evt_real", "portal_missing::20/2024"}


def test_top10_gap_report_rebuild_uses_current_coverage_gaps(tmp_path):
    from src.legal_corpus.portal_completeness import rebuild_top10_gap_report

    coverage = tmp_path / "coverage_gaps.json"
    coverage.write_text(
        json.dumps(
            {
                "gap_count": 3,
                "gaps": [
                    {
                        "event_id": "evt_anchor",
                        "operation": "SUBSTITUTE",
                        "target": {"component_id": "/in/union/rules/cgst-rules-2017/rule/89"},
                        "skip_reason": "apply_failed: Anchor not found: Statement 1A",
                    },
                    {
                        "event_id": "evt_target",
                        "operation": "SPLICE",
                        "target": {"component_id": "/in/union/rules/cgst-rules-2017/rule/142/subrule/1a"},
                        "skip_reason": "target component missing",
                    },
                    {
                        "event_id": "evt_unreviewed",
                        "operation": "UNKNOWN",
                        "target": {"component_id": "/in/union/rules/cgst-rules-2017/rule/142"},
                        "skip_reason": "event_status_not_validated",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    result = rebuild_top10_gap_report(
        coverage_gaps_path=coverage,
        output=tmp_path / "top10_gap_report.json",
        top_n=2,
    )

    assert result["total_gap_count"] == 3
    report = json.loads((tmp_path / "top10_gap_report.json").read_text(encoding="utf-8"))
    assert report["target_rules"] == ["rule/142", "rule/89"]
    assert report["rules"]["rule/142"]["lane_counts"] == {"event_resolution": 1, "target_creation": 1}
    assert report["rules"]["rule/89"]["lane_counts"] == {"anchor_normalization": 1}


def test_rfd01_statement_materializer_creates_first_class_statement_versions(tmp_path):
    from src.legal_corpus.form_version_snapshots import materialize_form_versions

    events_path = tmp_path / "events.jsonl"
    events_path.write_text(
        json.dumps(
            {
                "event_id": "evt_stmt_1a",
                "operation": "SUBSTITUTE",
                "status": "needs_review",
                "source": {
                    "document_id": "/in/union/notifications/cbic/central-tax/2018/75-2018",
                    "record_id": "75",
                    "publication_date": "2018-12-31",
                },
                "legal_time": {"applicability_start": "2018-12-31"},
                "target": {
                    "work_id": "/in/union/rules/cgst-rules-2017",
                    "component_id": "/in/union/rules/cgst-rules-2017/rule/89",
                },
                "payload": {"old_text": "Statement 1A", "new_text": "Statement 1A refund turnover details"},
                "evidence": {"excerpt": "In FORM GST RFD-01, Statement 1A shall be substituted."},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    manifest = materialize_form_versions(
        events_path=events_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "forms",
    )

    assert manifest["statement_applied_count"] == 1
    rows = [json.loads(line) for line in (tmp_path / "forms/node_versions.jsonl").read_text().splitlines()]
    statement_rows = [row for row in rows if row["component_id"] == "/in/union/forms/gst-rfd-01/statement/1a"]
    assert len(statement_rows) == 2
    assert statement_rows[-1]["created_by_event_id"] == "evt_stmt_1a"
    assert statement_rows[-1]["source_basis"]["operation"] == "STATEMENT_SUBSTITUTE"
    assert "refund turnover details" in statement_rows[-1]["text"]


def test_form_registry_routes_pending_baseline_forms_without_blocking_rfd01_statements(tmp_path):
    from src.legal_corpus.form_version_snapshots import materialize_form_versions

    registry_path = tmp_path / "form_registry.json"
    registry_path.write_text(
        json.dumps(
            {
                "version": "form-registry-test",
                "default_lane": "forms_lane_pending_baseline",
                "forms": [
                    {"form_id": "gst-rfd-01", "priority": 1, "baseline_status": "pending_structured_baseline"},
                    {"form_id": "gst-drc-03", "priority": 2, "baseline_status": "pending_structured_baseline"},
                    {"form_id": "gstr-1", "priority": 3, "baseline_status": "pending_structured_baseline"},
                ],
            }
        ),
        encoding="utf-8",
    )
    events = [
        {
            "event_id": "evt_rfd01_form",
            "operation": "SUBSTITUTE",
            "status": "needs_review",
            "source": {"publication_date": "2018-12-31"},
            "legal_time": {"applicability_start": "2018-12-31"},
            "target": {
                "work_id": "/in/union/rules/cgst-rules-2017",
                "component_id": "/in/union/rules/cgst-rules-2017/rule/89",
            },
            "payload": {},
            "evidence": {"excerpt": "for FORM GST RFD-01, the following form shall be substituted"},
        },
        {
            "event_id": "evt_stmt_1a",
            "operation": "SUBSTITUTE",
            "status": "needs_review",
            "source": {"publication_date": "2018-12-31"},
            "legal_time": {"applicability_start": "2018-12-31"},
            "target": {
                "work_id": "/in/union/rules/cgst-rules-2017",
                "component_id": "/in/union/rules/cgst-rules-2017/rule/89",
            },
            "payload": {"new_text": "Statement 1A refund turnover details"},
            "evidence": {"excerpt": "In FORM GST RFD-01, Statement 1A shall be substituted."},
        },
        {
            "event_id": "evt_gstr1",
            "operation": "UNKNOWN",
            "status": "needs_review",
            "source": {"publication_date": "2020-01-01"},
            "legal_time": {"applicability_start": "2020-01-01"},
            "target": {
                "work_id": "/in/union/rules/cgst-rules-2017",
                "component_id": "/in/union/rules/cgst-rules-2017/rule/46",
            },
            "payload": {"triage_lane": "forms_lane_pending_baseline", "forms_lane_pending_baseline": True},
            "evidence": {"excerpt": "In FORM GSTR-1, in the Instructions, after serial number 17"},
        },
        {
            "event_id": "evt_standalone_gstr11",
            "operation": "UNKNOWN",
            "status": "needs_review",
            "source": {"publication_date": "2020-01-01"},
            "legal_time": {"applicability_start": "2020-01-01"},
            "target": {
                "work_id": "/in/union/rules/cgst-rules-2017",
                "component_id": "/in/union/rules/cgst-rules-2017/rule/95",
            },
            "payload": {"triage_lane": "forms_lane_pending_baseline", "forms_lane_pending_baseline": True},
            "evidence": {"excerpt": "Table No. 6 will be auto-populated from details furnished in table 3 of GSTR-11."},
        },
        {
            "event_id": "evt_gst_reg16",
            "operation": "UNKNOWN",
            "status": "needs_review",
            "source": {"publication_date": "2020-01-01"},
            "legal_time": {"applicability_start": "2020-01-01"},
            "target": {
                "work_id": "/in/union/rules/cgst-rules-2017",
                "component_id": "/in/union/rules/cgst-rules-2017/rule/81",
            },
            "payload": {"triage_lane": "forms_lane_pending_baseline", "forms_lane_pending_baseline": True},
            "evidence": {"excerpt": "Amount of tax paid along with application for cancellation of registration (GST REG-16)."},
        },
        {
            "event_id": "evt_overrouted_rule_text",
            "operation": "UNKNOWN",
            "status": "needs_review",
            "source": {"publication_date": "2020-01-01"},
            "legal_time": {"applicability_start": "2020-01-01"},
            "target": {
                "work_id": "/in/union/rules/cgst-rules-2017",
                "component_id": "/in/union/rules/cgst-rules-2017/rule/89/subrule/5",
            },
            "payload": {"triage_lane": "forms_lane_pending_baseline", "forms_lane_pending_baseline": True},
            "evidence": {"excerpt": "in rule 89, for sub-rule (5), the following refund formula shall be substituted"},
        },
    ]
    events_path = tmp_path / "events.jsonl"
    events_path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")

    manifest = materialize_form_versions(
        events_path=events_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "forms",
        form_registry_path=registry_path,
    )

    assert manifest["statement_applied_count"] == 1
    assert manifest["forms_lane_pending_baseline_count"] == 5
    assert {event["event_id"] for event in manifest["forms_lane_pending_baseline_events"]} == {
        "evt_rfd01_form",
        "evt_gstr1",
        "evt_standalone_gstr11",
        "evt_gst_reg16",
        "evt_overrouted_rule_text",
    }
    pending_by_form = manifest["forms_lane_pending_baseline_by_form"]
    assert pending_by_form["gst-rfd-01"]["priority"] == 1
    assert pending_by_form["gst-rfd-01"]["count"] == 1
    assert pending_by_form["gstr-1"]["priority"] == 3
    assert pending_by_form["gstr-1"]["sample_event_ids"] == ["evt_gstr1"]
    assert pending_by_form["gstr-11"]["sample_event_ids"] == ["evt_standalone_gstr11"]
    assert pending_by_form["gst-reg-16"]["sample_event_ids"] == ["evt_gst_reg16"]
    assert set(manifest["forms_lane_pending_baseline_top_priority"]) == {"gst-rfd-01", "gstr-1"}
    assert manifest["forms_lane_pending_baseline_registered_count"] == 2
    assert manifest["forms_lane_pending_baseline_unregistered_count"] == 2
    assert manifest["forms_lane_pending_baseline_unclassified_count"] == 0
    assert manifest["forms_lane_pending_baseline_unclassified_event_ids"] == []
    assert manifest["forms_lane_pending_baseline_non_overroute_unclassified_event_ids"] == []
    assert manifest["forms_lane_pending_baseline_unclassified_by_bucket"]["form_rules_overroute_classified"]["count"] == 1
    assert manifest["forms_lane_overrouted_count"] == 1
    assert manifest["forms_lane_overrouted_event_ids"] == ["evt_overrouted_rule_text"]
    assert manifest["form_rules_overroute_classified_count"] == 1
    assert manifest["form_rules_overroute_classified_event_ids"] == ["evt_overrouted_rule_text"]
    assert manifest["forms_lane_true_pending_baseline_count"] == 0
    assert manifest["coverage_gap_count"] == 0
    coverage = json.loads((tmp_path / "forms/coverage_gaps.json").read_text(encoding="utf-8"))
    assert coverage["gap_count"] == 0
    assert len(coverage["forms_lane_pending_baseline_events"]) == 5
    rows = [json.loads(line) for line in (tmp_path / "forms/node_versions.jsonl").read_text().splitlines()]
    assert any(row["component_id"] == "/in/union/forms/gst-rfd-01/statement/1a" for row in rows)


def test_rule_materializer_routes_rfd01_statement_events_out_of_rules_gaps(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso><act><body><article refersTo="/in/union/rules/cgst-rules-2017/rule/89">
<num>89</num><heading>Application for refund</heading><content><p>89. Refund rule text.</p></content>
</article></body></act></akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-06-19"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(
        json.dumps(
            {
                "event_id": "evt_stmt_gap",
                "operation": "UNKNOWN",
                "status": "needs_review",
                "source": {"publication_date": "2018-01-01"},
                "legal_time": {"applicability_start": "2018-01-01"},
                "target": {
                    "work_id": "/in/union/rules/cgst-rules-2017",
                    "component_id": "/in/union/rules/cgst-rules-2017/rule/89",
                },
                "payload": {"old_text": "Statement 1A"},
                "evidence": {"excerpt": "In FORM GST RFD-01, Statement 1A shall be substituted."},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["coverage_gap_count"] == 0
    assert manifest["forms_lane_routed_count"] == 1


def test_phase3_backlog_consolidates_next_lanes(tmp_path):
    from src.legal_corpus.phase3_backlog import build_phase3_backlog

    rules_coverage = tmp_path / "rules_coverage.json"
    rules_coverage.write_text(
        json.dumps(
            {
                "coverage_gap_count": 2,
                "gaps": [
                    {"event_id": "evt_rule_1", "skip_reason": "apply_failed"},
                    {"event_id": "evt_rule_2", "skip_reason": "event_status_not_validated"},
                ],
            }
        ),
        encoding="utf-8",
    )
    forms_manifest = tmp_path / "forms_manifest.json"
    forms_manifest.write_text(
        json.dumps(
            {
                "forms_lane_pending_baseline_count": 4,
                "forms_lane_true_pending_baseline_count": 3,
                "forms_lane_overrouted_count": 1,
                "forms_lane_overrouted_event_ids": ["evt_overrouted"],
                "forms_lane_pending_baseline_unclassified_count": 2,
                "forms_lane_pending_baseline_unclassified_event_ids": ["evt_unknown_form", "evt_overrouted"],
                "forms_lane_pending_baseline_non_overroute_unclassified_event_ids": ["evt_unknown_form"],
                "forms_lane_pending_baseline_unclassified_by_bucket": {
                    "form_slug_unresolved": {"count": 1, "sample_event_ids": ["evt_unknown_form"]},
                    "suspected_rule_text_overrouted": {"count": 1, "sample_event_ids": ["evt_overrouted"]},
                },
                "forms_lane_pending_baseline_top_priority": {
                    "gst-rfd-01": {
                        "count": 2,
                        "priority": 1,
                        "baseline_status": "pending_structured_baseline",
                        "sample_event_ids": ["evt_rfd_1", "evt_rfd_2"],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    portal = tmp_path / "portal.json"
    portal.write_text(
        json.dumps(
            {
                "missing_source_notification_count": 0,
                "source_present_unlinked_notification_count": 4,
            }
        ),
        encoding="utf-8",
    )
    confidence = tmp_path / "confidence.json"
    confidence.write_text(
        json.dumps(
            {
                "tier_counts": {"A": 1, "D": 2},
                "component_details": {
                    "/in/union/rules/cgst-rules-2017/rule/40": {
                        "tier": "C",
                        "reconciliation_outcome": "true_substantive_mismatch",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    reconciliation_report = tmp_path / "reconciliation_report.json"
    reconciliation_report.write_text(
        json.dumps(
            {
                "unresolved_reconciliation_audit": {
                    "audit_class_counts": {"compound_split_needed": 1},
                    "rows": [
                        {
                            "component_id": "/in/union/rules/cgst-rules-2017/rule/40",
                            "audit_class": "compound_split_needed",
                            "candidate_event_count": 2,
                            "candidate_event_ids": ["evt_rule_40_a", "evt_rule_40_b"],
                        }
                    ],
                }
            }
        ),
        encoding="utf-8",
    )
    act_audit = tmp_path / "act_audit.json"
    act_audit.write_text(
        json.dumps({"summary": {"coverage_gap_count": 7, "confidence_tier_counts": {"C": 2, "D": 5}}}),
        encoding="utf-8",
    )

    backlog = build_phase3_backlog(
        rules_coverage_path=rules_coverage,
        forms_manifest_path=forms_manifest,
        portal_completeness_path=portal,
        confidence_tiers_path=confidence,
        act_audit_path=act_audit,
        reconciliation_report_path=reconciliation_report,
        output_path=tmp_path / "phase3_backlog.json",
        sample_limit=1,
    )

    item_ids = {item["id"] for item in backlog["items"]}
    assert {
        "rules-explicit-gaps",
        "portal-source-present-unlinked",
        "forms-baseline-gst-rfd-01",
        "forms-unclassified-pending-baseline",
        "act-pipeline-backlog",
        "future-compiler-context-inheritance",
    } <= item_ids
    assert backlog["summary"]["rules_coverage_gap_count"] == 2
    assert backlog["summary"]["forms_pending_baseline_count"] == 4
    assert backlog["summary"]["forms_true_pending_baseline_count"] == 3
    assert backlog["summary"]["forms_lane_overrouted_count"] == 1
    assert backlog["summary"]["confidence_tier_counts"] == {"A": 1, "D": 2}
    assert backlog["summary"]["unresolved_reconciliation_count"] == 1
    assert backlog["summary"]["act_coverage_gap_count"] == 7
    assert backlog["summary"]["act_confidence_tier_counts"] == {"C": 2, "D": 5}
    form_item = next(item for item in backlog["items"] if item["id"] == "forms-baseline-gst-rfd-01")
    assert form_item["sample_event_ids"] == ["evt_rfd_1"]
    act_item = next(item for item in backlog["items"] if item["id"] == "act-pipeline-backlog")
    assert act_item["summary"]["coverage_gap_count"] == 7
    overroute_item = next(item for item in backlog["items"] if item["id"] == "forms-lane-overrouted-rules")
    assert overroute_item["count"] == 1
    unclassified_item = next(item for item in backlog["items"] if item["id"] == "forms-unclassified-pending-baseline")
    assert unclassified_item["count"] == 1
    assert unclassified_item["sample_event_ids"] == ["evt_unknown_form"]
    recon_item = next(item for item in backlog["items"] if item["id"] == "rules-unresolved-reconciliation")
    assert recon_item["audit_class_counts"] == {"compound_split_needed": 1}
    assert recon_item["samples"][0]["audit_class"] == "compound_split_needed"
    assert recon_item["samples"][0]["candidate_event_ids"] == ["evt_rule_40_a", "evt_rule_40_b"]
    assert (tmp_path / "phase3_backlog.json").exists()


def test_rule_materializer_routes_statement_insert_sibling_out_of_rules_gaps(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso><act><body><article refersTo="/in/union/rules/cgst-rules-2017/rule/89">
<num>89</num><heading>Application for refund</heading><content><p>89. Refund rule text.</p></content>
</article></body></act></akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-06-19"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(
        json.dumps(
            {
                "event_id": "evt_stmt_5b",
                "operation": "INSERT_SIBLING",
                "status": "needs_review",
                "source": {"publication_date": "2017-10-31"},
                "legal_time": {"applicability_start": "2017-10-31"},
                "target": {
                    "work_id": "/in/union/rules/cgst-rules-2017",
                    "component_id": "/in/union/rules/cgst-rules-2017/rule/89",
                },
                "payload": {"label": "Statement 5B", "node_type": "statement", "content": "Statement 5B details"},
                "evidence": {"excerpt": "after Statement 5A, the following Statement shall be inserted, namely:-"},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["coverage_gap_count"] == 0
    assert manifest["forms_lane_routed_count"] == 1
    assert manifest["forms_lane_routed_events"][0]["event_id"] == "evt_stmt_5b"


def test_rule_materializer_does_not_retry_pending_context_recovery_rows(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso><act><body><article refersTo="/in/union/rules/cgst-rules-2017/rule/164">
<num>164</num><heading>Rule 164</heading><content><p>164. Existing rule text.</p></content>
</article></body></act></akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-06-19"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(
        json.dumps(
            {
                "event_id": "evt_pending_context_recovery",
                "operation": "SPLICE",
                "status": "needs_review",
                "source": {"publication_date": "2025-01-01"},
                "legal_time": {"applicability_start": "2025-01-01"},
                "target": {
                    "work_id": "/in/union/rules/cgst-rules-2017",
                    "component_id": "/in/union/rules/cgst-rules-2017/rule/164/subrule/4",
                    "anchor_text": "after payment of the full amount of tax",
                },
                "payload": {
                    "anchor_text": "after payment of the full amount of tax",
                    "insert_text": " inserted words",
                },
                "review": {"review_reasons": ["context_recovered_target_pending_validation"]},
                "validation": {"anchor_resolved": False, "materializable": False, "target_resolved": False},
                "evidence": {
                    "excerpt": (
                        "after the words after payment of the full amount of tax, "
                        "the words inserted words shall be inserted"
                    )
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["applied_count"] == 0
    assert manifest["coverage_gap_count"] == 1
    gaps = json.loads(Path(manifest["coverage_gaps"]).read_text(encoding="utf-8"))["gaps"]
    assert gaps[0]["skip_reason"] == "event_status_not_validated"


def test_rule_materializer_routes_rfd01_statement_block_out_of_rules_gaps(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso><act><body><article refersTo="/in/union/rules/cgst-rules-2017/rule/89">
<num>89</num><heading>Application for refund</heading><content><p>89. Refund rule text.</p></content>
</article></body></act></akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-06-19"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(
        json.dumps(
            {
                "event_id": "evt_rfd01_statement_block",
                "operation": "UNKNOWN",
                "status": "needs_review",
                "source": {"publication_date": "2022-07-05"},
                "legal_time": {"applicability_start": "2022-07-05"},
                "target": {
                    "work_id": "/in/union/rules/cgst-rules-2017",
                    "component_id": "/in/union/rules/cgst-rules-2017/rule/89",
                },
                "payload": {},
                "evidence": {
                    "excerpt": (
                        "In FORM-GST-RFD-01, after Statement-3A, the following statement "
                        "and table shall be inserted."
                    )
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["coverage_gap_count"] == 0
    assert manifest["forms_lane_routed_count"] == 1
    assert manifest["forms_lane_routed_events"][0]["event_id"] == "evt_rfd01_statement_block"


def test_rule_materializer_routes_rfd01_statement_fragments_out_of_rules_gaps(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso><act><body><article refersTo="/in/union/rules/cgst-rules-2017/rule/89">
<num>89</num><heading>Application for refund</heading><content><p>89. Refund rule text.</p></content>
</article></body></act></akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-06-19"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")
    fixtures = [
        (
            "evt_stmt4_fragment",
            "(b) after Statement 3A, the following Statement shall be inserted, namely:- "
            "Statement-4 [rule 89(2)(d) and 89(2)(e)] Refund Type: On account of supplies made to SEZ unit.",
        ),
        (
            "evt_declaration_fragment",
            "9. Whether Self-Declaration filed by Applicant u/s 54(4), Yes No if applicable "
            "DECLARATION [second proviso to section 54(3)] I hereby declare that the goods exported are not subject.",
        ),
        (
            "evt_instruction_fragment",
            "15. 'Turnover of zero rated supply of goods and services' shall have the same meaning as defined in rule 89(4).",
        ),
    ]
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(
        "\n".join(
            json.dumps(
                {
                    "event_id": event_id,
                    "operation": "UNKNOWN",
                    "status": "needs_review",
                    "source": {"publication_date": "2018-01-23"},
                    "legal_time": {"applicability_start": "2018-01-23"},
                    "target": {
                        "work_id": "/in/union/rules/cgst-rules-2017",
                        "component_id": "/in/union/rules/cgst-rules-2017/rule/89",
                    },
                    "payload": {},
                    "evidence": {"excerpt": excerpt},
                }
            )
            for event_id, excerpt in fixtures
        )
        + "\n",
        encoding="utf-8",
    )

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["coverage_gap_count"] == 0
    assert manifest["forms_lane_routed_count"] == 3
    assert {event["event_id"] for event in manifest["forms_lane_routed_events"]} == {
        "evt_stmt4_fragment",
        "evt_declaration_fragment",
        "evt_instruction_fragment",
    }


def test_rule_materializer_routes_rfd01_variants_mistargeted_to_rule_8_out_of_rules_gaps(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso><act><body><article refersTo="/in/union/rules/cgst-rules-2017/rule/8">
<num>8</num><heading>Application for registration</heading><content><p>8. Registration rule text.</p></content>
</article></body></act></akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-06-19"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")
    fixtures = [
        (
            "evt_rfd01_instruction_rule8",
            "INSERT_CHILD",
            "14. Availability of refund to be claimed in case of supplies made to SEZ unit or SEZ developer "
            "without payment of tax shall be worked out in accordance with the formula prescribed in rule 89(4).",
            {},
        ),
        (
            "evt_rfd01_statement_rule8",
            "INSERT_CHILD",
            "41. In the said rules, in FORM RFD-01,- (i) under the heading Instructions, in paragraph 10, "
            "for the figures, letters and words GSTR-1 and GSTR-2, the figures, letters and words "
            "GSTR-1 as amended by GSTR-1A, if any shall be substituted; (ii) after Statement-8, "
            "the following shall be inserted, namely:- Statement 9A [rule 89(2)(bb)] Refund Type.",
            {"label": "1", "node_type": "subrule", "content": "Statement 9A [rule 89(2)(bb)] Refund Type."},
        ),
    ]
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(
        "\n".join(
            json.dumps(
                {
                    "event_id": event_id,
                    "operation": operation,
                    "status": "needs_review",
                    "source": {"publication_date": "2024-10-08"},
                    "legal_time": {"applicability_start": "2024-10-08"},
                    "target": {
                        "work_id": "/in/union/rules/cgst-rules-2017",
                        "component_id": "/in/union/rules/cgst-rules-2017/rule/8/subrule/1",
                    },
                    "payload": payload,
                    "evidence": {"excerpt": excerpt},
                }
            )
            for event_id, operation, excerpt, payload in fixtures
        )
        + "\n",
        encoding="utf-8",
    )

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["coverage_gap_count"] == 0
    assert manifest["forms_lane_routed_count"] == 2
    assert {event["event_id"] for event in manifest["forms_lane_routed_events"]} == {
        "evt_rfd01_instruction_rule8",
        "evt_rfd01_statement_rule8",
    }


def test_rule_materializer_routes_form_serial_mutations_out_of_rules_gaps(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso><act><body><article refersTo="/in/union/rules/cgst-rules-2017/rule/89">
<num>89</num><heading>Application for refund</heading><content><p>89. Refund rule text.</p></content>
</article></body></act></akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-06-19"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(
        json.dumps(
            {
                "event_id": "evt_tran2_serial",
                "operation": "SUBSTITUTE",
                "status": "needs_review",
                "source": {"publication_date": "2017-08-30"},
                "legal_time": {"applicability_start": "2017-08-30"},
                "target": {
                    "work_id": "/in/union/rules/cgst-rules-2017",
                    "component_id": "/in/union/rules/cgst-rules-2017/rule/89",
                },
                "payload": {"old_text": "appointment date", "new_text": "appointed date"},
                "evidence": {
                    "excerpt": (
                        "with effect from the 1st day of July, 2017, in FORM GST TRAN-2, "
                        "in Serial No. 4, for the words appointment date, the words appointed "
                        "date shall be substituted"
                    )
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["coverage_gap_count"] == 0
    assert manifest["forms_lane_routed_count"] == 1
    assert manifest["forms_lane_routed_events"][0]["event_id"] == "evt_tran2_serial"


def test_rule_materializer_routes_form_instruction_mutations_out_of_rules_gaps(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso><act><body><article refersTo="/in/union/rules/cgst-rules-2017/rule/46">
<num>46</num><heading>Tax invoice</heading><content><p>46. Tax invoice rule text.</p></content>
</article></body></act></akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-06-19"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(
        json.dumps(
            {
                "event_id": "evt_gstr1_instruction",
                "operation": "UNKNOWN",
                "status": "needs_review",
                "source": {"publication_date": "2017-08-30"},
                "legal_time": {"applicability_start": "2017-08-30"},
                "target": {
                    "work_id": "/in/union/rules/cgst-rules-2017",
                    "component_id": "/in/union/rules/cgst-rules-2017/rule/46",
                },
                "payload": {},
                "evidence": {
                    "excerpt": (
                        "In FORM GSTR-1, in the Instructions, after serial number 17, "
                        "the following instruction shall be inserted, namely:- supply "
                        "made through e-commerce operator."
                    )
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["coverage_gap_count"] == 0
    assert manifest["forms_lane_routed_count"] == 1
    assert manifest["forms_lane_routed_events"][0]["event_id"] == "evt_gstr1_instruction"


def test_rule_materializer_routes_spaced_form_instruction_mutations_out_of_rules_gaps(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso><act><body><article refersTo="/in/union/rules/cgst-rules-2017/rule/36">
<num>36</num><heading>Documentary requirements</heading><content><p>36. Rule text mentioning FORM GSTR-1.</p></content>
</article></body></act></akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-06-19"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(
        json.dumps(
            {
                "event_id": "evt_gstr1_spaced_instruction",
                "operation": "SUBSTITUTE",
                "status": "needs_review",
                "source": {"publication_date": "2024-10-08"},
                "legal_time": {"applicability_start": "2024-10-08"},
                "target": {
                    "work_id": "/in/union/rules/cgst-rules-2017",
                    "component_id": "/in/union/rules/cgst-rules-2017/rule/36",
                    "anchor_text": "FORM GSTR-1",
                },
                "payload": {"old_text": "FORM GSTR- 1", "new_text": "FORM GSTR-1A"},
                "evidence": {
                    "excerpt": (
                        "In FORM GSTR- 1, in the Instructions, for the words, letters and "
                        "figure FORM GSTR- 1, the words, letters and figures FORM GSTR-1A "
                        "shall be substituted."
                    )
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["coverage_gap_count"] == 0
    assert manifest["forms_lane_routed_count"] == 1
    assert manifest["forms_lane_routed_events"][0]["event_id"] == "evt_gstr1_spaced_instruction"


def test_rule_materializer_routes_named_form_blocks_out_of_rules_gaps(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso><act><body>
<article refersTo="/in/union/rules/cgst-rules-2017/rule/80">
<num>80</num><heading>Annual return</heading><content><p>80. Annual return rule text.</p></content>
</article>
<article refersTo="/in/union/rules/cgst-rules-2017/rule/142">
<num>142</num><heading>Demand notices</heading><content><p>142. Demand rule text.</p></content>
</article>
</body></act></akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-06-19"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")
    fixtures = [
        (
            "evt_gstr9_insert",
            "/in/union/rules/cgst-rules-2017/rule/80",
            "In the said rules, after FORM GSTR-8, the following FORMS shall be inserted, namely:- "
            "“FORM GSTR-9 (See rule 80) Annual Return Pt. I Basic Details”",
        ),
        (
            "evt_gstr9_substitute",
            "/in/union/rules/cgst-rules-2017/rule/80",
            "In the said rules, for FORM GSTR 9, the following form shall be substituted, namely:- "
            "“FORM GSTR - 9 [See rule 80] Annual Return Pt. I Basic Details”",
        ),
        (
            "evt_drc01a_substitute",
            "/in/union/rules/cgst-rules-2017/rule/142",
            "In the said rules, for the FORM GST DRC-01A, the following Form shall be substituted, namely:- "
            "“FORM GST DRC-01A Intimation of tax ascertained as being payable [See Rule 142 (1A), (2A)]”",
        ),
    ]
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(
        "\n".join(
            json.dumps(
                {
                    "event_id": event_id,
                    "operation": "UNKNOWN",
                    "status": "needs_review",
                    "source": {"publication_date": "2018-09-04"},
                    "legal_time": {"applicability_start": "2018-09-04"},
                    "target": {
                        "work_id": "/in/union/rules/cgst-rules-2017",
                        "component_id": component_id,
                    },
                    "payload": {},
                    "evidence": {"excerpt": excerpt},
                }
            )
            for event_id, component_id, excerpt in fixtures
        )
        + "\n",
        encoding="utf-8",
    )

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["coverage_gap_count"] == 0
    assert manifest["forms_lane_routed_count"] == 3
    assert {event["event_id"] for event in manifest["forms_lane_routed_events"]} == {
        "evt_gstr9_insert",
        "evt_gstr9_substitute",
        "evt_drc01a_substitute",
    }


def test_materializer_repairs_context_recovered_neighbor_rule_target(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso><act><body>
<article refersTo="/in/union/rules/cgst-rules-2017/rule/138">
<num>138</num><heading>Information to be furnished prior to movement</heading><content><p>138. Rule text.</p></content>
</article>
<paragraph refersTo="/in/union/rules/cgst-rules-2017/rule/138/subrule/5">
<num>(5)</num><content><p>(5) Neighboring subrule text.</p></content>
</paragraph>
<article refersTo="/in/union/rules/cgst-rules-2017/rule/138a">
<num>138A</num><heading>Documents and devices to be carried</heading><content><p>138A. Documents. (5) Notwithstanding anything contained, the Commissioner may act.</p></content>
</article>
<paragraph refersTo="/in/union/rules/cgst-rules-2017/rule/138a/subrule/5">
<num>(5)</num><content><p>(5) Notwithstanding anything contained, the Commissioner may act.</p></content>
</paragraph>
</body></act></akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-06-19"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(
        json.dumps(
            {
                "event_id": "evt_cbic_c2b1f8125e9039bc",
                "operation": "SUBSTITUTE",
                "status": "validated",
                "source": {"publication_date": "2018-01-23"},
                "legal_time": {"applicability_start": "2018-02-01"},
                "target": {
                    "work_id": "/in/union/rules/cgst-rules-2017",
                    "component_id": "/in/union/rules/cgst-rules-2017/rule/138/subrule/5",
                    "anchor_text": "Notwithstanding anything contained",
                },
                "payload": {
                    "context_recovered_target": True,
                    "old_text": "Notwithstanding anything contained",
                    "new_text": "Notwithstanding anything contained in",
                },
                "validation": {
                    "target_resolved": True,
                    "anchor_resolved": True,
                    "date_resolved": True,
                    "source_span_verified": True,
                    "materializable": True,
                },
                "evidence": {
                    "source_span": {"start": 10, "text_hash": "abc"},
                    "excerpt": (
                        "(xii) with effect from 1st February, 2018, in rule 138A, in "
                        "sub-rule (5), for the words \"Notwithstanding anything contained\", "
                        "the words \"Notwithstanding anything contained in\" shall be substituted;"
                    ),
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["coverage_gap_count"] == 0
    assert manifest["applied_count"] == 1
    versions = [
        json.loads(line)
        for line in (tmp_path / "out/node_versions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    rule_138_versions = [
        row for row in versions if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/138/subrule/5"
    ]
    rule_138a_versions = [
        row for row in versions if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/138a/subrule/5"
    ]
    assert "Neighboring subrule text" in rule_138_versions[-1]["text"]
    assert "Notwithstanding anything contained in" not in rule_138_versions[-1]["text"]
    assert "Notwithstanding anything contained in" in rule_138a_versions[-1]["text"]


def test_materializer_repairs_rule_142_20_2024_subrule_4_section_74a(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso><act><body>
<article refersTo="/in/union/rules/cgst-rules-2017/rule/142">
<num>142</num><heading>Notice and order for demand</heading><content><p>142. Notice and order for demand. (4) The representation referred to in sub-section (9) of section 73 or sub-section (9) of section 74 or sub-section (3) of section 76 shall be in FORM GST DRC-06.</p></content>
</article>
<paragraph refersTo="/in/union/rules/cgst-rules-2017/rule/142/subrule/4">
<num>(4)</num><content><p>(4) The representation referred to in sub-section (9) of section 73 or sub-section (9) of section 74 or sub-section (3) of section 76 shall be in FORM GST DRC-06.</p></content>
</paragraph>
</body></act></akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-06-19"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(
        json.dumps(
            {
                "event_id": "evt_cbic_4e3d7e920de72e7d",
                "operation": "SPLICE",
                "status": "needs_review",
                "source": {
                    "document_id": "/in/union/notifications/cbic/central-tax/2024/20-2024",
                    "publication_date": "2024-10-08",
                },
                "legal_time": {"applicability_start": "2024-10-08"},
                "target": {
                    "work_id": "/in/union/rules/cgst-rules-2017",
                    "component_id": "/in/union/rules/cgst-rules-2017/rule/142",
                    "anchor_text": "of section 74",
                },
                "payload": {"position": "after", "insert_text": "or sub-section (6) of section 74A"},
                "validation": {
                    "target_resolved": True,
                    "anchor_resolved": True,
                    "date_resolved": True,
                    "source_span_verified": True,
                    "materializable": False,
                },
                "evidence": {
                    "source_span": {"start": 10, "text_hash": "abc"},
                    "excerpt": (
                        "(f) in sub-rule (4), after the words and figures \"of section 74\", "
                        "the words, brackets, figures and letters \"or sub-section (6) of "
                        "section 74A\" shall be inserted."
                    ),
                },
                "review": {"required": True, "review_reasons": ["same_effective_date_conflict"]},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["coverage_gap_count"] == 0
    assert manifest["applied_count"] == 1
    versions = [
        json.loads(line)
        for line in (tmp_path / "out/node_versions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    subrule_4_versions = [
        row for row in versions if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/142/subrule/4"
    ]
    assert "sub-section (6) of section 74A" in subrule_4_versions[-1]["text"]


def test_materializer_repairs_rule_142_1a_2a_sequence(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso><act><body>
<article refersTo="/in/union/rules/cgst-rules-2017/rule/142">
<num>142</num><heading>Notice and order for demand</heading><content><p>142. Notice and order for demand. Old rule text.</p></content>
</article>
<paragraph refersTo="/in/union/rules/cgst-rules-2017/rule/142/subrule/2">
<num>(2)</num><content><p>(2) Where any person makes payment of tax, interest, penalty or any other amount due in accordance with the provisions of the Act he shall inform the proper officer in FORM GST DRC-03.</p></content>
</paragraph>
</body></act></akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-06-19"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")
    events = [
        {
            "event_id": "evt_cbic_ba1f7eea0bfef2d7",
            "operation": "SUBSTITUTE",
            "status": "needs_review",
            "source": {
                "document_id": "/in/union/notifications/cbic/central-tax/2019/16-2019",
                "publication_date": "2019-03-29",
            },
            "legal_time": {"applicability_start": "2019-04-01"},
            "target": {
                "work_id": "/in/union/rules/cgst-rules-2017",
                "component_id": "/in/union/rules/cgst-rules-2017/rule/142",
            },
            "payload": {"structural_text": "142. Notice and order for demand. Truncated at sub-"},
            "evidence": {
                "source_span": {"start": 5, "text_hash": "rule142-16"},
                "excerpt": "for rule 142, the following rule shall be substituted",
            },
            "review": {"required": True, "review_reasons": ["unsupported_materializer_operation"]},
        },
        {
            "event_id": "evt_cbic_2c1e4044c813bf37",
            "operation": "SPLICE",
            "status": "needs_review",
            "source": {
                "document_id": "/in/union/notifications/cbic/central-tax/2019/49-2019",
                "publication_date": "2019-10-09",
            },
            "legal_time": {"applicability_start": "2019-10-09"},
            "target": {
                "work_id": "/in/union/rules/cgst-rules-2017",
                "component_id": "/in/union/rules/cgst-rules-2017/rule/142",
                "anchor_text": "in accordance with the provisions of the Act",
            },
            "payload": {
                "position": "after",
                "insert_text": (
                    ", whether on his own ascertainment or, as communicated by the proper "
                    "officer under sub-rule (1A),"
                ),
            },
            "evidence": {
                "source_span": {"start": 10, "text_hash": "rule142-49"},
                "excerpt": "in rule 142 ... after sub-rule (1) ... in sub-rule (2) ... after sub-rule (2)",
            },
            "review": {"required": True, "review_reasons": ["anchor_not_resolved"]},
        },
        {
            "event_id": "evt_cbic_9869af72fcfd6dc2",
            "operation": "SUBSTITUTE",
            "status": "needs_review",
            "source": {
                "document_id": "/in/union/notifications/cbic/central-tax/2020/79-2020",
                "publication_date": "2020-10-15",
            },
            "legal_time": {"applicability_start": "2020-10-15"},
            "target": {
                "work_id": "/in/union/rules/cgst-rules-2017",
                "component_id": "/in/union/rules/cgst-rules-2017/rule/142/subrule/1a",
                "anchor_text": "proper officer shall",
            },
            "payload": {"old_text": "proper officer shall", "new_text": "proper officer may"},
            "evidence": {
                "source_span": {"start": 20, "text_hash": "rule142-79"},
                "excerpt": "in rule 142, in sub-rule (1A), for the words proper officer shall ... shall communicate",
            },
            "review": {"required": True, "review_reasons": ["compound_block_contains_multiple_amendments"]},
        },
        {
            "event_id": "evt_cbic_d56f9b1cfb5e2603",
            "operation": "SPLICE",
            "status": "needs_review",
            "source": {
                "document_id": "/in/union/notifications/cbic/central-tax/2024/12-2024",
                "publication_date": "2024-07-10",
            },
            "legal_time": {"applicability_start": "2024-07-10"},
            "target": {
                "work_id": "/in/union/rules/cgst-rules-2017",
                "component_id": "/in/union/rules/cgst-rules-2017/rule/142",
                "anchor_text": "FORM GST DRC-01A",
            },
            "payload": {
                "position": "after",
                "insert_text": ", and thereafter the proper officer may issue an intimation in Part-C of FORM GST DRC-01A",
            },
            "evidence": {
                "source_span": {"start": 30, "text_hash": "rule142-12"},
                "excerpt": "in sub-rule (2A), after the words FORM GST DRC-01A",
            },
            "review": {"required": True, "review_reasons": ["anchor_not_resolved"]},
        },
        {
            "event_id": "evt_cbic_e6ed17e56a016068",
            "operation": "SPLICE",
            "status": "needs_review",
            "source": {
                "document_id": "/in/union/notifications/cbic/central-tax/2024/20-2024",
                "publication_date": "2024-10-08",
            },
            "legal_time": {"applicability_start": "2024-10-08"},
            "target": {
                "work_id": "/in/union/rules/cgst-rules-2017",
                "component_id": "/in/union/rules/cgst-rules-2017/rule/142",
                "anchor_text": "of section 74",
            },
            "payload": {"position": "after", "insert_text": "or sub-section (1) of section 74A"},
            "evidence": {
                "source_span": {"start": 40, "text_hash": "rule142-20"},
                "excerpt": "in sub-rule(1A), after the words and figures of section 74",
            },
            "review": {"required": True, "review_reasons": ["same_effective_date_conflict"]},
        },
    ]
    events_path = tmp_path / "events.jsonl"
    events_path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["coverage_gap_count"] == 0
    assert manifest["applied_count"] == 9
    assert {
        "evt_cbic_ba1f7eea0bfef2d7_rule142_2_replace",
        "evt_cbic_2c1e4044c813bf37_rule142_2_own_ascertainment_splice",
    }.issubset({event["event_id"] for event in manifest["applied_events"]})

    versions = [
        json.loads(line)
        for line in (tmp_path / "out" / "node_versions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    by_component = {}
    for version in versions:
        by_component.setdefault(version["component_id"], []).append(version)
    rule_text = by_component["/in/union/rules/cgst-rules-2017/rule/142"][-1]["text"]
    subrule_1a = by_component["/in/union/rules/cgst-rules-2017/rule/142/subrule/1a"][-1]["text"]
    subrule_2 = by_component["/in/union/rules/cgst-rules-2017/rule/142/subrule/2"][-1]["text"]
    subrule_2a = by_component["/in/union/rules/cgst-rules-2017/rule/142/subrule/2a"][-1]["text"]

    assert "communicated by the proper officer under sub-rule (1A)" in rule_text
    assert "communicated by the proper officer under sub-rule (1A)" in subrule_2
    assert any(
        version["created_by_event_id"] == "evt_cbic_ba1f7eea0bfef2d7_rule142_2_replace"
        and version["valid_from"] == "2019-04-01"
        for version in by_component["/in/union/rules/cgst-rules-2017/rule/142/subrule/2"]
    )
    assert any(
        version["created_by_event_id"] == "evt_cbic_2c1e4044c813bf37_rule142_2_own_ascertainment_splice"
        and version["valid_from"] == "2019-10-09"
        for version in by_component["/in/union/rules/cgst-rules-2017/rule/142/subrule/2"]
    )
    assert "proper officer may" in subrule_1a
    assert "communicate the details" in subrule_1a
    assert "or sub-section (1) of section 74A" in subrule_1a
    assert "Part-C of FORM GST DRC-01A" in subrule_2a


def test_materializer_repairs_rule_44_early_subrule_sequence(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso><act><body>
<article refersTo="/in/union/rules/cgst-rules-2017/rule/44">
<num>44</num><heading>Manner of reversal of credit under special circumstances</heading><content><p>44. Manner of reversal. (1) Inputs. (2) The amount, as specified in sub-rule (1) shall be determined separately for input tax credit of integrated tax and central tax. (2) Where the tax invoices related to the inputs held in stock are not available, the registered person shall estimate the amount under sub-rule (1) based on the prevailing market price of the goods on the effective date of the occurrence of any of the events specified in sub-section (4) of section 18 or, as the case may be, sub-section (5) of section 29. (5) The details furnished in accordance with sub-rule (3) shall be duly certified by a practicing chartered accountant or cost accountant.</p></content>
</article>
<paragraph refersTo="/in/union/rules/cgst-rules-2017/rule/44/subrule/2">
<num>(2)</num><content><p>(2) The amount, as specified in sub-rule (1) shall be determined separately for input tax credit of integrated tax and central tax</p></content>
</paragraph>
<paragraph refersTo="/in/union/rules/cgst-rules-2017/rule/44/subrule/3">
<num>(3)</num><content><p>(3) Where the tax invoices related to the inputs held in stock are not available, the registered person shall estimate the amount under sub-rule (1) based on the prevailing market price of the goods on the effective date of the occurrence of any of the events specified in sub-section (4) of section 18 or, as the case may be, sub-section (5) of section 29</p></content>
</paragraph>
<paragraph refersTo="/in/union/rules/cgst-rules-2017/rule/44/subrule/5">
<num>(5)</num><content><p>(5) The details furnished in accordance with sub-rule (3) shall be duly certified by a practicing chartered accountant or cost accountant</p></content>
</paragraph>
</body></act></akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-06-19"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")
    events = [
        {
            "event_id": "evt_cbic_3999927ca4e1a75a",
            "operation": "SUBSTITUTE",
            "status": "needs_review",
            "source": {"document_id": "/in/union/notifications/cbic/central-tax/2017/15-2017", "publication_date": "2017-07-01"},
            "legal_time": {"applicability_start": "2017-07-01"},
            "target": {
                "work_id": "/in/union/rules/cgst-rules-2017",
                "component_id": "/in/union/rules/cgst-rules-2017/rule/44/subrule/2",
            },
            "payload": {"old_text": "integrated tax", "new_text": "(2)"},
            "evidence": {"source_span": {"start": 1, "text_hash": "rule44-15"}},
            "review": {"required": True, "review_reasons": ["anchor_not_resolved"]},
        },
        {
            "event_id": "evt_cbic_b950b33bbe16bdcf",
            "operation": "SUBSTITUTE",
            "status": "validated",
            "source": {"document_id": "/in/union/notifications/cbic/central-tax/2017/15-2017", "publication_date": "2017-07-01"},
            "legal_time": {"applicability_start": "2017-07-01"},
            "target": {
                "work_id": "/in/union/rules/cgst-rules-2017",
                "component_id": "/in/union/rules/cgst-rules-2017/rule/44/subrule/2",
                "anchor_text": "integrated tax and central tax",
            },
            "payload": {
                "old_text": "integrated tax and central tax",
                "new_text": "central tax, State tax, Union territory tax and integrated tax",
                "triage_lane": "context_unresolved",
            },
            "evidence": {"source_span": {"start": 2, "text_hash": "rule44-15a"}},
            "review": {"required": False, "review_reasons": []},
        },
        {
            "event_id": "evt_cbic_64b4c848b468ead6",
            "operation": "SUBSTITUTE",
            "status": "validated",
            "source": {"document_id": "/in/union/notifications/cbic/central-tax/2017/17-2017", "publication_date": "2017-07-27"},
            "legal_time": {"applicability_start": "2017-07-27"},
            "target": {
                "work_id": "/in/union/rules/cgst-rules-2017",
                "component_id": "/in/union/rules/cgst-rules-2017/rule/44/subrule/1",
                "anchor_text": "sub-rules (2) and (3)",
            },
            "payload": {"old_text": "sub-rules (2) and (3)", "new_text": "truncated"},
            "evidence": {"source_span": {"start": 3, "text_hash": "rule44-17"}},
            "review": {"required": False, "review_reasons": []},
        },
        {
            "event_id": "evt_cbic_3f2c97833c47f80d",
            "operation": "SPLICE",
            "status": "validated",
            "source": {"document_id": "/in/union/notifications/cbic/central-tax/2017/34-2017", "publication_date": "2017-09-15"},
            "legal_time": {"applicability_start": "2017-09-15"},
            "target": {
                "work_id": "/in/union/rules/cgst-rules-2017",
                "component_id": "/in/union/rules/cgst-rules-2017/rule/44/subrule/5",
                "anchor_text": "or sub-rule (3)",
            },
            "payload": {"position": "after", "insert_text": "or sub-rule (3A)"},
            "evidence": {"source_span": {"start": 4, "text_hash": "rule44-34"}},
            "review": {"required": False, "review_reasons": []},
        },
    ]
    events_path = tmp_path / "events.jsonl"
    events_path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["coverage_gap_count"] == 0
    assert manifest["applied_count"] == 4
    versions = [
        json.loads(line)
        for line in (tmp_path / "out" / "node_versions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    by_component = {}
    for version in versions:
        by_component.setdefault(version["component_id"], []).append(version)
    rule_text = by_component["/in/union/rules/cgst-rules-2017/rule/44"][-1]["text"]
    subrule_2 = by_component["/in/union/rules/cgst-rules-2017/rule/44/subrule/2"][-1]["text"]
    subrule_3 = by_component["/in/union/rules/cgst-rules-2017/rule/44/subrule/3"][-1]["text"]
    subrule_5 = by_component["/in/union/rules/cgst-rules-2017/rule/44/subrule/5"][-1]["text"]

    assert "central tax, State tax, Union territory tax and integrated tax. (3) Where" in rule_text
    assert "central tax, State tax, Union territory tax and integrated tax" in subrule_2
    assert subrule_3.endswith("sub-section (5) of section 29.")
    assert "sub-rule (3) or sub-rule (3A)" in subrule_5


def test_materializer_repairs_rule_83_practitioner_duration_chain(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso><act><body>
<article refersTo="/in/union/rules/cgst-rules-2017/rule/83">
<num>83</num><heading>Provisions relating to a goods and services tax practitioner</heading><content><p>83. Provisions relating to a goods and services tax practitioner. (3) The enrolment made under sub-rule (2) shall be valid until it is cancelled: Provided further that no person to whom the provisions of clause (b) of sub-section (1) apply shall be eligible to remain enrolled unless he passes the said examination within a period of one year from the appointed date.</p></content>
</article>
<paragraph refersTo="/in/union/rules/cgst-rules-2017/rule/83/subrule/3">
<num>(3)</num><content><p>(3) The enrolment made under sub-rule (2) shall be valid until it is cancelled: Provided further that no person to whom the provisions of clause (b) of sub-section</p></content>
</paragraph>
</body></act></akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-06-19"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")
    events = [
        {
            "event_id": "evt_cbic_664c6e5cde7e01e4",
            "operation": "SUBSTITUTE",
            "status": "validated",
            "source": {"document_id": "/in/union/notifications/cbic/central-tax/2017/17-2017", "publication_date": "2017-07-27"},
            "legal_time": {"applicability_start": "2017-07-27"},
            "target": {
                "work_id": "/in/union/rules/cgst-rules-2017",
                "component_id": "/in/union/rules/cgst-rules-2017/rule/83/subrule/3",
                "anchor_text": "sub-section",
            },
            "payload": {"old_text": "sub-section", "new_text": "sub-rule"},
            "evidence": {"source_span": {"start": 1, "text_hash": "rule83-17"}},
            "review": {"required": False, "review_reasons": []},
        },
        {
            "event_id": "evt_cbic_a4492bf3eaf121cc",
            "operation": "SUBSTITUTE",
            "status": "needs_review",
            "source": {"document_id": "/in/union/notifications/cbic/central-tax/2018/26-2018", "publication_date": "2018-06-13"},
            "legal_time": {"applicability_start": "2018-06-13"},
            "target": {
                "work_id": "/in/union/rules/cgst-rules-2017",
                "component_id": "/in/union/rules/cgst-rules-2017/rule/83/subrule/3",
                "anchor_text": "one year",
            },
            "payload": {"old_text": "one year", "new_text": "eighteen months"},
            "evidence": {"source_span": {"start": 2, "text_hash": "rule83-26"}},
            "review": {"required": True, "review_reasons": ["anchor_not_resolved"]},
        },
        {
            "event_id": "evt_cbic_659de6170f492d5f",
            "operation": "SUBSTITUTE",
            "status": "validated",
            "source": {"document_id": "/in/union/notifications/cbic/central-tax/2019/3-2019", "publication_date": "2019-01-29"},
            "legal_time": {"applicability_start": "2019-02-18"},
            "target": {
                "work_id": "/in/union/rules/cgst-rules-2017",
                "component_id": "/in/union/rules/cgst-rules-2017/rule/83/subrule/3",
                "anchor_text": "eighteen months",
            },
            "payload": {"old_text": "eighteen months", "new_text": "thirty months"},
            "evidence": {"source_span": {"start": 3, "text_hash": "rule83-3"}},
            "review": {"required": False, "review_reasons": []},
        },
    ]
    events_path = tmp_path / "events.jsonl"
    events_path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["coverage_gap_count"] == 0
    assert manifest["applied_count"] == 3
    versions = [
        json.loads(line)
        for line in (tmp_path / "out" / "node_versions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    subrule_versions = [
        row for row in versions if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/83/subrule/3"
    ]
    assert subrule_versions[-1]["valid_from"] == "2019-02-18"
    assert "clause (b) of sub-rule (1)" in subrule_versions[-1]["text"]
    assert "within a period of thirty months from the appointed date" in subrule_versions[-1]["text"]


def test_materializer_repairs_rule_43_explanation_replacement(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso><act><body>
<article refersTo="/in/union/rules/cgst-rules-2017/rule/43">
<num>43</num><heading>Manner of determination of input tax credit in respect of capital goods and reversal</heading><content><p>43. Rule text. Explanation For the purposes of rule 42 and this rule, it is hereby clarified that the aggregate value of exempt supplies shall exclude the value of supply of services specified in the notification of the Government of India in the Ministry of Finance, Department of Revenue No. 42/2017-Integrated Tax (Rate), dated the 27th October, 2017 published in the Gazette of India, Extraordinary, Part II, Section 3, Sub-section (i), vide number GSR 1338(E) dated the 27th October, 2017.</p></content>
</article>
<paragraph refersTo="/in/union/rules/cgst-rules-2017/rule/43/explanation/explanation-293eb53e74">
<num>Explanation</num><content><p>Explanation
For the purposes of rule 42 and this rule, it is hereby clarified that the aggregate value of exempt supplies shall exclude the value of supply of services specified in the notification of the Government of India in the Ministry of Finance, Department of Revenue No. 42/2017-Integrated Tax (Rate), dated the 27th October, 2017 published in the Gazette of India, Extraordinary, Part II, Section 3, Sub-section (i), vide number GSR 1338(E) dated the 27th October, 2017.</p></content>
</paragraph>
</body></act></akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-11-15"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")
    event = {
        "event_id": "evt_cbic_e2a8ec5c8dfd4588",
        "operation": "SUBSTITUTE",
        "status": "needs_review",
        "source": {
            "document_id": "/in/union/notifications/cbic/central-tax/2018/3-2018",
            "publication_date": "2018-01-23",
            "record_id": "1000795",
        },
        "legal_time": {"applicability_start": "2018-01-23"},
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": "/in/union/rules/cgst-rules-2017/rule/43",
            "anchor_text": "the Explanation",
        },
        "payload": {
            "old_text": "the Explanation",
            "new_text": "Explanation:-For the purposes of rule 42 and this rule, it is hereby clarified that the aggregate value of exempt supplies shall exclude:-",
        },
        "evidence": {"source_span": {"start": 3200, "end": 4163, "text_hash": "rule43-3-2018"}},
        "review": {"required": True, "review_reasons": ["anchor_not_resolved"]},
    }
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(json.dumps(event) + "\n", encoding="utf-8")

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["coverage_gap_count"] == 0
    assert manifest["applied_count"] == 1
    versions = [
        json.loads(line)
        for line in (tmp_path / "out" / "node_versions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    explanation_versions = [
        row
        for row in versions
        if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/43/explanation/explanation-293eb53e74"
    ]
    assert explanation_versions[-1]["created_by_event_id"] == "evt_cbic_e2a8ec5c8dfd4588"
    assert "shall exclude:- (a) the value of supply of services" in explanation_versions[-1]["text"]
    assert "services by way of accepting deposits" in explanation_versions[-1]["text"]
    assert "transportation of goods by a vessel" in explanation_versions[-1]["text"]


def test_materializer_repairs_rule_96a_marginal_heading_substitution(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso><act><body>
<article refersTo="/in/union/rules/cgst-rules-2017/rule/96a">
<num>96A</num><heading>Refund of integrated tax paid on export of goods or services under bond or Letter of Undertaking</heading><content><p>(1) Any registered person may export under bond or Letter of Undertaking.</p></content>
</article>
</body></act></akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-10-18"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")
    event = {
        "event_id": "evt_cbic_2be49e85a83a7268",
        "operation": "SUBSTITUTE",
        "status": "validated",
        "source": {
            "document_id": "/in/union/notifications/cbic/central-tax/2019/3-2019",
            "publication_date": "2019-01-29",
            "record_id": "1000713",
        },
        "legal_time": {"applicability_start": "2019-02-18"},
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": "/in/union/rules/cgst-rules-2017/rule/96a",
        },
        "payload": {
            "old_text": "Refund of integrated tax paid on export",
            "new_text": "Export",
        },
        "evidence": {"source_span": {"start": 11505, "end": 11630, "text_hash": "rule96a-heading"}},
        "review": {"required": False, "review_reasons": []},
    }
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(json.dumps(event) + "\n", encoding="utf-8")

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["coverage_gap_count"] == 0
    versions = [
        json.loads(line)
        for line in (tmp_path / "out" / "node_versions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    rule_versions = [
        row for row in versions if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/96a"
    ]
    assert rule_versions[-1]["created_by_event_id"] == "evt_cbic_2be49e85a83a7268"
    assert "Export of goods or services under bond or Letter of Undertaking" in rule_versions[-1]["text"]
    assert "Refund of integrated tax paid on export" not in rule_versions[-1]["text"]
    assert "(1) Any registered person may export under bond" in rule_versions[-1]["text"]


def test_materializer_repairs_rule_96a_2017_provisos_to_parent_rule(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso><act><body>
<article refersTo="/in/union/rules/cgst-rules-2017/rule/96a">
<num>96A</num><heading>Refund of integrated tax paid on export of goods or services under bond or Letter of Undertaking</heading>
<content><p>96A. Refund of integrated tax paid on export of goods or services under bond or Letter of Undertaking. (1) Any registered person may export goods or services without payment of integrated tax after furnishing a bond or a Letter of Undertaking. (2) The details of the export invoices contained in FORM GSTR-1 furnished on the common portal shall be electronically transmitted to the system designated by Customs and a confirmation that the goods covered by the said invoices have been exported out of India shall be electronically transmitted to the common portal from the said system. (3) Where the goods are not exported out of India within three months, the registered person shall pay the amount due.</p></content>
</article>
</body></act></akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-06-19"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")
    event = {
        "event_id": "evt_cbic_3c97ca00a2d9edc0",
        "operation": "INSERT_CHILD",
        "status": "needs_review",
        "source": {
            "document_id": "/in/union/notifications/cbic/central-tax/2017/51-2017",
            "publication_date": "2017-10-28",
            "record_id": "1000822",
        },
        "legal_time": {"applicability_start": "2017-10-28"},
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": "/in/union/rules/cgst-rules-2017/rule/96a/subrule/provided that",
            "anchor_component_id": "/in/union/rules/cgst-rules-2017/rule/96a/subrule/2",
            "anchor_text": "the following provisos shall be inserted, namely:-",
        },
        "payload": {
            "anchor_component_id": "/in/union/rules/cgst-rules-2017/rule/96a/subrule/2",
            "content": (
                "Provided that where the date for furnishing the details of outward supplies "
                "in FORM GSTR-1 for a tax period has been extended in exercise of the powers "
                "conferred under section 37 of the Act, the supplier shall furnish the "
                "information relating to exports as specified in Table 6A of FORM GSTR-1 "
                "after the return in FORM GSTR-3B has been furnished and the same shall be "
                "transmitted electronically by the common port"
            ),
            "forms_lane_pending_baseline": True,
            "label": "Provided that",
            "node_type": "proviso",
            "parent_component_id": "/in/union/rules/cgst-rules-2017/rule/96a",
            "position": "after",
            "triage_lane": "forms_lane_pending_baseline",
        },
        "evidence": {"source_span": {"start": 2047, "end": 3245, "text_hash": "rule96a-provisos"}},
        "review": {"required": True, "review_reasons": ["anchor_not_resolved"]},
        "validation": {"anchor_resolved": False, "materializable": False, "target_resolved": True},
    }
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(json.dumps(event) + "\n", encoding="utf-8")

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["coverage_gap_count"] == 0
    assert manifest["applied_count"] == 1
    versions = [
        json.loads(line)
        for line in (tmp_path / "out" / "node_versions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    rule_versions = [
        row for row in versions if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/96a"
    ]
    text = rule_versions[-1]["text"]
    assert rule_versions[-1]["created_by_event_id"] == "evt_cbic_3c97ca00a2d9edc0"
    assert "common portal to the system designated by the Customs:" in text
    assert "Provided further that the information in Table 6A furnished under the first proviso" in text
    assert text.index("from the said system.") < text.index("Provided that where")
    assert text.index("the said tax period.") < text.index("(3) Where the goods are not exported")


def test_materializer_applies_reviewed_rule_96a_2024_splice_despite_stale_forms_lane(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso><act><body>
<article refersTo="/in/union/rules/cgst-rules-2017/rule/96a">
<num>96A</num><heading>Export of goods or services under bond or Letter of Undertaking</heading>
<content><p>96A. Export of goods or services under bond or Letter of Undertaking. (2) The details of the export invoices contained in FORM GSTR-1 furnished on the common portal shall be electronically transmitted to the system designated by Customs.</p></content>
</article>
</body></act></akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-06-19"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")
    event = {
        "event_id": "evt_cbic_bf470bd055dff9da",
        "operation": "SPLICE",
        "status": "validated",
        "source": {
            "document_id": "/in/union/notifications/cbic/central-tax/2024/12-2024",
            "publication_date": "2024-07-10",
            "record_id": "1010097",
        },
        "legal_time": {"applicability_start": "2024-07-10"},
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": "/in/union/rules/cgst-rules-2017/rule/96a",
            "anchor_text": "contained in FORM GSTR-1",
            "anchor_occurrence": 1,
        },
        "payload": {
            "forms_lane_pending_baseline": True,
            "insert_text": ", as amended in FORM GSTR-1A if any,",
            "position": "after",
            "triage_lane": "forms_lane_pending_baseline",
        },
        "evidence": {"source_span": {"start": 21488, "end": 21663, "text_hash": "rule96a-gstr1a"}},
        "review": {"required": False, "review_reasons": []},
        "validation": {"anchor_resolved": True, "materializable": True, "target_resolved": True},
    }
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(json.dumps(event) + "\n", encoding="utf-8")

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["coverage_gap_count"] == 0
    assert manifest["applied_count"] == 1
    versions = [
        json.loads(line)
        for line in (tmp_path / "out" / "node_versions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    rule_versions = [
        row for row in versions if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/96a"
    ]
    assert rule_versions[-1]["created_by_event_id"] == "evt_cbic_bf470bd055dff9da"
    normalized_text = re.sub(r"\s+", " ", rule_versions[-1]["text"]).replace(" ,", ",")
    assert "contained in FORM GSTR-1, as amended in FORM GSTR-1A if any, furnished" in normalized_text


def test_materializer_repairs_rule_96a_2024_clause_b_substitution_from_rule96_prefix_match(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso><act><body>
<article refersTo="/in/union/rules/cgst-rules-2017/rule/96">
<num>96</num><heading>Refund of integrated tax paid on goods or services exported out of India.</heading>
<content><p>96. Refund rule text. (1) A different sub-rule one text.</p></content>
</article>
<article refersTo="/in/union/rules/cgst-rules-2017/rule/96a">
<num>96A</num><heading>Export of goods or services under bond or Letter of Undertaking</heading>
<content><p>96A. Export of goods or services under bond or Letter of Undertaking. (1) Any registered person may export under bond within a period of - (a) fifteen days after the expiry of three months or such further period as may be allowed by the Commissioner from the date of issue of the invoice for export, if the goods are not exported out of India; or (b) fifteen days after the expiry of one year, or such further period as may be allowed by the Commissioner, from the date of issue of the invoice for export, if the payment of such services is not received by the exporter in convertible foreign exchange or in Indian rupees, wherever permitted by the Reserve Bank of India.</p></content>
</article>
</body></act></akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-06-19"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")
    event = {
        "event_id": "evt_cbic_fe42a22b62593e55",
        "operation": "SUBSTITUTE",
        "status": "validated",
        "source": {
            "document_id": "/in/union/notifications/cbic/central-tax/2024/12-2024",
            "publication_date": "2024-07-10",
            "record_id": "1010097",
        },
        "legal_time": {"applicability_start": "2024-07-10"},
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": "/in/union/rules/cgst-rules-2017/rule/96/subrule/1",
            "anchor_text": "existing text",
        },
        "payload": {
            "context_recovered_target": True,
            "new_text": "amended text",
            "old_text": "existing text",
        },
        "evidence": {"source_span": {"start": 20889, "end": 21487, "text_hash": "rule96a-clause-b"}},
        "review": {"required": False, "review_reasons": []},
        "validation": {"anchor_resolved": True, "materializable": True, "target_resolved": True},
    }
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(json.dumps(event) + "\n", encoding="utf-8")

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["coverage_gap_count"] == 0
    assert manifest["applied_count"] == 1
    versions = [
        json.loads(line)
        for line in (tmp_path / "out" / "node_versions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    rule96_versions = [
        row for row in versions if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/96"
    ]
    rule96a_versions = [
        row for row in versions if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/96a"
    ]
    assert len(rule96_versions) == 1
    assert rule96a_versions[-1]["created_by_event_id"] == "evt_cbic_fe42a22b62593e55"
    assert "Foreign Exchange Management Act, 1999 (42 of 1999)" in rule96a_versions[-1]["text"]
    assert "whichever is later, from the date of issue of the invoice for export" in rule96a_versions[-1]["text"]


def test_materializer_repairs_rule_133_explanation_retarget_from_rule_132(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso><act><body>
<article refersTo="/in/union/rules/cgst-rules-2017/rule/132">
<num>132</num><heading>Power to summon persons to give evidence and produce documents</heading><content><p>132. Rule 132 text.</p></content>
</article>
<article refersTo="/in/union/rules/cgst-rules-2017/rule/133">
<num>133</num><heading>Order of the Authority</heading><content><p>133. Order of the Authority. (3) The Authority may order deposit in the Fund of the concerned State. Explanation: For the purpose of this sub-rule, the expression, “concerned State” means the State in respect of which the Authority passes an order.</p></content>
</article>
</body></act></akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2019-06-28"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")
    event = {
        "event_id": "evt_cbic_b8c61a1dadce7286",
        "operation": "SPLICE",
        "status": "needs_review",
        "source": {
            "document_id": "/in/union/notifications/cbic/central-tax/2019/31-2019",
            "publication_date": "2019-06-28",
            "record_id": "1000684",
        },
        "legal_time": {"applicability_start": "2019-06-28"},
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": "/in/union/rules/cgst-rules-2017/rule/132/subrule/3",
            "anchor_text": "the expression, “concerned State” means the State",
        },
        "payload": {
            "insert_text": "or Union Territory",
            "position": "after",
        },
        "evidence": {"source_span": {"start": 9433, "end": 9593, "text_hash": "rule133-31-2019"}},
        "review": {"required": True, "review_reasons": ["context_recovered_target_pending_validation"]},
    }
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(json.dumps(event) + "\n", encoding="utf-8")

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["coverage_gap_count"] == 0
    versions = [
        json.loads(line)
        for line in (tmp_path / "out" / "node_versions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    rule_133_versions = [
        row for row in versions if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/133"
    ]
    assert rule_133_versions[-1]["created_by_event_id"] == "evt_cbic_b8c61a1dadce7286"
    assert "means the State or Union Territory in respect" in rule_133_versions[-1]["text"]


def test_materializer_repairs_rule_22_retarget_from_rule_21a_reference(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso><act><body>
<article refersTo="/in/union/rules/cgst-rules-2017/rule/21a">
<num>21A</num><heading>Suspension of registration</heading><content><p>21A. Suspension of registration.</p></content>
</article>
<article refersTo="/in/union/rules/cgst-rules-2017/rule/22">
<num>22</num><heading>Cancellation of registration</heading><content><p>22. Cancellation of registration. (3) The proper officer may cancel registration on the date of the reply to the show cause issued under sub-rule (1), cancel the registration. (4) Where the reply furnished under sub-rule (2) is found to be satisfactory, the proper officer shall drop the proceedings.</p></content>
</article>
<paragraph refersTo="/in/union/rules/cgst-rules-2017/rule/22/subrule/3">
<num>(3)</num><content><p>(3) The proper officer may cancel registration on the date of the reply to the show cause issued under sub-rule (1), cancel the registration.</p></content>
</paragraph>
<paragraph refersTo="/in/union/rules/cgst-rules-2017/rule/22/subrule/4">
<num>(4)</num><content><p>(4) Where the reply furnished under sub-rule (2) is found to be satisfactory, the proper officer shall drop the proceedings.</p></content>
</paragraph>
</body></act></akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2020-12-22"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")
    events = [
        {
            "event_id": "evt_cbic_692add868193fdca",
            "operation": "SPLICE",
            "status": "needs_review",
            "source": {"document_id": "/in/union/notifications/cbic/central-tax/2020/94-2020"},
            "legal_time": {"applicability_start": "2020-12-22"},
            "target": {
                "work_id": "/in/union/rules/cgst-rules-2017",
                "component_id": "/in/union/rules/cgst-rules-2017/rule/21a/subrule/3",
                "anchor_text": "the show cause issued under sub- rule (1)",
            },
            "payload": {"insert_text": "or under sub-rule (2A) of rule 21A", "position": "after"},
            "evidence": {"source_span": {"start": 7183, "end": 7382, "text_hash": "rule22-3"}},
            "review": {"required": True, "review_reasons": ["context_recovered_target_pending_validation"]},
        },
        {
            "event_id": "evt_cbic_bbb03703eb06d775",
            "operation": "SPLICE",
            "status": "validated",
            "source": {"document_id": "/in/union/notifications/cbic/central-tax/2020/94-2020"},
            "legal_time": {"applicability_start": "2020-12-22"},
            "target": {
                "work_id": "/in/union/rules/cgst-rules-2017",
                "component_id": "/in/union/rules/cgst-rules-2017/rule/21a/subrule/4",
                "anchor_text": "reply furnished under sub-rule (2)",
            },
            "payload": {
                "insert_text": "or in response to the notice issued under sub-rule (2A) of rule 21A",
                "position": "after",
            },
            "evidence": {"source_span": {"start": 7383, "end": 7608, "text_hash": "rule22-4"}},
            "review": {"required": False, "review_reasons": []},
        },
    ]
    events_path = tmp_path / "events.jsonl"
    events_path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["coverage_gap_count"] == 0
    versions = [
        json.loads(line)
        for line in (tmp_path / "out" / "node_versions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    by_component = {}
    for version in versions:
        by_component.setdefault(version["component_id"], []).append(version)
    subrule_3 = by_component["/in/union/rules/cgst-rules-2017/rule/22/subrule/3"][-1]["text"]
    subrule_4 = by_component["/in/union/rules/cgst-rules-2017/rule/22/subrule/4"][-1]["text"]
    assert "show cause issued under sub-rule (1) or under sub-rule (2A) of rule 21A" in subrule_3
    assert "reply furnished under sub-rule (2) or in response to the notice issued" in subrule_4


def test_materializer_repairs_rule_21a_subrule_3_retarget_from_rule_8_context(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso><act><body>
<article refersTo="/in/union/rules/cgst-rules-2017/rule/8">
<num>8</num><heading>Application for registration</heading><content><p>8. Rule 8 text.</p></content>
</article>
<article refersTo="/in/union/rules/cgst-rules-2017/rule/21a">
<num>21A</num><heading>Suspension of registration</heading><content><p>21A. Suspension. (3) A registered person, whose registration has been suspended under sub-rule (1) or sub-rule (2), shall not make any taxable supply during the period of suspension.</p></content>
</article>
<paragraph refersTo="/in/union/rules/cgst-rules-2017/rule/21a/subrule/3">
<num>(3)</num><content><p>(3) A registered person, whose registration has been suspended under sub-rule (1) or sub-rule (2), shall not make any taxable supply during the period of suspension.</p></content>
</paragraph>
</body></act></akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2020-12-22"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")
    event = {
        "event_id": "evt_cbic_863edab50c37895d",
        "operation": "SPLICE",
        "status": "needs_review",
        "source": {"document_id": "/in/union/notifications/cbic/central-tax/2020/94-2020"},
        "legal_time": {"applicability_start": "2020-12-22"},
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": "/in/union/rules/cgst-rules-2017/rule/8/subrule/3",
            "anchor_text": "or sub-rule (2)",
        },
        "payload": {"insert_text": "or sub-rule (2A)", "position": "after"},
        "evidence": {"source_span": {"start": 6310, "end": 6463, "text_hash": "rule21a-3"}},
        "review": {"required": True, "review_reasons": ["context_recovered_target_pending_validation"]},
    }
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(json.dumps(event) + "\n", encoding="utf-8")

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["coverage_gap_count"] == 0
    versions = [
        json.loads(line)
        for line in (tmp_path / "out" / "node_versions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    rule_21a_versions = [
        row for row in versions if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/21a"
    ]
    assert rule_21a_versions[-1]["created_by_event_id"] == "evt_cbic_863edab50c37895d"
    assert "sub-rule (1) or sub-rule (2) or sub-rule (2A)" in rule_21a_versions[-1]["text"]


def test_materializer_repairs_rule_159_subrule_3_and_routes_drc23_form_row(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso><act><body>
<article refersTo="/in/union/rules/cgst-rules-2017/rule/159">
<num>159</num><heading>Provisional attachment of property</heading><content><p>159. Provisional attachment of property. (3) Where the property attached is of perishable or hazardous nature, and if the person, whose property has been attached pays an amount equivalent to the market price of such property or the amount that is or may become payable by the taxable person, whichever is lower, then such property shall be released forthwith.</p></content>
</article>
<paragraph refersTo="/in/union/rules/cgst-rules-2017/rule/159/subrule/3">
<num>(3)</num><content><p>(3) Where the property attached is of perishable or hazardous nature, and if the person, whose property has been attached pays an amount equivalent to the market price of such property or the amount that is or may become payable by the taxable person, whichever is lower, then such property shall be released forthwith.</p></content>
</paragraph>
</body></act></akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2022-01-01"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")
    events = [
        {
            "event_id": "evt_cbic_1719ebffce519de9",
            "operation": "SUBSTITUTE",
            "status": "needs_review",
            "source": {"document_id": "/in/union/notifications/cbic/central-tax/2021/40-2021"},
            "legal_time": {"applicability_start": "2022-01-01"},
            "target": {
                "work_id": "/in/union/rules/cgst-rules-2017",
                "component_id": "/in/union/rules/cgst-rules-2017/rule/159",
            },
            "payload": {"old_text": "by the taxable person", "new_text": "by such person"},
            "evidence": {"source_span": {"start": 8930, "end": 9022, "text_hash": "rule159-3"}},
            "review": {"required": True, "review_reasons": ["context_recovered_target_pending_validation"]},
        },
        {
            "event_id": "evt_cbic_9067858b95db5341",
            "operation": "SUBSTITUTE",
            "status": "needs_review",
            "source": {"document_id": "/in/union/notifications/cbic/central-tax/2021/40-2021"},
            "legal_time": {"applicability_start": "2022-01-01"},
            "target": {
                "work_id": "/in/union/rules/cgst-rules-2017",
                "component_id": "/in/union/rules/cgst-rules-2017/rule/159",
            },
            "payload": {
                "old_text": "proceedings pending against the defaulting person which warrants the",
                "new_text": "requirement of",
            },
            "evidence": {
                "excerpt": "in FORM GST DRC-23, for the words proceedings pending against the defaulting person",
                "source_span": {"start": 14072, "end": 14241, "text_hash": "drc23-form"},
            },
            "review": {"required": True, "review_reasons": ["context_recovered_target_pending_validation"]},
        },
    ]
    events_path = tmp_path / "events.jsonl"
    events_path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["coverage_gap_count"] == 0
    assert manifest["forms_lane_pending_baseline_count"] == 1
    assert manifest["forms_lane_pending_baseline_events"][0]["event_id"] == "evt_cbic_9067858b95db5341"
    versions = [
        json.loads(line)
        for line in (tmp_path / "out" / "node_versions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    subrule_3_versions = [
        row for row in versions if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/159/subrule/3"
    ]
    assert subrule_3_versions[-1]["created_by_event_id"] == "evt_cbic_1719ebffce519de9"
    assert "may become payable by such person, whichever is lower" in subrule_3_versions[-1]["text"]


def test_materializer_repairs_rule_28_subrule_2_insertion(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso><act><body>
<article refersTo="/in/union/rules/cgst-rules-2017/rule/28">
<num>28</num><heading>Value of supply of goods or services or both between distinct or related persons</heading><content><p>28. Value of supply of goods or services or both between distinct or related persons, other than through an agent.- The value shall be determined under clauses (a), (b), and (c).</p></content>
</article>
</body></act></akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2023-10-26"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")
    events = [
        {
            "event_id": "evt_cbic_9e14c73c00cff0ac",
            "operation": "INSERT_CHILD",
            "status": "needs_review",
            "source": {"document_id": "/in/union/notifications/cbic/central-tax/2023/52-2023"},
            "legal_time": {"applicability_start": "2023-10-26"},
            "target": {
                "work_id": "/in/union/rules/cgst-rules-2017",
                "component_id": "/in/union/rules/cgst-rules-2017/rule/28/subrule/2",
                "anchor_component_id": "/in/union/rules/cgst-rules-2017/rule/28",
                "anchor_text": "rule 28",
            },
            "payload": {
                "parent_component_id": "/in/union/rules/cgst-rules-2017/rule/28",
                "label": "2",
                "node_type": "subrule",
                "content": "In the Central Goods and Services Tax Rules, 2017, rule 28",
                "triage_lane": "context_unresolved",
            },
            "evidence": {"source_span": {"start": 842, "end": 1468, "text_hash": "rule28-2"}},
            "review": {"required": True, "review_reasons": ["context_unresolved"]},
        },
        {
            "event_id": "evt_cbic_a3f6664ed9134482",
            "operation": "SPLICE",
            "status": "needs_review",
            "source": {"document_id": "/in/union/notifications/cbic/central-tax/2024/12-2024"},
            "legal_time": {
                "applicability_start": "2023-10-26",
                "commencement_date": "2023-10-26",
                "date_basis": "source_effective_date_context",
            },
            "target": {
                "work_id": "/in/union/rules/cgst-rules-2017",
                "component_id": "/in/union/rules/cgst-rules-2017/rule/21a/subrule/2",
                "anchor_text": "who is a related person",
            },
            "payload": {
                "insert_text": "located in India",
                "position": "after",
                "triage_lane": "context_unresolved",
            },
            "evidence": {"source_span": {"start": 2684, "end": 2801, "text_hash": "rule28-located-india"}},
            "review": {"required": True, "review_reasons": ["context_recovered_target_pending_validation"]},
        },
    ]
    events_path = tmp_path / "events.jsonl"
    events_path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["coverage_gap_count"] == 0
    versions = [
        json.loads(line)
        for line in (tmp_path / "out" / "node_versions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    subrule_2_versions = [
        row for row in versions if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/28/subrule/2"
    ]
    assert subrule_2_versions[-2]["created_by_event_id"] == "evt_cbic_9e14c73c00cff0ac"
    assert subrule_2_versions[-1]["created_by_event_id"] == "evt_cbic_a3f6664ed9134482"
    assert "recipient who is a related person located in India" in subrule_2_versions[-1]["text"]
    assert "one per cent of the amount of such guarantee offered" in subrule_2_versions[-1]["text"]


def test_materializer_repairs_rule_8_subrule_4b_insert_then_retrospective_substitution(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso><act><body>
<article refersTo="/in/union/rules/cgst-rules-2017/rule/8">
<num>8</num><heading>Application for registration</heading><content><p>8. Application for registration. (4A) Existing Aadhaar authentication rule.</p></content>
<subrule refersTo="/in/union/rules/cgst-rules-2017/rule/8/subrule/4a"><num>4A</num><content><p>(4A) Existing Aadhaar authentication rule.</p></content></subrule>
</article>
</body></act></akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2022-12-26"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")
    events = [
        {
            "event_id": "evt_cbic_7f8fd053493ca8f1",
            "operation": "INSERT_CHILD",
            "status": "validated",
            "source": {
                "document_id": "/in/union/notifications/cbic/central-tax/2022/26-2022",
                "publication_date": "2022-12-26",
            },
            "legal_time": {"applicability_start": "2022-12-26"},
            "target": {
                "work_id": "/in/union/rules/cgst-rules-2017",
                "component_id": "/in/union/rules/cgst-rules-2017/rule/8/subrule/4b",
                "anchor_component_id": "/in/union/rules/cgst-rules-2017/rule/8/subrule/4a",
            },
            "payload": {
                "parent_component_id": "/in/union/rules/cgst-rules-2017/rule/8",
                "anchor_component_id": "/in/union/rules/cgst-rules-2017/rule/8/subrule/4a",
                "label": "4B",
                "node_type": "subrule",
                "position": "after",
                "content": (
                    "The Central Government may, on the recommendations of the Council, by "
                    "notification specify the States or Union territories wherein the provisions "
                    "of sub-rule (4A) shall not apply"
                ),
            },
            "evidence": {"source_span": {"start": 2304, "end": 2567, "text_hash": "rule8-4b"}},
            "validation": {"materializable": True},
        },
        {
            "event_id": "evt_cbic_ad72e292d9f041d1",
            "operation": "SUBSTITUTE",
            "status": "validated",
            "source": {
                "document_id": "/in/union/notifications/cbic/central-tax/2023/4-2023",
                "publication_date": "2023-03-31",
            },
            "legal_time": {
                "applicability_start": "2022-12-26",
                "commencement_date": "2022-12-26",
                "retrospective": True,
            },
            "target": {
                "work_id": "/in/union/rules/cgst-rules-2017",
                "component_id": "/in/union/rules/cgst-rules-2017/rule/8/subrule/4b",
                "anchor_text": "provisions of",
            },
            "payload": {
                "old_text": "provisions of",
                "new_text": "proviso to",
                "triage_lane": "forms_lane_pending_baseline",
                "forms_lane_pending_baseline": True,
            },
            "evidence": {"source_span": {"start": 2298, "end": 2856, "text_hash": "rule8-4b-correction"}},
            "validation": {"materializable": True},
        },
    ]
    events_path = tmp_path / "events.jsonl"
    events_path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["coverage_gap_count"] == 0
    versions = [
        json.loads(line)
        for line in (tmp_path / "out" / "node_versions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    subrule_4b_versions = [
        row for row in versions if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/8/subrule/4b"
    ]
    assert subrule_4b_versions[-2]["created_by_event_id"] == "evt_cbic_7f8fd053493ca8f1"
    assert subrule_4b_versions[-1]["created_by_event_id"] == "evt_cbic_ad72e292d9f041d1"
    assert "wherein the provisions of sub-rule (4A) shall not apply" in subrule_4b_versions[-2]["text"]
    assert "wherein the proviso to sub-rule (4A) shall not apply" in subrule_4b_versions[-1]["text"]


def test_materializer_repairs_canonical_xml_whole_rule_omit_targets(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso><act><body>
<article refersTo="/in/union/rules/cgst-rules-2017/rule/69"><num>69</num><heading>Matching of claim of input tax credit</heading><content><p>69. Matching text.</p></content></article>
<article refersTo="/in/union/rules/cgst-rules-2017/rule/70"><num>70</num><heading>Final acceptance</heading><content><p>70. Acceptance text.</p></content></article>
<article refersTo="/in/union/rules/cgst-rules-2017/rule/38"><num>38</num><heading>Claim of credit</heading><content><p>38. Rule 38 text.</p></content></article>
</body></act></akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2022-10-01"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")
    common = {
        "event_type": "TEXTUAL_AMENDMENT",
        "operation": "OMIT",
        "status": "validated",
        "source": {
            "document_id": "/in/union/notifications/cbic/central-tax/2022/19-2022-central-tax",
            "publication_date": "2022-09-28",
        },
        "legal_time": {
            "applicability_start": "2022-10-01",
            "commencement_date": "2022-10-01",
            "date_basis": "publication_date_general_commencement_clause",
        },
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": "/in/union/rules/cgst-rules-2017/rule/38",
        },
        "evidence": {
            "excerpt": "rules 69, 70, 71, 72, 73, 74, 75, 76, 77 and 79 of the said rules shall be omitted",
            "source_span": {"start": 3460, "end": 4362, "text_hash": "xml-omit"},
        },
        "validation": {"materializable": True},
    }
    events = [
        {
            **common,
            "event_id": "evt_cbic_xml_606b88a02e3bbff7",
            "payload": {"whole_component": True, "omitted_label": "69"},
        },
        {
            **common,
            "event_id": "evt_cbic_xml_c78413ee5d3d34b8",
            "payload": {"whole_component": True, "omitted_label": "70"},
        },
    ]
    events_path = tmp_path / "events.jsonl"
    events_path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["coverage_gap_count"] == 0
    versions = [
        json.loads(line)
        for line in (tmp_path / "out" / "node_versions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    omitted = {
        row["component_id"]: row
        for row in versions
        if row["created_by_event_id"] in {"evt_cbic_xml_606b88a02e3bbff7", "evt_cbic_xml_c78413ee5d3d34b8"}
    }
    assert "[Omitted]" in omitted["/in/union/rules/cgst-rules-2017/rule/69"]["text"]
    assert "[Omitted]" in omitted["/in/union/rules/cgst-rules-2017/rule/70"]["text"]
    assert "/in/union/rules/cgst-rules-2017/rule/38" not in omitted


def test_materializer_repairs_rule_38_2022_compound_form_amendments(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso><act><body>
<article refersTo="/in/union/rules/cgst-rules-2017/rule/38">
<num>38</num><heading>Claim of credit by a banking company or a financial institution</heading>
<content><p>38. Claim of credit by a banking company or a financial institution.- A banking company or a financial institution, including a non-banking financial company, engaged in the supply of services by way of accepting deposits or extending loans or advances that chooses not to comply with the provisions of sub-section (2) of section 17, in accordance with the option permitted under sub-section (4) of that section, shall follow the following procedure, namely,- (a) the said company or institution shall not avail the credit of,- (i) the tax paid on inputs and input services that are used for non-business purposes; and (ii) the credit attributable to the supplies specified in sub-section (5) of section 17, in FORM GSTR-2; (b) the said company or institution shall avail the credit of tax paid on inputs and input services referred to in the second proviso to sub-section (4) of section 17 and not covered under clause (a); (c) fifty per cent. of the remaining amount of input tax shall be the input tax credit admissible to the company or the institution and shall be furnished in FORM GSTR- 2; (d) the amount referred to in clauses (b) and (c) shall, subject to the provisions of sections 41, 42 and 43, be credited to the electronic credit ledger of the said company or the institution.</p></content>
</article>
</body></act></akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2022-09-27"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")
    common = {
        "event_type": "TEXTUAL_AMENDMENT",
        "status": "needs_review",
        "source": {
            "document_id": "/in/union/notifications/cbic/central-tax/2022/19-2022",
            "publication_date": "2022-09-28",
        },
        "legal_time": {
            "applicability_start": "2022-10-01",
            "commencement_date": "2022-10-01",
            "date_basis": "publication_date_general_commencement_clause",
        },
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": "/in/union/rules/cgst-rules-2017",
        },
        "review": {"required": True, "review_reasons": ["forms_lane_pending_baseline"]},
        "validation": {"materializable": False},
    }
    events = [
        {
            **common,
            "event_id": "evt_cbic_1e72d7a4793d3a73",
            "operation": "OMIT",
            "payload": {
                "omit_text": "in FORM GSTR-2",
                "triage_lane": "forms_lane_pending_baseline",
                "forms_lane_pending_baseline": True,
            },
            "evidence": {
                "excerpt": "in clause (a), in sub-clause (ii), the word, letters and figure, \"in FORM GSTR-2\" shall be omitted",
                "source_span": {"start": 1000, "end": 1100, "text_hash": "rule38-a-ii"},
            },
        },
        {
            **common,
            "event_id": "evt_cbic_5d057e002ca2576d",
            "operation": "UNKNOWN",
            "payload": {
                "triage_lane": "forms_lane_pending_baseline",
                "forms_lane_pending_baseline": True,
            },
            "evidence": {
                "excerpt": "in clause (c), for the words ... shall be substituted; (c) clause (d) shall be omitted",
                "source_span": {"start": 1101, "end": 1300, "text_hash": "rule38-c-d"},
            },
        },
    ]
    events_path = tmp_path / "events.jsonl"
    events_path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["coverage_gap_count"] == 0
    assert manifest["applied_count"] == 2
    versions = [
        json.loads(line)
        for line in (tmp_path / "out" / "node_versions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    rule_38_versions = [
        row for row in versions if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/38"
    ]
    final_text = rule_38_versions[-1]["text"]
    assert "in FORM GSTR-2" not in final_text
    assert "shall be furnished in FORM GSTR" not in final_text
    assert "sections 41, 42 and 43" not in final_text
    assert "balance amount of input tax credit shall be reversed in FORM GSTR-3B" in final_text
    assert "(d) [Omitted]" in final_text


def test_materializer_repairs_rule_96c_sibling_insertion(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso><act><body>
<article refersTo="/in/union/rules/cgst-rules-2017/rule/96b">
<num>96B</num><heading>Recovery of refund</heading><content><p>96B. Recovery of refund of unutilised input tax credit.</p></content>
</article>
</body></act></akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2026-05-18"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")
    events = [
        {
            "event_id": "evt_cbic_f88ac7cd3d0ab081",
            "operation": "INSERT_SIBLING",
            "status": "needs_review",
            "source": {"document_id": "/in/union/notifications/cbic/central-tax/2021/35-2021"},
            "legal_time": {"applicability_start": "2026-05-19"},
            "target": {
                "work_id": "/in/union/rules/cgst-rules-2017",
                "component_id": "/in/union/rules/cgst-rules-2017/rule/96B",
                "anchor_component_id": None,
                "anchor_text": "After rule 96B of the said rules",
            },
            "payload": {
                "anchor_component_id": "/in/union/rules/cgst-rules-2017/rule/96B",
                "content": "After rule 96B of the said rules",
                "label": "Rule 96C",
                "node_type": "rule",
                "position": "after",
                "triage_lane": "context_unresolved",
            },
            "evidence": {"source_span": {"start": 5704, "end": 6849, "text_hash": "rule96c"}},
            "review": {"required": True, "review_reasons": ["context_unresolved"]},
        },
    ]
    events_path = tmp_path / "events.jsonl"
    events_path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["coverage_gap_count"] == 0
    versions = [
        json.loads(line)
        for line in (tmp_path / "out" / "node_versions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    rule_96c_versions = [
        row for row in versions if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/96c"
    ]
    assert rule_96c_versions[-1]["created_by_event_id"] == "evt_cbic_f88ac7cd3d0ab081"
    assert "Bank Account for credit of refund" in rule_96c_versions[-1]["text"]
    assert "Permanent Account Number of the proprietor" in rule_96c_versions[-1]["text"]


def test_materializer_repairs_rule_95_subrule_3_clause_a_substitution(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso><act><body>
<article refersTo="/in/union/rules/cgst-rules-2017/rule/95">
<num>95</num><heading>Refund of tax to certain persons</heading>
<paragraph refersTo="/in/union/rules/cgst-rules-2017/rule/95/subrule/3">
<num>(3)</num><content><p>(3) The refund of tax paid by the applicant shall be available if- (a) the inward supplies of goods or services or both were received from a registered person against a tax invoice and the price of the supply covered under a single tax invoice exceeds five thousand rupees, excluding tax paid, if any; (b) name and Goods and Services Tax Identification Number or Unique Identity Number of the applicant is mentioned in the tax invoice; and (c) such other restrictions or conditions as may be specified in the notification are satisfied.</p></content>
</paragraph>
</article>
</body></act></akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-06-19"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")
    events = [
        {
            "event_id": "evt_cbic_ef4c47389a681bf3",
            "operation": "SUBSTITUTE",
            "status": "needs_review",
            "source": {"document_id": "/in/union/notifications/cbic/central-tax/2018/26-2018"},
            "legal_time": {"applicability_start": "2018-06-13"},
            "target": {
                "work_id": "/in/union/rules/cgst-rules-2017",
                "component_id": "/in/union/rules/cgst-rules-2017/rule/95/subrule/3",
                "anchor_text": "clause (a)",
            },
            "payload": {
                "old_text": "clause (a)",
                "new_text": None,
                "context_recovered_target": True,
                "triage_lane": "context_unresolved",
            },
            "evidence": {"source_span": {"start": 2094, "end": 2224, "text_hash": "rule95-3-a"}},
            "review": {"required": True, "review_reasons": ["context_unresolved"]},
        },
    ]
    events_path = tmp_path / "events.jsonl"
    events_path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["coverage_gap_count"] == 0
    versions = [
        json.loads(line)
        for line in (tmp_path / "out" / "node_versions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    subrule_versions = [
        row for row in versions if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/95/subrule/3"
    ]
    assert subrule_versions[-1]["created_by_event_id"] == "evt_cbic_ef4c47389a681bf3"
    assert "received from a registered person against a tax invoice; (b) name" in subrule_versions[-1]["text"]
    assert "single tax invoice exceeds five thousand rupees" not in subrule_versions[-1]["text"]


def test_materializer_repairs_rule_60_whole_rule_then_subrule_7_substitution(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso><act><body>
<article refersTo="/in/union/rules/cgst-rules-2017/rule/60">
<num>60</num><heading>Form and manner of furnishing details of inward supplies</heading>
<content><p>60. Form and manner of furnishing details of inward supplies.- (7) The details of tax collected at source furnished by an e-commerce operator under section 52 in FORM GSTR-8 shall be made available to the concerned person in Part C of FORM GSTR 2A electronically through the common portal and such person may include the same in FORM GSTR-2.</p></content>
<paragraph refersTo="/in/union/rules/cgst-rules-2017/rule/60/subrule/7">
<num>(7)</num><content><p>(7) The details of tax collected at source furnished by an e-commerce operator under section 52 in FORM GSTR-8 shall be made available to the concerned person in Part C of FORM GSTR 2A electronically through the common portal and such person may include the same in FORM GSTR-2.</p></content>
</paragraph>
</article>
</body></act></akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-06-19"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")
    events = [
        {
            "event_id": "evt_cbic_c0defaa1ae2acdcb",
            "operation": "SUBSTITUTE",
            "status": "needs_review",
            "source": {"document_id": "/in/union/notifications/cbic/central-tax/2020/82-2020"},
            "legal_time": {"applicability_start": "2021-01-01", "commencement_date": "2021-01-01"},
            "target": {
                "work_id": "/in/union/rules/cgst-rules-2017",
                "component_id": "/in/union/rules/cgst-rules-2017/rule/60",
                "anchor_text": "rule 60",
            },
            "payload": {
                "label": "60",
                "node_type": "rule",
                "structural_text": (
                    "60. Form and manner of ascertaining details of inward supplies.- "
                    "(7) An auto-drafted statement containing the details of input tax "
                    "credit shall be made available to the registered person in FORM "
                    "GSTR-2B, for every month, electronically through the common portal."
                ),
                "forms_lane_pending_baseline": True,
                "triage_lane": "forms_lane_pending_baseline",
            },
            "evidence": {"source_span": {"start": 10, "end": 20, "text_hash": "rule60-whole"}},
            "validation": {
                "target_resolved": True,
                "anchor_resolved": True,
                "date_resolved": True,
                "source_span_verified": True,
                "materializable": False,
            },
            "review": {"required": True, "review_reasons": ["forms_lane_pending_baseline"]},
        },
        {
            "event_id": "evt_cbic_4ae2bad66449c4f4",
            "operation": "SUBSTITUTE",
            "status": "validated",
            "source": {"document_id": "/in/union/notifications/cbic/central-tax/2022/19-2022"},
            "legal_time": {"applicability_start": "2022-10-01", "commencement_date": "2022-10-01"},
            "target": {
                "work_id": "/in/union/rules/cgst-rules-2017",
                "component_id": "/in/union/rules/cgst-rules-2017/rule/60/subrule/7",
                "anchor_text": "auto-drafted",
            },
            "payload": {"old_text": "auto-drafted", "new_text": "auto-generated"},
            "evidence": {"source_span": {"start": 30, "end": 40, "text_hash": "rule60-subrule7"}},
            "validation": {
                "target_resolved": True,
                "anchor_resolved": True,
                "date_resolved": True,
                "source_span_verified": True,
                "materializable": True,
            },
            "review": {"required": False, "review_reasons": []},
        },
    ]
    events_path = tmp_path / "events.jsonl"
    events_path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["coverage_gap_count"] == 0
    assert {row["event_id"] for row in manifest["applied_events"]} == {
        "evt_cbic_c0defaa1ae2acdcb",
        "evt_cbic_c0defaa1ae2acdcb_rule60_7_replace",
        "evt_cbic_4ae2bad66449c4f4",
    }
    versions = [
        json.loads(line)
        for line in (tmp_path / "out" / "node_versions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    subrule_versions = [
        row for row in versions if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/60/subrule/7"
    ]
    assert subrule_versions[-1]["created_by_event_id"] == "evt_cbic_4ae2bad66449c4f4"
    assert "auto-generated statement containing the details of input tax credit" in subrule_versions[-1]["text"]
    assert "auto-drafted" not in subrule_versions[-1]["text"]


def test_materializer_repairs_rule_86_subrule_4b_insert_then_omit(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso><act><body>
<article refersTo="/in/union/rules/cgst-rules-2017/rule/86">
<num>86</num><heading>Electronic Credit Ledger</heading>
<content><p>86. Electronic Credit Ledger.- (4) If the refund so filed is rejected, either fully or partly, the amount debited under sub-rule (3), to the extent of rejection, shall be re-credited to the electronic credit ledger.</p></content>
</article>
</body></act></akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-06-19"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")
    events = [
        {
            "event_id": "evt_cbic_53eef1a4d4e92613",
            "operation": "UNKNOWN",
            "status": "needs_review",
            "source": {"document_id": "/in/union/notifications/cbic/central-tax/2022/14-2022"},
            "legal_time": {"applicability_start": "2022-07-05", "commencement_date": "2022-07-05"},
            "target": {
                "work_id": "/in/union/rules/cgst-rules-2017",
                "component_id": "/in/union/forms/gst-pmt-03a",
            },
            "payload": {
                "forms_lane_pending_baseline": True,
                "triage_lane": "forms_lane_pending_baseline",
                "text": (
                    "5. In the said rules, in rule 86, after sub-rule (4A), the following "
                    "sub-rule shall be inserted, namely: - \"(4B) Where a registered person "
                    "deposits the amount of erroneous refund sanctioned to him, - (a) under "
                    "sub-section (3) of section 54 of the Act, or (b) under sub-rule (3) of "
                    "rule 96, in contravention of sub-rule (10) of rule 96, along with "
                    "interest and penalty, wherever applicable, through FORM GST DRC-03...\""
                ),
            },
            "evidence": {"source_span": {"start": 50, "end": 60, "text_hash": "rule86-4b"}},
            "validation": {
                "target_resolved": True,
                "anchor_resolved": True,
                "date_resolved": True,
                "source_span_verified": True,
                "materializable": False,
            },
            "review": {"required": True, "review_reasons": ["forms_lane_pending_baseline"]},
        },
        {
            "event_id": "evt_cbic_71a544230507fb3f",
            "operation": "OMIT",
            "status": "validated",
            "source": {"document_id": "/in/union/notifications/cbic/central-tax/2024/20-2024"},
            "legal_time": {"applicability_start": "2024-10-08", "commencement_date": "2024-10-08"},
            "target": {
                "work_id": "/in/union/rules/cgst-rules-2017",
                "component_id": "/in/union/rules/cgst-rules-2017/rule/86/subrule/4b",
                "anchor_text": "in contravention of sub-rule (10) of rule 96,",
            },
            "payload": {
                "omit_text": "in contravention of sub-rule (10) of rule 96,",
                "whole_component": False,
                "noop_if_already_reflected": True,
            },
            "evidence": {"source_span": {"start": 70, "end": 80, "text_hash": "rule86-4b-omit"}},
            "validation": {
                "target_resolved": True,
                "anchor_resolved": True,
                "date_resolved": True,
                "source_span_verified": True,
                "materializable": True,
            },
            "review": {"required": False, "review_reasons": []},
        },
    ]
    events_path = tmp_path / "events.jsonl"
    events_path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["coverage_gap_count"] == 0
    versions = [
        json.loads(line)
        for line in (tmp_path / "out" / "node_versions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    subrule_versions = [
        row for row in versions if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/86/subrule/4b"
    ]
    assert [row["created_by_event_id"] for row in subrule_versions[-2:]] == [
        "evt_cbic_53eef1a4d4e92613",
        "evt_cbic_71a544230507fb3f",
    ]
    assert "under sub-rule (3) of rule 96, along with interest" in subrule_versions[-1]["text"]
    assert "in contravention of sub-rule (10) of rule 96" not in subrule_versions[-1]["text"]


def test_materializer_repairs_rule_21a_subrule_4_proviso_from_rule_10a_reference(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso><act><body>
<article refersTo="/in/union/rules/cgst-rules-2017/rule/10a">
<num>10A</num><heading>Furnishing of Bank Account Details</heading><content><p>10A. Furnishing of Bank Account Details.</p></content>
</article>
<article refersTo="/in/union/rules/cgst-rules-2017/rule/21a">
<num>21A</num><heading>Suspension of registration</heading><content><p>21A. Suspension of registration. (3) A registered person, whose registration has been suspended under sub-rule (1) or sub-rule (2) or sub-rule (2A), shall not make any taxable supply during the period of suspension. (4) The suspension of registration under sub-rule (1) or sub-rule (2) shall be deemed to be revoked upon completion of the proceedings by the proper officer under rule 22 and such revocation shall be effective from the date on which the suspension had come into effect. Provided that the suspension may be revoked by the proper officer. Provided further that another proviso already exists. (5) Later text.</p></content>
</article>
</body></act></akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2023-08-04"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")
    event = {
        "event_id": "evt_cbic_8ca0a1168d9339cb",
        "operation": "INSERT_CHILD",
        "status": "needs_review",
        "source": {"document_id": "/in/union/notifications/cbic/central-tax/2023/38-2023"},
        "legal_time": {"applicability_start": "2023-08-04", "commencement_date": "2023-08-04"},
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": "/in/union/rules/cgst-rules-2017/rule/10a/subrule/4/clause/ii",
            "anchor_text": "(ii)",
        },
        "payload": {
            "parent_component_id": "/in/union/rules/cgst-rules-2017/rule/10a/subrule/4",
            "label": "(ii)",
            "node_type": "clause",
            "content": "(ii) in sub-rule (4)",
        },
        "evidence": {"source_span": {"start": 2870, "end": 3291, "text_hash": "rule21a-4-proviso"}},
        "validation": {
            "target_resolved": False,
            "anchor_resolved": False,
            "date_resolved": True,
            "source_span_verified": True,
            "materializable": False,
        },
        "review": {"required": True, "review_reasons": ["context_recovered_target_pending_validation"]},
    }
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(json.dumps(event) + "\n", encoding="utf-8")

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["coverage_gap_count"] == 0
    versions = [
        json.loads(line)
        for line in (tmp_path / "out" / "node_versions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    parent_versions = [
        row for row in versions if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/21a"
    ]
    proviso_versions = [
        row
        for row in versions
        if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/21a/subrule/4/proviso/rule10a-compliance"
    ]
    assert parent_versions[-1]["created_by_event_id"] == "evt_cbic_8ca0a1168d9339cb"
    assert "revoked upon compliance with the provisions of rule 10A" in parent_versions[-1]["text"]
    assert proviso_versions[-1]["text"].startswith("Provided also that where the registration has been suspended")


def test_materializer_repairs_rule_89_subrule_4_omissions_retargeted_from_rule_88d(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso><act><body>
<article refersTo="/in/union/rules/cgst-rules-2017/rule/88d">
<num>88D</num><heading>Difference in ITC</heading><content><p>88D. Rule 88D text.</p></content>
</article>
<article refersTo="/in/union/rules/cgst-rules-2017/rule/89">
<num>89</num><heading>Application for refund</heading>
<content><p>89. Application for refund. (4) Formula text. (B) "Net ITC" means input tax credit availed on inputs and input services during the relevant period; (C) "Turnover of zero-rated supply of goods" means the value of zero-rated supply of goods made during the relevant period without payment of tax under bond or letter of undertaking or the value which is 1.5 times the value of like goods domestically supplied by the same or, similarly placed, supplier, as declared by the supplier, whichever is less, other than the turnover of supplies in respect of which refund is claimed under sub-rules (4A) or (4B) or both; (D) "Turnover of zero-rated supply of services" means services text.</p></content>
<paragraph refersTo="/in/union/rules/cgst-rules-2017/rule/89/subrule/4">
<num>(4)</num><content><p>(4) Formula text. (B) "Net ITC" means input tax credit availed on inputs and input services during the relevant period; (C) "Turnover of zero-rated supply of goods" means the value of zero-rated supply of goods made during the relevant period without payment of tax under bond or letter of undertaking or the value which is 1.5 times the value of like goods domestically supplied by the same or, similarly placed, supplier, as declared by the supplier, whichever is less, other than the turnover of supplies in respect of which refund is claimed under sub-rules (4A) or (4B) or both; (D) "Turnover of zero-rated supply of services" means services text.</p></content>
</paragraph>
</article>
</body></act></akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2024-11-01"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")

    def event(event_id, omit_text):
        return {
            "event_id": event_id,
            "operation": "OMIT",
            "status": "needs_review",
            "source": {"document_id": "/in/union/notifications/cbic/central-tax/2024/20-2024"},
            "legal_time": {"applicability_start": "2024-11-01", "commencement_date": "2024-11-01"},
            "target": {
                "work_id": "/in/union/rules/cgst-rules-2017",
                "component_id": "/in/union/rules/cgst-rules-2017/rule/88d/subrule/3",
                "anchor_text": "Rule 96",
            },
            "payload": {
                "omit_text": omit_text,
                "context_recovered_target": True,
                "context_recovery": {"matched_text": "in rule 88D, in sub-rule (3"},
                "whole_component": False,
            },
            "evidence": {"source_span": {"start": 1, "end": 2, "text_hash": event_id}},
            "review": {"required": True, "review_reasons": ["context_recovered_target_pending_validation"]},
            "validation": {
                "target_resolved": False,
                "anchor_resolved": False,
                "date_resolved": True,
                "source_span_verified": True,
                "materializable": False,
            },
        }

    events = [
        event(
            "evt_cbic_1c649897aa23b16c",
            "other than the input tax credit availed for which refund is claimed under sub-rules (4A) or (4B) or both",
        ),
        event(
            "evt_cbic_9a6b87987cee9078",
            ", other than the turnover of supplies in respect of which refund is claimed under sub- rules (4A) or (4B) or both",
        ),
    ]
    events_path = tmp_path / "events.jsonl"
    events_path.write_text("\n".join(json.dumps(row) for row in events) + "\n", encoding="utf-8")

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["coverage_gap_count"] == 0
    versions = [
        json.loads(line)
        for line in (tmp_path / "out" / "node_versions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    subrule_versions = [
        row for row in versions if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/89/subrule/4"
    ]
    assert [row["created_by_event_id"] for row in subrule_versions[-2:]] == [
        "evt_cbic_1c649897aa23b16c",
        "evt_cbic_9a6b87987cee9078",
    ]
    assert "other than the turnover of supplies in respect of which refund is claimed" not in subrule_versions[-1]["text"]
    assert '"Turnover of zero-rated supply of services" means services text' in subrule_versions[-1]["text"]


def test_materializer_repairs_rule_83_subrule_8_omit_retargeted_from_rule_89(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso><act><body>
<article refersTo="/in/union/rules/cgst-rules-2017/rule/83">
<num>83</num><heading>Goods and services tax practitioner</heading>
<content><p>83. Provisions relating to a goods and services tax practitioner.</p></content>
<paragraph refersTo="/in/union/rules/cgst-rules-2017/rule/83/subrule/8">
<num>(8)</num><content><p>(8) A goods and services tax practitioner can undertake any or all of the following activities on behalf of a registered person, if so authorised by him to- (a) furnish the details of outward and inward supplies; (b) furnish monthly, quarterly, annual or final return.</p></content>
</paragraph>
</article>
<article refersTo="/in/union/rules/cgst-rules-2017/rule/89">
<num>89</num><heading>Application for refund</heading><content><p>89. Refund text.</p></content>
</article>
</body></act></akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2022-09-30"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")

    event = {
        "event_id": "evt_cbic_8d1e9b596d074bea",
        "operation": "OMIT",
        "status": "validated",
        "source": {"document_id": "/in/union/notifications/cbic/central-tax/2022/19-2022"},
        "legal_time": {"applicability_start": "2022-10-01", "commencement_date": "2022-10-01"},
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": "/in/union/rules/cgst-rules-2017/rule/89/subrule/8",
            "anchor_component_id": "/in/union/rules/cgst-rules-2017/rule/83",
            "anchor_text": "and inward",
            "anchor_occurrence": 1,
        },
        "payload": {
            "omit_text": "and inward",
            "match_occurrence": 1,
            "context_recovered_target": True,
            "whole_component": False,
        },
        "evidence": {
            "excerpt": "10. In rule 83 of the said rules, in sub-rule (8), in clause (a), the words \"and inward\" shall be omitted;",
            "source_span": {"start": 1, "end": 2, "text_hash": "rule83"},
        },
        "review": {"required": False, "review_reasons": []},
        "validation": {
            "target_resolved": True,
            "anchor_resolved": True,
            "date_resolved": True,
            "source_span_verified": True,
            "materializable": True,
        },
    }
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(json.dumps(event) + "\n", encoding="utf-8")

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["coverage_gap_count"] == 0
    versions = [
        json.loads(line)
        for line in (tmp_path / "out" / "node_versions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    subrule_versions = [
        row for row in versions if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/83/subrule/8"
    ]
    assert subrule_versions[-1]["created_by_event_id"] == "evt_cbic_8d1e9b596d074bea"
    assert "outward supplies" in subrule_versions[-1]["text"]
    assert "and inward" not in subrule_versions[-1]["text"]


def test_materializer_repairs_rule_53_subrule_1a_retargeted_from_rule_80(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso><act><body>
<article refersTo="/in/union/rules/cgst-rules-2017/rule/53">
<num>53</num><heading>Revised tax invoice and credit or debit notes</heading>
<content><p>53. Revised tax invoice and credit or debit notes.- (1) A revised tax invoice referred to in section 31 and credit or debit notes referred to in section 34 shall contain the following particulars, namely:- (a) the word "Revised Invoice", wherever applicable, indicated prominently; (b) name, address and Goods and Services Tax Identification Number of the supplier. (2) Later subrule.</p></content>
<paragraph refersTo="/in/union/rules/cgst-rules-2017/rule/53/subrule/1">
<num>(1)</num><content><p>(1) A revised tax invoice referred to in section 31 and credit or debit notes referred to in section 34 shall contain the following particulars, namely:- (a) the word "Revised Invoice", wherever applicable, indicated prominently; (b) name, address and Goods and Services Tax Identification Number of the supplier.</p></content>
</paragraph>
<paragraph refersTo="/in/union/rules/cgst-rules-2017/rule/53/subrule/2">
<num>(2)</num><content><p>(2) Later subrule.</p></content>
</paragraph>
</article>
<article refersTo="/in/union/rules/cgst-rules-2017/rule/80">
<num>80</num><heading>Annual return</heading>
<paragraph refersTo="/in/union/rules/cgst-rules-2017/rule/80/subrule/1"><num>(1)</num><content><p>(1) Rule 80 baseline text.</p></content></paragraph>
<paragraph refersTo="/in/union/rules/cgst-rules-2017/rule/80/subrule/8"><num>(8)</num><content><p>(8) Existing Rule 80 subrule.</p></content></paragraph>
</article>
</body></act></akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-06-19"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")

    event = {
        "event_id": "evt_cbic_2bf46864fcabb9b6",
        "operation": "INSERT_CHILD",
        "status": "validated",
        "source": {"document_id": "/in/union/notifications/cbic/central-tax/2019/3-2019"},
        "legal_time": {"applicability_start": "2019-02-18", "commencement_date": "2019-02-18"},
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": "/in/union/rules/cgst-rules-2017/rule/80/subrule/8",
            "anchor_component_id": "/in/union/rules/cgst-rules-2017/rule/80/subrule/1",
            "anchor_text": "after sub-rule (1)",
            "anchor_occurrence": 1,
        },
        "payload": {
            "label": "8",
            "node_type": "subrule",
            "parent_component_id": "/in/union/rules/cgst-rules-2017/rule/80",
            "content": "A goods and services tax practitioner can undertake any or all activities.",
        },
        "evidence": {
            "excerpt": (
                "10. In the said rules, in rule 53, (d) after sub-rule (1), the following "
                "sub-rule shall be inserted, namely: (1A) A credit or debit note referred "
                "to in section 34 shall contain the following particulars..."
            ),
            "source_span": {"start": 6599, "end": 7819, "text_hash": "rule53-1a"},
        },
        "review": {"required": False, "review_reasons": []},
        "validation": {
            "target_resolved": True,
            "anchor_resolved": True,
            "date_resolved": True,
            "source_span_verified": True,
            "materializable": True,
        },
    }
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(json.dumps(event) + "\n", encoding="utf-8")

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["coverage_gap_count"] == 0
    versions = [
        json.loads(line)
        for line in (tmp_path / "out" / "node_versions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    rule_53_versions = [
        row for row in versions if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/53/subrule/1a"
    ]
    assert rule_53_versions[-1]["created_by_event_id"] == "evt_cbic_2bf46864fcabb9b6"
    assert "credit or debit note referred to in section 34" in rule_53_versions[-1]["text"]
    assert "serial number(s) and date(s) of the corresponding tax invoice(s)" in rule_53_versions[-1]["text"]
    assert not any(
        row["created_by_event_id"] == "evt_cbic_2bf46864fcabb9b6"
        and row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/80/subrule/8"
        for row in versions
    )


def test_materializer_keeps_rule_54_subrule_1a_and_retargets_2024_insert_to_rule_39(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso><act><body>
<article refersTo="/in/union/rules/cgst-rules-2017/rule/54">
<num>54</num><heading>Tax invoice in special cases</heading>
<content><p>54. Tax invoice in special cases. (1) Existing sub-rule one.</p></content>
<paragraph refersTo="/in/union/rules/cgst-rules-2017/rule/54/subrule/1">
<num>(1)</num><content><p>(1) Existing sub-rule one.</p></content>
</paragraph>
</article>
<article refersTo="/in/union/rules/cgst-rules-2017/rule/39">
<num>39</num><heading>Procedure for distribution of input tax credit by Input Service Distributor</heading>
<content><p>39. Procedure. (1) Existing distribution process.</p></content>
<paragraph refersTo="/in/union/rules/cgst-rules-2017/rule/39/subrule/1">
<num>(1)</num><content><p>(1) Existing distribution process.</p></content>
</paragraph>
</article>
</body></act></akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-06-19"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")

    old_insert = {
        "event_id": "evt_cbic_898c3f1e8a122c84",
        "operation": "INSERT_CHILD",
        "status": "validated",
        "source": {"document_id": "/in/union/notifications/cbic/central-tax/2018/3-2018"},
        "legal_time": {"applicability_start": "2018-01-23", "commencement_date": "2018-01-23"},
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": "/in/union/rules/cgst-rules-2017/rule/54/subrule/1a",
            "anchor_component_id": "/in/union/rules/cgst-rules-2017/rule/54/subrule/1",
            "anchor_text": "after sub-rule (1)",
            "anchor_occurrence": 1,
        },
        "payload": {
            "label": "1A",
            "node_type": "subrule",
            "parent_component_id": "/in/union/rules/cgst-rules-2017/rule/54",
            "content": (
                "(a) A registered person, having the same PAN and State code as an "
                "Input Service Distributor, may issue an invoice to transfer the credit "
                "of common input services to the Input Service Distributor, which shall "
                "contain the following details."
            ),
        },
        "evidence": {"source_span": {"start": 1, "end": 2, "text_hash": "old-rule54-1a"}},
        "review": {"required": False, "review_reasons": []},
        "validation": {
            "target_resolved": True,
            "anchor_resolved": True,
            "date_resolved": True,
            "source_span_verified": True,
            "materializable": True,
        },
    }
    new_insert = {
        "event_id": "evt_cbic_92c8303b6b01e1b9",
        "operation": "INSERT_CHILD",
        "status": "validated",
        "source": {"document_id": "/in/union/notifications/cbic/central-tax/2024/12-2024"},
        "legal_time": {"applicability_start": "2024-07-10", "commencement_date": "2024-07-10"},
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": "/in/union/rules/cgst-rules-2017/rule/54/subrule/1a",
            "anchor_component_id": "/in/union/rules/cgst-rules-2017/rule/54/subrule/1",
            "anchor_text": "after sub-rule (1)",
            "anchor_occurrence": 1,
        },
        "payload": {
            "label": "1A",
            "node_type": "subrule",
            "parent_component_id": "/in/union/rules/cgst-rules-2017/rule/54",
            "content": (
                "For the distribution of credit in respect of input services, attributable "
                "to one or more distinct persons, subject to levy of tax under sub-section "
                "(3) or (4) of section 9, a registered person, having the same PAN and "
                "State code as an Input Service Distributor, may issue an invoice."
            ),
        },
        "evidence": {"source_span": {"start": 3, "end": 4, "text_hash": "new-rule54-1a"}},
        "review": {"required": False, "review_reasons": []},
        "validation": {
            "target_resolved": True,
            "anchor_resolved": True,
            "date_resolved": True,
            "source_span_verified": True,
            "materializable": True,
        },
    }
    events_path = tmp_path / "events.jsonl"
    events_path.write_text("\n".join(json.dumps(event) for event in [old_insert, new_insert]) + "\n", encoding="utf-8")

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["coverage_gap_count"] == 0
    versions = [
        json.loads(line)
        for line in (tmp_path / "out" / "node_versions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    subrule_versions = [
        row for row in versions if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/54/subrule/1a"
    ]
    rule39_versions = [
        row for row in versions if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/39/subrule/1a"
    ]
    assert subrule_versions[-1]["created_by_event_id"] == "evt_cbic_898c3f1e8a122c84"
    assert "which shall contain the following details" in subrule_versions[-1]["text"]
    assert rule39_versions[-1]["created_by_event_id"] == "evt_cbic_92c8303b6b01e1b9"
    assert "section 9" in rule39_versions[-1]["text"]


def test_materializer_repairs_rule_54_subrule_2_proviso_from_wrapper_clause(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso><act><body>
<article refersTo="/in/union/rules/cgst-rules-2017/rule/54">
<num>54</num><heading>Tax invoice in special cases</heading>
<content><p>54. Tax invoice in special cases. (2) Existing sub-rule two.</p></content>
<paragraph refersTo="/in/union/rules/cgst-rules-2017/rule/54/subrule/2">
<num>(2)</num><content><p>(2) Existing sub-rule two.</p></content>
</paragraph>
</article>
</body></act></akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-06-19"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")

    event = {
        "event_id": "evt_cbic_7881d05408bb9183",
        "operation": "INSERT_CHILD",
        "status": "needs_review",
        "source": {"document_id": "/in/union/notifications/cbic/central-tax/2018/74-2018"},
        "legal_time": {"applicability_start": "2018-12-31", "commencement_date": "2018-12-31"},
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": "/in/union/rules/cgst-rules-2017/rule/54/subrule/2/clause/a",
            "anchor_component_id": "/in/union/union/cgst-rules-2017",
            "anchor_text": "in sub-rule (2), the following",
        },
        "payload": {
            "label": "a",
            "node_type": "clause",
            "parent_component_id": "/in/union/rules/cgst-rules-2017/rule/54/subrule/2",
            "content": "(a) in sub-rule (2), the following",
        },
        "evidence": {
            "excerpt": (
                "(a) in sub-rule (2), the following proviso shall be inserted, namely:- "
                "Provided that the signature or digital signature of the supplier..."
            ),
            "source_span": {"start": 1, "end": 2, "text_hash": "rule54-2-proviso"},
        },
        "review": {"required": True, "review_reasons": ["anchor_not_resolved"]},
        "validation": {
            "target_resolved": False,
            "anchor_resolved": False,
            "date_resolved": True,
            "source_span_verified": True,
            "materializable": False,
        },
    }
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(json.dumps(event) + "\n", encoding="utf-8")

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["coverage_gap_count"] == 0
    versions = [
        json.loads(line)
        for line in (tmp_path / "out" / "node_versions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    proviso_versions = [
        row
        for row in versions
        if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/54/subrule/2/proviso/no-signature-2018"
    ]
    assert proviso_versions[-1]["created_by_event_id"] == "evt_cbic_7881d05408bb9183"
    assert "signature or digital signature of the supplier" in proviso_versions[-1]["text"]
    assert not any(row["component_id"].endswith("/rule/54/subrule/2/clause/a") for row in versions)


def test_materializer_repairs_rule_83a_subrule_6_from_truncated_parent(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso><act><body>
<article refersTo="/in/union/rules/cgst-rules-2017/rule/83a">
<num>83A</num><heading>Examination of Goods and Services Tax Practitioners</heading>
<content><p>(1) Every person referred to in clause (b) of sub-rule (1) of rule 83 shall pass an examination. (2) The National Academy of Customs, Indirect Taxes and Narcotics</p></content>
</article>
</body></act></akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-06-19"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")

    event = {
        "event_id": "evt_cbic_095e46e40dae412f",
        "operation": "SUBSTITUTE",
        "status": "needs_review",
        "source": {"document_id": "/in/union/notifications/cbic/central-tax/2019/49-2019"},
        "legal_time": {"applicability_start": "2017-07-01", "commencement_date": "2017-07-01"},
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": "/in/union/rules/cgst-rules-2017/rule/83a/subrule/6",
            "anchor_text": "rule 83A, in sub-rule (6)",
        },
        "payload": {"old_text": "rule 83A, in sub-rule (6)", "new_text": None},
        "evidence": {
            "excerpt": (
                "In the said rules, in rule 83A, in sub-rule (6), for clause (i), "
                "the following clause shall be substituted..."
            ),
            "source_span": {"start": 1, "end": 2, "text_hash": "rule83a-6"},
        },
        "review": {"required": True, "review_reasons": ["incomplete_text_edit_payload"]},
        "validation": {
            "target_resolved": False,
            "anchor_resolved": False,
            "date_resolved": True,
            "source_span_verified": True,
            "materializable": False,
        },
    }
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(json.dumps(event) + "\n", encoding="utf-8")

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["coverage_gap_count"] == 0
    versions = [
        json.loads(line)
        for line in (tmp_path / "out" / "node_versions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    subrule_versions = [
        row for row in versions if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/83a/subrule/6"
    ]
    assert subrule_versions[-1]["created_by_event_id"] == "evt_cbic_095e46e40dae412f"
    assert "Every person referred to in clause (b) of sub-rule (1) of rule 83" in subrule_versions[-1]["text"]
    assert "within two years of enrolment" not in subrule_versions[-1]["text"]


def test_materializer_repairs_rule_96a_clause_b_placeholder_payload_from_rule96_prefix(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso><act><body>
<article refersTo="/in/union/rules/cgst-rules-2017/rule/96">
<num>96</num><heading>Refund of integrated tax paid on goods exported out of India</heading>
<content><p>96. Refund. (1) The shipping bill filed by an exporter shall be deemed to be an application for refund only when:- (a) export manifest is filed; and (b) the applicant has furnished a valid return in FORM GSTR-3;</p></content>
</article>
<article refersTo="/in/union/rules/cgst-rules-2017/rule/96a">
<num>96A</num><heading>Export of goods or services under bond or Letter of Undertaking</heading>
<content><p>96A. Export of goods or services under bond or Letter of Undertaking. (1) Any registered person may export under bond within a period of - (a) fifteen days after the expiry of three months or such further period as may be allowed by the Commissioner from the date of issue of the invoice for export, if the goods are not exported out of India; or (b) fifteen days after the expiry of one year, or such further period as may be allowed by the Commissioner, from the date of issue of the invoice for export, if the payment of such services is not received by the exporter in convertible foreign exchange or in Indian rupees, wherever permitted by the Reserve Bank of India.</p></content>
</article>
</body></act></akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-06-19"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")

    event = {
        "event_id": "evt_cbic_fe42a22b62593e55",
        "operation": "SUBSTITUTE",
        "status": "needs_review",
        "source": {"document_id": "/in/union/notifications/cbic/central-tax/2024/12-2024"},
        "legal_time": {"applicability_start": "2024-07-10", "commencement_date": "2024-07-10"},
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": "/in/union/rules/cgst-rules-2017/rule/96/subrule/1",
            "anchor_text": "existing text",
        },
        "payload": {"old_text": "existing text", "new_text": "amended text"},
        "evidence": {
            "excerpt": (
                "in sub-rule (1), for clause (b), the following shall be substituted, "
                "namely:- (b) fifteen days after the expiry of one year..."
            ),
            "source_span": {"start": 1, "end": 2, "text_hash": "rule96-1-b"},
        },
        "review": {"required": True, "review_reasons": ["llm_candidate_not_validated"]},
        "validation": {
            "target_resolved": False,
            "anchor_resolved": False,
            "date_resolved": True,
            "source_span_verified": True,
            "materializable": False,
        },
    }
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(json.dumps(event) + "\n", encoding="utf-8")

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["coverage_gap_count"] == 0
    versions = [
        json.loads(line)
        for line in (tmp_path / "out" / "node_versions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    rule96_versions = [
        row for row in versions if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/96"
    ]
    rule96a_versions = [
        row for row in versions if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/96a"
    ]
    assert len(rule96_versions) == 1
    assert "valid return in FORM GSTR-3" in rule96_versions[-1]["text"]
    assert rule96a_versions[-1]["created_by_event_id"] == "evt_cbic_fe42a22b62593e55"
    assert "Foreign Exchange Management Act, 1999" in rule96a_versions[-1]["text"]
    assert "wherever permitted by the Reserve Bank of India" in rule96a_versions[-1]["text"]


def test_materializer_repairs_rule_96_subrule_10_chain_for_explanation_and_omit(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso><act><body>
<article refersTo="/in/union/rules/cgst-rules-2017/rule/96">
<num>96</num><heading>Refund of integrated tax paid on goods exported out of India</heading>
<content><p>96. Refund of integrated tax paid on goods exported out of India. (1) Existing refund rule text.</p></content>
</article>
</body></act></akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-06-19"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")

    subrule_text = (
        "(10) The persons claiming refund of integrated tax paid on exports of goods or services "
        "should not have - (a) received supplies on which notification benefits have been availed; "
        "or (b) availed the benefit under notification No. 78/2017-Customs or notification "
        "No. 79/2017-Customs except so far it relates to receipt of capital goods by such person "
        "against Export Promotion Capital Goods Scheme."
    )
    events = [
        {
            "event_id": "evt_cbic_f71bca8f1cfeffcf",
            "operation": "SUBSTITUTE",
            "status": "validated",
            "source": {"document_id": "/in/union/notifications/cbic/central-tax/2018/54-2018"},
            "legal_time": {"applicability_start": "2018-10-09", "commencement_date": "2018-10-09"},
            "target": {
                "work_id": "/in/union/rules/cgst-rules-2017",
                "component_id": "/in/union/rules/cgst-rules-2017/rule/96/subrule/10",
            },
            "payload": {
                "structural_text": subrule_text,
                "allow_detached_component_version": True,
                "forms_lane_pending_baseline": True,
                "triage_lane": "forms_lane_pending_baseline",
            },
            "evidence": {"source_span": {"start": 1, "end": 2, "text_hash": "rule96-10-substitute"}},
            "review": {"required": False, "review_reasons": []},
            "validation": {
                "target_resolved": True,
                "anchor_resolved": True,
                "date_resolved": True,
                "source_span_verified": True,
                "materializable": True,
            },
        },
        {
            "event_id": "evt_cbic_b56b32722affd914",
            "operation": "INSERT_CHILD",
            "status": "needs_review",
            "source": {"document_id": "/in/union/notifications/cbic/central-tax/2020/16-2020"},
            "legal_time": {"applicability_start": "2017-10-23", "commencement_date": "2020-03-23"},
            "target": {
                "work_id": "/in/union/rules/cgst-rules-2017",
                "component_id": "/in/union/rules/cgst-rules-2017/rule/96/subrule/10/explanation/explanation",
                "anchor_component_id": "/in/union/rules/cgst-rules-2017/rule/96/subrule/10/clause/b",
            },
            "payload": {
                "label": "Explanation",
                "node_type": "explanation",
                "parent_component_id": "/in/union/rules/cgst-rules-2017/rule/96/subrule/10",
                "content": "For the purpose of this sub-rule, the benefit ... on the goods.",
            },
            "evidence": {"source_span": {"start": 1, "end": 2, "text_hash": "rule96-10-explanation"}},
            "review": {"required": True, "review_reasons": ["anchor_not_resolved"]},
            "validation": {
                "target_resolved": False,
                "anchor_resolved": False,
                "date_resolved": True,
                "source_span_verified": True,
                "materializable": False,
            },
        },
        {
            "event_id": "evt_cbic_44d6be8f142fc2e7",
            "operation": "OMIT",
            "status": "validated",
            "source": {"document_id": "/in/union/notifications/cbic/central-tax/2024/20-2024"},
            "legal_time": {"applicability_start": "2024-10-08", "commencement_date": "2024-10-08"},
            "target": {
                "work_id": "/in/union/rules/cgst-rules-2017",
                "component_id": "/in/union/rules/cgst-rules-2017/rule/96/subrule/10",
            },
            "payload": {
                "whole_component": True,
                "apply_to_parent_subrule_span": True,
                "parent_component_id": "/in/union/rules/cgst-rules-2017/rule/96",
                "label": "10",
            },
            "evidence": {"source_span": {"start": 1, "end": 2, "text_hash": "rule96-10-omit"}},
            "review": {"required": False, "review_reasons": []},
            "validation": {
                "target_resolved": True,
                "anchor_resolved": True,
                "date_resolved": True,
                "source_span_verified": True,
                "materializable": True,
            },
        },
    ]
    events_path = tmp_path / "events.jsonl"
    events_path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["coverage_gap_count"] == 0
    versions = [
        json.loads(line)
        for line in (tmp_path / "out" / "node_versions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    subrule_versions = [
        row for row in versions if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/96/subrule/10"
    ]
    explanation_versions = [
        row
        for row in versions
        if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/96/subrule/10/explanation/explanation"
    ]
    assert subrule_versions[-1]["created_by_event_id"] == "evt_cbic_44d6be8f142fc2e7"
    assert subrule_versions[-1]["text"] == "[Omitted]"
    assert explanation_versions[-1]["created_by_event_id"] == "evt_cbic_b56b32722affd914"
    assert "Compensation Cess on inputs" in explanation_versions[-1]["text"]
    assert "Basic Customs Duty (BCD)" in explanation_versions[-1]["text"]


def test_materializer_repairs_rule_142_section_74a_rows_from_rule96b_context(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso><act><body>
<article refersTo="/in/union/rules/cgst-rules-2017/rule/142">
<num>142</num><heading>Notice and order for demand of amounts payable under the Act</heading>
<content><p>142. Notice and order. (5) A summary of the order issued under sub-section (9) of section 73 or sub-section (9) of section 74 or section 129 shall be uploaded electronically in FORM GST DRC-07.</p></content>
<paragraph refersTo="/in/union/rules/cgst-rules-2017/rule/142/subrule/5">
<num>(5)</num><content><p>(5) A summary of the order issued under sub-section (9) of section 73 or sub-section (9) of section 74 or section 129 shall be uploaded electronically in FORM GST DRC-07.</p></content>
</paragraph>
</article>
</body></act></akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-06-19"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")

    base_event = {
        "operation": "SPLICE",
        "status": "validated",
        "source": {"document_id": "/in/union/notifications/cbic/central-tax/2024/20-2024"},
        "legal_time": {"applicability_start": "2024-11-01", "commencement_date": "2024-11-01"},
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": "/in/union/rules/cgst-rules-2017/rule/96b/subrule/5",
            "anchor_text": "or section 74",
        },
        "payload": {"insert_text": "or section 74A", "position": "after"},
        "evidence": {"source_span": {"start": 1, "end": 2, "text_hash": "rule142-74a"}},
        "review": {"required": False, "review_reasons": []},
        "validation": {
            "target_resolved": True,
            "anchor_resolved": True,
            "date_resolved": True,
            "source_span_verified": True,
            "materializable": True,
        },
    }
    events = [
        {
            **base_event,
            "event_id": "evt_cbic_b51e996e7ee01dcd",
            "evidence": {
                "excerpt": (
                    "(g) in sub-rule (5), after the words and figures “or section 74”, "
                    "the words, figure and letters “or section 74A” shall be inserted."
                ),
                "source_span": {"start": 1, "end": 2, "text_hash": "rule142-5"},
            },
        },
        {
            **base_event,
            "event_id": "evt_cbic_35cfa6b7ce9bd10c",
            "target": {
                **base_event["target"],
                "component_id": "/in/union/rules/cgst-rules-2017/rule/96b/subrule/2b",
            },
            "evidence": {
                "excerpt": (
                    "(d) in sub-rule (2B), after the words and figures “or section 74”, "
                    "the words, figures and letter “or section 74A” shall be inserted;"
                ),
                "source_span": {"start": 1, "end": 2, "text_hash": "rule142-2b"},
            },
        },
    ]
    events_path = tmp_path / "events.jsonl"
    events_path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["coverage_gap_count"] == 0
    assert manifest["context_unresolved_count"] == 1
    versions = [
        json.loads(line)
        for line in (tmp_path / "out" / "node_versions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    subrule_5_versions = [
        row for row in versions if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/142/subrule/5"
    ]
    assert subrule_5_versions[-1]["created_by_event_id"] == "evt_cbic_b51e996e7ee01dcd"
    assert "section 74 or section 74A" in subrule_5_versions[-1]["text"]


def test_materializer_repairs_rule_39_clause_references_from_rule54_context(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso><act><body>
<article refersTo="/in/union/rules/cgst-rules-2017/rule/39">
<num>39</num><heading>Procedure for distribution of input tax credit by Input Service Distributor</heading>
<content><p>39. Procedure for distribution of input tax credit by Input Service Distributor.- (2) The process specified in clause (j) of sub-rule (1) shall apply for reduction of credit. (3) Subject to sub-rule (2), the Input Service Distributor shall, on the basis of the Input Service Distributor credit note specified in clause (h) of sub-rule (1), issue an invoice.</p></content>
<paragraph refersTo="/in/union/rules/cgst-rules-2017/rule/39/subrule/2">
<num>(2)</num><content><p>[Omitted]</p></content>
</paragraph>
<paragraph refersTo="/in/union/rules/cgst-rules-2017/rule/39/subrule/3">
<num>(3)</num><content><p>(3) Subject to sub-rule (2), the Input Service Distributor shall, on the basis of the Input Service Distributor credit note specified in clause (h) of sub-rule (1), issue an invoice.</p></content>
</paragraph>
</article>
</body></act></akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-06-19"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")

    base_event = {
        "operation": "SUBSTITUTE",
        "status": "validated",
        "source": {"document_id": "/in/union/notifications/cbic/central-tax/2024/12-2024"},
        "legal_time": {"applicability_start": "2024-07-10", "commencement_date": "2024-07-10"},
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": "/in/union/rules/cgst-rules-2017/rule/54/subrule/2",
            "anchor_text": "clause (j)",
        },
        "payload": {"old_text": "clause (j)", "new_text": "clause (n)"},
        "evidence": {"source_span": {"start": 1, "end": 2, "text_hash": "rule39-clause-ref"}},
        "review": {"required": False, "review_reasons": []},
        "validation": {
            "target_resolved": True,
            "anchor_resolved": True,
            "date_resolved": True,
            "source_span_verified": True,
            "materializable": True,
        },
    }
    events = [
        {
            **base_event,
            "event_id": "evt_cbic_d80841df9d5fc78a",
            "evidence": {
                "excerpt": (
                    "(iii) in sub-rule (2), for the words and brackets \"clause (j)\", "
                    "the words and brackets \"clause (n)\" shall be substituted;"
                ),
                "source_span": {"start": 1, "end": 2, "text_hash": "rule39-2"},
            },
        },
        {
            **base_event,
            "event_id": "evt_cbic_92c371e969c66996",
            "target": {
                **base_event["target"],
                "component_id": "/in/union/rules/cgst-rules-2017/rule/54/subrule/3",
                "anchor_text": "clause (h)",
            },
            "payload": {"old_text": "clause (h)", "new_text": "clause (l)"},
            "evidence": {
                "excerpt": (
                    "(iv) in sub-rule (3), for the words and brackets \"clause (h)\", "
                    "the words and brackets \"clause (l)\" shall be substituted;"
                ),
                "source_span": {"start": 1, "end": 2, "text_hash": "rule39-3"},
            },
        },
    ]
    events_path = tmp_path / "events.jsonl"
    events_path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["coverage_gap_count"] == 0
    versions = [
        json.loads(line)
        for line in (tmp_path / "out" / "node_versions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    rule_39_versions = [row for row in versions if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/39"]
    subrule_3_versions = [
        row for row in versions if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/39/subrule/3"
    ]
    assert rule_39_versions[-1]["created_by_event_id"] == "evt_cbic_d80841df9d5fc78a"
    assert "clause (n)" in rule_39_versions[-1]["text"]
    assert subrule_3_versions[-1]["created_by_event_id"] == "evt_cbic_92c371e969c66996"
    assert "clause (l)" in subrule_3_versions[-1]["text"]


def test_materializer_repairs_rule_46_third_proviso_export_sez_endorsement(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso><act><body>
<article refersTo="/in/union/rules/cgst-rules-2017/rule/46">
<num>46</num><heading>Tax invoice</heading>
<content><p>46. Tax invoice.- Existing clauses: Provided also that in the case of the export of goods or services, the invoice shall carry an endorsement “SUPPLY MEANT FOR EXPORT ON PAYMENT OF INTEGRATED TAX” or “SUPPLY MEANT FOR EXPORT UNDER BOND OR LETTER OF UNDERTAKING WITHOUT PAYMENT OF INTEGRATED TAX”, as the case may be, and shall, in lieu of the details specified in clause (e), contain the following details, namely,- (i) name and address of the recipient; (ii) address of delivery; and (iii) name of the country of destination:</p></content>
</article>
</body></act></akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-06-19"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")

    event = {
        "event_id": "evt_cbic_751488db3b2252ac",
        "operation": "SUBSTITUTE",
        "status": "needs_review",
        "source": {"document_id": "/in/union/notifications/cbic/central-tax/2017/17-2017"},
        "legal_time": {"applicability_start": "2017-07-01", "commencement_date": "2017-07-01"},
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": "/in/union/rules/cgst-rules-2017/rule/46",
            "anchor_text": "the third proviso",
        },
        "payload": {
            "old_text": "the third proviso",
            "new_text": "Provided also that in the case of the export of goods or services, the invoice shall carry an endorsement",
        },
        "evidence": {
            "excerpt": (
                "(iv) in rule 46, for the third proviso, the following proviso shall be substituted, namely:- "
                "“Provided also that in the case of the export of goods or services, the invoice shall carry an endorsement...”"
            ),
            "source_span": {"start": 1, "end": 2, "text_hash": "rule46-export-sez"},
        },
        "review": {"required": True, "review_reasons": ["anchor_not_resolved"]},
        "validation": {
            "target_resolved": True,
            "anchor_resolved": False,
            "date_resolved": True,
            "source_span_verified": True,
            "materializable": False,
        },
    }
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(json.dumps(event) + "\n", encoding="utf-8")

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["coverage_gap_count"] == 0
    versions = [
        json.loads(line)
        for line in (tmp_path / "out" / "node_versions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    rule_versions = [row for row in versions if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/46"]
    assert rule_versions[-1]["created_by_event_id"] == "evt_cbic_751488db3b2252ac"
    assert "SUPPLY TO SEZ UNIT OR SEZ DEVELOPER" in rule_versions[-1]["text"]
    assert "SUPPLY MEANT FOR EXPORT ON PAYMENT OF INTEGRATED TAX" not in rule_versions[-1]["text"]


def test_materializer_repairs_rule_46a_insert_and_2022_proviso_payloads(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso><act><body>
<article refersTo="/in/union/rules/cgst-rules-2017/rule/46">
<num>46</num><heading>Tax invoice</heading>
<content><p>46. Tax invoice.- Existing Rule 46 text.</p></content>
</article>
</body></act></akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-06-19"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")

    events = [
        {
            "event_id": "evt_cbic_542473e134821bd1",
            "operation": "INSERT_SIBLING",
            "status": "needs_review",
            "source": {"document_id": "/in/union/notifications/cbic/central-tax/2017/45-2017"},
            "legal_time": {"applicability_start": "2017-10-13", "commencement_date": "2017-10-13"},
            "target": {
                "work_id": "/in/union/rules/cgst-rules-2017",
                "component_id": "/in/union/rules/cgst-rules-2017/rule/46a",
                "anchor_component_id": "/in/union/rules/cgst-rules-2017/rule/46",
                "anchor_text": "after rule 46",
            },
            "payload": {
                "label": "46A",
                "node_type": "rule",
                "heading": "Invoice",
                "content": (
                    "cum-bill of supply.- Notwithstanding anything contained in rule 46 or "
                    "rule 49 or rule 54, where a registered person is supplying taxable as "
                    "well as exempted goods or services or both to an unregistered person, "
                    "a single \"invoice-cum-bill of supply"
                ),
            },
            "evidence": {
                "excerpt": (
                    "after rule 46, the following rule shall be inserted, namely:- "
                    "46A. Invoice-cum-bill of supply.- Notwithstanding anything contained "
                    "in rule 46 or rule 49 or rule 54..."
                ),
                "source_span": {"start": 1, "end": 2, "text_hash": "rule46a-insert"},
            },
            "review": {"required": True, "review_reasons": ["payload_split"]},
            "validation": {"materializable": False},
        },
        {
            "event_id": "evt_cbic_03aea0568073d822",
            "operation": "INSERT_CHILD",
            "status": "needs_review",
            "source": {"document_id": "/in/union/notifications/cbic/central-tax/2022/26-2022"},
            "legal_time": {"applicability_start": "2022-12-26", "commencement_date": "2022-12-26"},
            "target": {
                "work_id": "/in/union/rules/cgst-rules-2017",
                "component_id": "/in/union/rules/cgst-rules-2017/rule/46a/proviso/provided",
                "anchor_component_id": "/in/union/rules/cgst-rules-2017/rule/46a",
                "anchor_text": "the following proviso shall be inserted",
            },
            "payload": {
                "label": "provided",
                "node_type": "proviso",
                "content": (
                    "Provided that the said invoice shall be issued within a period of "
                    "thirty days from the date of issue of such invoice."
                ),
            },
            "evidence": {
                "excerpt": (
                    "in rule 46A, the following proviso shall be inserted, namely:- "
                    "Provided that the said single \"invoice-cum-bill of supply\" shall "
                    "contain the particulars as specified under rule 46 or rule 54..."
                ),
                "source_span": {"start": 1, "end": 2, "text_hash": "rule46a-proviso"},
            },
            "review": {"required": True, "review_reasons": ["wrong_payload"]},
            "validation": {"materializable": False},
        },
    ]
    events_path = tmp_path / "events.jsonl"
    events_path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["coverage_gap_count"] == 0
    assert manifest["applied_count"] == 2
    versions = [
        json.loads(line)
        for line in (tmp_path / "out" / "node_versions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    rule_versions = [row for row in versions if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/46a"]
    proviso_versions = [
        row
        for row in versions
        if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/46a/proviso/provided"
    ]
    assert any(row["created_by_event_id"] == "evt_cbic_542473e134821bd1" for row in rule_versions)
    assert "Invoice-cum-bill of supply" in rule_versions[-1]["text"]
    assert "single \"invoice-cum-bill of supply\" may be issued for all such supplies" in rule_versions[-1]["text"]
    assert "thirty days from the date of issue" not in rule_versions[-1]["text"]
    assert proviso_versions[-1]["created_by_event_id"] == "evt_cbic_03aea0568073d822"
    assert "particulars as specified under rule 46 or rule 54" in proviso_versions[-1]["text"]
    assert "thirty days from the date of issue" not in proviso_versions[-1]["text"]


def test_materializer_repairs_rule_9_subrule_1_from_rule8_reference_context(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso><act><body>
<article refersTo="/in/union/rules/cgst-rules-2017/rule/9">
<num>9</num><heading>Verification of the application and approval</heading>
<content><p>9. Verification. (1) The application shall be forwarded to the proper officer who shall examine the application and the accompanying documents and if the same are found to be in order, approve the grant of registration to the applicant within a period of three working days from the date of submission of the application: Provided that old registration proviso.</p></content>
<paragraph refersTo="/in/union/rules/cgst-rules-2017/rule/9/subrule/1">
<num>(1)</num><content><p>(1) The application shall be forwarded to the proper officer who shall examine the application and the accompanying documents and if the same are found to be in order, approve the grant of registration to the applicant within a period of three working days from the date of submission of the application: Provided that old registration proviso.</p></content>
</paragraph>
</article>
<article refersTo="/in/union/rules/cgst-rules-2017/rule/8">
<num>8</num><heading>Application for registration</heading>
<content><p>8. Application for registration. (1) Existing Rule 8 text.</p></content>
<paragraph refersTo="/in/union/rules/cgst-rules-2017/rule/8/subrule/1">
<num>(1)</num><content><p>(1) Existing Rule 8 text.</p></content>
</paragraph>
</article>
</body></act></akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-06-19"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")

    event = {
        "event_id": "evt_cbic_32a1cc85c6d9561a",
        "operation": "SUBSTITUTE",
        "status": "needs_review",
        "source": {"document_id": "/in/union/notifications/cbic/central-tax/2020/62-2020"},
        "legal_time": {"applicability_start": "2020-08-21", "commencement_date": "2020-08-21"},
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": "/in/union/rules/cgst-rules-2017/rule/8/subrule/1",
            "anchor_text": "the proviso",
        },
        "payload": {
            "old_text": "the proviso",
            "new_text": "Provided that where a person fails to undergo authentication of Aadhaar number",
        },
        "evidence": {
            "excerpt": (
                "(i) in sub-rule (1), for the proviso, the following provisos shall be substituted, namely:- "
                "“Provided that where a person ... as specified in sub-rule (4A) of rule 8...”"
            ),
            "source_span": {"start": 1, "end": 2, "text_hash": "rule9-subrule1-aadhaar"},
        },
        "review": {"required": True, "review_reasons": ["anchor_not_resolved"]},
        "validation": {
            "target_resolved": True,
            "anchor_resolved": False,
            "date_resolved": True,
            "source_span_verified": True,
            "materializable": False,
        },
    }
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(json.dumps(event) + "\n", encoding="utf-8")

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["coverage_gap_count"] == 0
    versions = [
        json.loads(line)
        for line in (tmp_path / "out" / "node_versions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    rule9_versions = [
        row for row in versions if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/9/subrule/1"
    ]
    rule8_versions = [
        row for row in versions if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/8/subrule/1"
    ]
    assert rule9_versions[-1]["created_by_event_id"] == "evt_cbic_32a1cc85c6d9561a"
    assert "authentication of Aadhaar number" in rule9_versions[-1]["text"]
    assert rule8_versions[-1]["created_by_event_id"] is None


def test_materializer_routes_rule_87_online_money_gaming_missing_baseline_to_context_unresolved(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso><act><body>
<article refersTo="/in/union/rules/cgst-rules-2017/rule/87">
<num>87</num><heading>Electronic Cash Ledger</heading>
<content><p>87. Electronic Cash Ledger. (3) The deposit under sub-rule (2) shall be made through authorised modes: Provided further that the challan in FORM GST PMT-06 generated at the common portal shall be valid for a period of fifteen days.</p></content>
<paragraph refersTo="/in/union/rules/cgst-rules-2017/rule/87/subrule/3">
<num>(3)</num><content><p>(3) The deposit under sub-rule (2) shall be made through authorised modes: Provided further that the challan in FORM GST PMT-06 generated at the common portal shall be valid for a period of fifteen days.</p></content>
</paragraph>
</article>
</body></act></akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-06-19"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")

    event = {
        "event_id": "evt_cbic_8745b737d9bd5177",
        "operation": "SUBSTITUTE",
        "status": "needs_review",
        "source": {"document_id": "/in/union/notifications/cbic/central-tax/2023/51-2023"},
        "legal_time": {"applicability_start": "2023-10-01", "commencement_date": "2023-10-01"},
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": "/in/union/rules/cgst-rules-2017/rule/87/subrule/3",
            "anchor_text": "section 14",
        },
        "payload": {
            "old_text": "section 14",
            "new_text": (
                "section 14, or a person supplying online money gaming from a place outside "
                "India to a person in India as referred to in section 14A"
            ),
        },
        "evidence": {
            "excerpt": (
                "7. In the said rules, in rule 87, in sub-rule (3), in the second proviso, "
                "for the words and figures \"section 14\", the words, letters, brackets "
                "and figures \"section 14, or a person supplying online money gaming from "
                "a place outside India to a person in India as referred to in section 14A,\" "
                "shall be substituted."
            ),
            "source_span": {"start": 1, "end": 2, "text_hash": "rule87-online-gaming"},
        },
        "review": {"required": True, "review_reasons": ["anchor_not_resolved"]},
        "validation": {
            "target_resolved": True,
            "anchor_resolved": False,
            "date_resolved": True,
            "source_span_verified": True,
            "materializable": False,
        },
    }
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(json.dumps(event) + "\n", encoding="utf-8")

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["coverage_gap_count"] == 0
    assert manifest["context_unresolved_count"] == 1
    versions = [
        json.loads(line)
        for line in (tmp_path / "out" / "node_versions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    subrule_versions = [
        row for row in versions if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/87/subrule/3"
    ]
    assert subrule_versions[-1]["created_by_event_id"] is None
    assert "section 14A" not in subrule_versions[-1]["text"]


def test_materializer_repairs_rule_39_subrule_1a_insert_then_2025_splice(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso><act><body>
<article refersTo="/in/union/rules/cgst-rules-2017/rule/39">
<num>39</num><heading>Procedure for distribution of input tax credit by Input Service Distributor</heading>
<content><p>39. Procedure for distribution of input tax credit by Input Service Distributor. (1) Existing input service distributor text.</p></content>
<paragraph refersTo="/in/union/rules/cgst-rules-2017/rule/39/subrule/1">
<num>(1)</num><content><p>(1) Existing input service distributor text.</p></content>
</paragraph>
</article>
<article refersTo="/in/union/rules/cgst-rules-2017/rule/54">
<num>54</num><heading>Tax invoice in special cases</heading>
<content><p>54. Tax invoice in special cases. (1A) Old Rule 54 input service distributor invoice text.</p></content>
<paragraph refersTo="/in/union/rules/cgst-rules-2017/rule/54/subrule/1a">
<num>(1A)</num><content><p>(1A) Old Rule 54 input service distributor invoice text.</p></content>
</paragraph>
</article>
</body></act></akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-06-19"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")

    insert_event = {
        "event_id": "evt_cbic_92c8303b6b01e1b9",
        "operation": "INSERT_CHILD",
        "status": "needs_review",
        "source": {"document_id": "/in/union/notifications/cbic/central-tax/2024/12-2024"},
        "legal_time": {"applicability_start": "2024-07-10", "commencement_date": "2024-07-10"},
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": "/in/union/rules/cgst-rules-2017/rule/54/subrule/1a",
        },
        "payload": {
            "label": "1A",
            "node_type": "subrule",
            "parent_component_id": "/in/union/rules/cgst-rules-2017/rule/54",
            "content": (
                "For the distribution of credit in respect of input services, attributable "
                "to one or more distinct persons, subject to levy of tax under sub-section "
                "(3) or (4) of section 9, a registered person, having the same PAN and "
                "State code as an Input Service Distributor, may issue an invoice or, as "
                "the case may be, a credit or debit note as per the provisions of "
                "sub-rule(1A) of rule 54 to transfer the credit of such common input "
                "services to the Input Service Distributor, and such credit shall be "
                "distributed by the said Input Service Distributor in the manner as "
                "provided in sub-rule (1)."
            ),
        },
        "evidence": {
            "excerpt": (
                "In the said rules, in rule 39, after sub-rule (1), the following "
                "sub-rule shall be inserted, namely: sub-rule (1A) ..."
            ),
            "source_span": {"start": 1, "end": 2, "text_hash": "rule39-1a-insert"},
        },
        "review": {"required": True, "review_reasons": ["target_not_resolved"]},
        "validation": {
            "target_resolved": False,
            "anchor_resolved": False,
            "date_resolved": True,
            "source_span_verified": True,
            "materializable": False,
        },
    }
    splice_event = {
        "event_id": "evt_cbic_c0c3f3d6c377fae0",
        "operation": "SPLICE",
        "status": "needs_review",
        "source": {"document_id": "/in/union/notifications/cbic/central-tax/2025/13-2025"},
        "legal_time": {"applicability_start": "2025-09-22", "commencement_date": "2025-09-22"},
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": "/in/union/rules/cgst-rules-2017/rule/39/subrule/1a",
            "anchor_text": "of section 9",
        },
        "payload": {
            "insert_text": (
                "of the Central Goods and Services Tax Act, 2017 or under sub-section "
                "(3) or sub-section (4) of section 5 of the Integrated Goods and Service "
                "Tax Act, 2017 (13 of 2025)"
            ),
            "position": "after",
        },
        "evidence": {
            "excerpt": (
                "3. In the said rules, with effect from the 1st day of April, 2025, "
                "in rule 39, in sub-rule (1A), after the words and figures \"of section 9\", "
                "following shall be inserted."
            ),
            "source_span": {"start": 1, "end": 2, "text_hash": "rule39-1a"},
        },
        "review": {"required": True, "review_reasons": ["anchor_not_resolved"]},
        "validation": {
            "target_resolved": True,
            "anchor_resolved": False,
            "date_resolved": True,
            "source_span_verified": True,
            "materializable": False,
        },
    }
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(
        json.dumps(insert_event) + "\n" + json.dumps(splice_event) + "\n",
        encoding="utf-8",
    )

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["coverage_gap_count"] == 0
    assert manifest["context_unresolved_count"] == 0
    versions = [
        json.loads(line)
        for line in (tmp_path / "out" / "node_versions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    subrule_versions = [
        row for row in versions if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/39/subrule/1a"
    ]
    rule54_versions = [
        row for row in versions if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/54/subrule/1a"
    ]
    assert subrule_versions[-1]["created_by_event_id"] == "evt_cbic_c0c3f3d6c377fae0"
    assert "Integrated Goods and Service Tax Act, 2017 (13 of 2025)" in subrule_versions[-1]["text"]
    assert "sub-rule(1A) of rule 54" in subrule_versions[-1]["text"]
    assert rule54_versions[-1]["created_by_event_id"] is None
    assert "Old Rule 54 input service distributor invoice text." in rule54_versions[-1]["text"]


def test_materializer_routes_rule_43_proviso_rename_missing_insert_to_context_unresolved(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso><act><body>
<article refersTo="/in/union/rules/cgst-rules-2017/rule/43">
<num>43</num><heading>Manner of determination of input tax credit in respect of capital goods</heading>
<content><p>43. Manner of determination. (1) Existing text: Provided that where any capital goods earlier covered under clause (a) is subsequently covered under this clause, the value of A shall be reduced. (g) Existing ratio clause: Provided that where the registered person does not have any turnover during the said tax period, the value of E/F shall be calculated by taking prior values.</p></content>
</article>
</body></act></akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-06-19"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")

    event = {
        "event_id": "evt_cbic_d1aa88d5c1108c32",
        "operation": "SUBSTITUTE",
        "status": "needs_review",
        "source": {"document_id": "/in/union/notifications/cbic/central-tax/2019/16-2019"},
        "legal_time": {"applicability_start": "2019-03-29", "commencement_date": "2019-03-29"},
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": "/in/union/rules/cgst-rules-2017/rule/43",
        },
        "payload": {"old_text": "Provided", "new_text": "Provided further"},
        "evidence": {
            "excerpt": "(C) in the proviso, for the word \"Provided\", the words \"Provided further\" shall be substituted;",
            "source_span": {"start": 1, "end": 2, "text_hash": "rule43-proviso-rename"},
        },
        "review": {"required": True, "review_reasons": ["same_effective_date_conflict"]},
        "validation": {
            "target_resolved": True,
            "anchor_resolved": True,
            "date_resolved": True,
            "source_span_verified": True,
            "materializable": False,
        },
    }
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(json.dumps(event) + "\n", encoding="utf-8")

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["coverage_gap_count"] == 0
    assert manifest["context_unresolved_count"] == 1
    versions = [
        json.loads(line)
        for line in (tmp_path / "out" / "node_versions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    rule_versions = [row for row in versions if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/43"]
    assert rule_versions[-1]["created_by_event_id"] is None
    assert "Provided further" not in rule_versions[-1]["text"]


def test_materializer_repairs_rule_8_subrule_4a_replaces_contaminated_child_slot(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso><act><body>
<article refersTo="/in/union/rules/cgst-rules-2017/rule/8">
<num>8</num><heading>Application for registration</heading>
<content><p>8. Application for registration. (4) Existing sub-rule four. (4A) If the proper officer fails to take any action within three working days, the application shall be deemed approved.</p></content>
<paragraph refersTo="/in/union/rules/cgst-rules-2017/rule/8/subrule/4">
<num>(4)</num><content><p>(4) Existing sub-rule four.</p></content>
</paragraph>
<paragraph refersTo="/in/union/rules/cgst-rules-2017/rule/8/subrule/4a">
<num>(4A)</num><content><p>If the proper officer fails to take any action within three working days, the application shall be deemed approved.</p></content>
</paragraph>
</article>
</body></act></akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-06-19"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")

    event = {
        "event_id": "evt_cbic_e7c949e08edbb979",
        "operation": "INSERT_CHILD",
        "status": "validated",
        "source": {"document_id": "/in/union/notifications/cbic/central-tax/2020/16-2020"},
        "legal_time": {"applicability_start": "2026-06-01", "commencement_date": "2026-06-01"},
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": "/in/union/rules/cgst-rules-2017/rule/8/subrule/4a",
            "anchor_component_id": "/in/union/rules/cgst-rules-2017/rule/8/subrule/4",
            "anchor_text": "after sub-rule (4)",
            "anchor_occurrence": 1,
        },
        "payload": {
            "label": "4A",
            "node_type": "subrule",
            "parent_component_id": "/in/union/rules/cgst-rules-2017/rule/8",
            "content": (
                "The applicant shall, while submitting an application under sub-rule "
                "(4), with effect from 01.04.2020, undergo authentication of Aadhaar "
                "number for grant of registration."
            ),
        },
        "evidence": {
            "excerpt": (
                "2. In the Central Goods and Services Tax Rules, 2017, in rule 8, "
                "after sub-rule (4), the following sub-rule shall be inserted, namely:- "
                "(4A) The applicant shall..."
            ),
            "source_span": {"start": 1, "end": 2, "text_hash": "rule8-4a"},
        },
        "review": {"required": False, "review_reasons": []},
        "validation": {
            "target_resolved": True,
            "anchor_resolved": True,
            "date_resolved": True,
            "source_span_verified": True,
            "materializable": True,
        },
    }
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(json.dumps(event) + "\n", encoding="utf-8")

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["coverage_gap_count"] == 0
    versions = [
        json.loads(line)
        for line in (tmp_path / "out" / "node_versions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    subrule_versions = [
        row for row in versions if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/8/subrule/4a"
    ]
    assert subrule_versions[-1]["created_by_event_id"] == "evt_cbic_e7c949e08edbb979"
    assert "authentication of Aadhaar number" in subrule_versions[-1]["text"]
    assert "proper officer fails to take any action" not in subrule_versions[-1]["text"]


def test_materializer_carves_missing_subrule_before_text_edit(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso><act><body>
<article refersTo="/in/union/rules/cgst-rules-2017/rule/96b">
<num>96B</num><heading>Recovery of refund</heading>
<content><p>96B. Recovery of refund. (1) The amount refunded shall be recovered in accordance with the provisions of section 73 or 74 of the Act. (2) Later subrule.</p></content>
</article>
</body></act></akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2020-03-23"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")

    event = {
        "event_id": "evt_cbic_4dc58009c1e5b970",
        "operation": "SUBSTITUTE",
        "status": "validated",
        "source": {"document_id": "/in/union/notifications/cbic/central-tax/2024/20-2024"},
        "legal_time": {"applicability_start": "2024-10-08", "commencement_date": "2024-10-08"},
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": "/in/union/rules/cgst-rules-2017/rule/96b/subrule/1",
            "anchor_text": None,
        },
        "payload": {
            "old_text": "section 73 or 74",
            "new_text": "section 73 or section 74 or section 74A",
        },
        "evidence": {"source_span": {"start": 1, "end": 2, "text_hash": "rule96b-1"}},
        "review": {"required": False, "review_reasons": []},
        "validation": {
            "target_resolved": True,
            "anchor_resolved": True,
            "date_resolved": True,
            "source_span_verified": True,
            "materializable": True,
        },
    }
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(json.dumps(event) + "\n", encoding="utf-8")

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["coverage_gap_count"] == 0
    versions = [
        json.loads(line)
        for line in (tmp_path / "out" / "node_versions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    subrule_versions = [
        row for row in versions if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/96b/subrule/1"
    ]
    assert subrule_versions[-1]["created_by_event_id"] == "evt_cbic_4dc58009c1e5b970"
    assert "section 73 or section 74 or section 74A" in subrule_versions[-1]["text"]
    gaps_path = tmp_path / "out" / "coverage_gaps.json"
    assert "partial_apply" not in gaps_path.read_text(encoding="utf-8")


def test_materializer_carves_subrule_before_explanation_numbering(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso><act><body>
<article refersTo="/in/union/rules/cgst-rules-2017/rule/88b">
<num>88B</num><heading>Manner of calculating interest</heading>
<content><p>(1) Proceedings under section 73 or section 74 are excluded. (2) Other cases apply. Explanation. —For the purposes of this sub-rule, — (1) input tax credit wrongly availed shall be construed to have been utilised; (2) the date of utilisation shall be taken to be the return date.</p></content>
</article>
</body></act></akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2022-07-05"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")

    event = {
        "event_id": "evt_cbic_da65c6cfa8547f49",
        "operation": "SPLICE",
        "status": "validated",
        "source": {"document_id": "/in/union/notifications/cbic/central-tax/2024/20-2024"},
        "legal_time": {"applicability_start": "2024-11-01", "commencement_date": "2024-11-01"},
        "target": {
            "work_id": "/in/union/rules/cgst-rules-2017",
            "component_id": "/in/union/rules/cgst-rules-2017/rule/88b/subrule/1",
            "anchor_text": "or section 74",
        },
        "payload": {"insert_text": "or section 74A", "position": "after"},
        "evidence": {"source_span": {"start": 1, "end": 2, "text_hash": "rule88b-1"}},
        "review": {"required": False, "review_reasons": []},
        "validation": {
            "target_resolved": True,
            "anchor_resolved": True,
            "date_resolved": True,
            "source_span_verified": True,
            "materializable": True,
        },
    }
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(json.dumps(event) + "\n", encoding="utf-8")

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["coverage_gap_count"] == 0
    versions = [
        json.loads(line)
        for line in (tmp_path / "out" / "node_versions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    subrule_versions = [
        row for row in versions if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/88b/subrule/1"
    ]
    assert subrule_versions[-1]["created_by_event_id"] == "evt_cbic_da65c6cfa8547f49"
    assert "section 73 or section 74 or section 74A" in subrule_versions[-1]["text"]
    assert "input tax credit wrongly availed" not in subrule_versions[-1]["text"]


def test_materializer_repairs_rule_89_subrule_4_clause_c_substitution(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso><act><body>
<article refersTo="/in/union/rules/cgst-rules-2017/rule/89">
<num>89</num><heading>Application for refund</heading>
        <paragraph refersTo="/in/union/rules/cgst-rules-2017/rule/89/subrule/4">
<num>(4)</num><content><p>(4) In the case of zero-rated supply of goods or services or both without payment of tax under bond or letter of undertaking, refund of input tax credit shall be granted as per the following formula - Where,- (A) "Refund amount" means the maximum refund that is admissible; (B) "Net ITC" means input tax credit availed on inputs and input services during the relevant period; (C) "Turnover of zero-rated supply of goods" means the value of zero-rated supply of goods made during the relevant period without payment of tax under bond or letter of undertaking, other than the turnover of supplies in respect of which refund is claimed under sub-rules (4A) or (4B) or both; (D) "Turnover of zero-rated supply of services" means the value of zero-rated supply of services.</p></content>
</paragraph>
</article>
</body></act></akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-06-19"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")
    events = [
        {
            "event_id": "evt_cbic_e23ce17aa2de96c4",
            "operation": "SUBSTITUTE",
            "status": "needs_review",
            "source": {"document_id": "/in/union/notifications/cbic/central-tax/2020/16-2020"},
            "legal_time": {"applicability_start": "2020-04-01"},
            "target": {
                "work_id": "/in/union/rules/cgst-rules-2017",
                "component_id": "/in/union/rules/cgst-rules-2017/rule/89/subrule/4",
                "anchor_text": "clause (C)",
            },
            "payload": {
                "old_text": "clause (C)",
                "new_text": None,
                "context_recovered_target": True,
                "triage_lane": "context_unresolved",
            },
            "evidence": {"source_span": {"start": 5855, "end": 6441, "text_hash": "rule89-4-c"}},
            "review": {"required": True, "review_reasons": ["context_unresolved"]},
        },
    ]
    events_path = tmp_path / "events.jsonl"
    events_path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["coverage_gap_count"] == 0
    versions = [
        json.loads(line)
        for line in (tmp_path / "out" / "node_versions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    subrule_versions = [
        row for row in versions if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/89/subrule/4"
    ]
    assert subrule_versions[-1]["created_by_event_id"] == "evt_cbic_e23ce17aa2de96c4"
    assert "1.5 times the value of like goods domestically supplied" in subrule_versions[-1]["text"]
    assert "sub-rules (4A) or (4B) or both" in subrule_versions[-1]["text"]


def test_materializer_repairs_rule_96_subrule_3_gstr3b_substitution(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso><act><body>
<article refersTo="/in/union/rules/cgst-rules-2017/rule/96">
<num>96</num><heading>Refund of integrated tax paid on goods exported out of India</heading><content><p>96. Refund. (1) The applicant has furnished a valid return in FORM GSTR-3. (3) Upon the receipt of the information regarding the furnishing of a valid return in FORM GSTR-3 from the common portal, the system designated by the Customs shall process the claim for refund.</p></content>
</article>
<paragraph refersTo="/in/union/rules/cgst-rules-2017/rule/96/subrule/3">
<num>(3)</num><content><p>(3) Upon the receipt of the information regarding the furnishing of a valid return in FORM GSTR-3 from the common portal, the system designated by the Customs shall process the claim for refund.</p></content>
</paragraph>
</body></act></akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-06-19"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(
        json.dumps(
            {
                "event_id": "evt_cbic_7aabc9f26c533448",
                "operation": "SUBSTITUTE",
                "status": "needs_review",
                "source": {
                    "document_id": "/in/union/notifications/cbic/central-tax/2022/19-2022",
                    "publication_date": "2022-09-28",
                },
                "legal_time": {"applicability_start": "2022-10-01"},
                "target": {
                    "work_id": "/in/union/rules/cgst-rules-2017",
                    "component_id": "/in/union/rules/cgst-rules-2017/rule/96",
                    "anchor_text": "FORM GSTR- 3 or FORM GSTR-3B, as the case may be",
                },
                "payload": {
                    "old_text": "FORM GSTR-\n3 or FORM GSTR-3B, as the case may be",
                    "new_text": "FORM GSTR-3B",
                },
                "validation": {
                    "target_resolved": True,
                    "anchor_resolved": False,
                    "date_resolved": True,
                    "source_span_verified": True,
                    "materializable": False,
                },
                "evidence": {
                    "source_span": {"start": 13, "text_hash": "rule96-19"},
                    "excerpt": (
                        "In rule 96 of the said rules, in sub-rule (3), for the words, "
                        "letters and figures, FORM GSTR- 3 or FORM GSTR-3B, as the case "
                        "may be, the letters and figure, FORM GSTR-3B shall be substituted"
                    ),
                },
                "review": {"required": True, "review_reasons": ["anchor_not_resolved"]},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["coverage_gap_count"] == 0
    assert manifest["applied_count"] == 1
    versions = [
        json.loads(line)
        for line in (tmp_path / "out" / "node_versions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    subrule_3_versions = [
        row for row in versions if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/96/subrule/3"
    ]
    parent_versions = [
        row for row in versions if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/96"
    ]
    assert "valid return in FORM GSTR-3B from the common portal" in subrule_3_versions[-1]["text"]
    assert "The applicant has furnished a valid return in FORM GSTR-3." in parent_versions[-1]["text"]


def test_materializer_repairs_rule_36_subrule_3_section_74_parent_anchor(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso><act><body>
<article refersTo="/in/union/rules/cgst-rules-2017/rule/36">
<num>36</num><heading>Documentary requirements</heading><content><p>36. Documentary requirements. (3) No input tax credit shall be availed by a registered person in respect of any tax that has been paid in pursuance of any order where any demand has been confirmed on account of any fraud, willful misstatement or suppression of facts.</p></content>
</article>
<paragraph refersTo="/in/union/rules/cgst-rules-2017/rule/36/subrule/3">
<num>(3)</num><content><p>(3) of section 31, subject to the payment of tax; (c) a debit note issued by a supplier in accordance with the provisions of section 34.</p></content>
</paragraph>
</body></act></akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-06-19"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(
        json.dumps(
            {
                "event_id": "evt_cbic_4274a8ccd0fc33f3",
                "operation": "SPLICE",
                "status": "needs_review",
                "source": {
                    "document_id": "/in/union/notifications/cbic/central-tax/2024/20-2024",
                    "publication_date": "2024-10-08",
                },
                "legal_time": {"applicability_start": "2024-10-08"},
                "target": {
                    "work_id": "/in/union/rules/cgst-rules-2017",
                    "component_id": "/in/union/rules/cgst-rules-2017/rule/36/subrule/3",
                    "anchor_text": "suppression of facts",
                },
                "payload": {"position": "after", "insert_text": "under section 74"},
                "validation": {
                    "target_resolved": True,
                    "anchor_resolved": False,
                    "date_resolved": True,
                    "source_span_verified": True,
                    "materializable": False,
                },
                "evidence": {
                    "source_span": {"start": 10, "text_hash": "rule36-20"},
                    "excerpt": (
                        "In rule 36, in sub-rule (3), after the words \"suppression of facts\", "
                        "the words and figures \"under section 74\" shall be inserted."
                    ),
                },
                "review": {"required": True, "review_reasons": ["anchor_not_resolved"]},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["coverage_gap_count"] == 0
    assert manifest["applied_count"] == 1
    versions = [
        json.loads(line)
        for line in (tmp_path / "out" / "node_versions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    parent_versions = [
        row for row in versions if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/36"
    ]
    stale_child_versions = [
        row for row in versions if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/36/subrule/3"
    ]
    assert "suppression of facts under section 74." in parent_versions[-1]["text"]
    assert "suppression of facts under section 74" not in stale_child_versions[-1]["text"]


def test_materializer_records_already_reflected_rule_36_subrule_4_insert(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    rule_36_4_text = (
        "(4) Input tax credit to be availed by a registered person in respect of invoices "
        "or debit notes, the details of which have not been uploaded by the suppliers "
        "under sub-section (1) of section 37, shall not exceed 20 per cent. of the "
        "eligible credit available in respect of invoices or debit notes the details of "
        "which have been uploaded by the suppliers under sub-section (1) of section 37."
    )
    (baseline_dir / "baseline.xml").write_text(
        f"""
<akomaNtoso><act><body>
<article refersTo="/in/union/rules/cgst-rules-2017/rule/36">
<num>36</num><heading>Documentary requirements</heading><content><p>36. Documentary requirements.</p></content>
<paragraph refersTo="/in/union/rules/cgst-rules-2017/rule/36/subrule/3"><num>(3)</num><content><p>(3) Existing sub-rule.</p></content></paragraph>
<paragraph refersTo="/in/union/rules/cgst-rules-2017/rule/36/subrule/4"><num>(4)</num><content><p>{rule_36_4_text}</p></content></paragraph>
</article>
</body></act></akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-06-19"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(
        json.dumps(
            {
                "event_id": "evt_cbic_05c7c19bf7d3f0c5",
                "operation": "INSERT_CHILD",
                "status": "validated",
                "source": {
                    "document_id": "/in/union/notifications/cbic/central-tax/2019/49-2019",
                    "publication_date": "2019-10-09",
                    "record_id": "1000666",
                },
                "legal_time": {"applicability_start": "2019-10-09"},
                "target": {
                    "work_id": "/in/union/rules/cgst-rules-2017",
                    "component_id": "/in/union/rules/cgst-rules-2017/rule/36/subrule/4",
                    "anchor_component_id": "/in/union/rules/cgst-rules-2017/rule/36/subrule/3",
                },
                "payload": {
                    "parent_component_id": "/in/union/rules/cgst-rules-2017/rule/36",
                    "anchor_component_id": "/in/union/rules/cgst-rules-2017/rule/36/subrule/3",
                    "label": "4",
                    "node_type": "subrule",
                    "position": "after",
                    "content": rule_36_4_text,
                },
                "validation": {"materializable": True},
                "evidence": {
                    "source_span": {"start": 1576, "end": 2074, "text_hash": "rule36-4"},
                    "excerpt": "In rule 36, after sub-rule (3), the following sub-rule shall be inserted.",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["coverage_gap_count"] == 0
    assert manifest["applied_count"] == 1
    versions = [
        json.loads(line)
        for line in (tmp_path / "out" / "node_versions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    subrule_versions = [
        row for row in versions if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/36/subrule/4"
    ]
    assert subrule_versions[-1]["created_by_event_id"] == "evt_cbic_05c7c19bf7d3f0c5"
    assert "20 per cent." in subrule_versions[-1]["text"]


def test_materializer_repairs_rule_46_2023_proviso_substitution_to_child(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    old_text = (
        "name and address of the recipient along with its PIN code and the name of the State "
        "and the said address shall be deemed to be the address on record of the recipient"
    )
    new_text = "name of the state of the recipient and the same shall be deemed to be the address on record of the recipient"
    proviso_component = "/in/union/rules/cgst-rules-2017/rule/46/proviso/providedthat-de982caa10"
    (baseline_dir / "baseline.xml").write_text(
        f"""
<akomaNtoso><act><body>
<article refersTo="/in/union/rules/cgst-rules-2017/rule/46">
<num>46</num><heading>Tax invoice</heading><content><p>46. Tax invoice.</p></content>
<proviso refersTo="{proviso_component}"><num>Provided that</num><content><p>Provided that where any taxable service is supplied by or through an electronic commerce operator to a recipient who is un-registered, irrespective of the value of such supply, a tax invoice issued by the registered person shall contain the {old_text}.</p></content></proviso>
</article>
</body></act></akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-06-19"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(
        json.dumps(
            {
                "event_id": "evt_cbic_eb18afb06f304825",
                "operation": "SUBSTITUTE",
                "status": "validated",
                "source": {
                    "document_id": "/in/union/notifications/cbic/central-tax/2023/38-2023",
                    "publication_date": "2023-08-04",
                    "record_id": "1009820",
                },
                "legal_time": {"applicability_start": "2023-08-04"},
                "target": {
                    "work_id": "/in/union/rules/cgst-rules-2017",
                    "component_id": "/in/union/rules/cgst-rules-2017/rule/46",
                    "anchor_text": old_text,
                },
                "payload": {"old_text": old_text, "new_text": new_text},
                "validation": {"materializable": True},
                "evidence": {
                    "source_span": {"start": 6026, "end": 6427, "text_hash": "rule46-2023"},
                    "excerpt": "In rule 46, in clause (f), in the proviso, for the words ...",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["coverage_gap_count"] == 0
    assert manifest["applied_count"] == 1
    versions = [
        json.loads(line)
        for line in (tmp_path / "out" / "node_versions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    proviso_versions = [row for row in versions if row["component_id"] == proviso_component]
    parent_versions = [row for row in versions if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/46"]
    assert proviso_versions[-1]["created_by_event_id"] == "evt_cbic_eb18afb06f304825"
    assert new_text in proviso_versions[-1]["text"]
    assert old_text not in proviso_versions[-1]["text"]
    assert old_text in parent_versions[-1]["text"]


def test_materializer_repairs_rule_24_deadline_chain_on_parent_text(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso><act><body>
<article refersTo="/in/union/rules/cgst-rules-2017/rule/24">
<num>24</num><heading>Migration</heading><content><p>24. Migration. (4) Every person registered under any of the existing laws, who is not liable to be registered under the Act may, within a period of thirty days from the appointed day, at his option, submit an application electronically in FORM GST REG-29.</p></content>
</article>
<paragraph refersTo="/in/union/rules/cgst-rules-2017/rule/24/subrule/4">
<num>(4)</num><content><p>(4) Every person registered under any of the existing laws, who is not liable to be registered under the Act may, within a period of thirty days from the appointed day, at his option, submit an application electronically in FORM GST REG-29.</p></content>
</paragraph>
</body></act></akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-06-19"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")
    fixtures = [
        (
            "evt_cbic_876bade2978b29ea",
            "2017-07-27",
            "within a period of thirty days from the appointed day",
            "on or before 30th September, 2017",
            "within a period of thirty days from the appointed day",
        ),
        (
            "evt_cbic_ba5635bfca1625c3",
            "2017-09-29",
            "30th\nSeptember",
            "31st October",
            "30th September",
        ),
        (
            "evt_cbic_f14c69c16e5d3094",
            "2017-10-28",
            "on or before 31st\nOctober, 2017",
            "on or before 31st December, 2017",
            "on or before 31st October, 2017",
        ),
        (
            "evt_cbic_daa2627a546cc11a",
            "2018-01-23",
            "31st December, 2017",
            "31st March, 2018",
            "31st December, 2017",
        ),
    ]
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(
        "\n".join(
            json.dumps(
                {
                    "event_id": event_id,
                    "operation": "SUBSTITUTE",
                    "status": "needs_review",
                    "source": {"document_id": f"/in/union/notifications/cbic/central-tax/{date[:4]}/fixture", "publication_date": date},
                    "legal_time": {"applicability_start": date},
                    "target": {
                        "work_id": "/in/union/rules/cgst-rules-2017",
                        "component_id": "/in/union/rules/cgst-rules-2017/rule/24/subrule/4",
                        "anchor_text": anchor,
                    },
                    "payload": {"old_text": old_text, "new_text": new_text},
                    "validation": {
                        "target_resolved": True,
                        "anchor_resolved": False,
                        "date_resolved": True,
                        "source_span_verified": True,
                        "materializable": False,
                    },
                    "evidence": {
                        "source_span": {"start": index + 1, "text_hash": event_id},
                        "excerpt": f"for {anchor} substitute {new_text}",
                    },
                    "review": {"required": True, "review_reasons": ["anchor_not_resolved"]},
                }
            )
            for index, (event_id, date, old_text, new_text, anchor) in enumerate(fixtures)
        )
        + "\n",
        encoding="utf-8",
    )

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["coverage_gap_count"] == 0
    assert manifest["applied_count"] == 4
    versions = [
        json.loads(line)
        for line in (tmp_path / "out/node_versions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    rule_24_versions = [
        row for row in versions if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/24"
    ]
    assert rule_24_versions[-1]["valid_from"] == "2018-01-23"
    assert "on or before 31st March, 2018" in rule_24_versions[-1]["text"]


def test_materializer_repairs_rule_129_quoted_phrase_substitution(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso><act><body>
<article refersTo="/in/union/rules/cgst-rules-2017/rule/129">
<num>129</num><heading>Initiation and conduct of proceedings</heading><content><p>129. Proceedings.</p></content>
<paragraph refersTo="/in/union/rules/cgst-rules-2017/rule/129/subrule/6">
<num>(6)</num><content><p>(6) The Director General of Safeguards shall complete the investigation within a period of three months of the receipt of the reference from the Standing Committee or within such extended period not exceeding a further period of three months for reasons to be recorded in writing.</p></content>
</paragraph>
</article>
</body></act></akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-06-19"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(
        json.dumps(
            {
                "event_id": "evt_cbic_001cf1c32bf91009",
                "operation": "SUBSTITUTE",
                "status": "validated",
                "source": {
                    "document_id": "/in/union/notifications/cbic/central-tax/2019/31-2019",
                    "publication_date": "2019-06-28",
                },
                "legal_time": {"applicability_start": "2019-06-28"},
                "target": {
                    "work_id": "/in/union/rules/cgst-rules-2017",
                    "component_id": "/in/union/rules/cgst-rules-2017/rule/129/subrule/6",
                    "anchor_text": "three",
                },
                "payload": {
                    "old_text": "three",
                    "new_text": "shall complete the investigation within a period of three months",
                },
                "validation": {"materializable": True},
                "evidence": {
                    "source_span": {"start": 8382, "end": 8581, "text_hash": "rule129"},
                    "excerpt": (
                        "for the word \"three\" used in the phrase \"shall complete the investigation "
                        "within a period of three months\", the word \"six\" shall be substituted"
                    ),
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["coverage_gap_count"] == 0
    assert manifest["applied_count"] == 1
    versions = [
        json.loads(line)
        for line in (tmp_path / "out/node_versions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    subrule_versions = [
        row for row in versions if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/129/subrule/6"
    ]
    text = subrule_versions[-1]["text"]
    assert "shall complete the investigation within a period of six months" in text
    assert "further period of three months" in text


def test_materializer_repairs_rule_23_10b_condition_commencement(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso><act><body>
<article refersTo="/in/union/rules/cgst-rules-2017/rule/23">
<num>23</num><heading>Revocation</heading><content><p>23. Revocation. (1) A registered person, whose registration is cancelled by the proper officer on his own motion, may submit an application for revocation of cancellation of registration, in FORM GST REG-21.</p></content>
</article>
<paragraph refersTo="/in/union/rules/cgst-rules-2017/rule/23/subrule/1">
<num>(1)</num><content><p>(1) A registered person, whose registration is cancelled by the proper officer on his own motion, may submit an application for revocation of cancellation of registration, in FORM GST REG-21.</p></content>
</paragraph>
</body></act></akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-06-19"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(
        json.dumps(
            {
                "event_id": "evt_cbic_cc2573caef6b8746",
                "operation": "SPLICE",
                "status": "needs_review",
                "source": {
                    "document_id": "/in/union/notifications/cbic/central-tax/2021/35-2021",
                    "publication_date": "2021-09-24",
                },
                "legal_time": {"applicability_start": "2021-09-24"},
                "target": {
                    "work_id": "/in/union/rules/cgst-rules-2017",
                    "component_id": "/in/union/rules/cgst-rules-2017/rule/23",
                    "anchor_text": "may file",
                },
                "payload": {"position": "after", "insert_text": ", subject to the provisions of rule 10B,"},
                "validation": {
                    "target_resolved": True,
                    "anchor_resolved": False,
                    "date_resolved": True,
                    "source_span_verified": True,
                    "materializable": False,
                },
                "evidence": {
                    "source_span": {"start": 20, "text_hash": "abc"},
                    "excerpt": (
                        "in sub-rule (1), with effect from the date as may be notified, after "
                        "the words \"may file\", the words \", subject to the provisions of "
                        "rule 10B,\" shall be inserted"
                    ),
                },
                "review": {"required": True, "review_reasons": ["anchor_not_resolved"]},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["coverage_gap_count"] == 0
    assert manifest["applied_count"] == 1
    versions = [
        json.loads(line)
        for line in (tmp_path / "out/node_versions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    rule_23_versions = [
        row for row in versions if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/23"
    ]
    assert rule_23_versions[-1]["valid_from"] == "2022-01-01"
    assert "may, subject to the provisions of rule 10B, submit" in rule_23_versions[-1]["text"]


def test_materializer_repairs_rule_26_evc_proviso_chain(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso><act><body>
<article refersTo="/in/union/rules/cgst-rules-2017/rule/26">
<num>26</num><heading>Method of authentication</heading><content><p>26. Method of authentication. (1) Applications shall be authenticated: Provided that a registered person registered under the provisions of the Companies Act, 2013 (18 of 2013) shall furnish documents through digital signature certificate.</p></content>
</article>
<paragraph refersTo="/in/union/rules/cgst-rules-2017/rule/26/subrule/1">
<num>(1)</num><content><p>(1) Applications shall be authenticated: Provided that a registered person registered under the provisions of the Companies Act, 2013 (18 of 2013) shall furnish documents through digital signature certificate.</p></content>
</paragraph>
</body></act></akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-06-19"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")
    events_path = tmp_path / "events.jsonl"
    events = [
        {
            "event_id": "evt_cbic_5fd4db93a9083bb7",
            "operation": "INSERT_CHILD",
            "status": "validated",
            "source": {"document_id": "/in/union/notifications/cbic/central-tax/2020/38-2020", "publication_date": "2020-05-05"},
            "legal_time": {"applicability_start": "2020-04-21"},
            "target": {
                "work_id": "/in/union/rules/cgst-rules-2017",
                "component_id": "/in/union/rules/cgst-rules-2017/rule/26/subrule/1/proviso/providedfurtherthat-d30ed6bbe0",
                "anchor_component_id": "/in/union/rules/cgst-rules-2017/rule/26/subrule/1",
                "anchor_text": "/in/union/rules/cgst-rules-2017/rule/26/subrule/1",
            },
            "payload": {
                "parent_component_id": "/in/union/rules/cgst-rules-2017/rule/26/subrule/1",
                "label": "Provided further that",
                "node_type": "proviso",
                "content": (
                    "Provided further that a registered person registered under the provisions "
                    "of the Companies Act, 2013 (18 of 2013) shall, during the period from "
                    "the 21st day of April, 2020 to the 30th day of June, 2020, also be "
                    "allowed to furnish the return under section 39 in FORM GSTR-3B verified "
                    "through electronic verification code (EVC)."
                ),
            },
            "validation": {
                "target_resolved": True,
                "anchor_resolved": True,
                "date_resolved": True,
                "source_span_verified": True,
                "materializable": True,
            },
            "evidence": {"source_span": {"start": 1, "text_hash": "insert"}},
            "review": {"required": False, "review_reasons": []},
        },
        {
            "event_id": "evt_cbic_7a1f16dca7f92d9f",
            "operation": "SUBSTITUTE",
            "status": "needs_review",
            "source": {"document_id": "/in/union/notifications/cbic/central-tax/2020/48-2020", "publication_date": "2020-06-19"},
            "legal_time": {"applicability_start": "2020-05-27"},
            "target": {
                "work_id": "/in/union/rules/cgst-rules-2017",
                "component_id": "/in/union/rules/cgst-rules-2017/rule/26",
                "anchor_text": "the second proviso",
            },
            "payload": {"old_text": "the second proviso", "new_text": "truncated"},
            "validation": {
                "target_resolved": True,
                "anchor_resolved": False,
                "date_resolved": True,
                "source_span_verified": True,
                "materializable": False,
            },
            "evidence": {
                "source_span": {"start": 2, "text_hash": "sub"},
                "excerpt": (
                    "in rule 26 in sub-rule (1), for the second proviso, following provisos "
                    "shall be substituted"
                ),
            },
            "review": {"required": True, "review_reasons": ["anchor_not_resolved"]},
        },
        {
            "event_id": "evt_cbic_5f76bcfef1db6c64",
            "operation": "INSERT_CHILD",
            "status": "validated",
            "source": {"document_id": "/in/union/notifications/cbic/central-tax/2021/7-2021", "publication_date": "2021-04-27"},
            "legal_time": {"applicability_start": "2021-04-27"},
            "target": {
                "work_id": "/in/union/rules/cgst-rules-2017",
                "component_id": "/in/union/rules/cgst-rules-2017/rule/26/subrule/1/proviso/providedalsothat-3b0af079f2",
                "anchor_component_id": "/in/union/rules/cgst-rules-2017/rule/26/subrule/1",
                "anchor_text": "/in/union/rules/cgst-rules-2017/rule/26/subrule/1",
            },
            "payload": {
                "parent_component_id": "/in/union/rules/cgst-rules-2017/rule/26/subrule/1",
                "label": "Provided also that",
                "node_type": "proviso",
                "content": (
                    "Provided also that a registered person registered under the provisions of "
                    "the Companies Act, 2013 (18 of 2013) shall, during the period from the "
                    "27th day of April, 2021 to the 31st day of May, 2021, also be allowed "
                    "to furnish the return under section 39 in FORM GSTR-3B."
                ),
            },
            "validation": {
                "target_resolved": True,
                "anchor_resolved": True,
                "date_resolved": True,
                "source_span_verified": True,
                "materializable": True,
            },
            "evidence": {"source_span": {"start": 3, "text_hash": "insert2021"}},
            "review": {"required": False, "review_reasons": []},
        },
        {
            "event_id": "evt_cbic_8e62222199d9ad8c",
            "operation": "SUBSTITUTE",
            "status": "validated",
            "source": {"document_id": "/in/union/notifications/cbic/central-tax/2021/27-2021", "publication_date": "2021-06-01"},
            "legal_time": {"applicability_start": "2021-06-01"},
            "target": {
                "work_id": "/in/union/rules/cgst-rules-2017",
                "component_id": "/in/union/rules/cgst-rules-2017/rule/26",
                "anchor_text": "31st day of May, 2021",
            },
            "payload": {"old_text": "31st day of May, 2021", "new_text": "31st day of August, 2021"},
            "validation": {
                "target_resolved": True,
                "anchor_resolved": True,
                "date_resolved": True,
                "source_span_verified": True,
                "materializable": True,
            },
            "evidence": {"source_span": {"start": 4, "text_hash": "date2021"}},
            "review": {"required": False, "review_reasons": []},
        },
    ]
    events_path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["coverage_gap_count"] == 0
    assert manifest["applied_count"] == 5
    versions = [
        json.loads(line)
        for line in (tmp_path / "out/node_versions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    gstr3b_versions = [
        row
        for row in versions
        if row["component_id"]
        == "/in/union/rules/cgst-rules-2017/rule/26/subrule/1/proviso/providedfurtherthat-d30ed6bbe0"
    ]
    subrule_versions = [
        row for row in versions if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/26/subrule/1"
    ]
    gstr1_2021_versions = [
        row
        for row in versions
        if row["component_id"]
        == "/in/union/rules/cgst-rules-2017/rule/26/subrule/1/proviso/providedalsothat-3b0af079f2"
    ]
    assert "30th day of September, 2020" in gstr3b_versions[-1]["text"]
    assert "FORM GSTR-1 verified through electronic verification code (EVC)" in subrule_versions[-1]["text"]
    assert "31st day of August, 2021" in gstr1_2021_versions[-1]["text"]


def test_materializer_skips_already_reflected_rule_26_chapter_heading(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso><act><body>
<article refersTo="/in/union/rules/cgst-rules-2017/rule/26">
<num>26</num><heading>Method of authentication</heading><content><p>26. Method of authentication.</p></content>
</article>
<chapter><num>Chapter IV</num><heading>Determination of Value of Supply</heading></chapter>
</body></act></akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-06-19"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(
        json.dumps(
            {
                "event_id": "evt_cbic_be6914a5115d56f3",
                "operation": "INSERT_SIBLING",
                "status": "needs_review",
                "source": {"document_id": "/in/union/notifications/cbic/central-tax/2017/10-2017", "publication_date": "2017-06-28"},
                "legal_time": {"applicability_start": "2017-07-01"},
                "target": {
                    "work_id": "/in/union/rules/cgst-rules-2017",
                    "component_id": "/in/union/rules/cgst-rules-2017/rule/26",
                    "anchor_text": "after rule 26",
                },
                "payload": {"insert_text": "Chapter IV Determination of Value of Supply"},
                "validation": {
                    "target_resolved": False,
                    "anchor_resolved": False,
                    "date_resolved": True,
                    "source_span_verified": True,
                    "materializable": False,
                },
                "evidence": {
                    "source_span": {"start": 1, "text_hash": "chapter"},
                    "excerpt": "after rule 26, the following shall be inserted, namely:- Chapter IV Determination of Value of Supply",
                },
                "review": {
                    "required": True,
                    "review_reasons": ["anchor_not_resolved", "inserted_component_already_exists"],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["applied_count"] == 0
    assert manifest["coverage_gap_count"] == 0


def test_materializer_repairs_rule_61_subrule_5_2017_substitution(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso><act><body>
<article refersTo="/in/union/rules/cgst-rules-2017/rule/61">
<num>61</num><heading>Form and manner of submission of monthly return</heading><content><p>61. Form and manner. (5) The Commissioner may, by notification, specify that return shall be furnished in FORM GSTR-3B.</p></content>
</article>
<paragraph refersTo="/in/union/rules/cgst-rules-2017/rule/61/subrule/5">
<num>(5)</num><content><p>(5) The Commissioner may, by notification, specify that return shall be furnished in FORM GSTR-3B.</p></content>
</paragraph>
</body></act></akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-06-19"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(
        json.dumps(
            {
                "event_id": "evt_cbic_b852babb7c93019c",
                "operation": "SUBSTITUTE",
                "status": "needs_review",
                "source": {"document_id": "/in/union/notifications/cbic/central-tax/2017/22-2017", "publication_date": "2017-08-17"},
                "legal_time": {"applicability_start": "2017-08-17"},
                "target": {
                    "work_id": "/in/union/rules/cgst-rules-2017",
                    "component_id": "/in/union/rules/cgst-rules-2017/rule/61",
                    "anchor_text": "specify that",
                },
                "payload": {
                    "old_text": "specify that",
                    "new_text": "specify the manner and conditions subject to which the",
                },
                "validation": {
                    "target_resolved": True,
                    "anchor_resolved": False,
                    "date_resolved": True,
                    "source_span_verified": True,
                    "materializable": False,
                },
                "evidence": {
                    "source_span": {"start": 1, "text_hash": "rule61"},
                    "excerpt": (
                        "with effect from the 1st day of July, 2017, in sub-rule (5), "
                        "for the words \"specify that\", the words \"specify the manner "
                        "and conditions subject to which the\" shall be substituted"
                    ),
                },
                "review": {"required": True, "review_reasons": ["anchor_not_resolved"]},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["coverage_gap_count"] == 0
    assert manifest["applied_count"] == 1
    versions = [
        json.loads(line)
        for line in (tmp_path / "out/node_versions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    subrule_versions = [
        row for row in versions if row["component_id"] == "/in/union/rules/cgst-rules-2017/rule/61/subrule/5"
    ]
    assert subrule_versions[-1]["valid_from"] == "2017-07-01"
    assert "specify the manner and conditions subject to which the return" in subrule_versions[-1]["text"]


def test_materializer_skips_rule_61_notification_extension_as_metadata_only(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso><act><body>
<article refersTo="/in/union/rules/cgst-rules-2017/rule/61">
<num>61</num><heading>Form and manner of submission of monthly return</heading><content><p>61. Form and manner.</p></content>
</article>
</body></act></akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-06-19"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(
        json.dumps(
            {
                "event_id": "evt_cbic_a41be90129af4539",
                "operation": "UNKNOWN",
                "status": "needs_review",
                "source": {"document_id": "/in/union/notifications/cbic/central-tax/2018/62-2018", "publication_date": "2018-11-29"},
                "legal_time": {"applicability_start": "2018-11-29"},
                "target": {
                    "work_id": "/in/union/rules/cgst-rules-2017",
                    "component_id": "/in/union/rules/cgst-rules-2017/rule/61",
                    "anchor_text": "read with sub-rule (5) of rule 61",
                },
                "payload": {},
                "validation": {
                    "target_resolved": True,
                    "anchor_resolved": False,
                    "date_resolved": True,
                    "source_span_verified": True,
                    "materializable": False,
                },
                "evidence": {
                    "source_span": {"start": 1, "text_hash": "metadata"},
                    "excerpt": (
                        "read with sub-rule (5) of rule 61 ... makes the following further "
                        "amendments in notification number 34/2018"
                    ),
                },
                "review": {"required": True, "review_reasons": ["unsupported_materializer_operation"]},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["applied_count"] == 0
    assert manifest["coverage_gap_count"] == 0


def test_materializer_routes_act_schedule_rows_to_pending_baseline_lane(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        "<akomaNtoso><act><body /></act></akomaNtoso>",
        encoding="utf-8",
    )
    (baseline_dir / "baseline_components.jsonl").write_text("", encoding="utf-8")
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/acts/cgst-act-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-04-12"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(
        json.dumps(
            {
                "event_id": "evt_schedule_iii",
                "operation": "UNKNOWN",
                "status": "needs_review",
                "source": {"document_id": "/in/union/acts/source/finance-acts/2023/finance-act-2023"},
                "legal_time": {"applicability_start": "2023-10-01"},
                "target": {
                    "work_id": "/in/union/acts/cgst-act-2017",
                    "component_id": "/in/union/acts/cgst-act-2017",
                },
                "payload": {
                    "triage_lane": "schedule_lane_pending_baseline",
                    "schedule_lane_pending_baseline": True,
                },
                "evidence": {
                    "source_span": {"start": 0, "text_hash": "schedule"},
                    "excerpt": "In Schedule III to the Central Goods and Services Tax Act, paragraphs 7 and 8 shall be inserted.",
                },
                "review": {"required": True, "review_reasons": ["schedule_lane_pending_baseline"]},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    manifest = materialize_versions(
        target_work="/in/union/acts/cgst-act-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["applied_count"] == 0
    assert manifest["coverage_gap_count"] == 0
    assert manifest["schedule_lane_pending_baseline_count"] == 1
    assert manifest["schedule_lane_pending_baseline_events"][0]["event_id"] == "evt_schedule_iii"


def test_materializer_routes_non_target_act_rows_out_of_scope(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        "<akomaNtoso><act name=\"cgst-act-2017\"><body /></act></akomaNtoso>",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/acts/cgst-act-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-04-12"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(
        json.dumps(
            {
                "event_id": "evt_igst_section_16",
                "operation": "SUBSTITUTE",
                "status": "needs_review",
                "source": {"document_id": "/in/union/acts/source/finance-acts/2024/finance-act-2024"},
                "legal_time": {"applicability_start": "2024-11-01"},
                "target": {
                    "work_id": "/in/union/acts/cgst-act-2017",
                    "component_id": "/in/union/acts/cgst-act-2017/section/16",
                },
                "payload": {
                    "triage_lane": "act_out_of_scope",
                    "act_out_of_scope": True,
                    "act_out_of_scope_reason": "non_target_act_reference",
                },
                "evidence": {
                    "source_span": {"start": 0, "text_hash": "igst"},
                    "excerpt": "In section 16 of the Integrated Goods and Services Tax Act, words shall be substituted.",
                },
                "review": {"required": True, "review_reasons": ["act_out_of_scope"]},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    manifest = materialize_versions(
        target_work="/in/union/acts/cgst-act-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["applied_count"] == 0
    assert manifest["coverage_gap_count"] == 0
    assert manifest["act_out_of_scope_count"] == 1
    assert manifest["act_out_of_scope_events"][0]["event_id"] == "evt_igst_section_16"


def test_materializer_skips_already_reflected_rule_88c_row_mistargeted_to_rule_89(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso><act><body>
<article refersTo="/in/union/rules/cgst-rules-2017/rule/88c">
<num>88C</num><heading>Manner of dealing with difference</heading><content><p>88C. Statement furnished in FORM GSTR-1 or using the Invoice Furnishing Facility.</p></content>
</article>
<article refersTo="/in/union/rules/cgst-rules-2017/rule/89">
<num>89</num><heading>Application for refund</heading><content><p>89. Refund rule text.</p></content>
</article>
</body></act></akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-06-19"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "event_id": "evt_cbic_xml_cff41664511daf24",
                        "operation": "SPLICE",
                        "status": "validated",
                        "source": {"publication_date": "2024-07-10"},
                        "legal_time": {"applicability_start": "2024-07-10"},
                        "target": {
                            "work_id": "/in/union/rules/cgst-rules-2017",
                            "component_id": "/in/union/rules/cgst-rules-2017/rule/88c",
                            "anchor_text": "FORM GSTR-1",
                        },
                        "payload": {"position": "after", "insert_text": ", as amended in FORM GSTR-1A if any,"},
                        "validation": {
                            "target_resolved": True,
                            "anchor_resolved": True,
                            "date_resolved": True,
                            "source_span_verified": True,
                            "materializable": True,
                        },
                        "evidence": {"source_span": {"start": 0, "text_hash": "abc"}},
                        "review": {"required": False, "review_reasons": []},
                    }
                ),
                json.dumps(
                    {
                        "event_id": "evt_cbic_803f696fd8e1d231",
                        "operation": "INSERT_CHILD",
                        "status": "needs_review",
                        "source": {"publication_date": "2024-07-10"},
                        "legal_time": {"applicability_start": "2024-07-10"},
                        "target": {
                            "work_id": "/in/union/rules/cgst-rules-2017",
                            "component_id": "/in/union/rules/cgst-rules-2017/rule/89/subrule/1",
                            "anchor_component_id": "/in/union/rules/cgst-rules-2017/rule/89",
                            "anchor_text": "insert a new sub-rule",
                        },
                        "payload": {
                            "parent_component_id": "/in/union/rules/cgst-rules-2017/rule/89",
                            "label": "1",
                            "node_type": "subrule",
                            "content": "",
                        },
                        "validation": {
                            "target_resolved": True,
                            "anchor_resolved": False,
                            "date_resolved": True,
                            "source_span_verified": True,
                            "materializable": False,
                        },
                        "evidence": {
                            "source_span": {"start": 1, "text_hash": "def"},
                            "excerpt": (
                                "16. In the said rules, in rule 88C, in sub-rule (1), after the "
                                "words, letters and figures FORM GSTR-1, the letters, words and "
                                "figures, as amended in FORM GSTR-1A if any, shall be inserted."
                            ),
                        },
                        "review": {
                            "required": True,
                            "review_reasons": [
                                "anchor_not_resolved",
                                "inserted_component_already_exists",
                                "llm_candidate_not_validated",
                            ],
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["applied_count"] == 1
    assert manifest["coverage_gap_count"] == 0
    assert manifest["applied_events"][0]["event_id"] == "evt_cbic_xml_cff41664511daf24"


def test_rule_materializer_routes_form_substitutions_out_of_rules_gaps(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso><act><body><article refersTo="/in/union/rules/cgst-rules-2017/rule/100">
<num>100</num><heading>Assessment in certain cases</heading><content><p>100. Assessment in certain cases.</p></content>
</article></body></act></akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-06-19"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(
        json.dumps(
            {
                "event_id": "evt_rule_100_form",
                "operation": "UNKNOWN",
                "status": "needs_review",
                "source": {"publication_date": "2019-03-29"},
                "legal_time": {"applicability_start": "2019-04-01"},
                "target": {
                    "work_id": "/in/union/rules/cgst-rules-2017",
                    "component_id": "/in/union/rules/cgst-rules-2017/rule/100",
                },
                "payload": {},
                "evidence": {
                    "excerpt": (
                        "With effect from 1st April, 2019, in the said rules, for FORM GST "
                        "ASMT-13, the following FORM shall be substituted, namely:- ..."
                    )
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["coverage_gap_count"] == 0
    assert manifest["forms_lane_routed_count"] == 1
    assert manifest["forms_lane_routed_events"][0]["event_id"] == "evt_rule_100_form"


def test_rule_materializer_routes_rule_table_mutations_out_of_rules_gaps(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso><act><body><article refersTo="/in/union/rules/cgst-rules-2017/rule/7">
<num>7</num><heading>Rate of tax of the composition levy</heading><content><p>7. Rate of tax of the composition levy.- The category of registered persons specified in column (2) of the Table below shall pay tax at the rate specified in column (3) of the said Table:- Table.</p></content>
</article></body></act></akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-06-19"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")
    events_path = tmp_path / "events.jsonl"
    events = [
        {
            "event_id": "evt_rule7_validated_text",
            "operation": "SPLICE",
            "status": "validated",
            "source": {"publication_date": "2018-01-23"},
            "legal_time": {"applicability_start": "2018-01-01"},
            "target": {
                "work_id": "/in/union/rules/cgst-rules-2017",
                "component_id": "/in/union/rules/cgst-rules-2017/rule/7",
                "anchor_text": "Table.",
            },
            "payload": {"position": "after", "insert_text": " Applied text."},
            "validation": {
                "target_resolved": True,
                "anchor_resolved": True,
                "date_resolved": True,
                "source_span_verified": True,
                "materializable": True,
            },
            "evidence": {
                "source_span": {"start": 0, "text_hash": "validated"},
                "excerpt": (
                    "in rule 7, after the word Table, words shall be inserted; "
                    "in FORM GST CMP-01, in the Table, text shall be substituted"
                ),
            },
            "review": {"required": False, "review_reasons": []},
        },
        {
            "event_id": "evt_rule7_table_cell",
            "operation": "SUBSTITUTE",
            "status": "needs_review",
            "source": {"publication_date": "2018-01-23"},
            "legal_time": {"applicability_start": "2018-01-01"},
            "target": {
                "work_id": "/in/union/rules/cgst-rules-2017",
                "component_id": "/in/union/rules/cgst-rules-2017/rule/7",
                "anchor_text": "one per cent.",
            },
            "payload": {"old_text": "one per cent.", "new_text": "half per cent. of the turnover"},
            "evidence": {
                "excerpt": (
                    "with effect from 1st January, 2018, in rule 7, in the Table, "
                    "in Sl. No. 1, in column number (3), for the words \"one per cent.\", "
                    "the words \"half per cent. of the turnover\" shall be substituted"
                )
            },
            "review": {"required": True, "review_reasons": ["anchor_not_resolved"]},
        },
        {
            "event_id": "evt_rule7_whole_table",
            "operation": "UNKNOWN",
            "status": "needs_review",
            "source": {"publication_date": "2020-06-24"},
            "legal_time": {"applicability_start": "2020-04-01"},
            "target": {
                "work_id": "/in/union/rules/cgst-rules-2017",
                "component_id": "/in/union/rules/cgst-rules-2017/rule/7",
                "anchor_text": "Table",
            },
            "payload": {},
            "evidence": {
                "excerpt": (
                "In the Central Goods and Services Tax Rules, 2017, in rule 7, "
                "for the Table, the following Table shall be substituted, namely:-"
                )
            },
            "review": {"required": True, "review_reasons": ["unsupported_materializer_operation"]},
        },
        {
            "event_id": "evt_rule7_table_column",
            "operation": "SUBSTITUTE",
            "status": "needs_review",
            "source": {"publication_date": "2022-07-31"},
            "legal_time": {"applicability_start": "2022-07-01"},
            "target": {
                "work_id": "/in/union/rules/cgst-rules-2017",
                "component_id": "/in/union/rules/cgst-rules-2017/rule/7",
                "anchor_text": "(a)",
            },
            "payload": {
                "old_text": "(a)",
                "new_text": "(a)",
            },
            "evidence": {
                "excerpt": (
                    "with effect from 1st July, 2022, in rule 7, in column (2) of the table, "
                    "the brackets, letters and figures “(a)” the words “(aa)” shall be substituted"
                )
            },
            "review": {"required": True, "review_reasons": ["anchor_not_resolved"]},
        },
    ]
    events_path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    assert manifest["coverage_gap_count"] == 0
    assert manifest["applied_count"] == 1
    assert manifest["rules_table_lane_routed_count"] == 3
    assert {event["event_id"] for event in manifest["rules_table_lane_routed_events"]} == {
        "evt_rule7_table_cell",
        "evt_rule7_whole_table",
        "evt_rule7_table_column",
    }


def test_confidence_tiers_use_portal_missing_notification_blocker(tmp_path):
    from src.legal_corpus.confidence_tiers import compute_confidence_tiers

    node_versions = tmp_path / "node_versions.jsonl"
    node_versions.write_text(
        json.dumps(
            {
                "component_id": "/in/union/rules/cgst-rules-2017/rule/89",
                "valid_from": "2017-06-19",
                "text": "Rule 89",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    gaps = tmp_path / "coverage_gaps.json"
    gaps.write_text(json.dumps({"gaps": []}), encoding="utf-8")
    portal = tmp_path / "portal.json"
    portal.write_text(
        json.dumps(
            {
                "rules": {"89": {"portal_completeness_status": "incomplete"}},
                "missing_source_notifications": [
                    {
                        "rule": "89",
                        "component_id": "/in/union/rules/cgst-rules-2017/rule/89",
                        "notification_ref": "16/2020",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = compute_confidence_tiers(
        node_versions_path=node_versions,
        coverage_gaps_path=gaps,
        portal_completeness_path=portal,
    )

    detail = result["component_details"]["/in/union/rules/cgst-rules-2017/rule/89"]
    assert detail["tier"] == "D"
    assert detail["portal_completeness_status"] == "incomplete"
    assert detail["tier_blockers"][0]["reason"] == "missing_source_notification"


def test_confidence_tiers_consume_component_reconciliation_outcomes(tmp_path):
    from src.legal_corpus.confidence_tiers import compute_confidence_tiers

    node_versions = tmp_path / "node_versions.jsonl"
    node_versions.write_text(
        "\n".join(
            json.dumps({"component_id": cid, "valid_from": "2017-06-19", "text": cid})
            for cid in [
                "/in/union/rules/cgst-rules-2017/rule/1",
                "/in/union/rules/cgst-rules-2017/rule/2",
                "/in/union/rules/cgst-rules-2017/rule/3",
                "/in/union/rules/cgst-rules-2017/rule/4",
                "/in/union/rules/cgst-rules-2017/rule/5",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    gaps = tmp_path / "coverage_gaps.json"
    gaps.write_text(json.dumps({"gaps": []}), encoding="utf-8")
    reconciliation = tmp_path / "reconciliation.json"
    reconciliation.write_text(
        json.dumps(
            {
                "component_outcomes": {
                    "/in/union/rules/cgst-rules-2017/rule/1": {
                        "component_id": "/in/union/rules/cgst-rules-2017/rule/1",
                        "status": "format_only_match",
                    },
                    "/in/union/rules/cgst-rules-2017/rule/2": {
                        "component_id": "/in/union/rules/cgst-rules-2017/rule/2",
                        "status": "omitted_correct",
                    },
                    "/in/union/rules/cgst-rules-2017/rule/3": {
                        "component_id": "/in/union/rules/cgst-rules-2017/rule/3",
                        "status": "checkpoint_likely_wrong",
                    },
                    "/in/union/rules/cgst-rules-2017/rule/4": {
                        "component_id": "/in/union/rules/cgst-rules-2017/rule/4",
                        "status": "true_substantive_mismatch",
                    },
                    "/in/union/rules/cgst-rules-2017/rule/5": {
                        "component_id": "/in/union/rules/cgst-rules-2017/rule/5",
                        "status": "checkpoint_source_incomplete",
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    result = compute_confidence_tiers(
        node_versions_path=node_versions,
        coverage_gaps_path=gaps,
        reconciliation_report_path=reconciliation,
    )

    details = result["component_details"]
    assert details["/in/union/rules/cgst-rules-2017/rule/1"]["tier"] == "A"
    assert details["/in/union/rules/cgst-rules-2017/rule/2"]["tier"] == "A"
    assert details["/in/union/rules/cgst-rules-2017/rule/3"]["tier"] == "B"
    assert details["/in/union/rules/cgst-rules-2017/rule/4"]["tier"] == "C"
    assert details["/in/union/rules/cgst-rules-2017/rule/5"]["tier"] == "D"


def test_confidence_tiers_do_not_block_applied_needs_review_events(tmp_path):
    from src.legal_corpus.confidence_tiers import compute_confidence_tiers

    component_id = "/in/union/rules/cgst-rules-2017/rule/130"
    child_id = f"{component_id}/subrule/2"
    node_versions = tmp_path / "node_versions.jsonl"
    node_versions.write_text(
        json.dumps({"component_id": component_id, "valid_from": "2017-06-19", "text": "Rule 130"}) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "materialization_manifest.json").write_text(
        json.dumps(
            {
                "applied_events": [
                    {
                        "event_id": "evt_rule_130_applied",
                        "operation": "SUBSTITUTE",
                        "changed_components": [child_id],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    gaps = tmp_path / "coverage_gaps.json"
    gaps.write_text(json.dumps({"gaps": []}), encoding="utf-8")
    reconciliation = tmp_path / "reconciliation.json"
    reconciliation.write_text(
        json.dumps(
            {
                "component_outcomes": {
                    component_id: {
                        "component_id": component_id,
                        "status": "true_substantive_mismatch",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    events = tmp_path / "events.jsonl"
    events.write_text(
        json.dumps(
            {
                "event_id": "evt_rule_130_applied",
                "operation": "SUBSTITUTE",
                "status": "needs_review",
                "target": {"component_id": child_id},
                "payload": {"old_text": "Director General of Safeguards", "new_text": "Director General of Anti-profiteering"},
                "review": {
                    "required": True,
                    "review_reasons": [
                        "compound_block_contains_multiple_amendments",
                        "context_recovered_target_pending_validation",
                    ],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = compute_confidence_tiers(
        node_versions_path=node_versions,
        coverage_gaps_path=gaps,
        reconciliation_report_path=reconciliation,
        amendment_events_path=events,
    )

    detail = result["component_details"][component_id]
    assert detail["tier"] == "C"
    assert detail["has_needs_review_events"] is False
    assert [blocker["reason"] for blocker in detail["tier_blockers"]] == ["reconciliation_mismatch"]


def test_confidence_tiers_do_not_block_applied_insert_sibling_anchor(tmp_path):
    from src.legal_corpus.confidence_tiers import compute_confidence_tiers

    anchor_id = "/in/union/rules/cgst-rules-2017/rule/96b"
    inserted_id = "/in/union/rules/cgst-rules-2017/rule/96c"
    node_versions = tmp_path / "node_versions.jsonl"
    node_versions.write_text(
        json.dumps({"component_id": anchor_id, "valid_from": "2020-03-23", "text": "Rule 96B"})
        + "\n"
        + json.dumps({"component_id": inserted_id, "valid_from": "2026-05-19", "text": "Rule 96C"})
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "materialization_manifest.json").write_text(
        json.dumps(
            {
                "applied_events": [
                    {
                        "event_id": "evt_rule_96c_inserted",
                        "operation": "INSERT_SIBLING",
                        "changed_components": [inserted_id],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    gaps = tmp_path / "coverage_gaps.json"
    gaps.write_text(json.dumps({"gaps": []}), encoding="utf-8")
    reconciliation = tmp_path / "reconciliation.json"
    reconciliation.write_text(
        json.dumps(
            {
                "component_outcomes": {
                    anchor_id: {
                        "component_id": anchor_id,
                        "status": "true_substantive_mismatch",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    events = tmp_path / "events.jsonl"
    events.write_text(
        json.dumps(
            {
                "event_id": "evt_rule_96c_inserted",
                "operation": "INSERT_SIBLING",
                "status": "needs_review",
                "target": {
                    "component_id": anchor_id,
                    "anchor_component_id": anchor_id,
                },
                "payload": {
                    "anchor_component_id": anchor_id,
                    "label": "Rule 96C",
                    "content": "Rule 96C text",
                },
                "review": {
                    "required": True,
                    "review_reasons": [
                        "anchor_not_resolved",
                        "llm_candidate_not_validated",
                        "context_recovered_target_pending_validation",
                    ],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = compute_confidence_tiers(
        node_versions_path=node_versions,
        coverage_gaps_path=gaps,
        reconciliation_report_path=reconciliation,
        amendment_events_path=events,
    )

    detail = result["component_details"][anchor_id]
    assert detail["tier"] == "C"
    assert detail["has_needs_review_events"] is False
    assert [blocker["reason"] for blocker in detail["tier_blockers"]] == ["reconciliation_mismatch"]


def test_confidence_tiers_do_not_block_already_reflected_events(tmp_path):
    from src.legal_corpus.confidence_tiers import compute_confidence_tiers

    component_id = "/in/union/rules/cgst-rules-2017/rule/9"
    node_versions = tmp_path / "node_versions.jsonl"
    node_versions.write_text(
        json.dumps({"component_id": component_id, "valid_from": "2017-06-19", "text": "Rule 9 text with seven"})
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "materialization_manifest.json").write_text(
        json.dumps(
            {
                "applied_events": [],
                "already_reflected_events": [
                    {
                        "event_id": "evt_rule_9_reflected",
                        "operation": "SUBSTITUTE",
                        "skip_reason": "already_reflected",
                        "target": {"component_id": component_id},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    gaps = tmp_path / "coverage_gaps.json"
    gaps.write_text(json.dumps({"gaps": []}), encoding="utf-8")
    reconciliation = tmp_path / "reconciliation.json"
    reconciliation.write_text(
        json.dumps(
            {
                "component_outcomes": {
                    component_id: {
                        "component_id": component_id,
                        "status": "true_substantive_mismatch",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    events = tmp_path / "events.jsonl"
    events.write_text(
        json.dumps(
            {
                "event_id": "evt_rule_9_reflected",
                "operation": "SUBSTITUTE",
                "status": "needs_review",
                "target": {"component_id": component_id},
                "payload": {"old_text": "three", "new_text": "seven"},
                "review": {
                    "required": True,
                    "review_reasons": [
                        "context_recovered_target_pending_validation",
                        "document_scope_target_not_materializable",
                        "same_effective_date_conflict",
                        "unsafe_generic_substitution_anchor",
                    ],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = compute_confidence_tiers(
        node_versions_path=node_versions,
        coverage_gaps_path=gaps,
        reconciliation_report_path=reconciliation,
        amendment_events_path=events,
    )

    detail = result["component_details"][component_id]
    assert detail["tier"] == "C"
    assert detail["has_needs_review_events"] is False
    assert [blocker["reason"] for blocker in detail["tier_blockers"]] == ["reconciliation_mismatch"]


def test_confidence_tiers_do_not_make_manifest_context_unresolved_mismatches_tier_d(tmp_path):
    from src.legal_corpus.confidence_tiers import compute_confidence_tiers

    component_id = "/in/union/rules/cgst-rules-2017/rule/87"
    child_id = f"{component_id}/subrule/3"
    node_versions = tmp_path / "node_versions.jsonl"
    node_versions.write_text(
        json.dumps({"component_id": component_id, "valid_from": "2017-06-19", "text": "Rule 87"}) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "materialization_manifest.json").write_text(
        json.dumps(
            {
                "applied_events": [],
                "context_unresolved_events": [
                    {
                        "event_id": "evt_rule_87_context_gap",
                        "operation": "SUBSTITUTE",
                        "skip_reason": "context_unresolved",
                        "target": {"component_id": child_id},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    gaps = tmp_path / "coverage_gaps.json"
    gaps.write_text(json.dumps({"gaps": []}), encoding="utf-8")
    reconciliation = tmp_path / "reconciliation.json"
    reconciliation.write_text(
        json.dumps(
            {
                "component_outcomes": {
                    component_id: {
                        "component_id": component_id,
                        "status": "true_substantive_mismatch",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    events = tmp_path / "events.jsonl"
    events.write_text(
        json.dumps(
            {
                "event_id": "evt_rule_87_context_gap",
                "operation": "SUBSTITUTE",
                "status": "needs_review",
                "target": {"component_id": child_id},
                "payload": {"old_text": "section 14", "new_text": "section 14A"},
                "review": {
                    "required": True,
                    "review_reasons": [
                        "anchor_not_resolved",
                        "missing_prior_context",
                    ],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = compute_confidence_tiers(
        node_versions_path=node_versions,
        coverage_gaps_path=gaps,
        reconciliation_report_path=reconciliation,
        amendment_events_path=events,
    )

    detail = result["component_details"][component_id]
    assert detail["tier"] == "C"
    assert detail["has_needs_review_events"] is False
    assert [blocker["reason"] for blocker in detail["tier_blockers"]] == ["reconciliation_mismatch"]


def test_confidence_tiers_do_not_block_misrouted_events_applied_elsewhere(tmp_path):
    from src.legal_corpus.confidence_tiers import compute_confidence_tiers

    misrouted_id = "/in/union/rules/cgst-rules-2017/rule/88d"
    applied_id = "/in/union/rules/cgst-rules-2017/rule/89/subrule/4"
    node_versions = tmp_path / "node_versions.jsonl"
    node_versions.write_text(
        json.dumps({"component_id": misrouted_id, "valid_from": "2023-08-04", "text": "Rule 88D"})
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "materialization_manifest.json").write_text(
        json.dumps(
            {
                "applied_events": [
                    {
                        "event_id": "evt_rule_89_omit_misrouted",
                        "operation": "OMIT",
                        "changed_components": [applied_id],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    gaps = tmp_path / "coverage_gaps.json"
    gaps.write_text(json.dumps({"gaps": []}), encoding="utf-8")
    reconciliation = tmp_path / "reconciliation.json"
    reconciliation.write_text(
        json.dumps(
            {
                "component_outcomes": {
                    misrouted_id: {
                        "component_id": misrouted_id,
                        "status": "true_substantive_mismatch",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    events = tmp_path / "events.jsonl"
    events.write_text(
        json.dumps(
            {
                "event_id": "evt_rule_89_omit_misrouted",
                "operation": "OMIT",
                "status": "needs_review",
                "target": {"component_id": f"{misrouted_id}/subrule/3"},
                "payload": {"omit_text": "formula words"},
                "review": {
                    "required": True,
                    "review_reasons": [
                        "anchor_not_resolved",
                        "context_recovered_target_pending_validation",
                        "same_effective_date_conflict",
                        "target_component_outside_work",
                    ],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = compute_confidence_tiers(
        node_versions_path=node_versions,
        coverage_gaps_path=gaps,
        reconciliation_report_path=reconciliation,
        amendment_events_path=events,
    )

    detail = result["component_details"][misrouted_id]
    assert detail["tier"] == "C"
    assert detail["has_needs_review_events"] is False
    assert [blocker["reason"] for blocker in detail["tier_blockers"]] == ["reconciliation_mismatch"]


def test_act_pipeline_audit_reports_parallel_track_blockers(tmp_path):
    from src.legal_corpus.act_pipeline_audit import audit_act_pipeline

    events_path = tmp_path / "events.jsonl"
    events = [
        {
            "event_id": "evt_meta",
            "operation": "UNKNOWN",
            "status": "needs_review",
            "target": {"component_id": "/in/union/acts/cgst-act-2017"},
            "review": {"review_reasons": ["missing_source_text"]},
            "evidence": {"excerpt": "short title and commencement"},
        },
        {
            "event_id": "evt_new_section",
            "operation": "SPLICE",
            "status": "needs_review",
            "target": {"component_id": "/in/union/acts/cgst-act-2017/section/101a"},
            "review": {"review_reasons": ["target_not_resolved"]},
            "evidence": {"excerpt": "After section 101, the following section shall be inserted."},
        },
        {
            "event_id": "evt_schedule",
            "operation": "UNKNOWN",
            "status": "needs_review",
            "target": {"component_id": "/in/union/acts/cgst-act-2017"},
            "payload": {"triage_lane": "schedule_lane_pending_baseline", "schedule_lane_pending_baseline": True},
            "review": {"review_reasons": ["schedule_lane_pending_baseline"]},
            "evidence": {"excerpt": "In Schedule III to the Central Goods and Services Tax Act, paragraph 7 shall be inserted."},
        },
        {
            "event_id": "evt_igst",
            "operation": "SUBSTITUTE",
            "status": "needs_review",
            "target": {"component_id": "/in/union/acts/cgst-act-2017/section/16"},
            "payload": {"triage_lane": "act_out_of_scope", "act_out_of_scope": True},
            "review": {"review_reasons": ["act_out_of_scope"]},
            "evidence": {"excerpt": "In section 16 of the Integrated Goods and Services Tax Act, words shall be substituted."},
        },
    ]
    events_path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
    gaps_path = tmp_path / "gaps.json"
    gaps_path.write_text(
        json.dumps(
            {
                "gaps": [
                    {
                        "event_id": "evt_new_section",
                        "operation": "SPLICE",
                        "skip_reason": "apply_failed: Target component missing: /in/union/acts/cgst-act-2017/section/101a",
                        "target": {"component_id": "/in/union/acts/cgst-act-2017/section/101a"},
                        "evidence": {"excerpt": "After section 101, the following section shall be inserted."},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({"event_count": 2, "applied_count": 0, "coverage_gap_count": 1}), encoding="utf-8")
    baseline_components_path = tmp_path / "baseline_components.jsonl"
    baseline_components_path.write_text(
        "\n".join(
            json.dumps(row)
            for row in [
                {
                    "component_id": "/in/union/acts/cgst-act-2017/section/1",
                    "label": "1",
                    "heading": "Short title",
                    "text": "Finance Acts|12345678-1234-1234-1234-123456789abc contaminated text",
                    "blocked": True,
                },
                {
                    "component_id": "/in/union/acts/cgst-act-2017/section/2",
                    "label": "2",
                    "heading": "Definitions",
                    "text": "Board means the Central Board of Indirect Taxes and Customs.",
                    "blocked": False,
                },
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    baseline_reconciliation_path = tmp_path / "baseline_reconciliation.json"
    baseline_reconciliation_path.write_text(
        json.dumps({"blocked_count": 1, "blocked_components": ["/in/union/acts/cgst-act-2017/section/1"]}),
        encoding="utf-8",
    )
    confidence_path = tmp_path / "confidence.json"
    confidence_path.write_text(
        json.dumps(
            {
                "component_details": {
                    "/in/union/acts/cgst-act-2017/section/1": {"tier": "D", "tier_blockers": [{"reason": "baseline_contaminated"}]},
                    "/in/union/rules/cgst-rules-2017/rule/1": {"tier": "A"},
                }
            }
        ),
        encoding="utf-8",
    )
    output_path = tmp_path / "act_pipeline_audit.json"

    report = audit_act_pipeline(
        events_path=events_path,
        coverage_gaps_path=gaps_path,
        materialization_manifest_path=manifest_path,
        baseline_components_path=baseline_components_path,
        baseline_reconciliation_path=baseline_reconciliation_path,
        confidence_tiers_path=confidence_path,
        output_path=output_path,
        sample_limit=5,
    )

    assert output_path.exists()
    assert report["summary"]["coverage_gap_count"] == 1
    assert report["summary"]["metadata_only_candidate_count"] == 1
    assert report["summary"]["schedule_lane_pending_baseline_count"] == 1
    assert report["summary"]["act_out_of_scope_count"] == 1
    assert report["summary"]["confidence_tier_counts"] == {"D": 1}
    assert report["baseline_contamination_audit"]["blocked_count"] == 1
    assert report["baseline_contamination_audit"]["noise_pattern_candidate_count"] == 1
    assert report["metadata_only_audit"]["candidate_count"] == 1
    assert report["schedule_lane_audit"]["pending_baseline_count"] == 1
    assert report["act_out_of_scope_audit"]["event_count"] == 1
    assert report["missing_section_creation_audit"]["event_candidate_count"] == 1
    assert report["missing_section_creation_audit"]["gap_candidate_count"] == 1
    assert report["confidence_tier_integration"]["act_component_count"] == 1
    assert report["confidence_tier_integration"]["tier_counts"] == {"D": 1}


def test_corrigendum_materializer_writes_temporal_ledger(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "baseline.xml").write_text(
        """
<akomaNtoso><act><body><article refersTo="/in/union/rules/cgst-rules-2017/rule/89">
<num>89</num><heading>Application for refund</heading><content><p>89. paragraph 5 applies.</p></content>
</article></body></act></akomaNtoso>
""",
        encoding="utf-8",
    )
    registry_data = json.loads((ROOT / "data/Law/statute_identity_registry.json").read_text(encoding="utf-8"))
    for work in registry_data["works"]:
        if work["work_id"] == "/in/union/rules/cgst-rules-2017":
            work["baseline_path"] = str(baseline_dir)
            work["base_as_of"] = "2017-06-19"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(
        json.dumps(
            {
                "event_id": "evt_corr",
                "operation": "CORRIGENDUM",
                "status": "validated",
                "source": {
                    "document_id": "/in/union/notifications/cbic/central-tax/2017/16-2017-corrigendum",
                    "publication_date": "2017-09-01",
                },
                "legal_time": {"applicability_start": "2017-09-01"},
                "target": {
                    "work_id": "/in/union/rules/cgst-rules-2017",
                    "component_id": "/in/union/rules/cgst-rules-2017/rule/89",
                },
                "payload": {
                    "text": "In the notification No. 16/2017-Central Tax, in rule 89, for \u201cparagraph 5\u201d read \u201cparagraphs 3.20 and 3.21\u201d."
                },
                "validation": {"materializable": True},
                "evidence": {"source_span": {"start": 0}},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    manifest = materialize_versions(
        target_work="/in/union/rules/cgst-rules-2017",
        events_path=events_path,
        registry_path=registry_path,
        corpus_dir=tmp_path / "corpus",
        output_dir=tmp_path / "out",
        write_snapshots=False,
        refresh_baseline=False,
    )

    rows = [json.loads(line) for line in (tmp_path / "out/corrigendum_ledger.jsonl").read_text().splitlines()]
    assert manifest["corrigendum_ledger_count"] == 1
    assert rows[0]["corrected_notification_refs"] == ["16/2017"]
    assert rows[0]["corrigendum_effect_date"] == "2017-09-01"
    assert rows[0]["retrospective"] is False


def test_evidence_bundle_tier_a_requires_complete_event_provenance(tmp_path):
    from src.legal_corpus.evidence_bundles import build_evidence_bundle

    component_id = "/in/union/rules/cgst-rules-2017/rule/89"
    version_dir = tmp_path / "versions"
    version_dir.mkdir()
    baseline_path = tmp_path / "baseline_components.jsonl"
    baseline_path.write_text(
        json.dumps(
            {
                "component_id": component_id,
                "label": "89",
                "heading": "Application for refund",
                "text": "Baseline rule 89 text",
                "source": {"document_id": "baseline-rules"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (version_dir / "node_versions.jsonl").write_text(
        json.dumps(
            {
                "version_id": "rule89@2020-01-01",
                "component_id": component_id,
                "valid_from": "2020-01-01",
                "valid_to": None,
                "applicability_start": "2020-01-01",
                "text": "Materialized rule 89 text",
                "text_sha256": "text-hash",
                "created_by_event_id": "evt_rule89",
                "event_chain": ["evt_rule89"],
                "source_basis": {
                    "source_document_id": "notif-1",
                    "source_span": {"start": 10, "end": 30, "sha256": "span-hash"},
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (version_dir / "materialization_manifest.json").write_text(
        json.dumps(
            {
                "applied_events": [
                    {
                        "event_id": "evt_rule89",
                        "operation": "SUBSTITUTE",
                        "changed_components": [component_id],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (version_dir / "coverage_gaps.json").write_text(json.dumps({"gaps": []}), encoding="utf-8")
    (version_dir / "corrigendum_ledger.jsonl").write_text("", encoding="utf-8")
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(
        json.dumps(
            {
                "event_id": "evt_rule89",
                "operation": "SUBSTITUTE",
                "status": "validated",
                "source": {"document_id": "notif-1", "url": "https://example.test/notif-1.pdf"},
                "legal_time": {"applicability_start": "2020-01-01"},
                "target": {"component_id": component_id},
                "payload": {"old_text": "old", "new_text": "new"},
                "evidence": {
                    "excerpt": "for old read new",
                    "source_span": {"start": 10, "end": 30, "sha256": "span-hash"},
                },
                "validation": {"materializable": True},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    confidence_path = tmp_path / "confidence.json"
    confidence_path.write_text(
        json.dumps({"component_details": {component_id: {"tier": "A", "tier_blockers": []}}}),
        encoding="utf-8",
    )
    portal_path = tmp_path / "portal.json"
    portal_path.write_text(json.dumps({"rules": {"89": {"portal_completeness_status": "complete"}}}), encoding="utf-8")

    bundle = build_evidence_bundle(
        component_id,
        from_date="2019-01-01",
        to_date="2021-01-01",
        version_dir=version_dir,
        amendment_events_path=events_path,
        baseline_components_path=baseline_path,
        confidence_tiers_path=confidence_path,
        portal_completeness_path=portal_path,
    )

    assert bundle["deterministic_validation"]["bundle_citable"] is True
    assert bundle["deterministic_validation"]["events_missing_required_provenance"] == []
    assert bundle["amendment_events"][0]["event_id"] == "evt_rule89"
    assert bundle["amendment_events"][0]["source_span_hash"] == "span-hash"


def test_evidence_bundle_tier_d_html_explains_not_citable_blockers(tmp_path):
    from src.legal_corpus.evidence_bundles import build_evidence_bundle, render_evidence_bundle_html

    component_id = "/in/union/rules/cgst-rules-2017/rule/46"
    version_dir = tmp_path / "versions"
    version_dir.mkdir()
    baseline_path = tmp_path / "baseline_components.jsonl"
    baseline_path.write_text(
        json.dumps({"component_id": component_id, "label": "46", "text": "Baseline"}) + "\n",
        encoding="utf-8",
    )
    (version_dir / "node_versions.jsonl").write_text(
        json.dumps({"version_id": "rule46@base", "component_id": component_id, "valid_from": "2017-06-19", "text": "Baseline"})
        + "\n",
        encoding="utf-8",
    )
    (version_dir / "materialization_manifest.json").write_text(json.dumps({"applied_events": []}), encoding="utf-8")
    (version_dir / "coverage_gaps.json").write_text(
        json.dumps(
            {
                "gaps": [
                    {
                        "event_id": "evt_gap",
                        "operation": "SUBSTITUTE",
                        "skip_reason": "anchor_not_resolved for /in/union/rules/cgst-rules-2017/rule/46",
                        "target": {"component_id": component_id},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (version_dir / "corrigendum_ledger.jsonl").write_text("", encoding="utf-8")
    events_path = tmp_path / "events.jsonl"
    events_path.write_text("", encoding="utf-8")
    confidence_path = tmp_path / "confidence.json"
    confidence_path.write_text(
        json.dumps(
            {
                "component_details": {
                    component_id: {
                        "tier": "D",
                        "tier_blockers": [{"reason": "coverage_gap", "event_id": "evt_gap"}],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    portal_path = tmp_path / "portal.json"
    portal_path.write_text(json.dumps({"rules": {"46": {"portal_completeness_status": "incomplete"}}}), encoding="utf-8")

    bundle = build_evidence_bundle(
        component_id,
        from_date="2017-06-19",
        to_date="2026-06-17",
        version_dir=version_dir,
        amendment_events_path=events_path,
        baseline_components_path=baseline_path,
        confidence_tiers_path=confidence_path,
        portal_completeness_path=portal_path,
    )
    html = render_evidence_bundle_html(bundle)

    assert bundle["deterministic_validation"]["bundle_citable"] is False
    assert bundle["deterministic_validation"]["has_unresolved_blockers"] is True
    assert "Do not cite" in html
    assert "coverage_gap" in html


def test_evidence_bundle_preserves_corrigendum_event_payload_provenance(tmp_path):
    from src.legal_corpus.evidence_bundles import build_evidence_bundle

    component_id = "/in/union/rules/cgst-rules-2017/rule/89"
    version_dir = tmp_path / "versions"
    version_dir.mkdir()
    (tmp_path / "baseline_components.jsonl").write_text(
        json.dumps({"component_id": component_id, "label": "89", "text": "Baseline"}) + "\n",
        encoding="utf-8",
    )
    (version_dir / "node_versions.jsonl").write_text(
        json.dumps({"version_id": "v1", "component_id": component_id, "valid_from": "2020-01-01", "text": "Corrected text"})
        + "\n",
        encoding="utf-8",
    )
    (version_dir / "materialization_manifest.json").write_text(
        json.dumps({"applied_events": [{"event_id": "evt_corrected", "changed_components": [component_id]}]}),
        encoding="utf-8",
    )
    (version_dir / "coverage_gaps.json").write_text(json.dumps({"gaps": []}), encoding="utf-8")
    (version_dir / "corrigendum_ledger.jsonl").write_text(
        json.dumps({"corrigendum_event_id": "evt_corr", "corrected_event_id": "evt_corrected"}) + "\n",
        encoding="utf-8",
    )
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(
        json.dumps(
            {
                "event_id": "evt_corrected",
                "operation": "SUBSTITUTE",
                "status": "validated",
                "source": {"document_id": "notif-original"},
                "legal_time": {"applicability_start": "2020-01-01"},
                "target": {"component_id": component_id},
                "payload": {
                    "old_text": "old",
                    "new_text": "corrected",
                    "corrigendum_applications": [
                        {
                            "corrigendum_event_id": "evt_corr",
                            "original_source_span": {"start": 1, "end": 2, "sha256": "original-hash"},
                            "corrigendum_source_span": {"start": 3, "end": 4, "sha256": "corrigendum-hash"},
                            "retrospective": True,
                        }
                    ],
                },
                "evidence": {"source_span": {"start": 1, "end": 2, "sha256": "original-hash"}},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    confidence_path = tmp_path / "confidence.json"
    confidence_path.write_text(json.dumps({"component_details": {component_id: {"tier": "B"}}}), encoding="utf-8")
    portal_path = tmp_path / "portal.json"
    portal_path.write_text(json.dumps({"rules": {"89": {"portal_completeness_status": "complete"}}}), encoding="utf-8")

    bundle = build_evidence_bundle(
        component_id,
        from_date="2019-01-01",
        to_date="2021-01-01",
        version_dir=version_dir,
        amendment_events_path=events_path,
        baseline_components_path=tmp_path / "baseline_components.jsonl",
        confidence_tiers_path=confidence_path,
        portal_completeness_path=portal_path,
    )

    payload_application = bundle["corrigendum_provenance"]["event_payload_applications"][0]["corrigendum_provenance"][0]
    assert payload_application["corrigendum_event_id"] == "evt_corr"
    assert payload_application["original_source_span"]["sha256"] == "original-hash"
    assert payload_application["corrigendum_source_span"]["sha256"] == "corrigendum-hash"


def test_surgical_gap_queue_prioritizes_focus_rules_and_preserves_source_hashes(tmp_path):
    from src.legal_corpus.surgical_gap_queue import build_surgical_gap_queue

    coverage = tmp_path / "coverage_gaps.json"
    coverage.write_text(
        json.dumps(
            {
                "gaps": [
                    {
                        "event_id": "evt_unvalidated",
                        "date": "2020-01-01",
                        "operation": "SUBSTITUTE",
                        "status": "needs_review",
                        "skip_reason": "event_status_not_validated",
                        "target": {"component_id": "/in/union/rules/cgst-rules-2017/rule/46"},
                        "source_span": {"start": 1, "end": 2, "text_hash": "skip-hash"},
                    },
                    {
                        "event_id": "evt_rule46_anchor",
                        "date": "2021-01-01",
                        "operation": "SPLICE",
                        "status": "validated",
                        "skip_reason": "apply_failed: Anchor not found: 'Provided that'",
                        "target": {"component_id": "/in/union/rules/cgst-rules-2017/rule/46"},
                        "source_document_id": "/in/union/notifications/cbic/central-tax/2021/1-2021",
                        "source_span": {"start": 10, "end": 30, "text_hash": "span-hash-46"},
                        "excerpt": "in rule 46, after Provided that...",
                    },
                    {
                        "event_id": "evt_rule21_seq",
                        "date": "2021-01-02",
                        "operation": "SUBSTITUTE",
                        "status": "validated",
                        "skip_reason": "same_effective_date_conflict",
                        "target": {"component_id": "/in/union/rules/cgst-rules-2017/rule/21"},
                        "source_span": {"start": 11, "end": 31, "sha256": "span-hash-21"},
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    report = build_surgical_gap_queue(
        coverage_gaps_path=coverage,
        output=tmp_path / "surgical_gap_queue.json",
        focus_rules=["46"],
    )

    assert (tmp_path / "surgical_gap_queue.json").exists()
    assert report["summary"]["total_gap_count"] == 3
    assert report["summary"]["surgical_gap_count"] == 2
    assert report["queue"][0]["event_id"] == "evt_rule46_anchor"
    assert report["queue"][0]["focus_rule"] is True
    assert report["queue"][0]["source_span_hash"] == "span-hash-46"
    assert "evt_unvalidated" not in {item["event_id"] for item in report["queue"]}
    assert report["summary"]["non_surgical_reason_counts"]["event_status_not_validated"] == 1


def test_materialize_guard_rejects_target_output_mismatch():
    from src.legal_corpus.version_snapshots import _validate_output_dir_for_target

    with pytest.raises(ValueError):
        _validate_output_dir_for_target(
            "/in/union/acts/cgst-act-2017",
            Path("derived/version_history/cgst-rules-2017"),
        )


def test_materialize_guard_allows_matching_target_output():
    from src.legal_corpus.version_snapshots import _validate_output_dir_for_target

    _validate_output_dir_for_target(
        "/in/union/rules/cgst-rules-2017",
        Path("derived/version_history/cgst-rules-2017"),
    )


def test_component_id_for_manifest_label_uses_section_for_acts():
    from src.legal_corpus.reconciliation import _component_id_for_manifest_label

    result = _component_id_for_manifest_label("/in/union/acts/cgst-act-2017", "Section 43A")
    assert "/section/43a" in result


def test_component_id_for_manifest_label_uses_rule_for_rules():
    from src.legal_corpus.reconciliation import _component_id_for_manifest_label

    result = _component_id_for_manifest_label("/in/union/rules/cgst-rules-2017", "Rule 42")
    assert "/rule/42" in result


def test_rate_compiler_backfills_serial_context_to_subitem_events():
    # Services-schedule amendments group a serial-number context marker
    # ("against serial number 7, in column (3),-") as its own clause, with
    # the operative sub-clauses ("(a) for item (i) ... shall be substituted")
    # following it. The sub-clauses do not repeat the serial number, so the
    # compiled event must inherit it from the context (cf. notification
    # 13/2018 S.No 7 item (i) which previously carried an empty sno).
    from src.legal_corpus.rate_schedule_compiler import compile_amendment_notification

    xml_path = (
        "corpus/in/union/notifications/cbic/central-tax-rate/2018/"
        "13-2018-central-tax-rate.xml"
    )
    events = compile_amendment_notification(xml_path)
    sno7_item_substitutions = [
        e for e in events
        if e.target_notification == "11/2017-ct-rate"
        and e.operation == "RATE_SUBSTITUTE_COLUMN"
        and str(e.payload.get("sno", "")) == "7"
        and (e.payload.get("item") in ("i", "v"))
    ]
    assert sno7_item_substitutions, (
        "13/2018 S.No 7 sub-item substitution events must carry sno=7 "
        "(serial context backfilled from the parent clause)"
    )
    # The item (i) substitution introduces the post-amendment restaurant text
    # without the older "or in any other manner whatsoever" wording.
    item_i = next(
        e for e in sno7_item_substitutions if e.payload.get("item") == "i"
    )
    assert "or in any other manner whatsoever" not in item_i.payload.get("new_value", "")
    assert "whether for consumption on or away from the premises" in item_i.payload.get("new_value", "")


def test_rate_materializer_substitute_column_item_is_surgical():
    # A RATE_SUBSTITUTE_COLUMN that targets a single item within a multi-item
    # column-3 description must replace only that sub-item, leaving sibling
    # items intact (regression: previously it clobbered the whole entry).
    from src.legal_corpus.rate_schedule_materializer import RateMaterializer, RateScheduleState

    base = {
        "notification_id": "test",
        "schedules": {
            "I": {
                "schedule_id": "I",
                "rate_pct": 0.0,
                "entries": [
                    {
                        "sno": "7.",
                        "tariff_item": "Heading 9963",
                        "description": (
                            "(Heading) (i) old item one text here. 6 - "
                            "(ii) old item two text here. 9 -"
                        ),
                        "is_omitted": False,
                    }
                ],
            }
        },
    }
    mat = RateMaterializer(base)
    assert isinstance(mat.schedules["I"], RateScheduleState)
    mat.apply_events([{
        "event_id": "evt_test",
        "operation": "RATE_SUBSTITUTE_COLUMN",
        "target_notification": "11/2017-ct-rate",
        "target_schedule": "I",
        "payload": {
            "sno": "7",
            "column": 3,
            "item": "i",
            "new_value": "(i) brand new item one text. 2.5",
        },
        "effective_date": "2018-07-26",
        "publication_date": "2018-07-26",
        "source_notification": "test",
        "source_cbic_no": "13/2018",
        "clause_ref": "a",
    }])
    desc = mat.schedules["I"].entries[0].description
    assert "brand new item one text" in desc
    # Sibling item (ii) must survive.
    assert "old item two text here" in desc


def test_rate_materializer_substitute_item_appends_when_marker_absent():
    # When a substituted sub-item marker is not yet present in the entry
    # (the item is being newly introduced), the materializer must append it
    # rather than overwriting the whole description and destroying existing
    # sibling items (regression: 27/2018 S.No 17 item (viii) clobbered
    # items (i)-(vii)).
    from src.legal_corpus.rate_schedule_materializer import RateMaterializer

    base = {
        "notification_id": "test",
        "schedules": {
            "I": {
                "schedule_id": "I",
                "rate_pct": 0.0,
                "entries": [
                    {
                        "sno": "17.",
                        "tariff_item": "Heading 9973",
                        "description": "(Heading) (i) first item. 6 - (ii) second item. 9 -",
                        "is_omitted": False,
                    }
                ],
            }
        },
    }
    mat = RateMaterializer(base)
    mat.apply_events([{
        "event_id": "evt_test",
        "operation": "RATE_SUBSTITUTE_ITEM",
        "target_notification": "11/2017-ct-rate",
        "target_schedule": "I",
        "payload": {
            "sno": "17",
            "item_id": "(viii)",
            "new_text": "(viii) a brand new eighth item. 9 -",
        },
        "effective_date": "2019-01-01",
        "publication_date": "2018-12-31",
        "source_notification": "test",
        "source_cbic_no": "27/2018",
        "clause_ref": "e",
    }])
    desc = mat.schedules["I"].entries[0].description
    assert "a brand new eighth item" in desc
    # Existing items must survive.
    assert "first item" in desc
    assert "second item" in desc


def test_rate_reconciliation_treats_checkpoint_subset_artifact_as_format_only():
    # A checkpoint entry that is a truncated or column-scrambled subset of the
    # correct materialized description (nearly every checkpoint token present
    # in the materialized text) is a checkpoint PDF parsing artifact, not a
    # substantive description mismatch (cf. 11/2017 S.No 9 truncated body and
    # S.No 17 column-bleed scramble).
    from src.legal_corpus.rate_reconciliation import _classify_pair

    checkpoint_entry = {
        "sno": "17",
        "tariff_item": "Heading 9973",
        "description": (
            "(Leasing or Intellectual Property (IP) right in respect of "
            "(i) Temporary or permanent transfer or permitting the use or "
            "enjoyment of goods other than Information Technology permitting "
            "the use or enjoyment of Intellectual Property (IP) right in "
            "respect of Information Technology software."
        ),
        "is_omitted": False,
    }
    materialized_entry = {
        "sno": "17",
        "tariff_item": "Heading 9973",
        "description": (
            "(Leasing or rental services, with or without operator) "
            "(i) Temporary or permanent transfer or permitting the use or "
            "enjoyment of Intellectual Property (IP) right in respect of "
            "goods other than Information Technology software. 6 - "
            "(ii) Temporary or permanent transfer or permitting the use or "
            "enjoyment of Intellectual Property (IP) right in respect of "
            "Information Technology software."
        ),
        "is_omitted": False,
    }
    classification, _ = _classify_pair(checkpoint_entry, materialized_entry)
    assert classification == "format_only_match"


def test_rate_reconciliation_keeps_real_wording_change_as_mismatch():
    # Guard: a genuine description wording change (where the checkpoint
    # introduces many tokens absent from the materialized text) must remain a
    # description_mismatch and not be absorbed by the subset-artifact check.
    # The pair below shares only the leading "(Heading) (i)" prefix so the
    # common-prefix heuristic does not fire either, isolating the token-subset
    # guard (cf. a real wording substitution where the materialized text
    # still holds an older phrasing than the checkpoint).
    from src.legal_corpus.rate_reconciliation import _classify_pair

    checkpoint_entry = {
        "sno": "7",
        "tariff_item": "Heading 9963",
        "description": (
            "(Accommodation services) (i) Hotel accommodation in rooms with "
            "declared tariff above five thousand rupees per day for lodging "
            "purposes including clubs, campsites or guest houses."
        ),
        "is_omitted": False,
    }
    materialized_entry = {
        "sno": "7",
        "tariff_item": "Heading 9963",
        "description": (
            "(Beverage services) (i) Supply of meals prepared and served by "
            "a restaurant holding a licence to serve alcoholic liquor for "
            "human consumption on the premises where such drink is supplied."
        ),
        "is_omitted": False,
    }
    classification, _ = _classify_pair(checkpoint_entry, materialized_entry)
    assert classification == "description_mismatch"
