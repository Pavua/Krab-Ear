import AppKit
import WebKit

// MARK: - Экспорт аналитического отчёта в PDF
//
// Переиспользует backend generate_html_report (он отдаёт полный HTML-документ с
// CSS) и рендерит его в PDF нативно через WKWebView.createPDF — высокая вёрстка-
// точность без Python-зависимостей. PDF пишется во временный файл и открывается
// в Preview (юзер ⌘S сохранит/расшарит куда угодно), как и HTML-экспорт.
//
// WebKit работает ТОЛЬКО на main thread; loadHTMLString → didFinish → createPDF —
// асинхронная цепочка, поэтому рендерер держит сам себя живым (static active)
// до завершения.

/// Одноразовый рендерер HTML → PDF поверх off-screen WKWebView.
final class HTMLToPDFRenderer: NSObject, WKNavigationDelegate {
    private var webView: WKWebView?
    private var completion: ((Data?) -> Void)?
    // Удерживаем активный рендерер живым на время async-операции.
    nonisolated(unsafe) private static var active: HTMLToPDFRenderer?

    /// Рендерит HTML в PDF. completion вызывается на main thread (WebKit-колбэки
    /// приходят на main). nil при ошибке загрузки/рендера.
    @MainActor
    func render(html: String, completion: @escaping (Data?) -> Void) {
        // Лист ~US Letter при 72 dpi (612×792 pt). createPDF снимает весь контент.
        let config = WKWebViewConfiguration()
        let wv = WKWebView(frame: NSRect(x: 0, y: 0, width: 612, height: 792), configuration: config)
        wv.navigationDelegate = self
        self.webView = wv
        self.completion = completion
        HTMLToPDFRenderer.active = self
        wv.loadHTMLString(html, baseURL: nil)
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        let pdfConfig = WKPDFConfiguration()
        webView.createPDF(configuration: pdfConfig) { [weak self] result in
            switch result {
            case .success(let data): self?.finish(data)
            case .failure: self?.finish(nil)
            }
        }
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        finish(nil)
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        finish(nil)
    }

    private func finish(_ data: Data?) {
        let cb = completion
        completion = nil
        webView?.navigationDelegate = nil
        webView = nil
        HTMLToPDFRenderer.active = nil
        cb?(data)
    }
}

extension AnalyticsDashboardViewController {

    /// IPC generate_html_report → рендер PDF → запись во временный файл → Preview.
    @objc func onExportPDF() {
        statusLabel.stringValue = "Генерируем PDF…"
        let client = ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            do {
                let response = try client.call(method: "generate_html_report", params: [:])
                let result = (response["result"] as? [String: Any]) ?? [:]
                let ok = (result["ok"] as? Bool) ?? false
                let html = (result["html"] as? String) ?? ""
                guard ok, !html.isEmpty else {
                    let privacy = (result["reason"] as? String) == "privacy_mode_active"
                    DispatchQueue.main.async {
                        self?.statusLabel.stringValue = privacy
                            ? "PDF недоступен в режиме приватности"
                            : "Нет данных для PDF"
                    }
                    return
                }
                DispatchQueue.main.async {
                    self?.renderAndOpenPDF(html: html)
                }
            } catch {
                DispatchQueue.main.async {
                    self?.statusLabel.stringValue = "Ошибка PDF: \(error.localizedDescription)"
                }
            }
        }
    }

    @MainActor
    private func renderAndOpenPDF(html: String) {
        statusLabel.stringValue = "Рендерим PDF…"
        let renderer = HTMLToPDFRenderer()
        renderer.render(html: html) { [weak self] data in
            guard let self = self else { return }
            guard let data = data, !data.isEmpty else {
                self.statusLabel.stringValue = "Не удалось создать PDF"
                return
            }
            let stamp = Self.pdfTimestamp()
            let url = FileManager.default.temporaryDirectory
                .appendingPathComponent("KrabEar_Report_\(stamp).pdf")
            do {
                try data.write(to: url, options: .atomic)
                self.statusLabel.stringValue = "PDF готов"
                NSWorkspace.shared.open(url)
            } catch {
                self.statusLabel.stringValue = "Не удалось сохранить PDF: \(error.localizedDescription)"
            }
        }
    }

    /// Метка времени для имени файла (без двоеточий — недопустимы в путях).
    private static func pdfTimestamp() -> String {
        let fmt = DateFormatter()
        fmt.locale = Locale(identifier: "en_US_POSIX")
        fmt.dateFormat = "yyyyMMdd_HHmmss"
        return fmt.string(from: Date())
    }
}
