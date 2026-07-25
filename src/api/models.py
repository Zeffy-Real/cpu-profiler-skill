"""Pydantic models for the CPU Profiler API.

These models define the request/response schemas for all API endpoints.
"""

from typing import List, Optional

from pydantic import BaseModel, Field


class FlameGraphRequest(BaseModel):
    """Request body for POST /api/v1/profile/flamegraph (range query)."""

    start_time: str = Field(..., description="Start time as YYYYMMDD_HHMMSS")
    end_time: str = Field(..., description="End time as YYYYMMDD_HHMMSS")
    width: Optional[int] = Field(default=1200, description="SVG width in pixels")
    height: Optional[int] = Field(default=16, description="Frame height in pixels")
    title: Optional[str] = Field(default="CPU Flame Graph", description="Graph title")


class SliceInfo(BaseModel):
    """Information about a single perf data slice."""

    timestamp: str = Field(..., description="Slice timestamp as YYYYMMDD_HHMMSS")
    file: str = Field(..., description="Path to the perf.data file")
    duration: int = Field(..., description="Slice duration in seconds")
    size_bytes: int = Field(..., description="File size in bytes")
    status: str = Field(..., description="Slice status: success or failed")


class SlicesResponse(BaseModel):
    """Response for GET /api/v1/profile/slices."""

    slices: List[SliceInfo] = Field(default_factory=list)
    total_count: int = Field(..., description="Total number of slices returned")


class HealthResponse(BaseModel):
    """Response for GET /api/v1/health."""

    status: str = Field(..., description="Service status: ok or degraded")
    collector_running: bool = Field(..., description="Whether the collector daemon is running")
    data_dir: str = Field(..., description="Path to the data directory")
    disk_usage_mb: float = Field(..., description="Disk usage of perf data in MB")
    slice_count: int = Field(..., description="Number of slices in the index")


class ErrorResponse(BaseModel):
    """Standard error response."""

    error: str = Field(..., description="Error type")
    detail: str = Field(..., description="Error details")
