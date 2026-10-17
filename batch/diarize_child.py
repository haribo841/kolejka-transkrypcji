"""Run only with the isolated pyannote Python environment."""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import sys
import time
import traceback

# Set before importing any pyannote/Hugging Face modules.
os.environ["PYANNOTE_METRICS_ENABLED"] = "0"
os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["DO_NOT_TRACK"] = "1"
os.environ["OTEL_SDK_DISABLED"] = "true"

from .asr import checkpoint, pcm_samples, pcm_block_digest, cached_audio_block
from .common import RATE, ResourceError, atomic_json, blocks, digest, read_json, signature
from .failures import resource_failure


def emit(message, percent=0):
    print(json.dumps({"message": message, "percent": percent}, ensure_ascii=False), flush=True)


def gpu_probe():
    import torch
    if not torch.cuda.is_available():
        return {"device": "cpu", "message": "CUDA niedostępna. Diarizacja będzie korzystać z CPU."}
    try:
        torch.set_num_threads(max(1, min(8, os.cpu_count() or 4)))
        layer = torch.nn.Conv1d(1, 8, 5).to("cuda")
        with torch.inference_mode():
            result = layer(torch.randn(1, 1, RATE, device="cuda"))
            result.square().mean().item()
            torch.fft.rfft(torch.randn(1, 1024, device="cuda"))
            torch.cuda.synchronize()
        return {"device": "cuda", "message": "GPU: " + torch.cuda.get_device_name(0),
                "capability": list(torch.cuda.get_device_capability(0)), "torch": torch.__version__}
    except Exception as exc:
        return {"device": "cpu", "message": "Test CUDA nieudany, używam CPU: " + str(exc)[:500]}


def load_pipeline(model):
    from pyannote.audio import Pipeline
    pipeline = Pipeline.from_pretrained(str(model), token=False)
    if pipeline is None:
        raise RuntimeError("Nie można załadować lokalnego modelu Community-1.")
    # Read the instantiated model parameters, never guess its threshold.
    threshold = float(pipeline.parameters(instantiated=True)["clustering"]["threshold"])
    return pipeline, threshold


def prepare_pipeline(model, pcm, work):
    import numpy as np
    import torch
    try:
        pipeline, threshold = load_pipeline(model)
    except Exception as error:
        raise ResourceError("Nie można uruchomić modelu mówców: " + str(error)) from error
    device = gpu_probe()
    pipeline.to(torch.device(device["device"]))
    if device["device"] == "cuda":
        try:
            sample = np.fromfile(pcm, dtype="<f4", count=RATE * 5)
            sample = np.pad(sample, (0, max(0, RATE - len(sample))))
            test = torch.from_numpy(sample).unsqueeze(0)
            pipeline({"waveform": test, "sample_rate": RATE})
            torch.cuda.synchronize()
        except Exception as exc:
            pipeline.to(torch.device("cpu"))
            torch.cuda.empty_cache()
            device = {"device": "cpu", "message": "Model nie przeszedł próby GPU; CPU: " + str(exc)[:500]}
    emit(device["message"])
    atomic_json(work / "diar-device.json", device)
    return pipeline, threshold, device


def progress_hook(message, index, block_count):
    def hook(step_name, step_artifact, file=None, total=None, completed=None, **kwargs):
        fraction = completed / total if total and completed is not None else 0
        emit(f"{message}: {step_name}", (index + fraction) / block_count * 100)
    return hook


def annotation_turns(annotation, block):
    return [{"start": block["start"] + float(turn.start),
             "end": min(block["end"], block["start"] + float(turn.end)), "speaker": str(label)}
            for turn, _, label in annotation.itertracks(yield_label=True)
            if 0 <= float(turn.start) < min(float(turn.end), block["end"] - block["start"])]


