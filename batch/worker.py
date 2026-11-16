from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import threading
import time

from .asr import ASR
from .common import (APP, NO_WINDOW, ResourceError, WorkerLock, application_python, check_disk,
                     digest, job_folder, read_json, remove_work_file, settings, signature,
                     windows_sleep_guard)
from .diarization import run_diarization, validate_diarization
from .exporter import export, verify_outputs
from .result_paths import RESULT_MANIFEST, assert_output_owner, reserve_output
from .media import EmptyAudio, MediaError, decode, download, media_metadata
from .output_fallback import ensure_output
from .retranscription import (attempt_work, invalidate_captions, pending_request, previous_results,
                             retry_config, retry_input, validate_request)
from .outcomes import permanent_audio_failure
from .speakers import assign_words, cues_from_words, merge_speakers
from .store import Store, in_scope, now, scope_kind
from .failures import JobError, resource_failure
from .uvr import archive_source, separate, validate_uvr
from .youtube_access import preferences as youtube_preferences


def resolve_tools(config, kind):
    names = ("ffmpeg", "ffprobe") if kind == "local" else ("ffmpeg", "ffprobe", "node")
    for name in names:
        candidate = str(config[name])
        if not Path(candidate).is_file() and Path(candidate).name == candidate:
            candidate = shutil.which(candidate) or candidate
        if not Path(candidate).is_file():
            raise ResourceError(f"Brak programu {name}: {config[name]}")
        config[name] = candidate


def preflight(config, root, kind="all"):
    kind = scope_kind(kind)
    from transcribe import validate_model
    try:
        validate_model(Path(config["model"]))
    except Exception as exc:
        raise ResourceError("Model Whisper nie jest gotowy: " + str(exc)) from exc
    resolve_tools(config, kind)
    if config["diarization"]:
        validate_diarization(config)
    if config.get("uvr_enabled"):
        validate_uvr(config)
    check_disk(root, reserve_gb=config["min_free_gb"])
    for size, overlap in ((config["asr_seconds"], config["asr_context"]), (config["diar_seconds"], config["diar_context"])):
        if size <= 0 or overlap < 0 or overlap >= size / 2:
            raise ResourceError("Niepoprawne ustawienia długości bloków.")


def cleanup(work, job):
    work = Path(work)
    if (read_json(work.parent / RESULT_MANIFEST) or {}).get("preserve_work"):
        return
    # Never delete source paths. Only owned intermediate files in this job folder.
    for name in ("audio.f32le", "audio.f32le.partial"):
        remove_work_file(work / name, work)
    if job["kind"] == "youtube":
        for path in work.glob("download.*"):
            if path.suffix != ".json":
                remove_work_file(path, work)
    # Once the complete vocal file and exports are verified, per-block FLACs
    # are disposable. Their manifests remain as processing evidence.
    for path in (work / "uvr").glob("*.flac"):
        remove_work_file(path, work)
    remove_work_file(work / "uvr" / "audio.f32", work)
    for path in (work / "uvr" / "biezacy").glob("*.wav"):
        remove_work_file(path, work)


def audio_completion(work, duration, uvr_report):
    if uvr_report:
        recovery = uvr_report.get("timeline", {}).get("recovery")
        complete = uvr_report.get("processing_complete", uvr_report.get("complete", True))
        return max(duration, uvr_report.get("duration", duration)), bool(complete), recovery
    decoded = read_json(work / "pcm.json") or {}
    recovery = decoded.get("recovery")
    return max(duration, decoded.get("source_duration", duration)), True, recovery


def export_stage(request, warning):
    if request:
        return "Aktualizacja napisów po nowej transkrypcji"
    return "Gotowe: odzyskane audio" if warning else "Gotowe"



