"""Detached localization sessions with frozen IDs, durable cursor and shared compute lock."""
import argparse
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import subprocess
import threading
import time
import uuid

from ..common import APP, NO_WINDOW, ResourceError, WorkerLock, application_python, atomic_json, windows_sleep_guard
from ..store import now
from . import options, video_id
from .api import AuthError, QuotaError, YouTube, sync_catalog
from .prepare import prepare_one
from .publish import publish_one
from .store import LocalizationStore


def session_lock(root):
    return WorkerLock(Path(root) / "lokalizacje")


def recover_session(store):
    control = store.session()
    if control["state"] not in {"starting", "running"}:
        return control
    if WorkerLock.busy(store.root / "lokalizacje") or WorkerLock.busy(store.root / "lokalizacje" / "uruchamianie"):
        return control
    try:
        age = abs((datetime.now(timezone.utc) - datetime.fromisoformat(control["heartbeat"])).total_seconds())
    except (ValueError, TypeError):
        age = 60
    if age > 30:
        return store.session(state="paused", stop=1, pid=None,
            message="Wykonawca zakończył pracę bez potwierdzenia. Postęp zachowano. Wybierz Wznów zapisaną sesję lub sprawdź lokalizacje/worker.log.")
    return control


def release_gate(process, gate, root):
    try:
        store = LocalizationStore(root)
        deadline = time.monotonic() + 15
        while True:
            state = store.session()
            if state["state"] != "starting":
                return
            if process.poll() is not None:
                store.session(state="paused", stop=1, message="Proces lokalizacji nie wystartował. Otwórz lokalizacje/worker.log.")
                return
            if time.monotonic() >= deadline:
                store.session(state="paused", stop=1, message="Uruchamianie lokalizacji trwa zbyt długo. Sprawdź proces i log przed wznowieniem.")
                return
            time.sleep(.05)
    finally:
        gate.close()


def spawn(root, request):
    gate = WorkerLock(Path(root) / "lokalizacje" / "uruchamianie")
    if not gate.acquire():
        raise RuntimeError("Lokalizacje właśnie startują. Poczekaj na potwierdzenie.")
    try:
        with (Path(root) / "lokalizacje" / "worker.log").open("ab") as log:
            process = subprocess.Popen([application_python(), "-m", "batch.localization.worker", "--root", str(root), "--generation", request["generation"]],
                cwd=APP, stdout=log, stderr=log, creationflags=NO_WINDOW | getattr(subprocess, "DETACHED_PROCESS", 0),
                env=dict(os.environ, PYTHONIOENCODING="utf-8"), shell=False)
        threading.Thread(target=release_gate, args=(process, gate, root), daemon=True).start()
        return process
    except Exception:
        gate.close()
        raise


def launch(root, mode, ids, tasks=None, *, frozen=None):
    ids = list(dict.fromkeys(video_id(i) for i in ids))
    if mode not in {"prepare", "publish", "sync", "public_sync"} or (not ids and mode != "sync"):
        raise ValueError("Wybierz filmy i prawidłowy rodzaj sesji.")
    root = Path(root).resolve()
    lock = session_lock(root)
    if not lock.acquire():
        raise RuntimeError("Sesja lokalizacji już działa. Użyj Dokończ bieżący i zatrzymaj.")
    try:
        store = LocalizationStore(root)
        if mode == "prepare" and WorkerLock.busy(root):
            raise RuntimeError("Trwa transkrypcja lub próba modeli. Dokończ ją przed przygotowaniem lokalizacji.")
        if WorkerLock.busy(root / "lokalizacje" / "uruchamianie"):
            raise RuntimeError("Poprzednia sesja właśnie się uruchamia.")
        request = {"mode": mode, "ids": ids, "tasks": tasks or [], "frozen": frozen or {}, "generation": uuid.uuid4().hex}
        path = root / "lokalizacje" / "sesja-request.json"
        atomic_json(path, request)
        store.session(mode=mode, state="starting", ids=json.dumps(ids), tasks=json.dumps(request), cursor=0,
                      current_id="", stop=0, message="Uruchamianie sesji lokalizacji", heartbeat=now())
        return spawn(root, request)
    finally:
        lock.close()


def resume(root):
    root = Path(root)
    if WorkerLock.busy(root / "lokalizacje") or WorkerLock.busy(root / "lokalizacje" / "uruchamianie"):
        raise RuntimeError("Sesja lokalizacji już działa.")
    store = LocalizationStore(root)
    state = store.session()
    request = json.loads(state["tasks"])
    if not request or (request["mode"] != "sync" and state["cursor"] >= len(request["ids"])):
        raise ValueError("Zapisana sesja jest ukończona. Zaznacz następne filmy.")
    store.session(stop=0, state="starting", message="Wznawianie zapisanej sesji")
    return spawn(root, request)


