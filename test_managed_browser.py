"""Test an explicitly selected program browser with fabricated session data only."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from batch.common import atomic_json
from batch.managed_browser import (OWNER_FILE, browser_address, local_json, managed_profiles,
                                  open_managed, page_socket, read_cookies)
from batch.readiness import check_youtube, readiness_summary
from batch.youtube_access import DEFAULTS, SessionError, session_cookies, youtube_client

URL = "https://www.youtube.com/watch?v=abcdefghijk"
COOKIE = "FabricatedOfflineSession"


class ManagedBrowserTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.directory = self.root / "youtube-browser-chrome"
        self.executable = self.root / "chrome.exe"
        self.executable.write_bytes(b"Test fixture, never executed")
        self.record = {"mode": "managed", "browser": "chrome", "profile": str(self.directory),
                       "executable": str(self.executable)}
        self.access = {**DEFAULTS, "mode": "managed", "browser": "chrome", "profile": str(self.directory)}
        self.state = {"schema": 1, "browser": "chrome", "port": 12345, "browser_path": "/devtools/browser/fixture"}
        self.version = {"webSocketDebuggerUrl": "ws://127.0.0.1:12345/devtools/browser/fixture"}
        self.page = {"type": "page", "url": URL, "webSocketDebuggerUrl": "ws://127.0.0.1:12345/devtools/page/fixture"}

    def register(self):
        atomic_json(self.directory / OWNER_FILE, self.state)

    def test_detection_does_not_create_a_profile_or_read_a_session(self):
        with patch("batch.managed_browser.find_browser", return_value=str(self.executable)), \
                patch("batch.managed_browser.read_cookies") as cookies:
            records = managed_profiles(self.root)
        self.assertEqual(len(records), 2)
        self.assertFalse(self.directory.exists())
        cookies.assert_not_called()

    def test_explicit_login_creates_only_the_owned_profile_and_uses_loopback(self):
        with patch("batch.managed_browser.subprocess.Popen") as launch:
            open_managed(self.record, URL)
        command = launch.call_args.args[0]
        state = json.loads((self.directory / OWNER_FILE).read_text())
        self.assertGreater(state["port"], 0)
        self.assertIn("--remote-debugging-address=127.0.0.1", command)
        self.assertIn("--user-data-dir=" + str(self.directory), command)
        self.assertNotIn("--disable-web-security", command)
        self.assertFalse(launch.call_args.kwargs["shell"])
        self.assertEqual(command[-1], URL)

    def test_reopening_uses_the_same_port_instead_of_losing_the_session(self):
        self.register()
        with patch("batch.managed_browser.subprocess.Popen") as launch:
            open_managed(self.record, URL)
            open_managed(self.record, URL)
        self.assertEqual(launch.call_count, 2)
        self.assertIn("--remote-debugging-port=12345", launch.call_args.args[0])

    def test_regular_profiles_cannot_be_used_as_program_debugging_profiles(self):
        folder = self.root / "Chrome/User Data"
        folder.mkdir(parents=True)
        with patch("batch.managed_browser.subprocess.Popen") as launch, self.assertRaises(ValueError):
            open_managed({**self.record, "profile": str(folder)}, URL)
        launch.assert_not_called()
        self.assertFalse((folder / OWNER_FILE).exists())

    def test_unregistered_nonempty_folder_is_preserved(self):
        self.directory.mkdir()
        other = self.directory / "unrelated.txt"
        other.write_text("preserved")
        with patch("batch.managed_browser.subprocess.Popen") as launch, self.assertRaises(ValueError):
            open_managed(self.record, URL)
        launch.assert_not_called()
        self.assertEqual(other.read_text(), "preserved")

    def test_only_the_registered_local_youtube_page_is_selected_without_writing(self):
        self.register()
        marker = (self.directory / OWNER_FILE).read_bytes()
        with patch("batch.managed_browser.local_json", side_effect=[self.version, [self.page]]):
            self.assertEqual(page_socket(self.directory), self.page["webSocketDebuggerUrl"])
        self.assertEqual((self.directory / OWNER_FILE).read_bytes(), marker)

    def test_external_or_replaced_browser_endpoints_are_rejected(self):
        for address in ("ws://example.com:12345/devtools/browser/fixture",
                        "ws://127.0.0.1:12345/devtools/browser/another",
                        "ws://127.0.0.1:12346/devtools/browser/fixture"):
            with self.subTest(address=address), patch("batch.managed_browser.local_json", return_value={"webSocketDebuggerUrl": address}):
                with self.assertRaises(ValueError):
                    browser_address(self.state)
        with patch("batch.managed_browser.build_opener") as opener, self.assertRaises(ValueError):
            local_json("https://example.com/json/version")
        opener.assert_not_called()

    def test_login_page_is_not_accepted_until_a_youtube_video_is_open(self):
        self.register()
        page = {**self.page, "url": "https://accounts.google.com/"}
        with patch("batch.managed_browser.local_json", side_effect=[self.version, [page]]), self.assertRaises(ValueError):
            page_socket(self.directory)

    def test_node_response_is_held_in_memory_and_errors_do_not_expose_values(self):
        result = Mock(returncode=0, stdout=json.dumps([{"domain": ".youtube.com", "value": COOKIE}]))
        with patch("batch.managed_browser.page_socket", return_value=self.page["webSocketDebuggerUrl"]), \
                patch("batch.managed_browser.media_tool", return_value="node.exe"), \
                patch("batch.managed_browser.subprocess.run", return_value=result) as execute:
            cookies = read_cookies(self.directory, "node.exe")
            self.assertEqual(cookies[0]["value"], COOKIE)
            self.assertNotIn(COOKIE, repr(execute.call_args))
            result.returncode = 1
            result.stderr = COOKIE
            with self.assertRaises(SessionError) as caught:
                read_cookies(self.directory, "node.exe")
        self.assertNotIn(COOKIE, str(caught.exception))
        self.assertFalse(self.directory.exists())

    def test_cookie_jar_filters_domains_and_expiry_and_is_cleared_after_use(self):
        item = {"domain": ".youtube.com", "name": "SAPISID", "value": COOKIE, "expires": -1,
                "path": "/", "secure": True, "httpOnly": True}
        rows = [item, {**item, "domain": "example.com"}, {**item, "name": "OLD", "expires": 1},
                {**item, "domain": ".youtube.com.evil.test"}]
        with patch("batch.managed_browser.read_cookies", return_value=rows) as cookies:
            with youtube_client({"js_runtimes": {"node": {"path": "node.exe"}}}, self.access) as client:
                jar = client.cookiejar
                self.assertEqual([cookie.name for cookie in jar], ["SAPISID"])
                self.assertIn(COOKIE, jar.get_cookie_header(URL))
                self.assertIsNone(jar.get_cookie_header("https://example.com/"))
            cookies.assert_called_once_with(str(self.directory), "node.exe")
        self.assertEqual(list(jar), [])
        self.assertFalse(self.directory.exists())

    def test_malformed_or_empty_session_fails_without_disclosing_values(self):
        for rows in ([], [{"domain": ".youtube.com", "expires": COOKIE}]):
            with self.subTest(rows=bool(rows)), patch("batch.managed_browser.read_cookies", return_value=rows):
                with self.assertRaises(SessionError) as caught:
                    session_cookies(self.access, node="node.exe")
                self.assertNotIn(COOKIE, str(caught.exception))

    def test_closed_program_browser_is_a_clear_youtube_error_and_does_not_block_local(self):
        atomic_json(self.root / "youtube-dostep.json", self.access)
        with patch("batch.readiness.media_tool"), patch("batch.managed_browser.page_socket", side_effect=ValueError("Closed")):
            with self.assertRaisesRegex(ValueError, "Otwórz własne okno") as caught:
                check_youtube({"node": "node.exe"}, self.root)
        components = {key: {"status": "Próba zaliczona", "message": "Ready"}
                      for key in ("whisper", "tools", "diarization", "uvr")}
        components["youtube"] = {"status": "Wymaga działania", "message": str(caught.exception)}
        report = {"components": components, "passed": True, "test": {"at": "test"}}
        self.assertTrue(readiness_summary(report, "local")[0])
        self.assertFalse(readiness_summary(report, "youtube")[0])


if __name__ == "__main__":
    unittest.main()
