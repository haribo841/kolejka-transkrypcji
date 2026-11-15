from __future__ import annotations

import codecs
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from batch.common import atomic_json, atomic_new_bytes, digest, read_json
from batch.exporter import export, verify_outputs
from batch.result_paths import EXTENSIONS, LEGACY_OUTPUTS, output_paths, upgrade_output_names
from batch.transcript_edit import load_transcript, remove_speakers, save_without_speakers


def report(identity="yt:abcdefghijk", title="Rozmowa / przykład"):
    return {"identity": identity, "title": title, "duration": 4, "segments": [
        {"start": 1.2, "end": 3.8, "speaker": "Mówca 2", "text": "Mówca 1: to część cytowanej wypowiedzi."}]}


class ResultNamingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_two_recordings_with_the_same_title_have_different_file_names(self):
        first = export(self.root / "one", report("yt:abcdefghijk"), "one")
        before = {ext: path.read_bytes() for ext, path in first.items()}
        second = export(self.root / "two", report("yt:abcdefghijl"), "two")
        self.assertTrue(set(p.name for p in first.values()).isdisjoint(p.name for p in second.values()))
        self.assertIn("abcdefghijk", first["txt"].name)
        self.assertEqual(before, {ext: path.read_bytes() for ext, path in first.items()})
        self.assertTrue(verify_outputs(self.root / "one", "yt:abcdefghijk"))
        self.assertTrue(verify_outputs(self.root / "two", "yt:abcdefghijl"))

    def test_another_recording_cannot_overwrite_an_existing_folder(self):
        first = export(self.root, report(), "one")
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        second_report = report("yt:abcdefghijl")
        with self.assertRaises(FileExistsError):
            export(self.root, second_report, "two")
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir()})
        self.assertTrue(first["txt"].is_file())

    def test_reexport_keeps_the_previous_version(self):
        old = export(self.root, report(), "old")
        old_bytes = {ext: path.read_bytes() for ext, path in old.items()}
        updated = report()
        updated["segments"][0]["text"] = "Nowa wersja"
        new = export(self.root, updated, "new")
        self.assertNotEqual(new, old)
        self.assertEqual(old_bytes, {ext: path.read_bytes() for ext, path in old.items()})
        self.assertTrue(verify_outputs(self.root, updated["identity"], "new"))
        self.assertIn("Nowa wersja", new["txt"].read_text("utf-8"))

    def test_failed_reexport_preserves_the_last_complete_manifest(self):
        export(self.root, report(), "old")
        manifest = (self.root / "gotowe.json").read_bytes()
        updated_report = report()
        with patch("batch.exporter.atomic_new_bytes", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                export(self.root, updated_report, "new")
        self.assertEqual(manifest, (self.root / "gotowe.json").read_bytes())
        self.assertTrue(verify_outputs(self.root, report()["identity"], "old"))

    def test_legacy_upgrade_preserves_bytes_status_signature_and_user_files(self):
        named = export(self.root, report(), "same-signature")
        manifest = read_json(self.root / "gotowe.json")
        legacy = dict(zip(EXTENSIONS, LEGACY_OUTPUTS))
        for ext, path in named.items():
            path.rename(self.root / legacy[ext])
        manifest.pop("outputs")
        manifest.pop("format_version")
        manifest["files"] = {name: digest(self.root / name) for name in legacy.values()}
        atomic_json(self.root / "gotowe.json", manifest)
        (self.root / "moje-notatki.txt").write_bytes(b"user text")
        before = {name: (self.root / name).read_bytes() for name in legacy.values()}
        self.assertTrue(verify_outputs(self.root, report()["identity"], "same-signature"))
        result = upgrade_output_names(self.root, report()["identity"])
        for ext, path in result.items():
            self.assertEqual(path.read_bytes(), before[legacy[ext]])
            self.assertEqual((self.root / legacy[ext]).read_bytes(), before[legacy[ext]])
        self.assertEqual((self.root / "moje-notatki.txt").read_bytes(), b"user text")
        self.assertTrue(verify_outputs(self.root, report()["identity"], "same-signature"))
        self.assertEqual(upgrade_output_names(self.root, report()["identity"]), result)

    def test_atomic_new_file_never_replaces_existing_content(self):
        path = self.root / "wynik.txt"
        atomic_new_bytes(path, b"first")
        with self.assertRaises(FileExistsError):
            atomic_new_bytes(path, b"second")
        self.assertEqual(path.read_bytes(), b"first")
        self.assertEqual(list(self.root.glob(".zapis-*.tmp")), [])

    def test_manifest_cannot_redirect_results_outside_the_recording(self):
        export(self.root, report(), "v1")
        manifest = read_json(self.root / "gotowe.json")
        manifest["outputs"]["txt"] = "../other.txt"
        atomic_json(self.root / "gotowe.json", manifest)
        self.assertFalse(verify_outputs(self.root, report()["identity"]))


class AutomaticSpeakerExportTests(unittest.TestCase):
    def make_report(self, labels):
        segments = [{"start": index, "end": index + 1, "speaker": label, "text": f"Wypowiedź {index}: Żółw & <test>."}
                    for index, label in enumerate(labels)]
        return {"identity": "yt:abcdefghijk", "title": "Próba eksportu", "duration": 10,
                "processed_until": len(segments), "segments": segments,
                "words": [{"speaker": label, "text": "Żółw"} for label in labels]}

    def test_single_speaker_rule_is_saved_in_all_text_outputs_and_json_keeps_speakers(self):
        for labels in (["Mówca 1"], ["Mówca nieustalony"], ["Mówca 1", "Mówca nieustalony"]):
            with self.subTest(labels=labels), tempfile.TemporaryDirectory() as tmp:
                folder = Path(tmp)
                data = self.make_report(labels)
                paths = export(folder, data, "execution")
                self.assertEqual(output_paths(folder), paths)
                for extension in ("txt", "srt", "vtt"):
                    text = paths[extension].read_text(encoding="utf-8")
                    self.assertNotIn("Mówca", text)
                    self.assertIn("Wypowiedź", text)
                saved = read_json(paths["json"])
                self.assertEqual([s["speaker"] for s in saved["segments"]], labels)
                self.assertEqual([w["speaker"] for w in saved["words"]], labels)
                self.assertTrue(saved["text_export"]["speaker_labels_omitted"])
                self.assertTrue(verify_outputs(folder, data["identity"], "execution"))

    def test_other_speakers_preserve_all_labels_in_actual_exports(self):
        for labels in (["Mówca 1", "Mówca 2", "Mówca nieustalony"], ["Mówca 2"], ["Jan"]):
            with self.subTest(labels=labels), tempfile.TemporaryDirectory() as tmp:
                paths = export(Path(tmp), self.make_report(labels), "execution")
                for extension in ("txt", "srt", "vtt"):
                    text = paths[extension].read_text(encoding="utf-8")
                    for label in labels:
                        self.assertIn(label + ": ", text)

    def test_disabled_rule_preserves_a_single_speaker_label(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = self.make_report(["Mówca 1"])
            data["settings"] = {"omit_single_speaker_labels": False}
            paths = export(Path(tmp), data, "execution")
            self.assertIn("Mówca 1:", paths["txt"].read_text(encoding="utf-8"))
            self.assertFalse(read_json(paths["json"])["text_export"]["speaker_labels_omitted"])

    def test_partial_export_preserves_resume_notice_and_timestamps_without_labels(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = self.make_report(["Mówca 1", "Mówca nieustalony"])
            paths = export(Path(tmp), data, "execution", complete=False)
            text = paths["txt"].read_text(encoding="utf-8")
            self.assertIn("WYNIK CZĘŚCIOWY", text)
            self.assertIn("[00:00:00.000 - 00:00:01.000] Wypowiedź", text)
            self.assertNotIn("] Mówca", text)
            self.assertEqual(paths["srt"].read_text(encoding="utf-8").count(" --> "), 2)
            self.assertTrue(verify_outputs(tmp, data["identity"], "execution", partial=True))


class EditingTests(unittest.TestCase):
    def test_conditional_cleanup_reads_all_labels_and_preserves_other_speakers(self):
        first = "[00:00:01.000 - 00:00:02.000] Mówca 1: Tekst: Mówca 2 jest cytatem.\r\n"
        unknown = "[00:00:02.000 - 00:00:03.000] Mówca nieustalony: Dalszy tekst.\r\n"
        result = remove_speakers(first + unknown, ".txt", only_single=True)
        self.assertEqual(result.removed, 2)
        self.assertIn("Tekst: Mówca 2 jest cytatem.", result.text)
        self.assertNotIn("] Mówca", result.text)
        second = "[00:00:03.000 - 00:00:04.000] Mówca 2: Drugi głos.\r\n"
        mixed = first + unknown + second
        self.assertEqual(remove_speakers(mixed, ".txt", only_single=True).text, mixed)
        self.assertEqual(remove_speakers(second, ".txt", only_single=True).removed, 0)

    def test_conditional_subtitles_keep_times_and_real_voice_annotations(self):
        text = "1\n00:00:01,000 --> 00:00:02,000\nMówca 1: Żółw.\n\n2\n00:00:02,000 --> 00:00:03,000\nMówca nieustalony: Dalej.\n\n"
        result = remove_speakers(text, ".srt", only_single=True)
        self.assertEqual(result.removed, 2)
        self.assertEqual(result.text, text.replace("Mówca 1: ", "").replace("Mówca nieustalony: ", ""))
        vtt = "WEBVTT\n\n00:01.000 --> 00:02.000 align:start\n<v Mówca 1>Tekst.</v>\n\n"
        self.assertEqual(remove_speakers(vtt, ".vtt", only_single=True).text, vtt.replace("<v Mówca 1>", "").replace("</v>", ""))
        for other in ("<v Jan>", "<v Mówca 2>"):
            changed = vtt.replace("<v Mówca 1>", other)
            self.assertEqual(remove_speakers(changed, ".vtt", only_single=True).text, changed)

    def test_conditional_save_skips_other_speakers_without_creating_a_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "rozmowa.txt"
            text = "[00:01.000 - 00:02.000] Mówca 2: Drugi głos.\n"
            source.write_text(text, encoding="utf-8")
            target, result = save_without_speakers(source, Path(tmp) / "copies", only_single=True)
            self.assertIsNone(target)
            self.assertEqual(result.removed, 0)
            self.assertEqual(source.read_text(encoding="utf-8"), text)
            self.assertFalse((Path(tmp) / "copies").exists())

    def test_conditional_mode_checks_speakers_beyond_the_preview_limit(self):
        first = "[00:00:01.000 - 00:00:02.000] Mówca 1: Przykładowa wypowiedź.\n"
        text = first * 1200 + "[00:00:03.000 - 00:00:04.000] Mówca 2: Drugi głos.\n"
        self.assertGreater(len(text), 50000)
        result = remove_speakers(text, ".txt", only_single=True)
        self.assertEqual(result.text, text)
        self.assertEqual(result.removed, 0)

    def test_conditional_save_preserves_utf16_and_timestamps_in_an_actual_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "nagranie.txt"
            text = "[00:01.000 - 00:02.000] Mówca 1: Żółw.\r\n"
            raw = codecs.BOM_UTF16_LE + text.encode("utf-16-le")
            source.write_bytes(raw)
            target, result = save_without_speakers(source, only_single=True)
            self.assertEqual(target.parent, source.parent.resolve())
            self.assertEqual(target.read_bytes(), codecs.BOM_UTF16_LE + text.replace("Mówca 1: ", "").encode("utf-16-le"))
            self.assertEqual(source.read_bytes(), raw)
            self.assertEqual(result.removed, 1)

    def test_txt_preserves_times_speech_header_and_line_endings(self):
        text = ("Mówca 9: tytuł nagrania\r\n"
                "[01:12:34.200 - 01:12:39.800] Mówca 2: Cytat: Mówca 1: zostaje.\r\n"
                "[01:12:40.000 - 01:12:41.999] Mówca nieustalony: Tekst!\r\n"
                "[01:12:42.000 - 01:12:43.999] Speaker_00: English.\r\n")
        expected = text.replace("] Mówca 2: ", "] ").replace("] Mówca nieustalony: ", "] ").replace("] Speaker_00: ", "] ")
        result = remove_speakers(text, ".txt")
        self.assertEqual(result.text, expected)
        self.assertEqual((result.removed, result.cues), (3, 3))
        self.assertEqual(remove_speakers(result.text, ".txt").removed, 0)

    def test_srt_keeps_numbering_timing_multiline_speech_and_markup(self):
        text = "7\n00:00:01,200 --> 00:00:03,800\nMówca 2: To &amp; to.\nMówca 1: cytowana druga linia.\n\n"
        result = remove_speakers(text, ".srt")
        self.assertEqual(result.text, text.replace("Mówca 2: ", ""))
        self.assertEqual((result.removed, result.cues), (1, 1))

    def test_vtt_keeps_metadata_cue_settings_and_spoken_names(self):
        text = ("WEBVTT\n\nNOTE Mówca 1: opis pliku\n\n"
                "cue-id\n01:20.200 --> 01:25.800 align:start\n<v Jan>Ktoś mówi o Mówcy 2.\nDalszy tekst.</v>\n\n")
        result = remove_speakers(text, ".vtt")
        self.assertEqual(result.text, text.replace("<v Jan>", "").replace("</v>", ""))
        self.assertEqual((result.removed, result.cues), (1, 1))

    def test_no_annotation_means_no_changes(self):
        text = "Tytuł\n[00:00:01.000 - 00:00:02.000] Powiedział: Mówca 1.\n[Brak rozpoznanych wypowiedzi]\n"
        result = remove_speakers(text, ".txt")
        self.assertEqual(result.text, text)
        self.assertEqual(result.removed, 0)

    def test_save_preserves_original_encoding_and_earlier_copies(self):
        text = "[00:00:01.000 - 00:00:02.000] Mówca 2: Żółw.\r\n"
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            for bom, encoding in ((b"", "utf-8"), (codecs.BOM_UTF8, "utf-8"),
                                  (codecs.BOM_UTF16_LE, "utf-16-le"), (codecs.BOM_UTF16_BE, "utf-16-be")):
                source = folder / (encoding + str(len(bom)) + ".txt")
                raw = bom + text.encode(encoding)
                source.write_bytes(raw)
                first, result = save_without_speakers(source, folder / "copies")
                first.write_bytes(b"reczna korekta kopii")
                second, _ = save_without_speakers(source, folder / "copies")
                self.assertNotEqual(first, second)
                self.assertEqual(first.read_bytes(), b"reczna korekta kopii")
                self.assertEqual(source.read_bytes(), raw)
                self.assertEqual(second.read_bytes(), bom + result.text.encode(encoding))

    def test_concurrent_copy_writes_produce_two_complete_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "tekst.txt"
            source.write_text("[00:01.000 - 00:02.000] Mówca 1: tekst\n", encoding="utf-8")
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: save_without_speakers(source), range(2)))
            self.assertNotEqual(results[0][0], results[1][0])
            self.assertEqual(results[0][0].read_bytes(), results[1][0].read_bytes())

    def test_invalid_encoding_is_reported_instead_of_replacing_characters(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "tekst.txt"
            source.write_bytes(b"\xffbad\xfe")
            with self.assertRaisesRegex(ValueError, "kodowania"):
                load_transcript(source)
            self.assertEqual(source.read_bytes(), b"\xffbad\xfe")

    def test_named_copies_keep_the_recording_identifier(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / ("T" * 45 + "__local-" + "a" * 20 + "_transkrypcja.txt")
            source.write_text("[00:01.000 - 00:02.000] Mówca 1: tekst\n", encoding="utf-8")
            result, _ = save_without_speakers(source)
            self.assertIn("__local-" + "a" * 20, result.name)
            self.assertTrue(result.name.endswith("_bez_mowcow.txt"))


class EditingUITests(unittest.TestCase):
    def test_defaults_follow_sources_and_custom_folder_survives_another_import(self):
        from batch.gui import Window
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = Window(root)
            app.window.withdraw()
            try:
                editor = app.transcript_tab
                paths = []
                for name in ("a", "b", "c"):
                    folder = root / name
                    folder.mkdir()
                    source = folder / (name + ".txt")
                    source.write_text("[00:01.000 - 00:02.000] Mówca 1: Tekst.\n", encoding="utf-8")
                    paths.append(source)
                editor.add_paths(paths[:1])
                self.assertEqual(editor.destination.get(), str(paths[0].parent.resolve()))
                editor.add_paths(paths[1:2])
                self.assertEqual(editor.destination.get(), "Obok każdego pliku źródłowego")
                custom = root / "copies"
                with patch("batch.transcript_tab.filedialog.askdirectory", return_value=str(custom)):
                    editor.choose_folder()
                editor.add_paths(paths[2:])
                self.assertEqual(editor.destination.get(), str(custom))
                self.assertTrue(editor.destination_custom)
                editor.use_source_folders()
                editor.save(False)
                deadline = time.monotonic() + 5
                while editor.busy and time.monotonic() < deadline:
                    app.window.update()
                self.assertEqual((editor.saved, editor.skipped, editor.failed), (3, 0, 0))
                for source in paths:
                    copies = list(source.parent.glob("*_bez_mowcow.txt"))
                    self.assertEqual(len(copies), 1)
                    self.assertNotIn("Mówca 1:", copies[0].read_text(encoding="utf-8"))
                self.assertFalse(custom.exists())
            finally:
                app.close()

    def test_conditional_batch_writes_real_copies_and_keeps_other_speakers(self):
        from batch.gui import Window
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = Window(root)
            app.window.withdraw()
            try:
                sources = []
                for speaker in ("1", "nieustalony", "2"):
                    source = root / (speaker + ".txt")
                    source.write_text(f"[00:01.000 - 00:02.000] Mówca {speaker}: Tekst.\n", encoding="utf-8")
                    sources.append(source)
                editor = app.transcript_tab
                editor.add_paths(sources)
                editor.save(False)
                deadline = time.monotonic() + 5
                while editor.busy and time.monotonic() < deadline:
                    app.window.update()
                self.assertEqual((editor.saved, editor.skipped, editor.failed), (2, 1, 0))
                self.assertFalse((root / "2_bez_mowcow.txt").exists())
                self.assertIn("Mówca 2:", sources[-1].read_text(encoding="utf-8"))
                self.assertTrue(editor.auto_clean_new.get())
                with patch.object(app, "save_setting", return_value=True) as save:
                    editor.auto_clean_new.set(False)
                    editor.save_auto_option()
                save.assert_called_once_with("omit_single_speaker_labels", False)
            finally:
                app.close()

    def test_tab_previews_and_saves_two_files_without_touching_the_queue(self):
        from batch.gui import Window
        with tempfile.TemporaryDirectory() as tmp:
            app = Window(Path(tmp))
            app.window.withdraw()
            try:
                sources = []
                for index in range(2):
                    path = Path(tmp) / f"film-{index}.txt"
                    path.write_text(f"[00:00:01.000 - 00:00:02.000] Mówca {index+1}: Film {index}.\n", encoding="utf-8")
                    sources.append(path)
                before = app.store.control()
                original = [p.read_bytes() for p in sources]
                editor = app.transcript_tab
                editor.single_only.set(False)
                editor.add_paths(sources)
                editor.preview()
                self.assertIn("Mówca 1", editor.original.get("1.0", "end"))
                self.assertNotIn("Mówca 1", editor.edited.get("1.0", "end"))
                editor.save(False)
                deadline = time.monotonic() + 5
                while editor.busy and time.monotonic() < deadline:
                    app.window.update()
                self.assertFalse(editor.busy)
                self.assertEqual((editor.saved, editor.failed), (2, 0))
                self.assertEqual(len(list(Path(tmp).glob("*_bez_mowcow.txt"))), 2)
                self.assertEqual(original, [p.read_bytes() for p in sources])
                self.assertEqual(before, app.store.control())
            finally:
                app.close()


if __name__ == "__main__":
    unittest.main()
