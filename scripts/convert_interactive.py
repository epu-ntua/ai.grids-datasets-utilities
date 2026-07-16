from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from pathlib import Path
from typing import Iterable

from convert_runner import (
    SOURCE_FORMATS,
    ConvertOptions,
    add_common_arguments,
    default_output_dir,
    get_supported_target_formats,
    resolve_input_path,
    run_conversion,
)


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


class _Style:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def _wrap(self, code: str, text: str) -> str:
        if not self.enabled:
            return text
        return f"\033[{code}m{text}\033[0m"

    def bold(self, text: str) -> str:
        return self._wrap("1", text)

    def dim(self, text: str) -> str:
        return self._wrap("2", text)

    def accent(self, text: str) -> str:
        return self._wrap("36", text)

    def success(self, text: str) -> str:
        return self._wrap("32", text)

    def warning(self, text: str) -> str:
        return self._wrap("33", text)

    def danger(self, text: str) -> str:
        return self._wrap("31", text)


def _supports_color(no_color: bool) -> bool:
    if no_color:
        return False
    if os.environ.get("NO_COLOR") is not None:
        return False
    return sys.stdout.isatty()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="convert_interactive.py",
        description=(
            "Run conversions with a guided interactive workflow or pass the usual flags directly "
            "for scripted usage."
        ),
    )
    add_common_arguments(parser, required_core=False)
    parser.add_argument(
        "--no_color",
        action="store_true",
        help="Disable ANSI colors in interactive mode.",
    )
    return parser


def _has_required_args(args: argparse.Namespace) -> bool:
    required_values = (args.source_format, args.target_format, args.input_value)
    return all(value not in (None, "") for value in required_values)


def _print_banner(style: _Style) -> None:
    width = 78
    rule = "═" * width
    print(style.accent(rule))
    print(style.bold("Power Grid Dataset Conversion"))
    print(
        style.dim(
            "Guided workflow for selecting source, target, paths, and conversion options."
        )
    )
    print(style.accent(rule))


def _print_section(title: str, style: _Style) -> None:
    print()
    print(style.bold(title))
    print(style.dim("-" * len(title)))


def _print_note(message: str, style: _Style) -> None:
    print(style.dim(message))


def _prompt_choice(
    label: str,
    options: Iterable[str],
    *,
    style: _Style,
    default: str | None = None,
) -> str:
    items = list(options)
    if not items:
        raise ValueError(f"No options available for prompt: {label}")

    while True:
        print(style.bold(label))
        for idx, option in enumerate(items, start=1):
            marker = "•"
            suffix = ""
            if default == option:
                suffix = style.dim("  [default]")
            print(f"  {style.accent(str(idx).rjust(2))} {marker} {option}{suffix}")

        raw = input(
            "Select option number" + (f" [{default}]" if default else "") + ": "
        ).strip()
        if not raw and default is not None:
            return default
        if raw.isdigit():
            choice_idx = int(raw)
            if 1 <= choice_idx <= len(items):
                return items[choice_idx - 1]
        print(style.warning("Invalid selection. Enter one of the listed numbers."))
        print()


def _prompt_text(
    label: str,
    *,
    style: _Style,
    default: str | None = None,
    optional: bool = False,
    empty_returns_none: bool = False,
    extra_note: str | None = None,
) -> str | None:
    if extra_note:
        _print_note(extra_note, style)

    suffix = f" [{default}]" if default is not None else ""
    while True:
        raw = input(f"{label}{suffix}: ").strip()
        if raw:
            return raw
        if default is not None:
            return default
        if optional:
            return None if empty_returns_none else ""
        print(style.warning("A value is required."))


def _prompt_bool(label: str, *, style: _Style, default: bool = False) -> bool:
    suffix = "Y/n" if default else "y/N"
    while True:
        raw = input(f"{label} [{suffix}]: ").strip().lower()
        if not raw:
            return default
        if raw in {"y", "yes"}:
            return True
        if raw in {"n", "no"}:
            return False
        print(style.warning("Please answer with y or n."))


