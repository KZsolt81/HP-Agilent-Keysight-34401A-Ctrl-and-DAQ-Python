"""SCPI command tables and response interpretation for the HP/Agilent/Keysight 34401A.

Every command string here is taken verbatim from the original C# application
(MainWindow.xaml.cs, ``Serial_Queue_Command_Process`` and the button handlers),
so the instrument sees exactly the same traffic as with the Windows software.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional, Sequence, Tuple

#: Value the 34401A returns on overload (positive) / negative overload.
OVERLOAD_POS = "+9.90000000E+37"
OVERLOAD_NEG = "-9.90000000E+37"
OVERLOAD_THRESHOLD = 9.0e37

#: Substitute values the original used for MIN/MAX/AVG when an overload is read.
OVERLOAD_STAT_VALUE = 1.0e11

#: A valid READ? response is always ``±d.ddddddddE±dd`` (15 characters).
_READING_RE = re.compile(r"^[+-]\d\.\d{8}E[+-]\d{2}$")


@dataclass(frozen=True)
class RangeOption:
    label: str
    value: Optional[str]  # None means AUTO


@dataclass(frozen=True)
class Function:
    """One measurement function of the 34401A."""

    index: int
    key: str  # file/log key, e.g. "VDC"
    name: str  # human name used in logs, e.g. "DCV"
    conf: str  # CONFigure command
    prefix: str  # SCPI subsystem prefix used for RESolution / NPLCycles
    unit: str  # unit shown on the main display
    graph_unit: str  # unit used by graph windows
    graph_label: str  # Y-axis label used by graph windows
    speech: str  # word(s) used by the speech synthesizer
    folder: str  # log sub-folder name
    range_cmd: Optional[str] = None  # e.g. "VOLTage:DC:RANGe"
    ranges: Tuple[RangeOption, ...] = field(default_factory=tuple)
    supports_nplc: bool = False
    supports_resolution: bool = False
    supports_null: bool = False
    default_nplc: Optional[float] = None  # what *RST/CONF leaves selected

    # --- command builders -------------------------------------------------
    def range_command(self, option: RangeOption) -> str:
        if self.range_cmd is None:
            raise ValueError(f"{self.name} has no selectable range")
        if option.value is None:
            return f"{self.range_cmd}:AUTO ON"
        return f"{self.range_cmd} {option.value}"

    def range_query(self) -> str:
        if self.range_cmd is None:
            raise ValueError(f"{self.name} has no selectable range")
        return f"{self.range_cmd}?"

    def auto_range_query(self) -> str:
        if self.range_cmd is None:
            raise ValueError(f"{self.name} has no selectable range")
        return f"{self.range_cmd}:AUTO?"

    def nplc_command(self, nplc: float) -> str:
        if not self.supports_nplc:
            raise ValueError(f"NPLC is not available for {self.name}")
        return f"{self.prefix}NPLCycles {format_number(nplc)}"

    def nplc_query(self) -> str:
        if not self.supports_nplc:
            raise ValueError(f"NPLC is not available for {self.name}")
        return f"{self.prefix}NPLCycles?"

    def resolution_command(self, value: "float | str") -> str:
        if not self.supports_resolution:
            raise ValueError(f"Resolution is not available for {self.name}")
        if isinstance(value, str):
            arg = value.upper()  # MIN / MAX
        else:
            arg = format_number(value)
        return f"{self.prefix}RESolution {arg}"

    def resolution_query(self) -> str:
        if not self.supports_resolution:
            raise ValueError(f"Resolution is not available for {self.name}")
        return f"{self.prefix}RESolution?"


def _r(*pairs: Tuple[str, Optional[str]]) -> Tuple[RangeOption, ...]:
    return tuple(RangeOption(label, value) for label, value in pairs)


_V_RANGES = _r(("Auto", None), ("100 mV", "0.1"), ("1 V", "1"), ("10 V", "10"), ("100 V", "100"), ("1000 V", "1000"))
_ADC_RANGES = _r(("Auto", None), ("10 mA", "0.01"), ("100 mA", "0.1"), ("1 A", "1"), ("3 A", "3"))
_AAC_RANGES = _r(("Auto", None), ("1 A", "1"), ("3 A", "3"))
_OHM_RANGES = _r(
    ("Auto", None),
    ("100 Ω", "100"),
    ("1 kΩ", "1E+3"),
    ("10 kΩ", "10E+3"),
    ("100 kΩ", "100E+3"),
    ("1 MΩ", "1E+6"),
    ("10 MΩ", "10E+6"),
    ("100 MΩ", "100E+6"),
)

FUNCTIONS: Tuple[Function, ...] = (
    Function(0, "VDC", "DCV", "CONF:VOLT:DC DEF,DEF", "VOLTage:DC:", "VDC", "VDC", "VDC Voltage",
             "volts DC", "VDC", "VOLTage:DC:RANGe", _V_RANGES, True, True, True, 10),
    Function(1, "ADC", "DCI", "CONF:CURR:DC DEF,DEF", "CURRent:DC:", "ADC", "ADC", "ADC Current",
             "amps DC", "ADC", "CURRent:DC:RANGe", _ADC_RANGES, True, True, True, 10),
    Function(2, "VAC", "ACV", "CONF:VOLT:AC DEF,DEF", "VOLTage:AC:", "VAC", "VAC", "VAC Voltage",
             "volts AC", "VAC", "VOLTage:AC:RANGe", _V_RANGES, False, True, True),
    Function(3, "AAC", "ACI", "CONF:CURR:AC DEF,DEF", "CURRent:AC:", "AAC", "AAC", "AAC Current",
             "amps AC", "AAC", "CURRent:AC:RANGe", _AAC_RANGES, False, True, True),
    Function(4, "2WireOhms", "2W Ω", "CONF:RES DEF,DEF", "RESistance:", "Ω", "Ω", "Ω 2Wire Ohms",
             "ohms", "2WireOhms", "RESistance:RANGe", _OHM_RANGES, True, True, True, 10),
    Function(5, "4WireOhms", "4W Ω", "CONF:FRES DEF,DEF", "FRESistance:", "Ω", "Ω", "Ω 4Wire Ohms",
             "ohms", "4WireOhms", "FRESistance:RANGe", _OHM_RANGES, True, True, True, 10),
    Function(6, "FREQ", "Frequency", "CONF:FREQ DEF,DEF", "FREQuency:", "Hz", "Hz", "Hz Frequency",
             "hertz", "FREQ", "FREQuency:VOLTage:RANGe", _V_RANGES, False, False, True),
    Function(7, "PER", "Period", "CONF:PER DEF,DEF", "PERiod:", "SEC", "s", "T Period",
             "seconds", "PER", "PERiod:VOLTage:RANGe", _V_RANGES, False, False, True),
    Function(8, "DIODE", "Diode", "CONF:DIOD", "", "VDC", "VDC", "VDC Voltage Diode",
             "volts DC", "DIODE"),
    Function(9, "CONTINUITY", "Continuity", "CONF:CONT", "", "Ω", "Ω", "Ω Ohms Continuity",
             "ohms", "CONTINUITY"),
)

FUNCTIONS_BY_KEY = {f.key: f for f in FUNCTIONS}
CONF_COMMANDS = frozenset(f.conf for f in FUNCTIONS)
LOG_FOLDERS = tuple(f.folder for f in FUNCTIONS)

NPLC_CHOICES: Sequence[float] = (0.02, 0.2, 1, 10, 100)
APERTURE_CHOICES: Sequence[float] = (0.01, 0.1, 1)
AC_BANDWIDTH_CHOICES: Sequence[int] = (3, 20, 200)


def get_function(key_or_index: "str | int") -> Function:
    if isinstance(key_or_index, int):
        return FUNCTIONS[key_or_index]
    key = key_or_index.strip()
    if key in FUNCTIONS_BY_KEY:
        return FUNCTIONS_BY_KEY[key]
    aliases = {
        "DCV": 0, "VDC": 0, "DCI": 1, "ADC": 1, "ACV": 2, "VAC": 2, "ACI": 3, "AAC": 3,
        "OHM": 4, "OHM2": 4, "RES": 4, "2W": 4, "OHM4": 5, "FRES": 5, "4W": 5,
        "FREQ": 6, "PER": 7, "PERIOD": 7, "DIODE": 8, "DIOD": 8, "CONT": 9, "CONTINUITY": 9,
    }
    try:
        return FUNCTIONS[aliases[key.upper()]]
    except KeyError:
        raise ValueError(f"Unknown measurement function: {key_or_index!r}") from None


def format_number(value: float) -> str:
    """Format a number like C#'s ``(decimal)double`` ToString: no exponent, no trailing zeros."""
    from decimal import Decimal

    d = Decimal(f"{float(value):.15g}")
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    if s in ("-0", ""):
        s = "0"
    return s


