from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional, Tuple


def detect_iidm_namespace_and_version(
    xiidm_path: str | Path,
) -> Tuple[Optional[str], Optional[str]]:
    """
    Reads the root tag only (streaming) and returns (namespace_uri, version_from_uri_tail).
    """
    p = Path(xiidm_path)
    ctx = ET.iterparse(str(p), events=("start",))
    for _event, elem in ctx:
        tag = elem.tag
        if isinstance(tag, str) and tag.startswith("{") and "}" in tag:
            ns = tag[1:].split("}", 1)[0]
            ver = ns.rstrip("/").split("/")[-1] if ns else None
            return ns, ver
        return None, None
    return None, None
