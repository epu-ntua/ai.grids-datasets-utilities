from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


def get_repo_root(start: Path | None = None) -> Path:
    """
    Best-effort repository root discovery by walking up parent directories.

    Detection anchors:
    - `requirements.txt` (project dependency marker)
    - `.git` (repository marker)
    """
    p = (start or Path.cwd()).resolve()
    for _ in range(8):
        if (p / "requirements.txt").exists() or (p / ".git").exists():
            return p
        p = p.parent
    return (start or Path.cwd()).resolve()


def get_datasets_root(repo_root: Path) -> Path:
    """
    Resolve raw datasets root with explicit precedence.

    Order:
    1. `DATASETS_ROOT` environment variable
    2. `notebooks/.datasets_root` marker file
    3. hardcoded default path
    """
    env = os.getenv("DATASETS_ROOT")
    if env:
        return Path(env).expanduser().resolve()

    marker = repo_root / "notebooks" / ".datasets_root"
    if marker.exists():
        text = marker.read_text(encoding="utf-8").strip()
        if text:
            return Path(text).expanduser().resolve()

    return Path("/mnt/datadisk/data/datasets").resolve()


def require_existing_dir(p: Path, name: str) -> Path:
    """Validate that `p` exists and is a directory, with explicit error text."""
    if not p.exists():
        raise FileNotFoundError(f"{name} does not exist: {p}")
    if not p.is_dir():
        raise NotADirectoryError(f"{name} is not a directory: {p}")
    return p


def get_processed_root(repo_root: Optional[Path] = None) -> Path:
    """
    Resolve the standard processed-artifacts root.

    Default location:
    - `<repo_root>/data/processed`
    """
    rr = (repo_root or get_repo_root()).resolve()
    return (rr / "data" / "processed").resolve()
