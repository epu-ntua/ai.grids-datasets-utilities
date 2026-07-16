from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Optional

import pandas as pd

from dataset_utils.fingerprint import build_dataset_fingerprint, hash_key_columns
from dataset_utils.xiidm import parse_xiidm_sanity, read_report_json, write_report_json


def sanity_report(
    loader: Any,
    *,
    samples_per_tag: int = 15,
    max_issue_examples: int = 50,
    path_prefix_depth: int = 3,
    cache_path: Optional[Path] = None,
    force_recompute: bool = False,
) -> Dict[str, Any]:
    if cache_path is not None and cache_path.exists() and not force_recompute:
        return read_report_json(cache_path)

    report = parse_xiidm_sanity(
        loader.path,
        samples_per_tag=samples_per_tag,
        max_issue_examples=max_issue_examples,
        path_prefix_depth=path_prefix_depth,
    )

    if cache_path is not None:
        write_report_json(report, cache_path)

    return report


def top_tag_counts(report: Dict[str, Any], *, top_n: int = 40) -> Dict[str, int]:
    rows = report.get("counts", {}).get("tags_top40", [])
    rows = rows[:top_n]
    return {r["tag"]: int(r["count"]) for r in rows}


def validate(
    loader: Any,
    *,
    cache_path: Optional[Path] = None,
    force_recompute: bool = False,
) -> Dict[str, Any]:
    report = sanity_report(
        loader, cache_path=cache_path, force_recompute=force_recompute
    )

    file_sha = report.get("file", {}).get("sha256", None)
    file_path = Path(report.get("file", {}).get("path", str(loader.path)))

    tags_top = report.get("counts", {}).get("tags_top40", [])
    ids = report.get("ids", {})
    key_df = pd.DataFrame(tags_top)
    key_hashes = {
        "tags_top40": hash_key_columns(key_df, ["tag", "count"], sort_by=["tag"]),
        "ids_summary": hashlib.sha256(
            json.dumps(ids, sort_keys=True).encode("utf-8")
        ).hexdigest(),
    }

    fp = build_dataset_fingerprint(
        file_path, schema_version=loader.schema_version, key_hashes=key_hashes
    )

    return {
        "dataset": "rte7000_opensynth",
        "schema_version": loader.schema_version,
        "path": str(loader.path),
        "file_sha256": file_sha or fp["file_sha256"],
        "dataset_fingerprint": fp["fingerprint"],
        "checks": {
            "sanity_report": report,
        },
    }
