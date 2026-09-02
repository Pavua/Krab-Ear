.PHONY: test build sign run lint benchmark-llm benchmark-stt clean schemas app verify release reset-tcc clean-worktree-builds audit-orphans audit-orphan-panel-controls audit-handlers audit-duplicate-defs audit-cherry-pick audit-wiring audit-dead-modules audit-purge-coverage audit-inmemory-purge-coverage audit-path-containment audit-dispatch-test-targets audit-ipc-drift audit-fake-store-signatures audit-agent-settings-symmetry audit-dead-swift audit-all pre-merge-check dispatch-tests service-loc

VENV = .venv_krab_ear
PYTHON = $(VENV)/bin/python
SWIFT_DIR = native/KrabEarAgent
BUNDLE_ID = com.antigravity.krab-ear

test:
	PYTHONPATH=$$(pwd)/KrabEar $(PYTHON) -m unittest discover -s KrabEar/tests -p "test_*.py" -v

build:
	cd $(SWIFT_DIR) && swift build -c release

# Reset stale TCC entries for our bundle ID.
# При ad-hoc codesign каждый rebuild = новый cdhash → TCC считает app новым.
# Reset удаляет stale entries чтобы система не копила разрешений от прошлых
# cdhashes. Пользователю нужно выдать Accessibility снова после этого.
reset-tcc:
	@echo "Resetting TCC entries for $(BUNDLE_ID)..."
	@tccutil reset Accessibility $(BUNDLE_ID) 2>/dev/null || true
	@tccutil reset PostEvent $(BUNDLE_ID) 2>/dev/null || true
	@tccutil reset ListenEvent $(BUNDLE_ID) 2>/dev/null || true
	@tccutil reset AppleEvents $(BUNDLE_ID) 2>/dev/null || true
	@tccutil reset Microphone $(BUNDLE_ID) 2>/dev/null || true
	@echo "✓ TCC reset done. You need to re-grant Accessibility next time app requests it."

sign: build
	cp -f $(SWIFT_DIR)/.build/release/KrabEarAgent native/runtime/KrabEarAgent
	cp -f $(SWIFT_DIR)/.build/release/KrabEarAgent "Krab Ear.app/Contents/MacOS/KrabEarAgent"
	codesign -s - -f --identifier $(BUNDLE_ID) native/runtime/KrabEarAgent
	codesign -s - -f "Krab Ear.app"
	@echo ""
	@echo "⚠  Note: после каждого rebuild cdhash меняется. Если Accessibility снова"
	@echo "   запрашивает разрешение — запустите: make reset-tcc"
	@echo "   Или откройте: Repair Krab Ear Permissions.command"

run:
	$(PYTHON) KrabEar/main.py --data-dir ~/.krab_ear_data

lint:
	$(VENV)/bin/flake8 KrabEar/backend/ KrabEar/core/ --max-line-length=120 --ignore=E501,W503,E402

benchmark-llm:
	PYTHONPATH=$$(pwd)/KrabEar $(PYTHON) KrabEar/tests/benchmark_llm_models.py

benchmark-stt:
	PYTHONPATH=$$(pwd)/KrabEar $(PYTHON) KrabEar/tests/benchmark_stt.py

clean:
	cd $(SWIFT_DIR) && swift package clean

schemas:
	cd KrabEar && $(PYTHON) -m contracts.export

# Update .app bundle binary
app: sign
	cp -f "Krab Ear.app/Contents/MacOS/KrabEarAgent" "Krab Ear.app/Contents/MacOS/KrabEarAgent.bak" 2>/dev/null || true
	cp -f native/runtime/KrabEarAgent "Krab Ear.app/Contents/MacOS/KrabEarAgent"
	codesign -s - -f "Krab Ear.app"
	@echo "✓ App bundle updated"

# Verify everything
verify: test build
	codesign -v "Krab Ear.app"
	@echo "✓ All checks passed"

# Remove stale Swift .build caches from old worktrees (dry-run by default).
# Use ARGS=--apply to actually delete, ARGS="--apply --days 14" to tune age threshold.
clean-worktree-builds:
	@chmod +x scripts/cleanup_worktree_builds.command
	scripts/cleanup_worktree_builds.command $(ARGS)

# Full release cycle: build + sign both binaries + dSYM upload to Sentry.
# Вызывает scripts/build_and_deploy.command — idempotent, one-click.
# Для пропуска Sentry upload: make release ARGS=--no-sentry
release:
	@chmod +x scripts/build_and_deploy.command
	scripts/build_and_deploy.command $(ARGS)

# Audit orphan imports in service.py (W746/W771 regression guard).
# Detects names instantiated/decorated but never imported.
# Pass ARGS=--strict to also check lowercase function calls.
audit-orphans:
	python3 scripts/audit_orphan_imports.py $(ARGS)

