"""FastAPI server exposing CPU profiling data via REST API.

Endpoints:
    GET  /api/v1/health              — health check
    GET  /api/v1/profile/slices      — list perf data slices
    GET  /api/v1/profile/flamegraph  — generate flame graph for a single point in time
    POST /api/v1/profile/flamegraph  — generate flame graph for a time range
"""

import logging
import os
import subprocess
from datetime import datetime, timedelta
from typing import Optional, Tuple

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response

from src.core.config import Config
from src.core.flamegraph import FlameGraphGenerator, FlameGraphError
from src.collector.rotator import FileRotator
from src.api.models import (
    ErrorResponse,
    FlameGraphRequest,
    HealthResponse,
    SliceInfo,
    SlicesResponse,
)

logger = logging.getLogger("cpu-profiler.api")

# ----------------------------------------------------------------------
# Module-level config — loaded once at import time.
# DATA_DIR env var (set by conftest.py in tests) overrides the default.
# ----------------------------------------------------------------------
config = Config.from_env()

# Try to ensure the data directory exists; gracefully handle permission
# errors (e.g. non-root user, /var/lib/cpu-profiler not writable).
try:
    config.ensure_data_dir()
except PermissionError:
    logger.warning(
        "Cannot create data directory %s — running with reduced functionality",
        config.data_dir,
    )


# ----------------------------------------------------------------------
# FastAPI app
# ----------------------------------------------------------------------

