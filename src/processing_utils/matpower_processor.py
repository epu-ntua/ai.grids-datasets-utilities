from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

import pandas as pd

from dataset_utils.hashing import sha256_file
from dataset_utils.matpower import parse_matpower_case
from processing_utils.contracts import TargetFormat
from processing_utils.export_io import write_dataframe, write_json_file
from processing_utils.fs import ensure_dir
from processing_utils.manifest import (
    build_manifest,
    compute_schema_hash,
    write_manifest_json,
)


def _coerce_string_id(s: pd.Series) -> pd.Series:
    """Normalize identifier-like series to nullable string via numeric coercion."""
    return pd.to_numeric(s, errors="coerce").astype("Int64").astype("string")


@dataclass
class MatpowerProcessor:
    """
    Source-format processor for Matpower .m case files.

    - load(): parse case into tables
    - build_canonical(): nodes/edges
    - to(): export nodes/edges/tables/metadata to json/parquet/csv
    - write_manifest(): manifest.json with schema hash + provenance
    """

    source_format: str = "matpower"
    supported_target_formats: tuple[TargetFormat, ...] = (
        "json",
        "csv",
        "parquet",
        "pickle",
    )

    input_path: Optional[Path] = None
    scalars: Dict[str, Any] | None = None
    tables: Dict[str, pd.DataFrame] | None = None

    nodes: pd.DataFrame | None = None
    edges: pd.DataFrame | None = None
    metadata: Dict[str, Any] | None = None

    def load(self, input_path: Path) -> "MatpowerProcessor":
        """
        Parse source file and initialize in-memory canonical payload.

        Returns `self` to support fluent single-line usage.
        """
        input_path = Path(input_path)
        parsed = parse_matpower_case(input_path, use_cache=True)

        self.input_path = input_path
        self.scalars = dict(parsed.get("scalars", {}))
        self.tables = dict(parsed.get("tables", {}))

        # build canonical immediately (keeps processor single-call usable)
        self._build_canonical()
        return self

    # ----------------------------
    # Canonical build
    # ----------------------------

    def _build_canonical(self) -> None:
        """Build canonical `nodes`/`edges` and dataset metadata from parsed tables."""
        if self.input_path is None or self.tables is None:
            raise RuntimeError("Call load(input_path) first.")

        bus = self.tables.get("bus", pd.DataFrame()).copy()
        branch = self.tables.get("branch", pd.DataFrame()).copy()
        gen = self.tables.get("gen", pd.DataFrame()).copy()
        gencost = self.tables.get("gencost", pd.DataFrame()).copy()

        # ---- nodes (buses)
        if not bus.empty and "bus_id" in bus.columns:
            node_id = bus["bus_id"].astype("string")
        elif not bus.empty and "BUS_I" in bus.columns:
            node_id = _coerce_string_id(bus["BUS_I"])
        else:
            node_id = pd.Series([], dtype="string")

        n = int(len(node_id))
        nodes = pd.DataFrame(
            {
                "node_id": node_id,
                "node_type": pd.Series(["bus"] * n, dtype="string"),
                "in_service": (
                    bus["in_service"].astype("boolean")
                    if "in_service" in bus.columns
                    else pd.Series([pd.NA] * n, dtype="boolean")
                ),
                "bus_type": (
                    pd.to_numeric(bus["BUS_TYPE"], errors="coerce").astype("Int64")
                    if "BUS_TYPE" in bus.columns
                    else pd.Series([pd.NA] * n, dtype="Int64")
                ),
                "base_kv": (
                    pd.to_numeric(bus["BASE_KV"], errors="coerce").astype("Float64")
                    if "BASE_KV" in bus.columns
                    else pd.Series([pd.NA] * n, dtype="Float64")
                ),
                "vm": (
                    pd.to_numeric(bus["VM"], errors="coerce").astype("Float64")
                    if "VM" in bus.columns
                    else pd.Series([pd.NA] * n, dtype="Float64")
                ),
                "va": (
                    pd.to_numeric(bus["VA"], errors="coerce").astype("Float64")
                    if "VA" in bus.columns
                    else pd.Series([pd.NA] * n, dtype="Float64")
                ),
                "pd": (
                    pd.to_numeric(bus["PD"], errors="coerce").astype("Float64")
                    if "PD" in bus.columns
                    else pd.Series([pd.NA] * n, dtype="Float64")
                ),
                "qd": (
                    pd.to_numeric(bus["QD"], errors="coerce").astype("Float64")
                    if "QD" in bus.columns
                    else pd.Series([pd.NA] * n, dtype="Float64")
                ),
            }
        ).convert_dtypes()

        # ---- edges (branches)
        if (
            not branch.empty
            and "from_bus" in branch.columns
            and "to_bus" in branch.columns
        ):
            m = int(len(branch))
            edges = pd.DataFrame(
                {
                    "edge_id": pd.Series(
                        [f"branch:{i}" for i in range(m)], dtype="string"
                    ),
                    "src": branch["from_bus"].astype("string"),
                    "dst": branch["to_bus"].astype("string"),
                    "edge_type": pd.Series(["branch"] * m, dtype="string"),
                    "in_service": (
                        branch["in_service"].astype("boolean")
                        if "in_service" in branch.columns
                        else pd.Series([pd.NA] * m, dtype="boolean")
                    ),
                    "r": (
                        pd.to_numeric(branch["BR_R"], errors="coerce").astype("Float64")
                        if "BR_R" in branch.columns
                        else pd.Series([pd.NA] * m, dtype="Float64")
                    ),
                    "x": (
                        pd.to_numeric(branch["BR_X"], errors="coerce").astype("Float64")
                        if "BR_X" in branch.columns
                        else pd.Series([pd.NA] * m, dtype="Float64")
                    ),
                    "b": (
                        pd.to_numeric(branch["BR_B"], errors="coerce").astype("Float64")
                        if "BR_B" in branch.columns
                        else pd.Series([pd.NA] * m, dtype="Float64")
                    ),
                    "rate_a": (
                        pd.to_numeric(branch["RATE_A"], errors="coerce").astype(
                            "Float64"
                        )
                        if "RATE_A" in branch.columns
                        else pd.Series([pd.NA] * m, dtype="Float64")
                    ),
                    "tap": (
                        pd.to_numeric(branch["TAP"], errors="coerce").astype("Float64")
                        if "TAP" in branch.columns
                        else pd.Series([pd.NA] * m, dtype="Float64")
                    ),
                    "shift": (
                        pd.to_numeric(branch["SHIFT"], errors="coerce").astype(
                            "Float64"
                        )
                        if "SHIFT" in branch.columns
                        else pd.Series([pd.NA] * m, dtype="Float64")
                    ),
                    "angmin": (
                        pd.to_numeric(branch["ANGMIN"], errors="coerce").astype(
                            "Float64"
                        )
                        if "ANGMIN" in branch.columns
                        else pd.Series([pd.NA] * m, dtype="Float64")
                    ),
                    "angmax": (
                        pd.to_numeric(branch["ANGMAX"], errors="coerce").astype(
                            "Float64"
                        )
                        if "ANGMAX" in branch.columns
                        else pd.Series([pd.NA] * m, dtype="Float64")
                    ),
                }
            ).convert_dtypes()
        else:
            edges = pd.DataFrame(
                {
                    "edge_id": pd.Series([], dtype="string"),
                    "src": pd.Series([], dtype="string"),
                    "dst": pd.Series([], dtype="string"),
                    "edge_type": pd.Series([], dtype="string"),
                    "in_service": pd.Series([], dtype="boolean"),
                }
            )

        # ---- metadata
        # Keep a compact, provenance-focused metadata structure.
        meta: Dict[str, Any] = {
            "dataset": "matpower_case",
            "source_format": self.source_format,
            "input_path": str(self.input_path),
            "file_sha256": sha256_file(self.input_path),
            "mpc.version": (self.scalars or {}).get("version"),
            "mpc.baseMVA": (self.scalars or {}).get("baseMVA"),
            "counts": {
                "bus": int(len(bus)) if isinstance(bus, pd.DataFrame) else 0,
                "branch": int(len(branch)) if isinstance(branch, pd.DataFrame) else 0,
                "gen": int(len(gen)) if isinstance(gen, pd.DataFrame) else 0,
                "gencost": int(len(gencost))
                if isinstance(gencost, pd.DataFrame)
                else 0,
            },
        }

        self.nodes = nodes
        self.edges = edges
        self.metadata = meta

    # ----------------------------
    # Export
    # ----------------------------

    def to(
        self, target_format: TargetFormat, output_dir: Path, **opts: Any
    ) -> Dict[str, Any]:
        """
        Export canonical artifacts and source tables to disk.

        Returns a structured result containing artifact paths, counts,
        metadata snapshot, and schema hash.
        """
        if (
            self.input_path is None
            or self.tables is None
            or self.nodes is None
            or self.edges is None
            or self.metadata is None
        ):
            raise RuntimeError("Call load(input_path) first.")

        # Enforce supported formats early (clean CLI failure mode).
        if target_format not in self.supported_target_formats:
            raise ValueError(
                f"Unsupported target_format={target_format} for MatpowerProcessor. "
                f"Supported: {self.supported_target_formats}"
            )

        output_dir = ensure_dir(Path(output_dir))
        tables_dir = ensure_dir(output_dir / "tables")

        # Optional export knobs (kept generic for cross-processor parity).
        include_only_in_service: bool = bool(opts.get("include_only_in_service", False))
        write_manifest: bool = bool(opts.get("write_manifest", True))
        manifest_version: str = str(opts.get("manifest_version", "v1"))

        nodes = self.nodes
        edges = self.edges
        if include_only_in_service and "in_service" in edges.columns:
            edges = edges[edges["in_service"].fillna(False)].reset_index(drop=True)

        artifacts: Dict[str, Any] = {}

        def write_df(df: pd.DataFrame, path: Path) -> None:
            write_dataframe(df, path, target_format)

        # nodes/edges
        ext = {"json": "json", "csv": "csv", "parquet": "parquet", "pickle": "pickle"}[
            target_format
        ]
        nodes_path = output_dir / f"nodes.{ext}"
        edges_path = output_dir / f"edges.{ext}"
        write_df(nodes, nodes_path)
        write_df(edges, edges_path)
        artifacts["nodes"] = str(nodes_path.relative_to(output_dir))
        artifacts["edges"] = str(edges_path.relative_to(output_dir))

        # tables
        table_paths: Dict[str, str] = {}
        for name, df in sorted(self.tables.items(), key=lambda kv: kv[0]):
            if not isinstance(df, pd.DataFrame):
                continue
            p = tables_dir / f"{name}.{ext}"
            write_df(df, p)
            table_paths[name] = str(p.relative_to(output_dir))
        artifacts["tables"] = table_paths

        # Metadata is intentionally JSON regardless of tabular target format.
        meta_path = output_dir / "metadata.json"
        write_json_file(self.metadata, meta_path)
        artifacts["metadata"] = str(meta_path.relative_to(output_dir))

        source_counts = dict(self.metadata.get("counts", {}))
        export_counts = {
            "nodes": int(len(nodes)),
            "edges": int(len(edges)),
            "tables": {
                name: int(len(df))
                for name, df in self.tables.items()
                if isinstance(df, pd.DataFrame)
            },
        }
        counts = {"source": source_counts, "export": export_counts}

        schema_hash = compute_schema_hash(tables=self.tables, nodes=nodes, edges=edges)

        result = {
            "output_dir": str(output_dir),
            "artifacts": artifacts,
            "counts": counts,
            "metadata": dict(self.metadata),
            "schema_hash": schema_hash,
        }

        if write_manifest:
            manifest = build_manifest(
                source_format=self.source_format,
                input_path=str(self.input_path),
                output_dir=str(output_dir),
                artifacts=artifacts,
                counts=counts,
                metadata=dict(self.metadata),
                schema_hash=schema_hash,
                version=manifest_version,
            )
            mp = write_manifest_json(manifest, output_dir, filename="manifest.json")
            artifacts["manifest"] = str(mp.relative_to(output_dir))
            result["manifest_path"] = str(mp.relative_to(output_dir))

        return result

    def write_manifest(
        self, output_dir: Path, *, extra: Mapping[str, Any] | None = None
    ) -> Path:
        """Write a manifest for current in-memory state without re-exporting tables."""
        if (
            self.input_path is None
            or self.tables is None
            or self.nodes is None
            or self.edges is None
            or self.metadata is None
        ):
            raise RuntimeError("Call load(input_path) first.")

        output_dir = ensure_dir(Path(output_dir))
        schema_hash = compute_schema_hash(
            tables=self.tables, nodes=self.nodes, edges=self.edges
        )

        md = dict(self.metadata)
        if extra:
            md.update(dict(extra))

        manifest = build_manifest(
            source_format=self.source_format,
            input_path=str(self.input_path),
            output_dir=str(output_dir),
            artifacts={},  # if caller wants artifact paths, call to(...) first
            counts=dict(md.get("counts", {})),
            metadata=md,
            schema_hash=schema_hash,
            version="v1",
        )
        return write_manifest_json(manifest, output_dir, filename="manifest.json")
