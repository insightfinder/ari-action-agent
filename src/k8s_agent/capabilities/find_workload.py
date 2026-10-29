"""Turns the request into a concrete Deployment (or a namespace/cluster
scope for a health check).

The answer must exist in the cluster: a Deployment the model names that is
not in the live list is discarded, never passed on. When a single
Deployment name appears verbatim in the request, it is used without asking
the model at all. When the match is genuinely ambiguous, candidates are
returned instead of a guess — a wrong target becomes a wrong proposal.
"""

import json
import re

from common.capabilities_base import StepContext
from common.llm_json import extract

from ..stepkit import finish, options

_SENTINEL = "RESOLUTION_JSON:"
_MAX_CANDIDATES = 300
_SYSTEM = (
    "You map a request about a Kubernetes cluster onto the Deployments that actually exist. You never "
    "invent a namespace or Deployment name: every value you return must appear in the list you are given. "
    "If more than one Deployment plausibly matches, say so instead of choosing."
)


def _mentioned(prompt: str, deployments: list[dict]) -> list[dict]:
    text = prompt.lower()
    hits = []
    for d in deployments:
        name = (d.get("name") or "").lower()
        if name and re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", text):
            hits.append(d)
    return hits


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


async def run(ctx: StepContext) -> dict:
    list_deployments = ctx.tool("list_deployments")
    default_ns = ctx.params.get("namespace") or options(ctx).get("namespace")
    deployments = await list_deployments(None)
    known = {(d["namespace"], d["name"]) for d in deployments}
    namespaces = {d["namespace"] for d in deployments}

    hits = _prefer_namespace(_mentioned(ctx.prompt, deployments), ctx.prompt, default_ns)
    # One Deployment named verbatim settles it without the model, unless the prompt uses that name for a
    # namespace: "the pods in the cert-manager namespace" is not about the cert-manager Deployment alone.
    if len(hits) == 1 and not (hits[0]["name"] in namespaces and _names_namespace(ctx.prompt, hits[0]["name"])):
        hit = hits[0]
        return finish(
            {
                "resolution": {"scope": "deployment", "namespace": hit["namespace"], "name": hit["name"]},
                "candidates": [],
                "reasoning": "the Deployment name appears verbatim in the request",
            }
        )

    shortlist = hits or _shortlist(ctx.prompt, deployments, default_ns)
    shortlist_names = ["{}/{}".format(d["namespace"], d["name"]) for d in shortlist]
    user_prompt = (
        f"Request:\n{ctx.prompt}\n\n"
        f"Default namespace (if the request names none): {default_ns or 'none'}\n"
        f"Namespaces that have Deployments: {json.dumps(sorted(namespaces))}\n"
        f"Candidate Deployments (namespace/name): {json.dumps(shortlist_names)}\n\n"
        "Decide what the request is about: one Deployment, a whole namespace, or the whole cluster. "
        f"End your response with a line reading exactly '{_SENTINEL}' followed by ONLY a JSON object:\n"
        '{"scope": "deployment" | "namespace" | "cluster", "namespace": "<namespace or null>", '
        '"name": "<deployment name or null>", "confident": true|false, '
        '"candidates": ["<namespace/name>", ...], "reasoning": "<one line>"}'
    )
    raw = await ctx.tool("llm_complete")(ctx.config["llm"], _SYSTEM, user_prompt)
    try:
        parsed = extract(raw, _SENTINEL)
    except RuntimeError:
        parsed = {}

    scope = parsed.get("scope") if parsed.get("scope") in ("deployment", "namespace", "cluster") else None
    namespace, name = parsed.get("namespace"), parsed.get("name")
    candidates = [c for c in parsed.get("candidates") or [] if isinstance(c, str) and tuple(c.split("/", 1)) in known]

    resolution = None
    if scope == "deployment" and (namespace, name) in known and parsed.get("confident", True):
        resolution = {"scope": "deployment", "namespace": namespace, "name": name}
    elif scope == "namespace" and namespace in namespaces:
        resolution = {"scope": "namespace", "namespace": namespace, "name": None}
    elif scope == "cluster":
        resolution = {"scope": "cluster", "namespace": None, "name": None}
    elif scope == "deployment" and (namespace, name) in known:
        candidates = candidates or [f"{namespace}/{name}"]

    return finish(
        {
            "resolution": resolution,
            "candidates": candidates[:20],
            "reasoning": str(parsed.get("reasoning") or ("no confident match" if resolution is None else "")),
        }
    )
