"""Bounded official YouTube API access, including accounted resumable uploads."""
import calendar
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlparse
import re

import requests

from . import options, video_id
from .auth import AuthError, Authorization

BASE = "https://www.googleapis.com/youtube/v3/"
UPLOAD = "https://www.googleapis.com/upload/youtube/v3/captions"


class QuotaError(RuntimeError):
    pass


class ApiError(RuntimeError):
    pass


class ConflictError(ApiError):
    pass


def quota_day(current=None):
    """YouTube resets at midnight in America/Los_Angeles, including US DST."""
    current = current or datetime.now(timezone.utc)
    year = current.year
    march = calendar.monthcalendar(year, 3)
    november = calendar.monthcalendar(year, 11)
    sundays = [week[calendar.SUNDAY] for week in march if week[calendar.SUNDAY]]
    first = next(week[calendar.SUNDAY] for week in november if week[calendar.SUNDAY])
    start = datetime(year, 3, sundays[1], 10, tzinfo=timezone.utc)
    end = datetime(year, 11, first, 9, tzinfo=timezone.utc)
    return (current.astimezone(timezone.utc) - timedelta(hours=7 if start <= current < end else 8)).date().isoformat()


def checked_response(response):
    reasons = response_reasons(response) if response.status_code >= 400 else set()
    if response.status_code in {401, 403}:
        if reasons & {"quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded", "userRateLimitExceeded"}:
            raise QuotaError("YouTube wstrzymał żądania API. Postęp zachowano; ponów później.")
        if response.status_code == 401 or reasons & {"authError", "insufficientPermissions"}:
            raise AuthError("Dostęp Google wygasł albo ma zbyt małe uprawnienia. Połącz ponownie.")
    if response.status_code == 412:
        raise ConflictError("Dane filmu zmieniły się podczas publikacji. Odśwież i zatwierdź nowy podgląd.")
    if not 200 <= response.status_code < 300:
        explanation = {"invalidTitle": "Sprawdź długość i znaki tytułu.",
                       "invalidDescription": "Sprawdź długość i znaki opisu.",
                       "defaultLanguageNotSet": "Zatwierdź polski tytuł i opis, aby ustawić język metadanych.",
                       "captionExists": "Ścieżka już istnieje. Wznów, aby sprawdzić jej stan zamiast tworzyć duplikat.",
                       "videoNotFound": "Film jest niedostępny dla wybranego konta.",
                       "invalidMetadata": "Sprawdź język i parametry napisów.",
                       "forbidden": "Sprawdź wybrany kanał i uprawnienia konta."}
        details = " ".join(explanation.get(reason, reason) for reason in sorted(reasons))
        raise ApiError(f"YouTube API: HTTP {response.status_code}. " + (details or "Sprawdź dostęp do filmu i ponów czynność."))
    return response


