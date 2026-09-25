"""Streamlit UI regressions that do not require local AI models."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from src.config import PipelineConfig
from src.jobs import PRIORITIES, JobManager
from src.library import LibraryStore


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class StreamlitAppTest(unittest.TestCase):
    def test_all_workspaces_render_without_exceptions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temporary_root = Path(directory)
            source = temporary_root / "lecture.pdf"
            source.write_bytes(b"lecture")
            LibraryStore(temporary_root / "library").create_subject("Computer Science")
            seed_manager = JobManager(temporary_root, autostart=False)
            self.addCleanup(seed_manager.stop)
            completed_job = seed_manager.enqueue_lecture(
                config=PipelineConfig(),
                audio_path=None,
                presentation_path=source,
                lecture_title="Completed English lecture",
                subject_id=None,
                priority=PRIORITIES["Normal"],
            )
            seed_manager._claim_next_job()
            completed_run = temporary_root / "runs" / "completed-english-lecture"
            completed_run.mkdir(parents=True)
            completed_markdown = completed_run / "lecture_notes.md"
            completed_markdown.write_text("# English lecture\n\nEnglish notes.\n", encoding="utf-8")
            seed_manager._merge_job_result(
                completed_job.id,
                {"markdown_path": str(completed_markdown)},
            )
            seed_manager._finish_job(completed_job.id, "completed", "Lecture notes are ready", "")
            seed_manager.enqueue_lecture(
                config=PipelineConfig(),
                audio_path=None,
                presentation_path=source,
                lecture_title="Auditable lecture",
                subject_id=None,
                priority=PRIORITIES["Normal"],
            )
            with patch(
                "src.config.project_path",
                side_effect=lambda *parts: temporary_root.joinpath(*parts),
            ), patch("src.jobs.JobManager.start"):
                app = AppTest.from_file(str(PROJECT_ROOT / "app.py"), default_timeout=20).run()
                self.assertFalse(list(app.exception))
                workspace = next(item for item in app.radio if item.label == "Workspace")
                self.assertEqual(
                    workspace.options,
                    ["Library", "Agent", "Lecture Notes", "Job Queue"],
                )

                workspace.set_value("Agent").run()
                self.assertFalse(list(app.exception))
                self.assertIn("🤖 Local Library Agent", [item.value for item in app.title])
                active_agent = next(item for item in app.toggle if item.label == "Activate local agent")
                active_agent.set_value(True).run()
                self.assertFalse(list(app.exception))
                agent_expanders = [item.label for item in app.expander]
                self.assertNotIn("Insert documents into a subject", agent_expanders)
                self.assertNotIn("Quick file jobs", agent_expanders)

                workspace = next(item for item in app.radio if item.label == "Workspace")
                workspace.set_value("Job Queue").run()
                self.assertFalse(list(app.exception))
                self.assertIn("🗂️ Job Queue", [item.value for item in app.title])
                self.assertIn("Ollama model memory", [item.label for item in app.expander])
                self.assertIn(
                    "Automatically unload models after each task",
                    [item.label for item in app.toggle],
                )
                self.assertIn(
                    "Translate finished notes on demand",
                    [item.label for item in app.expander],
                )

                do_later = next(item for item in app.button if item.label == "Do later")
                do_later.click().run()
                self.assertFalse(list(app.exception))
                self.assertIn("Later (1)", [item.label for item in app.tabs])
                resume = next(item for item in app.button if item.label == "Resume from checkpoints")
                resume.click().run()
                self.assertFalse(list(app.exception))

                open_log = next(item for item in app.button if item.label == "Open processing log")
                open_log.click().run()
                self.assertFalse(list(app.exception))
                self.assertIn("📋 Lecture Processing Log", [item.value for item in app.title])

                workspace = next(item for item in app.radio if item.label == "Workspace")
                workspace.set_value("Lecture Notes").run()
                self.assertFalse(list(app.exception))
                self.assertIn("🎓 Queue Lecture Notes", [item.value for item in app.title])
                self.assertIn(
                    "Repair noisy transcript with local Qwen",
                    [item.label for item in app.checkbox],
                )
                spoken_language = next(
                    item for item in app.text_input if item.label == "Spoken language (ISO code)"
                )
                self.assertEqual(spoken_language.value, "en")

                input_method = next(item for item in app.radio if item.label == "Input method")
                input_method.set_value("Use local file paths").run()
                slides_path = next(
                    item for item in app.text_input if item.label == "Local slide-deck path (optional)"
                )
                slides_path.set_value(str(source)).run()
                queue_button = next(item for item in app.button if item.label == "Queue Lecture Task")
                queue_button.click().run()
                self.assertFalse(list(app.exception))
                self.assertIn("📋 Lecture Processing Log", [item.value for item in app.title])
                self.assertIn("Stored transcript", [item.value for item in app.subheader])
                self.assertIn("Processing timeline", [item.value for item in app.subheader])


if __name__ == "__main__":
    unittest.main()
