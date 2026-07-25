"""Tests for the collector module - FileRotator and ProfilerDaemon.

26 tests covering:
- FileRotator: filename generation, timestamp parsing, cleanup, disk usage,
  index read/write, atomic writes, deduplication (16 tests)
- ProfilerDaemon: initialization, command building, dry run, signal handling,
  interruptible sleep, single-slice collection (10 tests)
"""

import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.core.config import Config
from src.collector.rotator import FileRotator
from src.collector.daemon import ProfilerDaemon


class TestFileRotator:
    """Tests for FileRotator static methods."""

    def test_get_slice_filename(self):
        """Verify filename format: perf.data.YYYYMMDD_HHMMSS."""
        ts = datetime(2026, 7, 24, 15, 30, 45)
        filename = FileRotator.get_slice_filename(ts)
        assert filename == "perf.data.20260724_153045"

    def test_get_slice_filename_default(self):
        """Verify default (now) produces a valid filename."""
        filename = FileRotator.get_slice_filename()
        assert filename.startswith("perf.data.")
        # Verify the timestamp part is parseable
        ts_str = filename.replace("perf.data.", "")
        datetime.strptime(ts_str, "%Y%m%d_%H%M%S")

    def test_parse_slice_timestamp(self):
        """Verify generate → parse roundtrip."""
        ts = datetime(2026, 7, 24, 15, 30, 45)
        filename = FileRotator.get_slice_filename(ts)
        parsed = FileRotator.parse_slice_timestamp(filename)
        assert parsed == ts

    def test_parse_slice_timestamp_invalid(self):
        """Verify invalid filename returns None."""
        assert FileRotator.parse_slice_timestamp("invalid.txt") is None
        assert FileRotator.parse_slice_timestamp("perf.data.invalid") is None
        assert FileRotator.parse_slice_timestamp("") is None

    def test_cleanup_expired(self, tmp_data_dir):
        """Verify old files are deleted and new files are kept."""
        # Create old file (3 hours ago)
        old_ts = datetime.now() - timedelta(hours=3)
        old_filename = FileRotator.get_slice_filename(old_ts)
        old_path = Path(tmp_data_dir) / old_filename
        old_path.write_text("old data")

        # Create new file (10 minutes ago)
        new_ts = datetime.now() - timedelta(minutes=10)
        new_filename = FileRotator.get_slice_filename(new_ts)
        new_path = Path(tmp_data_dir) / new_filename
        new_path.write_text("new data")

        # Cleanup with 2-hour retention
        deleted = FileRotator.cleanup_expired(tmp_data_dir, retention_hours=2)

        assert len(deleted) == 1
        assert not old_path.exists()
        assert new_path.exists()

    def test_cleanup_expired_empty_dir(self, tmp_data_dir):
        """Verify empty directory returns empty list."""
        deleted = FileRotator.cleanup_expired(tmp_data_dir, retention_hours=2)
        assert deleted == []

    def test_read_write_index(self, tmp_data_dir):
        """Verify write → read consistency."""
        data = {
            "version": 1,
            "slices": [
                {
                    "timestamp": "20260724_153045",
                    "file": "/tmp/perf.data.20260724_153045",
                    "duration": 30,
                    "size_bytes": 1024,
                    "status": "success",
                }
            ],
        }
        FileRotator.write_index(tmp_data_dir, data)
        result = FileRotator.read_index(tmp_data_dir)
        assert result == data

    def test_read_index_missing(self, tmp_data_dir):
        """Verify missing index returns empty skeleton."""
        result = FileRotator.read_index(tmp_data_dir)
        assert result == {"slices": [], "version": 1}

    def test_read_index_corrupted(self, tmp_data_dir):
        """Verify corrupted index returns empty skeleton."""
        index_path = Path(tmp_data_dir) / "index.json"
        index_path.write_text("{invalid json content!!!}")
        result = FileRotator.read_index(tmp_data_dir)
        assert result == {"slices": [], "version": 1}

    def test_write_index_atomic(self, tmp_data_dir):
        """Verify no residual .tmp file after write."""
        data = {"slices": [], "version": 1}
        FileRotator.write_index(tmp_data_dir, data)
        tmp_path = Path(tmp_data_dir) / "index.json.tmp"
        assert not tmp_path.exists()
        index_path = Path(tmp_data_dir) / "index.json"
        assert index_path.exists()

    def test_add_slice_to_index(self, tmp_data_dir):
        """Verify slice is appended and persisted."""
        ts = datetime(2026, 7, 24, 15, 30, 45)
        FileRotator.add_slice_to_index(
            data_dir=tmp_data_dir,
            timestamp=ts,
            file_path="/tmp/perf.data.20260724_153045",
            duration=30,
            size_bytes=1024,
            status="success",
        )
        index = FileRotator.read_index(tmp_data_dir)
        assert len(index["slices"]) == 1
        assert index["slices"][0]["timestamp"] == "20260724_153045"
        assert index["slices"][0]["status"] == "success"

    def test_add_slice_dedup(self, tmp_data_dir):
        """Verify same timestamp updates instead of duplicating."""
        ts = datetime(2026, 7, 24, 15, 30, 45)
        # Add first time
        FileRotator.add_slice_to_index(
            data_dir=tmp_data_dir,
            timestamp=ts,
            file_path="/tmp/perf.data.20260724_153045",
            duration=30,
            size_bytes=1024,
            status="success",
        )
        # Add again with same timestamp
        FileRotator.add_slice_to_index(
            data_dir=tmp_data_dir,
            timestamp=ts,
            file_path="/tmp/perf.data.20260724_153045",
            duration=30,
            size_bytes=2048,
            status="success",
        )
        index = FileRotator.read_index(tmp_data_dir)
        assert len(index["slices"]) == 1
        assert index["slices"][0]["size_bytes"] == 2048

    def test_get_disk_usage(self, tmp_data_dir):
        """Verify only perf.data.* files are counted."""
        # Create perf data files
        (Path(tmp_data_dir) / "perf.data.20260724_153045").write_text("x" * 100)
        (Path(tmp_data_dir) / "perf.data.20260724_153115").write_text("y" * 200)
        # Create non-perf files (should be ignored)
        (Path(tmp_data_dir) / "index.json").write_text("{}")
        (Path(tmp_data_dir) / "readme.txt").write_text("z" * 500)

        usage = FileRotator.get_disk_usage(tmp_data_dir)
        assert usage == 300

    def test_get_disk_usage_empty(self, tmp_data_dir):
        """Verify empty directory returns 0."""
        assert FileRotator.get_disk_usage(tmp_data_dir) == 0

    def test_check_disk_space_ok(self, tmp_data_dir):
        """Verify threshold 0 always passes."""
        assert FileRotator.check_disk_space(tmp_data_dir, min_space_gb=0) is True

    def test_check_disk_space_low(self, tmp_data_dir):
        """Verify extremely high threshold fails."""
        assert FileRotator.check_disk_space(tmp_data_dir, min_space_gb=999999) is False


