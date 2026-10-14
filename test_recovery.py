"""Regression checks for continuous queues and sample based vocal recovery."""
import errno
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf

from batch.asr import ASR, checkpoint
from batch.common import ResourceError, atomic_json, blocks, defaults, digest, job_folder, signature
from batch.exporter import export, verify_outputs
from batch.failures import JobError
from batch.local_outputs import NAME_SURROGATE, ScanResult, linked_path, scan_media
from batch.store import Store
from batch.uvr_child import request_paths
from batch.uvr_recovery import VocalRecovery
from batch.uvr_timeline import UVR_RATE, decode_clock, prepare_clock_block
from batch.worker import Worker


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = defaults()
        self.config.update(min_free_gb=0, diarization=False, keep_audio=False)


class ContinuousQueueTests(Fixture):
    def jobs(self, count=4):
        store = Store(self.root)
        store.add_many([{"identity": "local:" + str(index) * 64, "kind": "local", "title": str(index),
                         "source": str(self.root / f"{index}.wav"), "date": f"2020-01-{index + 1:02}", "duration": 20}
                        for index in range(count)])
        return store

    def test_success_file_error_partial_and_next_are_processed_once(self):
        store = self.jobs()
        seen = []
        def process(job):
            seen.append(job["id"])
            if job["title"] == "1":
                raise JobError("Broken file", "bad_file")
            if job["title"] == "2":
                store.record_failure(job["id"], "UVR tail", "uvr_truncated", partial=True, processed_until=7)
            else:
                store.update(job["id"], status="done")
        store.start(kind="local")
        Worker(self.root, processor=process).run(check=False)
        self.assertEqual([j["status"] for j in store.jobs()], ["done", "error", "partial", "done"])
        self.assertEqual(len(seen), 4)
        self.assertEqual(store.control()["state"], "idle")
        self.assertTrue((job_folder(self.root, store.jobs()[1]) / "robocze/blad.txt").is_file())
        store.start(kind="local")
        self.assertEqual(store.jobs()[2]["stage"], "Oczekuje na wznowienie")
        Worker(self.root, processor=process).run(check=False)
        self.assertEqual(len(seen), 6)
        self.assertEqual(seen.count(store.jobs()[2]["id"]), 2)
        self.assertEqual(store.jobs()[2]["processed_until"], 7)
        self.assertEqual(len(json.loads(store.jobs()[2]["error_history"])), 2)

    def test_stop_after_partial_does_not_claim_next(self):
        store = self.jobs(2)
        def process(job):
            store.control(stop=1)
            store.record_failure(job["id"], "tail", "uvr_truncated", partial=True, processed_until=8)
        store.start(kind="local")
        Worker(self.root, processor=process).run(check=False)
        self.assertEqual([j["status"] for j in store.jobs()], ["partial", "pending"])

    def test_retry_clicked_mid_session_never_reprocesses_an_attempted_partial(self):
        store = self.jobs(2)
        seen = []
        def process(job):
            seen.append(job["id"])
            if job["title"] == "0":
                store.record_failure(job["id"], "tail", "uvr_truncated", partial=True, processed_until=8)
                store.retry([job["id"]])
            else:
                store.update(job["id"], status="done")
        store.start(kind="local")
        Worker(self.root, processor=process).run(check=False)
        self.assertEqual(seen, [job["id"] for job in store.jobs()])
        self.assertEqual([j["status"] for j in store.jobs()], ["pending", "done"])

    def test_changing_audio_track_resets_only_position_and_keeps_history(self):
        store = self.jobs(1)
        ident = store.jobs()[0]["id"]
        store.record_failure(ident, "tail", "uvr_truncated", partial=True, processed_until=8)
        store.configure([ident], audio_track=1)
        job = store.get(ident)
        self.assertEqual((job["audio_track"], job["processed_until"], job["status"]), (1, 0, "pending"))
        self.assertEqual(len(json.loads(job["error_history"])), 1)

    def test_shared_resource_failure_stops_but_bad_file_does_not(self):
        for name, error in (("disk", OSError(errno.ENOSPC, "full")), ("memory", MemoryError()),
                            ("model", ResourceError("missing model"))):
            with self.subTest(resource=name):
                root = self.root / name
                store = Store(root)
                store.add_many([{"identity": f"yt:abcdefghij{i}", "kind": "youtube", "title": str(i), "source": "offline"}
                                for i in range(2)])
                store.start()
                def process(job):
                    raise error
                Worker(root, processor=process).run(check=False)
                self.assertEqual([j["status"] for j in store.jobs()], ["error", "pending"])
                self.assertEqual(store.control()["stop"], 1)

    def test_selected_scope_resumes_partial_without_enabling_other_jobs(self):
        store = self.jobs(3)
        first, second, third = store.jobs()
        store.record_failure(second["id"], "tail", "uvr_truncated", partial=True, processed_until=5)
        store.start(kind="local", job_ids=[second["id"]])
        Worker(self.root, processor=lambda job: store.update(job["id"], status="done")).run(check=False)
        self.assertEqual([j["status"] for j in store.jobs()], ["pending", "done", "pending"])
        self.assertEqual(store.get(first["id"])["enabled"], store.get(third["id"])["enabled"])

    def test_retry_retains_error_history_and_clears_stale_stage(self):
        store = self.jobs(1)
        ident = store.jobs()[0]["id"]
        store.record_failure(ident, "truncated", "uvr_truncated", partial=True, processed_until=4)
        store.retry([ident])
        job = store.get(ident)
        self.assertEqual((job["status"], job["stage"], job["error"]), ("pending", "Oczekuje na wznowienie", "truncated"))
        self.assertEqual(len(json.loads(job["error_history"])), 1)
        store.update(ident, stage="Wymaga działania")
        self.assertEqual(Store(self.root).get(ident)["stage"], "Oczekuje na wznowienie")


