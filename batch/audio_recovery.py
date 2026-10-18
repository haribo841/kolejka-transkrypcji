"""Retry a damaged source once, without claiming that recovered audio is complete."""
import shutil

from .common import ResourceError
from .failures import JobError, resource_failure

DIAGNOSTIC_BYTES = 2500


def decoder_diagnostic(error_file, code):
    """Keep a bounded tail even when FFmpeg has produced a large error log."""
    fallback = f"FFmpeg zakończył się kodem {code}." if code else ""
    if not error_file.is_file():
        return fallback
    with error_file.open("rb") as stream:
        stream.seek(0, 2)
        stream.seek(max(0, stream.tell() - DIAGNOSTIC_BYTES))
        message = stream.read(DIAGNOSTIC_BYTES).decode("utf-8", errors="replace").strip()
    return message or fallback


def check_decoder_resources(message):
    if resource_failure(RuntimeError(message)):
        raise ResourceError("FFmpeg: " + message)


def run_decoder(command, execute, error_file, emit):
    """Return a damage report after a successful tolerant retry, or None if clean.

    Callers must still validate the emitted samples. In particular an empty
    tolerant result is a decoding failure, never evidence of an empty recording.
    """
    code = execute(command)
    if not code:
        return None
    strict_error = decoder_diagnostic(error_file, code)
    check_decoder_resources(strict_error)
    if "-xerror" not in command:
        raise JobError("Nie udało się odczytać ścieżki audio: " + strict_error, "audio_decode_failed")
    strict_log = error_file.with_name(error_file.stem + "-strict" + error_file.suffix)
    if error_file.is_file():
        shutil.copyfile(error_file, strict_log)
    emit("Uszkodzone pakiety audio. Jedna próba odzyskania dostępnego dźwięku; wynik będzie częściowy.")
    recovery_command = [argument for argument in command if argument != "-xerror"]
    code = execute(recovery_command)
    recovery_error = decoder_diagnostic(error_file, code) if code else ""
    if code:
        check_decoder_resources(recovery_error)
        raise JobError("Nie udało się odzyskać ścieżki audio. Plik źródłowy może być uszkodzony. " +
                       recovery_error, "audio_decode_failed")
    if error_file.is_file():
        recovery_error = decoder_diagnostic(error_file, code)
    return {"code": "audio_decode_recovered", "coverage_unknown": True,
            "message": "Odzyskano dźwięk z uszkodzonego źródła. Nie można potwierdzić kompletności; sprawdź wynik częściowy.",
            "strict_error": strict_error, "recovery_error": recovery_error, "strict_log": strict_log.name}
