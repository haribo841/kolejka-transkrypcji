"""Real decoder EOF, silence and automatic short paths keep the queue moving."""
import json
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import wave

import numpy as np

from batch.asr import ASR
from batch.common import atomic_json, defaults, digest, job_folder
from batch.exporter import verify_outputs
from batch.importers import import_local
from batch.media import EmptyAudio
from batch.output_fallback import ensure_output
from batch.result_paths import output_paths, reserve_output
from batch.store import Store
from batch.uvr import separate
from batch.uvr_timeline import decode_clock
from batch.worker import Worker
from batch.diarize_child import annotation_turns


class EmptyAndFallbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.store = Store(self.base / "queue")
        self.config = defaults()
        self.config.update(min_free_gb=0, diarization=True, uvr_enabled=False, keep_audio=True)
        for tool in ("ffmpeg", "ffprobe"):
            found = shutil.which(self.config[tool]) or self.config[tool]
            if not Path(found).is_file():
                self.skipTest("FFmpeg and FFprobe required")
            self.config[tool] = found
        atomic_json(self.store.root / "ustawienia.json", self.config)

    def wav(self, name, frames, value=0):
        source = self.base / (name + ".wav")
        with wave.open(str(source), "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(16000)
            writer.writeframes(np.full(frames, value, dtype="<i2").tobytes())
        return source

    def run_jobs(self, sources):
        originals = {path: digest(path) for path in sources}
        import_local(self.store, sources, config=self.config, output_mode="beside")
        self.store.start(kind="local")
        with patch("batch.asr.ASR.load", side_effect=AssertionError("Empty/silent inputs must not load Whisper")), \
                patch("batch.worker.run_diarization") as diar:
            Worker(self.store.root).run(check=False)
        diar.assert_not_called()
        self.assertEqual({path: digest(path) for path in sources}, originals)
        return self.store.jobs()

    def assert_empty_export(self, job):
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["progress"], 100)
        self.assertEqual(job["error"], "")
        self.assertIn("brak wypowiedzi", job["stage"])
        folder = job_folder(self.store.root, job)
        self.assertTrue(verify_outputs(folder, job["identity"]))
        paths = output_paths(folder)
        data = json.loads(paths["json"].read_text(encoding="utf-8"))
        self.assertEqual(data["words"], [])
        self.assertTrue(data["complete"])
        self.assertEqual(data["processed_until"], data["duration"])
        self.assertIn("Brak rozpoznanych wypowiedzi", paths["txt"].read_text(encoding="utf-8"))

    def test_zero_frames_zero_bytes_and_silence_are_successful_exports_and_continue(self):
        empty = self.wav("zero-frames", 0)
        zero_bytes = self.base / "zero-bytes.m4a"
        zero_bytes.touch()
        silence = self.wav("digital-silence", 16000)
        for job in self.run_jobs([empty, zero_bytes, silence]):
            self.assert_empty_export(job)
        self.assertEqual(self.store.control()["state"], "idle")

    def test_readable_video_without_audio_is_done_and_corruption_is_still_an_error(self):
        import subprocess
        video = self.base / "silent.mp4"
        subprocess.run([self.config["ffmpeg"], "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i",
                        "color=size=32x32:duration=0.1", "-an", str(video)], check=True, capture_output=True)
        corrupt = self.base / "broken.wav"
        corrupt.write_bytes(b"corrupt container")
        jobs = self.run_jobs([video, corrupt])
        silent = next(job for job in jobs if job["source"] == str(video))
        self.assert_empty_export(silent)
        self.assertGreater(silent["duration"], 0)
        failed = next(job for job in jobs if job["source"] == str(corrupt))
        self.assertEqual(failed["status"], "skipped")
        self.assertFalse(verify_outputs(job_folder(self.store.root, failed), failed["identity"]))

    def test_zero_frames_uvr_decoder_and_child_report_valid_empty_audio(self):
        source = self.wav("zero-uvr", 0)
        folder = self.base / "output"
        work = folder / "robocze"
        stage = work / "uvr"
        stage.mkdir(parents=True)
        with self.assertRaises(EmptyAudio):
            decode_clock(source, self.config, 0, stage, lambda *args: None)
        models = self.base / "models"
        models.mkdir()
        (models / self.config["uvr_model"]).write_bytes(b"unused model: no samples")
        config = {**self.config, "uvr_model_dir": str(models), "uvr_python": sys.executable}
        with patch("batch.uvr.validate_uvr"), self.assertRaises(EmptyAudio):
            separate(source, work, folder, 0, config, lambda *args: None)
        self.assertNotIn("Traceback", (work / "uvr.log").read_text(encoding="utf-8"))

    def test_short_whisper_input_is_padded_without_extending_word_timestamps(self):
        asr = ASR(self.config, self.store.root)
        pipe = Mock()
        pipe.get_generation_config.return_value = SimpleNamespace()
        pipe.generate.return_value = SimpleNamespace(texts=["word"], words=[
            SimpleNamespace(start_ts=0, end_ts=1, word="word")], chunks=[])
        asr.pipe = pipe
        samples = np.ones(160, dtype=np.float32)
        block = {"start": 0, "end": .01}
        _, _, words, silent = asr.infer_block(samples, block, {"language": None}, "auto")
        self.assertFalse(silent)
        generated = pipe.generate.call_args.args[0]
        self.assertGreater(len(generated), 200)
        np.testing.assert_array_equal(generated[:160], samples)
        self.assertEqual(words[0]["end"], .01)

    def test_short_diarization_context_cannot_create_turns_after_source_end(self):
        annotation = Mock()
        annotation.itertracks.return_value = [
            (SimpleNamespace(start=0, end=.8), None, "speaker"),
            (SimpleNamespace(start=.7, end=.9), None, "context-only")]
        turns = annotation_turns(annotation, {"start": 2, "end": 2.01})
        self.assertEqual(turns, [{"start": 2.0, "end": 2.01, "speaker": "speaker"}])

    def test_no_recognized_speech_does_not_launch_diarization(self):
        source = self.wav("non-speech", 16000, 100)
        import_local(self.store, [source], config=self.config, output_mode="beside")
        self.store.start(kind="local")
        with patch("batch.asr.ASR.run", return_value={"words": [], "blocks": []}), \
                patch("batch.worker.run_diarization") as diar:
            Worker(self.store.root).run(check=False)
        diar.assert_not_called()
        self.assert_empty_export(self.store.jobs()[0])

    def test_import_chooses_shorter_destination_and_keeps_source(self):
        deep = self.base / ("a" * 65) / ("b" * 65)
        deep.mkdir(parents=True)
        source = deep / "recording.wav"
        shutil.copy2(self.wav("short", 0), source)
        imported = import_local(self.store, [source], config=self.config, output_mode="beside")
        self.assertEqual(imported["errors"], 0)
        job = self.store.jobs()[0]
        self.assertEqual(job["status"], "pending")
        self.assertEqual(job["output_root"], str(self.store.root / "wyniki"))
        self.assertIn("output_fallback", json.loads(job["source_meta"]))
        self.store.start(kind="local")
        Worker(self.store.root).run(check=False)
        self.assert_empty_export(self.store.jobs()[0])
        self.assertTrue(source.is_file())

    def test_existing_long_destination_copies_and_verifies_checkpoints_without_removing_old(self):
        source = self.wav("existing", 0)
        deep = self.base / ("a" * 65) / ("b" * 65)
        self.store.add_many([{"identity": "local:" + digest(source), "kind": "local", "title": "Existing",
                             "source": str(source)}])
        job = self.store.jobs()[0]
        with self.store.connect() as db:
            db.execute("UPDATE jobs SET output_root=?,attempts=1 WHERE id=?", (str(deep), job["id"]))
        job = self.store.get(job["id"])
        previous = job_folder(self.store.root, job)
        reserve_output(previous, job["identity"])
        checkpoint = previous / "checkpoint.json"
        checkpoint.write_bytes(b"keep completed blocks")
        destination = ensure_output(self.store, job)
        self.assertNotEqual(destination, previous)
        self.assertEqual((destination / checkpoint.name).read_bytes(), checkpoint.read_bytes())
        self.assertEqual(ensure_output(self.store, job), destination)
        self.assertEqual(self.store.get(job["id"])["attempts"], 1)
        self.assertTrue(source.is_file())

    def test_legacy_output_path_error_is_claimed_and_completed_without_manual_destination(self):
        source = self.wav("legacy-error", 0)
        metadata = {"output_error": "Ścieżka wyników jest zbyt długa.", "intended_output_root": "old long path"}
        self.store.add_many([{"identity": "local:" + digest(source), "kind": "local", "title": "Legacy",
                             "source": str(source), "status": "output_required", "error_code": "output_path",
                             "error": metadata["output_error"], "meta": metadata}])
        before = self.store.jobs()[0]
        self.store.start(kind="local", job_ids=[before["id"]], order_ids=[before["id"]], scope_mode="visible")
        Worker(self.store.root).run(check=False)
        self.assert_empty_export(self.store.get(before["id"]))
        self.assertIn("output_fallback", json.loads(self.store.get(before["id"])["source_meta"]))
        self.assertEqual(json.loads(self.store.get(before["id"])["source_meta"])["output_fallback"]["requested"], "old long path")

    def test_foreign_destination_is_never_overwritten_by_checkpoint_copy(self):
        from batch.output_fallback import planned_output
        source = self.wav("collision", 0)
        self.store.add_many([{"identity": "local:" + digest(source), "kind": "local", "title": "Collision",
                             "source": str(source)}])
        job = self.store.jobs()[0]
        with self.store.connect() as db:
            db.execute("UPDATE jobs SET source_meta=? WHERE id=?", (json.dumps({"output_error": "long path"}), job["id"]))
        job = self.store.get(job["id"])
        previous = job_folder(self.store.root, job)
        reserve_output(previous, job["identity"])
        destination = planned_output(self.store.root, job)[2]
        reserve_output(destination, "local:foreign")
        sentinel = destination / "keep.txt"
        sentinel.write_bytes(b"foreign result")
        with self.assertRaises(FileExistsError):
            ensure_output(self.store, job)
        self.assertEqual(sentinel.read_bytes(), b"foreign result")
        self.assertEqual(self.store.get(job["id"])["folder"], job["folder"])


if __name__ == "__main__":
    unittest.main()
