"""`resize_pod`: change CPU/memory requests and/or limits of one container of ONE running pod in place, through
the `pods/resize` subresource. The pod is not recreated: same name, uid, IP and node (verified live on test,
2026-11-24; docs/MEMORY.md). Whether the container restarts is the container's own `resizePolicy`.

Compared with `set_resources` (which patches the Deployment's template and so rolls every pod), this is for
relief on a running pod without a rollout. It is deliberately narrow:

- One pod per proposal: the one the request names verbatim, or the Deployment's only active pod. A change
  across several pods that failed half way would be neither drift-free nor at target, so a retried apply
  could not tell what to do.
- Supported only when the API server serves `pods/resize` (read live from discovery) and both the API server
  and the pod's kubelet are 1.35 or later (GA; the floor the user chose); all re-checked at apply.
- Chosen only when the request itself asks for an in-place change (`policy.asks_in_place`); a request that
  just names a pod while asking for a Deployment change is planned as `set_resources`.
- Memory only when the container's memory `resizePolicy` is `RestartContainer`. A process that sizes its heap
  at start (the InsightFinder JVMs use -XX:MaxRAMPercentage) never uses a limit raised under `NotRequired`,
  and a limit lowered under it can OOM-kill the running process. The views strip env on purpose, so the
  runtime is not guessed.
- No clamp: a request increase must fit the free capacity of the pod's own node (the kubelet would otherwise
  leave the resize pending as Infeasible or Deferred). A rollout can land elsewhere; this cannot.
- The QoS class cannot change in place (the API rejects it), so such a change is refused up front.

The Deployment's template keeps its old values, so the resize lasts until the pod is replaced; the
persistence hint says where to change it in Git for good.
"""

import re

from ..stepkit import refusal
from ..tools import governance, policy, quantities
from .base import ActionSpec, Operation, target_of
from .set_resources import pick_container, request_over_limit, requested_values

# The floor the user set (2026-11-24): in-place resize is GA from 1.35. 1.33/1.34 serve pods/resize as beta
# (prod 1.34 and eu 1.33 do), with behaviour that differs in places; they are left out until checked.
MIN_VERSION = (1, 35)
_RESIZE_CONDITIONS = ("PodResizePending", "PodResizeInProgress")
_VERSION = re.compile(r"v?(\d+)\.(\d+)")


def kubelet_version(text: str | None) -> tuple[int, int] | None:
    match = _VERSION.match(str(text or ""))
    return (int(match.group(1)), int(match.group(2))) if match else None


def qos_class(resources: dict) -> str:
    """Kubernetes' QoS class from per-container `{requests, limits}` (a request defaults to its limit)."""
    any_set = False
    guaranteed = True
    for res in resources.values():
        requests, limits = res.get("requests") or {}, res.get("limits") or {}
        if requests or limits:
            any_set = True
        for resource in ("cpu", "memory"):
            limit = limits.get(resource)
            request = requests.get(resource, limit)
            if limit is None or request is None or quantities.to_units(resource, request) != quantities.to_units(resource, limit):
                guaranteed = False
    if not any_set:
        return "BestEffort"
    return "Guaranteed" if guaranteed else "Burstable"


def _node_free(node: dict, pods: list[dict], resource: str) -> int:
    allocatable = quantities.to_units(resource, (node.get("allocatable") or {}).get(resource) or "0")
    used = 0
    for pod in pods:
        if pod.get("node_name") != node.get("name") or pod.get("deleting"):
            continue
        for requests in pod.get("requests") or []:
            if requests.get(resource):
                used += quantities.to_units(resource, requests[resource])
    return allocatable - used


def resize_conditions(pod: dict) -> list[dict]:
    return [c for c in pod.get("conditions") or [] if c.get("type") in _RESIZE_CONDITIONS and c.get("status") == "True"]


def _same(resource: str, have, want) -> bool:
    try:
        return have is not None and quantities.to_units(resource, have) == quantities.to_units(resource, want)
    except (ValueError, TypeError):
        return False


def matches(resources: dict | None, approved: dict) -> bool:
    """`resources` (`{requests, limits}`) holds every approved value."""
    resources = resources or {}
    return all(
        _same(resource, (resources.get(side) or {}).get(resource), value)
        for side, values in (approved or {}).items()
        for resource, value in (values or {}).items()
    )


