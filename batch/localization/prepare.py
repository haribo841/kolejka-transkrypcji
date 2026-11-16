"""Prepare only missing/revised localization assets using retained original media."""
import json
import os
from pathlib import Path
import subprocess

from ..common import APP, NO_WINDOW, ResourceError, atomic_bytes, atomic_json, digest, read_json, settings, signature
from ..failures import resource_failure
from ..processes import ChildGuard
from . import directory, options
from .captions import render, retained_source, source_cues, warnings
from .store import metadata_source, source_field
from .translation import REVISION


PROCESS_LOG = "proces.log"
CAPTION_PREFIX = "captions:"


def text_key(store, value):
    return signature({"source": value, "model": REVISION, "glossary": options(store.root)["glossary"]})


def child(python, module, request, work, progress):
    if not Path(python).is_file():
        raise ResourceError("Brak środowiska pomocniczego. Otwórz kreator konfiguracji.")
    request_path = work / (module.rsplit(".", 1)[-1] + "-request.json")
    atomic_json(request_path, request)
    env = dict(os.environ, PYTHONIOENCODING="utf-8", HF_HUB_DISABLE_TELEMETRY="1", DO_NOT_TRACK="1")
    with (work / PROCESS_LOG).open("ab") as log:
        process = subprocess.Popen([python, "-m", module, str(request_path)], cwd=APP, env=env,
                                   creationflags=NO_WINDOW, stdout=subprocess.PIPE, stderr=log,
                                   text=True, encoding="utf-8", errors="replace", shell=False)
        with ChildGuard(process):
            try:
                for line in process.stdout:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    progress(event.get("message", "Przetwarzanie lokalne"))
                code = process.wait()
            finally:
                process.stdout.close()
    if code:
        with (work / PROCESS_LOG).open("rb") as reader:
            reader.seek(max(0, reader.seek(0, 2) - 4096))
            tail = reader.read().decode("utf-8", errors="replace")
        if resource_failure(RuntimeError(tail)):
            raise ResourceError("Brak zasobów w procesie pomocniczym. Sprawdź pamięć i miejsce na dysku. Log: " + str(work / PROCESS_LOG))
        raise RuntimeError("Proces pomocniczy zakończył się błędem. Otwórz log: " + str(work / PROCESS_LOG))


def metadata_units(store, ident):
    video = store.get_video(ident)
    snapshot = metadata_source(video)
    if not snapshot:
        raise ValueError("Najpierw użyj Odśwież z YouTube, aby pobrać aktualny tytuł i opis.")
    overrides = json.loads(video.get("language_overrides", "{}"))
    units = []
    for lang in ("pl", "en"):
        for kind in ("title", "description"):
            text = source_field(snapshot, kind, lang)
            key = text_key(store, {"text": text, "kind": kind, "lang": lang, "override": overrides.get(kind, "auto")})
            old = store.item(ident, kind, lang)
            if old["review"] == "approved" or old["source_hash"] == key:
                store.error(ident, kind, lang, "")
                continue
            units.append({"key": kind + ":" + lang, "lang": lang, "text": text,
                          "source_hash": key, "source_language": overrides.get(kind, "auto")})
    return units


def caption_units(store, ident, cues):
    units = []
    for lang in ("pl", "en"):
        key = text_key(store, cues)
        old = store.item(ident, "captions", lang)
        if old["review"] == "approved" or old["source_hash"] == key:
            store.error(ident, "captions", lang, "")
            continue
        if not cues:
            store.save_item(ident, "captions", lang, "", key, na=True)
            continue
        for cue in cues:
            units.append({"key": CAPTION_PREFIX + lang + ":" + cue["id"], "lang": lang, "text": cue["text"]})
    return units


def prepare_text(store, ident, tasks, progress):
    video = store.get_video(ident)
    base = directory(store.root, ident)
    work = base / "robocze-teksty"
    work.mkdir(parents=True, exist_ok=True)
    units, cues = [], []
    errors = []
    if "metadata" in tasks:
        try:
            units.extend(metadata_units(store, ident))
        except ValueError as error:
            errors.append(str(error))
            record_text_error(store, ident, ("title", "description"), str(error))
    if "captions" in tasks:
        try:
            cues = source_cues(store.root, video)
            units.extend(caption_units(store, ident, cues))
        except (ValueError, OSError) as error:
            if resource_failure(error):
                raise ResourceError(str(error)) from error
            errors.append(str(error))
            record_text_error(store, ident, ("captions",), str(error))
    if units:
        errors.extend(translate_units(store, ident, units, cues, work, progress))
    return errors


