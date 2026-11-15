from __future__ import annotations

from pathlib import Path
from contextlib import nullcontext
import json
import os
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import numpy as np
import soundfile as sf

from batch.common import WorkerLock, atomic_bytes, atomic_json, digest, job_folder, remove_work_file
from batch.media import cached_download, probe
from batch.paths import child_path, input_file, local_path, media_tool
from batch.transcript_edit import remove_speakers
from batch.uvr import archive_source, cached_archive
from batch.uvr_child import UVR_RATE, generated_vocal, request_paths, separator, write_vocal_block
from batch.worker import Worker
from batch.store import Store
from batch.localization.translation import setup as setup_translation, setup_paths


class LocalRegistrySecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def test_uri_devices_and_traversal_are_rejected_before_creating_a_registry(self):
        invalid = ("file:outside?mode=rwc", "sqlite:///outside", "https://host/db", "a\x00b", "\\\\.\\NUL",
                   str(self.root / "queue" / ".." / "outside"))
        with patch("batch.store.sqlite3.connect") as connect:
            for value in invalid:
                with self.subTest(value=value), self.assertRaises(ValueError):
                    Store(value)
        connect.assert_not_called()
        self.assertFalse((self.root / "outside").exists())

    def test_database_connections_are_literal_filenames_with_uri_mode_disabled(self):
        store = Store(self.root / "kolejka z polskimi znakami ąę")
        actual = sqlite3.connect
        with patch("batch.store.sqlite3.connect", wraps=actual) as connect:
            with store.connect() as db:
                self.assertEqual(db.execute("SELECT count(*) FROM jobs").fetchone()[0], 0)
            store.snapshot()
        self.assertEqual(connect.call_count, 2)
        for call in connect.call_args_list:
            self.assertEqual(call.args[0].name, "kolejka.sqlite3")
            self.assertFalse(call.kwargs["uri"])

    def test_replacing_database_path_cannot_redirect_writes(self):
        store = Store(self.root)
        store.path = "file:outside.sqlite3?mode=rwc"
        with patch("batch.store.sqlite3.connect") as connect:
            with self.assertRaises(ValueError):
                with store.connect():
                    pass
            with self.assertRaises(ValueError):
                store.snapshot()
        connect.assert_not_called()

    def test_hardlinked_database_is_rejected_before_sqlite_opens_it(self):
        outside = self.root / "outside.sqlite3"
        outside.write_bytes(b"preserve unrelated database")
        queue = self.root / "queue"
        queue.mkdir()
        os.link(outside, queue / "kolejka.sqlite3")
        with patch("batch.store.sqlite3.connect") as connect, self.assertRaises(ValueError):
            Store(queue)
        connect.assert_not_called()
        self.assertEqual(outside.read_bytes(), b"preserve unrelated database")


class TranslationSetupSecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.queue = self.root / "queue"
        self.queue.mkdir()
        self.temp_root = self.root / "os-temp"
        self.temp_root.mkdir()
        self.patch = patch("batch.localization.translation.tempfile.gettempdir", return_value=str(self.temp_root))
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_output_is_fixed_and_staging_is_derived_inside_os_temporary_directory(self):
        root, output, staging = setup_paths(self.queue)
        self.assertEqual(root, self.queue)
        self.assertEqual(output, self.queue / "modele" / "madlad400-3b-ct2")
        self.assertTrue(staging.is_relative_to(self.temp_root))
        self.assertEqual(staging, setup_paths(self.queue)[2])
        self.assertFalse(output.exists() or staging.exists())

    def test_configured_outside_destination_is_rejected_before_import_or_download(self):
        outside = self.root / "unrelated"
        atomic_json(self.queue / "lokalizacje-ustawienia.json", {"translation_model": str(outside)})
        with patch("batch.localization.translation.check_disk") as disk, self.assertRaises(ValueError):
            setup_translation(self.queue)
        disk.assert_not_called()
        self.assertFalse(outside.exists())
        self.assertEqual(list(self.temp_root.iterdir()), [])

    def test_model_parent_directory_link_cannot_escape_the_selected_registry(self):
        outside = self.root / "unrelated"
        outside.mkdir()
        if os.name == "nt":
            import _winapi
            _winapi.CreateJunction(str(outside), str(self.queue / "modele"))
        else:
            (self.queue / "modele").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            setup_paths(self.queue)
        self.assertEqual(list(outside.iterdir()), [])

    def test_arbitrary_staging_cli_argument_is_not_accepted(self):
        from batch.localization.translation import main
        with patch.object(sys, "argv", ["translation", "--setup", str(self.queue), "--staging", str(self.root / "outside")]), \
             patch("batch.localization.translation.setup") as setup, patch("sys.stderr"), self.assertRaises(SystemExit) as error:
            main()
        self.assertEqual(error.exception.code, 2)
        setup.assert_not_called()
        self.assertFalse((self.root / "outside").exists())

    def test_existing_model_is_not_overwritten_or_reconverted(self):
        _, output, _ = setup_paths(self.queue)
        output.mkdir(parents=True)
        model = output / "model.bin"
        model.write_bytes(b"keep working model")
        with self.assertRaisesRegex(RuntimeError, "już istnieje"):
            setup_translation(self.queue)
        self.assertEqual(model.read_bytes(), b"keep working model")
        self.assertEqual(list(self.temp_root.iterdir()), [])

    def test_conversion_uses_a_private_directory_and_never_forces_overwrite(self):
        module = "batch.localization.translation"
        def snapshot(_model, **kwargs):
            source = kwargs["local_dir"]
            source.mkdir(parents=True)
            (source / "tokenizer.json").write_text("{}", encoding="utf-8")
            return str(source)
        def convert(destination, **kwargs):
            self.assertFalse(kwargs["force"])
            target = Path(destination)
            self.assertTrue(target.parent.is_relative_to(self.queue / "modele"))
            self.assertEqual(target.name, "converted")
            self.assertFalse(target.exists())
            target.mkdir()
            (target / "model.bin").write_bytes(b"converted model")
        converter = MagicMock()
        converter.convert.side_effect = convert
        dependencies = {"huggingface_hub": SimpleNamespace(snapshot_download=snapshot),
                        "ctranslate2.converters": SimpleNamespace(TransformersConverter=lambda *_a, **_kw: converter)}
        with patch.dict(sys.modules, dependencies), patch(module + ".check_disk"), patch(module + ".importlib.metadata.version", return_value="pinned"):
            setup_translation(self.queue)
        output = self.queue / "modele" / "madlad400-3b-ct2"
        self.assertEqual((output / "model.bin").read_bytes(), b"converted model")
        self.assertTrue((output / "ready.json").is_file())
        self.assertTrue((output / "tokenizer.json").is_file())
        self.assertFalse(list(output.parent.glob(".madlad-*")))


class SonarConfigurationTests(unittest.TestCase):
    def test_all_test_modules_are_classified_as_tests_and_excluded_from_production(self):
        root = Path(__file__).resolve().parent
        properties = dict(line.split("=", 1) for line in
                          (root / ".sonarcloud.properties").read_text(encoding="utf-8").splitlines()
                          if line.strip() and not line.lstrip().startswith("#"))
        expected = {path.name for path in root.glob("test_*.py")}
        for key in ("sonar.tests", "sonar.exclusions"):
            with self.subTest(property=key):
                self.assertEqual(set(properties[key].split(",")), expected)


class WorkerLockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.worker = Worker(self.root)

    def test_missing_lock_acquires_exclusive_ownership_and_releases_it(self):
        lock = self.worker.execution_lock(None)
        self.assertIsInstance(lock, WorkerLock)
        self.addCleanup(lock.close)
        self.assertTrue(WorkerLock.busy(self.root))
        self.assertIsNone(self.worker.execution_lock(None))
        lock.close()
        self.assertFalse(WorkerLock.busy(self.root))

    def test_already_acquired_lock_is_reused_without_reacquiring(self):
        lock = WorkerLock(self.root)
        self.addCleanup(lock.close)
        self.assertTrue(lock.acquire())
        with patch.object(lock, "acquire") as acquire:
            self.assertIs(self.worker.execution_lock(lock), lock)
            acquire.assert_not_called()
        self.assertTrue(WorkerLock.busy(self.root))

    def test_unacquired_or_released_lock_is_rejected(self):
        lock = WorkerLock(self.root)
        self.addCleanup(lock.close)
        with self.assertRaisesRegex(ValueError, "blokada wykonawcy"):
            self.worker.execution_lock(lock)
        self.assertTrue(lock.acquire())
        lock.close()
        with self.assertRaisesRegex(ValueError, "blokada wykonawcy"):
            self.worker.execution_lock(lock)
        self.assertFalse(WorkerLock.busy(self.root))

    def test_lock_for_another_queue_is_rejected_without_releasing_it(self):
        foreign = self.root / "inna-kolejka"
        lock = WorkerLock(foreign)
        self.addCleanup(lock.close)
        self.assertTrue(lock.acquire())
        with self.assertRaisesRegex(ValueError, "blokada wykonawcy"):
            self.worker.execution_lock(lock)
        self.assertTrue(WorkerLock.busy(foreign))
        self.assertFalse(WorkerLock.busy(self.root))


class PathSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.work = self.root / "nagranie" / "robocze"
        self.work.mkdir(parents=True)
        self.source = self.root / "oryginal.wav"
        self.source.write_bytes(b"preserve original")

    def tearDown(self):
        self.temp.cleanup()

    def test_generated_paths_cannot_escape_owner_folder(self):
        for name in ("../oryginal.wav", "..\\oryginal.wav", str(self.source)):
            with self.subTest(name=name), self.assertRaises(ValueError):
                child_path(self.work, name)
        job = {"folder": "../../oryginal.wav"}
        with self.assertRaises(ValueError):
            job_folder(self.root, job)
        self.assertEqual(self.source.read_bytes(), b"preserve original")

    def test_cleanup_rejects_owner_directory_and_original(self):
        for target in (self.work, self.source):
            with self.subTest(target=target), self.assertRaises(ValueError):
                remove_work_file(target, self.work)
        self.assertTrue(self.work.is_dir())
        self.assertTrue(self.source.is_file())

    def test_corrupt_download_manifest_cannot_reuse_an_outside_file(self):
        atomic_json(self.work / "download.json", {"identity": "yt:abcdefghijk",
                    "filename": "../../oryginal.wav", "sha256": digest(self.source)})
        self.assertIsNone(cached_download({"identity": "yt:abcdefghijk"}, self.work))

    def test_atomic_overwrite_uses_unique_temporary_without_touching_a_stale_file(self):
        target = self.work / "state.json"
        stale = self.work / "state.json.tmp"
        stale.write_bytes(b"unrelated file")
        atomic_bytes(target, b"first")
        atomic_bytes(target, b"second")
        self.assertEqual(target.read_bytes(), b"second")
        self.assertEqual(stale.read_bytes(), b"unrelated file")
        self.assertFalse(list(self.work.glob(".zapis-*")))

    def test_invalid_paths_and_nonfiles_are_rejected(self):
        for value in ("", "a\x00b", "a\nb", "\\\\.\\NUL", "\\\\?\\C:\\file"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                local_path(value)
        with self.assertRaises(ValueError):
            input_file(self.work)

    @unittest.skipUnless(os.name == "nt", "Windows device names")
    def test_windows_devices_and_alternate_streams_are_rejected(self):
        for name in ("NUL.json", "CON.txt", "LPT1.flac", "state.json:secret"):
            path = self.root / name
            with self.subTest(name=name), self.assertRaises(ValueError):
                local_path(path)

    def test_probe_accepts_an_option_like_filename_as_a_local_argument(self):
        source = self.root / "-show_format.wav"
        source.write_bytes(b"synthetic")
        tool = self.root / "ffprobe.exe"
        tool.write_bytes(b"mock executable")
        with patch("batch.media.subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = b'{"streams":[]}'
            self.assertEqual(probe(source, {"ffprobe": str(tool)}), {"streams": []})
        command = run.call_args.args[0]
        self.assertEqual(command[-1], str(source.resolve()))
        self.assertIn("file,pipe", command)
        self.assertFalse(run.call_args.kwargs["shell"])
        self.assertEqual(command[0], str(tool.resolve()))

    def test_media_tool_rejects_shell_scripts_and_other_programs(self):
        tool = self.root / "powershell.exe"
        tool.write_bytes(b"never executed")
        with self.assertRaises(ValueError):
            media_tool(tool, "ffprobe")

    def test_uvr_accepts_downloaded_audio_and_rejects_arbitrary_outputs(self):
        source = self.work / "download.m4a"
        source.write_bytes(b"downloaded audio")
        output = self.work.parent / "audio" / "wokal.flac"
        request = {"source": str(source), "work": str(self.work), "output": str(output)}
        self.assertEqual(request_paths(request), (source, self.work, output))
        request["output"] = str(self.root / "other.flac")
        with self.assertRaises(ValueError):
            request_paths(request)
        self.assertFalse((self.root / "other.flac").exists())

    def test_uvr_cannot_read_a_generated_stem_outside_its_stage(self):
        stage = self.work / "uvr" / "biezacy"
        stage.mkdir(parents=True)
        for name in ("../../../oryginal.wav", str(self.source)):
            with self.subTest(name=name), self.assertRaises(ValueError):
                generated_vocal(stage, name)
        self.assertEqual(self.source.read_bytes(), b"preserve original")

    def test_archive_manifest_cannot_reuse_a_file_outside_the_audio_folder(self):
        audio = self.work.parent / "audio"
        audio.mkdir()
        atomic_json(audio / "zrodlo.json", {"source_sha256": "input", "track": 0,
                    "file": "../../oryginal.wav", "sha256": digest(self.source)})
        self.assertIsNone(cached_archive(audio, "input", 0))

    def test_uvr_model_path_cannot_escape_before_loading_dependencies(self):
        config = {"uvr_model_dir": str(self.work), "uvr_model": "../../oryginal.wav"}
        with self.assertRaises(ValueError):
            separator(config, self.work)
        self.assertEqual(self.source.read_bytes(), b"preserve original")

    def test_archiving_cannot_overwrite_its_local_source(self):
        folder = self.work.parent
        source = folder / "audio" / "zrodlo.wav"
        source.parent.mkdir()
        source.write_bytes(b"original")
        config = {"min_free_gb": 0}
        with patch("batch.uvr.probe", return_value={"streams": [{"codec_type": "audio"}]}):
            with self.assertRaisesRegex(RuntimeError, "nadpisać"):
                archive_source(source, folder, 0, config)
        self.assertEqual(source.read_bytes(), b"original")

    def test_atomic_write_refuses_a_symbolic_link_target(self):
        target = self.work / "state.json"
        with patch("batch.paths.Path.is_symlink", return_value=True):
            with self.assertRaisesRegex(ValueError, "dowiązanie"):
                atomic_bytes(target, b"changed")
        self.assertFalse(target.exists())


class SubtitleSafetyTests(unittest.TestCase):
    def test_many_vtt_classes_without_a_voice_name_are_preserved(self):
        tag = "<v" + ".speaker" * 512 + ">"
        text = "WEBVTT\n\n00:00.000 --> 00:01.000\n" + tag + "Tekst\n"
        self.assertEqual(remove_speakers(text, ".vtt").text, text)

    def test_many_vtt_classes_with_a_voice_name_are_removed(self):
        text = "WEBVTT\n\n00:00.000 --> 00:01.000\n<v" + ".speaker" * 512 + " Adam>Tekst</v>\n"
        result = remove_speakers(text, ".vtt")
        self.assertEqual(result.text, "WEBVTT\n\n00:00.000 --> 00:01.000\nTekst\n")
        self.assertEqual(result.removed, 1)


class VocalTimingTests(unittest.TestCase):
    def test_context_trimming_preserves_exact_sample_boundaries(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, stem = root / "vocals.wav", root / "part.flac"
            samples = np.zeros((UVR_RATE, 2), dtype=np.float32)
            samples[UVR_RATE // 4:UVR_RATE // 2] = .25
            sf.write(source, samples, UVR_RATE, subtype="FLOAT")
            block = {"start": 0, "end": 1, "core_start": .25, "core_end": .75}
            frames = write_vocal_block(source, stem, block)
            result, rate = sf.read(stem, dtype="float32", always_2d=True)
            self.assertEqual(frames, round(.75 * UVR_RATE) - round(.25 * UVR_RATE))
            self.assertEqual(rate, UVR_RATE)
            self.assertEqual(result.shape, (frames, 2))
            self.assertAlmostEqual(float(result[0, 0]), .25)
            self.assertEqual(float(result[-1, 0]), 0)

    def test_truncated_vocals_are_rejected_without_writing_a_checkpoint(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, stem = root / "vocals.wav", root / "part.flac"
            sf.write(source, np.zeros((100, 2), dtype=np.float32), UVR_RATE, subtype="FLOAT")
            block = {"start": 0, "end": 1, "core_start": 0, "core_end": 1}
            with self.assertRaisesRegex(RuntimeError, "skrócił"):
                write_vocal_block(source, stem, block)
            self.assertFalse(stem.exists())


class DiarizationRegressionTests(unittest.TestCase):
    def test_delayed_callbacks_keep_their_original_block_and_message(self):
        from batch.diarize_child import progress_hook
        first = progress_hook("blok pierwszy", 0, 4)
        second = progress_hook("blok drugi", 1, 4)
        with patch("batch.diarize_child.emit") as emit:
            second("embedding", None, total=2, completed=1)
            first("segmentation", None, total=2, completed=1)
        self.assertEqual(emit.call_args_list[0].args, ("blok drugi: embedding", 37.5))
        self.assertEqual(emit.call_args_list[1].args, ("blok pierwszy: segmentation", 12.5))

    def test_gpu_memory_failure_retries_the_same_waveform_on_cpu(self):
        from batch.diarize_child import Diarizer
        class GPUOutOfMemory(RuntimeError):
            pass
        torch = SimpleNamespace(inference_mode=nullcontext, device=lambda value: value,
                                cuda=SimpleNamespace(OutOfMemoryError=GPUOutOfMemory, empty_cache=MagicMock()))
        with tempfile.TemporaryDirectory() as folder:
            engine = Diarizer.__new__(Diarizer)
            engine.work = Path(folder)
            engine.pipeline = MagicMock(side_effect=[GPUOutOfMemory("GPU full"), "result"])
            waveform, hook = object(), object()
            with patch.dict("sys.modules", {"torch": torch}), patch("batch.diarize_child.emit"):
                self.assertEqual(engine.inference(waveform, hook), "result")
            self.assertEqual(engine.pipeline.call_count, 2)
            engine.pipeline.to.assert_called_once_with("cpu")
            torch.cuda.empty_cache.assert_called_once_with()
            first, second = engine.pipeline.call_args_list
            self.assertIs(first.args[0]["waveform"], waveform)
            self.assertIs(second.args[0]["waveform"], waveform)
            self.assertEqual(json.loads((engine.work / "diar-device.json").read_text())["device"], "cpu")

    def test_restart_reuses_completed_diarization_blocks(self):
        from batch.diarize_child import run
        with tempfile.TemporaryDirectory() as folder:
            pcm = Path(folder) / "audio.f32le"
            pcm.write_bytes(bytes(25 * 16000 * 4))
            request = {"work": folder, "pcm": str(pcm), "duration": 25, "signature": "recording"}
            calls = []
            class FakeDiarizer:
                def __init__(self, selected):
                    self.work = Path(selected["work"])
                    self.config = {"diar_seconds": 10, "diar_context": 1}
                    self.base = "same-model-and-input"
                    self.legacy_base = "legacy-model-and-input"
                def block_result(self, block, key, count):
                    calls.append(block["index"])
                    if len(calls) == 2:
                        raise RuntimeError("interrupted")
                    return {"signature": key, "block": block, "turns": []}
            with patch("batch.diarize_child.Diarizer", FakeDiarizer), patch("batch.diarize_child.emit"):
                with self.assertRaisesRegex(RuntimeError, "interrupted"):
                    run(request)
                first = (Path(folder) / "diar" / "000000.json").read_bytes()
                run(request)
            self.assertEqual(calls, [0, 1, 1, 2])
            self.assertEqual((Path(folder) / "diar" / "000000.json").read_bytes(), first)
            self.assertEqual(len(json.loads((Path(folder) / "diar-result.json").read_text())["blocks"]), 3)


if __name__ == "__main__":
    unittest.main()
