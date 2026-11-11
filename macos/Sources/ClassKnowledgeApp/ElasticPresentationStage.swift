import AppKit
import SwiftUI

/// Hold the elastic pull throughout scrolling; commit at most one page after release.
struct ElasticPresentationStage<Content: View>: View {
    @Binding var index: Int
    let count: Int
    @ViewBuilder var content: () -> Content
    @State private var pull: CGFloat = 0
    private let threshold: CGFloat = 118

    var body: some View {
        ZStack {
            RoundedRectangle(cornerRadius: 14)
                .fill(Color.white)
            content()
                .padding(16)
                // Keep the rendered image mounted: replacing its identity exposed the matte.
                .transaction { $0.animation = nil }
                .offset(y: -resistance * 28)
                .scaleEffect(x: 1 - abs(resistance) * 0.012, y: 1 + abs(resistance) * 0.018)
                .allowsHitTesting(false)
        }
        .clipped()
        .background(WheelInput(onDelta: receive, onQuiet: settle, onCancel: cancelPull))
        .accessibilityLabel("Presentation page \(index + 1) of \(count). Scroll to turn pages.")
    }

    private var resistance: CGFloat {
        let fraction = min(abs(pull) / threshold, 1)
        let atEdge = (index == 0 && pull < 0) || (index == count - 1 && pull > 0)
        return (pull < 0 ? -1 : 1) * (1 - exp(-fraction * 1.2)) * (atEdge ? 0.25 : 1)
    }

    private func receive(_ delta: CGFloat) {
        guard abs(delta) > 0 else { return }
        pull = min(threshold * 1.2, max(-threshold * 1.2, pull + delta))
    }

    private func cancelPull() {
        withAnimation(.spring(response: 0.30, dampingFraction: 0.85)) { pull = 0 }
    }

    private func settle() {
        let step = pull > 0 ? 1 : -1
        guard abs(pull) >= threshold, (0..<count).contains(index + step) else {
            withAnimation(.spring(response: 0.42, dampingFraction: 0.7)) { pull = 0 }
            return
        }
        withAnimation(.spring(response: 0.30, dampingFraction: 0.92)) {
            pull = 0
            index += step
        }
    }
}

/// Bounds markers let the stage route input across its reader without capturing the sidebar.
struct PresentationScrollRegion: NSViewRepresentable {
    var isText = false
    func makeNSView(context: Context) -> PresentationRegionView { PresentationRegionView(isText: isText) }
    func updateNSView(_ view: PresentationRegionView, context: Context) { view.isText = isText }
}
final class PresentationRegionView: NSView {
    var isText: Bool
    init(isText: Bool) { self.isText = isText; super.init(frame: .zero) }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    override func hitTest(_ point: NSPoint) -> NSView? { nil }
}

/// Observe wheel events only inside this stage, including trackpad momentum.
private struct WheelInput: NSViewRepresentable {
    let onDelta: (CGFloat) -> Void
    let onQuiet: () -> Void
    let onCancel: () -> Void

    func makeNSView(context: Context) -> WheelView {
        let view = WheelView()
        view.onDelta = onDelta
        view.onQuiet = onQuiet
        view.onCancel = onCancel
        return view
    }
    func updateNSView(_ view: WheelView, context: Context) {
        view.onDelta = onDelta
        view.onQuiet = onQuiet
        view.onCancel = onCancel
        view.installMonitorIfNeeded()
    }
    static func dismantleNSView(_ view: WheelView, coordinator: ()) { view.stop() }

    final class WheelView: NSView {
        var onDelta: ((CGFloat) -> Void)?
        var onQuiet: (() -> Void)?
        var onCancel: (() -> Void)?
        private enum Route { case navigation, text }
        private var route: Route?
        private var momentumRoute: Route?
        private var monitor: Any?
        private var quietWork: DispatchWorkItem?

        override func viewDidMoveToWindow() {
            super.viewDidMoveToWindow()
            // Fullscreen reparenting can temporarily detach this view mid-gesture.
            // Keep its monitor and pending release until the representable is dismantled.
            installMonitorIfNeeded()
        }
        func installMonitorIfNeeded() {
            guard window != nil, monitor == nil else { return }
            monitor = NSEvent.addLocalMonitorForEvents(matching: .scrollWheel) { [weak self] event in
                guard let self else { return event }
                return self.handle(event)
            }
        }

