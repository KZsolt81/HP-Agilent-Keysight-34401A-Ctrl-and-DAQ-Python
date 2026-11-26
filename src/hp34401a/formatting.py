"""Number formatting for the front-panel style display, MIN/MAX/AVG and log files."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Optional, Tuple

from .scpi import OVERLOAD_STAT_VALUE, format_number

# C# format "yyyy-MM-dd h:mm:ss.fff tt" is used for every timestamp the original wrote to disk.
# We keep it byte-for-byte so old and new log files can be mixed and re-plotted.
TIMESTAMP_FORMAT_DESC = "yyyy-MM-dd h:mm:ss.fff tt"


def format_timestamp(dt: datetime, millis: bool = True) -> str:
    hour = dt.hour % 12 or 12
    ampm = "AM" if dt.hour < 12 else "PM"
    base = f"{dt:%Y-%m-%d} {hour}:{dt:%M:%S}"
    if millis:
        base += f".{dt.microsecond // 1000:03d}"
    return f"{base} {ampm}"


def parse_timestamp(text: str) -> datetime:
    """Parse ``yyyy-MM-dd h:mm:ss.fff tt`` (also accepts ISO 8601 as a fallback)."""
    text = text.strip()
    for fmt in ("%Y-%m-%d %I:%M:%S.%f %p", "%Y-%m-%d %I:%M:%S %p"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    return datetime.fromisoformat(text)


def format_log_time(dt: datetime) -> str:
    """Output-log timestamp: ``yyyy-MM-dd h:mm:ss tt``."""
    return format_timestamp(dt, millis=False)


def _round(value: float, digits: Optional[int]) -> float:
    return value if digits is None else round(value, digits)


def partial_si(value: float) -> Tuple[str, str]:
    """"Partial SI prefix" main display mode (default in the original).

    Values with ``|v| < 1`` are shown in milli, ``> 999`` in k, ``> 999999`` in M,
    ``> 999999999`` in G.  The original only tested the positive side for k/M/G;
    here the magnitude is used so negative readings scale the same way.
    """
    a = abs(value)
    if value == 0:
        return "0", ""
    if a < 1:
        return format_number(value * 1e3), "m"
    if a > 999_999_999:
        return format_number(value / 1e9), "G"
    if a > 999_999:
        return format_number(value / 1e6), "M"
    if a > 999:
        return format_number(value / 1e3), "k"
    return format_number(value), ""


_ENG_PREFIX = {-12: "p", -9: "n", -6: "μ", -3: "m", 0: "", 3: "k", 6: "M", 9: "G", 12: "T"}


def full_si(raw: str, value: float) -> Tuple[str, str]:
    """"Full SI prefix" mode: engineering notation (exponent is a multiple of 3).

    The mantissa keeps the digits the meter sent (the raw string is used rather than
    the float to avoid introducing binary rounding noise).
    """
    if value == 0:
        return "0", ""
    try:
        exp = int(raw.strip().upper().split("E")[1])
    except (IndexError, ValueError):
        exp = int(math.floor(math.log10(abs(value))))
    eng = int(math.floor(exp / 3) * 3)
    eng = max(-12, min(12, eng))
    from decimal import Decimal

    try:
        mant = Decimal(raw.strip()).scaleb(-eng)
        text = format(mant.normalize(), "f")
    except Exception:  # pragma: no cover - defensive
        text = format_number(value / 10 ** eng)
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text, _ENG_PREFIX[eng]


def scale_stat(value: float, digits: Optional[int] = None) -> Tuple[str, str]:
    """Formatting used for the MIN / MAX / AVG fields (``updateMIN``/``updateMAX``/``updateAVG``)."""
    a = abs(value)
    if value == 0:
        return format_number(0), ""
    if a < 1e-6:
        return format_number(_round(value * 1e9, digits)), "n"
    if a < 1:
        return format_number(_round(value * 1e3, digits)), "m"
    if a > 99_999_999_999 or a >= OVERLOAD_STAT_VALUE:
        return "OVLD", ""
    if a > 999_999_999:
        return format_number(_round(value / 1e9, digits)), "G"
    if a > 999_999:
        return format_number(_round(value / 1e6, digits)), "M"
    if a > 999:
        return format_number(_round(value / 1e3, digits)), "k"
    return format_number(_round(value, digits)), ""


def speech_number(value: float, precision: int) -> str:
    return format_number(round(value, precision))
