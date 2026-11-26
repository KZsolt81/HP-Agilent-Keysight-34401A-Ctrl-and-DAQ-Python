"""Measurement Table window (port of Measurement_Table.xaml)."""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional, Tuple

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QFont
from PySide6.QtWidgets import QHeaderView, QLabel, QMainWindow, QTableView, QVBoxLayout, QWidget

from ..engine import Sample
from ..formatting import format_timestamp
from .common import add_action, add_choice_menu, add_toggle, save_file_dialog

HEADERS = ("#", "Date Time", "Measurement", "Unit")


class _Model(QAbstractTableModel):
    def __init__(self) -> None:
        super().__init__()
        self.rows: List[Tuple[str, str, str]] = []
        self.alternate = True
        self.fg: Optional[QColor] = None

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 4

    def data(self, index, role=Qt.DisplayRole):
        if role == Qt.DisplayRole:
            r = index.row()
            if index.column() == 0:
                return r + 1
            return self.rows[r][index.column() - 1]
        if role == Qt.ForegroundRole and self.fg is not None:
            return QBrush(self.fg)
        if role == Qt.TextAlignmentRole and index.column() == 2:
            return int(Qt.AlignRight | Qt.AlignVCenter)
        return None

    def headerData(self, section, orientation, role=Qt.DisplayRole):  # noqa: N802
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return HEADERS[section]
        return None

    def append(self, rows: List[Tuple[str, str, str]]) -> None:
        if not rows:
            return
        first = len(self.rows)
        self.beginInsertRows(QModelIndex(), first, first + len(rows) - 1)
        self.rows.extend(rows)
        self.endInsertRows()

    def clear(self) -> None:
        self.beginResetModel()
        self.rows.clear()
        self.endResetModel()


class TableWindow(QMainWindow):
    closed = Signal(object)

    def __init__(self, title: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self.setWindowTitle(f"{title} Measurement Table")
        self.model = _Model()
        self.view = QTableView()
        self.view.setModel(self.model)
        self.view.setAlternatingRowColors(True)
        self.view.verticalHeader().setVisible(False)
        self.view.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.view.horizontalHeader().setStretchLastSection(True)
        self.view.setColumnWidth(0, 70)
        self.view.setColumnWidth(1, 230)
        self.view.setColumnWidth(2, 170)
        self.total = QLabel("Total Measurement: 0")
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(self.view)
        lay.addWidget(self.total)
        self.setCentralWidget(w)
        self.auto_scroll = True
        self._pending: List[Tuple[str, str, str]] = []
        mb = self.menuBar()
        f = mb.addMenu("File")
        s = f.addMenu("Save")
        add_action(s, "To CSV File…", lambda: self.save("csv"))
        add_action(s, "To TEXT File…", lambda: self.save("txt"))
        add_action(f, "Exit", self.close)
        t = mb.addMenu("Table")
        add_action(t, "Clear Table", self.clear)
        add_toggle(t, "Auto Scroll", True, self._set_auto)
        add_toggle(t, "Alternating Background", True, self.view.setAlternatingRowColors)
        add_action(t, "Resize Columns", self.view.resizeColumnsToContents)
        add_choice_menu(t, "Font Size", [(str(s), s) for s in (9, 10, 12, 14, 16, 18)], 10, self._font_size)
        add_choice_menu(t, "Font Style", [(n, n) for n in ("Segoe UI", "Arial", "Courier New", "Times New Roman",
                                                           "Consolas", "DejaVu Sans")], "Segoe UI", self._font_family)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._flush)
        self.timer.start(200)
        self.resize(620, 700)

    def _set_auto(self, on: bool) -> None:
        self.auto_scroll = on

    def _font_size(self, pt: int) -> None:
        f = self.view.font()
        f.setPointSize(pt)
        self.view.setFont(f)
        self.view.verticalHeader().setDefaultSectionSize(int(pt * 2.2))

    def _font_family(self, family: str) -> None:
        f = QFont(family, self.view.font().pointSize())
        self.view.setFont(f)

    def add_sample(self, s: Sample) -> None:
        self._pending.append((format_timestamp(s.timestamp), s.raw, s.function.unit))

    def _flush(self) -> None:
        if not self._pending:
            return
        batch, self._pending = self._pending, []
        self.model.append(batch)
        self.total.setText(f"Total Measurement: {len(self.model.rows)}")
        if self.auto_scroll:
            self.view.scrollToBottom()

    def clear(self) -> None:
        self._pending.clear()
        self.model.clear()
        self.total.setText("Total Measurement: 0")

    def save(self, kind: str) -> None:
        path = save_file_dialog(self, "Save Table", f"Measurement Table_{datetime.now():%Y-%m-%d %H-%M-%S}.{kind}",
                                "CSV (*.csv)" if kind == "csv" else "Text (*.txt)")
        if not path:
            return
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            for i, (ts, val, unit) in enumerate(self.model.rows, 1):
                fh.write(f"{i},{ts},{val},{unit}\n")

    def closeEvent(self, ev) -> None:  # noqa: N802
        self.timer.stop()
        self.closed.emit(self)
        super().closeEvent(ev)
