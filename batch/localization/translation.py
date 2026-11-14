"""Pinned local MADLAD translation and content-addressed checkpoints in an isolated runtime."""
import argparse
from datetime import datetime, timezone
import importlib.metadata
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys

from ..common import atomic_json, check_disk, digest, read_json, signature
from ..paths import child_path
from . import options

MODEL = "google/madlad400-3b-mt"
REVISION = "fa184c675da0b5c9e1c8694fccd4e12e2d422094"
STATE_FILE = "lokalizacje-model.json"


def setup(root, staging):
    from huggingface_hub import snapshot_download
    from ctranslate2.converters import TransformersConverter
    config = options(root)
    output = Path(config["translation_model"])
    output.parent.mkdir(parents=True, exist_ok=True)
    check_disk(output.parent, 4 * 1024 ** 3)
    staging = Path(staging)
    staging.mkdir(parents=True, exist_ok=True)
    check_disk(staging, 16 * 1024 ** 3)
    atomic_json(Path(root) / STATE_FILE, {"state": "running", "message": "Pobieranie MADLAD-400 3B. Konwersja INT8 nastąpi po pobraniu."})
    source = snapshot_download(MODEL, revision="fa184c675da0b5c9e1c8694fccd4e12e2d422094", local_dir=staging / "source",
                               allow_patterns=["*.json", "*.model", "*.safetensors"])
    temporary = output.with_name(output.name + ".preparing")
    converter = TransformersConverter(source, load_as_float16=True)
    converter.convert(str(temporary), quantization="int8", force=True)
    for name in ("tokenizer.json", "tokenizer_config.json", "special_tokens_map.json", "added_tokens.json", "spiece.model"):
        candidate = Path(source) / name
        if candidate.is_file():
            shutil.copy2(candidate, temporary / name)
    files = {str(p.relative_to(temporary)): digest(p) for p in temporary.rglob("*") if p.is_file()}
    versions = {name: importlib.metadata.version(name) for name in ("ctranslate2", "transformers", "sentencepiece")}
    atomic_json(temporary / "ready.json", {"model": MODEL, "revision": REVISION, "quantization": "int8", "device": "cpu", "files": files, "versions": versions})
    if output.exists():
        raise RuntimeError("Model docelowy już istnieje. Zachowano poprzedni model i nową konwersję .preparing.")
    os.replace(temporary, output)
    atomic_json(Path(root) / STATE_FILE, {"state": "ready", "message": "Model tłumaczeń gotowy do próby.", "revision": REVISION, "versions": versions})


def ready(root):
    model = Path(options(root)["translation_model"])
    report = read_json(model / "ready.json", {})
    if report.get("revision") != REVISION or not (model / "model.bin").is_file():
        raise RuntimeError("Brak gotowego modelu tłumaczeń. Uruchom kreator lokalizacji.")
    return model, report


