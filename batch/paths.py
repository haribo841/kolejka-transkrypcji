"""Validate selected local paths and contain generated files in their owner folder."""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil

RESERVED_WINDOWS_NAMES = {"con", "nul", "prn", "aux", "conin$", "conout$"} | {
    prefix + digit for prefix in ("com", "lpt") for digit in "123456789¹²³"}


def reserved_component(part):
    return (part.endswith((" ", ".")) or ":" in part or
            part.partition(".")[0].casefold() in RESERVED_WINDOWS_NAMES)

def local_path(value, *, exists=False):
    text = os.fspath(value)
    if not isinstance(text, str) or not text.strip() or any(char in text for char in ("\x00", "\r", "\n")):
        raise ValueError("Wskaż poprawną lokalną ścieżkę.")
    if text.startswith(("\\\\.\\", "\\\\?\\")):
        raise ValueError("Ścieżki urządzeń Windows nie są obsługiwane.")
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", text) and not re.match(r"^[A-Za-z]:[\\/]", text):
        raise ValueError("Wskaż ścieżkę lokalną, nie URI ani ciąg połączenia.")
    if ".." in Path(text).parts:
        raise ValueError("Ścieżka nie może zawierać przejścia do katalogu nadrzędnego.")
    path = Path(text).expanduser().resolve(strict=exists)
    if os.name == "nt" and any(reserved_component(part) for part in path.parts[1:]):
        raise ValueError("Ścieżka zawiera nazwę urządzenia lub alternatywnego strumienia Windows.")
    return path


def input_file(value):
    path = local_path(value, exists=True)
    if not path.is_file():
        raise ValueError("Wejście musi być lokalnym plikiem.")
    return path


def output_file(value, *, suffix=None):
    raw = Path(os.fspath(value))
    if raw.is_symlink():
        raise ValueError("Odmowa zapisu przez dowiązanie pliku.")
    path = local_path(value)
    if not path.name or path.is_dir():
        raise ValueError("Wskaż plik wynikowy, nie katalog.")
    if suffix is not None and path.suffix.casefold() != suffix:
        raise ValueError(f"Plik wynikowy musi mieć rozszerzenie {suffix}.")
    return path


def within_root(value, root):
    root = local_path(root)
    path = local_path(value)
    if path == root or not path.is_relative_to(root):
        raise ValueError("Ścieżka wychodzi poza katalog należący do nagrania.")
    return path


def child_path(root, name):
    relative = Path(os.fspath(name))
    if relative.is_absolute() or not relative.parts or ".." in relative.parts or relative.drive:
        raise ValueError("Niepoprawna względna ścieżka pliku roboczego.")
    return within_root(Path(root) / relative, root)


def media_tool(value, name):
    candidate = os.fspath(value)
    found = shutil.which(candidate) if Path(candidate).name == candidate else candidate
    if not found:
        raise FileNotFoundError(f"Brak programu {name}.")
    executable = input_file(found)
    if executable.name.casefold() not in {name, name + ".exe"}:
        raise ValueError(f"Wybierz program {name}, nie skrypt powłoki ani inny program.")
    return str(executable)
