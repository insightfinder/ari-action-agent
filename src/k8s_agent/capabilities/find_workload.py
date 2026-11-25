"""Turns the request into a concrete workload — a Deployment, StatefulSet,
DaemonSet or CronJob — (or a namespace/cluster scope for a health check).

The answer must exist in the cluster: a workload the model names that is
not in the live list is discarded, never passed on. When a single
workload name appears verbatim in the request, it is used without asking
the model at all. When the match is genuinely ambiguous, candidates are
returned instead of a guess — a wrong target becomes a wrong proposal.

A Deployment resolves to `{"scope": "deployment", namespace, name}`, as it
always has; a StatefulSet, DaemonSet or CronJob to `{"scope": "workload",
"kind", namespace, name}` (II-25740). A node named verbatim (its full name or
the part before the first dot, e.g. `ip-10-1-21-112`) resolves to
`{"scope": "node", "kind": "Node", "name"}` only when the usual resolution
finds no workload (or only the whole cluster): on-call prompts quote node names
from findings while asking about a workload. A node is only ever looked at
(check_health); this agent does not change nodes.
"""

import json
import re

from common.capabilities_base import StepContext
from common.llm_json import extract

from ..stepkit import finish, options

_SENTINEL = "RESOLUTION_JSON:"
_MAX_CANDIDATES = 300
_SYSTEM = (
    "You map a request about a Kubernetes cluster onto the workloads (Deployments, StatefulSets, DaemonSets, "
    "CronJobs) "
    "that actually exist. You never invent a namespace or workload name: every value you return must appear in "
    "the list you are given. If more than one workload plausibly matches, say so instead of choosing."
)


def _mentioned(prompt: str, deployments: list[dict]) -> list[dict]:
    text = prompt.lower()
    hits = []
    for d in deployments:
        name = (d.get("name") or "").lower()
        if name and re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", text):
            hits.append(d)
    return hits


# Kubernetes' safe-encoding alphabet: a Deployment's pods are `<deployment>-<pod-template-hash>-<suffix>`.
_GENERATED = "[bcdfghjklmnpqrstvwxz2456789]"


# A pod's name after its workload's: `<deployment>-<pod-template-hash>-<suffix>`, `<statefulset>-<ordinal>`,
# `<daemonset>-<suffix>`, `<cronjob>-<scheduled minute>-<suffix>` (a scheduled run's pod).
_POD_SUFFIX = {
    "Deployment": rf"-{_GENERATED}{{6,10}}-{_GENERATED}{{5}}",
    "StatefulSet": r"-\d+",
    "DaemonSet": rf"-{_GENERATED}{{5}}",
    "CronJob": rf"-\d{{8,}}-{_GENERATED}{{5}}",
}


def _pods_mentioned(prompt: str, workloads: list[dict]) -> list[dict]:
    """Workloads one of whose pods the request names verbatim ("resize pod checkout-7d9f5c6b8-x2k4q")."""
    text = prompt.lower()
    hits = []
    for d in workloads:
        name = (d.get("name") or "").lower()
        pattern = rf"(?<![\w-]){re.escape(name)}{_POD_SUFFIX[d.get('kind') or 'Deployment']}(?![\w-])"
        if name and re.search(pattern, text):
            hits.append(d)
    return hits


def _label(w: dict) -> str:
    """How a workload is shown to the model: `namespace/name` for a Deployment, as before, plus the kind for
    the others."""
    text = f"{w['namespace']}/{w['name']}"
    return text if (w.get("kind") or "Deployment") == "Deployment" else f"{text} ({w['kind']})"


def _resolution(w: dict) -> dict:
    if (w.get("kind") or "Deployment") == "Deployment":
        return {"scope": "deployment", "namespace": w["namespace"], "name": w["name"]}
    return {"scope": "workload", "kind": w["kind"], "namespace": w["namespace"], "name": w["name"]}


def _prefer_namespace(hits: list[dict], prompt: str, default_ns: str | None) -> list[dict]:
    if len(hits) <= 1:
        return hits
    text = prompt.lower()
    # Only where the prompt uses it as a namespace: a namespace called "it" or "web" is also a word.
    named = [
        h for h in hits
        if h.get("namespace")
        and (
            _names_namespace(prompt, h["namespace"])
            or re.search(rf"(?<![\w-]){re.escape(h['namespace'].lower())}/{re.escape(h['name'].lower())}(?![\w-])", text)
        )
    ]
    if len(named) == 1:
        return named
    if default_ns:
        in_default = [h for h in hits if h.get("namespace") == default_ns]
        if len(in_default) == 1:
            return in_default
    return hits


def _names_namespace(prompt: str, name: str) -> bool:
    """Whether the prompt calls `name` a namespace ("the cert-manager namespace", "namespace cert-manager",
    "-n cert-manager"), so a Deployment of the same name is not necessarily what it is about."""
    n = re.escape(name.lower())
    word = r"(?:(?<![\w-])(?:namespace|ns)\b|命名空间)"
    patterns = (
        rf"(?<![\w-]){n}(?![\w-])\s*{word}",
        rf"{word}\s*[:=]?\s*{n}(?![\w-])",
        rf"(?<![\w-])-n\s+{n}(?![\w-])",
    )
    text = prompt.lower()
    return any(re.search(p, text) for p in patterns)


