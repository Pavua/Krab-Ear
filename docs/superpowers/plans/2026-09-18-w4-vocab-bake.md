# F1: запечь лексику W4 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** наблюдавшиеся искажения W4 исправляются автоматически: пары «вариант → канон» в фонетическом словаре (оба движка), hotwords для Whisper, дыра голого `dimatorzok` закрыта фильтром, REST-движок тоже корректирует.

**Architecture:** три независимых механизма, один принцип — детерминированные замены после STT (работают на обоих движках) вместо fuzzy-подсказок (только Whisper):
1. `phonetic_vocab.json` (пост-коррекция в `engine.py:1712`, после cleanup/snippets, до number/datetime) — все пары «вариант → канон»;
2. `initial_prompt` Whisper (`build_initial_prompt`, `merge_language_hotwords`) — 2 seed-слова, которых нет в искажениях, но движок должен emit'ить (`оверлей`, `openclaw`);
3. `_HALLUCINATION_PATTERNS` (`core/utils.py:383`) — tail-правило на голый токен.
Плюс включение флага (дефолт False → True) и проводка REST-движка (провайдер + settings_get), чтобы `/v1/stt/transcribe` корректировал как диктовка (вопрос VG из брифа 02.09).

**Tech Stack:** Python `re`/`unittest`. Новых зависимостей нет.

**База:** `origin/codex/krab-ear-v2`. Worktree: `.worktrees/w4-vocab-bake`, ветка `feat/w4-vocab-bake`.

**Баны:** список из [`EXECUTOR_PLAYBOOK.md`](../../EXECUTOR_PLAYBOOK.md) §1 целиком. Дополнительно: **не трогать GigaAM-адаптеры, чанкер, VAD, роутер**; не менять семантику `correct()` (только данные + флаг); не включать `auto_learn_corrections`; сырые тексты истории/транскриптов в отчёты/коммиты НЕ тащить (шаг 0 — только агрегаты).

---

## Проверенные факты (координатор, 17.09, file:line)

- Пост-коррекция: `engine.py:1710-1720` (`_phonetic_provider` + `PhoneticVocabulary(settings_get, entries_provider).correct(text)`); общая точка обоих движков (`:1608 raw → :1675 cleanup → :1695 snippets → :1710 phonetic → :1728 number/datetime`).
- Флаг: `PhoneticVocabulary._enabled()` читает `phonetic_vocab_enabled` (дефолт False, `config.py:1122`); алгоритм longest-first + `\b` + `re.escape` + IGNORECASE (`core/phonetic_vocabulary.py:56-100`).
- Файл: `{data_dir}/phonetic_vocab.json`, формат `{"entries":[{"canonical","variants"}],"updated_at"}`, лимиты 200 записей/200 символов (`backend/phonetic_vocab_service.py:9-28`); IPC add/list/remove (`:111,185,194`); `clear_all` для purge (`:96`).
- Проводка диктовки: `service.py:476` (сервис) + `:538` (`engine._phonetic_provider = ...get_entries`).
- REST-движок: `rest_server.py:875` `AudioEngine(skip_gigaam_warmup=True)` + `Transcriber(engine=engine)` БЕЗ settings_get и провайдера → коррекция пропускается. Паттерн доступа к настройкам в REST: `settings_get=lambda k, d=None: deps.store.load_settings().get(k, d)` (`rest_server.py:2647`); модульный `store = StateStore(settings.DATA_DIR)` рядом с созданием движка.
- Hotwords: `stt_hotwords` + `stt_hotwords_ru/es/en` (`config.py:1023-1027`, лимит 100 FIFO в `stt_management_service.py:27`); seed-категории `backend/default_hotwords.py:23+`; сборка `transcript_context.py:163,120,284-287` (лимиты: 250 terms, 560 chars, BPE 224); Whisper `initial_prompt` (`engine.py:1379,3080,3206`); GigaAM prompts НЕ принимает (`stt_gigaam.py` — 0 вхождений prompt).
- Фильтр: `_HALLUCINATION_PATTERNS` (`utils.py:383-397`, все tail-якоря `$`, поиск по lowered); `_strip_hallucinations` (`:644-655`): `start<=0 → ""`, иначе рез хвоста. Дыра: паттерн требует `субтитры <глагол> <ник>$`, голый токен проходит (`test_hallucination_subtitles_verb_W1894.py:51` фиксирует out-of-scope).
- Живые данные: `history.ndjson` (23k) + архив `transcripts.sqlite` (192k сегментов); агрегатный майнинг разрешён владельцем 16.09.

