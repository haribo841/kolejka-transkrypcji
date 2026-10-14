"""UVR must preserve time while distinguishing container tails from lost speech."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import numpy as np
import soundfile as sf

from batch.common import defaults
from batch.media import probe
from batch.uvr_child import UVR_RATE, prepare_input, write_vocal_block


class VocalTimingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.block = {"start": 3897, "core_start": 3900, "core_end": 3902, "end": 3902}
        self.required = 5 * UVR_RATE
        self.prepared = self.root / "input.wav"
        self.generated = self.root / "vocals.wav"
        self.stem = self.root / "stem.flac"

    def audio(self, input_frames, vocal_frames=None):
        sf.write(self.prepared, np.full((input_frames, 2), .25, dtype=np.float32), UVR_RATE, subtype="FLOAT")
        sf.write(self.generated, np.full((vocal_frames if vocal_frames is not None else input_frames, 2),
                                       .25, dtype=np.float32), UVR_RATE, subtype="FLOAT")

    def test_known_mkv_and_avi_tail_gaps_preserve_core_and_pad_only_the_end(self):
        for deficit in (2411, 4140, 12528, 44100):
            with self.subTest(deficit=deficit):
                self.audio(self.required - deficit)
                frames = write_vocal_block(self.generated, self.stem, self.block,
                                           prepared=self.prepared, final=True)
                actual, rate = sf.read(self.stem, always_2d=True)
                self.assertEqual(frames, 2 * UVR_RATE)
                self.assertEqual(len(actual), frames)
                self.assertEqual(rate, UVR_RATE)
                np.testing.assert_allclose(actual[:-deficit], .25)
                np.testing.assert_array_equal(actual[-deficit:], 0)

    def test_model_truncation_is_rejected_even_when_the_container_has_a_tail(self):
        self.audio(self.required - 4140, self.required - 4140 - 4410)
        with self.assertRaisesRegex(RuntimeError, "UVR skrócił"):
            write_vocal_block(self.generated, self.stem, self.block, prepared=self.prepared, final=True)
        self.assertFalse(self.stem.exists())

    def test_short_input_in_an_interior_block_is_not_padded_as_container_tail(self):
        self.audio(self.required - 4140)
        with self.assertRaisesRegex(RuntimeError, "Ścieżka audio jest krótsza"):
            write_vocal_block(self.generated, self.stem, self.block, prepared=self.prepared)
        self.assertFalse(self.stem.exists())

    def test_verified_container_tail_has_no_arbitrary_quarter_second_limit(self):
        self.audio(self.required - round(.3 * UVR_RATE))
        write_vocal_block(self.generated, self.stem, self.block, prepared=self.prepared, final=True)
        self.assertEqual(sf.info(self.stem).frames, 2 * UVR_RATE)

    def test_unverified_tail_still_requires_original_input(self):
        self.audio(self.required - 4140)
        with self.assertRaisesRegex(RuntimeError, "UVR skrócił"):
            write_vocal_block(self.generated, self.stem, self.block, final=True)

    def test_empty_vocal_is_rejected(self):
        self.audio(self.required, 0)
        with self.assertRaisesRegex(RuntimeError, "UVR skrócił"):
            write_vocal_block(self.generated, self.stem, self.block, prepared=self.prepared, final=True)


class RealContainerTailTests(unittest.TestCase):
    def test_avi_and_mkv_audio_end_before_video_without_losing_timestamps(self):
        config = defaults()
        if not Path(config["ffmpeg"]).is_file() or not Path(config["ffprobe"]).is_file():
            self.skipTest("Requires FFmpeg and FFprobe")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for extension, codec in (("avi", "libmp3lame"), ("mkv", "aac")):
                with self.subTest(format=extension):
                    source = root / ("video." + extension)
                    subprocess.run([config["ffmpeg"], "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                                    "-f", "lavfi", "-i", "color=size=32x32:rate=25:duration=2.12",
                                    "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                                    "-map", "0:v:0", "-map", "1:a:0", "-c:v", "mpeg4", "-c:a", codec,
                                    str(source)], check=True, capture_output=True)
                    duration = float(probe(source, config)["format"]["duration"])
                    block = {"start": 0, "core_start": 0, "core_end": duration, "end": duration}
                    stage = root / extension
                    stage.mkdir()
                    prepared = prepare_input(source, config, 0, stage, block)
                    generated = stage / "vocals.wav"
                    shutil.copyfile(prepared, generated)
                    info = sf.info(prepared)
                    self.assertGreater(duration - info.frames / UVR_RATE, .05)
                    stem = stage / "stem.flac"
                    frames = write_vocal_block(generated, stem, block, prepared=prepared, final=True)
                    # Container duration is quantized to the nearest audio sample.
                    self.assertAlmostEqual(frames / UVR_RATE, duration, delta=0.5 / UVR_RATE)
                    self.assertEqual(sf.info(stem).frames, frames)
                    samples, _ = sf.read(stem, always_2d=True)
                    self.assertGreater(float(np.max(np.abs(samples[:info.frames]))), .01)
                    np.testing.assert_array_equal(samples[info.frames:], 0)


if __name__ == "__main__":
    unittest.main()