class Worker:
    def __init__(self, root, processor=None):
        self.root = Path(root)
        self.store = Store(root)
        self.config = settings(root)
        self.asr = ASR(self.config, root)
        self.processor = processor or self.process
        self.last_update = 0
        self.current = None

    def progress(self, stage, percent):
        current = time.monotonic()
        if current - self.last_update < .5 and percent < 100:
            return
        self.last_update = current
        if self.current:
            self.store.update(self.current["id"], stage=stage, progress=float(percent))

    def process(self, job):
        request = pending_request(job)
        if request:
            self.process_retry(job, request)
            return
        config = self.config
        source = self.local_source(job) if job["kind"] == "local" else None
        folder = ensure_output(self.store, job)
        assert_output_owner(folder, job["identity"])
        reserve_output(folder, job["identity"])
        check_disk(folder, reserve_gb=config["min_free_gb"])
        work = folder / "robocze"
        work.mkdir(parents=True, exist_ok=True)
        execution = signature({"identity": job["identity"], "language": job["language"],
                               "track": job["audio_track"], "config": config, "format": 1})
        if verify_outputs(folder, job["identity"], execution):
            # Handles a crash after export but before the SQLite commit / cleanup.
            manifest = read_json(folder / RESULT_MANIFEST) or {}
            warning = manifest.get("terminal_reason") or {}
            self.store.update(job["id"], status="done", stage="Gotowe: odzyskane audio" if warning else "Gotowe",
                              progress=100, error=warning.get("message", ""), error_code=warning.get("code", ""),
                              processed_until=manifest.get("processed_until", job.get("processed_until", 0)))
            cleanup(work, job)
            return
        if job["kind"] == "youtube":
            source = download(job, work, config, self.progress, youtube_preferences(self.root))
        try:
            self.process_audio(job, source, folder, work, execution)
        except EmptyAudio as exc:
            self.finish_empty(job, source, folder, work, execution, exc)

    def process_retry(self, job, request):
        """One explicit attempt, resumable within its own fresh checkpoint set."""
        config, asr = self.config, self.asr
        localization_lock = WorkerLock(self.root / "lokalizacje")
        try:
            if not localization_lock.acquire():
                raise ResourceError("Lokalizacje YouTube pracują. Zatrzymaj sesję przed ponowną transkrypcją.")
            if WorkerLock.busy(self.root / "lokalizacje" / "uruchamianie"):
                raise ResourceError("Trwa uruchamianie lokalizacji. Poczekaj na zakończenie sesji.")
            self.config = retry_config(config, request)
            self.asr = ASR(self.config, self.root)
            preflight(self.config, self.root, kind=job["kind"] if request["input"] == "source" else "local")
            folder = ensure_output(self.store, job)
            reserve_output(folder, job["identity"])
            check_disk(folder, reserve_gb=self.config["min_free_gb"])
            work = attempt_work(folder, request)
            manifest, previous = previous_results(folder, work, job["identity"])
            execution = signature({"identity": job["identity"], "retry": request, "config": self.config})
            if not verify_outputs(folder, job["identity"], execution):
                self.retry_audio(job, request, folder, work, execution, manifest, previous)
            if verify_outputs(folder, job["identity"], execution):
                invalidate_captions(self.root, job, request, work)
                current = read_json(folder / RESULT_MANIFEST, {})
                warning = current.get("terminal_reason") or {}
                self.store.update(job["id"], status="done", stage="Gotowe: nowa transkrypcja", progress=100,
                                  processed_until=current["processed_until"], error=warning.get("message", ""),
                                  error_code=warning.get("code", ""), retry_request=json.dumps({**request, "state": "completed"}))
        finally:
            self.config, self.asr = config, asr
            localization_lock.close()

    def retry_audio(self, job, request, folder, work, execution, manifest, previous):
        self.progress("Przygotowanie nowej transkrypcji", 0)
        source = retry_input(self.root, job, request, manifest)
        track = 0
        if source is None:
            track = job["audio_track"]
            source = (self.local_source(job) if job["kind"] == "local" else
                      download(job, work, self.config, self.progress, youtube_preferences(self.root)))
        prepared = {"retained": list(manifest.get("audio_files", {})), "track": track,
                    "uvr": previous.get("uvr") if request["input"] == "vocal" else None}
        retry_job = {**job, "language": request["language"]}
        try:
            self.process_audio(retry_job, source, folder, work, execution, prepared=prepared)
        except EmptyAudio as exc:
            self.finish_empty(retry_job, source, folder, work, execution, exc, retained=prepared["retained"])

    def process_audio(self, job, source, folder, work, execution, *, prepared=None):
        config = self.config
        track = job["audio_track"] if prepared is None else prepared["track"]
        self.validate_audio({**job, "audio_track": track}, source)
        retained = [] if prepared is None else prepared["retained"]
        uvr_report = None if prepared is None else prepared["uvr"]
        if prepared is None and (config.get("keep_audio") or config.get("uvr_enabled")):
            self.progress("Zachowywanie źródłowej ścieżki audio", 0)
            archived = archive_source(source, folder, job["audio_track"], config)
            retained.append(str(archived.relative_to(folder)))
        if prepared is None and config.get("uvr_enabled"):
            source, uvr_report = separate(source, work, folder, job["audio_track"], config, self.progress,
                                          recovery_only=job.get("processed_until", 0) > 0)
            retained.append(str(source.relative_to(folder)))
        pcm, duration, pcm_key = decode(source, work, config, 0 if config.get("uvr_enabled") else track, self.progress)
        full_duration, complete, recovery = audio_completion(work, duration, uvr_report)
        self.store.update(job["id"], duration=full_duration)
        asr = self.asr.run(pcm, duration, pcm_key, job, work, self.progress)
        if config["diarization"] and asr["words"]:
            diar_parts = run_diarization(pcm, duration, pcm_key, work, config, self.progress)
            self.progress("Łączenie mówców i przypisywanie słów", 0)
            diar = merge_speakers(diar_parts)
            words = assign_words(asr["words"], diar)
        else:
            diar_parts, diar = [], {"enabled": False, "turns": [], "exclusive": [], "speakers": []}
            words = [{**word, "speaker": "Mówca nieustalony", "overlap": []} for word in asr["words"]]
        report = {"schema": 1, "identity": job["identity"], "title": job["title"],
                  "source": job["source"], "source_metadata": json.loads(job["source_meta"]),
                  "created": now(), "duration": full_duration, "processed_until": duration, "language": job["language"],
                  "sample_rate": 16000, "audio_track": job["audio_track"],
                  "settings": config, "asr": asr, "diarization": diar, "uvr": uvr_report,
                  "audio_recovery": recovery,
                  "processing_complete": complete, "source_complete": not bool(recovery),
                  "coverage_unknown": bool(recovery),
                  "retained_audio": retained,
                  "diarization_blocks": diar_parts, "words": words, "segments": cues_from_words(words),
                  "notes": ["Etykiety mówców są automatyczne i dotyczą wyłącznie tego nagrania.",
                            "Łączenie słów i mówców na granicach bloków jest heurystyczne.",
                            "Automatyczny język może być zawodny w krótkich i mieszanych językowo fragmentach."],
                  "review_warnings": [{"start": w["start"], "end": w["end"], "text": w["text"],
                                       "reason": "Słowo o nietypowo długim przedziale; sprawdź timestamp."}
                                      for w in words if w["end"] - w["start"] > 5]}
        if not words:
            report["no_speech_reason"] = "no_recognized_speech"
        self.finish(job, folder, work, report, execution, complete)
        if complete and not words and not recovery:
            self.store.update(job["id"], stage="Gotowe: brak wypowiedzi")

    def finish_empty(self, job, source, folder, work, execution, empty, *, retained=None):
        duration = empty.duration or job.get("duration") or 0.0
        retained = [] if retained is None else retained
        if empty.reason != "no_audio_stream" and (self.config.get("keep_audio") or self.config.get("uvr_enabled")):
            archived = archive_source(source, folder, job["audio_track"], self.config)
            retained.append(str(archived.relative_to(folder)))
        self.store.update(job["id"], duration=duration)
        report = {"schema": 1, "identity": job["identity"], "title": job["title"], "source": job["source"],
                  "source_metadata": json.loads(job["source_meta"]), "created": now(), "duration": duration,
                  "processed_until": duration, "language": job["language"], "sample_rate": 16000,
                  "audio_track": job["audio_track"], "settings": self.config, "words": [], "segments": [],
                  "asr": {"words": [], "blocks": []}, "diarization_blocks": [],
                  "diarization": {"enabled": False, "turns": [], "exclusive": [], "speakers": []},
                  "uvr": None, "retained_audio": retained, "no_speech_reason": empty.reason,
                  "notes": ["Nagranie poprawnie przetworzone; brak próbek audio lub wypowiedzi."]}
        self.finish(job, folder, work, report, execution, True)
        self.store.update(job["id"], stage="Gotowe: brak wypowiedzi")

    def local_source(self, job):
        source = Path(job["source"])
        if not source.is_file():
            raise MediaError("Nie znaleziono pliku lokalnego: " + str(source) +
                             ". Dodaj folder z obecną lokalizacją nagrania; zachowamy jego postęp.")
        self.progress("Sprawdzanie oryginalnego pliku", 0)
        if digest(source) != job["identity"].split(":", 1)[1]:
            raise MediaError("Zawartość pliku lokalnego zmieniła się. Dodaj plik ponownie jako nowe nagranie.")
        return source

    def finish(self, job, folder, work, report, execution, complete):
        request = pending_request(job)
        if request:
            report["retranscription"] = request
        duration, full_duration = report["processed_until"], report["duration"]
        self.progress("Atomowy zapis i sprawdzanie TXT, SRT, VTT, JSON", 0)
        if not complete:
            failure = (report.get("uvr") or {}).get("failure") or report.get("audio_recovery") or {}
            report["incomplete_reason"] = failure
            report["coverage_unknown"] = bool(failure.get("coverage_unknown"))
            report["missing_intervals"] = [{"start": duration, "end": full_duration}] if duration < full_duration else []
            export(folder, report, execution, complete=False)
            self.store.record_failure(job["id"], failure.get("message", "UVR: pozostała brakująca część."),
                                      failure.get("code", "uvr_partial"), partial=True, processed_until=duration)
            self.store.update(job["id"], progress=duration / full_duration * 100)
            self.save_error_log(job, failure.get("message", "Niepełny wokal UVR."))
            return
        warning = report.get("audio_recovery") or {}
        if warning:
            report.update(source_complete=False, coverage_unknown=True, retry_recommended=False,
                          preserve_work=True, terminal_reason=warning)
        export(folder, report, execution)
        self.store.update(job["id"], status="running" if request else "done",
                          stage=export_stage(request, warning),
                          progress=100, error=warning.get("message", ""), error_code=warning.get("code", ""), processed_until=duration)
        try:
            cleanup(work, job)
        except OSError as exc:
            # Outputs are valid. Keep evidence of cleanup failure, retry next start.
            self.store.update(job["id"], error="Wyniki gotowe; sprzątanie wymaga ponowienia: " + str(exc))

    def save_error_log(self, job, message):
        destination = job_folder(self.root, job)
        owned = destination.is_dir()
        if owned:
            try:
                assert_output_owner(destination, job["identity"])
            except (OSError, ValueError):
                owned = False
        folder = destination / "robocze" if owned else self.root / "bledy"
        folder.mkdir(parents=True, exist_ok=True)
        from .common import atomic_bytes
        name = "blad.txt" if owned else f"nagranie-{job['id']}.txt"
        atomic_bytes(folder / name, (now() + "\n" + message + "\n").encode("utf-8"))

    def failure(self, job, error, code, *, stop=False, status=None):
        logging.exception("Błąd nagrania %s", job["id"])
        self.store.record_failure(job["id"], str(error)[:2500], code)
        if status is not None:
            self.store.update(job["id"], status=status, stage="Zatrzymano etap")
        try:
            self.save_error_log(job, str(error))
        except OSError:
            logging.exception("Nie udało się zapisać logu nagrania %s", job["id"])
        if stop:
            self.store.control(stop=1, message=str(error))

    def validate_audio(self, job, source):
        metadata = media_metadata(source, self.config)
        if metadata.get("empty_audio"):
            raise EmptyAudio(metadata["duration"], metadata["empty_audio"])
        if not 0 <= job["audio_track"] < len(metadata["audio_tracks"]):
            raise MediaError("Wybrana ścieżka audio nie istnieje. Wybierz ścieżkę audio w oknie kolejki.")
        if metadata["duration"] is not None:
            self.store.update(job["id"], duration=metadata["duration"])

    def execution_lock(self, held_lock):
        if held_lock is not None:
            if held_lock.stream is None or held_lock.path.resolve() != (self.root / "worker.lock").resolve():
                raise ValueError("Niepoprawna przekazana blokada wykonawcy.")
            return held_lock
        lock = WorkerLock(self.root)
        return lock if lock.acquire() else None

    def heartbeat(self, stopped):
        while not stopped.wait(5):
            try:
                self.store.control(heartbeat=now())
            except sqlite3.OperationalError as exc:
                # Imports and queue updates can temporarily hold SQLite's write
                # lock. A delayed heartbeat must not kill the heartbeat thread.
                code = getattr(exc, "sqlite_errorcode", 0)
                if (code & 255) not in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED} and "locked" not in str(exc).casefold():
                    raise
                logging.warning("Nie zapisano sygnału wykonawcy; ponowię przy następnej próbie: %s", exc)

    def cleanup_completed(self, scope):
        for job in self.store.jobs():
            if job["status"] != "done" or not in_scope(job, scope):
                continue
            folder = job_folder(self.root, job)
            work = folder / "robocze"
            leftovers = (work / "audio.f32le").exists() or any(p.suffix != ".json" for p in work.glob("download.*"))
            if leftovers and verify_outputs(folder, job["identity"]):
                cleanup(work, job)

    def process_one(self, job):
        self.current = job
        self.last_update = 0
        self.store.update(job["id"], attempts=job["attempts"] + 1)
        try:
            self.processor(job)
        except MediaError as exc:
            if not self.skip_unreadable(job, exc, "media_" + exc.status):
                self.failure(job, exc, "media_" + exc.status, stop=exc.status == "blocked", status=exc.status)
        except JobError as exc:
            if not self.skip_unreadable(job, exc, exc.code):
                self.failure(job, exc, exc.code)
        except ResourceError as exc:
            self.failure(job, exc, "resource_unavailable", stop=True)
        except Exception as exc:
            self.failure(job, exc, "processing_failed", stop=resource_failure(exc))
        finally:
            self.store.control(current_id=None)
            self.current = None

    def skip_unreadable(self, job, error, code):
        if pending_request(job) or job["kind"] != "local" or not permanent_audio_failure(error, code):
            return False
        # A replaced/moved source is recoverable by reimport and must not be
        # dismissed using the diagnosis from a different file version.
        try:
            if digest(Path(job["source"])) != job["identity"].split(":", 1)[1]:
                return False
        except OSError:
            return False
        self.store.skip(job["id"], str(error), code)
        try:
            self.save_error_log(job, str(error))
        except OSError:
            logging.exception("Nie udało się zapisać powodu pominięcia %s", job["id"])
        return True

    def consume(self, max_jobs):
        processed = 0
        attempted = set()
        with windows_sleep_guard():
            while max_jobs is None or processed < max_jobs:
                job = self.store.claim(excluded=attempted)
                if job is None:
                    break
                attempted.add(job["id"])
                self.process_one(job)
                processed += 1

    def run(self, check=True, max_jobs=None, *, _held_lock=None):
        lock = self.execution_lock(_held_lock)
        if lock is None:
            return False
        heartbeat_stop = threading.Event()
        thread = threading.Thread(target=self.heartbeat, args=(heartbeat_stop,), daemon=True)
        try:
            self.store.control(state="running", pid=os.getpid(), heartbeat=now(), message="")
            self.store.recover()
            scope = self.store.control()
            if check:
                pending = [job for job in self.store.jobs() if in_scope(job, scope) and job["status"] == "pending"]
                if not pending or any(not pending_request(job) for job in pending):
                    preflight(self.config, self.root, kind=scope["scope_kind"])
            # Retry only cleanup of verified completed jobs, never inference.
            self.cleanup_completed(scope)
            thread.start()
            self.consume(max_jobs)
        except Exception as exc:
            logging.exception("Wykonawca został wstrzymany")
            self.store.control(stop=1, message=str(exc))
        finally:
            heartbeat_stop.set()
            if thread.is_alive():
                thread.join(timeout=6)
            self.store.control(state="idle", stop=1, pid=None, heartbeat=now(), current_id=None)
            lock.close()
        return True


