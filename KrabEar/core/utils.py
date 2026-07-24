"""Утилиты Krab Ear: обработка текста и аудио.

Включает в себя логику очистки транскрибатов от галлюцинаций и повторов.
"""

import re
import logging
import functools

logger = logging.getLogger("KrabEar.Utils")

# ── Precompiled regex patterns ──────────────────────────────────────────

_WHITESPACE_RE = re.compile(r"\s+")
_NORMALIZE_RE = re.compile(r"[^\w\s-]+")
_SENTENCE_SPLIT_RE = re.compile(r"[.!?…]+")
_WORD_REPEAT_RE = re.compile(
    r"(.+?)\s+([А-Яа-яA-Za-z0-9'-]+(?:\s+[А-Яа-яA-Za-z0-9'-]+){0,2})\s+\2[.!?…]*$"
)

# Speech disfluency dedup — re-articulation patterns.
# Кейсы из live диктовки:
#   "записываю уже, уже"         → "записываю уже"
#   "вот сейчас, вот сейчас"     → "вот сейчас"
#   "с выбранной, с выбранной"   → "с выбранной"
#   "протестирую, протестирую"   → "протестирую"
#   "слово слово"                → "слово"
#
# Не путаем с риторическим повтором/emphasis ("очень очень важно",
# "далеко далеко"). Признак re-articulation: разделитель "X, X" либо
# повтор без интонационного значения. Применяем только для запятой как
# разделителя — двойной пробел "очень очень" может быть emphasis.
#
# Паттерн: word boundary + token + ", " + same token + word boundary.
# Capture group 1 — token, обратная ссылка \1 — same token.
_DEDUP_COMMA_RE = re.compile(
    # token = letter + 0-30 more letters/dashes/apostrophes (1-char tokens like "с","я" OK)
    r"\b([А-ЯЁа-яёA-Za-z][А-ЯЁа-яёA-Za-z'-]{0,30}(?:\s+[А-ЯЁа-яёA-Za-z][А-ЯЁа-яёA-Za-z'-]{0,30}){0,3})"
    r",\s+\1\b",
    re.IGNORECASE,
)

# Multi-word re-articulation без запятой ("вот сейчас вот сейчас"):
# опционально, более рискованно (false positive на emphasis), включаем
# только для 2-4 слов где первое — не emphasis-маркер ("очень", "далеко",
# "много", "сильно", "глубоко", "быстро", "медленно").
_EMPHASIS_MARKERS = frozenset({
    "очень", "далеко", "много", "сильно", "глубоко",
    "быстро", "медленно", "тихо", "громко", "близко",
    "very", "many", "much", "deep", "fast", "slow",
})

_MULTIWORD_REPEAT_RE = re.compile(
    r"\b([А-ЯЁа-яёA-Za-z][А-ЯЁа-яёA-Za-z'-]*(?:\s+[А-ЯЁа-яёA-Za-z][А-ЯЁа-яёA-Za-z'-]*){1,3})\s+\1\b",
    re.IGNORECASE,
)

