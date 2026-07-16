from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional


def _sanitize_matlab_identifier(name: str) -> str:
    # MATLAB function names: start with letter, then letters/digits/underscore.
    out = []
    for ch in name:
        out.append(ch if (ch.isalnum() or ch == "_") else "_")
    s = "".join(out).strip("_")
    if not s:
        return "case"
    if not s[0].isalpha():
        s = "case_" + s
    return s


@dataclass(frozen=True)
class OutputPaths:
    # step artifacts (no numeric prefixes)
    sanity_json: Path
    sanity_txt: Path

    busmap_json: Path
    busmap_report_json: Path

    projected_json: Path
    projected_report_json: Path

    boundary_json: Path
    boundary_report_json: Path

    pu_json: Path
    pu_report_json: Path

    matpower_m: Path
    matpower_sidecar_json: Path
    matpower_write_report_json: Path

    validate_report_json: Path
    validate_txt: Path

    pruned_case_m: Path
    pruned_report_json: Path
    pruned_busmap_json: Path

    def ensure_dirs(self) -> None:
        for p in self.__dict__.values():
            p.parent.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True)
class Config:
    xiidm_path: Path
    out_dir: Path

    base_mva: float = 100.0
    case_name: Optional[str] = None

    compact_attribs: bool = False
    drop_original_dangling: bool = False

    # MATPOWER writing / validation / pruning
    assign_slack: bool = False
    slack_bus_id: Optional[int] = None
    default_q_limits: float = 9999.0
    run_pf: bool = False

    prune_isolated_buses: bool = True
    keep_largest_component: bool = False

    def resolved_case_name(self) -> str:
        if self.case_name:
            return _sanitize_matlab_identifier(self.case_name)
        return _sanitize_matlab_identifier(self.xiidm_path.stem)

    def build_paths(self) -> OutputPaths:
        case_dir = self.out_dir / self.xiidm_path.stem
        p = OutputPaths(
            sanity_json=case_dir / "sanity.json",
            sanity_txt=case_dir / "sanity.txt",
            busmap_json=case_dir / "busmap.json",
            busmap_report_json=case_dir / "busmap_report.json",
            projected_json=case_dir / "projected.json",
            projected_report_json=case_dir / "projected_report.json",
            boundary_json=case_dir / "projected_boundary.json",
            boundary_report_json=case_dir / "boundary_report.json",
            pu_json=case_dir / "pu.json",
            pu_report_json=case_dir / "pu_report.json",
            matpower_m=case_dir / f"{self.resolved_case_name()}.m",
            matpower_sidecar_json=case_dir / "matpower_sidecar.json",
            matpower_write_report_json=case_dir / "matpower_write_report.json",
            validate_report_json=case_dir / "validate_report.json",
            validate_txt=case_dir / "validate.txt",
            pruned_case_m=case_dir / f"{self.resolved_case_name()}_pruned.m",
            pruned_report_json=case_dir / "prune_report.json",
            pruned_busmap_json=case_dir / "prune_busmap.json",
        )
        p.ensure_dirs()
        return p
