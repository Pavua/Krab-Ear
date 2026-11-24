"""Поиск слова-пробуждения «Краб» по распознаванию речи (альтернатива акустической модели openWakeWord).

Зачем. Дообученная на голосе владельца модель openWakeWord не прошла приёмочный гейт (24–25.11.2026: ложные
срабатывания на его же речи при recall 44–52%), а этот подход на всех 273 клипах владельца (поток с паузами,
GigaAM v3 RNNT) даёт recall 118/132 = 89% и 0 ложных на реальных негативах (три «попадания» — сами слова «краб»). Модуль ловит не слово «в
потоке», а ИЗОЛИРОВАННУЮ КОРОТКУЮ РЕПЛИКУ: тишина → короткая речь (≤ 2,4 с) → тишина. Длинная речь (разговор,
телевизор, звонок) отсекается до распознавания и вообще не транскрибируется: это и экономия процессора,
и приватность.

Состав (чистая логика, без микрофона и без моделей — транскрибер приходит снаружи):
  * ``UtteranceSegmenter`` — энергетический VAD с адаптивным шумовым полом, режет поток кадров по 80 мс на реплики.
  * ``KeywordMatcher`` — сопоставление транскрипта со словом: «краб*», обрубки («кра», «раб»), только в короткой фразе.
  * ``SttKeywordModel`` — обёртка с контрактом openWakeWord-модели (``predict(chunk) -> {имя: score}``,
    ``reset()``): подставляется в ``OpenWakeWordAdapter._listen_loop`` вместо ``oww``, поэтому все защиты адаптера
    (privacy-ревизия, generation, watchdog, пауза на запись) продолжают действовать без изменений.

Инварианты (проверяются тестами ``test_stt_keyword_spotter.py``):
  * ``predict`` НИКОГДА не ждёт распознавание: оно идёт в отдельном потоке, иначе поток чтения микрофона
    переполнит буфер; пока воркер занят, новая реплика ОТБРАСЫВАЕТСЯ, а не копится в очереди.
  * Аудио и текст живут только в памяти: ни записи на диск, ни сети, ни транскрипта в логах (в лог — только типы
    ошибок и счётчики). Распознавание вызывается ТОЛЬКО для коротких изолированных реплик.
  * Сбой транскрибера = «не совпало», а не падение потока; ограничение частоты (по умолчанию 20 распознаваний/мин)
    и пауза после срабатывания защищают от шторма (урок инцидента с wake word 24.11: срабатывание каждые ~3 с).
"""

from __future__ import annotations

import logging
import math
import re
import threading
import time
from collections import deque
from typing import Any, Callable, Deque, Optional, Sequence

logger = logging.getLogger("KrabEar.Backend.SttKeywordSpotter")

SAMPLE_RATE = 16000
FRAME_SAMPLES = 1280  # 80 мс — тот же шаг, что у openWakeWord

# Сегментатор (кадры по 80 мс).
PRE_ROLL_FRAMES = 4      # тишина до реплики, попадающая в аудио для распознавания (0,32 с)
TRAIL_FRAMES = 4         # тишина после реплики, которая её закрывает (0,32 с)
MIN_LEAD_FRAMES = 5      # тишина ПЕРЕД репликой, чтобы она считалась изолированной (0,4 с > TRAIL)
MIN_SPEECH_FRAMES = 3    # короче — щелчок/вздох, не слово (0,24 с)
MAX_SPEECH_FRAMES = 30   # длиннее — разговор, а не обращение (2,4 с: вмещает «Краб, открой историю»)
ABS_RMS_THRESHOLD = 60.0    # int16; ниже — тишина при любом шумовом поле (40 уже разваливает VAD на шуме микрофона)
NOISE_FLOOR_FACTOR = 3.0    # речь = rms выше 3× шумового пола
NOISE_FLOOR_ALPHA = 0.05
# Внезапный устойчивый шум (вентилятор, кондиционер, посудомойка): сегмент становится «длинным» и не кончается,
# а пол в простое не обновляется → сегментатор глух. В таком сегменте пол подтягивается к НИЖНЕМУ уровню окна
# (минимум rms за последние кадры), а не к среднему: речь поверх шума пол не поднимает.
FLOOR_WINDOW_FRAMES = 12
FLOOR_FALL_ALPHA = 0.3
FLOOR_RISE_ALPHA = 0.25

