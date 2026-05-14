"""LLM rewriter для Krab Ear — пост-процессинг транскрипта через локальный LM Studio.

Модуль содержит:
- CircuitBreaker: state machine (CLOSED → OPEN → HALF_OPEN) с exponential backoff
- LLMRewriteResult: dataclass-результат попытки rewrite'а
- LLMRewriter: HTTP-клиент к OpenAI-compatible endpoint'у

Контракт LLMRewriter.rewrite(): НИКОГДА не raises, всегда возвращает LLMRewriteResult.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass

import requests
from enum import Enum
from typing import Callable, Optional

# Profiler singleton — защищаемся от ImportError чтобы llm_rewriter оставался standalone.
try:
    from backend.performance_profiler import profiler as _profiler
except Exception:  # pragma: no cover — defensive
    class _NoOpSpan:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class _NoOpProfiler:
        def start_span(self, name: str):
            return _NoOpSpan()

    _profiler = _NoOpProfiler()  # type: ignore[assignment]

logger = logging.getLogger("KrabEar.Backend.LLMRewriter")


class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """3-state circuit breaker с exponential backoff.

    ВАЖНО — контракт вызывающей стороны: если allow_request() вернул True
    в состоянии HALF_OPEN, вызывающий ОБЯЗАН затем вызвать record_success()
    или record_failure() (обернуть в try/finally). Иначе флаг пробы
    останется поднятым навсегда и circuit никогда не восстановится без
    рестарта процесса. LLMRewriter.rewrite() гарантирует это через свой
    "never raises" контракт.

    Thread safety: не требуется — IPC server в Krab Ear однопоточный.
    Если появится multi-threaded access, обернуть в threading.Lock.
    """

    def __init__(
        self,
        fail_threshold: int,
        initial_reset_sec: int,
        max_reset_sec: int = 600,
    ):
        self._fail_threshold = fail_threshold
        self._initial_reset_sec = initial_reset_sec
        self._max_reset_sec = max_reset_sec
        self._current_reset_sec = initial_reset_sec
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._opened_at: Optional[float] = None
        self._half_open_probe_in_flight = False

    @property
    def state(self) -> str:
        """Публичное имя состояния ('closed' | 'open' | 'half_open')."""
        return self._state.value

    def allow_request(self) -> bool:
        """Можно ли сейчас делать HTTP запрос?"""
        if self._state == CircuitState.CLOSED:
            return True

        if self._state == CircuitState.OPEN:
            if self._opened_at is None:
                return False
            elapsed = time.monotonic() - self._opened_at
            if elapsed >= self._current_reset_sec:
                self._transition_to(CircuitState.HALF_OPEN)
                self._half_open_probe_in_flight = True
                return True
            return False

        if self._state == CircuitState.HALF_OPEN:
            if self._half_open_probe_in_flight:
                return False
            self._half_open_probe_in_flight = True
            return True

        return False

    def record_success(self):
        self._half_open_probe_in_flight = False
        if self._state == CircuitState.HALF_OPEN:
            logger.info("Circuit breaker: HALF_OPEN -> CLOSED (проба успешна)")
            self._transition_to(CircuitState.CLOSED)
        self._consecutive_failures = 0

    def record_failure(self):
        self._half_open_probe_in_flight = False
        self._consecutive_failures += 1

        if self._state == CircuitState.HALF_OPEN:
            self._current_reset_sec = min(self._current_reset_sec * 2, self._max_reset_sec)
            logger.warning(
                "Circuit breaker: HALF_OPEN -> OPEN (проба провалилась), cooldown теперь %d сек",
                self._current_reset_sec,
            )
            self._transition_to(CircuitState.OPEN)
            return

        if (
            self._state == CircuitState.CLOSED
            and self._consecutive_failures >= self._fail_threshold
        ):
            logger.warning(
                "Circuit breaker: CLOSED -> OPEN (%d fails подряд), cooldown %d сек",
                self._consecutive_failures,
                self._current_reset_sec,
            )
            self._transition_to(CircuitState.OPEN)

    def _transition_to(self, new_state: CircuitState):
        self._state = new_state
        if new_state == CircuitState.OPEN:
            self._opened_at = time.monotonic()
            self._consecutive_failures = 0
        elif new_state == CircuitState.CLOSED:
            self._opened_at = None
            self._consecutive_failures = 0
            self._current_reset_sec = self._initial_reset_sec
            self._half_open_probe_in_flight = False


_PUNCTUATION_SYSTEM_PROMPTS = {
    "ru": (
        "Ты редактор пунктуации. Тебе дают сырой STT-текст без запятых. "
        "Добавь только запятые, точки, тире, вопросительные/восклицательные знаки. "
        "ЗАПРЕЩЕНО менять или удалять любые слова. "
        "ЗАПРЕЩЕНО менять регистр (кроме первой буквы предложения после точки). "
        "Верни тот же текст с пунктуацией. Без пояснений. Без кавычек."
    ),
    "es": (
        "Eres un editor de puntuación. Se te da un texto STT sin comas. "
        "Añade únicamente comas, puntos, guiones, signos de interrogación/exclamación. "
        "PROHIBIDO cambiar o eliminar palabras. "
        "PROHIBIDO cambiar mayúsculas (excepto la primera letra tras un punto). "
        "Devuelve el mismo texto con puntuación. Sin explicaciones. Sin comillas."
    ),
    "en": (
        "You are a punctuation editor. You are given raw STT text without commas. "
        "Add only commas, periods, dashes, question marks, and exclamation marks. "
        "FORBIDDEN to change or delete any words. "
        "FORBIDDEN to change capitalisation (except the first letter after a period). "
        "Return the same text with punctuation. No explanations. No quotes."
    ),
}

SYSTEM_PROMPT = """Ты — редактор диктовки. Твоя задача — исправить пунктуацию, орфографию и грамматику, сохранив смысл и стиль автора.

