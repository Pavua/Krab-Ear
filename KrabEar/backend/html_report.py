"""HTMLReportGenerator — генератор красивых HTML-отчётов для истории Krab Ear.

Генерирует автономный HTML-файл (все стили встроены, без внешних зависимостей):
- Адаптивная верстка (responsive layout)
- Dark/light тема через prefers-color-scheme
- Секции: заголовок + сводная статистика, временная линия, записи, облако слов (placeholder), footer
- Каждая запись: временна́я метка, значки спикеров, текст, перевод, полоса уверенности
"""

from __future__ import annotations

import html
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("KrabEar.Backend.HTMLReport")

# Цвета для значков спикеров (циклически)
_SPEAKER_COLORS = [
    "#4f8ef7",  # синий
    "#f76b4f",  # оранжево-красный
    "#4fcf8a",  # зелёный
    "#c47ef7",  # фиолетовый
    "#f7c04f",  # жёлтый
    "#4fd9f7",  # голубой
    "#f74f9d",  # розовый
    "#a0f74f",  # лаймовый
]

_CSS = """
/* ── Reset & base ── */
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
html { font-size: 16px; scroll-behavior: smooth; }

:root {
  --bg: #f5f6fa;
  --surface: #ffffff;
  --surface2: #f0f1f7;
  --border: #e0e2ef;
  --text: #1a1c2e;
  --text2: #555770;
  --text3: #8890a4;
  --accent: #4f8ef7;
  --accent-dark: #2d6fe0;
  --success: #34c47c;
  --warning: #f7b84f;
  --danger: #f74f4f;
  --shadow: 0 2px 12px rgba(0,0,0,.08);
  --radius: 12px;
  --radius-sm: 8px;
  --transition: 0.18s ease;
}

@media (prefers-color-scheme: dark) {
  :root {
    --bg: #12131f;
    --surface: #1e2030;
    --surface2: #262840;
    --border: #303350;
    --text: #e8eaf5;
    --text2: #9fa3bc;
    --text3: #636682;
    --accent: #5f9bff;
    --accent-dark: #4080f0;
    --success: #3dd88a;
    --warning: #f7c06a;
    --danger: #ff6060;
    --shadow: 0 2px 12px rgba(0,0,0,.32);
  }
}

body {
  background: var(--bg);
  color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
  line-height: 1.6;
  padding: 0 0 64px;
}

a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }

/* ── Layout ── */
.container { max-width: 900px; margin: 0 auto; padding: 0 20px; }

/* ── Report header ── */
.report-header {
  background: linear-gradient(135deg, var(--accent) 0%, var(--accent-dark) 100%);
  color: #fff;
  padding: 48px 0 40px;
  margin-bottom: 40px;
}
.report-header h1 { font-size: 2rem; font-weight: 700; letter-spacing: -0.02em; }
.report-header .subtitle {
  opacity: .8;
  font-size: .95rem;
  margin-top: 6px;
}
.report-header .meta {
  margin-top: 20px;
  display: flex;
  gap: 24px;
  flex-wrap: wrap;
  font-size: .85rem;
  opacity: .75;
}

/* ── Stats table ── */
.stats-card {
  background: var(--surface);
  border-radius: var(--radius);
  box-shadow: var(--shadow);
  padding: 28px 32px;
  margin-bottom: 36px;
}
.stats-card h2 { font-size: 1.1rem; font-weight: 600; margin-bottom: 20px; color: var(--text2); text-transform: uppercase; letter-spacing: .06em; font-size: .8rem; }
.stats-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 20px;
}
.stat-item {
  background: var(--surface2);
  border-radius: var(--radius-sm);
  padding: 16px 18px;
  text-align: center;
}
.stat-value { font-size: 2rem; font-weight: 700; color: var(--accent); line-height: 1; }
.stat-label { font-size: .78rem; color: var(--text3); margin-top: 6px; }

/* ── Daily recap ── */
.recap-block-title { font-size: .74rem; font-weight: 600; color: var(--text2); text-transform: uppercase; letter-spacing: .06em; margin: 22px 0 10px; }
.recap-chips { display: flex; flex-wrap: wrap; gap: 8px; }
.recap-chip { display: inline-block; padding: 4px 12px; border-radius: 999px; background: var(--surface2); color: var(--text2); font-size: .82rem; font-weight: 600; border: 1px solid var(--border); }
.recap-highlights { margin: 6px 0 0; padding-left: 20px; color: var(--text2); }
.recap-highlights li { margin: 4px 0; font-size: .9rem; line-height: 1.5; }

/* ── Section headings ── */
.section-title {
  font-size: 1rem;
  font-weight: 600;
  color: var(--text2);
  text-transform: uppercase;
  letter-spacing: .07em;
  font-size: .78rem;
  margin: 40px 0 16px;
  display: flex;
  align-items: center;
  gap: 8px;
}
.section-title::after {
  content: '';
  flex: 1;
  height: 1px;
  background: var(--border);
}

/* ── Timeline ── */
.timeline {
  position: relative;
  padding-left: 28px;
  margin-bottom: 40px;
}
.timeline::before {
  content: '';
  position: absolute;
  left: 9px;
  top: 0;
  bottom: 0;
  width: 2px;
  background: var(--border);
}
.timeline-item {
  position: relative;
  margin-bottom: 12px;
  display: flex;
  align-items: center;
  gap: 12px;
}
.timeline-item::before {
  content: '';
  position: absolute;
  left: -22px;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--accent);
  border: 2px solid var(--surface);
}
.timeline-ts { font-size: .78rem; color: var(--text3); white-space: nowrap; min-width: 72px; }
.timeline-text {
  font-size: .88rem;
  color: var(--text2);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  flex: 1;
}

/* ── Entry cards ── */
.entries-list { display: flex; flex-direction: column; gap: 18px; margin-bottom: 40px; }
.entry-card {
  background: var(--surface);
  border-radius: var(--radius);
  box-shadow: var(--shadow);
  padding: 20px 24px;
  border-left: 4px solid var(--accent);
  transition: box-shadow var(--transition);
}
.entry-card:hover { box-shadow: 0 4px 24px rgba(0,0,0,.14); }
.entry-card.favorite { border-left-color: var(--warning); }
.entry-meta {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 10px;
}
.entry-ts { font-size: .78rem; color: var(--text3); }
.entry-index { font-size: .72rem; color: var(--text3); background: var(--surface2); padding: 2px 7px; border-radius: 20px; }
.speaker-badge {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 9px;
  border-radius: 20px;
  font-size: .72rem;
  font-weight: 600;
  color: #fff;
}
.entry-text { font-size: .97rem; color: var(--text); margin-bottom: 6px; line-height: 1.65; }
.entry-translation {
  font-size: .88rem;
  color: var(--text2);
  padding: 8px 12px;
  background: var(--surface2);
  border-radius: var(--radius-sm);
  margin-top: 8px;
  border-left: 3px solid var(--border);
  font-style: italic;
}
.translation-label { font-size: .7rem; text-transform: uppercase; letter-spacing: .06em; color: var(--text3); margin-bottom: 4px; font-style: normal; }
.confidence-bar-wrap { margin-top: 10px; display: flex; align-items: center; gap: 10px; }
.confidence-label { font-size: .72rem; color: var(--text3); min-width: 52px; }
.confidence-bar {
  flex: 1;
  height: 5px;
  background: var(--surface2);
  border-radius: 3px;
  overflow: hidden;
}
.confidence-fill {
  height: 100%;
  border-radius: 3px;
  transition: width .4s ease;
}
.conf-high { background: var(--success); }
.conf-mid  { background: var(--warning); }
.conf-low  { background: var(--danger); }
.confidence-pct { font-size: .72rem; color: var(--text3); min-width: 32px; text-align: right; }
.tag-list { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
.tag {
  background: var(--surface2);
  color: var(--text2);
  font-size: .72rem;
  padding: 2px 8px;
  border-radius: 20px;
  border: 1px solid var(--border);
}

/* ── Word cloud placeholder ── */
.wordcloud-placeholder {
  background: var(--surface);
  border-radius: var(--radius);
  box-shadow: var(--shadow);
  padding: 36px;
  text-align: center;
  color: var(--text3);
  font-size: .88rem;
  margin-bottom: 36px;
}
.wordcloud-placeholder .icon { font-size: 2rem; margin-bottom: 10px; }
.wordcloud-words { display: flex; flex-wrap: wrap; gap: 8px 14px; justify-content: center; margin-top: 20px; }
.wc-word {
  color: var(--text);
  font-weight: 500;
  transition: color var(--transition);
}

/* ── Footer ── */
.report-footer {
  border-top: 1px solid var(--border);
  margin-top: 60px;
  padding-top: 24px;
  font-size: .78rem;
  color: var(--text3);
  display: flex;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 8px;
}

/* ── Responsive ── */
@media (max-width: 600px) {
  .stats-grid { grid-template-columns: repeat(2, 1fr); }
  .report-header h1 { font-size: 1.5rem; }
  .entry-card { padding: 14px 16px; }
}
"""


