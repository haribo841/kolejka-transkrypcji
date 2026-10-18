"""Recovered corrupt audio remains visibly incomplete in exports and the queue."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from batch.exporter import export, verify_outputs
from batch.local_tab import LocalQueueTab


class RecoveredExportTests(unittest.TestCase):
    def report(self, code="audio_decode_recovered"):
        reason = {"code": code, "message": "Uszkodzone pakiety źródła.", "coverage_unknown": True}
        return {"identity": "local:recovered-fixture", "title": "Odzyskana rozmowa", "duration": 600,
                "processed_until": 590, "incomplete_reason": reason,
                "segments": [{"start": 2, "end": 4, "text": "Zachowana wypowiedź.", "speaker": "Mówca 1"}],
                **({"audio_recovery": reason} if code == "audio_decode_recovered" else {})}

    def test_recovered_exports_warn_of_unknown_internal_gaps_without_fake_subtitles(self):
        with tempfile.TemporaryDirectory() as directory:
            report = self.report()
            paths = export(Path(directory), report, "execution", complete=False)
            txt = paths["txt"].read_text(encoding="utf-8")
            self.assertIn("luki wewnątrz nagrania", txt)
            self.assertIn("sprawną kopię", txt)
            self.assertNotIn("Przy następnym Start program podejmie brakującą część", txt)
            vtt = paths["vtt"].read_text(encoding="utf-8")
            self.assertTrue(vtt.startswith("WEBVTT\n\nNOTE "))
            self.assertIn("luki wewnątrz nagrania", vtt)
            self.assertEqual(vtt.count(" --> "), 1)
            self.assertEqual(paths["srt"].read_text(encoding="utf-8").count(" --> "), 1)
            self.assertTrue(paths["srt"].stem.endswith("_czesciowa"))
            saved = json.loads(paths["json"].read_text(encoding="utf-8"))
            self.assertFalse(saved["complete"])
            self.assertEqual(saved["audio_recovery"], report["audio_recovery"])
            self.assertTrue(verify_outputs(directory, report["identity"], "execution", partial=True))

    def test_uvr_partial_keeps_the_contiguous_prefix_resume_description(self):
        with tempfile.TemporaryDirectory() as directory:
            report = {**self.report("uvr_truncated"), "audio_recovery": None}
            paths = export(directory, report, "execution", complete=False)
            txt = paths["txt"].read_text(encoding="utf-8")
            self.assertIn("Przy następnym Start program podejmie brakującą część", txt)
            self.assertNotIn("luki wewnątrz nagrania", txt)

    def test_recovered_queue_detail_does_not_claim_missing_audio_is_only_a_tail(self):
        job = {"status": "partial", "processed_until": 590, "duration": 600, "error_code": "audio_decode_recovered"}
        detail = LocalQueueTab.partial_detail(job)
        self.assertIn("luki wewnątrz nagrania", detail)
        self.assertIn("inną ścieżkę audio", detail)
        self.assertNotIn("Pozostało:", detail)
        self.assertNotIn("Brakującą część podejmie następny Start", detail)

    def test_uvr_queue_detail_keeps_the_prefix_boundary_and_resume_action(self):
        job = {"status": "partial", "processed_until": 590, "duration": 600, "error_code": "uvr_truncated"}
        detail = LocalQueueTab.partial_detail(job)
        self.assertIn("Zachowano do 00:09:50", detail)
        self.assertIn("Pozostało: 00:00:10", detail)
        self.assertIn("Brakującą część podejmie następny Start", detail)
        job["status"] = "done"
        self.assertEqual(LocalQueueTab.partial_detail(job), "")


if __name__ == "__main__":
    unittest.main()
