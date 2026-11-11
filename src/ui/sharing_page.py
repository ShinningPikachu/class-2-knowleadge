"""GitHub sharing preparation for the subject library."""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from ..library import LibraryStore
from ..sharing import (
    GITHUB_BLOCK_BYTES,
    collect_sharing_status,
    prepare_share_snapshot,
)
from .common import format_size


def render_sharing(library: LibraryStore, repository_root: str | Path) -> None:
    """Show repository contents, Git LFS readiness, and local share actions."""
    st.title("📤 Share your library")
    st.caption("Prepare the course files in this repository for sharing through GitHub.")
    status = collect_sharing_status(library, repository_root)

    metrics = st.columns(4)
    metrics[0].metric("Subjects", status.subject_count)
    metrics[1].metric("Library documents", status.document_count)
    metrics[2].metric("Total files", status.total_file_count)
    metrics[3].metric("Content size", format_size(status.total_bytes))

    st.markdown(
        "The repository includes the subject library, standalone files in `audio/` and `pdf/`, "
        "and reviewed notes in `reviewed/`. Processing runs, queues, local models, and app settings stay local."
    )

    if not status.git_available:
        st.error("Git was not found for this workspace. Open it from a Git checkout to prepare a share index.")
    if status.blocked_file_count:
        st.error(
            f"{status.blocked_file_count} file(s) exceed GitHub’s 100 MiB regular-file limit and are not covered by Git LFS."
        )
    elif status.large_file_count:
        st.info(
            f"{status.large_file_count} file(s) are larger than 50 MiB. Audio and video are configured for Git LFS."
        )
    if status.large_file_count and not status.git_lfs_installed:
        st.warning("Git LFS is not installed. Install it and run `git lfs install` before staging the recordings.")
    if status.pending_file_count is not None:
        st.caption(f"{status.pending_file_count} shareable file(s) are not yet tracked by Git.")

    with st.expander("Large files", expanded=bool(status.large_files)):
        if not status.large_files:
            st.caption("No files exceed 50 MiB.")
        else:
            for item in status.large_files:
                label = f"{format_size(item['size_bytes'])} · {item['path']}"
                st.write(label)
                if item["size_bytes"] > GITHUB_BLOCK_BYTES and item["filter"] != "lfs":
                    st.caption("This file needs a Git LFS rule before it can be pushed to GitHub.")

    if st.button("Refresh share index", type="primary", disabled=not status.git_available):
        try:
            index = prepare_share_snapshot(library, repository_root)
            st.success(f"Updated `{index.name}`. No files were staged or pushed.")
            st.rerun()
        except (OSError, RuntimeError, ValueError) as exc:
            st.error(f"Could not update the share index: {exc}")

    with st.expander("GitHub steps", expanded=True):
        st.markdown(
            "1. Make sure Git LFS is installed.\n"
            "2. Review the changed files and the generated `SHARE_INDEX.md`.\n"
            "3. Commit and push the repository. Friends can clone it and open the catalog."
        )
        st.code(
            "git lfs install\n"
            "git status --short\n"
            "git add -A\n"
            'git commit -m "Share course library"\n'
            "git push",
            language="bash",
        )
        st.caption(
            "For sharing with selected friends, set the GitHub repository to private and invite them. "
            "They need Git LFS enabled to download the recordings."
        )
        if not status.git_lfs_installed:
            st.caption("On macOS with Homebrew: `brew install git-lfs`, then run `git lfs install` once.")
