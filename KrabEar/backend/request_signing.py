"""Подпись и верификация IPC-запросов Krab Ear.

Обеспечивает защиту IPC от несанкционированного доступа через HMAC-SHA256:
- Каждый запрос содержит nonce (случайный идентификатор) и timestamp.
- Повторные запросы (replay attacks) отклоняются через скользящее окно nonce'ов.
- Хранилище nonce'ов ограничено 1000 последними значениями (oldest-first eviction).

Использование::

    signer = RequestSigner()
    secret = signer.generate_secret()
    signed = signer.sign_request("ping", {}, secret)
    ok = signer.verify_request("ping", {}, signed.signature, secret,
                                signed.timestamp, signed.nonce)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Set


# ---------------------------------------------------------------------------
# Константы
# ---------------------------------------------------------------------------

# Временно́е окно (секунды): запросы старше этого значения отклоняются.
TIMESTAMP_WINDOW_SEC: int = 300  # 5 минут

# Максимальное число nonce'ов в памяти.
MAX_NONCES: int = 1000


# ---------------------------------------------------------------------------
# SignedRequest — dataclass для передачи подписанного запроса
# ---------------------------------------------------------------------------

@dataclass
class SignedRequest:
    """Подписанный IPC-запрос.

    Attributes:
        method: Имя IPC-метода (например, "ping").
        params: Словарь параметров запроса.
        signature: HMAC-SHA256 hex-строка.
        timestamp: Unix-время в секундах (float) на момент подписи.
        nonce: Случайная hex-строка (32 символа) для предотвращения replay.
    """

    method: str
    params: dict
    signature: str
    timestamp: float
    nonce: str = field(default_factory=lambda: secrets.token_hex(16))


# ---------------------------------------------------------------------------
# RequestSigner
# ---------------------------------------------------------------------------

class RequestSigner:
    """Генерация и верификация HMAC-SHA256 подписей для IPC-запросов.

    Потокобезопасен: все операции с множеством nonce'ов защищены lock'ом.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # deque используем как очередь для O(1) eviction (содержит (timestamp, nonce))
        self._seen_nonces: Deque[tuple[float, str]] = deque()
        self._nonce_set: Set[str] = set()

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    @staticmethod
    def generate_secret() -> str:
        """Генерирует 32-байтовый случайный секрет в виде hex-строки (64 символа)."""
        return secrets.token_hex(32)

    def sign_request(
        self,
        method: str,
        params: dict,
        secret: str,
        *,
        timestamp: float | None = None,
        nonce: str | None = None,
    ) -> SignedRequest:
        """Создаёт подписанный запрос.

        Args:
            method: Имя IPC-метода.
            params: Словарь параметров.
            secret: Общий секрет в виде hex-строки.
            timestamp: Unix-время (float). Если None — текущее время.
            nonce: Если None — генерируется автоматически через secrets.token_hex(16).

        Returns:
            SignedRequest с заполненными полями signature, timestamp, nonce.
        """
        if timestamp is None:
            timestamp = time.time()
        if nonce is None:
            nonce = secrets.token_hex(16)

        signature = self._compute_signature(method, params, secret, timestamp, nonce)
        return SignedRequest(
            method=method,
            params=params,
            signature=signature,
            timestamp=timestamp,
            nonce=nonce,
        )

    def verify_request(
        self,
        method: str,
        params: dict,
        signature: str,
        secret: str,
        timestamp: float | None = None,
        nonce: str | None = None,
    ) -> bool:
        """Верифицирует подпись запроса.

        Проверки выполняются в следующем порядке:
        1. Временно́е окно: |now - timestamp| <= TIMESTAMP_WINDOW_SEC.
        2. HMAC-SHA256 подпись совпадает (constant-time compare).
           Если подпись неверна — возвращаем False БЕЗ касания хранилища nonce'ов.
        3. Только если подпись верна — проверяем и регистрируем nonce (replay protection).

        Такой порядок предотвращает атаку на исчерпание nonce-хранилища:
        неаутентифицированный запрос с неверной подписью не расходует слот nonce.

        Args:
            method: Имя IPC-метода.
            params: Словарь параметров.
            signature: HMAC-SHA256 hex-строка из запроса.
            secret: Общий секрет в виде hex-строки.
            timestamp: Unix-время из запроса. Если None — проверка времени пропускается.
            nonce: Nonce из запроса. Если None — проверка уникальности пропускается.

        Returns:
            True если запрос прошёл все проверки, False иначе.
        """
        import math
        
        # 0. Защита от байпаса и type-based атак
        if timestamp is None or nonce is None or not signature:
            return False
        if not isinstance(signature, str) or not isinstance(nonce, str):
            return False
        if not isinstance(timestamp, (int, float)) or math.isnan(timestamp) or math.isinf(timestamp):
            return False

        # 1. Проверка временно́го окна (быстрый отсев устаревших запросов)
        now = time.time()
        if abs(now - timestamp) > TIMESTAMP_WINDOW_SEC:
            return False

        # 2. Вычисляем ожидаемую подпись и сравниваем constant-time.
        #    Хранилище nonce'ов НЕ трогаем до подтверждения подписи.
        expected = self._compute_signature(method, params, secret, timestamp, nonce)
        if not hmac.compare_digest(expected, signature):
            return False

        # 3. Подпись верна — теперь проверяем и регистрируем nonce (replay protection).
        #    Держим lock только на check-and-register, не во время хэширования.
        with self._lock:
            if nonce in self._nonce_set:
                return False  # replay attack
            self._register_nonce(nonce, now)

        return True

    # ------------------------------------------------------------------
    # Вспомогательные методы
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_signature(
        method: str,
        params: dict,
        secret: str,
        timestamp: float,
        nonce: str,
    ) -> str:
        """Вычисляет HMAC-SHA256 hex-подпись.

        Сообщение для подписи формируется как детерминированная JSON-строка:
            {"method": <method>, "nonce": <nonce>, "params": <params_sorted>, "timestamp": <ts_int>}

        timestamp округляется до целых секунд чтобы избежать флуктуаций float.
        params сериализуется с sort_keys=True для детерминизма.
        """
        message = json.dumps(
            {
                "method": method,
                "nonce": nonce,
                "params": params,
                "timestamp": int(timestamp),
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")

        key = secret.encode("utf-8")
        return hmac.new(key, message, hashlib.sha256).hexdigest()

    def _register_nonce(self, nonce: str, current_time: float) -> None:
        """Регистрирует nonce. Вызывать под self._lock.

        Удаляет все nonce, которые старше TIMESTAMP_WINDOW_SEC,
        чтобы предотвратить replay attack при вытеснении и предотвратить OOM.
        """
        # Вытесняем устаревшие nonce
        while self._seen_nonces and self._seen_nonces[0][0] < current_time - TIMESTAMP_WINDOW_SEC:
            _, oldest_nonce = self._seen_nonces.popleft()
            self._nonce_set.discard(oldest_nonce)

        # Ограничение на крайний случай, если слишком много запросов в окне (OOM protection)
        if len(self._seen_nonces) >= MAX_NONCES:
            _, oldest_nonce = self._seen_nonces.popleft()
            self._nonce_set.discard(oldest_nonce)

        self._seen_nonces.append((current_time, nonce))
        self._nonce_set.add(nonce)

    def clear_nonces(self) -> None:
        """Очищает хранилище nonce'ов. Используется в тестах."""
        with self._lock:
            self._seen_nonces.clear()
            self._nonce_set.clear()

    def nonce_count(self) -> int:
        """Возвращает количество запомненных nonce'ов (для отладки/тестов)."""
        with self._lock:
            return len(self._nonce_set)
