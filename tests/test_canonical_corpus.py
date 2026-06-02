import json
import xml.etree.ElementTree as ET
from pathlib import Path

import src.legal_corpus.structure_parser as structure_parser
import src.legal_corpus.source_archive as source_archive
from src.legal_corpus.api_payload import build_api_payload, write_api_payload
from src.legal_corpus.amendments import apply_amendments, plan_amendments, promote_amended_corpus
from src.legal_corpus.batch_ingest import ingest_inventory, write_batch_ingest_report
from src.legal_corpus.diff import compare_corpora, write_diff_report
from src.legal_corpus.graph_index import build_neo4j_payload, rebuild_graph_index, write_neo4j_payload
from src.legal_corpus.forms import split_forms_archive
from src.legal_corpus.html_renderer import build_html_site, write_html_site
from src.legal_corpus.ingest import ingest_source_file
from src.legal_corpus.paths import expected_corpus_relative_path
from src.legal_corpus.quality import audit_corpus_quality, write_quality_report
from src.legal_corpus.query import export_text, list_documents, lookup_canonical_id, normalize_query_id
from src.legal_corpus.references import build_unresolved_reference_report, write_unresolved_reference_report
from src.legal_corpus.review import apply_batch_promotion, plan_batch_promotion, write_promotion_plan
from src.legal_corpus.renderer import canonicalize_legacy_reference, render_source_document, write_xml
from src.legal_corpus.search_index import build_search_records, read_search_index, search_index, search_records, write_search_index
from src.legal_corpus.seed import seed_from_existing_data
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
from src.legal_corpus.verification import run_verification


ROOT = Path(__file__).resolve().parents[1]


def test_canonicalize_legacy_reference():
    assert canonicalize_legacy_reference("CGST_Rules/Rule_10/SubRule_1") == (
        "/in/union/rules/cgst-rules-2017/rule/10/subrule/1"
    )
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
