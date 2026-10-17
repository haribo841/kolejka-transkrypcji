"""Setup permission boundaries and recovery, without tokens or live downloads."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import tkinter as tk
import unittest
from unittest.mock import patch

import httpx
from huggingface_hub.errors import GatedRepoError, HfHubHTTPError

from batch.common import atomic_json
from batch.hf_access import MODEL_ID, access_error, check_access, redact
from batch.setup_model import AccessProblem, configure

TEST_TOKEN = "hf_OnlyAnOfflineTestValue"


class AccessTests(unittest.TestCase):
    def test_smoke_test_requires_explicit_local_sample(self):
        from batch.setup_model import smoke_test
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "Sprawdź na fragmencie"):
                smoke_test(directory)

    def test_invalid_input_never_contacts_hugging_face(self):
        with patch("huggingface_hub.HfApi") as api, patch("huggingface_hub.get_hf_file_metadata") as metadata:
            for text in ("", "Read", "https://huggingface.co/settings/tokens", "my password"):
                self.assertEqual(check_access(text, "revision")["code"], "invalid_token")
            api.assert_not_called()
            metadata.assert_not_called()

    def test_valid_identity_still_requires_access_to_the_actual_model(self):
        with patch("huggingface_hub.HfApi") as api, patch("huggingface_hub.get_hf_file_metadata") as metadata:
            api.return_value.whoami.return_value = {"name": "example"}
            result = check_access(TEST_TOKEN, "pinned-revision")
            self.assertTrue(result["ok"])
            self.assertEqual(result["account"], "example")
            api.return_value.whoami.assert_called_once_with(token=TEST_TOKEN)
            url = metadata.call_args.args[0]
            self.assertIn(MODEL_ID + "/resolve/pinned-revision/config.yaml", url)
            self.assertEqual(metadata.call_args.kwargs["token"], TEST_TOKEN)
            self.assertEqual(metadata.call_args.kwargs["timeout"], 15)
            denied = httpx.Response(403, request=httpx.Request("HEAD", url))
            metadata.side_effect = GatedRepoError("forbidden", response=denied)
            self.assertEqual(check_access(TEST_TOKEN, "pinned-revision")["code"], "access_denied")

    def test_errors_distinguish_token_permission_network_and_redact_secrets(self):
        for status, expected in ((401, "invalid_token"), (403, "access_denied"),
                                 (404, "access_denied"), (429, "rate_limit"), (503, "network")):
            response = httpx.Response(status, request=httpx.Request("GET", "https://huggingface.co"))
            error = HfHubHTTPError("secret " + TEST_TOKEN, response=response)
            result = access_error(error, TEST_TOKEN)
            self.assertEqual(result["code"], expected)
            self.assertNotIn(TEST_TOKEN, json.dumps(result))
        self.assertEqual(access_error(httpx.ConnectError("offline"))["code"], "network")
        self.assertNotIn(TEST_TOKEN, access_error(ValueError(TEST_TOKEN), TEST_TOKEN)["message"])
        self.assertNotIn(TEST_TOKEN, redact("value=" + TEST_TOKEN))

    def test_download_cannot_bypass_the_access_check(self):
        with tempfile.TemporaryDirectory() as tmp, patch("batch.setup_model.check_access", return_value={
                "ok": False, "code": "access_denied", "message": "Brak zgody"}), \
                patch("huggingface_hub.snapshot_download") as download:
            with self.assertRaises(AccessProblem):
                configure(Path(tmp), TEST_TOKEN)
            download.assert_not_called()
            for file in Path(tmp).glob("*.json"):
                self.assertNotIn(TEST_TOKEN, file.read_text(encoding="utf-8"))

    def test_model_download_waits_for_an_explicit_sample_without_running_inference(self):
        def download_fixture(**kwargs):
            destination = Path(kwargs['local_dir'])
            destination.mkdir(parents=True, exist_ok=True)
            (destination / 'config.yaml').write_text('offline fixture', encoding='utf-8')
        with tempfile.TemporaryDirectory() as folder, patch('batch.setup_model.check_access', return_value={'ok': True}), \
                patch('huggingface_hub.snapshot_download', side_effect=download_fixture) as download, \
                patch('batch.setup_model.validate_diarization'), patch('batch.setup_model.smoke_test') as inference:
            root = Path(folder)
            configure(root, TEST_TOKEN)
            inference.assert_not_called()
            self.assertEqual(download.call_args.kwargs['token'], TEST_TOKEN)
            state = json.loads((root / 'konfiguracja-modelu.json').read_text(encoding='utf-8'))
            self.assertEqual((state['state'], state['phase']), ('ready', 'download_complete'))
            self.assertNotIn('test_results', state)
            self.assertFalse((root / 'pierwsza-proba.json').exists())
            for path in root.rglob('*.json'):
                self.assertNotIn(TEST_TOKEN, path.read_text(encoding='utf-8'))


class WizardTests(unittest.TestCase):
    def setUp(self):
        from batch.gui import Window
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.tk = tk.Tk()
        self.tk.withdraw()
        self.app = Window(self.root, tk_root=self.tk)
        self.missing_model = patch("batch.setup_wizard.validate_diarization", side_effect=ValueError("missing"))
        self.missing_model.start()
        self.app.configure_model()
        self.wizard = self.app.setup_dialog
        self.wizard.dialog.withdraw()

    def tearDown(self):
        self.missing_model.stop()
        self.app.close()
        self.temp.cleanup()

    def poll_once(self):
        if self.wizard.after_id:
            self.wizard.dialog.after_cancel(self.wizard.after_id)
            self.wizard.after_id = None
        self.wizard.poll()

    def test_changed_token_cannot_reuse_an_earlier_permission_check(self):
        wizard = self.wizard
        wizard.token_var.set(TEST_TOKEN)
        original = wizard.fingerprint()
        wizard.token_var.set(TEST_TOKEN + "Changed")
        wizard.events.put((original, {"ok": True, "message": "ready"}))
        self.poll_once()
        self.assertIsNone(wizard.verified)
        self.assertIn("disabled", wizard.download_button.state())
        with patch.object(self.app, "setup_process") as launch:
            wizard.download()
            launch.assert_not_called()

    def test_verified_download_clears_token_and_does_not_start_the_queue(self):
        wizard = self.wizard
        wizard.token_var.set(TEST_TOKEN)
        wizard.events.put((wizard.fingerprint(), {"ok": True, "message": "ready", "account": "example"}))
        self.poll_once()
        before = self.app.store.control()
        with patch.object(self.app, "setup_process", return_value=True) as launch:
            wizard.download()
            launch.assert_called_once_with({"token": TEST_TOKEN})
        self.assertEqual(wizard.token_var.get(), "")
        self.assertIsNone(wizard.verified)
        self.assertEqual(before, self.app.store.control())
        self.assertIn("disabled", wizard.download_button.state())

    def test_local_model_trial_prompts_for_a_sample(self):
        self.wizard.local_ready = True
        with patch.object(self.app, "test_local") as choose, patch.object(self.app, "setup_process") as launch:
            self.wizard.download()
        choose.assert_called_once_with()
        launch.assert_not_called()

    def test_download_complete_offers_sample_selection_without_enabling_missing_results(self):
        before = self.app.store.control()
        state = {'state': 'ready', 'phase': 'download_complete', 'message': 'Wybierz próbkę'}
        atomic_json(self.root / 'konfiguracja-modelu.json', state)
        self.poll_once()
        self.assertTrue(self.wizard.local_ready)
        self.assertIn('disabled', self.wizard.results_button.state())
        with patch.object(self.app, 'test_local') as choose:
            self.wizard.download()
        choose.assert_called_once_with()
        self.assertEqual(before, self.app.store.control())
        state['test_results'] = str(self.root / 'nonexistent-results')
        atomic_json(self.root / 'konfiguracja-modelu.json', state)
        self.poll_once()
        self.assertIn('disabled', self.wizard.results_button.state())

    def test_process_exit_is_detected_even_when_the_state_file_did_not_change(self):
        atomic_json(self.root / "konfiguracja-modelu.json", {"state": "running", "message": "Pobieranie"})
        with patch("batch.setup_wizard.WorkerLock.busy", return_value=True):
            self.poll_once()
        with patch("batch.setup_wizard.WorkerLock.busy", return_value=False):
            self.poll_once()
        self.assertIn("przerwane", self.wizard.progress_message.get())

    def test_reopening_configuration_reuses_the_dialog_and_close_clears_token(self):
        self.wizard.token_var.set(TEST_TOKEN)
        self.app.configure_model()
        self.assertIs(self.app.setup_dialog, self.wizard)
        self.wizard.close()
        self.assertEqual(self.wizard.token_var.get(), "")
        self.app.configure_model()
        self.assertIsNot(self.app.setup_dialog, self.wizard)
        self.assertEqual(self.app.setup_dialog.token_var.get(), "")

    def test_start_with_missing_model_opens_configuration_without_mutating_queue(self):
        from test_support import wait_for_start
        self.app.store.add_many([{"identity": "yt:abcdefghij0", "kind": "youtube", "title": "Film",
                                 "source": "https://youtube.com/watch?v=abcdefghij0"}])
        before = self.app.store.control()
        with patch("batch.gui.messagebox.showerror") as popup, patch("batch.gui.launch") as launch:
            self.app.start()
            wait_for_start(self.app)
            launch.assert_not_called()
            popup.assert_not_called()
        self.assertEqual(self.app.store.control(), before)
        self.assertEqual(self.app.notebook.select(), str(self.app.config_page))
        self.assertTrue((self.root / "ostatni-blad-start.json").is_file())


if __name__ == "__main__":
    unittest.main()
