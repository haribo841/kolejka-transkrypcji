"""Transactional localization state. Never changes transcription status or filenames."""
import csv
from datetime import datetime, timezone
import json
import uuid

from ..common import atomic_new_bytes, signature
from ..store import Store, now
from . import KINDS, LANGUAGES, video_id


def packed(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


SESSION_UPDATES = {
    "mode": "UPDATE ytl_session SET mode=? WHERE id=1",
    "state": "UPDATE ytl_session SET state=? WHERE id=1",
    "ids": "UPDATE ytl_session SET ids=? WHERE id=1",
    "cursor": "UPDATE ytl_session SET cursor=? WHERE id=1",
    "current_id": "UPDATE ytl_session SET current_id=? WHERE id=1",
    "stop": "UPDATE ytl_session SET stop=? WHERE id=1",
    "message": "UPDATE ytl_session SET message=? WHERE id=1",
    "pid": "UPDATE ytl_session SET pid=? WHERE id=1",
    "heartbeat": "UPDATE ytl_session SET heartbeat=? WHERE id=1",
    "tasks": "UPDATE ytl_session SET tasks=? WHERE id=1",
}


class LocalizationStore:
    def __init__(self, root, *, timeout=3):
        self.base = Store(root, timeout=timeout)
        self.root = self.base.root
        assets = self.root / "lokalizacje"
        assets.mkdir(parents=True, exist_ok=True)
        try:
            atomic_new_bytes(assets / ".kolejka-nagranie.json", b'{"kind":"youtube-localizations","schema":1}')
        except FileExistsError:
            pass
        with self.base.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS ytl_videos(
                    video_id TEXT PRIMARY KEY,job_id INTEGER UNIQUE REFERENCES jobs(id),
                    snapshot TEXT NOT NULL DEFAULT '{}',source_hash TEXT NOT NULL DEFAULT '',
                    synced_at TEXT NOT NULL DEFAULT '',conflict TEXT NOT NULL DEFAULT '',
                    language_overrides TEXT NOT NULL DEFAULT '{}');
                CREATE TABLE IF NOT EXISTS ytl_items(
                    video_id TEXT REFERENCES ytl_videos(video_id),kind TEXT,lang TEXT,
                    value TEXT NOT NULL DEFAULT '',source_hash TEXT NOT NULL DEFAULT '',
                    artifact_path TEXT NOT NULL DEFAULT '',content_hash TEXT NOT NULL DEFAULT '',
                    revision INTEGER NOT NULL DEFAULT 0,review TEXT NOT NULL DEFAULT 'missing',
                    approval_hash TEXT NOT NULL DEFAULT '',published_hash TEXT NOT NULL DEFAULT '',
                    publication TEXT NOT NULL DEFAULT 'none',remote_id TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',updated TEXT NOT NULL,
                    PRIMARY KEY(video_id,kind,lang));
                CREATE TABLE IF NOT EXISTS ytl_ops(
                    id TEXT PRIMARY KEY,video_id TEXT,kind TEXT,lang TEXT,content_hash TEXT,
                    state TEXT NOT NULL,before_json TEXT NOT NULL,expected_json TEXT NOT NULL,
                    result_json TEXT NOT NULL DEFAULT '{}',remote_id TEXT NOT NULL DEFAULT '',
                    created TEXT NOT NULL,updated TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS ytl_session(
                    id INTEGER PRIMARY KEY CHECK(id=1),mode TEXT DEFAULT '',state TEXT DEFAULT 'idle',
                    ids TEXT DEFAULT '[]',cursor INTEGER DEFAULT 0,current_id TEXT DEFAULT '',
                    stop INTEGER DEFAULT 1,message TEXT DEFAULT '',pid INTEGER,
                    heartbeat TEXT DEFAULT '',tasks TEXT DEFAULT '[]');
                INSERT OR IGNORE INTO ytl_session(id) VALUES(1);
                CREATE TABLE IF NOT EXISTS ytl_quota(day TEXT PRIMARY KEY,used INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS ytl_history(
                    id INTEGER PRIMARY KEY,video_id TEXT,kind TEXT,lang TEXT,revision INTEGER,
                    value TEXT,source_hash TEXT,content_hash TEXT,review TEXT,created TEXT);
            ''')
            columns = {row[1] for row in db.execute("PRAGMA table_info(ytl_videos)")}
            if "public_snapshot" not in columns:
                db.execute("ALTER TABLE ytl_videos ADD COLUMN public_snapshot TEXT NOT NULL DEFAULT '{}'")
            if "public_synced_at" not in columns:
                db.execute("ALTER TABLE ytl_videos ADD COLUMN public_synced_at TEXT NOT NULL DEFAULT ''")
            if "metadata_error" not in columns:
                db.execute("ALTER TABLE ytl_videos ADD COLUMN metadata_error TEXT NOT NULL DEFAULT ''")
        self.bootstrap()

    def bootstrap(self):
        with self.base.connect() as db:
            db.execute("INSERT OR IGNORE INTO ytl_videos(video_id,job_id) SELECT substr(identity,4),id FROM jobs WHERE kind='youtube' AND identity LIKE 'yt:%'")
            for kind, lang in [(kind, lang) for kind in ("title", "description", "captions") for lang in LANGUAGES] + [("audio", "zxx"), ("audio", "pl"), ("audio", "en")]:
                state = "deferred" if kind == "audio" and lang != "zxx" else "missing"
                db.execute("INSERT OR IGNORE INTO ytl_items(video_id,kind,lang,review,updated) SELECT video_id,?,?,?,? FROM ytl_videos", (kind, lang, state, now()))

    def rows(self):
        with self.base.connect() as db:
            videos = [dict(r) for r in db.execute("SELECT v.video_id,v.job_id,v.synced_at,v.conflict,v.metadata_error,j.title,j.date,j.duration,j.status AS transcription FROM ytl_videos v JOIN jobs j ON j.id=v.job_id ORDER BY CASE WHEN j.date='' THEN 1 ELSE 0 END,j.date,j.id")]
            items = [dict(r) for r in db.execute("SELECT video_id,kind,lang,review,publication,error,content_hash,published_hash FROM ytl_items")]
        grouped = {}
        for item in items:
            grouped.setdefault(item["video_id"], {})[item["kind"] + ":" + item["lang"]] = item
        for row in videos:
            row["items"] = grouped.get(row["video_id"], {})
        return videos

    def get_video(self, ident):
        with self.base.connect() as db:
            row = db.execute("SELECT v.*,j.title,j.source,j.duration,j.folder,j.output_root,j.identity,j.status FROM ytl_videos v JOIN jobs j ON j.id=v.job_id WHERE video_id=?", (video_id(ident),)).fetchone()
            if row is None:
                raise ValueError("Filmu nie ma w katalogu lokalizacji.")
            return dict(row)

    def items(self, ident):
        with self.base.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM ytl_items WHERE video_id=? ORDER BY kind,lang", (video_id(ident),))]

    def item(self, ident, kind, lang):
        return next(i for i in self.items(ident) if i["kind"] == kind and i["lang"] == lang)

    def snapshot(self, data, *, accepted=False):
        ident = video_id(data["id"])
        relevant = {key: data.get(key, {}) for key in ("snippet", "localizations", "contentDetails")}
        checksum = signature(relevant)
        with self.base.connect() as db:
            old = db.execute("SELECT snapshot,source_hash FROM ytl_videos WHERE video_id=?", (ident,)).fetchone()
            if old is None:
                raise ValueError("Najpierw dodaj film do katalogu.")
            conflict = "Dane na YouTube zmieniły się. Porównaj przed publikacją." if not accepted and old["source_hash"] and old["source_hash"] != checksum else ""
            if conflict:
                previous = json.loads(old["snapshot"])
                for lang in LANGUAGES:
                    for kind in ("title", "description"):
                        before = source_field(previous, kind, lang)
                        after = source_field(data, kind, lang)
                        if before != after:
                            db.execute("UPDATE ytl_items SET review=CASE WHEN review='approved' THEN 'review' ELSE review END,approval_hash='' WHERE video_id=? AND kind=? AND lang=?", (ident, kind, lang))
            db.execute("UPDATE ytl_videos SET snapshot=?,source_hash=?,synced_at=?,conflict=?,metadata_error='' WHERE video_id=?", (packed(data), checksum, datetime.now(timezone.utc).isoformat(), conflict, ident))

    def public_metadata(self, data):
        """A public preview is never an authenticated publication baseline."""
        ident = video_id(data["id"])
        previous = metadata_source(self.get_video(ident))
        with self.base.connect() as db:
            for kind in ("title", "description"):
                for lang in LANGUAGES:
                    if previous and source_field(previous, kind, lang) != source_field(data, kind, lang):
                        db.execute("UPDATE ytl_items SET review=CASE WHEN review='approved' THEN 'review' ELSE review END,approval_hash='' WHERE video_id=? AND kind=? AND lang=?", (ident, kind, lang))
            db.execute("UPDATE ytl_videos SET public_snapshot=?,public_synced_at=?,metadata_error='' WHERE video_id=?", (packed(data), datetime.now(timezone.utc).isoformat(), ident))

    def metadata_error(self, ident, message):
        with self.base.connect() as db:
            db.execute("UPDATE ytl_videos SET metadata_error=? WHERE video_id=?", (message[:1500], video_id(ident)))

    def language_overrides(self, ident, title, description):
        if title not in {"auto", "pl", "en"} or description not in {"auto", "pl", "en"}:
            raise ValueError("Język źródła: auto, pl albo en.")
        with self.base.connect() as db:
            old = json.loads(db.execute("SELECT language_overrides FROM ytl_videos WHERE video_id=?", (ident,)).fetchone()[0])
            for kind, value in (("title", title), ("description", description)):
                if old.get(kind, "auto") != value:
                    db.execute("UPDATE ytl_items SET review=CASE WHEN review='approved' THEN 'review' ELSE review END,approval_hash='' WHERE video_id=? AND kind=?", (ident, kind))
            db.execute("UPDATE ytl_videos SET language_overrides=? WHERE video_id=?", (packed({"title": title, "description": description}), video_id(ident)))

    def save_item(self, ident, kind, lang, value, source_hash="", artifact_path="", *, generated=False, na=False):
        if kind not in KINDS or lang not in (*LANGUAGES, "zxx"):
            raise ValueError("Niepoprawny rodzaj lokalizacji.")
        old = self.item(ident, kind, lang)
        if generated and old["review"] == "approved":
            return old
        checksum = signature({"value": value, "path": artifact_path})
        if old["content_hash"] == checksum and old["source_hash"] == source_hash:
            return old
        with self.base.connect() as db:
            if old["content_hash"]:
                db.execute("INSERT INTO ytl_history(video_id,kind,lang,revision,value,source_hash,content_hash,review,created) VALUES(?,?,?,?,?,?,?,?,?)", (ident, kind, lang, old["revision"], old["value"], old["source_hash"], old["content_hash"], old["review"], now()))
            db.execute("UPDATE ytl_items SET value=?,source_hash=?,artifact_path=?,content_hash=?,revision=revision+1,review=?,approval_hash='',publication='none',error='',updated=? WHERE video_id=? AND kind=? AND lang=?",
                       (value, source_hash, artifact_path, checksum, "na" if na else "review", now(), ident, kind, lang))
        return self.item(ident, kind, lang)

    def approve(self, ident, kind, lang, *, allow_empty=False):
        from .captions import parse_srt
        item = self.item(ident, kind, lang)
        if kind == "title" and (not item["value"].strip() or len(item["value"]) > 100):
            raise ValueError("Tytuł musi zawierać od 1 do 100 znaków.")
        if kind in {"title", "description"} and any(c in item["value"] for c in "<>"):
            raise ValueError("Tytuł i opis nie mogą zawierać znaków < ani >.")
        if kind == "description" and len(item["value"].encode("utf-8")) > 5000:
            raise ValueError("Opis przekracza 5000 bajtów UTF-8.")
        if not item["value"].strip() and not allow_empty:
            raise ValueError("Potwierdź świadome pozostawienie pustej treści.")
        if kind == "captions" and item["value"].strip():
            parse_srt(item["value"], duration=self.get_video(ident)["duration"])
        if not item["content_hash"] or item["review"] == "deferred":
            raise ValueError("Najpierw przygotuj ten element.")
        with self.base.connect() as db:
            db.execute("UPDATE ytl_items SET review=?,approval_hash=content_hash,updated=? WHERE video_id=? AND kind=? AND lang=?", ("na" if not item["value"].strip() and kind == "captions" else "approved", now(), ident, kind, lang))

    def revoke(self, ident, kind, lang):
        with self.base.connect() as db:
            db.execute("UPDATE ytl_items SET review='review',approval_hash='',updated=? WHERE video_id=? AND kind=? AND lang=?", (now(), ident, kind, lang))

    def mark(self, ident, kind, lang, state, checksum, remote_id="", error=""):
        with self.base.connect() as db:
            db.execute("UPDATE ytl_items SET publication=?,published_hash=?,remote_id=?,error=?,updated=? WHERE video_id=? AND kind=? AND lang=? AND content_hash=?", (state, checksum if state in {"verified", "manual"} else "", remote_id, error, now(), ident, kind, lang, checksum))

    def error(self, ident, kind, lang, message):
        with self.base.connect() as db:
            db.execute("UPDATE ytl_items SET error=?,updated=? WHERE video_id=? AND kind=? AND lang=?", (message[:1500], now(), ident, kind, lang))

    def session(self, **values):
        if not values.keys() <= SESSION_UPDATES.keys():
            raise ValueError("Niepoprawne ustawienia sesji lokalizacji.")
        with self.base.connect() as db:
            for key, value in values.items():
                db.execute(SESSION_UPDATES[key], (value,))
            return dict(db.execute("SELECT * FROM ytl_session WHERE id=1").fetchone())

    def reserve_quota(self, day, cost, budget):
        from .api import QuotaError
        if type(budget) is not int or not 1 <= budget <= 10000 or cost <= 0:
            raise ValueError("Budżet API musi wynosić od 1 do 10000.")
        with self.base.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT OR IGNORE INTO ytl_quota VALUES(?,0)", (day,))
            used = db.execute("SELECT used FROM ytl_quota WHERE day=?", (day,)).fetchone()[0]
            if used + cost > budget:
                raise QuotaError("Dzienny budżet API wykorzystany. Ponów publikację w kolejnej puli.")
            db.execute("UPDATE ytl_quota SET used=used+? WHERE day=?", (cost, day))

    def operation(self, ident, kind, lang, checksum, before, expected):
        with self.base.connect() as db:
            old = db.execute("SELECT * FROM ytl_ops WHERE video_id=? AND kind=? AND lang=? AND content_hash=? AND state IN ('sending','processing','verified') ORDER BY created DESC LIMIT 1", (ident, kind, lang, checksum)).fetchone()
            if old:
                return dict(old)
            key = uuid.uuid4().hex
            db.execute("INSERT INTO ytl_ops(id,video_id,kind,lang,content_hash,state,before_json,expected_json,created,updated) VALUES(?,?,?,?,?,'sending',?,?,?,?)", (key, ident, kind, lang, checksum, packed(before), packed(expected), now(), now()))
            return dict(db.execute("SELECT * FROM ytl_ops WHERE id=?", (key,)).fetchone())

    def finish_operation(self, key, state, result, remote_id=""):
        with self.base.connect() as db:
            db.execute("UPDATE ytl_ops SET state=?,result_json=?,remote_id=?,updated=? WHERE id=?", (state, packed(result), remote_id, now(), key))

    def csv(self, path):
        def safe(value):
            text = str(value)
            return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) else text
        with open(path, "w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream, delimiter=";")
            writer.writerow(["ID", "Tytuł", "Element", "Język", "Przygotowanie", "Publikacja", "Błąd"])
            for row in self.rows():
                for item in row["items"].values():
                    writer.writerow([safe(v) for v in (row["video_id"], row["title"], item["kind"], item["lang"], item["review"], item["publication"], item["error"])])


def source_field(snapshot, kind, lang):
    localized = snapshot.get("localizations", {}).get(lang)
    if localized is not None:
        return localized.get(kind, "")
    return snapshot.get("snippet", {}).get(kind, "")


def metadata_source(video):
    public = json.loads(video.get("public_snapshot", "{}"))
    authenticated = json.loads(video["snapshot"])
    if public and (not authenticated or video.get("public_synced_at", "") > video["synced_at"]):
        return public
    return authenticated
