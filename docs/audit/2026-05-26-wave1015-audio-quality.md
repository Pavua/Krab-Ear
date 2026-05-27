# Аудит: AudioQualityAnalyzer — Wave W1015

**Файл:** `KrabEar/core/audio_quality.py`  
**Дата:** 2026-05-26  
**Статус:** 6 находок (1 HIGH, 3 MEDIUM, 2 LOW)

---

## Контекст

`AudioQualityAnalyzer` — pre-flight проверка аудио перед STT. Вычисляет RMS, peak, SNR, clipping ratio, silence ratio и итоговую оценку качества. Используется через IPC-метод `analyze_audio_quality` → `AudioAnalyticsService.handle_analyze_audio_quality` → `analyze_file()`.

W912 должен был унифицировать порог тишины с `SilenceDetector`. Проверка показала частичное выполнение.

---

## Находки

### F-1 · HIGH · NaN/Inf propagation в IPC-ответ

**Описание.** При подаче аудио с `NaN` или `Inf` семплами (повреждённый файл / артефакт конвертации):
- `rms_level = nan`, `peak_level = nan`  
- `snr_estimate_db = nan` (Inf-вход) или `-0.0` (NaN-вход)

`to_dict()` сериализует их как есть; `json.dumps(ensure_ascii=False)` — используемый в IPC — молча записывает литеральные токены `NaN` и `Infinity`, которые **не входят в спецификацию JSON RFC 8259**. Swift `JSONDecoder` аварийно завершает декодирование, возвращая ошибку вместо отчёта.

**Воспроизведение:**
```python
arr = np.full(16000, float('nan'), dtype=np.float32)
r = AudioQualityAnalyzer().analyze(arr, 16000)
print(r.rms_level)   # nan
import json; json.dumps(r.to_dict())  # '{"rms_level": NaN, ...}' — invalid JSON
```

**Исправление.** В `to_dict()` применить `math.isfinite` guard:
```python
def _safe(v: float) -> float:
    return v if math.isfinite(v) else 0.0
```
Или sanitize в `analyze()` перед `round()`.

---

### F-2 · MEDIUM · Двойной порог тишины внутри одного файла

**Описание.** В `audio_quality.py` существуют два разных определения «тишины»:

| Место | Порог | дБ |
|---|---|---|
| `_compute_silence_ratio()` | `_SILENCE_RMS_THRESHOLD = 0.001` | –60 dB |
| `_estimate_snr()` `quiet_mask` | `_SILENCE_RMS_THRESHOLD * 10 = 0.01` | –40 dB |

`silence_ratio` считается по –60 dB, SNR — ищет noise floor по –40 dB. Для аудио с фоновым шумом –50 dB эти пути дадут противоречивые результаты: фреймы будут «не тихими» для `silence_ratio`, но «тихими» для SNR.

**Исправление.** Выделить отдельные именованные константы:
```python
_SILENCE_RATIO_THRESHOLD_RMS = 0.001   # -60 dB: для классификации фреймов
_SNR_NOISE_FLOOR_THRESHOLD_RMS = 0.01  # -40 dB: совпадает с SilenceDetector
```

---

### F-3 · MEDIUM · W912 threshold gap: расхождение с SilenceDetector

**Описание.** W912 должен был унифицировать порог тишины. `SilenceDetector` (в `core/silence_detector.py`) использует по умолчанию `-40 dB` (amplitude threshold `0.01`). `_compute_silence_ratio` использует `0.001` (–60 dB).

Следствие: `silence_ratio` и `SilenceDetector.detect_silence()` дают **разные** границы регионов тишины для одного и того же аудио. Если оба результата используются в pipeline, интерпретация противоречивая.

**Исправление.** Синхронизировать `_compute_silence_ratio` с `_SNR_NOISE_FLOOR_THRESHOLD_RMS = 0.01` (–40 dB) или добавить параметр.

---

### F-4 · MEDIUM · Short audio: SNR всегда 0.0 → `poor` оценка

