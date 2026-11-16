"""Durable localization state, subtitle invariants and quota boundaries."""
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from batch.localization import directory
from batch.localization.api import QuotaError, quota_day
from batch.localization.captions import parse_srt, render
from batch.localization.editor import save_captions
from batch.localization.store import LocalizationStore
from batch.store import Store

IDENT = "abcdefghij0"
SRT = "1\n00:00:01,000 --> 00:00:03,000\nMówca 1: Dzień dobry.\n\n2\n00:00:04,200 --> 00:00:06,400\nSpeaker 2: Good morning.\n"


def remote(title="Original title", description="Original description"):
    return {"id": IDENT, "etag": "v1", "snippet": {"title": title, "description": description,
            "channelId": "UC" + "a" * 22, "categoryId": "20", "defaultLanguage": "en", "defaultAudioLanguage": "en", "tags": ["game"]},
            "localizations": {"de": {"title": "Spiel", "description": "Beschreibung"}},
            "contentDetails": {"duration": "PT10S"}}


class LocalizationCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        base = Store(self.root)
        base.add_many([{"identity": "yt:" + IDENT, "kind": "youtube", "title": "Film", "source": "https://youtube.com/watch?v=" + IDENT, "duration": 10, "status": "done"}])
        self.store = LocalizationStore(self.root)
        self.store.snapshot(remote())

    def tearDown(self):
        self.temp.cleanup()

    def save_approved(self, kind, lang, value):
        self.store.save_item(IDENT, kind, lang, value, "source")
        self.store.approve(IDENT, kind, lang, allow_empty=not value)
        return self.store.item(IDENT, kind, lang)


class StateTests(LocalizationCase):
    def test_session_rejects_unknown_keys_mixed_with_valid_updates_atomically(self):
        before = self.store.session()
        with self.assertRaises(ValueError):
            self.store.session(state="running", unexpected="outside schema")
        self.assertEqual(self.store.session(), before)

    def test_public_metadata_migration_preserves_legacy_snapshots_and_asr(self):
        before = self.store.base.jobs()
        with self.store.base.connect() as db:
            for column in ("public_snapshot", "public_synced_at", "metadata_error"):
                db.execute("ALTER TABLE ytl_videos DROP COLUMN " + column)
        reopened = LocalizationStore(self.root)
        self.assertEqual(reopened.base.jobs(), before)
        self.assertEqual(reopened.get_video(IDENT)["snapshot"], self.store.get_video(IDENT)["snapshot"])
        self.assertEqual(reopened.get_video(IDENT)["public_snapshot"], "{}")

    def test_public_preview_never_becomes_a_publication_baseline(self):
        from batch.localization.store import metadata_source
        original = self.store.get_video(IDENT)["snapshot"]
        public = {"id": IDENT, "source": "public", "snippet": {"title": "Public title", "description": "Polski opis"}}
        self.store.public_metadata(public)
        video = self.store.get_video(IDENT)
        self.assertEqual(video["snapshot"], original)
        self.assertEqual(metadata_source(video)["snippet"]["title"], "Public title")
        self.assertNotIn("localizations", metadata_source(video))

    def test_public_change_revokes_metadata_approval_without_touching_captions(self):
        self.save_approved("title", "pl", "Tytuł")
        self.save_approved("captions", "pl", render(parse_srt(SRT)))
        self.store.public_metadata({"id": IDENT, "source": "public", "snippet": {"title": "Changed", "description": "Other"}})
        self.assertEqual(self.store.item(IDENT, "title", "pl")["review"], "review")
        self.assertEqual(self.store.item(IDENT, "captions", "pl")["review"], "approved")

    def test_missing_source_preview_distinguishes_not_fetched_from_empty_description(self):
        from batch.localization.metadata import source_text
        with self.store.base.connect() as db:
            db.execute("UPDATE ytl_videos SET snapshot='{}' WHERE video_id=?", (IDENT,))
        text = source_text(self.store.get_video(IDENT))
        self.assertIn("Tytuł z katalogu: Film", text)
        self.assertIn("Opis nie został pobrany", text)
        self.store.public_metadata({"id": IDENT, "source": "public", "snippet": {"title": "Title", "description": ""}})
        text = source_text(self.store.get_video(IDENT))
        self.assertIn("Opis na YouTube jest pusty", text)
        self.assertIn("Nie sprawdzono", text)

    def test_migration_is_idempotent_and_does_not_reset_transcription(self):
        LocalizationStore(self.root)
        self.assertEqual(len(self.store.items(IDENT)), 9)
        self.assertEqual(self.store.base.jobs()[0]["status"], "done")
        self.assertEqual(self.store.item(IDENT, "audio", "en")["review"], "deferred")

    def test_edit_invalidates_only_changed_approval_and_published_revision(self):
        title = self.save_approved("title", "pl", "Tytuł")
        self.save_approved("description", "pl", "Opis")
        self.store.mark(IDENT, "title", "pl", "verified", title["content_hash"])
        self.store.save_item(IDENT, "title", "pl", "Inny tytuł")
        self.assertEqual(self.store.item(IDENT, "title", "pl")["approval_hash"], "")
        self.assertEqual(self.store.item(IDENT, "title", "pl")["publication"], "none")
        self.assertEqual(self.store.item(IDENT, "description", "pl")["review"], "approved")

    def test_changed_source_invalidates_titles_without_invalidating_captions(self):
        self.save_approved("title", "pl", "Tytuł")
        self.save_approved("captions", "pl", render(parse_srt(SRT)))
        self.store.snapshot(remote(title="Changed title"))
        self.assertEqual(self.store.item(IDENT, "title", "pl")["review"], "review")
        self.assertEqual(self.store.item(IDENT, "captions", "pl")["review"], "approved")
        self.assertTrue(self.store.get_video(IDENT)["conflict"])

    def test_owned_publication_refresh_keeps_approval(self):
        self.save_approved("title", "pl", "Tytuł")
        self.store.snapshot(remote(title="Tytuł"), accepted=True)
        self.assertEqual(self.store.item(IDENT, "title", "pl")["review"], "approved")

    def test_generated_result_does_not_overwrite_approved_correction(self):
        old = self.save_approved("title", "pl", "Poprawiony tytuł")
        self.store.save_item(IDENT, "title", "pl", "Nowy automat", generated=True)
        self.assertEqual(self.store.item(IDENT, "title", "pl")["value"], old["value"])

    def test_empty_description_requires_explicit_acceptance(self):
        self.store.save_item(IDENT, "description", "pl", "")
        with self.assertRaises(ValueError):
            self.store.approve(IDENT, "description", "pl")
        self.store.approve(IDENT, "description", "pl", allow_empty=True)
        self.assertEqual(self.store.item(IDENT, "description", "pl")["review"], "approved")

    def test_summary_does_not_load_subtitle_bodies(self):
        self.store.save_item(IDENT, "captions", "pl", SRT)
        self.assertNotIn("value", self.store.rows()[0]["items"]["captions:pl"])

    def test_quota_reservation_is_atomic_and_failed_reservation_is_not_charged(self):
        self.store.reserve_quota("2026-11-13", 400, 900)
        with self.assertRaises(QuotaError):
            self.store.reserve_quota("2026-11-13", 600, 900)
        self.store.reserve_quota("2026-11-13", 500, 900)
        self.store.reserve_quota("2026-11-14", 400, 900)

    def test_video_paths_cannot_escape_the_asset_root(self):
        with self.assertRaises(ValueError):
            directory(self.root, "../outside")
        self.assertEqual(directory(self.root, IDENT), self.root.resolve() / "lokalizacje" / IDENT)

    def test_csv_neutralizes_spreadsheet_formulas(self):
        self.store.base.add_many([{"identity": "yt:abcdefghij1", "kind": "youtube", "title": "=DANGEROUS()", "source": "url"}])
        self.store.bootstrap()
        target = self.root / "report.csv"
        self.store.csv(target)
        self.assertIn("'=DANGEROUS()", target.read_text(encoding="utf-8-sig"))

    def test_local_scan_excludes_localization_audio_and_workspaces(self):
        from batch.local_outputs import scan_media
        audio = directory(self.root, IDENT) / "audio" / "bez-glosow.flac"
        audio.parent.mkdir(parents=True)
        audio.write_bytes(b"generated-audio")
        self.assertEqual(list(scan_media(self.root, {".flac"})), [])


