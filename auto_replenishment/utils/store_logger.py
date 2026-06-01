"""
auto_replenishment/utils/store_logger.py

Per-store file-based logger for the Auto Replenishment forecast process.

Design:
  - One log file per store per run:
      {frappe_log_dir}/auto_replenishment/{run_date}/{log_name}/{safe_warehouse_name}.log
  - Uses Python's standard logging module with a FileHandler
  - Writes structured, human-readable lines with timestamps, level, and context
  - No database writes — zero performance impact on the forecast worker
  - The Frappe web API reads these files on demand for the UI log viewer

Usage inside a forecast worker:
    from auto_replenishment.utils.store_logger import StoreLogger

    with StoreLogger(log_name="AR-LOG-2026-02-001", warehouse="Store A - WH") as log:
        log.info("Starting forecast calculation")
        log.step("Fetching eligible items")
        log.metric("eligible_items", 8432)
        log.warning("Item ITEM-001 has no sales history")
        log.error("Database query failed", exc_info=True)
        log.success("Forecast complete — 312 items require replenishment")
"""

import os
import re
import sys
import logging
import traceback
from datetime import datetime
from typing import Optional

import frappe


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LOG_DIR_NAME = "auto_replenishment"
LOG_FORMAT = "%(asctime)s  %(levelname)-8s  %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# Custom level between INFO and WARNING — used for section headers / steps
STEP_LEVEL = 25
logging.addLevelName(STEP_LEVEL, "STEP")

# Custom level above INFO — used for metrics / numeric results
METRIC_LEVEL = 22
logging.addLevelName(METRIC_LEVEL, "METRIC")

# Custom level above INFO — used for success milestones
SUCCESS_LEVEL = 26
logging.addLevelName(SUCCESS_LEVEL, "SUCCESS")


# ---------------------------------------------------------------------------
# StoreLogger — context manager
# ---------------------------------------------------------------------------

