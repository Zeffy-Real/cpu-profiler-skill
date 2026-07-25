"""Flame graph generation pipeline.

Pipeline: perf.data → perf script → stackcollapse-perf.pl → flamegraph.pl → SVG

Uses TemporaryDirectory for intermediate files so they are automatically
cleaned up after generation.
"""

import os
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

from src.collector.rotator import FileRotator


class FlameGraphError(Exception):
    """Raised when flame graph generation fails."""
    pass


class FlameGraphGenerator:
    """Generate flame graph SVGs from perf data files."""

    def __init__(
        self,
        width: int = 1200,
        height: int = 16,
        title: str = "CPU Flame Graph",
    ):
        """Initialize the generator with flame graph visual parameters.

        Args:
            width: Width of the SVG in pixels.
            height: Height of each stack frame in pixels.
            title: Title displayed on the flame graph.
        """
        self.width = width
        self.height = height
        self.title = title

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def generate(self, perf_data_path: str, output_dir: str) -> str:
        """Generate a flame graph SVG from a single perf.data file.

        Args:
            perf_data_path: Path to the perf.data file.
            output_dir: Directory to write the SVG output.

        Returns:
            Path to the generated SVG file.

        Raises:
            FlameGraphError: If the perf data file is missing or
                             generation fails.
        """
        perf_path = Path(perf_data_path)
        if not perf_path.exists():
            raise FlameGraphError(f"Perf data file not found: {perf_data_path}")

        self._check_tools()

        os.makedirs(output_dir, exist_ok=True)
        output_svg = os.path.join(output_dir, "flamegraph.svg")

        with tempfile.TemporaryDirectory(prefix="flamegraph-") as tmpdir:
            script_path = os.path.join(tmpdir, "perf.script")
            folded_path = os.path.join(tmpdir, "folded.txt")

            self._run_perf_script(perf_data_path, script_path)
            self._run_stackcollapse(script_path, folded_path)
            self._run_flamegraph(folded_path, output_svg)

        if not os.path.exists(output_svg):
            raise FlameGraphError("Flame graph SVG was not generated")

        return output_svg

    def generate_from_time_range(
        self,
        data_dir: str,
        start: datetime,
        end: datetime,
        output_dir: str,
    ) -> str:
        """Generate a flame graph from all slices in a time range.

        Multiple perf.data files are processed and their folded stacks
        are concatenated before generating the final SVG.

        Args:
            data_dir: Directory containing perf data slices.
            start: Start datetime (inclusive).
            end: End datetime (inclusive).
            output_dir: Directory to write the SVG output.

        Returns:
            Path to the generated SVG file.

        Raises:
            FlameGraphError: If no slices are found or generation fails.
        """
        slices = self._find_slices_in_range(data_dir, start, end)
        if not slices:
            raise FlameGraphError(
                f"No perf data slices found in range {start} to {end}"
            )

        self._check_tools()
        os.makedirs(output_dir, exist_ok=True)
        output_svg = os.path.join(output_dir, "flamegraph.svg")

        with tempfile.TemporaryDirectory(prefix="flamegraph-range-") as tmpdir:
            folded_parts: List[str] = []

            for file_path, _ts in slices:
                script_path = os.path.join(tmpdir, f"perf_{os.path.basename(file_path)}.script")
                folded_path = os.path.join(tmpdir, f"folded_{os.path.basename(file_path)}.txt")

                self._run_perf_script(file_path, script_path)
                self._run_stackcollapse(script_path, folded_path)

                with open(folded_path, "r") as f:
                    folded_parts.append(f.read())

            # Concatenate all folded stacks
            combined_folded = os.path.join(tmpdir, "combined_folded.txt")
            with open(combined_folded, "w") as f:
                f.write("".join(folded_parts))

            self._run_flamegraph(combined_folded, output_svg)

        if not os.path.exists(output_svg):
            raise FlameGraphError("Flame graph SVG was not generated")

        return output_svg

    # ------------------------------------------------------------------
    # Tool checking
    # ------------------------------------------------------------------

    def _check_tools(self) -> None:
        """Verify that perf, stackcollapse-perf.pl, and flamegraph.pl exist.

        Raises FlameGraphError if any tool is missing.
        """
        missing: List[str] = []

        for tool in ["perf", "stackcollapse-perf.pl", "flamegraph.pl"]:
            found = shutil.which(tool)
            if found is None:
                # Check common installation paths
                common_paths = [f"/usr/local/bin/{tool}", f"/usr/bin/{tool}"]
                if not any(os.path.exists(p) for p in common_paths):
                    missing.append(tool)

        if missing:
            raise FlameGraphError(f"Missing required tools: {', '.join(missing)}")

    # ------------------------------------------------------------------
    # Pipeline stages
    # ------------------------------------------------------------------

    def _run_perf_script(self, perf_data_path: str, output_path: str) -> None:
        """Run ``perf script`` to extract stack traces from perf.data."""
        cmd = ["perf", "script", "-i", perf_data_path]
        with open(output_path, "w") as f:
            result = subprocess.run(cmd, stdout=f, stderr=subprocess.PIPE)
        if result.returncode != 0:
            raise FlameGraphError(
                f"perf script failed: {result.stderr.decode(errors='replace')}"
            )

    def _run_stackcollapse(self, script_path: str, output_path: str) -> None:
        """Run ``stackcollapse-perf.pl`` to fold stack traces."""
        cmd = ["stackcollapse-perf.pl"]
        with open(script_path, "r") as stdin, open(output_path, "w") as stdout:
            result = subprocess.run(cmd, stdin=stdin, stdout=stdout, stderr=subprocess.PIPE)
        if result.returncode != 0:
            raise FlameGraphError(
                f"stackcollapse-perf.pl failed: {result.stderr.decode(errors='replace')}"
            )

    def _run_flamegraph(self, folded_path: str, output_path: str) -> None:
        """Run ``flamegraph.pl`` to generate the final SVG."""
        cmd = [
            "flamegraph.pl",
            "--width", str(self.width),
            "--height", str(self.height),
            "--title", self.title,
        ]
        with open(folded_path, "r") as stdin, open(output_path, "w") as stdout:
            result = subprocess.run(cmd, stdin=stdin, stdout=stdout, stderr=subprocess.PIPE)
        if result.returncode != 0:
            raise FlameGraphError(
                f"flamegraph.pl failed: {result.stderr.decode(errors='replace')}"
            )

    # ------------------------------------------------------------------
    # Slice lookup
    # ------------------------------------------------------------------

    def _find_slices_in_range(
        self,
        data_dir: str,
        start: datetime,
        end: datetime,
    ) -> List[Tuple[str, datetime]]:
        """Find all perf data slices within the given time range.

        Uses the index.json first, then falls back to directory scanning.
        Only ``success`` status slices with existing physical files are
        returned.

        Args:
            data_dir: Directory containing perf data and index.json.
            start: Start datetime (inclusive).
            end: End datetime (inclusive).

        Returns:
            List of (file_path, timestamp) tuples sorted by timestamp.
        """
        results: List[Tuple[str, datetime]] = []
        seen_files: set = set()

        # 1. Try index-based lookup
        index = FileRotator.read_index(data_dir)
        for entry in index.get("slices", []):
            ts_str = entry.get("timestamp", "")
            ts = FileRotator.parse_slice_timestamp(f"perf.data.{ts_str}")
            if ts is None:
                continue

            if start <= ts <= end:
                # Skip failed slices
                if entry.get("status") == "failed":
                    continue
                file_path = entry.get("file", "")
                # Only include if the physical file exists
                if file_path and os.path.exists(file_path) and file_path not in seen_files:
                    results.append((file_path, ts))
                    seen_files.add(file_path)

        # 2. Fallback: directory scan (in case index is incomplete)
        if not results:
            data_path = Path(data_dir)
            if data_path.exists():
                for entry in data_path.iterdir():
                    if not entry.name.startswith("perf.data."):
                        continue
                    ts = FileRotator.parse_slice_timestamp(entry.name)
                    if ts is None:
                        continue
                    if start <= ts <= end:
                        file_path = str(entry)
                        if file_path not in seen_files:
                            results.append((file_path, ts))
                            seen_files.add(file_path)

        # Sort by timestamp
        results.sort(key=lambda x: x[1])
        return results
