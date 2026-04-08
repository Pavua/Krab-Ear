# D.10a LM Studio Integration — Design

**Track:** Track D (Krab Ear)
**Date:** 2026-04-09
**Status:** Design approved, ready for implementation plan
**Parent plan:** `docs/PLAN_TRACK_D_KRAB_EAR.md`
**Depends on:** D.7 (Dictation Quality Tuning), Variant B launchd supervision

---

## 1. Goal

После того как mlx-whisper транскрибирует диктовку и `TextUtils.cleanup_transcript` (D.7) нормализует бренды/время, локальная LLM через LM Studio (OpenAI-compatible API) исправляет пунктуацию, орфографию и грамматику, сохраняя смысл и стиль. Результат вставляется через стандартный PasteService. Если LLM недоступен — диктовка падает обратно на D.7-очищенный текст без потери функциональности.

**Primary user win:** заметно более чистая пунктуация на длинных русских фразах (email'ах, сообщениях для Claude Code) без ручной правки.

## 2. Non-goals

Явно out of scope для D.10a (фиксируем чтобы не было scope creep):

- ❌ Swift UI toggle/дропдаун — это D.10a.1, отдельная mini-задача
- ❌ Model tier selection (fast/balanced/heavy) — D.10a.2
- ❌ Learned replacements SQLite — D.10b
- ❌ Auto-vocabulary growth — D.10c
- ❌ Swift UI "Замены" вкладка — D.10d
- ❌ Cache rewrite'нутого текста — YAGNI, добавляем по данным если нужно
- ❌ Retry в sync пути — не добавляем (один shot)
- ❌ Multi-paragraph split toggle — добавляем только если пользователь запросит
- ❌ Streaming response от LLM — не нужно для sync pre-paste

## 3. Architecture

### 3.1 High-level flow

```
Right Option release
  ↓
Swift: stopRecording IPC
  ↓
BackendService.stop_recording()
  ↓
Transcriber.transcribe(audio)
  ↓
AudioEngine.transcribe(audio)
  ├── _transcribe_with_fallback() → raw_text (mlx-whisper)
  ├── TextUtils.cleanup_transcript(raw_text) → cleaned_text (D.7 normalization)
  └── if llm_rewrite_allowed:
        llm_rewriter.rewrite(cleaned_text)
          ├── CircuitBreaker.allow_request() → OPEN? return fallback
          ├── requests.post(base_url + /chat/completions, timeout=4s)
          ├── parse response → content
          ├── _postprocess(content) → strip quotes, prefixes, multi-paragraph
          └── return LLMRewriteResult(ok, text, fallback_reason, latency_ms)
  ↓
return dict with text=final_text, raw_text=raw_text, cleaned_text=cleaned, llm_applied, llm_latency_ms
  ↓
BackendService.stop_recording() → StateStore.append(item с всеми версиями)
  ↓
Swift: paste final_text
```

### 3.2 New files

- **`KrabEar/backend/llm_rewriter.py`** (~200 строк) — HTTP клиент к OpenAI-compatible endpoint'у, содержит `LLMRewriter`, `CircuitBreaker`, `LLMRewriteResult`.
- **`KrabEar/tests/test_llm_rewriter.py`** (~250 строк) — 25+ unit tests через mock `requests.post`.
- **`KrabEar/tests/test_engine_llm_integration.py`** (~100 строк) — 5 integration tests для engine hook.

### 3.3 Modified files

- **`KrabEar/core/config.py`** — добавить LLM settings в `Settings` класс, `llm_rewrite_enabled: false` в `DEFAULT_SETTINGS`, переключить `env_file` на абсолютный путь к `.secrets`.
- **`KrabEar/core/engine.py`** — `AudioEngine.__init__` принимает optional `llm_rewriter` и `settings_get` callback; в `transcribe()` после `cleanup_transcript` добавляется условный LLM вызов; возвращаемый dict расширяется полями `cleaned_text`, `llm_applied`, `llm_latency_ms`, `llm_fallback_reason`.
- **`KrabEar/backend/service.py`** — `BackendService.__init__` создаёт `LLMRewriter` если `settings.LLM_ENABLED`, передаёт в `Transcriber`; новый метод `_handle_llm_status()`; регистрация `llm_status` в dispatch; `HistoryItem` обогащается LLM полями.
- **`KrabEar/backend/transcriber.py`** — thin wrapper, пробрасывает `llm_rewriter` и `settings_get` в `AudioEngine`.
- **`KrabEar/tests/test_backend_service.py`** — добавить 3 теста на `llm_status` IPC и runtime toggle.
- **`~/Library/Application Support/KrabEar/.secrets`** — обновить `KRAB_EAR_LLM_MODEL=qwen3.5-9b@6bit`, добавить `KRAB_EAR_LLM_ENABLED=true`.

### 3.4 Unchanged files

- `requirements.txt` — ничего не добавляем, используем `requests` (уже есть)
- `launchagents/ai.krab.ear.backend.plist.template` — без изменений
- `scripts/install_backend_launchagent.command` — без изменений
- Swift код — без изменений (Swift UI отложен в D.10a.1)

## 4. Configuration

### 4.1 Secrets loading — fix for existing architectural gap

**Обнаруженная проблема:** `install_backend_launchagent.command` читает только `KRAB_EAR_HF_TOKEN` из `.secrets` (селективно через `grep`). Все остальные переменные в `.secrets` мертвы для backend'а. `config.py` использует `env_file=".env"` — относительный путь, резолвится от CWD (launchd CWD = project root), так что `.secrets` в `~/Library/Application Support/KrabEar/` не подхватывается.

**Решение:** переключить `env_file` на tuple с абсолютным путём:

```python
# core/config.py
from pathlib import Path

_SECRETS_FILE = Path.home() / "Library" / "Application Support" / "KrabEar" / ".secrets"

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="KRAB_EAR_",
        env_file=(str(_SECRETS_FILE), ".env"),  # tuple: последний с более высоким приоритетом
        extra="ignore",
    )
```

**Priority order** (от низкого к высокому):
1. Defaults в коде
2. `.secrets` (prod base)
3. `.env` (local dev override)
4. Environment variables (launchd plist) — **highest**

**Побочные эффекты:**
- HF_TOKEN в plist продолжает работать (env var priority > env_file)
- Любая будущая `KRAB_EAR_*` переменная автоматически доступна через `.secrets`
- macOS-specific захардкоженный путь — приемлемо, backend уже macOS-only (mlx-whisper, pyannote, sounddevice)

### 4.2 New Settings fields

```python
# Settings class extension
LLM_ENABLED: bool = False
LLM_BASE_URL: str = "http://localhost:1234/v1"
LLM_API_KEY: str = ""
LLM_MODEL: str = "qwen3.5-9b@6bit"
LLM_TIMEOUT_SEC: float = 4.0
LLM_CIRCUIT_FAIL_THRESHOLD: int = 3
LLM_CIRCUIT_INITIAL_RESET_SEC: int = 60
LLM_CIRCUIT_MAX_RESET_SEC: int = 600
```

### 4.3 DEFAULT_SETTINGS (runtime toggle)

```python
DEFAULT_SETTINGS = {
    # ... existing keys
    "llm_rewrite_enabled": False,  # runtime toggle через IPC update_settings
}
```

### 4.4 Hybrid toggle semantics

- `Settings.LLM_ENABLED` (env) = **admin flag**: LLM клиент инициализируется при старте backend'а, модуль активен. Защищает от спама в логах когда LM Studio не установлен.
- `llm_rewrite_enabled` (runtime) = **user toggle**: значение живёт в `StateStore.settings` (обновляется через IPC `update_settings`), дефолт `false` приходит из `DEFAULT_SETTINGS["llm_rewrite_enabled"]` при первом чтении. Пользователь может включать/выключать на конкретной сессии без рестарта backend'а.
- Effective condition на каждой транскрипции:
  ```python
  if settings.LLM_ENABLED and state_store.get_setting("llm_rewrite_enabled", False):
      llm_rewriter.rewrite(text)
  ```

### 4.5 `.secrets` file target state

```bash
# ~/Library/Application Support/KrabEar/.secrets
KRAB_EAR_HF_TOKEN=hf_REDACTED_REVOKED_2026_04_15
KRAB_EAR_LLM_ENABLED=true
KRAB_EAR_LLM_BASE_URL=http://localhost:1234/v1
KRAB_EAR_LLM_API_KEY=sk-lm-aM6s3ukv:YBV64I1QrsqVg6LYbH9H
KRAB_EAR_LLM_MODEL=qwen3.5-9b@6bit
KRAB_EAR_LLM_TIMEOUT_SEC=4.0
# Held for future D.10a.2 tier selection
KRAB_EAR_LLM_MODEL_FAST=qwen3.5-4b-mlx
KRAB_EAR_LLM_MODEL_HEAVY=qwen3.5-27b-claude-4.6-opus-reasoning-distilled-qx64-hi-mlx
```

## 5. `LLMRewriter` class design

### 5.1 Public API

```python
@dataclass
class LLMRewriteResult:
    ok: bool
    text: Optional[str]
    fallback_reason: Optional[str]  # "circuit_open" | "timeout" | "connection_error" |
                                     # "http_<status>" | "parse_error" | "empty_response" |
                                     # "empty_input"
    latency_ms: Optional[int]

    def text_or_fallback(self, fallback: str) -> str:
        return self.text if self.ok and self.text else fallback


class LLMRewriter:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout_sec: float = 4.0,
        circuit_fail_threshold: int = 3,
        circuit_initial_reset_sec: int = 60,
        circuit_max_reset_sec: int = 600,
    ): ...

    def rewrite(self, text: str) -> LLMRewriteResult:
        """Главный метод. НИКОГДА не raises."""

    def ping(self) -> bool:
        """GET /models для проверки доступности при старте."""

    def status(self) -> dict:
        """Health info для llm_status IPC."""
```

### 5.2 Key invariants

1. **`rewrite()` НИКОГДА не raises** — вся обработка ошибок внутри, наружу всегда `LLMRewriteResult`. Гарантирует что баг в LLM-клиенте не уронит диктовку.
2. **Circuit breaker counter обновляется ТОЛЬКО на реальные failures** — timeout, connection error, bad HTTP status, parse error, empty response. НЕ обновляется при `empty_input` (не вина сервера).
3. **Latency measurement всегда до max** — даже failed requests логируют latency для диагностики.
4. **Thread-unsafe by design** — IPC server в Krab Ear однопоточный. Если в будущем станет multi-threaded — обернуть `CircuitBreaker` в `threading.Lock`.

### 5.3 `_postprocess()` — LLM output cleanup

Защита от стандартных болячек Qwen'а:

```python
def _postprocess(self, content: str) -> str:
    s = content.strip()

    # Убрать обрамляющие кавычки ("...", «...», "...")
    if len(s) >= 2 and s[0] in ('"', '«', '"') and s[-1] in ('"', '»', '"'):
        s = s[1:-1].strip()

    # Убрать пояснительные префиксы
    for prefix in ("Исправленный текст:", "Исправлено:", "Результат:", "Вот:"):
        if s.lower().startswith(prefix.lower()):
            s = s[len(prefix):].strip()

    # Взять только первый параграф (защита от "<text>\n\n**Пояснение**: ...")
    if "\n\n" in s:
        s = s.split("\n\n", 1)[0].strip()

    return s
```

**Known limitation:** `\n\n` split блокирует multi-paragraph dictation. Для текущего use case (длинные monolith-сообщения без пустых строк) — безопасно. При необходимости добавим `settings.LLM_ALLOW_MULTIPARAGRAPH` toggle.

### 5.4 Dynamic `max_tokens` estimator

```python
def _estimate_max_tokens(self, text: str) -> int:
    """Адаптивный output cap на базе длины input'а."""
    word_count = len(text.split())
    input_tokens_estimate = word_count * 3  # ~2.5-3 tokens/word в русском
    max_tokens = int(input_tokens_estimate * 1.3) + 50  # 30% headroom + буфер на знаки
    return max(256, min(max_tokens, 4096))  # floor 256, ceiling 4096
```

**Эффекты:**
- 10-словная фраза → `max_tokens=256` (floor), decode быстрый
- 100-словная фраза → `max_tokens=~440`
- 500-словная диктовка → `max_tokens=~2000`
- 1500+ слов → `max_tokens=4096` (ceiling)

Дополнительная страховка от runaway галлюцинаций поверх `temperature=0`.

### 5.5 HTTP request structure

```python
payload = {
    "model": self._model,
    "messages": [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": text},
    ],
    "temperature": 0.0,
    "max_tokens": self._estimate_max_tokens(text),
    "stream": False,
    "stop": ["\n\n", "Исправленный текст:", "Исходный текст:"],
}
# Note: если LM Studio отказывается обрабатывать "\n\n" в stop tokens
# (некоторые провайдеры не поддерживают newline в stop), убрать только
# эту строку — postprocess всё равно отрезает multi-paragraph вывод.
headers = {
    "Content-Type": "application/json",
    "Authorization": f"Bearer {self._api_key}",
}
```

## 6. System prompt

```
Ты — редактор русской диктовки. Твоя задача — исправить пунктуацию, орфографию
и грамматику в тексте, сохранив смысл и стиль автора.

Жёсткие правила:
1. НЕ добавляй слов, которых нет в оригинале.
2. НЕ удаляй слов, кроме явных filler'ов в начале ("э-э", "ну", "вот").
3. НЕ меняй порядок слов, кроме случаев когда этого требует грамматика.
4. НЕ переформулируй фразы — только исправляй ошибки.
5. Бренды и технические термины оставляй латиницей: Spotify, YouTube, GitHub,
   Claude, OpenAI, Docker, Python, Swift, macOS, iPhone, iPad, Mac, Telegram,
   WhatsApp, Slack, Notion, Figma, VS Code, Xcode, Linux, Linear, Jira.
6. Расставь правильные знаки препинания: запятые, точки, тире, двоеточия.
7. Заглавные буквы в начале предложений и у имён собственных.
8. Если текст пустой или бессмысленный — верни его без изменений.

Верни ТОЛЬКО исправленный текст. Без пояснений. Без кавычек. Без префиксов типа
"Исправленный текст:".
```

**Design rationale:**
- **Нумерованные правила** — Qwen лучше следует пронумерованным инструкциям чем prose
- **Negative constraints первыми** — самый критичный класс ошибок small моделей
- **Встроенный brand whitelist** — контекстный prime для экстраполяции на похожие бренды (Netflix, Slack, etc.)
- **Явный запрет префиксов** — Qwen любит отвечать "Исправленный текст: ..."
- **Fallback для мусора** — защита от галлюцинаций на нечётком аудио

## 7. `CircuitBreaker` internal class

### 7.1 State machine

```
                  record_success
       ┌──────────────────────────────────┐
       ↓                                  │
  ┌─────────┐                       ┌───────────┐
  │ CLOSED  │                       │ HALF_OPEN │
  │(работает)│                       │(одна проба)│
  └─────────┘                       └───────────┘
       │                                   ↑
       │ 3 × record_failure                │ cooldown прошёл
       ↓                                   │
  ┌─────────┐                              │
  │  OPEN   │──────────────────────────────┘
  │  (блок) │
  └─────────┘
       │
       │ record_failure в HALF_OPEN → cooldown * 2
       ↓
  (OPEN с удвоенным cooldown)
```

### 7.2 Exponential backoff

- Initial cooldown: 60 сек
- При каждом failed probe в HALF_OPEN: `cooldown *= 2`
- Cap: 600 сек
- Reset на `initial_reset_sec` при успешном HALF_OPEN → CLOSED transition

**Sequence пример (LM Studio мёртв):**
- 3 fails → OPEN с cooldown=60с
- проба fail → cooldown=120с
- проба fail → cooldown=240с
- проба fail → cooldown=480с
- проба fail → cooldown=600с (cap)
- проба success → CLOSED, cooldown сброшен в 60с

### 7.3 Logging policy

- **Только на transitions**: CLOSED → OPEN (warning), OPEN → HALF_OPEN (debug), HALF_OPEN → CLOSED (info), HALF_OPEN → OPEN (warning с новым cooldown)
- **Ни одного лога** при каждом blocked request в OPEN state
- **Цель:** устранить D.11-паттерн (94× "Gateway Connection refused" spam в backend.log)

### 7.4 `time.monotonic()` not `time.time()`

Защита от NTP jumps и ручного перевода часов. Критично для cooldown measurement.

## 8. Engine integration

### 8.1 `AudioEngine.__init__` signature change

```python
from typing import Any, Callable, Optional

def __init__(
    self,
    ...,
    llm_rewriter: Optional["LLMRewriter"] = None,
    settings_get: Optional[Callable[[str, Any], Any]] = None,
):
    # ...
    self._llm_rewriter = llm_rewriter
    self._settings_get = settings_get or (lambda key, default: default)
```

**Layering:** `AudioEngine` **не импортирует** `StateStore` напрямую. `BackendService` инжектирует `settings_get` callback, читающий из state store. Границы слоёв сохраняются.

### 8.2 Hook в `transcribe()`

```python
# После существующего cleanup на строке ~222
cleaned_text = TextUtils.cleanup_transcript(raw_text, profile=cleanup_profile)
text = cleaned_text  # default

llm_result: Optional[LLMRewriteResult] = None
if self._llm_rewrite_allowed():
    llm_result = self._llm_rewriter.rewrite(cleaned_text)
    if llm_result.ok:
        logger.info(
            "LLM rewrite: %d chars -> %d chars, %d ms",
            len(cleaned_text), len(llm_result.text), llm_result.latency_ms
        )
        text = llm_result.text
    else:
        logger.debug(
            "LLM rewrite fallback: %s (latency=%s ms)",
            llm_result.fallback_reason, llm_result.latency_ms
        )

def _llm_rewrite_allowed(self) -> bool:
    if self._llm_rewriter is None:
        return False
    return bool(self._settings_get("llm_rewrite_enabled", False))
```

### 8.3 Return dict extension

```python
return {
    "text": text,                     # финальный (после LLM если был)
    "raw_text": raw_text,             # после whisper, до cleanup
    "cleaned_text": cleaned_text,     # NEW — после cleanup D.7, до LLM
    "llm_applied": llm_result is not None and llm_result.ok,
    "llm_latency_ms": llm_result.latency_ms if llm_result else None,
    "llm_fallback_reason": llm_result.fallback_reason if llm_result and not llm_result.ok else None,
    # ... existing fields (confidence, duration_ms, segments, diarization, etc.)
}
```

`HistoryItem` сохраняет **все три версии** (`raw_text`, `cleaned_text`, `text`) для диагностики, будущего "undo rewrite" UI (D.10d), и debug при регрессиях.

## 9. IPC — `llm_status` method

### 9.1 Request/response

```json
// Request
{"id": "1", "method": "llm_status"}

// Response
{
  "id": "1",
  "ok": true,
  "result": {
    "enabled": true,              // admin_enabled && runtime_enabled && reachable
    "admin_enabled": true,         // settings.LLM_ENABLED
    "runtime_enabled": true,       // DEFAULT_SETTINGS.llm_rewrite_enabled
    "reachable": true,             // ping succeeded OR circuit CLOSED
    "model": "qwen3.5-9b@6bit",
    "circuit_state": "closed",     // closed | open | half_open
    "last_latency_ms": 1847,
    "last_error": null
  }
}
```

### 9.2 Handler logic

```python
def _handle_llm_status(self) -> dict:
    if self._llm_rewriter is None:
        return {
            "enabled": False,
            "admin_enabled": settings.LLM_ENABLED,
            "runtime_enabled": self._state_store.get_setting("llm_rewrite_enabled", False),
            "reachable": False,
            "model": None,
            "circuit_state": None,
            "last_latency_ms": None,
            "last_error": "llm_rewriter не инициализирован (admin_enabled=False)",
        }

    status = self._llm_rewriter.status()
    status["admin_enabled"] = True
    status["runtime_enabled"] = self._state_store.get_setting("llm_rewrite_enabled", False)
    status["enabled"] = (
        status["admin_enabled"]
        and status["runtime_enabled"]
        and status["reachable"]
    )
    return status
```

## 10. Testing strategy

### 10.1 Unit tests — `tests/test_llm_rewriter.py` (25+ тестов)

**`TestLLMRewriterSuccess`:**
- Happy path: LM Studio 200 + valid JSON → ok=True
- Quote stripping (`"..."`, `«...»`)
- Explanatory prefix stripping (`Исправленный текст: ...`)
- Multi-paragraph split (берём первый блок до `\n\n`)
- Dynamic max_tokens scaling

**`TestLLMRewriterFailures`:**
- Empty input → `empty_input`, no HTTP call
- `requests.Timeout` → `timeout`, circuit counter +1
- `ConnectionError` → `connection_error`
- HTTP 500 → `http_500`
- Malformed JSON → `parse_error`
- Missing `choices` key → `parse_error`
- Empty content после postprocess → `empty_response`

**`TestCircuitBreaker`:**
- CLOSED → OPEN после 3 последовательных failures
- Success сбрасывает counter
- OPEN блокирует все запросы до cooldown
- HALF_OPEN разрешает одну пробу, последующие блокирует
- HALF_OPEN success → CLOSED + reset backoff
- HALF_OPEN failure → OPEN с удвоенным cooldown
- Backoff cap на 600 сек
- Использует monkeypatch `time.monotonic` для детерминизма

**`TestPingAndStatus`:**
- Ping → True на 200
- Ping → False на ConnectionError (no exception)
- Status возвращает все ключи

**`TestTextOrFallback`:**
- ok=True → returns text
- ok=False → returns fallback

**Coverage target:** >90% для `llm_rewriter.py`

### 10.2 Integration tests — `tests/test_engine_llm_integration.py`

- `transcribe()` без llm_rewriter → text = cleanup output
- `transcribe()` с mocked rewriter → text = rewriter output
- Runtime toggle=False → rewriter НЕ вызван даже если injected
- Return dict содержит `cleaned_text`, `llm_applied`, `llm_latency_ms`
- LLM failure → text = cleaned_text, llm_applied=False

### 10.3 Backend service tests — расширение `test_backend_service.py`

- `handle_llm_status` возвращает disabled когда не инициализирован
- `handle_llm_status` возвращает full info когда инициализирован
- `update_settings` с `llm_rewrite_enabled` персистит в StateStore

### 10.4 Manual smoke test checklist

Обязателен перед закрытием D.10a (не в CI):

**Pre-conditions:**
- LM Studio запущен, `qwen3.5-9b@6bit` загружен
- `.secrets` обновлён
- `install_backend_launchagent.command` запущен (для регенерации plist если надо)

**Bootstrap:**
- `logs/krab-ear-backend.out.log` содержит `LLM rewriter инициализирован`
- `llm_status` IPC → `reachable=true`, `circuit_state=closed`

**Функциональные:**
- Enable runtime toggle через IPC `update_settings`
- Короткая диктовка (~10 слов) → ≤2.5 сек задержка, правильная пунктуация
- Средняя (~30 слов) с брендами → "Docker", "Python", "Claude Code" латиницей
- Длинная (~100 слов) → latency 3-5 сек, текст цельный
- Диктовка в тишину → silence_guard блокирует ДО LLM

**Failure modes:**
- Закрыть LM Studio, диктовать 3 раза → первые 3 fail с одним warning "CLOSED → OPEN", 4-я сразу fallback
- Запустить LM Studio, подождать 60 сек, диктовать → HALF_OPEN → CLOSED, текст rewritten
- Runtime toggle=false → paste сразу без задержки

**История:**
- `history.ndjson` содержит `raw_text`, `cleaned_text`, `text`, `llm_applied`, `llm_latency_ms`

## 11. Acceptance criteria (DoD)

D.10a закрыт когда:

1. `llm_rewriter.py` существует с полным API (`LLMRewriter`, `CircuitBreaker`, `LLMRewriteResult`)
2. 25+ unit tests проходят, coverage >90% для нового модуля
3. Integration tests проходят (engine hook, fallback, return dict shape)
4. Backend startup log содержит `LLM rewriter инициализирован` при `LLM_ENABLED=true`
5. IPC `llm_status` отвечает с валидным dict
6. Manual smoke checklist пройден минимум один раз
7. Runtime toggle работает — `update_settings` меняет поведение на следующей диктовке без рестарта
8. Failure fallback работает — LM Studio закрытый не ломает диктовку, ровно один warning в лог
9. Все существующие тесты проходят — ноль регрессий
10. Новый коммит: `feat(ear): D.10a LM Studio rewriter integration`

## 12. Roll-out and rollback

### 12.1 Roll-out

1. Commit изменений в `claude/ecstatic-moser`
2. `python -m pytest KrabEar/tests/ -v` весь набор
3. Manual smoke на dev-backend'е (Variant B)
4. Commit + push
5. Merge в `codex/krab-ear-v2` через PR

### 12.2 Rollback

`KRAB_EAR_LLM_ENABLED=false` в `.secrets` + `launchctl kickstart -k gui/$(id -u)/ai.krab.ear.backend`. Backend стартует без `LLM_ENABLED`, ни одного HTTP запроса не делает, диктовка работает как в D.7. Zero-impact revert.

## 13. Known limitations and future work

| Limitation | Mitigation | Future task |
|---|---|---|
| Multi-paragraph диктовки обрезаются на первом `\n\n` | Use case не приоритетен для текущего юзера | Add toggle if requested |
| Нет выбора модели из UI | Env var override через `.secrets` + restart | D.10a.2 |
| Нет model tier auto-selection по длине | Single model покрывает 95% случаев | D.10a.3 |
| Нет кеша одинаковых фраз | Hit rate ~0-2% в dictation, YAGNI | Re-evaluate с метриками |
| Нет retry в sync path | Один shot, fallback на raw | Изменение UX model — out of scope |
| Нет Swift UI toggle | IPC `update_settings` из shell для dev | D.10a.1 |
| macOS absolute path в config.py | Backend уже macOS-only | Accepted |

## 14. Open questions

Ничего не осталось — все вопросы закрыты в brainstorming фазе.

---

**Status:** Design approved by user (sections 1-5). Ready for writing-plans skill invocation.
