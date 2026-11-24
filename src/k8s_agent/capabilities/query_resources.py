"""Lists any kind this agent can read as a table: which objects, which columns, filtered, sorted, capped.
Read-only.

The model only proposes the query (kind, namespace, label selector, filters, sort, columns, limit); code
checks every part against the request and the live cluster before anything is read, and the result echoes
the query as applied plus whatever was ignored and why. A partial list presented as complete is the failure
this exists to prevent (stg, 2026-11-19: "list all pods with cpu and memory limit, usage for insightfinder"
got the unhealthy pods only), so:

- a namespace, label selector or filter value the request does not itself state is not applied (it would
  narrow the list on the model's guess); what was dropped is listed in `ignored`;
- a kind the agent may not read (HTTP 403) or that the cluster does not serve is a gap, never an empty table;
- the table says how many rows matched and how many are returned (at most `table.MAX_ROWS`).

Secrets are refused outright (catalog.refused_kind).
"""

import difflib
import re

from common.capabilities_base import StepContext
from common.llm_json import extract

from ..stepkit import finish, options, refusal
from ..tools import catalog, read, table

_SENTINEL = "QUERY_JSON:"
_SYSTEM = (
    "You turn a request to list or show Kubernetes objects into a table query. You only choose among the "
    "kinds and columns you are given and never invent a namespace, label, name or number the request does "
    "not state."
)


def _kinds_prompt() -> str:
    lines = []
    for kind in catalog.queryable():
        lines.append(f"- {kind}: {', '.join(table.columns(kind))}")
    return "\n".join(lines)


async def _extract(ctx: StepContext) -> dict:
    user_prompt = (
        f"Request:\n{ctx.prompt}\n\n"
        "Kinds you can list, with their columns:\n"
        f"{_kinds_prompt()}\n\n"
        "Use `Container` for per-container CPU/memory requests, limits and usage (one row per container of "
        "each running pod), `Pod` for pod status. Secrets cannot be listed.\n"
        f"End your response with a line reading exactly '{_SENTINEL}' followed by ONLY a JSON object:\n"
        '{"kind": "<one of the kinds>", "namespace": "<namespace the request names, or null for all>", '
        '"label_selector": "<k=v[,k2=v2] exactly as written in the request, or null>", '
        '"filters": [{"column": "<column>", "op": "= | != | > | >= | < | <= | contains | not_contains", '
        '"value": "<value as written in the request>"}], '
        '"sort": [{"column": "<column>", "desc": true|false}], '
        '"columns": ["<column>", ...] or null for all, "limit": <int the request states, or null>, '
        '"reasoning": "<one line>"}'
    )
    raw = await ctx.tool("llm_complete")(ctx.config["llm"], _SYSTEM, user_prompt)
    try:
        return extract(raw, _SENTINEL)
    except RuntimeError:
        return {}


def _in_prompt(value, prompt: str) -> bool:
    text = str(value if value is not None else "").strip()
    return bool(text) and re.sub(r"\s+", " ", text).casefold() in re.sub(r"\s+", " ", prompt or "").casefold()


def _word_in_prompt(word: str, prompt: str) -> bool:
    return bool(word) and re.search(rf"(?<![\w-]){re.escape(word.lower())}(?![\w-])", (prompt or "").lower()) is not None


def _number_text(value) -> str:
    if isinstance(value, float) and value == int(value):
        return str(int(value))
    return str(value)