def is_valid_reading(text: str) -> bool:
    return bool(_READING_RE.match(text))


def is_overload(text_or_value: "str | float") -> bool:
    if isinstance(text_or_value, str):
        try:
            text_or_value = float(text_or_value)
        except ValueError:
            return False
    return abs(text_or_value) >= OVERLOAD_THRESHOLD


def display_text_command(message: str) -> str:
    """``DISPlay:TEXT 'msg'`` — the original only allowed alphanumerics (max 12 shown by the meter)."""
    message = message.strip()
    if not re.fullmatch(r"[A-Za-z0-9]*", message):
        raise ValueError("Front panel display text must be alphanumeric.")
    return f"DISPlay:TEXT '{message}'"


# ---------------------------------------------------------------------------
# Response interpretation (port of the big switch in Serial_Queue_Command_Process)
# ---------------------------------------------------------------------------

_FUNCTION_NAMES = {
    '"VOLT"': "DCV measurement is selected.",
    '"CURR"': "DCI measurement is selected.",
    '"VOLT:AC"': "ACV measurement is selected.",
    '"CURR:AC"': "ACI measurement is selected.",
    '"RES"': "2 Wire Ω measurement is selected.",
    '"FRES"': "4 Wire Ω measurement is selected.",
    '"FREQ"': "Frequency measurement is selected.",
    '"PER"': "Period measurement is selected.",
    '"DIOD"': "Diode measurement is selected.",
    '"CONT"': "Continuity measurement is selected.",
}


