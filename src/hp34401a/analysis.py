"""Statistics, math waveforms and histograms used by the graph windows.

Statistics follow MathNet.Numerics ``ArrayStatistics`` semantics, which the
original used: ``Variance``/``StandardDeviation`` are the *unbiased sample*
estimators (N-1), ``Population*`` use N.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------


def _arr(data: Sequence[float]) -> np.ndarray:
    a = np.asarray(data, dtype=float)
    if a.size == 0:
        raise ValueError("No samples.")
    return a


def geometric_mean(a: np.ndarray) -> float:
    with np.errstate(divide="ignore", invalid="ignore"):
        return float(np.exp(np.mean(np.log(a))))


def harmonic_mean(a: np.ndarray) -> float:
    with np.errstate(divide="ignore"):
        return float(a.size / np.sum(1.0 / a))


def _sample_var(a: np.ndarray) -> float:
    return float(np.var(a, ddof=1)) if a.size > 1 else float("nan")


STATISTICS: Dict[str, Callable[[np.ndarray], "float | Tuple[float, float]"]] = {
    "Mean (Average)": lambda a: float(np.mean(a)),
    "Std Deviation": lambda a: math.sqrt(_sample_var(a)),
    "Max Sample": lambda a: float(np.max(a)),
    "Min Sample": lambda a: float(np.min(a)),
    "|Max| Sample": lambda a: float(np.max(np.abs(a))),
    "|Min| Sample": lambda a: float(np.min(np.abs(a))),
    "Root Mean Square": lambda a: float(np.sqrt(np.mean(a * a))),
    "Variance": _sample_var,
    "Geometric Mean": geometric_mean,
    "Harmonic Mean": harmonic_mean,
    "Pop Variance": lambda a: float(np.var(a)),
    "Pop Std Deviation": lambda a: float(np.std(a)),
    "Mean Std Deviation": lambda a: (float(np.mean(a)), math.sqrt(_sample_var(a))),
    "Mean Variance": lambda a: (float(np.mean(a)), _sample_var(a)),
}


def compute_statistic(name: str, data: Sequence[float]):
    return STATISTICS[name](_arr(data))


def describe_statistic(name: str, data: Sequence[float], unit: str, start: int, end: int, all_samples: bool) -> str:
    result = compute_statistic(name, data)
    scope = f"[All Samples ({start}, {end})]" if all_samples else f"[N Samples ({start}, {end})]"
    if isinstance(result, tuple):
        what = "Std Deviation" if "Std" in name else "Variance"
        return (f"{scope}  Arithmetic Sample Mean: {result[0]} {unit}  "
                f"Unbiased Sample {what}: {result[1]} {unit}")
    return f"{scope}  Calculated {name}: {result} {unit}"


# ---------------------------------------------------------------------------
# Math waveforms
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MathOp:
    key: str
    label: str
    params: Tuple[str, ...]  # names of numeric parameters
    angle: bool = False  # accepts degree/radian option


MATH_OPS: Tuple[MathOp, ...] = (
    MathOp("add", "Samples + Value", ("Value",)),
    MathOp("sub", "Samples - Value", ("Value",)),
    MathOp("rsub", "Value - Samples", ("Value",)),
    MathOp("mul", "Samples * Value", ("Value",)),
    MathOp("div", "Samples / Value", ("Value",)),
    MathOp("rdiv", "Value / Samples", ("Value",)),
    MathOp("pct_err", "% Error = |(Samples - Y) / Y| x 100", ("Y",)),
    MathOp("db", "dB = A x log_B(|Samples| / C)", ("A", "B", "C")),
    MathOp("dbm", "dBm = A x log_B((Samples^2 / R) / P)", ("A", "B", "R", "P")),
    MathOp("value_pow", "(Value)^(Samples)", ("Value",)),
    MathOp("pow_value", "(Samples)^(Value)", ("Value",)),
    MathOp("log10", "log10(Samples)", ()),
    MathOp("ln", "ln(Samples)", ()),
    MathOp("sqrt", "√(Samples)", ()),
    MathOp("abs", "|Samples|", ()),
    MathOp("sin", "sin(Samples)", (), True),
    MathOp("cos", "cos(Samples)", (), True),
    MathOp("tan", "tan(Samples)", (), True),
    MathOp("asin", "asin(Samples)", (), True),
    MathOp("acos", "acos(Samples)", (), True),
    MathOp("atan", "atan(Samples)", (), True),
    MathOp("sinh", "sinh(Samples)", ()),
    MathOp("cosh", "cosh(Samples)", ()),
    MathOp("tanh", "tanh(Samples)", ()),
    MathOp("ema", "Exponential moving average (factor N)", ("N",)),
)
MATH_OPS_BY_KEY = {op.key: op for op in MATH_OPS}


def _log_base(x: np.ndarray, base: float) -> np.ndarray:
    return np.log(x) / math.log(base)


def apply_math(key: str, data: Sequence[float], params: Sequence[float] = (), degrees: bool = False) -> np.ndarray:
    """Apply a math operation. NaN/±inf results are replaced with 0, as in the original.

    Angle handling: with ``degrees=True`` the forward trig functions interpret samples as
    degrees and the inverse functions return degrees. (The original multiplied the *result*
    of sin/cos/tan by 180/π, which is mathematically wrong; this port fixes it.)
    """
    x = np.array(data, dtype=float, copy=True)
    p = [float(v) for v in params]
    op = MATH_OPS_BY_KEY[key]
    if len(p) != len(op.params):
        raise ValueError(f"{op.label} needs {len(op.params)} parameter(s)")
    if key in ("div", "pct_err") and p[0] == 0:
        raise ValueError("Cannot divide by 0.")
    if key == "db" and (p[1] <= 0 or p[1] == 1 or p[2] <= 0):
        raise ValueError("The base and the argument of the logarithm must be positive (base ≠ 1).")
    if key == "dbm" and (p[1] <= 0 or p[1] == 1 or p[2] <= 0 or p[3] <= 0):
        raise ValueError("The base and the argument of the logarithm must be positive (base ≠ 1).")
    if key == "ema" and p[0] < 1:
        raise ValueError("EMA factor must be ≥ 1.")
    rad = np.deg2rad if degrees else (lambda v: v)
    deg = np.rad2deg if degrees else (lambda v: v)
    with np.errstate(all="ignore"):
        fns: Dict[str, Callable[[], np.ndarray]] = {
            "add": lambda: x + p[0],
            "sub": lambda: x - p[0],
            "rsub": lambda: p[0] - x,
            "mul": lambda: x * p[0],
            "div": lambda: x / p[0],
            "rdiv": lambda: p[0] / x,
            "pct_err": lambda: np.abs((x - p[0]) / p[0]) * 100.0,
            "db": lambda: p[0] * _log_base(np.abs(x) / p[2], p[1]),
            "dbm": lambda: p[0] * _log_base((x ** 2 / p[2]) / p[3], p[1]),
            "value_pow": lambda: np.power(p[0], x),
            "pow_value": lambda: np.power(x, p[0]),
            "log10": lambda: np.log10(x),
            "ln": lambda: np.log(x),
            "sqrt": lambda: np.sqrt(x),
            "abs": lambda: np.abs(x),
            "sin": lambda: np.sin(rad(x)),
            "cos": lambda: np.cos(rad(x)),
            "tan": lambda: np.tan(rad(x)),
            "asin": lambda: deg(np.arcsin(x)),
            "acos": lambda: deg(np.arccos(x)),
            "atan": lambda: deg(np.arctan(x)),
            "sinh": lambda: np.sinh(x),
            "cosh": lambda: np.cosh(x),
            "tanh": lambda: np.tanh(x),
            "ema": lambda: running_average(x, int(p[0])),
        }
        y = np.asarray(fns[key](), dtype=float)
    y[~np.isfinite(y)] = 0.0
    return y


def math_title(key: str, params: Sequence[float], degrees: bool = False) -> str:
    from .scpi import format_number

    op = MATH_OPS_BY_KEY[key]
    label = op.label
    for name, val in zip(op.params, params):
        label += f"  {name}={format_number(val)}"
    if op.angle:
        label += " [deg]" if degrees else " [rad]"
    return label


def running_average(data: Sequence[float], factor: int) -> np.ndarray:
    """The original's "moving average": cumulative mean for n ≤ factor, then EMA(1/factor)."""
    out = np.empty(len(data), dtype=float)
    avg = 0.0
    for i, v in enumerate(data):
        avg += (v - avg) / min(i + 1, factor)
        out[i] = avg
    return out


