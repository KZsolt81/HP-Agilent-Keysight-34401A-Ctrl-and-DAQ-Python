"""GUI entry point: ``hp34401a`` / ``python -m hp34401a``."""

from __future__ import annotations

import argparse
import sys
from typing import List, Optional


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="hp34401a", description="HP 34401A control and data logging GUI")
    parser.add_argument("--simulate", action="store_true", help="connect to the built-in simulator immediately")
    parser.add_argument("--log-dir", default=None, help="log folder for --simulate (default: no file logging)")
    args, qt_args = parser.parse_known_args(argv)

    from PySide6.QtWidgets import QApplication

    from .main_window import MainWindow

    app = QApplication.instance() or QApplication([sys.argv[0], *qt_args])
    app.setApplicationName("HP 34401A")
    app.setOrganizationName("hp34401a")
    win = MainWindow()
    win.show()
    if args.simulate:
        from ..datalog import prepare_log_directory
        from ..transport import SimulatedTransport

        t = SimulatedTransport()
        t.open()
        idn = t.connect_sequence()
        root = prepare_log_directory(args.log_dir) if args.log_dir else None
        win.attach(t, root, idn)
    return app.exec()
