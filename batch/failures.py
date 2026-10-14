"""Separate failures of one recording from unavailable shared resources."""
import errno

from .common import ResourceError


class JobError(RuntimeError):
    def __init__(self, message, code="processing_failed"):
        super().__init__(message)
        self.code = code


def resource_failure(error):
    if isinstance(error, (ResourceError, MemoryError, ImportError)):
        return True
    if isinstance(error, OSError) and error.errno in {errno.ENOSPC, errno.EDQUOT, errno.ENOMEM}:
        return True
    if isinstance(error, OSError) and getattr(error, "winerror", None) in {8, 14, 112, 1455}:
        return True
    text = str(error).casefold()
    return any(term in text for term in ("no space left", "not enough space", "cannot allocate memory", "out of memory"))
