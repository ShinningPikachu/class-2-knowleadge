import AppKit
import AVKit
import PDFKit
import SwiftUI

struct NativeMediaPlayer: NSViewRepresentable {
    let player: AVPlayer

    func makeNSView(context: Context) -> AVPlayerView {
        let view = AVPlayerView()
        view.player = player
        view.controlsStyle = .inline
        view.videoGravity = .resizeAspect
        return view
    }

    func updateNSView(_ view: AVPlayerView, context: Context) {
        if view.player !== player { view.player = player }
        view.controlsStyle = .inline
        view.videoGravity = .resizeAspect
    }

    static func dismantleNSView(_ view: AVPlayerView, coordinator: ()) {
        view.player?.pause()
        view.player = nil
    }
}

struct LectureSlidesPreview: View {
    @EnvironmentObject private var model: AppModel
    let document: LibraryDocument
    let bundle: NativeLectureBundle
    @State private var slideIndex = 0
    @State private var language = "English"
    @State private var translationSlide: NativeLectureSlide?
    @State private var narrationPlayer: AVPlayer?
    @State private var isNarrating = false

    private var slide: NativeLectureSlide {
        bundle.slides[min(max(slideIndex, 0), max(bundle.slides.count - 1, 0))]
    }

    private var languages: [String] {
        ["English"] + slide.translations.keys.sorted()
    }

    private var note: String {
        guard language != "English" else { return slide.summary }
        return slide.translations[language] ?? slide.summary
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 13) {
                HStack(spacing: 10) {
                    Button {
                        slideIndex = max(0, slideIndex - 1)
                    } label: { Image(systemName: "chevron.left") }
                    .disabled(slideIndex == 0)
                    Text("Slide \(slide.slide) of \(bundle.slides.count)")
                        .font(.callout.weight(.semibold))
                        .frame(minWidth: 110)
                    Button {
                        slideIndex = min(bundle.slides.count - 1, slideIndex + 1)
                    } label: { Image(systemName: "chevron.right") }
                    .disabled(slideIndex >= bundle.slides.count - 1)
                    Spacer()
                    Button {
                        Task { await narrate() }
                    } label: {
                        if isNarrating { ProgressView().controlSize(.small) }
                        else { Label("Speak", systemImage: "speaker.wave.2") }
                    }
                    .disabled(isNarrating || note.isEmpty)
                    Button("Translate…") { translationSlide = slide }
                        .disabled(bundle.sourceJob == nil || slide.summary.isEmpty)
                    Button {
                        Task { await model.enqueueSlideReview(slide.slide) }
                    } label: {
                        Label("Deep review", systemImage: "brain.head.profile")
                    }
                    .disabled(bundle.sourceJob == nil || isReviewActive)
                }
                .buttonStyle(.bordered)

                if document.fileURL.pathExtension.lowercased() == "pdf" {
                    PDFDocumentView(url: document.fileURL, pageNumber: slide.slide)
                        .frame(height: 330)
                        .clipShape(RoundedRectangle(cornerRadius: 8))
                } else if !slide.previewImage.isEmpty,
                          let image = NSImage(contentsOfFile: slide.previewImage) {
                    Image(nsImage: image)
                        .resizable()
                        .scaledToFit()
                        .frame(maxWidth: .infinity)
                        .frame(height: 330)
                } else {
                    VStack(alignment: .leading, spacing: 8) {
                        Text(slide.title).font(.title3.weight(.semibold))
                        Text(slide.content.isEmpty ? "No extractable slide text." : slide.content)
                            .textSelection(.enabled)
                    }
                    .padding(16)
                    .frame(maxWidth: .infinity, minHeight: 210, alignment: .topLeading)
                    .background(.quaternary.opacity(0.55), in: RoundedRectangle(cornerRadius: 9))
                }

                VStack(alignment: .leading, spacing: 8) {
                    HStack(alignment: .firstTextBaseline) {
                        VStack(alignment: .leading, spacing: 3) {
                            Text(slide.title).font(.headline)
                            Text("Slide and lecture note")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                        Spacer()
                        Picker("Note language", selection: $language) {
                            ForEach(languages, id: \.self) { Text($0).tag($0) }
                        }
                        .labelsHidden()
                        .frame(maxWidth: 170)
                    }
                    Text(note.isEmpty ? "A combined slide-and-lecture note has not been generated for this slide." : note)
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                .padding(14)
                .background(Color(nsColor: .controlBackgroundColor), in: RoundedRectangle(cornerRadius: 9))

                if !slide.paragraphs.isEmpty {
                    DisclosureGroup("Teacher explanation · \(slide.paragraphs.count) passages") {
                        VStack(alignment: .leading, spacing: 9) {
                            ForEach(Array(slide.paragraphs.enumerated()), id: \.offset) { _, paragraph in
                                VStack(alignment: .leading, spacing: 3) {
                                    if let start = paragraph.startTime, !start.isEmpty {
                                        Text(start + (paragraph.endTime.map { "–\($0)" } ?? ""))
                                            .font(.caption.monospacedDigit())
                                            .foregroundStyle(.secondary)
                                    }
                                    Text(paragraph.text).textSelection(.enabled)
                                }
                            }
                        }
                        .padding(.top, 8)
                    }
                    .padding(12)
                    .background(.quaternary.opacity(0.4), in: RoundedRectangle(cornerRadius: 8))
                }

                reviewSection
                ForEach(slide.translationJobs) { job in
                    HStack {
                        Text("\(job.language) translation")
                        Spacer()
                        Text("\(job.status.capitalized) · \(job.progress)%")
                            .foregroundStyle(.secondary)
                    }
                    .font(.caption)
                }
                if let narrationPlayer {
                    NativeMediaPlayer(player: narrationPlayer)
                        .frame(height: 82)
                }
            }
            .padding(12)
        }
        .sheet(item: $translationSlide) { selectedSlide in
            SlideTranslationSheet(slide: selectedSlide) { target in
                Task { await model.enqueueSlideTranslation(selectedSlide, targetLanguage: target) }
            }
        }
        .onChange(of: slideIndex) { _, _ in
            if !languages.contains(language) { language = "English" }
        }
    }

