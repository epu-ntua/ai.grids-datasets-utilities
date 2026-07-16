from __future__ import annotations

import datetime as _dt
import hashlib
import math
from pathlib import Path
from typing import Any, List, Optional


def sha256_file(path: str | Path, chunk_size: int = 1024 * 1024) -> str:
    p = Path(path)
    h = hashlib.sha256()
    with p.open("rb") as f:
        while True:
            b = f.read(chunk_size)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def iso_now() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def localname(tag: str) -> str:
    if tag.startswith("{") and "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def ns_uri(tag: str) -> Optional[str]:
    if tag.startswith("{") and "}" in tag:
        return tag[1:].split("}", 1)[0]
    return None


def safe_float_opt(x: Any) -> Optional[float]:
    if x is None:
        return None
    try:
        v = float(x)
        if math.isnan(v) or math.isinf(v):
            return None
        return v
    except Exception:
        return None


def safe_float_strict(x: str) -> float:
    v = float(x)
    if math.isnan(v) or math.isinf(v):
        raise ValueError("NaN/inf not allowed")
    return v


def safe_int_strict(x: str) -> int:
    return int(x)


def ensure_list(obj: Any) -> List[Any]:
    return obj if isinstance(obj, list) else []