class RecoveryTests(Fixture):
    def setUp(self):
        super().setUp()
        self.source = self.root / "original.wav"
        samples = np.sin(np.arange(30 * UVR_RATE, dtype=np.float32) * .04) * .1
        sf.write(self.source, np.column_stack((samples, samples)), UVR_RATE, subtype="FLOAT")
        self.original_hash = digest(self.source)
        self.work = self.root / "result/robocze"
        self.work.mkdir(parents=True)
        self.config.update(uvr_seconds=10, uvr_context=1)
        self.request = {"source": str(self.source), "work": str(self.work),
                        "output": str(self.work.parent / "audio/wokal.flac"), "track": 0,
                        "config": self.config, "signature": "fixture"}
        self.calls = []
        self.api = SimpleNamespace(request_paths=request_paths, emit=lambda *args: None,
                                   separator=lambda *args: SimpleNamespace(torch_device="cpu"),
                                   separate_vocal=self.separate)
        self.effects = []
        self.version = patch("batch.uvr_recovery.importlib.metadata.version", return_value="fixture")
        self.version.start()
        self.addCleanup(self.version.stop)

    def separate(self, engine, config, stage, source):
        samples, rate = sf.read(source, dtype="float32", always_2d=True)
        self.calls.append(len(samples))
        effect = self.effects.pop(0) if self.effects else None
        if isinstance(effect, Exception):
            raise effect
        if isinstance(effect, int):
            samples = samples[:effect * UVR_RATE]
        path = stage / "generated.wav"
        sf.write(path, samples, rate, subtype="FLOAT")
        return engine, path

    def test_shortened_vocal_has_one_recovery_pass_then_partial_and_next_start_appends(self):
        self.effects = [6, JobError("tail failed", "uvr_test")]
        result = VocalRecovery(self.request, self.api).run()
        self.assertFalse(result["complete"])
        self.assertEqual(result["processed_until"], 5)
        self.assertEqual(result["duration"], 30)
        self.assertEqual(len(self.calls), 2)
        first = self.work / "uvr" / result["blocks"][0]["file"]
        before = first.read_bytes()
        old_partial = Path(result["output"])
        old_hash = digest(old_partial)
        resumed = VocalRecovery({**self.request, "recovery_only": True}, self.api).run()
        self.assertTrue(resumed["complete"])
        self.assertEqual(resumed["processed_until"], 30)
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(first.read_bytes(), before)
        self.assertEqual(digest(old_partial), old_hash)
        self.assertEqual(digest(self.source), self.original_hash)
        actual, _ = sf.read(resumed["output"], always_2d=True)
        expected, _ = sf.read(self.source, always_2d=True)
        np.testing.assert_allclose(actual, expected, atol=2e-7)

    def test_retry_failure_retains_new_prefix_and_does_not_loop_in_same_session(self):
        self.effects = [6, 8]
        result = VocalRecovery(self.request, self.api).run()
        self.assertFalse(result["complete"])
        self.assertEqual(result["processed_until"], 11)
        self.assertEqual(len(self.calls), 2)
        self.effects = [5]
        next_result = VocalRecovery(self.request, self.api).run()
        self.assertFalse(next_result["complete"])
        self.assertEqual(next_result["processed_until"], 14)
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(sf.info(next_result["output"]).frames, 14 * UVR_RATE)

    def test_no_valid_audio_remains_error_after_only_one_extra_attempt(self):
        self.effects = [0, 0]
        with self.assertRaisesRegex(JobError, "UVR skrócił"):
            VocalRecovery(self.request, self.api).run()
        self.assertEqual(len(self.calls), 2)
        self.assertFalse((self.work / "uvr-result.json").exists())

    def test_resource_failure_never_becomes_partial_or_retries(self):
        self.effects = [MemoryError("out of memory")]
        with self.assertRaises(MemoryError):
            VocalRecovery(self.request, self.api).run()
        self.assertEqual(len(self.calls), 1)

    def test_unusable_shared_uvr_model_stops_without_file_recovery(self):
        with patch.object(self.api, "separator", side_effect=RuntimeError("invalid model")):
            with self.assertRaisesRegex(ResourceError, "modelu UVR"):
                VocalRecovery(self.request, self.api).run()
        self.assertEqual(self.calls, [])

    def test_recovery_uses_sixty_second_cores_with_original_overlap(self):
        data = np.full((145 * UVR_RATE, 2), .1, dtype=np.float32)
        sf.write(self.source, data, UVR_RATE, subtype="FLOAT")
        self.effects = [6]
        result = VocalRecovery(self.request, self.api).run()
        self.assertTrue(result["complete"])
        self.assertEqual(self.calls, [11 * UVR_RATE, 62 * UVR_RATE, 62 * UVR_RATE, 21 * UVR_RATE])
        self.assertEqual([(p["start_frame"] / UVR_RATE, p["end_frame"] / UVR_RATE) for p in result["blocks"]],
                         [(0, 5), (5, 65), (65, 125), (125, 145)])

    def test_legacy_blocks_are_preserved_and_only_missing_tail_is_separated(self):
        directory = self.work / "uvr"
        directory.mkdir()
        samples, rate = sf.read(self.source, dtype="float32", always_2d=True)
        for block in list(blocks(30, 10, 1))[:2]:
            path = directory / f"{block['index']:06}.flac"
            sf.write(path, samples[int(block["core_start"] * rate):int(block["core_end"] * rate)], rate, subtype="PCM_24")
            checkpoint(path.with_suffix(".json"), {"signature": signature({"request": "fixture", "block": block}),
                                                  "sha256": digest(path), "block": block})
        before = (directory / "000000.flac").read_bytes()
        result = VocalRecovery(self.request, self.api).run()
        self.assertTrue(result["complete"])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(result["blocks"][0]["file"], "000000.flac")
        self.assertEqual((directory / "000000.flac").read_bytes(), before)

    def test_corrupt_checkpoint_recomputes_only_invalid_suffix(self):
        result = VocalRecovery(self.request, self.api).run()
        first = self.work / "uvr" / result["blocks"][0]["file"]
        before = first.read_bytes()
        (self.work / "uvr" / result["blocks"][1]["file"]).write_bytes(b"broken")
        self.calls.clear()
        result = VocalRecovery(self.request, self.api).run()
        self.assertTrue(result["complete"])
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(first.read_bytes(), before)


