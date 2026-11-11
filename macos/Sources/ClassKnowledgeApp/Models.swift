import Foundation

enum WorkspaceArea: String, CaseIterable, Identifiable {
    case library = "Library"
    case agent = "Agent"
    case lectureNotes = "Lecture Notes"
    case jobQueue = "Job Queue"
    case sharing = "Share"
    case settings = "Settings"

    var id: String { rawValue }

    var symbol: String {
        switch self {
        case .library: "books.vertical"
        case .agent: "sparkles"
        case .lectureNotes: "waveform.and.note"
        case .jobQueue: "tray.full"
        case .sharing: "square.and.arrow.up"
        case .settings: "gearshape"
        }
    }
}

struct LibrarySubject: Decodable, Identifiable, Hashable {
    let id: String
    let name: String
    let description: String
    let createdAt: String
}

struct LibraryFolder: Decodable, Identifiable, Hashable {
    let id: String
    let subjectId: String
    let name: String
    let createdAt: String
    let documentCount: Int
}

struct LibraryDocument: Decodable, Identifiable, Hashable {
    let id: String
    let subjectId: String
    let subjectName: String
    let originalName: String
    let storedPath: String
    let mediaType: String
    let sizeBytes: Int
    let sha256: String
    let status: String
    let extractionError: String
    let createdAt: String
    let folderId: String?
    let folderName: String

    var fileURL: URL { URL(fileURLWithPath: storedPath) }

    var displaySize: String {
        ByteCountFormatter.string(fromByteCount: Int64(sizeBytes), countStyle: .file)
    }
}

struct JobSummary: Decodable, Identifiable, Hashable {
    let id: String
    let kind: String
    let title: String
    let status: String
    let priority: Int
    let priorityLabel: String
    let stage: String
    let progress: Int
    let message: String
    let error: String
    let createdAt: String
    let updatedAt: String
    let completedAt: String
}

struct JobCounts: Decodable {
    let queued: Int
    let running: Int
    let waiting: Int
    let deferred: Int
    let completed: Int
    let failed: Int
    let cancelled: Int

    var active: Int { running + waiting }
}

struct WorkspaceSnapshot: Decodable {
    let projectRoot: String
    let subjects: [LibrarySubject]
    let jobs: [JobSummary]
    let counts: JobCounts
    let agentActive: Bool
    let ollamaAgentActive: Bool
}

struct SharingFile: Decodable, Identifiable {
    let path: String
    let sizeBytes: Int
    let filter: String

    var id: String { path }

    var displaySize: String {
        ByteCountFormatter.string(fromByteCount: Int64(sizeBytes), countStyle: .file)
    }
}

struct SharingStatus: Decodable {
    let repositoryRoot: String
    let indexPath: String
    let subjectCount: Int
    let documentCount: Int
    let libraryBytes: Int
    let looseFileCount: Int
    let looseBytes: Int
    let totalFileCount: Int
    let totalBytes: Int
    let largeFileCount: Int
    let blockedFileCount: Int
    let largeFiles: [SharingFile]
    let blockedFiles: [SharingFile]
    let gitAvailable: Bool
    let gitLfsInstalled: Bool
    let pendingFileCount: Int?

    var totalDisplaySize: String {
        ByteCountFormatter.string(fromByteCount: Int64(totalBytes), countStyle: .file)
    }
}

struct NativeSettings: Codable {
    var ollamaHost = "http://127.0.0.1:11434"
    var llmModel = "qwen3.5:27b"
    var embeddingModel = "nomic-embed-text"
    var llmTemperature = 0.1
    var ollamaNumCtx = 16_384
    var ollamaKeepAlive = "45m"
    var ollamaThinking = "high"
    var agentProvider = "ollama"
    var agentReasoningLevel = "deep"
    var codexModel = ""
    var codexReasoningEffort = "medium"
    var codexTimeoutSeconds = 180
    var whisperModel = "large-v3"
    var whisperDevice = "auto"
    var whisperComputeType = "float32"
    var whisperParallelWorkers = 2
    var language: String? = "en"
    var mediaChunkSeconds = 1_800
    var mediaOverlapSeconds = 15
    var enableTranscriptCleanup = true
    var transcriptCleanupBatchChars = 12_000
    var enableOcr = true
    var semanticAlignmentThreshold = 0.2
    var chunkSize = 900
    var chunkOverlap = 140
    var maxSlideContextChars = 28_000
    var qualityReview = true
    var noteGenerationProfile = "fast"
    var noteMaxOutputTokens = 1_200

