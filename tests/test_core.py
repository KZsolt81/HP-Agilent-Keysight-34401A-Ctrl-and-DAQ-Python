import math
import time
from datetime import datetime

import numpy as np
import pytest

from hp34401a import analysis, scpi
from hp34401a.datalog import DataLogger, OutputLogFile, prepare_log_directory
from hp34401a.engine import AcquisitionEngine, LogLevel, Sample, StatsTracker
from hp34401a.formatting import format_timestamp, full_si, parse_timestamp, partial_si, scale_stat
from hp34401a.transport import AR488Settings, AR488Transport, SimulatedTransport, TransportError


# ----------------------------------------------------------------------------- SCPI tables
def test_commands_match_original():
    vdc, adc, vac, aac, r2, r4, freq, per, diode, cont = scpi.FUNCTIONS
    assert vdc.conf == "CONF:VOLT:DC DEF,DEF"
    assert diode.conf == "CONF:DIOD" and cont.conf == "CONF:CONT"
    assert vdc.range_command(vdc.ranges[0]) == "VOLTage:DC:RANGe:AUTO ON"
    assert vdc.range_command(vdc.ranges[1]) == "VOLTage:DC:RANGe 0.1"
    assert adc.range_command(adc.ranges[1]) == "CURRent:DC:RANGe 0.01"
    assert aac.range_command(aac.ranges[2]) == "CURRent:AC:RANGe 3"
    assert r4.range_command(r4.ranges[2]) == "FRESistance:RANGe 1E+3"
    assert per.range_command(per.ranges[5]) == "PERiod:VOLTage:RANGe 1000"
    assert freq.auto_range_query() == "FREQuency:VOLTage:RANGe:AUTO?"
    assert r2.nplc_command(0.02) == "RESistance:NPLCycles 0.02"
    assert vdc.nplc_command(10) == "VOLTage:DC:NPLCycles 10"
    assert vac.resolution_command("max") == "VOLTage:AC:RESolution MAX"
    assert adc.resolution_command(0.00001) == "CURRent:DC:RESolution 0.00001"
    with pytest.raises(ValueError):
        vac.nplc_command(1)
    with pytest.raises(ValueError):
        diode.range_query()
    assert [f.supports_nplc for f in scpi.FUNCTIONS] == [True, True, False, False, True, True, False, False, False, False]


def test_get_function_aliases():
    assert scpi.get_function("4W").key == "4WireOhms"
    assert scpi.get_function("dcv").index == 0
    assert scpi.get_function(9).key == "CONTINUITY"
    with pytest.raises(ValueError):
        scpi.get_function("XYZ")


def test_format_number_is_decimal_like():
    assert scpi.format_number(0.1) == "0.1"
    assert scpi.format_number(1e-5) == "0.00001"
    assert scpi.format_number(1000.0) == "1000"
    assert scpi.format_number(-0.0) == "0"
    assert scpi.format_number(1.2345678000000001) == "1.2345678"


def test_reading_validation_and_overload():
    assert scpi.is_valid_reading("+1.23456789E+00")
    assert scpi.is_valid_reading("-9.90000000E+37")
    assert not scpi.is_valid_reading("+1.234E+00")
    assert not scpi.is_valid_reading("garbage")
    assert scpi.is_overload("+9.90000000E+37") and scpi.is_overload(-9.9e37)
    assert not scpi.is_overload(1e11)


def test_display_text_command():
    assert scpi.display_text_command(" HELLO1 ") == "DISPlay:TEXT 'HELLO1'"
    with pytest.raises(ValueError):
        scpi.display_text_command("no spaces!")


