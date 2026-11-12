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
    let fullScreen: Bool
    @State private var slideIndex = 0
    @State private var language = "English"
    @State private var translationSlide: NativeLectureSlide?
    @State private var currentSlideArtwork: NSImage?
    @State private var artworkCache: [Int: NSImage] = [:]
    @State private var slidePDFDocument: PDFDocument?
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

    private var noteContent: String {
        note.isEmpty ? slide.content : note
    }

    private var noteHeading: String {
        if note.isEmpty { return "Slide content" }
        return bundle.sourceJob == nil ? "Slide explanation" : "Slide and lecture note"
    }

    var body: some View {
        Group {
            if fullScreen {
                fullScreenSlideReader
            } else {
                VStack(alignment: .leading, spacing: 13) {
                    slideControls()
                    presentationStage.frame(height: 330)
                    Text("Scroll to turn pages · scroll the explanation to read")
                        .font(.caption).foregroundStyle(.secondary)
                    ScrollView {
                        slideDetails(fullScreen: false)
                    }
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                    .background(PresentationScrollRegion(isText: true))
                }
                .padding(12)
            }
        }
        .background(PresentationScrollRegion())
        .sheet(item: $translationSlide) { selectedSlide in
            SlideTranslationSheet(slide: selectedSlide) { target in
                Task { await model.enqueueSlideTranslation(selectedSlide, targetLanguage: target) }
            }
        }
        .onChange(of: slideIndex) { _, _ in
            if !languages.contains(language) { language = "English" }
        }
        .onAppear {
            loadCurrentSlideArtwork()
        }
    }

    private var fullScreenSlideReader: some View {
        GeometryReader { geometry in
            HStack(spacing: 0) {
                VStack(spacing: 10) {
                    slideControls().padding(.horizontal, 14).padding(.top, 14)
                    presentationStage.padding(12)
                }
                .frame(width: geometry.size.width * 0.62)
                Divider()
                ScrollView {
                    slideDetails(fullScreen: true)

                        .padding(22)
                }
                .frame(width: geometry.size.width * 0.38)
                .background(PresentationScrollRegion(isText: true))
            }
        }
    }

    private var presentationStage: some View {
        ElasticPresentationStage(index: Binding(get: { slideIndex }, set: { selectSlide($0) }), count: bundle.slides.count) {
            slideArtwork(for: slide, fullScreen: true)
        }
    }

    private func slideControls(scrollTo: ((Int) -> Void)? = nil) -> some View {
        HStack(spacing: 10) {
            Button {
                guard slideIndex > 0 else { return }
                selectSlide(slideIndex - 1, scrollTo: scrollTo)
            } label: { Image(systemName: "chevron.left") }
            .disabled(slideIndex == 0)
            Text("Slide \(slide.slide) of \(bundle.slides.count)")
                .font(.callout.weight(.semibold))
                .monospacedDigit()
                .frame(minWidth: 110)
            Button {
                guard slideIndex < bundle.slides.count - 1 else { return }
                selectSlide(slideIndex + 1, scrollTo: scrollTo)
            } label: { Image(systemName: "chevron.right") }
            .disabled(slideIndex >= bundle.slides.count - 1)
            Spacer()
            LocalVoiceControl(
                contentID: "\(document.id):\(slide.slide):\(language):\(noteContent)",
                buttonTitle: "Speak",
                text: { "Slide \(slide.slide). \(slide.title). \(noteContent)" }
            )
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
    }

    private func selectSlide(_ targetIndex: Int, scrollTo: ((Int) -> Void)? = nil) {
        guard bundle.slides.indices.contains(targetIndex), targetIndex != slideIndex else { return }
        withAnimation(.spring(response: 0.46, dampingFraction: 0.86)) {
            currentSlideArtwork = artworkImage(for: bundle.slides[targetIndex])
            slideIndex = targetIndex
        }
        DispatchQueue.main.async { preloadNeighbors() }
    }

    private func loadCurrentSlideArtwork() {
        currentSlideArtwork = artworkImage(for: slide)
        preloadNeighbors()
    }

    private func preloadNeighbors() {
        for neighbor in [slideIndex - 1, slideIndex + 1] where bundle.slides.indices.contains(neighbor) {
            _ = artworkImage(for: bundle.slides[neighbor])
        }
        let retained = Set([slideIndex - 1, slideIndex, slideIndex + 1].filter { bundle.slides.indices.contains($0) }.map { bundle.slides[$0].slide })
        artworkCache = artworkCache.filter { retained.contains($0.key) }
    }

    private func artworkImage(for page: NativeLectureSlide) -> NSImage? {
        if let cached = artworkCache[page.slide] { return cached }
        let image: NSImage?
        if !page.previewImage.isEmpty, let preview = NSImage(contentsOfFile: page.previewImage) {
            image = preview
        } else if document.fileURL.pathExtension.lowercased() == "pdf" {
            if slidePDFDocument == nil { slidePDFDocument = PDFDocument(url: document.fileURL) }
            image = slidePDFDocument?.page(at: max(0, page.slide - 1))?.thumbnail(of: NSSize(width: 1200, height: 900), for: .cropBox)
        } else {
            image = nil
        }
        if let image { artworkCache[page.slide] = image }
        return image
    }

    @ViewBuilder
    private func slideArtwork(for page: NativeLectureSlide, fullScreen: Bool) -> some View {
        if fullScreen, let image = currentSlideArtwork, page.slide == slide.slide {
            Image(nsImage: image)
                .resizable()
                .scaledToFit()
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        } else if fullScreen,
                  document.fileURL.pathExtension.lowercased() == "pdf",
                  let pdfPage = PDFDocument(url: document.fileURL)?.page(at: max(0, page.slide - 1)) {
            Image(nsImage: pdfPage.thumbnail(of: NSSize(width: 1200, height: 900), for: .cropBox))
                .resizable()
                .scaledToFit()
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .clipShape(RoundedRectangle(cornerRadius: 8))
        } else if document.fileURL.pathExtension.lowercased() == "pdf" {
            PDFDocumentView(url: document.fileURL, pageNumber: page.slide)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .frame(height: fullScreen ? nil : 330)
                .clipShape(RoundedRectangle(cornerRadius: 8))
        } else if !page.previewImage.isEmpty,
                  let image = NSImage(contentsOfFile: page.previewImage) {
            Image(nsImage: image)
                .resizable()
                .scaledToFit()
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        } else {
            VStack(alignment: .leading, spacing: 8) {
                Text(page.title).font(.title3.weight(.semibold))
                Text(page.content.isEmpty ? "No extractable slide text." : page.content)
                    .textSelection(.enabled)
            }
            .padding(16)
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
            .background(.quaternary.opacity(0.55), in: RoundedRectangle(cornerRadius: 9))
        }
    }

    private func slideDetails(fullScreen: Bool) -> some View {
        VStack(alignment: .leading, spacing: 13) {
            VStack(alignment: .leading, spacing: 8) {
                HStack(alignment: .firstTextBaseline) {
                    VStack(alignment: .leading, spacing: 3) {
                        Text(slide.title)
                            .font(.system(size: fullScreen ? 27 : 18, weight: .semibold))
                        Text(noteHeading)
                            .font(.system(size: fullScreen ? 15 : 12))
                            .foregroundStyle(.secondary)
                    }
                    Spacer()
                    Picker("Note language", selection: $language) {
                        ForEach(languages, id: \.self) { Text($0).tag($0) }
                    }
                    .labelsHidden()
                    .frame(maxWidth: 170)
                }
                Text(noteContent.isEmpty ? "No selectable text is available for this slide." : noteContent)
                    .font(.system(size: fullScreen ? 23 : 17))
                    .lineSpacing(fullScreen ? 6 : 0)
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
                                Text(paragraph.text)
                                    .font(.system(size: fullScreen ? 19 : 16))
                                    .textSelection(.enabled)
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
    let document: LibraryDocument

    var body: some View {
        HStack {
            Text("Local narration")
                .font(.caption.weight(.medium))
                .foregroundStyle(.secondary)
            Spacer()
            LocalVoiceControl(
                contentID: "\(document.id):\(document.fileURL.path)",
                buttonTitle: "Read PDF aloud",
                text: {
                    guard let pdf = PDFDocument(url: document.fileURL) else { return "" }
                    return (0..<pdf.pageCount)
                        .compactMap { pdf.page(at: $0)?.string }
                        .joined(separator: "\n\n")
                }
            )
        }
        .padding(9)
    }
}
