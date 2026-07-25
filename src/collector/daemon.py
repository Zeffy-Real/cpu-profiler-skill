"""Profiler daemon — runs perf in a loop to collect continuous CPU samples.

Each iteration:
1. Check disk space
2. Run ``perf record`` for ``slice_duration`` seconds
3. Update the index with the new slice metadata
4. Clean up expired slices
5. Sleep and repeat

Stop flag is an *instance* attribute (not module-global) to ensure
test isolation between ProfilerDaemon instances.
"""

import argparse
import logging
import signal
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from src.core.config import Config
from src.collector.rotator import FileRotator

logger = logging.getLogger("cpu-profiler.daemon")


class ProfilerDaemon:
    """Continuously collect perf CPU samples in time-sliced files."""

    def __init__(self, config: Optional[Config] = None):
        """Initialize the daemon.

        Args:
            config: Configuration. If None, loaded from environment.
        """
        self.config = config if config is not None else Config.from_env()
        # Instance-level stop flag (NOT module global) for test isolation
        self.stop_requested: bool = False
        self._process: Optional[subprocess.Popen] = None

    # ------------------------------------------------------------------
    # perf command building
    # ------------------------------------------------------------------

    def build_perf_command(self, output_file: str) -> List[str]:
        """Build the perf record command for a single slice.

        Returns:
            List of command arguments suitable for subprocess.
        """
        return [
            "perf", "record",
            "-F", str(self.config.sample_freq),   # sample frequency
            "-a",                                 # all CPUs
            "-g",                                 # call graphs
            "-o", output_file,                    # output file
            "--",                                 # separator
            "sleep", str(self.config.slice_duration),
        ]

    # ------------------------------------------------------------------
    # Single slice collection
    # ------------------------------------------------------------------

    def run_perf_record(self, output_path: str, max_retries: int = 3, retry_delay: float = 5.0) -> bool:
        """Run a single perf record command with retries.

        Args:
            output_path: Path for the perf.data output file.
            max_retries: Maximum number of retry attempts.
            retry_delay: Seconds to wait between retries.

        Returns:
            True if perf completed successfully, False otherwise.
        """
        cmd = self.build_perf_command(output_path)

        for attempt in range(1, max_retries + 1):
            if self.stop_requested:
                return False
            try:
                logger.info("perf record attempt %d/%d: %s", attempt, max_retries, " ".join(cmd))
                result = subprocess.run(cmd, capture_output=True, timeout=self.config.slice_duration + 30)
                if result.returncode == 0:
                    return True
                logger.warning("perf record failed (attempt %d): %s",
                               attempt, result.stderr.decode(errors="replace"))
            except subprocess.TimeoutExpired:
                logger.warning("perf record timed out (attempt %d)", attempt)
            except Exception as exc:
                logger.warning("perf record error (attempt %d): %s", attempt, exc)

            if attempt < max_retries:
                self._sleep_interruptible(retry_delay)

        return False

    def collect_single_slice(self) -> Optional[Path]:
        """Collect one perf data slice.

        Steps:
        1. Check disk space (return None if insufficient)
        2. Run perf record
        3. Update the index

        Returns:
            Path to the collected perf.data file, or None on failure.
        """
        # 1. Disk space check
        if not FileRotator.check_disk_space(self.config.data_dir, min_space_gb=1.0):
            logger.error("Insufficient disk space, skipping slice collection")
            return None

        # 2. Generate filename and run perf
        ts = datetime.now()
        filename = FileRotator.get_slice_filename(ts)
        output_path = str(Path(self.config.data_dir) / filename)

        success = self.run_perf_record(output_path)

        # 3. Get file size
        file_path = Path(output_path)
        size_bytes = file_path.stat().st_size if file_path.exists() else 0
        status = "success" if success else "failed"

        # 4. Update index (always update, even on failure — per convention,
        #    failed slices don't create physical files but are indexed)
        FileRotator.add_slice_to_index(
            data_dir=self.config.data_dir,
            timestamp=ts,
            file_path=output_path,
            duration=self.config.slice_duration,
            size_bytes=size_bytes,
            status=status,
        )

        if success and file_path.exists():
            logger.info("Collected slice: %s (%d bytes)", filename, size_bytes)
            return file_path
        else:
            logger.warning("Slice collection failed: %s", filename)
            # Remove failed file if it exists but is empty/corrupt
            if file_path.exists() and size_bytes == 0:
                try:
                    file_path.unlink()
                except OSError:
                    pass
            return None

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(self, once: bool = False, dry_run: bool = False) -> None:
        """Run the collection loop.

        Args:
            once: If True, collect a single slice and exit.
            dry_run: If True, print the perf command without executing.
        """
        if dry_run:
            ts = datetime.now()
            filename = FileRotator.get_slice_filename(ts)
            output_path = str(Path(self.config.data_dir) / filename)
            cmd = self.build_perf_command(output_path)
            print(f"Dry run — would execute: {' '.join(cmd)}")
            return

        try:
            self.config.ensure_data_dir()
        except PermissionError:
            logger.warning("Cannot create data dir %s — using existing or default", self.config.data_dir)

        if once:
            self.collect_single_slice()
            return

        logger.info("Starting continuous profiling (slice=%ds, freq=%dHz)",
                     self.config.slice_duration, self.config.sample_freq)

        while not self.stop_requested:
            self.collect_single_slice()

            # Cleanup expired slices after each collection
            deleted = FileRotator.cleanup_expired(
                self.config.data_dir, self.config.retention_hours
            )
            if deleted:
                logger.info("Cleaned up %d expired slice(s)", len(deleted))

            # Brief sleep between slices (interruptible)
            self._sleep_interruptible(1)

        logger.info("Profiler daemon stopped")

    # ------------------------------------------------------------------
    # Signal handling
    # ------------------------------------------------------------------

    def _handle_signal(self, signum, frame) -> None:
        """Signal handler — set stop flag and terminate any running process."""
        logger.info("Received signal %d, shutting down...", signum)
        self.stop_requested = True
        if self._process is not None:
            try:
                self._process.terminate()
            except OSError:
                pass

    def start(self, once: bool = False, dry_run: bool = False) -> None:
        """Register signal handlers and start the daemon loop."""
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)
        self.run(once=once, dry_run=dry_run)

    # ------------------------------------------------------------------
    # Interruptible sleep
    # ------------------------------------------------------------------

    def _sleep_interruptible(self, seconds: float) -> None:
        """Sleep for the given duration, checking stop flag each second."""
        end_time = time.time() + seconds
        while time.time() < end_time:
            if self.stop_requested:
                return
            time.sleep(min(1.0, end_time - time.time()))


# ----------------------------------------------------------------------
# CLI entry point
# ----------------------------------------------------------------------

def main():
    """CLI entry point for the profiler daemon."""
    parser = argparse.ArgumentParser(description="Continuous CPU Profiler Daemon")
    parser.add_argument("--once", action="store_true", help="Collect a single slice and exit")
    parser.add_argument("--dry-run", action="store_true", help="Print command without executing")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        help="Logging level")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    daemon = ProfilerDaemon()
    daemon.start(once=args.once, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
