"""Setlist data file: song order, names, durations and section maps (kept outside the code on purpose)."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class SetlistData:
    sections: dict = field(default_factory=dict)   # songId -> [(startSeconds, sectionId)]
    names: dict = field(default_factory=dict)      # songId -> name or None
    durations: dict = field(default_factory=dict)  # songId -> seconds
    order: list = field(default_factory=list)      # [songId] in setlist order
    setlist_id: Optional[int] = None               # Playback's own setlist ID (identity; the version is per setlist)
    version: Optional[int] = None                  # setlistCloudVersion the data was captured at
    section_names: dict = field(default_factory=dict)  # sectionId -> name or None


def load_setlist_file(path: str) -> SetlistData:
    """File format: {"setlistId": 93000001 (optional, Playback's own setlist id), "setlistVersion": 11, "songs": [{"id": 1, "position": 1, "name": "..",
    "durationSeconds": 308.4, "sections": [{"id": 10, "start": 0.0, "name": "Intro"}]}]}"""
    with open(path) as f:
        d = json.load(f)
    out = SetlistData(version=d.get("setlistVersion"), setlist_id=d.get("setlistId"))
    songs = sorted(d.get("songs", []), key=lambda s: s.get("position", 1e9))
    for s in songs:
        sid = s["id"]
        out.order.append(sid)
        out.names[sid] = s.get("name")
        if s.get("durationSeconds") is not None:
            out.durations[sid] = float(s["durationSeconds"])
        out.sections[sid] = [(float(x["start"]), int(x["id"])) for x in s.get("sections", [])]
        for x in s.get("sections", []):
            out.section_names[int(x["id"])] = x.get("name")
    return out