def _finish_launch(root, process, launch_lock):
    """Keep the launch gate until the child owns worker.lock, without blocking Tk."""
    try:
        limit = time.monotonic() + 10
        while not WorkerLock.busy(root):
            code = process.poll()
            if code is not None:
                if code:
                    Store(root).control(stop=1, message="Proces kolejki nie uruchomił się. Sprawdź worker.log i wznów ręcznie.")
                return
            if time.monotonic() >= limit:
                process.terminate()
                process.wait(timeout=5)
                Store(root).control(stop=1, message="Uruchamianie wykonawcy przekroczyło czas oczekiwania. Wznów kolejkę ręcznie.")
                return
            time.sleep(.02)
    finally:
        launch_lock.close()


def wait_for_launch(root, timeout=10):
    """Keep a short-lived CLI parent alive until startup has been acknowledged."""
    limit = time.monotonic() + timeout
    while WorkerLock.busy(Path(root) / "uruchamianie"):
        if time.monotonic() >= limit:
            raise ResourceError("Wykonawca nie potwierdził uruchomienia. Sprawdź worker.log przed kolejnym Start.")
        time.sleep(.02)


def enqueue_retry(store, kind, job_ids, order_ids, scope_mode, request):
    validate_request(request)
    if not job_ids or len(job_ids) != 1 or scope_mode != "selected" or list(order_ids or []) != list(job_ids):
        raise ValueError("Ponowna transkrypcja obejmuje dokładnie jedno zaznaczone nagranie.")
    if any(WorkerLock.busy(store.root / name) for name in ("konfiguracja", "lokalizacje", "lokalizacje/uruchamianie")):
        raise ResourceError("Zatrzymaj konfigurację i sesję lokalizacji przed ponowną transkrypcją.")
    selected = store.get(job_ids[0])
    if selected is None or selected["kind"] != kind:
        raise ValueError("Nagranie nie należy do wybranej kolejki.")
    store.request_retranscription(job_ids[0], request)


