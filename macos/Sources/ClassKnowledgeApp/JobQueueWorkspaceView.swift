import SwiftUI

private enum JobQueueBucket: String, CaseIterable, Identifiable {
    case active = "Active"
    case queued = "Queued"
    case later = "Later"
    case history = "History"
    var id: String { rawValue }

    func contains(_ status: String) -> Bool {
        switch self {
        case .active: ["running", "waiting"].contains(status)
        case .queued: status == "queued"
        case .later: status == "deferred"
        case .history: ["completed", "failed", "cancelled"].contains(status)
        }
    }
}

struct JobQueueWorkspaceView: View {
    @EnvironmentObject private var model: AppModel
    @State private var bucket: JobQueueBucket = .active
    @State private var autoUnload = true
    @State private var loadedModels: [RunningModel] = []
    @State private var modelStatus: String?
    @State private var isLoadingModels = false

    private var jobs: [JobSummary] {
        (model.snapshot?.jobs ?? []).filter { bucket.contains($0.status) }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            header
            if let counts = model.snapshot?.counts {
                HStack(spacing: 12) {
                    QueueMetric(title: "Queued", count: counts.queued, symbol: "clock")
                    QueueMetric(title: "Active", count: counts.active, symbol: "bolt.fill")
                    QueueMetric(title: "Later", count: counts.deferred, symbol: "pause.circle")
                    QueueMetric(title: "Completed", count: counts.completed, symbol: "checkmark.circle")
                    QueueMetric(title: "Failed", count: counts.failed, symbol: "exclamationmark.circle")
                }
            }
            OllamaControls(
                autoUnload: $autoUnload,
                loadedModels: $loadedModels,
                status: $modelStatus,
                isLoading: $isLoadingModels,
                refresh: refreshModels,
                setAutoUnload: setAutoUnload,
                unload: unloadModels
            )
            if model.snapshot?.agentActive == true {
                HStack {
                    Label(
                        model.snapshot?.ollamaAgentActive == true ? "Local agent is reserving Ollama" : "Codex agent is active",
                        systemImage: "sparkles"
                    )
                    .foregroundStyle(.secondary)
                    Spacer()
                    Button("Deactivate agent") { Task { await model.setAgentActive(false) } }
                }
                .padding(.horizontal, 4)
            }
            Picker("Job status", selection: $bucket) {
                ForEach(JobQueueBucket.allCases) { option in
                    Text("\(option.rawValue) (\(count(for: option)))").tag(option)
                }
            }
            .pickerStyle(.segmented)
            List(jobs) { job in
                NativeJobRow(job: job)
                    .listRowSeparator(.hidden)
            }
            .listStyle(.inset)
            .overlay {
                if jobs.isEmpty {
                    ContentUnavailableView(
                        emptyTitle,
                        systemImage: bucket == .history ? "checkmark.circle" : "tray",
                        description: Text(emptyDescription)
                    )
                }
            }
        }
        .padding(24)
        .task {
            do {
                let value: ManagerSettings = try await model.backend.call("jobs.settings")
                autoUnload = value.autoUnload
            } catch {
                modelStatus = error.localizedDescription
            }
            await refreshModels()
        }
    }

    private var header: some View {
        HStack {
            VStack(alignment: .leading, spacing: 4) {
                Text("Job Queue")
                    .font(.largeTitle.weight(.semibold))
                Text("Tasks run by priority and save checkpoints for safe recovery.")
                    .foregroundStyle(.secondary)
            }
            Spacer()
            Button {
                Task { await model.refresh() }
            } label: {
                Label("Refresh", systemImage: "arrow.clockwise")
            }
        }
    }

    private var emptyTitle: String {
        switch bucket {
        case .active: "No active tasks"
        case .queued: "Queue is clear"
        case .later: "Nothing saved for later"
        case .history: "No task history yet"
        }
    }

    private var emptyDescription: String {
        switch bucket {
        case .active: "Tasks appear here when local processing begins."
        case .queued: "Lecture, translation, and slide review tasks will be listed here."
        case .later: "Tasks stopped safely for later will keep their saved checkpoints."
        case .history: "Completed, failed, and cancelled work will appear here."
        }
    }

    private func count(for bucket: JobQueueBucket) -> Int {
        (model.snapshot?.jobs ?? []).filter { bucket.contains($0.status) }.count
    }

    private func refreshModels() async {
        isLoadingModels = true
        defer { isLoadingModels = false }
        do {
            loadedModels = try await model.loadedModels()
            modelStatus = loadedModels.isEmpty ? "No models are currently loaded." : nil
        } catch {
            modelStatus = error.localizedDescription
        }
    }

    private func setAutoUnload(_ enabled: Bool) async {
        do {
            let _: ManagerSettings = try await model.backend.call("jobs.settings", params: ["auto_unload": enabled])
            autoUnload = enabled
        } catch {
            modelStatus = error.localizedDescription
            autoUnload.toggle()
        }
    }

    private func unloadModels(_ names: [String]? = nil) async {
        do {
            let report = try await model.unloadModels(names)
            if report.failures.isEmpty {
                modelStatus = report.stopped.isEmpty ? "No idle models needed unloading." : "Unloaded: \(report.stopped.joined(separator: ", "))"
            } else {
                modelStatus = report.failures.map { "\($0.key): \($0.value)" }.joined(separator: "\n")
            }
            await refreshModels()
        } catch {
            modelStatus = error.localizedDescription
        }
    }
}

