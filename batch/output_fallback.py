"""Short automatic destinations without discarding existing work or sources."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import uuid

from .common import ResourceError, digest, job_folder
from .local_outputs import compact_folder
from .path_budget import PATH_LIMIT, path_units
from .result_paths import assert_output_owner, minimum_export_leaf_units


def fits_destination(folder, identity):
    reserve = max(64, minimum_export_leaf_units(identity))
    return path_units(Path(folder).resolve()) + 1 + reserve <= PATH_LIMIT


def fallback_destination(registry, title, identity):
    registry = Path(registry).resolve()
    candidates = [registry / "wyniki", Path(registry.anchor) / "Transkrypcje" / "wyniki"]
    for root in candidates:
        try:
            name = compact_folder(title, identity, root)
        except ResourceError:
            continue
        return root, name
    raise ResourceError("Nie można wybrać krótkiego folderu wyników na tym dysku.")


def planned_output(registry, job):
    previous = job_folder(registry, job)
    metadata = json.loads(job["source_meta"])
    if not metadata.get("output_error") and fits_destination(previous, job["identity"]):
        return job.get("output_root"), job["folder"], previous, False
    root, name = fallback_destination(registry, job["title"], job["identity"])
    return str(root), name, root / name, True


def fallback_metadata(metadata, previous, destination):
    metadata = dict(metadata)
    metadata["output_fallback"] = {"requested": str(metadata.get("intended_output_root", previous)), "selected": str(destination),
                                   "reason": "Poprzednia ścieżka wyników była zbyt długa."}
    metadata.pop("output_error", None)
    metadata.pop("intended_output_root", None)
    return metadata


def work_files(previous):
    files = []
    for path in previous.rglob("*"):
        if path.is_symlink() or path.is_junction():
            raise ValueError("Katalog wyników zawiera dowiązanie; nie skopiowano plików poza katalogiem nagrania.")
        if path.is_file():
            files.append(path.relative_to(previous))
    return files


def preserve_work(previous, destination, identity):
    if previous == destination or not previous.exists():
        return
    assert_output_owner(previous, identity)
    files = work_files(previous)
    if destination.exists():
        assert_output_owner(destination, identity)
        if all((destination / leaf).is_file() and digest(previous / leaf) == digest(destination / leaf) for leaf in files):
            return
        raise FileExistsError("W krótszym folderze są inne wyniki. Zachowano oba katalogi: " + str(destination))
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.parent / (".przenoszenie-" + uuid.uuid4().hex[:12])
    try:
        shutil.copytree(previous, temporary)
        if any(digest(previous / leaf) != digest(temporary / leaf) for leaf in files):
            raise OSError("Kopia punktów wznowienia nie przeszła weryfikacji.")
        temporary.rename(destination)
    finally:
        if temporary.exists():
            # Only this call's fixed child of the verified destination parent.
            if temporary.resolve().parent != destination.parent.resolve() or not temporary.name.startswith(".przenoszenie-"):
                raise ValueError("Niepoprawna lokalizacja tymczasowej kopii wyników.")
            shutil.rmtree(temporary)


def ensure_output(store, job):
    root, name, destination, changed = planned_output(store.root, job)
    if not changed:
        return destination
    previous = job_folder(store.root, job)
    preserve_work(previous, destination, job["identity"])
    metadata = fallback_metadata(json.loads(job["source_meta"]), previous, destination)
    with store.connect() as db:
        from .store import BEGIN_WRITE, now
        db.execute(BEGIN_WRITE)
        current = db.execute("SELECT folder,output_root FROM jobs WHERE id=?", (job["id"],)).fetchone()
        if (current["folder"], current["output_root"]) != (job["folder"], job.get("output_root")):
            raise ValueError("Miejsce wyników zmieniło się podczas wybierania krótszego folderu.")
        db.execute("UPDATE jobs SET folder=?,output_root=?,source_meta=?,updated=? WHERE id=?",
                   (name, root, json.dumps(metadata, ensure_ascii=False), now(), job["id"]))
    job.update(folder=name, output_root=root, source_meta=json.dumps(metadata, ensure_ascii=False))
    return destination
