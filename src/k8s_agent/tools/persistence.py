"""Where a change would have to be made in Git to last — read from the
cluster, never assumed.

The agent knows nothing about any GitOps repository, organisation, layout
or chart convention: those change (a cluster can move to a new repo, a
repo can be restructured) and a hard-coded answer would silently go
stale. Everything here comes from the Flux objects the cluster is
actually reconciling right now, and every hint says which commit it was
read at.

The chain, each link read from the previous object's labels/refs:

    changed object --helm.toolkit.fluxcd.io/*--> HelmRelease
                   --kustomize.toolkit.fluxcd.io/*--> Kustomization
                   --spec.sourceRef--> GitRepository / OCIRepository / Bucket

It deliberately stops there. Which file, and which chart values key,
renders the field cannot be read from the cluster without reading the
values themselves — and those (inline `spec.values`, or the `valuesFrom`
ConfigMaps) can hold plaintext secrets, so they are never read. What it
does add for whoever writes the Git change (a person, or a code agent the
on-call coordinator hands this to): the order the values sources are
merged in, each source's `targetPath`, where the chart itself comes from
(to look the values key up), the repository as plain coordinates, and
whether the change has settled enough to be worth persisting at all.
"""

import re
from urllib.parse import urlsplit

from .redact import strip_url_credentials

VALUES_PRECEDENCE = (
    "Flux merges these in the order listed: each valuesFrom entry overrides the ones before it, and inline "
    "spec.values (listed last, when present) overrides all of them. Change the key in the last source that sets "
    "it; a value written to an earlier source is overridden by any later source that also sets the key. A "
    "source with a target_path supplies exactly that one key."
)

_SCP_URL = re.compile(r"^(?:[\w.-]+@)?([\w.-]+):(?!//)(.+)$")
_COMMIT = re.compile(r"(?:sha1|sha256):([0-9a-f]{7,64})$|/([0-9a-f]{40})$")


def _ref_text(ref: dict) -> str | None:
    return next((f"{k}:{ref[k]}" for k in ("branch", "tag", "semver", "name", "commit") if ref.get(k)), None)


def helmrelease_summary(raw: dict, upgrade_activity: dict | None = None) -> dict:
    meta = raw.get("metadata") or {}
    spec = raw.get("spec") or {}
    chart_spec = (spec.get("chart") or {}).get("spec") or {}
    chart_ref = spec.get("chartRef") or {}
    values_sources = [
        {
            "kind": v.get("kind"),
            "name": v.get("name"),
            "values_key": v.get("valuesKey"),
            "target_path": v.get("targetPath"),
            "optional": bool(v.get("optional")),
        }
        for v in spec.get("valuesFrom") or []
    ]
    # Report that inline values exist, never what they are. Last, because
    # that is where Flux merges them.
    if spec.get("values"):
        values_sources.append(
            {"kind": "inline", "name": "spec.values", "values_key": None, "target_path": None, "optional": False}
        )
    labels = meta.get("labels") or {}
    source_ref = chart_spec.get("sourceRef") or chart_ref
    return {
        "name": meta.get("name"),
        "namespace": meta.get("namespace"),
        "chart": chart_spec.get("chart") or chart_ref.get("name"),
        "chart_version": chart_spec.get("version") or (raw.get("status") or {}).get("lastAttemptedRevision"),
        # `url`/`type`/`ref` are filled in by read.get_flux_chain from the
        # source object itself, when it can be read.
        "chart_source": {
            "kind": source_ref.get("kind"),
            "name": source_ref.get("name"),
            "namespace": source_ref.get("namespace") or meta.get("namespace"),
        },
        "values_sources": values_sources,
        "kustomization_ref": {
            "name": labels.get("kustomize.toolkit.fluxcd.io/name"),
            "namespace": labels.get("kustomize.toolkit.fluxcd.io/namespace"),
        },
        "upgrade_activity": upgrade_activity or {},
    }


def kustomization_summary(raw: dict) -> dict:
    meta = raw.get("metadata") or {}
    spec = raw.get("spec") or {}
    status = raw.get("status") or {}
    source = spec.get("sourceRef") or {}
    return {
        "name": meta.get("name"),
        "namespace": meta.get("namespace"),
        "path": spec.get("path"),
        "interval": spec.get("interval"),
        "applied_revision": status.get("lastAppliedRevision"),
        "source_ref": {
            "kind": source.get("kind"),
            "name": source.get("name"),
            "namespace": source.get("namespace") or meta.get("namespace"),
        },
    }


def source_summary(raw: dict, kind: str) -> dict:
    meta = raw.get("metadata") or {}
    spec = raw.get("spec") or {}
    ref = spec.get("ref") or {}
    return {
        "kind": kind,
        "name": meta.get("name"),
        "namespace": meta.get("namespace"),
        "url": strip_url_credentials(spec.get("url")),
        "ref": _ref_text(ref),
        "branch": ref.get("branch"),
        "artifact_revision": ((raw.get("status") or {}).get("artifact") or {}).get("revision"),
    }


def chart_source_details(raw: dict) -> dict:
    """Where the chart comes from (a HelmRepository, or a Git/OCI/Bucket
    source holding the chart): enough to look up which values key renders a
    field, when the values files do not already set it."""
    spec = raw.get("spec") or {}
    return {
        "url": strip_url_credentials(spec.get("url")),
        "type": spec.get("type") or ("oci" if str(spec.get("url") or "").startswith("oci://") else None),
        "ref": _ref_text(spec.get("ref") or {}),
    }


