# Engineering Reflection

### 1. Assumptions
I assumed that all timestamp fields returned by the Urja Ops portal are in Indian Standard Time (`Asia/Kolkata`, `+05:30`), as the physical meter installations and distribution transformers are localized around the Jaipur electrical grid. I also assumed that cumulative `kwh` and `kvah` registers are strictly monotonic counters where differences reflect consumption, that missing sensor values like `voltage_r` or blank hierarchy codes are best represented as explicit `null`s rather than dummy zero values or empty strings, and that an in-memory index seeded from the bulk export file is the optimal bridge between upstream latency/concurrency limits and immediate client responsiveness.

### 2. Hardest Part and How I Got Unstuck
The hardest part was reconciling the dual DOM templates (`legacy` key-value tables versus `v2` flex list containers) without writing brittle, hardcoded index selectors for each variation. I got unstuck by decoupling the HTML extraction into an alphanumeric key-normalization pipeline (`normalize_key`), which strips punctuation, casing, and whitespace so both `"Installation Status"` and `"InstallationStatus"` collapse into `"installationstatus"`. This allowed a single unified parsing schema to cleanly ingest all 403 meters across both builds into standard Pydantic models.

### 3. What I'd Do With Another Day
With another full day, I would implement SQLite with Spatialite or PostGIS for geo-spatial indexing instead of performing in-memory Haversine distance scans across all meters, which would scale better to millions of meters. I would also build the optional React + Vite interactive dashboard featuring an interactive Leaflet map of Jaipur's feeders, dynamic charts for half-hourly load curves, and a visual hierarchy breadcrumb explorer to make data quality anomalies immediately visible to ops engineers.

### 4. A Real Mistake I Made
A real mistake I made during initial API route registration was placing the parameterized route `@app.get("/api/v1/meters/{meter_id}")` before the static route `@app.get("/api/v1/meters/nearby")`. Because of FastAPI/Starlette URL matching order, calls to `/meters/nearby` were intercepted by `{meter_id}` with `meter_id="nearby"`, failing regex validation `^J\d+$` and returning a `400 Bad Request`. I diagnosed this quickly by inspecting the test failure traceback, moved the `/nearby` endpoint above the parameterized route, and added automated regression tests to prevent regressions.

### 5. What I'd Criticise in My Own Submission
I would criticise the consumption interval fallback mechanism: while the parser extracts live half-hourly consumption tables from the detail HTML seamlessly, when running offline or against meters where the portal provides truncated history, the service falls back to a simulated deterministic interval reading rather than persisting and aggregating historical time-series intervals in a dedicated time-series database like TimescaleDB or InfluxDB.
