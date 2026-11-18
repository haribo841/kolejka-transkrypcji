"""Offline session boundaries, resumability and login dialog regression tests."""
from __future__ import annotations

import json
import io
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import tempfile
import threading
import tkinter as tk
import unittest
from unittest.mock import MagicMock, patch

from batch.common import WorkerLock, atomic_json, defaults, digest
from batch.media import MediaError, classify_error, download, wait_before_youtube, youtube_options
from batch.store import Store
from batch.youtube_access import (DEFAULTS, SETTINGS_FILE, QuietLogger, SessionError, file_cookies,
    preferences, save_preferences, session_cookies, validate_preferences, youtube_client)
from batch.youtube_dialog import probe_process
from batch.youtube_probe import check_access

URL = "https://www.youtube.com/watch?v=abcdefghijk"
COOKIE_VALUE = "OnlyAFabricatedOfflineCookie"
COOKIE_TEXT = ("# Netscape HTTP Cookie File\n"
    f"#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t4102444800\tSAPISID\t{COOKIE_VALUE}\n"
    f".example.com\tTRUE\t/\tFALSE\t4102444800\tOTHER\t{COOKIE_VALUE}\n"
    ".youtube.com.evil.test\tTRUE\t/\tFALSE\t4102444800\tFAKE\twrong\n"
    ".youtube.com\tTRUE\t/\tTRUE\t1\tOLD\texpired\n")


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cookies = self.root / "cookies.txt"
        self.cookies.write_text(COOKIE_TEXT, encoding="utf-8")
        self.access = dict(DEFAULTS, mode="file", cookie_file=str(self.cookies))

    def tearDown(self):
        self.temp.cleanup()

    def test_cookie_logger_matches_the_library_contract_and_never_prints_sensitive_data(self):
        from yt_dlp.cookies import YDLLogger, _create_progress_bar
        logger = QuietLogger()
        self.assertIsInstance(logger, YDLLogger)
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            logger.debug(COOKIE_VALUE)
            logger.info(COOKIE_VALUE)
            logger.warning(COOKIE_VALUE, only_once=True)
            logger.error(COOKIE_VALUE)
            with _create_progress_bar(logger) as progress:
                progress.print(COOKIE_VALUE)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")

    def test_browser_extractor_receives_a_compatible_silent_logger(self):
        from yt_dlp.cookies import YDLLogger, _create_progress_bar
        def extract(profile, container, logger):
            self.assertIsInstance(logger, YDLLogger)
            self.assertEqual(profile, "offline-profile")
            self.assertIsNone(container)
            logger.warning(COOKIE_VALUE, only_once=True)
            with _create_progress_bar(logger) as progress:
                progress.print(COOKIE_VALUE)
            return file_cookies(self.cookies)
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("yt_dlp.cookies._extract_firefox_cookies", side_effect=extract), redirect_stdout(stdout), redirect_stderr(stderr):
            jar = session_cookies(dict(DEFAULTS, mode="browser", browser="firefox", profile="offline-profile"))
        self.assertEqual([cookie.name for cookie in jar], ["SAPISID"])
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")

    def test_default_never_reads_browser_or_file(self):
        with patch("yt_dlp.cookies.extract_cookies_from_browser") as browser, patch("batch.youtube_access.file_cookies") as file:
            self.assertIsNone(session_cookies(DEFAULTS))
            browser.assert_not_called()
            file.assert_not_called()
        self.assertEqual(preferences(self.root), DEFAULTS)
        self.assertFalse((self.root / SETTINGS_FILE).exists())

    def test_file_filters_domains_and_expired_entries_without_rewriting(self):
        before = digest(self.cookies)
        jar = file_cookies(self.cookies)
        self.assertEqual([c.name for c in jar], ["SAPISID"])
        self.assertIn(COOKIE_VALUE, jar.get_cookie_header("https://www.youtube.com/"))
        self.assertIsNone(jar.get_cookie_header("https://example.com/"))
        self.assertEqual(before, digest(self.cookies))

    def test_client_uses_selected_session_and_never_saves_it(self):
        before = digest(self.cookies)
        options = youtube_options(defaults(), self.access)
        with youtube_client(options, self.access) as client:
            self.assertIsNone(client.params.get("cookiefile"))
            self.assertIsNone(client.params.get("cookiesfrombrowser"))
            jar = client.cookiejar
            self.assertIn(COOKIE_VALUE, jar.get_cookie_header(URL))
            # This creates request handlers locally, without making a request.
            for handler in client._request_director.handlers.values():
                self.assertIs(handler.cookiejar, jar)
        self.assertEqual(list(jar), [])
        self.assertEqual(before, digest(self.cookies))

    def test_bad_export_error_does_not_include_cookie_values(self):
        self.cookies.write_text("# Netscape HTTP Cookie File\n" + COOKIE_VALUE, encoding="utf-8")
        with self.assertRaises(SessionError) as caught:
            session_cookies(self.access)
        self.assertNotIn(COOKIE_VALUE, str(caught.exception))

    def test_empty_or_expired_export_is_not_silently_anonymous(self):
        self.cookies.write_text("# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t1\tOLD\texpired\n", encoding="utf-8")
        with self.assertRaises(SessionError):
            session_cookies(self.access)
        self.cookies.unlink()
        with self.assertRaises(SessionError):
            session_cookies(self.access)

    def test_browser_selection_filters_other_sites_and_never_falls_back(self):
        original = file_cookies(self.cookies)
        other = next(iter(original))
        import copy
        other = copy.copy(other)
        other.domain = ".example.com"
        original.set_cookie(other)
        access = dict(DEFAULTS, mode="browser", browser="firefox", profile="chosen-profile")
        with patch("yt_dlp.cookies.extract_cookies_from_browser", return_value=original) as browser:
            jar = session_cookies(access)
            self.assertEqual([c.domain for c in jar], [".youtube.com"])
            self.assertEqual(list(original), [])
            self.assertEqual(browser.call_args.args, ("firefox",))
            self.assertEqual(browser.call_args.kwargs["profile"], "chosen-profile")
        with patch("yt_dlp.cookies.extract_cookies_from_browser", side_effect=RuntimeError(COOKIE_VALUE)):
            with self.assertRaises(SessionError) as caught:
                session_cookies(access)
        self.assertNotIn(COOKIE_VALUE, str(caught.exception))

    def test_saved_preferences_exclude_cookie_values_and_reject_bad_delays(self):
        saved = save_preferences(self.root, dict(self.access, cookies=COOKIE_VALUE, token=COOKIE_VALUE))
        self.assertEqual(saved, preferences(self.root))
        self.assertNotIn(COOKIE_VALUE, (self.root / SETTINGS_FILE).read_text(encoding="utf-8"))
        for delay in (0, -1, "NaN", "inf", 301):
            with self.assertRaises(ValueError):
                validate_preferences(dict(DEFAULTS, delay_seconds=delay))
        atomic_json(self.root / SETTINGS_FILE, {"mode": "unknown"})
        with self.assertRaises(SessionError):
            preferences(self.root)


