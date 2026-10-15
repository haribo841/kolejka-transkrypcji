"""Shared queue-table sorting and an accessible control in its title heading."""
from __future__ import annotations

import json
import math
from pathlib import Path
import tkinter as tk
from tkinter import font as tkfont, ttk
import unicodedata

from .common import atomic_json, read_json
from .paths import child_path
from .store import STATUSES
from .ui_help import Tooltip

SETTINGS_FILE = "tabele-ui.json"
NUMERIC_COLUMNS = {"enabled", "progress", "duration", "audio"}
ARROWS = {"asc": "▲", "desc": "▼"}
HEADER_REGIONS = {"heading", "separator"}
CHECKBOX_HELP = ("Przy aktywnym sortowaniu odkłada nagrania ze statusem Gotowe na koniec listy, "
                "w ich kolejności źródłowej. Częściowe i błędne nagrania pozostają w sortowaniu. "
                "Trzeci klik nagłówka przywraca kolejność źródłową całej listy. "
                "Space zmienia opcję; F1 pokazuje tę pomoc.")


def column_value(job, column):
    """Return source values rather than formatted durations or percentages."""
    if column == "status":
        return STATUSES.get(job.get("status"), job.get("status"))
    if column == "audio":
        return job.get("audio_track")
    if column == "format":
        return Path(job.get("source", "")).suffix.lstrip(".")
    if column == "type":
        metadata = job.get("source_meta", "{}")
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except ValueError:
                metadata = {}
        return metadata.get("kind", job.get("kind")) if isinstance(metadata, dict) else job.get("kind")
    return job.get(column)


def polish_key(value):
    """Portable Polish letter order without changing the process's locale."""
    text = unicodedata.normalize("NFC", str(value).casefold())
    variants = {"ą": "a", "ć": "c", "ę": "e", "ł": "l", "ń": "n", "ó": "o", "ś": "s", "ź": "z", "ż": "z"}
    return tuple((ord(variants.get(char, char)), 2 if char == "ż" else int(char in variants)) for char in text)


def _sort_key(value, column):
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if column in NUMERIC_COLUMNS:
        try:
            number = float(value)
            return number if math.isfinite(number) and number >= 0 else None
        except (ValueError, TypeError, OverflowError):
            return None
    return polish_key(value.isoformat() if hasattr(value, "isoformat") else value)


def _ordered(jobs, column, direction, value_for, skip_done):
    source = list(jobs)
    if direction is None or column is None:
        return source
    known, missing, completed = [], [], []
    for job in source:
        if skip_done and job.get("status") == "done":
            completed.append(job)
            continue
        key = _sort_key(value_for(job, column), column)
        if key is None:
            missing.append(job)
        else:
            known.append((key, job))
    known.sort(key=lambda pair: pair[0], reverse=direction == "desc")
    return [job for _, job in known] + missing + completed


