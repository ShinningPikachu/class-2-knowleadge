import AppKit
import AVKit
import PDFKit
import QuickLookUI
import SwiftUI
import UniformTypeIdentifiers

struct LibraryWorkspaceView: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.openWindow) private var openWindow
    @State private var showingNewSubject = false
    @State private var showingFolderName = false
    @State private var folderName = ""
    @State private var editingFolder: LibraryFolder?
    @State private var showingRenameDocument = false
    @State private var documentName = ""
    @State private var documentToDelete: LibraryDocument?
    @State private var subjectToDelete: LibrarySubject?
    @State private var folderToDelete: LibraryFolder?
    @State private var showingFilePicker = false
    @State private var searchSubmitted = false
    @State private var isFileDropTarget = false

    private var visibleDocuments: [LibraryDocument] {
        let foldered = model.documents.filter {
            $0.folderId == model.selectedFolderID && !isInternalLectureArtifact($0.originalName)
        }
        let needle = model.searchQuery.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !needle.isEmpty else { return foldered }
        return foldered.filter { $0.originalName.localizedCaseInsensitiveContains(needle) }
    }

    private var visibleSearchResults: [SearchResult] {
        model.searchResults.filter { !isInternalLectureArtifact($0.documentName) }
    }

    private var visibleDocumentCount: Int {
        model.documents.filter { !isInternalLectureArtifact($0.originalName) }.count
    }

    var body: some View {
        GeometryReader { geometry in
            let availableWidth = geometry.size.width
            let subjectWidth = min(260, max(170, availableWidth * 0.27))
            let contentsWidth = min(370, max(220, availableWidth * 0.39))

            HStack(spacing: 0) {
                subjectPane
                    .frame(width: subjectWidth)
                Divider()
                contentsPane
                    .frame(width: contentsWidth)
                Divider()
                previewPane
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        .background(Color(nsColor: .windowBackgroundColor))
        .sheet(isPresented: $showingNewSubject) {
            NewSubjectSheet()
                .environmentObject(model)
        }
        .sheet(isPresented: $showingFolderName) {
            NameEntrySheet(
                title: editingFolder == nil ? "New Folder" : "Rename Folder",
                label: "Folder name",
                name: $folderName
            ) {
                if let editingFolder {
                    Task { await model.renameFolder(editingFolder.id, name: folderName) }
                } else {
                    Task { await model.createFolder(name: folderName) }
                }
                showingFolderName = false
            }
        }
        .sheet(isPresented: $showingRenameDocument) {
            NameEntrySheet(title: "Rename File", label: "File name", name: $documentName) {
                if let document = model.selectedDocument {
                    Task { await model.renameDocument(document.id, name: documentName) }
                }
                showingRenameDocument = false
            }
        }
        .fileImporter(
            isPresented: $showingFilePicker,
            allowedContentTypes: [.item],
            allowsMultipleSelection: true
        ) { result in
            switch result {
            case .success(let urls): Task { await model.importFiles(urls) }
            case .failure(let error): model.errorMessage = error.localizedDescription
            }
        }
        .onChange(of: model.selectedSubjectID) { _, _ in
            model.selectedFolderID = nil
            model.selectedDocumentID = nil
            model.previewedDocument = nil
            model.lectureBundle = .empty
            searchSubmitted = false
            model.searchResults = []
            Task { await model.loadLibrary() }
        }
        .onChange(of: model.searchQuery) { _, _ in searchSubmitted = false }
        .confirmationDialog(
            "Delete \(documentToDelete?.originalName ?? "this file")?",
            isPresented: Binding(get: { documentToDelete != nil }, set: { if !$0 { documentToDelete = nil } }),
            presenting: documentToDelete
        ) { document in
            Button("Delete file", role: .destructive) {
                Task { await model.deleteDocument(document) }
                documentToDelete = nil
            }
            Button("Cancel", role: .cancel) { documentToDelete = nil }
        } message: { document in
            Text("This permanently removes \(document.originalName) from \(document.subjectName) and its search index.")
        }
        .confirmationDialog(
            "Delete subject \(subjectToDelete?.name ?? "")?",
            isPresented: Binding(get: { subjectToDelete != nil }, set: { if !$0 { subjectToDelete = nil } }),
            presenting: subjectToDelete
        ) { subject in
            Button("Delete subject and all files", role: .destructive) {
                Task { await model.deleteSubject(subject.id) }
                subjectToDelete = nil
            }
            Button("Cancel", role: .cancel) { subjectToDelete = nil }
        } message: { subject in
            Text("This permanently deletes \(subject.name), its documents, folders, and indexed text.")
        }
        .confirmationDialog(
            "Delete folder \(folderToDelete?.name ?? "")?",
            isPresented: Binding(get: { folderToDelete != nil }, set: { if !$0 { folderToDelete = nil } }),
            presenting: folderToDelete
        ) { folder in
            Button("Delete folder and keep files", role: .destructive) {
                Task { await model.deleteFolder(folder.id) }
                folderToDelete = nil
            }
            if folder.documentCount > 0 {
                Button("Delete folder and its files", role: .destructive) {
                    Task { await model.deleteFolder(folder.id, deleteDocuments: true) }
                    folderToDelete = nil
                }
            }
            Button("Cancel", role: .cancel) { folderToDelete = nil }
        } message: { folder in
            Text(folder.documentCount == 0
                 ? "This removes the empty folder."
                 : "Choose whether to keep its \(folder.documentCount) file(s) in the subject root or delete them too.")
        }
    }

    private var subjectPane: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("Subjects")
                    .font(.headline)
                Spacer()
                Button {
                    showingNewSubject = true
                } label: {
                    Label("New", systemImage: "plus")
                }
                .buttonStyle(.borderless)
                .help("Create a subject")
            }
            .padding(.horizontal, 14)
            .padding(.top, 14)

            List(selection: $model.selectedSubjectID) {
                ForEach(model.snapshot?.subjects ?? []) { subject in
                    Label {
                        Text(subject.name)
                            .lineLimit(1)
                            .truncationMode(.tail)
                            .frame(minWidth: 0, maxWidth: .infinity, alignment: .leading)
                    } icon: {
                        Image(systemName: "books.vertical")
                    }
                        .frame(minWidth: 0, maxWidth: .infinity, alignment: .leading)
                        .clipped()
                        .tag(subject.id as String?)
                        .help(subject.name)
                        .contextMenu {
                            Button("Delete Subject…", role: .destructive) { subjectToDelete = subject }
                        }
                }
            }
            .listStyle(.sidebar)
        }
    }

    private var contentsPane: some View {
        VStack(alignment: .leading, spacing: 12) {
            VStack(alignment: .leading, spacing: 9) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(model.selectedSubject?.name ?? "Library")
                        .font(.headline)
                        .lineLimit(2)
                        .fixedSize(horizontal: false, vertical: true)
                    Text("\(visibleDocumentCount) files · \(model.folders.count) folders")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                HStack(spacing: 8) {
                    Button {
                        showingFilePicker = true
                    } label: {
                        Label("Import", systemImage: "square.and.arrow.down")
                    }
                    .disabled(model.selectedSubjectID == nil || !model.isConnected)
                    .help("Import files into this subject")
                    Button {
                        editingFolder = nil
                        folderName = ""
                        showingFolderName = true
                    } label: {
                        Label("New Folder", systemImage: "folder.badge.plus")
                    }
                    .disabled(model.selectedSubjectID == nil || !model.isConnected)
                    .help("Create a folder")
                    Spacer(minLength: 0)
                }
                Label(
                    isFileDropTarget ? "Release to import files" : "Drop PDF, slides, audio, or Voice Memos here",
                    systemImage: "arrow.down.doc"
                )
                .font(.caption2)
                .foregroundStyle(isFileDropTarget ? Color.accentColor : Color.secondary)
            }
            .padding(.horizontal, 14)
            .padding(.top, 14)

            HStack(spacing: 7) {
                TextField("Search this subject", text: $model.searchQuery)
                    .textFieldStyle(.roundedBorder)
                    .onSubmit { submitSearch() }
                Button { submitSearch() } label: { Image(systemName: "magnifyingglass") }
                    .buttonStyle(.borderless)
                    .help("Search indexed document text")
                if !model.searchQuery.isEmpty {
                    Button {
                        model.searchQuery = ""
                        searchSubmitted = false
                        model.searchResults = []
                    } label: { Image(systemName: "xmark.circle.fill") }
                        .buttonStyle(.borderless)
                        .help("Clear search")
                }
            }
            .padding(.horizontal, 14)

            if !model.searchQuery.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && !searchSubmitted {
                Label("Press Return to search inside document text.", systemImage: "info.circle")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .padding(.horizontal, 14)
            }

            if !model.importErrors.isEmpty {
                DisclosureGroup("Import warnings · \(model.importErrors.count)") {
                    VStack(alignment: .leading, spacing: 6) {
                        ForEach(model.importErrors) { item in
                            Text("\(URL(fileURLWithPath: item.path).lastPathComponent): \(item.error)")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                                .textSelection(.enabled)
                        }
                    }
                    .padding(.top, 6)
                }
                .padding(.horizontal, 14)
            }

            List {
                if searchSubmitted && !model.searchQuery.isEmpty {
                    if visibleSearchResults.isEmpty {
                        ContentUnavailableView("No matches", systemImage: "magnifyingglass", description: Text("No indexed passages matched this search."))
                    }
                    ForEach(visibleSearchResults) { result in
                        Button {
                            Task { await model.openDocument(result.documentId) }
                        } label: {
                            VStack(alignment: .leading, spacing: 5) {
                                Text(result.documentName).font(.callout.weight(.semibold)).foregroundStyle(.primary)
                                Text(result.locator).font(.caption).foregroundStyle(.secondary)
                                Text(result.text).font(.caption).lineLimit(4).foregroundStyle(.secondary)
                            }
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(.vertical, 5)
                        }
                        .buttonStyle(.plain)
                    }
                } else {
                    if model.selectedFolderID != nil {
                        Button {
                            model.selectedFolderID = nil
                        } label: {
                            Label("Subject root", systemImage: "arrow.uturn.backward")
                        }
                        .buttonStyle(.plain)
                        .dropDestination(for: String.self) { documentIDs, _ in
                            guard !documentIDs.isEmpty else { return false }
                            for documentID in documentIDs {
                                Task { await model.moveDocument(documentID, toFolder: nil) }
                            }
                            return true
                        }
                    }
                    if model.selectedFolderID == nil {
                        ForEach(model.folders) { folder in
                            Button {
                                model.selectedFolderID = folder.id
                                model.selectedDocumentID = nil
                                model.previewedDocument = nil
                            } label: {
                                Label(folder.name, systemImage: "folder")
                                    .badge(folder.documentCount)
                            }
                            .buttonStyle(.plain)
                            .dropDestination(for: String.self) { documentIDs, _ in
                                guard !documentIDs.isEmpty else { return false }
                                for documentID in documentIDs {
                                    Task { await model.moveDocument(documentID, toFolder: folder.id) }
                                }
                                return true
                            }
                            .contextMenu {
                                Button("Rename Folder…") {
                                    editingFolder = folder
                                    folderName = folder.name
                                    showingFolderName = true
                                }
                                Button("Delete Folder…", role: .destructive) { folderToDelete = folder }
                            }
                        }
                    }
                    ForEach(visibleDocuments) { document in
                        Button {
                            model.selectDocument(document)
                        } label: {
                            HStack(spacing: 9) {
                                Image(systemName: symbol(for: document.originalName))
                                    .foregroundStyle(.tint)
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(document.originalName)
                                        .lineLimit(1)
                                        .foregroundStyle(.primary)
                                    Text("\(document.displaySize) · \(document.status.replacingOccurrences(of: "_", with: " "))")
                                        .font(.caption2)
                                        .foregroundStyle(.secondary)
                                }
                                Spacer(minLength: 0)
                            }
                            .padding(.vertical, 3)
                            .contentShape(Rectangle())
                            .background(model.selectedDocumentID == document.id ? Color.accentColor.opacity(0.12) : .clear)
                        }
                        .buttonStyle(.plain)
                        .draggable(document.id)
                    }
                }
            }
            .listStyle(.plain)
            .overlay {
                if model.selectedSubjectID == nil {
                    ContentUnavailableView {
                        Label("Create a subject", systemImage: "books.vertical")
                    } description: {
                        Text("Add a subject to start organizing local course materials.")
                    } actions: {
                        Button("Create Subject") { showingNewSubject = true }
                            .buttonStyle(.borderedProminent)
                    }
                } else if searchSubmitted && !model.searchQuery.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                    EmptyView()
                } else if model.searchQuery.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
                          visibleDocuments.isEmpty,
                          model.selectedFolderID == nil,
                          model.folders.isEmpty {
                    ContentUnavailableView {
                        Label("No files yet", systemImage: "doc.badge.plus")
                    } description: {
                        Text("Import PDFs, recordings, slides, and notes into this subject.")
                    } actions: {
                        Button("Import Files") { showingFilePicker = true }
                            .buttonStyle(.borderedProminent)
                    }
                } else if model.searchQuery.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
                          model.selectedFolderID != nil,
                          visibleDocuments.isEmpty {
                    ContentUnavailableView {
                        Label("This folder is empty", systemImage: "folder")
                    } description: {
                        Text("Import files here or drag documents into this folder.")
                    } actions: {
                        Button("Import Files") { showingFilePicker = true }
                            .buttonStyle(.borderedProminent)
                    }
                }
            }
        }
        .background {
            PromisedFileDropTarget(
                isTargeted: $isFileDropTarget,
                onFiles: importDroppedFiles,
                onError: { model.errorMessage = $0 }
            )
        }
        .onDrop(
            of: libraryDropTypes.map(\.identifier),
            isTargeted: $isFileDropTarget,
            perform: receiveDroppedProviders
        )
        .overlay {
            if isFileDropTarget {
                RoundedRectangle(cornerRadius: 12)
                    .strokeBorder(Color.accentColor, lineWidth: 2)
                    .padding(4)
                    .allowsHitTesting(false)
            }
        }
    }

    private func importDroppedFiles(_ urls: [URL]) {
        let files = urls.filter(\.isFileURL)
        guard !files.isEmpty else { return }
        guard model.selectedSubjectID != nil else {
            model.errorMessage = "Choose or create a subject before importing files."
            return
        }
        Task { await model.importFiles(files) }
    }

    private var libraryDropTypes: [UTType] {
        var types: [UTType] = [
            .fileURL, .audio, .movie, .pdf, .plainText,
            .mpeg4Audio, .mp3, .wav,
            .mpeg4Movie, .quickTimeMovie
        ]
        let extensions = [
            "mp3", "wav", "m4a", "aac", "flac", "ogg", "opus",
            "mp4", "mov", "mkv", "webm", "m4v",
            "ppt", "pptx", "md"
        ]
        types.append(contentsOf: extensions.compactMap { UTType(filenameExtension: $0) })
        return Array(Set(types))
    }

    private func receiveDroppedProviders(_ providers: [NSItemProvider]) -> Bool {
        DroppedItemProviderLoader.load(providers, acceptedTypes: libraryDropTypes) { urls, errors in
            if !urls.isEmpty {
                importDroppedFiles(urls)
            } else if let error = errors.first {
                model.errorMessage = "Could not receive the dropped file: \(error)"
            }
        }
    }

    private var previewPane: some View {
        Group {
            if let document = model.selectedDocument {
                VStack(spacing: 0) {
                    HStack {
                        VStack(alignment: .leading, spacing: 3) {
                            Text(document.originalName)
                                .font(.headline)
                                .lineLimit(1)
                            Text(document.folderName.isEmpty ? document.subjectName : "\(document.subjectName) / \(document.folderName)")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                        Spacer()
                        Button {
                            openWindow(id: "library-document", value: document.id)
                        } label: {
                            Image(systemName: "arrow.up.left.and.arrow.down.right")
                        }
                        .help("Open full screen")
                        Button {
                            NSWorkspace.shared.activateFileViewerSelecting([document.fileURL])
                        } label: {
                            Image(systemName: "folder")
                        }
                        .help("Show in Finder")
                        Menu {
                            Button("Rename…") {
                                documentName = document.originalName
                                showingRenameDocument = true
                            }
                            Menu("Move to subject") {
                                ForEach(model.snapshot?.subjects ?? []) { subject in
                                    if subject.id != document.subjectId {
                                        Button(subject.name) {
                                            Task { await model.moveDocument(document.id, toSubject: subject.id) }
                                        }
                                    }
                                }
                            }
                            Menu("Move to folder") {
                                Button("Subject root") {
                                    Task { await model.moveDocument(document.id, toFolder: nil) }
                                }
                                ForEach(model.folders.filter { $0.id != document.folderId }) { folder in
                                    Button(folder.name) {
                                        Task { await model.moveDocument(document.id, toFolder: folder.id) }
                                    }
                                }
                            }
                            Divider()
                            Button("Delete…", role: .destructive) { documentToDelete = document }
                        } label: {
                            Image(systemName: "ellipsis.circle")
                        }
                        .menuStyle(.borderlessButton)
                        .menuIndicator(.hidden)
                        .fixedSize()
                        .help("File actions")
                    }
                    .padding(14)
                    Divider()
                    DocumentPreview(document: document, bundle: model.lectureBundle, fullScreen: false)
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                }
            } else {
                VStack(spacing: 12) {
                    Image(systemName: "doc.text.magnifyingglass")
                        .font(.system(size: 34))
                        .foregroundStyle(.tertiary)
                    Text("Select a file")
                        .font(.title2.weight(.semibold))
                        .multilineTextAlignment(.center)
                    Text("Choose a file to preview it here.")
                        .font(.body)
                        .foregroundStyle(.secondary)
                        .multilineTextAlignment(.center)
                        .fixedSize(horizontal: false, vertical: true)
                        .frame(maxWidth: 300)
                }
                .padding(24)
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .center)
            }
        }
    }

    private func symbol(for filename: String) -> String {
        switch URL(fileURLWithPath: filename).pathExtension.lowercased() {
        case "pdf": "doc.richtext"
        case "ppt", "pptx", "key": "rectangle.on.rectangle"
        case "mp3", "m4a", "wav", "aac", "flac", "ogg", "opus", "mp4", "mov", "mkv", "webm": "waveform"
        case "md", "markdown", "txt": "doc.text"
        default: "doc"
        }
    }

    private func submitSearch() {
        searchSubmitted = !model.searchQuery.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        Task { await model.search() }
    }

    private func isInternalLectureArtifact(_ filename: String) -> Bool {
        let url = URL(fileURLWithPath: filename)
        let stem = url.deletingPathExtension().lastPathComponent.lowercased()
        let suffix = url.pathExtension.lowercased()
        let normalized = stem.unicodeScalars.map { scalar -> Character in
            CharacterSet.alphanumerics.contains(scalar) ? Character(String(scalar)) : "_"
        }
        .reduce(into: "") { value, character in
            if character == "_", value.last == "_" { return }
            value.append(character)
        }
        .trimmingCharacters(in: CharacterSet(charactersIn: "_"))
        let internalSuffixes = ["transcript_raw", "transcript_cleaned", "slides_extracted", "slide_summaries", "alignment", "quality_report", "manifest"]
        let transcript = normalized.contains("transcript") && ["json", "txt"].contains(suffix)
        let generatedMarkdown = normalized.hasSuffix("_notes") && ["md", "markdown"].contains(suffix)
        let translated = ["md", "markdown", "pdf"].contains(suffix)
            && (stem.hasPrefix("notes.") || stem.range(of: #"^slide_[0-9]+\."#, options: .regularExpression) != nil)
        let review = normalized.contains("_deep_review_") && ["md", "markdown"].contains(suffix)
        return transcript || internalSuffixes.contains(where: { normalized.hasSuffix($0) })
            || generatedMarkdown || translated || review
    }
}

struct DocumentPreview: View {
    let document: LibraryDocument
    let bundle: NativeLectureBundle
    let fullScreen: Bool
    @State private var text = ""
    @State private var loadError: String?
    @State private var mediaPlayer: AVPlayer?
    @State private var mediaPlayerDocumentID: String?

    private var suffix: String { document.fileURL.pathExtension.lowercased() }
    private var isText: Bool {
        ["txt", "md", "markdown", "csv", "tsv", "json", "yaml", "yml", "py", "html", "htm"].contains(suffix)
    }
    private var isMedia: Bool {
        ["mp3", "m4a", "wav", "aac", "flac", "ogg", "opus", "mp4", "mov", "mkv", "webm", "m4v"].contains(suffix)
    }
    private var isVideo: Bool { ["mp4", "mov", "mkv", "webm", "m4v"].contains(suffix) }

    var body: some View {
        Group {
            if isText {
                ScrollView {
                    if let loadError {
                        ContentUnavailableView("Preview unavailable", systemImage: "exclamationmark.triangle", description: Text(loadError))
                    } else {
                        Text(text)
                            .font(.system(.body, design: .monospaced))
                            .textSelection(.enabled)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(18)
                    }
                }
            } else if bundle.kind == "slides", !bundle.slides.isEmpty {
                LectureSlidesPreview(document: document, bundle: bundle, fullScreen: fullScreen)
            } else if bundle.kind == "audio", !bundle.paragraphs.isEmpty {
                AudioTranscriptView(url: document.fileURL, paragraphs: bundle.paragraphs, isVideo: isVideo)
            } else if suffix == "pdf" {
                FullScreenPDFPageView(url: document.fileURL)
            } else if isMedia {
                if let mediaPlayer, mediaPlayerDocumentID == document.id {
                    NativeMediaPlayer(player: mediaPlayer)
                } else {
                    ContentUnavailableView(
                        "Preparing playback",
                        systemImage: "waveform",
                        description: Text("Loading \(document.originalName)…")
                    )
                }
            } else {
                QuickLookDocumentView(url: document.fileURL)
            }
        }
        .task(id: document.id) {
            mediaPlayer = isMedia ? AVPlayer(url: document.fileURL) : nil
            mediaPlayerDocumentID = isMedia ? document.id : nil
            guard isText else { return }
            do {
                text = try String(contentsOf: document.fileURL, encoding: .utf8)
                loadError = nil
            } catch {
                loadError = error.localizedDescription
            }
        }
    }
}

struct FullScreenDocumentWindow: View {
    @EnvironmentObject private var model: AppModel
    let documentID: String?
    @State private var requestedFullScreen = false

    var body: some View {
        Group {
            if let documentID,
               let document = model.selectedDocument,
               document.id == documentID {
                FullScreenDocumentView(document: document, bundle: model.lectureBundle)
            } else if documentID != nil {
                ProgressView("Opening document…")
            } else {
                ContentUnavailableView("No document selected", systemImage: "doc")
            }
        }
        .frame(minWidth: 1200, minHeight: 760)
        .task(id: documentID) {
            guard let documentID,
                  model.selectedDocument?.id != documentID else { return }
            await model.openDocument(documentID)
        }
        .onAppear {
            guard !requestedFullScreen else { return }
            requestedFullScreen = true
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) {
                NSApp.keyWindow?.toggleFullScreen(nil)
            }
        }
    }
}

