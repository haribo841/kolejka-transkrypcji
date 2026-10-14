"""Mouse and keyboard help shared by the queue and its setup wizard."""
from __future__ import annotations

import tkinter as tk
import math
from tkinter import ttk


def status_tags(status):
    from .store import RETRYABLE
    if status in {"done", "draft", "partial"}:
        return [status]
    if status in RETRYABLE or status in {"no_audio", "output_required"}:
        return ["error"]
    return []


def sync_tree(tree, visible, rows):
    """Refresh rows while retaining the selection and avoiding repeated inserts."""
    selected = set(tree.selection())
    scroll = tree.yview()[0]
    wanted = []
    for item, values, tags in rows:
        wanted.append(item)
        if visible.get(item) != values:
            if tree.exists(item):
                tree.item(item, values=values, tags=tags)
            else:
                tree.insert("", "end", iid=item, values=values, tags=tags)
            visible[item] = values
    wanted_set = set(wanted)
    for item in set(visible) - wanted_set:
        tree.delete(item)
        del visible[item]
    if tree.get_children() != tuple(wanted):
        for index, item in enumerate(wanted):
            tree.move(item, "", index)
    remaining = selected & wanted_set
    if set(tree.selection()) != remaining:
        tree.selection_set(*sorted(remaining))
    tree.yview_moveto(scroll)


def duration_text(value):
    try:
        value = float(value)
        if not math.isfinite(value) or value < 0:
            return "Nieznany"
        total = round(value)
        return f"{total // 3600:02}:{total // 60 % 60:02}:{total % 60:02}"
    except (ValueError, TypeError):
        return "Nieznany"


def next_job(rows, kind, current_id=None):
    rows = [job for job in rows if job["kind"] == kind]
    current = next((job for job in rows if job["id"] == current_id and job["status"] == "running"), None)
    if current:
        return current
    return next((job for job in rows if job["enabled"] and
                 (job["status"] in {"pending", "partial", "blocked", "running"} or
                  (job["status"] == "error" and job["stage"] == "Wymaga działania"))), None)


def collapsible_help(parent, variable):
    box = ttk.Frame(parent)
    box.pack(fill="x", pady=(4, 0))
    body = ttk.Label(box, textvariable=variable, wraplength=1100, foreground="#344963")
    def toggle():
        if body.winfo_manager():
            body.pack_forget()
            button.configure(text="Pokaż podpowiedź")
        else:
            body.pack(fill="x", pady=4)
            button.configure(text="Ukryj podpowiedź")
    button = ttk.Button(box, text="Pokaż podpowiedź", command=toggle)
    button.pack(anchor="w")
    wrap_with_parent(body, 24)
    return box