class ResizePod(ActionSpec):
    name = "resize_pod"
    target_kinds = ("Pod",)
    prompt_line = (
        "change CPU and/or memory requests and/or limits of one container of ONE running pod IN PLACE (the pod "
        "is not recreated and the Deployment is not rolled out). Only when the request explicitly asks for an "
        "in-place change / pod resize / no rollout / no restart; naming a pod alone is not enough — otherwise "
        "use set_resources. "
        "Report only the fields the request names, and the pod name exactly as written in the request."
    )
    needs_capacity = True
    rbac_rule = 'patch on "pods/resize" (core API group; the oncallari chart grants it from 0.3.14)'
    fallback = "set_resources"

    def accepts(self, prompt):
        """Only a request that itself asks for an in-place change; naming a pod is not enough (on-call prompts
        quote pod names from health findings while asking for the Deployment's memory to change)."""
        return policy.asks_in_place(prompt)

    # ------------------------------------------------------------ target and state

    async def resolve_target(self, ctx, dep, change):
        pods = await ctx.tool("list_pods")(dep["namespace"], dep.get("label_selector") or dep.get("selector") or None, True)
        own = sorted(p["name"] for p in pods if not p.get("deleting") and governance.deployment_of(p) == dep["name"])
        named = str(change.get("pod") or "").strip()
        if named:
            if named not in own or named not in (ctx.prompt or ""):
                return None, refusal(
                    f"Pod {named!r} is not a running pod of {dep['namespace']}/{dep['name']} named in the request; "
                    "its pods are: " + (", ".join(own) or "none"),
                    pods=own,
                )
            return {"kind": "Pod", "namespace": dep["namespace"], "name": named}, None
        if len(own) == 1:
            return {"kind": "Pod", "namespace": dep["namespace"], "name": own[0]}, None
        return None, refusal(
            f"{dep['namespace']}/{dep['name']} has {len(own)} running pods; an in-place resize changes one pod per "
            "request, so name the pod (or ask for the Deployment's CPU/memory to be changed, which rolls every pod). "
            "Pods: " + (", ".join(own) or "none"),
            pods=own,
        )

    async def load_state(self, ctx, target):
        pod = await ctx.tool("get_pod")(target["namespace"], target["name"])
        support = await ctx.tool("get_resize_support")()
        dep = node = None
        if pod:
            owner = governance.deployment_of(pod)
            if owner:
                dep = await ctx.tool("get_deployment")(target["namespace"], owner)
            node = next((n for n in await ctx.tool("list_nodes")() if n.get("name") == pod.get("node_name")), None)
        return {"pod": pod, "deployment": dep, "node": node, "support": support}

    def target(self, action):
        return {"kind": "Pod", "namespace": action.get("namespace"), "name": action.get("pod")}

    # ------------------------------------------------------------ plan

    def plan(self, ctx, state, change, env):
        pod, node, support = state["pod"], state.get("node"), state.get("support") or {}
        dep = state.get("deployment") or {}
        if pod is None:
            return None, refusal("The pod no longer exists; ask again.")
        unsupported = support_problem(support, node)
        if unsupported:
            return None, refusal(
                f"In-place resize is not available for pod {pod['namespace']}/{pod['name']}: {unsupported}. Ask for "
                "the Deployment's CPU/memory to be changed instead (that rolls its pods).",
                server_version=support.get("server_version"),
                kubelet_version=(node or {}).get("kubelet_version"),
            )
        if pod.get("phase") != "Running" or pod.get("deleting"):
            return None, refusal(f"Pod {pod['namespace']}/{pod['name']} is not running ({pod.get('phase')}); only a running pod is resized in place.")
        busy = resize_conditions(pod)
        if busy:
            first = busy[0]
            return None, refusal(
                f"Pod {pod['namespace']}/{pod['name']} already has a resize under way ({first['type']}"
                + (f", {first.get('reason')}" if first.get("reason") else "")
                + "); ask again once it has finished."
            )

        name, refused = pick_container(change, (pod.get("resources") or {}).keys())
        if refused:
            return None, refused
        requested, refused = requested_values(ctx, change)
        if refused:
            return None, refused
        policy = (pod.get("resize_policy") or {}).get(name) or {"cpu": "NotRequired", "memory": "NotRequired"}
        if any(resource == "memory" for _side, resource in requested) and policy.get("memory") != "RestartContainer":
            return None, refusal(
                f"Container {name} has memory resizePolicy {policy.get('memory')}: a memory change would be applied "
                "to the running process without a restart, which a process that sizes its heap at start (a JVM with "
                "-XX:MaxRAMPercentage, for example) never uses, and a lowered limit can OOM-kill it. Memory is "
                "resized in place only for containers whose memory resizePolicy is RestartContainer. Ask for the "
                "Deployment's memory to be changed instead (that rolls its pods), or set the resizePolicy in the "
                "pod template first.",
                resize_policy=policy,
            )

        current = (pod.get("resources") or {}).get(name) or {}
        for resource in ("cpu", "memory"):
            problem = request_over_limit(resource, current, requested)
            if problem:
                return None, refusal(problem)
        before = {f"{s}.{r}": (current.get(s) or {}).get(r) for (s, r) in requested}
        if all(_same(k.split(".")[1], v, quantities.format_units(k.split(".")[1], requested[tuple(k.split("."))])) for k, v in before.items()):
            return None, refusal("The container already has those values; nothing to change.")

        patch_resources: dict = {}
        for (side, resource), amount in requested.items():
            patch_resources.setdefault(side, {})[resource] = quantities.format_units(resource, amount)

        after_all = {c: dict(r) for c, r in (pod.get("resources") or {}).items()}
        after_all[name] = {side: {**(current.get(side) or {}), **(patch_resources.get(side) or {})} for side in ("requests", "limits")}
        qos_now, qos_after = qos_class(pod.get("resources") or {}), qos_class(after_all)
        if qos_now != qos_after:
            return None, refusal(
                f"That would change the pod's QoS class from {qos_now} to {qos_after}, which an in-place resize cannot "
                "do. Change the values so the class stays the same, or ask for the Deployment's CPU/memory to be "
                "changed instead."
            )

        if node is not None:
            for resource in ("cpu", "memory"):
                new = requested.get(("requests", resource))
                old = (current.get("requests") or {}).get(resource)
                delta = new - quantities.to_units(resource, old) if new is not None and old else (new or 0)
                if delta > 0:
                    free = _node_free(node, env.get("pods") or [], resource)
                    if delta > free:
                        return None, refusal(
                            f"The pod's node {node['name']} has {quantities.format_units(resource, max(free, 0))} "
                            f"{resource} left unrequested; raising the request by {quantities.format_units(resource, delta)} "
                            "would leave the resize pending on that node. Ask for the Deployment's CPU/memory to be "
                            "changed instead (a rollout can place pods on other nodes), or a smaller value.",
                            node=node["name"],
                        )

        changed = sorted({resource for _side, resource in requested})
        restart = [r for r in changed if policy.get(r) == "RestartContainer"]
        applied_fmt = {f"{s}.{r}": quantities.format_units(r, a) for (s, r), a in requested.items()}
        template = next((c["resources"] for c in (dep.get("template") or {}).get("containers") or [] if c["name"] == name), {})
        action = {
            "action": "resize_pod",
            "namespace": pod["namespace"],
            "deployment": dep.get("name"),
            "pod": pod["name"],
            "container": name,
            "resources": patch_resources,
        }
        notes = [
            (
                f"Container {name} restarts in place for this resize (resizePolicy {', '.join(f'{r}: RestartContainer' for r in restart)}); "
                "the pod keeps its name, uid, IP and node."
                if restart
                else f"Container {name} is resized without a restart (resizePolicy NotRequired); the pod keeps its name, uid, IP and node."
            ),
            "The Deployment's pod template is not changed: other pods and new pods keep the template's values.",
        ]
        facts = {
            "before": before,
            "after": applied_fmt,
            "requested": applied_fmt,
            "clamped": [],
            "scale_down": False,
            "high_replicas": False,
            "new_pods": 0,
            "restart": restart,
            "notes": notes,
            "changed_object": {
                "kind": "Deployment",
                "name": dep.get("name"),
                "labels": dep.get("labels") or {},
                "annotations": dep.get("annotations") or {},
            },
            # What would have to change in Git to keep it: the Deployment template's fields.
            "rendered_changes": [
                {
                    "object": f"Deployment {pod['namespace']}/{dep.get('name')} container {name}",
                    "field": f"resources.{k}",
                    "from": (template.get(k.split(".")[0]) or {}).get(k.split(".")[1]),
                    "to": v,
                }
                for k, v in applied_fmt.items()
            ],
            "action_name": (
                f"Resize pod {pod['namespace']}/{pod['name']} ({name}) in place: "
                + ", ".join(f"{k} {before.get(k)} -> {v}" for k, v in applied_fmt.items())
            ),
        }
        return (action, facts), None

    # ------------------------------------------------------------ write

    def operations(self, action):
        resources = {side: dict(values) for side, values in (action.get("resources") or {}).items() if side in ("requests", "limits") and values}
        if not resources:
            raise ValueError("resize_pod needs at least one requests/limits value")
        return [
            Operation(
                verb="patch",
                kind="Pod",
                subresource="resize",
                resource="Pod.Resize",
                name=action["pod"],
                namespace=action.get("namespace"),
                body={"spec": {"containers": [{"name": action["container"], "resources": resources}]}},
                patch_type="strategic",
            )
        ]

    def summarize(self, op, raw, action):
        meta = raw.get("metadata") or {}
        containers = (raw.get("spec") or {}).get("containers") or []
        target = next((c for c in containers if c.get("name") == action.get("container")), {})
        res = target.get("resources") or {}
        return {
            "resource": op.resource,
            "name": meta.get("name"),
            "container": action.get("container"),
            "resources": {
                side: {k: str(v) for k, v in (res.get(side) or {}).items() if k in ("cpu", "memory")}
                for side in ("requests", "limits")
            },
        }

    def patch_action(self, proposal):
        return {
            **{k: proposal.get(k) for k in ("action", "namespace", "deployment", "pod", "container")},
            "resources": proposal["resources"],
        }

    # ------------------------------------------------------------ approval-time checks

    def snapshot(self, state):
        pod = state.get("pod") or {}
        return {
            "pod_uid": pod.get("uid"),
            "node": pod.get("node_name"),
            "container_resources": pod.get("resources"),
            "resize_policy": pod.get("resize_policy"),
        }

    def drift(self, proposal, state):
        snapshot = proposal["snapshot"]
        pod = state.get("pod")
        name = target_of(proposal)["name"]
        if pod is None:
            return [f"pod {name} no longer exists (replaced or removed)"]
        changes = []
        if pod.get("uid") != snapshot.get("pod_uid"):
            changes.append(f"pod {name} was replaced (new uid)")
        if (pod.get("resize_policy") or {}) != (snapshot.get("resize_policy") or {}):
            changes.append("the container resizePolicy changed since the proposal")
        container = proposal.get("container")
        if ((pod.get("resources") or {}).get(container)) != ((snapshot.get("container_resources") or {}).get(container)):
            changes.append(f"container {container} resources changed since the proposal")
        unsupported = support_problem(state.get("support") or {}, state.get("node"))
        if unsupported:
            changes.append(f"in-place resize is no longer available ({unsupported})")
        return changes

    def at_target(self, action, state):
        pod = state.get("pod")
        return bool(pod) and matches((pod.get("resources") or {}).get(action["container"]), action.get("resources"))

    def verify_target(self, action):
        return {"kind": "pod_resources", "pod": action["pod"], "container": action["container"], "resources": action["resources"]}

    async def verify(self, ctx, action, proposal, before, applied_at, max_wait):
        # Under RestartContainer the container restarts once; the verifier waits for that restart (counted from
        # just before this write) so an old container still reporting ready is never taken as the result.
        pod = (before or {}).get("pod") or {}
        policy_ = ((pod.get("resize_policy") or {}).get(action["container"])) or {}
        changed = {r for values in (action.get("resources") or {}).values() for r in (values or {})}
        restarts_before = None
        if before and any(policy_.get(r) == "RestartContainer" for r in changed):
            status = next((c for c in pod.get("containers") or [] if c.get("name") == action["container"]), {})
            restarts_before = status.get("restart_count") or 0
        return await ctx.tool("wait_resize")(
            action["namespace"], action["pod"], action["container"], action["resources"], proposal["snapshot"].get("pod_uid"),
            max_wait, restarts_before,
        )

    def base_tier(self, facts):
        return "medium" if facts.get("restart") else "low"

    def changed_object(self, action, state):
        dep = state.get("deployment") or {}
        return {"labels": dep.get("labels") or {}, "annotations": dep.get("annotations") or {}}

    def lifetime_notice(self, ownership, chain):
        return (
            "An in-place resize changes this one pod only; the Deployment's template keeps its old values, so the "
            "change lasts until this pod is replaced (a rollout, including one from an image update, an eviction or "
            "a node drain), and pods created meanwhile start with the template's values. To keep it, change it "
            "where the Deployment's values come from (see the persistence hint)."
        )


def support_problem(support: dict, node: dict | None) -> str | None:
    """Why this pod cannot be resized in place, or None: the API server must serve pods/resize (live
    discovery) and both it and the pod's kubelet must be at least MIN_VERSION."""
    floor = f"{MIN_VERSION[0]}.{MIN_VERSION[1]}"
    if not support.get("supported"):
        return support.get("reason") or "the API server does not serve pods/resize"
    server = kubelet_version(support.get("server_version"))
    if server is None or server < MIN_VERSION:
        return (
            f"the API server is Kubernetes {support.get('server_version') or 'of unknown version'}; this agent "
            f"resizes pods in place only from {floor} (where the feature is GA)"
        )
    if node is None:
        return "the pod's node could not be read, so its kubelet version is unknown"
    version = kubelet_version(node.get("kubelet_version"))
    if version is None or version < MIN_VERSION:
        return (
            f"its node {node.get('name')} runs kubelet {node.get('kubelet_version') or 'of unknown version'}; this "
            f"agent resizes pods in place only from {floor}"
        )
    return None
