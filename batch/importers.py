from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
from subprocess import TimeoutExpired
from urllib.parse import parse_qs, urlparse

from .common import ResourceError, digest, job_folder
from .import_report import diagnose, finalize_stats, issue
from .store import now

MEDIA = {".wav", ".mp3", ".mp4", ".mkv", ".webm", ".m4a", ".aac", ".flac",
         ".opus", ".ogg", ".aiff", ".aif", ".wma", ".wmv", ".mov", ".avi",
         ".m4v", ".mts", ".m2ts", ".ts", ".mpeg", ".mpg", ".3gp", ".mka", ".oga", ".weba", ".flv", ".mxf", ".vob"}


def video_id(url, supplied=""):
    supplied = str(supplied or "").strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", supplied):
        return supplied
    parsed = urlparse(str(url or ""))
    host = (parsed.hostname or "").lower()
    candidate = ""
    if host == "youtu.be":
        candidate = parsed.path.strip("/").split("/")[0]
    elif host in {"youtube.com", "www.youtube.com", "m.youtube.com", "studio.youtube.com"}:
        candidate = parse_qs(parsed.query).get("v", [""])[0]
        match = re.search(r"/(?:shorts|live|embed|video)/([A-Za-z0-9_-]{11})(?:/|$)", parsed.path)
        if match:
            candidate = match[1]
    return candidate if re.fullmatch(r"[A-Za-z0-9_-]{11}", candidate) else ""


def duration_seconds(value):
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, time):
        return value.hour * 3600 + value.minute * 60 + value.second
    if isinstance(value, (int, float)):
        return float(value) * 86400
    if value:
        try:
            result = 0
            for part in str(value).split(":"):
                result = result * 60 + float(part)
            return result
        except ValueError:
            pass
    return None


def catalog_header(sheet):
    for row in sheet.iter_rows(min_row=1, max_row=min(100, sheet.max_row)):
        names = {str(cell.value).strip().casefold(): i for i, cell in enumerate(row) if cell.value is not None}
        if {"tytuł", "link", "id filmu"} <= names.keys():
            return names, row[0].row
    raise ValueError("Nie znaleziono nagłówków Tytuł, Link i ID filmu.")


def catalog_value(row, header, name, default=""):
    index = header.get(name.casefold())
    if index is None or row[index].value is None:
        return default
    return row[index].value


def catalog_entry(row, header, source, epoch):
    from openpyxl.utils.datetime import from_excel
    title = str(catalog_value(row, header, "Tytuł")).strip()
    if not title:
        return None
    cell = row[header["link"]]
    link = cell.hyperlink.target if cell.hyperlink and cell.hyperlink.target else catalog_value(row, header, "Link")
    ident = video_id(link, catalog_value(row, header, "ID filmu"))
    date_value = catalog_value(row, header, "Data")
    if isinstance(date_value, (int, float)):
        date_value = from_excel(date_value, epoch)
    date_text = date_value.isoformat() if isinstance(date_value, (datetime, date)) else str(date_value)
    meta = {"catalog": str(source), "row": row[0].row, "ordinal": catalog_value(row, header, "Lp."),
            "kind": str(catalog_value(row, header, "Typ")), "source_status": str(catalog_value(row, header, "Status")),
            "date_meaning": str(catalog_value(row, header, "Znaczenie daty"))}
    if ident:
        identity, status = "yt:" + ident, "pending"
    else:
        key = hashlib.sha256(f"{source.name}|{meta['ordinal']}|{title}".encode()).hexdigest()[:24]
        identity, status = "draft:" + key, "draft"
    return {"identity": identity, "kind": "youtube", "title": title,
            "source": "https://www.youtube.com/watch?v=" + ident if ident else "",
            "date": date_text, "visibility": str(catalog_value(row, header, "Widoczność")),
            "duration": duration_seconds(catalog_value(row, header, "Długość", None)), "meta": meta,
            "status": status}