        private func finish(cancelled: Bool = false) {
            quietWork?.cancel()
            let completedRoute = route
            route = nil
            momentumRoute = completedRoute
            if completedRoute == .navigation {
                if cancelled { onCancel?() } else { onQuiet?() }
            }
        }

        private func handle(_ event: NSEvent) -> NSEvent? {
            guard let ownerWindow = window, (ownerWindow.isKeyWindow || event.window === ownerWindow),
                  !isHiddenOrHasHiddenAncestor, let root = ownerWindow.contentView else { return event }

            // Complete an owned gesture even if its pointer has left the reader.
            if event.phase.contains(.cancelled) || event.phase.contains(.ended) {
                let owned = route == .navigation
                finish(cancelled: event.phase.contains(.cancelled))
                return owned ? nil : event
            }
            if !event.momentumPhase.isEmpty {
                return momentumRoute == .navigation ? nil : event
            }
            if event.phase.contains(.began) {
                quietWork?.cancel()
                if route == .navigation { onCancel?() }
                route = nil
                momentumRoute = nil
            }
            guard abs(event.scrollingDeltaY) > 0.001 else { return event }

            // Some devices omit began/ended. Pick a route on the first real delta.
            if route == nil {
                let point = root.convert(event.locationInWindow, from: nil)
                let markers = regions(in: root).filter { !$0.isHiddenOrHasHiddenAncestor && !$0.visibleRect.isEmpty }
                let stageCenter = convert(NSPoint(x: bounds.midX, y: bounds.midY), to: root)
                let readers = markers.filter { !$0.isText && $0.convert($0.bounds, to: root).contains(stageCenter) }
                let reader = readers.min { $0.bounds.width * $0.bounds.height < $1.bounds.width * $1.bounds.height }
                let area = reader.map { $0.convert($0.bounds, to: root) } ?? convert(visibleRect, to: root)
                guard area.contains(point) else { return event }
                let overStage = convert(bounds, to: root).contains(point)
                let overText = !overStage && markers.contains { $0.isText && $0.convert($0.visibleRect, to: root).contains(point) }
                route = overText && textCanScroll(root: root, point: point, delta: -event.scrollingDeltaY) ? .text : .navigation
                momentumRoute = nil
            }
            let owned = route == .navigation
            if owned {
                onDelta?(-event.scrollingDeltaY * (event.hasPreciseScrollingDeltas ? 1 : 14))
            }
            quietWork?.cancel()
            let work = DispatchWorkItem { [weak self] in self?.finish() }
            quietWork = work
            // Release phases are immediate; this watchdog recovers missing end events.
            let delay = event.phase.isEmpty ? 0.08 : 0.35
            DispatchQueue.main.asyncAfter(deadline: .now() + delay, execute: work)
            return owned ? nil : event
        }
        private func regions(in view: NSView) -> [PresentationRegionView] {
            var result = (view as? PresentationRegionView).map { [$0] } ?? []
            for child in view.subviews { result += regions(in: child) }
            return result
        }
        private func textCanScroll(root: NSView, point: NSPoint, delta: CGFloat) -> Bool {
            var target = root.hitTest(point)
            while let view = target {
                if let scroll = view as? NSScrollView, let document = scroll.documentView {
                    let visible = scroll.contentView.bounds
                    let maximum = max(0, document.bounds.height - visible.height)
                    guard maximum > 1 else { return false }
                    let position = document.isFlipped ? visible.minY : maximum - visible.minY
                    return delta > 0 ? position < maximum - 1 : position > 1
                }
                target = view.superview
            }
            // SwiftUI can host text scrolling without exposing an NSScrollView ancestor.
            return true
        }
        func stop() {
            if let monitor { NSEvent.removeMonitor(monitor) }
            monitor = nil
            quietWork?.cancel()
            route = nil
            momentumRoute = nil
        }
    }
}