# Audit IPC handler complexity in service.py.
# Reports LOC, cyclomatic complexity, risky calls, and delegation type per handler.
# Pass ARGS=--json for machine-readable output.
audit-handlers:
	python3 scripts/audit_ipc_handler_complexity.py $(ARGS)

# Run only dispatch-invariant test files (fast regression gate, no pytest needed).
dispatch-tests:
	PYTHONPATH=$$(pwd)/KrabEar $(PYTHON) -m unittest \
		KrabEar/tests/test_ipc_dispatch_invariants.py \
		KrabEar/tests/test_dispatch_invariants_wave654.py \
		KrabEar/tests/test_dispatch_invariants_wave693.py \
		-v

# Audit duplicate function/method defs (W1441 guard, wired in CI).
# Exits non-zero if genuine shadowing bugs are found (@property pairs excluded).
audit-duplicate-defs:
	python3 scripts/audit_duplicate_defs.py $(ARGS)

# Audit test imports against source modules (W1538 guard, wired in CI).
# Detects symbols dropped by cherry-pick or rebase regressions.
# Pass ARGS=--json for machine-readable output.
audit-cherry-pick:
	python3 scripts/audit_cherry_pick_regressions.py $(ARGS)

# Audit for unwired late-injections / decorative-architecture pattern (W1686/W1691 guard).
# Detects service/collaborator instances assigned to attrs in __init__ but never called.
# Runs report-only in CI until batch 91 fix PRs (W1687/W1688/W1690) merge;
# pass ARGS=--fail-on-found to enable strict mode locally once those land.
audit-wiring:
	python3 scripts/audit_decorative_wiring.py $(ARGS)

# Audit dead extracted modules + dead cross-file duplicates (W1768 guard).
# Detects decorative extractions (W746/W797 class): a module/symbol "extracted"
# but production still uses an inline copy (e.g. ipc_dispatch.build_dispatch_table
# dead while service.py uses its own inline handlers dict; IPCServer duplicated).
# Strict (--fail-on-found) enforced in CI since W1774; pass ARGS=--json for machine output.
audit-dead-modules:
	python3 scripts/audit_dead_extracted_modules.py --fail-on-found $(ARGS)

# Audit privacy-purge coverage (W1768 guard).
# Ensures handle_purge_all_data covers every file-backed store that holds user data,
# or the store is explicitly allowlisted. Found 28 uncovered gaps in W1768; all fixed
# before this gate was enforced. Strict (--fail-on-found) enforced in CI since W1774.
# Pass ARGS=--json for machine-readable output.
audit-purge-coverage:
	python3 scripts/audit_purge_coverage.py --fail-on-found $(ARGS)

# Audit path-containment startswith checks (root-cause guard for the #1660 class).
# Flags ``X.startswith(Y)`` used as a filesystem path-containment test (prefix
# match instead of Path.relative_to / is_relative_to) — the classic sibling-prefix
# escape (``/home/user_evil`` passes ``startswith('/home/user')``). Strict
# (--fail-on-found) enforced in CI since #1674 (all findings fixed before gate
# enforcement — history_service.handle_import_history_ndjson was the last finding,
# now fixed). Pass ARGS=--json for machine-readable output,
# ARGS=--selftest for inline classifier asserts.
audit-path-containment:
	python3 scripts/audit_path_containment.py --fail-on-found $(ARGS)

# Audit "test validates the dead in-class copy" bug class (#47 / #1675 guard).
# Finds in-class BackendService._handle_<X> methods that are dead shadows of a
# live extracted dispatch target (send_to_telegram/list_telegram_chats → the
# apple_integration_service copy, +34 W797 siblings), plus the tests that
# AST/call-validate the dead copy instead of the live extracted handler.
# Found 36 dead duplicates (34 test-validated) in #1675; all deleted + tests
# repointed in #1689 (which also surfaced & fixed 3 real security divergences the
# dead copies masked: get_topic_timeline DoS + sentiment/keyword privacy leaks).
# Strict (--fail-on-found) enforced in CI since #1689. Pass ARGS=--json for
# machine output, ARGS=--selftest for the known-bad/good check.
audit-dispatch-test-targets:
	python3 scripts/audit_dispatch_test_targets.py --fail-on-found $(ARGS)

# Audit in-memory (in-RAM) privacy-purge coverage (curated-registry guard).
# Companion to audit-purge-coverage (disk-backed stores): asserts every in-RAM
# PII collaborator in the REGISTRY (context_memory deque, clipboard history,
# StateStore search caches, JobTracker, semantic purge-epoch) is cleared by
# handle_purge_all_data — else stale PII survives a purge in memory until restart.
# Curated registry: adding a new in-RAM PII collaborator there forces the purge
# wiring. Strict (--fail-on-found) enforced in CI since wave-30 (#1717, 0 gaps).
# Pass ARGS=--json for machine output, ARGS=--selftest for the known-bad/good check.
audit-inmemory-purge-coverage:
	python3 scripts/audit_inmemory_purge_coverage.py --fail-on-found $(ARGS)