## Утверждённые пары (владелец 17.09; полный список — в карточке, не в чате)

Phonetic (variant → canonical): `openclow→openclaw`; `травмадол,траммадол,тромадол,рамадол→трамадол`; `пегабалин,пегбалин,прегалин,пригаболин→прегабалин`; `парацетамон,парацемолом→парацетамол`; `oxicodon,oxycodona,oxipodona,oxigodona→oxicodona`; `pregabalin,pregabalino,pregabalín,pegabarina→pregabalina`; `fartestamol→paracetamol`; `ramadol,tromadol,dramadol→tramadol`; `оxicodona (кир.О+лат)→oxicodona`; `пregabalina (кир.П+лат)→pregabalina`; `висперед→whisper`; `лрд,lrd→p0lrd`.
Seed-hotwords: `оверлей`, `openclaw` (категория по соседству в `default_hotwords.py`, проверить слияние seed).
Фильтр: голый tail-токен `dimatorzok/dima torzok/диматорзок/дима торзок` (+пунктуация) → рез хвоста.
🔴 НЕ запекать: `maby` (ждёт примеров R2 — легитимное EN "maybe"); `китопрофен/кидопрофен` (это кетопрофен — другой препарат, ловушка из брифа VG 02.09); словоформы-падежи (`оксикодону`, `трамадолом` — не ошибки).

---

### Task 0: Ре-майнинг топ-листа (обязательно первым, только агрегаты)

- [ ] **Step 1:** найти читателей живых данных (`rg -ln 'transcripts.sqlite|history.ndjson' KrabEar/ --glob '!tests'`) и написать скрипт в `/tmp` (НЕ коммитить): топ токенов-кандидатов из обоих корпусов + сверка покрытия утверждённых пар + новые кандидаты (частота ≥20). Сырые тексты в stdout запрещены — только `токен:частота` и `вариант→кандидат?`.
- [ ] **Step 2:** прогнать локально venv'ом репо. Ожидаемо: все утверждённые пары подтверждаются частотой; `dimatorzok` в топе.
- [ ] **Step 3:** новые кандидаты (если есть) — СПИСКОМ в отчёт координатору, НЕ запекать самовольно.

### Task 1: Тест запекания (RED)

**Files:**
- Create: `KrabEar/tests/test_w4_vocab_bake_2026_09.py`

- [ ] **Step 1: Написать тест** (скелет; пары — из списка выше, дословно):

