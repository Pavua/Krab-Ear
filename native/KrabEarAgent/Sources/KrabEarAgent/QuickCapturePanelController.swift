/*
 QuickCapturePanelController — C3b Task 1: плавающая панель-скретчпад для
 быстрой голосовой заметки (C3a, Cmd+Shift+N, main+QuickCapture.swift).

 До этой волны быстрая заметка работала «вслепую» (только хоткей + звук +
 тост) — эта панель даёт визуальную обратную связь: статус записи, live-текст
 партиалов и список последних заметок с быстрым «Копировать».

 Task 1 — каркас панели + чистый рендер + test-hooks. Проводка (показ панели
 по месту вызова, поставка SSE-партиалов/списка заметок из backend'а) — Task 2,
 main+QuickCapturePanel.swift (ещё не существует). До Task 2 панель никем не
 инстанцируется в проде — `ipcClient`/`onToggleRecording` инжектируются
 владельцем ПОСЛЕ конструирования, паттерн идентичен `ipcClient`/`onFinished`
 в MeetingLivePanelController.

 Task 2 (C3b) — SSE: подписка на REST `/v1/events?filter=realtime.partial_transcript,
 realtime.final_transcript` тем же приёмом (`SSESessionDelegate`, sorted-filter URL,
 `event: `/`data: ` построчный парсинг, `obj["data"] ?? obj` фоллбэк на плоский
 payload), что MeetingLivePanelController. Формат payload'а СВЕРЕН с реальным
 event_bus.py/rest_server.py (не угадан, урок C2c): `realtime.partial_transcript`
 и `realtime.final_transcript` оба несут `{session_id, text, is_partial, ts}` —
 ОДНО поле "text" для обоих типов, поэтому dispatch единообразно зовёт
 ingestPartial(text) без ветвления по типу события. SSE стартует в show() и
 останавливается в windowWillClose. C3b ревью F3 (изначальный комментарий тут
 был неверен): сервер эмиттит эти события для ЛЮБОЙ активной записи общего
 recorder'а (обычная диктовка Right Option — тот же backend-пайплайн, что
 быстрая заметка), не только во время заметки — поэтому ingestPartial сама
 фильтрует по isRecording, иначе панель, открытая вручную вне заметки, рисовала
 бы live-текст чужой диктовки.

 Владельцем оставлен подход main+QuickCapture.swift (НЕ main+QuickCapturePanel.swift —
 план предполагал отдельный файл, реально проще встроить точки входа в уже
 существующий main+QuickCapture.swift, см. отчёт волны C3b Task 2).

 Panel-boilerplate — портирован ≥80% из MeetingLivePanelController.swift
 (styleMask/level/collectionBehavior/isReleasedWhenClosed/drag/savePosition/
 restorePosition/isOnScreen≥80%, включая ревью №4 «header-таймер не должен
 тикать за закрытой панелью») с новым positionKey.

 Глиф-гейт (AGENT-J/M, CLAUDE.md): ⏺/■ из плана — черновая нотация задачи,
 НЕ буквальные Unicode-литералы в коде (0 хитов в native/Sources до этой
 волны) — статус записи рендерится тем же `RecordingIndicator` (цветная точка
 с pulsing-анимацией), что и MeetingLivePanelController; кнопка старт/стоп —
 SF Symbols `record.circle.fill`/`stop.circle.fill`.

 S3 Task 8 (2026-07-31, спека Р12 находка I-E): раньше SSESessionDelegate
 конструировался без onComplete — обрыв потока (до этой волны редкий; после
 включения in-process REST режима рвётся при каждом лечении сторожа задачи 7
 и при самом рестарте REST) тихо ронял задачу, панель навсегда показывала
 последний партиал, хотя запись продолжалась и диагностика оставалась
 зелёной. Теперь startSSE/stopSSE/handleSSECompletion переподключаются с
 экспоненциальным backoff БЕЗ give-up-лимита, пока панель видима
 (`panel.isVisible` — жизненный цикл SSE привязан к show()/close(), в панели
 нет отдельного понятия «режим активен»); `sseGeneration` защищает от
 устаревшего завершения уже отменённой попытки (см. комментарий у объявления
 `sseGeneration`).
*/

import AppKit
import Foundation

@MainActor
final class QuickCapturePanelController: NSObject, NSWindowDelegate {

    private let panel: NSPanel
    private let positionKey = "KrabEar_QuickCapturePanelPosition"

    /// Публичное (без `_test`-префикса) окно — план Task 1 проверяет
    /// styleMask/isReleasedWhenClosed/level напрямую из теста, без обёрток.
    var window: NSPanel? { panel }

    // MARK: - Данные (инжектируются владельцем в Task 2)

    /// Off-main IPC (AGENT-3) для «→ Notes». nil до инъекции владельцем —
    /// кнопка тогда просто отказывает тостом, не крашится.
    var ipcClient: IPCClient?
    /// Зовётся из ⏺/■ — владелец (main+QuickCapturePanel.swift, Task 2)
    /// подключит к AgentAppDelegate.onQuickCaptureToggle(). Панель НЕ хранит
    /// прямую ссылку на AgentAppDelegate — тот же паттерн инъекции колбэка,
    /// что onFinished в MeetingLivePanelController.
    var onToggleRecording: (() -> Void)?