private struct FullScreenDocumentView: View {
    @Environment(\.dismiss) private var dismiss
    let document: LibraryDocument
    let bundle: NativeLectureBundle

    private var isPDF: Bool { document.fileURL.pathExtension.lowercased() == "pdf" }

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 12) {
                VStack(alignment: .leading, spacing: 3) {
                    Text(document.originalName)
                        .font(.headline)
                        .lineLimit(1)
                    Text(document.folderName.isEmpty ? document.subjectName : "\(document.subjectName) / \(document.folderName)")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                Spacer()
                Button("Close") { dismiss() }
                    .keyboardShortcut(.escape, modifiers: [])
            }
            .padding(.horizontal, 20)
            .padding(.vertical, 12)
            Divider()
            Group {
                if isPDF && bundle.kind != "slides" {
                    FullScreenPDFPageView(url: document.fileURL)
                } else {
                    DocumentPreview(document: document, bundle: bundle, fullScreen: true)
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color(nsColor: .windowBackgroundColor))
    }
}

private struct FullScreenPDFPageView: View {
    let url: URL
    @State private var index = 0
    @State private var document: PDFDocument?
    @State private var image: NSImage?
    @State private var pageImages: [Int: NSImage] = [:]
    private var count: Int { document?.pageCount ?? 0 }
    private var text: String { document?.page(at: index)?.string ?? "No selectable text is available." }

