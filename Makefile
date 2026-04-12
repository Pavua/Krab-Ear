.PHONY: test test-all test-pipeline test-rest test-cli test-core test-fast \
        build sign run lint coverage docs backup benchmark export status \
        benchmark-llm benchmark-stt clean schemas app verify release

VENV = .venv_krab_ear
PYTHON = $(VENV)/bin/python
SWIFT_DIR = native/KrabEarAgent

# ---------------------------------------------------------------------------
# Test targets
# ---------------------------------------------------------------------------

# Run the full test suite (unittest discover)
test:
	PYTHONPATH=$$(pwd)/KrabEar $(PYTHON) -m unittest discover -s KrabEar/tests -p "test_*.py" -v

# Alias: same as 'test'
test-all: test

# Pipeline tests only (pipeline_core + pipeline_integration + all stage_*)
test-pipeline:
	PYTHONPATH=$$(pwd)/KrabEar $(PYTHON) -m unittest discover -s KrabEar/tests -p "test_pipeline*.py" -v
	PYTHONPATH=$$(pwd)/KrabEar $(PYTHON) -m unittest discover -s KrabEar/tests -p "test_stage*.py" -v

# REST API tests only
test-rest:
	PYTHONPATH=$$(pwd)/KrabEar $(PYTHON) -m unittest discover -s KrabEar/tests -p "test_rest*.py" -v

# CLI tests only
test-cli:
	PYTHONPATH=$$(pwd)/KrabEar $(PYTHON) -m unittest KrabEar/tests/test_cli.py -v

# Core module tests (config, utils, engine cleanup, text utils, punctuation, term extractor, text comparator)
test-core:
	PYTHONPATH=$$(pwd)/KrabEar $(PYTHON) -m unittest \
		KrabEar/tests/test_config.py \
		KrabEar/tests/test_engine_cleanup.py \
		KrabEar/tests/test_text_utils_edge_cases.py \
		KrabEar/tests/test_punctuation_fixer.py \
		KrabEar/tests/test_term_extractor.py \
		KrabEar/tests/test_text_comparator.py \
		-v

# Fast subset: skip tests that import AudioEngine (no model download required)
test-fast:
	PYTHONPATH=$$(pwd)/KrabEar $(PYTHON) -m pytest KrabEar/tests/ \
		--ignore=KrabEar/tests/test_engine_diarization.py \
		--ignore=KrabEar/tests/test_engine_llm_integration.py \
		--ignore=KrabEar/tests/test_engine_preview_prompt.py \
		--ignore=KrabEar/tests/test_engine_remote_stt.py \
		--ignore=KrabEar/tests/test_e2e_voice_loop.py \
		-q

# ---------------------------------------------------------------------------
# Code quality
# ---------------------------------------------------------------------------

# Lint: prefer ruff if available, fall back to flake8
lint:
	@if command -v $(VENV)/bin/ruff > /dev/null 2>&1; then \
		$(VENV)/bin/ruff check KrabEar/backend/ KrabEar/core/ --line-length 120; \
	else \
		$(VENV)/bin/flake8 KrabEar/backend/ KrabEar/core/ --max-line-length=120 --ignore=E501,W503,E402; \
	fi

# Coverage report via pytest-cov (install with: pip install pytest-cov)
coverage:
	PYTHONPATH=$$(pwd)/KrabEar $(PYTHON) -m pytest KrabEar/tests/ \
		--ignore=KrabEar/tests/test_engine_diarization.py \
		--ignore=KrabEar/tests/test_engine_llm_integration.py \
		--ignore=KrabEar/tests/test_e2e_voice_loop.py \
		--cov=KrabEar --cov-report=term-missing --cov-report=html:htmlcov -q
	@echo "HTML report: htmlcov/index.html"

# ---------------------------------------------------------------------------
# Developer utilities
# ---------------------------------------------------------------------------

# Show API docs location (generated from schemas)
docs: schemas
	@echo "JSON schemas exported to KrabEar/contracts/schemas/"
	@echo "No further HTML doc generation configured — add pdoc/sphinx here if needed."

# Create a timestamped backup via CLI (backend must be running)
backup:
	PYTHONPATH=$$(pwd)/KrabEar $(PYTHON) -m KrabEar.cli health || true
	@echo "Triggering backup via IPC..."
	PYTHONPATH=$$(pwd)/KrabEar $(PYTHON) -c "\
import sys; sys.path.insert(0, 'KrabEar'); \
from cli import _ipc_call; r = _ipc_call('backup_history'); print(r)"

# Run STT benchmark (alias + legacy target)
benchmark: benchmark-stt

# Export history in all available formats to /tmp
export:
	@echo "Exporting history (SRT, Markdown, Obsidian) to /tmp/krabear_export_*"
	PYTHONPATH=$$(pwd)/KrabEar $(PYTHON) -m KrabEar.cli export --format srt    --output /tmp/krabear_export.srt
	PYTHONPATH=$$(pwd)/KrabEar $(PYTHON) -m KrabEar.cli export --format md     --output /tmp/krabear_export.md
	PYTHONPATH=$$(pwd)/KrabEar $(PYTHON) -m KrabEar.cli export --format obsidian --output /tmp/krabear_export_obsidian.md
	@echo "Done."

# Show backend status via CLI
status:
	PYTHONPATH=$$(pwd)/KrabEar $(PYTHON) -m KrabEar.cli status

# ---------------------------------------------------------------------------
# Build / Swift / bundle targets (unchanged)
# ---------------------------------------------------------------------------

build:
	cd $(SWIFT_DIR) && swift build -c release

sign: build
	cp -f $(SWIFT_DIR)/.build/release/KrabEarAgent native/runtime/KrabEarAgent
	cp -f $(SWIFT_DIR)/.build/release/KrabEarAgent "Krab Ear.app/Contents/MacOS/KrabEarAgent"
	codesign -s - -f native/runtime/KrabEarAgent
	codesign -s - -f "Krab Ear.app"

run:
	$(PYTHON) KrabEar/main.py --data-dir ~/.krab_ear_data

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

# Full release cycle
release: verify app
	@echo "✓ Release ready"