    private(set) var isRecording = false
    private var startedAt: TimeInterval?
    private var timerTick: Timer?

    // MARK: - UI (все токены — KrabEarTheme)

    private let recordIndicator = RecordingIndicator()
    private let headerTimerLabel = NSTextField(labelWithString: "00:00")
    private let statusLabel = NSTextField(labelWithString: "")
    private let placeholderLabel = NSTextField(labelWithString: "Говорите — текст появится здесь")
    private let liveTextView = NSTextView()
    private let liveTextContainer = NSView()
    private let notesStack = NSStackView()
    private let toggleButton = ThemePrimaryButton(title: "", target: nil, action: nil)
    private let copyAllButton = ThemeSecondaryButton(title: "Копировать всё", target: nil, action: nil)
    private let sendToNotesButton = ThemeSecondaryButton(title: "→ Notes", target: nil, action: nil)

    private static let maxNoteRows = 7

    // MARK: - SSE (Task 2 — партиалы записи, паттерн MeetingLivePanelController;
    // Task 8 — переподключение при обрыве, спека Р12 находка I-E)

    private let restBaseURL = "http://127.0.0.1:5005"
    private var sseTask: URLSessionDataTask?
    /// Новая `URLSession` на каждую попытку подключения (не общий lazy var,
    /// см. startSSE) — держим ссылку только чтобы явно инвалидировать её в
    /// stopSSE.
    private var sseSession: URLSession?
    private var pendingSSEEventType: String?

    /// Ровно ДВА типа события — сверены с realtime_partial.py/recording_core_service.py,
    /// оба несут одинаковую форму payload'а {session_id, text, is_partial, ts}.
    private static let quickCaptureEventTypes: Set<String> = [
        "realtime.partial_transcript", "realtime.final_transcript",
    ]

    /// Генерация текущей попытки подключения — растёт на каждый явный стоп
    /// (stopSSE зовётся и из hide()/windowWillClose, и из самого startSSE
    /// перед новым стартом). `URLSession` отменяет задачу асинхронно, поэтому
    /// колбэк завершения УЖЕ отменённой задачи может прийти уже после того,
    /// как sseTask начал указывать на новую попытку — generation, захваченная
    /// замыканием onComplete в момент создания КОНКРЕТНОЙ попытки (см.
    /// startSSE), отсекает такие устаревшие сигналы в handleSSECompletion.
    private var sseGeneration: UInt64 = 0

    /// Экспоненциальный backoff переподключения. 🔴 Кап `LiveSubtitlesOverlay`
    /// (5 попыток, до 8с — сдаётся примерно за полминуты) здесь скопировать
    /// НЕЛЬЗЯ: цикл сторожа REST (задача 7) — не меньше минуты на детекцию
    /// нездоровья плюс рестарт, скопированный кап сдался бы раньше, чем REST
    /// поднимется обратно, и воспроизвёл бы ровно тот застывший экран, который
    /// чинит эта задача. Пока панель видима — переподключаемся без give-up-
    /// лимита; счётчик попыток сбрасывается любой полученной строкой
    /// (handleSSELine) — доказательство, что поток снова живой.
    private var sseReconnectAttempts = 0
    private let sseReconnectBaseDelay: TimeInterval = 0.5
    private let sseReconnectMaxDelay: TimeInterval = 30.0
    private var sseReconnectWorkItem: DispatchWorkItem?

    // MARK: - Test hooks (паттерн MeetingLivePanelController)

    var _testStatusText: String { statusLabel.stringValue }
    var _testHeaderTimerActive: Bool { timerTick != nil }
    var _testLiveText: String { liveTextView.string }
    var _testNoteRowCount: Int { notesStack.arrangedSubviews.count }

    func _testSetRecording(_ recording: Bool) { setRecording(recording) }
    func _testIngestPartial(_ text: String) { ingestPartial(text) }
    func _testSetNotes(_ notes: [[String: Any]]) { renderNotes(notes) }
    /// C3b ревью F2 — прямой вызов ресинхронизации без реальной сети/окна
    /// (show() трогает URLSession/NSScreen, что нежелательно в headless-тестах).
    func _testResyncTimerAndPulse() { resyncTimerAndPulseIfNeeded() }
    /// Прямой вызов SSE-парсера строки — без реального сетевого стрима (образец
    /// MeetingLivePanelController._testHandleSSELine).
    func _testHandleSSELine(_ line: String) { handleSSELine(line) }

    // MARK: - Init

    override init() {
        panel = NSPanel(
            contentRect: NSRect(x: 0, y: 0, width: 320, height: 420),
            styleMask: [.nonactivatingPanel, .hudWindow, .utilityWindow],
            backing: .buffered, defer: false)
        super.init()
        setupPanel()
        // Панель закрываема крестиком и переиспользуема — isReleasedWhenClosed=false
        // обязателен, иначе повторный show() после закрытия обращается к
        // деаллоцированному окну (тот же урок, что MeetingLivePanelController).
        panel.styleMask.insert(.closable)
        panel.isReleasedWhenClosed = false
        panel.delegate = self
        buildLayout()
        renderNotes([])
        applyStatusText()
    }

