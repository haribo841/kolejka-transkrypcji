"""Explicit ASR retries must bypass old caches and protect previous results."""
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import numpy as np

from batch.asr import ASR
from batch.common import ResourceError, atomic_json, defaults, digest, job_folder, read_json
from batch.exporter import export, verify_outputs
from batch.localization.editor import save_captions
from batch.localization.store import LocalizationStore
from batch.retranscription import (attempt_work, invalidate_captions, new_request, pending_request,
                                   retained_inputs, retry_config, retry_input, validate_request)
from batch.result_paths import output_paths
from batch.store import Store
from batch.worker import Worker, launch


IDENT = "abcdefghij0"
SRT = "1\n00:00:01,000 --> 00:00:02,000\nOld text\n"


def enqueue_retry(case):
    case.store.request_retranscription(case.job["id"], case.request)
    case.store.start("youtube", [case.job["id"]], order_ids=[case.job["id"]], scope_mode="selected")
    return case.store.claim()


class RetryCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.store = Store(self.root)
        self.store.add_many([{"identity": "yt:" + IDENT, "kind": "youtube", "title": "Retry film",
                             "source": "https://youtube.com/watch?v=" + IDENT, "duration": 5, "status": "done"}])
        self.job = self.store.jobs()[0]
        self.folder = job_folder(self.root, self.job)
        audio = self.folder / "audio"
        audio.mkdir(parents=True)
        self.source = audio / "zrodlo.webm"
        self.source.write_bytes(b"retained original")
        (audio / "wokal.flac").write_bytes(b"retained vocal")
        atomic_json(audio / "zrodlo.json", {"file": self.source.name, "sha256": digest(self.source)})
        self.report = {"identity": self.job["identity"], "title": self.job["title"], "duration": 5,
                       "processed_until": 5, "words": [], "segments": [
                           {"start": 1, "end": 2, "speaker": "Mówca 1", "text": "Old text"}],
                       "retained_audio": [str(p.relative_to(self.folder)) for p in (self.source, audio / "wokal.flac")]}
        export(self.folder, self.report, "old")
        self.old_manifest = (self.folder / "gotowe.json").read_bytes()
        self.old_paths = output_paths(self.folder)
        self.old_hashes = {name: digest(path) for name, path in self.old_paths.items()}
        self.loc = LocalizationStore(self.root)
        for lang in ("pl", "en"):
            self.loc.save_item(IDENT, "captions", lang, SRT, "old-source")
            self.loc.approve(IDENT, "captions", lang)
        self.loc.save_item(IDENT, "title", "pl", "Tytuł", "title-source")
        self.loc.approve(IDENT, "title", "pl")
        self.request = new_request(language="en")

    def tearDown(self):
        self.temp.cleanup()

    def worker_fakes(self, *, infer=None):
        def decode(source, work, *_args):
            pcm = work / "audio.f32le"
            pcm.write_bytes(np.zeros(5 * 16000, dtype="<f4").tobytes())
            return pcm, 5.0, "pcm-key"
        stack = ExitStack()
        self.addCleanup(stack.close)
        checked = stack.enter_context(patch("batch.worker.preflight"))
        stack.enter_context(patch("batch.worker.Worker.validate_audio"))
        stack.enter_context(patch("batch.worker.decode", side_effect=decode))
        asr = stack.enter_context(patch("batch.worker.ASR.run", side_effect=infer,
                                      return_value={"words": [{"start": 1.0, "end": 2.0, "text": "New text"}], "blocks": []}))
        download = stack.enter_context(patch("batch.worker.download", side_effect=AssertionError("No network for retained audio")))
        separate = stack.enter_context(patch("batch.worker.separate", side_effect=AssertionError("No new UVR")))
        return checked, asr, download, separate


