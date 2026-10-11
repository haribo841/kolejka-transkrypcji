"""Local recordings view sharing checkpoints and one worker with YouTube."""
from __future__ import annotations

from collections import Counter
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .common import WorkerLock, atomic_json, job_folder, read_json
from .importers import MEDIA, import_local
from .store import RETRYABLE, STATUSES
from .ui_help import (HELP, Tooltip, collapsible_help, duration_text, help_button,
                      status_tags, sync_tree, wrap_with_parent)
from .local_outputs import OUTPUT_MODES, scan_media
from .readiness import readiness_summary

ENABLED_STATE = "!disabled"


def folder_media(folder, recursive=True, m4a_only=False, excluded=()):
    extensions = {".m4a"} if m4a_only else MEDIA
    return scan_media(folder, extensions, recursive, excluded)


class LocalQueueTab:
    def __init__(self, app, parent):
        self.app, self.parent = app, parent
        self.visible, self.controls = {}, {}
        self.filter_after = None
        state = read_json(app.root / "lokalne-ui.json", {})
        self.folder = tk.StringVar(master=parent, value=state.get("folder", ""))
        self.recursive = tk.BooleanVar(master=parent, value=state.get("recursive", True))
        self.m4a_only = tk.BooleanVar(master=parent, value=state.get("m4a_only", False))
        self.output_mode = tk.StringVar(master=parent, value=OUTPUT_MODES.get(state.get("output_mode", "beside"), OUTPUT_MODES["beside"]))
        self.output_folder = tk.StringVar(master=parent, value=state.get("output_folder", ""))
        self.search = tk.StringVar(master=parent)
        self.status = tk.StringVar(master=parent, value="Wszystkie")
        self.selected_only = tk.BooleanVar(master=parent, value=False)
        self.note = tk.StringVar(master=parent, value="Dodaj audio lub wideo. Import nie uruchamia transkrypcji.")
        self.counts, self.current, self.detail = (tk.StringVar(master=parent) for _ in range(3))
        self.hint = tk.StringVar(master=parent, value="Start obejmuje tylko pliki lokalne. Miejsce wyników zapisujemy osobno dla każdego nagrania.")
        frame = ttk.Frame(parent, padding=12)
        frame.pack(fill="both", expand=True)
        toolbar = ttk.Frame(frame)
        toolbar.pack(fill="x", pady=(0, 6))
        for key, text, action, hint in (
                ("next", "Bieżący / następny", lambda: app.jump_to_next("local"), HELP["next"]),
                ("configuration", "Konfiguracja", app.open_configuration, HELP["configuration"]),
                ("test", "Sprawdź na fragmencie", app.test_local, HELP["test"]),
                ("csv", "Raport CSV", lambda: app.report(kind="local"), "Zapisuje raport wyłącznie lokalnych nagrań.")):
            self.button(toolbar, key, text, action, hint).pack(side="left", padx=(0, 8))
        ttk.Label(frame, textvariable=app.local_model_state, wraplength=1200).pack(anchor="w", pady=(0, 6))
        source = ttk.LabelFrame(frame, text="Dodaj nagrania", padding=(8, 5))
        source.pack(fill="x")
        row = ttk.Frame(source)
        row.pack(fill="x")
        ttk.Label(row, text="Folder:").pack(side="left", padx=(0, 8))
        entry = ttk.Entry(row, textvariable=self.folder)
        entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.help(entry, "folder_path", "Folder źródłowy. Wybór nie uruchamia importu ani transkrypcji.")
        self.button(row, "browse", "Wybierz folder", self.choose_folder, "Wybiera folder źródłowy.").pack(side="left", padx=(0, 6))
        self.button(row, "import_folder", "Dodaj folder", self.add_folder, HELP["folder"]).pack(side="left", padx=(0, 6))
        self.button(row, "files", "Dodaj pliki", self.add_files, HELP["files"]).pack(side="left")
        row = ttk.Frame(source)
        row.pack(fill="x", pady=(5, 0))
        for key, text, variable in (("recursive", "Podfoldery", self.recursive), ("m4a_only", "Tylko M4A", self.m4a_only)):
            toggle = ttk.Checkbutton(row, text=text, variable=variable, command=self.save_view)
            toggle.pack(side="left", padx=(0, 12))
            self.help(toggle, key, "Filtr importu folderu. Domyślnie obsługiwane są wszystkie wymienione formaty audio i wideo.")
        ttk.Label(row, text="Wyniki nowych nagrań:").pack(side="left", padx=(0, 6))
        mode = ttk.Combobox(row, textvariable=self.output_mode, values=list(OUTPUT_MODES.values()), state="readonly", width=19)
        mode.pack(side="left", padx=(0, 8))
        mode.bind("<<ComboboxSelected>>", self.output_changed)
        self.help(mode, "output_mode", "Obok źródła: osobny podfolder. Folder centralny: H:\\Transkrypcje\\wyniki. Wskazany folder: własna lokalizacja. Dotychczasowe zadania zachowują swoje miejsca.")
        self.output_entry = ttk.Entry(row, textvariable=self.output_folder)
        self.output_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.output_entry.bind("<FocusOut>", lambda event: self.save_view())
        self.output_entry.bind("<Return>", lambda event: self.save_view())
        self.output_browse = self.button(row, "output_browse", "Wybierz folder wyników", self.choose_output,
                                          "Wybiera wspólny folder wyników nowych nagrań.")
        self.output_browse.pack(side="left")
        self.update_output_controls()
        note = ttk.Label(frame, textvariable=self.note, wraplength=1200, foreground="#596579")
        note.pack(anchor="w", pady=4)
        wrap_with_parent(note, 24)
        filters = ttk.Frame(frame)
        filters.pack(fill="x", pady=(0, 6))
        ttk.Label(filters, text="Szukaj:").pack(side="left", padx=(0, 6))
        search = ttk.Entry(filters, textvariable=self.search, width=42)
        search.pack(side="left", padx=(0, 12))
        self.help(search, "search", "Filtruje lokalne nagrania według tytułu lub ścieżki.")
        ttk.Label(filters, text="Status:").pack(side="left", padx=(0, 6))
        status = ttk.Combobox(filters, textvariable=self.status, values=["Wszystkie", *STATUSES.values()], state="readonly", width=22)
        status.pack(side="left", padx=(0, 12))
        self.help(status, "status", HELP["status"])
        self.search.trace_add("write", self.schedule_filter)
        self.status.trace_add("write", self.schedule_filter)
        listing = ttk.Frame(frame)
        listing.pack(fill="both", expand=True)
        columns = ("enabled", "status", "stage", "progress", "duration", "format", "title", "language", "audio")
        self.tree = ttk.Treeview(listing, columns=columns, show="headings", selectmode="extended", height=12)
        for column, title, width in zip(columns, ("Kolejka", "Status", "Etap", "Etap %", "Czas trwania", "Format", "Nagranie", "Język", "Audio"),
                                        (55, 125, 185, 55, 100, 65, 530, 60, 50)):
            self.tree.heading(column, text=title)
            self.tree.column(column, width=width, minwidth=40, stretch=column == "title")
        vertical = ttk.Scrollbar(listing, orient="vertical", command=self.tree.yview)
        horizontal = ttk.Scrollbar(listing, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        listing.rowconfigure(0, weight=1)
        listing.columnconfigure(0, weight=1)
        for tag, color in (("done", "#25723b"), ("error", "#ae2020")):
            self.tree.tag_configure(tag, foreground=color)
        self.tree.bind("<<TreeviewSelect>>", self.show_detail)
        self.tree.bind("<Control-a>", self.select_visible)
        self.tree.bind("<Double-1>", lambda event: app.open_results(self.selected()))
        self.controls["list"] = self.tree
        Tooltip(self.tree, HELP["list"], self.hint, popup=False)
        selection = ttk.Frame(frame)
        selection.pack(fill="x", pady=6)
        for key, text, action, hint in (
                ("select", "Zaznacz widoczne", self.select_visible, HELP["select"]),
                ("enable", "Włącz do kolejki", lambda: app.enable(True, self.selected()), HELP["enable"]),
                ("disable", "Wyłącz z kolejki", lambda: app.enable(False, self.selected()), HELP["disable"]),
                ("language", "Ustaw język", lambda: app.language(self.selected()), HELP["language"]),
                ("track", "Wybierz ścieżkę audio", lambda: app.audio_track(self.selected()), HELP["track"]),
                ("output", "Zmień miejsce wyników", self.change_output, "Stosuje wybrany sposób zapisu do zaznaczonych nagrań, które nie rozpoczęły przetwarzania. Gotowych i częściowych wyników nie przenosi.")):
            self.button(selection, key, text, action, hint).pack(side="left", padx=(0, 6))
        for variable in (self.counts, self.current, self.detail):
            label = ttk.Label(frame, textvariable=variable, wraplength=1200)
            label.pack(anchor="w", pady=(0, 3))
            wrap_with_parent(label, 24)
        self.progress = ttk.Progressbar(frame, maximum=100)
        self.progress.pack(fill="x", pady=(0, 6))
        self.controls["progress"] = self.progress
        Tooltip(self.progress, HELP["progress"], self.hint, popup=False)
        actions = ttk.Frame(frame)
        actions.pack(fill="x")
        for key, text, action, hint in (
                ("start", "Start/Wznów lokalne", self.start, "Przetwarza tylko lokalną kolejkę."),
                ("stop", "Dokończ bieżący i zatrzymaj", lambda: app.stop(kind="local"), HELP["stop"]),
                ("retry", "Ponów nieudane", lambda: app.retry(kind="local", ids=self.selected()), HELP["retry"]),
                ("results", "Otwórz wyniki", lambda: app.open_results(self.selected()), HELP["results"]),
                ("transcript", "Otwórz TXT", lambda: app.open_transcript(self.selected()), HELP["transcript"])):
            self.button(actions, key, text, action, hint).pack(side="left", padx=(0, 6))
        selected = ttk.Checkbutton(actions, text="Start tylko zaznaczonych", variable=self.selected_only)
        selected.pack(side="left")
        self.help(selected, "selected_only", HELP["selected_only"])
        collapsible_help(frame, self.hint)

    def help(self, widget, key, hint):
        self.controls[key] = widget
        Tooltip(widget, hint, self.hint)

    def button(self, parent, key, text, command, hint):
        button = help_button(parent, text, command, hint, self.hint)
        self.controls[key] = button
        return button

    def save_view(self):
        if self.app.closing:
            return
        atomic_json(self.app.root / "lokalne-ui.json", {"folder": self.folder.get(),
                    "recursive": self.recursive.get(), "m4a_only": self.m4a_only.get(),
                    "output_mode": self.output_policy()[0], "output_folder": self.output_folder.get()})

    def output_policy(self):
        mode = next(key for key, value in OUTPUT_MODES.items() if value == self.output_mode.get())
        return mode, self.output_folder.get().strip()

    def update_output_controls(self):
        custom = self.output_mode.get() == OUTPUT_MODES["custom"]
        self.output_entry.configure(state="normal" if custom else "disabled")
        self.output_browse.state([ENABLED_STATE] if custom else ["disabled"])

    def output_changed(self, event=None):
        self.update_output_controls()
        self.save_view()

    def choose_output(self):
        folder = filedialog.askdirectory(title="Wspólny folder wyników lokalnych nagrań", parent=self.app.window)
        if folder:
            self.output_folder.set(folder)
            self.save_view()

    def change_output(self):
        if WorkerLock.busy(self.app.root):
            self.note.set("Najpierw dokończ bieżące nagranie i zatrzymaj sesję.")
            return
        try:
            mode, custom = self.output_policy()
            count = self.app.store.change_output(self.selected(), mode, custom)
            self.note.set(f"Zmieniono miejsce wyników dla {count} nieprzetworzonych nagrań.")
            self.app.jobs = {job["id"]: job for job in self.app.store.jobs()}
            self.render()
        except (ValueError, OSError, RuntimeError) as exc:
            self.note.set(str(exc))
            messagebox.showerror("Miejsce wyników", str(exc), parent=self.app.window)

    def set_folder(self, path):
        self.folder.set(str(path))
        self.save_view()

    def choose_folder(self):
        initial = self.folder.get()
        path = filedialog.askdirectory(title="Folder lokalnych nagrań", parent=self.app.window,
                    initialdir=initial if initial and Path(initial).is_dir() else None)
        if path:
            self.set_folder(path)

    def add_folder(self):
        value = self.folder.get().strip()
        if not value:
            self.choose_folder()
            value = self.folder.get().strip()
        if not value:
            return
        recursive, m4a = self.recursive.get(), self.m4a_only.get()
        self.save_view()
        self.note.set("Wyszukiwanie i identyfikacja nagrań...")
        excluded = [job_folder(self.app.root, job) for job in self.app.store.jobs()]
        self.import_paths(lambda: folder_media(value, recursive, m4a, excluded))

    def add_files(self):
        paths = filedialog.askopenfilenames(parent=self.app.window, title="Lokalne nagrania do transkrypcji",
                    filetypes=[("Audio i wideo", " ".join("*" + ext for ext in sorted(MEDIA))), ("Wszystkie pliki", "*.*")])
        if paths:
            self.import_paths(lambda: paths)

    def import_paths(self, collect):
        mode, custom = self.output_policy()
        if mode == "custom" and not custom:
            self.note.set("Wskaż folder wyników przed importem.")
            return
        self.save_view()
        config = dict(self.app.config)
        def run():
            result = import_local(self.app.store, collect(), lambda value: self.app.events.put(("progress", value, self.note)),
                                  config=config, output_mode=mode, output_root=custom)
            result["note"] = (f"Znaleziono: {result['files']} | Dodano: {result['added']} | "
                              f"Kopie lub już w kolejce: {result['files'] - result['added']} | Błędy odczytu: {result['errors']}. Import zakończony.")
            return result
        self.app.background(run, note=self.note)

    def selected(self):
        return [int(item) for item in self.tree.selection()]

    def select_visible(self, event=None):
        self.tree.selection_set(self.tree.get_children())
        return "break"

    def schedule_filter(self, *_):
        if self.filter_after:
            self.parent.after_cancel(self.filter_after)
        self.filter_after = self.parent.after(200, self.filter_now)

    def filter_now(self):
        self.filter_after = None
        self.render()

    def render(self):
        rows = [job for job in self.app.jobs.values() if job["kind"] == "local"]
        visible_rows = []
        for job in rows:
            if self.search.get().casefold() not in (job["title"] + " " + job["source"]).casefold():
                continue
            if self.status.get() != "Wszystkie" and STATUSES[job["status"]] != self.status.get():
                continue
            item = str(job["id"])
            values = ("Tak" if job["enabled"] else "", STATUSES[job["status"]], job["stage"],
                      f"{job['progress']:.0f}", duration_text(job["duration"]), Path(job["source"]).suffix.upper().lstrip("."),
                      job["title"], job["language"], job["audio_track"] + 1)
            visible_rows.append((item, values, status_tags(job["status"])))
        sync_tree(self.tree, self.visible, visible_rows)
        self.show_detail()
        counts = Counter(job["status"] for job in rows)
        pending = sum(job["status"] == "pending" and job["enabled"] for job in rows)
        self.counts.set(f"Lokalne: {len(rows)} | Gotowe: {counts['done']} | Oczekujące: {counts['pending']} (włączone: {pending}) | Problemy: {sum(counts[s] for s in RETRYABLE)}")

    def refresh(self, control, alive):
        self.render()
        job = self.app.jobs.get(control["current_id"])
        scope = job["kind"] if job else control.get("scope_kind", "all")
        self.show_current(control, alive, job, scope)
        self.controls["start"].state(["disabled"] if alive or self.app.busy_import or self.app.metadata_busy or
                                      not readiness_summary(self.app.readiness, "local")[0] else [ENABLED_STATE])
        self.controls["stop"].state([ENABLED_STATE] if alive and scope == "local" else ["disabled"])

    def show_current(self, control, alive, job, scope):
        if alive and scope == "local":
            suffix = " | Zatrzymam się po tym nagraniu." if control["stop"] else ""
            self.current.set((f"{job['title']} | {job['stage']}" if job else "Przygotowanie lokalnej sesji...") + suffix)
            self.progress["value"] = job["progress"] if job else 0
        elif alive:
            self.current.set("Trwa inna sesja. Dokończ bieżący i zatrzymaj ją w karcie YouTube, potem uruchom kolejkę lokalną.")
            self.progress["value"] = 0
        else:
            message = control["message"][:500] if scope == "local" else ""
            failure = read_json(self.app.root / "ostatni-blad-start.json", {})
            if not message and failure.get("kind") == "local":
                message = failure.get("message", "")
            self.current.set("Kolejka lokalna zatrzymana. " + message)
            self.progress["value"] = 0

    def show_detail(self, event=None):
        ids = self.selected()
        if len(ids) == 1 and ids[0] in self.app.jobs:
            job = self.app.jobs[ids[0]]
            self.detail.set(f"Źródło: {job['source']}\nWyniki: {job_folder(self.app.root, job)}" +
                            ("\nBłąd: " + job["error"][:600] if job["error"] else ""))
        else:
            self.detail.set(f"Zaznaczono: {len(ids)} | Zaznacz jedno nagranie, aby zobaczyć jego miejsce wyników.")

    def start(self):
        ids = self.selected() if self.selected_only.get() else None
        if ids == []:
            messagebox.showinfo("Start lokalnej kolejki", "Zaznacz lokalne nagrania do przetworzenia.", parent=self.app.window)
            return
        self.app.start(kind="local", job_ids=ids)

    def close(self):
        if self.filter_after:
            self.parent.after_cancel(self.filter_after)
            self.filter_after = None