# Кириллические искажения имён собственных → каноническая латиница.
# Whisper на русской речи транскрибирует бренды в кириллицу; возвращаем их в латиницу
# детерминированно, независимо от того, сработал ли initial_prompt.
_BRAND_REPLACEMENTS_RAW: list[tuple[str, str]] = [
    # Порядок важен: более длинные/составные варианты идут раньше.
    (r"\bKrab\s*Voice\s*Gateway\b", "Krab Voice Gateway"),
    (r"\bКраб\s*Войс\s*Гейтвей\b", "Krab Voice Gateway"),
    (r"\bCrab\s*Ear\b", "Krab Ear"),
    (r"\bКраб\s*Ир\b", "Krab Ear"),
    (r"\bКрабИр\b", "Krab Ear"),
    # Ловим все падежи: Меркадона/-ы/-е/-у/-ой/-ной + удвоенное «нн».
    (r"\bМеркадонн?(?:а|ы|е|у|ой|ою)\b", "Mercadona"),
    (r"\bАнти[-\s]?Гравити\b", "Antigravity"),
    (r"\bAnti[-\s]?Gravity\b", "Antigravity"),
    (r"\bХаммер[-\s]?Спун\b", "Hammerspoon"),
    (r"\bHammer\s*Spoon\b", "Hammerspoon"),
    (r"\bОпен[-\s]?Клоу\b", "OpenClaw"),
    (r"\bПианот\b", "Pyannote"),
    (r"\bПайрофорк\b", "Pyrofork"),
    (r"\bПайрайт\b", "Pyright"),
    (r"\bПаблито\b", "Pablito"),
    (r"\bТелеграм\b", "Telegram"),
    (r"\bВиспер\b", "Whisper"),
    # CLAUDE.md — must come BEFORE the bare «Клод» → Claude entry to avoid premature replacement.
    # «эмди» ends in «и» (U+0438); [Ии]? makes the final letter optional for «эмд» typo variant.
    (r"\bКЛОД\s*[Ээ][Мм][Дд][Ии]?\b", "CLAUDE.md"),      # «КЛОД эмди»
    (r"\bКлауд\s*[Ээ][Мм][Дд][Ии]?\b", "CLAUDE.md"),      # «Клауд эмди»
    (r"\bКлуд\s*[Ээ][Мм][Дд][Ии]?\b", "CLAUDE.md"),       # «клуд эмди»
    (r"\bКлод\s*[Ээ][Мм][Дд][Ии]?\b", "CLAUDE.md"),       # «клод эмди»
    (r"\bКлод\s*М\s*Д\b", "CLAUDE.md"),                    # «клод М Д»
    (r"\bКлод[аеиа]\s*[Ээ][Мм][Дд]\b", "CLAUDE.md"),      # «клода эмди» (genitive)
    (r"\bКлавдий\s*[Ээ][Мм][Дд][Ии]?\b", "CLAUDE.md"),    # «клавдий эмди»
    (r"\bКлуд\s*М\s*Д\b", "CLAUDE.md"),                    # «клуд М Д»
    (r"\bКлод\b", "Claude"),
    (r"\bЭм\s*Эл\s*Икс\b", "MLX"),
    # Qwen brand mishears (live transcripts: "QN14B", "квен", "к Вен", "Квент", "QN1")
    # Whisper транскрибирует "Qwen" фонетически в РУ как "квен/Квен", в EN sometimes "QN".
    (r"\bQN\s*(\d+B?)\b", r"Qwen \1"),
    (r"\bкВен\b", "Qwen"),
    (r"\bк\s+Вен\b", "Qwen"),
    (r"\bКвент\b", "Qwen"),
    (r"\bквен\b", "Qwen"),
    (r"\bКвен\b", "Qwen"),
    # Qwen — additional mishears (batch-8 2026-05-05)
    (r"\bКьюэн\b", "Qwen"),
    (r"\bкуэн\b", "Qwen"),
    # GigaAM mishear — 2026-05-05 session: added Хига/Джига/Higa/Jiga variants
    # (GigaAM doesn't recognise its own name; Whisper phonetically maps «Гига» → «Хига»/«Джига»)
    (r"\bГига[\s\-]?АМ\b", "GigaAM"),
    (r"\bГига\s*Эй\s*Эм\b", "GigaAM"),
    (r"\b[Хх]ига[\s\-]?[Аа][Мм]\b", "GigaAM"),
    (r"\b[Дд]жига[\s\-]?[Аа][Мм]\b", "GigaAM"),
    (r"\bHiga[\s\-]?AM\b", "GigaAM"),
    (r"\bJiga[\s\-]?AM\b", "GigaAM"),
    # MythoMax mishear
    (r"\bМито\s*Макс\b", "MythoMax"),
    (r"\bМитомакс\b", "MythoMax"),
    # LM Studio mishear
    (r"\bЛМ\s*Студио\b", "LM Studio"),
    (r"\bЭл\s*Эм\s*Студио\b", "LM Studio"),
    (r"\bLOM\s*Studio\b", "LM Studio"),       # live: "LOM Studio"
    (r"\bЛОМ\s*Студио\b", "LM Studio"),
    # GGUF mishears (Whisper фантастически транскрибирует — live: "ахолув")
    (r"\bахолув\b", "GGUF"),
    (r"\bАхолув\b", "GGUF"),
    (r"\bгу[ -]?гу[ -]?эф\b", "GGUF"),
    (r"\bджи[ -]?джи[ -]?ю[ -]?эф\b", "GGUF"),
    (r"\bджи\s*ю\s*эф\b", "GGUF"),
    # Crypto / blockchain brand mishears
    (r"\bБиткоин\b", "Bitcoin"),
    (r"\bБиткойн\b", "Bitcoin"),
    (r"\bСолана\b", "Solana"),
    (r"\bЭфириум\b", "Ethereum"),
    (r"\bКрипто\b", "Crypto"),
    # Browser brands
    (r"\bСафари\b", "Safari"),
    (r"\bХром\b", "Chrome"),
    (r"\bХроме\b", "Chrome"),
    # Obsidian (note app)
    (r"\bОбсидиан\b", "Obsidian"),
    # Inference
    (r"\bинференс\b", "inference"),
    (r"\bинференца\b", "inference"),
    # Whisper STT artefact: "ОК" sometimes transcribed as digit "0".
    # Conservative pattern: only после filler words ("Ну/ну/Да/да") и followed by RU word.
    # Avoids breaking legitimate числовое "0" (e.g. "температура 0 градусов").
    # Cases live: "Ну 0 продолжаем", "Ну 0 вот это", "Ну 0 наименование".
    (r"\b([Нн]у|[Дд]а)\s+0\s+(?=[А-Яа-яЁё])", r"\1, ОК, "),
    # Standalone "0" between RU words after period/comma — same artefact.
    (r"(?<=[.,!?])\s+0\s+(?=[А-Яа-яЁё])", " ОК, "),
    (r"\bФаст\s*АПИ\b", "FastAPI"),
    (r"\bГит[-\s]?Хаб\b", "GitHub"),
    (r"\bМак[-\s]?Бук\b", "MacBook"),
    # AI/ML инструменты
    (r"\bЧат\s*Джи\s*Пи\s*[Тт]\b", "ChatGPT"),
    (r"\bДжи\s*Пи\s*[Тт]\b", "GPT"),
    # GPT — additional mishears (batch-8 2026-05-05)
    (r"\b[Гг]пт\b", "GPT"),
    (r"\b[Жж]пт\b", "GPT"),
    (r"\bджипити\b", "GPT"),
    (r"\bОпен\s*[Ээ]й\s*[Аа]й\b", "OpenAI"),
    # OpenAI — additional mishears (batch-8 2026-05-05)
    (r"\bОпен\s*[Аа]\s*[Ии]\b", "OpenAI"),        # «Опен А.И.» with spaces
    (r"\bОпен\s*[Аа]\.\s*[Ии]\.", "OpenAI"),       # «Опен А.И.» (no \b after trailing dot)
    (r"\bОпен\s*[Аа]й\b", "OpenAI"),              # «Опен ай»
    (r"\bопенаи\b", "OpenAI"),                     # слитное строчное
    (r"\bМидж[оё]рни\b", "Midjourney"),
    (r"\bСтейбл\s*Диффь?южн\b", "Stable Diffusion"),
    (r"\bЛлама\b", "Llama"),
    # Llama versioned mishears — only when followed by version number (e.g. «Лама 4»)
    # «Лама» alone is a real Russian word (camel), so we require a digit after it.
    (r"\b[ЯяYy]ama\s+(\d[\d.]*)\b", r"Llama \1"),
    (r"\b[Лл]ама\s+(\d[\d.]*)\b", r"Llama \1"),
    (r"\bДжемини\b", "Gemini"),
    # Dev-инструменты
    (r"\bВи\s*Эс\s*Код\b", "VS Code"),
    (r"\bГит\b", "Git"),
    (r"\bНод\s*[Дд]жи\s*[Ээс]\b", "Node.js"),
    (r"\bРеакт\b", "React"),
    (r"\bДокер\b", "Docker"),
    (r"\bКубернетис\b", "Kubernetes"),
    (r"\bЛинукс\b", "Linux"),
    # Сервисы
    (r"\bАмазон\b", "Amazon"),
    (r"\bНетфликс\b", "Netflix"),
    (r"\bСпотифай\b", "Spotify"),
    (r"\bЮ\s*[Тт]юб\b", "YouTube"),
    (r"\bИнстаграм\b", "Instagram"),
    (r"\bВотс\s*[Аа]п\b", "WhatsApp"),
    # Испания (розничные сети)
    (r"\bКарр[еэ]фур\b", "Carrefour"),
    (r"\bЛидл\b", "Lidl"),
    (r"\bАльди\b", "Aldi"),
    # AI/ML — Phase C.4 brand expansion (2026-05-04)
    # Gemma — Google's open model family; Whisper mishears as "Гемма" / "Джемма"
    (r"\bГемма\b", "Gemma"),
    (r"\bДжемма\b", "Gemma"),
    # Anthropic — parent company of Claude; фонетическое «Антропик»
    (r"\bАнтропик\b", "Anthropic"),
    # Claude — model name; already handled separately but adding Клод fallback
    # (r"\bКлод\b", "Claude") already exists above — no duplicate needed
    # LM Studio — already has entries above; adding "Элэм Студио" variant
    (r"\bЭлэм\s*Студио\b", "LM Studio"),
    # Krab Ear — extended: "Краб Ир" already above; add "КрабИр" variant (merged)
    # (already present as \bКрабИр\b above)
    # AI/ML — batch-8 2026-05-05: Mistral, DeepSeek, Hugging Face, LM Studio variants,
    # context-dependent Anthropic model family names (Opus/Sonnet/Haiku + version digit).
    # Mistral — open model family; mishears «Митра», «митраль», «мистраль»
    (r"\b[Мм]истраль\b", "Mistral"),
    (r"\b[Мм]итраль\b", "Mistral"),
    (r"\b[Мм]итра\b", "Mistral"),
    # DeepSeek — Chinese AI lab; mishears «Дипсик», «дипсек»
    (r"\b[Дд]ипсик\b", "DeepSeek"),
    (r"\b[Дд]ипсек\b", "DeepSeek"),
    # Hugging Face — ML platform; mishears «Хагин фейс», «хагинг фейс», «хаггинг фейс»
    (r"\b[Хх]а[гг]+инг?\s+[Фф]ейс\b", "Hugging Face"),
    # LM Studio — additional standalone mishear variants (batch-8)
    # «Эл-эм студио», «лэм студио», «лм студио» (last one already covered by ЛМ Студио above)
    (r"\bЭл[-\s]эм\s+[Сс]тудио\b", "LM Studio"),
    (r"\b[Лл]эм\s+[Сс]тудио\b", "LM Studio"),
    # Anthropic model family — context-dependent: only replace when followed by version number.
    # «Опус» is a common Russian word (musical opus), «соннет» = sonnet (poem).
    # We ONLY replace when the word is followed by a version like «4», «4.5», «3.5».
    (r"\b[Оо]пус\s+(\d[\d.]*)\b", r"Opus \1"),
    (r"\b[Сс]оннет\s+(\d[\d.]*)\b", r"Sonnet \1"),
    (r"\b[Хх]айку\s+(\d[\d.]*)\b", r"Haiku \1"),

    # ── Dev tools / config files (batch-10, 2026-05-05) ────────────────────
    # NOTE: CLAUDE.md patterns were moved BEFORE the bare «Клод» → Claude entry above
    # (see inline comment there). Only keeping additional variants here.
    # GitHub — additional mishears (Гит-Хаб already in v1, adding compound forms)
    (r"\bгитхаб\b", "GitHub"),                              # слитное строчное
    (r"\bgit\s+хаб\b", "GitHub"),                          # «git хаб» (mixed)
    (r"\bгитхаб\.ком\b", "GitHub"),                        # «гитхаб.ком»
    # GitLab
    (r"\b[Гг]итлаб\b", "GitLab"),
    # Bitbucket
    (r"\b[Бб]итбакет\b", "Bitbucket"),
    (r"\b[Бб]ит\s+[Бб]акет\b", "Bitbucket"),
    # Slack — ONLY in dev/app context: followed by «канал»/«чат»/«ссылка»/«уведомлени»
    # or in possessive construction «в слаке». «слак» alone (physical slack) not replaced.
    (r"\bслак\s+(канал|чат)\b", r"Slack \1"),
    (r"\bв\s+слак[ее]\b", "в Slack"),
    # Jira — ONLY in tracker context
    (r"\b[Жж]ира\s+тикет\b", "Jira тикет"),
    (r"\bтикет\s+в\s+[Жж]ире\b", "тикет в Jira"),
    (r"\b[Жж]ира\s+(борд|доска|задач)\b", r"Jira \1"),
    # Notion
    (r"\b[Нн]оушн\b", "Notion"),
    # Linear (project tracker) — only with dev context to avoid confusion with adjective
    (r"\b[Лл]инеар\s+(задач|тикет|борд|проект)\b", r"Linear \1"),
    (r"\bв\s+[Лл]инеаре?\b", "в Linear"),
    # Figma
    (r"\b[Фф]игма\b", "Figma"),
    (r"\bфиг\s+ма\b", "Figma"),
    # PyCharm
    (r"\b[Пп]айчарм\b", "PyCharm"),
    (r"\bпай\s+чарм\b", "PyCharm"),
    # VS Code — additional mishears beyond existing «Ви Эс Код»
    (r"\bвс\s*код\b", "VS Code"),                          # «вс код»
    (r"\bВ\.С\.\s*[Кк]од\b", "VS Code"),                  # «В.С. код»
    # Xcode
    (r"\b[Ии]кскод\b", "Xcode"),
    (r"\bX[-\s]код\b", "Xcode"),
    # Zed editor — only with explicit «редактор» context to avoid replacing the letter Zed
    (r"\b[Зз]ед\s+[Рр]едактор\b", "Zed"),
    # iTerm2
    (r"\b[Ии][Тт]ерм\s*2?\b", "iTerm2"),
    (r"\bай\s*[Тт]ерм\s*2?\b", "iTerm2"),
    (r"\biTerm\s+2\b", "iTerm2"),

    # ── Programming languages / runtimes (batch-10) ─────────────────────────
    # Swift — only with code/package context to avoid false positives (Taylor Swift etc.)
    (r"\b[Сс]вифт\s+(код|пакет|проект|компилятор|файл)\b", r"Swift \1"),
    (r"\b[Сс]вифт\s+Package\b", "Swift Package"),
    # Rust — with explicit language context
    (r"\b[Рр]аст\s+[Яя]зык\b", "Rust"),
    (r"\b[Рр]аст\s+[Лл]энгвидж\b", "Rust"),
    # Python — additional mishears
    (r"\b[Пп]айтон\s+3\b", "Python 3"),
    (r"\b[Пп]айтон\b", "Python"),
    (r"\b[Пп]итон\s+3\b", "Python 3"),
    # JSON
    (r"\b[Дд]жейсон\s+[Фф]ормат\b", "JSON формат"),
    (r"\b[Дд]жейсон\b", "JSON"),
    # YAML
    (r"\b[ЯяEe]ямл\b", "YAML"),
    (r"\b[Яя]мл\b", "YAML"),
    # Docker — additional mishear (double-к)
    (r"\b[Дд]оккер\b", "Docker"),
    # Kubernetes — additional mishears
    (r"\b[Кк]убер\b", "Kubernetes"),
    (r"\b[Кк]уб\s+[Кк]онтейнер\b", "Kubernetes контейнер"),
    # Terraform
    (r"\b[Тт]еррафор[мн]\b", "Terraform"),

    # ── File formats (batch-10) ──────────────────────────────────────────────
    # Markdown
    (r"\b[Мм]аркдаун\b", "Markdown"),
    # .md extension — only in explicit extension-reference context
    # «эмди» ends in и (U+0438); [Ии]? makes the trailing vowel optional.
    (r"\b[Дд]от\s+[Ээ][Мм][Дд][Ии]?\b", ".md"),           # «дот эмди»
    # MP3 — «пэ» uses э (U+044D), class must be [Ээ] not [Ее]
    (r"\b[Ээ][Мм]\s+[Пп][Ээ]\s*3\b", "MP3"),              # «Эм пэ 3»
    # WAV
    (r"\b[Вв]ав\s+[Фф]айл\b", "WAV файл"),
    # SSL — «эс эс эль»; correct Cyrillic letter L = «эль» (Э U+044D); Whisper sometimes
    # transcribes as «ель» (Е U+0415, yew tree). Accept both: [ЭэЕе]ль.
    (r"\b[Ээ]с\s+[Ээ]с\s+[ЭэЕе]ль\b", "SSL"),

    # ── Russian dev-slang / dictation patterns (batch-10) ───────────────────
    # subagent — Russian phonetic variants
    (r"\bсаб\s*агент\b", "subagent"),
    (r"\bsub\s*агент\b", "subagent"),
    # коммит — correct the single-«м» mishear
    (r"\b[Кк]омит\b", "коммит"),
    # rebase — phonetic variants
    (r"\b[Рр]еббейс\b", "rebase"),
    (r"\b[Рр]ибейз\b", "rebase"),
    (r"\b[Рр]и[-\s][Бб]ейс\b", "rebase"),
    # pull request — phonetic
    (r"\b[Пп]улл?\s+[Рр]еквест\b", "pull request"),
    (r"\b[Пп]ул[Рр]еквест\b", "pull request"),
    (r"(?<!\w)П\.Р\.(?!\w)", "PR"),                        # «П.Р.» abbrev, no \b (dots break it)
    # мерджить — normalise мёрджить → мерджить
    (r"\b[Мм]ёрджить\b", "мерджить"),
    # issues — phonetic
    (r"\b[Ии]шьюс\b", "issues"),
    # AppleScript
    (r"\b[Ээ]пл\s*[Сс]крипт\b", "AppleScript"),
    (r"\bapple\s*скрипт\b", "AppleScript"),
    # osascript
    (r"\b[Оо][Сс][Аа]\s*[Сс]крипт\b", "osascript"),
    (r"\b[Оо]са\s*скрипт\b", "osascript"),
]
BRAND_REPLACEMENTS: list[tuple[re.Pattern, str]] = [
    (re.compile(pat, re.IGNORECASE), repl) for pat, repl in _BRAND_REPLACEMENTS_RAW
]