class PartialExportTests(Fixture):
    def report(self):
        return {"identity": "local:fixture", "title": "Partial", "duration": 100, "processed_until": 12,
                "incomplete_reason": {"code": "uvr_truncated", "message": "tail"},
                "segments": [{"start": 1, "end": 2, "speaker": "Mówca 1", "text": "hello"}]}

    def test_partial_exports_are_verified_without_full_completion_marker(self):
        paths = export(self.root, self.report(), "version", complete=False)
        self.assertTrue(verify_outputs(self.root, "local:fixture", partial=True))
        self.assertFalse(verify_outputs(self.root, "local:fixture"))
        self.assertFalse((self.root / "gotowe.json").exists())
        self.assertTrue(all("czesciowa" in path.name for path in paths.values()))
        self.assertIn("WYNIK CZĘŚCIOWY", paths["txt"].read_text(encoding="utf-8"))
        report = json.loads(paths["json"].read_text(encoding="utf-8"))
        self.assertEqual((report["complete"], report["processed_until"], report["duration"]), (False, 12, 100))
        before = {name: path.read_bytes() for name, path in paths.items()}
        export(self.root, self.report(), "full")
        self.assertTrue(verify_outputs(self.root, "local:fixture"))
        self.assertEqual(before, {name: path.read_bytes() for name, path in paths.items()})

    def test_failed_reexport_keeps_the_previous_valid_partial(self):
        export(self.root, self.report(), "v1", complete=False)
        with patch("batch.exporter.atomic_new_bytes", side_effect=OSError("interrupted")):
            with self.assertRaises(OSError):
                export(self.root, self.report(), "v2", complete=False)
        self.assertTrue(verify_outputs(self.root, "local:fixture", "v1", partial=True))

    def test_partial_completion_in_worker_keeps_audio_and_durable_resume_position(self):
        source = self.root / "original.wav"
        source.write_bytes(b"original")
        atomic_json(self.root / "ustawienia.json", self.config)
        store = Store(self.root)
        store.add_many([{"identity": "local:" + digest(source), "kind": "local", "source": str(source), "title": "Partial"}])
        job = store.jobs()[0]
        folder = job_folder(self.root, job)
        work = folder / "robocze"
        work.mkdir(parents=True)
        pcm = work / "audio.f32le"
        pcm.write_bytes(b"saved pcm")
        report = {**self.report(), "identity": job["identity"], "uvr": {"failure": {"message": "tail", "code": "uvr_truncated"}}}
        Worker(self.root).finish(job, folder, work, report, "version", False)
        self.assertEqual(store.get(job["id"])["status"], "partial")
        self.assertEqual(store.get(job["id"])["processed_until"], 12)
        self.assertTrue(pcm.is_file())
        self.assertEqual(source.read_bytes(), b"original")
        self.assertTrue(verify_outputs(folder, job["identity"], partial=True))

    def test_legacy_full_exports_without_complete_field_remain_valid(self):
        paths = export(self.root, self.report(), "v1")
        report = json.loads(paths["json"].read_text(encoding="utf-8"))
        report.pop("complete")
        atomic_json(paths["json"], report)
        marker = json.loads((self.root / "gotowe.json").read_text(encoding="utf-8"))
        marker["files"][paths["json"].name] = digest(paths["json"])
        atomic_json(self.root / "gotowe.json", marker)
        self.assertTrue(verify_outputs(self.root, "local:fixture"))


