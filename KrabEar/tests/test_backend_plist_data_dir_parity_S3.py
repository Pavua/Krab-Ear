"""Паритет каталога данных в шаблоне ``ai.krab.ear.backend.plist.template`` (S3/Р3+Р9).

Каталог попадает в backend-процесс двумя разнотипными каналами: аргумент
``--data-dir`` (доходит только до ``StateStore`` через локальную переменную
``main()``) и переменная окружения ``KRAB_EAR_DATA_DIR`` (её читает всё
остальное — ``rest_server``, ``cloud_stt``, ``cloud_rewriter``,
``startup_diagnostics``). До этой волны переменной в backend-плисте не было —
она жила только в rest-плисте (с 16-07), потому что REST был отдельным
юнитом. После слияния in-process REST переезжает под backend-плист и без
переменной читал бы ``~/.krab_ear_data`` вместо канонического каталога.

Тест парсит шаблон КАК ЕСТЬ (плейсхолдеры ``__HOME__``/``__PROJECT_ROOT__`` не
раскрываются — сравнение строковое, без подстановки), поэтому не зависит от
`sed`-рендера установщика.
"""

from __future__ import annotations

import plistlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "KrabEar" / "launchagents" / "ai.krab.ear.backend.plist.template"


def _load_template() -> dict:
    # plistlib не понимает XML-комментарии со style-doctype без валидного XML —
    # если шаблон содержит "--" внутри <!-- --> комментария, здесь же и упадёт
    # с ExpatError (тот же класс сбоя, что и `plutil -lint` у установщика).
    return plistlib.loads(TEMPLATE.read_bytes())


def test_data_dir_env_matches_data_dir_argument() -> None:
    """``KRAB_EAR_DATA_DIR`` обязан побайтово совпадать со значением после ``--data-dir``.

    RED до правки: ключа ``KRAB_EAR_DATA_DIR`` в ``EnvironmentVariables`` нет
    вовсе — тест падает на ``KeyError``, а не молчаливым несовпадением.
    """
    plist = _load_template()
    args = plist["ProgramArguments"]

    assert "--data-dir" in args, "шаблон обязан передавать --data-dir в ProgramArguments"
    data_dir_arg = args[args.index("--data-dir") + 1]

    env_data_dir = plist["EnvironmentVariables"]["KRAB_EAR_DATA_DIR"]

    assert data_dir_arg == env_data_dir, (
        "settings.DATA_DIR (KRAB_EAR_DATA_DIR) разошёлся с --data-dir: "
        f"{env_data_dir!r} != {data_dir_arg!r} — половина процесса "
        "(rest_server/cloud_stt/cloud_rewriter/startup_diagnostics) "
        "смотрела бы не в тот каталог."
    )


def test_program_arguments_use_main_entrypoint() -> None:
    """S3/Р9: плист обязан запускать ``main.py``, а не ``backend/service.py`` напрямую.

    Прямой запуск ``backend/service.py`` делает его модулем ``__main__``, а
    ``rest_server.py`` импортирует ``backend.service`` — при включённом
    in-process REST файл исполнился бы ВТОРЫМ экземпляром модуля.
    """
    plist = _load_template()
    args = plist["ProgramArguments"]

    assert any(arg.endswith("KrabEar/main.py") for arg in args), (
        "ProgramArguments не содержит KrabEar/main.py — backend всё ещё "
        "запускается напрямую как backend/service.py (класс Р9)"
    )
    assert not any(arg.endswith("KrabEar/backend/service.py") for arg in args), (
        "ProgramArguments всё ещё содержит KrabEar/backend/service.py — "
        "двойное исполнение модуля backend.service при включённом REST (Р9)"
    )


def test_rest_template_is_valid_and_uses_same_data_dir() -> None:
    """Raw REST XML должен разбираться и ссылаться на общий каталог backend."""
    rest_path = TEMPLATE.with_name("ai.krab.ear.rest.plist.template")
    rest = plistlib.loads(rest_path.read_bytes())
    backend = _load_template()
    assert rest["Label"] == "ai.krab.ear.rest"
    assert rest["EnvironmentVariables"]["KRAB_EAR_DATA_DIR"] == backend["EnvironmentVariables"]["KRAB_EAR_DATA_DIR"]