class DownloadTests(unittest.TestCase):
    def test_cached_audio_needs_no_auth_and_no_network_delay(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            audio = work / "download.webm"
            audio.write_bytes(b"verified-existing-audio")
            atomic_json(work / "download.json", {"identity": "yt:abcdefghijk", "filename": audio.name, "sha256": digest(audio)})
            with patch("batch.media.youtube_client") as client, patch("batch.media.wait_before_youtube") as wait:
                result = download({"identity": "yt:abcdefghijk", "source": URL}, work, defaults(), lambda *_: None,
                                  dict(DEFAULTS, mode="file", cookie_file="missing.txt"))
            self.assertEqual(result, audio.resolve())
            client.assert_not_called()
            wait.assert_not_called()

    def test_blockage_stops_after_one_attempt_and_hides_arbitrary_error_data(self):
        with tempfile.TemporaryDirectory() as tmp, patch("batch.media.wait_before_youtube"), \
                patch("batch.media.youtube_client") as client:
            client.return_value.__enter__.side_effect = RuntimeError("Sign in to confirm you're not a bot " + COOKIE_VALUE)
            job, work, config = {"identity": "yt:abcdefghijk", "source": URL}, Path(tmp), defaults()
            with self.assertRaises(MediaError) as caught:
                download(job, work, config, lambda *_: None)
            self.assertEqual(caught.exception.status, "blocked")
            self.assertNotIn(COOKIE_VALUE, str(caught.exception))
            self.assertEqual(client.call_count, 1)
        self.assertEqual(classify_error("This content isn't available, try again later"), "blocked")
        self.assertEqual(classify_error("This video is not available"), "unavailable")

    def test_delay_reports_progress_and_spaces_requests(self):
        progress = MagicMock()
        with patch("batch.media.time.sleep") as sleep:
            wait_before_youtube(dict(DEFAULTS, delay_seconds=5), progress)
        self.assertEqual(sleep.call_count, 5)
        self.assertEqual(progress.call_count, 5)
        options = youtube_options(defaults(), DEFAULTS)
        self.assertEqual(options["sleep_interval_requests"], 1)
        self.assertEqual(options["concurrent_fragment_downloads"], 1)

    def test_invalid_probe_url_cannot_read_cookies_or_access_another_site(self):
        with patch("batch.youtube_probe.youtube_client") as client:
            result = check_access("https://example.com/watch?v=abcdefghijk", defaults(), DEFAULTS)
        self.assertFalse(result["ok"])
        client.assert_not_called()

    def test_probe_only_extracts_one_canonical_link_without_downloading(self):
        with patch("batch.youtube_probe.wait_before_youtube"), patch("batch.youtube_probe.youtube_client") as factory:
            client = factory.return_value.__enter__.return_value
            client.extract_info.return_value = {"formats": [{"url": "stream"}]}
            result = check_access(URL + "&list=PLnotRequested", defaults(), DEFAULTS)
            self.assertTrue(result["ok"])
            client.extract_info.assert_called_once_with(URL, download=False)
            client.process_info.assert_not_called()
            client.extract_info.side_effect = RuntimeError("Sign in to confirm you're not a bot " + COOKIE_VALUE)
            failed = check_access(URL, defaults(), DEFAULTS)
            self.assertEqual(failed["code"], "blocked")
            self.assertNotIn(COOKIE_VALUE, json.dumps(failed))

    def test_isolated_probe_releases_lock_and_never_mutates_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp)
            before = store.control()
            # Exercise the real child process, with an invalid URL to avoid networking.
            result = probe_process(tmp, {"url": "invalid", "config": defaults(), "access": DEFAULTS}, threading.Event())
            self.assertEqual(result["code"], "input")
            self.assertEqual(store.control(), before)
            self.assertFalse(WorkerLock.busy(tmp))

    def test_probe_refuses_to_compete_with_an_active_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = WorkerLock(tmp)
            self.assertTrue(lock.acquire())
            try:
                with patch("batch.youtube_dialog.subprocess.Popen") as popen:
                    result = probe_process(tmp, {"url": URL}, threading.Event())
                self.assertFalse(result["ok"])
                popen.assert_not_called()
            finally:
                lock.close()

    def test_cancelled_child_releases_worker_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            cancel = threading.Event()
            cancel.set()
            result = probe_process(tmp, {"url": "invalid", "config": defaults(), "access": DEFAULTS}, cancel)
            self.assertFalse(result["ok"])
            self.assertFalse(WorkerLock.busy(tmp))


