"""LLM rewriter для Krab Ear — пост-процессинг транскрипта через локальный LM Studio.

Модуль содержит:
- CircuitBreaker: state machine (CLOSED → OPEN → HALF_OPEN) с exponential backoff
- LLMRewriteResult: dataclass-результат попытки rewrite'а
- LLMRewriter: HTTP-клиент к OpenAI-compatible endpoint'у

Контракт LLMRewriter.rewrite(): НИКОГДА не raises, всегда возвращает LLMRewriteResult.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import requests
from enum import Enum
from typing import Optional

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
    text: Optional[str]
    fallback_reason: Optional[str]
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
        timeout_sec: float = 4.0,
        circuit_fail_threshold: int = 3,
        circuit_initial_reset_sec: int = 60,
        circuit_max_reset_sec: int = 600,
    ):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout = timeout_sec
        self._circuit = CircuitBreaker(
            fail_threshold=circuit_fail_threshold,
            initial_reset_sec=circuit_initial_reset_sec,
            max_reset_sec=circuit_max_reset_sec,
        )
        self._last_latency_ms: Optional[int] = None
        self._last_error: Optional[str] = None
        # Connection pooling: переиспользуем TCP соединение между запросами
        self._session = requests.Session()

    def set_model(self, new_model: str) -> bool:
        """Hot-swap rewriter model name without backend restart.

        Returns True if model name changed. Resets circuit breaker so the new
        model gets a fresh chance even if previous one was tripped.
        Caller is responsible for ensuring `new_model` is loadable in LM Studio
        (LM Studio JIT will auto-load on first request if it's downloaded).
        """
        new_model = (new_model or "").strip()
        if not new_model or new_model == self._model:
            return False
        logger.info("LLMRewriter: hot-swap model %r → %r", self._model, new_model)
        self._model = new_model
        # Reset circuit so the new model gets a fresh start
        self._circuit.record_success()  # closes circuit if it was open
        self._last_error = None
        self._last_latency_ms = None
        return True

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

    # ------------------------------------------------------------------
    # Endpoint mode selection
    # ------------------------------------------------------------------
    # ROOT CAUSE: LM Studio's /v1/chat/completions endpoint applies a
    # tool-extraction layer that parses any <tool_call>...</tool_call>
    # markers in raw model output and moves them into the structured
    # `tool_calls` field — clearing `content` in the process. This happens
    # even when client sends `tool_choice: "none"` and `tools: []`.
    #
    # Qwen3-family fine-tunes (Huihui, Josiefied, Qwen3.5) emit `<tool_call>`
    # JSON when given structured editor tasks (numbered rules trigger their
    # function-calling fine-tune). Direct comparison: same model loaded via
    # mlx_lm.server (no LM Studio tool extractor) returns clean text;
    # via LM Studio chat completions returns tool_calls.
    #
    # FIX: route requests through /v1/completions (raw text completion).
    # We build the chat template manually as a plain prompt string. LM Studio
    # treats it as completion (no tool extractor), returns plain `text`.
    # This is OpenAI-spec compliant and works with any LM Studio backend.
    # ------------------------------------------------------------------
    _QWEN_FAMILY_MARKERS = ("qwen", "Qwen", "QWEN")

    def _is_qwen_family(self) -> bool:
        return any(m in (self._model or "") for m in self._QWEN_FAMILY_MARKERS)

    def _build_qwen_chatml_prompt(self, text: str) -> str:
        """Manually build Qwen ChatML prompt for /v1/completions.

        Qwen3-Instruct chat template format (без tools preamble который
        провоцирует tool_calls в chat-mode):

            <|im_start|>system\n{SYSTEM}<|im_end|>
            <|im_start|>user\n{TEXT}<|im_end|>
            <|im_start|>assistant\n
        """
        return (
            "<|im_start|>system\n"
            f"{SYSTEM_PROMPT}<|im_end|>\n"
            "<|im_start|>user\n"
            f"{text}<|im_end|>\n"
            "<|im_start|>assistant\n"
        )

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

        # 3. Endpoint selection: Qwen family → /v1/completions (root-cause fix
        # для tool_calls leak'а в LM Studio), остальные → /v1/chat/completions.
        if self._is_qwen_family():
            payload = {
                "model": self._model,
                "prompt": self._build_qwen_chatml_prompt(cleaned_input),
                "temperature": 0.0,
                "max_tokens": self._estimate_max_tokens(cleaned_input),
                "stream": False,
                "stop": ["<|im_end|>", "<|im_start|>", "Исправленный текст:", "Исходный текст:"],
            }
            endpoint_path = "/completions"
        else:
            payload = {
                "model": self._model,
                "messages": self._build_messages(cleaned_input),
                "temperature": 0.0,
                "max_tokens": self._estimate_max_tokens(cleaned_input),
                "stream": False,
                "stop": ["Исправленный текст:", "Исходный текст:"],
                # Last-resort guard для не-Qwen моделей, эмитящих tool_calls
                # (видно в LM Studio логах). На /v1/chat/completions LM Studio
                # активирует tool extractor, который ловит <tool_call>...</tool_call>
                # markers и переносит в структурированное поле, теряя content.
                "tool_choice": "none",
                "tools": [],
            }
            endpoint_path = "/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }

        # 4. HTTP call with timing
        start = time.monotonic()
        try:
            response = self._session.post(
                f"{self._base_url}{endpoint_path}",
                json=payload,
                headers=headers,
                timeout=self._timeout,
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

        # 5. HTTP status check
        if response.status_code != 200:
            self._circuit.record_failure()
            self._last_error = f"http_{response.status_code}"
            return LLMRewriteResult(
                ok=False,
                text=None,
                fallback_reason=f"http_{response.status_code}",
                latency_ms=latency_ms,
            )

        # 6. Parse JSON response — schema differs between endpoints:
        #   /v1/completions:        choices[0].text                  (plain string)
        #   /v1/chat/completions:   choices[0].message.content       (+ optional .tool_calls)
        try:
            data = response.json()
            choice = data["choices"][0]
            if "text" in choice:
                # /v1/completions response — Qwen family path
                content = choice.get("text") or ""
                tool_calls = None  # raw completion endpoint never returns tool_calls
            else:
                message = choice["message"]
                content = message.get("content")
                tool_calls = message.get("tool_calls")
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            self._circuit.record_failure()
            self._last_error = f"parse_error: {exc}"
            return LLMRewriteResult(
                ok=False, text=None, fallback_reason="parse_error", latency_ms=latency_ms
            )

        # 6b. Guard: некоторые модели (Qwen 2.5 14B Uncensored)
        # игнорят tool_choice=none и эмитят tool_calls вместо content.
        # Это ломает rewrite — fallback на оригинал.
        if tool_calls and not content:
            self._circuit.record_failure()
            self._last_error = "tool_calls_emitted"
            logger.warning(
                "LLM emitted tool_calls instead of content (model=%s), falling back",
                self._model,
            )
            return LLMRewriteResult(
                ok=False, text=None, fallback_reason="tool_calls_emitted", latency_ms=latency_ms
            )
        if not content:
            content = ""

        # 7. Postprocess
        cleaned = self._postprocess(content)
        if not cleaned:
            self._circuit.record_failure()
            self._last_error = "empty_response"
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
        # Adaptive thresholds:
        # - Очень короткие inputs (<60 chars) часто содержат много повторов re-articulation,
        #   которые легитимно сжимаются до 15-25% (например "так так так так так так" → "так").
        # - Длинные inputs (>200 chars) редко сжимаются ниже 30% без потери смысла.
        # - Short range (60-200): in-between threshold 0.22.
        input_len = len(cleaned_input)
        output_len = len(cleaned)
        if input_len > 20:
            ratio = output_len / input_len
            if input_len < 60:
                short_thr = 0.15  # очень короткие — допускаем агрессивное сжатие
            elif input_len < 200:
                short_thr = 0.22
            else:
                short_thr = 0.30  # длинные — guard построже
            if ratio < short_thr:
                logger.warning(
                    "LLM output too short (%.0f%% of input, threshold %.0f%%), falling back to original",
                    ratio * 100, short_thr * 100,
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

    def fix_punctuation_only(self, text: str, language: str = "ru") -> Optional[str]:
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
            "tool_choice": "none",
            "tools": [],
        }
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }

        start = time.monotonic()
        try:
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
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }

        start = time.monotonic()
        try:
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

    def ping(self) -> bool:
        """Проверка доступности LM Studio через GET /models.

        Не трогает circuit breaker — это отдельный health check, используется
        только на старте backend'а. Возвращает False на любую ошибку.
        """
        try:
            response = self._session.get(
                f"{self._base_url}/models",
                headers={"Authorization": f"Bearer {self._api_key}"},
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
        """
        self._session.close()