class RequestTests(RetryCase):
    def test_attempt_directory_reuses_only_the_same_full_uuid(self):
        work = attempt_work(self.folder, self.request)
        self.assertEqual(attempt_work(self.folder, self.request), work)
        collision = {**self.request, "id": self.request["id"][:12] + "f" * 20}
        with self.assertRaises(ResourceError):
            attempt_work(self.folder, collision)
        self.assertEqual(read_json(work / "proba.json")["id"], self.request["id"])

    def test_migration_preserves_completed_job_and_import_is_idempotent(self):
        before = self.store.get(self.job["id"])
        with self.store.connect() as db:
            db.execute("ALTER TABLE jobs DROP COLUMN retry_request")
        self.assertEqual(Store(self.root).get(self.job["id"]), before)

    def test_normal_start_does_not_retranscribe_completed_recordings(self):
        self.store.start("youtube")
        self.assertIsNone(self.store.claim())
        self.assertEqual(self.store.get(self.job["id"])["retry_request"], "{}")

    def test_request_requeues_only_explicit_recording(self):
        self.store.add_many([{"identity": "yt:abcdefghij1", "kind": "youtube", "title": "Other", "source": "other", "status": "done"}])
        other = self.store.jobs()[1]
        job = enqueue_retry(self)
        self.assertEqual(pending_request(job), self.request)
        self.assertEqual(self.store.get(other["id"]), other)
        self.assertEqual((self.folder / "gotowe.json").read_bytes(), self.old_manifest)
        self.assertIsNone(self.store.claim(excluded={self.job["id"]}))

    def test_invalid_request_and_running_job_are_not_modified(self):
        for field, value in (("id", "../escape"), ("input", "arbitrary-path"), ("seconds", 0), ("diarization", "yes")):
            with self.assertRaises(ValueError):
                validate_request({**self.request, field: value})
        with self.assertRaises(ValueError):
            self.store.request_retranscription(self.job["id"], {**self.request, "state": "completed"})
        self.store.update(self.job["id"], status="running")
        with self.assertRaises(ValueError):
            self.store.request_retranscription(self.job["id"], self.request)

    def test_retained_source_and_vocal_are_verified_not_replaced(self):
        available = retained_inputs(self.root, self.job)
        self.assertEqual(set(available), {"original", "vocal"})
        manifest = read_json(self.folder / "gotowe.json")
        self.assertEqual(retry_input(self.root, self.job, self.request, manifest), self.source)
        vocal_request = {**self.request, "input": "vocal"}
        self.assertEqual(retry_input(self.root, self.job, vocal_request, manifest), available["vocal"])
        self.source.write_bytes(b"tampered")
        with self.assertRaises(ResourceError):
            retry_input(self.root, self.job, self.request, manifest)

    def test_config_disables_unneeded_uvr_without_mutating_defaults(self):
        config = defaults()
        before = dict(config)
        retry = retry_config(config, self.request)
        self.assertFalse(retry["uvr_enabled"])
        self.assertFalse(retry["keep_audio"])
        self.assertEqual((retry["asr_seconds"], retry["asr_context"]), (60, 5))
        self.assertEqual(config, before)


