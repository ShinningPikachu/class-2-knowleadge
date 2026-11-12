# Class Knowledge Library

An offline-first subject library, document assistant, lecture-note generator, and background job queue tuned for a 32 GB local machine. The application has five workspaces:

- **Library** — use a desktop-style two-pane file explorer with a folder sidebar and icon grid, drag local files directly onto the subject root or any folder to upload them, drag existing file tiles into any folder to move them, recognize file types from simple PDF/audio/Markdown/PowerPoint icons, click files to preview their contents, double-click a file to open its full-page view in a new browser tab, remove or rename files and folders, and search extracted text from the magnifier field at the top. Full-page PDF and PowerPoint views place one page or slide on the left and its text on the right; processed lecture decks show the combined slide and teacher explanation there. Other files use the same full-page route. A lecture recording opens with its cleaned, synchronized transcript timeline; class slides and concise notes are linked as one lecture set. Slide summaries and PDF pages can be explained aloud through an offline local voice, and lecture summary PDFs can also be played as one narration.
- **Agent** — explicitly activate a grounded assistant to ask questions across every subject or within one selected subject, with document/page/slide citations. Choose private, offline Ollama with a compact Light/Balanced/Deep selector, or an installed and signed-in Codex CLI for an optional cloud-backed provider. Codex is found automatically; select the account default or GPT-5.3 Codex, then choose Low, Medium, High, or Extra high reasoning. Codex receives only the question and retrieved excerpts, runs in an empty read-only workspace, and cannot edit the library. It can list, rename, and move documents or create a subject through conversational requests; every write action is previewed and requires confirmation.
- **Lecture Notes** — drag a recording from Voice Memos or Finder and/or a slide deck (`.pdf`, `.pptx`, or `.ppt`) into the source area, or choose files, then queue them for background processing. The learner-facing recording, class deck, concise notes PDF, and a general summary PDF are saved automatically into one lecture folder. Slide explanations, slide-to-transcript links, and cleaned transcripts are saved under the library's `derived` folder and load in the combined previews after a fresh clone.
- **Job Queue** — see what is planned, running, waiting, saved for later, completed, failed, or cancelled; stop active work safely; resume it from checkpoints with a new priority; restart a failed task with the same saved parameters; open a live processing log; preview or download stored transcripts; request an on-demand translation of finished notes; and inspect or unload Ollama models.
- **Share** — review repository content and Git LFS readiness, checkpoint the library catalog, and generate a readable GitHub index.

Lecture tasks are selected by priority (`High`, `Normal`, then `Low`) and creation time. **Stop safely · do later** is distinct from permanent cancellation: Whisper finishes its current chunk and stores the partial transcript, while an in-flight Qwen response finishes and is checkpointed before the task moves to **Later**. **Resume from checkpoints** returns it to the priority queue and reuses the same run folder, completed transcript chunks, cleanup batches, final transcript, and every completed slide note. If the application itself restarts during a task, that task is also recovered into **Later** instead of being marked failed. If a cleanup response fails its safety checks twice, that batch is retried automatically in smaller ordered pieces; a later retry of that cleanup failure also resumes its verified checkpoints. Whisper transcription can run while the interactive Qwen agent is active. Transcript cleanup and lecture-note generation use Qwen, so they wait between model calls while the interactive agent remains activated, then resume automatically after the agent is deactivated. Automatic model cleanup is enabled by default. Qwen stays warm while the interactive agent is activated, and lecture jobs hand resident models directly to queued work that uses the same Ollama host and model. Models are unloaded after completion, deferral, failure, cancellation, agent deactivation, or restart recovery only when no active or queued consumer still needs them. This releases unused model memory without causing avoidable unload/reload cycles or terminating the Ollama server.

Lecture processing uses one bounded, non-thinking Qwen call per slide. Every slide produces one concise study note that combines the essential slide content with the teacher's relevant aligned explanation. Repeated ideas appear once, and the note paraphrases instead of copying slide bullets or transcribing the recording. Detailed reasoning runs only from the **🧠** action beside an individual slide in the Library. That focused job uses high reasoning and a second factual audit, writes a separate PDF back into the lecture folder, and never overwrites the baseline notes.

