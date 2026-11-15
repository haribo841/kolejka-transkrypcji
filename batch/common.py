from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

from .paths import child_path, input_file, local_path, output_file, within_root

APP = Path(__file__).resolve().parent.parent


def default_registry(app):
    value = os.environ.get("KOLEJKA_ROOT")
    installation = Path(app) / "lokalna-instalacja.json"
    if not value and installation.is_file():
        data = json.loads(installation.read_text(encoding="utf-8"))
        value = data.get("root")
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Niepoprawny folder danych w lokalna-instalacja.json.")
    return local_path(value) if value else Path.home() / "Transkrypcje"


DEFAULT_ROOT = default_registry(APP)
RATE = 16000
MODEL_REVISION = "0250c28d68c7c10d6b5cb39707e876c0c66ab6f8"
DIAR_REVISION = "3533c8cf8e369892e6b79ff1bf80f7b0286a54ee"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
CONSOLE_PYTHON = "python.exe"


def application_python():
    """Use this application's interpreter, including when launched by pythonw."""
    interpreter = Path(sys.executable)
    console = interpreter.with_name(CONSOLE_PYTHON)
    if os.name == "nt" and interpreter.stem.lower() == "pythonw" and console.is_file():
        return str(console)
    return str(interpreter)


def external_tool(name, fallback):
    found = shutil.which(name)
    if found:
        return found
    return fallback if Path(fallback).is_file() else name


def defaults():
    return {
        "model": str(APP / "models" / "whisper-large-v3-turbo-fp16-ov"),
        "model_revision": MODEL_REVISION,
        "diar_model": str(DEFAULT_ROOT / "modele" / "community-1"),
        "diar_revision": DIAR_REVISION,
        "diar_python": str(APP / ".venv-diarization" / "Scripts" / CONSOLE_PYTHON),
        "ffmpeg": external_tool("ffmpeg", r"C:\ffmpeg\bin\ffmpeg.exe"),
        "ffprobe": external_tool("ffprobe", r"C:\ffmpeg\bin\ffprobe.exe"),
        "node": external_tool("node", r"C:\Program Files\nodejs\node.exe"),
        "asr_seconds": 600, "asr_context": 5,
        "diar_seconds": 1800, "diar_context": 10,
        "min_free_gb": 2, "diarization": True,
        "omit_single_speaker_labels": True,
        "language": "auto", "audio_track": 0,
        "device": "CPU", "hotwords": "",
        "uvr_enabled": False, "keep_audio": False,
        "uvr_python": str(APP / ".venv-uvr" / "Scripts" / CONSOLE_PYTHON),
        "uvr_model_dir": str(DEFAULT_ROOT / "modele" / "uvr"),
        "uvr_model": "UVR-MDX-NET-Voc_FT.onnx", "uvr_seconds": 300, "uvr_context": 3,
    }


def read_json(path, fallback=None):
    try:
        path = local_path(path)
        if path.suffix.casefold() != ".json":
            raise ValueError("Oczekiwano lokalnego pliku JSON.")
        return json.loads(input_file(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return fallback


def atomic_bytes(path, data):
    path = output_file(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".zapis-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def atomic_json(path, value):
    atomic_bytes(path, json.dumps(value, ensure_ascii=False, indent=2,
                                 allow_nan=False).encode("utf-8"))


def atomic_new_bytes(path, data):
    """Publish a complete new file without replacing an existing destination."""
    path = output_file(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".zapis-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name == "nt":
            os.rename(temporary, path)  # Windows refuses to replace a target.
        else:
            os.link(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def digest(path):
    with input_file(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def signature(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False).encode()).hexdigest()


def settings(root):
    root = local_path(root)
    root.mkdir(parents=True, exist_ok=True)
    path = root / "ustawienia.json"
    config = defaults()
    config["diar_model"] = str(root / "modele" / "community-1")
    config["uvr_model_dir"] = str(root / "modele" / "uvr")
    config.update(read_json(path, {}))
    if not path.exists():
        atomic_json(path, config)
    return config


def slug(text, limit=65):
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", text).strip(" .")[:limit]
    return text or "nagranie"


def job_folder(root, job):
    # The path is persisted at first import, so title changes cannot orphan work.
    base = local_path(job["output_root"]) if job.get("output_root") else Path(root) / "wyniki"
    return child_path(base, job["folder"])


def remove_work_file(path, work):
    path = within_root(path, work)
    if path.is_file():
        path.unlink()


def check_disk(root, required=0, reserve_gb=2):
    free = shutil.disk_usage(root).free
    if free < required + reserve_gb * 1024 ** 3:
        raise ResourceError("Za mało miejsca na dysku. Zwolnij miejsce i wznów kolejkę.")


def windows_sleep_guard():
    @contextlib.contextmanager
    def guard():
        if os.name == "nt":
            import ctypes
            if not ctypes.windll.kernel32.SetThreadExecutionState(0x80000001):
                raise OSError("Nie udało się zablokować automatycznego uśpienia.")
        try:
            yield
        finally:
            if os.name == "nt":
                ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
    return guard()


class ResourceError(RuntimeError):
    """A missing model or system resource must pause the whole queue."""


class WorkerLock:
    """Kernel lock: process exit releases it, stale files do not block restart."""
    def __init__(self, root):
        self.path = Path(root) / "worker.lock"
        self.stream = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open("a+b")
        self.stream.seek(0)
        if os.fstat(self.stream.fileno()).st_size == 0:
            self.stream.write(b"0")
            self.stream.flush()
        self.stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            self.stream.close()
            self.stream = None
            return False

    def close(self):
        if self.stream:
            self.stream.close()
            self.stream = None

    @classmethod
    def busy(cls, root):
        lock = cls(root)
        obtained = lock.acquire()
        lock.close()
        return not obtained


def blocks(duration, size, context):
    if size <= 0 or context < 0 or context >= size / 2:
        raise ValueError("Niepoprawna długość bloków lub kontekstu.")
    for index in range(math.ceil(duration / size)):
        start, end = index * size, min(duration, (index + 1) * size)
        yield {"index": index, "core_start": start, "core_end": end,
               "start": max(0, start - context), "end": min(duration, end + context)}


def timestamp(seconds, separator="."):
    value = max(0, round(seconds * 1000))
    hours, value = divmod(value, 3600000)
    minutes, value = divmod(value, 60000)
    seconds, ms = divmod(value, 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02}{separator}{ms:03}"
