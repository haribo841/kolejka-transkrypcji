"""Localization workspace with background reads and separate preparation/publication states."""
import json
import os
from pathlib import Path
import queue
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import webbrowser

from ..common import WorkerLock, digest
from ..table_view import polish_key
from ..ui_help import Tooltip, help_button
from . import PUBLICATION, REVIEW, configuration_summary, directory, place_dialog
from .api import quota_day
from .auth import connection_status
from .editor import CaptionEditor
from .metadata import source_text
from .publish import frozen_approvals, preview
from .store import LocalizationStore, metadata_source
from .worker import launch, recover_session

COLUMNS = ("date", "title", "meta_pl", "meta_en", "cap_pl", "cap_en", "background", "audio_pl", "audio_en")
ENABLED_STATE = "!disabled"
HEADINGS = ("Data", "Nagranie", "Tytuł/opis PL", "Tytuł/opis EN", "Napisy PL", "Napisy EN", "Bez głosów", "Audio PL", "Audio EN")


def item_label(item):
    if item.get("error"):
        return "Sprawdź szczegóły"
    if item["publication"] in {"verified", "manual", "processing"}:
        return PUBLICATION[item["publication"]]
    return REVIEW[item["review"]]


def row_values(row):
    items = row["items"]
    values = [row["date"][:10], row["title"]]
    for lang in ("pl", "en"):
        pair = [items[k + ":" + lang] for k in ("title", "description")]
        labels = [item_label(i) for i in pair]
        label = labels[0] if labels[0] == labels[1] else " / ".join(labels)
        values.append("Błąd odczytu" if row.get("metadata_error") else label)
    values.extend(item_label(items[k]) for k in ("captions:pl", "captions:en", "audio:zxx", "audio:pl", "audio:en"))
    return tuple(values)


def matches(row, text, status):
    if text.casefold() not in (row["title"] + " " + row["video_id"]).casefold():
        return False
    items = list(row["items"].values())
    if status == "Z gotową transkrypcją":
        return row.get("transcription") == "done"
    if status == "Do przygotowania":
        return any(i["review"] == "missing" for i in items)
    if status == "Do sprawdzenia":
        return any(i["review"] == "review" for i in items)
    if status == "Do publikacji":
        return any(i["review"] == "approved" and i["publication"] != "verified" and i["kind"] != "audio" for i in items)
    return bool(row.get("metadata_error") or any(i["error"] for i in items)) if status == "Problemy" else True


