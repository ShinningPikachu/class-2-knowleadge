"""Private newline-delimited JSON bridge used by the native macOS app.

The SwiftUI client launches this module as a child process. Standard output is
reserved for protocol messages; diagnostics from imported application code go
to standard error. This process never opens a network listener; it does expose a
user-only unix socket (<project-root>/.native_backend.sock, mode 600) so
local tooling such as the DSH plugin can drive the same API.
"""

from __future__ import annotations

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, fields, is_dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import sys
import threading
import traceback
from typing import Any


def _serialize(value: Any) -> Any:
    if is_dataclass(value):
        return _serialize(asdict(value))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _serialize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_serialize(item) for item in value]
    return value


def _encode(response: dict[str, Any]) -> str:
    return json.dumps(response, ensure_ascii=False, separators=(",", ":"))
def _job_summary(job: Any) -> dict[str, Any]:
    return {
        "id": job.id,
        "kind": job.kind,
        "title": job.title,
        "status": job.status,
        "priority": job.priority,
        "priority_label": job.priority_label,
        "stage": job.stage,
        "progress": job.progress,
        "message": job.message,
        "error": job.error,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "completed_at": job.completed_at,
    }


class NativeBackend:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root.resolve()
        sys.path.insert(0, str(self.project_root))

        # Imported libraries and the existing job manager may print diagnostics.
        # Keep that output off the line-delimited protocol stream.
        self.protocol_output = sys.stdout
        sys.stdout = sys.stderr

        from src.config import PipelineConfig
        from src.jobs import JobManager
        from src.library import LibraryStore

        self.PipelineConfig = PipelineConfig
        self.settings_path = self.project_root / "native_settings.json"
        self.config = self._load_config()
        self.library = LibraryStore(self.project_root / "library")
        self.manager = JobManager(self.project_root, autostart=True)
        self._write_lock = threading.Lock()

    def close(self) -> None:
        server = getattr(self, "_socket_server", None)
        if server is not None:
            try:
                server.close()
            except OSError:
                pass
            self._socket_server = None
        try:
            self.socket_path().unlink()
        except OSError:
            pass
        self.manager.stop(timeout=2.0)

    def _load_config(self) -> Any:
        if not self.settings_path.is_file():
            return self.PipelineConfig()
        try:
            values = json.loads(self.settings_path.read_text(encoding="utf-8"))
            allowed = {item.name for item in fields(self.PipelineConfig)}
            config = self.PipelineConfig(**{key: value for key, value in values.items() if key in allowed})
            config.validate()
            return config
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError(f"Native app settings could not be loaded: {exc}") from exc

    def _save_config(self, values: dict[str, Any]) -> Any:
        allowed = {item.name for item in fields(self.PipelineConfig)}
        merged = {**asdict(self.config), **{key: value for key, value in values.items() if key in allowed}}
        config = self.PipelineConfig(**merged)
        config.validate()
        temporary = self.settings_path.with_suffix(".json.part")
        temporary.write_text(json.dumps(asdict(config), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.settings_path)
        self.config = config
        return config

    def _configured_agent(self, config: Any) -> Any:
        from src.codex_agent import CodexLibraryAgent
        from src.library_agent import LibraryAgent

        if config.agent_provider == "codex":
            return CodexLibraryAgent(config)
        if config.agent_provider == "dsh":
            from src.dsh_agent import DshLibraryAgent

            return DshLibraryAgent(config)
        return LibraryAgent(config, chat_guard=lambda: self.manager.agent_qwen_slot(config))

    @staticmethod
    def _list_documents_answer(documents: list[Any]) -> str:
        if not documents:
            return "No documents are stored in that scope yet."
        lines = [
            f"- **{document.original_name}** — {document.subject_name} "
            f"({document.status.replace('_', ' ')})"
            for document in documents
        ]
        return "Here are the matching library documents:\n\n" + "\n".join(lines)

    def _ask_agent(self, params: dict[str, Any]) -> dict[str, Any]:
        from src.library_agent import LibraryAgentError

        config_values = params.get("config") or asdict(self.config)
        config = self.PipelineConfig(**config_values)
        config.validate()
        if not self.manager.is_agent_active():
            raise ValueError("Activate the agent before sending a question.")
        agent = self._configured_agent(config)
        prompt = str(params.get("prompt", "")).strip()
        scope = str(params.get("scope", "all"))
        if not prompt:
            raise ValueError("Ask a question before starting the library agent.")
        subjects = self.library.list_subjects()
        documents = self.library.list_documents()
        if agent.should_plan_action(prompt):
            plan = agent.plan(prompt, subjects, documents)
            if plan.action in {"rename_document", "move_document", "create_subject"}:
                return {
                    "answer": plan.message or "I prepared the requested library change. Review and confirm it before it runs.",
                    "sources": [],
                    "pending_action": _serialize(plan),
                }
            if plan.action == "list_documents":
                selected_scope = plan.target_subject_id or (None if scope == "all" else scope)
                return {
                    "answer": self._list_documents_answer(self.library.list_documents(selected_scope)),
                    "sources": [],
                    "pending_action": None,
                }
            if plan.message and not plan.query:
                return {"answer": plan.message, "sources": [], "pending_action": None}
            prompt = plan.query or prompt

        results = self.library.search_documents(
            prompt, subject_id=None if scope == "all" else scope, limit=8
        )
        try:
            answer = agent.answer(prompt, results)
        except LibraryAgentError:
            raise
        sources = [
            {"marker": f"S{index}", "citation": result.citation, "text": result.text}
            for index, result in enumerate(results, start=1)
        ]
        return {"answer": answer, "sources": sources, "pending_action": None}

    def _confirm_agent_action(self, params: dict[str, Any]) -> dict[str, str]:
        from src.library import LibraryError

        action = params.get("action") or {}
        kind = str(action.get("action", ""))
        try:
            if kind == "rename_document":
                updated = self.library.rename_document(str(action["document_id"]), str(action["new_name"]))
                message = f"Renamed the file to **{updated.original_name}**."
            elif kind == "move_document":
                updated = self.library.move_document(
                    str(action["document_id"]), str(action["target_subject_id"])
                )
                message = f"Moved **{updated.original_name}** to **{updated.subject_name}**."
            elif kind == "create_subject":
                subject = self.library.create_subject(str(action["subject_name"]))
                message = f"Created the subject **{subject.name}**."
            else:
                raise ValueError("This proposed library action cannot be confirmed.")
        except (KeyError, LibraryError) as exc:
            raise ValueError(str(exc)) from exc
        return {"message": message}

    @staticmethod
    def _read_json(path: Path | None) -> dict[str, Any]:
        if path is None or not path.is_file():
            return {}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}

    def _related_documents(self, document: Any) -> list[Any]:
        return [
            item for item in self.library.list_documents(document.subject_id)
            if item.folder_id == document.folder_id
        ]

    @staticmethod
    def _find_related_artifact(documents: list[Any], tokens: tuple[str, ...]) -> Path | None:
        for item in documents:
            normalized = re.sub(r"[^a-z0-9]+", "_", item.original_name.casefold()).strip("_")
            if all(token in normalized for token in tokens):
                return item.stored_path
        return None

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    def _matching_lecture_job(self, document: Any) -> Any | None:
        candidates = [
            job for job in self.manager.list_jobs(limit=500)
            if job.kind == "lecture" and job.status == "completed"
            and str(job.payload.get("subject_id") or "") == document.subject_id
        ]
        matching = []
        for job in candidates:
            roots = []
            run_dir = str(job.result.get("run_dir", "")).strip()
            if run_dir:
                roots.append(Path(run_dir) / "input")
            roots.append(self.manager.root / job.id / "input")
            found = False
            visited: set[Path] = set()
            for root in roots:
                try:
                    resolved = root.resolve()
                    if resolved in visited or not resolved.is_dir():
                        continue
                    visited.add(resolved)
                    for path in resolved.iterdir():
                        if path.is_file() and path.suffix.lower() == document.stored_path.suffix.lower():
                            if path.stat().st_size == document.size_bytes and self._sha256(path) == document.sha256:
                                found = True
                                break
                except OSError:
                    continue
                if found:
                    break
            if found:
                matching.append(job)
        return max(matching, key=lambda job: job.created_at) if matching else None

    @staticmethod
    def _result_path(job: Any | None, key: str) -> Path | None:
        if job is None:
            return None
        value = str(job.result.get(key, "")).strip()
        return Path(value) if value else None

    @staticmethod
    def _translated_sections(markdown: str) -> dict[int, str]:
        headings = list(re.finditer(r"^##[ \t]+(.+)$", markdown, re.MULTILINE))
        sections: dict[int, str] = {}
        for index, heading in enumerate(headings):
            match = re.match(
                r"(?:Slide\s+|幻灯片\s*|幻燈片\s*|投影片\s*|第\s*)([0-9]+)(?:\s*[：:页頁張张]|\b)",
                heading.group(1), re.IGNORECASE,
            )
            if not match:
                continue
            number = int(match.group(1))
            end = headings[index + 1].start() if index + 1 < len(headings) else len(markdown)
            section = markdown[heading.start():end].strip()
            if number in sections:
                return {}
            if markdown[heading.end():end].strip():
                sections[number] = section
        return sections

    def _saved_translations(self, related: list[Any], source_job: Any | None) -> dict[str, dict[str, str]]:
        candidates: list[tuple[Path, str]] = []
        source_path = self._result_path(source_job, "markdown_path")
        if source_path and source_path.is_file():
            for metadata_path in sorted((source_path.parent / "translations").glob("*.md.metadata.json")):
                metadata = self._read_json(metadata_path)
                path = metadata_path.with_name(metadata_path.name.removesuffix(".metadata.json"))
                language = str(metadata.get("target_language", "")).strip()
                if language and path.is_file():
                    candidates.append((path, language))
        for item in related:
            match = re.search(r"Notes\.([\w-]+)\.md$", item.original_name, re.IGNORECASE)
            if match:
                slug = match.group(1).lower()
                language = {
                    "chinese_simplified": "Chinese (Simplified)",
                    "chinese_traditional": "Chinese (Traditional)",
                }.get(slug, slug.replace("_", " ").title())
                candidates.append((item.stored_path, language))
        translations: dict[str, dict[str, str]] = {}
        for path, language in candidates:
            try:
                sections = self._translated_sections(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError):
                continue
            for number, text in sections.items():
                translations.setdefault(str(number), {}).setdefault(language, text)
        return translations

    def _presentation_preview_slides(self, document: Any) -> list[dict[str, Any]]:
        """Create a reusable slide preview for imported presentation files."""
        from src.pdf_processor import PDFProcessor

        cache_id = hashlib.sha256(
            f"{document.sha256}:native-presentation-preview-v1".encode("utf-8")
        ).hexdigest()
        preview_dir = self.library.root / ".preview_cache" / cache_id
        slides_path = preview_dir / "slides.json"
        cached = self._read_json(slides_path)
        cached_slides = [item for item in cached.get("slides", []) if isinstance(item, dict)]
        if cached_slides:
            return cached_slides

        preview_dir.mkdir(parents=True, exist_ok=True)
        config = self.PipelineConfig(enable_ocr=False)
        payload = PDFProcessor(config).process(document.stored_path, slides_path)
        return [item for item in payload.get("slides", []) if isinstance(item, dict)]

    def _manual_slide_summaries_path(self, document: Any) -> Path:
        """Find private, slide-specific explanations saved with a library deck."""
        artifact_key = document.folder_id or document.id
        return (
            self.library.subjects_root
            / document.subject_id
            / "derived"
            / artifact_key
            / "slide_summaries.json"
        )

    def _lecture_bundle(self, document: Any) -> dict[str, Any]:
        suffix = document.stored_path.suffix.lower()
        related = self._related_documents(document)
        source_job = self._matching_lecture_job(document)
        empty = {"kind": "none", "source_job": None, "slides": [], "paragraphs": [], "translations": {}}
        if suffix in {".pdf", ".ppt", ".pptx"}:
            slides_path = self._find_related_artifact(related, ("slides", "extracted")) or self._result_path(source_job, "slides_path")
            slide_payload = self._read_json(slides_path)
            slides = [item for item in slide_payload.get("slides", []) if isinstance(item, dict)]
            is_named_pdf_deck = suffix == ".pdf" and "slides" in re.sub(
                r"[^a-z0-9]+", "_", document.original_name.casefold()
            ) and "notes" not in document.original_name.casefold()
            if not slides and (suffix in {".ppt", ".pptx"} or is_named_pdf_deck):
                try:
                    slides = self._presentation_preview_slides(document)
                except Exception:
                    # Keep Quick Look available if local slide extraction fails.
                    slides = []
            is_deck = suffix in {".ppt", ".pptx"} or (
                suffix == ".pdf" and slides and "notes" not in document.original_name.casefold()
                and (source_job is not None or "slides" in re.sub(r"[^a-z0-9]+", "_", document.original_name.casefold()))
            )
            if not is_deck and suffix == ".pdf" and slides and "notes" not in document.original_name.casefold():
                try:
                    import fitz
                    with fitz.open(document.stored_path) as pdf:
                        is_deck = len(pdf) == len(slides)
                except Exception:
                    is_deck = False
            if is_deck and slides:
                summaries_path = self._find_related_artifact(related, ("slide", "summaries")) or self._result_path(source_job, "slide_summaries_path")
                if summaries_path is None:
                    manual_summaries = self._manual_slide_summaries_path(document)
                    summaries_path = manual_summaries if manual_summaries.is_file() else None
                summary_payload = self._read_json(summaries_path)
                summaries = {
                    str(int(item["slide"])): str(item.get("summary", "")).strip()
                    for item in summary_payload.get("slides", [])
                    if isinstance(item, dict) and str(item.get("slide", "")).isdigit()
                }
                alignment_path = self._find_related_artifact(related, ("alignment",)) or self._result_path(source_job, "alignment_path")
                alignment_payload = self._read_json(alignment_path)
                alignment = {
                    str(int(item["slide"])): [value for value in item.get("paragraphs", []) if isinstance(value, dict)]
                    for item in alignment_payload.get("slides", [])
                    if isinstance(item, dict) and str(item.get("slide", "")).isdigit()
                }
                translations = self._saved_translations(related, source_job)
                review_by_slide: dict[int, Any] = {}
                translation_status_by_slide: dict[int, list[dict[str, Any]]] = {}
                if source_job:
                    related_jobs = self.manager.list_jobs(limit=500)
                    reviews = [
                        job for job in related_jobs
                        if job.kind == "slide_review" and job.payload.get("source_job_id") == source_job.id
                    ]
                    for review in sorted(reviews, key=lambda item: item.created_at):
                        try:
                            number = int(review.payload.get("slide_number", 0))
                        except (TypeError, ValueError):
                            continue
                        review_by_slide[number] = review
                    for translation in related_jobs:
                        if translation.kind != "translation" or translation.payload.get("source_job_id") != source_job.id:
                            continue
                        try:
                            number = int(translation.payload.get("slide_number", 0))
                        except (TypeError, ValueError):
                            continue
                        translation_status_by_slide.setdefault(number, []).append({
                            "language": str(translation.payload.get("target_language", "")),
                            "status": translation.status,
                            "progress": translation.progress,
                            "message": translation.message,
                        })
                safe_slides = [
                    {
                        "slide": int(item.get("slide", index)),
                        "title": str(item.get("title", "")).strip() or f"Slide {index}",
                        "content": str(item.get("content", "")),
                        "preview_image": str(item.get("preview_image", "")),
                        "summary": summaries.get(str(int(item.get("slide", index))), ""),
                        "paragraphs": alignment.get(str(int(item.get("slide", index))), []),
                        "translations": translations.get(str(int(item.get("slide", index))), {}),
                        "translation_jobs": translation_status_by_slide.get(int(item.get("slide", index)), []),
                        "deep_review": self._deep_review_payload(
                            review_by_slide.get(int(item.get("slide", index)))
                        ),
                    }
                    for index, item in enumerate(slides, start=1)
                ]
                return {
                    "kind": "slides",
                    "source_job": _job_summary(source_job) if source_job else None,
                    "lecture_name": str(source_job.result.get("lecture_name", source_job.payload.get("lecture_name", document.folder_name or "Lecture"))) if source_job else (document.folder_name or "Lecture"),
                    "slides": safe_slides,
                    "paragraphs": [],
                    "translations": {},
                }
        if suffix in {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".mp4", ".mov", ".mkv", ".webm", ".m4v"}:
            transcript_path = next((
                item.stored_path for item in related
                if "transcript_cleaned" in re.sub(r"[^a-z0-9]+", "_", item.original_name.casefold()).strip("_")
                and item.stored_path.suffix.lower() == ".json"
            ), None) or self._result_path(source_job, "transcript_path")
            payload = self._read_json(transcript_path)
            metadata = payload.get("metadata", {}) if isinstance(payload.get("metadata"), dict) else {}
            kind = str(metadata.get("transcript_kind", "")).casefold()
            cleanup_enabled = True
            if source_job and isinstance(source_job.payload.get("config"), dict):
                cleanup_enabled = bool(source_job.payload["config"].get("enable_transcript_cleanup", True))
            paragraphs = payload.get("paragraphs", [])
            if (kind and kind != "cleaned") or (source_job and not cleanup_enabled):
                paragraphs = []
            return {
                "kind": "audio",
                "source_job": _job_summary(source_job) if source_job else None,
                "slides": [],
                "paragraphs": [item for item in paragraphs if isinstance(item, dict) and str(item.get("text", "")).strip()],
                "translations": {},
            }
        return empty

    def _deep_review_payload(self, job: Any | None) -> dict[str, Any] | None:
        if job is None:
            return None
        markdown_path = Path(str(job.result.get("markdown_path", "")))
        review_text = ""
        if job.status == "completed" and markdown_path.is_file():
            try:
                review_text = markdown_path.read_text(encoding="utf-8")[:200_000]
            except (OSError, UnicodeError):
                pass
        return {
            "job_id": job.id,
            "status": job.status,
            "progress": job.progress,
            "message": job.message,
            "text": review_text,
        }

    def dispatch(self, method: str, params: dict[str, Any]) -> Any:
        if method == "system.ping":
            return {"application": "Class Knowledge Library", "project_root": str(self.project_root)}
        if method == "system.shutdown":
            self.close()
            return {"stopped": True}
        if method == "app.snapshot":
            subjects = self.library.list_subjects()
            return {
                "project_root": str(self.project_root),
                "subjects": _serialize(subjects),
                "jobs": [_job_summary(job) for job in self.manager.list_jobs(limit=500)],
                "counts": self.manager.counts(),
                "agent_active": self.manager.is_agent_active(),
                "ollama_agent_active": self.manager.is_ollama_agent_active(),
                "config": _serialize(asdict(self.config)),
            }
        if method == "sharing.status":
            from src.sharing import collect_sharing_status

            return collect_sharing_status(self.library, self.project_root).to_dict()
        if method == "sharing.prepare":
            from src.sharing import prepare_share_snapshot

            return prepare_share_snapshot(self.library, self.project_root).to_dict()
        if method == "settings.get":
            return _serialize(asdict(self.config))
        if method == "settings.update":
            config = self._save_config(params.get("config") or {})
            return _serialize(asdict(config))
        if method == "agent.state":
            active = params.get("active")
            if active is not None:
                if bool(active):
                    values = params.get("config") or asdict(self.config)
                    config = self.PipelineConfig(**values)
                    config.validate()
                    if config.agent_provider == "codex":
                        from src.codex_agent import CodexLibraryAgent
                        codex_ready, codex_status = CodexLibraryAgent.status()
                        if not codex_ready:
                            raise ValueError(codex_status)
                    if config.agent_provider == "dsh":
                        from src.dsh_agent import DshLibraryAgent
                        dsh_ready, dsh_status_text = DshLibraryAgent(config).status()
                        if not dsh_ready:
                            raise ValueError(dsh_status_text)
                    self.manager.set_agent_active(True, config)
                else:
                    self.manager.set_agent_active(False)
            provider = str((params.get("config") or asdict(self.config)).get("agent_provider", "ollama"))
            codex_ready = True
            codex_status = ""
            if provider == "codex":
                from src.codex_agent import CodexLibraryAgent
                codex_ready, codex_status = CodexLibraryAgent.status()
            dsh_ready = True
            dsh_status = ""
            if provider == "dsh":
                from src.dsh_agent import DshLibraryAgent
                dsh_ready, dsh_status = DshLibraryAgent(self.config).status()
            return {
                "dsh_ready": dsh_ready,
                "dsh_status": dsh_status,
                "active": self.manager.is_agent_active(),
                "ollama_active": self.manager.is_ollama_agent_active(),
                "codex_ready": codex_ready,
                "codex_status": codex_status,
            }
        if method == "agent.ask":
            return self._ask_agent(params)
        if method == "agent.confirm_action":
            return self._confirm_agent_action(params)
        if method == "library.subjects":
            return _serialize(self.library.list_subjects())
        if method == "library.folders":
            return _serialize(self.library.list_folders(str(params["subject_id"])))
        if method == "library.documents":
            subject_id = str(params.get("subject_id") or "").strip() or None
            return _serialize(self.library.list_documents(subject_id))
        if method == "library.document":
            return _serialize(self.library.get_document(str(params["document_id"])))
        if method == "library.lecture_bundle":
            document = self.library.get_document(str(params["document_id"]))
            return _serialize(self._lecture_bundle(document))
        if method == "library.search":
            subject_id = str(params.get("subject_id") or "").strip() or None
            return _serialize(self.library.search_documents(
                str(params.get("query", "")), subject_id=subject_id, limit=int(params.get("limit", 10))
            ))
        if method == "library.create_subject":
            return _serialize(self.library.create_subject(
                str(params.get("name", "")), str(params.get("description", ""))
            ))
        if method == "library.delete_subject":
            return _serialize(self.library.delete_subject(str(params["subject_id"])))
        if method == "library.create_folder":
            return _serialize(self.library.create_folder(
                str(params["subject_id"]), str(params.get("name", ""))
            ))
        if method == "library.rename_folder":
            return _serialize(self.library.rename_folder(
                str(params["folder_id"]), str(params.get("name", ""))
            ))
        if method == "library.delete_folder":
            return _serialize(self.library.delete_folder(
                str(params["folder_id"]), delete_documents=bool(params.get("delete_documents", False))
            ))
        if method == "library.import":
            subject_id = str(params["subject_id"])
            folder_id = str(params.get("folder_id") or "").strip() or None
            imported: list[Any] = []
            errors: list[dict[str, str]] = []
            for raw_path in params.get("paths", []):
                source = Path(str(raw_path)).expanduser().resolve()
                try:
                    imported.append(self.library.add_document(subject_id, source, folder_id=folder_id))
                except Exception as exc:
                    errors.append({"path": str(source), "error": str(exc)})
            return {"documents": _serialize(imported), "errors": errors}
        if method == "library.rename_document":
            return _serialize(self.library.rename_document(
                str(params["document_id"]), str(params.get("name", ""))
            ))
        if method == "library.move_document":
            return _serialize(self.library.move_document(
                str(params["document_id"]), str(params["subject_id"])
            ))
        if method == "library.move_document_to_folder":
            folder_id = str(params.get("folder_id") or "").strip() or None
            return _serialize(self.library.move_document_to_folder(
                str(params["document_id"]), folder_id
            ))
        if method == "library.delete_document":
            return _serialize(self.library.delete_document(str(params["document_id"])))
        if method == "jobs.list":
            return [_job_summary(job) for job in self.manager.list_jobs(limit=int(params.get("limit", 100)))]
        if method == "jobs.detail":
            job = self.manager.get_job(str(params["job_id"]))
            return {**_job_summary(job), "payload": _serialize(job.payload), "result": _serialize(job.result)}
        if method == "jobs.events":
            events = self.manager.list_job_events(
                str(params["job_id"]), limit=int(params["limit"]) if params.get("limit") else None
            )
            return _serialize(events)
        if method == "jobs.enqueue_lecture":
            config = self.PipelineConfig(**params.get("config", asdict(self.config)))
            job = self.manager.enqueue_lecture(
                config=config,
                audio_path=str(params["audio_path"]) if params.get("audio_path") else None,
                presentation_path=(str(params["presentation_path"]) if params.get("presentation_path") else None),
                lecture_title=str(params["lecture_title"]).strip() or None
                if params.get("lecture_title") is not None else None,
                subject_id=str(params["subject_id"]) if params.get("subject_id") else None,
                priority=int(params.get("priority", 50)),
            )
            return _job_summary(job)
        if method == "jobs.enqueue_translation":
            return _job_summary(self.manager.enqueue_translation(
                str(params["source_job_id"]), str(params["target_language"]),
                priority=int(params.get("priority", 50)),
                slide_number=int(params["slide_number"]) if params.get("slide_number") is not None else None,
                explanation=str(params["explanation"]) if params.get("explanation") else None,
                slide_content=str(params["slide_content"]) if params.get("slide_content") else None,
            ))
        if method == "jobs.enqueue_slide_review":
            return _job_summary(self.manager.enqueue_slide_review(
                str(params["source_job_id"]), int(params["slide_number"]),
                priority=int(params.get("priority", 50)),
                library_subject_id=str(params["subject_id"]) if params.get("subject_id") else None,
                library_folder_id=str(params["folder_id"]) if params.get("folder_id") else None,
                lecture_name=str(params["lecture_name"]) if params.get("lecture_name") else None,
            ))
        if method == "voice.options":
            from src.voice import local_voice_options, neural_voice_setup_message
            return {"voices": list(local_voice_options()), "setup_message": neural_voice_setup_message() or ""}
        if method == "voice.synthesize":
            from src.voice import prepare_narration, synthesize_speech, wav_duration_seconds
            text, truncated = prepare_narration(str(params.get("text", "")), int(params.get("maximum_chars", 8_000)))
            audio = synthesize_speech(text, str(params.get("voice", "en_US-lessac-medium")))
            return {
                "audio_base64": base64.b64encode(audio).decode("ascii"),
                "duration_seconds": wav_duration_seconds(audio),
                "truncated": truncated,
                "characters": len(text),
            }
        if method == "jobs.action":
            job_id = str(params["job_id"])
            action = str(params.get("action", ""))
            if action == "cancel":
                job = self.manager.cancel_job(job_id)
            elif action == "defer":
                job = self.manager.defer_job(job_id)
            elif action == "resume":
                priority = int(params["priority"]) if params.get("priority") is not None else None
                job = self.manager.resume_job(job_id, priority=priority)
            elif action == "retry":
                job = self.manager.retry_failed_job(job_id)
            elif action == "priority":
                job = self.manager.update_priority(job_id, int(params["priority"]))
            else:
                raise ValueError(f"Unknown job action: {action}")
            return _job_summary(job)
        if method == "jobs.settings":
            if "auto_unload" in params:
                self.manager.set_auto_unload_enabled(bool(params["auto_unload"]))
            return {
                "auto_unload": self.manager.is_auto_unload_enabled(),
                "agent_active": self.manager.is_agent_active(),
                "ollama_agent_active": self.manager.is_ollama_agent_active(),
            }
        if method == "jobs.models":
            host = str(params.get("host") or self.config.ollama_host)
            return _serialize(self.manager.list_loaded_ollama_models(host))
        if method == "jobs.unload_models":
            host = str(params.get("host") or self.config.ollama_host)
            models = params.get("models")
            return _serialize(self.manager.stop_loaded_ollama_models(host, models=models))
        raise ValueError(f"Unsupported native backend method: {method}")

    def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        request_id = request.get("id")
        try:
            method = str(request.get("method", ""))
            params = request.get("params") or {}
            if not isinstance(params, dict):
                raise ValueError("Request params must be an object.")
            result = self.dispatch(method, params)
            return {"id": request_id, "result": _serialize(result)}
        except Exception as exc:
            traceback.print_exc(file=sys.stderr)
            return {"id": request_id, "error": {"message": str(exc), "type": type(exc).__name__}}

    def respond(self, request: dict[str, Any]) -> None:
        encoded = _encode(self.handle(request))
        with self._write_lock:
            self.protocol_output.write(encoded + "\n")
            self.protocol_output.flush()

    # ---- 本机 socket 通道：给 DSH 插件等本机进程用 ----
    def socket_path(self) -> Path:
        return self.project_root / ".native_backend.sock"

    def start_socket(self) -> None:
        path = self.socket_path()
        try:
            if path.exists():
                path.unlink()
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(path))
            os.chmod(path, 0o600)
            server.listen(8)
        except OSError as exc:
            print(f"socket channel disabled: {exc}", file=sys.stderr)
            return
        self._socket_server = server
        threading.Thread(target=self._socket_loop, name="socket-rpc", daemon=True).start()

    def _socket_loop(self) -> None:
        while True:
            try:
                conn, _ = self._socket_server.accept()
            except OSError:
                return
            threading.Thread(target=self._socket_conn, args=(conn,), daemon=True).start()

    def _socket_conn(self, conn: Any) -> None:
        with conn:
            stream = conn.makefile("rwb")
            with stream:
                for raw in stream:
                    try:
                        request = json.loads(raw.decode("utf-8"))
                    except Exception:
                        continue
                    if not isinstance(request, dict):
                        continue
                    stream.write((_encode(self.handle(request)) + "\n").encode("utf-8"))
                    stream.flush()


def main() -> int:
    parser = argparse.ArgumentParser(description="Local bridge for the native Class Knowledge app")
    parser.add_argument("--project-root", required=True, type=Path)
    args = parser.parse_args()
    project_root = args.project_root.expanduser().resolve()
    if not (project_root / "src" / "config.py").is_file():
        print(f"Not a Class Knowledge project directory: {project_root}", file=sys.stderr)
        return 2

    try:
        backend = NativeBackend(project_root)
    except Exception:
        traceback.print_exc(file=sys.stderr)
        return 1
    backend.start_socket()

    pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="native-rpc")
    try:
        for line in sys.stdin:
            try:
                request = json.loads(line)
                if not isinstance(request, dict):
                    raise ValueError("Request must be a JSON object.")
            except Exception as exc:
                backend.respond({"id": None, "method": "", "params": {"invalid": str(exc)}})
                continue
            pool.submit(backend.respond, request)
    finally:
        backend.close()
        pool.shutdown(wait=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