DEFAULT_NAME = "krab_stt"
DEFAULT_COOLDOWN_SEC = 3.0
DEFAULT_MAX_PER_MINUTE = 20

_NON_WORD = re.compile(r"[^\w\s]", re.UNICODE)


def _to_ints(chunk: Any) -> list[int]:
    """Кадр как список int (поддерживает list и numpy-массивы без жёсткой зависимости от numpy)."""
    if hasattr(chunk, "tolist"):
        chunk = chunk.tolist()
    return [int(v) for v in chunk]


def _rms(samples: Sequence[int]) -> float:
    if not samples:
        return 0.0
    return math.sqrt(sum(v * v for v in samples) / len(samples))


class TranscriberUnavailable(Exception):
    """Распознаватель сейчас недоступен (занят звонком/диктовкой или не прогрет): реплика пропускается, это не ошибка."""


class KeywordMatcher:
    """Сопоставление транскрипта со словом-пробуждением.

    Совпадение: слово начинается с «краб», либо слово — известный обрубок/искажение распознавания короткого
    «Краб» («кра», «раб», «бра», «крап», «айкрап»). «Кран», «Грабли», «Брат», «Карп», «Рабочий» не совпадают.
    Только в КОРОТКОЙ фразе (≤ ``max_words`` слов): длинное предложение со словом «краб» — это разговор о крабе.
    """

    DEFAULT_PREFIX = "краб"
    DEFAULT_FRAGMENTS = ("кра", "раб", "бра", "крап", "айкрап")

    def __init__(self, *, prefix: str = DEFAULT_PREFIX, fragments: Sequence[str] = DEFAULT_FRAGMENTS,
                 max_words: int = 6) -> None:
        self._prefix = prefix
        self._fragments = frozenset(fragments)
        self._max_words = max_words

    @staticmethod
    def normalize(text: str) -> list[str]:
        cleaned = _NON_WORD.sub(" ", text.lower().replace("ё", "е"))
        return cleaned.split()

    def kind(self, text: Optional[str]) -> Optional[str]:
        """``"word"`` — слово «краб*», ``"fragment"`` — только обрубок/искажение, ``None`` — не совпало."""
        if not isinstance(text, str) or not text.strip():
            return None
        words = self.normalize(text)
        if not words or len(words) > self._max_words:
            return None
        if any(w.startswith(self._prefix) for w in words):
            return "word"
        if any(w in self._fragments for w in words):
            return "fragment"
        return None

    def matches(self, text: Optional[str]) -> bool:
        return self.kind(text) is not None


