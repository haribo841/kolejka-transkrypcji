from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

from .asr import valid_checkpoint
from .common import APP, NO_WINDOW, ResourceError, atomic_json, read_json, signature
from .failures import JobError


def validate_diarization(config):
    python = Path(config["diar_python"])
    model = Path(config["diar_model"])
    if not python.is_file():
        raise ResourceError("Brak środowiska pyannote. Uruchom Instaluj-zaleznosci.ps1.")
    required = ["config.yaml", "embedding/pytorch_model.bin", "segmentation/pytorch_model.bin",
                "plda/plda.npz", "plda/xvec_transform.npz"]
    missing = [name for name in required if not (model / name).is_file() or not (model / name).stat().st_size]
    if missing:
        raise ResourceError("Model mówców nie jest gotowy. Otwórz Konfiguracja mówców i pobierz Community-1.")


def run_diarization(pcm, duration, pcm_key, work, config, progress):
    validate_diarization(config)
    work = Path(work)
    request = {"pcm": str(pcm), "duration": duration, "pcm_key": pcm_key,
               "work": str(work), "config": config}
    request["signature"] = signature(request)
    atomic_json(work / "diar-request.json", request)
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYANNOTE_METRICS_ENABLED="0",
               HF_HUB_DISABLE_TELEMETRY="1", HF_HUB_OFFLINE="1", OTEL_SDK_DISABLED="true")
    with (work / "pyannote.log").open("ab") as log:
        process = subprocess.Popen([config["diar_python"], "-m", "batch.diarize_child", str(work / "diar-request.json")],
            cwd=APP, env=env, stdout=subprocess.PIPE, stderr=log, text=True,
            encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
        from .processes import ChildGuard
        error, scope = "", "job"
        with ChildGuard(process):
            for line in process.stdout:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if "error" in event:
                    error = event["error"]
                    scope = event.get("scope", "job")
                else:
                    progress(event.get("message", "Mówcy"), event.get("percent", 0))
            code = process.wait()
            process.stdout.close()
    if code:
        message = "Diarizacja nie została ukończona: " + (error or f"kod {code}; zobacz pyannote.log")
        if scope == "session":
            raise ResourceError(message)
        raise JobError(message, "diarization_failed")
    result = valid_checkpoint(work / "diar-result.json", request["signature"])
    if result is None:
        raise JobError("Brak kompletnego, poprawnego wyniku diarizacji.", "diarization_result_invalid")
    return result["blocks"]
