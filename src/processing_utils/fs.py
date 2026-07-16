from __future__ import annotations

from pathlib import Path


def ensure_dir(p: Path) -> Path:
    """Create directory tree if missing and return the same `Path` object."""
    p.mkdir(parents=True, exist_ok=True)
    return p
