import AppKit
import SwiftUI
import UniformTypeIdentifiers

struct LectureWorkspaceView: View {
    @EnvironmentObject private var model: AppModel
    @State private var audioURL: URL?
    @State private var presentationURL: URL?
    @State private var lectureTitle = ""
    @State private var subjectID: String?
    @State private var priority = 50
    @State private var sourcesDropTargeted = false
    @State private var droppedFileMessage = ""

    private let audioExtensions = ["mp3", "wav", "m4a", "aac", "flac", "ogg", "opus", "mp4", "mov", "mkv", "webm", "m4v"]
    private let slideExtensions = ["pdf", "ppt", "pptx"]

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 22) {
                VStack(alignment: .leading, spacing: 5) {
                    Text("Lecture Notes")
                        .font(.largeTitle.weight(.semibold))
                    Text("Queue a recording, class slides, or both. Processing runs locally in the background.")
                        .foregroundStyle(.secondary)
                }

                GroupBox("1. Lecture sources") {
                    VStack(spacing: 12) {
                        fileChoice(
                            title: "Recording",
                            subtitle: "Drop audio or video here, or choose a recording.",
                            url: audioURL,
                            symbol: "waveform",
                            choose: chooseAudio,
                            remove: { audioURL = nil }
                        )
                        Divider()
                        fileChoice(
                            title: "Slides",
                            subtitle: "Drop PDF or PowerPoint slides here, or choose a deck.",
                            url: presentationURL,
                            symbol: "rectangle.on.rectangle",
                            choose: choosePresentation,
                            remove: { presentationURL = nil }
                        )
                        Label(
                            sourcesDropTargeted
                                ? "Release to add lecture sources"
                                : "Drop from Voice Memos or Finder here — recording and/or slides",
                            systemImage: "arrow.down.doc"
                        )
                        .font(.caption)
                        .foregroundStyle(sourcesDropTargeted ? Color.accentColor : Color.secondary)
                        .frame(maxWidth: .infinity, alignment: .center)
                        .padding(.vertical, 8)
                        .padding(.horizontal, 10)
                        .background(sourcesDropTargeted ? Color.accentColor.opacity(0.09) : Color.clear)
                        .clipShape(RoundedRectangle(cornerRadius: 8))
                        if !droppedFileMessage.isEmpty {
                            Text(droppedFileMessage)
                                .font(.caption)
                                .foregroundStyle(droppedFileMessage.hasPrefix("Added") ? Color.green : Color.red)
                                .frame(maxWidth: .infinity, alignment: .leading)
                        }
                    }
                    .padding(.top, 8)
                    .padding(.bottom, 8)
                    .background {
                        PromisedFileDropTarget(
                            isTargeted: $sourcesDropTargeted,
                            onFiles: { urls in _ = acceptDroppedSources(urls) },
                            onError: { droppedFileMessage = $0 }
                        )
                    }
                    .onDrop(
                        of: acceptedDropTypes.map(\.identifier),
                        isTargeted: $sourcesDropTargeted,
                        perform: receiveDroppedProviders
                    )
                    .overlay {
                        RoundedRectangle(cornerRadius: 10)
                            .stroke(
                                sourcesDropTargeted ? Color.accentColor : Color.clear,
                                style: StrokeStyle(lineWidth: 2, dash: [7, 5])
                            )
                            .allowsHitTesting(false)
                    }
                }

                GroupBox("2. Title and destination") {
                    VStack(alignment: .leading, spacing: 14) {
                        TextField("Lecture title (optional)", text: $lectureTitle, prompt: Text("Inferred from the selected file when blank"))
                            .textFieldStyle(.roundedBorder)
                        Picker("Save to subject", selection: $subjectID) {
                            Text("Do not add to Library").tag(nil as String?)
                            ForEach(model.snapshot?.subjects ?? []) { subject in
                                Text(subject.name).tag(subject.id as String?)
                            }
                        }
                        .disabled(model.snapshot?.subjects.isEmpty != false)
                        Picker("Priority", selection: $priority) {
                            Text("High").tag(10)
                            Text("Normal").tag(50)
                            Text("Low").tag(90)
                        }
                    }
                    .padding(.top, 8)
                }

                GroupBox("3. Add to queue") {
                    HStack(alignment: .center) {
                        VStack(alignment: .leading, spacing: 4) {
                            Text("Local processing")
                                .font(.headline)
                            Text("The selected files are copied into durable job storage before processing starts.")
                                .font(.callout)
                                .foregroundStyle(.secondary)
                        }
                        Spacer()
                        Button {
                            Task {
                                await model.enqueueLecture(
                                    audioURL: audioURL,
                                    presentationURL: presentationURL,
                                    title: lectureTitle,
                                    subjectID: subjectID,
                                    priority: priority
                                )
                            }
                        } label: {
                            Label("Queue Lecture Task", systemImage: "plus.circle.fill")
                        }
                        .buttonStyle(.borderedProminent)
                        .disabled(model.isBusy || (audioURL == nil && presentationURL == nil) || !model.isConnected)
                    }
                    .padding(.top, 8)
                }
                Spacer(minLength: 20)
            }
            .padding(26)
            .frame(maxWidth: 900, alignment: .leading)
            .frame(maxWidth: .infinity)
        }
        .onAppear {
            if subjectID == nil { subjectID = model.snapshot?.subjects.first?.id }
        }
    }

    private func fileChoice(
        title: String,
        subtitle: String,
        url: URL?,
        symbol: String,
        choose: @escaping () -> Void,
        remove: @escaping () -> Void
    ) -> some View {
        HStack(spacing: 12) {
            Image(systemName: symbol)
                .font(.title2)
                .foregroundStyle(.tint)
                .frame(width: 34)
            VStack(alignment: .leading, spacing: 3) {
                Text(title).font(.headline)
                Text(url?.lastPathComponent ?? subtitle)
                    .font(.callout)
                    .foregroundStyle(url == nil ? Color.secondary : Color.primary)
                    .lineLimit(1)
                    .truncationMode(.middle)
            }
            Spacer()
            if url != nil {
                Button("Remove", action: remove)
                    .buttonStyle(.borderless)
            }
            Button(url == nil ? "Choose…" : "Change…", action: choose)
                .buttonStyle(.bordered)
        }
        .padding(.vertical, 5)
    }

    private func chooseAudio() {
        chooseFile(title: "Choose a lecture recording", extensions: audioExtensions) { audioURL = $0 }
    }

    private func choosePresentation() {
        chooseFile(title: "Choose lecture slides", extensions: slideExtensions) { presentationURL = $0 }
    }

    private func chooseFile(title: String, extensions: [String], completion: (URL?) -> Void) {
        let panel = NSOpenPanel()
        panel.title = title
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = false
        panel.allowedContentTypes = extensions.compactMap { UTType(filenameExtension: $0) }
        if panel.runModal() == .OK { completion(panel.url) }
    }

    private func acceptDroppedSources(_ urls: [URL]) -> Bool {
        let files = urls.filter(\.isFileURL)
        let recordings = files.filter { audioExtensions.contains($0.pathExtension.lowercased()) }
        let presentations = files.filter { slideExtensions.contains($0.pathExtension.lowercased()) }

        guard !recordings.isEmpty || !presentations.isEmpty else {
            droppedFileMessage = "Use an audio/video recording, PDF, or PowerPoint file."
            return false
        }
        guard recordings.count <= 1, presentations.count <= 1 else {
            droppedFileMessage = "Drop at most one recording and one slide deck at a time."
            return false
        }

        if let recording = recordings.first { audioURL = recording }
        if let presentation = presentations.first { presentationURL = presentation }
        let names = (recordings + presentations).map(\.lastPathComponent)
        droppedFileMessage = "Added \(names.joined(separator: " and ")) to this lecture task."
        return true
    }

    private var acceptedDropTypes: [UTType] {
        var types: [UTType] = [
            .fileURL, .audio, .movie, .pdf,
            .mpeg4Audio, .mp3, .wav,
            .mpeg4Movie, .quickTimeMovie
        ]
        types.append(contentsOf: audioExtensions.compactMap { UTType(filenameExtension: $0) })
        types.append(contentsOf: slideExtensions.compactMap { UTType(filenameExtension: $0) })
        return Array(Set(types))
    }

    private func receiveDroppedProviders(_ providers: [NSItemProvider]) -> Bool {
        DroppedItemProviderLoader.load(providers, acceptedTypes: acceptedDropTypes) { urls, errors in
            if !urls.isEmpty {
                _ = acceptDroppedSources(urls)
            } else if let error = errors.first {
                droppedFileMessage = "Could not receive the dropped file: \(error)"
            } else {
                droppedFileMessage = "Use an audio/video recording, PDF, or PowerPoint file."
            }
        }
    }
}
