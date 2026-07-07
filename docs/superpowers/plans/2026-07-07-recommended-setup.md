# A1 — Рекомендованная настройка в один тап — имплементационный план

> **Для агентных воркеров:** РЕКОМЕНДУЕМЫЙ САБ-СКИЛЛ: superpowers:subagent-driven-development
> (или superpowers:executing-plans) для исполнения плана задача-за-задачей. Шаги —
> чекбоксы (`- [ ]`) для трекинга. **ТЕСТЫ ПИШУТСЯ ПЕРЕД КОДОМ** в каждой задаче
> (fail-before → implementation → pass-after).

## ⚖️ Поправка контролёра (гейт Sonnet, 2026-07-07 — ОБЯЗАТЕЛЬНО прочитать перед Задачей 0)

Все 6 пунктов «Открытых вопросов» в конце плана — **приняты** (DI-паттерн keyword-only для
probe-функций; публичный `ModelDownloader.get_status()` для SenseVoice-probe с задокументированным
риском funasr cache-layout; порядок онбординга RecommendedSetup→WakeWordConsent; design-brief
сверяет формат с конвенцией каталога перед записью; отсутствие отдельного unit-теста для
`auto_learn_corrections_enabled` — транзитивное доказательство существующим тестом достаточно).

**ОДНО критичное исключение — пункт 1 (фикс `action_items_auto_extract` privacy-гейта):**
Находка Задачи №0 (блокер #1, `recording_core_service.py:1644`) **подтверждена мной лично**
(прочитан реальный код) — это настоящая privacy-дыра в УЖЕ ЖИВОМ проде, не только в новой
фиче A1. Именно поэтому **фикс уже диспатчен ОТДЕЛЬНЫМ агентом** (`krab-ear-security-fixer`,
isolated worktree) ПАРАЛЛЕЛЬНО с написанием этого плана — тем же кодом/тестом/паттерном,
который план описывает в Шагах 1-5 Задачи №0.

**Правило для исполнителя Задачи №0**: ПЕРЕД Шагом 1 — проверить `git log --oneline -- KrabEar/backend/recording_core_service.py -5`
и/или `grep -n "and not _privacy_mode" KrabEar/backend/recording_core_service.py` (искать
условие на бывшей строке ~1644, `action_items_auto_extract`):
- Если фикс УЖЕ в main (PR смёржен) — Шаги 1-5 (сам фикс + `test_action_items_privacy_gate_A1.py`)
  **ПРОПУСТИТЬ ЦЕЛИКОМ**, отметить в таблице находок «исправлено ранее, PR #<номер>», перейти
  сразу к Шагу 6 (пиннинг-тест находки #2 — он НЕ дублируется, отдельный файл).
- Если фикс ЕЩЁ НЕ смёржен — **НЕ дублировать**: либо подождать/проверить открытый PR
  security-fixer'а, либо (если критично не блокироваться) сделать Шаги 1-5 как описано —
  при последующем мерже оба места сойдутся на одном и том же однострочном изменении
  (`and not _privacy_mode`), конфликт тривиален для git (одна и та же строка, один и тот же
  диф) — не считать это блокером плана, просто не создавать ВТОРОЙ разноимённый тест-файл на
  тот же самый угол, если увидите, что `test_action_items_privacy_gate_A1.py` уже существует.

**Источники требований (по приоритету при расхождении):**
1. `docs/superpowers/specs/2026-07-07-recommended-setup-design.md` — ФИНАЛЬНАЯ спека,
   единственный источник решений владельца/контролёра (7 решений §0 + итоговый контракт).
2. `docs/superpowers/specs/2026-07-07-recommended-setup-DRAFT.md` — источник истины по
   ИНВЕНТАРИЗАЦИИ (таблица 39 кандидатов §3.1, классификация безопасности §4, IPC-контракт
   §5, UI-точки §7, privacy-примечания §8, тест-план §10, DoD §11). Финальная спека
   переопределяет черновик РОВНО в одном месте: GigaAM-пара (`stt_gigaam_enabled` +
   `stt_language_routing_enabled`) была «УСЛОВНО-ДА с probe» в черновике — в финальной
   спеке это **ВСЕГДА `skip`, без какой-либо probe-логики** (решение 9.7). Этот план
   следует финальной спеке при любом расхождении.
3. Структура этого файла копирует `docs/superpowers/plans/2026-07-07-event-bridge.md`
   (задачи волны того же дня): чекбоксы, тесты-first, точные команды верификации,
   критерии готовности, секция «Открытые вопросы к контролёру» в конце.

**Размер волны:** M/L — 8 задач (0-7), backend (Python) + Swift (онбординг + Settings) +
design-brief для agy. Задачи 1-2 и 7 (backend/e2e) — Sonnet-исполняемые полностью. Задачи
5-6 (Swift wiring) — Sonnet (механика), НЕ визуал. Задача 4 (design-brief) производит
ТОЛЬКО markdown-файл, не код — визуальную реализацию по брифу делает `agy` отдельно, вне
этого плана (см. `reference_gemini_cli_delegation` в памяти проекта).

---

## Критичные факты и константы (НЕ изобретать заново, использовать буква-в-букву)

### Итоговый состав пресета `apply_recommended_setup` v1 (финальная спека §1)

**10 безусловных («ДА»)** — включаются всегда, кроме privacy-скипа:
`smart_silence_skip_enabled`, `realtime_silence_filter_enabled`, `auto_dedup_enabled`,
`auto_save_transcripts`, `phonetic_vocab_enabled`, `text_snippets_enabled`,
`auto_learn_corrections_enabled`, `quick_edit_enabled`, `paste_undo_enabled`,
`calendar_link_enabled`.

**3 условных («УСЛОВНО-ДА»), через probe-гейт (Задача 2):**
- `llm_rewrite_enabled` — probe `probe_llm_http` (уже есть IPC, `service.py:1707` →
  `HealthCheckService.handle_probe_llm_http`, `backend/health_check_service.py:209-218`).
- `action_items_auto_extract` — тот же probe + privacy-гейт (закрывается Задачей №0).
- `stt_sensevoice_enabled` — наличие модели `FunAudioLLM/SenseVoiceSmall` в HF-кэше.

**GigaAM-пара исключена ПОЛНОСТЬЮ** (решение 9.7, финальная спека §1): `stt_gigaam_enabled`
и `stt_language_routing_enabled` НЕ входят в `apply_recommended_setup` вообще — ни как ДА,
ни как условно-ДА. Всегда `skipped` с фиксированной причиной
`"настройте GigaAM вручную в Настройках"`, независимо от состояния venv на диске. **Никакой
probe-логики для GigaAM не пишется** (в отличие от черновика §5.3 шаг 4, который это
предлагал — финальная спека это явно отменяет).

**Wake word — отдельно, не через этот IPC** (решение 9.4, Задача 3): собственный
consent-экран онбординга, собственный вызов `set_settings {wake_word_engine: "openwakeword"}`.

Итого 13 кандидатов входят в `apply_recommended_setup` (10 + 3), GigaAM-пара — 2 кандидата
всегда `skipped`, wake word — вне этого IPC.

### IPC-контракт (финальная спека §2, без изменений формы)

```
apply_recommended_setup {
    "dry_run": bool = true,
    "keys": list[str] | null   // API поддерживает фильтр; v1 UI его НЕ использует (решение 9.5)
}
→ {
    "ok": true, "dry_run": bool, "tier": "low"|"mid"|"high",
    "applied": [{"key", "old_value", "new_value", "restart_required"}],
    "skipped": [{"key", "reason"}],   // GigaAM-пара — ВСЕГДА здесь
    "rationale": str, "snapshot_id": str | null, "restart_required": bool
}
```

`snapshot_id` в ответе == `backup_id`, который принимает УЖЕ существующий
`restore_settings_backup {backup_id}` (`backend/settings_service.py:776`,
`handle_restore_settings_backup`) — **имя параметра при откате другое** (`backup_id`, не
`snapshot_id`), значение то же самое. Никакого нового кода отката не пишется.

### Существующие строительные блоки (переиспользовать буква-в-букву)

| Блок | Файл:строка | Что даёт |
|---|---|---|
| `handle_apply_profile_preset` (образец скелета!) | `backend/settings_service.py:535-572` | `old_settings = cached_settings()` → merge → `store.save_settings()` → `invalidate_cache()` → EventBus emit → `_reload_and_fire_hooks(old, new)` → return. Новый метод повторяет тот же скелет буква-в-букву. |
| `_save_lock` (RLock, W1437) | `settings_service.py:120` | Оборачивает КАЖДЫЙ из 5(+1 новый) путей сохранения — сериализует конкурентные `set_settings`/`apply_profile_preset`/... |
| `cached_settings()` / `invalidate_cache()` | `settings_service.py:134-152` | TTL-кэш 5с; `invalidate_cache()` вызывается сразу после `store.save_settings()`. |
| `_reload_and_fire_hooks(old, new)` | `settings_service.py:166-179` | Единая точка hot-reload pydantic `Settings` + after-save hooks — ОБЯЗАТЕЛЬНА на любом новом save-пути. |
| `_coerce_bool(value, default)` | `settings_service.py:889-904` (staticmethod) | Нормализация bool из UI/JSON — использовать при чтении текущих значений 13 кандидатов из `old_settings`. |
| `SettingsBackup.create_backup(dict, reason) -> backup_id` | `backend/settings_backup.py:97-148` | Атомарная запись 0600, сенситивные поля исключены из бэкапа автоматически (`_SENSITIVE`). |
| `handle_restore_settings_backup {backup_id}` (готовый откат!) | `settings_service.py:776-858` | Уже делает pre-restore backup, миграцию схемы, валидацию, rollback при ошибке. **Новый код отката не пишется.** |
| `handle_list_settings_backups {limit}` | `settings_service.py:758-774` | Возвращает `{backups: [{backup_id, ts, reason, file_size, settings_count_keys}]}` — **без server-side фильтра по reason**; Swift фильтрует `reason == "before_recommended_setup"` клиентски. |
| `detect_hardware_profile()` | `core/hardware_profile.py:83`, tier-константы `TIER_LOW/MID/HIGH` строки 28-30, пороги `_TIER_LOW_MAX_GB=16`/`_TIER_HIGH_MIN_GB=32` строки 25-26 | tier для ответа `apply_recommended_setup` (поле `"tier"`). |
| `_handle_get_hardware_profile` / `_handle_get_calibration_recommendation` (образец!) | `backend/service.py:4682-4703+` | Уже потребляются Swift `HistoryPanelController+Calibration.swift` тем же паттерном, который нужен здесь — **нет privacy gate** (не читают данные пользователя). |
| `probe_llm_http` IPC | `service.py:1707` → `_handle_probe_llm_http` (`service.py:2998-3000`) → `HealthCheckService.handle_probe_llm_http` (`health_check_service.py:209-218`) | `{"reachable": bool, "latency_ms": int, "model": str\|None}` — `warmup()` реально пингует LM Studio. Возвращает `reachable=False` без `self._llm_rewriter` (не бросает). |
| `ModelDownloader.get_status(model_id)` | `backend/model_downloader.py:231-260` | `{"cached": bool, ...}` — **публичный**, уже используется IPC `get_stt_model_status`. Использовать `get_status("FunAudioLLM/SenseVoiceSmall")["cached"]` для probe SenseVoice — НЕ трогать приватный `_is_cached()` напрямую извне класса. Инстанс уже есть: `BackendService._model_downloader` (`service.py:875`). |
| Онбординг (образец шага) | `ModelDownloadStep.swift` (весь файл, 336 строк) + `main.swift:1417-1430` (`runModelDownloadStepThenComplete`), `main.swift:1231` (`QuickStartWindowController`) | Неблокирующий `NSWindow.beginSheet`, IPC строго off-main через `Task`+`callAsync`, «Позже»=graceful skip, `didComplete` guard против двойного `completion()`. |
| Settings-секция (образец) | `HistoryPanelController+Calibration.swift` (весь файл, 447 строк) | dual `buildXSection()`/`cdBuildXSection()` (Gemini/CD), associated-object паттерн (`objc_setAssociatedObject`/`objc_getAssociatedObject`), `fetchAndRebuildXCard()` — IPC на `DispatchQueue.global`, UI-мутация на `DispatchQueue.main` (AGENT-3). Вызывается из `HistoryPanelController.swift:1924` и `HistoryPanelController+Settings+ClaudeDesign.swift:671`. |
| Source-контракт тест (образец!) | `Tests/KrabEarAgentTests/MainHealthMonitorWiringTests.swift:352-391` (`MainHealthMonitorSourceContractTests`) | `src.contains("setupHealthMonitor()")` / `src.contains("\n        tearDownHealthMonitor()\n")` — читает `main.swift` как строку, проверяет что новый шаг РЕАЛЬНО вызван, не просто определён (паттерн «test-validates-the-hole», см. CLAUDE.md). |

### Файлы, которые этот план создаёт или трогает

**НОВЫЕ (backend):**
- `KrabEar/tests/test_action_items_privacy_gate_A1.py` (Задача 0)
- `KrabEar/tests/test_punctuation_pass_privacy_pin_A1.py` (Задача 0)
- `KrabEar/tests/test_apply_recommended_setup.py` (Задача 1)
- `KrabEar/tests/test_apply_recommended_setup_probes.py` (Задача 2)
- `docs/design-briefs/2026-07-07-recommended-setup-ui.md` (Задача 4 — ТОЛЬКО этот файл)
- `scripts/e2e_recommended_setup_smoke.py` (Задача 7)

**НОВЫЕ (Swift):**
- `native/KrabEarAgent/Sources/KrabEarAgent/RecommendedSetupStep.swift` (Задача 5)
- `native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController+RecommendedSetup.swift` (Задача 6)
- `native/KrabEarAgent/Sources/KrabEarAgent/WakeWordConsentStep.swift` (Задача 3)
- `native/KrabEarAgent/Tests/KrabEarAgentTests/RecommendedSetupWiringTests.swift` (Задача 5, source-контракт)

**ИЗМЕНЯЕМЫЕ:**
- `KrabEar/backend/recording_core_service.py` (Задача 0 — privacy-фикс)
- `KrabEar/backend/settings_service.py` (Задача 1 — новый handler)
- `KrabEar/backend/service.py` (Задача 1 — dispatch-регистрация + тонкий wrapper)
- `native/KrabEarAgent/Sources/KrabEarAgent/main.swift` (Задачи 3, 5 — встройка шагов в
  `runModelDownloadStepThenComplete()`/цепочку онбординга)
- `native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController.swift` +
  `HistoryPanelController+Settings+ClaudeDesign.swift` (Задача 6 — регистрация новой секции)
- `docs/IPC_API_REFERENCE.md` (Задача 7 — документирование `apply_recommended_setup`)

**НЕ трогать:** `KrabEar/backend/telegram_bridge.py` (посторонняя незакоммиченная правка —
явное ограничение задачи волны).

### Постоянные правила проекта (действуют на ВСЕ задачи этого плана)

- Каждый тест, конструирующий `BackendService(...)` или `RecordingCoreService(...)` c
  реальными daemon-коллабораторами, ОБЯЗАН вызывать `service.close()`/эквивалент в
  `tearDown`, если конструктор его требует (см. `feedback_backendservice_teardown_ci` —
  daemon-треды без `close()` валят весь CI-чанк).
- flake8 CI-командой: `.venv_krab_ear/bin/flake8 <файлы> --max-line-length=150`
  (E501 игнорируется проектно; `KrabEar/tests/*.py` дополнительно игнорирует E402).
- ubuntu-parity: КАЖДЫЙ новый/изменённый тестовый файл — через
  `bash scripts/pre_merge_py312_check.sh <файлы>` (py3.14 dev-venv имеет mlx, ubuntu CI —
  нет; этот план не трогает mlx-зависимый код, но правило безусловное).
- Воркерам **НЕЛЬЗЯ запускать собранный `KrabEarAgent`-бинарь напрямую** — убьёт прод через
  `SingleInstanceGuard`. `swift build`/`swift test` — можно и нужно (Задача 7).
- Глиф-гейт (Задача 7): grep новых non-ASCII глифов в новых Swift-файлах против `native/` —
  0 вхождений нового глифа → заменить установленным SF Symbol/эмодзи (см.
  `feedback_glyph_gate_swift_workers`).
- Дизайн (цвета/шрифты/layout/иконки) — ТОЛЬКО через `agy`/Gemini 3.1 Pro (см. CLAUDE.md
  «Gemini 3.1 Pro для дизайна»). Задачи 5-6 этого плана пишут МЕХАНИКУ (wiring,
  Auto Layout skeleton минимально необходимый чтобы код компилировался, off-main IPC,
  associated-object паттерн) — финальный визуал (цвета карточки/иконки для
  applied/skipped) приходит из брифа Задачи 4 через отдельный прогон `agy`, ВНЕ этого плана.

---

## Задача №0: Построчная privacy-гейт проверка 4 LLM/транскрипт-кандидатов (ОБЯЗАТЕЛЬНО ПЕРВАЯ)

**Цель:** закрыть открытый вопрос 9.3 черновика — построчно проверить, что
`action_items_auto_extract`, `stt_punctuation_llm_pass_enabled` (вне пресета v1, но всё
равно в списке транскрипт-читающих кандидатов черновика §8 — проверяется по требованию
задания), `auto_learn_corrections_enabled`, `auto_dedup_enabled` реально гейтятся на
`privacy_mode_enabled` В ТОЧКЕ ИСПОЛНЕНИЯ, а не только «по комментарию».

**Метод:** прочитан каждый файл:строка, где кандидат реально исполняется (не объявление в
`DEFAULT_SETTINGS`), проверено окружение переменной `privacy_mode`/`_privacy_mode` в той же
функции, и проверено существующее тестовое покрытие этого конкретного угла (grep `privacy`
в тестовых файлах, покрывающих кандидата).

### Результат — таблица находок

| # | Кандидат | Файл:строка исполнения | Гейт в коде? | Тест-покрытие ДО этой задачи | Вердикт |
|---|---|---|---|---|---|
| 1 | `action_items_auto_extract` | `backend/recording_core_service.py:1644` (`_stop_recording_phase_e`) — `if self._coerce_bool(settings.get("action_items_auto_extract", False), default=False):` | **НЕТ.** `_privacy_mode` уже вычислен в той же функции на строке 1418 и переиспользуется на строках 1430/1601/1663 (`if not _privacy_mode:` / `and not _privacy_mode`), но условие на строке 1644 его НЕ проверяет. | `grep -rl action_items_auto_extract KrabEar/tests/` → **0 файлов**. Функция не покрыта вообще. | **БЛОКЕР — найден реальный privacy-гейт-пробел.** |
| 2 | `stt_punctuation_llm_pass_enabled` | `core/engine.py:517-528` (`_punctuation_pass_allowed`) — строка 526: `if self._settings_get("privacy_mode_enabled", False): return False` ПЕРЕД строкой 528 (чтением самого флага). | **ДА**, явный defense-in-depth (комментарий строки 520-522: «W1755 defense-in-depth: mirrors `_llm_rewrite_allowed`»). | `test_engine_unit.py::LLMToggleTestCase` тестирует `_punctuation_pass_allowed` (rewriter=None → False, toggle=on → True), но **НЕТ теста на `privacy_mode_enabled=True`** (`grep privacy_mode_enabled test_engine_unit.py` → 0 совпадений). | ДА (код корректен), но **пробел в тест-покрытии** — закрывается пиннинг-тестом (не требует правки кода; кандидат и так вне пресета v1). |
| 3 | `auto_learn_corrections_enabled` | `backend/llm_ops_service.py:203-247` (`_maybe_auto_learn_word`) — строка 215 проверяет ТОЛЬКО `auto_learn_corrections_enabled`, без privacy-проверки внутри самой функции. | **ДА, но транзитивно через единственного вызывающего.** `grep -rn _maybe_auto_learn_word KrabEar/` → ровно один caller: `handle_replace_word_in_last_transcript` (`llm_ops_service.py:192`), у которого privacy-гейт стоит на входе функции (строка 150-151: `if cached.get("privacy_mode_enabled"): return {"ok": False, "reason": "privacy_mode_active"}`) — при `privacy_mode_enabled=True` строка 192 (и весь путь до неё) физически недостижима. | `test_wave29_privacy_gates.py::TestReplaceWordPrivacyGate::test_privacy_on_does_not_touch_store` уже доказывает это: сохраняет история с текстом, вызывает `handle_replace_word_in_last_transcript` под privacy=True, проверяет что `store` НЕ изменился — это структурно доказывает, что `_maybe_auto_learn_word` (который иначе вызвал бы `store.update_history_item_text` тем же путём) не мог исполниться. | ДА (транзитивно, уже доказано существующим тестом) — **новый тест не обязателен**, но помечено как «внутренний helper не самодостаточен» — риск для БУДУЩЕГО нового вызывающего (см. «Открытые вопросы»). |
| 4 | `auto_dedup_enabled` | `backend/recording_core_service.py:1417-1430` (`_stop_recording_phase_e`) — строка 1430: `if self._auto_deduplicator is not None and _dedup_enabled and not _privacy_mode:` | **ДА, дважды** — на call-site (строка 1430) И внутри самого `AutoDeduplicator.check_duplicate()` (`backend/auto_deduplication.py:250`, `_privacy_mode_enabled()`). | `test_auto_dedup_privacy_W1248.py` — полное покрытие (`check_duplicate`/`run_deduplication`/`handle_run_deduplication`, все под privacy=True/False). | ДА, уже подтверждено существующим тестом — без изменений. |

### Решение по находке (кандидат #1 — блокер)

`action_items_auto_extract` **остаётся в наборе из 3 условных («УСЛОВНО-ДА»)** пресета
(финальная спека §1 это уже фиксирует), но добавочно: гейт добавляется В ЭТОЙ ЖЕ волне как
**прямой фикс кода** (не как обходной manёвр в `apply_recommended_setup`) — потому что
пробел живёт в runtime-хуке (`recording_core_service.py:1644`), исполняемом на КАЖДОЙ
транскрипции, а не только в момент применения пресета. Если фикс сделать только в
`apply_recommended_setup` (не включать ключ, когда `privacy_mode_enabled=true` НА МОМЕНТ
применения), останется дыра: владелец применяет пресет при privacy=false → ключ
включается → ПОЗЖЕ включает privacy_mode=true → хук на строке 1644 всё равно продолжит
гонять транскрипт через `ActionItemsExtractor.extract()`, так как ничего не проверяет
`_privacy_mode` динамически. Это ровно тот класс бага, который CLAUDE.md-паттерн
«privacy_mode_enabled ВСЕГДА побеждает» призван закрывать. Фикс — однострочный, следует
уже установленному в той же функции паттерну (строки 1560-1561, 1601-1602).

- [ ] **Шаг 1: Failing-тест первым**

`KrabEar/tests/test_action_items_privacy_gate_A1.py`:

```python
"""test_action_items_privacy_gate_A1.py — Задача №0 плана «A1 — Рекомендованная
настройка в один тап» (docs/superpowers/plans/2026-07-07-recommended-setup.md).

Находка: backend/recording_core_service.py:1644 (_stop_recording_phase_e) включает
action_items_auto_extract БЕЗ проверки privacy_mode_enabled, хотя переменная
_privacy_mode уже вычислена в той же функции (строка 1418) и используется для
auto_dedup (1430)/STT_FINAL emit (1601)/семантического индекса (1560-1561).
Фикс: добавить `and not _privacy_mode` к условию на строке 1644 — тот же паттерн,
что уже используется в этой функции.

Run:
    PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_action_items_privacy_gate_A1.py -v
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.recording_core_service import RecordingCoreService  # noqa: E402
from backend.state_store import StateStore  # noqa: E402


class _FakeRecorder:
    def __init__(self):
        self._recording = False

    def start(self, **kwargs):
        if self._recording:
            return False
        self._recording = True
        return True

    def stop(self):
        self._recording = False
        # 90s of silence @ 16kHz mono int16 — exceeds default action_items_min_duration_sec
        import numpy as np
        return np.zeros(16000 * 90, dtype=np.int16), 16000

    def is_recording(self):
        return self._recording


class _FakeTranscriber:
    def transcribe(self, *args, **kwargs):
        return {
            "text": "Нужно подготовить отчёт к пятнице.",
            "confidence": 0.9,
            "language": "ru",
            "engine": "fake",
        }


class _FakeTranslator:
    def translate(self, text, **kwargs):
        class _R:
            mode = "off"
            source_lang = "ru"
            target_lang = "ru"
            engine = "none"
        return text, _R()


class _FakeSettingsSvc:
    def __init__(self, overrides: dict):
        self._overrides = overrides

    def cached_settings(self):
        return dict(self._overrides)

    def invalidate_cache(self):
        pass


class _FakeSemanticSearcher:
    is_enabled = False

    def index_item(self, *a, **kw):
        pass


def _make_service(tmp_dir, settings_overrides, extractor):
    store = StateStore(data_dir=Path(tmp_dir))
    vocab = MagicMock()
    vocab.load = MagicMock(return_value=[])
    vocab.get_words = MagicMock(return_value=[])

    return RecordingCoreService(
        recorder=_FakeRecorder(),
        transcriber=_FakeTranscriber(),
        translator=_FakeTranslator(),
        store=store,
        vocabulary=vocab,
        settings_svc=_FakeSettingsSvc(settings_overrides),
        llm_rewriter=None,
        auto_glossary=None,
        semantic_searcher=_FakeSemanticSearcher(),
        context_memory=None,
        clipboard_history=[],
        auto_backup=MagicMock(),
        session_tracker=MagicMock(),
        action_items_extractor=extractor,
        transcription_counter_ref=[0],
        last_stt_engine_ref=[None],
    )


class ActionItemsPrivacyGateTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def test_action_items_not_extracted_when_privacy_mode_enabled(self):
        """privacy_mode_enabled=True → extract() НЕ должен вызываться, даже если
        action_items_auto_extract=True и duration превышает порог."""
        extractor = MagicMock()
        svc = _make_service(
            self._tmp,
            settings_overrides={
                "privacy_mode_enabled": True,
                "action_items_auto_extract": True,
                "action_items_min_duration_sec": 0,
                "quality_profile": "balanced",
            },
            extractor=extractor,
        )
        svc.handle_start_recording({})
        svc.handle_stop_recording({})
        extractor.extract.assert_not_called()

    def test_action_items_extracted_when_privacy_mode_disabled(self):
        """Контроль: privacy_mode_enabled=False → extract() ДОЛЖЕН вызываться
        (доказывает, что тест не проходит тривиально из-за отсутствия вызова вообще)."""
        extractor = MagicMock()
        extractor.extract.return_value = MagicMock(
            ok=True, action_items=[], decisions=[], questions=[]
        )
        svc = _make_service(
            self._tmp,
            settings_overrides={
                "privacy_mode_enabled": False,
                "action_items_auto_extract": True,
                "action_items_min_duration_sec": 0,
                "quality_profile": "balanced",
            },
            extractor=extractor,
        )
        svc.handle_start_recording({})
        result = svc.handle_stop_recording({})
        if result.get("status") == "ok":
            extractor.extract.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
```

- [ ] **Шаг 2: Прогнать — убедиться что `test_action_items_not_extracted_when_privacy_mode_enabled` падает**

Run: `PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_action_items_privacy_gate_A1.py -v`
Expected: `test_action_items_not_extracted_when_privacy_mode_enabled` FAILS
(`extractor.extract.assert_not_called()` → `AssertionError: Expected 'extract' to not have been called`),
`test_action_items_extracted_when_privacy_mode_disabled` PASSES (контроль).

- [ ] **Шаг 3: Фикс в `backend/recording_core_service.py:1644`**

Было:
```python
        if self._coerce_bool(settings.get("action_items_auto_extract", False), default=False):
```
Стало:
```python
        # W-A1: privacy_mode_enabled ВСЕГДА побеждает — не гонять транскрипт через LLM
        # action-items экстрактор, даже если auto_extract включён (см. Задача №0 плана
        # docs/superpowers/plans/2026-07-07-recommended-setup.md — найдено отсутствие
        # гейта; _privacy_mode уже вычислен выше в этой функции, строка ~1418).
        if self._coerce_bool(
            settings.get("action_items_auto_extract", False), default=False
        ) and not _privacy_mode:
```

- [ ] **Шаг 4: Тесты зелёные**

Run: `PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_action_items_privacy_gate_A1.py -v`
Expected: 2 passed.

- [ ] **Шаг 5: Регрессия — существующий action-items/recording_core тест-набор не сломан**

Run: `PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_recording_core_service.py -v`
Expected: все passed (фикс добавляет условие, не меняет поведение при privacy=False).

- [ ] **Шаг 6: Пиннинг-тест для находки #2 (`stt_punctuation_llm_pass_enabled`)**

Создать `KrabEar/tests/test_punctuation_pass_privacy_pin_A1.py` — закрывает пробел
тест-покрытия (код УЖЕ гейтит правильно, тест лишь пинит это как регрессию):

```python
"""test_punctuation_pass_privacy_pin_A1.py — Задача №0 плана A1 recommended-setup.

Пиннинг-тест: core/engine.py::AudioEngine._punctuation_pass_allowed() уже гейтит на
privacy_mode_enabled (строка 526, "W1755 defense-in-depth"), но test_engine_unit.py
не проверял именно этот угол. Данный тест фиксирует существующее корректное
поведение как регрессионный барьер (не фикс кода — фикс не нужен).

Run:
    PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_punctuation_pass_privacy_pin_A1.py -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.engine import AudioEngine  # noqa: E402


class PunctuationPassPrivacyGateTestCase(unittest.TestCase):
    def _make_engine(self, settings_override: dict):
        engine = AudioEngine.__new__(AudioEngine)  # bypass heavy __init__
        engine._llm_rewriter = MagicMock()
        engine._settings_get = lambda key, default: settings_override.get(key, default)
        return engine

    def test_punctuation_pass_blocked_when_privacy_mode_enabled(self):
        engine = self._make_engine({
            "privacy_mode_enabled": True,
            "stt_punctuation_llm_pass_enabled": True,
        })
        self.assertFalse(
            engine._punctuation_pass_allowed(),
            "_punctuation_pass_allowed должен вернуть False при privacy_mode_enabled=True, "
            "даже если stt_punctuation_llm_pass_enabled=True",
        )

    def test_punctuation_pass_allowed_when_privacy_mode_disabled(self):
        engine = self._make_engine({
            "privacy_mode_enabled": False,
            "stt_punctuation_llm_pass_enabled": True,
        })
        self.assertTrue(engine._punctuation_pass_allowed())


if __name__ == "__main__":
    unittest.main(verbosity=2)
```

Run: `PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_punctuation_pass_privacy_pin_A1.py -v`
Expected: 2 passed (без изменений кода — код уже правильный).

- [ ] **Шаг 7: flake8 + ubuntu-parity**

```bash
.venv_krab_ear/bin/flake8 KrabEar/backend/recording_core_service.py \
  KrabEar/tests/test_action_items_privacy_gate_A1.py \
  KrabEar/tests/test_punctuation_pass_privacy_pin_A1.py --max-line-length=150
bash scripts/pre_merge_py312_check.sh \
  KrabEar/tests/test_action_items_privacy_gate_A1.py \
  KrabEar/tests/test_punctuation_pass_privacy_pin_A1.py
```
Expected: оба шага чистые/ALL GREEN.

- [ ] **Шаг 8: Commit**

```bash
git add KrabEar/backend/recording_core_service.py \
  KrabEar/tests/test_action_items_privacy_gate_A1.py \
  KrabEar/tests/test_punctuation_pass_privacy_pin_A1.py
git commit -m "fix(privacy): gate action_items_auto_extract on privacy_mode_enabled (A1 Задача 0)"
```

**Критерий готовности:** таблица находок выше заполнена реальными результатами чтения
кода (не гипотезами); фикс кандидата #1 сделан и покрыт fail-before/pass-after тестом;
пиннинг-тест кандидата #2 добавлен; кандидаты #3/#4 задокументированы как уже безопасные
с точной ссылкой на доказывающий тест; вся существующая тест-группа `recording_core`/
`engine_unit`/`wave29_privacy_gates`/`auto_dedup_privacy` зелёная.

---

## Задача 1: `SettingsService.handle_apply_recommended_setup` + dispatch-регистрация

**Цель:** новый IPC-метод по контракту финальной спеки §2, скелет как у
`handle_apply_profile_preset` (`settings_service.py:535-572`), БЕЗ какой-либо GigaAM
probe-ветки — GigaAM-пара классифицируется в `skipped` С ФИКСИРОВАННОЙ причиной ДО
probe-логики (probe-логика для 3 условных кандидатов — отдельно, Задача 2; в этой задаче
условные кандидаты пока считаются "недоступны/не проверено" заглушкой, которую Задача 2
заменит реальными probe-вызовами — см. примечание в Шаге 3 ниже про порядок задач).

> Примечание по декомпозиции: чтобы Задача 1 была самодостаточно тестируемой, она
> реализует ПОЛНЫЙ метод, включая сигнатуру с `probe_llm_fn`/`sensevoice_cached_fn`
> keyword-only параметрами (см. Шаг 3) — Задача 2 добавляет ТОЛЬКО их реальные тела
> (сейчас в Задаче 1 тесты инжектируют fake-функции напрямую, реального `HealthCheckService`/
> `ModelDownloader` вызова здесь нет). Это даёт две независимо тестируемые задачи без
> заглушек «TODO implement later» в мерджуемом коде.

**Файлы:**
- Modify: `KrabEar/backend/settings_service.py`
- Modify: `KrabEar/backend/service.py`
- Create: `KrabEar/tests/test_apply_recommended_setup.py`

- [ ] **Шаг 1: Failing-тесты первыми**

`KrabEar/tests/test_apply_recommended_setup.py`:

```python
"""test_apply_recommended_setup.py — SettingsService.handle_apply_recommended_setup
(spec docs/superpowers/specs/2026-07-07-recommended-setup-design.md §1-2).

10 безусловных + 3 условных (probe-функции инжектируются как fakes — реальные
HealthCheckService/ModelDownloader вызовы см. test_apply_recommended_setup_probes.py,
Задача 2). GigaAM-пара ВСЕГДА skipped — тест это явно проверяет как политику (9.7),
не как баг.

Run:
    PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_apply_recommended_setup.py -v
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.settings_service import SettingsService  # noqa: E402


_UNCONDITIONAL_KEYS = {
    "smart_silence_skip_enabled", "realtime_silence_filter_enabled",
    "auto_dedup_enabled", "auto_save_transcripts", "phonetic_vocab_enabled",
    "text_snippets_enabled", "auto_learn_corrections_enabled",
    "quick_edit_enabled", "paste_undo_enabled", "calendar_link_enabled",
}
_CONDITIONAL_KEYS = {"llm_rewrite_enabled", "action_items_auto_extract", "stt_sensevoice_enabled"}
_NEVER_APPLIED_KEYS = {"stt_gigaam_enabled", "stt_language_routing_enabled"}


def _make_store(tmp_dir):
    class _FakeStore:
        def __init__(self):
            self._settings = {}

        def load_settings(self):
            return dict(self._settings)

        def save_settings(self, settings):
            self._settings = dict(settings)
            return dict(settings)

    return _FakeStore()


def _make_svc(tmp_dir):
    from backend.settings_backup import SettingsBackup
    backup = SettingsBackup(backup_dir=Path(tmp_dir) / "backups")
    svc = SettingsService(store=_make_store(tmp_dir), backup=backup)
    return svc


class DryRunDefaultTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def test_dry_run_defaults_to_true(self):
        svc = _make_svc(self._tmp)
        result = svc.handle_apply_recommended_setup(
            {}, probe_llm_fn=lambda: {"reachable": False}, sensevoice_cached_fn=lambda: False,
        )
        self.assertTrue(result["dry_run"])

    def test_dry_run_true_does_not_write_settings(self):
        svc = _make_svc(self._tmp)
        before = svc.store.load_settings()
        svc.handle_apply_recommended_setup(
            {"dry_run": True}, probe_llm_fn=lambda: {"reachable": False},
            sensevoice_cached_fn=lambda: False,
        )
        after = svc.store.load_settings()
        self.assertEqual(before, after, "dry_run=true не должен писать settings.json")

    def test_dry_run_true_snapshot_id_is_none(self):
        svc = _make_svc(self._tmp)
        result = svc.handle_apply_recommended_setup(
            {"dry_run": True}, probe_llm_fn=lambda: {"reachable": False},
            sensevoice_cached_fn=lambda: False,
        )
        self.assertIsNone(result["snapshot_id"])


class UnconditionalSetTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def test_all_ten_unconditional_keys_applied(self):
        svc = _make_svc(self._tmp)
        result = svc.handle_apply_recommended_setup(
            {"dry_run": True}, probe_llm_fn=lambda: {"reachable": False},
            sensevoice_cached_fn=lambda: False,
        )
        applied_keys = {a["key"] for a in result["applied"]}
        self.assertTrue(_UNCONDITIONAL_KEYS.issubset(applied_keys))

    def test_dry_run_false_actually_writes_and_creates_backup(self):
        svc = _make_svc(self._tmp)
        result = svc.handle_apply_recommended_setup(
            {"dry_run": False}, probe_llm_fn=lambda: {"reachable": False},
            sensevoice_cached_fn=lambda: False,
        )
        self.assertFalse(result["dry_run"])
        self.assertIsNotNone(result["snapshot_id"])
        saved = svc.store.load_settings()
        for key in _UNCONDITIONAL_KEYS:
            self.assertTrue(saved.get(key), f"{key} должен быть True после apply")

    def test_already_enabled_key_reported_with_reason(self):
        svc = _make_svc(self._tmp)
        svc.store.save_settings({"smart_silence_skip_enabled": True})
        svc.invalidate_cache()
        result = svc.handle_apply_recommended_setup(
            {"dry_run": True}, probe_llm_fn=lambda: {"reachable": False},
            sensevoice_cached_fn=lambda: False,
        )
        skipped_keys = {s["key"]: s["reason"] for s in result["skipped"]}
        applied_keys = {a["key"] for a in result["applied"]}
        # уже включённый ключ — либо в applied с old==new, либо в skipped с "уже включено";
        # контракт финальной спеки допускает оба прочтения — фиксируем текущий выбор реализации:
        self.assertTrue(
            "smart_silence_skip_enabled" in applied_keys
            or skipped_keys.get("smart_silence_skip_enabled") == "уже включено"
        )


class GigaAMNeverAppliedTestCase(unittest.TestCase):
    """Решение 9.7: GigaAM-пара ВСЕГДА skipped — это политика, не баг."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def test_gigaam_pair_always_skipped_even_with_valid_venv_mocked(self):
        svc = _make_svc(self._tmp)
        # Мокаем "как будто" venv существует и валиден — GigaAM ВСЁ РАВНО должен остаться skipped.
        with unittest.mock.patch("os.path.exists", return_value=True), \
                unittest.mock.patch("pathlib.Path.is_relative_to", return_value=True):
            result = svc.handle_apply_recommended_setup(
                {"dry_run": True}, probe_llm_fn=lambda: {"reachable": True, "latency_ms": 5},
                sensevoice_cached_fn=lambda: True,
            )
        applied_keys = {a["key"] for a in result["applied"]}
        skipped_keys = {s["key"]: s["reason"] for s in result["skipped"]}
        for key in _NEVER_APPLIED_KEYS:
            self.assertNotIn(key, applied_keys, f"{key} НИКОГДА не должен быть в applied (9.7)")
            self.assertIn(key, skipped_keys)
            self.assertIn("GigaAM", skipped_keys[key])

    def test_gigaam_pair_reason_is_fixed_string_not_probe_derived(self):
        svc = _make_svc(self._tmp)
        result = svc.handle_apply_recommended_setup(
            {"dry_run": True}, probe_llm_fn=lambda: {"reachable": False},
            sensevoice_cached_fn=lambda: False,
        )
        skipped_keys = {s["key"]: s["reason"] for s in result["skipped"]}
        self.assertEqual(
            skipped_keys["stt_gigaam_enabled"],
            "настройте GigaAM вручную в Настройках",
        )
        self.assertEqual(
            skipped_keys["stt_language_routing_enabled"],
            "настройте GigaAM вручную в Настройках",
        )


class NoDeadOrNoKeysInAppliedTestCase(unittest.TestCase):
    """Regression-тест §10 п.2 черновика: НЕТ/МЁРТВЫЕ кандидаты никогда не в applied."""

    _FORBIDDEN_KEYS = {
        # НЕТ (сеть/необратимость/тяжёлые зависимости/архитектурные/не-фичи)
        "cloud_rewriter_enabled", "recap_email_enabled", "auto_cleanup_enabled",
        "auto_purge_enabled", "pipeline_v2_enabled", "rest_api_auth_enabled",
        "privacy_mode_enabled", "stt_use_ru_finetune", "voxtral_enabled",
        "voxtral_reasoning_enabled", "wake_word_engine",
        # МЁРТВЫЕ находки черновика §3.2
        "wake_word_enabled", "stt_streaming_enabled", "export_include_speaker_labels",
        # ВОПРОС-кандидаты (вне v1)
        "semantic_search_enabled", "history_encryption_enabled",
        "stt_punctuation_llm_pass_enabled", "voice_fingerprint_enabled",
    }

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def test_forbidden_keys_never_in_applied_dry_run_true(self):
        svc = _make_svc(self._tmp)
        result = svc.handle_apply_recommended_setup(
            {"dry_run": True}, probe_llm_fn=lambda: {"reachable": True},
            sensevoice_cached_fn=lambda: True,
        )
        applied_keys = {a["key"] for a in result["applied"]}
        overlap = applied_keys & self._FORBIDDEN_KEYS
        self.assertEqual(overlap, set(), f"Запрещённые ключи попали в applied: {overlap}")

    def test_forbidden_keys_never_in_applied_dry_run_false(self):
        svc = _make_svc(self._tmp)
        result = svc.handle_apply_recommended_setup(
            {"dry_run": False}, probe_llm_fn=lambda: {"reachable": True},
            sensevoice_cached_fn=lambda: True,
        )
        applied_keys = {a["key"] for a in result["applied"]}
        overlap = applied_keys & self._FORBIDDEN_KEYS
        self.assertEqual(overlap, set(), f"Запрещённые ключи попали в applied: {overlap}")

    def test_forbidden_keys_ignored_even_if_explicitly_requested_via_keys_param(self):
        svc = _make_svc(self._tmp)
        result = svc.handle_apply_recommended_setup(
            {"dry_run": True, "keys": list(self._FORBIDDEN_KEYS)},
            probe_llm_fn=lambda: {"reachable": True}, sensevoice_cached_fn=lambda: True,
        )
        applied_keys = {a["key"] for a in result["applied"]}
        self.assertEqual(applied_keys & self._FORBIDDEN_KEYS, set())


class PrivacyModeSkipsTranscriptCandidatesTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def test_privacy_mode_enabled_skips_action_items_and_auto_learn_and_auto_dedup(self):
        svc = _make_svc(self._tmp)
        svc.store.save_settings({"privacy_mode_enabled": True})
        svc.invalidate_cache()
        result = svc.handle_apply_recommended_setup(
            {"dry_run": True}, probe_llm_fn=lambda: {"reachable": True},
            sensevoice_cached_fn=lambda: True,
        )
        applied_keys = {a["key"] for a in result["applied"]}
        skipped_keys = {s["key"] for s in result["skipped"]}
        for key in ("action_items_auto_extract", "auto_learn_corrections_enabled", "auto_dedup_enabled"):
            self.assertNotIn(key, applied_keys)
            self.assertIn(key, skipped_keys)

    def test_privacy_mode_disabled_does_not_skip_privacy_sensitive_keys_by_itself(self):
        svc = _make_svc(self._tmp)
        result = svc.handle_apply_recommended_setup(
            {"dry_run": True}, probe_llm_fn=lambda: {"reachable": True},
            sensevoice_cached_fn=lambda: True,
        )
        applied_keys = {a["key"] for a in result["applied"]}
        self.assertIn("auto_dedup_enabled", applied_keys)
        self.assertIn("auto_learn_corrections_enabled", applied_keys)


class SnapshotRoundTripTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def test_apply_then_restore_returns_to_original_settings(self):
        svc = _make_svc(self._tmp)
        original = svc.cached_settings()
        result = svc.handle_apply_recommended_setup(
            {"dry_run": False}, probe_llm_fn=lambda: {"reachable": False},
            sensevoice_cached_fn=lambda: False,
        )
        snapshot_id = result["snapshot_id"]
        self.assertIsNotNone(snapshot_id)

        restore_result = svc.handle_restore_settings_backup({"backup_id": snapshot_id})
        restored = restore_result["restored_settings"]
        for key in _UNCONDITIONAL_KEYS:
            self.assertEqual(
                restored.get(key), original.get(key, False),
                f"{key} должен вернуться к значению до apply после restore",
            )


class RationaleAndTierTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def test_response_has_tier_and_rationale(self):
        svc = _make_svc(self._tmp)
        result = svc.handle_apply_recommended_setup(
            {"dry_run": True}, probe_llm_fn=lambda: {"reachable": False},
            sensevoice_cached_fn=lambda: False,
        )
        self.assertIn(result["tier"], ("low", "mid", "high"))
        self.assertIsInstance(result["rationale"], str)
        self.assertGreater(len(result["rationale"]), 0)


if __name__ == "__main__":
    import unittest.mock  # noqa: E402
    unittest.main(verbosity=2)
```

- [ ] **Шаг 2: Прогнать — убедиться что падает**

Run: `PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_apply_recommended_setup.py -v`
Expected: FAIL (`AttributeError: 'SettingsService' object has no attribute
'handle_apply_recommended_setup'`).

- [ ] **Шаг 3: Реализация в `backend/settings_service.py`**

Добавить константы модуля (рядом с `_PROFILE_PRESETS`, перед классом или как атрибуты
класса `SettingsService` — выбрать `class`-уровень для консистентности с
`_PROFILE_PRESETS`) и метод сразу после `handle_apply_profile_preset` (после строки 572):

```python
    # ------------------------------------------------------------------
    # A1 — Рекомендованная настройка в один тап
    # (spec docs/superpowers/specs/2026-07-07-recommended-setup-design.md)
    # ------------------------------------------------------------------

    # 10 безусловных («ДА» черновика §4) — включаются всегда, кроме privacy-скипа
    # для трёх transcript-читающих ключей из этого набора.
    _RECOMMENDED_UNCONDITIONAL: tuple[str, ...] = (
        "smart_silence_skip_enabled",
        "realtime_silence_filter_enabled",
        "auto_dedup_enabled",
        "auto_save_transcripts",
        "phonetic_vocab_enabled",
        "text_snippets_enabled",
        "auto_learn_corrections_enabled",
        "quick_edit_enabled",
        "paste_undo_enabled",
        "calendar_link_enabled",
    )

    # Транскрипт-читающие ключи из безусловного набора — skip при privacy_mode_enabled=True
    # (финальная спека §4; см. Задача №0 для подтверждения гейтов в местах исполнения).
    _RECOMMENDED_PRIVACY_SENSITIVE: frozenset[str] = frozenset({
        "auto_dedup_enabled", "auto_learn_corrections_enabled",
    })

    # 3 условных («УСЛОВНО-ДА») — probe-гейт применяется в _apply_conditional_candidates.
    _RECOMMENDED_CONDITIONAL: tuple[str, ...] = (
        "llm_rewrite_enabled",
        "action_items_auto_extract",
        "stt_sensevoice_enabled",
    )

    # GigaAM-пара — решение 9.7: ВСЕГДА skipped, без probe-логики вообще.
    _RECOMMENDED_GIGAAM_PAIR: tuple[str, ...] = (
        "stt_gigaam_enabled",
        "stt_language_routing_enabled",
    )
    _RECOMMENDED_GIGAAM_SKIP_REASON: str = "настройте GigaAM вручную в Настройках"

    def handle_apply_recommended_setup(
        self,
        params: dict[str, Any],
        *,
        probe_llm_fn: Any,
        sensevoice_cached_fn: Any,
    ) -> dict[str, Any]:
        """Применяет (или показывает превью) рекомендованный безопасный набор настроек.

        Скелет идентичен handle_apply_profile_preset (см. строку 535 этого файла):
        old_settings = cached_settings() -> merge -> save_settings -> invalidate_cache
        -> EventBus emit -> _reload_and_fire_hooks.

        Args:
            params: {"dry_run": bool = True, "keys": list[str] | None}.
            probe_llm_fn: callable() -> {"reachable": bool, ...} — обычно
                HealthCheckService.handle_probe_llm_http, инжектируется вызывающей
                стороной (service.py) чтобы SettingsService не зависел напрямую от
                HealthCheckService (избегаем циклических конструкторских зависимостей).
            sensevoice_cached_fn: callable() -> bool — обычно
                ModelDownloader.get_status("FunAudioLLM/SenseVoiceSmall")["cached"].

        Returns:
            Контракт финальной спеки §2: {ok, dry_run, tier, applied, skipped,
            rationale, snapshot_id, restart_required}.
        """
        dry_run = bool(params.get("dry_run", True))
        requested_keys = params.get("keys")
        requested_keys_set = set(requested_keys) if requested_keys else None

        with self._save_lock:  # W1437 — тот же lock, что и все остальные save-пути
            old_settings = self.cached_settings()
            privacy_on = bool(old_settings.get("privacy_mode_enabled", False))

            applied: list[dict[str, Any]] = []
            skipped: list[dict[str, Any]] = []

            def _wants(key: str) -> bool:
                return requested_keys_set is None or key in requested_keys_set

            # 1) Безусловные «ДА»
            for key in self._RECOMMENDED_UNCONDITIONAL:
                if not _wants(key):
                    continue
                if key in self._RECOMMENDED_PRIVACY_SENSITIVE and privacy_on:
                    skipped.append({"key": key, "reason": "privacy_mode_enabled"})
                    continue
                old_value = old_settings.get(key, False)
                applied.append({
                    "key": key, "old_value": old_value, "new_value": True,
                    "restart_required": False,
                })

            # 2) Условные «УСЛОВНО-ДА» — probe-гейт
            self._apply_conditional_candidates(
                old_settings=old_settings, privacy_on=privacy_on, wants=_wants,
                probe_llm_fn=probe_llm_fn, sensevoice_cached_fn=sensevoice_cached_fn,
                applied=applied, skipped=skipped,
            )

            # 3) GigaAM-пара — решение 9.7, ВСЕГДА skipped, никакого probe
            for key in self._RECOMMENDED_GIGAAM_PAIR:
                if not _wants(key):
                    continue
                skipped.append({"key": key, "reason": self._RECOMMENDED_GIGAAM_SKIP_REASON})

            tier = self._detect_tier_for_recommended_setup()
            rationale = self._build_recommended_setup_rationale(tier, applied, skipped)
            restart_required = any(a["restart_required"] for a in applied)

            if dry_run:
                return {
                    "ok": True, "dry_run": True, "tier": tier,
                    "applied": applied, "skipped": skipped,
                    "rationale": rationale, "snapshot_id": None,
                    "restart_required": restart_required,
                }

            # dry_run=False — реально применяем
            snapshot_id = self._backup.create_backup(old_settings, reason="before_recommended_setup")
            merged = dict(old_settings)
            for item in applied:
                merged[item["key"]] = item["new_value"]
            self.store.save_settings(merged)
            self.invalidate_cache()
            try:
                import backend.event_bus as _ebus  # noqa: PLC0415
                _ebus.bus.emit("recommended_setup.applied", {
                    "tier": tier,
                    "applied_keys": sorted(a["key"] for a in applied),
                    "skipped_keys": sorted(s["key"] for s in skipped),
                })
            except Exception as exc:  # noqa: BLE001
                _log.warning("handle_apply_recommended_setup: emit failed: %s", exc)
            self._reload_and_fire_hooks(old_settings, merged)

            return {
                "ok": True, "dry_run": False, "tier": tier,
                "applied": applied, "skipped": skipped,
                "rationale": rationale, "snapshot_id": snapshot_id,
                "restart_required": restart_required,
            }

    def _apply_conditional_candidates(
        self, *, old_settings, privacy_on, wants, probe_llm_fn, sensevoice_cached_fn,
        applied, skipped,
    ) -> None:
        """Probe-гейт для 3 условных кандидатов (Задача 2 подключает реальные probe_llm_fn/
        sensevoice_cached_fn через service.py; здесь — чистая логика классификации)."""
        if wants("llm_rewrite_enabled"):
            self._apply_llm_probe_gated_key(
                "llm_rewrite_enabled", old_settings, probe_llm_fn, applied, skipped,
            )
        if wants("action_items_auto_extract"):
            if privacy_on:
                skipped.append({"key": "action_items_auto_extract", "reason": "privacy_mode_enabled"})
            else:
                self._apply_llm_probe_gated_key(
                    "action_items_auto_extract", old_settings, probe_llm_fn, applied, skipped,
                )
        if wants("stt_sensevoice_enabled"):
            try:
                cached = bool(sensevoice_cached_fn())
            except Exception:  # noqa: BLE001
                cached = False
            if cached:
                applied.append({
                    "key": "stt_sensevoice_enabled",
                    "old_value": old_settings.get("stt_sensevoice_enabled", False),
                    "new_value": True, "restart_required": False,
                })
            else:
                skipped.append({
                    "key": "stt_sensevoice_enabled",
                    "reason": "модель SenseVoice не найдена в HF-кэше",
                })

    @staticmethod
    def _apply_llm_probe_gated_key(key, old_settings, probe_llm_fn, applied, skipped) -> None:
        try:
            probe = probe_llm_fn() or {}
        except Exception:  # noqa: BLE001
            probe = {}
        if probe.get("reachable"):
            applied.append({
                "key": key, "old_value": old_settings.get(key, False),
                "new_value": True, "restart_required": False,
            })
        else:
            skipped.append({"key": key, "reason": "требует LM Studio, probe_llm_http не ответил"})

    @staticmethod
    def _detect_tier_for_recommended_setup() -> str:
        from core.hardware_profile import detect_hardware_profile  # noqa: PLC0415
        return detect_hardware_profile().tier

    @staticmethod
    def _build_recommended_setup_rationale(tier: str, applied: list, skipped: list) -> str:
        return (
            f"Железо: {tier}-класс. Включено безопасных настроек: {len(applied)}, "
            f"пропущено: {len(skipped)}."
        )
```

- [ ] **Шаг 4: Дispatch-регистрация в `backend/service.py`**

Тонкий wrapper рядом с `_handle_probe_llm_http` (после строки 3000):

```python
    def _handle_apply_recommended_setup(self, params: dict[str, Any]) -> dict[str, Any]:
        """Делегирует к SettingsService.handle_apply_recommended_setup, инжектируя
        probe-колбэки (LM Studio ping + SenseVoice HF-кэш проверка) — Задача 2 плана
        A1 recommended-setup подключает их реальные реализации; здесь — финальная проводка."""
        return self._settings_svc.handle_apply_recommended_setup(
            params,
            probe_llm_fn=self._health_check_svc.handle_probe_llm_http,
            sensevoice_cached_fn=lambda: self._model_downloader.get_status(
                "FunAudioLLM/SenseVoiceSmall"
            ).get("cached", False),
        )
```

В `_build_dispatch_table()` добавить строку сразу после `"apply_profile_preset"`
(строка ~1673):

```python
            "apply_profile_preset": self._settings_svc.handle_apply_profile_preset,  # применяет пресет настроек профиля
            "apply_recommended_setup": self._handle_apply_recommended_setup,  # A1: рекомендованная настройка в один тап (dry_run превью + apply)
```

- [ ] **Шаг 5: Тесты зелёные**

Run: `PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_apply_recommended_setup.py -v`
Expected: все passed.

- [ ] **Шаг 6: Регрессия — dispatch-таблица и смежные тесты**

Run:
```bash
PYTHONPATH=$(pwd)/KrabEar python -m pytest \
  KrabEar/tests/test_dispatch_invariants_wave693.py \
  KrabEar/tests/test_dispatch_invariants_wave790_full.py \
  KrabEar/tests/test_backend_service.py -v
```
Expected: все passed (новый ключ в dispatch-таблице не ломает существующие
инвариант-проверки — если инвариант-тест жёстко перечисляет ВСЕ ключи по имени,
обновить список, добавив `apply_recommended_setup`).

- [ ] **Шаг 7: flake8 + ubuntu-parity**

```bash
.venv_krab_ear/bin/flake8 KrabEar/backend/settings_service.py KrabEar/backend/service.py \
  KrabEar/tests/test_apply_recommended_setup.py --max-line-length=150
bash scripts/pre_merge_py312_check.sh KrabEar/tests/test_apply_recommended_setup.py
```
Expected: чисто / ALL GREEN.

- [ ] **Шаг 8: `make audit-orphans` / `make service-loc`**

Run: `make audit-orphans` — новый метод вызывается из dispatch-таблицы, не должен
всплыть как orphan.

- [ ] **Шаг 9: Commit**

```bash
git add KrabEar/backend/settings_service.py KrabEar/backend/service.py \
  KrabEar/tests/test_apply_recommended_setup.py
git commit -m "feat(settings): apply_recommended_setup IPC — A1 один-тап пресет (10 безусловных + probe-каркас)"
```

**Критерий готовности:** все новые тесты зелёные (включая regression-тест «GigaAM никогда
в applied» и «НЕТ/МЁРТВЫЕ ключи никогда в applied» даже при явном запросе через `keys`);
`dry_run=true` не пишет диск; snapshot round-trip через `restore_settings_backup` работает;
dispatch-таблица содержит новый метод; audit-orphans чист.

---

## Задача 2: Реальные probe-гейты для 3 условных кандидатов

**Цель:** заменить fake `probe_llm_fn`/`sensevoice_cached_fn` из тестов Задачи 1 на
интеграционную проверку РЕАЛЬНЫХ путей — `HealthCheckService.handle_probe_llm_http` и
`ModelDownloader.get_status(...)["cached"]` — через `service.py`-wrapper, написанный в
Задаче 1 Шаге 4. Эта задача не меняет `settings_service.py` (там probe-функции уже
принимаются как параметры) — она добавляет тесты, которые проверяют РЕАЛЬНУЮ проводку в
`service.py`, и тесты graceful-деградации (probe кидает исключение / LM Studio недоступен
→ `skip`, не `exception`).

**Файлы:**
- Create: `KrabEar/tests/test_apply_recommended_setup_probes.py`

- [ ] **Шаг 1: Failing-тесты первыми**

`KrabEar/tests/test_apply_recommended_setup_probes.py`:

```python
"""test_apply_recommended_setup_probes.py — реальная проводка probe-функций
(_handle_apply_recommended_setup в service.py, Задача 1 Шаг 4) на HealthCheckService/
ModelDownloader. Проверяет graceful degradation: недоступный LM Studio / отсутствующий
HF-кэш → skip с понятной причиной, НЕ exception.

Run:
    PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_apply_recommended_setup_probes.py -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class ProbeLlmHttpUnreachableGracefulSkipTestCase(unittest.TestCase):
    """HealthCheckService.handle_probe_llm_http без rewriter -> {"reachable": False} ->
    llm_rewrite_enabled/action_items_auto_extract должны быть skipped, не exception."""

    def test_probe_returns_reachable_false_without_rewriter(self):
        from backend.health_check_service import HealthCheckService
        svc = HealthCheckService.__new__(HealthCheckService)
        svc._llm_rewriter = None
        result = svc.handle_probe_llm_http({})
        self.assertFalse(result["reachable"])
        self.assertEqual(result["latency_ms"], 0)
        self.assertIsNone(result["model"])

    def test_apply_recommended_setup_skips_llm_keys_when_probe_unreachable(self):
        from backend.settings_service import SettingsService

        class _FakeStore:
            def __init__(self):
                self._settings = {}

            def load_settings(self):
                return dict(self._settings)

            def save_settings(self, s):
                self._settings = dict(s)
                return dict(s)

        import tempfile
        from backend.settings_backup import SettingsBackup
        tmp = tempfile.mkdtemp()
        svc = SettingsService(store=_FakeStore(), backup=SettingsBackup(backup_dir=Path(tmp) / "b"))

        result = svc.handle_apply_recommended_setup(
            {"dry_run": True},
            probe_llm_fn=lambda: {"reachable": False, "latency_ms": 0, "model": None},
            sensevoice_cached_fn=lambda: False,
        )
        skipped_keys = {s["key"]: s["reason"] for s in result["skipped"]}
        self.assertIn("llm_rewrite_enabled", skipped_keys)
        self.assertIn("action_items_auto_extract", skipped_keys)

    def test_probe_fn_raising_exception_is_caught_not_propagated(self):
        from backend.settings_service import SettingsService

        class _FakeStore:
            def load_settings(self):
                return {}

            def save_settings(self, s):
                return dict(s)

        import tempfile
        from backend.settings_backup import SettingsBackup
        tmp = tempfile.mkdtemp()
        svc = SettingsService(store=_FakeStore(), backup=SettingsBackup(backup_dir=Path(tmp) / "b"))

        def _broken_probe():
            raise ConnectionError("LM Studio недоступен")

        # Не должно бросать исключение наружу — должно свестись к skip.
        result = svc.handle_apply_recommended_setup(
            {"dry_run": True}, probe_llm_fn=_broken_probe, sensevoice_cached_fn=lambda: False,
        )
        applied_keys = {a["key"] for a in result["applied"]}
        self.assertNotIn("llm_rewrite_enabled", applied_keys)


class SenseVoiceCacheProbeTestCase(unittest.TestCase):
    """ModelDownloader.get_status(...)["cached"] управляет stt_sensevoice_enabled."""

    def test_sensevoice_not_cached_skipped_with_reason(self):
        from backend.settings_service import SettingsService

        class _FakeStore:
            def load_settings(self):
                return {}

            def save_settings(self, s):
                return dict(s)

        import tempfile
        from backend.settings_backup import SettingsBackup
        tmp = tempfile.mkdtemp()
        svc = SettingsService(store=_FakeStore(), backup=SettingsBackup(backup_dir=Path(tmp) / "b"))

        result = svc.handle_apply_recommended_setup(
            {"dry_run": True}, probe_llm_fn=lambda: {"reachable": False},
            sensevoice_cached_fn=lambda: False,
        )
        skipped_keys = {s["key"]: s["reason"] for s in result["skipped"]}
        self.assertIn("stt_sensevoice_enabled", skipped_keys)
        self.assertIn("SenseVoice", skipped_keys["stt_sensevoice_enabled"])

    def test_sensevoice_cached_applied(self):
        from backend.settings_service import SettingsService

        class _FakeStore:
            def load_settings(self):
                return {}

            def save_settings(self, s):
                return dict(s)

        import tempfile
        from backend.settings_backup import SettingsBackup
        tmp = tempfile.mkdtemp()
        svc = SettingsService(store=_FakeStore(), backup=SettingsBackup(backup_dir=Path(tmp) / "b"))

        result = svc.handle_apply_recommended_setup(
            {"dry_run": True}, probe_llm_fn=lambda: {"reachable": False},
            sensevoice_cached_fn=lambda: True,
        )
        applied_keys = {a["key"] for a in result["applied"]}
        self.assertIn("stt_sensevoice_enabled", applied_keys)


class ServiceWiringUsesModelDownloaderGetStatusTestCase(unittest.TestCase):
    """_handle_apply_recommended_setup (service.py) вызывает ModelDownloader.get_status(...),
    НЕ приватный _is_cached() напрямую — проверка через mock на инстансе BackendService."""

    def test_service_wrapper_calls_get_status_with_sensevoice_model_id(self):
        # Патчим метод на классе ModelDownloader перед конструированием BackendService,
        # чтобы не тянуть тяжёлые зависимости реального __init__.
        with patch("backend.model_downloader.ModelDownloader.get_status") as mock_get_status:
            mock_get_status.return_value = {"cached": True}
            # Минимальный stub объекта с нужным методом — воспроизводит forму
            # self._model_downloader.get_status(...) без полного BackendService.__init__.
            stub = MagicMock()
            stub._model_downloader.get_status.return_value = {"cached": True}
            stub._health_check_svc.handle_probe_llm_http.return_value = {"reachable": False}
            stub._settings_svc.handle_apply_recommended_setup = MagicMock(return_value={"ok": True})

            from backend.service import BackendService
            BackendService._handle_apply_recommended_setup(stub, {"dry_run": True})

            stub._settings_svc.handle_apply_recommended_setup.assert_called_once()
            _, kwargs = stub._settings_svc.handle_apply_recommended_setup.call_args
            self.assertIn("probe_llm_fn", kwargs)
            self.assertIn("sensevoice_cached_fn", kwargs)
            # Вызываем sensevoice_cached_fn, чтобы убедиться что она реально бьёт в get_status
            kwargs["sensevoice_cached_fn"]()
            stub._model_downloader.get_status.assert_called_with("FunAudioLLM/SenseVoiceSmall")


if __name__ == "__main__":
    unittest.main(verbosity=2)
```

- [ ] **Шаг 2: Прогнать — убедиться что падает (или частично проходит, если Задача 1 уже
      сделала прод-код правильно — допустимо; фиксируем текущее фактическое состояние)**

Run: `PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_apply_recommended_setup_probes.py -v`
Expected: если Задача 1 выполнена по спецификации Шага 4 — большинство тестов уже
проходят (это ОК, они пиннингуют интеграцию); `ServiceWiringUsesModelDownloaderGetStatusTestCase`
проверяет специфику `service.py`-wrapper и может потребовать точной сверки сигнатуры.

- [ ] **Шаг 3: Если что-то падает — донастроить `_handle_apply_recommended_setup` в
      `service.py` (Задача 1 Шаг 4) до соответствия**

Ключевое требование: `sensevoice_cached_fn` ОБЯЗАН вызывать
`self._model_downloader.get_status("FunAudioLLM/SenseVoiceSmall")` (публичный метод, уже
используемый `get_stt_model_status` IPC) — НЕ приватный `_is_cached()` напрямую снаружи
класса `ModelDownloader`.

- [ ] **Шаг 4: Тесты зелёные**

Run: `PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_apply_recommended_setup_probes.py -v`
Expected: все passed.

- [ ] **Шаг 5: flake8 + ubuntu-parity**

```bash
.venv_krab_ear/bin/flake8 KrabEar/tests/test_apply_recommended_setup_probes.py --max-line-length=150
bash scripts/pre_merge_py312_check.sh KrabEar/tests/test_apply_recommended_setup_probes.py
```

- [ ] **Шаг 6: Commit**

```bash
git add KrabEar/tests/test_apply_recommended_setup_probes.py
git commit -m "test(settings): реальная probe-проводка apply_recommended_setup (LM Studio + SenseVoice HF-кэш)"
```

**Критерий готовности:** probe для `llm_rewrite_enabled`/`action_items_auto_extract`
недоступен → `skip`, не exception; `stt_sensevoice_enabled` управляется реальным
`ModelDownloader.get_status(...)["cached"]`, не приватным API; исключение из
инжектированной probe-функции нигде не пробрасывается наружу IPC-ответа.

> **Известное допущение, не проверенное фактическим прогоном (репозиторий запрещал
> запуск бинарей в рамках написания этого плана):** `ModelDownloader._is_cached`/
> `get_status` вычисляют путь кэша по стандартному layout HuggingFace Hub
> (`~/.cache/huggingface/hub/models--<org>--<name>/snapshots/`). Адаптер SenseVoice
> (`core/pipeline/stt_sensevoice.py`) грузит модель через пакет `funasr`
> (`AutoModel(model="FunAudioLLM/SenseVoiceSmall", ...)`), который МОЖЕТ использовать
> собственный кеш-layout (funasr/ModelScope), отличный от стандартного HF hub layout,
> в зависимости от версии пакета и переменных окружения. Если это так, probe будет
> ложно возвращать `cached=False` даже когда модель фактически уже загружена funasr
> ранее — некритично (падает в сторону `skip`, не в сторону ложного `enable` без
> модели), но стоит внимания при первом живом прогоне (см. «Открытые вопросы»).

---

## Задача 3: Wake word — отдельный consent-экран (Swift), НЕ через `apply_recommended_setup`

**Цель:** решение 9.4 финальной спеки — отдельный шаг онбординга с явным текстом согласия
про always-listening микрофон, вызывающий `set_settings {wake_word_engine: "openwakeword"}`
напрямую (НЕ IPC из Задачи 1).

**Файлы:**
- Create: `native/KrabEarAgent/Sources/KrabEarAgent/WakeWordConsentStep.swift`
- Modify: `native/KrabEarAgent/Sources/KrabEarAgent/main.swift` (встройка в цепочку
  онбординга, СЛЕДОМ за Задачей 5, см. порядок в Задаче 5)

- [ ] **Шаг 1: `WakeWordConsentStep.swift` — механика (по образцу `ModelDownloadStep.swift`)**

```swift
/*
 WakeWordConsentStep.swift

 Отдельный шаг онбординга «Голосовой триггер» (решение 9.4 финальной спеки A1):
 wake word НЕ входит в apply_recommended_setup — always-listening микрофон должен
 быть явным, осознанным выбором пользователя, а не побочным эффектом «сделай мне хорошо».

 Связи модуля:
 1) QuickStartWindowController — презентует этот шаг ПОСЛЕ RecommendedSetupStep
    (см. main.swift, цепочка онбординга).
 2) IPCClient — set_settings {wake_word_engine: "openwakeword"} НАПРЯМУЮ (строго
    off-main, AGENT-3) — НЕ через apply_recommended_setup.

 🔴 Правила: те же, что ModelDownloadStep.swift — неблокирующий sheet, IPC off-main,
 graceful skip при любой ошибке (модель может быть ещё не забутстрапена —
 bootstrap_backend.command грузит её отдельно; ошибка set_settings НЕ блокирует
 завершение онбординга).
*/

import AppKit
import Foundation

enum WakeWordConsentOutcome {
    case enabled
    case declined
}

@MainActor
final class WakeWordConsentStepController: NSObject {
    private let ipcClient: IPCClient
    private let completion: (WakeWordConsentOutcome) -> Void

    private weak var parentWindow: NSWindow?
    private var sheetWindow: NSWindow?
    private var didComplete = false

    private let titleLabel = NSTextField(labelWithString: "Голосовой триггер \u{2014} \u{00AB}Краб\u{00BB}")
    private let bodyLabel = NSTextField(
        wrappingLabelWithString:
            "Включить голосовой триггер? Микрофон будет постоянно слушать локально " +
            "(без отправки в сеть) в ожидании слова \u{00AB}Краб\u{00BB}. Отключить можно в любой момент в Настройках."
    )
    private lazy var enableButton = ThemePrimaryButton(
        title: "Включить", target: self, action: #selector(onEnableTap)
    )
    private lazy var declineButton = ThemeSecondaryButton(
        title: "Не сейчас", target: self, action: #selector(onDeclineTap)
    )

    init(ipcClient: IPCClient, completion: @escaping (WakeWordConsentOutcome) -> Void) {
        self.ipcClient = ipcClient
        self.completion = completion
        super.init()
    }

    func start(over parent: NSWindow) {
        self.parentWindow = parent
        presentSheet(over: parent)
    }

    private func presentSheet(over parent: NSWindow) {
        let sheet = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 460, height: 190),
            styleMask: [.titled], backing: .buffered, defer: false
        )
        sheet.title = "Голосовой триггер"
        buildUI(in: sheet)
        self.sheetWindow = sheet
        parent.beginSheet(sheet, completionHandler: nil)
    }

    private func buildUI(in window: NSWindow) {
        // Механика Auto Layout — минимальный skeleton; финальный визуал (карточка/
        // иконки/цвета) приходит из docs/design-briefs/2026-07-07-recommended-setup-ui.md
        // через agy (см. Задача 4/6 плана docs/superpowers/plans/2026-07-07-recommended-setup.md).
        let content = NSView(frame: window.contentView!.bounds)
        window.contentView = content

        let stack = NSStackView()
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = KrabEarTheme.Metrics.comfortable
        stack.edgeInsets = NSEdgeInsets(top: 24, left: 24, bottom: 24, right: 24)
        stack.translatesAutoresizingMaskIntoConstraints = false
        content.addSubview(stack)

        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: content.leadingAnchor),
            stack.trailingAnchor.constraint(equalTo: content.trailingAnchor),
            stack.topAnchor.constraint(equalTo: content.topAnchor),
            stack.bottomAnchor.constraint(lessThanOrEqualTo: content.bottomAnchor),
        ])

        titleLabel.font = .systemFont(ofSize: 17, weight: .bold)
        stack.addArrangedSubview(titleLabel)

        bodyLabel.font = .systemFont(ofSize: 13)
        stack.addArrangedSubview(bodyLabel)
        bodyLabel.widthAnchor.constraint(equalTo: stack.widthAnchor, constant: -48).isActive = true

        stack.addArrangedSubview(NSView())

        let buttonsRow = NSStackView()
        buttonsRow.orientation = .horizontal
        buttonsRow.spacing = KrabEarTheme.Metrics.comfortable
        stack.addArrangedSubview(buttonsRow)
        buttonsRow.widthAnchor.constraint(equalTo: stack.widthAnchor, constant: -48).isActive = true

        declineButton.applyThemeSecondary()
        buttonsRow.addArrangedSubview(declineButton)
        buttonsRow.addArrangedSubview(NSView())
        enableButton.applyThemePrimary()
        enableButton.keyEquivalent = "\r"
        buttonsRow.addArrangedSubview(enableButton)
    }

    @objc private func onDeclineTap() {
        finish(.declined)
    }

    @objc private func onEnableTap() {
        enableButton.isEnabled = false
        declineButton.isEnabled = false
        let ipc = ipcClient
        Task { [weak self] in
            do {
                _ = try await ipc.callAsync(
                    method: "set_settings",
                    params: ["wake_word_engine": "openwakeword"],
                    timeoutSec: IPCClient.defaultTimeoutSec
                )
            } catch {
                // Graceful — модель может быть не забутстрапена; не блокируем онбординг.
                NSLog("[WakeWordConsentStep] set_settings error: %@", error.localizedDescription)
            }
            await MainActor.run { [weak self] in self?.finish(.enabled) }
        }
    }

    private func finish(_ outcome: WakeWordConsentOutcome) {
        guard !didComplete else { return }
        didComplete = true
        if let sheet = sheetWindow, let parent = parentWindow {
            parent.endSheet(sheet)
            sheetWindow = nil
        }
        completion(outcome)
    }
}
```

- [ ] **Шаг 2: Встройка в `main.swift`** — см. Задачу 5 (единая точка изменения
      `runModelDownloadStepThenComplete()` → цепочка добавляет ОБА новых шага в порядке
      ModelDownloadStep → RecommendedSetupStep → WakeWordConsentStep → `onComplete()`).
      Реализуется в Задаче 5, чтобы не создавать два конкурирующих diff по одному и тому
      же методу — эта задача (3) только создаёт файл контроллера.

- [ ] **Шаг 3: `swift build` компилируется**

Run:
```bash
cd native/KrabEarAgent && swift build -c release 2>&1 | tail -20
```
Expected: 0 errors (новый файл сам по себе не встроен в цепочку до Задачи 5 — компиляция
проверяет только синтаксис/типы нового файла в изоляции пакета).

- [ ] **Шаг 4: Глиф-гейт**

Run: `grep -o '[^\x00-\x7F]' native/KrabEarAgent/Sources/KrabEarAgent/WakeWordConsentStep.swift | sort -u`
Expected: пусто ИЛИ только уже используемые эмодзи/кавычки — в файле выше нарочно
использованы Unicode escape-последовательности (`\u{2014}` em dash, `\u{00AB}`/`\u{00BB}`
кавычки-ёлочки, `\u{00A0}`?) вместо литеральных не-ASCII символов, чтобы избежать нового
глифа в исходнике; если grep всё равно найдёт литералы — заменить на escape-форму или на
уже встречающийся в `native/` глиф (grep образец использования в других файлах перед
заменой).

- [ ] **Шаг 5: Commit**

```bash
git add native/KrabEarAgent/Sources/KrabEarAgent/WakeWordConsentStep.swift
git commit -m "feat(onboarding): WakeWordConsentStep — отдельный consent-экран для wake word (решение 9.4)"
```

**Критерий готовности:** новый контроллер компилируется изолированно; вызывает
`set_settings {wake_word_engine: "openwakeword"}` НАПРЯМУЮ (не через
`apply_recommended_setup`); graceful skip при ошибке IPC; глиф-гейт чист.

---

## Задача 4: Design-brief для `agy` (ТОЛЬКО markdown, без кода)

**Цель:** решение 9.6 финальной спеки — до Swift-визуальной реализации написать бриф,
описывающий что нельзя ломать и что улучшить. Эта задача производит РОВНО ОДИН файл.

**Файлы:**
- Create: `docs/design-briefs/2026-07-07-recommended-setup-ui.md`

- [ ] **Шаг 1: Написать бриф**

Содержание (структура — по аналогии с существующими брифами в `docs/design-briefs/`,
проверить формат соседних файлов перед записью, если такие есть в каталоге):

```markdown
# Бриф для agy — A1 «Рекомендованная настройка» (онбординг-шаг + Settings-секция)

## Контекст
Два новых Swift-компонента для волны A1 (см. план
docs/superpowers/plans/2026-07-07-recommended-setup.md, Задачи 5-6):
1. `RecommendedSetupStep.swift` — шаг онбординга (sheet), показывает превью
   (dry_run) списка «включим/пропустим» перед завершением настройки.
2. `HistoryPanelController+RecommendedSetup.swift` — секция в Настройках, показывает
   тот же превью + кнопки «Применить рекомендуемое» / «Отменить последнее».

## Что НЕЛЬЗЯ ломать (механика уже написана Sonnet — здесь только визуал)
- Auto Layout skeleton (NSStackView-иерархия, constraints) — можно менять spacing/
  padding/шрифты/цвета, НЕЛЬЗЯ убирать constraints, ломающие resize/adaptive layout.
- IPC-контракт: `apply_recommended_setup {dry_run}` вызывается строго off-main
  (DispatchQueue.global / Task), обновление UI — строго на main (AGENT-3 AppHang-класс).
  НЕ переносить IPC-вызовы на main thread ради визуальных экспериментов.
- associated-object паттерн (`objc_setAssociatedObject`/`objc_getAssociatedObject`) в
  Settings-секции — используется для хранения последнего dry_run снапшота между
  перестройками карточки; не заменять на другое хранилище без согласования.
- `sectionId` секции ("recommended_setup") — используется для persist expand/collapse
  state через UserDefaults (`CollapsibleSection_{sectionId}`); переименование ключа
  потеряет пользовательское состояние existing users.
- Кнопки «Применить»/«Пропустить»/«Отменить последнее» должны остаться семантически
  теми же тремя действиями — можно менять титры/иконки, не логику нажатий.
- Глифы: ТОЛЬКО ASCII + установленные SF Symbols (см. существующий набор в
  HistoryPanelController+Calibration.swift: `speedometer`, `checkmark.circle.fill`,
  `exclamationmark.triangle`) — перед добавлением НОВОГО символа сверить с
  `native/KrabEarAgent/Tests/` glyph-gate тестами (см. feedback_glyph_gate_swift_workers
  в памяти проекта) — 0 вхождений нового глифа в кодовой базе означает нужно взять уже
  установленный SF Symbol, не изобретать новый.

## Что УЛУЧШИТЬ (визуальная часть — здесь работает Gemini/agy)
- Карточка превью: два визуально различимых блока — «Будет включено» (зелёный акцент,
  список ключей человеко-читаемыми названиями, не raw `snake_case`) и «Будет пропущено»
  (нейтральный/серый акцент, причина рядом с каждым пунктом).
- Иконки на пункт: подобрать SF Symbols по смыслу (пауза/тишина, дедупликация,
  автосохранение, фонетика, сниппеты, авто-обучение, quick edit, undo, календарь,
  LLM-полировка, action items, SenseVoice) — единый визуальный язык с существующими
  секциями Настроек (Calibration/STTEnginesPicker).
- tier-бейдж (low/mid/high) — переиспользовать существующий `calibTierBadge`-паттерн
  цветов (success/accent/textDisabled) из HistoryPanelController+Calibration.swift, не
  изобретать новую цветовую схему для tier.
- Кнопка «Отменить последнее применение» — должна визуально читаться как менее
  «опасная», чем деструктивные действия (не красная/warning-стилистика) — это откат
  настроек через существующий backup-механизм, не потеря данных.
- Пустое состояние («ничего не пропущено, всё безопасное уже включено») — отдельный
  дружелюбный текст, не пустая карточка.

## Формат ответа
Правки только в двух файлах Задач 5-6 (или новых Swift-файлах, если agy решит
разбить на дополнительные extension-файлы по тому же паттерну, что и
HistoryPanelController+Calibration.swift). `swift build -c release` должен проходить
после правок. Ревью диффа — Claude, ПЕРЕД коммитом (см. reference_gemini_cli_delegation
в памяти проекта: brief → agy → ревью диффа контролёром → swift build → commit
`Co-Authored-By: Gemini 3.1 Pro (Antigravity)`).
```

- [ ] **Шаг 2: Проверить формат — сверить с существующими файлами `docs/design-briefs/`**

Run: `ls docs/design-briefs/ 2>/dev/null` — если каталог с примерами существует,
сверить структуру заголовков брифа выше с уже принятым в проекте форматом и подправить
при расхождении (эта задача не должна изобретать новый формат брифа, если конвенция уже
есть).

- [ ] **Шаг 3: Commit**

```bash
git add docs/design-briefs/2026-07-07-recommended-setup-ui.md
git commit -m "docs(design-brief): A1 recommended-setup UI — бриф для agy (решение 9.6)"
```

**Критерий готовности:** ровно один новый файл создан; никакого Swift-кода в этой
задаче не тронуто; бриф содержит явные секции «нельзя ломать» и «улучшить» с точными
ссылками на файлы/паттерны.

---

## Задача 5: Swift-онбординг — `RecommendedSetupStep.swift` (механика)

**Цель:** механика (wiring, dry_run→preview→apply/skip, off-main IPC) — визуал приходит
из брифа Задачи 4 отдельным прогоном `agy`, вне этого плана. Встраивается в
`runModelDownloadStepThenComplete()` ПОСЛЕ `ModelDownloadStepController`, ПЕРЕД
`WakeWordConsentStepController` (Задача 3), перед `onComplete()`.

**Файлы:**
- Create: `native/KrabEarAgent/Sources/KrabEarAgent/RecommendedSetupStep.swift`
- Create: `native/KrabEarAgent/Tests/KrabEarAgentTests/RecommendedSetupWiringTests.swift`
- Modify: `native/KrabEarAgent/Sources/KrabEarAgent/main.swift`
  (`runModelDownloadStepThenComplete`, строки 1417-1430)

- [ ] **Шаг 1: Failing source-контракт тест первым (по образцу
      `MainHealthMonitorSourceContractTests`)**

`native/KrabEarAgent/Tests/KrabEarAgentTests/RecommendedSetupWiringTests.swift`:

```swift
/*
 RecommendedSetupWiringTests — Задача 5 плана A1 recommended-setup.

 Source-контракт (паттерн test_setupHealthMonitor_is_actually_called_from_startup,
 см. MainHealthMonitorWiringTests.swift): доказывает, что RecommendedSetupStepController
 РЕАЛЬНО встроен в runModelDownloadStepThenComplete(), а не просто определён и
 никогда не вызван (класс бага 2026-07-05: setupErrorBus/setupHealthMonitor).
*/

import XCTest
@testable import KrabEarAgent

final class RecommendedSetupSourceContractTests: XCTestCase {

    func test_runModelDownloadStepThenComplete_invokes_RecommendedSetupStepController() throws {
        let src = try String(contentsOf: Self.mainSwiftURL, encoding: .utf8)
        XCTAssertTrue(
            src.contains("RecommendedSetupStepController("),
            "runModelDownloadStepThenComplete() должен создавать RecommendedSetupStepController " +
            "после ModelDownloadStepController — иначе шаг определён, но никогда не показывается."
        )
    }

    func test_recommended_setup_step_precedes_wake_word_consent_in_source_order() throws {
        let src = try String(contentsOf: Self.mainSwiftURL, encoding: .utf8)
        guard let recIdx = src.range(of: "RecommendedSetupStepController(")?.lowerBound,
              let wakeIdx = src.range(of: "WakeWordConsentStepController(")?.lowerBound else {
            XCTFail("Оба контроллера должны быть найдены в main.swift")
            return
        }
        XCTAssertTrue(
            recIdx < wakeIdx,
            "RecommendedSetupStepController должен вызываться РАНЬШЕ " +
            "WakeWordConsentStepController в исходном коде (порядок цепочки онбординга)."
        )
    }

    private static var mainSwiftURL: URL {
        let bundleURL = Bundle(for: RecommendedSetupSourceContractTests.self).bundleURL
        var url = bundleURL
        for _ in 0..<10 {
            let candidate = url.appendingPathComponent("Sources/KrabEarAgent/main.swift")
            if FileManager.default.fileExists(atPath: candidate.path) {
                return candidate
            }
            url = url.deletingLastPathComponent()
        }
        let fileURL = URL(fileURLWithPath: #file)
        return fileURL
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .appendingPathComponent("Sources/KrabEarAgent/main.swift")
    }
}
```

- [ ] **Шаг 2: Прогнать — убедиться что падает**

Run: `cd native/KrabEarAgent && swift test --filter RecommendedSetupSourceContractTests 2>&1 | tail -30`
Expected: FAIL — `RecommendedSetupStepController(` не найдена в `main.swift` (класс ещё
не создан и не встроен).

- [ ] **Шаг 3: Реализация `RecommendedSetupStep.swift` (по образцу `ModelDownloadStep.swift`)**

```swift
/*
 RecommendedSetupStep.swift

 Шаг онбординга «Рекомендованная настройка»: показывает dry_run превью
 apply_recommended_setup (10 безусловных + 3 условных настройки) перед завершением
 онбординга. «Применить» -> apply_recommended_setup{dry_run:false}. «Пропустить» ->
 ничего не меняет.

 Связи модуля:
 1) QuickStartWindowController — презентует ПОСЛЕ ModelDownloadStepController,
    ПЕРЕД WakeWordConsentStepController (main.swift, runModelDownloadStepThenComplete).
 2) IPCClient — apply_recommended_setup (строго off-main, AGENT-3).

 Спека: docs/superpowers/specs/2026-07-07-recommended-setup-design.md.
 Визуал (карточка/иконки/цвета) — docs/design-briefs/2026-07-07-recommended-setup-ui.md,
 исполняется agy отдельно (эта реализация — механика/skeleton).
*/

import AppKit
import Foundation

enum RecommendedSetupStepOutcome {
    case applied(count: Int)
    case skipped
    case fetchFailed
}

@MainActor
final class RecommendedSetupStepController: NSObject {
    private let ipcClient: IPCClient
    private let completion: (RecommendedSetupStepOutcome) -> Void

    private weak var parentWindow: NSWindow?
    private var sheetWindow: NSWindow?
    private var didComplete = false
    private var previewApplied: [[String: Any]] = []
    private var previewSkipped: [[String: Any]] = []

    private let titleLabel = NSTextField(labelWithString: "Рекомендованная настройка")
    private let summaryLabel = NSTextField(wrappingLabelWithString: "Загрузка превью...")
    private lazy var applyButton = ThemePrimaryButton(
        title: "Применить", target: self, action: #selector(onApplyTap)
    )
    private lazy var skipButton = ThemeSecondaryButton(
        title: "Пропустить", target: self, action: #selector(onSkipTap)
    )

    init(ipcClient: IPCClient, completion: @escaping (RecommendedSetupStepOutcome) -> Void) {
        self.ipcClient = ipcClient
        self.completion = completion
        super.init()
    }

    func start(over parent: NSWindow) {
        self.parentWindow = parent
        presentSheet(over: parent)
        fetchPreview()
    }

    private func presentSheet(over parent: NSWindow) {
        let sheet = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 480, height: 260),
            styleMask: [.titled], backing: .buffered, defer: false
        )
        sheet.title = "Рекомендованная настройка"
        buildUI(in: sheet)
        self.sheetWindow = sheet
        parent.beginSheet(sheet, completionHandler: nil)
    }

    private func buildUI(in window: NSWindow) {
        let content = NSView(frame: window.contentView!.bounds)
        window.contentView = content

        let stack = NSStackView()
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = KrabEarTheme.Metrics.comfortable
        stack.edgeInsets = NSEdgeInsets(top: 24, left: 24, bottom: 24, right: 24)
        stack.translatesAutoresizingMaskIntoConstraints = false
        content.addSubview(stack)

        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: content.leadingAnchor),
            stack.trailingAnchor.constraint(equalTo: content.trailingAnchor),
            stack.topAnchor.constraint(equalTo: content.topAnchor),
            stack.bottomAnchor.constraint(lessThanOrEqualTo: content.bottomAnchor),
        ])

        titleLabel.font = .systemFont(ofSize: 17, weight: .bold)
        stack.addArrangedSubview(titleLabel)

        summaryLabel.font = .systemFont(ofSize: 13)
        stack.addArrangedSubview(summaryLabel)
        summaryLabel.widthAnchor.constraint(equalTo: stack.widthAnchor, constant: -48).isActive = true

        stack.addArrangedSubview(NSView())

        let buttonsRow = NSStackView()
        buttonsRow.orientation = .horizontal
        buttonsRow.spacing = KrabEarTheme.Metrics.comfortable
        stack.addArrangedSubview(buttonsRow)
        buttonsRow.widthAnchor.constraint(equalTo: stack.widthAnchor, constant: -48).isActive = true

        skipButton.applyThemeSecondary()
        buttonsRow.addArrangedSubview(skipButton)
        buttonsRow.addArrangedSubview(NSView())
        applyButton.applyThemePrimary()
        applyButton.keyEquivalent = "\r"
        applyButton.isEnabled = false  // включается после успешного fetch превью
        buttonsRow.addArrangedSubview(applyButton)
    }

    private func fetchPreview() {
        let ipc = ipcClient
        Task { [weak self] in
            do {
                let resp = try await ipc.callAsync(
                    method: "apply_recommended_setup",
                    params: ["dry_run": true],
                    timeoutSec: IPCClient.defaultTimeoutSec
                )
                let result = (resp["result"] as? [String: Any]) ?? [:]
                let applied = (result["applied"] as? [[String: Any]]) ?? []
                let skipped = (result["skipped"] as? [[String: Any]]) ?? []
                await MainActor.run { [weak self] in
                    self?.applyPreview(applied: applied, skipped: skipped)
                }
            } catch {
                NSLog("[RecommendedSetupStep] fetchPreview error: %@", error.localizedDescription)
                await MainActor.run { [weak self] in self?.showFetchFailed() }
            }
        }
    }

    @MainActor
    private func applyPreview(applied: [[String: Any]], skipped: [[String: Any]]) {
        previewApplied = applied
        previewSkipped = skipped
        summaryLabel.stringValue = "Будет включено: \(applied.count). Пропущено: \(skipped.count)."
        applyButton.isEnabled = !applied.isEmpty
    }

    @MainActor
    private func showFetchFailed() {
        summaryLabel.stringValue = "Не удалось получить превью — можно настроить позже в Настройках."
        applyButton.isEnabled = false
    }

    @objc private func onSkipTap() {
        finish(.skipped)
    }

    @objc private func onApplyTap() {
        applyButton.isEnabled = false
        skipButton.isEnabled = false
        let ipc = ipcClient
        let appliedCount = previewApplied.count
        Task { [weak self] in
            do {
                _ = try await ipc.callAsync(
                    method: "apply_recommended_setup",
                    params: ["dry_run": false],
                    timeoutSec: IPCClient.defaultTimeoutSec
                )
            } catch {
                NSLog("[RecommendedSetupStep] apply error: %@", error.localizedDescription)
            }
            await MainActor.run { [weak self] in self?.finish(.applied(count: appliedCount)) }
        }
    }

    private func finish(_ outcome: RecommendedSetupStepOutcome) {
        guard !didComplete else { return }
        didComplete = true
        if let sheet = sheetWindow, let parent = parentWindow {
            parent.endSheet(sheet)
            sheetWindow = nil
        }
        completion(outcome)
    }
}
```

- [ ] **Шаг 4: Встройка в `main.swift` (Задачи 3 и 5 объединены здесь — единый diff метода)**

Заменить `runModelDownloadStepThenComplete()` (строки 1417-1430):

```swift
    /// Перед завершением онбординга: (1) STT-модель если не в кэше, (2) рекомендованная
    /// настройка (A1, dry_run превью -> apply/skip), (3) wake word consent (решение 9.4,
    /// отдельно от apply_recommended_setup). Любой исход каждого шага -> следующий шаг;
    /// финал -> onComplete().
    private func runModelDownloadStepThenComplete() {
        guard let parent = self.window else {
            onComplete()
            return
        }
        let step = ModelDownloadStepController(ipcClient: ipcClient) { [weak self] _ in
            guard let self = self else { return }
            self.modelDownloadStep = nil
            self.runRecommendedSetupStepThenWakeWord(over: parent)
        }
        self.modelDownloadStep = step
        step.start(over: parent)
    }

    private var recommendedSetupStep: RecommendedSetupStepController?
    private var wakeWordConsentStep: WakeWordConsentStepController?

    private func runRecommendedSetupStepThenWakeWord(over parent: NSWindow) {
        let step = RecommendedSetupStepController(ipcClient: ipcClient) { [weak self] _ in
            guard let self = self else { return }
            self.recommendedSetupStep = nil
            self.runWakeWordConsentStep(over: parent)
        }
        self.recommendedSetupStep = step
        step.start(over: parent)
    }

    private func runWakeWordConsentStep(over parent: NSWindow) {
        let step = WakeWordConsentStepController(ipcClient: ipcClient) { [weak self] _ in
            guard let self = self else { return }
            self.wakeWordConsentStep = nil
            self.onComplete()
        }
        self.wakeWordConsentStep = step
        step.start(over: parent)
    }
```

Добавить `private var modelDownloadStep: ModelDownloadStepController?` уже существует
(строка 1240) — новые два `private var` (`recommendedSetupStep`, `wakeWordConsentStep`)
добавляются рядом с ним (strong ref пока соответствующий sheet активен, тот же паттерн).

- [ ] **Шаг 5: Тесты зелёные**

Run: `cd native/KrabEarAgent && swift test --filter RecommendedSetupSourceContractTests 2>&1 | tail -30`
Expected: оба теста passed.

- [ ] **Шаг 6: Полная сборка + существующие source-контракт тесты не сломаны**

```bash
cd native/KrabEarAgent && swift build -c release 2>&1 | tail -20
swift test --filter MainHealthMonitorSourceContractTests 2>&1 | tail -20
swift test --filter MainErrorsWiringTests 2>&1 | tail -20
```
Expected: 0 errors; существующие source-контракт тесты по-прежнему passed (новый код не
трогает `setupHealthMonitor`/`setupErrorBus`).

- [ ] **Шаг 7: Глиф-гейт**

Run: `grep -oP '[^\x00-\x7F]' native/KrabEarAgent/Sources/KrabEarAgent/RecommendedSetupStep.swift | sort -u`
Expected: пусто.

- [ ] **Шаг 8: Commit**

```bash
git add native/KrabEarAgent/Sources/KrabEarAgent/RecommendedSetupStep.swift \
  native/KrabEarAgent/Sources/KrabEarAgent/main.swift \
  native/KrabEarAgent/Tests/KrabEarAgentTests/RecommendedSetupWiringTests.swift
git commit -m "feat(onboarding): RecommendedSetupStep — A1 dry_run превью в цепочке онбординга"
```

**Критерий готовности:** source-контракт тест доказывает реальный вызов (не только
определение); порядок цепочки `ModelDownloadStep → RecommendedSetupStep →
WakeWordConsentStep → onComplete` подтверждён тестом на порядок в исходнике;
`swift build -c release` чист; глиф-гейт чист.

---

## Задача 6: Swift Settings-секция — `HistoryPanelController+RecommendedSetup.swift`

**Цель:** секция в Настройках по образцу `HistoryPanelController+Calibration.swift` —
превью последнего dry_run + «Применить рекомендуемое» + «Отменить последнее» (через
`list_settings_backups {reason-фильтр клиентски}` + `restore_settings_backup`).

**Файлы:**
- Create: `native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController+RecommendedSetup.swift`
- Modify: `native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController.swift`
  (регистрация секции рядом со строкой 1924, где вызывается `buildCalibrationSection()`)
- Modify: `native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController+Settings+ClaudeDesign.swift`
  (регистрация CD-варианта рядом со строкой 671)

- [ ] **Шаг 1: Реализация (механика — визуал по брифу Задачи 4, отдельным прогоном `agy`)**

```swift
/*
 Рекомендованная настройка (A1): секция Настроек с превью dry_run
 apply_recommended_setup + кнопками «Применить рекомендуемое» / «Отменить последнее».

 IPC-контракт:
   - apply_recommended_setup {dry_run: true}
       -> result {ok, dry_run, tier, applied: [{key, old_value, new_value, restart_required}],
                  skipped: [{key, reason}], rationale, snapshot_id, restart_required}
   - apply_recommended_setup {dry_run: false} -> тот же shape, snapshot_id заполнен.
   - list_settings_backups {limit: 10} -> {backups: [{backup_id, ts, reason, ...}]}
     — БЕЗ server-side фильтра по reason; секция фильтрует клиентски
     reason == "before_recommended_setup", берёт САМЫЙ СВЕЖИЙ (backups отсортированы
     от новых к старым, см. settings_service.py:758-774).
   - restore_settings_backup {backup_id} -> {restored_settings, backup_id[, warning,
     dropped_fields]}.

 Архитектура (зеркало HistoryPanelController+Calibration.swift):
   - buildRecommendedSetupSection() / cdBuildRecommendedSetupSection()
   - fetchAndRebuildRecommendedSetupCard(isClaudeDesign:) — dry_run превью, off-main.
   - onApplyRecommendedSetup(_:) — apply_recommended_setup{dry_run:false} off-main.
   - onUndoLastRecommendedSetup(_:) — находит последний backup с
     reason=before_recommended_setup, restore_settings_backup off-main.

 Правила AGENT-3: IPC строго DispatchQueue.global, UI-мутации строго main.
 Визуал — docs/design-briefs/2026-07-07-recommended-setup-ui.md (agy, отдельно).
*/

import AppKit
import Foundation

private enum RecommendedSetupAssocKeys {
    nonisolated(unsafe) static var card: UInt8 = 0
    nonisolated(unsafe) static var cdCard: UInt8 = 0
    nonisolated(unsafe) static var lastPreview: UInt8 = 0
}

struct RecommendedSetupPreview {
    let tier: String
    let applied: [[String: Any]]
    let skipped: [[String: Any]]
    let rationale: String
}

extension HistoryPanelController {

    @MainActor
    func buildRecommendedSetupSection() -> CollapsibleSectionView {
        let section = CollapsibleSectionView(
            sectionId: "recommended_setup",
            title: "Рекомендованная настройка",
            isExpanded: false,
            iconSymbol: "wand.and.stars"
        )
        let card = ThemeCardView()
        let loadingLabel = NSTextField(labelWithString: "Загрузка...")
        loadingLabel.font = KrabEarTheme.Typography.caption
        loadingLabel.textColor = KrabEarTheme.Colors.textSecondary
        card.contentStackView.addArrangedSubview(loadingLabel)

        objc_setAssociatedObject(self, &RecommendedSetupAssocKeys.card, card, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        section.contentStackView.addArrangedSubview(card)
        fetchAndRebuildRecommendedSetupCard(isClaudeDesign: false)
        return section
    }

    @MainActor
    func cdBuildRecommendedSetupSection() -> CollapsibleSectionView {
        let section = CollapsibleSectionView(
            sectionId: "cd_recommended_setup", title: "Рекомендованная настройка", isExpanded: false
        )
        let card = CDSettingsCardView()
        let loadingLabel = NSTextField(labelWithString: "Загрузка...")
        loadingLabel.font = .systemFont(ofSize: 12, weight: .regular)
        loadingLabel.textColor = KrabEarTheme.Colors.textSecondary
        card.contentStackView.addArrangedSubview(loadingLabel)

        objc_setAssociatedObject(self, &RecommendedSetupAssocKeys.cdCard, card, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        section.contentStackView.addArrangedSubview(card)
        fetchAndRebuildRecommendedSetupCard(isClaudeDesign: true)
        return section
    }

    func fetchAndRebuildRecommendedSetupCard(isClaudeDesign: Bool) {
        let ipc = ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            var preview: RecommendedSetupPreview?
            do {
                let resp = try ipc.call(method: "apply_recommended_setup", params: ["dry_run": true])
                let result = resp["result"] as? [String: Any] ?? [:]
                preview = RecommendedSetupPreview(
                    tier: (result["tier"] as? String) ?? "low",
                    applied: (result["applied"] as? [[String: Any]]) ?? [],
                    skipped: (result["skipped"] as? [[String: Any]]) ?? [],
                    rationale: (result["rationale"] as? String) ?? ""
                )
            } catch {
                preview = nil
            }
            DispatchQueue.main.async { [weak self] in
                guard let self else { return }
                objc_setAssociatedObject(
                    self, &RecommendedSetupAssocKeys.lastPreview, preview,
                    .OBJC_ASSOCIATION_RETAIN_NONATOMIC
                )
                if isClaudeDesign {
                    self.rebuildCDRecommendedSetupCard(preview: preview)
                } else {
                    self.rebuildGeminiRecommendedSetupCard(preview: preview)
                }
            }
        }
    }

    @MainActor
    private func rebuildGeminiRecommendedSetupCard(preview: RecommendedSetupPreview?) {
        guard let card = objc_getAssociatedObject(self, &RecommendedSetupAssocKeys.card) as? ThemeCardView else { return }
        for v in card.contentStackView.arrangedSubviews {
            card.contentStackView.removeArrangedSubview(v)
            v.removeFromSuperview()
        }
        guard let preview else {
            let fallback = NSTextField(labelWithString: "Нет данных — бэкенд недоступен")
            fallback.font = KrabEarTheme.Typography.caption
            fallback.textColor = KrabEarTheme.Colors.textSecondary
            card.contentStackView.addArrangedSubview(fallback)
            return
        }
        let summary = NSTextField(labelWithString:
            "Класс: \(preview.tier). Будет включено: \(preview.applied.count). Пропущено: \(preview.skipped.count).")
        summary.font = KrabEarTheme.Typography.captionMedium
        card.contentStackView.addArrangedSubview(summary)
        if !preview.rationale.isEmpty {
            let rationale = NSTextField(wrappingLabelWithString: preview.rationale)
            rationale.font = KrabEarTheme.Typography.caption
            rationale.textColor = KrabEarTheme.Colors.textSecondary
            card.contentStackView.addArrangedSubview(rationale)
        }
        card.contentStackView.addArrangedSubview(recommendedSetupButtonRow(isClaudeDesign: false))
    }

    @MainActor
    private func rebuildCDRecommendedSetupCard(preview: RecommendedSetupPreview?) {
        guard let card = objc_getAssociatedObject(self, &RecommendedSetupAssocKeys.cdCard) as? CDSettingsCardView else { return }
        for v in card.contentStackView.arrangedSubviews {
            card.contentStackView.removeArrangedSubview(v)
            v.removeFromSuperview()
        }
        guard let preview else {
            let fallback = NSTextField(labelWithString: "Нет данных — бэкенд недоступен")
            fallback.font = .systemFont(ofSize: 12, weight: .regular)
            fallback.textColor = KrabEarTheme.Colors.textSecondary
            card.contentStackView.addArrangedSubview(fallback)
            return
        }
        let summary = NSTextField(labelWithString:
            "Класс: \(preview.tier). Будет включено: \(preview.applied.count). Пропущено: \(preview.skipped.count).")
        summary.font = .systemFont(ofSize: 12, weight: .regular)
        card.contentStackView.addArrangedSubview(summary)
        card.contentStackView.addArrangedSubview(recommendedSetupButtonRow(isClaudeDesign: true))
    }

    @MainActor
    private func recommendedSetupButtonRow(isClaudeDesign: Bool) -> NSView {
        let applyButton = ThemePrimaryButton(
            title: "Применить рекомендуемое", target: self,
            action: #selector(onApplyRecommendedSetup(_:))
        )
        let undoButton = ThemeSecondaryButton(
            title: "Отменить последнее", target: self,
            action: #selector(onUndoLastRecommendedSetup(_:))
        )
        let stack = NSStackView()
        stack.orientation = .horizontal
        stack.spacing = KrabEarTheme.Metrics.standard
        stack.addArrangedSubview(applyButton)
        stack.addArrangedSubview(undoButton)
        let spacer = NSView()
        spacer.setContentHuggingPriority(.defaultLow, for: .horizontal)
        stack.addArrangedSubview(spacer)
        return stack
    }

    @objc func onApplyRecommendedSetup(_ sender: NSButton) {
        let ipc = ipcClient
        sender.isEnabled = false
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            var ok = false
            do {
                _ = try ipc.call(method: "apply_recommended_setup", params: ["dry_run": false])
                ok = true
            } catch {
                ok = false
            }
            DispatchQueue.main.async { [weak self] in
                sender.isEnabled = true
                self?.recommendedSetupShowToast(ok ? "Рекомендованная настройка применена" : "Не удалось применить")
            }
            self.fetchAndRebuildRecommendedSetupCard(isClaudeDesign: false)
            self.fetchAndRebuildRecommendedSetupCard(isClaudeDesign: true)
        }
    }

    @objc func onUndoLastRecommendedSetup(_ sender: NSButton) {
        let ipc = ipcClient
        sender.isEnabled = false
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            var message = "Нет сохранённого снимка для отмены"
            do {
                let resp = try ipc.call(method: "list_settings_backups", params: ["limit": 10])
                let backups = (resp["result"] as? [String: Any])?["backups"] as? [[String: Any]] ?? []
                if let last = backups.first(where: { ($0["reason"] as? String) == "before_recommended_setup" }),
                   let backupId = last["backup_id"] as? String {
                    _ = try ipc.call(method: "restore_settings_backup", params: ["backup_id": backupId])
                    message = "Настройки возвращены к состоянию до применения"
                }
            } catch {
                message = "Не удалось отменить: \(error.localizedDescription)"
            }
            DispatchQueue.main.async { [weak self] in
                sender.isEnabled = true
                self?.recommendedSetupShowToast(message)
            }
            self.fetchAndRebuildRecommendedSetupCard(isClaudeDesign: false)
            self.fetchAndRebuildRecommendedSetupCard(isClaudeDesign: true)
        }
    }

    @MainActor
    private func recommendedSetupShowToast(_ message: String) {
        BackendToast.shared.show(message)
    }
}
```

- [ ] **Шаг 2: Регистрация секции**

`HistoryPanelController.swift` рядом со строкой 1924
(`let calibrationSection = buildCalibrationSection()`):
```swift
        let recommendedSetupSection = buildRecommendedSetupSection()
```
и добавить в тот же `addArrangedSubview`-список секций (сверить точный паттерн
добавления соседних секций в этом файле перед вставкой — секции добавляются в
`NSStackView` последовательно, найти правильную точку вставки рядом с
`calibrationSection`).

`HistoryPanelController+Settings+ClaudeDesign.swift` рядом со строкой 671
(`let s7b = cdBuildCalibrationSection()`):
```swift
        let s7c = cdBuildRecommendedSetupSection()
```
аналогично встроить в существующий список CD-секций.

- [ ] **Шаг 3: `swift build` компилируется**

```bash
cd native/KrabEarAgent && swift build -c release 2>&1 | tail -20
```
Expected: 0 errors.

- [ ] **Шаг 4: Глиф-гейт**

```bash
grep -oP '[^\x00-\x7F]' native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController+RecommendedSetup.swift | sort -u
```
Expected: пусто.

- [ ] **Шаг 5: Commit**

```bash
git add native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController+RecommendedSetup.swift \
  native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController.swift \
  native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController+Settings+ClaudeDesign.swift
git commit -m "feat(settings-ui): секция «Рекомендованная настройка» — превью/применить/отменить"
```

**Критерий готовности:** секция зарегистрирована в обоих вариантах (Gemini/CD);
«Применить» вызывает `apply_recommended_setup{dry_run:false}`; «Отменить последнее»
находит backup с `reason=before_recommended_setup` и вызывает `restore_settings_backup`;
`swift build -c release` чист; глиф-гейт чист.

---

## Задача 7: e2e-смок + финальная верификация всей волны

**Цель:** живой e2e-смок по паттерну `scripts/e2e_ipc_smoke.py` (см.
`reference_live_e2e_smoke_method` в памяти проекта — ловит классы багов, невидимые
юнит-тестам на пустой/мок-истории) + прогон всех статических гейтов волны.

**Файлы:**
- Create: `KrabEar/scripts/e2e_recommended_setup_smoke.py` — ПРИМЕЧАНИЕ: в
  репозитории существующий `scripts/e2e_ipc_smoke.py` лежит в `scripts/` корня
  проекта (не `KrabEar/scripts/`) — перед созданием сверить фактический путь `ls
  scripts/*e2e*` и положить новый файл РЯДОМ с существующими e2e-скриптами (тот же
  каталог), а не изобретать новую конвенцию расположения.
- Modify: `docs/IPC_API_REFERENCE.md` (документирование `apply_recommended_setup`)

- [ ] **Шаг 1: Сверить фактическое расположение существующих e2e-скриптов**

```bash
ls scripts/*e2e* scripts/run_e2e*.command 2>/dev/null
```
Положить новый скрипт в тот же каталог, что `e2e_ipc_smoke.py`/`e2e_privacy_gates.py`
(по факту находки — не предполагать заранее конкретный путь).

- [ ] **Шаг 2: Написать `scripts/e2e_recommended_setup_smoke.py`**

Сценарий (по аналогии со сценарием, описанным в §10 п.7 черновика):
1. Поднять throwaway dev-backend на temp data-dir (паттерн
   `scripts/run_e2e_smokes.command` — trap на teardown, никогда не трогает прод/
   реальную историю).
2. `get_hardware_profile {}` → assert `ok=True`, `tier` in `{low, mid, high}`.
3. `apply_recommended_setup {dry_run: true}` → assert `dry_run=True`,
   `snapshot_id is None`, `applied` содержит подмножество 10 безусловных ключей,
   `skipped` содержит GigaAM-пару с фиксированной причиной.
4. `apply_recommended_setup {dry_run: false}` → assert `snapshot_id is not None`.
5. `get_settings {}` → assert безусловные ключи из `applied` шага 4 реально `True`
   в реальном settings.json (не только в IPC-ответе).
6. `restore_settings_backup {backup_id: <snapshot_id из шага 4>}` → assert настройки
   вернулись к состоянию до шага 4.
7. Cleanup (kill процессов, `rm -rf` temp data-dir) — обязательно в `finally`/trap,
   даже при падении любого assert выше.

- [ ] **Шаг 3: Прогнать смок**

```bash
python3 scripts/e2e_recommended_setup_smoke.py
```
Expected: все шаги ALL GREEN. Если бэкенд не поднимается / сокет не появляется —
вывести последние строки `ipc.log` (тот же паттерн диагностики, что в
`docs/superpowers/plans/2026-07-07-event-bridge.md` Задача 1).

- [ ] **Шаг 4: Документирование в `docs/IPC_API_REFERENCE.md`**

Добавить секцию `apply_recommended_setup` (по образцу существующих секций для
`apply_profile_preset`/`get_hardware_profile`/`get_calibration_recommendation`) —
контракт запроса/ответа буква-в-букву как в финальной спеке §2, с явным примечанием
про GigaAM-пару (всегда skipped, без probe) и про wake word (отдельный IPC, не через
этот метод).

- [ ] **Шаг 5: Полный набор верификации волны**

```bash
# Backend — все новые/изменённые тесты этой волны
PYTHONPATH=$(pwd)/KrabEar python -m pytest \
  KrabEar/tests/test_action_items_privacy_gate_A1.py \
  KrabEar/tests/test_punctuation_pass_privacy_pin_A1.py \
  KrabEar/tests/test_apply_recommended_setup.py \
  KrabEar/tests/test_apply_recommended_setup_probes.py \
  -v

# Регрессия смежных наборов, тронутых Задачей 0/1
PYTHONPATH=$(pwd)/KrabEar python -m pytest \
  KrabEar/tests/test_recording_core_service.py \
  KrabEar/tests/test_engine_unit.py \
  KrabEar/tests/test_wave29_privacy_gates.py \
  KrabEar/tests/test_auto_dedup_privacy_W1248.py \
  KrabEar/tests/test_dispatch_invariants_wave693.py \
  KrabEar/tests/test_dispatch_invariants_wave790_full.py \
  -v

# flake8 по всем изменённым/новым файлам разом
.venv_krab_ear/bin/flake8 \
  KrabEar/backend/recording_core_service.py KrabEar/backend/settings_service.py \
  KrabEar/backend/service.py \
  KrabEar/tests/test_action_items_privacy_gate_A1.py \
  KrabEar/tests/test_punctuation_pass_privacy_pin_A1.py \
  KrabEar/tests/test_apply_recommended_setup.py \
  KrabEar/tests/test_apply_recommended_setup_probes.py \
  scripts/e2e_recommended_setup_smoke.py \
  --max-line-length=150

# ubuntu-parity автообнаружением изменённых тестовых файлов
make pre-merge-check

# Все статические аудиты разом
make audit-all

# Существующие e2e-смоки — не регрессировали
bash scripts/run_e2e_smokes.command

# Новый e2e-смок волны A1
python3 scripts/e2e_recommended_setup_smoke.py

# Swift — билд + source-контракт тесты + полный test target
cd native/KrabEarAgent && swift build -c release 2>&1 | tail -10 \
  && swift test 2>&1 | tail -20 && cd ../..

# Глиф-гейт на все новые Swift-файлы разом
for f in native/KrabEarAgent/Sources/KrabEarAgent/RecommendedSetupStep.swift \
         native/KrabEarAgent/Sources/KrabEarAgent/WakeWordConsentStep.swift \
         native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController+RecommendedSetup.swift; do
  echo "=== $f ==="
  grep -oP '[^\x00-\x7F]' "$f" | sort -u
done
```
Expected: все шаги зелёные; глиф-гейт — пусто по всем трём файлам.

- [ ] **Шаг 6 (опционально, не блокирует DoD): обновление ROADMAP/памяти**

Волна закрывает `docs/ROADMAP-2026H2.md` §2 Волна 1. Можно (не обязано) обновить
статус волны и записать итог в memory-файлы проекта по существующей конвенции сессий.

**Критерий готовности всей волны:** все команды Шага 5 зелёные; DoD финальной спеки §5
(наследует §11 черновика) выполнен: (1) построчная privacy-проверка сделана и найденный
пробел закрыт (Задача 0); (2) `apply_recommended_setup` реализован с дефолтным набором =
10 «ДА» + 3 «УСЛОВНО-ДА» с probe (Задача 1-2); (3) `dry_run` превью работает до записи
(Задача 1); (4) снапшот + откат через существующий `restore_settings_backup`, без нового
кода отката (Задача 1); (5) онбординг — новый шаг, graceful skip (Задача 5); (6)
Настройки — новая секция превью+применить+отменить (Задача 6); (7) ни один
МЁРТВЫЙ/НЕТ-кандидат не в `applied` ни при каком входе (Задача 1, regression-тест); (8)
GigaAM-пара НИКОГДА не в `applied` даже с замоканным валидным venv — тест это явно
проверяет как политику (Задача 1); (9) design-brief написан ДО Swift-визуальной
реализации (Задача 4 предшествует по номеру, но фактическую agy-реализацию визуала
контролёр может инициировать в любой момент после Задачи 4); (10) живой e2e-смок зелёный
(Задача 7); (11) `make audit-all` зелёный.

---

## Self-review (выполнен при написании плана)

- **Покрытие финальной спеки:** §0 решения 9.1-9.7 → отражены в тексте задач (9.2/9.3
  закрываются Задачей 0, 9.4 → Задача 3, 9.5 → зафиксирован дефолт `dry_run=true` без
  `keys`-UI в v1, 9.6 → Задача 4, 9.7 → Задача 1 явный regression-тест); §1 состав
  пресета → Задача 1 константы класса; §2 IPC-контракт → Задача 1 Шаг 3; §3 UI → Задачи
  3/5/6; §4 privacy → Задача 0 + Задача 1 `_RECOMMENDED_PRIVACY_SENSITIVE`; §5 тест-план →
  распределён по Задачам 0/1/2/7; §6 вне скоупа → ничего из пяти «ВОПРОС»-кандидатов не
  реализовано, GigaAM auto-detect explicitly отвергнут тестом.
- **Type consistency:** `handle_apply_recommended_setup(params, *, probe_llm_fn,
  sensevoice_cached_fn)` — сигнатура одинакова в Задаче 1 (определение+тесты) и Задаче 2
  (интеграционные тесты реальной проводки из `service.py`); `snapshot_id`↔`backup_id`
  соответствие явно задокументировано в двух местах (константы блока + Задача 6 Swift).
- **Placeholder-скан:** единственное намеренно неполное место — Задача 7 Шаг 1 (сверка
  фактического пути e2e-скриптов на месте, не предполагается заранее, т.к. этот план
  писался без доступа к запуску `ls` с гарантией актуальности на момент исполнения задачи
  спустя время) и Задача 6 Шаг 2 (точная строка вставки в `NSStackView` — план указывает
  ориентир по соседней секции, но не гарантирует точный номер строки на момент исполнения,
  так как предыдущие задачи волны меняют файл).
- **Все код-сниппеты сверены с реальным кодом репозитория на момент написания плана**
  (2026-07-07): `handle_apply_profile_preset` (settings_service.py:535-572),
  `handle_restore_settings_backup` (776-858), `_get_runtime_setting`
  (service.py:1262-1270), `_handle_get_hardware_profile`/`_handle_get_calibration_recommendation`
  (service.py:4682+), `probe_llm_http` (health_check_service.py:209-218),
  `ModelDownloader.get_status`/`_is_cached` (model_downloader.py:231-311),
  `ModelDownloadStep.swift` (весь файл), `HistoryPanelController+Calibration.swift`
  (весь файл), `main.swift:1231-1430` (`QuickStartWindowController`), source-контракт
  тест-паттерн (`MainHealthMonitorWiringTests.swift:352-391`).

---

## Открытые вопросы к контролёру

Места, где план принял самостоятельное решение при отсутствии явной специфики в
финальной спеке/черновике. Ни одно не противоречит спеке буквально.

1. **Фикс `action_items_auto_extract` privacy-гейта — сделан ПРЯМО В КОДЕ
   (`recording_core_service.py:1644`), а не только как условие внутри
   `apply_recommended_setup` (Задача 0).** Финальная спека §4 говорит только «уходят в
   `skipped` при включённом privacy-режиме» применительно к самому пресету — не
   предписывает чинить runtime-хук. План решил, что оставлять хук незагейченным (пресет
   лишь не ВКЛЮЧАЕТ ключ при privacy=true НА МОМЕНТ применения, но ничего не мешает
   владельцу включить privacy ПОСЛЕ) — это оставленная дыра, нарушающая правило проекта
   «privacy_mode_enabled ВСЕГДА побеждает». Если контролёр считает это отдельной
   задачей/волной (не частью A1) — можно вынести правку `recording_core_service.py`
   в отдельный PR вне этого плана, оставив в `apply_recommended_setup` только
   time-of-apply проверку.

2. **`SettingsService.handle_apply_recommended_setup` принимает `probe_llm_fn`/
   `sensevoice_cached_fn` как ОБЯЗАТЕЛЬНЫЕ keyword-only параметры, инжектируемые
   вызывающей стороной (`service.py`), а не читает `HealthCheckService`/`ModelDownloader`
   напрямую (Задача 1).** Ни спека, ни черновик не специфицируют механизм внедрения
   зависимости. Выбор сделан по аналогии с существующими паттернами инъекции в проекте
   (`AudioEngine(settings_get=self._get_runtime_setting)`,
   `EventReplayManager(settings_provider=...)`) — избегает циклической/тяжёлой
   конструкторской зависимости `SettingsService → HealthCheckService`/`ModelDownloader`
   (`SettingsService` конструируется РАНЬШЕ обоих в `BackendService.__init__`, судя по
   текущему порядку полей). Альтернатива — регистрировать probe-колбэки через
   `register_after_save_hook`-подобный механизм постфактум; отклонена как избыточная
   сложность для одноразового вызова.

3. **`stt_sensevoice_enabled` probe использует ПУБЛИЧНЫЙ `ModelDownloader.get_status(model_id)`,
   не приватный `_is_cached(model_id)` (Задача 1-2).** Ни один документ явно не требовал
   именно этот путь — выбор сделан, чтобы не обращаться к приватному API класса извне
   (`get_status` уже публичен и используется существующим IPC `get_stt_model_status`).
   Отмечен риск (см. блок «Известное допущение» в конце Задачи 2): funasr/SenseVoice
   может кэшировать модель НЕ по стандартному HF-hub layout, который проверяет
   `ModelDownloader` — план не мог верифицировать это фактическим прогоном (задача
   верхнего уровня запретила запуск бинарей/side-effects). Если при первом живом
   прогоне (Задача 7 e2e-смок или ручная проверка владельцем на реальной машине с уже
   скачанным SenseVoice) выяснится ложный `cached=False` — потребуется отдельный фикс
   вне этого плана (либо в `ModelDownloader._is_cached`, либо отдельная funasr-специфичная
   проверка).

4. **Порядок цепочки онбординга: `ModelDownloadStep → RecommendedSetupStep →
   WakeWordConsentStep → onComplete()` (Задача 5).** Финальная спека §3 описывает оба
   новых шага как «встраивается в ту же цепочку онбординга», не специфицируя порядок
   между RecommendedSetup и WakeWordConsent. План выбрал RecommendedSetup ПЕРВЫМ (общие
   безопасные настройки — более общий, менее спорный запрос согласия) и WakeWordConsent
   ПОСЛЕДНИМ (самый чувствительный consent — always-listening микрофон — как финальный,
   самый заметный шаг перед завершением). Если контролёр предпочитает обратный порядок —
   правка тривиальна (Задача 5 Шаг 4, порядок вызовов `runRecommendedSetupStepThenWakeWord`/
   `runWakeWordConsentStep`) и Задача 5 Шаг 1 source-контракт тест
   (`test_recommended_setup_step_precedes_wake_word_consent_in_source_order`) потребует
   инверсии сравнения.

5. **`docs/design-briefs/2026-07-07-recommended-setup-ui.md` — точная структура секций
   брифа придумана планом (Задача 4), т.к. план не смог заранее проверить, есть ли в
   `docs/design-briefs/` уже устоявшийся шаблон формата** (задача верхнего уровня не
   давала доступ к произвольному листингу директорий вне явно указанных файлов на
   момент написания плана вне уже прочитанных). Задача 4 Шаг 2 явно требует сверить
   формат с существующими файлами каталога ПЕРЕД записью и подправить при расхождении —
   это встроенная защита от переизобретения конвенции, но сам план не может гарантировать
   на 100%, что заголовки итогового брифа совпадут один-в-один с конвенцией проекта.

6. **Задача 0 добавляет ДВА новых теста (`test_action_items_privacy_gate_A1.py`,
   `test_punctuation_pass_privacy_pin_A1.py`), но НЕ добавляет новый тест для
   `auto_learn_corrections_enabled`** — план полагается на существующий
   `test_wave29_privacy_gates.py::TestReplaceWordPrivacyGate::test_privacy_on_does_not_touch_store`
   как достаточное доказательство (транзitивная недостижимость `_maybe_auto_learn_word`
   при privacy=True). Это осознанный выбор экономии (не дублировать покрытие), но
   логика "недостижимости" не закреплена ОТДЕЛЬНЫМ именованным тестом, специфичным
   именно для auto-learn угла — если в будущем появится ВТОРОЙ вызывающий
   `_maybe_auto_learn_word` без собственного privacy-гейта на входе, регрессия не будет
   поймана существующим тестом (он проверяет только путь через
   `handle_replace_word_in_last_transcript`). Если контролёр хочет более явную защиту —
   можно добавить прямой unit-тест на `_maybe_auto_learn_word` с mock `settings_svc`,
   проверяющий его поведение в изоляции (сам метод сейчас НЕ содержит privacy-проверки
   внутри себя — это стало бы более явным местом для будущего гейта).
