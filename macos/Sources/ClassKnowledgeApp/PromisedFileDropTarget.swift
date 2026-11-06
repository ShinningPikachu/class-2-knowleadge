import AppKit
import SwiftUI
import UniformTypeIdentifiers

/// An AppKit drop target for sources that export files as AppKit file promises.
struct PromisedFileDropTarget: NSViewRepresentable {
    @Binding var isTargeted: Bool
    var onFiles: ([URL]) -> Void
    var onError: (String) -> Void

    func makeNSView(context: Context) -> FilePromiseReceiverView {
        let view = FilePromiseReceiverView()
        updateCallbacks(on: view)
        return view
    }

    func updateNSView(_ nsView: FilePromiseReceiverView, context: Context) {
        updateCallbacks(on: nsView)
    }

    private func updateCallbacks(on view: FilePromiseReceiverView) {
        view.onFiles = onFiles
        view.onError = onError
        view.onTargeted = { targeted in
            isTargeted = targeted
        }
    }
}

final class FilePromiseReceiverView: NSView {
    var onFiles: (([URL]) -> Void)?
    var onError: ((String) -> Void)?
    var onTargeted: ((Bool) -> Void)?

    private static let filePromiseOperationQueue: OperationQueue = {
        let queue = OperationQueue()
        queue.name = "local.classknowledge.file-promise-receiver"
        queue.maxConcurrentOperationCount = 2
        return queue
    }()

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        registerForDraggedTypes(Self.readableDraggedTypes)
        wantsLayer = true
        layer?.backgroundColor = NSColor.clear.cgColor
    }

    required init?(coder: NSCoder) {
        super.init(coder: coder)
        registerForDraggedTypes(Self.readableDraggedTypes)
    }

    override func draggingEntered(_ sender: NSDraggingInfo) -> NSDragOperation {
        guard hasSupportedFiles(sender.draggingPasteboard) else { return [] }
        onTargeted?(true)
        return .copy
    }

    override func draggingUpdated(_ sender: NSDraggingInfo) -> NSDragOperation {
        hasSupportedFiles(sender.draggingPasteboard) ? .copy : []
    }

    override func draggingExited(_ sender: NSDraggingInfo?) {
        onTargeted?(false)
    }

    override func prepareForDragOperation(_ sender: NSDraggingInfo) -> Bool {
        hasSupportedFiles(sender.draggingPasteboard)
    }

    override func performDragOperation(_ sender: NSDraggingInfo) -> Bool {
        onTargeted?(false)

        let pasteboard = sender.draggingPasteboard
        let fileURLs = pasteboard.readObjects(
            forClasses: [NSURL.self],
            options: [.urlReadingFileURLsOnly: true]
        ) as? [URL] ?? []
        if !fileURLs.isEmpty {
            onFiles?(fileURLs)
            return true
        }

        let promises = pasteboard.readObjects(
            forClasses: [NSFilePromiseReceiver.self],
            options: nil
        ) as? [NSFilePromiseReceiver] ?? []

        guard !promises.isEmpty else { return false }

        do {
            let destination = FileManager.default.temporaryDirectory
                .appendingPathComponent("ClassKnowledgePromisedFiles", isDirectory: true)
                .appendingPathComponent(UUID().uuidString, isDirectory: true)
            try FileManager.default.createDirectory(at: destination, withIntermediateDirectories: true)

            for promise in promises {
                promise.receivePromisedFiles(
                    atDestination: destination,
                    options: [:],
                    operationQueue: Self.filePromiseOperationQueue
                ) { [weak self] fileURL, error in
                    DispatchQueue.main.async {
                        if let error {
                            self?.onError?("Could not receive the dropped file: \(error.localizedDescription)")
                        } else {
                            self?.onFiles?([fileURL])
                        }
                    }
                }
            }
            return true
        } catch {
            onError?("Could not prepare a location for the dropped file: \(error.localizedDescription)")
            return false
        }
    }

    override func concludeDragOperation(_ sender: NSDraggingInfo?) {
        onTargeted?(false)
    }

    private func hasSupportedFiles(_ pasteboard: NSPasteboard) -> Bool {
        if pasteboard.canReadObject(
            forClasses: [NSURL.self],
            options: [.urlReadingFileURLsOnly: true]
        ) {
            return true
        }
        return pasteboard.readObjects(
            forClasses: [NSFilePromiseReceiver.self],
            options: nil
        )?.isEmpty == false
    }

    private static var readableDraggedTypes: [NSPasteboard.PasteboardType] {
        var types = NSFilePromiseReceiver.readableDraggedTypes.map(NSPasteboard.PasteboardType.init(rawValue:))
        types.append(.fileURL)
        return Array(Set(types))
    }
}

