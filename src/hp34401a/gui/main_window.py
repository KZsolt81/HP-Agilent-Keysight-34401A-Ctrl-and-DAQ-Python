"""Main control window (port of MainWindow.xaml / MainWindow.xaml.cs)."""

from __future__ import annotations

import random
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Sequence

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QDesktopServices, QFont
from PySide6.QtWidgets import (QButtonGroup, QFrame, QGridLayout, QGroupBox, QHBoxLayout, QInputDialog, QLabel,
                               QLineEdit, QMainWindow, QMessageBox, QPushButton, QSplitter, QTabWidget,
                               QVBoxLayout, QWidget)

from .. import __version__
from ..datalog import SAVE_INTERVAL_CHOICES, DataLogger, OutputLogFile
from ..engine import AcquisitionEngine, LogLevel, Sample, StatsTracker
from ..formatting import full_si, partial_si, scale_stat
from ..scpi import (AC_BANDWIDTH_CHOICES, APERTURE_CHOICES, FUNCTIONS, NPLC_CHOICES, Function, format_number,
                    is_overload)
from ..settings import AppConfig
from ..speech import RATE_WPM, Announcer, Speaker
from ..transport import Transport
from .common import (COLOR_PRESETS, LogBridge, LogView, add_action, add_choice_menu, add_toggle, labeled_row,
                     parse_float, parse_int, rgb_css, save_file_dialog)
from .connect_dialog import ConnectDialog
from .table_window import TableWindow
from .waveform import NSampleWindow, WaveformWindow

SELECTED_CSS = """
QPushButton:checked { border: 2px solid #00CE30; background-color: #e6ffe9; font-weight: bold; }
QPushButton { padding: 3px 8px; }
"""


class EngineBridge(QObject):
    sample = Signal(object)
    function_changed = Signal(object)
    busy = Signal(bool)
    exit_request = Signal()


def _btn(text: str, fn: Callable[[], None], checkable: bool = False, tip: str = "") -> QPushButton:
    b = QPushButton(text)
    b.setCheckable(checkable)
    if tip:
        b.setToolTip(tip)
    b.clicked.connect(lambda _=False: fn())
    return b


