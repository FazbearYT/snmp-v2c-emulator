from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .config import AgentConfig, ConfigurationError
from .config_editor import (
    EditorValidationError,
    read_device,
    read_scenario,
    save_pair,
    validate_pair,
)
from .domain.oid import ObjectIdentifier
from .domain.types import SnmpDataType

TEXT_TYPES = (SnmpDataType.OCTET_STRING, SnmpDataType.OBJECT_IDENTIFIER, SnmpDataType.IP_ADDRESS)
RULE_FIELDS = {
    "constant": ("value",),
    "jump": ("at", "value"),
    "step": ("step", "limit"),
    "sequence": ("values",),
}
JSON_FILES = [("JSON", "*.json")]


def number(value, convert):
    try:
        return convert(value)
    except ValueError:
        return value


def normalize_oid(value):
    try:
        return str(ObjectIdentifier.parse(value))
    except ValueError:
        return value


class Fields(ttk.Frame):
    def __init__(self, parent, side_by_side=False):
        super().__init__(parent)
        self.variables = {}
        self.controls = {}
        self.errors = {}
        self.rows = {}
        self.side_by_side = side_by_side

    def add(self, name, label, choices=(), editable=False, command=None):
        index = len(self.variables)
        row, column = (0, index) if self.side_by_side else (index, 0)
        self.columnconfigure(column, weight=1)
        field = ttk.Frame(self, padding=(0, 0, 8 if self.side_by_side else 0, 5))
        field.grid(row=row, column=column, sticky="nsew")
        field.columnconfigure(0, weight=1)
        variable = tk.StringVar(self)
        ttk.Label(field, text=label).grid(row=0, sticky="w")
        if command:
            control = ttk.Button(field, textvariable=variable, command=command)
        elif choices or editable:
            control = ttk.Combobox(
                field,
                width=15,
                textvariable=variable,
                values=choices,
                state="normal" if editable else "readonly",
            )
        else:
            control = ttk.Entry(field, width=15, textvariable=variable)
        control.grid(row=1, sticky="ew")
        error = ttk.Label(field, style="Error.TLabel", wraplength=240)
        error.grid(row=2, sticky="w")
        error.grid_remove()
        self.variables[name] = variable
        self.controls[name] = control
        self.errors[name] = error
        self.rows[name] = field
        return variable

    def values(self):
        return {key: variable.get() for key, variable in self.variables.items()}

    def set_values(self, values):
        for key, variable in self.variables.items():
            variable.set(str(values.get(key, "")))

    def error(self, name, message):
        label = self.errors.get(name)
        if label is None:
            return False
        label.configure(text=message)
        if message:
            label.grid()
        else:
            label.grid_remove()
        return True

    def clear_errors(self):
        for key in self.errors:
            self.error(key, "")

    def destroy(self):
        super().destroy()
        self.variables.clear()


