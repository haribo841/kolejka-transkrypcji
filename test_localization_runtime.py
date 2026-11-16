"""Frozen sessions, OAuth boundaries, HTTP accounting and local translation safeguards."""
import json
import sys
import os
import threading
from urllib.parse import parse_qs, urlparse
import requests
from unittest.mock import Mock, patch

from batch.common import ResourceError, WorkerLock, atomic_json
from batch.localization.api import AuthError, ConflictError, QuotaError, YouTube, checked_response
from batch.localization.auth import import_client
from batch.localization.prepare import metadata_units
from batch.localization.translation import Translator, split_text
from batch.localization.worker import Runner
from test_localization import IDENT, LocalizationCase


class SessionTests(LocalizationCase):
    def test_model_preflight_failure_releases_both_worker_locks_without_advancing(self):
        self.configure_session(ids=[IDENT])
        prepare = Mock()
        with patch("batch.localization.translation.ready", side_effect=ResourceError("Model unavailable")), self.assertLogs(level="ERROR"):
            Runner(self.store, prepare=prepare).run()
        prepare.assert_not_called()
        self.assertFalse(WorkerLock.busy(self.root))
        self.assertFalse(WorkerLock.busy(self.root / "lokalizacje"))
        self.assertEqual((self.store.session()["state"], self.store.session()["cursor"]), ("paused", 0))

    def test_session_lock_is_released_if_final_database_update_fails(self):
        self.configure_session(ids=[IDENT])
        original = self.store.session
        def session(**values):
            if values.get("state") == "idle":
                raise RuntimeError("Final database update failed")
            return original(**values)
        runner = Runner(self.store, prepare=Mock(return_value=[]))
        with patch.object(self.store, "session", side_effect=session), patch("batch.localization.translation.ready"):
            with self.assertRaises(RuntimeError):
                runner.run()
        self.assertFalse(WorkerLock.busy(self.root))
        self.assertFalse(WorkerLock.busy(self.root / "lokalizacje"))

    def test_successful_selected_refresh_has_no_remaining_videos(self):
        self.configure_session(mode="sync", ids=[IDENT])
        with patch("batch.localization.worker.sync_catalog") as sync:
            Runner(self.store, api=Mock()).run()
        sync.assert_called_once()
        state = self.store.session()
        self.assertEqual(state["state"], "idle")
        self.assertEqual(state["cursor"], len(json.loads(state["ids"])))

    def test_public_sync_reads_only_frozen_selection_without_authentication_or_models(self):
        self.configure_session(mode="public_sync", ids=[IDENT])
        before = self.store.base.jobs()
        with patch("batch.localization.metadata.refresh_public") as refresh, patch("batch.localization.worker.YouTube") as api, patch("batch.localization.translation.ready") as model:
            Runner(self.store).run()
        self.assertEqual(refresh.call_args.args[1], IDENT)
        api.assert_not_called()
        model.assert_not_called()
        self.assertEqual(self.store.session()["cursor"], 1)
        self.assertEqual(self.store.base.jobs(), before)

    def test_crashed_start_is_recoverable_after_reopening(self):
        from batch.localization.worker import recover_session
        self.store.session(state="starting", stop=0, heartbeat="2020-01-01T00:00:00+00:00")
        with patch("batch.localization.worker.WorkerLock.busy", return_value=False):
            control = recover_session(self.store)
        self.assertEqual(control["state"], "paused")
        self.assertIn("Wznów", control["message"])

    def configure_session(self, mode="prepare", ids=None):
        ids = ids or [IDENT, "abcdefghij1"]
        self.store.base.add_many([{"identity": "yt:abcdefghij1", "kind": "youtube", "title": "Drugi film", "source": "url"}])
        self.store.bootstrap()
        request = {"mode": mode, "ids": ids, "tasks": ["metadata"], "frozen": {}, "generation": "current"}
        self.store.session(ids=json.dumps(ids), tasks=json.dumps(request), stop=0, cursor=0, state="starting")
        atomic_json(self.root / "lokalizacje-ustawienia.json", {"translation_python": sys.executable})
        return request

    def runner(self, prepare):
        with patch("batch.localization.translation.ready", return_value=(self.root, {})):
            Runner(self.store, prepare=prepare).run()

    def test_file_failure_continues_and_completed_session_does_not_repeat(self):
        self.configure_session()
        visited = []
        def prepare(_store, ident, _tasks, _progress):
            visited.append(ident)
            if ident == IDENT:
                raise ValueError("Bad file")
            return []
        self.runner(prepare)
        self.assertEqual(visited, [IDENT, "abcdefghij1"])
        self.runner(prepare)
        self.assertEqual(len(visited), 2)

    def test_stop_finishes_current_film_and_preserves_cursor_for_resume(self):
        self.configure_session()
        visited = []
        def prepare(store, ident, _tasks, _progress):
            visited.append(ident)
            store.session(stop=1)
            return []
        self.runner(prepare)
        self.assertEqual(visited, [IDENT])
        self.assertEqual(self.store.session()["cursor"], 1)
        self.store.session(stop=0)
        self.runner(prepare)
        self.assertEqual(visited, [IDENT, "abcdefghij1"])

    def test_shared_resource_failure_stops_without_advancing(self):
        self.configure_session()
        prepare = Mock(side_effect=ResourceError("Out of memory"))
        self.runner(prepare)
        self.assertEqual(prepare.call_count, 1)
        self.assertEqual(self.store.session()["cursor"], 0)
        self.assertEqual(self.store.session()["state"], "paused")

    def test_active_transcription_lock_blocks_localization_inference(self):
        self.configure_session()
        lock = WorkerLock(self.root)
        self.assertTrue(lock.acquire())
        try:
            prepare = Mock()
            self.runner(prepare)
            prepare.assert_not_called()
        finally:
            lock.close()

    def test_stale_child_cannot_reset_or_run_the_new_session(self):
        self.configure_session()
        before = self.store.session()
        prepare = Mock()
        Runner(self.store, prepare=prepare).run("stale")
        self.assertEqual(self.store.session(), before)
        prepare.assert_not_called()

    def test_new_import_waits_for_next_session(self):
        self.configure_session(ids=[IDENT])
        visited = []
        def prepare(store, ident, _tasks, _progress):
            visited.append(ident)
            store.base.add_many([{"identity": "yt:abcdefghij2", "kind": "youtube", "title": "Nowy", "source": "url"}])
            store.bootstrap()
            return []
        self.runner(prepare)
        self.assertEqual(visited, [IDENT])


