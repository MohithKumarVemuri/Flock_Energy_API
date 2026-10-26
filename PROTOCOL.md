# Urja Ops Portal Recon Protocol (PROTOCOL.md)

This protocol document captures the observed behavior, authentication mechanisms, network routes, response payloads, DOM templates, and data anomalies of the legacy Urja Ops Portal (`https://urja-ops.flockenergy.tech`).

---

## 1. Authentication & Session Management

### 1.1 Login Flow
- **Endpoint**: `POST /login`
- **Content-Type**: `application/x-www-form-urlencoded`
- **CSRF Protection**: 
  - An initial `GET /login` renders a form with an optional/hidden CSRF input: `<input type="hidden" name="csrf_token" value="...">`.
  - The login request includes:
    - `username` / `email`: Portal operator account (e.g. `operator@urja.local` via `PORTAL_USERNAME` env var)
    - `password`: Portal operator password (via `PORTAL_PASSWORD` env var)
    - `csrf_token`: Parsed from the login form if present; omitted if not present.
    - Headers: `Origin` and `Referer` to satisfy SvelteKit CSRF origin checks.
- **Success Response**:
  - HTTP `302 Found` redirecting to `/meters` (or HTTP `200 OK`).
  - `Set-Cookie`: `urja_session=<token>; Path=/; HttpOnly; SameSite=Lax`.
- **Failure Response**:
  - HTTP `401 Unauthorized` or `200 OK` rendering `Invalid credentials` banner.

### 1.2 Session Expiry & Keepalive
- **Expiry Behavior**:
  - When the session expires or is missing, requesting protected endpoints (e.g. `GET /meters` or `GET /meters/J100000`) results in:
    1. HTTP `302 Found` with `Location: /login` or `Location: /login?expired=1`, OR
    2. HTTP `401 Unauthorized`.
- **Client Strategy**:
  - Catch both `302` redirecting to `/login` and `401`.
  - Re-authenticate transparently using an `asyncio.Lock` to ensure single flight re-auth across concurrent requests.
  - Replay the failed request once after successful re-login.

---

## 2. Endpoints & Data Surfaces

### 2.1 Meter List
- **Route**: `GET /meters`
- **Query Parameters**:
  - `page`: 1-based page index (defaults to `1`, up to `21`).
  - `q`: Search filter matching meter ID (e.g. `J100000`) or serial number (e.g. `SE33962`).
- **Response Format**: Server-rendered HTML (`text/html; charset=utf-8`).
- **Structure**:
  - An HTML `<table>` with headers: `Meter`, `Serial`, `Make`, `Phase`, `Status`, `DT`.
  - Each meter cell links to `/meters/{meter_id}`.
  - A pagination component `<div class="pagination">` indicating total pages (21) and total meter count (403), with 20 meters per page.

### 2.2 Meter Detail
- **Route**: `GET /meters/{meter_id}`
- **Parameter Validation**: Meter ID matches pattern `^J\d+$`.
- **Response Format**: Server-rendered HTML.
- **Dual Template Structures (`build`)**:
  - **`legacy` Template (~238 meters)**:
    - Key-value tables with labels like: `"Meter ID"`, `"Serial No"`, `"Phase Type"`, `"Installation Status"`, `"Installation Type"`, `"DT Code"`.
    - Coordinates in Location card: `"Latitude"` and `"Longitude"`.
  - **`v2` Template (~165 meters)**:
    - Modern card divs or definition lists with compact camelCase or joined labels: `"MeterId"`, `"SerialNo"`, `"PhaseType"`, `"InstallationStatus"`, `"InstallationType"`, `"DtCode"`.
    - Coordinates in Location card: `"Lat"` and `"Lng"`.
  - **Unified Normalization**:
    - The parser transforms all label keys to lower-case with whitespace/underscores removed (`meterid`, `serialno`, `phasetype`, `installationstatus`, `installationtype`, `dtcode`, `lat`/`latitude`, `lng`/`longitude`).
- **Hierarchy Breadcrumb**:
  - Found at top of detail page: `<nav class="breadcrumb">` or `.breadcrumb-container`.
  - 7 hierarchy levels separated by `>`:
    1. Zone
    2. Circle
    3. Division
    4. Subdivision
    5. Substation
    6. Feeder
    7. Distribution Transformer (DT)
  - String format matches `Name (CODE)`, e.g., `Jaipur Zone 1 (Z-01)`.
  - Tolerates missing names (e.g., `(C-01)`) or missing codes (e.g., `Substation 1` or `DT-007`).

### 2.3 Interval Consumption
- **Location**: Embedded in the meter detail page HTML within `.consumption-card` / `.consumption-table`.
- **Headers**: `Timestamp`, `kWh`, `kVAh`, `V(R)`.
- **Header Badge**: `"Latest: DD/MM/YYYY HH:mm"`.
- **Intervals**: 30-minute intervals.
- **Registers**: Cumulative monotonic kWh and kVAh counters.
- **Normalization**:
  - Timestamps formatted as `DD/MM/YYYY HH:mm` are parsed and converted to ISO 8601 with `+05:30` offset (e.g. `2026-10-25T16:30:00+05:30`).
  - Missing or `"N/A"` / `""` entries for nullable fields like voltage (`V(R)`) become `null`.
  - Numeric values parsed cleanly to `float`.

### 2.4 Bulk Data Export
- **Route**: `GET /export/meters.json` or `GET /api/export`
- **Format**: JSON array of 403 records.
- **Fields**: `meterId`, `serialNo`, `make`, `phaseType`, `installStatus`, `installType`, `build`, `dtCode`, `hierarchy`, `geo`.
- **Purpose**: Seeded into the fast in-memory index on startup for efficient filtering, bounding-box search, and hierarchy aggregation.

### 2.5 Transformers and Ops Desk
- `GET /transformers`: Read-only overview of distribution transformers, loading metrics, and aggregated meter counts.
- `GET /ops-desk`: Real-time operational exception view highlighting offline meters and phase abnormalities.

---

## 3. Discovered Anomalies & Data Quirks

| Anomaly / Quirk | Observation | Handling Strategy |
|---|---|---|
| **Non-unique Hierarchy Codes** | Subdivisions like `SD-01` exist under multiple divisions (`D-01`, `D-05`, `D-09`). | Hierarchy nodes must NEVER be keyed by code alone. Always key by composite full path (`Z-01/C-01/D-01/SD-01`). |
| **Blank Hierarchy Values** | 11 meters have empty circle codes or names. Substations and feeders also have isolated empty codes. | Parse as `null`, never empty string `""` or `"N/A"`. Maintain structure without discarding node. |
| **Conflicting DT Names** | `DT-007` maps to both `"Sanganer DT 7"` and `"Old Malviya Nagar Xfmr"`. | Retain names as parsed. Document anomaly in `data_quality` metadata in `/hierarchy`. |
| **Missing Decimals in Numbers** | Register readings often appear as integer strings like `"48440"`. | Safely cast to `float` (`48440.0`). |
| **Dual Detail Page DOMs** | Detail pages vary wildly between `legacy` (tables) and `v2` (flex key-value divs). | Match keys by lowercase alphanumeric normalization (`strip()`, `lower()`, remove non-alphanumeric). |
| **Portal Rate Limits** | Concurrent burst requests can trigger 429 or connection drops. | Limit concurrency to `3` via `asyncio.Semaphore(3)` with exponential backoff on retry. |
