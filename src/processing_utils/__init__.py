"""
Processing utility package.

Exports shared contracts and helpers used by source-format processors and
conversion entrypoints.
"""

from __future__ import annotations

from .contracts import (
    CanonicalBundle,
    DatasetLoaderLike,
    ProcessorLike,
    TargetFormat,
)
from .export_io import (
    write_dataframe,
    write_json_file,
)
from .fs import ensure_dir
from .manifest import (
    build_manifest,
    compute_schema_hash,
    write_manifest_json,
)

__all__ = [
    "CanonicalBundle",
    "DatasetLoaderLike",
    "ProcessorLike",
    "TargetFormat",
    "write_dataframe",
    "write_json_file",
    "ensure_dir",
    "build_manifest",
    "compute_schema_hash",
    "write_manifest_json",
]
