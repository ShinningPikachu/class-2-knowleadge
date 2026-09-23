"""Entry point for the local subject library and lecture assistant."""

from __future__ import annotations

import warnings

# The macOS system Python is linked to LibreSSL. urllib3 emits a compatibility
# warning when imported indirectly by local-only clients, although this app only
# contacts Ollama over loopback HTTP. Future installs also pin urllib3 1.26 on
# Python 3.9; this exact filter keeps existing environments quiet until rebuilt.
warnings.filterwarnings(
    "ignore",
    message=r"urllib3 v2 only supports OpenSSL 1\.1\.1\+.*",
    module=r"urllib3(?:\.__init__)?",
)

import streamlit as st

from src.config import project_path
from src.jobs import JobManager
from src.library import LibraryStore
from src.ui.agent_page import render_agent
from src.ui.common import render_model_settings
from src.ui.jobs_page import render_jobs
from src.ui.lecture_page import render_lecture_processor
from src.ui.library_page import render_library


st.set_page_config(page_title="Class Knowledge Library", page_icon="🎓", layout="wide")


@st.cache_resource
def _job_manager() -> JobManager:
    return JobManager(project_path())


library = LibraryStore(project_path("library"))
job_manager = _job_manager()
with st.sidebar:
    st.title("Class Knowledge")
    workspace = st.radio("Workspace", ["Library", "Agent", "Lecture Notes", "Job Queue"])
    job_counts = job_manager.counts()
    st.caption(
        f"Queue: {job_counts['queued']} planned · "
        f"{job_counts['running'] + job_counts['waiting']} active"
    )

if workspace == "Library":
    render_library(library)
elif workspace == "Job Queue":
    render_jobs(job_manager)
else:
    model_config = render_model_settings(workspace)
    if workspace == "Agent":
        render_agent(library, model_config, job_manager)
    else:
        render_lecture_processor(library, model_config, job_manager)