class HttpTests(LocalizationCase):
    def oauth_client(self):
        atomic_json(self.root / "konfiguracja" / "yt-oauth-client.json", {"installed": {"client_id": "id.apps.googleusercontent.com", "client_secret": "synthetic-test-secret"}})

    def test_login_wait_can_be_cancelled_without_a_google_callback(self):
        from batch.localization.auth import LoginCancelled, connect
        self.oauth_client()
        cancelled, vault = threading.Event(), Mock()
        with patch("batch.localization.auth.token_request") as exchange:
            with self.assertRaises(LoginCancelled):
                connect(self.root, vault=vault, browser=lambda _url: cancelled.set(), cancel=cancelled, timeout=60)
        exchange.assert_not_called()
        vault.write.assert_not_called()

    def test_google_denial_returns_an_actionable_error_and_can_be_retried(self):
        from batch.localization.auth import connect
        self.oauth_client()
        threads = []
        def browser(url):
            query = parse_qs(urlparse(url).query)
            def denied():
                requests.get(query["redirect_uri"][0], params={"state": query["state"][0], "error": "access_denied"}, timeout=3)
            thread = threading.Thread(target=denied)
            threads.append(thread)
            thread.start()
        vault = Mock()
        for _ in range(2):
            with self.assertRaisesRegex(AuthError, "użytkowników testowych"):
                connect(self.root, vault=vault, browser=browser, timeout=5)
        for thread in threads:
            thread.join(3)
        vault.write.assert_not_called()

    def test_cancelled_exchange_cannot_persist_credentials_from_an_old_attempt(self):
        from batch.localization.auth import LoginCancelled, connect
        self.oauth_client()
        cancelled, vault = threading.Event(), Mock()
        threads = []
        def browser(url):
            query = parse_qs(urlparse(url).query)
            def granted():
                requests.get(query["redirect_uri"][0], params={"state": query["state"][0], "code": "test-code"}, timeout=3)
            thread = threading.Thread(target=granted)
            threads.append(thread)
            thread.start()
        def exchange(_payload):
            cancelled.set()
            return {"access_token": "synthetic", "refresh_token": "must-not-be-persisted"}
        with patch("batch.localization.auth.token_request", side_effect=exchange):
            with self.assertRaises(LoginCancelled):
                connect(self.root, vault=vault, browser=browser, cancel=cancelled, timeout=5)
        for thread in threads:
            thread.join(3)
        vault.write.assert_not_called()

    def test_browser_failure_returns_without_waiting_for_the_login_timeout(self):
        from batch.localization.auth import connect
        self.oauth_client()
        vault = Mock()
        with self.assertRaisesRegex(AuthError, "przeglądarki"):
            connect(self.root, vault=vault, browser=lambda _url: False, timeout=60)
        vault.write.assert_not_called()

    def test_public_metadata_reads_exact_link_without_downloading_or_using_credentials(self):
        from batch.localization.metadata import extract_public
        from unittest.mock import MagicMock
        context = MagicMock()
        context.__enter__.return_value.extract_info.return_value = {"id": IDENT, "title": "English title", "description": "Polski opis", "channel_id": "channel"}
        with patch("batch.localization.metadata.wait_before_youtube"), patch("batch.localization.metadata.youtube_client", return_value=context) as client:
            data = extract_public(self.root, IDENT, lambda _: None)
        context.__enter__.return_value.extract_info.assert_called_once_with("https://www.youtube.com/watch?v=" + IDENT, download=False)
        self.assertTrue(client.call_args.args[0]["skip_download"])
        self.assertEqual(client.call_args.args[1]["mode"], "anonymous")
        self.assertNotIn("localizations", data)
        self.assertNotIn("defaultAudioLanguage", data["snippet"])
        self.assertEqual(data["snippet"]["description"], "Polski opis")

    def test_local_model_trial_does_not_imply_a_google_connection(self):
        from batch.localization import configuration_summary
        from batch.localization.auth import connection_status
        atomic_json(self.root / "modele" / "madlad400-3b-ct2" / "ready.json", {"revision": "test"})
        atomic_json(self.root / "lokalizacje" / "proba" / "passed.json", {"passed": True, "model": {"revision": "test"}})
        status = connection_status(self.root)
        self.assertFalse(status.ready)
        self.assertEqual(status.step, 0)
        self.assertIn("próba zaliczona", configuration_summary(self.root))
        self.assertIn("brak klienta Google OAuth", configuration_summary(self.root))

    def test_connection_status_checks_credentials_before_accepting_a_channel_id(self):
        from batch.localization.auth import connection_status
        atomic_json(self.root / "konfiguracja" / "yt-oauth-client.json", {"installed": {"client_id": "id.apps.googleusercontent.com", "client_secret": "test-only"}})
        with patch("batch.localization.auth.WindowsVault") as vault:
            vault.return_value.read.side_effect = AuthError("Missing")
            self.assertEqual(connection_status(self.root).step, 1)
            vault.return_value.read.side_effect = None
            self.assertEqual(connection_status(self.root).step, 2)
            atomic_json(self.root / "lokalizacje-ustawienia.json", {"channel_id": "channel"})
            self.assertTrue(connection_status(self.root).ready)

    def test_pkce_callback_state_and_secret_free_browser_url(self):
        from batch.localization.auth import connect
        atomic_json(self.root / "konfiguracja" / "yt-oauth-client.json", {"installed": {"client_id": "id.apps.googleusercontent.com", "client_secret": "test-only-secret"}})
        captured = {}
        threads = []
        def browser(url):
            query = parse_qs(urlparse(url).query)
            captured.update(query)
            self.assertNotIn("client_secret", query)
            callback = query["redirect_uri"][0]
            def respond():
                requests.get(callback, params={"state": "wrong-state", "code": "bad"}, timeout=3)
                requests.get(callback, params={"state": query["state"][0], "code": "good"}, timeout=3)
            thread = threading.Thread(target=respond)
            threads.append(thread)
            thread.start()
        vault = Mock()
        with patch("batch.localization.auth.token_request", return_value={"access_token": "not-persisted", "refresh_token": "stored-in-vault"}) as exchange:
            connect(self.root, vault=vault, browser=browser, timeout=5)
        for thread in threads:
            thread.join(3)
        self.assertEqual(captured["code_challenge_method"], ["S256"])
        self.assertEqual(exchange.call_args.args[0]["code"], "good")
        self.assertTrue(exchange.call_args.args[0]["code_verifier"])
        vault.write.assert_called_once_with("stored-in-vault", "id.apps.googleusercontent.com")

    def test_windows_vault_roundtrip_is_scoped_to_its_own_test_target(self):
        if os.name != "nt":
            self.skipTest("Windows Credential Manager")
        from batch.localization.auth import WindowsVault
        vault = WindowsVault(self.root)
        try:
            vault.write("synthetic-test-credential", "test-client")
            self.assertEqual(vault.read("test-client"), "synthetic-test-credential")
            with self.assertRaises(AuthError):
                vault.read("another-client")
        finally:
            vault.delete()

    def test_oauth_client_requires_desktop_type(self):
        path = self.root / "web-client.json"
        atomic_json(path, {"web": {"client_id": "id"}})
        with self.assertRaises(AuthError):
            import_client(self.root, path)

    def test_tokens_are_not_written_to_configuration(self):
        path = self.root / "client.json"
        atomic_json(path, {"installed": {"client_id": "id.apps.googleusercontent.com", "client_secret": "test-only", "refresh_token": "do-not-copy"}})
        import_client(self.root, path)
        copied = (self.root / "konfiguracja" / "yt-oauth-client.json").read_text(encoding="utf-8")
        self.assertNotIn("do-not-copy", copied)
        self.assertNotIn("refresh_token", copied)

    def test_request_accounts_quota_and_sends_timeout_and_conditional_header(self):
        transport = Mock()
        transport.request.return_value.status_code = 200
        transport.request.return_value.json.return_value = {"items": []}
        auth = Mock()
        auth.token.return_value = "test-token"
        api = YouTube(self.store, auth=auth, transport=transport)
        api.update_video({"id": IDENT}, "etag")
        kwargs = transport.request.call_args.kwargs
        self.assertEqual(kwargs["headers"]["If-Match"], "etag")
        self.assertEqual(kwargs["timeout"], (10, 90))
        with self.store.base.connect() as db:
            self.assertEqual(db.execute("SELECT SUM(used) FROM ytl_quota").fetchone()[0], 50)

    def test_http_error_never_exposes_response_body_or_token(self):
        response = Mock(status_code=403)
        response.json.return_value = {"error": {"errors": [{"reason": "quotaExceeded"}], "message": "secret"}}
        with self.assertRaises(QuotaError) as caught:
            checked_response(response)
        self.assertNotIn("secret", str(caught.exception))
        with self.assertRaises(ConflictError):
            checked_response(Mock(status_code=412))

    def test_upload_refuses_an_external_upload_location(self):
        transport = Mock()
        transport.request.return_value.status_code = 200
        transport.request.return_value.headers = {"Location": "https://evil.example/upload/youtube/captions"}
        auth = Mock()
        auth.token.return_value = "test-token"
        from batch.localization.api import ApiError
        api = YouTube(self.store, auth=auth, transport=transport)
        with self.assertRaises(ApiError):
            api.upload_caption({}, "captions")
        self.assertEqual(transport.request.call_count, 1)