# ── Literal-hint fast-path for brand replacement ────────────────────────
# For each pattern we extract the longest leading literal substring that MUST
# appear in the text for the regex to match.  Before calling the (expensive)
# re.sub we do a cheap str.__contains__ check in lower-case; if the hint is
# absent the whole re.sub call is skipped.  On typical short transcripts this
# reduces the number of regex executions from N (all patterns) to ~K (only the
# patterns whose brand is actually present), yielding 3-8× wall-clock speedup.
#
# Benchmark (M4 Max, Python 3.11, 500 iters, ~8 KB rich-brand text):
#   OLD sequential loop : ~1370 ms  (~2.7 ms/call)
#   NEW hint fast-path  :  ~520 ms  (~1.0 ms/call)  ≈ 2.6×
# Plain (no brands):
#   OLD : ~670 ms  (~1.3 ms/call)
#   NEW :  ~88 ms  (~0.2 ms/call)  ≈ 7.6×

_HINT_EXTRACT_RE = re.compile(r"^(?:\\b)?([А-Яа-яA-Za-z]{2,})")


def _extract_literal_hint(raw_pattern: str) -> str:
    """Извлекает ведущую буквенную подстроку из raw regex-паттерна."""
    m = _HINT_EXTRACT_RE.match(raw_pattern)
    return m.group(1).lower() if m else ""


