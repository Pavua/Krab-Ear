"""TimelineExporter — экспорт временной шкалы записей в SVG, JSON, iCalendar.

Форматы:
  - SVG  : самодостаточная SVG-визуализация с цветными блоками по часу/дню
  - JSON : структурированный JSON для внешних инструментов визуализации
  - iCal : формат iCalendar (RFC 5545) — каждый блок как VEVENT
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any
from xml.sax.saxutils import escape as _xml_sax_escape
from xml.sax.saxutils import quoteattr as _xml_sax_quoteattr

# ---------------------------------------------------------------------------
# Цветовая палитра для языков и блоков
# ---------------------------------------------------------------------------

_PALETTE = [
    "#4A90D9",  # синий
    "#E67E22",  # оранжевый
    "#2ECC71",  # зелёный
    "#9B59B6",  # фиолетовый
    "#E74C3C",  # красный
    "#1ABC9C",  # бирюзовый
    "#F39C12",  # жёлтый
    "#34495E",  # тёмно-серый
]

_LANG_COLORS: dict[str, str] = {
    "ru": "#4A90D9",
    "es": "#E67E22",
    "en": "#2ECC71",
    "de": "#9B59B6",
    "fr": "#E74C3C",
    "zh": "#1ABC9C",
    "ja": "#F39C12",
    "pt": "#34495E",
}

_DEFAULT_BLOCK_COLOR = "#7F8C8D"


def _block_color(block: dict[str, Any], index: int) -> str:
    """Возвращает цвет блока: по первому языку или по индексу из палитры."""
    langs = block.get("languages") or []
    if langs:
        lang = str(langs[0]).lower()
        if lang in _LANG_COLORS:
            return _LANG_COLORS[lang]
    return _PALETTE[index % len(_PALETTE)]


def _parse_ts(ts: Any) -> datetime | None:
    """Парсит ISO-8601 строку в datetime. None при ошибке."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).strip())
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _ical_dt(dt: datetime) -> str:
    """Форматирует datetime в строку iCal DTSTART/DTEND (UTC, basic)."""
    utc = dt.astimezone(timezone.utc)
    return utc.strftime("%Y%m%dT%H%M%SZ")


def _fold_ics_line(line: str, max_octets: int = 75) -> str:
    """RFC 5545 §3.1 line-folding: splits at max_octets octet boundary.

    Continuation lines are prefixed with CRLF + SP.  Input ``line`` must NOT
    include the trailing CRLF — it is added by the caller when joining.

    Args:
        line:       A single iCalendar content line (e.g. ``SUMMARY:text``).
        max_octets: Maximum octets per line segment (RFC 5545 mandates 75).

    Returns:
        The folded line with CRLF+SP inserted at every fold point.
    """
    encoded = line.encode("utf-8")
    if len(encoded) <= max_octets:
        return line

    parts: list[str] = []
    while len(encoded) > max_octets:
        # Split at exactly max_octets; back off if we are inside a multi-byte
        # UTF-8 sequence (continuation bytes start with 0b10xxxxxx = 0x80-0xBF).
        cut = max_octets
        while cut > 0 and (encoded[cut] & 0xC0) == 0x80:
            cut -= 1
        parts.append(encoded[:cut].decode("utf-8"))
        encoded = encoded[cut:]
        max_octets = 74  # continuation lines: 1 SP prefix → 74 content octets
    parts.append(encoded.decode("utf-8"))
    return "\r\n ".join(parts)


# ---------------------------------------------------------------------------
# Основной класс
# ---------------------------------------------------------------------------


