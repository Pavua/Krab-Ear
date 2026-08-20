/*
 MemoryDashboardViewController — визуальная панель для Memory Conductor.
 Показывает состояние резидентов (active/warm/idle) и статистику дирижёра.
*/

import AppKit
import Foundation

// MARK: - Data model

struct MemoryLedgerData {
    struct ResidentEntry {
        let name: String
        let owner: String
        let sizeMB: Double
        let state: String
        let idleSinceTs: Double?
        let updatedTs: Double
    }
    
    struct ConductorState {
        let enabled: Bool
        let threadAlive: Bool
        let lastTickTs: Double?
        let shadowSince: Double?
        let pressureStreak: Int
        
        struct ResidentDecision {
            let attempted: Int
            let succeeded: Int
            let skippedGate: Int
            let unknown: Int
            let failed: Int
            let would: Int
        }
        let residents: [String: ResidentDecision]
        let decisions: [String]
    }
    
    var entries: [ResidentEntry] = []
    var conductor: ConductorState?
    var totalSizeMB: Double = 0
    
    static func parse(from dict: [String: Any]) -> MemoryLedgerData {
        var d = MemoryLedgerData()
        
        guard let ledger = dict["ledger"] as? [String: Any],
              let entriesDict = ledger["entries"] as? [String: Any] else {
            return d
        }
        
        for (key, val) in entriesDict {
            guard let entryDict = val as? [String: Any] else { continue }
            let name = key.split(separator: "/").last.map(String.init) ?? key
            let owner = (entryDict["owner"] as? String) ?? ""
            let sizeMB = (entryDict["size_mb"] as? Double) ?? (entryDict["size_mb"] as? NSNumber)?.doubleValue ?? 0
            let state = (entryDict["state"] as? String) ?? "unknown"
            let idleSince = (entryDict["idle_since_ts"] as? Double) ?? (entryDict["idle_since_ts"] as? NSNumber)?.doubleValue
            let updated = (entryDict["updated_ts"] as? Double) ?? (entryDict["updated_ts"] as? NSNumber)?.doubleValue ?? 0
            
            d.entries.append(ResidentEntry(name: name, owner: owner, sizeMB: sizeMB, state: state, idleSinceTs: idleSince, updatedTs: updated))
            d.totalSizeMB += sizeMB
        }
        d.entries.sort { $0.sizeMB > $1.sizeMB }
        
        if let cond = dict["conductor"] as? [String: Any] {
            let enabled = cond["enabled"] as? Bool ?? false
            let threadAlive = cond["thread_alive"] as? Bool ?? false
            let lastTickTs = (cond["last_tick_ts"] as? Double) ?? (cond["last_tick_ts"] as? NSNumber)?.doubleValue
            let shadowSince = (cond["shadow_since"] as? Double) ?? (cond["shadow_since"] as? NSNumber)?.doubleValue
            let pressureStreak = cond["pressure_streak"] as? Int ?? (cond["pressure_streak"] as? NSNumber)?.intValue ?? 0
            
            var residentsMap: [String: ConductorState.ResidentDecision] = [:]
            if let residentsDict = cond["residents"] as? [String: Any] {
                for (rKey, rVal) in residentsDict {
                    if let rDict = rVal as? [String: Any] {
                        residentsMap[rKey] = ConductorState.ResidentDecision(
                            attempted: rDict["attempted"] as? Int ?? (rDict["attempted"] as? NSNumber)?.intValue ?? 0,
                            succeeded: rDict["succeeded"] as? Int ?? (rDict["succeeded"] as? NSNumber)?.intValue ?? 0,
                            skippedGate: rDict["skipped_gate"] as? Int ?? (rDict["skipped_gate"] as? NSNumber)?.intValue ?? 0,
                            unknown: rDict["unknown"] as? Int ?? (rDict["unknown"] as? NSNumber)?.intValue ?? 0,
                            failed: rDict["failed"] as? Int ?? (rDict["failed"] as? NSNumber)?.intValue ?? 0,
                            would: rDict["would"] as? Int ?? (rDict["would"] as? NSNumber)?.intValue ?? 0
                        )
                    }
                }
            }
            
            let decisions = cond["decisions"] as? [String] ?? []
            d.conductor = ConductorState(enabled: enabled, threadAlive: threadAlive, lastTickTs: lastTickTs, shadowSince: shadowSince, pressureStreak: pressureStreak, residents: residentsMap, decisions: decisions)
        }
        
        return d
    }
}

