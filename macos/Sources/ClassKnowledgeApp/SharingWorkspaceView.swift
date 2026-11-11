import SwiftUI

struct SharingWorkspaceView: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 22) {
                VStack(alignment: .leading, spacing: 6) {
                    Text("Share your library")
                        .font(.largeTitle.weight(.semibold))
                    Text("Prepare course materials in this repository for sharing through GitHub.")
                        .foregroundStyle(.secondary)
                }

                if let status = model.sharingStatus {
                    summary(status)
                    readiness(status)
                    largeFiles(status)
                    instructions(status)
                } else {
                    ProgressView("Checking repository contents…")
                }
            }
            .padding(24)
            .frame(maxWidth: 900, alignment: .leading)
        }
        .background(Color(nsColor: .windowBackgroundColor))
        .task { await model.refreshSharing() }
    }

    private func summary(_ status: SharingStatus) -> some View {
        GroupBox("Repository content") {
            LazyVGrid(columns: [GridItem(.adaptive(minimum: 150), alignment: .leading)], alignment: .leading, spacing: 14) {
                metric("Subjects", value: "\(status.subjectCount)")
                metric("Library documents", value: "\(status.documentCount)")
                metric("Total files", value: "\(status.totalFileCount)")
                metric("Content size", value: status.totalDisplaySize)
            }
            .padding(.top, 8)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private func metric(_ title: String, value: String) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(value).font(.title2.weight(.semibold))
            Text(title).font(.callout).foregroundStyle(.secondary)
        }
    }

    @ViewBuilder
    private func readiness(_ status: SharingStatus) -> some View {
        GroupBox("GitHub readiness") {
            VStack(alignment: .leading, spacing: 10) {
                Text("The repository includes the subject library, files in audio/ and pdf/, and reviewed notes. Processing runs, queues, models, and local settings remain local.")
                    .fixedSize(horizontal: false, vertical: true)
                if !status.gitAvailable {
                    Label("Git was not found for this workspace.", systemImage: "xmark.circle.fill")
                        .foregroundStyle(.red)
                }
                if status.blockedFileCount > 0 {
                    Label("\(status.blockedFileCount) file(s) over 100 MiB need Git LFS before GitHub can accept them.", systemImage: "exclamationmark.triangle.fill")
                        .foregroundStyle(.orange)
                } else if status.largeFileCount > 0 {
                    Label("\(status.largeFileCount) file(s) are over 50 MiB; audio and video use Git LFS.", systemImage: "checkmark.circle.fill")
                        .foregroundStyle(.secondary)
                }
                if status.largeFileCount > 0 && !status.gitLfsInstalled {
                    Label("Git LFS is not installed. Install it and run `git lfs install` before staging recordings.", systemImage: "exclamationmark.triangle.fill")
                        .foregroundStyle(.orange)
                }
                if let pendingCount = status.pendingFileCount {
                    Text("\(pendingCount) shareable file(s) are not yet tracked by Git.")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.top, 6)
        }
    }

    @ViewBuilder
    private func largeFiles(_ status: SharingStatus) -> some View {
        if !status.largeFiles.isEmpty {
            GroupBox("Large files") {
                VStack(alignment: .leading, spacing: 7) {
                    ForEach(status.largeFiles) { file in
                        HStack(alignment: .firstTextBaseline) {
                            Text(file.displaySize)
                                .monospacedDigit()
                                .foregroundStyle(.secondary)
                                .frame(width: 75, alignment: .trailing)
                            Text(file.path)
                                .textSelection(.enabled)
                                .lineLimit(1)
                                .truncationMode(.middle)
                        }
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.top, 6)
            }
        }
    }

    private func instructions(_ status: SharingStatus) -> some View {
        VStack(alignment: .leading, spacing: 16) {
            GroupBox("Prepare the catalog") {
                VStack(alignment: .leading, spacing: 12) {
                    Text("Refresh after adding or renaming course files. This checkpoints the library database and creates a GitHub-friendly list without copying files.")
                        .fixedSize(horizontal: false, vertical: true)
                    Button {
                        Task { await model.prepareSharing() }
                    } label: {
                        if model.isPreparingSharing {
                            ProgressView().controlSize(.small)
                        } else {
                            Label("Refresh share index", systemImage: "arrow.clockwise")
                        }
                    }
                    .disabled(model.isPreparingSharing || !status.gitAvailable)
                    Text(status.indexPath)
                        .font(.system(.caption, design: .monospaced))
                        .textSelection(.enabled)
                        .foregroundStyle(.secondary)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.top, 6)
            }

            GroupBox("Finish in Terminal") {
                VStack(alignment: .leading, spacing: 10) {
                    Text("Review the files before committing. Git LFS is required to upload and download the recordings.")
                        .fixedSize(horizontal: false, vertical: true)
                    Text("git lfs install\ngit status --short\ngit add -A\ngit commit -m \"Share course library\"\ngit push")
                        .font(.system(.callout, design: .monospaced))
                        .textSelection(.enabled)
                        .padding(10)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(.quaternary, in: RoundedRectangle(cornerRadius: 8))
                    Text("For selected friends, keep the GitHub repository private and invite them.")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                    if status.largeFileCount > 0 && !status.gitLfsInstalled {
                        Text("On macOS with Homebrew: brew install git-lfs, then run git lfs install once.")
                            .font(.callout)
                            .foregroundStyle(.orange)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.top, 6)
            }
        }
    }
}