def translate_units(store, ident, units, cues, work, progress):
    result_path = work / "result.json"
    request = {"root": str(store.root), "cache": str(directory(store.root, ident) / "cache-teksty"),
               "units": units, "result": str(result_path)}
    try:
        child(options(store.root)["translation_python"], "batch.localization.translation", request, work, progress)
        result = read_json(result_path)
        translated = {item["key"]: item["text"] for item in result["units"]}
        if set(translated) != {u["key"] for u in units}:
            raise ValueError("Tłumaczenie nie zawiera wszystkich oczekiwanych fragmentów.")
        save_translations(store, ident, units, translated, cues, result["model"])
        return []
    except ResourceError:
        raise
    except (ValueError, RuntimeError, OSError) as error:
        if resource_failure(error):
            raise ResourceError(str(error)) from error
        pairs = {tuple(unit["key"].split(":")[:2]) for unit in units}
        for kind, lang in pairs:
            store.error(ident, kind, lang, str(error))
        return [str(error)]


def record_text_error(store, ident, kinds, message):
    for kind in kinds:
        for lang in ("pl", "en"):
            store.error(ident, kind, lang, message)


def save_translations(store, ident, units, translated, cues, model):
    base = directory(store.root, ident)
    for unit in units:
        if not unit["key"].startswith(CAPTION_PREFIX):
            kind, lang = unit["key"].split(":")
            store.save_item(ident, kind, lang, translated[unit["key"]], unit["source_hash"], generated=True)
    for lang in ("pl", "en"):
        if cues and CAPTION_PREFIX + lang + ":" + cues[0]["id"] in translated:
            output = [{**c, "text": translated[CAPTION_PREFIX + lang + ":" + c["id"]]} for c in cues]
            text = render(output)
            path = base / ("napisy." + lang + ".srt")
            atomic_bytes(path, text.encode("utf-8"))
            store.save_item(ident, "captions", lang, text, text_key(store, cues), str(path), generated=True)
            atomic_json(base / ("napisy." + lang + ".kontrola.json"), {"warnings": warnings(output), "model": model})
        fields = {k: store.item(ident, k, lang)["value"] for k in ("title", "description")}
        atomic_json(base / ("metadane." + lang + ".json"), fields)


def prepare_background(store, ident, progress):
    from ..uvr import validate_uvr
    from .audio import background_key
    item = store.item(ident, "audio", "zxx")
    if item["review"] == "approved":
        return
    video = store.get_video(ident)
    source = retained_source(store.root, video)
    base = directory(store.root, ident)
    config = settings(store.root)
    if source.stat().st_size == 0:
        store.save_item(ident, "audio", "zxx", "", "empty-audio", na=True)
        return
    validate_uvr(config)
    archive = read_json(source.parent / "zrodlo.json", {})
    if archive.get("sha256") != digest(source):
        raise ValueError("Zachowane audio zostało zmienione. Sprawdź oryginał przed separacją.")
    output, work = base / "audio" / "bez-glosow.flac", base / "robocze-podklad"
    key = background_key(source, config)
    if item["source_hash"] == key and output.is_file() and item["value"] == digest(output):
        return
    work.mkdir(parents=True, exist_ok=True)
    request = {"root": str(store.root), "video_id": ident, "source": str(source), "work": str(work),
               "output": str(output), "track": 0, "config": config}
    child(config["uvr_python"], "batch.localization.audio", request, work, progress)
    report = read_json(work / "uvr-result.json", {})
    if report.get("empty_audio"):
        store.save_item(ident, "audio", "zxx", "", key, na=True)
        return
    if not report.get("source_complete", True):
        raise ValueError("Źródło ma uszkodzenia. Zachowano podkład, ale wymaga ręcznej kontroli osi czasu.")
    store.save_item(ident, "audio", "zxx", digest(output), key, str(output), generated=True)


def prepare_text_tasks(store, ident, tasks, progress):
    errors = []
    try:
        errors.extend(prepare_text(store, ident, tasks, progress))
    except ResourceError:
        raise
    except (ValueError, RuntimeError, OSError) as error:
        if resource_failure(error):
            raise ResourceError(str(error)) from error
        errors.append(str(error))
        kinds = ("title", "description") if "metadata" in tasks else ()
        if "captions" in tasks:
            kinds += ("captions",)
        record_text_error(store, ident, kinds, str(error))
    return errors


def prepare_one(store, ident, tasks, progress):
    errors = []
    if {"metadata", "captions"} & set(tasks):
        errors.extend(prepare_text_tasks(store, ident, tasks, progress))
    if "audio" in tasks:
        try:
            prepare_background(store, ident, progress)
        except ResourceError:
            raise
        except (ValueError, RuntimeError, OSError) as error:
            if resource_failure(error):
                raise ResourceError(str(error)) from error
            errors.append(str(error))
            store.error(ident, "audio", "zxx", str(error))
    return errors
