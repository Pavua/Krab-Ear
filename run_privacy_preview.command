#!/usr/bin/env swift

import AppKit
import SwiftUI

/// Модель состояния для превью
class PreviewState: ObservableObject {
    @Published var isPrivacyMode: Bool = true
}

/// SwiftUI-совместимое описание StatusIndicatorView с поддержкой приватного режима
struct StatusIndicatorPreview: View {
    @ObservedObject var state: PreviewState
    var healthColor: Color = .green

    var body: some View {
        Circle()
            .fill(state.isPrivacyMode ? Color(NSColor.systemPurple) : healthColor)
            .frame(width: 8, height: 8)
            .overlay(
                // Имитация severity badge из Phase B.1 (если нужен)
                Group {
                    if !state.isPrivacyMode {
                        Circle()
                            .fill(Color.clear)
                            .frame(width: 4, height: 4)
                            .offset(x: 2, y: -2)
                    }
                }
            )
    }
}

/// Компактный баннер "Приватный режим"
struct PrivacyBannerView: View {
    var onDismiss: () -> Void

    var body: some View {
        HStack(spacing: 6) {
            Image(systemName: "lock.fill")
                .font(.system(size: 10, weight: .medium))
                .foregroundColor(Color(NSColor.systemPurple))
            
            Text("Приватный режим")
                .font(.system(size: 12, weight: .medium))
                .foregroundColor(.secondary)
            
            Spacer()
            
            Button(action: onDismiss) {
                Image(systemName: "xmark")
                    .font(.system(size: 8, weight: .bold))
                    .foregroundColor(.secondary)
            }
            .buttonStyle(PlainButtonStyle())
            .padding(.trailing, 4)
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 4)
        .frame(height: 28)
        .background(
            VisualEffectView(material: .hudWindow, blendingMode: .withinWindow)
                .cornerRadius(6)
        )
        .overlay(
            RoundedRectangle(cornerRadius: 6)
                .stroke(Color(NSColor.systemPurple).opacity(0.3), lineWidth: 1)
        )
    }
}

/// Имитация таба с замком
struct HistoryTabItem: View {
    let title: String
    let isSelected: Bool
    @ObservedObject var state: PreviewState

    var body: some View {
        HStack(spacing: 4) {
            Text(title)
                .font(.system(size: 13, weight: isSelected ? .semibold : .regular))
                .foregroundColor(isSelected ? .primary : .secondary)
            
            if state.isPrivacyMode {
                Image(systemName: "lock.fill")
                    .font(.system(size: 8))
                    .foregroundColor(Color(NSColor.systemPurple))
                    .offset(y: -4) // Badge overlay effect
            }
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 4)
        .background(isSelected ? Color.secondary.opacity(0.2) : Color.clear)
        .cornerRadius(4)
    }
}

/// Вспомогательная view для NSVisualEffectView
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

/// Основной экран Header'а
struct HistoryPanelHeaderPreview: View {
    @StateObject private var state = PreviewState()
    @State private var showBanner = true

    var body: some View {
        VStack(spacing: 0) {
            // Верхняя часть заголовка (Tabs + Status)
            HStack {
                StatusIndicatorPreview(state: state)
                
                HStack(spacing: 12) {
                    HistoryTabItem(title: "История", isSelected: true, state: state)
                    HistoryTabItem(title: "Аналитика", isSelected: false, state: state)
                }
                .padding(.leading, 8)
                
                Spacer()
                
                Toggle("Privacy Mode", isOn: $state.isPrivacyMode)
                    .toggleStyle(SwitchToggleStyle(tint: Color(NSColor.systemPurple)))
                    .onChange(of: state.isPrivacyMode) { newValue in
                        if newValue { showBanner = true }
                    }
            }
            .padding()
            
            // Баннер ниже вьюхи табов, чтобы не перекрывать их
            if state.isPrivacyMode && showBanner {
                PrivacyBannerView {
                    showBanner = false
                }
                .padding(.horizontal)
                .padding(.bottom, 8)
                .transition(.opacity.combined(with: .move(edge: .top)))
            }
            
            Divider()
            
            Spacer()
            
            Text("Контент панели...")
                .foregroundColor(.secondary)
            
            Spacer()
        }
        .frame(width: 450, height: 250)
        .background(VisualEffectView(material: .popover, blendingMode: .behindWindow))
    }
}

// Запуск приложения
let app = NSApplication.shared
app.setActivationPolicy(.regular)

let window = NSWindow(
    contentRect: NSRect(x: 0, y: 0, width: 450, height: 250),
    styleMask: [.titled, .closable, .miniaturizable, .fullSizeContentView],
    backing: .buffered,
    defer: false
)
window.center()
window.title = "Krab Ear Privacy Mode Preview"
window.titlebarAppearsTransparent = true
window.isMovableByWindowBackground = true

let hostingView = NSHostingView(rootView: HistoryPanelHeaderPreview())
window.contentView = hostingView

window.makeKeyAndOrderFront(nil)
app.activate(ignoringOtherApps: true)
app.run()
