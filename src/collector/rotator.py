"""File rotation and index management for perf data slices.

All methods are static so the class acts as a namespace — no instance
state is needed, which simplifies testing and usage from both the
daemon and the API server.
"""

import json
import os
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional


class FileRotator:
    """Manage perf data file naming, rotation, cleanup, and indexing."""

    # ------------------------------------------------------------------
    # Filename helpers
    # ------------------------------------------------------------------

    @staticmethod
    def get_slice_filename(ts: Optional[datetime] = None) -> str:
        """Generate a perf data filename: perf.data.YYYYMMDD_HHMMSS.

        Args:
            ts: Timestamp for the slice. Defaults to ``datetime.now()``.

        Returns:
            Filename string, e.g. ``perf.data.20260724_153045``.
        """
        if ts is None:
            ts = datetime.now()
        return f"perf.data.{ts.strftime('%Y%m%d_%H%M%S')}"

    @staticmethod
    def parse_slice_timestamp(filename: str) -> Optional[datetime]:
        """Parse a perf data filename back to a datetime.

        Args:
            filename: Filename like ``perf.data.20260724_153045``.

        Returns:
            datetime object, or ``None`` if the filename is invalid.
        """
        if not filename or not filename.startswith("perf.data."):
            return None
        ts_str = filename.replace("perf.data.", "")
        try:
            return datetime.strptime(ts_str, "%Y%m%d_%H%M%S")
        except (ValueError, TypeError):
            return None

    # ------------------------------------------------------------------
    # Cleanup / retention
    # ------------------------------------------------------------------

    @staticmethod
    def cleanup_expired(data_dir: str, retention_hours: int) -> List[str]:
        """Delete perf data files older than the retention period.

        Args:
            data_dir: Directory containing perf data files.
            retention_hours: Files older than this many hours are deleted.

        Returns:
            List of deleted file paths.
        """
        data_path = Path(data_dir)
        if not data_path.exists():
            return []

        cutoff = datetime.now() - timedelta(hours=retention_hours)
        deleted: List[str] = []

        for entry in data_path.iterdir():
            if not entry.name.startswith("perf.data."):
                continue
            # Use the timestamp embedded in the filename, not file mtime,
            # because mtime can be changed by copy operations.
            ts = FileRotator.parse_slice_timestamp(entry.name)
            if ts is None:
                continue
            if ts < cutoff:
                try:
                    entry.unlink()
                    deleted.append(str(entry))
                except OSError:
                    pass

        return deleted

    # ------------------------------------------------------------------
    # Disk usage
    # ------------------------------------------------------------------

    @staticmethod
    def get_disk_usage(data_dir: str) -> int:
        """Return total size in bytes of all perf.data.* files in data_dir.

        Non-perf files (index.json, etc.) are ignored.
        """
        data_path = Path(data_dir)
        if not data_path.exists():
            return 0

        total = 0
        for entry in data_path.iterdir():
            if entry.name.startswith("perf.data."):
                try:
                    total += entry.stat().st_size
                except OSError:
                    pass
        return total

    @staticmethod
    def check_disk_space(data_dir: str, min_space_gb: float = 1.0) -> bool:
        """Check if the filesystem has at least ``min_space_gb`` free.

        Args:
            data_dir: Directory on the filesystem to check.
            min_space_gb: Minimum free space in GB.

        Returns:
            True if enough space is available, False otherwise.
        """
        try:
            usage = shutil.disk_usage(data_dir)
            free_gb = usage.free / (1024 ** 3)
            return free_gb >= min_space_gb
        except OSError:
            return False

    # ------------------------------------------------------------------
    # Index management (atomic read / write)
    # ------------------------------------------------------------------

    @staticmethod
    def read_index(data_dir: str) -> Dict:
        """Read the index.json file.

        Returns ``{"slices": [], "version": 1}`` if the file is missing
        or corrupted.
        """
        index_path = Path(data_dir) / "index.json"
        if not index_path.exists():
            return {"slices": [], "version": 1}

        try:
            with open(index_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return {"slices": [], "version": 1}
            if "slices" not in data:
                data["slices"] = []
            if "version" not in data:
                data["version"] = 1
            return data
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            return {"slices": [], "version": 1}

    @staticmethod
    def write_index(data_dir: str, data: Dict) -> None:
        """Write the index.json file atomically (tmp + rename).

        This prevents index corruption if the process is killed mid-write.
        """
        data_path = Path(data_dir)
        data_path.mkdir(parents=True, exist_ok=True)

        index_path = data_path / "index.json"
        tmp_path = data_path / "index.json.tmp"

        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

        # Atomic on POSIX (rename), best-effort on Windows
        os.replace(str(tmp_path), str(index_path))

    @staticmethod
    def add_slice_to_index(
        data_dir: str,
        timestamp: datetime,
        file_path: str,
        duration: int,
        size_bytes: int,
        status: str,
    ) -> Dict:
        """Add or update a slice entry in the index.

        If a slice with the same timestamp already exists, it is updated
        rather than duplicated.

        Returns the updated index dict.
        """
        index = FileRotator.read_index(data_dir)
        ts_str = timestamp.strftime("%Y%m%d_%H%M%S")

        new_entry = {
            "timestamp": ts_str,
            "file": file_path,
            "duration": duration,
            "size_bytes": size_bytes,
            "status": status,
        }

        # Check for existing entry with same timestamp (dedup)
        found = False
        for i, slice_entry in enumerate(index["slices"]):
            if slice_entry.get("timestamp") == ts_str:
                index["slices"][i] = new_entry
                found = True
                break

        if not found:
            index["slices"].append(new_entry)

        # Keep slices sorted by timestamp
        index["slices"].sort(key=lambda s: s.get("timestamp", ""))

        FileRotator.write_index(data_dir, index)
        return index