private struct NativeJobRow: View {
    @EnvironmentObject private var model: AppModel
    let job: JobSummary
    @State private var isShowingEvents = false
    @State private var events: [JobEvent] = []
    @State private var isLoadingEvents = false
    @State private var presentedSheet: NativeJobRowSheet?
    @State private var isLoadingResult = false

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .top, spacing: 12) {
                Image(systemName: symbol)
                    .font(.title3)
                    .foregroundStyle(statusColor)
                    .frame(width: 25)
                    .padding(.top, 2)
                VStack(alignment: .leading, spacing: 5) {
                    HStack {
                        Text(job.title).font(.headline)
                        Spacer()
                        Text(job.status.capitalized)
                            .font(.caption.weight(.semibold))
                            .padding(.horizontal, 8)
                            .padding(.vertical, 4)
                            .background(statusColor.opacity(0.12), in: Capsule())
                    }
                    Text(job.message)
                        .font(.callout)
                        .foregroundStyle(.secondary)
                    if job.status == "running" || job.status == "waiting" {
                        ProgressView(value: Double(job.progress), total: 100) {
                            Text("\(job.progress)% · \(job.stage.replacingOccurrences(of: "_", with: " "))")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                    }
                    if job.status == "failed", !job.error.isEmpty {
                        Text(job.error)
                            .font(.caption)
                            .foregroundStyle(.red)
                            .textSelection(.enabled)
                    }
                }
            }
            HStack(spacing: 8) {
                actionButtons
                Spacer()
                if job.status == "completed" {
                    if job.kind == "lecture" {
                        Button("Translate notes…") { presentedSheet = .translation(job) }
                            .buttonStyle(.borderless)
                    }
                    Button {
                        Task { await loadResult() }
                    } label: {
                        if isLoadingResult { ProgressView().controlSize(.small) }
                        else { Label("View results", systemImage: "doc.text.magnifyingglass") }
                    }
                    .buttonStyle(.borderless)
                    .disabled(isLoadingResult)
                }
                Button {
                    isShowingEvents.toggle()
                    if isShowingEvents && events.isEmpty { Task { await loadEvents() } }
                } label: {
                    Label("Processing log", systemImage: "text.alignleft")
                }
                .buttonStyle(.borderless)
            }
            if isShowingEvents {
                if isLoadingEvents {
                    ProgressView().controlSize(.small)
                } else if events.isEmpty {
                    Text("No processing events recorded yet.").font(.caption).foregroundStyle(.secondary)
                } else {
                    VStack(alignment: .leading, spacing: 7) {
                        ForEach(events) { event in
                            HStack(alignment: .top, spacing: 8) {
                                Text(event.stage.isEmpty ? event.level.uppercased() : event.stage.replacingOccurrences(of: "_", with: " ").capitalized)
                                    .font(.caption.weight(.semibold))
                                    .foregroundStyle(.secondary)
                                    .frame(width: 110, alignment: .leading)
                                Text(event.message)
                                    .font(.caption)
                                    .textSelection(.enabled)
                            }
                        }
                    }
                    .padding(11)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(.quaternary.opacity(0.45), in: RoundedRectangle(cornerRadius: 8))
                }
            }
        }
        .padding(13)
        .background(Color(nsColor: .controlBackgroundColor), in: RoundedRectangle(cornerRadius: 11))
        .padding(.vertical, 4)
        .sheet(item: $presentedSheet) { sheet in
            switch sheet {
            case .result(let detail):
                NativeJobResultSheet(detail: detail)
            case .translation(let job):
                FullLectureTranslationSheet { language in
                    Task { await model.enqueueLectureTranslation(sourceJobID: job.id, targetLanguage: language) }
                }
            }
        }
    }

    @ViewBuilder private var actionButtons: some View {
        switch job.status {
        case "queued":
            Button("Move to Later") { Task { await model.performJobAction(job.id, action: "defer") } }
            Menu("Priority") {
                priorityButton("High", value: 10)
                priorityButton("Normal", value: 50)
                priorityButton("Low", value: 90)
            }
            Button("Cancel", role: .destructive) { Task { await model.performJobAction(job.id, action: "cancel") } }
        case "running", "waiting":
            Button("Stop safely · do later") { Task { await model.performJobAction(job.id, action: "defer") } }
            Button("Cancel task", role: .destructive) { Task { await model.performJobAction(job.id, action: "cancel") } }
        case "deferred":
            Button("Resume from checkpoints") { Task { await model.performJobAction(job.id, action: "resume") } }
            Button("Cancel", role: .destructive) { Task { await model.performJobAction(job.id, action: "cancel") } }
        case "failed":
            Button("Retry with saved inputs") { Task { await model.performJobAction(job.id, action: "retry") } }
        default:
            EmptyView()
        }
    }

    private var symbol: String {
        switch job.status {
        case "running", "waiting": "gearshape.2.fill"
        case "queued": "clock"
        case "deferred": "pause.circle"
        case "completed": "checkmark.circle.fill"
        case "failed": "exclamationmark.triangle.fill"
        default: "xmark.circle"
        }
    }

    private var statusColor: Color {
        switch job.status {
        case "running", "waiting": .blue
        case "queued": .orange
        case "deferred": .secondary
        case "completed": .green
        case "failed": .red
        default: .secondary
        }
    }

    private func priorityButton(_ label: String, value: Int) -> some View {
        Button(label) { Task { await model.performJobAction(job.id, action: "priority", priority: value) } }
    }

    private func loadEvents() async {
        isLoadingEvents = true
        defer { isLoadingEvents = false }
        do { events = try await model.jobEvents(job.id) }
        catch { model.errorMessage = error.localizedDescription }
    }

    private func loadResult() async {
        isLoadingResult = true
        defer { isLoadingResult = false }
        do {
            let detail: NativeJobDetail = try await model.backend.call("jobs.detail", params: ["job_id": job.id])
            presentedSheet = .result(detail)
        } catch {
            model.errorMessage = error.localizedDescription
        }
    }
}