class TranslationTests(LocalizationCase):
    def test_mixed_public_title_and_description_can_be_prepared_without_oauth(self):
        with self.store.base.connect() as db:
            db.execute("UPDATE ytl_videos SET snapshot='{}' WHERE video_id=?", (IDENT,))
        self.store.public_metadata({"id": IDENT, "source": "public", "snippet": {"title": "English title", "description": "Polski opis"}})
        units = metadata_units(self.store, IDENT)
        self.assertEqual({u["text"] for u in units}, {"English title", "Polski opis"})
        self.assertEqual({u["source_language"] for u in units}, {"auto"})

    def fake_translation(self, _python, _module, request, _work, _progress):
        atomic_json(request["result"], {"units": [{"key": u["key"], "text": u["text"]} for u in request["units"]], "model": {"revision": "test"}})

    def test_missing_transcription_does_not_mark_prepared_metadata_failed(self):
        from batch.localization.prepare import prepare_text
        with patch("batch.localization.prepare.source_cues", side_effect=ValueError("Brak transkrypcji")), patch("batch.localization.prepare.child", side_effect=self.fake_translation):
            errors = prepare_text(self.store, IDENT, ["metadata", "captions"], lambda _: None)
        self.assertTrue(errors)
        self.assertEqual(self.store.item(IDENT, "title", "pl")["error"], "")
        self.assertEqual(self.store.item(IDENT, "title", "pl")["review"], "review")
        self.assertIn("Brak transkrypcji", self.store.item(IDENT, "captions", "pl")["error"])

    def test_missing_youtube_metadata_does_not_mark_prepared_captions_failed(self):
        from batch.localization.prepare import prepare_text
        from batch.localization.captions import parse_srt
        from test_localization import SRT
        with self.store.base.connect() as db:
            db.execute("UPDATE ytl_videos SET snapshot='{}' WHERE video_id=?", (IDENT,))
        with patch("batch.localization.prepare.source_cues", return_value=parse_srt(SRT)), patch("batch.localization.prepare.child", side_effect=self.fake_translation):
            errors = prepare_text(self.store, IDENT, ["metadata", "captions"], lambda _: None)
        self.assertTrue(errors)
        self.assertEqual(self.store.item(IDENT, "captions", "pl")["error"], "")
        self.assertEqual(self.store.item(IDENT, "captions", "pl")["review"], "review")
        self.assertIn("Odśwież", self.store.item(IDENT, "title", "pl")["error"])

    def test_native_instrumental_cache_depends_on_actual_model_weights(self):
        from batch.localization.audio import background_key
        source, model = self.root / "original.wav", self.root / "model.onnx"
        source.write_bytes(b"original-mix")
        model.write_bytes(b"model-version-one")
        config = {"uvr_model_dir": str(self.root), "uvr_model": model.name, "uvr_seconds": 300, "uvr_context": 3}
        before = background_key(source, config)
        model.write_bytes(b"model-version-two")
        self.assertNotEqual(background_key(source, config), before)
        self.assertEqual(source.read_bytes(), b"original-mix")

    def test_disk_exhaustion_pauses_preparation_instead_of_marking_every_file_failed(self):
        import errno
        from batch.localization.prepare import prepare_one
        with patch("batch.localization.prepare.prepare_text", side_effect=OSError(errno.ENOSPC, "No space left on device")):
            with self.assertRaises(ResourceError):
                prepare_one(self.store, IDENT, ["metadata"], lambda _: None)

    def test_source_language_is_independent_for_title_and_description(self):
        self.store.language_overrides(IDENT, "pl", "en")
        units = metadata_units(self.store, IDENT)
        self.assertEqual({u["source_language"] for u in units if u["key"].startswith("title:")}, {"pl"})
        self.assertEqual({u["source_language"] for u in units if u["key"].startswith("description:")}, {"en"})

    def test_protected_urls_timestamps_names_and_numbers_are_unchanged(self):
        translator = Translator.__new__(Translator)
        translator.glossary = ["Dark Souls"]
        translator.piece = lambda value, _lang: value.replace("Zobacz", "See")
        text = "01:23 Zobacz Dark Souls https://example.com/a?b=1 #gra 42"
        self.assertEqual(translator.protected(text, "en"), text.replace("Zobacz", "See"))

    def test_long_input_is_split_at_a_word_boundary_without_silent_truncation(self):
        text = "word " * 1000
        left, right = split_text(text)
        self.assertEqual((left + " " + right).strip(), text.strip())
        with self.assertRaises(ValueError):
            split_text("x" * 2000)
