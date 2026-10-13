"""Session controls and browser selection stay explicit without exposing credentials."""
import json
from pathlib import Path
import tempfile
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

from batch.browser_profiles import chromium_profiles, firefox_profiles, open_profile
from batch.common import WorkerLock, atomic_json, defaults
from batch.gui import Window
from batch.readiness import youtube_test_matches
from batch.youtube_access import DEFAULTS, access_fingerprint

URL = "https://www.youtube.com/watch?v=abcdefghij0"
READY = {"components": {key: {"status": "Próba zaliczona", "message": "Ready fixture"}
                        for key in ("whisper", "tools", "diarization", "uvr", "youtube")},
         "passed": True, "test": {"at": "test"}}


class ProfileTests(unittest.TestCase):
    def test_firefox_profile_metadata_is_enough_and_no_cookies_are_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Profiles/main").mkdir(parents=True)
            (root / "profiles.ini").write_text("[Profile0]\nName=Main\nPath=Profiles/main\nIsRelative=1\nDefault=1\n", encoding="utf-8")
            with patch("yt_dlp.cookies.extract_cookies_from_browser") as cookies:
                records = firefox_profiles(root, "firefox.exe")
            cookies.assert_not_called()
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["profile"], str((root / "Profiles/main").resolve()))
            self.assertTrue(records[0]["preferred"])

    def test_chromium_detects_last_profile_and_rejects_escape_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("Default", "Profile 1"):
                (root / name).mkdir()
            (root / "Local State").write_text(json.dumps({"profile": {"last_used": "Profile 1", "info_cache": {
                "Default": {"name": "Personal"}, "Profile 1": {"name": "Lectures"}, "../escape": {"name": "invalid"}}}}))
            records = chromium_profiles("chrome", root, "chrome.exe")
            self.assertEqual(len(records), 2)
            preferred = next(record for record in records if record["preferred"])
            self.assertEqual(Path(preferred["profile"]).name, "Profile 1")

    def test_broken_chromium_metadata_falls_back_to_existing_folders(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Default").mkdir()
            (root / "Local State").write_text('{"profile": null}')
            self.assertEqual(len(chromium_profiles("edge", root, "msedge.exe")), 1)

    def test_browser_launch_uses_the_selected_profile_with_no_shell(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            browser = root / "chrome.exe"
            browser.write_bytes(b"test executable")
            folder = root / "User Data/Profile 1"
            folder.mkdir(parents=True)
            record = {"browser": "chrome", "executable": str(browser), "profile": str(folder)}
            with patch("batch.browser_profiles.subprocess.Popen") as launch:
                open_profile(record, URL)
            command = launch.call_args.args[0]
            self.assertIn("--profile-directory=Profile 1", command)
            self.assertIn("--user-data-dir=" + str(folder.parent), command)
            self.assertEqual(command[-1], URL)
            self.assertFalse(launch.call_args.kwargs["shell"])


class SessionAccessUITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        atomic_json(self.root / "ustawienia.json", defaults())
        self.profile_path = self.root / "browser-profile"
        self.profile_path.mkdir()
        self.record = {"mode": "browser", "browser": "firefox", "profile": str(self.profile_path),
                       "executable": str(self.root / "firefox.exe"), "label": "Firefox | Lectures", "preferred": True}
        self.readiness = patch("batch.gui.collect_readiness", return_value=READY)
        self.profiles = patch("batch.youtube_dialog.discover_profiles", return_value=[self.record])
        self.readiness.start()
        self.profiles.start()
        managed = patch("batch.youtube_dialog.managed_profiles", return_value=[])
        managed.start()
        self.addCleanup(managed.stop)
        self.addCleanup(self.readiness.stop)
        self.addCleanup(self.profiles.stop)
        self.tk = tk.Tk()
        self.tk.withdraw()
        self.app = Window(self.root, tk_root=self.tk)
        self.addCleanup(self.app.close)
        self.app.store.add_many([{"identity": "local:" + "a" * 64, "kind": "local", "title": "Lecture",
                                 "source": str(self.root / "lecture.mkv"), "duration": 20}])
        self.app.jobs = {job["id"]: job for job in self.app.store.jobs()}
        self.app.readiness = READY
        self.app.update_session_controls(alive=False)

    def test_pending_local_jobs_have_a_visible_enabled_start_after_import(self):
        button = self.app.local_tab.controls["start"]
        self.app.busy_import = True
        self.app.update_session_controls(alive=False)
        self.assertTrue(button.instate(["disabled"]))
        self.app.handle_event("finished", None, self.app.local_tab.note)
        self.assertTrue(button.instate(["!disabled", "!pressed"]))
        self.assertEqual(button.cget("background"), "#1465a3")

    def test_start_latches_immediately_and_repeated_clicks_cannot_launch_again(self):
        process = Mock()
        process.poll.return_value = None
        button = self.app.local_tab.controls["start"]
        with patch("batch.gui.preflight"), patch("batch.gui.check_destination"), patch("batch.gui.launch", return_value=process) as launch:
            button.invoke()
            self.assertTrue(button.instate(["disabled", "pressed"]))
            self.assertEqual(button.cget("relief"), "sunken")
            self.assertEqual(button.cget("text"), "Uruchamianie...")
            button.invoke()
            self.app.start(kind="local")
            launch.assert_called_once()

    def test_running_and_finish_then_stop_remain_latched_until_worker_is_idle(self):
        button = self.app.local_tab.controls["start"]
        lock = WorkerLock(self.root)
        self.assertTrue(lock.acquire())
        try:
            self.app.store.control(state="running", scope_kind="local", stop=0)
            self.app.update_session_controls()
            self.assertEqual(button.cget("text"), "Sesja trwa")
            self.app.stop(kind="local")
            self.assertTrue(button.instate(["pressed", "disabled"]))
            self.assertIn("zatrzymam", button.cget("text"))
            self.app.store.control(state="idle", stop=1)
        finally:
            lock.close()
        self.app.update_session_controls()
        self.assertTrue(button.instate(["!pressed", "!disabled"]))

    def test_failed_preflight_restores_start_and_shows_the_reason(self):
        failure = {"components": {"whisper": {"status": "Wymaga działania", "message": "Missing model"}}, "passed": False}
        self.readiness.return_value = failure
        with patch("batch.gui.collect_readiness", return_value=failure), patch("batch.gui.launch") as launch:
            self.app.local_tab.controls["start"].invoke()
            launch.assert_not_called()
        self.assertIn("Missing model", self.app.local_tab.note.get())
        self.assertTrue(self.app.local_tab.controls["start"].instate(["!pressed", "!disabled"]))

    def test_probe_lock_does_not_look_like_a_running_local_session(self):
        self.app.update_session_controls(control={**self.app.store.control(), "scope_kind": "local"}, alive=True)
        self.assertTrue(self.app.local_tab.controls["start"].instate(["disabled", "!pressed"]))

    def dialog(self):
        self.app.configure_youtube()
        self.app.youtube_dialog.dialog.withdraw()
        self.app.youtube_dialog.url.set(URL)
        return self.app.youtube_dialog

    def test_login_automatically_chooses_and_opens_the_same_discovered_profile(self):
        with patch("yt_dlp.cookies.extract_cookies_from_browser") as cookies, patch("batch.youtube_dialog.open_profile") as open_browser:
            dialog = self.dialog()
            dialog.open_video()
            open_browser.assert_called_once_with(self.record, URL)
            self.assertEqual(dialog.mode.get(), "browser")
            self.assertEqual(dialog.profile.get(), str(self.profile_path))
            cookies.assert_not_called()
        self.assertFalse((self.root / "youtube-dostep.json").exists())

    def test_login_prefers_the_program_browser_without_exporting_cookies(self):
        record = {"mode": "managed", "browser": "chrome", "executable": "chrome.exe",
                  "profile": str(self.root / "youtube-browser-chrome"), "label": "Chrome | Own window"}
        with patch("batch.youtube_dialog.managed_profiles", return_value=[record]), \
                patch("batch.youtube_dialog.open_managed") as open_browser, \
                patch("batch.youtube_dialog.open_profile") as regular:
            dialog = self.dialog()
            dialog.open_video()
        open_browser.assert_called_once_with(record, URL)
        regular.assert_not_called()
        self.assertEqual(dialog.mode.get(), "managed")
        self.assertEqual(dialog.profile.get(), record["profile"])
        self.assertFalse(Path(record["profile"]).exists())

    def test_saved_program_browser_settings_refresh_without_disabling_local_start(self):
        atomic_json(self.root / "youtube-dostep.json", {**DEFAULTS, "mode": "managed", "browser": "chrome",
                    "profile": str(self.root / "youtube-browser-chrome")})
        self.app.refresh_models()
        self.app.update_session_controls(alive=False)
        self.assertIn("własne okno chrome", self.app.youtube_state.get())
        self.assertTrue(self.app.local_tab.controls["start"].instate(["!disabled", "!pressed"]))

    def test_matching_success_can_close_without_starting_a_queue_or_storing_cookies(self):
        dialog = self.dialog()
        dialog.connection_choice.set(self.record["label"])
        dialog.choose_connection()
        values = dialog.values()
        dialog.show_probe_result({"url": URL, "access": values}, {"ok": True, "message": "Link ready"})
        self.assertFalse(dialog.use_button.instate(["disabled"]))
        report = json.loads((self.root / "youtube-proba.json").read_text())
        self.assertEqual(set(report), {"ok", "at", "access_signature", "video_id"})
        before = self.app.store.control()
        with patch("batch.gui.launch") as launch:
            dialog.use_connection()
            launch.assert_not_called()
        self.assertTrue(dialog.closed)
        self.assertEqual(before, self.app.store.control())

    def test_changed_fields_cannot_confirm_an_old_success(self):
        dialog = self.dialog()
        tested = {"url": URL, "access": dialog.values()}
        dialog.show_probe_result(tested, {"ok": True, "message": "Ready"})
        dialog.delay.set("25")
        dialog.use_connection()
        self.assertFalse(dialog.closed)
        self.assertIn("Zmieniono ustawienia", dialog.result.get())

    def test_invalid_changed_fields_do_not_close_or_raise_after_a_success(self):
        dialog = self.dialog()
        dialog.show_probe_result({"url": URL, "access": dialog.values()}, {"ok": True, "message": "Ready"})
        dialog.delay.set("invalid")
        dialog.use_connection()
        self.assertFalse(dialog.closed)
        self.assertTrue(dialog.use_button.instate(["disabled"]))

    def test_file_changes_invalidate_youtube_probe_without_reading_its_contents(self):
        path = self.root / "cookies.txt"
        path.write_text("secret session contents")
        access = {**DEFAULTS, "mode": "file", "cookie_file": str(path)}
        atomic_json(self.root / "youtube-dostep.json", access)
        atomic_json(self.root / "youtube-proba.json", {"ok": True, "access_signature": access_fingerprint(access)})
        with patch("batch.youtube_access.file_cookies") as cookie_reader:
            self.assertTrue(youtube_test_matches(self.root))
            path.write_text("different session contents")
            self.assertFalse(youtube_test_matches(self.root))
            cookie_reader.assert_not_called()


if __name__ == "__main__":
    unittest.main()
