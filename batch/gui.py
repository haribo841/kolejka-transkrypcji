from __future__ import annotations

from collections import Counter
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from .common import APP, NO_WINDOW, WorkerLock, application_python, atomic_json, job_folder, read_json, settings
from .importers import enrich_local, import_xlsx
from .local_tab import LocalQueueTab
from .media import audio_tracks
from .result_paths import output_paths
from .store import RETRYABLE, STATUSES, Store
from .transcript_tab import TranscriptTab
from .ui_help import (HELP, Tooltip, collapsible_help, duration_text, help_button, next_job,
                      status_tags, sync_tree, wrap_with_parent)
from .readiness import (ACTION, collect_readiness, file_stamp, readiness_summary, check_destination)
from .worker import launch, preflight
from .store import now
from .youtube_access import SessionError, error_message, preferences as youtube_preferences


MODEL_STATE_FILE = 'konfiguracja-modelu.json'
SETTINGS_FILE = 'ustawienia.json'
START_ERROR_FILE = 'ostatni-blad-start.json'
AUDIO_TRACK_TITLE = 'Ścieżka audio'
FONT_FAMILY = 'Segoe UI'


class QueueWindow:
    def __init__(self, root, tk_root=None, start_page=None):
        self.root = Path(root)
        self.store, self.config = Store(root), settings(root)
        self.tk_root = tk_root or tk.Tk()
        self.window.title("Kolejka transkrypcji")
        self.window.geometry("1360x860")
        self.window.minsize(1220, 760)
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self.events = queue.Queue()
        self.visible, self.jobs, self.controls = {}, {}, {}
        self.session_view = read_json(self.root / "okno-ui.json", {})
        self.selection_queue = self.session_view.get("queue", "youtube")
        self.busy_import = self.readiness_busy = self.metadata_busy = self.closing = False
        self.pending_filter = self.refresh_after = self.events_after = self.navigation_after = None
        self.setup_dialog = self.youtube_dialog = self.guide_dialog = None
        self.readiness, self.readiness_key = {}, None
        self.readiness_checked = 0
        self.navigation_done = set()
        self.metadata_attempted = set()
        self.help_status = tk.StringVar(master=self.window, value="Podpowiedź pojawi się po zatrzymaniu kursora. Tab i F1: pomoc z klawiatury.")
        self.counts, self.current, self.detail, self.model_state, self.local_model_state = (tk.StringVar(master=self.window) for _ in range(5))
        self.uvr_enabled = tk.BooleanVar(master=self.window, value=self.config.get("uvr_enabled", False))
        self.keep_audio = tk.BooleanVar(master=self.window, value=self.config.get("keep_audio", False))
        self.style = ttk.Style(self.window)
        if "vista" in self.style.theme_names():
            self.style.theme_use("vista")
        self.window.option_add("*Font", "{Segoe UI} 10")
        self.window.bind("<MouseWheel>", lambda event: Tooltip.active.hide() if Tooltip.active else None, add="+")
        self.window.bind("<Unmap>", lambda event: Tooltip.active.hide() if Tooltip.active else None, add="+")
        self.notebook = ttk.Notebook(self.window)
        self.notebook.pack(fill="both", expand=True)
        self.queue_page, self.local_page, self.processing_page, self.config_page = (ttk.Frame(self.notebook) for _ in range(4))
        for page, title in ((self.queue_page, "Kolejka YouTube"), (self.local_page, "Kolejka lokalna"),
                            (self.processing_page, "Obróbka transkrypcji"), (self.config_page, "Konfiguracja")):
            self.notebook.add(page, text=title)
        Tooltip(self.notebook, "YouTube i pliki lokalne mają oddzielne listy. Konfiguracja jest wspólna.", self.help_status, popup=False)
        self.build_youtube()
        self.local_tab = LocalQueueTab(self, self.local_page)
        self.controls["files"], self.controls["folder"] = self.local_tab.controls["files"], self.local_tab.controls["browse"]
        self.transcript_tab = TranscriptTab(self, self.processing_page)
        from .config_tab import ConfigurationTab
        self.config_tab = ConfigurationTab(self, self.config_page)
        self.notebook.bind("<<NotebookTabChanged>>", self.queue_changed)
        self.window.bind("<Map>", lambda event: self.schedule_navigation() if event.widget is self.window else None, add="+")
        self.refresh()
        control = self.store.control()
        active = control.get("scope_kind") if control["state"] == "running" and WorkerLock.busy(self.root) else None
        preferred = start_page or active or self.selection_queue
        self.notebook.select(self.local_page if preferred == "local" else self.queue_page)
        self.schedule_navigation()
        self.events_after = self.window.after(250, self.consume_events)

    def build_youtube(self):
        outer = ttk.Frame(self.queue_page, padding=12)
        outer.pack(fill="both", expand=True)
        top = ttk.Frame(outer)
        top.pack(fill="x", pady=(0, 6))
        for key, text, action in (("import", "Importuj katalog YouTube", self.import_catalog),
                                  ("next", "Bieżący / następny", self.jump_youtube),
                                  ("configuration", "Konfiguracja", self.open_configuration),
                                  ("test", "Sprawdź na fragmencie", self.test_local),
                                  ("youtube_access", "Dostęp YouTube", self.configure_youtube),
                                  ("csv", "Raport CSV", self.report), ("guide", "Pomoc", self.show_guide)):
            self.button(top, key, text, action).pack(side="left", padx=(0, 7))
        self.source_note = tk.StringVar(master=self.window, value="Import nie uruchamia pracy. Kolejność: od najstarszych.")
        with self.store.connect() as db:
            last_import = db.execute("SELECT notes FROM imports ORDER BY id DESC LIMIT 1").fetchone()
            if last_import:
                self.source_note.set(last_import["notes"])
        ttk.Label(outer, textvariable=self.source_note, wraplength=1220).pack(anchor="w")
        self.youtube_state = tk.StringVar(master=self.window)
        ttk.Label(outer, textvariable=self.youtube_state, wraplength=1220, foreground="#596579").pack(anchor="w", pady=(2, 4))
        ttk.Label(outer, textvariable=self.model_state, wraplength=1220).pack(anchor="w", pady=(0, 6))
        filters = ttk.Frame(outer)
        filters.pack(fill="x", pady=(0, 8))
        self.search, self.date_from, self.date_to = (tk.StringVar(master=self.window) for _ in range(3))
        self.status = tk.StringVar(master=self.window, value="Wszystkie")
        self.kind = tk.StringVar(master=self.window, value="Wszystkie")
        for key, label, variable, width in (("search", "Szukaj", self.search, 27),
                                            ("date_from", "Od (RRRR-MM-DD)", self.date_from, 12),
                                            ("date_to", "Do", self.date_to, 12)):
            ttk.Label(filters, text=label).pack(side="left", padx=(0, 5))
            field = ttk.Entry(filters, textvariable=variable, width=width)
            field.pack(side="left", padx=(0, 10))
            self.add_help(field, key)
        for key, label, variable, values in (("status", "Status", self.status, ["Wszystkie", *STATUSES.values()]),
                                             ("type", "Typ", self.kind, ["Wszystkie", "Film", "Transmisja", "Short"])):
            ttk.Label(filters, text=label).pack(side="left", padx=(0, 5))
            field = ttk.Combobox(filters, textvariable=variable, values=values, state="readonly", width=18)
            field.pack(side="left", padx=(0, 10))
            self.add_help(field, key)
        for variable in (self.search, self.status, self.kind, self.date_from, self.date_to):
            variable.trace_add("write", self.schedule_filter)
        listing = ttk.Frame(outer)
        listing.pack(fill="both", expand=True)
        columns = ("enabled", "date", "type", "status", "progress", "duration", "title", "language", "audio")
        self.tree = ttk.Treeview(listing, columns=columns, show="headings", selectmode="extended", height=12)
        self.controls["list"] = self.tree
        Tooltip(self.tree, HELP["list"], self.help_status, popup=False)
        for column, title, width in zip(columns, ("Kolejka", "Data", "Typ", "Status", "Etap %", "Czas trwania", "Tytuł", "Język", "Audio"),
                                        (55, 98, 85, 135, 55, 100, 550, 60, 50)):
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
        self.tree.bind("<<TreeviewSelect>>", self.show_detail)
        self.tree.bind("<Double-1>", lambda event: self.open_results())
        self.tree.bind("<Control-a>", self.select_visible)
        for tag, color in (("done", "#25723b"), ("error", "#ae2020"), ("draft", "#777777")):
            self.tree.tag_configure(tag, foreground=color)
        selection = ttk.Frame(outer)
        selection.pack(fill="x", pady=6)
        for key, text, action in (("select", "Zaznacz widoczne", self.select_visible),
                                  ("enable", "Włącz do kolejki", lambda: self.enable(True)),
                                  ("disable", "Wyłącz z kolejki", lambda: self.enable(False)),
                                  ("language", "Ustaw język", self.language), ("track", "Wybierz ścieżkę audio", self.audio_track)):
            self.button(selection, key, text, action).pack(side="left", padx=(0, 6))
        ttk.Label(outer, textvariable=self.counts).pack(anchor="w")
        ttk.Label(outer, textvariable=self.current, wraplength=1230).pack(anchor="w", pady=3)
        self.progress = ttk.Progressbar(outer, maximum=100)
        self.progress.pack(fill="x", pady=(0, 4))
        self.controls["progress"] = self.progress
        Tooltip(self.progress, HELP["progress"], self.help_status, popup=False)
        ttk.Label(outer, textvariable=self.detail, wraplength=1230).pack(anchor="w", pady=(0, 5))
        actions = ttk.Frame(outer)
        actions.pack(fill="x")
        for key, text, action in (("start", "Start/Wznów", self.start), ("stop", "Dokończ bieżący i zatrzymaj", self.stop),
                                  ("retry", "Ponów nieudane", self.retry), ("results", "Otwórz wyniki", self.open_results),
                                  ("transcript", "Otwórz TXT", self.open_transcript)):
            self.button(actions, key, text, action).pack(side="left", padx=(0, 8))
        self.selected_only = tk.BooleanVar(master=self.window, value=False)
        selection_option = ttk.Checkbutton(actions, text="Start tylko zaznaczonych", variable=self.selected_only)
        selection_option.pack(side="left")
        self.add_help(selection_option, "selected_only")
        collapsible_help(outer, self.help_status)
        self.wrap_labels(outer)

    @property
    def window(self):
        """Retain the existing embedding API while naming the Tk field explicitly."""
        return self.tk_root

    def wrap_labels(self, outer):
        for widget in outer.winfo_children():
            if isinstance(widget, ttk.Label) and int(widget.cget("wraplength") or 0):
                wrap_with_parent(widget, 24)

    def add_help(self, widget, key):
        self.controls[key] = widget
        Tooltip(widget, HELP[key], self.help_status)

    def button(self, parent, key, label, action):
        button = help_button(parent, label, action, HELP[key], self.help_status)
        self.controls[key] = button
        return button

    def close(self):
        # The detached process owns computation and the sleep-prevention lease.
        if self.transcript_tab.busy:
            self.window.withdraw()
            self.transcript_tab.on_finished = self.close
            return
        self.closing = True
        self.transcript_tab.close()
        self.local_tab.close()
        if self.setup_dialog and not self.setup_dialog.closed:
            self.setup_dialog.close()
        if self.youtube_dialog and not self.youtube_dialog.closed:
            self.youtube_dialog.close()
        # Cancel only Python callbacks we own. ttk progress bars also schedule
        # native Tcl scripts, which must be stopped by their own widgets.
        for callback in (self.pending_filter, self.refresh_after, self.events_after, getattr(self, "navigation_after", None)):
            if callback:
                self.window.after_cancel(callback)
        self.window.destroy()

    def queue_changed(self, event=None):
        previous = self.selection_queue
        page = self.notebook.select()
        if page == str(self.local_page):
            self.selection_queue = "local"
        elif page == str(self.queue_page):
            self.selection_queue = "youtube"
        if self.selection_queue != previous:
            atomic_json(self.root / "okno-ui.json", {"queue": self.selection_queue})
        if hasattr(self, "local_tab"):
            self.schedule_navigation()

    def open_configuration(self):
        self.notebook.select(self.config_page)

    def schedule_navigation(self):
        if self.closing or self.navigation_after:
            return
        def run():
            self.navigation_after = None
            self.initial_navigation()
        self.navigation_after = self.window.after_idle(run)

    def initial_navigation(self):
        kind = "local" if self.notebook.select() == str(self.local_page) else "youtube"
        if self.notebook.select() not in {str(self.local_page), str(self.queue_page)} or kind in self.navigation_done:
            return
        tree = self.local_tab.tree if kind == "local" else self.tree
        if tree.winfo_ismapped() and tree.get_children():
            self.navigation_done.add(kind)
            self.jump_to_next(kind, clear_filters=False)

    def jump_youtube(self):
        self.jump_to_next("youtube")

    def jump_to_next(self, kind, clear_filters=True):
        job = next_job(self.jobs.values(), kind, self.store.control().get("current_id"))
        if job is None:
            (self.local_tab.note if kind == "local" else self.source_note).set("Nie ma włączonych nagrań oczekujących w tej kolejce.")
            return
        tree = self.local_tab.tree if kind == "local" else self.tree
        item = str(job["id"])
        if not tree.exists(item) and clear_filters:
            if kind == "local":
                self.local_tab.search.set("")
                self.local_tab.status.set("Wszystkie")
                self.local_tab.render()
            else:
                for variable in (self.search, self.date_from, self.date_to):
                    variable.set("")
                self.status.set("Wszystkie")
                self.kind.set("Wszystkie")
                self.render_list()
        if tree.exists(item):
            tree.selection_set(item)
            tree.focus(item)
            tree.see(item)
            children = tree.get_children()
            tree.yview_moveto(children.index(item) / len(children))

    def request_readiness(self, force=False):
        if self.readiness_busy or self.closing:
            return
        self.config = settings(self.root)
        key = tuple(tuple(file_stamp(self.root / name)) for name in
                    (SETTINGS_FILE, "pierwsza-proba.json", MODEL_STATE_FILE, "konfiguracja-uvr.json", "youtube-dostep.json"))
        if not force and key == self.readiness_key and time.monotonic() - self.readiness_checked < 60:
            return
        self.readiness_busy = True
        self.readiness = {}
        self.readiness_key = key
        self.readiness_checked = time.monotonic()
        config = dict(self.config)
        if hasattr(self, "config_tab"):
            self.config_tab.checking()
        def run():
            try:
                result = collect_readiness(config, self.root)
            except Exception as exc:
                result = {"components": {k: {"status": ACTION, "message": str(exc)} for k in
                                         ("whisper", "tools", "diarization", "uvr", "youtube")}, "passed": False, "test": {}}
            self.events.put(("readiness", (config, result)))
        threading.Thread(target=run, daemon=True).start()

    def enrich_metadata(self):
        if self.metadata_busy:
            return
        jobs = [job for job in self.jobs.values() if job["kind"] == "local" and job["status"] == "pending"
                and job["duration"] is None and job["id"] not in self.metadata_attempted and Path(job["source"]).is_file()]
        if not jobs:
            return
        self.metadata_attempted.update(job["id"] for job in jobs)
        self.metadata_busy = True
        config = dict(self.config)
        def run():
            try:
                enrich_local(self.store, jobs, config)
            finally:
                self.events.put(("metadata", None))
        threading.Thread(target=run, daemon=True).start()

    def save_setting(self, key, value):
        if WorkerLock.busy(self.root) or WorkerLock.busy(self.root / "konfiguracja"):
            messagebox.showinfo("Ustawienia sesji", "Najpierw dokończ bieżące nagranie lub próbę i zatrzymaj sesję.", parent=self.window)
            return False
        config = settings(self.root)
        config[key] = value
        atomic_json(self.root / SETTINGS_FILE, config)
        self.config = config
        self.readiness_key = None
        self.request_readiness(force=True)
        return True

    def selected(self):
        self.queue_changed()
        if self.selection_queue == "local":
            return self.local_tab.selected()
        return [int(item) for item in self.tree.selection()]

    def select_visible(self, event=None):
        self.tree.selection_set(self.tree.get_children())
        return "break"

    def schedule_filter(self, *_):
        if self.pending_filter:
            self.window.after_cancel(self.pending_filter)
        self.pending_filter = self.window.after(250, self.filter_now)

    def filter_now(self):
        self.pending_filter = None
        self.render_list()

    def type_of(self, job):
        return "Lokalne" if job["kind"] == "local" else json.loads(job["source_meta"]).get("kind", "YouTube")

    def matches_youtube_view(self, job):
        if job["kind"] != "youtube":
            return False
        if self.search.get().casefold() not in (job["title"] + " " + job["identity"]).casefold():
            return False
        if self.status.get() != "Wszystkie" and STATUSES[job["status"]] != self.status.get():
            return False
        kind = self.kind.get()
        if kind not in {"Wszystkie", "YouTube"} and self.type_of(job) != kind:
            return False
        day = job["date"][:10]
        return not ((self.date_from.get() and day < self.date_from.get()) or
                    (self.date_to.get() and day > self.date_to.get()))

    def render_list(self):
        visible_rows = []
        for job in self.jobs.values():
            if not self.matches_youtube_view(job):
                continue
            day = job["date"][:10]
            item = str(job["id"])
            values = ("Tak" if job["enabled"] else "", day, self.type_of(job), STATUSES[job["status"]],
                      f"{job['progress']:.0f}", duration_text(job["duration"]), job["title"], job["language"], job["audio_track"] + 1)
            visible_rows.append((item, values, status_tags(job["status"])))
        sync_tree(self.tree, self.visible, visible_rows)

    def refresh(self):
        rows = self.store.jobs()
        self.jobs = {job["id"]: job for job in rows}
        youtube_rows = [job for job in rows if job["kind"] == "youtube"]
        counts = Counter(job["status"] for job in youtube_rows)
        enabled = sum(j["status"] == "pending" and j["enabled"] for j in youtube_rows)
        self.counts.set(f"YouTube: {len(youtube_rows)} | Gotowe: {counts['done']} | Oczekujące: {counts['pending']} (włączone: {enabled}) | Problemy: {sum(counts[s] for s in RETRYABLE)} | Szkice: {counts['draft']}")
        control = self.store.control()
        job = self.jobs.get(control["current_id"])
        alive = WorkerLock.busy(self.root)
        scope = job["kind"] if job else control.get("scope_kind", "all")
        self.show_current(control, alive, job, scope)
        self.refresh_models()
        self.render_list()
        self.local_tab.refresh(control, alive)
        self.controls["start"].state(["disabled"] if alive or self.busy_import or not readiness_summary(self.readiness, "youtube")[0] else ["!disabled"])
        self.controls["stop"].state(["!disabled"] if alive and scope != "local" else ["disabled"])
        self.enrich_metadata()
        self.schedule_navigation()
        self.refresh_after = self.window.after(1200, self.refresh)

    def idle_title(self, alive):
        checking = self.youtube_dialog and self.youtube_dialog.checking and not self.youtube_dialog.closed
        if checking:
            return "Trwa test dostępu YouTube. "
        if alive:
            return "Przygotowanie pracy..."
        return "Kolejka zatrzymana. "

    def show_current(self, control, alive, job, scope):
        if alive and scope == "local":
            self.current.set("Trwa kolejka lokalna. Jej postęp i zatrzymanie są dostępne w karcie Kolejka lokalna.")
            self.progress["value"] = 0
        elif job and alive:
            suffix = " | Zatrzymam się po tym filmie." if control["stop"] else ""
            self.current.set(f"{job['title']} | {job['stage']}{suffix}")
            self.progress["value"] = job["progress"]
        else:
            message = control["message"] if scope != "local" else ""
            failure = read_json(self.root / START_ERROR_FILE, {})
            if not message and failure.get("kind") == "youtube":
                message = failure.get("message", "")
            if "not a bot" in message.casefold() or "too many requests" in message.casefold():
                message = error_message(message)
            self.current.set(self.idle_title(alive) + message[:500])
            self.progress["value"] = 0

    def refresh_models(self):
        try:
            access = youtube_preferences(self.root)
            mode = {"anonymous": "anonimowo", "browser": "sesja " + access["browser"], "file": "plik cookies"}[access["mode"]]
            self.youtube_state.set(f"YouTube: {mode} | przerwa przed filmem: {access['delay_seconds']:g} s | przy blokadzie otwórz Dostęp YouTube")
        except (SessionError, OSError):
            self.youtube_state.set("Sprawdź ustawienia w Dostęp YouTube.")
        self.request_readiness()
        self.model_state.set("Konfiguracja YouTube: " + readiness_summary(self.readiness, "youtube")[1])
        self.local_model_state.set("Konfiguracja lokalna: " + readiness_summary(self.readiness, "local")[1])
        state = read_json(self.root / MODEL_STATE_FILE, {})
        if state.get("state") == "running":
            self.config_tab.last_test.set(state.get("message", "Trwa próba lub konfiguracja…"))

    def show_detail(self, event=None):
        selected = [int(item) for item in self.tree.selection()]
        if len(selected) == 1 and selected[0] in self.jobs:
            job = self.jobs[selected[0]]
            try:
                result = str(output_paths(job_folder(self.root, job))["txt"]) if job["status"] == "done" else job["source"]
                error = job["error"]
                if job["kind"] == "youtube" and error and ("ERROR:" in error or "not a bot" in error.casefold()):
                    error = error_message(error)
                self.detail.set((error or result)[:600])
            except (ValueError, OSError, TypeError):
                self.detail.set("Nie udało się odczytać nazwy wyniku. Otwórz folder nagrania.")
        else:
            self.detail.set(f"Zaznaczono: {len(selected)}")

    def background(self, function, note=None):
        if self.busy_import:
            messagebox.showinfo("Trwa operacja", "Poczekaj na zakończenie bieżącego importu.", parent=self.window)
            return
        self.busy_import = True
        def run():
            try:
                self.events.put(("result", function(), note))
            except Exception as exc:
                self.events.put(("error", str(exc), note))
            finally:
                self.events.put(("finished", None))
        threading.Thread(target=run, daemon=True).start()

    def consume_events(self):
        while not self.events.empty():
            event = self.events.get_nowait()
            kind, value = event[:2]
            note = event[2] if len(event) == 3 and event[2] is not None else self.source_note
            self.handle_event(kind, value, note)
        self.events_after = self.window.after(250, self.consume_events)

    def handle_event(self, kind, value, note):
        if kind == "finished":
            self.busy_import = False
        elif kind == "readiness":
            self.readiness_busy = False
            config, result = value
            if config == settings(self.root):
                self.readiness = result
                self.config_tab.update(result)
            else:
                self.readiness_key = None
        elif kind == "metadata":
            self.metadata_busy = False
        elif kind == "error":
            note.set("Operacja przerwana: " + str(value))
            messagebox.showerror("Nie udało się wykonać operacji", str(value), parent=self.window)
        elif kind == "progress":
            note.set(str(value))
        elif isinstance(value, dict):
            note.set(value.get("note") or f"Import: dodano {value.get('added', 0)}, znaleziono {value.get('files', value.get('rows', 0))}.")
            if value.get("local_import"):
                self.local_tab.import_finished(value)
        else:
            note.set(str(value))

    def import_catalog(self):
        path = filedialog.askopenfilename(title="Katalog YouTube", filetypes=[("Arkusz Excel", "*.xlsx")], parent=self.window)
        if path:
            self.source_note.set("Importowanie katalogu...")
            self.background(lambda: import_xlsx(self.store, path))

    def add_files(self):
        self.notebook.select(self.local_page)
        self.local_tab.add_files()

    def add_folder(self):
        self.notebook.select(self.local_page)
        self.local_tab.choose_and_import_folder()

    def enable(self, enabled, ids=None):
        self.store.configure(self.selected() if ids is None else ids, enabled=int(enabled))

    def language(self, ids=None):
        ids = self.selected() if ids is None else ids
        if not ids:
            return
        value = simpledialog.askstring("Język zaznaczonych", "Kod języka, np. pl, en, de lub auto. Nagrania gotowe i bieżące pozostają bez zmian.", initialvalue="auto", parent=self.window)
        if value:
            value = value.strip().lower()
            import re
            if value != "auto" and not re.fullmatch(r"[a-z]{2,3}", value):
                messagebox.showerror("Język", "Podaj kod języka lub auto.", parent=self.window)
                return
            self.store.configure(ids, language=value)

    def audio_track(self, ids=None):
        ids = self.selected() if ids is None else ids
        if len(ids) != 1:
            messagebox.showinfo(AUDIO_TRACK_TITLE, "Zaznacz jeden plik lokalny.", parent=self.window)
            return
        job = self.jobs[ids[0]]
        if job["kind"] != "local":
            messagebox.showinfo(AUDIO_TRACK_TITLE, "Dla YouTube pobierany jest najlepszy dostępny strumień audio.", parent=self.window)
            return
        try:
            tracks = audio_tracks(job["source"], self.config)
            descriptions = [f"{index + 1}: {s.get('codec_name')} | {s.get('channels')} kanałów | {s.get('tags', {}).get('language', '')} {s.get('tags', {}).get('title', '')}" for index, s in enumerate(tracks)]
            value = simpledialog.askinteger(AUDIO_TRACK_TITLE, "\n".join(descriptions) + "\n\nNumer ścieżki:", minvalue=1, maxvalue=len(tracks), initialvalue=job["audio_track"] + 1, parent=self.window)
            if value:
                self.store.configure(ids, audio_track=value - 1)
        except Exception as exc:
            messagebox.showerror(AUDIO_TRACK_TITLE, str(exc), parent=self.window)

    def start(self, kind="youtube", job_ids=None):
        if self.start_in_progress():
            return
        try:
            if not self.ready_to_start(kind):
                return
            if kind == "youtube" and self.selected_only.get():
                job_ids = [int(item) for item in self.tree.selection()]
                if not job_ids:
                    messagebox.showinfo("Start", "Zaznacz materiały do przetworzenia.", parent=self.window)
                    return
            pending = [job for job in self.store.jobs() if job["kind"] == kind and job["enabled"]
                       and (job["status"] in {"pending", "blocked", "running"}
                            or (job["status"] == "error" and job["stage"] == "Wymaga działania"))
                       and (job_ids is None or job["id"] in job_ids)]
            if not pending:
                messagebox.showinfo("Kolejka", "Nie ma włączonych nagrań oczekujących w tym zakresie. Nieudane możesz ponowić odpowiednim przyciskiem.", parent=self.window)
                return
            check_destination(job_folder(self.root, pending[0]), self.config["min_free_gb"])
            launch(self.root, kind=kind, job_ids=job_ids)
            atomic_json(self.root / START_ERROR_FILE, {"at": now(), "kind": kind, "message": ""})
        except Exception as exc:
            self.record_start_error(kind, str(exc))
            messagebox.showerror("Kolejka nie została uruchomiona", str(exc), parent=self.window)

    def record_start_error(self, kind, message):
        from .hf_access import redact
        message = redact(message)[:2500]
        atomic_json(self.root / START_ERROR_FILE, {"at": now(), "kind": kind, "message": message})
        (self.local_tab.note if kind == "local" else self.source_note).set("Start przerwany: " + message)

    def start_in_progress(self):
        if self.busy_import:
            messagebox.showinfo("Trwa import", "Poczekaj na zakończenie importu przed uruchomieniem kolejki.", parent=self.window)
            return True
        if WorkerLock.busy(self.root):
            messagebox.showinfo("Trwa sesja", "Najpierw dokończ bieżące nagranie i zatrzymaj trwającą kolejkę. Start nie zmienia zakresu aktywnej sesji.", parent=self.window)
            return True
        if WorkerLock.busy(self.root / "konfiguracja"):
            self.open_configuration()
            self.config_tab.last_test.set("Poczekaj na zakończenie bieżącej próby lub konfiguracji modeli.")
            return True
        if self.youtube_dialog and self.youtube_dialog.running:
            if not self.youtube_dialog.closed:
                self.youtube_dialog.dialog.lift()
            self.help_status.set("Poczekaj na zakończenie lub anulowanie testu YouTube, a następnie naciśnij Start/Wznów.")
            return True
        return False

    def ready_to_start(self, kind):
        self.config = settings(self.root)
        result = collect_readiness(self.config, self.root)
        self.readiness = result
        self.config_tab.update(result)
        ready, message = readiness_summary(result, kind)
        if not ready:
            self.open_configuration()
            self.record_start_error(kind, message)
        else:
            preflight(self.config, self.root, kind=kind)
        return ready

    def stop(self, kind="youtube"):
        control = self.store.control()
        job = self.store.get(control["current_id"]) if control["current_id"] else None
        scope = job["kind"] if job else control.get("scope_kind", "all")
        if WorkerLock.busy(self.root) and scope not in {kind, "all"}:
            messagebox.showinfo("Inna kolejka", "Użyj zatrzymania w karcie trwającej kolejki.", parent=self.window)
            return
        self.store.control(stop=1)

    def retry(self, kind="youtube", ids=None):
        candidates = [job["id"] for job in self.store.jobs() if job["kind"] == kind]
        if ids is None:
            ids = [int(item) for item in self.tree.selection()]
        scoped = [ident for ident in ids if ident in candidates] if ids else candidates
        count = self.store.retry(scoped) if scoped else 0
        note = self.local_tab.note if kind == "local" else self.source_note
        note.set(f"Przywrócono do oczekujących: {count}. Naciśnij Start/Wznów.")

    def results_folder(self, ids):
        if ids:
            paths = [job_folder(self.root, self.jobs[ident]) for ident in ids]
            parents = {path.parent for path in paths}
            if len(paths) > 1 and len(parents) != 1:
                messagebox.showinfo("Wyniki", "Zaznacz jedno nagranie. Wybrane materiały mają różne miejsca wyników.", parent=self.window)
                return None
            return paths[0] if len(paths) == 1 else parents.pop()
        if self.selection_queue != "local":
            return self.root / "wyniki"
        mode, custom = self.local_tab.output_policy()
        if mode == "custom":
            return Path(custom)
        if mode == "beside":
            if self.local_tab.folder.get().strip():
                return Path(self.local_tab.folder.get())
            self.local_tab.note.set("Zaznacz nagranie lub wybierz folder źródłowy.")
            return None
        return self.root / "wyniki"

    def open_results(self, ids=None):
        path = self.results_folder(self.selected() if ids is None else ids)
        if path is None:
            return
        if path.is_dir():
            os.startfile(path)
        else:
            (self.local_tab.note if self.selection_queue == "local" else self.source_note).set("Wyniki nie zostały jeszcze utworzone: " + str(path))

    def open_transcript(self, ids=None):
        ids = self.selected() if ids is None else ids
        if len(ids) != 1 or self.jobs[ids[0]]["status"] != "done":
            messagebox.showinfo("Transkrypcja", "Zaznacz jedno nagranie ze statusem Gotowe.", parent=self.window)
            return
        try:
            path = output_paths(job_folder(self.root, self.jobs[ids[0]]))["txt"]
            os.startfile(path)
        except (ValueError, OSError, TypeError) as exc:
            messagebox.showerror("Nie udało się otworzyć transkrypcji", str(exc), parent=self.window)

    def report(self, kind="youtube"):
        path = filedialog.asksaveasfilename(title="Raport do Excela", defaultextension=".csv", initialfile="postep.csv", filetypes=[("CSV", "*.csv")], parent=self.window)
        if path:
            note = self.local_tab.note if kind == "local" else self.source_note
            self.background(lambda: (self.store.export_csv(path, kind=kind), f"Zapisano raport: {path}")[1], note=note)

    def setup_process(self, request):
        if WorkerLock.busy(self.root):
            messagebox.showinfo("Konfiguracja", "Najpierw dokończ bieżące nagranie i zatrzymaj kolejkę.", parent=self.window)
            return False
        if WorkerLock.busy(self.root / "konfiguracja"):
            messagebox.showinfo("Konfiguracja", "Pobieranie lub próba już trwa.", parent=self.window)
            return False
        try:
            atomic_json(self.root / MODEL_STATE_FILE, {"state": "running", "phase": "starting",
                        "message": "Uruchamianie konfiguracji. Możesz pozostawić to okno otwarte."})
            with (self.root / "konfiguracja.log").open("ab") as log:
                process = subprocess.Popen([application_python(), "-m", "batch.setup_model", str(self.root)],
                    cwd=APP, stdin=subprocess.PIPE, stdout=log, stderr=log,
                    creationflags=NO_WINDOW | getattr(subprocess, "DETACHED_PROCESS", 0),
                    env=dict(os.environ, PYTHONIOENCODING="utf-8"))
                with process.stdin:
                    process.stdin.write((json.dumps(request) + "\n").encode("utf-8"))
        except (OSError, ValueError):
            # Never include request contents, command-line tokens or HTTP traces.
            message = "Nie udało się uruchomić konfiguracji. Sprawdź dostęp do folderu wyników i środowisko Python aplikacji, a potem spróbuj ponownie."
            atomic_json(self.root / MODEL_STATE_FILE, {"state": "error", "message": message})
            messagebox.showerror("Konfiguracja", message, parent=self.window)
            return False
        self.model_state.set("Uruchamianie konfiguracji...")
        return True

    def save_audio_options(self):
        if WorkerLock.busy(self.root) or WorkerLock.busy(self.root / "konfiguracja"):
            messagebox.showinfo("Ustawienia sesji", "Najpierw dokończ bieżący film i zatrzymaj kolejkę. Opcje audio zmienisz przed następnym Start.", parent=self.window)
            self.uvr_enabled.set(self.config.get("uvr_enabled", False))
            self.keep_audio.set(self.config.get("keep_audio", False))
            return
        if self.uvr_enabled.get():
            self.keep_audio.set(True)
        self.config = settings(self.root)
        self.config.update(uvr_enabled=self.uvr_enabled.get(), keep_audio=self.keep_audio.get())
        atomic_json(self.root / SETTINGS_FILE, self.config)
        self.readiness_key = None
        self.request_readiness(force=True)

    def setup_uvr(self):
        if WorkerLock.busy(self.root) or WorkerLock.busy(self.root / "konfiguracja"):
            self.open_configuration()
            self.config_tab.last_test.set("Poczekaj na zakończenie bieżącej sesji lub próby.")
            return
        if WorkerLock.busy(Path(self.config["uvr_model_dir"])):
            return
        python = Path(self.config["uvr_python"])
        if not python.is_file():
            messagebox.showinfo("UVR", "Uruchom Instaluj-UVR.ps1, a następnie pobierz model.", parent=self.window)
            return
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        env["PATH"] = str(Path(self.config["ffmpeg"]).parent) + os.pathsep + env.get("PATH", "")
        with (self.root / "konfiguracja-uvr.log").open("ab") as log:
            subprocess.Popen([str(python), "-m", "batch.uvr_child", "--setup", str(self.root)], cwd=APP,
                stdout=log, stderr=log, stdin=subprocess.DEVNULL, env=env,
                creationflags=NO_WINDOW | getattr(subprocess, "DETACHED_PROCESS", 0))

    def test_local(self):
        if WorkerLock.busy(self.root):
            messagebox.showinfo("Próba", "Najpierw dokończ bieżące nagranie i zatrzymaj sesję.", parent=self.window)
            return
        ids = self.selected()
        job = self.jobs.get(ids[0]) if len(ids) == 1 else None
        path = job["source"] if job and job["kind"] == "local" and Path(job["source"]).is_file() else ""
        if not path:
            path = filedialog.askopenfilename(title="Audio lub wideo do próby (odczytamy do 60 sekund)", parent=self.window)
        if path and self.setup_process({"test_only": True, "source": path,
                                       "track": job["audio_track"] if job and job["kind"] == "local" else 0,
                                       "language": job["language"] if job else self.config.get("language", "auto")}):
            self.open_configuration()
            self.config_tab.last_test.set("Trwa próba fragmentu. Kolejka pozostaje zatrzymana.")

    def configure_model(self):
        from .setup_wizard import SetupWizard
        if self.setup_dialog and not self.setup_dialog.closed:
            self.setup_dialog.dialog.deiconify()
            self.setup_dialog.dialog.lift()
            return
        self.setup_dialog = SetupWizard(self)

    def configure_youtube(self):
        from .youtube_dialog import YouTubeDialog
        if self.youtube_dialog and not self.youtube_dialog.closed:
            self.youtube_dialog.dialog.deiconify()
            self.youtube_dialog.dialog.lift()
            return
        self.youtube_dialog = YouTubeDialog(self)

    def show_guide(self):
        if self.guide_dialog and self.guide_dialog.winfo_exists():
            self.guide_dialog.lift()
            return
        dialog = tk.Toplevel(self.window)
        self.guide_dialog = dialog
        dialog.title("Jak korzystać z kolejki")
        dialog.geometry("820x650")
        dialog.minsize(640, 480)
        dialog.transient(self.window)
        frame = ttk.Frame(dialog, padding=16)
        frame.pack(fill="both", expand=True)
        heading = ttk.Label(frame, text="Pierwsza sesja: modele → krótka próba → wybór nagrań → Start", font=(FONT_FAMILY, 12, "bold"), wraplength=770)
        heading.pack(anchor="w", pady=(0, 10))
        wrap_with_parent(heading, 32)
        body = ttk.Frame(frame)
        body.pack(fill="both", expand=True)
        content = tk.Text(body, wrap="word", padx=12, pady=10, font=(FONT_FAMILY, 10))
        scrollbar = ttk.Scrollbar(body, orient="vertical", command=content.yview)
        content.configure(yscrollcommand=scrollbar.set)
        content.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        content.tag_configure("heading", font=(FONT_FAMILY, 11, "bold"), spacing1=12, spacing3=4)
        for heading, keys in (("Przygotowanie", ("speakers", "test", "youtube_access", "import", "files", "folder")),
                              ("Wybór i ustawienia nagrań", ("search", "select", "enable", "disable", "language", "track", "uvr", "keep", "uvr_model")),
                              ("Praca i wyniki", ("start", "selected_only", "stop", "retry", "results", "transcript", "csv"))):
            content.insert("end", heading + "\n", "heading")
            for key in keys:
                widget = self.controls[key]
                label = widget.cget("text") if "text" in widget.keys() else "Szukaj i filtry"
                content.insert("end", label + "\n", "heading")
                content.insert("end", HELP[key] + "\n")
        content.insert("end", "\nZamknięcie okna nie przerywa rozpoczętej pracy. Najedź na element okna lub użyj Tab i F1, aby wyświetlić podpowiedź.\n")
        content.insert("end", "Obróbka transkrypcji\n", "heading")
        content.insert("end", "Karta Obróbka transkrypcji usuwa etykiety mówców z gotowych TXT, SRT i VTT. Dodaj pliki lub wybierz Wszystkie gotowe, sprawdź podgląd i zapisz kopie. Timestampy oraz wypowiedzi pozostają bez zmian. Domyślny folder kopii to obrobione; oryginały pozostają zachowane.\n")
        content.insert("end", "Kolejka lokalna\n", "heading")
        content.insert("end", "Dodaj folder... otwiera wybór folderu i od razu importuje znalezione nagrania. Możesz też wkleić ścieżkę i kliknąć Dodaj z podanej ścieżki. Opcje Podfoldery i Tylko M4A określają zakres skanowania. Po imporcie lista pokazuje i zaznacza znalezione pozycje, także już obecne; komunikat podaje liczbę nowych nagrań i duplikatów. Ponowny import nie resetuje postępu. Materiały z błędem przywracasz przyciskiem Ponów nieudane. Start/Wznów lokalne obejmuje tylko pliki lokalne. Opcja Start tylko zaznaczonych ogranicza sesję bez wyłączania innych materiałów. Jeden wykonawca obsługuje jedną sesję; aby zmienić kolejkę, dokończ bieżące nagranie i zatrzymaj.\n")
        content.configure(state="disabled")
        Tooltip(content, "Przewijaj kółkiem myszy lub klawiszami Page Up / Page Down. Tekst możesz zaznaczyć i skopiować.", popup=False)
        help_button(frame, "Zamknij pomoc", dialog.destroy, "Wraca do kolejki bez zmiany jej stanu.").pack(anchor="e", pady=(10, 0))

    def run(self):
        self.window.mainloop()


# Preserve the entry point used by existing launchers and embedding code.
Window = QueueWindow