    // MARK: - Public API

    /// Показывает панель на сохранённой (или дефолтной топ-правой) позиции и
    /// запускает SSE-подписку на партиалы записи. nonactivatingPanel — НЕ
    /// активирует приложение, фокус остаётся у текущего.
    func show() {
        restorePosition()
        panel.orderFront(nil)
        resyncTimerAndPulseIfNeeded()
        startSSE()
    }

    /// C3b ревью F4: 0 живых вызывающих сейчас (взведённая мина для будущего
    /// вызова) — зеркалит windowWillClose, иначе скрытая панель тянула бы
    /// таймер/SSE вечно за кадром.
    func hide() {
        panel.orderOut(nil)
        stopTimer()
        stopSSE()
    }

    /// Ревью-урок C2c №4: header-таймер не должен тикать за закрытой панелью;
    /// тот же принцип применён к SSE — закрытая панель не тянет партиалы.
    /// Сама запись (backend) закрытием панели НЕ останавливается.
    func windowWillClose(_ notification: Notification) {
        stopTimer()
        stopSSE()
    }

    // MARK: - Recording state (чистая функция состояния → UI)

    /// Единственная точка входа состояния записи — владелец (Task 2) зовёт её
    /// из внешнего источника (poll/событие quickCaptureActive), кнопка ⏺/■
    /// сама НЕ меняет это состояние напрямую — она только форвардит намерение
    /// пользователя через onToggleRecording (см. onToggleTapped).
    func setRecording(_ recording: Bool) {
        guard recording != isRecording else { return }
        isRecording = recording
        if recording {
            startedAt = Date().timeIntervalSince1970
            liveTextView.string = ""
            placeholderLabel.isHidden = false
            recordIndicator.startPulsing()
            startTimer()
        } else {
            // C3b ревью F6a: плейсхолдер «Говорите — текст появится здесь»
            // не должен маячить в idle-состоянии (запись не идёт вовсе) —
            // только пока запись реально активна и живой текст ещё не пришёл.
            placeholderLabel.isHidden = true
            recordIndicator.stopPulsing()
            stopTimer()
        }
        applyStatusText()
        updateToggleButton()
    }

    /// C3b ревью F2: панель, закрытую крестиком мид-записи, windowWillClose уже
    /// заглушил (таймер/пульс остановлены), но isRecording остаётся true — сама
    /// запись продолжается за кадром (закрытие панели ≠ стоп заметки, см.
    /// windowWillClose). Без ресинхронизации при повторном показе таймер/пульс
    /// висят замороженными до следующего реального перехода состояния recording
    /// (которого может и не быть до самого стопа) — setRecording(true) при уже
    /// isRecording=true молча не делает ничего (guard выше).
    private func resyncTimerAndPulseIfNeeded() {
        guard isRecording, timerTick == nil else { return }
        startTimer()
        recordIndicator.startPulsing()
    }

    private func applyStatusText() {
        statusLabel.stringValue = isRecording ? "Идёт запись…" : "Запись не идёт. Готов начать."
    }

    private func updateToggleButton() {
        let symbolName = isRecording ? "stop.circle.fill" : "record.circle.fill"
        let accessibilityLabel = isRecording ? "Остановить запись" : "Начать запись"
        toggleButton.image = NSImage(systemSymbolName: symbolName, accessibilityDescription: accessibilityLabel)
        toggleButton.imagePosition = .imageLeading
        toggleButton.title = isRecording ? "Остановить" : "Начать запись"
    }

    // MARK: - Live text (партиалы — Task 2 поставляет через SSE)

    /// Заменяет live-текст последним партиалом, НЕ копит инкрементально.
    /// realtime.partial_transcript/realtime.final_transcript оба несут
    /// ре-транскрипцию СКОЛЬЗЯЩЕГО окна последних buffer_sec секунд аудио
    /// (backend/realtime_partial.py, ре-эмит каждые interval_sec) — append
    /// здесь дал бы дублирующийся/наслаивающийся текст на любой заметке
    /// длиннее ~3с ("привет" → "привет привет мир"). Тот же приём, что
    /// established потребитель ЭТИХ ЖЕ событий — RealtimeOverlayController.
    /// showPartialText/showFinalText (оба тоже replace). setRecording(true)
    /// сбрасывает буфер под новую заметку. Финальный сохранённый текст
    /// заметки приходит из результата stop_recording (main+QuickCapture.swift),
    /// этой панели не касается — здесь только живое превью.
    func ingestPartial(_ text: String) {
        // C3b ревью F3: realtime.partial_transcript эмитится ЛЮБОЙ активной
        // записью общего recorder'а (обычная диктовка Right Option — тот же
        // backend-пайплайн, что быстрая заметка, см. main+QuickCapture.swift
        // onQuickCaptureToggle), а не только быстрой заметкой. Панель, открытая
        // вручную вне активной заметки, не должна показывать live-текст чужой
        // диктовки поверх статуса «Запись не идёт».
        guard isRecording, !text.isEmpty else { return }
        liveTextView.string = text
        placeholderLabel.isHidden = true
    }

