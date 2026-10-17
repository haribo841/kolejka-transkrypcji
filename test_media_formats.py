"""Real FFmpeg inputs exercise the same decoding used by ASR and UVR."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from batch.common import NO_WINDOW, atomic_json, defaults, digest
from batch.importers import import_local
from batch.media import MediaError, decode, media_metadata
from batch.setup_model import smoke_test
from batch.store import Store
from batch.uvr import archive_source
from batch.uvr_child import prepare_input


class MediaFormatTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = defaults()
        cls.config.update(min_free_gb=0, diarization=False, uvr_enabled=False)
        for name in ("ffmpeg", "ffprobe"):
            command = str(cls.config[name])
            command = shutil.which(command) or command
            if not Path(command).is_file():
                raise unittest.SkipTest("FFmpeg / FFprobe required for integration tests")
            cls.config[name] = command
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.base = Path(cls.temp.name) / "nagrania łódź"
        cls.base.mkdir()
        cls.inputs = {}
        for extension, codec in (("wav", "pcm_s16le"), ("mp3", "libmp3lame"), ("m4a", "aac"),
                                 ("aac", "aac"), ("flac", "flac"), ("ogg", "libvorbis"), ("opus", "libopus")):
            path = cls.base / ("źródło." + extension)
            cls.ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1",
                       "-c:a", codec, str(path))
            cls.inputs[extension] = path
        for extension, video, audio in (("mp4", "libx264", "aac"), ("mkv", "ffv1", "aac"),
                                       ("webm", "libvpx-vp9", "libopus"), ("mov", "libx264", "aac"),
                                       ("avi", "mpeg4", "libmp3lame")):
            path = cls.base / ("źródło." + extension)
            cls.ffmpeg("-f", "lavfi", "-i", "color=size=32x32:rate=10:duration=1",
                       "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1",
                       "-map", "0:v:0", "-map", "1:a:0", "-c:v", video, "-c:a", audio,
                       "-shortest", str(path))
            cls.inputs[extension] = path

    @classmethod
    def ffmpeg(cls, *arguments):
        result = subprocess.run([cls.config["ffmpeg"], "-nostdin", "-hide_banner", "-loglevel", "error",
                                 "-y", *arguments], capture_output=True, timeout=30, creationflags=NO_WINDOW)
        if result.returncode:
            raise AssertionError(result.stderr.decode("utf-8", errors="replace"))

    def test_format_matrix_decodes_prepares_uvr_and_preserves_audio(self):
        for extension, source in self.inputs.items():
            with self.subTest(extension=extension):
                source_hash = digest(source)
                metadata = media_metadata(source, self.config)
                self.assertGreater(metadata["duration"], .8)
                self.assertEqual(len(metadata["audio_tracks"]), 1)
                folder = self.base / ("wynik-" + extension)
                work = folder / "robocze"
                work.mkdir(parents=True)
                pcm, duration, key = decode(source, work, self.config, 0, lambda *args: None)
                samples = np.fromfile(pcm, dtype="<f4")
                self.assertTrue(np.isfinite(samples).all())
                self.assertGreater(float(np.max(np.abs(samples))), .01)
                if metadata["has_video"]:
                    # Video time includes verified silence after source audio EOF.
                    self.assertAlmostEqual(duration, metadata["duration"], delta=.04)
                else:
                    self.assertAlmostEqual(duration, 1, delta=.1)
                before = pcm.stat().st_mtime_ns
                self.assertEqual(decode(source, work, self.config, 0, lambda *args: None), (pcm, duration, key))
                self.assertEqual(pcm.stat().st_mtime_ns, before)
                stage = work / "uvr"
                stage.mkdir()
                prepared = prepare_input(source, self.config, 0, stage, {"start": 0, "end": .5})
                stream = media_metadata(prepared, self.config)["audio_tracks"][0]
                self.assertEqual((stream["sample_rate"], stream["channels"]), ("44100", 2))
                archived = archive_source(source, folder, 0, self.config)
                self.assertTrue(archived.is_file())
                archived_meta = media_metadata(archived, self.config)
                self.assertFalse(archived_meta["has_video"])
                self.assertEqual(digest(source), source_hash)
                if not metadata["has_video"]:
                    self.assertEqual(digest(archived), source_hash)

    def test_multiple_tracks_select_second_audio_through_whole_preparation(self):
        source = self.base / "dwa głosy.mkv"
        self.ffmpeg("-f", "lavfi", "-i", "color=size=32x32:duration=1",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1",
                    "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=48000:duration=1",
                    "-map", "0:v", "-map", "1:a", "-map", "2:a", "-c:v", "ffv1", "-c:a", "pcm_s16le", str(source))
        self.assertEqual(len(media_metadata(source, self.config)["audio_tracks"]), 2)
        folder = self.base / "wiele-ścieżek"
        work = folder / "robocze"
        work.mkdir(parents=True)
        pcm, _, _ = decode(source, work, self.config, 1, lambda *args: None)
        samples = np.fromfile(pcm, dtype="<f4")
        peak = np.argmax(np.abs(np.fft.rfft(samples))) * 16000 / len(samples)
        self.assertAlmostEqual(peak, 880, delta=5)
        archived = archive_source(source, folder, 1, self.config)
        self.assertEqual(len(media_metadata(archived, self.config)["audio_tracks"]), 1)
        prepared = prepare_input(source, self.config, 1, work, {"start": 0, "end": .5})
        other_work = folder / "uvr-check"
        other_work.mkdir()
        pcm, _, _ = decode(prepared, other_work, self.config, 0, lambda *args: None)
        samples = np.fromfile(pcm, dtype="<f4")
        peak = np.argmax(np.abs(np.fft.rfft(samples))) * 16000 / len(samples)
        self.assertAlmostEqual(peak, 880, delta=5)
        with self.assertRaises(MediaError):
            decode(source, other_work, self.config, 9, lambda *args: None)

    def test_bad_inputs_have_individual_errors_and_do_not_block_valid_files(self):
        silent_video = self.base / "bez audio.mp4"
        self.ffmpeg("-f", "lavfi", "-i", "color=size=32x32:duration=0.5", "-an", str(silent_video))
        corrupt = self.base / "uszkodzony.mkv"
        corrupt.write_bytes(b"broken multimedia data")
        self.assertEqual(media_metadata(silent_video, self.config)["empty_audio"], "no_audio_stream")
        with self.assertRaisesRegex(MediaError, "FFprobe"):
            media_metadata(corrupt, self.config)
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp)
            summary = import_local(store, [silent_video, corrupt, self.inputs["mkv"]], config=self.config,
                                   output_mode="beside")
            self.assertEqual((summary["added"], summary["errors"]), (3, 1))
            self.assertEqual(sorted(j["status"] for j in store.jobs()), ["error", "pending", "pending"])
            self.assertTrue(all(j["error"] for j in store.jobs() if j["status"] == "error"))
            before = store.jobs()
            import_local(store, [self.inputs["mkv"]], config=self.config, output_mode="central")
            self.assertEqual(before, store.jobs())

    def test_silence_cannot_prove_that_the_model_ran(self):
        source = self.base / "cisza.wav"
        self.ffmpeg("-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono", "-t", "0.25", str(source))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            atomic_json(root / "ustawienia.json", self.config)
            with patch("batch.worker.preflight"), patch("batch.setup_model.environment_versions", return_value={}), \
                    patch("batch.setup_model.configuration_fingerprint", return_value="fixture"):
                with self.assertRaisesRegex(RuntimeError, "Whisper nie został wywołany"):
                    smoke_test(root, source, seconds=.25)
            report = json.loads((root / "pierwsza-proba.json").read_text(encoding="utf-8"))
            self.assertFalse(report["passed"])
            self.assertTrue(Path(report["results"]).is_dir())


if __name__ == "__main__":
    unittest.main()
