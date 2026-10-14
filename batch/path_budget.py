"""Portable budgets for generated paths passed to native Windows tools."""
from __future__ import annotations

import os
import re

PATH_LIMIT = 240


def path_units(value):
    """Count Windows WCHAR units, including both halves of non-BMP characters."""
    return len(os.fspath(value).encode("utf-16-le", errors="surrogatepass")) // 2


def truncate_units(text, limit):
    """Keep complete characters while staying within a UTF-16 unit budget."""
    kept, used = [], 0
    for character in str(text):
        size = path_units(character)
        if used + size > limit:
            break
        kept.append(character)
        used += size
    return "".join(kept)


def fit_slug(text, limit):
    """Sanitize a title with a fallback that also fits the requested budget."""
    if limit < 1:
        raise ValueError("Brak miejsca na nazwę nagrania w ścieżce wyników.")
    sanitized = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(text)).strip(" .")
    shortened = truncate_units(sanitized, limit).rstrip(" .")
    return shortened or truncate_units("nagranie", limit)