private enum NativeJobRowSheet: Identifiable {
    case result(NativeJobDetail)
    case translation(JobSummary)

    var id: String {
        switch self {
        case .result(let detail): "result-\(detail.id)"
        case .translation(let job): "translation-\(job.id)"
        }
    }
}

private struct OllamaControls: View {
    @EnvironmentObject private var model: AppModel
    @Binding var autoUnload: Bool
    @Binding var loadedModels: [RunningModel]
    @Binding var status: String?
    @Binding var isLoading: Bool
    let refresh: () async -> Void
    let setAutoUnload: (Bool) async -> Void
    let unload: ([String]?) async -> Void

    var body: some View {
        DisclosureGroup {
            VStack(alignment: .leading, spacing: 12) {
                Toggle("Automatically unload idle models", isOn: Binding(
                    get: { autoUnload },
                    set: { value in Task { await setAutoUnload(value) } }
                ))
                HStack {
                    Text("Ollama model memory")
                        .font(.subheadline.weight(.semibold))
                    Spacer()
                    Button("Refresh") { Task { await refresh() } }
                    Button("Unload all idle models") { Task { await unload(nil) } }
                        .disabled(loadedModels.isEmpty || isLoading)
                }
                if let status { Text(status).font(.caption).foregroundStyle(.secondary).textSelection(.enabled) }
                if loadedModels.isEmpty {
                    Text("No models are loaded, or Ollama is unavailable.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                } else {
                    ForEach(loadedModels) { modelInfo in
                        HStack {
                            Text(modelInfo.name).font(.callout)
                            Text(ByteCountFormatter.string(fromByteCount: Int64(modelInfo.sizeBytes), countStyle: .memory))
                                .font(.caption)
                                .foregroundStyle(.secondary)
                            Spacer()
                            Button("Unload") { Task { await unload([modelInfo.name]) } }
                                .disabled(isLoading)
                        }
                    }
                }
            }
            .padding(.top, 10)
        } label: {
            Label("Ollama model memory", systemImage: "memorychip")
                .font(.headline)
        }
        .padding(14)
        .background(Color(nsColor: .controlBackgroundColor), in: RoundedRectangle(cornerRadius: 10))
    }
}

private struct QueueMetric: View {
    let title: String
    let count: Int
    let symbol: String

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Label(title, systemImage: symbol)
                .font(.caption)
                .foregroundStyle(.secondary)
            Text("\(count)")
                .font(.title2.weight(.semibold))
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(12)
        .background(.quaternary.opacity(0.45), in: RoundedRectangle(cornerRadius: 10))
    }
}
