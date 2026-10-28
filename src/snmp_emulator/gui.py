from __future__ import annotations

import sys
import tkinter as tk
from collections import deque
from pathlib import Path
from tkinter import filedialog, ttk

from .gui_console import SnmpConsoleWindow
from .gui_controller import PanelController, PanelSnapshot, examples_directory

PROFILES = {
    "Стабильные показатели": "stable.json",
    "Скачок температуры": "temperature-jump.json",
    "Рост загрузки процессора": "cpu-step.json",
}
METRIC_LABELS = {
    "cpu_usage": "Загрузка процессора",
    "temperature": "Температура",
    "description": "Описание устройства",
    "memory_usage": "Использование памяти",
    "status": "Состояние устройства",
}
STATES = {
    "idle": "Готов к запуску",
    "starting": "Загрузка и запуск…",
    "running": "Эмулятор работает",
    "completed": "Сценарий завершён · значения доступны по SNMP",
    "stopped": "Эмулятор остановлен · порт освобождён",
    "error": "Не удалось запустить или продолжить работу",
}


class EmulatorPanel:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.controller = PanelController()
        self.console: SnmpConsoleWindow | None = None
        self._closing = False
        self._stopping = False
        self._history: deque[tuple[float, dict[str, int]]] = deque(maxlen=600)
        self._numeric_names: list[str] = []
        self._examples = examples_directory()
        self.mode = tk.StringVar(value="preset")
        self.profile = tk.StringVar(value=next(iter(PROFILES)))
        self.device = tk.StringVar(value=str(self._examples / "device.json"))
        self.scenario = tk.StringVar(value=str(self._examples / "stable.json"))
        self.status = tk.StringVar(value=STATES["idle"])
        self.details = tk.StringVar(value="Адрес и порт будут прочитаны из устройства")
        self.error = tk.StringVar()
        self.selected_metric = tk.StringVar()
        self._build()
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(100, self._refresh)

    def _build(self) -> None:
        root = self.root
        root.title("SNMP · Панель эмулятора")
        root.geometry("1060x780")
        root.minsize(850, 680)
        root.configure(background="#edf2f7")
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("TFrame", background="#edf2f7")
        style.configure("TLabel", background="#edf2f7", foreground="#24354b", font=("Segoe UI", 10))
        style.configure("Title.TLabel", font=("Segoe UI", 22, "bold"))
        style.configure("Status.TLabel", font=("Segoe UI", 13, "bold"))
        style.configure("Error.TLabel", foreground="#a32727")
        style.configure("TButton", padding=(14, 8), font=("Segoe UI", 10))
        style.configure("Treeview", rowheight=30, font=("Segoe UI", 10))
        style.configure("Treeview.Heading", font=("Segoe UI", 10, "bold"))
        frame = ttk.Frame(root, padding=24)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(8, weight=1)
        frame.rowconfigure(10, weight=1)
        ttk.Label(frame, text="Эмулятор SNMP", style="Title.TLabel").grid(
            row=0, column=0, columnspan=3, sticky="w"
        )
        ttk.Label(
            frame, text="Выберите пример, запустите устройство и наблюдайте за показателями."
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(4, 18))
        modes = ttk.Frame(frame)
        modes.grid(row=2, column=0, columnspan=3, sticky="w", pady=(0, 8))
        self._inputs: list[ttk.Widget] = []
        for value, label in (("preset", "Готовый пример"), ("custom", "Свои JSON-файлы")):
            button = ttk.Radiobutton(
                modes, text=label, value=value, variable=self.mode, command=self._change_mode
            )
            button.pack(side="left", padx=(0, 24))
            self._inputs.append(button)
        self.preset_frame = ttk.Frame(frame)
        self.preset_frame.grid(row=3, column=0, columnspan=3, sticky="ew")
        self.preset_frame.columnconfigure(1, weight=1)
        ttk.Label(self.preset_frame, text="Демонстрация").grid(row=0, column=0, padx=(0, 16))
        self.profiles = ttk.Combobox(
            self.preset_frame, textvariable=self.profile, values=list(PROFILES), state="readonly"
        )
        self.profiles.grid(row=0, column=1, sticky="ew", pady=4)
        self._inputs.append(self.profiles)
        self.files_frame = ttk.Frame(frame)
        self.files_frame.grid(row=4, column=0, columnspan=3, sticky="ew")
        self.files_frame.columnconfigure(1, weight=1)
        for row, label, variable in (
            (0, "Устройство JSON", self.device),
            (1, "Сценарий JSON", self.scenario),
        ):
            ttk.Label(self.files_frame, text=label).grid(
                row=row, column=0, sticky="w", padx=(0, 16)
            )
            entry = ttk.Entry(self.files_frame, textvariable=variable, state="readonly")
            entry.grid(row=row, column=1, sticky="ew", pady=4)
            button = ttk.Button(
                self.files_frame, text="Выбрать файл…", command=lambda v=variable: self._browse(v)
            )
            button.grid(row=row, column=2, padx=(8, 0))
            self._inputs.extend((entry, button))
        self._change_mode()
        actions = ttk.Frame(frame)
        actions.grid(row=5, column=0, columnspan=3, sticky="ew", pady=16)
        self.start_button = ttk.Button(actions, text="Запустить", command=self.start)
        self.start_button.grid(row=0, column=0)
        self.stop_button = ttk.Button(
            actions, text="Остановить", command=self.stop, state="disabled"
        )
        self.stop_button.grid(row=0, column=1, padx=8)
        ttk.Button(actions, text="SNMP-запросы…", command=self.open_console).grid(row=0, column=2)
        ttk.Label(actions, textvariable=self.status, style="Status.TLabel", wraplength=740).grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(8, 0)
        )
        ttk.Label(frame, textvariable=self.details).grid(row=6, column=0, columnspan=3, sticky="w")
        ttk.Label(frame, textvariable=self.error, style="Error.TLabel", wraplength=940).grid(
            row=7, column=0, columnspan=3, sticky="w", pady=(4, 8)
        )
        table_frame = ttk.Frame(frame)
        table_frame.grid(row=8, column=0, columnspan=3, sticky="nsew")
        self.table = ttk.Treeview(
            table_frame, columns=("name", "value", "type", "oid"), show="headings", height=5
        )
        for name, title, width in (
            ("name", "Показатель", 230),
            ("value", "Текущее значение", 180),
            ("type", "Тип", 100),
            ("oid", "OID", 250),
        ):
            self.table.heading(name, text=title)
            self.table.column(name, width=width, minwidth=70)
        scrollbar = ttk.Scrollbar(table_frame, command=self.table.yview)
        self.table.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.table.pack(fill="both", expand=True)
        chart_header = ttk.Frame(frame)
        chart_header.grid(row=9, column=0, columnspan=3, sticky="ew", pady=(16, 6))
        ttk.Label(chart_header, text="Изменение показателя").pack(side="left", padx=(0, 12))
        self.metric_picker = ttk.Combobox(
            chart_header, textvariable=self.selected_metric, state="readonly"
        )
        self.metric_picker.pack(side="left")
        self.metric_picker.bind("<<ComboboxSelected>>", lambda _: self._draw_chart())
        self.chart = tk.Canvas(frame, background="white", height=170, highlightthickness=0)
        self.chart.grid(row=10, column=0, columnspan=3, sticky="nsew")
        self.chart.bind("<Configure>", lambda _: self._draw_chart())
        ttk.Label(frame, text="График: последние 60 секунд · время от запуска, с").grid(
            row=11, column=0, columnspan=3, sticky="w", pady=(6, 0)
        )

    def _change_mode(self) -> None:
        if self.mode.get() == "preset":
            self.preset_frame.grid()
            self.files_frame.grid_remove()
        else:
            self.preset_frame.grid_remove()
            self.files_frame.grid()

    def selected_paths(self) -> tuple[Path, Path]:
        if self.mode.get() == "preset":
            return self._examples / "device.json", self._examples / PROFILES[self.profile.get()]
        return Path(self.device.get()), Path(self.scenario.get())

    def _browse(self, variable: tk.StringVar) -> None:
        path = filedialog.askopenfilename(
            parent=self.root, title="Выберите готовый JSON-файл", filetypes=[("JSON", "*.json")]
        )
        if path:
            variable.set(path)

    def start(self) -> None:
        if self.controller.start(*self.selected_paths()):
            self._stopping = False
            self._history.clear()
            self._numeric_names.clear()
            self.table.delete(*self.table.get_children())
            self.error.set("")
            self._set_active(True)

    def _set_active(self, active: bool) -> None:
        self.start_button.configure(state="disabled" if active else "normal")
        self.stop_button.configure(state="normal" if active and not self._stopping else "disabled")
        for widget in self._inputs:
            state = "disabled" if active else "normal"
            if not active and isinstance(widget, (ttk.Entry, ttk.Combobox)):
                state = "readonly"
            widget.configure(state=state)

    def stop(self) -> None:
        self._stopping = True
        if self.console is not None:
            self.console.client.cancel()
            self.console.refresh(self.controller.snapshot(), False)
        self.controller.stop()
        if self.controller.active:
            self.status.set("Остановка…")
            self.stop_button.configure(state="disabled")

    def close(self) -> None:
        self._closing = True
        self.controller.stop()
        if self.console is not None:
            self.console.client.cancel()
        self.status.set("Завершение работы…")
        self._set_active(True)
        self.stop_button.configure(state="disabled")

    def _refresh(self) -> None:
        if self._closing:
            if not self.controller.active and (
                self.console is None or not self.console.client.active
            ):
                self.root.destroy()
                return
        else:
            snapshot = self.controller.snapshot()
            if self.console is not None:
                self.console.refresh(
                    snapshot,
                    self.controller.active
                    and not self._stopping
                    and snapshot.state in ("running", "completed"),
                )
            self.status.set(
                "Остановка…"
                if self._stopping and self.controller.active
                else STATES[snapshot.state]
            )
            self.error.set(snapshot.error)
            self._set_active(self.controller.active)
            if snapshot.metrics:
                self._show_metrics(snapshot)
        self.root.after(100, self._refresh)

    def open_console(self) -> None:
        if self.console is None:
            self.console = SnmpConsoleWindow(self.root)
        snapshot = self.controller.snapshot()
        self.console.refresh(
            snapshot,
            self.controller.active
            and not self._stopping
            and snapshot.state in ("running", "completed"),
        )
        self.console.show()

    def _show_metrics(self, snapshot: PanelSnapshot) -> None:
        duration = f" / {snapshot.duration:g} с" if snapshot.duration is not None else " с"
        self.details.set(
            f"UDP {snapshot.address} · community: {snapshot.community} · "
            f"время {snapshot.elapsed:.1f}{duration}"
        )
        numeric = {}
        for metric in snapshot.metrics:
            values = (
                METRIC_LABELS.get(metric.name, metric.name),
                str(metric.value),
                metric.data_type.value,
                str(metric.oid),
            )
            if self.table.exists(metric.name):
                self.table.item(metric.name, values=values)
            else:
                self.table.insert("", "end", iid=metric.name, values=values)
            if isinstance(metric.value, int):
                numeric[metric.name] = metric.value
        if list(numeric) != self._numeric_names:
            self._numeric_names = list(numeric)
            labels = [METRIC_LABELS.get(name, name) for name in numeric]
            self.metric_picker.configure(values=labels)
            self.selected_metric.set(labels[0] if labels else "")
        if not self._history or snapshot.elapsed > self._history[-1][0]:
            self._history.append((snapshot.elapsed, numeric))
        self._draw_chart()

    def _draw_chart(self) -> None:
        canvas = self.chart
        canvas.delete("all")
        index = self.metric_picker.current()
        if index < 0 or not self._history:
            canvas.create_text(
                24,
                24,
                anchor="nw",
                text="Запустите пример с числовыми показателями",
                fill="#53657c",
            )
            return
        name = self._numeric_names[index]
        samples = [(elapsed, values[name]) for elapsed, values in self._history if name in values]
        width, height = canvas.winfo_width(), canvas.winfo_height()
        left, top, right, bottom = 64, 20, width - 24, height - 30
        low, high = min(v for _, v in samples), max(v for _, v in samples)
        if high == low:
            low, high = low - 1, high + 1
        first, last = samples[0][0], samples[-1][0]
        canvas.create_line(left, top, left, bottom, right, bottom, fill="#c8d3e0")
        for value, y in ((high, top), (low, bottom)):
            canvas.create_text(left - 8, y, anchor="e", text=str(value), fill="#53657c")
        canvas.create_text(left, bottom + 15, text=f"{first:.1f}", fill="#53657c")
        canvas.create_text(right, bottom + 15, text=f"{last:.1f}", fill="#53657c")
        points = []
        for elapsed, value in samples:
            points.extend(
                (
                    left + (elapsed - first) / max(last - first, 1) * (right - left),
                    bottom - (value - low) / (high - low) * (bottom - top),
                )
            )
        if len(points) >= 4:
            canvas.create_line(*points, fill="#2376c8", width=3)
        x, y = points[-2:]
        canvas.create_oval(x - 4, y - 4, x + 4, y + 4, fill="#2376c8", outline="")
        canvas.create_text(
            right,
            top,
            anchor="ne",
            text=str(samples[-1][1]),
            fill="#24354b",
            font=("Segoe UI", 16, "bold"),
        )


def main() -> int:
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print(f"Не удалось открыть GUI: {exc}. Нужны Tk и графическая сессия.", file=sys.stderr)
        return 1
    EmulatorPanel(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
