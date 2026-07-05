import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, "src")

from legal_corpus.rate_schedule_materializer import RateMaterializer
from scripts import vlm_enhance_service_checkpoint as vlm


REPO_ROOT = Path(__file__).resolve().parents[1]
VLM_CACHE = REPO_ROOT / "derived" / "vlm_cache"
CHECKPOINT_2024 = (
    REPO_ROOT
    / "derived"
    / "version_history"
    / "rate-schedules"
    / "checkpoints"
    / "checkpoint_svc_2024-10-24.json"
)
PDF_2024 = (
    REPO_ROOT
    / "docs"
    / "service_rate_checkpoints"
    / "Upto 54th Service Notification.pdf"
)


def _new_serials_from_cache_page(page: int) -> list[str]:
    html = (VLM_CACHE / f"Upto_54th_Service_Notification_p{page}.html").read_text(
        encoding="utf-8"
    )
    serials: list[str] = []
    for row in vlm._extract_page_rows(html):
        classified = vlm._classify_row(row)
        if classified and classified["type"] == "new":
            serials.append(classified["sno"])
    return serials


def test_vlm_parser_preserves_variant_serials_from_real_cache():
    assert "31A" in _new_serials_from_cache_page(38)
    assert "38" in _new_serials_from_cache_page(40)
    assert "39" in _new_serials_from_cache_page(41)
    assert "2A" in _new_serials_from_cache_page(42)


def test_vlm_parser_splits_embedded_rate_condition_from_real_cache():
    html = (VLM_CACHE / "Upto_54th_Service_Notification_p16.html").read_text(
        encoding="utf-8"
    )
    row = next(
        row
        for row in vlm._extract_page_rows(html)
        if "(iii) Supply of goods" in " ".join(row)
    )

    classified = vlm._classify_row(row)

    assert classified["type"] == "continuation"
    assert classified["rate"] == "2.5"
    assert classified["desc"].startswith("(iii) Supply of goods")
    assert "Provided that credit" not in classified["desc"]
    assert classified["condition"].startswith("Provided that credit")


def test_vlm_parser_keeps_condition_only_rows_out_of_description():
    html = (VLM_CACHE / "Upto_54th_Service_Notification_p16.html").read_text(
        encoding="utf-8"
    )
    row = next(
        row
        for row in vlm._extract_page_rows(html)
        if "not been taken [Please refer to Explanation no. (iv)]" in " ".join(row)
    )

    classified = vlm._classify_row(row)

    assert classified["type"] == "continuation"
    assert classified["desc"] == ""
    assert classified["condition"].startswith("not been taken")


def test_vlm_parser_skips_structural_column_number_row_from_2025_cache():
    html = (VLM_CACHE / "1_Full_booklet_till_55th_Council_p3.html").read_text(
        encoding="utf-8"
    )
    row = next(row for row in vlm._extract_page_rows(html) if "(1)" in " ".join(row))

    assert vlm._classify_row(row) is None


def test_vlm_parser_skips_quoted_historical_rows_from_real_cache():
    html = (VLM_CACHE / "Upto_54th_Service_Notification_p39.html").read_text(
        encoding="utf-8"
    )
    row = next(
        row
        for row in vlm._extract_page_rows(html)
        if row and row[0].strip().startswith('"32')
    )

    assert vlm._classify_row(row) is None


def test_vlm_reroutes_split_2025_passenger_duplicate_but_keeps_tariff_code():
    page23 = (VLM_CACHE / "1_Full_booklet_till_55th_Council_p23.html").read_text(
        encoding="utf-8"
    )
    passenger_rows = [
        vlm._classify_row(row)
        for row in vlm._extract_page_rows(page23)
        if "Passenger transport services other than" in " ".join(row)
    ]
    previous = {"category": "", "desc_parts": [passenger_rows[0]["desc"]]}
    current = {"category": "(Goods transport services)", "desc_parts": []}

    assert vlm._should_reroute_to_previous(passenger_rows[-1], previous, current)

    page41 = (VLM_CACHE / "1_Full_booklet_till_55th_Council_p41.html").read_text(
        encoding="utf-8"
    )
    tariff_row = next(
        vlm._classify_row(row)
        for row in vlm._extract_page_rows(page41)
        if "9989 [Other manufacturing services" in " ".join(row)
    )
    manufacturing = {
        "category": "(Manufacturing services on physical inputs (goods) owned by others)",
        "desc_parts": [],
    }

    assert not vlm._should_reroute_to_previous(tariff_row, manufacturing, {"desc_parts": []})


