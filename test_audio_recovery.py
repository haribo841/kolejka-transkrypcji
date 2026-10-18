"""The tolerant decoder is bounded and cannot certify a damaged source as complete."""
from pathlib import Path
import tempfile
import unittest

from batch.audio_recovery import DIAGNOSTIC_BYTES, run_decoder
from batch.common import ResourceError
from batch.failures import JobError


class DecoderRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.log = self.directory / "timeline-ffmpeg.log"
        self.command = ["ffmpeg", "-xerror", "-copyts", "-i", "source.ts", "-map", "0:a:1", "out.f32"]

    def execute(self, outcomes):
        self.calls = []
        def run(command):
            self.calls.append(list(command))
            code, diagnostic = outcomes[len(self.calls) - 1]
            self.log.write_text(diagnostic, encoding="utf-8")
            return code
        return run

    def test_clean_decode_is_not_repeated(self):
        execute = self.execute([(0, "")])
        result = run_decoder(self.command, execute, self.log, self.fail)
        self.assertIsNone(result)
        self.assertEqual(self.calls, [self.command])
        self.assertFalse((self.directory / "timeline-ffmpeg-strict.log").exists())

    def test_one_recovery_preserves_timestamps_track_and_damage_evidence(self):
        execute = self.execute([(1, "Header missing"), (0, "Error submitting packet to decoder")])
        messages = []
        original = list(self.command)
        result = run_decoder(self.command, execute, self.log, messages.append)
        self.assertEqual(self.command, original)
        self.assertEqual(self.calls[1], [part for part in original if part != "-xerror"])
        self.assertEqual(len(messages), 1)
        self.assertTrue(result["coverage_unknown"])
        self.assertEqual(result["code"], "audio_decode_recovered")
        self.assertIn("Header missing", result["strict_error"])
        self.assertIn("Error submitting", result["recovery_error"])
        self.assertEqual((self.directory / result["strict_log"]).read_text(encoding="utf-8"), "Header missing")

    def test_failed_recovery_is_bounded_and_stays_an_error(self):
        execute = self.execute([(1, "Header missing"), (1, "Invalid data")])
        with self.assertRaises(JobError) as caught:
            run_decoder(self.command, execute, self.log, lambda message: None)
        self.assertEqual(caught.exception.code, "audio_decode_failed")
        self.assertEqual(len(self.calls), 2)
        self.assertIn("Invalid data", str(caught.exception))

    def test_resource_failure_never_retries(self):
        execute = self.execute([(1, "No space left on device")])
        with self.assertRaises(ResourceError):
            run_decoder(self.command, execute, self.log, self.fail)
        self.assertEqual(len(self.calls), 1)

    def test_resource_failure_during_recovery_stops_the_session(self):
        execute = self.execute([(1, "Header missing"), (1, "Cannot allocate memory")])
        with self.assertRaises(ResourceError):
            run_decoder(self.command, execute, self.log, lambda message: None)
        self.assertEqual(len(self.calls), 2)

    def test_diagnostic_tail_is_bounded_but_strict_log_is_preserved(self):
        diagnostic = "x" * (DIAGNOSTIC_BYTES * 3) + "corrupt packet"
        execute = self.execute([(1, diagnostic), (0, "")])
        result = run_decoder(self.command, execute, self.log, lambda message: None)
        self.assertLessEqual(len(result["strict_error"]), DIAGNOSTIC_BYTES)
        self.assertTrue(result["strict_error"].endswith("corrupt packet"))
        self.assertEqual((self.directory / result["strict_log"]).read_text(encoding="utf-8"), diagnostic)

    def test_without_strict_flag_does_not_repeat_the_same_command(self):
        execute = self.execute([(1, "Invalid data")])
        command = [part for part in self.command if part != "-xerror"]
        with self.assertRaises(JobError):
            run_decoder(command, execute, self.log, self.fail)
        self.assertEqual(len(self.calls), 1)

    def test_empty_tolerant_decode_still_has_unknown_coverage(self):
        # No samples are created here. The report cannot authorize EmptyAudio.
        execute = self.execute([(1, "corrupt input packet"), (0, "")])
        result = run_decoder(self.command, execute, self.log, lambda message: None)
        self.assertTrue(result["coverage_unknown"])
        self.assertNotIn("complete", result)


if __name__ == "__main__":
    unittest.main()
