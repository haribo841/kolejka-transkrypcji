"""Remove speaker annotations while preserving cue times and spoken text."""
from __future__ import annotations

import codecs
from dataclasses import dataclass
from pathlib import Path
import re

from .common import atomic_new_bytes, slug

FORMATS = {".txt", ".srt", ".vtt"}
TIME = r"(?:\d+:)?\d{2}:\d{2}(?:[.,]\d{1,3})?"
TXT_TIME = re.compile(r"^(?P<time>[ \t]*\[" + TIME + r"(?:[ \t]*-[ \t]*" + TIME + r")?\])")
SUBTITLE_TIME = re.compile(r"^[ \t]*" + TIME + r"[ \t]+-->[ \t]+" + TIME)
SPEAKER = re.compile(r"^(?P<indent>[ \t]*(?:-[ \t]*)?)(?P<label>M[oó]wca[ \t]+(?:\d+|nieustalony)|"
                     r"Speaker[ _-]*\d+|Unknown[ \t]+speaker)[ \t]*:[ \t]*", re.IGNORECASE)
VOICE = re.compile(r"<v(?:\.[^\s.>]+)*+[ \t]++(?P<label>[^>\r\n]++)>", re.IGNORECASE)
VOICE_END = re.compile(r"</v[ \t]*>", re.IGNORECASE)
SINGLE_SPEAKER = re.compile(r"M[oó]wca[ \t]+(?:0*1|nieustalony)\Z", re.IGNORECASE)
AUTO_SPEAKER_SETTING = "omit_single_speaker_labels"


@dataclass
class Transcript:
    path: Path
    text: str
    encoding: str
    bom: bytes

    def encode(self, text):
        return self.bom + text.encode(self.encoding)


@dataclass
class EditedTranscript:
    text: str
    removed: int
    cues: int
    labels: tuple[str, ...] = ()


def single_speaker_labels(labels):
    values = [str(label).strip() for label in labels]
    return bool(values) and all(SINGLE_SPEAKER.fullmatch(value) for value in values)


def load_transcript(path):
    path = Path(path).resolve(strict=True)
    if path.suffix.lower() not in FORMATS:
        raise ValueError("Wybierz transkrypcję TXT albo napisy SRT/VTT.")
    if path.stat().st_size > 64 * 1024 * 1024:
        raise ValueError("Plik tekstowy przekracza 64 MB. Podziel go na mniejsze pliki do obróbki.")
    raw = path.read_bytes()
    encoding, bom = "utf-8", b""
    for mark, codec in ((codecs.BOM_UTF8, "utf-8"), (codecs.BOM_UTF16_LE, "utf-16-le"),
                        (codecs.BOM_UTF16_BE, "utf-16-be")):
        if raw.startswith(mark):
            encoding, bom, raw = codec, mark, raw[len(mark):]
            break
    try:
        text = raw.decode(encoding)
    except UnicodeError as exc:
        raise ValueError("Nie rozpoznano kodowania tekstu. Zapisz kopię jako UTF-8 lub UTF-16 i dodaj ją ponownie.") from exc
    return Transcript(path, text, encoding, bom)


def speaker_payload(payload, eligible=True, labels=None):
    match = SPEAKER.match(payload) if eligible else None
    if match:
        if labels is not None:
            labels.add(match["label"].strip())
        return match["indent"] + payload[match.end():], 1
    return payload, 0


def remove_txt_speakers(text):
    lines, removed, cues = [], 0, 0
    labels = set()
    for line in text.splitlines(keepends=True):
        match = TXT_TIME.match(line)
        if match is None:
            lines.append(line)
            continue
        payload, count = speaker_payload(line[match.end():], labels=labels)
        lines.append(line[:match.end()] + payload)
        removed += count
        cues += 1
    return EditedTranscript("".join(lines), removed, cues, tuple(sorted(labels)))


def remove_subtitle_speakers(text, extension):
    lines, removed, cues = [], 0, 0
    labels = set()
    in_cue = False
    first_payload = False
    for line in text.splitlines(keepends=True):
        if not line.strip():
            in_cue = False
        if SUBTITLE_TIME.match(line):
            cues += 1
            in_cue = True
            first_payload = True
            lines.append(line)
            continue
        if not in_cue:
            lines.append(line)
            continue
        payload = line
        if extension == ".vtt":
            labels.update(match["label"].strip() for match in VOICE.finditer(payload))
            payload, count = VOICE.subn("", payload)
            removed += count
            # A voice span can close on a later line within the same cue.
            payload = VOICE_END.sub("", payload)
        payload, count = speaker_payload(payload, first_payload or payload.lstrip(" \t").startswith("-"), labels)
        first_payload = False
        removed += count
        lines.append(payload)
    return EditedTranscript("".join(lines), removed, cues, tuple(sorted(labels)))


def remove_speakers(text, extension, *, only_single=False):
    extension = extension.lower()
    if extension not in FORMATS:
        raise ValueError("Obsługiwane formaty to TXT, SRT i VTT.")
    result = remove_txt_speakers(text) if extension == ".txt" else remove_subtitle_speakers(text, extension)
    if only_single and not single_speaker_labels(result.labels):
        return EditedTranscript(text, 0, result.cues, result.labels)
    return result


def save_without_speakers(source, output_dir=None, *, only_single=False):
    loaded = load_transcript(source)
    edited = remove_speakers(loaded.text, loaded.path.suffix, only_single=only_single)
    if only_single and not edited.removed:
        return None, edited
    destination = Path(output_dir) if output_dir else loaded.path.parent
    stem = loaded.path.stem
    if stem.casefold() in {"transkrypcja", "transkrybcja", "transcript"}:
        stem = loaded.path.parent.name + "_" + stem
    stem = re.sub(r"_transkrypcja(?=(?:__wersja-\d+)?$)", "", stem)
    stem = slug(stem, limit=110).rstrip(" .") + "_bez_mowcow"
    version = 1
    while True:
        suffix = "" if version == 1 else f"__{version}"
        path = destination / (stem + suffix + loaded.path.suffix.lower())
        try:
            atomic_new_bytes(path, loaded.encode(edited.text))
            return path, edited
        except FileExistsError:
            version += 1
