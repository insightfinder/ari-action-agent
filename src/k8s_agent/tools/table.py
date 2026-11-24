"""Tables over the catalog: the rows a listing question asks for, filtered, sorted and capped in code.

Every row comes from a kind's whitelisted view (`views.py`) reduced to flat columns (`catalog.py` rows), so a
table carries nothing a view does not. `Container` is not an API kind: one row per container of every
active pod, joined with metrics.k8s.io usage — what "CPU and memory limits and usage of every pod" asks for
(stg, 2026-11-19: the health check answered it with the unhealthy pods only).

Nothing here talks to the model. The query it applies has already been checked by `query_resources`.
"""

import math
import re

from . import catalog, governance, quantities

MAX_ROWS = 200
OPS = ("=", "!=", ">", ">=", "<", "<=", "contains", "not_contains")

CONTAINER_COLUMNS = [
    "namespace",
    "pod",
    "container",
    "owner_kind",
    "owner",
    "node",
    "ready",
    "restarts",
    "cpu_request",
    "cpu_limit",
    "cpu_usage",
    "cpu_usage_pct_of_request",
    "cpu_usage_pct_of_limit",
    "memory_request",
    "memory_limit",
    "memory_usage",
    "memory_usage_pct_of_request",
    "memory_usage_pct_of_limit",
]
# Columns holding Kubernetes quantities: compared and sorted by value, not as text.
QUANTITY_COLUMNS = {
    "cpu_request": "cpu",
    "cpu_limit": "cpu",
    "cpu_usage": "cpu",
    "memory_request": "memory",
    "memory_limit": "memory",
    "memory_usage": "memory",
    "cpu_allocatable": "cpu",
    "memory_allocatable": "memory",
    "capacity": "memory",  # storage quantities compare the same way
}
_IDENTITY = ("namespace", "name", "pod", "container")

# ---------------------------------------------------------------- column types (the data contract)

SCHEMA = "k8s.query_result/v1"
# Every column has one of these types, declared here and sent with the table as `column_types`, so a caller
# can render, sort or convert without guessing. Any value may also be null (unknown / not set).
TYPES = {
    "string": "text",
    "integer": "whole number",
    "number": "decimal number",
    "boolean": "true/false",
    "timestamp": "RFC 3339 date-time string",
    "cpu": "Kubernetes CPU quantity string (e.g. 500m, 2); usage is in whole millicores (e.g. 741m)",
    "memory": "Kubernetes memory quantity string (e.g. 512Mi, 2Gi); usage is in whole Mi (e.g. 2488Mi)",
    "storage": "Kubernetes storage quantity string (e.g. 10Gi)",
    "percent": "decimal number, percent (90.0 means 90%); null when either side is unknown",
    "integer_or_string": "whole number or a percentage string (e.g. 1 or 50%)",
}
_BY_COLUMN = {
    **{c: "integer" for c in (
        "restarts", "replicas", "available", "updated", "desired", "current", "misscheduled", "count",
        "current_replicas", "desired_replicas", "disruptions_allowed", "not_ready", "min", "max", "active", "failed",
    )},
    **{c: "boolean" for c in (
        "immutable", "owned_by_scaledobject", "tls", "allow_volume_expansion", "default", "suspend", "paused",
    )},
    **{c: "timestamp" for c in (
        "created_at", "last_seen", "start_time", "completion_time", "last_schedule_time", "last_successful_time",
    )},
    "min_available": "integer_or_string",
    "max_unavailable": "integer_or_string",
    "cpu_request": "cpu", "cpu_limit": "cpu", "cpu_usage": "cpu", "cpu_allocatable": "cpu",
    "memory_request": "memory", "memory_limit": "memory", "memory_usage": "memory", "memory_allocatable": "memory",
    "capacity": "storage",
    "cpu_usage_pct_of_request": "percent", "cpu_usage_pct_of_limit": "percent",
    "memory_usage_pct_of_request": "percent", "memory_usage_pct_of_limit": "percent",
}
# Where the same column name means something else for one kind.
_BY_KIND = {
    ("Container", "ready"): "boolean",
    ("Node", "ready"): "boolean",
    ("DaemonSet", "ready"): "integer",
    ("ReplicaSet", "ready"): "integer",
    ("EndpointSlice", "ready"): "integer",
    ("ControllerRevision", "revision"): "integer",
}


def column_type(kind: str, column: str) -> str:
    return _BY_KIND.get((kind, column)) or _BY_COLUMN.get(column) or "string"


def columns(kind: str) -> list[str]:
    if kind == catalog.CONTAINER:
        return list(CONTAINER_COLUMNS)
    k = catalog.get(kind)
    return list(k.row(k.view({})).keys())


def rows_from_views(kind: str, views_: list[dict]) -> list[dict]:
    k = catalog.get(kind)
    return [k.row(view) for view in views_]


def _pct(used, total) -> float | None:
    if used is None or not total:
        return None
    value = round(100.0 * used / total, 1)
    return value if math.isfinite(value) else None


def _units(resource: str, value) -> int | None:
    try:
        return quantities.to_units(resource, value) if value not in (None, "") else None
    except (ValueError, TypeError):
        return None


def _usage_text(resource: str, units: int | None) -> str | None:
    """Usage as people read it: memory in whole Mi, CPU in whole millicores (metrics report Ki and n)."""
    if units is None:
        return None
    if resource == "memory":
        return f"{round(units / quantities.to_units('memory', '1Mi'))}Mi"
    return quantities.format_units("cpu", units)


