"""Synthetic наблюдение одного leaf; не durable terminal и не drain."""

from concurrent.futures import Future, ThreadPoolExecutor
import os
import threading
from types import MethodType

from core.maintenance_synthetic_submit import SyntheticExecutor, SubmitRefused, submit_once

_STANDARD_SUBMIT = ThreadPoolExecutor.submit


class LeafObservation:
    """Наблюдение wrapper и точного Future стандартного thread pool."""

    def __init__(self):
        self._pid = os.getpid()
        self._lock = threading.Lock()
        self._attempted = False
        self._unknown = False
        self._entered = False
        self._finished = False
        self._future = None

    def start(self, permit, scope, handle, fn):
        if os.getpid() != self._pid:
            raise SubmitRefused("Наблюдение нельзя использовать после fork")
        with self._lock:
            if self._attempted:
                raise SubmitRefused("Наблюдение уже использовано")
            self._unknown = True
            self._attempted = True
        try:
            if (type(handle) is not SyntheticExecutor or type(handle._executor) is not ThreadPoolExecutor
                    or type(handle._submit_method) is not MethodType
                    or getattr(handle._submit_method, "__self__", None) is not handle._executor
                    or getattr(handle._submit_method, "__func__", None) is not _STANDARD_SUBMIT):
                raise SubmitRefused("Поддерживается только стандартный synthetic thread pool")

            def leaf():
                with self._lock:
                    self._entered = True
                try:
                    return fn()
                finally:
                    with self._lock:
                        self._finished = True

            future = submit_once(permit, scope, handle, leaf)
            if type(future) is not Future:
                raise SubmitRefused("Неизвестный Future")
            with self._lock:
                self._future = future
                self._unknown = False
            return future
        except BaseException:
            with self._lock:
                self._unknown = True
            raise

    def status(self):
        if os.getpid() != self._pid:
            return {"state": "UNKNOWN", "drain_authority": False}
        with self._lock:
            if self._unknown:
                state = "UNKNOWN"
            elif not self._attempted:
                state = "NOT_STARTED"
            elif self._future is None or not self._future.done():
                state = "PENDING"
            elif self._future.cancelled() and not self._entered:
                state = "OBSERVED_CANCELLED"
            elif self._finished:
                state = "OBSERVED_FINISHED"
            else:
                state = "UNKNOWN"
        return {"state": state, "drain_authority": False}