def response_reasons(response):
    try:
        values = response.json().get("error", {}).get("errors", [])
        return {entry["reason"] for entry in values if isinstance(entry.get("reason"), str)
                and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", entry["reason"])}
    except (ValueError, AttributeError, TypeError):
        return set()


class YouTube:
    def __init__(self, store, *, auth=None, transport=None):
        self.store = store
        self.auth = auth or Authorization(store.root)
        self.transport = transport or requests.Session()
        self.budget = int(options(store.root)["budget"])

    def request(self, method, resource, *, cost=1, params=None, body=None, etag=None, raw=False):
        if resource not in {"channels", "playlistItems", "videos", "captions"} and not resource.startswith("captions/"):
            raise ValueError("Nieobsługiwany zasób YouTube.")
        token = self.auth.token()
        self.store.reserve_quota(quota_day(), cost, self.budget)
        headers = {"Authorization": "Bearer " + token}
        if etag:
            headers["If-Match"] = etag
        try:
            response = self.transport.request(method, BASE + resource, params=params, json=body,
                                              headers=headers, timeout=(10, 90), allow_redirects=False)
            checked_response(response)
            return response.content.decode("utf-8-sig") if raw else response.json()
        except (requests.RequestException, ValueError) as error:
            raise ApiError("Niepewny wynik połączenia z YouTube. Przy wznowieniu sprawdzimy stan zdalny.") from error

    def channels(self):
        return self.request("GET", "channels", params={"part": "snippet,contentDetails", "mine": "true", "maxResults": 50}).get("items", [])

    def videos(self, ids):
        ids = [video_id(i) for i in ids]
        if not ids or len(ids) > 50:
            raise ValueError("Jedno żądanie może obejmować od 1 do 50 filmów.")
        return self.request("GET", "videos", params={"part": "snippet,localizations,contentDetails,status", "id": ",".join(ids)}).get("items", [])

    def video(self, ident):
        values = self.videos([ident])
        if not values:
            raise ApiError("Film nie jest dostępny dla połączonego konta.")
        expected = options(self.store.root)["channel_id"]
        if not expected or values[0]["snippet"]["channelId"] != expected:
            raise AuthError("Film nie należy do wybranego kanału. Sprawdź konto w kreatorze.")
        return values[0]

    def captions(self, ident):
        return self.request("GET", "captions", cost=50, params={"part": "snippet", "videoId": video_id(ident)}).get("items", [])

    def download_caption(self, ident):
        return self.request("GET", "captions/" + quote(ident, safe=""), cost=200, params={"tfmt": "srt"}, raw=True)

    def upload_caption(self, metadata, text, *, caption_id=""):
        token = self.auth.token()
        self.store.reserve_quota(quota_day(), 450 if caption_id else 400, self.budget)
        body = {"snippet": metadata}
        if caption_id:
            body["id"] = caption_id
        headers = {"Authorization": "Bearer " + token, "X-Upload-Content-Type": "application/octet-stream"}
        try:
            response = self.transport.request("PUT" if caption_id else "POST", UPLOAD,
                params={"part": "snippet", "uploadType": "resumable"}, json=body, headers=headers, timeout=(10, 90), allow_redirects=False)
            checked_response(response)
            location = response.headers.get("Location", "")
            parsed = urlparse(location)
            if parsed.scheme != "https" or parsed.hostname != "www.googleapis.com" or parsed.username or parsed.password or parsed.port not in {None, 443} or not parsed.path.startswith("/upload/youtube/"):
                raise ApiError("Google nie zwrócił poprawnej sesji wysyłania napisów.")
            response = self.transport.request("PUT", location, data=text.encode("utf-8"),
                headers={"Authorization": "Bearer " + token, "Content-Type": "application/octet-stream"}, timeout=(10, 90), allow_redirects=False)
            checked_response(response)
            return response.json()
        except (requests.RequestException, ValueError) as error:
            raise ApiError("Przerwano wysyłanie napisów. Nie ponawiaj bez odczytu kontrolnego; wybierz Wznów publikację.") from error

    def update_video(self, body, etag):
        return self.request("PUT", "videos", cost=50, params={"part": "snippet,localizations"}, body=body, etag=etag)


def sync_catalog(store, api, ids, progress):
    """Full upload-list refresh is cheap; caption details are fetched only for selection."""
    selected = list(ids)
    if not selected:
        channel_id = options(store.root)["channel_id"]
        channel = next((c for c in api.channels() if c["id"] == channel_id), None)
        if channel is None:
            raise AuthError("Wybierz kanał w kreatorze publikacji.")
        playlist = channel["contentDetails"]["relatedPlaylists"]["uploads"]
        token = ""
        progress("Zbieranie katalogu filmów wybranego kanału…")
        while True:
            page = api.request("GET", "playlistItems", params={"part": "contentDetails", "playlistId": playlist, "maxResults": 50, "pageToken": token})
            selected.extend(item["contentDetails"]["videoId"] for item in page.get("items", []))
            progress(f"Znaleziono {len(selected)} filmów w katalogu kanału")
            token = page.get("nextPageToken", "")
            if not token:
                break
    selected = list(dict.fromkeys(selected))
    for offset in range(0, len(selected), 50):
        progress(f"Pobieranie metadanych {offset + 1}-{min(offset + 50, len(selected))} / {len(selected)}")
        data = api.videos(selected[offset:offset + 50])
        rows = []
        for item in data:
            if item["snippet"]["channelId"] != options(store.root)["channel_id"]:
                raise AuthError("W odpowiedzi jest film innego kanału. Synchronizacja przerwana.")
            snippet = item["snippet"]
            rows.append({"identity": "yt:" + item["id"], "kind": "youtube", "title": snippet["title"],
                         "source": "https://www.youtube.com/watch?v=" + item["id"], "date": snippet.get("publishedAt", ""),
                         "visibility": item.get("status", {}).get("privacyStatus", ""), "meta": {"origin": "youtube-api"}})
        store.base.add_many(rows)
        store.bootstrap()
        for item in data:
            store.snapshot(item)
        progress(f"Odświeżono {min(offset + 50, len(selected))} / {len(selected)} filmów")
    if ids:
        from ..common import atomic_json
        from . import directory
        for index, ident in enumerate(ids, start=1):
            progress(f"Sprawdzanie napisów filmu {index} / {len(ids)}")
            atomic_json(directory(store.root, ident) / "youtube-captions.json", api.captions(ident))
