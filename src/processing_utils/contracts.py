from __future__ import annotations

from pathlib import Path
from typing import (
    Any,
    Dict,
    Literal,
    Mapping,
    Protocol,
    TypedDict,
    runtime_checkable,
)

import pandas as pd

# Centralized type contract for export targets supported by processors/CLI.
TargetFormat = Literal["json", "parquet", "csv", "npz", "pt", "pickle", "matpower"]


class CanonicalBundle(TypedDict):
    """
    Canonical in-memory payload shared across datasets.

    - tables: dataset-native tables after cleaning (Excel sheets, Matpower matrices, etc.)
    - nodes/edges: canonical graph tables for ML consumption
    - metadata: dataset-level metadata (fingerprints, versions, provenance)
    """

    tables: Dict[str, pd.DataFrame]
    nodes: pd.DataFrame
    edges: pd.DataFrame
    metadata: Dict[str, Any]


@runtime_checkable
class DatasetLoaderLike(Protocol):
    """
    Implemented by dataloaders/* classes.

    Required methods:
      - load_tables()
      - validate()
      - to_canonical()
    """

    def load_tables(self, **kwargs: Any) -> Dict[str, pd.DataFrame]: ...

    def validate(self) -> Dict[str, Any]: ...

    def to_canonical(self, **kwargs: Any) -> CanonicalBundle: ...


@runtime_checkable
class ProcessorLike(Protocol):
    """
    Implemented by processing_utils/* processors.

    Required methods:
      - load(input_path)
      - to(target_format, output_dir, **opts)
      - write_manifest(output_dir)
    """

    source_format: str

    def load(self, input_path: Path) -> "ProcessorLike": ...

    def to(
        self, target_format: TargetFormat, output_dir: Path, **opts: Any
    ) -> Dict[str, Any]:
        """
        Executes conversion/export.

        Returns a small dict describing what was written:
          {
            "output_dir": "...",
            "artifacts": {"nodes": "...", "edges": "...", "tables": {...}, ...},
            "counts": {...},
            "metadata": {...},
          }
        """
        ...

    def write_manifest(
        self, output_dir: Path, *, extra: Mapping[str, Any] | None = None
    ) -> Path: ...
