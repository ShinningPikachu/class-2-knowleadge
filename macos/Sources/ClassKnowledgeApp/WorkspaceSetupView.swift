import AppKit
import SwiftUI
import UniformTypeIdentifiers

struct WorkspaceSetupView: View {
    @EnvironmentObject private var model: AppModel
    @State private var isChoosing = false

    var body: some View {
        VStack(spacing: 18) {
            Image(systemName: "books.vertical")
                .font(.system(size: 42, weight: .regular))
                .foregroundStyle(.tint)
            Text("Choose your Class Knowledge workspace")
                .font(.title2.weight(.semibold))
                .multilineTextAlignment(.center)
            Text("Select the project folder that contains app.py, src/, and its .venv. Your existing Library, queue, and lecture files will stay there.")
                .multilineTextAlignment(.center)
                .foregroundStyle(.secondary)
                .frame(maxWidth: 520)
            if let message = model.workspaceSetupMessage {
            Text(LocalizedStringKey(message))
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
                    .textSelection(.enabled)
                    .frame(maxWidth: 520)
            }
            Button {
                chooseWorkspace()
            } label: {
                if isChoosing {
                    ProgressView().controlSize(.small)
                } else {
                    Label("Choose Workspace…", systemImage: "folder.badge.plus")
                }
            }
            .buttonStyle(.borderedProminent)
            .disabled(isChoosing)
        }
        .padding(40)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color(nsColor: .windowBackgroundColor))
    }

    private func chooseWorkspace() {
        let panel = NSOpenPanel()
        panel.title = "Choose a Class Knowledge project folder"
        panel.prompt = "Open Workspace"
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = false
        panel.allowedContentTypes = [.folder]
        guard panel.runModal() == .OK, let url = panel.url else { return }
        isChoosing = true
        Task {
            await model.switchWorkspace(to: url)
            isChoosing = false
        }
    }
}
