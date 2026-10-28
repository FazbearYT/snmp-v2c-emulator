import gc
import json
import os
import time
from pathlib import Path

import pytest
from installed_smoke import reserve_udp_port

tk = pytest.importorskip("tkinter")

from snmp_emulator.gui import PROFILES, EmulatorPanel  # noqa: E402


@pytest.fixture(scope="module")
def desktop():
    gc.collect()
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        if os.environ.get("SNMP_GUI_REQUIRED") == "1":
            raise
        pytest.skip(f"Graphical session required: {exc}")
    root.withdraw()
    yield root
    root.destroy()


@pytest.fixture
def window(desktop, tmp_path):
    gc.collect()
    root = tk.Toplevel(desktop)
    panel = EmulatorPanel(root)
    document = json.loads((Path(__file__).parents[1] / "examples" / "device.json").read_text())
    document["agent"]["port"] = reserve_udp_port()
    device = tmp_path / "device.json"
    device.write_text(json.dumps(document), encoding="utf-8")
    panel.device.set(str(device))
    panel.mode.set("custom")
    panel._change_mode()
    yield root, panel
    panel.controller.stop()
    deadline = time.monotonic() + 5
    while panel.controller.active and time.monotonic() < deadline:
        root.update()
        time.sleep(0.01)
    assert not panel.controller.active
    if root.winfo_exists():
        root.destroy()


def pump_until(root, predicate):
    deadline = time.monotonic() + 5
    while not predicate():
        assert time.monotonic() < deadline
        root.update()
        time.sleep(0.01)
    root.update()


def test_window_controls_real_application(window):
    root, panel = window
    panel.profile.set("Рост загрузки процессора")
    assert PROFILES[panel.profile.get()] == "cpu-step.json"
    panel.scenario.set(str(panel._examples / "cpu-step.json"))
    panel.start()
    panel.start()
    pump_until(root, lambda: bool(panel.table.get_children()))
    assert len(panel.table.get_children()) == 5
    assert str(panel.start_button["state"]) == "disabled"
    assert "UDP" in panel.details.get()
    pump_until(root, lambda: len(panel._history) > 1)
    assert panel.chart.find_all()
    panel.stop()
    pump_until(root, lambda: not panel.controller.active)
    panel.start()
    pump_until(root, lambda: panel.controller.snapshot().state == "running")
    panel.close()
    pump_until(root, lambda: not panel.controller.active)


def test_window_reports_file_error_and_keeps_responsive(window):
    root, panel = window
    device_path = panel.device.get()
    panel.device.set("missing-device.json")
    panel.start()
    pump_until(root, lambda: bool(panel.error.get()))
    assert "missing-device.json" in panel.error.get()
    assert str(panel.start_button["state"]) == "normal"
    panel.device.set(device_path)
    panel.start()
    pump_until(root, lambda: bool(panel.table.get_children()))
    assert not panel.error.get()


def test_modes_keep_custom_files_independent(window, monkeypatch):
    root, panel = window
    custom_paths = panel.selected_paths()
    panel.mode.set("preset")
    panel._change_mode()
    root.update()
    assert not panel.files_frame.winfo_ismapped()
    assert panel.preset_frame.winfo_ismapped()
    panel.profile.set("Скачок температуры")
    assert panel.selected_paths()[1].name == "temperature-jump.json"
    panel.mode.set("custom")
    panel._change_mode()
    root.update()
    assert panel.selected_paths() == custom_paths
    assert not panel.preset_frame.winfo_ismapped()
    monkeypatch.setattr("snmp_emulator.gui.filedialog.askopenfilename", lambda **_: "other.json")
    panel._browse(panel.scenario)
    assert panel.selected_paths()[1].name == "other.json"
    assert panel.profile.get() == "Скачок температуры"
    monkeypatch.setattr("snmp_emulator.gui.filedialog.askopenfilename", lambda **_: "")
    panel._browse(panel.scenario)
    assert panel.selected_paths()[1].name == "other.json"
