"""Graph windows.

* :class:`WaveformWindow` – the "Graphing Module" (x = sample #) and the "Date Time Graph"
  (x = wall-clock time). Also used for every math waveform, which can itself spawn more math
  waveforms and histograms without limit, like the original.
* :class:`HistogramWindow` – histogram of all or a range of samples.
* :class:`NSampleWindow` – persistence view of the last 1–10 blocks of N samples.
"""

from __future__ import annotations

import math
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

import numpy as np
import pyqtgraph as pg
import pyqtgraph.exporters
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QFont, QGuiApplication
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDockWidget, QFormLayout, QGridLayout, QGroupBox,
                               QInputDialog, QLabel, QLineEdit, QMainWindow, QMenu, QPushButton, QTabWidget,
                               QVBoxLayout, QWidget)

from .. import analysis
from ..engine import LogLevel, Sample
from ..formatting import format_timestamp
from ..scpi import format_number, is_overload
from .common import (COLOR_PRESETS, ColorButton, LogView, RangeSelector, add_action, add_choice_menu, add_toggle,
                     labeled_row, parse_float, random_rgb, save_file_dialog)

pg.setConfigOptions(antialias=False, useOpenGL=False)

THEMES = {
    # name: (plot background, axis/text foreground, grid alpha)
    "Default": ("#ffffff", "#000000"),
    "Black": ("#000000", "#e0e0e0"),
    "Gray": ("#d0d0d0", "#000000"),
    "Gray Black": ("#303030", "#e0e0e0"),
    "Blue": ("#0b1f3a", "#e0e8ff"),
}

REFRESH_CHOICES = [("1 ms", 1), ("5 ms", 5), ("10 ms", 10), ("50 ms", 50), ("100 ms", 100), ("200 ms", 200),
                   ("500 ms", 500), ("1 s", 1000), ("2 s", 2000), ("5 s", 5000)]


class SeriesBuffer:
    """Growable (timestamp, value) storage."""

    def __init__(self, capacity: int = 4096):
        self.y = np.empty(capacity, dtype=float)
        self.t = np.empty(capacity, dtype=float)  # POSIX seconds
        self.n = 0

    def clear(self) -> None:
        self.n = 0

    def _grow(self, need: int) -> None:
        cap = len(self.y)
        if need <= cap:
            return
        while cap < need:
            cap *= 2
        self.y = np.resize(self.y, cap)
        self.t = np.resize(self.t, cap)

    def append(self, value: float, ts: float) -> None:
        self._grow(self.n + 1)
        self.y[self.n] = value
        self.t[self.n] = ts
        self.n += 1

    def extend(self, values: np.ndarray, ts: np.ndarray) -> None:
        k = len(values)
        self._grow(self.n + k)
        self.y[self.n:self.n + k] = values
        self.t[self.n:self.n + k] = ts
        self.n += k

    @property
    def values(self) -> np.ndarray:
        return self.y[: self.n]

    @property
    def times(self) -> np.ndarray:
        return self.t[: self.n]


