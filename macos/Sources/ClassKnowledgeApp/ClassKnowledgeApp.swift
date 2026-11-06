import SwiftUI

@main
struct ClassKnowledgeApp: App {
    @StateObject private var model = AppModel()

    var body: some Scene {
        WindowGroup {
            NativeRootView()
                .environmentObject(model)
                .frame(minWidth: 980, minHeight: 650)
        }
        .defaultSize(width: 1280, height: 820)
        .commands {
            CommandGroup(replacing: .newItem) { }
            CommandMenu("Library") {
                Button("Refresh Library") {
                    Task { await model.refresh() }
                }
                .keyboardShortcut("r", modifiers: [.command])
                .disabled(!model.isConnected)
            }
        }
    }
}
