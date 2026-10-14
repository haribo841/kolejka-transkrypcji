"""Integration regressions for safe automatic discovery and local imports."""
from __future__ import annotations

import errno
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from batch.common import digest
from batch.importers import MEDIA, enrich_local, import_local
from batch.local_outputs import scan_media
from batch.media import MediaError
from batch.path_budget import PATH_LIMIT, path_units
from batch.store import RETRYABLE, Store


MEDIA_METADATA = {"duration": 1.0, "audio_tracks": [{"index": 0, "codec_name": "pcm_s16le"}],
                  "has_video": False, "container": "wav"}
CMAKE_TIMESTAMP = (b"# CMAKE generated file: DO NOT EDIT!\n"
                   b"# Timestamp file for compiler generated dependencies management for Utilities.\n")


class ImportRepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source_root = self.root / "recordings"
        self.source_root.mkdir()
        self.store = Store(self.root / "registry")

    def source(self, relative, data=b"recording"):
        source = self.source_root / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(data)
        return source.resolve()

    def long_source(self, name, data):
        parent = self.source_root / "deep"
        parent /= "d" * max(1, PATH_LIMIT - 35 - path_units(parent) - 1)
        parent.mkdir(parents=True, exist_ok=True)
        source = parent / name
        source.write_bytes(data)
        self.assertGreaterEqual(path_units(parent), PATH_LIMIT - 35)
        return source.resolve()

    def test_scanner_prunes_confirmed_environments_and_keeps_ordinary_names(self):
        self.source("arbitrary-runtime/pyvenv.cfg", b"home = Python\n")
        hidden = self.source("arbitrary-runtime/samples/library.wav")
        package = self.source("runtime/lib/python/site-packages/scipy/io/tests/test.wav")
        technical_ts = self.source("build/CMakeFiles/Utilities.dir/compiler_depend.ts", CMAKE_TIMESTAMP)
        expected = {self.source(f"{name}/lesson.wav", name.encode()) for name in ("env", "test", "build", "venv")}
        result = scan_media(self.source_root, MEDIA)
        self.assertEqual(set(result), expected)
        self.assertNotIn(hidden, result)
        self.assertNotIn(package, result)
        self.assertEqual(result.warnings, [])
        self.assertEqual(len(result.exclusions), 3)
        self.assertEqual({entry["kind"] for entry in result.exclusions}, {"file", "directory"})
        self.assertTrue(all(entry["phase"] == "scan" and entry["code"] == "technical_media"
                            for entry in result.exclusions))
        self.assertEqual({entry["path"] for entry in result.exclusions if entry["kind"] == "file"}, {str(technical_ts)})
        # A pruned environment is one excluded directory, not a list of its files.
        self.assertFalse(any(entry["path"] in {str(hidden), str(package)} for entry in result.exclusions))

    def test_include_technical_allows_environment_fixtures_and_text_ts(self):
        self.source("custom-runtime/pyvenv.cfg", b"home = Python\n")
        expected = {
            self.source("custom-runtime/samples/test.wav"),
            self.source("runtime/site-packages/example.wav"),
            self.source("build/compiler_depend.ts", CMAKE_TIMESTAMP),
            self.source("ordinary/lesson.mp3"),
        }
        result = scan_media(self.source_root, MEDIA, include_technical=True)
        self.assertEqual(set(result), expected)
        self.assertEqual(result.exclusions, [])
        self.assertEqual(result.warnings, [])

    def test_explicit_file_import_allows_intentionally_selected_library_fixture(self):
        fixture = self.source("runtime/site-packages/scipy/io/tests/example.wav", b"chosen fixture")
        self.assertEqual(scan_media(self.source_root, MEDIA), [])
        before_control = self.store.control()
        with patch("batch.media.media_metadata", return_value=MEDIA_METADATA) as probe:
            result = import_local(self.store, [fixture], config={}, output_mode="central")
        self.assertEqual((result["files"], result["added"], result["matched_jobs"]), (1, 1, 1))
        self.assertEqual(self.store.jobs()[0]["source"], str(fixture))
        self.assertEqual(self.store.jobs()[0]["status"], "pending")
        self.assertEqual(self.store.control(), before_control)
        probe.assert_called_once_with(fixture, {})

    def test_technical_directory_and_file_counts_are_separate_from_media_paths(self):
        self.source("first-environment/pyvenv.cfg", b"home = Python\n")
        self.source("first-environment/samples/one.wav", b"fixture one")
        self.source("runtime/dist-packages/tests/two.wav", b"fixture two")
        self.source("build/compiler_depend.ts", CMAKE_TIMESTAMP)
        self.source("src/application.ts", b"export const answer: number = 42;\n")
        first = self.source("real/lesson.wav", b"same intended recording")
        second = self.source("copy/lesson.wav", first.read_bytes())
        with patch("batch.media.media_metadata", return_value=MEDIA_METADATA):
            result = import_local(self.store, [self.source_root], config={}, output_mode="central")
        self.assertEqual((result["files"], result["added"], result["matched_jobs"], result["duplicate_paths"]),
                         (2, 1, 1, 1))
        self.assertEqual((result["technical_directories"], result["technical_files"]), (2, 2))
        self.assertEqual((result["errors"], result["skipped"], result["problem_files"], result["problem_jobs"]),
                         (0, 0, 0, 0))
        self.assertEqual(result["scan_warnings"], [])
        self.assertEqual(result["scan_problem_counts"], {})
        self.assertEqual(result["job_ids"], [self.store.jobs()[0]["id"]])
        self.assertIn(self.store.jobs()[0]["source"], {str(first), str(second)})

    def test_no_audio_is_a_unique_non_retryable_problem_while_copies_count_as_paths(self):
        first = self.source("first/silent.mp4", b"same silent video")
        second = self.source("second/silent.mp4", first.read_bytes())
        message = "Plik nie zawiera ścieżki audio. Wybierz nagranie z dźwiękiem."
        with patch("batch.media.media_metadata", side_effect=MediaError(message)):
            result = import_local(self.store, [first, second], config={}, output_mode="central")
        job = self.store.jobs()[0]
        self.assertEqual((result["files"], result["added"], result["errors"], result["matched_jobs"]), (2, 1, 2, 1))
        self.assertEqual((result["problem_files"], result["problem_jobs"], result["duplicate_paths"]), (2, 1, 1))
        self.assertEqual(result["file_problem_counts"], {"no_audio": 2})
        self.assertEqual(result["job_problem_counts"], {"no_audio": 1})
        self.assertEqual((job["status"], job["error_code"]), ("no_audio", "no_audio"))
        self.assertNotIn("no_audio", RETRYABLE)
        before = self.store.jobs()
        self.assertEqual(self.store.retry([job["id"]]), 0)
        self.assertEqual(self.store.jobs(), before)
        self.store.start(kind="local")
        self.assertIsNone(self.store.claim())
        self.assertEqual(self.store.get(job["id"]), job)

    def test_probe_timeout_preserves_error_and_imports_the_next_valid_file(self):
        first = self.source("a-timeout.wav", b"slow media")
        second = self.source("b-valid.wav", b"valid media")
        error = subprocess.TimeoutExpired(["ffprobe", "-show_streams", str(first)], 90)
        before_control = self.store.control()
        with patch("batch.media.media_metadata", side_effect=[error, MEDIA_METADATA]) as probe:
            result = import_local(self.store, [first, second], config={}, output_mode="central")
        self.assertEqual(probe.call_count, 2)
        self.assertEqual((result["files"], result["added"], result["errors"], result["skipped"]), (2, 2, 1, 0))
        jobs = {job["source"]: job for job in self.store.jobs()}
        failed, valid = jobs[str(first)], jobs[str(second)]
        self.assertEqual((failed["status"], failed["error_code"]), ("error", "media_probe"))
        self.assertEqual(json.loads(failed["source_meta"])["probe_error_code"], "media_probe")
        self.assertIn("timed out", failed["error"])
        self.assertEqual(valid["status"], "pending")
        self.assertEqual(result["file_problem_counts"], {"media_probe": 1})
        self.assertEqual(result["job_problem_counts"], {"media_probe": 1})
        self.assertEqual(self.store.control(), before_control)

    def test_later_no_audio_validation_preserves_done_and_never_becomes_claimable(self):
        pending_source = self.source("pending.wav", b"unvalidated silent recording")
        done_source = self.source("done.wav", b"completed recording")
        import_local(self.store, [pending_source, done_source], config=None)
        jobs = {job["source"]: job for job in self.store.jobs()}
        pending_id, done_id = jobs[str(pending_source)]["id"], jobs[str(done_source)]["id"]
        self.store.update(done_id, status="done", progress=100, attempts=3, processed_until=1, error="saved note")
        before_done = self.store.get(done_id)
        before_control = self.store.control()
        message = "Plik nie zawiera ścieżki audio. Wybierz nagranie z dźwiękiem."
        with patch("batch.media.media_metadata", side_effect=MediaError(message)):
            enrich_local(self.store, self.store.jobs(), {})
        pending = self.store.get(pending_id)
        self.assertEqual((pending["status"], pending["error_code"], pending["error"]),
                         ("no_audio", "no_audio", message))
        self.assertEqual(json.loads(pending["source_meta"])["probe_error_code"], "no_audio")
        self.assertEqual(self.store.get(done_id), before_done)
        self.assertEqual(self.store.control(), before_control)
        self.assertEqual(self.store.retry([pending_id]), 0)
        self.store.start(kind="local")
        self.assertIsNone(self.store.claim())
        self.assertEqual(self.store.get(pending_id), pending)
        self.assertEqual(self.store.get(done_id), before_done)

    def test_unusable_output_root_preserves_a_registered_actionable_job(self):
        source = self.long_source("lesson.wav", b"intended deep source")
        original_hash = digest(source)
        before_control = self.store.control()
        with patch("batch.media.media_metadata", return_value=MEDIA_METADATA):
            result = import_local(self.store, [source], config={}, output_mode="beside")
        job = self.store.jobs()[0]
        self.assertEqual((result["files"], result["added"], result["skipped"], result["matched_jobs"]), (1, 1, 0, 1))
        self.assertEqual((job["status"], job["error_code"], job["source"]),
                         ("output_required", "output_path", str(source)))
        self.assertIsNone(job["output_root"])
        self.assertEqual(json.loads(job["source_meta"])["intended_output_root"], str(source.parent))
        self.assertEqual(result["file_problem_counts"], {"output_path": 1})
        self.assertEqual(result["job_problem_counts"], {"output_path": 1})
        self.assertEqual(self.store.control(), before_control)
        self.assertEqual(digest(source), original_hash)

    def test_output_repair_restores_the_original_codec_error_without_starting(self):
        source = self.long_source("codec.wav", b"same unsupported recording")
        original_hash = digest(source)
        before_control = self.store.control()
        message = "FFprobe nie rozpoznaje kodeka audio tego pliku."
        with patch("batch.media.media_metadata", side_effect=MediaError(message)):
            result = import_local(self.store, [source], config={}, output_mode="beside")
        job = self.store.jobs()[0]
        metadata = json.loads(job["source_meta"])
        self.assertEqual((job["status"], job["error_code"]), ("output_required", "output_path"))
        self.assertEqual((metadata["probe_error_code"], metadata["probe_error"]), ("unsupported_codec", message))
        self.assertIn("output_error", metadata)
        self.assertEqual(result["file_problem_counts"], {"unsupported_codec": 1, "output_path": 1})
        self.assertEqual(self.store.retry([job["id"]]), 0)
        self.assertEqual(self.store.get(job["id"]), job)
        self.assertEqual(self.store.control(), before_control)
        self.assertEqual(self.store.change_output([job["id"]], "central"), 1)
        repaired = self.store.get(job["id"])
        self.assertEqual((repaired["status"], repaired["error_code"], repaired["error"]),
                         ("error", "unsupported_codec", message))
        self.assertEqual((repaired["source"], repaired["identity"]), (job["source"], job["identity"]))
        self.assertEqual(repaired["output_root"], str(self.store.root / "wyniki"))
        self.assertEqual(json.loads(repaired["source_meta"])["probe_error_code"], "unsupported_codec")
        self.assertNotIn("output_error", json.loads(repaired["source_meta"]))
        self.assertEqual(self.store.control(), before_control)
        self.assertEqual(digest(source), original_hash)

    def test_no_audio_remains_primary_when_the_output_root_also_requires_repair(self):
        source = self.long_source("silent.wav", b"silent deep recording")
        before_control = self.store.control()
        message = "Plik nie zawiera ścieżki audio. Wybierz nagranie z dźwiękiem."
        with patch("batch.media.media_metadata", side_effect=MediaError(message)):
            result = import_local(self.store, [source], config={}, output_mode="beside")
        job = self.store.jobs()[0]
        metadata = json.loads(job["source_meta"])
        self.assertEqual((job["status"], job["error_code"], job["error"]), ("no_audio", "no_audio", message))
        self.assertEqual(metadata["probe_error_code"], "no_audio")
        self.assertIn("output_error", metadata)
        self.assertEqual(result["file_problem_counts"], {"no_audio": 1, "output_path": 1})
        self.assertEqual(self.store.retry([job["id"]]), 0)
        self.assertEqual(self.store.get(job["id"]), job)
        self.assertEqual(self.store.change_output([job["id"]], "central"), 1)
        repaired = self.store.get(job["id"])
        self.assertEqual((repaired["status"], repaired["error_code"], repaired["error"]),
                         ("no_audio", "no_audio", message))
        self.assertEqual(self.store.retry([job["id"]]), 0)
        self.assertEqual(self.store.control(), before_control)

    def test_duplicate_from_deep_root_preserves_entire_completed_row(self):
        first = self.source("first/lesson.wav", b"same completed recording")
        with patch("batch.media.media_metadata", return_value=MEDIA_METADATA):
            import_local(self.store, [first], config={}, output_mode="central")
        job = self.store.jobs()[0]
        self.store.update(job["id"], status="done", progress=100, processed_until=1, attempts=3, error="saved note")
        before = self.store.get(job["id"])
        before_control = self.store.control()
        duplicate = self.long_source("copy.wav", first.read_bytes())
        with patch("batch.media.media_metadata", return_value=MEDIA_METADATA):
            result = import_local(self.store, [duplicate], config={}, output_mode="beside")
        self.assertEqual((result["files"], result["added"], result["matched_jobs"], result["existing_jobs"]), (1, 0, 1, 1))
        self.assertEqual(result["job_statuses"], ["done"])
        self.assertEqual(result["job_problem_counts"], {})
        self.assertEqual(self.store.get(job["id"]), before)
        self.assertEqual(self.store.control(), before_control)

    def test_scanner_reports_ts_permission_error_and_continues_other_files(self):
        blocked = self.source("blocked.ts", b"export const value = 1;\n")
        good = self.source("good.wav")
        original = Path.open

        def inaccessible(path, *args, **kwargs):
            if path == blocked:
                raise PermissionError(errno.EACCES, "denied", str(path))
            return original(path, *args, **kwargs)

        with patch.object(Path, "open", inaccessible):
            result = scan_media(self.source_root, MEDIA)
        self.assertEqual(result, [good])
        self.assertEqual(result.exclusions, [])
        self.assertEqual(len(result.warnings), 1)
        self.assertEqual((result.warnings[0]["path"], result.warnings[0]["phase"], result.warnings[0]["code"]),
                         (str(blocked), "scan", "file_access"))

    def test_real_mpeg_ts_remains_discoverable_even_with_a_technical_filename(self):
        executable = shutil.which("ffmpeg")
        if not executable and Path(r"C:\ffmpeg\bin\ffmpeg.exe").is_file():
            executable = r"C:\ffmpeg\bin\ffmpeg.exe"
        if not executable:
            self.skipTest("FFmpeg required for a real MPEG-TS fixture")
        source = self.source_root / "compiler_depend.ts"
        result = subprocess.run(
            [executable, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
             "sine=frequency=440:sample_rate=48000:duration=0.5", "-c:a", "mp2", "-f", "mpegts", str(source)],
            capture_output=True, timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))
        found = scan_media(self.source_root, MEDIA)
        self.assertEqual(found, [source.resolve()])
        self.assertEqual(found.exclusions, [])
        self.assertEqual(found.warnings, [])


if __name__ == "__main__":
    unittest.main()