Жёсткие правила:
1. НЕ добавляй слов, которых нет в оригинале.
2. НЕ удаляй слов, кроме (а) явных filler'ов в начале ("э-э", "ну", "вот") и (б) немедленных повторов от re-articulation: если человек переспрашивает слово сразу же ("записываю уже, уже" → "записываю уже"; "слово, слово" → "слово"; "вот сейчас, вот сейчас" → "вот сейчас"), оставляй ОДНО вхождение. Не путай с риторическими повторами и emphasis ("очень очень важно" → оставь как есть).
3. НЕ меняй порядок слов, кроме случаев когда этого требует грамматика.
4. НЕ переформулируй фразы — только исправляй ошибки.
5. СОХРАНЯЙ язык ввода — НЕ переводи между языками. Если вход на испанском — выход на испанском. Если на английском — на английском.
6. Бренды и технические термины оставляй латиницей: Spotify, YouTube, GitHub, Claude, OpenAI, Docker, Python, Swift, macOS, iPhone, iPad, Mac, Telegram, WhatsApp, Slack, Notion, Figma, VS Code, Xcode, Linux, Linear, Jira, Qwen, MLX, GigaAM, Krab Ear.
7. Расставь правильные знаки препинания: запятые, точки, тире, двоеточия.
8. Заглавные буквы в начале предложений и у имён собственных.
9. Если текст пустой или бессмысленный — верни его без изменений.
10. Не используй <think> теги или reasoning chains.

Верни ТОЛЬКО исправленный текст. Без пояснений. Без кавычек. Без префиксов типа "Исправленный текст:"."""

_QUOTE_OPENERS = ('"', "«", "\u201c")
_QUOTE_CLOSERS = ('"', "»", "\u201d")
_EXPLANATORY_PREFIXES = (
    "Исправленный текст:",
    "Исправлено:",
    "Результат:",
    "Вот:",
)

# Chatbot response markers — if LLM output starts with any of these,
# the model switched to assistant mode instead of editing the text.
_CHATBOT_MARKERS = (
    "извините",
    "пожалуйста, укажите",
    "пожалуйста, предоставьте",
    "как я могу",
    "чем могу помочь",
    "к сожалению",
    "я не могу",
    "i'm sorry",
    "i apologize",
    "here is",
    "sure,",
    "конечно,",
    "вот исправленный",
)


@dataclass
class LLMRewriteResult:
    """Результат попытки rewrite'а. Всегда возвращается, никогда не raises."""

    ok: bool
    text: str | None
    fallback_reason: str | None
    latency_ms: Optional[int]

    def text_or_fallback(self, fallback: str) -> str:
        """Helper: вернуть rewritten text если ok=True и text непустой, иначе fallback."""
        if self.ok and self.text:
            return self.text
        return fallback