English is the default recording and canonical-output language. Transcript repair and lecture-note generation explicitly remain in English; they never translate automatically. After a lecture finishes, expand **Translate finished notes on demand**, choose Chinese or another target language, and queue a separate translation task. Only that explicit action creates translated Markdown/PDF files. The English transcript and notes remain unchanged, and translation batches support the same safe stop and checkpoint-resume workflow.

Subject/folder metadata and extracted text are stored in `library/library.sqlite3`; original files live under stable subject and lecture-folder IDs in `library/subjects/`. Queue state and the complete timestamped processing timeline for each task are persisted in `jobs/jobs.sqlite3`. PDF pages, PowerPoint slides, Markdown, text, CSV, JSON, and related text formats are searchable immediately. Other file types are preserved and marked as stored until a suitable processor is available.

Two-hour recordings are split into overlapping chunks and transcribed by up to two bounded local workers. Every completed chunk updates an atomic `transcript.partial.json`, so an interrupted or failed task retains readable timestamped text in addition to its individual chunk checkpoints. Whisper is released before the writing model loads.

It reconstructs the class from both sources: the slide text/structure and the professor's timestamped explanation. No cloud API keys are used and the application only contacts the local Ollama endpoint (`127.0.0.1` by default).

## Architecture

```text
Audio/video ──> bundled PyAV decoder ─> overlapping chunks ─> faster-whisper large-v3
                                                              ├─> transcript.raw.json
                                                              └─> per-chunk checkpoints
                                                                         │
                                              grounded local Qwen cleanup ─> transcript.json
                                                                         └─> cleanup checkpoints
                                                       │
Slides PDF/PPTX ──> PyMuPDF/python-pptx ─> slides.json ├─> local semantic alignment
                         │                             │       │
                         └─> local Tesseract OCR        │       └─> alignment.json
                                                             
slides + cleaned transcript ──> Ollama embeddings ──> ChromaDB (per-run, on disk)
                                                           │
strictly filtered evidence ──> bounded Ollama baseline ──> hierarchical final notes + PDF
                                      └─> optional per-slide deep reasoning + factual review
```

The pipeline is deliberately simple and inspectable:

1. `src/audio_processor.py` uses faster-whisper's bundled **PyAV** decoder and **large-v3** model to produce timestamped speech in `transcript.raw.json`. The default 30-minute cores have 15 seconds of overlap; midpoint filtering prevents duplicated text while preserving boundary context. Up to two chunks run concurrently by default and are checkpointed in chronological order. Each checkpoint also rebuilds `transcript.partial.json`; the Job Queue log screen previews and downloads that file while processing and retains it after interruption. MP3, WAV, M4A, AAC, FLAC, OGG, OPUS, MP4, MOV, MKV, WebM, and M4V are supported.
2. `src/transcript_cleaner.py` sends timestamped paragraph batches plus slide-subject context to the configured local Qwen model. It repairs readability and removes clearly unrelated personal conversation, background discussion, greetings, and off-topic speech while retaining uncertain or substantive lecture material. Removed paragraph IDs and reasons are audited in the cleaned JSON; the untouched raw transcript is never overwritten.
3. `src/pdf_processor.py` uses **PyMuPDF** for PDFs and **python-pptx** for PPTX. It preserves slide number, inferred title, text, PowerPoint speaker notes, embedded image paths, best-effort local OCR text, and a slide preview. Independent slide OCR tasks run in a bounded pool of up to four workers, with slide order preserved and only a small number of slides queued at once. LibreOffice supplies faithful PowerPoint previews when installed; a readable local fallback is produced otherwise.
4. `src/alignment.py` maps each cleaned transcript paragraph to a slide. Spoken references such as “slide 8” have priority. Otherwise it selects a slide through local Ollama embedding cosine similarity. Any weak match is marked `temporal_fallback` in `alignment.json` rather than being disguised as a reliable mapping.
5. `src/embeddings.py` chunks both sources and `src/rag.py` uses **LangChain's Chroma integration** to store their **local Ollama vectors** in a persistent **ChromaDB** directory. Metadata keeps `source`, `slide_number`, aligned slide, transcript paragraph ID, and timestamps.
6. `src/agent.py` retrieves evidence restricted to the current slide and stores one integrated slide-and-teacher note in `slide_summaries.json`, together with source-coverage metadata. It then creates a separate general lecture summary from every completed note. Fast baseline mode disables thinking, makes one call, and caps the response; deep mode enables high reasoning and a second factual audit. Long per-slide transcripts and the final lecture synthesis use hierarchical map-reduce, so later material is not silently truncated. A final quality gate requires exactly one non-empty, bounded note per source section and verifies its source-coverage record before export.
7. `src/quality.py` measures alignment reliability and structural completeness. If more than 70% of speech requires low-confidence temporal alignment, the run stops instead of exporting potentially misleading notes.
8. `src/exporter.py` writes canonical Markdown and renders a local PDF with ReportLab—no online conversion service or browser dependency.
9. `app.py` is the Streamlit interface. It supports browser uploads and direct local paths for multi-gigabyte recordings, displays live stages, and exposes every intermediate artifact.