enum DroppedItemProviderLoader {
    static func load(
        _ providers: [NSItemProvider],
        acceptedTypes: [UTType],
        completion: @escaping ([URL], [String]) -> Void
    ) -> Bool {
        let matches = providers.compactMap { provider -> (NSItemProvider, String, Bool)? in
            if provider.hasItemConformingToTypeIdentifier(UTType.fileURL.identifier) {
                return (provider, UTType.fileURL.identifier, true)
            }

            let registeredType = provider.registeredTypeIdentifiers.first { identifier in
                guard let type = UTType(identifier) else { return false }
                return acceptedTypes.contains { type == $0 || type.conforms(to: $0) }
            }
            guard let registeredType else { return nil }
            return (provider, registeredType, false)
        }

        guard !matches.isEmpty else { return false }

        let group = DispatchGroup()
        let lock = NSLock()
        var loadedURLs: [URL] = []
        var errors: [String] = []

        for (provider, typeIdentifier, isFileURL) in matches {
            group.enter()
            if isFileURL {
                provider.loadItem(forTypeIdentifier: typeIdentifier, options: nil) { item, error in
                    defer { group.leave() }
                    if let url = fileURL(from: item) {
                        lock.withLock { loadedURLs.append(url) }
                    } else {
                        let message = error?.localizedDescription ?? "The dropped item did not contain a file URL."
                        lock.withLock { errors.append(message) }
                    }
                }
            } else {
                provider.loadFileRepresentation(forTypeIdentifier: typeIdentifier) { sourceURL, error in
                    defer { group.leave() }
                    do {
                        guard let sourceURL else {
                            throw error ?? CocoaError(.fileReadUnknown)
                        }
                        let copiedURL = try copyTemporaryRepresentation(
                            sourceURL,
                            suggestedName: provider.suggestedName,
                            typeIdentifier: typeIdentifier
                        )
                        lock.withLock { loadedURLs.append(copiedURL) }
                    } catch {
                        lock.withLock { errors.append(error.localizedDescription) }
                    }
                }
            }
        }

        group.notify(queue: .main) {
            completion(loadedURLs, errors)
        }
        return true
    }

    private static func fileURL(from item: NSSecureCoding?) -> URL? {
        if let url = item as? URL { return url }
        if let url = item as? NSURL { return url as URL }
        if let data = item as? Data,
           let value = String(data: data, encoding: .utf8) {
            return URL(string: value.trimmingCharacters(in: .whitespacesAndNewlines))
        }
        if let value = item as? String {
            return URL(string: value.trimmingCharacters(in: .whitespacesAndNewlines))
        }
        return nil
    }

    private static func copyTemporaryRepresentation(
        _ sourceURL: URL,
        suggestedName: String?,
        typeIdentifier: String
    ) throws -> URL {
        let fileManager = FileManager.default
        let directory = fileManager.temporaryDirectory
            .appendingPathComponent("ClassKnowledgeDroppedFiles", isDirectory: true)
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try fileManager.createDirectory(at: directory, withIntermediateDirectories: true)

        let sourceExtension = sourceURL.pathExtension
        let inferredExtension = UTType(typeIdentifier)?.preferredFilenameExtension ?? ""
        let fileExtension = sourceExtension.isEmpty ? inferredExtension : sourceExtension
        let rawName = suggestedName?.trimmingCharacters(in: .whitespacesAndNewlines)
        var filename = (rawName?.isEmpty == false ? rawName! : sourceURL.lastPathComponent)
            .replacingOccurrences(of: "/", with: "-")
            .replacingOccurrences(of: ":", with: "-")
        if filename.isEmpty { filename = "Dropped file" }
        if URL(fileURLWithPath: filename).pathExtension.isEmpty && !fileExtension.isEmpty {
            filename += ".\(fileExtension)"
        }

        let destination = directory.appendingPathComponent(filename)
        try fileManager.copyItem(at: sourceURL, to: destination)
        return destination
    }
}
