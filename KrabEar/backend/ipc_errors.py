# -*- coding: utf-8 -*-
"""Exception types that shape IPC dispatch error classification."""
from __future__ import annotations


class IpcOperationalError(RuntimeError):
    """A GENUINE operational failure (remote service down, disk/IO error) — must
    stay loud (internal_error + Sentry), NOT be downgraded to invalid_request.

    Subclasses RuntimeError so any existing `except RuntimeError` callers keep
    working; the dispatch catches IpcOperationalError BEFORE the generic
    (ValueError, RuntimeError) validation branch.
    """
