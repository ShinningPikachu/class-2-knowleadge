"""Synchronized local audio player and cleaned transcript timeline."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import streamlit.components.v1 as components
from streamlit import runtime


def _media_url(path: Path, media_type: str, key: str) -> str:
    """Register a local recording with Streamlit's session media server."""
    if not runtime.exists():
        return ""
    try:
        return str(
            runtime.get_instance().media_file_mgr.add(
                str(path),
                media_type,
                f"audio-transcript-{key}",
            )
        )
    except Exception:
        return ""


def render_audio_transcript(
    path: Path,
    media_type: str,
    paragraphs: list[dict[str, Any]],
    *,
    key: str,
    raw_paragraphs: list[dict[str, Any]] | None = None,
    height: int = 650,
    focus_text: str = "",
) -> bool:
    """Render transcript cues that follow playback and seek the recording."""
    audio_url = _media_url(path, media_type, key)
    if not audio_url:
        return False

    raw_by_id = {
        str(item.get("id")): str(item.get("text", "")).strip()
        for item in (raw_paragraphs or [])
        if item.get("id") is not None
    }
    cues = [
        {
            "paragraph_id": item.get("id"),
            "raw": raw_by_id.get(str(item.get("id")), ""),
            "start": float(item.get("start", 0.0) or 0.0),
            "end": float(item.get("end", item.get("start", 0.0)) or 0.0),
            "start_time": str(item.get("start_time", "")),
            "end_time": str(item.get("end_time", "")),
            "text": str(item.get("text", "")).strip(),
        }
        for item in paragraphs
        if str(item.get("text", "")).strip()
    ]
    payload = json.dumps(cues, ensure_ascii=False).replace("</", "<\\/")
    source = json.dumps(audio_url).replace("</", "<\\/")
    focus = json.dumps(focus_text).replace("</", "<\\/")
    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    :root {{ color-scheme: light dark; --surface:#fff; --soft:#f6f7fa; --text:#20242c;
      --muted:#667085; --border:#d9dde7; --active:#fff2f2; --accent:#ff4b4b; }}
    * {{ box-sizing: border-box; }}
    html, body {{ margin:0; padding:0; background:transparent; color:var(--text);
      font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }}
    .viewer {{ height:{height - 8}px; display:flex; flex-direction:column; gap:10px; }}
    .player, .timeline {{ border:1px solid var(--border); border-radius:14px; background:var(--surface); }}
    .player {{ flex:none; display:grid; grid-template-columns:94px minmax(0,1fr); align-items:center;
      gap:12px; padding:10px 14px; }}
    .eyebrow {{ color:var(--muted); font-size:.72rem; font-weight:750; letter-spacing:.05em;
      text-transform:uppercase; }}
    audio {{ width:100%; }}
    .timeline {{ flex:1; min-height:0; padding:8px; overflow:auto; scroll-behavior:smooth; }}
    .cue {{ width:100%; display:grid; grid-template-columns:104px minmax(0,1fr); gap:12px;
      margin:0; padding:12px; border:0; border-bottom:1px solid var(--border); border-radius:9px;
      background:transparent; color:inherit; cursor:pointer; text-align:left; }}
    .cue:last-child {{ border-bottom:0; }}
    .cue:hover {{ background:var(--soft); }}
    .cue.active {{ background:var(--active); box-shadow:inset 3px 0 0 var(--accent); }}
    .time {{ color:var(--muted); font-size:.72rem; font-weight:700; font-variant-numeric:tabular-nums; }}
    .copy {{ font-size:.87rem; line-height:1.48; }}
    .raw {{ display:none; margin-top:7px; color:var(--muted); font-size:.78rem; white-space:pre-wrap; }}
    .viewer.audit .raw {{ display:block; }}
    .audit-tools {{ display:flex; flex-wrap:wrap; gap:7px; align-items:center; padding:2px 3px; }}
    .audit-tools[hidden] {{ display:none; }}
    .audit-tools button {{ padding:6px 10px; border:1px solid var(--border); border-radius:7px; background:var(--soft); color:var(--text); cursor:pointer; }}
    .audit-tools button:focus-visible {{ outline:2px solid var(--accent); }}
    .cue.flagged {{ outline:1px solid var(--accent); }}
    .empty {{ padding:24px; color:var(--muted); text-align:center; }}
    @media (max-width:720px) {{
      .viewer {{ height:auto; }}
      .player {{ grid-template-columns:1fr; gap:6px; }}
      .timeline {{ max-height:500px; }}
    }}
    @media (prefers-color-scheme:dark) {{
      :root {{ --surface:#15171c; --soft:#20232b; --text:#fafafa; --muted:#a7adba;
        --border:#3a3f4b; --active:#321f24; }}
    }}
  </style>
</head>
<body>
  <main class="viewer">
    <section class="player">
      <div class="eyebrow">Recording</div>
      <audio id="audio" controls preload="metadata"></audio>
    </section>
    <section id="audit-tools" class="audit-tools" hidden>
      <button id="audit-toggle" type="button">Compare original ASR</button>
      <button id="audit-mark" type="button">Flag current passage</button>
      <button id="audit-export" type="button">Export flagged passages</button>
    </section>
    <section id="timeline" class="timeline" aria-label="Cleaned transcript timeline"></section>
  </main>
  <script>
    const source = {source};
    const cues = {payload};
    const focusText = {focus};
    const audio = document.getElementById("audio");
    const timeline = document.getElementById("timeline");
    const viewer = document.querySelector(".viewer");
    const auditTools = document.getElementById("audit-tools");
    const auditToggle = document.getElementById("audit-toggle");
    const auditMark = document.getElementById("audit-mark");
    const auditExport = document.getElementById("audit-export");
    const flagged = new Set();
    audio.src = source;
    if (cues.some((cue) => cue.raw)) auditTools.hidden = false;

    function formatTime(seconds) {{
      const safe = Math.max(0, Math.floor(Number(seconds) || 0));
      const hours = Math.floor(safe / 3600);
      const minutes = Math.floor((safe % 3600) / 60);
      const remainder = safe % 60;
      return [hours, minutes, remainder].map((value) => String(value).padStart(2, "0")).join(":");
    }}

    const nodes = cues.map((cue, index) => {{
      const button = document.createElement("button");
      button.type = "button";
      button.className = "cue";
      button.dataset.index = String(index);
      const time = document.createElement("span");
      time.className = "time";
      time.textContent = cue.start_time && cue.end_time
        ? `${{cue.start_time}}–${{cue.end_time}}`
        : formatTime(cue.start);
      const copy = document.createElement("span");
      copy.className = "copy";
      copy.textContent = cue.text;
      const raw = document.createElement("span");
      raw.className = "raw";
      raw.textContent = cue.raw ? "Whisper original: " + cue.raw : "No raw transcript available for this passage.";
      const content = document.createElement("span");
      content.append(copy, raw);
      button.append(time, content);
      button.addEventListener("click", () => {{
        audio.currentTime = cue.start;
        audio.play().catch(() => undefined);
      }});
      timeline.appendChild(button);
      return button;
    }});

    function words(text) {{
      return String(text || "").toLowerCase().match(/[\p{{L}}\p{{N}}']{{3,}}/gu) || [];
    }}

    function focusCue() {{
      const terms = [...new Set(words(focusText))].slice(0, 12);
      if (!terms.length) return;
      let bestIndex = -1;
      let bestScore = 0;
      cues.forEach((cue, index) => {{
        const copy = String(cue.text || "").toLowerCase();
        const score = terms.reduce((total, term) => total + (copy.includes(term) ? 1 : 0), 0);
        if (score > bestScore) {{ bestScore = score; bestIndex = index; }}
      }});
      if (bestIndex < 0) return;
      nodes[bestIndex].classList.add("active");
      nodes[bestIndex].scrollIntoView({{ behavior: "auto", block: "center" }});
      audio.addEventListener("loadedmetadata", () => {{
        audio.currentTime = cues[bestIndex].start;
      }}, {{ once: true }});
    }}

    if (!nodes.length) {{
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = "No cleaned lecture transcript is available.";
      timeline.appendChild(empty);
    }}

    let activeIndex = -1;
    auditToggle.addEventListener("click", () => {{
      const enabled = viewer.classList.toggle("audit");
      auditToggle.textContent = enabled ? "Hide original ASR" : "Compare original ASR";
    }});
    auditMark.addEventListener("click", () => {{
      if (activeIndex < 0) return;
      if (flagged.has(activeIndex)) flagged.delete(activeIndex);
      else flagged.add(activeIndex);
      nodes[activeIndex].classList.toggle("flagged", flagged.has(activeIndex));
      auditMark.textContent = "Flag current passage (" + flagged.size + ")";
    }});
    auditExport.addEventListener("click", () => {{
      const report = [...flagged].sort((a, b) => a - b).map((index) => ({ ...cues[index], audit_status: "needs_review" }));
      const blob = new Blob([JSON.stringify({ flagged_passages: report }, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = "transcript_audit.json";
      link.click();
      URL.revokeObjectURL(url);
    }});
    function update() {{
      const current = audio.currentTime || 0;
      let next = -1;
      for (let index = 0; index < cues.length; index += 1) {{
        if (current >= cues[index].start && current <= Math.max(cues[index].end, cues[index].start + 0.5)) {{
          next = index;
          break;
        }}
      }}
      if (next === -1) {{
        for (let index = cues.length - 1; index >= 0; index -= 1) {{
          if (current >= cues[index].start) {{ next = index; break; }}
        }}
      }}
      if (next === activeIndex) return;
      if (activeIndex >= 0 && nodes[activeIndex]) nodes[activeIndex].classList.remove("active");
      activeIndex = next;
      if (activeIndex >= 0 && nodes[activeIndex]) {{
        nodes[activeIndex].classList.add("active");
        nodes[activeIndex].scrollIntoView({{ behavior:"smooth", block:"nearest" }});
      }}
    }}
    audio.addEventListener("timeupdate", update);
    audio.addEventListener("seeked", update);
    focusCue();
  </script>
</body>
</html>"""
    components.html(html, height=height, scrolling=False)
    return True
