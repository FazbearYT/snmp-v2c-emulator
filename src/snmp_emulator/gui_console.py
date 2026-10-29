from __future__ import annotations

import queue
import tkinter as tk
from collections import deque
from datetime import datetime
from tkinter import ttk

from .gui_controller import PanelSnapshot
from .snmp_console import (
    COMMAND_HELP,
    LOCAL_COMMANDS,
    ConsoleClient,
    parse_request,
    subtree_root,
    suggestions,
)


class SnmpConsoleWindow:
    def __init__(self, parent: tk.Misc) -> None:
        self.window = tk.Toplevel(parent)
        self.window.withdraw()
        self.window.title("SNMP · Консоль запросов")
        self.window.geometry("1000x700")
        self.window.minsize(760, 560)
        self.window.protocol("WM_DELETE_WINDOW", self.hide)
        self.client = ConsoleClient()
        self.snapshot = PanelSnapshot()
        self.ready = False
        self._suggestions: list[tuple[str, str]] = []
        self._history: deque[str] = deque(maxlen=100)
        self._history_index = 0
        self.command = tk.StringVar(master=self.window)
        self.hint = tk.StringVar(master=self.window, value="Выберите шаблон команды ниже")
        self.target = tk.StringVar(
            master=self.window, value="Сначала запустите эмулятор в главном окне"
        )
        self._build()
        self.command.trace_add("write", self._update_suggestions)
        self._update_suggestions()
        self._append(
            "Консоль выполняет реальные SNMP v2c-запросы по UDP.\n"
            "Шаблон подставляет команду; Enter или «Выполнить» отправляет запрос.\n"
            "Tab — первая подсказка, ↑/↓ — история. HELP — справка, CLEAR — очистка.\n"
        )

    def _build(self) -> None:
        frame = ttk.Frame(self.window, padding=18)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="SNMP-запросы к работающему эмулятору", style="Status.TLabel").pack(
            anchor="w"
        )
        ttk.Label(frame, textvariable=self.target).pack(anchor="w", pady=(6, 12))
        templates = ttk.Frame(frame)
        templates.pack(fill="x")
        self.template_buttons = {}
        for operation in COMMAND_HELP:
            button = ttk.Button(
                templates, text=operation, command=lambda op=operation: self.template(op)
            )
            button.pack(side="left", padx=(0, 8))
            self.template_buttons[operation] = button
        ttk.Label(frame, textvariable=self.hint, wraplength=900).pack(anchor="w", pady=(10, 6))
        command_row = ttk.Frame(frame)
        command_row.pack(fill="x")
        self.entry = ttk.Entry(command_row, textvariable=self.command, font=("Consolas", 11))
        self.entry.pack(side="left", fill="x", expand=True)
        self.entry.bind("<Tab>", self._complete_first)
        self.entry.bind("<Up>", lambda _: self._recall(-1))
        self.entry.bind("<Down>", lambda _: self._recall(1))
        self.run_button = ttk.Button(
            command_row, text="Выполнить", command=self.execute, state="disabled"
        )
        self.run_button.pack(side="left", padx=8)
        self.cancel_button = ttk.Button(
            command_row, text="Отменить", command=self.client.cancel, state="disabled"
        )
        self.cancel_button.pack(side="left")
        choices_frame = ttk.Frame(frame)
        choices_frame.pack(fill="x", pady=(6, 10))
        self.choices = tk.Listbox(
            choices_frame, height=4, exportselection=False, font=("Consolas", 10)
        )
        self.choices_scrollbar = ttk.Scrollbar(choices_frame, command=self.choices.yview)
        self.choices.configure(yscrollcommand=self.choices_scrollbar.set)
        self.choices_scrollbar.pack(side="right", fill="y")
        self.choices.pack(side="left", fill="x", expand=True)
        self.choices.bind("<<ListboxSelect>>", self._complete_selected)
        self.choices.bind("<MouseWheel>", self._scroll_choices)
        self.choices.bind("<Button-4>", lambda _: self._scroll_choices_units(-1))
        self.choices.bind("<Button-5>", lambda _: self._scroll_choices_units(1))
        output_frame = ttk.Frame(frame)
        output_frame.pack(fill="both", expand=True)
        self.output = tk.Text(
            output_frame,
            state="disabled",
            wrap="none",
            background="#152232",
            foreground="#e3edf8",
            font=("Consolas", 11),
            padx=12,
            pady=12,
        )
        vertical = ttk.Scrollbar(output_frame, command=self.output.yview)
        horizontal = ttk.Scrollbar(output_frame, orient="horizontal", command=self.output.xview)
        self.output.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        output_frame.columnconfigure(0, weight=1)
        output_frame.rowconfigure(0, weight=1)
        self.output.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")

        for widget in (self.entry, self.choices, self.run_button, *self.template_buttons.values()):
            widget.bind("<Return>", self._submit_key)
            widget.bind("<KP_Enter>", self._submit_key)

    def _submit_key(self, _event: tk.Event) -> str:
        self.execute()
        return "break"

    def _focus_command_entry(self) -> None:
        self.entry.icursor("end")
        self.entry.focus_set()
        self.window.after_idle(self.entry.focus_set)

    def _append(self, text: str) -> None:
        self.output.configure(state="normal")
        self.output.insert("end", text + "\n")
        if int(self.output.index("end-1c").split(".")[0]) > 1200:
            self.output.delete("1.0", "end-1000l")
        self.output.see("end")
        self.output.configure(state="disabled")

    def show(self) -> None:
        self.window.deiconify()
        self.window.lift()
        self._focus_command_entry()

    def hide(self) -> None:
        self.client.cancel()
        self.window.withdraw()

    def refresh(self, snapshot: PanelSnapshot, ready: bool) -> None:
        self._drain_output()
        session_changed = snapshot.session != self.snapshot.session
        if session_changed:
            self._event(f"Сеанс {snapshot.session} · {snapshot.profile}")
        if session_changed or snapshot.state != self.snapshot.state:
            message = {
                "starting": "Загрузка конфигурации и запуск",
                "running": f"Эмулятор запущен · UDP {snapshot.address}",
                "completed": "Сценарий завершён · последние значения доступны по SNMP",
                "stopped": "Эмулятор остановлен · UDP-порт освобождён",
                "error": f"Ошибка эмулятора: {snapshot.error}",
            }.get(snapshot.state)
            if message:
                self._event(message)
        changed = [(m.name, m.oid) for m in snapshot.metrics] != [
            (m.name, m.oid) for m in self.snapshot.metrics
        ]
        self.snapshot = snapshot
        self.ready = ready
        self.target.set(
            f"Сеанс {snapshot.session} · SNMP v2c · UDP {snapshot.address}"
            if ready
            else "Эмулятор остановлен. Запустите его в главном окне."
        )
        self._update_run_button()
        self.cancel_button.configure(state="normal" if self.client.active else "disabled")
        if changed:
            self._update_suggestions()

    def _event(self, message: str) -> None:
        self._append(f"[{datetime.now().astimezone():%Y-%m-%d %H:%M:%S %z}] {message}")

    def _update_run_button(self) -> None:
        local = self.command.get().strip().upper() in LOCAL_COMMANDS
        enabled = local or (self.ready and not self.client.active)
        self.run_button.configure(state="normal" if enabled else "disabled")

    def _drain_output(self) -> None:
        while True:
            try:
                self._append(self.client.output.get_nowait())
            except queue.Empty:
                break

    def template(self, operation: str) -> None:
        metrics = self.snapshot.metrics
        oid = str(metrics[0].oid) if metrics and operation == "GET" else subtree_root(metrics)
        self.command.set(
            operation
            if operation in LOCAL_COMMANDS
            else f"{operation} {oid}" + (" 5" if operation == "GETBULK" else "")
        )
        self._focus_command_entry()

    def _update_suggestions(self, *_args: object) -> None:
        text = self.command.get()
        operation = text.split()[0].upper() if text.split() else ""
        self.hint.set(
            COMMAND_HELP.get(operation, "Выберите команду; для SNMP введите OID или имя метрики")
        )
        self._suggestions = suggestions(text, self.snapshot.metrics)
        self.choices.delete(0, "end")
        for label, _ in self._suggestions:
            self.choices.insert("end", label)
        self._update_run_button()

    def _scroll_choices(self, event: tk.Event) -> str:
        units = (
            -max(1, abs(event.delta) // 120) if event.delta > 0 else max(1, abs(event.delta) // 120)
        )
        return self._scroll_choices_units(units)

    def _scroll_choices_units(self, units: int) -> str:
        self.choices.yview_scroll(units, "units")
        return "break"

    def _complete_first(self, _event: object = None) -> str:
        if self._suggestions:
            self.command.set(self._suggestions[0][1])
            self.entry.icursor("end")
        return "break"

    def _complete_selected(self, _event: object = None) -> None:
        selected = self.choices.curselection()
        if selected:
            replacement = self._suggestions[selected[0]][1]
            self.command.set(replacement)
            self._focus_command_entry()

    def _recall(self, step: int) -> str:
        self._history_index = min(len(self._history), max(0, self._history_index + step))
        self.command.set(
            self._history[self._history_index] if self._history_index < len(self._history) else ""
        )
        self.entry.icursor("end")
        return "break"

    def help(self) -> None:
        self._append(
            "\n".join(COMMAND_HELP.values()) + "\nWALK использует GETNEXT до конца ветки.\n"
            "GETBULK: non-repeaters=0, количество — max-repetitions (по умолчанию 5).\n"
            "OID можно вводить вручную, включая отсутствующие, или выбрать из подсказок.\n"
            "Таймаут ответа 1 с, без повторов. WALK: до 256 метрик, максимум 15 с.\n"
        )

    def execute(self) -> None:
        text = self.command.get().strip()
        if text.upper() == "HELP":
            self.help()
            return
        if text.upper() == "CLEAR":
            self.output.configure(state="normal")
            self.output.delete("1.0", "end")
            self.output.configure(state="disabled")
            return
        if not self.ready:
            self._append("Сначала запустите эмулятор в главном окне.")
            return
        if self.client.active:
            self._append("Дождитесь ответа или отмените текущий запрос.")
            return
        try:
            request = parse_request(text, self.snapshot.metrics)
        except ValueError as exc:
            self._append(f"Ошибка команды: {exc}")
            return
        host, port = self.snapshot.address.rsplit(":", 1)
        if self.client.start(request, host, int(port), self.snapshot.community):
            self._history.append(text)
            self._history_index = len(self._history)
            self.run_button.configure(state="disabled")
