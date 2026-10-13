"""Discover browser executables and profile names without reading session cookies."""
import configparser
import json
import os
from pathlib import Path
import shutil
import subprocess

from .common import NO_WINDOW

EXECUTABLES = {"firefox": "firefox.exe", "chrome": "chrome.exe", "edge": "msedge.exe"}
NAMES = {"firefox": "Firefox", "chrome": "Chrome", "edge": "Edge"}


def registry_executable(filename):
    if os.name != "nt":
        return None
    import winreg
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, r"Software\Microsoft\Windows\CurrentVersion\App Paths" + chr(92) + filename) as key:
                return Path(os.path.expandvars(winreg.QueryValue(key, None)).strip('"'))
        except (OSError, TypeError, ValueError):
            continue
    return None


def find_browser(browser, env=None):
    env = os.environ if env is None else env
    filename = EXECUTABLES[browser]
    candidates = [shutil.which(filename), registry_executable(filename)]
    relative = {"firefox": "Mozilla Firefox/firefox.exe", "chrome": "Google/Chrome/Application/chrome.exe",
                "edge": "Microsoft/Edge/Application/msedge.exe"}[browser]
    for name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        if env.get(name):
            candidates.append(Path(env[name]) / relative)
    return next((str(Path(path).resolve()) for path in candidates
                 if path and Path(path).is_file() and Path(path).name.casefold() == filename), "")


def profile(browser, path, name, executable, preferred=False):
    return {"mode": "browser", "browser": browser, "profile": str(path.resolve()),
            "label": NAMES[browser] + " | " + str(name) + " (" + path.name + ")",
            "executable": executable, "preferred": preferred}


def firefox_profiles(root, executable):
    result = []
    config = configparser.ConfigParser(interpolation=None)
    try:
        config.read(root / "profiles.ini", encoding="utf-8")
        for section in config.sections():
            if not section.startswith("Profile") or not config.has_option(section, "Path"):
                continue
            path = Path(config[section]["Path"])
            if config[section].get("IsRelative", "1") == "1":
                path = root / path
            if path.is_dir():
                result.append(profile("firefox", path, config[section].get("Name", section), executable,
                                      config[section].get("Default") == "1"))
    except (OSError, configparser.Error):
        return []
    return result


def chromium_profiles(browser, root, executable):
    if not root.is_dir():
        return []
    try:
        state_path = root / "Local State"
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.stat().st_size < 10 * 1024**2 else {}
        values = state.get("profile", {})
        info = values.get("info_cache", {})
        info = {key: value for key, value in info.items() if isinstance(value, dict)} if isinstance(info, dict) else {}
        last = values.get("last_used", "Default")
    except (OSError, ValueError, AttributeError):
        info, last = {}, "Default"
    names = set(info) | {path.name for path in root.iterdir() if path.name == "Default" or path.name.startswith("Profile ")}
    result = []
    for name in sorted(names):
        if Path(name).name == name and (root / name).is_dir():
            result.append(profile(browser, root / name, info.get(name, {}).get("name", name), executable, name == last))
    return result


def discover_profiles(env=None):
    env = os.environ if env is None else env
    result = []
    for browser in EXECUTABLES:
        executable = find_browser(browser, env)
        if not executable:
            continue
        if browser == "firefox" and env.get("APPDATA"):
            result.extend(firefox_profiles(Path(env["APPDATA"]) / "Mozilla/Firefox", executable))
        elif browser != "firefox" and env.get("LOCALAPPDATA"):
            relative = "Google/Chrome/User Data" if browser == "chrome" else "Microsoft/Edge/User Data"
            result.extend(chromium_profiles(browser, Path(env["LOCALAPPDATA"]) / relative, executable))
    return sorted(result, key=lambda item: (item["browser"] != "firefox", not item["preferred"], item["label"]))


def open_profile(record, url):
    executable, directory = Path(record["executable"]), Path(record["profile"])
    if not executable.is_file() or executable.name.casefold() != EXECUTABLES[record["browser"]]:
        raise ValueError("Nie znaleziono wybranej przeglądarki. Odśwież listę profili.")
    if not directory.is_dir():
        raise ValueError("Folder profilu nie istnieje. Odśwież listę lub wskaż profil w ustawieniach zaawansowanych.")
    command = [str(executable)]
    if record["browser"] == "firefox":
        command += ["-profile", str(directory), "-new-tab", url]
    else:
        command += ["--user-data-dir=" + str(directory.parent), "--profile-directory=" + directory.name, url]
    return subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=NO_WINDOW, shell=False)
