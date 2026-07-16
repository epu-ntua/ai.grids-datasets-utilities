from __future__ import annotations

from typing import Dict, Type

from processing_utils.matpower_processor import MatpowerProcessor
from processing_utils.rte7000_opensynth_case_processor import Rte7000OpenSynthProcessor

_PROCESSORS: Dict[str, Type] = {
    # Registry keys correspond to CLI `--from` values.
    "matpower": MatpowerProcessor,
    "rte7000_opensynth": Rte7000OpenSynthProcessor,
    "xiidm": Rte7000OpenSynthProcessor,  # backwards-compatible alias
}


def available_source_formats() -> list[str]:
    """List registered source formats accepted by `get_processor`."""
    return sorted(_PROCESSORS.keys())


def get_processor(source_format: str):
    """Instantiate processor implementation for the requested source format."""
    key = str(source_format).strip().lower()
    if key not in _PROCESSORS:
        raise ValueError(
            f"Unknown source_format={source_format}. Available: {available_source_formats()}"
        )
    return _PROCESSORS[key]()  # instance
