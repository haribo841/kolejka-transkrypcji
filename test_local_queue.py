from __future__ import annotations

import json
from pathlib import Path
import tempfile
import time
import tkinter as tk
import unittest
from unittest.mock import patch

from batch.common import WorkerLock, atomic_json, defaults, digest
from batch.gui import Window
from batch.local_tab import folder_media


class FolderTests(unittest.TestCase):
    def test_folder_formats_subfolders_and_case(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            child = root / "podfolder"
            child.mkdir()
            for path in (root / "pierwsze.M4A", root / "drugie.mp4", root / "tekst.txt", child / "trzecie.m4a"):
                path.write_bytes(b"sample")
            self.assertEqual({p.name for p in folder_media(root, False, True)}, {"pierwsze.M4A"})
            self.assertEqual({p.name for p in folder_media(root, True, True)}, {"pierwsze.M4A", "trzecie.m4a"})
            self.assertEqual({p.name for p in folder_media(root, False, False)}, {"pierwsze.M4A", "drugie.mp4"})
            with self.assertRaises(ValueError):
                folder_media(root / "pierwsze.M4A")


class LocalTabTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        config = defaults()
        config["diarization"] = False
        atomic_json(self.root / "ustawienia.json", config)
        self.tk = tk.Tk()
        self.tk.withdraw()
        self.app = Window(self.root, tk_root=self.tk)
        self.readiness_patch = patch("batch.gui.collect_readiness", return_value={
            "components": {key: {"status": "Próba zaliczona", "message": "Offline test fixture"}
                           for key in ("whisper", "tools", "diarization", "uvr", "youtube")},
            "passed": True, "test": {"at": "test"}})
        self.readiness_patch.start()
        self.addCleanup(self.readiness_patch.stop)
        self.app.store.add_many([
            {"identity": "yt:abcdefghij0", "kind": "youtube", "title": "Film", "source": "https://youtube.com/watch?v=abcdefghij0", "date": "2020-01-01"},
            {"identity": "local:" + "a" * 64, "kind": "local", "title": "Wykład M4A", "source": str(self.root / "wyklad.m4a"), "date": "2021-01-01"},
            {"identity": "local:" + "b" * 64, "kind": "local", "title": "Drugi wykład", "source": str(self.root / "drugi.m4a"), "date": "2022-01-01"},
        ])
        self.reload()
        self.local_ids = [job["id"] for job in self.app.jobs.values() if job["kind"] == "local"]
        self.youtube_id = next(job["id"] for job in self.app.jobs.values() if job["kind"] == "youtube")

    def tearDown(self):
        self.app.close()
        self.temp.cleanup()

    def reload(self):
        self.app.jobs = {job["id"]: job for job in self.app.store.jobs()}
        self.app.render_list()
        self.app.local_tab.render()

    def wait_for_import(self):
        deadline = time.monotonic() + 5
        while self.app.busy_import and time.monotonic() < deadline:
            self.tk.update()
            time.sleep(.01)
        self.assertFalse(self.app.busy_import)

    def test_separate_tabs_lists_counts_and_tooltips(self):
        titles = [self.app.notebook.tab(tab, "text") for tab in self.app.notebook.tabs()]
        self.assertEqual(titles, ["Kolejka YouTube", "Kolejka lokalna", "Obróbka transkrypcji", "Konfiguracja"])
        self.assertEqual(set(self.app.tree.get_children()), {str(self.youtube_id)})
        self.assertEqual(set(self.app.local_tab.tree.get_children()), {str(i) for i in self.local_ids})
        self.assertIn("Lokalne: 2", self.app.local_tab.counts.get())
        for widget in self.app.local_tab.controls.values():
            self.assertTrue(widget.help_text)

    def test_start_local_and_youtube_use_different_persistent_scopes(self):
        before = self.app.store.control()
        with patch("batch.gui.preflight") as preflight, patch("batch.gui.launch") as launch:
            self.app.local_tab.start()
            launch.assert_called_once_with(self.root, kind="local", job_ids=None)
            self.assertEqual(preflight.call_args.kwargs, {"kind": "local"})
        with patch("batch.gui.preflight"), patch("batch.gui.launch") as launch:
            self.app.start()
            launch.assert_called_once_with(self.root, kind="youtube", job_ids=None)
        self.assertEqual(self.app.store.control(), before)
        self.assertTrue(all(job["enabled"] for job in self.app.store.jobs()))

    def test_selected_local_session_does_not_disable_other_jobs(self):
        self.app.local_tab.tree.selection_set(str(self.local_ids[1]))
        with patch("batch.gui.preflight"), patch("batch.gui.launch") as launch:
            self.app.local_tab.start_selected()
            launch.assert_called_once_with(self.root, kind="local", job_ids=[self.local_ids[1]])
        self.assertTrue(all(job["enabled"] for job in self.app.store.jobs()))

    def test_empty_local_selection_does_not_start(self):
        with patch("batch.local_tab.messagebox.showinfo"), patch("batch.gui.launch") as launch:
            self.app.local_tab.start_selected()
        launch.assert_not_called()

    def test_main_local_start_ignores_selection(self):
        self.app.local_tab.tree.selection_set(str(self.local_ids[1]))
        with patch("batch.gui.preflight"), patch("batch.gui.launch") as launch:
            self.app.local_tab.start()
        launch.assert_called_once_with(self.root, kind="local", job_ids=None)

    def test_idle_session_can_resume_abandoned_running_local_jobs(self):
        for ident in self.local_ids:
            self.app.store.update(ident, status="running", stage="Transkrypcja")
        with patch("batch.gui.preflight"), patch("batch.gui.launch") as launch:
            self.app.local_tab.start()
            launch.assert_called_once_with(self.root, kind="local", job_ids=None)

    def test_start_while_importing_or_processing_does_not_change_queue(self):
        before = self.app.store.control()
        self.app.busy_import = True
        with patch("batch.gui.messagebox.showinfo"), patch("batch.gui.launch") as launch:
            self.app.local_tab.start()
            launch.assert_not_called()
        self.app.busy_import = False
        lock = WorkerLock(self.root)
        self.assertTrue(lock.acquire())
        try:
            with patch("batch.gui.messagebox.showinfo"), patch("batch.gui.launch") as launch:
                self.app.local_tab.start()
                launch.assert_not_called()
        finally:
            lock.close()
        self.assertEqual(self.app.store.control(), before)

    def test_local_retry_does_not_retry_youtube(self):
        for job in self.app.jobs.values():
            self.app.store.update(job["id"], status="error", error="example")
        self.app.retry(kind="local", ids=[])
        self.assertEqual(self.app.store.get(self.youtube_id)["status"], "error")
        self.assertEqual([self.app.store.get(i)["status"] for i in self.local_ids], ["pending", "pending"])

    def test_folder_selection_persists_without_import_or_start(self):
        before = self.app.store.control()
        source = self.root / "źródła"
        source.mkdir()
        with patch("batch.local_tab.filedialog.askdirectory", return_value=str(source)), patch("batch.gui.launch") as launch:
            self.app.local_tab.choose_folder()
            launch.assert_not_called()
        state = json.loads((self.root / "lokalne-ui.json").read_text(encoding="utf-8"))
        self.assertEqual(state["folder"], str(source))
        self.assertEqual(len(self.app.store.jobs()), 3)
        self.assertEqual(before, self.app.store.control())
        self.assertNotIn("folder", json.loads((self.root / "ustawienia.json").read_text(encoding="utf-8")))

    def test_add_folder_button_imports_shows_and_selects_new_jobs_without_start(self):
        source = self.root / "nowe nagrania"
        source.mkdir()
        recording = source / "Wykład.mkv"
        recording.write_bytes(b"new local recording")
        tab = self.app.local_tab
        tab.search.set("niepasujący tekst")
        tab.status.set("Gotowe")
        tab.render()
        self.assertFalse(tab.tree.get_children())
        # An import must update the table itself, without the periodic refresh.
        self.tk.after_cancel(self.app.refresh_after)
        self.app.refresh_after = None
        before = self.app.store.control()
        with patch("batch.local_tab.filedialog.askdirectory", return_value=str(source)), \
                patch("batch.media.media_metadata", return_value={"duration": 30, "audio_tracks": []}), \
                patch("batch.gui.launch") as launch:
            tab.controls["browse"].invoke()
            self.wait_for_import()
            launch.assert_not_called()
        job = next(job for job in self.app.store.jobs() if job["source"] == str(recording.resolve()))
        self.assertEqual(tab.search.get(), "")
        self.assertEqual(tab.status.get(), "Wszystkie")
        self.assertIn(str(job["id"]), tab.tree.get_children())
        self.assertEqual(tab.tree.selection(), (str(job["id"]),))
        self.assertEqual(tab.tree.item(str(job["id"]), "values")[4], "00:00:30")
        self.assertIn("Znaleziono: 1 | Dodano: 1", tab.note.get())
        self.assertIn("Start pozostaje ręczny", tab.note.get())
        self.assertEqual(self.app.store.control(), before)

    def test_cancel_add_folder_keeps_current_folder_queue_and_filters(self):
        tab = self.app.local_tab
        tab.set_folder(self.root)
        tab.search.set("Wykład")
        tab.status.set("Oczekuje")
        before = self.app.store.jobs()
        with patch("batch.local_tab.filedialog.askdirectory", return_value=""), \
                patch("batch.local_tab.import_local") as importer:
            tab.controls["browse"].invoke()
            importer.assert_not_called()
        self.assertEqual(tab.folder.get(), str(self.root))
        self.assertEqual(tab.search.get(), "Wykład")
        self.assertEqual(tab.status.get(), "Oczekuje")
        self.assertEqual(self.app.store.jobs(), before)

    def test_technical_scan_switch_persists_and_applies_to_next_import(self):
        source = self.root / "pakiet" / "site-packages"
        source.mkdir(parents=True)
        recording = source / "example.wav"
        recording.write_bytes(b"technical fixture")
        tab = self.app.local_tab
        tab.folder.set(str(source.parent))
        with patch("batch.media.media_metadata", return_value={"duration": 1, "audio_tracks": []}), \
                patch("batch.gui.launch") as launch:
            tab.add_folder()
            self.wait_for_import()
            self.assertEqual(len(self.app.store.jobs()), 3)
            tab.include_technical.set(True)
            tab.save_view()
            self.assertEqual(len(self.app.store.jobs()), 3)
            tab.add_folder()
            self.wait_for_import()
            launch.assert_not_called()
        self.assertEqual(len(self.app.store.jobs()), 4)
        state = json.loads((self.root / "lokalne-ui.json").read_text(encoding="utf-8"))
        self.assertTrue(state["include_technical"])

    def test_output_required_details_offer_the_existing_fix_without_start(self):
        job = self.app.store.get(self.local_ids[0])
        metadata = json.loads(job["source_meta"])
        metadata.update(output_error="Ścieżka wyników jest zbyt długa", intended_output_root="C:/very/deep")
        with self.app.store.connect() as db:
            db.execute("UPDATE jobs SET status='output_required',source_meta=?,error_code='output_path' WHERE id=?",
                       (json.dumps(metadata), job["id"]))
        self.app.jobs = {row["id"]: row for row in self.app.store.jobs()}
        tab = self.app.local_tab
        tab.render()
        tab.tree.selection_set(str(job["id"]))
        tab.show_detail()
        self.assertIn("Wymagany krótszy folder", tab.detail.get())
        self.assertIn("Zmień miejsce wyników", tab.detail.get())
        self.assertFalse(tab.eligible(self.app.jobs[job["id"]]))
        tab.output_mode.set("Folder centralny")
        with patch("batch.gui.launch") as launch:
            tab.change_output()
            launch.assert_not_called()
        self.assertEqual(self.app.store.get(job["id"])["status"], "pending")

    def test_empty_folder_import_explains_filters_and_rescan_uses_typed_path(self):
        source = self.root / "filmy"
        source.mkdir()
        (source / "Film.avi").write_bytes(b"video fixture")
        tab = self.app.local_tab
        tab.folder.set(str(source))
        tab.m4a_only.set(True)
        tab.recursive.set(False)
        with patch("batch.local_tab.filedialog.askdirectory") as chooser, \
                patch("batch.gui.launch") as launch:
            tab.controls["import_folder"].invoke()
            self.wait_for_import()
            chooser.assert_not_called()
            launch.assert_not_called()
        self.assertIn("Znaleziono: 0", tab.note.get())
        self.assertIn("tylko M4A, bez podfolderów", tab.note.get())
        self.assertIn("Sprawdź folder oraz opcje", tab.note.get())
        self.assertEqual(len(self.app.store.jobs()), 3)
        tab.m4a_only.set(False)
        with patch("batch.media.media_metadata", return_value={"duration": 12, "audio_tracks": []}), \
                patch("batch.gui.launch") as launch:
            tab.controls["import_folder"].invoke()
            self.wait_for_import()
            tab.controls["import_folder"].invoke()
            self.wait_for_import()
            launch.assert_not_called()
        self.assertEqual(len(self.app.store.jobs()), 4)
        self.assertIn("Dodano: 0", tab.note.get())
        self.assertIn("Kopie lub już w kolejce: 1", tab.note.get())
        matched = next(job for job in self.app.store.jobs() if job["source"] == str((source / "Film.avi").resolve()))
        self.assertEqual(tab.tree.selection(), (str(matched["id"]),))
        self.assertIn("Zaznaczone pozycje: Oczekuje: 1", tab.note.get())

    def test_reimport_copy_reveals_original_completed_job_without_resetting_progress(self):
        source = self.root / "pierwszy"
        source.mkdir()
        original = source / "Film.mkv"
        original.write_bytes(b"the same recording")
        tab = self.app.local_tab
        tab.set_folder(source)
        with patch("batch.media.media_metadata", return_value={"duration": 10, "audio_tracks": []}):
            tab.add_folder()
            self.wait_for_import()
        job = next(job for job in self.app.store.jobs() if job["source"] == str(original.resolve()))
        self.app.store.update(job["id"], status="done", progress=100, stage="Zapisane wyniki")
        duplicate_folder = self.root / "kopia"
        duplicate_folder.mkdir()
        duplicate = duplicate_folder / "Inna nazwa.mkv"
        duplicate.write_bytes(original.read_bytes())
        tab.set_folder(duplicate_folder)
        tab.search.set("Inna nazwa")
        tab.status.set("Oczekuje")
        with patch("batch.media.media_metadata", return_value={"duration": 10, "audio_tracks": []}), \
                patch("batch.gui.launch") as launch:
            tab.add_folder()
            self.wait_for_import()
            launch.assert_not_called()
        self.assertEqual(len(self.app.store.jobs()), 4)
        self.assertEqual(tab.tree.selection(), (str(job["id"]),))
        current = self.app.store.get(job["id"])
        self.assertEqual((current["status"], current["progress"], current["stage"]), ("done", 100, "Zapisane wyniki"))
        self.assertEqual(current["source"], str(original.resolve()))
        self.assertIn("Dodano: 0", tab.note.get())
        self.assertIn("Zaznaczone pozycje: Gotowe: 1", tab.note.get())

    def test_regular_refresh_preserves_local_filters(self):
        tab = self.app.local_tab
        tab.search.set("Wykład")
        tab.status.set("Gotowe")
        tab.refresh(self.app.store.control(), False)
        self.assertEqual(tab.search.get(), "Wykład")
        self.assertEqual(tab.status.get(), "Gotowe")

    def test_real_background_import_deduplicates_m4a_and_preserves_sources(self):
        folder = self.root / "nagrania"
        folder.mkdir()
        first, copy = folder / "CPS1.m4a", folder / "Pełny tytuł.m4a"
        first.write_bytes(b"same audio content")
        copy.write_bytes(first.read_bytes())
        (folder / "istniejąca transkrypcja.txt").write_text("original text", encoding="utf-8")
        before = {path.name: digest(path) for path in folder.iterdir()}
        control = self.app.store.control()
        youtube_note = self.app.source_note.get()
        self.app.local_tab.set_folder(folder)
        with patch("batch.gui.launch") as launch:
            self.app.local_tab.add_folder()
            deadline = time.monotonic() + 5
            while self.app.busy_import and time.monotonic() < deadline:
                self.tk.update()
                time.sleep(.01)
            launch.assert_not_called()
        self.assertFalse(self.app.busy_import)
        self.assertEqual(len(self.app.store.jobs()), 4)
        self.assertEqual(self.app.store.control(), control)
        self.assertEqual(self.app.source_note.get(), youtube_note)
        self.assertIn("Dodano: 1", self.app.local_tab.note.get())
        self.assertIn("Kopie lub już w kolejce: 1", self.app.local_tab.note.get())
        self.assertEqual({path.name: digest(path) for path in folder.iterdir()}, before)
        self.app.local_tab.add_folder()
        deadline = time.monotonic() + 5
        while self.app.busy_import and time.monotonic() < deadline:
            self.tk.update()
            time.sleep(.01)
        self.assertEqual(len(self.app.store.jobs()), 4)
        self.assertIn("Dodano: 0", self.app.local_tab.note.get())

    def test_transcript_processing_remembers_selection_from_local_tab(self):
        self.app.notebook.select(self.app.local_page)
        self.app.queue_changed()
        self.app.local_tab.tree.selection_set(str(self.local_ids[0]))
        self.app.notebook.select(self.app.processing_page)
        self.app.queue_changed()
        self.assertEqual(self.app.selected(), [self.local_ids[0]])
        self.app.notebook.select(self.app.queue_page)
        self.app.tree.selection_set(str(self.youtube_id))
        self.assertEqual(self.app.selected(), [self.youtube_id])

    def test_local_stop_does_not_stop_an_active_youtube_session(self):
        self.app.store.control(stop=0, scope_kind="youtube")
        lock = WorkerLock(self.root)
        self.assertTrue(lock.acquire())
        try:
            with patch("batch.gui.messagebox.showinfo"):
                self.app.stop(kind="local")
            self.assertEqual(self.app.store.control()["stop"], 0)
            self.app.stop(kind="youtube")
            self.assertEqual(self.app.store.control()["stop"], 1)
        finally:
            lock.close()


if __name__ == "__main__":
    unittest.main()
