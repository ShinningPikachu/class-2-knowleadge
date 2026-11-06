import Foundation

@MainActor
final class BackendClient {
    private var process: Process?
    private var input: Pipe?
    private var output: Pipe?
    private var readBuffer = Data()
    private var nextID = 1
    private var processGeneration = 0
    private var pending: [Int: CheckedContinuation<Data, Error>] = [:]

    private(set) var projectRoot: URL?

    func start(projectRoot: URL) throws {
        guard process?.isRunning != true else { return }
        let python = projectRoot.appending(path: ".venv/bin/python")
        guard FileManager.default.isExecutableFile(atPath: python.path) else {
            throw NativeBackendError.pythonEnvironmentMissing(python.path)
        }

        let script = projectRoot.appending(path: "src/native_backend.py")
        guard FileManager.default.fileExists(atPath: script.path) else {
            throw NativeBackendError.workspaceNotFound
        }

        let child = Process()
        let childInput = Pipe()
        let childOutput = Pipe()
        processGeneration += 1
        let generation = processGeneration
        child.executableURL = python
        child.arguments = ["-u", script.path, "--project-root", projectRoot.path]
        child.currentDirectoryURL = projectRoot
        child.environment = ProcessInfo.processInfo.environment.merging(
            ["PYTHONUNBUFFERED": "1"],
            uniquingKeysWith: { _, new in new }
        )
        child.standardInput = childInput
        child.standardOutput = childOutput
        child.standardError = FileHandle.standardError
        child.terminationHandler = { [weak self] child in
            let message = "Python helper stopped (exit code \(child.terminationStatus))."
            Task { @MainActor in
                guard let self, self.processGeneration == generation else { return }
                self.failPending(message)
                self.clearProcessState()
            }
        }

        do {
            try child.run()
        } catch {
            throw NativeBackendError.processFailed("Could not start the Python helper: \(error.localizedDescription)")
        }

        self.process = child
        self.input = childInput
        self.output = childOutput
        self.projectRoot = projectRoot
        childOutput.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty else { return }
            Task { @MainActor in
                guard let self, self.processGeneration == generation else { return }
                self.consume(data)
            }
        }
    }

    func call<T: Decodable>(
        _ method: String,
        params: [String: Any] = [:],
        as type: T.Type = T.self
    ) async throws -> T {
        let data = try await callRaw(method, params: params)
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        do {
            return try decoder.decode(type, from: data)
        } catch {
            throw NativeBackendError.processFailed("Could not read the \(method) response: \(error.localizedDescription)")
        }
    }

    func stop() async {
        let stoppingGeneration = processGeneration
        if process?.isRunning == true {
            _ = try? await callRaw("system.shutdown", params: [:])
        }
        guard processGeneration == stoppingGeneration else { return }
        processGeneration += 1
        input?.fileHandleForWriting.closeFile()
        if process?.isRunning == true { process?.terminate() }
        clearProcessState()
    }

    private func clearProcessState() {
        output?.fileHandleForReading.readabilityHandler = nil
        process = nil
        input = nil
        output = nil
        projectRoot = nil
        readBuffer = Data()
    }

    private func callRaw(_ method: String, params: [String: Any]) async throws -> Data {
        guard process?.isRunning == true, let input else {
            throw NativeBackendError.processFailed("The Python helper is not running.")
        }
        let id = nextID
        nextID += 1
        let request: [String: Any] = ["id": id, "method": method, "params": params]
        let encoded: Data
        do {
            encoded = try JSONSerialization.data(withJSONObject: request, options: [.fragmentsAllowed])
        } catch {
            throw NativeBackendError.processFailed("Could not encode the \(method) request: \(error.localizedDescription)")
        }

        return try await withCheckedThrowingContinuation { continuation in
            pending[id] = continuation
            input.fileHandleForWriting.write(encoded + Data([0x0A]))
        }
    }

    private func consume(_ data: Data) {
        readBuffer.append(data)
        while let newline = readBuffer.firstRange(of: Data([0x0A])) {
            let line = Data(readBuffer[..<newline.lowerBound])
            readBuffer.removeSubrange(..<newline.upperBound)
            guard !line.isEmpty else { continue }
            handle(line)
        }
    }

    private func handle(_ line: Data) {
        guard
            let response = try? JSONSerialization.jsonObject(with: line) as? [String: Any],
            let id = response["id"] as? Int,
            let continuation = pending.removeValue(forKey: id)
        else { return }

        if let error = response["error"] as? [String: Any],
           let message = error["message"] as? String {
            continuation.resume(throwing: NativeBackendError.remote(message))
            return
        }
        guard let result = response["result"] else {
            continuation.resume(throwing: NativeBackendError.malformedResponse)
            return
        }
        do {
            let resultData = try JSONSerialization.data(withJSONObject: result, options: [.fragmentsAllowed])
            continuation.resume(returning: resultData)
        } catch {
            continuation.resume(throwing: NativeBackendError.malformedResponse)
        }
    }

    private func failPending(_ message: String) {
        let outstanding = pending.values
        pending.removeAll()
        for continuation in outstanding {
            continuation.resume(throwing: NativeBackendError.processFailed(message))
        }
    }
}