def launch(root, *, kind="all", job_ids=None, order_ids=None, scope_mode="legacy", retry_request=None):
    root = Path(root).resolve()
    kind = scope_kind(kind)
    launch_lock = WorkerLock(root / "uruchamianie")
    if not launch_lock.acquire():
        raise ResourceError("Wykonawca jest już uruchamiany. Poczekaj, aż pojawi się jego stan.")
    worker_lock = WorkerLock(root)
    started, store = False, None
    try:
        # Acquire before writing the scope: a live worker's stop request and
        # current selection must never be replaced by another Start click.
        if not worker_lock.acquire():
            raise ResourceError("Kolejka już pracuje. Dokończ bieżące nagranie i zatrzymaj ją przed zmianą rodzaju lub zakresu.")
        store = Store(root)
        if retry_request is not None:
            enqueue_retry(store, kind, job_ids, order_ids, scope_mode, retry_request)
        store.start(kind=kind, job_ids=job_ids, order_ids=order_ids, scope_mode=scope_mode)
        started = True
        log_path = root / "worker.log"
        with log_path.open("ab", buffering=0) as log:
            process = subprocess.Popen([application_python(), str(APP / "kolejka.py"), "--root", str(root),
                "worker", "--wait-for-launch"], cwd=APP, stdout=log, stderr=log,
                stdin=subprocess.DEVNULL, creationflags=NO_WINDOW | getattr(subprocess, "DETACHED_PROCESS", 0),
                close_fds=True)
    except Exception:
        if started:
            store.control(stop=1, message="Nie udało się uruchomić wykonawcy. Popraw przyczynę i wznów ręcznie.")
        launch_lock.close()
        raise
    finally:
        worker_lock.close()
    code = process.poll()
    if code is not None:
        if code:
            store.control(stop=1, message="Proces kolejki nie uruchomił się. Sprawdź worker.log i wznów ręcznie.")
        launch_lock.close()
    else:
        threading.Thread(target=_finish_launch, args=(root, process, launch_lock), daemon=True).start()
    return process
