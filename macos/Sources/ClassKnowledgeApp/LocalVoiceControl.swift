import AVKit
import SwiftUI

/// Shared offline Piper narration control for slide notes and PDFs.
/// The model and text stay on the local machine; only the initial voice download uses the network.
struct LocalVoiceControl: View {
    @EnvironmentObject private var model: AppModel

    let contentID: String
    let buttonTitle: String
    let text: () -> String

    @State private var presenting = false
    @State private var options: VoiceOptions?
    @State private var chosenVoice = ""
    @State private var isLoading = false
    @State private var isInstalling = false
    @State private var isGenerating = false
    @State private var player: AVPlayer?
    @State private var outputURL: URL?
    @State private var requestID = UUID()
    @State private var errorMessage: String?

    var body: some View {
        Button {
            presenting = true
        } label: {
            Label(buttonTitle, systemImage: "speaker.wave.2")
        }
        .buttonStyle(.bordered)
        .popover(isPresented: $presenting, arrowEdge: .bottom) {
            VStack(alignment: .leading, spacing: 12) {
                Text("Offline narration")
                    .font(.headline)
                Text("Piper runs on this computer. The initial voice download requires internet access.")
                    .font(.caption)
                    .foregroundStyle(.secondary)

                if isLoading {
                    ProgressView("Checking installed voices…")
                }

                if let errorMessage {
                    Text(errorMessage)
                        .font(.caption)
                        .foregroundStyle(.red)
                        .textSelection(.enabled)
                }

                if let options {
                    if options.voices.isEmpty {
                        Text(options.setupMessage.isEmpty
                             ? "No Piper voices found in models/piper."
                             : options.setupMessage)
                            .font(.callout)
                            .textSelection(.enabled)

                        if !options.setupMessage.contains("runtime is not installed") {
                            Button {
                                Task { await installEnglishVoice() }
                            } label: {
                                if isInstalling {
                                    ProgressView().controlSize(.small)
                                } else {
                                    Label("Install English voice", systemImage: "arrow.down.circle")
                                }
                            }
                            .disabled(isInstalling)
                        } else {
                            Text("Run: .venv/bin/python -m pip install -r requirements.txt")
                                .font(.caption.monospaced())
                                .textSelection(.enabled)
                        }
                    } else {
                        Picker("Installed voice", selection: $chosenVoice) {
                            ForEach(options.voices, id: \.self) { voice in
                                Text(voice).tag(voice)
                            }
                        }
                        .pickerStyle(.menu)

                        Button {
                            Task { await generate() }
                        } label: {
                            if isGenerating {
                                ProgressView().controlSize(.small)
                            } else {
                                Label("Generate and play", systemImage: "play.fill")
                            }
                        }
                        .disabled(isGenerating || isInstalling || chosenVoice.isEmpty)

                        if let player {
                            NativeMediaPlayer(player: player)
                                .frame(height: 82)
                            Button("Stop and clear audio", systemImage: "stop.fill") {
                                clearAudio()
                            }
                        }
                        Text("Choose a voice matching the language of the displayed text.")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
            }
            .padding(16)
            .frame(width: 360)
            .task { await refreshVoices() }
        }
        .onChange(of: contentID) { _, _ in clearAudio() }
        .onChange(of: chosenVoice) { _, _ in clearAudio() }
        .onDisappear { clearAudio() }
    }

    private func refreshVoices() async {
        isLoading = true
        defer { isLoading = false }
        do {
            let received = try await model.availableVoices()
            options = received
            if !received.voices.contains(chosenVoice) {
                chosenVoice = received.voices.first ?? ""
            }
            errorMessage = nil
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func installEnglishVoice() async {
        isInstalling = true
        errorMessage = nil
        defer { isInstalling = false }
        do {
            let received = try await model.installDefaultVoice()
            options = received
            chosenVoice = received.voices.contains("en_US-lessac-medium")
                ? "en_US-lessac-medium" : (received.voices.first ?? "")
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func generate() async {
        let spoken = text().trimmingCharacters(in: .whitespacesAndNewlines)
        guard !spoken.isEmpty else {
            errorMessage = "This document has no readable text to narrate."
            return
        }
        // A single huge WAV would overwhelm the JSON bridge and macOS player.
        // Reject oversized requests explicitly instead of silently clipping speech.
        guard spoken.count <= 20_000 else {
            errorMessage = "This document contains \(spoken.count) characters. For reliable local playback, narrate a shorter section (up to 20,000 characters)."
            return
        }
        guard !chosenVoice.isEmpty else {
            errorMessage = "Install and select a Piper voice first."
            return
        }

        clearAudio()
        let currentRequest = UUID()
        requestID = currentRequest
        isGenerating = true
        errorMessage = nil
        defer { isGenerating = false }
        do {
            let url = try await model.synthesizeVoice(spoken, voice: chosenVoice)
            guard currentRequest == requestID else {
                try? FileManager.default.removeItem(at: url)
                return
            }
            outputURL = url
            let audioPlayer = AVPlayer(url: url)
            player = audioPlayer
            audioPlayer.play()
        } catch {
            if currentRequest == requestID {
                errorMessage = error.localizedDescription
            }
        }
    }

    private func clearAudio() {
        requestID = UUID()
        player?.pause()
        player = nil
        if let outputURL {
            try? FileManager.default.removeItem(at: outputURL)
            self.outputURL = nil
        }
    }
}
