import SwiftUI

struct NativeRootView: View {
    @EnvironmentObject private var model: AppModel

    private var selection: Binding<WorkspaceArea?> {
        Binding(
            get: { model.selectedArea },
            set: { if let newValue = $0 { model.selectedArea = newValue } }
        )
    }

    var body: some View {
        Group {
            if model.needsWorkspaceSetup {
                WorkspaceSetupView()
            } else {
                connectedContent
            }
        }
        .task {
            await model.start()
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(3))
                await model.refresh()
            }
        }
        .alert(
            "Class Knowledge",
            isPresented: Binding(
                get: { model.errorMessage != nil },
                set: { if !$0 { model.errorMessage = nil } }
            )
        ) {
            Button("OK", role: .cancel) { model.errorMessage = nil }
        } message: {
            Text(model.errorMessage ?? "")
        }
    }

    private var connectedContent: some View {
        NavigationSplitView {
            List(selection: selection) {
                Section("Study tools") {
                    ForEach(WorkspaceArea.allCases) { area in
                        Label(area.rawValue, systemImage: area.symbol)
                            .tag(area)
                    }
                }

                if let counts = model.snapshot?.counts {
                    Section("Queue") {
                        Label("\(counts.active) active", systemImage: "bolt.fill")
                        Label("\(counts.queued) queued", systemImage: "clock")
                        Label("\(counts.deferred) later", systemImage: "pause.circle")
                    }
                    .font(.callout)
                    .foregroundStyle(.secondary)
                }
            }
            .listStyle(.sidebar)
            .navigationSplitViewColumnWidth(min: 185, ideal: 205, max: 245)
            .navigationTitle("Class Knowledge")
        } detail: {
            Group {
                switch model.selectedArea {
                case .library:
                    LibraryWorkspaceView()
                case .agent:
                    AgentWorkspaceView()
                case .lectureNotes:
                    LectureWorkspaceView()
                case .jobQueue:
                    JobQueueWorkspaceView()
                case .settings:
                    SettingsWorkspaceView()
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        .safeAreaInset(edge: .bottom, spacing: 0) {
            HStack(spacing: 8) {
                Circle()
                    .fill(model.isConnected ? Color.green : Color.orange)
                    .frame(width: 7, height: 7)
                Text(model.statusMessage)
                    .lineLimit(1)
                Spacer()
                if model.isBusy { ProgressView().controlSize(.small) }
            }
            .font(.caption)
            .foregroundStyle(.secondary)
            .padding(.horizontal, 14)
            .padding(.vertical, 7)
            .background(.bar)
        }
    }
}
