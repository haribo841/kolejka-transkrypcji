"""Destination checks must fail promptly after source folders are moved."""
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

from batch.readiness import check_destination


class DestinationAccessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()

    def tearDown(self):
        self.temporary.cleanup()

    def test_missing_destination_cannot_fall_back_to_drive_root(self):
        target = self.root / "moved-source" / "recording-results"
        anchor = Path(target.anchor)
        with patch("batch.readiness.local_path", return_value=target), \
                patch.object(Path, "exists", autospec=True, side_effect=lambda path: path == anchor), \
                patch("batch.readiness.check_disk") as disk, \
                patch("batch.readiness.os.open") as write_probe:
            with self.assertRaisesRegex(ValueError, "Sprawdź lokalizację nagrania"):
                check_destination(target)
        disk.assert_not_called()
        write_probe.assert_not_called()

    def test_denied_write_is_attempted_once_and_preserves_original_error(self):
        denied = PermissionError("Access is denied")
        with patch("batch.readiness.check_disk"), \
                patch("batch.readiness.os.open", side_effect=denied) as write_probe:
            with self.assertRaisesRegex(OSError, "Access is denied") as caught:
                check_destination(self.root)
        write_probe.assert_called_once()
        self.assertIs(caught.exception.__cause__, denied)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_successful_probe_uses_exclusive_create_and_leaves_no_files(self):
        target = self.root / "new" / "recording-results"
        with patch("batch.readiness.check_disk") as disk, \
                patch("batch.readiness.os.open", wraps=os.open) as write_probe:
            check_destination(target, reserve_gb=0)
        disk.assert_called_once_with(self.root, reserve_gb=0)
        write_probe.assert_called_once()
        self.assertEqual(write_probe.call_args.args[0].parent, self.root)
        self.assertEqual(write_probe.call_args.args[1], os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertFalse(target.exists())

    def test_explicit_existing_drive_root_remains_an_allowed_destination(self):
        anchor = Path(self.root.anchor)
        with patch("batch.readiness.local_path", return_value=anchor), \
                patch.object(Path, "exists", return_value=True), \
                patch.object(Path, "is_dir", return_value=True), \
                patch("batch.readiness.check_disk"), \
                patch("batch.readiness.os.open", side_effect=PermissionError("Denied")) as write_probe:
            with self.assertRaisesRegex(OSError, "Denied"):
                check_destination(anchor)
        write_probe.assert_called_once()

    def test_failed_write_removes_the_owned_probe(self):
        with patch("batch.readiness.check_disk"), \
                patch("batch.readiness.os.write", side_effect=OSError("Cannot write")), \
                patch("batch.readiness.os.open", wraps=os.open) as write_probe:
            with self.assertRaisesRegex(OSError, "Cannot write"):
                check_destination(self.root)
        write_probe.assert_called_once()
        self.assertEqual(list(self.root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