@pytest.mark.parametrize("cmd,resp,expected", [
    ("ZERO:AUTO?", "1", "Auto Zero is ON"),
    ("ZERO:AUTO?", "0", "Auto Zero is OFF"),
    ("FUNCtion?", '"FRES"', "4 Wire Ω measurement is selected."),
    ("VOLTage:DC:RANGe:AUTO?", "1", "VOLTAGE:DC:RANGE:AUTO? is ON"),
    ("CURRent:DC:RANGe?", "+1.00000000E-02", "CURRENT:DC:RANGE? +1.00000000E-02"),
    ("DETector:BANDwidth?", "+2.00000000E+01", "ACV ACI Bandwidth: 20Hz"),
    ("*IDN?", "HEWLETT-PACKARD,34401A,0,11-5-2", "Device ID: HEWLETT-PACKARD,34401A,0,11-5-2"),
    ("VOLTage:DC:NPLCycles?", "+1.00000000E+01", "VOLTage:DC:NPLCycles?  10"),
    ("CALCulate:NULL:OFFSet?", "+1.50000000E+00", "Query Null Set Value: 1.5"),
])
def test_describe_response(cmd, resp, expected):
    assert scpi.describe_response(cmd, resp) == expected


def test_describe_null_state():
    assert scpi.describe_null_state("NULL", "1") == "NULL function is active."
    assert scpi.describe_null_state("NULL", "0") == "NULL function is not active."


# ----------------------------------------------------------------------------- formatting
def test_timestamp_roundtrip_matches_csharp_format():
    dt = datetime(2024, 3, 5, 0, 7, 9, 45000)
    assert format_timestamp(dt) == "2024-03-05 12:07:09.045 AM"
    assert format_timestamp(datetime(2024, 3, 5, 13, 0, 0)) == "2024-03-05 1:00:00.000 PM"
    assert parse_timestamp("2024-03-05 12:07:09.045 AM") == dt


def test_partial_si():
    assert partial_si(0.0) == ("0", "")
    assert partial_si(0.0123456) == ("12.3456", "m")
    assert partial_si(5.00012) == ("5.00012", "")
    assert partial_si(4700.5) == ("4.7005", "k")
    assert partial_si(-4700.5) == ("-4.7005", "k")
    assert partial_si(12.5e6) == ("12.5", "M")


def test_full_si_engineering():
    assert full_si("+1.23456789E-05", 1.23456789e-05) == ("12.3456789", "μ")
    assert full_si("+4.70040000E+03", 4700.4) == ("4.7004", "k")
    assert full_si("+1.23400000E+01", 12.34) == ("12.34", "")
    assert full_si("-5.00000000E-10", -5e-10) == ("-500", "p")


def test_scale_stat():
    assert scale_stat(5e-7) == ("500", "n")
    assert scale_stat(0.5) == ("500", "m")
    assert scale_stat(1.23456789, 3) == ("1.235", "")
    assert scale_stat(2.5e9) == ("2.5", "G")
    assert scale_stat(1e11) == ("OVLD", "")


# ----------------------------------------------------------------------------- stats tracker
def test_stats_tracker_running_then_exponential():
    st = StatsTracker(factor=2)
    for v in (1.0, 3.0, 5.0):
        st.update(v)
    assert st.min == 1.0 and st.max == 5.0
    # n=1: 1, n=2: 2 (mean), n=3: 2 + (5-2)/2 = 3.5 (EMA with alpha=1/2)
    assert st.avg == pytest.approx(3.5)
    st.request_reset()
    st.update(10.0)
    assert (st.min, st.max, st.avg) == (10.0, 10.0, 10.0)
    st.update(9.9e37)
    assert st.max == 1e11


# ----------------------------------------------------------------------------- analysis
def test_statistics_match_mathnet_semantics():
    d = [1.0, 2.0, 3.0, 4.0]
    assert analysis.compute_statistic("Mean (Average)", d) == 2.5
    assert analysis.compute_statistic("Variance", d) == pytest.approx(5 / 3)
    assert analysis.compute_statistic("Pop Variance", d) == pytest.approx(1.25)
    assert analysis.compute_statistic("Std Deviation", d) == pytest.approx(math.sqrt(5 / 3))
    assert analysis.compute_statistic("Root Mean Square", d) == pytest.approx(math.sqrt(7.5))
    assert analysis.compute_statistic("Geometric Mean", d) == pytest.approx(24 ** 0.25)
    assert analysis.compute_statistic("Harmonic Mean", d) == pytest.approx(4 / (1 + 1 / 2 + 1 / 3 + 1 / 4))
    assert analysis.compute_statistic("|Min| Sample", [-3, 2, -1]) == 1
    assert analysis.compute_statistic("Mean Std Deviation", d) == (2.5, pytest.approx(math.sqrt(5 / 3)))
    with pytest.raises(ValueError):
        analysis.compute_statistic("Mean (Average)", [])


