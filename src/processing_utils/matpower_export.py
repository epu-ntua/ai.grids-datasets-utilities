from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from dataset_utils.paths import get_processed_root
from processing_utils.contracts import TargetFormat
from processing_utils.matpower_processor import MatpowerProcessor


def export_matpower_ml_ready(
    input_path: Path,
    *,
    format: TargetFormat = "parquet",
    output_dir: Path | None = None,
    force: bool = False,
    include_only_in_service: bool = False,
    manifest_version: str = "v1",
) -> Dict[str, Any]:
    if output_dir is not None:
        out = Path(output_dir)
    else:
        case_name = Path(input_path).stem
        out = (
            get_processed_root()
            / "matpower_case"
            / case_name
            / "default"
            / manifest_version
        )

    if out.exists() and force:
        for child in out.iterdir():
            if child.is_dir():
                import shutil

                shutil.rmtree(child)
            else:
                child.unlink()

    proc = MatpowerProcessor().load(Path(input_path))
    return proc.to(
        format,
        out,
        include_only_in_service=include_only_in_service,
        write_manifest=True,
        manifest_version=manifest_version,
    )
