from __future__ import annotations


def dtype_compatible(expected: str, actual: str) -> bool:
    """
    Compare dtype labels with pragmatic compatibility rules.

    This function is intentionally permissive for pandas nullable-extension
    dtypes vs legacy dtype strings commonly found in hand-maintained specs.
    """
    e = expected.lower()
    a = actual.lower()

    if e in {"object", "string"} and a in {"object", "string"}:
        return True
    if e in {"bool", "boolean"} and a in {"bool", "boolean"}:
        return True

    # integer families (nullable Int64 vs uint32 etc.)
    if e.startswith(("int", "uint")) and a.startswith(("int", "uint")):
        return True

    # floats
    if e.startswith("float") and a.startswith("float"):
        return True

    return e == a