def _commit(revision: str | None) -> str | None:
    match = _COMMIT.search(revision or "")
    return (match.group(1) or match.group(2)) if match else None


def git_coordinates(source: dict | None, revision: str | None) -> dict | None:
    """The Git source as plain fields a caller can hand to a code agent
    without parsing anything: `owner/repo`, an https URL, the branch Flux
    tracks and the commit it applied. None for a non-Git source or a URL
    that cannot be split into host and path."""
    if not source or source.get("kind") != "GitRepository" or not source.get("url"):
        return None
    url = source["url"]
    parts = urlsplit(url)
    if parts.scheme and parts.hostname:
        host, path = parts.hostname, parts.path
    else:
        match = _SCP_URL.match(url)
        if not match:
            return None
        host, path = match.group(1), match.group(2)
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    if not path:
        return None
    return {
        "host": host,
        "repo_slug": path,
        "https_url": f"https://{host}/{path}",
        "branch": source.get("branch"),
        "commit": _commit(revision),
    }


def _manifest_value(value):
    """A value as it is written in a manifest. Numbers become strings: A2A
    artifacts carry numbers as protobuf doubles, so a replica count of 2
    would otherwise reach whoever writes the Git change as `2.0`."""
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def manifest_changes(changes: list[dict]) -> list[dict]:
    return [{**c, "from": _manifest_value(c.get("from")), "to": _manifest_value(c.get("to"))} for c in changes or []]


def readiness(outcome: str | None) -> dict:
    """Whether the change is worth persisting yet, from the apply's
    verification status (None while it is only proposed)."""
    if outcome is None:
        return {
            "ready_to_persist": False,
            "readiness_reason": "Not applied yet. Persist it only after this proposal is approved and the resulting "
            "action_result.status is 'succeeded'.",
        }
    if outcome == "succeeded":
        return {
            "ready_to_persist": True,
            "readiness_reason": "The change is live and verified; persisting it keeps it past the next reconcile or upgrade.",
        }
    if outcome == "in_progress":
        return {
            "ready_to_persist": False,
            "readiness_reason": "The rollout had not finished when the wait ended. Confirm the Deployment is healthy "
            "before persisting the change.",
        }
    return {
        "ready_to_persist": False,
        "readiness_reason": f"The change did not settle (status {outcome}); persisting it would make the problem "
        "permanent. Resolve it first.",
    }


def build_hint(chain: dict, changes: list[dict], outcome: str | None = None) -> dict:
    """The persistence hint shown in the proposal and the result.

    `chain` is what `read.get_flux_chain` resolved (each link may be None,
    with `broken_at` saying where it stopped). `changes` are the rendered
    fields this change touches, as `{object, field, from, to}` (values are
    rendered as manifest strings). `outcome` is the apply's verification
    status, None for a proposal.
    """
    changes = manifest_changes(changes)
    ownership = chain.get("ownership") or {}
    if ownership.get("class") == "unowned":
        return {
            "available": False,
            "reason": "This object has no Flux or Helm owner, so there is no Git source to update.",
            "changes": changes,
            "ready_to_persist": False,
            "readiness_reason": "Nothing to persist: the object is not reconciled from Git.",
        }
    helmrelease = chain.get("helmrelease")
    kustomization = chain.get("kustomization")
    source = chain.get("source")
    revision = (kustomization or {}).get("applied_revision") or (source or {}).get("artifact_revision")
    hint = {
        "available": bool(source),
        "as_of_revision": revision,
        "source": source,
        "git": git_coordinates(source, revision),
        "directory": (kustomization or {}).get("path"),
        "kustomization": {k: (kustomization or {}).get(k) for k in ("name", "namespace")} if kustomization else None,
        "helmrelease": (
            {
                **{k: helmrelease.get(k) for k in ("name", "namespace", "chart", "chart_version", "chart_source", "values_sources")},
                "values_precedence": VALUES_PRECEDENCE,
            }
            if helmrelease
            else None
        ),
        "changes": changes,
        "broken_at": chain.get("broken_at"),
        **readiness(outcome),
    }
    if helmrelease:
        chart_url = (helmrelease.get("chart_source") or {}).get("url")
        hint["instruction"] = (
            f"In {source.get('url') if source else 'the source repository'} at "
            f"{revision or 'its current revision'}, under {hint['directory'] or 'the Kustomization path'}, find the "
            f"values used by HelmRelease {helmrelease.get('name')} (sources listed above, merged in that order) "
            "and change the value that renders each field listed in `changes`, in the last source that sets it. "
            "If no source sets the key (the chart default applies), add it to the source that already holds "
            "this workload's other settings, provided no later source sets it, using the key path chart "
            f"{helmrelease.get('chart')} {helmrelease.get('chart_version') or ''} defines"
            + (f" (chart source: {chart_url})" if chart_url else "")
            + ". Change nothing else."
        )
    elif kustomization:
        hint["instruction"] = (
            f"In {source.get('url') if source else 'the source repository'} at {revision or 'its current revision'}, "
            f"under {hint['directory'] or 'the Kustomization path'}, change the manifest field(s) listed in `changes`."
        )
    if chain.get("broken_at"):
        hint["note"] = f"Could not follow the ownership chain past: {chain['broken_at']}. Only what was read is shown."
    return hint