def speaker_embeddings(result):
    import numpy as np
    embeddings = {}
    for index, label in enumerate(result.speaker_diarization.labels()):
        vector = result.speaker_embeddings[index] if result.speaker_embeddings is not None else None
        embeddings[str(label)] = vector.tolist() if vector is not None and np.isfinite(vector).all() else None
    return embeddings


class Diarizer:
    def __init__(self, request):
        self.request = request
        self.config = request["config"]
        self.model, self.work = Path(self.config["diar_model"]), Path(request["work"])
        self.versions = {name: importlib.metadata.version(name) for name in ("pyannote.audio", "torch", "torchaudio")}
        parameters = {"revision": self.config["diar_revision"],
                      "config": digest(self.model / "config.yaml"), "versions": self.versions, "version": 1}
        self.legacy_base = signature({"pcm": request["pcm_key"], **parameters})
        self.base = signature({**parameters, "version": 2})
        self.pipeline, self.device, self.threshold = None, None, None

    def inference(self, waveform, hook):
        import torch
        with torch.inference_mode():
            try:
                return self.pipeline({"waveform": waveform, "sample_rate": RATE}, hook=hook)
            except torch.cuda.OutOfMemoryError:
                self.pipeline.to(torch.device("cpu"))
                torch.cuda.empty_cache()
                self.device = {"device": "cpu", "message": "Za mało pamięci GPU. Dalsza diarizacja na CPU."}
                emit(self.device["message"])
                atomic_json(self.work / "diar-device.json", self.device)
                return self.pipeline({"waveform": waveform, "sample_rate": RATE}, hook=hook)

    def block_result(self, block, key, count):
        import numpy as np
        import torch
        if self.pipeline is None:
            emit("Ładowanie lokalnego modelu mówców")
            self.pipeline, self.threshold, self.device = prepare_pipeline(self.model, self.request["pcm"], self.work)
        started = time.perf_counter()
        message = f"Mówcy: blok {block['index'] + 1}/{count} ({self.device['device']})"
        hook = progress_hook(message, block["index"], count)
        with pcm_samples(self.request["pcm"], block) as audio:
            samples = np.array(audio, copy=True)
        samples = np.pad(samples, (0, max(0, RATE - len(samples))))
        waveform = torch.from_numpy(samples).unsqueeze(0)
        result = self.inference(waveform, hook)
        return {"signature": key, "block": block, "turns": annotation_turns(result.speaker_diarization, block),
                "exclusive": annotation_turns(result.exclusive_speaker_diarization, block),
                "embeddings": speaker_embeddings(result), "threshold": self.threshold,
                "device": self.device, "versions": self.versions, "seconds": time.perf_counter() - started}


def run(request):
    diarizer = Diarizer(request)
    config = diarizer.config
    jobs = list(blocks(request["duration"], config["diar_seconds"], config["diar_context"]))
    output = []
    for block in jobs:
        audio_hash = pcm_block_digest(request["pcm"], block)
        key = signature({"base": diarizer.base, "block": block, "audio": audio_hash})
        path = diarizer.work / "diar" / f"{block['index']:06}.json"
        part = cached_audio_block(path, key, signature({"base": diarizer.legacy_base, "block": block}), audio_hash)
        if part is None:
            part = diarizer.block_result(block, key, len(jobs))
            part["audio_sha256"] = audio_hash
            checkpoint(path, part)
        output.append(part)
        emit(f"Mówcy: zapisano {block['index'] + 1}/{len(jobs)} bloków", (block["index"] + 1) / len(jobs) * 100)
    checkpoint(diarizer.work / "diar-result.json", {"signature": request["signature"], "blocks": output})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("request", nargs="?")
    parser.add_argument("--probe", action="store_true")
    args = parser.parse_args()
    if args.probe:
        from pyannote.audio import Pipeline  # Verify dependencies as well as CUDA.
        print(json.dumps(gpu_probe(), ensure_ascii=False))
    else:
        run(read_json(args.request))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"error": str(exc), "scope": "session" if resource_failure(exc) else "job"}, ensure_ascii=False), flush=True)
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)