    // MARK: - SSE (общий SSESessionDelegate, паттерн MeetingLivePanelController)

    private func startSSE() {
        stopSSE()
        sseGeneration &+= 1
        let generation = sseGeneration

        let filter = Self.quickCaptureEventTypes.sorted().joined(separator: ",")
        // sorted() — детерминированный URL для тестов/логов; backend принимает
        // comma-list в любом порядке (rest_server.py:1649).
        guard let url = URL(string: "\(restBaseURL)/v1/events?filter=\(filter)") else { return }
        var request = URLRequest(url: url, timeoutInterval: 600)
        request.setValue("text/event-stream", forHTTPHeaderField: "Accept")

        // Отдельные делегат+сессия на каждую попытку (не общий lazy var, как
        // раньше) — иначе onComplete не смог бы различить завершение старой,
        // уже отменённой задачи и текущей: обе используют один и тот же
        // делегат, а generation, захваченная замыканием ниже, определена
        // именно для ЭТОЙ попытки.
        let delegate = SSESessionDelegate(
            onLine: { [weak self] line in
                Task { @MainActor [weak self] in self?.handleSSELine(line) }
            },
            onComplete: { [weak self] _ in
                Task { @MainActor [weak self] in self?.handleSSECompletion(generation: generation) }
            }
        )
        let session = URLSession(configuration: .default, delegate: delegate, delegateQueue: nil)
        sseSession = session
        let task = session.dataTask(with: request)
        sseTask = task
        task.resume()
    }

    private func stopSSE() {
        sseReconnectWorkItem?.cancel()
        sseReconnectWorkItem = nil
        sseGeneration &+= 1
        sseTask?.cancel()
        sseTask = nil
        sseSession?.invalidateAndCancel()
        sseSession = nil
        pendingSSEEventType = nil
    }

    /// Задача 8: раньше делегат создавался без onComplete — обрыв потока (в
    /// т.ч. каждое лечение сторожа REST из задачи 7 и сам рестарт REST при
    /// включении in-process режима) тихо ронял задачу, панель навсегда
    /// показывала последний полученный партиал, хотя запись продолжалась.
    private func handleSSECompletion(generation: UInt64) {
        // Устаревшее завершение уже отменённой (стопом или новым стартом)
        // задачи — молчим, текущая попытка либо не начиналась, либо это она.
        guard generation == sseGeneration else { return }
        sseTask = nil
        // Финал ревью S3: раньше сессия здесь просто обнулялась БЕЗ
        // invalidateAndCancel() — URLSession с кастомным делегатом держит
        // его сильной ссылкой до явной инвалидации, поэтому каждый обрыв
        // (нормальный путь этого метода — REST лежит/лечится не меньше
        // минуты, задача 7) копил ОДНУ неинвалидированную сессию+делегат
        // навсегда. Тот же вызов, что stopSSE() выше и образец
        // LiveSubtitlesOverlay.swift::closeCurrentSSEConnection().
        sseSession?.invalidateAndCancel()
        sseSession = nil
        // Панель закрыта — не переподключаемся, тот же принцип, что и
        // таймер/пульс (resyncTimerAndPulseIfNeeded, windowWillClose):
        // «панель видима» = panel.isVisible, в контроллере нет отдельного
        // понятия «режим активен».
        guard panel.isVisible else { return }
        scheduleSSEReconnect(afterGeneration: generation)
    }

    private func scheduleSSEReconnect(afterGeneration generation: UInt64) {
        let exponent = min(sseReconnectAttempts, 6)
        let delay = min(sseReconnectBaseDelay * pow(2.0, Double(exponent)), sseReconnectMaxDelay)
        sseReconnectAttempts += 1

        let workItem = DispatchWorkItem { [weak self] in
            guard let self, self.sseGeneration == generation, self.panel.isVisible else { return }
            self.startSSE()
        }
        sseReconnectWorkItem = workItem
        DispatchQueue.main.asyncAfter(deadline: .now() + delay, execute: workItem)
    }

    /// Паттерн MeetingLivePanelController.handleSSELine: `event: ` трекает тип,
    /// `data: ` диспатчит по нему.
    private func handleSSELine(_ line: String) {
        // Любая полученная строка (включая пустые keep-alive) доказывает, что
        // поток жив — сбрасываем backoff, чтобы следующий обрыв снова начинал
        // с минимальной задержки, а не с потолка, накопленного прошлой серией.
        sseReconnectAttempts = 0
        if line.hasPrefix("event: ") {
            pendingSSEEventType = String(line.dropFirst(7)).trimmingCharacters(in: .whitespaces)
        } else if line.hasPrefix("data: ") {
            let type = pendingSSEEventType
            pendingSSEEventType = nil
            guard let type, Self.quickCaptureEventTypes.contains(type) else { return }
            dispatchSSEEvent(json: String(line.dropFirst(6)))
        } else if line.isEmpty {
            pendingSSEEventType = nil
        }
    }