class RowsForm(ttk.Frame):
    def __init__(self, parent, columns, fields, defaults, height=4, oid_choices=None):
        super().__init__(parent)
        self.columns = columns
        self.defaults = defaults
        self.rows = []
        self.row_errors = {}
        self.current = None
        self.traces = {}
        self.columnconfigure(0, weight=3)
        self.columnconfigure(1, weight=2, minsize=305)
        self.rowconfigure(0, weight=1)

        table = ttk.Frame(self)
        table.grid(row=0, column=0, sticky="nsew", padx=(0, 16))
        table.columnconfigure(0, weight=1)
        table.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(
            table,
            columns=[name for name, _, _ in columns],
            show="headings",
            height=height,
            selectmode="browse",
        )
        for name, title, width in columns:
            self.tree.heading(name, text=title)
            self.tree.column(name, width=width, minwidth=38)
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.tree.tag_configure("invalid", foreground="#a32727")
        scrollbar = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.bind("<<TreeviewSelect>>", self._select)
        toolbar = ttk.Frame(table)
        toolbar.grid(row=1, column=0, sticky="w", pady=(5, 0))
        ttk.Button(toolbar, text="Добавить", command=self.add).pack(side="left")
        self.remove_button = ttk.Button(toolbar, text="Удалить", command=self.remove)
        self.remove_button.pack(side="left", padx=(6, 0))

        self.fields = Fields(self, side_by_side="mode" in defaults)
        self.fields.grid(row=0, column=1, sticky="new")
        for name, title, choices in fields:
            variable = self.fields.add(
                name,
                title,
                choices,
                editable=name == "oid" and oid_choices is not None,
                command=self._edit_values if name == "values" else None,
            )
            if name == "oid" and oid_choices is not None:
                control = self.fields.controls[name]
                control.configure(
                    postcommand=lambda widget=control: widget.configure(values=oid_choices())
                )
            if name != "values":
                self.traces[name] = variable.trace_add(
                    "write", lambda *_, key=name: self._edit(key)
                )
        self.error_label = ttk.Label(self, style="Error.TLabel", wraplength=700)
        self.error_label.grid(row=1, column=0, columnspan=2, sticky="w")
        self.set_rows([])

    def destroy(self):
        for key, trace in self.traces.items():
            self.fields.variables[key].trace_remove("write", trace)
        super().destroy()

    def set_rows(self, rows, index=0):
        self.current = None
        self.rows = [
            {
                key: list(row.get(key, ())) if key == "values" else str(row.get(key, default))
                for key, default in self.defaults.items()
            }
            for row in rows
        ]
        self.row_errors.clear()
        self.error_label.configure(text="")
        self.tree.delete(*self.tree.get_children())
        for row_index in range(len(self.rows)):
            self.tree.insert("", "end", iid=str(row_index))
            self._update_row(row_index)
        if self.rows:
            self.select(index)
        else:
            self._show_row(None)

    def select(self, index):
        if 0 <= index < len(self.rows):
            item = str(index)
            self.tree.selection_set(item)
            self.tree.see(item)
            self._show_row(index)

    def add(self):
        row = self.defaults.copy()
        if "values" in row:
            row["values"] = []
        self.set_rows([*self.rows, row], len(self.rows))
        next(iter(self.fields.controls.values())).focus_set()

    def remove(self):
        if self.current is not None:
            index = self.current
            rows = self.rows[:index] + self.rows[index + 1 :]
            self.set_rows(rows, min(index, len(rows) - 1))

    def _select(self, _event):
        selected = self.tree.selection()
        index = int(selected[0]) if selected else None
        if index != self.current:
            self._show_row(index)

    def _show_row(self, index):
        self.current = None
        self.remove_button.configure(state="disabled" if index is None else "normal")
        self.fields.clear_errors()
        row = self.rows[index] if index is not None else {}
        self.fields.set_values(row)
        if "values" in self.fields.variables:
            self.fields.variables["values"].set(f"Значения… ({len(row.get('values', []))})")
        self.current = index
        self._show_rule_fields()
        if index is not None:
            self.fields.grid()
            for key, message in self.row_errors.get(index, {}).items():
                self.fields.error(key, message)
        else:
            self.fields.grid_remove()

    def _show_rule_fields(self):
        if "type" not in self.fields.variables or "values" not in self.fields.variables:
            return
        rule_type = self.fields.variables["type"].get()
        for name in ("at", "value", "step", "limit", "values"):
            if name in RULE_FIELDS.get(rule_type, ()):
                self.fields.rows[name].grid()
            else:
                self.fields.rows[name].grid_remove()

    def _edit(self, name):
        if self.current is not None:
            self.rows[self.current][name] = self.fields.variables[name].get()
            self._update_row(self.current)
            self._show_rule_fields()

    def _update_row(self, index):
        row = self.rows[index]
        self.tree.item(
            str(index),
            values=[row[name] for name, _, _ in self.columns],
            tags=("invalid",) if index in self.row_errors else (),
        )

    def _edit_values(self):
        if self.current is None:
            return
        row = self.rows[self.current]
        dialog = tk.Toplevel(self)
        dialog.title("Значения последовательности")
        dialog.transient(self.winfo_toplevel())
        frame = ttk.Frame(dialog, padding=12)
        frame.pack(fill="both", expand=True)
        items = tk.Listbox(frame, height=7, width=40, exportselection=False)
        items.pack(fill="both", expand=True)
        values = list(row["values"])
        for value in values:
            items.insert("end", value)
        entry = ttk.Entry(frame)
        entry.pack(fill="x", pady=6)

        def select_value(_event):
            if items.curselection():
                index = items.curselection()[0]
                entry.delete(0, "end")
                entry.insert(0, values[index])

        def add_value():
            values.append(entry.get())
            items.insert("end", entry.get())

        def change_value():
            if items.curselection():
                index = items.curselection()[0]
                values[index] = entry.get()
                items.delete(index)
                items.insert(index, values[index])
                items.selection_set(index)

        def delete_value():
            if items.curselection():
                index = items.curselection()[0]
                values.pop(index)
                items.delete(index)

        def done():
            row["values"] = values
            self.fields.variables["values"].set(f"Значения… ({len(values)})")
            dialog.destroy()

        items.bind("<<ListboxSelect>>", select_value)
        toolbar = ttk.Frame(frame)
        toolbar.pack(fill="x")
        for text, command in (
            ("Добавить", add_value),
            ("Изменить", change_value),
            ("Удалить", delete_value),
            ("Готово", done),
        ):
            ttk.Button(toolbar, text=text, command=command).pack(side="left", padx=(0, 4))
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        dialog.grab_set()
        entry.focus_set()
        self.wait_window(dialog)
        self.winfo_toplevel().grab_set()

    def clear_errors(self):
        indices = list(self.row_errors)
        self.row_errors.clear()
        for index in indices:
            self._update_row(index)
        self.fields.clear_errors()
        self.error_label.configure(text="")

    def show_error(self, field, message):
        if len(field) > 1 and isinstance(field[0], int) and field[0] < len(self.rows):
            index, key = field[:2]
            if key in self.fields.errors:
                self.row_errors.setdefault(index, {})[key] = message
                self._update_row(index)
                if index == self.current:
                    self.fields.error(key, message)
                return
        self.error_label.configure(text=message)


