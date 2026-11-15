"""Desktop OAuth with PKCE. Refresh credentials live only in Windows Credential Manager."""
import base64
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import os
from pathlib import Path
import secrets
import threading
import time
from urllib.parse import parse_qs, urlencode
import webbrowser

import requests

from ..common import atomic_json, read_json, signature

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/youtube.force-ssl"


class AuthError(RuntimeError):
    pass


class LoginCancelled(AuthError):
    pass


class Credential(ctypes.Structure):
    _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
                ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
                ("LastWritten", wintypes.FILETIME), ("CredentialBlobSize", wintypes.DWORD),
                ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)), ("Persist", wintypes.DWORD),
                ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p),
                ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR)]


class WindowsVault:
    def __init__(self, root):
        if os.name != "nt":
            raise AuthError("Publikowanie wymaga magazynu poświadczeń Windows.")
        self.target = "Kolejka/YouTube/" + signature(str(Path(root).resolve()))[:24]
        self.api = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        self.api.CredWriteW.argtypes = [ctypes.POINTER(Credential), wintypes.DWORD]
        self.api.CredWriteW.restype = wintypes.BOOL
        self.api.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(Credential))]
        self.api.CredReadW.restype = wintypes.BOOL
        self.api.CredFree.argtypes = [ctypes.c_void_p]
        self.api.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]

    def write(self, refresh_token, client_id):
        raw = refresh_token.encode("utf-8")
        if not raw or len(raw) > 2560:
            raise AuthError("Niepoprawna długość poświadczenia Google.")
        blob = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
        item = Credential(Type=1, TargetName=self.target, UserName=client_id,
                          Persist=2, CredentialBlobSize=len(raw), CredentialBlob=blob)
        if not self.api.CredWriteW(ctypes.byref(item), 0):
            raise AuthError("Windows nie zapisał poświadczenia. Sprawdź magazyn poświadczeń.")

    def read(self, client_id):
        pointer = ctypes.POINTER(Credential)()
        if not self.api.CredReadW(self.target, 1, 0, ctypes.byref(pointer)):
            raise AuthError("Połącz konto YouTube w kreatorze lokalizacji.")
        try:
            if pointer.contents.UserName != client_id:
                raise AuthError("Zmieniono klienta OAuth. Połącz konto ponownie.")
            return ctypes.string_at(pointer.contents.CredentialBlob, pointer.contents.CredentialBlobSize).decode("utf-8")
        finally:
            self.api.CredFree(pointer)

    def delete(self):
        self.api.CredDeleteW(self.target, 1, 0)


def client_path(root):
    return Path(root) / "konfiguracja" / "yt-oauth-client.json"


def client_settings(path):
    data = read_json(path, {})
    client = data.get("installed", {})
    if not client.get("client_id", "").endswith(".apps.googleusercontent.com") or not client.get("client_secret"):
        raise AuthError("Wybierz JSON klienta OAuth typu Desktop app z Google Cloud.")
    return {key: client[key] for key in ("client_id", "client_secret")}


def import_client(root, path):
    client = client_settings(path)
    atomic_json(client_path(root), {"installed": client})


@dataclass(frozen=True)
class ConnectionStatus:
    ready: bool
    step: int
    message: str


def connection_status(root):
    """Check only local configuration. A model trial never proves API access."""
    from . import options
    try:
        client = client_settings(client_path(root))
    except (AuthError, OSError):
        return ConnectionStatus(False, 0, "brak klienta Google OAuth (krok 1 kreatora)")
    try:
        WindowsVault(root).read(client["client_id"])
    except (AuthError, OSError):
        return ConnectionStatus(False, 1, "konto Google niepołączone (krok 2 kreatora)")
    if not options(root)["channel_id"]:
        return ConnectionStatus(False, 2, "nie wybrano kanału (krok 3 kreatora)")
    return ConnectionStatus(True, 6, "konto i kanał skonfigurowane; odśwież dane, aby sprawdzić dostęp")


