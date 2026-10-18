"""Start checks stay off the Tk thread and keep the requested scope frozen."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import threading
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

from batch.common import WorkerLock, atomic_json, defaults
from batch.gui import Window
from test_support import wait_for_start


READY = {"passed": True, "test": {}, "components": {
    key: {"status": "Gotowy do próby", "message": "Offline fixture"}
    for key in ("whisper", "tools", "diarization", "uvr", "youtube")}}


class StartResponsivenessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        config = defaults()
        config.update(uvr_enabled=False, diarization=False)
        atomic_json(self.root / "ustawienia.json", config)
        self.entered, self.release = threading.Event(), threading.Event()
        self.check_thread_ids = []
        self.patches = [patch("batch.gui.QueueWindow.request_readiness"),
                        patch("batch.gui.QueueWindow.enrich_metadata"),
                        patch("batch.gui.collect_readiness", return_value=READY),
                        patch("batch.gui.preflight"), patch("batch.gui.check_destination")]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        self.tk = tk.Tk()
        self.tk.withdraw()
        self.app = Window(self.root, tk_root=self.tk)
        self.app.readiness = READY
        self.app.store.add_many([
            {"identity": "local:" + "a" * 64, "kind": "local", "title": "Alpha lecture",
             "source": str(self.root / "alpha.mkv"), "date": "2020-01-01", "duration": 20},
            {"identity": "local:" + "b" * 64, "kind": "local", "title": "Beta lecture",
             "source": str(self.root / "beta.avi"), "date": "2020-01-02", "duration": 10},
            {"identity": "local:" + "c" * 64, "kind": "local", "title": "Other recording",
             "source": str(self.root / "other.mp4"), "date": "2020-01-03", "duration": 30}])
        self.app.jobs = {job["id"]: job for job in self.app.store.jobs()}
        self.ids = [job["id"] for job in self.app.store.jobs()]
        self.app.local_tab.render()

    def tearDown(self):
        self.release.set()
        if self.app.start_check_thread is not None:
            self.app.start_check_thread.join(5)
        if not self.app.closing:
            self.app.close()
        self.temp.cleanup()

    def blocked_check(self, *_args, **_kwargs):
        self.check_thread_ids.append(threading.get_ident())
        self.entered.set()
        if not self.release.wait(5):
            raise RuntimeError("Test did not release the configuration check")
        return READY

    def launch_process(self):
        process = Mock()
        process.poll.return_value = 0
        return patch("batch.gui.launch", return_value=process)

    def filter_local(self, text):
        tab = self.app.local_tab
        tab.search.set(text)
        if tab.filter_after is not None:
            tab.parent.after_cancel(tab.filter_after)
        tab.filter_now()

    def test_slow_environment_and_destination_checks_leave_tk_responsive(self):
        for target in ("collect_readiness", "preflight", "check_destination"):
            with self.subTest(check=target):
                self.entered.clear()
                self.release.clear()
                heartbeat = []
                with patch("batch.gui." + target, side_effect=self.blocked_check), self.launch_process() as launch:
                    self.app.local_tab.start()
                    self.assertTrue(self.entered.wait(2))
                    self.tk.after(0, lambda: heartbeat.append(threading.get_ident()))
                    self.tk.update()
                    self.assertEqual(heartbeat, [threading.get_ident()])
                    self.assertEqual(self.app.start_pending, "local")
                    self.assertTrue(self.app.local_tab.controls["start"].instate(["pressed", "disabled"]))
                    launch.assert_not_called()
                    self.app.start(kind="local")
                    launch.assert_not_called()
                    self.release.set()
                    wait_for_start(self.app)
                    launch.assert_called_once()
                    self.assertIsNone(self.app.start_pending)
        self.assertTrue(all(ident != threading.get_ident() for ident in self.check_thread_ids))

    def test_view_changes_and_new_import_do_not_change_pending_start_order(self):
        tab = self.app.local_tab
        self.filter_local("lecture")
        tab.tree.tk.call(tab.tree.heading("duration", "command"))
        expected = [self.ids[1], self.ids[0]]
        with patch("batch.gui.preflight", side_effect=self.blocked_check), self.launch_process() as launch:
            tab.start()
            self.assertTrue(self.entered.wait(2))
            self.filter_local("Other")
            tab.tree.tk.call(tab.tree.heading("title", "command"))
            self.app.store.add_many([{"identity": "local:" + "d" * 64, "kind": "local", "title": "New lecture",
                                     "source": str(self.root / "new.mkv"), "date": "2019-01-01"}])
            self.release.set()
            wait_for_start(self.app)
            launch.assert_called_once_with(self.root, kind="local", job_ids=expected,
                                           order_ids=expected, scope_mode="visible")

    def test_successful_start_replaces_checking_note_in_both_queues(self):
        self.app.store.add_many([{"identity": "youtube:abcdefghijk", "kind": "youtube", "title": "Online lecture",
                                 "source": "https://www.youtube.com/watch?v=abcdefghijk", "date": "2020-01-01"}])
        for kind, note, count in (("local", self.app.local_tab.note, 3), ("youtube", self.app.source_note, 1)):
            with self.subTest(queue=kind), self.launch_process() as launch:
                self.app.start(kind=kind)
                wait_for_start(self.app)
                launch.assert_called_once()
                self.assertEqual(note.get(), f"Kontrola konfiguracji i folderu wyników zakończona. Zakres sesji: {count} nagrań.")
                self.app.update_session_controls()
                self.assertNotIn("Sprawdzanie", note.get())

    def test_selected_snapshot_is_unchanged_when_selection_changes_during_check(self):
        tab = self.app.local_tab
        tab.tree.selection_set(str(self.ids[1]))
        with patch("batch.gui.preflight", side_effect=self.blocked_check), self.launch_process() as launch:
            tab.start_selected()
            self.assertTrue(self.entered.wait(2))
            tab.tree.selection_set(str(self.ids[0]))
            self.release.set()
            wait_for_start(self.app)
            launch.assert_called_once_with(self.root, kind="local", job_ids=[self.ids[1]],
                                           order_ids=[self.ids[1]], scope_mode="selected")

    def test_failed_background_check_restores_controls_and_records_the_reason(self):
        before = self.app.store.control()
        with patch("batch.gui.check_destination", side_effect=OSError("Destination is unavailable")), \
                patch("batch.gui.messagebox.showerror") as popup, self.launch_process() as launch:
            self.app.local_tab.start()
            wait_for_start(self.app)
            launch.assert_not_called()
            popup.assert_called_once()
        self.assertIsNone(self.app.start_pending)
        self.assertTrue(self.app.local_tab.controls["start"].instate(["!pressed", "!disabled"]))
        self.assertEqual(self.app.store.control(), before)
        saved = json.loads((self.root / "ostatni-blad-start.json").read_text(encoding="utf-8"))
        self.assertIn("Destination is unavailable", saved["message"])

    def test_changed_settings_require_a_new_start(self):
        with patch("batch.gui.preflight", side_effect=self.blocked_check), \
                patch("batch.gui.messagebox.showerror") as popup, self.launch_process() as launch:
            self.app.local_tab.start()
            self.assertTrue(self.entered.wait(2))
            config = defaults()
            config.update(uvr_enabled=True, diarization=False)
            atomic_json(self.root / "ustawienia.json", config)
            self.release.set()
            wait_for_start(self.app)
            launch.assert_not_called()
            popup.assert_called_once()
        self.assertIsNone(self.app.start_pending)
        self.assertIn("Ustawienia zmieniły się", self.app.local_tab.note.get())

    def test_closing_window_while_checking_never_launches_a_worker(self):
        with patch("batch.gui.preflight", side_effect=self.blocked_check), self.launch_process() as launch:
            self.app.local_tab.start()
            self.assertTrue(self.entered.wait(2))
            self.app.close()
            self.release.set()
            wait_for_start(self.app)
            launch.assert_not_called()
        self.assertTrue(self.app.closing)
        self.assertEqual(self.app.store.control()["state"], "idle")

    def test_pending_start_blocks_model_setup_uvr_setup_and_sample_trial(self):
        with patch("batch.gui.preflight", side_effect=self.blocked_check), \
                patch("batch.gui.messagebox.showinfo"), patch("batch.gui.subprocess.Popen") as process, \
                patch("batch.gui.filedialog.askopenfilename") as choose_sample, self.launch_process() as launch:
            self.app.local_tab.start()
            self.assertTrue(self.entered.wait(2))
            self.assertFalse(self.app.setup_process({"test_only": True}))
            self.app.setup_uvr()
            self.app.test_local()
            process.assert_not_called()
            choose_sample.assert_not_called()
            launch.assert_not_called()
            self.assertEqual(self.app.start_pending, "local")
            self.assertFalse((self.root / "konfiguracja-modelu.json").exists())
            self.assertFalse((self.root / "konfiguracja-uvr.log").exists())
            self.release.set()
            wait_for_start(self.app)
            launch.assert_called_once()

    def test_configuration_lock_acquired_during_check_prevents_launch(self):
        before = self.app.store.control()
        lock = WorkerLock(self.root / "konfiguracja")
        with patch("batch.gui.preflight", side_effect=self.blocked_check), \
                patch("batch.gui.messagebox.showerror") as popup, self.launch_process() as launch:
            self.app.local_tab.start()
            self.assertTrue(self.entered.wait(2))
            self.assertTrue(lock.acquire())
            try:
                self.release.set()
                wait_for_start(self.app)
                launch.assert_not_called()
                popup.assert_called_once()
                self.assertIsNone(self.app.start_pending)
                self.assertTrue(self.app.local_tab.controls["start"].instate(["!pressed"]))
                self.assertIn("Trwa próba lub konfiguracja modeli", self.app.local_tab.note.get())
            finally:
                lock.close()
        self.app.update_session_controls()
        self.assertTrue(self.app.local_tab.controls["start"].instate(["!pressed", "!disabled"]))
        self.assertEqual(self.app.store.control(), before)


if __name__ == "__main__":
    unittest.main()