class TimelineExporter:
    """Экспортирует список временных блоков (TimelineBlock.to_dict()) в SVG/JSON/iCal."""

    # ── SVG ─────────────────────────────────────────────────────────────────

    def export_svg(
        self,
        blocks: list[dict[str, Any]],
        width: int = 1200,
        height: int = 400,
        privacy_mode: bool = False,
    ) -> str:
        """Генерирует SVG-визуализацию временной шкалы.

        Каждый блок отображается в виде прямоугольника, ширина которого
        пропорциональна ``items_count``.  Поддерживает пустой список —
        возвращает SVG с заглушкой «No data».

        Args:
            blocks: список dict (TimelineBlock.to_dict()).
            width:  ширина SVG в пикселях (по умолчанию 1200).
            height: высота SVG в пикселях (по умолчанию 400).

        Returns:
            Строка с самодостаточным SVG-документом.
        """
        width = max(200, int(width))
        height = max(100, int(height))

        if not blocks:
            return self._svg_empty(width, height)

        # Горизонтальные отступы
        pad_x = 40
        pad_y = 60
        bar_area_w = width - 2 * pad_x
        bar_area_h = height - pad_y - 40  # место для оси X и заголовка

        total_items = sum(int(b.get("items_count", 0)) for b in blocks)
        if total_items == 0:
            total_items = len(blocks)  # равномерное распределение

        # Строим rect элементы
        rects: list[str] = []
        labels: list[str] = []
        x_cursor = pad_x

        for idx, block in enumerate(blocks):
            count = max(1, int(block.get("items_count", 1)))
            frac = count / total_items
            bar_w = max(2, bar_area_w * frac)
            bar_h = bar_area_h

            color = _block_color(block, idx)
            start_ts = str(block.get("start_time", ""))
            # F4: privacy guard — omit transcript keywords from SVG tooltip
            if privacy_mode:
                summary = ""
            else:
                summary = str(block.get("summary_text", ""))[:40]
            lang_str = ", ".join(block.get("languages") or [])
            duration = block.get("total_duration_sec", 0)

            # Tooltip текст
            tooltip = (
                f"{start_ts} | {count} items"
                + (f" | {lang_str}" if lang_str else "")
                + (f" | {duration:.0f}s" if duration else "")
                + (f" | {summary}" if summary else "")
            )

            # Rect с hover opacity
            rect_id = f"b{idx}"
            rects.append(
                f'  <rect id="{rect_id}" x="{x_cursor:.1f}" y="{pad_y}"'
                f' width="{bar_w:.1f}" height="{bar_h}" fill="{color}"'
                f' rx="3" opacity="0.85">'
                f"<title>{self._xml_escape(tooltip)}</title></rect>"
            )

            # Метка по оси X — короткая дата/час
            label = self._short_label(start_ts)
            if len(blocks) <= 30 or idx % max(1, len(blocks) // 10) == 0:
                lx = x_cursor + bar_w / 2
                labels.append(
                    f'  <text x="{lx:.1f}" y="{pad_y + bar_h + 20}"'
                    f' text-anchor="middle" font-size="10" fill="#555">'
                    f"{self._xml_escape(label)}</text>"
                )

            x_cursor += bar_w

        # CSS + defs
        css = """
    <style>
      rect { transition: opacity 0.15s; cursor: default; }
      rect:hover { opacity: 1 !important; }
      text.title { font-family: system-ui, sans-serif; font-weight: 600; }
      text { font-family: system-ui, sans-serif; }
    </style>"""

        # Заголовок
        title_elem = (
            f'  <text x="{width // 2}" y="24" text-anchor="middle"'
            f' font-size="14" fill="#333" class="title">Recording Timeline</text>'
        )

        # Ось Y — count info
        y_axis_label = (
            f'  <text x="{pad_x - 5}" y="{pad_y + bar_area_h // 2}"'
            f' text-anchor="end" font-size="10" fill="#777"'
            f' transform="rotate(-90,{pad_x - 5},{pad_y + bar_area_h // 2})">'
            f"count</text>"
        )

        # Нижняя подпись
        total_label = (
            f'  <text x="{width // 2}" y="{height - 5}"'
            f' text-anchor="middle" font-size="10" fill="#999">'
            f"Total: {total_items} recordings in {len(blocks)} blocks</text>"
        )

        body = "\n".join(rects + labels + [title_elem, y_axis_label, total_label])

        svg = (
            f'<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<svg xmlns="http://www.w3.org/2000/svg"'
            f' width="{width}" height="{height}"'
            f' viewBox="0 0 {width} {height}">\n'
            f"{css}\n"
            f"{body}\n"
            f"</svg>"
        )
        return svg

    # ── JSON ─────────────────────────────────────────────────────────────────

    def export_json(self, blocks: list[dict[str, Any]]) -> str:
        """Сериализует список блоков в структурированный JSON.

        Добавляет мета-поля: ``exported_at``, ``total_blocks``,
        ``total_recordings``, ``total_duration_sec``.

        Args:
            blocks: список dict (TimelineBlock.to_dict()).

        Returns:
            Отформатированная JSON-строка (indent=2).
        """
        total_recordings = sum(int(b.get("items_count", 0)) for b in blocks)
        total_duration = sum(float(b.get("total_duration_sec", 0.0)) for b in blocks)

        payload = {
            "schema_version": "1.0",
            "exported_at": datetime.now(tz=timezone.utc).isoformat(),
            "total_blocks": len(blocks),
            "total_recordings": total_recordings,
            "total_duration_sec": round(total_duration, 3),
            "blocks": blocks,
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)

    # ── iCalendar ────────────────────────────────────────────────────────────

    def export_ical(
        self,
        items: list[dict[str, Any]],
        privacy_mode: bool = False,
    ) -> str:
        """Экспортирует список блоков (или элементов истории) в iCalendar (RFC 5545).

        Каждый элемент становится VEVENT:
          - DTSTART / DTEND из ``start_time`` / ``end_time``
            (или ``ts`` + ``audio_duration_sec`` для элементов истории).
          - SUMMARY из ``summary_text`` или ``text`` (усечённый).
            Если ``privacy_mode=True`` — заменяется на generic «Krab Ear recording».
          - DESCRIPTION — языки + кол-во записей.
          - Все строки соответствуют RFC 5545 §3.1 (75-octet line-folding).

        Args:
            items:        список dict — TimelineBlock.to_dict() или элементы истории.
            privacy_mode: если True — SUMMARY не содержит текст транскрипции.

        Returns:
            Строка iCalendar.
        """
        lines = [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//KrabEar//TimelineExport//EN",
            "CALSCALE:GREGORIAN",
            "METHOD:PUBLISH",
        ]

        now_str = _ical_dt(datetime.now(tz=timezone.utc))

        for item in items:
            uid = str(uuid.uuid4()) + "@krabear"

            # Определяем DTSTART / DTEND
            start_ts = item.get("start_time") or item.get("ts")
            end_ts = item.get("end_time")

            start_dt = _parse_ts(start_ts)
            if start_dt is None:
                continue  # пропускаем без даты

            if end_ts:
                end_dt = _parse_ts(end_ts)
            else:
                # Для элементов истории: start + duration
                duration = float(item.get("audio_duration_sec") or 0)
                from datetime import timedelta
                end_dt = start_dt + timedelta(seconds=max(duration, 60))

            if end_dt is None:
                from datetime import timedelta
                end_dt = start_dt + timedelta(hours=1)

            # SUMMARY — F2: privacy guard
            if privacy_mode:
                summary_text = "Krab Ear recording"
            else:
                summary_text = (
                    item.get("summary_text")
                    or item.get("text", "")[:80]
                    or "Recording"
                )
            summary_escaped = self._ical_escape(str(summary_text))

            # DESCRIPTION
            langs = item.get("languages") or []
            lang_str = ", ".join(str(ln) for ln in langs) if langs else ""
            count = item.get("items_count", "")
            desc_parts = []
            if lang_str:
                desc_parts.append(f"Languages: {lang_str}")
            if count:
                desc_parts.append(f"Recordings: {count}")
            duration_s = item.get("total_duration_sec")
            if duration_s:
                desc_parts.append(f"Duration: {float(duration_s):.0f}s")
            description_escaped = self._ical_escape(" | ".join(desc_parts)) if desc_parts else ""

            # F1: RFC 5545 §3.1 — fold all content lines at 75 octets
            vevent_lines = [
                "BEGIN:VEVENT",
                _fold_ics_line(f"UID:{uid}"),
                _fold_ics_line(f"DTSTAMP:{now_str}"),
                _fold_ics_line(f"DTSTART:{_ical_dt(start_dt)}"),
                _fold_ics_line(f"DTEND:{_ical_dt(end_dt)}"),
                _fold_ics_line(f"SUMMARY:{summary_escaped}"),
            ]
            if description_escaped:
                vevent_lines.append(_fold_ics_line(f"DESCRIPTION:{description_escaped}"))
            vevent_lines.append("END:VEVENT")
            lines.extend(vevent_lines)

        lines.append("END:VCALENDAR")
        return "\r\n".join(lines) + "\r\n"

    # ── Вспомогательные методы ───────────────────────────────────────────────

    @staticmethod
    def _svg_empty(width: int, height: int) -> str:
        """SVG-заглушка для пустого списка блоков."""
        cx, cy = width // 2, height // 2
        return (
            f'<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<svg xmlns="http://www.w3.org/2000/svg"'
            f' width="{width}" height="{height}">\n'
            f'  <rect width="{width}" height="{height}" fill="#f5f5f5"/>\n'
            f'  <text x="{cx}" y="{cy}" text-anchor="middle"'
            f' font-family="system-ui,sans-serif" font-size="16" fill="#aaa">'
            f"No data</text>\n"
            f"</svg>"
        )

    # Карта дополнительных сущностей для кавычек (escape() сам по себе
    # экранирует только &, <, >; кавычки добавляем явно для контекста атрибутов).
    _XML_ESCAPE_ENTITIES = {'"': "&quot;", "'": "&#39;"}

    @staticmethod
    def _xml_escape(text: str) -> str:
        """Экранирует пользовательский текст для встраивания в SVG/XML.

        Использует канонический ``xml.sax.saxutils.escape`` (надёжнее ручной
        цепочки ``replace``): экранирует ``&``, ``<``, ``>`` плюс ``"`` и ``'``
        через карту сущностей — безопасно как для содержимого элементов
        (``<text>``/``<title>``), так и для значений атрибутов.
        """
        return _xml_sax_escape(str(text), TimelineExporter._XML_ESCAPE_ENTITIES)

    @staticmethod
    def _xml_attr(text: str) -> str:
        """Экранирует и заключает в кавычки пользовательскую строку для XML-атрибута.

        Возвращает значение УЖЕ В КАВЫЧКАХ (``quoteattr`` сам выбирает безопасные
        кавычки). Использовать там, где пользовательский текст попадает в значение
        атрибута SVG/XML.
        """
        return _xml_sax_quoteattr(str(text))

    @staticmethod
    def _ical_escape(text: str) -> str:
        """Экранирует пользовательский текст для iCalendar TEXT-значений (RFC 5545 §3.3.11).

        Защищает от инъекции новых iCal-свойств/строк:
          - обратный слэш ``\\`` → ``\\\\`` (первым, чтобы не двойного-экранировать ниже);
          - ``;`` → ``\\;`` и ``,`` → ``\\,`` (разделители параметров/значений);
          - ЛЮБОЙ перевод строки (CRLF, LF, либо ОДИНОЧНЫЙ CR) → литеральное ``\\n``.
            Голый CR раньше молча отбрасывался — теперь нормализуется, чтобы ни
            при каких условиях в значение свойства не попал сырой перевод строки.
        """
        escaped = str(text).replace("\\", "\\\\")
        escaped = escaped.replace(";", "\\;").replace(",", "\\,")
        # Нормализуем все варианты перевода строки в литеральное "\n":
        # сначала CRLF/CR → LF, затем LF → "\n" (порядок исключает двойные "\n").
        escaped = escaped.replace("\r\n", "\n").replace("\r", "\n")
        escaped = escaped.replace("\n", "\\n")
        return escaped

    @staticmethod
    def _short_label(ts: str) -> str:
        """Короткая метка для оси X из ISO-строки."""
        try:
            dt = datetime.fromisoformat(ts)
            # Если время != полночь — показываем час
            if dt.hour != 0 or dt.minute != 0:
                return dt.strftime("%m/%d %H:%M")
            return dt.strftime("%m/%d")
        except (ValueError, TypeError):
            return ts[:10] if ts else ""
