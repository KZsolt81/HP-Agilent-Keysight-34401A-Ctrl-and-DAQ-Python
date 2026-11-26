"""Acquisition engine: the multimeter I/O thread.

Port of ``HP34401ACommunicateEvent`` / ``Serial_Queue_Command_Process`` from the
original MainWindow.  All meter traffic happens on one dedicated thread; front-panel
style commands are queued and executed between readings, exactly like the
original's ``SerialWriteQueue``.

The engine is GUI-agnostic: consumers register callbacks (``add_sample_listener``,
``log`` sink, ``on_busy``) and the Qt layer marshals them onto the GUI thread.
"""

from __future__ import annotations

import enum
import queue
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Dict, List, Optional

from . import scpi
from .scpi import FUNCTIONS, Function, RangeOption, describe_null_state, describe_response, format_number
from .transport import Transport, TransportError


class LogLevel(enum.IntEnum):
    """Same codes/colours as ``insert_Log`` in the original."""

    SUCCESS = 0  # green
    ERROR = 1  # red
    WARNING = 2  # orange
    INFO = 3  # blue
    PLAIN = 4  # black
    COMMAND = 5  # violet

    @property
    def tag(self) -> str:
        return {0: "[Success]", 1: "[Error]", 2: "[Warning]"}.get(int(self), "")


LogSink = Callable[[str, LogLevel], None]


@dataclass(frozen=True)
class Sample:
    timestamp: datetime
    raw: str
    value: float
    function: Function

    @property
    def overload(self) -> bool:
        return scpi.is_overload(self.value)


@dataclass
class Action:
    """A queued unit of instrument work, executed on the I/O thread."""

    label: str
    run: Callable[["AcquisitionEngine"], None]


class StatsTracker:
    """MIN / MAX / AVG as computed by ``Display_MIN_MAX_AVG`` & ``updateAVG``.

    The average is ``avg += (x - avg) / min(n, factor)``: a true running mean for the first
    ``factor`` samples, then an exponential moving average with alpha = 1/factor.
    Overloads are counted as ±1e11, like the original.
    """

    def __init__(self, factor: int = 1000):
        self.factor = factor
        self.calculate_avg = True
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self.min: Optional[float] = None
            self.max: Optional[float] = None
            self.avg: float = 0.0
            self.count = 0
            self._pending_reset = False

    def request_reset(self) -> None:
        self._pending_reset = True

    def update(self, value: float) -> None:
        if scpi.is_overload(value):
            value = scpi.OVERLOAD_STAT_VALUE if value > 0 else -scpi.OVERLOAD_STAT_VALUE
        with self._lock:
            if self._pending_reset or self.min is None:
                self.min = self.max = value
                self.avg = 0.0
                self.count = 0
                self._pending_reset = False
            if value < self.min:
                self.min = value
            if value > self.max:
                self.max = value
            if self.calculate_avg:
                self.count += 1
                self.avg += (value - self.avg) / min(self.count, max(1, self.factor))


@dataclass
class EngineState:
    function: Function = FUNCTIONS[0]
    auto_range: Dict[int, bool] = field(default_factory=lambda: {f.index: True for f in FUNCTIONS})
    null_value: float = 0.0