    func dictionary() throws -> [String: Any] {
        let encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
        let data = try encoder.encode(self)
        return try JSONSerialization.jsonObject(with: data) as? [String: Any] ?? [:]
    }
}

struct AgentSource: Codable, Identifiable {
    let marker: String
    let citation: String
    let text: String
    var id: String { "\(marker)-\(citation)" }
}

struct PendingAgentAction: Codable {
    let action: String
    let message: String
    let query: String
    let documentId: String
    let newName: String
    let targetSubjectId: String
    let subjectName: String

    var confirmationTitle: String {
        switch action {
        case "rename_document": "Rename \(newName.isEmpty ? "document" : newName)"
        case "move_document": "Move document to \(targetSubjectId)"
        case "create_subject": "Create subject \(subjectName)"
        default: "Confirm library change"
        }
    }
}

struct AgentReply: Decodable {
    let answer: String
    let sources: [AgentSource]
    let pendingAction: PendingAgentAction?
}

struct AgentState: Decodable {
    let active: Bool
    let ollamaActive: Bool
    let codexReady: Bool
    let codexStatus: String
}

struct AgentConfirmation: Decodable {
    let message: String
}

struct NativeTranscriptParagraph: Decodable, Identifiable {
    let paragraphId: String?
    let start: Double?
    let end: Double?
    let startTime: String?
    let endTime: String?
    let text: String

    var id: String { paragraphId ?? "\(start ?? 0)-\(text.prefix(36))" }

    var startSeconds: Double {
        if let start { return start }
        return Self.parseTime(startTime ?? "")
    }

    var endSeconds: Double {
        if let end { return end }
        return Self.parseTime(endTime ?? "")
    }

    private static func parseTime(_ value: String) -> Double {
        let parts = value.split(separator: ":").compactMap { Double($0) }
        return parts.reversed().enumerated().reduce(0) { total, item in
            total + item.element * pow(60, Double(item.offset))
        }
    }
}

struct NativeDeepReview: Decodable {
    let jobId: String
    let status: String
    let progress: Int
    let message: String
    let text: String
}

struct NativeTranslationJob: Decodable, Identifiable {
    let language: String
    let status: String
    let progress: Int
    let message: String
    var id: String { "\(language)-\(status)-\(progress)" }
}

struct NativeLectureSlide: Decodable, Identifiable {
    let slide: Int
    let title: String
    let content: String
    let previewImage: String
    let summary: String
    let paragraphs: [NativeTranscriptParagraph]
    let translations: [String: String]
    let translationJobs: [NativeTranslationJob]
    let deepReview: NativeDeepReview?
    var id: Int { slide }
}

struct NativeLectureBundle: Decodable {
    let kind: String
    let sourceJob: JobSummary?
    let lectureName: String?
    let slides: [NativeLectureSlide]
    let paragraphs: [NativeTranscriptParagraph]
    let translations: [String: String]

    static let empty = NativeLectureBundle(
        kind: "none", sourceJob: nil, lectureName: nil, slides: [], paragraphs: [], translations: [:]
    )
}

struct VoiceOptions: Decodable {
    let voices: [String]
    let setupMessage: String
}

struct VoiceAudio: Decodable {
    let audioBase64: String
    let durationSeconds: Double
    let truncated: Bool
    let characters: Int
}

struct AgentMessage: Identifiable, Codable {
    let id: UUID
    let role: String
    let content: String
    let sources: [AgentSource]

    init(role: String, content: String, sources: [AgentSource] = []) {
        self.id = UUID()
        self.role = role
        self.content = content
        self.sources = sources
    }
}

struct LibraryImportResult: Decodable {
    let documents: [LibraryDocument]
    let errors: [ImportError]
}

