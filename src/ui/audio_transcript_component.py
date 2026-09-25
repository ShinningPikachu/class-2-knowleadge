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
    height: int = 650,
) -> bool:
    """Render transcript cues that follow playback and seek the recording."""
    audio_url = _media_url(path, media_type, key)
    if not audio_url:
        return False

    cues = [
        {
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
    <section id="timeline" class="timeline" aria-label="Cleaned transcript timeline"></section>
  </main>
  <script>
    const source = {source};
    const cues = {payload};
    const audio = document.getElementById("audio");
    const timeline = document.getElementById("timeline");
    audio.src = source;

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
      button.append(time, copy);
      button.addEventListener("click", () => {{
        audio.currentTime = cue.start;
        audio.play().catch(() => undefined);
      }});
      timeline.appendChild(button);
      return button;
    }});

    if (!nodes.length) {{
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = "No cleaned lecture transcript is available.";
      timeline.appendChild(empty);
    }}

    let activeIndex = -1;
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
  </script>
</body>
</html>"""
    components.html(html, height=height, scrolling=False)
    return True
