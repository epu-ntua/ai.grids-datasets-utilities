from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict

from .config import Config, OutputPaths


@dataclass
class State:
    cfg: Config
    paths: OutputPaths

    # lightweight bookkeeping (no big blobs)
    timings_s: Dict[str, float] = field(default_factory=dict)
    summaries: Dict[str, Any] = field(default_factory=dict)

    def record(
        self, step: str, elapsed_s: float, summary: Dict[str, Any] | None = None
    ) -> None:
        self.timings_s[step] = float(elapsed_s)
        if summary is not None:
            self.summaries[step] = summary