    private var review: NativeDeepReview? { slide.deepReview }
    private var isReviewActive: Bool { ["queued", "running", "waiting", "deferred"].contains(review?.status ?? "") }

    @ViewBuilder private var reviewSection: some View {
        if let review, review.status == "completed", !review.text.isEmpty {
            DisclosureGroup("Detailed explanation") {
                Text(review.text)
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.top, 8)
            }
            .padding(12)
            .background(.quaternary.opacity(0.4), in: RoundedRectangle(cornerRadius: 8))
        } else if let review, isReviewActive {
            HStack {
                ProgressView(value: Double(review.progress), total: 100)
                Text("Deep review · \(review.status) · \(review.progress)%")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        } else if let review, review.status == "failed" {
            Text("Deep review failed: \(review.message)")
                .font(.caption)
                .foregroundStyle(.red)
        }
    }

    private func narrate() async {
        isNarrating = true
        defer { isNarrating = false }
        let spoken = "Slide \(slide.slide). \(slide.title). \(note)"
        do { narrationPlayer = AVPlayer(url: try await model.synthesizeVoice(spoken)) }
        catch { model.errorMessage = error.localizedDescription }
    }
}

private struct SlideTranslationSheet: View {
    let slide: NativeLectureSlide
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
            Text("Translate Slide \(slide.slide)")
                .font(.title2.weight(.semibold))
            Picker("Target language", selection: $language) {
                ForEach(options, id: \.self) { Text($0).tag($0) }
            }
            if language == "Other language" {
                TextField("Target language", text: $customLanguage, prompt: Text("e.g. Italian"))
                    .textFieldStyle(.roundedBorder)
            }
            Text("Only this slide’s combined English note will be translated.")
                .font(.callout)
                .foregroundStyle(.secondary)
            HStack {
                Spacer()
                Button("Cancel") { dismiss() }
                Button("Queue translation") {
                    queue(language == "Other language" ? customLanguage.trimmingCharacters(in: .whitespacesAndNewlines) : language)
                    dismiss()
                }
                .buttonStyle(.borderedProminent)
                .disabled(language == "Other language" && customLanguage.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
        }
        .padding(24)
        .frame(width: 410)
    }
}

