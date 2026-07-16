from __future__ import annotations

import collections
import datetime as _dt
import json
import math
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from dataset_utils.hashing import sha256_file

# ----------------------------
# low-level helpers
# ----------------------------


def localname(tag: str) -> str:
    """Strip XML namespace from a tag name."""
    # '{ns}name' -> 'name'
    if tag.startswith("{") and "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def ns_uri(tag: str) -> Optional[str]:
    """Extract XML namespace URI from a namespaced tag, if present."""
    if tag.startswith("{") and "}" in tag:
        return tag[1:].split("}", 1)[0]
    return None


def iso_now() -> str:
    """Timezone-aware ISO timestamp for report metadata."""
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def short(s: str, n: int = 180) -> str:
    """Single-line truncate helper for compact error examples."""
    s = s.replace("\n", "\\n")
    return s if len(s) <= n else s[: n - 3] + "..."


def safe_float(x: str) -> float:
    """Strict float parser rejecting NaN/Inf sentinel values."""
    v = float(x)
    if math.isnan(v) or math.isinf(v):
        raise ValueError("NaN/inf not allowed")
    return v


def safe_int(x: str) -> int:
    """Strict integer parser used in numeric attribute checks."""
    return int(x)


def _schema_version_from_namespace(uri: Optional[str]) -> Optional[str]:
    """Best-effort extraction of IIDM schema version from namespace URI."""
    if not uri:
        return None
    parts = uri.rstrip("/").split("/")
    return parts[-1] if parts else None


# ----------------------------
# main sanity / inspection pass
# ----------------------------
DEFAULT_REQUIRE_VL_CONTEXT = {
    "busbarSection",
    "switch",
    "load",
    "generator",
    "danglingLine",
    "shunt",
    "staticVarCompensator",
}

DEFAULT_NUMERIC_ATTRS = {
    # voltages/limits
    "nominalV",
    "lowVoltageLimit",
    "highVoltageLimit",
    "ratedU1",
    "ratedU2",
    # series parameters
    "r",
    "x",
    # shunt parameters
    "g",
    "b",
    "g1",
    "g2",
    "b1",
    "b2",
    "bMax",
    "bMin",
    # power setpoints / limits
    "p",
    "q",
    "p0",
    "q0",
    "minP",
    "maxP",
    "reactivePowerSetpoint",
    # regulation/voltage
    "voltageSetpoint",
}
DEFAULT_INT_ATTRS = {"node", "node1", "node2", "tapPosition"}


DEFAULT_SAMPLE_SPECS = {
    "substation": ["id", "country", "tso"],
    "voltageLevel": [
        "id",
        "nominalV",
        "topologyKind",
        "lowVoltageLimit",
        "highVoltageLimit",
        "fictitious",
    ],
    "busbarSection": ["id", "name", "node"],
    "switch": [
        "id",
        "name",
        "kind",
        "open",
        "retained",
        "fictitious",
        "node1",
        "node2",
    ],
    "load": ["id", "loadType", "node"],
    "generator": ["id", "energySource", "minP", "maxP", "voltageRegulatorOn", "node"],
    "staticVarCompensator": [
        "id",
        "node",
        "p",
        "q",
        "bMin",
        "bMax",
        "regulationMode",
        "voltageSetpoint",
        "reactivePowerSetpoint",
    ],
    "line": [
        "id",
        "r",
        "x",
        "g1",
        "b1",
        "g2",
        "b2",
        "voltageLevelId1",
        "node1",
        "voltageLevelId2",
        "node2",
    ],
    "twoWindingsTransformer": [
        "id",
        "r",
        "x",
        "g",
        "b",
        "ratedU1",
        "ratedU2",
        "voltageLevelId1",
        "node1",
        "voltageLevelId2",
        "node2",
    ],
    "danglingLine": ["id", "name", "pairingKey", "r", "x", "g", "b", "node"],
}

DEFAULT_ATTRKEY_TAGS = [
    "substation",
    "voltageLevel",
    "busbarSection",
    "switch",
    "load",
    "generator",
    "staticVarCompensator",
    "line",
    "twoWindingsTransformer",
    "danglingLine",
]


