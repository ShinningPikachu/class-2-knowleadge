# Class Knowledge Library

An offline-first subject library, document assistant, lecture-note generator, and background job queue tuned for a 32 GB local machine. The Streamlit application has four workspaces:

- **Library** — use a desktop-style two-pane file explorer with a folder sidebar and icon grid, drag file tiles into any folder, recognize file types from simple PDF/audio/Markdown/PowerPoint icons, click files to preview their contents, remove or rename files and folders, and search extracted text.
- **Agent** — explicitly activate a local assistant to ask grounded questions across every subject or within one selected subject, with document/page/slide citations. It can list, rename, and move documents or create a subject through conversational requests; every write action is previewed and requires confirmation.
- **Lecture Notes** — queue a university lecture recording, slide deck (`.pdf`, `.pptx`, or `.ppt`), or both for background processing. The original sources, stored transcript, and both final note formats can be saved automatically into one lecture folder inside a subject.
- **Job Queue** — see what is planned, running, waiting, saved for later, completed, failed, or cancelled; stop active work safely; resume it from checkpoints with a new priority; open a live processing log; preview or download stored transcripts; request an on-demand translation of finished notes; and inspect or unload Ollama models.

Lecture tasks are selected by priority (`High`, `Normal`, then `Low`) and creation time. **Stop safely · do later** is distinct from permanent cancellation: Whisper finishes its current chunk and stores the partial transcript, while an in-flight Qwen response finishes and is checkpointed before the task moves to **Later**. **Resume from checkpoints** returns it to the priority queue and reuses the same run folder, completed transcript chunks, cleanup batches, final transcript, and every completed slide note. If the application itself restarts during a task, that task is also recovered into **Later** instead of being marked failed. Whisper transcription can run while the interactive Qwen agent is active. Transcript cleanup and lecture-note generation use Qwen, so they wait between model calls while the interactive agent remains activated, then resume automatically after the agent is deactivated. Automatic model cleanup is enabled by default. Qwen stays warm while the interactive agent is activated, and lecture jobs hand resident models directly to queued work that uses the same Ollama host and model. Models are unloaded after completion, deferral, failure, cancellation, agent deactivation, or restart recovery only when no active or queued consumer still needs them. This releases unused model memory without causing avoidable unload/reload cycles or terminating the Ollama server.

Lecture processing uses one bounded, non-thinking Qwen call per slide. Every slide produces a concise summary combining its visible content with the professor's relevant aligned explanation. Detailed reasoning runs only after **Deep Review** is clicked beside an individual slide in the Library. That focused job uses high reasoning and a second factual audit, writes separate Markdown/PDF files back into the lecture folder, and never overwrites the baseline notes.

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
3. `src/pdf_processor.py` uses **PyMuPDF** for PDFs and **python-pptx** for PPTX. It preserves slide number, inferred title, text, PowerPoint speaker notes, embedded image paths, best-effort local OCR text, and a slide preview. LibreOffice supplies faithful PowerPoint previews when installed; a readable local fallback is produced otherwise.
4. `src/alignment.py` maps each cleaned transcript paragraph to a slide. Spoken references such as “slide 8” have priority. Otherwise it selects a slide through local Ollama embedding cosine similarity. Any weak match is marked `temporal_fallback` in `alignment.json` rather than being disguised as a reliable mapping.
5. `src/embeddings.py` chunks both sources and `src/rag.py` uses **LangChain's Chroma integration** to store their **local Ollama vectors** in a persistent **ChromaDB** directory. Metadata keeps `source`, `slide_number`, aligned slide, transcript paragraph ID, and timestamps.
6. `src/agent.py` retrieves evidence restricted to the current slide and stores its combined concise summary in `slide_summaries.json`. Fast baseline mode disables thinking, makes one call, and caps the response; deep mode enables high reasoning and a second factual audit. Long per-slide transcripts and the final lecture synthesis use hierarchical map-reduce, so later material is not silently truncated.
7. `src/quality.py` measures alignment reliability and structural completeness. If more than 70% of speech requires low-confidence temporal alignment, the run stops instead of exporting potentially misleading notes.
8. `src/exporter.py` writes canonical Markdown and renders a local PDF with ReportLab—no online conversion service or browser dependency.
9. `app.py` is the Streamlit interface. It supports browser uploads and direct local paths for multi-gigabyte recordings, displays live stages, and exposes every intermediate artifact.

## Project layout

```text
class-2-knowleadge/
├── app.py
├── requirements.txt
├── library/                # created at runtime; subject catalog and original files
├── jobs/                   # created at runtime; persistent queue and staged inputs
├── models/                 # optional local model files
├── audio/                  # reserved local audio workspace
├── pdf/                    # reserved local slide workspace
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
    ├── ui/                 # focused Library, Agent, and Lecture page modules
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

Start in **Library** to create a subject and optional lecture folders. The file explorer has persistent folder locations on the left, file/folder icons in the center, and a content preview on the right—there are no repeated document rows. Click a folder to browse it, click a file once to preview it, or press and drag a file tile onto any folder (including the subject root) to move it. When a lecture PDF/PPT is clicked, the same preview pane shows the selected slide on the left and its exact slide-plus-professor summary on the right, with **Deep Review** available only for that slide. In **Lecture Notes**, provide one or both lecture inputs, choose a priority and optional destination subject, and select **Queue Lecture Task**. A blank title is inferred automatically into a consistent name such as `Lecture_01_Introduction`; related source and result files use the same base name and are stored together. With both inputs, professor speech is aligned to the relevant slides.

Open **Job Queue → Ollama model memory** to disable automatic unloading, inspect resident models, or unload an idle model manually. Manual unloading is blocked while a lecture or model call is active and for models required by queued work, so it cannot interrupt a task or force the next task to reload the same model.

## Output format

The generated Markdown follows this structure:

```markdown
# Lecture title

## Overall Summary

# Slide 1: Title
## Concise summary
## Slide content
## Professor explanation
## Important concepts
## Exam points

...

# Complete Lecture Summary
# Key Definitions
# Important Formulas
# Possible Exam Questions
```

Every run is stored under `runs/<run-id>/`:

- `input/` — immutable copies of the uploaded recording and deck
- `transcript.partial.json` — atomically refreshed raw transcript of all completed recording chunks
- `transcript.raw.json` — untouched Whisper segments and paragraphs for audit/comparison
- `transcript_cleanup/` and `transcript.cleanup.partial.json` — resumable Qwen cleanup checkpoints
- `transcript.json` — cleaned, timestamped human-readable paragraphs used downstream
- `transcript.txt` — cleaned transcript in a directly readable text format
- `transcript_chunks/` — completed chunk checkpoints for inspecting long transcriptions
- `slides.json` — slide text, titles, notes, image paths, OCR hints, and rendered preview paths
- `slide_summaries.json` — one concise slide-plus-professor summary per slide
- `alignment.json` — each transcript paragraph’s assigned slide, timestamp, confidence, and method
- `quality_report.json` — evidence coverage, low-confidence ratio, warnings, and failures
- `database/` — local ChromaDB vectors for that lecture only
- `notes_checkpoints/` and `lecture_notes.partial.md` — atomic per-slide model checkpoints and a readable partial preview used when resuming
- `lecture_notes.md` and `lecture_notes.pdf` — canonical English exports
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
