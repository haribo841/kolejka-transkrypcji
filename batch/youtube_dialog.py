"""User-directed YouTube login, session choice and a single isolated probe."""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time
import tkinter as tk
from tkinter import filedialog, ttk
import webbrowser

from .common import APP, NO_WINDOW, WorkerLock, application_python
from .importers import video_id
from .processes import ChildGuard
from .ui_help import Tooltip, help_button, wrap_with_parent
from .youtube_access import DEFAULTS, SessionError, preferences, save_preferences, validate_preferences
from .browser_profiles import discover_profiles, find_browser, open_profile
from .managed_browser import managed_profiles, open_managed

COOKIE_GUIDE = "https://github.com/yt-dlp/yt-dlp/wiki/Extractors#exporting-youtube-cookies"
BROWSER_NAMES = {"Firefox": "firefox", "Chrome": "chrome", "Edge": "edge"}
ANONYMOUS_CHOICE = "Anonimowo, bez logowania"
FILE_CHOICE = "Plik cookies.txt z ustawień zaawansowanych"


def probe_process(root, request, cancelled):
    """No cookie values in argv, stdout logs, settings or exception messages."""
    lock = WorkerLock(root)
    try:
        if not lock.acquire():
            return {"ok": False, "message": "Kolejka lub inny test już pracuje. Najpierw dokończ bieżący film i zatrzymaj kolejkę."}
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        command = [application_python(), "-m", "batch.youtube_probe"]
        process = subprocess.Popen(command, cwd=APP, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
                                   env=env, creationflags=NO_WINDOW)
        with ChildGuard(process):
            try:
                process.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
                process.stdin.close()
                deadline = time.monotonic() + float(request["access"]["delay_seconds"]) + 90
                while process.poll() is None:
                    if cancelled.wait(0.2):
                        return {"ok": False, "message": "Test został anulowany. Kolejka pozostaje zatrzymana."}
                    if time.monotonic() >= deadline:
                        return {"ok": False, "message": "Test przekroczył czas oczekiwania. Sprawdź połączenie, sesję i spróbuj później."}
                result = json.loads(process.stdout.read(24000))
                if not isinstance(result.get("ok"), bool) or not isinstance(result.get("message"), str):
                    raise ValueError()
                return result
            finally:
                process.stdin.close()
                process.stdout.close()
    except Exception:
        return {"ok": False, "message": "Nie udało się wykonać testu. Sprawdź lokalne ustawienia i spróbuj ponownie."}
    finally:
        lock.close()


