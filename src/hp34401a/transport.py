"""Physical links to the 34401A.

Three transports mirror the three editions of the original Windows software:

* :class:`SerialTransport`  – the meter's own RS-232 port (USB-serial adapter + null modem).
* :class:`AR488Transport`   – GPIB through an AR488 Arduino adapter (Prologix-style ``++`` commands).
* :class:`VisaTransport`    – any VISA GPIB/USB/LAN interface via PyVISA (NI-VISA, Keysight IO
  Libraries, or the pure-python ``pyvisa-py`` backend).

A fourth, :class:`SimulatedTransport`, emulates the meter so the software can be
tried and tested without hardware.
"""

from __future__ import annotations

import math
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional


class TransportError(IOError):
    """Raised for any communication failure."""


class Transport:
    """Minimal line-oriented SCPI link."""

    #: Human readable identifier used in window titles and log-file names (e.g. ``COM3``).
    name: str = "?"
    #: Pause after every queued front-panel command (the RS-232 edition used 1.2 s).
    command_settle_s: float = 0.0
    #: Pause between a CONFigure command and the first READ? (original: 0.5 s).
    conf_settle_s: float = 0.5

    def open(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def close(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def write(self, command: str) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def read(self) -> str:  # pragma: no cover - interface
        raise NotImplementedError

    def query(self, command: str) -> str:
        self.write(command)
        return self.read()

    # --- lifecycle helpers -------------------------------------------------
    def connect_sequence(self) -> str:
        """Verify the instrument is a 34401A, reset it and put it in remote. Returns ``*IDN?``."""
        idn = self.query("*IDN?").strip()
        if "34401A" not in idn:
            raise TransportError(f"Connected device is not a 34401A (IDN: {idn!r})")
        self.write("*RST")
        return idn

    def go_to_local(self) -> None:
        """Return the front panel to the user (``Local Exit`` in the original)."""

    def recover(self) -> None:
        """Called after a failed read. Default: nothing."""

    @property
    def file_tag(self) -> str:
        """Safe version of :attr:`name` for use in file names."""
        return "".join(c for c in self.name if c.isalnum() or c in "-_.") or "meter"

    def __enter__(self) -> "Transport":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# ---------------------------------------------------------------------------
# RS-232
# ---------------------------------------------------------------------------

PARITY_CHOICES = {"None": "N", "Even": "E", "Odd": "O", "Mark": "M", "Space": "S"}
STOPBIT_CHOICES = {"1": 1, "1.5": 1.5, "2": 2}
HANDSHAKE_CHOICES = ("None", "XOnXOff", "RequestToSend", "RequestToSendXOnXOff")


@dataclass
class SerialSettings:
    port: str = ""
    baudrate: int = 9600
    bytesize: int = 8
    parity: str = "None"
    stopbits: str = "1"
    handshake: str = "None"
    write_timeout_ms: int = 4000
    read_timeout_ms: int = 9000
    rts: bool = False
    dtr: bool = False


def _open_serial(s: SerialSettings):
    try:
        import serial  # pyserial
    except ImportError as e:  # pragma: no cover
        raise TransportError("pyserial is required: pip install pyserial") from e
    ser = serial.Serial()
    ser.port = s.port
    ser.baudrate = int(s.baudrate)
    ser.bytesize = int(s.bytesize)
    ser.parity = PARITY_CHOICES.get(s.parity, s.parity)
    ser.stopbits = STOPBIT_CHOICES.get(str(s.stopbits), 1)
    ser.timeout = s.read_timeout_ms / 1000.0
    ser.write_timeout = s.write_timeout_ms / 1000.0
    ser.xonxoff = s.handshake in ("XOnXOff", "RequestToSendXOnXOff")
    ser.rtscts = s.handshake in ("RequestToSend", "RequestToSendXOnXOff")
    # Set modem lines *before* open (pyserial applies them on open). Keeping DTR low by
    # default matches .NET's SerialPort default and avoids resetting Arduino-based AR488s.
    ser.dtr = bool(s.dtr)
    if not ser.rtscts:
        ser.rts = bool(s.rts)
    try:
        ser.open()
    except Exception as e:
        raise TransportError(f"Cannot open {s.port}: {e}") from e
    return ser


class _SerialLineMixin:
    _ser = None
    _lock: threading.RLock

    def _write_line(self, command: str) -> None:
        if self._ser is None:
            raise TransportError("Port is not open")
        try:
            self._ser.write((command + "\n").encode("ascii", errors="replace"))
            self._ser.flush()
        except Exception as e:
            raise TransportError(f"Write failed: {e}") from e

    def _read_line(self) -> str:
        if self._ser is None:
            raise TransportError("Port is not open")
        try:
            raw = self._ser.readline()
        except Exception as e:
            raise TransportError(f"Read failed: {e}") from e
        if not raw.endswith(b"\n"):
            raise TransportError("Read timed out" + (f" (partial: {raw!r})" if raw else ""))
        return raw.decode("ascii", errors="replace").strip()


class SerialTransport(_SerialLineMixin, Transport):
    """34401A RS-232 interface. Requires a null-modem cable/adapter."""

    command_settle_s = 1.2  # Serial_WriteQueue() slept 1.2 s after every command

    def __init__(self, settings: SerialSettings):
        self.settings = settings
        self.name = settings.port
        self._lock = threading.RLock()

    def open(self) -> None:
        self._ser = _open_serial(self.settings)

    def close(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            finally:
                self._ser = None

    def write(self, command: str) -> None:
        with self._lock:
            self._write_line(command)

    def read(self) -> str:
        with self._lock:
            return self._read_line()

    def query(self, command: str) -> str:
        with self._lock:
            self._write_line(command)
            return self._read_line()

    def connect_sequence(self) -> str:
        idn = super().connect_sequence()
        self.write("SYSTem:REMote")
        return idn

    def go_to_local(self) -> None:
        self.write("SYSTem:LOCal")

    def recover(self) -> None:
        if self._ser is not None:
            try:
                self._ser.reset_input_buffer()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# AR488 Arduino GPIB adapter
# ---------------------------------------------------------------------------


@dataclass
class AR488Settings(SerialSettings):
    baudrate: int = 115200
    gpib_address: int = 22
    #: Seconds to wait after opening the port (an Arduino may reboot when the port opens).
    boot_delay_s: float = 0.0
    #: ``++read_tmo_ms`` sent after connecting (original: 9000).
    read_tmo_ms: int = 9000


class AR488Transport(_SerialLineMixin, Transport):
    """GPIB via an AR488 adapter in controller mode with ``++auto 0``."""

    def __init__(self, settings: AR488Settings):
        self.settings = settings
        self.name = f"{settings.port}_GPIB{settings.gpib_address}"
        self._lock = threading.RLock()

    def open(self) -> None:
        self._ser = _open_serial(self.settings)
        if self.settings.boot_delay_s > 0:
            time.sleep(self.settings.boot_delay_s)
            self._ser.reset_input_buffer()
        with self._lock:
            for cmd in (f"++addr {int(self.settings.gpib_address)}", "++ren 1", "++auto 0", "++eor 2"):
                self._write_line(cmd)

    def close(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            finally:
                self._ser = None

    def write(self, command: str) -> None:
        with self._lock:
            self._write_line(command)

    def read(self) -> str:
        with self._lock:
            self._write_line("++read")
            return self._read_line()

    def query(self, command: str) -> str:
        with self._lock:
            self._write_line(command)
            self._write_line("++read")
            return self._read_line()

    def adapter_query(self, command: str) -> str:
        """Query the adapter itself (``++ver``, ``++addr`` ...)."""
        with self._lock:
            self._write_line(command)
            return self._read_line()

    def adapter_reset(self) -> None:
        with self._lock:
            self._write_line("++rst")

    def connect_sequence(self) -> str:
        idn = super().connect_sequence()
        self.write(f"++read_tmo_ms {int(self.settings.read_tmo_ms)}")
        return idn

    def go_to_local(self) -> None:
        self.write("++ren 0")

    def recover(self) -> None:
        if self._ser is not None:
            try:
                self._ser.reset_input_buffer()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# VISA
# ---------------------------------------------------------------------------


@dataclass
class VisaSettings:
    resource: str = "GPIB0::22::INSTR"
    backend: str = ""  # "" = default (NI/Keysight VISA), "@py" = pyvisa-py
    exclusive_lock: bool = False
    open_timeout_ms: int = 10000
    timeout_ms: int = 15000
    reconnect_delay_s: float = 10.0


def list_visa_resources(backend: str = "", query: str = "?*::INSTR") -> List[str]:
    try:
        import pyvisa
    except ImportError as e:  # pragma: no cover
        raise TransportError("pyvisa is required: pip install pyvisa (and pyvisa-py or NI-VISA)") from e
    rm = pyvisa.ResourceManager(backend) if backend else pyvisa.ResourceManager()
    try:
        return list(rm.list_resources(query))
    finally:
        try:
            rm.close()
        except Exception:
            pass


class VisaTransport(Transport):
    conf_settle_s = 0.5

    def __init__(self, settings: VisaSettings):
        self.settings = settings
        self.name = settings.resource
        self._rm = None
        self._inst = None
        self._lock = threading.RLock()

    @property
    def file_tag(self) -> str:
        return self.settings.resource.replace(":", "")

    def open(self) -> None:
        try:
            import pyvisa
            from pyvisa import constants
        except ImportError as e:  # pragma: no cover
            raise TransportError("pyvisa is required: pip install pyvisa") from e
        try:
            self._rm = pyvisa.ResourceManager(self.settings.backend) if self.settings.backend else pyvisa.ResourceManager()
            mode = constants.AccessModes.exclusive_lock if self.settings.exclusive_lock else constants.AccessModes.no_lock
            self._inst = self._rm.open_resource(
                self.settings.resource, access_mode=mode, open_timeout=self.settings.open_timeout_ms
            )
            self._inst.timeout = self.settings.timeout_ms
            self._inst.read_termination = "\n"
            self._inst.write_termination = "\n"
        except Exception as e:
            self.close()
            raise TransportError(f"Cannot open {self.settings.resource}: {e}") from e

    def close(self) -> None:
        for obj in (self._inst, self._rm):
            if obj is not None:
                try:
                    obj.close()
                except Exception:
                    pass
        self._inst = None
        self._rm = None

    def write(self, command: str) -> None:
        with self._lock:
            if self._inst is None:
                raise TransportError("VISA session is not open")
            try:
                self._inst.write(command)
            except Exception as e:
                raise TransportError(f"VISA write failed: {e}") from e

    def read(self) -> str:
        with self._lock:
            if self._inst is None:
                raise TransportError("VISA session is not open")
            try:
                return self._inst.read().strip()
            except Exception as e:
                raise TransportError(f"VISA read failed: {e}") from e

    def query(self, command: str) -> str:
        with self._lock:
            self.write(command)
            return self.read()

    def go_to_local(self) -> None:
        with self._lock:
            if self._inst is not None:
                try:
                    from pyvisa import constants

                    self._inst.control_ren(constants.RENLineOperation.deassert_gtl)
                except Exception:
                    pass

    def recover(self) -> None:
        """Port of ``GPIB_Reconnect``: drop the session, wait, reopen."""
        with self._lock:
            self.close()
            time.sleep(self.settings.reconnect_delay_s)
            self.open()


# ---------------------------------------------------------------------------
# Simulator
# ---------------------------------------------------------------------------


@dataclass
class _SimState:
    function: str = "VOLT"
    ranges: Dict[str, str] = field(default_factory=dict)
    auto: Dict[str, int] = field(default_factory=dict)
    nplc: Dict[str, float] = field(default_factory=dict)
    resolution: Dict[str, float] = field(default_factory=dict)
    trig_source: str = "IMM"
    trig_delay: float = 0.0
    trig_delay_auto: int = 1
    zero_auto: int = 1
    display: int = 1
    display_text: str = ""
    impedance_auto: int = 0
    bandwidth: float = 20
    freq_aperture: float = 0.1
    per_aperture: float = 0.1
    null_state: int = 0
    null_offset: float = 0.0
    errors: List[str] = field(default_factory=list)


class SimulatedTransport(Transport):
    """A tiny 34401A emulator. Readings are a noisy sine around a per-function nominal value."""

    command_settle_s = 0.0
    conf_settle_s = 0.0

    _FUNC_FROM_CONF = {
        "VOLT:DC": "VOLT", "VOLT": "VOLT", "CURR:DC": "CURR", "CURR": "CURR", "VOLT:AC": "VOLT:AC",
        "CURR:AC": "CURR:AC", "RES": "RES", "FRES": "FRES", "FREQ": "FREQ", "PER": "PER",
        "DIOD": "DIOD", "CONT": "CONT",
    }
    _NOMINAL = {
        "VOLT": 5.00012, "CURR": 0.0123456, "VOLT:AC": 230.123, "CURR:AC": 0.51234, "RES": 4699.87,
        "FRES": 99.98765, "FREQ": 50.0012, "PER": 0.0199995, "DIOD": 0.61234, "CONT": 9.9e37,
    }
    _PREFIX = {
        "VOLTAGE:DC": "VOLT", "VOLT:DC": "VOLT", "CURRENT:DC": "CURR", "CURR:DC": "CURR",
        "VOLTAGE:AC": "VOLT:AC", "VOLT:AC": "VOLT:AC", "CURRENT:AC": "CURR:AC", "CURR:AC": "CURR:AC",
        "RESISTANCE": "RES", "RES": "RES", "FRESISTANCE": "FRES", "FRES": "FRES",
        "FREQUENCY:VOLTAGE": "FREQ", "FREQ:VOLT": "FREQ", "PERIOD:VOLTAGE": "PER", "PER:VOLT": "PER",
        "FREQUENCY": "FREQ", "PERIOD": "PER",
    }

    def __init__(self, name: str = "SIM", noise: float = 2e-5, read_delay_s: Optional[float] = None,
                 overload_probability: float = 0.0, seed: Optional[int] = None):
        self.name = name
        self.noise = noise
        self.read_delay_s = read_delay_s
        self.overload_probability = overload_probability
        self.state = _SimState()
        self._pending: List[str] = []
        self._rng = random.Random(seed)
        self._t0 = time.monotonic()
        self._open = False
        self.history: List[str] = []
        self._lock = threading.RLock()

    def open(self) -> None:
        self._open = True

    def close(self) -> None:
        self._open = False

    def _reading(self) -> str:
        f = self.state.function
        nplc = self.state.nplc.get(f, 10.0)
        delay = self.read_delay_s if self.read_delay_s is not None else min(0.2, nplc / 50.0) + 0.002
        if delay:
            time.sleep(delay)
        if self._rng.random() < self.overload_probability or f == "CONT":
            return "+9.90000000E+37"
        t = time.monotonic() - self._t0
        nominal = self._NOMINAL[f]
        value = nominal * (1 + 0.001 * math.sin(2 * math.pi * t / 20.0)) + nominal * self._rng.gauss(0, self.noise)
        if self.state.null_state:
            value -= self.state.null_offset
        return f"{value:+.8E}"

    def _key(self, prefix: str) -> str:
        return self._PREFIX.get(prefix.upper().rstrip(":"), self.state.function)

    def write(self, command: str) -> None:
        with self._lock:
            if not self._open:
                raise TransportError("Simulator is not open")
            self.history.append(command)
            self._handle(command.strip())

    def read(self) -> str:
        with self._lock:
            if not self._pending:
                raise TransportError("Read timed out (no query pending)")
            return self._pending.pop(0)

    def _reply(self, text: str) -> None:
        self._pending.append(text)

    def _handle(self, cmd: str) -> None:  # noqa: C901 - straightforward dispatch
        u = cmd.upper()
        s = self.state
        if u == "*IDN?":
            return self._reply("HEWLETT-PACKARD,34401A,0,11-5-2")
        if u == "*RST":
            self.state = _SimState()
            return
        if u in ("*CLS", "SYSTEM:REMOTE", "SYST:REM", "SYSTEM:LOCAL", "SYSTEM:BEEPER", "DISPLAY:TEXT:CLEAR"):
            if u == "DISPLAY:TEXT:CLEAR":
                s.display_text = ""
            return
        if u == "*STB?":
            return self._reply("+0")
        if u in ("READ?", "MEAS?"):
            return self._reply(self._reading())
        if u.startswith("CONF:") and not u.endswith("?"):
            key = u[5:].split(" ")[0]
            s.function = self._FUNC_FROM_CONF.get(key, s.function)
            s.auto[s.function] = 1
            return
        if u in ("CONFIGURE?", "CONF?"):
            return self._reply(f'"{s.function} +1.000000E+01,+3.000000E-06"')
        if u in ("FUNCTION?", "FUNC?"):
            return self._reply(f'"{s.function}"')
        if u == "SYSTEM:ERROR?":
            return self._reply(s.errors.pop(0) if s.errors else '+0,"No error"')
        if u == "SYSTEM:VERSION?":
            return self._reply("1992.0")
        if u == "ROUTE:TERMINALS?":
            return self._reply("FRON")
        if u.startswith("CALIBRATION:"):
            return self._reply({"CALIBRATION:COUNT?": "+42", "CALIBRATION:SECURE:STATE?": "1",
                                "CALIBRATION:STRING?": '"SIMULATED"', "CALIBRATION:VALUE?": "+0.00000000E+00"}.get(u, "0"))
        if u.startswith("TRIGGER:SOURCE"):
            if u.endswith("?"):
                return self._reply(s.trig_source)
            s.trig_source = "EXT" if "EXT" in u else "IMM"
            return
        if u == "TRIGGER:DELAY?":
            return self._reply(f"{s.trig_delay:+.8E}")
        if u == "TRIGGER:DELAY:AUTO?":
            return self._reply(str(s.trig_delay_auto))
        if u.startswith("TRIGGER:DELAY:AUTO "):
            s.trig_delay_auto = 1 if u.endswith("ON") else 0
            return
        if u.startswith("TRIGGER:DELAY "):
            s.trig_delay = float(u.split()[-1])
            s.trig_delay_auto = 0
            return
        if u.startswith("ZERO:AUTO"):
            if u.endswith("?"):
                return self._reply(str(s.zero_auto))
            s.zero_auto = 0 if u.endswith("OFF") else 1
            return
        if u.startswith("DISPLAY:TEXT "):
            s.display_text = cmd.split(" ", 1)[1].strip("'\"")
            return
        if u.startswith("DISPLAY"):
            if u.endswith("?"):
                return self._reply(str(s.display))
            s.display = 0 if u.endswith("OFF") else 1
            return
        if u.startswith("INPUT:IMPEDANCE:AUTO"):
            if u.endswith("?"):
                return self._reply(str(s.impedance_auto))
            s.impedance_auto = 1 if u.endswith("ON") else 0
            return
        if u.startswith("DETECTOR:BANDWIDTH"):
            if u.endswith("?"):
                return self._reply(f"{s.bandwidth:+.8E}")
            s.bandwidth = float(u.split()[-1])
            return
        for attr, head in (("freq_aperture", "FREQUENCY:APERTURE"), ("per_aperture", "PERIOD:APERTURE")):
            if u.startswith(head):
                if u.endswith("?"):
                    return self._reply(f"{getattr(s, attr):+.8E}")
                setattr(s, attr, float(u.split()[-1]))
                return
        if u.startswith("CALCULATE:"):
            if u == "CALCULATE:FUNCTION?":
                return self._reply("NULL")
            if u == "CALCULATE:STATE?":
                return self._reply(str(s.null_state))
            if u.startswith("CALCULATE:STATE "):
                s.null_state = 1 if u.endswith("ON") else 0
                return
            if u == "CALCULATE:NULL:OFFSET?":
                return self._reply(f"{s.null_offset:+.8E}")
            if u.startswith("CALCULATE:NULL:OFFSET "):
                s.null_offset = float(u.split()[-1])
                return
            return
        if ":RANGE" in u:
            head, _, tail = u.partition(":RANGE")
            key = self._key(head)
            if tail == ":AUTO?":
                return self._reply(str(s.auto.get(key, 1)))
            if tail.startswith(":AUTO "):
                s.auto[key] = 1 if tail.endswith("ON") else 0
                return
            if tail == "?":
                return self._reply(f"{float(s.ranges.get(key, '10')):+.8E}")
            s.ranges[key] = tail.strip()
            s.auto[key] = 0
            return
        if ":NPLCYCLES" in u:
            head, _, tail = u.partition(":NPLCYCLES")
            key = self._key(head)
            if tail == "?":
                return self._reply(f"{s.nplc.get(key, 10.0):+.8E}")
            s.nplc[key] = float(tail.strip())
            return
        if ":RESOLUTION" in u:
            head, _, tail = u.partition(":RESOLUTION")
            key = self._key(head)
            if tail == "?":
                return self._reply(f"{s.resolution.get(key, 3e-6):+.8E}")
            arg = tail.strip()
            s.resolution[key] = {"MIN": 3e-7, "MAX": 3e-5}.get(arg, None) or float(arg)
            return
        if u.endswith("?"):
            s.errors.append('-113,"Undefined header"')
            return self._reply("0")
        # Unknown write: record an error like the real meter.
        s.errors.append('-113,"Undefined header"')