class StoreLogger:
    """
    Context-manager logger that writes detailed structured logs for one
    store's forecast run into a dedicated file on the server.

    All public methods are safe to call even if the logger failed to
    initialise — errors are swallowed so the forecast worker is never
    interrupted by a logging failure.
    """

    def __init__(self, log_name: str, warehouse: str, run_date: Optional[str] = None):
        self.log_name = log_name
        self.warehouse = warehouse
        self.run_date = run_date or datetime.today().strftime("%Y-%m-%d")
        self.log_file_path: Optional[str] = None
        self._logger: Optional[logging.Logger] = None
        self._handler: Optional[logging.FileHandler] = None
        self._start_time = datetime.now()

    # ── Context manager protocol ───────────────────────────────────────────

    def __enter__(self) -> "StoreLogger":
        self._setup()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            # Uncaught exception inside the with-block — log it
            self.error(
                f"Uncaught exception escaped context: {exc_val}",
                exc_info=(exc_type, exc_val, exc_tb),
            )
        self._teardown()
        return False  # Do not suppress exceptions

    # ── Logging methods ────────────────────────────────────────────────────

    def debug(self, msg: str):
        self._log(logging.DEBUG, msg)

    def info(self, msg: str):
        self._log(logging.INFO, msg)

    def step(self, msg: str):
        """Section header — marks the start of a major processing phase."""
        separator = "─" * 60
        self._log(STEP_LEVEL, separator)
        self._log(STEP_LEVEL, f"▶  {msg}")
        self._log(STEP_LEVEL, separator)

    def metric(self, label: str, value, unit: str = ""):
        """Structured numeric result — easy to grep / parse."""
        unit_str = f" {unit}" if unit else ""
        self._log(METRIC_LEVEL, f"[METRIC] {label} = {value}{unit_str}")

    def success(self, msg: str):
        """Milestone completion — stands out visually."""
        self._log(SUCCESS_LEVEL, f"✓  {msg}")

    def warning(self, msg: str):
        self._log(logging.WARNING, msg)

    def error(self, msg: str, exc_info=None):
        """
        Log an error. If exc_info is provided (True or a tuple),
        the full traceback is written to the file.
        """
        if not self._logger:
            return
        try:
            if exc_info is True:
                tb = traceback.format_exc()
                self._log(logging.ERROR, msg)
                for line in tb.splitlines():
                    self._log(logging.ERROR, f"  {line}")
            elif exc_info and isinstance(exc_info, tuple):
                tb_lines = traceback.format_exception(*exc_info)
                self._log(logging.ERROR, msg)
                for chunk in tb_lines:
                    for line in chunk.splitlines():
                        self._log(logging.ERROR, f"  {line}")
            else:
                self._log(logging.ERROR, msg)
        except Exception:
            pass

    def separator(self, char: str = "═", width: int = 70):
        self._log(logging.INFO, char * width)

    def banner(self, title: str):
        """Write a prominent header block."""
        width = 70
        self._log(logging.INFO, "═" * width)
        self._log(logging.INFO, f"  {title}")
        self._log(logging.INFO, "═" * width)

    # ── Property: file path (used by scheduler to store in DB row) ────────

    @property
    def file_path(self) -> Optional[str]:
        return self.log_file_path

    # ── Private setup / teardown ───────────────────────────────────────────

    def _setup(self):
        """Initialise the file handler and write the opening banner."""
        try:
            self.log_file_path = _build_log_path(
                self.log_name, self.warehouse, self.run_date
            )
            os.makedirs(os.path.dirname(self.log_file_path), exist_ok=True)

            logger_name = f"ar.store.{self.log_name}.{_safe_name(self.warehouse)}"
            self._logger = logging.getLogger(logger_name)
            self._logger.setLevel(logging.DEBUG)
            self._logger.propagate = False  # Don't bleed into Frappe's root logger

            # Remove stale handlers (in case of worker reuse)
            self._logger.handlers.clear()

            self._handler = logging.FileHandler(
                self.log_file_path, mode="a", encoding="utf-8"
            )
            self._handler.setLevel(logging.DEBUG)
            formatter = logging.Formatter(fmt=LOG_FORMAT, datefmt=LOG_DATE_FORMAT)
            self._handler.setFormatter(formatter)
            self._logger.addHandler(self._handler)

            # Opening banner
            self.banner(
                f"AUTO REPLENISHMENT — STORE FORECAST LOG"
            )
            self.info(f"  Log Name    : {self.log_name}")
            self.info(f"  Warehouse   : {self.warehouse}")
            self.info(f"  Run Date    : {self.run_date}")
            self.info(f"  Started At  : {self._start_time.strftime('%Y-%m-%d %H:%M:%S')}")
            self.info(f"  Log File    : {self.log_file_path}")
            self.separator()

        except Exception as e:
            # Logging must never crash the worker
            sys.stderr.write(
                f"[StoreLogger] Failed to initialise log file for {self.warehouse}: {e}\n"
            )

    def _teardown(self):
        """Write the closing summary and flush/close the file handler."""
        try:
            elapsed = (datetime.now() - self._start_time).total_seconds()
            self.separator()
            self.info(f"  Finished At  : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            self.info(f"  Elapsed Time : {elapsed:.1f} seconds")
            self.separator("═")
        except Exception:
            pass

        try:
            if self._handler:
                self._handler.flush()
                self._handler.close()
                if self._logger:
                    self._logger.removeHandler(self._handler)
        except Exception:
            pass

    def _log(self, level: int, msg: str):
        if self._logger:
            try:
                self._logger.log(level, msg)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def get_log_dir() -> str:
    """Return the base directory for all Auto Replenishment log files."""
    bench_path = frappe.utils.get_bench_path()
    return os.path.join(bench_path, "logs", LOG_DIR_NAME)


def _build_log_path(log_name: str, warehouse: str, run_date: str) -> str:
    """
    Build an absolute path for the store log file.

    Structure:
        {bench}/logs/auto_replenishment/{run_date}/{log_name}/{warehouse}.log

    Example:
        /home/frappe/frappe-bench/logs/auto_replenishment/
            2026-02-21/AR-LOG-2026-02-001/Store-A---WH.log
    """
    base = get_log_dir()
    safe_wh = _safe_name(warehouse)
    return os.path.join(base, run_date, log_name, f"{safe_wh}.log")


def _safe_name(name: str) -> str:
    """Convert a warehouse name to a filesystem-safe filename."""
    safe = re.sub(r"[^\w\-]", "_", name)
    safe = re.sub(r"_+", "_", safe).strip("_")
    return safe[:100]  # Limit length


def list_log_files(log_name: str, run_date: str) -> list:
    """
    Return a list of {warehouse, path, size_bytes, modified_at} for all
    log files belonging to a given run. Used by admin utilities.
    """
    import glob
    base = get_log_dir()
    pattern = os.path.join(base, run_date, log_name, "*.log")
    results = []
    for path in sorted(glob.glob(pattern)):
        stat = os.stat(path)
        results.append({
            "path": path,
            "filename": os.path.basename(path),
            "size_bytes": stat.st_size,
            "modified_at": datetime.fromtimestamp(stat.st_mtime).strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
        })
    return results