def import_xlsx(store, source):
    from openpyxl import load_workbook

    source = Path(source).resolve(strict=True)
    before = digest(source)
    # Read-only source file. It is never saved, renamed or modified.
    workbook = load_workbook(source, read_only=False, data_only=True)
    try:
        if "Materiały" not in workbook.sheetnames:
            raise ValueError("Brak arkusza Materiały.")
        sheet = workbook["Materiały"]
        header, header_row = catalog_header(sheet)
        result = []
        drafts = 0
        for row in sheet.iter_rows(min_row=header_row + 1):
            entry = catalog_entry(row, header, source, workbook.epoch)
            if entry is not None:
                result.append(entry)
                drafts += entry["status"] == "draft"
    finally:
        workbook.close()
    if digest(source) != before:
        raise RuntimeError("Arkusz zmienił się podczas odczytu. Zamknij edycję i ponów import.")
    inserted = store.add_many(result)
    note = f"{len(result)} wierszy, {drafts} bez poprawnego ID/linku."
    with store.connect() as db:
        db.execute("INSERT INTO imports(path,sha256,imported,rows,notes) VALUES(?,?,?,?,?)",
                   (str(source), before, now(), len(result), note))
    return {"rows": len(result), "added": inserted, "drafts": drafts, "sha256": before, "note": note}