def _as_float(text: str) -> Optional[float]:
    try:
        return float(text)
    except ValueError:
        return None


def _on_off(text: str, on_msg: str, off_msg: str, other_prefix: str) -> str:
    v = _as_float(text)
    if v == 1:
        return on_msg
    if v == 0:
        return off_msg
    return f"{other_prefix}{text}"


def describe_response(command: str, response: str) -> str:
    """Turn a query and its raw answer into the human-readable log line the original printed."""
    r = response.strip()
    cmd = command.strip()
    v = _as_float(r)
    simple = {
        "TRIGger:DELay?": "Trigger Delay Value: ",
        "CALibration:COUNt?": "Calibration Count: ",
        "CALibration:SECure:STATe?": "Calibration Secure State: ",
        "CALibration:STRing?": "Calibration Stored Message: ",
        "CALibration:VALue?": "Calibration Value: ",
        "SYSTem:ERRor?": "Error: ",
        "ROUTe:TERMinals?": "Input Terminal: ",
        "*IDN?": "Device ID: ",
        "*STB?": "STB: ",
        "SYSTem:VERSion?": "SCPI Version: ",
        "TRIGger:SOURce?": "Trigger is currently set to ",
    }
    if cmd in simple:
        return simple[cmd] + r
    if cmd == "TRIGger:DELay:AUTO?":
        return _on_off(r, "Trigger Auto Delay is ON", "Trigger Auto Delay is OFF", "Trigger Auto Delay: ")
    if cmd == "DISPlay?":
        return _on_off(r, "VFD Display is ON", "VFD Display is OFF", "VFD Display: ")
    if cmd == "INPut:IMPedance:AUTO?":
        if v == 1:
            return "Auto Input Impedance is ON. Input Impedance is >10G Ω for DC 100mV 1V 10V"
        if v == 0:
            return "Auto Input Impedance is OFF. Input Impedance is 10M Ω for all DC Ranges."
        return "DC Input Impedance: " + r
    if cmd == "DETector:BANDwidth?":
        return "ACV ACI Bandwidth: " + (format_number(v) + "Hz" if v is not None else r)
    if cmd == "PERiod:APERture?":
        return "Period Aperture: " + (format_number(v) if v is not None else r)
    if cmd == "FREQuency:APERture?":
        return "Frequency Aperture: " + (format_number(v) if v is not None else r)
    if cmd == "ZERO:AUTO?":
        return _on_off(r, "Auto Zero is ON", "Auto Zero is OFF", "Auto Zero: ")
    if cmd == "FUNCtion?":
        return _FUNCTION_NAMES.get(r, f"{r} measurement is selected.")
    if cmd == "CONFigure?":
        return r
    if cmd == "CALCulate:NULL:OFFSet?":
        return "Query Null Set Value: " + (format_number(v) if v is not None else r)
    if cmd.endswith(":RANGe:AUTO?"):
        return _on_off(r, f"{cmd.upper()} is ON", f"{cmd.upper()} is OFF", f"{cmd.upper()} ")
    if cmd.endswith(":RANGe?"):
        return f"{cmd.upper()} {r}"
    if cmd.endswith("RESolution?") or cmd.endswith("NPLCycles?"):
        return f"{cmd}  " + (format_number(v) if v is not None else r)
    return f"{cmd} -> {r}"


def describe_null_state(function_response: str, state_response: str) -> str:
    fn = function_response.strip()
    st = state_response.strip()
    v = _as_float(st)
    if v == 0:
        return f"{fn} function is not active."
    if v == 1:
        return f"{fn} function is active."
    return f"Math Function: {fn} State: {st}"
