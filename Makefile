.PHONY: test build sign run lint benchmark-llm benchmark-stt clean schemas app verify release reset-tcc

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

# Full release cycle
release: verify app
	@echo "✓ Release ready"