class ConfigEditor:
    def __init__(
        self, parent: tk.Tk, paths: tuple[Path, Path], on_saved: Callable[[Path, Path], None]
    ) -> None:
        self.window = tk.Toplevel(parent)
        self.window.title("SNMP · Редактор конфигураций")
        self.window.geometry("990x740")
        self.window.minsize(800, 670)
        self.window.transient(parent)
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self.paths = None
        self.directory = paths[0].parent
        self.on_saved = on_saved
        self.source = tk.StringVar(self.window)
        self.status = tk.StringVar(self.window)

        frame = ttk.Frame(self.window, padding=18)
        frame.pack(fill="both", expand=True)
        toolbar = ttk.Frame(frame)
        toolbar.pack(fill="x", pady=(0, 10))
        ttk.Button(toolbar, text="Новая пара", command=self.new).pack(side="left")
        ttk.Button(toolbar, text="Открыть пару…", command=self.open).pack(side="left", padx=8)
        ttk.Label(toolbar, textvariable=self.source).pack(side="left", padx=6)

        self.tabs = ttk.Notebook(frame)
        self.tabs.pack(fill="both", expand=True)
        self.device_tab = ttk.Frame(self.tabs, padding=12)
        self.scenario_tab = ttk.Frame(self.tabs, padding=12)
        self.tabs.add(self.device_tab, text="Устройство")
        self.tabs.add(self.scenario_tab, text="Сценарий")
        self._build_device()
        self._build_scenario()

        footer = ttk.Frame(frame)
        footer.pack(fill="x", pady=(8, 0))
        self.status_label = ttk.Label(footer, textvariable=self.status, wraplength=900)
        self.status_label.pack(anchor="w", pady=(0, 6))
        ttk.Button(footer, text="Проверить", command=self.validate).pack(side="left")
        ttk.Button(footer, text="Сохранить для запуска…", command=self.save).pack(
            side="left", padx=8
        )
        ttk.Button(footer, text="Закрыть", command=self.close).pack(side="right")
        self._set_documents()
        self.load(*paths)
        self.window.grab_set()

    def _build_device(self):
        agent_box = ttk.LabelFrame(self.device_tab, text="Параметры агента", padding=8)
        agent_box.pack(fill="x", pady=(0, 8))
        self.agent = Fields(agent_box, side_by_side=True)
        self.agent.pack(fill="x")
        self.agent.add("host", "Адрес IPv4")
        self.agent.add("port", "Порт UDP")
        self.agent.add("community", "Community")

        metrics_box = ttk.LabelFrame(self.device_tab, text="Метрики устройства", padding=8)
        metrics_box.pack(fill="both", expand=True)
        self.metrics = RowsForm(
            metrics_box,
            (("name", "Название", 122), ("oid", "OID", 178), ("initial", "Начальное", 85)),
            (
                ("name", "Название метрики", ()),
                ("oid", "OID", ()),
                ("type", "Тип SNMP", tuple(SnmpDataType)),
                ("initial", "Начальное значение", ()),
            ),
            {"name": "", "oid": "", "type": "Integer32", "initial": "0", "access": "read-only"},
            height=8,
        )
        self.metrics.pack(fill="both", expand=True)

    def _build_scenario(self):
        settings = ttk.LabelFrame(
            self.scenario_tab, text="Время и доступность устройства", padding=8
        )
        settings.pack(fill="x", pady=(0, 8))
        self.times = Fields(settings, side_by_side=True)
        self.times.pack(fill="x")
        self.times.add("duration", "Длительность, с")
        self.times.add("update_interval", "Интервал обновления, с")

        self.outage_enabled = tk.BooleanVar(self.window)
        ttk.Checkbutton(
            settings,
            text="Полная недоступность",
            variable=self.outage_enabled,
            command=self._show_outage,
        ).pack(anchor="w")
        self.outage = Fields(settings, side_by_side=True)
        self.outage.add("at", "Начало недоступности, с")
        self.outage.add("duration", "Длительность недоступности, с")

        sections = ttk.Notebook(self.scenario_tab)
        sections.pack(fill="both", expand=True)
        rules_tab = ttk.Frame(sections, padding=8)
        modes_tab = ttk.Frame(sections, padding=8)
        sections.add(rules_tab, text="Изменения метрик")
        sections.add(modes_tab, text="Доступность OID")
        self.scenario_sections = sections
        self.rules_tab = rules_tab
        self.modes_tab = modes_tab

        self.rules = RowsForm(
            rules_tab,
            (("oid", "OID", 190), ("type", "Правило", 95)),
            (
                ("oid", "OID метрики", ()),
                ("type", "Правило", tuple(RULE_FIELDS)),
                ("at", "Момент скачка, с", ()),
                ("value", "Значение", ()),
                ("step", "Шаг (со знаком)", ()),
                ("limit", "Предел", ()),
                ("values", "Последовательность", ()),
            ),
            {
                "oid": "",
                "type": "constant",
                "at": "0",
                "value": "",
                "step": "1",
                "limit": "1",
                "values": [],
            },
            oid_choices=self.metric_oids,
        )
        self.rules.pack(fill="both", expand=True)
        self.modes = RowsForm(
            modes_tab,
            (("oid", "OID", 188), ("mode", "Режим ответа", 110)),
            (
                ("oid", "OID метрики", ()),
                ("mode", "Режим ответа", ("normal", "noSuchObject", "noSuchInstance", "genErr")),
            ),
            {"oid": "", "mode": "normal"},
            oid_choices=self.metric_oids,
        )
        self.modes.pack(fill="both", expand=True)

    def metric_oids(self):
        return [row["oid"] for row in self.metrics.rows if row["oid"]]

    def _show_outage(self):
        if self.outage_enabled.get():
            self.outage.pack(fill="x")
        else:
            self.outage.pack_forget()

    def _set_documents(self, device=None, scenario=None):
        device = device or {}
        scenario = scenario or {}
        self.agent.set_values(
            AgentConfig().model_dump() | device["agent"]
            if device
            else AgentConfig(host="127.0.0.1").model_dump()
        )
        self.metrics.set_rows(device.get("metrics", []))
        self.times.set_values({"duration": 10, "update_interval": 1} | scenario)
        self.rules.set_rows(scenario.get("rules", []))
        self.modes.set_rows(scenario.get("oid_responses", []))
        self.outage_enabled.set(scenario.get("unavailability") is not None)
        self.outage.set_values(scenario.get("unavailability") or {"at": 0, "duration": 1})
        self._show_outage()
        self._clear_errors()
        self.loaded_documents = self._documents()
        self.source.set(
            " + ".join(path.name for path in self.paths) if self.paths else "Новая пара"
        )

    def _documents(self):
        agent = self.agent.values()
        agent["port"] = number(agent["port"], int)
        metrics = [
            row | {"initial": self._metric_value(row["type"], row["initial"])}
            for row in self.metrics.rows
        ]
        types = {normalize_oid(row["oid"]): row["type"] for row in self.metrics.rows}
        rules = []
        for row in self.rules.rows:
            rule = {"oid": row["oid"], "type": row["type"]}
            for key in RULE_FIELDS[row["type"]]:
                value = row[key]
                data_type = types.get(normalize_oid(row["oid"]))
                if key == "values":
                    value = [self._metric_value(data_type, item) for item in value]
                elif key == "value":
                    value = self._metric_value(data_type, value)
                else:
                    value = number(value, float if key == "at" else int)
                rule[key] = value
            rules.append(rule)
        scenario = {key: number(value, float) for key, value in self.times.values().items()}
        scenario["rules"] = rules
        scenario["oid_responses"] = [row.copy() for row in self.modes.rows]
        if self.outage_enabled.get():
            scenario["unavailability"] = {
                key: number(value, float) for key, value in self.outage.values().items()
            }
        return {"agent": agent, "metrics": metrics}, scenario

    @staticmethod
    def _metric_value(data_type, value):
        if data_type is None or data_type in TEXT_TYPES or not isinstance(value, str):
            return value
        return number(value, int)

    def _clear_errors(self):
        for form in (self.agent, self.times, self.outage, self.metrics, self.rules, self.modes):
            form.clear_errors()
        self.status.set("")
        self.status_label.configure(style="TLabel")

    def _error(self, message):
        self.status.set(message)
        self.status_label.configure(style="Error.TLabel")

    def _show_errors(self, error):
        forms = {
            "agent": self.agent,
            "metrics": self.metrics,
            "rules": self.rules,
            "unavailability": self.outage,
            "oid_responses": self.modes,
        }
        for index, issue in enumerate(error.issues):
            first, *part = issue.field or ("",)
            form = forms.get(first)
            if isinstance(form, RowsForm):
                form.show_error(part, issue.message)
                if index == 0 and part and isinstance(part[0], int):
                    form.select(part[0])
            elif isinstance(form, Fields):
                form.error(part[0] if part else "duration", issue.message)
            elif first in self.times.variables:
                self.times.error(first, issue.message)
            if index == 0:
                self.tabs.select(
                    self.device_tab if issue.document == "device" else self.scenario_tab
                )
                if form is self.rules:
                    self.scenario_sections.select(self.rules_tab)
                if form is self.modes:
                    self.scenario_sections.select(self.modes_tab)
        if error.issues:
            self._error(f"Проверьте конфигурацию: {error.issues[0].message}")

    def validate(self):
        self._clear_errors()
        documents = self._documents()
        try:
            validate_pair(*documents)
        except EditorValidationError as exc:
            self._show_errors(exc)
            return None
        self.status.set("Конфигурация корректна.")
        return documents

    def _discard_changes(self):
        return self._documents() == self.loaded_documents or messagebox.askyesno(
            "Несохранённые изменения", "Продолжить без сохранения изменений?", parent=self.window
        )

    def load(self, device_path, scenario_path):
        try:
            device = read_device(device_path)
            scenario = read_scenario(scenario_path)
            validate_pair(device, scenario)
        except ConfigurationError as exc:
            self._error(f"Не удалось открыть пару: {exc}")
            return False
        self.paths = device_path, scenario_path
        self.directory = device_path.parent
        self._set_documents(device, scenario)
        self.status.set("Файлы открыты.")
        return True

    def new(self):
        if self._discard_changes():
            self.paths = None
            self._set_documents()
            self.metrics.add()
            self.loaded_documents = self._documents()
            self.tabs.select(self.device_tab)

    def open(self):
        if not self._discard_changes():
            return
        device = filedialog.askopenfilename(
            parent=self.window,
            title="Открыть устройство JSON",
            initialdir=self.directory,
            filetypes=JSON_FILES,
        )
        if device:
            scenario = filedialog.askopenfilename(
                parent=self.window,
                title="Открыть сценарий JSON",
                initialdir=Path(device).parent,
                filetypes=JSON_FILES,
            )
            if scenario:
                self.load(Path(device), Path(scenario))

    def save(self):
        documents = self.validate()
        if documents is None:
            return
        device = filedialog.asksaveasfilename(
            parent=self.window,
            title="Сохранить устройство JSON",
            initialdir=self.directory,
            initialfile="device-edited.json",
            defaultextension=".json",
            filetypes=JSON_FILES,
        )
        if not device:
            return
        scenario = filedialog.asksaveasfilename(
            parent=self.window,
            title="Сохранить сценарий JSON",
            initialdir=Path(device).parent,
            initialfile="scenario-edited.json",
            defaultextension=".json",
            filetypes=JSON_FILES,
        )
        if not scenario:
            return
        try:
            save_pair(Path(device), Path(scenario), *documents)
        except ConfigurationError as exc:
            self._error(f"Не удалось сохранить пару: {exc}")
            return
        self.on_saved(Path(device), Path(scenario))
        self.window.destroy()

    def close(self):
        if self._discard_changes():
            self.window.destroy()