class Tooltip:
    active = None

    def __init__(self, widget, text, status=None, delay=800, popup=True):
        self.widget, self.text, self.status, self.delay = widget, text, status, delay
        self.pending, self.popup, self.expiry = None, None, None
        self.allow_popup = popup and not isinstance(widget, (ttk.Treeview, ttk.Notebook, ttk.Progressbar, tk.Text))
        self.hovered = False
        widget.help_text = text
        widget.tooltip = self
        widget.bind("<Enter>", self.enter, add="+")
        widget.bind("<Motion>", self.motion, add="+")
        widget.bind("<FocusIn>", self.describe, add="+")
        widget.bind("<Leave>", self.leave, add="+")
        widget.bind("<FocusOut>", self.hide, add="+")
        widget.bind("<ButtonPress>", self.hide, add="+")
        widget.bind("<Escape>", self.hide, add="+")
        widget.bind("<F1>", self.show, add="+")
        widget.bind("<Destroy>", self.hide, add="+")
        widget.bind("<MouseWheel>", self.hide, add="+")
        widget.winfo_toplevel().bind("<Deactivate>", self.hide, add="+")

    def describe(self, event=None):
        if self.status is not None:
            self.status.set(self.text)

    def enter(self, event=None):
        self.hovered = True
        self.schedule(event)

    def leave(self, event=None):
        self.hovered = False
        self.hide(event)

    def motion(self, event=None):
        if self.hovered:
            self.schedule(event)

    def schedule(self, event=None):
        self.hide()
        self.describe(event)
        if self.allow_popup:
            self.pending = self.widget.after(self.delay, self.show_hover)

    def show_hover(self):
        self.pending = None
        if not self.hovered:
            return
        x, y = self.widget.winfo_pointerxy()
        inside = self.widget.winfo_containing(x, y)
        if inside is self.widget or (inside is not None and str(inside).startswith(str(self.widget) + ".")):
            self.show()

    def show(self, event=None):
        self.cancel()
        if self.popup:
            return "break"
        if not self.widget.winfo_exists() or not self.widget.winfo_viewable():
            return None
        if Tooltip.active and Tooltip.active is not self:
            Tooltip.active.hide()
        Tooltip.active = self
        popup = self.popup = tk.Toplevel(self.widget)
        popup.withdraw()
        popup.wm_overrideredirect(True)
        popup.transient(self.widget.winfo_toplevel())
        frame = tk.Frame(popup, bg="#fffbe6", borderwidth=1, relief="solid")
        frame.pack(fill="both", expand=True)
        tk.Label(frame, text=self.text, justify="left", wraplength=420,
                 background="#fffbe6", foreground="#202020", padx=12, pady=9).pack()
        popup.update_idletasks()
        x = min(self.widget.winfo_rootx() + 10,
                self.widget.winfo_screenwidth() - popup.winfo_reqwidth() - 12)
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        if y + popup.winfo_reqheight() > self.widget.winfo_screenheight() - 12:
            y = self.widget.winfo_rooty() - popup.winfo_reqheight() - 6
        popup.geometry(f"+{max(0, x)}+{max(0, y)}")
        popup.deiconify()
        self.expiry = self.widget.after(6000, self.hide)
        return "break"

    def cancel(self):
        if self.pending:
            try:
                self.widget.after_cancel(self.pending)
            except tk.TclError:
                pass
            self.pending = None

    def hide(self, event=None):
        self.cancel()
        if self.expiry:
            try:
                self.widget.after_cancel(self.expiry)
            except tk.TclError:
                pass
            self.expiry = None
        if self.popup:
            try:
                self.popup.destroy()
            except tk.TclError:
                pass
            self.popup = None
        if Tooltip.active is self:
            Tooltip.active = None


def help_button(parent, text, command, help_text, status=None, **kwargs):
    button = ttk.Button(parent, text=text, command=command, **kwargs)
    Tooltip(button, help_text, status)
    return button


def wrap_with_parent(label, padding):
    label.master.bind("<Configure>", lambda event: label.configure(
        wraplength=max(240, event.width - padding)), add="+")


