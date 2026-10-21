"""Linear, explicitly initiated setup of the current transcription pipeline."""
from __future__ import annotations

from pathlib import Path
import time
import tkinter as tk
from tkinter import filedialog, ttk

from .common import WorkerLock, read_json
from .readiness import AVAILABLE, CHECKING, OFF, PASSED
from .ui_help import help_button, wrap_with_parent


STEPS = ("Whisper i narzędzia", "Opcjonalny UVR", "Opcjonalni mówcy", "Opcjonalny YouTube", "Próba i zakończenie")
READY = {AVAILABLE, PASSED, OFF}
MODEL_STATE_FILE = "konfiguracja-modelu.json"


class ConfigurationWizard:
    def __init__(self, app):
        self.app, self.closed, self.index = app, False, 0
        self.after_id, self.started = None, 0
        self.operation = None
        self.seen_busy = False
        self.before_state = None
        self.dialog = tk.Toplevel(app.window)
        self.dialog.title("Kreator konfiguracji Kolejki transkrypcji")
        self.dialog.geometry("820x600")
        self.dialog.minsize(740, 560)
        self.dialog.transient(app.window)
        self.dialog.protocol("WM_DELETE_WINDOW", self.close)
        outer = ttk.Frame(self.dialog, padding=20)
        outer.pack(fill="both", expand=True)
        self.heading = tk.StringVar(master=self.dialog)
        ttk.Label(outer, textvariable=self.heading, font=("Segoe UI", 16, "bold")).pack(anchor="w")
        ttk.Label(outer, text="Wykonuj po jednym kroku. Dalej uruchamia potrzebną czynność albo przechodzi do następnego kroku.",
                  wraplength=760).pack(anchor="w", fill="x", pady=(8, 12))
        self.step_progress = ttk.Progressbar(outer, maximum=len(STEPS), mode="determinate")
        self.step_progress.pack(fill="x")
        self.content = ttk.Frame(outer)
        self.content.pack(fill="both", expand=True, pady=16)
        self.pages, self.options = [], []
        self.uvr = tk.BooleanVar(master=self.dialog, value=app.config.get("uvr_enabled", False))
        self.diarization = tk.BooleanVar(master=self.dialog, value=app.config.get("diarization", False))
        self.youtube = tk.BooleanVar(master=self.dialog, value=app.selection_queue == "youtube")
        self.status = tk.StringVar(master=self.dialog, value="Sprawdzam istniejące ustawienia w tle…")
        for build in (self.build_core, self.build_uvr, self.build_speakers, self.build_youtube, self.build_trial):
            page = ttk.Frame(self.content)
            self.pages.append(page)
            build(page)
        status = ttk.Label(outer, textvariable=self.status, wraplength=760, justify="left")
        status.pack(fill="x", pady=(0, 12))
        wrap_with_parent(status, 40)
        self.activity = ttk.Progressbar(outer, mode="indeterminate")
        self.activity.pack(fill="x", pady=(0, 12))
        footer = ttk.Frame(outer)
        footer.pack(fill="x")
        self.back_button = ttk.Button(footer, text="Wstecz", command=self.back)
        self.back_button.pack(side="left")
        self.next_button = ttk.Button(footer, text="Dalej", command=self.advance)
        self.next_button.pack(side="right")
        ttk.Button(footer, text="Zamknij kreator", command=self.close).pack(side="right", padx=8)
        ttk.Label(outer, text="Konfiguracja i próba nie uruchamiają kolejki. Po zakończeniu wybierzesz Start samodzielnie.",
                  wraplength=760).pack(fill="x", pady=(12, 0))
        self.show_page()
        app.request_readiness(force=True)
        self.poll()

    def note(self, page, text):
        label = ttk.Label(page, text=text, wraplength=740, justify="left")
        label.pack(fill="x", pady=(0, 14))
        wrap_with_parent(label, 12)

    def build_core(self, page):
        self.note(page, "Whisper rozpoznaje mowę lokalnie na CPU. Najpierw sprawdzimy FFmpeg, miejsce na dysku i kompletność modelu. "
                  "Jeżeli modelu brakuje, Dalej pobierze przypiętą wersję (~1,63 GB). Poczekaj, aż pojawi się potwierdzenie zakończenia.")
        self.model_path = tk.StringVar(master=self.dialog, value=self.app.config["model"])
        ttk.Label(page, text="Folder modelu Whisper:").pack(anchor="w")
        ttk.Label(page, textvariable=self.model_path, wraplength=740).pack(anchor="w", pady=(4, 12))
        ttk.Button(page, text="Wybierz istniejący folder modelu…", command=self.choose_model).pack(anchor="w")
        ttk.Button(page, text="Ustawienia ścieżek FFmpeg i środowisk…", command=self.advanced).pack(anchor="w", pady=8)

    def build_uvr(self, page):
        option = ttk.Checkbutton(page, text="Oddzielaj wokal od muzyki przez UVR", variable=self.uvr, command=self.save_uvr)
        option.pack(anchor="w", pady=(0, 14))
        self.options.append(option)
        self.note(page, "Zaznacz tę opcję, jeśli transkrypcja ma korzystać z wydzielonego wokalu. "
                  "Dalej przygotuje model i poczeka na zakończenie pobierania. Rzeczywiste działanie sprawdzimy dopiero w ostatnim kroku. "
                  "Odznaczenie pomija UVR i nie usuwa pobranego modelu.")
        self.note(page, "UVR zachowuje audio źródłowe i wokal. Przy braku środowiska Python zainstaluj je przez Instaluj-UVR.ps1 w folderze aplikacji.")

    def build_speakers(self, page):
        option = ttk.Checkbutton(page, text="Rozróżniaj mówców przez Community-1", variable=self.diarization,
                                 command=self.save_speakers)
        option.pack(anchor="w", pady=(0, 14))
        self.options.append(option)
        self.note(page, "Dalej otworzy osobny kreator zgody na dostęp, tokenu Read i pobierania Community-1. "
                  "Po zakończeniu wróć tutaj. Token wpisujesz tylko w ukrytym polu tego kreatora. "
                  "Jeżeli model jest już kompletny, przejdziesz dalej bez tokenu.")
        self.note(page, "Możesz odznaczyć rozróżnianie mówców. Wtedy ten krok zostanie pominięty, a tekst nadal otrzyma timestampy.")

    def build_youtube(self, page):
        option = ttk.Checkbutton(page, text="Chcę też pobierać filmy z YouTube", variable=self.youtube, command=self.refresh)
        option.pack(anchor="w", pady=(0, 14))
        self.options.append(option)
        self.note(page, "Do nagrań lokalnych ten krok nie jest potrzebny. Jeśli go włączysz, Dalej otworzy konfigurację dostępu: "
                  "wybierz profil, otwórz film i zaloguj się, następnie sprawdź dostęp do jednego linku. "
                  "Czekaj na wynik testu, po sukcesie zatwierdź go przyciskiem Gotowe - użyj i zamknij.")
        self.note(page, "Ten test sprawdza połączenie. Nie pobiera całej kolejki ani nie uruchamia transkrypcji.")

    def build_trial(self, page):
        self.note(page, "Ostatni krok sprawdza cały wybrany pipeline na maksymalnie 60 sekundach lokalnego nagrania. "
                  "Kliknij Dalej, wskaż plik z mową i poczekaj. Użyjemy UVR oraz rozróżniania mówców tylko wtedy, gdy zostały włączone.")
        self.note(page, "Zielone potwierdzenie wymaga faktycznej próby bieżących ustawień. Sama obecność modeli na dysku tego nie potwierdza. "
                  "Po sukcesie możesz otworzyć wyniki i zakończyć kreator. Produkcyjna kolejka nadal będzie zatrzymana.")
        help_button(page, "Otwórz wyniki ostatniej próby", self.app.config_tab.open_test,
                    "Otwiera zapisany tekst i napisy próbki. Nie uruchamia kolejki.").pack(anchor="w")

    def choose_model(self):
        if self.busy():
            return
        path = filedialog.askdirectory(parent=self.dialog, title="Istniejący folder modelu Whisper")
        if path and self.app.save_setting("model", path):
            self.model_path.set(path)
        self.refresh()

    def advanced(self):
        self.app.open_configuration()
        self.close()

    def save_uvr(self):
        self.app.uvr_enabled.set(self.uvr.get())
        self.app.save_audio_options()
        self.uvr.set(self.app.config.get("uvr_enabled", False))
        self.refresh()

    def save_speakers(self):
        self.app.save_setting("diarization", self.diarization.get())
        self.diarization.set(self.app.config.get("diarization", False))
        self.app.config_tab.diarization.set(self.diarization.get())
        self.refresh()

    def busy(self):
        return (self.operation is not None or self.app.start_pending or WorkerLock.busy(self.app.root)
                or WorkerLock.busy(self.app.root / "konfiguracja")
                or WorkerLock.busy(Path(self.app.config["uvr_model_dir"])))

    def components(self):
        return self.app.readiness.get("components", {})

    def component(self, key):
        return self.components().get(key, {"status": CHECKING, "message": "Poczekaj na sprawdzenie ustawień."})

    def ready(self, key):
        return self.component(key)["status"] in READY

    def action(self):
        if self.index == 0 and not self.ready("whisper"):
            return "Pobierz Whisper", self.ready("tools")
        if self.index == 1 and self.uvr.get() and not self.ready("uvr"):
            return "Przygotuj UVR", True
        if self.index == 2 and self.diarization.get() and not self.ready("diarization"):
            return "Skonfiguruj mówców", True
        if self.index == 3 and self.youtube.get() and self.component("youtube")["status"] != PASSED:
            return "Połącz YouTube", True
        if self.index == 4:
            return ("Zakończ", True) if self.app.readiness.get("passed") else ("Wybierz plik i wykonaj próbę", all(
                self.ready(key) for key in ("whisper", "tools", "uvr", "diarization")))
        return "Dalej", self.index != 0 or self.ready("tools")

    def refresh(self):
        if self.closed:
            return
        busy = self.busy()
        checking = self.app.readiness_busy
        label, enabled = self.action()
        self.next_button.configure(text="Poczekaj…" if busy else label, state="normal" if enabled and not busy and not checking else "disabled")
        self.back_button.configure(state="normal" if self.index and not busy else "disabled")
        for option in self.options:
            option.configure(state="disabled" if busy else "normal")
        if busy:
            self.activity.start(15)
            self.status.set(self.operation_message())
        else:
            self.activity.stop()
            self.status.set("Sprawdzanie ustawień w tle. Poczekaj…" if checking else self.step_message())

    def step_message(self):
        keys = (("whisper", "tools"), ("uvr",), ("diarization",), ("youtube",), ())[self.index]
        if self.index == 3 and not self.youtube.get():
            return "YouTube pominięty. Możesz przejść dalej i używać kolejki lokalnej."
        if self.index == 4:
            return "Próba zaliczona. Konfiguracja gotowa; Start pozostaje ręczny." if self.app.readiness.get("passed") else "Czeka na próbę aktualnych ustawień."
        return "\n".join(self.component(key)["status"] + ": " + self.component(key)["message"] for key in keys)

    def operation_message(self):
        if self.operation:
            name = "konfiguracja-uvr.json" if self.operation == "uvr" else MODEL_STATE_FILE
            state = read_json(self.app.root / name, {})
            if state != self.before_state:
                return state.get("message", "Trwa przygotowanie. Poczekaj…")
        return "Trwa pobieranie, próba lub sesja. Poczekaj na zakończenie. Następny krok jest zablokowany."

    def show_page(self):
        for page in self.pages:
            page.pack_forget()
        self.pages[self.index].pack(fill="both", expand=True)
        self.heading.set(f"Krok {self.index + 1} z {len(STEPS)}: {STEPS[self.index]}")
        self.step_progress.configure(value=self.index + 1)

    def back(self):
        if not self.busy() and self.index:
            self.index -= 1
            self.show_page()
            self.refresh()

    def started_operation(self, name, before):
        self.operation, self.before_state = name, before
        self.started, self.seen_busy = time.monotonic(), False
        self.refresh()

    def advance(self):
        if self.busy() or self.app.readiness_busy or not self.action()[1]:
            return
        if self.index == 0 and not self.ready("whisper"):
            before = read_json(self.app.root / MODEL_STATE_FILE, {})
            if self.app.setup_process({"download_whisper": True}):
                self.started_operation("whisper", before)
        elif self.index == 1 and self.uvr.get() and not self.ready("uvr"):
            before = read_json(self.app.root / "konfiguracja-uvr.json", {})
            self.app.setup_uvr()
            self.started_operation("uvr", before)
        elif self.index == 2 and self.diarization.get() and not self.ready("diarization"):
            self.app.configure_model()
        elif self.index == 3 and self.youtube.get() and self.component("youtube")["status"] != PASSED:
            self.app.configure_youtube()
        elif self.index == 4:
            self.finish_or_test()
        else:
            self.index += 1
            self.show_page()
        self.refresh()

    def finish_or_test(self):
        if self.app.readiness.get("passed"):
            self.close()
            return
        path = filedialog.askopenfilename(parent=self.dialog, title="Nagranie z mową do próby (do 60 sekund)")
        before = read_json(self.app.root / MODEL_STATE_FILE, {})
        if path and self.app.setup_process({"test_only": True, "source": path, "track": 0,
                                           "language": self.app.config.get("language", "auto")}):
            self.started_operation("trial", before)

    def operation_finished(self):
        if self.operation is None:
            return
        lock = Path(self.app.config["uvr_model_dir"]) if self.operation == "uvr" else self.app.root / "konfiguracja"
        active = WorkerLock.busy(lock)
        self.seen_busy |= active
        if not active and (self.seen_busy or time.monotonic() - self.started >= 4):
            self.operation = None
            self.app.request_readiness(force=True)

    def poll(self):
        if self.closed:
            return
        self.operation_finished()
        self.refresh()
        self.after_id = self.dialog.after(400, self.poll)

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.activity.stop()
        if self.after_id:
            self.dialog.after_cancel(self.after_id)
        self.dialog.destroy()
