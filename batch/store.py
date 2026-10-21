from __future__ import annotations

import csv
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
from .paths import output_file
import sqlite3

from .common import job_folder, slug

STATUSES = {
    "pending": "Oczekuje", "running": "Przetwarzanie", "done": "Gotowe", "partial": "Częściowo gotowe",
    "error": "Błąd", "unavailable": "Niedostępny", "login_required": "Wymaga logowania",
    "scheduled": "Zaplanowany", "live": "Trwa transmisja", "draft": "Szkic bez linku",
    "blocked": "Blokada YouTube",
    "skipped": "Pominięte",
    "no_audio": "Bez ścieżki audio", "output_required": "Wybierz miejsce wyników",
}
RETRYABLE = ("error", "partial", "unavailable", "login_required", "scheduled", "live", "blocked")
SCOPE_KINDS = ("all", "youtube", "local")
SCOPE_MODES = ("legacy", "visible", "selected")
BEGIN_WRITE = "BEGIN IMMEDIATE"
SELECT_CONTROL = "SELECT * FROM control WHERE id=1"
SELECT_JOB = "SELECT * FROM jobs WHERE id=?"


def scope_kind(value):
    if value not in SCOPE_KINDS:
        raise ValueError("Zakres kolejki musi mieć wartość all, youtube albo local.")
    return value


def scope_ids(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError) as exc:
            raise ValueError("Zakres nagrań musi być listą identyfikatorów.") from exc
    if not isinstance(value, (list, tuple, set)) or any(type(item) is not int or item <= 0 for item in value):
        raise ValueError("Zakres nagrań musi zawierać dodatnie całkowite identyfikatory.")
    return sorted(set(value))


def scope_mode(value):
    if value not in SCOPE_MODES:
        raise ValueError("Tryb sesji musi mieć wartość legacy, visible albo selected.")
    return value


def ordered_ids(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError) as exc:
            raise ValueError("Kolejność sesji musi być listą identyfikatorów.") from exc
    if not isinstance(value, (list, tuple)) or any(type(item) is not int or item <= 0 for item in value):
        raise ValueError("Kolejność sesji musi zawierać dodatnie całkowite identyfikatory.")
    return list(dict.fromkeys(value))


def session_order(mode, ids, order):
    mode = scope_mode(mode)
    if mode == "legacy":
        if order is not None:
            raise ValueError("Kolejność sesji wymaga trybu visible albo selected.")
        return []
    order = ordered_ids(order)
    if not order or set(order) != set(ids):
        raise ValueError("Kolejność sesji musi obejmować dokładnie wskazane nagrania.")
    return order


def validate_selection(db, kind, ids):
    if not ids:
        return
    selected = db.execute("SELECT id,kind FROM jobs WHERE id IN (SELECT value FROM json_each(?))",
                          (json.dumps(ids),)).fetchall()
    if len(selected) != len(ids) or any(kind != "all" and row["kind"] != kind for row in selected):
        raise ValueError("Wybrane identyfikatory nie należą do wskazanego rodzaju kolejki.")


def scope_filter(kind, ids, *, enabled=False):
    """Build a parameterized predicate shared by claiming and scoped retries."""
    kind, ids = scope_kind(kind), scope_ids(ids)
    clauses, parameters = [], []
    if enabled:
        clauses.append("enabled=1")
    if kind != "all":
        clauses.append("kind=?")
        parameters.append(kind)
    if ids:
        clauses.append("id IN (" + ",".join("?" for _ in ids) + ")")
        parameters.extend(ids)
    return " AND ".join(clauses) or "1=1", parameters


def in_scope(job, control):
    ids = scope_ids(control["scope_ids"])
    return (control["scope_kind"] == "all" or job["kind"] == control["scope_kind"]) and (not ids or job["id"] in ids)


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def restored_output_state(job, metadata):
    if job["status"] != "output_required":
        return job["status"], job["error"], job["error_code"], job["stage"]
    error = metadata.get("probe_error", "")
    if not error:
        return "pending", "", "", ""
    code = metadata.get("probe_error_code") or "media_probe"
    status = "no_audio" if code == "no_audio" else "error"
    return status, error, code, "Walidacja pliku"


