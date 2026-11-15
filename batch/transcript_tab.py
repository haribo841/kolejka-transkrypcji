"""File-based transcript processing, independent of audio/model inference."""
from __future__ import annotations

import os
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, ttk

from .common import job_folder
from .result_paths import output_paths
from .transcript_edit import AUTO_SPEAKER_SETTING, FORMATS, load_transcript, remove_speakers, save_without_speakers
from .ui_help import Tooltip, help_button, wrap_with_parent


class TranscriptTab:
    def __init__(self, app, parent):
        self.app, self.parent = app, parent
        self.files = {}
        self.busy = False
        self.cancel_requested = False
        self.on_finished = None
        self.save_after = self.preview_after = None
        self.pending = []
        self.buttons = []
        self.last_folder = None
        self.hint = tk.StringVar(master=parent, value="Dodaj gotowe transkrypcje. Zapis utworzy osobne kopie i zachowa oryginały.")
        self.status = tk.StringVar(master=parent, value="Nie dodano plików.")
        self.details = tk.StringVar(master=parent)
        self.remove_labels = tk.BooleanVar(master=parent, value=True)
        self.single_only = tk.BooleanVar(master=parent, value=True)
        self.auto_clean_new = tk.BooleanVar(master=parent, value=app.config.get(AUTO_SPEAKER_SETTING, True))
        self.destination_custom = False
        self.destination = tk.StringVar(master=parent)
        frame = ttk.Frame(parent, padding=12)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Obróbka gotowych transkrypcji", font=("Segoe UI", 15, "bold")).pack(anchor="w")
        subtitle = ttk.Label(frame, text="Usuwanie mówców zachowuje timestampy i treść wypowiedzi. Nie uruchamia ponownie transkrypcji audio.", wraplength=1200)
        subtitle.pack(anchor="w", pady=(4, 10))
        wrap_with_parent(subtitle, 24)
        toolbar = ttk.Frame(frame)
        toolbar.pack(fill="x")
        for text, command, hint in (
                ("Dodaj pliki TXT / SRT / VTT", self.pick_files, "Wybierz jedną lub wiele gotowych transkrypcji. Obsługiwane są TXT z czasami oraz napisy SRT i VTT."),
                ("Zaznaczone z kolejki", lambda: self.from_queue(True), "Dodaje transkrypcje TXT gotowych nagrań zaznaczonych w ostatnio używanej karcie Kolejka YouTube lub Kolejka lokalna."),
                ("Wszystkie gotowe", lambda: self.from_queue(False), "Dodaje transkrypcje TXT wszystkich ukończonych nagrań. Samo dodanie nie zapisuje jeszcze kopii."),
                ("Usuń z listy", self.remove_selected, "Usuwa zaznaczone pozycje wyłącznie z tej listy. Nie usuwa żadnych plików z dysku."),
                ("Wyczyść listę", self.clear, "Opróżnia listę do obróbki. Oryginały i zapisane kopie pozostają na dysku.")):
            self.button(toolbar, text, command, hint).pack(side="left", padx=(0, 6), pady=(0, 8))
        listing = ttk.Frame(frame)
        listing.pack(fill="x")
        self.tree = ttk.Treeview(listing, columns=("name", "status", "folder"), show="headings", height=6, selectmode="extended")
        for name, title, width in (("name", "Plik", 480), ("status", "Stan obróbki", 220), ("folder", "Folder źródłowy", 430)):
            self.tree.heading(name, text=title)
            self.tree.column(name, width=width, minwidth=80, stretch=True)
        scroll = ttk.Scrollbar(listing, orient="vertical", command=self.tree.yview)
        horizontal = ttk.Scrollbar(listing, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=scroll.set, xscrollcommand=horizontal.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        listing.columnconfigure(0, weight=1)
        self.tree.bind("<<TreeviewSelect>>", self.schedule_preview)
        self.tree.bind("<Control-a>", lambda event: (self.tree.selection_set(self.tree.get_children()), "break")[-1])
        Tooltip(self.tree, "Zaznacz plik, aby porównać oryginał i kopię. Ctrl/Shift wybiera wiele plików, Ctrl+A zaznacza wszystkie.", self.hint)
        options = ttk.Frame(frame)
        options.pack(fill="x", pady=8)
        self.operation = ttk.Checkbutton(options, text="Usuń oznaczenia mówców", variable=self.remove_labels, command=self.operation_changed)
        self.operation.pack(side="left", padx=(0, 20))
        Tooltip(self.operation, "Usuwa etykiety takie jak Mówca 1, Mówca nieustalony i Speaker 1 z początku wypowiedzi. Zachowuje czasy i tekst.", self.hint)
        self.single_option = ttk.Checkbutton(options, text="Tylko: Mówca 1 / Mówca nieustalony", variable=self.single_only, command=self.schedule_preview)
        self.single_option.pack(side="left", padx=(0, 20))
        Tooltip(self.single_option, "Sprawdza cały plik. Gdy występuje inny mówca, zachowuje etykiety i pomija zapis identycznej kopii. Odznaczenie usuwa wszystkie obsługiwane etykiety.", self.hint)
        ttk.Label(options, text="Timestampy: zachowaj     Treść wypowiedzi: zachowaj").pack(side="left")
        self.auto_option = ttk.Checkbutton(frame, text="Nowe transkrypcje: zapisuj bez etykiet, jeśli są tylko Mówca 1 i/lub Mówca nieustalony",
                                          variable=self.auto_clean_new, command=self.save_auto_option)
        self.auto_option.pack(anchor="w", pady=(0, 8))
        Tooltip(self.auto_option, "Dotyczy każdego nowego eksportu TXT, SRT i VTT z kolejki lokalnej i YouTube. Dane mówców pozostają w JSON. Istniejące transkrypcje dodaj do listy i zapisz serię kopii.", self.hint)
        preview = ttk.Panedwindow(frame, orient="horizontal")
        preview.pack(fill="both", expand=True)
        self.original = self.preview_box(preview, "Oryginał")
        self.edited = self.preview_box(preview, "Podgląd zapisywanej kopii")
        detail = ttk.Label(frame, textvariable=self.details, wraplength=1200)
        detail.pack(anchor="w", fill="x", pady=(6, 4))
        wrap_with_parent(detail, 24)
        destination_row = ttk.Frame(frame)
        destination_row.pack(fill="x", pady=4)
        ttk.Label(destination_row, text="Folder nowych kopii:").pack(side="left", padx=(0, 8))
        output = ttk.Entry(destination_row, textvariable=self.destination, state="readonly")
        output.pack(side="left", fill="x", expand=True, padx=(0, 8))
        Tooltip(output, "Kopie otrzymają dopisek _bez_mowcow. Jeśli nazwa jest zajęta, program doda numer i zachowa istniejący plik.", self.hint)
        self.button(destination_row, "Zmień folder", self.choose_folder, "Wybiera folder dla nowych kopii; oryginalne pliki pozostają na miejscu.").pack(side="left")
        self.button(destination_row, "Obok źródeł", self.use_source_folders, "Przywraca zapis obok każdego pliku źródłowego, także gdy dodano transkrypcje z różnych folderów.").pack(side="left", padx=(6, 0))
        actions = ttk.Frame(frame)
        actions.pack(fill="x", pady=6)
        self.save_selected = self.button(actions, "Zapisz zaznaczone", lambda: self.save(True), "Tworzy kopie bez mówców tylko dla zaznaczonych plików z listy powyżej.")
        self.save_selected.pack(side="left", padx=(0, 8))
        self.save_all = self.button(actions, "Zapisz wszystkie", lambda: self.save(False), "Tworzy kopie bez mówców dla wszystkich plików z tej listy. Nie zmienia kolejki nagrań.")
        self.save_all.pack(side="left", padx=(0, 8))
        self.cancel_button = help_button(actions, "Zatrzymaj zapisywanie", self.cancel,
                    "Zatrzymuje tę serię po zapisaniu bieżącego pliku. Gotowe kopie pozostają na dysku; kolejka nagrań działa niezależnie.", self.hint, state="disabled")
        self.cancel_button.pack(side="left", padx=(0, 8))
        help_button(actions, "Otwórz folder kopii", self.open_folder, "Otwiera folder ostatnio zapisanych kopii lub wybrany folder docelowy.", self.hint).pack(side="left")
        ttk.Label(frame, textvariable=self.status).pack(anchor="w", pady=(2, 6))
        help_frame = ttk.LabelFrame(frame, text="Podpowiedź", padding=(8, 5), height=66)
        help_frame.pack(fill="x")
        help_frame.pack_propagate(False)
        label = ttk.Label(help_frame, textvariable=self.hint, wraplength=1200, foreground="#344963")
        label.pack(fill="both", expand=True)
        wrap_with_parent(label, 20)

    def button(self, parent, text, command, hint):
        button = help_button(parent, text, command, hint, self.hint)
        self.buttons.append(button)
        return button

    def preview_box(self, parent, title):
        frame = ttk.LabelFrame(parent, text=title, padding=6)
        text = tk.Text(frame, height=8, width=40, wrap="word", font=("Consolas", 10), state="disabled")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        Tooltip(text, "Podgląd pierwszych 50 000 znaków. Zapis obejmuje cały plik. Tekst możesz zaznaczyć i skopiować.", self.hint)
        parent.add(frame, weight=1)
        return text

    def pick_files(self):
        paths = filedialog.askopenfilenames(parent=self.app.window, title="Gotowe transkrypcje do obróbki",
                    filetypes=[("Transkrypcje i napisy", "*.txt *.srt *.vtt")])
        self.add_paths(paths)

    def add_paths(self, paths):
        if self.busy:
            return
        added = 0
        for path in paths:
            path = Path(path).resolve()
            key = os.path.normcase(str(path))
            if path.suffix.lower() not in FORMATS or key in self.files:
                continue
            self.files[key] = path
            self.tree.insert("", "end", iid=key, values=(path.name, "Do obróbki", str(path.parent)))
            added += 1
        self.status.set(f"Dodano: {added}. Pliki na liście: {len(self.files)}.")
        if added:
            self.last_folder = None
        self.update_destination()
        if not self.tree.selection() and self.files:
            self.tree.selection_set(next(iter(self.files)))

    def from_queue(self, selected):
        ids = set(self.app.selected()) if selected else None
        if selected and not ids:
            self.status.set("Najpierw zaznacz gotowe nagrania w karcie Kolejka YouTube lub Kolejka lokalna.")
            return
        paths, missing = [], 0
        for job in self.app.store.jobs():
            if job["status"] != "done" or (ids is not None and job["id"] not in ids):
                continue
            try:
                path = output_paths(job_folder(self.app.root, job))["txt"]
                if not path.is_file():
                    raise FileNotFoundError(path)
                paths.append(path)
            except (ValueError, OSError, TypeError):
                missing += 1
        self.add_paths(paths)
        if missing:
            self.status.set(self.status.get() + f" Brak poprawnych plików dla nagrań: {missing}.")

    def remove_selected(self):
        if not self.busy:
            for key in self.tree.selection():
                self.files.pop(key, None)
                self.tree.delete(key)
            self.status.set(f"Pliki na liście: {len(self.files)}.")
            self.update_destination()
            self.schedule_preview()

    def clear(self):
        if not self.busy:
            self.tree.delete(*self.tree.get_children())
            self.files.clear()
            self.update_destination()
            self.schedule_preview()
            self.status.set("Lista jest pusta. Pliki na dysku pozostały bez zmian.")

    def schedule_preview(self, event=None):
        if self.preview_after:
            self.parent.after_cancel(self.preview_after)
        self.preview_after = self.parent.after(100, self.preview)

    def set_text(self, widget, text):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text[:50000])
        widget.configure(state="disabled")

    def preview(self):
        self.preview_after = None
        selected = self.tree.selection()
        if not selected:
            self.set_text(self.original, "")
            self.set_text(self.edited, "")
            self.details.set("Zaznacz plik, aby zobaczyć podgląd.")
            return
        try:
            loaded = load_transcript(self.files[selected[0]])
            result = remove_speakers(loaded.text, loaded.path.suffix, only_single=self.single_only.get())
            self.set_text(self.original, loaded.text)
            self.set_text(self.edited, result.text if self.remove_labels.get() else loaded.text)
            message = f"{loaded.path.name} | Przedziały czasowe: {result.cues} | Etykiety do usunięcia: {result.removed if self.remove_labels.get() else 0}"
            if result.removed == 0:
                message += " | Zachowano etykiety: występują inni mówcy." if result.labels else " | Brak etykiet mówców."
                if self.single_only.get():
                    message += " Zapis zostanie pominięty."
            self.details.set(message)
        except (ValueError, OSError) as exc:
            self.set_text(self.original, "")
            self.set_text(self.edited, "")
            self.details.set(str(exc))

    def operation_changed(self):
        state = "normal" if self.remove_labels.get() and not self.busy else "disabled"
        self.save_selected.configure(state=state)
        self.save_all.configure(state=state)
        self.schedule_preview()

    def choose_folder(self):
        initial = str(self.app.root)
        if self.destination_custom:
            initial = self.destination.get()
        elif self.files:
            initial = str(next(iter(self.files.values())).parent)
        path = filedialog.askdirectory(parent=self.app.window, title="Folder kopii bez mówców", initialdir=initial)
        if path:
            self.destination_custom = True
            self.last_folder = None
            self.destination.set(path)

    def update_destination(self):
        if self.destination_custom:
            return
        folders = {path.parent for path in self.files.values()}
        value = "Obok każdego pliku źródłowego" if len(folders) > 1 else ""
        if len(folders) == 1:
            value = str(next(iter(folders)))
        self.destination.set(value)

    def use_source_folders(self):
        self.destination_custom = False
        self.last_folder = None
        self.update_destination()

    def save_auto_option(self):
        if not self.app.save_setting(AUTO_SPEAKER_SETTING, self.auto_clean_new.get()):
            self.auto_clean_new.set(self.app.config.get(AUTO_SPEAKER_SETTING, True))

    def save(self, selected_only):
        if self.busy or not self.remove_labels.get():
            return
        ids = list(self.tree.selection() if selected_only else self.tree.get_children())
        if not ids:
            self.status.set("Dodaj pliki i zaznacz je lub użyj Zapisz wszystkie.")
            return
        self.pending = [(key, self.files[key]) for key in ids]
        self.save_folder = Path(self.destination.get()).resolve() if self.destination_custom else None
        self.save_single_only = self.single_only.get()
        self.total, self.saved, self.failed, self.skipped = len(ids), 0, 0, 0
        self.busy, self.cancel_requested = True, False
        for button in self.buttons:
            button.configure(state="disabled")
        self.operation.configure(state="disabled")
        self.single_option.configure(state="disabled")
        self.auto_option.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        self.save_after = self.parent.after(1, self.save_next)

    def save_next(self):
        self.save_after = None
        if not self.pending or self.cancel_requested:
            self.finish()
            return
        key, source = self.pending.pop(0)
        try:
            path, result = save_without_speakers(source, self.save_folder, only_single=self.save_single_only)
            if path is None:
                self.skipped += 1
                self.tree.set(key, "status", "Bez zmian: inni mówcy" if result.labels else "Bez zmian: brak etykiet")
                self.details.set(f"Pozostawiono bez zmian: {source}")
            else:
                self.saved += 1
                self.last_folder = path.parent
                self.tree.set(key, "status", f"Zapisano; usunięto etykiet: {result.removed}")
                self.details.set(f"Zapisano: {path}")
        except (ValueError, OSError) as exc:
            self.failed += 1
            self.tree.set(key, "status", "Błąd: " + str(exc))
            self.details.set(f"{source.name}: {exc}")
        self.status.set(f"Zapisano: {self.saved}; bez zmian: {self.skipped}; błędy: {self.failed}; pozostało: {len(self.pending)} z {self.total}.")
        # Work on one text file per UI iteration so stopping and closing remain responsive.
        self.save_after = self.parent.after(10, self.save_next)

    def cancel(self):
        self.cancel_requested = True

    def finish(self):
        self.busy = False
        for button in self.buttons:
            button.configure(state="normal")
        self.operation.configure(state="normal")
        self.single_option.configure(state="normal")
        self.auto_option.configure(state="normal")
        self.cancel_button.configure(state="disabled")
        self.status.set(f"{'Zatrzymano' if self.cancel_requested else 'Zakończono'}. Zapisano: {self.saved}; bez zmian: {self.skipped}; błędy: {self.failed}. Oryginały zachowane.")
        self.pending.clear()
        if self.on_finished:
            callback, self.on_finished = self.on_finished, None
            callback()

    def open_folder(self):
        selected = self.tree.selection()
        source = self.files[selected[0]] if selected else next(iter(self.files.values()), None)
        path = self.last_folder
        if path is None and self.destination_custom:
            path = Path(self.destination.get())
        if path is None and source is not None:
            path = source.parent
        if path is None:
            self.status.set("Najpierw dodaj transkrypcje albo wybierz folder kopii.")
            return
        if path.is_dir():
            os.startfile(path)
        else:
            self.status.set("Folder kopii powstanie podczas pierwszego zapisu.")

    def close(self):
        for callback in (self.preview_after, self.save_after):
            if callback:
                self.parent.after_cancel(callback)
