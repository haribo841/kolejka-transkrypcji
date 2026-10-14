"""Import diagnostics with separate counts for file paths and queue jobs.

The old ``files``, ``added``, ``errors`` and ``skipped`` values are retained.
They are not interchangeable: files are paths, added values are unique jobs,
and legacy errors can count several copies of the same recording.
"""
from __future__ import annotations

from collections import Counter
import json


PROBLEM_NAMES = {
    "output_path": "Zbyt długa ścieżka wyników",
    "source_changed": "Plik zmienił się podczas odczytu",
    "file_access": "Problem dostępu do źródła",
    "no_audio": "Brak ścieżki audio",
    "unsupported_codec": "Nierozpoznany kodek audio",
    "media_probe": "Problem sprawdzenia FFprobe",
    "other": "Inny problem",
}
STATUS_NAMES = {
    "pending": "Oczekuje", "running": "Przetwarzanie", "done": "Gotowe",
    "partial": "Częściowo gotowe", "error": "Błąd",
    "no_audio": "Bez ścieżki audio", "output_required": "Wybierz miejsce wyników",
}
TECHNICAL_CODES = {"technical_media", "technical_exclusion"}
PROBLEM_STATUSES = {"error", "partial", "no_audio", "output_required"}


def diagnose(message):
    """Return a stable cause code for an exception or an existing message."""
    for name in ("code", "error_code"):
        explicit = getattr(message, name, None)
        if isinstance(explicit, str) and explicit:
            return explicit
    text = str(message or "").casefold()
    if ("ścieżka wyników" in text and "długa" in text) or (
            "output" in text and "too long" in text):
        return "output_path"
    if any(part in text for part in ("plik zmienił się", "file changed", "changed during import")):
        return "source_changed"
    if any(part in text for part in ("nie zawiera ścieżki audio", "no audio stream",
                                     "no audio track", "does not contain audio")):
        return "no_audio"
    if any(part in text for part in ("nie rozpoznaje kodeka audio", "unknown audio codec",
                                     "unsupported audio codec")):
        return "unsupported_codec"
    if isinstance(message, OSError):
        return "file_access"
    if any(part in text for part in ("ffprobe", "media probe", "timed out")):
        return "media_probe"
    if any(part in text for part in ("permission denied", "access is denied", "cannot be accessed",
                                     "no such file", "odmowa dostępu", "brak dostępu", "winerror")):
        return "file_access"
    return "other"


def issue(path, phase, message, *, code=None, identity=None, job_id=None, kind=None):
    """Build a JSON-safe diagnosis; a single path can have several causes."""
    result = {"path": str(path or ""), "phase": str(phase),
              "code": code or diagnose(message), "message": str(message)}
    if identity is not None:
        result["identity"] = str(identity)
    if job_id is not None:
        result["job_id"] = int(job_id)
    if kind is not None:
        result["kind"] = str(kind)
    return result


def _technical(job):
    metadata = job.get("source_meta", {})
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (TypeError, ValueError):
            metadata = {}
    return (isinstance(metadata, dict) and bool(metadata.get("technical_exclusion"))
            and not job.get("enabled", True))


def _ready(job):
    return bool(job.get("enabled", True) and (
        job["status"] in {"pending", "partial", "running"} or
        (job["status"] == "error" and job.get("stage") == "Wymaga działania")))


def _path_counts(issues):
    files, scans, technical_files, technical_directories = set(), set(), set(), set()
    for item in issues:
        code = item.get("code") or diagnose(item.get("message", ""))
        path = str(item.get("path", ""))
        if code in TECHNICAL_CODES:
            if item.get("kind") == "file":
                technical_files.add(path)
            elif item.get("kind") == "directory":
                technical_directories.add(path)
        elif item.get("phase") == "scan":
            scans.add((code, path))
        else:
            files.add((code, path))
    return files, scans, technical_files, technical_directories


def finalize_stats(result, issues=(), jobs=(), *, found=None):
    """Add statistics without changing the meaning of the legacy counters.

    ``jobs`` are final canonical rows matched by this import, not the whole
    queue. File counts deduplicate (cause, path); job counts deduplicate IDs.
    Scan problems are separately counted paths, which can be directories.
    """
    if found is not None:
        result["job_ids"] = list(found)
        result["job_statuses"] = list(found.values())
    rows = {job["id"]: dict(job) for job in jobs}
    if "job_ids" not in result:
        result["job_ids"] = list(rows)
    if "job_statuses" not in result:
        result["job_statuses"] = [rows[ident]["status"] for ident in result["job_ids"] if ident in rows]
    matched = set(result["job_ids"])
    rows = {ident: row for ident, row in rows.items() if ident in matched}
    problems = list(issues)
    files, scans, technical_files, technical_directories = _path_counts(problems)
    job_counts = Counter()
    technical_jobs, ready_jobs = 0, 0
    for job in rows.values():
        if _technical(job):
            technical_jobs += 1
            continue
        ready_jobs += _ready(job)
        if job["status"] in PROBLEM_STATUSES:
            code = job.get("error_code") or diagnose(job.get("error", ""))
            if job["status"] != "partial" or job.get("error") or job.get("error_code"):
                job_counts[code] += 1
    result.update({
        "report_version": 2, "issues": problems,
        "matched_jobs": len(matched),
        "existing_jobs": max(0, len(matched) - result.get("added", 0)),
        "duplicate_paths": max(0, result.get("files", 0) - result.get("skipped", 0) - len(matched)),
        "file_problem_counts": dict(Counter(code for code, _ in files)),
        "job_problem_counts": dict(job_counts),
        "scan_problem_counts": dict(Counter(code for code, _ in scans)),
        "problem_files": len({path for _, path in files}),
        "problem_jobs": sum(job_counts.values()),
        "technical_files": len(technical_files), "technical_directories": len(technical_directories),
        "technical_jobs": technical_jobs,
        "ready_jobs": ready_jobs,
    })
    return result


