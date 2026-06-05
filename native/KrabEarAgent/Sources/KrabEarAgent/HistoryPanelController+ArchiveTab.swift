import AppKit

extension HistoryPanelController {
    
    // MARK: - Accessor
    
    /// Контроллер вкладки «Архив».
    var archiveVC: ArchiveTabViewController? {
        get { objc_getAssociatedObject(self, &HistoryPanelController.archiveVCKey) as? ArchiveTabViewController }
        set { objc_setAssociatedObject(self, &HistoryPanelController.archiveVCKey, newValue, .OBJC_ASSOCIATION_RETAIN_NONATOMIC) }
    }
    
    nonisolated(unsafe) static var archiveVCKey: UInt8 = 0
    
    // MARK: - Setup
    
    func setupArchiveTab(contentView archiveContentView: NSView) {
        let vc = ArchiveTabViewController()
        archiveVC = vc
        
        // Встроить view VC в content view таба
        vc.view.translatesAutoresizingMaskIntoConstraints = false
        archiveContentView.addSubview(vc.view)
        NSLayoutConstraint.activate([
            vc.view.topAnchor.constraint(equalTo: archiveContentView.topAnchor),
            vc.view.leadingAnchor.constraint(equalTo: archiveContentView.leadingAnchor),
            vc.view.trailingAnchor.constraint(equalTo: archiveContentView.trailingAnchor),
            vc.view.bottomAnchor.constraint(equalTo: archiveContentView.bottomAnchor),
        ])
        
        // Pass current privacy state
        vc.isPrivacyModeEnabled = settingsProvider().privacyModeEnabled
        
        // Trigger viewDidLoad
        _ = vc.view
    }
}
