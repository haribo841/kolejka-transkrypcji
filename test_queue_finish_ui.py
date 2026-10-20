"""Terminal outcomes, technical exclusions and bounded GUI database reads."""
import json
import io
from pathlib import Path
import sqlite3
import tempfile
import time
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

from batch.common import digest, read_json
from batch.gui import QueueWindow
from batch.importers import import_local
from batch.readiness import AVAILABLE, OFF
from batch.store import Store
from batch.worker import Worker


class QueueFinishTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.store = Store(self.root / "queue")

    def test_technical_file_requires_explicit_import_option(self):
        source = self.root / "site-packages" / "test.wav"
        source.parent.mkdir()
        source.write_bytes(b"technical fixture")
        result = import_local(self.store, [source])
        self.assertEqual(self.store.jobs(), [])
        self.assertEqual(result["technical_files"], 1)
        import_local(self.store, [source], include_technical=True)
        self.assertEqual(len(self.store.jobs()), 1)

    def test_heartbeat_retries_after_sqlite_lock(self):
        worker = Worker(self.store.root)
        stopped = Mock()
        stopped.wait.side_effect = [False, False, True]
        with patch.object(worker.store, "control", side_effect=[sqlite3.OperationalError("database is locked"), {}]) as update:
            worker.heartbeat(stopped)
        self.assertEqual(update.call_count, 2)

    def test_whisper_download_action_uses_configured_directory_without_queue_start(self):
        from batch.setup_model import main
        with patch("batch.setup_model.sys.argv", ["setup", str(self.store.root)]), \
                patch("batch.setup_model.sys.stdin", io.StringIO('{"download_whisper":true}\n')), \
                patch("transcribe.download") as download, patch("batch.setup_model.Worker") as worker:
            self.assertEqual(main(), 0)
        self.assertIsInstance(download.call_args.args[0].model, Path)
        self.assertEqual(read_json(self.store.root / "konfiguracja-modelu.json")["phase"], "whisper_complete")
        worker.assert_not_called()
        self.assertEqual(self.store.jobs(), [])

    def test_terminal_skip_does_not_reenter_retry_or_start_and_keeps_source(self):
        source = self.root / "bad.wav"
        source.write_bytes(b"invalid")
        self.store.add_many([{"identity": "local:" + digest(source), "kind": "local", "title": "Bad", "source": str(source)}])
        job = self.store.jobs()[0]
        self.store.record_failure(job["id"], "Old diagnostic", "old")
        self.store.skip(job["id"], "Not audio", "technical_media")
        self.store.retry()
        self.store.start(kind="local")
        self.assertIsNone(self.store.claim())
        skipped = self.store.get(job["id"])
        self.assertEqual(skipped["status"], "skipped")
        self.assertEqual(len(json.loads(skipped["error_history"])), 2)
        self.assertEqual(source.read_bytes(), b"invalid")

    def test_real_sqlite_exclusive_lock_is_bounded_and_recovers(self):
        connection = sqlite3.connect(self.store.path)
        self.addCleanup(connection.close)
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("BEGIN EXCLUSIVE")
        start = time.monotonic()
        with self.assertRaises(sqlite3.OperationalError):
            self.store.snapshot(timeout=.03)
        self.assertLess(time.monotonic() - start, .5)
        connection.rollback()
        rows, control = self.store.snapshot()
        self.assertEqual(rows, [])
        self.assertEqual(control["state"], "idle")

    def test_permanent_probe_failure_is_skipped_but_access_errors_remain_retryable(self):
        for name, error, expected in (("corrupt", RuntimeError("FFprobe: Invalid data found when processing input"), "skipped"),
                                       ("denied", PermissionError("Permission denied"), "error")):
            source = self.root / (name + ".wav")
            source.write_bytes(name.encode())
            with patch("batch.media.media_metadata", side_effect=error):
                import_local(self.store, [source], config={})
            self.assertEqual(self.store.jobs()[-1]["status"], expected)


class QueueGuidanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.tk = tk.Tk()
        self.tk.withdraw()
        with patch.object(QueueWindow, "request_readiness"), patch.object(QueueWindow, "enrich_metadata"):
            self.app = QueueWindow(self.root, tk_root=self.tk, start_page="local")
        self.addCleanup(self.app.close)

    def test_refresh_lock_keeps_rows_and_schedules_retry_without_blocking_tk(self):
        old = self.app.jobs.copy()
        with patch.object(self.app.store, "snapshot", side_effect=sqlite3.OperationalError("database is locked")):
            started = time.monotonic()
            self.app.refresh()
            self.assertLess(time.monotonic() - started, .5)
            self.assertEqual(self.app.jobs, old)
            fired = []
            self.tk.after(1, lambda: fired.append(True))
            self.tk.update()
            time.sleep(.01)
            self.tk.update()
            self.assertTrue(fired)
            self.assertTrue(self.app.refresh_after)
            self.assertIn("Baza jest zajęta", self.app.current.get())

    def test_technical_and_skipped_hidden_by_default_but_audit_filter_available(self):
        self.app.store.add_many([{"identity": "local:fixture", "kind": "local", "title": "Technical", "source": "offline",
                                 "meta": {"technical_exclusion": "site-packages"}}])
        job = self.app.store.jobs()[0]
        self.app.jobs = {job["id"]: job}
        self.app.local_tab.render()
        self.assertFalse(self.app.local_tab.tree.get_children())
        self.app.store.skip(job["id"], "Technical", "technical_media")
        self.app.jobs = {job["id"]: self.app.store.get(job["id"])}
        self.app.local_tab.status.set("Pominięte")
        self.app.local_tab.render()
        self.assertEqual(len(self.app.local_tab.tree.get_children()), 1)

    def test_linear_wizard_waits_during_operation_and_skips_disabled_options(self):
        self.app.config.update(uvr_enabled=False, diarization=False)
        self.app.readiness = {"components": {key: {"status": OFF if key in {"uvr", "diarization"} else AVAILABLE,
                                                   "message": "ready"}
                                            for key in ("whisper", "tools", "uvr", "diarization", "youtube")}, "passed": False}
        with patch.object(self.app, "request_readiness"), patch("batch.config_wizard.WorkerLock.busy", return_value=False):
            self.app.config_tab.open_wizard()
            wizard = self.app.config_tab.wizard
            wizard.youtube.set(False)
            for expected in (1, 2, 3, 4):
                wizard.advance()
                self.assertEqual(wizard.index, expected)
            wizard.operation = "trial"
            wizard.refresh()
            self.assertEqual(str(wizard.next_button.cget("state")), "disabled")
            wizard.back()
            self.assertEqual(wizard.index, 4)
            wizard.operation = None
            with patch("batch.config_wizard.filedialog.askopenfilename", return_value=""), patch.object(self.app, "setup_process") as launch:
                wizard.advance()
                launch.assert_not_called()
            self.assertEqual(self.app.store.control()["stop"], 1)

    def test_whisper_download_never_marks_missing_community_model_as_ready(self):
        with patch("batch.setup_wizard.validate_diarization", side_effect=RuntimeError("Missing")):
            self.app.configure_model()
        wizard = self.app.setup_dialog
        wizard.show_state({"state": "ready", "phase": "whisper_complete", "message": "Whisper ready"}, False)
        self.assertFalse(wizard.local_ready)
        self.assertIn("Community-1 wymaga", wizard.progress_message.get())


if __name__ == "__main__":
    unittest.main()