# ---------------------------------------------------------------------------
# Histogram
# ---------------------------------------------------------------------------


@dataclass
class HistogramResult:
    counts: np.ndarray
    edges: np.ndarray  # len(counts)+1
    bin_size: float

    @property
    def centers(self) -> np.ndarray:
        return (self.edges[:-1] + self.edges[1:]) / 2


def histogram(data: Sequence[float], bin_size: float = 0.0) -> HistogramResult:
    """Histogram with fixed bin width. ``bin_size == 0`` → (max-min)/√N, as in the original."""
    a = _arr(data)
    a = a[np.isfinite(a)]
    if a.size == 0:
        raise ValueError("No finite samples.")
    lo, hi = float(a.min()), float(a.max())
    if bin_size <= 0:
        bin_size = (hi - lo) / math.sqrt(a.size)
    if bin_size <= 0 or not math.isfinite(bin_size):
        bin_size = abs(lo) * 1e-6 or 1e-12  # all samples identical
    n_bins = max(1, int(math.floor((hi - lo) / bin_size)) + 1)
    if n_bins > 1_000_000:
        raise ValueError("Bin size is too small for the data range (more than 1,000,000 bins).")
    edges = lo + bin_size * np.arange(n_bins + 1)
    counts, _ = np.histogram(a, bins=edges)
    return HistogramResult(counts.astype(float), edges, bin_size)


def time_difference(t_start, t_end, unit: str = "s") -> float:
    seconds = (t_end - t_start).total_seconds()
    return {"s": seconds, "min": seconds / 60.0, "h": seconds / 3600.0}[unit]


def validate_range(start: int, end: int, count: int, what: str = "N Samples") -> Optional[str]:
    if start < 0 or end < 0:
        return f"{what} Start/End must be non-negative integers."
    if start >= end:
        return f"{what} Start Value must be less than End Value."
    if end >= count:
        return f"{what} End Value must be less than the number of samples captured ({count})."
    return None


def parse_rgb(values: Sequence[str]) -> Optional[Tuple[int, int, int]]:
    try:
        rgb = tuple(int(v) for v in values)
    except ValueError:
        return None
    if len(rgb) != 3 or any(c < 0 or c > 255 for c in rgb):
        return None
    return rgb  # type: ignore[return-value]


__all__: List[str] = [
    "STATISTICS", "compute_statistic", "describe_statistic", "MATH_OPS", "MATH_OPS_BY_KEY", "apply_math",
    "math_title", "running_average", "histogram", "HistogramResult", "time_difference", "validate_range", "parse_rgb",
]
