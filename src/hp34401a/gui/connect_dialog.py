"""Connection window (port of COM_Select_Window / GPIB_Select_Window)."""

from __future__ import annotations

from dataclasses import asdict, fields
from pathlib import Path
from typing import Any, Dict, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog,
                               QFormLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QSpinBox, QTabWidget,
                               QVBoxLayout, QWidget)

from ..datalog import LOG_DIR_NAME, default_log_base, prepare_log_directory
from ..engine import LogLevel
from ..transport import (HANDSHAKE_CHOICES, PARITY_CHOICES, STOPBIT_CHOICES, AR488Settings, AR488Transport,
                         SerialSettings, SerialTransport, SimulatedTransport, Transport, TransportError,
                         VisaSettings, VisaTransport, list_visa_resources)
from .common import LogView

BAUD_RATES = (300, 600, 1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200, 230400)


def list_serial_ports():
    try:
        from serial.tools import list_ports

        return [(p.device, p.description) for p in sorted(list_ports.comports(), key=lambda p: p.device)]
    except Exception:
        return []


class _SerialForm(QWidget):
    """Port parameters shared by the RS-232 and AR488 tabs."""

    def __init__(self, defaults: SerialSettings, show_gpib: bool = False):
        super().__init__()
        form = QFormLayout(self)
        self.port = QComboBox(editable=True)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh_ports)
        row = QHBoxLayout()
        row.addWidget(self.port, 1)
        row.addWidget(refresh)
        form.addRow("Port:", row)
        self.baud = QComboBox(editable=True)
        self.baud.addItems([str(b) for b in BAUD_RATES])
        self.databits = QComboBox()
        self.databits.addItems(["8", "7"])
        self.parity = QComboBox()
        self.parity.addItems(list(PARITY_CHOICES))
        self.stopbits = QComboBox()
        self.stopbits.addItems(list(STOPBIT_CHOICES))
        self.handshake = QComboBox()
        self.handshake.addItems(list(HANDSHAKE_CHOICES))
        self.wto = QSpinBox(maximum=600000, singleStep=500, suffix=" ms")
        self.rto = QSpinBox(maximum=600000, singleStep=500, suffix=" ms")
        self.rts = QCheckBox("RTS")
        self.dtr = QCheckBox("DTR")
        form.addRow("Baud rate:", self.baud)
        form.addRow("Data bits:", self.databits)
        form.addRow("Parity:", self.parity)
        form.addRow("Stop bits:", self.stopbits)
        form.addRow("Handshake:", self.handshake)
        form.addRow("Write timeout:", self.wto)
        form.addRow("Read timeout:", self.rto)
        lines = QHBoxLayout()
        lines.addWidget(self.rts)
        lines.addWidget(self.dtr)
        lines.addStretch(1)
        form.addRow("Assert:", lines)
        self.gpib = QSpinBox(minimum=0, maximum=30)
        self.boot = QDoubleSpinBox(maximum=10, singleStep=0.5, suffix=" s")
        if show_gpib:
            form.addRow("GPIB address:", self.gpib)
            form.addRow("Wait after open:", self.boot)
        self.refresh_ports()
        self.load(defaults)

    def refresh_ports(self) -> None:
        current = self.port.currentText()
        self.port.clear()
        for dev, desc in list_serial_ports():
            self.port.addItem(dev, dev)
            self.port.setItemData(self.port.count() - 1, desc, Qt.ToolTipRole)
        if current:
            self.port.setCurrentText(current)

    def load(self, s: SerialSettings) -> None:
        if s.port:
            self.port.setCurrentText(s.port)
        self.baud.setCurrentText(str(s.baudrate))
        self.databits.setCurrentText(str(s.bytesize))
        self.parity.setCurrentText(s.parity)
        self.stopbits.setCurrentText(str(s.stopbits))
        self.handshake.setCurrentText(s.handshake)
        self.wto.setValue(int(s.write_timeout_ms))
        self.rto.setValue(int(s.read_timeout_ms))
        self.rts.setChecked(bool(s.rts))
        self.dtr.setChecked(bool(s.dtr))
        if isinstance(s, AR488Settings):
            self.gpib.setValue(int(s.gpib_address))
            self.boot.setValue(float(s.boot_delay_s))

    def values(self) -> Dict[str, Any]:
        d = dict(port=self.port.currentText().strip(), baudrate=int(self.baud.currentText()),
                 bytesize=int(self.databits.currentText()), parity=self.parity.currentText(),
                 stopbits=self.stopbits.currentText(), handshake=self.handshake.currentText(),
                 write_timeout_ms=self.wto.value(), read_timeout_ms=self.rto.value(), rts=self.rts.isChecked(),
                 dtr=self.dtr.isChecked())
        return d


