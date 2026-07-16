# notebooks/_notebook_bootstrap.py
from __future__ import annotations

import sys
from pathlib import Path


def bootstrap():
    # repo root = parent of this file’s directory (notebooks/)
    repo_root = Path(__file__).resolve().parents[1]
    src = repo_root / "src"

    for p in (repo_root, src):
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))

    from dataset_utils.paths import get_datasets_root, require_existing_dir

    datasets_root = require_existing_dir(get_datasets_root(repo_root), "DATASETS_ROOT")
    return repo_root, datasets_root
