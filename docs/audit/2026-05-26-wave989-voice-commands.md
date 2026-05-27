# Аудит voice_commands.py — Wave 989

**Файл:** `KrabEar/core/voice_commands.py`  
**Дата:** 2026-05-26  
**Метод:** read-only  
**Тесты:** `KrabEar/tests/test_voice_commands.py`

---

## Итог: 6 findings (1 HIGH, 3 MEDIUM, 2 LOW)

---

## F1 — HIGH: \b word boundary не работает для Cyrillic

**Файл:** `voice_commands.py:131`

```python
return re.compile(r"\b" + escaped + r"\b", re.IGNORECASE)
```

`\b` в Python `re` определяет границу слова через `\w` / `\W`. В Python `\w` для кириллицы работает корректно (Unicode), НО поведение `\b` с кириллическим текстом нестабильно на некоторых платформах и зависит от локали. Конкретная проблема: `\b` между двумя кириллическими буквами часто не совпадает ожидаемым образом при смешанном тексте (кириллица + пробел + ASCII или знаки препинания). Например, паттерн `\bточка\b` корректно не совпадёт с `«точка»` (кавычки — не `\W` в unicode context).

**Риск:** команда `точка` внутри текста типа `"результат — точка!"` (с em dash перед) может не совпасть, т.к. `—` является `\W`, но `\b` требует чередования `\w/\W`. Реально паттерн использует `pattern.match(text, pos)` — то есть матч только с текущей позиции `pos`, а позиция `pos` инкрементируется по одному символу. Таким образом `\b` в начале паттерна избыточен при `pattern.match()` с явным `pos`, но `\b` в конце паттерна (`\b` после последнего слова команды) нужен и может давать false-negative при кириллическом следующем символе без пробела.

**Статус теста:** `test_word_boundary_preserved` проверяет только очевидный случай (`"в запятой строке"`), но не тест граничных символов типа `"(точка)"` или `"точка-тире"`.

**Рекомендация:** заменить trailing `\b` на `(?=\s|$|[^а-яёa-z])` (lookahead) или использовать `re.UNICODE` явно и проверить edge cases с non-space разделителями.

---

## F2 — MEDIUM: «точка» vs «точка с запятой» — порядок зависит от match, а не longest-first в реальном потоке

**Файл:** `voice_commands.py:39–40, 47, 285–338`

Таблица команд задаёт правильный порядок (составные раньше одиночных), НО алгоритм `_apply_commands` использует `pattern.match(text, pos)` и берёт **первый** совпавший паттерн, а не **самый длинный**. Для `"точка с запятой"` порядок в таблице правильный (строка 40 до строки 47), поэтому работает.

Скрытый риск: в ES-таблице строка `"nueva línea"` продублирована дважды (строки 71–72):

```python
(r"nueva línea", "insert", "\n"),
(r"nueva línea", "insert", "\n"),  # дублирует строку выше
```

Это не баг-по-результату (оба действия одинаковы), но создаёт ложное ощущение, что паттерн применится дважды. На самом деле `break` (строка 338) останавливает на первом совпадении. **Дубликат можно убрать.**

**Риск:** будущее добавление паттерна с пересекающимся prefix без учёта порядка сломает disambiguation.

---

## F3 — MEDIUM: Команда «удалить последнее» (fallback) конкурирует с «удалить последнее слово/предложение/абзац»

**Файл:** `voice_commands.py:31–34`

```python
(r"удалить последнее слово",      "delete_last", "word"),
(r"удалить последнее предложение","delete_last", "sentence"),
(r"удалить последний абзац",      "delete_last", "paragraph"),
(r"удалить последнее",            "delete_last", "word"),  # fallback
```

Порядок корректен (длинные раньше). Однако у ES-варианта порядок нарушен:

```python
(r"borrar última palabra",  "delete_last", "word"),
(r"borrar último párrafo",  "delete_last", "paragraph"),
(r"borrar última oración",  "delete_last", "sentence"),
(r"borrar último",          "delete_last", "word"),
```

