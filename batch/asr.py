from __future__ import annotations

import importlib.metadata
from contextlib import contextmanager
import math
import hashlib
from pathlib import Path
import re
import time

from .common import RATE, ResourceError, atomic_json, blocks, read_json, signature


def valid_checkpoint(path, key):
    try:
        data = read_json(path)
        if data and data.get("signature") == key:
            expected = data.get("checksum")
            unsigned = {k: v for k, v in data.items() if k != "checksum"}
            if expected == signature(unsigned):
                return data
    except (ValueError, OSError, TypeError, AttributeError):
        pass
    return None


def checkpoint(path, data):
    atomic_json(path, {**data, "checksum": signature(data)})


def pcm_block_digest(pcm, block):
    left = round(block["start"] * RATE) * 4
    remaining = (round(block["end"] * RATE) - round(block["start"] * RATE)) * 4
    result = hashlib.sha256()
    with Path(pcm).open("rb") as reader:
        reader.seek(left)
        while remaining:
            data = reader.read(min(1024 * 1024, remaining))
            if not data:
                raise RuntimeError("Niekompletny blok PCM. Zachowano wcześniejsze punkty kontrolne.")
            result.update(data)
            remaining -= len(data)
    return result.hexdigest()


def cached_audio_block(path, key, legacy_key, audio_hash):
    part = valid_checkpoint(path, key)
    if part is None:
        part = valid_checkpoint(path, legacy_key)
        if part is not None:
            part = {**part, "signature": key, "audio_sha256": audio_hash}
            part.pop("checksum", None)
            checkpoint(path, part)
    return part


def word_key(text):
    return re.sub(r"\W+", "", text.casefold())


def trim_boundary(output, owned, boundary):
    tail = [w for w in output if w["end"] >= boundary - 2]
    head = [w for w in owned if w["start"] <= boundary + 2]
    for length in range(min(len(tail), len(head), 12), 0, -1):
        pairs = zip(tail[-length:], head[:length])
        if all(word_key(a["text"]) and word_key(a["text"]) == word_key(b["text"])
               and min(a["end"], b["end"]) > max(a["start"], b["start"])
               for a, b in pairs):
            return owned[length:]
    return owned


def recover_context(parts, output):
    for part in parts:
        block = part["block"]
        for word in part["words"]:
            midpoint = (word["start"] + word["end"]) / 2
            if block["core_start"] <= midpoint < block["core_end"] or word["end"] <= word["start"]:
                continue
            nearby = any(w["start"] < word["end"] and w["end"] > word["start"] for w in output)
            if not nearby:
                output.append({**word, "recovered_from_context": True})


@contextmanager
def pcm_samples(pcm, block):
    import numpy as np
    offset = round(block["start"] * RATE)
    count = round(block["end"] * RATE) - offset
    audio = np.memmap(pcm, dtype="<f4", mode="r", offset=offset * 4, shape=(count,))
    try:
        yield audio
    finally:
        audio._mmap.close()


def convert_word(word, block):
    start, end = float(word.start_ts), float(word.end_ts)
    if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end < start:
        raise RuntimeError("Whisper zwrócił niepełne timestampy. Blok wymaga ponowienia.")
    return {"start": min(block["end"], block["start"] + start),
            "end": min(block["end"], block["start"] + end), "text": word.word}


def convert_segment(chunk, block):
    return {"start": block["start"] + float(chunk.start_ts) if chunk.start_ts >= 0 else None,
            "end": block["start"] + float(chunk.end_ts) if chunk.end_ts >= 0 else None,
            "text": chunk.text}


