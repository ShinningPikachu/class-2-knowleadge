import SwiftUI

struct AgentWorkspaceView: View {
    @EnvironmentObject private var model: AppModel
    @State private var prompt = ""
    @State private var scope = "all"

    private var isActive: Bool { model.snapshot?.agentActive == true }
    private var isCodex: Bool { model.settings.agentProvider == "codex" }

    var body: some View {
        VStack(spacing: 0) {
            controls
            Divider()
            if !isActive {
                ContentUnavailableView {
                    Label("Agent is off", systemImage: "sparkles")
                } description: {
                    Text("Activate it to ask questions about your library and get cited answers.")
                } actions: {
                    Button {
                        Task { await model.setAgentActive(true) }
                    } label: {
                        Label("Activate agent", systemImage: "power")
                    }
                    .buttonStyle(.borderedProminent)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                conversation
            }
            Divider()
            composer
        }
        .onChange(of: model.settings.agentProvider) { _, _ in
            Task {
                try? await model.saveSettings()
                if isActive { await model.setAgentActive(true) }
            }
        }
        .onChange(of: model.settings.agentReasoningLevel) { _, _ in updateActiveConfig() }
        .onChange(of: model.settings.codexReasoningEffort) { _, _ in updateActiveConfig() }
        .onChange(of: scope) { _, _ in }
    }

    private var controls: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .center) {
                VStack(alignment: .leading, spacing: 3) {
                    Text("Library Agent")
                        .font(.largeTitle.weight(.semibold))
                    Text("Ask about indexed course materials. Answers include source citations.")
                        .foregroundStyle(.secondary)
                }
                Spacer()
                Toggle("Activate agent", isOn: Binding(
                    get: { isActive },
                    set: { active in Task { await model.setAgentActive(active) } }
                ))
                .toggleStyle(.switch)
                .labelsHidden()
                Text(isActive ? "On" : "Off")
                    .font(.callout.weight(.medium))
            }
            HStack(spacing: 14) {
                Picker("Provider", selection: $model.settings.agentProvider) {
                    Text("Local Ollama").tag("ollama")
                    Text("Codex").tag("codex")
                    Text("DeepSeek Harness").tag("dsh")
                }
                .frame(width: 200)
                if isCodex {
                    Picker("Reasoning", selection: $model.settings.codexReasoningEffort) {
                        Text("Low").tag("low")
                        Text("Medium").tag("medium")
                        Text("High").tag("high")
                        Text("Extra high").tag("xhigh")
                    }
                    .frame(width: 170)
                    Picker("Model", selection: $model.settings.codexModel) {
                        Text("Account default").tag("")
                        Text("GPT-5.3 Codex").tag("gpt-5.3-codex")
                    }
                    .frame(width: 190)
                } else {
                    Picker("Reasoning", selection: $model.settings.agentReasoningLevel) {
                        Text("Light").tag("light")
                        Text("Balanced").tag("balanced")
                        Text("Deep").tag("deep")
                    }
                    .frame(width: 170)
                    Text("Model: \(model.settings.llmModel)")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }
                Spacer()
            }
        }
        .padding(20)
    }

    private var conversation: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 16) {
                    if model.agentMessages.isEmpty {
                        ContentUnavailableView(
                            "Ask a question",
                            systemImage: "text.bubble",
                            description: Text("Try asking about a concept in your subject materials.")
                        )
                        .padding(.top, 70)
                    }
                    ForEach(model.agentMessages) { message in
                        AgentMessageView(message: message)
                            .id(message.id)
                    }
                    if let action = model.pendingAgentAction {
                        PendingAgentActionCard(
                            action: action,
                            confirm: { Task { await model.confirmAgentAction() } },
                            cancel: model.cancelAgentAction
                        )
                        .id("pending-action")
                    }
                    if model.isAgentResponding {
                        HStack(spacing: 10) {
                            ProgressView().controlSize(.small)
                            Text("Working with local sources…")
                                .foregroundStyle(.secondary)
                        }
                        .padding(.leading, 12)
                    }
                }
                .padding(22)
                .frame(maxWidth: 900, alignment: .leading)
                .frame(maxWidth: .infinity)
            }
            .onChange(of: model.agentMessages.count) { _, _ in
                if let last = model.agentMessages.last { proxy.scrollTo(last.id, anchor: .bottom) }
            }
        }
    }

    private var composer: some View {
        HStack(alignment: .bottom, spacing: 12) {
            Picker("Scope", selection: $scope) {
                Text("All subjects").tag("all")
                ForEach(model.snapshot?.subjects ?? []) { subject in
                    Text(subject.name).tag(subject.id)
                }
            }
            .frame(width: 180)
            TextField("Ask a question or request a simple library action", text: $prompt, axis: .vertical)
                .textFieldStyle(.roundedBorder)
                .lineLimit(1...5)
                .onSubmit(send)
                .disabled(!isActive || model.isAgentResponding)
            Button(action: send) {
                Image(systemName: "arrow.up.circle.fill")
                    .font(.title2)
            }
            .buttonStyle(.plain)
            .disabled(prompt.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || !isActive || model.isAgentResponding)
            .help("Send question")
        }
        .padding(16)
    }

    private func send() {
        let request = prompt
        guard !request.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }
        prompt = ""
        Task { await model.askAgent(request, scope: scope) }
    }

    private func updateActiveConfig() {
        guard isActive else { return }
        Task { await model.setAgentActive(true) }
    }
}

private struct AgentMessageView: View {
    let message: AgentMessage
    private var isUser: Bool { message.role == "user" }

    var body: some View {
        HStack(alignment: .top, spacing: 11) {
            Image(systemName: isUser ? "person.crop.circle.fill" : "sparkles")
                .font(.title3)
                .foregroundStyle(isUser ? Color.secondary : Color.accentColor)
                .frame(width: 24)
            VStack(alignment: .leading, spacing: 9) {
                Text(isUser ? "You" : "Class Knowledge")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.secondary)
                Text(message.content)
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
                if !message.sources.isEmpty {
                    DisclosureGroup("Sources used (\(message.sources.count))") {
                        VStack(alignment: .leading, spacing: 12) {
                            ForEach(message.sources) { source in
                                VStack(alignment: .leading, spacing: 4) {
                                    Text("[\(source.marker)] \(source.citation)")
                                        .font(.callout.weight(.semibold))
                                    Text(source.text)
                                        .font(.callout)
                                        .foregroundStyle(.secondary)
                                        .textSelection(.enabled)
                                }
                            }
                        }
                        .padding(.top, 8)
                    }
                    .font(.callout)
                    .padding(.top, 2)
                }
            }
            .padding(13)
            .background(isUser ? Color.accentColor.opacity(0.08) : Color(nsColor: .controlBackgroundColor), in: RoundedRectangle(cornerRadius: 12))
            Spacer(minLength: 20)
        }
    }
}

private struct PendingAgentActionCard: View {
    let action: PendingAgentAction
    let confirm: () -> Void
    let cancel: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Label("Review proposed change", systemImage: "exclamationmark.bubble")
                .font(.headline)
            Text(action.message.isEmpty ? action.confirmationTitle : action.message)
                .textSelection(.enabled)
            Text("The library changes only after you confirm this action.")
                .font(.caption)
                .foregroundStyle(.secondary)
            HStack {
                Button("Cancel", action: cancel)
                Button("Confirm action", action: confirm)
                    .buttonStyle(.borderedProminent)
            }
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.orange.opacity(0.10), in: RoundedRectangle(cornerRadius: 12))
    }
}
