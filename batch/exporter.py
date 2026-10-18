from __future__ import annotations

import json
import math
from pathlib import Path

from .common import atomic_new_bytes, atomic_json, digest, read_json, timestamp
from .result_paths import (EXTENSIONS, LEGACY_OUTPUTS, assert_output_owner, output_paths,
                           output_stem, unused_paths)

OUTPUTS = LEGACY_OUTPUTS


def verify_outputs(folder, identity, signature=None, *, partial=False):
    folder = Path(folder)
    try:
        manifest = read_json(folder / ("czesciowe.json" if partial else "gotowe.json"))
        if not manifest or manifest["identity"] != identity or (signature and manifest["signature"] != signature):
            return False
        paths = output_paths(folder, manifest)
        if set(manifest["files"]) != {path.name for path in paths.values()}:
            return False
        for path in paths.values():
            if digest(path) != manifest["files"][path.name]:
                return False
        for filename, checksum in manifest.get("audio_files", {}).items():
            path = (folder / filename).resolve()
            if not path.is_relative_to(folder.resolve()) or digest(path) != checksum:
                return False
        report = read_json(paths["json"])
        if report["identity"] != identity or bool(report.get("complete", True)) == partial:
            return False
        return True
    except (ValueError, KeyError, OSError, TypeError):
        return False


def validate_segments(segments, duration):
    for segment in segments:
        if not all(math.isfinite(segment[name]) for name in ("start", "end")):
            raise ValueError("Niepoprawny timestamp wyniku.")
        if not 0 <= segment["start"] <= segment["end"] <= duration + .01:
            raise ValueError("Wynik zawiera przedział poza nagraniem.")


def partial_notice(report, duration):
    reason = report.get("incomplete_reason", {})
    recovery = report.get("audio_recovery") or {}
    if reason.get("code") == "audio_decode_recovered" or recovery.get("code") == "audio_decode_recovered":
        return (f"[WYNIK CZĘŚCIOWY: zakres osi czasu do {timestamp(duration)} z {timestamp(report['duration'])}]\n"
                "Źródło zawiera uszkodzone pakiety. Odzyskane audio może mieć luki wewnątrz nagrania; "
                "brakujący dźwięk nie musi znajdować się tylko na końcu.\n"
                "Dodaj ponownie sprawną kopię nagrania albo wybierz inną ścieżkę audio. "
                "Ponowny Start tego samego uszkodzonego źródła nie potwierdzi kompletności.\n\n"
                "Przyczyna: " + reason.get("message", recovery.get("message", "Uszkodzone źródło audio.")) + "\n\n")
    return (f"[WYNIK CZĘŚCIOWY: do {timestamp(duration)} z {timestamp(report['duration'])}]\n"
            "Przy następnym Start program podejmie brakującą część.\n\n"
            "Przyczyna: " + reason.get("message", "Niepełne audio.") + "\n\n")


def export(folder, report, execution_signature, *, complete=True):
    folder = Path(folder)
    assert_output_owner(folder, report["identity"])
    duration = report["duration"] if complete else report["processed_until"]
    segments = report["segments"]
    validate_segments(segments, duration)
    report["complete"] = complete
    lines = [f"{report['title']}\n"]
    srt, vtt = [], ["WEBVTT\n\n"]
    if not complete:
        notice = partial_notice(report, duration)
        lines.append(notice)
        vtt.append("NOTE " + " ".join(notice.replace("-->", "→").splitlines()) + "\n\n")
    cue_number = 0
    for segment in segments:
        start, end = segment["start"], segment["end"]
        text = segment["text"].replace("\r", " ").replace("\n", " ").strip()
        label = segment["speaker"]
        lines.append(f"[{timestamp(start)} - {timestamp(end)}] {label}: {text}\n")
        if round(end * 1000) <= round(start * 1000) or not text:
            continue
        cue_number += 1
        # Escape markup characters so speech remains literal subtitle text.
        escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        srt.append(f"{cue_number}\n{timestamp(start, ',')} --> {timestamp(end, ',')}\n{label}: {escaped}\n\n")
        vtt.append(f"{timestamp(start)} --> {timestamp(end)}\n{label}: {escaped}\n\n")
    if not segments:
        lines.append("[Brak rozpoznanych wypowiedzi]\n")
    content = dict(zip(EXTENSIONS, ("".join(lines), "".join(srt), "".join(vtt),
                                  json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))))
    stem = output_stem(report["title"], report["identity"]) + ("" if complete else "_czesciowa")
    paths = unused_paths(folder, stem, identity=report["identity"])
    for extension, path in paths.items():
        atomic_new_bytes(path, content[extension].encode("utf-8"))
    manifest = {"format_version": 2, "identity": report["identity"], "signature": execution_signature,
                "outputs": {ext: path.name for ext, path in paths.items()},
                "files": {path.name: digest(path) for path in paths.values()},
                "audio_files": {name: digest(folder / name) for name in report.get("retained_audio", [])}}
    manifest.update(complete=complete, processed_until=report.get("processed_until", duration))
    atomic_json(folder / ("gotowe.json" if complete else "czesciowe.json"), manifest)
    if not verify_outputs(folder, report["identity"], execution_signature, partial=not complete):
        raise OSError("Sprawdzenie zapisanych wyników nie powiodło się.")
    return paths
