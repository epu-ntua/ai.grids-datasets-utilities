from __future__ import annotations

import re
from pathlib import Path
from typing import Optional, Tuple


def read_text(path: str | Path) -> str:
    return Path(path).read_text(encoding="utf-8", errors="replace")


def write_text(path: str | Path, text: str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def extract_case_name_and_baseMVA(
    case_m_path: str | Path,
) -> Tuple[Optional[str], Optional[float]]:
    text = read_text(case_m_path)
    m_name = re.search(r"function\s+mpc\s*=\s*([A-Za-z0-9_]+)\s*", text)
    m_base = re.search(r"mpc\.baseMVA\s*=\s*([0-9eE\+\-\.]+)\s*;", text)
    name = m_name.group(1) if m_name else None
    base = float(m_base.group(1)) if m_base else None
    return name, base
