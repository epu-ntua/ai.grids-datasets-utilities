from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from processing_utils.fs import ensure_dir


def write_dataframe(df: pd.DataFrame, path: Path, fmt: str) -> None:
    """Write a dataframe in a processor-supported tabular format."""
    ensure_dir(path.parent)

    if fmt == "json":
        with path.open("w", encoding="utf-8") as f:
            json.dump(
                json.loads(df.to_json(orient="records")),
                f,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        return
    if fmt == "csv":
        df.to_csv(path, index=False)
        return
    if fmt == "parquet":
        df.to_parquet(path, index=False)
        return
    if fmt == "pickle":
        df.to_pickle(path)
        return

    raise ValueError(f"Unsupported dataframe format: {fmt}")


def write_json_file(payload: Any, path: Path) -> None:
    """Write a JSON file with stable formatting."""
    ensure_dir(path.parent)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
