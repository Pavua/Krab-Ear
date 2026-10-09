"""Внутренние адаптеры файловых sinks; precheck не является разрешением записи."""

from typing import Callable, TypeVar

from backend.plaintext_export_authorization import (
    BackendSink,
    PlaintextExportAuthorizer,
    PlaintextExportDenied,
    REASON_PLAINTEXT_POLICY_UNAVAILABLE,
)

__all__ = ["BackendSink", "PlaintextExportDenied", "export_context", "precheck_export", "run_export_write"]

# None в Python API обозначает отсутствие namespace. Явный JSON null должен
# остаться malformed, а не превращаться в разрешённый OFF legacy-вызов.
_INVALID_CONTEXT = object()
_Result = TypeVar("_Result")


def export_context(params: object) -> object:
    """Достаёт namespace, различая absent и supplied null без его нормализации."""
    if type(params) is not dict:
        return _INVALID_CONTEXT
    if "plaintext_export" not in params:
        return None
    context = params["plaintext_export"]
    return _INVALID_CONTEXT if context is None else context


def precheck_export(authorizer: PlaintextExportAuthorizer | None, context: object, sink: BackendSink) -> None:
    """Нет authorizer/он сломан — fail-closed; нет cached/default OFF."""
    try:
        if authorizer is None or authorizer.precheck_backend_export(context, sink) is not None:
            raise PlaintextExportDenied(REASON_PLAINTEXT_POLICY_UNAVAILABLE)
    except PlaintextExportDenied:
        raise
    except Exception:
        raise PlaintextExportDenied(REASON_PLAINTEXT_POLICY_UNAVAILABLE) from None


def run_export_write(
    authorizer: PlaintextExportAuthorizer | None, context: object,
    sink: BackendSink, writer: Callable[[], _Result],
) -> _Result:
    """Одна fresh проверка и один writer вне locks, ошибки I/O не перехватываются."""
    precheck_export(authorizer, context, sink)
    return writer()
