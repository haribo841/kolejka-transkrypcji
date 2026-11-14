"""Publish a frozen reviewed revision, journal before writes, verify before retry."""
import html
import json
import re

from ..common import signature
from .api import ConflictError, ApiError
from .captions import parse_srt
from .store import source_field

WRITABLE_SNIPPET = ("title", "description", "tags", "categoryId", "defaultLanguage", "defaultAudioLanguage")
TRACK_NAMES = {"pl": "Polski (Kolejka transkrypcji)", "en": "English (Kolejka transkrypcji)"}


def approved(item):
    return item["review"] == "approved" and item["approval_hash"] == item["content_hash"] and bool(item["content_hash"])


def frozen_approvals(store, ids):
    result = {}
    for ident in ids:
        candidates = {i["kind"] + ":" + i["lang"]: i for i in store.items(ident) if approved(i) and i["kind"] != "audio"}
        for lang in ("pl", "en"):
            keys = ("title:" + lang, "description:" + lang)
            if not all(k in candidates for k in keys):
                for key in keys:
                    candidates.pop(key, None)
        result[ident] = {key: item["content_hash"] for key, item in candidates.items()
                         if item["publication"] != "verified" or item["published_hash"] != item["content_hash"]}
        # Metadata is written as a pair, even if just one approved field changed.
        for lang in ("pl", "en"):
            keys = ("title:" + lang, "description:" + lang)
            if any(k in result[ident] for k in keys):
                result[ident].update({k: candidates[k]["content_hash"] for k in keys})
    return result


def selected_items(store, ident, frozen):
    result = {}
    for item in store.items(ident):
        key = item["kind"] + ":" + item["lang"]
        if key not in frozen:
            continue
        if not approved(item) or frozen[key] != item["content_hash"]:
            raise ConflictError("Zmieniono treść od podglądu. Otwórz nowy podgląd i zatwierdź publikację.")
        result[key] = item
    return result


def metadata_body(remote, items):
    snippet = {k: v for k, v in remote["snippet"].items() if k in WRITABLE_SNIPPET}
    localizations = dict(remote.get("localizations", {}))
    used = []
    for lang in ("pl", "en"):
        keys = ["title:" + lang, "description:" + lang]
        if not all(key in items for key in keys):
            continue
        pair = {kind: items[kind + ":" + lang]["value"] for kind in ("title", "description")}
        if lang == "pl":
            snippet.update(pair, defaultLanguage="pl")
            if "pl" in localizations:
                localizations["pl"] = pair
        else:
            localizations["en"] = pair
        used.extend(keys)
    if used and snippet.get("defaultLanguage") != "pl":
        raise ValueError("Zatwierdź polski tytuł i opis, aby ustawić spójne metadane podstawowe.")
    return {"id": remote["id"], "snippet": snippet, "localizations": localizations}, used


def relevant(remote):
    return {"snippet": {k: v for k, v in remote.get("snippet", {}).items() if k in WRITABLE_SNIPPET},
            "localizations": remote.get("localizations", {})}


def check_baseline(store, ident, remote):
    baseline = json.loads(store.get_video(ident)["snapshot"])
    if not baseline or relevant(baseline) != relevant(remote):
        raise ConflictError("Metadane na YouTube różnią się od podglądu. Odśwież i zatwierdź zmiany ponownie.")


def publish_metadata(store, api, ident, items, progress):
    remote = api.video(ident)
    body, keys = metadata_body(remote, items)
    if not keys:
        return
    target = {"snippet": body["snippet"], "localizations": body["localizations"]}
    checksum = signature(target)
    operation = store.operation(ident, "metadata", "pl-en", checksum, remote, target)
    if relevant(remote) != target:
        check_baseline(store, ident, remote)
        progress("Publikacja metadanych PL/EN")
        api.update_video(body, remote["etag"])
        remote = api.video(ident)
    if relevant(remote) != target:
        raise ApiError("Odczyt kontrolny metadanych nie zgadza się z zatwierdzoną treścią.")
    store.finish_operation(operation["id"], "verified", remote)
    for key in keys:
        item = items[key]
        store.mark(ident, item["kind"], item["lang"], "verified", item["content_hash"])
    store.snapshot(remote, accepted=True)


def canonical_captions(text):
    cues = parse_srt(text)
    return [(round(c["start"] * 1000), round(c["end"] * 1000),
             re.sub(r"\s+", " ", html.unescape(c["text"])).strip()) for c in cues]