    /// Конверт {type, data: {...}} ИЛИ плоский {...} — реальный event_bus.py
    /// сериализует SSE data-строку как `json.dumps(event['data'])`, т.е. ПЛОСКИЙ
    /// payload (rest_server.py::sse_stream) — фоллбэк `?? obj` покрывает и его.
    /// realtime.partial_transcript/realtime.final_transcript оба несут одно и то
    /// же поле "text" (session_id/is_partial/ts не нужны панели-скретчпаду),
    /// поэтому единая точка диспатча без ветвления по типу события.
    private func dispatchSSEEvent(json: String) {
        guard let data = json.data(using: .utf8),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }
        let eventData = obj["data"] as? [String: Any] ?? obj
        let text = (eventData["text"] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        ingestPartial(text)
    }

    // MARK: - Notes list (до 7 строк: текст-превью + «Копировать»)

    /// Единственная точка входа списка заметок. `notes` — словари вида
    /// {"text": String, "ts": String} (поля сверены с HistoryItem.to_dict(),
    /// тот же контракт, что rebuildQuickNotesSubmenu в main+QuickCapture.swift).
    func renderNotes(_ notes: [[String: Any]]) {
        notesStack.arrangedSubviews.forEach { notesStack.removeArrangedSubview($0); $0.removeFromSuperview() }
        let latest = notes.prefix(Self.maxNoteRows)
        if latest.isEmpty {
            let emptyStack = NSStackView()
            emptyStack.orientation = .vertical
            emptyStack.alignment = .centerX
            emptyStack.spacing = KrabEarTheme.Metrics.tight

            let iconConfig = NSImage.SymbolConfiguration(pointSize: 16, weight: .regular)
            let icon = NSImage(systemSymbolName: "doc.text", accessibilityDescription: nil)?.withSymbolConfiguration(iconConfig)
            let iconView = NSImageView(image: icon ?? NSImage())
            iconView.contentTintColor = KrabEarTheme.Colors.textDisabled

            let emptyLabel = NSTextField(labelWithString: "Заметок пока нет")
            emptyLabel.font = KrabEarTheme.Typography.caption
            emptyLabel.textColor = KrabEarTheme.Colors.textSecondary
            emptyLabel.isBordered = false
            emptyLabel.drawsBackground = false

            emptyStack.addArrangedSubview(iconView)
            emptyStack.addArrangedSubview(emptyLabel)
            
            let container = NSView()
            container.translatesAutoresizingMaskIntoConstraints = false
            emptyStack.translatesAutoresizingMaskIntoConstraints = false
            container.addSubview(emptyStack)
            NSLayoutConstraint.activate([
                emptyStack.centerXAnchor.constraint(equalTo: container.centerXAnchor),
                emptyStack.centerYAnchor.constraint(equalTo: container.centerYAnchor),
                emptyStack.topAnchor.constraint(equalTo: container.topAnchor, constant: KrabEarTheme.Metrics.spacious),
                emptyStack.bottomAnchor.constraint(equalTo: container.bottomAnchor, constant: -KrabEarTheme.Metrics.spacious)
            ])
            notesStack.addArrangedSubview(container)
            container.widthAnchor.constraint(equalTo: notesStack.widthAnchor).isActive = true
            return
        }
        for note in latest {
            let text = note["text"] as? String ?? ""
            let row = makeNoteRow(text: text)
            notesStack.addArrangedSubview(row)
            // C3b ревью F6b: без явного пина строки не растягивались на всю
            // ширину стека (в отличие от emptyStack-контейнера выше) — разной
            // ширины карточки выглядели прижатыми к leading-краю.
            row.widthAnchor.constraint(equalTo: notesStack.widthAnchor).isActive = true
        }
    }

    private func makeNoteRow(text: String) -> NSView {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        let previewLimit = 60
        let preview: String
        if trimmed.isEmpty {
            preview = "(без текста)"
        } else if trimmed.count > previewLimit {
            preview = String(trimmed.prefix(previewLimit)) + "…"
        } else {
            preview = trimmed
        }
        return QuickNoteRowView(previewText: preview) { [weak self] in
            self?.copyToClipboard(text)
        }
    }

    private func copyToClipboard(_ text: String) {
        guard !text.isEmpty else { return }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
        BackendToast.shared.show("Скопировано")
    }

    // MARK: - «→ Notes» видимость (Task 2 читает quick_capture_send_to_notes)

    func setSendToNotesVisible(_ visible: Bool) {
        sendToNotesButton.isHidden = !visible
    }

    // MARK: - Button actions

    @objc private func onToggleTapped() {
        onToggleRecording?()
    }

    @objc private func onCopyAllTapped() {
        copyToClipboard(liveTextView.string)
    }