struct ImportError: Decodable, Identifiable {
    let path: String
    let error: String
    var id: String { path }
}

struct SearchResult: Decodable, Identifiable {
    let documentId: String
    let documentName: String
    let subjectId: String
    let subjectName: String
    let locator: String
    let text: String
    let score: Double

    var id: String { "\(documentId)-\(locator)" }
}

struct JobEvent: Decodable, Identifiable {
    let id: Int
    let jobId: String
    let createdAt: String
    let level: String
    let stage: String
    let progress: Int
    let message: String
}

struct RunningModel: Decodable, Identifiable {
    let name: String
    let sizeBytes: Int
    let vramBytes: Int
    let expiresAt: String
    var id: String { name }
}

struct ModelUnloadReport: Decodable {
    let stopped: [String]
    let retained: [String]
    let failures: [String: String]
}

struct ManagerSettings: Decodable {
    let autoUnload: Bool
    let agentActive: Bool
    let ollamaAgentActive: Bool
}

enum JSONValue: Decodable {
    case string(String)
    case number(Double)
    case bool(Bool)
    case object([String: JSONValue])
    case array([JSONValue])
    case null

    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if container.decodeNil() { self = .null }
        else if let value = try? container.decode(Bool.self) { self = .bool(value) }
        else if let value = try? container.decode(Double.self) { self = .number(value) }
        else if let value = try? container.decode(String.self) { self = .string(value) }
        else if let value = try? container.decode([String: JSONValue].self) { self = .object(value) }
        else if let value = try? container.decode([JSONValue].self) { self = .array(value) }
        else { throw DecodingError.dataCorruptedError(in: container, debugDescription: "Unsupported JSON value") }
    }

    var stringValue: String? {
        if case .string(let value) = self { value } else { nil }
    }
}

struct NativeJobDetail: Decodable, Identifiable {
    let id: String
    let kind: String
    let title: String
    let status: String
    let result: [String: JSONValue]
    let error: String

    var artifacts: [NativeJobArtifact] {
        let candidates: [(String, String)] = [
            ("Lecture notes (Markdown)", "markdown_path"),
            ("Lecture notes (PDF)", "pdf_path"),
            ("Lecture summary (Markdown)", "lecture_summary_markdown_path"),
            ("Lecture summary (PDF)", "lecture_summary_pdf_path"),
            ("Translated notes (Markdown)", "markdown_path"),
            ("Translated notes (PDF)", "pdf_path"),
            ("Transcript", "transcript_text_path"),
        ]
        var seen = Set<String>()
        return candidates.compactMap { label, key in
            guard let path = result[key]?.stringValue, !path.isEmpty,
                  FileManager.default.fileExists(atPath: path), seen.insert(path).inserted else { return nil }
            let title: String
            if key == "markdown_path" || key == "pdf_path" {
                title = titleForResult(key: key)
            } else {
                title = label
            }
            return NativeJobArtifact(title: title, url: URL(fileURLWithPath: path))
        }
    }

    private func titleForResult(key: String) -> String {
        switch kind {
        case "translation": key == "pdf_path" ? "Translated notes (PDF)" : "Translated notes (Markdown)"
        case "slide_review": key == "pdf_path" ? "Deep review (PDF)" : "Deep review (Markdown)"
        default: key == "pdf_path" ? "Lecture notes (PDF)" : "Lecture notes (Markdown)"
        }
    }
}

struct NativeJobArtifact: Identifiable, Hashable {
    let title: String
    let url: URL
    var id: String { url.path }
}

struct BackendErrorResponse: Decodable {
    let message: String
    let type: String
}

enum NativeBackendError: LocalizedError {
    case workspaceNotFound
    case pythonEnvironmentMissing(String)
    case processFailed(String)
    case remote(String)
    case malformedResponse

    var errorDescription: String? {
        switch self {
        case .workspaceNotFound:
            "Select a Class Knowledge workspace folder containing app.py and src/."
        case .pythonEnvironmentMissing(let path):
            "Python environment not found at \(path). Create the project .venv and install requirements.txt."
        case .processFailed(let message), .remote(let message): message
        case .malformedResponse: "The Python helper returned an invalid response."
        }
    }
}