def _prompt_optional_int(
    label: str,
    *,
    style: _Style,
    default: int | None = None,
) -> int | None:
    default_text = str(default) if default is not None else None
    while True:
        raw = _prompt_text(
            label,
            style=style,
            default=default_text,
            optional=True,
            empty_returns_none=True,
        )
        if raw is None:
            return None
        try:
            return int(raw)
        except ValueError:
            print(style.warning("Enter an integer value or leave it blank."))


def _to_options(args: argparse.Namespace) -> ConvertOptions:
    output_dir = args.output_dir
    if isinstance(output_dir, str) and not output_dir.strip():
        output_dir = None

    return ConvertOptions(
        source_format=args.source_format,
        target_format=args.target_format,
        input_value=args.input_value,
        dataset=args.dataset,
        output_dir=output_dir,
        force=bool(args.force),
        dry_run=bool(args.dry_run),
        include_only_in_service=bool(args.include_only_in_service),
        assign_slack=bool(args.assign_slack),
        slack_bus_id=args.slack_bus_id,
    )


def _prefill_or_none(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _collect_interactively(
    initial_args: argparse.Namespace, *, repo_root: Path, style: _Style
) -> ConvertOptions:
    _print_banner(style)

    _print_section("1. Source selection", style)
    source_default = (
        initial_args.source_format
        if initial_args.source_format in SOURCE_FORMATS
        else None
    )
    source_format = _prompt_choice(
        "Choose a source format",
        SOURCE_FORMATS,
        style=style,
        default=source_default,
    )

    target_choices = get_supported_target_formats(source_format)
    target_default = (
        initial_args.target_format
        if initial_args.target_format in target_choices
        else None
    )
    if initial_args.target_format and target_default is None:
        print(
            style.warning(
                f"'{initial_args.target_format}' is not supported for '{source_format}'. "
                f"Available targets: {target_choices}"
            )
        )
    _print_section("2. Target selection", style)
    _print_note(
        f"Supported targets for {source_format}: {', '.join(target_choices)}", style
    )
    target_format = _prompt_choice(
        "Choose a target format",
        target_choices,
        style=style,
        default=target_default,
    )

    _print_section("3. Input and output", style)
    input_value = _prompt_text(
        "Input path or dataset-relative path",
        style=style,
        default=_prefill_or_none(initial_args.input_value),
        extra_note="Absolute paths are accepted. Relative paths are resolved against DATASETS_ROOT.",
    )
    dataset = _prompt_text(
        "Dataset key (optional)",
        style=style,
        default=_prefill_or_none(initial_args.dataset),
        optional=True,
        empty_returns_none=True,
        extra_note="Use this only when --input should be resolved under DATASETS_ROOT/<dataset>/...",
    )
    output_dir = _prompt_text(
        "Output directory (optional)",
        style=style,
        default=_prefill_or_none(initial_args.output_dir),
        optional=True,
        empty_returns_none=True,
        extra_note=f"Leave blank to use the default: {default_output_dir(repo_root)}",
    )

    _print_section("4. Conversion options", style)
    dry_run = _prompt_bool(
        "Preview conversion without writing files",
        style=style,
        default=bool(initial_args.dry_run),
    )
    force = False
    if not dry_run:
        force = _prompt_bool(
            "Overwrite output directory if needed",
            style=style,
            default=bool(initial_args.force),
        )
    include_only_in_service = _prompt_bool(
        "Export only in-service elements when supported",
        style=style,
        default=bool(initial_args.include_only_in_service),
    )
    assign_slack = _prompt_bool(
        "Assign a slack bus when supported",
        style=style,
        default=bool(initial_args.assign_slack),
    )
    slack_bus_id = None
    if assign_slack:
        slack_bus_id = _prompt_optional_int(
            "Slack bus id (optional)",
            style=style,
            default=initial_args.slack_bus_id,
        )

    opts = ConvertOptions(
        source_format=source_format,
        target_format=target_format,
        input_value=input_value or "",
        dataset=dataset,
        output_dir=output_dir,
        force=force,
        dry_run=dry_run,
        include_only_in_service=include_only_in_service,
        assign_slack=assign_slack,
        slack_bus_id=slack_bus_id,
    )

    _print_section("5. Review", style)
    _print_summary(opts, repo_root=repo_root, style=style)
    print()
    print(style.dim("Equivalent CLI command:"))
    print(style.accent(_render_cli_command(opts)))
    print()
    if not _prompt_bool("Proceed with this conversion", style=style, default=True):
        raise SystemExit("Cancelled.")
    return opts


def _render_cli_command(opts: ConvertOptions) -> str:
    parts = [
        "python",
        "scripts/convert.py",
        "--from",
        opts.source_format,
        "--to",
        opts.target_format,
        "--input",
        opts.input_value,
    ]
    if opts.dataset:
        parts.extend(["--dataset", opts.dataset])
    if opts.output_dir:
        parts.extend(["--output_dir", str(opts.output_dir)])
    if opts.force:
        parts.append("--force")
    if opts.dry_run:
        parts.append("--dry_run")
    if opts.include_only_in_service:
        parts.append("--include_only_in_service")
    if opts.assign_slack:
        parts.append("--assign_slack")
    if opts.slack_bus_id is not None:
        parts.extend(["--slack_bus_id", str(opts.slack_bus_id)])
    return " ".join(shlex.quote(part) for part in parts)


def _safe_resolve_input(repo_root: Path, opts: ConvertOptions) -> str:
    try:
        resolved = resolve_input_path(
            repo_root=repo_root,
            input_value=opts.input_value,
            dataset=opts.dataset,
        )
        return str(resolved)
    except Exception as exc:  # best-effort preview only
        return f"<unresolved: {exc}>"


def _effective_output_dir(repo_root: Path, opts: ConvertOptions) -> str:
    if opts.output_dir is None or not str(opts.output_dir).strip():
        return str(default_output_dir(repo_root))
    return str(Path(opts.output_dir).expanduser().resolve())


def _print_summary(opts: ConvertOptions, *, repo_root: Path, style: _Style) -> None:
    rows = [
        ("Source format", opts.source_format),
        ("Target format", opts.target_format),
        ("Input", opts.input_value),
        ("Resolved input", _safe_resolve_input(repo_root, opts)),
        ("Dataset", opts.dataset or "<none>"),
        ("Output directory", _effective_output_dir(repo_root, opts)),
        ("Dry run", str(bool(opts.dry_run))),
        ("Force overwrite", str(bool(opts.force))),
        ("In-service only", str(bool(opts.include_only_in_service))),
        ("Assign slack", str(bool(opts.assign_slack))),
        (
            "Slack bus id",
            str(opts.slack_bus_id) if opts.slack_bus_id is not None else "<none>",
        ),
    ]
    width = max(len(key) for key, _ in rows)
    for key, value in rows:
        print(f"  {style.bold(key.ljust(width))} : {value}")


def _run(opts: ConvertOptions, *, repo_root: Path) -> int:
    result = run_conversion(repo_root=repo_root, opts=opts)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    style = _Style(enabled=_supports_color(bool(args.no_color)))

    if _has_required_args(args):
        return _run(_to_options(args), repo_root=REPO_ROOT)

    if not sys.stdin.isatty():
        parser.error(
            "Missing required arguments for non-interactive use. Provide --from, --to, and --input, "
            "or run this command in a terminal to use the guided workflow."
        )

    opts = _collect_interactively(args, repo_root=REPO_ROOT, style=style)
    return _run(opts, repo_root=REPO_ROOT)


if __name__ == "__main__":
    raise SystemExit(main())
