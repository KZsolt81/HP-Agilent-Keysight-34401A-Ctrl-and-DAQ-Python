# HP / Agilent / Keysight 34401A — Control & DAQ (Python)

A cross-platform Python port of Nirav Patel's
[HP Agilent Keysight 34401A Control and Data Logging Software](https://github.com/Niravk1997/HP-Agilent-Keysight-34401A-Control-and-Data-Logging-Software)
(C#/WPF, Windows only, MIT).

The original shipped as three separate programs: RS-232, AR488 Arduino GPIB, and VISA GPIB.
This port is one program with one code base, and the connection type is chosen at connect time.
It runs on Windows, Linux and macOS, and it adds a headless command-line logger and a built-in
34401A simulator.

## Features

| Area | What you get |
|---|---|
| **Connections** | RS-232 (USB-serial adapter + null modem), **AR488** Arduino GPIB adapter, any **VISA** resource (NI-VISA, Keysight IO Libraries or pure-Python `pyvisa-py`: GPIB, USB-TMC, LAN gateways), and a **simulator** |
| **Measurements** | DCV, DCI, ACV, ACI, 2-wire Ω, 4-wire Ω, Frequency, Period, Diode, Continuity |
| **Configuration** | Range (auto and every fixed range), resolution MIN/MAX/value, NPLC 0.02–100, trigger source/delay/auto-delay, NULL (on/off/offset), auto-zero on/off/once, DC input impedance, frequency/period aperture, AC detector bandwidth, VFD on/off and text, error queue, terminals, beep, `*IDN?`, `*STB?`, `*CLS`, SCPI version, calibration count/secure/message/value, and a raw SCPI command box |
| **Display** | Large front-panel readout with partial-SI (m/k/M/G) or full engineering prefixes, MIN / MAX / AVG (running mean, then EMA with a selectable factor), sample and invalid counters, runtime, colour themes, and `Settings.txt` compatibility |
| **Acquisition** | Dedicated I/O thread, adjustable update speed (default 1 s; "Fast" = 10 ms), and a **Sampling-only** mode that reads back-to-back as fast as the meter answers |
| **Data logging** | Same folder layout, file names and line format as the original (`Log Data (HP34401A)/VDC/2024-05-01_COM3_VDC.txt`, `yyyy-MM-dd h:mm:ss.fff tt,+1.23456789E+00`). Writes are buffered and flushed every 5 s – 10 min. The output log is saved too |
| **Graphs** | Live graph against sample number, **Date-Time graph**, **N-sample persistence graph** (1–10 overlaid blocks). Pan/zoom, auto-axis, themes, grid, legend, labels, font size, log-Y, PNG/JPG/SVG export, clipboard copy, and data export |
| **Analysis** | 14 statistics (mean, sample/population σ and variance, RMS, geometric/harmonic mean, \|max\|/\|min\| …) on all samples or an N-sample range. **Math waveforms** (± × ÷, % error, dB, dBm, powers, log, ln, √, \|x\|, trig/inverse/hyperbolic in degrees or radians, EMA), and math can be applied to math waveforms again without limit. **Histograms** with automatic or fixed bin size. Time difference between samples |
| **Markers / tracker** | Two draggable horizontal and two vertical markers with Δ readouts, a "use markers as N-sample range" option, and a crosshair tracker that shows coordinates and the nearest sample |
| **Table** | Live measurement table with CSV/TXT export, auto-scroll and fonts |
| **Speech** | Speaks the reading every N minutes, plus MIN/MAX threshold warnings, with voice, speed, volume and precision settings (`pyttsx3`) |
| **Re-plot** | Open any measurement file written by this program or by the original, then analyse it |

## Installation

Python 3.9 or newer.

```bash
git clone https://github.com/KZsolt81/HP-Agilent-Keysight-34401A-Ctrl-and-DAQ-Python.git
```

```bash
cd HP-Agilent-Keysight-34401A-Ctrl-and-DAQ-Python
```

```bash
pip install ".[all]"
```

`[all]` installs the optional VISA (`pyvisa`, `pyvisa-py`) and speech (`pyttsx3`) support. A plain
`pip install .` is enough for RS-232, AR488 and the simulator.

* **VISA GPIB cards/adapters:** install NI-VISA or Keysight IO Libraries and leave *Backend* empty.
  Alternatively use `pyvisa-py` (backend `@py`), which needs `linux-gpib` / `gpib-ctypes` for GPIB
  hardware. With a Keysight 82357B and both vendors' libraries installed, set NI-VISA as the primary
  VISA (the same advice as the original).
* **Speech on Linux** needs `espeak-ng` (`sudo apt install espeak-ng`). On Windows it uses SAPI5, on
  macOS NSSpeechSynthesizer.
* **Linux serial ports:** add yourself to the `dialout` group (or `uucp` on Arch).

## Running

```bash
hp34401a
```

```bash
hp34401a --simulate
```

The first command opens the GUI. Use **Config → Connect…**, pick the tab (RS-232 / AR488 GPIB / VISA /
Simulator), choose the log folder, then click **Verify 34401A** or **Connect**. Connecting checks that
`*IDN?` contains `34401A`, sends `*RST`, puts the meter in remote (RS-232: `SYSTem:REMote`, AR488:
`++read_tmo_ms`) and starts sampling DCV. The second command starts the GUI already connected to the
simulator. `python -m hp34401a` is equivalent to `hp34401a`.

**Config → Local Exit** returns the meter to front-panel control (`SYSTem:LOCal`, `++ren 0`, or VISA
GTL) and quits.

### Headless logger

```bash
hp34401a-log serial --port /dev/ttyUSB0 --function VDC --interval 0.5 --csv run.csv
```

```bash
hp34401a-log visa --resource GPIB0::22::INSTR --function 4W --nplc 10 --count 1000 --log-dir ~/meas
```

```bash
hp34401a-log ar488 --port COM5 --gpib 22 --function FREQ --duration 3600 --fast
```

```bash
hp34401a-log sim --function VAC --count 20
```

`hp34401a-log <transport> --help` lists all options: range, NPLC, count, duration, CSV output, the
original log-folder tree, and fast (sampling-only) mode.

## Hardware notes

* **RS-232:** the 34401A's port needs a **null-modem** cable or adapter. The meter's factory settings
  are 9600 baud, 8 data bits, no parity; they are changed in its I/O menu. Defaults here match the
  original: 9600-8-N-1, no handshake, 4 s write / 9 s read timeouts. The RS-232 transport pauses 1.2 s
  after every configuration command, exactly as the original did. If every read times out, try asserting **DTR** (Connect dialog). The meter uses DTR/DSR
  flow control and some cables do not loop it back. Confidence: moderate; this depends on the cable.
* **AR488:** default 115200 baud and GPIB address 22. The port opens with DTR **low** so an Arduino Uno
  or Nano does not reboot. If your board resets anyway, set *Wait after open* to about 2 s. The `++ver`,
  `++addr?` and `++rst` buttons talk to the adapter itself.
* **Speed:** with GPIB in sampling-only mode the original reached about 140 readings/s, and RS-232
  about 30/s. Expect the same order here; the limit is the meter and the bus, not Python.

## Files

| File | Content |
|---|---|
| `Log Data (HP34401A)/<FUNC>/<UTC date>_<port>_<FUNC>.txt` | `yyyy-MM-dd h:mm:ss.fff tt,<raw reading>`, one per line (local time stamp, UTC date in the file name, as in the original) |
| `Log Data (HP34401A)/<port>_Output Log.txt` | `[yyyy-MM-dd h:mm:ss tt] [Level] message` |
| Graph "Save Data" | `timestamp,value` |
| `Settings.txt` (optional) | The original's display-colour file (4 RGB lines and `TRUE`/`FALSE` for partial-SI). It is read from the working directory or the config folder |
| `config.json` | Last connection, log folder and display options, stored in `%APPDATA%\hp34401a`, `~/Library/Application Support/hp34401a` or `~/.config/hp34401a` |

## Architecture

```
src/hp34401a/
  scpi.py        command tables (verbatim from the original) + response decoding
  transport.py   Serial / AR488 / VISA / Simulated transports
  engine.py      I/O thread: read loop, command queue, MIN/MAX/AVG tracker
  datalog.py     buffered per-function log files, output log
  analysis.py    statistics, math waveforms, histogram (numpy)
  formatting.py  display prefixes, C#-compatible timestamps
  speech.py      pyttsx3 announcer
  cli.py         hp34401a-log
  gui/           PySide6 + pyqtgraph windows
tests/           pytest suite (simulator-driven, includes an off-screen GUI test)
```

The engine has no Qt dependency, so it can be scripted directly:

```python
from hp34401a.transport import VisaTransport, VisaSettings
from hp34401a.engine import AcquisitionEngine
from hp34401a.scpi import get_function

t = VisaTransport(VisaSettings("GPIB0::22::INSTR")); t.open(); t.connect_sequence()
eng = AcquisitionEngine(t, log=print, interval_s=0.2)
eng.add_sample_listener(lambda s: print(s.timestamp, s.value, s.function.unit))
eng.start(); eng.select_function(get_function("4W"))
```

## Deliberate differences from the original

Most of these fix bugs in the original. The rest are adaptations to Qt and to multiple platforms.

* **Trig math in degrees:** the original computed `sin(x)·180/π`, which is mathematically wrong.
  Here, forward functions take degrees and inverse functions return degrees.
* **Full-SI display** uses true engineering notation. The original showed 12.3 V as "0.0123 KV".
  Partial-SI also scales negative kilo/mega/giga readings, which the original only scaled for
  positive values.
* **ACI units:** ACI MIN/MAX/AVG and the table now say `AAC`; the original said `ADC`. The MAX "G"
  range is now divided by 10⁹ instead of multiplied.
* **Function switch:** readings that were in flight when you change function are no longer tagged
  with the new function. The meter is reconfigured first, then MIN/MAX/AVG, graphs and logging switch.
* **Statistics labels:** "Mean Std Deviation" and "Mean Variance" are labelled *unbiased sample*.
  The values are MathNet's, which the original mislabelled "Unbiased Pop".
* **Output log:** auto-clear defaults to 200 lines instead of 20; 20, 40 and 60 are still available.
* **Windows and threads:** the original opened each window in its own WPF thread. Qt needs all
  windows on one GUI thread, so here only the I/O, logging and speech run on their own threads.
  Rendering is throttled with a selectable refresh rate.
* **Graph cosmetics:** the many per-element colour menus (axis, grid, background, each
  individually) are replaced by themes and a measurement-colour menu. pyqtgraph's right-click
  menu adds axis-range and export options.

## Tests

```bash
pip install ".[test]"
```

```bash
pytest
```

The tests cover the SCPI strings against the original, response decoding, number and timestamp
formatting, the statistics, math and histogram code, the engine and its command ordering with the
simulator, the log-file format, the AR488 `++` protocol (with a fake serial port), the CLI, and an
off-screen end-to-end GUI run.

They have **not** been run against a physical 34401A. The SCPI traffic is copied from the original
program, which was used with real meters.

## Credits

* Original software and design: **Nirav Patel** ([Niravk1997](https://github.com/Niravk1997)), MIT.
* AR488 adapter: [Twilight-Logic/AR488](https://github.com/Twilight-Logic/AR488).