def token_request(payload):
    try:
        response = requests.post(TOKEN_URL, data=payload, timeout=(10, 45), allow_redirects=False)
        if response.status_code != 200:
            raise AuthError("Google odrzucił dostęp. Wybierz Połącz ponownie i sprawdź klienta OAuth.")
        result = response.json()
        if not result.get("access_token"):
            raise AuthError("Google nie zwrócił dostępu do API.")
        return result
    except (requests.RequestException, ValueError) as error:
        raise AuthError("Nie udało się połączyć z Google. Sprawdź internet i ponów logowanie.") from error


def check_login_cancelled(cancel):
    if cancel.is_set():
        raise LoginCancelled("Anulowano logowanie. Możesz od razu połączyć konto ponownie.")


def wait_for_callback(server, callback, cancel, timeout):
    server.timeout = .25
    deadline = time.monotonic() + timeout
    while not callback and time.monotonic() < deadline:
        check_login_cancelled(cancel)
        server.handle_request()
    check_login_cancelled(cancel)
    if callback.get("error") == "access_denied":
        raise AuthError("Google odmówił dostępu. Dodaj konto do użytkowników testowych w Google Auth Platform > Odbiorcy, a następnie wybierz Ponów logowanie.")
    if not callback.get("code") or callback.get("error"):
        raise AuthError("Logowanie nie zostało zakończone. Wybierz Ponów logowanie. Jeśli przeglądarka pokazuje błąd Google, najpierw popraw wskazane ustawienie projektu.")


def connect(root, *, vault=None, browser=webbrowser.open, timeout=300, cancel=None):
    client = client_settings(client_path(root))
    vault = vault or WindowsVault(root)
    cancel = cancel or threading.Event()
    check_login_cancelled(cancel)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    state, callback = secrets.token_urlsafe(32), {}

    class Callback(BaseHTTPRequestHandler):
        def do_GET(self):
            query = parse_qs(self.path.partition("?")[2])
            valid = secrets.compare_digest(query.get("state", [""])[0], state)
            if valid:
                callback.update(code=query.get("code", [""])[0], error=query.get("error", [""])[0])
            self.send_response(200 if valid else 400)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write("Wróć do aplikacji Kolejka transkrypcji.".encode("utf-8"))

        def log_message(self, *_args):
            return

    with HTTPServer(("127.0.0.1", 0), Callback) as server:
        redirect = f"http://127.0.0.1:{server.server_port}/"
        params = {**client, "redirect_uri": redirect, "response_type": "code", "scope": SCOPE,
                  "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
                  "access_type": "offline", "prompt": "consent"}
        params.pop("client_secret")
        if browser(AUTH_URL + "?" + urlencode(params)) is False:
            raise AuthError("Nie udało się otworzyć przeglądarki. Sprawdź domyślną przeglądarkę i ponów logowanie.")
        wait_for_callback(server, callback, cancel, timeout)
    check_login_cancelled(cancel)
    token = token_request({**client, "code": callback["code"], "code_verifier": verifier,
                           "redirect_uri": redirect, "grant_type": "authorization_code"})
    if not token.get("refresh_token"):
        raise AuthError("Nie przyznano dostępu offline. Połącz konto ponownie.")
    check_login_cancelled(cancel)
    vault.write(token["refresh_token"], client["client_id"])


class Authorization:
    def __init__(self, root, *, vault=None):
        self.client = client_settings(client_path(root))
        self.vault = vault or WindowsVault(root)
        self.access, self.expires = "", 0

    def token(self):
        if time.monotonic() >= self.expires:
            token = token_request({**self.client, "refresh_token": self.vault.read(self.client["client_id"]), "grant_type": "refresh_token"})
            self.access = token["access_token"]
            self.expires = time.monotonic() + max(0, token.get("expires_in", 3600) - 120)
        return self.access