class UtteranceSegmenter:
    """Режет поток кадров ``FRAME_SAMPLES`` на изолированные короткие реплики (энергетический VAD)."""

    def __init__(self) -> None:
        self._floor = 0.0
        self.reset()

    def reset(self) -> None:
        self._silent_run = 0
        self._pre: Deque[list[int]] = deque(maxlen=PRE_ROLL_FRAMES)
        self._active: Optional[dict[str, Any]] = None
        self._recent: Deque[float] = deque(maxlen=FLOOR_WINDOW_FRAMES)

    def _threshold(self) -> float:
        return max(ABS_RMS_THRESHOLD, NOISE_FLOOR_FACTOR * self._floor)

    def push(self, chunk: Any) -> Optional[list[int]]:
        """Принимает ровно один кадр; возвращает аудио реплики (с тишиной до/после), когда она закончилась."""
        samples = _to_ints(chunk)
        if len(samples) != FRAME_SAMPLES:
            raise ValueError(f"ожидался кадр из {FRAME_SAMPLES} отсчётов, получено {len(samples)}")
        rms = _rms(samples)
        is_speech = rms > self._threshold()
        active = self._active

        if active is None:
            if is_speech:
                self._active = {
                    "frames": [f for chunk_ in self._pre for f in chunk_] + samples,
                    "span": 1,
                    "gap": [],
                    "valid": self._silent_run >= MIN_LEAD_FRAMES,
                }
                self._silent_run = 0
                self._pre.clear()
            else:
                alpha = FLOOR_FALL_ALPHA if rms < self._floor else NOISE_FLOOR_ALPHA  # вниз быстро, вверх медленно
                self._floor = (1 - alpha) * self._floor + alpha * rms
                self._silent_run = min(self._silent_run + 1, 10_000)
                self._pre.append(samples)
            return None

        if not active["valid"] and active["span"] > MAX_SPEECH_FRAMES:
            # сегмент давно не реплика: подтягиваем пол к нижнему уровню окна, чтобы выйти из него, когда шум стих
            # до «ровного» (или поднять порог над устойчивым шумом); только вверх — понижает пол простой.
            self._recent.append(rms)
            if len(self._recent) >= FLOOR_WINDOW_FRAMES:
                low = min(self._recent)
                if low > self._floor:
                    self._floor += FLOOR_RISE_ALPHA * (low - self._floor)
        else:
            self._recent.clear()

        if is_speech:
            gap = active["gap"]
            active["span"] += len(gap) + 1
            if active["span"] > MAX_SPEECH_FRAMES:
                active["valid"] = False
            if active["valid"]:
                for g in gap:
                    active["frames"].extend(g)
                active["frames"].extend(samples)
            active["gap"] = []
            return None

        active["gap"].append(samples)
        if len(active["gap"]) < TRAIL_FRAMES:
            return None
        # реплика закончилась: TRAIL_FRAMES тишины подряд
        gap_frames = active["gap"]
        emit = None
        if active["valid"] and active["span"] >= MIN_SPEECH_FRAMES:
            emit = list(active["frames"]) + [v for g in gap_frames for v in g]
        self._active = None
        self._silent_run = len(gap_frames)
        self._pre.clear()
        for g in gap_frames[-PRE_ROLL_FRAMES:]:
            self._pre.append(g)
        return emit


