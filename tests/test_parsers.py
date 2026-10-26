from pathlib import Path
from app.parsers import (
    parse_meter_detail,
    parse_consumption,
    parse_meter_list,
    parse_timestamp,
    parse_number,
    parse_breadcrumb_item,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def test_parse_timestamp_normalization():
    # Standard portal format
    assert parse_timestamp("25/10/2026 16:30") == "2026-10-25T16:30:00+05:30"
    assert parse_timestamp("01/01/2026 00:00:00") == "2026-01-01T00:00:00+05:30"
    # ISO pass-through
    assert parse_timestamp("2026-10-25T16:30:00+05:30") == "2026-10-25T16:30:00+05:30"


def test_parse_number_normalization():
    assert parse_number("48440") == 48440.0
    assert parse_number("3958.5") == 3958.5
    assert parse_number("12,500.5") == 12500.5
    assert parse_number("N/A") is None
    assert parse_number("") is None
    assert parse_number("-") is None
    assert parse_number(None) is None


def test_parse_breadcrumb_item():
    name, code = parse_breadcrumb_item("Jaipur Zone 1 (Z-01)")
    assert name == "Jaipur Zone 1"
    assert code == "Z-01"

    name_blank, code = parse_breadcrumb_item(" (C-01)")
    assert name_blank is None
    assert code == "C-01"

    name, code_blank = parse_breadcrumb_item("Substation 1")
    assert name == "Substation 1"
    assert code_blank is None


def test_parse_legacy_template():
    html_content = (FIXTURES_DIR / "meter_detail_legacy.html").read_text(encoding="utf-8")
    meter = parse_meter_detail(html_content)

    assert meter.meter_id == "J100000"
    assert meter.serial_no == "SE33962"
    assert meter.make == "HPL"
    assert meter.phase_type == "single"
    assert meter.status == "Decommissioned"
    assert meter.install_type == "Whole Current"
    assert meter.dt_code == "DT-001"
    assert meter.build == "legacy"
    assert meter.location.lat == 26.938961
    assert meter.location.lng == 75.830957

    # Hierarchy verification
    assert "zone" in meter.hierarchy
    assert meter.hierarchy["zone"].name == "Jaipur Zone 1"
    assert meter.hierarchy["zone"].code == "Z-01"
    assert meter.hierarchy["dt"].code == "DT-001"
    assert meter.hierarchy["dt"].path_key == "Z-01/C-01/D-01/SD-01/SS-01/F-001/DT-001"

    # Consumption
    readings = parse_consumption(html_content)
    assert len(readings) == 3
    assert readings[0].timestamp == "2026-10-25T15:30:00+05:30"
    assert readings[0].kwh == 48430.0
    assert readings[0].kvah == 3950.0
    assert readings[0].voltage_r == 238.1


def test_parse_v2_template():
    html_content = (FIXTURES_DIR / "meter_detail_v2.html").read_text(encoding="utf-8")
    meter = parse_meter_detail(html_content)

    assert meter.meter_id == "J100004"
    assert meter.serial_no == "SE10004"
    assert meter.make == "L&T"
    assert meter.phase_type == "single"
    assert meter.status == "Installed"
    assert meter.install_type == "Whole Current"
    assert meter.dt_code == "DT-003"
    assert meter.build == "v2"
    assert meter.location.lat == 26.852104
    assert meter.location.lng == 75.814239

    readings = parse_consumption(html_content)
    assert len(readings) == 4
    # The last reading in v2 fixture has empty voltage
    assert readings[-1].voltage_r is None


def test_parse_blank_hierarchy_template():
    html_content = (FIXTURES_DIR / "meter_detail_blank_hierarchy.html").read_text(encoding="utf-8")
    meter = parse_meter_detail(html_content)

    assert meter.meter_id == "J100011"
    # Circle has empty name, but code C-01
    assert meter.hierarchy["circle"].name is None
    assert meter.hierarchy["circle"].code == "C-01"
    # Substation has name Substation 1, but empty code
    assert meter.hierarchy["substation"].name == "Substation 1"
    assert meter.hierarchy["substation"].code is None

    readings = parse_consumption(html_content)
    # First reading has V(R) = "N/A" -> should be None, not string "N/A"
    assert readings[0].voltage_r is None


def test_parse_meter_list():
    html_content = (FIXTURES_DIR / "meter_list.html").read_text(encoding="utf-8")
    meters, page, total_pages, total = parse_meter_list(html_content)

    assert len(meters) == 5
    assert page == 1
    assert total_pages == 21
    assert total == 403
    assert meters[0].meter_id == "J100000"
    assert meters[0].make == "HPL"
    assert meters[0].status == "Decommissioned"
