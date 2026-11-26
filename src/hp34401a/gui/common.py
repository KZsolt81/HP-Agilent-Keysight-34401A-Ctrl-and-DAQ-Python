"""Widgets and helpers shared by all windows."""

from __future__ import annotations

import html
import random
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence, Tuple

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QAction, QActionGroup, QColor, QFont, QTextCursor
from PySide6.QtWidgets import (QCheckBox, QColorDialog, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QMenu,
                               QPushButton, QRadioButton, QTextEdit, QVBoxLayout, QWidget)

from ..engine import LogLevel
from ..formatting import format_log_time

LEVEL_COLORS = {
    LogLevel.SUCCESS: "#1a8f1a",
    LogLevel.ERROR: "#d01818",
    LogLevel.WARNING: "#d97a00",
    LogLevel.INFO: "#1f5fd1",
    LogLevel.PLAIN: None,  # palette text colour
    LogLevel.COMMAND: "#8a2be2",
}

#: Colour presets offered by the original's menus.
COLOR_PRESETS = {
    "Green": (0, 206, 48),
    "Blue": (0, 192, 255),
    "Red": (255, 40, 40),
    "Yellow": (255, 230, 0),
    "Orange": (255, 140, 0),
    "Pink": (255, 105, 180),
    "Violet": (148, 0, 211),
    "White": (255, 255, 255),
    "Black": (0, 0, 0),
    "Gray": (128, 128, 128),
}


class LogBridge(QObject):
    """Thread-safe entry point for log messages (emit from any thread)."""

    message = Signal(str, int)

    def __call__(self, message: str, level: LogLevel = LogLevel.PLAIN) -> None:
        self.message.emit(message, int(level))


class LogView(QWidget):
    """Coloured output log with auto-scroll and auto-clear, like the original's Output Log."""

    def __init__(self, parent: Optional[QWidget] = None, limit: int = 200):
        super().__init__(parent)
        self.limit = limit
        self._count = 0
        self.on_line: Optional[Callable[[str, LogLevel, datetime], None]] = None
        self.text = QTextEdit(readOnly=True)
        self.text.setLineWrapMode(QTextEdit.WidgetWidth)
        f = QFont("Consolas")
        f.setStyleHint(QFont.Monospace)
        f.setPointSize(9)
        self.text.setFont(f)
        self.auto_scroll = QCheckBox("Auto scroll", checked=True)
        clear = QPushButton("Clear")
        clear.clicked.connect(self.clear)
        row = QHBoxLayout()
        row.addWidget(QLabel("<b>Output Log</b>"))
        row.addStretch(1)
        row.addWidget(self.auto_scroll)
        row.addWidget(clear)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addLayout(row)
        lay.addWidget(self.text)

    def clear(self) -> None:
        self.text.clear()
        self._count = 0

    def append(self, message: str, level: "LogLevel | int" = LogLevel.PLAIN) -> None:
        level = LogLevel(int(level))
        now = datetime.now()
        if self.limit and self._count >= self.limit:
            self.clear()
            self._insert(now, "Output Log has been auto cleared.", LogLevel.SUCCESS, tag="")
        self._insert(now, message, level)
        if self.on_line:
            self.on_line(message, level, now)

    def _insert(self, now: datetime, message: str, level: LogLevel, tag: Optional[str] = None) -> None:
        tag = level.tag if tag is None else tag
        color = LEVEL_COLORS.get(level)
        line = html.escape(f"[{format_log_time(now)}] {tag} {message}".replace("  ", " "))
        self.text.append(f'<span style="color:{color}">{line}</span>' if color else line)
        self._count += 1
        if self.auto_scroll.isChecked():
            self.text.moveCursor(QTextCursor.End)

    def plain_text(self) -> str:
        return self.text.toPlainText()


def rgb_css(rgb: Sequence[int]) -> str:
    return f"rgb({rgb[0]},{rgb[1]},{rgb[2]})"


def random_rgb() -> Tuple[int, int, int]:
    return (random.randint(0, 255), random.randint(0, 255), random.randint(0, 255))