    var body: some View {
        GeometryReader { geometry in
            VStack(spacing: 12) {
                HStack {
                    Button { turn(-1) } label: { Image(systemName: "chevron.left") }
                        .disabled(index == 0)
                    Text("Page \(index + 1) of \(max(count, 1)) · Scroll to turn")
                        .monospacedDigit()
                    Button { turn(1) } label: { Image(systemName: "chevron.right") }
                        .disabled(index >= count - 1)
                    Spacer()
                }
                HStack(spacing: 16) {
                    ElasticPresentationStage(index: Binding(get: { index }, set: { selectPage($0) }), count: count) {
                        if let image {
                            Image(nsImage: image).resizable().scaledToFit()
                        }
                    }
                    .frame(width: geometry.size.width * 0.6)
                    ScrollView {
                        VStack(alignment: .leading, spacing: 12) {
                            Text("Page \(index + 1) · explanation").font(.headline)
                            Text(text).textSelection(.enabled)
                        }
                        .frame(maxWidth: .infinity, alignment: .leading)

                    }
                    .background(PresentationScrollRegion(isText: true))
                }
            }
            .padding(14)
        }
        .background(PresentationScrollRegion())
        .onAppear { load() }
        .onChange(of: url) { _, _ in index = 0; load() }

    }
    private func load() { pageImages = [:]; document = PDFDocument(url: url); renderPage() }
    private func renderPage() {
        image = cachedImage(index)
        preloadPages()
    }
    private func cachedImage(_ page: Int) -> NSImage? {
        if let cached = pageImages[page] { return cached }
        let result = document?.page(at: page)?.thumbnail(of: NSSize(width: 1400, height: 1000), for: .cropBox)
        if let result { pageImages[page] = result }
        return result
    }
    private func preloadPages() {
        for page in [index - 1, index + 1] where (0..<count).contains(page) { _ = cachedImage(page) }
        pageImages = pageImages.filter { abs($0.key - index) <= 1 }
    }
    private func turn(_ step: Int) {
        guard (0..<count).contains(index + step) else { return }
        withAnimation(.spring(response: 0.30, dampingFraction: 0.92)) { selectPage(index + step) }
    }
    private func selectPage(_ target: Int) {
        guard (0..<count).contains(target) else { return }
        let nextImage = cachedImage(target)
        image = nextImage
        index = target
        DispatchQueue.main.async { preloadPages() }
    }
}

