from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

import pandas as pd

PathLike = Union[str, Path]


# ----------------------------
# Text / column normalization helpers (shared across Excel dataloaders)
# ----------------------------


def normalize_text_token(x: Any) -> str:
    """
    Normalization used for robust column matching:
      - None -> ""
      - replace NBSP with space
      - collapse whitespace
      - strip + lower
    """
    s = "" if x is None else str(x)
    s = s.replace("\u00a0", " ")
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s


def make_unique_columns(cols: Iterable[Any]) -> List[str]:
    """
    Uniquify columns while preserving order.
    Empty headers become 'col', then 'col_1', 'col_2', ...
    """
    raw: List[str] = []
    for c in cols:
        raw.append("" if c is None else str(c))

    seen: Dict[str, int] = {}
    out: List[str] = []
    for c in raw:
        base = c if c else "col"
        if base not in seen:
            seen[base] = 0
            out.append(base)
        else:
            seen[base] += 1
            out.append(f"{base}_{seen[base]}")
    return out


def clean_excel_columns(cols: Iterable[Any]) -> List[str]:
    """
    Stronger version of the previous _clean_columns:
      - strips
      - replaces NBSP
      - collapses whitespace
      - turns "Unnamed:*" into empty
      - then enforces uniqueness
    """
    cleaned: List[str] = []
    for c in cols:
        if c is None:
            cleaned.append("")
            continue
        s = str(c)
        s = s.replace("\u00a0", " ")
        s = re.sub(r"\s+", " ", s).strip()
        if s.startswith("Unnamed:"):
            s = ""
        cleaned.append(s)
    return make_unique_columns(cleaned)


def normalize_dataframe_columns(df: pd.DataFrame) -> pd.DataFrame:
    """
    Apply clean_excel_columns to a DataFrame (copying).
    """
    out = df.copy()
    out.columns = clean_excel_columns(out.columns)
    return out


def find_column(df: pd.DataFrame, candidates: List[str]) -> Optional[str]:
    """
    Match one of 'candidates' to an actual df column using normalize_text_token().
    Returns the original column name if found, else None.
    """
    cmap: Dict[str, str] = {}
    for c in df.columns:
        k = normalize_text_token(c)
        # keep first occurrence
        if k not in cmap:
            cmap[k] = c

    for cand in candidates:
        k = normalize_text_token(cand)
        if k in cmap:
            return cmap[k]
    return None


def normalize_first_unnamed_index_column(
    df: pd.DataFrame, *, index_name: str = "idx"
) -> pd.DataFrame:
    """
    Common Excel export pattern: first column is an unnamed index.
    If it looks like an index, rename it to index_name (default 'idx').

    Works with either raw headers ("Unnamed: 0") or after cleaning ("col", "col_1", "").
    """
    if df.shape[1] == 0:
        return df

    first = df.columns[0]
    first_s = "" if first is None else str(first)
    first_tok = normalize_text_token(first_s)

    # Accept both pre-clean and post-clean forms
    looks_unnamed = (
        first_s.strip() == ""
        or first_tok in {"unnamed: 0", "col", "col_1", "index"}
        or first_s == "Unnamed: 0"
    )

    def _values_look_like_index(col: pd.Series) -> bool:
        v = pd.to_numeric(col, errors="coerce")
        # require most values numeric
        if v.notna().mean() < 0.8:
            return False

        vv = v.dropna()
        if vv.empty:
            return False

        # integer-like
        if ((vv - vv.round()).abs() > 1e-9).any():
            return False

        vi = vv.round().astype("int64")
        # uniqueness
        if vi.duplicated().any():
            return False

        # monotonic (either direction)
        return bool(vi.is_monotonic_increasing or vi.is_monotonic_decreasing)

    if looks_unnamed and first != index_name and _values_look_like_index(df.iloc[:, 0]):
        out = df.copy()
        out = out.rename(columns={out.columns[0]: index_name})
        return out

    return df


# ----------------------------
# Core stats / previews
# ----------------------------


def nonempty_stats(df: pd.DataFrame) -> Dict[str, Any]:
    """Return simple occupancy metrics used in sheet overviews."""
    r, c = df.shape
    if r == 0 or c == 0:
        return {"rows": r, "cols": c, "nonempty_cells": 0, "nonempty_pct": 0.0}
    nonempty = int(df.notna().sum().sum())
    pct = 100.0 * nonempty / float(r * c)
    return {"rows": r, "cols": c, "nonempty_cells": nonempty, "nonempty_pct": pct}


def trim_empty(df: pd.DataFrame) -> pd.DataFrame:
    """Drop fully-empty rows and columns."""
    return df.dropna(axis=0, how="all").dropna(axis=1, how="all")


def tsv_preview(
    df: pd.DataFrame,
    *,
    rows: int = 25,
    cols: int = 15,
    include_header: bool = True,
    include_index: bool = False,
) -> str:
    """Create a compact TSV preview block for logs/debug UIs."""
    block = df.iloc[:rows, :cols].copy()
    block = block.fillna("")
    return block.to_csv(
        sep="\t",
        index=include_index,
        header=include_header,
        lineterminator="\n",
    ).rstrip("\n")


