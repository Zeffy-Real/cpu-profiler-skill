"""Tests for the FlameGraph generation module.

9 tests covering:
- FlameGraphGeneration: full pipeline, missing file, custom params, empty range (4 tests)
- FlameGraphUnit: init params, defaults, tool check, index query, failed slice filtering (5 tests)

Tests requiring perf/flamegraph tools are automatically skipped.
"""

import os
import subprocess
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from src.core.flamegraph import FlameGraphGenerator, FlameGraphError
from src.collector.rotator import FileRotator


@pytest.fixture
def real_perf_data(tmp_data_dir, perf_available):
    """Create real perf data (3-second recording) for integration tests."""
    ts = datetime.now()
    filename = FileRotator.get_slice_filename(ts)
    file_path = os.path.join(tmp_data_dir, filename)

    cmd = ["perf", "record", "-F", "99", "-a", "-g", "-o", file_path, "--", "sleep", "3"]
    result = subprocess.run(cmd, capture_output=True)

    if result.returncode != 0:
        pytest.skip(f"perf record failed: {result.stderr.decode(errors='replace')}")

    # Update index
    size_bytes = os.path.getsize(file_path) if os.path.exists(file_path) else 0
    FileRotator.add_slice_to_index(
        data_dir=tmp_data_dir,
        timestamp=ts,
        file_path=file_path,
        duration=30,
        size_bytes=size_bytes,
        status="success",
    )

    return {
        "file_path": file_path,
        "timestamp": ts,
        "data_dir": tmp_data_dir,
    }


class TestFlameGraphGeneration:
    """Integration tests for flame graph generation pipeline."""

    def test_generate_flamegraph(self, real_perf_data, tmp_data_dir, flamegraph_available, stackcollapse_available):
        """Verify complete pipeline produces valid SVG."""
        gen = FlameGraphGenerator()
        output_dir = os.path.join(tmp_data_dir, "output")
        svg_path = gen.generate(real_perf_data["file_path"], output_dir)

        assert os.path.exists(svg_path)
        with open(svg_path, "r") as f:
            content = f.read()
        assert "<svg" in content
        assert "flame" in content.lower() or "stack" in content.lower()

    def test_generate_missing_file(self, tmp_data_dir):
        """Verify FlameGraphError for non-existent file."""
        gen = FlameGraphGenerator()
        with pytest.raises(FlameGraphError) as exc_info:
            gen.generate("/tmp/nonexistent_perf_file", tmp_data_dir)
        assert "not found" in str(exc_info.value).lower()

    def test_generate_with_custom_params(self, real_perf_data, tmp_data_dir, flamegraph_available, stackcollapse_available):
        """Verify custom width/height/title are applied."""
        gen = FlameGraphGenerator(width=800, height=20, title="Test Graph")
        output_dir = os.path.join(tmp_data_dir, "output")
        svg_path = gen.generate(real_perf_data["file_path"], output_dir)

        with open(svg_path, "r") as f:
            content = f.read()
        assert "<svg" in content
        assert "Test Graph" in content

    def test_generate_from_time_range_empty(self, tmp_data_dir):
        """Verify exception when no slices in range."""
        gen = FlameGraphGenerator()
        start = datetime(2020, 1, 1, 0, 0, 0)
        end = datetime(2020, 1, 1, 1, 0, 0)
        with pytest.raises(FlameGraphError) as exc_info:
            gen.generate_from_time_range(tmp_data_dir, start, end, tmp_data_dir)
        assert "No perf data slices" in str(exc_info.value)


class TestFlameGraphUnit:
    """Unit tests for FlameGraphGenerator."""

    def test_init_params(self):
        """Verify constructor parameters are set."""
        gen = FlameGraphGenerator(width=1000, height=24, title="My Title")
        assert gen.width == 1000
        assert gen.height == 24
        assert gen.title == "My Title"

    def test_init_defaults(self):
        """Verify default values when no params given."""
        gen = FlameGraphGenerator()
        assert gen.width == 1200
        assert gen.height == 16
        assert gen.title == "CPU Flame Graph"

    def test_check_tools_missing(self, monkeypatch):
        """Verify FlameGraphError when tools are missing."""
        gen = FlameGraphGenerator()
        # Mock shutil.which to return None for all tools
        monkeypatch.setattr("shutil.which", lambda x: None)
        # Also mock os.path.exists to return False for tool paths
        monkeypatch.setattr("os.path.exists", lambda x: False)
        with pytest.raises(FlameGraphError) as exc_info:
            gen._check_tools()
        assert "Missing required tools" in str(exc_info.value)

    def test_find_slices_in_range_with_index(self, tmp_data_dir):
        """Verify index-based slice lookup."""
        ts1 = datetime(2026, 7, 24, 10, 0, 0)
        ts2 = datetime(2026, 7, 24, 11, 0, 0)

        # Create perf data files
        for ts in [ts1, ts2]:
            filename = FileRotator.get_slice_filename(ts)
            file_path = os.path.join(tmp_data_dir, filename)
            Path(file_path).write_text("dummy")
            FileRotator.add_slice_to_index(
                data_dir=tmp_data_dir,
                timestamp=ts,
                file_path=file_path,
                duration=30,
                size_bytes=100,
                status="success",
            )

        gen = FlameGraphGenerator()
        start = datetime(2026, 7, 24, 9, 0, 0)
        end = datetime(2026, 7, 24, 12, 0, 0)
        result = gen._find_slices_in_range(tmp_data_dir, start, end)

        assert len(result) == 2
        assert result[0][1] == ts1
        assert result[1][1] == ts2

    def test_find_slices_skips_failed(self, tmp_data_dir):
        """Verify failed slices are skipped (no physical file created)."""
        ts_success = datetime(2026, 7, 24, 10, 0, 0)
        ts_failed = datetime(2026, 7, 24, 11, 0, 0)

        # Create success slice with physical file
        filename = FileRotator.get_slice_filename(ts_success)
        file_path = os.path.join(tmp_data_dir, filename)
        Path(file_path).write_text("dummy")
        FileRotator.add_slice_to_index(
            data_dir=tmp_data_dir,
            timestamp=ts_success,
            file_path=file_path,
            duration=30,
            size_bytes=100,
            status="success",
        )

        # Add failed slice WITHOUT creating physical file
        # (failed slices don't create physical files per convention)
        FileRotator.add_slice_to_index(
            data_dir=tmp_data_dir,
            timestamp=ts_failed,
            file_path=os.path.join(tmp_data_dir, FileRotator.get_slice_filename(ts_failed)),
            duration=30,
            size_bytes=0,
            status="failed",
        )

        gen = FlameGraphGenerator()
        start = datetime(2026, 7, 24, 9, 0, 0)
        end = datetime(2026, 7, 24, 12, 0, 0)
        result = gen._find_slices_in_range(tmp_data_dir, start, end)

        # Only the success slice should be returned
        assert len(result) == 1
        assert result[0][1] == ts_success
