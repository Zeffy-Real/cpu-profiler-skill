"""Configuration management for CPU Profiler."""
import os
from dataclasses import dataclass


@dataclass
class Config:
    """Configuration loaded from environment variables."""
    sample_freq: int = int(os.getenv("PROFILER_SAMPLE_FREQ", "99"))
    slice_duration: int = int(os.getenv("PROFILER_SLICE_DURATION", "30"))
    retention_hours: int = int(os.getenv("PROFILER_RETENTION_HOURS", "2"))
    data_dir: str = os.getenv("PROFILER_DATA_DIR", "/var/lib/cpu-profiler")
    api_host: str = os.getenv("PROFILER_API_HOST", "0.0.0.0")
    api_port: int = int(os.getenv("PROFILER_API_PORT", "8765"))
