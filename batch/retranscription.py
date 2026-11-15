"""Explicit, versioned ASR retries without deleting old audio or checkpoints."""
import json
from pathlib import Path
import re
import uuid

from .common import ResourceError, atomic_bytes, atomic_json, digest, job_folder, read_json
from .result_paths import assert_output_owner, output_paths


def validate_request(request):
    if (not isinstance(request, dict) or
            not re.fullmatch(r"[0-9a-f]{32}", str(request.get("id", ""))) or
            request.get("input") not in {"original", "vocal", "source"} or
            request.get("language") not in {"auto", "pl", "en"} or
            request.get("seconds") not in {30, 60, 120, 600} or
            type(request.get("diarization")) is not bool or
            request.get("state") not in {"requested", "completed"}):
        raise ValueError("Niepoprawne ustawienia ponownej transkrypcji.")
    return request


def new_request(*, input_kind="original", language="auto", seconds=60, diarization=False):
    return validate_request({"id": uuid.uuid4().hex, "input": input_kind, "language": language,
                             "seconds": seconds, "diarization": diarization, "state": "requested"})


def pending_request(job):
    request = json.loads(job.get("retry_request", "{}"))
    if not request:
        return None
    validate_request(request)
    return request if request["state"] == "requested" else None


def retry_config(config, request):
    # A retained vocal is already separated; the original is an explicit bypass
    # of UVR. Never replace the archived source or vocal while retrying ASR.
    return {**config, "uvr_enabled": False, "keep_audio": False,
            "diarization": request["diarization"], "asr_seconds": request["seconds"], "asr_context": 5}


def attempt_work(folder, request):
    # Short sibling name preserves the existing Windows path budget. Check the
    # full UUID before reusing it, including the unlikely short-name collision.
    work = folder / ("ponow-" + request["id"][:12])
    work.mkdir(parents=True, exist_ok=True)
    owner = read_json(work / "proba.json", {})
    if owner and owner.get("id") != request["id"]:
        raise ResourceError("Katalog należy do innej próby transkrypcji. Otwórz opcję ponowienia jeszcze raz.")
    atomic_json(work / "proba.json", request)
    return work


def retained_inputs(root, job):
    """Cheap discovery for the dialog. The worker verifies hashes before use."""
    folder = job_folder(root, job)
    archive = read_json(folder / "audio" / "zrodlo.json", {})
    candidates = {"vocal": folder / "audio" / "wokal.flac"}
    name = archive.get("file")
    if isinstance(name, str) and Path(name).name == name:
        candidates["original"] = folder / "audio" / name
    return {kind: path for kind, path in candidates.items()
            if path.resolve().is_relative_to(folder.resolve()) and path.is_file()}


def retry_input(root, job, request, manifest):
    if request["input"] == "source":
        return None
    folder = job_folder(root, job)
    path = retained_inputs(root, job).get(request["input"])
    if path is None:
        raise ResourceError("Brak wybranego zachowanego audio. Wybierz ponownie źródło transkrypcji.")
    expected = manifest.get("audio_files", {}).get(str(path.relative_to(folder)))
    if not expected and request["input"] == "original":
        expected = (read_json(folder / "audio" / "zrodlo.json", {}) or {}).get("sha256")
    if not expected or digest(path) != expected:
        raise ResourceError("Zachowane audio nie przeszło kontroli sumy SHA-256. Wybierz oryginalne źródło nagrania.")
    return path


def previous_results(folder, work, identity):
    """Keep the old manifest so a crash after export still has a stable baseline."""
    assert_output_owner(folder, identity)
    saved = work / "poprzednie-wyniki.json"
    if not saved.exists():
        atomic_json(saved, read_json(folder / "gotowe.json", {}) or read_json(folder / "czesciowe.json", {}))
    manifest = read_json(saved, {})
    report = read_json(output_paths(folder, manifest)["json"], {}) if manifest else {}
    return manifest, report


def invalidate_captions(root, job, request, work):
    """Invalidate only derived captions, retaining text, approvals and remote IDs as history."""
    if job["kind"] != "youtube":
        return
    from .localization.store import LocalizationStore
    store = LocalizationStore(root)
    ident = job["identity"].split(":", 1)[1]
    saved = work / "poprzednie-napisy.json"
    if not saved.exists():
        atomic_json(saved, [item for item in store.items(ident) if item["kind"] == "captions"])
    for item in read_json(saved, []):
        path = work / ("poprzednie-napisy." + item["lang"] + ".srt")
        if item["value"] and not path.exists():
            atomic_bytes(path, item["value"].encode("utf-8"))
    store.invalidate_captions(ident, request["id"])