def test_vlm_enhancement_attaches_leading_continuations_to_prior_entry(tmp_path):
    checkpoint = tmp_path / CHECKPOINT_2024.name
    manifest = tmp_path / "manifest.json"
    shutil.copyfile(CHECKPOINT_2024, checkpoint)

    extraction = vlm.enhance_checkpoint(
        str(checkpoint),
        str(PDF_2024),
        max_page=50,
        manifest_path=manifest,
    )

    assert extraction["leading_continuation_target"] == "3"
    assert "6" in [item["sno"] for item in extraction["stale_serials"]]

    data = json.loads(checkpoint.read_text(encoding="utf-8"))
    entries = next(iter(data["instruments"].values()))["schedules"]["I"]["entries"]
    by_sno = {entry["sno"].rstrip(".").upper(): entry for entry in entries}

    assert "31A" in by_sno
    assert len(by_sno["3"]["description"]) < 16309
    assert "Explanation. - 1. The promoter shall maintain" in by_sno["3"]["conditions"]
    assert "Passenger transport services other than" not in by_sno["9"]["description"]
    assert "Goods transport services other than" not in by_sno["10"]["description"]
    assert "Heading 9954" in by_sno["38"]["tariff_item"]
    assert by_sno["39"]["description"].startswith("Supply of services")

    written_manifest = json.loads(manifest.read_text(encoding="utf-8"))
    assert written_manifest["extractions"][0]["checkpoint_json_path"] == str(checkpoint)
    assert written_manifest["extractions"][0]["serial_coverage"]["vlm_entry_count"] >= 39
    reroutes = written_manifest["extractions"][0]["rerouted_continuations"]
    assert any(item["from_sno"] == "9" and item["to_sno"] == "8" for item in reroutes)
    assert any(item["from_sno"] == "10" and item["to_sno"] == "9" for item in reroutes)


def test_join_omit_preserves_word_boundary():
    assert RateMaterializer._join_omit("services", "without") == "services without"


def test_materializer_filters_service_checkpoint_prefix_dates(tmp_path):
    from legal_corpus.rate_schedule_materializer import materialize_schedule

    base = tmp_path / "base.json"
    events = tmp_path / "events.jsonl"
    base.write_text(
        json.dumps(
            {
                "notification_id": "11/2017-ct-rate",
                "schedules": {
                    "I": {
                        "schedule_id": "I",
                        "rate_pct": 0,
                        "entries": [
                            {"sno": "1.", "tariff_item": "Heading 9991", "description": "Base"}
                        ],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    rows = [
        {
            "event_id": "before",
            "operation": "RATE_INSERT_ENTRIES",
            "target_notification": "11/2017-ct-rate",
            "target_schedule": "I",
            "effective_date": "2024-01-01",
            "publication_date": "2024-01-01",
            "payload": {
                "after_sno": "1",
                "entries": [
                    {"sno": "2", "tariff_item": "Heading 9992", "description": "Before"}
                ],
            },
        },
        {
            "event_id": "future",
            "operation": "RATE_INSERT_ENTRIES",
            "target_notification": "11/2017-ct-rate",
            "target_schedule": "I",
            "effective_date": "2025-01-01",
            "publication_date": "2025-01-01",
            "payload": {
                "after_sno": "2",
                "entries": [
                    {"sno": "3", "tariff_item": "Heading 9993", "description": "Future"}
                ],
            },
        },
    ]
    events.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    snapshot = materialize_schedule(
        base,
        events,
        "11/2017-ct-rate",
        checkpoint_date="svc_2024-10-24",
    )

    snos = [
        entry["sno"].rstrip(".")
        for entry in snapshot["schedules"]["I"]["entries"]
        if not entry.get("is_omitted")
    ]
    assert snos == ["1", "2"]
