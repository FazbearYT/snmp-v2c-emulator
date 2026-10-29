import gc
import json
import os
import time
from dataclasses import replace
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
def window(desktop, tmp_path, monkeypatch):
    gc.collect()
    root = tk.Toplevel(desktop)
    root.withdraw()
    monkeypatch.setattr("snmp_emulator.gui_console.SnmpConsoleWindow.show", lambda _: None)
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
    if panel.console is not None:
        panel.console.client.cancel()
    deadline = time.monotonic() + 5
    while (
        panel.controller.active or (panel.console is not None and panel.console.client.active)
    ) and time.monotonic() < deadline:
        root.update()
        time.sleep(0.01)
    assert not panel.controller.active
    assert panel.console is None or not panel.console.client.active
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
    assert PROFILES[panel.profile.get()] == ("device.json", "cpu-step.json")
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
    assert panel.files_frame.winfo_manager() == ""
    assert panel.preset_frame.winfo_manager() == "grid"
    panel.profile.set("Скачок температуры")
    assert panel.selected_paths()[1].name == "temperature-jump.json"
    panel.mode.set("custom")
    panel._change_mode()
    root.update()
    assert panel.selected_paths() == custom_paths
    assert panel.preset_frame.winfo_manager() == ""
    monkeypatch.setattr("snmp_emulator.gui.filedialog.askopenfilename", lambda **_: "other.json")
    panel._browse(panel.scenario)
    assert panel.selected_paths()[1].name == "other.json"
    assert panel.profile.get() == "Скачок температуры"
    monkeypatch.setattr("snmp_emulator.gui.filedialog.askopenfilename", lambda **_: "")
    panel._browse(panel.scenario)
    assert panel.selected_paths()[1].name == "other.json"


def test_console_templates_completion_requests_and_history(window):
    root, panel = window
    panel.start()
    pump_until(root, lambda: bool(panel.table.get_children()))
    panel.open_console()
    console = panel.console
    console.command.set("GET cpu")
    assert "cpu_usage" in console.choices.get(0)
    console._complete_first()
    assert console.command.get() == "GET 1.3.6.1.4.1.55555.1.1.0"
    for operation in ("GET", "GETNEXT", "WALK", "GETBULK"):
        console.template(operation)
        console.execute()
        pump_until(root, lambda: not console.client.active)
        console.refresh(panel.controller.snapshot(), True)
    output = console.output.get("1.0", "end")
    assert "Ошибка" not in output
    assert output.count("Завершено за") == 4
    assert "OctetString" in output
    assert "Конец ветки" in output
    console._recall(-1)
    assert console.command.get().startswith("GETBULK")
    console.command.set("GET missing")
    console.execute()
    assert "Ошибка команды" in console.output.get("1.0", "end")
    console.command.set("HELP")
    console.execute()
    assert "non-repeaters=0" in console.output.get("1.0", "end")
    console.command.set("CLEAR")
    console.execute()
    assert not console.output.get("1.0", "end").strip()
    panel.stop()
    console.command.set("GET cpu_usage")
    console.execute()
    assert "Сначала запустите" in console.output.get("1.0", "end")
    panel.close()
    pump_until(root, lambda: not panel.controller.active and not console.client.active)


def test_closing_panel_cancels_pending_console_request(window):
    root, panel = window
    panel.start()
    pump_until(root, lambda: bool(panel.table.get_children()))
    panel.open_console()
    console = panel.console
    console.refresh(
        replace(panel.controller.snapshot(), address=f"127.0.0.1:{reserve_udp_port()}"), True
    )
    console.command.set("GET cpu_usage")
    console.execute()
    assert console.client.active
    console.hide()
    panel.close()
    pump_until(root, lambda: not panel.controller.active and not console.client.active)


@pytest.mark.parametrize(
    "profile,count,branch",
    [
        ("Коммутатор: рост трафика", 7, "2"),
        ("ИБП: отключение питания", 8, "3"),
    ],
)
def test_device_profiles_populate_table_and_console(window, profile, count, branch):
    root, panel = window
    custom_device = Path(panel.device.get())
    panel.mode.set("preset")
    panel.profile.set(profile)
    device_path, scenario_path = panel.selected_paths()
    document = json.loads(device_path.read_text(encoding="utf-8"))
    assert 5 <= len(document["metrics"]) <= 10
    document["agent"]["port"] = reserve_udp_port()
    custom_device.write_text(json.dumps(document), encoding="utf-8")
    panel.mode.set("custom")
    panel.scenario.set(str(scenario_path))
    panel.start()
    pump_until(root, lambda: len(panel.table.get_children()) == count)
    panel.open_console()
    panel.console.command.set("GET ")
    suggestions = panel.console.choices.get(0, "end")
    assert len(suggestions) == count
    assert all(f"1.3.6.1.4.1.55555.{branch}." in item for item in suggestions)
    assert set(panel.table.get_children()) == {item["name"] for item in document["metrics"]}
