"""Durable contiguous vocal pieces and one bounded recovery pass per session."""
import importlib.metadata
import os

from .asr import checkpoint, valid_checkpoint
from .common import ResourceError, atomic_json, blocks, check_disk, digest, signature
from .failures import JobError, resource_failure
from .paths import child_path
from .uvr_timeline import UVR_RATE, decode_clock, prepare_clock_block

RECOVERY_SECONDS = 60


class VocalRecovery:
    def __init__(self, request, api):
        self.request, self.api, self.config = request, api, request["config"]
        self.source, self.work, self.output = api.request_paths(request)
        self.directory = child_path(self.work, "uvr")
        self.directory.mkdir(parents=True, exist_ok=True)
        self.output.parent.mkdir(parents=True, exist_ok=True)
        self.pcm, self.clock = decode_clock(self.source, self.config, request["track"], self.directory, api.emit)
        self.key = signature({"request": request["signature"], "timeline": self.clock["signature"], "version": 2})
        self.ledger_path = child_path(self.directory, "ledger.json")
        saved = valid_checkpoint(self.ledger_path, self.key) or {}
        self.parts = self.valid_prefix(saved.get("parts", []))
        self.recovering = bool(saved.get("recovering") or request.get("recovery_only"))
        self.engine = None
        self.failure = saved.get("failure", {})
        if not self.parts:
            self.adopt_legacy()

    @property
    def cursor(self):
        return self.parts[-1]["end_frame"] if self.parts else 0

    def valid_prefix(self, parts):
        import soundfile as sf
        valid, cursor = [], 0
        for part in parts:
            try:
                path = child_path(self.directory, part["file"])
                info = sf.info(path)
                correct = (part["start_frame"] == cursor and part["end_frame"] > cursor and
                           part["end_frame"] <= self.clock["frames"] and info.samplerate == UVR_RATE and
                           info.channels == 2 and info.frames == part["end_frame"] - cursor and digest(path) == part["sha256"])
            except (OSError, RuntimeError, KeyError, ValueError, TypeError):
                correct = False
            if not correct:
                break
            valid.append(part)
            cursor = part["end_frame"]
        return valid

    def adopt_legacy(self):
        import soundfile as sf
        if self.clock["origin"] or self.clock["audio_start"]:
            return
        for block in blocks(self.clock["duration"], self.config["uvr_seconds"], self.config["uvr_context"]):
            key = signature({"request": self.request["signature"], "block": block})
            saved = valid_checkpoint(child_path(self.directory, f"{block['index']:06}.json"), key)
            path = child_path(self.directory, f"{block['index']:06}.flac")
            if not saved or not path.is_file() or digest(path) != saved.get("sha256"):
                break
            start, end = round(block["core_start"] * UVR_RATE), round(block["core_end"] * UVR_RATE)
            try:
                info = sf.info(path)
            except (RuntimeError, OSError):
                break
            if info.frames != end - start or info.channels != 2 or info.samplerate != UVR_RATE:
                break
            self.parts.append({**saved, "start_frame": start, "end_frame": end, "file": path.name})
        self.save()

    def save(self):
        checkpoint(self.ledger_path, {"signature": self.key, "parts": self.parts,
                                     "recovering": self.recovering, "failure": self.failure, "clock": self.clock})

    def next_block(self, seconds):
        start = self.cursor / UVR_RATE
        end = min(self.clock["duration"], start + seconds)
        return {"index": len(self.parts), "core_start": start, "core_end": end,
                "start": max(0, start - self.config["uvr_context"]),
                "end": min(self.clock["duration"], end + self.config["uvr_context"])}

    def generated(self, block):
        import numpy as np
        import soundfile as sf
        stage = child_path(self.directory, "biezacy")
        stage.mkdir(exist_ok=True)
        prepared = prepare_clock_block(self.pcm, self.clock, stage, block)
        if round(block["start"] * UVR_RATE) >= self.clock["audio_frames"]:
            return sf.read(prepared, dtype="float32", always_2d=True)[0]
        if self.engine is None:
            try:
                self.engine = self.api.separator(self.config, stage)
            except Exception as error:
                raise ResourceError("Nie można uruchomić modelu UVR: " + str(error)) from error
        self.engine, output = self.api.separate_vocal(self.engine, self.config, stage, prepared)
        samples, rate = sf.read(output, dtype="float32", always_2d=True)
        if rate != UVR_RATE or samples.shape[1] != 2 or not np.isfinite(samples).all():
            raise JobError("UVR zwrócił niepoprawne próbki wokalu.", "uvr_invalid_audio")
        return samples

    def append(self, samples, start, end, *, complete):
        import soundfile as sf
        name = f"piece-{start:014}-{end:014}.flac"
        path = child_path(self.directory, name)
        temporary = path.with_suffix(".partial")
        sf.write(temporary, samples, UVR_RATE, format="FLAC", subtype="PCM_24")
        with temporary.open("r+b") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        self.parts.append({"file": name, "start_frame": start, "end_frame": end,
                           "sha256": digest(path), "complete_block": complete,
                           "device": str(self.engine.torch_device) if self.engine is not None else "silence"})
        self.save()

    def process_block(self, block):
        import numpy as np
        start, end = round(block["core_start"] * UVR_RATE), round(block["core_end"] * UVR_RATE)
        left = start - round(block["start"] * UVR_RATE)
        samples = self.generated(block)
        available = max(0, min(end - start, len(samples) - left))
        source_needed = max(0, min(end, self.clock["audio_frames"]) - start)
        if available >= source_needed:
            core = samples[left:left + available]
            if available < end - start:
                core = np.pad(core, ((0, end - start - available), (0, 0)))
            self.append(core, start, end, complete=True)
            return
        # Retain an interior prefix and repeat its edge with the usual context.
        available = max(0, available - round(self.config["uvr_context"] * UVR_RATE))
        if available:
            self.append(samples[left:left + available], start, start + available, complete=False)
        raise JobError("UVR skrócił wokal względem wejścia. Zachowano poprawny początek.", "uvr_truncated")

    def pass_until_failure(self, seconds):
        while self.cursor < self.clock["frames"]:
            block = self.next_block(seconds)
            self.api.emit("UVR: " + ("odzyskiwanie końcówki" if self.recovering else "separacja") +
                          f" od {block['core_start']:.3f} s", self.cursor / self.clock["frames"] * 100)
            try:
                self.process_block(block)
            except Exception as error:
                if resource_failure(error):
                    raise
                self.failure = {"code": getattr(error, "code", "uvr_failed"), "message": str(error)[:2000],
                                "at_seconds": self.cursor / UVR_RATE}
                self.save()
                return False
        return True

    def join(self, complete):
        import soundfile as sf
        part_key = signature(self.parts)[:12]
        output = self.output if complete else self.output.with_name(f"wokal_czesciowy-{self.cursor}-{part_key}.flac")
        temporary = output.with_suffix(".partial")
        with sf.SoundFile(temporary, "w", samplerate=UVR_RATE, channels=2, format="FLAC", subtype="PCM_24") as writer:
            for part in self.parts:
                with sf.SoundFile(child_path(self.directory, part["file"])) as reader:
                    for samples in reader.blocks(blocksize=UVR_RATE * 30, dtype="float32", always_2d=True):
                        writer.write(samples)
        with temporary.open("r+b") as stream:
            os.fsync(stream.fileno())
        if sf.info(temporary).frames != self.cursor:
            raise JobError("Niekompletne połączenie fragmentów wokalu.", "uvr_join_failed")
        os.replace(temporary, output)
        return output

    def run(self):
        check_disk(self.output.parent, self.clock["frames"] * 12, self.config["min_free_gb"])
        complete = self.pass_until_failure(RECOVERY_SECONDS if self.recovering else self.config["uvr_seconds"])
        if not complete and not self.recovering:
            self.recovering = True
            self.save()
            self.api.emit("UVR: jedna dodatkowa próba brakującej części w blokach 60 s")
            complete = self.pass_until_failure(RECOVERY_SECONDS)
        if not self.cursor:
            raise JobError(self.failure.get("message", "Brak poprawnego fragmentu wokalu."), self.failure.get("code", "uvr_failed"))
        output = self.join(complete)
        result = {"signature": self.request["signature"], "sha256": digest(output), "model": self.config["uvr_model"],
                  "version": importlib.metadata.version("audio-separator"), "duration": self.clock["duration"],
                  "processed_until": self.cursor / UVR_RATE, "complete": complete, "blocks": self.parts,
                  "output": str(output), "failure": {} if complete else self.failure, "timeline": self.clock}
        atomic_json(self.work / "uvr-result.json", result)
        self.api.emit("UVR: wokal gotowy" if complete else "UVR: zachowano częściowy wokal; transkrybuję poprawną część", 100)
        return result
