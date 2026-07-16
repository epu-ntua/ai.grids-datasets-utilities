from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from dataset_utils.hashing import sha256_file

# ----------------------------
# Matpower canonical column names (v2)
# ----------------------------

_BUS_COLS_V2 = [
    "BUS_I",
    "BUS_TYPE",
    "PD",
    "QD",
    "GS",
    "BS",
    "BUS_AREA",
    "VM",
    "VA",
    "BASE_KV",
    "ZONE",
    "VMAX",
    "VMIN",
]

_GEN_COLS_V2 = [
    "GEN_BUS",
    "PG",
    "QG",
    "QMAX",
    "QMIN",
    "VG",
    "MBASE",
    "GEN_STATUS",
    "PMAX",
    "PMIN",
    "PC1",
    "PC2",
    "QC1MIN",
    "QC1MAX",
    "QC2MIN",
    "QC2MAX",
    "RAMP_AGC",
    "RAMP_10",
    "RAMP_30",
    "RAMP_Q",
    "APF",
]

_BRANCH_COLS_V2 = [
    "F_BUS",
    "T_BUS",
    "BR_R",
    "BR_X",
    "BR_B",
    "RATE_A",
    "RATE_B",
    "RATE_C",
    "TAP",
    "SHIFT",
    "BR_STATUS",
    "ANGMIN",
    "ANGMAX",
]

_GENCOST_HEAD = ["MODEL", "STARTUP", "SHUTDOWN", "NCOST"]


# ----------------------------
# Text preprocessing
# ----------------------------


def strip_matlab_comments(text: str) -> str:
    # Remove everything after '%' per line (Matlab comments).
    out_lines: List[str] = []
    for ln in text.splitlines():
        out_lines.append(ln.split("%", 1)[0])
    return "\n".join(out_lines)


def remove_line_continuations(text: str) -> str:
    # Matpower cases often use "..." continuation tokens
    return text.replace("...", " ")


# ----------------------------
# Scalar parsing
# ----------------------------


def parse_mpc_version(text: str) -> Optional[str]:
    """Extract `mpc.version` scalar if present."""
    m = re.search(
        r"mpc\s*\.\s*version\s*=\s*['\"]([^'\"]+)['\"]\s*;", text, flags=re.IGNORECASE
    )
    return m.group(1).strip() if m else None


def parse_mpc_baseMVA(text: str) -> Optional[float]:
    """Extract numeric `mpc.baseMVA` scalar if present and parseable."""
    m = re.search(
        r"mpc\s*\.\s*baseMVA\s*=\s*([+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s*;",
        text,
        flags=re.IGNORECASE,
    )
    if not m:
        return None
    try:
        return float(m.group(1))
    except Exception:
        return None


# ----------------------------
# Matrix extraction / conversion
# ----------------------------


def extract_matrix_blocks(text: str) -> Dict[str, str]:
    """
    Extracts blocks of the form:
      mpc.<name> = [ ... ];
    Returns: {name: "<inside brackets>"}
    """
    blocks: Dict[str, str] = {}

    current: Optional[str] = None
    buf: List[str] = []

    start_re = re.compile(
        r"^\s*mpc\s*\.\s*([A-Za-z_]\w*)\s*=\s*\[\s*(.*)$", flags=re.IGNORECASE
    )

    for ln in text.splitlines():
        if current is None:
            m = start_re.match(ln)
            if not m:
                continue
            current = m.group(1)
            rest = m.group(2)

            if "];" in rest:
                inside = rest.split("];", 1)[0]
                blocks[current] = inside
                current = None
                buf = []
            else:
                buf = [rest] if rest.strip() else []
        else:
            if "];" in ln:
                before = ln.split("];", 1)[0]
                if before.strip():
                    buf.append(before)
                blocks[current] = "\n".join(buf)
                current = None
                buf = []
            else:
                buf.append(ln)

    return blocks


