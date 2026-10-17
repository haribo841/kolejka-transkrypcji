"""Explicit, verified path repair after files and their results were moved externally."""
from __future__ import annotations

import json

from .common import WorkerLock, digest, job_folder, read_json
from .local_outputs import OWNER_FILE
from .paths import input_file, local_path
from .result_paths import assert_output_owner
from .store import BEGIN_WRITE, now


def verify_result_location(store, old, root):
    changed = {**old, "output_root": root}
    previous, destination = job_folder(store.root, old), job_folder(store.root, changed)
    if destination == previous:
        return
    if previous.exists():
        raise ValueError("Dotychczasowe wyniki nadal istnieją; nie zmieniono lokalizacji: " + str(previous))
    if destination.exists():
        assert_output_owner(destination, old["identity"])
        markers = [read_json(destination / name, {}) for name in
                   (OWNER_FILE, "gotowe.json", "czesciowe.json", "transkrypcja.json")]
        if not any(marker.get("identity") == old["identity"] for marker in markers):
            raise ValueError("Brak potwierdzenia właściciela przeniesionych wyników: " + str(destination))
    elif old["attempts"] or old["processed_until"] or old["status"] in {"done", "partial"}:
        raise ValueError("Nie znaleziono przeniesionych wyników lub punktów wznowienia: " + str(destination))
    if not destination.parent.is_dir():
        raise ValueError("Folder wyników nie istnieje: " + str(destination.parent))


def relocate_local(store, entries, progress=lambda message: None):
    """Validate an explicit ID/path map, then atomically change paths, never processing state.

    Call in a background operation. Back up the registry before calling this
    administrative operation. Source bytes and output ownership are checked;
    files themselves are never moved, rewritten or deleted.
    """
    gate, worker = WorkerLock(store.root / "uruchamianie"), WorkerLock(store.root)
    try:
        if not gate.acquire() or not worker.acquire():
            raise ValueError("Najpierw zatrzymaj sesję i poczekaj na zakończenie bieżącego nagrania.")
        control = store.control()
        if control["state"] != "idle" or not control["stop"]:
            raise ValueError("Relokacja wymaga zatrzymanej kolejki.")
        prepared, seen = [], set()
        for entry in entries:
            old = store.get(entry["id"])
            if not old or old["kind"] != "local" or old["id"] in seen:
                raise ValueError("Niepoprawne lub powtórzone ID nagrania w mapie relokacji.")
            seen.add(old["id"])
            if old["source"] != entry["old_source"] or old["output_root"] != entry["old_output_root"]:
                raise ValueError("Lokalizacja nagrania zmieniła się po przygotowaniu mapy.")
            progress(f"Sprawdzanie nagrania {len(prepared) + 1}: {old['title']}")
            source = input_file(entry["new_source"])
            stamp = source.stat()
            if digest(source) != old["identity"].partition(":")[2]:
                raise ValueError("Zawartość przeniesionego nagrania nie zgadza się z SHA-256: " + str(source))
            if (stamp.st_size, stamp.st_mtime_ns) != (source.stat().st_size, source.stat().st_mtime_ns):
                raise ValueError("Nagranie zmieniło się podczas sprawdzania: " + str(source))
            root = str(local_path(entry["new_output_root"])) if entry["new_output_root"] is not None else None
            verify_result_location(store, old, root)
            metadata = json.loads(old["source_meta"])
            if "new_intended_output_root" in entry:
                metadata["intended_output_root"] = str(local_path(entry["new_intended_output_root"]))
            prepared.append((old, str(source), root, json.dumps(metadata, ensure_ascii=False), stamp))
        with store.connect() as db:
            db.execute(BEGIN_WRITE)
            for old, source, root, metadata, stamp in prepared:
                current = dict(db.execute("SELECT * FROM jobs WHERE id=?", (old["id"],)).fetchone())
                if current != old:
                    raise ValueError("Nagranie zmieniło się podczas relokacji; operacja została wycofana.")
                db.execute("UPDATE jobs SET source=?,output_root=?,source_meta=?,updated=? WHERE id=?",
                           (source, root, metadata, now(), old["id"]))
                db.execute("INSERT OR REPLACE INTO local_cache VALUES(?,?,?,?)",
                           (source, stamp.st_size, stamp.st_mtime_ns, old["identity"].partition(":")[2]))
        return len(prepared)
    finally:
        worker.close()
        gate.close()
