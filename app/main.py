import sys
import re
import logging
from pathlib import Path
from typing import Optional, List

# Ensure project root is in sys.path for Vercel execution
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from fastapi import FastAPI, Request, Response, Query, HTTPException, status
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager

from app.service import MeterService
from app.client import PortalAuthError, PortalUnavailable, PortalParseError
from app.models import (
    Meter,
    MeterSummary,
    Page,
    ConsumptionResponse,
    ErrorResponse,
    DashboardStats,
)

logger = logging.getLogger(__name__)

METER_ID_REGEX = re.compile(r"^J\d+$")

# Singleton service instance
service: Optional[MeterService] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global service
    service = MeterService()
    logger.info("MeterOps API starting: fetching live bulk export from portal...")
    try:
        source = await service.load_or_refresh_bulk_data(force_refresh=True)
        logger.info(f"Bulk data initialized with source={source}")
    except Exception as e:
        logger.warning(f"Initial bulk export fetch failed: {e}")
    yield
    if service and service.client:
        await service.client.close()
    logger.info("MeterOps API shutdown complete")


app = FastAPI(
    title="Flock Energy - Urja Meter Ops API",
    description="Clean, normalized, and cached REST API layer over the legacy Urja Ops portal.",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    lifespan=lifespan,
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

STATIC_DIR = ROOT_DIR / "static"
INDEX_HTML = STATIC_DIR / "index.html"
PUBLIC_HTML = ROOT_DIR / "public" / "index.html"

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", include_in_schema=False)
async def serve_dashboard():
    if PUBLIC_HTML.exists():
        return FileResponse(PUBLIC_HTML)
    if INDEX_HTML.exists():
        return FileResponse(INDEX_HTML)
    return JSONResponse({"message": "Urja Meter Ops API is running. Visit /docs for Swagger UI."})


def get_service() -> MeterService:
    global service
    if service is None:
        service = MeterService()
    return service


# --- Exception Handlers for Consistent Error Responses ---

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    code_map = {
        400: "BAD_REQUEST",
        404: "NOT_FOUND",
        422: "VALIDATION_ERROR",
        500: "INTERNAL_SERVER_ERROR",
        502: "BAD_GATEWAY",
        503: "SERVICE_UNAVAILABLE",
    }
    error_code = code_map.get(exc.status_code, "ERROR")
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": error_code, "message": str(exc.detail)}},
    )


@app.exception_handler(PortalAuthError)
async def portal_auth_error_handler(request: Request, exc: PortalAuthError):
    return JSONResponse(
        status_code=status.HTTP_502_BAD_GATEWAY,
        content={"error": {"code": "BAD_GATEWAY", "message": f"Authentication to upstream portal failed: {exc}"}},
    )


@app.exception_handler(PortalUnavailable)
async def portal_unavailable_error_handler(request: Request, exc: PortalUnavailable):
    return JSONResponse(
        status_code=status.HTTP_502_BAD_GATEWAY,
        content={"error": {"code": "BAD_GATEWAY", "message": f"Upstream Urja portal is unreachable: {exc}"}},
    )


@app.exception_handler(PortalParseError)
async def portal_parse_error_handler(request: Request, exc: PortalParseError):
    return JSONResponse(
        status_code=status.HTTP_502_BAD_GATEWAY,
        content={"error": {"code": "BAD_GATEWAY", "message": f"Failed to parse upstream response: {exc}"}},
    )


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    logger.exception(f"Unhandled error processing {request.url.path}: {exc}")
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"error": {"code": "INTERNAL_SERVER_ERROR", "message": str(exc)}},
    )


# --- API Routes ---

@app.get(
    "/api/v1/health",
    summary="Health Check",
    description="Check API service status, index metrics, and upstream portal reachability via real login ping.",
    tags=["System"],
)
async def health(response: Response):
    svc = get_service()
    res = await svc.check_health()
    response.headers["X-Data-Source"] = svc.data_source
    return res


@app.get(
    "/api/v1/stats",
    response_model=DashboardStats,
    summary="Dashboard Statistics",
    description="Compute operational summary statistics directly from real index/portal data.",
    tags=["System"],
)
def get_stats(response: Response):
    svc = get_service()
    stats = svc.get_stats()
    response.headers["X-Data-Source"] = svc.data_source
    return stats