// MARK: - Main window controller

@MainActor
final class MemoryDashboardWindowController: NSWindowController {
    convenience init(ipcClient: IPCClient) {
        let win = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 640, height: 720),
            styleMask: [.titled, .closable, .resizable, .miniaturizable],
            backing: .buffered,
            defer: false
        )
        win.title = "Память (Дирижёр)"
        win.minSize = NSSize(width: 600, height: 600)
        let vc = MemoryDashboardViewController(ipcClient: ipcClient)
        win.contentViewController = vc
        self.init(window: win)
    }
}

// MARK: - View Controller

@MainActor
final class MemoryDashboardViewController: NSViewController {
    let ipcClient: IPCClient
    private var data = MemoryLedgerData()
    private var isLoading = false
    private var refreshTimer: Timer?
    
    private let scrollView = NSScrollView()
    private let contentStack = NSStackView()
    private let statusLabel = NSTextField(labelWithString: "")
    
    // Sections
    private let residentsStack = NSStackView()
    private let residentsTotalLabel = NSTextField(labelWithString: "")
    private let modeTitleLabel = NSTextField(labelWithString: "")
    private let modeDetailsLabel = NSTextField(labelWithString: "")
    private let decisionsTableStack = NSStackView()
    private let decisionsLogView = NSTextView()
    
    init(ipcClient: IPCClient) {
        self.ipcClient = ipcClient
        super.init(nibName: nil, bundle: nil)
    }
    
    required init?(coder: NSCoder) {
        fatalError("init(coder:) not supported")
    }
    
    override func loadView() {
        view = NSView(frame: NSRect(x: 0, y: 0, width: 640, height: 720))
        view.wantsLayer = true
    }
    
    override func viewDidLoad() {
        super.viewDidLoad()
        buildUI()
        refresh()
    }
    
