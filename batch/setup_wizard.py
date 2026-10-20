"""Guided Hugging Face setup with an explicit access check and live progress."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk
import webbrowser

from .common import WorkerLock, read_json
from .diarization import validate_diarization
from .hf_access import MODEL_ID, MODEL_URL, TOKENS_URL, check_access, redact
from .ui_help import Tooltip, help_button, wrap_with_parent


DOWNLOAD_LABEL = 'Pobierz model'


class SetupWizard:
    def __init__(self, app):
        self.app = app
        self.dialog = tk.Toplevel(app.window)
        self.dialog.title("Mówcy: konfiguracja krok po kroku")
        self.dialog.geometry("860x650")
        self.dialog.minsize(820, 620)
        self.dialog.transient(app.window)
        self.dialog.protocol("WM_DELETE_WINDOW", self.close)
        self.closed, self.checking = False, False
        self.events = queue.Queue()
        self.verified = None
        self.started = 0
        self.after_id = None
        self.last_state = None
        self.last_busy = None
        self.opened = time.monotonic()
        self.help_status = tk.StringVar(master=self.dialog, value="Najedź na przycisk lub naciśnij Tab i F1, aby zobaczyć pomoc.")
        outer = ttk.Frame(self.dialog, padding=18)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="Przygotuj rozpoznawanie mówców", font=("Segoe UI", 17, "bold")).pack(anchor="w")
        ttk.Label(outer, text="Jednorazowo pobierz model. Później Twoje nagrania będą przetwarzane lokalnie.",
                  wraplength=800).pack(anchor="w", pady=(5, 12))
        self.tabs = ttk.Notebook(outer)
        self.tabs.pack(fill="both", expand=True)
        self.pages = []
        for title in ("1. Dostęp do modelu", "2. Token i sprawdzenie", "3. Pobranie i próba"):
            frame = ttk.Frame(self.tabs, padding=16)
            self.tabs.add(frame, text=title)
            self.pages.append(frame)
        Tooltip(self.tabs, "Przechodź przez trzy kroki. Możesz wrócić do poprzedniego bez utraty wpisanego tokenu.", self.help_status)
        self.build_access()
        self.build_token()
        self.build_download()
        ttk.Separator(outer).pack(fill="x", pady=(12, 8))
        ttk.Label(outer, textvariable=self.help_status, wraplength=800, foreground="#44546a", justify="left").pack(anchor="w", fill="x")
        footer = ttk.Frame(outer)
        footer.pack(fill="x", pady=(10, 0))
        ttk.Button(footer, text="Wstecz", command=self.previous_step).pack(side="left")
        self.next_step_button = ttk.Button(footer, text="Dalej", command=self.next_step)
        self.next_step_button.pack(side="right", padx=8)
        self.button(footer, "Zamknij", self.close,
                    "Zamyka kreator i usuwa token z jego pola. Rozpoczęte pobieranie lub próba działają dalej w tle.").pack(side="right")
        for parent, padding in ((outer, 36), *((page, 32) for page in self.pages)):
            for widget in parent.winfo_children():
                if isinstance(widget, ttk.Label) and int(widget.cget("wraplength") or 0):
                    wrap_with_parent(widget, padding)
        try:
            validate_diarization(app.config)
            self.local_ready = True
        except Exception:
            self.local_ready = False
        if self.local_ready or WorkerLock.busy(app.root / "konfiguracja"):
            self.tabs.select(2)
        if self.local_ready:
            self.progress_message.set("Model jest już na dysku. Możesz wykonać próbę bez wpisywania tokenu.")
            self.download_button.configure(text="Uruchom krótką próbę", state="normal")
        self.poll()

    def previous_step(self):
        if not self.checking and not WorkerLock.busy(self.app.root / "konfiguracja"):
            self.tabs.select(max(0, self.tabs.index(self.tabs.select()) - 1))

    def next_step(self):
        index = self.tabs.index(self.tabs.select())
        if self.checking or WorkerLock.busy(self.app.root / "konfiguracja"):
            return
        if index == 0:
            self.tabs.select(1)
        elif index == 1:
            if self.verified == self.fingerprint():
                self.tabs.select(2)
            else:
                self.check()
        elif self.local_ready:
            self.close()
        else:
            self.download()

    def button(self, parent, text, action, hint, **kwargs):
        return help_button(parent, text, action, hint, self.help_status, **kwargs)

    def label(self, parent, text, bold=False, **kwargs):
        label = ttk.Label(parent, text=text, wraplength=760, justify="left",
                          **({"font": ("Segoe UI", 11, "bold")} if bold else {}), **kwargs)
        label.pack(anchor="w", fill="x", pady=(0, 10))
        return label

    def open_url(self, url):
        try:
            opened = webbrowser.open(url)
            self.help_status.set("Otwarto stronę w przeglądarce. Po zakończeniu wróć do tego okna." if opened else
                                 "Nie udało się otworzyć przeglądarki. Skopiuj adres i wklej w jej pasek adresu.")
        except OSError:
            self.help_status.set("Nie udało się otworzyć przeglądarki. Skopiuj adres i wklej w jej pasek adresu.")

    def copy_url(self):
        self.dialog.clipboard_clear()
        self.dialog.clipboard_append(MODEL_URL)
        self.help_status.set("Skopiowano bezpośredni adres modelu. Wklej go w pasek adresu przeglądarki.")

    def build_access(self):
        page = self.pages[0]
        self.label(page, "Otwórz konkretną stronę modelu, bez szukania w katalogu", True)
        self.label(page, "Właściciel: pyannote    •    Model: speaker-diarization-community-1")
        self.label(page, "To model opublikowany przez pyannote. Nie musisz dodawać go do swoich modeli na Hugging Face.")
        address = ttk.Entry(page)
        address.insert(0, MODEL_URL)
        address.configure(state="readonly")
        address.pack(fill="x", pady=(0, 10))
        Tooltip(address, "Pełny adres właściwego modelu. Możesz zaznaczyć go i skopiować skrótem Ctrl+C.", self.help_status)
        actions = ttk.Frame(page)
        actions.pack(fill="x", pady=(0, 14))
        self.button(actions, "Otwórz stronę Community-1", lambda: self.open_url(MODEL_URL),
                    "Otwiera dokładnie model pyannote/speaker-diarization-community-1 w domyślnej przeglądarce.").pack(side="left", padx=(0, 8))
        self.button(actions, "Kopiuj adres", self.copy_url,
                    "Kopiuje adres modelu, jeżeli wolisz użyć już otwartej przeglądarki.").pack(side="left")
        self.label(page, "Na stronie Hugging Face:")
        self.label(page, "1. Zaloguj się przyciskiem Log in, jeśli strona o to prosi.\n"
                        "2. Przy modelu wypełnij formularz dostępu i zaakceptuj jego warunki.\n"
                        "3. Po uzyskaniu dostępu wróć tutaj i przejdź do tokenu.")
        self.label(page, "Warunki Community-1 obejmują udostępnienie danych kontaktowych autorom modelu. "
                        "To decyzja na stronie Hugging Face; aplikacja nie zaznacza tych zgód za Ciebie.", foreground="#44546a")
        self.button(page, "Mam dostęp, przejdź do tokenu →", lambda: self.tabs.select(1),
                    "Przechodzi do kroku 2. Rzeczywiste uprawnienia zostaną sprawdzone po wklejeniu tokenu.").pack(anchor="e", pady=(4, 0))

    def build_token(self):
        page = self.pages[1]
        self.label(page, "Typ tokenu: Read (odczyt) • zalecany dla tej aplikacji", True)
        self.label(page, "W Access Tokens kliknij Create new token. W sekcji Token type zaznacz Read. "
                        "Nadaj nazwę, np. Kolejka transkrypcji, utwórz token i skopiuj jego wartość hf_…")
        self.button(page, "Otwórz Access Tokens", lambda: self.open_url(TOKENS_URL),
                    "Otwiera ustawienia tokenów. Wystarczy Read. Nie są potrzebne uprawnienia Write ani płatny abonament.").pack(anchor="w", pady=(0, 10))
        self.label(page, "Read wystarcza do pobrania modelu. Write służy także do zapisu i nie jest potrzebny. "
                        "Fine-grained to opcjonalny wariant z ręcznym doborem uprawnień; tutaj wybierz Read. "
                        "Użyj tego samego konta, na którym masz dostęp do Community-1.", foreground="#44546a")
        ttk.Label(page, text="Token Hugging Face (zaczyna się od hf_)").pack(anchor="w", pady=(2, 4))
        row = ttk.Frame(page)
        row.pack(fill="x")
        self.token_var = tk.StringVar(master=self.dialog)
        self.token_entry = ttk.Entry(row, textvariable=self.token_var, show="*", width=48)
        self.token_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        Tooltip(self.token_entry, "Wklej wartość tokenu, nie jego nazwę. Token pozostaje tylko w pamięci do końca konfiguracji.", self.help_status)
        self.button(row, "Wklej", self.paste_token, "Wkleja token ze schowka do ukrytego pola. Nie zapisuje go na dysku.").pack(side="left", padx=(0, 8))
        self.show_token = tk.BooleanVar(master=self.dialog, value=False)
        show = ttk.Checkbutton(row, text="Pokaż", variable=self.show_token,
                               command=lambda: self.token_entry.configure(show="" if self.show_token.get() else "*"))
        show.pack(side="left")
        Tooltip(show, "Pokazuje lub ukrywa wpisany token, aby można było sprawdzić, czy został wklejony w całości.", self.help_status)
        self.token_var.trace_add("write", self.token_changed)
        self.access_message = tk.StringVar(master=self.dialog, value="Po wklejeniu tokenu kliknij Sprawdź dostęp.")
        ttk.Label(page, textvariable=self.access_message, wraplength=760, justify="left").pack(anchor="w", fill="x", pady=12)
        self.check_button = self.button(page, "Sprawdź dostęp", self.check,
                    "Sprawdza ważność tokenu i dostęp do Community-1. To krótki test połączenia, bez pobierania całego modelu.")
        self.check_button.pack(anchor="e")

    def build_download(self):
        page = self.pages[2]
        self.label(page, "Pobierz model i sprawdź krótkie nagranie", True)
        self.label(page, "Po sprawdzeniu dostępu aplikacja pobierze model. Następnie wybierz własne nagranie "
                        "do próby aktualnego pipeline'u. Przetworzymy do 60 sekund; długiego pliku nie trzeba ciąć. Możesz zamknąć to okno i wrócić później.")
        self.label(page, f"Model: {MODEL_ID}\nMiejsce zapisu: {self.app.config['diar_model']}", foreground="#44546a")
        self.progress_message = tk.StringVar(master=self.dialog, value="Najpierw sprawdź dostęp w kroku 2.")
        ttk.Label(page, textvariable=self.progress_message, wraplength=760, justify="left").pack(anchor="w", fill="x", pady=12)
        self.progress = ttk.Progressbar(page, mode="indeterminate", maximum=100)
        self.progress.pack(fill="x", pady=(0, 14))
        Tooltip(self.progress, "Pokazuje postęp pobierania lub pracy próbnej. Gdy nie da się określić czasu, pasek sygnalizuje trwającą pracę.", self.help_status)
        self.download_button = self.button(page, DOWNLOAD_LABEL, self.download,
                    "Pobiera Community-1. Następnie wybierz własne nagranie do krótkiej próby. Wymaga poprawnego dostępu z kroku 2.", state="disabled")
        self.download_button.pack(anchor="w", pady=(0, 10))
        self.results_button = self.button(page, "Otwórz wyniki próby", self.open_test_results,
                    "Otwiera folder z transkrypcją próby, aby można było sprawdzić tekst i etykiety mówców.", state="disabled")
        self.results_button.pack(anchor="w")

    def paste_token(self):
        try:
            self.token_var.set(self.dialog.clipboard_get().strip())
        except tk.TclError:
            self.access_message.set("Schowek nie zawiera tekstu. Skopiuj token z Hugging Face i spróbuj ponownie.")

    def fingerprint(self):
        return hashlib.sha256(self.token_var.get().strip().encode()).hexdigest()

    def token_changed(self, *_):
        self.verified = None
        if hasattr(self, "download_button") and not getattr(self, "local_ready", False):
            self.download_button.configure(state="disabled")
        if hasattr(self, "access_message") and not self.checking:
            self.access_message.set("Kliknij Sprawdź dostęp, aby zweryfikować ten token.")

    def check(self):
        if self.checking:
            return
        token = self.token_var.get().strip()
        fingerprint = self.fingerprint()
        self.checking = True
        self.verified = None
        if not self.local_ready:
            self.download_button.configure(state="disabled")
        self.check_button.configure(state="disabled")
        self.access_message.set("Sprawdzam token i zgodę na dostęp do Community-1…")
        def check_background():
            try:
                result = check_access(token, self.app.config["diar_revision"])
            except Exception:
                result = {"ok": False, "message": "Nie udało się sprawdzić dostępu. Spróbuj ponownie."}
            self.events.put((fingerprint, result))
        threading.Thread(target=check_background, daemon=True).start()

    def download(self):
        if self.local_ready:
            self.app.test_local()
            return
        if self.verified != self.fingerprint():
            self.tabs.select(1)
            self.access_message.set("Najpierw sprawdź dostęp dla wpisanego tokenu.")
            return
        request = {"token": self.token_var.get().strip()}
        if not self.app.setup_process(request):
            return
        self.started = time.monotonic()
        self.last_state = None
        self.token_var.set("")
        self.download_button.configure(state="disabled")
        self.progress_message.set("Uruchamianie konfiguracji. Możesz pozostawić to okno otwarte.")
        self.progress.start(12)

    def consume_access(self):
        while not self.events.empty():
            fingerprint, result = self.events.get_nowait()
            self.checking = False
            self.check_button.configure(state="normal")
            if fingerprint != self.fingerprint():
                self.access_message.set("Token zmienił się podczas sprawdzania. Kliknij Sprawdź dostęp ponownie.")
                continue
            self.access_message.set(result["message"])
            if result["ok"]:
                self.verified = fingerprint
                self.download_button.configure(state="normal")
                account = result.get("account", "")
                self.progress_message.set((f"Konto: {account}. " if account else "") + result["message"])
                self.tabs.select(2)

    def show_running(self, percent):
        self.download_button.configure(state="disabled")
        if percent is None:
            self.progress.configure(mode="indeterminate")
            self.progress.start(12)
        else:
            self.progress.stop()
            self.progress.configure(mode="determinate", value=percent)

    def show_ready(self, state):
        self.local_ready = True
        self.progress.configure(mode="determinate", value=100)
        self.download_button.configure(text="Uruchom krótką próbę", state="normal")
        results_path = state.get("test_results")
        has_results = bool(results_path and Path(results_path).is_dir())
        self.results_button.configure(state="normal" if has_results else "disabled")

    def show_interrupted(self, state):
        try:
            validate_diarization(self.app.config)
            self.local_ready = True
        except Exception:
            self.local_ready = False
        self.download_button.configure(text="Ponów krótką próbę" if self.local_ready else DOWNLOAD_LABEL,
                                       state="normal" if self.local_ready else "disabled")
        if state.get("state") == "running":
            self.progress_message.set("Poprzednie pobieranie lub próba zostały przerwane. Wróć do kroku 2 albo uruchom próbę, jeśli model jest już pobrany.")
        elif not self.local_ready:
            self.progress_message.set(redact(state.get("message", "")) + "\nWróć do kroku 2, aby sprawdzić dostęp ponownie.")

    def show_state(self, state, busy):
        self.last_state, self.last_busy = state, busy
        if state.get("phase") in {"whisper_complete", "download_whisper"}:
            self.progress_message.set("To stan modelu Whisper. Community-1 wymaga osobnego sprawdzenia dostępu i pobrania.")
            return
        self.progress_message.set(redact(state.get("message", "")))
        phase = state.get("state")
        if phase == "running" and busy:
            self.show_running(state.get("percent"))
            return
        self.progress.stop()
        if phase == "ready":
            self.show_ready(state)
        elif phase in {"error", "running"}:
            self.show_interrupted(state)

    def poll(self):
        if self.closed:
            return
        self.consume_access()
        state = read_json(self.app.root / "konfiguracja-modelu.json", {})
        busy = WorkerLock.busy(self.app.root / "konfiguracja")
        state_changed = state != self.last_state or busy != self.last_busy
        # A new detached process needs a moment to acquire its lock. Once that
        # lock has been seen, its disappearance means an interrupted operation.
        launching = (state.get("state") == "running" and not busy and self.last_busy is not True
                     and time.monotonic() - (self.started or self.opened) < 4)
        if state and state_changed and not launching:
            self.show_state(state, busy)
        index = self.tabs.index(self.tabs.select())
        label = ("Dalej", "Sprawdź dostęp i przejdź dalej", "Zakończ" if self.local_ready else "Pobierz model")[index]
        self.next_step_button.configure(text="Poczekaj…" if busy or self.checking else label,
                                        state="disabled" if busy or self.checking else "normal")
        self.after_id = self.dialog.after(400, self.poll)

    def open_test_results(self):
        state = read_json(self.app.root / "konfiguracja-modelu.json", {})
        path = state.get("test_results")
        if path and Path(path).is_dir():
            os.startfile(path)

    def close(self):
        self.closed = True
        self.progress.stop()
        self.token_var.set("")
        self.verified = None
        if self.after_id:
            self.dialog.after_cancel(self.after_id)
        self.dialog.destroy()