```python
"""F1: лексика W4 запечена (phonetic-пары, hotwords-seed, фильтр dimatorzok)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.phonetic_vocabulary import PhoneticVocabulary

# вариант -> канон (утверждено владельцем 17.09; maby и кетопрофеновая
# ловушка исключены осознанно — см. карточку).
PAIRS = {
    "openclow": "openclaw",
    "травмадол": "трамадол",
    "траммадол": "трамадол",
    "тромадол": "трамадол",
    "рамадол": "трамадол",
    "пегабалин": "прегабалин",
    "пегбалин": "прегабалин",
    "прегалин": "прегабалин",
    "пригаболин": "прегабалин",
    "парацетамон": "парацетамол",
    "парацемолом": "парацетамол",
    "oxicodon": "oxicodona",
    "oxycodona": "oxicodona",
    "oxipodona": "oxicodona",
    "oxigodona": "oxicodona",
    "pregabalin": "pregabalina",
    "pregabalino": "pregabalina",
    "pregabalín": "pregabalina",
    "pegabarina": "pregabalina",
    "fartestamol": "paracetamol",
    "ramadol": "tramadol",
    "tromadol": "tramadol",
    "dramadol": "tramadol",
    "висперед": "whisper",
    "лрд": "p0lrd",
    "lrd": "p0lrd",
    "оxicodona": "oxicodona",
    "пregabalina": "pregabalina",
}

CLEAN_SENTENCES = [
    "Принимаю трамадол утром и прегабалин вечером",
    "Take tramadol and pregabalina daily",
    "Открой оверлей и проверь openclaw",
]


def _vocab(entries):
    return PhoneticVocabulary(
        settings_get=lambda k, d: True if k == "phonetic_vocab_enabled" else d,
        entries_provider=lambda: entries,
    )


class W4VocabBakeTests(unittest.TestCase):
    def test_live_entries_cover_approved_pairs(self) -> None:
        """Запечённые entries покрывают все утверждённые пары (через live-сервис)."""
        from backend.phonetic_vocab_service import PhoneticVocabService
        import tempfile
        from pathlib import Path as _Path
        with tempfile.TemporaryDirectory() as d:
            svc = PhoneticVocabService(data_dir=_Path(d))
            # seed: записать entries кодом из Task 2 (дифф seed-файла), прочитать назад
            seeded = {(e["canonical"], v) for e in svc.get_entries() for v in e.get("variants", [])}
        for variant, canon in PAIRS.items():
            self.assertIn((canon, variant), seeded, variant)

    def test_pairs_correct_text(self) -> None:
        from backend.phonetic_vocab_service import PhoneticVocabService
        import tempfile
        from pathlib import Path as _Path
        with tempfile.TemporaryDirectory() as d:
            svc = PhoneticVocabService(data_dir=_Path(d))
            vocab = _vocab(svc.get_entries())
        for variant, canon in PAIRS.items():
            with self.subTest(variant=variant):
                self.assertEqual(vocab.correct(f"сказал {variant} вчера"), f"сказал {canon} вчера")

    def test_clean_text_untouched_and_idempotent(self) -> None:
        from backend.phonetic_vocab_service import PhoneticVocabService
        import tempfile
        from pathlib import Path as _Path
        with tempfile.TemporaryDirectory() as d:
            svc = PhoneticVocabService(data_dir=_Path(d))
            vocab = _vocab(svc.get_entries())
        for s in CLEAN_SENTENCES:
            with self.subTest(s=s):
                once = vocab.correct(s)
                self.assertEqual(once, s)
                self.assertEqual(vocab.correct(once), once)

    def test_disabled_is_passthrough(self) -> None:
        vocab = PhoneticVocabulary(
            settings_get=lambda k, d: False,
            entries_provider=lambda: [{"canonical": "трамадол", "variants": ["травмадол"]}],
        )
        self.assertEqual(vocab.correct("пил травмадол"), "пил травмадол")

    def test_bare_dimatorzok_tail_is_stripped(self) -> None:
        from core.utils import TextUtils
        self.assertEqual(TextUtils._strip_hallucinations("текст диктовки диматорзок"), "текст диктовки")
        self.assertEqual(TextUtils._strip_hallucinations("Диматорзок"), "")
        self.assertEqual(TextUtils._strip_hallucinations("торговля идёт бойко"), "торговля идёт бойко")

    def test_seed_hotwords_contain_owner_terms(self) -> None:
        # seed_hotwords(settings_svc) пишет в settings и возвращает int —
        # здесь проверяем дефолтный список (плоский), из которого seed берёт слова.
        from backend.default_hotwords import get_default_hotwords
        flat = get_default_hotwords()
        for w in ("оверлей", "openclaw"):
            self.assertIn(w, flat)

    def test_rest_engine_wired_via_ast(self) -> None:
        """REST-движок получает провайдер и настройки (AST, без импорта rest_server —
        модуль тяжёлый: engine и store конструируются на import)."""
        import ast as _ast
        src = (PROJECT_ROOT / "backend" / "rest_server.py").read_text(encoding="utf-8")
        tree = _ast.parse(src)
        assigns = [
            (t.id, n.lineno) for n in _ast.walk(tree)
            if isinstance(n, _ast.Assign)
            for t in n.targets
            if isinstance(t, _ast.Attribute) and t.attr in {"_phonetic_provider", "_settings_get"}
        ]
        attrs = {a for a, _ in assigns}
        self.assertIn("_phonetic_provider", attrs, "REST-движку не назначен phonetic-провайдер")
        self.assertIn("_settings_get", attrs, "REST-движку не назначены настройки")


if __name__ == "__main__":
    unittest.main()
```

🔴 Две проверки перед прогоном (имена могут отличаться — сверить `rg` и поправить тест, не код): конструктор `PhoneticVocabService` принимает `Path` (не str); `get_default_hotwords() -> list[str]` (плоский; `seed_hotwords(settings_svc)` возвращает int и в тесте не вызывается); имя `TextUtils._strip_hallucinations` (staticmethod?). Несовпадение имени — править ТЕСТ под код.

- [ ] **Step 2: Прогнать — ждём RED**

```bash
PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_w4_vocab_bake_2026_09.py -v -p no:cacheprovider
```

Ожидаемо: FAIL по парам/seed/фильтру (незапечено). Если FAIL по именам конструкторов — это проверка имён выше, не RED.

### Task 2: Запекание (GREEN)

