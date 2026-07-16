from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import pandas as pd

import processing_utils.validation.rte7000_opensynth as rte_validation
from dataset_utils.fingerprint import build_dataset_fingerprint, hash_key_columns
from processing_utils.contracts import CanonicalBundle


@dataclass(frozen=True)
class Rte7000OpenSynth:
    """
    Loader for the RTE7000 OpenSynth XIIDM snapshot.

    Current scope is metadata/sanity extraction rather than full topology
    canonicalization. The class still exposes the same `validate` and
    `to_canonical` surface used by other loaders for API consistency.
    """

    datasets_root: Path
    relpath: str = "d_gitt_rte7000_2021/rte-01Jan21-0000.xiidm"
    schema_version: str = "v1"

    @property
    def path(self) -> Path:
        return self.datasets_root / self.relpath

    def sanity_report(
        self,
        *,
        samples_per_tag: int = 15,
        max_issue_examples: int = 50,
        path_prefix_depth: int = 3,
        cache_path: Optional[Path] = None,
        force_recompute: bool = False,
    ) -> Dict[str, Any]:
        return rte_validation.sanity_report(
            self,
            samples_per_tag=samples_per_tag,
            max_issue_examples=max_issue_examples,
            path_prefix_depth=path_prefix_depth,
            cache_path=cache_path,
            force_recompute=force_recompute,
        )

    def top_tag_counts(
        self, report: Dict[str, Any], *, top_n: int = 40
    ) -> Dict[str, int]:
        return rte_validation.top_tag_counts(report, top_n=top_n)

    # ---------------------
    # Processing
    # ---------------------
    def validate(
        self,
        *,
        cache_path: Optional[Path] = None,
        force_recompute: bool = False,
    ) -> Dict[str, Any]:
        return rte_validation.validate(
            self,
            cache_path=cache_path,
            force_recompute=force_recompute,
        )

    def to_canonical(
        self,
        *,
        cache_path: Optional[Path] = None,
        force_recompute: bool = False,
    ) -> CanonicalBundle:
        """
        Return a canonical bundle placeholder for contract compatibility.

        Minimal canonical bundle for now:
          - nodes/edges intentionally empty (full extraction deferred)
          - metadata includes the sanity report summary + fingerprint
        """
        report = self.sanity_report(
            cache_path=cache_path, force_recompute=force_recompute
        )
        file_path = Path(report.get("file", {}).get("path", str(self.path)))

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
            file_path, schema_version=self.schema_version, key_hashes=key_hashes
        )

        nodes = pd.DataFrame(
            {
                "node_id": pd.Series([], dtype="string"),
                "node_type": pd.Series([], dtype="string"),
            }
        )
        edges = pd.DataFrame(
            {
                "edge_id": pd.Series([], dtype="string"),
                "src": pd.Series([], dtype="string"),
                "dst": pd.Series([], dtype="string"),
                "edge_type": pd.Series([], dtype="string"),
            }
        )

        metadata: Dict[str, Any] = {
            "dataset": "rte7000_opensynth",
            "schema_version": self.schema_version,
            "path": str(self.path),
            "file_sha256": report.get("file", {}).get("sha256", fp["file_sha256"]),
            "dataset_fingerprint": fp["fingerprint"],
            "iidm_schema_version": report.get("iidm", {}).get(
                "schema_version_from_namespace"
            ),
            "ids": report.get("ids", {}),
            "topology": report.get("topology", {}),
        }

        # Tables are intentionally omitted until full XIIDM extraction lands.
        return {"tables": {}, "nodes": nodes, "edges": edges, "metadata": metadata}
