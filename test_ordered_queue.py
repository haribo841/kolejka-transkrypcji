"""Immutable session ordering, legacy migration, and worker continuation."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from batch.failures import JobError
from batch.store import Store
from batch.worker import Worker, launch


class OrderedQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root)
        self.store.add_many([self.row(index) for index in range(4)])
        self.ids = [job["id"] for job in self.store.jobs()]

    def row(self, index):
        return {"identity": "local:" + f"{index:064x}", "kind": "local",
                "source": str(self.root / f"{index}.wav"), "title": str(index),
                "date": f"2020-01-{index + 1:02}"}

    def start(self, order, mode="visible"):
        self.store.start(kind="local", job_ids=sorted(set(order)), order_ids=order, scope_mode=mode)

    def drain(self):
        claimed = []
        while (job := self.store.claim()) is not None:
            claimed.append(job["id"])
            self.store.update(job["id"], status="done")
        return claimed

    def test_snapshot_order_differs_from_sorted_membership_and_deduplicates_first(self):
        order = [self.ids[2], self.ids[0], self.ids[2], self.ids[3]]
        self.start(order, "selected")
        control = self.store.control()
        self.assertEqual(json.loads(control["scope_ids"]), sorted(set(order)))
        self.assertEqual(json.loads(control["order_ids"]), [self.ids[2], self.ids[0], self.ids[3]])
        self.assertEqual(control["scope_mode"], "selected")
        self.assertEqual(self.drain(), [self.ids[2], self.ids[0], self.ids[3]])

    def test_new_import_waits_for_next_start_and_disabled_job_is_skipped(self):
        order = self.ids[::-1]
        self.start(order)
        self.store.add_many([self.row(9)])
        self.store.configure([order[1]], enabled=0)
        self.assertEqual(self.drain(), [order[0], *order[2:]])
        new = next(job for job in self.store.jobs() if job["title"] == "9")
        self.assertEqual(new["status"], "pending")
        self.assertEqual(json.loads(self.store.control()["order_ids"]), order)

    def test_success_file_error_and_partial_continue_once_in_visible_order(self):
        order = self.ids[::-1]
        self.start(order)
        seen = []
        def process(job):
            seen.append(job["id"])
            if job["id"] == order[0]:
                raise JobError("broken recording", "file_error")
            if job["id"] == order[1]:
                self.store.record_failure(job["id"], "missing tail", "uvr_partial", partial=True, processed_until=3)
                self.store.retry([job["id"]])
            else:
                self.store.update(job["id"], status="done")
        Worker(self.root, processor=process).run(check=False)
        self.assertEqual(seen, order)
        self.assertEqual([self.store.get(ident)["attempts"] for ident in order], [1] * 4)
        self.assertEqual(self.store.get(order[1])["processed_until"], 3)

    def test_stop_finishes_current_and_restart_preserves_snapshot_order(self):
        order = self.ids[::-1]
        self.start(order)
        seen = []
        def process(job):
            seen.append(job["id"])
            self.store.control(stop=1)
            self.store.update(job["id"], status="done")
        Worker(self.root, processor=process).run(check=False)
        self.assertEqual(seen, order[:1])
        self.assertEqual(json.loads(Store(self.root).control()["order_ids"]), order)
        self.store.control(stop=0)
        self.assertEqual(self.drain(), order[1:])

    def test_crash_recover_retries_claimed_job_then_snapshot_tail(self):
        order = self.ids[::-1]
        self.start(order)
        first = self.store.claim()
        self.assertEqual(first["id"], order[0])
        self.store.add_many([self.row(9)])
        seen = []
        def process(job):
            seen.append(job["id"])
            self.store.update(job["id"], status="done")
        Worker(self.root, processor=process).run(check=False)
        self.assertEqual(seen, order)
        self.assertEqual(json.loads(self.store.control()["order_ids"]), order)
        self.assertEqual(next(job for job in self.store.jobs() if job["title"] == "9")["status"], "pending")

    def test_large_snapshot_uses_json_ordinals_and_keeps_exclusions(self):
        self.store.add_many([self.row(index) for index in range(4, 5040)])
        order = [job["id"] for job in self.store.jobs()][::-1]
        self.start(order)
        self.assertEqual(self.store.claim(excluded=order[:2])["id"], order[2])

    def test_invalid_orders_modes_and_ids_leave_control_and_jobs_unchanged(self):
        for ident in self.ids:
            self.store.record_failure(ident, "tail", "uvr_partial", partial=True, processed_until=2)
        before = self.store.control(), self.store.jobs()
        cases = [([1], [], "visible"), ([1], None, "visible"), ([1], [2], "visible"),
                 ([1], [1, True], "visible"), ([1], [1, 0], "visible"), ([1], {1}, "visible"),
                 ([1], [1], "invalid"), ([1], [1], "legacy"), ([999999], [999999], "visible"),
                 ([], [], "selected")]
        for ids, order, mode in cases:
            with self.subTest(ids=ids, order=order, mode=mode), self.assertRaises(ValueError):
                self.store.start(kind="local", job_ids=ids, order_ids=order, scope_mode=mode)
            self.assertEqual((self.store.control(), self.store.jobs()), before)
        self.store.add_many([{"identity": "yt:abcdefghijk", "kind": "youtube", "title": "video", "source": "offline"}])
        youtube = next(job["id"] for job in self.store.jobs() if job["kind"] == "youtube")
        before = self.store.control(), self.store.jobs()
        with self.assertRaises(ValueError):
            self.start([youtube])
        self.assertEqual((self.store.control(), self.store.jobs()), before)

    def test_control_order_validation_preserves_first_order(self):
        before = self.store.control()
        for value in ("bad-json", "{}", "[true]", "[0]", "[-1]", "[1.5]", None):
            with self.assertRaises(ValueError):
                self.store.control(order_ids=value)
        for mode in (None, "other", 1):
            with self.assertRaises(ValueError):
                self.store.control(scope_mode=mode)
        self.assertEqual(self.store.control(), before)
        changed = self.store.control(order_ids="[3, 1, 3]")
        self.assertEqual(json.loads(changed["order_ids"]), [3, 1])

    def test_legacy_start_remains_chronological_and_clears_previous_snapshot(self):
        self.start(self.ids[::-1])
        self.store.start(kind="local")
        control = self.store.control()
        self.assertEqual((control["scope_mode"], control["order_ids"]), ("legacy", "[]"))
        self.assertEqual(self.drain(), self.ids)

    def test_migration_preserves_old_active_control_jobs_and_settings(self):
        self.store.control(state="running", stop=0, current_id=self.ids[0], scope_ids=self.ids)
        self.store.update(self.ids[0], status="running", progress=17, attempts=2)
        before_jobs = self.store.jobs()
        before_control = {key: value for key, value in self.store.control().items() if key not in {"order_ids", "scope_mode"}}
        config = self.root / "ustawienia.json"
        config.write_bytes(b'{"custom":"preserve"}')
        with closing(sqlite3.connect(self.store.path)) as db:
            db.execute("ALTER TABLE control DROP COLUMN order_ids")
            db.execute("ALTER TABLE control DROP COLUMN scope_mode")
        migrated = Store(self.root)
        control = migrated.control()
        self.assertEqual(migrated.jobs(), before_jobs)
        self.assertEqual({key: value for key, value in control.items() if key not in {"order_ids", "scope_mode"}}, before_control)
        self.assertEqual((control["scope_mode"], control["order_ids"]), ("legacy", "[]"))
        self.assertEqual(config.read_bytes(), b'{"custom":"preserve"}')

    def test_launch_passes_snapshot_to_store_before_spawning(self):
        order = self.ids[::-1]
        with patch("batch.worker.subprocess.Popen", side_effect=OSError("fixture spawn failure")):
            with self.assertRaises(OSError):
                launch(self.root, kind="local", job_ids=self.ids, order_ids=order, scope_mode="visible")
        control = self.store.control()
        self.assertEqual(json.loads(control["order_ids"]), order)
        self.assertEqual(control["scope_mode"], "visible")
        self.assertEqual(control["stop"], 1)


if __name__ == "__main__":
    unittest.main()
