import SwiftUI
import AppKit

@main
struct SettingsPreviewApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var appDelegate
    
    var body: some Scene {
        WindowGroup {
            SettingsView()
                .frame(minWidth: 700, minHeight: 500)
                .background(VisualEffectView(material: .sidebar, blendingMode: .behindWindow).ignoresSafeArea())
        }
        .windowStyle(.hiddenTitleBar)
    }
}

class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        NSApp.activate(ignoringOtherApps: true)
    }
}

struct VisualEffectView: NSViewRepresentable {
    var material: NSVisualEffectView.Material
    var blendingMode: NSVisualEffectView.BlendingMode

    func makeNSView(context: Context) -> NSVisualEffectView {
        let view = NSVisualEffectView()
        view.material = material
        view.blendingMode = blendingMode
        view.state = .active
        return view
    }

    func updateNSView(_ nsView: NSVisualEffectView, context: Context) {
        nsView.material = material
        nsView.blendingMode = blendingMode
    }
}

enum SettingsSection: String, CaseIterable, Identifiable {
    case record = "Запись"
    case translate = "Перевод"
    case ai = "AI-переписчик"
    case privacy = "Приватность"
    case integrations = "Интеграции"
    case notifications = "Уведомления"
    
    var id: String { self.rawValue }
    
    var iconName: String {
        switch self {
        case .record: return "mic.fill"
        case .translate: return "globe"
        case .ai: return "brain"
        case .privacy: return "hand.raised.fill"
        case .integrations: return "puzzlepiece.fill"
        case .notifications: return "bell.badge.fill"
        }
    }
    
    var iconColor: Color {
        switch self {
        case .record: return .red
        case .translate: return .blue
        case .ai: return .purple
        case .privacy: return .teal
        case .integrations: return .orange
        case .notifications: return .pink
        }
    }
}

struct SettingsView: View {
    @State private var selectedSection: SettingsSection? = .record
    
    var body: some View {
        NavigationSplitView {
            List(SettingsSection.allCases, selection: $selectedSection) { section in
                NavigationLink(value: section) {
                    HStack(spacing: 12) {
                        ZStack {
                            RoundedRectangle(cornerRadius: 6, style: .continuous)
                                .fill(section.iconColor)
                                .frame(width: 24, height: 24)
                            Image(systemName: section.iconName)
                                .font(.system(size: 13, weight: .medium))
                                .foregroundColor(.white)
                        }
                        Text(section.rawValue)
                            .font(.system(size: 14))
                    }
                    .padding(.vertical, 2)
                }
            }
            .navigationTitle("Настройки")
            .listStyle(.sidebar)
        } detail: {
            if let section = selectedSection {
                DetailView(section: section)
            } else {
                Text("Выберите раздел")
                    .foregroundColor(.secondary)
            }
        }
    }
}

struct DetailView: View {
    let section: SettingsSection
    
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 24) {
                Text(section.rawValue)
                    .font(.system(size: 24, weight: .bold))
                    .padding(.bottom, 8)
                
                switch section {
                case .record:
                    RecordSettings()
                case .translate:
                    TranslateSettings()
                case .ai:
                    AISettings()
                case .privacy:
                    PrivacySettings()
                case .integrations:
                    IntegrationSettings()
                case .notifications:
                    NotificationSettings()
                }
                
                Spacer()
            }
            .padding(32)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .background(Color(NSColor.controlBackgroundColor))
    }
}

struct RecordSettings: View {
    @State private var quality = "High"
    @State private var useGigaAM = true
    
    var body: some View {
        Form {
            Picker("Профиль качества:", selection: $quality) {
                Text("Low").tag("Low")
                Text("Medium").tag("Medium")
                Text("High").tag("High")
            }
            .pickerStyle(.radioGroup)
            
            Toggle("Использовать GigaAM вместо Whisper", isOn: $useGigaAM)
        }
        .formStyle(.grouped)
    }
}

struct TranslateSettings: View {
    @State private var langPair = "RU -> EN"
    
    var body: some View {
        Form {
            Picker("Языковая пара:", selection: $langPair) {
                Text("RU -> EN").tag("RU -> EN")
                Text("EN -> RU").tag("EN -> RU")
            }
            
            Button("Настроить глоссарий...") { }
        }
        .formStyle(.grouped)
    }
}

struct AISettings: View {
    @State private var model = "GPT-4o"
    @State private var temperature = 0.7
    
    var body: some View {
        Form {
            Picker("Модель LLM:", selection: $model) {
                Text("GPT-4o").tag("GPT-4o")
                Text("Claude 3.5 Sonnet").tag("Claude")
                Text("Gemini 1.5 Pro").tag("Gemini")
            }
            
            VStack(alignment: .leading) {
                Text("Креативность (Temperature): \(temperature, specifier: "%.1f")")
                Slider(value: $temperature, in: 0...1, step: 0.1)
            }
        }
        .formStyle(.grouped)
    }
}

struct PrivacySettings: View {
    @State private var privacyMode = false
    
    var body: some View {
        Form {
            Toggle("Приватный режим (Локальная обработка)", isOn: $privacyMode)
            
            Button("Очистить все данные (Purge)", role: .destructive) { }
                .buttonStyle(.borderedProminent)
                .tint(.red)
        }
        .formStyle(.grouped)
    }
}

struct IntegrationSettings: View {
    @State private var imessage = true
    @State private var notes = true
    @State private var calendar = false
    @State private var telegram = false
    
    var body: some View {
        Form {
            Toggle("Apple iMessage", isOn: $imessage)
            Toggle("Apple Notes", isOn: $notes)
            Toggle("Apple Calendar", isOn: $calendar)
            Toggle("Telegram Bridge", isOn: $telegram)
        }
        .formStyle(.grouped)
    }
}

struct NotificationSettings: View {
    @State private var confidence = 0.8
    
    var body: some View {
        Form {
            VStack(alignment: .leading) {
                Text("Порог предупреждений (Confidence): \(confidence, specifier: "%.2f")")
                Slider(value: $confidence, in: 0...1, step: 0.05)
            }
        }
        .formStyle(.grouped)
    }
}