class YouTubeDialog:
    def __init__(self, app):
        self.app, self.closed, self.checking = app, False, False
        self.events, self.cancelled = queue.Queue(), threading.Event()
        self.thread = None
        self.after_id = None
        try:
            saved, initial_message = preferences(app.root), ""
        except SessionError as exc:
            saved, initial_message = dict(DEFAULTS), str(exc)
        self.dialog = tk.Toplevel(app.window)
        self.dialog.title("Dostęp YouTube")
        self.dialog.geometry("960x820")
        self.dialog.minsize(860, 710)
        self.dialog.transient(app.window)
        self.dialog.protocol("WM_DELETE_WINDOW", self.close)
        self.help_status = tk.StringVar(master=self.dialog, value="Zaloguj się samodzielnie w przeglądarce. Program nie prosi o hasło Google.")
        self.mode = tk.StringVar(value=saved["mode"])
        self.browser = tk.StringVar(value=next(name for name, code in BROWSER_NAMES.items() if code == saved["browser"]))
        self.profile = tk.StringVar(value=saved["profile"])
        self.cookie_file = tk.StringVar(value=saved["cookie_file"])
        self.delay = tk.StringVar(value=str(int(saved["delay_seconds"])))
        self.request_delay = saved["request_delay_seconds"]
        self.url = tk.StringVar(value=self.initial_url())
        self.result = tk.StringVar(value=initial_message or "Test wykonuje jedno sprawdzenie wybranego filmu. Nie pobiera audio i nie zmienia postępu kolejki.")
        self.connection_choice = tk.StringVar(master=self.dialog)
        self.connections, self.tested_request = {}, None

        frame = ttk.Frame(self.dialog, padding=16)
        frame.pack(fill="both", expand=True)
        self.label(frame, "Połącz z YouTube", bold=True)
        self.label(frame, "Profile wykrywamy automatycznie. Otwórz film w wybranym profilu, zaloguj się lub potwierdź dostęp, a następnie kliknij Połącz i sprawdź dostęp.")
        self.tabs = ttk.Notebook(frame)
        self.tabs.pack(fill="both", expand=True, pady=10)
        pages = [ttk.Frame(self.tabs, padding=12) for _ in range(2)]
        for page, title in zip(pages, ("Połączenie", "Ustawienia zaawansowane")):
            self.tabs.add(page, text=title)
        self.build_login(pages[0])
        self.build_session(pages[1])
        self.build_test(pages[0])
        footer = ttk.Frame(frame)
        footer.pack(fill="x")
        self.save_button = self.button(footer, "Zapisz ustawienia", self.save,
            "Zapisuje wybrane źródło sesji i przerwy. Nie odczytuje cookies i nie uruchamia pobierania. Start użyje tych ustawień.")
        self.save_button.pack(side="left")
        self.use_button = self.button(footer, "Gotowe - użyj i zamknij", self.use_connection,
            "Po udanym teście zatwierdza tę samą sesję i wraca do kolejki. Start pozostaje ręczny.")
        self.use_button.pack(side="left", padx=10)
        self.use_button.configure(state="disabled")
        self.button(footer, "Zamknij", self.close,
            "Zamyka konfigurację i anuluje ewentualny test. Zapisane ustawienia pozostają. Nie uruchamia kolejki.").pack(side="right")
        help_frame = ttk.LabelFrame(frame, text="Podpowiedź", padding=8, height=78)
        help_frame.pack(fill="x", pady=(8, 0))
        help_frame.pack_propagate(False)
        help_label = ttk.Label(help_frame, textvariable=self.help_status, wraplength=800, justify="left")
        help_label.pack(fill="both", expand=True)
        wrap_with_parent(help_label, 20)
        self.poll()

    def label(self, parent, text, bold=False):
        label = ttk.Label(parent, text=text, justify="left", wraplength=790,
                          font=("Segoe UI", 11, "bold") if bold else ("Segoe UI", 10))
        label.pack(fill="x", pady=(0, 10))
        wrap_with_parent(label, 34)
        return label

    def button(self, parent, text, action, help_text):
        return help_button(parent, text, action, help_text, self.help_status)

    def initial_url(self):
        jobs = list(self.app.jobs.values())
        blocked = [j for j in jobs if j["status"] == "blocked" and j["kind"] == "youtube"]
        selected = [self.app.jobs[i] for i in self.app.selected() if i in self.app.jobs and self.app.jobs[i]["kind"] == "youtube"]
        candidates = blocked or selected or [j for j in jobs if j["kind"] == "youtube" and video_id(j["source"])]
        return candidates[0]["source"] if candidates else ""

    def build_login(self, page):
        self.label(page, "1. Film i źródło dostępu", bold=True)
        entry = ttk.Entry(page, textvariable=self.url)
        entry.pack(fill="x", pady=(0, 10))
        Tooltip(entry, "Link do jednego filmu YouTube. Domyślnie wybierany jest zablokowany film. Możesz wkleić inny link.", self.help_status)
        source = ttk.Frame(page)
        source.pack(fill="x", pady=(0, 10))
        ttk.Label(source, text="Profil / sposób dostępu:").pack(side="left", padx=(0, 8))
        self.connection_selector = ttk.Combobox(source, textvariable=self.connection_choice, state="readonly", width=62)
        self.connection_selector.pack(side="left", fill="x", expand=True)
        self.connection_selector.bind("<<ComboboxSelected>>", self.choose_connection)
        self.button(source, "Wykryj ponownie", self.detect_connections,
                    "Odczytuje nazwy profili i lokalizacje przeglądarek. Nie odczytuje cookies, nie testuje i nie uruchamia kolejki.").pack(side="left", padx=(8, 0))
        self.detect_connections()
        row = ttk.Frame(page)
        row.pack(fill="x", pady=(0, 12))
        self.login_button = self.button(row, "2. Zaloguj / potwierdź w wybranym profilu", self.open_video,
            "Otwiera film w dokładnie tym profilu, którego sesji użyje test. W razie potrzeby wykonaj logowanie lub CAPTCHA w przeglądarce.")
        self.login_button.pack(side="left", padx=(0, 8))
        self.button(row, "Kopiuj link", self.copy_link,
            "Kopiuje wyłącznie link filmu. Możesz wkleić go w wybranej przeglądarce lub profilu.").pack(side="left")
        self.label(page, "Własne okno programu: zaloguj się raz i pozostaw przeglądarkę otwartą lub zminimalizowaną. Sesja jest odczytywana automatycznie, bez eksportu cookies.txt. Przy zwykłym profilu Chrome/Edge odczyt może być blokowany.")

    def build_session(self, page):
        self.label(page, "Jak program ma uzyskać dostęp?", bold=True)
        for mode, text, help_text in (
            ("anonymous", "Anonimowo, bez sesji konta", "Domyślne ustawienie. Program nie odczytuje cookies ani profilu przeglądarki."),
            ("managed", "Własne okno Chrome / Edge (bez eksportu cookies)", "Zaloguj się w osobnym oknie programu. Sesja pozostaje lokalnie w tym profilu przeglądarki. Test i Start odczytują aktualne cookies YouTube."),
            ("browser", "Sesja z przeglądarki (Firefox najprostszy w Windows)", "Odczyt wybranego profilu nastąpi po naciśnięciu testu lub Start. Tryb prywatny nie jest odczytywany. Chrome i Edge mogą blokować odszyfrowanie cookies."),
            ("file", "Plik cookies.txt wyeksportowany dla YouTube", "Plik w formacie Netscape. Program odczytuje cookies YouTube i nie nadpisuje tego pliku. To nie jest token Hugging Face.")):
            option = ttk.Radiobutton(page, text=text, variable=self.mode, value=mode, command=self.update_fields)
            option.pack(anchor="w", pady=2)
            Tooltip(option, help_text, self.help_status)
        fields = ttk.Frame(page)
        fields.pack(fill="x", pady=8)
        fields.columnconfigure(1, weight=1)
        ttk.Label(fields, text="Przeglądarka:").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=3)
        self.browser_choice = ttk.Combobox(fields, textvariable=self.browser, values=list(BROWSER_NAMES), width=18, state="readonly")
        self.browser_choice.grid(row=0, column=1, sticky="w", pady=3)
        Tooltip(self.browser_choice, "Wybierz przeglądarkę, w której sprawdzono film. Gdy odczyt Chrome/Edge nie działa, użyj Firefoksa lub eksportu cookies.", self.help_status)
        ttk.Label(fields, text="Folder profilu (opcjonalnie):").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=3)
        self.profile_entry = ttk.Entry(fields, textvariable=self.profile)
        self.profile_entry.grid(row=1, column=1, sticky="ew", pady=3)
        Tooltip(self.profile_entry, "Puste: yt-dlp wybierze profil automatycznie. Przy wielu profilach wskaż folder tego, w którym zalogowano się do YouTube. Firefox pokazuje go na about:profiles; Chrome/Edge na stronie version.", self.help_status)
        self.profile_button = self.button(fields, "Wybierz folder", self.pick_profile, "Wskazuje folder konkretnego profilu przeglądarki. Nie odczytuje jego zawartości.")
        self.profile_button.grid(row=1, column=2, padx=(8, 0))
        ttk.Label(fields, text="Plik cookies:").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=3)
        self.file_entry = ttk.Entry(fields, textvariable=self.cookie_file)
        self.file_entry.grid(row=2, column=1, sticky="ew", pady=3)
        Tooltip(self.file_entry, "Wyeksportuj wyłącznie youtube.com do cookies.txt, zgodnie z instrukcją yt-dlp. Nie wysyłaj pliku innym osobom ani na czat.", self.help_status)
        self.file_button = self.button(fields, "Wybierz plik", self.pick_file, "Wybiera istniejący eksport cookies.txt. Sam wybór nie odczytuje sesji i nie uruchamia testu.")
        self.file_button.grid(row=2, column=2, padx=(8, 0))
        self.label(page, "Cookies dają dostęp do sesji konta. Nie przesyłaj ich na czat. Program nie zapisuje ich w raportach; zachowuje jedynie wybór przeglądarki lub ścieżkę pliku. Nie zmienia zabezpieczeń Chrome/Edge.")
        row = ttk.Frame(page)
        row.pack(fill="x")
        self.button(row, "Instrukcja eksportu cookies (yt-dlp)", lambda: webbrowser.open(COOKIE_GUIDE),
            "Otwiera instrukcję yt-dlp: osobna sesja prywatna, eksport youtube.com i zamknięcie tego okna. Sesji prywatnej nie odczytuj opcją Przeglądarka.").pack(side="left")
        self.button(row, "Wróć do połączenia", lambda: self.tabs.select(0), "Wraca do testu dostępu z tymi ustawieniami.").pack(side="right")
        self.update_fields()

    def build_test(self, page):
        self.label(page, "3. Połącz i sprawdź jeden film", bold=True)
        row = ttk.Frame(page)
        row.pack(fill="x", pady=(0, 12))
        ttk.Label(row, text="Przerwa przed każdym filmem (sekundy):").pack(side="left")
        delay = ttk.Spinbox(row, from_=5, to=300, textvariable=self.delay, width=6)
        delay.pack(side="left", padx=8)
        Tooltip(delay, "Domyślnie 15 sekund przed próbą pobrania filmu i 1 sekunda między żądaniami metadanych. Test też czeka. Przerwy zmniejszają tempo żądań, lecz nie gwarantują braku blokad.", self.help_status)
        self.test_button = self.button(page, "Połącz i sprawdź dostęp", self.test,
            "Zapisuje ustawienia, odczytuje wybraną sesję i sprawdza formaty jednego filmu. Nie pobiera audio, nie ponawia automatycznie i nie uruchamia kolejki.")
        self.test_button.pack(anchor="w", pady=(0, 10))
        self.firefox_button = self.button(page, "Użyj wykrytego Firefoksa", self.use_firefox,
            "Wybiera istniejący profil Firefoksa i otwiera w nim film. Potwierdź logowanie, następnie użyj testu.")
        self.firefox_button.pack(anchor="w", pady=(0, 6))
        self.firefox_button.configure(state="disabled")
        self.progress = ttk.Progressbar(page, mode="indeterminate")
        self.progress.pack(fill="x", pady=(0, 10))
        result_label = ttk.Label(page, textvariable=self.result, justify="left", wraplength=790)
        result_label.pack(fill="x", pady=(0, 12))
        wrap_with_parent(result_label, 34)
        self.label(page, "Po sukcesie kliknij Gotowe - użyj i zamknij, potem ręcznie Start/Wznów. Logowanie nie gwarantuje usunięcia ograniczenia konta lub adresu IP.")

    def detect_connections(self):
        profiles = discover_profiles()
        owned = managed_profiles(self.app.root)
        records = [{"mode": "anonymous", "label": ANONYMOUS_CHOICE}, {"mode": "file", "label": FILE_CHOICE}, *owned, *profiles]
        self.connections = {record["label"]: record for record in records}
        self.connection_selector.configure(values=list(self.connections))
        selected = next((record["label"] for record in [*owned, *profiles] if record["mode"] == self.mode.get() and
                         record["browser"] == BROWSER_NAMES.get(self.browser.get()) and
                         (not self.profile.get() or record["profile"] == self.profile.get())), None)
        self.connection_choice.set(selected or (FILE_CHOICE if self.mode.get() == "file" else ANONYMOUS_CHOICE))
        if selected:
            self.choose_connection()

    def choose_connection(self, event=None):
        record = self.connections[self.connection_choice.get()]
        self.mode.set(record["mode"])
        if record["mode"] in {"browser", "managed"}:
            self.browser.set(next(name for name, code in BROWSER_NAMES.items() if code == record["browser"]))
            self.profile.set(record["profile"])
        if hasattr(self, "browser_choice"):
            self.update_fields()
        if hasattr(self, "use_button"):
            self.use_button.configure(state="disabled")
        self.tested_request = None

    def use_connection(self):
        if self.tested_request is None:
            return
        tested = self.tested_request
        try:
            changed = tested["access"] != self.values() or video_id(tested["url"]) != video_id(self.url.get())
        except ValueError:
            changed = True
        if changed:
            self.result.set("Zmieniono ustawienia. Sprawdź ponownie dostęp przed zatwierdzeniem.")
            self.use_button.configure(state="disabled")
            return
        self.app.request_readiness(force=True)
        self.close()

    def use_firefox(self):
        choice = next((item for item in self.connections.values() if item.get("browser") == "firefox"), None)
        if choice:
            self.connection_choice.set(choice["label"])
            self.choose_connection()
            self.open_video()

    def login_profile(self):
        if self.mode.get() in {"browser", "managed"}:
            browser, path = BROWSER_NAMES[self.browser.get()], self.profile.get()
            match = next((item for item in self.connections.values() if item["mode"] == self.mode.get() and item.get("browser") == browser and
                          (not path or item["profile"] == path)), None)
            if match:
                return match
            if path:
                return {"mode": "browser", "browser": browser, "profile": path, "executable": find_browser(browser)}
        return next((item for item in self.connections.values() if item["mode"] in {"managed", "browser"}), None)

    def update_fields(self):
        if self.mode.get() == "managed":
            choices = [item for item in self.connections.values() if item["mode"] == "managed"]
            choice = next((item for item in choices if item["browser"] == BROWSER_NAMES.get(self.browser.get())), None)
            if choice is None and choices:
                choice = choices[0]
            if choice:
                self.browser.set(next(name for name, code in BROWSER_NAMES.items() if code == choice["browser"]))
                self.profile.set(choice["profile"])
                self.connection_choice.set(choice["label"])
        browser, file = self.mode.get() == "browser", self.mode.get() == "file"
        self.browser_choice.configure(state="readonly" if browser else "disabled")
        for widget in (self.profile_entry, self.profile_button):
            widget.configure(state="normal" if browser else "disabled")
        for widget in (self.file_entry, self.file_button):
            widget.configure(state="normal" if file else "disabled")

    def pick_profile(self):
        path = filedialog.askdirectory(title="Folder profilu przeglądarki", parent=self.dialog)
        if path:
            self.profile.set(path)

    def pick_file(self):
        path = filedialog.askopenfilename(title="Cookies YouTube (format Netscape)",
            filetypes=[("Cookies TXT", "*.txt"), ("Wszystkie", "*.*")], parent=self.dialog)
        if path:
            self.cookie_file.set(path)
            self.mode.set("file")
            self.connection_choice.set(FILE_CHOICE)
            self.update_fields()

    def canonical_url(self):
        identifier = video_id(self.url.get().strip())
        if not identifier:
            self.result.set("Wklej link do jednego filmu YouTube w kroku 1.")
            self.tabs.select(0)
            return None
        return "https://www.youtube.com/watch?v=" + identifier

    def open_video(self):
        url = self.canonical_url()
        if not url or self.checking:
            return
        record = self.login_profile()
        if record is None:
            self.result.set("Nie wykryto profilu przeglądarki. Otwórz film i zaloguj się, potem kliknij Wykryj ponownie lub wskaż profil w zaawansowanych.")
            webbrowser.open(url)
            return
        if record.get("label"):
            self.connection_choice.set(record["label"])
            self.choose_connection()
        try:
            opener = open_managed if record["mode"] == "managed" else open_profile
            opener(record, url)
            self.result.set("Otwarto wybrany profil. Zaloguj się lub potwierdź odtwarzanie, następnie wróć i kliknij Połącz i sprawdź dostęp.")
        except (ValueError, OSError) as exc:
            self.result.set(str(exc))

    def copy_link(self):
        url = self.canonical_url()
        if url:
            self.dialog.clipboard_clear()
            self.dialog.clipboard_append(url)
            self.help_status.set("Link skopiowany. Wklej go w przeglądarce, której sesję wybierzesz w kroku 2.")

    def values(self):
        return validate_preferences({"mode": self.mode.get(), "browser": BROWSER_NAMES.get(self.browser.get(), ""),
            "profile": self.profile.get(), "cookie_file": self.cookie_file.get(),
            "delay_seconds": self.delay.get(), "request_delay_seconds": self.request_delay})

    def save(self):
        if self.checking:
            return None
        lock = WorkerLock(self.app.root)
        if not lock.acquire():
            self.help_status.set("Kolejka lub inny test pracuje. Najpierw dokończ bieżący film i zatrzymaj kolejkę.")
            return None
        try:
            values = save_preferences(self.app.root, self.values())
            self.help_status.set("Ustawienia zapisane. Cookies nie zostały jeszcze odczytane. Użyj testu albo Start/Wznów.")
            return values
        except (ValueError, OSError):
            self.help_status.set("Nie zapisano ustawień. Sprawdź wybór sesji, ścieżkę pliku i przerwę 5-300 sekund.")
            return None
        finally:
            lock.close()

    def test(self):
        if self.checking:
            return
        url = self.canonical_url()
        if not url:
            return
        values = self.save()
        if values is None:
            return
        self.checking = True
        self.cancelled.clear()
        self.test_button.configure(state="disabled")
        self.save_button.configure(state="disabled")
        self.use_button.configure(state="disabled")
        self.tested_request = None
        self.progress.start(15)
        self.result.set(f"Sprawdzanie jednego filmu, z przerwą {values['delay_seconds']:g} s, a następnie do 90 s na odpowiedź. Kolejka pozostaje zatrzymana.")
        request = {"url": url, "config": dict(self.app.config), "access": values}
        def run():
            self.events.put((request, probe_process(self.app.root, request, self.cancelled)))
        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    @property
    def running(self):
        return bool((self.thread and self.thread.is_alive()) or (self.checking and not self.closed))

    def poll(self):
        if self.closed:
            return
        while not self.events.empty():
            tested, result = self.events.get_nowait()
            self.show_probe_result(tested, result)
        self.after_id = self.dialog.after(250, self.poll)

    def show_probe_result(self, tested, result):
        self.checking = False
        self.progress.stop()
        self.test_button.configure(state="normal")
        self.save_button.configure(state="normal")
        try:
            changed = tested["access"] != self.values() or video_id(tested["url"]) != video_id(self.url.get())
        except ValueError:
            changed = True
        suffix = "\nZmieniono pola w trakcie testu. Wynik dotyczy wcześniejszego linku i zapisanych ustawień; nowy wybór trzeba zapisać i sprawdzić." if changed else ""
        self.result.set(result["message"] + suffix)
        self.tested_request = tested if result["ok"] and not changed else None
        self.use_button.configure(state="normal" if self.tested_request else "disabled")
        has_firefox = any(item.get("browser") == "firefox" for item in self.connections.values())
        self.firefox_button.configure(state="normal" if not result["ok"] and has_firefox else "disabled")
        if self.tested_request:
            from .common import atomic_json
            from .store import now
            from .youtube_access import access_fingerprint
            atomic_json(self.app.root / "youtube-proba.json", {"ok": True, "at": now(),
                        "access_signature": access_fingerprint(tested["access"]), "video_id": video_id(tested["url"])})

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.cancelled.set()
        self.progress.stop()
        if self.after_id:
            self.dialog.after_cancel(self.after_id)
        self.dialog.destroy()
