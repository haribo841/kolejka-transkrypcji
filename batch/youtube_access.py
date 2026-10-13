"""Explicit, local YouTube session access. Never persist cookie values."""
from __future__ import annotations

from contextlib import contextmanager
import copy
from http.cookiejar import Cookie
import math
from pathlib import Path
import re
import time

from .common import ResourceError, atomic_json, read_json, signature

SETTINGS_FILE = "youtube-dostep.json"
BROWSERS = ("firefox", "chrome", "edge")
DEFAULTS = {"mode": "anonymous", "browser": "firefox", "profile": "", "cookie_file": "",
            "delay_seconds": 15, "request_delay_seconds": 1}


class SessionError(ResourceError):
    pass


class QuietLogger:
    # Some browser-cookie errors include cookie rows. Do not pass them to logs.
    def debug(self, *_args, **_kwargs):
        pass

    info = warning = error = debug


def validate_preferences(values):
    values = {key: values.get(key, default) for key, default in DEFAULTS.items()}
    if values["mode"] not in {"anonymous", "browser", "file", "managed"} or values["browser"] not in BROWSERS:
        raise ValueError("Wybierz pobieranie anonimowe, sesję przeglądarki albo plik cookies.")
    for key in ("profile", "cookie_file"):
        if not isinstance(values[key], str) or any(c in values[key] for c in ("\x00", "\n", "\r")):
            raise ValueError("Niepoprawna ścieżka ustawień YouTube.")
        values[key] = values[key].strip()
    if values["mode"] == "file" and not values["cookie_file"]:
        raise ValueError("Wskaż plik cookies.txt wyeksportowany dla youtube.com.")
    if values["mode"] == "managed" and (values["browser"] not in {"chrome", "edge"} or not values["profile"]):
        raise ValueError("Wybierz własne okno Chrome lub Edge z listy wykrytych sposobów dostępu.")
    for key, low, high in (("delay_seconds", 5, 300), ("request_delay_seconds", 1, 10)):
        number = float(values[key])
        if not math.isfinite(number) or not low <= number <= high:
            raise ValueError(f"Odstęp musi wynosić od {low} do {high} sekund.")
        values[key] = number
    return values


def preferences(root):
    try:
        return validate_preferences(read_json(Path(root) / SETTINGS_FILE, DEFAULTS))
    except (ValueError, TypeError, AttributeError):
        raise SessionError("Niepoprawne ustawienia YouTube. Otwórz Dostęp YouTube i zapisz je ponownie.") from None


def save_preferences(root, values):
    values = validate_preferences(values)
    # Only the source choice and paths are stored, never account/session data.
    atomic_json(Path(root) / SETTINGS_FILE, values)
    return values


def access_fingerprint(values):
    values = validate_preferences(values)
    stamp = None
    if values["mode"] == "file":
        try:
            info = Path(values["cookie_file"]).stat()
            stamp = [info.st_size, info.st_mtime_ns]
        except OSError:
            pass
    return signature({"access": values, "file_stamp": stamp})


def youtube_domain(domain):
    domain = str(domain).lstrip(".").lower()
    return domain == "youtube.com" or domain.endswith(".youtube.com")


def parse_cookie(line):
    http_only = line.startswith("#HttpOnly_")
    if http_only:
        line = line[len("#HttpOnly_"):]
    elif not line.strip() or line.startswith("#"):
        return None
    fields = line.split("\t")
    if len(fields) != 7:
        raise ValueError()
    domain, subdomains, cookie_path, secure, expires, name, value = fields
    if subdomains not in {"TRUE", "FALSE"} or secure not in {"TRUE", "FALSE"} or not cookie_path.startswith("/"):
        raise ValueError()
    if expires and not re.fullmatch(r"\d+(?:\.\d+)?", expires):
        raise ValueError()
    expiration = int(float(expires)) if expires else 0
    if not youtube_domain(domain) or (expiration and expiration <= time.time()):
        return None
    return Cookie(0, name, value, None, False, domain, subdomains == "TRUE",
                  domain.startswith("."), cookie_path, True, secure == "TRUE", expiration or None,
                  not expiration, None, None, {"HttpOnly": None} if http_only else {}, False)


def file_cookies(path):
    from yt_dlp.cookies import YoutubeDLCookieJar
    path = Path(path)
    try:
        if not path.is_file():
            raise SessionError("Nie znaleziono wybranego pliku cookies. Wskaż go ponownie w Dostęp YouTube.")
        if path.stat().st_size > 10 * 1024 * 1024:
            raise SessionError("Plik cookies jest za duży. Wyeksportuj tylko cookies witryny youtube.com.")
        lines = path.read_text(encoding="utf-8-sig").splitlines()
        if not lines or lines[0].strip() not in {"# Netscape HTTP Cookie File", "# HTTP Cookie File"}:
            raise ValueError()
        jar = YoutubeDLCookieJar()
        for line in lines[1:]:
            cookie = parse_cookie(line)
            if cookie is not None:
                jar.set_cookie(cookie)
        return jar
    except (ValueError, OverflowError):
        raise SessionError("Niepoprawny plik cookies. Potrzebny jest eksport youtube.com w formacie Netscape cookies.txt, nie JSON ani token Hugging Face.") from None
    except OSError:
        raise SessionError("Nie można odczytać wybranego pliku cookies. Sprawdź ścieżkę i dostęp do pliku.") from None


