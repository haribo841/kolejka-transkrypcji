"""Local UVR models via audio-separator, isolated from ASR and pyannote."""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import traceback

os.environ["DO_NOT_TRACK"] = "1"
from .common import (NO_WINDOW, WorkerLock, atomic_json, check_disk, digest,
                     read_json, settings)
from .paths import child_path, input_file, local_path, media_tool, output_file, within_root
from .processes import ChildGuard
from .failures import resource_failure


UVR_STATE_FILE = 'konfiguracja-uvr.json'

UVR_RATE = 44100
MODEL_ROUNDING_SECONDS = .05


def emit(message, percent=0):
    print(json.dumps({"message": message, "percent": percent}, ensure_ascii=False), flush=True)


def separator(config, output, online=False, force_cpu=False):
    directory = local_path(config["uvr_model_dir"])
    model_name = str(child_path(directory, config["uvr_model"]).relative_to(directory))
    import torch
    import onnxruntime as ort
    # Load matching CUDA/cuDNN DLLs from the isolated PyTorch installation.
    if hasattr(ort, "preload_dlls"):
        ort.preload_dlls()
    if not online:
        import requests
        def offline(*args, **kwargs):
            raise RuntimeError("UVR pracuje offline. Pobierz kompletny model przed uruchomieniem kolejki.")
        requests.sessions.Session.request = offline
    from audio_separator.separator import Separator
    engine = Separator(log_level=logging.WARNING, model_file_dir=str(directory),
        output_dir=str(output), output_format="WAV", output_single_stem="Vocals",
        use_soundfile=True, normalization_threshold=1.0, amplification_threshold=0.0,
        sample_rate=UVR_RATE, mdx_params={"hop_length": 1024, "segment_size": 256,
                                       "overlap": .25, "batch_size": 1, "enable_denoise": False})
    if force_cpu:
        engine.torch_device = torch.device("cpu")
        engine.onnx_execution_provider = ["CPUExecutionProvider"]
    engine.load_model(model_name)
    return engine


def setup(root):
    root = local_path(root)
    config = settings(root)
    directory = local_path(config["uvr_model_dir"])
    child_path(directory, config["uvr_model"])
    directory.mkdir(parents=True, exist_ok=True)
    lock = WorkerLock(directory)
    if not lock.acquire():
        return
    try:
        atomic_json(root / UVR_STATE_FILE, {"state": "running", "message": "Pobieranie i sprawdzanie modelu UVR"})
        separator(config, directory / "test", online=True)
        manifest = {"model": config["uvr_model"], "version": importlib.metadata.version("audio-separator"),
                    "files": {name: digest(child_path(directory, name)) for name in (config["uvr_model"],
                        "download_checks.json", "vr_model_data.json", "mdx_model_data.json")}}
        atomic_json(directory / "gotowe.json", manifest)
        atomic_json(root / UVR_STATE_FILE, {"state": "ready", "message": "Model UVR gotowy"})
    except Exception as exc:
        atomic_json(root / UVR_STATE_FILE, {"state": "error", "message": str(exc)[:1500]})
        raise
    finally:
        lock.close()


def request_paths(request):
    source = input_file(request["source"])
    work = local_path(request["work"])
    output = output_file(request["output"], suffix=".flac")
    if work.name != "robocze" or output != child_path(work.parent, "audio/wokal.flac"):
        raise ValueError("UVR może zapisywać tylko wokal w katalogu bieżącego nagrania.")
    if source == output or source.is_relative_to(child_path(work, "uvr")):
        raise ValueError("Źródło UVR nie może być jego plikiem pośrednim ani wynikowym.")
    return source, work, output


def generated_vocal(stage, filename):
    name = Path(filename)
    path = within_root(name, stage) if name.is_absolute() else child_path(stage, name)
    return input_file(path)


def prepare_input(source, config, track, stage, block):
    path = output_file(child_path(stage, "input.wav"))
    command = [media_tool(config["ffmpeg"], "ffmpeg"), "-nostdin", "-hide_banner", "-loglevel", "error", "-xerror", "-y",
        "-ss", str(block["start"]), "-protocol_whitelist", "file,pipe", "-i", str(source),
        "-t", str(block["end"] - block["start"]), "-map", f"0:a:{track}", "-vn", "-ac", "2", "-ar", str(UVR_RATE),
        "-c:a", "pcm_f32le", str(path)]
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=sys.stderr, creationflags=NO_WINDOW, shell=False)
    with ChildGuard(process):
        if process.wait():
            raise RuntimeError("FFmpeg nie przygotował bloku do UVR.")
    return path


