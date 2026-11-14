"""Subtitle parsing, original result discovery and immutable timeline validation."""
import json
import math
import re

from ..common import job_folder, read_json
from ..paths import child_path
from ..result_paths import output_paths

TIMING = re.compile(r"(\d{2,}):(\d{2}):(\d{2})[,.](\d{3})")
SPEAKER = re.compile(r"^\s*(?:Mówca\s+(?:\d+|nieustalony)|Speaker\s+\d+)\s*:\s*", re.IGNORECASE)


def seconds(text):
    match = TIMING.fullmatch(text.strip())
    if not match:
        raise ValueError("Niepoprawny czas SRT: " + text)
    hours, minutes, sec, ms = map(int, match.groups())
    if minutes >= 60 or sec >= 60:
        raise ValueError("Niepoprawne minuty lub sekundy SRT.")
    return hours * 3600 + minutes * 60 + sec + ms / 1000


def stamp(value):
    ms = round(value * 1000)
    return f"{ms // 3600000:02}:{ms // 60000 % 60:02}:{ms // 1000 % 60:02},{ms % 1000:03}"


def validate(cues, duration=None):
    previous, identifiers = -1, set()
    for cue in cues:
        start, end = cue["start"], cue["end"]
        if not math.isfinite(start) or not math.isfinite(end) or not 0 <= start < end or start < previous:
            raise ValueError("Napisy mają niepoprawne lub nieuporządkowane przedziały.")
        if duration is not None and end > duration + .1:
            raise ValueError("Napisy wychodzą poza czas trwania filmu.")
        if cue["id"] in identifiers or not cue["text"].strip():
            raise ValueError("Powtórzony identyfikator albo pusta wypowiedź SRT.")
        previous = start
        identifiers.add(cue["id"])
    return cues


def parse_srt(text, duration=None):
    result = []
    for block in re.split(r"\n\s*\n", text.lstrip("\ufeff").replace("\r\n", "\n").strip()):
        if not block.strip():
            continue
        lines = block.splitlines()
        if len(lines) < 3 or not lines[0].isdigit() or " --> " not in lines[1]:
            raise ValueError("Każdy blok SRT wymaga numeru, przedziału czasu i tekstu.")
        start, end = lines[1].split(" --> ", 1)
        result.append({"id": lines[0], "start": seconds(start), "end": seconds(end),
                       "text": SPEAKER.sub("", "\n".join(lines[2:])).strip()})
    return validate(result, duration)


def render(cues):
    validate(cues)
    return "\n\n".join(f"{c['id']}\n{stamp(c['start'])} --> {stamp(c['end'])}\n{c['text']}" for c in cues) + ("\n" if cues else "")


def warnings(cues):
    return [f"Segment {c['id']}: długi napis lub szybkie czytanie." for c in cues
            if len(c["text"]) > 160 or len(c["text"]) / (c["end"] - c["start"]) > 25]


def source_cues(root, job):
    folder = job_folder(root, job)
    paths = output_paths(folder)
    if paths["json"].is_file():
        report = json.loads(paths["json"].read_text(encoding="utf-8-sig"))
        if report.get("identity") != job["identity"] or not report.get("complete", True):
            raise ValueError("Źródłowa transkrypcja jest niepełna albo należy do innego filmu.")
        cues = [{"id": str(i + 1), "start": s["start"], "end": s["end"],
                 "text": SPEAKER.sub("", s["text"]).strip()} for i, s in enumerate(report["segments"]) if s["text"].strip()]
        return validate(cues, job["duration"])
    if paths["srt"].is_file():
        return parse_srt(paths["srt"].read_text(encoding="utf-8-sig"), job["duration"])
    raise ValueError("Brak ukończonej transkrypcji. Najpierw przetwórz film w kolejce YouTube.")


def retained_source(root, job):
    folder = job_folder(root, job)
    manifest = read_json(folder / "audio" / "zrodlo.json", {})
    if manifest.get("file"):
        path = child_path(folder / "audio", manifest["file"])
        if path.is_file():
            return path
    raise ValueError("Brak zachowanego oryginału audio. Dodaj źródło przez kolejkę transkrypcji.")