struct PDFDocumentView: NSViewRepresentable {
    let url: URL
    var pageNumber: Int? = nil
    var onPageChange: ((Int) -> Void)? = nil
    var scrollToPage: Int? = nil

    final class Coordinator: NSObject {
        weak var view: PDFView?
        var onPageChange: ((Int) -> Void)?
        var lastRequestedPage: Int?

        init(onPageChange: ((Int) -> Void)?) {
            self.onPageChange = onPageChange
        }

        @objc func pageChanged(_ notification: Notification) {
            guard let view, let page = view.currentPage, let document = view.document else { return }
            let number = document.index(for: page) + 1
            guard number > 0 else { return }
            DispatchQueue.main.async { [weak self] in self?.onPageChange?(number) }
        }
    }

    func makeCoordinator() -> Coordinator { Coordinator(onPageChange: onPageChange) }

    func makeNSView(context: Context) -> PDFView {
        let view = PDFView()
        view.autoScales = true
        view.displayMode = pageNumber == nil ? .singlePageContinuous : .singlePage
        view.displayDirection = .vertical
        view.document = PDFDocument(url: url)
        if let pageNumber, let page = view.document?.page(at: max(0, pageNumber - 1)) {
            view.go(to: page)
        }
        context.coordinator.view = view
        NotificationCenter.default.addObserver(
            context.coordinator,
            selector: #selector(Coordinator.pageChanged(_:)),
            name: .PDFViewPageChanged,
            object: view
        )
        return view
    }

