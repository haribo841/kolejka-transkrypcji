"""Offline integration checks for ordered visible queue snapshots and Tk state."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

from batch.common import WorkerLock, atomic_json, defaults
from batch.gui import Window


def ready_fixture(*_args):
    return {"passed": True, "test": {"at": "offline"}, "components": {
        key: {"status": "Próba zaliczona", "message": "Offline fixture"}
        for key in ("whisper", "tools", "diarization", "uvr", "youtube")}}


class SortedQueueIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        config = defaults()
        config.update(diarization=False, uvr_enabled=False)
        atomic_json(self.root / "ustawienia.json", config)
        self.readiness_patch = patch("batch.gui.collect_readiness", side_effect=ready_fixture)
        self.readiness_patch.start()
        self.addCleanup(self.readiness_patch.stop)
        self.metadata_patch = patch("batch.gui.QueueWindow.enrich_metadata")
        self.metadata_patch.start()
        self.addCleanup(self.metadata_patch.stop)
        self.app = None
        self.open_window()
        self.identifiers = {}
        for kind in ("local", "youtube"):
            specifications = (
                ("zeta", "Zeta pokaz", 10, "pending", True),
                ("alpha", "Alpha pokaz", 2, "pending", True),
                ("lodz", "Łódź pokaz", 5, "pending", True),
                ("done", "Aardvark pokaz gotowe", 1, "done", True),
                ("hidden", "Inny materiał", 3, "pending", True),
                ("disabled", "Wyłączony pokaz", 4, "pending", False),
                ("unknown", "Nieznany pokaz", None, "pending", True),
                ("failed", "Błąd pokaz", 6, "unavailable", True),
            )
            for offset, (key, title, duration, status, enabled) in enumerate(specifications):
                identity = "local:" + f"{offset + 1:064x}" if kind == "local" else f"yt:abcdefghij{offset}"
                self.app.store.add_many([{
                    "identity": identity, "kind": kind, "title": title,
                    "source": str(self.root / "źródła" / f"{key}.m4a") if kind == "local" else
                              f"https://www.youtube.com/watch?v=abcdefghij{offset}",
                    "date": f"2020-01-{offset + 1:02d}", "duration": duration,
                    "status": status, "meta": {"kind": "Film"}}])
                job = next(row for row in self.app.store.jobs() if row["identity"] == identity)
                self.identifiers[kind, key] = job["id"]
                if not enabled:
                    self.app.store.configure([job["id"]], enabled=0)
        self.reload()

    def tearDown(self):
        if self.app is not None:
            self.app.close()
        self.temporary.cleanup()

    def open_window(self):
        self.tk = tk.Tk()
        self.tk.withdraw()
        self.app = Window(self.root, tk_root=self.tk)

    def reload(self):
        self.app.jobs = {row["id"]: row for row in self.app.store.jobs()}
        self.app.render_list()
        self.app.local_tab.render()

    def view(self, kind):
        return self.app.local_tab if kind == "local" else self.app

    def ids(self, kind, *keys):
        return [self.identifiers[kind, key] for key in keys]

    def click_heading(self, kind, column):
        tree = self.view(kind).tree
        command = tree.heading(column, "command")
        self.assertTrue(command, f"{kind}: {column} has no sorting command")
        tree.tk.call(command)

    def set_skip_done(self, kind, enabled):
        table = self.view(kind).table_view
        if table.skip_done.get() != enabled:
            table.checkbox.invoke()
        self.assertEqual(table.skip_done.get(), enabled)

    def filtered(self, kind):
        view = self.view(kind)
        view.search.set("pokaz")
        view.status.set("Oczekuje")
        if kind == "local":
            view.filter_now()
        else:
            self.app.filter_now()

    def start_and_capture(self, kind, selected=False):
        process = Mock()
        process.poll.return_value = 0
        with patch("batch.gui.preflight"), patch("batch.gui.check_destination"), \
                patch("batch.gui.launch", return_value=process) as launch:
            if kind == "local":
                (self.app.local_tab.start_selected if selected else self.app.local_tab.start)()
            else:
                (self.app.start_youtube_selected if selected else self.app.start)()
            launch.assert_called_once()
            return launch.call_args

    def assert_snapshot(self, captured, kind, identifiers, mode):
        self.assertEqual(captured.args, (self.root,))
        self.assertEqual(captured.kwargs, {"kind": kind, "job_ids": identifiers,
                                         "order_ids": identifiers, "scope_mode": mode})

    def test_local_main_start_uses_only_filtered_eligible_numeric_order(self):
        self.filtered("local")
        self.click_heading("local", "duration")
        before = self.app.store.control(), self.app.store.jobs()
        captured = self.start_and_capture("local")
        self.assert_snapshot(captured, "local", self.ids("local", "alpha", "lodz", "zeta", "unknown"), "visible")
        self.assertEqual(self.app.store.control(), before[0])
        self.assertEqual(self.app.store.jobs(), before[1])

    def test_youtube_main_start_obeys_search_dates_status_and_descending_view(self):
        self.filtered("youtube")
        self.app.date_from.set("2020-01-02")
        self.app.date_to.set("2020-01-07")
        self.app.kind.set("Film")
        self.click_heading("youtube", "duration")
        self.click_heading("youtube", "duration")
        self.assert_snapshot(self.start_and_capture("youtube"), "youtube",
                             self.ids("youtube", "lodz", "alpha", "unknown"), "visible")

    def test_selected_local_start_is_visible_subset_in_table_order_without_rewriting_flags(self):
        self.filtered("local")
        self.click_heading("local", "duration")
        tree = self.app.local_tab.tree
        tree.selection_set(*map(str, self.ids("local", "zeta", "disabled", "alpha")))
        before = self.app.store.jobs()
        self.assert_snapshot(self.start_and_capture("local", selected=True), "local",
                             self.ids("local", "alpha", "zeta"), "selected")
        self.assertEqual(self.app.store.jobs(), before)

    def test_selected_youtube_start_does_not_widen_to_other_visible_or_local_jobs(self):
        self.filtered("youtube")
        self.click_heading("youtube", "duration")
        self.app.tree.selection_set(*map(str, self.ids("youtube", "unknown", "alpha")))
        self.assert_snapshot(self.start_and_capture("youtube", selected=True), "youtube",
                             self.ids("youtube", "alpha", "unknown"), "selected")
        self.assertTrue(all(row["status"] == "pending" for row in self.app.store.jobs()
                            if row["kind"] == "local" and row["status"] != "done" and row["status"] != "unavailable"))

    def test_empty_filtered_scope_never_starts_hidden_pending_jobs(self):
        for kind in ("youtube", "local"):
            with self.subTest(kind=kind):
                view = self.view(kind)
                view.search.set("nieistniejący tytuł")
                view.filter_now()
                before = self.app.store.control()
                with patch("batch.gui.preflight"), patch("batch.gui.check_destination"), \
                        patch("batch.gui.messagebox.showinfo"), patch("batch.gui.launch") as launch:
                    (view.start if kind == "local" else self.app.start)()
                    launch.assert_not_called()
                self.assertEqual(self.app.store.control(), before)

    def test_start_button_counts_exclude_hidden_disabled_and_completed_recordings(self):
        for kind in ("local", "youtube"):
            with self.subTest(kind=kind):
                self.filtered(kind)
                view = self.view(kind)
                view.tree.selection_set(*map(str, self.ids(kind, "disabled", "alpha")))
                self.app.update_session_controls()
                self.assertEqual(view.controls["start"].cget("text"), "Start/Wznów widoczne (4)")
                self.assertEqual(view.controls["start_selected"].cget("text"), "Start zaznaczonych (1)")

    def test_each_queue_restores_its_own_sort_and_skip_done_settings(self):
        self.click_heading("youtube", "duration")
        self.click_heading("local", "title")
        self.click_heading("local", "title")
        self.set_skip_done("youtube", True)
        self.set_skip_done("local", False)
        expected = ((self.app.table_view.sort_column, self.app.table_view.sort_direction),
                    (self.app.local_tab.table_view.sort_column, self.app.local_tab.table_view.sort_direction))
        before = self.app.store.control(), self.app.store.jobs()
        self.app.close()
        self.app = None
        self.open_window()
        self.reload()
        actual = ((self.app.table_view.sort_column, self.app.table_view.sort_direction),
                  (self.app.local_tab.table_view.sort_column, self.app.local_tab.table_view.sort_direction))
        self.assertEqual(actual, expected)
        self.assertTrue(self.app.table_view.skip_done.get())
        self.assertFalse(self.app.local_tab.table_view.skip_done.get())
        self.assertEqual(self.app.store.control(), before[0])
        self.assertEqual(self.app.store.jobs(), before[1])

    def test_skip_done_groups_results_only_while_a_sort_is_active_and_never_hides_them(self):
        for kind in ("local", "youtube"):
            with self.subTest(kind=kind):
                view = self.view(kind)
                self.set_skip_done(kind, True)
                initial = tuple(str(row["id"]) for row in self.app.store.jobs() if row["kind"] == kind)
                self.assertEqual(view.tree.get_children(), initial)
                self.click_heading(kind, "title")
                self.assertEqual(view.tree.get_children()[-1], str(self.identifiers[kind, "done"]))
                self.assertEqual(set(view.tree.get_children()), set(initial))
                self.click_heading(kind, "title")
                self.click_heading(kind, "title")
                self.assertEqual(view.tree.get_children(), initial)

    def test_refresh_retains_sort_selection_and_scroll_after_progress_changes(self):
        for kind in ("youtube", "local"):
            with self.subTest(kind=kind):
                view = self.view(kind)
                self.click_heading(kind, "duration")
                tree = view.tree
                selected = tuple(map(str, self.ids(kind, "alpha", "zeta")))
                tree.selection_set(selected)
                expected_order = tree.get_children()
                self.app.jobs[self.identifiers[kind, "alpha"]]["progress"] = 27
                with patch.object(tree, "yview", return_value=(.35, .65)), \
                        patch.object(tree, "yview_moveto") as move:
                    (view.render if kind == "local" else self.app.render_list)()
                    self.assertEqual(set(tree.selection()), set(selected))
                    self.assertEqual(tree.get_children(), expected_order)
                    move.assert_called_once_with(.35)
                self.assertEqual(view.table_view.sort_column, "duration")

    def test_user_sort_moves_to_top_without_changing_queue_control(self):
        for kind in ("local", "youtube"):
            with self.subTest(kind=kind):
                tree = self.view(kind).tree
                before = self.app.store.control()
                with patch.object(tree, "yview", return_value=(.5, .8)), \
                        patch.object(tree, "yview_moveto") as move:
                    self.click_heading(kind, "duration")
                    self.assertIn(((0,), {}), [(call.args, call.kwargs) for call in move.call_args_list])
                self.assertEqual(self.app.store.control(), before)

    def test_double_click_on_heading_never_opens_selected_recording_results(self):
        self.tk.attributes("-alpha", 0)
        self.tk.deiconify()
        try:
            for index, kind in enumerate(("local", "youtube")):
                with self.subTest(kind=kind):
                    page = self.app.local_page if kind == "local" else self.app.queue_page
                    self.app.notebook.select(page)
                    self.tk.update()
                    tree = self.view(kind).tree
                    tree.selection_set(str(self.identifiers[kind, "alpha"]))
                    x, y = 12, 10
                    self.assertEqual(tree.identify_region(x, y), "heading")
                    with patch.object(self.app, "open_results") as open_results:
                        for click in range(2):
                            clock = 1000 + index * 1000 + click * 100
                            tree.event_generate("<ButtonPress-1>", x=x, y=y, time=clock)
                            tree.event_generate("<ButtonRelease-1>", x=x, y=y, time=clock + 10)
                            self.tk.update()
                        open_results.assert_not_called()
        finally:
            self.tk.withdraw()

    def test_view_changes_during_live_local_session_do_not_rewrite_frozen_scope(self):
        ordered = self.ids("local", "zeta", "alpha")
        self.app.store.start(kind="local", job_ids=ordered, order_ids=ordered, scope_mode="visible")
        self.app.store.control(state="running")
        lock = WorkerLock(self.root)
        self.assertTrue(lock.acquire())
        try:
            before = self.app.store.control(), self.app.store.jobs()
            self.filtered("local")
            self.click_heading("local", "duration")
            self.click_heading("youtube", "title")
            self.set_skip_done("local", True)
            self.app.update_session_controls()
            with patch("batch.gui.launch") as launch:
                self.app.local_tab.controls["start"].invoke()
                self.app.local_tab.controls["start_selected"].invoke()
                launch.assert_not_called()
            self.assertEqual(self.app.store.control(), before[0])
            self.assertEqual(self.app.store.jobs(), before[1])
        finally:
            lock.close()

    def test_session_latches_the_button_that_started_it_despite_both_scopes_having_ids(self):
        lock = WorkerLock(self.root)
        self.assertTrue(lock.acquire())
        try:
            for kind in ("local", "youtube"):
                selected = self.ids(kind, "alpha")
                for mode in ("visible", "selected"):
                    with self.subTest(kind=kind, mode=mode):
                        self.app.store.start(kind=kind, job_ids=selected, order_ids=selected, scope_mode=mode)
                        self.app.store.control(state="running")
                        self.app.update_session_controls()
                        main = self.view(kind).controls["start"]
                        subset = self.view(kind).controls["start_selected"]
                        self.assertEqual(main.instate(["pressed"]), mode == "visible")
                        self.assertEqual(subset.instate(["pressed"]), mode == "selected")
                        self.app.store.control(stop=1)
                        self.app.update_session_controls()
                        latched = main if mode == "visible" else subset
                        self.assertIn("Dokończę", latched.cget("text"))
        finally:
            lock.close()


if __name__ == "__main__":
    unittest.main()