class HTMLReportGenerator:
    """Генерирует красивые HTML-отчёты для истории Krab Ear.

    Всё встроено inline (CSS, данные) — файл полностью автономный.
    """

    def generate_report(
        self,
        items: list[dict[str, Any]],
        title: str = "Krab Ear Report",
        daily_digest: dict[str, Any] | None = None,
    ) -> str:
        """Создаёт полный HTML-документ для списка записей истории.

        Args:
            items: список dict-представлений HistoryItem (from_dict / to_dict).
            title: заголовок отчёта.
            daily_digest: опциональный payload generate_daily_digest
                (date/total_recordings/total_duration_min/total_words/
                languages_used/top_topics/highlights). Если передан — между
                сводной статистикой и таймлайном вставляется карточка
                «Сводка дня». ``None`` — карточка не выводится.

        Returns:
            Строка с полным HTML-документом.
        """
        stats = self._compute_stats(items)
        header_html = self._render_header(title, stats, items)
        stats_html = self._render_stats_card(stats)
        recap_html = self._render_daily_recap(daily_digest) if daily_digest else ""
        timeline_html = self._render_timeline(items)
        entries_html = self._render_entries(items)
        wordcloud_html = self._render_wordcloud(items)
        footer_html = self._render_footer(stats)

        body = (
            header_html
            + '<div class="container">'
            + stats_html
            + recap_html
            + timeline_html
            + entries_html
            + wordcloud_html
            + footer_html
            + "</div>"
        )

        return self._wrap_document(title, body)

    # ------------------------------------------------------------------
    # Stats computation
    # ------------------------------------------------------------------

    def _compute_stats(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        total = len(items)
        if total == 0:
            return {
                "total": 0,
                "translated": 0,
                "favorites": 0,
                "speakers": 0,
                "avg_confidence": None,
                "total_words": 0,
                "earliest": None,
                "latest": None,
            }

        translated = sum(
            1 for it in items
            if it.get("translated_text") or it.get("translation_status") == "ok"
        )
        favorites = sum(1 for it in items if it.get("favorite"))

        # Уникальные спикеры из диаризации
        all_speakers: set[str] = set()
        for it in items:
            diar = it.get("diarization")
            if diar and isinstance(diar, dict):
                turns = diar.get("speaker_turns", [])
                for t in turns:
                    spk = t.get("speaker")
                    if spk:
                        all_speakers.add(spk)

        # Средняя уверенность
        confidences = [
            float(it["confidence"])
            for it in items
            if it.get("confidence") is not None
        ]
        avg_conf = round(sum(confidences) / len(confidences), 3) if confidences else None

        # Всего слов
        total_words = sum(len((it.get("text") or "").split()) for it in items)

        # Временной диапазон
        timestamps = sorted(
            it["ts"] for it in items if it.get("ts")
        )
        earliest = timestamps[0] if timestamps else None
        latest = timestamps[-1] if timestamps else None

        return {
            "total": total,
            "translated": translated,
            "favorites": favorites,
            "speakers": len(all_speakers),
            "avg_confidence": avg_conf,
            "total_words": total_words,
            "earliest": earliest,
            "latest": latest,
        }

    # ------------------------------------------------------------------
    # Header
    # ------------------------------------------------------------------

    def _render_header(
        self,
        title: str,
        stats: dict[str, Any],
        items: list[dict[str, Any]],
    ) -> str:
        safe_title = html.escape(title)
        generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        earliest = stats.get("earliest") or ""
        latest = stats.get("latest") or ""
        date_range = ""
        if earliest and latest and earliest != latest:
            date_range = f"<span>{html.escape(earliest[:10])} — {html.escape(latest[:10])}</span>"
        elif earliest:
            date_range = f"<span>{html.escape(earliest[:10])}</span>"

        return f"""
<div class="report-header">
  <div class="container">
    <h1>{safe_title}</h1>
    <div class="subtitle">История транскрипций Krab Ear</div>
    <div class="meta">
      <span>Записей: <strong>{stats['total']}</strong></span>
      {date_range}
      <span>Сгенерировано: {generated_at}</span>
    </div>
  </div>
</div>
"""

    # ------------------------------------------------------------------
    # Stats card
    # ------------------------------------------------------------------

    def _render_stats_card(self, stats: dict[str, Any]) -> str:
        avg_conf_str = (
            f"{stats['avg_confidence'] * 100:.1f}%"
            if stats["avg_confidence"] is not None
            else "—"
        )
        grid_items = [
            ("total", str(stats["total"]), "Всего записей"),
            ("translated", str(stats["translated"]), "Переведено"),
            ("favorites", str(stats["favorites"]), "Избранное"),
            ("speakers", str(stats["speakers"]) if stats["speakers"] else "—", "Спикеров"),
            ("total_words", f"{stats['total_words']:,}", "Слов"),
            ("avg_confidence", avg_conf_str, "Ср. уверенность"),
        ]
        cells_html = "\n".join(
            f'<div class="stat-item"><div class="stat-value">{html.escape(v)}</div>'
            f'<div class="stat-label">{html.escape(lbl)}</div></div>'
            for _, v, lbl in grid_items
        )
        return f"""
<div class="stats-card">
  <h2>Сводная статистика</h2>
  <div class="stats-grid">
    {cells_html}
  </div>
</div>
"""

    # ------------------------------------------------------------------
    # Daily recap
    # ------------------------------------------------------------------

    def _render_daily_recap(self, digest: dict[str, Any]) -> str:
        """Render a «Сводка дня» card from a generate_daily_digest payload.

        Mirrors the in-app Daily Recap section: three metric tiles + language /
        topic chips + highlights.  Every user-derived string is html.escape'd —
        top_topics/highlights are extracted from raw transcript text, so without
        escaping they could inject markup into this standalone HTML file when
        opened in a browser (stored-XSS, defense-in-depth).
        """
        if not isinstance(digest, dict):
            return ""

        date_str = html.escape(str(digest.get("date") or ""))
        try:
            recordings_int = int(digest.get("total_recordings", 0) or 0)
        except (TypeError, ValueError):
            recordings_int = 0
        try:
            duration_str = f"{float(digest.get('total_duration_min', 0) or 0):.1f}"
        except (TypeError, ValueError):
            duration_str = "0.0"
        try:
            words_int = int(digest.get("total_words", 0) or 0)
        except (TypeError, ValueError):
            words_int = 0

        tiles = [
            (str(recordings_int), "Записей"),
            (duration_str, "Минут"),
            (f"{words_int:,}", "Слов"),
        ]
        tiles_html = "\n".join(
            f'<div class="stat-item"><div class="stat-value">{html.escape(v)}</div>'
            f'<div class="stat-label">{html.escape(lbl)}</div></div>'
            for v, lbl in tiles
        )

        blocks = ""

        langs = digest.get("languages_used")
        if isinstance(langs, dict) and langs:
            ordered = sorted(
                langs.items(), key=lambda kv: (-int(kv[1] or 0), str(kv[0]))
            )
            chips = "".join(
                f'<span class="recap-chip">{html.escape(str(code))} · '
                f'{int(cnt or 0)}</span>'
                for code, cnt in ordered
            )
            blocks += (
                '<div class="recap-block-title">Языки</div>'
                f'<div class="recap-chips">{chips}</div>'
            )

        topics = digest.get("top_topics")
        if isinstance(topics, list) and topics:
            chips = "".join(
                f'<span class="recap-chip">{html.escape(str(t))}</span>'
                for t in topics if str(t).strip()
            )
            if chips:
                blocks += (
                    '<div class="recap-block-title">Темы</div>'
                    f'<div class="recap-chips">{chips}</div>'
                )

        highlights = digest.get("highlights")
        if isinstance(highlights, list) and highlights:
            lis = "\n".join(
                f"<li>{html.escape(str(h))}</li>"
                for h in highlights if str(h).strip()
            )
            if lis:
                blocks += (
                    '<div class="recap-block-title">Главное</div>'
                    f'<ul class="recap-highlights">{lis}</ul>'
                )

        date_suffix = f" — {date_str}" if date_str else ""
        return f"""
<div class="stats-card">
  <h2>Сводка дня{date_suffix}</h2>
  <div class="stats-grid">
    {tiles_html}
  </div>
  {blocks}
</div>
"""

    # ------------------------------------------------------------------
    # Timeline
    # ------------------------------------------------------------------

    def _render_timeline(self, items: list[dict[str, Any]]) -> str:
        if not items:
            return ""
        # Показываем до 20 последних в хронологическом порядке
        timeline_items = items[:20]
        rows = []
        for it in timeline_items:
            ts = it.get("ts", "")
            ts_short = ts[11:16] if len(ts) >= 16 else ts[:16]
            text = (it.get("text") or "").strip()
            text_preview = html.escape(text[:80] + ("…" if len(text) > 80 else ""))
            rows.append(
                f'<div class="timeline-item">'
                f'<span class="timeline-ts">{html.escape(ts_short)}</span>'
                f'<span class="timeline-text">{text_preview}</span>'
                f"</div>"
            )
        rows_html = "\n".join(rows)
        return f"""
<div class="section-title">Временная линия</div>
<div class="timeline">
  {rows_html}
</div>
"""

    # ------------------------------------------------------------------
    # Entries
    # ------------------------------------------------------------------

    def _render_entries(self, items: list[dict[str, Any]]) -> str:
        if not items:
            return '<p style="color:var(--text3);text-align:center;padding:40px 0">Нет записей</p>'

        cards = []
        for idx, it in enumerate(items, start=1):
            cards.append(self._render_entry_card(it, idx))
        cards_html = "\n".join(cards)
        return f"""
<div class="section-title">Записи ({len(items)})</div>
<div class="entries-list">
  {cards_html}
</div>
"""

    def _render_entry_card(self, it: dict[str, Any], idx: int) -> str:
        is_favorite = bool(it.get("favorite"))
        card_cls = "entry-card favorite" if is_favorite else "entry-card"

        # Временна́я метка
        ts = it.get("ts", "")
        ts_display = ts.replace("T", " ")[:19] if len(ts) >= 16 else ts

        # Значки спикеров из диаризации
        speaker_badges_html = self._render_speaker_badges(it)

        # Текст
        text = html.escape((it.get("text") or "").strip())

        # Перевод
        translation_html = ""
        translated_text = (it.get("translated_text") or "").strip()
        if translated_text:
            engine = it.get("translation_engine") or ""
            engine_label = f" ({html.escape(engine)})" if engine else ""
            translation_html = (
                f'<div class="entry-translation">'
                f'<div class="translation-label">Перевод{engine_label}</div>'
                f"{html.escape(translated_text)}"
                f"</div>"
            )

        # Полоса уверенности
        confidence_html = self._render_confidence_bar(it.get("confidence"))

        # Теги
        tags_html = ""
        tags = it.get("tags") or []
        if tags:
            tag_spans = " ".join(
                f'<span class="tag">{html.escape(str(t))}</span>' for t in tags
            )
            tags_html = f'<div class="tag-list">{tag_spans}</div>'

        return f"""<div class="{card_cls}">
  <div class="entry-meta">
    <span class="entry-index">#{idx}</span>
    <span class="entry-ts">{html.escape(ts_display)}</span>
    {"⭐" if is_favorite else ""}
    {speaker_badges_html}
  </div>
  <div class="entry-text">{text}</div>
  {translation_html}
  {confidence_html}
  {tags_html}
</div>"""

    def _render_speaker_badges(self, it: dict[str, Any]) -> str:
        diar = it.get("diarization")
        if not diar or not isinstance(diar, dict):
            return ""
        turns = diar.get("speaker_turns", [])
        speakers_seen: list[str] = []
        for t in turns:
            spk = t.get("speaker")
            if spk and spk not in speakers_seen:
                speakers_seen.append(spk)
        badges = []
        for i, spk in enumerate(speakers_seen):
            color = _SPEAKER_COLORS[i % len(_SPEAKER_COLORS)]
            badges.append(
                f'<span class="speaker-badge" style="background:{color}">'
                f"{html.escape(spk)}"
                f"</span>"
            )
        return " ".join(badges)

    def _render_confidence_bar(self, confidence: float | None) -> str:
        if confidence is None:
            return ""
        pct = max(0.0, min(1.0, float(confidence))) * 100
        if pct >= 75:
            cls = "conf-high"
        elif pct >= 45:
            cls = "conf-mid"
        else:
            cls = "conf-low"
        return (
            f'<div class="confidence-bar-wrap">'
            f'<span class="confidence-label">Уверенность</span>'
            f'<div class="confidence-bar">'
            f'<div class="confidence-fill {cls}" style="width:{pct:.1f}%"></div>'
            f"</div>"
            f'<span class="confidence-pct">{pct:.0f}%</span>'
            f"</div>"
        )

    # ------------------------------------------------------------------
    # Word cloud placeholder
    # ------------------------------------------------------------------

    def _render_wordcloud(self, items: list[dict[str, Any]]) -> str:
        # Частотный анализ слов (топ-30) как текстовый placeholder
        from collections import Counter

        stop_words = {
            "и", "в", "не", "на", "я", "что", "с", "это", "а", "то", "по", "он",
            "как", "но", "из", "к", "у", "за", "так", "же", "от", "для", "all",
            "the", "is", "in", "it", "of", "and", "to", "a", "that", "was",
        }
        words: list[str] = []
        for it in items:
            text = (it.get("text") or "").lower()
            for w in text.split():
                w = w.strip(".,!?;:—-\"'()")
                if len(w) >= 3 and w not in stop_words:
                    words.append(w)

        counter = Counter(words)
        top_words = counter.most_common(30)

        if not top_words:
            placeholder = '<div class="wordcloud-placeholder"><div class="icon">☁️</div><p>Недостаточно данных для облака слов</p></div>'
            return f'<div class="section-title">Облако слов</div>\n{placeholder}\n'

        max_count = top_words[0][1] if top_words else 1
        word_spans = []
        for w, cnt in top_words:
            size_em = 0.75 + (cnt / max_count) * 1.5
            word_spans.append(
                f'<span class="wc-word" style="font-size:{size_em:.2f}em">{html.escape(w)}</span>'
            )
        words_html = "\n".join(word_spans)

        return f"""
<div class="section-title">Облако слов</div>
<div class="wordcloud-placeholder">
  <div class="icon">☁</div>
  <p>Топ-{len(top_words)} слов по частоте</p>
  <div class="wordcloud-words">
    {words_html}
  </div>
</div>
"""

    # ------------------------------------------------------------------
    # Footer
    # ------------------------------------------------------------------

    def _render_footer(self, stats: dict[str, Any]) -> str:
        generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        return f"""
<footer class="report-footer">
  <span>Krab Ear &mdash; HTML Report</span>
  <span>Сгенерировано {generated_at}</span>
</footer>
"""

    # ------------------------------------------------------------------
    # Document wrapper
    # ------------------------------------------------------------------

    def _wrap_document(self, title: str, body: str) -> str:
        safe_title = html.escape(title)
        return f"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{safe_title}</title>
  <style>
{_CSS}
  </style>
</head>
<body>
{body}
</body>
</html>"""