def _shortlist(prompt: str, deployments: list[dict], default_ns: str | None) -> list[dict]:
    words = {w for w in re.findall(r"[a-z0-9][a-z0-9-]{2,}", prompt.lower())}
    scored = []
    for d in deployments:
        name = (d.get("name") or "").lower()
        score = sum(1 for w in words if w in name)
        if d.get("namespace") == default_ns:
            score += 0.5
        if score:
            scored.append((score, d))
    scored.sort(key=lambda s: -s[0])
    shortlist = [d for _, d in scored[:_MAX_CANDIDATES]]
    if not shortlist and default_ns:
        shortlist = [d for d in deployments if d.get("namespace") == default_ns][:_MAX_CANDIDATES]
    return shortlist


def _nodes_mentioned(prompt: str, nodes: list[dict]) -> list[dict]:
    text = prompt.lower()
    hits = []
    for n in nodes:
        name = (n.get("name") or "").lower()
        short = name.split(".", 1)[0]
        if name and any(re.search(rf"(?<![\w.-]){re.escape(form)}(?![\w-]|\.\w)", text) for form in {name, short}):
            hits.append(n)
    return hits


def _node_resolution(node: dict, reasoning: str) -> dict:
    return finish(
        {"resolution": {"scope": "node", "kind": "Node", "namespace": None, "name": node["name"]}, "candidates": [], "reasoning": reasoning}
    )


async def run(ctx: StepContext) -> dict:
    default_ns = ctx.params.get("namespace") or options(ctx).get("namespace")
    workloads = await ctx.tool("list_workloads")(None)
    by_label = {_label(w): w for w in workloads}
    namespaces = {w["namespace"] for w in workloads}

    hits = _prefer_namespace(_mentioned(ctx.prompt, workloads) or _pods_mentioned(ctx.prompt, workloads), ctx.prompt, default_ns)
    nodes = _nodes_mentioned(ctx.prompt, await ctx.tool("list_nodes")())
    node = nodes[0] if len(nodes) == 1 else None
    # One workload named verbatim settles it without the model, unless the prompt uses that name for a
    # namespace: "the pods in the cert-manager namespace" is not about the cert-manager Deployment alone.
    if len(hits) == 1 and not (hits[0]["name"] in namespaces and _names_namespace(ctx.prompt, hits[0]["name"])):
        return finish(
            {
                "resolution": _resolution(hits[0]),
                "candidates": [],
                "reasoning": "the workload name, or one of its pods' names, appears verbatim in the request",
            }
        )

    shortlist = hits or _shortlist(ctx.prompt, workloads, default_ns)
    user_prompt = (
        f"Request:\n{ctx.prompt}\n\n"
        f"Default namespace (if the request names none): {default_ns or 'none'}\n"
        f"Namespaces that have workloads: {json.dumps(sorted(namespaces))}\n"
        "Candidate workloads (namespace/name; a StatefulSet, DaemonSet or CronJob has its kind in parentheses): "
        f"{json.dumps([_label(w) for w in shortlist])}\n\n"
        "Decide what the request is about: one workload, a whole namespace, or the whole cluster. "
        f"End your response with a line reading exactly '{_SENTINEL}' followed by ONLY a JSON object:\n"
        '{"scope": "deployment" | "workload" | "namespace" | "cluster", '
        '"kind": "Deployment" | "StatefulSet" | "DaemonSet" | "CronJob" | null, "namespace": "<namespace or null>", '
        '"name": "<workload name or null>", "confident": true|false, '
        '"candidates": ["<candidate exactly as listed>", ...], "reasoning": "<one line>"}\n'
        'Use scope "deployment" for a Deployment and "workload" (with its kind) for a StatefulSet, DaemonSet or '
        "CronJob."
    )
    raw = await ctx.tool("llm_complete")(ctx.config["llm"], _SYSTEM, user_prompt)
    try:
        parsed = extract(raw, _SENTINEL)
    except RuntimeError:
        parsed = {}

    scope = parsed.get("scope") if parsed.get("scope") in ("deployment", "workload", "namespace", "cluster") else None
    namespace, name = parsed.get("namespace"), parsed.get("name")
    kind = parsed.get("kind") if parsed.get("kind") in ("StatefulSet", "DaemonSet", "CronJob") and scope == "workload" else "Deployment"
    chosen = by_label.get(_label({"kind": kind, "namespace": namespace, "name": name})) if namespace and name else None
    candidates = [c for c in parsed.get("candidates") or [] if isinstance(c, str) and c in by_label]

    resolution = None
    if scope in ("deployment", "workload") and chosen and parsed.get("confident", True):
        resolution = _resolution(chosen)
    elif scope == "namespace" and namespace in namespaces:
        resolution = {"scope": "namespace", "namespace": namespace, "name": None}
    elif scope == "cluster":
        resolution = {"scope": "cluster", "namespace": None, "name": None}
    elif scope in ("deployment", "workload") and chosen:
        candidates = candidates or [_label(chosen)]
    if node and ((resolution is None and not candidates) or (resolution or {}).get("scope") == "cluster"):
        # No workload found: the node the request names is what it is about ("check node ip-10-1-21-112").
        return _node_resolution(node, "no workload matched; the node's name appears verbatim in the request")

    return finish(
        {
            "resolution": resolution,
            "candidates": candidates[:20],
            "reasoning": str(parsed.get("reasoning") or ("no confident match" if resolution is None else "")),
        }
    )