def parse_xiidm_sanity(
    xiidm_path: Path,
    *,
    samples_per_tag: int = 15,
    max_issue_examples: int = 50,
    path_prefix_depth: int = 3,
    require_voltagelevel_context: Optional[set[str]] = None,
    numeric_attr_names: Optional[set[str]] = None,
    int_attr_names: Optional[set[str]] = None,
    sample_specs: Optional[dict[str, list[str]]] = None,
    selected_attrkey_tags: Optional[list[str]] = None,
) -> Dict[str, Any]:
    """
    Streaming sanity pass for XIIDM/IIDM XML.
    Returns a JSON-serializable dict (report).
    """
    if not xiidm_path.exists():
        raise FileNotFoundError(xiidm_path)

    require_voltagelevel_context = require_voltagelevel_context or set(
        DEFAULT_REQUIRE_VL_CONTEXT
    )
    numeric_attr_names = numeric_attr_names or set(DEFAULT_NUMERIC_ATTRS)
    int_attr_names = int_attr_names or set(DEFAULT_INT_ATTRS)
    sample_specs = sample_specs or dict(DEFAULT_SAMPLE_SPECS)
    selected_attrkey_tags = selected_attrkey_tags or list(DEFAULT_ATTRKEY_TAGS)

    file_size = xiidm_path.stat().st_size
    file_hash = sha256_file(xiidm_path)

    # Streaming parser keeps memory bounded for large XIIDM files.
    context = ET.iterparse(str(xiidm_path), events=("start", "end", "start-ns"))

    # Global counters and accumulators used in the final JSON report.
    ns_decl_counts = collections.Counter()  # uri -> count
    tag_counts = collections.Counter()  # local tag -> count
    path_counts = collections.Counter()  # prefix path -> count

    # iidm root info
    iidm_ns: Optional[str] = None
    iidm_schema_version: Optional[str] = None
    root_tag_local: Optional[str] = None
    network_meta: Dict[str, Any] = {}

    # ids
    substation_ids: set[str] = set()
    voltagelevel_ids: set[str] = set()
    connectable_ids: set[str] = set()

    # refs (bounded storage not required here; these counts are not massive for this file)
    refs_substation: List[Tuple[str, str, str, str]] = []
    refs_voltagelevel: List[Tuple[str, str, str, str]] = []
    terminalref_connectable: List[Tuple[str, str, str]] = []

    # checks
    context_violations_count = 0
    context_violations_examples: List[Dict[str, Any]] = []

    numeric_errors_count = 0
    numeric_errors_examples: List[Dict[str, Any]] = []

    # topologyKind distribution
    topology_kind_counts = collections.Counter()  # topologyKind -> count

    # attr keys discovery
    attr_keys: Dict[str, set[str]] = {t: set() for t in selected_attrkey_tags}

    # samples
    samples: Dict[str, List[Dict[str, Any]]] = collections.defaultdict(list)

    # nesting context
    stack: List[Tuple[str, Optional[str]]] = []
    current_substation: Optional[str] = None
    current_voltagelevel: Optional[str] = None

    def _push(tag_l: str, elem_id: Optional[str]) -> None:
        nonlocal current_substation, current_voltagelevel
        stack.append((tag_l, elem_id))
        if tag_l == "substation":
            current_substation = elem_id
        elif tag_l == "voltageLevel":
            current_voltagelevel = elem_id

    def _pop_to(tag_l: str) -> None:
        nonlocal current_substation, current_voltagelevel
        while stack:
            t, _ = stack.pop()
            if t == tag_l:
                break
        current_substation = None
        current_voltagelevel = None
        for t, eid in stack:
            if t == "substation":
                current_substation = eid
            elif t == "voltageLevel":
                current_voltagelevel = eid

    def _recompute_context() -> None:
        nonlocal current_substation, current_voltagelevel
        current_substation = None
        current_voltagelevel = None
        for t, eid in stack:
            if t == "substation":
                current_substation = eid
            elif t == "voltageLevel":
                current_voltagelevel = eid

    root_seen = False

    for event, payload in context:
        if event == "start-ns":
            prefix, uri = payload
            ns_decl_counts[uri] += 1
            continue

        elem: ET.Element = payload
        tag_l = localname(elem.tag)

        if event == "start":
            if not root_seen:
                root_seen = True
                root_tag_local = tag_l
                iidm_ns = ns_uri(elem.tag)
                iidm_schema_version = _schema_version_from_namespace(iidm_ns)
                for k in (
                    "caseDate",
                    "forecastDistance",
                    "id",
                    "minimumValidationLevel",
                    "sourceFormat",
                ):
                    if k in elem.attrib:
                        network_meta[k] = elem.attrib.get(k)

            elem_id = elem.attrib.get("id")
            _push(tag_l, elem_id)

            # Path-prefix counting gives a coarse structural profile.
            if path_prefix_depth > 0:
                prefix = "/".join([t for t, _ in stack[:path_prefix_depth]])
                path_counts[prefix] += 1

            # tag attribute keys (selected)
            if tag_l in attr_keys:
                for k in elem.attrib.keys():
                    attr_keys[tag_l].add(k)

            # collect ids
            if tag_l == "substation" and elem_id:
                substation_ids.add(elem_id)
            if tag_l == "voltageLevel" and elem_id:
                voltagelevel_ids.add(elem_id)
                tk = elem.attrib.get("topologyKind")
                if tk is not None:
                    topology_kind_counts[tk] += 1

            # connectables referenced by terminalRef.connectableId
            if (
                tag_l
                in {
                    "load",
                    "generator",
                    "line",
                    "twoWindingsTransformer",
                    "danglingLine",
                    "shunt",
                    "staticVarCompensator",
                    "threeWindingsTransformer",
                    "hvdcLine",
                    "battery",
                }
                and elem_id
            ):
                connectable_ids.add(elem_id)

            # reference capture
            stable_id = elem_id or ""

            if "substationId" in elem.attrib:
                refs_substation.append(
                    (tag_l, stable_id, "substationId", elem.attrib["substationId"])
                )

            for a in ("voltageLevelId", "voltageLevelId1", "voltageLevelId2"):
                if a in elem.attrib:
                    refs_voltagelevel.append((tag_l, stable_id, a, elem.attrib[a]))

            if tag_l == "terminalRef":
                cid = elem.attrib.get("connectableId")
                if cid is not None:
                    terminalref_connectable.append(
                        (elem.attrib.get("id", ""), "connectableId", cid)
                    )

            # numeric parsing checks
            for k, v in elem.attrib.items():
                if k in numeric_attr_names:
                    try:
                        _ = safe_float(v)
                    except Exception as e:
                        numeric_errors_count += 1
                        if len(numeric_errors_examples) < max_issue_examples:
                            numeric_errors_examples.append(
                                {
                                    "tag": tag_l,
                                    "id": stable_id,
                                    "attr": k,
                                    "value": short(v, 220),
                                    "error": str(e),
                                }
                            )
                elif k in int_attr_names:
                    try:
                        _ = safe_int(v)
                    except Exception as e:
                        numeric_errors_count += 1
                        if len(numeric_errors_examples) < max_issue_examples:
                            numeric_errors_examples.append(
                                {
                                    "tag": tag_l,
                                    "id": stable_id,
                                    "attr": k,
                                    "value": short(v, 220),
                                    "error": str(e),
                                }
                            )

            # Context check: elements that should live under a voltageLevel.
            if tag_l in require_voltagelevel_context:
                has_vl_ref = (
                    "voltageLevelId" in elem.attrib
                    or "voltageLevelId1" in elem.attrib
                    or "voltageLevelId2" in elem.attrib
                )
                if current_voltagelevel is None and not has_vl_ref:
                    context_violations_count += 1
                    if len(context_violations_examples) < max_issue_examples:
                        context_violations_examples.append(
                            {
                                "tag": tag_l,
                                "id": stable_id,
                                "substation_ctx": current_substation,
                                "voltageLevel_ctx": current_voltagelevel,
                            }
                        )

            # samples
            if tag_l in sample_specs and len(samples[tag_l]) < samples_per_tag:
                row = {c: elem.attrib.get(c) for c in sample_specs[tag_l]}
                if tag_l not in ("substation", "voltageLevel"):
                    row["substationId"] = elem.attrib.get(
                        "substationId", current_substation
                    )
                    row["voltageLevelId"] = elem.attrib.get(
                        "voltageLevelId", current_voltagelevel
                    )
                samples[tag_l].append(row)

        elif event == "end":
            tag_counts[tag_l] += 1

            if tag_l in ("substation", "voltageLevel"):
                _pop_to(tag_l)
            else:
                if stack:
                    stack.pop()
                    _recompute_context()

            elem.clear()

    # Post-pass resolution checks: references pointing to unknown IDs.
    unresolved_substation = [
        (t, eid, a, r) for (t, eid, a, r) in refs_substation if r not in substation_ids
    ]
    unresolved_voltagelevel = [
        (t, eid, a, r)
        for (t, eid, a, r) in refs_voltagelevel
        if r not in voltagelevel_ids
    ]
    unresolved_terminalref = [
        (tid, a, cid)
        for (tid, a, cid) in terminalref_connectable
        if cid not in connectable_ids
    ]

    # namespaces: split iidm vs non-iidm (extensions)
    iidm_ns_base = None
    if iidm_ns and "schema/iidm" in iidm_ns:
        iidm_ns_base = iidm_ns.split("/schema/iidm", 1)[0] + "/schema/iidm"
    non_iidm_namespaces = []
    for uri in ns_decl_counts.keys():
        if uri == iidm_ns:
            continue
        if iidm_ns_base and uri.startswith(iidm_ns_base):
            # still iidm but different extension path
            non_iidm_namespaces.append(uri)
        elif "schema/iidm" in uri:
            non_iidm_namespaces.append(uri)
        else:
            non_iidm_namespaces.append(uri)
    non_iidm_namespaces = sorted(set(non_iidm_namespaces))

    report: Dict[str, Any] = {
        "run": {
            "timestamp": iso_now(),
        },
        "file": {
            "path": str(xiidm_path),
            "size_bytes": int(file_size),
            "sha256": file_hash,
        },
        "iidm": {
            "root_tag": root_tag_local,
            "namespace": iidm_ns,
            "schema_version_from_namespace": iidm_schema_version,
            "network_meta": network_meta,
        },
        "counts": {
            "tags_total_unique": int(len(tag_counts)),
            "tags_top40": [
                {"tag": t, "count": int(c)} for t, c in tag_counts.most_common(40)
            ],
            "tag_paths_top50_prefixDepth": int(path_prefix_depth),
            "tag_paths_top50": [
                {"path": p, "count": int(c)} for p, c in path_counts.most_common(50)
            ],
            "namespaces_top": [
                {"uri": u, "count": int(c)} for u, c in ns_decl_counts.most_common(50)
            ],
            "non_iidm_namespaces": non_iidm_namespaces,
        },
        "ids": {
            "substations": int(len(substation_ids)),
            "voltageLevels": int(len(voltagelevel_ids)),
            "connectables": int(len(connectable_ids)),
        },
        "topology": {
            "voltageLevel_topologyKind_counts": [
                {"topologyKind": k, "count": int(v)}
                for k, v in topology_kind_counts.most_common()
            ],
        },
        "checks": {
            "reference_resolution": {
                "substationId": {
                    "refs_total": int(len(refs_substation)),
                    "unresolved_count": int(len(unresolved_substation)),
                    "unresolved_examples": unresolved_substation[:max_issue_examples],
                },
                "voltageLevelId": {
                    "refs_total": int(len(refs_voltagelevel)),
                    "unresolved_count": int(len(unresolved_voltagelevel)),
                    "unresolved_examples": unresolved_voltagelevel[:max_issue_examples],
                },
                "terminalRef_connectableId": {
                    "refs_total": int(len(terminalref_connectable)),
                    "unresolved_count": int(len(unresolved_terminalref)),
                    "unresolved_examples": unresolved_terminalref[:max_issue_examples],
                },
            },
            "contextual_containment": {
                "requires_voltageLevel_context": sorted(
                    list(require_voltagelevel_context)
                ),
                "violations_count": int(context_violations_count),
                "violation_examples": context_violations_examples,
            },
            "numeric_parsing": {
                "errors_count": int(numeric_errors_count),
                "error_examples": numeric_errors_examples,
            },
        },
        "samples": {k: v for k, v in samples.items()},
        "attribute_keys": {k: sorted(list(v)) for k, v in attr_keys.items()},
    }

    return report


def write_report_json(report: Dict[str, Any], path: Path) -> None:
    """Persist sanity report JSON to disk, creating parent directories."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)


def read_report_json(path: Path) -> Dict[str, Any]:
    """Load previously persisted sanity report JSON."""
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)