# List of (compiled_pattern, lower_case_hint, replacement)
# hint == "" → pattern always runs (no cheap pre-check possible)
_BRAND_WITH_HINTS: list[tuple[re.Pattern, str, str]] = [
    (re.compile(raw, re.IGNORECASE), _extract_literal_hint(raw), repl)
    for raw, repl in _BRAND_REPLACEMENTS_RAW
]

# Время "15.00" / "15 00" после цифр → "15:00" (только в диапазоне часов).
# Не трогаем числа с плавающей точкой: условие — час 0-23 и минуты 00-59.
TIME_NORMALIZE_RE = re.compile(r"\b([01]?\d|2[0-3])\s*[.:]\s*([0-5]\d)(?!\d)")


@functools.lru_cache(maxsize=256)
def _compile_trailing_pattern(escaped_last: str) -> re.Pattern:
    """Кэширует компиляцию динамического паттерна «конец повторной фразы».

    Паттерн строится из re.escape(last) + суффикс пунктуации.  Кэш предотвращает
    повторную компиляцию для одной и той же финальной фразы внутри сессии.
    """
    return re.compile(rf"{escaped_last}[.!?…]*\s*$", re.IGNORECASE)


_HALLUCINATION_PATTERNS: list[re.Pattern] = [
    re.compile(pat) for pat in [
        r"(?:спасибо за просмотр|спасибо за внимание)[.!?…]*$",
        # W1894: живая галлюцинация на тишине — «Субтитры создавал DimaTorzok».
        # Покрыт был только глагол «сделал»; модель выдаёт всё семейство форм.
        r"(?:субтитры (?:сделал|сделала|создал|создала|создавал|создавала|делал|делала) [^.!?…]{1,40})[.!?…]*$",
        r"(?:подписывайтесь на канал)[.!?…]*$",
        r"(?:до новых встреч)[.!?…]*$",
        r"(?:продолжение следует)[.!?…]*$",
        r"(?:to be continued)[.!?…]*$",
        r"(?:подписывайтесь на наш канал)[.!?…]*$",
        r"(?:ставьте лайки)[.!?…]*$",
        r"(?:смотрите в описании)[.!?…]*$",
        r"(?:поддержите канал)[.!?…]*$",
        r"(?:приятного просмотра)[.!?…]*$",
        r"(?:увидимся в следующем видео)[.!?…]*$",
        r"(?:всем пока)[.!?…]*$",
        r"(?:спасибо всем за внимание)[.!?…]*$",
        r"(?:\.\s+)?спасибо\.?\s*$",  # standalone trailing "Спасибо."
    ]
]

