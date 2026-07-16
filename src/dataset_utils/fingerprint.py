from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

import pandas as pd
from pandas.api.types import is_float_dtype, is_integer_dtype

from dataset_utils.hashing import sha256_file


def _stable_json_dumps(x: Any) -> str:
    """Stable JSON serialization for deterministic hash payloads."""
    return json.dumps(x, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def hash_key_columns(
    df: pd.DataFrame,
    cols: List[str],
    *,
    sort_by: Optional[List[str]] = None,
    max_rows: Optional[int] = None,
) -> str:
    """
    Deterministic hash of selected dataframe columns (values), intended for dataset fingerprinting.
    - selects cols that exist
    - sorts deterministically (mergesort)
    - converts to string and fills NA with ""
    - optionally truncates to max_rows (still deterministic after sort)
    """
    # Ignore missing columns so callers can pass shared specs safely.
    use_cols = [c for c in cols if c in df.columns]
    if not use_cols:
        return hashlib.sha256(b"").hexdigest()

    block = df[use_cols].copy()

    # Deterministic ordering is required for stable hashes across runs.
    sb = sort_by or use_cols
    sb_use = [c for c in sb if c in block.columns]
    if sb_use:
        block = block.sort_values(sb_use, kind="mergesort", na_position="last")

    if max_rows is not None and max_rows >= 0:
        block = block.head(int(max_rows))

    # Normalize representation before serialization.
    for c in use_cols:
        if is_float_dtype(block[c]):
            block[c] = pd.to_numeric(block[c], errors="coerce").astype("float64")
        elif is_integer_dtype(block[c]):
            block[c] = pd.to_numeric(block[c], errors="coerce").astype("Int64")
        else:
            block[c] = block[c].astype("string")

    payload = block.to_csv(
        sep="\t",
        index=False,
        lineterminator="\n",
        na_rep="",
        float_format="%.12g",
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def build_dataset_fingerprint(
    file_path: Path,
    *,
    schema_version: str,
    key_hashes: Mapping[str, str],
) -> Dict[str, str]:
    """
    Deterministic dataset fingerprint = sha256( file_sha256 + schema_version + key_hashes ).
    Returns {file_sha256, fingerprint}.
    """
    # File hash anchors the fingerprint to exact source bytes.
    file_sha = sha256_file(file_path)

    payload = {
        "file_sha256": file_sha,
        "schema_version": schema_version,
        "key_hashes": dict(key_hashes),
    }
    fp = hashlib.sha256(_stable_json_dumps(payload).encode("utf-8")).hexdigest()
    return {"file_sha256": file_sha, "fingerprint": fp}