    func updateNSView(_ view: PDFView, context: Context) {
        let displayMode: PDFDisplayMode = pageNumber == nil ? .singlePageContinuous : .singlePage
        if view.displayMode != displayMode { view.displayMode = displayMode }
        if !view.autoScales { view.autoScales = true }
        context.coordinator.onPageChange = onPageChange
        if view.document?.documentURL != url {
            view.document = PDFDocument(url: url)
            context.coordinator.lastRequestedPage = nil
        }
        if let pageNumber, let page = view.document?.page(at: max(0, pageNumber - 1)), view.currentPage !== page {
            view.go(to: page)
        }
        if let scrollToPage,
           scrollToPage != context.coordinator.lastRequestedPage,
           let page = view.document?.page(at: max(0, scrollToPage - 1)) {
            context.coordinator.lastRequestedPage = scrollToPage
            view.go(to: page)
        } else if scrollToPage == nil {
            context.coordinator.lastRequestedPage = nil
        }
    }

    static func dismantleNSView(_ view: PDFView, coordinator: Coordinator) {
        NotificationCenter.default.removeObserver(coordinator, name: .PDFViewPageChanged, object: view)
    }
}

struct QuickLookDocumentView: NSViewRepresentable {
    let url: URL

    func makeNSView(context: Context) -> QLPreviewView {
        let view = QLPreviewView(frame: .zero, style: .normal)!
        view.autostarts = true
        view.previewItem = url as NSURL
        return view
    }

