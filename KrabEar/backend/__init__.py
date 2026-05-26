"""Backend-пакет Krab Ear: IPC сервис, запись аудио и хранение истории.

Важно:
модуль не импортирует `service` на старте, чтобы утилиты отчётности могли
использовать `state_store` без обязательных аудио-зависимостей.
"""

__all__ = ["BackendService", "IPCServer"]


def __getattr__(name: str):
    if name == "BackendService":
        from .service import BackendService
        return BackendService
    if name == "IPCServer":
        from .ipc_server import IPCServer
        return IPCServer
    raise AttributeError(name)