class MainWindow(QMainWindow):
    def __init__(self, config: Optional[AppConfig] = None):
        super().__init__()
        self.cfg = config or AppConfig.load()
        self.setWindowTitle("HP 34401A")
        self.setStyleSheet(SELECTED_CSS)
        self.engine: Optional[AcquisitionEngine] = None
        self.transport: Optional[Transport] = None
        self.datalogger: Optional[DataLogger] = None
        self.outlog: Optional[OutputLogFile] = None
        self.stats = StatsTracker(self.cfg.avg_factor)
        self.avg_resolution = self.cfg.avg_resolution
        self.partial_si = self.cfg.display.partial_si
        self.latest: Optional[float] = None
        self.show_display_errors = False
        self.graphs: List[WaveformWindow] = []
        self.nsample_graphs: List[NSampleWindow] = []
        self.tables: List[TableWindow] = []
        self.add_to_graphs = True
        self.add_to_table = True
        self._display_dirty = False
        self._last_sample: Optional[Sample] = None

        self.bridge = EngineBridge()
        self.bridge.sample.connect(self._on_sample)
        self.bridge.function_changed.connect(self._on_function_changed)
        self.bridge.busy.connect(self._on_busy)
        self.bridge.exit_request.connect(self.close)
        self.log_bridge = LogBridge()
        self.log_view = LogView(limit=self.cfg.output_log_limit)
        self.log_view.on_line = self._log_to_file
        self.log_bridge.message.connect(self.log_view.append)

        self.speaker: Optional[Speaker] = None
        self.announcer: Optional[Announcer] = None

        self._build_ui()
        self._build_menus()
        self._apply_display_colors()
        self._set_controls_enabled(False)

        self.ui_timer = QTimer(self)
        self.ui_timer.timeout.connect(self._refresh_status)
        self.ui_timer.start(1000)
        self.display_timer = QTimer(self)
        self.display_timer.timeout.connect(self._refresh_display)
        self.display_timer.start(50)

        self.log("Click the Config Menu then click Connect.", LogLevel.COMMAND)
        self.log("Supports RS-232 (null modem), AR488 GPIB adapters and VISA GPIB/USB/LAN interfaces.",
                 LogLevel.COMMAND)
        self.log("This software only works with HP Agilent Keysight 34401A.", LogLevel.COMMAND)
        self.resize(1280, 900)

    # ================================================================== logging
    def log(self, message: str, level: LogLevel = LogLevel.PLAIN) -> None:
        """Thread-safe."""
        self.log_bridge(message, level)

    def _log_to_file(self, message: str, level: LogLevel, when: datetime) -> None:
        if self.outlog and not self.outlog.write(message, level, when):
            self.save_log_action.setChecked(False)
            self.log_view.append("Cannot write Output Log to text file. Save Output Log option disabled.",
                                 LogLevel.ERROR)

    # ================================================================== UI construction
    def _build_ui(self) -> None:
        central = QWidget()
        outer = QVBoxLayout(central)
        outer.setContentsMargins(6, 6, 6, 6)
        outer.addWidget(self._build_display())

        mid = QHBoxLayout()
        left = QVBoxLayout()
        left.addWidget(self._build_functions())
        left.addWidget(self._build_ranges())
        left.addWidget(self._build_update_speed())
        left.addStretch(1)
        mid.addLayout(left, 2)
        self.config_tabs = self._build_config_tabs()
        mid.addWidget(self.config_tabs, 3)
        top = QWidget()
        top.setLayout(mid)

        split = QSplitter(Qt.Vertical)
        split.addWidget(top)
        split.addWidget(self.log_view)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        outer.addWidget(split, 1)
        self.setCentralWidget(central)

    def _build_display(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("display")
        v = QVBoxLayout(frame)
        big = QFont("DejaVu Sans Mono")
        big.setStyleHint(QFont.Monospace)
        big.setPointSize(54)
        big.setBold(True)
        unit_font = QFont()
        unit_font.setPointSize(30)
        self.value_label = QLabel("-----")
        self.value_label.setFont(big)
        self.value_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.prefix_label = QLabel("")
        self.prefix_label.setFont(unit_font)
        self.unit_label = QLabel(FUNCTIONS[0].unit)
        self.unit_label.setFont(unit_font)
        self.unit_label.setMinimumWidth(110)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(self.value_label)
        row.addSpacing(8)
        row.addWidget(self.prefix_label, 0, Qt.AlignBottom)
        row.addWidget(self.unit_label, 0, Qt.AlignBottom)
        v.addLayout(row)

        small = QFont()
        small.setPointSize(14)
        self.stat_labels = {}
        row = QHBoxLayout()
        for name in ("MIN", "MAX", "AVG"):
            title = QLabel(name + ":")
            val = QLabel("0")
            unit = QLabel(FUNCTIONS[0].unit)
            for w in (title, val, unit):
                w.setFont(small)
            val.setMinimumWidth(150)
            val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            val.setToolTip("Double-click to reset MIN / MAX / AVG")
            val.mouseDoubleClickEvent = lambda _e: self.reset_stats()  # type: ignore[assignment]
            unit.setMinimumWidth(60)
            self.stat_labels[name] = (title, val, unit)
            row.addWidget(title)
            row.addWidget(val)
            row.addWidget(unit)
            row.addSpacing(30)
        row.addStretch(1)
        reset = QPushButton("Reset MIN/MAX/AVG")
        reset.clicked.connect(self.reset_stats)
        row.addWidget(reset)
        v.addLayout(row)

        self.status_label = QLabel("Not connected")
        self.samples_label = QLabel("Samples: 0")
        self.invalid_label = QLabel("Invalid: 0")
        self.runtime_label = QLabel("Runtime: 00:00:00")
        self.busy_label = QLabel("")
        row = QHBoxLayout()
        for w in (self.status_label, self.samples_label, self.invalid_label, self.runtime_label, self.busy_label):
            row.addWidget(w)
            row.addSpacing(12)
            w.setStyleSheet("color: #9aa;")
        row.addStretch(1)
        v.addLayout(row)
        self.display_frame = frame
        return frame

    def _build_functions(self) -> QGroupBox:
        box = QGroupBox("Measurement")
        grid = QGridLayout(box)
        self.func_group = QButtonGroup(self)
        self.func_group.setExclusive(True)
        labels = ["DCV", "DCI", "ACV", "ACI", "2W Ω", "4W Ω", "Freq", "Period", "Diode", "Cont"]
        for f, label in zip(FUNCTIONS, labels):
            b = _btn(label, lambda f=f: self.select_function(f), checkable=True, tip=f.conf)
            self.func_group.addButton(b, f.index)
            grid.addWidget(b, f.index // 5, f.index % 5)
        self.func_group.button(0).setChecked(True)
        self.functions_box = box
        return box

    def _build_ranges(self) -> QGroupBox:
        box = QGroupBox("Range")
        v = QVBoxLayout(box)
        self.range_row = QHBoxLayout()
        v.addLayout(self.range_row)
        q = QHBoxLayout()
        q.addWidget(_btn("Range?", lambda: self._guarded(range(8), "Range Query only valid for: DCV DCI ACV ACI 2/4 "
                                                         "Wire Ω Freq Period", lambda e: e.query_range())))
        q.addWidget(_btn("Auto Range?", lambda: self._guarded(range(8), "Auto Range Query only valid for: DCV DCI ACV "
                                                              "ACI 2/4 Wire Ω Freq Period",
                                                              lambda e: e.query_auto_range())))
        q.addStretch(1)
        v.addLayout(q)
        self.range_group = QButtonGroup(self)
        self.range_box = box
        self._rebuild_ranges(FUNCTIONS[0])
        return box

    def _rebuild_ranges(self, f: Function) -> None:
        while self.range_row.count():
            item = self.range_row.takeAt(0)
            if item.widget():
                self.range_group.removeButton(item.widget())
                item.widget().deleteLater()
        if not f.ranges:
            lab = QLabel(f"{f.name}: fixed range")
            self.range_row.addWidget(lab)
            return
        for i, opt in enumerate(f.ranges):
            b = _btn(opt.label, lambda o=opt: self._set_range(o), checkable=True, tip=f.range_command(opt))
            self.range_group.addButton(b, i)
            self.range_row.addWidget(b)
            if i == 0:
                b.setChecked(True)
        self.range_row.addStretch(1)

    def _build_update_speed(self) -> QGroupBox:
        box = QGroupBox("Update Speed")
        h = QHBoxLayout(box)
        self.speed_edit = QLineEdit(format_number(self.cfg.update_speed_s))
        self.speed_edit.setMaximumWidth(80)
        h.addWidget(QLabel("Seconds:"))
        h.addWidget(self.speed_edit)
        h.addWidget(_btn("Set", lambda: self._set_speed(parse_float(self.speed_edit.text(), False))))
        h.addWidget(_btn("Default (1 s)", lambda: self._set_speed(1.0)))
        h.addWidget(_btn("Fast (0.01 s)", lambda: self._set_speed(0.01)))
        h.addStretch(1)
        self.speed_box = box
        return box

    def _group(self, title: str, rows: Sequence[Sequence], exclusive_ids: Optional[str] = None) -> QGroupBox:
        box = QGroupBox(title)
        v = QVBoxLayout(box)
        for r in rows:
            v.addLayout(labeled_row(*r, None))
        return box

    def _exclusive(self, buttons: Iterable[QPushButton]) -> List[QPushButton]:
        grp = QButtonGroup(self)
        grp.setExclusive(True)
        out = []
        for b in buttons:
            b.setCheckable(True)
            grp.addButton(b)
            out.append(b)
        self._groups.append(grp)
        return out

    def _build_config_tabs(self) -> QTabWidget:
        self._groups: List[QButtonGroup] = []
        tabs = QTabWidget()
        E = self._cmd  # shorthand: queue a call on the engine

        # ---------------- Measurement config
        w = QWidget()
        v = QVBoxLayout(w)
        self.res_edit = QLineEdit()
        self.res_edit.setPlaceholderText("e.g. 0.00001")
        res_buttons = self._exclusive([
            _btn("Resolution MAX", lambda: self._guarded(range(6), "Resolution only valid for: DCV DCI ACV ACI 2/4 Wire Ω",
                                                         lambda e: e.set_resolution("MAX"))),
            _btn("Resolution MIN", lambda: self._guarded(range(6), "Resolution only valid for: DCV DCI ACV ACI 2/4 Wire Ω",
                                                         lambda e: e.set_resolution("MIN"))),
        ])
        self.res_buttons = res_buttons
        v.addWidget(self._group("Resolution", [
            res_buttons,
            ["Value:", self.res_edit, _btn("Set", self._set_resolution_value),
             _btn("Query", lambda: self._guarded(range(6), "Resolution only valid for: DCV DCI ACV ACI 2/4 Wire Ω",
                                                 lambda e: e.query_resolution()))],
        ]))
        nplc_buttons = self._exclusive(
            _btn(f"{format_number(n)}", lambda n=n: self._guarded((0, 1, 4, 5), "NPLC only valid for: DCV DCI 2/4 Wire Ω",
                                                                  lambda e: e.set_nplc(n))) for n in NPLC_CHOICES)
        self.nplc_buttons = nplc_buttons
        v.addWidget(self._group("Integration time (NPLC)", [
            ["NPLC:", *nplc_buttons, _btn("Query", lambda: self._guarded((0, 1, 4, 5), "NPLC only valid for: DCV DCI "
                                                                         "2/4 Wire Ω", lambda e: e.query_nplc()))]]))
        trig = self._exclusive([_btn("Immediate", lambda: E(lambda e: e.write("TRIGger:SOURce IMMediate"))),
                                _btn("External", lambda: E(lambda e: e.write("TRIGger:SOURce EXTernal")))])
        trig[0].setChecked(True)
        v.addWidget(self._group("Trigger", [
            ["Source:", *trig, _btn("Query", lambda: E(lambda e: e.query("TRIGger:SOURce?")))],
            ["Delay:", _btn("0 s", lambda: E(lambda e: e.write("TRIGger:DELay 0"))),
             _btn("Query", lambda: E(lambda e: e.query("TRIGger:DELay?"))),
             _btn("Auto ON", lambda: E(lambda e: e.write("TRIGger:DELay:AUTO ON"))),
             _btn("Auto?", lambda: E(lambda e: e.query("TRIGger:DELay:AUTO?")))],
        ]))
        v.addStretch(1)
        tabs.addTab(w, "Measure")

        # ---------------- Null / zero / impedance
        w = QWidget()
        v = QVBoxLayout(w)
        self.null_edit = QLineEdit()
        self.null_edit.setPlaceholderText("offset")
        null_msg = "Null Math Function is only available for DCV, DCI, ACV, ACI, 2/4 Wire Ohms, Freq, Period."
        null_btns = self._exclusive([_btn("ON", lambda: self._guarded(range(8), null_msg, lambda e: e.set_null(True))),
                                     _btn("OFF", lambda: self._guarded(range(8), null_msg, lambda e: e.set_null(False)))])
        null_btns[1].setChecked(True)
        v.addWidget(self._group("Null (CALCulate:FUNCtion NULL)", [
            [*null_btns, _btn("Status?", lambda: self._guarded(range(8), null_msg, lambda e: e.query_null()))],
            ["Offset:", self.null_edit, _btn("Set", self._set_null_value),
             _btn("Query", lambda: self._guarded(range(8), null_msg, lambda e: e.query_null_value()))],
        ]))
        az_msg = "Auto Zero only applies to DCV DCI 2W Ω"
        az = self._exclusive([_btn(t, lambda t=t: self._guarded((0, 1, 4), az_msg, lambda e: e.write(f"ZERO:AUTO {t}")))
                              for t in ("ON", "OFF", "ONCE")])
        az[0].setChecked(True)
        v.addWidget(self._group("Auto Zero", [[*az, _btn("Query", lambda: E(lambda e: e.query("ZERO:AUTO?")))]]))
        imp_msg = "Input impedance setting only applies to DCV."
        imp = self._exclusive([
            _btn("Auto ON (>10 GΩ)", lambda: self._guarded((0,), imp_msg, lambda e: e.write("INPut:IMPedance:AUTO ON"))),
            _btn("Auto OFF (10 MΩ)", lambda: self._guarded((0,), imp_msg, lambda e: e.write("INPut:IMPedance:AUTO OFF")))])
        imp[1].setChecked(True)
        v.addWidget(self._group("DC Input Impedance", [
            [*imp, _btn("Query", lambda: E(lambda e: e.query("INPut:IMPedance:AUTO?")))]]))
        v.addStretch(1)
        tabs.addTab(w, "Null / Zero")

        # ---------------- AC / frequency
        w = QWidget()
        v = QVBoxLayout(w)
        fa = self._exclusive(_btn(f"{format_number(a)} s", lambda a=a: self._guarded(
            (6,), "Frequency aperture requires Frequency measurement.",
            lambda e: e.write(f"FREQuency:APERture {format_number(a)}"))) for a in APERTURE_CHOICES)
        pa = self._exclusive(_btn(f"{format_number(a)} s", lambda a=a: self._guarded(
            (7,), "Period aperture requires Period measurement.",
            lambda e: e.write(f"PERiod:APERture {format_number(a)}"))) for a in APERTURE_CHOICES)
        bw = self._exclusive(_btn(f"{b} Hz", lambda b=b: self._guarded(
            (2, 3), "AC filter applies to ACV and ACI only.",
            lambda e: e.write(f"DETector:BANDwidth {b}"))) for b in AC_BANDWIDTH_CHOICES)
        fa[1].setChecked(True)
        pa[1].setChecked(True)
        bw[1].setChecked(True)
        v.addWidget(self._group("Frequency Aperture", [[*fa, _btn("Query", lambda: E(lambda e: e.query("FREQuency:APERture?")))]]))
        v.addWidget(self._group("Period Aperture", [[*pa, _btn("Query", lambda: E(lambda e: e.query("PERiod:APERture?")))]]))
        v.addWidget(self._group("ACV / ACI Detector Bandwidth", [[*bw, _btn("Query", lambda: E(lambda e: e.query("DETector:BANDwidth?")))]]))
        v.addStretch(1)
        tabs.addTab(w, "AC / Freq")

        # ---------------- System
        w = QWidget()
        v = QVBoxLayout(w)
        self.disp_edit = QLineEdit()
        self.disp_edit.setMaxLength(12)
        self.disp_edit.setPlaceholderText("max 12 chars, A-Z 0-9")
        disp = self._exclusive([_btn("ON", lambda: E(lambda e: e.write("DISPlay ON"))),
                                _btn("OFF", lambda: E(lambda e: e.write("DISPlay OFF")))])
        disp[0].setChecked(True)
        v.addWidget(self._group("Front Panel Display", [
            ["VFD:", *disp, _btn("Query", lambda: E(lambda e: e.query("DISPlay?")))],
            ["Text:", self.disp_edit, _btn("Set", self._set_display_text),
             _btn("Clear", lambda: E(lambda e: e.write("DISPlay:TEXT:CLEar")))],
        ]))
        v.addWidget(self._group("System", [
            [_btn("*IDN?", lambda: E(lambda e: e.query("*IDN?"))),
             _btn("Error?", lambda: E(lambda e: e.query("SYSTem:ERRor?"))),
             _btn("Terminals?", lambda: E(lambda e: e.query("ROUTe:TERMinals?"))),
             _btn("*STB?", lambda: E(lambda e: e.query("*STB?"))),
             _btn("SCPI ver?", lambda: E(lambda e: e.query("SYSTem:VERSion?")))],
            [_btn("*CLS", lambda: E(lambda e: e.write("*CLS"))),
             _btn("Beep", lambda: E(lambda e: e.write("SYSTem:BEEPer"))),
             _btn("Function?", lambda: E(lambda e: e.query("FUNCtion?"))),
             _btn("Configure?", lambda: E(lambda e: e.query("CONFigure?")))],
        ]))
        v.addWidget(self._group("Calibration (read only)", [[
            _btn("Count?", lambda: E(lambda e: e.query("CALibration:COUNt?"))),
            _btn("Secure state?", lambda: E(lambda e: e.query("CALibration:SECure:STATe?"))),
            _btn("Message?", lambda: E(lambda e: e.query("CALibration:STRing?"))),
            _btn("Value?", lambda: E(lambda e: e.query("CALibration:VALue?")))]]))
        self.scpi_edit = QLineEdit()
        self.scpi_edit.setPlaceholderText("raw SCPI, e.g. VOLT:DC:NPLC?")
        self.scpi_edit.returnPressed.connect(self._send_raw)
        v.addWidget(self._group("Send SCPI command", [[self.scpi_edit, _btn("Send", self._send_raw)]]))
        v.addStretch(1)
        tabs.addTab(w, "System")

        # ---------------- Speech
        w = QWidget()
        v = QVBoxLayout(w)
        self.speech_cont = QLineEdit()
        self.speech_cont.setPlaceholderText("minutes")
        self.speech_min = QLineEdit()
        self.speech_max = QLineEdit()
        self.speech_state = QLabel("Speech is disabled (Speech menu).")
        v.addWidget(self.speech_state)
        v.addWidget(self._group("Voice measurement", [
            ["Every", self.speech_cont, "minutes", _btn("Set", self._speech_set_continuous),
             _btn("Clear", lambda: self._speech_clear("cont"))]]))
        v.addWidget(self._group("Threshold warnings", [
            ["MIN ≤", self.speech_min, _btn("Set", lambda: self._speech_set_threshold("min")),
             _btn("Clear", lambda: self._speech_clear("min"))],
            ["MAX ≥", self.speech_max, _btn("Set", lambda: self._speech_set_threshold("max")),
             _btn("Clear", lambda: self._speech_clear("max"))]]))
        v.addStretch(1)
        tabs.addTab(w, "Speech")
        return tabs

    # ================================================================== menus
    def _build_menus(self) -> None:
        mb = self.menuBar()
        c = mb.addMenu("Config")
        self.connect_action = add_action(c, "Connect…", self.open_connect, "Ctrl+K")
        self.sampling_action = add_toggle(c, "Sample Measurements", True, self._toggle_sampling)
        self.sampling_only_action = add_toggle(c, "Sampling Only (fast, no commands)", False, self._toggle_sampling_only)
        self.local_exit_action = add_action(c, "Local Exit (return to front panel and quit)", self._local_exit)
        c.addSeparator()
        self.show_io_errors_action = add_toggle(c, "Show communication errors", True, self._set_show_io_errors)
        add_toggle(c, "Show display errors", False, lambda on: setattr(self, "show_display_errors", on))
        c.addSeparator()
        add_action(c, "Exit", self.close, "Ctrl+Q")
        for a in (self.sampling_action, self.sampling_only_action, self.local_exit_action):
            a.setEnabled(False)

        d = mb.addMenu("Data Logger")
        add_action(d, "Open Log Data Folder", self._open_folder)
        self.save_log_action = add_toggle(d, "Save Output Log", True, self._toggle_save_log)
        self.save_meas_action = add_toggle(d, "Save Measurements", True, self._toggle_save_meas)
        add_choice_menu(d, "Save Measurements Interval",
                        [(f"{s} s" if s < 60 else f"{s // 60} min", s) for s in SAVE_INTERVAL_CHOICES],
                        self.cfg.save_interval_s, self._set_save_interval)
        d.addSeparator()
        add_action(d, "Save Output Log As…", self._save_log_as)
        add_action(d, "Clear Output Log", self.log_view.clear)
        add_choice_menu(d, "Auto Clear Output Log", [("20", 20), ("40", 40), ("60", 60), ("200", 200), ("1000", 1000),
                                                     ("Never", 0)], self.cfg.output_log_limit, self._set_log_limit)
        self.logger_menu = d

        g = mb.addMenu("Graph")
        add_action(g, "Show Graph (sample #)", lambda: self.open_graph(False), "Ctrl+G")
        add_action(g, "Show Date Time Graph", lambda: self.open_graph(True), "Ctrl+D")
        add_action(g, "N Sample Graph…", self.open_nsample)
        self.add_graph_action = add_toggle(g, "Add Data to Graphs", True, lambda on: setattr(self, "add_to_graphs", on))
        add_action(g, "Open Saved Data File…", self.open_saved_file)

        t = mb.addMenu("Table")
        add_action(t, "Show Measurement Table", self.open_table, "Ctrl+T")
        add_toggle(t, "Add Data to Table", True, lambda on: setattr(self, "add_to_table", on))

        s = mb.addMenu("Speech")
        self.speech_action = add_toggle(s, "Enable Speech Synthesizer", False, self._toggle_speech)
        add_choice_menu(s, "Voice", [("Male", "male"), ("Female", "female")], "male",
                        lambda g_: self.speaker and self.speaker.configure(gender=g_))
        add_choice_menu(s, "Speed", [(k, k) for k in RATE_WPM], "Medium",
                        lambda r: self.speaker and self.speaker.configure(rate=r))
        add_choice_menu(s, "Volume", [(f"{v}%", v) for v in range(10, 101, 10)], 100,
                        lambda v: self.speaker and self.speaker.configure(volume=v))
        add_choice_menu(s, "Precision (decimals)", [(str(i), i) for i in range(8)], 1, self._set_speech_precision)
        self._speech_precision = 1

        disp = mb.addMenu("Display")
        add_choice_menu(disp, "Measurement Prefix",
                        [("Partial SI (m, k, M, G)", True), ("Full SI / engineering (p … T)", False)],
                        self.partial_si, self._set_prefix_mode)
        for label, key in (("Measurement Colour", "measurement"), ("MIN Colour", "minimum"),
                           ("MAX Colour", "maximum"), ("AVG Colour", "average"), ("Background", "background")):
            add_choice_menu(disp, label, list(COLOR_PRESETS.items()), None,
                            lambda rgb, k=key: self._set_display_color(k, rgb))
        add_action(disp, "Randomize Display Colours", self._randomize_colors)
        disp.addSeparator()
        add_toggle(disp, "Calculate AVG", True, self._set_calc_avg)
        add_choice_menu(disp, "AVG Resolution", [(str(i), i) for i in range(2, 7)], self.avg_resolution,
                        self._set_avg_res)
        add_choice_menu(disp, "AVG Factor", [(str(i), i) for i in (50, 100, 200, 400, 800, 1000)], self.stats.factor,
                        self._set_avg_factor)

        h = mb.addMenu("Help")
        add_action(h, "Device Support", lambda: self.log(
            "Connect via RS-232 (USB-serial + null modem), an AR488 GPIB adapter, or any VISA interface.",
            LogLevel.PLAIN))
        add_action(h, "About / Credits", self._about)

    # ================================================================== connection
    def open_connect(self) -> None:
        if self.engine is not None:
            QMessageBox.information(self, "Connected", "Already connected. Restart the program to change connection.")
            return
        dlg = ConnectDialog(self.cfg.connection, self.cfg.log_base, self)
        if dlg.exec() != ConnectDialog.Accepted or dlg.transport is None:
            return
        self.cfg.connection = dlg.settings_to_save()
        self.cfg.log_base = dlg.log_base()
        self.cfg.save()
        self.attach(dlg.transport, dlg.log_root, dlg.idn)

    def attach(self, transport: Transport, log_root: Optional[Path], idn: str = "") -> None:
        """Start acquisition on an already-connected transport."""
        self.transport = transport
        self.setWindowTitle(f"HP 34401A {transport.name}")
        if log_root is not None:
            self.log_root = log_root
            self.outlog = OutputLogFile(log_root, transport.file_tag)
            self.outlog.enabled = self.save_log_action.isChecked()
            self.datalogger = DataLogger(log_root, transport.file_tag, self.cfg.save_interval_s, log=self.log)
            self.datalogger.enabled = self.save_meas_action.isChecked()
            self.datalogger.start()
        else:
            self.log_root = None
        if idn:
            self.log(f"Device ID: {idn}", LogLevel.SUCCESS)
        eng = AcquisitionEngine(transport, log=self.log, interval_s=self.cfg.update_speed_s)
        eng.on_busy = self.bridge.busy.emit
        eng.on_exit_request = self.bridge.exit_request.emit
        eng.add_sample_listener(self.bridge.sample.emit)
        eng.add_function_change_listener(self.bridge.function_changed.emit)
        if self.datalogger:
            eng.add_sample_listener(self.datalogger)
        self.engine = eng
        eng.start()
        self.status_label.setText(f"Connected: {transport.name}")
        self.connect_action.setEnabled(False)
        for a in (self.sampling_action, self.sampling_only_action, self.local_exit_action):
            a.setEnabled(True)
        self._set_controls_enabled(True)
        if self.log_root:
            self.log(f"Test Data will be saved here: {self.log_root}", LogLevel.COMMAND)
        self.select_function(FUNCTIONS[0])

    # ================================================================== engine commands
    def _cmd(self, fn: Callable[[AcquisitionEngine], None]) -> None:
        if self.engine is None:
            self.log("Not connected.", LogLevel.WARNING)
            return
        try:
            fn(self.engine)
        except ValueError as e:
            self.log(str(e), LogLevel.WARNING)

    def _guarded(self, allowed: Iterable[int], message: str, fn: Callable[[AcquisitionEngine], None]) -> None:
        if self.engine is None:
            self.log("Not connected.", LogLevel.WARNING)
            return
        if self.engine.function.index not in tuple(allowed):
            self.log(message, LogLevel.WARNING)
            return
        self._cmd(fn)

    def select_function(self, f: Function) -> None:
        self.func_group.button(f.index).setChecked(True)
        self._rebuild_ranges(f)
        for b in self.res_buttons:
            b.setChecked(False)
        self._set_group_checked(self.nplc_buttons, NPLC_CHOICES.index(10) if f.supports_nplc else None)
        self._cmd(lambda e: e.select_function(f))

    def _on_function_changed(self, f: Function) -> None:
        """The meter has been reconfigured (I/O thread event): reset display, stats and graphs."""
        for _t, _v, unit in self.stat_labels.values():
            unit.setText(f.unit)
        self.unit_label.setText(f.unit)
        self.value_label.setText("")
        self.prefix_label.setText("")
        self.stats.request_reset()
        self.log("Reset MIN, MAX, AVG values.", LogLevel.SUCCESS)
        self.latest = None
        self._last_sample = None
        for w in self.graphs:
            w.reset(f.graph_unit, f.graph_label)
        for w in self.nsample_graphs:
            w.reset(f.graph_unit, f.graph_label)

    @staticmethod
    def _set_group_checked(buttons: Sequence[QPushButton], idx: Optional[int]) -> None:
        grp = buttons[0].group() if buttons else None
        if grp is not None:
            grp.setExclusive(False)
        for i, b in enumerate(buttons):
            b.setChecked(i == idx)
        if grp is not None:
            grp.setExclusive(True)

    def _set_range(self, opt) -> None:
        self._cmd(lambda e: e.set_range(opt))

    def _set_resolution_value(self) -> None:
        v = parse_float(self.res_edit.text(), allow_negative=False)
        if v is None or v <= 0:
            self.log("Set Resolution Value must be a number greater than 0.", LogLevel.WARNING)
            return
        self._guarded(range(6), "Resolution only valid for: DCV DCI ACV ACI 2/4 Wire Ω", lambda e: e.set_resolution(v))

    def _set_null_value(self) -> None:
        v = parse_float(self.null_edit.text())
        if v is None:
            self.log("Set Null Value must be a real number. Try again.", LogLevel.WARNING)
            return
        self._guarded(range(8), "Null Math Function is only available for DCV, DCI, ACV, ACI, 2/4 Wire Ohms, Freq, "
                                "Period.", lambda e: e.set_null_value(v))

    def _set_display_text(self) -> None:
        self._cmd(lambda e: e.set_display_text(self.disp_edit.text()))

    def _send_raw(self) -> None:
        cmd = self.scpi_edit.text().strip()
        if not cmd:
            return
        if cmd.endswith("?"):
            self._cmd(lambda e: e.query(cmd))
        else:
            self._cmd(lambda e: e.write(cmd))
        self.log(f"Sent: {cmd}", LogLevel.COMMAND)

    def _set_speed(self, v: Optional[float]) -> None:
        if v is None or v <= 0:
            self.log("Update Speed must be a number greater than 0. Minimum value can be 0.01 seconds.", LogLevel.ERROR)
            return
        self.cfg.update_speed_s = v
        self.speed_edit.setText(format_number(v))
        self._cmd(lambda e: e.set_interval(v))

    def _toggle_sampling(self, on: bool) -> None:
        self._cmd(lambda e: e.set_sampling(on))

    def _toggle_sampling_only(self, on: bool) -> None:
        self._cmd(lambda e: e.set_sampling_only(on))
        self._set_controls_enabled(not on)
        self.local_exit_action.setEnabled(not on)

    def _set_show_io_errors(self, on: bool) -> None:
        if self.engine:
            self.engine.show_read_errors = on

    def _local_exit(self) -> None:
        if QMessageBox.question(self, "Local Exit", "Return the meter to front-panel (local) control and quit?") \
                == QMessageBox.Yes:
            self._cmd(lambda e: e.local_exit())

    def _set_controls_enabled(self, on: bool) -> None:
        for w in (self.functions_box, self.range_box, self.config_tabs, self.speed_box):
            w.setEnabled(on)
        if on:
            self.config_tabs.setTabEnabled(4, True)

    def _on_busy(self, busy: bool) -> None:
        self.busy_label.setText("Sending commands…" if busy else "")
        if not (self.engine and self.engine.sampling_only):
            for w in (self.functions_box, self.range_box, self.config_tabs):
                w.setEnabled(not busy)

    # ================================================================== samples & display
    def _on_sample(self, s: Sample) -> None:
        self._last_sample = s
        self.stats.update(s.value)
        self.latest = s.value
        self._display_dirty = True
        if self.add_to_graphs:
            for w in self.graphs:
                w.add_sample(s)
            for w in self.nsample_graphs:
                w.add_sample(s)
        if self.add_to_table:
            for t in self.tables:
                t.add_sample(s)

    def _refresh_display(self) -> None:
        if not self._display_dirty or self._last_sample is None:
            return
        self._display_dirty = False
        s = self._last_sample
        try:
            if is_overload(s.value):
                self.value_label.setText("OPEN" if s.value > 0 and s.function.index in (8, 9) else "OVLD")
                self.prefix_label.setText("")
            else:
                text, prefix = partial_si(s.value) if self.partial_si else full_si(s.raw, s.value)
                self.value_label.setText(text)
                self.prefix_label.setText(prefix)
            st = self.stats
            for name, value, digits in (("MIN", st.min, None), ("MAX", st.max, None),
                                        ("AVG", st.avg if st.calculate_avg else None, self.avg_resolution)):
                _t, val, unit = self.stat_labels[name]
                if value is None:
                    continue
                text, prefix = scale_stat(value, digits)
                val.setText(text)
                unit.setText(f"{prefix}{s.function.unit}")
        except Exception as e:  # pragma: no cover - defensive
            if self.show_display_errors:
                self.log(f"Sample display process failed: {e}", LogLevel.WARNING)

    def _refresh_status(self) -> None:
        if self.engine is None:
            return
        self.samples_label.setText(f"Samples: {self.engine.total_samples}")
        self.invalid_label.setText(f"Invalid: {self.engine.invalid_samples}")
        if self.engine.sampling:
            span = datetime.now() - self.engine.start_time
            secs = int(span.total_seconds())
            self.runtime_label.setText(f"Runtime: {secs // 3600:02d}:{secs // 60 % 60:02d}:{secs % 60:02d}")

    def reset_stats(self) -> None:
        self.stats.request_reset()
        self.log("Reset MIN, MAX, AVG command has been send.", LogLevel.PLAIN)

    # ================================================================== display options
    def _apply_display_colors(self) -> None:
        d = self.cfg.display
        self.display_frame.setStyleSheet(
            f"QFrame#display {{ background-color: {rgb_css(d.background)}; border-radius: 6px; }}")
        for w in (self.value_label, self.prefix_label, self.unit_label):
            w.setStyleSheet(f"color: {rgb_css(d.measurement)};")
        for name, key in (("MIN", "minimum"), ("MAX", "maximum"), ("AVG", "average")):
            for w in self.stat_labels[name]:
                w.setStyleSheet(f"color: {rgb_css(getattr(d, key))};")
        for w in (self.status_label, self.samples_label, self.invalid_label, self.runtime_label, self.busy_label):
            w.setStyleSheet("color: #8a9a9a;")

    def _set_display_color(self, key: str, rgb) -> None:
        setattr(self.cfg.display, key, tuple(rgb))
        self._apply_display_colors()

    def _randomize_colors(self) -> None:
        for key in ("measurement", "minimum", "maximum", "average"):
            setattr(self.cfg.display, key, (random.randint(60, 255), random.randint(60, 255), random.randint(60, 255)))
        self._apply_display_colors()

    def _set_prefix_mode(self, partial: bool) -> None:
        self.partial_si = partial
        self.cfg.display.partial_si = partial
        self._display_dirty = True

    def _set_calc_avg(self, on: bool) -> None:
        self.stats.calculate_avg = on
        self.log("Average will be calculated." if on else "Average will not be calculated.",
                 LogLevel.SUCCESS if on else LogLevel.WARNING)

    def _set_avg_res(self, n: int) -> None:
        self.avg_resolution = n
        self.cfg.avg_resolution = n
        self._display_dirty = True

    def _set_avg_factor(self, n: int) -> None:
        self.stats.factor = n
        self.cfg.avg_factor = n
        self.log(f"AVG factor set to {n}.", LogLevel.SUCCESS)

    # ================================================================== data logger options
    def _open_folder(self) -> None:
        root = getattr(self, "log_root", None)
        if not root:
            self.log("Not connected: no log folder yet.", LogLevel.WARNING)
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(root)))

    def _toggle_save_log(self, on: bool) -> None:
        if self.outlog:
            self.outlog.enabled = on
        self.log("Output Log entries will be saved to a text file." if on else "Output Log entries will not be saved.",
                 LogLevel.SUCCESS if on else LogLevel.WARNING)

    def _toggle_save_meas(self, on: bool) -> None:
        if self.datalogger:
            self.datalogger.enabled = on
        self.log("Measurement data will be saved." if on else "Measurement data will not be saved.",
                 LogLevel.SUCCESS if on else LogLevel.WARNING)

    def _set_save_interval(self, s: int) -> None:
        self.cfg.save_interval_s = s
        if self.datalogger:
            self.datalogger.set_interval(s)
        self.log(f"Save Measurement Interval set to {s} seconds.", LogLevel.SUCCESS)

    def _set_log_limit(self, n: int) -> None:
        self.log_view.limit = n
        self.cfg.output_log_limit = n

    def _save_log_as(self) -> None:
        p = save_file_dialog(self, "Save Output Log", f"Output Log_{datetime.now():%Y-%m-%d %H-%M-%S}.txt", "Text (*.txt)")
        if p:
            p.write_text(self.log_view.plain_text(), encoding="utf-8")

    # ================================================================== child windows
    def _owner(self) -> str:
        return f"HP 34401A {self.transport.name}" if self.transport else "HP 34401A"

    def _current_function(self) -> Function:
        return self.engine.function if self.engine else FUNCTIONS[0]

    def open_graph(self, datetime_axis: bool) -> None:
        f = self._current_function()
        kind = "Date Time Graph" if datetime_axis else "Graphing Module"
        w = WaveformWindow(f"{self._owner()} {kind}", f.graph_unit, f.graph_label, datetime_axis=datetime_axis,
                           live=True, owner=self._owner())
        self._track(w, self.graphs)
        self.log(f"HP34401A {kind} has been opened.", LogLevel.SUCCESS)

    def open_nsample(self) -> None:
        n, ok = QInputDialog.getInt(self, "N Sample Graph", "Samples per waveform (N ≥ 10):", 100, 10, 10_000_000)
        if not ok:
            return
        f = self._current_function()
        w = NSampleWindow(n, f.graph_unit, f.graph_label, self._owner())
        self._track(w, self.nsample_graphs)

    def open_table(self) -> None:
        w = TableWindow(self._owner())
        self._track(w, self.tables)

    def _track(self, w, registry: list) -> None:
        registry.append(w)
        w.closed.connect(lambda obj, reg=registry: reg.remove(obj) if obj in reg else None)
        w.show()

    def open_saved_file(self) -> None:
        """Re-plot a measurement file written by this program or by the original."""
        from PySide6.QtWidgets import QFileDialog

        from ..formatting import parse_timestamp

        start = str(getattr(self, "log_root", "") or "")
        path, _ = QFileDialog.getOpenFileName(self, "Open measurement file", start, "Data (*.txt *.csv);;All (*)")
        if not path:
            return
        import numpy as np

        vals, times = [], []
        bad = 0
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                parts = line.strip().split(",")
                if len(parts) < 2:
                    continue
                try:
                    v = float(parts[-1] if len(parts) == 2 else parts[2] if len(parts) >= 4 else parts[1])
                    ts = parse_timestamp(parts[0] if len(parts) <= 3 else parts[1]).timestamp()
                except ValueError:
                    bad += 1
                    continue
                if is_overload(v):
                    bad += 1
                    continue
                vals.append(v)
                times.append(ts)
        if not vals:
            self.log(f"No readable samples in {path}", LogLevel.ERROR)
            return
        name = Path(path).name
        w = WaveformWindow(f"{name}", "", name, datetime_axis=True, live=False, values=np.array(vals),
                           times=np.array(times), owner=name)
        self._track(w, [])
        self.log(f"Loaded {len(vals)} samples from {name}" + (f" ({bad} skipped)" if bad else ""), LogLevel.SUCCESS)

    # ================================================================== speech
    def _ensure_speech(self) -> bool:
        if self.speaker is None:
            self.speaker = Speaker(log=self.log)
            self.announcer = Announcer(self.speaker, self.stats, self._current_function, lambda: self.latest)
            self.announcer.precision = self._speech_precision
        if not self.speaker.available:
            self.log(f"Speech synthesizer unavailable ({self.speaker.error}). Install pyttsx3 "
                     "(and espeak-ng on Linux).", LogLevel.ERROR)
            return False
        return True

    def _toggle_speech(self, on: bool) -> None:
        if on and not self._ensure_speech():
            self.speech_action.setChecked(False)
            return
        if self.announcer:
            self.announcer.enabled = on
        self.speech_state.setText("Speech is enabled." if on else "Speech is disabled (Speech menu).")
        self.log("Speech Synthesizer enabled." if on else "Speech Synthesizer disabled.",
                 LogLevel.SUCCESS if on else LogLevel.WARNING)

    def _set_speech_precision(self, n: int) -> None:
        self._speech_precision = n
        if self.announcer:
            self.announcer.precision = n

    def _speech_ready(self) -> bool:
        if not (self.announcer and self.announcer.enabled):
            self.log("The Speech Synthesizer is not enabled. Enable it from Speech Menu.", LogLevel.WARNING)
            return False
        return True

    def _speech_set_continuous(self) -> None:
        if not self._speech_ready():
            return
        v = parse_float(self.speech_cont.text(), allow_negative=False)
        if not v:
            self.log("Continuous voice value must be a positive number.", LogLevel.ERROR)
            return
        self.announcer.set_continuous(v)
        self.log(f"Continuously voice measurement every {format_number(v)} minutes.", LogLevel.SUCCESS)

    def _speech_set_threshold(self, which: str) -> None:
        if not self._speech_ready():
            return
        edit = self.speech_min if which == "min" else self.speech_max
        v = parse_float(edit.text())
        if v is None:
            self.log(f"{which.upper()} value must be a real number.", LogLevel.ERROR)
            return
        setattr(self.announcer, f"{which}_threshold", v)
        self.log(f"Speech {which.upper()} warning set to {format_number(v)}.", LogLevel.SUCCESS)

    def _speech_clear(self, which: str) -> None:
        if not self.announcer:
            return
        if which == "cont":
            self.announcer.set_continuous(None)
        else:
            setattr(self.announcer, f"{which}_threshold", None)
        self.log("Speech setting cleared.", LogLevel.SUCCESS)

    # ================================================================== misc
    def _about(self) -> None:
        QMessageBox.about(self, "About", (
            f"<b>HP 34401A Control &amp; DAQ (Python)</b> v{__version__}<br><br>"
            "Python port of <i>HP Agilent Keysight 34401A Control and Data Logging Software</i> "
            "by Nirav Patel (MIT licence).<br>"
            "Plotting: pyqtgraph · GUI: Qt for Python (PySide6) · I/O: pyserial / PyVISA."))

    def closeEvent(self, ev) -> None:  # noqa: N802
        for reg in (self.graphs, self.nsample_graphs, self.tables):
            for w in list(reg):
                w.close()
        if self.engine:
            self.engine.stop()
        if self.datalogger:
            self.datalogger.stop()
        if self.transport:
            try:
                self.transport.close()
            except Exception:
                pass
        if self.announcer:
            self.announcer.stop()
        if self.speaker:
            self.speaker.shutdown()
        self.cfg.save()
        super().closeEvent(ev)