def _legacy_stats(result):
    # Old warnings mixed scanning with import skips. Only a specific message
    # establishes an output-path or changed-file cause; access stage is unknown.
    issues = []
    for warning in result.get("scan_warnings", []):
        code = warning.get("code") or diagnose(warning.get("message", ""))
        phase = warning.get("phase") or ("register" if code in {"output_path", "source_changed"} else "scan")
        issues.append(issue(warning.get("path", ""), phase, warning.get("message", ""), code=code))
    return finalize_stats(dict(result), issues)


def _breakdown(stats):
    file_counts, job_counts = stats["file_problem_counts"], stats["job_problem_counts"]
    parts = []
    for code in sorted(file_counts.keys() | job_counts.keys()):
        counts = []
        if job_counts.get(code):
            counts.append(f"{job_counts[code]} zadań")
        if file_counts.get(code):
            counts.append(f"{file_counts[code]} ścieżek")
        if counts:
            parts.append(f"{PROBLEM_NAMES.get(code, 'Inny problem (' + code + ')')}: " + " / ".join(counts))
    return ", ".join(parts)


def _actions(stats):
    counts = stats["file_problem_counts"].keys() | stats["job_problem_counts"].keys()
    actions = []
    if "output_path" in counts:
        actions.append('wybierz krótki "Folder centralny" i "Zmień miejsce wyników" '
                       "(dla pominiętych plików ze starszego importu ponów import)")
    if "no_audio" in counts:
        actions.append('brak audio wymaga źródła z dźwiękiem; "Ponów nieudane" nie doda brakującego audio')
    if counts & {"media_probe", "unsupported_codec"}:
        actions.append("FFprobe/kodek: sprawdź lub przekonwertuj źródło")
    if "file_access" in counts or "file_access" in stats["scan_problem_counts"]:
        actions.append("przywróć dostęp do źródła i ponów import")
    if "source_changed" in counts:
        actions.append("zakończ zapis zmieniającego się pliku i ponów import")
    return "; ".join(actions)


def import_note(result, folder_scope=None):
    """Format an import result, including reports saved before version 2."""
    stats = result if result.get("report_version", 0) >= 2 else _legacy_stats(result)
    files, added, skipped = (stats.get(name, 0) for name in ("files", "added", "skipped"))
    copies = max(0, files - added - skipped)
    parts = [f"Znaleziono: {files} | Dodano: {added} (ścieżki plików / nowe unikalne zadania); "
             f"Kopie lub już w kolejce: {copies} ścieżek; Pominięte pliki: {skipped}; "
             f"już w kolejce: {stats['existing_jobs']}; kopie plików w imporcie: {stats['duplicate_paths']}."]
    if not files and folder_scope:
        parts.append(f"Nie znaleziono nagrań; skanowanie: {folder_scope}; Sprawdź folder oraz opcje Tylko M4A "
                     "i Podfoldery; własne katalogi wyników są pomijane.")
    statuses = Counter(stats.get("job_statuses", []))
    details = []
    if statuses:
        summary = ", ".join(f"{STATUS_NAMES.get(status, status)}: {count}"
                            for status, count in sorted(statuses.items()))
        details.append(f"Zaznaczone pozycje: {summary} (unikalne zadania)")
    causes = _breakdown(stats)
    if causes:
        details.append("Przyczyny: " + causes)
    if stats["scan_problem_counts"]:
        details.append(f"Ostrzeżenia skanowania (pliki lub foldery): {sum(stats['scan_problem_counts'].values())}")
    if stats.get("technical_files") or stats.get("technical_directories") or stats.get("technical_jobs"):
        details.append(f"Materiały techniczne: {stats.get('technical_files', 0)} plików, "
                       f"{stats.get('technical_directories', 0)} folderów, "
                       f"{stats.get('technical_jobs', 0)} wyłączonych zadań")
    if details:
        parts.append("; ".join(details) + ".")
    actions = _actions(stats)
    if actions:
        parts.append("Działanie: " + actions + ".")
    parts.append("Ponowny import nie resetuje postępu; Start pozostaje ręczny; szczegóły w raporcie JSON.")
    return " ".join(parts)