def _from_dict(cls, data: Dict[str, Any]):
    names = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in (data or {}).items() if k in names})


class ConnectDialog(QDialog):
    def __init__(self, saved: Dict[str, Any], log_base: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("Connect to HP 34401A")
        self.transport: Optional[Transport] = None
        self.idn = ""
        self.log_root: Optional[Path] = None
        self.saved = saved or {}
        self.tabs = QTabWidget()

        self.serial_form = _SerialForm(_from_dict(SerialSettings, self.saved.get("serial", {})))
        serial_tab = QWidget()
        v = QVBoxLayout(serial_tab)
        v.addWidget(QLabel("34401A rear RS-232 port. Needs a USB-serial adapter and a null-modem cable/adapter.\n"
                           "Meter defaults: 9600 baud, 8 data bits, no parity (I/O menu on the front panel)."))
        v.addWidget(self.serial_form)
        self.tabs.addTab(serial_tab, "RS-232")

        ar = _from_dict(AR488Settings, self.saved.get("ar488", {}))
        self.ar_form = _SerialForm(ar, show_gpib=True)
        ar_tab = QWidget()
        v = QVBoxLayout(ar_tab)
        v.addWidget(QLabel("GPIB through an AR488 Arduino adapter (controller mode)."))
        v.addWidget(self.ar_form)
        row = QHBoxLayout()
        for label, fn in (("++ver", self._ar_ver), ("++addr?", self._ar_addr), ("++rst", self._ar_rst)):
            b = QPushButton(label)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch(1)
        v.addLayout(row)
        self.tabs.addTab(ar_tab, "AR488 GPIB")

        vs = _from_dict(VisaSettings, self.saved.get("visa", {}))
        visa_tab = QWidget()
        form = QFormLayout(visa_tab)
        self.visa_res = QComboBox(editable=True)
        self.visa_res.setCurrentText(vs.resource)
        self.visa_backend = QComboBox(editable=True)
        self.visa_backend.addItems(["", "@py", "@ivi"])
        self.visa_backend.setCurrentText(vs.backend)
        self.visa_lock = QCheckBox("Exclusive lock")
        self.visa_lock.setChecked(vs.exclusive_lock)
        find = QPushButton("Find resources")
        find.clicked.connect(self._find_visa)
        row = QHBoxLayout()
        row.addWidget(self.visa_res, 1)
        row.addWidget(find)
        form.addRow(QLabel("Any VISA interface: Keysight 82357B, NI GPIB-USB-HS, Prologix-ethernet, …\n"
                           "Backend: empty = installed NI/Keysight VISA, '@py' = pyvisa-py."))
        form.addRow("Resource:", row)
        form.addRow("Backend:", self.visa_backend)
        form.addRow("", self.visa_lock)
        self.tabs.addTab(visa_tab, "VISA")

        sim_tab = QWidget()
        v = QVBoxLayout(sim_tab)
        v.addWidget(QLabel("Built-in 34401A simulator — try the software without hardware."))
        self.sim_overload = QDoubleSpinBox(maximum=1.0, singleStep=0.01, decimals=3)
        f = QFormLayout()
        f.addRow("Overload probability:", self.sim_overload)
        v.addLayout(f)
        v.addStretch(1)
        self.tabs.addTab(sim_tab, "Simulator")
        self.tabs.setCurrentIndex(int(self.saved.get("tab", 0)))

        self.folder = QLineEdit(log_base or str(default_log_base()))
        browse = QPushButton("Select…")
        browse.clicked.connect(self._browse)
        frow = QHBoxLayout()
        frow.addWidget(QLabel("Log data folder:"))
        frow.addWidget(self.folder, 1)
        frow.addWidget(browse)

        self.info = LogView(limit=0)
        self.info.setMinimumHeight(140)
        buttons = QDialogButtonBox()
        verify = buttons.addButton("Verify 34401A", QDialogButtonBox.ActionRole)
        buttons.addButton("Connect", QDialogButtonBox.AcceptRole)
        buttons.addButton(QDialogButtonBox.Cancel)
        verify.clicked.connect(self._verify)
        buttons.accepted.connect(self._connect)
        buttons.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addWidget(self.tabs)
        lay.addLayout(frow)
        lay.addWidget(QLabel(f"Data is saved in a '{LOG_DIR_NAME}' sub-folder."))
        lay.addWidget(self.info)
        lay.addWidget(buttons)
        self.resize(640, 720)

    # ------------------------------------------------------------------
    def _browse(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Choose log folder", self.folder.text())
        if d:
            self.folder.setText(d)

    def _make_transport(self) -> Transport:
        tab = self.tabs.currentIndex()
        if tab == 0:
            s = SerialSettings(**self.serial_form.values())
            if not s.port:
                raise TransportError("Select a COM port.")
            return SerialTransport(s)
        if tab == 1:
            s = AR488Settings(**self.ar_form.values(), gpib_address=self.ar_form.gpib.value(),
                              boot_delay_s=self.ar_form.boot.value())
            if not s.port:
                raise TransportError("Select a COM port.")
            return AR488Transport(s)
        if tab == 2:
            return VisaTransport(VisaSettings(resource=self.visa_res.currentText().strip(),
                                              backend=self.visa_backend.currentText().strip(),
                                              exclusive_lock=self.visa_lock.isChecked()))
        return SimulatedTransport(overload_probability=self.sim_overload.value())

    def _busy(self, on: bool) -> None:
        if on:
            QGuiApplication.setOverrideCursor(Qt.WaitCursor)
        else:
            QGuiApplication.restoreOverrideCursor()

    def _with_transport(self, fn) -> None:
        self._busy(True)
        t = None
        try:
            t = self._make_transport()
            t.open()
            fn(t)
        except Exception as e:
            self.info.append(str(e), LogLevel.ERROR)
        finally:
            if t is not None:
                try:
                    t.close()
                except Exception:
                    pass
            self._busy(False)

    def _verify(self) -> None:
        def run(t: Transport) -> None:
            idn = t.query("*IDN?").strip()
            ok = "34401A" in idn
            self.info.append(f"Verify {'successful' if ok else 'failed'}: {idn}",
                             LogLevel.SUCCESS if ok else LogLevel.ERROR)

        self._with_transport(run)

    def _ar_ver(self) -> None:
        self._with_ar(lambda t: self.info.append("AR488: " + t.adapter_query("++ver"), LogLevel.SUCCESS))

    def _ar_addr(self) -> None:
        self._with_ar(lambda t: self.info.append("GPIB address: " + t.adapter_query("++addr"), LogLevel.SUCCESS))

    def _ar_rst(self) -> None:
        self._with_ar(lambda t: (t.adapter_reset(), self.info.append("AR488 reset sent.", LogLevel.SUCCESS)))

    def _with_ar(self, fn) -> None:
        self.tabs.setCurrentIndex(1)
        self._with_transport(fn)

    def _find_visa(self) -> None:
        self._busy(True)
        try:
            res = list_visa_resources(self.visa_backend.currentText().strip())
            self.visa_res.clear()
            self.visa_res.addItems(res)
            self.info.append(f"Found {len(res)} VISA resource(s).", LogLevel.SUCCESS if res else LogLevel.WARNING)
        except Exception as e:
            self.info.append(f"VISA search failed: {e}", LogLevel.ERROR)
        finally:
            self._busy(False)

    def _connect(self) -> None:
        try:
            root = prepare_log_directory(self.folder.text().strip() or default_log_base())
        except OSError as e:
            self.info.append(f"Log Data Directory cannot be created: {e}", LogLevel.ERROR)
            self.info.append("Choose another path by clicking the select button.", LogLevel.ERROR)
            return
        self._busy(True)
        t: Optional[Transport] = None
        try:
            t = self._make_transport()
            t.open()
            self.idn = t.connect_sequence()
        except Exception as e:
            if t is not None:
                try:
                    t.close()
                except Exception:
                    pass
            self.info.append(f"Connect failed: {e}", LogLevel.ERROR)
            return
        finally:
            self._busy(False)
        self.transport = t
        self.log_root = root
        self.info.append(f"Connected: {self.idn}", LogLevel.SUCCESS)
        self.accept()

    def settings_to_save(self) -> Dict[str, Any]:
        ar = dict(self.ar_form.values(), gpib_address=self.ar_form.gpib.value(), boot_delay_s=self.ar_form.boot.value())
        return {
            "tab": self.tabs.currentIndex(),
            "serial": self.serial_form.values(),
            "ar488": ar,
            "visa": asdict(VisaSettings(resource=self.visa_res.currentText().strip(),
                                        backend=self.visa_backend.currentText().strip(),
                                        exclusive_lock=self.visa_lock.isChecked())),
        }

    def log_base(self) -> str:
        return self.folder.text().strip()
