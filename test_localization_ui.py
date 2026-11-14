"""Localization UI uses explicit reviewed state without automatically starting a worker."""
import time
import tkinter as tk
from unittest.mock import patch

from batch.localization.tab import LocalizationTab
from test_localization import IDENT, LocalizationCase


class TabTests(LocalizationCase):
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