class Runner:
    def __init__(self, store, *, api=None, prepare=prepare_one, publish=publish_one):
        self.store, self.api = store, api
        self.prepare, self.publish = prepare, publish
        self.last = 0

    def progress(self, message):
        if time.monotonic() - self.last > .4:
            self.store.session(message=message, heartbeat=now())
            self.last = time.monotonic()

    def heartbeat(self, stop):
        while not stop.wait(3):
            try:
                self.store.session(heartbeat=now())
            except Exception:
                logging.exception("Heartbeat lokalizacji")

    def prepare_environment(self, request, compute):
        if not compute.acquire():
            raise RuntimeError("Zasoby zajmuje kolejka transkrypcji. Wznów lokalizacje po jej zatrzymaniu.")
        if {"metadata", "captions"} & set(request["tasks"]):
            from .translation import ready
            ready(self.store.root)
            if not Path(options(self.store.root)["translation_python"]).is_file():
                raise ResourceError("Brak środowiska tłumaczeń. Otwórz kreator lokalizacji.")

    def finish_session(self, owns, compute, stop, heartbeat):
        stop.set()
        if heartbeat.is_alive():
            heartbeat.join(timeout=5)
        if compute is not None:
            compute.close()
        if owns:
            state = self.store.session()["state"]
            self.store.session(state="paused" if state == "paused" else "idle", pid=None, current_id="", stop=1, heartbeat=now())

    def run(self, generation=None):
        lock, compute = session_lock(self.store.root), None
        if not lock.acquire():
            return
        stop = threading.Event()
        owns = False
        heartbeat = threading.Thread(target=self.heartbeat, args=(stop,), daemon=True)
        try:
            request = json.loads(self.store.session()["tasks"])
            if not isinstance(request, dict) or not request or (generation and generation != request.get("generation")):
                return
            mode, ids = request["mode"], request["ids"]
            owns = True
            if mode == "prepare":
                compute = WorkerLock(self.store.root)
                self.prepare_environment(request, compute)
            self.store.session(state="running", pid=os.getpid(), heartbeat=now())
            heartbeat.start()
            if mode in {"publish", "sync"} and self.api is None:
                self.api = YouTube(self.store)
            if mode == "sync":
                sync_catalog(self.store, self.api, ids, self.progress)
                self.store.session(cursor=len(ids), message="Odświeżanie zakończone. Korekty i kolejka transkrypcji zachowane.")
            else:
                self.consume(request)
        except (RuntimeError, OSError, ValueError) as error:
            logging.exception("Sesja lokalizacji wstrzymana")
            self.store.session(state="paused", message=str(error)[:1500], stop=1)
        finally:
            try:
                self.finish_session(owns, compute, stop, heartbeat)
            finally:
                lock.close()

    def process_video(self, request, ident):
        if request["mode"] == "public_sync":
            from .metadata import refresh_public
            refresh_public(self.store, ident, self.progress)
        elif request["mode"] == "prepare":
            errors = self.prepare(self.store, ident, request["tasks"], self.progress)
            if errors:
                self.store.session(message="Zapisano postęp; sprawdź szczegóły filmu: " + "; ".join(errors)[:1000])
        else:
            self.publish(self.store, self.api, ident, request["frozen"].get(ident, {}), self.progress)

    def record_video_error(self, request, ident, error):
        logging.exception("Problem filmu %s", ident)
        if request["mode"] != "public_sync":
            for item in self.store.items(ident):
                if item["review"] == "approved" and item["publication"] != "verified":
                    self.store.error(ident, item["kind"], item["lang"], str(error))
        self.store.session(message="Film wymaga sprawdzenia: " + str(error)[:1000])

    def consume(self, request):
        ids = request["ids"]
        with windows_sleep_guard():
            for index in range(self.store.session()["cursor"], len(ids)):
                if self.store.session()["stop"]:
                    break
                ident = ids[index]
                self.store.session(current_id=ident, cursor=index, message=f"{index + 1}/{len(ids)}: {self.store.get_video(ident)['title']}")
                try:
                    self.process_video(request, ident)
                except (AuthError, QuotaError, ResourceError):
                    raise
                except Exception as error:
                    self.record_video_error(request, ident, error)
                self.store.session(cursor=index + 1)
        remaining = len(ids) - self.store.session()["cursor"]
        self.store.session(message=f"Sesja zakończona. Pozostało {remaining} filmów w zapisanym zakresie. Zmiany widoku obowiązują od następnego Start.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--generation")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    Runner(LocalizationStore(args.root)).run(args.generation)


if __name__ == "__main__":
    main()
