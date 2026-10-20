"""A single place for pipeline settings and clearly dated verification."""
from __future__ import annotations

from pathlib import Path
import tkinter as tk
from tkinter import filedialog, ttk

from .readiness import ACTION, AVAILABLE, CHECKING, OFF, PASSED, device_text, test_time
from .ui_help import Tooltip, help_button, wrap_with_parent


class ConfigurationTab:
    def __init__(self, app, parent):
        self.app = app
        self.wizard = None
        self.variables, self.labels, self.messages = {}, {}, {}
        self.paths = {}
        frame = ttk.Frame(parent, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Konfiguracja i próba działania", font=("Segoe UI", 16, "bold")).pack(anchor="w")
        ttk.Label(frame, text="Obecność plików pozwala wykonać próbę. Zielony status potwierdza przetworzenie fragmentu z bieżącymi ustawieniami.").pack(anchor="w", pady=(3, 10))
        tools = ttk.Frame(frame)
        tools.pack(fill="x", pady=(0, 12))
        help_button(tools, "Kreator krok po kroku", self.open_wizard,
                    "Prowadzi przez modele, opcjonalny YouTube i końcową próbę. Dalej staje się dostępne po zakończeniu czynności.").pack(side="left", padx=(0, 8))
        help_button(tools, "Sprawdź konfigurację", lambda: app.request_readiness(force=True),
                    "Sprawdza pliki i środowiska w tle, bez uruchamiania modeli i kolejki.").pack(side="left", padx=(0, 8))
        help_button(tools, "Sprawdź na fragmencie", app.test_local,
                    "Przetwarza do 60 sekund wybranego lokalnego pliku lub nagrania wskazanego w oknie wyboru.").pack(side="left", padx=(0, 8))
        help_button(tools, "Otwórz wyniki próby", self.open_test,
                    "Otwiera folder wyników ostatniej zakończonej próby.").pack(side="left")
        content = ttk.Frame(frame)
        canvas = tk.Canvas(content, highlightthickness=0,
                           background=ttk.Style(parent).lookup("TFrame", "background") or "#f0f0f0")
        scrollbar = ttk.Scrollbar(content, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        grid = ttk.Frame(canvas)
        embedded = canvas.create_window((0, 0), window=grid, anchor="nw")
        grid.bind("<Configure>", lambda event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(embedded, width=event.width))
        app.window.bind("<MouseWheel>", lambda event: self.scroll(event, canvas), add="+")
        app.window.bind("<FocusIn>", lambda event: self.show_focused(event, canvas, grid), add="+")
        for index, (key, title) in enumerate((("whisper", "Whisper"), ("diarization", "Rozróżnianie mówców"),
                                               ("uvr", "UVR i zachowane audio"), ("tools", "Narzędzia i dysk"),
                                               ("youtube", "Dostęp do YouTube"))):
            box = ttk.LabelFrame(grid, text=title, padding=10)
            box.grid(row=index // 2, column=index % 2, sticky="nsew", padx=(0, 10), pady=(0, 10))
            status, message = tk.StringVar(master=parent, value=CHECKING), tk.StringVar(master=parent)
            self.variables[key], self.messages[key] = status, message
            label = ttk.Label(box, textvariable=status, font=("Segoe UI", 11, "bold"))
            label.pack(anchor="w")
            self.labels[key] = label
            note = ttk.Label(box, textvariable=message, wraplength=540)
            note.pack(fill="x", pady=(3, 8))
            wrap_with_parent(note, 24)
            getattr(self, "build_" + key)(box)
        grid.columnconfigure(0, weight=1, uniform="components")
        grid.columnconfigure(1, weight=1, uniform="components")
        self.last_test = tk.StringVar(master=parent, value="Brak próby aktualnej konfiguracji.")
        summary = ttk.Label(frame, textvariable=self.last_test, wraplength=1200)
        summary.pack(side="bottom", fill="x", pady=(8, 0))
        wrap_with_parent(summary, 24)
        content.pack(fill="both", expand=True)

    def open_wizard(self):
        if self.wizard and not self.wizard.closed:
            self.wizard.dialog.lift()
            return
        from .config_wizard import ConfigurationWizard
        self.wizard = ConfigurationWizard(self.app)

    def scroll(self, event, canvas):
        if self.app.notebook.select() == str(self.app.config_page):
            canvas.yview_scroll(-int(event.delta / 120), "units")

    def show_focused(self, event, canvas, grid):
        if not str(event.widget).startswith(str(grid) + "."):
            return
        top = event.widget.winfo_rooty() - grid.winfo_rooty()
        bottom = top + event.widget.winfo_height()
        visible_top = canvas.canvasy(0)
        if top < visible_top:
            canvas.yview_moveto(top / max(1, grid.winfo_height()))
        elif bottom > visible_top + canvas.winfo_height():
            canvas.yview_moveto((bottom - canvas.winfo_height()) / max(1, grid.winfo_height()))

    def path_entry(self, box, title, key, directory=False):
        ttk.Label(box, text=title).pack(anchor="w")
        row = ttk.Frame(box)
        row.pack(fill="x", pady=(1, 5))
        variable = tk.StringVar(master=box, value=self.app.config.get(key, ""))
        self.paths[key] = variable
        entry = ttk.Entry(row, textvariable=variable)
        entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        Tooltip(entry, "Zmień ścieżkę i zatwierdź klawiszem Enter albo wybierz przyciskiem.", self.app.help_status)
        entry.bind("<Return>", lambda event: self.save_path(key))
        help_button(row, "Zapisz", lambda: self.save_path(key), "Zapisuje wpisaną ścieżkę i ponownie sprawdza konfigurację.").pack(side="left", padx=(0, 6))
        help_button(row, "Wybierz", lambda: self.choose_path(key, directory), "Wybiera lokalny folder lub program.").pack(side="left")

    def save_path(self, key):
        if not self.app.save_setting(key, self.paths[key].get().strip()):
            self.paths[key].set(self.app.config.get(key, ""))

    def choose_path(self, key, directory):
        method = filedialog.askdirectory if directory else filedialog.askopenfilename
        path = method(parent=self.app.window, title="Wybierz " + key)
        if path and self.app.save_setting(key, path):
            self.paths[key].set(path)

    def build_whisper(self, box):
        self.path_entry(box, "Folder modelu OpenVINO:", "model", directory=True)
        ttk.Label(box, text="Transkrypcja lokalna na CPU, język auto lub ustawiony dla nagrania.").pack(anchor="w")

    def build_diarization(self, box):
        self.diarization = tk.BooleanVar(master=box, value=self.app.config.get("diarization", True))
        toggle = ttk.Checkbutton(box, text="Rozróżniaj mówców", variable=self.diarization,
                                command=lambda: self.save_diarization())
        toggle.pack(anchor="w")
        Tooltip(toggle, "Wyłączenie pomija pyannote i jego wymagania podczas próby oraz transkrypcji.")
        self.app.controls["speakers"] = help_button(box, "Konfiguracja Community-1", self.app.configure_model,
            "Otwiera przewodnik pobierania Community-1. Token Hugging Face typu Read wystarcza.")
        self.app.controls["speakers"].pack(anchor="w", pady=(6, 0))

    def save_diarization(self):
        if not self.app.save_setting("diarization", self.diarization.get()):
            self.diarization.set(self.app.config.get("diarization", True))

    def build_uvr(self, box):
        for key, text, variable in (("uvr", "Transkrybuj wokal wydzielony przez UVR", self.app.uvr_enabled),
                                    ("keep", "Zachowuj źródłowe audio", self.app.keep_audio)):
            widget = ttk.Checkbutton(box, text=text, variable=variable, command=self.app.save_audio_options)
            widget.pack(anchor="w", pady=2)
            self.app.add_help(widget, key)
        self.app.controls["uvr_model"] = help_button(box, "Pobierz / sprawdź model UVR", self.app.setup_uvr,
            "Przygotowuje UVR. Sprawdź na fragmencie potwierdza później faktyczne działanie.")
        self.app.controls["uvr_model"].pack(anchor="w", pady=(5, 0))

    def build_tools(self, box):
        self.path_entry(box, "FFmpeg:", "ffmpeg")
        self.path_entry(box, "FFprobe:", "ffprobe")

    def build_youtube(self, box):
        self.path_entry(box, "Node.js:", "node")
        help_button(box, "Dostęp YouTube", self.app.configure_youtube,
                    "Konfiguracja sesji i test jednego linku. Nie uruchamia kolejki.").pack(anchor="w")

    def open_test(self):
        result = self.app.readiness.get("test", {}).get("results")
        if result and Path(result).is_dir():
            import os
            os.startfile(result)
        else:
            self.last_test.set("Nie ma dostępnych wyników próby. Kliknij Sprawdź na fragmencie.")

    def checking(self):
        for key in self.variables:
            self.variables[key].set(CHECKING)
            self.labels[key].configure(foreground="#596579")

    def update(self, report):
        colors = {PASSED: "#25723b", ACTION: "#ae2020", AVAILABLE: "#916100", OFF: "#777777", CHECKING: "#596579"}
        for key, state in report["components"].items():
            self.variables[key].set(state["status"])
            self.messages[key].set(state["message"])
            self.labels[key].configure(foreground=colors[state["status"]])
        test = report.get("test", {})
        if report.get("passed"):
            devices = test.get("devices", {})
            self.last_test.set(f"Próba zaliczona: {test_time(test.get('at', ''))} | Whisper: {devices.get('whisper', '')} | "
                               f"Mówcy: {device_text(devices.get('diarization', [])) or 'wyłączeni'} | "
                               f"UVR: {device_text(devices.get('uvr', [])) or 'wyłączony'}")
        elif test.get("error"):
            self.last_test.set("Ostatnia próba: " + test_time(test.get("at", "")) + " | Błąd: " + test["error"][:300])
        else:
            self.last_test.set("Brak próby aktualnej konfiguracji. Wybierz nagranie i kliknij Sprawdź na fragmencie.")
