"""Moved recordings retain their IDs, checkpoints, errors and processing state."""
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from batch.common import WorkerLock, digest, job_folder
from batch.relocation import relocate_local
from batch.result_paths import reserve_output
from batch.store import Store
from batch.worker import Worker


class RelocationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.old = self.base / "old"
        self.new = self.base / "new"
        self.old.mkdir()
        self.store = Store(self.base / "registry")

    def recording(self, title="recording", status="pending"):
        source = self.old / (title + ".mkv")
        source.write_bytes(title.encode())
        self.store.add_many([{"identity": "local:" + digest(source), "kind": "local", "title": title,
                             "source": str(source), "output_root": str(self.old), "status": status}])
        return next(job for job in self.store.jobs() if job["title"] == title)

    def entry(self, job):
        return {"id": job["id"], "old_source": job["source"], "new_source": str(self.new / Path(job["source"]).name),
                "old_output_root": job["output_root"], "new_output_root": str(self.new)}

    def test_moved_completed_and_partial_jobs_keep_their_state_and_work(self):
        for status in ("done", "partial"):
            job = self.recording(status, status)
            self.store.update(job["id"], attempts=2, processed_until=13, progress=50, error="historical failure")
            folder = job_folder(self.store.root, job)
            reserve_output(folder, job["identity"])
            (folder / "checkpoint.json").write_text("checkpoint", encoding="utf-8")
        before = self.store.jobs()
        shutil.move(self.old, self.new)
        self.assertEqual(relocate_local(self.store, [self.entry(job) for job in before]), 2)
        after = self.store.jobs()
        for previous, current in zip(before, after):
            for key in previous.keys() - {"source", "output_root", "updated"}:
                self.assertEqual(current[key], previous[key])
            self.assertEqual((job_folder(self.store.root, current) / "checkpoint.json").read_text(), "checkpoint")
        self.assertFalse(self.old.exists())
        self.assertEqual(self.store.control()["stop"], 1)

    def test_bad_source_or_foreign_output_rolls_back_all_jobs(self):
        first, second = self.recording("first"), self.recording("second")
        shutil.move(self.old, self.new)
        before = self.store.jobs()
        (self.new / "second.mkv").write_bytes(b"wrong source")
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            relocate_local(self.store, [self.entry(first), self.entry(second)])
        self.assertEqual(self.store.jobs(), before)
        (self.new / "second.mkv").write_bytes(b"second")
        destination = self.new / second["folder"]
        reserve_output(destination, "local:another")
        with self.assertRaises(FileExistsError):
            relocate_local(self.store, [self.entry(first), self.entry(second)])
        self.assertEqual(self.store.jobs(), before)

    def test_missing_checkpoints_are_not_silently_replaced(self):
        job = self.recording()
        self.store.update(job["id"], attempts=1, processed_until=15)
        shutil.move(self.old, self.new)
        with self.assertRaisesRegex(ValueError, "punktów wznowienia"):
            relocate_local(self.store, [self.entry(job)])

    def test_running_session_and_stale_plan_are_rejected(self):
        job = self.recording()
        shutil.move(self.old, self.new)
        lock = WorkerLock(self.store.root)
        self.assertTrue(lock.acquire())
        try:
            with self.assertRaisesRegex(ValueError, "zatrzymaj"):
                relocate_local(self.store, [self.entry(job)])
        finally:
            lock.close()
        entry = {**self.entry(job), "old_source": "stale path"}
        with self.assertRaisesRegex(ValueError, "po przygotowaniu"):
            relocate_local(self.store, [entry])

    def test_missing_source_does_not_recreate_old_tree_and_logs_in_registry(self):
        job = self.recording()
        shutil.move(self.old, self.new)
        worker = Worker(self.store.root)
        worker.current = job
        with patch("batch.worker.reserve_output") as reserve, patch("batch.worker.check_disk") as disk:
            worker.process_one(job)
        reserve.assert_not_called()
        disk.assert_not_called()
        self.assertFalse(self.old.exists())
        self.assertEqual(self.store.get(job["id"])["status"], "error")
        self.assertTrue((self.store.root / "bledy" / f"nagranie-{job['id']}.txt").is_file())

    def test_foreign_result_folder_keeps_its_log_untouched(self):
        job = self.recording()
        folder = job_folder(self.store.root, job)
        reserve_output(folder, "local:another")
        work = folder / "robocze"
        work.mkdir()
        log = work / "blad.txt"
        log.write_text("another job's log", encoding="utf-8")
        worker = Worker(self.store.root)
        worker.save_error_log(job, "failure for this recording")
        self.assertEqual(log.read_text(encoding="utf-8"), "another job's log")
        self.assertTrue((self.store.root / "bledy" / f"nagranie-{job['id']}.txt").is_file())


if __name__ == "__main__":
    unittest.main()
