from __future__ import annotations

import json
from pathlib import Path
import tempfile
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

from batch.common import ResourceError, atomic_json, default_registry, defaults, job_folder
from batch.importers import import_local
from batch.local_outputs import compact_folder, scan_media
from batch.readiness import (ACTION, AVAILABLE, OFF, PASSED, collect_readiness,
                             configuration_fingerprint, readiness_summary)
from batch.result_paths import reserve_output
from batch.store import Store
from batch.ui_help import Tooltip, duration_text, next_job


def ready_fixture():
    return {"passed": True, "test": {"at": "test"}, "components": {
        key: {"status": PASSED, "message": "Offline fixture"}
        for key in ("whisper", "tools", "diarization", "uvr", "youtube")}}


class OutputPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.source = self.base / "źródła"
        self.source.mkdir()
        self.recording = self.source / "Wykład.wav"
        self.recording.write_bytes(b"recording fixture")
        self.store = Store(self.base / "rejestr")

    def tearDown(self):
        self.temp.cleanup()

    def imported(self, mode="beside", root=""):
        import_local(self.store, [self.recording], output_mode=mode, output_root=root)
        return self.store.jobs()[0]

    def test_migration_keeps_legacy_paths_and_progress(self):
        job = self.imported()
        self.store.update(job["id"], status="done", attempts=2, progress=100)
        old = self.store.get(job["id"])
        old.pop("output_root")
        with self.store.connect() as db:
            db.execute("ALTER TABLE jobs DROP COLUMN output_root")
        current = Store(self.store.root).get(job["id"])
        self.assertIsNone(current.pop("output_root"))
        self.assertEqual(old, current)
        self.assertEqual(job_folder(self.store.root, current), (self.store.root / "wyniki" / current["folder"]).resolve())

    def test_new_destination_beside_source_and_duplicate_cannot_move_it(self):
        job = self.imported()
        destination = job_folder(self.store.root, job)
        self.assertEqual(destination.parent, self.source.resolve())
        self.store.update(job["id"], status="done", attempts=1)
        before = self.store.get(job["id"])
        self.imported("custom", str(self.base / "inne wyniki"))
        self.assertEqual(before, self.store.get(job["id"]))
        self.assertTrue(self.recording.exists())

    def test_central_and_custom_destinations(self):
        job = self.imported("central")
        self.assertEqual(job_folder(self.store.root, job).parent, (self.store.root / "wyniki").resolve())
        self.store.change_output([job["id"]], "custom", self.base / "wspólne")
        changed = self.store.get(job["id"])
        self.assertEqual(job_folder(self.store.root, changed).parent, (self.base / "wspólne").resolve())
        self.assertEqual(changed["status"], "pending")

    def test_change_is_atomic_when_one_selected_job_has_started(self):
        first = self.imported()
        other = self.source / "Drugi.wav"
        other.write_bytes(b"another recording")
        import_local(self.store, [other], output_mode="beside")
        second = self.store.jobs()[1]
        self.store.update(second["id"], attempts=1)
        before = self.store.jobs()
        with self.assertRaisesRegex(ValueError, "bez rozpoczętego"):
            self.store.change_output([first["id"], second["id"]], "central")
        self.assertEqual(before, self.store.jobs())

    def test_partial_outputs_and_active_session_cannot_be_moved(self):
        job = self.imported()
        folder = job_folder(self.store.root, job)
        folder.mkdir()
        (folder / "punkt.json").write_text("checkpoint", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.store.change_output([job["id"]], "central")
        (folder / "punkt.json").unlink()
        self.store.start(kind="local")
        with self.assertRaises(ValueError):
            self.store.change_output([job["id"]], "central")

    def test_rescan_excludes_owned_and_registered_output_folders(self):
        job = self.imported()
        folder = job_folder(self.store.root, job)
        reserve_output(folder, job["identity"])
        (folder / "wokal.flac").write_bytes(b"output")
        (folder / "audio").mkdir()
        (folder / "audio" / "zrodlo.wav").write_bytes(b"copy")
        legacy = self.source / "stary folder wyników"
        legacy.mkdir()
        (legacy / "fragment.wav").write_bytes(b"work")
        found = scan_media(self.source, {".wav", ".flac"}, excluded=[legacy])
        self.assertEqual(found, [self.recording.resolve()])
        self.assertEqual(import_local(self.store, [self.source], output_mode="beside")["added"], 1)
        self.assertEqual(len(self.store.jobs()), 2)  # unregistered legacy folder remains user input

    def test_stable_short_names_reject_owner_collision_and_long_roots(self):
        first = self.imported()
        folder = job_folder(self.store.root, first)
        reserve_output(folder, first["identity"])
        other = dict(first, identity="local:" + "f" * 64)
        with self.assertRaises(FileExistsError):
            reserve_output(folder, other["identity"])
        self.assertLessEqual(len(compact_folder("a" * 500, first["identity"], self.base)), 46)
        with self.assertRaisesRegex(ResourceError, "zbyt długa"):
            compact_folder("wykład", first["identity"], "x" * 240)


class ReadinessTests(unittest.TestCase):
    def test_installation_preserves_its_registry_and_explicit_environment_takes_priority(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp)
            local = app / "obecna baza"
            other = app / "inna baza"
            atomic_json(app / "lokalna-instalacja.json", {"root": str(local)})
            with patch.dict("os.environ", {"KOLEJKA_ROOT": ""}):
                self.assertEqual(default_registry(app), local.resolve())
            with patch.dict("os.environ", {"KOLEJKA_ROOT": str(other)}):
                self.assertEqual(default_registry(app), other.resolve())

    def test_duration_long_audio_and_unknown_values(self):
        for value, expected in ((4017.484, "01:06:57"), (3982.684, "01:06:23"),
                                (360000, "100:00:00"), (0, "00:00:00"),
                                (None, "Nieznany"), (-1, "Nieznany"), (float("nan"), "Nieznany")):
            with self.subTest(value=value):
                self.assertEqual(duration_text(value), expected)

    def test_youtube_access_does_not_block_local_readiness(self):
        report = ready_fixture()
        report["components"]["youtube"] = {"status": ACTION, "message": "Brak cookies"}
        self.assertTrue(readiness_summary(report, "local")[0])
        self.assertFalse(readiness_summary(report, "youtube")[0])
        report["passed"] = False
        self.assertIn("Sprawdź na fragmencie", readiness_summary(report, "local")[1])

    def test_disabled_models_are_not_validated_and_old_proof_is_not_green(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, config = Path(tmp), defaults()
            config.update(diarization=False, uvr_enabled=False)
            atomic_json(root / "pierwsza-proba.json", {"passed": True, "fingerprint": "old"})
            with patch("batch.readiness.check_whisper", return_value="Whisper"), \
                    patch("batch.readiness.check_tools", return_value="FFmpeg"), \
                    patch("batch.readiness.check_youtube", return_value="YouTube"), \
                    patch("batch.readiness.check_diar") as diar, patch("batch.readiness.check_uvr") as uvr, \
                    patch("batch.readiness.environment_versions", return_value={}), \
                    patch("batch.readiness.configuration_fingerprint", return_value="current"):
                report = collect_readiness(config, root)
                self.assertFalse(report["passed"])
                self.assertEqual(report["components"]["whisper"]["status"], AVAILABLE)
                self.assertEqual(report["components"]["diarization"]["status"], OFF)
                self.assertEqual(report["components"]["uvr"]["status"], OFF)
                diar.assert_not_called()
                uvr.assert_not_called()
                atomic_json(root / "pierwsza-proba.json", {"passed": True, "fingerprint": "current"})
                report = collect_readiness(config, root)
                self.assertTrue(report["passed"])
                self.assertEqual(report["components"]["whisper"]["status"], PASSED)

    def test_configuration_and_model_changes_invalidate_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = defaults()
            config.update(model=str(root), diarization=False, uvr_enabled=False)
            model = root / "encoder.bin"
            model.write_bytes(b"weights")
            baseline = configuration_fingerprint(config, {})
            self.assertNotEqual(baseline, configuration_fingerprint(dict(config, keep_audio=True), {}))
            model.write_bytes(b"different weights")
            self.assertNotEqual(baseline, configuration_fingerprint(config, {}))

    def test_immediate_worker_failure_is_saved_and_does_not_claim_jobs(self):
        from batch.worker import launch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root)
            store.add_many([{"identity": "local:" + "a" * 64, "kind": "local", "source": str(root / "a.wav"), "title": "a"}])
            process = Mock()
            process.poll.return_value = 1
            with patch("batch.worker.subprocess.Popen", return_value=process):
                launch(root, kind="local")
            control = store.control()
            self.assertEqual(control["stop"], 1)
            self.assertIn("worker.log", control["message"])
            self.assertIsNone(control["current_id"])
            self.assertEqual(store.jobs()[0]["status"], "pending")


class TooltipTests(unittest.TestCase):
    def setUp(self):
        self.widget = Mock()
        self.widget.after.side_effect = ["pending1", "pending2", "expiry"]
        self.status = Mock()
        self.tooltip = Tooltip(self.widget, "opis", self.status)

    def tearDown(self):
        self.tooltip.hide()

    def test_hover_waits_800ms_and_motion_restarts_delay(self):
        self.tooltip.enter()
        self.widget.after.assert_called_with(800, self.tooltip.show_hover)
        self.tooltip.motion()
        self.widget.after_cancel.assert_called_with("pending1")
        self.widget.after.assert_called_with(800, self.tooltip.show_hover)
        self.assertIsNone(self.tooltip.popup)

    def test_keyboard_focus_only_updates_help_and_exit_cancels(self):
        bindings = {args[0]: args[1] for args, kwargs in self.widget.bind.call_args_list}
        bindings["<FocusIn>"]()
        self.status.set.assert_called_with("opis")
        self.widget.after.assert_not_called()
        self.assertEqual(bindings["<F1>"], self.tooltip.show)
        self.tooltip.enter()
        self.tooltip.leave()
        self.assertFalse(self.tooltip.hovered)
        self.assertIsNone(self.tooltip.pending)
        for name in ("<ButtonPress>", "<MouseWheel>", "<Escape>", "<FocusOut>"):
            self.assertEqual(bindings[name], self.tooltip.hide)
        self.widget.winfo_toplevel().bind.assert_called_with("<Deactivate>", self.tooltip.hide, add="+")

    def test_popup_expires_after_six_seconds(self):
        self.widget.winfo_rootx.return_value = 20
        self.widget.winfo_rooty.return_value = 20
        self.widget.winfo_height.return_value = 20
        self.widget.winfo_screenwidth.return_value = 1920
        self.widget.winfo_screenheight.return_value = 1080
        with patch("batch.ui_help.tk.Toplevel") as factory, patch("batch.ui_help.tk.Frame"), patch("batch.ui_help.tk.Label"):
            popup = factory.return_value
            popup.winfo_reqwidth.return_value = 200
            popup.winfo_reqheight.return_value = 60
            self.tooltip.show()
            self.widget.after.assert_called_with(6000, self.tooltip.hide)
            self.tooltip.hide()
            popup.destroy.assert_called_once()


class NavigationTests(unittest.TestCase):
    def setUp(self):
        from batch.gui import Window
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.collect = patch("batch.gui.collect_readiness", side_effect=lambda *args: ready_fixture())
        self.collect.start()
        self.addCleanup(self.collect.stop)
        self.tk = tk.Tk()
        self.tk.withdraw()
        atomic_json(self.root / "okno-ui.json", {"queue": "local"})
        self.app = Window(self.root, tk_root=self.tk)
        self.app.store.add_many([{"identity": f"yt:{index:011}", "kind": "youtube", "source": "https://youtube.com",
                                 "title": str(index), "date": f"2020-{index:04}", "status": "done" if index < 60 else "pending"}
                                for index in range(80)])
        self.app.jobs = {job["id"]: job for job in self.app.store.jobs()}
        self.app.render_list()

    def tearDown(self):
        self.app.close()
        self.temp.cleanup()

    def test_last_queue_restored_and_initial_navigation_chooses_pending(self):
        self.assertEqual(self.app.notebook.select(), str(self.app.local_page))
        self.app.notebook.select(self.app.queue_page)
        self.app.navigation_done.clear()
        with patch.object(self.app.tree, "winfo_ismapped", return_value=True), patch.object(self.app.tree, "see") as see:
            self.app.initial_navigation()
            self.assertEqual(self.app.tree.selection(), ("61",))
            see.assert_called_once_with("61")
            self.app.tree.selection_set("20")
            self.app.initial_navigation()
            self.assertEqual(self.app.tree.selection(), ("20",))

    def test_live_job_takes_priority_and_next_button_clears_hidden_filters(self):
        self.app.store.update(75, status="running")
        self.app.store.control(current_id=75)
        self.app.jobs = {job["id"]: job for job in self.app.store.jobs()}
        self.app.search.set("does not exist")
        self.app.render_list()
        with patch.object(self.app.tree, "see") as see:
            self.app.jump_to_next("youtube")
            self.assertEqual(self.app.search.get(), "")
            self.assertEqual(self.app.tree.selection(), ("75",))
            see.assert_called_once_with("75")

    def test_refresh_retains_scroll_and_user_selection(self):
        tree = self.app.tree
        tree.selection_set("31", "32")
        with patch.object(tree, "yview", return_value=(.3, .5)), patch.object(tree, "yview_moveto") as move:
            self.app.jobs[31]["progress"] = 10
            self.app.render_list()
            self.assertEqual(set(tree.selection()), {"31", "32"})
            move.assert_called_once_with(.3)
        self.assertFalse(tree.tooltip.allow_popup)
        self.assertFalse(self.app.notebook.tooltip.allow_popup)

    def test_failure_is_durable_and_never_claims_a_job(self):
        from test_support import wait_for_start
        before = self.app.store.control()
        with patch("batch.gui.preflight", side_effect=ResourceError("Testowy błąd procesu")), \
                patch("batch.gui.messagebox.showerror") as popup, patch("batch.gui.launch") as launch:
            self.app.start()
            wait_for_start(self.app)
            launch.assert_not_called()
            popup.assert_called_once()
        saved = json.loads((self.root / "ostatni-blad-start.json").read_text(encoding="utf-8"))
        self.assertIn("Testowy błąd procesu", saved["message"])
        self.assertEqual(self.app.store.control(), before)

    def test_disabled_pending_jobs_are_skipped(self):
        rows = list(self.app.jobs.values())
        rows[60]["enabled"] = False
        self.assertEqual(next_job(rows, "youtube")["id"], 62)


if __name__ == "__main__":
    unittest.main()
