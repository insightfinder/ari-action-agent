"""Namespaces and workloads this agent never changes, whatever a request or an approval says.

Decided 2026-11-26 (nothing that could break a product environment): the cluster's own system namespaces, and
the workloads the agent itself and its approval path run on. The agent runs in the same namespace as the
services it changes (`insightfinder`), so it and its approval path are protected by name, not by namespace.

The oncallari chart sets both lists from its values (the same values also render a ValidatingAdmissionPolicy
that has the API server deny these writes to the agent's ServiceAccount, so a bug here is not the last line):

- `K8S_AGENT_PROTECTED_NAMESPACES`: comma-separated namespaces.
- `K8S_AGENT_PROTECTED_WORKLOADS`: comma-separated `namespace/name`; a name ending in `*` is a prefix.
  The chart always adds the agent's own API and worker Deployments.

When a variable is not set at all (a run outside the chart), the defaults below apply: the system namespaces,
and no workload (their names depend on the installation); set to an empty string it means none. Checked for
the Deployment a change lands on (a pod's and an autoscaler's included), in `propose_change` and again in
`apply_approved_change`.
"""

import os

DEFAULT_NAMESPACES = (
    "kube-system",
    "kube-public",
    "kube-node-lease",
    "flux-system",
    "cert-manager",
    "vault",
    "keda",
    "traefik",
)
# Workload names depend on the installation: the chart lists them (its own API and worker Deployments always).
DEFAULT_WORKLOADS: tuple[str, ...] = ()


def _list(variable: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.environ.get(variable)
    if raw is None:
        return default
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def protected_namespaces() -> tuple[str, ...]:
    return _list("K8S_AGENT_PROTECTED_NAMESPACES", DEFAULT_NAMESPACES)


def protected_workloads() -> tuple[str, ...]:
    return _list("K8S_AGENT_PROTECTED_WORKLOADS", DEFAULT_WORKLOADS)


def _matches(pattern: str, namespace: str, name: str) -> bool:
    ns, _, wanted = pattern.partition("/")
    if ns != namespace or not wanted:
        return False
    return name.startswith(wanted[:-1]) if wanted.endswith("*") else name == wanted


def why_protected(namespace: str | None, name: str | None) -> str | None:
    """Why the workload `namespace/name` may not be changed, or None when it may."""
    if namespace in protected_namespaces():
        return (
            f"Namespace {namespace} is protected: this agent changes nothing in it (system components). "
            "Nothing was proposed; use kubectl if a change is really needed."
        )
    for pattern in protected_workloads():
        if name and namespace and _matches(pattern, namespace, name):
            return (
                f"{namespace}/{name} is protected: it runs this agent or its approval path ({pattern}), and this "
                "agent never changes it. Nothing was proposed; use kubectl if a change is really needed."
            )
    return None
