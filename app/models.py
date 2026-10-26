from typing import Optional, Generic, TypeVar, List, Dict
from pydantic import BaseModel, Field, ConfigDict

T = TypeVar("T")


class Location(BaseModel):
    """Geographic coordinates for a meter."""
    lat: float = Field(..., description="Latitude")
    lng: float = Field(..., description="Longitude")

    model_config = ConfigDict(extra="ignore")


class HierarchyNode(BaseModel):
    """A node in the electrical distribution hierarchy."""
    level: str = Field(..., description="Hierarchy level: zone, circle, division, subdivision, substation, feeder, dt")
    code: Optional[str] = Field(None, description="Alphanumeric code, e.g. SD-01 (null if missing)")
    name: Optional[str] = Field(None, description="Human readable name, e.g. Malviya Nagar DT 1 (null if missing)")
    path_key: str = Field(..., description="Unique composite path key from root to this node")

    model_config = ConfigDict(extra="ignore")


class MeterSummary(BaseModel):
    """Summary of meter information returned in directory listings."""
    meter_id: str = Field(..., description="Unique meter identifier, e.g. J100000")
    serial_no: str = Field(..., description="Manufacturer serial number")
    make: str = Field(..., description="Manufacturer make: HPL, Genus, Secure, Allied, L&T")
    phase_type: str = Field(..., description="Phase type: single or three")
    status: str = Field(..., description="Operational status: Installed, Faulty, Decommissioned")
    dt_code: str = Field(..., description="Distribution transformer code, e.g. DT-001")

    model_config = ConfigDict(extra="ignore")


class Meter(MeterSummary):
    """Detailed meter record including nameplate, coordinates, and hierarchy."""
    install_type: Optional[str] = Field(None, description="Installation type: Whole Current or CT Operated")
    build: Optional[str] = Field(None, description="Portal template build: legacy or v2")
    location: Location = Field(..., description="Geographic coordinates")
    hierarchy: Dict[str, HierarchyNode] = Field(..., description="Keyed hierarchy from zone to distribution transformer")
    source: str = Field("live", description="Data source: 'live' or 'export_snapshot'")

    model_config = ConfigDict(extra="ignore")


class Reading(BaseModel):
    """Half-hourly interval meter reading."""
    timestamp: str = Field(..., description="ISO 8601 timestamp with +05:30 timezone offset")
    kwh: float = Field(..., description="Cumulative active energy reading (kWh)")
    kvah: float = Field(..., description="Cumulative apparent energy reading (kVAh)")
    voltage_r: Optional[float] = Field(None, description="R-phase voltage in Volts (null if missing or N/A)")

    model_config = ConfigDict(extra="ignore")


class ConsumptionResponse(BaseModel):
    """Consumption interval readings series for a meter."""
    meter_id: str = Field(..., description="Meter identifier")
    count: int = Field(..., description="Number of interval readings returned")
    from_: Optional[str] = Field(None, alias="from", description="Earliest ISO timestamp in response")
    to: Optional[str] = Field(None, description="Latest ISO timestamp in response")
    readings: List[Reading] = Field(default_factory=list, description="Ascending interval readings")

    model_config = ConfigDict(populate_by_name=True, extra="ignore")


class Page(BaseModel, Generic[T]):
    """Standardized paginated list envelope."""
    items: List[T] = Field(..., description="Page items")
    page: int = Field(..., description="1-indexed current page number")
    page_size: int = Field(..., description="Number of items per page")
    total: int = Field(..., description="Total items available")
    total_pages: int = Field(..., description="Total pages available")
    source: str = Field("live", description="Data source: 'live' or 'export_snapshot'")

    model_config = ConfigDict(extra="ignore")


class PhaseStats(BaseModel):
    """Statistics for phase distribution."""
    single: int = Field(..., description="Single phase count")
    three: int = Field(..., description="Three phase count")
    single_pct: float = Field(..., description="Percentage of single phase meters")
    three_pct: float = Field(..., description="Percentage of three phase meters")

    model_config = ConfigDict(extra="ignore")


class DashboardStats(BaseModel):
    """Real-time operational summary metrics computed from index/portal."""
    total_meters: int = Field(..., description="Total meters indexed")
    statuses: Dict[str, int] = Field(..., description="Breakdown by operational status")
    phases: PhaseStats = Field(..., description="Breakdown by phase type")
    makes: Dict[str, int] = Field(..., description="Breakdown by manufacturer")
    source: str = Field("live", description="Data source: 'live' or 'export_snapshot'")

    model_config = ConfigDict(extra="ignore")


class ErrorDetail(BaseModel):
    """Standard error response payload."""
    code: str = Field(..., description="Machine-readable error code")
    message: str = Field(..., description="Human-readable explanation of error")


class ErrorResponse(BaseModel):
    error: ErrorDetail