class Translator:
    def __init__(self, root, cache):
        import ctranslate2
        from lingua import Language, LanguageDetectorBuilder
        path, self.manifest = ready(root)
        for name, checksum in self.manifest["files"].items():
            file = child_path(path, name)
            if digest(file) != checksum:
                raise RuntimeError("Model tłumaczeń nie przeszedł sprawdzenia sum kontrolnych.")
        self.tokenizer = LocalTokenizer(path / "tokenizer.json")
        self.engine = ctranslate2.Translator(str(path), device="cpu", compute_type="int8", inter_threads=1,
                                            intra_threads=max(1, min(8, (os.cpu_count() or 2) // 2)))
        self.detector = LanguageDetectorBuilder.from_languages(Language.POLISH, Language.ENGLISH).build()
        self.cache = Path(cache)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.cache_db = sqlite3.connect(self.cache / "teksty.sqlite3")
        self.cache_db.execute("PRAGMA journal_mode=WAL")
        self.cache_db.execute("PRAGMA synchronous=FULL")
        self.cache_db.execute("CREATE TABLE IF NOT EXISTS cache(key TEXT PRIMARY KEY,value TEXT NOT NULL,checksum TEXT NOT NULL)")
        self.glossary = [str(x) for x in options(root).get("glossary", []) if str(x).strip()]

    def language(self, text):
        values = self.detector.compute_language_confidence_values(text)
        if values and values[0].value >= .85:
            return values[0].language.iso_code_639_1.name.lower()
        return "auto"

    def piece(self, text, lang):
        if not text.strip():
            return text
        tokens = self.tokenizer.convert_ids_to_tokens(self.tokenizer.encode(f"<2{lang}> " + text))
        if len(tokens) > 480:
            left, right = split_text(text)
            return self.piece(left, lang) + " " + self.piece(right, lang)
        result = self.engine.translate_batch([tokens], beam_size=3, max_decoding_length=512)[0]
        translated = self.tokenizer.decode(self.tokenizer.convert_tokens_to_ids(result.hypotheses[0]), skip_special_tokens=True).strip()
        if not translated or len(result.hypotheses[0]) >= 512:
            raise ValueError("Tłumaczenie puste albo ucięte. Sprawdź treść źródłową.")
        return translated

    def translate(self, text, lang, source_language="auto"):
        key = signature({"text": text, "target": lang, "source": source_language, "model": self.manifest,
                         "glossary": self.glossary, "format": 1})
        saved = self.cache_db.execute("SELECT value,checksum FROM cache WHERE key=?", (key,)).fetchone()
        if saved and saved[1] == signature(saved[0]):
            return saved[0]
        output = []
        for line in text.splitlines(keepends=True):
            ending = "\n" if line.endswith("\n") else ""
            body = line.rstrip("\r\n")
            detected = source_language if source_language != "auto" else self.language(body)
            if detected == lang or not body.strip():
                output.append(body + ending)
            else:
                output.append(self.protected(body, lang) + ending)
        value = "".join(output)
        with self.cache_db:
            self.cache_db.execute("INSERT OR REPLACE INTO cache VALUES(?,?,?)", (key, value, signature(value)))
        return value

    def protected(self, text, lang):
        terms = [r"https?://\S+", r"\b\d{1,2}:\d{2}(?::\d{2})?\b", r"#[\w-]+", r"\b\d+(?:[.,]\d+)*\b"]
        terms.extend(r"(?<!\w)" + re.escape(t) + r"(?!\w)" for t in sorted(self.glossary, key=len, reverse=True))
        pattern = re.compile("(" + "|".join(terms) + ")", re.IGNORECASE)
        pieces = pattern.split(text)
        return "".join(p if i % 2 else preserve_spaces(p, self.piece(p, lang)) for i, p in enumerate(pieces))


class LocalTokenizer:
    """Load the verified tokenizer JSON directly, without any Hub/network code."""
    def __init__(self, path):
        from tokenizers import Tokenizer
        self.backend = Tokenizer.from_file(str(path))

    def encode(self, text):
        return self.backend.encode(text).ids

    def convert_ids_to_tokens(self, values):
        return [self.backend.id_to_token(value) for value in values]

    def convert_tokens_to_ids(self, values):
        result = [self.backend.token_to_id(value) for value in values]
        if any(value is None for value in result):
            raise ValueError("Nieznany token w wyniku tłumaczenia.")
        return result

    def decode(self, values, skip_special_tokens=True):
        return self.backend.decode(values, skip_special_tokens=skip_special_tokens)


def preserve_spaces(source, value):
    if not source.strip():
        return source
    return source[:len(source) - len(source.lstrip())] + value.strip() + source[len(source.rstrip()):]


def split_text(text):
    middle = len(text) // 2
    points = [m.start() for m in re.finditer(r"\s+", text)]
    if not points:
        raise ValueError("Zbyt długi fragment bez spacji. Podziel go ręcznie.")
    at = min(points, key=lambda p: abs(p - middle))
    return text[:at].rstrip(), text[at:].lstrip()


def run_request(path):
    request = read_json(path)
    translator = Translator(request["root"], request["cache"])
    output = []
    for index, unit in enumerate(request["units"]):
        text = translator.translate(unit["text"], unit["lang"], unit.get("source_language", "auto"))
        output.append({"key": unit["key"], "text": text})
        print(json.dumps({"message": f"Tłumaczenia: {index + 1}/{len(request['units'])}"}), flush=True)
    atomic_json(request["result"], {"units": output, "model": translator.manifest,
                                  "created": datetime.now(timezone.utc).isoformat(),
                                  "runtime": {"python": sys.version.split()[0], "device": "cpu", "compute_type": "int8"}})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--setup", type=Path)
    parser.add_argument("--staging", type=Path)
    parser.add_argument("request", nargs="?", type=Path)
    args = parser.parse_args()
    os.environ.update(HF_HUB_DISABLE_TELEMETRY="1", DO_NOT_TRACK="1", TOKENIZERS_PARALLELISM="false")
    try:
        if args.setup:
            if not args.staging:
                raise ValueError("Wskaż folder roboczy konwersji.")
            setup(args.setup, args.staging)
        else:
            os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
            run_request(args.request)
    except Exception as error:
        if args.setup:
            atomic_json(args.setup / STATE_FILE, {"state": "error", "message": str(error)[:1000]})
        raise


if __name__ == "__main__":
    main()