def find_track(tracks, item):
    if item["remote_id"]:
        found = next((t for t in tracks if t["id"] == item["remote_id"]), None)
        if found is not None:
            return found
    return next((t for t in tracks if t["snippet"].get("language") == item["lang"] and
                 t["snippet"].get("name") == TRACK_NAMES[item["lang"]]), None)


def verify_track(api, track, item):
    snippet = track["snippet"]
    if snippet.get("isDraft") or snippet.get("status") != "serving":
        return False
    return canonical_captions(api.download_caption(track["id"])) == canonical_captions(item["value"])


def publish_caption(store, api, ident, item, progress):
    lang = item["lang"]
    parse_srt(item["value"], duration=store.get_video(ident)["duration"])
    if not item["value"].strip():
        return
    tracks = api.captions(ident)
    track = find_track(tracks, item)
    checksum = item["content_hash"]
    operation = store.operation(ident, "captions", lang, checksum, tracks, {"srt": item["value"]})
    if track and verify_track(api, track, item):
        finish_caption(store, ident, item, operation, track)
        return
    if track and track["snippet"].get("status") == "syncing":
        store.mark(ident, "captions", lang, "processing", checksum, track["id"])
        store.finish_operation(operation["id"], "processing", track, track["id"])
        return
    if track and not item["remote_id"] and operation["created"] == operation["updated"]:
        # A conflicting pre-existing named track is not an authorization to replace it.
        raise ConflictError("Istnieje inna ścieżka o nazwie programu. Otwórz Studio i sprawdź ją przed zastąpieniem.")
    progress("Publikacja napisów " + lang.upper())
    metadata = {"videoId": ident, "language": lang, "name": TRACK_NAMES[lang], "isDraft": False}
    uploaded = api.upload_caption(metadata, item["value"], caption_id=track["id"] if track else "")
    remote_id = uploaded["id"]
    store.mark(ident, "captions", lang, "processing", checksum, remote_id)
    store.finish_operation(operation["id"], "processing", uploaded, remote_id)
    tracks = api.captions(ident)
    current = next((t for t in tracks if t["id"] == remote_id), None)
    if current and verify_track(api, current, item):
        finish_caption(store, ident, item, operation, current)


def finish_caption(store, ident, item, operation, track):
    if track["snippet"].get("videoId") not in {None, ident} or track["snippet"].get("language") != item["lang"]:
        raise ApiError("YouTube zwrócił inną ścieżkę napisów.")
    store.mark(ident, "captions", item["lang"], "verified", item["content_hash"], track["id"])
    store.finish_operation(operation["id"], "verified", track, track["id"])


def publish_one(store, api, ident, frozen, progress):
    items = selected_items(store, ident, frozen)
    if not items:
        return
    publish_metadata(store, api, ident, items, progress)
    for lang in ("pl", "en"):
        key = "captions:" + lang
        if key in items:
            publish_caption(store, api, ident, items[key], progress)


def preview(store, ids):
    lines = []
    estimated = 0
    for ident in ids:
        video = store.get_video(ident)
        snapshot = json.loads(video["snapshot"])
        lines.extend(["\n" + video["title"] + " [" + ident + "]", "Podstawowy język metadanych: polski. Język audio pozostaje bez zmian."])
        for item in store.items(ident):
            if approved(item) and item["kind"] != "audio":
                value = item["value"] if item["kind"] != "captions" else f"{len(parse_srt(item['value']))} segmentów z własnymi timestampami"
                old = source_field(snapshot, item["kind"], item["lang"]) if item["kind"] != "captions" else "Istniejące ścieżki innych nazw pozostają zachowane."
                lines.append(f"\n{item['kind']} {item['lang'].upper()}\nObecnie: {old}\nPo zmianie: {value}")
        frozen = frozen_approvals(store, [ident])[ident]
        if frozen:
            estimated += 52 if any(k.startswith(("title:", "description:")) for k in frozen) else 1
            estimated += sum(950 for k in frozen if k.startswith("captions:"))
    lines.append("\nNapisy: insert 400 lub update 450 jednostek; kontrola listy 50 i odczyt treści 200. Metadane: zapis 50. Odczyty i ponowienia też zużywają pulę.")
    lines.append(f"Szacowany górny koszt tej partii: {estimated} jednostek API. Przy wyczerpaniu budżetu publikacja zatrzyma się z zachowaniem postępu.")
    return "\n".join(lines)
