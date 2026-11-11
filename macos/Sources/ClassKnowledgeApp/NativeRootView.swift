import SwiftUI

struct NativeRootView: View {
    @EnvironmentObject private var model: AppModel
    @AppStorage("classKnowledgeUILanguage") private var interfaceLanguage = "en"

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
            Text(LocalizedStringKey(model.errorMessage ?? ""))
        }
        .environment(\.locale, Locale(identifier: interfaceLanguage))
    }

    private var connectedContent: some View {
        VStack(spacing: 0) {
            NavigationSplitView {
                List(selection: selection) {
                    Section("Study tools") {
                        ForEach(WorkspaceArea.allCases) { area in
                            Label {
                                Text(LocalizedStringKey(area.rawValue))
                            } icon: {
                                Image(systemName: area.symbol)
                            }
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
                    case .sharing:
                        SharingWorkspaceView()
                    case .settings:
                        SettingsWorkspaceView()
                    }
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)

            Divider()
            HStack(spacing: 8) {
                Circle()
                    .fill(model.isConnected ? Color.green : Color.orange)
                    .frame(width: 7, height: 7)
                statusMessageView
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

    @ViewBuilder
    private var statusMessageView: some View {
        let message = model.statusMessage
        if let range = message.range(of: "Connected · ") {
            Text("Connected · \(String(message[range.upperBound...]))")
        } else {
            Text(LocalizedStringKey(message))
        }
    }
}
