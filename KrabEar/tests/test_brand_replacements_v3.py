"""Тесты расширения _BRAND_REPLACEMENTS_RAW (batch-10, 2026-05-05).

Покрывает новые записи: CLAUDE.md, GitHub/GitLab/Bitbucket, Notion, Figma, PyCharm,
VS Code (доп. варианты), Xcode, iTerm2, Zed, Swift/Rust/Python/JSON/YAML/Docker/
Kubernetes/Terraform (языки), Markdown/.md/MP3/WAV/SSL (форматы),
subagent/коммит/rebase/pull request/issues/AppleScript/osascript (dev-сленг).

Запуск:
    PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_brand_replacements_v3.py -v
"""
from __future__ import annotations

import os
import sys
import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.utils import TextUtils  # noqa: E402


def ne(text: str) -> str:
    """Shortcut: normalize_entities."""
    return TextUtils.normalize_entities(text)


# ---------------------------------------------------------------------------
# Positive cases — must be replaced
# ---------------------------------------------------------------------------
POSITIVE_CASES: list[tuple[str, str, str]] = [
    # (id, input, expected_substring)

    # CLAUDE.md mishears
    ("claude_md_klod_emdi", "открой клод эмди", "CLAUDE.md"),
    ("claude_md_klaud_emdi", "Клауд эмди обновили", "CLAUDE.md"),
    ("claude_md_klud_emdi", "клуд эмди файл", "CLAUDE.md"),
    ("claude_md_klod_m_d", "правь клод М Д", "CLAUDE.md"),
    ("claude_md_klavdiy", "Клавдий эмди нашли", "CLAUDE.md"),

    # GitHub additional mishears
    ("github_gitkhab_lower", "залей в гитхаб", "GitHub"),
    ("github_git_hub_mixed", "git хаб ссылка", "GitHub"),
    ("github_gitkhab_com", "гитхаб.ком страница", "GitHub"),

    # GitLab
    ("gitlab_capital", "Гитлаб репо", "GitLab"),
    ("gitlab_lower", "гитлаб пайплайн", "GitLab"),

    # Bitbucket
    ("bitbucket_capital", "Битбакет репозиторий", "Bitbucket"),
    ("bitbucket_lower", "битбакет клон", "Bitbucket"),
    ("bitbucket_space", "бит бакет сервер", "Bitbucket"),

    # Slack — with context words
    ("slack_kanal", "слак канал обновился", "Slack"),
    ("slack_chat", "слак чат переписка", "Slack"),
    ("slack_v_slake", "напишу в слаке", "в Slack"),

    # Jira — with context
    ("jira_tiket", "Жира тикет создан", "Jira тикет"),
    ("jira_v_zhire", "тикет в жире закрыт", "тикет в Jira"),

    # Notion
    ("notion_noush_n", "Ноушн страница", "Notion"),
    ("notion_lower", "ноушн документ", "Notion"),

    # Figma
    ("figma_capital", "Фигма макет", "Figma"),
    ("figma_spaced", "фиг ма дизайн", "Figma"),
    ("figma_lower", "фигма экспорт", "Figma"),

    # PyCharm
    ("pycharm_capital", "Пайчарм открой", "PyCharm"),
    ("pycharm_spaced", "пай чарм плагин", "PyCharm"),

    # VS Code additional mishears
    ("vscode_vs_kod", "вс код терминал", "VS Code"),
    ("vscode_v_s_kod_dot", "В.С. Код расширение", "VS Code"),

    # Xcode
    ("xcode_ikskod", "Икскод проект", "Xcode"),
    ("xcode_x_kod", "X-код архив", "Xcode"),

    # iTerm2
    ("iterm2_iterm", "итерм открыть", "iTerm2"),
    ("iterm2_ai_term", "ай терм 2 сессия", "iTerm2"),
    ("iterm2_iterm_capital", "открой иТерм", "iTerm2"),

    # Zed editor — with explicit «редактор» context
    ("zed_redaktor", "зед редактор быстрый", "Zed"),

    # Swift — only with code context
    ("swift_kod", "свифт код скомпилировать", "Swift"),
    ("swift_paket", "Swift пакет установить", "Swift"),

    # Rust
    ("rust_yazyk", "Раст язык системный", "Rust"),
    ("rust_lengvizh", "раст лэнгвидж безопасный", "Rust"),

    # Python mishears
    ("python_paiton_3", "Пайтон 3 скрипт", "Python 3"),
    ("python_paiton_no_num", "Пайтон установить", "Python"),
    ("python_piton_3", "Питон 3 вирт энв", "Python 3"),

    # JSON
    ("json_dzheyson_format", "Джейсон формат запроса", "JSON формат"),
    ("json_dzheyson_standalone", "открой Джейсон файл", "JSON"),

    # YAML
    ("yaml_yaml_ru", "ямл конфиг", "YAML"),

    # Docker additional mishear
    ("docker_dokker", "Доккер контейнер", "Docker"),

    # Kubernetes additional
    ("k8s_kuber", "Кубер кластер", "Kubernetes"),

    # Terraform
    ("terraform_ru", "Терраформ применить", "Terraform"),

    # Markdown
    ("markdown_ru", "маркдаун разметка", "Markdown"),

    # .md extension
    ("dotmd_dot_emdi", "дот эмди расширение", ".md"),

    # MP3
    ("mp3_em_pe_3", "конвертировать в Эм пэ 3", "MP3"),

    # WAV
    ("wav_vav_fail", "записать вав файл", "WAV файл"),

    # SSL — W1059 regression: [ЭэЕе]ль must match both Э (correct) and Е (Whisper mishear)
    ("ssl_es_es_el_correct", "Эс Эс Эль сертификат", "SSL"),
    ("ssl_es_es_el_whisper", "эс эс ель подключение", "SSL"),
    ("ssl_uppercase_start", "Эс Эс Эль соединение", "SSL"),

    # subagent
    ("subagent_sab_agent", "саб агент выполнил", "subagent"),
    ("subagent_sub_agent_mixed", "sub агент запущен", "subagent"),

    # коммит
    ("commit_single_m", "сделать комит", "коммит"),

    # rebase
    ("rebase_rebbeis", "реббейс ветки", "rebase"),
    ("rebase_ribayz", "рибейз сделай", "rebase"),
    ("rebase_ri_beis", "ри-бейс конфликт", "rebase"),

    # pull request
    ("pr_pull_request_ru", "открой пулл реквест", "pull request"),
    ("pr_merged", "пулреквест принят", "pull request"),
    ("pr_abbrev", "П.Р. одобрен", "PR"),

    # мерджить
    ("merzh_yo", "мёрджить ветку", "мерджить"),

    # issues
    ("issues_ishyus", "ишьюс в гитхабе", "issues"),

    # AppleScript
    ("applescript_epl", "эпл скрипт запустить", "AppleScript"),
    ("applescript_apple_lower", "apple скрипт команда", "AppleScript"),

    # osascript
    ("osascript_osa", "оса скрипт выполнить", "osascript"),
]


