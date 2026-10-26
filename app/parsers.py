import re
from datetime import datetime
from typing import Optional, List, Dict, Tuple, Any
from bs4 import BeautifulSoup, Tag

from app.models import (
    MeterSummary,
    Meter,
    Location,
    HierarchyNode,
    Reading,
)


HIERARCHY_LEVELS = [
    "zone",
    "circle",
    "division",
    "subdivision",
    "substation",
    "feeder",
    "dt",
]


def normalize_key(raw_key: str) -> str:
    """Normalize a string key to lowercase alphanumeric for robust template matching."""
    return re.sub(r"[^a-z0-9]", "", raw_key.lower())


def parse_clean_string(val: Optional[str]) -> Optional[str]:
    """Clean string values, converting empty/N/A variations to None."""
    if val is None:
        return None
    cleaned = val.strip()
    if cleaned == "" or cleaned.upper() in ("N/A", "NA", "NULL", "NONE", "-"):
        return None
    return cleaned


def parse_number(val: Any) -> Optional[float]:
    """Safely parse numbers from string or numeric types, treating missing/NA as None."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    cleaned = str(val).strip().replace(",", "")
    if cleaned == "" or cleaned.upper() in ("N/A", "NA", "NULL", "NONE", "-"):
        return None
    try:
        return float(cleaned)
    except (ValueError, TypeError):
        return None


def parse_timestamp(raw: str) -> str:
    """
    Parse timestamp string and normalize to ISO 8601 with +05:30 timezone offset.
    Supports DD/MM/YYYY HH:mm and existing ISO formats.
    """
    cleaned = raw.strip()
    # Check if already ISO format with offset
    if re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\+\d{2}:\d{2}|Z)$", cleaned):
        if cleaned.endswith("Z"):
            cleaned = cleaned[:-1] + "+00:00"
        return cleaned

    # Common portal format: DD/MM/YYYY HH:mm or DD/MM/YYYY HH:mm:ss
    dt_match = re.match(r"^(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?)?$", cleaned)
    if dt_match:
        day, month, year = int(dt_match.group(1)), int(dt_match.group(2)), int(dt_match.group(3))
        hour = int(dt_match.group(4)) if dt_match.group(4) is not None else 0
        minute = int(dt_match.group(5)) if dt_match.group(5) is not None else 0
        second = int(dt_match.group(6)) if dt_match.group(6) is not None else 0
        dt = datetime(year, month, day, hour, minute, second)
        return dt.strftime("%Y-%m-%dT%H:%M:%S+05:30")

    # Fallback to general ISO representation if standard format
    return cleaned


def parse_breadcrumb_item(text: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Parse a single hierarchy breadcrumb string into (name, code).
    Examples:
      - 'Jaipur Zone 1 (Z-01)' -> ('Jaipur Zone 1', 'Z-01')
      - ' (C-01)' -> (None, 'C-01')
      - 'Substation 1' -> ('Substation 1', None)
      - 'DT-007' -> (None, 'DT-007') or ('DT-007', 'DT-007')
    """
    cleaned = text.strip()
    if not cleaned or cleaned.upper() in ("N/A", "NA", "-"):
        return None, None

    # Check for Name (CODE) pattern
    m = re.match(r"^(.*?)\s*\(([^)]+)\)$", cleaned)
    if m:
        name_part = parse_clean_string(m.group(1))
        code_part = parse_clean_string(m.group(2))
        return name_part, code_part

    # If it looks like a standalone code e.g. DT-007, SS-01, Z-01
    if re.match(r"^[A-Z]{1,3}-\d+$", cleaned):
        return None, cleaned

    return cleaned, None


