from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict

import pandas as pd

import processing_utils.validation.matpower_case as matpower_validation
from dataset_utils.matpower import matpower_overview, parse_matpower_case
from processing_utils.contracts import TargetFormat
from processing_utils.matpower_export import export_matpower_ml_ready


@dataclass(frozen=True)
class MatpowerCase:
    """
    Loader facade for a single Matpower `.m` case file.

    Responsibilities:
    - Parse source tables (`bus`, `gen`, `branch`, `gencost`) via `dataset_utils.matpower`
    - Expose lightweight quality checks used in notebooks and reporting
    - Delegate ML-ready export to `MatpowerProcessor`
    """

    datasets_root: Path
    relpath: str = "matpower/case.m"

    @property
    def path(self) -> Path:
        return self.datasets_root / self.relpath

    # ----------------------------
    # Loading
    # ----------------------------

    def load_tables(self) -> Dict[str, pd.DataFrame]:
        """Parse the case file and return normalized Matpower tables."""
        parsed = parse_matpower_case(self.path, use_cache=True)
        return parsed["tables"]

    def overview(self) -> pd.DataFrame:
        """Return high-level file and table summary for quick inspection."""
        return matpower_overview(self.path)

    # ----------------------------
    # Counts / checks
    # ----------------------------

    def basic_counts(self) -> Dict[str, int]:
        return matpower_validation.basic_counts(self)

    def counts_for_plot(self) -> Dict[str, int]:
        return matpower_validation.counts_for_plot(self)

    def duplicate_and_empty_checks(self) -> Dict[str, Any]:
        return matpower_validation.duplicate_and_empty_checks(self)

    def endpoint_integrity(self) -> Dict[str, Any]:
        return matpower_validation.endpoint_integrity(self)

    # ----------------------------
    # Connectivity (union-find; no networkx dependency)
    # ----------------------------

    def connectivity_stats(
        self, *, include_only_in_service: bool = True
    ) -> Dict[str, Any]:
        return matpower_validation.connectivity_stats(
            self, include_only_in_service=include_only_in_service
        )

    # -------------------
    # ML PREPARATION
    # ------------------
    def export_ml_ready(
        self,
        *,
        format: TargetFormat = "parquet",
        output_dir: Path | None = None,
        force: bool = False,
        include_only_in_service: bool = False,
        manifest_version: str = "v1",
    ) -> Dict[str, Any]:
        """
        Export canonical artifacts via `MatpowerProcessor`.

        Writes:
          nodes.<fmt>, edges.<fmt>, tables/*.<fmt>, metadata.json, manifest.json
        """
        return export_matpower_ml_ready(
            self.path,
            format=format,
            output_dir=output_dir,
            force=force,
            include_only_in_service=include_only_in_service,
            manifest_version=manifest_version,
        )
