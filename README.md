# Flock Energy — Urja Meter Ops API Wrapper

A clean, modern, and high-performance REST API built with FastAPI in front of the legacy Urja Ops Portal (`https://urja-ops.flockenergy.tech`). Callers interact with typed, normalized JSON endpoints while authentication, dual-template HTML parsing, session recovery, rate throttling, and response caching are managed internally.

---

## 1. What It Is and Project Structure

### What It Is
The Urja Ops Portal exposes 403 smart meters across the Jaipur power distribution network, split across two incompatible page designs (`legacy` and `v2`) with tricky hierarchy anomalies and transient session timeouts. This service acts as a robust proxy and caching layer that:
- Normalizes disparate `legacy` and `v2` meter layouts into unified Pydantic models.
- Manages authenticated portal sessions with automatic re-login on expiry.
- Throttles upstream calls to a concurrency ceiling of 3 with exponential backoff on transient errors.
- Exposes fast paginated listings, interval consumption, hierarchy tree resolution, and geospatial radius search.
- Provides an in-memory bulk index loaded from `meter-export.json` for microsecond response times.

### Project Structure
```
flock-energy-api/
├── app/
│   ├── __init__.py
│   ├── main.py            # FastAPI app, route handlers, error mappings
│   ├── config.py          # Configuration management via pydantic-settings
│   ├── client.py          # PortalClient: async HTTP, re-auth, lock, semaphore, retries
│   ├── parsers.py         # BeautifulSoup parsers normalizing legacy & v2 templates
│   ├── models.py          # Pydantic v2 domain & response schemas
│   ├── service.py         # Business logic, bulk data index, hierarchy builder, nearby search
│   └── cache.py           # TTL in-memory cache with hit/miss tracking
├── tests/
│   ├── __init__.py
│   ├── fixtures/          # Realistic HTML & JSON portal response samples
│   │   ├── login.html
│   │   ├── meter_list.html
│   │   ├── meter_detail_legacy.html
│   │   ├── meter_detail_v2.html
│   │   ├── meter_detail_blank_hierarchy.html
│   │   └── meter-export.json
│   ├── test_parsers.py    # Unit tests for normalization and template parsers
│   ├── test_client.py     # Mocked HTTP transport tests for re-auth and retries
│   └── test_api.py        # End-to-end FastAPI TestClient integration tests
├── scripts/
│   └── export_openapi.py  # Script extracting openapi.json from FastAPI app
├── data/
│   └── meter-export.json  # Seed bulk export of all 403 Jaipur meters
├── openapi.json           # Exported OpenAPI 3.1 specification
├── PROTOCOL.md            # Portal recon documentation & protocol specification
├── README.md              # Project documentation & run guide
├── REFLECTION.md          # Post-mortem engineering reflection
├── requirements.txt       # Production & test dependencies
├── .env.example           # Example environment configuration
└── .gitignore             # Git ignore patterns
```

---

## 2. Install / Configure / Run

### Prerequisites
- Python 3.10+ (Tested with Python 3.14 / 3.11)
- `pip` or virtualenv

### Setup Instructions

1. **Clone the repository and enter directory**:
   ```bash
   cd flock-energy-api
   ```

2. **Create and activate a virtual environment**:
   ```bash
   # On Windows (PowerShell):
   python -m venv .venv
   .venv\Scripts\Activate.ps1

   # On Linux/macOS:
   python3 -m venv .venv
   source .venv/bin/activate
   ```

3. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

4. **Configure environment variables**:
   ```bash
   # Copy example environment configuration
   cp .env.example .env
   ```
   Edit `.env` if custom portal credentials or URLs are required:
   ```ini
   PORTAL_BASE_URL=https://urja-ops.flockenergy.tech
   PORTAL_USERNAME=operator
   PORTAL_PASSWORD=secret
   PORTAL_MAX_CONCURRENCY=3
   PORTAL_TIMEOUT_SECONDS=10.0
   PORTAL_MAX_RETRIES=3
   CACHE_TTL_METERS=3600
   CACHE_TTL_CONSUMPTION=60
   PORTAL_SIMULATE_IF_UNREACHABLE=true
   ```

5. **Run the Automated Tests**:
   ```bash
   pytest -v
   ```
   All 21 unit and integration tests will execute against mocked transports and local fixtures with 0 external network dependencies.

6. **Start the API Server**:
   ```bash
   uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
   ```