def parse_breadcrumb(soup: BeautifulSoup) -> Dict[str, HierarchyNode]:
    """
    Extract breadcrumb trail and build hierarchy nodes keyed by level.
    Ensures path_key is constructed hierarchically across parents.
    """
    crumbs: List[str] = []
    
    # Check for dedicated crumb items first (exact class name, not container)
    crumb_items = soup.find_all(
        lambda tag: tag.name != "nav" and tag.get("class") and any(
            c.lower() in ("crumb", "breadcrumb-item", "crumb-item") for c in tag.get("class")
        )
    )
    if crumb_items:
        crumbs = [el.get_text(strip=True) for el in crumb_items]
    else:
        nav_el = soup.find("nav", class_=re.compile(r"breadcrumb", re.I)) or soup.find(class_=re.compile(r"breadcrumb", re.I))
        if not nav_el:
            main_el = soup.find("main")
            if main_el:
                nav_el = main_el.find("nav")
        if nav_el:
            spans = [s.get_text(strip=True) for s in nav_el.find_all("span")]
            if spans:
                crumbs = [s for s in spans if s not in ("›", ">", "/", "-", "»") and s]
            else:
                raw_text = nav_el.get_text(separator=" ")
                crumbs = [c.strip() for c in re.split(r"[>›»]", raw_text) if c.strip()]

    hierarchy: Dict[str, HierarchyNode] = {}
    path_segments: List[str] = []

    for idx, level in enumerate(HIERARCHY_LEVELS):
        name: Optional[str] = None
        code: Optional[str] = None

        if idx < len(crumbs):
            name, code = parse_breadcrumb_item(crumbs[idx])

        # Composite path key prevents code reuse collisions (e.g. SD-01)
        segment = code if code else (name if name else f"unknown_{level}")
        path_segments.append(segment)
        path_key = "/".join(path_segments)

        hierarchy[level] = HierarchyNode(
            level=level,
            code=code,
            name=name,
            path_key=path_key,
        )

    return hierarchy


def extract_key_values(soup: BeautifulSoup) -> Dict[str, str]:
    """
    Extract all key-value pairs from tables, definition lists, and flex key-value divs.
    Keys are normalized using normalize_key.
    """
    kv_pairs: Dict[str, str] = {}

    # 1. From HTML <table> elements (legacy style)
    for row in soup.find_all("tr"):
        cells = row.find_all(["td", "th"])
        if len(cells) == 2:
            key_text = cells[0].get_text(strip=True)
            val_text = cells[1].get_text(strip=True)
            norm_k = normalize_key(key_text)
            if norm_k:
                kv_pairs[norm_k] = val_text

    # 2. From key-value divs / spans (v2 style: .kv-item, .k, .v)
    for kv_item in soup.find_all(class_=re.compile(r"kv-item|property-item|detail-item", re.I)):
        k_el = kv_item.find(class_=re.compile(r"\bk\b|key|label", re.I))
        v_el = kv_item.find(class_=re.compile(r"\bv\b|value|val", re.I))
        if k_el and v_el:
            norm_k = normalize_key(k_el.get_text(strip=True))
            if norm_k:
                kv_pairs[norm_k] = v_el.get_text(strip=True)
        else:
            # Try colon split inside item
            item_text = kv_item.get_text(strip=True)
            if ":" in item_text:
                parts = item_text.split(":", 1)
                norm_k = normalize_key(parts[0])
                if norm_k:
                    kv_pairs[norm_k] = parts[1].strip()

    # 3. From <dt>/<dd> pairs
    dts = soup.find_all("dt")
    dds = soup.find_all("dd")
    if len(dts) == len(dds):
        for dt_el, dd_el in zip(dts, dds):
            norm_k = normalize_key(dt_el.get_text(strip=True))
            if norm_k:
                kv_pairs[norm_k] = dd_el.get_text(strip=True)

    return kv_pairs


def parse_meter_detail(html: str) -> Meter:
    """
    Parse a meter detail page into a unified Meter Pydantic model.
    Seamlessly normalizes both legacy and v2 HTML templates.
    """
    soup = BeautifulSoup(html, "html.parser")
    kv = extract_key_values(soup)

    # Detect template build
    build = "legacy"
    nameplate = soup.find(class_=re.compile(r"nameplate", re.I))
    if nameplate and isinstance(nameplate, Tag) and nameplate.get("data-build"):
        build = str(nameplate["data-build"])
    elif "meterid" in kv and "serialno" in kv and "lat" in kv:
        build = "v2"

    meter_id = kv.get("meterid") or kv.get("meter", "")
    serial_no = kv.get("serialno") or kv.get("serial", "")
    make = kv.get("make") or ""
    phase_type = kv.get("phasetype") or kv.get("phase") or "single"
    status = kv.get("installationstatus") or kv.get("status") or "Installed"
    dt_code = kv.get("dtcode") or kv.get("dt") or ""
    install_type = kv.get("installationtype") or kv.get("installtype")

    # Latitude & Longitude
    lat_val = parse_number(kv.get("latitude") or kv.get("lat"))
    lng_val = parse_number(kv.get("longitude") or kv.get("lng"))

    location = Location(
        lat=lat_val if lat_val is not None else 0.0,
        lng=lng_val if lng_val is not None else 0.0,
    )

    hierarchy = parse_breadcrumb(soup)

    # Cross-reference DT code if not in table but present in breadcrumb
    if not dt_code and hierarchy.get("dt") and hierarchy["dt"].code:
        dt_code = hierarchy["dt"].code

    return Meter(
        meter_id=meter_id,
        serial_no=serial_no,
        make=make,
        phase_type=phase_type,
        status=status,
        dt_code=dt_code,
        install_type=install_type,
        build=build,
        location=location,
        hierarchy=hierarchy,
    )


