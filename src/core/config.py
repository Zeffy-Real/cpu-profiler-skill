"""Configuration module for CPU Profiler.

Provides a Config dataclass with environment-variable loading and
derived properties for data paths and retention timing.
"""

import os
from dataclasses import dataclass
from pathlib import Path
from datetime import timedelta


@dataclass
class Config:
    """Runtime configuration for the CPU profiler.

    Fields can be loaded from environment variables via ``Config.from_env()``.
    """

    sample_freq: int = 99
    slice_duration: int = 30          # seconds per perf recording
    retention_hours: int = 2          # how long to keep old slices
    data_dir: str = "/var/lib/cpu-profiler"
    api_host: str = "0.0.0.0"
    api_port: int = 8765

    # ------------------------------------------------------------------
    # Derived properties
    # ------------------------------------------------------------------

    @property
    def data_path(self) -> Path:
        """Return the data directory as a Path object."""
        return Path(self.data_dir)

    @property
    def index_file(self) -> Path:
        """Return the path to the index.json file."""
        return self.data_path / "index.json"

    @property
    def retention_seconds(self) -> int:
        """Return retention period in seconds."""
        return int(timedelta(hours=self.retention_hours).total_seconds())

    # ------------------------------------------------------------------
    # Directory management
    # ------------------------------------------------------------------

    def ensure_data_dir(self) -> None:
        """Create the data directory if it does not exist.

        Raises PermissionError if the directory cannot be created.
        """
        self.data_path.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Environment loading
    # ------------------------------------------------------------------

    @classmethod
    def from_env(cls) -> "Config":
        """Create a Config instance from environment variables.

        Environment variable names are the uppercased field names:
        SAMPLE_FREQ, SLICE_DURATION, RETENTION_HOURS, DATA_DIR,
        API_HOST, API_PORT.
        """
        def _get_int(name: str, default: int) -> int:
            val = os.environ.get(name)
            return int(val) if val is not None else default

        def _get_str(name: str, default: str) -> str:
            return os.environ.get(name, default)

        return cls(
            sample_freq=_get_int("SAMPLE_FREQ", 99),
            slice_duration=_get_int("SLICE_DURATION", 30),
            retention_hours=_get_int("RETENTION_HOURS", 2),
            data_dir=_get_str("DATA_DIR", "/var/lib/cpu-profiler"),
            api_host=_get_str("API_HOST", "0.0.0.0"),
            api_port=_get_int("API_PORT", 8765),
        )