def join_blocks(parts):
    """Midpoint ownership plus a time-constrained alignment at block boundaries.

    Matching overlap words get one canonical instance. Repeated phrases far apart
    in time are never removed. This is heuristic, so raw block hypotheses remain.
    """
    output = []
    for part in parts:
        block = part["block"]
        owned = [dict(word) for word in part["words"]
                 if block["core_start"] <= (word["start"] + word["end"]) / 2 < block["core_end"]]
        if output and owned:
            owned = trim_boundary(output, owned, block["core_start"])
        output.extend(owned)
    # Recover words recognized only in a neighbour's context when the owning
    # block has a gap. Keep conflicting hypotheses in the raw block report.
    recover_context(parts, output)
    return sorted(output, key=lambda item: (item["start"], item["end"]))


class ASR:
    def __init__(self, config, root):
        self.config, self.root, self.pipe = config, Path(root), None

    def load(self):
        if self.pipe is None:
            from transcribe import validate_model
            try:
                validate_model(Path(self.config["model"]))
                import openvino_genai as genai
                self.pipe = genai.WhisperPipeline(self.config["model"], self.config["device"],
                    CACHE_DIR=str(self.root / "cache" / "openvino"), word_timestamps=True)
            except Exception as exc:
                raise ResourceError("Nie można uruchomić modelu Whisper: " + str(exc)) from exc
        return self.pipe

    def infer_block(self, samples, block, generation, language):
        import numpy as np
        silent = float(np.max(np.abs(samples))) < 1e-7
        if silent:
            return "", [], [], True
        pipe = self.load()
        options = pipe.get_generation_config()
        if generation["language"] and generation["language"] not in options.lang_to_id:
            raise ValueError(f"Nieznany kod języka: {language}")
        for name, value in generation.items():
            setattr(options, name, value)
        result = pipe.generate(samples, generation_config=options)
        text = "".join(result.texts)
        words = [convert_word(word, block) for word in (result.words or [])]
        segments = [convert_segment(chunk, block) for chunk in (result.chunks or [])]
        if text.strip() and not words:
            raise RuntimeError("Whisper zwrócił tekst bez wymaganych timestampów słów.")
        return text, segments, words, False

    def run_block(self, pcm, block, key, generation, language, versions):
        with pcm_samples(pcm, block) as samples:
            started = time.perf_counter()
            text, segments, words, silent = self.infer_block(samples, block, generation, language)
            return {"signature": key, "block": block, "text": text, "words": words,
                    "segments": segments, "generation": generation, "versions": versions,
                    "seconds": time.perf_counter() - started, "digital_silence": silent}

    def run(self, pcm, duration, pcm_key, job, work, progress):
        config = self.config
        generation = {"task": "transcribe", "return_timestamps": True, "word_timestamps": True,
                      "language": None if job["language"] == "auto" else f"<|{job['language']}|>"}
        if config.get("hotwords"):
            generation["hotwords"] = config["hotwords"]
        versions = {name: importlib.metadata.version(name) for name in ("openvino", "openvino-genai", "numpy")}
        parameters = {"model": config["model"],
                          "revision": config["model_revision"], "generation": generation,
                          "versions": versions, "join_version": 2}
        legacy_base = signature({"pcm": pcm_key, **parameters})
        base = signature({**parameters, "join_version": 3})
        parts = []
        jobs = list(blocks(duration, config["asr_seconds"], config["asr_context"]))
        for block in jobs:
            audio_hash = pcm_block_digest(pcm, block)
            key = signature({"base": base, "block": block, "audio": audio_hash})
            path = Path(work) / "asr" / f"{block['index']:06}.json"
            part = cached_audio_block(path, key, signature({"base": legacy_base, "block": block}), audio_hash)
            if part is None:
                progress(f"Whisper: blok {block['index'] + 1}/{len(jobs)} (CPU)", block["index"] / len(jobs) * 100)
                part = self.run_block(pcm, block, key, generation, job["language"], versions)
                part["audio_sha256"] = audio_hash
                checkpoint(path, part)
            parts.append(part)
            progress(f"Whisper: zapisano {block['index'] + 1}/{len(jobs)} bloków", (block["index"] + 1) / len(jobs) * 100)
        return {"words": join_blocks(parts), "blocks": parts, "generation": generation,
                "versions": versions, "signature": base}
