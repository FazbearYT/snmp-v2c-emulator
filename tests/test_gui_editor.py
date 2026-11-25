import json

from installed_smoke import reserve_udp_port
from test_gui import desktop as desktop
from test_gui import pump_until, tk
from test_gui import window as window

from snmp_emulator import gui_editor
from snmp_emulator.gui_editor import ttk


def widgets(container):
    for widget in container.winfo_children():
        yield widget
        yield from widgets(widget)


def click(container, text):
    button = next(
        widget
        for widget in widgets(container)
        if isinstance(widget, ttk.Button) and widget.cget("text") == text
    )
    button.invoke()


def choose_paths(monkeypatch, dialog, *paths):
    choices = iter(str(path) for path in paths)
    monkeypatch.setattr(gui_editor.filedialog, dialog, lambda **_: next(choices))


def open_step_editor(panel):
    panel.mode.set("preset")
    panel._change_mode()
    panel.profile.set("device · 5 метрик · Рост загрузки процессора")
    panel.editor_button.invoke()
    return panel.editor


def test_invalid_port_and_rule_mark_fields_without_saving(window, monkeypatch):
    root, panel = window
    editor = open_step_editor(panel)
    paths = panel.selected_paths()
    editor.agent.variables["port"].set("потерян")
    editor.rules.fields.variables["step"].set("0")
    dialogs = []
    monkeypatch.setattr(
        gui_editor.filedialog, "asksaveasfilename", lambda **options: dialogs.append(options)
    )

    click(editor.window, "Сохранить для запуска…")
    root.update()
    assert editor.agent.errors["port"].cget("text")
    assert editor.tabs.select() == str(editor.device_tab)
    editor.agent.variables["port"].set("1161")

    click(editor.window, "Сохранить для запуска…")
    root.update()
    assert not editor.agent.errors["port"].cget("text")
    assert editor.rules.fields.errors["step"].cget("text")
    assert "invalid" in editor.rules.tree.item("0", "tags")
    assert editor.tabs.select() == str(editor.scenario_tab)
    assert editor.scenario_sections.select() == str(editor.rules_tab)
    assert dialogs == []
    assert panel.selected_paths() == paths
    assert not panel.controller.active

    editor.rules.fields.variables["step"].set("10")
    click(editor.window, "Проверить")
    assert editor.status.get() == "Конфигурация корректна."
    assert not editor.rules.fields.errors["step"].cget("text")

    editor.metrics.select(1)
    editor.metrics.fields.variables["name"].set("cpu_usage")
    click(editor.window, "Проверить")
    assert editor.tabs.select() == str(editor.device_tab)
    assert editor.metrics.error_label.cget("text")


def test_save_reopen_and_run_edited_pair(window, tmp_path, monkeypatch):
    root, panel = window
    editor = open_step_editor(panel)
    source_device, source_scenario = panel.selected_paths()
    input_contents = source_device.read_bytes(), source_scenario.read_bytes()
    saved_device, saved_scenario = tmp_path / "device-new.json", tmp_path / "scenario-new.json"
    port = reserve_udp_port()
    editor.agent.variables["port"].set(str(port))
    editor.metrics.fields.variables["initial"].set("20")
    editor.times.variables["duration"].set("0.3")
    editor.times.variables["update_interval"].set("0.05")
    editor.rules.fields.variables["step"].set("7")
    editor.rules.fields.variables["limit"].set("34")
    choose_paths(monkeypatch, "asksaveasfilename", saved_device, saved_scenario)

    click(editor.window, "Сохранить для запуска…")
    assert not editor.window.winfo_exists()
    assert panel.mode.get() == "custom"
    assert panel.selected_paths() == (saved_device, saved_scenario)
    assert (source_device.read_bytes(), source_scenario.read_bytes()) == input_contents

    editor = open_step_editor(panel)
    choose_paths(monkeypatch, "askopenfilename", saved_device, saved_scenario)
    click(editor.window, "Открыть пару…")
    assert editor.agent.variables["port"].get() == str(port)
    assert editor.metrics.fields.variables["initial"].get() == "20"
    assert editor.rules.fields.variables["limit"].get() == "34"
    click(editor.window, "Закрыть")

    panel.mode.set("custom")
    panel._change_mode()
    panel.start_button.invoke()
    pump_until(root, lambda: panel.controller.snapshot().state == "completed")
    snapshot = panel.controller.snapshot()
    assert snapshot.metrics[0].name == "cpu_usage"
    assert snapshot.metrics[0].value == 34
    assert snapshot.address == f"127.0.0.1:{port}"
    assert panel.editor_button.instate(["disabled"])


