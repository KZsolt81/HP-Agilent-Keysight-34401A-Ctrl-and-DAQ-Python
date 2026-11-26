"""Headless data logger: ``hp34401a-log``.

Example::

    hp34401a-log serial --port /dev/ttyUSB0 --function VDC --interval 0.5 --csv run.csv
    hp34401a-log visa --resource GPIB0::22::INSTR --function 4W --nplc 10 --count 1000
    hp34401a-log ar488 --port COM5 --gpib 22 --function FREQ --duration 3600
    hp34401a-log sim --function VAC --count 20
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path
from typing import List, Optional

from . import __version__
from .datalog import DataLogger, OutputLogFile, prepare_log_directory
from .engine import AcquisitionEngine, LogLevel, Sample
from .formatting import format_timestamp
from .scpi import get_function
from .transport import (AR488Settings, AR488Transport, SerialSettings, SerialTransport, SimulatedTransport,
                        Transport, VisaSettings, VisaTransport)


def build_transport(args: argparse.Namespace) -> Transport:
    if args.transport == "serial":
        return SerialTransport(SerialSettings(port=args.port, baudrate=args.baud, bytesize=args.databits,
                                              parity=args.parity, stopbits=args.stopbits, handshake=args.handshake,
                                              dtr=args.dtr, rts=args.rts))
    if args.transport == "ar488":
        return AR488Transport(AR488Settings(port=args.port, baudrate=args.baud, gpib_address=args.gpib,
                                            boot_delay_s=args.boot_delay))
    if args.transport == "visa":
        return VisaTransport(VisaSettings(resource=args.resource, backend=args.backend,
                                          exclusive_lock=args.lock))
    return SimulatedTransport()


def make_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hp34401a-log", description="Log readings from an HP/Agilent/Keysight 34401A.")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="transport", required=True)

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--function", "-f", default="VDC",
                        help="VDC, ADC, VAC, AAC, 2W, 4W, FREQ, PER, DIODE, CONT (default VDC)")
        sp.add_argument("--range", dest="rng", default=None,
                        help="fixed range value as accepted by the meter, e.g. 10 or 1E+3 (default auto)")
        sp.add_argument("--nplc", type=float, default=None, help="integration time in power-line cycles")
        sp.add_argument("--interval", "-i", type=float, default=1.0, help="seconds between readings (default 1)")
        sp.add_argument("--count", "-n", type=int, default=0, help="stop after N readings (0 = unlimited)")
        sp.add_argument("--duration", "-d", type=float, default=0, help="stop after S seconds (0 = unlimited)")
        sp.add_argument("--fast", action="store_true", help="sampling-only mode: read back-to-back, ignore --interval")
        sp.add_argument("--csv", type=Path, default=None, help="also write timestamp,value to this CSV file")
        sp.add_argument("--log-dir", type=Path, default=None,
                        help="write the original 'Log Data (HP34401A)' folder tree under this directory")
        sp.add_argument("--quiet", "-q", action="store_true", help="do not print readings")

    sp = sub.add_parser("serial", help="34401A RS-232 port (null modem)")
    sp.add_argument("--port", required=True)
    sp.add_argument("--baud", type=int, default=9600)
    sp.add_argument("--databits", type=int, default=8, choices=(7, 8))
    sp.add_argument("--parity", default="None", choices=("None", "Even", "Odd"))
    sp.add_argument("--stopbits", default="1", choices=("1", "1.5", "2"))
    sp.add_argument("--handshake", default="None", choices=("None", "XOnXOff", "RequestToSend", "RequestToSendXOnXOff"))
    sp.add_argument("--dtr", action="store_true", help="assert DTR")
    sp.add_argument("--rts", action="store_true", help="assert RTS")
    common(sp)

    sp = sub.add_parser("ar488", help="AR488 Arduino GPIB adapter")
    sp.add_argument("--port", required=True)
    sp.add_argument("--baud", type=int, default=115200)
    sp.add_argument("--gpib", type=int, default=22)
    sp.add_argument("--boot-delay", type=float, default=0.0, help="seconds to wait after opening (Arduino reset)")
    common(sp)

    sp = sub.add_parser("visa", help="VISA resource (GPIB/USB/LAN)")
    sp.add_argument("--resource", required=True, help="e.g. GPIB0::22::INSTR")
    sp.add_argument("--backend", default="", help="'@py' for pyvisa-py, empty for system VISA")
    sp.add_argument("--lock", action="store_true", help="open with an exclusive lock")
    common(sp)

    sp = sub.add_parser("sim", help="built-in simulator (no hardware)")
    common(sp)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = make_parser().parse_args(argv)
    function = get_function(args.function)

    out_log: Optional[OutputLogFile] = None

    def log(msg: str, level: LogLevel) -> None:
        if level in (LogLevel.ERROR, LogLevel.WARNING) or not args.quiet:
            print(f"{level.tag} {msg}".strip(), file=sys.stderr)
        if out_log:
            out_log.write(msg, level)

    transport = build_transport(args)
    try:
        transport.open()
        idn = transport.connect_sequence()
    except Exception as e:
        print(f"Connect failed: {e}", file=sys.stderr)
        return 2
    log(f"Connected: {idn}", LogLevel.SUCCESS)

    engine = AcquisitionEngine(transport, log=log, interval_s=args.interval)
    engine.sampling = False
    datalogger = None
    if args.log_dir:
        root = prepare_log_directory(args.log_dir)
        out_log = OutputLogFile(root, transport.file_tag)
        datalogger = DataLogger(root, transport.file_tag, interval_s=10, log=log)
        datalogger.start()
        engine.add_sample_listener(datalogger)

    csv_fh = open(args.csv, "a", encoding="utf-8") if args.csv else None
    done = threading.Event()
    count = [0]

    def on_sample(s: Sample) -> None:
        line = f"{format_timestamp(s.timestamp)},{s.raw}"
        if csv_fh:
            csv_fh.write(line + "\n")
        if not args.quiet:
            print(line, flush=True)
        count[0] += 1
        if args.count and count[0] >= args.count:
            engine.sampling = False
            done.set()

    engine.add_sample_listener(on_sample)
    engine.start()
    try:
        engine.select_function(function)
        if args.rng:
            from .scpi import RangeOption

            engine.set_range(RangeOption(args.rng, args.rng))
        if args.nplc is not None:
            engine.set_nplc(args.nplc)
        engine.run_sync(lambda e: None, "configure")
        engine.sampling = True
        if args.fast:
            engine.set_sampling_only(True)
        engine.kick()
        t0 = time.monotonic()
        while not done.is_set():
            if args.duration and time.monotonic() - t0 >= args.duration:
                break
            done.wait(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        engine.sampling = False
        engine.sampling_only = False
        engine.stop()
        try:
            transport.go_to_local()
        except Exception:
            pass
        transport.close()
        if datalogger:
            datalogger.stop()
        if csv_fh:
            csv_fh.close()
    log(f"{count[0]} readings, {engine.invalid_samples} invalid.", LogLevel.SUCCESS)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
