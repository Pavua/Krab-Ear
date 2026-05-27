# Audit W1064: GainNormalizer — аудит нормализации усиления аудио

**Дата:** 2026-05-26  
**Файл:** `KrabEar/core/gain_normalizer.py` (267 строк)  
**Тесты:** `KrabEar/tests/test_gain_normalizer.py` (39 тестов, все проходят)

---

## Резюме

`GainNormalizer` — добротно структурированный модуль с soft-knee limiter'ом и авто-режимом. Тесты охватывают основные сценарии (тишина, стерео, идемпотентность, конкурентность). Обнаружены **5 реальных находок**: два bug-класса (NaN/Inf propagation), одна дыра в API-контракте (target_db не валидируется), дефект формулы soft-knee (разрыв непрерывности) и критический факт — модуль **нигде не подключён** в production-коде.

---

## Находки

### F1 — BUG: NaN во входных данных распространяется в выход (HIGH)

**Файл:** `gain_normalizer.py:61-64`, `130`

`_rms_db()` использует `np.sqrt(np.mean(audio ** 2))`. Если аудиомассив содержит `NaN`, `np.mean` возвращает `NaN`, `rms < 1e-12` даёт `False`, и вычисление продолжается с `gain_db = NaN`, `gain_linear = NaN`. Умножение массива на `NaN` (строка 130) выдаёт `RuntimeWarning` и массив целиком заполняется `NaN`. Downstream STT получит `NaN`-аудио и зависнет или выдаст мусор.

```python
# Воспроизведение:
audio = np.array([float('nan'), 0.1, 0.2], dtype=np.float32)
r = GainNormalizer().normalize(audio)
# r.audio — весь NaN, r.gain_applied_db = nan (no exception raised)
```

**Рекомендация:** добавить guard в начало `normalize()` и `auto_gain()`:
```python
audio = self._to_mono_float32(audio)
if len(audio) and (np.any(np.isnan(audio)) or np.any(np.isinf(audio))):
    logger.warning("GainNormalizer: входной сигнал содержит NaN/Inf — заменяем нулями")
    audio = np.nan_to_num(audio, nan=0.0, posinf=0.0, neginf=0.0)
```

---

### F2 — BUG: Inf во входных данных пропускается молча (MEDIUM)

**Файл:** `gain_normalizer.py:61-64`

Входной `+inf` даёт `_rms_db() = +inf`, `gain_db = target_db - inf = -inf`. `_db_to_linear(-inf) = 0.0`, поэтому умножение `audio * 0` обнуляет сигнал — потеря входного аудио. `input_rms_db = round(inf, 3)` не бросает исключение, но поле содержит `inf` (невалидный JSON при сериализации).

```python
# r.input_rms_db = inf, r.gain_applied_db = -inf — невалидный JSON
```

**Рекомендация:** обрабатывать вместе с F1 через `np.nan_to_num` или явный guard.

---

### F3 — DESIGN: Soft-knee formula создаёт разрыв непрерывности (MEDIUM)

**Файл:** `gain_normalizer.py:256`

```python
compressed_abs = threshold + (knee_top - threshold) * (2 * t - t ** 2) * 0.5
```

При `t=1` (семпл на верхней границе knee-зоны): `(2·1 - 1²) · 0.5 = 0.5`, поэтому максимальный выход колена = `threshold + (knee_top - threshold) · 0.5 = 0.975` — это **середина** диапазона `[threshold, knee_top]`, а не `knee_top`. Следующий семпл с `|x| >= knee_top` уходит в hard-clip до `1.0`. Скачок: `0.975 → 1.0` при переходе через `knee_top=1.0`.

Стандартная квадратичная формула для soft-knee должна давать `compressed = knee_top` при `t=1`. Правильный вариант:
```python
# Стандартная параболическая компрессия: f(0)=threshold, f(1)=knee_top
compressed_abs = threshold + (knee_top - threshold) * (2 * t - t ** 2)
# (без умножения на 0.5)
```

Текущий вариант звучит «мягче» (меньше компрессии в колене), но нарушает C¹-непрерывность на верхней границе — потенциально слышимый артефакт при агрессивном усилении.

---

### F4 — UNWIRED: Модуль не импортируется ни в одном production-файле (HIGH)

