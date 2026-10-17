"""Lightweight offline checks and evidence tied to the current pipeline."""
from __future__ import annotations

import importlib.metadata
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import uuid

from .common import APP, NO_WINDOW, application_python, check_disk, read_json, signature
from .diarization import validate_diarization
from .paths import local_path, media_tool
from .uvr import validate_uvr
from .youtube_access import SessionError, access_fingerprint, preferences

OFF = "Wyłączony"
CHECKING = "Sprawdzanie"
ACTION = "Wymaga działania"
AVAILABLE = "Gotowy do próby"
PASSED = "Próba zaliczona"
CORE = ("whisper", "tools", "diarization", "uvr")
KEYS = ("model", "model_revision", "device", "language", "hotwords", "audio_track", "asr_seconds",
        "asr_context", "diarization", "diar_model", "diar_revision", "diar_python", "diar_seconds",
        "diar_context", "uvr_enabled", "uvr_python", "uvr_model_dir", "uvr_model", "uvr_seconds",
        "uvr_context", "keep_audio", "ffmpeg", "ffprobe")


def test_time(value):
    try:
        return datetime.fromisoformat(value).astimezone().strftime("%d.%m.%Y %H:%M:%S")
    except (TypeError, ValueError):
        return str(value or "")


def device_text(values):
    names = {"cuda": "GPU (CUDA)", "cpu": "CPU", "unknown": "nieustalone"}
    return ", ".join(names.get(value, value) for value in values)


def package_versions(python, packages):
    if not Path(python).is_file():
        raise FileNotFoundError("Nie znaleziono środowiska Python: " + str(python))
    if Path(python).resolve() == Path(application_python()).resolve():
        result = {}
        for package in packages:
            try:
                result[package] = importlib.metadata.version(package)
            except importlib.metadata.PackageNotFoundError:
                result[package] = None
        return result
    script = ("import importlib.metadata as m,json,sys\nresult={}\n"
              "for p in sys.argv[1:]:\n try:result[p]=m.version(p)\n"
              " except m.PackageNotFoundError:result[p]=None\nprint(json.dumps(result))")
    result = subprocess.run([str(python), "-c", script, *packages], capture_output=True,
                            creationflags=NO_WINDOW, timeout=20, shell=False)
    if result.returncode:
        raise RuntimeError("Środowisko Python nie uruchamia się: " + str(python))
    return json.loads(result.stdout)


def environment_versions(config):
    result = {"whisper": package_versions(application_python(), ["openvino", "openvino-genai", "numpy"])}
    if config.get("diarization"):
        result["diarization"] = package_versions(config["diar_python"], ["torch", "pyannote.audio"])
    if config.get("uvr_enabled"):
        result["uvr"] = package_versions(config["uvr_python"], ["torch", "audio-separator", "onnxruntime", "onnxruntime-gpu"])
    return result


def file_stamp(path):
    path = Path(path)
    try:
        stat = path.stat()
        return [str(path), stat.st_size, stat.st_mtime_ns]
    except OSError:
        return [str(path), None, None]


def configuration_fingerprint(config, versions=None):
    stamps = [file_stamp(config[key]) for key in ("ffmpeg", "ffprobe")]
    for enabled, key in ((True, "model"), (config.get("diarization"), "diar_model"),
                         (config.get("uvr_enabled"), "uvr_model_dir")):
        if enabled:
            directory = Path(config[key])
            stamps.extend(file_stamp(p) for p in sorted(directory.rglob("*"))
                          if p.is_file() and ".cache" not in p.parts and p.suffix in {".xml", ".bin", ".onnx", ".yaml", ".json", ".npz"})
    backend = {name: signature((APP / "batch" / name).read_text(encoding="utf-8"))
               for name in ("asr.py", "worker.py", "media.py", "uvr.py", "uvr_child.py", "uvr_recovery.py",
                            "uvr_timeline.py", "diarization.py", "diarize_child.py", "setup_model.py", "output_fallback.py")}
    return signature({"format": 1, "settings": {key: config.get(key) for key in KEYS},
                      "files": stamps, "versions": versions if versions is not None else environment_versions(config),
                      "backend": backend})


def require_packages(python, packages):
    versions = package_versions(python, packages)
    missing = [name for name, version in versions.items() if version is None]
    if missing:
        raise RuntimeError("Brak pakietów: " + ", ".join(missing) + ". Uzupełnij środowisko aplikacji.")
    return versions


def check_whisper(config):
    from transcribe import validate_model
    validate_model(Path(config["model"]))
    require_packages(application_python(), ["openvino", "openvino-genai", "numpy"])
    return "Whisper large-v3-turbo | " + config.get("device", "CPU")


def check_tools(config, root):
    for name in ("ffmpeg", "ffprobe"):
        media_tool(config[name], name)
    check_disk(root, reserve_gb=config["min_free_gb"])
    for size, context in ((config["asr_seconds"], config["asr_context"]),
                          (config["diar_seconds"], config["diar_context"]),
                          (config["uvr_seconds"], config["uvr_context"])):
        if size <= 0 or context < 0 or context >= size / 2:
            raise ValueError("Niepoprawna długość bloku lub kontekstu.")
    return "FFmpeg, FFprobe i miejsce na dysku dostępne. Folder nagrania sprawdzamy przed Start."


def check_diar(config):
    validate_diarization(config)
    require_packages(config["diar_python"], ["torch", "pyannote.audio"])
    return "Community-1 dostępny; urządzenie potwierdzi próba."


