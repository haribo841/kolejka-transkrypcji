"""Explicit ASR retry options shared by queues and the subtitle editor."""
import queue
from pathlib import Path
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from .common import WorkerLock, settings
from .retranscription import new_request, retained_inputs
from .store import Store


INPUTS = {"original": "Zachowane oryginalne audio (bez UVR)",
          "vocal": "Zachowany wokal UVR", "source": "Źródło nagrania (plik lub ponowne pobranie)"}
LANGUAGES = {"auto": "Automatycznie", "pl": "Polski", "en": "Angielski"}


def open_retranscription(parent, root, ids, *, started=None):
    root = Path(root)
    if len(ids) != 1:
        messagebox.showinfo("Nowa transkrypcja", "Zaznacz dokładnie jedno nagranie do ponownej transkrypcji.", parent=parent)
        return None
    if any(WorkerLock.busy(root / name) for name in (".", "uruchamianie", "konfiguracja", "lokalizacje", "lokalizacje/uruchamianie")):
        messagebox.showinfo("Trwa praca", "Dokończ bieżące nagranie i zatrzymaj aktywną sesję przed ponowną transkrypcją.", parent=parent)
        return None
    job = Store(root).get(ids[0])
    if not job or job["status"] in {"draft", "skipped"}:
        messagebox.showinfo("Nowa transkrypcja", "Wybierz nagranie audio lub wideo z dostępnym źródłem.", parent=parent)
        return None
    return RetryTranscriptionDialog(parent, root, job, started)


class RetryTranscriptionDialog:
    def __init__(self, parent, root, job, started):
        self.root, self.job, self.started = root, job, started
        self.events, self.running, self.poll = queue.Queue(), False, None
        config = settings(root)
        self.dialog = tk.Toplevel(parent)
        self.dialog.title("Transkrybuj ponownie")
        self.dialog.transient(parent)
        self.dialog.protocol("WM_DELETE_WINDOW", self.close)
        body = ttk.Frame(self.dialog, padding=14)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=job["title"], wraplength=600).pack(anchor="w", pady=(0, 10))
        available = retained_inputs(root, job)
        self.inputs = [INPUTS[k] for k in ("original", "vocal", "source") if k == "source" or k in available]
        self.input = tk.StringVar(master=self.dialog, value=self.inputs[0])
        self.language = tk.StringVar(master=self.dialog, value=LANGUAGES.get(job["language"], LANGUAGES["auto"]))
        self.seconds = tk.StringVar(master=self.dialog, value="60")
        self.diarization = tk.BooleanVar(master=self.dialog, value=bool(config["diarization"]))
        for label, variable, values in (("Audio do rozpoznania", self.input, self.inputs),
                                        ("Język mowy (nie język napisów)", self.language, list(LANGUAGES.values())),
                                        ("Długość bloków w sekundach", self.seconds, ("30", "60", "120", "600"))):
            ttk.Label(body, text=label).pack(anchor="w", pady=(6, 2))
            ttk.Combobox(body, textvariable=variable, values=values, state="readonly", width=62).pack(fill="x")
        ttk.Checkbutton(body, text="Rozpoznawaj mówców", variable=self.diarization).pack(anchor="w", pady=10)
        ttk.Label(body, wraplength=610, text=(
            "Whisper rozpozna całe nagranie od nowa. Krótsze bloki i wybór języka mogą pomóc przy błędach, "
            "ale ponowienie nie gwarantuje innego tekstu. Zachowane audio i poprzednie wersje TXT/SRT/VTT/JSON pozostaną na dysku. "
            "UVR nie zostanie uruchomiony ponownie.")).pack(fill="x")
        if job["kind"] == "youtube":
            ttk.Label(body, wraplength=610, text=(
                "Po sukcesie przygotuj ponownie napisy PL/EN w Lokalizacjach YouTube i sprawdź je przed zatwierdzeniem. "
                "Dotychczasowe napisy zostaną zarchiwizowane; metadane i publikacje na YouTube pozostaną bez zmian.")).pack(fill="x", pady=8)
        self.note = tk.StringVar(master=self.dialog)
        ttk.Label(body, textvariable=self.note, wraplength=610).pack(fill="x", pady=6)
        row = ttk.Frame(body)
        row.pack(fill="x")
        self.start_button = ttk.Button(row, text="Transkrybuj ponownie", command=self.start)
        self.start_button.pack(side="left")
        ttk.Button(row, text="Zamknij", command=self.close).pack(side="right")
        self.dialog.bind("<Escape>", lambda _: self.close())

    def start(self):
        if self.running:
            return
        request = new_request(input_kind=next(k for k, v in INPUTS.items() if v == self.input.get()),
                              language=next(k for k, v in LANGUAGES.items() if v == self.language.get()),
                              seconds=int(self.seconds.get()), diarization=self.diarization.get())
        self.running = True
        self.start_button.configure(state="disabled")
        self.note.set("Uruchamianie jednej nowej transkrypcji w tle…")
        threading.Thread(target=self.launch, args=(request,), daemon=True).start()
        self.poll = self.dialog.after(100, self.tick)

    def launch(self, request):
        from .worker import launch
        try:
            launch(self.root, kind=self.job["kind"], job_ids=[self.job["id"]], order_ids=[self.job["id"]],
                   scope_mode="selected", retry_request=request)
            self.events.put(None)
        except Exception as error:
            self.events.put(str(error))

    def tick(self):
        self.poll = None
        try:
            error = self.events.get_nowait()
        except queue.Empty:
            self.poll = self.dialog.after(100, self.tick)
            return
        self.running = False
        if error:
            self.note.set("Nie uruchomiono: " + error)
            self.start_button.configure(state="normal")
        else:
            self.note.set("Zlecono nową transkrypcję. Postęp jest widoczny w kolejce " +
                          ("YouTube." if self.job["kind"] == "youtube" else "lokalnej."))
            if self.started:
                self.started()

    def close(self):
        if self.running:
            self.note.set("Trwa uruchamianie. Poczekaj na potwierdzenie; kolejne nagrania nie zostaną uruchomione.")
            return
        if self.poll:
            self.dialog.after_cancel(self.poll)
        self.dialog.destroy()
