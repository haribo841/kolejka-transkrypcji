"""Distinguish terminal source defects from retryable access/settings failures."""
from .failures import resource_failure


def permanent_audio_failure(error, code):
    if resource_failure(error):
        return False
    message = str(error).casefold()
    if any(text in message for text in ("permission denied", "access is denied", "no such file", "nie znaleziono", "winerror")):
        return False
    if code == "audio_decode_failed":
        return True
    if code == "unsupported_codec":
        return True
    return any(text in message for text in (
        "uszkodzone źródło nie zawiera próbek możliwych do odzyskania",
        "audio zawiera niefinitywne wartości", "plik nie zawiera ścieżki audio",
        "ffprobe nie rozpoznaje kodeka audio")) or (message.startswith("ffprobe:") and any(text in message for text in (
            "invalid data found when processing input", "invalid header", "could not find codec parameters",
            "not yet implemented in ffmpeg")))
