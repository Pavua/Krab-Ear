# Wave 1003 — FuzzySearcher audit

**File**: `KrabEar/core/fuzzy_search.py`  
**Date**: 2026-05-26  
**Scope**: algorithm, locale handling, performance, thresholds, tokenization, privacy, wire status, tests, caching, memory

---

## 1. Algorithm — difflib.SequenceMatcher (не Levenshtein)

`FuzzySearcher._score()` использует `difflib.SequenceMatcher.ratio()` — longest-common-subsequence подход, не Levenshtein/Jaro-Winkler/Damerau-Levenshtein. Нормализация: `ratio()` возвращает `2.0 * M / T` где `M` — совпадающие символы, `T` — суммарная длина. Диапазон [0.0, 1.0] корректен.

Плюсы: нет внешних зависимостей, работает с Unicode из коробки.  
Минус: `SequenceMatcher` — O(n²) в худшем случае по символам, медленнее специализированных расстояний.

`_partial_ratio()` реализует скользящее окно размером `len(query)` по тексту — хорошо работает для подстрочного поиска.

---

## 2. Cyrillic/Spanish accents — `é` ≠ `e` (может быть проблемой)

`query.lower()` / `text.lower()` выполняется, но нормализация Unicode (NFC/NFD/NFKD) **не применяется**. Это означает:

- `é` (U+00E9) vs `e` (U+0065) → разные символы, SequenceMatcher их не отождествляет.
- Запрос `"fantastico"` и текст `"fantástico"` — SequenceMatcher посчитает расстояние 1 символ, score ≈ 0.94. Тест `test_spanish_typo_tolerance` подтверждает, что при threshold=0.7 матч проходит.
- Для чисто кириллических запросов проблем нет — у кириллицы нет diacritics, `lower()` достаточен.

**Вывод**: поведение приемлемо для целевых языков (RU/ES/EN). `é` ≠ `e` в _full_score_, но partial_ratio поглощает разницу в 1 символ при длинных строках. Критичный сценарий: очень короткие испанские запросы с диакритикой (2-3 символа) могут не матчить. Не блокер, но стоит задокументировать.

---

## 3. Производительность — O(N × L²) в худшем случае

На каждый запрос:
- Итерация по всем `N` записям истории (в `handle_fuzzy_search`: `limit=10_000`).
- Для каждой записи вызов `_score()` = `_full_score` + `_partial_ratio`.
- `_partial_ratio` с шагом `step = max(1, (t_len - q_len) // 20)` ограничивает количество позиций до ≈20 + 2. Каждый `SequenceMatcher.ratio()` — O(q_len × window_len).

**Бенчмарк в тестах** (`test_performance_benchmarks.py`): 1000 текстов < 2.0 s. Для 10,000 записей — линейный рост, ожидаемо ~15–20 s. Это **медленно для интерактивного поиска**.

Оптимизация шага (до 20 позиций) реально работает и значительно снижает O для длинных транскриптов. Тем не менее, ранней отсечки по длине нет (кроме `min_text_len`).

**Находка**: `handle_fuzzy_search` создаёт новый `FuzzySearcher()` на **каждый вызов** — это не проблема (объект stateless), но паттерн несимметричен с другими сервисами.

---

## 4. Threshold по умолчанию — 0.6 (приемлем, но граничен)

`threshold=0.6` — стандарт. Сравнение:
- `fuzzywuzzy` / `rapidfuzz` обычно используют 0.7–0.8 для production UI.
- При 0.6 возможны ложные срабатывания для коротких запросов (слова 3-5 символов).
- `min_text_len = max(1, query_len // 3)` отсекает слишком короткие тексты, что немного компенсирует шум.

**Вывод**: 0.6 приемлемо для RU-транскриптов (длинные тексты). Для поиска по отдельным словам может давать шум. Альтернатива — поднять default до 0.65–0.7 или добавить length-aware threshold.

---

## 5. Токенизация — full-text (посимвольный), не по словам

`FuzzySearcher` работает с полными строками посимвольно. Нет токенизации по словам, нет TF-IDF. Это значит:

- Запрос `"транскрипция"` в тексте `"Голосовая транскрипция в Krab Ear"` (33 символа) даёт partial_ratio ≈ 1.0 для 12-символьного окна — ✅ работает.
- Для многословных запросов с перестановкой слов (`"мир Привет"` vs `"Привет мир"`) — score существенно ниже, т.к. позиция символов важна.

