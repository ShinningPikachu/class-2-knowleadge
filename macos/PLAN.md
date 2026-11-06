# Native macOS application plan

## Objective

Replace the Streamlit browser interface with a fully native macOS application while preserving the existing local library, lecture-processing, agent, and job-queue behavior. The existing Streamlit app remains available during the transition.

## Architecture

- **Native UI:** SwiftUI and AppKit controls, with native file panels, drag and drop, PDFKit/Quick Look previews, AVKit playback, menus, and macOS settings integration.
- **Existing domain layer:** Keep the Python library, Ollama/Codex agents, Whisper/PDF processing, voice synthesis, and background job manager. Avoid duplicating those rules in Swift.
- **Local IPC:** The Swift app launches a Python helper and exchanges newline-delimited JSON requests and responses over stdin/stdout. This avoids a browser, web server, open network port, and cloud dependency.
- **Workspace data:** Preserve the existing `library/`, `jobs/`, and `runs/` data. Support selecting a workspace root for installed app use; development defaults to the repository root.
- **Lifecycle:** Start and stop the backend with the app, recover interrupted jobs through the existing checkpoint recovery, and offer launch-at-login through macOS's supported login-item API.

## Delivery phases

1. **Foundation** — define the IPC protocol, start/stop the Python helper, add the SwiftUI app shell and build script, and prove the app can connect to the existing workspace.
2. **Library** — native subject and folder navigation; import, search, rename, move, and delete; file previews for text, PDF, slides, and audio; lecture links, translations, deep slide review, and voice controls.
3. **Agent** — native activation/provider/reasoning controls, cited conversation history, and review/confirm/cancel for proposed library changes.
4. **Lecture Notes** — native file selection and drag-and-drop for recordings and slide decks, destination selection, priority, and lecture task submission.
5. **Job Queue** — live status and progress, task logs and artifacts, safe defer/cancel/resume/retry/priority changes, and Ollama model controls.
6. **App integration** — persistent settings/workspace selection, preferences, accessibility and keyboard behavior, launch at login, app bundle, and installation guidance.
7. **Verification** — build the native app, exercise each workflow against a disposable workspace, run existing backend tests, and verify local-only behavior and restart recovery.

## Completion criteria

- The app opens as a native macOS window without opening a browser.
- All four workspaces and their existing user-visible capabilities are available from native controls.
- The app reads and updates the existing project data without destructive migration.
- Background jobs, their progress, and safe recovery continue to work when the window closes or the app restarts.
- The project includes reproducible build/run instructions and a distributable `.app` build path.
- Swift and Python verification pass, with any environmental limitation clearly recorded.

## Progress

- [x] Confirm current implementation is Streamlit backed by reusable Python domain services.
- [x] Select native SwiftUI frontend plus a private local Python child process as the implementation path.
- [x] Build native app shell and JSONL IPC foundation.
- [x] Implement Library, Agent, Lecture Notes, and Job Queue workflows in native views.
- [x] Add workspace selection, editable processing settings, result previews, local voice playback, and launch-at-login controls.
- [x] Build and verify an ad-hoc signed `.app` bundle.
- [x] Verify the backend and native UI against a disposable workspace; run the existing Python test suite.

## Verification record

- `./scripts/build_macos_app.sh` completed and `codesign --verify --deep --strict` accepted the resulting bundle.
- SwiftPM compiled the full SwiftUI app using the installed macOS Command Line Tools SDK.
- `python -m unittest discover -s tests -v` passed all 137 existing tests.
- `src/native_backend.py` passed `py_compile` and a disposable-workspace JSONL smoke covering startup, settings, Library create/import/rename/move/delete, agent activation, and shutdown.
- The app opened a native window, connected to an isolated workspace, created a subject, imported and previewed a Markdown file, and displayed completed-job output and translation controls without launching a browser.
- A synthetic lecture fixture resolved its slide note and aligned transcript passage through the backend's lecture-bundle path.

## Delivery limits

- The bundle is ad-hoc signed for local use. Full Xcode is absent, so archive, notarization, and App Store signing are not available in this environment.
- The app uses the project's `.venv`, Python source, cached Whisper weights, and separately installed Ollama/Piper components; it is not a self-contained installer.
- **Open Class Knowledge when I sign in** is available in Settings and uses `SMAppService`; it must be enabled from the app on the user's Mac. macOS opens it after sign-in, and queued processing runs while the app is open.

## Installed-app delivery follow-up

- [x] Add a staged installer for `~/Applications/Class Knowledge.app`, including signature verification and rollback if bundle replacement fails.
- [x] Add first-launch workspace onboarding for app copies outside the repository and persist the selected workspace for later launches.
- [x] Install a signed build in the standard user Applications folder; `codesign --verify --deep --strict` passed.
- [x] Exercise workspace selection and persistence across relaunch using a disposable app copy and isolated workspace.
- [x] Make the startup toggle reflect the actual `SMAppService` status, including macOS approval and missing-app states.
- [x] Launch the installed app against the selected workspace, enable sign-in startup, and verify it remains enabled after relaunch.

The latest build is installed at `~/Applications/Class Knowledge.app` and verified with `codesign --verify --deep --strict`. In the installed app, the startup toggle showed **Login item enabled**; after quitting and reopening the app, it reconnected to the saved workspace and still showed the toggle enabled. A read-only query of `jobs/jobs.sqlite3` found 30 completed, 4 cancelled, 4 failed, and 1 deferred job, with no queued or running jobs. No job was started by this verification.

## Usability review

- [x] Rename the navigation group to **Study tools** and use **Queued**/**Active** consistently in queue summaries and filters.
- [x] Show labeled **Import** and **New Folder** actions, wrap long subject names, and provide direct **Create Subject** and **Import Files** actions in empty states.
- [x] Add an **Activate agent** action to the inactive-agent state and clarify when Return searches document text.
- [x] Protect workspace switching from stale backend-process termination callbacks; switching to a disposable workspace and back now reconnects cleanly.
- [x] Build and install the updated app, then verify the Library empty states and the Agent and Queue labels in the running native UI.

The empty Library states were checked in a disposable workspace under `/private/tmp`; the installed app was then restored to the project workspace. The real workspace still reports the same queue totals and no queued or running work.

## Current environment constraints

- Swift 6.4 and the macOS 26.5 SDK are available through Command Line Tools. `xcodebuild` is unavailable because full Xcode is not installed; SwiftPM builds are the available verification route.
- Distribution will still need a documented Python runtime/dependency setup because this project depends on large local media/ML libraries and Ollama is installed separately.