async def run(ctx: StepContext) -> dict:
    query = await _extract(ctx)
    asked = query.get("kind")
    if catalog.refused_kind(asked):
        return refusal("Secrets are never read or listed by this agent: Kubernetes returns their values with every list.")
    kind = catalog.resolve(asked)
    if kind is None:
        return refusal(
            f"What to list is not clear ({asked!r} is not a kind this agent can list). Kinds: "
            + ", ".join(catalog.queryable()),
            kinds=catalog.queryable(),
        )
    cols = table.columns(kind)
    ignored: list[str] = []
    notes: list[str] = []
    gaps: list[str] = []

    # ---- namespace: stated in the request, or given by the caller; never the model's guess
    namespaced = kind == catalog.CONTAINER or catalog.get(kind).namespaced
    namespace = None
    candidate = query.get("namespace")
    given = ctx.params.get("namespace") or options(ctx).get("namespace")
    if namespaced:
        if candidate and (_word_in_prompt(str(candidate), ctx.prompt) or candidate == ctx.params.get("namespace")):
            namespace = str(candidate)
        elif candidate:
            ignored.append(f"namespace {candidate!r} (not stated in the request)")
        if namespace is None and given:
            namespace = str(given)
            notes.append(f"namespace {namespace} is the caller's default; the request names none")
        if namespace:
            names = await ctx.tool("namespace_names")()
            if names is not None and namespace not in names:
                close = difflib.get_close_matches(namespace, names, n=5, cutoff=0.6) or [
                    n for n in names if namespace.lower() in n.lower() or n.lower() in namespace.lower()
                ]
                return refusal(
                    f"Namespace {namespace!r} does not exist in this cluster."
                    + (f" Similar: {', '.join(close)}." if close else ""),
                    namespaces=names if len(names) <= 100 else names[:100],
                )
            if names is None:
                notes.append("namespaces could not be listed (not permitted), so the namespace name was not checked")

    # ---- label selector: only as written in the request
    selector = query.get("label_selector")
    if selector and not (re.fullmatch(r"[\w./-]+(!?=[\w./-]*)?(,[\w./-]+(!?=[\w./-]*)?)*", str(selector)) and _in_prompt(selector, ctx.prompt)):
        ignored.append(f"label selector {selector!r} (not written that way in the request)")
        selector = None

    # ---- filters, sort, columns, limit
    filters = []
    for f in query.get("filters") or []:
        if not isinstance(f, dict):
            continue
        column, op, value = f.get("column"), f.get("op"), f.get("value")
        if column not in cols or op not in table.OPS:
            ignored.append(f"filter {column} {op} {value!r} (unknown column or operator)")
        elif not _in_prompt(_number_text(value), ctx.prompt):
            ignored.append(f"filter {column} {op} {value!r} (the value is not stated in the request)")
        else:
            filters.append({"column": column, "op": op, "value": value})
    sort = [
        {"column": s["column"], "desc": bool(s.get("desc"))}
        for s in query.get("sort") or []
        if isinstance(s, dict) and s.get("column") in cols
    ]
    select = [c for c in query.get("columns") or [] if c in cols] or None
    limit = table.MAX_ROWS
    raw_limit = query.get("limit")
    if isinstance(raw_limit, (int, float)) and not isinstance(raw_limit, bool) and raw_limit >= 1:
        if _in_prompt(_number_text(raw_limit), ctx.prompt):
            limit = min(int(raw_limit), table.MAX_ROWS)
        else:
            ignored.append(f"limit {raw_limit} (not stated in the request)")

    # ---- read
    try:
        if kind == catalog.CONTAINER:
            pods = await ctx.tool("list_views")("Pod", namespace, selector)
            metrics = await ctx.tool("get_pod_metrics")(namespace)
            if metrics.get("gap"):
                gaps.append(f"usage columns are empty: {metrics['gap']}")
            rows = table.container_rows(pods, metrics.get("usage"))
            notes.append("one row per container of each pod that is not Succeeded/Failed")
        else:
            rows = table.rows_from_views(kind, await ctx.tool("list_views")(kind, namespace, selector))
    except (read.ReadForbidden, read.KindNotServed) as exc:
        gaps.append(str(exc))
        rows = []

    result = table.apply(kind, rows, cols, filters, sort, select, limit)
    if not rows and not gaps:
        notes.append("nothing of this kind matched" + (f" in namespace {namespace}" if namespace else ""))
    return finish(
        {
            "table": {
                "schema": table.SCHEMA,
                "kind": kind,
                "query": {
                    "namespace": namespace if namespaced else None,
                    "all_namespaces": namespaced and namespace is None,
                    "label_selector": selector,
                    "filters": filters,
                    "sort": sort,
                    "limit": limit,
                },
                "ignored": ignored,
                **result,
                "gaps": gaps,
                "notes": notes,
            }
        }
    )