    override func viewDidAppear() {
        super.viewDidAppear()
        refreshTimer = Timer.scheduledTimer(withTimeInterval: 5.0, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.refresh() }
        }
    }
    
    override func viewDidDisappear() {
        super.viewDidDisappear()
        refreshTimer?.invalidate()
        refreshTimer = nil
    }
    
    private func buildUI() {
        let toolbarStack = NSStackView()
        toolbarStack.orientation = .horizontal
        toolbarStack.spacing = KrabEarTheme.Metrics.standard
        toolbarStack.alignment = .centerY
        
        let titleLabel = NSTextField(labelWithString: "Состояние памяти")
        titleLabel.font = .systemFont(ofSize: 16, weight: .semibold)
        titleLabel.textColor = KrabEarTheme.Colors.textPrimary
        
        let refreshBtn = ThemeSecondaryButton(title: "Обновить", target: self, action: #selector(onRefresh))
        
        statusLabel.font = KrabEarTheme.Typography.caption
        statusLabel.textColor = KrabEarTheme.Colors.textSecondary
        statusLabel.stringValue = ""
        
        let spacer = NSView()
        spacer.setContentHuggingPriority(.defaultLow, for: .horizontal)
        
        toolbarStack.addArrangedSubview(titleLabel)
        toolbarStack.addArrangedSubview(spacer)
        toolbarStack.addArrangedSubview(statusLabel)
        toolbarStack.addArrangedSubview(refreshBtn)
        
        scrollView.translatesAutoresizingMaskIntoConstraints = false
        scrollView.hasVerticalScroller = true
        scrollView.drawsBackground = false
        
        contentStack.orientation = .vertical
        contentStack.spacing = KrabEarTheme.Metrics.standard
        contentStack.alignment = .leading
        contentStack.edgeInsets = NSEdgeInsets(
            top: KrabEarTheme.Metrics.comfortable,
            left: KrabEarTheme.Metrics.comfortable,
            bottom: KrabEarTheme.Metrics.comfortable,
            right: KrabEarTheme.Metrics.comfortable
        )
        contentStack.translatesAutoresizingMaskIntoConstraints = false
        
        contentStack.addArrangedSubview(buildResidentsSection())
        contentStack.addArrangedSubview(buildModeSection())
        contentStack.addArrangedSubview(buildDecisionsSection())
        
        let clipView = NSClipView()
        clipView.documentView = contentStack
        scrollView.contentView = clipView
        
        toolbarStack.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(toolbarStack)
        view.addSubview(scrollView)
        
        NSLayoutConstraint.activate([
            toolbarStack.topAnchor.constraint(equalTo: view.topAnchor, constant: KrabEarTheme.Metrics.comfortable),
            toolbarStack.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: KrabEarTheme.Metrics.comfortable),
            toolbarStack.trailingAnchor.constraint(equalTo: view.trailingAnchor, constant: -KrabEarTheme.Metrics.comfortable),
            
            scrollView.topAnchor.constraint(equalTo: toolbarStack.bottomAnchor, constant: KrabEarTheme.Metrics.standard),
            scrollView.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            scrollView.trailingAnchor.constraint(equalTo: view.trailingAnchor),
            scrollView.bottomAnchor.constraint(equalTo: view.bottomAnchor),
            
            contentStack.widthAnchor.constraint(equalTo: scrollView.widthAnchor),
        ])
    }
    
    private func buildSectionCard(title: String) -> (CollapsibleSectionView, ThemeCardView) {
        let section = CollapsibleSectionView(sectionId: "memory_\(title.lowercased())", title: title, isExpanded: true)
        let card = ThemeCardView()
        card.translatesAutoresizingMaskIntoConstraints = false
        section.contentStackView.addArrangedSubview(card)
        section.translatesAutoresizingMaskIntoConstraints = false
        return (section, card)
    }
    
    private func buildResidentsSection() -> NSView {
        let (section, card) = buildSectionCard(title: "Резиденты")
        
        residentsStack.orientation = .vertical
        residentsStack.spacing = KrabEarTheme.Metrics.tight
        residentsStack.alignment = .leading
        
        residentsTotalLabel.font = KrabEarTheme.Typography.body
        residentsTotalLabel.textColor = KrabEarTheme.Colors.textPrimary
        
        card.contentStackView.addArrangedSubview(residentsStack)
        card.contentStackView.addArrangedSubview(residentsTotalLabel)
        
        return section
    }
    
    private func buildModeSection() -> NSView {
        let (section, card) = buildSectionCard(title: "Режим дирижёра")
        
        modeTitleLabel.font = .systemFont(ofSize: 18, weight: .bold)
        modeTitleLabel.textColor = KrabEarTheme.Colors.textPrimary
        
        modeDetailsLabel.font = KrabEarTheme.Typography.body
        modeDetailsLabel.textColor = KrabEarTheme.Colors.textSecondary
        
        card.contentStackView.addArrangedSubview(modeTitleLabel)
        card.contentStackView.addArrangedSubview(modeDetailsLabel)
        
        return section
    }
    
    private func buildDecisionsSection() -> NSView {
        let (section, card) = buildSectionCard(title: "Решения")
        
        decisionsTableStack.orientation = .vertical
        decisionsTableStack.spacing = 4
        decisionsTableStack.alignment = .leading
        
        let logScroll = NSScrollView()
        logScroll.hasVerticalScroller = true
        logScroll.translatesAutoresizingMaskIntoConstraints = false
        logScroll.heightAnchor.constraint(equalToConstant: 150).isActive = true
        
        decisionsLogView.isEditable = false
        decisionsLogView.isSelectable = true
        decisionsLogView.font = KrabEarTheme.Typography.monospace
        decisionsLogView.backgroundColor = KrabEarTheme.Colors.cardBackground
        decisionsLogView.textColor = KrabEarTheme.Colors.textPrimary
        
        logScroll.documentView = decisionsLogView
        
        card.contentStackView.addArrangedSubview(decisionsTableStack)
        card.contentStackView.addArrangedSubview(logScroll)
        
        logScroll.widthAnchor.constraint(equalTo: card.contentStackView.widthAnchor).isActive = true
        
        return section
    }
    
    @objc private func onRefresh() {
        refresh()
    }
    
    private func refresh() {
        guard !isLoading else { return }
        isLoading = true
        statusLabel.stringValue = "Загрузка…"
        
        let client = ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            do {
                let response = try client.call(method: "get_memory_ledger", params: [:])
                let result = (response["result"] as? [String: Any]) ?? [:]
                let parsed = MemoryLedgerData.parse(from: result)
                DispatchQueue.main.async {
                    self?.apply(parsed)
                    self?.isLoading = false
                    
                    let f = DateFormatter()
                    f.timeStyle = .short
                    self?.statusLabel.stringValue = "Обновлено: \(f.string(from: Date()))"
                }
            } catch {
                DispatchQueue.main.async {
                    self?.isLoading = false
                    self?.statusLabel.stringValue = "Ошибка: \(error.localizedDescription)"
                    
                    self?.showEmptyState("Данные недоступны")
                }
            }
        }
    }
    
    private func showEmptyState(_ msg: String) {
        residentsStack.arrangedSubviews.forEach { $0.removeFromSuperview() }
        let emptyLabel = NSTextField(labelWithString: msg)
        emptyLabel.textColor = KrabEarTheme.Colors.textSecondary
        residentsStack.addArrangedSubview(emptyLabel)
        
        modeTitleLabel.stringValue = msg
        modeDetailsLabel.stringValue = ""
        decisionsTableStack.arrangedSubviews.forEach { $0.removeFromSuperview() }
        decisionsLogView.string = ""
    }
    
    private func apply(_ d: MemoryLedgerData) {
        self.data = d
        
        if d.entries.isEmpty && d.conductor == nil {
            showEmptyState("Дирижёр ещё не публиковал данные")
            return
        }
        
        let nowTs = Date().timeIntervalSince1970
        
        // Residents
        residentsStack.arrangedSubviews.forEach { $0.removeFromSuperview() }
        let maxMB = d.entries.map { $0.sizeMB }.max() ?? 1024.0
        let baseline = max(maxMB, 1024.0)
        
        for entry in d.entries {
            let row = NSStackView()
            row.orientation = .horizontal
            row.spacing = KrabEarTheme.Metrics.standard
            row.alignment = .centerY
            
            let nameLbl = NSTextField(labelWithString: "\(entry.name) (\(entry.owner))")
            nameLbl.font = KrabEarTheme.Typography.body
            nameLbl.textColor = KrabEarTheme.Colors.textPrimary
            nameLbl.translatesAutoresizingMaskIntoConstraints = false
            nameLbl.widthAnchor.constraint(equalToConstant: 160).isActive = true
            
            let barContainer = NSView()
            barContainer.wantsLayer = true
            barContainer.layer?.backgroundColor = KrabEarTheme.Colors.border.cgColor
            barContainer.layer?.cornerRadius = 4
            barContainer.translatesAutoresizingMaskIntoConstraints = false
            barContainer.widthAnchor.constraint(equalToConstant: 200).isActive = true
            barContainer.heightAnchor.constraint(equalToConstant: 12).isActive = true
            
            let pct = max(0.02, min(1.0, entry.sizeMB / baseline))
            let barFill = NSView()
            barFill.wantsLayer = true
            barFill.layer?.cornerRadius = 4
            
            if entry.state == "active" {
                barFill.layer?.backgroundColor = KrabEarTheme.Colors.accent.cgColor
            } else if entry.state == "warm" {
                barFill.layer?.backgroundColor = NSColor.systemGray.cgColor
            } else {
                barFill.layer?.backgroundColor = KrabEarTheme.Colors.textDisabled.cgColor
            }
            
            barFill.translatesAutoresizingMaskIntoConstraints = false
            barContainer.addSubview(barFill)
            
            NSLayoutConstraint.activate([
                barFill.leadingAnchor.constraint(equalTo: barContainer.leadingAnchor),
                barFill.topAnchor.constraint(equalTo: barContainer.topAnchor),
                barFill.bottomAnchor.constraint(equalTo: barContainer.bottomAnchor),
                barFill.widthAnchor.constraint(equalTo: barContainer.widthAnchor, multiplier: CGFloat(pct))
            ])
            
            var detailText = String(format: "%.1f ГБ", entry.sizeMB / 1024.0)
            if entry.state == "idle", let idleSince = entry.idleSinceTs {
                let mins = max(0, Int((nowTs - idleSince) / 60))
                detailText += " (простаивает \(mins) мин)"
            } else {
                detailText += " (\(entry.state))"
            }
            
            let detailLbl = NSTextField(labelWithString: detailText)
            detailLbl.font = KrabEarTheme.Typography.caption
            detailLbl.textColor = entry.state == "active" ? KrabEarTheme.Colors.textPrimary : KrabEarTheme.Colors.textSecondary
            
            row.addArrangedSubview(nameLbl)
            row.addArrangedSubview(barContainer)
            row.addArrangedSubview(detailLbl)
            
            residentsStack.addArrangedSubview(row)
        }
        
        residentsTotalLabel.stringValue = String(format: "Суммарный объём: %.1f ГБ", d.totalSizeMB / 1024.0)
        
        // Conductor
        if let cond = d.conductor {
            if let shadowSince = cond.shadowSince {
                let days = max(0, Int((nowTs - shadowSince) / 86400))
                modeTitleLabel.stringValue = "SHADOW — решения только логируются (\(days) дн)"
                modeTitleLabel.textColor = NSColor.systemOrange
            } else {
                modeTitleLabel.stringValue = "ENFORCE"
                modeTitleLabel.textColor = KrabEarTheme.Colors.success
            }
            
            var details = [] as [String]
            details.append(cond.threadAlive ? "Поток жив" : "Поток мёртв")
            details.append("Давление: \(cond.pressureStreak)")
            if let tick = cond.lastTickTs {
                let secs = max(0, Int(nowTs - tick))
                details.append("Последний тик: \(secs) сек назад")
            }
            modeDetailsLabel.stringValue = details.joined(separator: " · ")
            
            // Decisions table
            decisionsTableStack.arrangedSubviews.forEach { $0.removeFromSuperview() }
            
            let headerStr = String(format: "%-16s | %5s | %5s | %5s | %5s | %5s | %5s", "Resident", "would", "attem", "succe", "skipG", "unkn", "fail")
            let headerLbl = NSTextField(labelWithString: headerStr)
            headerLbl.font = KrabEarTheme.Typography.monospace
            headerLbl.textColor = KrabEarTheme.Colors.textSecondary
            decisionsTableStack.addArrangedSubview(headerLbl)
            
            for (rName, dec) in cond.residents.sorted(by: { $0.key < $1.key }) {
                let rowStr = String(format: "%-16s | %5d | %5d | %5d | %5d | %5d | %5d",
                                    String(rName.prefix(16)), dec.would, dec.attempted, dec.succeeded, dec.skippedGate, dec.unknown, dec.failed)
                let rowLbl = NSTextField(labelWithString: rowStr)
                rowLbl.font = KrabEarTheme.Typography.monospace
                rowLbl.textColor = KrabEarTheme.Colors.textPrimary
                decisionsTableStack.addArrangedSubview(rowLbl)
            }
            
            if cond.residents.isEmpty {
                let emptyLbl = NSTextField(labelWithString: "Нет активности")
                emptyLbl.textColor = KrabEarTheme.Colors.textSecondary
                decisionsTableStack.addArrangedSubview(emptyLbl)
            }
            
            decisionsLogView.string = cond.decisions.joined(separator: "\n")
            decisionsLogView.scrollToEndOfDocument(nil)
            
        } else {
            modeTitleLabel.stringValue = "Нет данных дирижёра"
            modeDetailsLabel.stringValue = ""
            decisionsTableStack.arrangedSubviews.forEach { $0.removeFromSuperview() }
            decisionsLogView.string = ""
        }
    }
}