def container_rows(pods: list[dict], usage: dict | None) -> list[dict]:
    """One row per container of each active pod (`pods` are pod views); `usage` is
    `read.get_pod_metrics()["usage"]` (None when metrics are unavailable)."""
    rows = []
    for pod in pods:
        if pod.get("phase") in ("Succeeded", "Failed"):
            continue
        statuses = {c.get("name"): c for c in pod.get("containers") or []}
        pod_usage = (usage or {}).get(f"{pod.get('namespace')}/{pod.get('name')}") or {}
        owner = governance.deployment_of(pod)
        for name, res in (pod.get("resources") or {}).items():
            requests, limits = res.get("requests") or {}, res.get("limits") or {}
            used = pod_usage.get(name) or {}
            status = statuses.get(name) or {}
            row = {
                "namespace": pod.get("namespace"),
                "pod": pod.get("name"),
                "container": name,
                "owner_kind": "Deployment" if owner else pod.get("owner_kind"),
                "owner": owner or pod.get("owner_name"),
                "node": pod.get("node_name"),
                "ready": status.get("ready"),
                "restarts": status.get("restart_count"),
            }
            for resource in ("cpu", "memory"):
                req, lim, use = requests.get(resource), limits.get(resource), used.get(resource)
                row[f"{resource}_request"] = req
                row[f"{resource}_limit"] = lim
                row[f"{resource}_usage"] = _usage_text(resource, _units(resource, use))
                row[f"{resource}_usage_pct_of_request"] = _pct(_units(resource, use), _units(resource, req))
                row[f"{resource}_usage_pct_of_limit"] = _pct(_units(resource, use), _units(resource, lim))
            rows.append(row)
    return rows


# ---------------------------------------------------------------- filter, sort, project


def _number(value) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(float(value)) else None
    try:
        number = float(str(value).strip().rstrip("%"))
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _comparable(column: str, value):
    """`(kind, key)`: ("num", float) for quantities and numbers, ("text", str) otherwise, None for empty."""
    if value is None or value == "":
        return None
    if column in QUANTITY_COLUMNS:
        units = _units(QUANTITY_COLUMNS[column], value)
        return ("num", float(units)) if units is not None else ("text", str(value).casefold())
    number = _number(value)
    if number is not None:
        return ("num", number)
    return ("text", str(value).casefold())


def _matches(row: dict, f: dict) -> bool:
    column, op, wanted = f["column"], f["op"], f.get("value")
    have = row.get(column)
    if op in ("contains", "not_contains"):
        found = have is not None and str(wanted).casefold() in str(have).casefold()
        return found if op == "contains" else not found
    a, b = _comparable(column, have), _comparable(column, wanted)
    if op in ("=", "!="):
        if a is None or b is None:
            same = a is None and b is None
        elif a[0] != b[0]:
            same = str(have).casefold() == str(wanted).casefold()
        else:
            same = a[1] == b[1]
        return same if op == "=" else not same
    # Ordering: a missing value never matches, and numbers only compare with numbers.
    if a is None or b is None or a[0] != "num" or b[0] != "num":
        return False
    return {">": a[1] > b[1], ">=": a[1] >= b[1], "<": a[1] < b[1], "<=": a[1] <= b[1]}[op]


def _sort_pass(rows: list[dict], column: str, descending: bool) -> list[dict]:
    """One stable sort by `column`: numbers (by value), then text, then missing values — missing always last,
    whichever the direction."""
    nums, texts, missing = [], [], []
    for row in rows:
        key = _comparable(column, row.get(column))
        (missing if key is None else nums if key[0] == "num" else texts).append((key, row))
    nums.sort(key=lambda kr: kr[0][1], reverse=descending)
    texts.sort(key=lambda kr: kr[0][1], reverse=descending)
    return [row for _key, row in nums + texts + missing]


def apply(kind: str, rows: list[dict], cols: list[str], filters: list[dict], sort: list[dict], select: list[str] | None, limit: int) -> dict:
    """Filter, sort (stable; missing values last), project and cap. Returns the table: `columns` once and
    `rows` as lists, plus how many matched and how many are returned."""
    matched = [r for r in rows if all(_matches(r, f) for f in filters)]
    for spec in reversed(sort):  # last key first, so the first key decides
        matched = _sort_pass(matched, spec["column"], bool(spec.get("desc")))
    shown = [c for c in (select or cols) if c in cols]
    for ident in reversed([c for c in _IDENTITY if c in cols]):
        if ident not in shown:
            shown.insert(0, ident)
    capped = matched[: max(1, min(limit, MAX_ROWS))]
    return {
        "columns": shown,
        "column_types": [column_type(kind, c) for c in shown],
        "rows": [[r.get(c) for c in shown] for r in capped],
        "matched": len(matched),
        "returned": len(capped),
        "truncated": len(capped) < len(matched),
    }


_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
_QUANTITY = re.compile(r"[0-9.]+([numkKMGTPE]i?|e[0-9]+)?")
_PERCENTAGE = re.compile(r"[0-9.]+%")


def conforms(type_: str, value) -> bool:
    """Whether `value` is valid for a declared column type (null always is). Used by the tests and the live
    check that hold every kind's rows to `column_type`."""
    if value is None:
        return True
    if type_ == "string":
        return isinstance(value, str)
    if type_ == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if type_ in ("number", "percent"):
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    if type_ == "boolean":
        return isinstance(value, bool)
    if type_ == "timestamp":
        return isinstance(value, str) and bool(_TIMESTAMP.match(value))
    if type_ in ("cpu", "memory", "storage"):
        return isinstance(value, str) and bool(_QUANTITY.fullmatch(value))
    if type_ == "integer_or_string":
        return (isinstance(value, int) and not isinstance(value, bool)) or (isinstance(value, str) and bool(_PERCENTAGE.fullmatch(value)))
    return False
