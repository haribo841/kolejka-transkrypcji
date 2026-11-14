"""Reviewed Polish/English YouTube localization, independent of ASR job state."""
from pathlib import Path
import re

from ..common import APP, read_json
from ..paths import child_path

VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}\Z")
LANGUAGES = ("pl", "en")
KINDS = ("title", "description", "captions", "audio")
REVIEW = {"missing": "Brak", "review": "Do sprawdzenia", "approved": "Zatwierdzone",
          "na": "Nie dotyczy", "deferred": "Odłożone"}
PUBLICATION = {"none": "Nie wysłano", "sending": "Wysyłanie / weryfikacja",
               "processing": "YouTube przetwarza", "verified": "Potwierdzone",
               "manual": "Potwierdzone ręcznie", "error": "Do ponowienia"}


def video_id(value):
    if not isinstance(value, str) or not VIDEO_ID.fullmatch(value):
        raise ValueError("Brak poprawnego ID filmu YouTube.")
    return value


def directory(root, ident):
    return child_path(Path(root) / "lokalizacje", video_id(ident))


def place_dialog(dialog, parent, width, height):
    parent = parent.winfo_toplevel()
    parent.update_idletasks()
    x = max(0, min(parent.winfo_rootx() + (parent.winfo_width() - width) // 2, parent.winfo_screenwidth() - width))
    y = max(0, min(parent.winfo_rooty() + (parent.winfo_height() - height) // 2, parent.winfo_screenheight() - height - 50))
    dialog.geometry(f"{width}x{height}+{x}+{y}")


def options(root):
    defaults = {"budget": 9000, "channel_id": "", "glossary": [],
                "translation_python": str(APP / ".venv-translation" / "Scripts" / "python.exe"),
                "translation_model": str(Path(root) / "modele" / "madlad400-3b-ct2")}
    defaults.update(read_json(Path(root) / "lokalizacje-ustawienia.json", {}))
    return defaults


def configuration_summary(root):
    config = options(root)
    manifest = read_json(Path(config["translation_model"]) / "ready.json", {})
    trial = read_json(Path(root) / "lokalizacje" / "proba" / "passed.json", {})
    if not manifest:
        model = "Tłumaczenia: wymaga pobrania modelu"
    elif trial.get("passed") and trial.get("model") == manifest:
        model = "Tłumaczenia: próba zaliczona " + trial.get("at", "")[:19].replace("T", " ")
    else:
        model = "Tłumaczenia: model gotowy do próby"
    from .auth import connection_status
    account = "YouTube: " + connection_status(root).message
    return model + " | " + account + f" | Budżet API: {config['budget']} / dzień"