def test_create_pair_with_sequence_and_oid_response(window, tmp_path, monkeypatch):
    _, panel = window
    editor = open_step_editor(panel)
    click(editor.window, "Новая пара")
    oid = "1.3.6.1.4.1.55555.1.1.0"
    editor.metrics.fields.variables["name"].set("cpu_usage")
    editor.metrics.fields.variables["oid"].set(oid)
    editor.metrics.fields.variables["initial"].set("5")
    editor.tabs.select(editor.scenario_tab)
    click(editor.rules, "Добавить")
    editor.rules.fields.variables["oid"].set(oid)
    editor.rules.fields.variables["type"].set("sequence")

    def set_values():
        dialog = next(widget for widget in widgets(editor.rules) if isinstance(widget, tk.Toplevel))
        try:
            entry = next(widget for widget in widgets(dialog) if isinstance(widget, ttk.Entry))
            items = next(widget for widget in widgets(dialog) if isinstance(widget, tk.Listbox))
            for value in ("15", "20", "30"):
                entry.delete(0, "end")
                entry.insert(0, value)
                click(dialog, "Добавить")
            items.selection_set(1)
            entry.delete(0, "end")
            entry.insert(0, "25")
            click(dialog, "Изменить")
            items.selection_clear(0, "end")
            items.selection_set(2)
            click(dialog, "Удалить")
            click(dialog, "Готово")
        finally:
            if dialog.winfo_exists():
                dialog.destroy()

    editor.window.after_idle(set_values)
    editor.rules.fields.controls["values"].invoke()
    assert editor.rules.fields.variables["values"].get() == "Значения… (2)"

    editor.scenario_sections.select(editor.modes_tab)
    click(editor.modes, "Добавить")
    editor.modes.fields.variables["oid"].set(oid)
    editor.modes.fields.variables["mode"].set("noSuchInstance")
    device, scenario = tmp_path / "new-device.json", tmp_path / "new-scenario.json"
    choose_paths(monkeypatch, "asksaveasfilename", device, scenario)
    click(editor.window, "Сохранить для запуска…")

    document = json.loads(scenario.read_text(encoding="utf-8"))
    assert document["rules"] == [{"type": "sequence", "oid": oid, "values": [15, 25]}]
    assert document["oid_responses"] == [{"oid": oid, "mode": "noSuchInstance"}]


def test_outage_validation_and_unsupported_file_keep_loaded_pair(window, tmp_path, monkeypatch):
    _, panel = window
    panel.mode.set("preset")
    panel._change_mode()
    panel.profile.set("Демо ТЗ · 4/5 · Полная недоступность")
    panel.editor_button.invoke()
    editor = panel.editor
    device, scenario = editor.paths
    editor.outage.variables["duration"].set("999")
    click(editor.window, "Проверить")
    assert "interval" in editor.status.get()
    assert editor.outage.errors["duration"].cget("text")
    assert editor.tabs.select() == str(editor.scenario_tab)

    unknown = tmp_path / "unsupported.json"
    contents = {**json.loads(scenario.read_text(encoding="utf-8")), "unknown": 1}
    unknown.write_text(json.dumps(contents), encoding="utf-8")
    monkeypatch.setattr(gui_editor.messagebox, "askyesno", lambda *_args, **_kwargs: True)
    choose_paths(monkeypatch, "askopenfilename", device, unknown)
    click(editor.window, "Открыть пару…")

    assert "не поддерживается" in editor.status.get()
    assert editor.paths == (device, scenario)
    assert editor.outage.variables["duration"].get() == "999"
    assert json.loads(unknown.read_text(encoding="utf-8")) == contents

    editor.outage.variables["duration"].set("3")
    saved_device = tmp_path / "edited-device.json"
    saved_scenario = tmp_path / "edited-scenario.json"
    choose_paths(monkeypatch, "asksaveasfilename", saved_device, saved_scenario)
    click(editor.window, "Сохранить для запуска…")
    saved = json.loads(saved_scenario.read_text(encoding="utf-8"))
    assert saved["unavailability"] == {"at": 2, "duration": 3}


def test_cancelled_save_leaves_editor_and_files_intact(window, tmp_path, monkeypatch):
    _, panel = window
    editor = open_step_editor(panel)
    paths = panel.selected_paths()
    editor.rules.fields.variables["limit"].set("70")
    device = tmp_path / "device.json"
    device.write_text("old contents", encoding="utf-8")
    choose_paths(monkeypatch, "asksaveasfilename", device, "")

    click(editor.window, "Сохранить для запуска…")
    assert editor.window.winfo_exists()
    assert panel.selected_paths() == paths
    assert editor.rules.fields.variables["limit"].get() == "70"
    assert device.read_text(encoding="utf-8") == "old contents"

    choose_paths(monkeypatch, "asksaveasfilename", device, device)
    click(editor.window, "Сохранить для запуска…")
    assert "разные файлы" in editor.status.get()
    assert device.read_text(encoding="utf-8") == "old contents"