def resolved_output_history(job):
    """Keep the cleared current path diagnostic, without repeating old entries."""
    if job["status"] != "output_required" or not job["error"]:
        return job["error_history"]
    history = json.loads(job["error_history"])
    code = job["error_code"] or "output_path"
    if any(entry.get("code") == code and entry.get("message") == job["error"] for entry in history):
        return job["error_history"]
    history.append({"at": job["updated"], "code": code, "message": job["error"],
                    "processed_until": job["processed_until"]})
    return json.dumps(history, ensure_ascii=False)


def validate_output_change(job, folder):
    message = "Miejsce można zmienić tylko dla lokalnych nagrań bez rozpoczętego przetwarzania: " + job["title"]
    if job["kind"] != "local" or job["status"] in {"running", "done", "draft", "partial"}:
        raise ValueError(message)
    if any(job[field] > 0 for field in ("attempts", "processed_until", "progress")):
        raise ValueError(message)
    if folder.exists() and any(folder.iterdir()):
        raise ValueError(message)


class Store:
    def __init__(self, root, *, timeout=30):
        self.timeout = timeout
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "kolejka.sqlite3"
        with self.connect() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY, identity TEXT UNIQUE NOT NULL,
                    kind TEXT NOT NULL, title TEXT NOT NULL, source TEXT NOT NULL,
                    date TEXT DEFAULT '', visibility TEXT DEFAULT '',
                    duration REAL, source_meta TEXT DEFAULT '{}',
                    folder TEXT NOT NULL, output_root TEXT, status TEXT NOT NULL DEFAULT 'pending',
                    enabled INTEGER DEFAULT 1, language TEXT DEFAULT 'auto',
                    audio_track INTEGER DEFAULT 0, stage TEXT DEFAULT '',
                    progress REAL DEFAULT 0, error TEXT DEFAULT '',
                    attempts INTEGER DEFAULT 0, created TEXT NOT NULL, updated TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS control (
                    id INTEGER PRIMARY KEY CHECK(id=1), stop INTEGER DEFAULT 1,
                    state TEXT DEFAULT 'idle', pid INTEGER, heartbeat TEXT,
                    current_id INTEGER, message TEXT DEFAULT '',
                    scope_kind TEXT NOT NULL DEFAULT 'all', scope_ids TEXT NOT NULL DEFAULT '[]'
                    ,order_ids TEXT NOT NULL DEFAULT '[]', scope_mode TEXT NOT NULL DEFAULT 'legacy'
                );
                INSERT OR IGNORE INTO control(id) VALUES(1);
                CREATE TABLE IF NOT EXISTS imports (
                    id INTEGER PRIMARY KEY, path TEXT, sha256 TEXT, imported TEXT,
                    rows INTEGER, notes TEXT
                );
                CREATE TABLE IF NOT EXISTS local_cache (
                    path TEXT PRIMARY KEY, size INTEGER, mtime_ns INTEGER, sha256 TEXT
                );
            ''')
            # Serialize inspection and ALTER: two GUI processes can open an old
            # database at the same time without racing the same schema change.
            db.execute(BEGIN_WRITE)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(control)")}
            if "scope_kind" not in columns:
                db.execute("ALTER TABLE control ADD COLUMN scope_kind TEXT NOT NULL DEFAULT 'all'")
            if "scope_ids" not in columns:
                db.execute("ALTER TABLE control ADD COLUMN scope_ids TEXT NOT NULL DEFAULT '[]'")
            if "order_ids" not in columns:
                db.execute("ALTER TABLE control ADD COLUMN order_ids TEXT NOT NULL DEFAULT '[]'")
            if "scope_mode" not in columns:
                db.execute("ALTER TABLE control ADD COLUMN scope_mode TEXT NOT NULL DEFAULT 'legacy'")
            job_columns = {row["name"] for row in db.execute("PRAGMA table_info(jobs)")}
            if "output_root" not in job_columns:
                db.execute("ALTER TABLE jobs ADD COLUMN output_root TEXT")
            for name, definition in (("processed_until", "REAL NOT NULL DEFAULT 0"),
                                     ("error_code", "TEXT NOT NULL DEFAULT ''"),
                                     ("error_history", "TEXT NOT NULL DEFAULT '[]'")):
                if name not in job_columns:
                    db.execute("ALTER TABLE jobs ADD COLUMN " + name + " " + definition)
            db.execute("UPDATE jobs SET stage='Oczekuje na wznowienie' WHERE status='pending' AND stage='Wymaga działania'")

    @contextmanager
    def connect(self, *, timeout=None):
        connection = sqlite3.connect(self.path, timeout=self.timeout if timeout is None else timeout)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA synchronous=FULL")
            with connection:
                yield connection
        finally:
            connection.close()

    def snapshot(self, *, timeout=0.05):
        """Read the table and session from one bounded, read-only transaction."""
        connection = sqlite3.connect(self.path, timeout=timeout)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            rows = [dict(row) for row in connection.execute(
                "SELECT * FROM jobs ORDER BY CASE WHEN date='' THEN 1 ELSE 0 END,date,id")]
            control = dict(connection.execute(SELECT_CONTROL).fetchone())
            return rows, control
        finally:
            connection.close()

    def add_many(self, rows):
        count = 0
        with self.connect() as db:
            db.execute(BEGIN_WRITE)
            for row in rows:
                stamp = now()
                old = db.execute("SELECT source FROM jobs WHERE identity=?", (row["identity"],)).fetchone()
                if old is not None:
                    # The first import owns the destination and processing state.
                    # A deep duplicate must not need a new output path at all.
                    if row["kind"] == "local" and not Path(old["source"]).exists():
                        db.execute("UPDATE jobs SET source=?,updated=? WHERE identity=?",
                                   (row["source"], stamp, row["identity"]))
                    continue
                folder = slug(row["title"]) + "__" + row["identity"].replace(":", "-")
                if row.get("output_root"):
                    from .local_outputs import compact_folder
                    folder = compact_folder(row["title"], row["identity"], row["output_root"])
                cursor = db.execute('''INSERT OR IGNORE INTO jobs
                    (identity,kind,title,source,date,visibility,duration,source_meta,folder,output_root,
                     status,enabled,stage,error,error_code,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (row["identity"], row["kind"], row["title"], row["source"],
                     row.get("date", ""), row.get("visibility", ""), row.get("duration"),
                     json.dumps(row.get("meta", {}), ensure_ascii=False), folder, row.get("output_root"),
                     row.get("status", "pending"), int(row.get("status") not in {"draft", "skipped"}),
                     row.get("stage", ""), row.get("error", ""), row.get("error_code", ""), stamp, stamp))
                count += cursor.rowcount
        return count

    def jobs(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM jobs ORDER BY CASE WHEN date='' THEN 1 ELSE 0 END,date,id")]

    def get(self, job_id):
        with self.connect() as db:
            row = db.execute(SELECT_JOB, (job_id,)).fetchone()
            return dict(row) if row else None

    def update(self, job_id, **values):
        allowed = {"stage", "progress", "error", "status", "duration", "attempts", "processed_until", "error_code", "error_history"}
        if not values.keys() <= allowed:
            raise ValueError("Niepoprawne pola aktualizacji zadania.")
        values["updated"] = now()
        with self.connect() as db:
            db.execute("UPDATE jobs SET " + ",".join(f"{key}=?" for key in values) + " WHERE id=?",
                       (*values.values(), job_id))

    def control(self, **values):
        allowed = {"stop", "state", "pid", "heartbeat", "current_id", "message", "scope_kind", "scope_ids", "order_ids", "scope_mode"}
        if not values.keys() <= allowed:
            raise ValueError("Niepoprawne pola sterowania.")
        if "scope_kind" in values:
            values["scope_kind"] = scope_kind(values["scope_kind"])
        if "scope_ids" in values:
            values["scope_ids"] = json.dumps(scope_ids(values["scope_ids"]))
        if "order_ids" in values:
            values["order_ids"] = json.dumps(ordered_ids(values["order_ids"]))
        if "scope_mode" in values:
            values["scope_mode"] = scope_mode(values["scope_mode"])
        with self.connect() as db:
            if values:
                db.execute("UPDATE control SET " + ",".join(f"{key}=?" for key in values) + " WHERE id=1",
                           tuple(values.values()))
            return dict(db.execute(SELECT_CONTROL).fetchone())

    def claim(self, excluded=()):
        # Stop and claiming the next film are serialized in the same database.
        with self.connect() as db:
            db.execute(BEGIN_WRITE)
            control = db.execute(SELECT_CONTROL).fetchone()
            if control["stop"]:
                return None
            snapshot = control["scope_mode"] != "legacy"
            predicate, parameters = scope_filter(control["scope_kind"], [] if snapshot else control["scope_ids"], enabled=True)
            if excluded:
                predicate += " AND jobs.id NOT IN (SELECT value FROM json_each(?))"
                parameters.append(json.dumps(sorted(excluded)))
            joined = " JOIN json_each(?) AS sequence ON jobs.id=sequence.value" if snapshot else ""
            order = "CAST(sequence.key AS INTEGER)" if snapshot else "CASE WHEN date='' THEN 1 ELSE 0 END,date,jobs.id"
            if snapshot:
                parameters.insert(0, control["order_ids"])
            row = db.execute("SELECT jobs.* FROM jobs" + joined + " WHERE status='pending' AND " + predicate +
                " ORDER BY " + order + " LIMIT 1", parameters).fetchone()
            if not row:
                return None
            db.execute("UPDATE jobs SET status='running',updated=? WHERE id=?", (now(), row["id"]))
            db.execute("UPDATE control SET current_id=? WHERE id=1", (row["id"],))
            return dict(row)

    def recover(self):
        # Call only with the operating-system worker lock held.
        with self.connect() as db:
            control = db.execute(SELECT_CONTROL).fetchone()
            predicate, parameters = scope_filter(control["scope_kind"], control["scope_ids"])
            db.execute("UPDATE jobs SET status='pending',stage='Wznawianie' WHERE status='running' AND " + predicate,
                       parameters)

    def start(self, kind="all", job_ids=None, *, order_ids=None, scope_mode="legacy"):
        kind = scope_kind(kind)
        ids = [] if job_ids is None else scope_ids(job_ids)
        if job_ids is not None and not ids:
            raise ValueError("Zaznacz przynajmniej jedno nagranie do uruchomienia.")
        order = session_order(scope_mode, ids, order_ids)
        predicate, parameters = scope_filter(kind, ids, enabled=True)
        with self.connect() as db:
            db.execute(BEGIN_WRITE)
            validate_selection(db, kind, ids)
            db.execute("UPDATE jobs SET status='pending',stage='Oczekuje na wznowienie' WHERE (status IN ('blocked','partial') OR "
                       "(status='output_required' AND error_code='output_path') OR "
                       "(status='error' AND stage='Wymaga działania')) AND " + predicate, parameters)
            db.execute("UPDATE control SET stop=0,message='',scope_kind=?,scope_ids=?,order_ids=?,scope_mode=? WHERE id=1",
                       (kind, json.dumps(ids), json.dumps(order), scope_mode))

    def retry(self, ids=None):
        with self.connect() as db:
            query = "UPDATE jobs SET status='pending',stage='Oczekuje na wznowienie',updated=? WHERE status IN (" + ",".join("?" for _ in RETRYABLE) + ")"
            args = [now(), *RETRYABLE]
            if ids:
                query += " AND id IN (" + ",".join("?" for _ in ids) + ")"
                args.extend(ids)
            return db.execute(query, args).rowcount

    def record_failure(self, job_id, message, code, *, partial=False, processed_until=0):
        job = self.get(job_id)
        history = json.loads(job["error_history"])
        if job["error"] and not history:
            history.append({"at": job["updated"], "message": job["error"], "code": job["error_code"] or "legacy_error",
                            "processed_until": job["processed_until"]})
        history.append({"at": now(), "message": message, "code": code, "processed_until": processed_until})
        self.update(job_id, status="partial" if partial else "error", error=message, error_code=code,
                    processed_until=max(job["processed_until"], processed_until),
                    stage="Częściowo gotowe" if partial else "Wymaga działania",
                    error_history=json.dumps(history, ensure_ascii=False))

    def configure(self, ids, **values):
        if not values.keys() <= {"enabled", "language", "audio_track"}:
            raise ValueError("Niepoprawne ustawienie zadania.")
        if not ids:
            return
        with self.connect() as db:
            if "audio_track" in values:
                db.execute("UPDATE jobs SET processed_until=0,progress=0,status='pending',stage='Oczekuje na wznowienie' "
                           "WHERE audio_track<>? AND status NOT IN ('running','done','draft','skipped','output_required','no_audio') AND id IN (" +
                           ",".join("?" for _ in ids) + ")", (values["audio_track"], *ids))
            db.execute("UPDATE jobs SET " + ",".join(f"{key}=?" for key in values) +
                       " WHERE status NOT IN ('running','done','draft','skipped') AND id IN (" +
                       ",".join("?" for _ in ids) + ")", (*values.values(), *ids))

    def skip(self, job_id, message, code):
        """Archive an unprocessable source without inventing a transcription."""
        with self.connect() as db:
            db.execute(BEGIN_WRITE)
            job = db.execute(SELECT_JOB, (job_id,)).fetchone()
            if job is None:
                return
            metadata = json.loads(job["source_meta"])
            if job["status"] == "done" and not (code == "technical_media" and metadata.get("technical_exclusion")):
                return
            metadata["skip_reason"] = {"message": message, "code": code, "at": now(), "previous_status": job["status"]}
            history = json.loads(job["error_history"])
            history.append({"at": now(), "message": message, "code": code,
                            "processed_until": job["processed_until"]})
            db.execute("UPDATE jobs SET status='skipped',enabled=0,stage='Zakończono: pominięte',"
                       "error=?,error_code=?,source_meta=?,error_history=?,updated=? WHERE id=?",
                       (message, code, json.dumps(metadata, ensure_ascii=False),
                        json.dumps(history, ensure_ascii=False), now(), job_id))

    def export_csv(self, path, kind=None):
        path = output_file(path, suffix=".csv")
        if kind is not None:
            kind = scope_kind(kind)
        rows = self.jobs()
        if kind is not None and kind != "all":
            rows = [row for row in rows if row["kind"] == kind]
        with path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream, delimiter=";")
            writer.writerow(["ID", "Tytuł", "Data", "Typ", "Status", "Etap", "Postęp %", "Błąd", "Źródło", "Wyniki",
                             "Czas trwania (s)", "Opracowano do (s)", "Kod błędu"])
            for job in rows:
                # Prevent spreadsheet formulas from imported titles / error text.
                def safe(value):
                    value = str(value)
                    return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value
                writer.writerow(map(safe, [job["identity"], job["title"], job["date"], job["kind"],
                    STATUSES[job["status"]], job["stage"], round(job["progress"], 1), job["error"],
                    job["source"], str(job_folder(self.root, job)), job["duration"] if job["duration"] is not None else "",
                    job["processed_until"], job["error_code"]]))

    def prepare_output_change(self, job, mode, custom):
        from .local_outputs import compact_folder, output_root_for
        old = job_folder(self.root, job)
        validate_output_change(job, old)
        base = output_root_for(job["source"], self.root, mode, custom)
        folder = compact_folder(job["title"], job["identity"], base)
        metadata = json.loads(job["source_meta"])
        metadata.pop("output_error", None)
        metadata.pop("intended_output_root", None)
        status, error, code, stage = restored_output_state(job, metadata)
        history = resolved_output_history(job)
        return (str(base), folder, json.dumps(metadata, ensure_ascii=False),
                status, error, code, stage, history, now(), job["id"])

    def change_output(self, ids, mode, custom=""):
        if not ids:
            raise ValueError("Zaznacz nagrania, dla których chcesz zmienić miejsce wyników.")
        with self.connect() as db:
            db.execute(BEGIN_WRITE)
            control = db.execute(SELECT_CONTROL).fetchone()
            if control["state"] == "running" or not control["stop"]:
                raise ValueError("Najpierw dokończ bieżące nagranie i zatrzymaj sesję.")
            selected = []
            for ident in ids:
                row = db.execute(SELECT_JOB, (ident,)).fetchone()
                if row is None:
                    raise ValueError("Nie znaleziono wybranego nagrania.")
                selected.append(self.prepare_output_change(dict(row), mode, custom))
            db.executemany("UPDATE jobs SET output_root=?,folder=?,source_meta=?,status=?,error=?,error_code=?,stage=?,error_history=?,updated=? WHERE id=?", selected)
        return len(selected)
