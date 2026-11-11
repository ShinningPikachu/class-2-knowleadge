import AppKit
import ServiceManagement
import SwiftUI
import UniformTypeIdentifiers

struct SettingsWorkspaceView: View {
    @EnvironmentObject private var model: AppModel
    @AppStorage("classKnowledgeUILanguage") private var interfaceLanguage = "en"
    @State private var launchAtLogin = false
    @State private var savedNotice: String?
    @State private var launchStatus: String?
    @State private var isSaving = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 22) {
                VStack(alignment: .leading, spacing: 5) {
                    Text("Settings")
                        .font(.largeTitle.weight(.semibold))
                    Text("Choose where local data lives and configure the on-device models.")
                        .foregroundStyle(.secondary)
                }

                GroupBox("Language") {
                    HStack {
                        Text("Interface language")
                        Spacer()
                        Picker("Interface language", selection: $interfaceLanguage) {
                            Text("English").tag("en")
                            Text("简体中文").tag("zh-Hans")
                        }
                        .labelsHidden()
                        .frame(width: 180)
                    }
                    .padding(.top, 8)
                }

                GroupBox("Workspace") {
                    VStack(alignment: .leading, spacing: 12) {
                        Text(model.projectRoot?.path ?? "No workspace connected")
                            .font(.system(.callout, design: .monospaced))
                            .textSelection(.enabled)
                            .lineLimit(2)
                            .truncationMode(.middle)
                        HStack {
                            Text("The existing library, jobs, and lecture outputs remain in this folder.")
                                .font(.callout)
                                .foregroundStyle(.secondary)
                            Spacer()
                            Button("Choose Workspace…", action: chooseWorkspace)
                        }
                    }
                    .padding(.top, 8)
                }

                GroupBox("Local models") {
                    VStack(alignment: .leading, spacing: 14) {
                        LabeledContent("Ollama URL") {
                            TextField("http://127.0.0.1:11434", text: $model.settings.ollamaHost)
                                .textFieldStyle(.roundedBorder)
                                .frame(maxWidth: 360)
                        }
                        LabeledContent("Assistant model") {
                            TextField("qwen3.5:27b", text: $model.settings.llmModel)
                                .textFieldStyle(.roundedBorder)
                                .frame(maxWidth: 360)
                        }
                        LabeledContent("Generation temperature") {
                            Stepper(model.settings.llmTemperature.formatted(.number.precision(.fractionLength(2))), value: $model.settings.llmTemperature, in: 0...1, step: 0.05)
                                .frame(width: 200)
                        }
                        LabeledContent("Embedding model") {
                            TextField("nomic-embed-text", text: $model.settings.embeddingModel)
                                .textFieldStyle(.roundedBorder)
                                .frame(maxWidth: 360)
                        }
                        LabeledContent("Whisper model or local path") {
                            TextField("large-v3", text: $model.settings.whisperModel)
                                .textFieldStyle(.roundedBorder)
                                .frame(maxWidth: 360)
                        }
                        LabeledContent("Transcription device") {
                            Picker("Transcription device", selection: $model.settings.whisperDevice) {
                                Text("Automatic").tag("auto")
                                Text("CPU").tag("cpu")
                                Text("CUDA").tag("cuda")
                            }
                            .labelsHidden()
                            .frame(width: 120)
                        }
                        LabeledContent("Whisper precision") {
                            Picker("Whisper precision", selection: $model.settings.whisperComputeType) {
                                Text("Float32").tag("float32")
                                Text("Int8").tag("int8")
                                Text("Float16").tag("float16")
                            }
                            .labelsHidden()
                            .frame(width: 120)
                        }
                        LabeledContent("Spoken language") {
                            TextField("en", text: Binding(
                                get: { model.settings.language ?? "" },
                                set: { model.settings.language = $0.isEmpty ? nil : $0 }
                            ))
                            .textFieldStyle(.roundedBorder)
                            .frame(maxWidth: 360)
                        }
                        HStack {
                            Text("Transcription workers")
                            Spacer()
                            Picker("Transcription workers", selection: $model.settings.whisperParallelWorkers) {
                                Text("1").tag(1)
                                Text("2").tag(2)
                            }
                            .labelsHidden()
                            .frame(width: 100)
                        }
                        LabeledContent("Ollama keep-alive") {
                            TextField("45m", text: $model.settings.ollamaKeepAlive)
                                .textFieldStyle(.roundedBorder)
                                .frame(maxWidth: 160)
                        }
                        LabeledContent("Lecture reasoning") {
                            Picker("Lecture reasoning", selection: $model.settings.ollamaThinking) {
                                Text("Low").tag("low")
                                Text("Medium").tag("medium")
                                Text("High").tag("high")
                            }
                            .labelsHidden()
                            .frame(width: 120)
                        }
                    }
                    .padding(.top, 8)
                }

                GroupBox("Lecture processing") {
                    VStack(alignment: .leading, spacing: 14) {
                        HStack {
                            Text("Recording chunk length")
                            Spacer()
                            Picker("Chunk length", selection: $model.settings.mediaChunkSeconds) {
                                Text("10 minutes").tag(600)
                                Text("20 minutes").tag(1_200)
                                Text("30 minutes").tag(1_800)
                                Text("45 minutes").tag(2_700)
                            }
                            .labelsHidden()
                            .frame(width: 150)
                        }
                        HStack {
                            Text("Chunk overlap")
                            Spacer()
                            Picker("Chunk overlap", selection: $model.settings.mediaOverlapSeconds) {
                                ForEach([5, 10, 15, 20, 25, 30], id: \.self) { seconds in
                                    Text("\(seconds) seconds").tag(seconds)
                                }
                            }
                            .labelsHidden()
                            .frame(width: 150)
                        }
                        Toggle("Clean transcription batches in English", isOn: $model.settings.enableTranscriptCleanup)
                        Toggle("Use local Tesseract OCR for slide images", isOn: $model.settings.enableOcr)
                        Toggle("Run a quality review of generated notes", isOn: $model.settings.qualityReview)
                        LabeledContent("Note generation profile") {
                            Picker("Note generation profile", selection: $model.settings.noteGenerationProfile) {
                                Text("Fast").tag("fast")
                                Text("Deep").tag("deep")
                            }
                            .labelsHidden()
                            .frame(width: 120)
                        }
                        LabeledContent("Maximum note output") {
                            Stepper("\(model.settings.noteMaxOutputTokens) tokens", value: $model.settings.noteMaxOutputTokens, in: 512...8_192, step: 256)
                                .frame(width: 240)
                        }
                        LabeledContent("Transcript cleanup batch size") {
                            Stepper("\(model.settings.transcriptCleanupBatchChars) characters", value: $model.settings.transcriptCleanupBatchChars, in: 2_000...24_000, step: 1_000)
                                .frame(width: 240)
                        }
                        LabeledContent("Ollama context window") {
                            Picker("Context window", selection: $model.settings.ollamaNumCtx) {
                                ForEach([4_096, 8_192, 16_384, 32_768], id: \.self) { value in
                                    Text(value.formatted()).tag(value)
                                }
                            }
                            .labelsHidden()
                            .frame(width: 150)
                        }
                        LabeledContent("Maximum slide context") {
                            Stepper("\(model.settings.maxSlideContextChars) characters", value: $model.settings.maxSlideContextChars, in: 2_000...50_000, step: 1_000)
                                .frame(width: 240)
                        }
                        LabeledContent("Alignment confidence threshold") {
                            Slider(value: $model.settings.semanticAlignmentThreshold, in: 0...0.8, step: 0.05) {
                                Text("Alignment confidence threshold")
                            } minimumValueLabel: {
                                Text("0")
                            } maximumValueLabel: {
                                Text(model.settings.semanticAlignmentThreshold.formatted(.number.precision(.fractionLength(2))))
                                    .monospacedDigit()
                            }
                            .frame(maxWidth: 280)
                        }
                        LabeledContent("Text chunk size") {
                            Stepper("\(model.settings.chunkSize) characters", value: $model.settings.chunkSize, in: 200...3_000, step: 100)
                                .frame(width: 240)
                        }
                        LabeledContent("Text chunk overlap") {
                            Stepper("\(model.settings.chunkOverlap) characters", value: $model.settings.chunkOverlap, in: 0...min(1_000, max(0, model.settings.chunkSize - 1)), step: 20)
                                .frame(width: 240)
                        }
                    }
                    .padding(.top, 8)
                }

                GroupBox("App startup") {
                    VStack(alignment: .leading, spacing: 9) {
                        Toggle("Open Class Knowledge when I sign in", isOn: Binding(
                            get: { launchAtLogin },
                            set: { updateLoginItem($0) }
                        ))
                        Text("macOS starts the app after you sign in. The local job queue only runs while the app is open.")
                            .font(.callout)
                            .foregroundStyle(.secondary)
                        if let launchStatus {
                            Text(LocalizedStringKey(launchStatus))
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                    }
                    .padding(.top, 8)
                }

                HStack {
                    if let savedNotice {
                        Label(LocalizedStringKey(savedNotice), systemImage: "checkmark.circle.fill")
                            .font(.callout)
                            .foregroundStyle(.green)
                    }
                    Spacer()
                    Button {
                        Task { await save() }
                    } label: {
                        if isSaving { ProgressView().controlSize(.small) }
                        else { Text("Save Settings") }
                    }
                    .buttonStyle(.borderedProminent)
                    .disabled(isSaving || !model.isConnected)
                }
                Spacer(minLength: 30)
            }
            .padding(26)
            .frame(maxWidth: 850, alignment: .leading)
            .frame(maxWidth: .infinity)
        }
        .task {
            refreshLoginItemStatus()
        }
    }

    private func save() async {
        isSaving = true
        defer { isSaving = false }
        do {
            try await model.saveSettings()
            savedNotice = "Settings saved to this workspace."
        } catch {
            model.errorMessage = error.localizedDescription
        }
    }

    private func chooseWorkspace() {
        let panel = NSOpenPanel()
        panel.title = "Choose a Class Knowledge workspace"
        panel.prompt = "Open Workspace"
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = false
        panel.allowedContentTypes = [.folder]
        guard panel.runModal() == .OK, let url = panel.url else { return }
        Task { await model.switchWorkspace(to: url) }
    }

    private func updateLoginItem(_ enabled: Bool) {
        do {
            if enabled { try SMAppService.mainApp.register() }
            else { try SMAppService.mainApp.unregister() }
            refreshLoginItemStatus()
        } catch {
            refreshLoginItemStatus(error: error)
        }
    }

    private func refreshLoginItemStatus(error: Error? = nil) {
        switch SMAppService.mainApp.status {
        case .enabled:
            launchAtLogin = true
            launchStatus = error.map { "Login item is enabled, but macOS reported: \($0.localizedDescription)" }
                ?? "Login item enabled. It opens after you sign in."
        case .notRegistered:
            launchAtLogin = false
            launchStatus = error.map { "Could not update the login item: \($0.localizedDescription)" }
                ?? "Login item disabled."
        case .requiresApproval:
            launchAtLogin = false
            launchStatus = "macOS needs approval. Allow Class Knowledge in System Settings → General → Login Items."
        case .notFound:
            launchAtLogin = false
            launchStatus = "macOS could not locate this app. Keep it in Applications, then try again."
        @unknown default:
            launchAtLogin = false
            launchStatus = error.map { "Could not update the login item: \($0.localizedDescription)" }
                ?? "macOS returned an unknown login item status."
        }
    }
}