    /// create_apple_note IPC — строго off-main (AGENT-3, CLAUDE.md). Параметры
    /// title/body/folder сверены с sendQuickCaptureNoteToAppleNotes
    /// (main+QuickCapture.swift) — тот же контракт backend'а
    /// (apple_integration_service.py::handle_create_apple_note).
    @objc private func onSendToNotesTapped() {
        let text = liveTextView.string
        guard !text.isEmpty else {
            BackendToast.shared.show("Нечего отправлять — текст пуст")
            return
        }
        guard let client = ipcClient else {
            BackendToast.shared.show("Нет соединения с backend'ом")
            return
        }
        let firstLine = text.split(separator: "\n", maxSplits: 1, omittingEmptySubsequences: false)
            .first.map(String.init) ?? text
        let trimmedFirstLine = firstLine.trimmingCharacters(in: .whitespacesAndNewlines)
        let title = trimmedFirstLine.count > 60
            ? String(trimmedFirstLine.prefix(60)) + "…"
            : trimmedFirstLine
        DispatchQueue.global(qos: .userInitiated).async {
            do {
                nonisolated(unsafe) let response = try client.call(
                    method: "create_apple_note",
                    params: ["title": title.isEmpty ? "Быстрая заметка" : title,
                             "body": text, "folder": "Krab Ear"])
                nonisolated(unsafe) let result = response["result"] as? [String: Any] ?? [:]
                DispatchQueue.main.async {
                    if (result["ok"] as? Bool) == true {
                        BackendToast.shared.show("Отправлено в Notes")
                    } else {
                        // Ответ backend'а несёт user_msg (человекочитаемо, напр.
                        // privacy-гейт) либо error (техническое сообщение osascript).
                        let message = (result["user_msg"] as? String)
                            ?? (result["error"] as? String)
                            ?? "Не удалось отправить в Notes"
                        BackendToast.shared.show(message)
                    }
                }
            } catch {
                DispatchQueue.main.async {
                    BackendToast.shared.show("Не удалось отправить в Notes: \(error.localizedDescription)")
                }
            }
        }
    }

    // MARK: - Header-таймер (mm:ss от startedAt, только пока isRecording)