# ----------------------------
# Workbook I/O
# ----------------------------


def open_workbook(excel_path: PathLike) -> pd.ExcelFile:
    """Open workbook with existence validation."""
    p = Path(excel_path)
    if not p.exists():
        raise FileNotFoundError(p)
    return pd.ExcelFile(p)


def list_sheets(excel_path: PathLike) -> List[str]:
    """List sheet names from an Excel workbook."""
    xl = open_workbook(excel_path)
    return list(xl.sheet_names)


def read_sheet_raw(
    excel_path: PathLike,
    sheet_name: str,
    *,
    dtype: Any = object,
    engine: Optional[str] = None,
) -> pd.DataFrame:
    """
    Raw read: no header inference; shows the worksheet exactly as a grid.
    """
    return pd.read_excel(
        excel_path,
        sheet_name=sheet_name,
        header=None,
        dtype=dtype,
        engine=engine,
    )


def _row_header_score(row: Sequence[Any]) -> float:
    """
    Heuristic header score:
      - prefer rows with many non-empty cells
      - prefer many unique non-empty tokens
      - penalize mostly numeric rows
    """
    vals = [v for v in row if pd.notna(v)]
    if not vals:
        return -1e9

    as_str = [str(v).strip() for v in vals if str(v).strip() != ""]
    if not as_str:
        return -1e9

    nonempty = len(as_str)
    unique = len(set(as_str))

    numeric_like = 0
    for s in as_str:
        try:
            float(s)
            numeric_like += 1
        except Exception:
            pass

    numeric_frac = numeric_like / max(1, nonempty)
    uniq_frac = unique / max(1, nonempty)

    return (nonempty * 1.0) + (uniq_frac * 10.0) - (numeric_frac * 6.0)


def detect_header_row(
    raw_df: pd.DataFrame,
    *,
    max_scan_rows: int = 50,
    min_nonempty: int = 3,
    min_unique_frac: float = 0.6,
) -> Optional[int]:
    """
    Detect a likely header row from a raw (header=None) dataframe.
    Returns row index or None.
    """
    scan_rows = min(max_scan_rows, len(raw_df))
    best_pos = None
    best_score = -1e18

    for i in range(scan_rows):
        row = raw_df.iloc[i, :].tolist()
        score = _row_header_score(row)
        if score > best_score:
            best_score = score
            best_pos = i

    if best_pos is None:
        return None

    candidate = raw_df.iloc[best_pos, :].tolist()
    vals = [str(v).strip() for v in candidate if pd.notna(v) and str(v).strip() != ""]
    if len(vals) < min_nonempty:
        return None
    uniq_frac = len(set(vals)) / max(1, len(vals))
    if uniq_frac < min_unique_frac:
        return None

    return int(raw_df.index[best_pos])


def read_sheet_table(
    excel_path: PathLike,
    sheet_name: str,
    *,
    header: Union[int, None, str] = "auto",
    engine: Optional[str] = None,
    dtype: Any = object,
    convert_dtypes: bool = True,
    trim_fully_empty: bool = True,
) -> Tuple[pd.DataFrame, Optional[int]]:
    """
    Table read:
      - header="auto": detect header row from the raw grid
      - header=int/None: pass through to pandas
    Returns (df, header_row_index_used_or_None).
    """
    # `auto` path: inspect raw sheet first to infer most likely header row.
    if header == "auto":
        raw = read_sheet_raw(excel_path, sheet_name, dtype=object, engine=engine)
        if trim_fully_empty:
            raw = trim_empty(raw)
        h = detect_header_row(raw)
        if h is None:
            # Fallback to headerless read when no robust header candidate exists.
            df = pd.read_excel(
                excel_path,
                sheet_name=sheet_name,
                header=None,
                dtype=dtype,
                engine=engine,
            )
            if trim_fully_empty:
                df = trim_empty(df)
            if convert_dtypes:
                df = df.convert_dtypes()
            return df, None

        df = pd.read_excel(
            excel_path, sheet_name=sheet_name, header=h, dtype=dtype, engine=engine
        )
        df.columns = clean_excel_columns(df.columns)
        if trim_fully_empty:
            df = trim_empty(df)
        if convert_dtypes:
            df = df.convert_dtypes()
        return df, int(h)

    # Explicit header mode (int/None) delegates semantics to pandas.
    df = pd.read_excel(
        excel_path, sheet_name=sheet_name, header=header, dtype=dtype, engine=engine
    )
    if header is not None:
        df.columns = clean_excel_columns(df.columns)
    if trim_fully_empty:
        df = trim_empty(df)
    if convert_dtypes:
        df = df.convert_dtypes()
    return df, (int(header) if isinstance(header, int) else None)