HELP = {
    "import": "Otwiera katalog Excel z arkuszem Materiały i dodaje linki YouTube. Powtórny import pomija duplikaty. Nie rozpoczyna pobierania.",
    "files": "Dodaje wybrane pliki audio lub wideo z dysku. Oryginały pozostają na miejscu. Po imporcie naciśnij Start.",
    "folder": "Otwiera wybór folderu i po zatwierdzeniu dodaje jego nagrania do listy. Opcje Podfoldery i Tylko M4A określają zakres skanowania. Porównuje zawartość, aby nie dublować pracy. Transkrypcję uruchamiasz osobno przyciskiem Start.",
    "speakers": "Otwiera przewodnik: bezpośrednia strona Community-1, token Hugging Face, sprawdzenie dostępu i pobranie. Konfiguracja jest jednorazowa.",
    "test": "Sprawdza do 60 sekund wybranego nagrania z aktualnymi opcjami UVR i mówców. Długiego pliku nie trzeba ciąć. Próba nie uruchamia produkcyjnej kolejki.",
    "next": "Przewija listę do bieżącego nagrania albo pierwszego włączonego zadania oczekującego. Nie rozpoczyna pracy.",
    "configuration": "Otwiera ustawienia modeli, UVR i dostępu do YouTube oraz wyniki kontroli konfiguracji.",
    "youtube_access": "Pomaga przy blokadzie YouTube: otwiera film do ręcznego potwierdzenia, pozwala wybrać sesję przeglądarki lub cookies.txt i przetestować jeden link. Nie wznawia kolejki automatycznie.",
    "csv": "Zapisuje status wszystkich nagrań w CSV do otwarcia w Excelu. Nie zmienia źródłowego katalogu XLSX.",
    "guide": "Otwiera krótką instrukcję przycisków i kolejności pierwszego uruchomienia.",
    "uvr": "Przed transkrypcją oddziela głos od muzyki modelem UVR. Zachowuje źródłowe audio i wokal.flac. Może pomóc przy tle muzycznym, ale wydłuża obliczenia.",
    "keep": "Zachowuje kopię źródłowej ścieżki audio w folderze wyników. UVR włącza tę opcję automatycznie. Oryginalne pliki lokalne nigdy nie są usuwane.",
    "uvr_model": "Jednorazowo pobiera model oddzielający wokal od tła. Nie wymaga konta ani tokenu Hugging Face. Już pobrany model zostanie wykorzystany ponownie.",
    "search": "Filtruje listę według tytułu lub identyfikatora filmu. Zmienia tylko widok, a nie to, które nagrania są włączone do kolejki.",
    "date_from": "Pokazuje materiały od tej daty włącznie. Wpisz np. 2020-01-01. Puste pole oznacza brak ograniczenia.",
    "date_to": "Pokazuje materiały do tej daty włącznie. Wpisz datę jako RRRR-MM-DD. Puste pole oznacza brak ograniczenia.",
    "status": "Pokazuje tylko nagrania o wybranym statusie, np. Oczekuje albo Błąd. Filtr nie wyłącza pozostałych z kolejki.",
    "type": "Ogranicza widok do YouTube, plików lokalnych, filmów, transmisji lub Shorts. Nie zmienia kolejności przetwarzania.",
    "list": "Kliknij wiersz, aby zobaczyć jego opis lub błąd. Ctrl/Shift zaznacza wiele pozycji; Ctrl+A zaznacza widoczne. Dwuklik otwiera folder wyników. Kolumna Kolejka określa, czy Start obejmie pozycję.",
    "select": "Zaznacza wszystkie pozycje widoczne po zastosowaniu filtrów. Samo zaznaczenie nie dodaje ich do przetwarzania ani go nie rozpoczyna.",
    "enable": "Włącza zaznaczone, oczekujące materiały do kolejnej sesji Start. Nie resetuje gotowych nagrań.",
    "disable": "Pomija zaznaczone materiały przy następnym Start. Pozostają w bazie i można je później ponownie włączyć.",
    "language": "Ustawia język dla zaznaczonych, nieukończonych nagrań: auto wykrywa język; pl lub en wymusza polski albo angielski. Nie tłumaczy wypowiedzi.",
    "track": "Dla jednego zaznaczonego pliku lokalnego pokazuje dostępne ścieżki audio i pozwala wybrać jedną. Domyślnie używana jest pierwsza.",
    "progress": "Postęp bieżącego etapu jednego filmu. Po przejściu do kolejnego etapu procent zaczyna się od nowa. Ukończone bloki pozostają zapisane.",
    "start": "Rozpoczyna lub wznawia włączone nagrania YouTube od najstarszych. Pliki lokalne uruchamia się w karcie Kolejka lokalna. Gotowe materiały są pomijane. Sam filtr listy nie ogranicza zakresu Start.",
    "stop": "Zapisuje żądanie zatrzymania po całym bieżącym filmie: pobraniu, UVR, transkrypcji, mówcach i eksporcie. Nie przerywa aktualnego etapu. Następny film nie ruszy.",
    "retry": "Przywraca problematyczne nagrania do oczekujących. Działa na zaznaczonych; bez zaznaczenia obejmuje wszystkie nieudane. Potem naciśnij Start.",
    "results": "Otwiera folder wyników jednego zaznaczonego filmu. Przy wielu zaznaczeniach lub braku zaznaczenia otwiera wspólny folder wyników.",
    "transcript": "Otwiera plik TXT jednego zaznaczonego, ukończonego nagrania. Nazwa pliku zawiera tytuł i identyfikator nagrania.",
    "selected_only": "Ogranicza tę sesję do zaznaczonych nagrań YouTube. Nie wyłącza innych materiałów w bazie. Przy kolejnym Start możesz wrócić do całego katalogu.",
}