## Project layout

```text
class-2-knowleadge/
├── app.py
├── requirements.txt
├── library/                # shareable subject catalog and original files
├── jobs/                   # created at runtime; persistent queue and staged inputs
├── models/                 # optional local model files
├── audio/                  # shareable standalone recordings
├── pdf/                    # shareable standalone slide decks
├── reviewed/               # shareable reviewed notes and revisions
├── database/               # reserved local Chroma workspace
├── runs/                   # created at runtime; one auditable folder per lecture
└── src/
    ├── audio_processor.py
    ├── transcript_cleaner.py
    ├── translator.py
    ├── pdf_processor.py
    ├── alignment.py
    ├── embeddings.py
    ├── rag.py
    ├── agent.py
    ├── exporter.py
    ├── pipeline.py
    ├── quality.py
    ├── library.py          # subject/document catalog, extraction, and local search
    ├── library_agent.py    # source-grounded library Q&A
    ├── jobs.py             # priority queue, safe defer/resume, cancellation, and Qwen coordination
    ├── lecture_library.py  # completed-run handoff to a subject
    ├── ui/                 # focused Library, Agent, Lecture, and Share page modules
    └── config.py
```

## Installation

### 1. Create the Python environment

```bash
cd class-2-knowleadge
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Python 3.9 or newer is supported by the pinned dependency range currently used by the project; Python 3.11 is recommended for the smoothest installation and performance. Python 3.9 installs urllib3 1.26 because Apple's system build uses LibreSSL, which urllib3 2 does not support.

For OCR of text embedded in diagrams and screenshots, install the optional local Tesseract binary (`brew install tesseract` or `sudo apt install tesseract-ocr`). The application still works without it; only visual-text hints are skipped.

For broad Unicode coverage in PDF exports, optionally place `NotoSans-Regular.ttf` and `NotoSans-Bold.ttf` in `models/`. The exporter also checks common macOS and Linux Unicode-font locations, then falls back to built-in Helvetica.

For legacy `.ppt` upload, install LibreOffice locally. PDF and `.pptx` do not require LibreOffice.

Each slide has a **Note language** selector showing English and only the languages already translated for that slide. Existing translated lecture notes are discovered from the lecture run and Library folder and linked by slide number, including Chinese `幻灯片` headings. New translations can be requested through the 🌐 control; their language becomes selectable after completion. Translation Markdown is kept as an internal backing artifact, while translated Markdown and PDF files stay hidden from the learner-facing file list. Repeated requests for the same lecture, slide, and language reuse the existing job. Your selected language persists as you navigate the lecture. A slide without that translation shows English temporarily; the selected language returns on the next slide where it is available. The original slide image remains beside the translated combined note. Audio controls read the combined note; long narration is generated in segments and joined without clipping.

Voice explanations use **Piper**, a local neural ONNX text-to-speech engine, rather than operating-system speech services. After installing dependencies, open any 🔊 control in the Library and choose **Install English neural voice** once; it downloads the default `en_US-lessac-medium` model into `models/piper/`. You can also add a Piper `.onnx` model and its matching `.onnx.json` configuration to that directory; it appears automatically in the voice picker. Once a model is present, narration is completely offline. Full text is generated in validated segments and joined into one standard PCM WAV; if a segment is truncated, the app retries it in smaller pieces. The player reports the resulting duration and source character count. Generated audio is held only in the current browser session unless you choose to download it. Empty, silent, incomplete, and implausibly short audio is rejected.

### 2. Install and start Ollama locally

Install Ollama for your operating system, then make the two default local models available:

```bash
ollama pull qwen3.5:27b
ollama pull nomic-embed-text
ollama serve
```

`qwen3.5:27b` is the 32 GB quality-profile writing model; `nomic-embed-text` is a compact local embedding model used for alignment and RAG. Whisper is released before Ollama loads the 27B model. Both names remain editable in the UI. If Ollama is already running as a service, omit `ollama serve`.

### 3. Cache faster-whisper before going air-gapped

The application uses `local_files_only=True`; it never downloads Whisper weights during lecture processing. Populate the local cache explicitly once during installation:

```bash
python -c "from faster_whisper import download_model; download_model('large-v3')"
```

Alternatively, download the CTranslate2 model into `models/` and paste that local filesystem path into **faster-whisper model or local path** in the UI. Once Python packages and model weights are present, normal use has no network dependency.

On a 32 GB RAM machine, `large-v3` with `device=auto` and `compute_type=float32` is the quality-first default. On macOS, faster-whisper normally runs on the CPU (it does not use Apple GPU acceleration). The default two parallel transcription workers reduce elapsed time without changing the Whisper model, precision, or decoding settings. Use one worker only if the machine becomes memory-constrained; use an available CUDA device with `float16` for a materially faster transcription path.

## Run the app

```bash
cd class-2-knowleadge
source .venv/bin/activate
streamlit run app.py
```

Then open the local URL Streamlit prints. The server is explicitly bound to `127.0.0.1` so the local-file controls are not exposed to other machines by default.

## Share course materials through GitHub

The **Share** workspace prepares the repository's content for sharing. The subject database, original documents, and portable lecture artifacts in `library/`, plus loose source files in `audio/`, `pdf/`, and reviewed notes in `reviewed/`, are included in Git. Processing runs, job queues, generated caches, local models, and machine-specific app settings stay local. Use **Refresh share index** after library changes to checkpoint the SQLite catalog and update [`SHARE_INDEX.md`](SHARE_INDEX.md), a readable catalog with links to the files.

For each completed lecture, `library/subjects/<subject-id>/derived/<lecture-folder-id>/` contains `slides.json`, `slide_summaries.json`, `alignment.json`, `transcript.json`, `transcript.txt`, and, for new runs, the original `transcript.raw.json`. The folder ID links these files to the deck and recording in `library.sqlite3`. Each slide number links a slide, its explanation, and its aligned transcript paragraphs. Commit new files in `derived/` when sharing newly processed lectures. To backfill from local completed runs, run `python3 scripts/export_lecture_artifacts.py` and refresh the share index. **Privacy:** raw Whisper transcripts may contain off-topic or private speech removed during cleanup. Review them carefully before committing them to GitHub, especially in a public repository. Previously published lectures need a local run archive to backfill the raw transcript; it cannot be reconstructed from cleaned text.

Lecture audio and video use Git LFS rules in [`.gitattributes`](.gitattributes). Install Git LFS before staging recordings:

```bash
brew install git-lfs
git lfs install
git status --short
```

Review the changed files, then commit and push the repository as usual. Friends who clone it should have Git LFS enabled to download the media. For invited friends, set the GitHub repository to private and invite them. Files above 100 MiB are blocked in ordinary Git and need Git LFS; GitHub Free currently includes 10 GiB of LFS storage and 10 GiB of monthly download bandwidth. See [GitHub's large-file guidance](https://docs.github.com/en/repositories/working-with-files/managing-large-files/about-large-files-on-github) and [Git LFS billing](https://docs.github.com/en/billing/concepts/product-billing/git-lfs) for current limits and usage.

## Validate and regenerate older lecture notes

After cloning the repository, install Git LFS and the Python environment, then validate the downloaded material:

```bash
git lfs pull
python3 scripts/validate_library.py --hashes
python3 scripts/migrate_source_provenance.py
python3 scripts/rebuild_legacy_lectures.py
```

The first command downloads actual recordings. The validator detects missing originals, incomplete LFS checkouts, inconsistent slide links, missing source coverage and suspicious chronology. The migration and rebuild commands start as **dry runs**.

Older summaries and alignments were created before sequence-aware slide matching. To repair metadata only, run `python3 scripts/migrate_source_provenance.py --apply`. This reconstructs evidence-availability flags and retains a private backup, but does not improve the underlying text or fix a misaligned recording.

To regenerate the actual notes for a chosen lecture, first install the local Ollama and Whisper models listed above. Copy the lecture folder ID shown by the dry run, and execute:

```bash
python3 scripts/rebuild_legacy_lectures.py --folder YOUR_FOLDER_ID --execute
```

An explicit `--all --execute` option processes every detected legacy recorded lecture. The rebuild runs the full audio, slide extraction, chronological alignment, explanation and PDF pipeline. Existing derived artifacts and PDFs are backed up under ignored `runs/rebuild_backups/`. Updated Notes.pdf and Summary.pdf keep their library document IDs. This process uses local compute resources and is **not performed by a Git clone or a pull request merge**. Inspect results and privacy-sensitive transcript text before committing the newly regenerated artifacts.

## Native macOS app

The native Library's **Speak** control on a lecture slide and **Read PDF aloud** control open an offline Piper voice picker. If no voice is installed, select **Install English voice** to download the default voice into the ignored `models/piper/` directory. Select an installed model, then **Generate and play**; a standard macOS audio player provides playback controls. Playback stops when you switch slides or leave the reader. The current slide's displayed translation is used for speech, so select a Piper voice appropriate for its language. The same picker is available for PDFs; large documents take longer to synthesize. No API keys or hosted speech services are involved. This requires the existing project's Python `.venv` with `piper-tts` installed.


The SwiftUI macOS app presents the Library, Agent, Lecture Notes, Job Queue, Share, and Settings in a native window without starting a browser. It uses the same Python services, local models, and project data through a private child process. Use the expand button in any Library preview to open that file full screen. Slide decks show slides on the left and their lecture explanation or extracted text on the right; scroll the slide column and the text follows the slide that is mostly visible. Full-screen explanation text is larger; other file types open in their full-window preview. Build a checkout app in the temporary build cache and open it from the repository root:

```bash
./scripts/build_macos_app.sh
open "${TMPDIR:-/private/tmp}/class-knowledge-native-build/Class Knowledge.app"
```

The checkout build is labeled **Class Knowledge Dev** and uses separate macOS preferences. The installed daily-use app remains **Class Knowledge**.

To install a stable copy in `~/Applications` and open it:

```bash
./scripts/install_macos_app.sh
```

On first launch, choose the project folder containing `app.py`, `src/`, and `.venv/`. That workspace choice is saved for future launches. In **Settings → App startup**, turn on **Open Class Knowledge when I sign in** to add the installed app to macOS Login Items. It opens after sign-in; its local queue runs while the app is open and recovers interrupted work into **Later** after restart. See [`macos/README.md`](macos/README.md) and [`macos/PLAN.md`](macos/PLAN.md) for details.

### DeepSeek Harness plugin

The [`dsh-plugin/`](dsh-plugin/README.md) package adds a **Class Knowledge** entry to Harness's left sidebar. Its internal navigation follows the native app's **Library, Agent, Lecture Notes, Job Queue, and Settings** sections. In **Agent**, ask questions scoped to the current subject or document; the active Harness model receives related library passages, cites them in its answer, and can fill gaps with general knowledge. The panel also browses subjects, folders, and documents, shows lecture queue status, opens the native app, and exposes library tools to Harness agents. The desktop profile can install it from this repository with `pnpm add file:/path/to/class-2-knowleadge/dsh-plugin`; see the plugin guide for profile configuration and environment variables.

Start in **Library** to create a subject and optional lecture folders. The file explorer has persistent folder locations on the left, file/folder icons in the center, and a content preview on the right—there are no repeated document rows. Drag local files onto the subject root or any folder in the explorer to upload them; drop into open space to upload into the folder being viewed. Click a file once to preview it, double-click a file to open its full-page view in a new browser tab, or press and drag a file tile onto any folder (including the subject root) to move it. Use the magnifier field at the top to search this subject. The full-page browser view uses the available width and opens every file type; PDFs and PowerPoint decks show the current page or slide beside its text, and processed lecture decks show the combined teacher explanation. A lecture recording appears above its cleaned transcript; playback highlights the matching timestamped passage, and selecting a passage seeks the audio. When a lecture PDF/PPT is clicked, the same preview pane shows the selected slide beside one concise note that combines its essential content with relevant teacher explanation, with arrow navigation and a **🧠** action for that slide. The recording, class slides, and concise notes PDF are linked as one lecture set. In **Lecture Notes**, drop up to one recording and one slide deck from Voice Memos or Finder into the source area, or use the file buttons; choose an optional destination subject and select **Queue Lecture Task**. New lecture jobs use Normal priority; checkpoint resumption and priority changes are available in **Job Queue**. A supplied title is kept verbatim for the task and its folder, which is created immediately; a blank title is inferred automatically into a consistent name such as `Lecture_01_Introduction`. Within each lecture folder, learner-facing files use simple names such as `Slides.pdf`, `Recording.m4a`, and `Notes.pdf`. With both inputs, recording transcription and slide extraction run together before teacher speech is aligned to the relevant slides.

Open **Job Queue → Ollama model memory** to disable automatic unloading, inspect resident models, or unload an idle model manually. Manual unloading is blocked while a lecture or model call is active and for models required by queued work, so it cannot interrupt a task or force the next task to reload the same model.

## Output format

The generated Markdown and PDF intentionally contain only the concise content:

```markdown
# Lecture title