class _PlotHost(QMainWindow):
    """Common plot setup: theme, grid, legend, labels, image export, font size."""

    closed = Signal(object)

    def __init__(self, title: str, datetime_axis: bool = False, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self.setWindowTitle(title)
        axis = {"bottom": pg.DateAxisItem(orientation="bottom")} if datetime_axis else {}
        self.plot = pg.PlotWidget(axisItems=axis)
        self.plot_item: pg.PlotItem = self.plot.getPlotItem()
        self.legend = self.plot_item.addLegend(offset=(10, 10))
        self.theme = "Default"
        self.grid_x = True
        self.grid_y = True
        self.font_size = 10
        self.apply_theme("Default")
        self.plot_item.showGrid(x=True, y=True, alpha=0.3)

    # -- appearance -----------------------------------------------------------
    def apply_theme(self, name: str) -> None:
        self.theme = name
        bg, fg = THEMES[name]
        self.plot.setBackground(bg)
        for ax in ("left", "bottom", "right", "top"):
            a = self.plot_item.getAxis(ax)
            a.setPen(pg.mkPen(fg))
            a.setTextPen(pg.mkPen(fg))
        self.plot_item.titleLabel.setAttr("color", fg)
        self.legend.setLabelTextColor(fg)
        self._fg = fg
        self._set_labels_color()

    def _set_labels_color(self) -> None:
        for ax in ("left", "bottom"):
            a = self.plot_item.getAxis(ax)
            a.labelStyle = {"color": self._fg, "font-size": f"{self.font_size + 1}pt"}
            a._updateLabel()

    def set_font_size(self, pt: int) -> None:
        self.font_size = pt
        f = QFont()
        f.setPointSize(pt)
        for ax in ("left", "bottom"):
            self.plot_item.getAxis(ax).setTickFont(f)
        self._set_labels_color()

    def set_grid(self, x: Optional[bool] = None, y: Optional[bool] = None) -> None:
        if x is not None:
            self.grid_x = x
        if y is not None:
            self.grid_y = y
        self.plot_item.showGrid(x=self.grid_x, y=self.grid_y, alpha=0.3)

    def set_legend_visible(self, visible: bool) -> None:
        self.legend.setVisible(visible)

    def set_legend_corner(self, corner: str) -> None:
        anchors = {"Top Left": ((0, 0), (10, 10)), "Top Right": ((1, 0), (-10, 10)),
                   "Bottom Left": ((0, 1), (10, -30)), "Bottom Right": ((1, 1), (-10, -30))}
        (ax, ay), off = anchors[corner]
        self.legend.anchor(itemPos=(ax, ay), parentPos=(ax, ay), offset=off)

    def edit_label(self, which: str) -> None:
        current = {"title": self.plot_item.titleLabel.text,
                   "x": self.plot_item.getAxis("bottom").labelText,
                   "y": self.plot_item.getAxis("left").labelText}[which]
        text, ok = QInputDialog.getText(self, "Label", f"New {which} label:", text=current or "")
        if ok:
            if which == "title":
                self.plot_item.setTitle(text, color=self._fg)
            else:
                self.plot_item.setLabel("bottom" if which == "x" else "left", text)

    def save_image(self) -> None:
        path = save_file_dialog(self, "Save Graph Image", f"Graph_{datetime.now():%Y-%m-%d %H-%M-%S}.png",
                                "PNG (*.png);;JPEG (*.jpg);;SVG (*.svg)")
        if not path:
            return
        if path.suffix.lower() == ".svg":
            ex = pg.exporters.SVGExporter(self.plot_item)
        else:
            ex = pg.exporters.ImageExporter(self.plot_item)
            ex.parameters()["width"] = max(1200, int(self.plot.width()))
        ex.export(str(path))

    def copy_image(self) -> None:
        QGuiApplication.clipboard().setPixmap(self.plot.grab())

    def _appearance_menu(self, menu: QMenu) -> None:
        add_choice_menu(menu, "Graph Theme", [(k, k) for k in THEMES], self.theme, self.apply_theme)
        grid = menu.addMenu("Graph Grid")
        add_toggle(grid, "Vertical Grid", True, lambda on: self.set_grid(x=on))
        add_toggle(grid, "Horizontal Grid", True, lambda on: self.set_grid(y=on))
        leg = menu.addMenu("Graph Legend")
        add_toggle(leg, "Show Legend", True, self.set_legend_visible)
        add_choice_menu(leg, "Legend Position", [(c, c) for c in ("Top Left", "Top Right", "Bottom Left", "Bottom Right")],
                        "Top Left", self.set_legend_corner)
        lab = menu.addMenu("Axis Labels")
        add_action(lab, "Title Label…", lambda: self.edit_label("title"))
        add_action(lab, "X-Axis Label…", lambda: self.edit_label("x"))
        add_action(lab, "Y-Axis Label…", lambda: self.edit_label("y"))
        add_choice_menu(menu, "Font Size", [(str(s), s) for s in (8, 10, 12, 14, 16, 18)], self.font_size,
                        self.set_font_size)

    def closeEvent(self, ev) -> None:  # noqa: N802
        self.closed.emit(self)
        super().closeEvent(ev)


class WaveformWindow(_PlotHost):
    """Live or static waveform with statistics, math, histogram, markers and tracker."""

    def __init__(self, title: str, unit: str, y_label: str, datetime_axis: bool = False, live: bool = True,
                 values: Optional[np.ndarray] = None, times: Optional[np.ndarray] = None,
                 color: Tuple[int, int, int] = (0, 0, 255), x_offset: int = 0, owner: str = "",
                 parent: Optional[QWidget] = None):
        super().__init__(title, datetime_axis, parent)
        self.unit = unit
        self.y_label = y_label
        self.datetime_axis = datetime_axis
        self.live = live
        self.owner = owner
        self.x_offset = x_offset  # sample number of the first point (math on N-sample ranges)
        self.color = color
        self.children: List[QMainWindow] = []
        self.buf = SeriesBuffer()
        if values is not None:
            self.buf.extend(np.asarray(values, float),
                            np.asarray(times, float) if times is not None else np.zeros(len(values)))
        # info counters (original "Information" tab)
        self.total = self.invalid = self.positive = self.negative = 0
        self.latest = 0.0
        self.max_rec = -math.inf
        self.min_rec = math.inf
        self.mavg = 0.0
        self.mavg_count = 0
        self.mavg_factor = 50
        self.mavg_res = 8
        self.auto_axis = True
        self._pending: List[Sample] = []
        self._dirty = True
        if values is not None:
            for v in self.buf.values:
                self._account(float(v))

        self.curve = self.plot_item.plot(name=y_label, pen=pg.mkPen(QColor(*color), width=1))
        self.curve.setDownsampling(auto=True, method="peak")
        self.curve.setClipToView(True)
        self.plot_item.setLabel("left", y_label)
        self.plot_item.setLabel("bottom", "Date / Time" if datetime_axis else "N Samples")
        self.plot_item.vb.sigRangeChangedManually.connect(self._manual_range)

        self.log = LogView(limit=200)
        self._build_markers()
        self._build_tracker()
        self._build_docks()
        self._build_menus()
        self.setCentralWidget(self.plot)
        self.resize(1250, 760)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._render)
        self.timer.start(200)
        self.log.append(f"{'Live graph' if live else 'Waveform'} ready.", LogLevel.SUCCESS)

    # ---------------------------------------------------------------- data in
    def add_sample(self, s: Sample) -> None:
        """Called on the GUI thread for every new reading."""
        self._pending.append(s)

    def reset(self, unit: Optional[str] = None, y_label: Optional[str] = None) -> None:
        if unit:
            self.unit = unit
        if y_label:
            self.y_label = y_label
            self.plot_item.setLabel("left", y_label)
            self.legend.clear()
            self.legend.addItem(self.curve, y_label)
        self._pending.clear()
        self.buf.clear()
        self.total = self.invalid = self.positive = self.negative = 0
        self.latest, self.max_rec, self.min_rec = 0.0, -math.inf, math.inf
        self.mavg, self.mavg_count = 0.0, 0
        self._dirty = True
        self.log.append("Graph has been reset.", LogLevel.SUCCESS)

    def _account(self, v: float) -> None:
        self.total += 1
        self.latest = v
        if v >= 0:
            self.positive += 1
        else:
            self.negative += 1
        self.max_rec = max(self.max_rec, v)
        self.min_rec = min(self.min_rec, v)
        self.mavg_count += 1
        self.mavg += (v - self.mavg) / min(self.mavg_count, self.mavg_factor)

    def _ingest(self) -> None:
        if not self._pending:
            return
        batch, self._pending = self._pending, []
        for s in batch:
            if is_overload(s.value):
                self.invalid += 1
                continue
            self.buf.append(s.value, s.timestamp.timestamp())
            self._account(s.value)
        self._dirty = True

    def x_values(self) -> np.ndarray:
        if self.datetime_axis:
            return self.buf.times
        return np.arange(self.x_offset, self.x_offset + self.buf.n, dtype=float)

    def _render(self) -> None:
        self._ingest()
        if not self._dirty:
            return
        self._dirty = False
        if self.buf.n:
            self.curve.setData(self.x_values(), self.buf.values, skipFiniteCheck=True)
        else:
            self.curve.setData([], [])
        if self.auto_axis:
            self.plot_item.enableAutoRange()
        self._update_info()

    def _manual_range(self, *_a) -> None:
        if self.auto_axis:
            self.auto_axis = False
            self.auto_axis_action.setChecked(False)

    # ---------------------------------------------------------------- docks
    def _build_docks(self) -> None:
        info = QWidget()
        form = QFormLayout(info)
        self.info_labels = {}
        for key in ("Total Samples", "Invalid Samples", "Positive Samples", "Negative Samples", "Latest Sample",
                    "Max Recorded Sample", "Min Recorded Sample", "Moving AVG"):
            lab = QLabel("0")
            lab.setTextInteractionFlags(Qt.TextSelectableByMouse)
            self.info_labels[key] = lab
            form.addRow(key + ":", lab)
        form.addRow(QLabel("<b>Markers</b>"))
        self.marker_info = QLabel("—")
        self.marker_info.setWordWrap(True)
        self.marker_info.setTextInteractionFlags(Qt.TextSelectableByMouse)
        form.addRow(self.marker_info)

        tabs = QTabWidget()
        tabs.addTab(self._stats_tab(), "Statistics")
        tabs.addTab(self._math_tab(), "Math")
        tabs.addTab(self._hist_tab(), "Histogram")
        tabs.addTab(self._time_tab(), "Time Δ")

        side = QWidget()
        v = QVBoxLayout(side)
        box = QGroupBox("Information")
        QVBoxLayout(box).addWidget(info)
        v.addWidget(box)
        v.addWidget(tabs)
        v.addStretch(1)
        dock = QDockWidget("Tools", self)
        dock.setWidget(side)
        dock.setFeatures(QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetFloatable)
        self.addDockWidget(Qt.RightDockWidgetArea, dock)

        ldock = QDockWidget("Output Log", self)
        ldock.setWidget(self.log)
        ldock.setFeatures(QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetFloatable | QDockWidget.DockWidgetClosable)
        self.addDockWidget(Qt.BottomDockWidgetArea, ldock)
        self.log_dock = ldock

    def _update_info(self) -> None:
        L = self.info_labels
        L["Total Samples"].setText(str(self.total))
        L["Invalid Samples"].setText(str(self.invalid))
        L["Positive Samples"].setText(str(self.positive))
        L["Negative Samples"].setText(str(self.negative))
        if self.total:
            L["Latest Sample"].setText(f"{format_number(self.latest)} {self.unit}")
            L["Max Recorded Sample"].setText(f"{format_number(self.max_rec)} {self.unit}")
            L["Min Recorded Sample"].setText(f"{format_number(self.min_rec)} {self.unit}")
            L["Moving AVG"].setText(f"{format_number(round(self.mavg, self.mavg_res))} {self.unit}")

    def _selected(self, selector: RangeSelector) -> Optional[Tuple[int, int, np.ndarray, np.ndarray]]:
        rng, err = selector.get(self.buf.n)
        if err:
            self.log.append(err, LogLevel.ERROR)
            return None
        s, e = rng
        return s, e, self.buf.values[s:e + 1].copy(), self.buf.times[s:e + 1].copy()

    def _from_markers(self, selector: RangeSelector) -> None:
        if not (self.vmarkers[0].isVisible() and self.vmarkers[1].isVisible()):
            self.log.append("Add both vertical markers first (Markers menu).", LogLevel.WARNING)
            return
        a, b = sorted(self._marker_index(m) for m in self.vmarkers)
        selector.set_range(a, b)

    def _range_widget(self) -> RangeSelector:
        sel = RangeSelector()
        return sel

    def _range_block(self, layout: QVBoxLayout) -> RangeSelector:
        sel = self._range_widget()
        btn = QPushButton("Use vertical markers")
        btn.clicked.connect(lambda: self._from_markers(sel))
        layout.addWidget(sel)
        layout.addWidget(btn)
        return sel

    # statistics
    def _stats_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        self.stat_combo = QComboBox()
        self.stat_combo.addItems(list(analysis.STATISTICS))
        self.stat_range = self._range_block(v)
        calc = QPushButton("Calculate")
        calc.clicked.connect(self._calc_stat)
        all_btn = QPushButton("Calculate all")
        all_btn.clicked.connect(lambda: [self._calc_stat(name) for name in analysis.STATISTICS])
        v.addLayout(labeled_row("Statistic:", self.stat_combo))
        v.addLayout(labeled_row(calc, all_btn))
        v.addStretch(1)
        return w

    def _calc_stat(self, name: Optional[str] = None) -> None:
        name = name or self.stat_combo.currentText()
        sel = self._selected(self.stat_range)
        if not sel:
            return
        s, e, ys, _ = sel
        try:
            msg = analysis.describe_statistic(name, ys, self.unit, s + self.x_offset, e + self.x_offset,
                                              self.stat_range.all_radio.isChecked())
            self.log.append(msg, LogLevel.INFO)
        except Exception as ex:
            self.log.append(f"Could not calculate {name}: {ex}", LogLevel.ERROR)

    # math
    def _math_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        self.math_combo = QComboBox()
        for op in analysis.MATH_OPS:
            self.math_combo.addItem(op.label, op.key)
        grid = QGridLayout()
        self.math_param_labels = []
        self.math_params = []
        for i in range(4):
            lab, edit = QLabel(), QLineEdit()
            edit.setMaximumWidth(110)
            grid.addWidget(lab, i // 2, (i % 2) * 2)
            grid.addWidget(edit, i // 2, (i % 2) * 2 + 1)
            self.math_param_labels.append(lab)
            self.math_params.append(edit)
        defaults = {"db": ["20", "10", "1"], "dbm": ["10", "10", "600", "0.001"], "ema": ["50"]}
        self._math_defaults = defaults
        self.math_degrees = QCheckBox("Degrees (else radians)")
        self.math_title = QLineEdit(placeholderText="graph title (optional)")
        self.math_ylabel = QLineEdit(placeholderText=f"Y-axis label (default: {self.y_label})")
        self.math_color = ColorButton(random_rgb())
        rnd = QPushButton("Randomize")
        rnd.clicked.connect(lambda: self.math_color.set_rgb(random_rgb()))
        self.math_combo.currentIndexChanged.connect(self._math_op_changed)
        v.addWidget(self.math_combo)
        v.addLayout(grid)
        v.addWidget(self.math_degrees)
        self.math_range = self._range_block(v)
        v.addWidget(self.math_title)
        v.addWidget(self.math_ylabel)
        v.addLayout(labeled_row("Colour:", self.math_color, rnd, None))
        create = QPushButton("Create math waveform")
        create.clicked.connect(self._create_math)
        v.addWidget(create)
        v.addStretch(1)
        self._math_op_changed()
        return w

    def _math_op_changed(self) -> None:
        op = analysis.MATH_OPS_BY_KEY[self.math_combo.currentData()]
        defaults = self._math_defaults.get(op.key, [])
        for i, (lab, edit) in enumerate(zip(self.math_param_labels, self.math_params)):
            visible = i < len(op.params)
            lab.setVisible(visible)
            edit.setVisible(visible)
            if visible:
                lab.setText(op.params[i] + ":")
                if not edit.text() and i < len(defaults):
                    edit.setText(defaults[i])
        self.math_degrees.setEnabled(op.angle)

    def _create_math(self) -> None:
        key = self.math_combo.currentData()
        op = analysis.MATH_OPS_BY_KEY[key]
        params = []
        for i in range(len(op.params)):
            v = parse_float(self.math_params[i].text())
            if v is None:
                self.log.append(f"{op.label}: parameter {op.params[i]} must be a real number.", LogLevel.ERROR)
                return
            params.append(v)
        sel = self._selected(self.math_range)
        if not sel:
            return
        s, e, ys, ts = sel
        degrees = self.math_degrees.isChecked() and op.angle
        try:
            out = analysis.apply_math(key, ys, params, degrees)
        except ValueError as ex:
            self.log.append(f"Cannot create math waveform: {ex}", LogLevel.ERROR)
            return
        scope = "All Samples" if self.math_range.all_radio.isChecked() else f"N Samples {s + self.x_offset}–{e + self.x_offset}"
        title = f"Math Waveform [{scope}]: {analysis.math_title(key, params, degrees)}"
        ylabel = self.math_ylabel.text().strip() or self.y_label
        win = WaveformWindow(f"{self.owner} {title}".strip(), self.unit, ylabel, self.datetime_axis, live=False,
                             values=out, times=ts, color=self.math_color.rgb, x_offset=s + self.x_offset,
                             owner=self.owner)
        if self.math_title.text().strip():
            win.plot_item.setTitle(self.math_title.text().strip())
        self._spawn(win)
        self.log.append(f"Created: {title}", LogLevel.SUCCESS)

    # histogram
    def _hist_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        self.hist_bin = QLineEdit("0")
        self.hist_width = QLineEdit("0")
        self.hist_border = QLineEdit("1")
        self.hist_curve = QCheckBox("Plot curve")
        self.hist_title = QLineEdit(placeholderText="graph title (optional)")
        self.hist_xlabel = QLineEdit(placeholderText=f"X-axis label (default: {self.y_label})")
        self.hist_color = ColorButton(random_rgb())
        rnd = QPushButton("Randomize")
        rnd.clicked.connect(lambda: self.hist_color.set_rgb(random_rgb()))
        form = QFormLayout()
        form.addRow("Bin size (0 = auto):", self.hist_bin)
        form.addRow("Bar width (0 = bin):", self.hist_width)
        form.addRow("Bar border:", self.hist_border)
        v.addLayout(form)
        v.addWidget(self.hist_curve)
        self.hist_range = self._range_block(v)
        v.addWidget(self.hist_title)
        v.addWidget(self.hist_xlabel)
        v.addLayout(labeled_row("Colour:", self.hist_color, rnd, None))
        b = QPushButton("Create histogram")
        b.clicked.connect(self._create_hist)
        v.addWidget(b)
        v.addStretch(1)
        return w

    def _create_hist(self) -> None:
        bin_size = parse_float(self.hist_bin.text(), allow_negative=False)
        width = parse_float(self.hist_width.text(), allow_negative=False)
        border = parse_float(self.hist_border.text(), allow_negative=False)
        if None in (bin_size, width, border):
            self.log.append("Histogram: bin size, bar width and border must be non-negative numbers.", LogLevel.ERROR)
            return
        sel = self._selected(self.hist_range)
        if not sel:
            return
        s, e, ys, ts = sel
        try:
            res = analysis.histogram(ys, bin_size)
        except ValueError as ex:
            self.log.append(f"Cannot create histogram: {ex}", LogLevel.ERROR)
            return
        title = f"Histogram Waveform [{s + self.x_offset}, {e + self.x_offset}]"
        win = HistogramWindow(f"{self.owner} {title}".strip(), ys, ts, res,
                              x_label=self.hist_xlabel.text().strip() or self.y_label, unit=self.unit,
                              color=self.hist_color.rgb, bar_width=width or None, border=border,
                              curve=self.hist_curve.isChecked(), graph_title=self.hist_title.text().strip())
        self._spawn(win)
        self.log.append(f"Created: {title}, bin size {format_number(res.bin_size)}", LogLevel.SUCCESS)

    # time difference
    def _time_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        self.time_unit = QComboBox()
        self.time_unit.addItem("Seconds", "s")
        self.time_unit.addItem("Minutes", "min")
        self.time_unit.addItem("Hours", "h")
        self.time_range = self._range_block(v)
        v.addLayout(labeled_row("Duration in:", self.time_unit, None))
        b = QPushButton("Calculate")
        b.clicked.connect(self._calc_time)
        v.addWidget(b)
        v.addStretch(1)
        return w

    def _calc_time(self) -> None:
        sel = self._selected(self.time_range)
        if not sel:
            return
        s, e, ys, ts = sel
        if not ts[0] and not ts[-1]:
            self.log.append("This waveform has no timestamps.", LogLevel.ERROR)
            return
        t0, t1 = datetime.fromtimestamp(ts[0]), datetime.fromtimestamp(ts[-1])
        unit = self.time_unit.currentData()
        d = analysis.time_difference(t0, t1, unit)
        name = {"s": "Seconds", "min": "Minutes", "h": "Hours"}[unit]
        self.log.append(f"[Start Sample: {format_timestamp(t0)}, {format_number(ys[0])}{self.unit}, "
                        f"End Sample: {format_timestamp(t1)}, {format_number(ys[-1])}{self.unit}]", LogLevel.SUCCESS)
        self.log.append(f"∆ Time Difference between [Start Sample: {s + self.x_offset}, End Sample: "
                        f"{e + self.x_offset}] is {d} {name}", LogLevel.SUCCESS)

    def _spawn(self, win: QMainWindow) -> None:
        self.children.append(win)
        win.closed.connect(lambda w: self.children.remove(w) if w in self.children else None)
        win.show()

    # ---------------------------------------------------------------- markers & tracker
    def _build_markers(self) -> None:
        self.hmarkers: List[pg.InfiniteLine] = []
        self.vmarkers: List[pg.InfiniteLine] = []
        colors = [(220, 20, 60), (30, 144, 255)]
        for i in range(2):
            h = pg.InfiniteLine(angle=0, movable=True, pen=pg.mkPen(colors[i], width=1.5, style=Qt.DashLine),
                                label=f"H{i + 1}: {{value:.7g}}", labelOpts={"position": 0.05, "color": colors[i]})
            v = pg.InfiniteLine(angle=90, movable=True, pen=pg.mkPen(colors[i], width=1.5, style=Qt.DashLine),
                                label=f"V{i + 1}", labelOpts={"position": 0.95, "color": colors[i]})
            for m in (h, v):
                m.setVisible(False)
                m.setZValue(50)
                self.plot_item.addItem(m, ignoreBounds=True)
                m.sigPositionChanged.connect(self._update_marker_info)
            self.hmarkers.append(h)
            self.vmarkers.append(v)

    def _place_marker(self, kind: str, i: int) -> None:
        (xr, yr) = self.plot_item.vb.viewRange()
        m = (self.hmarkers if kind == "h" else self.vmarkers)[i]
        frac = 0.33 if i == 0 else 0.66
        if kind == "h":
            m.setPos(yr[0] + (yr[1] - yr[0]) * frac)
        else:
            m.setPos(xr[0] + (xr[1] - xr[0]) * frac)
        m.setVisible(True)
        self._update_marker_info()

    def _clear_markers(self, kind: str) -> None:
        for m in ((self.hmarkers if kind in ("h", "all") else []) + (self.vmarkers if kind in ("v", "all") else [])):
            m.setVisible(False)
        self._update_marker_info()

    def _marker_index(self, m: pg.InfiniteLine) -> int:
        n = self.buf.n
        if n == 0:
            return 0
        x = m.value()
        if self.datetime_axis:
            idx = int(np.clip(np.searchsorted(self.buf.times, x), 0, n - 1))
        else:
            idx = int(np.clip(round(x) - self.x_offset, 0, n - 1))
        return idx

    def _update_marker_info(self) -> None:
        parts = []
        h = [m for m in self.hmarkers if m.isVisible()]
        for i, m in enumerate(self.hmarkers):
            if m.isVisible():
                parts.append(f"H{i + 1} = {m.value():.9g} {self.unit}")
        if len(h) == 2:
            parts.append(f"ΔH = {self.hmarkers[1].value() - self.hmarkers[0].value():.9g} {self.unit}")
        for i, m in enumerate(self.vmarkers):
            if m.isVisible():
                idx = self._marker_index(m)
                if self.buf.n:
                    val = self.buf.values[idx]
                    ts = self.buf.times[idx]
                    when = f", {format_timestamp(datetime.fromtimestamp(ts))}" if ts else ""
                    parts.append(f"V{i + 1}: #{idx + self.x_offset} = {val:.9g} {self.unit}{when}")
                    m.label.setFormat(f"V{i + 1}: #{idx + self.x_offset}")
        if all(m.isVisible() for m in self.vmarkers) and self.buf.n:
            a, b = (self._marker_index(m) for m in self.vmarkers)
            dt = self.buf.times[b] - self.buf.times[a]
            dv = self.buf.values[b] - self.buf.values[a]
            parts.append(f"ΔV: {abs(b - a)} samples, Δy = {dv:.9g} {self.unit}" + (f", Δt = {dt:.3f} s" if dt else ""))
        self.marker_info.setText("\n".join(parts) if parts else "—")

    def _build_tracker(self) -> None:
        pen = pg.mkPen((128, 128, 128), width=1, style=Qt.DotLine)
        self.track_h = pg.InfiniteLine(angle=0, movable=False, pen=pen)
        self.track_v = pg.InfiniteLine(angle=90, movable=False, pen=pen)
        self.track_text = pg.TextItem(anchor=(0, 1), color=(80, 80, 80), fill=pg.mkBrush(255, 255, 255, 200))
        for it in (self.track_h, self.track_v, self.track_text):
            it.setVisible(False)
            it.setZValue(100)
            self.plot_item.addItem(it, ignoreBounds=True)
        self.tracker_on = False
        self.tracker_coords = True
        self.tracker_data = True
        self.plot.scene().sigMouseMoved.connect(self._mouse_moved)

    def set_tracker(self, on: bool) -> None:
        self.tracker_on = on
        for it in (self.track_h, self.track_v, self.track_text):
            it.setVisible(on)

    def set_tracker_style(self, style: Qt.PenStyle) -> None:
        for it in (self.track_h, self.track_v):
            p = it.pen
            p.setStyle(style)
            it.setPen(p)

    def set_tracker_color(self, rgb) -> None:
        for it in (self.track_h, self.track_v):
            it.setPen(pg.mkPen(rgb, width=1, style=it.pen.style()))

    def _mouse_moved(self, pos) -> None:
        if not self.tracker_on or not self.plot_item.sceneBoundingRect().contains(pos):
            return
        p = self.plot_item.vb.mapSceneToView(pos)
        self.track_h.setPos(p.y())
        self.track_v.setPos(p.x())
        lines = []
        if self.tracker_coords:
            xs = datetime.fromtimestamp(p.x()).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] if self.datetime_axis and p.x() > 0 \
                else f"{p.x():.6g}"
            lines.append(f"X: {xs}  Y: {p.y():.7g}")
        if self.tracker_data and self.buf.n:
            if self.datetime_axis:
                idx = int(np.clip(np.searchsorted(self.buf.times, p.x()), 0, self.buf.n - 1))
            else:
                idx = int(np.clip(round(p.x()) - self.x_offset, 0, self.buf.n - 1))
            ts = self.buf.times[idx]
            when = f"  {format_timestamp(datetime.fromtimestamp(ts))}" if ts else ""
            lines.append(f"#{idx + self.x_offset}: {self.buf.values[idx]:.9g} {self.unit}{when}")
        self.track_text.setText("\n".join(lines))
        self.track_text.setPos(p.x(), p.y())

    # ---------------------------------------------------------------- menus
    def _build_menus(self) -> None:
        mb = self.menuBar()
        f = mb.addMenu("File")
        sd = f.addMenu("Save Data")
        add_action(sd, "To Text File…", lambda: self.save_data("txt"))
        add_action(sd, "To CSV File…", lambda: self.save_data("csv"))
        add_action(f, "Save Graph Image…", self.save_image, "Ctrl+S")
        add_action(f, "Copy Graph Image", self.copy_image, "Ctrl+C")
        f.addSeparator()
        add_action(f, "Exit", self.close)

        g = mb.addMenu("Graph Settings")
        self.auto_axis_action = add_toggle(g, "Auto Axis", True, self._set_auto_axis)
        add_action(g, "Force Auto Axis", lambda: self.plot_item.enableAutoRange(), "Ctrl+A")
        if self.live:
            add_action(g, "Reset Graph", self.reset)
            add_choice_menu(g, "Refresh Rate", REFRESH_CHOICES, 200, lambda ms: self.timer.setInterval(ms))
        g.addSeparator()
        self._appearance_menu(g)
        add_choice_menu(g, "Measurement Colour", [(k, v) for k, v in COLOR_PRESETS.items()], None, self.set_curve_color)
        add_toggle(g, "Log scale Y", False, lambda on: self.plot_item.setLogMode(y=on))
        ma = g.addMenu("Moving AVG")
        add_choice_menu(ma, "Factor", [(str(v), v) for v in (50, 100, 200, 400, 800, 1000)], 50, self._set_mavg_factor)
        add_choice_menu(ma, "Resolution", [(str(v), v) for v in range(2, 9)], 8, self._set_mavg_res)

        m = mb.addMenu("Markers")
        hm = m.addMenu("Horizontal Markers")
        add_action(hm, "Add Draggable Marker 1", lambda: self._place_marker("h", 0))
        add_action(hm, "Add Draggable Marker 2", lambda: self._place_marker("h", 1))
        add_action(hm, "Clear Horizontal Markers", lambda: self._clear_markers("h"))
        vm = m.addMenu("Vertical Markers")
        add_action(vm, "Add Draggable Marker 1", lambda: self._place_marker("v", 0))
        add_action(vm, "Add Draggable Marker 2", lambda: self._place_marker("v", 1))
        add_action(vm, "Clear Vertical Markers", lambda: self._clear_markers("v"))
        add_action(vm, "Write to N Samples Fields", self._markers_to_all_ranges)
        add_action(m, "Clear All Markers", lambda: self._clear_markers("all"))

        t = mb.addMenu("Mouse Tracker")
        add_toggle(t, "Show Tracker", False, self.set_tracker)

        def _coords(on: bool) -> None:
            self.tracker_coords = on

        def _data(on: bool) -> None:
            self.tracker_data = on

        add_toggle(t, "Show X,Y Coordinates", True, _coords)
        add_toggle(t, "Show Measurement Data", True, _data)
        add_choice_menu(t, "Tracker Style", [("Dotted Lines", Qt.DotLine), ("Dashed Lines", Qt.DashLine),
                                             ("Dot-Dash Lines", Qt.DashDotLine), ("Solid Lines", Qt.SolidLine)],
                        Qt.DotLine, self.set_tracker_style)
        add_choice_menu(t, "Tracker Color", [(k, v) for k, v in COLOR_PRESETS.items()], None, self.set_tracker_color)

        lg = mb.addMenu("Output Log")
        add_action(lg, "Save Output Log…", self._save_log)
        add_action(lg, "Clear Output Log", self.log.clear)
        add_choice_menu(lg, "Auto Clear", [("20 Logs", 20), ("40 Logs", 40), ("80 Logs", 80), ("100 Logs", 100),
                                           ("200 Logs", 200), ("Never", 0)], 200, self._set_log_limit)
        lg.addAction(self.log_dock.toggleViewAction())

        h = mb.addMenu("Help")
        add_action(h, "Information", lambda: self.log.append(
            "Drag to pan, right-drag or wheel to zoom, right-click the plot for pyqtgraph's own "
            "menu (export, axis ranges). Analysis tools are in the Tools panel.", LogLevel.INFO))

    def _set_log_limit(self, n: int) -> None:
        self.log.limit = n

    def _set_auto_axis(self, on: bool) -> None:
        self.auto_axis = on
        if on:
            self.plot_item.enableAutoRange()

    def _set_mavg_factor(self, v: int) -> None:
        self.mavg_factor = v
        self.log.append(f"Exponential Moving Average's factor set to {v}.", LogLevel.SUCCESS)

    def _set_mavg_res(self, v: int) -> None:
        self.mavg_res = v
        self._update_info()

    def set_curve_color(self, rgb) -> None:
        self.color = rgb
        self.curve.setPen(pg.mkPen(QColor(*rgb), width=1))

    def _markers_to_all_ranges(self) -> None:
        for sel in (self.stat_range, self.math_range, self.hist_range, self.time_range):
            self._from_markers(sel)

    def _save_log(self) -> None:
        path = save_file_dialog(self, "Save Output Log", f"Graph Output Log_{datetime.now():%Y-%m-%d %H-%M-%S}.txt",
                                "Text (*.txt)")
        if path:
            path.write_text(self.log.plain_text(), encoding="utf-8")

    def save_data(self, kind: str) -> None:
        name = f"Graph Plot Data_{self.unit}_{datetime.now():%Y-%m-%d %H-%M-%S}.{kind}"
        path = save_file_dialog(self, "Save Data", name, "CSV (*.csv)" if kind == "csv" else "Text (*.txt)")
        if not path:
            return
        try:
            write_series(path, self.buf.values, self.buf.times)
            self.log.append(f"Saved {self.buf.n} samples to {path}", LogLevel.SUCCESS)
        except OSError as e:
            self.log.append(f"Could not save Graph Plot Data: {e}", LogLevel.ERROR)

    def closeEvent(self, ev) -> None:  # noqa: N802
        self.timer.stop()
        super().closeEvent(ev)


def write_series(path: Path, values: Sequence[float], times: Sequence[float]) -> None:
    """``yyyy-MM-dd h:mm:ss.fff tt,value`` per line — the original's export format."""
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for v, t in zip(values, times):
            stamp = format_timestamp(datetime.fromtimestamp(t)) if t else ""
            fh.write(f"{stamp},{format_number(v)}\n")


class HistogramWindow(_PlotHost):
    def __init__(self, title: str, values: np.ndarray, times: np.ndarray, result: analysis.HistogramResult,
                 x_label: str, unit: str, color=(0, 120, 255), bar_width: Optional[float] = None, border: float = 1.0,
                 curve: bool = False, graph_title: str = "", parent: Optional[QWidget] = None):
        super().__init__(title, False, parent)
        self.values, self.times, self.result, self.unit = values, times, result, unit
        width = bar_width or result.bin_size
        self.bars = pg.BarGraphItem(x=result.centers, height=result.counts, width=width,
                                    brush=pg.mkBrush(QColor(*color)), pen=pg.mkPen((0, 0, 0), width=border))
        self.plot_item.addItem(self.bars)
        self.legend.addItem(self.bars, "Count")
        if curve:
            self.plot_item.plot(result.centers, result.counts, pen=pg.mkPen((0, 0, 0), width=max(1.0, border)),
                                name="Curve")
        self.plot_item.setLabel("bottom", x_label)
        self.plot_item.setLabel("left", "Count (#)")
        if graph_title:
            self.plot_item.setTitle(graph_title)
        info = QWidget()
        form = QFormLayout(info)
        form.addRow("Total Samples:", QLabel(str(len(values))))
        form.addRow("Positive Samples:", QLabel(str(int(np.sum(values >= 0)))))
        form.addRow("Negative Samples:", QLabel(str(int(np.sum(values < 0)))))
        form.addRow("Bin Size:", QLabel(format_number(result.bin_size)))
        form.addRow("Bins:", QLabel(str(len(result.counts))))
        form.addRow("Mean:", QLabel(f"{format_number(float(np.mean(values)))} {unit}"))
        sd = float(np.std(values, ddof=1)) if len(values) > 1 else float("nan")
        form.addRow("Std Deviation:", QLabel(f"{sd:.9g} {unit}"))
        dock = QDockWidget("Information", self)
        dock.setWidget(info)
        self.addDockWidget(Qt.RightDockWidgetArea, dock)
        self.setCentralWidget(self.plot)
        mb = self.menuBar()
        f = mb.addMenu("File")
        sd_menu = f.addMenu("Save Data")
        add_action(sd_menu, "Samples to Text File…", lambda: self._save_samples("txt"))
        add_action(sd_menu, "Samples to CSV File…", lambda: self._save_samples("csv"))
        add_action(sd_menu, "Bins (center,count) to CSV…", self._save_bins)
        add_action(f, "Save Graph Image…", self.save_image)
        add_action(f, "Copy Graph Image", self.copy_image)
        add_action(f, "Exit", self.close)
        self._appearance_menu(mb.addMenu("Graph Settings"))
        self.resize(1000, 650)

    def _save_samples(self, kind: str) -> None:
        path = save_file_dialog(self, "Save", f"Histogram Plot Data_{datetime.now():%Y-%m-%d %H-%M-%S}.{kind}",
                                "CSV (*.csv)" if kind == "csv" else "Text (*.txt)")
        if path:
            write_series(path, self.values, self.times)

    def _save_bins(self) -> None:
        path = save_file_dialog(self, "Save", f"Histogram Bins_{datetime.now():%Y-%m-%d %H-%M-%S}.csv", "CSV (*.csv)")
        if path:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("bin_start,bin_end,count\n")
                for a, b, c in zip(self.result.edges[:-1], self.result.edges[1:], self.result.counts):
                    fh.write(f"{a!r},{b!r},{int(c)}\n")


N_SAMPLE_COLORS = ["#ff0000", "#ff4010", "#ff5d22", "#ff7333", "#ff8746", "#ff995a", "#ffaa6f", "#ffba85",
                   "#ffc99d", "#ffead3"]


class NSampleWindow(_PlotHost):
    """Shows the newest block of N samples; older blocks fade out behind it."""

    def __init__(self, n: int, unit: str, y_label: str, title: str, parent: Optional[QWidget] = None):
        super().__init__(f"{title} N Sample Graph (N = {n})", False, parent)
        self.n = n
        self.unit = unit
        self.on_screen = 1
        self.blocks: List[np.ndarray] = []
        self._acc: List[float] = []
        self.total_waveforms = self.total_samples = self.pos = self.neg = 0
        self.auto_axis = True
        self.curves = []
        for i in reversed(range(10)):
            c = self.plot_item.plot(pen=pg.mkPen(N_SAMPLE_COLORS[i], width=1), name=f"Waveform {i + 1}")
            self.curves.insert(0, c)
        self.plot_item.setLabel("left", y_label)
        self.plot_item.setLabel("bottom", "N Samples")
        self._dirty = False
        self.labels = {}
        info = QWidget()
        form = QFormLayout(info)
        for k in ("Total Waveforms", "Total Samples", "Positive Samples", "Negative Samples", "Waveform 1 Average",
                  "Waveform 1 Maximum", "Waveform 1 Minimum"):
            self.labels[k] = QLabel("0")
            form.addRow(k + ":", self.labels[k])
        dock = QDockWidget("Information", self)
        dock.setWidget(info)
        self.addDockWidget(Qt.RightDockWidgetArea, dock)
        self.setCentralWidget(self.plot)
        mb = self.menuBar()
        f = mb.addMenu("File")
        add_action(f, "Save Graph Image…", self.save_image)
        add_action(f, "Copy Graph Image", self.copy_image)
        add_action(f, "Exit", self.close)
        g = mb.addMenu("Graph Settings")
        add_toggle(g, "Auto Axis", True, self._set_auto)
        add_action(g, "Force Auto Axis", lambda: self.plot_item.enableAutoRange())
        add_action(g, "Reset Graph", self.reset)
        self._appearance_menu(g)
        add_choice_menu(mb.addMenu("Waveforms"), "Waveforms on screen",
                        [(f"{i} Waveform{'s' if i > 1 else ''}", i) for i in range(1, 11)], 1, self.set_on_screen)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._render)
        self.timer.start(100)
        self.set_on_screen(1)
        self.resize(1000, 650)

    def _set_auto(self, on: bool) -> None:
        self.auto_axis = on

    def set_on_screen(self, k: int) -> None:
        self.on_screen = k
        self.legend.clear()
        for i, c in enumerate(self.curves):
            c.setVisible(i < k)
            if i < k:
                self.legend.addItem(c, f"Waveform {i + 1}")
        self.blocks = self.blocks[:k]
        self._dirty = True

    def reset(self, unit: Optional[str] = None, y_label: Optional[str] = None) -> None:
        if unit:
            self.unit = unit
        if y_label:
            self.plot_item.setLabel("left", y_label)
        self.blocks.clear()
        self._acc.clear()
        self.total_waveforms = self.total_samples = self.pos = self.neg = 0
        self._dirty = True

    def add_sample(self, s: Sample) -> None:
        if is_overload(s.value):
            return
        self._acc.append(s.value)
        self.total_samples += 1
        if s.value >= 0:
            self.pos += 1
        else:
            self.neg += 1
        if len(self._acc) >= self.n:
            block = np.array(self._acc[: self.n])
            self._acc = self._acc[self.n:]
            self.blocks.insert(0, block)
            del self.blocks[self.on_screen:]
            self.total_waveforms += 1
            self._dirty = True

    def _render(self) -> None:
        if not self._dirty:
            return
        self._dirty = False
        x = np.arange(self.n)
        for i, c in enumerate(self.curves):
            if i < len(self.blocks):
                c.setData(x, self.blocks[i])
            else:
                c.setData([], [])
        L = self.labels
        L["Total Waveforms"].setText(str(self.total_waveforms))
        L["Total Samples"].setText(str(self.total_samples))
        L["Positive Samples"].setText(str(self.pos))
        L["Negative Samples"].setText(str(self.neg))
        if self.blocks:
            b = self.blocks[0]
            L["Waveform 1 Average"].setText(f"{round(float(b.mean()), 7)} {self.unit}")
            L["Waveform 1 Maximum"].setText(f"{round(float(b.max()), 7)} {self.unit}")
            L["Waveform 1 Minimum"].setText(f"{round(float(b.min()), 7)} {self.unit}")
        if self.auto_axis:
            self.plot_item.enableAutoRange()

    def closeEvent(self, ev) -> None:  # noqa: N802
        self.timer.stop()
        super().closeEvent(ev)