def parse_number(tok: str) -> float:
    """Parse Matpower numeric token, including inf/-inf/nan spellings."""
    t = tok.strip()
    if t == "":
        return float("nan")
    tl = t.lower()
    if tl in {"inf", "+inf"}:
        return float("inf")
    if tl == "-inf":
        return float("-inf")
    if tl == "nan":
        return float("nan")
    return float(t)


def rows_to_table(block: str) -> List[List[float]]:
    # Matpower/MATLAB allows rows separated by either ';' OR newlines.
    # Tokens separated by whitespace and/or commas.
    b = block.replace("\r", "\n")

    # Split into row fragments on ';' or newline.
    parts = [p.strip() for p in re.split(r";|\n", b)]

    rows: List[List[float]] = []
    for p in parts:
        if not p:
            continue
        toks = [t for t in re.split(r"[,\s]+", p) if t.strip() != ""]
        if toks:
            rows.append([parse_number(t) for t in toks])
    return rows


def pad_ragged(rows: List[List[float]]) -> List[List[float]]:
    if not rows:
        return []
    m = max(len(r) for r in rows)
    out: List[List[float]] = []
    for r in rows:
        out.append(r + [float("nan")] * (m - len(r)) if len(r) < m else r)
    return out


def default_cols(prefix: str, n: int) -> List[str]:
    return [f"{prefix}{i}" for i in range(1, n + 1)]


def matrix_to_df(name: str, rows: List[List[float]]) -> pd.DataFrame:
    """
    Map matrix rows to canonical column names for known Matpower blocks.

    Unknown/extra columns are preserved with generated names to avoid data loss.
    """
    rows = pad_ragged(rows)
    if not rows:
        return pd.DataFrame()

    ncols = len(rows[0])
    lname = name.lower()

    if lname == "bus":
        cols = _BUS_COLS_V2[:ncols] + default_cols(
            "BUS_EXTRA_", max(0, ncols - len(_BUS_COLS_V2))
        )
    elif lname == "gen":
        cols = _GEN_COLS_V2[:ncols] + default_cols(
            "GEN_EXTRA_", max(0, ncols - len(_GEN_COLS_V2))
        )
    elif lname == "branch":
        cols = _BRANCH_COLS_V2[:ncols] + default_cols(
            "BR_EXTRA_", max(0, ncols - len(_BRANCH_COLS_V2))
        )
    elif lname == "gencost":
        if ncols <= 4:
            cols = _GENCOST_HEAD[:ncols]
        else:
            extra = [f"COST_{i}" for i in range(1, (ncols - 4) + 1)]
            cols = _GENCOST_HEAD + extra
    else:
        cols = default_cols("COL_", ncols)

    return pd.DataFrame(rows, columns=cols[:ncols]).convert_dtypes()


def attach_standard_keys(tables: Dict[str, pd.DataFrame]) -> Dict[str, pd.DataFrame]:
    """
    Adds stable cross-table keys:
      - bus.bus_id, bus.in_service
      - gen.bus_id, gen.in_service
      - branch.from_bus, branch.to_bus, branch.in_service
    """
    out = {k: v.copy() for k, v in tables.items()}

    bus = out.get("bus")
    if isinstance(bus, pd.DataFrame) and not bus.empty and "BUS_I" in bus.columns:
        bus["bus_id"] = pd.to_numeric(bus["BUS_I"], errors="coerce").astype("Int64")
        if "BUS_TYPE" in bus.columns:
            bus["in_service"] = (
                pd.to_numeric(bus["BUS_TYPE"], errors="coerce").astype("Int64") != 4
            ).astype("boolean")

    gen = out.get("gen")
    if isinstance(gen, pd.DataFrame) and not gen.empty and "GEN_BUS" in gen.columns:
        gen["bus_id"] = pd.to_numeric(gen["GEN_BUS"], errors="coerce").astype("Int64")
        if "GEN_STATUS" in gen.columns:
            gen["in_service"] = (
                pd.to_numeric(gen["GEN_STATUS"], errors="coerce").astype("Int64") == 1
            ).astype("boolean")

    branch = out.get("branch")
    if isinstance(branch, pd.DataFrame) and not branch.empty:
        if "F_BUS" in branch.columns:
            branch["from_bus"] = pd.to_numeric(branch["F_BUS"], errors="coerce").astype(
                "Int64"
            )
        if "T_BUS" in branch.columns:
            branch["to_bus"] = pd.to_numeric(branch["T_BUS"], errors="coerce").astype(
                "Int64"
            )
        if "BR_STATUS" in branch.columns:
            branch["in_service"] = (
                pd.to_numeric(branch["BR_STATUS"], errors="coerce").astype("Int64") == 1
            ).astype("boolean")

    out["bus"] = bus if isinstance(bus, pd.DataFrame) else out.get("bus")
    out["gen"] = gen if isinstance(gen, pd.DataFrame) else out.get("gen")
    out["branch"] = branch if isinstance(branch, pd.DataFrame) else out.get("branch")
    return out


