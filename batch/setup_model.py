"""One-time gated model download. Token is read from stdin, never saved."""
from __future__ import annotations

import json
from datetime import datetime
import os
from pathlib import Path
import sys
import subprocess

os.environ["PYANNOTE_METRICS_ENABLED"] = "0"
os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
os.environ["DO_NOT_TRACK"] = "1"
os.environ["OTEL_SDK_DISABLED"] = "true"

from .common import APP, NO_WINDOW, WorkerLock, atomic_json, digest, job_folder, settings
from .diarization import validate_diarization
from .exporter import verify_outputs
from .importers import import_local
from .hf_access import MODEL_ID, access_error, check_access, redact
from .media import media_metadata
from .paths import input_file, media_tool
from .processes import ChildGuard
from .readiness import configuration_fingerprint, environment_versions
from .store import Store, now
from .worker import Worker


class AccessProblem(RuntimeError):
    def __init__(self, result):
        super().__init__(result["message"])
        self.result = result


def write_state(root, state, message, phase="", **extra):
    atomic_json(Path(root) / "konfiguracja-modelu.json",
                {"state": state, "phase": phase, "message": redact(message), "at": now(), **extra})


def smoke_test(root, source=None, *, track=0, language=None, seconds=60):
    root = Path(root)
    config = settings(root)
    if not source:
        raise ValueError("Wybierz nagranie przyciskiem Sprawdź na fragmencie.")
    source = input_file(source)
    metadata = media_metadata(source, config)
    if type(track) is not int or not 0 <= track < len(metadata["audio_tracks"]):
        raise ValueError("Wybierz istniejącą ścieżkę audio do próby.")
    if not 0 < seconds <= 120:
        raise ValueError("Długość próby musi wynosić od 1 do 120 sekund.")
    # Every smoke test executes the current models, even if an earlier sample
    # with the same content has already passed under different settings.
    test_root = root / "proba" / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    test_root.mkdir(parents=True, exist_ok=True)
    atomic_json(test_root / "ustawienia.json", config)
    sample = test_root / "fragment.wav"
    with (test_root / "fragment-ffmpeg.log").open("wb") as errors:
        process = subprocess.Popen([media_tool(config["ffmpeg"], "ffmpeg"), "-nostdin", "-hide_banner", "-loglevel", "error",
            "-xerror", "-protocol_whitelist", "file,pipe", "-i", str(source), "-t", str(seconds),
            "-map", f"0:a:{track}", "-vn", "-sn", "-dn", "-ac", "2", "-ar", "44100", "-c:a", "pcm_s16le", str(sample)],
            stderr=errors, stdout=subprocess.DEVNULL, creationflags=NO_WINDOW, shell=False)
        with ChildGuard(process):
            if process.wait():
                raise RuntimeError("Nie udało się odczytać fragmentu audio do próby. Sprawdź fragment-ffmpeg.log w " + str(test_root))
    store = Store(test_root)
    import_local(store, [sample], config=config)
    wanted = "local:" + digest(sample)
    # A previous failed sample must not be chosen in place of the new one.
    for job in store.jobs():
        store.configure([job["id"]], enabled=int(job["identity"] == wanted))
    store.retry()
    job = next(j for j in store.jobs() if j["identity"] == wanted)
    store.configure([job["id"]], language=language or config.get("language", "auto"))
    store.start(kind="local")
    Worker(test_root).run(max_jobs=1)
    job = next(j for j in store.jobs() if j["identity"] == wanted)
    folder = job_folder(test_root, job)
    passed = job["status"] == "done" and verify_outputs(folder, wanted)
    versions = environment_versions(config)
    from .result_paths import output_paths
    from .common import read_json
    detail = read_json(output_paths(folder)["json"], {}) if passed else {}
    invoked = any(not block.get("digital_silence", True) for block in detail.get("asr", {}).get("blocks", []))
    error = job["error"] or store.control()["message"]
    if passed and not invoked:
        passed = False
        error = "Fragment zawiera wyłącznie cyfrową ciszę. Whisper nie został wywołany; wybierz fragment z mową."
    devices = {"whisper": config.get("device", "CPU"),
               "diarization": sorted({p.get("device", {}).get("device", "unknown") for p in detail.get("diarization_blocks", [])}),
               "uvr": sorted({p.get("device", "unknown") for p in (detail.get("uvr") or {}).get("blocks", [])})}
    report = {"passed": passed, "at": now(), "source": str(source), "identity": wanted,
              "whisper_revision": config["model_revision"], "diar_revision": config["diar_revision"],
              "fingerprint": configuration_fingerprint(config, versions), "versions": versions, "devices": devices,
              "uvr_enabled": config.get("uvr_enabled", False), "diarization": config.get("diarization", False),
              "settings": config, "track": track, "sample_seconds": job["duration"],
              "results": str(folder), "error": error}
    atomic_json(root / "pierwsza-proba.json", report)
    if not passed:
        raise RuntimeError("Próba całego procesu nie powiodła się: " + report["error"])
    return report


