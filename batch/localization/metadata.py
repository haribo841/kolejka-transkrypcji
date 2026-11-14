"""Read-only public metadata previews, distinct from authenticated API snapshots."""
from datetime import datetime, timezone

from ..common import settings
from ..media import youtube_options, wait_before_youtube
from ..youtube_access import DEFAULTS, error_message, youtube_client
from . import video_id
from .store import metadata_source


def source_text(video):
    data = metadata_source(video)
    snippet = data.get("snippet", {})
    origin = "publiczna strona filmu, bez sprawdzenia lokalizacji i uprawnień do publikacji" if data.get("source") == "public" else "YouTube API"
    if not data:
        return ("Źródło: katalog lokalny. Dane z YouTube nie zostały jeszcze pobrane.\n"
                f"Tytuł z katalogu: {video['title']}\n\n"
                "Opis nie został pobrany. Kliknij Pobierz tytuł i opis. "
                "Zaliczona próba tłumaczenia sprawdza model lokalny, a nie połączenie z YouTube.")
    description = snippet.get("description", "")
    lines = ["Źródło: " + origin, "Tytuł: " + snippet.get("title", video["title"]),
             "Język metadanych: " + snippet.get("defaultLanguage", "nieustalony"),
             "", "Opis:", description or "[Opis na YouTube jest pusty.]", "", "Dotychczasowe lokalizacje:"]
    if data.get("source") == "public":
        lines.append("Nie sprawdzono. Połącz konto Google i odśwież przez API, aby zobaczyć wszystkie lokalizacje.")
    elif data.get("localizations"):
        for lang, fields in sorted(data["localizations"].items()):
            lines.extend(["", lang.upper() + ": " + fields.get("title", ""), fields.get("description", "")])
    else:
        lines.append("API nie zwróciło dodatkowych lokalizacji.")
    return "\n".join(lines)


def extract_public(root, ident, progress):
    ident = video_id(ident)
    access = dict(DEFAULTS)
    config = settings(root)
    params = youtube_options(config, access)
    params.update(skip_download=True, ignore_no_formats_error=True,
                  extractor_args={"youtube": {"skip": ["dash", "hls", "translated_subs"],
                                             "player_skip": ["js"]}})
    wait_before_youtube(access, lambda message, _value: progress(message))
    progress("Pobieranie publicznego tytułu i opisu: " + ident)
    try:
        with youtube_client(params, access) as client:
            info = client.extract_info("https://www.youtube.com/watch?v=" + ident, download=False)
    except Exception as error:
        raise RuntimeError(error_message(error)) from None
    if not isinstance(info, dict) or info.get("id") != ident or not info.get("title") or not isinstance(info.get("description"), str):
        raise ValueError("YouTube nie zwrócił pełnego tytułu i opisu. Ponów odczyt albo połącz konto Google w kreatorze.")
    # Do not infer metadata/audio languages or claim that other localizations are absent.
    return {"id": ident, "source": "public", "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "snippet": {"title": info["title"], "description": info["description"],
                        "channelId": info.get("channel_id", "")}}


def refresh_public(store, ident, progress):
    try:
        store.public_metadata(extract_public(store.root, ident, progress))
    except (ValueError, RuntimeError, OSError) as error:
        store.metadata_error(ident, str(error))
        raise
