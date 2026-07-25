"""Pytest configuration and fixtures for CPU Profiler tests.

CRITICAL: The os.environ["DATA_DIR"] override at the top must execute
before any import of src modules, so that Config.from_env() picks up
the temporary directory instead of /var/lib/cpu-profiler.
"""

import os
import sys
import shutil
import tempfile

# ---------------------------------------------------------------------------
# Add project root to sys.path so `src` package is importable.
# ---------------------------------------------------------------------------
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# ---------------------------------------------------------------------------
# Set DATA_DIR to a temporary directory BEFORE any src imports.
# This prevents server.py's module-level ensure_data_dir() from
# trying to create /var/lib/cpu-profiler (which requires root).
# ---------------------------------------------------------------------------
_tmp_base = tempfile.mkdtemp(prefix="cpu-profiler-test-")
os.environ["DATA_DIR"] = _tmp_base
os.environ["API_HOST"] = "127.0.0.1"
os.environ["API_PORT"] = "8765"

import pytest  # noqa: E402

# Now safe to import src modules
from src.core.config import Config  # noqa: E402


@pytest.fixture
def tmp_data_dir():
    """Provide a fresh temporary data directory for each test."""
    d = tempfile.mkdtemp(prefix="cpu-profiler-test-")
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def tmp_config(tmp_data_dir):
    """Provide a Config instance pointing to a temporary directory."""
    return Config(
        sample_freq=99,
        slice_duration=30,
        retention_hours=2,
        data_dir=tmp_data_dir,
        api_host="127.0.0.1",
        api_port=8765,
    )


@pytest.fixture
def perf_available():
    """Skip test if perf is not available."""
    perf_path = shutil.which("perf")
    if perf_path is None:
        # Also check common Linux path
        if os.path.exists("/usr/bin/perf") or os.path.exists("/usr/local/bin/perf"):
            return True
        pytest.skip("perf not available")
    return True


@pytest.fixture
def flamegraph_available():
    """Skip test if flamegraph.pl is not available."""
    fg_path = shutil.which("flamegraph.pl")
    if fg_path is None:
        if os.path.exists("/usr/local/bin/flamegraph.pl"):
            return True
        pytest.skip("flamegraph.pl not available")
    return True


@pytest.fixture
def stackcollapse_available():
    """Skip test if stackcollapse-perf.pl is not available."""
    sc_path = shutil.which("stackcollapse-perf.pl")
    if sc_path is None:
        if os.path.exists("/usr/local/bin/stackcollapse-perf.pl"):
            return True
        pytest.skip("stackcollapse-perf.pl not available")
    return True
