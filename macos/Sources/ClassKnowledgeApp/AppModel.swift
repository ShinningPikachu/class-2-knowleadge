import AppKit
import Combine
import Foundation

@MainActor
final class AppModel: ObservableObject {
    @Published var selectedArea: WorkspaceArea = .library
    @Published var snapshot: WorkspaceSnapshot?
    @Published var documents: [LibraryDocument] = []
    @Published var folders: [LibraryFolder] = []
    @Published var selectedSubjectID: String?
    @Published var selectedDocumentID: String?
    @Published var previewedDocument: LibraryDocument?
    @Published var lectureBundle = NativeLectureBundle.empty
    @Published var selectedFolderID: String?
    @Published var searchQuery = ""
    @Published var searchResults: [SearchResult] = []
    @Published var isConnected = false
    @Published var isBusy = false
    @Published var statusMessage = "Starting local services…"
    @Published var needsWorkspaceSetup = false
    @Published var workspaceSetupMessage: String?
    @Published var errorMessage: String?
    @Published var importErrors: [ImportError] = []
    @Published var settings = NativeSettings()
    @Published var sharingStatus: SharingStatus?
    @Published var isPreparingSharing = false
    @Published var agentMessages: [AgentMessage] = []
    @Published var pendingAgentAction: PendingAgentAction?
    @Published var isAgentResponding = false

    let backend = BackendClient()
    private var didStart = false
    private let workspaceDefaultsKey = "classKnowledgeWorkspaceRoot"

    init() {
        if let saved = UserDefaults.standard.string(forKey: workspaceDefaultsKey) {
            selectedWorkspacePath = saved
        }
    }

    @Published var selectedWorkspacePath: String?

    var projectRoot: URL? { backend.projectRoot }
    var selectedSubject: LibrarySubject? {
        snapshot?.subjects.first(where: { $0.id == selectedSubjectID })
    }
    var selectedDocument: LibraryDocument? {
        if let previewedDocument, previewedDocument.id == selectedDocumentID { return previewedDocument }
        return documents.first(where: { $0.id == selectedDocumentID })
    }

    func start() async {
        guard !didStart else { return }
        didStart = true
        do {
            let root = try locateProjectRoot()
            try await connect(to: root)
        } catch {
            workspaceSetupMessage = error.localizedDescription
            needsWorkspaceSetup = true
            statusMessage = "Choose a workspace"
            didStart = false
        }
    }

    func switchWorkspace(to root: URL) async {
        let normalized = root.standardizedFileURL
        guard FileManager.default.fileExists(atPath: normalized.appending(path: "src/config.py").path),
              FileManager.default.fileExists(atPath: normalized.appending(path: "app.py").path) else {
            workspaceSetupMessage = NativeBackendError.workspaceNotFound.localizedDescription
            needsWorkspaceSetup = true
            return
        }
        await backend.stop()
        didStart = false
        isConnected = false
        snapshot = nil
        documents = []
        folders = []
        selectedSubjectID = nil
        selectedDocumentID = nil
        previewedDocument = nil
        do {
            try await connect(to: normalized)
            selectedWorkspacePath = normalized.path
            UserDefaults.standard.set(normalized.path, forKey: workspaceDefaultsKey)
        } catch {
            didStart = false
            workspaceSetupMessage = error.localizedDescription
            needsWorkspaceSetup = true
            statusMessage = "Workspace setup needed"
        }
    }

    func refresh() async {
        guard isConnected else { return }
        do {
            snapshot = try await backend.call("app.snapshot", as: WorkspaceSnapshot.self)
            if let selectedSubjectID,
               !snapshot!.subjects.contains(where: { $0.id == selectedSubjectID }) {
                self.selectedSubjectID = snapshot?.subjects.first?.id
            }
            await loadLibrary()
        } catch {
            statusMessage = error.localizedDescription
        }
    }