**Вывод**: приемлемо для типичного сценария (короткий запрос ⊆ длинный транскрипт). Перестановка слов не поддерживается — это известное ограничение `SequenceMatcher`.

---

## 6. Privacy mode — не учитывается ⚠️

`handle_fuzzy_search` в `history_service.py` грузит до 10 000 записей истории и передаёт их тексты в `FuzzySearcher.search()`. Проверки на `privacy_mode` нет.

Сравнение: `SemanticSearcher` (backend/semantic_search.py) аналогично не проверяет privacy_mode. Это системная особенность — privacy mode влияет на paste/record, но не на поиск по уже сохранённым данным. Тем не менее, если policy требует запрещать поиск в privacy-режиме, это потребует явной проверки флага.

**Риск**: низкий при текущей архитектуре, но стоит задокументировать как known gap.

---

## 7. Wire status — активно подключён в production ✅

`service.py:918`:
```python
"fuzzy_search": self._history.handle_fuzzy_search,
```

Делегирует в `HistoryService.handle_fuzzy_search` → `FuzzySearcher.search()`. Путь полностью рабочий.

---

## 8. Тестовое покрытие — хорошее

Три тест-класса:
- `FuzzySearcherUnitTests` (10 тестов): exact match, partial, typo, threshold, RU, empty, sort, short-text skip.
- `FuzzySearcherAdditionalTests` (10 тестов): no-match, score range, empty strings, ES с диакритикой, index correctness, case preservation, threshold=0.
- `FuzzySearcherWave111Tests` (6 тестов): exact score 1.0, typo, dissimilar, unicode substring, empty query, case-insensitive, concurrent.
- `HistoryServiceFuzzySearchTests` (5 интеграционных тестов): IPC path, RU query, empty query, response structure, threshold=1.0.

Перформанс: `test_performance_benchmarks.py` — 1000 текстов < 2.0 s.  
Property: `test_property_based.py::TestFuzzySearcherProperties`.

**Отсутствуют**: тест с privacy_mode active; тест при 10 000+ записях (только 1000 в benchmark).

---

## 9. Index caching — нет, rebuild на каждый запрос

`FuzzySearcher` stateless — нет внутреннего индекса, нет кэша. Каждый вызов `search()` проходит полный O(N) скан. `handle_fuzzy_search` создаёт новый `FuzzySearcher()` на каждый IPC вызов.

Для сравнения: `SearchIndex` (`core/search_index.py`) — инвертированный индекс для быстрого поиска. `FuzzySearcher` — отдельный path без индекса.

**Вывод**: при 10K записей и частых поисковых запросах возможна деградация. Кэш fuzzy-результатов (TTL 30 s) или предварительный фильтр через `SearchIndex` мог бы помочь.

---

## 10. Memory bound — линейный, не растёт без ограничений

`handle_fuzzy_search` читает до `10_000` записей через `get_history_page_filtered`. Этот предел жёстко задан в коде. При история > 10K записей старые записи не обыскиваются — это **ограничение функциональности**, но защита от OOM.

`FuzzySearcher` сам не хранит состояние — памяти потребляет только время выполнения (один `window` строки в `_partial_ratio`). Нет утечек.

---

## Итог: 5 приоритетных находок

| # | Находка | Серьёзность |
|---|---------|-------------|
| 1 | **Производительность**: 10K записей × скользящее окно может занимать ~15–20 s для длинных транскриптов | MEDIUM |
| 2 | **Нет кэша / предварительного фильтра**: каждый запрос — полный O(N) скан, нет TTL cache или `SearchIndex` pre-filter | MEDIUM |
| 3 | **Privacy mode не проверяется** в `handle_fuzzy_search` — нет явного отказа при `privacy_mode=True` | LOW |
| 4 | **Диакритика**: `é` ≠ `e` (Unicode normalization отсутствует) — приемлемо для RU, граничный случай для коротких ES-запросов | LOW |
| 5 | **Threshold 0.6** может давать шум для коротких (3–5 символов) запросов — рассмотреть length-aware adaptive threshold | INFO |

Критических багов не обнаружено. Алгоритм корректен, тестовое покрытие хорошее, wire status активен.
