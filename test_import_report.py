"""Import summaries must distinguish physical file paths from unique jobs."""
import errno
import json
from pathlib import Path
import subprocess
import unittest

from batch.import_report import diagnose, finalize_stats, import_note, issue


class ImportReportTests(unittest.TestCase):
    def test_diagnoses_are_specific_before_generic_probe_or_access_errors(self):
        cases = (
            ("Ścieżka wyników jest zbyt długa. Wybierz krótszy folder wyników.", "output_path"),
            ("Plik zmienił się podczas importu: x.wav", "source_changed"),
            (PermissionError(errno.EACCES, "blocked", "x.wav"), "file_access"),
            ("Plik nie zawiera ścieżki audio. Wybierz nagranie z dźwiękiem.", "no_audio"),
            ("FFprobe nie rozpoznaje kodeka audio tego pliku.", "unsupported_codec"),
            ("FFprobe: invalid input", "media_probe"),
            (subprocess.TimeoutExpired(["ffprobe", "x.wav"], 90), "media_probe"),
            ("[WinError 1920] The file cannot be accessed by the system", "file_access"),
            ("unknown failure", "other"),
        )
        for message, expected in cases:
            with self.subTest(message=message):
                self.assertEqual(diagnose(message), expected)

    def test_explicit_exception_code_survives_localized_messages(self):
        error = RuntimeError("dowolny komunikat")
        error.code = "no_audio"
        self.assertEqual(diagnose(error), "no_audio")

    def test_issue_is_json_safe_and_can_reference_canonical_job(self):
        problem = issue(Path("x.wav"), "probe", RuntimeError("FFprobe: invalid"),
                        identity="local:abc", job_id=7)
        self.assertEqual(json.loads(json.dumps(problem)), {
            "path": "x.wav", "phase": "probe", "code": "media_probe", "message": "FFprobe: invalid",
            "identity": "local:abc", "job_id": 7,
        })

    def test_two_copies_without_audio_are_two_file_problems_and_one_job_problem(self):
        result = {"files": 2, "added": 1, "skipped": 0, "errors": 2,
                  "job_ids": [7], "job_statuses": ["no_audio"], "scan_warnings": []}
        message = "Plik nie zawiera ścieżki audio."
        problems = [issue(path, "probe", message, job_id=7) for path in ("a.mp4", "b.mp4")]
        job = {"id": 7, "status": "no_audio", "error_code": "no_audio", "error": message}
        self.assertIs(finalize_stats(result, problems, [job, job]), result)
        self.assertEqual(result["file_problem_counts"], {"no_audio": 2})
        self.assertEqual(result["job_problem_counts"], {"no_audio": 1})
        self.assertEqual((result["matched_jobs"], result["existing_jobs"], result["duplicate_paths"]), (1, 0, 1))
        self.assertEqual((result["errors"], result["problem_files"], result["problem_jobs"]), (2, 2, 1))
        note = import_note(result)
        self.assertIn("Brak ścieżki audio: 1 zadań / 2 ścieżek", note)
        self.assertIn('"Ponów nieudane" nie doda brakującego audio', note)

    def test_existing_done_job_and_multiple_copies_have_distinct_counts(self):
        result = {"files": 4, "added": 1, "skipped": 0, "errors": 0}
        jobs = [{"id": 8, "status": "done"}, {"id": 9, "status": "pending", "enabled": 1}]
        finalize_stats(result, [], jobs, found={8: "done", 9: "pending"})
        self.assertEqual((result["matched_jobs"], result["existing_jobs"], result["duplicate_paths"]), (2, 1, 2))
        self.assertEqual(result["ready_jobs"], 1)
        self.assertEqual(result["job_problem_counts"], {})
        self.assertIn("Kopie lub już w kolejce: 3", import_note(result))
        self.assertIn("już w kolejce: 1; kopie plików w imporcie: 2", import_note(result))

    def test_scan_access_problem_is_a_folder_warning_not_a_counted_file(self):
        result = {"files": 0, "added": 0, "skipped": 0, "errors": 0}
        finalize_stats(result, [issue("private", "scan", PermissionError("permission denied"))])
        self.assertEqual(result["scan_problem_counts"], {"file_access": 1})
        self.assertEqual(result["file_problem_counts"], {})
        self.assertEqual(result["problem_files"], 0)
        note = import_note(result, "tylko M4A, bez podfolderów")
        self.assertIn("Ostrzeżenia skanowania (pliki lub foldery)", note)
        self.assertIn("tylko M4A, bez podfolderów", note)
        self.assertIn("Sprawdź folder oraz opcje", note)

    def test_both_probe_and_output_diagnoses_survive_for_one_path(self):
        result = {"files": 1, "added": 0, "skipped": 1, "errors": 1}
        problems = [issue("a.mp4", "probe", "Plik nie zawiera ścieżki audio."),
                    issue("a.mp4", "register", "Ścieżka wyników jest zbyt długa.")]
        finalize_stats(result, problems + problems)
        self.assertEqual(result["file_problem_counts"], {"no_audio": 1, "output_path": 1})
        self.assertEqual(result["problem_files"], 1)
        self.assertIn('"Zmień miejsce wyników"', import_note(result))

    def test_output_required_job_is_actionable_and_never_ready(self):
        result = {"files": 1, "added": 1, "skipped": 0, "errors": 1}
        job = {"id": 11, "status": "output_required", "enabled": 1, "error_code": "output_path",
               "error": "wybierz folder"}
        finalize_stats(result, [issue("a.wav", "register", job["error"], code="output_path")], [job])
        self.assertEqual(result["job_problem_counts"], {"output_path": 1})
        self.assertEqual(result["ready_jobs"], 0)
        note = import_note(result)
        self.assertIn("Zaznaczone pozycje: Wybierz miejsce wyników: 1", note)
        self.assertIn('"Folder centralny"', note)
        self.assertNotIn("Błędy odczytu", note)
        self.assertNotIn("niedostępne pliki/foldery", note)

    def test_technical_exclusions_are_information_not_failed_transcriptions(self):
        result = {"files": 2, "added": 0, "skipped": 0, "errors": 0}
        job = {"id": 11, "status": "error", "enabled": 0, "error_code": "no_audio", "error": "no audio",
               "source_meta": json.dumps({"technical_exclusion": "model sample"})}
        problems = [issue("sample.wav", "scan", "model sample", code="technical_media", kind="file")]
        finalize_stats(result, problems, [job])
        self.assertEqual((result["technical_files"], result["technical_jobs"]), (1, 1))
        self.assertEqual(result["file_problem_counts"], {})
        self.assertEqual(result["job_problem_counts"], {})
        self.assertEqual(result["ready_jobs"], 0)
        self.assertIn("Materiały techniczne", import_note(result))

    def test_technical_environment_directories_are_not_counted_as_files(self):
        result = {"files": 1, "added": 1, "skipped": 0, "errors": 0}
        problems = [issue("venv", "scan", "environment", code="technical_media", kind="directory"),
                    issue("sample.wav", "scan", "model sample", code="technical_media", kind="file")]
        finalize_stats(result, problems + problems, [{"id": 7, "status": "pending"}])
        self.assertEqual((result["technical_files"], result["technical_directories"]), (1, 1))
        self.assertEqual(result["duplicate_paths"], 0)
        self.assertEqual(result["skipped"], 0)
        self.assertEqual(result["file_problem_counts"], {})
        self.assertEqual(result["scan_problem_counts"], {})
        self.assertIn("Materiały techniczne: 1 plików, 1 folderów", import_note(result))

    def test_intentionally_enabled_technical_jobs_are_ready_or_have_their_actual_problem(self):
        for enabled in (True, None):
            with self.subTest(enabled=enabled):
                jobs = [
                    {"id": 7, "status": "pending", "source_meta": {"technical_exclusion": "model sample"}},
                    {"id": 8, "status": "error", "error_code": "media_probe", "error": "FFprobe: corrupt",
                     "source_meta": {"technical_exclusion": "model sample"}},
                ]
                if enabled is not None:
                    for job in jobs:
                        job["enabled"] = enabled
                before = json.dumps(jobs)
                result = {"files": 2, "added": 0, "skipped": 0, "errors": 1}
                finalize_stats(result, [], jobs)
                self.assertEqual(result["technical_jobs"], 0)
                self.assertEqual(result["ready_jobs"], 1)
                self.assertEqual(result["job_problem_counts"], {"media_probe": 1})
                self.assertNotIn("wyłączonych zadań", import_note(result))
                self.assertEqual(json.dumps(jobs), before)

    def test_ready_count_matches_start_for_recoverable_errors_and_actionable_states(self):
        jobs = [
            {"id": 1, "status": "error", "stage": "Wymaga działania", "enabled": 1, "error_code": "media_probe"},
            {"id": 2, "status": "error", "stage": "Walidacja pliku", "enabled": 1, "error_code": "media_probe"},
            {"id": 3, "status": "error", "stage": "Wymaga działania", "enabled": 0, "error_code": "media_probe"},
            {"id": 4, "status": "no_audio", "stage": "Wymaga działania", "enabled": 1, "error_code": "no_audio"},
            {"id": 5, "status": "output_required", "stage": "Wymaga działania", "enabled": 1,
             "error_code": "output_path"},
        ]
        result = {"files": 5, "added": 0, "skipped": 0, "errors": 5}
        finalize_stats(result, [], jobs)
        self.assertEqual(result["ready_jobs"], 1)
        self.assertEqual(result["job_problem_counts"], {"media_probe": 3, "no_audio": 1, "output_path": 1})

    def test_legacy_report_does_not_describe_output_paths_as_access_errors(self):
        result = {"files": 3060, "added": 751, "errors": 1944, "skipped": 994,
                  "job_ids": list(range(782)), "job_statuses": ["pending"] * 498 + ["error"] * 253 + ["done"] * 31,
                  "scan_warnings": [{"path": str(index), "message": "Ścieżka wyników jest zbyt długa."}
                                    for index in range(994)]}
        before = json.dumps(result)
        note = import_note(result)
        self.assertEqual(json.dumps(result), before)
        self.assertIn("Znaleziono: 3060 | Dodano: 751", note)
        self.assertIn("już w kolejce: 31; kopie plików w imporcie: 1284", note)
        self.assertIn("Zbyt długa ścieżka wyników: 994", note)
        self.assertIn("dla pominiętych plików ze starszego importu ponów import", note)
        self.assertNotIn("Błędy odczytu", note)
        self.assertNotIn("niedostępne", note)
        self.assertNotIn("1944", note)
        self.assertLessEqual(len(note), 900)
        self.assertLessEqual(note.count("."), 4)

    def test_job_cause_uses_final_canonical_row_not_duplicate_probe_result(self):
        result = {"files": 1, "added": 0, "skipped": 0, "errors": 1,
                  "job_ids": [7], "job_statuses": ["done"]}
        finalize_stats(result, [issue("copy.wav", "probe", "FFprobe: temporary failure", job_id=7)],
                       [{"id": 7, "status": "done", "error": "", "processed_until": 20}])
        self.assertEqual(result["file_problem_counts"], {"media_probe": 1})
        self.assertEqual(result["job_problem_counts"], {})
        self.assertIn("Zaznaczone pozycje: Gotowe: 1", import_note(result))
        self.assertIn("Ponowny import nie resetuje postępu; Start pozostaje ręczny", import_note(result))

    def test_rows_outside_import_do_not_inflate_problem_counts(self):
        result = {"files": 1, "added": 0, "skipped": 0, "errors": 0,
                  "job_ids": [7], "job_statuses": ["done"]}
        finalize_stats(result, [], [{"id": 7, "status": "done"},
                                    {"id": 8, "status": "error", "error_code": "no_audio"}])
        self.assertEqual(result["matched_jobs"], 1)
        self.assertEqual(result["job_problem_counts"], {})


if __name__ == "__main__":
    unittest.main()
