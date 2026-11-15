"""Sequential setup with visible asynchronous progress and explicit optional account steps."""
import json
from datetime import datetime, timezone
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import tkinter as tk
from tkinter import filedialog, ttk
import webbrowser

from ..common import APP, NO_WINDOW, WorkerLock, atomic_json, read_json
from ..processes import ChildGuard
from . import options, place_dialog
from .api import YouTube
from .auth import AuthError, WindowsVault, client_path, client_settings, connect, connection_status, import_client
from .metadata import refresh_public, source_text
from .prepare import child
from .store import LocalizationStore, metadata_source
from .translation import REVISION, ready

STEPS = ("Klient Google OAuth", "Połączenie konta", "Wybór kanału", "Środowisko tłumaczeń", "Model lokalny", "Próba tłumaczenia", "Pobranie danych YouTube")


def save_options(root, **changes):
    value = options(root)
    value.update(changes)
    atomic_json(Path(root) / "lokalizacje-ustawienia.json", value)


def install_environment():
    interpreter = APP / ".venv-translation" / "Scripts" / "python.exe"
    if not interpreter.exists():
        launcher = shutil.which("py")
        if not launcher:
            raise RuntimeError("Zainstaluj Python 3.12 z launcherem py, a następnie ponów ten krok.")
        subprocess.run([launcher, "-3.12", "-m", "venv", str(interpreter.parent.parent)], check=True,
                       creationflags=NO_WINDOW, shell=False, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return interpreter


class LocalizationWizard:
    def __init__(self, app, *, selected_id=None):
        status = connection_status(app.root)
        self.app, self.step, self.closed, self.busy, self.complete = app, status.step if not status.ready else 3, False, False, False
        self.selected_id = selected_id
        self.operation, self.login_cancel, self.retry_login = "", None, False
        self.events, self.channel_data = queue.Queue(), []
        self.dialog = tk.Toplevel(app.window)
        self.dialog.title("Lokalizacje YouTube: konfiguracja krok po kroku")
        place_dialog(self.dialog, app.window, 790, 510)
        self.dialog.transient(app.window)
        self.dialog.protocol("WM_DELETE_WINDOW", self.close)
        self.title = tk.StringVar(master=self.dialog)
        self.message = tk.StringVar(master=self.dialog)
        ttk.Label(self.dialog, textvariable=self.title, font=("Segoe UI", 15, "bold")).pack(anchor="w", padx=20, pady=15)
        self.body = ttk.Frame(self.dialog, padding=(20, 0))
        self.body.pack(fill="both", expand=True)
        ttk.Label(self.dialog, textvariable=self.message, wraplength=740).pack(fill="x", padx=20, pady=8)
        nav = ttk.Frame(self.dialog, padding=15)
        nav.pack(fill="x")
        self.back = ttk.Button(nav, text="Wstecz", command=self.previous)
        self.back.pack(side="left")
        self.skip = ttk.Button(nav, text="Pomiń Google (bez publikacji)", command=self.skip_account)
        self.skip.pack(side="left", padx=8)
        self.next = ttk.Button(nav, text="Dalej", command=self.advance)
        self.next.pack(side="right")
        self.draw()
        self.after = self.dialog.after(200, self.poll)

    def draw(self):
        for child_widget in self.body.winfo_children():
            child_widget.destroy()
        self.title.set(f"Krok {self.step + 1} z {len(STEPS)}: {STEPS[self.step]}")
        self.message.set("")
        self.complete = False
        self.back.state(["disabled"] if self.step == 0 else ["!disabled"])
        self.skip.state(["!disabled"] if self.step < 3 else ["disabled"])
        hints = [
            "Utwórz projekt w Google Cloud, włącz YouTube Data API v3 i skonfiguruj ekran zgody. Dodaj swoje konto jako użytkownika testowego. Utwórz klienta OAuth typu Desktop app i pobierz JSON. To połączenie służy publikacji; cookies do pobierania audio są ustawiane osobno.",
            "Kliknij Połącz konto. W przeglądarce wybierz konto i udziel dostępu do YouTube. Token odświeżania zostanie zapisany w magazynie poświadczeń Windows. Przy wygaśnięciu uprawnień wróć do tego kroku.",
            "Pobierz listę kanałów przypisanych do połączonego konta i wybierz kanał publikacji. Aplikacja sprawdzi właściciela każdego filmu przed zapisem.",
            "Zainstalujemy osobne środowisko Python 3.12 z przypiętymi zależnościami. Whisper i UVR zachowują swoje środowiska. Poczekaj na koniec instalacji przed Dalej.",
            "Pobierzemy MADLAD-400 3B i przekonwertujemy do INT8 na CPU. Pobieranie wymaga około 12 GB, a konwersja dodatkowego miejsca. Pliki robocze trafiają na dysk systemowy, model do folderu danych. Zakończ aktywną kolejkę przed rozpoczęciem.",
            "Krótka próba tłumaczy tekst PL i EN lokalnie. Nie publikuje niczego i nie uruchamia kolejki filmów. Po próbie przejrzyj przykłady w folderze lokalizacje/proba.",
            "Osobno sprawdzimy pobranie prawdziwego tytułu i opisu jednego filmu. Bez konta Google dostępny jest publiczny odczyt. Pełne lokalizacje, napisy na kanale i publikacja wymagają kroków 1-3. Żaden test modelu nie zastępuje połączenia Google.",
        ]
        ttk.Label(self.body, text=hints[self.step], wraplength=735, justify="left").pack(fill="x", pady=(0, 15))
        actions = [("Otwórz Google Cloud", lambda: webbrowser.open("https://console.cloud.google.com/apis/credentials")),
                   ("Połącz konto / połącz ponownie", self.connect), ("Pobierz kanały", self.channels),
                   ("Zainstaluj lub sprawdź środowisko", self.install), ("Pobierz i sprawdź model", self.model),
                   ("Wykonaj próbę lokalną", self.test), ("Pobierz i sprawdź tytuł oraz opis", self.check_metadata)]
        label, action = actions[self.step]
        self.action_label = label
        self.action_button = ttk.Button(self.body, text=label, command=action)
        self.action_button.pack(anchor="w", pady=4)
        self.cancel_button = None
        if self.step == 0:
            ttk.Button(self.body, text="Wybierz pobrany JSON klienta Desktop", command=self.choose_client).pack(anchor="w", pady=4)
            self.complete = client_path(self.app.root).is_file()
        if self.step == 1:
            self.cancel_button = ttk.Button(self.body, text="Anuluj oczekiwanie na logowanie", command=self.cancel_login)
            self.cancel_button.pack(anchor="w", pady=4)
            try:
                client = client_settings(client_path(self.app.root))
                WindowsVault(self.app.root).read(client["client_id"])
                self.complete = True
                self.message.set("Poświadczenie jest zapisane. Możesz przejść dalej albo ponownie połączyć konto.")
            except (AuthError, OSError):
                self.complete = False
        if self.step == 2:
            self.channel = tk.StringVar(master=self.dialog)
            self.channel_combo = ttk.Combobox(self.body, state="readonly", textvariable=self.channel, width=80)
            self.channel_combo.pack(fill="x", pady=8)
            self.channel_combo.bind("<<ComboboxSelected>>", lambda _: self.channel_selected())
            saved = options(self.app.root)
            if saved["channel_id"]:
                self.show_channels([{"id": saved["channel_id"], "snippet": {"title": saved.get("channel_name", saved["channel_id"])}}])
            ttk.Label(self.body, text="Dzienny budżet aplikacji w jednostkach API (maks. 10000):").pack(anchor="w", pady=(12, 3))
            self.budget = tk.StringVar(master=self.dialog, value=str(saved["budget"]))
            ttk.Spinbox(self.body, from_=1, to=10000, increment=500, textvariable=self.budget, width=12).pack(anchor="w")
        if self.step == 3:
            self.complete = Path(options(self.app.root)["translation_python"]).is_file()
        if self.step == 4:
            try:
                ready(self.app.root)
                self.complete = True
                self.message.set("Model obecny. Następny krok sprawdzi działanie.")
            except RuntimeError:
                self.message.set(read_json(self.app.root / "lokalizacje-model.json", {}).get("message", "Model wymaga pobrania."))
        if self.step == 5:
            trial = read_json(self.app.root / "lokalizacje" / "proba" / "passed.json", {})
            try:
                _path, manifest = ready(self.app.root)
                self.complete = bool(trial.get("passed") and trial.get("model") == manifest)
                if self.complete:
                    self.message.set("Próba aktualnego modelu zaliczona: " + trial.get("at", "") + ". Możesz zakończyć albo powtórzyć próbę.")
            except RuntimeError:
                self.complete = False
        if self.step == 6:
            self.metadata_preview = tk.Text(self.body, height=9, wrap="word")
            self.metadata_preview.pack(fill="both", expand=True, pady=6)
            try:
                video = self.sample_video()
                self.show_metadata(video)
            except ValueError as error:
                self.message.set(str(error))
        self.update_navigation()

    def update_navigation(self):
        final_text = "Zakończ konfigurację" if connection_status(self.app.root).ready else "Zakończ tryb lokalny (bez publikacji)"
        self.next.configure(text=final_text if self.step == len(STEPS) - 1 else "Dalej")
        self.next.state(["!disabled"] if self.complete and not self.busy else ["disabled"])
        self.back.state(["disabled"] if self.busy or self.step == 0 else ["!disabled"])
        self.skip.state(["disabled"] if self.busy or self.step >= 3 else ["!disabled"])
        logging_in = self.busy and self.operation == "connect"
        self.action_button.configure(text="Ponów logowanie" if logging_in else self.action_label)
        self.action_button.state(["disabled"] if self.busy and not logging_in else ["!disabled"])
        if self.cancel_button is not None:
            self.cancel_button.state(["!disabled"] if logging_in else ["disabled"])

    def perform(self, function, callback=None, *, operation=""):
        if self.busy:
            self.message.set("Czynność nadal trwa. Przy logowaniu możesz wybrać Ponów logowanie lub Anuluj oczekiwanie.")
            return
        self.busy = True
        self.operation = operation
        message = "Oczekiwanie na decyzję w przeglądarce. Jeśli Google odrzuci logowanie, popraw ustawienia i kliknij Ponów logowanie tutaj."
        self.message.set(message if operation == "connect" else "Trwa wykonywanie kroku. Poczekaj na potwierdzenie zakończenia.")
        self.update_navigation()
        def run():
            try:
                self.events.put((callback, function(), None))
            except Exception as error:
                self.events.put((None, None, str(error)))
        threading.Thread(target=run, daemon=True).start()

    def poll(self):
        if self.closed:
            return
        try:
            callback, result, error = self.events.get_nowait()
            self.busy = False
            finished = self.operation
            self.operation = ""
            if finished == "connect" and self.retry_login:
                self.retry_login = False
                self.connect()
            else:
                self.complete = error is None
                self.message.set(error or "Krok zakończony. Możesz przejść dalej.")
                if callback and not error:
                    callback(result)
            self.update_navigation()
        except queue.Empty:
            pass
        self.after = self.dialog.after(250, self.poll)

    def choose_client(self):
        path = filedialog.askopenfilename(parent=self.dialog, filetypes=[("Klient OAuth", "*.json")])
        if path:
            self.perform(lambda: import_client(self.app.root, path))

    def connect(self):
        if self.busy and self.operation == "connect":
            self.retry_login = True
            self.login_cancel.set()
            self.message.set("Kończę poprzednie oczekiwanie i otwieram nową próbę logowania. Nie musisz zamykać kreatora.")
            return
        cancel = threading.Event()
        self.login_cancel = cancel
        self.perform(lambda: connect(self.app.root, cancel=cancel), operation="connect")

    def cancel_login(self):
        if self.login_cancel is not None:
            self.retry_login = False
            self.login_cancel.set()
            self.message.set("Anulowanie oczekiwania na logowanie…")

    def channels(self):
        self.perform(lambda: YouTube(LocalizationStore(self.app.root)).channels(), self.show_channels)

    def show_channels(self, channels):
        self.channel_data = channels
        self.channel_combo.configure(values=[c["snippet"]["title"] + " [" + c["id"] + "]" for c in channels])
        self.complete = False
        if len(channels) == 1:
            self.channel_combo.current(0)
            self.channel_selected()
        elif not channels:
            self.message.set("Na tym koncie nie znaleziono kanału. Wróć i połącz właściwe konto.")

    def channel_selected(self):
        index = self.channel_combo.current()
        if 0 <= index < len(self.channel_data):
            save_options(self.app.root, channel_id=self.channel_data[index]["id"], channel_name=self.channel_data[index]["snippet"]["title"])
            self.complete = True
            self.update_navigation()

    def install(self):
        def run():
            interpreter = install_environment()
            path = self.app.root / "lokalizacje-instalacja.log"
            with path.open("ab") as log:
                result = subprocess.run([str(interpreter), "-m", "pip", "install", "-r", str(APP / "requirements-translation.lock.txt")],
                                        stdout=log, stderr=log, creationflags=NO_WINDOW, shell=False)
                if result.returncode:
                    raise RuntimeError("Instalacja nieudana. Otwórz " + str(path))
                subprocess.run([str(interpreter), "-m", "pip", "check"], check=True, stdout=log, stderr=log, creationflags=NO_WINDOW)
            save_options(self.app.root, translation_python=str(interpreter))
        self.perform(run)

    def model(self):
        def run():
            config = options(self.app.root)
            lock = WorkerLock(self.app.root)
            if not lock.acquire():
                raise RuntimeError("Trwają obliczenia. Zatrzymaj je po bieżącym filmie i ponów ten krok.")
            try:
                try:
                    ready(self.app.root)
                    return
                except RuntimeError:
                    pass
                with (self.app.root / "lokalizacje-model.log").open("ab") as log:
                    process = subprocess.Popen([config["translation_python"], "-m", "batch.localization.translation", "--setup", str(self.app.root)],
                                               cwd=APP, stdout=log, stderr=log, creationflags=NO_WINDOW, shell=False)
                    with ChildGuard(process):
                        if process.wait():
                            raise RuntimeError("Model nie jest gotowy. Sprawdź lokalizacje-model.log i ponów krok; pobranie korzysta z cache.")
            finally:
                lock.close()
        self.perform(run)

    def test(self):
        def run():
            lock = WorkerLock(self.app.root)
            if not lock.acquire():
                raise RuntimeError("Trwają obliczenia. Wykonaj próbę po ich zakończeniu.")
            try:
                work = self.app.root / "lokalizacje" / "proba"
                work.mkdir(parents=True, exist_ok=True)
                request = {"root": str(self.app.root), "cache": str(work / "cache"), "result": str(work / "result.json"),
                           "units": [{"key": "pl-en", "text": "Wybieramy częstotliwość próbkowania sygnału.", "lang": "en", "source_language": "pl"},
                                     {"key": "en-pl", "text": "The recording contains music and sound effects.", "lang": "pl", "source_language": "en"}]}
                child(options(self.app.root)["translation_python"], "batch.localization.translation", request, work, lambda _message: None)
                result = read_json(work / "result.json")
                if len(result["units"]) != 2 or not all(i["text"].strip() for i in result["units"]):
                    raise RuntimeError("Próba zwróciła niepełny wynik.")
                atomic_json(work / "passed.json", {"revision": REVISION, "model": result["model"], "device": "cpu", "passed": True,
                                                  "at": datetime.now(timezone.utc).isoformat(), "runtime": result.get("runtime", {})})
            finally:
                lock.close()
        self.perform(run, lambda _: self.message.set("Próba zaliczona. Przejrzyj wynik w lokalizacje/proba. Tłumaczenia filmów nadal wymagają zatwierdzenia."))

    def sample_video(self):
        store = LocalizationStore(self.app.root)
        if not self.selected_id:
            with store.base.connect() as db:
                row = db.execute("SELECT video_id FROM ytl_videos ORDER BY job_id LIMIT 1").fetchone()
            if not row:
                raise ValueError("Najpierw importuj katalog YouTube. Nie ma filmu do sprawdzenia.")
            self.selected_id = row[0]
        return store.get_video(self.selected_id)

    def check_metadata(self):
        def run():
            store = LocalizationStore(self.app.root)
            video = self.sample_video()
            if connection_status(self.app.root).ready:
                store.snapshot(YouTube(store).video(video["video_id"]))
            else:
                refresh_public(store, video["video_id"], lambda _message: None)
            return store.get_video(video["video_id"])
        self.perform(run, self.show_metadata)

    def show_metadata(self, video):
        self.metadata_preview.configure(state="normal")
        self.metadata_preview.delete("1.0", "end")
        self.metadata_preview.insert("1.0", source_text(video))
        self.metadata_preview.configure(state="disabled")
        status = connection_status(self.app.root)
        self.complete = bool(metadata_source(video))
        if status.ready:
            if metadata_source(video).get("source") == "public" or not self.complete:
                self.complete = False
                self.message.set("Konto jest skonfigurowane, ale pokazane dane nie potwierdzają odczytu przez API. Kliknij Pobierz i sprawdź tytuł oraz opis. Próba dotyczy jednego filmu.")
            else:
                self.message.set("Pokazano dane jednego filmu pobrane przez API. Po zakończeniu kreatora użyj Odśwież z YouTube, aby pobrać pozostałe. Bez zaznaczenia odświeżysz cały katalog kanału.")
        else:
            self.message.set("Tryb lokalny. Publikacja nie jest skonfigurowana: " + status.message + ". Publiczny odczyt nie potwierdza dostępu do konta.")

    def previous(self):
        if not self.busy and self.step:
            self.step -= 1
            self.draw()

    def advance(self):
        if self.complete and not self.busy:
            if self.step == 2:
                try:
                    budget = int(self.budget.get())
                    if not 1 <= budget <= 10000:
                        raise ValueError("Budżet musi wynosić od 1 do 10000.")
                    save_options(self.app.root, budget=budget)
                except ValueError as error:
                    self.message.set(str(error))
                    return
            if self.step == len(STEPS) - 1:
                self.close()
            else:
                self.step += 1
                self.draw()

    def skip_account(self):
        if not self.busy:
            self.step = 3
            self.draw()

    def close(self):
        self.closed = True
        if self.login_cancel is not None:
            self.login_cancel.set()
        if hasattr(self, "after"):
            self.dialog.after_cancel(self.after)
        self.dialog.destroy()


def language_dialog(parent, store, ident):
    video = store.get_video(ident)
    current = json.loads(video["language_overrides"])
    dialog = tk.Toplevel(parent)
    dialog.title("Język źródłowych pól i zachowywane nazwy")
    place_dialog(dialog, parent, 650, 390)
    variables = {}
    for key, title in (("title", "Język oryginalnego tytułu"), ("description", "Język opisu, auto rozpoznaje akapity")):
        ttk.Label(dialog, text=title).pack(anchor="w", padx=12, pady=(10, 2))
        var = tk.StringVar(master=dialog, value=current.get(key, "auto"))
        ttk.Combobox(dialog, textvariable=var, values=("auto", "pl", "en"), state="readonly").pack(anchor="w", padx=12)
        variables[key] = var
    ttk.Label(dialog, text="Wspólny słownik zachowywanych nazw, po jednej w wierszu:").pack(anchor="w", padx=12, pady=8)
    text = tk.Text(dialog, height=6)
    text.pack(fill="both", expand=True, padx=12)
    text.insert("1.0", "\n".join(options(store.root)["glossary"]))
    def save():
        store.language_overrides(ident, variables["title"].get(), variables["description"].get())
        save_options(store.root, glossary=[s.strip() for s in text.get("1.0", "end-1c").splitlines() if s.strip()])
        dialog.destroy()
    ttk.Button(dialog, text="Zapisz", command=save).pack(pady=10)