class LocalizationTab:
    def __init__(self, app, parent):
        self.app, self.parent = app, parent
        self.store, self.rows, self.events = None, [], queue.Queue()
        self.closed, self.pending, self.after, self.wizard = False, False, None, None
        self.pending_background, self.pending_action, self.queued_action = False, "", None
        self.worker_busy, self.worker_mode, self.google_ready, self.next_snapshot = False, "", False, 0
        self.controls_state = None
        self.loaded, self.sort_column, self.sort_direction = "", None, None
        self.cache, self.visible, self.metadata = {}, [], {}
        self.field_baselines = {}
        self.approval_vars = {}
        self.note = tk.StringVar(master=parent, value="Wczytywanie katalogu lokalizacji w tle…")
        self.summary = tk.StringVar(master=parent)
        self.search, self.filter = tk.StringVar(master=parent), tk.StringVar(master=parent, value="Wszystkie")
        self.tasks = {k: tk.BooleanVar(master=parent, value=True) for k in ("metadata", "captions", "audio")}
        frame = ttk.Frame(parent, padding=10)
        frame.pack(fill="both", expand=True)
        toolbar = ttk.Frame(frame)
        toolbar.pack(fill="x")
        actions = [("Kreator lokalizacji", self.configure, "Połączenie konta Google, wybór kanału i lokalny model tłumaczeń."),
                   ("Odśwież z YouTube", self.sync, "Pobiera dane zaznaczonych filmów. Bez połączenia Google odczytuje publiczny tytuł i opis. Pełny katalog, lokalizacje i publikacja wymagają konta z kreatora."),
                   ("Następny nieukończony", self.next, "Przewija do pierwszego widocznego filmu wymagającego przygotowania albo sprawdzenia."),
                   ("Raport CSV", self.report, "Eksportuje stany przygotowania, publikacji i przyczyny problemów do Excela.")]
        for text, action, hint in actions:
            button = help_button(toolbar, text, action, hint, app.help_status)
            button.pack(side="left", padx=(0, 6))
            if action == self.sync:
                self.sync_button = button
        self.configuration = tk.StringVar(master=parent)
        ttk.Label(frame, textvariable=self.configuration, wraplength=1240).pack(fill="x", pady=(5, 0))
        filters = ttk.Frame(frame)
        filters.pack(fill="x", pady=6)
        ttk.Label(filters, text="Szukaj:").pack(side="left")
        entry = ttk.Entry(filters, textvariable=self.search, width=40)
        entry.pack(side="left", padx=6)
        combo = ttk.Combobox(filters, textvariable=self.filter, state="readonly", width=25, values=("Wszystkie", "Z gotową transkrypcją", "Do przygotowania", "Do sprawdzenia", "Do publikacji", "Problemy"))
        combo.pack(side="left", padx=6)
        self.search.trace_add("write", lambda *_: self.render(reset=True))
        combo.bind("<<ComboboxSelected>>", lambda _: self.render(reset=True))
        table = ttk.Frame(frame)
        table.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(table, columns=COLUMNS, show="headings", height=10, selectmode="extended")
        for column, title in zip(COLUMNS, HEADINGS):
            self.tree.heading(column, text=title, command=lambda c=column: self.sort(c))
            self.tree.column(column, width=350 if column == "title" else 105, minwidth=75, stretch=column == "title")
        vs = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        hs = ttk.Scrollbar(table, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vs.grid(row=0, column=1, sticky="ns")
        hs.grid(row=1, column=0, sticky="ew")
        table.rowconfigure(0, weight=1)
        table.columnconfigure(0, weight=1)
        self.tree.bind("<<TreeviewSelect>>", self.selection_changed)
        self.tree.bind("<Control-a>", self.select_all)
        self.tree.bind("<Double-1>", self.open_row)
        details = ttk.LabelFrame(frame, text="Wybrany film: oryginał z YouTube i przygotowane wersje", padding=6)
        details.pack(fill="x", pady=5)
        self.original = tk.StringVar(master=parent, value="Wybierz film. Status Gotowe w transkrypcji nie oznacza publikacji lokalizacji.")
        ttk.Label(details, textvariable=self.original, wraplength=1220).pack(fill="x")
        fields = ttk.Frame(details)
        fields.pack(fill="x")
        for index, lang in enumerate(("pl", "en")):
            box = ttk.LabelFrame(fields, text="Polski: wersja do sprawdzenia" if lang == "pl" else "English: version to review", padding=5)
            box.grid(row=0, column=index, sticky="nsew", padx=3)
            fields.columnconfigure(index, weight=1)
            title = tk.StringVar(master=parent)
            ttk.Entry(box, textvariable=title).pack(fill="x")
            description = tk.Text(box, height=3, wrap="word", undo=True)
            description.pack(fill="x", pady=3)
            row = ttk.Frame(box)
            row.pack(fill="x")
            self.metadata[lang] = (title, description)
            for kind, label in (("title", "Zatwierdź tytuł"), ("description", "Zatwierdź opis")):
                variable = tk.BooleanVar(master=parent)
                self.approval_vars[(kind, lang)] = variable
                check = ttk.Checkbutton(row, text=label, variable=variable, command=lambda kind=kind, language=lang: self.toggle_approval(kind, language))
                check.pack(side="left", padx=(0, 4))
                Tooltip(check, "Zapisuje i zatwierdza wyłącznie ten element. Odznaczenie cofa zgodę na jego publikację.", app.help_status)
            help_button(row, "Sprawdź napisy", lambda language=lang: self.captions(language), "Otwiera SRT do korekty i zatwierdzenia, z ochroną timestampów.", app.help_status).pack(side="left")
        row = ttk.Frame(details)
        row.pack(fill="x", pady=(4, 0))
        for text, callback in (("Zapisz teksty", lambda: self.save_fields(confirm_conflict=True)), ("Pokaż oryginalny opis", self.show_source), ("Język źródła / słownik", self.language_options),
                               ("Odsłuchaj podkład", self.play_background), ("Zatwierdź podkład", self.approve_background),
                               ("Potwierdź publikację audio", self.manual_audio)):
            ttk.Button(row, text=text, command=callback).pack(side="left", padx=(0, 5))
        choices = ttk.Frame(frame)
        choices.pack(fill="x")
        ttk.Label(choices, text="Przygotuj zaznaczone:").pack(side="left")
        for key, title in (("metadata", "Tytuły i opisy"), ("captions", "Napisy PL/EN"), ("audio", "Podkład bez głosów")):
            check = ttk.Checkbutton(choices, text=title, variable=self.tasks[key])
            check.pack(side="left", padx=5)
            Tooltip(check, "Wybór dotyczy następnej sesji przygotowania. Gotowe, niezmienione elementy są pomijane.", app.help_status)
        ttk.Label(frame, text="Metadane: zatwierdź tytuł i opis danego języka. Audio PL/EN: odłożone. Publikacja audio odbywa się w Studio.").pack(anchor="w", pady=3)
        ttk.Label(frame, textvariable=self.summary, wraplength=1250).pack(fill="x", pady=3)
        self.progress = ttk.Label(frame, textvariable=self.note, wraplength=1250)
        self.progress.pack(fill="x", pady=3)
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x")
        self.controls = {}
        for key, text, callback in (("prepare", "Przygotuj zaznaczone", self.prepare), ("preview", "Podgląd zmian", self.show_preview),
                                    ("publish", "Opublikuj zatwierdzone", lambda: self.show_preview(publish=True)),
                                    ("resume", "Wznów zapisaną sesję", self.resume), ("stop", "Dokończ bieżący i zatrzymaj", self.stop),
                                    ("files", "Otwórz pliki", self.open_files), ("studio", "Otwórz w Studio", self.open_studio)):
            button = help_button(buttons, text, callback, text + ". Zmiany widoku obowiązują od następnego Start.", app.help_status)
            button.pack(side="left", padx=(0, 5))
            self.controls[key] = button
        self.tick()

    def select_all(self, _event=None):
        self.tree.selection_set(self.tree.get_children())
        return "break"

    def open_row(self, event):
        if self.tree.identify_region(event.x, event.y) == "cell":
            ident = self.tree.identify_row(event.y)
            if ident:
                self.open_files(ident)

    def selected(self):
        selection = set(self.tree.selection())
        return [ident for ident in self.visible if ident in selection] if selection else []

    def perform(self, function, callback=None, *, background=False, action=""):
        if self.pending:
            return self.queue_action(function, callback, background, action)
        self.pending = True
        self.pending_background, self.pending_action = background, action
        self.update_action_controls()
        def run():
            try:
                result = function()
                self.events.put((callback, result, None))
            except Exception as error:
                self.events.put((None, None, str(error)))
        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        return True

    def queue_action(self, function, callback, background, action):
        if background:
            return False
        if self.pending_background and self.queued_action is None:
            self.queued_action = (function, callback, action)
            self.note.set("Polecenie przyjęte. Kończę odczyt listy w tle i wykonam je automatycznie.")
            self.update_action_controls()
            return True
        self.note.set("Polecenie jest już przyjęte albo trwa inna czynność. Poczekaj na jej wynik.")
        return False

    def update_action_controls(self):
        command_busy = (self.pending and not self.pending_background) or self.queued_action is not None
        busy = self.worker_busy or command_busy
        queued = self.queued_action[2] if self.queued_action else ""
        refreshing = self.pending_action == "sync" or queued == "sync" or (self.worker_busy and self.worker_mode in {"sync", "public_sync"})
        state = (busy, self.google_ready, refreshing, self.worker_busy)
        if state == self.controls_state:
            return
        self.controls_state = state
        for key in ("prepare", "publish", "resume"):
            self.controls[key].state(["disabled"] if busy else [ENABLED_STATE])
        if not self.google_ready:
            self.controls["publish"].state(["disabled"])
        self.sync_button.configure(text="Odświeżanie…" if refreshing else "Odśwież z YouTube")
        self.sync_button.state(["pressed"] if refreshing else ["!pressed"])
        self.sync_button.state(["disabled"] if busy else [ENABLED_STATE])
        self.controls["stop"].state([ENABLED_STATE] if self.worker_busy else ["disabled"])

    def snapshot(self):
        if self.store is None:
            self.store = LocalizationStore(self.app.root)
        if getattr(self, "job_count", -1) != len(self.app.jobs):
            self.store.bootstrap()
            self.job_count = len(self.app.jobs)
        control = recover_session(self.store)
        with self.store.base.connect() as db:
            row = db.execute("SELECT used FROM ytl_quota WHERE day=?", (quota_day(),)).fetchone()
        control["quota_used"] = row[0] if row else 0
        ident = self.loaded
        details = (ident, self.store.get_video(ident), self.store.items(ident)) if ident else None
        return self.store.rows(), control, details

    def received(self, result):
        self.rows, control, details = result
        self.configuration.set(configuration_summary(self.app.root))
        self.render()
        remaining = max(0, len(json.loads(control["ids"])) - control["cursor"])
        drafts = sum(j.get("status") == "draft" for j in self.app.jobs.values())
        self.summary.set(f"Filmy: {len(self.rows)} | Szkice bez ID: {drafts} (kolejka YouTube) | Widoczne: {len(self.visible)} | Zaznaczone: {len(self.selected())} | API dziś: {control.get('quota_used', 0)} jedn. | Sesja: {control['mode'] or 'brak'}, pozostało {remaining}. {control['message']}")
        if self.note.get() == "Wczytywanie katalogu lokalizacji w tle…":
            self.note.set("Wybierz filmy i kliknij Odśwież z YouTube. Potem Przygotuj zaznaczone wypełni wersje PL/EN.")
        was_busy = self.worker_busy
        self.worker_busy = WorkerLock.busy(self.app.root / "lokalizacje") or control["state"] == "starting"
        self.worker_mode = control["mode"]
        if was_busy and not self.worker_busy and control["message"]:
            self.note.set(control["message"])
        self.google_ready = connection_status(self.app.root).ready
        self.update_action_controls()
        current = next((r for r in self.rows if r["video_id"] == self.loaded), None)
        if current:
            for (kind, lang), variable in self.approval_vars.items():
                variable.set(current["items"][kind + ":" + lang]["review"] == "approved")
        if details:
            self.load_details(details, preserve_edits=True)

    def tick(self):
        if self.closed:
            return
        self.receive_events()
        if not self.pending:
            self.start_next_action()
        self.after = self.parent.after(150, self.tick)

    def receive_events(self):
        while True:
            try:
                callback, result, error = self.events.get_nowait()
            except queue.Empty:
                break
            self.complete_action(callback, result, error)
        self.update_action_controls()

    def complete_action(self, callback, result, error):
        background, action = self.pending_background, self.pending_action
        self.pending, self.pending_background, self.pending_action = False, False, ""
        if error:
            self.note.set(error)
            if not background:
                messagebox.showerror("Lokalizacje YouTube", error, parent=self.parent)
            return
        if action in {"sync", "prepare", "publish", "resume"}:
            self.worker_busy = True
            if action != "resume":
                self.worker_mode = action
            self.next_snapshot = 0
        if callback:
            callback(result)

    def start_next_action(self):
        if self.queued_action is not None:
            function, callback, action = self.queued_action
            self.queued_action = None
            self.perform(function, callback, action=action)
            return
        selected = self.selected()
        if len(selected) == 1 and selected[0] != self.loaded:
            self.selection_changed()
        elif time.monotonic() >= self.next_snapshot and (self.store is None or self.app.notebook.select() == str(self.parent)):
            self.next_snapshot = time.monotonic() + 1.5
            self.perform(self.snapshot, self.received, background=True)

    def render(self, reset=False):
        rows = [r for r in self.rows if matches(r, self.search.get(), self.filter.get())]
        if self.sort_direction:
            index = COLUMNS.index(self.sort_column)
            rows.sort(key=lambda r: polish_key(row_values(r)[index]), reverse=self.sort_direction == "desc")
        self.visible = [r["video_id"] for r in rows]
        desired = set(self.visible)
        y = self.tree.yview()
        for item in self.tree.get_children():
            if item not in desired:
                self.tree.delete(item)
                self.cache.pop(item, None)
        current = list(self.tree.get_children())
        reorder = current != self.visible
        for index, row in enumerate(rows):
            ident, values = row["video_id"], row_values(row)
            if not self.tree.exists(ident):
                self.tree.insert("", "end", iid=ident, values=values)
            elif self.cache.get(ident) != values:
                self.tree.item(ident, values=values)
            if reorder:
                self.tree.move(ident, "", index)
            self.cache[ident] = values
        if y:
            self.tree.yview_moveto(0 if reset else y[0])

    def sort(self, column):
        if self.sort_column != column:
            self.sort_column, self.sort_direction = column, "asc"
        else:
            self.sort_direction = {None: "asc", "asc": "desc", "desc": None}[self.sort_direction]
        arrow = {"asc": " ▲", "desc": " ▼"}.get(self.sort_direction, "")
        for key, title in zip(COLUMNS, HEADINGS):
            suffix = arrow if key == column else ""
            self.tree.heading(key, text=title + suffix)
        self.render(reset=True)

    def selection_changed(self, _event=None):
        ids = self.selected()
        if len(ids) != 1 or self.store is None or ids[0] == self.loaded:
            return
        if self.loaded and not self.save_fields():
            self.tree.selection_set(self.loaded)
            return
        ident = ids[0]
        def load():
            return ident, self.store.get_video(ident), self.store.items(ident)
        self.perform(load, self.load_details, background=True)

    def load_details(self, result, *, preserve_edits=False):
        ident, video, items = result
        if self.selected() != [ident]:
            return
        self.loaded = ident
        data = {(i["kind"], i["lang"]): i for i in items}
        snapshot = metadata_source(video)
        source = snapshot.get("snippet", {})
        errors = list(dict.fromkeys(i["error"] for i in items if i["error"]))
        status = "Publiczny odczyt; pozostałe lokalizacje nie zostały sprawdzone." if snapshot.get("source") == "public" else "Odczyt YouTube API."
        if not snapshot:
            status = "Tytuł z katalogu. Kliknij Odśwież z YouTube, aby pobrać opis. Próba modelu nie pobiera danych filmów."
        prepared = any(data[(kind, lang)]["content_hash"] for kind in ("title", "description") for lang in ("pl", "en"))
        if not prepared:
            status += " Wersje PL/EN pojawią się po Przygotuj zaznaczone."
        self.original.set(f"{source.get('title', video['title'])} | {status} {video['conflict']} {video.get('metadata_error', '')} " + " ".join(errors)[:600])
        for lang, widgets in self.metadata.items():
            self.load_language(lang, widgets, data, preserve_edits)

    def load_language(self, lang, widgets, data, preserve_edits):
        title, description = widgets
        for kind, current in (("title", title.get()), ("description", description.get("1.0", "end-1c"))):
            key = (kind, lang)
            baseline = self.field_baselines.get(key)
            if preserve_edits and baseline and current != baseline["value"]:
                continue
            item = data[key]
            if item["value"] != current:
                if kind == "title":
                    title.set(item["value"])
                else:
                    description.delete("1.0", "end")
                    description.insert("1.0", item["value"])
            self.field_baselines[key] = {"value": item["value"], "hash": item["content_hash"]}

    def confirm_field_change(self, kind, lang, old, baseline, text, confirm_conflict):
        if old["content_hash"] == baseline["hash"] or text == old["value"]:
            return True
        self.note.set("Pole " + kind + " " + lang.upper() + " zmieniło się w tle. Twoja korekta pozostaje w oknie. Kliknij Zapisz teksty, aby zdecydować o zastąpieniu nowszej wersji.")
        return confirm_conflict and messagebox.askyesno("Pole zmienione w tle", "Zastąpić nowszą zapisaną wersję pola " + kind + " " + lang.upper() + " Twoją korektą z okna? Poprzednia wersja pozostanie w historii.", parent=self.parent)

    def save_fields(self, *, confirm_conflict=False):
        if not self.loaded:
            return True
        changes = []
        for lang, (title, description) in self.metadata.items():
            for kind, text in (("title", title.get()), ("description", description.get("1.0", "end-1c"))):
                baseline = self.field_baselines.get((kind, lang))
                if baseline is None or text == baseline["value"]:
                    continue
                old = self.store.item(self.loaded, kind, lang)
                if not self.confirm_field_change(kind, lang, old, baseline, text, confirm_conflict):
                    return False
                changes.append((kind, lang, text, old))
        for kind, lang, text, old in changes:
            item = self.store.save_item(self.loaded, kind, lang, text, old["source_hash"])
            self.field_baselines[(kind, lang)] = {"value": item["value"], "hash": item["content_hash"]}
        if changes:
            self.note.set("Zapisano teksty. Zmienione pola wymagają ponownego zatwierdzenia.")
        return True

    def approve_field(self, kind, lang):
        try:
            if not self.loaded or self.selected() != [self.loaded]:
                self.approval_vars[(kind, lang)].set(False)
                self.note.set("Wybierz jeden film i poczekaj na wczytanie jego szczegółów.")
                return
            if not self.save_fields():
                self.approval_vars[(kind, lang)].set(False)
                return
            if not self.loaded:
                return
            empty = not self.store.item(self.loaded, kind, lang)["value"].strip()
            if empty and not messagebox.askyesno("Pusty opis", "Świadomie pozostawić to pole puste?", parent=self.parent):
                return
            if not self.store.item(self.loaded, kind, lang)["content_hash"]:
                self.store.save_item(self.loaded, kind, lang, "")
            self.store.approve(self.loaded, kind, lang, allow_empty=empty)
            self.note.set("Zatwierdzono " + kind + " " + lang.upper())
        except (ValueError, OSError) as error:
            self.approval_vars[(kind, lang)].set(False)
            messagebox.showerror("Zatwierdzenie", str(error), parent=self.parent)

    def toggle_approval(self, kind, lang):
        if not self.loaded:
            self.approval_vars[(kind, lang)].set(False)
        elif self.approval_vars[(kind, lang)].get():
            self.approve_field(kind, lang)
        else:
            self.store.revoke(self.loaded, kind, lang)
            self.note.set("Cofnięto zatwierdzenie tego pola.")

    def show_source(self):
        if not self.loaded:
            return
        ident = self.loaded
        video = self.store.get_video(ident)
        dialog = tk.Toplevel(self.parent)
        dialog.title("Oryginał i dotychczasowe lokalizacje YouTube")
        place_dialog(dialog, self.parent, 850, 600)
        text = tk.Text(dialog, wrap="word")
        text.pack(fill="both", expand=True, padx=10, pady=10)
        text.insert("1.0", source_text(video))
        text.configure(state="disabled")
        def refresh():
            dialog.destroy()
            self.sync([ident])
        ttk.Button(dialog, text="Pobierz tytuł i opis", command=refresh).pack(pady=6)

    def captions(self, lang):
        if self.loaded:
            ident = self.loaded
            CaptionEditor(self.parent, self.store, ident, lang,
                          lambda: self.note.set("Zmieniono napisy. Odświeżenie zachowa wybór i przewijanie."),
                          retranscribe=lambda parent: self.retranscribe(ident, parent))

    def retranscribe(self, ident, parent):
        from ..retranscription_ui import open_retranscription
        job_id = self.store.get_video(ident)["job_id"]
        def started():
            self.note.set("Nowa transkrypcja pracuje w kolejce YouTube. Po sukcesie użyj Przygotuj zaznaczone z włączonymi napisami PL/EN.")
        open_retranscription(parent, self.store.root, [job_id], started=started)

    def require_ids(self):
        ids = self.selected()
        if not ids:
            raise ValueError("Zaznacz filmy na liście. Ctrl/Shift pozwala wybrać kilka.")
        return ids

    def prepare(self):
        try:
            if not self.save_fields():
                return
            ids = self.require_ids()
            tasks = [k for k, value in self.tasks.items() if value.get()]
            if not tasks:
                raise ValueError("Wybierz co przygotować.")
            self.perform(lambda: launch(self.app.root, "prepare", ids, tasks), lambda _: self.note.set("Przygotowanie uruchomione. Kolejność i zakres zapisano; zamknięcie okna nie przerywa pracy."), action="prepare")
        except ValueError as error:
            self.note.set(str(error))

    def sync(self, ids=None):
        ids = list(self.selected() if ids is None else ids)
        status = connection_status(self.app.root)
        if not status.ready and not ids:
            self.note.set("Zaznacz filmy, aby pobrać publiczny tytuł i opis. Pełny katalog i publikacja wymagają połączenia Google: " + status.message)
            return
        mode = "sync" if status.ready else "public_sync"
        message = "Odświeżanie przez API rozpoczęte." if status.ready else "Pobieranie publicznych tytułów i opisów. Konto do publikacji nie jest połączone."
        scope = f"Zaznaczone filmy: {len(ids)}." if ids else "Cały katalog kanału."
        self.note.set("Przyjęto odświeżanie. " + scope)
        self.perform(lambda: launch(self.app.root, mode, ids), lambda _: self.note.set(message + " " + scope), action="sync")

    def show_preview(self, publish=False):
        try:
            if not self.save_fields():
                return
            ids = self.require_ids()
            frozen = frozen_approvals(self.store, ids)
            if not any(frozen.values()):
                raise ValueError("Zatwierdź tytuł i opis danego języka albo napisy. Potwierdzone już publikacje są pomijane.")
            dialog = tk.Toplevel(self.parent)
            dialog.title("Podgląd publikacji zatwierdzonych wersji")
            place_dialog(dialog, self.parent, 950, 650)
            text = tk.Text(dialog, wrap="word")
            text.pack(fill="both", expand=True, padx=10, pady=10)
            text.insert("1.0", preview(self.store, ids))
            text.configure(state="disabled")
            if publish:
                def submit():
                    self.perform(lambda: launch(self.app.root, "publish", ids, frozen=frozen), action="publish")
                    dialog.destroy()
                ttk.Button(dialog, text="Potwierdzam publikację powyższych wersji", command=submit).pack(pady=8)
            ttk.Button(dialog, text="Zamknij", command=dialog.destroy).pack(pady=6)
        except (ValueError, OSError) as error:
            messagebox.showerror("Podgląd", str(error), parent=self.parent)

    def stop(self):
        if self.store:
            self.store.session(stop=1)
            self.note.set("Zatrzymanie po ukończeniu całego bieżącego filmu.")

    def resume(self):
        from .worker import resume
        self.perform(lambda: resume(self.app.root), lambda _: self.note.set("Wznowiono zapisaną sesję, z jej poprzednim zakresem i zatwierdzeniami."), action="resume")

    def next(self):
        by_id = {row["video_id"]: row for row in self.rows}
        for ident in self.visible:
            if any(i["review"] in {"missing", "review"} for i in by_id[ident]["items"].values()):
                self.tree.selection_set(ident)
                self.tree.see(ident)
                return

    def open_files(self, ident=None):
        ident = ident or self.loaded
        if ident:
            path = directory(self.app.root, ident)
            path.mkdir(parents=True, exist_ok=True)
            os.startfile(str(path))

    def open_studio(self):
        if self.loaded:
            webbrowser.open("https://studio.youtube.com/video/" + self.loaded + "/translations")

    def play_background(self):
        if self.loaded:
            item = self.store.item(self.loaded, "audio", "zxx")
            if item["artifact_path"] and Path(item["artifact_path"]).is_file():
                os.startfile(item["artifact_path"])
            else:
                self.note.set("Najpierw przygotuj podkład bez głosów dla tego filmu.")

    def approve_background(self):
        ident = self.loaded
        if ident and messagebox.askyesno("Odsłuch podkładu", "Potwierdzasz odsłuch i zgodność podkładu z filmem?", parent=self.parent):
            def approve():
                item = self.store.item(ident, "audio", "zxx")
                if not item["artifact_path"] or digest(item["artifact_path"]) != item["value"]:
                    raise ValueError("Podkład nie istnieje albo został zmieniony.")
                self.store.approve(ident, "audio", "zxx")
            self.perform(approve, lambda _: self.note.set("Podkład zatwierdzony po odsłuchu i sprawdzeniu pliku."))

    def manual_audio(self):
        if self.loaded and messagebox.askyesno("Publikacja audio", "Czy rzeczywiście opublikowano tę ścieżkę w YouTube Studio? Sam zapis lokalny nie oznacza publikacji. Nie oznaczaj jej jako PL ani EN.", parent=self.parent):
            item = self.store.item(self.loaded, "audio", "zxx")
            if item["review"] != "approved":
                self.note.set("Najpierw odsłuchaj i zatwierdź podkład.")
                return
            self.store.mark(self.loaded, "audio", "zxx", "manual", item["content_hash"])

    def language_options(self):
        from .wizard import language_dialog
        if self.loaded:
            language_dialog(self.parent, self.store, self.loaded)

    def report(self):
        path = filedialog.asksaveasfilename(parent=self.parent, defaultextension=".csv", initialfile="lokalizacje.csv", filetypes=[("CSV", "*.csv")])
        if path and self.store:
            self.perform(lambda: self.store.csv(path), lambda _: self.note.set("Raport zapisany."))

    def configure(self):
        from .wizard import LocalizationWizard
        if self.wizard and not self.wizard.closed:
            self.wizard.dialog.lift()
        else:
            ids = self.selected()
            self.wizard = LocalizationWizard(self.app, selected_id=ids[0] if ids else None)

    def close(self):
        self.closed = True
        if self.after:
            self.parent.after_cancel(self.after)
        if self.wizard and not self.wizard.closed:
            self.wizard.close()
        thread = getattr(self, "thread", None)
        if thread and thread.is_alive():
            thread.join(timeout=.5)