app = FastAPI(
    title="CPU Profiler API",
    description="Continuous CPU profiling with perf and flame graphs",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ----------------------------------------------------------------------
# Helper functions
# ----------------------------------------------------------------------

def _parse_time_str(time_str: str) -> Optional[datetime]:
    """Parse a YYYYMMDD_HHMMSS string to datetime. Returns None on failure."""
    try:
        return datetime.strptime(time_str, "%Y%m%d_%H%M%S")
    except (ValueError, TypeError):
        return None


def _find_slice_for_time(target: datetime, duration: int = 30) -> Optional[Tuple[str, datetime]]:
    """Find the perf data slice covering the target time.

    Double matching strategy:
    1. ts <= target <= ts + slice_duration  (target falls within slice)
    2. target - duration <= ts <= target     (slice starts within lookback window)

    Returns (file_path, timestamp) or None if no match found.
    """
    index = FileRotator.read_index(config.data_dir)

    for entry in index.get("slices", []):
        if entry.get("status") == "failed":
            continue

        ts_str = entry.get("timestamp", "")
        ts = FileRotator.parse_slice_timestamp(f"perf.data.{ts_str}")
        if ts is None:
            continue

        slice_duration = entry.get("duration", duration)

        # Match strategy 1: target falls within [ts, ts + duration]
        if ts <= target <= _add_seconds(ts, slice_duration):
            file_path = entry.get("file", "")
            if file_path and os.path.exists(file_path):
                return (file_path, ts)

        # Match strategy 2: ts falls within [target - duration, target]
        lookback = _add_seconds(target, -duration)
        if lookback <= ts <= target:
            file_path = entry.get("file", "")
            if file_path and os.path.exists(file_path):
                return (file_path, ts)

    return None


def _add_seconds(dt: datetime, seconds: int) -> datetime:
    """Add seconds to a datetime (handles negative values)."""
    return dt + timedelta(seconds=seconds)


def _is_collector_running() -> bool:
    """Check if the profiler daemon is running using pgrep."""
    try:
        result = subprocess.run(
            ["pgrep", "-f", "ProfilerDaemon"],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


# ----------------------------------------------------------------------
# Endpoints
# ----------------------------------------------------------------------

@app.get("/api/v1/health", response_model=HealthResponse)
async def health_check():
    """Health check endpoint.

    Returns service status, collector state, disk usage, and slice count.
    """
    disk_usage_bytes = FileRotator.get_disk_usage(config.data_dir)
    disk_usage_mb = round(disk_usage_bytes / (1024 * 1024), 2)

    index = FileRotator.read_index(config.data_dir)
    slice_count = len(index.get("slices", []))

    return HealthResponse(
        status="ok",
        collector_running=_is_collector_running(),
        data_dir=config.data_dir,
        disk_usage_mb=disk_usage_mb,
        slice_count=slice_count,
    )


@app.get("/api/v1/profile/slices", response_model=SlicesResponse)
async def get_slices(
    start: Optional[str] = Query(default=None, description="Start time YYYYMMDD_HHMMSS"),
    end: Optional[str] = Query(default=None, description="End time YYYYMMDD_HHMMSS"),
):
    """List perf data slices, optionally filtered by time range."""
    index = FileRotator.read_index(config.data_dir)
    slices = index.get("slices", [])

    # Apply time filters if provided
    if start:
        slices = [s for s in slices if s.get("timestamp", "") >= start]
    if end:
        slices = [s for s in slices if s.get("timestamp", "") <= end]

    slice_infos = [
        SliceInfo(
            timestamp=s.get("timestamp", ""),
            file=s.get("file", ""),
            duration=s.get("duration", 0),
            size_bytes=s.get("size_bytes", 0),
            status=s.get("status", "unknown"),
        )
        for s in slices
    ]

    return SlicesResponse(slices=slice_infos, total_count=len(slice_infos))


@app.get("/api/v1/profile/flamegraph")
async def get_flamegraph(
    time: str = Query(..., description="Target time as YYYYMMDD_HHMMSS"),
    duration: int = Query(default=30, description="Lookback duration in seconds"),
    width: int = Query(default=1200, description="SVG width"),
    height: int = Query(default=16, description="Frame height"),
    title: str = Query(default="CPU Flame Graph", description="Graph title"),
):
    """Generate a flame graph for a single point in time.

    Finds the perf data slice covering the specified time and generates
    a flame graph SVG.
    """
    target = _parse_time_str(time)
    if target is None:
        raise HTTPException(status_code=400, detail=f"Invalid time format: {time}")

    result = _find_slice_for_time(target, duration)
    if result is None:
        raise HTTPException(status_code=404, detail=f"No perf data slice found for time {time}")

    file_path, _ts = result

    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail=f"Perf data file not found: {file_path}")

    try:
        gen = FlameGraphGenerator(width=width, height=height, title=title)
        output_dir = os.path.join(config.data_dir, "output")
        svg_path = gen.generate(file_path, output_dir)

        with open(svg_path, "r", encoding="utf-8") as f:
            svg_content = f.read()

        return Response(content=svg_content, media_type="image/svg+xml")

    except FlameGraphError as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/v1/profile/flamegraph")
async def post_flamegraph(request: FlameGraphRequest):
    """Generate a flame graph for a time range.

    Merges all perf data slices within the specified time range into
    a single flame graph SVG.
    """
    start_dt = _parse_time_str(request.start_time)
    end_dt = _parse_time_str(request.end_time)

    if start_dt is None or end_dt is None:
        raise HTTPException(status_code=400, detail="Invalid time format (expected YYYYMMDD_HHMMSS)")

    if end_dt <= start_dt:
        raise HTTPException(status_code=400, detail="end_time must be after start_time")

    try:
        gen = FlameGraphGenerator(
            width=request.width or 1200,
            height=request.height or 16,
            title=request.title or "CPU Flame Graph",
        )
        output_dir = os.path.join(config.data_dir, "output")
        svg_path = gen.generate_from_time_range(
            config.data_dir, start_dt, end_dt, output_dir
        )

        with open(svg_path, "r", encoding="utf-8") as f:
            svg_content = f.read()

        return Response(content=svg_content, media_type="image/svg+xml")

    except FlameGraphError as e:
        if "No perf data slices" in str(e):
            raise HTTPException(status_code=404, detail=str(e))
        raise HTTPException(status_code=500, detail=str(e))


# ----------------------------------------------------------------------
# Global exception handler
# ----------------------------------------------------------------------

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Catch-all exception handler returning a structured error response."""
    logger.error("Unhandled exception: %s", exc, exc_info=True)
    return Response(
        content=ErrorResponse(
            error="internal_server_error",
            detail=str(exc),
        ).model_dump_json(),
        status_code=500,
        media_type="application/json",
    )
