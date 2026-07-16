from __future__ import annotations

import datetime as _dt
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Mapping

import pandas as pd

from .fs import ensure_dir


def _iso_now() -> str:
    """Timezone-aware ISO timestamp for manifest run metadata."""
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _stable_json_dumps(x: Any) -> str:
    """Deterministic JSON encoding for stable hashing."""
    return json.dumps(x, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def compute_schema_hash(
    *,
    tables: Dict[str, pd.DataFrame],
    nodes: pd.DataFrame,
    edges: pd.DataFrame,
) -> str:
    """
    Deterministic hash of dataframe *schemas* (column names + dtypes), not values.
    Used for tracking compatibility across exports.
    """

    def df_schema(df: pd.DataFrame) -> Dict[str, Any]:
        return {
            "columns": [str(c) for c in df.columns],
            "dtypes": {str(c): str(df[c].dtype) for c in df.columns},
            "ncols": int(df.shape[1]),
        }

    payload: Dict[str, Any] = {
        "tables": {
            name: df_schema(df)
            for name, df in sorted(tables.items(), key=lambda kv: kv[0])
        },
        "nodes": df_schema(nodes),
        "edges": df_schema(edges),
    }

    s = _stable_json_dumps(payload).encode("utf-8")
    return hashlib.sha256(s).hexdigest()


def build_manifest(
    *,
    source_format: str,
    input_path: str,
    output_dir: str,
    artifacts: Mapping[str, Any],
    counts: Mapping[str, Any],
    metadata: Mapping[str, Any],
    schema_hash: str,
    version: str = "v1",
) -> Dict[str, Any]:
    """Build normalized manifest payload for conversion outputs."""
    return {
        "run": {"timestamp": _iso_now()},
        "source": {"format": source_format, "input_path": input_path},
        "output": {"dir": output_dir, "version": version, "schema_hash": schema_hash},
        "artifacts": dict(artifacts),
        "counts": dict(counts),
        "metadata": dict(metadata),
    }


def write_manifest_json(
    manifest: Mapping[str, Any],
    output_dir: Path,
    *,
    filename: str = "manifest.json",
) -> Path:
    """Write manifest JSON file under `output_dir`."""
    ensure_dir(output_dir)
    p = output_dir / filename
    with p.open("w", encoding="utf-8") as f:
        json.dump(dict(manifest), f, ensure_ascii=False, indent=2, sort_keys=True)
    return p