class SubtitleTests(LocalizationCase):
    def test_speaker_cleanup_preserves_original_times_and_ids(self):
        cues = parse_srt(SRT)
        self.assertEqual(cues[0]["text"], "Dzień dobry.")
        self.assertEqual(cues[1]["start"], 4.2)
        self.assertEqual(parse_srt(render(cues)), cues)

    def test_missing_reordered_invalid_and_out_of_range_cues_are_rejected(self):
        for text in [SRT.replace("00:00:03,000", "00:00:00,500"), SRT.replace("00:00:06,400", "00:61:00,000")]:
            with self.assertRaises(ValueError):
                parse_srt(text)
        with self.assertRaises(ValueError):
            parse_srt(SRT, duration=5)

    def test_correction_preserves_times_and_missing_cue_is_rejected(self):
        self.store.save_item(IDENT, "captions", "pl", render(parse_srt(SRT)), "source")
        save_captions(self.store, IDENT, "pl", SRT.replace("Dzień dobry.", "Witaj."))
        self.assertIn("Witaj", self.store.item(IDENT, "captions", "pl")["value"])
        incomplete = render(parse_srt(SRT)[:1])
        with self.assertRaises(ValueError):
            save_captions(self.store, IDENT, "pl", incomplete)

    def test_no_speech_has_an_explicit_not_applicable_state(self):
        from batch.localization.prepare import caption_units
        self.assertEqual(caption_units(self.store, IDENT, []), [])
        self.assertEqual(self.store.item(IDENT, "captions", "en")["review"], "na")

    def test_replaced_speech_free_source_can_prepare_newly_available_cues(self):
        from batch.localization.prepare import caption_units
        caption_units(self.store, IDENT, [])
        units = caption_units(self.store, IDENT, parse_srt(SRT))
        self.assertEqual(len(units), 4)


class ClockTests(unittest.TestCase):
    def test_quota_reset_uses_pacific_day_including_daylight_saving(self):
        self.assertEqual(quota_day(datetime(2026, 11, 13, 7, 59, tzinfo=timezone.utc)), "2026-11-12")
        self.assertEqual(quota_day(datetime(2026, 11, 13, 8, tzinfo=timezone.utc)), "2026-11-13")
        self.assertEqual(quota_day(datetime(2026, 7, 13, 6, 59, tzinfo=timezone.utc)), "2026-07-12")
        self.assertEqual(quota_day(datetime(2026, 7, 13, 7, tzinfo=timezone.utc)), "2026-07-13")


if __name__ == "__main__":
    unittest.main()