class TableView:
    def __init__(self, tree, registry, kind, changed, status=None, *, value_for=None, scroll_set=None):
        if kind not in {"youtube", "local"}:
            raise ValueError("Widok tabeli musi dotyczyć YouTube albo nagrań lokalnych.")
        self.tree, self.kind, self.changed, self.status = tree, kind, changed, status
        self.path = child_path(registry, SETTINGS_FILE)
        self.value_for, self.scroll_set = value_for or column_value, scroll_set
        self.columns = tuple(tree.cget("columns"))
        self.original_headings = {column: dict(tree.heading(column)) for column in self.columns}
        self.original_scroll = tree.cget("xscrollcommand")
        self.bindings, self.commands = [], []
        self.after_id, self.closed = None, False
        saved = self._preferences().get(kind, {})
        saved = saved if isinstance(saved, dict) else {}
        self.sort_column = saved.get("sort_column")
        self.sort_direction = saved.get("sort_direction")
        if self.sort_column not in self.columns or not isinstance(self.sort_direction, str) or self.sort_direction not in ARROWS:
            self.sort_column = self.sort_direction = None
        self.skip_done = tk.BooleanVar(master=tree, value=saved.get("skip_done") is True)
        for column in self.columns:
            command = tree.register(lambda column=column: self.cycle(column))
            self.commands.append(command)
            tree.heading(column, command=command)
        self.scroll_command = tree.register(self._scrolled)
        self.commands.append(self.scroll_command)
        tree.configure(xscrollcommand=self.scroll_command)
        self.checkbox = ttk.Checkbutton(tree, text="Pomiń przetworzone", variable=self.skip_done,
                                        command=self._checkbox_changed, takefocus=True)
        self.tooltip = Tooltip(self.checkbox, CHECKBOX_HELP, status)
        if "title" in self.columns:
            tree.heading("title", anchor="w")
        self._headings()
        for sequence in ("<Configure>", "<Map>", "<<ThemeChanged>>", "<B1-Motion>", "<ButtonRelease-1>"):
            self.bindings.append((sequence, tree.bind(sequence, self._schedule_geometry, add="+")))
        self.bindings.append(("<Destroy>", tree.bind("<Destroy>", self._destroyed, add="+")))
        self._schedule_geometry()

    def _preferences(self):
        try:
            value = read_json(self.path, {})
            return value if isinstance(value, dict) else {}
        except (ValueError, OSError):
            return {}

    def _save(self):
        value = self._preferences()
        value[self.kind] = {"sort_column": self.sort_column, "sort_direction": self.sort_direction,
                            "skip_done": self.skip_done.get()}
        try:
            atomic_json(self.path, value)
        except (ValueError, OSError):
            if self.status is not None:
                self.status.set("Widok zmieniony. Nie udało się zapisać ustawień tabeli na kolejne uruchomienie.")

    def _headings(self):
        for column in self.columns:
            text = self.original_headings[column]["text"]
            if column == self.sort_column and self.sort_direction in ARROWS:
                text += " " + ARROWS[self.sort_direction]
            self.tree.heading(column, text=text)

    def cycle(self, column):
        if self.closed or column not in self.columns:
            return
        if column != self.sort_column:
            self.sort_column, self.sort_direction = column, "asc"
        elif self.sort_direction == "asc":
            self.sort_direction = "desc"
        else:
            self.sort_column = self.sort_direction = None
        self._headings()
        self._changed()

    def _checkbox_changed(self):
        if not self.closed:
            self._changed()

    def _changed(self):
        self._save()
        self.changed()
        self.tree.yview_moveto(0)
        self._schedule_geometry()

    def order(self, jobs):
        return _ordered(jobs, self.sort_column, self.sort_direction, self.value_for, self.skip_done.get())

    def _scrolled(self, first, last):
        if self.scroll_set is not None:
            self.scroll_set(first, last)
        elif self.original_scroll:
            self.tree.tk.call(*self.tree.tk.splitlist(self.original_scroll), first, last)
        self._schedule_geometry()

    def _schedule_geometry(self, event=None):
        if not self.closed and self.after_id is None:
            self.after_id = self.tree.after_idle(self._place_checkbox)

    def _display_columns(self):
        displayed = tuple(self.tree.cget("displaycolumns"))
        if displayed == ("#all",):
            return self.columns
        return tuple(self.columns[int(value)] if str(value).isdigit() else str(value) for value in displayed)

    def _column_index(self, x, count):
        column = self.tree.identify_column(x)
        if column.startswith("#"):
            return int(column[1:])
        return 0 if x < 8 else count + 1

    def _first_column(self, target, width, count):
        left, right = 1, width - 1
        while left < right:
            middle = (left + right) // 2
            if self._column_index(middle, count) >= target:
                right = middle
            else:
                left = middle + 1
        return left

    def _heading_height(self, x):
        top = None
        for y in range(self.tree.winfo_height()):
            is_header = self.tree.identify_region(x, y) in HEADER_REGIONS
            if is_header and top is None:
                top = y
            elif top is not None and not is_header:
                return top, y - top
        return None

    def _heading_rectangle(self):
        displayed = self._display_columns()
        if "title" not in displayed or self.tree.winfo_width() < 2:
            return None
        target, width = displayed.index("title") + 1, self.tree.winfo_width()
        left = self._first_column(target, width, len(displayed))
        if self._column_index(left, len(displayed)) != target:
            return None
        right = self._first_column(target + 1, width, len(displayed))
        header = self._heading_height((left + right) // 2)
        if header is None:
            return None
        center_y = header[0] + header[1] // 2
        # identify_column includes the resize handle on Windows. Keep the
        # control inside the actual heading, leaving that handle usable.
        while right > left and self.tree.identify_region(right - 1, center_y) != "heading":
            right -= 1
        while left < right and self.tree.identify_region(left, center_y) != "heading":
            left += 1
        return (left, right, *header) if left < right else None

    def _place_checkbox(self):
        self.after_id = None
        if self.closed:
            return
        rectangle = self._heading_rectangle() if self.tree.winfo_ismapped() else None
        if rectangle is None:
            self.checkbox.place_forget()
            return
        left, right, top, height = rectangle
        width = self.checkbox.winfo_reqwidth()
        font = ttk.Style(self.tree).lookup("Treeview.Heading", "font") or "TkDefaultFont"
        label_width = tkfont.Font(root=self.tree, font=font).measure(self.tree.heading("title", "text"))
        if right - left < width + label_width + 24:
            self.checkbox.place_forget()
            return
        control_height = min(height, self.checkbox.winfo_reqheight())
        self.checkbox.place(x=right - width - 8, y=top + (height - control_height) // 2,
                            width=width, height=control_height)
        self.checkbox.lift()

    def _destroyed(self, event):
        if event.widget is self.tree:
            self.close()

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self.after_id:
            self.tree.after_cancel(self.after_id)
            self.after_id = None
        self.tooltip.hide()
        self.checkbox.destroy()
        if self.tree.winfo_exists():
            self.tree.configure(xscrollcommand=self.original_scroll)
            for column, heading in self.original_headings.items():
                self.tree.heading(column, text=heading["text"], anchor=heading["anchor"], command=heading["command"])
            for sequence, identifier in self.bindings:
                self.tree.unbind(sequence, identifier)
        for command in self.commands:
            self.tree.deletecommand(command)
