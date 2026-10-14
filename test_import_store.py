"""Isolated import identity and actionable destination state regressions."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from batch.common import ResourceError, job_folder
from batch.store import RETRYABLE, STATUSES, Store


class ImportStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.source = self.base / "recording.wav"
        self.source.write_bytes(b"local audio fixture")
        self.store = Store(self.base / "registry")
        self.identity = "local:" + "f" * 64

    def tearDown(self):
        self.temp.cleanup()

    def row(self, **values):
        row = {"identity": self.identity, "kind": "local", "title": "Recording",
               "source": str(self.source), "output_root": str(self.base / "outputs")}
        row.update(values)
        return row

    def output_required(self, probe_error="", probe_code=""):
        metadata = {"output_error": "Ścieżka wyników jest zbyt długa.",
                    "intended_output_root": "x" * 240, "sha256": "f" * 64}
        if probe_error:
            metadata.update(probe_error=probe_error, probe_error_code=probe_code)
        self.store.add_many([self.row(output_root=None, status="output_required",
                                      error=metadata["output_error"], error_code="output_path",
                                      stage="Wybierz miejsce wyników", meta=metadata)])
        job = next(job for job in self.store.jobs() if job["identity"] == self.identity)
        history = json.dumps([{"code": "past_error", "message": "Kept prior diagnostic"}])
        self.store.update(job["id"], error_history=history)
        return self.store.get(job["id"])

    def test_complete_duplicate_ignores_unusable_new_output_root_and_keeps_results(self):
        self.assertEqual(self.store.add_many([self.row()]), 1)
        job = self.store.jobs()[0]
        self.store.update(job["id"], status="done", progress=100, attempts=2,
                          stage="Gotowe", processed_until=12)
        folder = job_folder(self.store.root, job)
        folder.mkdir(parents=True)
        result = folder / "previous.txt"
        result.write_bytes(b"preserved completed transcription")
        duplicate = self.base / "renamed.wav"
        duplicate.write_bytes(self.source.read_bytes())
        before = self.store.get(job["id"])
        with patch("batch.local_outputs.compact_folder", side_effect=ResourceError("deep output path")) as compact:
            self.assertEqual(self.store.add_many([self.row(source=str(duplicate), title="Renamed",
                                                          output_root="x" * 240, status="error",
                                                          error="new probe failed")]), 0)
        compact.assert_not_called()
        self.assertEqual(self.store.get(job["id"]), before)
        self.assertEqual(result.read_bytes(), b"preserved completed transcription")

    def test_missing_original_can_use_deep_duplicate_without_moving_completed_results(self):
        self.store.add_many([self.row()])
        job = self.store.jobs()[0]
        self.store.update(job["id"], status="done", progress=100, attempts=1)
        duplicate = self.base / "alternate.wav"
        duplicate.write_bytes(self.source.read_bytes())
        self.source.unlink()
        before = self.store.get(job["id"])
        with patch("batch.local_outputs.compact_folder", side_effect=ResourceError("deep output path")) as compact:
            self.assertEqual(self.store.add_many([self.row(source=str(duplicate), output_root="x" * 240)]), 0)
        compact.assert_not_called()
        after = self.store.get(job["id"])
        self.assertEqual(after["source"], str(duplicate))
        for key in before.keys() - {"source", "updated"}:
            self.assertEqual(after[key], before[key])

    def test_new_actionable_rows_store_codes_and_are_not_retried_or_claimed(self):
        self.store.add_many([self.row(status="no_audio", error_code="no_audio"),
                             self.row(identity="local:" + "a" * 64, output_root=None,
                                      status="output_required", error_code="output_required")])
        self.assertEqual(STATUSES["no_audio"], "Bez ścieżki audio")
        self.assertEqual(STATUSES["output_required"], "Wybierz miejsce wyników")
        self.assertNotIn("no_audio", RETRYABLE)
        self.assertNotIn("output_required", RETRYABLE)
        self.assertEqual([job["error_code"] for job in self.store.jobs()], ["no_audio", "output_required"])
        self.assertEqual(self.store.retry(), 0)
        self.store.start(kind="local")
        self.assertIsNone(self.store.claim())

    def test_valid_destination_clears_only_current_output_failure_and_keeps_history(self):
        before = self.output_required()
        destination = self.base / "short"
        self.assertEqual(self.store.change_output([before["id"]], "custom", destination), 1)
        after = self.store.get(before["id"])
        self.assertEqual(after["status"], "pending")
        self.assertEqual(after["error"], "")
        self.assertEqual(after["error_code"], "")
        self.assertEqual(after["stage"], "")
        history = json.loads(after["error_history"])
        self.assertEqual(history[:-1], json.loads(before["error_history"]))
        self.assertEqual(history[-1], {"at": before["updated"], "code": "output_path",
                                      "message": before["error"], "processed_until": before["processed_until"]})
        self.assertEqual(job_folder(self.store.root, after).parent, destination.resolve())
        metadata = json.loads(after["source_meta"])
        self.assertNotIn("output_error", metadata)
        self.assertNotIn("intended_output_root", metadata)
        self.assertEqual(metadata["sha256"], "f" * 64)

    def test_current_path_error_already_in_history_is_not_duplicated(self):
        before = self.output_required()
        history = json.dumps([{"at": before["updated"], "code": "output_path",
                               "message": before["error"], "processed_until": before["processed_until"]}])
        self.store.update(before["id"], error_history=history)
        self.store.change_output([before["id"]], "custom", self.base / "short")
        self.assertEqual(self.store.get(before["id"])["error_history"], history)
        self.store.change_output([before["id"]], "custom", self.base / "another-short")
        self.assertEqual(self.store.get(before["id"])["error_history"], history)

    def test_destination_repair_restores_probe_failure_and_preserves_diagnosis(self):
        for code, expected in (("no_audio", "no_audio"), ("unsupported_codec", "error")):
            with self.subTest(code=code):
                self.identity = "local:" + ("a" if code == "no_audio" else "b") * 64
                before = self.output_required("Original probe failure", code)
                self.store.change_output([before["id"]], "custom", self.base / code)
                after = self.store.get(before["id"])
                self.assertEqual(after["status"], expected)
                self.assertEqual(after["error"], "Original probe failure")
                self.assertEqual(after["error_code"], code)
                self.assertEqual(after["stage"], "Walidacja pliku")
                history = json.loads(after["error_history"])
                self.assertEqual(history[:-1], json.loads(before["error_history"]))
                self.assertEqual(history[-1]["code"], "output_path")
                self.assertEqual(history[-1]["message"], before["error"])
                metadata = json.loads(after["source_meta"])
                self.assertEqual(metadata["probe_error_code"], code)
                self.assertNotIn("output_error", metadata)

    def test_compound_output_and_probe_failure_archives_resolved_path_error(self):
        job = self.output_required("Unsupported audio codec", "unsupported_codec")
        self.store.update(job["id"], error_history="[]")
        before = self.store.get(job["id"])
        self.store.change_output([job["id"]], "custom", self.base / "usable-output")
        after = self.store.get(job["id"])
        self.assertEqual(after["status"], "error")
        self.assertEqual(after["error_code"], "unsupported_codec")
        self.assertEqual(after["error"], "Unsupported audio codec")
        self.assertEqual(json.loads(after["error_history"]), [
            {"at": before["updated"], "code": "output_path", "message": before["error"],
             "processed_until": before["processed_until"]}])

    def test_failed_destination_selection_is_atomic(self):
        before = self.output_required()
        with patch("batch.local_outputs.compact_folder", side_effect=ResourceError("too long")):
            with self.assertRaises(ResourceError):
                self.store.change_output([before["id"]], "custom", self.base / "still-too-long")
        self.assertEqual(self.store.get(before["id"]), before)

    def test_missing_work_folder_cannot_move_partial_or_any_recorded_progress(self):
        for index, changes in enumerate(({"status": "partial"}, {"processed_until": 1}, {"progress": 1})):
            with self.subTest(changes=changes):
                identity = "local:" + str(index) * 64
                self.store.add_many([self.row(identity=identity)])
                job = next(job for job in self.store.jobs() if job["identity"] == identity)
                self.store.update(job["id"], **changes)
                before = self.store.get(job["id"])
                self.assertEqual(before["attempts"], 0)
                self.assertFalse(job_folder(self.store.root, before).exists())
                with self.assertRaisesRegex(ValueError, "bez rozpoczętego przetwarzania"):
                    self.store.change_output([before["id"]], "custom", self.base / "new-place")
                self.assertEqual(self.store.get(before["id"]), before)

    def test_audio_track_change_cannot_release_missing_output_or_no_audio(self):
        first = self.output_required()
        self.store.add_many([self.row(identity="local:" + "a" * 64, status="no_audio",
                                      error="No audio stream", error_code="no_audio")])
        second = self.store.jobs()[1]
        self.store.configure([first["id"], second["id"]], audio_track=1)
        for before in (first, second):
            after = self.store.get(before["id"])
            self.assertEqual(after["audio_track"], 1)
            self.assertEqual(after["status"], before["status"])
            self.assertEqual(after["error"], before["error"])
            self.assertEqual(after["error_code"], before["error_code"])


if __name__ == "__main__":
    unittest.main()
