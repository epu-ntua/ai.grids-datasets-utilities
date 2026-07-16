from __future__ import annotations

import hashlib
from pathlib import Path


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    """
    Compute SHA-256 for a file using chunked reads.

    Used for:
    - cache keys and invalidation
    - provenance metadata in reports/manifests
    - deterministic dataset fingerprint composition
    """
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            b = f.read(chunk_size)
            if not b:
                break
            h.update(b)
    return h.hexdigest()
