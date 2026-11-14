"""Explicit subtitle review; originals and cue times are protected."""
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from ..common import atomic_bytes
from . import directory, place_dialog
from .captions import parse_srt, render, warnings


def save_captions(store, ident, lang, text):
    cues = parse_srt(text, duration=store.get_video(ident)["duration"])
    old = store.item(ident, "captions", lang)
    if old["value"]:
        previous = parse_srt(old["value"])
        before = [(c["id"], round(c["start"] * 1000), round(c["end"] * 1000)) for c in previous]
        after = [(c["id"], round(c["start"] * 1000), round(c["end"] * 1000)) for c in cues]
        if before != after:
            raise ValueError("Zmieniono liczbę, ID lub czasy segmentów. Edytor lokalizacji zachowuje oś czasu źródła.")
    path = directory(store.root, ident) / ("napisy." + lang + ".srt")
    normalized = render(cues)
    atomic_bytes(path, normalized.encode("utf-8"))
    store.save_item(ident, "captions", lang, normalized, old["source_hash"], str(path))
    return warnings(cues)


class CaptionEditor:
    def __init__(self, parent, store, ident, lang, changed):
        self.store, self.ident, self.lang, self.changed = store, ident, lang, changed
        self.dialog = tk.Toplevel(parent)
        self.dialog.title("Sprawdź napisy " + lang.upper())
        place_dialog(self.dialog, parent, 950, 650)
        ttk.Label(self.dialog, text="Popraw tekst wypowiedzi. Numery i timestampy pozostają zgodne ze źródłem.").pack(anchor="w", padx=12, pady=8)
        frame = ttk.Frame(self.dialog)
        frame.pack(fill="both", expand=True, padx=12)
        self.text = tk.Text(frame, wrap="word", undo=True)
        scroll = ttk.Scrollbar(frame, command=self.text.yview)
        self.text.configure(yscrollcommand=scroll.set)
        self.text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.text.insert("1.0", store.item(ident, "captions", lang)["value"])
        self.note = tk.StringVar(master=self.dialog)
        ttk.Label(self.dialog, textvariable=self.note, wraplength=900).pack(fill="x", padx=12, pady=8)
        row = ttk.Frame(self.dialog)
        row.pack(fill="x", padx=12, pady=(0, 12))
        for label, callback in (("Importuj poprawiony SRT", self.import_file), ("Sprawdź i zapisz", self.save),
                                ("Zapisz i zatwierdź", lambda: self.save(approve=True)), ("Zamknij", self.dialog.destroy)):
            ttk.Button(row, text=label, command=callback).pack(side="left", padx=(0, 6))

    def import_file(self):
        path = filedialog.askopenfilename(parent=self.dialog, filetypes=[("Napisy", "*.srt")])
        if path:
            try:
                content = Path(path).read_text(encoding="utf-8-sig")
                parse_srt(content)
                self.text.delete("1.0", "end")
                self.text.insert("1.0", content)
                self.note.set("Wczytano do edytora. Zapis sprawdzi komplet segmentów i ich czasy.")
            except (OSError, ValueError) as error:
                messagebox.showerror("Napisy", str(error), parent=self.dialog)

    def save(self, approve=False):
        try:
            notices = save_captions(self.store, self.ident, self.lang, self.text.get("1.0", "end-1c"))
            item = self.store.item(self.ident, "captions", self.lang)
            if approve:
                if not item["value"] and not messagebox.askyesno("Brak wypowiedzi", "Potwierdzasz, że materiał nie wymaga napisów?", parent=self.dialog):
                    return
                self.store.approve(self.ident, "captions", self.lang, allow_empty=not item["value"])
            self.note.set(("Zatwierdzono. " if approve else "Zapisano. ") + (" ".join(notices[:3]) or "Czasy i segmenty są poprawne."))
            self.changed()
        except (ValueError, OSError) as error:
            messagebox.showerror("Sprawdź napisy", str(error), parent=self.dialog)
