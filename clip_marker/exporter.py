"""
exporter.py  ─  output helpers
  • save_csv()   : write timestamps to CSV
  • save_json()  : write timestamps to JSON
  • embed_chapters() : burn chapter metadata into output MP4 via ffmpeg
"""

import csv
import json
import subprocess
import tempfile
import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from detector import MarkerEvent


def save_csv(events: list, path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["time_sec", "timecode", "event_type", "confidence", "label"])
        writer.writeheader()
        for e in events:
            writer.writerow({
                "time_sec": round(e.time_sec, 3),
                "timecode": _fmt_tc(e.time_sec),
                "event_type": e.event_type,
                "confidence": round(e.confidence, 3),
                "label": e.label,
            })


def save_json(events: list, path: str) -> None:
    data = [
        {
            "time_sec": round(e.time_sec, 3),
            "timecode": _fmt_tc(e.time_sec),
            "event_type": e.event_type,
            "confidence": round(e.confidence, 3),
            "label": e.label,
        }
        for e in events
    ]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def embed_chapters(src_video: str, events: list, out_path: str) -> None:
    """
    Re-encode (stream-copy) the video adding ffmpeg chapter metadata.
    Requires ffmpeg in PATH.
    """
    # build FFMETADATA chapter block
    lines = [";FFMETADATA1\n"]
    for e in events:
        start_ms = int(e.time_sec * 1000)
        lines.append("[CHAPTER]\n")
        lines.append("TIMEBASE=1/1000\n")
        lines.append(f"START={start_ms}\n")
        lines.append(f"END={start_ms + 3000}\n")   # 3-sec chapter width
        lines.append(f"title={e.label} ({_fmt_tc(e.time_sec)})\n\n")

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8"
    ) as f:
        f.writelines(lines)
        meta_path = f.name

    try:
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", src_video,
                "-i", meta_path,
                "-map_metadata", "1",
                "-map_chapters", "1",
                "-codec", "copy",
                out_path,
            ],
            check=True,
            capture_output=True,
        )
    finally:
        os.unlink(meta_path)


def _fmt_tc(sec: float) -> str:
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"