@pytest.mark.parametrize("case_id,text,expected", POSITIVE_CASES, ids=[c[0] for c in POSITIVE_CASES])
def test_positive_replacement(case_id: str, text: str, expected: str) -> None:
    """Проверяем, что мишир заменяется на каноническое написание."""
    result = ne(text)
    assert expected in result, (
        f"[{case_id}] Expected '{expected}' in normalized text, got: {result!r}"
    )


# ---------------------------------------------------------------------------
# Negative cases — must NOT be replaced (false-positive guard)
# ---------------------------------------------------------------------------
NEGATIVE_CASES: list[tuple[str, str, str]] = [
    # (id, input, must_not_be_in_result)

    # «слак» standalone (physical slack/looseness) — should NOT become Slack
    ("slack_standalone_no_replace", "возьми слак верёвки", "Slack"),
    ("slack_standalone_adj", "слак в тросе", "Slack"),

    # «свифт» without code context — Taylor Swift etc.
    ("swift_no_code_context", "Тейлор Свифт певица", "Swift"),

    # «жира» as feminine Russian word (жира = grease/fat genitive)
    ("zhira_fat_no_replace", "много жира в еде", "Jira"),

    # «зед» alone (not «зед редактор») — should NOT become Zed
    ("zed_standalone_no_replace", "зед последняя буква", "Zed"),

    # «Линеар» alone (adjective «linear» not in tracker context) — should NOT become Linear brand
    ("linear_adj_no_replace", "линейный алгоритм", "Linear"),

    # «ямл» only → YAML but we check result is correct, not double-replaced
    ("yaml_result_correct", "ямл конфиг правильный", "ямл"),

    # «коммит» (already correct, no single-м version) should stay коммит
    ("commit_already_correct", "коммит уже создан", "комит"),

    # «мерджить» (already correct) should stay мерджить
    ("merzh_already_correct", "мерджить ветки в мейн", "мёрджить"),

    # «Опус» without digit stays as-is (already in v2, re-check here)
    ("opus_no_version_stays", "Опус Бетховена великий", "Opus"),

    # «линейный» (adjective, not in brand context) should NOT become Linear
    ("linear_adj_linear_not_replaced", "линейный алгоритм сложности", "Linear"),
]


@pytest.mark.parametrize("case_id,text,must_not", NEGATIVE_CASES, ids=[c[0] for c in NEGATIVE_CASES])
def test_negative_no_replacement(case_id: str, text: str, must_not: str) -> None:
    """Проверяем, что нейтральный контекст НЕ даёт ложного срабатывания."""
    result = ne(text)
    assert must_not not in result, (
        f"[{case_id}] Expected '{must_not}' NOT in result, got: {result!r}"
    )
