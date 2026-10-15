"""Shared sorting, persisted view state and native heading-checkbox behavior."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import tkinter as tk
from tkinter import ttk
import unittest

from batch.common import atomic_json
from batch.table_view import SETTINGS_FILE, TableView, _ordered, column_value, polish_key
from batch.ui_help import sync_tree


def recording(ident, title, duration=None, status="pending", **extra):
    return {"id": ident, "title": title, "duration": duration, "status": status,
            "date": f"2020-01-{ident:02}", "progress": 0, "audio_track": 0,
            "enabled": 1, "source": f"nagranie-{ident}.m4a", "language": "auto", **extra}


def identifiers(rows):
    return [row["id"] for row in rows]


class OrderingTests(unittest.TestCase):
    def test_polish_order_is_case_insensitive_and_keeps_accented_letter_positions(self):
        titles = ["Żaba", "źrebak", "Zebra", "Łódź", "las", "ćma", "Cel", "ącki", "Adam", "świt", "Sen"]
        self.assertEqual(sorted(titles, key=polish_key),
                         ["Adam", "ącki", "Cel", "ćma", "las", "Łódź", "Sen", "świt", "Zebra", "źrebak", "Żaba"])
        self.assertEqual(polish_key("ZaŻÓŁĆ"), polish_key("zażółć"))
        self.assertEqual(polish_key("a\u0328cki"), polish_key("ącki"))

    def test_duration_is_numeric_and_missing_values_stay_last_in_both_directions(self):
        rows = [recording(1, "hour", 36000), recording(2, "unknown"), recording(3, "short", 9),
                recording(4, "nonfinite", float("nan")), recording(5, "minute", 90),
                recording(6, "negative", -1)]
        self.assertEqual(identifiers(_ordered(rows, "duration", "asc", column_value, False)), [3, 5, 1, 2, 4, 6])
        self.assertEqual(identifiers(_ordered(rows, "duration", "desc", column_value, False)), [1, 5, 3, 2, 4, 6])

    def test_numeric_progress_audio_and_enabled_do_not_use_formatted_text(self):
        rows = [recording(1, "a", progress=100, audio_track=10, enabled=1),
                recording(2, "b", progress=9, audio_track=2, enabled=0),
                recording(3, "c", progress=20, audio_track=0, enabled=1)]
        self.assertEqual(identifiers(_ordered(rows, "progress", "asc", column_value, False)), [2, 3, 1])
        self.assertEqual(identifiers(_ordered(rows, "audio", "asc", column_value, False)), [3, 2, 1])
        self.assertEqual(identifiers(_ordered(rows, "enabled", "asc", column_value, False)), [2, 1, 3])

    def test_equal_keys_keep_canonical_order_ascending_and_descending(self):
        rows = [recording(3, "ALFA"), recording(1, "alfa"), recording(2, "Beta")]
        self.assertEqual(identifiers(_ordered(rows, "title", "asc", column_value, False)), [3, 1, 2])
        self.assertEqual(identifiers(_ordered(rows, "title", "desc", column_value, False)), [2, 3, 1])

    def test_skip_done_moves_only_done_to_the_end_in_canonical_order(self):
        rows = [recording(1, "Z", status="done"), recording(2, "B", status="partial"),
                recording(3, "A", status="error"), recording(4, "A", status="done"), recording(5, "C")]
        self.assertEqual(identifiers(_ordered(rows, "title", "asc", column_value, True)), [3, 2, 5, 1, 4])
        self.assertEqual(identifiers(_ordered(rows, "title", "desc", column_value, True)), [5, 2, 3, 1, 4])
        self.assertEqual(_ordered(rows, None, None, column_value, True), rows)

    def test_column_values_use_status_names_metadata_and_recording_format(self):
        job = recording(1, "a", status="partial", source_meta='{"kind":"Transmisja"}')
        self.assertEqual(column_value(job, "status"), "Częściowo gotowe")
        self.assertEqual(column_value(job, "type"), "Transmisja")
        self.assertEqual(column_value(job, "format"), "m4a")
        self.assertEqual(column_value(job, "audio"), 0)
        self.assertIsNone(column_value(job, "absent"))

    def test_dates_and_override_values_are_used_without_mutating_source(self):
        rows = [recording(1, "a", type="Film", date="2024-10-01"),
                recording(2, "b", type="Transmisja", date="2019-10-01"), recording(3, "c", type="Film", date="")]
        original = [dict(row) for row in rows]
        self.assertEqual(identifiers(_ordered(rows, "date", "desc", column_value, False)), [1, 2, 3])
        def value_for(job, column):
            return job["type"] if column == "type" else column_value(job, column)
        self.assertEqual(identifiers(_ordered(rows, "type", "desc", value_for, False)), [2, 1, 3])
        self.assertEqual(rows, original)


class TableViewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.registry = Path(self.temp.name)
        self.root = tk.Tk()
        self.root.attributes("-alpha", 0)
        self.root.geometry("820x270+0+0")
        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        self.columns = ("date", "duration", "title", "status", "progress", "audio", "enabled", "language", "format")
        self.tree = ttk.Treeview(self.root, columns=self.columns, show="headings")
        for column, label, width in zip(self.columns,
                ("Data", "Czas trwania", "Nagranie", "Status", "Etap %", "Audio", "Kolejka", "Język", "Format"),
                (90, 100, 570, 110, 80, 80, 70, 80, 70)):
            self.tree.heading(column, text=label)
            self.tree.column(column, width=width, stretch=False, minwidth=40)
        self.tree.pack(fill="both", expand=True)
        self.scroll_values, self.change_count, self.visible = [], 0, {}
        self.rows = [recording(1, "Zulu", 100, status="done"), recording(2, "Alfa", 9), recording(3, "Beta", 20)]
        self.status = tk.StringVar(master=self.root)
        self.table = TableView(self.tree, self.registry, "local", self.render, self.status,
                               scroll_set=lambda first, last: self.scroll_values.append((first, last)))
        self.render()
        self.root.update()

    def tearDown(self):
        self.table.close()
        self.root.destroy()
        self.temp.cleanup()

    def render(self):
        self.change_count += 1
        rows = [(str(job["id"]), tuple(column_value(job, column) for column in self.columns), [])
                for job in self.table.order(self.rows)]
        sync_tree(self.tree, self.visible, rows)

    def invoke_heading(self, column):
        self.root.tk.call(self.tree.heading(column, "command"))
        self.root.update()

    def test_default_constructing_table_never_creates_preferences(self):
        self.assertFalse(self.table.skip_done.get())
        self.assertIsNone(self.table.sort_column)
        self.assertIsNone(self.table.sort_direction)
        self.assertFalse((self.registry / SETTINGS_FILE).exists())

    def test_heading_cycles_arrows_and_third_click_restores_source_even_when_checked(self):
        self.table.checkbox.invoke()
        self.invoke_heading("title")
        self.assertEqual((self.table.sort_column, self.table.sort_direction), ("title", "asc"))
        self.assertEqual(self.tree.heading("title", "text"), "Nagranie ▲")
        self.assertEqual(self.tree.get_children(), ("2", "3", "1"))
        self.invoke_heading("title")
        self.assertEqual(self.tree.heading("title", "text"), "Nagranie ▼")
        self.assertEqual(self.tree.get_children(), ("3", "2", "1"))
        self.invoke_heading("title")
        self.assertEqual(self.tree.heading("title", "text"), "Nagranie")
        self.assertEqual(self.tree.get_children(), ("1", "2", "3"))
        self.assertIsNone(self.table.sort_direction)

    def test_switching_column_starts_ascending_and_clears_previous_arrow(self):
        self.invoke_heading("title")
        self.invoke_heading("title")
        self.invoke_heading("duration")
        self.assertEqual((self.table.sort_column, self.table.sort_direction), ("duration", "asc"))
        self.assertEqual(self.tree.heading("title", "text"), "Nagranie")
        self.assertEqual(self.tree.heading("duration", "text"), "Czas trwania ▲")
        self.assertEqual(self.tree.get_children(), ("2", "3", "1"))

    def test_refresh_maintains_sort_selection_and_new_row_placement(self):
        self.tree.selection_set("1", "3")
        self.invoke_heading("title")
        self.rows.append(recording(4, "Aardvark", 1))
        self.render()
        self.render()
        self.assertEqual(self.tree.get_children(), ("4", "2", "3", "1"))
        self.assertEqual(set(self.tree.selection()), {"1", "3"})
        self.assertEqual(self.tree.heading("title", "text"), "Nagranie ▲")

    def test_click_saves_only_this_kind_and_reopen_restores_arrow_and_checkbox(self):
        path = self.registry / SETTINGS_FILE
        atomic_json(path, {"youtube": {"sort_column": "date", "sort_direction": "desc", "skip_done": False}})
        self.invoke_heading("duration")
        self.table.checkbox.invoke()
        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(saved["youtube"], {"sort_column": "date", "sort_direction": "desc", "skip_done": False})
        self.assertEqual(saved["local"], {"sort_column": "duration", "sort_direction": "asc", "skip_done": True})
        self.table.close()
        self.table = TableView(self.tree, self.registry, "local", self.render, self.status)
        self.assertEqual((self.table.sort_column, self.table.sort_direction), ("duration", "asc"))
        self.assertTrue(self.table.skip_done.get())
        self.assertEqual(self.tree.heading("duration", "text"), "Czas trwania ▲")

    def test_invalid_or_corrupted_preferences_start_with_safe_defaults(self):
        self.table.close()
        path = self.registry / SETTINGS_FILE
        atomic_json(path, {"local": {"sort_column": "not-a-column", "sort_direction": "asc", "skip_done": "true"}})
        self.table = TableView(self.tree, self.registry, "local", self.render)
        self.assertIsNone(self.table.sort_column)
        self.assertFalse(self.table.skip_done.get())
        self.table.close()
        path.write_text("malformed JSON", encoding="utf-8")
        self.table = TableView(self.tree, self.registry, "local", self.render)
        self.assertIsNone(self.table.sort_direction)
        self.assertFalse(self.table.skip_done.get())

    def test_unexpected_preference_types_do_not_break_table_construction(self):
        self.table.close()
        atomic_json(self.registry / SETTINGS_FILE,
                    {"local": {"sort_column": "title", "sort_direction": ["asc"], "skip_done": 1}})
        self.table = TableView(self.tree, self.registry, "local", self.render)
        self.assertIsNone(self.table.sort_direction)
        self.assertFalse(self.table.skip_done.get())

    def test_native_checkbox_has_focus_space_f1_and_does_not_cycle_a_heading(self):
        self.assertIsInstance(self.table.checkbox, ttk.Checkbutton)
        self.assertEqual(str(self.table.checkbox.cget("takefocus")), "1")
        before = self.change_count
        checkbox = self.table.checkbox
        checkbox.event_generate("<Enter>")
        checkbox.event_generate("<ButtonPress-1>", x=8, y=8)
        checkbox.event_generate("<ButtonRelease-1>", x=8, y=8)
        self.root.update()
        self.assertTrue(self.table.skip_done.get())
        self.assertEqual(self.change_count, before + 1)
        self.assertIsNone(self.table.sort_column)
        checkbox.focus_force()
        self.root.update()
        checkbox.event_generate("<Key-space>")
        self.root.update()
        self.assertFalse(self.table.skip_done.get())
        self.assertIsNone(self.table.sort_column)
        checkbox.event_generate("<F1>")
        self.root.update()
        self.assertIn("Częściowe", self.status.get())
        self.assertIsNotNone(self.table.tooltip.popup)

    def test_header_geometry_works_without_any_rows_and_avoids_separator(self):
        self.rows = []
        self.render()
        self.root.update()
        checkbox = self.table.checkbox
        self.assertTrue(checkbox.winfo_ismapped())
        x, y = checkbox.winfo_x(), checkbox.winfo_y()
        end = x + checkbox.winfo_width() - 1
        title_column = "#" + str(self.columns.index("title") + 1)
        self.assertEqual(self.tree.identify_column(x), title_column)
        self.assertEqual(self.tree.identify_column(end), title_column)
        self.assertEqual(self.tree.identify_region(end, y), "heading")
        self.assertEqual(str(self.tree.heading("title", "anchor")), "w")

    def test_horizontal_scroll_repositions_control_and_forwards_scrollbar_updates(self):
        checkbox = self.table.checkbox
        original_x = checkbox.winfo_x()
        self.tree.xview_moveto(.1)
        self.root.update()
        self.assertTrue(self.scroll_values)
        self.assertTrue(checkbox.winfo_ismapped())
        self.assertLess(checkbox.winfo_x(), original_x)
        self.tree.column("format", width=900)
        self.tree.xview_moveto(1)
        self.root.update()
        self.assertFalse(checkbox.winfo_ismapped())
        self.tree.xview_moveto(0)
        self.root.update()
        self.assertTrue(checkbox.winfo_ismapped())

    def test_resizing_and_theme_change_recompute_header_control(self):
        self.tree.column("title", width=100)
        self.tree.event_generate("<ButtonRelease-1>", x=200, y=8)
        self.root.update()
        self.assertFalse(self.table.checkbox.winfo_ismapped())
        self.tree.column("title", width=570)
        self.root.geometry("760x230")
        self.tree.event_generate("<<ThemeChanged>>")
        self.root.update()
        self.assertTrue(self.table.checkbox.winfo_ismapped())
        self.assertLessEqual(self.table.checkbox.winfo_x() + self.table.checkbox.winfo_width(), self.tree.winfo_width())

    def test_reordered_or_hidden_display_columns_keep_checkbox_in_the_title_heading(self):
        displayed = ("title", "date", "duration", "status")
        self.tree.configure(displaycolumns=displayed)
        self.tree.event_generate("<Configure>")
        self.root.update()
        self.assertTrue(self.table.checkbox.winfo_ismapped())
        x, y = self.table.checkbox.winfo_x(), self.table.checkbox.winfo_y()
        self.assertEqual(self.tree.identify_column(x), "#1")
        self.assertEqual(self.tree.identify_region(x, y), "heading")
        self.tree.configure(displaycolumns=("date", "duration"))
        self.tree.event_generate("<Configure>")
        self.root.update()
        self.assertFalse(self.table.checkbox.winfo_ismapped())

    def test_original_scroll_command_is_forwarded_and_restored(self):
        self.table.close()
        values = []
        original = self.tree.register(lambda first, last: values.append((first, last)))
        self.tree.configure(xscrollcommand=original)
        self.table = TableView(self.tree, self.registry, "local", self.render)
        self.tree.xview_moveto(.1)
        self.root.update()
        self.assertTrue(values)
        self.table.close()
        self.assertEqual(str(self.tree.cget("xscrollcommand")), original)

    def test_destroying_tree_closes_controller_without_leftover_callbacks(self):
        self.table._schedule_geometry()
        self.tree.destroy()
        self.root.update()
        self.assertTrue(self.table.closed)
        self.assertIsNone(self.table.after_id)

    def test_click_returns_to_top_but_plain_refresh_retains_scroll(self):
        self.rows = [recording(index, f"Film {index}", index) for index in range(1, 60)]
        self.render()
        self.root.update()
        self.tree.yview_moveto(.5)
        self.render()
        self.assertGreater(self.tree.yview()[0], .3)
        self.invoke_heading("title")
        self.assertEqual(self.tree.yview()[0], 0)

    def test_native_heading_click_only_cycles_once(self):
        self.tree.event_generate("<ButtonPress-1>", x=40, y=8)
        self.tree.event_generate("<ButtonRelease-1>", x=40, y=8)
        self.root.update()
        self.assertEqual((self.table.sort_column, self.table.sort_direction), ("date", "asc"))

    def test_close_cancels_callbacks_restores_native_configuration_and_is_repeatable(self):
        self.invoke_heading("title")
        self.table._schedule_geometry()
        self.assertIsNotNone(self.table.after_id)
        self.table.close()
        self.table.close()
        self.root.update()
        self.assertIsNone(self.table.after_id)
        self.assertEqual(self.tree.heading("title", "text"), "Nagranie")
        self.assertEqual(self.tree.heading("title", "command"), "")
        self.assertEqual(str(self.tree.heading("title", "anchor")), "center")
        self.assertEqual(self.tree.cget("xscrollcommand"), "")


if __name__ == "__main__":
    unittest.main()
