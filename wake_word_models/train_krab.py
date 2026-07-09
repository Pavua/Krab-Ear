#!/usr/bin/env python3
"""train_krab.py — CLI-тренировка кастомной русской wake-word модели «Краб» (openWakeWord).

Спека: docs/superpowers/specs/2026-07-09-wake-model-krab-training-design.md (Волна 3a, T1).
Полная инструкция для человека: wake_word_models/README.md — читайте её в первую очередь.

Этапы (``--stage <name>`` или ``--all``, каждый resume-friendly — пропускает уже
готовое по маркер-файлам ``.done_<stage>.json``, ``--force`` для повтора):

    corpora    -- скачивает официальные ресурсы openWakeWord (fp-валидация,
                  опционально ACAV100M негативы, опционально MIT RIR).
    positives  -- синтезирует «Краб» / «эй, Краб» через Silero RU TTS.
    negatives  -- синтезирует нейтральную RU-речь + adversarial-слова/фразы.
    features   -- аугментация (openwakeword.data.augment_clips) + featurization
                  (openwakeword.utils.compute_features_from_generator) в .npy.
    train      -- openwakeword.train.Model.auto_train() + гейт по
                  max_fp_per_hour через _select_best_model().
    export     -- экспорт обученного чекпоинта в krab_ru.onnx.
    install    -- копия в {data_dir}/wake_word_models/ + постпроверка загрузки.

ВАЖНО (mlx-masking-класс урок из CLAUDE.md): этот файл ОБЯЗАН импортироваться
без побочных эффектов и БЕЗ torch/openwakeword/numpy в окружении — все тяжёлые
импорты (torch, openwakeword.*, numpy, scipy, huggingface_hub) сделаны ЛЕНИВО
внутри функций конкретных этапов. Юнит-тесты (KrabEar/tests/test_wake_training_
helpers_W3a.py) гоняются на ubuntu-parity py3.12 БЕЗ этого стека и обязаны
проходить.

Известные риски (подробнее — README «Известные риски»):
  - openwakeword.data / openwakeword.train требуют доп. пакеты, НЕ входящие в
    KrabEar/requirements.txt: pronouncing/audiomentations/speechbrain/mutagen/
    acoustics (обязательны даже для одного лишь augment_clips()) + torchinfo
    (openwakeword/train.py импортирует его безусловно на уровне модуля, хотя
    .summary() мы не вызываем). Проверено 2026-07-09 в .venv_krab_ear.
  - openwakeword.data.generate_adversarial_texts официально документирован как
    English-only (CMUdict/pronouncing); для кириллицы уходит в OOV-ветку с
    англоязычным DeepPhonemizer — используется best-effort, никогда не роняет
    пайплайн (см. _try_oww_adversarial_texts). Основная защита — вшитый RU-список.
  - openwakeword.train.Model.__init__ автоопределяет device только для CUDA,
    НЕ для MPS -- на Apple Silicon без явного оверрайда тренировка тихо
    останется на CPU (см. _resolve_torch_device).
  - torch >= 2.6 меняет дефолт torch.load(weights_only=True); наш чекпоинт --
    результат Model.save_model(), который сериализует ЦЕЛИКОМ объект nn.Module
    (не только его веса/state_dict), поэтому export-этап грузит его с explicit
    weights_only=False (это доверенный локальный файл, созданный этим же
    скриптом на предыдущем этапе, а не сторонний источник).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

# ============================================================================
# Константы
# ============================================================================

SCRIPT_DIR = Path(__file__).resolve().parent

DEFAULT_PHRASE = "Краб"
DEFAULT_SECONDARY_PHRASES: tuple[str, ...] = ("эй, Краб",)
DEFAULT_MODEL_NAME = "krab_ru"
DEFAULT_SPEAKERS: tuple[str, ...] = ("aidar", "baya", "kseniya", "xenia", "eugene")

# Совпадает с core/config.py Settings.TTS_SILERO_MODEL -- см. backend/tts_service.py.
SILERO_REPO = "snakers4/silero-models"
DEFAULT_SILERO_MODEL_ID = "v4_ru"

STAGES: tuple[str, ...] = (
    "corpora", "positives", "negatives", "features", "train", "export", "install",
)

# Официальные ресурсы openWakeWord -- сверены 2026-07-09 напрямую через
# dscripka/openWakeWord (notebooks/automatic_model_training.ipynb) и HuggingFace
# Hub API (репозитории публичные, не gated). См. README «Откуда взяты датасеты».
HF_FEATURES_REPO = "davidscripka/openwakeword_features"
HF_FEATURES_VALIDATION_FILE = "validation_set_features.npy"          # ~0.18 GB, ~11ч, ОБЯЗАТЕЛЕН
HF_FEATURES_ACAV100M_FILE = "openwakeword_features_ACAV100M_2000_hrs_16bit.npy"  # ~17.3 GB, опционален
HF_RIR_REPO = "davidscripka/MIT_environmental_impulse_responses"
HF_RIR_ALLOW_PATTERN = "16khz/*.wav"                                   # 271 файлов, несколько МБ

# Длительность fp-валидационного набора в часах -- совпадает и с описанием
# датасета ("~11 hours"), и с дефолтом openwakeword.train.Model._select_best_model
# (val_set_hrs=11.3), поэтому берём его как дефолт CLI-гейта.
DEFAULT_VAL_SET_HRS = 11.3

# Фонетически/орфографически близкие к «краб» слова и словоформы -- см. риск
# §3.1 спеки: "краба", "корабль", "прораб", "крабы"... Учитываем оглушение
# конечного «б»->«п» в русском: «краб» произносится как [крап], поэтому «крап»
# (рябь/крапинки) -- почти точный омофон и особенно ценный hard-negative.
# НИ ОДНО слово не должно совпадать с самой фразой -- проверяется
# validate_negative_words() при каждом использовании этого списка.
_ADVERSARIAL_WORDS_RU: tuple[str, ...] = (
    # Словоформы самого «краб» (падежи/число) -- НЕ должны триггерить детектор
    "краба", "крабу", "крабом", "крабе", "крабы", "крабов", "крабам", "крабами", "крабах",
    "крабик", "крабики", "крабчик",
    # Омофоны/рифмы за счёт оглушения конечного согласного и общего кластера "-раб"/"крап"
    "крап", "крапом", "крапа", "крапу",
    "трап", "трапа", "трапом", "трапу",
    "храп", "храпа", "храпом",
    "граб", "граба", "грабом",   # граб (дерево) -- почти минимальная пара с «краб»
    "раб", "раба", "рабом", "рабы",  # минимальная пара без начального «к»
    # Лексически близкие слова, явно названные в спеке
    "корабль", "кораблю", "кораблик", "кораблики",
    "прораб", "прорабу", "прорабы", "прорабом",
    "укроп", "укропа",
    # Дополнительные дистракторы того же фонетического кластера
    "скраб", "скрабом", "скрабы",
    "краш", "краше",
    "драп", "драпа",
    "крах", "краха",
)

# ~110+ нейтральных RU-предложений без корня «краб» -- см. build_neutral_sentences().
# Топики выбраны для разнообразия фонем/просодии: погода, работа, техника, еда,
# путешествия, семья, здоровье, спорт, город/новости, хобби, дом/быт, учёба.
_NEUTRAL_RU_SENTENCES: tuple[str, ...] = (
    # Погода
    "Сегодня отличная погода для прогулки в парке.",
    "Завтра обещают дождь и сильный ветер.",
    "Зимой в этом городе очень холодно и ветрено.",
    "Летом мы часто ездим на море отдыхать.",
    "Осенние листья уже начали желтеть и опадать.",
    "Весной природа просыпается после долгой зимы.",
    "На улице сегодня туман, видимость почти нулевая.",
    "Синоптики обещают на выходных ясное небо.",
    "Гроза началась внезапно, пришлось спрятаться под навесом.",
    "После дождя на небе появилась яркая радуга.",
    # Работа
    "Мне нужно закончить отчёт до конца недели.",
    "Совещание перенесли на два часа дня.",
    "Начальник попросил подготовить презентацию к среде.",
    "Коллеги обсуждают новый проект в переговорной.",
    "Зарплату обычно переводят десятого числа месяца.",
    "Резюме нужно отправить до пятницы вечером.",
    "На собеседовании спросили про опыт работы.",
    "Дедлайн по проекту сдвинули на следующий месяц.",
    "Бухгалтерия готовит квартальный финансовый отчёт.",
    "Новый сотрудник вышел на работу в понедельник.",
    # Техника
    "Новый смартфон получил улучшенную камеру и батарею.",
    "Программисты весь день исправляли ошибки в коде.",
    "Обновление операционной системы вышло на прошлой неделе.",
    "Беспроводные наушники быстро разряжаются на морозе.",
    "Искусственный интеллект меняет многие профессии.",
    "Ноутбук завис, пришлось перезагружать систему.",
    "Интернет в этом районе работает нестабильно.",
    "Разработчики выпустили важное обновление безопасности.",
    "Облачное хранилище позволяет синхронизировать файлы.",
    "Виртуальная реальность становится доступнее для пользователей.",
    # Еда
    "На ужин мы приготовили овощной суп и салат.",
    "Свежий хлеб пахнет удивительно вкусно по утрам.",
    "Рецепт этого пирога передавался в семье поколениями.",
    "На рынке продают спелые арбузы и дыни.",
    "Кофе с молоком помогает проснуться по утрам.",
    "Мама испекла яблочный пирог на десерт.",
    "В этом ресторане подают отличную домашнюю пасту.",
    "Чай с мёдом хорошо помогает при простуде.",
    "Овощи лучше готовить на пару, так полезнее.",
    "Шеф-повар предложил новое сезонное меню.",
    # Путешествия
    "Мы планируем поездку в горы на выходные.",
    "Билеты на поезд лучше покупать заранее.",
    "В аэропорту образовалась длинная очередь на регистрацию.",
    "Экскурсовод рассказал интересную историю о старом городе.",
    "Отель находится в пяти минутах от пляжа.",
    "Виза оформляется примерно две недели.",
    "Мы забронировали номер с видом на море.",
    "Дорога до соседнего города заняла три часа.",
    "Туристы фотографировали закат на набережной.",
    "Чемодан пришлось сдавать в багаж отдельно.",
    # Семья
    "Бабушка каждое воскресенье печёт пироги для внуков.",
    "Дети играли во дворе до самого вечера.",
    "Дедушка любит рассказывать истории о своей молодости.",
    "Сестра поступила в университет на медицинский факультет.",
    "Родители отвезли детей в школу на машине.",
    "Семья собралась за большим столом на праздник.",
    "Младший брат учится кататься на велосипеде.",
    "Тётя приехала в гости на все выходные.",
    "Свадьбу решили отпраздновать в загородном доме.",
    "Дядя подарил племяннику новую настольную игру.",
    # Здоровье
    "Врач посоветовал больше гулять на свежем воздухе.",
    "Утренняя зарядка помогает взбодриться перед работой.",
    "После болезни важно постепенно возвращаться к нагрузкам.",
    "Стоматолог порекомендовал посещать его дважды в год.",
    "Здоровый сон крайне важен для хорошего самочувствия.",
    "Аптека на углу работает круглосуточно.",
    "Регулярные тренировки укрепляют сердце и мышцы.",
    "Врач измерил давление и назначил анализы.",
    "Витамины лучше принимать после консультации с врачом.",
    "Массаж помогает снять напряжение в спине.",
    # Спорт
    "Футбольный матч закончился со счётом два один.",
    "Тренер попросил команду прибавить скорость на тренировке.",
    "Марафон в этом году собрал рекордное число участников.",
    "Хоккеисты вышли на лёд под аплодисменты зрителей.",
    "Теннисист выиграл финал в трёх сетах.",
    "Бассейн открыт для посещения с раннего утра.",
    "Велосипедная прогулка заняла почти два часа.",
    "Баскетбольная команда готовится к важному матчу.",
    "Лыжники стартовали при минусовой температуре.",
    "Боксёр тренируется в зале шесть дней в неделю.",
    # Город / новости
    "В центре города открыли новый пешеходный мост.",
    "Мэрия объявила о ремонте главной улицы.",
    "Библиотеку недавно переехали в новое здание.",
    "На площади проходит ежегодная городская ярмарка.",
    "Общественный транспорт с завтрашнего дня подорожает.",
    "Парк отремонтировали и открыли для посетителей.",
    "В музее открылась выставка современного искусства.",
    "Строительство нового квартала завершится к осени.",
    "Городские власти обещают расширить велосипедные дорожки.",
    "На вокзале установили новые электронные табло.",
    # Хобби / музыка
    "Вечером мы слушали любимую музыку и читали книги.",
    "Гитарист исполнил несколько известных мелодий на бис.",
    "Художник работал над новой картиной несколько месяцев.",
    "Оркестр репетировал новую программу перед концертом.",
    "Фотограф снимал закат на берегу озера.",
    "Книжный клуб собирается каждый второй четверг.",
    "Певица выпустила новый альбом в конце года.",
    "Театр представил премьеру спектакля по классике.",
    "Кинотеатр показывает новый фильм с завтрашнего дня.",
    "Хор репетирует в актовом зале по вторникам.",
    # Дом / быт / учёба
    "Соседи затеяли ремонт квартиры на верхнем этаже.",
    "Кот уснул на подоконнике под тёплым солнцем.",
    "В саду расцвели первые весенние тюльпаны.",
    "Собака радостно встречала хозяина у порога.",
    "На балконе выросли свежие томаты и зелень.",
    "Стиральная машина сломалась в самый неподходящий момент.",
    "Соседский двор украсили гирляндами к празднику.",
    "Почтальон принёс письмо и несколько журналов.",
    "Вечером во дворе играли дети из соседних домов.",
    "Электрик быстро починил проводку на кухне.",
    "Учитель объяснил новую тему очень понятно.",
    "Студенты готовились к экзамену всю ночь.",
    "Библиотекарь помогла найти нужную книгу.",
    "Урок музыки перенесли на другой день.",
)

logger = logging.getLogger("krab_wake_training")


# ============================================================================
# Resume-маркеры (чистые, только filesystem — без torch/сети)
# ============================================================================

def marker_path(stage_dir: Path, stage: str) -> Path:
    """Путь к resume-маркеру этапа внутри его рабочей директории."""
    return Path(stage_dir) / f".done_{stage}.json"


def is_stage_done(stage_dir: Path, stage: str) -> bool:
    """True если этап уже помечен завершённым (наличие маркер-файла)."""
    return marker_path(stage_dir, stage).exists()


def write_marker(stage_dir: Path, stage: str, meta: dict[str, Any]) -> Path:
    """Пишет resume-маркер этапа с метаданными (счётчики, пути, таймстемп)."""
    stage_dir = Path(stage_dir)
    stage_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "stage": stage,
        "completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        **meta,
    }
    path = marker_path(stage_dir, stage)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def read_marker(stage_dir: Path, stage: str) -> dict[str, Any] | None:
    """Читает resume-маркер, если он есть и валиден; иначе None (graceful)."""
    path = marker_path(stage_dir, stage)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("read_marker: не удалось прочитать %s: %s", path, exc)
        return None


# ============================================================================
# Чистые хелперы — юнит-тестируемые без сети/torch/openwakeword
# ============================================================================

def normalize_phrase_for_compare(text: str) -> str:
    """Нормализует текст для сравнения на точное совпадение: нижний регистр,
    схлопнутые пробелы, без краевых пробелов."""
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def build_positive_phrases(phrase: str, secondary_phrases: Sequence[str]) -> list[str]:
    """Возвращает список фраз-позитивов (основная форма + вторичные), без дублей.

    Raises:
        ValueError: если ``phrase`` пуста.
    """
    phrase = (phrase or "").strip()
    if not phrase:
        raise ValueError("phrase не может быть пустой строкой")
    result = [phrase]
    seen = {normalize_phrase_for_compare(phrase)}
    for p in secondary_phrases:
        p = (p or "").strip()
        key = normalize_phrase_for_compare(p)
        if p and key not in seen:
            result.append(p)
            seen.add(key)
    return result


def validate_negative_words(words: Iterable[str], forbidden_phrases: Sequence[str]) -> None:
    """Гарантирует, что ни один негативный текст НЕ совпадает точно (без учёта
    регистра/пробелов) ни с одной из ``forbidden_phrases``.

    Негатив, случайно равный целевой фразе, испортил бы гейт (модель бы училась
    НЕ реагировать на саму фразу). Используется для обоих источников негативов
    (adversarial-слова и нейтральные предложения) как defense-in-depth.

    Raises:
        ValueError: при первом найденном точном совпадении.
    """
    forbidden = {normalize_phrase_for_compare(p) for p in forbidden_phrases}
    for w in words:
        if normalize_phrase_for_compare(w) in forbidden:
            raise ValueError(
                f"Негативный текст {w!r} точно совпадает с целевой фразой -- "
                "удалите его из списка адверсариалов/нейтральных предложений"
            )


def build_adversarial_words(extra: Iterable[str] | None = None,
                            forbidden_phrases: Sequence[str] | None = None) -> list[str]:
    """Вшитый список RU-слов, фонетически/орфографически близких к «краб»
    (см. ``_ADVERSARIAL_WORDS_RU``), опционально дополненный ``extra``
    (напр. выводом ``openwakeword.data.generate_adversarial_texts``).

    Всегда валидирует итоговый список через ``validate_negative_words``.
    """
    words = list(_ADVERSARIAL_WORDS_RU)
    if extra:
        seen = {normalize_phrase_for_compare(w) for w in words}
        for w in extra:
            key = normalize_phrase_for_compare(w)
            if w and key and key not in seen:
                words.append(w)
                seen.add(key)
    validate_negative_words(words, forbidden_phrases or [DEFAULT_PHRASE])
    return words


def build_neutral_sentences(target_count: int = 100,
                            forbidden_phrases: Sequence[str] | None = None) -> list[str]:
    """Возвращает нейтральные RU-предложения без корня «краб» для негативов.

    Базовый вшитый список (``_NEUTRAL_RU_SENTENCES``, >100 предложений) сначала
    валидируется. Если ``target_count`` больше длины базового списка -- список
    расширяется детерминированными вариациями (вводные слова), сохраняя
    уникальность и отсутствие целевой фразы.
    """
    base = list(_NEUTRAL_RU_SENTENCES)
    validate_negative_words(base, forbidden_phrases or [DEFAULT_PHRASE])

    if target_count <= len(base):
        return base[: max(target_count, 1)]

    prefixes = ("Кстати, ", "Между прочим, ", "Как известно, ", "")
    extended = list(base)
    seen = {normalize_phrase_for_compare(s) for s in base}
    idx = 0
    # Верхний предел итераций -- защита от бесконечного цикла при крошечном base.
    max_attempts = max(target_count * 4, 40)
    while len(extended) < target_count and idx < max_attempts:
        prefix = prefixes[idx % len(prefixes)]
        base_sentence = base[idx % len(base)]
        if prefix and base_sentence:
            candidate = f"{prefix}{base_sentence[0].lower()}{base_sentence[1:]}"
        else:
            candidate = base_sentence
        key = normalize_phrase_for_compare(candidate)
        if key not in seen:
            extended.append(candidate)
            seen.add(key)
        idx += 1
    return extended


def _escape_xml(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def build_ssml(text: str, rate_pct: int | None, pitch_pct: int | None) -> str:
    """Строит ``<speak><prosody rate="..%" pitch="..%">text</prosody></speak>``.

    Best-effort: не все версии Silero-хаба поддерживают ``ssml_text=`` в
    свободной функции ``apply_tts`` (см. риски в докстроке модуля) -- вызывающий
    код (``_synthesize_one``) обязан graceful-деградировать на обычный текст.
    """
    attrs = []
    if rate_pct is not None:
        sign = "+" if rate_pct >= 0 else ""
        attrs.append(f'rate="{sign}{rate_pct}%"')
    if pitch_pct is not None:
        sign = "+" if pitch_pct >= 0 else ""
        attrs.append(f'pitch="{sign}{pitch_pct}%"')
    attr_str = (" " + " ".join(attrs)) if attrs else ""
    return f"<speak><prosody{attr_str}>{_escape_xml(text)}</prosody></speak>"


def parse_percent_list(raw: str) -> list[int]:
    """``'-10,0,10'`` -> ``[-10, 0, 10]``. Пустая/None строка -> ``[]``.

    Raises:
        ValueError: если один из элементов не целое число.
    """
    raw = (raw or "").strip()
    if not raw:
        return []
    out: list[int] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if chunk:
            out.append(int(chunk))
    return out


def iter_prosody_grid(rates: Sequence[int], pitches: Sequence[int]) -> list[tuple[int | None, int | None]]:
    """Декартово произведение rate x pitch вариаций для SSML.

    Пустой список для одной из осей означает «без вариации по этой оси»
    (``None``). Обе пустые -> ``[(None, None)]`` (совсем без просодии).
    """
    r: Sequence[int | None] = list(rates) or [None]
    p: Sequence[int | None] = list(pitches) or [None]
    return [(ri, pi) for ri in r for pi in p]


def build_synthesis_plan(
    phrases: Sequence[str],
    speakers: Sequence[str],
    prosody_grid: Sequence[tuple[int | None, int | None]],
    seed: int = 13,
) -> list[tuple[str, str, int | None, int | None]]:
    """Детерминированный (по ``seed``) план синтеза: полное декартово
    произведение (фраза, спикер, rate%, pitch%), перемешанное для равномерного
    распределения при последующем циклическом усечении до целевого count.

    Raises:
        ValueError: если ``phrases`` или ``speakers`` пусты.
    """
    if not phrases:
        raise ValueError("phrases не должен быть пустым")
    if not speakers:
        raise ValueError("speakers не должен быть пустым")
    grid = list(prosody_grid) or [(None, None)]
    combos = [(p, s, r, pi) for p in phrases for s in speakers for (r, pi) in grid]
    rnd = random.Random(seed)
    rnd.shuffle(combos)
    return combos


def deterministic_train_test_split(
    items: Sequence[Any], test_ratio: float = 0.1, seed: int = 13,
) -> tuple[list[Any], list[Any]]:
    """Детерминированный (по ``seed``) train/test сплит без sklearn/numpy.

    Используется и для комбинаций синтеза (избегаем train/test-«утечки» через
    идентичные raw-параметры синтеза), и для списков путей WAV-файлов.

    Raises:
        ValueError: если ``test_ratio`` вне ``[0, 1)``.
    """
    if not 0.0 <= test_ratio < 1.0:
        raise ValueError("test_ratio должен быть в диапазоне [0, 1)")
    items = list(items)
    rnd = random.Random(seed)
    shuffled = items[:]
    rnd.shuffle(shuffled)
    n_test = int(round(len(shuffled) * test_ratio))
    if items and test_ratio > 0:
        n_test = max(1, n_test)
    test = shuffled[:n_test]
    train = shuffled[n_test:]
    return train, test


def compute_total_length_samples(
    durations: Sequence[int], min_len: int = 32000, buffer: int = 12000,
) -> int:
    """Повторяет расчёт ``total_length`` из ``openwakeword.train`` (``__main__``):
    медиана длительностей (в сэмплах) + буфер, округление до тысяч сэмплов,
    минимум ``min_len`` (32000 = 2с @ 16kHz), приведение к ``min_len`` если
    результат очень близко к нему (в пределах 4000 сэмплов).

    Пустой ``durations`` -> ``min_len``.
    """
    if not durations:
        return min_len
    median = statistics.median(durations)
    total = int(round(median / 1000) * 1000) + buffer
    if total < min_len:
        total = min_len
    elif abs(total - min_len) <= 4000:
        total = min_len
    return total


def _apply_limit(target: int, limit: int | None) -> int:
    """Применяет глобальный ``--limit`` (если задан) как верхнюю границу count."""
    if limit is None:
        return target
    return min(target, max(0, limit))


def _glob_wavs(directory: Path | str | None) -> list[str]:
    """Список .wav файлов в директории (рекурсивно), либо ``[]`` если директория
    не задана / не существует -- graceful для опциональных корпусов (RIR/фон)."""
    if not directory:
        return []
    d = Path(directory)
    if not d.exists():
        return []
    return sorted(str(p) for p in d.rglob("*.wav"))


# ============================================================================
# Пути проекта
# ============================================================================

@dataclass
class ProjectPaths:
    """Рабочие директории тренировочного пайплайна (все, кроме ``root``,
    гитигнорены -- см. wake_word_models/.gitignore)."""

    root: Path
    corpora: Path
    positives: Path
    negatives: Path
    features: Path
    artifacts: Path

    @classmethod
    def from_work_dir(cls, work_dir: Path) -> "ProjectPaths":
        work_dir = Path(work_dir)
        return cls(
            root=work_dir,
            corpora=work_dir / "corpora",
            positives=work_dir / "positives",
            negatives=work_dir / "negatives",
            features=work_dir / "features",
            artifacts=work_dir / "artifacts",
        )

    def report_path(self, model_name: str) -> Path:
        return self.root / f"report_{model_name}.md"


# ============================================================================
# CLI
# ============================================================================

def build_arg_parser() -> argparse.ArgumentParser:
    """Строит argparse-парсер со всеми этапами/флагами. Чистая функция --
    вызывается и в main(), и напрямую в юнит-тестах."""
    parser = argparse.ArgumentParser(
        prog="train_krab.py",
        description=(
            "Тренировочный CLI кастомной русской wake-word модели «Краб» "
            "(openWakeWord). Этапы: corpora -> positives -> negatives -> "
            "features -> train -> export -> install. См. README.md рядом с этим "
            "файлом для полной инструкции и пререквизитов."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    stage_group = parser.add_mutually_exclusive_group()
    stage_group.add_argument(
        "--stage", choices=STAGES, default=None, help="Запустить один этап",
    )
    stage_group.add_argument(
        "--all", action="store_true", help="Запустить все этапы по порядку",
    )

    common = parser.add_argument_group("Общие")
    common.add_argument(
        "--work-dir", default=None,
        help="Корень рабочих директорий (по умолчанию -- каталог этого скрипта)",
    )
    common.add_argument("--phrase", default=DEFAULT_PHRASE, help="Целевая фраза")
    common.add_argument(
        "--secondary-phrase", action="append", default=None,
        help="Доп. форма фразы (флаг можно повторять); по умолчанию 'эй, Краб'",
    )
    common.add_argument("--model-name", default=DEFAULT_MODEL_NAME, help="Имя выходной модели")
    common.add_argument("--seed", type=int, default=13, help="Seed для детерминизма выборок/сплитов")
    common.add_argument(
        "--force", action="store_true",
        help="Игнорировать resume-маркеры и повторить этап(ы) заново",
    )
    common.add_argument(
        "--limit", type=int, default=None,
        help="Ограничить кол-во генерируемых клипов (smoke/debug прогон)",
    )
    common.add_argument(
        "--data-dir",
        default=str(Path.home() / "Library" / "Application Support" / "KrabEar"),
        help="Data dir установки backend'а (используется этапом install)",
    )
    common.add_argument("-v", "--verbose", action="store_true", help="DEBUG-логирование")

    corpora_g = parser.add_argument_group("corpora")
    corpora_g.add_argument("--features-repo", default=HF_FEATURES_REPO,
                           help="HF dataset repo с предвычисленными negative features")
    corpora_g.add_argument("--fp-validation-file", default=HF_FEATURES_VALIDATION_FILE,
                           help="Файл fp-валидации (~0.18 GB, обязателен для гейта)")
    corpora_g.add_argument("--acav100m-file", default=HF_FEATURES_ACAV100M_FILE,
                           help="Файл общего negative-корпуса ACAV100M (~17.3 GB)")
    corpora_g.add_argument(
        "--fetch-acav100m", action="store_true",
        help="Скачать общий негативный корпус ACAV100M (~17.3 GB, опционально, тяжёлый)",
    )
    corpora_g.add_argument("--rir-repo", default=HF_RIR_REPO, help="HF dataset repo с RIR")
    corpora_g.add_argument("--rir-pattern", default=HF_RIR_ALLOW_PATTERN,
                           help="allow_patterns для snapshot_download RIR")
    corpora_g.add_argument(
        "--skip-rir", action="store_true",
        help="Не скачивать RIR (реверберация в features будет отключена)",
    )

    pos_g = parser.add_argument_group("positives")
    pos_g.add_argument("--speakers", default=",".join(DEFAULT_SPEAKERS),
                       help="Silero-спикеры через запятую")
    pos_g.add_argument("--silero-model-id", default=DEFAULT_SILERO_MODEL_ID,
                       help="Silero speaker-пакет (аргумент torch.hub.load)")
    pos_g.add_argument("--positives-count", type=int, default=4000,
                       help="Целевое кол-во позитивных клипов (train+test)")
    pos_g.add_argument("--rate-variants", default="-15,0,15",
                       help="SSML rate%% через запятую, пустая строка = без вариации")
    pos_g.add_argument("--pitch-variants", default="-10,0,10",
                       help="SSML pitch%% через запятую, пустая строка = без вариации")
    pos_g.add_argument("--test-ratio", type=float, default=0.1,
                       help="Доля test для positives/negatives сплитов")

    neg_g = parser.add_argument_group("negatives")
    neg_g.add_argument("--neutral-target-count", type=int, default=1200,
                       help="Целевое кол-во нейтральных RU-негативов")
    neg_g.add_argument("--adversarial-target-count", type=int, default=1200,
                       help="Целевое кол-во adversarial-негативов")
    neg_g.add_argument(
        "--use-oww-adversarial-texts", action=argparse.BooleanOptionalAction, default=True,
        help=(
            "Доп. вызов openwakeword.data.generate_adversarial_texts (best-effort; "
            "документирован как English-only, для кириллицы может не сработать -- "
            "см. риски в докстроке модуля). Отключить: --no-use-oww-adversarial-texts"
        ),
    )

    feat_g = parser.add_argument_group("features")
    feat_g.add_argument("--augmentation-rounds", type=int, default=2,
                        help="Сколько раз аугментировать каждый raw-клип")
    feat_g.add_argument("--augmentation-batch-size", type=int, default=128)
    feat_g.add_argument("--feature-ncpu", type=int, default=max(1, (os.cpu_count() or 2) // 2))
    feat_g.add_argument("--background-dir", default=None,
                        help="Опц. каталог фоновых WAV для AddBackgroundNoise (не автоскачивается)")
    feat_g.add_argument("--total-length", type=int, default=None,
                        help="Оверрайд длины клипа в сэмплах (иначе авто из позитивов)")

    train_g = parser.add_argument_group("train")
    train_g.add_argument("--steps", type=int, default=50000)
    train_g.add_argument("--max-negative-weight", type=int, default=1000)
    train_g.add_argument("--target-fp-per-hour", type=float, default=0.2,
                         help="Внутренняя адаптивная цель auto_train")
    train_g.add_argument("--max-fp-per-hour", type=float, default=1.0,
                         help="Финальный офлайн-гейт (роадмап: <= 1.0)")
    train_g.add_argument("--min-recall", type=float, default=0.20,
                         help="Минимальный recall кандидата под гейтом")
    train_g.add_argument("--val-set-hrs", type=float, default=DEFAULT_VAL_SET_HRS,
                         help="Длительность fp-валидационного набора в часах")
    train_g.add_argument("--model-type", choices=("dnn", "rnn"), default="dnn",
                         help="rnn (LSTM) может не иметь полной поддержки MPS")
    train_g.add_argument("--layer-size", type=int, default=128)
    train_g.add_argument("--device", choices=("auto", "cpu", "mps", "cuda"), default="auto")
    train_g.add_argument(
        "--negatives-dir", default=None,
        help=(
            "Путь к предвычисленному общему негативному .npy (напр. ACAV100M "
            "features) -- переопределяет corpora/<acav100m-file>; наличие "
            "проверяется, при отсутствии train использует только RU-синтетику"
        ),
    )

    install_g = parser.add_argument_group("install")
    install_g.add_argument(
        "--skip-load-check", action="store_true",
        help="Не проверять загрузку установленной модели через openwakeword.model.Model",
    )

    return parser


def configure_cli_logging(verbose: bool) -> None:
    """Настраивает root-логгер для CLI (stdout, человекочитаемый формат).

    Вызывается ТОЛЬКО из main() -- импорт модуля не имеет побочных эффектов.
    """
    level = logging.DEBUG if verbose else logging.INFO
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)


# ============================================================================
# Этап: corpora
# ============================================================================

def stage_corpora(args: argparse.Namespace, paths: ProjectPaths) -> None:
    """Скачивает официальные ресурсы openWakeWord для тренировки.

    Источники (сверены 2026-07-09 напрямую в dscripka/openWakeWord и на
    HuggingFace Hub API -- см. README «Откуда взяты датасеты»):
      - validation_set_features.npy (~0.18 GB, ~11ч) -- ОБЯЗАТЕЛЕН для
        fp/час-гейта (used as false_positive_validate_data в _select_best_model).
      - openwakeword_features_ACAV100M_2000_hrs_16bit.npy (~17.3 GB) -- ОПЦИОНАЛЕН
        (--fetch-acav100m), общий "negative" класс; без него train использует
        только RU-синтетику как негативы (см. риск #1 в спеке -- гейт слабее).
      - MIT room impulse responses (271 файл, 16 kHz wav, несколько МБ) --
        ОПЦИОНАЛЕН, используется augment_clips() для реверберации.

    НЕ качает background noise (AudioSet/FMA) -- augment_clips() штатно
    деградирует без него (AddBackgroundNoise просто не добавляется в Compose,
    см. openwakeword/data.py). См. README, как подключить свой каталог через
    --background-dir на этапе features.
    """
    if is_stage_done(paths.corpora, "corpora") and not args.force:
        logger.info(
            "corpora: уже выполнено (%s), пропуск (--force для повтора)",
            marker_path(paths.corpora, "corpora"),
        )
        return

    paths.corpora.mkdir(parents=True, exist_ok=True)

    try:
        from huggingface_hub import hf_hub_download, snapshot_download
    except ImportError as exc:
        raise RuntimeError(
            "huggingface_hub не установлен. Выполните: pip install huggingface_hub"
        ) from exc

    downloaded: dict[str, Any] = {}

    # 1. FP-валидация -- обязательна для train-этапа
    val_path = paths.corpora / args.fp_validation_file
    if val_path.exists() and not args.force:
        logger.info("corpora: %s уже скачан, пропуск", val_path.name)
    else:
        logger.info(
            "corpora: скачиваю %s/%s (~0.18 GB) -- fp/час валидация",
            args.features_repo, args.fp_validation_file,
        )
        fetched = hf_hub_download(
            repo_id=args.features_repo, filename=args.fp_validation_file,
            repo_type="dataset", local_dir=str(paths.corpora),
        )
        val_path = Path(fetched)
    downloaded["fp_validation_path"] = str(val_path)

    # 2. ACAV100M -- опционален, тяжёлый (~17.3 GB)
    if args.fetch_acav100m:
        acav_path = paths.corpora / args.acav100m_file
        if acav_path.exists() and not args.force:
            logger.info("corpora: %s уже скачан, пропуск", acav_path.name)
        else:
            logger.warning(
                "corpora: скачиваю %s (~17.3 GB) -- убедитесь, что есть место на "
                "диске, это займёт время", args.acav100m_file,
            )
            fetched = hf_hub_download(
                repo_id=args.features_repo, filename=args.acav100m_file,
                repo_type="dataset", local_dir=str(paths.corpora),
            )
            acav_path = Path(fetched)
        downloaded["acav100m_path"] = str(acav_path)
    else:
        logger.info(
            "corpora: --fetch-acav100m не задан -- общий негативный корпус "
            "(~17.3 GB) пропущен, train будет использовать только RU-синтетику "
            "как негативы (см. --negatives-dir)"
        )

    # 3. MIT RIR -- опционален
    if not args.skip_rir:
        rir_dir = paths.corpora / "rir"
        if rir_dir.exists() and any(rir_dir.rglob("*.wav")) and not args.force:
            logger.info("corpora: RIR уже скачаны (%s), пропуск", rir_dir)
        else:
            try:
                logger.info("corpora: скачиваю MIT RIR (%s, ~271 файлов)", args.rir_repo)
                snapshot_download(
                    repo_id=args.rir_repo, repo_type="dataset",
                    allow_patterns=[args.rir_pattern], local_dir=str(rir_dir),
                )
            except Exception as exc:  # noqa: BLE001 -- не блокируем пайплайн без RIR
                logger.warning(
                    "corpora: не удалось скачать RIR (%s) -- augment_clips() "
                    "деградирует без реверберации, качество гейта может быть "
                    "ниже. Проверьте репозиторий/URL на месте (риск #1 в спеке).",
                    exc,
                )
        downloaded["rir_dir"] = str(rir_dir)
    else:
        logger.info("corpora: --skip-rir задан, RIR пропущены")

    write_marker(paths.corpora, "corpora", downloaded)
    logger.info("corpora: этап завершён")


# ============================================================================
# Этап: positives
# ============================================================================

def _load_silero_tts(model_id: str) -> dict[str, Any]:
    """Ленивая загрузка Silero TTS через torch.hub (self-contained, НЕ зависит
    от запущенного backend'а -- см. решение §3.2 спеки).

    Тот же repo_or_dir/паттерн распаковки, что и backend/tts_service.py::_load_silero,
    для консистентности с production-кодом (хотя сам скрипт независим от него).
    """
    import torch  # noqa: F401 -- проверка доступности до torch.hub.load
    device = torch.device("cpu")
    model, symbols, sample_rate, _example_text, apply_tts = torch.hub.load(
        repo_or_dir=SILERO_REPO,
        model="silero_tts",
        language="ru",
        speaker=model_id,
        trust_repo=True,
    )
    model = model.to(device)
    return {
        "model": model,
        "symbols": symbols,
        "sample_rate": sample_rate,
        "apply_tts": apply_tts,
        "device": device,
    }


_SSML_UNSUPPORTED_WARNED = False


def _synthesize_one(
    silero_ctx: dict[str, Any], text: str, speaker: str,
    rate_pct: int | None = None, pitch_pct: int | None = None,
):
    """Синтезирует один клип. При заданных rate/pitch пробует SSML
    (best-effort); при несовместимости текущей версии Silero-хаба -- graceful
    fallback на обычный текст (см. риски в докстроке модуля)."""
    global _SSML_UNSUPPORTED_WARNED
    import numpy as np

    apply_tts = silero_ctx["apply_tts"]
    kwargs = dict(
        model=silero_ctx["model"], sample_rate=silero_ctx["sample_rate"],
        symbols=silero_ctx["symbols"], device=silero_ctx["device"], speaker=speaker,
    )
    audio_tensor = None
    if rate_pct is not None or pitch_pct is not None:
        ssml = build_ssml(text, rate_pct, pitch_pct)
        try:
            audio_tensor = apply_tts(ssml_text=ssml, **kwargs)
        except TypeError:
            if not _SSML_UNSUPPORTED_WARNED:
                logger.warning(
                    "positives: apply_tts этой версии Silero не поддерживает "
                    "ssml_text= -- дальше синтез без rate/pitch вариаций "
                    "(fallback на обычный текст, см. README «Известные риски»)"
                )
                _SSML_UNSUPPORTED_WARNED = True
        except Exception as exc:  # noqa: BLE001 -- SSML best-effort, не роняем клип
            logger.debug("positives: SSML-синтез не удался (%s) -- fallback на texts=[]", exc)

    if audio_tensor is None:
        audio_tensor = apply_tts(texts=[text], **kwargs)

    samples = audio_tensor.squeeze().cpu().numpy()
    pcm = (samples * 32767).clip(-32768, 32767).astype(np.int16)
    return pcm, silero_ctx["sample_rate"]


def _write_wav(path: Path, pcm: Any, sample_rate: int) -> None:
    import wave
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm.tobytes())


def _synthesize_batch(
    silero_ctx: dict[str, Any],
    combos: Sequence[tuple[str, str, int | None, int | None]],
    out_dir: Path,
    count: int,
    prefix: str,
) -> int:
    """Циклически проходит по ``combos`` (round-robin), синтезируя ``count``
    клипов в ``out_dir``. Единичные сбои синтеза логируются и пропускаются --
    не роняют весь батч (сетевой/GPU flake на одном клипе не должен стоить
    часов прогона)."""
    import itertools

    if not combos or count <= 0:
        return 0

    written = 0
    for idx, (text, speaker, rate, pitch) in zip(range(count), itertools.cycle(combos)):
        try:
            pcm, sr = _synthesize_one(silero_ctx, text, speaker, rate, pitch)
        except Exception as exc:  # noqa: BLE001 -- один плохой клип не должен рушить батч
            logger.warning("synth: клип %d (%r/%s) не удался: %s", idx, text, speaker, exc)
            continue
        fname = f"{prefix}_{idx:05d}_{speaker}.wav"
        _write_wav(out_dir / fname, pcm, sr)
        written += 1
        if written % 200 == 0:
            logger.info("%s: %d/%d клипов", out_dir.name, written, count)
    return written


def stage_positives(args: argparse.Namespace, paths: ProjectPaths) -> None:
    """Синтезирует позитивные клипы «Краб» / «эй, Краб» через Silero RU TTS.

    5 спикеров (aidar/baya/kseniya/xenia/eugene по умолчанию) x SSML rate/pitch
    вариации x 2 формы фразы. Комбинации синтеза заранее разбиваются на train/
    test ДО циклического заполнения, чтобы избежать train/test-утечки через
    идентичные raw-параметры синтеза (аугментация в stage_features добавляет
    основную практическую вариативность поверх этого).
    """
    if is_stage_done(paths.positives, "positives") and not args.force:
        logger.info(
            "positives: уже выполнено (%s), пропуск (--force для повтора)",
            marker_path(paths.positives, "positives"),
        )
        return

    train_dir = paths.positives / "train"
    test_dir = paths.positives / "test"
    train_dir.mkdir(parents=True, exist_ok=True)
    test_dir.mkdir(parents=True, exist_ok=True)

    phrases = build_positive_phrases(args.phrase, args.secondary_phrase)
    speakers = [s.strip() for s in args.speakers.split(",") if s.strip()]
    rates = parse_percent_list(args.rate_variants)
    pitches = parse_percent_list(args.pitch_variants)
    prosody_grid = iter_prosody_grid(rates, pitches)

    target = _apply_limit(args.positives_count, args.limit)
    test_target = max(1, round(target * args.test_ratio))
    train_target = max(1, target - test_target)

    combos = build_synthesis_plan(phrases, speakers, prosody_grid, seed=args.seed)
    combos_train, combos_test = deterministic_train_test_split(
        combos, test_ratio=args.test_ratio, seed=args.seed,
    )
    combos_train = combos_train or combos[:1]
    combos_test = combos_test or combos[-1:]

    logger.info(
        "positives: %d фраз x %d спикеров x %d просодий = %d комбинаций "
        "(train=%d combos / test=%d combos)",
        len(phrases), len(speakers), len(prosody_grid), len(combos),
        len(combos_train), len(combos_test),
    )

    silero_ctx = _load_silero_tts(args.silero_model_id)
    written_train = _synthesize_batch(silero_ctx, combos_train, train_dir, train_target, prefix="pos")
    written_test = _synthesize_batch(silero_ctx, combos_test, test_dir, test_target, prefix="pos")

    write_marker(paths.positives, "positives", {
        "phrases": phrases, "speakers": speakers,
        "train_count": written_train, "test_count": written_test,
    })
    logger.info("positives: готово train=%d test=%d", written_train, written_test)


# ============================================================================
# Этап: negatives
# ============================================================================

def _try_oww_adversarial_texts(phrase: str, n: int) -> list[str]:
    """Best-effort вызов ``openwakeword.data.generate_adversarial_texts``.

    ВНИМАНИЕ: эта функция официально документирована в openwakeword/data.py как
    "Currently only works for english texts" -- использует CMUdict (пакет
    ``pronouncing``) для фонем; для кириллического слова CMUdict вернёт [] для
    каждого слова, что уводит в OOV-ветку с англоязычной DeepPhonemizer-моделью
    (скачивается с S3 при первом вызове). Результат для «краб» непредсказуем:
    пустой список / нерелевантные фонемы / исключение. Управляется явным флагом
    ``--use-oww-adversarial-texts`` (default True) и НИКОГДА не роняет пайплайн
    -- любая ошибка логируется, основная защита остаётся вшитый RU-список
    (см. build_adversarial_words / _ADVERSARIAL_WORDS_RU).
    """
    try:
        from openwakeword.data import generate_adversarial_texts
    except ImportError as exc:
        logger.warning(
            "negatives: openwakeword.data недоступен (%s) -- пропускаю "
            "generate_adversarial_texts. Нужны доп. пакеты: pip install "
            "pronouncing audiomentations speechbrain mutagen acoustics "
            "(см. README «Пререквизиты»)", exc,
        )
        return []
    try:
        texts = generate_adversarial_texts(
            input_text=phrase, N=max(1, n), include_partial_phrase=1.0, include_input_words=0.2,
        )
        logger.info("negatives: generate_adversarial_texts вернул %d вариантов для %r", len(texts), phrase)
        return texts
    except Exception as exc:  # noqa: BLE001 -- ожидаемо нестабильно для RU, см. докстроку
        logger.warning(
            "negatives: generate_adversarial_texts провалился для %r (%s) -- "
            "известное ограничение (EN-only API, см. openwakeword/data.py), "
            "использую только вшитый RU-список адверсариалов", phrase, exc,
        )
        return []


def stage_negatives(args: argparse.Namespace, paths: ProjectPaths) -> None:
    """Синтезирует негативные клипы: (а) нейтральная RU-речь без корня «краб»,
    (б) adversarial-слова/фразы, фонетически близкие к «краб» (см. решение
    §3.3 спеки)."""
    if is_stage_done(paths.negatives, "negatives") and not args.force:
        logger.info(
            "negatives: уже выполнено (%s), пропуск (--force для повтора)",
            marker_path(paths.negatives, "negatives"),
        )
        return

    ru_dir = paths.negatives / "ru_synthetic"
    adv_dir = paths.negatives / "adversarial"
    ru_dir.mkdir(parents=True, exist_ok=True)
    adv_dir.mkdir(parents=True, exist_ok=True)

    phrase_all = build_positive_phrases(args.phrase, args.secondary_phrase)
    neutral_target = _apply_limit(args.neutral_target_count, args.limit)
    adversarial_target = _apply_limit(args.adversarial_target_count, args.limit)
    speakers = [s.strip() for s in args.speakers.split(",") if s.strip()]

    silero_ctx = _load_silero_tts(args.silero_model_id)

    # (а) нейтральная RU-синтетика
    neutral_sentences = build_neutral_sentences(
        target_count=max(neutral_target, 100), forbidden_phrases=phrase_all,
    )
    combos_ru = build_synthesis_plan(neutral_sentences, speakers, [(None, None)], seed=args.seed)
    written_ru = _synthesize_batch(silero_ctx, combos_ru, ru_dir, neutral_target, prefix="neg_ru")

    # (б) adversarial (вшитый список + опциональный best-effort oww-генератор)
    extra_adversarial: list[str] = []
    if args.use_oww_adversarial_texts:
        extra_adversarial = _try_oww_adversarial_texts(args.phrase, n=max(1, adversarial_target // 2))
    adversarial_words = build_adversarial_words(extra_adversarial, forbidden_phrases=phrase_all)
    combos_adv = build_synthesis_plan(adversarial_words, speakers, [(None, None)], seed=args.seed + 1)
    written_adv = _synthesize_batch(silero_ctx, combos_adv, adv_dir, adversarial_target, prefix="neg_adv")

    write_marker(paths.negatives, "negatives", {
        "ru_count": written_ru, "adversarial_count": written_adv,
        "oww_adversarial_texts_used": len(extra_adversarial),
    })
    logger.info("negatives: готово ru=%d adversarial=%d (oww-доп.=%d)",
                written_ru, written_adv, len(extra_adversarial))


# ============================================================================
# Этап: features
# ============================================================================

def _resolve_total_length(positive_test_clips: Sequence[Path], override: int | None) -> int:
    if override:
        return int(override)
    import scipy.io.wavfile  # core-зависимость проекта (requirements.txt), не training-only

    n = min(50, len(positive_test_clips))
    if n == 0:
        return 32000
    rnd = random.Random(13)
    sample = rnd.sample(list(positive_test_clips), n)
    durations = []
    for p in sample:
        sr, dat = scipy.io.wavfile.read(str(p))
        durations.append(len(dat))
    return compute_total_length_samples(durations)


def stage_features(args: argparse.Namespace, paths: ProjectPaths) -> None:
    """Аугментация (``openwakeword.data.augment_clips``) + featurization
    (``openwakeword.utils.compute_features_from_generator``) позитивов и
    негативов в .npy для тренировки. Мирроит логику ``openwakeword.train``
    ``__main__`` (тот же вызов тех же функций пакета)."""
    if is_stage_done(paths.features, "features") and not args.force:
        logger.info(
            "features: уже выполнено (%s), пропуск (--force для повтора)",
            marker_path(paths.features, "features"),
        )
        return
    paths.features.mkdir(parents=True, exist_ok=True)

    try:
        from openwakeword.data import augment_clips
        from openwakeword.utils import compute_features_from_generator
    except ImportError as exc:
        raise RuntimeError(
            f"openwakeword.data недоступен ({exc}). Требуются доп. пакеты: "
            "pip install pronouncing audiomentations speechbrain mutagen acoustics "
            "(см. README «Пререквизиты»)."
        ) from exc

    pos_train = sorted((paths.positives / "train").glob("*.wav"))
    pos_test = sorted((paths.positives / "test").glob("*.wav"))
    neg_paths = (
        sorted((paths.negatives / "ru_synthetic").glob("*.wav"))
        + sorted((paths.negatives / "adversarial").glob("*.wav"))
    )
    if not pos_train or not pos_test:
        raise RuntimeError("Нет позитивных клипов -- запустите --stage positives")
    if not neg_paths:
        raise RuntimeError("Нет негативных клипов -- запустите --stage negatives")

    neg_train, neg_test = deterministic_train_test_split(
        neg_paths, test_ratio=args.test_ratio, seed=args.seed,
    )
    # Защита от вырожденного сплита при крошечных --limit (сплит форсирует
    # n_test>=1 и может опустошить train) -- тот же паттерн, что у combos_train.
    neg_train = neg_train or neg_paths[:1]

    total_length = _resolve_total_length(pos_test, args.total_length)

    rir_paths = [] if args.skip_rir else _glob_wavs(paths.corpora / "rir")
    background_paths = _glob_wavs(args.background_dir)
    if not rir_paths:
        logger.warning(
            "features: RIR не найдены -- реверберация отключена "
            "(augment_clips деградирует штатно, см. риск #1 в спеке)"
        )
    if not background_paths:
        logger.info("features: --background-dir не задан -- AddBackgroundNoise отключён штатно")

    jobs: list[tuple[str, Sequence[Path]]] = [
        ("positive_features_train.npy", pos_train),
        ("positive_features_test.npy", pos_test),
        ("negative_features_train.npy", neg_train),
        ("negative_features_test.npy", neg_test),
    ]
    for out_name, clip_paths in jobs:
        out_path = paths.features / out_name
        if out_path.exists() and not args.force:
            logger.info("features: %s уже существует, пропуск", out_name)
            continue
        clips = [str(p) for p in clip_paths] * max(1, args.augmentation_rounds)
        logger.info(
            "features: %s -- %d исходных клипов x%d раундов аугментации = %d",
            out_name, len(clip_paths), args.augmentation_rounds, len(clips),
        )
        gen = augment_clips(
            clips, total_length=total_length, batch_size=args.augmentation_batch_size,
            background_clip_paths=background_paths, RIR_paths=rir_paths,
        )
        # device="cpu": AudioFeatures здесь поддерживает только cpu/CUDA
        # (onnxruntime CPU/CUDAExecutionProvider) -- MPS этому слою не нужен,
        # ONNX-фичеризация лёгкая; MPS используется только на train-этапе.
        compute_features_from_generator(
            gen, n_total=len(clips), clip_duration=total_length,
            output_file=str(out_path), device="cpu", ncpu=args.feature_ncpu,
        )

    write_marker(paths.features, "features", {
        "total_length_samples": total_length, "test_ratio": args.test_ratio,
        "rir_count": len(rir_paths), "background_count": len(background_paths),
    })
    logger.info("features: этап завершён (total_length=%d сэмплов)", total_length)


# ============================================================================
# Этап: train
# ============================================================================

def _resolve_torch_device(device_arg: str):
    """Разрешает ``--device`` в ``torch.device``.

    ``openwakeword.train.Model.__init__`` автоопределяет device ТОЛЬКО для CUDA
    (``torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')``) -- на
    Apple Silicon без явного оверрайда тренировка тихо останется на CPU. Здесь
    ``auto`` предпочитает CUDA, затем MPS, затем CPU.
    """
    import torch

    if device_arg == "cpu":
        return torch.device("cpu")
    if device_arg == "mps":
        if not torch.backends.mps.is_available():
            raise RuntimeError("--device mps запрошен, но MPS недоступен на этой машине")
        return torch.device("mps")
    if device_arg == "cuda":
        if not torch.cuda.is_available():
            raise RuntimeError("--device cuda запрошен, но CUDA недоступна")
        return torch.device("cuda")
    # auto
    if torch.cuda.is_available():
        return torch.device("cuda")
    if torch.backends.mps.is_available():
        return torch.device("mps")
    return torch.device("cpu")


def _read_marker_total_length(features_dir: Path) -> int | None:
    meta = read_marker(features_dir, "features")
    if meta and "total_length_samples" in meta:
        return int(meta["total_length_samples"])
    return None


def _write_training_report(
    report_path: Path, *, args: argparse.Namespace, input_shape: Any,
    gate_note: str, history: dict[str, Any], checkpoint_path: Path,
) -> None:
    lines = [
        f"# Отчёт обучения wake-word модели «{args.phrase}» ({args.model_name})",
        "",
        f"- Дата: {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}",
        f"- input_shape: {input_shape}",
        f"- steps: {args.steps}, model_type: {args.model_type}, layer_size: {args.layer_size}",
        f"- device: {args.device}",
        f"- Гейт: max_fp_per_hour<={args.max_fp_per_hour}, "
        f"min_recall>={args.min_recall}, val_set_hrs={args.val_set_hrs}",
        "",
        "## Результат гейта",
        "",
        gate_note,
        "",
        "## История обучения (val_* метрики по контрольным точкам)",
        "",
    ]
    for key in ("val_accuracy", "val_recall", "val_fp_per_hr", "val_n_fp"):
        values = history.get(key) or []
        if values:
            tail_vals = list(values)[-5:]
            tail = ", ".join(f"{float(v):.4f}" for v in tail_vals)
            lines.append(f"- **{key}** (последние {len(tail_vals)} из {len(values)}): {tail}")
        else:
            lines.append(f"- **{key}**: нет данных")
    lines += [
        "",
        "## Артефакты",
        "",
        f"- Чекпоинт: `{checkpoint_path}`",
        "",
        "Следующий шаг: `--stage export`, затем `--stage install`, затем T5 "
        "(живая owner-валидация голосом -- см. README «Протокол owner-валидации»).",
        "",
    ]
    report_path.write_text("\n".join(lines), encoding="utf-8")


def stage_train(args: argparse.Namespace, paths: ProjectPaths) -> None:
    """Обучает ``openwakeword.train.Model`` через ``auto_train`` и гейтует
    итог по ``max_fp_per_hour`` через ``_select_best_model`` (решение §3.5
    спеки). Сохраняет чекпоинт (.pt) для этапа export и офлайн-отчёт
    (``report_<model_name>.md``)."""
    if is_stage_done(paths.artifacts, "train") and not args.force:
        logger.info(
            "train: уже выполнено (%s), пропуск (--force для повтора)",
            marker_path(paths.artifacts, "train"),
        )
        return
    paths.artifacts.mkdir(parents=True, exist_ok=True)

    try:
        import numpy as np
        import torch
        from openwakeword.data import mmap_batch_generator
        from openwakeword.train import Model as TrainableModel
        from openwakeword.utils import AudioFeatures
    except ImportError as exc:
        raise RuntimeError(
            f"Тренировочный стек недоступен ({exc}). Требуются: torch, "
            "openwakeword + доп. пакеты (pronouncing, audiomentations, "
            "speechbrain, mutagen, acoustics, torchinfo) -- см. README "
            "«Пререквизиты»."
        ) from exc

    required = [
        "positive_features_train.npy", "positive_features_test.npy",
        "negative_features_train.npy", "negative_features_test.npy",
    ]
    missing = [f for f in required if not (paths.features / f).exists()]
    if missing:
        raise RuntimeError(f"Нет features {missing} -- запустите --stage features")

    fp_val_path = paths.corpora / args.fp_validation_file
    if not fp_val_path.exists():
        raise RuntimeError(f"Нет fp-валидации {fp_val_path} -- запустите --stage corpora")

    device = _resolve_torch_device(args.device)
    logger.info("train: device=%s", device)

    audio_features = AudioFeatures(device="cpu")
    total_length = _read_marker_total_length(paths.features) or 32000
    input_shape = audio_features.get_embedding_shape(total_length / 16000)
    logger.info("train: input_shape=%s total_length=%d сэмплов", input_shape, total_length)

    def _reshape_transform(x: Any, n: int = 16) -> Any:
        # Повторяет f() из openwakeword.train __main__: приводит фичи произвольной
        # длины к окнам по n=16 таймстепов (ожидаемый input_shape[0]).
        if n > x.shape[1] or n < x.shape[1]:
            x = np.vstack(x)
            return np.array([x[i:i + n, :] for i in range(0, x.shape[0] - n, n)])
        return x

    feature_files: dict[str, str] = {
        "positive": str(paths.features / "positive_features_train.npy"),
        "adversarial_negative": str(paths.features / "negative_features_train.npy"),
    }
    negatives_override = Path(args.negatives_dir) if args.negatives_dir else (
        paths.corpora / args.acav100m_file
    )
    if negatives_override.exists():
        feature_files["general_negative"] = str(negatives_override)
        logger.info("train: подключён общий негативный корпус %s", negatives_override)
    else:
        logger.warning(
            "train: общий негативный корпус (%s) не найден -- используются "
            "только RU-синтетика+adversarial негативы (риск #1 в спеке, "
            "качество гейта может быть ниже, отмечено в отчёте)", negatives_override,
        )

    data_transforms = {k: _reshape_transform for k in feature_files}
    label_transforms: dict[str, Callable[[Any], list[int]]] = {
        k: (lambda x: [1 for _ in x]) if k == "positive" else (lambda x: [0 for _ in x])
        for k in feature_files
    }

    # n_per_class намеренно НЕ передаём -- mmap_batch_generator сам вычисляет
    # пропорциональные размеры по фактическим shape массивов (см. data.py).
    batch_gen = mmap_batch_generator(
        feature_files, data_transform_funcs=data_transforms, label_transform_funcs=label_transforms,
    )

    class _IterDataset(torch.utils.data.IterableDataset):
        def __init__(self, generator: Any) -> None:
            self.generator = generator

        def __iter__(self) -> Any:
            return self.generator

    # num_workers=0 намеренно: DataLoader с num_workers>0 на macOS использует
    # multiprocessing "spawn" (не "fork") -- живой Python-генератор/mmap-объект
    # внутри _IterDataset не переживает передачу в дочерний процесс. Один
    # процесс достаточен -- узкое место здесь GPU/MPS-обучение, не I/O.
    x_train = torch.utils.data.DataLoader(_IterDataset(batch_gen), batch_size=None, num_workers=0)

    x_val_pos = np.load(paths.features / "positive_features_test.npy")
    x_val_neg = np.load(paths.features / "negative_features_test.npy")
    val_labels = np.hstack(
        (np.ones(x_val_pos.shape[0]), np.zeros(x_val_neg.shape[0]))
    ).astype(np.float32)
    x_val = torch.utils.data.DataLoader(
        torch.utils.data.TensorDataset(
            torch.from_numpy(np.vstack((x_val_pos, x_val_neg))), torch.from_numpy(val_labels),
        ),
        batch_size=len(val_labels),
    )

    x_val_fp_raw = np.load(fp_val_path)
    x_val_fp_windows = np.array([
        x_val_fp_raw[i:i + input_shape[0]]
        for i in range(0, x_val_fp_raw.shape[0] - input_shape[0], 1)
    ])
    x_val_fp_labels = np.zeros(x_val_fp_windows.shape[0]).astype(np.float32)
    x_val_fp = torch.utils.data.DataLoader(
        torch.utils.data.TensorDataset(
            torch.from_numpy(x_val_fp_windows), torch.from_numpy(x_val_fp_labels),
        ),
        batch_size=len(x_val_fp_labels),
    )

    oww = TrainableModel(
        n_classes=1, input_shape=input_shape, model_type=args.model_type,
        layer_dim=args.layer_size, seconds_per_example=1280 * input_shape[0] / 16000,
    )
    oww.device = device  # см. _resolve_torch_device -- пакет не автоопределяет MPS

    logger.info("train: запускаю auto_train (steps=%d) -- это может занять часы", args.steps)
    combined_model = oww.auto_train(
        X_train=x_train, X_val=x_val, false_positive_val_data=x_val_fp,
        steps=args.steps, max_negative_weight=args.max_negative_weight,
        target_fp_per_hour=args.target_fp_per_hour,
    )

    logger.info(
        "train: гейтую по max_fp_per_hour=%.2f min_recall=%.2f (val_set_hrs=%.1f)",
        args.max_fp_per_hour, args.min_recall, args.val_set_hrs,
    )
    gated_model = None
    try:
        gated_model = oww._select_best_model(
            x_val_fp, val_set_hrs=args.val_set_hrs,
            max_fp_per_hour=args.max_fp_per_hour, min_recall=args.min_recall,
        )
    except Exception as exc:  # noqa: BLE001 -- гейт best-effort, не роняем весь прогон
        logger.warning("train: _select_best_model упал (%s) -- использую комбинированную модель", exc)

    if gated_model is None:
        gate_note = (
            f"Гейт max_fp_per_hour<={args.max_fp_per_hour} не нашёл кандидата с "
            f"recall>={args.min_recall} среди контрольных точек -- использована "
            "комбинированная модель auto_train (усреднение >90-го перцентиля). "
            "Метрики истории обучения см. ниже; перед owner-валидацией (T5) "
            "рассмотрите больше --steps или мягче --max-fp-per-hour/--min-recall."
        )
        logger.warning("train: %s", gate_note)
        selected_model = combined_model
    else:
        gate_note = f"Гейт max_fp_per_hour<={args.max_fp_per_hour} УДОВЛЕТВОРЁН выбранной контрольной точкой."
        selected_model = gated_model

    checkpoint_path = paths.artifacts / f"{args.model_name}_checkpoint.pt"
    oww.model = selected_model
    oww.save_model(str(checkpoint_path))

    report_path = paths.report_path(args.model_name)
    _write_training_report(
        report_path, args=args, input_shape=input_shape, gate_note=gate_note,
        history=dict(oww.history), checkpoint_path=checkpoint_path,
    )

    write_marker(paths.artifacts, "train", {
        "checkpoint_path": str(checkpoint_path), "report_path": str(report_path),
        "gate_satisfied": gated_model is not None,
    })
    logger.info("train: этап завершён -- чекпоинт %s, отчёт %s", checkpoint_path, report_path)


# ============================================================================
# Этап: export
# ============================================================================

def stage_export(args: argparse.Namespace, paths: ProjectPaths) -> None:
    """Экспортирует обученный чекпоинт (.pt) в ONNX (``<model_name>.onnx``)
    для рантайм-адаптера (``backend/openwakeword_adapter.py``)."""
    if is_stage_done(paths.artifacts, "export") and not args.force:
        logger.info(
            "export: уже выполнено (%s), пропуск (--force для повтора)",
            marker_path(paths.artifacts, "export"),
        )
        return

    checkpoint_path = paths.artifacts / f"{args.model_name}_checkpoint.pt"
    if not checkpoint_path.exists():
        raise RuntimeError(f"Нет чекпоинта {checkpoint_path} -- запустите --stage train")

    try:
        import torch
        from openwakeword.train import Model as TrainableModel
        from openwakeword.utils import AudioFeatures
    except ImportError as exc:
        raise RuntimeError(f"Тренировочный стек недоступен ({exc})") from exc

    total_length = _read_marker_total_length(paths.features) or 32000
    audio_features = AudioFeatures(device="cpu")
    input_shape = audio_features.get_embedding_shape(total_length / 16000)

    # torch >= 2.6 меняет дефолт weights_only=True для torch.load. Наш чекпоинт
    # -- результат Model.save_model(), которая сериализует ЦЕЛИКОМ объект
    # nn.Module (не только state_dict), поэтому явно просим weights_only=False.
    # Это доверенный локальный файл, созданный этим же скриптом на предыдущем
    # этапе (--stage train), а не сторонний/скачанный источник.
    loaded_module = torch.load(checkpoint_path, map_location="cpu", weights_only=False)

    # model_type/layer_size здесь нужны только чтобы создать валидный объект
    # Model (input_shape/n_classes) -- реальная архитектура приходит из
    # чекпоинта через model=loaded_module (export_model делает deepcopy).
    oww = TrainableModel(
        n_classes=1, input_shape=input_shape, model_type=args.model_type,
        layer_dim=args.layer_size, seconds_per_example=1280 * input_shape[0] / 16000,
    )
    oww.export_model(model=loaded_module, model_name=args.model_name, output_dir=str(paths.artifacts))

    onnx_path = paths.artifacts / f"{args.model_name}.onnx"
    if not onnx_path.exists():
        raise RuntimeError(f"export_model не создал {onnx_path} -- см. лог выше")

    write_marker(paths.artifacts, "export", {"onnx_path": str(onnx_path)})
    logger.info("export: готово -- %s", onnx_path)


# ============================================================================
# Этап: install
# ============================================================================

def stage_install(args: argparse.Namespace, paths: ProjectPaths) -> None:
    """Копирует ``artifacts/<model>.onnx`` -> ``{data_dir}/wake_word_models/``
    (см. ``backend/openwakeword_adapter.py::_CUSTOM_MODELS_DIR``) и проверяет,
    что openwakeword может загрузить установленный файл."""
    onnx_path = paths.artifacts / f"{args.model_name}.onnx"
    if not onnx_path.exists():
        raise RuntimeError(f"Нет {onnx_path} -- запустите --stage export")

    data_dir = Path(args.data_dir).expanduser().resolve()
    dest_dir = data_dir / "wake_word_models"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / onnx_path.name

    if is_stage_done(dest_dir, "install") and not args.force:
        logger.info(
            "install: уже выполнено (%s), пропуск (--force для повтора)",
            marker_path(dest_dir, "install"),
        )
        return

    # Path-containment guard -- тот же паттерн, что openwakeword_adapter.py's
    # _load_model (relative_to, не строковый startswith -- см. CLAUDE.md
    # "Path-containment guard").
    resolved_dest = dest_path.resolve()
    if not resolved_dest.is_relative_to(dest_dir.resolve()):
        raise ValueError(f"Путь установки выходит за пределы {dest_dir}: {resolved_dest}")

    import shutil
    shutil.copy2(onnx_path, dest_path)
    logger.info("install: скопировано %s -> %s", onnx_path, dest_path)

    if not args.skip_load_check:
        try:
            from openwakeword.model import Model as InferenceModel
            InferenceModel(wakeword_models=[str(dest_path)])
            logger.info("install: постпроверка загрузки прошла успешно")
        except ImportError as exc:
            logger.warning(
                "install: openwakeword не установлен в текущем окружении (%s) -- "
                "постпроверка пропущена, проверьте вручную из .venv_krab_ear", exc,
            )
        except Exception as exc:  # noqa: BLE001 -- реальная ошибка загрузки должна остановить установку
            raise RuntimeError(f"Установленная модель не загружается openwakeword'ом: {exc}") from exc

    write_marker(dest_dir, "install", {"onnx_path": str(dest_path)})
    logger.info(
        "install: готово. Модель %r появится в Settings-пикере после "
        "перезапуска backend (wake_word_list_models IPC) -- см. README.",
        args.model_name,
    )


# ============================================================================
# Оркестрация
# ============================================================================

STAGE_FUNCS: dict[str, Callable[[argparse.Namespace, ProjectPaths], None]] = {
    "corpora": stage_corpora,
    "positives": stage_positives,
    "negatives": stage_negatives,
    "features": stage_features,
    "train": stage_train,
    "export": stage_export,
    "install": stage_install,
}


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if not args.stage and not args.all:
        parser.error("укажите --stage <этап> или --all")

    if not args.secondary_phrase:
        args.secondary_phrase = list(DEFAULT_SECONDARY_PHRASES)

    configure_cli_logging(args.verbose)

    work_dir = Path(args.work_dir).expanduser().resolve() if args.work_dir else SCRIPT_DIR
    paths = ProjectPaths.from_work_dir(work_dir)

    stages = list(STAGES) if args.all else [args.stage]

    for stage in stages:
        logger.info("=== Этап: %s ===", stage)
        started = time.monotonic()
        try:
            STAGE_FUNCS[stage](args, paths)
        except KeyboardInterrupt:
            logger.warning("Прервано пользователем (Ctrl+C)")
            return 130
        except Exception as exc:  # noqa: BLE001 -- CLI top-level: показать причину и остановиться
            logger.error("Этап %s провалился: %s", stage, exc)
            if args.verbose:
                logger.exception("Трассировка:")
            return 1
        logger.info("=== Этап %s завершён за %.1fs ===", stage, time.monotonic() - started)

    return 0


if __name__ == "__main__":
    sys.exit(main())