## Slide 1: Title
One to three precise sentences combining this slide with the professor's directly relevant explanation.

...
```

Every run is stored under `runs/<run-id>/`:

- `input/` — immutable copies of the uploaded recording and deck
- `transcript.partial.json` — atomically refreshed raw transcript of all completed recording chunks
- `transcript.raw.json` — untouched Whisper segments and paragraphs for audit/comparison
- `transcript_cleanup/` and `transcript.cleanup.partial.json` — resumable Qwen cleanup checkpoints
- `transcript.json` — cleaned, timestamped paragraphs used by the recording's synchronized Library view
- `transcript.txt` — internal plain-text copy of the same cleaned transcript
- `transcript_chunks/` — completed chunk checkpoints for inspecting long transcriptions
- `slides.json` — slide text, titles, notes, image paths, OCR hints, and rendered preview paths
- `slide_summaries.json` — one concise slide-plus-professor summary per slide
- `alignment.json` — each transcript paragraph’s assigned slide, timestamp, confidence, and method
- `quality_report.json` — evidence coverage, low-confidence ratio, warnings, and failures
- `database/` — local ChromaDB vectors for that lecture only
- `notes_checkpoints/` and `lecture_notes.partial.md` — atomic per-slide model checkpoints and a readable partial preview used when resuming
- `lecture_notes.md` and `lecture_notes.pdf` — concise, slide-by-slide English exports
- `lecture_summary.md` and `lecture_summary.pdf` — source-grounded overview of the complete lecture material
- `lecture_manifest.json` — named inventory of the source and generated artifacts
- `slide_reviews/` — separate on-demand deep-review Markdown/PDF files for selected slides
- `translations/` — created only after an explicit translation request

## Configuration and privacy

All runtime model settings live in `PipelineConfig` and are exposed in the Streamlit sidebar:

- Ollama host, LLM model, embedding model, and generation temperature
- faster-whisper model/path, device, compute type, and spoken language (`en` by default)
- concise baseline lecture-note mode, recording chunk duration/overlap, grounded transcript cleanup and its batch size, Ollama context, OCR, and alignment threshold
- chunk size/overlap and source-context limit (available in `src/config.py` for programmatic use)

The application has no cloud client and no API-key configuration. It disables Chroma anonymized telemetry and Streamlit usage statistics, and rejects non-loopback Ollama URLs and cloud-tagged model names. Ollama calls use `http://127.0.0.1:11434` by default.

## Troubleshooting

- **“Could not generate notes” / “Could not use local Ollama”**: start Ollama and ensure both configured models appear in `ollama list`.
- **Recording transcription fails**: check the media audio track and confirm that the configured Whisper model is cached or the configured local model path exists.
- **Warnings remain after updating**: reinstall the pinned dependencies with `pip install -r requirements.txt`. The app suppresses MuPDF's harmless `Screen`-annotation diagnostics and a known Apple-silicon NumPy matrix-warning false positive only inside the affected processing paths; genuine PDF and audio failures still appear as job errors.
- **Quality gate stops the run**: inspect `quality_report.json`. A mostly temporal alignment is not considered reliable enough to produce notes; confirm the correct deck/recording pair and spoken language.
- **No text from a scanned PDF**: install Tesseract and keep OCR enabled; very visual diagrams may still need manual interpretation because the system intentionally will not invent diagram meaning.
- **PPT conversion fails**: save the file as PPTX/PDF, or install LibreOffice for legacy `.ppt` conversion.
- **Alignment is weak**: review `alignment.json`. Reduce the semantic threshold slightly only when the slide and transcript use very different wording; `spoken_reference` mappings are highest confidence.
