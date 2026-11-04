"""Независимая synthetic проверка submit; не production admission."""

import os
import threading
import uuid

from core.maintenance_claim_journal import _SyntheticPermit


class SubmitRefused(RuntimeError):
    """Повторный или неподходящий synthetic permit."""


class SyntheticExecutor:
    """Неизменяемая кооперативная локальная привязка к executor."""

    __slots__ = ("_identity", "_executor", "_submit_method", "_creator_pid")

    def __init__(self, executor):
        object.__setattr__(self, "_identity", uuid.uuid4().hex)
        object.__setattr__(self, "_executor", executor)
        object.__setattr__(self, "_submit_method", executor.submit)
        object.__setattr__(self, "_creator_pid", os.getpid())

    def __setattr__(self, name, value):
        raise AttributeError("Synthetic executor handle неизменяем")

    @property
    def identity(self):
        return self._identity

    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        return self

    def __reduce_ex__(self, protocol):
        raise TypeError("Synthetic executor handle нельзя сериализовать")

    def _submit(self, fn):
        if os.getpid() != self._creator_pid:
            raise SubmitRefused("Synthetic executor не используется после fork")
        return self._submit_method(fn)


def submit_once(permit, scope, executor, fn):
    """Одна попытка submit; завершение и drain этим не подтверждаются."""
    if (type(permit) is not _SyntheticPermit or type(executor) is not SyntheticExecutor
            or type(scope) is not tuple or len(scope) != 5
            or executor.identity != scope[3] or executor._creator_pid != os.getpid()):
        raise SubmitRefused("Неизвестный synthetic permit или scope")
    if not permit.consume(*scope):
        raise SubmitRefused("Synthetic permit уже использован или не подходит")
    creator_pid = os.getpid()
    lock = threading.Lock()
    entered = False

    def run_once():
        nonlocal entered
        if os.getpid() != creator_pid:
            raise SubmitRefused("Synthetic leaf не исполняется после fork")
        with lock:
            if entered:
                raise SubmitRefused("Synthetic leaf уже запущен")
            entered = True
        return fn()

    return executor._submit(run_once)