class ColorButton(QPushButton):
    """Push button showing a colour; click opens a colour picker."""

    def __init__(self, rgb: Tuple[int, int, int] = (255, 0, 0), parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setFixedWidth(60)
        self.set_rgb(rgb)
        self.clicked.connect(self._pick)

    def set_rgb(self, rgb: Tuple[int, int, int]) -> None:
        self.rgb = tuple(int(c) for c in rgb)
        self.setStyleSheet(f"background-color: {rgb_css(self.rgb)}; border: 1px solid #888; min-height: 18px;")

    def _pick(self) -> None:
        c = QColorDialog.getColor(QColor(*self.rgb), self, "Choose colour")
        if c.isValid():
            self.set_rgb((c.red(), c.green(), c.blue()))


def add_choice_menu(menu: QMenu, title: str, choices: Iterable[Tuple[str, object]], current: object,
                    on_select: Callable[[object], None]) -> QMenu:
    """Add an exclusive, checkable sub-menu."""
    sub = menu.addMenu(title)
    group = QActionGroup(sub)
    group.setExclusive(True)
    for label, value in choices:
        act = QAction(label, sub, checkable=True)
        act.setChecked(value == current)
        act.triggered.connect(lambda _=False, v=value: on_select(v))
        group.addAction(act)
        sub.addAction(act)
    return sub


def add_toggle(menu: QMenu, title: str, checked: bool, on_toggle: Callable[[bool], None]) -> QAction:
    act = QAction(title, menu, checkable=True)
    act.setChecked(checked)
    act.toggled.connect(on_toggle)
    menu.addAction(act)
    return act


def add_action(menu: QMenu, title: str, fn: Callable[[], None], shortcut: Optional[str] = None) -> QAction:
    act = QAction(title, menu)
    if shortcut:
        act.setShortcut(shortcut)
    act.triggered.connect(lambda _=False: fn())
    menu.addAction(act)
    return act


def save_file_dialog(parent: QWidget, title: str, default_name: str, filt: str) -> Optional[Path]:
    path, _ = QFileDialog.getSaveFileName(parent, title, default_name, filt)
    return Path(path) if path else None


def parse_float(text: str, allow_negative: bool = True) -> Optional[float]:
    try:
        v = float(text.strip().replace(",", "."))
    except ValueError:
        return None
    if not allow_negative and v < 0:
        return None
    return v


def parse_int(text: str, allow_negative: bool = False) -> Optional[int]:
    try:
        v = int(text.strip())
    except ValueError:
        return None
    if not allow_negative and v < 0:
        return None
    return v


class RangeSelector(QWidget):
    """"All Samples" vs "N Samples [start, end]" selector used by the analysis tabs."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.all_radio = QRadioButton("All samples", checked=True)
        self.n_radio = QRadioButton("N samples")
        self.start = QLineEdit(placeholderText="start #")
        self.end = QLineEdit(placeholderText="end #")
        for w in (self.start, self.end):
            w.setMaximumWidth(90)
            w.textEdited.connect(lambda _t: self.n_radio.setChecked(True))
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.all_radio)
        lay.addWidget(self.n_radio)
        lay.addWidget(self.start)
        lay.addWidget(QLabel("…"))
        lay.addWidget(self.end)
        lay.addStretch(1)

    def set_range(self, start: int, end: int) -> None:
        self.start.setText(str(start))
        self.end.setText(str(end))
        self.n_radio.setChecked(True)

    def get(self, count: int) -> Tuple[Optional[Tuple[int, int]], Optional[str]]:
        """Returns ((start, end_inclusive), None) or (None, error)."""
        if count <= 0:
            return None, "No samples captured yet."
        if self.all_radio.isChecked():
            return (0, count - 1), None
        s, e = parse_int(self.start.text()), parse_int(self.end.text())
        if s is None or e is None:
            return None, "N Samples Start/End must be non-negative integers."
        from ..analysis import validate_range

        err = validate_range(s, e, count)
        return (None, err) if err else ((s, e), None)


def labeled_row(*widgets) -> QHBoxLayout:
    row = QHBoxLayout()
    for w in widgets:
        if isinstance(w, str):
            row.addWidget(QLabel(w))
        elif w is None:
            row.addStretch(1)
        else:
            row.addWidget(w)
    return row


ALIGN_RIGHT_VCENTER = Qt.AlignRight | Qt.AlignVCenter