class TestProfilerDaemon:
    """Tests for ProfilerDaemon."""

    def test_daemon_init(self, tmp_config):
        """Verify config is loaded and stop_requested is False."""
        daemon = ProfilerDaemon(config=tmp_config)
        assert daemon.config is tmp_config
        assert daemon.stop_requested is False
        assert daemon._process is None

    def test_daemon_init_default(self):
        """Verify no-args initialization works."""
        daemon = ProfilerDaemon()
        assert daemon.config is not None
        assert daemon.config.sample_freq == 99
        assert daemon.stop_requested is False

    def test_build_perf_command(self, tmp_config):
        """Verify command contains required flags."""
        daemon = ProfilerDaemon(config=tmp_config)
        cmd = daemon.build_perf_command("/tmp/output.perf")
        assert "-F" in cmd
        assert "99" in cmd
        assert "-a" in cmd
        assert "-g" in cmd
        assert "-o" in cmd
        assert "/tmp/output.perf" in cmd
        assert "--" in cmd
        assert "sleep" in cmd
        assert "30" in cmd

    def test_dry_run(self, tmp_config, capsys):
        """Verify dry run prints command without executing."""
        daemon = ProfilerDaemon(config=tmp_config)
        daemon.run(dry_run=True)
        captured = capsys.readouterr()
        assert "Dry run" in captured.out
        assert "perf" in captured.out
        assert "record" in captured.out

    def test_stop_flag_isolation(self, tmp_config):
        """Verify each daemon instance has independent stop flag."""
        d1 = ProfilerDaemon(config=tmp_config)
        d2 = ProfilerDaemon(config=tmp_config)
        d1.stop_requested = True
        assert d2.stop_requested is False

    def test_signal_handling(self, tmp_config):
        """Verify signal handler terminates the process."""
        daemon = ProfilerDaemon(config=tmp_config)
        # Mock a running process
        mock_proc = MagicMock()
        daemon._process = mock_proc
        daemon._handle_signal(15, None)
        assert daemon.stop_requested is True
        mock_proc.terminate.assert_called_once()

    def test_signal_handling_no_proc(self, tmp_config):
        """Verify signal handler works without a running process."""
        daemon = ProfilerDaemon(config=tmp_config)
        daemon._handle_signal(15, None)
        assert daemon.stop_requested is True

    def test_sleep_interruptible(self, tmp_config):
        """Verify sleep returns immediately when stop is requested."""
        daemon = ProfilerDaemon(config=tmp_config)
        daemon.stop_requested = True
        start = time.time()
        daemon._sleep_interruptible(10)
        elapsed = time.time() - start
        assert elapsed < 1.0

    def test_daemon_once_mode(self, tmp_config, perf_available):
        """Verify real perf collection in once mode (1 second slice)."""
        # Use 1-second slice for fast testing
        tmp_config.slice_duration = 1
        daemon = ProfilerDaemon(config=tmp_config)
        result = daemon.collect_single_slice()
        assert result is not None
        assert result.exists()

    def test_collect_single_slice_low_disk(self, tmp_config, monkeypatch):
        """Verify collection returns None when disk space is low."""
        daemon = ProfilerDaemon(config=tmp_config)
        # Mock disk check to fail
        monkeypatch.setattr(
            "src.collector.rotator.FileRotator.check_disk_space",
            staticmethod(lambda *args, **kwargs: False),
        )
        result = daemon.collect_single_slice()
        assert result is None