@app.get(
    "/api/v1/meters",
    response_model=Page[MeterSummary],
    summary="List Meters",
    description="Retrieve paginated meters directory with optional filtering by query, make, status, phase type, and DT code.",
    tags=["Meters"],
)
async def list_meters(
    response: Response,
    page: int = Query(1, ge=1, description="Page number (1-indexed)"),
    page_size: int = Query(20, ge=1, le=500, description="Items per page"),
    q: Optional[str] = Query(None, description="Search term matching meter ID or serial number"),
    make: Optional[str] = Query(None, description="Filter by make: HPL, Genus, Secure, Allied, L&T"),
    status: Optional[str] = Query(None, description="Filter by status: Installed, Faulty, Decommissioned"),
    phase_type: Optional[str] = Query(None, description="Filter by phase: single or three"),
    dt_code: Optional[str] = Query(None, description="Filter by distribution transformer code, e.g. DT-001"),
    refresh: bool = Query(False, description="Force refresh bulk export from portal"),
):
    svc = get_service()
    if refresh:
        await svc.load_or_refresh_bulk_data(force_refresh=True)
    page_res = svc.list_meters(
        page=page,
        page_size=page_size,
        q=q,
        make=make,
        status=status,
        phase_type=phase_type,
        dt_code=dt_code,
    )
    response.headers["X-Data-Source"] = page_res.source
    return page_res


@app.get(
    "/api/v1/meters/nearby",
    summary="Find Nearby Meters",
    description="Find meters within a specified radius using Haversine great-circle distance calculation.",
    tags=["Meters"],
)
async def get_nearby_meters(
    response: Response,
    lat: float = Query(..., description="Latitude of center point"),
    lng: float = Query(..., description="Longitude of center point"),
    radius_km: float = Query(5.0, ge=0.1, le=100.0, description="Search radius in kilometers"),
    limit: int = Query(20, ge=1, le=100, description="Maximum meters to return"),
    refresh: bool = Query(False, description="Force refresh bulk export from portal"),
):
    svc = get_service()
    if refresh:
        await svc.load_or_refresh_bulk_data(force_refresh=True)
    nearby = svc.find_nearby_meters(lat=lat, lng=lng, radius_km=radius_km, limit=limit)
    response.headers["X-Data-Source"] = svc.data_source
    return nearby


@app.get(
    "/api/v1/meters/{meter_id}",
    response_model=Meter,
    responses={
        400: {"model": ErrorResponse, "description": "Invalid meter ID format"},
        404: {"model": ErrorResponse, "description": "Meter not found"},
        502: {"model": ErrorResponse, "description": "Upstream portal unavailable"},
    },
    summary="Get Meter Details",
    description="Retrieve full details for a meter including nameplate, coordinates, and normalized distribution hierarchy.",
    tags=["Meters"],
)
async def get_meter(
    meter_id: str,
    response: Response,
    refresh: bool = Query(False, description="Bypass cache and force refresh from upstream portal"),
):
    if not METER_ID_REGEX.match(meter_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid meter ID '{meter_id}'. Meter IDs must follow format 'J' followed by digits (e.g. J100000).",
        )

    svc = get_service()
    try:
        meter, is_hit, source = await svc.get_meter(meter_id=meter_id, refresh=refresh)
        response.headers["X-Cache"] = "HIT" if is_hit else "MISS"
        response.headers["X-Data-Source"] = source
        return meter
    except KeyError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Meter '{meter_id}' not found.",
        )


@app.get(
    "/api/v1/meters/{meter_id}/consumption",
    response_model=ConsumptionResponse,
    responses={
        400: {"model": ErrorResponse, "description": "Invalid meter ID format"},
        404: {"model": ErrorResponse, "description": "Meter not found"},
        502: {"model": ErrorResponse, "description": "Upstream portal unavailable"},
    },
    summary="Get Meter Interval Consumption",
    description="Retrieve half-hourly kWh, kVAh, and voltage interval readings sorted ascending by ISO timestamp. Never synthesized.",
    tags=["Consumption"],
)
async def get_consumption(
    meter_id: str,
    response: Response,
    from_: Optional[str] = Query(None, alias="from", description="Earliest ISO timestamp (inclusive)"),
    to: Optional[str] = Query(None, description="Latest ISO timestamp (inclusive)"),
    limit: Optional[int] = Query(None, ge=1, le=1000, description="Maximum number of readings to return"),
    refresh: bool = Query(False, description="Bypass cache and force refresh from upstream portal"),
):
    if not METER_ID_REGEX.match(meter_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid meter ID '{meter_id}'. Format must be 'J' followed by digits.",
        )

    svc = get_service()
    try:
        data, is_hit = await svc.get_consumption(
            meter_id=meter_id,
            from_ts=from_,
            to_ts=to,
            limit=limit,
            refresh=refresh,
        )
        response.headers["X-Cache"] = "HIT" if is_hit else "MISS"
        response.headers["X-Data-Source"] = "live"
        return data
    except KeyError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Meter '{meter_id}' not found.",
        )


@app.get(
    "/api/v1/hierarchy",
    summary="Get Hierarchy Tree",
    description="Get the full distribution tree keyed by unique composite paths, with data quality anomaly reporting.",
    tags=["Hierarchy"],
)
async def get_hierarchy(
    response: Response,
    refresh: bool = Query(False, description="Force refresh bulk export from portal"),
):
    svc = get_service()
    if refresh:
        await svc.load_or_refresh_bulk_data(force_refresh=True)
    tree = svc.get_hierarchy_tree()
    response.headers["X-Data-Source"] = svc.data_source
    return tree


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=True)
