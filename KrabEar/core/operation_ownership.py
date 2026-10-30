"""Локальное владение REST → adapter subtree; не fence и не полный server drain.

Внешний callable может завершиться раньше дочернего executor. Его token живёт
до завершения всех зарегистрированных потомков. Регистрируем child ДО submit.
Вложенные pools самого адаптера (включая GigaAM _stuck_future) не охвачены.
"""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
import os
import threading
from typing import Any, Callable
import uuid
import weakref


_CURRENT: ContextVar[tuple | None] = ContextVar('ear_owned_operation', default=None)
_EXCLUSIONS = (
    'upload_auth_parse', 'unregistered_adapter_pools', 'gigaam_internal_stuck_future',
    'ru_ipc', 'native_ws', 'tts', 'batch', 'ui', 'internal_warmup',
)


@dataclass(frozen=True)
class OperationToken:
    epoch: str
    operation_id: str


@dataclass
class _Operation:
    token: OperationToken
    parent: str | None
    sequence: int
    children: set[str] = field(default_factory=set)
    callable_state: str = 'running'
    started: bool = False
    sealed: bool = False
    uncertain: bool = False
    future_ref: weakref.ReferenceType | None = None


class OwnedOperations:
    """Bounded metadata owner; admission всегда open, полной authority нет.

    running означает принятое владение (включая ещё не начатую задачу),
    execution_started отдельно отмечает вход wrapper. Не native GPU busy.
    При overflow вызов продолжается без token, coverage_lost остаётся True.
    Pending/unknown никогда не вытесняются; terminal также пока не compact-им.
    """

    def __init__(self, scope_id: str = 'rest_post_native_adapter', capacity: int = 256):
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1:
            raise ValueError('capacity must be a positive integer')
        self._scope_id = str(scope_id)
        self._capacity = capacity
        self._lock = threading.Lock()
        self._pid = os.getpid()
        self._epoch = uuid.uuid4().hex
        self._revision = 0
        self._sequence = 0
        self._operations: dict[str, _Operation] = {}
        self._coverage_lost = False
        if hasattr(os, 'register_at_fork'):
            reference = weakref.ref(self)

            def reset_child():
                owner = reference()
                if owner is not None:
                    owner._after_fork()
            os.register_at_fork(after_in_child=reset_child)

    def _after_fork(self) -> None:
        """Новый lock/epoch, без чтения унаследованного lock или очистки parent."""
        self._lock = threading.Lock()
        self._pid = os.getpid()
        self._epoch = uuid.uuid4().hex
        self._revision = 0
        self._sequence = 0
        self._operations = {}
        self._coverage_lost = True  # новый idle не аттестует старые owners

    def _ensure_process(self) -> None:
        if self._pid != os.getpid():
            self._after_fork()

    def _get_locked(self, token: OperationToken | None) -> _Operation | None:
        if not isinstance(token, OperationToken) or token.epoch != self._epoch:
            return None
        return self._operations.get(token.operation_id)

    def _lose_coverage_locked(self) -> None:
        if not self._coverage_lost:
            self._coverage_lost = True
            self._revision += 1

    def _uncertain_locked(self, operation: _Operation) -> None:
        if not operation.uncertain:
            operation.uncertain = True
            self._revision += 1

    def _new_locked(self, parent: _Operation | None) -> OperationToken | None:
        if len(self._operations) >= self._capacity:
            self._lose_coverage_locked()
            if parent is not None:
                self._uncertain_locked(parent)
            return None
        self._sequence += 1
        token = OperationToken(self._epoch, uuid.uuid4().hex)
        self._operations[token.operation_id] = _Operation(
            token, parent.token.operation_id if parent is not None else None, self._sequence,
        )
        if parent is not None:
            parent.children.add(token.operation_id)
        self._revision += 1
        return token

    def admit_root(self) -> OperationToken | None:
        self._ensure_process()
        with self._lock:
            return self._new_locked(None)

    def register_child(self, parent: OperationToken) -> OperationToken | None:
        self._ensure_process()
        with self._lock:
            operation = self._get_locked(parent)
            if operation is None:
                self._lose_coverage_locked()
                return None
            if operation.sealed:
                # Поздняя регистрация нарушает seal, но не блокирует обычный вызов.
                self._uncertain_locked(operation)
                self._lose_coverage_locked()
                return None
            return self._new_locked(operation)

    def mark_unknown(self, token: OperationToken | None) -> None:
        self._ensure_process()
        with self._lock:
            operation = self._get_locked(token)
            if operation is not None:
                self._uncertain_locked(operation)

    def run(self, token: OperationToken | None, fn: Callable, *args, **kwargs) -> Any:
        self._ensure_process()
        with self._lock:
            operation = self._get_locked(token)
            if operation is not None and not operation.started and not operation.sealed:
                operation.started = True
                self._revision += 1
                bound = token
            else:
                if operation is not None:
                    self._uncertain_locked(operation)
                bound = None
        context = _CURRENT.set((self, bound))
        try:
            return fn(*args, **kwargs)
        finally:
            _CURRENT.reset(context)
            if bound is not None:
                with self._lock:
                    operation = self._get_locked(bound)
                    if operation is not None:
                        operation.callable_state = 'completed'
                        operation.sealed = True
                        self._revision += 1

    def follow_future(self, token: OperationToken | None, future: Any) -> None:
        self._ensure_process()
        try:
            reference = weakref.ref(future)
        except TypeError:
            self.mark_unknown(token)
            return
        with self._lock:
            operation = self._get_locked(token)
            if operation is None:
                return
            if operation.future_ref is not None:
                # Один actual Future на operation; ни чужой, ни duplicate не rebind-ят.
                return
            operation.future_ref = reference
            self._revision += 1

        def done(completed):
            self._ensure_process()
            try:
                cancelled = completed.cancelled()
                finished = completed.done()
                with self._lock:
                    operation = self._get_locked(token)
                    if (operation is None or operation.future_ref is not reference
                            or reference() is not completed):
                        return
                    if cancelled is True and finished is True and not operation.started:
                        if not operation.sealed:
                            operation.callable_state = 'completed'
                            operation.sealed = True
                            self._revision += 1
                    elif finished is not True or operation.callable_state != 'completed':
                        # Нестандартный Future не заменяет фактический wrapper finally.
                        self._uncertain_locked(operation)
            except Exception:
                self.mark_unknown(token)
        try:
            future.add_done_callback(done)
        except Exception:
            self.mark_unknown(token)

    def _states_locked(self) -> dict[str, str]:
        # Child всегда добавлен позже parent: обратный порядок даёт O(n), без recursion.
        states: dict[str, str] = {}
        for key, operation in reversed(self._operations.items()):
            children = [states[child] for child in operation.children]
            if operation.uncertain or operation.callable_state == 'unknown' or 'unknown' in children:
                state = 'unknown'
            elif operation.callable_state == 'completed' and not operation.sealed:
                state = 'unknown'
            elif operation.callable_state != 'completed' or not operation.sealed or 'running' in children:
                state = 'running'
            else:
                state = 'completed'
            states[key] = state
        return states

    def snapshot(self) -> dict:
        self._ensure_process()
        with self._lock:
            states = self._states_locked()
            return {
                'schema_version': 1, 'scope_id': self._scope_id, 'owner_epoch': self._epoch,
                'revision': self._revision, 'scope_complete': False, 'admission': 'open',
                'fence_result': None, 'coverage_lost': self._coverage_lost,
                'exclusions': list(_EXCLUSIONS),
                'operations': [{
                    'operation_id': operation.token.operation_id,
                    'parent_operation_id': operation.parent, 'admission_seq': operation.sequence,
                    'callable_state': operation.callable_state,
                    'execution_started': operation.started,
                    'registration_sealed': operation.sealed,
                    'state': states[operation.token.operation_id],
                } for operation in self._operations.values()],
            }


def submit_owned(executor, fn: Callable, *args, **kwargs):
    """Тот же actual Future; контекст worker задаётся явно, не наследуется."""
    current = _CURRENT.get()
    if current is None or current[1] is None:
        return executor.submit(fn, *args, **kwargs)
    owner, parent = current
    child = owner.register_child(parent)
    try:
        future = executor.submit(owner.run, child, fn, *args, **kwargs)
    except BaseException:
        # Даже submit мог уже запустить worker: HTTP/outer exception не drain.
        owner.mark_unknown(child)
        raise
    owner.follow_future(child, future)
    return future