def parse_consumption(html: str) -> List[Reading]:
    """Parse interval consumption table from meter detail page."""
    soup = BeautifulSoup(html, "html.parser")
    readings: List[Reading] = []

    # Find table containing consumption data
    table = soup.find("table", class_=re.compile(r"consumption", re.I))
    if not table:
        for t in soup.find_all("table"):
            header_text = t.get_text().lower()
            if "kwh" in header_text and "timestamp" in header_text:
                table = t
                break

    if not table:
        return readings

    tbody = table.find("tbody") or table
    for tr in tbody.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if len(cells) < 3:
            continue
        
        # Skip header rows inside tbody
        first_text = cells[0].get_text(strip=True).lower()
        if "timestamp" in first_text or "kwh" in first_text:
            continue

        raw_ts = cells[0].get_text(strip=True)
        raw_kwh = cells[1].get_text(strip=True)
        raw_kvah = cells[2].get_text(strip=True)
        raw_vr = cells[3].get_text(strip=True) if len(cells) > 3 else None

        kwh = parse_number(raw_kwh)
        kvah = parse_number(raw_kvah)

        if raw_ts and kwh is not None and kvah is not None:
            iso_ts = parse_timestamp(raw_ts)
            voltage_r = parse_number(raw_vr)
            readings.append(
                Reading(
                    timestamp=iso_ts,
                    kwh=kwh,
                    kvah=kvah,
                    voltage_r=voltage_r,
                )
            )

    return readings


def parse_meter_list(html: str) -> Tuple[List[MeterSummary], int, int, int]:
    """
    Parse meter directory table and pagination metrics.
    Returns: (meters, current_page, total_pages, total_count)
    """
    soup = BeautifulSoup(html, "html.parser")
    meters: List[MeterSummary] = []

    table = soup.find("table", class_=re.compile(r"meters", re.I)) or soup.find("table")
    if table:
        tbody = table.find("tbody") or table
        for tr in tbody.find_all("tr"):
            cells = tr.find_all("td")
            if len(cells) >= 6:
                meter_id = cells[0].get_text(strip=True)
                serial_no = cells[1].get_text(strip=True)
                make = cells[2].get_text(strip=True)
                phase_type = cells[3].get_text(strip=True)
                status = cells[4].get_text(strip=True)
                dt_code = cells[5].get_text(strip=True)

                if meter_id and serial_no:
                    meters.append(
                        MeterSummary(
                            meter_id=meter_id,
                            serial_no=serial_no,
                            make=make,
                            phase_type=phase_type,
                            status=status,
                            dt_code=dt_code,
                        )
                    )

    # Extract pagination metrics
    page = 1
    total_pages = 1
    total = len(meters)

    pagination_el = soup.find(class_=re.compile(r"pagination", re.I))
    if pagination_el and isinstance(pagination_el, Tag):
        if pagination_el.get("data-page"):
            page = int(pagination_el["data-page"])
        if pagination_el.get("data-total-pages"):
            total_pages = int(pagination_el["data-total-pages"])
        if pagination_el.get("data-total"):
            total = int(pagination_el["data-total"])
        else:
            # Check text e.g. "Showing page 1 of 21 (403 total meters)"
            text = pagination_el.get_text()
            m = re.search(r"page\s+(\d+)\s+of\s+(\d+)", text, re.I)
            if m:
                page = int(m.group(1))
                total_pages = int(m.group(2))
            m_total = re.search(r"\((\d+)\s+total", text, re.I)
            if m_total:
                total = int(m_total.group(1))

    return meters, page, total_pages, total