def separate_vocal(engine, config, stage, source):
    try:
        filenames = engine.separate(str(source))
    except Exception as exc:
        if not any(term in str(exc).casefold() for term in ("cuda", "cudnn", "cublas", "gpu", "out of memory")):
            raise
        emit("UVR: próba GPU nieudana, ponawiam blok na CPU: " + str(exc)[:250])
        engine = separator(config, stage, force_cpu=True)
        filenames = engine.separate(str(source))
    if len(filenames) != 1:
        raise RuntimeError("UVR nie zwrócił dokładnie jednej ścieżki Vocals.")
    return engine, generated_vocal(stage, filenames[0])


def vocal_padding(samples, rate, required, prepared, final):
    """Distinguish missing model output from an audio track ending before video."""
    import soundfile as sf
    tolerance = round(rate * MODEL_ROUNDING_SECONDS)
    source_tail = 0
    if prepared is not None:
        original = sf.info(prepared)
        if original.samplerate != rate or original.channels != 2 or not original.frames:
            raise RuntimeError("Niepoprawne wejście bloku UVR.")
        source_tail = max(0, required - original.frames)
        if source_tail > tolerance and not final:
            raise RuntimeError("Ścieżka audio jest krótsza od planu bloku UVR. Sprawdź wybraną ścieżkę audio.")
    deficit = max(0, required - len(samples))
    if deficit > source_tail and (prepared is not None or deficit > tolerance):
        raise RuntimeError("UVR skrócił nagranie; nie można zachować timestampów.")
    return deficit


def write_vocal_block(generated, stem, block, *, prepared=None, final=False):
    import numpy as np
    import soundfile as sf
    samples, rate = sf.read(generated, dtype="float32", always_2d=True)
    if rate != UVR_RATE or samples.shape[1] != 2 or not np.isfinite(samples).all():
        raise RuntimeError("UVR zwrócił niepoprawne audio.")
    left = round(block["core_start"] * rate) - round(block["start"] * rate)
    expected = round(block["core_end"] * rate) - round(block["core_start"] * rate)
    deficit = vocal_padding(samples, rate, left + expected, prepared, final)
    if deficit:
        samples = np.pad(samples, ((0, deficit), (0, 0)))
    temporary = output_file(stem.with_suffix(".partial"))
    sf.write(temporary, samples[left:left + expected], rate, format="FLAC", subtype="PCM_24")
    with temporary.open("r+b") as stream:
        os.fsync(stream.fileno())
    os.replace(temporary, stem)
    return expected


def join_vocal_blocks(output, jobs, block_dir, duration):
    import soundfile as sf
    temporary = output_file(output.with_suffix(".partial"))
    with sf.SoundFile(temporary, "w", samplerate=UVR_RATE, channels=2, format="FLAC", subtype="PCM_24") as writer:
        for block in jobs:
            with sf.SoundFile(child_path(block_dir, f"{block['index']:06}.flac")) as reader:
                for samples in reader.blocks(blocksize=UVR_RATE * 30, dtype="float32", always_2d=True):
                    writer.write(samples)
    with temporary.open("r+b") as stream:
        os.fsync(stream.fileno())
    if sf.info(temporary).frames != round(duration * UVR_RATE):
        raise RuntimeError("Długość połączonego wokalu jest niepoprawna.")
    os.replace(temporary, output)


def run(request):
    from .uvr_recovery import VocalRecovery
    return VocalRecovery(request, sys.modules[__name__]).run()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("request", nargs="?")
    parser.add_argument("--setup", type=Path)
    args = parser.parse_args()
    if args.setup:
        setup(args.setup)
    else:
        run(read_json(args.request))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"error": str(exc), "code": getattr(exc, "code", "uvr_failed"),
                          "scope": "session" if resource_failure(exc) else "job"}, ensure_ascii=False), flush=True)
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)