# Audit Swift↔Python IPC method-name drift + param-key drift + SSE type-string drift.
# Part A (method-name) is the hard gate — fails on any Swift call site whose method
# literal is absent from the Python dispatch table. Part B (param-key) and Part C
# (SSE dot/underscore mismatch) are report-only unless --strict is passed.
# Allowlist: scripts/ipc_drift_allowlist.txt (one entry per line).
# Pass ARGS=--json for machine-readable output, ARGS=--strict to fail on B/C too.
audit-ipc-drift:
	python3 scripts/audit_ipc_contract_drift.py $(ARGS)

# Audit fake-StateStore signature drift in tests (#1916).
# Tests hand-roll fake stores; the real StateStore signature moves on (the
# lock-contention wave added load_settings(lock_timeout_sec=, nowait=)) and the
# fakes silently diverge. The break is DELAYED — a diverged fake stays green
# until the code under test finally reaches the new kwarg, which is why 7 files
# went red at once in #1916 while 22 more sat armed and unnoticed.
# Criterion: a fake must accept exactly the KEYWORD arguments production calls it
# with; positional call sites impose nothing (demanding the full signature yielded
# 21 false positives on save_settings, whose fakes just name the param differently).
# Pass ARGS=--json for machine output, ARGS=--selftest for the known-bad/good check.
audit-fake-store-signatures:
	python3 scripts/audit_fake_store_signatures.py --fail-on-found $(ARGS)

# Парные пороги: один предел, выраженный в файле и константой, и литералом.
audit-paired-thresholds:
	python3 scripts/audit_paired_thresholds.py --fail-on-found $(ARGS)

# Односторонняя настройка: ключ, который AgentSettings читает, но не отправляет
# обратно (или наоборот). Панель тогда либо не может сохранить значение, либо
# сбрасывает его на эхе backend'а — контрол в такой паре мёртв по построению.
audit-agent-settings-symmetry:
	python3 scripts/audit_agent_settings_symmetry.py --selftest
	python3 scripts/audit_agent_settings_symmetry.py --fail-on-found $(ARGS)

# Осиротевший контрол панели: объявлен, но без проводки (target+action или чтение
# значения из ДОСТИЖИМОГО кода) либо без места в иерархии видов — украшение.
# Так пикер микрофона заполнялся из get_audio_devices и никуда не отправлял выбор.
audit-orphan-panel-controls:
	python3 scripts/audit_orphan_panel_controls.py --selftest
	python3 scripts/audit_orphan_panel_controls.py --fail-on-found $(ARGS)

# Run all static audit checks (CI parity — runs same checks as CI guard jobs).
# Audit dead Swift methods (W6 guard).
# The Python side has five dead-code guards; the Swift agent had none, and the class
# is live there too -- setupErrorBus was dead in production for months behind 100%
# green tests. Report-only for now (NO --fail-on-found): the first live run returns
# 19 dead + 45 test-only methods, and the owner has not yet approved that list.
# Flip to --fail-on-found (and add to audit-all) once the backlog is cleared.
# Pass ARGS=--json for machine-readable output.
audit-dead-swift:
	python3 scripts/audit_dead_swift_methods.py --selftest
	python3 scripts/audit_dead_swift_methods.py $(ARGS)

audit-all: audit-orphans audit-duplicate-defs audit-cherry-pick audit-wiring audit-dead-modules audit-purge-coverage audit-path-containment audit-dispatch-test-targets audit-inmemory-purge-coverage audit-ipc-drift audit-fake-store-signatures audit-paired-thresholds audit-agent-settings-symmetry audit-orphan-panel-controls
	@echo "All audit checks passed."

# Reproduce the ubuntu krab-ear-ci env LOCALLY (Python 3.12, mlx ABSENT) and run
# changed/given test files BEFORE the slow remote CI — breaks the "mlx-masking"
# red-tip cycle (dev .venv_krab_ear is py3.14 WITH mlx → false-green vs ubuntu).
# Usage: make pre-merge-check                       (auto-detect changed test files)
#        make pre-merge-check ARGS="KrabEar/tests/test_foo.py KrabEar/tests/test_bar.py"
#        make pre-merge-check ARGS=REBUILD          (force-rebuild the harness venv)
pre-merge-check:
	@chmod +x scripts/pre_merge_py312_check.sh
	scripts/pre_merge_py312_check.sh $(ARGS)

# Print current service.py line count (quick monolith size gauge).
service-loc:
	@wc -l KrabEar/backend/service.py | awk '{print $$1}'
