import json
import math
import logging
import re
import time
import hmac
import hashlib
from pathlib import Path
from typing import Optional, List, Dict, Tuple, Any

import httpx
from app.config import settings
from app.cache import cache
from app.client import PortalClient, PortalUnavailable, PortalAuthError, PortalParseError
from app.parsers import (
    parse_clean_string,
    parse_number,
    parse_timestamp,
    parse_meter_detail,
    parse_consumption,
    parse_meter_list,
    HIERARCHY_LEVELS,
)
from app.models import (
    MeterSummary,
    Meter,
    Location,
    HierarchyNode,
    Reading,
    ConsumptionResponse,
    Page,
    PhaseStats,
    DashboardStats,
)

logger = logging.getLogger(__name__)


def haversine_distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate the great-circle distance between two points on Earth in km."""
    radius_earth_km = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2.0) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(dlon / 2.0) ** 2
    )
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return radius_earth_km * c


class MeterService:
    """Core domain service managing meters, caching, portal integration, and hierarchy indexing."""

    def __init__(self, client: Optional[PortalClient] = None, data_path: Optional[str] = None):
        self.client = client or PortalClient()
        self.data_path = Path(data_path or settings.DATA_PATH)
        
        # In-memory bulk data index
        self._meters_by_id: Dict[str, Meter] = {}
        self._summaries_list: List[MeterSummary] = []
        self._data_source: str = "export_snapshot"
        self._last_bulk_refresh: float = 0.0
        
        # Seed initial fallback snapshot so app is immediately queryable synchronously
        self._load_snapshot_file()

    @property
    def data_source(self) -> str:
        return self._data_source

    def _ingest_raw_meters(self, raw_data: List[Dict[str, Any]]) -> None:
        """Parse raw export meters into in-memory indices."""
        self._meters_by_id.clear()
        self._summaries_list.clear()

        for item in raw_data:
            meter_id = item.get("meterId")
            if not meter_id:
                continue

            hierarchy_dict: Dict[str, HierarchyNode] = {}
            path_segments: List[str] = []
            raw_h = item.get("hierarchy", {})

            for level in HIERARCHY_LEVELS:
                node_raw = raw_h.get(level, {})
                code = node_raw.get("code") if node_raw.get("code") else None
                name = node_raw.get("name") if node_raw.get("name") else None
                
                segment = code if code else (name if name else f"unknown_{level}")
                path_segments.append(segment)
                path_key = "/".join(path_segments)

                hierarchy_dict[level] = HierarchyNode(
                    level=level,
                    code=code,
                    name=name,
                    path_key=path_key,
                )

            geo = item.get("geo", {})
            lat = float(geo.get("lat", 0.0))
            lng = float(geo.get("lng", 0.0))

            meter = Meter(
                meter_id=meter_id,
                serial_no=item.get("serialNo", ""),
                make=item.get("make", ""),
                phase_type=item.get("phaseType", "single"),
                status=item.get("installStatus", "Installed"),
                dt_code=item.get("dtCode", ""),
                install_type=item.get("installType"),
                build=item.get("build"),
                location=Location(lat=lat, lng=lng),
                hierarchy=hierarchy_dict,
                source=self._data_source,
            )

            self._meters_by_id[meter_id] = meter
            self._summaries_list.append(
                MeterSummary(
                    meter_id=meter.meter_id,
                    serial_no=meter.serial_no,
                    make=meter.make,
                    phase_type=meter.phase_type,
                    status=meter.status,
                    dt_code=meter.dt_code,
                )
            )

    def _load_snapshot_file(self) -> None:
        """Load fallback meter-export.json snapshot into memory when live fetch fails."""
        if not self.data_path.exists():
            logger.warning(f"Fallback snapshot data file not found at {self.data_path}")
            return

        try:
            with open(self.data_path, "r", encoding="utf-8") as f:
                raw_data = json.load(f)

            if isinstance(raw_data, list):
                self._data_source = "export_snapshot"
                self._ingest_raw_meters(raw_data)
            logger.info(f"Loaded {len(self._meters_by_id)} meters from fallback snapshot {self.data_path}")
        except Exception as e:
            logger.error(f"Failed to load snapshot from {self.data_path}: {e}")

    async def fetch_bulk_export(self) -> Optional[List[Dict[str, Any]]]:
        """
        Fetch the bulk export directly from the live portal:
        1. Fetch signingSecret from /portal/keys
        2. HMAC-SHA256 sign GET /portal/export page=1
        3. Fetch /portal/export?page=1
        """
        try:
            r_keys = await self.client.request("GET", "/portal/keys")
            if r_keys.status_code != 200:
                logger.warning(f"Failed to fetch portal signing keys: status {r_keys.status_code}")
                return None
            signing_secret = r_keys.json().get("data", {}).get("signingSecret")
            if not signing_secret:
                return None

            timestamp = str(int(time.time()))
            method = "GET"
            path = "/portal/export"
            query = "page=1"
            msg = f"{method}\n{path}\n{query}\n{timestamp}".encode("utf-8")
            signature = hmac.new(signing_secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()

            headers = {
                "x-timestamp": timestamp,
                "x-signature": signature,
            }
            resp = await self.client.request("GET", f"{path}?{query}", headers=headers)
            if resp.status_code == 200:
                data = resp.json().get("data", [])
                if isinstance(data, list) and len(data) > 0:
                    return data
            return None
        except Exception as e:
            logger.warning(f"Failed to fetch live bulk export: {e}")
            return None

    async def load_or_refresh_bulk_data(self, force_refresh: bool = False) -> str:
        """
        Refresh bulk meter data.
        Fetches live export from portal. If successful, sets data_source='live'.
        Only if live export fails, falls back to data/meter-export.json (data_source='export_snapshot').
        """
        now = time.time()
        if not force_refresh and (now - self._last_bulk_refresh < settings.CACHE_TTL_METERS) and self._data_source == "live":
            return self._data_source

        live_export = await self.fetch_bulk_export()
        if live_export:
            self._data_source = "live"
            self._ingest_raw_meters(live_export)
            self._last_bulk_refresh = now
            logger.info("Successfully loaded bulk export from live portal (source=live)")
            return "live"

        # Fall back to snapshot file
        logger.warning(f"Live export failed. Falling back to local snapshot: {self.data_path}")
        self._load_snapshot_file()
        self._data_source = "export_snapshot"
        self._last_bulk_refresh = now
        return "export_snapshot"

    def get_stats(self) -> DashboardStats:
        """Compute real operational statistics directly from the current index / live data."""
        total = len(self._summaries_list)
        statuses: Dict[str, int] = {}
        phases: Dict[str, int] = {"single": 0, "three": 0}
        makes: Dict[str, int] = {}

        for m in self._summaries_list:
            statuses[m.status] = statuses.get(m.status, 0) + 1
            phase_norm = m.phase_type.lower()
            phases[phase_norm] = phases.get(phase_norm, 0) + 1
            makes[m.make] = makes.get(m.make, 0) + 1

        single_cnt = phases.get("single", 0)
        three_cnt = phases.get("three", 0)
        single_pct = round((single_cnt / total * 100), 1) if total > 0 else 0.0
        three_pct = round((three_cnt / total * 100), 1) if total > 0 else 0.0

        return DashboardStats(
            total_meters=total,
            statuses=statuses,
            phases=PhaseStats(
                single=single_cnt,
                three=three_cnt,
                single_pct=single_pct,
                three_pct=three_pct,
            ),
            makes=makes,
            source=self._data_source,
        )

    async def check_health(self) -> Dict[str, Any]:
        """Check status of API service, in-memory index, and portal reachability via real login ping."""
        portal_status = "unreachable"
        try:
            resp = await self.client.request("GET", "/login")
            if resp.status_code in (200, 302):
                portal_status = "reachable"
        except Exception as e:
            logger.debug(f"Health check portal ping: {e}")
            portal_status = "unreachable"

        stats = self.get_stats()

        return {
            "status": "healthy" if portal_status == "reachable" else "degraded",
            "portal": portal_status,
            "indexed_meters_count": len(self._meters_by_id),
            "cache_ttl_meters_sec": settings.CACHE_TTL_METERS,
            "cache_ttl_consumption_sec": settings.CACHE_TTL_CONSUMPTION,
            "data_source": self._data_source,
            "stats": stats.model_dump(),
        }

    def list_meters(
        self,
        page: int = 1,
        page_size: int = 20,
        q: Optional[str] = None,
        make: Optional[str] = None,
        status: Optional[str] = None,
        phase_type: Optional[str] = None,
        dt_code: Optional[str] = None,
    ) -> Page[MeterSummary]:
        """Filter and paginate meters efficiently from the current index."""
        results = self._summaries_list

        if q:
            q_clean = q.strip().lower()
            results = [
                m for m in results
                if q_clean in m.meter_id.lower() or q_clean in m.serial_no.lower()
            ]

        if make:
            make_clean = make.strip().lower()
            results = [m for m in results if m.make.lower() == make_clean]

        if status:
            status_clean = status.strip().lower()
            results = [m for m in results if m.status.lower() == status_clean]

        if phase_type:
            phase_clean = phase_type.strip().lower()
            results = [m for m in results if m.phase_type.lower() == phase_clean]

        if dt_code:
            dt_clean = dt_code.strip().lower()
            results = [m for m in results if m.dt_code.lower() == dt_clean]

        total = len(results)
        page_size = max(1, min(page_size, 500))
        total_pages = max(1, math.ceil(total / page_size))
        page = max(1, min(page, total_pages))

        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size
        page_items = results[start_idx:end_idx]

        return Page[MeterSummary](
            items=page_items,
            page=page,
            page_size=page_size,
            total=total,
            total_pages=total_pages,
            source=self._data_source,
        )

    async def get_meter(self, meter_id: str, refresh: bool = False) -> Tuple[Meter, bool, str]:
        """
        Retrieve meter detail by ID:
        1. Check cache (unless refresh=True)
        2. Attempt live portal fetch (/meters/{id} and /portal/meters/{id}/geo)
        3. Fall back to export snapshot ONLY when live fetch fails
        Returns: (Meter, is_cache_hit, source)
        """
        cache_key = f"meter:{meter_id}"
        if not refresh:
            cached_meter, hit = cache.get(cache_key)
            if hit and cached_meter:
                return cached_meter, True, cached_meter.source

        meter: Optional[Meter] = None
        source = "live"

        # Attempt live portal fetch
        try:
            resp = await self.client.request("GET", f"/meters/{meter_id}")
            if resp.status_code == 200:
                meter = parse_meter_detail(resp.text)
                if not meter.meter_id:
                    meter.meter_id = meter_id

                # If coordinates are missing/0, attempt to fetch /portal/meters/{meter_id}/geo
                if meter.location.lat == 0.0 and meter.location.lng == 0.0:
                    try:
                        r_geo = await self.client.request("GET", f"/portal/meters/{meter_id}/geo")
                        if r_geo.status_code == 200:
                            g_data = r_geo.json().get("data", {})
                            lat = parse_number(g_data.get("latitude") or g_data.get("lat"))
                            lng = parse_number(g_data.get("longitude") or g_data.get("lng"))
                            if lat is not None and lng is not None:
                                meter.location = Location(lat=lat, lng=lng)
                    except Exception:
                        pass

                meter.source = "live"
                source = "live"
            elif resp.status_code == 404:
                raise KeyError(f"Meter with ID {meter_id} not found")
        except KeyError:
            raise
        except (PortalUnavailable, PortalAuthError, PortalParseError, httpx.RequestError) as e:
            logger.warning(f"Live portal fetch for {meter_id} failed: {e}. Falling back to export snapshot.")

        # Fallback to export snapshot
        if meter is None:
            if meter_id in self._meters_by_id:
                meter = self._meters_by_id[meter_id].model_copy()
                meter.source = "export_snapshot"
                source = "export_snapshot"
            else:
                raise KeyError(f"Meter with ID {meter_id} not found")

        # Cache the result
        cache.set(cache_key, meter, settings.CACHE_TTL_METERS)
        return meter, False, source

    async def get_consumption(
        self,
        meter_id: str,
        from_ts: Optional[str] = None,
        to_ts: Optional[str] = None,
        limit: Optional[int] = None,
        refresh: bool = False,
    ) -> Tuple[ConsumptionResponse, bool]:
        """
        Retrieve half-hourly consumption readings for a meter.
        Never synthesized or simulated. If portal unreachable or fails, raises PortalUnavailable.
        Returns: (ConsumptionResponse, is_cache_hit)
        """
        cache_key = f"consumption:{meter_id}"
        readings: Optional[List[Reading]] = None
        is_hit = False

        if not refresh:
            cached_readings, hit = cache.get(cache_key)
            if hit and cached_readings is not None:
                readings = cached_readings
                is_hit = True

        if readings is None:
            # 1. Try live portal JSON endpoint /portal/meters/{meter_id}/energy
            try:
                resp = await self.client.request("GET", f"/portal/meters/{meter_id}/energy")
                if resp.status_code == 200:
                    data = resp.json()
                    items = data.get("data", [])
                    readings_list = []
                    for item in items:
                        ts = parse_timestamp(item.get("timestamp", ""))
                        kwh = parse_number(item.get("kwh"))
                        kvah = parse_number(item.get("kvah"))
                        volt_r = parse_number(item.get("voltR") or item.get("volt_r") or item.get("voltage_r"))
                        if ts and kwh is not None:
                            readings_list.append(
                                Reading(
                                    timestamp=ts,
                                    kwh=kwh,
                                    kvah=kvah if kvah is not None else 0.0,
                                    voltage_r=volt_r,
                                )
                            )
                    if readings_list:
                        readings = readings_list
            except Exception as e:
                logger.debug(f"Live /energy endpoint for {meter_id}: {e}")

            # 2. Try HTML table from /meters/{meter_id}
            if readings is None:
                try:
                    resp_html = await self.client.request("GET", f"/meters/{meter_id}")
                    if resp_html.status_code == 200:
                        parsed = parse_consumption(resp_html.text)
                        if parsed:
                            readings = parsed
                    elif resp_html.status_code == 404:
                        raise KeyError(f"Meter with ID {meter_id} not found")
                except KeyError:
                    raise
                except Exception as e:
                    logger.debug(f"HTML consumption table for {meter_id}: {e}")

            # If still None or empty, NEVER simulate! Raise PortalUnavailable
            if not readings:
                raise PortalUnavailable(f"Upstream portal returned no consumption data for meter {meter_id}")

            cache.set(cache_key, readings, settings.CACHE_TTL_CONSUMPTION)

        # Sort ascending by timestamp
        sorted_readings = sorted(readings, key=lambda r: r.timestamp)

        # Filter by from_ts / to_ts
        if from_ts:
            sorted_readings = [r for r in sorted_readings if r.timestamp >= from_ts]
        if to_ts:
            sorted_readings = [r for r in sorted_readings if r.timestamp <= to_ts]

        if limit and limit > 0:
            sorted_readings = sorted_readings[:limit]

        earliest = sorted_readings[0].timestamp if sorted_readings else None
        latest = sorted_readings[-1].timestamp if sorted_readings else None

        response = ConsumptionResponse(
            meter_id=meter_id,
            count=len(sorted_readings),
            from_=earliest,
            to=latest,
            readings=sorted_readings,
        )
        return response, is_hit

    def get_hierarchy_tree(self) -> Dict[str, Any]:
        """
        Build complete hierarchical distribution tree keyed by composite path.
        Emits nodes with code: null / name: null where blank.
        Includes data_quality block listing anomalies and data source indicator.
        """
        tree_root: Dict[str, Any] = {}
        anomalies: List[Dict[str, Any]] = []

        code_parents: Dict[str, set] = {}
        dt_names: Dict[str, set] = {}

        for meter in self._meters_by_id.values():
            curr_level_dict = tree_root
            parent_path = ""

            for level in HIERARCHY_LEVELS:
                node = meter.hierarchy.get(level)
                if not node:
                    continue

                code = node.code
                name = node.name
                path_key = node.path_key

                # Record anomaly for blank code or name
                if code is None or name is None:
                    anomalies.append({
                        "level": level,
                        "meter_id": meter.meter_id,
                        "path_key": path_key,
                        "anomaly": "missing_code" if code is None else "missing_name",
                        "code": code,
                        "name": name,
                    })

                # Track parent for code reuse detection
                if code:
                    if code not in code_parents:
                        code_parents[code] = set()
                    code_parents[code].add(parent_path)

                # Track DT names for conflicting DT name detection
                if level == "dt" and code and name:
                    if code not in dt_names:
                        dt_names[code] = set()
                    dt_names[code].add(name)

                # Nest in tree
                if path_key not in curr_level_dict:
                    curr_level_dict[path_key] = {
                        "level": level,
                        "code": code,
                        "name": name,
                        "path_key": path_key,
                        "children": {},
                        "meter_count": 0,
                    }

                curr_level_dict[path_key]["meter_count"] += 1
                curr_level_dict = curr_level_dict[path_key]["children"]
                parent_path = path_key

        reused_codes = [
            {"code": c, "parents": sorted(list(parents))}
            for c, parents in code_parents.items()
            if len(parents) > 1 and any(p != "" for p in parents)
        ]

        conflicting_dts = [
            {"dt_code": dt, "conflicting_names": sorted(list(names))}
            for dt, names in dt_names.items()
            if len(names) > 1
        ]

        seen_anom = set()
        dedup_anomalies = []
        for anom in anomalies:
            sig = (anom.get("level"), anom.get("path_key"), anom.get("anomaly"))
            if sig not in seen_anom:
                seen_anom.add(sig)
                dedup_anomalies.append(anom)

        return {
            "source": self._data_source,
            "hierarchy": tree_root,
            "data_quality": {
                "unique_hierarchy_paths": len(tree_root),
                "blank_fields_anomalies_count": len(dedup_anomalies),
                "reused_codes": reused_codes,
                "conflicting_dt_names": conflicting_dts,
                "anomalies_sample": dedup_anomalies[:15],
            },
        }

    def find_nearby_meters(
        self,
        lat: float,
        lng: float,
        radius_km: float = 5.0,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """Find meters within radius_km sorted by distance using Haversine formula."""
        nearby = []
        for meter in self._meters_by_id.values():
            dist = haversine_distance_km(lat, lng, meter.location.lat, meter.location.lng)
            if dist <= radius_km:
                nearby.append({
                    "meter_id": meter.meter_id,
                    "serial_no": meter.serial_no,
                    "make": meter.make,
                    "status": meter.status,
                    "distance_km": round(dist, 3),
                    "location": {"lat": meter.location.lat, "lng": meter.location.lng},
                    "dt_code": meter.dt_code,
                    "source": self._data_source,
                })

        nearby.sort(key=lambda m: m["distance_km"])
        return nearby[:limit]
