"""Publication contracts with a stateful API double, never contacting a real channel."""
import copy
from unittest.mock import Mock

from batch.localization.api import ApiError, ConflictError
from batch.localization.publish import (TRACK_NAMES, frozen_approvals, publish_caption, publish_one)
from test_localization import IDENT, SRT, LocalizationCase, remote


class FakeYouTube:
    def __init__(self):
        self.data = remote()
        self.tracks = []
        self.contents = {}
        self.writes, self.uploads = 0, 0
        self.timeout = self.syncing = False

    def video(self, _ident):
        return copy.deepcopy(self.data)

    def update_video(self, body, etag):
        if etag != self.data["etag"]:
            raise ConflictError("ETag conflict")
        self.data.update(copy.deepcopy(body))
        self.data["etag"] = "v2"
        self.writes += 1

    def captions(self, _ident):
        return copy.deepcopy(self.tracks)

    def download_caption(self, ident):
        return self.contents[ident]

    def upload_caption(self, metadata, text, caption_id=""):
        self.uploads += 1
        ident = caption_id or "track-" + str(self.uploads)
        item = {"id": ident, "snippet": {**metadata, "status": "syncing" if self.syncing else "serving"}}
        self.tracks = [t for t in self.tracks if t["id"] != ident] + [item]
        self.contents[ident] = text
        if self.timeout:
            self.timeout = False
            raise ApiError("Timeout after upload")
        return item


class PublicationTests(LocalizationCase):
    def metadata(self):
        for kind, pl, en in (("title", "Polski tytuł", "English title"), ("description", "Polski opis", "English description")):
            self.save_approved(kind, "pl", pl)
            self.save_approved(kind, "en", en)

    def test_publication_preserves_other_languages_tags_category_and_audio_language(self):
        self.metadata()
        api = FakeYouTube()
        frozen = frozen_approvals(self.store, [IDENT])[IDENT]
        publish_one(self.store, api, IDENT, frozen, lambda _: None)
        self.assertEqual(api.data["snippet"]["defaultLanguage"], "pl")
        self.assertEqual(api.data["snippet"]["defaultAudioLanguage"], "en")
        self.assertEqual(api.data["snippet"]["categoryId"], "20")
        self.assertEqual(api.data["snippet"]["tags"], ["game"])
        self.assertEqual(api.data["localizations"]["de"]["title"], "Spiel")
        self.assertEqual(api.data["localizations"]["en"]["title"], "English title")
        self.assertEqual(self.store.item(IDENT, "title", "pl")["publication"], "verified")
        publish_one(self.store, api, IDENT, frozen, lambda _: None)
        self.assertEqual(api.writes, 1)

    def test_edit_after_preview_is_rejected_without_any_write(self):
        self.metadata()
        frozen = frozen_approvals(self.store, [IDENT])[IDENT]
        self.store.save_item(IDENT, "title", "pl", "Unreviewed change")
        api = FakeYouTube()
        with self.assertRaises(ConflictError):
            publish_one(self.store, api, IDENT, frozen, lambda _: None)
        self.assertEqual(api.writes, 0)

    def test_external_change_requires_new_review(self):
        self.metadata()
        api = FakeYouTube()
        api.data["snippet"]["description"] = "Edited in Studio"
        with self.assertRaises(ConflictError):
            publish_one(self.store, api, IDENT, frozen_approvals(self.store, [IDENT])[IDENT], lambda _: None)
        self.assertEqual(api.writes, 0)

    def test_timeout_after_caption_creation_is_verified_without_duplicate_upload(self):
        item = self.save_approved("captions", "pl", SRT)
        api = FakeYouTube()
        api.timeout = True
        with self.assertRaises(ApiError):
            publish_caption(self.store, api, IDENT, item, lambda _: None)
        publish_caption(self.store, api, IDENT, item, lambda _: None)
        self.assertEqual(api.uploads, 1)
        self.assertEqual(self.store.item(IDENT, "captions", "pl")["publication"], "verified")

    def test_processing_caption_is_not_marked_published_and_can_be_verified_later(self):
        item = self.save_approved("captions", "en", SRT)
        api = FakeYouTube()
        api.syncing = True
        publish_caption(self.store, api, IDENT, item, lambda _: None)
        self.assertEqual(self.store.item(IDENT, "captions", "en")["publication"], "processing")
        api.tracks[0]["snippet"]["status"] = "serving"
        publish_caption(self.store, api, IDENT, self.store.item(IDENT, "captions", "en"), lambda _: None)
        self.assertEqual(api.uploads, 1)
        self.assertEqual(self.store.item(IDENT, "captions", "en")["publication"], "verified")

    def test_conflicting_preexisting_named_track_is_not_overwritten(self):
        item = self.save_approved("captions", "pl", SRT)
        api = FakeYouTube()
        api.upload_caption({"language": "pl", "name": TRACK_NAMES["pl"], "isDraft": False}, SRT.replace("Dzień dobry", "Different"))
        with self.assertRaises(ConflictError):
            publish_caption(self.store, api, IDENT, item, lambda _: None)
        self.assertEqual(api.uploads, 1)

    def test_no_approved_items_means_no_api_activity(self):
        api = Mock()
        publish_one(self.store, api, IDENT, {}, lambda _: None)
        api.video.assert_not_called()
