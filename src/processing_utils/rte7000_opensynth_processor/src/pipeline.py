from __future__ import annotations

import time
from dataclasses import dataclass
from typing import List, Protocol

from .boundary_dangling import materialize_dangling
from .busmap import build_busmap
from .per_unitize import per_unitize
from .project_equipment import project_equipment

# Import step functions (your renamed legacy scripts-as-modules)
from .sanity import run_sanity
from .state import State
from .validate_case import validate_case
from .write_matpower import write_matpower

# 07 needs a callable; see "Legacy updates" below
try:
    from .drop_isolated_buses import drop_isolated_buses_case
except Exception:  # keep import-time failure explicit at runtime if step is enabled
    drop_isolated_buses_case = None  # type: ignore


class Step(Protocol):
    name: str

    def run(self, st: State) -> None: ...


@dataclass(frozen=True)
class Pipeline:
    steps: List[Step]

    def run(self, st: State) -> State:
        for step in self.steps:
            t0 = time.perf_counter()
            step.run(st)
            st.record(step.name, time.perf_counter() - t0)
        return st


# ---------------- step wrappers ----------------


@dataclass(frozen=True)
class SanityStep:
    name: str = "sanity"

    def run(self, st: State) -> None:
        run_sanity(
            xiidm_path=str(st.cfg.xiidm_path),
            out_prefix=str(st.paths.sanity_json.with_suffix("")),
        )


@dataclass(frozen=True)
class BusmapStep:
    name: str = "busmap"

    def run(self, st: State) -> None:
        build_busmap(
            xiidm_path=str(st.cfg.xiidm_path),
            out_path=str(st.paths.busmap_json),
            report_path=str(st.paths.busmap_report_json),
            bus_id_start=1,
        )


@dataclass(frozen=True)
class ProjectEquipmentStep:
    name: str = "project_equipment"

    def run(self, st: State) -> None:
        project_equipment(
            xiidm_path=str(st.cfg.xiidm_path),
            busmap_path=str(st.paths.busmap_json),
            out_path=str(st.paths.projected_json),
            report_path=str(st.paths.projected_report_json),
            keep_all_attribs=(not st.cfg.compact_attribs),
        )


@dataclass(frozen=True)
class BoundaryDanglingStep:
    name: str = "boundary_dangling"

    def run(self, st: State) -> None:
        materialize_dangling(
            in_path=str(st.paths.projected_json),
            out_path=str(st.paths.boundary_json),
            report_path=str(st.paths.boundary_report_json),
            preserve_original_dangling_list=(not st.cfg.drop_original_dangling),
        )


@dataclass(frozen=True)
class PerUnitizeStep:
    name: str = "per_unitize"

    def run(self, st: State) -> None:
        per_unitize(
            in_path=str(st.paths.boundary_json),
            out_path=str(st.paths.pu_json),
            report_path=str(st.paths.pu_report_json),
            base_mva=float(st.cfg.base_mva),
        )


@dataclass(frozen=True)
class WriteMatpowerStep:
    name: str = "write_matpower"

    def run(self, st: State) -> None:
        write_matpower(
            in_path=str(st.paths.pu_json),
            out_m_path=str(st.paths.matpower_m),
            sidecar_path=str(st.paths.matpower_sidecar_json),
            report_path=str(st.paths.matpower_write_report_json),
            case_name=st.cfg.resolved_case_name(),
            assign_slack=bool(st.cfg.assign_slack),
            slack_bus_id=st.cfg.slack_bus_id,
            default_q_limits=float(st.cfg.default_q_limits),
        )


@dataclass(frozen=True)
class ValidateCaseStep:
    name: str = "validate_case"

    def run(self, st: State) -> None:
        validate_case(
            case_path=str(st.paths.matpower_m),
            sidecar_path=str(st.paths.matpower_sidecar_json),
            pu_json_path=str(st.paths.pu_json),
            report_path=str(st.paths.validate_report_json),
            txt_path=str(st.paths.validate_txt),
            run_pf=bool(st.cfg.run_pf),
        )


@dataclass(frozen=True)
class PruneIsolatedBusesStep:
    name: str = "prune_isolated_buses"

    def run(self, st: State) -> None:
        if not st.cfg.prune_isolated_buses:
            return
        if drop_isolated_buses_case is None:
            raise RuntimeError(
                "drop_isolated_buses_case not available. Update src/drop_isolated_buses.py to expose a callable."
            )

        drop_isolated_buses_case(
            case_path=str(st.paths.matpower_m),
            out_path=str(st.paths.pruned_case_m),
            report_path=str(st.paths.pruned_report_json),
            busmap_path=str(st.paths.pruned_busmap_json),
            keep_largest=bool(st.cfg.keep_largest_component),
            case_name=f"{st.cfg.resolved_case_name()}_pruned",
        )


def build_default_pipeline(prune_isolated: bool = True) -> Pipeline:
    steps: List[Step] = [
        SanityStep(),
        BusmapStep(),
        ProjectEquipmentStep(),
        BoundaryDanglingStep(),
        PerUnitizeStep(),
        WriteMatpowerStep(),
        ValidateCaseStep(),
    ]
    if prune_isolated:
        steps.append(PruneIsolatedBusesStep())
    return Pipeline(steps=steps)