**Files:**
- Modify: seed entries `phonetic_vocab.json` (через код seed'а — см. ниже ГДЕ),
- Modify: `KrabEar/backend/default_hotwords.py` (+`оверлей`, `openclaw`),
- Modify: `KrabEar/core/utils.py` (+tail-правило голого токена в `_HALLUCINATION_PATTERNS`),
- Modify: `KrabEar/core/config.py` (`phonetic_vocab_enabled` False → True),
- Modify: `KrabEar/backend/rest_server.py` (провайдер + settings_get для REST-движка),
- Test: `KrabEar/tests/test_w4_vocab_bake_2026_09.py`

- [ ] **Step 1: Seed entries.** `phonetic_vocab.json` живёт в data_dir (не в git) — запекание = код, кладущий entries при пустом файле. Найти существующий seed-путь (`rg -n 'seed|initial|default.*entr' KrabEar/backend/phonetic_vocab_service.py`); если seed'а нет — добавить метод `ensure_seeded()` + вызов из `get_entries()` при пустом списке; entries — из списка выше (canonical + variants, longest-first делает `correct()` сам). Лимит 200 записей/200 символов не превышаем (~13 записей).
- [ ] **Step 2: Hotwords** — добавить `оверлей`, `openclaw` в подходящую категорию `default_hotwords.py` (проверить слияние seed в `build_initial_prompt`; бюджет 560 chars/BPE 224 не трогаем — 2 коротких слова).
- [ ] **Step 3: Фильтр** — в `_HALLUCINATION_PATTERNS` добавить tail-правило СТРОГО ПОСЛЕ существующего субтитр-паттерна (иначе «…субтитры создавал диматорзок» срежет только токен, оставив «субтитры создавал»):
  `r"(?:dima\s?torzok|дима\s?торзок|dimatorzok|диматорзок)[.!?…]*$"` — рез хвоста по общей механике (`start<=0 → ""`). НЕ резать в середине текста (галлюцинации дописываются в хвост; середина — живая речь владельца про саму проблему).
- [ ] **Step 4: Флаг** — `config.py:1122` False → True + валидатор/рекомендованный сетап при необходимости (проверить `settings_validator.py` и `apply_recommended_setup` на упоминания флага).
- [ ] **Step 5: REST** — после создания `transcriber` в `rest_server.py` (~875): `engine._settings_get = lambda k, d=None: store.load_settings().get(k, d)` (паттерн `:2647`) + `engine._phonetic_provider = PhoneticVocabService(data_dir=store.data_dir).get_entries`. Проверить отсутствие второго engine-конструктора в файле (`rg -n 'AudioEngine\('`).
- [ ] **Step 6: GREEN** — команда Task 1 Step 2. Ожидаемо: все тесты зелёные (включая новый `test_rest_engine_wired_via_ast`).
- [ ] **Step 7: Регрессия** — существующие наборы путей: `test_phonetic_vocabulary.py`, `test_hallucination_subtitles_verb_W1894.py`, `test_engine_cleanup.py`, `test_auto_learn_e2e.py`, `test_rest_server_unit.py`. Ожидаемо: зелёные без правок (правка W1894-файла запрещена — bare-token покрыт новым файлом).
- [ ] **Step 8: Гейт** — flake8 (max-120) изменённых файлов + `scripts/pre_merge_py312_check.sh` на новый тест + `make audit-all` (тронут `core/` и сервисы — гейт обязателен по playbook).

### Task 3: Коммит и PR

- [ ] `git branch --show-current` → `feat/w4-vocab-bake`
- [ ] `git add` **явными путями** (только файлы Task 2)
- [ ] Коммит: `feat(stt): лексика W4 запечена (phonetic + hotwords + dimatorzok-фильтр)`
- [ ] PR в `codex/krab-ear-v2` с цифрами Task 0 (покрытие пар); НЕ мержить.

## Definition of Done

- Новый тест RED→GREEN; регрессия 5 файлов зелёная; audit-all зелёный.
- REST `/v1/stt/transcribe` корректирует (доказано `test_rest_engine_wired_via_ast` из Task 1).
- WER-замер до/после — НЕ в этой карточке (ждёт R2-эталоны владельца); в отчёт — только покрытие Task 0.
- Прод-backend/REST не перезапускались (деплой — отдельной волной по окну владельца).

## Вне scope (записать в отчёт, не чинить)

Новые кандидаты Task 0 (частота ≥20, не из списка) — списком координатору. `maby` — только примеры. Мёртвый `hallucination_manager.py` — не трогать.