**Статус:** dead code для STT pipeline

Полный поиск по всем `.py` файлам `KrabEar/` (кроме тестов) не выявил ни одного `import` или `from ... import` `GainNormalizer`. Ни `engine.py`, ни модули `core/pipeline/`, ни `backend/service.py`, ни любой другой production-файл не подключают нормализатор.

`engine.py` выполняет собственную нормализацию аудио перед Whisper (через `AudioEngine`). `GainNormalizer` добавлен как self-contained модуль, но не интегрирован в STT-цепочку.

**Рекомендация:** либо подключить в `core/pipeline/` (например, как стадию перед STT), либо зафиксировать как «готовая, но неактивная» фича с TODO-комментарием в модуле.

---

### F5 — DESIGN: target_db не валидируется, принимает физически невозможные значения (LOW)

**Файл:** `gain_normalizer.py:97-101`

`target_db` не ограничен диапазоном. Вызов с `target_db=+6.0` (выше 0 dBFS) даёт `gain_applied_db ≈ 69 dB` для тихого сигнала, limiter обрезает до `1.0` — всё «работает», но диагностика (`gain_applied_db=69 дБ`) вводит в заблуждение. Значения `target_db > -3.0` для RMS-нормализации речевых сигналов лишены смысла.

```python
# Разумный guard:
if target_db > 0.0:
    raise ValueError(f"target_db={target_db} выше 0 dBFS — недопустимо для RMS-нормализации")
```

---

### F6 — MINOR: Пустой массив вызывает RuntimeWarning из numpy (LOW)

**Файл:** `gain_normalizer.py:61`

`_rms_db(np.array([]))` → `np.mean([]) = nan` с `RuntimeWarning: Mean of empty slice`. Хотя код корректно обрабатывает пустой массив через `rms < 1e-12` guard (так как `nan < 1e-12 = False`, но `nan` возвращается как `_SILENCE_FLOOR_DB` по той ветке... нет, `nan < 1e-12` это `False`, значит уходит в `math.log10(nan)` → `RuntimeWarning: invalid value`), итоговый `_rms_db` для пустого массива возвращает `nan`, а не `_SILENCE_FLOOR_DB`.

Практический эффект: пустой массив → `input_rms_db = nan` → проверка `<= _SILENCE_FLOOR_DB` (`nan <= -80.0 = False`) → вычисление `gain_db = target_db - nan = nan` → выход с пустым аудио (т.к. размножение на NaN пустого массива = пустой массив). Конечный результат корректен (пустой массив), но путь через NaN — хрупок и шумит в логах.

**Рекомендация:** добавить early-return до `_rms_db()`:
```python
if len(audio) == 0:
    return GainResult(audio=audio, gain_applied_db=0.0,
                      input_rms_db=_SILENCE_FLOOR_DB, output_rms_db=_SILENCE_FLOOR_DB,
                      clipped_samples=0)
```

---

## Тестовое покрытие

| Сценарий | Покрыт |
|----------|--------|
| RMS достигает target_db | да |
| Тишина / пустой массив | да (но RuntimeWarning не проверяется) |
| Soft-knee limiter peak ≤ 1.0 | да |
| `auto_gain` тихий/громкий | да |
| Стерео → моно | да |
| Идемпотентность | да |
| Конкурентный доступ | да |
| **NaN во входных данных** | **нет** |
| **Inf во входных данных** | **нет** |
| **target_db > 0** | нет (нет guard'а) |
| Soft-knee непрерывность на границе | нет |

Итого: **39/39 проходят**, но F1/F2 не покрыты тестами — production-риск при приходе аудио из поврежденных буферов или ошибочных float-конверсий.

---

## Wire-статус

`GainNormalizer` **не подключён** к production-коду (F4). Модуль изолирован — изменения в нём не влияют на работу STT pipeline до момента интеграции.

---

## Рекомендуемые приоритеты

1. **F4** — принять решение: интегрировать в pipeline или пометить как TODO
2. **F1 + F2** (можно одним коммитом) — `np.nan_to_num` guard + тест
3. **F6** — early-return для пустого массива
4. **F3** — исправить формулу knee (при интеграции в pipeline)
5. **F5** — добавить validation при интеграции