struct AudioTranscriptView: View {
    let url: URL
    let paragraphs: [NativeTranscriptParagraph]
    let isVideo: Bool
    @State private var player: AVPlayer
    @State private var currentSeconds = 0.0
    @State private var observer: Any?

    init(url: URL, paragraphs: [NativeTranscriptParagraph], isVideo: Bool) {
        self.url = url
        self.paragraphs = paragraphs
        self.isVideo = isVideo
        _player = State(initialValue: AVPlayer(url: url))
    }

    private var activeParagraphID: String? {
        paragraphs.first(where: { currentSeconds >= $0.startSeconds && currentSeconds < max($0.endSeconds, $0.startSeconds + 1) })?.id
    }

    var body: some View {
        VStack(spacing: 8) {
            NativeMediaPlayer(player: player)
                .frame(height: isVideo ? 220 : 82)
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 3) {
                        ForEach(Array(paragraphs.enumerated()), id: \.offset) { _, paragraph in
                            Button {
                                player.seek(to: CMTime(seconds: paragraph.startSeconds, preferredTimescale: 600))
                                player.play()
                            } label: {
                                HStack(alignment: .top, spacing: 10) {
                                    Text(paragraph.startTime ?? Self.formatTime(paragraph.startSeconds))
                                        .font(.caption.monospacedDigit())
                                        .foregroundStyle(.secondary)
                                        .frame(width: 62, alignment: .leading)
                                    Text(paragraph.text)
                                        .font(.callout)
                                        .foregroundStyle(.primary)
                                        .frame(maxWidth: .infinity, alignment: .leading)
                                }
                                .padding(9)
                                .background(activeParagraphID == paragraph.id ? Color.accentColor.opacity(0.13) : .clear, in: RoundedRectangle(cornerRadius: 7))
                            }
                            .buttonStyle(.plain)
                            .id(paragraph.id)
                        }
                    }
                    .padding(8)
                }
                .onChange(of: activeParagraphID) { _, newValue in
                    guard let newValue else { return }
                    withAnimation(.easeInOut(duration: 0.2)) { proxy.scrollTo(newValue, anchor: .center) }
                }
            }
        }
        .padding(9)
        .onAppear {
            guard observer == nil else { return }
            observer = player.addPeriodicTimeObserver(
                forInterval: CMTime(seconds: 0.5, preferredTimescale: 600), queue: .main
            ) { time in currentSeconds = time.seconds }
        }
        .onDisappear {
            if let observer { player.removeTimeObserver(observer) }
            observer = nil
        }
    }

    private static func formatTime(_ seconds: Double) -> String {
        guard seconds.isFinite, seconds >= 0 else { return "00:00" }
        let total = Int(seconds)
        return String(format: "%02d:%02d", total / 60, total % 60)
    }
}

struct PDFVoiceControl: View {
    @EnvironmentObject private var model: AppModel
    let document: LibraryDocument
    @State private var isGenerating = false
    @State private var narrationPlayer: AVPlayer?

    var body: some View {
        VStack(spacing: 8) {
            HStack {
                Text("Local narration")
                    .font(.caption.weight(.medium))
                    .foregroundStyle(.secondary)
                Spacer()
                Button {
                    Task { await generate() }
                } label: {
                    if isGenerating { ProgressView().controlSize(.small) }
                    else { Label("Read PDF aloud", systemImage: "speaker.wave.2") }
                }
                .disabled(isGenerating)
            }
            if let narrationPlayer {
                NativeMediaPlayer(player: narrationPlayer)
                    .frame(height: 72)
            }
        }
        .padding(9)
    }

    private func generate() async {
        isGenerating = true
        defer { isGenerating = false }
        guard let pdf = PDFDocument(url: document.fileURL) else {
            model.errorMessage = "Could not read this PDF."
            return
        }
        let text = (0..<pdf.pageCount).compactMap { pdf.page(at: $0)?.string }.joined(separator: "\n\n")
        do { narrationPlayer = AVPlayer(url: try await model.synthesizeVoice(text, maximumCharacters: 20_000)) }
        catch { model.errorMessage = error.localizedDescription }
    }
}