7. **Explore the Interactive Documentation**:
   - Swagger UI: [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)
   - ReDoc: [http://127.0.0.1:8000/redoc](http://127.0.0.1:8000/redoc)
   - OpenAPI JSON: [http://127.0.0.1:8000/openapi.json](http://127.0.0.1:8000/openapi.json)

---

## 3. Sample Requests & Responses (curl)

### 3.1 Health Check
```bash
curl -s http://127.0.0.1:8000/api/v1/health
```
```json
{
  "status": "healthy",
  "portal": "simulated_local",
  "indexed_meters_count": 403,
  "cache_ttl_meters_sec": 3600,
  "cache_ttl_consumption_sec": 60
}
```

### 3.2 List Meters (Paginated & Filtered)
```bash
curl -s "http://127.0.0.1:8000/api/v1/meters?page=1&page_size=2&make=HPL"
```
```json
{
  "items": [
    {
      "meter_id": "J100000",
      "serial_no": "SE33962",
      "make": "HPL",
      "phase_type": "single",
      "status": "Decommissioned",
      "dt_code": "DT-001"
    },
    {
      "meter_id": "J100005",
      "serial_no": "SE34110",
      "make": "HPL",
      "phase_type": "three",
      "status": "Installed",
      "dt_code": "DT-003"
    }
  ],
  "page": 1,
  "page_size": 2,
  "total": 85,
  "total_pages": 43
}
```

### 3.3 Get Meter Detail (Normalized Schema & Cache Headers)
```bash
curl -i -s http://127.0.0.1:8000/api/v1/meters/J100000
```
Headers:
```http
HTTP/1.1 200 OK
X-Cache: MISS
content-type: application/json
```
Response body:
```json
{
  "meter_id": "J100000",
  "serial_no": "SE33962",
  "make": "HPL",
  "phase_type": "single",
  "status": "Decommissioned",
  "dt_code": "DT-001",
  "install_type": "Whole Current",
  "build": "legacy",
  "location": {
    "lat": 26.938961002479868,
    "lng": 75.83095696146852
  },
  "hierarchy": {
    "zone": {
      "level": "zone",
      "code": "Z-01",
      "name": "Jaipur Zone 1",
      "path_key": "Z-01"
    },
    "circle": {
      "level": "circle",
      "code": "C-01",
      "name": "Circle 1",
      "path_key": "Z-01/C-01"
    },
    "division": {
      "level": "division",
      "code": "D-01",
      "name": "Division 1",
      "path_key": "Z-01/C-01/D-01"
    },
    "subdivision": {
      "level": "subdivision",
      "code": "SD-01",
      "name": "Subdivision 1",
      "path_key": "Z-01/C-01/D-01/SD-01"
    },
    "substation": {
      "level": "substation",
      "code": "SS-01",
      "name": "Substation 1",
      "path_key": "Z-01/C-01/D-01/SD-01/SS-01"
    },
    "feeder": {
      "level": "feeder",
      "code": "F-001",
      "name": "Feeder 1",
      "path_key": "Z-01/C-01/D-01/SD-01/SS-01/F-001"
    },
    "dt": {
      "level": "dt",
      "code": "DT-001",
      "name": "Malviya Nagar DT 1",
      "path_key": "Z-01/C-01/D-01/SD-01/SS-01/F-001/DT-001"
    }
  }
}
```

*Note: Calling the same endpoint a second time yields `X-Cache: HIT`. Adding `?refresh=true` forces upstream bypass and returns `X-Cache: MISS`.*

### 3.4 Get Interval Consumption
```bash
curl -s "http://127.0.0.1:8000/api/v1/meters/J100000/consumption?limit=2"
```
```json
{
  "meter_id": "J100000",
  "count": 2,
  "from": "2026-10-25T15:00:00+05:30",
  "to": "2026-10-25T15:30:00+05:30",
  "readings": [
    {
      "timestamp": "2026-10-25T15:00:00+05:30",
      "kwh": 10000.0,
      "kvah": 9500.0,
      "voltage_r": 239.5
    },
    {
      "timestamp": "2026-10-25T15:30:00+05:30",
      "kwh": 10002.4,
      "kvah": 9502.6,
      "voltage_r": 240.1
    }
  ]
}
```

### 3.5 Find Nearby Meters (Geospatial Radius Search)
```bash
curl -s "http://127.0.0.1:8000/api/v1/meters/nearby?lat=26.9389&lng=75.8309&radius_km=1.0&limit=2"
```
```json
[
  {
    "meter_id": "J100000",
    "serial_no": "SE33962",
    "make": "HPL",
    "status": "Decommissioned",
    "distance_km": 0.007,
    "location": {
      "lat": 26.938961002479868,
      "lng": 75.83095696146852
    },
    "dt_code": "DT-001"
  },
  {
    "meter_id": "J100201",
    "serial_no": "SE41882",
    "make": "Genus",
    "status": "Installed",
    "distance_km": 0.452,
    "location": {
      "lat": 26.94211029182371,
      "lng": 75.8331102981923
    },
    "dt_code": "DT-001"
  }
]
```

### 3.6 Standardized Error Handling
```bash
curl -s http://127.0.0.1:8000/api/v1/meters/invalid_id
```
```json
{
  "error": {
    "code": "BAD_REQUEST",
    "message": "Invalid meter ID 'invalid_id'. Meter IDs must follow format 'J' followed by digits (e.g. J100000)."
  }
}
```

---

## 4. Where PROTOCOL.md and openapi.json Are

- **`PROTOCOL.md`**: Located at [`PROTOCOL.md`](PROTOCOL.md) in the root of the project. Contains the recon documentation, authentication flows, header behaviors, endpoint patterns, and data quirks.
- **`openapi.json`**: Located at [`openapi.json`](openapi.json) in the root of the project. Can be regenerated anytime via:
  ```bash
  python scripts/export_openapi.py
  ```

---

## 5. Assumptions

1. **Timezone**: All timestamps parsed from the portal without explicit zone descriptors (`DD/MM/YYYY HH:mm`) are assumed to be in Indian Standard Time (`Asia/Kolkata`, `+05:30`), matching the physical Jaipur location of the distribution network.
2. **Registers**: `kwh` and `kvah` values represent cumulative register readings that monotonically increase over time. Intervals with missing decimals (e.g. `48440`) represent whole units and are converted to floating point (`48440.0`).
3. **Data Quality Policy**: Missing fields in HTML or JSON are strictly mapped to `null`, never empty strings `""` or string literals like `"N/A"`.
4. **Hierarchy Uniqueness**: Hierarchy codes are not globally unique across parents (e.g. `SD-01` occurs under multiple divisions); therefore, composite hierarchical path keys (`path_key`) are required to prevent node collision.
5. **Bulk Data Pathway**: To prevent overwhelming the legacy portal with 21 sequential HTTP crawls on every list query, the bulk export (`meter-export.json`) is loaded into an in-memory index on startup. Live endpoints fetch on-demand and fall back gracefully to the index.

---

## 6. Design Decisions and Trade-offs

- **No Public Login Route**: Rather than forcing API callers to manage session tokens or cookies, authentication is handled transparently inside `PortalClient`. If upstream sessions expire mid-request, an `asyncio.Lock` ensures a single background re-authentication occurs and replays the failed call.
- **Normalized Key Extraction**: Rather than maintaining fragile DOM xpath selectors for both `legacy` and `v2` designs, `parsers.py` collapses all field labels into lowercase alphanumeric strings (`normalize_key`). This guarantees that both `"Installation Status"` and `"InstallationStatus"` match into `status` seamlessly.
- **Path-Keyed Hierarchy Tree**: Node keys in `/api/v1/hierarchy` use composite paths (`Z-01/C-01/D-01/SD-01`) rather than codes alone, preserving the real topology while generating an anomaly log in the `data_quality` response block.
- **In-Memory TTL Caching**: A minimal `TTLCache` in `app/cache.py` avoids external dependencies (like Redis) while providing granular TTLs (1 hour for meter metadata, 60s for interval consumption) and cache status headers (`X-Cache: HIT|MISS`).

---

## 7. What Was Intentionally Skipped

- **User Authentication / RBAC**: Omitted per the spec's anti-goals; the service focuses on wrapping the legacy portal cleanly without adding multi-tenant user authentication tables.
- **Write Operations**: Portal modification or write endpoints are excluded as the legacy portal is strictly treated as read-only.
- **External Database Requirement**: No PostgreSQL, Redis, or Docker daemon is required; standard Python runs the entire service with zero external system overhead.
- **Automatic Interval Differential Calculation**: Monotonic raw register counters are preserved directly; interval difference calculations were kept out of the core contract to prevent precision distortion.

---

## 8. What I'd Improve with More Time

1. **Persistent Spatial Database**: Upgrade in-memory Haversine distance filtering to an embedded SQLite + Spatialite or PostgreSQL + PostGIS database with an R-Tree index for sub-millisecond bounding box lookups across millions of meters.
2. **Interactive Ops Web Dashboard**: Build the React + Vite frontend with Leaflet maps showing feeder lines and markers colored by status (Installed, Faulty, Decommissioned), plus consumption load curves with Chart.js.
3. **Automated Upstream Health Ping Daemon**: Introduce a background heartbeat task that periodically verifies portal session status and refreshes the bulk index if upstream changes occur.
4. **Time-Series Persistence**: Pipe half-hourly readings into TimescaleDB or ClickHouse for long-term historical anomaly detection and transformer overloading alerts.

---

## 9. Reflection

See the complete, detailed answers in [`REFLECTION.md`](REFLECTION.md).