    func refreshSharing() async {
        guard isConnected else { return }
        do {
            sharingStatus = try await backend.call("sharing.status", as: SharingStatus.self)
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func prepareSharing() async {
        guard isConnected else { return }
        isPreparingSharing = true
        defer { isPreparingSharing = false }
        do {
            sharingStatus = try await backend.call("sharing.prepare", as: SharingStatus.self)
            statusMessage = "Share index refreshed"
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func loadLibrary() async {
        guard let selectedSubjectID else {
            documents = []
            folders = []
            return
        }
        do {
            async let docs: [LibraryDocument] = backend.call(
                "library.documents", params: ["subject_id": selectedSubjectID]
            )
            async let items: [LibraryFolder] = backend.call(
                "library.folders", params: ["subject_id": selectedSubjectID]
            )
            documents = try await docs
            folders = try await items
            if let selectedDocumentID, !documents.contains(where: { $0.id == selectedDocumentID }) {
                self.selectedDocumentID = nil
                previewedDocument = nil
            }
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func createSubject(name: String, description: String = "") async throws {
        isBusy = true
        defer { isBusy = false }
        let _: LibrarySubject = try await backend.call(
            "library.create_subject", params: ["name": name, "description": description]
        )
        await refresh()
        selectedSubjectID = snapshot?.subjects.first(where: { $0.name == name })?.id
        await loadLibrary()
    }

    func importFiles(_ urls: [URL]) async {
        guard let selectedSubjectID, !urls.isEmpty else { return }
        importErrors = []
        isBusy = true
        defer { isBusy = false }
        do {
            let result: LibraryImportResult = try await backend.call(
                "library.import",
                params: [
                    "subject_id": selectedSubjectID,
                    "folder_id": selectedFolderID as Any? ?? NSNull(),
                    "paths": urls.map(\.path),
                ]
            )
            importErrors = result.errors
            await refresh()
            selectedDocumentID = result.documents.first?.id
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func deleteDocument(_ document: LibraryDocument) async {
        do {
            let _: LibraryDocument = try await backend.call(
                "library.delete_document", params: ["document_id": document.id]
            )
            selectedDocumentID = nil
            await refresh()
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func openDocument(_ documentID: String) async {
        do {
            let document: LibraryDocument = try await backend.call(
                "library.document", params: ["document_id": documentID]
            )
            previewedDocument = document
            selectedDocumentID = document.id
            await loadLectureBundle(documentID)
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func selectDocument(_ document: LibraryDocument) {
        selectedDocumentID = document.id
        previewedDocument = document
        Task { await loadLectureBundle(document.id) }
    }

    func loadLectureBundle(_ documentID: String) async {
        do {
            lectureBundle = try await backend.call(
                "library.lecture_bundle", params: ["document_id": documentID]
            )
        } catch {
            lectureBundle = .empty
            errorMessage = error.localizedDescription
        }
    }

    func synthesizeVoice(_ text: String, maximumCharacters: Int = 8_000) async throws -> URL {
        let options: VoiceOptions = try await backend.call("voice.options")
        let voice = options.voices.first ?? "en_US-lessac-medium"
        let audio: VoiceAudio = try await backend.call(
            "voice.synthesize",
            params: ["text": text, "maximum_chars": maximumCharacters, "voice": voice]
        )
        guard let data = Data(base64Encoded: audio.audioBase64) else {
            throw NativeBackendError.malformedResponse
        }
        let directory = FileManager.default.temporaryDirectory.appending(path: "class-knowledge-audio", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let output = directory.appending(path: "narration-\(UUID().uuidString).wav")
        try data.write(to: output, options: .atomic)
        return output
    }

    func deleteSubject(_ subjectID: String) async {
        do {
            let _: LibrarySubject = try await backend.call(
                "library.delete_subject", params: ["subject_id": subjectID]
            )
            if selectedSubjectID == subjectID { selectedSubjectID = nil }
            await refresh()
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func createFolder(name: String) async {
        guard let selectedSubjectID else { return }
        do {
            let _: LibraryFolder = try await backend.call(
                "library.create_folder", params: ["subject_id": selectedSubjectID, "name": name]
            )
            await loadLibrary()
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func renameFolder(_ folderID: String, name: String) async {
        do {
            let _: LibraryFolder = try await backend.call(
                "library.rename_folder", params: ["folder_id": folderID, "name": name]
            )
            await loadLibrary()
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func deleteFolder(_ folderID: String, deleteDocuments: Bool = false) async {
        do {
            let _: LibraryFolder = try await backend.call(
                "library.delete_folder", params: ["folder_id": folderID, "delete_documents": deleteDocuments]
            )
            if selectedFolderID == folderID { selectedFolderID = nil }
            await loadLibrary()
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func renameDocument(_ documentID: String, name: String) async {
        do {
            let document: LibraryDocument = try await backend.call(
                "library.rename_document", params: ["document_id": documentID, "name": name]
            )
            previewedDocument = document
            await refresh()
            selectedDocumentID = document.id
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func moveDocument(_ documentID: String, toSubject subjectID: String) async {
        do {
            let document: LibraryDocument = try await backend.call(
                "library.move_document", params: ["document_id": documentID, "subject_id": subjectID]
            )
            await refresh()
            self.selectedSubjectID = document.subjectId
            await loadLibrary()
            previewedDocument = document
            selectedDocumentID = document.id
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func moveDocument(_ documentID: String, toFolder folderID: String?) async {
        do {
            let document: LibraryDocument = try await backend.call(
                "library.move_document_to_folder",
                params: ["document_id": documentID, "folder_id": folderID as Any? ?? NSNull()]
            )
            await loadLibrary()
            previewedDocument = document
            selectedDocumentID = document.id
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func enqueueSlideReview(_ slideNumber: Int) async {
        guard let sourceJob = lectureBundle.sourceJob, let document = selectedDocument else { return }
        do {
            let _: JobSummary = try await backend.call(
                "jobs.enqueue_slide_review",
                params: [
                    "source_job_id": sourceJob.id,
                    "slide_number": slideNumber,
                    "subject_id": document.subjectId,
                    "folder_id": document.folderId as Any? ?? NSNull(),
                    "lecture_name": lectureBundle.lectureName ?? "Lecture",
                    "priority": 50,
                ]
            )
            await refresh()
            await loadLectureBundle(document.id)
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func enqueueSlideTranslation(_ slide: NativeLectureSlide, targetLanguage: String) async {
        guard let sourceJob = lectureBundle.sourceJob else { return }
        do {
            let _: JobSummary = try await backend.call(
                "jobs.enqueue_translation",
                params: [
                    "source_job_id": sourceJob.id,
                    "target_language": targetLanguage,
                    "slide_number": slide.slide,
                    "explanation": slide.summary,
                    "slide_content": slide.content,
                    "priority": 50,
                ]
            )
            await refresh()
            if let selectedDocumentID { await loadLectureBundle(selectedDocumentID) }
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func enqueueLectureTranslation(sourceJobID: String, targetLanguage: String) async {
        let language = targetLanguage.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !language.isEmpty else { return }
        do {
            let _: JobSummary = try await backend.call(
                "jobs.enqueue_translation",
                params: [
                    "source_job_id": sourceJobID,
                    "target_language": language,
                    "priority": 50,
                ]
            )
            await refresh()
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func search() async {
        guard !searchQuery.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            searchResults = []
            return
        }
        do {
            searchResults = try await backend.call(
                "library.search",
                params: ["query": searchQuery, "subject_id": selectedSubjectID as Any? ?? NSNull(), "limit": 20]
            )
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func saveSettings() async throws {
        let _: NativeSettings = try await backend.call(
            "settings.update", params: ["config": settings.dictionary()]
        )
    }

    func setAgentActive(_ active: Bool) async {
        do {
            if active { try await saveSettings() }
            let _: AgentState = try await backend.call(
                "agent.state",
                params: ["active": active, "config": settings.dictionary()]
            )
            await refresh()
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func askAgent(_ prompt: String, scope: String) async {
        let cleaned = prompt.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !cleaned.isEmpty else { return }
        agentMessages.append(AgentMessage(role: "user", content: cleaned))
        pendingAgentAction = nil
        isAgentResponding = true
        defer { isAgentResponding = false }
        do {
            let reply: AgentReply = try await backend.call(
                "agent.ask",
                params: ["prompt": cleaned, "scope": scope, "config": settings.dictionary()]
            )
            agentMessages.append(AgentMessage(role: "assistant", content: reply.answer, sources: reply.sources))
            pendingAgentAction = reply.pendingAction
        } catch {
            agentMessages.append(AgentMessage(role: "assistant", content: "I could not complete that request: \(error.localizedDescription)"))
        }
    }

    func confirmAgentAction() async {
        guard let pendingAgentAction else { return }
        do {
            let encoder = JSONEncoder()
            encoder.keyEncodingStrategy = .convertToSnakeCase
            let data = try encoder.encode(pendingAgentAction)
            let action = try JSONSerialization.jsonObject(with: data)
            let confirmation: AgentConfirmation = try await backend.call(
                "agent.confirm_action", params: ["action": action]
            )
            agentMessages.append(AgentMessage(role: "assistant", content: confirmation.message))
            self.pendingAgentAction = nil
            await refresh()
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func cancelAgentAction() {
        pendingAgentAction = nil
    }

    func enqueueLecture(
        audioURL: URL?,
        presentationURL: URL?,
        title: String,
        subjectID: String?,
        priority: Int
    ) async {
        guard audioURL != nil || presentationURL != nil else {
            errorMessage = "Choose a recording, a slide deck, or both."
            return
        }
        isBusy = true
        defer { isBusy = false }
        do {
            try await saveSettings()
            let _: JobSummary = try await backend.call(
                "jobs.enqueue_lecture",
                params: [
                    "audio_path": audioURL?.path as Any? ?? NSNull(),
                    "presentation_path": presentationURL?.path as Any? ?? NSNull(),
                    "lecture_title": title.trimmingCharacters(in: .whitespacesAndNewlines),
                    "subject_id": subjectID as Any? ?? NSNull(),
                    "priority": priority,
                    "config": settings.dictionary(),
                ]
            )
            await refresh()
            selectedArea = .jobQueue
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func performJobAction(_ jobID: String, action: String, priority: Int? = nil) async {
        var params: [String: Any] = ["job_id": jobID, "action": action]
        if let priority { params["priority"] = priority }
        do {
            let _: JobSummary = try await backend.call("jobs.action", params: params)
            await refresh()
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func jobEvents(_ jobID: String) async throws -> [JobEvent] {
        try await backend.call("jobs.events", params: ["job_id": jobID])
    }

    func loadedModels() async throws -> [RunningModel] {
        try await backend.call("jobs.models", params: ["host": settings.ollamaHost])
    }

    func unloadModels(_ names: [String]? = nil) async throws -> ModelUnloadReport {
        var params: [String: Any] = ["host": settings.ollamaHost]
        if let names { params["models"] = names }
        return try await backend.call("jobs.unload_models", params: params)
    }

    func shutdown() async {
        await backend.stop()
        isConnected = false
    }

    private func locateProjectRoot() throws -> URL {
        let fileManager = FileManager.default
        var candidates: [URL] = []
        if let selectedWorkspacePath {
            candidates.append(URL(fileURLWithPath: selectedWorkspacePath, isDirectory: true))
        }
        if let configured = ProcessInfo.processInfo.environment["CLASS_KNOWLEDGE_PROJECT_ROOT"] {
            candidates.append(URL(fileURLWithPath: configured, isDirectory: true))
        }
        candidates.append(URL(fileURLWithPath: fileManager.currentDirectoryPath, isDirectory: true))
        candidates.append(Bundle.main.bundleURL.deletingLastPathComponent())

        for candidate in candidates {
            var cursor = candidate.standardizedFileURL
            for _ in 0..<8 {
                if fileManager.fileExists(atPath: cursor.appending(path: "app.py").path),
                   fileManager.fileExists(atPath: cursor.appending(path: "src/config.py").path) {
                    return cursor
                }
                let parent = cursor.deletingLastPathComponent()
                if parent.path == cursor.path { break }
                cursor = parent
            }
        }
        throw NativeBackendError.workspaceNotFound
    }

    private func connect(to root: URL) async throws {
        do {
            try backend.start(projectRoot: root)
            settings = try await backend.call("settings.get", as: NativeSettings.self)
            snapshot = try await backend.call("app.snapshot", as: WorkspaceSnapshot.self)
            let normalized = root.standardizedFileURL
            selectedWorkspacePath = normalized.path
            UserDefaults.standard.set(normalized.path, forKey: workspaceDefaultsKey)
            isConnected = true
            needsWorkspaceSetup = false
            workspaceSetupMessage = nil
            statusMessage = "Connected · \(normalized.path)"
            if selectedSubjectID == nil { selectedSubjectID = snapshot?.subjects.first?.id }
            await loadLibrary()
        } catch {
            await backend.stop()
            throw error
        }
    }
}
