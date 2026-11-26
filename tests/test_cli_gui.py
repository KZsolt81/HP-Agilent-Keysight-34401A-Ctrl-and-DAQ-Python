import os
import time

import pytest

from hp34401a import cli


def test_cli_simulator(tmp_path, capsys):
    csv = tmp_path / "out.csv"
    rc = cli.main(["sim", "-f", "4W", "-n", "3", "-i", "0.01", "--csv", str(csv), "--log-dir", str(tmp_path)])
    assert rc == 0
    lines = csv.read_text().splitlines()
    assert len(lines) == 3
    assert all(l.split(",")[1].startswith("+9.") or l.split(",")[1].startswith("+1.") for l in lines)
    logged = list((tmp_path / "Log Data (HP34401A)" / "4WireOhms").glob("*.txt"))
    assert logged and len(logged[0].read_text().splitlines()) == 3


def _qt_available() -> bool:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        from PySide6.QtWidgets import QApplication  # noqa: F401
        import pyqtgraph  # noqa: F401
    except Exception:
        return False
    return True


@pytest.mark.skipif(not _qt_available(), reason="Qt not available")
def test_gui_end_to_end(tmp_path):
    from PySide6.QtWidgets import QApplication

    from hp34401a.datalog import prepare_log_directory
    from hp34401a.gui.main_window import MainWindow
    from hp34401a.scpi import FUNCTIONS
    from hp34401a.settings import AppConfig
    from hp34401a.transport import SimulatedTransport

    app = QApplication.instance() or QApplication([])
    w = MainWindow(AppConfig())
    t = SimulatedTransport(read_delay_s=0.002)
    t.open()
    w.attach(t, prepare_log_directory(tmp_path), t.connect_sequence())
    w._set_speed(0.01)
    w.open_graph(False)
    w.open_graph(True)
    w.open_table()

    def pump(seconds):
        end = time.time() + seconds
        while time.time() < end:
            app.processEvents()
            time.sleep(0.01)

    pump(1.0)
    g = w.graphs[0]
    assert g.buf.n > 5
    g.math_combo.setCurrentIndex(0)
    g.math_params[0].setText("1")
    g._create_math()
    g._create_hist()
    assert len(g.children) == 2
    math_win = g.children[0]
    assert math_win.buf.values[0] == pytest.approx(g.buf.values[0] + 1)
    w.select_function(FUNCTIONS[6])
    pump(1.0)
    assert w.engine.active_function.key == "FREQ"
    assert all(40 < v < 60 for v in g.buf.values)  # graph was reset, only Hz readings
    assert w.unit_label.text() == "Hz"
    assert len(w.tables[0].model.rows) > 5
    w.close()