class ExecutionTests(RetryCase):
    def test_launcher_freezes_one_job_and_worker_does_not_start_next_pending_video(self):
        self.store.add_many([{"identity": "yt:abcdefghij1", "kind": "youtube", "title": "Next video", "source": "other"}])
        other = self.store.jobs()[1]
        process = Mock()
        process.poll.return_value = 0
        with patch("batch.worker.subprocess.Popen", return_value=process):
            launch(self.root, kind="youtube", job_ids=[self.job["id"]], order_ids=[self.job["id"]],
                   scope_mode="selected", retry_request=self.request)
        checked, asr, _, _ = self.worker_fakes()
        Worker(self.root).run()
        checked.assert_called_once()  # Only the retry's effective configuration.
        self.assertEqual(asr.call_count, 1)
        self.assertEqual(self.store.get(other["id"]), other)
        self.assertEqual(json.loads(self.store.control()["order_ids"]), [self.job["id"]])

    def test_local_source_retry_keeps_original_and_does_not_modify_youtube_captions(self):
        source = self.root / "local.wav"
        source.write_bytes(b"local original")
        self.store.add_many([{"identity": "local:" + digest(source), "kind": "local", "title": "Local video",
                             "source": str(source), "duration": 5, "status": "done"}])
        job = self.store.jobs()[1]
        self.store.request_retranscription(job["id"], new_request(input_kind="source", language="en"))
        captions = self.loc.items(IDENT)
        self.worker_fakes()
        Worker(self.root).process(self.store.get(job["id"]))
        self.assertEqual(self.store.get(job["id"])["status"], "done")
        self.assertEqual(source.read_bytes(), b"local original")
        self.assertEqual(self.loc.items(IDENT), captions)
        self.assertTrue(verify_outputs(job_folder(self.root, job), job["identity"]))

    def test_new_asr_versions_outputs_preserves_audio_and_invalidates_only_captions(self):
        checked, asr, download, separate = self.worker_fakes()
        title = self.loc.item(IDENT, "title", "pl")
        job = enqueue_retry(self)
        worker = Worker(self.root)
        config = worker.config
        worker.process(job)
        checked.assert_called_once()
        self.assertEqual(checked.call_args.kwargs["kind"], "local")
        self.assertFalse(checked.call_args.args[0]["uvr_enabled"])
        self.assertIs(worker.config, config)
        asr.assert_called_once()
        self.assertEqual(asr.call_args.args[3]["language"], "en")
        self.assertNotEqual(asr.call_args.args[4], self.folder / "robocze")
        download.assert_not_called()
        separate.assert_not_called()
        self.assertTrue(verify_outputs(self.folder, job["identity"]))
        self.assertNotEqual(output_paths(self.folder), self.old_paths)
        self.assertEqual({name: digest(path) for name, path in self.old_paths.items()}, self.old_hashes)
        self.assertEqual(self.source.read_bytes(), b"retained original")
        self.assertEqual(self.loc.item(IDENT, "title", "pl"), title)
        caption = self.loc.item(IDENT, "captions", "pl")
        self.assertEqual((caption["review"], caption["approval_hash"], caption["value"]), ("missing", "", ""))
        work = asr.call_args.args[4]
        self.assertEqual((work / "poprzednie-napisy.pl.srt").read_text(encoding="utf-8"), SRT)
        self.assertEqual(read_json(work / "poprzednie-wyniki.json")["signature"], "old")
        done = self.store.get(job["id"])
        self.assertEqual((done["status"], done["processed_until"]), ("done", 5))
        self.assertIsNone(pending_request(done))
        self.assertEqual(read_json(output_paths(self.folder)["json"])["retranscription"]["id"], self.request["id"])

    def test_failed_asr_preserves_current_outputs_and_caption_approvals(self):
        self.worker_fakes(infer=RuntimeError("ASR failed"))
        job = enqueue_retry(self)
        captions = self.loc.items(IDENT)
        with self.assertLogs(level="ERROR"):
            Worker(self.root).process_one(job)
        self.assertEqual((self.folder / "gotowe.json").read_bytes(), self.old_manifest)
        self.assertEqual(self.loc.items(IDENT), captions)
        self.assertTrue(verify_outputs(self.folder, job["identity"]))
        self.assertEqual(self.store.get(job["id"])["status"], "error")
        self.assertEqual(pending_request(self.store.get(job["id"])), self.request)

    def test_crash_after_export_resumes_finalization_without_repeating_asr(self):
        _, asr, _, _ = self.worker_fakes()
        job = enqueue_retry(self)
        worker = Worker(self.root)
        with patch("batch.worker.invalidate_captions", side_effect=OSError("simulated crash")):
            with self.assertRaises(OSError):
                worker.process(job)
        self.assertEqual(self.store.get(job["id"])["status"], "running")
        self.store.recover()
        recovered = self.store.claim()
        Worker(self.root).process(recovered)
        self.assertEqual(asr.call_count, 1)
        self.assertEqual(self.store.get(job["id"])["status"], "done")

    def test_completed_retry_is_not_repeated_by_next_normal_session(self):
        _, asr, _, _ = self.worker_fakes()
        Worker(self.root).process(enqueue_retry(self))
        self.store.start("youtube")
        self.assertIsNone(self.store.claim())
        self.assertEqual(asr.call_count, 1)

    def test_same_retry_resumes_checkpoints_but_new_request_gets_fresh_directory(self):
        self.worker_fakes()
        job = enqueue_retry(self)
        worker = Worker(self.root)
        worker.process(job)
        first = self.folder / ("ponow-" + self.request["id"][:12])
        self.request = new_request(language="en")
        Worker(self.root).process(enqueue_retry(self))
        second = self.folder / ("ponow-" + self.request["id"][:12])
        self.assertNotEqual(first, second)
        self.assertTrue(first.is_dir() and second.is_dir())


class CaptionRetryTests(RetryCase):
    def test_editor_cannot_save_old_captions_while_new_asr_is_queued(self):
        revision = self.loc.item(IDENT, "captions", "pl")["revision"]
        enqueue_retry(self)
        with self.assertRaisesRegex(ValueError, "Trwa ponowna transkrypcja"):
            save_captions(self.loc, IDENT, "pl", SRT, expected_revision=revision)
        self.assertEqual(self.loc.item(IDENT, "captions", "pl")["value"], SRT)

    def test_invalidation_is_idempotent_and_preserves_remote_caption_id(self):
        old = self.loc.item(IDENT, "captions", "en")
        self.loc.mark(IDENT, "captions", "en", "verified", old["content_hash"], "remote-caption-id")
        work = self.folder / "retry-test"
        work.mkdir()
        invalidate_captions(self.root, self.job, self.request, work)
        once = self.loc.items(IDENT)
        invalidate_captions(self.root, self.job, self.request, work)
        self.assertEqual(self.loc.items(IDENT), once)
        self.assertEqual(self.loc.item(IDENT, "captions", "en")["remote_id"], "remote-caption-id")
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM ytl_history WHERE kind='captions'").fetchone()[0], 2)

    def test_stale_editor_cannot_restore_old_timeline_after_new_asr(self):
        revision = self.loc.item(IDENT, "captions", "pl")["revision"]
        self.loc.invalidate_captions(IDENT, self.request["id"])
        with self.assertRaisesRegex(ValueError, "Otwórz je ponownie"):
            save_captions(self.loc, IDENT, "pl", SRT, expected_revision=revision)
        self.assertEqual(self.loc.item(IDENT, "captions", "pl")["value"], "")