class LLMRewriter:
    """HTTP-клиент к OpenAI-compatible LLM endpoint'у (LM Studio).

    Контракт: rewrite() НИКОГДА не raises. Все ошибки возвращаются как
    LLMRewriteResult(ok=False, fallback_reason=...).

    Connection pooling via requests.Session() для переиспользования TCP соединений
    и снижения латентности на 15-20ms per call.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout_sec: float = 45.0,  # was 4.0 → raised to 15.0 for JIT cold load; now 45.0 for vision multimodal (gemma-4-E4B-it-MLX-4bit + vision add-on cold load ~20-30s on M-series from external SSD)
        circuit_fail_threshold: int = 3,
        circuit_initial_reset_sec: int = 60,
        circuit_max_reset_sec: int = 600,
        idle_keepalive_enabled: bool = False,  # default OFF: модель естественно выгружается через LM Studio TTL чтобы не держать RAM. Включается через settings.LLM_IDLE_KEEPALIVE_ENABLED.
        idle_keepalive_sec: int = 1500,  # 25 min — LM Studio default idle TTL = 30 min
        runtime_timeout_provider: Optional[Callable[[], float]] = None,  # если задан — читается перед каждым HTTP-запросом вместо fallback timeout
    ):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._fallback_timeout = timeout_sec  # статический fallback (init-time value)
        self._runtime_timeout_provider = runtime_timeout_provider
        self._circuit = CircuitBreaker(
            fail_threshold=circuit_fail_threshold,
            initial_reset_sec=circuit_initial_reset_sec,
            max_reset_sec=circuit_max_reset_sec,
        )
        self._last_latency_ms: Optional[int] = None
        self._last_error: str | None = None
        # Connection pooling: переиспользуем TCP соединение между запросами
        self._session = requests.Session()
        # Serialise ALL POST /v1/chat/completions calls — LM Studio JIT-loads the model
        # on the first concurrent request and cannot handle parallel POSTs during cold load
        # (returns "Unexpected endpoint or method. Returning 200 anyway" + Channel Errors).
        self._post_lock = threading.Lock()
        # Idle keepalive — пингуем модель раз в 25 минут чтобы LM Studio не выгружал её
        # из памяти по idle TTL (30 min default). Cold reload на gemma-4-e4b-it-mlx ~20-30s,
        # триггерит intermittent timeouts. Daemon thread, проглатывает ошибки.
        self._idle_keepalive_sec = idle_keepalive_sec
        self._idle_keepalive_enabled = idle_keepalive_enabled
        self._shutdown_event = threading.Event()
        if idle_keepalive_enabled:
            self._idle_keepalive_thread: Optional[threading.Thread] = threading.Thread(
                target=self._idle_keepalive_loop,
                name="LLMRewriter-IdleKeepalive",
                daemon=True,
            )
            self._idle_keepalive_thread.start()
        else:
            self._idle_keepalive_thread = None

    @property
    def _timeout(self) -> float:
        """Effective timeout — читается из runtime provider при каждом вызове.

        Если provider задан и вернул корректное значение > 0 — используем его.
        Иначе fallback к значению, переданному при __init__.
        """
        if self._runtime_timeout_provider is not None:
            try:
                val = float(self._runtime_timeout_provider())
                if val > 0:
                    return val
            except Exception:
                pass
        return self._fallback_timeout

    @_timeout.setter
    def _timeout(self, value: float) -> None:
        """Setter для совместимости с tests, которые assign'ят `_timeout = X`.

        Записывается в `_fallback_timeout` (init-time fallback). Если provider
        задан — он всё равно read'ится первым, но если вернёт invalid — этот
        новый fallback используется. Без setter @property raises AttributeError
        на assignment, ломая legacy test setups.
        """
        self._fallback_timeout = float(value)

    def _idle_keepalive_loop(self) -> None:
        """Фоновый loop: каждые idle_keepalive_sec вызывает warmup_probe чтобы
        предотвратить выгрузку модели из памяти LM Studio по idle TTL.

        Использует _shutdown_event.wait() — корректно завершается при close().
        Ошибки warmup_probe проглатываются (они уже логируются внутри).
        """
        logger.info(
            "LLM idle keepalive started: interval=%ds model=%s",
            self._idle_keepalive_sec, self._model,
        )
        while not self._shutdown_event.wait(self._idle_keepalive_sec):
            try:
                result = self.warmup_probe(timeout_sec=60.0)
                logger.info(
                    "LLM idle keepalive ping: ok=%s latency_ms=%s model=%s",
                    result.get("ok"), result.get("latency_ms"), self._model,
                )
            except Exception as exc:  # never raise from keepalive
                logger.warning("LLM idle keepalive failed: %s", exc)
        logger.info("LLM idle keepalive stopped")

    def _lm_studio_headers(self) -> dict:
        """Build HTTP headers for LM Studio POST requests.

        Includes ``Authorization: Bearer <token>`` only when api_key is set.
        Empty api_key: no Authorization header (backward-compat with LM Studio < 0.3
        that did not require authentication).
        """
        headers: dict = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def _lm_studio_get_headers(self) -> dict:
        """Build HTTP headers for GET requests (no Content-Type needed)."""
        headers: dict = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def _push_error(self, code: str, message_debug: str, severity: str = "warn") -> None:
        """Push KrabError to attached ErrorBus if available. Late-injected attribute."""
        error_bus = getattr(self, "_error_bus", None)
        if error_bus is None:
            return
        try:
            from backend.error_bus import KrabError
            from backend.error_codes import ERROR_REGISTRY
            from datetime import datetime, timezone
            entry = ERROR_REGISTRY.get(code, {})
            err = KrabError(
                severity=severity,
                component="rewriter",
                code=code,
                message_user=entry.get("user_msg_ru", "Rewriter ошибка"),
                message_debug=message_debug,
                timestamp=datetime.now(timezone.utc),
                context={"model": self._model, "base_url": self._base_url},
                actionable=entry.get("actionable", False),
                action_id=entry.get("action_id"),
            )
            error_bus.push(err)
        except Exception:  # never raise from rewriter
            logger.exception("error_bus.push failed for code=%s", code)

    def _postprocess(self, content: str) -> str:
        """Убирает типичный мусор в ответе LLM (кавычки, префиксы, multi-paragraph)."""
        s = (content or "").strip()
        if not s:
            return ""

        if len(s) >= 2 and s[0] in _QUOTE_OPENERS and s[-1] in _QUOTE_CLOSERS:
            s = s[1:-1].strip()

        for prefix in _EXPLANATORY_PREFIXES:
            if s.lower().startswith(prefix.lower()):
                s = s[len(prefix):].strip()
                break

        if "\n\n" in s:
            s = s.split("\n\n", 1)[0].strip()

        return s

    def _estimate_max_tokens(self, text: str) -> int:
        """Динамический output cap на базе длины input'а.

        Русский ~2.5-3 токена на слово, output ≈ input по длине.
        30% headroom + 50 токенов буфера на знаки препинания.
        Floor 256 — достаточно для моделей без reasoning (qwen3-4b-abliterated).
        """
        word_count = len((text or "").split())
        input_tokens_estimate = word_count * 3
        max_tokens = int(input_tokens_estimate * 1.3) + 50
        return max(256, min(max_tokens, 4096))

    def _build_messages(self, text: str) -> list:
        return [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ]

    def rewrite(self, text: str) -> LLMRewriteResult:
        """Отправляет текст в LLM и возвращает исправленную версию.

        Контракт: НИКОГДА не raises. Все ошибки — через LLMRewriteResult.ok=False.
        """
        with _profiler.start_span("llm_rewrite"):
            return self._rewrite_impl(text)

    def _rewrite_impl(self, text: str) -> LLMRewriteResult:
        """Внутренняя реализация rewrite() — обёрнута профайлером в rewrite()."""
        # 1. Валидация входа
        cleaned_input = (text or "").strip()
        if not cleaned_input:
            return LLMRewriteResult(
                ok=False, text=None, fallback_reason="empty_input", latency_ms=None
            )

        # 2. Circuit breaker check
        if not self._circuit.allow_request():
            return LLMRewriteResult(
                ok=False, text=None, fallback_reason="circuit_open", latency_ms=None
            )

        # 2a. Testing hook: KRAB_EAR_LLM_FORCE_TIMEOUT simulates timeout without HTTP call
        if os.getenv("KRAB_EAR_LLM_FORCE_TIMEOUT") == "1":
            self._circuit.record_failure()
            self._last_error = "timeout"
            self._push_error("rewriter.timeout", "forced via KRAB_EAR_LLM_FORCE_TIMEOUT")
            return LLMRewriteResult(
                ok=False, text=None, fallback_reason="timeout", latency_ms=None
            )

        # 3. Подготовка запроса
        payload = {
            "model": self._model,
            "messages": self._build_messages(cleaned_input),
            "temperature": 0.0,
            "max_tokens": self._estimate_max_tokens(cleaned_input),
            "stream": False,
            # Убран "\n\n" из stop — qwen3.5 с reasoning mode ставит \n\n
            # между thinking и ответом, что обрезало content до пустоты.
            "stop": [
                "Исправленный текст:",
                "Исходный текст:",
                "<end_of_turn>",
                "<start_of_turn>",
                "</s>",
            ],
            "tool_choice": "none",
        }
        headers = self._lm_studio_headers()

        # 4. HTTP call with timing — serialised via _post_lock (LM Studio JIT-load safety)
        start = time.monotonic()
        try:
            with self._post_lock:
                response = self._session.post(
                    f"{self._base_url}/chat/completions",
                    json=payload,
                    headers=headers,
                    timeout=self._timeout,
                )
        except requests.Timeout:
            self._circuit.record_failure()
            self._last_error = "timeout"
            logger.warning(
                "LLM rewriter failure: kind=timeout model=%s base_url=%s elapsed_ms=%s",
                self._model, self._base_url, int(self._timeout * 1000),
            )
            self._push_error("rewriter.timeout", f"timeout after {self._timeout}s")
            return LLMRewriteResult(
                ok=False, text=None, fallback_reason="timeout", latency_ms=None
            )
        except (requests.ConnectionError, requests.RequestException) as exc:
            self._circuit.record_failure()
            exc_str = str(exc)
            self._last_error = f"connection_error: {exc_str}"
            logger.warning(
                "LLM rewriter failure: kind=connection_error model=%s base_url=%s elapsed_ms=%s exc=%s",
                self._model, self._base_url, int((time.monotonic() - start) * 1000),
                type(exc).__name__,
            )
            if "channel error" in exc_str.lower():
                self._push_error(
                    "rewriter.channel_error",
                    f"{type(exc).__name__}: {exc_str[:500]}",
                )
            else:
                self._push_error("rewriter.connection_error", f"{type(exc).__name__}: {exc_str}")
            return LLMRewriteResult(
                ok=False, text=None, fallback_reason="connection_error", latency_ms=None
            )

        latency_ms = int((time.monotonic() - start) * 1000)
        self._last_latency_ms = latency_ms

        # 5. HTTP status check — JIT retry on 503 (LM Studio model cold loading)
        if response.status_code == 503:
            logger.warning(
                "LLM rewriter: 503 from LM Studio (JIT cold load), waiting 10s before retry "
                "model=%s base_url=%s",
                self._model, self._base_url,
            )
            time.sleep(10)
            start = time.monotonic()
            try:
                with self._post_lock:
                    response = self._session.post(
                        f"{self._base_url}/chat/completions",
                        json=payload,
                        headers=headers,
                        timeout=self._timeout,
                    )
            except requests.Timeout:
                self._circuit.record_failure()
                self._last_error = "timeout"
                logger.warning(
                    "LLM rewriter failure: kind=timeout model=%s base_url=%s elapsed_ms=%s",
                    self._model, self._base_url, int(self._timeout * 1000),
                )
                self._push_error("rewriter.timeout", f"timeout after {self._timeout}s (503 retry)")
                return LLMRewriteResult(
                    ok=False, text=None, fallback_reason="timeout", latency_ms=None
                )
            except (requests.ConnectionError, requests.RequestException) as exc:
                self._circuit.record_failure()
                exc_str = str(exc)
                self._last_error = f"connection_error: {exc_str}"
                logger.warning(
                    "LLM rewriter failure: kind=connection_error model=%s base_url=%s elapsed_ms=%s exc=%s",
                    self._model, self._base_url, int((time.monotonic() - start) * 1000),
                    type(exc).__name__,
                )
                if "channel error" in exc_str.lower():
                    self._push_error(
                        "rewriter.channel_error",
                        f"{type(exc).__name__}: {exc_str[:500]} (503 retry)",
                    )
                else:
                    self._push_error("rewriter.connection_error", f"{type(exc).__name__}: {exc_str} (503 retry)")
                return LLMRewriteResult(
                    ok=False, text=None, fallback_reason="connection_error", latency_ms=None
                )
            latency_ms = int((time.monotonic() - start) * 1000)
            self._last_latency_ms = latency_ms

        # 5a. mlx_lm 0.31.3 bundled bug: HTTP 500 with UnboundLocalError on 'token'
        if response.status_code == 500 and "cannot access local variable 'token'" in response.text:
            logger.warning(
                "LM Studio mlx_lm token UnboundLocalError detected, retrying once "
                "model=%s base_url=%s",
                self._model, self._base_url,
            )
            start = time.monotonic()
            try:
                with self._post_lock:
                    response = self._session.post(
                        f"{self._base_url}/chat/completions",
                        json=payload,
                        headers=headers,
                        timeout=self._timeout,
                    )
                latency_ms = int((time.monotonic() - start) * 1000)
                self._last_latency_ms = latency_ms
            except (requests.Timeout, requests.ConnectionError, requests.RequestException):
                pass
            if response.status_code == 500 and "cannot access local variable 'token'" in response.text:
                self._push_error(
                    "rewriter.mlx_token_bug",
                    "mlx_lm UnboundLocalError 'token' persists after retry",
                    severity="warn",
                )

        if response.status_code == 401:
            self._circuit.record_failure()
            self._last_error = "unauthorized"
            logger.warning(
                "LLM rewriter failure: kind=unauthorized model=%s base_url=%s "
                "— LM Studio requires API token (v0.3+). Set LM_STUDIO_API_KEY in settings.",
                self._model, self._base_url,
            )
            self._push_error(
                "rewriter.unauthorized",
                "HTTP 401: LM Studio requires Bearer token. Set lm_studio_api_key in settings "
                "or disable authentication in LM Studio Server Settings.",
                severity="error",
            )
            return LLMRewriteResult(
                ok=False, text=None, fallback_reason="unauthorized", latency_ms=latency_ms
            )
        if response.status_code != 200:
            self._circuit.record_failure()
            self._last_error = f"http_{response.status_code}"
            body_preview = (response.text or "")[:120]
            logger.warning(
                "LLM rewriter failure: kind=http_error model=%s base_url=%s elapsed_ms=%s status=%s body=%s",
                self._model, self._base_url, latency_ms, response.status_code, body_preview,
            )
            if "channel error" in body_preview.lower():
                self._push_error(
                    "rewriter.channel_error",
                    f"http_{response.status_code}: {body_preview}",
                )
            else:
                self._push_error("rewriter.timeout", f"http_{response.status_code}_after_retry")
            return LLMRewriteResult(
                ok=False,
                text=None,
                fallback_reason=f"http_{response.status_code}",
                latency_ms=latency_ms,
            )

        # 6. Parse JSON response
        try:
            data = response.json()
            message = data["choices"][0]["message"]
            content = message.get("content") if isinstance(message, dict) else message["content"]
            # 6a. tool_calls_emitted guard — gemma-4 / tool-capable models leak tool_calls
            if not content and isinstance(message, dict) and message.get("tool_calls"):
                self._circuit.record_failure()
                self._last_error = "tool_calls_emitted"
                logger.warning(
                    "LLM emitted tool_calls instead of content model=%s base_url=%s",
                    self._model, self._base_url,
                )
                self._push_error(
                    "rewriter.tool_calls_emitted",
                    f"model={self._model} emitted tool_calls instead of text content",
                )
                return LLMRewriteResult(
                    ok=False, text=None, fallback_reason="tool_calls_emitted", latency_ms=latency_ms
                )
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            self._circuit.record_failure()
            self._last_error = f"parse_error: {exc}"
            self._push_error("rewriter.parse_error", f"parse_error: {exc}")
            return LLMRewriteResult(
                ok=False, text=None, fallback_reason="parse_error", latency_ms=latency_ms
            )

        # 7. Postprocess
        cleaned = self._postprocess(content or "")
        if not cleaned:
            self._circuit.record_failure()
            self._last_error = "empty_response"
            self._push_error(
                "rewriter.empty_response",
                f"model={self._model} returned empty content",
            )
            return LLMRewriteResult(
                ok=False, text=None, fallback_reason="empty_response", latency_ms=latency_ms
            )

        # 8. Chatbot detection — model answered as assistant instead of editing
        cleaned_lower = cleaned.lower()
        for marker in _CHATBOT_MARKERS:
            if cleaned_lower.startswith(marker):
                logger.warning(
                    "LLM chatbot detected (starts with '%s'), falling back to original",
                    marker,
                )
                self._last_error = "chatbot_response"
                return LLMRewriteResult(
                    ok=False, text=None, fallback_reason="chatbot_response", latency_ms=latency_ms
                )

        # 9. Length ratio guard — dramatic shrink/expansion = hallucination
        input_len = len(cleaned_input)
        output_len = len(cleaned)
        if input_len > 20:
            ratio = output_len / input_len
            if ratio < 0.35:
                logger.warning(
                    "LLM output too short (%.0f%% of input), falling back to original",
                    ratio * 100,
                )
                self._last_error = "output_too_short"
                return LLMRewriteResult(
                    ok=False, text=None, fallback_reason="output_too_short", latency_ms=latency_ms
                )
            if ratio > 3.0:
                logger.warning(
                    "LLM output too long (%.0f%% of input), falling back to original",
                    ratio * 100,
                )
                self._last_error = "output_too_long"
                return LLMRewriteResult(
                    ok=False, text=None, fallback_reason="output_too_long", latency_ms=latency_ms
                )

        # 10. Success
        self._circuit.record_success()
        self._last_error = None
        return LLMRewriteResult(
            ok=True, text=cleaned, fallback_reason=None, latency_ms=latency_ms
        )

    def fix_punctuation_only(self, text: str, language: str = "ru") -> str | None:
        """Минимальный LLM pass: только пунктуация, слова не меняются.

        В отличие от rewrite(), который допускает лёгкую редактуру, этот метод
        применяет строгие word-set и word-count guards: любое расхождение слов
        означает, что LLM что-то изменил, и результат отвергается.

        Контракт: НИКОГДА не raises. Возвращает строку с пунктуацией или None.
        - None если circuit open, LM Studio недоступен, слова изменились,
          количество слов изменилось, или любая другая ошибка.
        - Пустой input (после strip) → возвращает оригинальный text без изменений.
        """
        cleaned_input = (text or "").strip()
        if not cleaned_input:
            return text

        if not self._circuit.allow_request():
            logger.debug("fix_punctuation_only: circuit open, skip")
            return None

        lang_key = (language or "ru").lower()
        if lang_key not in _PUNCTUATION_SYSTEM_PROMPTS:
            lang_key = "ru"
        system_prompt = _PUNCTUATION_SYSTEM_PROMPTS[lang_key]

        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": cleaned_input},
            ],
            "temperature": 0.0,
            "max_tokens": self._estimate_max_tokens(cleaned_input),
            "stream": False,
        }
        headers = self._lm_studio_headers()

        start = time.monotonic()
        try:
            with self._post_lock:
                response = self._session.post(
                    f"{self._base_url}/chat/completions",
                    json=payload,
                    headers=headers,
                    timeout=self._timeout,
                )
        except requests.Timeout:
            self._circuit.record_failure()
            logger.debug("fix_punctuation_only: timeout")
            return None
        except (requests.ConnectionError, requests.RequestException) as exc:
            self._circuit.record_failure()
            logger.debug("fix_punctuation_only: connection error: %s", exc)
            return None

        latency_ms = int((time.monotonic() - start) * 1000)
        self._last_latency_ms = latency_ms

        if response.status_code != 200:
            self._circuit.record_failure()
            logger.debug("fix_punctuation_only: http %d", response.status_code)
            return None

        try:
            data = response.json()
            content = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            self._circuit.record_failure()
            logger.debug("fix_punctuation_only: parse error: %s", exc)
            return None

        result_text = self._postprocess(content)
        if not result_text:
            self._circuit.record_failure()
            return None

        # Word-count guard: word count must be identical (punctuation adds no words)
        input_words = cleaned_input.split()
        result_words = result_text.split()
        if len(input_words) != len(result_words):
            logger.warning(
                "fix_punctuation_only: word count mismatch (%d -> %d), rejecting",
                len(input_words), len(result_words),
            )
            return None

        # Word-set guard: exact vocabulary must match (case-insensitive, strip punct)
        _punct_chars = ".,!?;:-—"
        input_set = {w.lower().strip(_punct_chars) for w in input_words}
        result_set = {w.lower().strip(_punct_chars) for w in result_words}
        if input_set != result_set:
            logger.warning(
                "fix_punctuation_only: word set mismatch, LLM changed words, rejecting"
            )
            return None

        self._circuit.record_success()
        logger.info(
            "fix_punctuation_only: ok, %d words, %d ms", len(result_words), latency_ms
        )
        return result_text

    def summarize(self, text: str, max_sentences: int = 3) -> LLMRewriteResult:
        """Генерирует краткое summary текста через LLM.

        Контракт: НИКОГДА не raises. Все ошибки — через LLMRewriteResult.ok=False.
        """
        cleaned_input = (text or "").strip()
        if not cleaned_input:
            return LLMRewriteResult(
                ok=False, text=None, fallback_reason="empty_input", latency_ms=None
            )

        if not self._circuit.allow_request():
            return LLMRewriteResult(
                ok=False, text=None, fallback_reason="circuit_open", latency_ms=None
            )

        system_prompt = (
            f"Сделай краткое summary ({max_sentences} предложения) этого разговора/диктовки. "
            "Верни ТОЛЬКО summary. Без пояснений. Без кавычек. Без префиксов."
        )
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": cleaned_input},
            ],
            "temperature": 0.3,
            "max_tokens": 512,
            "stream": False,
        }
        headers = self._lm_studio_headers()

        start = time.monotonic()
        try:
            with self._post_lock:
                response = self._session.post(
                    f"{self._base_url}/chat/completions",
                    json=payload,
                    headers=headers,
                    timeout=self._timeout * 2,  # summary может быть длиннее
                )
        except requests.Timeout:
            self._circuit.record_failure()
            self._last_error = "timeout"
            return LLMRewriteResult(
                ok=False, text=None, fallback_reason="timeout", latency_ms=None
            )
        except (requests.ConnectionError, requests.RequestException) as exc:
            self._circuit.record_failure()
            self._last_error = f"connection_error: {exc}"
            return LLMRewriteResult(
                ok=False, text=None, fallback_reason="connection_error", latency_ms=None
            )

        latency_ms = int((time.monotonic() - start) * 1000)
        self._last_latency_ms = latency_ms

        if response.status_code != 200:
            self._circuit.record_failure()
            self._last_error = f"http_{response.status_code}"
            return LLMRewriteResult(
                ok=False, text=None,
                fallback_reason=f"http_{response.status_code}",
                latency_ms=latency_ms,
            )

        try:
            data = response.json()
            content = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            self._circuit.record_failure()
            self._last_error = f"parse_error: {exc}"
            return LLMRewriteResult(
                ok=False, text=None, fallback_reason="parse_error", latency_ms=latency_ms
            )

        cleaned = (content or "").strip()
        if not cleaned:
            self._circuit.record_failure()
            self._last_error = "empty_response"
            return LLMRewriteResult(
                ok=False, text=None, fallback_reason="empty_response", latency_ms=latency_ms
            )

        self._circuit.record_success()
        self._last_error = None
        return LLMRewriteResult(
            ok=True, text=cleaned, fallback_reason=None, latency_ms=latency_ms
        )

    def warmup(self, timeout_sec: Optional[float] = None) -> bool:
        """Отправляет минимальный probe в LLM endpoint для прогрева модели в памяти.

        Использует тот же _session и headers что и rewrite().
        При успехе reset'ит circuit breaker (см. warmup_probe docstring).
        Возвращает True если HTTP 200, False при любой ошибке.
        Проглатывает все exceptions.

        Args:
            timeout_sec: таймаут запроса. Если None — используется self._timeout.
        """
        result = self.warmup_probe(timeout_sec=timeout_sec)
        return result["ok"]

    def warmup_probe(self, timeout_sec: Optional[float] = None) -> dict:
        """Отправляет минимальный probe и возвращает структурированный результат.

        Используется IPC-методом warmup_rewriter для возврата латентности и ошибки.
        Reset'ит circuit breaker при успехе (semantically a successful probe):
        warmup success означает LM Studio доступен и модель загружена — эквивалент
        HALF_OPEN→CLOSED перехода. Без этого user видит "Load Model OK" но следующий
        реальный rewrite всё равно блокируется открытым circuit'ом (confusing UX).

        Returns:
            dict с ключами: ok (bool), latency_ms (int), error (str | None).
        """
        effective_timeout = timeout_sec if timeout_sec is not None else self._timeout
        start = time.monotonic()
        try:
            payload = {
                "model": self._model,
                "messages": [{"role": "user", "content": "."}],
                "max_tokens": 1,
                "stream": False,
            }
            headers = self._lm_studio_headers()
            with self._post_lock:
                response = self._session.post(
                    f"{self._base_url}/chat/completions",
                    json=payload,
                    headers=headers,
                    timeout=effective_timeout,
                )
            elapsed_ms = int((time.monotonic() - start) * 1000)
            ok = response.status_code == 200
            error: Optional[str] = None if ok else f"http_{response.status_code}"
            logger.info("LLM warmup: ok=%s elapsed_ms=%d", ok, elapsed_ms)
            # 2026-05-09 fix: warmup success implies LM Studio is reachable + model loaded.
            # Reset circuit breaker — otherwise next user-facing rewrite blocks despite
            # warmup OK, which is confusing UX (user "loaded model" but still no LLM).
            if ok and self._circuit.state != "closed":
                logger.info(
                    "warmup_probe success → resetting circuit breaker (was %s)",
                    self._circuit.state,
                )
                self._circuit.record_success()  # HALF_OPEN → CLOSED if applicable
                # Force CLOSED if still OPEN (record_success only transitions from HALF_OPEN)
                if self._circuit.state == "open":
                    self._circuit._transition_to(CircuitState.CLOSED)
            return {"ok": ok, "latency_ms": elapsed_ms, "error": error}
        except requests.Timeout:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            logger.warning(
                "LLM warmup failed: exc=Timeout elapsed_ms=%d timeout_sec=%.1f",
                elapsed_ms, effective_timeout,
            )
            self._push_error(
                "rewriter.warmup_timeout",
                f"warmup_probe Timeout after {elapsed_ms}ms (timeout_sec={effective_timeout:.1f})",
            )
            return {"ok": False, "latency_ms": elapsed_ms, "error": "timeout"}
        except Exception as exc:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            logger.warning(
                "LLM warmup failed: exc=%s elapsed_ms=%d", type(exc).__name__, elapsed_ms
            )
            return {"ok": False, "latency_ms": elapsed_ms, "error": type(exc).__name__}

    def warmup_sync(
        self,
        timeout_sec: Optional[float] = None,
        retry_delays: Optional[list] = None,
    ) -> None:
        """Синхронный wrapper для запуска warmup в daemon-треде с exponential backoff retry.

        Вызывается через threading.Thread(target=...) при старте backend'а.
        Если warmup не удался (LM Studio ещё не готов) — ждёт и повторяет попытку
        согласно retry_delays перед тем как окончательно сдаться.
        LLMHttpProbe продолжает периодическую проверку каждые 30 сек после этого.

        Результат логируется. Все исключения проглатываются.

        Args:
            timeout_sec: таймаут одной warmup-пробы. None → self._timeout.
            retry_delays: список задержек в секундах между попытками.
                          По умолчанию [5, 10, 20, 30, 60] — 5 попыток суммарно
                          около 2 мин 5 сек. Покрывает типичный boot LM Studio
                          (20-60 с после логина).
        """
        if retry_delays is None:
            retry_delays = [5, 10, 20, 30, 60]

        attempt = 1
        max_attempts = len(retry_delays) + 1

        result = self.warmup(timeout_sec=timeout_sec)
        if result:
            logger.info(
                "LLM warmup succeeded on attempt %d/%d model=%s",
                attempt, max_attempts, self._model,
            )
            return

        for delay in retry_delays:
            attempt += 1
            logger.info(
                "LLM warmup attempt %d/%d failed, retrying in %ds (LM Studio may still be starting) model=%s",
                attempt - 1, max_attempts, delay, self._model,
            )
            # Use shutdown_event.wait so the loop exits cleanly if backend shuts down
            if self._shutdown_event.wait(timeout=delay):
                logger.debug("LLM warmup_sync: shutdown requested, stopping retry loop")
                return
            result = self.warmup(timeout_sec=timeout_sec)
            if result:
                logger.info(
                    "LLM warmup succeeded on attempt %d/%d model=%s",
                    attempt, max_attempts, self._model,
                )
                return

        logger.warning(
            "LLM warmup did not succeed after %d attempts (%.0f s total). "
            "LLMHttpProbe will retry every 30 s and recover automatically when LM Studio is ready. model=%s",
            max_attempts,
            sum(retry_delays),
            self._model,
        )

    def passive_health_check(self) -> tuple[bool, bool]:
        """Passive health check via GET /v1/models. Does NOT trigger JIT reload.

        Returns: (is_reachable, has_target_model)
            is_reachable=True если HTTP 200
            has_target_model=True если self._model in response.data[*].id

        Designed for LLMHttpProbe to avoid JIT churn. Use warmup() for
        explicit cold-start triggers.
        """
        try:
            # LM Studio exposes the models list at /api/v1/models (not /v1/models).
            # Derive the base host from _base_url (strip /v1 suffix if present) so
            # the probe URL is correct regardless of how LLM_BASE_URL is configured.
            import re as _re
            _host = _re.sub(r"/v\d+$", "", self._base_url.rstrip("/"))
            _url = f"{_host}/api/v1/models"
            response = self._session.get(
                _url,
                headers=self._lm_studio_get_headers(),
                timeout=5.0,  # short timeout — /models is fast metadata call
            )
            if response.status_code != 200:
                return (False, False)
            data = response.json()
            ids = [m.get("id") for m in data.get("data", [])]
            return (True, self._model in ids)
        except (requests.ConnectionError, requests.Timeout, requests.RequestException):
            return (False, False)
        except (ValueError, KeyError):
            return (True, False)  # reachable but bad JSON

    def set_model(self, model: str) -> None:
        """Обновляет активную модель и запускает фоновый warmup новой модели.

        Сбрасывает circuit breaker и метрики — новая модель начинает с чистого листа.
        После сброса запускает warmup в фоне: user-facing вызов всё равно работает,
        потому что circuit CLOSED после reset.
        """
        if self._model == model:
            return
        self._model = model
        # Reset state for new model
        self._last_latency_ms = None
        self._last_error = None
        self._circuit = CircuitBreaker(
            fail_threshold=self._circuit._fail_threshold,
            initial_reset_sec=self._circuit._initial_reset_sec,
            max_reset_sec=self._circuit._max_reset_sec,
        )
        # warm up new model in background; user-facing call still works because circuit is CLOSED
        threading.Thread(target=self.warmup, daemon=True).start()

    def set_api_key(self, api_key: str) -> None:
        """Обновляет Bearer-токен для LM Studio без перезапуска backend.

        Сбрасывает circuit breaker — предыдущие 401 ошибки, накопленные при
        неверном/отсутствующем ключе, больше не блокируют запросы.
        """
        if self._api_key == api_key:
            return
        self._api_key = api_key
        self._last_error = None
        self._circuit = CircuitBreaker(
            fail_threshold=self._circuit._fail_threshold,
            initial_reset_sec=self._circuit._initial_reset_sec,
            max_reset_sec=self._circuit._max_reset_sec,
        )
        logger.info("LLMRewriter: API key updated, circuit breaker reset")

    def ping(self) -> bool:
        """Проверка доступности LM Studio через GET /models.

        Не трогает circuit breaker — это отдельный health check, используется
        только на старте backend'а. Возвращает False на любую ошибку.
        """
        try:
            response = self._session.get(
                f"{self._base_url}/models",
                headers=self._lm_studio_get_headers(),
                timeout=self._timeout,
            )
            return response.status_code == 200
        except Exception:
            return False

    def status(self) -> dict:
        """Health info для llm_status IPC метода."""
        return {
            "reachable": self._circuit.state != "open",
            "model": self._model,
            "circuit_state": self._circuit.state,
            "last_latency_ms": self._last_latency_ms,
            "last_error": self._last_error,
        }

    def close(self):
        """Закрывает HTTP session и освобождает connection pool.

        Вызывается при завершении backend'а для корректного очищения ресурсов.
        Также сигналит keepalive-треду на остановку.
        """
        try:
            self._shutdown_event.set()
        except Exception:
            pass
        self._session.close()


# ---------------------------------------------------------------------------
# Fallback chain
# ---------------------------------------------------------------------------

@dataclass
class FallbackRewriteResult:
    """Результат RewriterFallbackChain.rewrite(). Всегда возвращается, никогда не raises."""

    ok: bool
    text: "str | None"
    model_used: "str | None"
    fallback_used: bool
    fallback_reason: "str | None"
    latency_ms: "Optional[int]"

    def text_or_fallback(self, fallback: str) -> str:
        if self.ok and self.text:
            return self.text
        return fallback


class RewriterFallbackChain:
    """Пробует primary model -> fallback 1 -> fallback 2 -> raw text."""

    def __init__(self, primary_rewriter, fallback_models: list):
        self._primary = primary_rewriter
        self._fallback_models = list(fallback_models)
        self._primary_model = primary_rewriter._model
        self._fallback_breakers = {
            m: CircuitBreaker(
                fail_threshold=primary_rewriter._circuit._fail_threshold,
                initial_reset_sec=primary_rewriter._circuit._initial_reset_sec,
                max_reset_sec=primary_rewriter._circuit._max_reset_sec,
            )
            for m in self._fallback_models
        }
        self._lock = threading.Lock()

    def rewrite(self, text: str) -> "FallbackRewriteResult":
        """Пробует модели по очереди. Контракт: НИКОГДА не raises."""
        with _profiler.start_span("rewriter_fallback_chain"):
            return self._rewrite_impl(text)

    def _rewrite_impl(self, text: str) -> "FallbackRewriteResult":
        try:
            primary_result = self._primary.rewrite(text)
        except Exception as exc:
            logger.error("RewriterFallbackChain: unexpected exception from primary: %s", exc)
            primary_result = LLMRewriteResult(
                ok=False, text=None, fallback_reason="unexpected_exception", latency_ms=None
            )

        if primary_result.ok:
            return FallbackRewriteResult(
                ok=True, text=primary_result.text, model_used=self._primary_model,
                fallback_used=False, fallback_reason=None, latency_ms=primary_result.latency_ms,
            )

        for model in self._fallback_models:
            breaker = self._fallback_breakers[model]
            if not breaker.allow_request():
                logger.debug("RewriterFallbackChain: skipping %s — breaker open", model)
                continue
            result = self._call_fallback(text, model, breaker)
            if result.ok:
                self._push_fallback_used_error(model)
                return result

        last_reason = primary_result.fallback_reason or "unknown"
        return FallbackRewriteResult(
            ok=False, text=None, model_used=None, fallback_used=False,
            fallback_reason="all_models_failed:{}".format(last_reason), latency_ms=None,
        )

    def _call_fallback(self, text, model, breaker):
        with self._lock:
            original_model = self._primary._model
            original_circuit = self._primary._circuit
            self._primary._model = model
            self._primary._circuit = breaker
            try:
                result = self._primary.rewrite(text)
            except Exception as exc:
                logger.error("RewriterFallbackChain: unexpected exception from fallback %s: %s", model, exc)
                result = LLMRewriteResult(ok=False, text=None, fallback_reason="unexpected_exception", latency_ms=None)
            finally:
                self._primary._model = original_model
                self._primary._circuit = original_circuit

        if result.ok:
            return FallbackRewriteResult(
                ok=True, text=result.text, model_used=model, fallback_used=True,
                fallback_reason=None, latency_ms=result.latency_ms,
            )
        return FallbackRewriteResult(
            ok=False, text=None, model_used=model, fallback_used=True,
            fallback_reason=result.fallback_reason, latency_ms=result.latency_ms,
        )

    def _push_fallback_used_error(self, model_used):
        error_bus = getattr(self._primary, "_error_bus", None)
        if error_bus is None:
            return
        try:
            from backend.error_bus import KrabError
            from backend.error_codes import ERROR_REGISTRY
            from datetime import datetime, timezone
            entry = ERROR_REGISTRY.get("rewriter.fallback_used", {})
            err = KrabError(
                severity="info", component="rewriter", code="rewriter.fallback_used",
                message_user=entry.get("user_msg_ru", "Основная модель сбоит — переключились на резервную"),
                message_debug="fell back to model={}".format(model_used),
                timestamp=datetime.now(timezone.utc),
                context={"fallback_model": model_used, "primary_model": self._primary_model},
                actionable=False, action_id=None,
            )
            error_bus.push(err)
        except Exception:
            logger.exception("RewriterFallbackChain._push_fallback_used_error failed")

    def status(self) -> dict:
        return {
            "primary": self._primary.status(),
            "fallback_models": self._fallback_models,
            "fallback_breakers": {m: b.state for m, b in self._fallback_breakers.items()},
        }

    def set_primary_model(self, model: str) -> None:
        self._primary_model = model
        self._primary.set_model(model)

    @property
    def primary(self):
        return self._primary