    private func startTimer() {
        stopTimer()
        updateTimer()
        timerTick = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.updateTimer() }
        }
    }

    private func stopTimer() {
        timerTick?.invalidate()
        timerTick = nil
    }

    private func updateTimer() {
        guard isRecording, let startedAt else { return }
        let elapsed = max(0, Int(Date().timeIntervalSince1970 - startedAt))
        headerTimerLabel.stringValue = String(format: "%02d:%02d", elapsed / 60, elapsed % 60)
    }

    // MARK: - Panel setup (паттерн MeetingLivePanelController/ConversationStatusOverlay)

    private func setupPanel() {
        panel.styleMask.insert(.resizable)
        panel.minSize = NSSize(width: 300, height: 320)
        panel.level = .floating
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        panel.isMovableByWindowBackground = true
        panel.backgroundColor = .clear
        panel.hasShadow = true
        panel.isOpaque = false
        panel.alphaValue = 0.97
        // Живой смок волны C3b: `.closable` во flags БЕЗ `.titled` не рендерит
        // никакой видимый крестик (AX на реальной панели — 0 кнопок с
        // subrole AXCloseButton), у пользователя физически нет способа закрыть
        // панель кликом. `.titled` даёт нативную рабочую кнопку закрытия;
        // titlebarAppearsTransparent + titleVisibility=.hidden убирают саму
        // полосу тайтлбара визуально — стандартный приём безрамочных HUD-окон.
        panel.styleMask.insert(.titled)
        panel.titlebarAppearsTransparent = true
        panel.titleVisibility = .hidden
    }

    private func buildLayout() {
        headerTimerLabel.font = KrabEarTheme.Typography.display.tabular()
        headerTimerLabel.textColor = KrabEarTheme.Colors.textPrimary
        headerTimerLabel.isBordered = false
        headerTimerLabel.drawsBackground = false

        statusLabel.font = KrabEarTheme.Typography.caption
        statusLabel.textColor = KrabEarTheme.Colors.textSecondary
        statusLabel.isBordered = false
        statusLabel.drawsBackground = false
        statusLabel.lineBreakMode = .byTruncatingTail
        statusLabel.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)

        let headerRow = NSStackView(views: [recordIndicator, headerTimerLabel, statusLabel])
        headerRow.orientation = .horizontal
        headerRow.spacing = KrabEarTheme.Metrics.standard
        headerRow.alignment = .centerY

        liveTextView.isEditable = false
        liveTextView.isSelectable = true
        liveTextView.font = KrabEarTheme.Typography.body
        liveTextView.textColor = KrabEarTheme.Colors.textPrimary
        liveTextView.backgroundColor = .clear
        liveTextView.drawsBackground = false
        liveTextView.string = ""

        let liveScroll = NSScrollView()
        liveScroll.hasVerticalScroller = true
        liveScroll.drawsBackground = false
        liveScroll.documentView = liveTextView
        liveScroll.translatesAutoresizingMaskIntoConstraints = false
        liveScroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 120).isActive = true

        // Карточка-обрамление под live-текст — тот же приём, что
        // transcriptContainer в MeetingLivePanelController.
        placeholderLabel.font = KrabEarTheme.Typography.body
        placeholderLabel.textColor = KrabEarTheme.Colors.textSecondary
        placeholderLabel.isBordered = false
        placeholderLabel.drawsBackground = false
        placeholderLabel.alignment = .center
        placeholderLabel.translatesAutoresizingMaskIntoConstraints = false

        liveTextContainer.wantsLayer = true
        liveTextContainer.layer?.cornerRadius = KrabEarTheme.Metrics.innerCornerRadius
        liveTextContainer.layer?.backgroundColor = KrabEarTheme.Colors.cardBackground.cgColor
        liveTextContainer.layer?.borderColor = KrabEarTheme.Colors.border.cgColor
        liveTextContainer.layer?.borderWidth = 1.0
        liveTextContainer.translatesAutoresizingMaskIntoConstraints = false
        liveTextContainer.addSubview(placeholderLabel)
        liveTextContainer.addSubview(liveScroll)
        NSLayoutConstraint.activate([
            placeholderLabel.centerXAnchor.constraint(equalTo: liveTextContainer.centerXAnchor),
            placeholderLabel.centerYAnchor.constraint(equalTo: liveTextContainer.centerYAnchor),
            liveScroll.topAnchor.constraint(equalTo: liveTextContainer.topAnchor, constant: KrabEarTheme.Metrics.standard),
            liveScroll.bottomAnchor.constraint(equalTo: liveTextContainer.bottomAnchor, constant: -KrabEarTheme.Metrics.standard),
            liveScroll.leadingAnchor.constraint(equalTo: liveTextContainer.leadingAnchor, constant: KrabEarTheme.Metrics.standard),
            liveScroll.trailingAnchor.constraint(equalTo: liveTextContainer.trailingAnchor, constant: -KrabEarTheme.Metrics.standard),
        ])

        notesStack.orientation = .vertical
        notesStack.spacing = KrabEarTheme.Metrics.tight
        notesStack.alignment = .leading
        notesStack.translatesAutoresizingMaskIntoConstraints = false

        let notesScroll = NSScrollView()
        notesScroll.hasVerticalScroller = true
        notesScroll.drawsBackground = false
        notesScroll.documentView = notesStack
        notesScroll.translatesAutoresizingMaskIntoConstraints = false
        notesScroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 110).isActive = true

        toggleButton.target = self
        toggleButton.action = #selector(onToggleTapped)
        updateToggleButton()

        copyAllButton.target = self
        copyAllButton.action = #selector(onCopyAllTapped)

        sendToNotesButton.target = self
        sendToNotesButton.action = #selector(onSendToNotesTapped)
        // Видимость управляется владельцем через setSendToNotesVisible(_:) —
        // по умолчанию скрыта до того, как Task 2 прочитает
        // quick_capture_send_to_notes из настроек.
        sendToNotesButton.isHidden = true

        let rightButtons = NSStackView(views: [copyAllButton, sendToNotesButton])
        rightButtons.orientation = .horizontal
        rightButtons.spacing = KrabEarTheme.Metrics.standard
        
        let spacer = NSView()
        spacer.translatesAutoresizingMaskIntoConstraints = false
        spacer.setContentHuggingPriority(.defaultLow, for: .horizontal)

        let buttonsRow = NSStackView(views: [toggleButton, spacer, rightButtons])
        buttonsRow.orientation = .horizontal
        buttonsRow.spacing = KrabEarTheme.Metrics.standard
        buttonsRow.alignment = .centerY

        let contentStack = NSStackView(views: [
            headerRow, liveTextContainer, notesScroll, buttonsRow,
        ])
        contentStack.orientation = .vertical
        contentStack.spacing = KrabEarTheme.Metrics.comfortable
        contentStack.alignment = .leading
        contentStack.edgeInsets = NSEdgeInsets(
            top: KrabEarTheme.Metrics.spacious,
            left: KrabEarTheme.Metrics.spacious,
            bottom: KrabEarTheme.Metrics.spacious,
            right: KrabEarTheme.Metrics.spacious
        )
        contentStack.translatesAutoresizingMaskIntoConstraints = false

        let backdrop = NSVisualEffectView()
        backdrop.material = .popover
        backdrop.blendingMode = .behindWindow
        backdrop.state = .active
        backdrop.wantsLayer = true
        backdrop.layer?.cornerRadius = KrabEarTheme.Metrics.cardCornerRadius
        backdrop.layer?.cornerCurve = .continuous
        backdrop.layer?.masksToBounds = true

        backdrop.addSubview(contentStack)

        NSLayoutConstraint.activate([
            contentStack.topAnchor.constraint(equalTo: backdrop.topAnchor),
            contentStack.leadingAnchor.constraint(equalTo: backdrop.leadingAnchor),
            contentStack.trailingAnchor.constraint(equalTo: backdrop.trailingAnchor),
            contentStack.bottomAnchor.constraint(lessThanOrEqualTo: backdrop.bottomAnchor),
            liveTextContainer.widthAnchor.constraint(equalTo: contentStack.widthAnchor, constant: -(KrabEarTheme.Metrics.spacious * 2)),
            notesScroll.widthAnchor.constraint(equalTo: contentStack.widthAnchor, constant: -(KrabEarTheme.Metrics.spacious * 2)),
            notesStack.widthAnchor.constraint(equalTo: notesScroll.widthAnchor),
            // C3b ревью F6c: без пина ширины buttonsRow (contentStack.alignment
            // = .leading) стек хуг'ает контент — spacer между toggleButton и
            // rightButtons коллапсировал до ~0, вторичные кнопки не уезжали
            // вправо, как задумывал бриф полировки.
            buttonsRow.widthAnchor.constraint(equalTo: contentStack.widthAnchor, constant: -(KrabEarTheme.Metrics.spacious * 2)),
        ])

        backdrop.frame = panel.contentView!.bounds
        backdrop.autoresizingMask = [.width, .height]
        panel.contentView = backdrop

        let drag = NSPanGestureRecognizer(target: self, action: #selector(handleDrag(_:)))
        backdrop.addGestureRecognizer(drag)
    }

    // MARK: - Position persistence (паттерн MeetingLivePanelController/ConversationStatusOverlay)

    private func placeTopRight() {
        guard let screen = NSScreen.main else { return }
        let vf = screen.visibleFrame
        let size = panel.frame.size
        let x = vf.maxX - size.width - 24
        let y = vf.maxY - size.height - 24
        panel.setFrame(NSRect(x: x, y: y, width: size.width, height: size.height), display: true)
    }

    /// Валидна ли candidate-позиция — хотя бы 80% площади пересекается с
    /// visibleFrame какого-нибудь ТЕКУЩЕГО экрана (без этого отключение
    /// второго монитора навсегда прячет панель за экраном).
    private func isOnScreen(_ candidate: NSRect) -> Bool {
        let totalArea = candidate.width * candidate.height
        guard totalArea > 0 else { return false }
        return NSScreen.screens.contains { screen in
            let intersection = candidate.intersection(screen.visibleFrame)
            let coveredArea = intersection.width * intersection.height
            return coveredArea / totalArea >= 0.80
        }
    }

    private func restorePosition() {
        if let saved = UserDefaults.standard.string(forKey: positionKey),
           let data = saved.data(using: .utf8),
           let dict = try? JSONSerialization.jsonObject(with: data) as? [String: CGFloat],
           let x = dict["x"], let y = dict["y"] {
            let size = panel.frame.size
            let candidate = NSRect(x: x, y: y, width: size.width, height: size.height)
            if isOnScreen(candidate) {
                panel.setFrame(candidate, display: false)
                return
            }
        }
        placeTopRight()
    }

    private func savePosition() {
        let origin = panel.frame.origin
        let dict: [String: CGFloat] = ["x": origin.x, "y": origin.y]
        if let data = try? JSONSerialization.data(withJSONObject: dict),
           let str = String(data: data, encoding: .utf8) {
            UserDefaults.standard.set(str, forKey: positionKey)
        }
    }

    @objc private func handleDrag(_ gr: NSPanGestureRecognizer) {
        if gr.state == .ended || gr.state == .changed {
            savePosition()
        }
    }
}

