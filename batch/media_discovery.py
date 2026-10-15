"""Conservative exclusions for automatic discovery of local recordings.

These helpers describe technical inputs; the caller decides whether to exclude
them. Explicitly selected files can still be imported. I/O errors are deliberately
left to the caller so inaccessible inputs are reported instead of disappearing.
"""
from __future__ import annotations

import codecs
from pathlib import Path
import re
import stat


TS_SNIFF_BYTES = 64 * 1024
PACKAGE_DIRECTORIES = {"site-packages", "dist-packages"}
TYPESCRIPT_SOURCE = re.compile(
    r"^\s*(?:"
    r"import\s+(?:type\s+)?(?:[{'\"*]|[A-Za-z_$][\w$]*(?:\s+from\b|\s*[,=]))"
    r"|export\s+(?:default\b|(?:declare\s+)?(?:const|let|var|class|interface|type|enum|function|namespace|async)\b)"
    r"|(?:declare\s+)?(?:interface|type|enum|namespace)\s+[A-Za-z_$][\w$]*"
    r"|(?:const|let|var)\s+[A-Za-z_$][\w$]*\s*[:=]"
    r"|(?:async\s+)?function\s+[A-Za-z_$][\w$]*\s*\("
    r"|(?:abstract\s+)?class\s+[A-Za-z_$][\w$]*\s*(?:[<{]|extends\b|implements\b)"
    r")",
    re.MULTILINE,
)
QT_TRANSLATION = re.compile(r"<(?:!DOCTYPE\s+TS\b|TS[\s>])")


def _path_stat(path):
    try:
        return path.stat()
    except (FileNotFoundError, NotADirectoryError):
        return None


def _regular_file(path):
    metadata = _path_stat(path)
    return metadata is not None and stat.S_ISREG(metadata.st_mode)


def technical_directory(path):
    """Return a reason for a confirmed environment/package directory, if any."""
    path = Path(path)
    if path.name.casefold() in PACKAGE_DIRECTORIES:
        metadata = _path_stat(path)
        if metadata is not None and stat.S_ISDIR(metadata.st_mode):
            return "Pakiety Pythona (" + path.name + ")"
    if _regular_file(path / "pyvenv.cfg"):
        return "Środowisko Pythona (pyvenv.cfg)"
    if _regular_file(path / "conda-meta" / "history"):
        return "Środowisko Conda (conda-meta/history)"
    return None


def _transport_stream(data):
    # MPEG TS uses 188-byte packets; M2TS and Reed-Solomon variants use 192/204.
    # Positive recognition protects binary inputs even if the bytes also happen
    # to decode as text. Failure to recognize packets never excludes a file.
    for packet_size in (188, 192, 204):
        count = 5
        for offset in range(min(packet_size, len(data))):
            last = offset + (count - 1) * packet_size
            if last < len(data) and all(data[offset + index * packet_size] == 0x47 for index in range(count)):
                return True
    return False


def _printable_text(data):
    if not data:
        return None
    encoding = "utf-16" if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)) else "utf-8-sig"
    try:
        # The bounded sample may finish in the middle of a Unicode character.
        text = codecs.getincrementaldecoder(encoding)(errors="strict").decode(data, final=False)
    except UnicodeDecodeError:
        return None
    if any(not character.isprintable() and character not in "\t\r\n\f" for character in text):
        return None
    return text


def _technical_ts(path):
    with path.open("rb") as stream:
        data = stream.read(TS_SNIFF_BYTES)
    if _transport_stream(data):
        return None
    text = _printable_text(data)
    if text is None:
        return None
    if "# CMAKE generated file:" in text and "Timestamp file for compiler generated dependencies" in text:
        return "Tekstowy plik zależności CMake (.ts)"
    if QT_TRANSLATION.search(text):
        return "Tekstowy plik tłumaczeń Qt (.ts)"
    if TYPESCRIPT_SOURCE.search(text):
        return "Kod źródłowy TypeScript (.ts)"
    return None


def technical_source(path):
    """Describe a source under technical directories or a clear textual .ts.

    Ordinary names such as env, venv, test, build and vendor do not establish an
    exclusion. Unrecognized and damaged binary .ts files remain discoverable so
    the media validator can provide their individual errors.
    """
    path = Path(path)
    for parent in path.parents:
        reason = technical_directory(parent)
        if reason:
            return reason + ": " + str(parent)
    if path.suffix.casefold() == ".ts":
        return _technical_ts(path)
    return None
