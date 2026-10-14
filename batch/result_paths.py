"""Named result files, legacy compatibility and protection against overwrite."""
from pathlib import Path

from .common import ResourceError, atomic_new_bytes, atomic_json, digest, read_json, signature, slug
from .path_budget import PATH_LIMIT, fit_slug, path_units


RESULT_MANIFEST = 'gotowe.json'

EXTENSIONS = ("txt", "srt", "vtt", "json")
LEGACY_OUTPUTS = tuple("transkrypcja." + extension for extension in EXTENSIONS)


def output_key(identity):
    kind, _, value = identity.partition(":")
    if kind == "yt" and len(value) == 11:
        key = "yt-" + value
    elif kind == "local" and value:
        key = "local-" + value[:20]
    else:
        key = signature(identity)[:20]
    return slug(key)


def output_stem(title, identity):
    return slug(title)[:45].rstrip(" .") + "__" + output_key(identity) + "_transkrypcja"


def version_suffix(version):
    return "" if version == 1 else f"__wersja-{version}"


def minimum_export_leaf_units(identity, *, partial=True, version=9999):
    """Smallest named export, reserving its identity, status and version."""
    tail = "__" + output_key(identity) + "_transkrypcja"
    if partial:
        tail += "_czesciowa"
    return path_units("n" + tail + version_suffix(version) + ".json")


def bounded_output_stem(folder, stem, suffix, identity):
    """Shorten only a new export's title, preserving its identity and suffixes."""
    tail = "__" + output_key(identity) + "_transkrypcja"
    if stem.endswith(tail + "_czesciowa"):
        tail += "_czesciowa"
    if not stem.endswith(tail):
        raise ValueError("Nazwa wyniku nie zawiera identyfikatora nagrania.")
    budget = PATH_LIMIT - path_units(Path(folder).resolve()) - 1 - path_units(tail + suffix + ".json")
    if budget < 1:
        raise ResourceError("Ścieżka wyników jest zbyt długa. Wybierz krótszy folder wyników.")
    title = stem[:-len(tail)]
    return fit_slug(title, budget) + tail


def output_paths(folder, manifest=None):
    folder = Path(folder)
    manifest = read_json(folder / RESULT_MANIFEST, {}) if manifest is None else manifest
    if not isinstance(manifest, dict):
        raise ValueError("Niepoprawny manifest wyników nagrania.")
    names = manifest.get("outputs", dict(zip(EXTENSIONS, LEGACY_OUTPUTS)))
    if not isinstance(names, dict) or set(names) != set(EXTENSIONS):
        raise ValueError("Niekompletna lista plików wynikowych.")
    paths = {}
    for extension, name in names.items():
        if not isinstance(name, str) or Path(name).name != name or not name.endswith("." + extension):
            raise ValueError("Niepoprawna nazwa pliku wynikowego.")
        path = folder / name
        if not path.resolve().is_relative_to(folder.resolve()):
            raise ValueError("Plik wynikowy znajduje się poza katalogiem nagrania.")
        paths[extension] = path
    return paths


def assert_output_owner(folder, identity):
    folder = Path(folder)
    from .local_outputs import OWNER_FILE
    owner = read_json(folder / OWNER_FILE, {})
    if owner and owner.get("identity") != identity:
        raise FileExistsError("Katalog roboczy należy do innego nagrania: " + str(folder))
    for name in (RESULT_MANIFEST, "czesciowe.json"):
        manifest = read_json(folder / name, {})
        if manifest and manifest.get("identity") != identity:
            raise FileExistsError("Katalog zawiera wyniki innego nagrania. Zachowano dotychczasowe pliki: " + str(folder))
    legacy = read_json(folder / "transkrypcja.json", {})
    if legacy and legacy.get("identity") != identity:
        raise FileExistsError("W katalogu istnieje transkrypcja innego nagrania: " + str(folder))


def reserve_output(folder, identity):
    from .local_outputs import OWNER_FILE
    assert_output_owner(folder, identity)
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    marker = folder / OWNER_FILE
    if not marker.exists():
        import json
        atomic_new_bytes(marker, json.dumps({"identity": identity}, ensure_ascii=False).encode("utf-8"))


def unused_paths(folder, stem, *, identity=None):
    version = 1
    while True:
        suffix = version_suffix(version)
        bounded = bounded_output_stem(folder, stem, suffix, identity) if identity is not None else stem
        paths = {ext: Path(folder) / f"{bounded}{suffix}.{ext}" for ext in EXTENSIONS}
        if any(path_units(path.resolve()) > PATH_LIMIT for path in paths.values()):
            raise ResourceError("Ścieżka wyników jest zbyt długa. Wybierz krótszy folder wyników.")
        if not any(path.exists() for path in paths.values()):
            return paths
        version += 1


def upgrade_output_names(folder, identity):
    """Make named copies of complete legacy exports, preserving originals."""
    from .exporter import verify_outputs
    folder = Path(folder)
    if not verify_outputs(folder, identity):
        raise ValueError("Nie można zmienić nazw niekompletnych lub zmodyfikowanych wyników: " + str(folder))
    manifest = read_json(folder / RESULT_MANIFEST)
    if "outputs" in manifest:
        return output_paths(folder, manifest)
    old = output_paths(folder, manifest)
    report = read_json(old["json"])
    paths = unused_paths(folder, output_stem(report["title"], identity), identity=identity)
    for ext, path in paths.items():
        atomic_new_bytes(path, old[ext].read_bytes())
        if digest(path) != manifest["files"][old[ext].name]:
            raise OSError("Sprawdzenie kopii wyniku nie powiodło się.")
    manifest.update(format_version=2, outputs={ext: path.name for ext, path in paths.items()},
                    legacy_files=manifest["files"], files={path.name: digest(path) for path in paths.values()})
    atomic_json(folder / RESULT_MANIFEST, manifest)
    if not verify_outputs(folder, identity):
        raise OSError("Sprawdzenie nazwanych wyników nie powiodło się.")
    return paths
