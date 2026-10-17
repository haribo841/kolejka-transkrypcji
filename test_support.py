"""Helpers for completing asynchronous UI requests within test mock scopes."""
from __future__ import annotations

import queue


def wait_for_start(app, timeout=5):
    """Finish a start check and deliver its event on the calling Tk thread."""
    thread = app.start_check_thread
    if thread is not None:
        thread.join(timeout)
        if thread.is_alive():
            raise AssertionError("Start configuration check did not finish")
    while True:
        try:
            event = app.events.get_nowait()
        except queue.Empty:
            break
        kind, value = event[:2]
        note = event[2] if len(event) == 3 and event[2] is not None else app.source_note
        app.handle_event(kind, value, note)