def managed_cookie_jar(values, node):
    from yt_dlp.cookies import YoutubeDLCookieJar
    from .managed_browser import read_cookies
    jar = YoutubeDLCookieJar()
    try:
        for item in read_cookies(values["profile"], node):
            cookie = managed_cookie(item)
            if cookie is not None:
                jar.set_cookie(cookie)
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        jar.clear()
        raise SessionError("Niepoprawna odpowiedź własnego okna przeglądarki. Otwórz film i ponów test dostępu.") from None
    return jar


def managed_cookie(item):
    expiration = int(item.get("expires", -1))
    if not youtube_domain(item.get("domain")) or (expiration > 0 and expiration <= time.time()):
        return None
    domain = item["domain"]
    return Cookie(0, item["name"], item["value"], None, False, domain, domain.startswith("."),
                  domain.startswith("."), item.get("path", "/"), True, item.get("secure", False),
                  expiration if expiration > 0 else None, expiration <= 0, None, None,
                  {"HttpOnly": None} if item.get("httpOnly") else {}, False)


def session_cookies(values, node=None):
    from yt_dlp.cookies import YoutubeDLCookieJar, extract_cookies_from_browser
    values = validate_preferences(values)
    mode = values["mode"]
    if mode == "anonymous":
        return None
    if mode == "managed":
        jar = managed_cookie_jar(values, node)
    elif mode == "file":
        jar = file_cookies(values["cookie_file"])
    else:
        try:
            original = extract_cookies_from_browser(values["browser"], profile=values["profile"] or None,
                                                    logger=QuietLogger())
            jar = YoutubeDLCookieJar()
            for cookie in original:
                if youtube_domain(cookie.domain) and not cookie.is_expired():
                    jar.set_cookie(copy.copy(cookie))
            original.clear()
        except Exception:
            raise SessionError("Nie udało się odczytać sesji przeglądarki. Zamknij ją całkowicie i ponów test. "
                "Chrome/Edge mogą blokować odczyt zaszyfrowanych cookies; wtedy użyj Firefoksa albo pliku cookies.txt wyeksportowanego dla YouTube.") from None
    if not list(jar):
        raise SessionError("Nie znaleziono aktualnych cookies YouTube. Zaloguj się w wybranym profilu przeglądarki lub wyeksportuj nowy plik cookies.txt.")
    return jar


@contextmanager
def youtube_client(options, values):
    import yt_dlp
    # Read the explicitly selected session before building any network handler.
    if values.get("mode") == "managed":
        node = options.get("js_runtimes", {}).get("node", {}).get("path")
        jar = session_cookies(values, node=node)
    else:
        jar = session_cookies(values)
    try:
        with yt_dlp.YoutubeDL(dict(options, cookiefile=None, cookiesfrombrowser=None)) as ydl:
            if jar is not None:
                ydl.cookiejar = jar
            yield ydl
    finally:
        if jar is not None:
            jar.clear()


def error_message(message):
    lower = str(message).casefold()
    if any(term in lower for term in ("not a bot", "confirm you're not", "confirm you’re not")):
        return "YouTube wymaga potwierdzenia użytkownika. Otwórz Dostęp YouTube, zaloguj się i potwierdź w przeglądarce, wybierz sesję lub plik cookies, a następnie przetestuj link."
    if any(term in lower for term in ("429", "too many requests", "rate limit", "this content isn't available, try again later")):
        return "YouTube ograniczył liczbę żądań. Przerwij pobieranie i spróbuj później. Logowanie nie gwarantuje zdjęcia tego ograniczenia."
    if "po token" in lower:
        return "YouTube wymaga dodatkowego PO Token dla tego sposobu pobierania. Sama sesja logowania może nie wystarczyć; kolejka została zatrzymana."
    if any(term in lower for term in ("cookies", "cookie", "dpapi", "decrypt")):
        return "Nie udało się użyć sesji YouTube. Otwórz Dostęp YouTube i sprawdź wybrany profil lub odśwież plik cookies."
    if any(term in lower for term in ("video is not available", "video unavailable", "has been removed", "private video")):
        return "Film jest niedostępny dla tej sesji. Sprawdź jego link w przeglądarce i widoczność w YouTube Studio."
    if any(term in lower for term in ("sign in", "login", "age-restricted", "members-only")):
        return "Ten materiał wymaga dostępu zalogowanego użytkownika. Sprawdź sesję w Dostęp YouTube."
    if "403" in lower:
        return "YouTube odmówił pobrania (HTTP 403). Sprawdź sesję i spróbuj później."
    if any(term in lower for term in ("timed out", "timeout", "connection", "network", "resolve")):
        return "Nie udało się połączyć z YouTube. Sprawdź połączenie i spróbuj później."
    # Never return arbitrary library errors that could include session values.
    return "YouTube nie udostępnił nagrania. Sprawdź link i dostęp w przeglądarce, a następnie użyj testu w Dostęp YouTube."