`"borrar último párrafo"` содержит `"párrafo"`, а `"borrar última oración"` — `"oración"`. Поскольку fallback `"borrar último"` идёт последним — порядок ОК. Но `"borrar última"` не совпадёт с `"borrar último"` (разный род), что означает что fallback никогда не сработает для `"borrar última oración"` если пользователь скажет просто `"borrar última"`. **Fallback на женский род отсутствует.**

---

## F4 — MEDIUM: `uppercase_sent` не сбрасывается командой `capitalize_next`; флаги независимы

**Файл:** `voice_commands.py:312–353`

```python
capitalize_next = False
uppercase_next_sentence = False
```

Оба флага независимы и не сбрасываются друг другом. Если пользователь скажет `"верхний регистр большая буква слово"` — оба флага будут `True` одновременно. `uppercase_next_sentence` имеет приоритет (ветка `elif`) и `capitalize_next` никогда не применится, пока `uppercase_next_sentence=True`. После знака препинания `uppercase_next_sentence` сбросится, и `capitalize_next` применится к **следующему** слову — что является неожиданным поведением.

Тестов на комбинацию `capitalize_next + uppercase_sent` нет.

---

## F5 — LOW: Instantiation внутри hot-path (engine.py:933)

**Файл:** `KrabEar/core/engine.py:932–933`

```python
from core.voice_commands import VoiceCommandProcessor  # lazy — avoid circular
_vc_processor = VoiceCommandProcessor(settings_get=self._settings_get)
```

`VoiceCommandProcessor` инстанцируется на каждый вызов `transcribe()`. Объект stateless и лёгкий, но импорт `from core.voice_commands` и конструктор вызываются при каждой транскрипции. `_COMPILED` кэшируется на уровне модуля, поэтому компиляция regex выполняется один раз. Практического влияния на latency нет (~мкс), но паттерн не соответствует практике остальных сервисов (создаются один раз в `__init__`).

---

## F6 — LOW: Нет теста на идемпотентность

**Файл:** `KrabEar/tests/test_voice_commands.py`

Тесты не проверяют, что `process(process(text))` == `process(text)`. Например, результат `"раз, два"` содержит запятую, но `process` уже не распознает её как команду (это не слово `"запятая"`). Идемпотентность де-факто выполняется из-за характера паттернов, но не задокументирована тестом.

---

## Wire Status

**АКТИВЕН.** Вызывается в `KrabEar/core/engine.py:932–937` как шаг `4.3` pipeline после `cleanup_transcript` и до `number_normalizer`. Включён по умолчанию (`voice_commands_enabled=True`). Настраивается через IPC: `voice_commands_enabled` (bool) + `voice_commands_languages` (list).

---

## Покрытие тестами

| Категория                          | Статус     |
|------------------------------------|------------|
| Базовые RU команды (13 тестов)     | Покрыто    |
| Базовые ES команды (7 тестов)      | Покрыто    |
| Базовые EN команды (11 тестов)     | Покрыто    |
| Code-switching (язык не совпадает) | Покрыто    |
| Disabled flag                      | Покрыто    |
| Edge cases (empty, only cmd, etc.) | Покрыто    |
| Thread safety                      | Покрыто    |
| `\b` с non-space разделителями     | **НЕ ПОКРЫТО** |
| `capitalize_next` + `uppercase_sent` combo | **НЕ ПОКРЫТО** |
| ES fallback род (borrar última)    | **НЕ ПОКРЫТО** |
| Идемпотентность                    | **НЕ ПОКРЫТО** |

---

## Действия (опционально)

1. **F1** (HIGH): добавить тест `"(точка)"` → `"(.)"` и `"точка-тире"` → ожидаемое. Если падает — заменить trailing `\b` на `(?=\s|$)`.
2. **F2**: удалить дубликат `"nueva línea"` в `_ES_COMMANDS` (строка 72).
3. **F3**: добавить `(r"borrar última", "delete_last", "word")` как дополнительный ES fallback.
4. **F4**: при установке `capitalize_next` сбрасывать `uppercase_next_sentence` и наоборот — или задокументировать приоритет.
5. **F5**: вынести instantiation в `AudioEngine.__init__` как `self._vc_processor`.
