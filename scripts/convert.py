from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _bootstrap_sys_path() -> Path:
    """
    Ensure running from repo without installation:
      - add repo_root and repo_root/src to sys.path
    """
    here = Path(__file__).resolve()
    repo_root = here.parents[1]
    src = repo_root / "src"
    for p in (repo_root, src):
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))
    return repo_root


REPO_ROOT = _bootstrap_sys_path()

from convert_runner import (  # noqa: E402
    ConvertOptions,
    add_common_arguments,
    run_conversion,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="convert.py",
        description="Convert datasets from source formats to ML-friendly artifacts.",
    )
    return add_common_arguments(parser, required_core=True)


def _to_options(args: argparse.Namespace) -> ConvertOptions:
    return ConvertOptions(
        source_format=args.source_format,
        target_format=args.target_format,
        input_value=args.input_value,
        dataset=args.dataset,
        output_dir=args.output_dir,
        force=bool(args.force),
        dry_run=bool(args.dry_run),
        include_only_in_service=bool(args.include_only_in_service),
        assign_slack=bool(args.assign_slack),
        slack_bus_id=args.slack_bus_id,
    )


def main() -> int:
    args = _build_parser().parse_args()
    result = run_conversion(repo_root=REPO_ROOT, opts=_to_options(args))
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