class DialogTests(unittest.TestCase):
    def setUp(self):
        from batch.gui import Window
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.tk = tk.Tk()
        self.tk.withdraw()
        self.app = Window(self.root, tk_root=self.tk)
        self.app.configure_youtube()
        self.dialog = self.app.youtube_dialog
        self.dialog.dialog.withdraw()

    def tearDown(self):
        self.app.close()
        self.temp.cleanup()

    def test_opening_and_saving_session_choice_never_reads_cookies_or_starts_queue(self):
        before = self.app.store.control()
        with patch("yt_dlp.cookies.extract_cookies_from_browser") as extract, patch("batch.gui.launch") as launch:
            self.dialog.mode.set("browser")
            self.dialog.browser.set("Firefox")
            self.assertIsNotNone(self.dialog.save())
            self.app.configure_youtube()
            self.assertIs(self.app.youtube_dialog, self.dialog)
        extract.assert_not_called()
        launch.assert_not_called()
        self.assertEqual(self.app.store.control(), before)
        self.assertFalse(WorkerLock.busy(self.root))

    def test_invalid_test_settings_do_not_spawn_process(self):
        self.dialog.url.set(URL)
        self.dialog.mode.set("file")
        self.dialog.cookie_file.set("")
        with patch("batch.youtube_dialog.threading.Thread") as thread:
            self.dialog.test()
            thread.assert_not_called()
        self.assertFalse(self.dialog.checking)

    def test_active_worker_prevents_saving_changed_session(self):
        before = self.app.store.control()
        lock = WorkerLock(self.root)
        self.assertTrue(lock.acquire())
        try:
            self.dialog.mode.set("browser")
            self.assertIsNone(self.dialog.save())
            self.assertFalse((self.root / SETTINGS_FILE).exists())
            self.assertEqual(self.app.store.control(), before)
        finally:
            lock.close()

    def test_start_during_probe_does_not_change_queue(self):
        before = self.app.store.control()
        self.dialog.checking = True
        with patch("batch.gui.launch") as launch:
            self.app.start()
            launch.assert_not_called()
        self.assertEqual(self.app.store.control(), before)

    def test_finished_probe_does_not_launch_queue_and_marks_changed_settings(self):
        before = self.app.store.control()
        self.dialog.events.put(({"access": dict(DEFAULTS), "url": URL}, {"ok": True, "message": "Formaty dostępne"}))
        self.dialog.mode.set("browser")
        if self.dialog.after_id:
            self.dialog.dialog.after_cancel(self.dialog.after_id)
        with patch("batch.gui.launch") as launch:
            self.dialog.poll()
            launch.assert_not_called()
        self.assertIn("Zmieniono pola", self.dialog.result.get())
        self.assertEqual(self.app.store.control(), before)
        self.dialog.close()
        self.assertTrue(self.dialog.cancelled.is_set())


if __name__ == "__main__":
    unittest.main()