def local_checksum(store, path):
    stat = path.stat()
    with store.connect() as db:
        cached = db.execute("SELECT * FROM local_cache WHERE path=?", (str(path),)).fetchone()
    if cached and cached["size"] == stat.st_size and cached["mtime_ns"] == stat.st_mtime_ns:
        return stat, cached["sha256"]
    checksum = digest(path)
    after = path.stat()
    if (stat.st_size, stat.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError(f"Plik zmienił się podczas importu: {path}")
    with store.connect() as db:
        db.execute("INSERT OR REPLACE INTO local_cache VALUES(?,?,?,?)",
                   (str(path), stat.st_size, stat.st_mtime_ns, checksum))
    return stat, checksum


def local_entry(store, path, config, output_mode, output_root):
    from .local_outputs import output_root_for
    from .media import media_metadata
    stat, checksum = local_checksum(store, path)
    metadata = {"sha256": checksum, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    error, duration = "", None
    if config is not None:
        try:
            media = media_metadata(path, config)
            metadata.update(media)
            duration = media["duration"]
        except (ValueError, OSError, RuntimeError, TimeoutExpired) as exc:
            error = str(exc)
            metadata["probe_error"] = error
            metadata["probe_error_code"] = diagnose(exc)
    base = str(output_root_for(path, store.root, output_mode, output_root)) if output_mode else None
    status = "error" if error else "pending"
    from .outcomes import permanent_audio_failure
    if error and permanent_audio_failure(error, metadata["probe_error_code"]):
        status = "skipped"
        metadata["skip_reason"] = {"code": metadata["probe_error_code"], "message": error}
    if metadata.get("probe_error_code") == "no_audio":
        status = "skipped"
    stage = "Walidacja pliku" if error else ""
    if status == "skipped":
        stage = "Zakończono: pominięte"
    entry = {"identity": "local:" + checksum, "kind": "local", "source": str(path), "title": path.stem,
            "date": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
            "duration": duration, "meta": metadata, "output_root": base,
            "status": status,
            "error_code": metadata.get("probe_error_code", ""), "error": error,
            "stage": stage}
    return validate_local_output(store, entry)


def validate_local_output(store, entry):
    from .local_outputs import compact_folder
    if not entry["output_root"]:
        return entry
    with store.connect() as db:
        existing = db.execute("SELECT id FROM jobs WHERE identity=?", (entry["identity"],)).fetchone()
    if existing:
        return entry
    try:
        compact_folder(entry["title"], entry["identity"], entry["output_root"])
    except ResourceError:
        from .output_fallback import fallback_destination, fallback_metadata
        root, name = fallback_destination(store.root, entry["title"], entry["identity"])
        entry["meta"] = fallback_metadata(entry["meta"], entry["output_root"], root / name)
        entry["output_root"] = str(root)
    return entry


def collect_media_file(path, files, warnings, include_technical):
    from .media_discovery import technical_source
    reason = None if include_technical else technical_source(path)
    if reason:
        warnings.append(issue(path, "scan", reason, code="technical_media", kind="file"))
    else:
        files.add(path)


def collect_local_files(store, paths, allow_unknown, progress, include_technical=False):
    from .local_outputs import linked_path, scan_media
    files, warnings = set(), list(getattr(paths, "warnings", []))
    warnings.extend(getattr(paths, "exclusions", []))
    excluded = [job_folder(store.root, job) for job in store.jobs()]
    for source in paths:
        try:
            if linked_path(Path(source)):
                continue
            path = Path(source).resolve(strict=True)
            if path.is_dir():
                scanned = scan_media(path, MEDIA, excluded=excluded, progress=progress, include_technical=include_technical)
                files.update(scanned)
                warnings.extend(scanned.warnings)
                warnings.extend(scanned.exclusions)
            elif path.suffix.lower() in MEDIA or allow_unknown:
                collect_media_file(path, files, warnings, include_technical)
        except (OSError, ValueError) as error:
            warnings.append(issue(source, "scan", error))
    return files, warnings


def import_local(store, paths, progress=lambda message: None, *, config=None, output_mode=None, output_root="", include_technical=False):
    files, warnings = collect_local_files(store, paths, config is not None, progress, include_technical)
    count, errors, skipped, found = 0, 0, 0, {}
    issues, jobs = list(warnings), {}
    for index, path in enumerate(sorted(files), 1):
        progress(f"Identyfikacja plików: {index}/{len(files)}: {path.name}")
        try:
            entry = local_entry(store, path, config, output_mode, output_root)
            count += store.add_many([entry])
        except (OSError, ValueError, ResourceError) as error:
            problem = issue(path, "register", error)
            warnings.append(problem)
            issues.append(problem)
            skipped += 1
            errors += 1
            continue
        errors += bool(entry["error"]) and entry["status"] != "skipped"
        with store.connect() as db:
            for job in db.execute("SELECT * FROM jobs WHERE identity=?", (entry["identity"],)):
                found[job["id"]] = job["status"]
                jobs[job["id"]] = dict(job)
                if entry["meta"].get("probe_error") and entry["status"] != "skipped":
                    issues.append(issue(path, "probe", entry["meta"]["probe_error"], code=entry["meta"]["probe_error_code"],
                                        identity=entry["identity"], job_id=job["id"]))
                if entry["meta"].get("output_error"):
                    issues.append(issue(path, "register", entry["meta"]["output_error"], code="output_path",
                                        identity=entry["identity"], job_id=job["id"]))
    result = {"files": len(files), "added": count, "errors": errors, "skipped": skipped,
              "scan_warnings": [item for item in warnings if item.get("code") != "technical_media"]}
    return finalize_stats(result, issues, jobs.values(), found=found)


def enrich_local(store, jobs, config):
    from .media import media_metadata
    for job in jobs:
        try:
            metadata = media_metadata(job["source"], config)
            error = ""
        except Exception as exc:
            metadata, error = {"probe_error": str(exc), "probe_error_code": diagnose(exc)}, str(exc)
        previous = json.loads(job["source_meta"])
        previous.update(metadata)
        with store.connect() as db:
            db.execute("UPDATE jobs SET duration=?,source_meta=?,updated=? WHERE id=? AND status='pending'",
                       (metadata.get("duration"), json.dumps(previous, ensure_ascii=False), now(), job["id"]))
            if error:
                status = "no_audio" if metadata["probe_error_code"] == "no_audio" else "error"
                db.execute("UPDATE jobs SET status=?,stage='Walidacja pliku',error=?,error_code=? "
                           "WHERE id=? AND status='pending'", (status, error, metadata["probe_error_code"], job["id"]))
        if error:
            from .outcomes import permanent_audio_failure
            if permanent_audio_failure(error, metadata["probe_error_code"]):
                store.skip(job["id"], error, metadata["probe_error_code"])
