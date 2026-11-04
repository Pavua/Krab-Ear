# Проверка файлов MLX Whisper в HF-кэше

**Цель:** config-only snapshot не выдаётся за готовую STT-модель и не запрещает
явную загрузку владельцем. Успешный возврат snapshot_download без весов не
порождает событие done.

**База:** origin/codex/krab-ear-v2 @ 6e2043fb. Отдельная ветка
codex/ear-hf-cache-1104; conductor-коммит be9f8a71 остаётся независимым.

**Свидетельство:** read-only осмотр 04.11: локальный turbo snapshot содержит
config.json/README/.gitattributes без weights. StartupDiagnostics использует
model_dir.exists(), ModelDownloader — непустой snapshots. Установленный
mlx_whisper/load_models.py читает config.json и выбирает weights.safetensors
при наличии, иначе weights.npz. Загрузчик не импортируется и не запускается.

## Границы

- Никаких загрузок, ремонтов настоящего кэша, токенов, ML, вызовов/рестартов Ear,
  моделей, клона или OrbStack. Только source и временные FS-фикстуры.
- Публикации/CI/merge/deploy в карточку не входят. Чужие WIP сохраняются.
- Generic ModelCacheManager.is_model_cached остаётся учётом присутствия кэша;
  для готовности MLX Whisper нужен отдельный точный файловый контракт.
- Это проверка наличия непустых файлов, не их валидности и не inference-ready.
- Сначала causal RED, затем фикс. Тесты по одному файлу, аудит последовательно;
  тяжёлые запуски — только при доступном ресурсном окне.

## Task 1: общий локальный файловый предикат

**Files:** backend/model_cache_manager.py, backend/model_downloader.py,
backend/startup_diagnostics.py; tests/test_mlx_whisper_cache_readiness_2026_11_04.py.

- [x] RED: воспроизвести false cached в обоих consumers, skip user download и
  ошибочное done после возврата config-only snapshot. Положительные контроли:
  npz, safetensors, HF-ссылка на существующий blob.
- [x] Добавить в ModelCacheManager локальную проверку config + выбранных
  загрузчиком весов. HF try_to_load_from_cache разрешает текущий main без сети;
  произвольный старый snapshot не подтверждает текущую ревизию.
- [x] Применить одинаковый предикат в startup check и downloader cache check;
  проверить возвращённый snapshot до state/event done.
- [x] Исправить старые синтетические successful-download фикстуры: физические
  config и веса в TemporaryDirectory вместо несуществующего /tmp fake path.
- [x] GREEN и зависимые тесты cache-manager/downloader/startup/error-bus,
  Python3.12 без MLX, lint и audit-all. Независимое whole-diff review.
- [x] Подготовить локальный commit и отчёт об оставшемся отсутствии реальных весов.

## Результаты 04.11

- Причинный RED: 16 ожидаемых assertion failures (включая subtests обоих
  consumers), 3 положительных контроля PASS, ошибок импорта/окружения нет.
- GREEN: новый файл — 12 тестов и 20 subtests. Временный HF cache проверяет
  config-only, пустые веса, битую ссылку, отсутствие config/main ref, старый
  полный snapshot при текущем неполном, npz/safetensors и рабочую blob-ссылку.
  Пустой предпочитаемый safetensors не маскируется существующим npz.
- CPython 3.14: 197 PASS и 20 subtests в 6 отдельных pytest-процессах:
  новый cache contract 12, cache-manager 53, downloader 36, startup 69,
  error-bus phase B 20, recommended-setup probes 7.
- CPython 3.12.11 без mlx.core/mlx_whisper: 117 PASS и 20 subtests,
  три изменённых test-файла отдельными процессами (12 + 36 + 69).
  Использован существующий /tmp/py312 без установки пакетов. Полный
  pre_merge_py312_check.sh с backend-import preflight не запускался.
- Во время pytest IP socket connections отклонялись тестовым runner; реальные
  snapshot_download и модели не запускались. PYTEST_DISABLE_PLUGIN_AUTOLOAD=1,
  addopts очищен; предупреждения timeout config ожидаемы. В dev-окружении
  остаётся существующее предупреждение TorchCodec/FFmpeg при импортах.
- Focused flake8 всех шести изменённых Python-файлов, git diff --check и
  make audit-all: exit 0; аудит сообщил All audit checks passed.
- Независимое Astra/ultra whole-diff review: GO, обязательных замечаний нет;
  reviewer независимо подтвердил 12/12 новых тестов.
- Raw-логи, PER_FILE_RESULTS.json и PY312_RESULTS.json сохранены в локальном
  каталоге артефактов текущей Codex-сессии hf-cache-readiness/; в Git не входят.
- На source-checkpoint turbo snapshot оставался без весов: кэш не исправлялся
  и не скачивался, inference не квалифицировался. Последующее отдельное
  owner-approved восстановление и узкий ES probe записаны в
  [runtime-карточке](2026-11-04-turbo-cache-recovery.md).
  Публикации, hosted CI, merge и deploy этого source-изменения нет.
