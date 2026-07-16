from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from dataset_utils.hashing import sha256_file
from processing_utils.contracts import TargetFormat
from processing_utils.fs import ensure_dir
from processing_utils.manifest import build_manifest, write_manifest_json
from processing_utils.rte7000_opensynth_processor import (
    Config as XiidmConfig,
)
from processing_utils.rte7000_opensynth_processor import (
    State as XiidmState,
)
from processing_utils.rte7000_opensynth_processor import (
    build_default_pipeline,
)


def _relpath(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _read_validate_sizes(validate_report_path: Path) -> Dict[str, int]:
    if not validate_report_path.exists():
        return {}
    try:
        import json

        payload = json.loads(validate_report_path.read_text(encoding="utf-8"))
        sizes = payload.get("matpower", {}).get("sizes", {})
        out: Dict[str, int] = {}
        for key in ("bus", "gen", "branch"):
            if key in sizes:
                out[key] = int(sizes[key])
        return out
    except Exception:
        return {}


@dataclass
class Rte7000OpenSynthProcessor:
    """
    XIIDM -> MATPOWER processor for RTE7000 OpenSynth.

    This processor intentionally supports only one target format: `matpower`.
    """

    source_format: str = "rte7000_opensynth"
    supported_target_formats: tuple[TargetFormat, ...] = ("matpower",)

    input_path: Optional[Path] = None
    metadata: Dict[str, Any] | None = None

    _last_manifest: Dict[str, Any] | None = None

    def load(self, input_path: Path) -> "Rte7000OpenSynthProcessor":
        input_path = Path(input_path).expanduser().resolve()
        if not input_path.exists():
            raise FileNotFoundError(input_path)
        if input_path.is_dir():
            raise IsADirectoryError(input_path)

        self.input_path = input_path
        self.metadata = None
        self._last_manifest = None
        return self

    def _run_pipeline(
        self,
        *,
        out_dir: Path,
        opts: Mapping[str, Any],
    ) -> tuple[XiidmState, Path, Dict[str, Any]]:
        if self.input_path is None:
            raise RuntimeError("Call load(input_path) first.")

        slack_bus_id_raw = opts.get("slack_bus_id")
        slack_bus_id = int(slack_bus_id_raw) if slack_bus_id_raw is not None else None

        cfg = XiidmConfig(
            xiidm_path=self.input_path,
            out_dir=out_dir,
            base_mva=float(opts.get("base_mva", 100.0)),
            case_name=opts.get("case_name"),
            compact_attribs=bool(opts.get("compact_attribs", False)),
            drop_original_dangling=bool(opts.get("drop_original_dangling", False)),
            # Keep MATPOWER output PF-sound by default.
            assign_slack=bool(opts.get("assign_slack", True)),
            slack_bus_id=slack_bus_id,
            default_q_limits=float(opts.get("default_q_limits", 9999.0)),
            run_pf=bool(opts.get("run_pf", False)),
            prune_isolated_buses=bool(opts.get("prune_isolated_buses", True)),
            keep_largest_component=bool(opts.get("keep_largest_component", True)),
        )

        st = XiidmState(cfg=cfg, paths=cfg.build_paths())
        pipe = build_default_pipeline(prune_isolated=cfg.prune_isolated_buses)
        pipe.run(st)

        use_pruned_case = bool(opts.get("use_pruned_case", cfg.prune_isolated_buses))
        selected_case = (
            st.paths.pruned_case_m
            if use_pruned_case and st.paths.pruned_case_m.exists()
            else st.paths.matpower_m
        )
        if not selected_case.exists():
            raise FileNotFoundError(
                f"Pipeline completed but case file not found: {selected_case}"
            )

        artifacts: Dict[str, str] = {
            "case_dir": _relpath(st.paths.matpower_m.parent, out_dir),
            "selected_case": _relpath(selected_case, out_dir),
        }

        def add_if_exists(name: str, path: Path) -> None:
            if path.exists():
                artifacts[name] = _relpath(path, out_dir)

        add_if_exists("sanity_json", st.paths.sanity_json)
        add_if_exists("busmap_json", st.paths.busmap_json)
        add_if_exists("projected_json", st.paths.projected_json)
        add_if_exists("boundary_json", st.paths.boundary_json)
        add_if_exists("pu_json", st.paths.pu_json)
        add_if_exists("matpower_case", st.paths.matpower_m)
        add_if_exists("matpower_sidecar_json", st.paths.matpower_sidecar_json)
        add_if_exists("validate_report_json", st.paths.validate_report_json)
        add_if_exists("validate_txt", st.paths.validate_txt)
        add_if_exists("pruned_case_m", st.paths.pruned_case_m)
        add_if_exists("pruned_report_json", st.paths.pruned_report_json)
        add_if_exists("pruned_busmap_json", st.paths.pruned_busmap_json)

        return st, selected_case, artifacts

    def to(
        self, target_format: TargetFormat, output_dir: Path, **opts: Any
    ) -> Dict[str, Any]:
        if self.input_path is None:
            raise RuntimeError("Call load(input_path) first.")

        if target_format not in self.supported_target_formats:
            raise ValueError(
                f"Unsupported target_format={target_format} for Rte7000OpenSynthProcessor. "
                f"Supported: {self.supported_target_formats}"
            )

        output_dir = ensure_dir(Path(output_dir).expanduser().resolve())
        write_manifest: bool = bool(opts.get("write_manifest", True))
        manifest_version: str = str(opts.get("manifest_version", "v1"))

        st, selected_case, artifacts = self._run_pipeline(out_dir=output_dir, opts=opts)

        validate_sizes = _read_validate_sizes(st.paths.validate_report_json)
        counts: Dict[str, Any] = {
            "source": validate_sizes,
            "export": {
                "matpower_case": 1,
            },
        }

        metadata: Dict[str, Any] = {
            "dataset": "rte7000_opensynth",
            "source_format": self.source_format,
            "target_format": "matpower",
            "input_path": str(self.input_path),
            "input_file_sha256": sha256_file(self.input_path),
            "conversion_case_path": str(selected_case),
            "used_pruned_case": bool(selected_case == st.paths.pruned_case_m),
            "timings_s": dict(st.timings_s),
        }

        # No table schema exists for a .m output target; use case file hash as
        # deterministic compatibility fingerprint in the manifest slot.
        schema_hash = sha256_file(selected_case)

        result = {
            "output_dir": str(output_dir),
            "artifacts": artifacts,
            "counts": counts,
            "metadata": metadata,
            "schema_hash": schema_hash,
        }

        manifest = build_manifest(
            source_format=self.source_format,
            input_path=str(self.input_path),
            output_dir=str(output_dir),
            artifacts=artifacts,
            counts=counts,
            metadata=metadata,
            schema_hash=schema_hash,
            version=manifest_version,
        )
        self._last_manifest = manifest

        if write_manifest:
            mpath = write_manifest_json(manifest, output_dir, filename="manifest.json")
            artifacts["manifest"] = str(mpath.relative_to(output_dir))
            result["manifest_path"] = str(mpath.relative_to(output_dir))

        self.metadata = metadata
        return result

    def write_manifest(
        self, output_dir: Path, *, extra: Mapping[str, Any] | None = None
    ) -> Path:
        if self._last_manifest is None:
            raise RuntimeError(
                "No manifest payload available. Call to(..., write_manifest=True) first."
            )

        output_dir = ensure_dir(Path(output_dir))
        manifest = dict(self._last_manifest)
        if extra:
            manifest.setdefault("extra", {})
            manifest["extra"] = {**dict(manifest["extra"]), **dict(extra)}

        return write_manifest_json(manifest, output_dir, filename="manifest.json")
