import AppKit
import SwiftUI

struct NativeJobResultSheet: View {
    let detail: NativeJobDetail
    @Environment(\.dismiss) private var dismiss
    @State private var selectedArtifactID: String?
    @State private var text = ""
    @State private var loadError: String?
    @State private var saveMessage: String?

    private var selectedArtifact: NativeJobArtifact? {
        detail.artifacts.first(where: { $0.id == selectedArtifactID }) ?? detail.artifacts.first
    }

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                VStack(alignment: .leading, spacing: 3) {
                    Text(detail.title).font(.title3.weight(.semibold))
                    Text("Completed job results")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                Spacer()
                if let artifact = selectedArtifact {
                    Button("Show in Finder") {
                        NSWorkspace.shared.activateFileViewerSelecting([artifact.url])
                    }
                    Button("Save a Copy…") { saveCopy(artifact) }
                    Button("Open in Default App") { NSWorkspace.shared.open(artifact.url) }
                }
                Button("Done") { dismiss() }
                    .keyboardShortcut(.cancelAction)
            }
            .padding(16)
            Divider()
            if let saveMessage {
                Text(saveMessage)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal, 14)
                    .padding(.vertical, 7)
            }
            HStack(spacing: 0) {
                List(detail.artifacts) { artifact in
                    Button {
                        selectedArtifactID = artifact.id
                    } label: {
                        Label(artifact.title, systemImage: artifact.url.pathExtension.lowercased() == "pdf" ? "doc.richtext" : "doc.text")
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .listRowBackground(selectedArtifact?.id == artifact.id ? Color.accentColor.opacity(0.12) : .clear)
                }
                .listStyle(.sidebar)
                .frame(minWidth: 220, idealWidth: 255, maxWidth: 300)
                Divider()
                preview
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .frame(minWidth: 850, minHeight: 600)
        .task(id: selectedArtifact?.id) { loadText() }
        .onAppear { selectedArtifactID = detail.artifacts.first?.id }
    }

    @ViewBuilder private var preview: some View {
        if let artifact = selectedArtifact {
            if artifact.url.pathExtension.lowercased() == "pdf" {
                PDFDocumentView(url: artifact.url)
            } else if isText(artifact.url) {
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
            } else {
                QuickLookDocumentView(url: artifact.url)
            }
        } else {
            ContentUnavailableView(
                "No output files",
                systemImage: "doc.questionmark",
                description: Text(detail.error.isEmpty ? "This job did not produce a previewable file." : detail.error)
            )
        }
    }

    private func isText(_ url: URL) -> Bool {
        ["md", "markdown", "txt"].contains(url.pathExtension.lowercased())
    }

    private func loadText() {
        guard let artifact = selectedArtifact, isText(artifact.url) else {
            text = ""
            loadError = nil
            return
        }
        do {
            text = try String(contentsOf: artifact.url, encoding: .utf8)
            loadError = nil
        } catch {
            text = ""
            loadError = error.localizedDescription
        }
    }

    private func saveCopy(_ artifact: NativeJobArtifact) {
        let panel = NSSavePanel()
        panel.nameFieldStringValue = artifact.url.lastPathComponent
        panel.canCreateDirectories = true
        guard panel.runModal() == .OK, let destination = panel.url else { return }
        do {
            if FileManager.default.fileExists(atPath: destination.path) {
                try FileManager.default.removeItem(at: destination)
            }
            try FileManager.default.copyItem(at: artifact.url, to: destination)
            saveMessage = "Saved a copy to \(destination.path)"
        } catch {
            saveMessage = "Could not save the file: \(error.localizedDescription)"
        }
    }
}

struct FullLectureTranslationSheet: View {
    let queue: (String) -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var language = "Chinese (Simplified)"
    @State private var customLanguage = ""

    private let options = [
        "Chinese (Simplified)", "Chinese (Traditional)", "French", "Dutch", "German",
        "Spanish", "Japanese", "Korean", "Other language",
    ]

    var body: some View {
        VStack(alignment: .leading, spacing: 15) {
            Text("Translate finished notes")
                .font(.title2.weight(.semibold))
            Picker("Target language", selection: $language) {
                ForEach(options, id: \.self) { Text($0).tag($0) }
            }
            if language == "Other language" {
                TextField("Target language", text: $customLanguage, prompt: Text("e.g. Italian"))
                    .textFieldStyle(.roundedBorder)
            }
            Text("This queues a separate translation job and keeps the original English notes unchanged.")
                .font(.callout)
                .foregroundStyle(.secondary)
            HStack {
                Spacer()
                Button("Cancel") { dismiss() }
                Button("Queue translation") {
                    let target = language == "Other language"
                        ? customLanguage.trimmingCharacters(in: .whitespacesAndNewlines)
                        : language
                    queue(target)
                    dismiss()
                }
                .buttonStyle(.borderedProminent)
                .disabled(language == "Other language" && customLanguage.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
        }
        .padding(24)
        .frame(width: 430)
    }
}
