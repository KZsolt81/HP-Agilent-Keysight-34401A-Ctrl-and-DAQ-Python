"""Persistent settings.

* ``Settings.txt`` — the original's display-colour file (5 lines: measurement RGB, MIN RGB,
  MAX RGB, AVG RGB, ``TRUE``/``FALSE`` for partial-SI prefix). Still honoured if found next
  to the program or in the config directory.
* ``config.json`` — everything else (last connection parameters, log folder, UI options),
  stored in the per-user config directory.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

RGB = Tuple[int, int, int]


def config_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "hp34401a"


@dataclass
class DisplaySettings:
    measurement: RGB = (0, 192, 255)
    minimum: RGB = (0, 192, 255)
    maximum: RGB = (0, 192, 255)
    average: RGB = (0, 192, 255)
    background: RGB = (0, 0, 0)
    partial_si: bool = True


def _parse_rgb_line(line: str) -> Optional[RGB]:
    try:
        r, g, b = (int(x) for x in line.split(",")[:3])
    except ValueError:
        return None
    if all(0 <= c <= 255 for c in (r, g, b)):
        return (r, g, b)
    return None


def load_settings_txt(path: Path) -> Optional[DisplaySettings]:
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return None
    ds = DisplaySettings()
    names = ("measurement", "minimum", "maximum", "average")
    for name, line in zip(names, lines):
        rgb = _parse_rgb_line(line)
        if rgb:
            setattr(ds, name, rgb)
    if len(lines) >= 5:
        ds.partial_si = lines[4].split(",")[0].strip().upper() != "FALSE"
    return ds


def find_settings_txt() -> Optional[Path]:
    candidates = [Path.cwd() / "Settings.txt", config_dir() / "Settings.txt"]
    if getattr(sys, "frozen", False):
        candidates.insert(0, Path(sys.executable).parent / "Settings.txt")
    for c in candidates:
        if c.is_file():
            return c
    return None


@dataclass
class AppConfig:
    display: DisplaySettings = field(default_factory=DisplaySettings)
    connection: Dict[str, Any] = field(default_factory=dict)
    log_base: str = ""
    save_interval_s: int = 60
    output_log_limit: int = 20
    update_speed_s: float = 1.0
    avg_factor: int = 1000
    avg_resolution: int = 5

    @classmethod
    def load(cls) -> "AppConfig":
        cfg = cls()
        path = config_dir() / "config.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        disp = data.pop("display", None)
        for k, v in data.items():
            if hasattr(cfg, k):
                setattr(cfg, k, v)
        if isinstance(disp, dict):
            for k, v in disp.items():
                if hasattr(cfg.display, k):
                    setattr(cfg.display, k, tuple(v) if isinstance(v, list) else v)
        txt = find_settings_txt()
        if txt:
            loaded = load_settings_txt(txt)
            if loaded:
                loaded.background = cfg.display.background
                cfg.display = loaded
        return cfg

    def save(self) -> None:
        path = config_dir() / "config.json"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        except OSError:
            pass