class SttKeywordModel:
    """Модель-обёртка с контрактом openWakeWord: ``predict(chunk) -> {имя: score}``, ``reset()``.

    ``transcribe(samples) -> str`` вызывается в рабочем потоке для КОРОТКОЙ изолированной реплики (int16, 16 кГц,
    numpy-массив при наличии numpy, иначе список). Результат predict: ``1.0`` ровно один раз после совпадения,
    иначе ``0.0``.
    """

    def __init__(
        self,
        transcribe: Callable[[Any], str],
        *,
        name: str = DEFAULT_NAME,
        matcher: Optional[KeywordMatcher] = None,
        cooldown_sec: float = DEFAULT_COOLDOWN_SEC,
        max_per_minute: int = DEFAULT_MAX_PER_MINUTE,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._transcribe = transcribe
        self._name = name
        self._matcher = matcher or KeywordMatcher()
        self._cooldown = max(0.0, float(cooldown_sec))
        self._max_per_minute = max(1, int(max_per_minute))
        self._clock = clock
        self._segmenter = UtteranceSegmenter()
        self._lock = threading.Lock()
        self._pending = 0
        self._busy_since: Optional[float] = None  # монотонное время начала текущего распознавания (health)
        self._fired = False
        self._gen = 0  # reset() увеличивает: результат распознавания, запущенного до reset, устарел
        self._cooldown_until = 0.0
        self._starts: Deque[float] = deque()
        self._stats = {"utterances": 0, "transcribed": 0, "matched": 0, "matched_word": 0, "matched_fragment": 0, "errors": 0, "unavailable": 0,
                       "dropped_busy": 0, "dropped_rate": 0, "suppressed_cooldown": 0, "stale_discarded": 0}
        self._cond = threading.Condition(self._lock)
        self._queue: Deque[Any] = deque()
        self._closed = False
        self._worker = threading.Thread(target=self._run, name="SttKeywordSpotter", daemon=True)
        self._worker.start()

    # ------------------------------------------------------------------ контракт модели
    def predict(self, chunk: Any) -> dict[str, float]:
        utterance = self._segmenter.push(chunk)
        if utterance is not None:
            self._submit(utterance)
        with self._lock:
            fired, self._fired = self._fired, False
        return {self._name: 1.0 if fired else 0.0}

    def reset(self) -> None:
        self._segmenter.reset()
        with self._lock:
            self._fired = False
            self._gen += 1

    # ------------------------------------------------------------------ служебное
    def stats(self) -> dict[str, int]:
        with self._lock:
            return dict(self._stats)

    def is_alive(self) -> bool:
        return self._worker.is_alive()

    def busy_for(self) -> float:
        """Сколько секунд идёт текущее распознавание (0.0 — воркер свободен). Для health: зависший транскрибер
        держит ``_pending`` и молча оглушает споттер, ``is_alive()`` при этом True."""
        with self._lock:
            started = self._busy_since
        return 0.0 if started is None else max(0.0, time.monotonic() - started)

    def drain(self, timeout: float = 5.0) -> bool:
        """Ждёт, пока воркер закончит текущее распознавание (для тестов и остановки)."""
        deadline = time.monotonic() + timeout  # реальное время: инжектируемые часы в тестах могут стоять
        with self._cond:
            while self._pending > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(min(remaining, 0.05))
        return True

    def close(self, timeout: float = 2.0) -> None:
        with self._cond:
            if self._closed:
                return
            self._closed = True
            self._queue.clear()
            self._cond.notify_all()
        self._worker.join(timeout)

    # ------------------------------------------------------------------ внутреннее
    def _submit(self, utterance: list[int]) -> None:
        now = self._clock()
        with self._cond:
            self._stats["utterances"] += 1
            if self._closed:
                return
            if self._pending > 0:
                self._stats["dropped_busy"] += 1
                return
            while self._starts and now - self._starts[0] > 60.0:
                self._starts.popleft()
            if len(self._starts) >= self._max_per_minute:
                self._stats["dropped_rate"] += 1
                return
            self._starts.append(now)
            self._pending += 1
            self._busy_since = time.monotonic()
            self._queue.append((self._gen, utterance))
            self._cond.notify_all()

    def _run(self) -> None:
        while True:
            with self._cond:
                while not self._queue and not self._closed:
                    self._cond.wait(0.2)
                if self._closed:
                    return
                gen, utterance = self._queue.popleft()
            kind = None
            try:
                samples = self._as_audio(utterance)
                text = self._transcribe(samples)
                kind = self._matcher.kind(text)
                with self._lock:
                    self._stats["transcribed"] += 1
            except TranscriberUnavailable:
                with self._lock:
                    self._stats["unavailable"] += 1
            except Exception as exc:  # noqa: BLE001 — сбой распознавания = «не совпало», воркер живёт
                with self._lock:
                    self._stats["errors"] += 1
                logger.warning("SttKeywordSpotter: распознавание реплики не удалось (%s)", type(exc).__name__)
            finally:
                with self._cond:
                    if kind is not None and gen != self._gen:
                        self._stats["stale_discarded"] += 1  # reset() случился, пока шло распознавание
                    elif kind is not None:
                        now = self._clock()
                        if now < self._cooldown_until:
                            self._stats["suppressed_cooldown"] += 1
                        else:
                            self._stats["matched"] += 1
                            self._stats["matched_word" if kind == "word" else "matched_fragment"] += 1
                            self._fired = True
                            self._cooldown_until = now + self._cooldown
                    self._pending -= 1
                    self._busy_since = None
                    self._cond.notify_all()

    @staticmethod
    def _as_audio(samples: list[int]) -> Any:
        try:
            import numpy as np
        except ImportError:  # pragma: no cover — numpy есть в окружении backend
            return samples
        return np.asarray(samples, dtype=np.int16)
