"""A dedicated local Chrome/Edge profile with explicit user login and session access."""
import json
from pathlib import Path
import socket
import subprocess
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener

from .browser_profiles import find_browser
from .common import APP, NO_WINDOW, atomic_json, read_json
from .paths import local_path, media_tool

OWNER_FILE = "kolejka-browser.json"


class LocalRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise ValueError("The local browser endpoint cannot redirect")


def managed_profiles(root):
    records = []
    for browser, name in (("chrome", "Chrome"), ("edge", "Edge")):
        executable = find_browser(browser)
        if executable:
            records.append({"mode": "managed", "browser": browser, "executable": executable,
                            "profile": str(Path(root) / ("youtube-browser-" + browser)),
                            "label": name + " | własne okno programu (bez eksportu cookies)"})
    return records


def local_json(url):
    address = urlparse(url)
    if address.scheme != "http" or address.hostname != "127.0.0.1":
        raise ValueError("Only the local program browser endpoint is allowed")
    opener = build_opener(ProxyHandler({}), LocalRedirectHandler())
    with opener.open(url, timeout=3) as response:
        return json.loads(response.read(1024 * 1024))


def valid_state(state):
    port = state.get("port")
    return (state.get("schema") == 1 and state.get("browser") in {"chrome", "edge"} and
            type(port) is int and 0 < port < 65536)


def browser_address(state):
    port = state.get("port", 0)
    if not valid_state(state):
        raise ValueError("Invalid program browser state")
    base = "http://127.0.0.1:" + str(port)
    version = local_json(base + "/json/version")
    address = urlparse(version["webSocketDebuggerUrl"])
    if address.scheme != "ws" or address.hostname != "127.0.0.1" or address.port != port:
        raise ValueError("Only the local program browser is allowed")
    if state.get("browser_path") and state["browser_path"] != address.path:
        raise ValueError("Browser instance changed")
    return base, address.path


def open_managed(record, url):
    directory = local_path(record["profile"])
    if record["browser"] not in {"chrome", "edge"} or directory.name != "youtube-browser-" + record["browser"]:
        raise ValueError("Własne okno programu wymaga osobnego folderu profilu.")
    executable = media_tool(record["executable"], "chrome" if record["browser"] == "chrome" else "msedge")
    if directory.is_dir() and any(directory.iterdir()) and not (directory / OWNER_FILE).is_file():
        raise ValueError("Ten folder nie jest profilem utworzonym przez program. Wybierz własne okno z listy.")
    directory.mkdir(parents=True, exist_ok=True)
    state = read_json(directory / OWNER_FILE, {})
    if not valid_state(state) or state["browser"] != record["browser"]:
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        state = {"schema": 1, "browser": record["browser"], "port": port}
    else:
        # The same port also works during startup and when reopening the owned profile.
        state.pop("browser_path", None)
    atomic_json(directory / OWNER_FILE, state)
    command = [executable, "--user-data-dir=" + str(directory),
               "--remote-debugging-port=" + str(state["port"]), "--remote-debugging-address=127.0.0.1",
               "--no-first-run", "--no-default-browser-check", url]
    return subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=NO_WINDOW, shell=False)


def page_socket(directory, *, remember=False):
    directory = local_path(directory, exists=True)
    state = read_json(directory / OWNER_FILE, {})
    base, browser_path = browser_address(state)
    pages = local_json(base + "/json/list")
    selected = next((page for page in pages if page.get("type") == "page" and
                     urlparse(page.get("url", "")).hostname in {"youtube.com", "www.youtube.com"}), None)
    if selected is None:
        raise ValueError("Finish login and open a YouTube video in the program browser")
    url = selected["webSocketDebuggerUrl"]
    address = urlparse(url)
    if address.scheme != "ws" or address.hostname != "127.0.0.1" or address.port != state["port"]:
        raise ValueError("Only the local program browser page is allowed")
    if remember:
        atomic_json(directory / OWNER_FILE, {**state, "browser_path": browser_path})
    return url


def read_cookies(directory, node):
    from .youtube_access import SessionError
    try:
        endpoint = page_socket(directory, remember=True)
        result = subprocess.run([media_tool(node, "node"), str(APP / "batch" / "managed_session.mjs")],
                                input=json.dumps({"socket": endpoint}), capture_output=True, text=True,
                                encoding="utf-8", creationflags=NO_WINDOW, timeout=20, shell=False)
        if result.returncode:
            raise ValueError("Browser did not provide the session")
        cookies = json.loads(result.stdout)
        if not isinstance(cookies, list):
            raise ValueError("Invalid session response")
        return cookies
    except Exception:
        raise SessionError("Otwórz własne okno programu przyciskiem Zaloguj / potwierdź. Zakończ logowanie, "
                           "otwórz film YouTube i pozostaw to okno uruchomione. Następnie ponów test; potrzebny jest Node.js 22.") from None