def check_uvr(config):
    validate_uvr(config)
    versions = require_packages(config["uvr_python"], ["torch", "audio-separator"])
    runtimes = package_versions(config["uvr_python"], ["onnxruntime", "onnxruntime-gpu"])
    if not any(runtimes.values()):
        raise RuntimeError("Brak ONNX Runtime w środowisku UVR.")
    return config["uvr_model"] + " | audio-separator " + versions["audio-separator"]


def check_youtube(config, root):
    media_tool(config["node"], "node")
    access = preferences(root)
    if access["mode"] == "file" and not Path(access["cookie_file"]).is_file():
        raise ValueError("Wybrany plik cookies.txt nie istnieje. Otwórz Dostęp YouTube.")
    if access["mode"] == "managed":
        from .managed_browser import page_socket
        try:
            page_socket(access["profile"])
        except Exception:
            raise ValueError("Otwórz własne okno programu w Dostęp YouTube, zaloguj się i pozostaw otwartą kartę filmu. "
                             "To ustawienie nie blokuje kolejki lokalnej.") from None
    return "Ustawienia dostępne. Dostęp do filmu potwierdź w Dostęp YouTube."


def youtube_test_matches(root):
    try:
        tested = read_json(Path(root) / "youtube-proba.json", {})
        return bool(tested.get("ok") and tested.get("access_signature") == access_fingerprint(preferences(root)))
    except (OSError, ValueError, AttributeError, SessionError):
        return False


def component_state(enabled, check):
    if not enabled:
        return {"status": OFF, "message": "Etap pomijany w obecnej konfiguracji."}
    try:
        return {"status": AVAILABLE, "message": check()}
    except Exception as exc:
        return {"status": ACTION, "message": str(exc)}


def passed_components(components, test):
    for name in CORE:
        if components[name]["status"] != AVAILABLE:
            continue
        components[name]["status"] = PASSED
        devices = test.get("devices", {}).get(name, [])
        if name in {"diarization", "uvr"} and devices:
            if name == "diarization":
                components[name]["message"] = "Community-1 dostępny."
            components[name]["message"] += " | Próba: " + device_text(devices)


def collect_readiness(config, root):
    checks = (("whisper", True, lambda: check_whisper(config)),
              ("tools", True, lambda: check_tools(config, root)),
              ("diarization", config.get("diarization"), lambda: check_diar(config)),
              ("uvr", config.get("uvr_enabled"), lambda: check_uvr(config)),
              ("youtube", True, lambda: check_youtube(config, root)))
    components = {name: component_state(enabled, check) for name, enabled, check in checks}
    test = read_json(Path(root) / "pierwsza-proba.json", {})
    fingerprint, versions = None, {}
    if all(components[name]["status"] != ACTION for name in CORE):
        try:
            versions = environment_versions(config)
            fingerprint = configuration_fingerprint(config, versions)
        except Exception as exc:
            components["tools"] = {"status": ACTION, "message": str(exc)}
    passed = bool(test.get("passed") and fingerprint and test.get("fingerprint") == fingerprint)
    if passed:
        passed_components(components, test)
    if components["youtube"]["status"] == AVAILABLE and youtube_test_matches(root):
        tested = read_json(Path(root) / "youtube-proba.json", {})
        components["youtube"] = {"status": PASSED, "message": "Sprawdzono dostęp do jednego linku: " +
                                 test_time(tested.get("at")) + ". Kolejka nadal wymaga ręcznego Start."}
    return {"components": components, "passed": passed, "fingerprint": fingerprint,
            "versions": versions, "test": test}


def readiness_summary(result, kind):
    components = result.get("components", {})
    required = (*CORE, "youtube") if kind == "youtube" else CORE
    for name in required:
        state = components.get(name, {"status": CHECKING, "message": ""})
        if state["status"] in {ACTION, CHECKING}:
            return False, state["status"] + ": " + state["message"]
    if not result.get("passed"):
        return False, "Gotowy do próby. Kliknij Sprawdź na fragmencie dla aktualnych ustawień."
    message = "Próba zaliczona. Gotowy do ręcznego Start. | Test: " + test_time(result["test"].get("at", ""))
    if kind == "youtube":
        message += " | Dostęp do linku sprawdzisz w Dostęp YouTube."
    return True, message


def check_destination(folder, reserve_gb=2):
    path = local_path(folder)
    parent = path
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    if parent == parent.parent and parent != path:
        raise ValueError("Folder wyników nie istnieje; dostępny jest tylko katalog główny dysku: " +
                         str(path) + ". Sprawdź lokalizację nagrania i wybierz istniejący folder wyników.")
    if not parent.is_dir():
        raise ValueError("Folder wyników nie jest dostępny: " + str(path))
    check_disk(parent, reserve_gb=reserve_gb)
    probe_path = parent / (".kolejka-zapis-" + uuid.uuid4().hex + ".tmp")
    created = False
    try:
        try:
            descriptor = os.open(probe_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            created = True
            try:
                payload = b"write-access"
                if os.write(descriptor, payload) != len(payload):
                    raise OSError("Nie udało się zapisać całego pliku próbnego.")
            finally:
                os.close(descriptor)
        finally:
            if created:
                probe_path.unlink(missing_ok=True)
    except OSError as exc:
        raise OSError("Nie można zapisać wyników w folderze: " + str(path) +
                      ". Sprawdź dostęp do " + str(parent) + ": " + str(exc)) from exc