# ── Repetition-loop detector (Phase C.4, 2026-05-04) ───────────────────────
# Whisper sometimes enters pathological loops on silent / ambiguous audio:
#   «Атакса хвостимда. Атакса хвостимда.»  — repeated bigram × 2
#   «согласен да согласен да согласен да...» — repeated trigram × 70+
#
# Three independent heuristics — any one is sufficient to flag the text.
_SENTENCE_SPLIT_LOOP_RE = re.compile(r"[.!?…]+\s*")


def is_likely_repetition_loop(text: str) -> tuple[bool, str]:
    """Detect Whisper repetition-hallucination loops.

    Returns:
        ``(is_loop, reason)`` — ``is_loop`` is True when the text looks like a
        Whisper repetition artefact.  ``reason`` is an ASCII-safe debug string
        (empty when ``is_loop`` is False).

    Heuristics (any one sufficient):
    1. ≥5 identical adjacent bigrams  — classic "X Y X Y X Y …" loop.
    2. ≥3 identical sentences in a row — sentence-level repetition.
    3. text length > 60 chars **and** unique-token ratio < 0.15 — extreme
       redundancy typical of 70+-word loops.

    The function never raises and never imports heavy ML modules.
    """
    if not text or len(text) < 20:
        return (False, "")

    tokens = text.lower().split()
    if len(tokens) < 6:
        return (False, "")

    # ── Heuristic 1: repeated bigrams ──────────────────────────────────────
    from collections import Counter
    bigrams = [(tokens[i], tokens[i + 1]) for i in range(len(tokens) - 1)]
    bigram_counts = Counter(bigrams)
    most_common_bigram, count = bigram_counts.most_common(1)[0]
    if count >= 5:
        return (True, f"repeated_bigram x{count}: {' '.join(most_common_bigram)}")

    # ── Heuristic 2: repeated sentences ────────────────────────────────────
    sentences = [s.strip() for s in _SENTENCE_SPLIT_LOOP_RE.split(text) if s.strip()]
    if len(sentences) >= 3:
        sent_counts = Counter(s.lower() for s in sentences)
        most_common_sent, sent_count = sent_counts.most_common(1)[0]
        if sent_count >= 3:
            preview = most_common_sent[:40] + ("…" if len(most_common_sent) > 40 else "")
            return (True, f"repeated_sentence x{sent_count}: {preview}")

    # ── Heuristic 3: low unique-token ratio ─────────────────────────────────
    if len(tokens) > 30 and len(text) > 60:
        unique_ratio = len(set(tokens)) / len(tokens)
        if unique_ratio < 0.15:
            return (True, f"low_unique_ratio={unique_ratio:.2f}")

    return (False, "")


