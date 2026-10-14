"""Decode a selected audio track once and preserve its sample positions on video time."""
import math
import os
import subprocess

from .asr import checkpoint, valid_checkpoint
from .common import NO_WINDOW, ResourceError, check_disk, digest, signature
from .failures import JobError, resource_failure
from .media import probe, positive_duration
from .paths import child_path, media_tool
from .processes import ChildGuard

UVR_RATE = 44100


def time_value(value, default=0):
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def tagged_end(stream):
    try:
        hours, minutes, seconds = stream.get("tags", {})["DURATION"].split(":")
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    except (KeyError, AttributeError, ValueError, TypeError):
        return None


def clock_metadata(source, config, track, data=None):
    data = probe(source, config) if data is None else data
    audio = [s for s in data.get("streams", []) if s.get("codec_type") == "audio"]
    if not 0 <= track < len(audio):
        raise JobError("Wybrana ścieżka audio nie istnieje.", "audio_track_missing")
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    origin = time_value((video or audio[track]).get("start_time"), time_value(data.get("format", {}).get("start_time")))
    duration = None
    if video:
        end = tagged_end(video)
        duration = (positive_duration(video.get("duration")) or positive_duration(end - origin if end else None) or
                    positive_duration(data.get("format", {}).get("duration")))
    return {"origin": origin, "audio_start": time_value(audio[track].get("start_time"), origin),
            "video_duration": duration, "has_video": video is not None}


def clock_filters(metadata, rate):
    return (f"asetpts=PTS-({metadata['origin']})/TB,aresample={rate}:async=1:first_pts=0:"
            "min_hard_comp=0:max_soft_comp=0:rematrix_maxval=1.0")


def decode_clock(source, config, track, directory, emit):
    metadata = clock_metadata(source, config, track)
    key = signature({"source": digest(source), "track": track, "clock": metadata, "decoder": 2, "rate": UVR_RATE})
    path, marker = child_path(directory, "audio.f32"), child_path(directory, "audio-clock.json")
    saved = valid_checkpoint(marker, key)
    if saved and path.is_file() and path.stat().st_size == saved["audio_frames"] * 8 and digest(path) == saved["sha256"]:
        return path, saved
    estimate = metadata["video_duration"] or 0
    check_disk(directory, estimate * UVR_RATE * 8, config["min_free_gb"])
    temporary = path.with_suffix(".partial")
    # Hard padding/trimming follows timestamps; soft stretching remains disabled.
    filters = clock_filters(metadata, UVR_RATE)
    command = [media_tool(config["ffmpeg"], "ffmpeg"), "-nostdin", "-v", "error", "-xerror", "-y",
               "-copyts", "-protocol_whitelist", "file,pipe", "-i", str(source), "-map", f"0:a:{track}",
               "-vn", "-sn", "-dn", "-ac", "2", "-af", filters, "-ar", str(UVR_RATE),
               "-c:a", "pcm_f32le", "-f", "f32le", "-progress", "pipe:1", str(temporary)]
    execute_decoder(command, directory, config, emit)
    size = temporary.stat().st_size
    if not size or size % 8:
        raise JobError("Brak poprawnych próbek wybranej ścieżki audio.", "audio_decode_failed")
    os.replace(temporary, path)
    audio_frames = size // 8
    frames = max(audio_frames, round((metadata["video_duration"] or 0) * UVR_RATE))
    saved = {**metadata, "signature": key, "sha256": digest(path), "audio_frames": audio_frames,
             "frames": frames, "duration": frames / UVR_RATE, "rate": UVR_RATE}
    checkpoint(marker, saved)
    return path, saved


def execute_decoder(command, directory, config, emit):
    error_file = child_path(directory, "timeline-ffmpeg.log")
    with error_file.open("wb") as errors:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=errors, text=True,
                                   encoding="utf-8", creationflags=NO_WINDOW, shell=False)
        with ChildGuard(process):
            try:
                for line in process.stdout:
                    if line.startswith("out_time_us="):
                        emit("UVR: sprawdzanie osi czasu wybranej ścieżki audio")
                        check_disk(directory, reserve_gb=config["min_free_gb"])
                code = process.wait()
            finally:
                process.stdout.close()
    if code:
        message = error_file.read_text(encoding="utf-8", errors="replace")[-2000:]
        if resource_failure(RuntimeError(message)):
            raise ResourceError("FFmpeg: " + message)
        raise JobError("Nie udało się odczytać ścieżki audio: " + message, "audio_decode_failed")


def prepare_clock_block(pcm, clock, stage, block):
    import numpy as np
    import soundfile as sf
    start = round(block["start"] * UVR_RATE)
    count = round(block["end"] * UVR_RATE) - start
    available = max(0, min(count, clock["audio_frames"] - start))
    samples = np.zeros((count, 2), dtype=np.float32)
    if available:
        source = np.memmap(pcm, dtype="<f4", mode="r", offset=start * 8, shape=(available, 2))
        try:
            samples[:available] = source
        finally:
            source._mmap.close()
    if not np.isfinite(samples).all():
        raise JobError("Źródłowe audio zawiera niepoprawne próbki.", "audio_invalid")
    path = child_path(stage, "input.wav")
    sf.write(path, samples, UVR_RATE, subtype="FLOAT")
    return path
