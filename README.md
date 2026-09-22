# class-2-knowleadge

An offline-first lecture-note generator tuned for a 32 GB local machine. Provide a university lecture recording (audio or video), a slide deck (`.pdf`, `.pptx`, or `.ppt`), or both and receive structured, source-grounded Markdown and PDF study notes. Two-hour recordings are split into overlapping chunks and transcribed by up to two bounded local workers; Whisper is still released before the writing model loads.

It reconstructs the class from both sources: the slide text/structure and the professor's timestamped explanation. No cloud API keys are used and the application only contacts the local Ollama endpoint (`127.0.0.1` by default).

## Architecture

```text
Audio/video ──> bundled PyAV decoder ─> 30-minute overlapping chunks ─> faster-whisper large-v3 ─┐
                                                                                 ├─> transcript.json
                                                                                 └─> per-chunk checkpoints
                                                       │
Slides PDF/PPTX ──> PyMuPDF/python-pptx ─> slides.json ├─> local semantic alignment
                         │                             │       │
                         └─> local Tesseract OCR        │       └─> alignment.json
                                                             
slides + transcript chunks ──> Ollama embeddings ──> ChromaDB (per-run, on disk)
                                                           │
strictly filtered evidence ──> Ollama qwen3.5:27b draft + factual review ──> hierarchical final notes + PDF
```

The pipeline is deliberately simple and inspectable:

1. `src/audio_processor.py` uses faster-whisper's bundled **PyAV** decoder and **large-v3** model to produce timestamped speech segments and readable paragraphs in `transcript.json`. The default 30-minute cores have 15 seconds of overlap; midpoint filtering prevents duplicated text while preserving boundary context. Up to two chunks run concurrently by default and are checkpointed in chronological order. The UI reports completed and active chunks with a confirmed transcript preview, and continues safely from valid checkpoints after interruption. MP3, WAV, M4A, AAC, FLAC, OGG, OPUS, MP4, MOV, MKV, WebM, and M4V are supported. For video, the audio track is transcribed; visual-only information must also appear in the supplied deck.
2. `src/pdf_processor.py` uses **PyMuPDF** for PDFs and **python-pptx** for PPTX. It preserves slide number, inferred title, text, PowerPoint speaker notes, embedded image paths, and best-effort local OCR text. Legacy PPT is converted locally through LibreOffice when available.
3. `src/alignment.py` maps each transcript paragraph to a slide. Spoken references such as “slide 8” have priority. Otherwise it selects a slide through local Ollama embedding cosine similarity. Any weak match is marked `temporal_fallback` in `alignment.json` rather than being disguised as a reliable mapping.
4. `src/embeddings.py` chunks both sources and `src/rag.py` uses **LangChain's Chroma integration** to store their **local Ollama vectors** in a persistent **ChromaDB** directory. Metadata keeps `source`, `slide_number`, aligned slide, transcript paragraph ID, and timestamps.
5. `src/agent.py` retrieves evidence restricted to the current slide, uses the 27B model's thinking mode to write a draft, and runs a second factual audit for every slide. Long per-slide transcripts and the final lecture synthesis use hierarchical map-reduce, so later material is not silently truncated. Its prompt forbids invented information and requires spoken-only claims to start with `Professor explanation:`.
6. `src/quality.py` measures alignment reliability and structural completeness. If more than 70% of speech requires low-confidence temporal alignment, the run stops instead of exporting potentially misleading notes.
7. `src/exporter.py` writes canonical Markdown and renders a local PDF with ReportLab—no online conversion service or browser dependency.
8. `app.py` is the Streamlit interface. It supports browser uploads and direct local paths for multi-gigabyte recordings, displays live stages, and exposes every intermediate artifact.

## Project layout

```text
class-2-knowleadge/
├── app.py
├── requirements.txt
├── models/                 # optional local model files
├── audio/                  # reserved local audio workspace
├── pdf/                    # reserved local slide workspace
├── database/               # reserved local Chroma workspace
├── runs/                   # created at runtime; one auditable folder per lecture
└── src/
    ├── audio_processor.py
    ├── pdf_processor.py
    ├── alignment.py
    ├── embeddings.py
    ├── rag.py
    ├── agent.py
    ├── exporter.py
    ├── pipeline.py
    ├── quality.py
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

Python 3.9 or newer is supported by the pinned dependency range currently used by the project; Python 3.11 is recommended for the smoothest installation and performance.

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

Then open the local URL Streamlit prints, provide one or both inputs, optionally adjust the local model settings, and select **Generate Lecture Notes**. With a deck only, the result contains slide-grounded notes. With a recording only, it contains timestamped 15-minute recording sections grounded in professor speech. With both, it aligns professor speech to slides.

## Output format

The generated Markdown follows this structure:

```markdown
# Lecture title

## Overall Summary

# Slide 1: Title
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
- `transcript.json` — timestamps, segments, and paragraphs
- `transcript_chunks/` — completed chunk checkpoints for inspecting long transcriptions
- `slides.json` — slide text, titles, notes, image paths, and OCR hints
- `alignment.json` — each transcript paragraph’s assigned slide, timestamp, confidence, and method
- `quality_report.json` — evidence coverage, low-confidence ratio, warnings, and failures
- `database/` — local ChromaDB vectors for that lecture only
- `lecture_notes.md` and `lecture_notes.pdf` — final exports

## Configuration and privacy

All runtime model settings live in `PipelineConfig` and are exposed in the Streamlit sidebar:

- Ollama host, LLM model, embedding model, and generation temperature
- faster-whisper model/path, device, compute type, and language
- recording chunk duration/overlap, Ollama context, OCR, and alignment threshold (model thinking and factual review remain enabled)
- chunk size/overlap and source-context limit (available in `src/config.py` for programmatic use)

The application has no cloud client and no API-key configuration. It disables Chroma anonymized telemetry and Streamlit usage statistics, and rejects non-loopback Ollama URLs and cloud-tagged model names. Ollama calls use `http://127.0.0.1:11434` by default.

## Troubleshooting

- **“Could not generate notes” / “Could not use local Ollama”**: start Ollama and ensure both configured models appear in `ollama list`.
- **Recording transcription fails**: check the media audio track and confirm that the configured Whisper model is cached or the configured local model path exists.
- **Quality gate stops the run**: inspect `quality_report.json`. A mostly temporal alignment is not considered reliable enough to produce notes; confirm the correct deck/recording pair and spoken language.
- **No text from a scanned PDF**: install Tesseract and keep OCR enabled; very visual diagrams may still need manual interpretation because the system intentionally will not invent diagram meaning.
- **PPT conversion fails**: save the file as PPTX/PDF, or install LibreOffice for legacy `.ppt` conversion.
- **Alignment is weak**: review `alignment.json`. Reduce the semantic threshold slightly only when the slide and transcript use very different wording; `spoken_reference` mappings are highest confidence.