def configure(root, token):
    from huggingface_hub import snapshot_download
    root = Path(root)
    config = settings(root)
    destination = Path(config["diar_model"])
    write_state(root, "running", "Sprawdzanie tokenu i dostępu do Community-1…", "access")
    access = check_access(token, config["diar_revision"])
    if not access["ok"]:
        raise AccessProblem(access)
    write_state(root, "running", "Dostęp potwierdzony. Pobieranie Community-1. Przy wolnym łączu może potrwać kilka minut; okno można zamknąć.", "download")
    # token is passed directly to the request; huggingface_hub.login is never used.
    snapshot_download(repo_id=MODEL_ID, revision=config["diar_revision"],
                      local_dir=destination, token=token, max_workers=2)
    write_state(root, "running", "Model pobrany. Sprawdzanie kompletności plików…", "verify")
    validate_diarization(config)
    files = {str(p.relative_to(destination)): digest(p) for p in destination.rglob("*")
             if p.is_file() and ".cache" not in p.parts and p.name != "pobrano.json"}
    atomic_json(destination / "pobrano.json", {"repository": MODEL_ID,
                "revision": config["diar_revision"], "files": files, "at": now()})
    write_state(root, "ready", "Model mówców pobrany i sprawdzony. Kliknij Sprawdź na fragmencie i wybierz własne nagranie; przetworzymy do 60 sekund. Przed próbą pobierz także model Whisper. Kolejka filmów czeka na Twój Start.", "download_complete")


def main():
    root = Path(sys.argv[1]).resolve()
    lock = WorkerLock(root / "konfiguracja")
    if not lock.acquire():
        return 0
    token = ""
    request = {}
    try:
        request = json.loads(sys.stdin.readline())
        if request.get("download_whisper"):
            from transcribe import download
            from types import SimpleNamespace
            write_state(root, "running", "Pobieranie przypiętego modelu Whisper. Poczekaj na zakończenie.", "download_whisper")
            download(SimpleNamespace(model=Path(settings(root)["model"])))
            write_state(root, "ready", "Whisper pobrany i sprawdzony. Wróć do kreatora i kliknij Dalej.", "whisper_complete")
        elif request.get("test_only"):
            write_state(root, "running", "Trwa próba fragmentu z aktualnymi opcjami Whisper, UVR i mówców. Pierwsze uruchomienie modeli może potrwać kilka minut.", "test")
            report = smoke_test(root, request.get("source"), track=request.get("track", 0),
                                language=request.get("language"), seconds=request.get("seconds", 60))
            write_state(root, "ready", "Próba zakończona. Otwórz wyniki i sprawdź tekst. Kolejka filmów czeka na Twój Start.", "complete", test_results=report["results"])
        else:
            token = request.pop("token", "").strip()
            if not token:
                raise ValueError("Wpisz token Hugging Face z dostępem do pobrania modelu.")
            configure(root, token)
        return 0
    except Exception as exc:
        if request.get("download_whisper"):
            result = {"message": "Pobieranie Whisper nie powiodło się: " + str(exc), "code": "whisper_download_failed"}
        elif request.get("test_only"):
            result = {"message": str(exc), "code": "test_failed"}
            atomic_json(root / "pierwsza-proba.json", {"passed": False, "at": now(), "error": redact(str(exc)), "source": request.get("source", "")})
        else:
            result = exc.result if isinstance(exc, AccessProblem) else access_error(exc, token)
        write_state(root, "error", redact(result["message"], token), code=result["code"])
        return 1
    finally:
        token = ""
        lock.close()


if __name__ == "__main__":
    sys.exit(main())