class CheckpointTests(unittest.TestCase):
    def test_old_cache_is_bypassed_and_interrupted_retry_reuses_its_own_blocks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pcm = root / "audio.f32le"
            pcm.write_bytes(np.ones(16000, dtype="<f4").tobytes())
            config = retry_config(defaults(), new_request())
            asr = ASR(config, root)
            old_work, new_work = root / "robocze", root / "ponow-test"
            def infer(_samples, block, _generation, _language):
                return "Test", [], [{"start": block["start"], "end": block["end"], "text": "Test"}], False
            with patch.object(asr, "infer_block", side_effect=infer) as call, patch("batch.asr.importlib.metadata.version", return_value="test"):
                asr.run(pcm, 1, "key", {"language": "auto"}, old_work, lambda *_: None)
                old_hash = digest(old_work / "asr" / "000000.json")
                asr.run(pcm, 1, "key", {"language": "auto"}, new_work, lambda *_: None)
                asr.run(pcm, 1, "key", {"language": "auto"}, new_work, lambda *_: None)
                self.assertEqual(call.call_count, 2)
                self.assertEqual(digest(old_work / "asr" / "000000.json"), old_hash)


class DialogTests(RetryCase):
    def tk_root(self):
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        self.addCleanup(root.destroy)
        return root

    def test_completed_recording_opens_options_without_starting_automatically(self):
        from batch.retranscription_ui import INPUTS, open_retranscription
        root = self.tk_root()
        with patch("batch.worker.launch") as start:
            dialog = open_retranscription(root, self.root, [self.job["id"]])
        self.assertEqual(dialog.input.get(), INPUTS["original"])
        self.assertEqual(dialog.seconds.get(), "60")
        start.assert_not_called()

    def test_editor_exposes_retry_for_the_same_video(self):
        from batch.localization.editor import CaptionEditor
        root = self.tk_root()
        callback = Mock()
        editor = CaptionEditor(root, self.loc, IDENT, "pl", Mock(), retranscribe=callback)
        def buttons(widget):
            for child in widget.winfo_children():
                if child.winfo_class() == "TButton":
                    yield child
                yield from buttons(child)
        button = next(b for b in buttons(editor.dialog) if b.cget("text").startswith("Transkrybuj ponownie"))
        button.invoke()
        callback.assert_called_once_with(editor.dialog)

    def test_start_debounces_clicks_and_launches_exactly_one_retry(self):
        from batch.retranscription_ui import RetryTranscriptionDialog
        root = self.tk_root()
        started = Mock()
        dialog = RetryTranscriptionDialog(root, self.root, self.job, started)
        with patch("batch.retranscription_ui.threading.Thread") as thread:
            dialog.start()
            dialog.start()
        self.assertEqual(thread.call_count, 1)
        request = thread.call_args.kwargs["args"][0]
        self.assertEqual((request["input"], request["seconds"]), ("original", 60))
        with patch("batch.worker.launch") as start:
            dialog.launch(request)
        start.assert_called_once_with(self.root, kind="youtube", job_ids=[self.job["id"]],
                                      order_ids=[self.job["id"]], scope_mode="selected", retry_request=request)
        dialog.dialog.after_cancel(dialog.poll)
        dialog.tick()
        started.assert_called_once_with()
        self.assertEqual(str(dialog.start_button.cget("state")), "disabled")

    def test_busy_worker_and_multiple_selection_are_explained_before_opening_options(self):
        from batch.retranscription_ui import open_retranscription
        with patch("batch.retranscription_ui.messagebox.showinfo") as info, patch("batch.retranscription_ui.WorkerLock.busy", return_value=True):
            self.assertIsNone(open_retranscription(None, self.root, [self.job["id"]]))
            self.assertIsNone(open_retranscription(None, self.root, [self.job["id"], 999]))
        self.assertEqual(info.call_count, 2)


if __name__ == "__main__":
    unittest.main()
