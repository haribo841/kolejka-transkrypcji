"""Local recordings view sharing checkpoints and one worker with YouTube."""
from __future__ import annotations

from collections import Counter
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import json
import os

from .common import WorkerLock, atomic_json, job_folder, read_json
from .importers import MEDIA, import_local
from .import_report import import_note
from .store import RETRYABLE, STATUSES, now
from .ui_help import (HELP, Tooltip, collapsible_help, duration_text, help_button,
                      session_mode, session_summary, startable, status_tags, sync_tree, wrap_with_parent)
from .table_view import TableView
from .local_outputs import OUTPUT_MODES, scan_media
from .session_button import SessionButton

ENABLED_STATE = "!disabled"
LOCAL_IMPORT_REPORT = "ostatni-import-lokalny.json"


def folder_media(folder, recursive=True, m4a_only=False, excluded=(), *, progress=lambda message: None,
                 include_technical=False):
    extensions = {".m4a"} if m4a_only else MEDIA
    return scan_media(folder, extensions, recursive, excluded, progress=progress, include_technical=include_technical)


class LocalQueueTab:
    def __init__(self, app, parent):
        self.app, self.parent = app, parent
        self.visible, self.controls = {}, {}
        self.filter_after = None
        state = read_json(app.root / "lokalne-ui.json", {})
        self.folder = tk.StringVar(master=parent, value=state.get("folder", ""))
        self.recursive = tk.BooleanVar(master=parent, value=state.get("recursive", True))
        self.m4a_only = tk.BooleanVar(master=parent, value=state.get("m4a_only", False))
        self.include_technical = tk.BooleanVar(master=parent, value=state.get("include_technical", False))
        self.output_mode = tk.StringVar(master=parent, value=OUTPUT_MODES.get(state.get("output_mode", "beside"), OUTPUT_MODES["beside"]))
        self.output_folder = tk.StringVar(master=parent, value=state.get("output_folder", ""))
        self.search = tk.StringVar(master=parent)
        self.status = tk.StringVar(master=parent, value="Wszystkie")
        self.note = tk.StringVar(master=parent, value="Dodaj folder... wybiera folder i dodaje jego nagrania do listy. Transkrypcję uruchamiasz osobno przyciskiem Start.")
        last_import = read_json(app.root / LOCAL_IMPORT_REPORT, {})
        if last_import.get("files") is not None:
            self.note.set(import_note(last_import))
        self.counts, self.current, self.detail = (tk.StringVar(master=parent) for _ in range(3))
        self.scope_summary = tk.StringVar(master=parent)
        self.hint = tk.StringVar(master=parent, value="Start obejmuje widoczne, włączone pliki lokalne w kolejności tabeli. Zmiany widoku podczas sesji obowiązują od następnego Start.")
        frame = ttk.Frame(parent, padding=12)
        frame.pack(fill="both", expand=True)
        toolbar = ttk.Frame(frame)
        toolbar.pack(fill="x", pady=(0, 6))
        for key, text, action, hint in (
                ("next", "Bieżący / następny", lambda: app.jump_to_next("local"), HELP["next"]),
                ("configuration", "Konfiguracja", app.open_configuration, HELP["configuration"]),
                ("test", "Sprawdź na fragmencie", app.test_local, HELP["test"]),
                ("import_report", "Raport importu", self.open_import_report, "Otwiera pełny raport ostatniego importu: pominięte źródła, duplikaty i przyczyny problemów."),
                ("csv", "Raport CSV", lambda: app.report(kind="local"), "Zapisuje raport wyłącznie lokalnych nagrań.")):
            self.button(toolbar, key, text, action, hint).pack(side="left", padx=(0, 8))
        ttk.Label(frame, textvariable=app.local_model_state, wraplength=1200).pack(anchor="w", pady=(0, 6))
        source = ttk.LabelFrame(frame, text="Dodaj nagrania", padding=(8, 5))
        source.pack(fill="x")
        row = ttk.Frame(source)
        row.pack(fill="x")
        ttk.Label(row, text="Folder źródłowy:").pack(side="left", padx=(0, 8))
        entry = ttk.Entry(row, textvariable=self.folder)
        entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        entry.bind("<Return>", lambda event: self.add_folder())
        self.help(entry, "folder_path", "Ścieżka folderu źródłowego. Wpisz lub wklej ścieżkę, potem kliknij Dodaj z podanej ścieżki albo naciśnij Enter.")
        self.button(row, "browse", "Dodaj folder...", self.choose_and_import_folder, HELP["folder"]).pack(side="left", padx=(0, 6))
        self.button(row, "import_folder", "Dodaj z podanej ścieżki", self.add_folder,
                    "Dodaje nagrania z folderu wpisanego obok. Przy ponownym skanowaniu pomija materiały już w kolejce. Nie uruchamia transkrypcji.").pack(side="left", padx=(0, 6))
        self.button(row, "files", "Dodaj pliki", self.add_files, HELP["files"]).pack(side="left")
        row = ttk.Frame(source)
        row.pack(fill="x", pady=(5, 0))
        for key, text, variable in (("recursive", "Podfoldery", self.recursive), ("m4a_only", "Tylko M4A", self.m4a_only),
                                    ("include_technical", "Pliki techniczne", self.include_technical)):
            toggle = ttk.Checkbutton(row, text=text, variable=variable, command=self.save_view)
            toggle.pack(side="left", padx=(0, 12))
            self.help(toggle, key, self.scan_help(key))
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
        self.tree = ttk.Treeview(listing, columns=columns, show="headings", selectmode="extended", height=6)
        for column, title, width in zip(columns, ("Kolejka", "Status", "Etap", "Etap %", "Czas trwania", "Format", "Nagranie", "Język", "Audio"),
                                        (55, 125, 185, 55, 100, 65, 530, 60, 50)):
            self.tree.heading(column, text=title)
            self.tree.column(column, width=width, minwidth=40, stretch=column == "title")
        vertical = ttk.Scrollbar(listing, orient="vertical", command=self.tree.yview)
        horizontal = ttk.Scrollbar(listing, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vertical.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        listing.rowconfigure(0, weight=1)
        listing.columnconfigure(0, weight=1)
        for tag, color in (("done", "#25723b"), ("error", "#ae2020"), ("partial", "#916100")):
            self.tree.tag_configure(tag, foreground=color)
        self.tree.bind("<<TreeviewSelect>>", self.show_detail)
        self.tree.bind("<Control-a>", self.select_visible)
        self.tree.bind("<Double-1>", self.open_row_results)
        self.controls["list"] = self.tree
        Tooltip(self.tree, HELP["list"], self.hint, popup=False)
        self.table_view = TableView(self.tree, app.root, "local", self.render, self.hint, scroll_set=horizontal.set)
        # Reserve the footer before allocating the remaining height to the table.
        # Recovery and stop controls must remain reachable at minimum window size.
        footer = ttk.Frame(frame)
        footer.pack(side="bottom", fill="x", before=listing)
        selection = ttk.Frame(footer)
        selection.pack(fill="x", pady=6)
        for key, text, action, hint in (
                ("select", "Zaznacz widoczne", self.select_visible, HELP["select"]),
                ("enable", "Włącz do kolejki", lambda: app.enable(True, self.selected()), HELP["enable"]),
                ("disable", "Wyłącz z kolejki", lambda: app.enable(False, self.selected()), HELP["disable"]),
                ("language", "Ustaw język", lambda: app.language(self.selected()), HELP["language"]),
                ("track", "Wybierz ścieżkę audio", lambda: app.audio_track(self.selected()), HELP["track"]),
                ("output", "Zmień miejsce wyników", self.change_output, "Stosuje wybrany sposób zapisu do zaznaczonych nagrań, które nie rozpoczęły przetwarzania. Gotowych i częściowych wyników nie przenosi.")):
            self.button(selection, key, text, action, hint).pack(side="left", padx=(0, 6))
        for variable in (self.counts, self.scope_summary, self.current, self.detail):
            label = ttk.Label(footer, textvariable=variable, wraplength=1200)
            label.pack(anchor="w", pady=(0, 3))
            wrap_with_parent(label, 24)
        self.progress = ttk.Progressbar(footer, maximum=100)
        self.progress.pack(fill="x", pady=(0, 6))
        self.controls["progress"] = self.progress
        Tooltip(self.progress, HELP["progress"], self.hint, popup=False)
        actions = ttk.Frame(footer)
        actions.pack(fill="x")
        for key, text, action, hint in (
                ("start", "Start/Wznów widoczne (0)", self.start, HELP["start"]),
                ("start_selected", "Start zaznaczonych (0)", self.start_selected, "Przetwarza wyłącznie zaznaczone nagrania. Po ostatnim z nich sesja się kończy."),
                ("stop", "Dokończ bieżący i zatrzymaj", lambda: app.stop(kind="local"), HELP["stop"])):
            self.button(actions, key, text, action, hint).pack(side="left", padx=(0, 6))
        recovery = ttk.Frame(footer)
        recovery.pack(fill="x", pady=(5, 0))
        for key, text, action, hint in (
                ("retry", "Ponów nieudane", lambda: app.retry(kind="local", ids=self.selected()), HELP["retry"]),
                ("resume", "Wznów brakującą część", self.start_selected, "Uruchamia zaznaczone nagranie od zachowanego punktu. Gotowy początek zostaje wykorzystany."),
                ("partial_txt", "Otwórz częściowy TXT", self.open_partial, "Otwiera zweryfikowany częściowy wynik zaznaczonego nagrania."),
                ("error_log", "Otwórz log błędu", self.open_error_log, "Otwiera zapis błędu zaznaczonego nagrania, np. UVR lub FFmpeg."),
                ("results", "Otwórz wyniki", lambda: app.open_results(self.selected()), HELP["results"]),
                ("transcript", "Otwórz TXT", lambda: app.open_transcript(self.selected()), HELP["transcript"])):
            self.button(recovery, key, text, action, hint).pack(side="left", padx=(0, 6))
        collapsible_help(footer, self.hint)

    def open_import_report(self):
        path = self.app.root / LOCAL_IMPORT_REPORT
        if path.is_file():
            os.startfile(str(path))
        else:
            self.note.set("Raport powstanie po dodaniu folderu lub plików.")

    def open_row_results(self, event):
        if self.tree.identify_region(event.x, event.y) in {"cell", "tree"} and self.tree.identify_row(event.y):
            self.app.open_results(self.selected())

    def help(self, widget, key, hint):
        self.controls[key] = widget
        Tooltip(widget, hint, self.hint)

    @staticmethod
    def scan_help(key):
        return {
            "recursive": "Przy następnym dodaniu folderu przeszuka wszystkie poziomy podfolderów. Pomija katalogi wyników, dowiązania i junctions. Sam checkbox nie dodaje plików.",
            "m4a_only": "Przy następnym dodaniu folderu wybierze tylko M4A. Wyłącz, aby importować pozostałe obsługiwane formaty audio i wideo.",
            "include_technical": "Domyślnie pomija potwierdzone środowiska i pakiety Pythona oraz tekstowe pliki .ts. Zaznacz, aby uwzględnić je przy następnym skanowaniu. Dodaj pliki pozwala wskazać je pojedynczo."
        }[key]

    def button(self, parent, key, text, command, hint):
        factory = SessionButton if key in {"start", "start_selected"} else help_button
        button = factory(parent, text, command, hint, self.hint)
        self.controls[key] = button
        return button

    def save_view(self):
        if self.app.closing:
            return
        atomic_json(self.app.root / "lokalne-ui.json", {"folder": self.folder.get(),
                    "recursive": self.recursive.get(), "m4a_only": self.m4a_only.get(),
                    "include_technical": self.include_technical.get(),
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
            return True
        return False

    def choose_and_import_folder(self):
        if self.choose_folder():
            self.add_folder()

    def add_folder(self):
        value = self.folder.get().strip()
        if not value:
            self.choose_folder()
            value = self.folder.get().strip()
        if not value:
            return
        recursive, m4a = self.recursive.get(), self.m4a_only.get()
        include_technical = self.include_technical.get()
        self.save_view()
        excluded = [job_folder(self.app.root, job) for job in self.app.store.jobs()]
        scope = "tylko M4A" if m4a else "audio i wideo"
        scope += ", z podfolderami" if recursive else ", bez podfolderów"
        notify = lambda message: self.app.events.put(("progress", message, self.note))
        self.import_paths(lambda: folder_media(value, recursive, m4a, excluded, progress=notify,
                                              include_technical=include_technical), folder_scope=scope)

    def add_files(self):
        paths = filedialog.askopenfilenames(parent=self.app.window, title="Lokalne nagrania do transkrypcji",
                    filetypes=[("Audio i wideo", " ".join("*" + ext for ext in sorted(MEDIA))), ("Wszystkie pliki", "*.*")])
        if paths:
            self.import_paths(lambda: paths)

    def import_paths(self, collect, folder_scope=None):
        if self.app.busy_import:
            messagebox.showinfo("Trwa import", "Poczekaj na zakończenie bieżącego importu.", parent=self.app.window)
            return
        mode, custom = self.output_policy()
        if mode == "custom" and not custom:
            self.note.set("Wskaż folder wyników przed importem.")
            return
        self.save_view()
        self.note.set("Wyszukiwanie i identyfikacja nagrań... Lista zostanie odświeżona po imporcie.")
        config = dict(self.app.config)
        def run():
            result = import_local(self.app.store, collect(), lambda value: self.app.events.put(("progress", value, self.note)),
                                  config=config, output_mode=mode, output_root=custom)
            result["local_import"] = True
            result["note"] = import_note(result, folder_scope)
            atomic_json(self.app.root / LOCAL_IMPORT_REPORT, {"at": now(), **result})
            return result
        self.app.background(run, note=self.note)

    def import_finished(self, result):
        """Show an import's committed jobs immediately on the Tk main thread."""
        self.search.set("")
        self.status.set("Wszystkie")
        if result.get("issues"):
            self.note.set(self.note.get() + " | Raport: " + str(self.app.root / LOCAL_IMPORT_REPORT))
        self.app.jobs = {job["id"]: job for job in self.app.store.jobs()}
        self.render()
        added = [str(ident) for ident in result.get("job_ids", []) if self.tree.exists(str(ident))]
        if added:
            self.tree.selection_set(added)
            self.tree.focus(added[0])
            self.tree.see(added[0])
            self.show_detail()

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

    def matches_view(self, job):
        return (job["kind"] == "local" and self.search.get().casefold() in
                (job["title"] + " " + job["source"]).casefold() and
                (self.status.get() == "Wszystkie" or STATUSES[job["status"]] == self.status.get()))

    def ordered_jobs(self, rows=None):
        rows = self.app.jobs.values() if rows is None else rows
        return self.table_view.order([job for job in rows if self.matches_view(job)])

    def render(self):
        rows = [job for job in self.app.jobs.values() if job["kind"] == "local"]
        visible_rows = []
        for job in self.ordered_jobs(rows):
            item = str(job["id"])
            values = ("Tak" if job["enabled"] else "", STATUSES[job["status"]], job["stage"],
                      f"{job['progress']:.0f}", duration_text(job["duration"]), Path(job["source"]).suffix.upper().lstrip("."),
                      job["title"], job["language"], job["audio_track"] + 1)
            visible_rows.append((item, values, status_tags(job["status"])))
        sync_tree(self.tree, self.visible, visible_rows)
        self.show_detail()
        technical = sum(bool(json.loads(job["source_meta"]).get("technical_exclusion")) and not job["enabled"] for job in rows)
        counts = Counter(job["status"] for job in rows if not json.loads(job["source_meta"]).get("technical_exclusion") or job["enabled"])
        pending = sum(job["status"] == "pending" and job["enabled"] for job in rows)
        self.counts.set(f"Lokalne: {len(rows)} | Gotowe: {counts['done']} | Częściowe: {counts['partial']} | Oczekujące: {counts['pending']} (włączone: {pending}) | Problemy: {sum(counts[s] for s in RETRYABLE if s != 'partial')} | Bez audio: {counts['no_audio']} | Miejsce wyników: {counts['output_required']} | Techniczne wyłączone: {technical}")

    def refresh(self, control, alive):
        self.render()
        job = self.app.jobs.get(control["current_id"])
        scope = job["kind"] if job else control.get("scope_kind", "all")
        self.show_current(control, alive, job, scope)
        session = alive and control["state"] == "running"
        self.controls["stop"].state([ENABLED_STATE] if session and scope == "local" else ["disabled"])
        for key in ("browse", "import_folder", "files"):
            self.controls[key].state(["disabled"] if self.app.busy_import else [ENABLED_STATE])

    def show_current(self, control, alive, job, scope):
        session = alive and control["state"] == "running"
        if session and scope == "local":
            suffix = " | Zatrzymam się po tym nagraniu." if control["stop"] else ""
            self.current.set((f"{job['title']} | {job['stage']}" if job else "Przygotowanie lokalnej sesji...") + suffix)
            self.progress["value"] = job["progress"] if job else 0
        elif session:
            self.current.set("Trwa inna sesja. Dokończ bieżący i zatrzymaj ją w karcie YouTube, potem uruchom kolejkę lokalną.")
            self.progress["value"] = 0
        elif alive:
            self.current.set("Trwa test dostępu lub przygotowanie sesji. Poczekaj na jego zakończenie albo anuluj test.")
            self.progress["value"] = 0
        else:
            self.current.set("Kolejka lokalna zatrzymana. " + self.stopped_message(control, scope))
            self.progress["value"] = 0

    def stopped_message(self, control, scope):
        message = control["message"][:500] if scope == "local" else ""
        failure = read_json(self.app.root / "ostatni-blad-start.json", {})
        if not message and failure.get("kind") == "local":
            message = failure.get("message", "")
        return message

    def show_detail(self, event=None):
        ids = self.selected()
        if len(ids) == 1 and ids[0] in self.app.jobs:
            job = self.app.jobs[ids[0]]
            self.detail.set(f"Źródło: {job['source']}\nWyniki: {self.result_location(job)}" +
                            (f"\nZachowano do {duration_text(job['processed_until'])} z {duration_text(job['duration'])}. "
                             f"Pozostało: {duration_text(max(0, (job['duration'] or 0) - job['processed_until']))}. Brakującą część podejmie następny Start."
                             if job.get("processed_until") and job['status'] != 'done' else "") +
                            ("\nOstatni problem: " + job["error"][:600] if job["error"] else "") + self.action_hint(job))
        else:
            self.detail.set(f"Zaznaczono: {len(ids)} | Zaznacz jedno nagranie, aby zobaczyć jego miejsce wyników.")
        if hasattr(self, "scope_summary") and "start_selected" in self.controls:
            waiting = WorkerLock.busy(self.app.root / "uruchamianie")
            self.update_start_controls(self.app.store.control(), WorkerLock.busy(self.app.root), waiting)

    def result_location(self, job):
        meta = json.loads(job["source_meta"])
        if meta.get("output_error"):
            return "Wymagany krótszy folder zamiast: " + str(meta.get("intended_output_root", ""))
        return str(job_folder(self.app.root, job))

    @staticmethod
    def action_hint(job):
        meta = json.loads(job["source_meta"])
        if meta.get("technical_exclusion") and not job["enabled"]:
            return "\nPominięto materiał techniczny: " + meta["technical_exclusion"] + ". Możesz świadomie włączyć tę pozycję."
        if job["status"] == "no_audio":
            return "\nTen plik nie ma dźwięku. Dodaj nagranie zawierające ścieżkę audio; ponowienie nie naprawi źródła."
        if meta.get("output_error"):
            return '\nWybierz Folder centralny lub Wskazany folder, potem kliknij Zmień miejsce wyników dla tej pozycji.'
        return ""

    def start(self):
        self.app.start(kind="local")

    def start_selected(self):
        ids = self.selected()
        if not ids:
            messagebox.showinfo("Start lokalnej kolejki", "Zaznacz lokalne nagrania do przetworzenia.", parent=self.app.window)
            return
        self.app.start(kind="local", job_ids=ids)

    def eligible(self, job):
        return job['kind'] == 'local' and startable(job)

    def update_start_controls(self, control, alive, waiting):
        selected = set(self.selected())
        visible = self.ordered_jobs()
        count = sum(self.eligible(job) and job['id'] in selected for job in visible)
        button = self.controls['start_selected']
        button.idle_text = f"Start zaznaczonych ({count})"
        session = alive and control['state'] == 'running' and control.get('scope_kind') in {'local', 'all'}
        starting = self.app.start_pending == 'local' or (waiting and self.app.launch_kind == 'local')
        mode = session_mode(control) if session else self.app.start_scope_mode
        scoped = mode == "selected"
        enabled = count > 0 and not alive and not self.app.busy_import and not self.app.start_pending and not waiting
        button.show_session(active=session and scoped, starting=starting and scoped,
                            stopping=session and bool(control['stop']), enabled=enabled)
        main = self.controls['start']
        total = sum(self.eligible(job) for job in visible)
        main.idle_text = f"Start/Wznów widoczne ({total})"
        main.show_session(active=session and not scoped, starting=starting and not scoped,
                          stopping=session and bool(control['stop']),
                          enabled=total > 0 and not alive and not self.app.busy_import and not self.app.start_pending and not waiting)
        self.update_scope_summary(session, count)
        self.update_recovery_controls(selected, enabled)

    def update_scope_summary(self, session, count):
        if session:
            self.scope_summary.set(session_summary(self.app.store.control(), self.app.jobs.values()))
        else:
            total = sum(self.eligible(job) for job in self.ordered_jobs())
            self.scope_summary.set(f"Start/Wznów widoczne: {total} | Start zaznaczonych: {count}. Filtry ograniczają zakres; kolejność według tabeli.")

    def update_recovery_controls(self, selected, enabled):
        single = len(selected) == 1
        for key in ('resume', 'partial_txt', 'error_log'):
            self.controls[key].state(['!disabled'] if single and (key != 'resume' or enabled) else ['disabled'])

    def open_partial(self):
        from .exporter import verify_outputs
        ids = self.selected()
        if len(ids) != 1:
            return
        job = self.app.jobs[ids[0]]
        folder = job_folder(self.app.root, job)
        if not verify_outputs(folder, job['identity'], partial=True):
            self.note.set('Brak zweryfikowanego częściowego eksportu. Zachowane bloki zostaną wykorzystane po wznowieniu.')
            return
        from .result_paths import output_paths
        path = output_paths(folder, read_json(folder / 'czesciowe.json'))['txt']
        os.startfile(str(path))

    def open_error_log(self):
        ids = self.selected()
        if len(ids) != 1:
            return
        folder = job_folder(self.app.root, self.app.jobs[ids[0]]) / 'robocze'
        path = next((p for p in (folder / 'blad.txt', folder / 'uvr.log', folder / 'ffmpeg.log', folder / 'pyannote.log') if p.is_file()), None)
        if path:
            os.startfile(str(path))
        else:
            self.note.set('Brak pliku logu. Szczegóły ostatniego problemu są widoczne pod listą.')

    def close(self):
        self.table_view.close()
        if self.filter_after:
            self.parent.after_cancel(self.filter_after)
            self.filter_after = None