class AcquisitionEngine:
    """Runs the read loop and the command queue on a background thread."""

    def __init__(self, transport: Transport, log: Optional[LogSink] = None, interval_s: float = 1.0,
                 show_read_errors: bool = True):
        self.transport = transport
        self._log_sink = log
        self.interval_s = float(interval_s)
        self.show_read_errors = show_read_errors
        self.state = EngineState()
        self.active_function: Function = self.state.function
        self.sampling = True
        self.sampling_only = False
        self.total_samples = 0
        self.invalid_samples = 0
        self.start_time = datetime.now()
        self._queue: "queue.Queue[Action]" = queue.Queue()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._sample_listeners: List[Callable[[Sample], None]] = []
        self._reset_listeners: List[Callable[[Function], None]] = []
        self.on_busy: Optional[Callable[[bool], None]] = None
        self.on_exit_request: Optional[Callable[[], None]] = None
        self._listeners_lock = threading.Lock()

    # ------------------------------------------------------------------ plumbing
    def log(self, message: str, level: LogLevel = LogLevel.PLAIN) -> None:
        if self._log_sink is not None:
            self._log_sink(message, level)

    def add_sample_listener(self, fn: Callable[[Sample], None]) -> None:
        with self._listeners_lock:
            self._sample_listeners.append(fn)

    def remove_sample_listener(self, fn: Callable[[Sample], None]) -> None:
        with self._listeners_lock:
            if fn in self._sample_listeners:
                self._sample_listeners.remove(fn)

    def add_function_change_listener(self, fn: Callable[[Function], None]) -> None:
        self._reset_listeners.append(fn)

    @property
    def function(self) -> Function:
        return self.state.function

    @property
    def busy(self) -> bool:
        return not self._queue.empty()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self.start_time = datetime.now()
        self._thread = threading.Thread(target=self._run, name="34401A-IO", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None

    def kick(self) -> None:
        """Wake the I/O thread now (e.g. after changing ``sampling`` directly)."""
        self._wake.set()

    def set_interval(self, seconds: float) -> None:
        if seconds <= 0:
            raise ValueError("Update speed must be greater than 0.")
        old = self.interval_s
        self.interval_s = float(seconds)
        self.log(f"Update Speed has been set to {format_number(seconds)} seconds.", LogLevel.SUCCESS)
        if seconds < old:
            self._wake.set()

    def set_sampling(self, enabled: bool) -> None:
        self.sampling = enabled
        if enabled:
            self.start_time = datetime.now()
            self.log("Software is reading measurement data from multimeter.", LogLevel.SUCCESS)
        else:
            self.log("Software will not read measurement data from multimeter.", LogLevel.WARNING)
        self._wake.set()

    def set_sampling_only(self, enabled: bool) -> None:
        self.sampling_only = enabled
        if enabled:
            self.log("Software will now only read measurements from the multimeter.", LogLevel.WARNING)
            self.log("All Write (front panel) operations are disabled.", LogLevel.WARNING)
        else:
            self.log("Software will allow commands to be send to the multimeter.", LogLevel.SUCCESS)
            self.log("Sampling only mode disabled. Returned to normal mode.", LogLevel.SUCCESS)
        self._wake.set()

    def submit(self, action: Action) -> None:
        if self.sampling_only:
            self.log("Sampling only mode is active: command ignored.", LogLevel.WARNING)
            return
        self._queue.put(action)
        if self.on_busy:
            self.on_busy(True)
        self._wake.set()

    # ------------------------------------------------------------------ main loop
    def _run(self) -> None:
        next_read = time.monotonic()
        while not self._stop.is_set():
            try:
                if not self._queue.empty():
                    self._drain_queue()
                    next_read = time.monotonic()
                if self.sampling and time.monotonic() >= next_read:
                    next_read = time.monotonic() + self.interval_s
                    self._read_once()
                    # "Sampling only" = read back-to-back as fast as the meter answers.
                    while self.sampling_only and self.sampling and not self._stop.is_set():
                        self._read_once()
            except TransportError as e:
                self._handle_io_error(e)
            except Exception as e:  # pragma: no cover - last-resort guard
                self.log(f"Unexpected error: {e!r}", LogLevel.ERROR)
            timeout = max(0.0, next_read - time.monotonic()) if self.sampling else 0.25
            self._wake.wait(timeout)
            self._wake.clear()

    def _handle_io_error(self, e: Exception) -> None:
        if self.show_read_errors:
            self.log(str(e), LogLevel.WARNING)
            self.log("Could not get a measurement reading.", LogLevel.WARNING)
            self.log("Don't worry. Trying again.", LogLevel.WARNING)
            self.log("Slow the Update Speed if warning persists.", LogLevel.WARNING)
        try:
            self.transport.recover()
        except Exception as re:  # pragma: no cover
            self.log(f"Reconnect failed: {re}", LogLevel.ERROR)

    def _drain_queue(self) -> None:
        try:
            while True:
                try:
                    action = self._queue.get_nowait()
                except queue.Empty:
                    break
                try:
                    action.run(self)
                except TransportError as e:
                    self.log(f"{action.label} failed: {e}", LogLevel.ERROR)
                    self.transport.recover()
                except Exception as e:
                    self.log(f"{action.label} failed: {e}", LogLevel.ERROR)
                if self.transport.command_settle_s:
                    time.sleep(self.transport.command_settle_s)
        finally:
            if self.on_busy:
                self.on_busy(False)

    def _read_once(self) -> None:
        data = self.transport.query("READ?").strip()
        if scpi.is_valid_reading(data):
            self.total_samples += 1
            sample = Sample(datetime.now(), data, float(data), self.active_function)
            with self._listeners_lock:
                listeners = list(self._sample_listeners)
            for fn in listeners:
                try:
                    fn(sample)
                except Exception as e:  # a broken consumer must not kill acquisition
                    self.log(f"Sample consumer failed: {e}", LogLevel.WARNING)
        else:
            self.invalid_samples += 1
            self.log(data or "(empty response)", LogLevel.ERROR)

    # ------------------------------------------------------------------ low-level helpers (I/O thread)
    def _query_log(self, command: str) -> str:
        r = self.transport.query(command).strip()
        self.log(describe_response(command, r), LogLevel.SUCCESS)
        return r

    # ------------------------------------------------------------------ public command API
    def write(self, command: str, label: Optional[str] = None) -> None:
        self.submit(Action(label or command, lambda e: e.transport.write(command)))

    def query(self, command: str, label: Optional[str] = None) -> None:
        self.submit(Action(label or command, lambda e: e._query_log(command)))

    def select_function(self, function: Function) -> None:
        """Queue ``CONF:...``.

        ``state.function`` (what the user selected, used to build later commands) changes now;
        ``active_function`` (used to tag readings) changes on the I/O thread once the meter has been
        reconfigured, so a reading already in flight is never mislabelled. Function-change
        listeners are called from the I/O thread at that moment.
        """

        def run(e: "AcquisitionEngine") -> None:
            e.transport.write(function.conf)
            if e.transport.conf_settle_s:
                time.sleep(e.transport.conf_settle_s)
            # The first sample must be discarded, it may belong to the previous function.
            try:
                e.transport.query("READ?")
            except TransportError:
                pass
            e.active_function = function
            for fn in list(e._reset_listeners):
                fn(function)

        self.state.function = function
        self.state.auto_range[function.index] = True
        self.log(f"{function.name} Measurement Selected.", LogLevel.INFO)
        self.submit(Action(function.conf, run))

    def set_range(self, option: RangeOption) -> None:
        f = self.state.function
        cmd = f.range_command(option)
        self.state.auto_range[f.index] = option.value is None
        self.log(f"{f.name} Range set to {option.label}.", LogLevel.COMMAND)
        self.write(cmd)

    def query_range(self) -> None:
        self.query(self.state.function.range_query())

    def query_auto_range(self) -> None:
        self.query(self.state.function.auto_range_query())

    def set_nplc(self, nplc: float) -> None:
        f = self.state.function
        self.log(f"{f.name}: NPLC set to {format_number(nplc)}", LogLevel.COMMAND)
        self.write(f.nplc_command(nplc))

    def query_nplc(self) -> None:
        self.query(self.state.function.nplc_query())

    def set_resolution(self, value: "float | str") -> None:
        f = self.state.function
        if not isinstance(value, str):
            if value <= 0:
                raise ValueError("Set Resolution Value must be greater than 0.")
            if self.state.auto_range[f.index]:
                raise ValueError(f"{f.name}: Cannot set Resolution value, Manual Range not set.")
        cmd = f.resolution_command(value)
        self.log(cmd, LogLevel.COMMAND)
        self.write(cmd)

    def query_resolution(self) -> None:
        self.query(self.state.function.resolution_query())

    def set_null(self, enabled: bool) -> None:
        if enabled:
            def run(e: "AcquisitionEngine") -> None:
                e.transport.write("CALCulate:FUNCtion NULL")
                e.transport.write("CALCulate:STATe ON")

            self.submit(Action("NULL ON", run))
        else:
            self.write("CALCulate:STATe OFF", "NULL OFF")

    def query_null(self) -> None:
        def run(e: "AcquisitionEngine") -> None:
            fn = e.transport.query("CALCulate:FUNCtion?")
            st = e.transport.query("CALCulate:STATe?")
            e.log(describe_null_state(fn, st), LogLevel.SUCCESS)

        self.submit(Action("NULL?", run))

    def set_null_value(self, value: float) -> None:
        self.state.null_value = value

        def run(e: "AcquisitionEngine") -> None:
            e.transport.write(f"CALCulate:NULL:OFFSet {format_number(value)}")
            e.log(f"Null Value has been set to {format_number(value)}", LogLevel.SUCCESS)

        self.submit(Action("NULL_SET", run))

    def query_null_value(self) -> None:
        self.query("CALCulate:NULL:OFFSet?")

    def set_display_text(self, message: str) -> None:
        self.write(scpi.display_text_command(message))

    def local_exit(self) -> None:
        """Return the meter to front-panel control, then ask the application to quit."""

        def run(e: "AcquisitionEngine") -> None:
            e.transport.go_to_local()
            time.sleep(0.2)
            e.sampling = False
            if e.on_exit_request:
                e.on_exit_request()

        self.submit(Action("LOCAL_EXIT", run))

    def run_sync(self, fn: Callable[["AcquisitionEngine"], None], label: str = "sync", timeout: float = 30.0) -> None:
        """Queue ``fn`` and block until it ran (for scripts/tests)."""
        done = threading.Event()
        err: List[BaseException] = []

        def wrapper(e: "AcquisitionEngine") -> None:
            try:
                fn(e)
            except BaseException as ex:  # noqa: BLE001
                err.append(ex)
            finally:
                done.set()

        self.submit(Action(label, wrapper))
        if not done.wait(timeout):
            raise TimeoutError(label)
        if err:
            raise err[0]
