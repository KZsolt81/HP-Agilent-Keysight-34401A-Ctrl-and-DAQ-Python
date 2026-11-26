"""Measurement and output-log persistence (port of ``Save_MeasurementData_to_files``).

Directory layout is identical to the original::

    <base>/Log Data (HP34401A)/
        <tag>_Output Log.txt
        VDC/2024-05-01_COM3_VDC.txt
        ADC/...  VAC/  AAC/  2WireOhms/  4WireOhms/  FREQ/  PER/  DIODE/  CONTINUITY/

Each measurement line is ``yyyy-MM-dd h:mm:ss.fff tt,<raw reading>``. Readings are buffered
in memory and appended to disk every ``interval_s`` seconds (default 60 s, like the original)
so a slow disk never stalls acquisition. The daily file name uses the UTC date, as before.
"""

from __future__ import annotations

import os
import threading
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .engine import LogLevel, Sample
from .formatting import format_log_time, format_timestamp
from .scpi import LOG_FOLDERS

LOG_DIR_NAME = "Log Data (HP34401A)"
SAVE_INTERVAL_CHOICES = (5, 10, 20, 40, 60, 240, 480, 600)


def prepare_log_directory(base: "str | os.PathLike") -> Path:
    """Create ``<base>/Log Data (HP34401A)`` and all per-function sub-folders."""
    root = Path(base)
    if root.name != LOG_DIR_NAME:
        root = root / LOG_DIR_NAME
    root.mkdir(parents=True, exist_ok=True)
    for folder in LOG_FOLDERS:
        (root / folder).mkdir(exist_ok=True)
    return root


def default_log_base() -> Path:
    return Path.home() / "Documents" if (Path.home() / "Documents").is_dir() else Path.home()


class DataLogger:
    def __init__(self, root: Path, tag: str, interval_s: float = 60.0,
                 log: Optional[Callable[[str, LogLevel], None]] = None):
        self.root = Path(root)
        self.tag = tag
        self.interval_s = float(interval_s)
        self.enabled = True
        self._log = log
        self._buf: Dict[str, List[str]] = defaultdict(list)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._kick = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # Sample listener (called from the I/O thread)
    def __call__(self, sample: Sample) -> None:
        if not self.enabled:
            return
        line = f"{format_timestamp(sample.timestamp)},{sample.raw}"
        with self._lock:
            self._buf[sample.function.folder].append(line)

    def set_interval(self, seconds: float) -> None:
        self.interval_s = float(seconds)
        self._kick.set()

    def file_for(self, folder: str, when: Optional[datetime] = None) -> Path:
        when = when or datetime.now(timezone.utc)
        return self.root / folder / f"{when:%Y-%m-%d}_{self.tag}_{folder}.txt"

    def flush(self) -> int:
        with self._lock:
            pending, self._buf = self._buf, defaultdict(list)
        if not self.enabled:
            return 0
        written = 0
        for folder, lines in pending.items():
            if not lines:
                continue
            path = self.file_for(folder)
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with open(path, "a", encoding="utf-8", newline="\n") as fh:
                    fh.write("\n".join(lines) + "\n")
                written += len(lines)
            except OSError as e:
                if self._log:
                    self._log(f"Cannot save {folder} measurements to text file: {e}", LogLevel.ERROR)
        return written

    def start(self) -> None:
        if self._thread:
            return
        self._thread = threading.Thread(target=self._run, name="34401A-datalog", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            self._kick.wait(self.interval_s)
            self._kick.clear()
            self.flush()

    def stop(self) -> None:
        self._stop.set()
        self._kick.set()
        if self._thread:
            self._thread.join(5)
            self._thread = None
        self.flush()


class OutputLogFile:
    """Appends output-log lines to ``<root>/<tag>_Output Log.txt``."""

    def __init__(self, root: Path, tag: str):
        self.path = Path(root) / f"{tag}_Output Log.txt"
        self.enabled = True
        self._lock = threading.Lock()

    def write(self, message: str, level: LogLevel, when: Optional[datetime] = None) -> bool:
        if not self.enabled:
            return True
        line = f"[{format_log_time(when or datetime.now())}] {level.tag} {message}".strip()
        try:
            with self._lock, open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            return True
        except OSError:
            self.enabled = False
            return False