def workbook_overview(
    excel_path: PathLike,
    *,
    trim_fully_empty: bool = True,
) -> pd.DataFrame:
    """
    Per-sheet grid stats (raw, header=None) so you can quickly see sheet shapes.
    """
    xl = open_workbook(excel_path)
    rows: List[Dict[str, Any]] = []
    for s in xl.sheet_names:
        raw = pd.read_excel(xl, sheet_name=s, header=None, dtype=object)
        if trim_fully_empty:
            raw = trim_empty(raw)
        st = nonempty_stats(raw)
        rows.append({"sheet": s, **st})
    return pd.DataFrame(rows).sort_values(["sheet"]).reset_index(drop=True)


# ----------------------------
# Casting helpers (generic)
# ----------------------------


def to_numeric_series(s: pd.Series, *, kind: str) -> pd.Series:
    """
    kind: 'float' or 'int'
    Uses pandas nullable dtypes where possible.
    """
    if kind == "float":
        out = pd.to_numeric(s, errors="coerce")
        return out.astype("Float64")
    if kind == "int":
        out = pd.to_numeric(s, errors="coerce")
        return out.astype("Int64")
    raise ValueError(f"Unknown kind={kind}")


def apply_dtype_spec(
    df: pd.DataFrame,
    spec: Dict[str, str],
) -> pd.DataFrame:
    """
    Apply a {column: dtype_string} spec to a dataframe (best-effort).
    Supported spec values (case-insensitive):
      - object, string
      - bool, boolean
      - float, float64
      - int, int64, uint32, uint64 (mapped to nullable Int64)
    """
    out = df.copy()

    for col, dt in spec.items():
        if col not in out.columns:
            continue
        dt_l = str(dt).lower()

        if dt_l == "object":
            out[col] = out[col].astype("object")
        elif dt_l == "string":
            out[col] = out[col].astype("string")
        elif dt_l in {"bool", "boolean"}:
            out[col] = out[col].astype("boolean")
        elif dt_l.startswith("float"):
            out[col] = to_numeric_series(out[col], kind="float")
        elif dt_l.startswith("int") or dt_l.startswith("uint"):
            out[col] = to_numeric_series(out[col], kind="int")
        else:
            try:
                out[col] = out[col].astype(dt)
            except Exception:
                pass

    return out


def parse_dtypes_sheet(dtypes_df: pd.DataFrame) -> Dict[str, Dict[str, str]]:
    """
    Parse a conventional ``dtypes`` sheet with columns: element, column, dtype.
    Returns ``{element: {column: dtype}}``.
    """
    required = {"element", "column", "dtype"}
    if not required.issubset(set(dtypes_df.columns)):
        return {}

    out: Dict[str, Dict[str, str]] = {}
    for _, r in dtypes_df.iterrows():
        element = r.get("element")
        col = r.get("column")
        dt = r.get("dtype")
        if pd.isna(element) or pd.isna(col) or pd.isna(dt):
            continue
        element_s = str(element).strip()
        col_s = str(col).strip()
        dt_s = str(dt).strip()
        out.setdefault(element_s, {})[col_s] = dt_s
    return out


def apply_declared_dtypes(
    tables: Dict[str, pd.DataFrame],
    *,
    dtypes_sheet_name: str = "dtypes",
) -> Dict[str, pd.DataFrame]:
    """
    Apply dtype declarations from a workbook ``dtypes`` sheet to matching tables.
    Mutates and returns ``tables`` for convenient chaining.
    """
    if dtypes_sheet_name not in tables:
        return tables

    spec = parse_dtypes_sheet(tables[dtypes_sheet_name])
    for element, colspec in spec.items():
        if element in tables:
            tables[element] = apply_dtype_spec(tables[element], colspec)
    return tables


def dtype_report_from_tables(
    tables: Dict[str, pd.DataFrame],
    *,
    dtype_compatible_fn: Any,
    dtypes_sheet_name: str = "dtypes",
) -> Dict[str, pd.DataFrame]:
    """
    Compare dtype declarations from ``dtypes`` against actual dataframe dtypes.
    ``dtype_compatible_fn`` is injected to avoid a circular import with schema.py.
    """
    if dtypes_sheet_name not in tables:
        return {}

    spec = parse_dtypes_sheet(tables[dtypes_sheet_name])
    reports: Dict[str, pd.DataFrame] = {}

    for element, colspec in spec.items():
        if element not in tables:
            continue
        df = tables[element]
        rows: List[Dict[str, Any]] = []
        for col, expected in colspec.items():
            if col not in df.columns:
                rows.append(
                    {
                        "column": col,
                        "expected": expected,
                        "actual": None,
                        "match": False,
                        "status": "missing_column",
                    }
                )
                continue
            actual = str(df[col].dtype)
            match = dtype_compatible_fn(str(expected), actual)
            rows.append(
                {
                    "column": col,
                    "expected": str(expected),
                    "actual": actual,
                    "match": bool(match),
                    "status": "ok" if match else "mismatch",
                }
            )
        reports[element] = (
            pd.DataFrame(rows).sort_values(["status", "column"]).reset_index(drop=True)
        )

    return reports
