# Native macOS app

The native client is a SwiftUI app. It starts `src/native_backend.py` as a private Python child process and talks to it over newline-delimited JSON on stdin/stdout. It does not open a browser or listen on a network port.

## Install for daily use

Install Python dependencies in the project's `.venv` as described in the root README, then install a stable app copy in `~/Applications`:

```sh
./scripts/install_macos_app.sh
```

On first launch, choose the project folder containing `app.py`, `src/`, and `.venv/`. The workspace path is saved in the app's preferences so later launches and the login item can find the Python helper after the app is installed outside the checkout. To switch later, choose **Settings → Choose Workspace**. Existing `library/`, `jobs/`, and `runs/` data stay in that workspace.

In **Settings → App startup**, enable **Open Class Knowledge when I sign in** to register the installed app as a macOS login item. macOS opens it after sign-in; it does not run before a user signs in. Processing runs while the app is open, and unfinished tasks are recovered into **Later** if the app quits or macOS restarts.

## Local text-to-speech

Piper is part of `requirements.txt`. With the project's Python environment installed, open a processed lecture's slide, click **Speak**, and select **Install English voice** in the popover (one-time model download). Choose an installed voice and click **Generate and play**. The PDF reader offers the same controls. Speech synthesis itself is offline, and the voice files remain in the local ignored `models/piper/` directory.

You can also install voices manually from the repository root:

```sh
source .venv/bin/activate
python -m piper.download_voices en_US-lessac-medium --data-dir models/piper
# View the available models and pick a matching voice for translated notes:
python -m piper.download_voices
```

Each voice must have both its `.onnx` weights and matching `.onnx.json` configuration file. Models appear in the picker when the popover is reopened. The first voice download needs internet; subsequent speech generation uses local computation and no remote API. For a fresh clone, install the Python requirements and download the voice again because the `models/` directory is excluded from Git.

## Build and run from this checkout

To build a checkout `.app` in the temporary build cache without installing it, run:

```sh
./scripts/build_macos_app.sh
open "${TMPDIR:-/private/tmp}/class-knowledge-native-build/Class Knowledge.app"
```

The checkout build is labeled **Class Knowledge Dev** and uses separate macOS preferences. The installed daily-use app remains **Class Knowledge**.

The installer accepts `CLASS_KNOWLEDGE_INSTALL_DIR` to choose a different destination. Use `--no-open` to install without launching, which is useful for verification:

```sh
CLASS_KNOWLEDGE_INSTALL_DIR=/tmp/class-knowledge-apps ./scripts/install_macos_app.sh --no-open
```

Full Xcode is not installed in the current environment, so the Swift package is built with SwiftPM and the installed Command Line Tools SDK. See [`PLAN.md`](PLAN.md) for the implementation scope and verification record.