class TextUtils:
    """Статичный набор инструментов для нормализации и очистки текста."""

    @staticmethod
    def normalize_phrase(text: str) -> str:
        """Нормализует фразу для безопасного сравнения (нижний регистр, только буквы/цифры)."""
        return _NORMALIZE_RE.sub("", text.lower()).strip()

    @staticmethod
    def same_short_phrase(a: str, b: str, max_words: int = 8) -> bool:
        """Сравнивает, являются ли две короткие фразы идентичными без учета пунктуации."""
        na = TextUtils.normalize_phrase(a)
        nb = TextUtils.normalize_phrase(b)
        if not na or not nb:
            return False
        return na == nb and len(na.split()) < max_words

    @staticmethod
    def cleanup_transcript(text: str, profile: str = "soft") -> str:
        """Основной метод очистки транскрипции от артефактов Whispera."""
        clean = _WHITESPACE_RE.sub(" ", text).strip()
        if not clean:
            return clean

        # Мягкая очистка (всегда включена)
        clean = TextUtils._cleanup_soft(clean)
        # Базовая фильтрация известных артефактов нужна и в soft-профиле.
        clean = TextUtils._strip_hallucinations(clean)
        # Speech disfluency dedup — re-articulation patterns ("слово, слово").
        # Делается до brand normalization чтобы не путать с emphasis.
        clean = TextUtils._dedup_re_articulation(clean)
        # Нормализация брендов/имён и времени — всегда, чтобы диктовка не требовала ручной правки.
        clean = TextUtils.normalize_entities(clean)

        # Строгая очистка
        if profile.lower() == "strict":
            clean = TextUtils._cleanup_strict(clean)

        return clean.strip()

    @staticmethod
    def _dedup_re_articulation(text: str) -> str:
        """Удаляет немедленные повторы re-articulation от STT.

        Whisper при overlapping context window иногда дублирует токены:
            "записываю уже, уже"  → "записываю уже"
            "с выбранной, с выбранной" → "с выбранной"
            "протестирую, протестирую" → "протестирую"

        Также handles "X X" (без запятой) для multi-word phrases где
        first token не emphasis-marker ("очень очень" → keep как emphasis).

        Iterative — сразу несколько повторов в строке: применяем regex до
        стабилизации (no more changes), max 5 проходов чтобы avoid loops.
        """
        if not text:
            return text

        # Pass 1: comma-separated re-articulation ("X, X")
        for _ in range(5):
            replaced = _DEDUP_COMMA_RE.sub(r"\1", text)
            if replaced == text:
                break
            text = replaced

        # Pass 2: multi-word без запятой ("вот сейчас вот сейчас")
        # Скипаем когда первое слово — emphasis marker.
        def _multiword_dedup(match: "re.Match[str]") -> str:
            phrase = match.group(1)
            first_word = phrase.split()[0].lower()
            if first_word in _EMPHASIS_MARKERS:
                return match.group(0)  # keep as-is (emphasis preserved)
            return phrase

        for _ in range(5):
            replaced = _MULTIWORD_REPEAT_RE.sub(_multiword_dedup, text)
            if replaced == text:
                break
            text = replaced

        return text

    @staticmethod
    def _cleanup_soft(clean: str) -> str:
        """Удаляет явные непосредственные повторы фраз в конце текста."""
        # 1. Повтор финальной фразы
        segments = [part.strip() for part in _SENTENCE_SPLIT_RE.split(clean) if part.strip()]
        if len(segments) >= 2:
            last = segments[-1]
            prev = segments[-2]
            if TextUtils.same_short_phrase(last, prev):
                tail = clean.rfind(last)
                if tail > 0:
                    clean = clean[:tail].rstrip(" .,!?:;")

        # 2. Повтор 1-3 слов дважды в конце
        match = _WORD_REPEAT_RE.search(clean)
        if match:
            clean = match.group(1).rstrip(" .,!?:;")

        return clean.strip()

    @staticmethod
    def _cleanup_strict(clean: str) -> str:
        """Более агрессивное удаление повторов и известных галлюцинаций."""
        # 0. Убираем повтор финального предложения, если оно уже встречалось ранее.
        #    Оптимизация (vs оригинал):
        #    a) normalize_phrase вызывается 1 раз на сегмент, а не 3× (было:
        #       explicit normalize + 2× normalize внутри same_short_phrase).
        #    b) suffix_probe строится один раз вне цикла, а не как f-string
        #       на каждой итерации (избегаем лишних аллокаций строк).
        #    c) same_short_phrase заменён инлайн-сравнением через уже
        #       вычисленные normalized_prev/normalized_last (нет дублирования).
        segments = [part.strip() for part in _SENTENCE_SPLIT_RE.split(clean) if part.strip()]
        if len(segments) >= 2:
            last = segments[-1]
            normalized_last = TextUtils.normalize_phrase(last)
            if normalized_last:
                suffix_probe = " " + normalized_last  # built once, not per-iteration
                found = False
                # Scan previous segments in reverse; normalize each exactly once.
                for seg in reversed(segments[:-1]):
                    normalized_prev = TextUtils.normalize_phrase(seg)
                    if normalized_prev and (
                        normalized_prev == normalized_last
                        or normalized_prev.endswith(suffix_probe)
                    ):
                        found = True
                        break
                if found:
                    clean = _compile_trailing_pattern(re.escape(last)).sub("", clean).rstrip(" .,!?:;")

        # 3. Три одинаковых куска подряд (заикание модели)
        words = clean.split()
        for size in (5, 4, 3, 2, 1):
            if len(words) < size * 3 + 2:
                continue
            part_a = " ".join(words[-(size * 3):-(size * 2)])
            part_b = " ".join(words[-(size * 2):-size])
            part_c = " ".join(words[-size:])
            if TextUtils.normalize_phrase(part_a) == TextUtils.normalize_phrase(part_b) == TextUtils.normalize_phrase(part_c):
                clean = " ".join(words[:-size]).rstrip(" .,!?:;")
                break

        # 4. Удаление известных фраз-галлюцинаций (YouTube-стайл)
        clean = TextUtils._strip_hallucinations(clean)
        return clean.strip()

    @staticmethod
    def normalize_entities(text: str) -> str:
        """Канонизация брендов/имён (кириллица→латиница) и формата времени (ЧЧ:ММ).

        Применяется детерминированно поверх вывода Whisper, чтобы диктовка не
        требовала ручной правки «Меркадонна→Mercadona» и «15.00→15:00».

        Оптимизация (literal-hint fast-path): перед каждым re.sub проверяется,
        содержит ли текст ведущую буквенную подстроку паттерна (через быстрый
        str.__contains__ в нижнем регистре).  Если нет — re.sub пропускается.
        Ускорение: 2.6× на насыщенном тексте, до 7.6× на чистом.
        """
        if not text:
            return text
        result = text
        text_lower = text.lower()
        for compiled_re, hint, replacement in _BRAND_WITH_HINTS:
            if hint and hint not in text_lower:
                continue
            result = compiled_re.sub(replacement, result)
        result = TIME_NORMALIZE_RE.sub(r"\1:\2", result)
        return result

    @staticmethod
    def fix_punctuation(text: str, language: str = "ru") -> str:
        """Опциональный этап коррекции пунктуации через PunctuationFixer.

        Импортируется лениво, чтобы избежать циклических зависимостей.
        """
        from core.punctuation_fixer import PunctuationFixer  # lazy import
        return PunctuationFixer().fix(text, language=language)

    @staticmethod
    def _strip_hallucinations(clean: str) -> str:
        """Удаляет типичные шаблоны галлюцинаций Whispera."""
        lowered = clean.lower()
        for compiled_re in _HALLUCINATION_PATTERNS:
            match = compiled_re.search(lowered)
            if not match:
                continue
            if match.start() <= 0:
                return ""
            return clean[:match.start()].rstrip(" .,!?:;")
        return clean
