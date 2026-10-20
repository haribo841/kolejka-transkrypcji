"""Recovered sources finish with an honest warning, never invented silence."""
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import wave

import numpy as np
import soundfile as sf

from batch.common import atomic_json, defaults, digest, job_folder, read_json
from batch.exporter import verify_outputs
from batch.failures import JobError
from batch.media import MediaError, decode
from batch.store import Store
from batch.uvr_child import request_paths
from batch.uvr_recovery import VocalRecovery
from batch.uvr_timeline import UVR_RATE, decode_clock, prepare_clock_block
from batch.worker import Worker


RECOVERY = {"code": "audio_decode_recovered", "message": "Damaged source", "coverage_unknown": True}
KNOWN_CLOCK = {"origin": 0, "audio_start": 0, "audio_end": 1, "video_duration": 1, "has_video": True}


class DamagedMediaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = defaults()
        self.config.update(min_free_gb=0, diarization=False, keep_audio=False, uvr_enabled=False,
                           uvr_seconds=10, uvr_context=1)
        for name in ("ffmpeg", "ffprobe"):
            executable = shutil.which(name) or self.config[name]
            if not Path(executable).is_file():
                self.skipTest("Requires FFmpeg and FFprobe")
            self.config[name] = executable
        self.source = self.root / "damaged.wav"
        self.write_wave(self.source)
        # Keep an aligned 200 ms payload but retain a header declaring one second.
        with self.source.open("r+b") as stream:
            stream.truncate(44 + 6400)
        self.source_hash = digest(self.source)

    @staticmethod
    def write_wave(path, count=16000):
        with wave.open(str(path), "wb") as writer:
            writer.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            samples = (np.sin(np.arange(count) * .2) * 5000).astype("<i2")
            writer.writeframes(samples.tobytes())

    def directory(self, name):
        directory = self.root / name
        directory.mkdir()
        return directory

    def test_mono_recovers_real_corrupt_payload_without_padding_and_keeps_cache_warning(self):
        work = self.directory("mono")
        with patch("batch.uvr_timeline.clock_metadata", return_value=KNOWN_CLOCK):
            pcm, duration, key = decode(self.source, work, self.config, 0, lambda *args: None)
            report = read_json(work / "pcm.json")
            self.assertAlmostEqual(duration, .2, places=3)
            self.assertEqual(pcm.stat().st_size, 3200 * 4)
            self.assertEqual(report["source_duration"], 1)
            self.assertTrue(report["recovery"]["coverage_unknown"])
            self.assertIn("corrupt input packet", report["recovery"]["strict_error"])
            before = pcm.stat().st_mtime_ns
            with patch("batch.media.run_decoder", side_effect=AssertionError("cached decode was repeated")):
                self.assertEqual(decode(self.source, work, self.config, 0, lambda *args: None), (pcm, duration, key))
            self.assertEqual(pcm.stat().st_mtime_ns, before)
            self.assertEqual(read_json(work / "pcm.json")["recovery"], report["recovery"])
        self.assertEqual(digest(self.source), self.source_hash)

    def test_uvr_clock_recovers_real_payload_without_inventing_tail_and_keeps_cache_warning(self):
        work = self.directory("clock")
        with patch("batch.uvr_timeline.clock_metadata", return_value=KNOWN_CLOCK):
            pcm, clock = decode_clock(self.source, self.config, 0, work, lambda *args: None)
            self.assertAlmostEqual(clock["duration"], .2, places=3)
            self.assertEqual(clock["audio_frames"], clock["frames"])
            self.assertEqual(clock["source_duration"], 1)
            self.assertEqual(pcm.stat().st_size, clock["frames"] * 8)
            self.assertTrue(clock["recovery"]["coverage_unknown"])
            with patch("batch.uvr_timeline.run_decoder", side_effect=AssertionError("cached decode was repeated")):
                cached, report = decode_clock(self.source, self.config, 0, work, lambda *args: None)
            self.assertEqual(cached, pcm)
            self.assertEqual({key: value for key, value in report.items() if key != "checksum"}, clock)
        self.assertEqual(digest(self.source), self.source_hash)

    @staticmethod
    def empty_recovery(command, execute, error_file, emit):
        # The valid empty WAV really goes through FFmpeg. Force the damage
        # evidence to exercise a recovered source that yielded no samples.
        code = execute(command)
        if code:
            raise AssertionError("Empty WAV fixture should decode without an FFmpeg error")
        return dict(RECOVERY)

    def empty_wave(self):
        source = self.root / "empty.wav"
        self.write_wave(source, 0)
        return source

    def test_empty_recovered_mono_is_an_error_not_a_valid_empty_transcription(self):
        work, source = self.directory("empty-mono"), self.empty_wave()
        with patch("batch.media.run_decoder", side_effect=self.empty_recovery), \
                self.assertRaisesRegex(MediaError, "Uszkodzone źródło"):
            decode(source, work, self.config, 0, lambda *args: None)
        self.assertFalse((work / "pcm.json").exists())

    def test_empty_recovered_uvr_clock_is_an_error_not_a_valid_empty_transcription(self):
        work, source = self.directory("empty-clock"), self.empty_wave()
        with patch("batch.uvr_timeline.run_decoder", side_effect=self.empty_recovery), \
                self.assertRaisesRegex(JobError, "Uszkodzone źródło"):
            decode_clock(source, self.config, 0, work, lambda *args: None)
        self.assertFalse((work / "audio-clock.json").exists())

    def nonfinite_source(self):
        source = self.root / "nonfinite.wav"
        sf.write(source, np.full(1600, np.nan, dtype=np.float32), 16000, subtype="FLOAT")
        return source

    def test_nonfinite_mono_samples_are_rejected_before_checkpoint(self):
        work, source = self.directory("nan-mono"), self.nonfinite_source()
        with self.assertRaisesRegex(MediaError, "niefinitywne"):
            decode(source, work, self.config, 0, lambda *args: None)
        self.assertFalse((work / "pcm.json").exists())

    def test_nonfinite_uvr_samples_cannot_become_model_input(self):
        work, source = self.directory("nan-clock"), self.nonfinite_source()
        pcm, clock = decode_clock(source, self.config, 0, work, lambda *args: None)
        stage = work / "stage"
        stage.mkdir()
        with self.assertRaisesRegex(JobError, "niepoprawne próbki"):
            prepare_clock_block(pcm, clock, stage, {"start": 0, "end": clock["duration"]})
        self.assertFalse((stage / "input.wav").exists())

    def test_successful_uvr_finishes_processing_without_certifying_source_completeness(self):
        work = self.root / "result" / "robocze"
        work.mkdir(parents=True)
        request = {"source": str(self.source), "work": str(work), "track": 0, "config": self.config,
                   "output": str(work.parent / "audio" / "wokal.flac"), "signature": "damaged-fixture"}
        calls = []
        def separate(engine, config, stage, source):
            samples, rate = sf.read(source, dtype="float32", always_2d=True)
            calls.append(len(samples))
            output = stage / "generated.wav"
            sf.write(output, samples, rate, subtype="FLOAT")
            return engine, output
        api = SimpleNamespace(request_paths=request_paths, emit=lambda *args: None,
                              separator=lambda *args: SimpleNamespace(torch_device="cpu"), separate_vocal=separate)
        with patch("batch.uvr_timeline.clock_metadata", return_value=KNOWN_CLOCK), \
                patch("batch.uvr_recovery.importlib.metadata.version", return_value="fixture"):
            result = VocalRecovery(request, api).run()
        self.assertTrue(result["complete"])
        self.assertTrue(result["processing_complete"])
        self.assertFalse(result["source_complete"])
        self.assertAlmostEqual(result["duration"], 1)
        self.assertAlmostEqual(result["processed_until"], .2, places=3)
        self.assertTrue(result["timeline"]["recovery"]["coverage_unknown"])
        self.assertEqual(result["failure"], {})
        self.assertEqual(sf.info(result["output"]).frames, round(.2 * UVR_RATE))
        self.assertEqual(len(calls), 1)
        self.assertTrue(all(part["complete_block"] for part in result["blocks"]))
        self.assertEqual(digest(self.source), self.source_hash)

    def test_worker_finishes_damaged_source_with_warning_and_never_automatically_retries(self):
        for with_words in (True, False):
            with self.subTest(with_words=with_words):
                root = self.directory("worker-words" if with_words else "worker-no-words")
                good = root / "good.wav"
                self.write_wave(good, 8000)
                atomic_json(root / "ustawienia.json", self.config)
                store = Store(root)
                store.add_many([{"identity": "local:" + digest(source), "kind": "local", "source": str(source),
                                 "title": title, "date": f"2000-01-0{index + 1}"}
                                for index, (source, title) in enumerate(((self.source, "Damaged"), (good, "Good")))])
                before = {source: digest(source) for source in (self.source, good)}
                worker = Worker(root)
                words = [{"start": .02, "end": .1, "text": "hello"}] if with_words else []
                with patch.object(worker.asr, "run", return_value={"words": words, "blocks": []}) as asr:
                    store.start(kind="local")
                    worker.run(check=False)
                damaged, complete = store.jobs()
                self.assertEqual((damaged["status"], complete["status"]), ("done", "done"))
                self.assertEqual(damaged["stage"], "Gotowe: odzyskane audio")
                self.assertEqual(damaged["progress"], 100)
                self.assertEqual(damaged["error_code"], "audio_decode_recovered")
                self.assertEqual(asr.call_count, 2)
                self.assertEqual(store.control()["state"], "idle")
                folder = job_folder(root, damaged)
                self.assertTrue(verify_outputs(folder, damaged["identity"]))
                self.assertFalse(verify_outputs(folder, damaged["identity"], partial=True))
                self.assertTrue((folder / "robocze" / "audio.f32le").is_file())
                marker = read_json(folder / "gotowe.json")
                report = read_json(folder / marker["outputs"]["json"])
                self.assertTrue(report["complete"])
                self.assertFalse(report["source_complete"])
                self.assertFalse(report["retry_recommended"])
                self.assertTrue(report["coverage_unknown"])
                self.assertEqual(report["audio_recovery"]["code"], "audio_decode_recovered")
                self.assertEqual(report["words"] != [], with_words)
                with patch.object(worker.asr, "run", side_effect=AssertionError("A finished damaged source was retried")):
                    store.start(kind="local")
                    worker.run(check=False)
                self.assertTrue((folder / "robocze" / "audio.f32le").is_file())
                self.assertEqual(before, {source: digest(source) for source in before})


if __name__ == "__main__":
    unittest.main()