/// Строка списка заметок: текст-превью + кнопка «Копировать». Отдельный
/// маленький NSView (паттерн SpeakerChipView в MeetingLivePanelController) —
/// хранит колбэк копирования, а не полагается на representedObject кнопки.
private final class QuickNoteRowView: NSView {
    private let onCopy: () -> Void

    init(previewText: String, onCopy: @escaping () -> Void) {
        self.onCopy = onCopy
        super.init(frame: .zero)

        self.wantsLayer = true
        self.layer?.cornerRadius = KrabEarTheme.Metrics.innerCornerRadius
        self.layer?.backgroundColor = KrabEarTheme.Colors.cardBackground.cgColor
        self.layer?.borderColor = KrabEarTheme.Colors.border.cgColor
        self.layer?.borderWidth = 1.0

        let label = NSTextField(labelWithString: previewText)
        label.font = KrabEarTheme.Typography.caption
        label.textColor = KrabEarTheme.Colors.textPrimary
        label.isBordered = false
        label.drawsBackground = false
        label.lineBreakMode = .byTruncatingTail
        label.maximumNumberOfLines = 1
        label.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)

        let copyButton = ThemeSecondaryButton(title: "", target: self, action: #selector(handleCopyTap))
        copyButton.image = NSImage(systemSymbolName: "doc.on.doc", accessibilityDescription: "Копировать")
        copyButton.imagePosition = .imageOnly
        copyButton.controlSize = .small
        copyButton.setContentHuggingPriority(.required, for: .horizontal)

        let row = NSStackView(views: [label, copyButton])
        row.orientation = .horizontal
        row.spacing = KrabEarTheme.Metrics.standard
        row.alignment = .centerY
        row.translatesAutoresizingMaskIntoConstraints = false

        addSubview(row)
        NSLayoutConstraint.activate([
            row.topAnchor.constraint(equalTo: topAnchor, constant: KrabEarTheme.Metrics.tight),
            row.bottomAnchor.constraint(equalTo: bottomAnchor, constant: -KrabEarTheme.Metrics.tight),
            row.leadingAnchor.constraint(equalTo: leadingAnchor, constant: KrabEarTheme.Metrics.standard),
            row.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -KrabEarTheme.Metrics.standard),
        ])
    }

    required init?(coder: NSCoder) { fatalError() }

    @objc private func handleCopyTap() { onCopy() }
}
