"""Stable destinations for local jobs and scans that exclude our own outputs."""
from __future__ import annotations

import os
from pathlib import Path
import stat

from .common import ResourceError, read_json
from .media_discovery import technical_directory, technical_source
from .path_budget import PATH_LIMIT, fit_slug, path_units
from .paths import local_path

OUTPUT_MODES = {"beside": "Obok źródła", "central": "Folder centralny", "custom": "Wskazany folder"}
OWNER_FILE = ".kolejka-nagranie.json"
# Microsoft name-surrogate tags cover junctions and Linux/WSL links too.
NAME_SURROGATE = 0x20000000


def output_root_for(source, registry, mode, custom=""):
    if mode == "beside":
        return local_path(source).parent
    if mode == "central":
        return local_path(registry) / "wyniki"
    if mode == "custom" and str(custom).strip():
        return local_path(custom)
    raise ValueError("Wybierz miejsce wyników; dla własnego folderu podaj ścieżkę.")


def compact_folder(title, identity, root):
    from .result_paths import minimum_export_leaf_units
    # Budget actual export names and native intermediate paths together.
    key = identity.partition(":")[2][:12]
    suffix = "__" + key
    reserve = max(64, minimum_export_leaf_units(identity))
    budget = min(32, PATH_LIMIT - path_units(root) - 2 - reserve - path_units(suffix))
    if budget < 1:
        raise ResourceError("Ścieżka wyników jest zbyt długa. Wybierz krótszy folder wyników.")
    return fit_slug(title, budget) + suffix


def owned_folder(path):
    if (path / OWNER_FILE).is_file():
        return True
    for name in ("gotowe.json", "czesciowe.json", "transkrypcja.json"):
        try:
            marker = path / name
            if not marker.is_file():
                continue
            data = read_json(marker, {})
            if isinstance(data, dict) and str(data.get("identity", "")).startswith(("local:", "yt:")):
                return True
        except (OSError, ValueError):
            continue
    return False


class ScanResult(list):
    def __init__(self):
        super().__init__()
        self.warnings = []
        self.exclusions = []
        self.directories = 0

    def failed(self, error, path=None):
        self.warnings.append({"path": str(path or getattr(error, "filename", "") or ""),
                              "message": str(error), "phase": "scan", "code": "file_access"})

    def exclude(self, path, reason, kind):
        self.exclusions.append({"path": str(path), "message": reason, "phase": "scan",
                                "code": "technical_media", "kind": kind})


def linked_path(path):
    metadata = path.lstat()
    return stat.S_ISLNK(metadata.st_mode) or bool(getattr(metadata, "st_reparse_tag", 0) & NAME_SURROGATE)


def scan_directory(path, excluded, result, include_technical=False):
    try:
        if path in excluded or linked_path(path) or owned_folder(path):
            return False
        reason = None if include_technical else technical_directory(path)
        if reason:
            result.exclude(path, reason, "directory")
            return False
        return True
    except OSError as error:
        result.failed(error, path)
        return False


def scan_file(path, result, include_technical=False):
    try:
        if linked_path(path) or not path.is_file():
            return False
        reason = technical_source(path) if path.suffix.casefold() == ".ts" and not include_technical else None
        if reason:
            result.exclude(path, reason, "file")
            return False
        return True
    except OSError as error:
        result.failed(error, path)
        return False


def scan_media(folder, extensions, recursive=True, excluded=(), *, progress=lambda message: None,
               include_technical=False):
    if linked_path(Path(folder).expanduser()):
        raise ValueError("Wybierz rzeczywisty folder z nagraniami zamiast dowiązania.")
    path = local_path(folder, exists=True)
    if not path.is_dir():
        raise ValueError("Wskaż folder z nagraniami.")
    excluded = {local_path(p) for p in excluded}
    result = ScanResult()
    for current, dirs, files in os.walk(path, followlinks=False, onerror=result.failed):
        result.directories += 1
        if result.directories % 500 == 0:
            progress(f"Wyszukiwanie: sprawdzono {result.directories} katalogów, znaleziono {len(result)} nagrań.")
        current = Path(current)
        if not scan_directory(current, excluded, result, include_technical):
            dirs[:] = []
            continue
        dirs[:] = [name for name in dirs if scan_directory(current / name, excluded, result, include_technical)] if recursive else []
        result.extend(current / name for name in files if Path(name).suffix.casefold() in extensions
                      and scan_file(current / name, result, include_technical))
    result.sort()
    progress(f"Wyszukiwanie zakończone: {result.directories} katalogów, {len(result)} nagrań.")
    return result