class BlockCacheTests(Fixture):
    def test_appending_tail_reuses_prefix_and_recomputes_only_changed_context(self):
        self.config.update(asr_seconds=10, asr_context=1)
        pcm = self.root / "audio.f32le"
        np.ones(25 * 16000, dtype="<f4").tofile(pcm)
        engine = ASR(self.config, self.root)
        calls = []
        class Pipeline:
            def get_generation_config(self):
                return SimpleNamespace(lang_to_id={})
            def generate(self, samples, **kwargs):
                calls.append(len(samples))
                return SimpleNamespace(texts=[" word"], words=[SimpleNamespace(start_ts=1, end_ts=2, word=" word")], chunks=[])
        engine.pipe = Pipeline()
        engine.run(pcm, 25, "old-pcm", {"language": "auto"}, self.root, lambda *args: None)
        before = (self.root / "asr/000000.json").read_bytes()
        np.ones(45 * 16000, dtype="<f4").tofile(pcm)
        engine.run(pcm, 45, "new-pcm", {"language": "auto"}, self.root, lambda *args: None)
        self.assertEqual(len(calls), 6)
        self.assertEqual((self.root / "asr/000000.json").read_bytes(), before)

    def test_diarization_prefix_cache_survives_a_different_whole_pcm_key(self):
        from batch.diarize_child import run
        self.config.update(diar_seconds=10, diar_context=1)
        pcm = self.root / "audio.f32le"
        np.ones(25 * 16000, dtype="<f4").tofile(pcm)
        request = {"pcm": str(pcm), "duration": 25, "pcm_key": "old", "work": str(self.root),
                   "config": self.config, "signature": "request"}
        calls = []
        class Engine:
            def __init__(self, request):
                self.config, self.work = request["config"], Path(request["work"])
                self.base, self.legacy_base = "block-model", request["pcm_key"]
            def block_result(self, block, key, count):
                calls.append(block["index"])
                return {"signature": key, "block": block, "turns": [], "exclusive": [], "embeddings": {}}
        with patch("batch.diarize_child.Diarizer", Engine), patch("batch.diarize_child.emit"):
            run(request)
            before = (self.root / "diar/000000.json").read_bytes()
            np.ones(45 * 16000, dtype="<f4").tofile(pcm)
            run({**request, "duration": 45, "pcm_key": "new"})
        self.assertEqual(calls, [0, 1, 2, 2, 3, 4])
        self.assertEqual((self.root / "diar/000000.json").read_bytes(), before)


