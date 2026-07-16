from __future__ import annotations

import argparse
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Sequence

from dataset_utils.paths import get_datasets_root, require_existing_dir
from processing_utils.registry import get_processor

SOURCE_FORMATS: tuple[str, ...] = (
    "matpower",
    "rte7000_opensynth",
    "xiidm",
)

DEFAULT_TARGET_FORMATS: tuple[str, ...] = (
    "json",
    "parquet",
    "csv",
    "pt",
    "pickle",
    "matpower",
)


@dataclass(slots=True)
class ConvertOptions:
    source_format: str
    target_format: str
    input_value: str
    dataset: str | None = None
    output_dir: str | Path | None = None
    force: bool = False
    dry_run: bool = False
    include_only_in_service: bool = False
    assign_slack: bool = False
    slack_bus_id: int | None = None


def add_common_arguments(
    parser: argparse.ArgumentParser, *, required_core: bool = True
) -> argparse.ArgumentParser:
    parser.add_argument(
        "--from",
        dest="source_format",
        required=required_core,
        choices=list(SOURCE_FORMATS),
        help="Source format.",
    )
    parser.add_argument(
        "--to",
        dest="target_format",
        required=required_core,
        choices=list(DEFAULT_TARGET_FORMATS),
        help="Target output format.",
    )
    parser.add_argument(
        "--input",
        dest="input_value",
        required=required_core,
        help="Input path or dataset-relative path.",
    )
    parser.add_argument(
        "--dataset",
        dest="dataset",
        default=None,
        help="Optional dataset folder key. If set, --input is resolved under DATASETS_ROOT/<dataset>/...",
    )
    parser.add_argument(
        "--output_dir",
        dest="output_dir",
        default=None,
        help="Output directory. Default: <repo_root>/data/processed",
    )
    parser.add_argument(
        "--force", action="store_true", help="Overwrite output_dir if it exists."
    )
    parser.add_argument(
        "--dry_run", action="store_true", help="Print plan only; do not write files."
    )
    parser.add_argument(
        "--include_only_in_service",
        action="store_true",
        help="If supported by processor, export only in-service edges/elements.",
    )
    parser.add_argument(
        "--assign_slack",
        action="store_true",
        help="If supported by processor, force assignment of one REF/slack bus.",
    )
    parser.add_argument(
        "--slack_bus_id",
        type=int,
        default=None,
        help="If supported by processor, original bus id to use as REF/slack bus.",
    )
    return parser


def _ordered_unique(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out


def _order_target_formats(values: Iterable[str]) -> list[str]:
    ordered = _ordered_unique(values)
    preferred = [fmt for fmt in DEFAULT_TARGET_FORMATS if fmt in ordered]
    extras = [fmt for fmt in ordered if fmt not in preferred]
    return preferred + extras


def get_supported_target_formats(source_format: str) -> list[str]:
    proc = get_processor(source_format)
    supported = getattr(proc, "supported_target_formats", None)
    if supported:
        return _order_target_formats(supported)
    return list(DEFAULT_TARGET_FORMATS)


def resolve_input_path(
    *, repo_root: Path, input_value: str, dataset: str | None
) -> Path:
    in_path = Path(input_value).expanduser()
    if in_path.exists():
        return in_path.resolve()

    datasets_root = require_existing_dir(get_datasets_root(repo_root), "DATASETS_ROOT")
    if dataset:
        return (datasets_root / dataset / input_value).resolve()
    return (datasets_root / input_value).resolve()


def default_output_dir(repo_root: Path) -> Path:
    return (repo_root / "data" / "processed").resolve()


def prepare_output_dir(output_dir: Path, *, force: bool) -> None:
    if output_dir.exists():
        if not force and any(output_dir.iterdir()):
            raise FileExistsError(
                f"output_dir exists and is not empty (use --force): {output_dir}"
            )

        for child in list(output_dir.iterdir()):
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
    else:
        output_dir.mkdir(parents=True, exist_ok=True)


def _build_plan(
    *,
    proc: Any,
    opts: ConvertOptions,
    input_path: Path,
    output_dir: Path,
) -> Dict[str, Any]:
    plan: Dict[str, Any] = {
        "source_format": opts.source_format,
        "target_format": opts.target_format,
        "input_path": str(input_path),
        "output_dir": str(output_dir),
        "opts": {
            "include_only_in_service": bool(opts.include_only_in_service),
            "assign_slack": bool(opts.assign_slack),
            "slack_bus_id": opts.slack_bus_id,
        },
    }

    md = getattr(proc, "metadata", None)
    if isinstance(md, dict):
        plan["counts"] = md.get("counts", {})
        plan["metadata"] = md

    return plan


def _build_to_options(opts: ConvertOptions) -> Dict[str, Any]:
    to_opts: Dict[str, Any] = {
        "include_only_in_service": bool(opts.include_only_in_service),
        "write_manifest": True,
        "manifest_version": "v1",
    }
    if bool(opts.assign_slack):
        to_opts["assign_slack"] = True
    if opts.slack_bus_id is not None:
        to_opts["slack_bus_id"] = int(opts.slack_bus_id)
    return to_opts


def validate_target_format(
    source_format: str, target_format: str, *, supported: Sequence[str] | None = None
) -> None:
    allowed = (
        list(supported)
        if supported is not None
        else get_supported_target_formats(source_format)
    )
    if target_format not in allowed:
        raise SystemExit(
            f"--to {target_format} not supported for --from {source_format}. Supported: {allowed}"
        )


def run_conversion(*, repo_root: Path, opts: ConvertOptions) -> Dict[str, Any]:
    proc = get_processor(opts.source_format)
    supported = getattr(proc, "supported_target_formats", None)
    if supported:
        validate_target_format(
            opts.source_format,
            opts.target_format,
            supported=_order_target_formats(supported),
        )

    input_path = resolve_input_path(
        repo_root=repo_root,
        input_value=opts.input_value,
        dataset=opts.dataset,
    )
    output_dir = (
        Path(opts.output_dir).expanduser().resolve()
        if opts.output_dir is not None and str(opts.output_dir).strip()
        else default_output_dir(repo_root)
    )

    proc.load(input_path)

    if opts.dry_run:
        return _build_plan(
            proc=proc, opts=opts, input_path=input_path, output_dir=output_dir
        )

    prepare_output_dir(output_dir, force=bool(opts.force))
    return proc.to(
        opts.target_format,
        output_dir,
        **_build_to_options(opts),
    )
