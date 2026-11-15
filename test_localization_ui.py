"""Localization UI uses explicit reviewed state without automatically starting a worker."""
import time
import threading
import tkinter as tk
from unittest.mock import patch

from batch.localization.tab import LocalizationTab
from test_localization import IDENT, LocalizationCase


class TabTests(LocalizationCase):
    def test_full_catalog_selection_is_read_once_and_keeps_table_order(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        ids = [f"{i:011d}" for i in range(5035)]
        tree = Mock()
        tree.selection.return_value = tuple(reversed(ids))
        tab = SimpleNamespace(tree=tree, visible=ids)
        self.assertEqual(LocalizationTab.selected(tab), ids)
        tree.selection.assert_called_once_with()
        tree.get_children.assert_not_called()

    def test_idle_poll_reads_selection_once_without_repainting_controls(self):
        self.load_selected()
        self.tab.next_snapshot = time.monotonic() + 100
        with patch.object(self.tab, "selected", wraps=self.tab.selected) as selected:
            self.tab.start_next_action()
        selected.assert_called_once_with()
        self.tab.update_action_controls()
        with patch.object(self.tab.sync_button, "configure", wraps=self.tab.sync_button.configure) as configure:
            self.tab.update_action_controls()
            self.tab.update_action_controls()
        configure.assert_not_called()

    def test_completed_refresh_replaces_the_started_message(self):
        self.tab.worker_busy = True
        self.tab.note.set("Odświeżanie przez API rozpoczęte.")
        self.store.session(mode="sync", state="idle", message="Odświeżanie zakończone. Pobrano dane.")
        self.tab.received(self.tab.snapshot())
        self.assertEqual(self.tab.note.get(), "Odświeżanie zakończone. Pobrano dane.")

    def wait_until(self, condition, timeout=4):
        deadline = time.monotonic() + timeout
        while not condition() and time.monotonic() < deadline:
            self.window.update()
            time.sleep(.01)
        self.assertTrue(condition(), "Asynchronous UI operation did not finish")

    def test_refresh_queued_during_a_background_read_runs_once_with_its_original_ids(self):
        self.load_selected()
        started, release = threading.Event(), threading.Event()
        def read():
            started.set()
            release.wait(3)
        self.tab.perform(read, background=True)
        self.assertTrue(started.wait(1))
        with patch("batch.localization.tab.launch") as launch:
            self.tab.sync()
            self.assertIsNotNone(self.tab.queued_action)
            self.assertIn("disabled", self.tab.sync_button.state())
            self.assertIn("pressed", self.tab.sync_button.state())
            self.assertIn("Polecenie przyjęte", self.tab.note.get())
            self.tab.sync()
            self.tab.tree.selection_remove(IDENT)
            release.set()
            self.wait_until(lambda: launch.call_count == 1)
            self.wait_until(lambda: not self.tab.pending)
        launch.assert_called_once_with(self.root, "public_sync", [IDENT])

    def test_background_refresh_does_not_overwrite_an_action_message(self):
        self.tab.note.set("Odświeżanie rozpoczęte dla zaznaczonych filmów.")
        self.tab.received(self.tab.snapshot())
        self.assertEqual(self.tab.note.get(), "Odświeżanie rozpoczęte dla zaznaczonych filmów.")
        self.assertIn("Filmy:", self.tab.summary.get())

    def test_refresh_button_stays_busy_for_the_detached_sync_session(self):
        self.store.session(mode="sync", state="running")
        with patch("batch.localization.tab.WorkerLock.busy", return_value=True):
            self.tab.received(self.tab.snapshot())
        self.assertIn("disabled", self.tab.sync_button.state())
        self.assertIn("pressed", self.tab.sync_button.state())
        self.assertEqual(self.tab.sync_button.cget("text"), "Odświeżanie…")
        self.store.session(state="idle")
        self.tab.received(self.tab.snapshot())
        self.assertNotIn("disabled", self.tab.sync_button.state())
        self.assertNotIn("pressed", self.tab.sync_button.state())

    def test_repeated_login_click_cancels_the_wait_and_retries_in_the_same_wizard(self):
        from batch.localization.auth import ConnectionStatus, LoginCancelled
        from batch.localization.wizard import LocalizationWizard
        attempts = []
        def connect(_root, *, cancel):
            attempts.append(cancel)
            if len(attempts) == 1:
                cancel.wait(3)
                raise LoginCancelled("Cancelled old attempt")
        with patch("batch.localization.wizard.connection_status", return_value=ConnectionStatus(False, 1, "Test")), patch("batch.localization.wizard.connect", side_effect=connect):
            wizard = LocalizationWizard(self.tab.app)
            try:
                wizard.action_button.invoke()
                self.wait_until(lambda: len(attempts) == 1)
                self.assertEqual(wizard.action_button.cget("text"), "Ponów logowanie")
                wizard.action_button.invoke()
                self.wait_until(lambda: len(attempts) == 2 and not wizard.busy)
                self.assertTrue(attempts[0].is_set())
                self.assertFalse(attempts[1].is_set())
                self.assertTrue(wizard.complete)
                self.assertTrue(wizard.dialog.winfo_exists())
            finally:
                wizard.close()

    def test_login_cancel_and_close_release_the_wait_without_recreating_the_wizard(self):
        from batch.localization.auth import ConnectionStatus, LoginCancelled
        from batch.localization.wizard import LocalizationWizard
        attempts, stopped = [], threading.Event()
        def connect(_root, *, cancel):
            attempts.append(cancel)
            cancel.wait(3)
            stopped.set()
            raise LoginCancelled("Cancelled")
        with patch("batch.localization.wizard.connection_status", return_value=ConnectionStatus(False, 1, "Test")), patch("batch.localization.wizard.connect", side_effect=connect):
            wizard = LocalizationWizard(self.tab.app)
            try:
                wizard.action_button.invoke()
                self.wait_until(lambda: len(attempts) == 1)
                wizard.cancel_button.invoke()
                self.wait_until(lambda: not wizard.busy)
                self.assertFalse(wizard.complete)
                stopped.clear()
                wizard.action_button.invoke()
                self.wait_until(lambda: len(attempts) == 2)
            finally:
                wizard.close()
            self.assertTrue(stopped.wait(1))
            self.assertTrue(attempts[-1].is_set())

    def load_selected(self):
        self.tab.tree.selection_set(IDENT)
        self.tab.load_details((IDENT, self.store.get_video(IDENT), self.store.items(IDENT)))

    def test_same_selected_row_refreshes_new_metadata_and_prepared_text(self):
        from test_localization import remote
        self.load_selected()
        self.store.snapshot(remote(title="Updated source"))
        self.store.save_item(IDENT, "title", "pl", "Nowy tytuł")
        self.tab.received(self.tab.snapshot())
        self.assertIn("Updated source", self.tab.original.get())
        self.assertEqual(self.tab.metadata["pl"][0].get(), "Nowy tytuł")

    def test_refresh_preserves_unsaved_correction_and_does_not_save_stale_fields(self):
        self.load_selected()
        self.tab.metadata["pl"][0].set("Moja korekta")
        self.store.save_item(IDENT, "description", "en", "New prepared description")
        self.tab.received(self.tab.snapshot())
        self.assertEqual(self.tab.metadata["pl"][0].get(), "Moja korekta")
        self.assertTrue(self.tab.save_fields())
        self.assertEqual(self.store.item(IDENT, "description", "en")["value"], "New prepared description")
        self.assertEqual(self.store.item(IDENT, "title", "pl")["value"], "Moja korekta")

    def test_conflicting_edit_requires_explicit_choice_and_keeps_both_versions(self):
        self.load_selected()
        self.tab.metadata["pl"][0].set("Moja korekta")
        self.store.save_item(IDENT, "title", "pl", "Prepared in background")
        self.tab.received(self.tab.snapshot())
        self.assertFalse(self.tab.save_fields())
        self.assertEqual(self.tab.metadata["pl"][0].get(), "Moja korekta")
        self.assertEqual(self.store.item(IDENT, "title", "pl")["value"], "Prepared in background")
        with patch("batch.localization.tab.messagebox.askyesno", return_value=True):
            self.assertTrue(self.tab.save_fields(confirm_conflict=True))
        self.assertEqual(self.store.item(IDENT, "title", "pl")["value"], "Moja korekta")

    def test_without_google_sync_reads_selected_public_metadata_and_disables_publication(self):
        self.load_selected()
        self.tab.received(self.tab.snapshot())
        self.assertIn("disabled", self.tab.controls["publish"].state())
        with patch.object(self.tab, "perform") as perform:
            self.tab.sync()
        with patch("batch.localization.tab.launch") as launch:
            perform.call_args.args[0]()
        launch.assert_called_once_with(self.root, "public_sync", [IDENT])

    def test_missing_google_does_not_start_full_catalog_sync(self):
        with patch.object(self.tab, "perform") as perform:
            self.tab.sync()
        perform.assert_not_called()
        self.assertIn("Zaznacz filmy", self.tab.note.get())

    def test_wizard_checks_real_metadata_separately_from_the_translation_trial(self):
        from batch.localization.wizard import LocalizationWizard
        wizard = LocalizationWizard(self.tab.app, selected_id=IDENT)
        try:
            wizard.step = 6
            wizard.draw()
            self.assertIn("bez publikacji", wizard.next.cget("text"))
            self.assertIn("nie jest skonfigurowana", wizard.message.get())
            self.assertIn("Original description", wizard.metadata_preview.get("1.0", "end-1c"))
        finally:
            wizard.close()

    def test_cached_public_preview_does_not_confirm_a_new_google_connection(self):
        from batch.localization.auth import ConnectionStatus
        from batch.localization.wizard import LocalizationWizard
        with self.store.base.connect() as db:
            db.execute("UPDATE ytl_videos SET snapshot='{}' WHERE video_id=?", (IDENT,))
        self.store.public_metadata({"id": IDENT, "source": "public", "snippet": {"title": "Public title", "description": "Public description"}})
        with patch("batch.localization.wizard.connection_status", return_value=ConnectionStatus(True, 6, "Configured")):
            wizard = LocalizationWizard(self.tab.app, selected_id=IDENT)
            try:
                wizard.step = 6
                wizard.draw()
                self.assertFalse(wizard.complete)
                self.assertIn("disabled", wizard.next.state())
                self.assertIn("nie potwierdzają odczytu przez API", wizard.message.get())
            finally:
                wizard.close()

    def setUp(self):
        super().setUp()
        self.window = tk.Tk()
        self.window.withdraw()
        from tkinter import ttk
        from types import SimpleNamespace
        notebook = ttk.Notebook(self.window)
        page = ttk.Frame(notebook)
        notebook.add(page, text="Lokalizacje")
        notebook.pack(fill="both", expand=True)
        app = SimpleNamespace(root=self.root, window=self.window, notebook=notebook, jobs={}, help_status=tk.StringVar(master=self.window))
        self.tab = LocalizationTab(app, page)
        deadline = time.monotonic() + 5
        while not self.tab.rows and time.monotonic() < deadline:
            self.window.update()
            time.sleep(.01)
        self.assertEqual(len(self.tab.rows), 1)

    def tearDown(self):
        self.tab.close()
        self.window.destroy()
        super().tearDown()

    def test_initialization_does_not_launch_preparation_or_publication(self):
        self.assertEqual(self.store.session()["state"], "idle")
        self.assertIn("Odłożone", str(self.tab.tree.item(IDENT, "values")))

    def test_filter_sort_and_refresh_preserve_selection_and_three_click_cycle(self):
        self.tab.tree.selection_set(IDENT)
        self.tab.sort("title")
        self.assertEqual(self.tab.sort_direction, "asc")
        self.tab.sort("title")
        self.assertEqual(self.tab.sort_direction, "desc")
        self.tab.sort("title")
        self.assertIsNone(self.tab.sort_direction)
        self.tab.render()
        self.assertEqual(self.tab.tree.selection(), (IDENT,))
        self.tab.search.set("does not exist")
        self.assertEqual(self.tab.tree.get_children(), ())

    def test_double_click_header_does_not_open_results(self):
        from types import SimpleNamespace
        with patch.object(self.tab.tree, "identify_region", return_value="heading"), patch.object(self.tab, "open_files") as opened:
            self.tab.open_row(SimpleNamespace(x=20, y=10))
            opened.assert_not_called()
        with patch.object(self.tab.tree, "identify_region", return_value="cell"), patch.object(self.tab.tree, "identify_row", return_value=IDENT), patch.object(self.tab, "open_files") as opened:
            self.tab.open_row(SimpleNamespace(x=20, y=40))
            opened.assert_called_once_with(IDENT)

    def test_no_selection_cannot_start_a_worker(self):
        with patch("batch.localization.tab.launch") as launch:
            self.tab.prepare()
            launch.assert_not_called()
        self.assertIn("Zaznacz", self.tab.note.get())

    def test_wizard_is_visible_and_next_waits_for_a_completed_step(self):
        from batch.localization.wizard import LocalizationWizard
        wizard = LocalizationWizard(self.tab.app)
        try:
            self.window.update_idletasks()
            self.assertGreaterEqual(wizard.dialog.winfo_x(), 0)
            self.assertGreaterEqual(wizard.dialog.winfo_y(), 0)
            self.assertIn("disabled", wizard.next.state())
            wizard.skip_account()
            self.assertEqual(wizard.step, 3)
        finally:
            wizard.close()