class RealTimelineTests(Fixture):
    def setUp(self):
        super().setUp()
        if not Path(self.config["ffmpeg"]).is_file() or not Path(self.config["ffprobe"]).is_file():
            self.skipTest("Requires FFmpeg and FFprobe")

    def video(self, name, *, video_start=0, audio_start=0, audio_duration=1, codec="pcm_f32le"):
        source = self.root / name
        subprocess.run([self.config["ffmpeg"], "-nostdin", "-v", "error", "-y",
                        "-itsoffset", str(video_start), "-f", "lavfi", "-i", "color=size=32x32:rate=25:duration=3",
                        "-itsoffset", str(audio_start), "-f", "lavfi", "-i", f"sine=frequency=440:duration={audio_duration}",
                        "-map", "0:v:0", "-map", "1:a:0", "-c:v", "mpeg4", "-c:a", codec,
                        "-copyts", "-avoid_negative_ts", "disabled", str(source)], check=True, capture_output=True)
        return source

    def test_audio_offset_and_silence_tail_follow_video_clock_without_stretching(self):
        source = self.video("offset.mkv", video_start=2, audio_start=3)
        directory = self.root / "clock"
        directory.mkdir()
        pcm, clock = decode_clock(source, self.config, 0, directory, lambda *args: None)
        self.assertEqual(clock["origin"], 2)
        self.assertEqual(clock["audio_start"], 3)
        self.assertEqual(clock["duration"], 3)
        stage = directory / "stage"
        stage.mkdir()
        prepared = prepare_clock_block(pcm, clock, stage, {"start": 0, "end": 3})
        samples, rate = sf.read(prepared, always_2d=True)
        self.assertEqual(rate, UVR_RATE)
        np.testing.assert_array_equal(samples[:UVR_RATE - 100], 0)
        self.assertGreater(float(np.max(np.abs(samples[UVR_RATE + 100:2 * UVR_RATE - 100]))), .01)
        np.testing.assert_array_equal(samples[2 * UVR_RATE + 100:], 0)
        from batch.media import decode
        mono_work = self.root / "mono"
        mono_work.mkdir()
        mono, duration, _ = decode(source, mono_work, self.config, 0, lambda *args: None)
        samples = np.fromfile(mono, dtype="<f4")
        self.assertEqual(duration, 3)
        np.testing.assert_array_equal(samples[:15900], 0)
        self.assertGreater(float(np.max(np.abs(samples[16100:31900]))), .01)
        np.testing.assert_array_equal(samples[32100:], 0)

    def test_avi_audio_ends_more_than_quarter_second_before_video(self):
        source = self.video("tail.avi", audio_duration=2.7, codec="libmp3lame")
        directory = self.root / "clock"
        directory.mkdir()
        pcm, clock = decode_clock(source, self.config, 0, directory, lambda *args: None)
        self.assertGreater(clock["duration"] - clock["audio_frames"] / UVR_RATE, .25)
        samples = np.fromfile(pcm, dtype="<f4").reshape(-1, 2)
        self.assertEqual(len(samples), clock["audio_frames"])
        stage = directory / "stage"
        stage.mkdir()
        prepared = prepare_clock_block(pcm, clock, stage, {"start": 0, "end": clock["duration"]})
        actual, _ = sf.read(prepared, always_2d=True)
        self.assertEqual(len(actual), clock["frames"])
        np.testing.assert_array_equal(actual[clock["audio_frames"]:], 0)
        self.assertGreater(float(np.max(np.abs(actual[:clock["audio_frames"]]))), .01)


