"""Tests for the API server - health, slices, and flamegraph endpoints.

10 tests covering:
- Health check (1 test)
- Slice listing with filters (3 tests)
- Flamegraph generation - single point and range (6 tests)

Uses FastAPI TestClient for all tests. Tests requiring perf/flamegraph
tools are automatically skipped if tools are not available.
"""

import os
import subprocess
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.core.config import Config
from src.collector.rotator import FileRotator


@pytest.fixture
def api_client(tmp_data_dir):
    """Provide a TestClient with config pointing to a temp directory."""
    test_config = Config(
        sample_freq=99,
        slice_duration=30,
        retention_hours=2,
        data_dir=tmp_data_dir,
        api_host="127.0.0.1",
        api_port=8765,
    )
    # Patch the module-level config in server
    import src.api.server as server_module
    original_config = server_module.config
    server_module.config = test_config

    client = TestClient(server_module.app)
    yield client

    # Restore original config
    server_module.config = original_config


@pytest.fixture
def perf_slice(api_client, perf_available):
    """Create a real perf data slice and update the index."""
    import src.api.server as server_module
    data_dir = server_module.config.data_dir

    ts = datetime.now()
    filename = FileRotator.get_slice_filename(ts)
    file_path = os.path.join(data_dir, filename)

    # Run perf record for 1 second
    cmd = ["perf", "record", "-F", "99", "-a", "-g", "-o", file_path, "--", "sleep", "1"]
    result = subprocess.run(cmd, capture_output=True)

    if result.returncode != 0:
        pytest.skip(f"perf record failed: {result.stderr.decode(errors='replace')}")

    size_bytes = os.path.getsize(file_path) if os.path.exists(file_path) else 0

    FileRotator.add_slice_to_index(
        data_dir=data_dir,
        timestamp=ts,
        file_path=file_path,
        duration=30,
        size_bytes=size_bytes,
        status="success",
    )

    return {
        "timestamp": ts.strftime("%Y%m%d_%H%M%S"),
        "file": file_path,
        "duration": 30,
        "size_bytes": size_bytes,
        "status": "success",
        "datetime": ts,
    }


class TestHealth:
    """Tests for GET /api/v1/health."""

    def test_health(self, api_client):
        """Verify health endpoint returns 200 with all fields."""
        response = api_client.get("/api/v1/health")
        assert response.status_code == 200
        data = response.json()
        assert "status" in data
        assert "collector_running" in data
        assert "data_dir" in data
        assert "disk_usage_mb" in data
        assert "slice_count" in data
        assert data["slice_count"] == 0


class TestSlices:
    """Tests for GET /api/v1/profile/slices."""

    def test_get_slices_empty(self, api_client):
        """Verify empty slice list when no data."""
        response = api_client.get("/api/v1/profile/slices")
        assert response.status_code == 200
        data = response.json()
        assert data["slices"] == []
        assert data["total_count"] == 0

    def test_get_slices_with_data(self, api_client):
        """Verify slices are returned from index."""
        import src.api.server as server_module
        data_dir = server_module.config.data_dir

        ts = datetime.now()
        FileRotator.add_slice_to_index(
            data_dir=data_dir,
            timestamp=ts,
            file_path="/tmp/perf.data.test",
            duration=30,
            size_bytes=1024,
            status="success",
        )

        response = api_client.get("/api/v1/profile/slices")
        assert response.status_code == 200
        data = response.json()
        assert data["total_count"] == 1
        assert data["slices"][0]["timestamp"] == ts.strftime("%Y%m%d_%H%M%S")

    def test_get_slices_with_filter(self, api_client):
        """Verify start/end time filtering."""
        import src.api.server as server_module
        data_dir = server_module.config.data_dir

        # Add slices at different times
        ts1 = datetime(2026, 7, 24, 10, 0, 0)
        ts2 = datetime(2026, 7, 24, 11, 0, 0)
        ts3 = datetime(2026, 7, 24, 12, 0, 0)

        for ts in [ts1, ts2, ts3]:
            FileRotator.add_slice_to_index(
                data_dir=data_dir,
                timestamp=ts,
                file_path=f"/tmp/perf.data.{ts.strftime('%Y%m%d_%H%M%S')}",
                duration=30,
                size_bytes=1024,
                status="success",
            )

        # Filter: only 11:00
        response = api_client.get(
            "/api/v1/profile/slices",
            params={"start": "20260724_110000", "end": "20260724_115000"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["total_count"] == 1
        assert data["slices"][0]["timestamp"] == "20260724_110000"


class TestFlamegraph:
    """Tests for flamegraph endpoints."""

    def test_get_flamegraph_not_found(self, api_client):
        """Verify 404 when no slice matches the time."""
        response = api_client.get(
            "/api/v1/profile/flamegraph",
            params={"time": "20260101_000000"},
        )
        assert response.status_code == 404

    def test_get_flamegraph_missing_file(self, api_client):
        """Verify 404 when index has entry but file doesn't exist."""
        import src.api.server as server_module
        data_dir = server_module.config.data_dir

        ts = datetime.now()
        FileRotator.add_slice_to_index(
            data_dir=data_dir,
            timestamp=ts,
            file_path="/tmp/nonexistent_perf_data_file",
            duration=30,
            size_bytes=1024,
            status="success",
        )

        response = api_client.get(
            "/api/v1/profile/flamegraph",
            params={"time": ts.strftime("%Y%m%d_%H%M%S")},
        )
        assert response.status_code == 404

    def test_get_flamegraph_success(self, api_client, perf_slice, flamegraph_available, stackcollapse_available):
        """Verify successful flamegraph generation from real perf data."""
        response = api_client.get(
            "/api/v1/profile/flamegraph",
            params={"time": perf_slice["timestamp"]},
        )
        assert response.status_code == 200
        assert "image/svg+xml" in response.headers.get("content-type", "")
        assert "<svg" in response.text
        assert "flame" in response.text.lower()

    def test_post_flamegraph_invalid_range(self, api_client):
        """Verify 400 when end_time <= start_time."""
        response = api_client.post(
            "/api/v1/profile/flamegraph",
            json={
                "start_time": "20260724_120000",
                "end_time": "20260724_110000",
            },
        )
        assert response.status_code == 400

    def test_post_flamegraph_no_slices(self, api_client):
        """Verify 404 when no slices in range."""
        response = api_client.post(
            "/api/v1/profile/flamegraph",
            json={
                "start_time": "20260101_000000",
                "end_time": "20260101_010000",
            },
        )
        assert response.status_code == 404

    def test_post_flamegraph_range(self, api_client, perf_slice, flamegraph_available, stackcollapse_available):
        """Verify successful range flamegraph generation."""
        ts = perf_slice["datetime"]
        start = (ts - timedelta(seconds=60)).strftime("%Y%m%d_%H%M%S")
        end = (ts + timedelta(seconds=60)).strftime("%Y%m%d_%H%M%S")

        response = api_client.post(
            "/api/v1/profile/flamegraph",
            json={
                "start_time": start,
                "end_time": end,
            },
        )
        assert response.status_code == 200
        assert "image/svg+xml" in response.headers.get("content-type", "")
        assert "<svg" in response.text