    func updateNSView(_ view: QLPreviewView, context: Context) {
        view.previewItem = url as NSURL
    }
}

private struct NewSubjectSheet: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.dismiss) private var dismiss
    @State private var name = ""
    @State private var description = ""
    @State private var error: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("New Subject")
                .font(.title2.weight(.semibold))
            TextField("Subject name", text: $name)
                .textFieldStyle(.roundedBorder)
            TextField("Description (optional)", text: $description)
                .textFieldStyle(.roundedBorder)
            if let error { Text(error).foregroundStyle(.red).font(.callout) }
            HStack {
                Spacer()
                Button("Cancel") { dismiss() }
                Button("Create") {
                    Task {
                        do {
                            try await model.createSubject(name: name, description: description)
                            dismiss()
                        } catch {
                            self.error = error.localizedDescription
                        }
                    }
                }
                .keyboardShortcut(.defaultAction)
                .disabled(name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || model.isBusy)
            }
        }
        .padding(24)
        .frame(width: 400)
    }
}

private struct NameEntrySheet: View {
    let title: String
    let label: String
    @Binding var name: String
    let save: () -> Void
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text(title).font(.title2.weight(.semibold))
            TextField(label, text: $name)
                .textFieldStyle(.roundedBorder)
                .onSubmit(save)
            HStack {
                Spacer()
                Button("Cancel") { dismiss() }
                Button("Save", action: save)
                    .keyboardShortcut(.defaultAction)
                    .disabled(name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
        }
        .padding(24)
        .frame(width: 400)
    }
}