def test_math_ops():
    x = [1.0, 2.0, 4.0]
    assert list(analysis.apply_math("add", x, [1])) == [2, 3, 5]
    assert list(analysis.apply_math("rdiv", [0.0, 2.0], [4])) == [0, 2]  # inf -> 0
    assert list(analysis.apply_math("pct_err", [9.0, 11.0], [10])) == pytest.approx([10, 10])
    assert analysis.apply_math("db", [10.0], [20, 10, 1])[0] == pytest.approx(20)
    # dBm across 600 ohm: 0.7746 V -> 1 mW -> 0 dBm
    assert analysis.apply_math("dbm", [0.7745967], [10, 10, 600, 0.001])[0] == pytest.approx(0, abs=1e-5)
    assert list(analysis.apply_math("ln", [-1.0, math.e])) == [0, pytest.approx(1)]
    assert analysis.apply_math("sin", [90.0], degrees=True)[0] == pytest.approx(1)
    assert analysis.apply_math("asin", [1.0], degrees=True)[0] == pytest.approx(90)
    assert analysis.apply_math("sin", [math.pi / 2])[0] == pytest.approx(1)
    with pytest.raises(ValueError):
        analysis.apply_math("div", x, [0])
    with pytest.raises(ValueError):
        analysis.apply_math("add", x, [])


def test_running_average_equals_original_formula():
    data = np.arange(10, dtype=float)
    out = analysis.running_average(data, 3)
    avg = 0.0
    for i, v in enumerate(data):
        avg += (v - avg) / min(i + 1, 3)
    assert out[-1] == pytest.approx(avg)
    assert out[1] == pytest.approx(0.5)


def test_histogram():
    res = analysis.histogram([0, 0.1, 0.2, 1.0, 1.05], bin_size=0.5)
    assert res.counts.sum() == 5
    assert list(res.counts) == [3, 0, 2]  # bins [0,.5) [.5,1) [1,1.5]
    auto = analysis.histogram(np.random.default_rng(1).normal(size=400))
    assert auto.bin_size == pytest.approx((auto.edges[-1] - auto.edges[0]) / len(auto.counts))
    assert auto.counts.sum() == 400
    same = analysis.histogram([5.0, 5.0, 5.0])
    assert same.counts.sum() == 3


def test_validate_range():
    assert analysis.validate_range(0, 5, 10) is None
    assert analysis.validate_range(5, 5, 10)
    assert analysis.validate_range(0, 10, 10)


# ----------------------------------------------------------------------------- engine with simulator
def _engine(**kw):
    logs = []
    t = SimulatedTransport(read_delay_s=0.001, seed=1, **kw)
    t.open()
    t.connect_sequence()
    e = AcquisitionEngine(t, log=lambda m, l: logs.append((m, l)), interval_s=0.01)
    return t, e, logs


def test_engine_reads_and_configures():
    t, e, logs = _engine()
    samples = []
    changed = []
    e.add_sample_listener(samples.append)
    e.add_function_change_listener(changed.append)
    e.start()
    try:
        e.select_function(scpi.FUNCTIONS[5])
        e.set_range(scpi.FUNCTIONS[5].ranges[3])
        e.set_nplc(1)
        e.query("ZERO:AUTO?")
        e.run_sync(lambda _e: None)
        assert changed == [scpi.FUNCTIONS[5]]
        n0 = len(samples)
        deadline = time.time() + 5
        while len(samples) < n0 + 5 and time.time() < deadline:
            time.sleep(0.01)
    finally:
        e.stop()
    assert len(samples) >= n0 + 5
    assert samples[-1].function.key == "4WireOhms"
    assert 90 < samples[-1].value < 110
    h = t.history
    i = h.index("CONF:FRES DEF,DEF")
    assert h[i + 1] == "READ?"  # first reading after CONF is discarded
    assert "FRESistance:RANGe 10E+3" in h and "FRESistance:NPLCycles 1" in h
    assert ("Auto Zero is ON", LogLevel.SUCCESS) in logs