class RecursiveScanTests(Fixture):
    def test_linux_reparse_directory_is_skipped_before_reading_owner(self):
        good = self.root / "good"
        good.mkdir()
        source = good / "clip.mkv"
        source.write_bytes(b"recording")
        bad = self.root / "lib64"
        bad.mkdir()
        original = Path.lstat
        def metadata(path, *args, **kwargs):
            if path == bad.resolve():
                return SimpleNamespace(st_mode=0o40777, st_reparse_tag=0xA000001D)
            return original(path, *args, **kwargs)
        from batch.local_outputs import owned_folder
        def guarded_owner(path):
            self.assertNotEqual(path, bad.resolve(), "A Linux link must never be traversed")
            return owned_folder(path)
        with patch.object(Path, "lstat", metadata), patch("batch.local_outputs.owned_folder", side_effect=guarded_owner):
            result = scan_media(self.root, {".mkv"})
        self.assertEqual(result, [source.resolve()])

    def test_cloud_placeholder_is_not_treated_as_directory_link(self):
        with patch.object(Path, "lstat", return_value=SimpleNamespace(st_mode=0o40777, st_reparse_tag=0x9000001A)):
            self.assertFalse(linked_path(self.root))
        self.assertTrue(0xA000001D & NAME_SURROGATE)

    def test_owner_marker_access_error_is_reported_and_other_folders_continue(self):
        bad = self.root / "bad"
        bad.mkdir()
        good = self.root / "good"
        good.mkdir()
        source = good / "clip.avi"
        source.write_bytes(b"recording")
        original = Path.is_file
        def inaccessible(path, *args, **kwargs):
            if path.parent == bad.resolve() and path.name == ".kolejka-nagranie.json":
                error = OSError(errno.EIO, "[WinError 1920] The file cannot be accessed by the system", str(path))
                raise error
            return original(path, *args, **kwargs)
        with patch.object(Path, "is_file", inaccessible):
            result = scan_media(self.root, {".avi"})
        self.assertEqual(result, [source.resolve()])
        self.assertEqual(len(result.warnings), 1)
        self.assertEqual(result.warnings[0]["path"], str(bad.resolve()))
        self.assertIn("1920", result.warnings[0]["message"])

    def test_inaccessible_recording_is_skipped_and_hashable_recording_imports(self):
        from batch.importers import import_local, local_checksum
        bad = self.root / "bad.wav"
        good = self.root / "good.m4a"
        bad.write_bytes(b"blocked")
        good.write_bytes(b"recording")
        def inaccessible(store, path):
            if path.name == bad.name:
                raise PermissionError(errno.EACCES, "blocked", str(path))
            return local_checksum(store, path)
        store = Store(self.root / "registry")
        with patch("batch.importers.local_checksum", side_effect=inaccessible):
            result = import_local(store, [bad, good])
        self.assertEqual((result["files"], result["added"], result["errors"], result["skipped"]), (2, 1, 1, 1))
        self.assertEqual(store.jobs()[0]["source"], str(good.resolve()))
        self.assertIn("bad.wav", result["scan_warnings"][0]["path"])

    def test_all_levels_skip_owned_output_and_linked_directories(self):
        deep = self.root / "one/two/three"
        deep.mkdir(parents=True)
        source = deep / "clip.mkv"
        source.write_bytes(b"source")
        output = self.root / "output"
        output.mkdir()
        atomic_json(output / ".kolejka-nagranie.json", {"identity": "local:owned"})
        (output / "vocal.wav").write_bytes(b"output")
        result = scan_media(self.root, {".mkv", ".wav"})
        self.assertEqual(result, [source.resolve()])
        self.assertEqual(scan_media(self.root, {".mkv"}, False), [])
        with patch("batch.local_outputs.linked_path", side_effect=lambda p: p.name == "one"):
            self.assertEqual(scan_media(self.root, {".mkv"}), [])

    def test_inaccessible_directory_is_reported(self):
        def failed_walk(root, **kwargs):
            kwargs["onerror"](PermissionError(errno.EACCES, "blocked", str(root / "private")))
            yield str(root), [], []
        with patch("batch.local_outputs.os.walk", side_effect=failed_walk):
            result = scan_media(self.root, {".mkv"})
        self.assertIsInstance(result, ScanResult)
        self.assertEqual(len(result.warnings), 1)
        self.assertIn("private", result.warnings[0]["path"])

    def test_large_scan_reports_progress_before_import(self):
        def many_folders(root, **kwargs):
            for _ in range(500):
                yield str(root), [], []
        messages = []
        with patch("batch.local_outputs.os.walk", side_effect=many_folders):
            result = scan_media(self.root, {".mkv"}, progress=messages.append)
        self.assertEqual(result.directories, 500)
        self.assertEqual(len(messages), 2)
        self.assertIn("sprawdzono 500 katalogów", messages[0])
        self.assertIn("Wyszukiwanie zakończone", messages[1])


if __name__ == "__main__":
    unittest.main()
