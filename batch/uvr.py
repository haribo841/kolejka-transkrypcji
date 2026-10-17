from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

from .common import APP, NO_WINDOW, ResourceError, atomic_json, check_disk, digest, read_json, signature
from .media import EmptyAudio, probe
from .paths import child_path, input_file, local_path, media_tool, output_file
from .processes import ChildGuard
from .failures import JobError, resource_failure


def validate_uvr(config):
    if not Path(config["uvr_python"]).is_file():
        raise ResourceError("Brak środowiska UVR. Uruchom Instaluj-UVR.ps1.")
    directory = local_path(config["uvr_model_dir"])
    for name in (config["uvr_model"], "download_checks.json", "mdx_model_data.json", "vr_model_data.json", "gotowe.json"):
        if not child_path(directory, name).is_file():
            raise ResourceError("Brak modelu UVR. Kliknij Pobierz model UVR.")


def separate(source, work, folder, track, config, progress, *, recovery_only=False):
    validate_uvr(config)
    source, work, folder = input_file(source), local_path(work), local_path(folder)
    key = signature({"source": digest(source), "track": track, "model": config["uvr_model"],
                     "model_sha256": digest(child_path(config["uvr_model_dir"], config["uvr_model"])),
                     "seconds": config["uvr_seconds"], "context": config["uvr_context"], "version": 1})
    output = folder / "audio" / "wokal.flac"
    manifest = read_json(work / "uvr-result.json")
    if manifest and manifest.get("complete", True) and manifest.get("signature") == key and output.is_file() and digest(output) == manifest["sha256"]:
        progress("UVR: używam zachowanego wokalu", 100)
        return output, manifest
    request = {"source": str(source), "work": str(work), "output": str(output), "track": track,
               "config": config, "signature": key, "recovery_only": recovery_only}
    atomic_json(work / "uvr-request.json", request)
    env = dict(os.environ, PYTHONIOENCODING="utf-8", DO_NOT_TRACK="1")
    env["PATH"] = str(Path(config["ffmpeg"]).parent) + os.pathsep + env.get("PATH", "")
    with (work / "uvr.log").open("ab") as log:
        process = subprocess.Popen([config["uvr_python"], "-m", "batch.uvr_child", str(work / "uvr-request.json")],
            cwd=APP, stdout=subprocess.PIPE, stderr=log, encoding="utf-8", errors="replace", env=env,
            creationflags=NO_WINDOW)
        error, failure_scope, failure_code = "", "job", "uvr_failed"
        empty = None
        with ChildGuard(process):
            for line in process.stdout:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("empty_audio"):
                    empty = event
                elif "error" in event:
                    error = event["error"]
                    failure_scope, failure_code = event.get("scope", "job"), event.get("code", "uvr_failed")
                else:
                    progress(event.get("message", "Separacja UVR"), event.get("percent", 0))
            code = process.wait()
            process.stdout.close()
    if not code and empty is not None:
        raise EmptyAudio(empty.get("duration"), empty.get("reason", "decoder_eof"))
    if code:
        message = "Separacja UVR nie została ukończona: " + (error or "sprawdź uvr.log")
        if failure_scope == "session":
            raise ResourceError(message)
        raise JobError(message, failure_code)
    manifest = read_json(work / "uvr-result.json")
    return verified_vocal(folder, manifest, key)


def verified_vocal(folder, manifest, key):
    try:
        output = child_path(folder, str(Path(manifest["output"]).relative_to(folder)))
        valid = manifest["signature"] == key and output.is_file() and digest(output) == manifest["sha256"]
    except (ValueError, KeyError, TypeError, OSError):
        valid = False
    if not valid:
        raise JobError("Niekompletny wynik UVR. Zachowano pobrane audio i ukończone bloki.", "uvr_result_invalid")
    return output, manifest


def cached_archive(audio_dir, source_hash, track):
    try:
        saved = read_json(child_path(audio_dir, "zrodlo.json"))
        if saved and saved.get("source_sha256") == source_hash and saved.get("track") == track:
            path = child_path(audio_dir, saved["file"])
            if path.is_file() and digest(path) == saved["sha256"]:
                return path
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def archive_source(source, folder, track, config):
    source, folder = input_file(source), local_path(folder)
    audio_dir = child_path(folder, "audio")
    audio_dir.mkdir(parents=True, exist_ok=True)
    source_hash = digest(source)
    cached = cached_archive(audio_dir, source_hash, track)
    if cached is not None:
        return cached
    metadata = probe(source, config) if source.stat().st_size else {"streams": []}
    muxed = any(s.get("codec_type") == "video" for s in metadata["streams"])
    destination = output_file(child_path(audio_dir, "zrodlo.mka" if muxed else "zrodlo" + source.suffix.lower()))
    temporary = output_file(destination.with_name(destination.name + ".partial"))
    if source in {destination, temporary}:
        raise ResourceError("Archiwizacja nie może nadpisać pliku źródłowego.")
    check_disk(folder, source.stat().st_size, config["min_free_gb"])
    if muxed:
        with (audio_dir / "archiwizacja.log").open("wb") as log:
            process = subprocess.Popen([media_tool(config["ffmpeg"], "ffmpeg"), "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                "-protocol_whitelist", "file,pipe", "-i", str(source), "-map", f"0:a:{track}", "-vn", "-c:a", "copy", "-f", "matroska", str(temporary)],
                stderr=log, stdout=subprocess.DEVNULL, creationflags=NO_WINDOW, shell=False)
            with ChildGuard(process):
                code = process.wait()
            if code:
                message = (audio_dir / "archiwizacja.log").read_text(encoding="utf-8", errors="replace")[-2000:]
                if resource_failure(RuntimeError(message)):
                    raise ResourceError("Archiwizacja audio: " + message)
                raise JobError("Nie udało się zachować ścieżki audio: " + message, "audio_archive_failed")
        if not temporary.stat().st_size:
            raise JobError("Archiwum audio jest puste.", "audio_archive_failed")
    else:
        with source.open("rb") as reader, temporary.open("wb") as writer:
            shutil.copyfileobj(reader, writer, length=1024 * 1024)
            writer.flush()
            os.fsync(writer.fileno())
        if digest(temporary) != source_hash:
            raise JobError("Kopia źródłowego audio nie przeszła weryfikacji.", "audio_archive_failed")
    os.replace(temporary, destination)
    atomic_json(audio_dir / "zrodlo.json", {"source_sha256": source_hash, "track": track,
                "file": destination.name, "sha256": digest(destination), "stream_copy": muxed})
    return destination