@lru_cache(maxsize=16)
def _parse_matpower_case_cached(path_str: str) -> Dict[str, Any]:
    """Cached Matpower parse keyed by absolute path string."""
    path = Path(path_str)
    text = path.read_text(encoding="utf-8", errors="ignore")
    text = remove_line_continuations(strip_matlab_comments(text))

    version = parse_mpc_version(text)
    base_mva = parse_mpc_baseMVA(text)

    blocks = extract_matrix_blocks(text)

    tables: Dict[str, pd.DataFrame] = {}
    # Parse only known core matrices; leave others discoverable via `blocks_present`.
    for key, block in blocks.items():
        if key.lower() in {"bus", "gen", "branch", "gencost"}:
            rows = rows_to_table(block)
            tables[key.lower()] = matrix_to_df(key, rows)

    tables = attach_standard_keys(tables)

    return {
        "scalars": {"version": version, "baseMVA": base_mva},
        "tables": tables,
        "blocks_present": sorted(list(blocks.keys())),
    }


def parse_matpower_case(path: Path, *, use_cache: bool = True) -> Dict[str, Any]:
    """
    Returns:
      {
        "scalars": {"version": str|None, "baseMVA": float|None},
        "tables": {"bus": df, "gen": df, "branch": df, "gencost": df},
        "blocks_present": [ ... ],
      }
    """
    if not path.exists():
        raise FileNotFoundError(path)
    return (
        _parse_matpower_case_cached(str(path))
        if use_cache
        else _parse_matpower_case_cached.__wrapped__(str(path))
    )  # type: ignore[attr-defined]


def matpower_overview(path: Path) -> pd.DataFrame:
    """Return file- and table-level summary suitable for notebook display."""
    p = Path(path)
    parsed = parse_matpower_case(p, use_cache=True)
    tables = parsed["tables"]
    scalars = parsed["scalars"]

    rows: List[Dict[str, Any]] = [
        {"key": "path", "value": str(p)},
        {"key": "size_bytes", "value": int(p.stat().st_size)},
        {"key": "sha256", "value": sha256_file(p)},
        {"key": "mpc.version", "value": scalars.get("version")},
        {"key": "mpc.baseMVA", "value": scalars.get("baseMVA")},
        {"key": "blocks_present", "value": ", ".join(parsed.get("blocks_present", []))},
    ]

    for name in ["bus", "gen", "branch", "gencost"]:
        df = tables.get(name)
        if isinstance(df, pd.DataFrame):
            rows.append({"key": f"{name}.rows", "value": int(df.shape[0])})
            rows.append({"key": f"{name}.cols", "value": int(df.shape[1])})
        else:
            rows.append({"key": f"{name}.rows", "value": 0})
            rows.append({"key": f"{name}.cols", "value": 0})

    return pd.DataFrame(rows)