**Описание.** `_estimate_snr` возвращает `0.0` для аудио короче `4 * _SILENCE_FRAME_SIZE = 4096` семплов (~0.256 с при 16 kHz). `_score()` при `snr_db=0.0` возвращает `"poor"`.

Это означает, что любой корректный звуковой фрагмент длиной < 0.26 с автоматически маркируется как `poor`. Сценарий: пользователь записывает короткое слово или тест-щелчок → pre-flight check блокирует/предупреждает неверно.

**Дополнительный аспект.** Единственный семпл (1 sample) корректно вычисляет RMS=0.5 и peak=0.5, но `snr=0.0`, `quality_score="poor"`. Это может дезориентировать вызывающий код.

**Исправление.** При `n < 4 * _SILENCE_FRAME_SIZE` возвращать специальное значение (например, `None` или `float('nan')`) либо применять упрощённый SNR через единственный peak/RMS ratio.

---

### F-5 · LOW · `_error_bus` wiring мёртв в продакшне

**Описание.** Wave 64 добавил guard: при `n_samples == 0` пушить `stt.empty_audio_warning` в `ErrorBus` (строки 78–98). Однако атрибут `_error_bus` выставляется только если вызывающий код **явно** его устанавливает. `AudioAnalyticsService` создаёт `AudioQualityAnalyzer` лениво через `analyze_file()`, которая не принимает `error_bus`. В продакшне атрибут никогда не установлен → guard-код (`lines 78–98`) никогда не исполняется.

**Исправление.** Добавить `error_bus` параметр в `AudioQualityAnalyzer.__init__` (optional, default None) и передавать его из `AudioAnalyticsService`.

---

### F-6 · LOW · `sample_rate=0` возвращает некорректную длительность

**Описание.** `duration_sec = n_samples / max(sample_rate, 1)`. При `sample_rate=0` делитель становится `1`, возвращая `n_samples` секунд. Для массива из 100 семплов → `100.0` секунд. Случай маловероятен, но не защищён явным raise.

**Исправление.** Добавить guard в начале `analyze()`:
```python
if sample_rate <= 0:
    raise ValueError(f"sample_rate must be positive, got {sample_rate}")
```

---

## Тестовое покрытие

Из 35 тестов в `test_audio_quality.py`:
- Пустой массив: покрыт (`test_empty_audio_does_not_raise`, `test_empty_audio_returns_zero_metrics`)
- NaN/Inf вход: **не покрыт** — нет тестов на nan/inf аудио
- Короткое аудио `snr=0.0 → poor`: **не покрыт явно** (единственный семпл тестируется только на panic-безопасность)
- `sample_rate=0`: покрыт (`test_duration_zero_sample_rate_no_crash`)
- Thread safety: покрыт (`test_concurrent_analyze_thread_safe`)
- `_error_bus` wiring в `AudioAnalyticsService`: **не покрыт**

---

## Wire status

`analyze_audio_quality` → `AudioAnalyticsService.handle_analyze_audio_quality` → `analyze_file()` → `AudioQualityAnalyzer.analyze()`.

Wiring корректен. Метод зарегистрирован в `service.py:1020`. Диспетчер покрыт `test_dispatch_complete.py:821`.

---

## Итог

| ID | Severity | Суть | Статус |
|---|---|---|---|
| F-1 | HIGH | NaN/Inf → невалидный JSON → Swift crash | Открыт |
| F-2 | MEDIUM | Двойной порог тишины внутри файла | Открыт |
| F-3 | MEDIUM | W912 gap: audio_quality –60dB vs SilenceDetector –40dB | Частично |
| F-4 | MEDIUM | Short audio always `poor` из-за SNR=0.0 early return | Открыт |
| F-5 | LOW | `_error_bus` guard мёртв в продакшне | Открыт |
| F-6 | LOW | `sample_rate=0` → некорректная duration без raise | Открыт |

Производительность хорошая: 1-часовое аудио (57.6 М семплов) анализируется за ~0.55 с на M4 Max.