def test_engine_rejects_resolution_in_auto_range():
    t, e, logs = _engine()
    with pytest.raises(ValueError):
        e.set_resolution(0.001)
    e.set_range(scpi.FUNCTIONS[0].ranges[2])
    e.set_resolution(0.001)  # queued, no exception


def test_engine_counts_invalid_and_overload():
    t, e, logs = _engine(overload_probability=1.0)
    got = []
    e.add_sample_listener(got.append)
    e._read_once()
    assert got[0].overload and e.total_samples == 1
    t._pending.append("garbage")
    t.query = lambda cmd: "garbage"  # type: ignore[assignment]
    e._read_once()
    assert e.invalid_samples == 1


def test_sampling_only_blocks_commands():
    t, e, logs = _engine()
    e.set_sampling_only(True)
    e.write("*CLS")
    assert e._queue.empty()


# ----------------------------------------------------------------------------- data logger
def test_datalogger_layout(tmp_path):
    root = prepare_log_directory(tmp_path)
    assert root.name == "Log Data (HP34401A)"
    assert {p.name for p in root.iterdir()} >= set(scpi.LOG_FOLDERS)
    dl = DataLogger(root, "COM3")
    s = Sample(datetime(2024, 1, 2, 15, 4, 5, 6000), "+1.00000000E+00", 1.0, scpi.FUNCTIONS[0])
    dl(s)
    dl(Sample(s.timestamp, "+2.00000000E+03", 2000.0, scpi.FUNCTIONS[4]))
    assert dl.flush() == 2
    vdc = list((root / "VDC").glob("*_COM3_VDC.txt"))
    assert vdc and vdc[0].read_text() == "2024-01-02 3:04:05.006 PM,+1.00000000E+00\n"
    assert list((root / "2WireOhms").glob("*_COM3_2WireOhms.txt"))
    dl.enabled = False
    dl(s)
    assert dl.flush() == 0
    out = OutputLogFile(root, "COM3")
    out.write("hello", LogLevel.ERROR, datetime(2024, 1, 2, 9, 0, 0))
    assert out.path.read_text() == "[2024-01-02 9:00:00 AM] [Error] hello\n"


# ----------------------------------------------------------------------------- AR488 protocol
class FakeSerial:
    def __init__(self, replies):
        self.written = []
        self.replies = list(replies)

    def write(self, b):
        self.written.append(b.decode().strip())

    def flush(self):
        pass

    def readline(self):
        return (self.replies.pop(0) + "\r\n").encode() if self.replies else b""

    def reset_input_buffer(self):
        pass

    def close(self):
        pass


def test_ar488_sends_read_after_query(monkeypatch):
    fake = FakeSerial(["HEWLETT-PACKARD,34401A,0,11-5-2", "+1.00000000E+00"])
    monkeypatch.setattr("hp34401a.transport._open_serial", lambda s: fake)
    t = AR488Transport(AR488Settings(port="X", gpib_address=7))
    t.open()
    assert fake.written[:4] == ["++addr 7", "++ren 1", "++auto 0", "++eor 2"]
    t.connect_sequence()
    assert fake.written[4:8] == ["*IDN?", "++read", "*RST", "++read_tmo_ms 9000"]
    assert t.query("READ?") == "+1.00000000E+00"
    assert fake.written[-2:] == ["READ?", "++read"]
    t.go_to_local()
    assert fake.written[-1] == "++ren 0"
    with pytest.raises(TransportError):
        t.read()  # no reply -> timeout


def test_connect_rejects_other_instruments():
    t = SimulatedTransport()
    t.open()
    t._handle = lambda cmd: t._reply("FLUKE,8846A,0,1") if cmd == "*IDN?" else None  # type: ignore
    with pytest.raises(TransportError):
        t.connect_sequence()
